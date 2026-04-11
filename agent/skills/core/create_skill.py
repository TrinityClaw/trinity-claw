import os
import ast
import urllib.request
import urllib.error
from pathlib import Path

NAME = "create_skill"
SHORT_DOC = "Create and install new Python skills into /app/skills/dynamic/; reload all skills."
DOC = (
    "Create and install new Python skills into /app/skills/dynamic/. "
    "Returns: create_new_skill(skill_filename, code)→confirmation with the path where the file was saved, "
    "or error if the code fails AST validation; "
    "reload()→confirmation that all skills were reloaded and list of currently loaded skill names."
)

# Path mapping for Docker environment
SKILLS_DIR = Path("/app/skills/dynamic").resolve()

# Max chars for SHORT_DOC before it inflates the skills_doc line noticeably
_SHORT_DOC_LIMIT = 120
# Max chars for a function docstring before the tool schema entry becomes expensive
_FUNC_DOC_LIMIT = 80


def _audit_skill_tokens(skill_name: str, tree: ast.Module):
    """
    Audit a parsed skill AST for token hygiene.

    Returns (errors, warnings, token_report) where:
      errors   — list of blocking issues (skill must not be saved)
      warnings — list of non-blocking issues shown in the success message
      token_report — one-line cost summary shown in the success message
    """
    errors = []
    warnings = []

    # ── 1. SHORT_DOC required ─────────────────────────────────────────────────
    short_doc_value = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "SHORT_DOC" for t in node.targets)
        ):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                short_doc_value = node.value.value
            break

    if short_doc_value is None:
        errors.append(
            "Missing SHORT_DOC variable.\n"
            "Every skill MUST define SHORT_DOC at module level — it is injected into the\n"
            "system prompt on every single request.\n"
            "Rule: SHORT_DOC = one concise sentence, max 120 chars.\n"
            "Example: SHORT_DOC = \"Send and read emails via SMTP/IMAP.\""
        )
    elif len(short_doc_value) > _SHORT_DOC_LIMIT:
        warnings.append(
            f"SHORT_DOC is {len(short_doc_value)} chars (limit {_SHORT_DOC_LIMIT}). "
            "Trim it — it lands in every system prompt."
        )

    # ── 2. Function docstring length ──────────────────────────────────────────
    func_nodes = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")
    ]
    expensive_funcs = []
    for fn in func_nodes:
        doc = ast.get_docstring(fn) or ""
        if len(doc) > _FUNC_DOC_LIMIT:
            expensive_funcs.append((fn.name, len(doc)))

    if expensive_funcs:
        for fname, dlen in expensive_funcs:
            warnings.append(
                f"  {fname}() docstring is {dlen} chars (limit {_FUNC_DOC_LIMIT}). "
                "It gets copied verbatim into the tools schema JSON on every request."
            )

    # ── 3. Token cost estimate ────────────────────────────────────────────────
    # skills_doc line: "[SKILL: name] {short_doc} (functions: sig1, sig2)"
    func_sigs = ", ".join(
        f"{fn.name}({', '.join(a.arg for a in fn.args.args)})"
        for fn in func_nodes
    )
    short_doc_str = short_doc_value or ""
    skills_doc_line = f"[SKILL: {skill_name}] {short_doc_str} (functions: {func_sigs})"
    tk_skills_doc = max(1, len(skills_doc_line) // 4)

    # tools schema: per-function JSON overhead + name + description + ~20 tokens per param
    tk_tools = 0
    for fn in func_nodes:
        doc = ast.get_docstring(fn) or ""
        n_params = len([a for a in fn.args.args])
        tk_tools += max(1, (80 + len(skill_name) + len(fn.name) + len(doc) + 20 * n_params) // 4)

    tk_total = tk_skills_doc + tk_tools
    token_report = (
        f"Estimated token cost per request: ~{tk_total} tokens "
        f"(skills_doc line ~{tk_skills_doc}, tools schema ~{tk_tools} across {len(func_nodes)} function(s))"
    )

    return errors, warnings, token_report


def create_new_skill(skill_filename: str, code: str) -> str:
    """The only way Trinity can write new code to his own brain."""
    try:
        # 1. Path Lockdown — take only the first line and strip whitespace
        # (guards against LLM passing "name\ndoc string" as the filename arg)
        filename = os.path.basename(skill_filename.split('\n')[0].strip())
        if not filename.endswith(".py"):
            filename += ".py"
        
        file_path = (SKILLS_DIR / filename).resolve()
        
        # Security: Prevent path traversal (writing outside the sandbox)
        if not str(file_path).startswith(str(SKILLS_DIR)):
            return "❌ ACCESS DENIED: Cannot write outside /app/skills/dynamic"
        
        # 2. Pre-sanitize common LLM Unicode artifacts that break Python syntax
        _UNICODE_FIXES = [
            ("\u2014", "-"),   # em dash — → -
            ("\u2013", "-"),   # en dash – → -
            ("\u2018", "'"),   # left single quote ' → '
            ("\u2019", "'"),   # right single quote ' → '
            ("\u201c", '"'),   # left double quote " → "
            ("\u201d", '"'),   # right double quote " → "
            ("\u2026", "..."), # ellipsis … → ...
            ("\u00a0", " "),   # non-breaking space → regular space
        ]
        for bad_char, replacement in _UNICODE_FIXES:
            code = code.replace(bad_char, replacement)

        # 2b. Structural Code Audit
        try:
            print(f"🛠️ [DEBUG] create_skill.py: Parsing code (first 100 chars): {repr(code[:100])}")
            ast.parse(code)
        except SyntaxError as e:
            print(f"❌ [DEBUG] create_skill.py: SyntaxError: {e}")
            # Show the actual broken line to help the LLM self-correct
            lines = code.splitlines()
            bad_line = lines[e.lineno - 1].strip() if e.lineno and e.lineno <= len(lines) else "(unknown)"
            return (
                f"❌ SYNTAX ERROR on line {e.lineno}: {e.msg}\n"
                f"   Offending line: {bad_line}\n"
                f"   Fix this specific line and resubmit the corrected code."
            )

        # 2b. Dangerous Import Blocklist
        _BLOCKED_IMPORTS = {
            "subprocess", "ctypes", "cffi", "socket", "multiprocessing",
            "pty", "tty", "termios", "fcntl", "signal", "mmap",
            "pickle", "marshal", "shelve",
            "sys",      # sys.exit() crashes agent; sys.modules allows hijacking
            "shutil",   # shutil.rmtree() can wipe /app/memory
        }
        # Text patterns blocked regardless of import aliasing
        # NOTE: os is NOT fully blocked — os.path.* is legitimate.
        #       Only the dangerous os sub-APIs are blocked below.
        _BLOCKED_PATTERNS = [
            ("__import__",            "__import__() call"),
            ("importlib.import_module", "dynamic importlib import"),
            ("os.system",             "os.system() call"),
            ("os.popen",              "os.popen() call"),
            ("os.exec",               "os.exec*() call"),
            ("os.environ",            "os.environ access (exposes secrets)"),
            ("os.remove",             "os.remove() — use the files skill instead"),
            ("os.unlink",             "os.unlink() — use the files skill instead"),
            ("os.rmdir",              "os.rmdir() — use the files skill instead"),
            ("os.chmod",              "os.chmod() permission change"),
            ("os.chown",              "os.chown() ownership change"),
        ]
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                # Block dangerous imports
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root = alias.name.split(".")[0]
                        if root in _BLOCKED_IMPORTS:
                            return f"❌ BLOCKED: Import of '{alias.name}' is not allowed in dynamic skills."
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        root = node.module.split(".")[0]
                        if root in _BLOCKED_IMPORTS:
                            return f"❌ BLOCKED: Import of '{node.module}' is not allowed in dynamic skills."
                # AST-exact check for eval/exec builtins (avoids false positives on execute(), etc.)
                elif isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Name) and func.id in ("eval", "exec"):
                        return f"❌ BLOCKED: '{func.id}()' builtin is not allowed in dynamic skills."
        except SyntaxError:
            pass  # Already caught above

        # Block dangerous os sub-APIs by source text scan
        for pattern, label in _BLOCKED_PATTERNS:
            if pattern in code:
                return f"❌ BLOCKED: '{label}' is not allowed in dynamic skills."

        # 2c. Token hygiene audit — runs on the already-parsed tree from step 2b
        _audit_tree = ast.parse(code)
        _skill_stem = os.path.splitext(os.path.basename(skill_filename.split('\n')[0].strip()))[0]
        _audit_errors, _audit_warnings, _token_report = _audit_skill_tokens(_skill_stem, _audit_tree)
        if _audit_errors:
            return "❌ TOKEN HYGIENE REJECTED:\n" + "\n\n".join(_audit_errors)

        # 3. Protect System Integrity — all core skills are read-only
        protected = {
            "__init__.py",
            "create_skill.py",
            "code_executor.py",
            "dashboard.py",
            "data_science.py",
            "document_parser.py",
            "email_sender.py",
            "files.py",
            "git_manager.py",
            "image_viewer.py",
            "notes.py",
            "terminal.py",
            "scheduler.py",
            "self_improvement.py",
            "sys.py",
            "telegram_bot.py",
            "url_monitor.py",
            "web.py",
            "web_builder.py",
        }
        if filename in protected:
            return f"❌ ACCESS DENIED: '{filename}' is a core system file and cannot be modified."

        # 4. Ensure Directory Exists
        SKILLS_DIR.mkdir(parents=True, exist_ok=True)
        
        # 5. Physical Write & Verification
        file_path.write_text(code, encoding='utf-8')

        # --- THE REALITY CHECK ---
        if not file_path.exists():
            return f"❌ WRITE FAILURE: File vanished after writing to {file_path}. Check Docker mounts."

        file_size = file_path.stat().st_size

        # 6. Extract public function names so the agent knows what to call
        skill_name = filename[:-3]
        funcs = []
        try:
            _final_tree = ast.parse(code)
            funcs = [
                node.name for node in ast.walk(_final_tree)
                if isinstance(node, ast.FunctionDef) and not node.name.startswith('_')
            ]
        except Exception:
            pass

        # 7. Auto-reload so the skill is immediately usable (no separate reload step)
        reload_result = reload()

        func_list = ", ".join(funcs) if funcs else "none detected"
        call_hint = (
            f"<skill:{skill_name}.{funcs[0]}>args</skill:{skill_name}.{funcs[0]}>"
            if funcs else f"<skill:{skill_name}.FUNCTION_NAME></skill:{skill_name}.FUNCTION_NAME>"
        )
        _warn_block = (
            "\n⚠️ Token warnings:\n" + "\n".join(_audit_warnings)
            if _audit_warnings else ""
        )
        return (
            f"✅ SUCCESS: '{filename}' saved ({file_size} bytes). "
            f"Functions: [{func_list}]. "
            f"Example call: {call_hint}. "
            f"Reload: {reload_result}\n"
            f"📊 {_token_report}{_warn_block}"
        )

    except Exception as e:
        return f"❌ SYSTEM ERROR: {str(e)}"


def reload() -> str:
    """Reload all skills. Call this when the user asks to activate a skill they moved to core/."""
    try:
        req = urllib.request.Request(
            "http://localhost:8001/skills/reload",
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return "✅ Skills reloaded — all skills in core/ and dynamic/ are now active."
    except urllib.error.URLError as e:
        return f"⚠️ Could not reach reload endpoint: {e.reason}. Ask user to restart the agent container."
    except Exception as e:
        return f"⚠️ Reload failed: {str(e)}"