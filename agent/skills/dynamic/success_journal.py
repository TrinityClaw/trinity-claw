"""
Success Journal — the agent learns from WINS, not just errors.

Every non-trivial successful task appends one compact line here (rate-limited
by the app.py reflection hook). The daily briefing reads the recent entries
("what I learned recently") and the nightly miner can find repeated skill
chains worth automating.

File: /app/memory/success_lessons.jsonl — append-only, you can clear it anytime.
"""
import json
import os
from datetime import datetime
from pathlib import Path

NAME = "success_journal"
SHORT_DOC = "Append/inspect compact records of successful task patterns."
DOC = (
    "Success journal: record(task, tools_used, duration_s, skills_chain) appends one "
    "line to memory/success_lessons.jsonl; recent(n=3) returns the last entries formatted. "
    "Feeds the daily briefing and weekly consolidation."
)
__all__ = ["NAME", "SHORT_DOC", "DOC", "record", "recent"]

_MAX_LINES = 400


def _path(memory_dir=None) -> Path:
    base = memory_dir or os.getenv("TRINITY_MEMORY_DIR", "/app/memory")
    return Path(base) / "success_lessons.jsonl"


def record(task: str = "", tools_used: int = 0, duration_s: int = 0,
           skills_chain: str = "", outcome: str = "success", memory_dir=None) -> str:
    """Append one success record. Returns a short confirmation."""
    if not task and not skills_chain:
        return "Nothing to record (empty task and skills_chain)."
    p = _path(memory_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "task": (task or "")[:200],
        "tools_used": int(tools_used or 0),
        "duration_s": int(duration_s or 0),
        "skills_chain": (skills_chain or "")[:300],
        "outcome": outcome or "success",
    }
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    # Occasionally trim so the file never grows unbounded
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        if len(lines) > _MAX_LINES + 50:
            p.write_text("\n".join(lines[-_MAX_LINES:]) + "\n", encoding="utf-8")
    except Exception:
        pass
    return f"Recorded: {entry['task'][:60]} ({entry['tools_used']} tools, {entry['duration_s']}s)"


def recent(n: int = 3, memory_dir=None) -> list:
    """Last n entries as formatted strings (oldest first). Empty list if none."""
    return [
        f"[{e.get('ts', '?')}] {e.get('task', '?')} "
        f"({e.get('tools_used', 0)} tools, {e.get('duration_s', 0)}s) "
        f"via {e.get('skills_chain', '?')}"
        for e in records(n, memory_dir)
    ]


def records(n: int = 100, memory_dir=None) -> list:
    """Last n entries as parsed dicts (oldest first). Empty list if none."""
    p = _path(memory_dir)
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines()[-max(1, int(n)):]:
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return out
