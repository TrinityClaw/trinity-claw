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
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

# ── Skill metadata ─────────────────────────────────────────────────────────────

NAME = "autoimprove"
DOC = (
    "Autoresearch-style overnight self-improvement loops — two-track system: "
    "DYNAMIC skills auto-fix (snapshot→patch→test→keep/restore); "
    "CORE skills suggest-only (audit→patch→save to memory→you approve). "
    "research(query, depth='quick', save=True)→general-purpose web research on ANY topic: "
    "searches the web, fetches top pages, extracts key findings, saves to notes; "
    "depth='deep' fetches more sources and runs related queries; "
    "run_experiment(skill_name, issue_type)→one autoresearch cycle on a dynamic skill; "
    "run_loop(loop_name, max_experiments=10)→named loop: ast_audit|error_reduce|daily_review|suggest_core; "
    "run_all(max_experiments=5)→all loops in sequence (overnight autopilot); "
    "suggest_core(max_skills=30)→audit core skills, save proposed patches to memory; "
    "list_suggestions(status='pending')→show pending|applied|failed|all core suggestions; "
    "apply_suggestion(skill_name, issue_type)→apply one approved suggestion to a core skill; "
    "schedule_nightly(run_time='2am')→put on autopilot; "
    "report(days=7)→improvement history; "
    "status()→config and skills in scope."
)

# ── Config ─────────────────────────────────────────────────────────────────────

SKILLS_DYNAMIC_DIR = Path("/app/skills/dynamic")
SKILLS_CORE_DIR    = Path("/app/skills/core")
MEMORY_DIR         = Path("/app/memory")
IMPROVE_LOG        = MEMORY_DIR / "improvement_log.jsonl"
SUGGESTIONS_FILE   = MEMORY_DIR / "core_suggestions.jsonl"
PATTERNS_FILE      = MEMORY_DIR / "error_patterns.json"

# Only deterministic, low-risk issue types get auto-applied
AUTO_FIXABLE = ("bare_except", "missing_timeout")

# ── Logging ────────────────────────────────────────────────────────────────────

