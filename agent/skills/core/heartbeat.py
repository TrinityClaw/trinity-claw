"""Heartbeat: ambient thought between conversations.

Every HEARTBEAT_INTERVAL_SECONDS of quiet, the app's background thread asks:
is there anything worth doing right now (open loops, due tasks, parked ideas)?
The tick may act through the normal skill funnel (deny-list enforced) and may
ping the user via Telegram — otherwise it replies HEARTBEAT_MARKER and nothing
reaches anyone. One audit row per tick lands in memory/heartbeat.jsonl.
"""
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

DOC = "Ambient heartbeat ticks between conversations (started automatically at boot)."
SHORT_DOC = "Silent unless something is worth doing; pings via telegram; audit in memory/heartbeat.jsonl."

__all__: list = []

HEARTBEAT_MARKER = "NO_REPLY"
_AUDIT_MAX = 200
_TRUE_SET = ("true", "1", "yes", "on")


def _home() -> Path:
    return Path(os.getenv("MEMORY_HOME", "/app/memory"))


def _cfg() -> dict:
    def _int(var: str, default: int, lo: int = 1) -> int:
        try:
            v = int(os.getenv(var, str(default)))
        except (TypeError, ValueError):
            return default
        return max(lo, v) if v < lo else v

    return {
        "enabled": os.getenv("HEARTBEAT_ENABLED", "true").strip().lower() in _TRUE_SET,
        "interval": _int("HEARTBEAT_INTERVAL_SECONDS", 900, lo=60),
        "max_cloud": _int("HEARTBEAT_MAX_CLOUD", 1, lo=0),
        "num_predict": _int("HEARTBEAT_NUM_PREDICT", 1500),
    }


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


def _audit(row: dict) -> None:
    """Append one JSONL row, then rewrite the file trimmed to _AUDIT_MAX rows."""
    f = _home() / "heartbeat.jsonl"
    try:
        rows = []
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    rows.append(line)
        rows.append(json.dumps(row, ensure_ascii=False))
        _atomic_write(f, "\n".join(rows[-_AUDIT_MAX:]) + "\n")
    except Exception:
        pass


def _parse_naive_local(ts) -> "datetime | None":
    try:
        dt = datetime.fromisoformat(str(ts))
    except Exception:
        return None
    if dt.tzinfo is not None:
        dt = dt.replace(tzinfo=None)
    return dt  # scheduled tasks use naive local timestamps by house convention


def _tick_context(quiet_minutes: int) -> str:
    """Compact per-tick briefing. Reads stores; persists hygiene only."""
    lines = [f"Away for {quiet_minutes} min of quiet."]
    try:
        from skills.core import open_loops as ol
        loops, _ = ol._load()
        ol._save_clean()
        open_ls = [l for l in loops if l["state"] == "open"]
        exp_ls = [l for l in loops if l["state"] == "expired"]
        if open_ls:
            for l in open_ls[:3]:
                lines.append(f"open loop [{l['id']}]: {l['text']}")
            if len(open_ls) > 3:
                lines.append(f"(+{len(open_ls) - 3} more open loops)")
        if exp_ls:
            lines.append(f"{len(exp_ls)} expired promise(s) need re-committing or ownership")
    except Exception:
        pass
    try:
        f = _home() / "scheduled_tasks.json"
        if f.exists():
            data = json.loads(f.read_text(encoding="utf-8"))
            now = datetime.now()
            window = now + timedelta(minutes=60)
            due = []
            for name, t in (data or {}).items():
                if not isinstance(t, dict):
                    continue
                dt = _parse_naive_local(t.get("next_run"))
                if dt and now <= dt <= window:
                    due.append(f"{name} due {dt.strftime('%H:%M')}")
            if due:
                lines.append("tasks due within the hour: " + "; ".join(due[:4]))
    except Exception:
        pass
    try:
        aj = _home() / "daily_journal.jsonl"
        if aj.exists():
            entries = [l for l in aj.read_text(encoding="utf-8").splitlines() if l.strip()]
            if entries:
                last = json.loads(entries[-1])
                summary = str(last.get("summary", ""))[:120]
                if summary:
                    lines.append(f"last journal: {summary}")
    except Exception:
        pass
    try:
        from skills.core import autoimprove
        ideas = [r for r in autoimprove._load_ideas() if r.get("status") == "open"]
        if ideas:
            lines.append(f"{len(ideas)} parked idea(s) waiting in autoimprove")
    except Exception:
        pass
    return "\n".join(lines)


