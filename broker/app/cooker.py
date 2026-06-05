"""Thin bridge to the existing tools/gdtf_cooker.py.

We REUSE the cooker's GDTF Share client, GDTF parser, and JSON writer rather
than reimplementing any of it — the broker is just orchestration around the
tooling that already produces the catalog.
"""
import os
import sys
import time
import json
import threading

from . import config

# Make `import gdtf_cooker` resolve to <catalog>/tools/gdtf_cooker.py.
if config.TOOLS_DIR not in sys.path:
    sys.path.insert(0, config.TOOLS_DIR)

import gdtf_cooker  # noqa: E402  (path set above)

# Keep downloaded .gdtf files on the data volume, NOT in the repo working tree
# (otherwise they'd show up in git status / risk being committed).
gdtf_cooker.GdtfShareClient.CACHE_DIR = os.path.join(config.DATA_DIR, "gdtf_cache")

_client = None
_client_lock = threading.Lock()
_list_cache = None          # in-memory list of fixtures
_list_fetched_at = 0.0


def _get_client():
    """Return a logged-in GdtfShareClient (lazy, shared, re-auth on demand)."""
    global _client
    with _client_lock:
        if _client and _client.authenticated:
            return _client
        if not config.GDTF_USER or not config.GDTF_PASS:
            raise RuntimeError("GDTF Share credentials not configured")
        c = gdtf_cooker.GdtfShareClient()
        if not c.login(config.GDTF_USER, config.GDTF_PASS):
            raise RuntimeError("GDTF Share login failed")
        _client = c
        return _client


def _load_disk_cache():
    if os.path.exists(config.CACHE_PATH):
        try:
            with open(config.CACHE_PATH) as f:
                blob = json.load(f)
            return blob.get("fetched_at", 0), blob.get("list", [])
        except Exception:
            pass
    return 0, []


def _save_disk_cache(lst):
    try:
        with open(config.CACHE_PATH, "w") as f:
            json.dump({"fetched_at": time.time(), "list": lst}, f)
    except Exception:
        pass


def get_list(force=False):
    """Cached GDTF Share fixture list. Refreshes at most every LIST_TTL."""
    global _list_cache, _list_fetched_at
    now = time.time()
    if _list_cache is None:
        _list_fetched_at, _list_cache = _load_disk_cache()
    fresh = (now - _list_fetched_at) < config.LIST_TTL_SECONDS
    if _list_cache and fresh and not force:
        return _list_cache
    # Refresh from Share.
    lst = _get_client().get_fixture_list()
    if lst:
        _list_cache = lst
        _list_fetched_at = now
        _save_disk_cache(lst)
    return _list_cache or []


_last_force = 0.0


def force_refresh(min_interval=60):
    """Force a re-fetch of the GDTF Share list (bypasses the 24h cache), so a
    just-uploaded fixture shows up. Rate-limited so it can't be hammered."""
    global _last_force
    now = time.time()
    if now - _last_force < min_interval:
        return {"refreshed": False, "count": len(get_list()),
                "wait": int(min_interval - (now - _last_force))}
    _last_force = now
    lst = get_list(force=True)
    return {"refreshed": True, "count": len(lst)}


def _fixture_name(f):
    return f.get("fixture", f.get("name", "")) or ""


def search(query, limit=80):
    """Return a cleaned, grouped list of fixtures matching `query`."""
    q = (query or "").strip().lower()
    out = []
    for f in get_list():
        name = _fixture_name(f)
        mfg = f.get("manufacturer", "")
        if q and q not in name.lower() and q not in mfg.lower():
            continue
        out.append({
            "rid": f.get("rid"),
            "manufacturer": mfg,
            "name": name,
            "revision": f.get("revision", ""),
            "creator": f.get("creator", ""),
        })
        if len(out) >= limit:
            break
    out.sort(key=lambda x: (x["manufacturer"].lower(), x["name"].lower()))
    return out


def _download_and_parse(rid):
    path = _get_client().download_fixture(int(rid))
    if not path or not os.path.exists(path):
        raise RuntimeError("download failed")
    profiles = gdtf_cooker.GdtfParser().parse(path)
    if not profiles:
        raise RuntimeError("no DMX modes found in this GDTF")
    return profiles


def modes(rid):
    """List the DMX modes (name + channel footprint) for a fixture revision."""
    profiles = _download_and_parse(rid)
    writer = gdtf_cooker.JsonWriter()
    out = []
    for p in profiles:
        pid = writer.profile_id(p)
        d = writer._profile_to_dict(p, pid)
        out.append({
            "mode": p.mode_name,
            "footprint": d.get("footprint", 0),
            "type": d.get("fixture_type", ""),
            "id": pid,
        })
    return {
        "manufacturer": profiles[0].manufacturer,
        "name": profiles[0].name,
        "modes": out,
    }


def cook_to_pending(rid, mode_name, submitter=""):
    """Cook one mode of a fixture revision into PENDING_DIR. Returns metadata."""
    profiles = _download_and_parse(rid)
    chosen = next((p for p in profiles if p.mode_name == mode_name), None)
    if chosen is None:
        raise RuntimeError(f"mode '{mode_name}' not found")

    writer = gdtf_cooker.JsonWriter()
    pid = writer.profile_id(chosen)                 # "<mfg>/<model-mode>"
    obj = writer._profile_to_dict(chosen, pid)

    # Attribution / provenance lives in meta so we can trace where it came from.
    meta = obj.setdefault("meta", {})
    meta.setdefault("source", "gdtf-share")
    meta["gdtf_rid"] = int(rid)
    if submitter:
        meta["submitted_by"] = submitter[:60]

    rel = f"sources/{pid}.json"
    pend_path = os.path.join(config.PENDING_DIR, pid.replace("/", "__") + ".json")
    os.makedirs(os.path.dirname(pend_path), exist_ok=True) if "/" in pend_path else None
    with open(pend_path, "w") as f:
        json.dump({"id": pid, "rel": rel, "submitter": submitter, "profile": obj},
                  f, indent=2, ensure_ascii=False)
    return {"id": pid, "rel": rel, "footprint": obj.get("footprint", 0)}
