import json
import logging
from pathlib import Path
from datetime import datetime, date, timedelta

NAME = "notes"

logger = logging.getLogger(__name__)
SHORT_DOC = "Persistent notes storage — save, load, search, tag, and delete notes across sessions."
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
    "update_user_model(insight)→append a free-form insight about the user to the persistent profile; "
    "get_user_model()→retrieve the full structured user model (preferences, patterns, context, rejections, insights); "
    "set_preference(key, value, source?, confidence?)→set a named user preference; source: user|inferred|system, confidence 0.0-1.0; "
    "get_preference(key, default?)→retrieve a single preference value; "
    "set_context(key, value)→update current working context (e.g. project, focus, deadline); "
    "record_pattern(pattern, evidence?, action?)→add or increment a behavioral pattern Trinity has observed; "
    "add_rejection(idea, reason?)→record a dismissed idea so Trinity never suggests it again; "
    "get_context_for_prompt()→compact user model summary for injection into system prompt — call at session start; "
    "prune_user_model(days_old?)→remove stale inferred patterns and low-confidence inferred preferences (default 30 days); "
    "log_activity(action, result, source?)→log a completed manual task to the activity log (source defaults to 'manual'); "
    "get_activity_log(hours?)→show what the agent did in the last N hours, both scheduled and manual (default 24h); "
    "append(title, content)→add content to an existing note without overwriting it (creates note if it does not exist yet); "
    "end_day(summary, next_steps?)→wrap the day: writes today's journal entry with auto-pulled activity log and returns a full day overview; "
    "set_user_fact(key, value, source?, episode_id?)→store a permanent fact about the user (e.g. language, name, projects); archives previous value automatically; always call when learning stable user info; "
    "get_user_facts_card()→return compact user fact card with source and valid_from metadata; "
    "get_fact_history(key)→return full timeline of a user fact — current value plus all archived previous values with validity windows; "
    "get_preference_history(key)→return full timeline of a preference — current plus archived previous values; "
    "compress_journal(days_old=15)→compress journal entries older than N days: archives originals to daily_journal_archive.jsonl and replaces them with summary-only stubs to reduce token load; called automatically by end_day()."
)

_LOGS_FILE    = Path("/app/memory/session_logs.jsonl")
_ACTIVITY_LOG = Path("/app/memory/activity_log.jsonl")

NOTES_FILE             = Path("/app/memory/notes.json")
JOURNAL_FILE           = Path("/app/memory/daily_journal.jsonl")
JOURNAL_ARCHIVE_FILE   = Path("/app/memory/daily_journal_archive.jsonl")
USER_MODEL_FILE        = Path("/app/memory/user_model.json")
USER_FACTS_FILE        = Path("/app/memory/user_facts.json")
USER_FACTS_HISTORY_FILE = Path("/app/memory/user_facts_history.jsonl")
USER_PREFS_HISTORY_FILE = Path("/app/memory/user_prefs_history.jsonl")

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
    """Load all daily journal entries from the JSONL file. Returns a dict keyed by date string."""
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


def compress_journal(days_old: int = 15) -> str:
    """Compress journal entries older than days_old days to save tokens.
    Archives full originals to daily_journal_archive.jsonl, then replaces
    old entries in the main journal with a lean summary-only stub.
    Returns a brief status string. Called automatically by end_day()."""
    try:
        days_old = int(days_old)
        cutoff = (date.today() - timedelta(days=days_old)).isoformat()
        entries = _load_journal()

        old = {k: v for k, v in entries.items()
               if k < cutoff and not v.get("compressed")}
        if not old:
            return f"(journal already compact — no entries older than {days_old} days to compress)"

        # Archive originals first (append-safe)
        JOURNAL_ARCHIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with JOURNAL_ARCHIVE_FILE.open("a", encoding="utf-8") as f:
            for e in sorted(old.values(), key=lambda x: x["date"]):
                f.write(json.dumps(e) + "\n")

        # Replace with compressed stubs in the live journal
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
#
# Schema v2:
#   preferences:  {key: {value, confidence, source, updated_at}}
#   patterns:     [{pattern, evidence, evidence_count, last_seen, suggested_action}]
#   context:      {key: value}   — current project/focus/deadline
#   rejections:   [{idea, reason, dismissed_at}]
#   insights:     [{date, insight}]  — free-form, kept for backward compat
#   meta:         {schema_version, last_updated}
#
# Migration: v1 files only had "insights" + "last_updated" — _load_user_model()
# detects and upgrades them transparently on first read.

