"""Broker configuration — everything comes from the environment.

Secrets (GDTF Share login, GitHub token, admin password) are NEVER committed;
in production they're set as Unraid container env vars, and for local dev they
live in a gitignored .env that docker-compose / uvicorn loads.
"""
import os

# --- Repo layout ---------------------------------------------------------
# The broker lives at <catalog>/broker/, so the catalog root is one level up.
# That checkout is the working tree we cook into + push from.
BROKER_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_env_file():
    """Load broker/.env into the environment, FILE WINS.

    `docker run --env-file` is only read at container creation, so a plain
    `docker restart` won't pick up edits. Reading the file ourselves at startup
    means editing .env + `docker restart vibe-broker` always takes effect.
    Values are taken verbatim after the first '=' (so '&' in passwords is fine).
    """
    path = os.path.join(BROKER_DIR, ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ[k.strip()] = v.strip()
    except Exception:
        pass


_load_env_file()

CATALOG_DIR = os.environ.get("CATALOG_DIR", os.path.dirname(BROKER_DIR))
TOOLS_DIR   = os.path.join(CATALOG_DIR, "tools")
SOURCES_DIR = os.path.join(CATALOG_DIR, "sources")

# Pending (cooked but unapproved) profiles + the GDTF-list cache live on a
# writable data volume, OUTSIDE git, so moderation never touches the repo until
# you approve.
DATA_DIR    = os.environ.get("BROKER_DATA_DIR", os.path.join(BROKER_DIR, "data"))
PENDING_DIR = os.path.join(DATA_DIR, "pending")
CACHE_PATH  = os.path.join(DATA_DIR, "gdtf_list.json")

# --- GDTF Share service account -----------------------------------------
GDTF_USER = os.environ.get("GDTF_SHARE_USER", "")
GDTF_PASS = os.environ.get("GDTF_SHARE_PASS", "")

# --- GitHub publish ------------------------------------------------------
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO  = os.environ.get("GITHUB_REPO", "shanerehm1234/rehmlights-profiles")
GIT_AUTHOR   = os.environ.get("GIT_AUTHOR", "vibe-broker")
GIT_EMAIL    = os.environ.get("GIT_EMAIL", "vibe-broker@rehmlights.com")
PUBLISH_ENABLED = os.environ.get("PUBLISH_ENABLED", "1") != "0"  # set 0 to dry-run

# --- Admin gate ----------------------------------------------------------
# Shared password for the /admin endpoints. The broker is only reachable
# through nginx on your LAN, but this keeps approvals owner-only.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

# --- Email alerts + one-click review links -------------------------------
# When a fixture is submitted, email the owner with Approve/Reject links that
# work from anywhere (each link is HMAC-signed so only the broker's own emails
# are valid). Leave SMTP_HOST blank to disable email.
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "") or SMTP_USER
MAIL_TO   = os.environ.get("MAIL_TO", "")
# Public base URL the review links point at (proxied to the broker via nginx).
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "https://rehmlights.com").rstrip("/")
# Secret used to sign the one-click review links. REQUIRED for email links.
REVIEW_SECRET = os.environ.get("REVIEW_SECRET", "")

# Refresh the cached GDTF Share fixture list at most this often.
LIST_TTL_SECONDS = int(os.environ.get("GDTF_LIST_TTL", str(24 * 3600)))

os.makedirs(PENDING_DIR, exist_ok=True)
