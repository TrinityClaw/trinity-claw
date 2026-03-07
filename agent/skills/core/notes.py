import json
import logging
from pathlib import Path
from datetime import datetime

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
    "export_all()→JSON dump of all notes."
)

_LOGS_FILE = Path("/app/memory/session_logs.jsonl")

NOTES_FILE = Path("/app/memory/notes.json")

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
