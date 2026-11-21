"""Background jobs: run independent work in a separate agent session.

A job is a POST to this agent's own /chat endpoint with a dedicated
session_id (`job_<id>`), the same self-dispatch pattern scheduler.py uses.
That keeps the main conversation responsive while the job works, and gives
each job its own session history, cancellation event and JSONL transcript.

Lifecycle: queued -> running -> done | failed | cancelled | interrupted.
`interrupted` is assigned at startup to jobs whose process died mid-run.

Store: memory/jobs.json (atomic write). Per-job transcript:
memory/jobs/<id>.log. Jobs are capped so the store cannot grow forever.
"""
import json
import os
import secrets
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

NAME = "jobs"
SHORT_DOC = ("delegate(prompt, name) runs a task in the background; list() and "
             "result(id) show it, cancel(id) stops it.")
DOC = ("Run independent work in a background session so the chat stays responsive. "
       "delegate(prompt, name='', model='') -> job id; the job runs with the normal tools "
       "and reports back here. list(state='') shows jobs with status and elapsed; "
       "result(id) returns a finished job's output; cancel(id) stops one. "
       "Use for research, builds, audits and "
       "anything over a couple of minutes. Never delegate tasks that need user approval, "
       "and never delegate from inside a job (depth limit 1).")

__all__ = ["delegate", "list", "result", "cancel"]

# ── Config ───────────────────────────────────────────────────────────────────
_MAX_WORKERS = max(1, int(os.getenv("JOBS_MAX_WORKERS", "2")))
_MAX_ACTIVE = max(1, int(os.getenv("JOBS_MAX_ACTIVE", "4")))
_TIMEOUT = max(60, int(os.getenv("JOBS_TIMEOUT_SECONDS", "1800")))
_KEEP_JOBS = max(20, int(os.getenv("JOBS_KEEP", "100")))
_TRANSIENT = {429, 502, 503, 504}
_STORE_NAME = "jobs.json"
_LOG_DIR = "jobs"

# Skills that hold process-wide state or rewrite the skill registry. Two of
# these running at once (a job + the main chat) corrupts state rather than
# merely racing: one Playwright/CDP connection, one preview server, one
# registry rebuild. Jobs get the read-only/pure tools by default.
_DEFAULT_DENY = frozenset({
    "browser_session",   # single global CDP/Playwright driver
    "web_builder",       # single preview server + project lock
    "create_skill",      # reload_skills() swaps the global registry mid-turn
    "autoimprove",       # mutates skills/ and the registry
    "mcp_client",        # rewrites mcp_servers.json + tool schema cache
})

_TERMINAL = ("done", "failed", "cancelled", "interrupted")

# ── State ────────────────────────────────────────────────────────────────────
# create_skill.reload() drops skills.* from sys.modules and re-imports, so the
# lock and semaphore live on `sys`: old and new module instances must still
# serialise store writes and share one concurrency cap.
_lock = getattr(sys, "_trinity_jobs_lock", None)
if _lock is None:
    _lock = threading.RLock()
    sys._trinity_jobs_lock = _lock

_ctx = getattr(sys, "_trinity_jobs_ctx", None)
if _ctx is None:
    _ctx = threading.local()
    sys._trinity_jobs_ctx = _ctx


def _sem() -> threading.BoundedSemaphore:
    sem = getattr(sys, "_trinity_jobs_sem", None)
    if sem is None:
        sem = threading.BoundedSemaphore(_MAX_WORKERS)
        sys._trinity_jobs_sem = sem
    return sem


def _spawn_worker(job_id: str) -> None:
    """Daemon thread: a stuck job must never delay container shutdown."""
    def _run():
        with _sem():
            _worker(job_id)
    threading.Thread(target=_run, daemon=True, name=f"job-{job_id}").start()


# ── Paths / store ────────────────────────────────────────────────────────────

def _home() -> Path:
    return Path(os.getenv("MEMORY_HOME", "/app/memory"))


def _store() -> Path:
    return _home() / _STORE_NAME