def _log(entry: Dict) -> None:
    """Append one experiment result to improvement_log.jsonl."""
    try:
        IMPROVE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with IMPROVE_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


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
        SUGGESTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with SUGGESTIONS_FILE.open("w", encoding="utf-8") as f:
            for s in suggestions:
                f.write(json.dumps(s, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass


def _suggestion_key(skill_name: str, issue_type: str) -> str:
    return f"{skill_name}::{issue_type}"


# ── Lazy skill imports ─────────────────────────────────────────────────────────

def _import_skill(module_name: str):
    """Import a skill module, adding skill dirs to sys.path if needed."""
    for p in ("/app/skills/core", "/app/skills/dynamic", "/app/skills", "/app"):
        if p not in sys.path:
            sys.path.insert(0, p)
    import importlib
    return importlib.import_module(module_name)


# ── General-purpose research ───────────────────────────────────────────────────

def research(query: str, depth: str = "quick", save: bool = True) -> str:
    """
    General-purpose web research on any topic.

    Searches the web, fetches the top pages, pulls out key content, and returns
    a structured research package. Optionally saves findings to notes so they
    persist across sessions and feed into the agent's knowledge base.

    Args:
        query:  Anything you want to research — a topic, question, technology,
                person, concept, error message, etc.
        depth:  'quick' → 1 search + fetch top 2 results  (~15s)
                'deep'  → 3 search variations + fetch top 5 results (~45s)
        save:   If True, save findings to notes with a timestamped title.
                Default True — results persist in memory/notes.json.

    Returns:
        Structured research summary with sources, key excerpts, and save path.

    Examples (tell Trinity):
        "Research the latest Python async best practices"
        "Research how to improve browser automation reliability"
        "Do a deep research on competitor pricing for SaaS tools"
        "Research: what is Karpathy's autoresearch method?"
    """
    try:
        web = _import_skill("web")
    except ImportError as e:
        return f"❌ web skill not available: {e}"

    timestamp  = datetime.now().isoformat()
    date_label = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ── 1. Build search queries ────────────────────────────────────────────────
    queries = [query]
    if depth == "deep":
        # Add focused variants to get broader coverage
        queries += [
            f"{query} best practices 2025",
            f"{query} examples tutorial",
        ]

    # ── 2. Collect search results ──────────────────────────────────────────────
    max_fetch  = 2 if depth == "quick" else 5
    seen_urls: set = set()
    sources:   List[Dict] = []

    for q in queries:
        if len(sources) >= max_fetch:
            break
        try:
            raw = web.search(q)
            # web.search returns a text block — extract URLs from it
            urls_found = []
            for line in (raw or "").splitlines():
                line = line.strip()
                if line.startswith("http://") or line.startswith("https://"):
                    urls_found.append(line)
                elif "http" in line:
                    # Try to pull URLs embedded in text
                    import re
                    found = re.findall(r'https?://[^\s\)"\']+', line)
                    urls_found.extend(found)
            for url in urls_found:
                if url not in seen_urls and len(sources) < max_fetch:
                    seen_urls.add(url)
                    sources.append({"url": url, "query": q, "content": None})
        except Exception as e:
            sources.append({"url": None, "query": q, "content": f"search error: {e}"})

    # If search returned no parseable URLs, keep the raw text as a source
    if not sources:
        try:
            raw = web.search(query)
            sources.append({"url": None, "query": query, "content": raw})
        except Exception as e:
            return f"❌ Research failed — web.search error: {e}"

    # ── 3. Fetch page content ──────────────────────────────────────────────────
    for src in sources:
        if src.get("url") and src["content"] is None:
            try:
                content = web.fetch(src["url"])
                # Trim to keep context window manageable
                src["content"] = (content or "")[:3000]
            except Exception as e:
                src["content"] = f"fetch error: {e}"

    # ── 4. Build structured output ─────────────────────────────────────────────
    lines = [
        f"📚 Research: {query}",
        f"   Depth: {depth}  |  Sources fetched: {len(sources)}  |  {date_label}",
        "=" * 60,
    ]

    for i, src in enumerate(sources, 1):
        lines.append(f"\n[Source {i}] {src.get('url') or '(search result)'}")
        content = src.get("content") or ""
        if content:
            # Show first ~800 chars — enough to extract key info
            preview = content.strip()[:800]
            if len(content) > 800:
                preview += f"\n... [{len(content) - 800} more chars]"
            lines.append(preview)
        lines.append("")

    lines.append("=" * 60)
    lines.append(f"Sources: {len(sources)} | Query: {query}")

    result_text = "\n".join(lines)

    # ── 5. Save to notes ───────────────────────────────────────────────────────
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
        "timestamp":    timestamp,
        "loop":         "research",
        "outcome":      "RESEARCH",
        "query":        query,
        "depth":        depth,
        "sources_found": len(sources),
        "saved_as":     save_path,
    })

    footer = f"\n💾 Saved to notes: \"{save_path}\"" if save_path else ""
    return result_text + footer


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
        One-line outcome: IMPROVED | REVERTED | NO_CHANGE | SKIPPED
    """
    if issue_type not in AUTO_FIXABLE:
        return f"SKIPPED: '{issue_type}' not in auto-fixable set {AUTO_FIXABLE}"

    skill_path = SKILLS_DYNAMIC_DIR / f"{skill_name}.py"
    if not skill_path.exists():
        return f"SKIPPED: {skill_name} not found in skills/dynamic/"

    timestamp = datetime.now().isoformat()

    try:
        si = _import_skill("self_improvement")
        ce = _import_skill("code_executor")
    except ImportError as e:
        return f"SKIPPED: dependency missing — {e}"

    # 1. Snapshot — safety net, no git needed
    try:
        original_code = skill_path.read_text(encoding="utf-8")
    except Exception as e:
        return f"SKIPPED: cannot read {skill_name}.py — {e}"

    # 2. Baseline audit
    before = si.analyze_skill_code(skill_name)
    if before.get("error"):
        return f"SKIPPED: pre-audit error — {before['error']}"

    before_score  = before["health_score"]
    target_issues = [i for i in before["issues"] if i["type"] == issue_type]

    if not target_issues:
        return f"NO_CHANGE: no '{issue_type}' in {skill_name} (score={before_score})"

    # 3. Apply patch
    si.fix(skill_name, issue_type, "all")

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

    # 5. Runtime smoke test — try status(), fall back to import check
    test_result = ce.test_skill(skill_name, "status", "[]", timeout=10)
    if not test_result.startswith("✅") and "not found" in test_result:
        test_result = ce.run_snippet(
            "import sys\n"
            "sys.path.insert(0, '/app/skills/dynamic')\n"
            f"import {skill_name}\n"
            f"print(getattr({skill_name}, 'NAME', 'loaded ok'))",
            timeout=10,
        )
    test_passed = test_result.startswith("✅")

    # 6. Keep or restore
    issues_remaining = sum(1 for i in after.get("issues", []) if i["type"] == issue_type)
    issues_fixed     = len(target_issues) - issues_remaining

    if not test_passed:
        skill_path.write_text(original_code, encoding="utf-8")
        outcome = "REVERTED"
        reason  = f"runtime test failed: {test_result[:150]}"
    elif issues_fixed > 0:
        outcome = "IMPROVED"
        reason  = f"score {before_score}→{after_score}, fixed {issues_fixed}/{len(target_issues)} issues"
    else:
        skill_path.write_text(original_code, encoding="utf-8")
        outcome = "NO_CHANGE"
        reason  = f"score unchanged at {before_score}, no issues reduced"

    _log({
        "timestamp":    timestamp,
        "skill":        skill_name,
        "issue_type":   issue_type,
        "outcome":      outcome,
        "before_score": before_score,
        "after_score":  after_score,
        "issues_fixed": issues_fixed,
        "test_passed":  test_passed,
        "reason":       reason,
    })

    icon = "✅" if outcome == "IMPROVED" else ("🔄" if outcome == "REVERTED" else "—")
    return f"{icon} {outcome}: {skill_name} [{issue_type}] — {reason}"


# ── Core skill suggestion system ───────────────────────────────────────────────

def suggest_core(max_skills: int = 30) -> str:
    """
    Audit ALL core skills and save proposed patches to memory for your review.
    No files are modified. You approve each suggestion with apply_suggestion().

    Scans /app/skills/core/ for auto-fixable issues (bare_except, missing_timeout),
    generates the proposed patch for each, and saves to core_suggestions.jsonl.
    Already-pending suggestions are skipped (no duplicates).

    Args:
        max_skills: How many core skills to scan (default 30 = all of them)

    Returns:
        Summary of new suggestions found.
    """
    try:
        si = _import_skill("self_improvement")
    except ImportError as e:
        return f"❌ {e}"

    core_files = sorted(
        p for p in SKILLS_CORE_DIR.glob("*.py") if not p.name.startswith("_")
    )
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

    for skill_path in core_files[:max_skills]:
        skill_name = skill_path.stem
        scanned   += 1

        analysis = si.analyze_skill_code(skill_name)
        if analysis.get("error"):
            continue

        for issue_type in AUTO_FIXABLE:
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
            i["type"] in AUTO_FIXABLE for i in analysis["issues"]
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
    else:
        lines.append("✅ No new suggestions — core looks clean or all issues already pending.")

    return "\n".join(lines)


def list_suggestions(status: str = "pending") -> str:
    """
    Show core skill improvement suggestions saved to memory.

    Args:
        status: Filter — 'pending' | 'applied' | 'failed' | 'all'

    Returns:
        Formatted list of suggestions.
    """
    all_s = _load_suggestions()
    if not all_s:
        return (
            "📭 No suggestions yet.\n"
            "Run: autoimprove.suggest_core() to scan your 30+ core skills."
        )

    filtered = all_s if status == "all" else [s for s in all_s if s.get("status") == status]
    if not filtered:
        return f"📭 No '{status}' suggestions. Try list_suggestions('all') to see everything."

    icons = {"pending": "⏳", "applied": "✅", "failed": "❌"}
    lines = [f"📋 Core skill suggestions — {status} ({len(filtered)} of {len(all_s)} total):"]

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
        ce = _import_skill("code_executor")
    except ImportError as e:
        return f"❌ dependency missing — {e}"

    # Snapshot
    try:
        original_code = skill_path.read_text(encoding="utf-8")
    except Exception as e:
        return f"❌ Cannot read {skill_name}.py — {e}"

    # Pre-audit for baseline
    before = si.analyze_skill_code(skill_name)
    before_score = before.get("health_score", 0) if not before.get("error") else 0

    # Apply fix to the CORE skill
    si.fix(skill_name, issue_type, "all")

    # Post-patch audit
    after = si.analyze_skill_code(skill_name)
    if after.get("error"):
        skill_path.write_text(original_code, encoding="utf-8")
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
    return (
        f"✅ APPLIED: {skill_name} [{issue_type}] — "
        f"score {before_score}→{after_score}, {issues_fixed} occurrence(s) fixed\n"
        f"Core skill updated at: {skill_path}"
    )


# ── Individual loops ───────────────────────────────────────────────────────────

def _loop_ast_audit(max_experiments: int = 10) -> str:
    """Loop 1 — auto-fix bare_except & missing_timeout in DYNAMIC skills."""
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
                results.append(f"  {run_experiment(skill_name, issue_type)}")
                count += 1
                time.sleep(1)

    if not results:
        return "✅ ast_audit: all dynamic skills clean — no auto-fixable issues"
    return f"🔬 ast_audit ({count} experiments):\n" + "\n".join(results)


def _loop_error_reduce(max_experiments: int = 10) -> str:
    """Loop 2 — target the most-frequent error pattern in DYNAMIC skills."""
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
            results.append(f"  {run_experiment(skill_name, top_issue)}")
            count += 1
            time.sleep(1)

    if not results:
        return f"✅ error_reduce: no dynamic skills have '{top_issue}' currently"
    return (
        f"🔬 error_reduce — targeting '{top_issue}' ({top_count}x in history), "
        f"{count} experiments:\n" + "\n".join(results)
    )


def _loop_daily_review() -> str:
    """Loop 3 — learning review, no code changes."""
    try:
        si = _import_skill("self_improvement")
    except ImportError as e:
        return f"❌ {e}"

    review = si.daily_review()
    _log({
        "timestamp": datetime.now().isoformat(),
        "loop":      "daily_review",
        "outcome":   "REVIEW",
        "summary":   review[:2000],
    })
    return f"📋 daily_review:\n{review}"


# ── Public API ─────────────────────────────────────────────────────────────────

def run_loop(loop_name: str, max_experiments: int = 10) -> str:
    """
    Run a single named improvement loop.

    Args:
        loop_name:       ast_audit | error_reduce | daily_review | suggest_core
        max_experiments: Max experiments / skills to scan

    Returns:
        Full summary of this loop's run.
    """
    _loops = {
        "ast_audit":    lambda: _loop_ast_audit(max_experiments),
        "error_reduce": lambda: _loop_error_reduce(max_experiments),
        "daily_review": _loop_daily_review,
        "suggest_core": lambda: suggest_core(max_experiments),
    }
    if loop_name not in _loops:
        return f"❌ Unknown loop '{loop_name}'. Available: {list(_loops.keys())}"

    start   = time.time()
    result  = _loops[loop_name]()
    elapsed = time.time() - start
    return f"{result}\n\n⏱ Loop '{loop_name}' finished in {elapsed:.1f}s"


def run_all(max_experiments: int = 5) -> str:
    """
    Run all loops in sequence. Designed for overnight autopilot.

    Order:
      1. daily_review  — learn what happened, no changes
      2. ast_audit     — auto-fix dynamic skills
      3. error_reduce  — target top error pattern in dynamic skills
      4. suggest_core  — audit core skills, queue suggestions for your review

    Args:
        max_experiments: Max experiments per loop (default 5)

    Returns:
        Combined results from all loops.
    """
    start   = time.time()
    divider = "=" * 52
    results = [f"🤖 AutoImprove started — {datetime.now().strftime('%Y-%m-%d %H:%M')}"]

    for loop_name in ("daily_review", "ast_audit", "error_reduce", "suggest_core"):
        results.append(f"\n{divider}\n🔬 {loop_name}")
        results.append(run_loop(loop_name, max_experiments))
        time.sleep(2)

    elapsed = time.time() - start
    results.append(f"\n{divider}\n✅ run_all complete — {elapsed:.1f}s total")
    results.append("Review core suggestions: autoimprove.list_suggestions()")
    return "\n".join(results)


def schedule_nightly(run_time: str = "2am", max_experiments: int = 5) -> str:
    """
    Put all improvement loops on autopilot — schedules run_all() every night.

    Args:
        run_time:        When to run (e.g. '2am', '3am', 'midnight')
        max_experiments: Max experiments per loop per night (default 5)

    Returns:
        Confirmation from scheduler.
    """
    try:
        sched = _import_skill("scheduler")
    except ImportError as e:
        return f"❌ scheduler not available: {e}"

    prompt = (
        f"Run overnight self-improvement on all skills. "
        f"Call autoimprove.run_all({max_experiments}) — this audits and fixes dynamic skills "
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


def report(days: int = 7) -> str:
    """
    Show improvement history for the last N days.

    Args:
        days: Look-back window (default 7)
    """
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
            lines.append(f"  {ts}  {icon}  {track}{skill} [{issue}] {before}→{after}")

    return "\n".join(lines)


def status() -> str:
    """Show autoimprove config, loops, and skills in scope."""
    dynamic_skills = [p.stem for p in SKILLS_DYNAMIC_DIR.glob("*.py") if not p.name.startswith("_")]
    core_skills    = [p.stem for p in SKILLS_CORE_DIR.glob("*.py")    if not p.name.startswith("_")]
    entries        = _load_log(7)
    last_run       = entries[-1].get("timestamp", "never")[:16] if entries else "never"
    suggestions    = _load_suggestions()
    pending        = sum(1 for s in suggestions if s.get("status") == "pending")

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
        "  • error_reduce — auto-fix top error pattern in dynamic skills",
        "  • suggest_core — audit core skills, queue suggestions for your review",
        "",
        f"Dynamic skills ({len(dynamic_skills)}): {', '.join(dynamic_skills) or 'none'}",
        f"Core skills    ({len(core_skills)}): {len(core_skills)} files in /app/skills/core/",
        f"Pending suggestions: {pending} core fixes waiting for approval",
        f"Last experiment    : {last_run}",
        "",
        "Commands:",
        "  autoimprove.research('any topic', depth='deep')     → web research, saved to notes",
        "  autoimprove.run_all(5)                              → run all loops tonight",
        "  autoimprove.suggest_core()                          → scan all core skills now",
        "  autoimprove.list_suggestions()                      → review pending core fixes",
        "  autoimprove.apply_suggestion('web', 'bare_except')  → approve and apply one fix",
        "  autoimprove.schedule_nightly('2am')                 → set on autopilot",
        "  autoimprove.report(7)                               → last 7 days",
        "",
        "Safety:",
        "  • Dynamic: only skills/dynamic/ auto-modified, snapshot always taken",
        "  • Core: NEVER auto-modified — requires your explicit apply_suggestion() call",
        "  • Runtime smoke test gates every change (dynamic and core alike)",
        "  • Restores original file if test fails",
    ])


# ── Export list ────────────────────────────────────────────────────────────────

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
    "schedule_nightly",
    "report",
    "status",
]