def _load_user_model() -> dict:
    """Load user model from disk, migrating from v1 (flat insights) if needed."""
    _empty = {
        "preferences": {}, "patterns": [], "context": {},
        "rejections": [], "insights": [],
        "meta": {"schema_version": 2, "last_updated": None},
    }
    if not USER_MODEL_FILE.exists():
        return _empty
    try:
        model = json.loads(USER_MODEL_FILE.read_text(encoding="utf-8"))
        if "preferences" not in model:          # v1 → v2 migration
            model = {
                **_empty,
                "insights": model.get("insights", []),
                "meta": {"schema_version": 2, "last_updated": model.get("last_updated")},
            }
        return model
    except Exception as exc:
        logger.error(f"Failed to load user model: {exc}")
        return _empty


def _save_user_model(model: dict) -> None:
    model.setdefault("meta", {})["last_updated"] = datetime.now().isoformat()
    USER_MODEL_FILE.parent.mkdir(parents=True, exist_ok=True)
    USER_MODEL_FILE.write_text(json.dumps(model, indent=2, ensure_ascii=False), encoding="utf-8")


def set_preference(key: str, value, source: str = "user", confidence: float = 1.0, episode_id: str = "") -> str:
    """Set a named preference. source: 'user'|'inferred'|'system'. confidence: 0.0–1.0.
    Automatically archives the previous value with a valid_until timestamp before overwriting."""
    try:
        model = _load_user_model()

        # Archive old value before overwriting
        old = model.get("preferences", {}).get(key)
        if old is not None:
            archived = {
                "key":        key,
                "value":      old.get("value"),
                "source":     old.get("source", "unknown"),
                "confidence": old.get("confidence", 1.0),
                "valid_from": old.get("valid_from") or old.get("updated_at", "")[:10],
                "valid_until": date.today().isoformat(),
                "episode_id": old.get("episode_id"),
                "archived_at": datetime.now().isoformat(),
            }
            USER_PREFS_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            with USER_PREFS_HISTORY_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(archived) + "\n")

        model["preferences"][key] = {
            "value":      value,
            "confidence": round(float(confidence), 2),
            "source":     source,
            "updated_at": datetime.now().isoformat(),
            "valid_from": date.today().isoformat(),
            "episode_id": episode_id or None,
        }
        _save_user_model(model)
        icon = {"user": "👤", "inferred": "🤖", "system": "⚙️"}.get(source, "•")
        return f"✅ Preference set: {key} = {value!r} {icon} (confidence: {float(confidence):.0%})"
    except Exception as e:
        return f"❌ set_preference error: {e}"


def get_preference(key: str, default=None):
    """Return a single preference value, or default if not set."""
    try:
        pref = _load_user_model()["preferences"].get(key)
        return pref["value"] if pref else default
    except Exception:
        return default


def get_preference_history(key: str) -> str:
    """Return the full timeline of a preference — current value plus all archived previous values."""
    try:
        lines = [f"📜 Preference history: '{key}'"]

        # Current value
        model = _load_user_model()
        current = model.get("preferences", {}).get(key)
        if current:
            vf   = current.get("valid_from") or current.get("updated_at", "?")[:10]
            src  = current.get("source", "?")
            conf = current.get("confidence", 1.0)
            ep   = current.get("episode_id") or ""
            ep_str = f" ep={ep}" if ep else ""
            lines.append(f"  [current]  {current['value']!r}  conf={conf:.0%}  since {vf}  ({src}{ep_str})")

        # Archived values
        if USER_PREFS_HISTORY_FILE.exists():
            history = []
            for line in USER_PREFS_HISTORY_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    if e.get("key") == key:
                        history.append(e)
                except Exception:
                    continue
            for e in sorted(history, key=lambda x: x.get("valid_from") or "", reverse=True):
                vf   = e.get("valid_from") or "?"
                vu   = e.get("valid_until") or "?"
                src  = e.get("source") or "?"
                conf = e.get("confidence", 1.0)
                ep   = e.get("episode_id") or ""
                ep_str = f" ep={ep}" if ep else ""
                lines.append(f"  [{vf} → {vu}]  {e['value']!r}  conf={conf:.0%}  ({src}{ep_str})")

        if len(lines) == 1:
            return f"No preference found for key '{key}'"
        return "\n".join(lines)
    except Exception as e:
        return f"❌ get_preference_history error: {e}"