def _log_path(job_id: str) -> Path:
    return _home() / _LOG_DIR / f"{job_id}.log"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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


def _read() -> dict:
    """Return {job_id: record}. Corrupt/unexpected rows are dropped."""
    f = _store()
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}
    raw = data.get("jobs") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return {}
    out = {}
    for jid, rec in raw.items():
        if not isinstance(rec, dict):
            continue
        if not rec.get("id") or not rec.get("status"):
            continue
        out[str(jid)] = rec
    return out


def _save(jobs: dict) -> None:
    # Keep every active job; trim the oldest terminal ones beyond _KEEP_JOBS.
    rows = sorted(jobs.values(), key=lambda r: r.get("created") or "", reverse=True)
    active = [r for r in rows if r.get("status") not in _TERMINAL]
    terminal = [r for r in rows if r.get("status") in _TERMINAL][:_KEEP_JOBS]
    keep = {r["id"]: r for r in active + terminal}
    _atomic_write(_store(), json.dumps(
        {"_updated": _now_iso(), "jobs": keep}, ensure_ascii=False, indent=2
    ))
    # Don't leak transcripts for jobs that fell out of the store.
    for jid in set(jobs) - set(keep):
        try:
            _log_path(jid).unlink()
        except OSError:
            pass


def _update(job_id: str, **fields) -> dict | None:
    with _lock:
        jobs = _read()
        rec = jobs.get(job_id)
        if rec is None:
            return None
        rec.update(fields)
        _save(jobs)
        return dict(rec)


