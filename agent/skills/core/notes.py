import json
import logging
from pathlib import Path
from datetime import datetime, date, timedelta

NAME = "notes"

logger = logging.getLogger(__name__)
DOC = (
    "Persistent notes storage across sessions. "
    "Functions: "
    "save(title, content, tags?)→save note; tags is optional comma-separated string e.g. 'meeting,q1'; "
    "load(title)→retrieve note text; "
    "tag(title, tags)→add tags to an existing note; "
    "list_notes()→all notes with previews and tags; "
    "list_by_tag(tag)→filter notes by a specific tag; "
    "search(term)→notes matching keyword; "
    "delete(title)→remove note; "
    "export_all()→JSON dump of all notes; "
    "write_daily_entry(summary, learned, user_insights?, next_steps?)→save today's learning journal; "
    "get_journal(days=7)→retrieve last N days of journal entries; pass an integer e.g. get_journal(7); "
    "get_today()→get today's journal entry; "
    "update_user_model(insight)→append a new insight about the user to the persistent profile; "
    "get_user_model()→retrieve all accumulated user insights; "
    "log_activity(action, result, source?)→log a completed manual task to the activity log (source defaults to 'manual'); "
    "get_activity_log(hours?)→show what the agent did in the last N hours, both scheduled and manual (default 24h); "
    "append(title, content)→add content to an existing note without overwriting it (creates note if it does not exist yet); "
    "end_day(summary, next_steps?)→wrap the day: writes today's journal entry with auto-pulled activity log and returns a full day overview."
)

_LOGS_FILE    = Path("/app/memory/session_logs.jsonl")
_ACTIVITY_LOG = Path("/app/memory/activity_log.jsonl")

NOTES_FILE = Path("/app/memory/notes.json")
JOURNAL_FILE = Path("/app/memory/daily_journal.jsonl")
USER_MODEL_FILE = Path("/app/memory/user_model.json")

def _load_notes():
    """Load notes from file"""
    NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    if NOTES_FILE.exists():
        try:
            return json.loads(NOTES_FILE.read_text())
        except Exception as e:
            logger.error(f"Failed to parse notes file, returning empty: {e}")
            return {}
    return {}

def _save_notes(notes):
    """Save notes to file"""
    NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    NOTES_FILE.write_text(json.dumps(notes, indent=2))

def save(title: str, content: str, tags: str = "") -> str:
    """Save a note with optional comma-separated tags"""
    try:
        notes    = _load_notes()
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        notes[title] = {
            "content": content,
            "created": datetime.now().isoformat(),
            "updated": datetime.now().isoformat(),
            "tags":    tag_list,
        }
        _save_notes(notes)
        tag_str = f" [tags: {', '.join(tag_list)}]" if tag_list else ""
        return f"✅ Note saved: '{title}' ({len(content)} chars){tag_str}"
    except Exception as e:
        return f"❌ Error saving note: {e}"

def load(title: str) -> str:
    """Load a note by title"""
    try:
        notes = _load_notes()
        if title in notes:
            content = notes[title]["content"]
            updated = notes[title].get("updated", "unknown")
            return f"📝 {title}\n(Updated: {updated})\n\n{content}"
        return f"❌ Note '{title}' not found"
    except Exception as e:
        return f"❌ Error loading note: {e}"

def list_notes() -> str:
    """List all note titles with metadata and tags"""
    try:
        notes = _load_notes()
        if not notes:
            return "📭 No notes saved yet"

        result = "📚 Your Notes:\n"
        for i, (title, data) in enumerate(notes.items(), 1):
            content_preview = data["content"][:50].replace('\n', ' ')
            tags    = data.get("tags", [])
            tag_str = f" [{', '.join(tags)}]" if tags else ""
            result += f"{i}. **{title}**{tag_str} - {content_preview}...\n"
        return result
    except Exception as e:
        return f"❌ Error listing notes: {e}"

