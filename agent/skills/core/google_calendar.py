import os
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

NAME = "google_calendar"
DOC = (
    "Google Calendar — read, create, update, and delete events. "
    "One-time setup: place credentials.json in /app/memory/ then call authorize() to get a URL, "
    "then authorize(YOUR_CODE) to save the token. After that, fully automatic. "
    "Functions: authorize(code=''), list_events(days_ahead='7'), "
    "create_event(title, date, time, duration_mins='60', description=''), "
    "delete_event(event_id), update_event(event_id, field, value), "
    "find_free_slots(date, duration_mins='30'), status()"
)

_MEMORY_DIR = Path(os.getenv("MEMORY_DIR", "/app/memory"))
_CREDENTIALS_FILE = _MEMORY_DIR / "gcal_credentials.json"
_TOKEN_FILE = _MEMORY_DIR / "gcal_token.json"
_AUTH_STATE_FILE = _MEMORY_DIR / "gcal_auth_pending.json"
_SCOPES = ["https://www.googleapis.com/auth/calendar"]


# ── Internal helpers ──────────────────────────────────────────────────────────

def _check_deps() -> tuple[bool, str]:
    """Returns (ok, error_message). Checks that Google client libraries are installed."""
    try:
        import googleapiclient  # noqa
        import google_auth_oauthlib  # noqa
        import google.auth  # noqa
        return True, "ok"
    except ImportError as e:
        return False, (
            f"Missing package: {e}. "
            "Add to requirements.txt:\n"
            "  google-api-python-client\n"
            "  google-auth-httplib2\n"
            "  google-auth-oauthlib"
        )


def _get_service():
    """
    Build and return an authenticated Google Calendar API service object.
    Raises FileNotFoundError if credentials.json is missing.
    Raises PermissionError if the token is missing or expired with no refresh token.
    Auto-refreshes an expired token if a refresh token is available.
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    if not _CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"credentials.json not found at {_CREDENTIALS_FILE}. "
            "Please complete the setup steps first."
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
                "Not authorized yet. Call authorize() to get the Google consent URL, "
                "then call authorize(YOUR_CODE) with the code Google gives you."
            )

    return build("calendar", "v3", credentials=creds)


def _parse_date(date_str: str):
    """Parse a flexible date string into a date object."""
    from dateutil import parser as dp
    s = date_str.strip().lower()
    if s == "today":
        return datetime.now().date()
    if s == "tomorrow":
        return (datetime.now() + timedelta(days=1)).date()
    return dp.parse(date_str.strip()).date()


def _parse_time(time_str: str):
    """Parse a flexible time string into a time object."""
    from dateutil import parser as dp
    return dp.parse(time_str.strip()).time()


def _fmt_event_dt(ev: dict) -> str:
    """Format an event's start time as a readable string."""
    from dateutil import parser as dp
    start = ev["start"].get("dateTime", ev["start"].get("date", ""))
    try:
        dt = dp.parse(start)
        if "T" in start:
            return dt.strftime("%a %b %d, %I:%M %p")
        return dt.strftime("%a %b %d (all-day)")
    except Exception:
        return start


# ── Public skill functions ─────────────────────────────────────────────────────

