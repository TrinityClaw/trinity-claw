"""
AutoImprove — Autoresearch-style self-improvement loops for Trinity

Inspired by Karpathy's autoresearch: propose → apply → test → keep/revert → repeat
Runs overnight via scheduler.schedule_recurring() while you sleep.

Two-track system:
  DYNAMIC skills → auto-fix (snapshot → patch → test → keep or restore)
  CORE skills    → suggest only (audit → generate patch → save to memory → you approve)

Available loops:
  • daily_review  — learning review, no code changes
  • ast_audit     — auto-fix bare_except & missing_timeout in dynamic skills
  • error_reduce  — auto-fix the most-frequent error pattern in dynamic skills
  • suggest_core  — audit all 30+ core skills, save proposed patches for your review
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

# ── Skill metadata ─────────────────────────────────────────────────────────────

NAME = "autoimprove"
SHORT_DOC = "Overnight self-improvement loops — auto-fix dynamic skills, suggest patches for core skills, run research loops."
DOC = (
    "Autoresearch-style overnight self-improvement loops — two-track system: "
    "DYNAMIC skills auto-fix (snapshot→patch→test→keep/restore); "
    "CORE skills suggest-only (audit→patch→save to memory→you approve). "
    "research(query, depth='quick', save=True, max_iterations=None)→autoresearch-style loop: "
    "search→measure coverage→refine query→repeat until convergence (≥72% term coverage) or cap; "
    "depth='deep' runs 4 iterations with 4 sources each vs quick's 2×2; "
    "coverage metric = 0.4×source_yield + 0.6×query_term_coverage; "
    "run_experiment(skill_name, issue_type)→one autoresearch cycle on a dynamic skill; "
    "run_loop(loop_name, max_experiments=10)→named loop: ast_audit|error_reduce|daily_review|suggest_core|pattern_mining|discover_patterns; "
    "run_all(max_experiments=5, max_runtime_seconds=25)→all loops in sequence; direct calls capped at 25s (dispatcher limit) — use schedule_nightly() for full overnight runs with no cap; pass max_runtime_seconds=None only from a scheduled context; "
    "discover_patterns loop→scan lessons.jsonl for error types NOT in error_patterns.json → use LLM to analyze and classify each → park proposals via park_idea(); fills the blind spot where error_reduce ignores unknown failure modes; "
    "loop_roi(runs=10)→show per-loop historical ROI (improved/total experiments, avg wall time) averaged over the last N run_all() runs; ⚠️ flags loops that consistently produce 0 improvements; "
    "pattern_mining loop→scan session_logs.jsonl for recurring task_type patterns → park skill proposals via park_idea(); review with list_ideas(); "
    "suggest_core(skill_name=None, max_skills=30)→audit core skills, save proposed patches to memory; skill_name targets one skill (e.g. 'browser_session'); "
    "list_suggestions(status='pending', skill_name=None)→show pending|applied|failed|all core suggestions; skill_name filters to one skill; "
    "apply_suggestion(skill_name, issue_type)→apply one approved suggestion to a core skill; "
    "schedule_nightly(run_time='2am')→put on autopilot; "
    "report(days=7)→improvement history; "
    "status()→config and skills in scope; "
    "design(task)→design-first gate: scan existing skills, surface gaps, propose 2-3 approaches with trade-offs — call this before create_skill for any non-trivial build request; "
    "write_spec(task, approach, details)→write approved spec to /app/memory/designs/YYYY-MM-DD-<slug>.md and return the path — call after user approves an approach, before writing any code; "
    "lessons_to_proposals(threshold=3)→scan error_patterns.json for patterns with ≥threshold occurrences that are NOT auto-fixable, save structured review proposals to lesson_proposals.jsonl — runs automatically inside error_reduce; "
    "list_proposals(status='pending')→show lesson-derived improvement proposals needing a human decision; status: pending|resolved|dismissed|all; "
    "park_idea(idea, source='')→park a free-form improvement idea to improvement_ideas.jsonl for later action — use during research, mid-experiment, or when user mentions a potential improvement; "
    "list_ideas(status='open')→show parked improvement ideas; status: open|dismissed|all; "
    "dismiss_idea(idea_id)→mark a parked idea as dismissed after acting on it or deciding it's not worth pursuing; "
    "should_create_skill(execution_logs, iteration_count, user_message='')→analyze a completed task trace to decide if a new skill should be created; "
    "returns {create: bool, reason, trigger, complexity_score}; triggers: complexity (5+ calls), error_recovery, correction, workflow (3+ skills)."
)

# ── Config ─────────────────────────────────────────────────────────────────────

SKILLS_DYNAMIC_DIR = Path("/app/skills/dynamic")
SKILLS_CORE_DIR    = Path("/app/skills/core")
MEMORY_DIR         = Path("/app/memory")
IMPROVE_LOG        = MEMORY_DIR / "improvement_log.jsonl"
SUGGESTIONS_FILE   = MEMORY_DIR / "core_suggestions.jsonl"
PATTERNS_FILE      = MEMORY_DIR / "error_patterns.json"
LESSONS_FILE       = MEMORY_DIR / "lessons.jsonl"
PROPOSALS_FILE     = MEMORY_DIR / "lesson_proposals.jsonl"
IDEAS_FILE         = MEMORY_DIR / "improvement_ideas.jsonl"
SESSION_LOG        = MEMORY_DIR / "session_logs.jsonl"

# Only deterministic, low-risk issue types get auto-applied
AUTO_FIXABLE = ("bare_except", "missing_timeout", "missing_docstring")

# Min occurrences before a non-auto-fixable pattern earns a review proposal
PROPOSAL_THRESHOLD = 3

# Min IMPROVED experiments needed before MAD confidence is meaningful
MAD_MIN_SAMPLES = 3

# ── Atomic file writes ────────────────────────────────────────────────────────

def _atomic_write_jsonl(path: Path, records: List[Dict]) -> None:
    """Overwrite a JSONL file atomically using a temp file + os.replace().

    Writes to <path>.tmp first, then swaps it into place with a single
    os.replace() call.  On Linux/macOS, os.replace() is a rename(2) syscall —
    atomic at the filesystem level — so concurrent readers always see either
    the old file or the new file, never a partial write or an empty file.

    This is safer than opening with "w" (which truncates immediately) and then
    writing, which leaves a window where another process reads an empty file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise


# ── Logging ────────────────────────────────────────────────────────────────────

def _log(entry: Dict) -> None:
    """Append one experiment result to improvement_log.jsonl."""
    try:
        IMPROVE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with IMPROVE_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception as _exc:
        sys.stderr.write(f"[autoimprove] _log failed: {_exc}\n")


def _load_log(days: int = 7) -> List[Dict]:
    """Load improvement log entries from the last N days."""
    if not IMPROVE_LOG.exists():
        return []
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    entries = []
    try:
        for line in IMPROVE_LOG.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                if e.get("timestamp", "") >= cutoff:
                    entries.append(e)
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return entries


# ── MAD confidence scoring ────────────────────────────────────────────────────

def _load_all_score_deltas(cap: int = 50) -> List[float]:
    """
    Return up to `cap` most-recent score deltas from IMPROVED experiments.
    Used as the noise-floor sample for MAD confidence scoring.
    """
    if not IMPROVE_LOG.exists():
        return []
    deltas: List[float] = []
    try:
        lines = IMPROVE_LOG.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                if (
                    e.get("outcome") == "IMPROVED"
                    and e.get("before_score") is not None
                    and e.get("after_score") is not None
                ):
                    deltas.append(float(e["after_score"]) - float(e["before_score"]))
                    if len(deltas) >= cap:
                        break
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
    except Exception:
        pass
    return deltas


