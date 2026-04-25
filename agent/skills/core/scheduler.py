# scheduler.py
import concurrent.futures
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
    'USAGE: schedule(name:str, when:str, prompt:str) — Schedule a one-time task. '
    'EXAMPLE: schedule("backup", "in 2 hours", "Run database backup and notify admin")\n\n'
    'Schedule agent prompts to run once or recurring. Natural language: "tomorrow at 3pm", "in 2 hours", "every 1h". '
    'Functions: schedule(name, when, prompt), schedule_recurring(name, every, prompt), '
    'list_tasks()→all tasks with truncated prompt preview, '
    'get_task(name)→FULL task details including complete prompt — use this when user asks to see or edit a task, '
    'edit_task_prompt(name, new_prompt)→replace a task\'s prompt without changing its schedule, '
    'edit_task_when(name, new_when)→change WHEN a task runs (once: new time expression; recurring: new interval or calendar spec), '
    'run_now(name)→immediately fire a task (bypasses schedule; once tasks are NOT deleted), '
    'remove(name), clear(), status(), stop()→halt the scheduler thread, '
    'parse_preview(when)→preview a time/interval expression without scheduling (works for both one-time and recurring specs), '
    'help_schedule()→full usage docs, '
    'get_activity_log(hours=24), '
    'get_task_report(name, limit=50)→full result history for one task. '
    'IMPORTANT: list_tasks() truncates prompts. Always use get_task(name) when the user wants to read or edit a specific task. '
    'EDITING: to change what a task does use edit_task_prompt(name, new_prompt); '
    'to change when it runs use edit_task_when(name, new_when). '
    'Do NOT call schedule() or schedule_recurring() on an existing task name — use the edit_* functions instead.'
)

__all__ = [
    "schedule", "schedule_recurring", "remove", "list_tasks",
    "get_task", "edit_task_prompt", "edit_task_when", "run_now", "clear", "status", "stop",
    "get_activity_log", "get_task_report", "parse_preview", "help_schedule",
]

_TASKS_FILE    = Path("/app/memory/scheduled_tasks.json")
_ACTIVITY_LOG  = Path("/app/memory/activity_log.jsonl")
_lock = threading.Lock()
_start_lock = threading.Lock()   # guards _ensure_running check+set atomicity
_firing_tasks: set = set()       # names of tasks currently being dispatched
_running = False
_thread = None

_MAX_DISPATCH_WORKERS = 10       # cap concurrent task dispatches

_LOG_MAX_BYTES  = 5 * 1024 * 1024   # rotate when file exceeds 5 MB
_LOG_KEEP_LINES = 8_000              # keep this many lines after rotation


def _rotate_activity_log():
    """Trim the activity log to the last _LOG_KEEP_LINES lines; backs up the evicted lines first."""
    try:
        lines = _ACTIVITY_LOG.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(lines) > _LOG_KEEP_LINES:
            backup = _ACTIVITY_LOG.with_suffix(".bak")
            backup.write_text("".join(lines[:-_LOG_KEEP_LINES]), encoding="utf-8")
            _ACTIVITY_LOG.write_text("".join(lines[-_LOG_KEEP_LINES:]), encoding="utf-8")
    except Exception:
        pass


def _append_activity(source: str, action: str, result: str):
    """Append one line to the shared activity log (best-effort, never raises)."""
    try:
        _ACTIVITY_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = json.dumps({
            "ts":     datetime.now().isoformat(timespec="seconds"),
            "source": source,
            "action": action[:500],
            "result": result[:800],
            "ok":     not result.startswith("❌"),
        })
        with _ACTIVITY_LOG.open("a", encoding="utf-8") as f:
            f.write(entry + "\n")
        if _ACTIVITY_LOG.stat().st_size > _LOG_MAX_BYTES:
            _rotate_activity_log()
    except Exception:
        pass


# ── Persistence ───────────────────────────────────────────────────────────────

