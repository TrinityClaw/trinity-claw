import json
import logging
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timedelta

# Ensure this directory is on sys.path so the private store modules are importable
_CORE_DIR = str(Path(__file__).parent)
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)

from _store_utils import _trunc, _file_lock, _atomic_write
from _user_model_store import (
    set_preference, get_preference, get_preference_history,
    set_context, delete_context, record_pattern, add_rejection,
    update_user_model, get_user_model, get_context_for_prompt, prune_user_model,
    set_user_fact, get_user_facts_card, get_fact_history,
    add_goal, complete_goal, cancel_goal, list_goals, get_goals_for_prompt,
)
from _journal_store import (
    write_daily_entry, get_journal, get_today, get_archive, compress_journal, end_day,
)

# Explicit re-exports: the skill loader resolves functions via getattr() at
# runtime, so these imports are used — they are not dead code.
__all__ = [
    # notes (defined below)
    "save", "load", "list_notes", "delete", "tag", "delete_tag",
    "list_by_tag", "search", "export_all", "append",
    # session + activity logs (defined below)
    "get_last_logs", "search_logs", "log_activity", "get_activity_log",
    # FTS5 episodic search (also called internally from app.py)
    "fts_index_entry",
    # user model (re-exported from _user_model_store)
    "set_preference", "get_preference", "get_preference_history",
    "set_context", "record_pattern", "add_rejection",
    "update_user_model", "get_user_model", "get_context_for_prompt", "prune_user_model",
    "set_user_fact", "get_user_facts_card", "get_fact_history",
    # goal tracking (re-exported from _user_model_store)
    "add_goal", "complete_goal", "cancel_goal", "list_goals", "get_goals_for_prompt",
    # context management (re-exported from _user_model_store)
    "delete_context",
    # journal (re-exported from _journal_store)
    "write_daily_entry", "get_journal", "get_today", "get_archive", "compress_journal", "end_day",
]

NAME = "notes"
logger = logging.getLogger(__name__)
SHORT_DOC = "Persistent notes storage — save, load, search, tag, and delete notes across sessions."
DOC = (
    "Persistent notes storage across sessions. "
    "Functions: "
    "save(title, content, tags?)→save note; tags is optional comma-separated string e.g. 'meeting,q1'; "
    "load(title)→retrieve note text; "
    "tag(title, tags)→add tags to an existing note; "
    "delete_tag(title, tags)→remove one or more tags from a note; tags is comma-separated; "
    "list_notes()→all notes with previews and tags; "
    "list_by_tag(tag)→filter notes by a specific tag; "
    "search(query)→notes matching keyword; "
    "delete(title)→remove note; "
    "export_all()→JSON dump of all notes; "
    "write_daily_entry(summary, learned, user_insights?, next_steps?)→save today's learning journal; "
    "get_journal(days=7)→retrieve last N days of journal entries; pass an integer e.g. get_journal(7); "
    "get_today()→get today's journal entry; "
    "update_user_model(insight)→append a free-form insight about the user to the persistent profile; "
    "get_user_model()→retrieve the full structured user model (preferences, patterns, context, rejections, insights); "
    "set_preference(key, value, source?, confidence?)→set a named user preference; source: user|inferred|system, confidence 0.0-1.0; "
    "get_preference(key, default?)→retrieve a single preference value; "
    "set_context(key, value)→update current working context (e.g. project, focus, deadline); "
    "delete_context(key)→remove a key from the current working context; "
    "record_pattern(pattern, evidence?, action?)→add or increment a behavioral pattern Trinity has observed; "
    "add_rejection(idea, reason?)→record a dismissed idea so Trinity never suggests it again; "
    "get_context_for_prompt()→compact user model summary for injection into system prompt — call at session start; "
    "prune_user_model(days_old?)→remove stale inferred patterns and low-confidence inferred preferences (default 30 days); "
    "log_activity(action, result, source?)→log a completed manual task to the activity log (source defaults to 'manual'); "
    "get_activity_log(hours?)→show what the agent did in the last N hours, both scheduled and manual (default 24h); "
    "append(title, content, tags?)→add content to an existing note without overwriting it (creates note if it does not exist yet); "
    "end_day(summary, next_steps?, user_insights?)→wrap the day: writes today's journal entry with auto-pulled activity log and returns a full day overview; user_insights is what you learned about the user today; "
    "set_user_fact(key, value, source?, episode_id?)→store a permanent fact about the user (e.g. language, name, projects); archives previous value automatically; always call when learning stable user info; "
    "get_user_facts_card()→return compact user fact card with source and valid_from metadata; "
    "get_fact_history(key)→return full timeline of a user fact — current value plus all archived previous values with validity windows; "
    "get_preference_history(key)→return full timeline of a preference — current plus archived previous values; "
    "get_archive(days=30)→return full original journal entries from the archive for the last N days (entries that were compressed out of the active journal); "
    "compress_journal(days_old=15)→compress journal entries older than N days: archives originals to daily_journal_archive.jsonl and replaces them with summary-only stubs to reduce token load; called automatically by end_day(); "
    "add_goal(goal, due_date?, context?)→track a user goal or objective; due_date is optional ISO date e.g. '2026-04-30'; "
    "complete_goal(goal_text)→mark a goal as completed by case-insensitive substring match; "
    "cancel_goal(goal_text)→mark a goal as cancelled (abandoned/invalid) by case-insensitive substring match; "
    "list_goals(status?)→show goals filtered by status ('open' or 'completed', default 'open'); "
    "get_goals_for_prompt()→compact active goal list for system prompt injection."
)

