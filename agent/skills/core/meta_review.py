import json
import logging
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter, defaultdict

NAME       = "meta_review"
SHORT_DOC  = "Weekly synthesis of improvement logs, error patterns, loop ROI, and journal themes."
DOC = (
    "Synthesizes all memory files into actionable meta-insights. "
    "Functions: "
    "weekly_digest(days=7)→cross-file synthesis: top errors, loop ROI, skill improvements, journal themes, recommendations; "
    "loop_roi_report()→full-history per-loop breakdown from improvement_log.jsonl; "
    "error_trend_report(days=30)→error type frequencies with per-type fix coverage rate."
)
__all__ = ["weekly_digest", "loop_roi_report", "error_trend_report"]

_MEM              = Path("/app/memory")
_IMPROVEMENT_LOG  = _MEM / "improvement_log.jsonl"
_LESSONS_FILE     = _MEM / "lessons.jsonl"
_ERROR_PATTERNS   = _MEM / "error_patterns.json"
_JOURNAL_FILE     = _MEM / "daily_journal.jsonl"

logger = logging.getLogger(__name__)


def _read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            continue
    return entries


def _filter_days(entries: list, days: int, ts_key: str = "timestamp") -> list:
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    return [e for e in entries if e.get(ts_key, "") >= cutoff]


def weekly_digest(days: int = 7) -> str:
    """Cross-file weekly synthesis: top errors, fix coverage, code improvements, journal themes.
    days: look-back window (default 7)."""
    try:
        days  = int(days)
        lines = [f"📊 Weekly Meta-Review — last {days} days", "=" * 50]

        # ── 1. Error patterns ──────────────────────────────────────────────────
        try:
            if _ERROR_PATTERNS.exists():
                patterns = json.loads(_ERROR_PATTERNS.read_text(encoding="utf-8"))
                ranked   = sorted(patterns.items(), key=lambda x: x[1].get("count", 0), reverse=True)
                if ranked:
                    lines.append("\n🔴 Error Pattern Accumulator (all-time counts):")
                    for etype, data in ranked[:6]:
                        count    = data.get("count", 0)
                        has_fix  = "✅ fix recorded" if data.get("fix") else "❌ no fix"
                        severity = data.get("severity", "?")
                        lines.append(f"  • {etype}: {count}× [{severity}]  {has_fix}")
        except Exception as e:
            lines.append(f"  (error patterns unavailable: {e})")

        # ── 2. Lessons / fix coverage ──────────────────────────────────────────
        try:
            all_lessons   = _read_jsonl(_LESSONS_FILE)
            recent        = _filter_days(all_lessons, days)
            if recent:
                skill_ctr  = Counter(l.get("skill", "unknown") for l in recent)
                etype_ctr  = Counter(l.get("error_type", "unknown") for l in recent)
                fixed_cnt  = sum(1 for l in recent if l.get("fix_applied"))
                fix_rate   = fixed_cnt / len(recent) * 100
                lines.append(f"\n📚 Lessons (last {days}d): {len(recent)} total  fix_coverage={fix_rate:.0f}%")
                top_skills = skill_ctr.most_common(3)
                if top_skills:
                    lines.append("  Most troubled: " + ", ".join(f"{s}({c})" for s, c in top_skills))
                top_etypes = etype_ctr.most_common(3)
                if top_etypes:
                    lines.append("  Top error types: " + ", ".join(f"{et}({c})" for et, c in top_etypes))
                if fix_rate < 30:
                    lines.append(f"  ⚠️  Fix coverage below 30% — run autoimprove.run_loop('error_reduce')")
            else:
                lines.append(f"\n📚 Lessons: none in last {days} days")
        except Exception as e:
            lines.append(f"  (lessons unavailable: {e})")

        # ── 3. Actual code improvements ────────────────────────────────────────
        try:
            all_exp    = _read_jsonl(_IMPROVEMENT_LOG)
            recent_exp = _filter_days(all_exp, days)
            improved   = [e for e in recent_exp if e.get("outcome") == "IMPROVED"]
            if improved:
                lines.append(f"\n🔧 Code Improvements (last {days}d): {len(improved)} fixes applied")
                for e in improved[:5]:
                    skill  = e.get("skill", "?")
                    issue  = e.get("issue_type", "?")
                    before = e.get("before_score")
                    after  = e.get("after_score")
                    delta  = e.get("score_delta")
                    score_str = ""
                    if delta is not None:
                        score_str = f"  Δ={delta:+}"
                    elif before is not None and after is not None:
                        score_str = f"  {before}→{after}"
                    lines.append(f"  • {skill} / {issue}{score_str}")
            else:
                lines.append(f"\n🔧 Code Improvements: none applied in last {days} days")
        except Exception as e:
            lines.append(f"  (improvement log unavailable: {e})")

        # ── 4. Journal themes ──────────────────────────────────────────────────
        try:
            cutoff_date    = (datetime.now() - timedelta(days=days)).date().isoformat()
            all_journal    = _read_jsonl(_JOURNAL_FILE)
            journal_recent = [j for j in all_journal if j.get("date", "") >= cutoff_date]
            if journal_recent:
                lines.append(f"\n📔 Journal ({len(journal_recent)} entries in last {days}d):")
                for entry in sorted(journal_recent, key=lambda x: x.get("date", ""), reverse=True)[:4]:
                    label   = entry.get("date", "?")
                    summary = entry.get("summary", "")[:110]
                    if summary:
                        lines.append(f"  [{label}] {summary}")
            else:
                lines.append(f"\n📔 Journal: no entries in last {days} days")
        except Exception as e:
            lines.append(f"  (journal unavailable: {e})")

        # ── 5. Recommendations ─────────────────────────────────────────────────
        lines.append("\n💡 Recommendations:")
        recs = []
        try:
            if _ERROR_PATTERNS.exists():
                pats = json.loads(_ERROR_PATTERNS.read_text(encoding="utf-8"))
                unfixed = [(k, v) for k, v in pats.items()
                           if v.get("count", 0) >= 5 and not v.get("fix")]
                if unfixed:
                    top = max(unfixed, key=lambda x: x[1]["count"])
                    recs.append(
                        f"  ⚠️  '{top[0]}' has {top[1]['count']}× with no fix — "
                        f"run autoimprove.run_loop('error_reduce')"
                    )
        except Exception:
            pass
        try:
            recent_lessons = _filter_days(_read_jsonl(_LESSONS_FILE), days)
            if recent_lessons:
                rate = sum(1 for l in recent_lessons if l.get("fix_applied")) / len(recent_lessons)
                if rate < 0.3:
                    recs.append(
                        f"  📉 Fix coverage {rate:.0%} — run autoimprove.run_loop('ast_audit')"
                    )
        except Exception:
            pass
        if not recs:
            recs.append("  ✅ No critical issues detected — system healthy")
        lines.extend(recs)

        return "\n".join(lines)

    except Exception as exc:
        logger.error("weekly_digest error: %s", exc)
        return f"❌ weekly_digest error: {exc}"