def set_context(key: str, value: str) -> str:
    """Update one field of the current working context (e.g. project, focus, deadline)."""
    try:
        model = _load_user_model()
        model.setdefault("context", {})[key] = value
        _save_user_model(model)
        return f"✅ Context updated: {key} = {value!r}"
    except Exception as e:
        return f"❌ set_context error: {e}"


def record_pattern(pattern: str, evidence: str = "", action: str = "") -> str:
    """Add or increment a behavioral pattern Trinity has observed about the user."""
    try:
        model = _load_user_model()
        existing = next((p for p in model.get("patterns", []) if p["pattern"] == pattern), None)
        if existing:
            existing["evidence_count"] += 1
            existing["last_seen"] = datetime.now().isoformat()
            if action:
                existing["suggested_action"] = action
            count = existing["evidence_count"]
        else:
            model.setdefault("patterns", []).append({
                "pattern": pattern,
                "evidence": evidence,
                "evidence_count": 1,
                "last_seen": datetime.now().isoformat(),
                "suggested_action": action,
            })
            count = 1
        _save_user_model(model)
        return f"📝 Pattern recorded: '{pattern}' (seen {count}×)"
    except Exception as e:
        return f"❌ record_pattern error: {e}"


def add_rejection(idea: str, reason: str = "") -> str:
    """Record a dismissed idea so Trinity doesn't suggest it again."""
    try:
        model = _load_user_model()
        model.setdefault("rejections", []).append({
            "idea": idea,
            "reason": reason,
            "dismissed_at": datetime.now().isoformat()[:10],
        })
        _save_user_model(model)
        return f"🚫 Rejection recorded: '{idea}'"
    except Exception as e:
        return f"❌ add_rejection error: {e}"


def get_context_for_prompt() -> str:
    """
    Return a compact user-model block for injection into Trinity's system prompt.
    Only includes high-confidence preferences, strong patterns (≥3 evidence),
    active context, and the last few rejections.
    Returns an empty string if no useful data exists yet.
    """
    try:
        model = _load_user_model()
        lines = []

        prefs = {k: v for k, v in model.get("preferences", {}).items()
                 if v.get("confidence", 1.0) >= 0.6}
        if prefs:
            parts = [f"{k}={v['value']!r}" for k, v in prefs.items()]
            lines.append("User preferences: " + ", ".join(parts))

        ctx = model.get("context", {})
        if ctx:
            lines.append("Current context: " + ", ".join(f"{k}={v}" for k, v in ctx.items()))

        strong = [p for p in model.get("patterns", []) if p.get("evidence_count", 0) >= 3]
        if strong:
            parts = [
                p["pattern"] + (f" → {p['suggested_action']}" if p.get("suggested_action") else "")
                for p in strong[-4:]
            ]
            lines.append("Observed patterns: " + "; ".join(parts))

        rejections = model.get("rejections", [])
        if rejections:
            lines.append("Do not suggest: " + ", ".join(r["idea"] for r in rejections[-5:]))

        insights = model.get("insights", [])
        if insights:
            lines.append("Recent user insight: " + insights[-1]["insight"])

        return "\n".join(lines)
    except Exception as exc:
        logger.error(f"get_context_for_prompt error: {exc}")
        return ""


