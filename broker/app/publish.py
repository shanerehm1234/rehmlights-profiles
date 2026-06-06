"""Moderation + publishing: pending list, approve (promote to the catalog and
push), reject. Approval is the only path that ever writes to the repo.
"""
import os
import sys
import json
import glob
import subprocess

from . import config


def _pending_path(pid):
    return os.path.join(config.PENDING_DIR, pid.replace("/", "__") + ".json")


def list_pending():
    out = []
    for p in sorted(glob.glob(os.path.join(config.PENDING_DIR, "*.json"))):
        try:
            with open(p) as f:
                d = json.load(f)
            prof = d.get("profile", {})
            out.append({
                "id": d.get("id"),
                "rel": d.get("rel"),
                "submitter": d.get("submitter", ""),
                "manufacturer": prof.get("manufacturer", ""),
                "name": prof.get("name", ""),
                "mode": prof.get("mode", ""),
                "footprint": prof.get("footprint", 0),
            })
        except Exception:
            continue
    return out


def reject(pid):
    path = _pending_path(pid)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def _run(cmd, cwd):
    """Run a git/tool command, raising a clean (token-scrubbed) error with the
    real stderr so failures are diagnosable instead of an opaque exit code."""
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()
        if config.GITHUB_TOKEN:
            err = err.replace(config.GITHUB_TOKEN, "***")
        raise RuntimeError(f"`{' '.join(cmd[:3])}` failed: {err[:400]}")
    return r


def _git_sync_clean(repo):
    """Token-free origin + credential store, then HARD-sync the working tree to
    the latest origin/main. This discards any local drift (stray commits, dirty
    files) so the upcoming commit fast-forwards cleanly — robust to the checkout
    having moved out of sync with the catalog."""
    _run(["git", "config", "user.name", config.GIT_AUTHOR], repo)
    _run(["git", "config", "user.email", config.GIT_EMAIL], repo)
    _run(["git", "remote", "set-url", "origin",
          f"https://github.com/{config.GITHUB_REPO}.git"], repo)
    cred = os.path.join(config.DATA_DIR, ".git-credentials")
    with open(cred, "w") as f:
        f.write(f"https://x-access-token:{config.GITHUB_TOKEN}@github.com\n")
    os.chmod(cred, 0o600)
    _run(["git", "config", "credential.helper", f"store --file={cred}"], repo)
    # Unshallow if needed (original checkout may be a --depth 1 clone).
    if os.path.exists(os.path.join(repo, ".git", "shallow")):
        subprocess.run(["git", "fetch", "--unshallow", "origin", "main"], cwd=repo,
                       capture_output=True, text=True)  # best-effort
    _run(["git", "fetch", "origin", "main"], repo)
    _run(["git", "reset", "--hard", "origin/main"], repo)
    _run(["git", "clean", "-fd"], repo)


def _git_commit_push(repo):
    _run(["git", "add", "sources", "index.json"], repo)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo).returncode == 0:
        return "nothing to commit"
    _run(["git", "commit", "-m", "Add fixture profile via broker"], repo)
    _run(["git", "push", "origin", "HEAD:main"], repo)
    return "pushed"


def approve(pid):
    """Promote a pending profile into sources/, rebuild the index, push."""
    path = _pending_path(pid)
    if not os.path.exists(path):
        raise RuntimeError("pending profile not found")
    with open(path) as f:
        d = json.load(f)

    publishing = config.PUBLISH_ENABLED and config.GITHUB_TOKEN

    # Sync to clean latest main FIRST, so we write + commit on top of it (the
    # push then always fast-forwards — no rebase, no drift).
    if publishing:
        _git_sync_clean(config.CATALOG_DIR)

    rel = d["rel"]                      # sources/<mfg>/<model-mode>.json
    dest = os.path.join(config.CATALOG_DIR, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "w") as f:
        json.dump(d["profile"], f, indent=2, ensure_ascii=False)
        f.write("\n")

    # Rebuild index.json via the existing tool.
    _run([sys.executable, os.path.join(config.TOOLS_DIR, "build_index.py")],
         config.CATALOG_DIR)

    result = "staged (publish disabled)"
    if publishing:
        result = _git_commit_push(config.CATALOG_DIR)

    os.remove(path)                    # clear from the pending queue
    prof = d.get("profile", {})
    return {"id": pid, "rel": rel, "publish": result,
            "submitter": d.get("submitter", ""),
            "manufacturer": prof.get("manufacturer", ""),
            "name": prof.get("name", ""),
            "mode": prof.get("mode", ""),
            "footprint": prof.get("footprint", 0)}
