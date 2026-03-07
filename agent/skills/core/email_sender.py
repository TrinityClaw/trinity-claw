import os
import smtplib
import ssl
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path
from typing import Optional

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

import dotenv
dotenv.load_dotenv('/app/.env', override=True)

logger = logging.getLogger(__name__)

NAME = "email_sender"
DOC = (
    "Email sending via SMTP (Gmail, Outlook, any server) or SendGrid. "
    "Credentials auto-loaded from .env — no setup() needed. "
    "Usage: send(to, subject, body), send_html(to, subject, html), "
    "send_with_attachment(to, subject, body, filepath), status(), test(to)"
)

# ── Credentials from .env ──────────────────────────────────────────────────────
_provider    = os.getenv("EMAIL_PROVIDER", "smtp").lower()   # "smtp" or "sendgrid"
_from_addr   = os.getenv("EMAIL_FROM", "")

# SMTP settings
_smtp_host   = os.getenv("SMTP_HOST", "smtp.gmail.com")
_smtp_port   = int(os.getenv("SMTP_PORT", "587"))
_smtp_user   = os.getenv("SMTP_USER", "")
_smtp_pass   = os.getenv("SMTP_PASSWORD", "")

# SendGrid settings
_sg_api_key  = os.getenv("SENDGRID_API_KEY", "")


# ── Internal helpers ────────────────────────────────────────────────────────────

def _is_configured() -> tuple[bool, str]:
    """Returns (ok, reason). Checks whichever provider is active."""
    if _provider == "sendgrid":
        if not _sg_api_key:
            return False, "SENDGRID_API_KEY not set in .env"
        if not _from_addr:
            return False, "EMAIL_FROM not set in .env"
        return True, "ok"
    else:  # smtp
        if not _smtp_user or not _smtp_pass:
            return False, "SMTP_USER and/or SMTP_PASSWORD not set in .env"
        if not _from_addr:
            return False, "EMAIL_FROM not set in .env"
        return True, "ok"


def _send_smtp(to: str, subject: str, body: str, html: bool = False,
               attachment_path: Optional[str] = None) -> str:
    """Send via SMTP (port 587 STARTTLS by default)."""
    msg = MIMEMultipart("alternative" if html else "mixed")
    msg["Subject"] = subject
    msg["From"]    = _from_addr or _smtp_user
    msg["To"]      = to

    part = MIMEText(body, "html" if html else "plain", "utf-8")
    msg.attach(part)

    if attachment_path:
        path = Path(attachment_path)
        if not path.exists():
            return f"❌ Attachment not found: {attachment_path}"
        with open(path, "rb") as f:
            part_attach = MIMEBase("application", "octet-stream")
            part_attach.set_payload(f.read())
        encoders.encode_base64(part_attach)
        part_attach.add_header(
            "Content-Disposition", f'attachment; filename="{path.name}"'
        )
        msg.attach(part_attach)

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(_smtp_host, _smtp_port, timeout=15) as server:
            server.ehlo()
            server.starttls(context=context)
            server.login(_smtp_user, _smtp_pass)
            server.sendmail(msg["From"], [to], msg.as_string())
        return f"✅ Email sent to {to} via SMTP ({_smtp_host})"
    except smtplib.SMTPAuthenticationError:
        return "❌ SMTP auth failed — check SMTP_USER / SMTP_PASSWORD (Gmail needs an App Password)"
    except smtplib.SMTPRecipientsRefused:
        return f"❌ Recipient refused: {to}"
    except smtplib.SMTPException as e:
        return f"❌ SMTP error: {e}"
    except Exception as e:
        return f"❌ Unexpected error: {type(e).__name__}: {e}"


def _send_sendgrid(to: str, subject: str, body: str, html: bool = False) -> str:
    """Send via SendGrid v3 Mail Send API using requests (no sendgrid package needed)."""
    if not HAS_REQUESTS:
        return "❌ requests library not installed"

    payload = {
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": _from_addr},
        "subject": subject,
        "content": [{
            "type": "text/html" if html else "text/plain",
            "value": body
        }]
    }

    try:
        resp = _requests.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {_sg_api_key}",
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=15
        )
        if resp.status_code == 202:
            return f"✅ Email sent to {to} via SendGrid"
        elif resp.status_code == 401:
            return "❌ SendGrid 401 — check SENDGRID_API_KEY"
        elif resp.status_code == 403:
            return "❌ SendGrid 403 — sender not verified (verify EMAIL_FROM in SendGrid dashboard)"
        else:
            return f"❌ SendGrid {resp.status_code}: {resp.text[:300]}"
    except Exception as e:
        return f"❌ SendGrid request failed: {type(e).__name__}: {e}"


# ── Public skill functions ──────────────────────────────────────────────────────

def send(to: str, subject: str, body: str) -> str:
    """
    Send a plain-text email.

    Args:
        to:      Recipient email address
        subject: Email subject line
        body:    Plain-text body
    """
    ok, reason = _is_configured()
    if not ok:
        return f"❌ Email not configured: {reason}"

    if _provider == "sendgrid":
        return _send_sendgrid(to, subject, body, html=False)
    return _send_smtp(to, subject, body, html=False)


def send_html(to: str, subject: str, html_body: str) -> str:
    """
    Send an HTML email.

    Args:
        to:        Recipient email address
        subject:   Email subject line
        html_body: HTML content (full HTML or just body content)
    """
    ok, reason = _is_configured()
    if not ok:
        return f"❌ Email not configured: {reason}"

    if _provider == "sendgrid":
        return _send_sendgrid(to, subject, html_body, html=True)
    return _send_smtp(to, subject, html_body, html=True)


def send_with_attachment(to: str, subject: str, body: str, filepath: str) -> str:
    """
    Send an email with a file attachment (SMTP only).

    Args:
        to:       Recipient email address
        subject:  Email subject line
        body:     Plain-text body
        filepath: Absolute path to the file to attach
    """
    ok, reason = _is_configured()
    if not ok:
        return f"❌ Email not configured: {reason}"

    if _provider == "sendgrid":
        return "⚠️ Attachments not supported for SendGrid in this skill — use SMTP instead"
    return _send_smtp(to, subject, body, html=False, attachment_path=filepath)


def test(to: str) -> str:
    """
    Send a test email to verify configuration is working.

    Args:
        to: Recipient email address for the test
    """
    return send(
        to=to,
        subject="TrinityClaw — Email Test",
        body=(
            "This is a test email from your TrinityClaw AI agent.\n\n"
            f"Provider: {_provider.upper()}\n"
            f"From: {_from_addr or _smtp_user}\n\n"
            "If you received this, email is working correctly."
        )
    )


def status() -> str:
    """Show current email configuration status (no secrets exposed)."""
    ok, reason = _is_configured()
    lines = [
        "📧 Email Skill Status",
        f"Provider:  {_provider.upper()}",
        f"From:      {_from_addr or '(not set)'}",
    ]

    if _provider == "smtp":
        lines += [
            f"SMTP Host: {_smtp_host}:{_smtp_port}",
            f"SMTP User: {'✅ set' if _smtp_user else '❌ not set'}",
            f"SMTP Pass: {'✅ set' if _smtp_pass else '❌ not set'}",
        ]
    else:
        lines.append(f"SG Key:    {'✅ set (' + _sg_api_key[:8] + '...)' if _sg_api_key else '❌ not set'}")

    lines.append(f"Ready:     {'✅' if ok else '❌ ' + reason}")
    return "\n".join(lines)