def authorize(code: str = "") -> str:
    """
    One-time Google Calendar authorization (two-step).

    Step 1 — call with no args to get the Google consent URL.
    Step 2 — call with the code Google gives you to save the token.

    Args:
        code: The authorization code from the Google consent page (leave blank for step 1)
    """
    import json as _json
    from urllib.parse import urlencode as _urlencode
    import requests as _req

    ok, reason = _check_deps()
    if not ok:
        return f"❌ {reason}"

    if not _CREDENTIALS_FILE.exists():
        return (
            f"❌ credentials.json not found at {_CREDENTIALS_FILE}\n\n"
            "Please complete setup:\n"
            "  → Go to console.cloud.google.com\n"
            "  → Create a project, enable the Google Calendar API\n"
            "  → Create OAuth 2.0 credentials (Desktop app)\n"
            "  → Download the JSON, rename it gcal_credentials.json\n"
            "  → Copy it into the /app/memory/ folder inside the container"
        )

    try:
        secrets = _json.loads(_CREDENTIALS_FILE.read_text())
        cfg = secrets.get("installed") or secrets.get("web") or {}
        client_id     = cfg["client_id"]
        client_secret = cfg["client_secret"]
        auth_uri      = cfg.get("auth_uri",  "https://accounts.google.com/o/oauth2/auth")
        token_uri     = cfg.get("token_uri", "https://oauth2.googleapis.com/token")
        redirect_uri  = "urn:ietf:wg:oauth:2.0:oob"
    except (KeyError, Exception) as e:
        return f"❌ Could not read credentials.json: {e}"

    if not code.strip():
        # ── Step 1: build auth URL manually — zero PKCE, zero library state ──
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
            "🔐 Google Calendar — Authorization Step 1 of 2\n\n"
            "Open this URL in your browser (copy the FULL URL — do not truncate it):\n\n"
            f"```\n{auth_url}\n```\n\n"
            "Then:\n"
            "  1. Sign in with your Google account\n"
            "  2. Click Allow\n"
            "  3. Copy the code Google shows you\n"
            "  4. Paste ONLY the code:\n"
            "     authorize(PASTE_CODE_HERE)\n\n"
            "The code is valid for 10 minutes."
        )

    # ── Step 2: exchange code directly via HTTP — no library, no PKCE ────────
    try:
        resp = _req.post(token_uri, data={
            "code":          code.strip(),
            "client_id":     client_id,
            "client_secret": client_secret,
            "redirect_uri":  redirect_uri,
            "grant_type":    "authorization_code",
        }, timeout=15)
        token_data = resp.json()
    except Exception as e:
        return f"❌ Token exchange request failed: {e}"

    if "error" in token_data:
        err_code = token_data.get("error", "")
        err_desc = token_data.get("error_description", "")
        if err_code == "invalid_grant":
            return (
                "❌ Google rejected the code.\n\n"
                "This means:\n"
                "  • The code was already used (each code is single-use)\n"
                "  • OR more than 10 minutes passed before pasting it\n\n"
                "Call authorize() again (no args) to get a fresh URL."
            )
        return f"❌ Token error: {err_code} — {err_desc}"

    # Build a proper Credentials object and save it
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

    if _AUTH_STATE_FILE.exists():
        _AUTH_STATE_FILE.unlink()

    refresh_note = ""
    if not token_data.get("refresh_token"):
        refresh_note = (
            "\n\n⚠️  No refresh token received — token will expire in ~1 hour.\n"
            "To fix: go to myaccount.google.com/permissions, revoke this app, "
            "then call authorize() again."
        )

    return (
        "✅ Google Calendar authorized and ready!\n"
        f"Token saved to {_TOKEN_FILE}\n"
        "You can now use list_events, create_event, and all other functions."
        + refresh_note
    )


def list_events(days_ahead: str = "7") -> str:
    """
    List upcoming events from your primary Google Calendar.

    Args:
        days_ahead: How many days ahead to look (default: 7)
    """
    ok, reason = _check_deps()
    if not ok:
        return f"❌ {reason}"

    try:
        service = _get_service()
        days = int(days_ahead)
        now = datetime.utcnow()
        time_min = now.isoformat() + "Z"
        time_max = (now + timedelta(days=days)).isoformat() + "Z"

        result = service.events().list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            maxResults=25,
            singleEvents=True,
            orderBy="startTime"
        ).execute()

        events = result.get("items", [])
        if not events:
            return f"📅 No events in the next {days} day{'s' if days != 1 else ''}."

        lines = [f"📅 Next {days} days — {len(events)} event(s):\n"]
        for ev in events:
            label = _fmt_event_dt(ev)
            title = ev.get("summary", "(no title)")
            ev_id = ev.get("id", "")
            location = ev.get("location", "")
            loc = f" @ {location}" if location else ""
            lines.append(f"• {label}{loc} — {title}\n  ID: {ev_id}")

        return "\n".join(lines)

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ Failed to list events: {type(e).__name__}: {e}"


