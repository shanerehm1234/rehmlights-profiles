#!/usr/bin/env python3
"""
reonboard.py — re-cook every catalog fixture from its GDTF Share revision.

Why: the catalog's `sources/**/*.json` were cooked with an older gdtf_cooker
that (a) capped names at 21 chars — shorter than the firmware's char[24] name
buffers — and (b) predated the manufacturer-prefix stripper, so some names read
"Chauvet DJ Intimidato" / "Generic 15Wx4 Mini Mo" (mangled). Every source JSON
still records its origin in meta.gdtf_rid, so we can re-download the original
GDTF and re-cook it with the CURRENT rules (MAX_NAME_LENGTH=24 + _strip_mfg_prefix).

What it does, per existing source file:
  1. read meta.gdtf_rid + the stored `mode`,
  2. download that revision from GDTF Share (needs a logged-in session),
  3. parse + pick the matching DMX mode,
  4. rewrite the source JSON (new clean name may change its slug/path — the old
     file is removed so no orphan is left behind),
  5. preserve provenance (meta.source / gdtf_rid / submitted_by).
Then it rebuilds index.json via build_index.py.

Credentials (GDTF Share service account — same one the broker uses):
    GDTF_SHARE_USER / GDTF_SHARE_PASS   (preferred; matches broker/.env)
    or GDTF_USER / GDTF_PASSWORD        (matches gdtf_cooker's own convention)

Usage (run where the GDTF Share creds live — i.e. the broker host):
    GDTF_SHARE_USER=... GDTF_SHARE_PASS=... python3 tools/reonboard.py
    python3 tools/reonboard.py --dry-run     # no network: audit rids/modes only
    python3 tools/reonboard.py --only chauvet-dj  # limit to one mfg dir

This is safe to re-run (idempotent): unchanged fixtures re-cook to the same
path; only names that were truncated/prefixed move. Review `git diff` before you
push. Devices pick up clean names on their next library browse/download; a
fixture already PATCHED keeps the name copied into it until you re-add it.
"""
import argparse
import glob
import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_DIR = os.path.join(REPO_ROOT, "sources")
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))
import gdtf_cooker  # noqa: E402


def _creds():
    u = os.environ.get("GDTF_SHARE_USER") or os.environ.get("GDTF_USER") or ""
    p = os.environ.get("GDTF_SHARE_PASS") or os.environ.get("GDTF_PASSWORD") or ""
    return u.strip(), p.strip()


def main():
    ap = argparse.ArgumentParser(description="Re-cook the whole catalog from GDTF Share.")
    ap.add_argument("--dry-run", action="store_true",
                    help="don't download or write — just report what each source records")
    ap.add_argument("--only", default="",
                    help="limit to one manufacturer directory (e.g. 'generic')")
    args = ap.parse_args()

    pattern = os.path.join(SOURCES_DIR, args.only or "**", "*.json")
    paths = sorted(glob.glob(pattern, recursive=True))
    if not paths:
        print(f"No source JSONs under {SOURCES_DIR}/{args.only}")
        return 1

    # Gather (rid, mode, old_path) up front so a missing rid is caught before login.
    work, skipped = [], []
    for p in paths:
        try:
            d = json.load(open(p))
        except json.JSONDecodeError as e:
            skipped.append((p, f"invalid JSON: {e}"))
            continue
        rid = (d.get("meta") or {}).get("gdtf_rid")
        mode = d.get("mode", "")
        if not rid:
            skipped.append((p, "no meta.gdtf_rid — cook manually"))
            continue
        work.append({"path": p, "rid": int(rid), "mode": mode,
                     "old_name": d.get("name", ""), "meta": d.get("meta") or {}})

    print(f"{len(work)} fixtures with a gdtf_rid, {len(skipped)} skipped.")
    for pth, why in skipped:
        print(f"  SKIP {os.path.relpath(pth, REPO_ROOT)} — {why}")

    if args.dry_run:
        # Audit only: no network. Flag the names that WILL likely change.
        changed = [w for w in work if len(w["old_name"]) >= 21]
        print(f"\n[dry-run] {len(changed)} name(s) are at the 21-char cap and "
              f"should get re-cooked cleaner once you run for real:")
        for w in changed:
            print(f"  rid {w['rid']:>7}  mode {w['mode']!r:<14}  {w['old_name']!r}")
        return 0

    user, pw = _creds()
    if not user or not pw:
        print("ERROR: set GDTF_SHARE_USER + GDTF_SHARE_PASS (or GDTF_USER + "
              "GDTF_PASSWORD) — this must run where the GDTF Share creds live.")
        return 2

    client = gdtf_cooker.GdtfShareClient()
    if not client.login(user, pw):
        print("ERROR: GDTF Share login failed.")
        return 2

    parser = gdtf_cooker.GdtfParser()
    writer = gdtf_cooker.JsonWriter()
    renamed, unchanged, failed = [], 0, []

    for w in work:
        try:
            path = client.download_fixture(w["rid"], force=True)
            profiles = parser.parse(path)
            chosen = next((pr for pr in profiles if pr.mode_name == w["mode"]), None)
            if chosen is None:
                # Mode label can shift between GDTF revisions; fall back to the
                # single mode if there's only one, else report the mismatch.
                if len(profiles) == 1:
                    chosen = profiles[0]
                else:
                    failed.append((w["path"], f"mode {w['mode']!r} not in "
                                   f"{[pr.mode_name for pr in profiles]}"))
                    continue

            new_pid = writer.profile_id(chosen)
            mfg_slug, rest_slug = new_pid.split("/", 1)
            new_rel = os.path.join("sources", mfg_slug, f"{rest_slug}.json")
            new_path = os.path.join(REPO_ROOT, new_rel)

            obj = writer._profile_to_dict(chosen, new_pid)
            # Preserve provenance the cooker doesn't re-derive.
            meta = obj.setdefault("meta", {})
            meta["source"] = w["meta"].get("source", "gdtf-share")
            meta["gdtf_rid"] = w["rid"]
            if w["meta"].get("submitted_by"):
                meta["submitted_by"] = w["meta"]["submitted_by"]

            os.makedirs(os.path.dirname(new_path), exist_ok=True)
            with open(new_path, "w") as f:
                json.dump(obj, f, indent=2, ensure_ascii=False)
                f.write("\n")

            if os.path.abspath(new_path) != os.path.abspath(w["path"]):
                os.remove(w["path"])   # name changed → drop the stale slug
                renamed.append((w["old_name"], obj["name"]))
            else:
                unchanged += 1
        except Exception as e:  # noqa: BLE001 — report and keep going
            failed.append((w["path"], repr(e)))

    print(f"\nRe-cooked: {len(renamed)} renamed, {unchanged} unchanged, "
          f"{len(failed)} failed.")
    for old, new in renamed:
        print(f"  {old!r:32} -> {new!r}")
    for pth, why in failed:
        print(f"  FAIL {os.path.relpath(pth, REPO_ROOT)} — {why}")

    print("\nRebuilding index.json ...")
    subprocess.run([sys.executable, os.path.join(REPO_ROOT, "tools", "build_index.py")],
                   check=True)
    print("Done. Review `git diff`, then commit + push to publish.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