def delete(title: str) -> str:
    """Delete a note"""
    try:
        notes = _load_notes()
        if title in notes:
            del notes[title]
            _save_notes(notes)
            return f"✅ Note deleted: '{title}'"
        return f"❌ Note '{title}' not found"
    except Exception as e:
        return f"❌ Error deleting note: {e}"

def tag(title: str, tags: str) -> str:
    """Add tags to an existing note. Tags is a comma-separated string."""
    try:
        notes = _load_notes()
        if title not in notes:
            return f"❌ Note '{title}' not found"
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
        existing = notes[title].get("tags", [])
        # Merge, preserving order, no duplicates
        merged = list(dict.fromkeys(existing + tag_list))
        notes[title]["tags"]    = merged
        notes[title]["updated"] = datetime.now().isoformat()
        _save_notes(notes)
        return f"✅ Tags on '{title}': {', '.join(merged)}"
    except Exception as e:
        return f"❌ Error tagging note: {e}"


def list_by_tag(tag_name: str) -> str:
    """List all notes that have a specific tag."""
    try:
        notes   = _load_notes()
        tag_lc  = tag_name.strip().lower()
        matches = [
            (title, data) for title, data in notes.items()
            if tag_lc in [t.lower() for t in data.get("tags", [])]
        ]
        if not matches:
            return f"📭 No notes tagged '{tag_name}'"
        result = f"🏷️  Notes tagged '{tag_name}':\n"
        for i, (title, data) in enumerate(matches, 1):
            preview = data["content"][:60].replace("\n", " ")
            result += f"{i}. **{title}** — {preview}...\n"
        return result
    except Exception as e:
        return f"❌ Error listing by tag: {e}"


def search(keyword: str) -> str:
    """Search notes by keyword"""
    try:
        notes = _load_notes()
        results = []
        for title, data in notes.items():
            if keyword.lower() in data["content"].lower() or keyword.lower() in title.lower():
                results.append(f"- **{title}**: {data['content'][:100]}...")
        
        if results:
            return "🔍 Search results:\n" + "\n".join(results)
        return f"❌ No notes found containing '{keyword}'"
    except Exception as e:
        return f"❌ Error searching notes: {e}"

def export_all() -> str:
    """Export all notes as JSON string"""
    try:
        notes = _load_notes()
        return json.dumps(notes, indent=2)
    except Exception as e:
        return f"❌ Error exporting notes: {e}"

def get_last_logs(n: int = 10) -> str:
    """Retrieve the last N entries from session logs"""
    n = int(n)
    if not _LOGS_FILE.exists():
        return "❌ No session logs found."
    try:
        lines = _LOGS_FILE.read_text().splitlines()
        entries = [json.loads(l) for l in lines[-n:] if l.strip()]
        return json.dumps(entries, indent=2)
    except Exception as e:
        return f"❌ Error reading logs: {e}"

def search_logs(keyword: str) -> str:
    """Search session logs for a keyword (e.g. 'error', 'git')"""
    if not _LOGS_FILE.exists():
        return "❌ No session logs found."
    try:
        matches = []
        for line in _LOGS_FILE.read_text().splitlines():
            if line.strip() and keyword.lower() in line.lower():
                matches.append(json.loads(line))
        if not matches:
            return f"❌ No log entries found containing '{keyword}'"
        return json.dumps(matches[-5:], indent=2)
    except Exception as e:
        return f"❌ Error searching logs: {e}"


# ── Daily Journal ──────────────────────────────────────────────────────────────

def _load_journal() -> dict:
    entries = {}
    if not JOURNAL_FILE.exists():
        return entries
    try:
        for line in JOURNAL_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    e = json.loads(line)
                    entries[e["date"]] = e
                except Exception:
                    pass
    except Exception as exc:
        logger.error(f"Failed to load journal: {exc}")
    return entries


