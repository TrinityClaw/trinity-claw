"""
Private module — daily journal, compression, and end-of-day wrap.
Not a skill. Imported by notes.py which re-exports everything under the 'notes' skill.
"""
import json
import logging
from pathlib import Path
from datetime import datetime, date, timedelta
from contextlib import contextmanager
import fcntl

logger = logging.getLogger(__name__)

JOURNAL_FILE         = Path("/app/memory/daily_journal.jsonl")
JOURNAL_ARCHIVE_FILE = Path("/app/memory/daily_journal_archive.jsonl")
_ACTIVITY_LOG        = Path("/app/memory/activity_log.jsonl")


def _trunc(s, n: int) -> str:
    return str(s)[:n]


@contextmanager
def _file_lock(path: Path):
    lock_path = path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _atomic_write(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding=encoding)
    tmp.replace(path)


# ── Journal ────────────────────────────────────────────────────────────────────

def _load_journal() -> dict:
    """Load all daily journal entries from the JSONL file. Returns dict keyed by date string."""
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
    lines = [json.dumps(e) for e in sorted(entries.values(), key=lambda x: x["date"])]
    _atomic_write(JOURNAL_FILE, "\n".join(lines) + "\n")


def compress_journal(days_old: int = 15) -> str:
    """Compress journal entries older than days_old days to save tokens.
    Archives full originals to daily_journal_archive.jsonl, then replaces
    old entries with lean summary-only stubs. Called automatically by end_day()."""
    try:
        days_old = int(days_old)
        cutoff   = (date.today() - timedelta(days=days_old)).isoformat()
        with _file_lock(JOURNAL_FILE):
            entries = _load_journal()
            old = {k: v for k, v in entries.items()
                   if k < cutoff and not v.get("compressed")}
            if not old:
                return f"(journal already compact — no entries older than {days_old} days to compress)"

            JOURNAL_ARCHIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with _file_lock(JOURNAL_ARCHIVE_FILE):
                with JOURNAL_ARCHIVE_FILE.open("a", encoding="utf-8") as f:
                    for e in sorted(old.values(), key=lambda x: x["date"]):
                        f.write(json.dumps(e) + "\n")

            for date_key, e in old.items():
                entries[date_key] = {
                    "date":       e["date"],
                    "written_at": e.get("written_at", ""),
                    "summary":    e.get("summary", "")[:250],
                    "compressed": True,
                }
            _save_journal(entries)

        return f"compressed {len(old)} journal entries older than {days_old} days (originals archived)"
    except Exception as ex:
        return f"compress_journal error: {ex}"


def write_daily_entry(summary: str, learned: str, user_insights: str = "", next_steps: str = "") -> str:
    """Save or update today's journal entry. Appends to an existing entry if one already exists."""
    try:
        today = date.today().isoformat()
        with _file_lock(JOURNAL_FILE):
            entries = _load_journal()
            if today in entries:
                old           = entries[today]
                summary       = (old.get("summary", "") + "\n\n[UPDATE] " + summary).strip()
                learned       = (old.get("learned", "") + ("\n" + learned if learned else "")).strip()
                user_insights = (old.get("user_insights", "") + ("\n" + user_insights if user_insights else "")).strip()
                next_steps    = next_steps or old.get("next_steps", "")
            entries[today] = {
                "date":          today,
                "written_at":    datetime.now().isoformat(),
                "summary":       summary,
                "learned":       learned,
                "user_insights": user_insights,
                "next_steps":    next_steps,
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
        cutoff  = (date.today() - timedelta(days=days)).isoformat()
        entries = _load_journal()
        recent  = [e for e in entries.values() if e["date"] >= cutoff]
        if not recent:
            return f"No journal entries in the last {days} days."
        result = []
        for e in sorted(recent, key=lambda x: x["date"], reverse=True):
            label = " [compressed]" if e.get("compressed") else ""
            result.append(f"=== {e['date']}{label} ===")
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
        today   = date.today().isoformat()
        entries = _load_journal()
        if today not in entries:
            return f"No entry for today ({today}) yet."
        e     = entries[today]
        label = " [compressed]" if e.get("compressed") else ""
        parts = [f"Today ({today}){label}:"]
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


def end_day(summary: str, next_steps: str = "", user_insights: str = "") -> str:
    """Wrap up the day: writes today's journal entry with a full activity summary.
    Automatically pulls today's activity log AND extracts user insights so nothing important is missed."""
    try:
        today = date.today().isoformat()

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
        learned        = f"Tasks today ({len(activity_lines)}):\n{activity_block}"

        # Auto-extract user insights if none provided
        if not user_insights.strip():
            # Derive insights from today's activity and interactions
            if activity_lines:
                # Look for patterns in today's activities that reveal user preferences/habits
                user_actions = [a for a in activity_lines if "user" in a.lower() or "request" in a.lower()]
                if user_actions:
                    user_insights = f"User interacted with agent on {len(user_actions)} tasks today"
                else:
                    user_insights = f"Agent completed {len(activity_lines)} tasks for user today"
            else:
                user_insights = "No user interactions logged today"

        journal_result  = write_daily_entry(
            summary=summary,
            learned=learned,
            user_insights=user_insights,
            next_steps=next_steps,
        )
        compress_result = compress_journal(days_old=15)

        lines = [
            f"🌙 Day wrapped — {today}",
            "=" * 42,
            f"Summary: {summary}",
            "",
            learned,
        ]
        if next_steps:
            lines += ["", f"Tomorrow: {next_steps}"]
        if user_insights:
            lines += ["", f"User insights: {user_insights}"]
        lines += ["", journal_result, f"(journal: {compress_result})"]
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error in end_day: {e}"
