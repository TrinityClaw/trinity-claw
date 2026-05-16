"""
Private module — proactive memory engine.
Scans activity logs, monitors goals, analyzes journal, surfaces anticipatory suggestions.
Called at session start via suggest_actions() in notes.py.
"""
import json
import logging
from pathlib import Path
from datetime import datetime, date, timedelta
from collections import Counter

logger = logging.getLogger(__name__)

_ACTIVITY_LOG = Path("/app/memory/activity_log.jsonl")
_USER_MODEL_FILE = Path("/app/memory/user_model.json")
_JOURNAL_FILE = Path("/app/memory/daily_journal.jsonl")
_SESSION_LOGS = Path("/app/memory/session_logs.jsonl")

# ── Pattern Mining ─────────────────────────────────────────────────────────────

def _mine_patterns(hours: int = 168) -> list:
    """Scan activity log for recurring action patterns. Auto-creates pattern records."""
    if not _ACTIVITY_LOG.exists():
        return []

    cutoff = datetime.now() - timedelta(hours=hours)
    actions = []
    sources = []
    failed = []

    for line in _ACTIVITY_LOG.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            e = json.loads(line)
            ts = datetime.fromisoformat(e["ts"])
            if ts >= cutoff:
                actions.append(e["action"].lower())
                sources.append(e.get("source", "unknown"))
                if not e.get("ok", True):
                    failed.append(e["action"])
        except Exception:
            continue

    if not actions:
        return []

    # Find recurring actions (3+ occurrences)
    action_counts = Counter(actions)
    patterns = []

    for action, count in action_counts.most_common(10):
        if count >= 3:
            # Determine time pattern
            patterns.append({
                "pattern": f"User frequently does: {action}",
                "evidence": f"Seen {count}× in last {hours//24} days",
                "evidence_count": count,
                "suggested_action": f"Consider offering to help with '{action}' proactively",
            })

    # Failed task patterns
    if failed:
        failed_counts = Counter(failed)
        for action, count in failed_counts.most_common(3):
            if count >= 2:
                patterns.append({
                    "pattern": f"Task fails repeatedly: {action}",
                    "evidence": f"Failed {count}× recently",
                    "evidence_count": count,
                    "suggested_action": f"Investigate why '{action}' keeps failing",
                })

    # Source distribution insight
    source_counts = Counter(sources)
    top_source = source_counts.most_common(1)[0] if source_counts else None
    if top_source and top_source[1] >= 5:
        patterns.append({
            "pattern": f"Most activity comes from: {top_source[0]}",
            "evidence": f"{top_source[1]} actions from this source",
            "evidence_count": top_source[1],
            "suggested_action": "",
        })

    return patterns


# ── Goal Deadline Monitoring ───────────────────────────────────────────────────

