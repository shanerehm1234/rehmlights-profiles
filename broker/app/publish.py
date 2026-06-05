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
    return subprocess.run(cmd, cwd=cwd, check=True,
                          capture_output=True, text=True)


def _git_push():
    """Commit sources/ + index.json and push.

    The token goes into a git credential STORE on the private data volume — it
    never appears in a git argv (so it can't end up in an error message), and
    git references the remote only as `origin`.
    """
    repo = config.CATALOG_DIR
    _run(["git", "config", "user.name", config.GIT_AUTHOR], repo)
    _run(["git", "config", "user.email", config.GIT_EMAIL], repo)

    # Plain (token-free) remote URL + a credential helper file holding the PAT.
    _run(["git", "remote", "set-url", "origin",
          f"https://github.com/{config.GITHUB_REPO}.git"], repo)
    cred = os.path.join(config.DATA_DIR, ".git-credentials")
    with open(cred, "w") as f:
        f.write(f"https://x-access-token:{config.GITHUB_TOKEN}@github.com\n")
    os.chmod(cred, 0o600)
    _run(["git", "config", "credential.helper", f"store --file={cred}"], repo)

    _run(["git", "add", "sources", "index.json"], repo)
    if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo).returncode == 0:
        return "nothing to commit"
    _run(["git", "commit", "-m", "Add fixture profile via broker"], repo)

    # The catalog may have moved since this checkout — sync before pushing so
    # the broker's commit fast-forwards onto the latest main.
    _run(["git", "fetch", "origin", "main"], repo)
    _run(["git", "rebase", "origin/main"], repo)
    _run(["git", "push", "origin", "HEAD:main"], repo)
    return "pushed"


def approve(pid):
    """Promote a pending profile into sources/, rebuild the index, push."""
    path = _pending_path(pid)
    if not os.path.exists(path):
        raise RuntimeError("pending profile not found")
    with open(path) as f:
        d = json.load(f)

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
    if config.PUBLISH_ENABLED and config.GITHUB_TOKEN:
        result = _git_push()

    os.remove(path)                    # clear from the pending queue
    return {"id": pid, "rel": rel, "publish": result}