_LOGS_FILE    = Path("/app/memory/session_logs.jsonl")
_ACTIVITY_LOG = Path("/app/memory/activity_log.jsonl")
NOTES_FILE    = Path("/app/memory/notes.json")

# ── FTS5 session search ────────────────────────────────────────────────────────
_FTS_DB = Path("/app/memory/databases/session_fts.db")


def _fts_connect() -> sqlite3.Connection:
    _FTS_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(_FTS_DB), check_same_thread=False)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS session_fts USING fts5("
        "  entry_id UNINDEXED,"
        "  session_id UNINDEXED,"
        "  timestamp UNINDEXED,"
        "  user,"
        "  assistant,"
        "  tokenize='porter unicode61'"
        ")"
    )
    con.commit()
    return con


def _fts_backfill() -> None:
    """Index any session_logs.jsonl entries not yet in the FTS5 DB."""
    if not _LOGS_FILE.exists():
        return
    try:
        con = _fts_connect()
        existing = {r[0] for r in con.execute("SELECT entry_id FROM session_fts")}
        rows = []
        for line in _LOGS_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            eid = e.get("id") or e.get("timestamp", "")
            if eid and eid not in existing:
                rows.append((eid, e.get("session_id") or "", e.get("timestamp", ""),
                             e.get("user", ""), e.get("assistant", "")))
        if rows:
            con.executemany(
                "INSERT INTO session_fts(entry_id, session_id, timestamp, user, assistant)"
                " VALUES (?,?,?,?,?)",
                rows,
            )
            con.commit()
        con.close()
    except Exception as exc:
        logging.getLogger(__name__).warning("FTS5 backfill failed: %s", exc)


def fts_index_entry(entry_id: str, session_id: str, timestamp: str,
                    user: str, assistant: str) -> None:
    """Index a single session entry. Called from app.py after every save_to_jsonl."""
    try:
        con = _fts_connect()
        # Skip if already indexed (idempotent)
        if not con.execute("SELECT 1 FROM session_fts WHERE entry_id=?", (entry_id,)).fetchone():
            con.execute(
                "INSERT INTO session_fts(entry_id, session_id, timestamp, user, assistant)"
                " VALUES (?,?,?,?,?)",
                (entry_id, session_id or "", timestamp, user, assistant),
            )
            con.commit()
        con.close()
    except Exception as exc:
        logging.getLogger(__name__).warning("FTS5 index failed: %s", exc)


# Backfill on module load (fast when DB is already up-to-date)
try:
    _fts_backfill()
except Exception:
    pass

MAX_NOTES   = 500
MAX_TITLE   = 200
MAX_CONTENT = 100_000
MAX_TAG_LEN = 50
MAX_TAGS    = 20


def _clean_tags(raw: str) -> list:
    tags = [_trunc(t.strip(), MAX_TAG_LEN) for t in raw.split(",") if t.strip()]
    return tags[:MAX_TAGS]