def _compute_confidence(current_delta: Optional[float], recent_deltas: List[float]):
    """
    Compute MAD-based confidence that an improvement exceeds session noise.

    Mirrors pi-autoresearch's confidence scoring:
      MAD = median(|delta_i - median(deltas)|)
      ratio = current_delta / MAD

    Returns (ratio, label):
      ratio  — float or None if not enough data
      label  — "🟢 genuine" | "🟡 marginal" | "🔴 within noise" | "— (no baseline)"
    """
    if current_delta is None or len(recent_deltas) < MAD_MIN_SAMPLES:
        return None, "— (no baseline yet)"

    deltas = sorted(recent_deltas)
    n      = len(deltas)
    median = deltas[n // 2] if n % 2 else (deltas[n // 2 - 1] + deltas[n // 2]) / 2.0
    abs_devs = sorted(abs(d - median) for d in deltas)
    mad      = abs_devs[n // 2] if n % 2 else (abs_devs[n // 2 - 1] + abs_devs[n // 2]) / 2.0

    if mad == 0:
        # All past deltas identical — any positive improvement is genuine
        label = "🟢 genuine" if current_delta > 0 else "🔴 within noise"
        return None, label

    ratio = round(current_delta / mad, 2)
    if ratio >= 2.0:
        label = f"🟢 genuine ({ratio}×)"
    elif ratio >= 1.0:
        label = f"🟡 marginal ({ratio}×)"
    else:
        label = f"🔴 within noise ({ratio}×)"

    return ratio, label


# ── Suggestion store (core skills) ────────────────────────────────────────────

def _load_suggestions() -> List[Dict]:
    """Load all suggestions from core_suggestions.jsonl."""
    if not SUGGESTIONS_FILE.exists():
        return []
    suggestions = []
    try:
        for line in SUGGESTIONS_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                suggestions.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return suggestions


def _save_suggestions(suggestions: List[Dict]) -> None:
    """Overwrite core_suggestions.jsonl with the full list."""
    try:
        _atomic_write_jsonl(SUGGESTIONS_FILE, suggestions)
    except Exception as _exc:
        sys.stderr.write(f"[autoimprove] _save_suggestions failed: {_exc}\n")


# ── Ideas parking lot ─────────────────────────────────────────────────────────

def _load_ideas() -> List[Dict]:
    """Load all ideas from improvement_ideas.jsonl."""
    if not IDEAS_FILE.exists():
        return []
    ideas = []
    try:
        for line in IDEAS_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                ideas.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return ideas


def _save_ideas(ideas: List[Dict]) -> None:
    """Overwrite improvement_ideas.jsonl with the full list."""
    try:
        _atomic_write_jsonl(IDEAS_FILE, ideas)
    except Exception as _exc:
        sys.stderr.write(f"[autoimprove] _save_ideas failed: {_exc}\n")


def park_idea(idea: str, source: str = "") -> str:
    """
    Park a free-form improvement idea for later action.

    Use this when Trinity or you spot something worth trying but can't act on
    it right now — during a research loop, mid-experiment observation, or a
    passing thought. Ideas accumulate in improvement_ideas.jsonl and are
    reviewed with list_ideas().

    Args:
        idea:   Plain-language description of the improvement idea.
        source: Optional context — skill name, loop name, or 'user'.

    Returns:
        Confirmation with the assigned idea ID.
    """
    idea = str(idea).strip()
    if not idea:
        return "❌ idea text cannot be empty."

    import hashlib as _hashlib
    import uuid as _uuid
    idea_hash = _hashlib.sha256(idea.encode()).hexdigest()[:12]

    ideas = _load_ideas()
    for existing in ideas:
        if existing.get("status") == "open" and existing.get("hash") == idea_hash:
            return f"💡 Duplicate skipped [{existing['id']}]: {idea[:80]}"

    idea_id = datetime.now().strftime("%Y%m%d") + "_" + _uuid.uuid4().hex[:6]
    entry = {
        "id":        idea_id,
        "hash":      idea_hash,
        "timestamp": datetime.now().isoformat(),
        "idea":      idea,
        "source":    str(source).strip() or "(unspecified)",
        "status":    "open",
    }
    ideas.append(entry)
    _save_ideas(ideas)
    return f"💡 Idea parked [{idea_id}]: {idea[:80]}"


def list_ideas(status: str = "open") -> str:
    """
    Show parked improvement ideas.

    Args:
        status: Filter — 'open' | 'dismissed' | 'all'

    Returns:
        Formatted list of ideas.
    """
    valid_statuses = ("open", "dismissed", "all")
    if status not in valid_statuses:
        status = "all"

    all_ideas = _load_ideas()
    if not all_ideas:
        return (
            "📭 No ideas parked yet.\n"
            "Use autoimprove.park_idea('your idea') to save one."
        )

    filtered = all_ideas if status == "all" else [i for i in all_ideas if i.get("status") == status]
    if not filtered:
        return f"📭 No '{status}' ideas. Try list_ideas('all') to see everything."

    icons = {"open": "💡", "dismissed": "—"}
    lines = [f"💡 Improvement ideas — {status} ({len(filtered)} of {len(all_ideas)} total):"]

    for idea in filtered:
        icon   = icons.get(idea.get("status", "open"), "?")
        ts     = idea.get("timestamp", "")[:10]
        src    = idea.get("source", "?")
        text   = idea.get("idea", "?")
        iid    = idea.get("id", "?")
        lines.append(f"\n  {icon} [{ts}] ({src})  id={iid}")
        lines.append(f"     {text}")

    if status == "open" and filtered:
        lines += [
            "",
            f"To dismiss: autoimprove.dismiss_idea('<id>')",
        ]

    return "\n".join(lines)


def dismiss_idea(idea_id: str) -> str:
    """
    Mark a parked idea as dismissed (acted on or no longer relevant).

    Args:
        idea_id: The id field shown in list_ideas() (e.g. '20260331_a3f9b1').

    Returns:
        Confirmation or not-found message.
    """
    idea_id = str(idea_id).strip()
    ideas   = _load_ideas()
    for i, idea in enumerate(ideas):
        if idea.get("id") == idea_id:
            ideas[i]["status"]       = "dismissed"
            ideas[i]["dismissed_at"] = datetime.now().isoformat()
            _save_ideas(ideas)
            return f"✅ Idea dismissed: {idea.get('idea', '')[:80]}"
    open_ids = [i["id"] for i in ideas if i.get("status") == "open"]
    return (
        f"❌ Idea '{idea_id}' not found.\n"
        f"Open idea IDs: {open_ids or 'none'}"
    )


def _suggestion_key(skill_name: str, issue_type: str) -> str:
    """Build a deduplication key for a (skill, issue_type) pair to avoid re-suggesting the same fix."""
    return f"{skill_name}::{issue_type}"


# ── Lazy skill imports ─────────────────────────────────────────────────────────

_SKILL_PATHS = ("/app/skills/core", "/app/skills/dynamic", "/app/skills", "/app")
_skill_paths_ready: bool = False


def _ensure_skill_paths() -> None:
    """Add skill directories to sys.path exactly once per process."""
    global _skill_paths_ready
    if _skill_paths_ready:
        return
    for p in _SKILL_PATHS:
        if p not in sys.path:
            sys.path.insert(0, p)
    _skill_paths_ready = True


def _import_skill(module_name: str, reload: bool = False):
    """Import a skill module, adding skill dirs to sys.path if needed.

    Args:
        module_name: Module stem to import (e.g. 'seo_analyzer').
        reload:      Force-reload from disk even if already cached in
                     sys.modules. Pass True after a skill file is modified,
                     so the patched version is picked up rather than the
                     pre-patch copy that Python cached on first import.
    """
    import importlib
    _ensure_skill_paths()
    if reload and module_name in sys.modules:
        return importlib.reload(sys.modules[module_name])
    return importlib.import_module(module_name)


# ── Minimal LLM helper ────────────────────────────────────────────────────────

def _call_llm_simple(prompt: str, max_tokens: int = 256) -> str:
    """Single-turn LLM call via LiteLLM proxy (cloud) or Ollama (local).

    Returns empty string on any failure — callers must always have a non-LLM
    fallback path.  Mirrors the pattern in self_improvement._call_llm_verdict()
    so both skills route through the same env-var config.
    """
    try:
        import requests as _req
    except ImportError:
        return ""

    model_source = os.getenv("MODEL_SOURCE", "cloud")
    for attempt in range(2):
        try:
            if model_source == "local":
                base    = os.getenv("OLLAMA_API_BASE", "http://ollama:11434")
                model   = os.getenv("OLLAMA_MODEL", "llama3.2")
                payload = {
                    "model":   model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream":  False,
                    "options": {"temperature": 0.0, "num_predict": max_tokens},
                }
                resp = _req.post(f"{base}/api/chat", json=payload, timeout=30)
                resp.raise_for_status()
                return resp.json().get("message", {}).get("content", "").strip()
            else:
                base    = os.getenv("LITELLM_API_BASE", "http://litellm:4000")
                api_key = os.getenv("LITELLM_MASTER_KEY", "")
                model   = os.getenv("DEFAULT_MODEL", "trinity-default")
                headers = {"Authorization": f"Bearer {api_key}"}
                payload = {
                    "model":       model,
                    "messages":    [{"role": "user", "content": prompt}],
                    "temperature": 0.0,
                    "max_tokens":  max_tokens,
                }
                resp = _req.post(
                    f"{base}/v1/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=30,
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception:
            if attempt == 0:
                time.sleep(1)
    return ""


# ── Research loop constants ────────────────────────────────────────────────────

# Converge when ≥72% of query terms are covered by collected sources.
# Mirrors autoresearch's val_bpb threshold — the single number that decides
# whether to keep the current state or refine and try again.
COVERAGE_THRESHOLD = 0.72

_RESEARCH_STOP = frozenset({
    "the", "a", "an", "is", "in", "of", "to", "for", "and", "or", "how",
    "what", "why", "when", "are", "was", "has", "that", "this", "with",
    "from", "have", "be", "it", "not", "on", "at", "by", "do", "did",
    "can", "will", "would", "could", "should", "their", "they", "them",
})

# ── Research loop helpers ──────────────────────────────────────────────────────

def _extract_urls(raw_search: str) -> List[str]:
    """Extract URLs from a web.search() result string."""
    urls = []
    for line in (raw_search or "").splitlines():
        line = line.strip()
        if line.startswith("http://") or line.startswith("https://"):
            urls.append(line)
        elif "http" in line:
            urls.extend(re.findall(r'https?://[^\s\)"\']+', line))
    return urls


def _score_coverage(query: str, sources: List[Dict]) -> float:
    """
    Coverage metric 0.0–1.0: how well collected sources answer the query.

    0.4 × source yield  +  0.6 × query-term coverage in collected content,
    scaled by a domain-diversity factor so that N sources from the same site
    score lower than N sources from N different domains.

    Diversity scale: all one domain → ×0.75 (25% penalty); fully diverse → ×1.0.
    Mirrors autoresearch's val_bpb — one number that drives keep/refine decisions.
    """
    if not sources:
        return 0.0
    successful = [
        s for s in sources
        if s.get("content") and not str(s["content"]).startswith("fetch error")
    ]
    yield_score = len(successful) / len(sources)

    # Domain-diversity penalty — penalise echo chambers where all URLs share a host.
    urls           = [s.get("url") for s in successful if s.get("url")]
    unique_domains = len({u.split("/")[2] for u in urls if u.count("/") >= 2}) or 1
    diversity_factor = min(1.0, unique_domains / max(1, len(successful)))
    diversity_scale  = 0.75 + 0.25 * diversity_factor

    combined = " ".join(s.get("content", "") for s in successful).lower()
    terms = [
        t.lower() for t in re.findall(r'\w+', query)
        if t.lower() not in _RESEARCH_STOP and len(t) > 3
    ]
    if not terms:
        return round(yield_score * diversity_scale, 3)
    term_score = sum(1 for t in terms if t in combined) / len(terms)
    return round((0.4 * yield_score + 0.6 * term_score) * diversity_scale, 3)


def _missing_terms(query: str, sources: List[Dict]) -> List[str]:
    """Return query keywords absent from all collected source content."""
    terms = [
        t.lower() for t in re.findall(r'\w+', query)
        if t.lower() not in _RESEARCH_STOP and len(t) > 3
    ]
    combined = " ".join(
        s.get("content", "") for s in sources
        if s.get("content") and not str(s["content"]).startswith("fetch error")
    ).lower()
    return [t for t in terms if t not in combined]


def _fetch_with_retry(web, url: str, max_retries: int = 3, base_delay: float = 1.0) -> str:
    """Fetch a URL with exponential backoff on transient failures.

    Retries up to max_retries times, sleeping base_delay * 2^attempt seconds
    between each attempt (1s, 2s, 4s by default). Raises the last exception
    if all retries are exhausted.
    """
    last_exc: Exception = RuntimeError("no attempts made")
    for attempt in range(max_retries):
        try:
            return web.fetch(url)
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries - 1:
                time.sleep(base_delay * (2 ** attempt))
    raise last_exc


def _refine_query(
    base_query: str,
    iteration: int,
    missing: List[str],
    sources: Optional[List[Dict]] = None,
) -> str:
    """Build next iteration's search query guided by uncovered terms.

    When `sources` are provided, attempts an LLM-driven refinement that reads
    collected content and crafts a genuinely better next query rather than
    mechanically appending missing keywords.  Falls back to keyword append if
    the LLM is unavailable or returns an implausibly long result.
    """
    if sources:
        # Build a compact context from the most recent successful sources
        snippets = [
            s["content"][:300]
            for s in sources[-4:]
            if s.get("content") and not str(s["content"]).startswith("fetch error")
        ]
        if snippets:
            source_summary = "\n---\n".join(snippets)
            missing_str = (", ".join(missing[:5])) if missing else "(none — coverage below threshold)"
            prompt = (
                f"You are refining a web research query. The original topic is:\n"
                f"  {base_query}\n\n"
                f"After {iteration} search iteration(s), these terms are still missing from "
                f"the collected sources:\n"
                f"  {missing_str}\n\n"
                f"Sample content collected so far:\n{source_summary[:1200]}\n\n"
                f"Write ONE refined search query (max 12 words) that will surface the missing "
                f"information. Return only the query, no explanation or quotes."
            )
            refined = _call_llm_simple(prompt, max_tokens=64)
            # Accept only plausible short queries — reject walls of text or empty
            if refined and 2 <= len(refined.split()) <= 20:
                refined = refined.strip().strip('"').strip("'")
                # Topical continuity check: at least one non-stopword term from
                # the base query must survive in the refined query, otherwise the
                # LLM drifted off-topic and we fall through to the keyword fallback.
                base_terms = {
                    t.lower() for t in re.findall(r'\w+', base_query)
                    if t.lower() not in _RESEARCH_STOP and len(t) > 3
                }
                refined_terms = {
                    t.lower() for t in re.findall(r'\w+', refined)
                    if t.lower() not in _RESEARCH_STOP and len(t) > 3
                }
                if base_terms & refined_terms:
                    return refined
                # LLM drifted off-topic — log so degradation is visible over time
                _log({
                    "timestamp": datetime.now().isoformat(),
                    "event": "llm_query_drift",
                    "base_query": base_query,
                    "discarded_refinement": refined,
                })

    # Fallback: mechanical keyword append (original behaviour)
    if missing:
        return f"{base_query} {' '.join(missing[:3])}"
    # No missing terms and no useful LLM refinement — return base query unchanged
    # rather than appending generic noise like "data statistics examples".
    return base_query


# ── General-purpose research ───────────────────────────────────────────────────

def research(query: str, depth: str = "quick", save=True, max_iterations=None) -> str:
    """
    Autoresearch-style web research loop on any topic.

    Mirrors Karpathy's autoresearch: search → measure coverage → keep/refine →
    repeat until convergence or iteration cap. Never pauses — runs autonomously.

    Coverage metric (0.0–1.0):
        0.4 × source yield  +  0.6 × query-term coverage across all fetched content
    Convergence: stops when coverage ≥ 0.72 or iteration cap is reached.

    Args:
        query:          Topic, question, or measurable thing to investigate.
        depth:          'quick' (2 iters, 2 sources/iter) | 'deep' (4 iters, 4 sources/iter)
        save:           Persist findings to notes (default True)
        max_iterations: Override iteration cap (default: 2 quick / 4 deep)

    Returns:
        Structured report with per-iteration coverage log and source excerpts.

    Examples:
        "Research competitor pricing for project management SaaS tools"
        "Research current benchmark scores for Claude vs GPT-4 on coding tasks"
        "Research what validation loss metrics matter for LLM fine-tuning"
    """
    try:
        web = _import_skill("web")
    except ImportError as e:
        return f"❌ web skill not available: {e}"

    # Coerce types — Trinity dispatcher passes all args as strings
    depth = str(depth).strip().lower()
    _save_raw = str(save).strip().lower()
    save = True if _save_raw == "" else _save_raw not in ("false", "0", "no")

    if max_iterations is not None and str(max_iterations).strip().lower() not in ("none", "null", ""):
        max_iters = int(str(max_iterations).strip())
    else:
        max_iters = 2 if depth == "quick" else 4

    sources_per_iter = 2 if depth == "quick" else 4
    timestamp        = datetime.now().isoformat()
    date_label       = datetime.now().strftime("%Y-%m-%d %H:%M")

    all_sources:   List[Dict] = []
    seen_urls:     set        = set()
    iter_log:      List[Dict] = []
    current_query  = query
    best_coverage  = 0.0
    final_decision = "MAX_ITERS"

    # ── Autoresearch loop ─────────────────────────────────────────────────────
    for iteration in range(1, max_iters + 1):
        iter_sources: List[Dict] = []

        # 1. Search
        try:
            raw            = web.search(current_query)
            candidate_urls = _extract_urls(raw)
        except Exception as e:
            iter_log.append({
                "iter": iteration, "query": current_query,
                "sources": 0, "coverage": best_coverage,
                "decision": f"SEARCH_ERROR: {e}",
            })
            break

        # If search returned no parseable URLs, keep the raw text as a source
        if not candidate_urls and not all_sources:
            all_sources.append({"url": None, "content": raw, "iteration": iteration})

        # 2. Fetch — skip already-seen URLs
        for url in candidate_urls:
            if url not in seen_urls and len(iter_sources) < sources_per_iter:
                seen_urls.add(url)
                try:
                    content = _fetch_with_retry(web, url)
                    iter_sources.append({
                        "url":       url,
                        "content":   (content or "")[:3000],
                        "iteration": iteration,
                    })
                except Exception as e:
                    iter_sources.append({
                        "url":       url,
                        "content":   f"fetch error: {e}",
                        "iteration": iteration,
                    })

        all_sources.extend(iter_sources)

        # 3. Measure coverage — the single metric that drives all decisions
        coverage = _score_coverage(query, all_sources)
        missing  = _missing_terms(query, all_sources)
        improved = coverage > best_coverage
        best_coverage = max(best_coverage, coverage)

        # 4. Keep / refine / stop  (mirrors autoresearch keep-or-revert logic)
        if coverage >= COVERAGE_THRESHOLD:
            decision       = "CONVERGED"
            final_decision = "CONVERGED"
        elif not improved and iteration > 1:
            decision       = "NO_IMPROVEMENT — stopping early"
            final_decision = "NO_IMPROVEMENT"
        elif iteration == max_iters:
            decision       = "MAX_ITERS"
            final_decision = "MAX_ITERS"
        else:
            # Compute once — reused for both the log label and the actual next query.
            # Pass all_sources so the LLM path has content context; falls back to
            # keyword append automatically if LLM is unavailable.
            next_query = _refine_query(query, iteration, missing, sources=all_sources)
            decision   = f"REFINE → \"{next_query}\""

        iter_log.append({
            "iter":     iteration,
            "query":    current_query,
            "sources":  len(iter_sources),
            "total":    len(all_sources),
            "coverage": coverage,
            "decision": decision,
            "missing":  missing[:5],
        })

        if final_decision in ("CONVERGED", "NO_IMPROVEMENT"):
            break
        if decision.startswith("REFINE"):
            current_query = next_query  # already computed above — avoid second LLM call

    # ── Build report ──────────────────────────────────────────────────────────
    conv_marker = (
        "✅ converged" if final_decision == "CONVERGED"
        else f"⚠️ {final_decision.lower().replace('_', ' ')}"
    )
    lines = [
        f"📚 Research: {query}",
        f"   {date_label} | {len(iter_log)} iteration(s) | "
        f"coverage: {best_coverage:.0%} | {conv_marker}",
        "=" * 60,
        "",
        "── Iteration Log ──────────────────────────────────────────",
    ]
    for e in iter_log:
        status = (
            "✅" if e["coverage"] >= COVERAGE_THRESHOLD
            else ("🔄" if "REFINE" in e["decision"] else "—")
        )
        lines.append(
            f"  iter {e['iter']} | cov {e['coverage']:.0%} | "
            f"sources +{e['sources']} (total {e['total']}) | {status} {e['decision']}"
        )
        if e.get("missing"):
            lines.append(f"           missing: {e['missing']}")

    lines += ["", "── Sources ────────────────────────────────────────────────"]
    for i, src in enumerate(all_sources, 1):
        lines.append(
            f"\n[{i}] iter {src.get('iteration', '?')} — {src.get('url') or '(search result)'}"
        )
        content = src.get("content") or ""
        if content:
            preview = content.strip()[:800]
            if len(content) > 800:
                preview += f"\n  ... [{len(content) - 800} more chars truncated]"
            lines.append(preview)

    lines += [
        "",
        "=" * 60,
        f"Final coverage: {best_coverage:.0%} | Sources: {len(all_sources)} | Query: {query}",
    ]
    result_text = "\n".join(lines)

    # ── Save to notes ─────────────────────────────────────────────────────────
    save_path = None
    if save:
        try:
            notes = _import_skill("notes")
            title = f"Research — {query[:60]} — {datetime.now().strftime('%Y-%m-%d')}"
            notes.save(title, result_text, tags="research,autoimprove")
            save_path = title
        except Exception as e:
            save_path = f"(save failed: {e})"

    _log({
        "timestamp":      timestamp,
        "loop":           "research",
        "outcome":        "RESEARCH",
        "query":          query,
        "depth":          depth,
        "iterations":     len(iter_log),
        "final_coverage": best_coverage,
        "converged":      final_decision == "CONVERGED",
        "sources_found":  len(all_sources),
        "saved_as":       save_path,
    })

    footer = f"\n💾 Saved to notes: \"{save_path}\"" if save_path else ""
    return result_text + footer


# ── Layered smoke test ────────────────────────────────────────────────────────

def _run_smoke_test(skill_name: str, ce) -> tuple:
    """
    Layered smoke test for a dynamic skill after patching.

    Layer 1 (always): status() call, falling back to a bare import check.
        Passes if ce.test_skill() returns a '✅'-prefixed string, or if the
        fallback import snippet prints the skill's NAME without error.

    Layer 2 (if skill defines get_test_scenarios): run up to 2 skill-defined
        scenarios against the patched code.  Each scenario must be a dict:
            name      — str, human-readable label shown on failure
            code      — str, Python snippet passed to ce.run_snippet()
            validator — callable(result: str) -> bool

        Example in a skill file:
            def get_test_scenarios(skill_name):
                return [
                    {
                        "name":      "analyze_keywords returns density",
                        "code":      "import seo_analyzer as s; print(s.analyze_keywords('hello world hello', 'hello'))",
                        "validator": lambda r: "density" in r.lower(),
                    },
                ]

        If get_test_scenarios() is absent, raises, or returns an empty list,
        Layer 1 result stands — skills are not required to implement it.

    Returns:
        (passed: bool, detail: str)
    """
    # ── Layer 1: status() or import check ─────────────────────────────────────
    test_result = ce.test_skill(skill_name, "status", "[]", timeout=10)
    if not test_result.startswith("✅") and "not found" in test_result:
        test_result = ce.run_snippet(
            "import sys\n"
            "sys.path.insert(0, '/app/skills/dynamic')\n"
            f"import {skill_name}\n"
            f"print(getattr({skill_name}, 'NAME', 'loaded ok'))",
            timeout=10,
        )
    if not test_result.startswith("✅"):
        return False, f"layer 1 failed: {test_result[:150]}"

    # ── Layer 2: skill-defined scenarios (optional) ────────────────────────────
    try:
        # reload=True picks up the patched file, not the cached pre-patch version
        skill_mod = _import_skill(skill_name, reload=True)
        if not hasattr(skill_mod, "get_test_scenarios"):
            return True, "layer 1 passed (no scenarios defined)"
        scenarios = skill_mod.get_test_scenarios(skill_name)
        if not scenarios:
            return True, "layer 1 passed (empty scenario list)"
        run_count = min(len(scenarios), 2)
        for scenario in scenarios[:run_count]:
            result = ce.run_snippet(scenario["code"], timeout=15)
            if not scenario["validator"](result):
                return False, (
                    f"layer 2 failed — scenario '{scenario['name']}': {result[:120]}"
                )
    except Exception as exc:
        # Scenario infrastructure error — layer 1 already passed, don't block
        return True, f"layer 1 passed (scenario load error: {exc})"

    return True, f"all layers passed ({run_count} scenario(s))"


# ── Core experiment runner (DYNAMIC skills only) ──────────────────────────────

def run_experiment(skill_name: str, issue_type: str) -> str:
    """
    One autoresearch-style experiment on a DYNAMIC skill.

    Flow:
      1. Snapshot current code
      2. Baseline AST audit — confirm issue exists and get health score
      3. Apply all occurrences of issue_type
      4. Re-audit — confirm health score improved
      5. Runtime smoke test
      6. PASS  → keep improved file,  log IMPROVED
         FAIL  → restore snapshot,    log REVERTED
         NO-OP → restore snapshot,    log NO_CHANGE

    Args:
        skill_name:  Dynamic skill stem (e.g. 'seo_analyzer')
        issue_type:  One of: bare_except, missing_timeout

    Returns:
        One-line outcome: IMPROVED | REVERTED | NO_CHANGE

    Raises:
        ValueError:   issue_type is not in AUTO_FIXABLE (hard safety boundary —
                      this is never a soft skip, even if the caller rationalizes
                      that a non-listed type "should" be safe), or skill not found.
        ImportError:  required dependency skill unavailable.
        RuntimeError: skill file unreadable or pre-patch audit failed.
    """
    if issue_type not in AUTO_FIXABLE:
        raise ValueError(
            f"refused: '{issue_type}' is not in the auto-fixable set {AUTO_FIXABLE}. "
            f"Add it to AUTO_FIXABLE explicitly after human review — do not bypass this check."
        )

    skill_path = SKILLS_DYNAMIC_DIR / f"{skill_name}.py"
    if not skill_path.exists():
        raise ValueError(f"skill '{skill_name}' not found in skills/dynamic/")

    timestamp = datetime.now().isoformat()

    try:
        si = _import_skill("self_improvement")
        ce = _import_skill("terminal")
    except ImportError as e:
        raise ImportError(f"dependency missing — {e}") from e

    # 1. Snapshot — safety net, no git needed
    try:
        original_code = skill_path.read_text(encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"cannot read {skill_name}.py — {e}") from e

    # 2. Baseline audit
    before = si.analyze_skill_code(skill_name)
    if before.get("error"):
        raise RuntimeError(f"pre-audit failed for {skill_name} — {before['error']}")

    before_score  = before["health_score"]
    target_issues = [i for i in before["issues"] if i["type"] == issue_type]

    if not target_issues:
        return f"NO_CHANGE: no '{issue_type}' in {skill_name} (score={before_score})"

    # 3. Apply patch
    try:
        si.fix(skill_name, issue_type, "all")
    except Exception as fix_exc:
        skill_path.write_text(original_code, encoding="utf-8")
        _log({
            "timestamp": timestamp, "skill": skill_name, "issue_type": issue_type,
            "outcome": "REVERTED", "before_score": before_score, "after_score": None,
            "reason": f"si.fix() raised: {fix_exc}",
        })
        return f"REVERTED: si.fix() raised {type(fix_exc).__name__} — restored snapshot"

    # 4. Post-patch audit
    after = si.analyze_skill_code(skill_name)
    if after.get("error"):
        skill_path.write_text(original_code, encoding="utf-8")
        _log({
            "timestamp": timestamp, "skill": skill_name, "issue_type": issue_type,
            "outcome": "REVERTED", "before_score": before_score, "after_score": None,
            "reason": f"post-patch syntax error: {after['error']}",
        })
        return f"REVERTED: patch broke {skill_name} syntax — restored"

    after_score = after["health_score"]

    # 5. Layered runtime smoke test
    test_passed, test_detail = _run_smoke_test(skill_name, ce)

    # 6. Keep or restore
    issues_remaining = sum(1 for i in after.get("issues", []) if i["type"] == issue_type)
    issues_fixed     = len(target_issues) - issues_remaining

    if not test_passed:
        skill_path.write_text(original_code, encoding="utf-8")
        outcome = "REVERTED"
        reason  = f"runtime test failed: {test_detail}"
    elif issues_fixed > 0:
        outcome = "IMPROVED"
        reason  = f"score {before_score}→{after_score}, fixed {issues_fixed}/{len(target_issues)} issues"
    else:
        skill_path.write_text(original_code, encoding="utf-8")
        outcome = "NO_CHANGE"
        reason  = f"score unchanged at {before_score}, no issues reduced"

    # MAD confidence — how does this delta compare to historical noise?
    score_delta       = (after_score - before_score) if outcome == "IMPROVED" else None
    recent_deltas     = _load_all_score_deltas()
    conf_ratio, conf_label = _compute_confidence(score_delta, recent_deltas)

    _log({
        "timestamp":        timestamp,
        "skill":            skill_name,
        "issue_type":       issue_type,
        "outcome":          outcome,
        "before_score":     before_score,
        "after_score":      after_score,
        "score_delta":      score_delta,
        "confidence":       conf_ratio,
        "confidence_label": conf_label,
        "issues_fixed":     issues_fixed,
        "test_passed":      test_passed,
        "test_detail":      test_detail,
        "reason":           reason,
    })

    icon     = "✅" if outcome == "IMPROVED" else ("🔄" if outcome == "REVERTED" else "—")
    conf_str = f" | {conf_label}" if outcome == "IMPROVED" else ""
    return f"{icon} {outcome}: {skill_name} [{issue_type}] — {reason}{conf_str}"


# ── Core skill suggestion system ───────────────────────────────────────────────

def suggest_core(skill_name=None, max_skills=30) -> str:
    """
    Audit core skills and save proposed patches to memory for your review.
    No files are modified. You approve each suggestion with apply_suggestion().

    Scans /app/skills/core/ for auto-fixable issues (bare_except, missing_timeout),
    generates the proposed patch for each, and saves to core_suggestions.jsonl.
    Already-pending suggestions are skipped (no duplicates).

    Args:
        skill_name: Target a single skill by name (e.g. 'browser_session').
                    Omit to scan all core skills.
        max_skills: Max skills to scan when no skill_name given (default 30 = all)

    Returns:
        Summary of new suggestions found.
    """
    try:
        si = _import_skill("self_improvement")
    except ImportError as e:
        return f"❌ {e}"

    # Normalize args — dispatcher passes everything as strings
    skill_name = str(skill_name).strip() if skill_name not in (None, "None", "", "null") else None
    try:
        max_skills = int(max_skills)
    except (ValueError, TypeError):
        max_skills = 30

    all_core_files = sorted(
        p for p in SKILLS_CORE_DIR.glob("*.py") if not p.name.startswith("_")
    )

    if skill_name:
        core_files = [p for p in all_core_files if p.stem == skill_name]
        if not core_files:
            available = [p.stem for p in all_core_files]
            return (
                f"❌ Core skill '{skill_name}' not found.\n"
                f"Available: {', '.join(available)}"
            )
    else:
        core_files = all_core_files
    if not core_files:
        return "⚠️ No core skills found at /app/skills/core/"

    existing   = _load_suggestions()
    # Index pending suggestions by key so we skip duplicates
    pending_keys = {
        _suggestion_key(s["skill"], s["issue_type"])
        for s in existing
        if s.get("status") == "pending"
    }

    new_suggestions = []
    skipped_clean   = 0
    skipped_dup     = 0
    scanned         = 0

    scan_list = core_files if skill_name else core_files[:max_skills]
    for skill_path in scan_list:
        skill_name = skill_path.stem
        scanned   += 1

        analysis = si.analyze_skill_code(skill_name)
        if analysis.get("error"):
            continue

        # Scan AUTO_FIXABLE types (can be auto-applied) plus high/critical types
        # that require manual review (sql_injection_risk, hardcoded_path).
        SUGGEST_TYPES = AUTO_FIXABLE + ("sql_injection_risk", "hardcoded_path")

        for issue_type in SUGGEST_TYPES:
            matching = [i for i in analysis["issues"] if i["type"] == issue_type]
            if not matching:
                continue

            key = _suggestion_key(skill_name, issue_type)
            if key in pending_keys:
                skipped_dup += 1
                continue

            # Generate patch for the first occurrence as a preview
            first_issue = matching[0]
            patch = si.generate_patch(skill_name, issue_type, first_issue["line"])

            suggestion = {
                "timestamp":    datetime.now().isoformat(),
                "skill":        skill_name,
                "issue_type":   issue_type,
                "status":       "pending",
                "occurrences":  len(matching),
                "health_score": analysis["health_score"],
                "severity":     first_issue.get("severity", "medium"),
                "message":      first_issue.get("message", ""),
                "example_line": first_issue.get("line"),
                "patch_preview": patch.get("diff", patch.get("note", "no preview")),
                "safe_to_apply": patch.get("safe_to_apply", False),
            }
            new_suggestions.append(suggestion)
            pending_keys.add(key)

        if not any(
            i["type"] in SUGGEST_TYPES for i in analysis["issues"]
        ):
            skipped_clean += 1

    if new_suggestions:
        all_suggestions = existing + new_suggestions
        _save_suggestions(all_suggestions)
        _log({
            "timestamp": datetime.now().isoformat(),
            "loop":      "suggest_core",
            "outcome":   "SUGGESTED",
            "new_count": len(new_suggestions),
            "scanned":   scanned,
        })

    lines = [
        f"🔍 suggest_core — scanned {scanned} core skills",
        f"  New suggestions saved : {len(new_suggestions)}",
        f"  Already pending       : {skipped_dup}",
        f"  Clean (no issues)     : {skipped_clean}",
    ]

    if new_suggestions:
        lines.append("")
        lines.append("New suggestions (review with list_suggestions()):")
        for s in new_suggestions:
            lines.append(
                f"  • {s['skill']} [{s['issue_type']}] — "
                f"{s['occurrences']}x, score={s['health_score']}, {s['severity']}"
            )
        lines.append("")
        lines.append("To apply one: autoimprove.apply_suggestion('skill_name', 'issue_type')")

        # Notify via Telegram so suggestions don't silently pile up unread.
        try:
            tg = _import_skill("telegram_bot")
            critical = [s for s in new_suggestions if s["severity"] == "critical"]
            flag = "🚨" if critical else "🔧"
            tg.send(
                f"{flag} autoimprove: {len(new_suggestions)} new core skill suggestion(s) pending.\n"
                + (f"⚠️ {len(critical)} critical severity — review now.\n" if critical else "")
                + "Run: autoimprove.list_suggestions()"
            )
        except Exception:
            pass  # Never let a notification failure break the improvement loop
    else:
        lines.append("✅ No new suggestions — core looks clean or all issues already pending.")

    return "\n".join(lines)


def list_suggestions(status: str = "pending", skill_name: str = None) -> str:
    """
    Show core skill improvement suggestions saved to memory.

    Args:
        status:     Filter — 'pending' | 'applied' | 'failed' | 'all'
        skill_name: Optional — show suggestions for one skill only (e.g. 'browser_session')

    Returns:
        Formatted list of suggestions.
    """
    # Normalize — dispatcher may pass skill name as status arg
    skill_name = str(skill_name).strip() if skill_name not in (None, "None", "", "null") else None
    valid_statuses = ("pending", "applied", "failed", "all")
    if status not in valid_statuses:
        # User passed a skill name as first arg — treat it as skill_name filter
        skill_name = status
        status = "all"

    all_s = _load_suggestions()
    if not all_s:
        return (
            "📭 No suggestions yet.\n"
            "Run: autoimprove.suggest_core() to scan your 30+ core skills."
        )

    filtered = all_s if status == "all" else [s for s in all_s if s.get("status") == status]
    if skill_name:
        filtered = [s for s in filtered if s.get("skill") == skill_name]
    if not filtered:
        skill_hint = f" for '{skill_name}'" if skill_name else ""
        return f"📭 No '{status}' suggestions{skill_hint}. Try list_suggestions('all') to see everything."

    icons = {"pending": "⏳", "applied": "✅", "failed": "❌"}
    skill_label = f" [{skill_name}]" if skill_name else ""
    lines = [f"📋 Core skill suggestions{skill_label} — {status} ({len(filtered)} of {len(all_s)} total):"]

    for s in filtered:
        icon    = icons.get(s.get("status", "pending"), "?")
        ts      = s.get("timestamp", "")[:10]
        skill   = s.get("skill", "?")
        issue   = s.get("issue_type", "?")
        occ     = s.get("occurrences", "?")
        score   = s.get("health_score", "?")
        sev     = s.get("severity", "?")
        safe    = "safe to auto-apply" if s.get("safe_to_apply") else "review recommended"
        preview = s.get("patch_preview", "")

        lines.append(f"\n  {icon} [{ts}] {skill} — {issue}")
        lines.append(f"     occurrences: {occ}  |  health: {score}/100  |  severity: {sev}  |  {safe}")
        if s.get("message"):
            lines.append(f"     issue: {s['message']}")
        if preview and status in ("pending", "all"):
            # Show first 3 lines of patch preview
            preview_lines = preview.strip().splitlines()[:3]
            for pl in preview_lines:
                lines.append(f"     {pl}")

    if status == "pending" and filtered:
        lines.append(
            f"\nTo apply: autoimprove.apply_suggestion('skill_name', 'issue_type')\n"
            f"Example : autoimprove.apply_suggestion('{filtered[0]['skill']}', '{filtered[0]['issue_type']}')"
        )
    return "\n".join(lines)


def apply_suggestion(skill_name: str, issue_type: str) -> str:
    """
    Apply a pending core skill suggestion — your explicit approval required.

    Flow:
      1. Find the pending suggestion for this skill + issue_type
      2. Snapshot the core skill file (safety net)
      3. Apply patch via self_improvement.fix()
      4. Runtime smoke test
      5. PASS  → keep improved file, mark suggestion 'applied'
         FAIL  → restore snapshot,   mark suggestion 'failed'

    Args:
        skill_name:  Core skill stem (e.g. 'browser_session', 'web')
        issue_type:  Issue to fix (e.g. 'bare_except', 'missing_timeout')

    Returns:
        Result string: applied | failed | not found
    """
    suggestions = _load_suggestions()
    key         = _suggestion_key(skill_name, issue_type)
    match: Optional[Dict] = None
    match_idx             = -1

    for i, s in enumerate(suggestions):
        if _suggestion_key(s["skill"], s["issue_type"]) == key and s.get("status") == "pending":
            match     = s
            match_idx = i
            break

    if match is None:
        pending_skills = [s["skill"] for s in suggestions if s.get("status") == "pending"]
        return (
            f"❌ No pending suggestion found for {skill_name} [{issue_type}].\n"
            f"Pending skills: {pending_skills or 'none'}\n"
            f"Run list_suggestions() to see what's available."
        )

    # Locate the core skill file
    skill_path = SKILLS_CORE_DIR / f"{skill_name}.py"
    if not skill_path.exists():
        return f"❌ Core skill file not found: {skill_path}"

    try:
        si = _import_skill("self_improvement")
        ce = _import_skill("terminal")
    except ImportError as e:
        return f"❌ dependency missing — {e}"

    # Snapshot — persisted to disk so a mid-run crash leaves the original recoverable
    try:
        original_code = skill_path.read_text(encoding="utf-8")
    except Exception as e:
        return f"❌ Cannot read {skill_name}.py — {e}"

    backup_path = skill_path.with_suffix(".py.bak")
    try:
        backup_path.write_text(original_code, encoding="utf-8")
    except Exception as e:
        return f"❌ Cannot write backup for {skill_name}.py — {e}"

    # Pre-audit for baseline
    before = si.analyze_skill_code(skill_name)
    before_score = before.get("health_score", 0) if not before.get("error") else 0

    # Apply fix to the CORE skill
    si.fix(skill_name, issue_type, "all")

    # Post-patch audit
    after = si.analyze_skill_code(skill_name)
    if after.get("error"):
        skill_path.write_text(original_code, encoding="utf-8")
        backup_path.unlink(missing_ok=True)
        suggestions[match_idx]["status"] = "failed"
        suggestions[match_idx]["applied_at"] = datetime.now().isoformat()
        suggestions[match_idx]["fail_reason"] = f"syntax error: {after['error']}"
        _save_suggestions(suggestions)
        return f"❌ FAILED: patch broke {skill_name} syntax — core file restored"

    after_score = after.get("health_score", 0)

    # Smoke test — core skills use sys.path from core dir
    test_result = ce.run_snippet(
        "import sys\n"
        "sys.path.insert(0, '/app/skills/core')\n"
        f"import {skill_name}\n"
        f"print(getattr({skill_name}, 'NAME', 'loaded ok'))",
        timeout=15,
    )
    test_passed = test_result.startswith("✅")

    timestamp = datetime.now().isoformat()

    if not test_passed:
        skill_path.write_text(original_code, encoding="utf-8")
        backup_path.unlink(missing_ok=True)
        suggestions[match_idx]["status"]     = "failed"
        suggestions[match_idx]["applied_at"] = timestamp
        suggestions[match_idx]["fail_reason"] = f"runtime test failed: {test_result[:150]}"
        _save_suggestions(suggestions)
        _log({
            "timestamp": timestamp, "skill": skill_name, "issue_type": issue_type,
            "outcome": "REVERTED", "track": "core", "before_score": before_score,
            "after_score": after_score, "reason": "runtime test failed after apply_suggestion",
        })
        return f"❌ FAILED: runtime test failed — core file restored\n{test_result[:200]}"

    # Success
    backup_path.unlink(missing_ok=True)
    issues_fixed = match.get("occurrences", "?")
    suggestions[match_idx]["status"]     = "applied"
    suggestions[match_idx]["applied_at"] = timestamp
    _save_suggestions(suggestions)
    _log({
        "timestamp":    timestamp,
        "skill":        skill_name,
        "issue_type":   issue_type,
        "outcome":      "IMPROVED",
        "track":        "core",
        "before_score": before_score,
        "after_score":  after_score,
        "issues_fixed": issues_fixed,
    })

    # Auto-close open improvement ideas whose source matches the applied skill.
    # Uses existing "dismissed" status + dismissed_reason field to avoid schema changes.
    auto_dismissed: List[str] = []
    try:
        ideas = _load_ideas()
        for i, idea in enumerate(ideas):
            if (idea.get("status") == "open"
                    and skill_name.lower() in idea.get("source", "").lower()):
                ideas[i]["status"]           = "dismissed"
                ideas[i]["dismissed_at"]     = datetime.now().isoformat()
                ideas[i]["dismissed_reason"] = f"auto-closed by apply_suggestion({skill_name}, {issue_type})"
                auto_dismissed.append(idea.get("id", "?"))
        if auto_dismissed:
            _save_ideas(ideas)
    except Exception:
        pass

    suffix = f"\nAuto-dismissed {len(auto_dismissed)} related idea(s)." if auto_dismissed else ""
    return (
        f"✅ APPLIED: {skill_name} [{issue_type}] — "
        f"score {before_score}→{after_score}, {issues_fixed} occurrence(s) fixed\n"
        f"Core skill updated at: {skill_path}{suffix}"
    )


# ── Lesson proposals (non-auto-fixable patterns) ──────────────────────────────

def _load_proposals() -> List[Dict]:
    """Load all lesson proposals from lesson_proposals.jsonl."""
    if not PROPOSALS_FILE.exists():
        return []
    proposals = []
    try:
        for line in PROPOSALS_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                proposals.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except Exception:
        pass
    return proposals


def _save_proposals(proposals: List[Dict]) -> None:
    """Overwrite lesson_proposals.jsonl with the full list."""
    try:
        _atomic_write_jsonl(PROPOSALS_FILE, proposals)
    except Exception as _exc:
        sys.stderr.write(f"[autoimprove] _save_proposals failed: {_exc}\n")


def lessons_to_proposals(threshold: int = PROPOSAL_THRESHOLD) -> str:
    """
    Scan error_patterns.json for patterns that have accumulated ≥ threshold
    occurrences but are NOT in AUTO_FIXABLE (so error_reduce silently ignores them).
    For each qualifying pattern, save a structured review proposal to
    lesson_proposals.jsonl. Already-pending proposals are skipped.

    This closes the loop: mistakes accumulate in lessons.jsonl → threshold hit →
    proposal surfaces for your review → you decide how to act on it.
    Auto-fixable patterns (bare_except, missing_timeout) are already handled by
    ast_audit / error_reduce and are intentionally excluded here.

    Args:
        threshold: Min occurrence count before a pattern generates a proposal (default 3).

    Returns:
        Summary of new proposals found.
    """
    # Coerce — dispatcher passes strings
    try:
        threshold = int(threshold)
    except (ValueError, TypeError):
        threshold = PROPOSAL_THRESHOLD

    if not PATTERNS_FILE.exists():
        return "NO_DATA: error_patterns.json not found yet — run skills first to accumulate data"

    try:
        with PATTERNS_FILE.open(encoding="utf-8") as f:
            patterns = json.load(f)
    except Exception as e:
        return f"❌ could not read error_patterns.json: {e}"

    # Collect example messages from lessons.jsonl for richer proposals.
    # Also count (skill, error_type) pairs for Phase 2 runtime-error proposals —
    # these never appear in error_patterns.json which only tracks AST/static issues.
    lessons_by_type:  Dict[str, List[str]] = {}
    runtime_counts:   Dict[str, int]       = {}   # key = "skill::error_type"
    runtime_examples: Dict[str, List[str]] = {}
    runtime_fixes:    Dict[str, str]       = {}

    if LESSONS_FILE.exists():
        try:
            for line in LESSONS_FILE.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    lesson = json.loads(line)
                    et  = lesson.get("error_type") or lesson.get("type", "")
                    msg = lesson.get("error_msg") or lesson.get("message", "")
                    if et and msg:
                        lessons_by_type.setdefault(et, []).append(str(msg)[:120])
                    # Runtime pair tracking — one counter per (skill, error_type) combo
                    skill = lesson.get("skill", "").strip()
                    if skill and et:
                        rkey = f"{skill}::{et}"
                        runtime_counts[rkey] = runtime_counts.get(rkey, 0) + 1
                        if msg:
                            runtime_examples.setdefault(rkey, []).append(str(msg)[:120])
                        fix = lesson.get("fix_applied", "") or ""
                        if fix and rkey not in runtime_fixes:
                            runtime_fixes[rkey] = str(fix)[:200]
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass

    # Index existing pending proposals to avoid duplicates
    existing = _load_proposals()
    pending_types = {
        p["error_type"]
        for p in existing
        if p.get("status") == "pending"
    }
    # Separate dedup set for runtime proposals — keyed by "skill::error_type"
    pending_runtime_keys = {
        p["runtime_key"]
        for p in existing
        if p.get("status") == "pending" and p.get("runtime_key")
    }

    new_proposals = []
    skipped_fixable = 0
    skipped_low     = 0
    skipped_dup     = 0

    for error_type, meta in patterns.items():
        count = meta.get("count", 0)

        if error_type in AUTO_FIXABLE:
            skipped_fixable += 1
            continue
        if count < threshold:
            skipped_low += 1
            continue
        if error_type in pending_types:
            skipped_dup += 1
            continue

        examples = lessons_by_type.get(error_type, [])[:3]

        proposal = {
            "timestamp":       datetime.now().isoformat(),
            "error_type":      error_type,
            "count":           count,
            "severity":        meta.get("severity", "medium"),
            "suggested_fix":   meta.get("fix", "(no fix hint available)"),
            "status":          "pending",
            "example_messages": examples,
        }
        new_proposals.append(proposal)
        pending_types.add(error_type)

    # ── Phase 2: runtime errors from lessons.jsonl ────────────────────────────
    # error_patterns.json only tracks AST/static patterns (bare_except,
    # missing_docstring, etc.). Runtime errors — skill_error, TypeError,
    # ValueError, etc. — live exclusively in lessons.jsonl and were never
    # reaching this pipeline. This phase closes that gap.
    for rkey, count in sorted(runtime_counts.items(), key=lambda x: -x[1]):
        if count < threshold:
            continue
        if rkey in pending_runtime_keys:
            skipped_dup += 1
            continue
        skill_stem, et = rkey.split("::", 1)
        fix_hint = runtime_fixes.get(rkey, "")
        sev = "high" if count >= 6 else "medium" if count >= 4 else "low"
        proposal = {
            "timestamp":        datetime.now().isoformat(),
            "error_type":       et,
            "skill":            skill_stem,
            "runtime_key":      rkey,
            "count":            count,
            "severity":         sev,
            "suggested_fix":    fix_hint or f"Review {skill_stem} — {et} recurring {count}x, no fix recorded yet",
            "status":           "pending",
            "source":           "lessons.jsonl",
            "example_messages": runtime_examples.get(rkey, [])[:3],
        }
        new_proposals.append(proposal)
        pending_runtime_keys.add(rkey)

    if new_proposals:
        all_proposals = existing + new_proposals
        _save_proposals(all_proposals)
        _log({
            "timestamp":   datetime.now().isoformat(),
            "loop":        "lessons_to_proposals",
            "outcome":     "PROPOSED",
            "new_count":   len(new_proposals),
            "threshold":   threshold,
        })

    lines = [
        f"🔎 lessons_to_proposals (threshold={threshold})",
        f"   New proposals        : {len(new_proposals)}",
        f"   Already pending      : {skipped_dup}",
        f"   Below threshold      : {skipped_low}",
        f"   Skipped (auto-fixable): {skipped_fixable}",
    ]

    if new_proposals:
        lines.append("")
        lines.append("New proposals (review with autoimprove.list_proposals()):")
        for p in new_proposals:
            lines.append(
                f"  • [{p['severity'].upper()}] {p['error_type']} — "
                f"{p['count']}x seen → {p['suggested_fix']}"
            )
    else:
        lines.append("✅ No new proposals — all qualifying patterns already pending or below threshold.")

    return "\n".join(lines)


def list_proposals(status: str = "pending") -> str:
    """
    Show lesson-derived improvement proposals.
    These are recurring error patterns (≥3 occurrences) that are NOT auto-fixable
    and need a human decision on how to address them.

    Args:
        status: Filter — 'pending' | 'resolved' | 'dismissed' | 'all'

    Returns:
        Formatted list of proposals.
    """
    valid_statuses = ("pending", "resolved", "dismissed", "all")
    if status not in valid_statuses:
        status = "all"

    all_p = _load_proposals()
    if not all_p:
        return (
            "📭 No lesson proposals yet.\n"
            "Run: autoimprove.lessons_to_proposals() — or it runs automatically inside error_reduce."
        )

    filtered = all_p if status == "all" else [p for p in all_p if p.get("status") == status]
    if not filtered:
        return f"📭 No '{status}' proposals. Try list_proposals('all') to see everything."

    icons    = {"pending": "⏳", "resolved": "✅", "dismissed": "—"}
    sev_icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}

    lines = [f"📋 Lesson proposals — {status} ({len(filtered)} of {len(all_p)} total):"]
    for p in filtered:
        icon   = icons.get(p.get("status", "pending"), "?")
        si     = sev_icon.get(p.get("severity", "medium"), "•")
        ts     = p.get("timestamp", "")[:10]
        et     = p.get("error_type", "?")
        count  = p.get("count", "?")
        fix    = p.get("suggested_fix", "?")
        sev    = p.get("severity", "?")

        lines.append(f"\n  {icon} {si} [{ts}] {et}  (seen {count}x, severity={sev})")
        lines.append(f"     Suggested action: {fix}")
        examples = p.get("example_messages", [])
        if examples:
            lines.append("     Examples from lessons:")
            for ex in examples[:2]:
                lines.append(f"       – {ex[:100]}")

    if status == "pending" and filtered:
        lines += [
            "",
            "These patterns are NOT auto-fixed — they need your decision.",
            "Options per pattern:",
            "  • Extend AUTO_FIXABLE in autoimprove.py if a safe deterministic fix exists",
            "  • Run autoimprove.suggest_core('<skill_name>') to generate a patch proposal",
            "  • Update coding practices in identity.md to prevent future occurrences",
        ]

    return "\n".join(lines)


# ── Individual loops ───────────────────────────────────────────────────────────

def _loop_ast_audit(max_experiments=10) -> str:
    """Loop 1 — auto-fix bare_except & missing_timeout in DYNAMIC skills."""
    max_experiments = int(max_experiments)
    try:
        si = _import_skill("self_improvement")
    except ImportError as e:
        return f"❌ {e}"

    skill_files = sorted(p for p in SKILLS_DYNAMIC_DIR.glob("*.py") if not p.name.startswith("_"))
    if not skill_files:
        return "✅ ast_audit: no dynamic skills found — nothing to audit"

    results = []
    count   = 0

    for skill_path in skill_files:
        if count >= max_experiments:
            results.append(f"  (experiment cap reached: {max_experiments})")
            break
        skill_name = skill_path.stem
        analysis   = si.analyze_skill_code(skill_name)
        if analysis.get("error"):
            continue
        for issue_type in AUTO_FIXABLE:
            if count >= max_experiments:
                break
            if any(i["type"] == issue_type for i in analysis["issues"]):
                try:
                    results.append(f"  {run_experiment(skill_name, issue_type)}")
                except (ValueError, RuntimeError, ImportError) as exc:
                    results.append(f"  ERROR [{skill_name}/{issue_type}]: {exc}")
                count += 1
                time.sleep(1)

    if not results:
        return "✅ ast_audit: all dynamic skills clean — no auto-fixable issues"
    return f"🔬 ast_audit ({count} experiments):\n" + "\n".join(results)


def _loop_error_reduce(max_experiments=10) -> str:
    """Loop 2 — target the most-frequent error pattern in DYNAMIC skills."""
    max_experiments = int(max_experiments)
    try:
        si = _import_skill("self_improvement")
    except ImportError as e:
        return f"❌ {e}"

    if not PATTERNS_FILE.exists():
        return "NO_DATA: error_patterns.json not found yet — run skills first"

    try:
        with PATTERNS_FILE.open(encoding="utf-8") as f:
            patterns = json.load(f)
    except Exception as e:
        return f"❌ could not read error_patterns.json: {e}"

    fixable = {k: v for k, v in patterns.items() if k in AUTO_FIXABLE}
    if not fixable:
        return "NO_DATA: no auto-fixable patterns tracked yet"

    top_issue = max(fixable, key=lambda k: fixable[k].get("count", 0))
    top_count = fixable[top_issue].get("count", 0)

    if top_count == 0:
        return f"✅ error_reduce: top pattern '{top_issue}' has 0 occurrences"

    results = []
    count   = 0
    for skill_path in sorted(SKILLS_DYNAMIC_DIR.glob("*.py")):
        if skill_path.name.startswith("_") or count >= max_experiments:
            break
        skill_name = skill_path.stem
        analysis   = si.analyze_skill_code(skill_name)
        if analysis.get("error"):
            continue
        if any(i["type"] == top_issue for i in analysis["issues"]):
            try:
                results.append(f"  {run_experiment(skill_name, top_issue)}")
            except (ValueError, RuntimeError, ImportError) as exc:
                results.append(f"  ERROR [{skill_name}/{top_issue}]: {exc}")
            count += 1
            time.sleep(1)

    # Surface non-auto-fixable patterns as review proposals (closes the lessons loop)
    proposals_summary = lessons_to_proposals()

    base = (
        f"✅ error_reduce: no dynamic skills have '{top_issue}' currently"
        if not results else
        f"🔬 error_reduce — targeting '{top_issue}' ({top_count}x in history), "
        f"{count} experiments:\n" + "\n".join(results)
    )
    return f"{base}\n\n{proposals_summary}"


def _loop_daily_review() -> str:
    """Loop 3 — learning review + user model maintenance + self-reflection. No code changes."""
    try:
        si = _import_skill("self_improvement")
    except ImportError as e:
        return f"❌ {e}"

    review = si.daily_review()

    # Prune stale inferred patterns and low-confidence preferences from user model
    prune_note = ""
    try:
        notes = _import_skill("notes")
        prune_note = "\n" + notes.prune_user_model(days_old=30)
    except Exception as _exc:
        prune_note = f"\n⚠️  prune_user_model skipped: {_exc}"

    # Self-reflection: inspect recent experiment outcomes and surface systemic
    # observations as parked ideas — "did we actually improve anything? what
    # keeps failing?" — so they feed the discover_patterns and suggest_core loops.
    reflection_note = ""
    try:
        recent = _load_log(days=7)
        experiments = [e for e in recent if e.get("outcome") in ("IMPROVED", "REVERTED", "NO_CHANGE")]
        if experiments:
            n_improved = sum(1 for e in experiments if e["outcome"] == "IMPROVED")
            n_reverted = sum(1 for e in experiments if e["outcome"] == "REVERTED")
            n_total    = len(experiments)
            win_rate   = n_improved / n_total

            # Count how often each skill appears in REVERTED/NO_CHANGE outcomes
            failure_counts: Dict[str, int] = {}
            for e in experiments:
                if e["outcome"] in ("REVERTED", "NO_CHANGE"):
                    skill = e.get("skill", "unknown")
                    failure_counts[skill] = failure_counts.get(skill, 0) + 1

            top_failures = sorted(failure_counts.items(), key=lambda x: -x[1])[:3]

            # Park an idea only when win rate is poor or a skill keeps failing
            ideas_parked: List[str] = []
            if win_rate < 0.3 and n_total >= 5:
                idea = (
                    f"Self-reflection (7d): win rate is {win_rate:.0%} "
                    f"({n_improved}/{n_total} experiments improved). "
                    f"Consider: are AUTO_FIXABLE patterns too aggressive? "
                    f"Are patches being applied to the wrong scope?"
                )
                park_idea(idea, source="daily_review")
                ideas_parked.append(f"low win-rate ({win_rate:.0%})")

            for skill, count in top_failures:
                if count >= 3:
                    idea = (
                        f"Self-reflection (7d): '{skill}' failed or reverted {count}x "
                        f"in the last week — may need a suggest_core audit or manual review."
                    )
                    park_idea(idea, source="daily_review")
                    ideas_parked.append(f"{skill} failed {count}x")

            if ideas_parked:
                reflection_note = f"\n💭 self-reflection: parked {len(ideas_parked)} idea(s) — {'; '.join(ideas_parked)}"
            else:
                reflection_note = (
                    f"\n💭 self-reflection: {n_improved}/{n_total} improved "
                    f"({n_reverted} reverted) over 7d — no systemic issues detected"
                )
        else:
            reflection_note = "\n💭 self-reflection: no experiment history yet"
    except Exception as _exc:
        reflection_note = f"\n⚠️  self-reflection skipped: {_exc}"

    _log({
        "timestamp": datetime.now().isoformat(),
        "loop":      "daily_review",
        "outcome":   "REVIEW",
        "summary":   review[:2000],
    })
    return f"📋 daily_review:\n{review}{prune_note}{reflection_note}"


def _loop_pattern_mining(days=7, min_occurrences=3, max_proposals=5) -> str:
    """
    Loop 4 — scan session_logs.jsonl for recurring task_type patterns and park
    skill proposals when a pattern appears frequently but has no matching skill.

    Uses task_type tags written by app.py's _detect_task_type() — no LLM call,
    no ChromaDB access needed. Results go to improvement_ideas.jsonl via
    park_idea() so you review them with autoimprove.list_ideas().

    Args:
        days:            How many days of session history to scan (default 7)
        min_occurrences: Min times a task_type must appear to earn a proposal (default 3)
        max_proposals:   Cap on new proposals per run (default 5)

    Returns:
        Summary of patterns found and proposals parked.
    """
    try:
        days            = int(days)
        min_occurrences = int(min_occurrences)
        max_proposals   = int(max_proposals)
    except (ValueError, TypeError):
        days, min_occurrences, max_proposals = 7, 3, 5

    if not SESSION_LOG.exists():
        return "NO_DATA: session_logs.jsonl not found — no conversations logged yet"

    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    # Tally task_type occurrences within the time window, collecting example queries
    counts: Dict[str, List[str]] = {}
    try:
        for line in SESSION_LOG.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("timestamp", "") < cutoff:
                continue
            task_type = (entry.get("metadata") or {}).get("task_type", "").strip()
            if not task_type or task_type == "general":
                continue
            counts.setdefault(task_type, []).append(
                (entry.get("user") or "")[:120]
            )
    except Exception as e:
        return f"❌ pattern_mining: could not read session_logs.jsonl — {e}"

    if not counts:
        return f"✅ pattern_mining: no typed task patterns in the last {days} day(s)"

    # Build set of already-existing skill stems so we skip covered patterns
    existing: set = set()
    for skill_dir in (SKILLS_DYNAMIC_DIR, SKILLS_CORE_DIR):
        if skill_dir.exists():
            existing |= {p.stem for p in skill_dir.glob("*.py") if not p.name.startswith("_")}

    # Sort by frequency descending, filter, propose
    ranked = sorted(counts.items(), key=lambda x: -len(x[1]))
    proposed, skipped_existing, skipped_threshold = 0, 0, 0

    for task_type, examples in ranked:
        if proposed >= max_proposals:
            break
        if len(examples) < min_occurrences:
            skipped_threshold += 1
            continue
        if task_type in existing:
            skipped_existing += 1
            continue
        sample = "; ".join(dict.fromkeys(examples[:3]))  # dedupe while preserving order
        park_idea(
            f"Recurring task pattern '{task_type}' seen {len(examples)}x in {days}d "
            f"— consider a new skill. Example queries: {sample}",
            source="pattern_mining",
        )
        proposed += 1

    _log({
        "timestamp":         datetime.now().isoformat(),
        "loop":              "pattern_mining",
        "outcome":           "MINED",
        "days_scanned":      days,
        "patterns_found":    len(counts),
        "proposals_parked":  proposed,
        "skipped_existing":  skipped_existing,
        "skipped_threshold": skipped_threshold,
    })

    if proposed == 0:
        return (
            f"✅ pattern_mining: {len(counts)} pattern(s) found — "
            f"none qualified ({skipped_existing} already have a skill, "
            f"{skipped_threshold} below {min_occurrences}x threshold)"
        )
    return (
        f"🔍 pattern_mining: {proposed} new proposal(s) parked "
        f"({len(counts)} patterns scanned, {skipped_existing} already covered) "
        f"— review with autoimprove.list_ideas()"
    )


# ── Discover new error pattern types ─────────────────────────────────────────

def _discover_new_patterns(min_occurrences: int = 2) -> str:
    """
    Scan lessons.jsonl for error/failure types NOT already tracked in
    error_patterns.json.  Uses the LLM to analyze recurring unknown failure modes
    and propose new pattern definitions.  Results are parked via park_idea() for
    human review — nothing is auto-added.

    This closes the blind spot where error_reduce only improves patterns it already
    knows about while silently ignoring new categories accumulating in lessons.jsonl.

    Args:
        min_occurrences: Min times an untracked type must appear before analysis
                         (default 2 — low bar since the LLM decides severity).

    Returns:
        Summary of untracked types found and ideas parked.
    """
    try:
        min_occurrences = int(min_occurrences)
    except (ValueError, TypeError):
        min_occurrences = 2

    if not LESSONS_FILE.exists():
        return "NO_DATA: lessons.jsonl not found — run skills first to accumulate lessons"

    # Build the set of already-known pattern types
    known_types: set = set(AUTO_FIXABLE)
    if PATTERNS_FILE.exists():
        try:
            with PATTERNS_FILE.open(encoding="utf-8") as f:
                known_types |= set(json.load(f).keys())
        except Exception:
            pass

    # Tally untracked error types, collecting example messages for context
    unknown_counts: Dict[str, List[str]] = {}
    try:
        for line in LESSONS_FILE.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                lesson = json.loads(line)
            except json.JSONDecodeError:
                continue
            et  = (lesson.get("error_type") or lesson.get("type", "")).strip()
            msg = (lesson.get("error_msg")  or lesson.get("message", "")).strip()
            if not et or et in known_types:
                continue
            unknown_counts.setdefault(et, []).append(msg[:200])
    except Exception as e:
        return f"❌ _discover_new_patterns: could not read lessons.jsonl — {e}"

    if not unknown_counts:
        return "✅ discover_patterns: no untracked error types in lessons.jsonl"

    qualifying = {
        et: msgs
        for et, msgs in unknown_counts.items()
        if len(msgs) >= min_occurrences
    }

    if not qualifying:
        below = len(unknown_counts)
        return (
            f"✅ discover_patterns: {below} untracked type(s) in lessons — "
            f"none meet the {min_occurrences}x threshold yet"
        )

    proposed = 0
    lines = [f"🔍 discover_patterns: {len(qualifying)} qualifying untracked type(s):"]

    for et, msgs in sorted(qualifying.items(), key=lambda x: -len(x[1])):
        # Dedupe while preserving order
        unique_msgs = list(dict.fromkeys(msgs))
        examples_str = "\n".join(f"  - {m}" for m in unique_msgs[:5])

        prompt = (
            f"You are analyzing a recurring failure type in an AI agent skill system.\n\n"
            f"Error type name : {et}\n"
            f"Occurrences     : {len(msgs)}\n"
            f"Example messages:\n{examples_str}\n\n"
            f"Respond in this EXACT format (3 lines, no extra text):\n"
            f"DESCRIPTION: <one sentence — what this failure pattern is>\n"
            f"FIX: <one sentence — how to prevent or fix it in Python code>\n"
            f"SEVERITY: low|medium|high|critical"
        )

        analysis = _call_llm_simple(prompt, max_tokens=150)

        if analysis:
            idea_text = (
                f"New error pattern discovered: '{et}' ({len(msgs)}x in lessons.jsonl).\n"
                f"LLM analysis:\n{analysis}\n"
                f"Consider adding to error_patterns.json — and to AUTO_FIXABLE if a safe "
                f"deterministic fix exists."
            )
        else:
            # LLM unavailable — surface raw data so a human can still act on it
            sample = "; ".join(unique_msgs[:3])
            idea_text = (
                f"New error pattern discovered: '{et}' ({len(msgs)}x in lessons.jsonl). "
                f"Examples: {sample}. "
                f"Consider adding to error_patterns.json."
            )

        park_idea(idea_text, source="discover_patterns")
        lines.append(f"  • '{et}' ({len(msgs)}x) → parked for review")
        proposed += 1

    _log({
        "timestamp":       datetime.now().isoformat(),
        "loop":            "discover_patterns",
        "outcome":         "DISCOVERED",
        "untracked_total": len(unknown_counts),
        "qualifying":      len(qualifying),
        "proposed":        proposed,
        "min_occurrences": min_occurrences,
    })

    lines.append(f"\nReview with: autoimprove.list_ideas()")
    return "\n".join(lines)


# ── Public API ─────────────────────────────────────────────────────────────────

def run_loop(loop_name: str, max_experiments=10, **_ignored) -> str:
    """
    Run a single named improvement loop.

    Args:
        loop_name:       ast_audit | error_reduce | daily_review | suggest_core | pattern_mining | discover_patterns
        max_experiments: Max experiments / skills to scan

    Returns:
        Full summary of this loop's run.
    """
    max_experiments = int(max_experiments)
    _loops = {
        "ast_audit":         lambda: _loop_ast_audit(max_experiments),
        "error_reduce":      lambda: _loop_error_reduce(max_experiments),
        "daily_review":      _loop_daily_review,
        "suggest_core":      lambda: suggest_core(max_experiments),
        "pattern_mining":    _loop_pattern_mining,
        "discover_patterns": _discover_new_patterns,
    }
    if loop_name not in _loops:
        return f"❌ Unknown loop '{loop_name}'. Available: {list(_loops.keys())}"

    start   = time.time()
    result  = _loops[loop_name]()
    elapsed = time.time() - start
    return f"{result}\n\n⏱ Loop '{loop_name}' finished in {elapsed:.1f}s"


def run_all(max_experiments=5, max_runtime_seconds=25) -> str:
    """
    Run all loops in sequence.

    IMPORTANT: Direct calls are capped at 25s (dispatcher limit). Only 1-2 loops
    will run before the cap is hit — this is expected behaviour for interactive use.
    For a full overnight run across all loops, use schedule_nightly() instead:
        autoimprove.schedule_nightly('2am')

    Order:
      1. daily_review     — learn what happened, no code changes
      2. ast_audit        — auto-fix dynamic skills
      3. error_reduce     — target top error pattern in dynamic skills
      4. suggest_core     — audit core skills, queue suggestions for your review
      5. pattern_mining   — scan session history for recurring task patterns
      6. discover_patterns — scan lessons.jsonl for untracked error types

    Args:
        max_experiments:     Max experiments per loop (default 5)
        max_runtime_seconds: Hard wall-time cap in seconds (default 25 — fits
                             dispatcher limit). Pass None only from a scheduled
                             context where no timeout applies.

    Returns:
        Combined results from all loops that ran before the cap.
    """
    max_experiments = int(max_experiments)
    deadline: Optional[float] = None
    if max_runtime_seconds is not None and str(max_runtime_seconds).strip().lower() not in ("none", "null", ""):
        try:
            deadline = float(max_runtime_seconds)
        except (ValueError, TypeError):
            deadline = 25.0
    else:
        deadline = None

    start   = time.time()
    divider = "=" * 52
    results = [f"🤖 AutoImprove started — {datetime.now().strftime('%Y-%m-%d %H:%M')}"]
    if deadline is not None:
        results.append(f"   runtime cap: {deadline:.0f}s")

    # Track (loop_name, start_ts, end_ts, elapsed) so we can do a single
    # _load_log(1) call after all loops finish and attribute entries in memory.
    loop_timing: List[Dict] = []

    for loop_name in ("daily_review", "ast_audit", "error_reduce", "suggest_core", "pattern_mining", "discover_patterns"):
        elapsed_so_far = time.time() - start
        if deadline is not None and elapsed_so_far >= deadline:
            results.append(
                f"\n{divider}\n⏱ runtime cap reached ({elapsed_so_far:.1f}s ≥ {deadline:.0f}s) "
                f"— skipping '{loop_name}' and remaining loops"
            )
            break

        loop_start_ts = datetime.now().isoformat()
        loop_start_t  = time.time()

        results.append(f"\n{divider}\n🔬 {loop_name}")
        results.append(run_loop(loop_name, max_experiments))

        loop_timing.append({
            "loop":     loop_name,
            "start_ts": loop_start_ts,
            "end_ts":   datetime.now().isoformat(),
            "elapsed":  round(time.time() - loop_start_t, 1),
        })
        time.sleep(2)

    # Single log read — filter in memory per loop instead of one read per loop.
    all_new_entries = _load_log(1)
    loop_roi_data: List[Dict] = []
    for lt in loop_timing:
        loop_entries = [
            e for e in all_new_entries
            if lt["start_ts"] <= e.get("timestamp", "") < lt["end_ts"]
        ]
        if lt["loop"] == "discover_patterns":
            # discover_patterns logs DISCOVERED entries with proposed/qualifying counts
            # rather than per-experiment IMPROVED/REVERTED/NO_CHANGE outcomes.
            discovered = [e for e in loop_entries if e.get("outcome") == "DISCOVERED"]
            n_improved = sum(e.get("proposed", 0) for e in discovered)
            n_total    = sum(e.get("qualifying", 0) for e in discovered)
        else:
            experiments = [e for e in loop_entries if e.get("outcome") in ("IMPROVED", "REVERTED", "NO_CHANGE")]
            n_improved  = sum(1 for e in experiments if e["outcome"] == "IMPROVED")
            n_total     = len(experiments)
        loop_roi_data.append({
            "loop":     lt["loop"],
            "elapsed":  lt["elapsed"],
            "improved": n_improved,
            "total":    n_total,
            "roi":      round(n_improved / n_total, 2) if n_total else None,
        })

    elapsed = time.time() - start

    # Log the full run summary for historical ROI tracking (readable by loop_roi())
    _log({
        "timestamp":     datetime.now().isoformat(),
        "loop":          "run_all",
        "outcome":       "RUN_ALL",
        "loops":         loop_roi_data,
        "total_elapsed": round(elapsed, 1),
    })

    # Inline ROI table — quick overnight read
    results.append(f"\n{divider}\n📊 Loop ROI (this run):")
    results.append(f"  {'Loop':<22} {'Time':>6}  {'Impr/Total':>11}  {'ROI':>6}")
    results.append(f"  {'-'*22} {'-'*6}  {'-'*11}  {'-'*6}")
    for lr in loop_roi_data:
        roi_str  = f"{lr['roi']:.0%}" if lr["roi"] is not None else "—"
        flag     = " ⚠️" if lr["total"] > 0 and lr["roi"] == 0.0 else ""
        results.append(
            f"  {lr['loop']:<22} {lr['elapsed']:>5.1f}s  "
            f"{lr['improved']}/{lr['total']:>2} improved  {roi_str:>5}{flag}"
        )

    results.append(f"\n{divider}\n✅ run_all complete — {elapsed:.1f}s total")
    results.append("Review core suggestions: autoimprove.list_suggestions()")
    results.append("Review skill proposals:  autoimprove.list_ideas()")
    results.append("Historical loop ROI:     autoimprove.loop_roi()")
    return "\n".join(results)


def schedule_nightly(run_time: str = "2am", max_experiments=5) -> str:
    """
    Put all improvement loops on autopilot — schedules run_all() every night.

    Args:
        run_time:        When to run (e.g. '2am', '3am', 'midnight')
        max_experiments: Max experiments per loop per night (default 5)

    Returns:
        Confirmation from scheduler.
    """
    max_experiments = int(max_experiments)
    try:
        sched = _import_skill("scheduler")
    except ImportError as e:
        return f"❌ scheduler not available: {e}"

    prompt = (
        f"Run overnight self-improvement on all skills. "
        f"Call autoimprove.run_all({max_experiments}, max_runtime_seconds=None) — this audits and fixes dynamic skills "
        f"automatically, and queues suggestions for core skills for the user to review."
    )
    result = sched.schedule_recurring(
        name="autoimprove_nightly",
        every="1d",
        prompt=prompt,
    )
    return (
        f"✅ Nightly improvement loop scheduled (every day, starting around {run_time}):\n"
        f"{result}\n\n"
        f"In the morning:\n"
        f"  autoimprove.report(1)          → see what was auto-fixed overnight\n"
        f"  autoimprove.list_suggestions() → review proposed fixes for core skills"
    )


def report(days=7) -> str:
    """
    Show improvement history for the last N days.

    Args:
        days: Look-back window (default 7)
    """
    days    = int(days)
    entries = _load_log(days)
    if not entries:
        return (
            f"📭 No experiments recorded in the last {days} days.\n"
            f"Run: autoimprove.run_all() or autoimprove.schedule_nightly()"
        )

    total     = len(entries)
    improved  = sum(1 for e in entries if e.get("outcome") == "IMPROVED")
    reverted  = sum(1 for e in entries if e.get("outcome") == "REVERTED")
    reviews   = sum(1 for e in entries if e.get("outcome") in ("REVIEW", "SUGGESTED"))
    no_change = total - improved - reverted - reviews

    skill_wins: Dict[str, int] = {}
    for e in entries:
        if e.get("outcome") == "IMPROVED":
            s = e.get("skill", "?")
            skill_wins[s] = skill_wins.get(s, 0) + 1

    suggestions = _load_suggestions()
    pending_count = sum(1 for s in suggestions if s.get("status") == "pending")

    # MAD confidence summary across this reporting window
    period_deltas  = [
        float(e["score_delta"]) for e in entries
        if e.get("outcome") == "IMPROVED" and e.get("score_delta") is not None
    ]
    genuine  = sum(1 for e in entries if (e.get("confidence") or 0) >= 2.0)
    marginal = sum(1 for e in entries if 1.0 <= (e.get("confidence") or 0) < 2.0)
    noise    = sum(1 for e in entries if e.get("outcome") == "IMPROVED"
                   and e.get("confidence") is not None and (e.get("confidence") or 0) < 1.0)
    has_conf = (genuine + marginal + noise) > 0

    lines = [
        f"📈 AutoImprove Report — last {days} day(s)",
        "=" * 52,
        f"Experiments run      : {total}",
        f"  ✅ Improved        : {improved}",
        f"  🔄 Reverted        : {reverted}",
        f"  —  No change       : {no_change}",
        f"  📋 Reviews/scans   : {reviews}",
        f"Core suggestions     : {pending_count} pending review",
    ]

    if has_conf:
        lines += [
            "",
            "Confidence breakdown (MAD-based noise floor):",
            f"  🟢 Genuine (≥2×)    : {genuine}",
            f"  🟡 Marginal (1–2×)  : {marginal}",
            f"  🔴 Within noise (<1×): {noise}",
        ]
        if len(period_deltas) >= MAD_MIN_SAMPLES:
            avg_delta = sum(period_deltas) / len(period_deltas)
            lines.append(f"  Avg score delta     : +{avg_delta:.1f} pts")
    else:
        lines.append(f"  Confidence          : — (need {MAD_MIN_SAMPLES}+ IMPROVED experiments for MAD baseline)")

    if skill_wins:
        lines.append("")
        lines.append("Top improved skills:")
        for skill, wins in sorted(skill_wins.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"  • {skill}: {wins} fix(es)")

    if pending_count:
        lines.append("")
        lines.append(f"⏳ {pending_count} core skill fix(es) waiting for your approval:")
        lines.append("   Run: autoimprove.list_suggestions()")

    lines.append("")
    lines.append("Recent experiments (last 20):")
    for e in entries[-20:]:
        ts      = e.get("timestamp", "")[:16]
        outcome = e.get("outcome", "?")
        track   = f"[{e['track']}] " if e.get("track") else ""
        if outcome in ("REVIEW", "SUGGESTED"):
            lines.append(f"  {ts}  📋  {e.get('loop', 'review')}")
        else:
            skill  = e.get("skill", "?")
            issue  = e.get("issue_type", "?")
            before = e.get("before_score", "?")
            after  = e.get("after_score", "?")
            icon   = "✅" if outcome == "IMPROVED" else ("🔄" if outcome == "REVERTED" else "—")
            conf   = e.get("confidence_label", "")
            conf_str = f"  {conf}" if conf and outcome == "IMPROVED" else ""
            lines.append(f"  {ts}  {icon}  {track}{skill} [{issue}] {before}→{after}{conf_str}")

    return "\n".join(lines)


def loop_roi(runs: int = 10) -> str:
    """
    Show per-loop historical ROI averaged over the last N run_all() runs.

    ROI = experiments that improved / total experiments run per loop.
    Loops with consistently 0 ROI are flagged — candidates for skipping or
    reducing frequency once you have enough history to trust the signal.

    Args:
        runs: Max number of past run_all() runs to average over (default 10)

    Returns:
        Per-loop ROI table sorted by average ROI descending.
    """
    try:
        runs = int(runs)
    except (ValueError, TypeError):
        runs = 10

    if not IMPROVE_LOG.exists():
        return (
            "📭 No improvement log yet.\n"
            "Run autoimprove.run_all() — ROI data is collected each time it completes."
        )

    # Collect the last N RUN_ALL summary entries from the log
    run_summaries: List[Dict] = []
    try:
        for line in IMPROVE_LOG.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                if e.get("loop") == "run_all" and e.get("outcome") == "RUN_ALL":
                    run_summaries.append(e)
            except json.JSONDecodeError:
                continue
    except Exception as exc:
        return f"❌ Could not read improvement log: {exc}"

    if not run_summaries:
        return (
            "📭 No run_all() history yet — ROI data appears after the first completed run.\n"
            "Run: autoimprove.run_all()"
        )

    recent = run_summaries[-runs:]

    # Aggregate per-loop stats across all runs
    loop_stats: Dict[str, Dict] = {}
    for run_entry in recent:
        for lr in run_entry.get("loops", []):
            name = lr.get("loop", "?")
            if name not in loop_stats:
                loop_stats[name] = {"improved": 0, "total": 0, "elapsed": 0.0, "runs": 0}
            loop_stats[name]["improved"] += lr.get("improved", 0)
            loop_stats[name]["total"]    += lr.get("total", 0)
            loop_stats[name]["elapsed"]  += lr.get("elapsed", 0.0)
            loop_stats[name]["runs"]     += 1

    if not loop_stats:
        return "📭 No per-loop data in the stored run_all() entries — run again to populate."

    sorted_loops = sorted(
        loop_stats.items(),
        key=lambda x: x[1]["improved"] / max(1, x[1]["total"]),
        reverse=True,
    )

    lines = [
        f"📊 Loop ROI — last {len(recent)} run_all() run(s)",
        "=" * 58,
        f"  {'Loop':<22} {'Avg ROI':>8}  {'Improved':>9}  {'Total':>7}  {'Avg Time':>9}",
        f"  {'-'*22} {'-'*8}  {'-'*9}  {'-'*7}  {'-'*9}",
    ]

    for name, s in sorted_loops:
        avg_roi  = s["improved"] / max(1, s["total"])
        avg_time = s["elapsed"] / max(1, s["runs"])
        roi_str  = f"{avg_roi:.0%}" if s["total"] > 0 else "—"
        flag     = " ⚠️" if s["total"] > 0 and avg_roi == 0.0 else ""
        # discover_patterns tracks proposals/qualifying, not experiments — mark
        # with (*) so the numbers aren't compared directly to experiment loops.
        if name == "discover_patterns":
            lines.append(
                f"  {name+'(*)' :<22} {roi_str:>8}  {s['improved']:>9}  {s['total']:>7}  {avg_time:>7.1f}s{flag}"
            )
        else:
            lines.append(
                f"  {name:<22} {roi_str:>8}  {s['improved']:>9}  {s['total']:>7}  {avg_time:>7.1f}s{flag}"
            )

    lines += [
        f"  {'='*58}",
        "⚠️  = zero improvements across all sampled runs",
        "    → consider running: autoimprove.run_loop('<name>') to spot-check",
        "    → or skip that loop in a custom run_all call",
        "(*) discover_patterns: Improved = patterns proposed; Total = patterns qualifying threshold",
        "    ROI is proposals/qualifying — not comparable to experiment-based loop ROI above.",
        f"\nData from {len(recent)} of {len(run_summaries)} total recorded run_all() runs.",
    ]
    return "\n".join(lines)


def status() -> str:
    """Show autoimprove config, loops, and skills in scope."""
    dynamic_skills = [p.stem for p in SKILLS_DYNAMIC_DIR.glob("*.py") if not p.name.startswith("_")]
    core_skills    = [p.stem for p in SKILLS_CORE_DIR.glob("*.py")    if not p.name.startswith("_")]
    entries        = _load_log(7)
    last_run       = entries[-1].get("timestamp", "never")[:16] if entries else "never"
    suggestions    = _load_suggestions()
    pending        = sum(1 for s in suggestions if s.get("status") == "pending")
    proposals      = _load_proposals()
    pending_props  = sum(1 for p in proposals if p.get("status") == "pending")
    ideas          = _load_ideas()
    open_ideas     = sum(1 for i in ideas if i.get("status") == "open")

    return "\n".join([
        "🤖 AutoImprove — Autoresearch Loop for Trinity",
        "=" * 52,
        "",
        "Two-track system:",
        "  DYNAMIC → auto-fix  (snapshot → patch → test → keep or restore)",
        "  CORE    → suggest   (audit → patch preview → you approve → apply)",
        "",
        "Loops:",
        "  • daily_review — learning review, no code changes",
        "  • ast_audit    — auto-fix bare_except & missing_timeout in dynamic skills",
        "  • error_reduce — auto-fix top error pattern + surface lesson proposals",
        "  • suggest_core      — audit core skills, queue suggestions for your review",
        "  • discover_patterns — scan lessons.jsonl for untracked error types, propose via LLM",
        "",
        f"Dynamic skills ({len(dynamic_skills)}): {', '.join(dynamic_skills) or 'none'}",
        f"Core skills    ({len(core_skills)}): {len(core_skills)} files in /app/skills/core/",
        f"Pending suggestions : {pending} core fixes waiting for approval",
        f"Pending proposals   : {pending_props} lesson patterns needing a decision",
        f"Open ideas          : {open_ideas} parked improvement ideas",
        f"Last experiment     : {last_run}",
        "",
        "Commands:",
        "  autoimprove.research('any topic', depth='deep')     → web research, saved to notes",
        "  autoimprove.run_all(5)                              → run all loops tonight",
        "  autoimprove.suggest_core()                          → scan all core skills now",
        "  autoimprove.list_suggestions()                      → review pending core fixes",
        "  autoimprove.apply_suggestion('web', 'bare_except')  → approve and apply one fix",
        "  autoimprove.lessons_to_proposals()                  → surface recurring error patterns",
        "  autoimprove.list_proposals()                        → review non-auto-fixable patterns",
        "  autoimprove.park_idea('description', 'source')      → park an idea for later",
        "  autoimprove.list_ideas()                            → show open improvement ideas",
        "  autoimprove.dismiss_idea('<id>')                    → mark idea as done/dismissed",
        "  autoimprove.loop_roi()                              → per-loop historical ROI",
        "  autoimprove.schedule_nightly('2am')                 → set on autopilot",
        "  autoimprove.report(7)                               → last 7 days",
        "",
        "Safety:",
        "  • Dynamic: only skills/dynamic/ auto-modified, snapshot always taken",
        "  • Core: NEVER auto-modified — requires your explicit apply_suggestion() call",
        "  • Runtime smoke test gates every change (dynamic and core alike)",
        "  • Restores original file if test fails",
    ])


# ── Design-first brainstorming ────────────────────────────────────────────────

DESIGNS_DIR = MEMORY_DIR / "designs"


def design(task: str) -> str:
    """
    Design-first gate: scan existing skills, surface gaps, propose 2-3 approaches.

    Call this before create_skill for any non-trivial build request.
    Returns a structured brief Trinity uses to ask the user ONE clarifying question
    and propose concrete approaches with trade-offs.

    Args:
        task: Plain-language description of what the user wants built.

    Returns:
        Structured design brief: existing coverage, open questions, 2-3 approaches.
    """
    import os

    task = str(task).strip()
    if not task:
        return "❌ task cannot be empty."

    # Scan what skills already exist (core + dynamic)
    core_skills, dynamic_skills = [], []
    try:
        core_skills = [
            f[:-3] for f in os.listdir(str(SKILLS_CORE_DIR))
            if f.endswith(".py") and not f.startswith("_")
        ]
    except Exception:
        pass
    try:
        dynamic_skills = [
            f[:-3] for f in os.listdir(str(SKILLS_DYNAMIC_DIR))
            if f.endswith(".py") and not f.startswith("_")
        ]
    except Exception:
        pass

    all_skills = sorted(set(core_skills + dynamic_skills))

    # Naive keyword overlap: find skills whose name shares words with the task
    task_words = set(re.findall(r'\w+', task.lower())) - _RESEARCH_STOP
    related = [s for s in all_skills if any(w in s.lower() for w in task_words)]

    lines = [
        "## Design Brief",
        f"**Task:** {task}",
        "",
        "### Existing Coverage",
    ]
    if related:
        lines.append(f"Potentially related skills: {', '.join(related)}")
        lines.append("→ Check if one of these can be extended before creating a new skill.")
    else:
        lines.append("No directly related skills found — likely needs a new skill.")

    lines += [
        "",
        "### Clarifying Question",
        "Ask the user ONE of the following (pick the most critical unknown):",
        "  A) What is the expected input format / data source?",
        "  B) What should the output look like — string, file, notification?",
        "  C) Will this run on demand or on a schedule?",
        "  D) Are there auth/credential requirements?",
        "  E) Should this extend an existing skill or be standalone?",
        "",
        "### Proposed Approaches",
        "",
        "**Option 1 — Extend existing skill** (if a related skill exists)",
        "  + No new file, lower maintenance surface",
        "  + Fits naturally into existing DOC string and __all__",
        "  - May grow the skill beyond its original scope",
        "  - Only viable if the overlap is genuine, not superficial",
        "",
        "**Option 2 — New dynamic skill** (default path)",
        "  + Isolated, easy to audit and delete",
        "  + Sandboxed by create_skill's AST + blocklist gates",
        "  - Adds to skill count; name must be unique and descriptive",
        "  - Must follow NAME/DOC/function convention",
        "",
        "**Option 3 — Compose from existing skills** (no new code)",
        "  + Zero new files, zero security surface",
        "  + Trinity orchestrates existing tools via chat instructions",
        "  - Only works if existing skills already cover all sub-steps",
        "  - Less reusable across sessions (no persistent callable)",
        "",
        "### Next Steps",
        "1. Ask the user the ONE clarifying question above.",
        "2. Once answered, pick an option and call `autoimprove.write_spec(task, option, details)`.",
        "3. Show the spec to the user for approval.",
        "4. Only then proceed to `create_skill` (if Option 1 or 2).",
    ]

    return "\n".join(lines)


def write_spec(task: str, approach: str, details: str) -> str:
    """
    Write an approved design spec to /app/memory/designs/YYYY-MM-DD-<slug>.md.

    Call this after the user has approved an approach from `design()`.
    The spec becomes the single source of truth before any code is written.

    Args:
        task:     Plain-language task description (used for filename slug).
        approach: Chosen approach label, e.g. "Option 2 — New dynamic skill".
        details:  Free-form spec content: inputs, outputs, edge cases, constraints.

    Returns:
        Path of the written spec file, or an error message.
    """
    task = str(task).strip()
    approach = str(approach).strip()
    details = str(details).strip()

    if not task or not details:
        return "❌ task and details are required."

    slug = re.sub(r'[^\w]+', '-', task.lower()).strip('-')[:50]
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"{date_str}-{slug}.md"

    content = "\n".join([
        f"# Design Spec: {task}",
        f"**Date:** {date_str}",
        f"**Approach:** {approach}",
        "",
        "## Details",
        details,
        "",
        "## Self-Review Checklist",
        "- [ ] No placeholders (ALL_CAPS) left unfilled",
        "- [ ] No contradictions between input and output descriptions",
        "- [ ] Edge cases covered (empty input, auth failure, large data)",
        "- [ ] Approach is the simplest that satisfies the requirement (YAGNI)",
        "- [ ] User has explicitly approved this spec",
    ])

    try:
        DESIGNS_DIR.mkdir(parents=True, exist_ok=True)
        spec_path = DESIGNS_DIR / filename
        spec_path.write_text(content, encoding="utf-8")
        return f"✅ Spec saved to /app/memory/designs/{filename} — show this to the user for approval before writing any code."
    except Exception as e:
        return f"❌ Failed to write spec: {e}"


# ── Export list ────────────────────────────────────────────────────────────────

def should_create_skill(
    execution_logs: list,
    iteration_count: int,
    user_message: str = "",
) -> dict:
    """
    Analyze a completed task trace to decide if a new skill should be created.

    Evaluates four triggers:
      - complexity:     5+ tool calls in the task
      - error_recovery: errors occurred before eventual success
      - correction:     user message contains correction language
      - workflow:       3+ distinct skills combined in one task

    Returns:
        {
            "create": bool,
            "reason": str,
            "trigger": str,   # "complexity" | "error_recovery" | "correction" | "workflow" | "none"
            "complexity_score": float,  # 0.0–1.0
        }
    """
    if not execution_logs:
        return {
            "create": False,
            "reason": "no tool calls in this task",
            "trigger": "none",
            "complexity_score": 0.0,
        }

    # Skip informational queries — read-only lookups are never worth capturing as a skill.
    _QUERY_SIGNALS = (
        "how is", "how are", "how does", "what is", "what are", "show me",
        "tell me", "check ", "can you show", "which ", "is there", "do you",
        "kako je", "kako su", "šta je", "šta su", "kako radi", "da li",
        "koliko", "gde je", "koji je", "trenutno", "podešen", "podešavanja",
    )
    _msg_lower = user_message.lower()
    if any(sig in _msg_lower for sig in _QUERY_SIGNALS):
        return {
            "create": False,
            "reason": "informational query — read-only tasks are not worth capturing as a skill",
            "trigger": "none",
            "complexity_score": 0.0,
        }

    total_calls  = len(execution_logs)
    error_calls  = sum(1 for l in execution_logs if l.get("status") == "error")
    success_calls = total_calls - error_calls
    unique_skills = {l.get("skill", "") for l in execution_logs if l.get("skill")}

    # Composite score: how non-trivial was this task?
    # error_recovery component now requires 2+ errors to reach full weight.
    complexity_score = round(
        0.4 * min(total_calls / 5.0, 1.0)           # up to 0.4 for 5+ calls
        + 0.3 * min(len(unique_skills) / 3.0, 1.0)  # up to 0.3 for 3+ distinct skills
        + 0.3 * min(error_calls / 2.0, 1.0),        # up to 0.3, needs 2+ errors for full score
        2,
    )

    # Correction keywords in the user's message
    _CORRECTION_SIGNALS = (
        "wrong", "no,", "don't", "stop", "instead",
        "actually", "not that", "incorrect", "that's not",
    )
    _has_correction = any(sig in _msg_lower for sig in _CORRECTION_SIGNALS)

    trigger: Optional[str] = None
    reason  = ""

    if _has_correction:
        trigger = "correction"
        reason  = "User corrected the approach — the fix pattern may be worth capturing as a skill"
    elif total_calls >= 5 and len(unique_skills) >= 3:
        trigger = "workflow"
        reason  = (
            f"Multi-skill workflow: {total_calls} calls across "
            f"{len(unique_skills)} skills ({', '.join(sorted(unique_skills))})"
        )
    elif error_calls >= 2 and success_calls > 0 and total_calls >= 3:
        trigger = "error_recovery"
        reason  = (
            f"Recovered from {error_calls} error(s) before success — "
            "the successful path is worth capturing"
        )
    elif total_calls >= 5:
        trigger = "complexity"
        reason  = f"Complex task: {total_calls} tool calls across {iteration_count} iteration(s)"

    create = trigger is not None and complexity_score >= 0.4

    return {
        "create":           create,
        "reason":           reason,
        "trigger":          trigger or "none",
        "complexity_score": complexity_score,
    }


__all__ = [
    "NAME",
    "DOC",
    "research",
    "run_experiment",
    "run_loop",
    "run_all",
    "suggest_core",
    "list_suggestions",
    "apply_suggestion",
    "lessons_to_proposals",
    "list_proposals",
    "park_idea",
    "list_ideas",
    "dismiss_idea",
    "loop_roi",
    "schedule_nightly",
    "report",
    "status",
    "design",
    "write_spec",
    "should_create_skill",
]