def create_event(title: str, date: str, time: str,
                 duration_mins: str = "60", description: str = "") -> str:
    """
    Create a new event on your primary Google Calendar.

    Args:
        title:         Event title
        date:          Date — e.g. 2026-03-01, today, tomorrow, March 5
        time:          Start time — e.g. 09:00, 2pm, 14:30
        duration_mins: Duration in minutes (default: 60)
        description:   Optional description / notes
    """
    ok, reason = _check_deps()
    if not ok:
        return f"❌ {reason}"

    try:
        service = _get_service()

        date_obj = _parse_date(date)
        time_obj = _parse_time(time)
        start_dt = datetime.combine(date_obj, time_obj)
        end_dt = start_dt + timedelta(minutes=int(duration_mins))

        event_body = {
            "summary": title.strip(),
            "start": {"dateTime": start_dt.isoformat(), "timeZone": "UTC"},
            "end":   {"dateTime": end_dt.isoformat(),   "timeZone": "UTC"},
        }
        if description.strip():
            event_body["description"] = description.strip()

        created = service.events().insert(calendarId="primary", body=event_body).execute()
        ev_id = created.get("id", "")
        link = created.get("htmlLink", "")

        return (
            f"✅ Event created: {title}\n"
            f"Date:     {date_obj.strftime('%A, %B %d, %Y')}\n"
            f"Time:     {time_obj.strftime('%I:%M %p')} ({duration_mins} min)\n"
            f"ID:       {ev_id}\n"
            f"Link:     {link}"
        )

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ Failed to create event: {type(e).__name__}: {e}"


def delete_event(event_id: str) -> str:
    """
    Delete a calendar event by its ID.

    Args:
        event_id: The event ID shown by list_events
    """
    ok, reason = _check_deps()
    if not ok:
        return f"❌ {reason}"

    try:
        service = _get_service()
        service.events().delete(calendarId="primary", eventId=event_id.strip()).execute()
        return f"✅ Event deleted (ID: {event_id.strip()})"

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ {e}"
    except Exception as e:
        if "404" in str(e):
            return f"❌ Event not found: {event_id.strip()}"
        return f"❌ Failed to delete event: {type(e).__name__}: {e}"


def update_event(event_id: str, field: str, value: str) -> str:
    """
    Update one field of an existing event.

    Args:
        event_id: The event ID (from list_events)
        field:    Field to change — one of: title, description, location, date, time
        value:    New value (e.g. "New Title", "2026-03-10", "3pm")
    """
    ok, reason = _check_deps()
    if not ok:
        return f"❌ {reason}"

    try:
        from dateutil import parser as dp

        service = _get_service()
        event = service.events().get(calendarId="primary", eventId=event_id.strip()).execute()

        field = field.strip().lower()
        value = value.strip()

        if field == "title":
            event["summary"] = value

        elif field == "description":
            event["description"] = value

        elif field == "location":
            event["location"] = value

        elif field == "date":
            old_start_str = event["start"].get("dateTime", "")
            old_end_str = event["end"].get("dateTime", "")
            if old_start_str and old_end_str:
                s = dp.parse(old_start_str)
                e_dt = dp.parse(old_end_str)
                duration = e_dt - s
                new_date = _parse_date(value)
                new_start = datetime.combine(new_date, s.time())
                new_end = new_start + duration
                event["start"]["dateTime"] = new_start.isoformat()
                event["end"]["dateTime"] = new_end.isoformat()
            else:
                # All-day event
                event["start"]["date"] = value
                event["end"]["date"] = value

        elif field == "time":
            old_start_str = event["start"].get("dateTime", "")
            old_end_str = event["end"].get("dateTime", "")
            if not old_start_str:
                return "❌ Cannot set a time on an all-day event"
            s = dp.parse(old_start_str)
            e_dt = dp.parse(old_end_str)
            duration = e_dt - s
            new_time = _parse_time(value)
            new_start = datetime.combine(s.date(), new_time)
            new_end = new_start + duration
            event["start"]["dateTime"] = new_start.isoformat()
            event["end"]["dateTime"] = new_end.isoformat()

        else:
            return (
                f"❌ Unknown field '{field}'. "
                "Valid options: title, description, location, date, time"
            )

        updated = service.events().update(
            calendarId="primary", eventId=event_id.strip(), body=event
        ).execute()
        return (
            f"✅ Event updated\n"
            f"Field:    {field}\n"
            f"New value: {value}\n"
            f"ID:       {updated.get('id')}"
        )

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ {e}"
    except Exception as e:
        if "404" in str(e):
            return f"❌ Event not found: {event_id.strip()}"
        return f"❌ Failed to update event: {type(e).__name__}: {e}"