# ── Notes ──────────────────────────────────────────────────────────────────────

def _load_notes() -> dict:
    NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
    if NOTES_FILE.exists():
        try:
            return json.loads(NOTES_FILE.read_text())
        except Exception as e:
            logger.error(f"Failed to parse notes file, returning empty: {e}")
            return {}
    return {}


def _save_notes(notes: dict) -> None:
    _atomic_write(NOTES_FILE, json.dumps(notes, indent=2))


def save(title: str, content: str, tags: str = "") -> str:
    """Save a note with optional comma-separated tags."""
    try:
        title    = _trunc(title, MAX_TITLE)
        content  = _trunc(content, MAX_CONTENT)
        tag_list = _clean_tags(tags)
        with _file_lock(NOTES_FILE):
            notes = _load_notes()
            if title not in notes and len(notes) >= MAX_NOTES:
                return f"❌ Note limit reached ({MAX_NOTES}). Delete old notes before saving new ones."
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
    """Load a note by title."""
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
    """List all note titles with metadata and tags."""
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
    """Delete a note."""
    try:
        with _file_lock(NOTES_FILE):
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
        tag_list = _clean_tags(tags)
        with _file_lock(NOTES_FILE):
            notes = _load_notes()
            if title not in notes:
                return f"❌ Note '{title}' not found"
            existing = notes[title].get("tags", [])
            merged   = list(dict.fromkeys(existing + tag_list))[:MAX_TAGS]
            notes[title]["tags"]    = merged
            notes[title]["updated"] = datetime.now().isoformat()
            _save_notes(notes)
        return f"✅ Tags on '{title}': {', '.join(merged)}"
    except Exception as e:
        return f"❌ Error tagging note: {e}"


def delete_tag(title: str, tags: str) -> str:
    """Remove one or more tags from a note. Tags is a comma-separated string."""
    try:
        to_remove = {t.strip().lower() for t in tags.split(",") if t.strip()}
        with _file_lock(NOTES_FILE):
            notes = _load_notes()
            if title not in notes:
                return f"❌ Note '{title}' not found"
            current = notes[title].get("tags", [])
            updated = [t for t in current if t.lower() not in to_remove]
            notes[title]["tags"]    = updated
            notes[title]["updated"] = datetime.now().isoformat()
            _save_notes(notes)
        removed = len(current) - len(updated)
        tag_str = ", ".join(updated) if updated else "(none)"
        return f"✅ Removed {removed} tag(s) from '{title}'. Remaining: {tag_str}"
    except Exception as e:
        return f"❌ Error deleting tag: {e}"


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


def search(query: str) -> str:
    """Search notes by keyword."""
    try:
        notes   = _load_notes()
        results = []
        for title, data in notes.items():
            if query.lower() in data["content"].lower() or query.lower() in title.lower():
                results.append(f"- **{title}**: {data['content'][:100]}...")
        if results:
            return "🔍 Search results:\n" + "\n".join(results)
        return f"❌ No notes found containing '{query}'"
    except Exception as e:
        return f"❌ Error searching notes: {e}"


def export_all() -> str:
    """Export all notes as JSON string."""
    try:
        notes = _load_notes()
        return json.dumps(notes, indent=2)
    except Exception as e:
        return f"❌ Error exporting notes: {e}"


def append(title: str, content: str, tags: str = "") -> str:
    """Add content to the bottom of an existing note without overwriting it.
    Creates the note if it does not exist yet. Optional tags apply only when creating."""
    try:
        title    = _trunc(title, MAX_TITLE)
        content  = _trunc(content, MAX_CONTENT)
        tag_list = _clean_tags(tags)
        with _file_lock(NOTES_FILE):
            notes = _load_notes()
            if title in notes:
                existing = notes[title]["content"]
                combined = _trunc(existing + "\n\n" + content, MAX_CONTENT)
                notes[title]["content"] = combined
                notes[title]["updated"] = datetime.now().isoformat()
                if tag_list:
                    merged = list(dict.fromkeys(notes[title].get("tags", []) + tag_list))[:MAX_TAGS]
                    notes[title]["tags"] = merged
                action = "Appended to"
            else:
                if len(notes) >= MAX_NOTES:
                    return f"❌ Note limit reached ({MAX_NOTES}). Delete old notes before creating new ones."
                notes[title] = {
                    "content": content,
                    "created": datetime.now().isoformat(),
                    "updated": datetime.now().isoformat(),
                    "tags":    tag_list,
                }
                action = "Created"
            _save_notes(notes)
        return f"✅ {action} note '{title}' ({len(content)} chars added)"
    except Exception as e:
        return f"❌ Error appending to note: {e}"