def _append_log(job_id: str, line: str) -> None:
    try:
        p = _log_path(job_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and p.stat().st_size > 200_000:
            p.write_text("", encoding="utf-8")
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{_now_iso()}  {line}\n")
    except Exception:
        pass


# ── Run context (set by app.chat / app.chat_stream) ───────────────────────────

def _set_run_context(session_id: str | None) -> None:
    """Called at the top of app.chat() so jobs can tell a delegated run from a
    user turn (recursion guard + skill deny list)."""
    _ctx.session_id = session_id or None


def _current_session() -> str:
    return getattr(_ctx, "session_id", "") or ""


def _is_job_session() -> bool:
    return _current_session().startswith("job_")


def _denied_skills() -> frozenset:
    raw = os.getenv("JOBS_DENY_SKILLS", "").strip()
    if not raw:
        return _DEFAULT_DENY
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


def _job_denial(skill_name: str) -> str | None:
    """Called by app.call_skill_improved(). None means allowed."""
    if not _is_job_session():
        return None
    if skill_name in _denied_skills():
        return (f"❌ Skill '{skill_name}' is not available inside a delegated job "
                f"(process-wide state). Do the work with files/web/notes/memory, "
                f"or report that this step needs the main session.")
    return None


# ── Helpers ──────────────────────────────────────────────────────────────────

def _base_url() -> str:
    url = os.getenv("TRINITY_AGENT_URL", "http://127.0.0.1:8001/chat")
    return url.rsplit("/chat", 1)[0].rstrip("/")


def _headers() -> dict:
    key = os.getenv("TRINITY_API_KEY", "")
    return {"X-API-Key": key} if key else {}


def _new_id(existing: set) -> str:
    for _ in range(5):
        cand = "j_" + secrets.token_hex(3)
        if cand not in existing:
            return cand
    return "j_" + secrets.token_hex(4)


def _elapsed(rec: dict) -> str:
    try:
        start = datetime.fromisoformat(rec.get("started") or rec.get("created"))
        end = datetime.fromisoformat(rec["finished"]) if rec.get("finished") else datetime.now(timezone.utc)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        secs = max(0, int((end - start).total_seconds()))
    except Exception:
        return "?"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    return f"{secs // 3600}h{(secs % 3600) // 60:02d}m"


def _short(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _status_icon(status: str) -> str:
    return {
        "queued": "⏳", "running": "🔄", "done": "✅",
        "failed": "❌", "cancelled": "⏹️", "interrupted": "⚠️",
    }.get(status, "•")


def _telegram():
    """Locate the loaded telegram_bot module.

    The skill loader registers modules as `skills.<stem>`, while some skills
    import each other as `skills.core.<stem>` — importing the latter a second
    time would create a duplicate module. Prefer what is already loaded.
    """
    for key in ("skills.telegram_bot", "skills.core.telegram_bot", "telegram_bot"):
        mod = sys.modules.get(key)
        if mod is not None and hasattr(mod, "send"):
            return mod
    try:
        from skills import telegram_bot as tg  # type: ignore
        return tg
    except Exception:
        return None


def _notify(rec: dict) -> None:
    if os.getenv("JOBS_NOTIFY", "1").strip().lower() in ("0", "false", "no", "off"):
        return
    if not os.getenv("TELEGRAM_CHAT_ID"):
        return
    tg = _telegram()
    if tg is None:
        return
    icon = _status_icon(rec.get("status"))
    body = rec.get("result") or rec.get("error") or ""
    msg = (f"{icon} Background job {rec['id']} — {rec.get('status')}\n"
           f"{rec.get('name') or _short(rec.get('prompt', ''), 60)}\n\n"
           f"{_short(body, 700)}")
    try:
        tg.send(msg)
    except Exception as e:
        _append_log(rec["id"], f"notify failed: {e}")


# ── Worker ───────────────────────────────────────────────────────────────────

_JOB_DIRECTIVE = (
    "[BACKGROUND JOB — NO USER PRESENT]\n"
    "You are running delegated work in a separate session. Nobody will answer "
    "questions here. Complete the task end-to-end; if a detail is missing, pick "
    "the most reasonable assumption and state it. When finished, reply with a "
    "compact result the parent agent can relay: what you did, key findings, "
    "files/paths, and anything still unresolved. Do not call jobs__delegate.\n\n"
)


def _worker(job_id: str) -> None:
    with _lock:
        rec = _read().get(job_id)
    if not rec or rec.get("status") != "queued":
        return  # cancelled before it started

    _update(job_id, status="running", started=_now_iso())
    _append_log(job_id, "started")
    payload = {
        "message": _JOB_DIRECTIVE + rec.get("prompt", ""),
        "session_id": f"job_{job_id}",
        "model": rec.get("model") or "trinity-default",
    }

    last_err = ""
    status, result, error = "failed", "", ""
    for attempt in (1, 2):
        try:
            resp = requests.post(_base_url() + "/chat", json=payload,
                                 headers=_headers(), timeout=(10, _TIMEOUT))
            if resp.ok:
                data = resp.json()
                result = str(data.get("response") or data)[:200_000]
                status, error = "done", ""
                break
            if resp.status_code in _TRANSIENT and attempt == 1:
                last_err = f"HTTP {resp.status_code}"
                _append_log(job_id, f"transient {last_err}, retrying")
                time.sleep(5)
                continue
            error = f"HTTP {resp.status_code}: {resp.text[:300]}"
            break
        except requests.ConnectionError as e:
            last_err = f"connection: {e}"
            if attempt == 1:
                _append_log(job_id, f"{last_err} — retrying once")
                time.sleep(5)
                continue
            error = last_err
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            break

    with _lock:
        current = _read().get(job_id) or {}
        if current.get("status") == "cancelled":
            _append_log(job_id, "finished after cancel request — keeping cancelled state")
            _update(job_id, finished=current.get("finished") or _now_iso())
            return
        _update(job_id, status=status, result=result, error=error,
                finished=_now_iso(), delivered=False)
    _append_log(job_id, f"{status}: {_short(result or error, 300)}")
    rec = _read().get(job_id) or {}
    if rec:
        _notify(rec)


# ── Public tool API ──────────────────────────────────────────────────────────

def delegate(prompt: str, name: str = "", model: str = "") -> str:
    """Start a background job. Returns its id immediately.

    Args:
        prompt: Full, self-contained task description (the job cannot ask questions).
        name: Short label for the job list (defaults to the prompt's first words).
        model: Optional model override; empty uses trinity-default.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        return "Error: prompt is required — give the job a self-contained task."
    if _is_job_session():
        return "Error: cannot delegate from inside a delegated job (depth limit 1)."
    if not os.getenv("TRINITY_API_KEY", "").strip():
        return ("Error: background jobs need TRINITY_API_KEY — a job dispatches to this "
                "agent's own /chat endpoint, which fails closed without the key.")

    with _lock:
        jobs = _read()
        active = [r for r in jobs.values() if r.get("status") in ("queued", "running")]
        if len(active) >= _MAX_ACTIVE:
            ids = ", ".join(f"{r['id']} ({r.get('status')})" for r in active[:5])
            return (f"Error: {_MAX_ACTIVE} jobs already active ({ids}). "
                    f"Wait for one via jobs__list() or stop it with jobs__cancel(id).")
        jid = _new_id(set(jobs))
        rec = {
            "id": jid,
            "name": _short(name, 60) if name else _short(prompt, 60),
            "prompt": prompt[:20_000],
            "model": (model or "").strip(),
            "status": "queued",
            "created": _now_iso(),
            "started": None,
            "finished": None,
            "result": "",
            "error": "",
            "delivered": False,
            "session_id": f"job_{jid}",
        }
        jobs[jid] = rec
        _save(jobs)

    _append_log(jid, f"queued: {_short(prompt, 200)}")
    try:
        _spawn_worker(jid)
    except Exception as e:
        _update(jid, status="failed", error=f"could not start worker: {e}",
                finished=_now_iso())
        return f"❌ Job {jid} could not start: {e}"

    return (f"✅ Job {jid} started — \"{rec['name']}\".\n"
            f"It runs in its own session; keep working here. "
            f"Check it with jobs__list() or read it with jobs__result('{jid}'). "
            f"It also appears in the background jobs list on the next turn.")


def list(state: str = "") -> str:
    """List jobs, newest first. Filter by state if given.

    Args:
        state: queued, running, done, failed, cancelled, interrupted, or empty for all.
    """
    state = (state or "").strip().lower()
    valid = ("queued", "running") + _TERMINAL
    if state and state not in valid:
        return f"Error: state must be one of {', '.join(valid)} (or empty)."
    jobs = _read()
    rows = sorted(jobs.values(), key=lambda r: r.get("created") or "", reverse=True)
    if state:
        rows = [r for r in rows if r.get("status") == state]
    if not rows:
        return "No jobs" + (f" in state '{state}'." if state else " yet.")
    lines = [f"Jobs ({len(rows)}):"]
    for r in rows[:20]:
        icon = _status_icon(r.get("status"))
        lines.append(f"{icon} {r['id']}  {r.get('status'):<11} {_elapsed(r):>6}  "
                     f"{_short(r.get('name') or r.get('prompt'), 60)}")
    if len(rows) > 20:
        lines.append(f"… {len(rows) - 20} older")
    return "\n".join(lines)


def result(job_id: str) -> str:
    """Return a finished job's output (marks it read).

    Args:
        job_id: The id returned by delegate().
    """
    jid = str(job_id).strip()
    with _lock:
        jobs = _read()
        rec = jobs.get(jid)
        if not rec:
            return f"Error: no job '{job_id}'. Use jobs__list() to see ids."
        rec["delivered"] = True
        _save(jobs)
    if rec.get("status") in ("queued", "running"):
        return (f"{_status_icon(rec['status'])} Job {jid} is still {rec['status']} "
                f"({_elapsed(rec)}). Ask again when it finishes.")
    body = rec.get("result") or rec.get("error") or "(no output)"
    header = f"{_status_icon(rec['status'])} Job {jid} — {rec['status']}"
    return f"{header}\nName: {rec.get('name')}\nElapsed: {_elapsed(rec)}\n\n{body[:8000]}"


def cancel(job_id: str) -> str:
    """Cancel a queued or running job.

    Args:
        job_id: The id returned by delegate().
    """
    jid = str(job_id).strip()
    with _lock:
        jobs = _read()
        rec = jobs.get(jid)
        if not rec:
            return f"Error: no job '{job_id}'. Use jobs__list() to see ids."
        if rec.get("status") in _TERMINAL:
            return f"Job {jid} is already {rec['status']} — nothing to cancel."
        was_running = rec.get("status") == "running"
        rec["status"] = "cancelled"
        rec["finished"] = rec.get("finished") or _now_iso()
        rec["error"] = rec.get("error") or "cancelled by user"
        rec["delivered"] = True
        _save(jobs)
    # Queued jobs need no signal: the worker exits when status != queued.
    if was_running:
        try:
            requests.post(_base_url() + "/session/cancel",
                          params={"session_id": f"job_{jid}"},
                          headers=_headers(), timeout=(5, 10))
        except Exception as e:
            _append_log(jid, f"cancel signal failed: {e}")
    _append_log(jid, "cancelled by user")
    return f"⏹️ Job {jid} cancelled."


# ── Prompt block (rendered per turn) ─────────────────────────────────────────

def _render_block() -> str:
    """Compact snapshot for the system prompt. '' when there is nothing to say."""
    try:
        jobs = _read()
    except Exception:
        return ""
    running = sorted((r for r in jobs.values() if r.get("status") == "running"),
                     key=lambda r: r.get("started") or "")[:3]
    queued = [r for r in jobs.values() if r.get("status") == "queued"]
    ready = sorted((r for r in jobs.values()
                    if r.get("status") in _TERMINAL and not r.get("delivered")),
                   key=lambda r: r.get("finished") or r.get("created") or "")[-5:]
    if not running and not queued and not ready:
        return ""
    lines = ["<BACKGROUND_JOBS>"]
    for r in running:
        lines.append(f"running: {r['id']} \"{_short(r.get('name'), 40)}\" ({_elapsed(r)})")
    if queued:
        lines.append(f"queued: {len(queued)} job(s)")
    for r in ready:
        lines.append(f"finished: {r['id']} \"{_short(r.get('name'), 40)}\" "
                     f"({r['status']}) — read with jobs__result('{r['id']}')")
    lines.append("Rule: long independent work → jobs__delegate(prompt, name). "
                 "Read finished jobs and report them in your reply. "
                 "Never delegate anything that needs user approval.")
    lines.append("</BACKGROUND_JOBS>")
    return "\n".join(lines)


# ── API helpers (used by app.py endpoints; not LLM tools) ────────────────────

def _api_list() -> dict:
    jobs = _read()
    rows = sorted(jobs.values(), key=lambda r: r.get("created") or "", reverse=True)
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    for r in rows:
        r["elapsed"] = _elapsed(r)
        r.pop("prompt", None)  # prompt can be huge; detail endpoint has it
    return {"jobs": rows[:100], "counts": counts, "active": counts.get("running", 0) + counts.get("queued", 0)}


def _api_get(job_id: str) -> dict | None:
    rec = _read().get(str(job_id).strip())
    if not rec:
        return None
    rec["elapsed"] = _elapsed(rec)
    return rec


def _api_cancel(job_id: str) -> dict:
    msg = cancel(job_id)
    ok = not msg.startswith("Error:")
    return {"ok": ok, "message": msg, "job": _api_get(job_id)}


# ── Startup reconciliation ───────────────────────────────────────────────────

def _boot() -> None:
    """Mark jobs orphaned by a crash/restart. Runs once per process, not on
    create_skill.reload() (which re-imports this module)."""
    if getattr(sys, "_trinity_jobs_booted", False):
        return
    sys._trinity_jobs_booted = True
    try:
        with _lock:
            jobs = _read()
            changed = False
            for rec in jobs.values():
                if rec.get("status") in ("queued", "running"):
                    rec["status"] = "interrupted"
                    rec["error"] = "agent restarted while the job was running"
                    rec["finished"] = rec.get("finished") or _now_iso()
                    rec["delivered"] = False
                    changed = True
            if changed:
                _save(jobs)
    except Exception:
        pass


_boot()