def prune_user_model(days_old: int = 30) -> str:
    """Remove stale inferred patterns and low-confidence inferred preferences."""
    try:
        days_old = int(days_old)
        model = _load_user_model()
        cutoff = (datetime.now() - timedelta(days=days_old)).isoformat()

        before_p = len(model.get("patterns", []))
        model["patterns"] = [
            p for p in model.get("patterns", [])
            if p.get("last_seen", "") >= cutoff
        ]
        pruned_p = before_p - len(model["patterns"])

        pruned_pref = 0
        for key in list(model.get("preferences", {}).keys()):
            p = model["preferences"][key]
            if (p.get("source") == "inferred"
                    and p.get("confidence", 1.0) < 0.6
                    and p.get("updated_at", "") < cutoff):
                del model["preferences"][key]
                pruned_pref += 1

        _save_user_model(model)
        return f"🧹 Pruned: {pruned_p} stale patterns, {pruned_pref} low-confidence preferences"
    except Exception as e:
        return f"❌ prune_user_model error: {e}"


def update_user_model(insight: str) -> str:
    """Append a free-form insight about the user to the persistent profile."""
    try:
        model = _load_user_model()
        model.setdefault("insights", []).append({
            "date": datetime.now().isoformat()[:10],
            "insight": insight,
        })
        _save_user_model(model)
        return f"✅ User model updated: {insight[:80]}"
    except Exception as e:
        return f"❌ Error updating user model: {e}"


def get_user_model() -> str:
    """Return the full structured user model."""
    try:
        if not USER_MODEL_FILE.exists():
            return "No user model built yet."
        model = _load_user_model()
        updated = (model.get("meta") or {}).get("last_updated") or "unknown"
        lines = [f"👤 User Model  (last updated: {updated[:10]})"]

        prefs = model.get("preferences", {})
        lines.append(f"\n🔹 Preferences ({len(prefs)}):")
        if prefs:
            for k, v in prefs.items():
                icon = {"user": "👤", "inferred": "🤖", "system": "⚙️"}.get(v.get("source", ""), "•")
                lines.append(f"  {icon} {k}: {v['value']!r}  conf={v.get('confidence', 1.0):.0%}  [{v.get('updated_at', '')[:10]}]")
        else:
            lines.append("  (none set)")

        patterns = model.get("patterns", [])
        lines.append(f"\n🔹 Patterns ({len(patterns)}):")
        if patterns:
            for p in sorted(patterns, key=lambda x: -x.get("evidence_count", 0))[:8]:
                action = f" → {p['suggested_action']}" if p.get("suggested_action") else ""
                lines.append(f"  • {p['pattern']} ({p.get('evidence_count', 1)}×){action}")
        else:
            lines.append("  (none yet)")

        ctx = model.get("context", {})
        lines.append("\n🔹 Current Context:")
        if ctx:
            for k, v in ctx.items():
                lines.append(f"  • {k}: {v}")
        else:
            lines.append("  (none active)")

        rejections = model.get("rejections", [])
        lines.append(f"\n🔹 Dismissed Ideas ({len(rejections)}):")
        if rejections:
            for r in rejections[-5:]:
                reason = f" — {r['reason']}" if r.get("reason") else ""
                lines.append(f"  • {r['idea']}{reason}")
        else:
            lines.append("  (none)")

        insights = model.get("insights", [])
        if insights:
            lines.append(f"\n🔹 Free-form Insights (last 5 of {len(insights)}):")
            for item in insights[-5:]:
                lines.append(f"  [{item['date']}] {item['insight']}")

        return "\n".join(lines)
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

        # Silently compress old journal entries to keep memory lean
        compress_journal(days_old=15)

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


# ── User Facts Card ────────────────────────────────────────────────────────────
# A flat key-value store of permanent facts about the user.
# Separate from user_model.json (which is complex and underused).
# Always injected into every system prompt — tiny, always relevant.