_TICK_RULES = """You are the quiet-time heartbeat of the agent. Nobody is watching.
Decide rhythmically, not desperately. Reply rules, EXACTLY:
- Nothing worth doing: reply with NO_REPLY alone. Most ticks. A statement with no
  task, no danger, and nothing the user would want to know NOW is NOT worth doing —
  never send filler like "Everything looks fine." or "Observation." in its place.
- Something the user would want to know NOW: reply PING: <one short sentence>.
- A small concrete task you can finish yourself with tools: execute it like in a
  normal turn using your tools, then reply ACT DONE: <one-line summary>.
Never: terminal, x_engagement, knowledge_base ingest, git push-class ops,
autoimprove mutating calls, web_builder. These are hard-refused in tick mode.
Allowed: memory, memory_search, open_loops, notes, dashboard, telegram send
(ping path), read-only web fetches/price checks.
Tool calls in ticks use the same formats as normal turns (e.g. <skill:name.func>
or <tool:name.func> style tags) — execute them as usual in tick mode.
Keep every ponder under 12 seconds of compute. When in doubt, NO_REPLY."""


_running = False                       # re-entry guard for the whole tick

_LAST_CALL_TOKENS = {"prompt": None, "output": None}


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _llm_tick_call(system: str, user: str, num_predict: int) -> str:
    """One small local-Ollama call for a tick. Stubbed in tests."""
    import requests
    base = os.getenv("OLLAMA_API_BASE", "http://ollama:11434")
    payload = {
        "model": os.getenv("OLLAMA_MODEL", "llama3.2-vision"),
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
        "think": False,
        "options": {"num_predict": num_predict,
                    "temperature": float(os.getenv("OLLAMA_TEMPERATURE", "0.6")),
                    "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "12288"))},
    }
    resp = requests.post(f"{base}/api/chat", json=payload,
                         timeout=int(os.getenv("HEARTBEAT_TIMEOUT", "120")))
    resp.raise_for_status()
    data = resp.json()
    message = data.get("message", {}) or {}
    _LAST_CALL_TOKENS.clear()
    _LAST_CALL_TOKENS["prompt"] = _int_or_none(data.get("prompt_eval_count"))
    _LAST_CALL_TOKENS["output"] = _int_or_none(data.get("eval_count"))
    return message.get("content") or ""


def _llm_escalate(system: str, user: str, num_predict: int) -> str | None:
    """One cloud fallback via litellm proxy; None if unavailable."""
    import requests
    key = os.getenv("API_KEY_LIGHTLLM", "sk-1234567890")
    base = os.getenv("LITELLM_API_BASE", "http://litellm:4000")
    try:
        resp = requests.post(
            f"{base}/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": os.getenv("CLOUD_MODEL", "gpt-4o-mini"),
                  "messages": [{"role": "system", "content": system},
                               {"role": "user", "content": user}],
                  "max_tokens": num_predict},
            timeout=120)
        resp.raise_for_status()
        data = resp.json()
        usage = data.get("usage", {}) or {}
        _LAST_CALL_TOKENS.clear()
        _LAST_CALL_TOKENS["prompt"] = _int_or_none(usage.get("prompt_tokens"))
        _LAST_CALL_TOKENS["output"] = _int_or_none(usage.get("completion_tokens"))
        return data.get("choices", [{}])[0].get("message", {}).get("content")
    except Exception:
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