def find_free_slots(date: str, duration_mins: str = "30") -> str:
    """
    Find free time slots on a given day (checks 8am–8pm window).

    Args:
        date:          Date to check — e.g. today, tomorrow, 2026-03-05
        duration_mins: Minimum free slot length in minutes (default: 30)
    """
    ok, reason = _check_deps()
    if not ok:
        return f"❌ {reason}"

    try:
        from dateutil import parser as dp

        service = _get_service()
        date_obj = _parse_date(date)
        min_duration = timedelta(minutes=int(duration_mins))

        day_start = datetime.combine(date_obj, datetime.min.time().replace(hour=8))
        day_end = datetime.combine(date_obj, datetime.min.time().replace(hour=20))

        result = service.events().list(
            calendarId="primary",
            timeMin=day_start.isoformat() + "Z",
            timeMax=day_end.isoformat() + "Z",
            singleEvents=True,
            orderBy="startTime"
        ).execute()

        events = result.get("items", [])

        busy = []
        for ev in events:
            s_str = ev["start"].get("dateTime")
            e_str = ev["end"].get("dateTime")
            if s_str and e_str:
                busy.append((
                    dp.parse(s_str).replace(tzinfo=None),
                    dp.parse(e_str).replace(tzinfo=None)
                ))
        busy.sort()

        free_slots = []
        cursor = day_start
        for b_start, b_end in busy:
            if cursor < b_start and (b_start - cursor) >= min_duration:
                free_slots.append((cursor, b_start))
            cursor = max(cursor, b_end)
        if cursor < day_end and (day_end - cursor) >= min_duration:
            free_slots.append((cursor, day_end))

        date_label = date_obj.strftime("%A, %B %d")
        if not free_slots:
            return (
                f"📅 No free slots on {date_label} (8am–8pm window, "
                f"min {duration_mins} min). Calendar is fully booked."
            )

        lines = [f"🟢 Free slots on {date_label} (min {duration_mins} min):\n"]
        for s, e in free_slots:
            total_min = int((e - s).total_seconds() // 60)
            lines.append(
                f"• {s.strftime('%I:%M %p')} – {e.strftime('%I:%M %p')} "
                f"({total_min} min available)"
            )
        return "\n".join(lines)

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ Failed to find free slots: {type(e).__name__}: {e}"


def status() -> str:
    """Show Google Calendar skill configuration and auth token status."""
    ok, dep_reason = _check_deps()
    creds_exists = _CREDENTIALS_FILE.exists()
    token_exists = _TOKEN_FILE.exists()

    lines = [
        "📅 Google Calendar Skill Status",
        f"Dependencies:     {'✅ installed' if ok else '❌ ' + dep_reason}",
        f"credentials.json: {'✅ found' if creds_exists else '❌ not found at ' + str(_CREDENTIALS_FILE)}",
        f"Token:            {'✅ saved' if token_exists else '❌ not authorized — call authorize()'}",
    ]

    if token_exists and ok:
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE), _SCOPES)
            if creds.valid:
                lines.append("Auth status:      ✅ valid and ready")
            elif creds.expired and creds.refresh_token:
                creds.refresh(Request())
                _TOKEN_FILE.write_text(creds.to_json())
                lines.append("Auth status:      ✅ token was expired — refreshed automatically")
            else:
                lines.append("Auth status:      ❌ token expired, no refresh token — call authorize() again")
        except Exception as e:
            lines.append(f"Auth status:      ❌ could not read token: {e}")

    return "\n".join(lines)
