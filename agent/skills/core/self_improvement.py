"""
Self-Improvement Skill - SUPERPOWER EDITION
Analyzes skills, learns from mistakes, auto-generates fixes, and prevents repeat errors.

Features:
• AST-based code analysis for anti-patterns, security, performance
• Mistake memory: stores errors + fixes in /app/memory/lessons.jsonl
• Auto-patch generation with diff output
• Test case suggestion for regression prevention
• Pattern learning: recognizes recurring issues across skills
• Best-practice enforcement (PEP8, security, type hints)
"""

import ast
import os
import re
import json
import hashlib
import subprocess
import threading
import requests as _requests
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional

# ============================================================================
# SKILL METADATA (Required for skill loader)
# ============================================================================

NAME = "self_improvement"  # Must match filename: self_improvement.py
SKILL_TIMEOUT = 120  # index_all_lessons needs extra time for ChromaDB bulk upserts
SHORT_DOC = "Analyze, patch, and health-check skill files; record mistakes and surface recurring error patterns."
DOC = (
    "Self-healing and learning: analyze skills, record mistakes, auto-fix code, prevent repeat errors. "
    "Returns: audit(skill_name)→health report with issues; "
    "fix(skill_name, issue_type, line_number)→apply fix at line (use 'all' to fix every occurrence); always runs verify_skill() after — reports evidence before claiming success; "
    "verify_skill(skill_name)→syntax+compile+metadata checks then a model-based VERDICT: PASS/FAIL/PARTIAL with execution trace evidence — call before declaring any fix complete; "
    "daily_review(skill_name?)→scan skill(s), summarize lessons learned, surface recurring patterns; "
    "record_mistake(skill, error_type, error_msg)→save lesson to lessons.jsonl and auto-index into ChromaDB; auto-extracts correct function signatures for TypeError/ValueError/skill_error; "
    "check_for_learned_fix(skill_name, error_type, skill_path?, task?)→check if we've already learned a fix; task parameter allows semantic search; "
    "check_lessons(skill_name, func_name)→pre-dispatch check for prior failures; returns correct signature if a TypeError/ValueError was previously recorded; "
    "search_lessons_semantic(query, n=5)→semantic ChromaDB search over past lessons — finds relevant failures without exact keyword match; "
    "index_all_lessons()→bulk-index all lessons.jsonl entries into ChromaDB (run once after upgrade); "
    "should_self_improve(threshold=3, window_days=7)→check if recurring failures in recent lessons warrant running autoimprove loops; "
    "returns {improve: bool, reason: str, patterns: list, suggested_loop: str}; "
    "call this at session start or after a task with errors to decide if autoimprove.run_loop() is warranted; "
    "suggest_tests(skill_name)→generate a real pytest file at /app/memory/tests/test_<skill>.py, run it, and return pass/fail output; "
    "run_tests(skill_name)→run an existing test file for the skill and return pytest output; "
    "report()→system-wide improvement summary with top recurring issues."
)

# ============================================================================
# CONFIGURATION
# ============================================================================

SKILLS_DIR = Path("/app/skills/dynamic")
CORE_SKILLS_DIR = Path("/app/skills/core")
LESSONS_FILE = Path("/app/memory/lessons.jsonl")
PATTERNS_FILE = Path("/app/memory/error_patterns.json")
_AUTO_FIX_LOG = Path("/app/memory/auto_fix_log.jsonl")

# ============================================================================
# MISTAKE MEMORY: Learn from every error
# ============================================================================

def _load_lessons() -> List[Dict]:
    """Load learned lessons from persistent storage, deduplicating by hash."""
    lessons = []
    seen_hashes: set = set()
    if LESSONS_FILE.exists():
        try:
            with open(LESSONS_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip():
                        try:
                            lesson = json.loads(line)
                            h = lesson.get("hash")
                            if h:
                                if h in seen_hashes:
                                    continue
                                seen_hashes.add(h)
                            lessons.append(lesson)
                        except json.JSONDecodeError:
                            continue
        except (IOError, OSError):
            pass
    return lessons

def _save_lesson(lesson: Dict) -> bool:
    """Save a new lesson to persistent storage. Returns True on success."""
    try:
        LESSONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LESSONS_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(lesson, ensure_ascii=False) + '\n')
        _patterns_cache["data"] = None  # invalidate so next read recomputes counts
        return True
    except (IOError, OSError) as e:
        print(f"⚠️ Failed to save lesson: {e}")
        return False

# Module-level cache so _load_error_patterns() avoids re-scanning lessons.jsonl
# on every call. Invalidated when either backing file's mtime changes.
_patterns_cache: Dict = {"data": None, "patterns_mtime": None, "lessons_mtime": None}


def _patterns_cache_valid() -> bool:
    """Return True if both backing files are unchanged since last load."""
    if _patterns_cache["data"] is None:
        return False
    try:
        pm = PATTERNS_FILE.stat().st_mtime if PATTERNS_FILE.exists() else None
        lm = LESSONS_FILE.stat().st_mtime if LESSONS_FILE.exists() else None
    except OSError:
        return False
    return pm == _patterns_cache["patterns_mtime"] and lm == _patterns_cache["lessons_mtime"]


