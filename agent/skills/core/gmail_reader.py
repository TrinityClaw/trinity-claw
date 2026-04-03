# gmail_reader.py
"""
Core skill: read Gmail inbox via the Gmail API.

Reuses the same gcal_credentials.json OAuth app — just needs Gmail API
enabled in Google Cloud Console and a one-time authorize() call.

Token stored separately as gmail_token.json so Calendar auth is unaffected.
"""

import base64
import email as _email_stdlib
import imaplib
import json
import os
import random
import re
import string
from email.header import decode_header as _decode_header
from datetime import datetime
from pathlib import Path

NAME = "gmail"
SHORT_DOC = "Read Gmail inbox, search emails, and reply via the Gmail API."
DOC = (
    "Read your Gmail inbox using the Gmail API. "
    "One-time setup: enable Gmail API in Google Cloud Console (same project as Calendar), "
    "then call authorize() → open URL → Allow → authorize(YOUR_CODE). "
    "Functions: "
    "authorize(code?)→one-time OAuth setup; "
    "list_inbox(n?, query?)→list recent emails with sender/subject/date; "
    "read_email(message_id)→get full body of a specific email; "
    "search_emails(query, n?)→search with Gmail operators (from:, subject:, after:, etc); "
    "get_attachments(message_id, save_dir?)→download attachments to memory/; "
    "mark_read(message_id)→mark email as read; "
    "summarize_inbox(n?)→AI-ready summary of recent emails; "
    "status()→auth health check. "
    "--- Email Monitor/Gmail (draft-approval workflow) --- "
    "check_new()→get new unread emails since last call, filters automated mail, tracks seen IDs; "
    "save_draft(to, subject, body, original_id?)→save a pending reply draft, returns ref code; "
    "approve_draft(ref)→retrieve draft data then call email_sender.send() to send it; "
    "reject_draft(ref)→discard draft without sending; "
    "list_drafts()→show all drafts awaiting approval; "
    "get_rules()→load response rules from memory/email_rules.md. "
    "--- IMAP (any mail server — set IMAP_HOST/USER/PASSWORD in .env) --- "
    "imap_check_new()→check for new unread emails via IMAP, same draft-approval workflow; "
    "imap_list_inbox(n?)→list recent inbox emails via IMAP; "
    "imap_read_email(uid)→read full email body by UID; "
    "imap_status()→test IMAP connection and show config."
)

# ── Config ─────────────────────────────────────────────────────────────────────

_MEMORY_DIR       = Path(os.getenv("MEMORY_DIR", "/app/memory"))
_CREDENTIALS_FILE = _MEMORY_DIR / "gcal_credentials.json"   # reuse same OAuth app
_TOKEN_FILE       = _MEMORY_DIR / "gmail_token.json"         # separate token
_ATTACHMENTS_DIR  = _MEMORY_DIR / "email_attachments"
_SCOPES           = ["https://www.googleapis.com/auth/gmail.modify"]  # read + mark-as-read

# Email monitor state files
_MONITOR_STATE_FILE  = _MEMORY_DIR / "email_monitor_state.json"
_IMAP_STATE_FILE     = _MEMORY_DIR / "email_imap_state.json"
_DRAFTS_FILE         = _MEMORY_DIR / "email_drafts.json"
_RULES_FILE          = _MEMORY_DIR / "email_rules.md"

# Senders/subjects to skip automatically (loop + spam protection)
_SKIP_SENDER_KEYWORDS = [
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "notifications@", "alerts@",
]
_SKIP_SUBJECT_PREFIXES = (
    "auto:", "automatic reply", "out of office", "delivery status",
    "undeliverable", "delivery failure", "mail delivery",
    "receipt for", "order confirmation",
)


# ── Dependency check ───────────────────────────────────────────────────────────

def _check_deps() -> tuple:
    try:
        import googleapiclient   # noqa
        import google_auth_oauthlib  # noqa
        import google.auth        # noqa
        return True, "ok"
    except ImportError as e:
        return False, (
            f"Missing package: {e}. "
            "These should already be in requirements.txt:\n"
            "  google-api-python-client\n"
            "  google-auth-httplib2\n"
            "  google-auth-oauthlib"
        )


# ── Auth ───────────────────────────────────────────────────────────────────────

