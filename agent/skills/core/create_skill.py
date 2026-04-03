import os
import ast
import urllib.request
import urllib.error
from pathlib import Path

# Updated to match your filename
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
            tree = ast.parse(code)
            funcs = [
                node.name for node in ast.walk(tree)
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
        return (
            f"✅ SUCCESS: '{filename}' saved ({file_size} bytes). "
            f"Functions: [{func_list}]. "
            f"Example call: {call_hint}. "
            f"Reload: {reload_result}"
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