"""
Daily Briefing — one morning message that makes the agent feel alive.

Composes (deterministically, no LLM): yesterday's journal summary, overnight
self-improvement results, today's scheduled queue, recent successes the agent
learned from, and 1-3 concrete nudges from parked ideas.

The scheduled task calls morning() and sends the result via telegram_bot.send().
Missed while the PC was off → the scheduler fires it once on next boot (< 2
intervals overdue) or skips stale ones (>= 2 intervals) — never breaks.
"""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

NAME = "daily_briefing"
SHORT_DOC = "Compose the morning briefing: yesterday, overnight learning, today's queue, nudges."
DOC = (
    "Daily briefing. Functions: morning() -> formatted briefing text. "
    "The scheduled task sends it via telegram_bot.send()."
)
__all__ = ["NAME", "SHORT_DOC", "DOC", "morning"]


def _tail(path: Path, n: int = 1) -> list:
    try:
        return path.read_text(encoding="utf-8").splitlines()[-n:]
    except Exception:
        return []


def _yesterday_summary(mem: Path) -> str:
    lines = _tail(mem / "daily_journal.jsonl", 1)
    for line in lines:
        try:
            e = json.loads(line)
            day = str(e.get("day", e.get("date", "")))[:10]
            summary = str(e.get("summary", ""))[:220]
            if summary:
                return f"• Yesterday ({day}): {summary}"
        except Exception:
            continue
    return "• Yesterday: no journal entry found."


def _overnight_learning(mem: Path) -> str:
    out = []
    for line in _tail(mem / "improvement_log.jsonl", 3):
        try:
            e = json.loads(line)
            if e.get("loop") == "run_all":
                continue
            loop = e.get("loop", "?")
            summary = str(e.get("summary", ""))[:160].replace("\n", " ")
            out.append(f"• Overnight [{loop}]: {summary}")
        except Exception:
            continue
    # lesson count last 24h
    cutoff = (datetime.now() - timedelta(hours=24)).isoformat()
    n = 0
    for line in _tail(mem / "lessons.jsonl", 50):
        try:
            if json.loads(line).get("timestamp", "") >= cutoff:
                n += 1
        except Exception:
            continue
    out.append(f"• Lessons learned (24h): {n}")
    return "\n".join(out) if out else "• Overnight: nothing new."


def _today_queue(mem: Path) -> str:
    try:
        tasks = json.loads((mem / "scheduled_tasks.json").read_text(encoding="utf-8"))
    except Exception:
        return "• Today: schedule unavailable."
    now = datetime.now()
    due = []
    for name, t in tasks.items():
        try:
            nr = datetime.fromisoformat(str(t.get("next_run")))
            if nr <= now + timedelta(hours=24):
                due.append(f"  - {name} at {nr.strftime('%H:%M')}")
        except Exception:
            continue
    return "• Next 24h:\n" + "\n".join(due[:6]) if due else "• Next 24h: nothing scheduled."


def _recent_successes(mem: Path) -> str:
    try:
        sj_mod = __import__("success_journal")
        rec = sj_mod.recent(3, memory_dir=str(mem))
    except Exception:
        rec = []
    if not rec:
        return "• Recently learned: (no success records yet)"
    return "• Recently learned:\n" + "\n".join(f"  - {r[:140]}" for r in rec)


def _nudges(mem: Path) -> str:
    """Concrete 'noticed X — want me to Y?' items from parked ideas."""
    ideas = []
    for line in _tail(mem / "improvement_ideas.jsonl", 30):
        try:
            e = json.loads(line)
            if str(e.get("status", "open")) in ("open", "parked", ""):
                txt = str(e.get("idea", e.get("text", "")))[:130]
                if txt:
                    ideas.append(txt)
        except Exception:
            continue
    if not ideas:
        return ""
    picks = ideas[-2:]  # most recent open ideas
    return ("• Suggestions — want me to act on these? (just say yes/no)\n"
            + "\n".join(f"  {i + 1}. {t}" for i, t in enumerate(picks)))


def morning(memory_dir: str = None) -> str:
    """Compose the full briefing text."""
    base = Path(memory_dir or os.getenv("TRINITY_MEMORY_DIR", "/app/memory"))
    parts = [
        f"🌅 Good morning — {datetime.now().strftime('%a %d %b, %H:%M')}",
        _yesterday_summary(base),
        _overnight_learning(base),
        _today_queue(base),
        _recent_successes(base),
    ]
    nudges = _nudges(base)
    if nudges:
        parts.append(nudges)
    return "\n".join(p for p in parts if p)