def _save_journal(entries: dict) -> None:
    JOURNAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e) for e in sorted(entries.values(), key=lambda x: x["date"])]
    JOURNAL_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_daily_entry(summary: str, learned: str, user_insights: str = "", next_steps: str = "") -> str:
    """Save or update today's journal entry. Appends to an existing entry if one exists for today."""
    try:
        today = date.today().isoformat()
        entries = _load_journal()
        if today in entries:
            old = entries[today]
            summary = (old.get("summary", "") + "\n\n[UPDATE] " + summary).strip()
            learned = (old.get("learned", "") + ("\n" + learned if learned else "")).strip()
            user_insights = (old.get("user_insights", "") + ("\n" + user_insights if user_insights else "")).strip()
            next_steps = next_steps or old.get("next_steps", "")
        entries[today] = {
            "date": today,
            "written_at": datetime.now().isoformat(),
            "summary": summary,
            "learned": learned,
            "user_insights": user_insights,
            "next_steps": next_steps,
        }
        _save_journal(entries)
        return f"✅ Daily entry for {today} saved."
    except Exception as e:
        return f"❌ Error writing daily entry: {e}"


def get_journal(days: int = 7) -> str:
    """Return journal entries for the last N days, newest first."""
    try:
        try:
            days = int(str(days).strip())
        except (ValueError, TypeError):
            days = 7
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        entries = _load_journal()
        recent = [e for e in entries.values() if e["date"] >= cutoff]
        if not recent:
            return f"No journal entries in the last {days} days."
        result = []
        for e in sorted(recent, key=lambda x: x["date"], reverse=True):
            result.append(f"=== {e['date']} ===")
            result.append(f"Summary: {e['summary']}")
            if e.get("learned"):
                result.append(f"Learned: {e['learned']}")
            if e.get("user_insights"):
                result.append(f"User insights: {e['user_insights']}")
            if e.get("next_steps"):
                result.append(f"Next steps: {e['next_steps']}")
            result.append("")
        return "\n".join(result).strip()
    except Exception as e:
        return f"❌ Error reading journal: {e}"


def get_today() -> str:
    """Return today's journal entry, or a message if none exists yet."""
    try:
        today = date.today().isoformat()
        entries = _load_journal()
        if today not in entries:
            return f"No entry for today ({today}) yet."
        e = entries[today]
        parts = [f"Today ({today}):"]
        parts.append(f"Summary: {e['summary']}")
        if e.get("learned"):
            parts.append(f"Learned: {e['learned']}")
        if e.get("user_insights"):
            parts.append(f"User insights: {e['user_insights']}")
        if e.get("next_steps"):
            parts.append(f"Next steps: {e['next_steps']}")
        return "\n".join(parts)
    except Exception as e:
        return f"❌ Error reading today's entry: {e}"


# ── User Model ─────────────────────────────────────────────────────────────────

def update_user_model(insight: str) -> str:
    """Append a new insight about the user to the persistent user model."""
    try:
        model = {}
        if USER_MODEL_FILE.exists():
            model = json.loads(USER_MODEL_FILE.read_text(encoding="utf-8"))
        model.setdefault("insights", []).append({
            "date": datetime.now().isoformat()[:10],
            "insight": insight,
        })
        model["last_updated"] = datetime.now().isoformat()
        USER_MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
        USER_MODEL_FILE.write_text(json.dumps(model, indent=2), encoding="utf-8")
        return f"✅ User model updated: {insight[:80]}"
    except Exception as e:
        return f"❌ Error updating user model: {e}"


def get_user_model() -> str:
    """Return all accumulated insights about the user (last 30)."""
    try:
        if not USER_MODEL_FILE.exists():
            return "No user model built yet."
        model = json.loads(USER_MODEL_FILE.read_text(encoding="utf-8"))
        insights = model.get("insights", [])
        if not insights:
            return "No user insights recorded yet."
        result = [f"User Model ({len(insights)} total insights, showing last 30):"]
        for item in insights[-30:]:
            result.append(f"[{item['date']}] {item['insight']}")
        return "\n".join(result)
    except Exception as e:
        return f"❌ Error reading user model: {e}"


# ── Activity Log ───────────────────────────────────────────────────────────────