def _get_service():
    """Build an authenticated Gmail API service. Auto-refreshes expired tokens."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    if not _CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"gcal_credentials.json not found at {_CREDENTIALS_FILE}. "
            "Copy your Google OAuth credentials file there first."
        )

    creds = None
    if _TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE), _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _TOKEN_FILE.write_text(creds.to_json())
        else:
            raise PermissionError(
                "Gmail not authorized. Call authorize() to get the consent URL, "
                "then authorize(YOUR_CODE) with the code Google gives you."
            )

    return build("gmail", "v1", credentials=creds)


# ── Body / header helpers ──────────────────────────────────────────────────────

def _decode_b64(data: str) -> str:
    """Decode base64url Gmail payload data."""
    # Pad if needed
    padded = data + "=" * (4 - len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _extract_body(payload: dict) -> str:
    """Walk a Gmail message payload and return the best plain-text body."""
    mime = payload.get("mimeType", "")

    # Simple single-part plain text
    if mime == "text/plain":
        data = payload.get("body", {}).get("data", "")
        return _decode_b64(data) if data else ""

    # Multipart — prefer text/plain, fall back to text/html (stripped)
    if "parts" in payload:
        plain = ""
        html  = ""
        for part in payload["parts"]:
            part_mime = part.get("mimeType", "")
            data      = part.get("body", {}).get("data", "")
            if not data:
                # Nested multipart
                nested = _extract_body(part)
                if nested:
                    plain = plain or nested
                continue
            if part_mime == "text/plain":
                plain = _decode_b64(data)
            elif part_mime == "text/html" and not html:
                raw  = _decode_b64(data)
                html = re.sub(r"<[^>]+>", " ", raw)
                html = re.sub(r"\s{2,}", " ", html).strip()
        return plain or html or "[No readable body]"

    return "[No readable body]"


def _get_header(headers: list, name: str) -> str:
    """Extract a specific header value by name (case-insensitive)."""
    name_lower = name.lower()
    for h in headers:
        if h.get("name", "").lower() == name_lower:
            return h.get("value", "")
    return ""


def _fmt_date(raw: str) -> str:
    """Format an RFC 2822 email date to something readable."""
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(raw)
        return dt.strftime("%b %d, %Y %I:%M %p")
    except Exception:
        return raw[:30] if raw else "unknown date"


def _fetch_metadata(service, msg_id: str) -> dict:
    """Fetch just headers for a message (fast, no body)."""
    msg = service.users().messages().get(
        userId="me",
        id=msg_id,
        format="metadata",
        metadataHeaders=["From", "To", "Subject", "Date"],
    ).execute()
    headers = msg.get("payload", {}).get("headers", [])
    return {
        "id":      msg_id,
        "from":    _get_header(headers, "From"),
        "to":      _get_header(headers, "To"),
        "subject": _get_header(headers, "Subject"),
        "date":    _fmt_date(_get_header(headers, "Date")),
        "snippet": msg.get("snippet", ""),
        "unread":  "UNREAD" in msg.get("labelIds", []),
    }


# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC SKILL FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def authorize(*args) -> str:
    """
    One-time Gmail authorization (two-step, same flow as Google Calendar).

    Step 1 — call with no args to get the consent URL.
    Step 2 — call with the code Google gives you to save the token.

    Usage: authorize()  then  authorize(PASTE_CODE_HERE)
    """
    import requests as _req
    from urllib.parse import urlencode as _urlencode

    ok, reason = _check_deps()
    if not ok:
        return f"❌ {reason}"

    if not _CREDENTIALS_FILE.exists():
        return (
            f"❌ gcal_credentials.json not found at {_CREDENTIALS_FILE}\n\n"
            "Setup steps:\n"
            "  1. Go to console.cloud.google.com (same project as Calendar)\n"
            "  2. APIs & Services → Library → Gmail API → Enable\n"
            "  3. Your gcal_credentials.json already works — just copy it if missing:\n"
            f"     docker cp gcal_credentials.json trinity-claw-trinity-agent-1:{_CREDENTIALS_FILE}"
        )

    try:
        secrets = json.loads(_CREDENTIALS_FILE.read_text())
        cfg          = secrets.get("installed") or secrets.get("web") or {}
        client_id    = cfg["client_id"]
        client_secret= cfg["client_secret"]
        auth_uri     = cfg.get("auth_uri",  "https://accounts.google.com/o/oauth2/auth")
        token_uri    = cfg.get("token_uri", "https://oauth2.googleapis.com/token")
        redirect_uri = "urn:ietf:wg:oauth:2.0:oob"
    except (KeyError, Exception) as e:
        return f"❌ Could not read gcal_credentials.json: {e}"

    code = str(args[0]).strip() if args else ""

    if not code:
        # Step 1 — build auth URL
        params = {
            "client_id":     client_id,
            "redirect_uri":  redirect_uri,
            "response_type": "code",
            "scope":         " ".join(_SCOPES),
            "access_type":   "offline",
            "prompt":        "consent",
        }
        auth_url = auth_uri + "?" + _urlencode(params)
        return (
            "🔐 Gmail — Authorization Step 1 of 2\n\n"
            f"[Click here to authorize Gmail]({auth_url})\n\n"
            "Then:\n"
            "  1. Sign in with your Google account\n"
            "  2. Click Allow\n"
            "  3. Copy the code Google shows you\n"
            "  4. Call: authorize(PASTE_CODE_HERE)\n\n"
            "The code expires in 10 minutes."
        )

    # Step 2 — exchange code for token
    try:
        resp = _req.post(token_uri, data={
            "code":          code,
            "client_id":     client_id,
            "client_secret": client_secret,
            "redirect_uri":  redirect_uri,
            "grant_type":    "authorization_code",
        }, timeout=15)
        token_data = resp.json()
    except Exception as e:
        return f"❌ Token exchange failed: {e}"

    if "error" in token_data:
        err  = token_data.get("error", "")
        desc = token_data.get("error_description", "")
        if err == "invalid_grant":
            return (
                "❌ Code rejected by Google.\n"
                "  • Each code is single-use\n"
                "  • Codes expire after 10 minutes\n"
                "Call authorize() again for a fresh URL."
            )
        return f"❌ Token error: {err} — {desc}"

    try:
        from google.oauth2.credentials import Credentials
        creds = Credentials(
            token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token"),
            token_uri=token_uri,
            client_id=client_id,
            client_secret=client_secret,
            scopes=_SCOPES,
        )
        _TOKEN_FILE.write_text(creds.to_json())
    except Exception as e:
        return f"❌ Failed to save token: {e}"

    refresh_note = ""
    if not token_data.get("refresh_token"):
        refresh_note = (
            "\n\n⚠️  No refresh token — token will expire in ~1 hour.\n"
            "Fix: revoke at myaccount.google.com/permissions then call authorize() again."
        )

    return (
        "✅ Gmail authorized and ready!\n"
        f"Token saved to {_TOKEN_FILE}\n"
        "You can now use list_inbox, read_email, search_emails, and all other functions."
        + refresh_note
    )


def list_inbox(*args) -> str:
    """
    List recent emails from your inbox.

    Usage: list_inbox()  or  list_inbox(20)  or  list_inbox(10, 'is:unread')
    Args:
        n     : Number of emails to list (default: 10)
        query : Optional Gmail search query (default: inbox)
    """
    ok, reason = _check_deps()
    if not ok:
        return f"❌ {reason}"

    n     = int(args[0]) if args else 10
    query = str(args[1]).strip() if len(args) > 1 else "in:inbox"

    try:
        service = _get_service()
        results = service.users().messages().list(
            userId="me", maxResults=n, q=query
        ).execute()
        messages = results.get("messages", [])

        if not messages:
            return f"📭 No emails found (query: '{query}')"

        lines = [f"📬 {len(messages)} email(s) — query: '{query}'\n"]
        for i, m in enumerate(messages, 1):
            meta = _fetch_metadata(service, m["id"])
            unread = "🔵 " if meta["unread"] else "   "
            lines.append(
                f"{unread}[{i}] {meta['date']}\n"
                f"     From:    {meta['from']}\n"
                f"     Subject: {meta['subject']}\n"
                f"     Preview: {meta['snippet'][:120]}\n"
                f"     ID:      {meta['id']}\n"
            )
        return "\n".join(lines)

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ list_inbox failed: {type(e).__name__}: {e}"


def read_email(*args) -> str:
    """
    Read the full body of a specific email by its ID.

    Usage: read_email(MESSAGE_ID)
    Get message IDs from list_inbox() or search_emails().
    """
    ok, reason = _check_deps()
    if not ok:
        return f"❌ {reason}"

    if not args:
        return "❌ Usage: read_email(MESSAGE_ID)"

    msg_id = str(args[0]).strip()

    try:
        service = _get_service()
        msg     = service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()

        headers = msg.get("payload", {}).get("headers", [])
        body    = _extract_body(msg.get("payload", {}))

        # Attachment summary
        attachments = []
        for part in msg.get("payload", {}).get("parts", []):
            fname = part.get("filename", "")
            if fname:
                size = part.get("body", {}).get("size", 0)
                attachments.append(f"{fname} ({size} bytes)")

        lines = [
            f"📧 Email: {msg_id}",
            f"From:    {_get_header(headers, 'From')}",
            f"To:      {_get_header(headers, 'To')}",
            f"Date:    {_fmt_date(_get_header(headers, 'Date'))}",
            f"Subject: {_get_header(headers, 'Subject')}",
        ]
        if attachments:
            lines.append(f"Attachments: {', '.join(attachments)}")
        lines.append(f"\n{'─'*60}\n{body.strip()}")

        return "\n".join(lines)

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ read_email failed: {type(e).__name__}: {e}"


def search_emails(*args) -> str:
    """
    Search your Gmail using Gmail search operators.

    Usage: search_emails(query)  or  search_emails(query, n_results)

    Query examples:
      from:john@example.com
      subject:invoice after:2026/1/1
      is:unread has:attachment
      from:boss@company.com is:unread
    Default n_results: 10
    """
    ok, reason = _check_deps()
    if not ok:
        return f"❌ {reason}"

    if not args:
        return "❌ Usage: search_emails(query)  or  search_emails('from:john', 20)"

    query = str(args[0]).strip()
    n     = int(args[1]) if len(args) > 1 else 10

    try:
        service = _get_service()
        results = service.users().messages().list(
            userId="me", maxResults=n, q=query
        ).execute()
        messages = results.get("messages", [])

        if not messages:
            return f"🔍 No emails found for: '{query}'"

        lines = [f"🔍 {len(messages)} result(s) for: '{query}'\n"]
        for i, m in enumerate(messages, 1):
            meta = _fetch_metadata(service, m["id"])
            unread = "🔵 " if meta["unread"] else "   "
            lines.append(
                f"{unread}[{i}] {meta['date']}\n"
                f"     From:    {meta['from']}\n"
                f"     Subject: {meta['subject']}\n"
                f"     Preview: {meta['snippet'][:120]}\n"
                f"     ID:      {meta['id']}\n"
            )
        return "\n".join(lines)

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ search_emails failed: {type(e).__name__}: {e}"


def get_attachments(*args) -> str:
    """
    Download all attachments from an email to memory/email_attachments/.

    Usage: get_attachments(MESSAGE_ID)  or  get_attachments(MESSAGE_ID, '/app/memory/custom/')
    """
    ok, reason = _check_deps()
    if not ok:
        return f"❌ {reason}"

    if not args:
        return "❌ Usage: get_attachments(MESSAGE_ID)"

    msg_id   = str(args[0]).strip()
    save_dir = Path(str(args[1]).strip()) if len(args) > 1 else _ATTACHMENTS_DIR
    save_dir.mkdir(parents=True, exist_ok=True)

    try:
        service = _get_service()
        msg     = service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()

        saved = []
        for part in msg.get("payload", {}).get("parts", []):
            filename = part.get("filename", "")
            body     = part.get("body", {})
            if not filename:
                continue

            attachment_id = body.get("attachmentId")
            if attachment_id:
                att = service.users().messages().attachments().get(
                    userId="me", messageId=msg_id, id=attachment_id
                ).execute()
                data = _decode_b64(att.get("data", ""))
            elif body.get("data"):
                data = _decode_b64(body["data"])
            else:
                continue

            out_path = save_dir / filename
            out_path.write_bytes(data.encode("latin-1", errors="replace"))
            saved.append(f"  ✅ {filename} → {out_path}")

        if not saved:
            return f"📎 No attachments found in email {msg_id}"

        return f"📎 Downloaded {len(saved)} attachment(s):\n" + "\n".join(saved)

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ get_attachments failed: {type(e).__name__}: {e}"


def mark_read(*args) -> str:
    """
    Mark an email as read (removes the UNREAD label).

    Usage: mark_read(MESSAGE_ID)
    """
    ok, reason = _check_deps()
    if not ok:
        return f"❌ {reason}"

    if not args:
        return "❌ Usage: mark_read(MESSAGE_ID)"

    msg_id = str(args[0]).strip()

    try:
        service = _get_service()
        service.users().messages().modify(
            userId="me",
            id=msg_id,
            body={"removeLabelIds": ["UNREAD"]},
        ).execute()
        return f"✅ Marked as read: {msg_id}"

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ mark_read failed: {type(e).__name__}: {e}"


def summarize_inbox(*args) -> str:
    """
    Get a concise AI-ready summary of your recent inbox — ideal for daily briefings.

    Shows sender, subject, date, and a short preview for each email.
    Usage: summarize_inbox()  or  summarize_inbox(20)
    Default: 10 most recent inbox emails.
    """
    ok, reason = _check_deps()
    if not ok:
        return f"❌ {reason}"

    n = int(args[0]) if args else 10

    try:
        service = _get_service()
        results = service.users().messages().list(
            userId="me", maxResults=n, q="in:inbox"
        ).execute()
        messages = results.get("messages", [])

        if not messages:
            return "📭 Inbox is empty."

        unread_count = 0
        lines = [f"📬 Inbox summary — {len(messages)} most recent emails:\n"]

        for i, m in enumerate(messages, 1):
            meta = _fetch_metadata(service, m["id"])
            if meta["unread"]:
                unread_count += 1
            flag = "🔵 UNREAD" if meta["unread"] else "      read"
            lines.append(
                f"[{i}] {flag} | {meta['date']}\n"
                f"     From: {meta['from']}\n"
                f"     Re:   {meta['subject']}\n"
                f"     {meta['snippet'][:150]}\n"
                f"     ID: {meta['id']}\n"
            )

        lines.insert(1, f"🔵 {unread_count} unread of {len(messages)} shown\n")
        return "\n".join(lines)

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ summarize_inbox failed: {type(e).__name__}: {e}"


def status(*args) -> str:
    """Show Gmail skill auth status and configuration."""
    ok, dep_reason = _check_deps()
    creds_exists   = _CREDENTIALS_FILE.exists()
    token_exists   = _TOKEN_FILE.exists()

    lines = [
        "📧 Gmail Skill Status",
        f"  Dependencies  : {'✅ installed' if ok else '❌ ' + dep_reason}",
        f"  credentials   : {'✅ found' if creds_exists else '❌ not found — ' + str(_CREDENTIALS_FILE)}",
        f"  gmail_token   : {'✅ saved' if token_exists else '❌ not authorized — call authorize()'}",
    ]

    if token_exists and ok:
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE), _SCOPES)
            if creds.valid:
                lines.append("  Auth status   : ✅ valid and ready")
            elif creds.expired and creds.refresh_token:
                creds.refresh(Request())
                _TOKEN_FILE.write_text(creds.to_json())
                lines.append("  Auth status   : ✅ token refreshed automatically")
            else:
                lines.append("  Auth status   : ❌ expired, no refresh token — call authorize() again")
        except Exception as e:
            lines.append(f"  Auth status   : ❌ could not read token: {e}")

    lines.append(f"  Attachments → : {_ATTACHMENTS_DIR}")
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL MONITOR — draft-approval workflow
# ══════════════════════════════════════════════════════════════════════════════

def _load_monitor_state() -> dict:
    if _MONITOR_STATE_FILE.exists():
        try:
            return json.loads(_MONITOR_STATE_FILE.read_text())
        except Exception:
            pass
    return {"seen_ids": []}


def _save_monitor_state(state: dict):
    _MONITOR_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _MONITOR_STATE_FILE.write_text(json.dumps(state, indent=2))


def _load_drafts() -> dict:
    if _DRAFTS_FILE.exists():
        try:
            return json.loads(_DRAFTS_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_drafts(drafts: dict):
    _DRAFTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _DRAFTS_FILE.write_text(json.dumps(drafts, indent=2))


def _gen_ref() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=6))


def _is_automated(sender: str, subject: str) -> bool:
    s   = sender.lower()
    sub = subject.lower()
    if any(kw in s for kw in _SKIP_SENDER_KEYWORDS):
        return True
    if any(sub.startswith(p) for p in _SKIP_SUBJECT_PREFIXES):
        return True
    return False


def check_new(*args) -> str:
    """
    Check Gmail for new unread emails since the last call.
    Automatically skips automated/loop emails (noreply, out-of-office, etc).
    Returns full details for each new message so you can draft replies.
    Tracks seen IDs so the same email is never returned twice.
    """
    ok, reason = _check_deps()
    if not ok:
        return f"❌ {reason}"

    try:
        service = _get_service()
        state   = _load_monitor_state()
        seen    = set(state.get("seen_ids", []))

        results  = service.users().messages().list(
            userId="me", maxResults=25, q="is:unread in:inbox"
        ).execute()
        messages = results.get("messages", [])

        if not messages:
            return "📭 No unread emails in inbox."

        new_emails  = []
        skipped     = []
        newly_seen  = []

        for m in messages:
            mid = m["id"]
            if mid in seen:
                continue

            msg     = service.users().messages().get(
                userId="me", id=mid, format="full"
            ).execute()
            headers = msg.get("payload", {}).get("headers", [])
            sender  = _get_header(headers, "From")
            subject = _get_header(headers, "Subject")
            to_hdr  = _get_header(headers, "To")
            date    = _fmt_date(_get_header(headers, "Date"))
            body    = _extract_body(msg.get("payload", {}))

            newly_seen.append(mid)

            if _is_automated(sender, subject):
                skipped.append(f"  ⏭ [{subject[:55]}] from {sender[:40]}")
                continue

            new_emails.append(
                f"📧 ID: {mid}\n"
                f"   From:    {sender}\n"
                f"   To:      {to_hdr}\n"
                f"   Subject: {subject}\n"
                f"   Date:    {date}\n"
                f"   Body:\n{body[:1500]}"
                + (" [truncated]" if len(body) > 1500 else "")
            )

        # Persist seen IDs, cap at 500
        state["seen_ids"]   = list((seen | set(newly_seen)))[-500:]
        state["last_check"] = datetime.now().isoformat()
        _save_monitor_state(state)

        if not new_emails and not skipped:
            return "📭 No new emails since last check."

        lines = []
        if new_emails:
            lines.append(f"📬 {len(new_emails)} new email(s) to review:\n")
            lines.extend(new_emails)
        if skipped:
            lines.append(f"\n(Skipped {len(skipped)} automated email(s):")
            lines.extend(skipped)
            lines.append(")")
        return "\n\n".join(lines)

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ check_new failed: {type(e).__name__}: {str(e)[:200]}"


def save_draft(to: str, subject: str, body: str, original_id: str = "") -> str:
    """
    Save a pending email reply draft awaiting user approval.
    Returns a short ref code — send this to the user so they can approve or reject.
    """
    try:
        drafts = _load_drafts()
        ref    = _gen_ref()
        while ref in drafts:
            ref = _gen_ref()

        drafts[ref] = {
            "to":          to,
            "subject":     subject,
            "body":        body,
            "original_id": original_id,
            "created":     datetime.now().isoformat(),
        }
        _save_drafts(drafts)
        return (
            f"✅ Draft saved — Ref: {ref}\n"
            f"   To: {to}\n"
            f"   Subject: {subject}\n"
            f"   Preview: {body[:120]}...\n\n"
            f"Notify the user: 'approve {ref}' to send, 'reject {ref}' to discard."
        )
    except Exception as e:
        return f"❌ save_draft failed: {e}"


def approve_draft(ref: str) -> str:
    """
    Retrieve an approved draft and remove it from the queue.
    Returns the full draft body — then call email_sender.send(to, subject, body) to send it.
    """
    try:
        drafts = _load_drafts()
        ref    = ref.strip().upper()
        if ref not in drafts:
            hint = f" Pending: {list(drafts.keys())}" if drafts else " No drafts pending."
            return f"❌ No draft found with ref '{ref}'.{hint}"

        d = drafts.pop(ref)
        _save_drafts(drafts)
        return (
            f"✅ Draft {ref} approved and removed from queue.\n"
            f"Now call: email_sender.send(to='{d['to']}', subject='{d['subject']}', body=<below>)\n\n"
            f"--- BODY START ---\n{d['body']}\n--- BODY END ---"
        )
    except Exception as e:
        return f"❌ approve_draft failed: {e}"


def reject_draft(ref: str) -> str:
    """Discard a pending draft without sending."""
    try:
        drafts = _load_drafts()
        ref    = ref.strip().upper()
        if ref not in drafts:
            return f"❌ No draft found with ref '{ref}'."
        d = drafts.pop(ref)
        _save_drafts(drafts)
        return f"🗑 Draft {ref} discarded (was: to {d['to']} — {d['subject'][:50]})"
    except Exception as e:
        return f"❌ reject_draft failed: {e}"


def list_drafts(*args) -> str:
    """Show all email drafts currently pending user approval."""
    try:
        drafts = _load_drafts()
        if not drafts:
            return "📭 No pending email drafts."
        lines = [f"📋 {len(drafts)} draft(s) pending approval:\n"]
        for ref, d in drafts.items():
            lines.append(
                f"  [{ref}]  To: {d['to']}\n"
                f"          Subject: {d['subject']}\n"
                f"          Saved:   {d['created'][:16]}\n"
                f"          Preview: {d['body'][:100]}...\n"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"❌ list_drafts failed: {e}"


def get_rules(*args) -> str:
    """
    Load your email response rules from memory/email_rules.md.
    Always call this before drafting a reply to follow the user's preferences.
    """
    if not _RULES_FILE.exists():
        return (
            "⚠️ No email_rules.md found at /app/memory/email_rules.md.\n"
            "Create this file to define: who to respond to, tone, common scenarios, "
            "what to escalate, and what to skip."
        )
    return _RULES_FILE.read_text()


# ══════════════════════════════════════════════════════════════════════════════
# IMAP — works with any mail server (local or hosted domain email)
# Set IMAP_HOST, IMAP_PORT, IMAP_USER, IMAP_PASSWORD in .env
# ══════════════════════════════════════════════════════════════════════════════

def _imap_connect() -> imaplib.IMAP4:
    """Open an authenticated IMAP connection. Caller must call .logout() when done."""
    host     = os.getenv("IMAP_HOST", "").strip()
    port     = int(os.getenv("IMAP_PORT", "993"))
    user     = os.getenv("IMAP_USER", "").strip()
    password = os.getenv("IMAP_PASSWORD", "").strip()

    if not host or not user or not password:
        raise ValueError(
            "IMAP_HOST, IMAP_USER, and IMAP_PASSWORD must all be set in .env"
        )

    use_ssl = port == 993 or os.getenv("IMAP_SSL", "true").lower() == "true"
    conn = imaplib.IMAP4_SSL(host, port) if use_ssl else imaplib.IMAP4(host, port)
    conn.login(user, password)
    return conn


def _imap_decode_header(value: str) -> str:
    """Decode an encoded email header value to plain string."""
    if not value:
        return ""
    parts = _decode_header(value)
    result = []
    for part, charset in parts:
        if isinstance(part, bytes):
            result.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)


def _imap_extract_body(msg) -> str:
    """Extract best plain-text body from an email.message.Message object."""
    if msg.is_multipart():
        # Prefer text/plain
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # Fallback: strip HTML
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    html = payload.decode(charset, errors="replace")
                    return re.sub(r"<[^>]+>", " ", html).strip()
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return "[No readable body]"


def _load_imap_state() -> dict:
    if _IMAP_STATE_FILE.exists():
        try:
            return json.loads(_IMAP_STATE_FILE.read_text())
        except Exception:
            pass
    return {"seen_uids": []}


def _save_imap_state(state: dict):
    _IMAP_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _IMAP_STATE_FILE.write_text(json.dumps(state, indent=2))


def imap_list_inbox(n: int = 10) -> str:
    """
    List the most recent emails in your IMAP inbox.
    Uses IMAP_HOST / IMAP_USER / IMAP_PASSWORD from .env.
    Usage: imap_list_inbox()  or  imap_list_inbox(20)
    """
    try:
        conn = _imap_connect()
        conn.select("INBOX", readonly=True)

        _, data = conn.search(None, "ALL")
        all_uids = data[0].split()
        recent   = all_uids[-(n):][::-1]  # newest first

        if not recent:
            conn.logout()
            return "📭 IMAP inbox is empty."

        lines = [f"📬 IMAP inbox — {len(recent)} most recent emails:\n"]
        for uid in recent:
            _, msg_data = conn.fetch(uid, "(RFC822.HEADER)")
            raw_header  = msg_data[0][1]
            msg         = _email_stdlib.message_from_bytes(raw_header)
            sender      = _imap_decode_header(msg.get("From", ""))
            subject     = _imap_decode_header(msg.get("Subject", "(no subject)"))
            date        = msg.get("Date", "")[:40]
            lines.append(
                f"  UID {uid.decode()} | {date}\n"
                f"  From:    {sender}\n"
                f"  Subject: {subject}\n"
            )

        conn.logout()
        return "\n".join(lines)

    except ValueError as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ imap_list_inbox failed: {type(e).__name__}: {str(e)[:200]}"


def imap_read_email(uid: str) -> str:
    """
    Read the full body of a specific email by its IMAP UID.
    Get UIDs from imap_list_inbox() or imap_check_new().
    Usage: imap_read_email(UID)
    """
    try:
        conn = _imap_connect()
        conn.select("INBOX", readonly=True)

        _, msg_data = conn.fetch(str(uid).encode(), "(RFC822)")
        conn.logout()

        if not msg_data or msg_data[0] is None:
            return f"❌ No email found with UID {uid}"

        raw  = msg_data[0][1]
        msg  = _email_stdlib.message_from_bytes(raw)

        sender  = _imap_decode_header(msg.get("From", ""))
        to_hdr  = _imap_decode_header(msg.get("To", ""))
        subject = _imap_decode_header(msg.get("Subject", "(no subject)"))
        date    = msg.get("Date", "")
        body    = _imap_extract_body(msg)

        return (
            f"📧 UID: {uid}\n"
            f"   From:    {sender}\n"
            f"   To:      {to_hdr}\n"
            f"   Subject: {subject}\n"
            f"   Date:    {date}\n"
            f"   Body:\n{body[:2000]}"
            + (" [truncated]" if len(body) > 2000 else "")
        )

    except ValueError as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ imap_read_email failed: {type(e).__name__}: {str(e)[:200]}"


def imap_check_new(*args) -> str:
    """
    Check IMAP inbox for new unread emails since the last call.
    Filters automated/loop emails automatically.
    Returns full details for each new message — use save_draft() to queue replies.
    Works with any mail server: local, cPanel, Mailcow, hMailServer, etc.
    """
    try:
        conn  = _imap_connect()
        state = _load_imap_state()
        seen  = set(state.get("seen_uids", []))

        conn.select("INBOX", readonly=False)
        _, data = conn.search(None, "UNSEEN")
        uids    = data[0].split()

        if not uids:
            conn.logout()
            return "📭 No new unread emails."

        new_emails = []
        skipped    = []
        newly_seen = []

        for uid in uids:
            uid_str = uid.decode()
            if uid_str in seen:
                continue

            _, msg_data = conn.fetch(uid, "(RFC822)")
            if not msg_data or msg_data[0] is None:
                continue

            msg     = _email_stdlib.message_from_bytes(msg_data[0][1])
            sender  = _imap_decode_header(msg.get("From", ""))
            to_hdr  = _imap_decode_header(msg.get("To", ""))
            subject = _imap_decode_header(msg.get("Subject", "(no subject)"))
            date    = msg.get("Date", "")
            body    = _imap_extract_body(msg)

            newly_seen.append(uid_str)

            if _is_automated(sender, subject):
                skipped.append(f"  ⏭ [{subject[:55]}] from {sender[:40]}")
                continue

            new_emails.append(
                f"📧 UID: {uid_str}\n"
                f"   From:    {sender}\n"
                f"   To:      {to_hdr}\n"
                f"   Subject: {subject}\n"
                f"   Date:    {date[:40]}\n"
                f"   Body:\n{body[:1500]}"
                + (" [truncated]" if len(body) > 1500 else "")
            )

        conn.logout()

        # Persist seen UIDs, cap at 500
        state["seen_uids"]  = list((seen | set(newly_seen)))[-500:]
        state["last_check"] = datetime.now().isoformat()
        _save_imap_state(state)

        if not new_emails and not skipped:
            return "📭 No new emails since last check."

        lines = []
        if new_emails:
            lines.append(f"📬 {len(new_emails)} new email(s) to review:\n")
            lines.extend(new_emails)
        if skipped:
            lines.append(f"\n(Skipped {len(skipped)} automated email(s):")
            lines.extend(skipped)
            lines.append(")")
        return "\n\n".join(lines)

    except ValueError as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ imap_check_new failed: {type(e).__name__}: {str(e)[:200]}"


def imap_status(*args) -> str:
    """Test the IMAP connection and show current configuration."""
    host     = os.getenv("IMAP_HOST", "")
    port     = os.getenv("IMAP_PORT", "993")
    user     = os.getenv("IMAP_USER", "")
    use_ssl  = os.getenv("IMAP_PORT", "993") == "993" or os.getenv("IMAP_SSL", "true") == "true"

    lines = [
        "📧 IMAP Status",
        f"  Host  : {host or '❌ not set (IMAP_HOST)'}",
        f"  Port  : {port} ({'SSL' if use_ssl else 'plain'})",
        f"  User  : {user or '❌ not set (IMAP_USER)'}",
        f"  Pass  : {'✅ set' if os.getenv('IMAP_PASSWORD') else '❌ not set (IMAP_PASSWORD)'}",
    ]

    if host and user and os.getenv("IMAP_PASSWORD"):
        try:
            conn = _imap_connect()
            _, counts = conn.select("INBOX", readonly=True)
            total = int(counts[0]) if counts and counts[0] else 0
            conn.logout()
            lines.append(f"  Connection : ✅ OK — {total} messages in INBOX")
        except Exception as e:
            lines.append(f"  Connection : ❌ Failed — {str(e)[:100]}")
    else:
        lines.append("  Connection : ⏸ Skipped — credentials not fully set")

    return "\n".join(lines)
