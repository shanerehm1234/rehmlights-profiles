"""vibe-broker — FastAPI service that turns a GDTF Share fixture into a Vibe
catalog profile, with owner moderation.

Public (through nginx):
  GET  /                         portal page
  GET  /api/profiles/search?q=
  GET  /api/profiles/modes?rid=
  POST /api/profiles/submit      {rid, mode, submitter}

Owner only (X-Admin-Token):
  GET  /admin                    approve/reject page
  GET  /api/admin/pending
  POST /api/admin/approve        {id}
  POST /api/admin/reject         {id}
"""
import os

from fastapi import FastAPI, HTTPException, Header, Body
from fastapi.responses import JSONResponse, FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config, cooker, publish, mailer

app = FastAPI(title="vibe-broker", version="0.1")

STATIC = os.path.join(config.BROKER_DIR, "static")


def _err(e):
    msg = str(e)
    # Defence in depth: never echo the token back, even if some lower layer
    # managed to include it in an exception string.
    if config.GITHUB_TOKEN:
        msg = msg.replace(config.GITHUB_TOKEN, "***")
    return JSONResponse(status_code=400, content={"error": msg})


# ---- public portal API ----------------------------------------------------
@app.get("/api/profiles/search")
def api_search(q: str = ""):
    try:
        return {"results": cooker.search(q)}
    except Exception as e:
        return _err(e)


@app.post("/api/profiles/refresh")
def api_refresh():
    """Force-refresh the GDTF Share list (for just-uploaded fixtures)."""
    try:
        return cooker.force_refresh()
    except Exception as e:
        return _err(e)


@app.get("/api/profiles/modes")
def api_modes(rid: int):
    try:
        return cooker.modes(rid)
    except Exception as e:
        return _err(e)


@app.get("/api/profiles/thumb")
def api_thumb(rid: int):
    """Fixture preview image pulled straight out of the GDTF archive. 404 when
    the file carries no thumbnail (the page just hides the <img>)."""
    try:
        data, ctype = cooker.thumbnail(rid)
    except Exception:
        data = None
    if not data:
        return Response(status_code=404)
    return Response(content=data, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=86400"})


@app.post("/api/profiles/submit")
def api_submit(body: dict = Body(...)):
    rid = body.get("rid")
    mode = body.get("mode")
    submitter = (body.get("submitter") or "")[:60]
    if rid is None or not mode:
        raise HTTPException(400, "rid and mode are required")
    try:
        info = cooker.cook_to_pending(rid, mode, submitter)
        # Email the owner a review alert with one-click approve/reject links.
        mailer.notify_submission(info, submitter)
        return {"status": "pending", **info}
    except Exception as e:
        return _err(e)


@app.get("/api/review")
def api_review(action: str, id: str, sig: str):
    """One-click approve/reject from an email link (HMAC-signed, no login)."""
    if action not in ("approve", "reject") or not mailer.valid(action, id, sig):
        return HTMLResponse(mailer.result_page("Invalid or expired link",
            "This review link isn't valid. It may have already been used.", ok=False),
            status_code=403)
    try:
        if action == "approve":
            r = publish.approve(id)
            pub = r.get("publish", "")
            return HTMLResponse(mailer.result_page("Approved & published",
                f"{id} is now in the catalog. Devices will see it on the next library refresh.",
                ok=True))
        else:
            publish.reject(id)
            return HTMLResponse(mailer.result_page("Rejected", f"{id} was discarded.", ok=True))
    except Exception as e:
        msg = str(e)
        if config.GITHUB_TOKEN:
            msg = msg.replace(config.GITHUB_TOKEN, "***")
        return HTMLResponse(mailer.result_page("Something went wrong", msg, ok=False),
                            status_code=400)


# ---- owner moderation -----------------------------------------------------
def _check_admin(token):
    if not config.ADMIN_TOKEN or token != config.ADMIN_TOKEN:
        raise HTTPException(403, "forbidden")


@app.get("/api/admin/pending")
def api_pending(x_admin_token: str = Header("")):
    _check_admin(x_admin_token)
    return {"pending": publish.list_pending()}


@app.post("/api/admin/approve")
def api_approve(body: dict = Body(...), x_admin_token: str = Header("")):
    _check_admin(x_admin_token)
    pid = body.get("id")
    if not pid:
        raise HTTPException(400, "id required")
    try:
        return publish.approve(pid)
    except Exception as e:
        return _err(e)


@app.post("/api/admin/reject")
def api_reject(body: dict = Body(...), x_admin_token: str = Header("")):
    _check_admin(x_admin_token)
    pid = body.get("id")
    if not pid:
        raise HTTPException(400, "id required")
    return {"rejected": publish.reject(pid)}


@app.get("/healthz")
def healthz():
    return {"ok": True, "gdtf_configured": bool(config.GDTF_USER),
            "publish": config.PUBLISH_ENABLED and bool(config.GITHUB_TOKEN),
            "email": mailer.enabled()}


# ---- static pages ---------------------------------------------------------
@app.get("/")
def portal():
    return FileResponse(os.path.join(STATIC, "add-fixture.html"))


@app.get("/admin")
def admin():
    return FileResponse(os.path.join(STATIC, "admin.html"))


app.mount("/static", StaticFiles(directory=STATIC), name="static")