def _load() -> dict:
    """Load the scheduled tasks dict from disk. Returns an empty dict if missing or corrupt."""
    _TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _TASKS_FILE.exists():
        try:
            return json.loads(_TASKS_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save(tasks: dict):
    """Persist the scheduled tasks dict to disk (atomic write via temp+replace)."""
    _TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = _TASKS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(tasks, indent=2))
    tmp.replace(_TASKS_FILE)


# ── Time Parsing ──────────────────────────────────────────────────────────────

_UNIT_PATTERN = (
    r'(s|sec|secs|second|seconds|'
    r'm|min|mins|minute|minutes|'
    r'h|hr|hrs|hour|hours|'
    r'd|day|days|'
    r'w|wk|wks|week|weeks)'
)


def _unit_to_seconds(n: float, unit: str) -> int:
    if unit.startswith('s'):  return int(n)
    if unit.startswith('m'):  return int(n * 60)
    if unit.startswith('h'):  return int(n * 3600)
    if unit.startswith('d'):  return int(n * 86400)
    if unit.startswith('w'):  return int(n * 604800)
    raise ValueError(f"Unknown unit: {unit}")


def _parse_when(when_str: str) -> datetime:
    """
    Parse natural language or ISO datetime to a datetime object.

    Supported formats:
      - "in 2 hours", "in 30 minutes", "in 1 day", "in 2 weeks"
      - "tomorrow", "tomorrow at 3pm", "tomorrow at 14:30"
      - "today at 5pm"
      - "at 3pm", "at 14:30"  (today if future, else tomorrow)
      - "next monday", "next friday at 9am"
      - ISO: "2026-03-01 10:00", "2026-03-01T10:00:00"
    """
    s = when_str.strip().lower()
    now = datetime.now()

    # "in X seconds/minutes/hours/days/weeks"
    m = re.match(r'in\s+(\d+(?:\.\d+)?)\s*' + _UNIT_PATTERN, s)
    if m:
        return now + timedelta(seconds=_unit_to_seconds(float(m.group(1)), m.group(2)))

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

    # ISO / structured date string fallback — requires python-dateutil.
    try:
        from dateutil import parser as dparser
    except ImportError:
        dparser = None  # optional dependency; skip fallback
    if dparser is not None:
        try:
            return dparser.parse(when_str, default=now).replace(tzinfo=None)
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
    Examples: '30m', '2h', '1.5h', '1d', '2w', 'every 6 hours', '90 minutes', '45s'
    Also handles ranges like '3-4h' or '2-3 hours' by taking the lower bound.
    """
    s = re.sub(r'^every\s+', '', interval_str.strip().lower())
    # Handle range notation like '3-4h' or '2-3 hours' — take the lower bound
    range_m = re.match(r'(\d+(?:\.\d+)?)-\d+(?:\.\d+)?\s*' + _UNIT_PATTERN, s)
    if range_m:
        s = range_m.group(1) + range_m.group(2)  # collapse to lower bound
    m = re.match(r'(\d+(?:\.\d+)?)\s*' + _UNIT_PATTERN, s)
    if m:
        return _unit_to_seconds(float(m.group(1)), m.group(2))
    raise ValueError(
        f"Cannot parse interval: '{interval_str}'. "
        "Try: '30m', '2h', '1d', '2w', 'every 6 hours', '90 minutes'"
    )


def _human_interval(seconds: int) -> str:
    """Convert a seconds count to a compact human-readable string (e.g. '2h', '30m')."""
    if seconds < 60:       return f"{seconds}s"
    if seconds < 3600:     return f"{seconds // 60}m"
    if seconds < 86400:    return f"{seconds // 3600}h"
    if seconds < 604800:   return f"{seconds // 86400}d"
    return f"{seconds // 604800}w"


def _eta(next_run: datetime) -> str:
    """Return a human-readable ETA string for a future datetime (e.g. 'in 2h 15m')."""
    if next_run.tzinfo:
        now = datetime.now().astimezone(next_run.tzinfo)
    else:
        now = datetime.now()
    diff = next_run - now
    secs = int(diff.total_seconds())
    if secs <= 0:
        return "overdue"
    h, rem = divmod(secs, 3600)
    m = rem // 60
    if h > 0:
        return f"in {h}h {m}m"
    return f"in {m}m"


# ── Calendar-Aligned Recurrence ───────────────────────────────────────────────

_WEEKDAY_MAP = {
    'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
    'friday': 4, 'saturday': 5, 'sunday': 6,
}
_WEEKDAY_GROUPS = {
    'weekday':  [0, 1, 2, 3, 4],
    'weekdays': [0, 1, 2, 3, 4],
    'weekend':  [5, 6],
    'weekends': [5, 6],
}


def _next_calendar_run(weekdays, hour: int, minute: int, after: datetime = None) -> datetime:
    """Return the next datetime matching (weekdays, hour, minute), strictly after `after`."""
    base = after if after is not None else datetime.now()
    if weekdays is not None and len(weekdays) == 0:
        weekdays = None  # treat empty list as "any day"
    for delta in range(8):  # max 7 days covers any weekday combination
        candidate = (base + timedelta(days=delta)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        if candidate <= base:
            continue
        if weekdays is None or candidate.weekday() in weekdays:
            return candidate
    raise ValueError(
        f"Could not compute next run for calendar spec weekdays={weekdays} at {hour:02d}:{minute:02d}"
    )


def _parse_recurring_spec(every_str: str) -> dict:
    """
    Parse a recurrence spec. Returns either:
      {"kind": "interval", "seconds": N}
    or:
      {"kind": "calendar", "weekdays": list|None, "hour": H, "minute": M, "label": str}

    Calendar patterns recognised:
      "every day at 8pm", "every daily at 9am"
      "every monday at 9am", "every friday"
      "every weekday at 8am", "every weekend at 10am"
    """
    s = every_str.strip().lower()
    base = re.sub(r'^every\s+', '', s)

    # Extract optional "at H:MM [am/pm]"
    tp = re.search(r'at\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?', base)
    if tp:
        t_hour = int(tp.group(1))
        t_min  = int(tp.group(2) or 0)
        ampm   = tp.group(3)
        if ampm == 'pm' and t_hour != 12:
            t_hour += 12
        elif ampm == 'am' and t_hour == 12:
            t_hour = 0
        day_part = base[:tp.start()].strip()
    else:
        t_hour = t_min = None
        day_part = base.strip()

    # Match day tokens
    weekdays = None
    day_label = None
    if day_part in ('day', 'days', 'daily'):
        weekdays  = None   # every day
        day_label = 'day'
    elif day_part in _WEEKDAY_GROUPS:
        weekdays  = _WEEKDAY_GROUPS[day_part]
        day_label = day_part
    elif day_part in _WEEKDAY_MAP:
        weekdays  = [_WEEKDAY_MAP[day_part]]
        day_label = day_part

    if day_label is not None:
        hour   = t_hour if t_hour is not None else 9
        minute = t_min  if t_min  is not None else 0
        label  = f"every {day_label} at {hour:02d}:{minute:02d}"
        return {"kind": "calendar", "weekdays": weekdays, "hour": hour, "minute": minute, "label": label}

    # Fall back to duration interval
    secs = _parse_interval(every_str)
    return {"kind": "interval", "seconds": secs}


def _recur_label(t: dict) -> str:
    """Return a human-readable recurrence description for a task dict."""
    if t.get('recur_kind') == 'calendar':
        return t.get('cal_label', f"every {t.get('cal_hour', 9):02d}:{t.get('cal_minute', 0):02d}")
    secs = t.get('interval_seconds')
    return f"every {_human_interval(secs)}" if secs else "recurring"


# ── Agent Dispatch ────────────────────────────────────────────────────────────

_AGENT_URL        = os.getenv("TRINITY_AGENT_URL", "http://127.0.0.1:8001/chat")
_DISPATCH_RETRIES = 2
_DISPATCH_RETRY_DELAY = 5  # seconds between retries
_TRANSIENT_HTTP_CODES = {429, 502, 503, 504}


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
    last_err = ""
    for attempt in range(1 + _DISPATCH_RETRIES):
        try:
            resp = requests.post(
                _AGENT_URL,
                json={"message": execution_prompt, "session_id": f"sched_{task_name}"},
                headers=headers,
                timeout=(10, 1800),  # (connect_timeout, read_timeout)
            )
            if resp.ok:
                data = resp.json()
                return data.get("response", str(data))[:300]
            if resp.status_code in _TRANSIENT_HTTP_CODES:
                last_err = f"HTTP {resp.status_code}: {resp.text[:100]}"
                if attempt < _DISPATCH_RETRIES:
                    time.sleep(_DISPATCH_RETRY_DELAY)
                continue
            return f"HTTP {resp.status_code}: {resp.text[:200]}"
        except requests.ConnectionError as e:
            last_err = str(e)
            if attempt < _DISPATCH_RETRIES:
                time.sleep(_DISPATCH_RETRY_DELAY)
        except Exception as e:
            return f"dispatch error: {e}"
    return f"❌ dispatch failed after {1 + _DISPATCH_RETRIES} attempts: {last_err}"


# ── Background Loop ───────────────────────────────────────────────────────────

def _run():
    """Background loop: check every 30 s and fire any tasks whose next_run has passed."""
    global _running
    while _running:
        try:
            now = datetime.now()

            # Phase 1: identify tasks to fire (lock held briefly, no I/O)
            with _lock:
                tasks = _load()
                to_fire = []
                for name, t in tasks.items():
                    if name in _firing_tasks:
                        continue  # already dispatching — skip to prevent double-fire
                    try:
                        next_run_dt = datetime.fromisoformat(t['next_run'])
                        _now = datetime.now(next_run_dt.tzinfo) if next_run_dt.tzinfo else now
                        if _now >= next_run_dt:
                            to_fire.append((name, t['prompt'], t['type'],
                                            t.get('interval_seconds')))
                            _firing_tasks.add(name)
                    except (ValueError, KeyError) as e:
                        print(f"[scheduler] skipping malformed task '{name}': {e}")

            # Phase 2: dispatch concurrently outside the lock (capped thread pool)
            results = []
            if to_fire:
                def _fire_one(task_tuple):
                    name, prompt, kind, interval_secs = task_tuple
                    fired_at = datetime.now()
                    print(f"[scheduler] firing: {name}")
                    try:
                        result = _dispatch(prompt, name)
                    except Exception as e:
                        result = f"❌ dispatch exception: {e}"
                    finally:
                        with _lock:
                            _firing_tasks.discard(name)
                    print(f"[scheduler] {name} → {result[:80]}")
                    return (name, prompt, kind, interval_secs, fired_at, result)

                with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(len(to_fire), _MAX_DISPATCH_WORKERS),
                    thread_name_prefix="sched"
                ) as pool:
                    for item in pool.map(_fire_one, to_fire):
                        results.append(item)

            # Phase 3: persist results (lock held briefly)
            if results:
                with _lock:
                    tasks = _load()
                    for name, prompt, kind, interval_secs, fired_at, result in results:
                        if name not in tasks:
                            continue  # task was removed while we were dispatching
                        t = tasks[name]
                        t['last_run']    = fired_at.isoformat()
                        t['last_result'] = result[:300]
                        t['run_count']   = t.get('run_count', 0) + 1
                        _append_activity(f"scheduler:{name}", prompt, result)
                        if kind == 'once':
                            del tasks[name]
                        elif t.get('recur_kind') == 'calendar':
                            # Advance to the next calendar-aligned occurrence
                            try:
                                t['next_run'] = _next_calendar_run(
                                    t.get('cal_weekdays'), t['cal_hour'], t['cal_minute'],
                                    after=datetime.now()
                                ).isoformat()
                            except Exception as e:
                                print(f"[scheduler] calendar advance error for '{name}': {e}")
                        else:
                            # Advance from the stored next_run (not dispatch time)
                            # to preserve schedule alignment (e.g. always at :00).
                            # Cap to now so next_run is never in the past if
                            # dispatch exceeded the interval.
                            # Use interval_seconds from the fresh disk load so that
                            # edit_task_when() changes made during dispatch take effect.
                            fresh_secs = t.get('interval_seconds') or interval_secs
                            prev_next = datetime.fromisoformat(t['next_run'])
                            ideal = prev_next + timedelta(seconds=fresh_secs)
                            now_cmp = datetime.now(ideal.tzinfo) if ideal.tzinfo else datetime.now()
                            t['next_run'] = max(ideal, now_cmp).isoformat()
                    _save(tasks)
        except Exception as e:
            print(f"[scheduler] loop error: {e}")

        time.sleep(30)  # check every 30 seconds


def _ensure_running():
    """Start the background scheduler thread if it is not already running."""
    global _running, _thread
    with _start_lock:
        if not _running:
            _running = True
            _thread = threading.Thread(target=_run, daemon=True, name="scheduler")
            _thread.start()


# Auto-start when skill is imported
_ensure_running()


def stop():
    """Stop the background scheduler thread (for clean shutdown or tests)."""
    global _running, _thread
    _running = False
    if _thread is not None:
        _thread.join(timeout=35)  # wait up to one loop tick + buffer
        _thread = None


# ── Public API ────────────────────────────────────────────────────────────────

def schedule(name: str, when: str = None, prompt: str = None, *, every: str = None) -> str:
    """
    Schedule a prompt to run ONCE at a specific time.

    name   — unique identifier for this task
    when   — when to run: 'tomorrow at 3pm', 'in 2 hours', 'at 14:30',
             'next friday at 9am', '2026-03-01 10:00'
    prompt — what the agent should do at that time
    """
    name = name.strip() if name else name
    # Accept 'every' as alias for 'when' (LLM sometimes passes every= to schedule() by mistake)
    if when is None and every is not None:
        when = every
    if not all([name, when, prompt]):
        missing = [arg for arg, val in [('name', name), ('when', when), ('prompt', prompt)] if not val]
        return f"❌ scheduler.schedule missing required argument(s): {', '.join(missing)}. Usage: schedule(name, when, prompt)"

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
        if name in tasks:
            return _duplicate_task_error(name, tasks[name])
        tasks[name] = task
        _save(tasks)
    _ensure_running()
    return (
        f"✅ Scheduled '{name}' to run once at "
        f"{run_at.strftime('%Y-%m-%d %H:%M')} ({_eta(run_at)})"
    )


def _duplicate_task_error(name: str, existing: dict) -> str:
    """Return a helpful error when a task name already exists."""
    try:
        next_str = datetime.fromisoformat(existing['next_run']).strftime('%Y-%m-%d %H:%M')
    except (ValueError, KeyError):
        next_str = "unknown"
    return (
        f"⚠️  Task '{name}' already exists (next run: {next_str}, type: {existing['type']}). "
        f"To change what it does: edit_task_prompt('{name}', new_prompt). "
        f"To change when it runs: edit_task_when('{name}', new_when). "
        f"To replace it entirely: remove('{name}') then schedule a new one."
    )


def schedule_recurring(name: str, every: str = None, prompt: str = None, *, when: str = None) -> str:
    """
    Schedule a prompt to run REPEATEDLY.

    name   — unique identifier for this task
    every  — how often: '30m', '2h', '1d', '2w', 'every day at 8pm',
             'every monday at 9am', 'every weekday at 8am'
    prompt — what the agent should do each time
    """
    name = name.strip() if name else name
    # Accept 'when' as alias for 'every' (LLM sometimes confuses schedule() and schedule_recurring())
    if every is None and when is not None:
        every = when
    missing = [arg for arg, val in [('every', every), ('prompt', prompt)] if not val]
    if missing:
        return (
            f"❌ scheduler.schedule_recurring missing required argument(s): {', '.join(missing)}. "
            f"Usage: schedule_recurring(name, every, prompt) — e.g. schedule_recurring('check_prices', '1h', 'Check DDR5 prices')"
        )

    try:
        spec = _parse_recurring_spec(every)
    except ValueError as e:
        return f"❌ {e}"

    if spec['kind'] == 'calendar':
        try:
            next_run = _next_calendar_run(spec['weekdays'], spec['hour'], spec['minute'])
        except ValueError as e:
            return f"❌ {e}"
        task = {
            "type":             "recurring",
            "recur_kind":       "calendar",
            "prompt":           prompt,
            "next_run":         next_run.isoformat(),
            "interval_seconds": None,
            "cal_weekdays":     spec['weekdays'],
            "cal_hour":         spec['hour'],
            "cal_minute":       spec['minute'],
            "cal_label":        spec['label'],
            "created":          datetime.now().isoformat(),
            "last_run":         None,
            "run_count":        0,
        }
        display = spec['label']
        next_str = next_run.strftime('%Y-%m-%d %H:%M')
    else:
        secs     = spec['seconds']
        next_run = datetime.now() + timedelta(seconds=secs)
        task = {
            "type":             "recurring",
            "recur_kind":       "interval",
            "prompt":           prompt,
            "next_run":         next_run.isoformat(),
            "interval_seconds": secs,
            "created":          datetime.now().isoformat(),
            "last_run":         None,
            "run_count":        0,
        }
        display  = f"every {_human_interval(secs)}"
        next_str = next_run.strftime('%H:%M')

    with _lock:
        tasks = _load()
        if name in tasks:
            return _duplicate_task_error(name, tasks[name])
        tasks[name] = task
        _save(tasks)
    _ensure_running()
    return f"✅ Scheduled '{name}' to run {display}, first at {next_str}"


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
    with _lock:
        tasks = _load()
    if not tasks:
        return "📭 No scheduled tasks"

    lines = [f"📅 Scheduled tasks ({len(tasks)}):"]
    for name, t in tasks.items():
        try:
            next_run = datetime.fromisoformat(t['next_run'])
            next_str = f"{next_run.strftime('%Y-%m-%d %H:%M')} ({_eta(next_run)})"
        except (ValueError, KeyError):
            next_str = "⚠️ invalid next_run"
            next_run = None
        kind        = "🔁 recurring" if t['type'] == 'recurring' else "1️⃣  once"
        ivl         = f" {_recur_label(t)}" if t['type'] == 'recurring' else ""
        last_result = t.get('last_result', '')
        last_ok     = "✅" if last_result and not last_result.startswith("❌") else ("❌" if last_result else "—")
        last_run_str = f"  last run: {t['last_run']} {last_ok} {last_result[:80]}\n" if last_result else ""
        lines.append(
            f"\n  {kind}{ivl} | {name}\n"
            f"  next: {next_str} | "
            f"runs so far: {t.get('run_count', 0)}\n"
            f"{last_run_str}"
            f"  prompt: \"{t['prompt'][:80]}{'...' if len(t['prompt']) >= 80 else ''}\""
        )
    return "\n".join(lines)


def get_task(name: str) -> str:
    """Get the FULL details of a scheduled task by name, including its complete prompt.
    Use this when the user wants to see or edit a task's prompt — list_tasks() truncates it."""
    with _lock:
        tasks = _load()
    if name not in tasks:
        return f"❌ Task '{name}' not found. Use list_tasks() to see available tasks."
    t = tasks[name]
    try:
        next_run = datetime.fromisoformat(t['next_run'])
        next_str = f"{next_run.strftime('%Y-%m-%d %H:%M')} ({_eta(next_run)})"
    except (ValueError, KeyError):
        next_str = "⚠️ invalid next_run — use edit_task_when() to fix"
    kind        = "recurring" if t['type'] == 'recurring' else "once"
    ivl         = f" {_recur_label(t)}" if t['type'] == 'recurring' else ""
    last_result = t.get('last_result', 'never run yet')
    lines = [
        f"📋 Task: {name}",
        f"  Type:      {kind}{ivl}",
        f"  Next run:  {next_str}",
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


def edit_task_when(name: str, new_when: str) -> str:
    """Change WHEN an existing task runs, without touching its prompt.

    For once tasks    — new_when is a time expression: 'tomorrow at 9am', 'in 2 hours'.
    For recurring tasks — new_when is a new interval or calendar spec: '2h', 'every day at 8pm'.
    """
    with _lock:
        tasks = _load()
        if name not in tasks:
            return f"❌ Task '{name}' not found. Use list_tasks() to see available tasks."
        t = tasks[name]
        try:
            old_next = datetime.fromisoformat(t['next_run']).strftime('%Y-%m-%d %H:%M')
        except (ValueError, KeyError):
            old_next = "unknown"

        if t['type'] == 'once':
            try:
                run_at = _parse_when(new_when)
            except ValueError as e:
                return f"❌ {e}"
            if run_at <= datetime.now():
                return f"❌ Parsed time {run_at.strftime('%Y-%m-%d %H:%M')} is already in the past."
            t['next_run'] = run_at.isoformat()
            _save(tasks)
            return (
                f"✅ Rescheduled '{name}' to run once at "
                f"{run_at.strftime('%Y-%m-%d %H:%M')} ({_eta(run_at)})\n"
                f"  (was: {old_next})"
            )

        # recurring task
        try:
            spec = _parse_recurring_spec(new_when)
        except ValueError as e:
            return f"❌ {e}"

        if spec['kind'] == 'calendar':
            try:
                next_run = _next_calendar_run(spec['weekdays'], spec['hour'], spec['minute'])
            except ValueError as e:
                return f"❌ {e}"
            t['recur_kind']       = 'calendar'
            t['interval_seconds'] = None
            t['cal_weekdays']     = spec['weekdays']
            t['cal_hour']         = spec['hour']
            t['cal_minute']       = spec['minute']
            t['cal_label']        = spec['label']
            t['next_run']         = next_run.isoformat()
            display = spec['label']
        else:
            secs     = spec['seconds']
            next_run = datetime.now() + timedelta(seconds=secs)
            t['recur_kind']       = 'interval'
            t['interval_seconds'] = secs
            t['next_run']         = next_run.isoformat()
            for key in ('cal_weekdays', 'cal_hour', 'cal_minute', 'cal_label'):
                t.pop(key, None)
            display = f"every {_human_interval(secs)}"

        _save(tasks)
        return (
            f"✅ Rescheduled '{name}' to run {display}, next at "
            f"{next_run.strftime('%Y-%m-%d %H:%M')} ({_eta(next_run)})\n"
            f"  (was: {old_next})"
        )


def run_now(name: str) -> str:
    """Immediately fire a scheduled task by name, regardless of its next_run time.
    The task is NOT deleted after firing — its schedule is preserved exactly as-is.
    Use this to test a task or force an out-of-band execution."""
    with _lock:
        tasks = _load()
        if name not in tasks:
            return f"❌ Task '{name}' not found. Use list_tasks() to see available tasks."
        prompt = tasks[name]['prompt']
        if name in _firing_tasks:
            return f"⚠️ Task '{name}' is already running. Try again after it completes."
        _firing_tasks.add(name)

    try:
        fired_at = datetime.now()
        print(f"[scheduler] run_now: {name}")
        result = _dispatch(prompt, name)
    except Exception as e:
        result = f"❌ dispatch exception: {e}"
    finally:
        with _lock:
            _firing_tasks.discard(name)

    with _lock:
        tasks = _load()
        if name in tasks:
            tasks[name]['last_run']    = fired_at.isoformat()
            tasks[name]['last_result'] = result[:300]
            tasks[name]['run_count']   = tasks[name].get('run_count', 0) + 1
            _save(tasks)
    _append_activity(f"scheduler:{name}", prompt, result)
    icon = "✅" if not result.startswith("❌") else "❌"
    return f"{icon} run_now '{name}' fired at {fired_at.strftime('%H:%M:%S')} → {result[:200]}"


def clear() -> str:
    """Cancel and remove ALL scheduled tasks."""
    with _lock:
        tasks = _load()
        count = len(tasks)
        _save({})
    return f"✅ Cleared {count} task(s)"


def status() -> str:
    """Show scheduler health: running state, task count, check interval."""
    with _lock:
        tasks = _load()
    thread_alive = _thread is not None and _thread.is_alive()
    return (
        f"Scheduler: {'running ✅' if thread_alive else 'stopped ❌'} | "
        f"Tasks: {len(tasks)} | "
        f"Poll interval: 30s (after dispatch completes) | "
        f"Storage: {_TASKS_FILE}"
    )


def _read_activity_log_entries() -> list:
    """Parse all valid JSON lines from the activity log. Returns [] if missing."""
    if not _ACTIVITY_LOG.exists():
        return []
    entries = []
    for line in _ACTIVITY_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    return entries


def get_activity_log(hours: int = 24) -> str:
    """Show what the agent did in the last N hours (scheduled + manual tasks).
    Reads from /app/memory/activity_log.jsonl. Default: last 24 hours."""
    try:
        all_entries = _read_activity_log_entries()
        if not all_entries:
            return "📭 No activity logged yet."
        cutoff = datetime.now() - timedelta(hours=hours)
        entries = [e for e in all_entries if datetime.fromisoformat(e["ts"]) >= cutoff]
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
        all_entries = _read_activity_log_entries()
        if not all_entries:
            return "📭 No activity logged yet."
        prefix = f"scheduler:{name}"
        entries = [e for e in all_entries if e.get("source") == prefix][-limit:]
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


def help_schedule() -> str:
    """Return usage documentation for the schedule skill."""
    return (
        "📋 schedule(name, when, prompt)\n"
        "  name:   unique task ID (e.g., 'daily_backup')\n"
        "  when:   natural language time ('in 2h', 'tomorrow at 9am', 'next monday')\n"
        "  prompt: what the agent should execute\n"
        "Example: schedule_recurring('report', 'every monday at 8am', 'Generate weekly metrics')\n"
        "         schedule_recurring('prices', '6h', 'Check DDR5 prices')\n"
        "         schedule_recurring('standup', 'every weekday at 9am', 'Send standup summary')\n\n"
        "Recurrence formats accepted:\n"
        "  Duration intervals:  '30m', '2h', '1d', '2w', 'every 6 hours'\n"
        "  Calendar-aligned:    'every day at 8pm', 'every monday at 9am',\n"
        "                       'every weekday at 8am', 'every weekend at 10am'\n\n"
        "Other functions: list_tasks(), "
        "get_task(name), edit_task_prompt(name, new_prompt), "
        "edit_task_when(name, new_when) — change when a task runs without touching its prompt, "
        "run_now(name) — immediately fire a task without affecting its schedule, "
        "remove(name), clear(), status(), stop(), "
        "parse_preview(when) — preview what a time or recurring expression resolves to, "
        "get_activity_log(hours=24), get_task_report(name, limit=50)"
    )


def parse_preview(when: str) -> str:
    """
    Preview what a time or recurrence expression resolves to without scheduling anything.
    Works for both one-time ('in 2h', 'tomorrow at 9am') and recurring specs ('every monday at 9am', '2h').
    """
    s = when.strip().lower()
    # Try recurring spec first if it looks like an interval/recurring pattern
    if s.startswith('every') or re.match(r'\d+(?:\.\d+)?\s*' + _UNIT_PATTERN, s):
        try:
            spec = _parse_recurring_spec(when)
            if spec['kind'] == 'calendar':
                next_run = _next_calendar_run(spec['weekdays'], spec['hour'], spec['minute'])
                return (
                    f"🔁 '{when}' → recurring {spec['label']}\n"
                    f"   next occurrence: {next_run.strftime('%Y-%m-%d %H:%M')} ({_eta(next_run)})"
                )
            else:
                next_run = datetime.now() + timedelta(seconds=spec['seconds'])
                return (
                    f"🔁 '{when}' → recurring every {_human_interval(spec['seconds'])}\n"
                    f"   first run: {next_run.strftime('%Y-%m-%d %H:%M')} ({_eta(next_run)})"
                )
        except ValueError:
            pass  # fall through to one-time parse
    try:
        dt = _parse_when(when)
        now = datetime.now()
        if dt <= now:
            return f"⚠️  '{when}' → {dt.strftime('%Y-%m-%d %H:%M')} (already in the past!)"
        return f"🕐 '{when}' → {dt.strftime('%Y-%m-%d %H:%M')} ({_eta(dt)})"
    except ValueError as e:
        return f"❌ {e}"
