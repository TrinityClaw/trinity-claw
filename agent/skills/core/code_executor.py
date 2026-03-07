"""
Code Executor Skill — Trinity's Runtime Sandbox

Closes the loop between create_skill + self_improvement by actually
running code. Without this, Trinity writes blind — syntax can be valid
but logic can still be broken.

Security layers:
  1. AST pre-scan blocks dangerous imports and shell-injection calls
  2. Subprocess isolation — never exec() in-process, always a child process
  3. Hard timeout with process kill
  4. Output truncation (2KB) to protect LLM context
  5. Neutral /tmp working directory (not /app/skills)

Integration with self_improvement:
  audit(skill) → find issue → fix(skill,...) → test_skill(skill, fn, args) → verify
"""

import ast
import math
import operator
import os
import re
import sys
import json
import tempfile
import textwrap
import subprocess
import shlex
from pathlib import Path
from typing import Optional

# ── Skill metadata (required by skill loader) ────────────────────────────────
NAME = "code_executor"
DOC = (
    "Safe Python sandbox: run code snippets, test existing skills, and calculate expressions in isolated subprocesses. "
    "Returns: run_snippet(code)→stdout output of the code (up to 2KB) or error message; "
    "test_skill(skill_name, fn_name, args_json)→function return value as text, useful for verifying a skill works; "
    "run_bash(command)→stdout of the bash command; "
    "calc(expression)→numeric result of a math expression as a string."
)

# ── Config ───────────────────────────────────────────────────────────────────
SKILLS_DIR   = Path("/app/skills")
MAX_OUTPUT   = 2000   # chars — keeps LLM context clean
MAX_TIMEOUT  = 30     # hard ceiling regardless of caller request
DEFAULT_TIMEOUT = 10

# Imports that signal dangerous intent inside untrusted snippets
_BLOCKED_IMPORTS = frozenset({
    "ctypes", "cffi", "socket", "multiprocessing",
})

# Regex patterns for shell-injection calls that AST alone can't catch reliably
_BLOCKED_PATTERNS = [
    (r"subprocess\.[a-zA-Z_]+\s*\([^)]*shell\s*=\s*True", "subprocess with shell=True"),
    (r"os\.system\s*\(",  "os.system()"),
    (r"os\.popen\s*\(",   "os.popen()"),
    (r"os\.execv[ep]?\s*\(", "os.exec*()"),
    (r"__import__\s*\(",  "__import__()"),
]

# Bash commands Trinity is allowed to run via run_bash()
# Note: 'python' is intentionally excluded — use run_snippet() for Python
_BASH_WHITELIST = frozenset({
    "ls", "cat", "echo", "pwd", "env", "which",
    "find", "head", "tail", "wc", "pip", "git", "touch",
})
_BASH_FORBIDDEN_CHARS = frozenset({";", "&", "|", ">", "<", "$", "(", ")", "`", "\n"})


# ── Internal helpers ─────────────────────────────────────────────────────────

def _clamp_timeout(t: int) -> int:
    return min(max(int(t), 1), MAX_TIMEOUT)


def _truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    snip = f"\n... [{len(text) - MAX_OUTPUT} chars truncated]"
    return text[:MAX_OUTPUT] + snip


def _scan_snippet(code: str) -> Optional[str]:
    """
    AST + regex pre-scan for untrusted snippets.
    Returns an error string if blocked, None if clean.
    """
    # 1. Syntax check
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"SyntaxError on line {e.lineno}: {e.msg}"

    # 2. AST walk — block dangerous top-level imports
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in _BLOCKED_IMPORTS:
                    return f"BLOCKED: 'import {alias.name}' is not permitted in the sandbox"
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top = node.module.split(".")[0]
                if top in _BLOCKED_IMPORTS:
                    return f"BLOCKED: 'from {node.module} import ...' is not permitted in the sandbox"

    # 3. Regex scan for shell-injection patterns
    for pattern, label in _BLOCKED_PATTERNS:
        if re.search(pattern, code):
            return f"BLOCKED: {label} is not permitted in the sandbox"

    return None  # Clean


