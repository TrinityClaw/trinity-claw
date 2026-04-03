# scheduler.py
import json
import os
import re
import threading
import time
import requests
from datetime import datetime, timedelta
from pathlib import Path

NAME = 'scheduler'
SHORT_DOC = "Schedule agent prompts to run once or on a recurring schedule using natural language times."
DOC = (
    'Schedule agent prompts to run once or recurring. Natural language: "tomorrow at 3pm", "in 2 hours", "every 1h". '
    'Functions: schedule(name, when, prompt), schedule_recurring(name, every, prompt), '
    'list_tasks()→all tasks with truncated prompt preview, '
    'get_task(name)→FULL task details including complete prompt — use this when user asks to see or edit a task, '
    'edit_task_prompt(name, new_prompt)→replace a task\'s prompt without changing its schedule, '
    'remove(name), clear(), status(), parse_preview(when), get_activity_log(hours=24), '
    'get_task_report(name, limit=50)→full result history for one task. '
    'IMPORTANT: list_tasks() truncates prompts. Always use get_task(name) when the user wants to read or edit a specific task.'
)

_TASKS_FILE    = Path("/app/memory/scheduled_tasks.json")
_ACTIVITY_LOG  = Path("/app/memory/activity_log.jsonl")
_lock = threading.Lock()
_running = False
_thread = None


def _append_activity(source: str, action: str, result: str):
    """Append one line to the shared activity log (best-effort, never raises)."""
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
    except Exception:
        pass


# ── Persistence ───────────────────────────────────────────────────────────────

