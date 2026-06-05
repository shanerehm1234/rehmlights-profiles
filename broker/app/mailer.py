"""Email alerts with one-click, HMAC-signed Approve/Reject links.

A submission emails the owner a summary + two links that work from anywhere.
Each link carries an HMAC of (action, id) so only links the broker generated
are honoured — no admin login, no admin page exposed to the internet.
"""
import hmac
import html
import hashlib
import smtplib
import ssl
import threading
from email.message import EmailMessage
from urllib.parse import quote

from . import config


def sign(action, pid):
    msg = f"{action}:{pid}".encode()
    return hmac.new(config.REVIEW_SECRET.encode(), msg, hashlib.sha256).hexdigest()[:24]


def valid(action, pid, sig):
    if not config.REVIEW_SECRET or not sig:
        return False
    return hmac.compare_digest(sign(action, pid), sig)


def _link(action, pid):
    return (f"{config.PUBLIC_BASE_URL}/api/review?action={action}"
            f"&id={quote(pid, safe='')}&sig={sign(action, pid)}")


def enabled():
    return bool(config.SMTP_HOST and config.MAIL_TO and config.REVIEW_SECRET)


def _send(subject, body_html):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = config.MAIL_FROM or config.SMTP_USER
    msg["To"] = config.MAIL_TO
    msg.set_content("This message requires an HTML-capable email client.")
    msg.add_alternative(body_html, subtype="html")
    ctx = ssl.create_default_context()
    if config.SMTP_PORT == 465:
        with smtplib.SMTP_SSL(config.SMTP_HOST, 465, context=ctx) as s:
            if config.SMTP_USER:
                s.login(config.SMTP_USER, config.SMTP_PASS)
            s.send_message(msg)
    else:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as s:
            s.ehlo()
            s.starttls(context=ctx)
            if config.SMTP_USER:
                s.login(config.SMTP_USER, config.SMTP_PASS)
            s.send_message(msg)


def notify_submission(info, submitter):
    """Fire off the review email in a background thread (never blocks/fails the
    user's submit response)."""
    if not enabled():
        return

    def work():
        try:
            pid = info["id"]
            e = html.escape
            approve = _link("approve", pid)
            reject = _link("reject", pid)
            body = f"""\
<div style="font:15px system-ui,Segoe UI,Roboto,sans-serif;max-width:520px;
margin:0 auto;color:#e6eaf0;background:#0a0e14;padding:24px;border-radius:14px">
  <h2 style="color:#00d9ff;margin:0 0 4px">New VIBE fixture submitted</h2>
  <p style="color:#7a8794;margin:0 0 18px">Review it for the catalog.</p>
  <div style="background:#131820;border:1px solid #2a3645;border-radius:12px;padding:16px;margin-bottom:18px">
    <div style="font-size:18px;font-weight:600">{e(info.get('manufacturer',''))} &middot; {e(info.get('name',''))}</div>
    <div style="color:#7a8794;margin-top:4px">{e(info.get('mode',''))} &middot; {info.get('footprint',0)} channels</div>
    <div style="color:#7a8794;margin-top:4px">id: {e(pid)}</div>
    {f'<div style="color:#7a8794;margin-top:4px">by: {e(submitter)}</div>' if submitter else ''}
  </div>
  <a href="{approve}" style="display:inline-block;background:#0e2a16;border:1px solid #2a6;
   color:#44cc66;text-decoration:none;padding:12px 22px;border-radius:10px;font-weight:600;margin-right:10px">✓ Approve &amp; publish</a>
  <a href="{reject}" style="display:inline-block;background:#2a0e12;border:1px solid #a23;
   color:#ff3b5c;text-decoration:none;padding:12px 22px;border-radius:10px;font-weight:600">✗ Reject</a>
  <p style="color:#4a5564;font-size:12px;margin-top:20px">Links are single-purpose and signed; safe to click from any device.</p>
</div>"""
            _send(f"VIBE: {info.get('manufacturer','')} {info.get('name','')} submitted", body)
        except Exception:
            pass

    threading.Thread(target=work, daemon=True).start()


def result_page(title, msg, ok=True):
    color = "#44cc66" if ok else "#ff3b5c"
    return f"""<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<body style="margin:0;background:#0a0e14;color:#e6eaf0;font:16px system-ui;display:flex;
min-height:100vh;align-items:center;justify-content:center">
<div style="text-align:center;padding:24px">
<div style="font-size:40px;color:{color}">{'✓' if ok else '✗'}</div>
<h2 style="color:{color};margin:8px 0">{html.escape(title)}</h2>
<p style="color:#7a8794;max-width:380px">{html.escape(msg)}</p></div></body>"""