def log_activity(action: str, result: str, source: str = "manual") -> str:
    """Log a completed manual task to the shared activity log.
    Call this after finishing any significant user-requested task.
    action: short description of what was done (e.g. 'liked 2 AI tweets')
    result: outcome summary (e.g. '✅ Done' or '❌ failed: ...')
    source: who triggered it (default 'manual')"""
    try:
        _ACTIVITY_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = json.dumps({
            "ts":     datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "action": action[:120],
            "result": result[:200],
            "ok":     not result.startswith("❌"),
        })
        with _ACTIVITY_LOG.open("a", encoding="utf-8") as f:
            f.write(entry + "\n")
        return f"✅ Activity logged: {action[:60]}"
    except Exception as e:
        return f"❌ log_activity error: {e}"


def append(title: str, content: str) -> str:
    """Add content to the bottom of an existing note without overwriting it.
    Creates the note if it does not exist yet."""
    try:
        notes = _load_notes()
        if title in notes:
            notes[title]["content"] = notes[title]["content"] + "\n\n" + content
            notes[title]["updated"] = datetime.now().isoformat()
            action = "Appended to"
        else:
            notes[title] = {
                "content": content,
                "created": datetime.now().isoformat(),
                "updated": datetime.now().isoformat(),
                "tags":    [],
            }
            action = "Created"
        _save_notes(notes)
        return f"✅ {action} note '{title}' ({len(content)} chars added)"
    except Exception as e:
        return f"❌ Error appending to note: {e}"


def end_day(summary: str, next_steps: str = "") -> str:
    """Wrap up the day: writes today's journal entry with a full activity summary.
    Automatically pulls today's activity log so nothing important is missed."""
    try:
        today = date.today().isoformat()

        # Collect today's activity log entries (midnight → now)
        activity_lines = []
        if _ACTIVITY_LOG.exists():
            midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            for line in _ACTIVITY_LOG.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    if datetime.fromisoformat(e["ts"]) >= midnight:
                        icon = "✅" if e.get("ok") else "❌"
                        activity_lines.append(f"{icon} {e['action'][:80]}")
                except Exception:
                    continue

        activity_block = "\n".join(activity_lines) if activity_lines else "No logged activity today."
        learned = f"Tasks today ({len(activity_lines)}):\n{activity_block}"

        # Write (or append to) today's journal entry
        journal_result = write_daily_entry(
            summary=summary,
            learned=learned,
            next_steps=next_steps,
        )

        lines = [
            f"🌙 Day wrapped — {today}",
            "=" * 42,
            f"Summary: {summary}",
            "",
            learned,
        ]
        if next_steps:
            lines += ["", f"Tomorrow: {next_steps}"]
        lines += ["", journal_result]
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error in end_day: {e}"


def get_activity_log(hours: int = 24) -> str:
    """Show what the agent did in the last N hours (scheduled + manual tasks).
    Returns a formatted list of all logged actions with ✅/❌ status."""
    try:
        try:
            hours = int(str(hours).strip())
        except (ValueError, TypeError):
            hours = 24
        if not _ACTIVITY_LOG.exists():
            return "📭 No activity logged yet."
        cutoff = datetime.now() - timedelta(hours=hours)
        entries = []
        for line in _ACTIVITY_LOG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                ts = datetime.fromisoformat(e["ts"])
                if ts >= cutoff:
                    entries.append(e)
            except Exception:
                continue
        if not entries:
            return f"📭 No activity in the last {hours}h."
        lines = [f"📋 Activity log — last {hours}h ({len(entries)} entries):"]
        for e in entries:
            icon = "✅" if e.get("ok") else "❌"
            lines.append(f"  {e['ts']}  {icon}  [{e['source']}]  {e['action'][:70]}")
            if not e.get("ok"):
                lines.append(f"       ↳ {e['result'][:100]}")
        return "\n".join(lines)
    except Exception as ex:
        return f"❌ get_activity_log error: {ex}"
