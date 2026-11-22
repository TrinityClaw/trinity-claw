"""Persistent pending-confirmation store (survives restart).

Extracted from app.py to keep the main module lighter. Handles reading,
writing, expiring, and validating pending user confirmations for destructive
or outward-facing skill actions (e.g. files.rm, git_manager.push, email).
All confirmations live in /app/memory/confirmations.json.
"""

import json
import os
import tempfile
import threading
import time
from pathlib import Path

_CONFIRM_STORE_NAME = "confirmations.json"
_CONFIRM_TTL_SECONDS = 2 * 3600  # pending confirmations expire after 2h
_confirm_lock = threading.Lock()


def _confirm_store() -> Path:
    return Path(os.getenv("MEMORY_HOME", "/app/memory")) / _CONFIRM_STORE_NAME


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


def _read_confirms() -> dict:
    f = _confirm_store()
    if not f.exists():
        return {}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    now = time.time()
    for sid, rec in data.items():
        if not isinstance(rec, dict):
            continue
        if "skill" not in rec or "function" not in rec:
            continue
        # Drop expired confirmations
        if now - rec.get("ts", 0) > _CONFIRM_TTL_SECONDS:
            continue
        out[str(sid)] = rec
    return out


def _save_confirms(confirms: dict) -> None:
    _atomic_write(_confirm_store(), json.dumps(confirms, ensure_ascii=False, indent=2))


def _load_confirms() -> None:
    """No-op at import — confirms are read lazily per-session (no global state)."""
    pass


_load_confirms()


def _get_pending_confirm(session_id: str) -> dict | None:
    with _confirm_lock:
        confirms = _read_confirms()
        return confirms.get(session_id)


def _set_pending_confirm(session_id: str, rec: dict) -> None:
    with _confirm_lock:
        confirms = _read_confirms()
        rec["ts"] = time.time()
        confirms[session_id] = rec
        _save_confirms(confirms)


def _clear_pending_confirm(session_id: str) -> None:
    with _confirm_lock:
        confirms = _read_confirms()
        if session_id in confirms:
            del confirms[session_id]
            _save_confirms(confirms)


def _confirm_yes(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in ("yes", "y", "ye", "yea", "yeah", "yep", "sure", "ok", "okay",
                 "confirm", "confirmed", "approve", "approved", "go", "go ahead",
                 "proceed", "do it", "execute")


def _confirm_no(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in ("no", "n", "nope", "cancel", "cancelled", "canceled", "abort",
                 "stop", "dont", "don't", "do not", "never", "reject", "rejected")


def _format_confirm_result(result: dict) -> str:
    """Turn a call_skill_improved result dict into a plain-text reply."""
    if result.get("confirmation_required"):
        return result.get("question", "Confirmation still required.")
    if result.get("success"):
        return str(result.get("result", ""))
    return f"❌ {result.get('error', 'Action failed')}"


__all__ = [
    "_CONFIRM_STORE_NAME",
    "_CONFIRM_TTL_SECONDS",
    "_confirm_store",
    "_read_confirms",
    "_save_confirms",
    "_load_confirms",
    "_get_pending_confirm",
    "_set_pending_confirm",
    "_clear_pending_confirm",
    "_confirm_yes",
    "_confirm_no",
    "_format_confirm_result",
]