def _run_in_subprocess(code: str, timeout: int) -> str:
    """
    Write code to a temp file, execute with the current Python interpreter,
    capture stdout/stderr, clean up. Returns formatted result string.
    """
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", prefix="trinity_exec_",
            dir="/tmp", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            tmp_path = f.name

        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd="/tmp",   # neutral working directory
        )

        stdout = _truncate(result.stdout)
        stderr = _truncate(result.stderr)

        if result.returncode == 0:
            output = stdout.strip() if stdout.strip() else "(no output)"
            return f"✅ Exit 0:\n{output}"
        else:
            output = stderr.strip() or stdout.strip() or "(no output)"
            return f"❌ Exit {result.returncode}:\n{output}"

    except subprocess.TimeoutExpired:
        return f"⌛ TIMEOUT: Exceeded {timeout}s limit. Possible infinite loop — increase timeout or fix the code."
    except Exception as e:
        return f"❌ EXECUTOR ERROR: {type(e).__name__}: {e}"
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# ── Public skill functions ───────────────────────────────────────────────────

def run_snippet(code: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """
    Run a Python code snippet in an isolated subprocess.

    Args:
        code:    Python source code to execute (multi-line supported).
        timeout: Max seconds to wait (1–30, default 10).

    Returns:
        Formatted string with exit status and stdout/stderr output.

    Example usage in Trinity:
        <skill:code_executor.run_snippet>
        result = 2 ** 10
        print(f"2^10 = {result}")
        </skill:code_executor.run_snippet>
    """
    timeout = _clamp_timeout(timeout)

    block_reason = _scan_snippet(code)
    if block_reason:
        return f"❌ REJECTED by security scanner:\n  {block_reason}"

    return _run_in_subprocess(code, timeout)


def test_skill(skill_name: str, fn_name: str, args_json: str = "[]", timeout: int = 15) -> str:
    """
    Import a skill module and call one of its functions with test arguments.
    This is the primary integration point with self_improvement — use it
    after applying a patch to verify the fix actually works at runtime.

    Args:
        skill_name: Skill filename stem (e.g. "notes", "weather").
        fn_name:    Function to call inside the skill.
        args_json:  JSON array of positional arguments. Default: "[]"
                    Examples: '["hello"]', '[42, true]', '["file.txt", "some content"]'
        timeout:    Max seconds (1–30, default 15).

    Returns:
        Formatted string with the function's return value or runtime error.

    Example — verify notes.add_note works:
        <skill:code_executor.test_skill>notes|add_note|["Test note from executor"]</skill:code_executor.test_skill>

    Integration workflow:
        1. self_improvement.audit("my_skill")       → finds issue
        2. self_improvement.fix("my_skill", ...)    → applies patch
        3. code_executor.test_skill("my_skill", "run", '["arg1"]')  → confirms fix
    """
    timeout = _clamp_timeout(timeout)

    # Locate the skill file
    skill_path = SKILLS_DIR / "dynamic" / f"{skill_name}.py"
    if not skill_path.exists():
        skill_path = SKILLS_DIR / "core" / f"{skill_name}.py"
    if not skill_path.exists():
        return (
            f"❌ Skill '{skill_name}' not found.\n"
            f"  Checked: {SKILLS_DIR}/dynamic/{skill_name}.py\n"
            f"           {SKILLS_DIR}/core/{skill_name}.py"
        )

    # Parse args
    try:
        args = json.loads(args_json)
        if not isinstance(args, list):
            args = [args]
    except json.JSONDecodeError:
        return (
            f"❌ Invalid args_json — must be a JSON array.\n"
            f"  Got: {args_json!r}\n"
            f"  Examples: '[]'  '[\"hello\"]'  '[42, true]'"
        )

    # Build a self-contained test harness (runs in subprocess, so imports are isolated)
    args_repr = repr(args)
    skill_path_str = str(skill_path).replace("\\", "/")

    harness = textwrap.dedent(f"""\
        import sys, json, traceback
        sys.path.insert(0, '/app/skills/core')
        sys.path.insert(0, '/app/skills/dynamic')
        sys.path.insert(0, '/app/skills')
        sys.path.insert(0, '/app')

        import importlib.util
        spec = importlib.util.spec_from_file_location("{skill_name}", "{skill_path_str}")
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception as e:
            print(f"❌ Failed to load skill '{skill_name}': {{type(e).__name__}}: {{e}}")
            traceback.print_exc()
            sys.exit(1)

        fn = getattr(mod, "{fn_name}", None)
        if fn is None:
            available = [x for x in dir(mod) if not x.startswith('_') and callable(getattr(mod, x))]
            print(f"❌ Function '{fn_name}' not found in '{skill_name}'.")
            print(f"   Available: {{available}}")
            sys.exit(1)

        args = {args_repr}
        try:
            result = fn(*args)
            if isinstance(result, (dict, list)):
                print(json.dumps(result, indent=2, default=str))
            elif result is None:
                print("(function returned None)")
            else:
                print(str(result))
        except Exception as e:
            print(f"❌ Runtime error in {{'{skill_name}.{fn_name}'}}: {{type(e).__name__}}: {{e}}")
            traceback.print_exc()
            sys.exit(1)
    """)

    # The harness itself is trusted code — skip scanner, run directly
    return _run_in_subprocess(harness, timeout)


def run_bash(command: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    """
    Run a whitelisted bash command in a subprocess (no shell=True).
    More permissive than safe_terminal but still restricted — no shell
    chaining characters, and base command must be in the whitelist.

    Allowed commands: ls, cat, echo, pwd, env, which, find, head, tail,
                      wc, pip, git, touch

    Use run_snippet() for Python — 'python' is not in the bash whitelist
    to prevent bypassing the code scanner.

    Args:
        command: The shell command string (no ; & | > < $ etc.)
        timeout: Max seconds (1–30, default 10).

    Returns:
        Formatted string with exit status and command output.
    """
    timeout = _clamp_timeout(timeout)

    # Block shell chaining
    if any(c in command for c in _BASH_FORBIDDEN_CHARS):
        bad = [c for c in _BASH_FORBIDDEN_CHARS if c in command]
        return f"❌ REJECTED: Forbidden characters detected: {bad}"

    try:
        args = shlex.split(command)
    except ValueError as e:
        return f"❌ PARSE ERROR: {e}"

    if not args:
        return "❌ REJECTED: Empty command."

    base = args[0]
    if base not in _BASH_WHITELIST:
        return (
            f"❌ REJECTED: '{base}' is not whitelisted.\n"
            f"  Allowed commands: {sorted(_BASH_WHITELIST)}\n"
            f"  Tip: use run_snippet() to run Python code."
        )

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = _truncate(result.stdout if result.returncode == 0 else result.stderr)
        label = "✅" if result.returncode == 0 else f"❌ Exit {result.returncode}"
        body = output.strip() if output.strip() else "(no output)"
        return f"{label}:\n{body}"

    except subprocess.TimeoutExpired:
        return f"⌛ TIMEOUT: Command exceeded {timeout}s."
    except Exception as e:
        return f"❌ ERROR: {type(e).__name__}: {e}"


def status() -> str:
    """Show code_executor capabilities, limits, and integration guide."""
    return "\n".join([
        "⚡ Code Executor — Trinity's Runtime Sandbox",
        "=" * 52,
        "",
        "Functions:",
        "  run_snippet(code, timeout=10)",
        "    → Run arbitrary Python in an isolated subprocess",
        "",
        "  test_skill(skill_name, fn_name, args_json='[]', timeout=15)",
        "    → Import a skill and call a function with test args",
        "    → Primary integration point with self_improvement",
        "",
        "  run_bash(command, timeout=10)",
        "    → Run a whitelisted bash command (no shell=True)",
        "",
        "  calc(expression)",
        "    → Evaluate a math expression instantly (no subprocess, no eval)",
        "    → Supports: + - * / // % ** () sqrt sin cos log pi e ...",
        "",
        "  status()",
        "    → This help message",
        "",
        "Security layers:",
        f"  • Blocked imports:  {sorted(_BLOCKED_IMPORTS)}",
        "  • Blocked calls:    os.system, os.popen, os.exec*, subprocess shell=True, __import__",
        f"  • Max output:       {MAX_OUTPUT} chars (protects LLM context)",
        f"  • Max timeout:      {MAX_TIMEOUT}s",
        "  • Isolation:        subprocess (never exec() in-process)",
        "  • Working dir:      /tmp (neutral, not /app/skills)",
        "",
        "Self-improvement integration loop:",
        "  1. self_improvement.audit('my_skill')           → find issues",
        "  2. self_improvement.fix('my_skill', type, line) → apply patch",
        "  3. code_executor.test_skill('my_skill', 'fn', '[\"arg\"]')  → verify it works",
        "  4. self_improvement.record_mistake(...) if test fails       → learn from it",
    ])


# ── Safe math evaluator ──────────────────────────────────────────────────────

_MATH_OPS = {
    ast.Add:      operator.add,
    ast.Sub:      operator.sub,
    ast.Mult:     operator.mul,
    ast.Div:      operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod:      operator.mod,
    ast.Pow:      operator.pow,
    ast.USub:     operator.neg,
    ast.UAdd:     operator.pos,
}

_MATH_FUNCS = {
    "abs": abs, "round": round,
    "sqrt": math.sqrt, "floor": math.floor, "ceil": math.ceil,
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "log": math.log, "log10": math.log10, "log2": math.log2,
    "exp": math.exp, "pow": math.pow,
    "pi": math.pi, "e": math.e,
}

def _eval_node(node):
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.Name) and node.id in _MATH_FUNCS:
        return _MATH_FUNCS[node.id]
    if isinstance(node, ast.BinOp) and type(node.op) in _MATH_OPS:
        return _MATH_OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _MATH_OPS:
        return _MATH_OPS[type(node.op)](_eval_node(node.operand))
    if isinstance(node, ast.Call):
        func = _eval_node(node.func)
        if not callable(func):
            raise ValueError(f"Not callable: {ast.dump(node.func)}")
        return func(*[_eval_node(a) for a in node.args])
    raise ValueError(f"Unsupported: {ast.dump(node)}")


def calc(expression: str) -> str:
    """
    Evaluate a math expression safely — no subprocess, no eval().
    Supports: +  -  *  /  //  %  **  ()
    Functions: sqrt, abs, round, floor, ceil, sin, cos, tan,
               log, log10, log2, exp, pow
    Constants: pi, e

    Args:
        expression: Math expression string, e.g. "sqrt(2) * pi" or "2**10"

    Returns:
        Result string.

    Example:
        <skill:code_executor.calc>sqrt(144) + 2**8</skill:code_executor.calc>
    """
    try:
        tree = ast.parse(expression.strip(), mode="eval")
        result = _eval_node(tree)
        # Show as int if the float has no fractional part
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        return f"✅ {expression.strip()} = {result}"
    except ZeroDivisionError:
        return "❌ Division by zero"
    except SyntaxError as e:
        return f"❌ Syntax error: {e.msg}"
    except ValueError as e:
        return f"❌ Unsupported operation: {e}"
    except Exception as e:
        return f"❌ Error: {type(e).__name__}: {e}"


# ── Export list (helps skill loader detect public functions) ─────────────────
__all__ = [
    "NAME",
    "DOC",
    "run_snippet",
    "test_skill",
    "run_bash",
    "calc",
    "status",
]