def set_user_fact(key: str, value: str, source: str = "user", episode_id: str = "") -> str:
    """Store a permanent fact about the user. key examples: language, name, projects, timezone.
    Call this whenever you learn something stable about the user that should persist across sessions.
    Automatically archives the previous value with a valid_until timestamp before overwriting."""
    try:
        facts: dict = {}
        if USER_FACTS_FILE.exists():
            try:
                facts = json.loads(USER_FACTS_FILE.read_text(encoding="utf-8"))
            except Exception:
                facts = {}

        k = key.strip().lower()

        # Archive old value before overwriting
        old = facts.get(k)
        if old is not None and not k.startswith("_"):
            if isinstance(old, dict):
                old_val  = old.get("value", "")
                old_src  = old.get("source", "unknown")
                old_vf   = old.get("valid_from")
                old_ep   = old.get("episode_id")
            else:
                old_val  = old
                old_src  = "unknown"
                old_vf   = None
                old_ep   = None
            archived = {
                "key":        k,
                "value":      old_val,
                "source":     old_src,
                "valid_from": old_vf,
                "valid_until": date.today().isoformat(),
                "episode_id": old_ep,
                "archived_at": datetime.now().isoformat(),
            }
            USER_FACTS_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
            with USER_FACTS_HISTORY_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(archived) + "\n")

        facts[k] = {
            "value":      value.strip(),
            "source":     source,
            "valid_from": date.today().isoformat(),
            "episode_id": episode_id or None,
        }
        facts["_updated"] = datetime.now().isoformat()
        USER_FACTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        USER_FACTS_FILE.write_text(json.dumps(facts, indent=2, ensure_ascii=False), encoding="utf-8")
        return f"✅ User fact saved: {key} = {value!r}"
    except Exception as e:
        return f"❌ set_user_fact error: {e}"


def get_user_facts_card() -> str:
    """Return all stored user facts as a readable card with source and valid_from metadata."""
    try:
        if not USER_FACTS_FILE.exists():
            return "No user facts saved yet."
        facts = json.loads(USER_FACTS_FILE.read_text(encoding="utf-8"))
        lines = []
        for k, v in facts.items():
            if k.startswith("_"):
                continue
            if isinstance(v, dict):
                display = v.get("value", "")
                src     = v.get("source", "")
                vf      = v.get("valid_from", "")
                meta    = f"  [{src}, since {vf}]" if (src or vf) else ""
                lines.append(f"  {k}: {display}{meta}")
            else:
                lines.append(f"  {k}: {v}")
        if not lines:
            return "No user facts saved yet."
        updated = facts.get("_updated", "")[:10]
        return f"User Facts (last updated {updated}):\n" + "\n".join(lines)
    except Exception as e:
        return f"❌ get_user_facts_card error: {e}"


def get_fact_history(key: str) -> str:
    """Return the full timeline of a user fact — current value plus all archived previous values
    with their validity windows and provenance (source, episode_id)."""
    try:
        k = key.strip().lower()
        lines = [f"📜 Fact history: '{k}'"]

        # Current value
        if USER_FACTS_FILE.exists():
            facts = json.loads(USER_FACTS_FILE.read_text(encoding="utf-8"))
            current = facts.get(k)
            if current is not None:
                if isinstance(current, dict):
                    vf     = current.get("valid_from", "?")
                    src    = current.get("source", "?")
                    ep     = current.get("episode_id") or ""
                    ep_str = f" ep={ep}" if ep else ""
                    lines.append(f"  [current]  {current['value']!r}  since {vf}  ({src}{ep_str})")
                else:
                    lines.append(f"  [current]  {current!r}  (no metadata)")

        # Archived values
        if USER_FACTS_HISTORY_FILE.exists():
            history = []
            for line in USER_FACTS_HISTORY_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    if e.get("key") == k:
                        history.append(e)
                except Exception:
                    continue
            for e in sorted(history, key=lambda x: x.get("valid_from") or "", reverse=True):
                vf     = e.get("valid_from") or "?"
                vu     = e.get("valid_until") or "?"
                src    = e.get("source") or "?"
                ep     = e.get("episode_id") or ""
                ep_str = f" ep={ep}" if ep else ""
                lines.append(f"  [{vf} → {vu}]  {e['value']!r}  ({src}{ep_str})")

        if len(lines) == 1:
            return f"No fact found for key '{k}'"
        return "\n".join(lines)
    except Exception as e:
        return f"❌ get_fact_history error: {e}"


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