def _check_goal_deadlines() -> list:
    """Check for goals due within 48 hours or overdue."""
    if not _USER_MODEL_FILE.exists():
        return []

    try:
        model = json.loads(_USER_MODEL_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []

    alerts = []
    today = date.today()
    soon = today + timedelta(days=2)

    for goal in model.get("goals", []):
        if goal.get("status") != "open":
            continue
        due = goal.get("due_date")
        if not due:
            continue
        try:
            due_date = date.fromisoformat(due)
        except ValueError:
            continue

        if due_date < today:
            days_over = (today - due_date).days
            alerts.append({
                "type": "overdue",
                "goal": goal["goal"],
                "due": due,
                "message": f" OVERDUE ({days_over}d): {goal['goal']}",
            })
        elif due_date <= soon:
            days_left = (due_date - today).days
            alerts.append({
                "type": "urgent",
                "goal": goal["goal"],
                "due": due,
                "message": f"⚠️ Due in {days_left}d: {goal['goal']}",
            })

    return alerts


# ── Journal Insight Analysis ──────────────────────────────────────────────────

def _analyze_journal(days: int = 7) -> list:
    """Analyze recent journal entries for real insights (not just counts)."""
    if not _JOURNAL_FILE.exists():
        return []

    try:
        entries = {}
        for line in _JOURNAL_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                e = json.loads(line)
                entries[e["date"]] = e
    except Exception:
        return []

    cutoff = (date.today() - timedelta(days=days)).isoformat()
    recent = [e for e in entries.values() if e["date"] >= cutoff and not e.get("compressed")]

    if not recent:
        return []

    insights = []

    # Analyze learned content for topic clusters
    all_learned = " ".join(e.get("learned", "") for e in recent)
    all_insights = " ".join(e.get("user_insights", "") for e in recent)

    # Topic keywords to track
    topics = {
        "web": ["web", "html", "css", "js", "site", "page", "frontend"],
        "email": ["email", "gmail", "mail", "inbox"],
        "code": ["code", "python", "script", "function", "api"],
        "data": ["data", "csv", "excel", "analysis", "chart"],
        "social": ["twitter", "x", "linkedin", "instagram", "post"],
    }

    topic_counts = {}
    for topic, keywords in topics.items():
        count = sum(1 for kw in keywords if kw in all_learned.lower())
        if count >= 2:
            topic_counts[topic] = count

    if topic_counts:
        top_topic = max(topic_counts, key=topic_counts.get)
        insights.append(f"📊 Primary focus this week: {top_topic} ({topic_counts[top_topic]} mentions)")

    # Task volume trend
    task_counts = []
    for e in sorted(recent, key=lambda x: x["date"]):
        learned = e.get("learned", "")
        count = learned.count("✅") + learned.count("❌")
        task_counts.append(count)

    if len(task_counts) >= 3:
        recent_avg = sum(task_counts[-3:]) / 3
        older_avg = sum(task_counts[:-3]) / (len(task_counts) - 3) if len(task_counts) > 3 else 0
        if recent_avg > older_avg * 1.5:
            insights.append(f"📈 Activity increased: {recent_avg:.0f} tasks/day vs {older_avg:.0f} previously")
        elif recent_avg < older_avg * 0.5 and older_avg > 0:
            insights.append(f"📉 Activity decreased: {recent_avg:.0f} tasks/day vs {older_avg:.0f} previously")

    # Failed task pattern
    all_text = all_learned + " " + all_insights
    fail_count = all_text.count("❌")
    if fail_count >= 3:
        insights.append(f"⚠️ {fail_count} failed tasks this week — consider reviewing what went wrong")

    return insights


# ── Session Context Matching ───────────────────────────────────────────────────

def _match_session_context(current_message: str) -> list:
    """Find relevant past sessions matching current topic."""
    if not _SESSION_LOGS.exists():
        return []

    try:
        msg_lower = current_message.lower()
        # Extract key terms (skip common words)
        skip = {"the", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with",
                "is", "it", "this", "that", "i", "you", "we", "a", "an", "can", "do",
                "does", "did", "will", "would", "should", "could", "have", "has", "had",
                "be", "been", "being", "am", "are", "was", "were", "not", "no", "yes",
                "ok", "get", "make", "use", "run", "help", "need", "want", "like",
                "just", "also", "very", "much", "some", "any", "all", "each", "every",
                "other", "same", "only", "own", "so", "than", "too", "s", "t", "don",
                "doesn", "didn", "won", "wouldn", "couldn", "shouldn", "isn", "aren",
                "wasn", "weren", "hasn", "haven", "hadn", "let", "lets", "please",
                "thanks", "thank", "hello", "hi", "hey", "what", "where", "when",
                "which", "who", "why", "how", "now", "then", "here", "there", "up",
                "down", "out", "off", "over", "under", "again", "once", "always",
                "never", "sometimes", "often", "usually", "already", "yet", "still",
                "even", "either", "neither", "both", "first", "last", "next", "new",
                "old", "good", "bad", "right", "wrong", "true", "false", "back",
                "check", "see", "look", "know", "think", "say", "said", "going",
                "want", "try", "going", "come", "take", "give", "find", "tell",
                "ask", "work", "call", "show", "put", "set", "keep", "hold",
                "leave", "play", "move", "live", "believe", "bring", "happen",
                "write", "provide", "sit", "stand", "lose", "pay", "meet",
                "include", "continue", "learn", "change", "lead", "understand",
                "watch", "follow", "stop", "create", "speak", "read", "allow",
                "add", "spend", "grow", "open", "walk", "win", "offer",
                "remember", "love", "consider", "appear", "buy", "wait",
                "serve", "die", "send", "expect", "build", "stay", "fall",
                "cut", "reach", "kill", "remain", "suggest", "raise",
                "pass", "sell", "require", "report", "decide", "pull"}
        terms = [w for w in msg_lower.split() if w.isalpha() and len(w) > 3 and w not in skip]

        if not terms:
            return []

        matches = []
        for line in _SESSION_LOGS.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
                user_msg = e.get("user", "").lower()
                # Count matching terms
                match_count = sum(1 for t in terms if t in user_msg)
                if match_count >= 2:
                    matches.append({
                        "timestamp": e.get("timestamp", "")[:10],
                        "preview": e.get("user", "")[:100],
                        "match_score": match_count,
                    })
            except Exception:
                continue

        # Return top 3 most relevant past sessions
        matches.sort(key=lambda x: -x["match_score"])
        return matches[:3]

    except Exception as exc:
        logger.warning(f"Session context matching failed: {exc}")
        return []


# ── Main Entry Point ───────────────────────────────────────────────────────────

def suggest_actions(current_message: str = "") -> str:
    """
    Proactive memory engine — call at session start.
    Returns a formatted block of anticipatory suggestions, goal alerts, and insights.
    """
    sections = []

    # 1. Goal deadline alerts (highest priority)
    goal_alerts = _check_goal_deadlines()
    if goal_alerts:
        sections.append("🎯 Goal Alerts:")
        for alert in goal_alerts:
            sections.append(f"  {alert['message']}")

    # 2. Journal insights
    journal_insights = _analyze_journal()
    if journal_insights:
        sections.append("📊 This Week's Insights:")
        for insight in journal_insights:
            sections.append(f"  {insight}")

    # 3. Mined patterns
    patterns = _mine_patterns()
    if patterns:
        sections.append("🔍 Detected Patterns:")
        for p in patterns[:5]:
            action = f" → {p['suggested_action']}" if p.get("suggested_action") else ""
            sections.append(f"  • {p['pattern']} ({p['evidence']}){action}")

    # 4. Session context matching (if message provided)
    if current_message:
        context_matches = _match_session_context(current_message)
        if context_matches:
            sections.append("💡 Related Past Sessions:")
            for m in context_matches:
                sections.append(f"  [{m['timestamp']}] {m['preview']}...")

    # 5. Anticipatory suggestions
    suggestions = _generate_suggestions()
    if suggestions:
        sections.append("💭 Suggestions:")
        for s in suggestions:
            sections.append(f"  {s}")

    if not sections:
        return ""

    return "\n\n".join(sections)


def _generate_suggestions() -> list:
    """Generate anticipatory suggestions based on time patterns.
    Failed task suggestions are already covered by _mine_patterns."""
    suggestions = []

    # Time-based routine suggestions
    hour = datetime.now().hour
    if 8 <= hour <= 10:
        suggestions.append("Morning routine: check emails, review goals, plan the day?")
    elif 17 <= hour <= 19:
        suggestions.append("Evening wrap-up: review today's progress, log journal entry?")

    return suggestions
