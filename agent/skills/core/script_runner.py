"""
Script Runner — executes Python scripts that call skills via RPC.

This is the zero-context-cost multi-step engine: instead of the model emitting
one skill tag per iteration (each iteration re-sends the full system prompt),
the model writes ONE script that calls many skills through the tool() helper.
The script runs in an isolated subprocess and talks to the agent's own
/skill/call endpoint over HTTP — no LLM in the loop between steps.

Trust model: same as the terminal skill — the model's own code, sandboxed in
a subprocess. Skills themselves stay access-controlled by /skill/call.
"""

import os
import re
import sys
import subprocess
import tempfile

NAME = "script_runner"
SHORT_DOC = "Run a Python script that calls many skills via tool() in one turn."
DOC = (
    "Zero-context multi-step engine: write ONE Python script and run it — skills are called "
    "inside via tool(skill_name, function_name, *args, **kwargs), which returns the result "
    "(failures raise RuntimeError). The whole pipeline collapses into one turn. "
    "Functions: run_script(code, timeout)."
)
SKILL_TIMEOUT = int(os.getenv("SCRIPT_RUNNER_TIMEOUT", "300"))

__all__ = ["NAME", "SHORT_DOC", "DOC", "SKILL_TIMEOUT", "run_script"]

_DEFAULT_SCRIPT_TIMEOUT = int(os.getenv("SCRIPT_RUNNER_TIMEOUT", "300"))
_SCRIPT_OUTPUT_CAP = 3000

# The script subprocess gets only a safe baseline plus what it needs to talk
# to the agent's own endpoint (the API key) — same posture as mcp_client stdio.
_SCRIPT_ENV_BASELINE = (
    "PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "TEMP", "TMP",
    "TRINITY_API_KEY", "TRINITY_SELF_BASE",
)

_PRELUDE = '''import os as _os, requests as _requests
_API = _os.environ.get("TRINITY_API_KEY", "")
_BASE = _os.environ.get("TRINITY_SELF_BASE", "http://localhost:8001")

def tool(skill, func, *args, **kwargs):
    """Call an agent skill via RPC. Returns the result; failures raise RuntimeError."""
    r = _requests.post(
        _BASE + "/skill/call",
        json={"skill": skill, "function": func, "args": list(args), "kwargs": kwargs},
        headers={"X-API-Key": _API},
        timeout=120,
    )
    if r.status_code != 200:
        try:
            d = r.json()
            err = d.get("detail") or d.get("error") or r.text[:200]
        except Exception:
            err = r.text[:200]
        if isinstance(err, list):
            err = str(err)[:200]
        raise RuntimeError(f"tool {skill}.{func} failed: {err}")
    d = r.json()
    if not d.get("success"):
        raise RuntimeError(f"tool {skill}.{func} failed: {d.get('error')}")
    return d.get("result")
'''


def run_script(code: str, timeout: int = None) -> str:
    """Execute a Python script that calls skills via the tool() RPC helper.

    The script runs in an isolated subprocess with access to the agent's own
    /skill/call endpoint. tool(skill, function, *args, **kwargs) returns the
    skill result; a failed call raises RuntimeError. Output is capped at 3000
    chars so the model's context never balloons.

    Use for multi-step tasks (3+ tool calls): the whole pipeline collapses
    into one turn with zero context cost between steps. For single quick
    commands, use a plain skill tag instead.
    """
    code = (code or "").strip()
    if not code:
        return "No script provided."

    # Strip markdown code fences if the model wrapped the code
    md = re.search(r"```(?:python|py)?\s*(.*?)\s*```", code, re.DOTALL)
    if md:
        code = md.group(1).strip()

    try:
        timeout = int(timeout) if timeout else _DEFAULT_SCRIPT_TIMEOUT
    except (TypeError, ValueError):
        timeout = _DEFAULT_SCRIPT_TIMEOUT

    with tempfile.NamedTemporaryFile(
        "w", suffix=".py", delete=False, encoding="utf-8"
    ) as f:
        f.write(_PRELUDE + "\n" + code + "\n")
        path = f.name

    try:
        env = {k: os.environ[k] for k in _SCRIPT_ENV_BASELINE if k in os.environ}
        proc = subprocess.run(
            [sys.executable, path],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd="/tmp" if os.path.isdir("/tmp") else None,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            tail = err[-1500:] if err else f"exit code {proc.returncode}"
            result = f"Script failed (exit {proc.returncode}):\n{tail}"
            if out:
                result += f"\n\nstdout:\n{out[:1000]}"
            return result
        if not out:
            if err:
                return f"Script completed.\nstderr: {err[:500]}"
            return "Script completed with no output."
        if len(out) > _SCRIPT_OUTPUT_CAP:
            out = out[:_SCRIPT_OUTPUT_CAP] + f"\n...[truncated, {len(out)} chars total]"
        return out
    except subprocess.TimeoutExpired:
        return f"Script timed out after {timeout}s — split it into smaller scripts."
    except Exception as e:
        return f"Script error: {e}"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
