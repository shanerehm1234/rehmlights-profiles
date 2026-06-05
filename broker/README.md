# vibe-broker

The "front door" that turns a [GDTF Share](https://gdtf-share.com/) fixture into
a VIBE catalog profile, with owner moderation. End users never touch the GDTF
site, the cooker, or GitHub.

```
 Browser (add-fixture.html)
   │  search / pick mode / submit
   ▼
 nginx  ──/api/──►  vibe-broker (this service)
                      │  reuses ../tools/gdtf_cooker.py
                      ▼
                GDTF Share API  →  cook-json  →  pending/ (on /data)
                      │
            you approve in /admin  →  sources/ + build_index → git push
                      ▼
        raw.githubusercontent  →  Vibe device "＋ FROM LIBRARY"
```

It **reuses the existing cooker** (`../tools/gdtf_cooker.py`) — GDTF Share client,
GDTF parser, JSON writer — so there's no duplicated parsing logic.

## Endpoints

Public (through nginx):
- `GET  /` — the portal page
- `GET  /api/profiles/search?q=` — search the cached GDTF Share list
- `GET  /api/profiles/modes?rid=` — DMX modes for a fixture revision
- `POST /api/profiles/submit` — `{rid, mode, submitter}` → cooks into `pending/`

Owner only (`X-Admin-Token` header):
- `GET  /admin` — approve/reject page
- `GET  /api/admin/pending`
- `POST /api/admin/approve` — `{id}` → promote to `sources/`, build index, push
- `POST /api/admin/reject` — `{id}`

`GET /healthz` reports whether GDTF + publish are configured.

## Configuration (env vars — see `.env.example`)

| var | purpose |
|-----|---------|
| `GDTF_SHARE_USER` / `GDTF_SHARE_PASS` | GDTF Share service account |
| `GITHUB_TOKEN` | fine-grained PAT, Contents:RW on the catalog repo |
| `GITHUB_REPO` | default `shanerehm1234/rehmlights-profiles` |
| `ADMIN_TOKEN` | password for `/admin` |
| `PUBLISH_ENABLED` | `0` = cook + stage but don't push (dry run) |
| `CATALOG_DIR` | path to the catalog checkout (default: parent of `broker/`) |
| `BROKER_DATA_DIR` | pending queue + gdtf cache (default `/data` in the image) |

Secrets are read only from the environment — nothing sensitive is committed.

## Run locally

```bash
cd broker
cp .env.example .env          # fill in GDTF_SHARE_PASS, ADMIN_TOKEN
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
GDTF_SHARE_USER=... GDTF_SHARE_PASS=... ADMIN_TOKEN=dev \
  uvicorn app.main:app --reload --port 8000
# portal:  http://localhost:8000/
# admin:   http://localhost:8000/admin
```

Or `docker compose up --build` (mounts the repo at `/catalog`).

## Deploy on Unraid (next to the nginx container)

1. **Clone the catalog** onto appdata:
   `/mnt/user/appdata/rehmlights-profiles/` (this is the working tree it pushes from).
2. **Add a container** (Docker tab → Add Container), image built from this
   `broker/` folder, with:
   - **Network:** same custom Docker network as your nginx container (so nginx
     can reach it by name `vibe-broker`).
   - **Volumes:**
     - `/mnt/user/appdata/rehmlights-profiles` → `/catalog`
     - `/mnt/user/appdata/vibe-broker/data` → `/data`
   - **Env:** `GDTF_SHARE_USER`, `GDTF_SHARE_PASS`, `GITHUB_TOKEN`,
     `ADMIN_TOKEN` (+ optional `PUBLISH_ENABLED=0` while testing).
   - No published WAN port — only nginx talks to it.
3. **nginx** — add inside the rehmlights `server { }`:
   ```nginx
   location /api/ {
       proxy_pass http://vibe-broker:8000;
       proxy_set_header Host $host;
   }
   location = /add-fixture { proxy_pass http://vibe-broker:8000/; }
   location = /admin       { proxy_pass http://vibe-broker:8000/admin; }
   ```
   (Or just serve `static/add-fixture.html` straight from the site and let only
   `/api/` proxy to the broker.)
4. Visit `https://rehmlights.com/add-fixture`.

## Moderation

Submissions are cooked into `pending/` on the data volume (never the repo).
Open `/admin`, enter `ADMIN_TOKEN`, and **Approve** (promotes to `sources/`,
rebuilds `index.json`, pushes) or **Reject** (discards). Devices see approved
fixtures within minutes via the existing library fetch.