def loop_roi_report() -> str:
    """Full-history per-loop breakdown: experiment counts and score deltas from improvement_log.jsonl."""
    try:
        experiments = _read_jsonl(_IMPROVEMENT_LOG)
        if not experiments:
            return "📭 No improvement log found."

        # Classify by loop and outcome
        loop_stats = defaultdict(lambda: {"runs": 0, "improved": 0, "deltas": []})
        for e in experiments:
            loop = e.get("loop", "unknown")
            loop_stats[loop]["runs"] += 1
            if e.get("outcome") == "IMPROVED":
                loop_stats[loop]["improved"] += 1
            delta = e.get("score_delta")
            if delta is not None:
                try:
                    loop_stats[loop]["deltas"].append(float(delta))
                except (ValueError, TypeError):
                    pass

        lines = [f"🔧 Loop ROI Report — {len(experiments)} total log entries", "=" * 45]
        for loop_name, s in sorted(loop_stats.items(), key=lambda x: -x[1]["runs"]):
            improved_pct = s["improved"] / s["runs"] * 100 if s["runs"] else 0
            avg_delta    = ""
            if s["deltas"]:
                avg = sum(s["deltas"]) / len(s["deltas"])
                avg_delta = f"  avg_delta={avg:+.1f}"
            lines.append(
                f"  {loop_name:<22} {s['runs']:>4} runs  "
                f"{s['improved']:>3} improved ({improved_pct:.0f}%){avg_delta}"
            )
        return "\n".join(lines)

    except Exception as exc:
        logger.error("loop_roi_report error: %s", exc)
        return f"❌ loop_roi_report error: {exc}"


def error_trend_report(days: int = 30) -> str:
    """Error type frequencies over the last N days with per-type fix coverage."""
    try:
        days    = int(days)
        lessons = _filter_days(_read_jsonl(_LESSONS_FILE), days)
        if not lessons:
            return f"📭 No lessons in the last {days} days."

        by_type = defaultdict(lambda: {"count": 0, "fixed": 0, "skills": set()})
        for l in lessons:
            et = l.get("error_type", "unknown")
            by_type[et]["count"]  += 1
            if l.get("fix_applied"):
                by_type[et]["fixed"] += 1
            skill = l.get("skill")
            if skill:
                by_type[et]["skills"].add(skill)

        lines = [
            f"📈 Error Trend — last {days}d  ({len(lessons)} lessons)", "=" * 50
        ]
        for etype, s in sorted(by_type.items(), key=lambda x: -x[1]["count"]):
            cov      = s["fixed"] / s["count"] * 100 if s["count"] else 0
            sk_str   = ", ".join(sorted(s["skills"])[:3]) or "?"
            lines.append(
                f"  {etype:<30} {s['count']:>4}×  fix={cov:.0f}%  [{sk_str}]"
            )

        # Flag types unknown to error_patterns.json
        if _ERROR_PATTERNS.exists():
            try:
                known   = set(json.loads(_ERROR_PATTERNS.read_text(encoding="utf-8")).keys())
                unknown = [et for et in by_type if et not in known]
                if unknown:
                    lines.append(
                        "\n⚠️  Not tracked in error_patterns.json: " + ", ".join(unknown)
                    )
            except Exception:
                pass

        return "\n".join(lines)

    except Exception as exc:
        logger.error("error_trend_report error: %s", exc)
        return f"❌ error_trend_report error: {exc}"