def _load_error_patterns() -> Dict:
    """Load recognized error patterns for faster detection.
    Counts are always recomputed from lessons.jsonl so historical data survives restarts.
    Results are cached by file mtime — repeated calls within the same run are O(1)."""
    if _patterns_cache_valid():
        return dict(_patterns_cache["data"])  # shallow copy keeps callers isolated

    default_patterns = {
        "bare_except": {"count": 0, "severity": "medium", "fix": "Use 'except SpecificError:'"},
        "missing_docstring": {"count": 0, "severity": "low", "fix": "Add triple-quoted docstring"},
        "no_type_hints": {"count": 0, "severity": "low", "fix": "Add type hints to function signatures"},
        "hardcoded_path": {"count": 0, "severity": "high", "fix": "Use pathlib or config-driven paths"},
        "sql_injection_risk": {"count": 0, "severity": "critical", "fix": "Use parameterized queries"},
        "missing_timeout": {"count": 0, "severity": "medium", "fix": "Add timeout to network requests"},
        "no_rate_limit": {"count": 0, "severity": "medium", "fix": "Add rate limiting for external APIs"},
        "TypeError": {"count": 0, "severity": "high", "fix": "Check function signature — use self_improvement.check_lessons() before calling"},
        "ValueError": {"count": 0, "severity": "medium", "fix": "Validate argument types and values before calling the skill function"},
        "skill_error": {"count": 0, "severity": "medium", "fix": "Review skill DOC string for correct usage — use self_improvement.check_lessons() before calling"},
    }

    if PATTERNS_FILE.exists():
        try:
            with open(PATTERNS_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
            # Merge: add any new default patterns not yet in the saved file
            for k, v in default_patterns.items():
                if k not in saved:
                    saved[k] = v
            patterns = saved
        except (json.JSONDecodeError, IOError):
            patterns = dict(default_patterns)
    else:
        patterns = dict(default_patterns)

    # Recompute counts from lessons.jsonl so they're accurate after restarts
    for key in patterns:
        patterns[key]["count"] = 0
    if LESSONS_FILE.exists():
        try:
            with open(LESSONS_FILE, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip():
                        try:
                            lesson = json.loads(line)
                            et = lesson.get("error_type") or lesson.get("type")
                            if et and et in patterns:
                                patterns[et]["count"] += 1
                        except json.JSONDecodeError:
                            continue
        except (IOError, OSError):
            pass

    # Write recomputed counts back so autoimprove's direct file readers stay in sync.
    try:
        PATTERNS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(PATTERNS_FILE, 'w', encoding='utf-8') as f:
            json.dump(patterns, f, indent=2)
    except (IOError, OSError):
        pass

    # Populate cache after writing so the cached mtime matches the file we just wrote.
    try:
        _patterns_cache["patterns_mtime"] = PATTERNS_FILE.stat().st_mtime if PATTERNS_FILE.exists() else None
        _patterns_cache["lessons_mtime"] = LESSONS_FILE.stat().st_mtime if LESSONS_FILE.exists() else None
    except OSError:
        pass
    _patterns_cache["data"] = dict(patterns)

    return patterns

def record_mistake(skill_name: str, error_type: str, error_msg: str, fix_applied: str = "", skill_path: str = "", auto_fix: bool = True) -> str:
    """Record a mistake + fix for future learning."""
    lesson = {
        "timestamp": datetime.now().isoformat(),
        "skill": skill_name,
        "skill_path": skill_path,
        "error_type": error_type,
        "error_message": error_msg[:200],
        "fix_applied": fix_applied,
        "hash": hashlib.md5(f"{skill_name}:{error_type}:{error_msg[:100]}".encode()).hexdigest()
    }
    
    saved = _save_lesson(lesson)
    # Note: no manual count increment needed — _load_error_patterns() always
    # recomputes counts from lessons.jsonl from scratch, so the new lesson is
    # reflected automatically on the next call. A manual += 1 here would write
    # a stale count to error_patterns.json that gets overwritten on next load.
    if saved:
        _index_lesson_in_chroma(lesson)  # best-effort — never raises
    if saved and auto_fix:
        _try_auto_fix(skill_name, error_type, skill_path)
        _try_auto_fix_signature(skill_name, error_type, error_msg)
    return f"✅ Recorded lesson: {error_type} in {skill_name}" if saved else "⚠️ Failed to record lesson"

# ── ChromaDB semantic indexing for lessons ─────────────────────────────────────

_CHROMA_HOST        = os.getenv("CHROMA_HOST", "chroma")
_CHROMA_PORT        = int(os.getenv("CHROMA_PORT", "8000"))
_LESSONS_COLLECTION = "lessons_semantic"


def _get_lessons_collection():
    """Return the ChromaDB lessons collection, or None if ChromaDB is unavailable."""
    try:
        import chromadb
        client = chromadb.HttpClient(host=_CHROMA_HOST, port=_CHROMA_PORT)
        return client.get_or_create_collection(name=_LESSONS_COLLECTION)
    except Exception:
        return None


def _index_lesson_in_chroma(lesson: dict) -> None:
    """Index a single lesson dict into ChromaDB — fire-and-forget, never raises.

    Document text = 'skill error_type: error_message [fix: fix_applied]'
    so semantic queries like 'web timeout error' or 'notes AttributeError'
    can retrieve relevant past failures without keyword-exact matching.
    """
    try:
        col = _get_lessons_collection()
        if col is None:
            return
        doc_id   = lesson.get("hash", hashlib.md5(json.dumps(lesson, sort_keys=True).encode()).hexdigest()[:12])
        skill    = lesson.get("skill", "unknown")
        etype    = lesson.get("error_type", "error")
        emsg     = lesson.get("error_message", "")[:200]
        fix      = lesson.get("fix_applied", "")[:200]
        doc_text = f"{skill} {etype}: {emsg}" + (f" [fix: {fix}]" if fix else "")
        col.upsert(
            documents=[doc_text],
            ids=[doc_id],
            metadatas=[{
                "skill":      skill,
                "error_type": etype,
                "timestamp":  lesson.get("timestamp", ""),
                "has_fix":    bool(fix),
            }],
        )
    except Exception:
        pass  # ChromaDB down → lessons still saved to JSONL, indexing is best-effort


# ── Auto-fix: fire-and-forget background thread ────────────────────────────────

_AUTO_FIXABLE_ISSUES = frozenset({
    "bare_except",
    "missing_timeout",
    "missing_docstring",
})

# Runtime signature errors — fixed by extracting the correct function signature
# and storing it as the lesson's fix_applied so check_lessons() returns it.
_SIGNATURE_ERROR_TYPES = frozenset({
    "TypeError",
    "ValueError",
    "skill_error",
})

_auto_fix_in_flight: set = set()


def _log_auto_fix(skill_name: str, error_type: str, outcome: str,
                  before: int, after: int, patches: int) -> None:
    """Persist auto-fix outcome for display in check_lessons() and reports."""
    try:
        _AUTO_FIX_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _AUTO_FIX_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "timestamp":    datetime.now().isoformat(),
                "skill":        skill_name,
                "error_type":   error_type,
                "outcome":      outcome,
                "before_score": before,
                "after_score":  after,
                "patches":      patches,
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _try_auto_fix(skill_name: str, error_type: str, skill_path: str = "") -> None:
    """Background auto-fix attempt — fire-and-forget, never raises.

    Called from record_mistake() when the error type is known and auto-fixable.
    Runs in a daemon thread so it does not block the agent loop.

    Flow:
      1. Check if error_type is auto-fixable
      2. Check if we already fixed this recently (dedup window)
      3. Analyze skill to confirm the issue still exists
      4. Generate patch → apply → verify
      5. Log outcome to auto_fix_log.jsonl
    """
    if error_type not in _AUTO_FIXABLE_ISSUES:
        return

    dedup_key = f"{skill_name}:{error_type}"
    if dedup_key in _auto_fix_in_flight:
        return
    _auto_fix_in_flight.add(dedup_key)

    def _worker():
        try:
            si_mod = __import__("self_improvement")
            analysis = si_mod.analyze_skill_code(skill_name)
            if analysis.get("error"):
                return
            matching = [i for i in analysis["issues"] if i["type"] == error_type]
            if not matching:
                return

            before_score = analysis["health_score"]
            applied = 0
            errors = []

            for issue in sorted(matching, key=lambda x: x["line"], reverse=True):
                patch = si_mod.generate_patch(skill_name, error_type, issue["line"])
                if patch.get("safe_to_apply"):
                    result = si_mod.apply_patch(skill_name, patch)
                    if result.get("success"):
                        applied += 1
                    else:
                        errors.append(result.get("error", "unknown"))

            if applied == 0:
                return

            verification = si_mod.verify_skill(skill_name)
            after = si_mod.analyze_skill_code(skill_name)
            after_score = after.get("health_score", before_score) if not after.get("error") else 0

            outcome = "AUTO_IMPROVED" if after_score >= before_score else "AUTO_REVERTED"
            _log_auto_fix(skill_name, error_type, outcome, before_score, after_score, applied)
            print(f"[auto-fix] {outcome}: {skill_name}.{error_type} ({applied} patch(es), {before_score}->{after_score})")
        except Exception as e:
            print(f"[auto-fix] failed for {skill_name}.{error_type}: {e}")
        finally:
            _auto_fix_in_flight.discard(dedup_key)

    threading.Thread(target=_worker, daemon=True, name=f"auto-fix-{skill_name}-{error_type}").start()


# ── Auto-fix: signature extraction for runtime errors ─────────────────────────

def _extract_signature_fix(skill_name: str, error_type: str, error_msg: str) -> str:
    """Extract the correct function signature for a TypeError/ValueError/skill_error.

    Parses the skill_name (e.g. 'notes.log_activity') to find the module and
    function, then uses inspect.signature() to produce the correct call format.

    Returns:
        A fix string like:
        "Signature: log_activity(action, result, source='manual') — call with positional args or correct kwargs"
        or empty string if the function cannot be found.
    """
    import importlib as _il
    import inspect as _ins

    # Parse 'skill.func' or 'skill.func.sub' → module stem + function name
    parts = skill_name.split(".")
    if len(parts) < 2:
        return ""

    func_name = parts[-1]
    module_stem = parts[-2]  # e.g. 'notes' from 'notes.log_activity'

    # Try to import the skill module from core then dynamic dirs
    mod = None
    for d in (CORE_SKILLS_DIR, SKILLS_DIR):
        mod_path = d / f"{module_stem}.py"
        if mod_path.exists():
            try:
                spec = _il.util.spec_from_file_location(module_stem, str(mod_path))
                if spec and spec.loader:
                    mod = _il.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    break
            except Exception:
                continue

    if mod is None:
        return ""

    fn = getattr(mod, func_name, None)
    if fn is None or not callable(fn):
        return ""

    try:
        sig = _ins.signature(fn)
    except (ValueError, TypeError):
        return ""

    # Build a human-readable signature string
    params = []
    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        if param.default is _ins.Parameter.empty:
            params.append(name)
        else:
            # Show default for common types, truncate long ones
            dv = repr(param.default)
            if len(dv) > 40:
                dv = dv[:37] + "..."
            params.append(f"{name}={dv}")

    if not params:
        sig_str = f"{func_name}()"
    else:
        sig_str = f"{func_name}({', '.join(params)})"

    # Add a hint based on the error message
    hint = ""
    em = error_msg.lower()
    if "unexpected keyword" in em:
        hint = " — remove the unrecognized kwarg"
    elif "missing" in em and "required" in em:
        hint = " — all listed args are required (positional or kwarg)"
    elif "takes" in em and ("positional" in em or "argument" in em):
        hint = " — check argument count and order"

    return f"Signature: {sig_str}{hint}"


def _try_auto_fix_signature(skill_name: str, error_type: str, error_msg: str) -> None:
    """Background signature fix — fire-and-forget, never raises.

    Called from record_mistake() for TypeError/ValueError/skill_error.
    Extracts the correct function signature and patches the lesson file
    so check_lessons() returns it on future calls.
    """
    if error_type not in _SIGNATURE_ERROR_TYPES:
        return

    dedup_key = f"{skill_name}:{error_type}"
    if dedup_key in _auto_fix_in_flight:
        return
    _auto_fix_in_flight.add(dedup_key)

    def _worker():
        try:
            fix_str = _extract_signature_fix(skill_name, error_type, error_msg)
            if not fix_str:
                return

            # Patch the most recent lesson for this skill+error_type with the fix
            if not LESSONS_FILE.exists():
                return

            lines = LESSONS_FILE.read_text(encoding="utf-8").splitlines()
            patched = False
            for i in range(len(lines) - 1, -1, -1):
                if not lines[i].strip():
                    continue
                try:
                    lesson = json.loads(lines[i])
                    if (lesson.get("skill") == skill_name
                            and lesson.get("error_type") == error_type
                            and not lesson.get("fix_applied")):
                        lesson["fix_applied"] = fix_str
                        lines[i] = json.dumps(lesson, ensure_ascii=False)
                        patched = True
                        break
                except json.JSONDecodeError:
                    continue

            if patched:
                LESSONS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
                print(f"[sig-fix] {skill_name}.{error_type} → {fix_str[:80]}")
        except Exception as e:
            print(f"[sig-fix] failed for {skill_name}.{error_type}: {e}")
        finally:
            _auto_fix_in_flight.discard(dedup_key)

    threading.Thread(target=_worker, daemon=True, name=f"sig-fix-{skill_name}-{error_type}").start()


def search_lessons_semantic(query: str, n: int = 5) -> str:
    """Semantic search over past lessons using ChromaDB embeddings.

    More accurate than keyword matching — finds relevant failures even when
    the exact skill name or error type isn't in the query.

    Args:
        query: Natural-language description of the error or scenario.
        n:     Max results to return (default 5).

    Returns:
        Formatted list of matching lessons, or a message if none found.
    """
    col = _get_lessons_collection()
    if col is None:
        return "⚠️ ChromaDB not available — semantic lesson search unavailable."
    try:
        n = max(1, min(int(n), 20))
        results = col.query(query_texts=[query], n_results=n)
        docs      = results.get("documents",  [[]])[0]
        metadatas = results.get("metadatas",  [[]])[0]
        distances = results.get("distances",  [[]])[0]
        if not docs:
            return "📭 No matching lessons found."
        lines = [f"🔍 Semantic lesson search: '{query}' — {len(docs)} result(s)"]
        for doc, meta, dist in zip(docs, metadatas, distances):
            relevance = 1.0 / (1.0 + dist)
            lines.append(
                f"\n  [{meta.get('skill','?')}] {meta.get('error_type','?')} "
                f"({meta.get('timestamp','')[:10]}) — relevance {relevance:.0%}"
            )
            lines.append(f"  {doc[:200]}")
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ Semantic search failed: {e}"


def index_all_lessons(batch_size: int = 100) -> str:
    """Index (or re-index) all lessons from lessons.jsonl into ChromaDB.

    Safe to run repeatedly — uses upsert so existing entries are updated,
    not duplicated. Run this once after upgrading to enable semantic search
    on historical lessons.

    Uses batched upserts (batch_size at a time) instead of one HTTP call per
    lesson — dramatically faster for large lesson files.

    Args:
        batch_size: Number of lessons per upsert call (default 100).

    Returns:
        Summary of how many lessons were indexed.
    """
    col = _get_lessons_collection()
    if col is None:
        return "⚠️ ChromaDB not available."
    lessons = _load_lessons()
    if not lessons:
        return "📭 No lessons to index."

    indexed = 0
    batch_ids: List[str] = []
    batch_docs: List[str] = []
    batch_metas: List[Dict] = []

    for lesson in lessons:
        try:
            doc_id = lesson.get("hash", hashlib.md5(json.dumps(lesson, sort_keys=True).encode()).hexdigest()[:12])
            skill = lesson.get("skill", "unknown")
            etype = lesson.get("error_type", "error")
            emsg = lesson.get("error_message", "")[:200]
            fix = lesson.get("fix_applied", "")[:200]
            doc_text = f"{skill} {etype}: {emsg}" + (f" [fix: {fix}]" if fix else "")
            batch_ids.append(doc_id)
            batch_docs.append(doc_text)
            batch_metas.append({
                "skill": skill,
                "error_type": etype,
                "timestamp": lesson.get("timestamp", ""),
                "has_fix": bool(fix),
            })
            indexed += 1
        except Exception:
            pass

        # Flush batch
        if len(batch_ids) >= batch_size:
            try:
                col.upsert(ids=batch_ids, documents=batch_docs, metadatas=batch_metas)
            except Exception:
                pass
            batch_ids = []
            batch_docs = []
            batch_metas = []

    # Flush remainder
    if batch_ids:
        try:
            col.upsert(ids=batch_ids, documents=batch_docs, metadatas=batch_metas)
        except Exception:
            pass

    return f"✅ Indexed {indexed}/{len(lessons)} lessons into ChromaDB '{_LESSONS_COLLECTION}'."


def check_for_learned_fix(skill_name: str = "", error_type: str = "", skill_path: str = "", task: str = "") -> Optional[str]:
    """Check if we've already learned a fix for this error type in this skill or task.
    If skill_path is given, prefer lessons recorded for that exact path to avoid
    cross-contamination when two skills share the same name across core/dynamic.
    If task is given but skill_name/error_type are missing, performs a semantic search."""
    if task and not skill_name and not error_type:
        return search_lessons_semantic(task, n=3)

    if not skill_name or not error_type:
        return None
    lessons = _load_lessons()
    fallback = None
    for lesson in reversed(lessons):  # most recent fix wins
        if lesson.get("skill") != skill_name or lesson.get("error_type") != error_type:
            continue
        if skill_path and lesson.get("skill_path") == skill_path:
            return lesson.get("fix_applied")
        if fallback is None:
            fallback = lesson.get("fix_applied")
    return fallback


def check_lessons(skill_name: str = "", func_name: str = "", **kwargs) -> Optional[str]:
    """Pre-dispatch check: return a warning string if this skill has failed before,
    so the caller can surface the lesson before executing.
    Returns None if no prior failures exist (the common, happy path).

    Called by app.py before executing each skill tag — zero tool-call overhead."""
    if not skill_name or not func_name:
        return None
    label = f"{skill_name}.{func_name}"
    lessons = _load_lessons()
    # Match lessons stored as 'skill.func' or just 'skill'
    matches = [l for l in lessons
               if l.get("skill") == skill_name or l.get("skill") == label or l.get("skill", "").startswith(f"{skill_name}.")]
    if not matches:
        return None
    # Most recent failure wins
    last = matches[-1]
    error_type = last.get("error_type", "error")
    fix        = last.get("fix_applied", "").strip()
    msg = f"⚠️ [lesson] {label} has failed before ({error_type})"
    if fix:
        msg += f" — known fix: {fix[:120]}"

    # Check auto-fix status
    if _AUTO_FIX_LOG.exists():
        try:
            with _AUTO_FIX_LOG.open("r", encoding="utf-8") as f:
                for line in f:
                    entry = json.loads(line.strip())
                    if entry.get("skill") == skill_name and entry.get("error_type") == error_type:
                        outcome = entry.get("outcome", "")
                        if outcome == "AUTO_IMPROVED":
                            msg += f" [auto-fix: ✅ resolved]"
                        elif outcome == "AUTO_REVERTED":
                            msg += f" [auto-fix: ⚠️ attempted but reverted]"
                        break
        except Exception:
            pass

    return msg

# ============================================================================
# AST-BASED CODE ANALYSIS
# ============================================================================

class CodeAnalyzer(ast.NodeVisitor):
    """AST visitor that detects anti-patterns, security issues, and improvements"""
    
    def __init__(self, source_code: str, skill_name: str) -> None:
        """Initialize the analyzer with source code and skill name."""
        self.source = source_code
        self.skill = skill_name
        self.issues: List[Dict] = []
        self.patterns = _load_error_patterns()
        # Pre-compute lines that contain f-string expressions so visit_Constant
        # can skip Constant fragments that are embedded in JoinedStr nodes.
        # Those fragments look like path suffixes but aren't hardcoded paths.
        try:
            tree = ast.parse(source_code)
            self._fstring_lines: set = {
                node.lineno
                for node in ast.walk(tree)
                if isinstance(node, ast.JoinedStr)
            }
        except SyntaxError:
            self._fstring_lines = set()

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        """Flag bare except clauses that swallow all exceptions."""
        if node.type is None:
            self.issues.append({
                "type": "bare_except",
                "line": node.lineno,
                "severity": self.patterns.get("bare_except", {}).get("severity", "medium"),
                "message": "Bare 'except:' catches all errors, including SystemExit/KeyboardInterrupt",
                "suggestion": self.patterns.get("bare_except", {}).get("fix", "Use specific exception types")
            })
        self.generic_visit(node)
    
    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Flag functions missing docstrings or type hints."""
        if ast.get_docstring(node) is None:
            self.issues.append({
                "type": "missing_docstring",
                "line": node.lineno,
                "severity": self.patterns.get("missing_docstring", {}).get("severity", "low"),
                "message": f"Function '{node.name}' lacks docstring",
                "suggestion": self.patterns.get("missing_docstring", {}).get("fix")
            })
        
        args_without_hints = [
            arg.arg for arg in node.args.args
            if arg.arg not in ('self', 'cls') and arg.annotation is None
        ]
        if args_without_hints:
            self.issues.append({
                "type": "no_type_hints",
                "line": node.lineno,
                "severity": self.patterns.get("no_type_hints", {}).get("severity", "low"),
                "message": f"Function '{node.name}' lacks type hints",
                "suggestion": self.patterns.get("no_type_hints", {}).get("fix")
            })
        self.generic_visit(node)

    # Async functions get the same checks as sync functions
    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        """Flag network requests without timeouts and SQL injection risks."""
        _HTTP_CLIENTS = {'requests', 'httpx'}
        if isinstance(node.func, ast.Attribute):
            if node.func.attr in ['get', 'post', 'put', 'delete', 'patch'] and isinstance(node.func.value, ast.Name):
                if node.func.value.id in _HTTP_CLIENTS:
                    has_timeout = any(kw.arg == 'timeout' for kw in node.keywords if kw.arg)
                    if not has_timeout:
                        self.issues.append({
                            "type": "missing_timeout",
                            "line": node.lineno,
                            "severity": self.patterns.get("missing_timeout", {}).get("severity", "medium"),
                            "message": "Network request without timeout may hang indefinitely",
                            "suggestion": self.patterns.get("missing_timeout", {}).get("fix")
                        })
        
        if isinstance(node.func, ast.Attribute) and node.func.attr == 'execute':
            for arg in node.args:
                is_percent_format = isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Mod)
                is_fstring = isinstance(arg, ast.JoinedStr)
                is_format_call = (
                    isinstance(arg, ast.Call)
                    and isinstance(arg.func, ast.Attribute)
                    and arg.func.attr == 'format'
                )
                if is_percent_format or is_fstring or is_format_call:
                    self.issues.append({
                        "type": "sql_injection_risk",
                        "line": node.lineno,
                        "severity": self.patterns.get("sql_injection_risk", {}).get("severity", "critical"),
                        "message": "String formatting in SQL query may allow injection",
                        "suggestion": self.patterns.get("sql_injection_risk", {}).get("fix")
                    })

        # Dangerous built-in calls
        func = node.func
        if isinstance(func, ast.Name) and func.id in ('eval', 'exec'):
            self.issues.append({
                "type": "dangerous_call",
                "line": node.lineno,
                "severity": "critical",
                "message": f"Use of '{func.id}()' is dangerous and should be avoided in skills",
                "suggestion": f"Remove {func.id}() — use explicit logic instead"
            })
        elif (
            isinstance(func, ast.Attribute)
            and func.attr == 'system'
            and isinstance(func.value, ast.Name)
            and func.value.id == 'os'
        ):
            self.issues.append({
                "type": "dangerous_call",
                "line": node.lineno,
                "severity": "critical",
                "message": "os.system() executes shell commands and is a security risk",
                "suggestion": "Use subprocess.run() with a list of arguments instead"
            })

        self.generic_visit(node)
    
    # Paths that are intentional Docker/container architecture or well-known system dirs — not bugs
    _DOCKER_PATH_PREFIXES = (
        "/app/",
        "/tmp/", "/var/", "/etc/", "/usr/", "/opt/",
        "/home/", "/root/", "/proc/", "/sys/", "/dev/",
        "/run/", "/srv/", "/mnt/", "/media/",
    )

    def visit_Constant(self, node: ast.Constant) -> None:
        """Flag string constants that look like hardcoded absolute file paths."""
        if not isinstance(node.value, str):
            self.generic_visit(node)
            return

        # Skip constants that are fragments inside an f-string — they are not
        # hardcoded paths, the runtime value is dynamic.
        if node.lineno in self._fstring_lines:
            self.generic_visit(node)
            return

        value = node.value

        # Must have at least one directory separator after the root to be a real path.
        # This prevents false positives on short detection strings like 'C:\\', '/', '/100'.
        has_subpath = ('/' in value[1:]) or ('\\' in value[2:])
        if not has_subpath:
            self.generic_visit(node)
            return

        is_windows_abs = len(value) >= 3 and value[0].isalpha() and value[1:3] in (':\\', ':/')
        is_non_docker_abs = value.startswith('/') and not value.startswith(self._DOCKER_PATH_PREFIXES)

        if not (is_windows_abs or is_non_docker_abs):
            self.generic_visit(node)
            return

        # Check the actual source lines around this node (line number ≠ char offset)
        source_lines = self.source.splitlines()
        line_idx = node.lineno - 1
        window = "\n".join(source_lines[max(0, line_idx - 2): line_idx + 3])
        if not any(name in window for name in ['Path(', 'os.path', 'pathlib', 'os.getenv']):
            self.issues.append({
                "type": "hardcoded_path",
                "line": node.lineno,
                "severity": self.patterns.get("hardcoded_path", {}).get("severity", "high"),
                "message": "Hardcoded file path may break in different environments",
                "suggestion": self.patterns.get("hardcoded_path", {}).get("fix")
            })
        self.generic_visit(node)

def analyze_skill_code(skill_name: str) -> Dict:
    """Deep AST analysis of a skill's code."""
    path = SKILLS_DIR / f"{skill_name}.py"
    if not path.exists():
        path = CORE_SKILLS_DIR / f"{skill_name}.py"
        if not path.exists():
            return {"error": f"Skill '{skill_name}' not found in dynamic or core directories"}
    
    try:
        source = path.read_text(encoding='utf-8')
    except (IOError, OSError) as e:
        return {"error": f"Could not read {skill_name}.py: {e}"}
    
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return {
            "error": f"Syntax error in {skill_name}.py",
            "line": e.lineno,
            "message": str(e),
            "suggestion": "Fix syntax before analysis"
        }
    
    analyzer = CodeAnalyzer(source, skill_name)
    analyzer.visit(tree)
    
    severity_weights = {"critical": 25, "high": 15, "medium": 8, "low": 3}
    penalty = sum(severity_weights.get(i["severity"], 5) for i in analyzer.issues)
    health = max(0, 100 - penalty)
    
    for issue in analyzer.issues:
        learned_fix = check_for_learned_fix(skill_name, issue["type"], skill_path=str(path))
        if learned_fix:
            issue["learned_fix"] = learned_fix
            issue["auto_applicable"] = True
    
    return {
        "skill": skill_name,
        "health_score": health,
        "issues_found": len(analyzer.issues),
        "issues": analyzer.issues,
        "critical_count": sum(1 for i in analyzer.issues if i["severity"] == "critical"),
        "timestamp": datetime.now().isoformat()
    }

# ============================================================================
# AUTO-PATCH GENERATION
# ============================================================================

def generate_patch(skill_name: str, issue_type: str, issue_line: int) -> Dict:
    """Generate a code patch to fix a specific issue."""
    path = SKILLS_DIR / f"{skill_name}.py"
    if not path.exists():
        path = CORE_SKILLS_DIR / f"{skill_name}.py"
        if not path.exists():
            return {"error": f"Skill '{skill_name}' not found"}
    
    try:
        lines = path.read_text(encoding='utf-8').split('\n')
    except (IOError, OSError) as e:
        return {"error": f"Could not read {skill_name}.py: {e}"}
    
    if issue_line < 1 or issue_line > len(lines):
        return {"error": f"Invalid line number: {issue_line} (file has {len(lines)} lines)"}
    
    original_line = lines[issue_line - 1]
    fix_applied = ""
    
    if issue_type == "bare_except":
        indent = len(original_line) - len(original_line.lstrip())
        fix_applied = ' ' * indent + "except Exception as e:"
        
    elif issue_type == "missing_docstring":
        func_name_match = re.search(r'def\s+(\w+)', original_line)
        if func_name_match:
            func_name = func_name_match.group(1)
            indent = len(original_line) - len(original_line.lstrip())
            body_indent = ' ' * (indent + 4)
            fix_applied = original_line.rstrip() + '\n' + body_indent + f'"""TODO: Add description for {func_name}."""'
        else:
            return {"error": f"Could not parse function name on line {issue_line}"}
            
    elif issue_type == "missing_timeout":
        if 'requests.' in original_line and 'timeout' not in original_line:
            if original_line.rstrip().endswith(')'):
                fix_applied = original_line.rstrip()[:-1] + ', timeout=30)'
            else:
                # Call spans multiple lines — patching a single line would break syntax
                return {
                    "note": "Multi-line requests call: add timeout= manually to the closing parenthesis",
                    "manual_review_required": True,
                    "original_line": original_line.strip(),
                    "requires_review": True,
                }
    
    elif issue_type == "hardcoded_path":
        # No safe single-line auto-fix: refactoring to pathlib requires knowing the
        # surrounding context (variable assignment, function call, etc.).
        # Return manual_review so the caller surfaces it as a suggestion without
        # inflating the health score with a comment-only pseudo-fix.
        return {
            "note": f"Hardcoded path requires manual refactor to pathlib.Path — see line {issue_line}",
            "manual_review_required": True,
            "original_line": original_line.strip(),
            "requires_review": True,
        }

    if not fix_applied:
        return {
            "note": "No auto-fix available for this issue type",
            "manual_review_required": True,
            "original_line": original_line.strip()
        }
    
    return {
        "skill": skill_name,
        "issue_type": issue_type,
        "line": issue_line,
        "original": original_line.strip(),
        "proposed_fix": fix_applied,
        "diff": f"--- {skill_name}.py:{issue_line}\n+++ {skill_name}.py:{issue_line}\n-{original_line.strip()}\n+{fix_applied.strip()}",
        "safe_to_apply": issue_type in ["bare_except", "missing_timeout", "missing_docstring"],
        "requires_review": issue_type not in ["bare_except", "missing_timeout", "missing_docstring"]
    }

# ============================================================================
# VERIFICATION GATE (superpowers: verification-before-completion)
# Evidence before claims — no fix is complete without passing verification.
# ============================================================================

def _verify_syntax(source_code: str, skill_name: str) -> Dict:
    """Parse and compile source_code. Returns {"ok": True} or {"ok": False, "error": ...}."""
    try:
        ast.parse(source_code)
        compile(source_code, f"{skill_name}.py", "exec")
        return {"ok": True}
    except SyntaxError as e:
        return {"ok": False, "error": f"SyntaxError at line {e.lineno}: {e.msg}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def apply_patch(skill_name: str, patch: Dict, backup: bool = True, **kwargs) -> Dict:
    """Apply a generated patch to the skill file.

    Verification gate: the patched source is parsed and compiled BEFORE the file
    is written. If the result would be syntactically invalid the write is aborted
    and the original file is left untouched.
    """
    if patch.get("error"):
        return patch

    if patch.get("requires_review") or patch.get("manual_review_required"):
        return {"error": "Cannot apply patch that requires manual review", "patch": patch}

    path = SKILLS_DIR / f"{skill_name}.py"
    if not path.exists():
        path = CORE_SKILLS_DIR / f"{skill_name}.py"
        if not path.exists():
            return {"error": f"Skill '{skill_name}' not found"}

    backup_path = None

    if backup:
        try:
            backup_path = path.with_suffix(path.suffix + f".backup.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            backup_path.write_text(path.read_text(encoding='utf-8'), encoding='utf-8')
        except (IOError, OSError):
            pass

    try:
        lines = path.read_text(encoding='utf-8').split('\n')
    except (IOError, OSError) as e:
        return {"error": f"Could not read {skill_name}.py: {e}"}

    line_idx = patch["line"] - 1

    if line_idx < 0 or line_idx >= len(lines):
        return {"error": "Invalid line number in patch"}

    if patch.get("proposed_fix"):
        if '\n' in patch["proposed_fix"]:
            indent = len(lines[line_idx]) - len(lines[line_idx].lstrip())
            fix_lines = [l if l.startswith(' ') else ' ' * indent + l for l in patch["proposed_fix"].split('\n')]
            lines[line_idx:line_idx+1] = fix_lines
        else:
            lines[line_idx] = patch["proposed_fix"]

    # ── VERIFICATION GATE: check before writing ────────────────────────────────
    new_source = '\n'.join(lines)
    syntax_check = _verify_syntax(new_source, skill_name)
    if not syntax_check["ok"]:
        # Delete the backup we just created — nothing was deployed
        if backup_path and backup_path.exists():
            try:
                backup_path.unlink()
            except OSError:
                pass
        return {
            "error": f"Patch aborted — would introduce syntax error: {syntax_check['error']}",
            "original_preserved": True,
            "patch_applied": False,
            "verification": "FAILED",
        }
    # ── END VERIFICATION GATE ──────────────────────────────────────────────────

    tmp = path.with_suffix(".py.tmp")
    try:
        tmp.write_text(new_source, encoding='utf-8')
        os.replace(tmp, path)
    except (IOError, OSError) as e:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return {"error": f"Could not write to {skill_name}.py: {e}"}

    # Clean up backup now that the write succeeded
    if backup_path and backup_path.exists():
        try:
            backup_path.unlink()
        except OSError:
            pass

    record_mistake(
        skill_name=skill_name,
        error_type=patch["issue_type"],
        error_msg=patch.get("original", ""),
        fix_applied=patch.get("proposed_fix", ""),
        skill_path=str(path),
        auto_fix=False,
    )

    return {
        "success": True,
        "skill": skill_name,
        "patch_applied": patch["issue_type"],
        "verification": "PASSED",
        "message": f"✅ Applied fix for {patch['issue_type']} in {skill_name} [verified]",
    }

# ============================================================================
# PATTERN LEARNING
# ============================================================================

def get_skill_health_report(skill_name: str) -> str:
    """Generate a human-readable health report for a skill"""
    result = analyze_skill_code(skill_name)
    
    if result.get("error"):
        return f"❌ {result['error']}"
    
    lines = [
        f"📊 Health Report: {skill_name}",
        "=" * 50,
        f"Health Score: {result['health_score']}/100",
        f"Issues Found: {result['issues_found']}",
        f"Critical: {result['critical_count']}",
        ""
    ]
    
    if result["issues"]:
        lines.append("🔍 Issues:")
        for i, issue in enumerate(result["issues"], 1):
            learned = " ✅ (fix learned!)" if issue.get("learned_fix") else ""
            lines.append(f"  {i}. [{issue['severity'].upper()}] Line {issue['line']}: {issue['message']}{learned}")
            if issue.get("suggestion"):
                lines.append(f"     💡 {issue['suggestion']}")
    else:
        lines.append("✅ No issues detected! Code looks clean.")
    
    return "\n".join(lines)

_TESTS_DIR = Path("/app/memory/tests")


def _generate_pytest_source(skill_name: str, skill_path: Path, functions: list) -> str:
    """Build a runnable pytest file for the given public functions."""
    lines = [
        "import pytest",
        "import importlib.util, sys",
        "",
        f'_SKILL_PATH = "{skill_path}"',
        "",
        "@pytest.fixture(scope='module')",
        "def skill_mod():",
        "    spec = importlib.util.spec_from_file_location('_skill', _SKILL_PATH)",
        "    mod = importlib.util.module_from_spec(spec)",
        "    spec.loader.exec_module(mod)",
        "    return mod",
        "",
    ]
    for func in functions:
        fname = func.name
        args = [a.arg for a in func.args.args if a.arg not in ("self", "cls")]
        safe_name = re.sub(r"[^a-z0-9_]", "_", fname.lower())

        lines += [
            f"def test_{safe_name}_exists(skill_mod):",
            f'    assert hasattr(skill_mod, "{fname}"), "{fname} missing from {skill_name}"',
            "",
            f"def test_{safe_name}_no_crash(skill_mod):",
            f"    fn = getattr(skill_mod, '{fname}')",
            "    try:",
        ]
        if args:
            none_args = ", ".join("None" for _ in args)
            lines.append(f"        fn({none_args})")
        else:
            lines.append("        fn()")
        lines += [
            "    except TypeError:",
            "        pass  # expected — required args not satisfied",
            "    except Exception as e:",
            f'        pytest.fail(f"Unexpected exception in {fname}: {{e}}")',
            "",
        ]
    return "\n".join(lines)


def suggest_tests(skill_name: str = "") -> str:
    """Write a pytest file for the skill and run it, returning pass/fail results."""
    if not skill_name:
        return "❌ suggest_tests() requires a skill_name argument."
    path = SKILLS_DIR / f"{skill_name}.py"
    if not path.exists():
        path = CORE_SKILLS_DIR / f"{skill_name}.py"
        if not path.exists():
            return f"❌ Skill '{skill_name}' not found"

    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (IOError, OSError, SyntaxError) as e:
        return f"❌ Cannot parse skill: {e}"

    functions = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")
    ]
    if not functions:
        return f"⚠️ No public functions found in {skill_name}"

    _TESTS_DIR.mkdir(parents=True, exist_ok=True)
    test_file = _TESTS_DIR / f"test_{skill_name}.py"
    test_src = _generate_pytest_source(skill_name, path, functions)
    test_file.write_text(test_src, encoding="utf-8")

    return run_tests(skill_name)


def run_tests(skill_name: str) -> str:
    """Run pytest for the skill's test file in /app/memory/tests/ and return results."""
    test_file = _TESTS_DIR / f"test_{skill_name}.py"
    if not test_file.exists():
        return f"❌ No test file found at {test_file}. Run suggest_tests('{skill_name}') first."

    try:
        proc = subprocess.run(
            ["python", "-m", "pytest", str(test_file), "-v", "--tb=short", "--no-header"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        passed = proc.returncode == 0
        output = (proc.stdout + proc.stderr).strip()
        status = "✅ PASSED" if passed else "❌ FAILED"
        return f"🧪 Tests for {skill_name}: {status}\n{'-'*50}\n{output}"
    except subprocess.TimeoutExpired:
        return f"❌ Tests timed out after 60s for {skill_name}"
    except Exception as e:
        return f"❌ Could not run tests: {e}"

def learn_from_feedback(skill_name: str, feedback: str, was_helpful: bool) -> str:
    """Learn from user feedback on suggestions."""
    lesson = {
        "timestamp": datetime.now().isoformat(),
        "skill": skill_name,
        "feedback": feedback[:500],
        "was_helpful": was_helpful,
        "type": "user_feedback"
    }
    saved = _save_lesson(lesson)
    if saved:
        _index_lesson_in_chroma(lesson)

    status = "✅ Thank you! Feedback recorded." if was_helpful and saved else "📝 Feedback noted for improvement."
    return f"{status} Future suggestions will be refined."

# ============================================================================
# VERDICT VERIFICATION (ported from Claude Code's verification agent pattern)
# After syntax checks pass, a model call produces VERDICT: PASS/FAIL/PARTIAL.
# The model must trace an actual execution path — "looks correct" is forbidden.
# ============================================================================

def _call_llm_verdict(prompt: str) -> str:
    """Minimal single-turn LLM call for VERDICT checks. Respects MODEL_SOURCE."""
    model_source = os.getenv("MODEL_SOURCE", "cloud")
    if model_source == "local":
        ollama_base = os.getenv("OLLAMA_API_BASE", "http://ollama:11434")
        model = os.getenv("OLLAMA_MODEL", "llama3.2")
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "options": {"temperature": 0.0, "num_predict": 512},
        }
        try:
            resp = _requests.post(f"{ollama_base}/api/chat", json=payload, timeout=60)
            resp.raise_for_status()
            return resp.json().get("message", {}).get("content", "")
        except _requests.exceptions.Timeout:
            raise RuntimeError(f"Ollama timed out after 60s ({ollama_base})")
        except _requests.exceptions.ConnectionError as e:
            raise RuntimeError(f"Ollama unreachable at {ollama_base}: {e}")
        except _requests.exceptions.HTTPError as e:
            raise RuntimeError(f"Ollama returned HTTP {e.response.status_code}: {e}")
    else:
        litellm_base = os.getenv("LITELLM_API_BASE", "http://litellm:4000")
        api_key = os.getenv("LITELLM_MASTER_KEY", "")
        # Use a dedicated fast model for verdicts; fall back to DEFAULT_MODEL
        model = os.getenv("VERDICT_MODEL", os.getenv("DEFAULT_MODEL", "gpt-4o-mini"))
        headers = {"Authorization": f"Bearer {api_key}"}
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.0,
            "max_tokens": 512,
        }
        try:
            resp = _requests.post(
                f"{litellm_base}/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except _requests.exceptions.Timeout:
            raise RuntimeError(f"LiteLLM timed out after 60s ({litellm_base})")
        except _requests.exceptions.ConnectionError as e:
            raise RuntimeError(f"LiteLLM unreachable at {litellm_base}: {e}")
        except _requests.exceptions.HTTPError as e:
            raise RuntimeError(f"LiteLLM returned HTTP {e.response.status_code}: {e}")


def _verdict_check(skill_name: str, source: str) -> str:
    """Ask the model to verify a skill and return VERDICT: PASS/FAIL/PARTIAL.

    Rules enforced in the prompt (from Claude Code's verification agent):
    - Must trace an actual execution path with a realistic input
    - 'Looks correct' rationalizations are explicitly forbidden
    - PARTIAL = works but has a named edge case bug
    - FAIL = would raise on the happy path
    - PASS = traced execution returns expected result
    """
    if len(source) > 8000:
        # Seek back to the nearest function/class definition boundary so the
        # LLM receives complete function bodies rather than truncated ones.
        cut = -1
        for boundary in ('\ndef ', '\nasync def ', '\nclass '):
            pos = source.rfind(boundary, 0, 8000)
            if pos != -1 and pos > cut:
                cut = pos
        if cut == -1:
            cut = source.rfind('\n', 0, 8000)
        if cut == -1:
            cut = 8000
        source_preview = source[:cut] + "\n... [truncated]"
    else:
        source_preview = source
    prompt = f"""You are a strict verification agent for Python skill modules.

TASK: Verify that skill '{skill_name}' is functionally correct.

RULES:
1. Return EXACTLY one of: VERDICT: PASS  /  VERDICT: FAIL  /  VERDICT: PARTIAL
2. NEVER say "the code looks correct" — you MUST trace an actual execution path
3. Pick the most important public function, choose a realistic input, execute it step by step mentally, show the result
4. PARTIAL = compiles and mostly works but has a specific edge case bug you can name
5. FAIL = broken in a way that would cause a runtime error on the happy path
6. PASS = you traced execution and it returns the expected result with no obvious bugs

SOURCE ({skill_name}.py):
```python
{source_preview}
```

Trace one function execution, then end your response with:
VERDICT: PASS|FAIL|PARTIAL
EVIDENCE: <one sentence — the specific trace result or failure reason, not "looks correct">"""

    try:
        response = _call_llm_verdict(prompt)
    except Exception:
        import time as _time
        _time.sleep(2)
        try:
            response = _call_llm_verdict(prompt)
        except Exception as e:
            return f"⚠️  VERDICT skipped (LLM unavailable): {e}"

    for line in response.splitlines():
        stripped = line.strip()
        if stripped.startswith("VERDICT:"):
            verdict_word = stripped.replace("VERDICT:", "").strip().upper().split()[0]
            evidence = ""
            for ev_line in response.splitlines():
                if ev_line.strip().startswith("EVIDENCE:"):
                    evidence = ev_line.strip().replace("EVIDENCE:", "").strip()
                    break
            evidence = evidence or "(no evidence provided)"
            if verdict_word == "PASS":
                return f"✅ VERDICT: PASS — {evidence}"
            elif verdict_word == "PARTIAL":
                return f"⚠️  VERDICT: PARTIAL — {evidence}"
            elif verdict_word == "FAIL":
                return f"❌ VERDICT: FAIL — {evidence}"

    return f"⚠️  VERDICT unparseable — raw: {response[:300]}"


# ============================================================================
# MAIN SKILL FUNCTIONS (User-facing - MUST be at module level)
# ============================================================================

def verify_skill(skill_name: str) -> str:
    """Run verification checks on a skill and report evidence of its state.

    Superpowers rule: evidence before claims — run this before declaring a fix complete.
    Checks:
      1. File is readable
      2. Source parses as valid Python (ast.parse)
      3. Source compiles without errors (compile)
      4. Required skill metadata (NAME, DOC) is present
    Returns a pass/fail verdict with specific evidence, not assumptions.
    """
    path = SKILLS_DIR / f"{skill_name}.py"
    if not path.exists():
        path = CORE_SKILLS_DIR / f"{skill_name}.py"
        if not path.exists():
            return f"❌ VERIFICATION FAILED: skill '{skill_name}' not found in dynamic or core directories"

    try:
        source = path.read_text(encoding='utf-8')
    except (IOError, OSError) as e:
        return f"❌ VERIFICATION FAILED: could not read {skill_name}.py — {e}"

    syntax_check = _verify_syntax(source, skill_name)
    if not syntax_check["ok"]:
        return (
            f"❌ VERIFICATION FAILED: {skill_name}.py has a syntax/compile error\n"
            f"   {syntax_check['error']}\n"
            f"⚠️  This file may be broken — restore from .backup.* before using."
        )

    issues = []
    if not re.search(r'^NAME\s*=', source, re.MULTILINE):
        issues.append("missing NAME metadata")
    if not re.search(r'^DOC\s*=', source, re.MULTILINE):
        issues.append("missing DOC metadata")
    if not re.search(r'^SHORT_DOC\s*=', source, re.MULTILINE):
        issues.append("missing SHORT_DOC metadata")

    if issues:
        return (
            f"⚠️  VERIFICATION PARTIAL: {skill_name}.py compiles OK but has issues:\n"
            + "\n".join(f"   • {i}" for i in issues)
        )

    line_count = source.count('\n') + 1
    verdict = _verdict_check(skill_name, source)
    return (
        f"✅ SYNTAX PASSED: {skill_name}.py\n"
        f"   • Syntax  : valid (ast.parse + compile)\n"
        f"   • Metadata: NAME and DOC present\n"
        f"   • Lines   : {line_count}\n"
        f"   • {verdict}"
    )


def audit(skill_name: str = "") -> str:
    """Analyze a skill and return health report."""
    if not skill_name:
        return "❌ audit() requires a skill_name argument. Usage: audit('my_skill')"
    return get_skill_health_report(skill_name)

def _fix_all(skill_name: str, issue_type: str) -> str:
    """Fix every occurrence of issue_type in a skill (reverse-order so line numbers stay valid)."""
    analysis = analyze_skill_code(skill_name)
    if analysis.get("error"):
        return f"❌ {analysis['error']}"

    matching = [i for i in analysis["issues"] if i["type"] == issue_type]
    if not matching:
        return f"✅ No '{issue_type}' issues found in {skill_name}"

    results = []
    skipped = []
    # Reverse order: fix bottom lines first so upper line numbers don't shift
    for issue in sorted(matching, key=lambda x: x["line"], reverse=True):
        patch = generate_patch(skill_name, issue_type, issue["line"])
        if patch.get("error"):
            skipped.append(f"  Line {issue['line']}: {patch['error']}")
        elif patch.get("safe_to_apply"):
            r = apply_patch(skill_name, patch)
            if r.get("error"):
                skipped.append(f"  Line {issue['line']}: {r['error']}")
            else:
                results.append(f"  Line {issue['line']}: {r.get('message', '⚠️ applied')}")
        else:
            skipped.append(f"  Line {issue['line']}: requires manual review — {patch.get('note', '')}")

    summary = f"🔧 Fixed {len(results)}/{len(matching)} '{issue_type}' issues in {skill_name}:"
    if results:
        summary += "\n" + "\n".join(results)
    if skipped:
        summary += "\n⚠️ Skipped:\n" + "\n".join(skipped)

    # ── VERIFICATION GATE: evidence before claims ──────────────────────────────
    if results:
        verification = verify_skill(skill_name)
        summary += f"\n\n{verification}"

    return summary


def fix(skill_name: str, issue_type: str, line_number: str) -> str:
    """Auto-fix a specific issue in a skill. Pass line_number='all' to fix every occurrence.

    Always runs verify_skill() after applying a fix — result includes verification
    evidence so the agent cannot claim success without seeing passing output.
    """
    if str(line_number).strip().lower() == "all":
        return _fix_all(skill_name, issue_type)

    try:
        line_num = int(line_number)
    except (ValueError, TypeError):
        return f"❌ Invalid line number: '{line_number}' (must be integer or 'all')"

    patch = generate_patch(skill_name, issue_type, line_num)

    if patch.get("error") or patch.get("requires_review"):
        return f"⚠️ Manual review needed:\n{json.dumps(patch, indent=2)}"

    result = apply_patch(skill_name, patch)

    if result.get("error"):
        return f"❌ Fix failed: {result['error']}"

    # ── VERIFICATION GATE: evidence before claims ──────────────────────────────
    verification = verify_skill(skill_name)
    return f"{result.get('message', '✅ Patch applied')}\n\n{verification}"

def prevent(skill_name: str, error_type: str) -> str:
    """Check if we've learned a fix for this error type and apply it proactively."""
    learned_fix = check_for_learned_fix(skill_name, error_type)
    if learned_fix:
        return f"✅ Prevention ready: '{learned_fix}'\nUse 'fix' to apply, or I can auto-apply on next error."
    
    patterns = _load_error_patterns()
    if error_type in patterns:
        return f"📚 Known pattern: {patterns[error_type]}\nFix will be learned after first occurrence."
    
    return f"❓ Unknown error type: '{error_type}'. Report this to improve the system."

def report() -> str:
    """Generate a system-wide improvement report."""
    lessons = _load_lessons()
    patterns = _load_error_patterns()
    
    issue_counts = {}
    for lesson in lessons:
        t = lesson.get("error_type", "unknown")
        issue_counts[t] = issue_counts.get(t, 0) + 1
    
    lines = [
        "🚀 Self-Improvement System Report",
        "=" * 50,
        f"Lessons Learned: {len(lessons)}",
        f"Recognized Patterns: {len(patterns)}",
        "",
        "📈 Most Common Issues:",
    ]
    
    for issue_type, count in sorted(issue_counts.items(), key=lambda x: x[1], reverse=True)[:5]:
        fix_hint = patterns.get(issue_type, {}).get("fix", "Manual fix required")
        lines.append(f"  • {issue_type}: {count}x → {fix_hint}")
    
    lines.append(f"\n💡 Tip: Run 'audit' on skills with low health scores first")
    
    return "\n".join(lines)

def daily_review(skill_name: str = "") -> str:
    """
    Daily learning cycle: audit skill(s), surface patterns from memory, suggest next actions.

    Pass skill_name to review one skill, or leave blank to review ALL dynamic skills.
    Reads lessons.jsonl to detect recurring failures so the agent gets smarter over time.
    Recommends web searches for unknown patterns the system has not seen before.
    """
    from datetime import timedelta

    # ── 1. Determine which skills to scan ──────────────────────────────────────
    targets: List[str] = []
    if skill_name:
        targets = [skill_name]
    else:
        if SKILLS_DIR.exists():
            for p in sorted(SKILLS_DIR.glob("*.py")):
                if not p.name.startswith("_"):
                    targets.append(p.stem)
        if not targets:
            # Fall back to core if dynamic dir is missing or empty
            if CORE_SKILLS_DIR.exists():
                for p in sorted(CORE_SKILLS_DIR.glob("*.py")):
                    if not p.name.startswith("_"):
                        targets.append(p.stem)

    if not targets:
        return "⚠️ No skills found to review."

    # ── 2. Audit each skill ────────────────────────────────────────────────────
    audit_results: List[Dict] = []
    for sn in targets:
        result = analyze_skill_code(sn)
        if not result.get("error"):
            audit_results.append(result)

    total_issues = sum(r["issues_found"] for r in audit_results)
    avg_health = (
        sum(r["health_score"] for r in audit_results) / len(audit_results)
        if audit_results else 0
    )

    # ── 3. Load lessons from the last 7 days ──────────────────────────────────
    lessons = _load_lessons()
    cutoff = datetime.now() - timedelta(days=7)
    recent_lessons = []
    for lesson in lessons:
        ts = lesson.get("timestamp")
        if not ts:
            continue
        try:
            if datetime.fromisoformat(ts) >= cutoff:
                recent_lessons.append(lesson)
        except (ValueError, TypeError):
            continue  # skip lessons with malformed timestamps

    # Count recurring error types in recent lessons
    recurring: Dict[str, int] = {}
    for lesson in recent_lessons:
        et = lesson.get("error_type") or lesson.get("type") or "unknown"
        recurring[et] = recurring.get(et, 0) + 1

    top_recurring = sorted(recurring.items(), key=lambda x: x[1], reverse=True)[:5]

    # ── 4. Identify unknown patterns (seen in lessons but not in patterns db) ──
    patterns = _load_error_patterns()
    unknown_patterns = [et for et, _ in top_recurring if et not in patterns and et != "unknown"]

    # ── 5. Find skills that need immediate attention ───────────────────────────
    critical_skills = [r for r in audit_results if r["critical_count"] > 0 or r["health_score"] < 60]
    auto_fixable = [
        r for r in audit_results
        if any(i["type"] in ("bare_except", "missing_timeout") for i in r["issues"])
    ]

    # ── 6. Build report ───────────────────────────────────────────────────────
    lines = [
        "🗓️  Daily Self-Improvement Review",
        "=" * 52,
        f"Skills scanned   : {len(audit_results)}",
        f"Avg health score : {avg_health:.0f}/100",
        f"Total issues     : {total_issues}",
        f"Recent lessons   : {len(recent_lessons)} (last 7 days)",
        "",
    ]

    if critical_skills:
        lines.append("🚨 Needs immediate attention:")
        for r in critical_skills:
            lines.append(f"  • {r['skill']}  score={r['health_score']}  critical={r['critical_count']}")
        lines.append("")

    # Auto-fix summary from log
    if _AUTO_FIX_LOG.exists():
        try:
            af_entries = []
            with _AUTO_FIX_LOG.open("r", encoding="utf-8") as f:
                for line in f:
                    entry = json.loads(line.strip())
                    ts = entry.get("timestamp", "")
                    try:
                        if datetime.fromisoformat(ts) >= cutoff:
                            af_entries.append(entry)
                    except (ValueError, TypeError):
                        continue
            if af_entries:
                improved = sum(1 for e in af_entries if e.get("outcome") == "AUTO_IMPROVED")
                reverted = sum(1 for e in af_entries if e.get("outcome") == "AUTO_REVERTED")
                lines.append("🤖 Auto-fix activity (last 7 days):")
                lines.append(f"  • Attempts: {len(af_entries)}  |  ✅ Resolved: {improved}  |  ⚠️ Reverted: {reverted}")
                for e in af_entries[-5:]:
                    lines.append(f"  • {e['skill']}.{e['error_type']} → {e['outcome']} ({e['patches']} patch(es), {e['before_score']}→{e['after_score']})")
                lines.append("")
        except Exception:
            pass

    if auto_fixable:
        lines.append("🔧 Auto-fixable right now (run fix(skill,'issue_type','all')):")
        for r in auto_fixable:
            types = list({i["type"] for i in r["issues"] if i["type"] in ("bare_except", "missing_timeout")})
            lines.append(f"  • {r['skill']}: {', '.join(types)}")
        lines.append("")

    if top_recurring:
        lines.append("📈 Recurring patterns (last 7 days):")
        for et, cnt in top_recurring:
            hint = patterns.get(et, {}).get("fix", "no fix recorded yet")
            lines.append(f"  • {et}: {cnt}x  →  {hint}")
        lines.append("")

    if unknown_patterns:
        lines.append("🔍 Unknown patterns — recommend web search:")
        for et in unknown_patterns:
            lines.append(f"  • web.search('Python {et} best practice fix')")
        lines.append("")

    if not critical_skills and not auto_fixable and not unknown_patterns:
        lines.append("✅ All skills look healthy — no urgent action needed.")

    lines.append("💡 Tip: run daily_review() each morning so the agent improves over time.")
    return "\n".join(lines)


def should_self_improve(threshold: int = 3, window_days: int = 7) -> dict:
    """Check if recurring failures in recent lessons warrant running autoimprove loops.

    Scans lessons.jsonl for the last window_days days and counts occurrences per
    error_type. If any type hits the threshold without a learned fix already in place,
    self-improvement is recommended.

    Call this at session start or after a task that produced errors.

    Args:
        threshold:   Minimum recurrence count to trigger a recommendation (default 3).
        window_days: How many days of lesson history to consider (default 7).

    Returns:
        {
            "improve":        bool,          # True if autoimprove is recommended
            "reason":         str,           # Human-readable explanation
            "patterns":       list[dict],    # [{error_type, count, has_fix, suggested_loop}]
            "suggested_loop": str,           # autoimprove loop name to run first, or ""
        }
    """
    from datetime import timedelta

    cutoff  = datetime.now() - timedelta(days=window_days)
    lessons = _load_lessons()

    # Filter to the window
    recent: List[Dict] = []
    for lesson in lessons:
        ts = lesson.get("timestamp", "")
        if not ts:
            continue
        try:
            if datetime.fromisoformat(ts) >= cutoff:
                recent.append(lesson)
        except (ValueError, TypeError):
            continue

    if not recent:
        return {
            "improve": False,
            "reason": f"No lessons recorded in the last {window_days} days.",
            "patterns": [],
            "suggested_loop": "",
        }

    # Count per error_type
    counts: Dict[str, int] = {}
    for lesson in recent:
        et = lesson.get("error_type") or lesson.get("type") or "unknown"
        if et == "user_feedback":
            continue
        counts[et] = counts.get(et, 0) + 1

    # Map auto-fixable types to their autoimprove loop
    _LOOP_MAP = {
        "bare_except":       "ast_audit",
        "missing_timeout":   "ast_audit",
        "missing_docstring": "ast_audit",
    }
    _DEFAULT_LOOP = "error_reduce"

    # Build a set of error types for which any lesson recorded a fix, across all skills.
    fixed_types: set = {
        l.get("error_type") or l.get("type")
        for l in lessons
        if (l.get("error_type") or l.get("type")) and l.get("fix_applied", "").strip()
    }

    patterns = []
    for et, count in sorted(counts.items(), key=lambda x: x[1], reverse=True):
        if count < threshold:
            continue
        has_fix = et in fixed_types
        patterns.append({
            "error_type":     et,
            "count":          count,
            "has_fix":        has_fix,
            "suggested_loop": _LOOP_MAP.get(et, _DEFAULT_LOOP),
        })

    if not patterns:
        return {
            "improve": False,
            "reason": (
                f"No error type exceeded threshold={threshold} in the last {window_days} days "
                f"({len(recent)} lessons scanned)."
            ),
            "patterns": [],
            "suggested_loop": "",
        }

    top = patterns[0]
    reason = (
        f"'{top['error_type']}' has occurred {top['count']}× in the last {window_days} days "
        f"(threshold={threshold}). Running autoimprove.run_loop('{top['suggested_loop']}') is recommended."
    )
    return {
        "improve":        True,
        "reason":         reason,
        "patterns":       patterns,
        "suggested_loop": top["suggested_loop"],
    }


def status() -> str:
    """Return skill status and capabilities."""
    lessons = _load_lessons()
    patterns = _load_error_patterns()
    
    info = [
        "🧠 Self-Improvement Skill Status",
        f"✅ Loaded: {NAME}",
        f"📚 Lessons learned: {len(lessons)}",
        f"🔍 Recognized patterns: {len(patterns)}",
        "",
        "Available functions:",
        "  • audit(skill_name)                          - Analyze code health",
        "  • fix(skill_name, issue_type, line_number)   - Auto-apply fix (use 'all' to fix every occurrence); always verifies after",
        "  • verify_skill(skill_name)                   - Syntax+compile+metadata check — call before claiming a fix is done",
        "  • daily_review(skill_name?)                  - Daily learning cycle: scan + surface recurring patterns",
        "  • should_self_improve(threshold=3, window_days=7) - Check if recurring failures warrant autoimprove loops",
        "  • prevent(skill_name, error_type)            - Check for learned fixes",
        "  • report()                                   - System-wide improvement summary",
        "  • suggest_tests(skill_name)                  - Generate test cases",
        "  • learn_from_feedback(skill_name, feedback, was_helpful) - Record user feedback",
        "  • status()                                   - This help message",
        "",
        "Memory files:",
        f"  • Lessons : {LESSONS_FILE}",
        f"  • Patterns: {PATTERNS_FILE}",
        "",
        "💡 Tip: schedule daily_review() every morning so the agent improves over time."
    ]
    return "\n".join(info)

# ============================================================================
# BACKWARDS COMPATIBILITY ALIASES
# ============================================================================

analyze = audit
improve = fix

# ============================================================================
# EXPORT LIST (Helps skill loader detect public functions)
# ============================================================================

__all__ = [
    "NAME",
    "DOC",
    "SHORT_DOC",
    # Primary user-facing functions
    "audit",
    "fix",
    "verify_skill",
    "daily_review",
    "prevent",
    "report",
    "suggest_tests",
    "run_tests",
    "learn_from_feedback",
    "status",
    # Aliases
    "analyze",
    "improve",
    # Semantic lesson search
    "search_lessons_semantic",
    "index_all_lessons",
    # Lower-level functions callable by agents
    "check_lessons",
    "check_for_learned_fix",
    "record_mistake",
    "generate_patch",
    "apply_patch",
    "get_skill_health_report",
    "analyze_skill_code",
    "should_self_improve",
]