# ── Session Logs ───────────────────────────────────────────────────────────────

def get_last_logs(n: int = 10, hours: int = None) -> str:
    """Retrieve the last N entries from session logs, optionally filtered to the last `hours`."""
    n = int(n)
    cutoff = None
    if hours is not None:
        try:
            cutoff = datetime.now() - timedelta(hours=int(hours))
        except (ValueError, TypeError):
            pass
    if not _LOGS_FILE.exists():
        return "❌ No session logs found."
    try:
        lines   = _LOGS_FILE.read_text().splitlines()
        entries = []
        for l in reversed(lines):
            if not l.strip():
                continue
            try:
                e = json.loads(l)
                ts = datetime.fromisoformat(e["timestamp"])
                if cutoff is not None and ts < cutoff:
                    break
                entries.append(e)
                if len(entries) >= n:
                    break
            except Exception:
                continue
        return json.dumps(list(reversed(entries)), indent=2)
    except Exception as e:
        return f"❌ Error reading logs: {e}"


def search_logs(keyword: str, limit: int = 8) -> str:
    """Search session logs using FTS5 full-text search (supports phrases, prefix*, boolean AND/OR/NOT).
    Falls back to linear scan if the FTS5 DB is unavailable.
    keyword: search query e.g. 'send email', 'git*', 'deploy AND error'
    limit: max results to return (default 8)"""
    limit = min(int(limit), 20)
    try:
        con = _fts_connect()
        rows = con.execute(
            "SELECT timestamp, session_id, user, assistant"
            " FROM session_fts"
            " WHERE session_fts MATCH ?"
            " ORDER BY rank"
            " LIMIT ?",
            (keyword, limit),
        ).fetchall()
        con.close()
        if not rows:
            return f"No session log entries matched '{keyword}'."
        results = [
            {"timestamp": r[0], "session_id": r[1], "user": r[2], "assistant": r[3][:400]}
            for r in rows
        ]
        return json.dumps(results, indent=2)
    except Exception as fts_err:
        # FTS5 unavailable — fall back to linear scan
        logging.getLogger(__name__).warning("FTS5 search failed (%s), using linear scan", fts_err)
        if not _LOGS_FILE.exists():
            return "❌ No session logs found."
        try:
            matches = []
            for line in _LOGS_FILE.read_text(encoding="utf-8").splitlines():
                if line.strip() and keyword.lower() in line.lower():
                    matches.append(json.loads(line))
            if not matches:
                return f"No log entries found containing '{keyword}'"
            return json.dumps(matches[-limit:], indent=2)
        except Exception as e:
            return f"❌ Error searching logs: {e}"


# ── Activity Log ───────────────────────────────────────────────────────────────

def log_activity(action: str, result: str, source: str = "manual") -> str:
    """Log a completed manual task to the shared activity log.
    action: short description of what was done.
    result: outcome summary.
    source: who triggered it (default 'manual')."""
    try:
        _ACTIVITY_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = json.dumps({
            "ts":     datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "action": _trunc(action, 120),
            "result": _trunc(result, 200),
            "ok":     not result.startswith("❌"),
        })
        with _file_lock(_ACTIVITY_LOG):
            with _ACTIVITY_LOG.open("a", encoding="utf-8") as f:
                f.write(entry + "\n")
        return f"✅ Activity logged: {action[:60]}"
    except Exception as e:
        return f"❌ log_activity error: {e}"


def get_activity_log(hours: int = 24) -> str:
    """Show what the agent did in the last N hours (scheduled + manual tasks)."""
    try:
        try:
            hours = int(str(hours).strip())
        except (ValueError, TypeError):
            hours = 24
        if not _ACTIVITY_LOG.exists():
            return "📭 No activity logged yet."
        cutoff  = datetime.now() - timedelta(hours=hours)
        entries = []
        for line in _ACTIVITY_LOG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e  = json.loads(line)
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
