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

# Refresh the cached GDTF Share fixture list at most this often.
LIST_TTL_SECONDS = int(os.environ.get("GDTF_LIST_TTL", str(24 * 3600)))

os.makedirs(PENDING_DIR, exist_ok=True)