def _load() -> dict:
    _TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _TASKS_FILE.exists():
        try:
            return json.loads(_TASKS_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save(tasks: dict):
    _TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TASKS_FILE.write_text(json.dumps(tasks, indent=2, default=str))


# ── Time Parsing ──────────────────────────────────────────────────────────────

def _parse_when(when_str: str) -> datetime:
    """
    Parse natural language or ISO datetime to a datetime object.

    Supported formats:
      - "in 2 hours", "in 30 minutes", "in 1 day"
      - "tomorrow", "tomorrow at 3pm", "tomorrow at 14:30"
      - "today at 5pm"
      - "at 3pm", "at 14:30"  (today if future, else tomorrow)
      - "next monday", "next friday at 9am"
      - ISO: "2026-03-01 10:00", "2026-03-01T10:00:00"
    """
    s = when_str.strip().lower()
    now = datetime.now()

    # "in X seconds/minutes/hours/days"
    m = re.match(
        r'in\s+(\d+)\s*'
        r'(s|sec|secs|second|seconds|'
        r'm|min|mins|minute|minutes|'
        r'h|hr|hrs|hour|hours|'
        r'd|day|days)',
        s
    )
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit.startswith('s'):   return now + timedelta(seconds=n)
        if unit.startswith('m'):   return now + timedelta(minutes=n)
        if unit.startswith('h'):   return now + timedelta(hours=n)
        if unit.startswith('d'):   return now + timedelta(days=n)

    # Extract optional "at H:MM [am/pm]" component
    tp = re.search(r'at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', s)
    t_hour = t_min = None
    if tp:
        t_hour = int(tp.group(1))
        t_min  = int(tp.group(2) or 0)
        ampm   = tp.group(3)
        if ampm == 'pm' and t_hour != 12:
            t_hour += 12
        elif ampm == 'am' and t_hour == 12:
            t_hour = 0

    # Determine base date
    base = None
    if 'tomorrow' in s:
        base = (now + timedelta(days=1)).date()
    elif 'today' in s:
        base = now.date()
    else:
        weekdays = {
            'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
            'friday': 4, 'saturday': 5, 'sunday': 6,
        }
        for day_name, day_num in weekdays.items():
            if day_name in s:
                diff = (day_num - now.weekday()) % 7 or 7  # always future
                base = (now + timedelta(days=diff)).date()
                break

    if base is not None:
        hour   = t_hour if t_hour is not None else 9   # default 09:00
        minute = t_min  if t_min  is not None else 0
        return datetime.combine(base, datetime.min.time()).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )

    # "at H:MM" alone → today if still in the future, otherwise tomorrow
    if t_hour is not None:
        candidate = now.replace(hour=t_hour, minute=t_min, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    # ISO / structured date string fallback (uses python-dateutil if available)
    try:
        from dateutil import parser as dparser
        return dparser.parse(when_str, default=now)
    except Exception:
        pass

    raise ValueError(
        f"Cannot parse time expression: '{when_str}'. "
        "Try: 'tomorrow at 3pm', 'in 2 hours', 'at 14:30', "
        "'next monday at 9am', '2026-03-01 10:00'"
    )


def _parse_interval(interval_str: str) -> int:
    """
    Parse an interval string to seconds.
    Examples: '30m', '2h', '1d', 'every 6 hours', '90 minutes', '45s'
    Also handles ranges like '3-4h' or '2-3 hours' by taking the lower bound.
    """
    s = re.sub(r'^every\s+', '', interval_str.strip().lower())
    # Handle range notation like '3-4h' or '2-3 hours' — take the lower bound
    range_m = re.match(
        r'(\d+)-\d+\s*'
        r'(s|sec|secs|second|seconds|'
        r'm|min|mins|minute|minutes|'
        r'h|hr|hrs|hour|hours|'
        r'd|day|days)',
        s
    )
    if range_m:
        s = range_m.group(1) + range_m.group(2)  # collapse to lower bound
    m = re.match(
        r'(\d+)\s*'
        r'(s|sec|secs|second|seconds|'
        r'm|min|mins|minute|minutes|'
        r'h|hr|hrs|hour|hours|'
        r'd|day|days)',
        s
    )
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit.startswith('s'):  return n
        if unit.startswith('m'):  return n * 60
        if unit.startswith('h'):  return n * 3600
        if unit.startswith('d'):  return n * 86400
    raise ValueError(
        f"Cannot parse interval: '{interval_str}'. "
        "Try: '30m', '2h', '1d', 'every 6 hours', '90 minutes'"
    )


def _human_interval(seconds: int) -> str:
    if seconds < 60:    return f"{seconds}s"
    if seconds < 3600:  return f"{seconds // 60}m"
    if seconds < 86400: return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _eta(next_run: datetime) -> str:
    diff = next_run - datetime.now()
    secs = int(diff.total_seconds())
    if secs <= 0:
        return "overdue"
    h, rem = divmod(secs, 3600)
    m = rem // 60
    if h > 0:
        return f"in {h}h {m}m"
    return f"in {m}m"


# ── Agent Dispatch ────────────────────────────────────────────────────────────

def _dispatch(prompt: str, task_name: str) -> str:
    """POST the stored prompt to the agent's /chat endpoint."""
    api_key = os.getenv("TRINITY_API_KEY", "")
    headers = {"X-API-Key": api_key} if api_key else {}
    # Prepend a hard execution directive so scheduled tasks never trigger the
    # Design-First Rule (SO #24) or go into planning mode — they must act immediately.
    execution_prompt = (
        "[SCHEDULED TASK — EXECUTE IMMEDIATELY]\n"
        "Do NOT call autoimprove.design. Do NOT plan or describe. "
        "Output skill tags immediately and execute to completion.\n\n"
        + prompt
    )
    try:
        resp = requests.post(
            "http://localhost:8001/chat",
            json={"message": execution_prompt, "session_id": f"sched_{task_name}"},
            headers=headers,
            timeout=1800,
        )
        if resp.ok:
            data = resp.json()
            return data.get("response", str(data))[:300]
        return f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as e:
        return f"dispatch error: {e}"


# ── Background Loop ───────────────────────────────────────────────────────────

def _run():
    global _running
    while _running:
        try:
            now = datetime.now()
            with _lock:
                tasks = _load()
                changed = False
                to_delete = []

                for name, t in tasks.items():
                    next_run = datetime.fromisoformat(t['next_run'])
                    if now >= next_run:
                        print(f"[scheduler] firing: {name}")
                        result = _dispatch(t['prompt'], name)
                        print(f"[scheduler] {name} → {result[:80]}")
                        t['last_run']    = now.isoformat()
                        t['last_result'] = result[:200]
                        t['run_count']   = t.get('run_count', 0) + 1
                        _append_activity(f"scheduler:{name}", t['prompt'][:80], result)
                        changed = True
                        if t['type'] == 'once':
                            to_delete.append(name)
                        else:
                            t['next_run'] = (
                                now + timedelta(seconds=t['interval_seconds'])
                            ).isoformat()

                for name in to_delete:
                    del tasks[name]

                if changed:
                    _save(tasks)
        except Exception as e:
            print(f"[scheduler] loop error: {e}")

        time.sleep(30)  # check every 30 seconds


def _ensure_running():
    global _running, _thread
    if not _running:
        _running = True
        _thread = threading.Thread(target=_run, daemon=True, name="scheduler")
        _thread.start()


# Auto-start when skill is imported
_ensure_running()


# ── Public API ────────────────────────────────────────────────────────────────

def schedule(name: str, when: str, prompt: str) -> str:
    """
    Schedule a prompt to run ONCE at a specific time.

    name   — unique identifier for this task
    when   — when to run: 'tomorrow at 3pm', 'in 2 hours', 'at 14:30',
             'next friday at 9am', '2026-03-01 10:00'
    prompt — what the agent should do at that time
    """
    try:
        run_at = _parse_when(when)
    except ValueError as e:
        return f"❌ {e}"

    if run_at <= datetime.now():
        return f"❌ Parsed time {run_at.strftime('%Y-%m-%d %H:%M')} is already in the past."

    task = {
        "type":             "once",
        "prompt":           prompt,
        "next_run":         run_at.isoformat(),
        "interval_seconds": None,
        "created":          datetime.now().isoformat(),
        "last_run":         None,
        "run_count":        0,
    }
    with _lock:
        tasks = _load()
        tasks[name] = task
        _save(tasks)
    _ensure_running()
    return (
        f"✅ Scheduled '{name}' to run once at "
        f"{run_at.strftime('%Y-%m-%d %H:%M')} ({_eta(run_at)})"
    )


def schedule_recurring(name: str, every: str, prompt: str) -> str:
    """
    Schedule a prompt to run REPEATEDLY on an interval.

    name   — unique identifier for this task
    every  — how often: '30m', '2h', '1d', 'every 6 hours'
    prompt — what the agent should do each time
    """
    try:
        secs = _parse_interval(every)
    except ValueError as e:
        return f"❌ {e}"

    next_run = datetime.now() + timedelta(seconds=secs)
    task = {
        "type":             "recurring",
        "prompt":           prompt,
        "next_run":         next_run.isoformat(),
        "interval_seconds": secs,
        "created":          datetime.now().isoformat(),
        "last_run":         None,
        "run_count":        0,
    }
    with _lock:
        tasks = _load()
        tasks[name] = task
        _save(tasks)
    _ensure_running()
    return (
        f"✅ Scheduled '{name}' to run every {_human_interval(secs)}, "
        f"first at {next_run.strftime('%H:%M')}"
    )


def remove(name: str) -> str:
    """Remove a scheduled task by name."""
    with _lock:
        tasks = _load()
        if name in tasks:
            del tasks[name]
            _save(tasks)
            return f"✅ Removed task '{name}'"
        return f"❌ Task '{name}' not found. Use list_tasks to see what's scheduled."


def list_tasks() -> str:
    """List all scheduled tasks with next run time and prompt preview."""
    tasks = _load()
    if not tasks:
        return "📭 No scheduled tasks"

    lines = [f"📅 Scheduled tasks ({len(tasks)}):"]
    for name, t in tasks.items():
        next_run = datetime.fromisoformat(t['next_run'])
        kind     = "🔁 recurring" if t['type'] == 'recurring' else "1️⃣  once"
        ivl      = f" every {_human_interval(t['interval_seconds'])}" if t['type'] == 'recurring' else ""
        last_result = t.get('last_result', '')
        last_ok     = "✅" if last_result and not last_result.startswith("❌") else ("❌" if last_result else "—")
        last_run_str = f"  last run: {t['last_run']} {last_ok} {last_result[:80]}\n" if last_result else ""
        lines.append(
            f"\n  {kind}{ivl} | {name}\n"
            f"  next: {next_run.strftime('%Y-%m-%d %H:%M')} ({_eta(next_run)}) | "
            f"runs so far: {t.get('run_count', 0)}\n"
            f"{last_run_str}"
            f"  prompt: \"{t['prompt'][:80]}{'...' if len(t['prompt']) > 80 else ''}\""
        )
    return "\n".join(lines)


def get_task(name: str) -> str:
    """Get the FULL details of a scheduled task by name, including its complete prompt.
    Use this when the user wants to see or edit a task's prompt — list_tasks() truncates it."""
    tasks = _load()
    if name not in tasks:
        return f"❌ Task '{name}' not found. Use list_tasks() to see available tasks."
    t = tasks[name]
    next_run = datetime.fromisoformat(t['next_run'])
    kind = "recurring" if t['type'] == 'recurring' else "once"
    ivl  = f" every {_human_interval(t['interval_seconds'])}" if t['type'] == 'recurring' else ""
    last_result = t.get('last_result', 'never run yet')
    lines = [
        f"📋 Task: {name}",
        f"  Type:      {kind}{ivl}",
        f"  Next run:  {next_run.strftime('%Y-%m-%d %H:%M')} ({_eta(next_run)})",
        f"  Run count: {t.get('run_count', 0)}",
        f"  Last run:  {t.get('last_run', 'never')}",
        f"  Last result: {last_result}",
        f"  Created:   {t.get('created', 'unknown')}",
        f"",
        f"  Full prompt:",
        f"  {t['prompt']}",
    ]
    return "\n".join(lines)


def edit_task_prompt(name: str, new_prompt: str) -> str:
    """Replace the prompt of an existing scheduled task without changing its schedule.
    Use this when the user wants to edit what a task does."""
    with _lock:
        tasks = _load()
        if name not in tasks:
            return f"❌ Task '{name}' not found. Use list_tasks() to see available tasks."
        old_prompt = tasks[name]['prompt']
        tasks[name]['prompt'] = new_prompt
        _save(tasks)
    return (
        f"✅ Prompt updated for task '{name}'.\n"
        f"  Old: {old_prompt[:100]}{'...' if len(old_prompt) > 100 else ''}\n"
        f"  New: {new_prompt[:100]}{'...' if len(new_prompt) > 100 else ''}"
    )


def clear() -> str:
    """Cancel and remove ALL scheduled tasks."""
    with _lock:
        tasks = _load()
        count = len(tasks)
        _save({})
    return f"✅ Cleared {count} task(s)"


def status() -> str:
    """Show scheduler health: running state, task count, check interval."""
    tasks = _load()
    thread_alive = _thread is not None and _thread.is_alive()
    return (
        f"Scheduler: {'running ✅' if thread_alive else 'stopped ❌'} | "
        f"Tasks: {len(tasks)} | "
        f"Check interval: 30s | "
        f"Storage: {_TASKS_FILE}"
    )


def get_activity_log(hours: int = 24) -> str:
    """Show what the agent did in the last N hours (scheduled + manual tasks).
    Reads from /app/memory/activity_log.jsonl. Default: last 24 hours."""
    try:
        if not _ACTIVITY_LOG.exists():
            return "📭 No activity logged yet."
        cutoff = datetime.now() - timedelta(hours=int(hours))
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
            lines.append(f"  {e['ts']}  {icon}  [{e['source']}]  {e['action'][:60]}")
            if e.get("result"):
                lines.append(f"       ↳ {e['result'][:200]}")
        return "\n".join(lines)
    except Exception as ex:
        return f"❌ get_activity_log error: {ex}"


def get_task_report(name: str, limit: int = 50) -> str:
    """Show the full result history for a specific task by name.
    Scans the activity log for all entries matching this task. limit=50 by default."""
    try:
        if not _ACTIVITY_LOG.exists():
            return "📭 No activity logged yet."
        prefix = f"scheduler:{name}"
        entries = []
        for line in _ACTIVITY_LOG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                if e.get("source") == prefix:
                    entries.append(e)
            except Exception:
                continue
        entries = entries[-int(limit):]
        if not entries:
            return f"📭 No activity found for task '{name}'."
        ok_count   = sum(1 for e in entries if e.get("ok"))
        fail_count = len(entries) - ok_count
        lines = [
            f"📊 Report for task '{name}' ({len(entries)} runs shown, "
            f"✅ {ok_count} ok  ❌ {fail_count} failed):"
        ]
        for e in entries:
            icon = "✅" if e.get("ok") else "❌"
            lines.append(f"\n  {e['ts']}  {icon}")
            lines.append(f"  prompt:  {e['action']}")
            lines.append(f"  result:  {e['result']}")
        return "\n".join(lines)
    except Exception as ex:
        return f"❌ get_task_report error: {ex}"


def parse_preview(when: str) -> str:
    """
    Preview what a time expression resolves to without scheduling anything.
    Useful to verify before committing: parse_preview('next monday at 9am')
    """
    try:
        dt = _parse_when(when)
        now = datetime.now()
        if dt <= now:
            return f"⚠️  '{when}' → {dt.strftime('%Y-%m-%d %H:%M')} (already in the past!)"
        return f"🕐 '{when}' → {dt.strftime('%Y-%m-%d %H:%M')} ({_eta(dt)})"
    except ValueError as e:
        return f"❌ {e}"
