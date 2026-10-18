"""Open loops: promises I make in conversation, tracked until closed.

States: open -> done | expired (lazy 14-day expiry on read/write).
DOC supplements the system prompt; function names below are the LLM tool API.
"""
import json
import os
import secrets
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_LIST = list

DOC = "Track promises I make in conversation (open loops) until they're closed or expired."
SHORT_DOC = "add(text) to register a promise; done(id) when fulfilled; list() to inspect. Loops older than 14d auto-expire."

__all__ = ["add", "done", "list"]

_MAX_OPEN = 20
_MAX_TEXT = 200
_EXPIRY_DAYS = 14
_VALID_STATES = ("open", "done", "expired")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts) -> datetime | None:
    """Parse an ISO timestamp; naive strings are treated as UTC (tz hardening)."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _home() -> Path:
    return Path(os.getenv("MEMORY_HOME", "/app/memory"))


def _file() -> Path:
    return _home() / "open_loops.json"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n",
                                      dir=str(path.parent), delete=False)
    try:
        tmp.write(text)
        tmp.close()
        os.replace(tmp.name, str(path))
    except Exception:
        try:
            tmp.close()
        except Exception:
            pass
        try:
            os.remove(tmp.name)
        except OSError:
            pass
        raise


def _dump(loops: _LIST) -> str:
    return json.dumps({"_updated": _now().isoformat(), "loops": loops},
                      ensure_ascii=False, indent=2)


def _load() -> tuple:
    """Load loops, lazily expiring stale open ones. Returns (loops, changed).

    Corrupt rows (missing id/text/opened, unknown state) are dropped and the
    change is reported so callers can persist the cleaned list."""
    f = _file()
    if not f.exists():
        return [], False
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return [], False
    if not isinstance(data, dict):
        return [], True
    raw = data.get("loops", [])
    # module-level def list() shadows the builtin at call time — use _LIST
    if not isinstance(raw, _LIST):
        return [], False
    now = _now()
    loops, changed = [], False
    for row in raw:
        if not isinstance(row, dict):
            changed = True
            continue
        opened = _parse(row.get("opened"))
        state = row.get("state")
        if not row.get("id") or not row.get("text") or opened is None \
                or state not in _VALID_STATES:
            changed = True
            continue
        if state == "open" and now - opened > timedelta(days=_EXPIRY_DAYS):
            row["state"], row["resolved"] = "expired", now.isoformat()
            changed = True
        loops.append(row)
    return loops, changed


def _save_clean() -> None:
    loops, changed = _load()
    if changed or not _file().exists():
        _atomic_write(_file(), _dump(loops))


def _new_id(existing: set) -> str:
    for _ in range(3):
        cand = secrets.token_hex(2)
        if cand not in existing:
            return cand
    suffix = 0
    while True:
        cand = secrets.token_hex(2) + str(suffix)
        if cand not in existing:
            return cand
        suffix += 1


def add(text: str) -> str:
    """Register a promise I just made to the user. Errors if 20 open."""
    if not isinstance(text, str) or not text.strip():
        return "Error: text required (the promise, verbatim)."
    text = text.strip()[:_MAX_TEXT]
    loops, _ = _load()
    if sum(1 for l in loops if l["state"] == "open") >= _MAX_OPEN:
        return (f"Error: loop limit ({_MAX_OPEN}) reached. Close one first via "
                f"open_loops__done(id), or ask the user which stale promise to drop.")
    loop = {"id": _new_id({l["id"] for l in loops}), "text": text,
            "opened": _now().isoformat(), "state": "open", "resolved": None}
    loops.append(loop)
    _atomic_write(_file(), _dump(loops))
    return f"Logged. Open loop {loop['id']}: {text}"


def done(loop_id: str) -> str:
    """Mark a loop handled. Idempotent; works on expired loops too."""
    loop_id = str(loop_id).strip()
    loops, _ = _load()
    for loop in loops:
        if loop["id"] == loop_id:
            if loop["state"] == "done":
                return f"Loop {loop_id}: already closed. Nothing to do."
            loop["state"], loop["resolved"] = "done", _now().isoformat()
            _atomic_write(_file(), _dump(loops))
            return f"Loop {loop_id} marked done: {loop['text']}"
    open_ls = [l for l in loops if l["state"] == "open"]
    shown = "; ".join(f"{l['id']} ({l['text'][:40]})" for l in open_ls[:5])
    if len(open_ls) > 5:
        shown += f"; … +{len(open_ls) - 5} more"
    shown = shown or "(none)"
    return f"Error: no loop with id '{loop_id}'. Open loops: {shown}"


def list(state=None) -> str:
    """Inventory loops (optionally filtered by state)."""
    if state is not None and state not in _VALID_STATES:
        return f"Error: state must be one of {_VALID_STATES}."
    loops, _ = _load()
    _save_clean()
    rows = [l for l in loops if state is None or l["state"] == state]
    if not rows:
        return f"No{' ' + state if state else ''} loops."
    now = _now()
    lines = []
    for l in rows:
        opened = _parse(l["opened"])
        days = max(0, (now - opened).days) if opened else "?"
        lines.append(f"{l['id']}  {l['text']}  ({l['state']}, {days}d)")
    summary = f"Loops ({len(rows)}):\n" + "\n".join(lines)
    open_n = sum(1 for l in loops if l["state"] == "open")
    return summary + f"\n{_MAX_OPEN - open_n} of {_MAX_OPEN} slots free."


def _render_block(clock=None) -> str:
    """Snapshot payload. '' when nothing to show. Open loops numbered,
    newest first; expired loops summarized in one ownership line."""
    loops, _ = _load()
    _save_clean()
    now = clock() if callable(clock) else _now()
    open_ls = [l for l in loops if l["state"] == "open"]
    exp_ls = [l for l in loops if l["state"] == "expired"]
    if not open_ls and not exp_ls:
        return ""
    lines = []
    if open_ls:
        lines.append("Open follow-ups (promises you made):")
        for i, l in enumerate(reversed(open_ls[-6:]), 1):
            opened = _parse(l["opened"])
            days = max(0, (now - opened).days) if opened else "?"
            lines.append(f"{i}. {l['text']} — opened {days}d ago")
        if len(open_ls) > 6:
            lines.append(f"… {len(open_ls) - 6} more (oldest)")
    if exp_ls:
        texts = ", ".join(f"'{l['text'][:60]}'" for l in exp_ls[-3:])
        lines.append(f"Dropped promises (expired — re-commit or own it): {texts}")
    return "\n".join(lines)
