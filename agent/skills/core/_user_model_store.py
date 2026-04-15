"""
Private module — user model, preferences, patterns, context, rejections, facts.
Not a skill. Imported by notes.py which re-exports everything under the 'notes' skill.
"""
import json
import logging
from pathlib import Path
from datetime import datetime, date, timedelta
from contextlib import contextmanager
import fcntl

logger = logging.getLogger(__name__)

USER_MODEL_FILE         = Path("/app/memory/user_model.json")
USER_FACTS_FILE         = Path("/app/memory/user_facts.json")
USER_FACTS_HISTORY_FILE = Path("/app/memory/user_facts_history.jsonl")
USER_PREFS_HISTORY_FILE = Path("/app/memory/user_prefs_history.jsonl")

MAX_KEY_LEN   = 100
MAX_VALUE_LEN = 2_000
MAX_INSIGHT   = 1_000
MAX_PATTERN   = 500
MAX_REJECTION = 500


def _trunc(s, n: int) -> str:
    return str(s)[:n]


@contextmanager
def _file_lock(path: Path):
    lock_path = path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _atomic_write(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding=encoding)
    tmp.replace(path)


# ── User Model ─────────────────────────────────────────────────────────────────
#
# Schema v2:
#   preferences:  {key: {value, confidence, source, updated_at}}
#   patterns:     [{pattern, evidence, evidence_count, last_seen, suggested_action}]
#   context:      {key: value}
#   rejections:   [{idea, reason, dismissed_at}]
#   insights:     [{date, insight}]  — free-form, kept for backward compat
#   meta:         {schema_version, last_updated}

def _load_user_model() -> dict:
    _empty = {
        "preferences": {}, "patterns": [], "context": {},
        "rejections": [], "insights": [],
        "meta": {"schema_version": 2, "last_updated": None},
    }
    if not USER_MODEL_FILE.exists():
        return _empty
    try:
        model = json.loads(USER_MODEL_FILE.read_text(encoding="utf-8"))
        if "preferences" not in model:          # v1 → v2 migration
            model = {
                **_empty,
                "insights": model.get("insights", []),
                "meta": {"schema_version": 2, "last_updated": model.get("last_updated")},
            }
        return model
    except Exception as exc:
        logger.error(f"Failed to load user model: {exc}")
        return _empty


def _save_user_model(model: dict) -> None:
    model.setdefault("meta", {})["last_updated"] = datetime.now().isoformat()
    _atomic_write(USER_MODEL_FILE, json.dumps(model, indent=2, ensure_ascii=False))


def set_preference(key: str, value, source: str = "user", confidence: float = 1.0, episode_id: str = "") -> str:
    try:
        key   = _trunc(key, MAX_KEY_LEN)
        value = _trunc(value, MAX_VALUE_LEN)
        with _file_lock(USER_MODEL_FILE):
            model = _load_user_model()
            old = model.get("preferences", {}).get(key)
            if old is not None:
                archived = {
                    "key":         key,
                    "value":       old.get("value"),
                    "source":      old.get("source", "unknown"),
                    "confidence":  old.get("confidence", 1.0),
                    "valid_from":  old.get("valid_from") or old.get("updated_at", "")[:10],
                    "valid_until": date.today().isoformat(),
                    "episode_id":  old.get("episode_id"),
                    "archived_at": datetime.now().isoformat(),
                }
                USER_PREFS_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
                with USER_PREFS_HISTORY_FILE.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(archived) + "\n")
            model["preferences"][key] = {
                "value":      value,
                "confidence": round(float(confidence), 2),
                "source":     source,
                "updated_at": datetime.now().isoformat(),
                "valid_from": date.today().isoformat(),
                "episode_id": episode_id or None,
            }
            _save_user_model(model)
        icon = {"user": "👤", "inferred": "🤖", "system": "⚙️"}.get(source, "•")
        return f"✅ Preference set: {key} = {value!r} {icon} (confidence: {float(confidence):.0%})"
    except Exception as e:
        return f"❌ set_preference error: {e}"


def get_preference(key: str, default=None):
    try:
        pref = _load_user_model()["preferences"].get(key)
        return pref["value"] if pref else default
    except Exception:
        return default


def get_preference_history(key: str) -> str:
    try:
        lines = [f"📜 Preference history: '{key}'"]
        model   = _load_user_model()
        current = model.get("preferences", {}).get(key)
        if current:
            vf     = current.get("valid_from") or current.get("updated_at", "?")[:10]
            ep_str = f" ep={current['episode_id']}" if current.get("episode_id") else ""
            lines.append(
                f"  [current]  {current['value']!r}  conf={current.get('confidence', 1.0):.0%}"
                f"  since {vf}  ({current.get('source', '?')}{ep_str})"
            )
        if USER_PREFS_HISTORY_FILE.exists():
            history = []
            for line in USER_PREFS_HISTORY_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    if e.get("key") == key:
                        history.append(e)
                except Exception:
                    continue
            for e in sorted(history, key=lambda x: x.get("valid_from") or "", reverse=True):
                ep_str = f" ep={e['episode_id']}" if e.get("episode_id") else ""
                lines.append(
                    f"  [{e.get('valid_from','?')} → {e.get('valid_until','?')}]"
                    f"  {e['value']!r}  conf={e.get('confidence', 1.0):.0%}"
                    f"  ({e.get('source','?')}{ep_str})"
                )
        if len(lines) == 1:
            return f"No preference found for key '{key}'"
        return "\n".join(lines)
    except Exception as e:
        return f"❌ get_preference_history error: {e}"


def set_context(key: str, value: str) -> str:
    try:
        key   = _trunc(key, MAX_KEY_LEN)
        value = _trunc(value, MAX_VALUE_LEN)
        with _file_lock(USER_MODEL_FILE):
            model = _load_user_model()
            model.setdefault("context", {})[key] = value
            _save_user_model(model)
        return f"✅ Context updated: {key} = {value!r}"
    except Exception as e:
        return f"❌ set_context error: {e}"


def record_pattern(pattern: str, evidence: str = "", action: str = "") -> str:
    try:
        pattern  = _trunc(pattern, MAX_PATTERN)
        evidence = _trunc(evidence, MAX_VALUE_LEN)
        action   = _trunc(action, MAX_VALUE_LEN)
        with _file_lock(USER_MODEL_FILE):
            model    = _load_user_model()
            existing = next((p for p in model.get("patterns", []) if p["pattern"] == pattern), None)
            if existing:
                existing["evidence_count"] += 1
                existing["last_seen"]       = datetime.now().isoformat()
                if action:
                    existing["suggested_action"] = action
                count = existing["evidence_count"]
            else:
                model.setdefault("patterns", []).append({
                    "pattern":          pattern,
                    "evidence":         evidence,
                    "evidence_count":   1,
                    "last_seen":        datetime.now().isoformat(),
                    "suggested_action": action,
                })
                count = 1
            _save_user_model(model)
        return f"📝 Pattern recorded: '{pattern}' (seen {count}×)"
    except Exception as e:
        return f"❌ record_pattern error: {e}"


def add_rejection(idea: str, reason: str = "") -> str:
    try:
        idea   = _trunc(idea, MAX_REJECTION)
        reason = _trunc(reason, MAX_VALUE_LEN)
        with _file_lock(USER_MODEL_FILE):
            model = _load_user_model()
            model.setdefault("rejections", []).append({
                "idea":         idea,
                "reason":       reason,
                "dismissed_at": datetime.now().isoformat()[:10],
            })
            _save_user_model(model)
        return f"🚫 Rejection recorded: '{idea}'"
    except Exception as e:
        return f"❌ add_rejection error: {e}"


def update_user_model(insight: str) -> str:
    try:
        insight = _trunc(insight, MAX_INSIGHT)
        with _file_lock(USER_MODEL_FILE):
            model = _load_user_model()
            model.setdefault("insights", []).append({
                "date":    datetime.now().isoformat()[:10],
                "insight": insight,
            })
            _save_user_model(model)
        return f"✅ User model updated: {insight[:80]}"
    except Exception as e:
        return f"❌ Error updating user model: {e}"


def get_user_model() -> str:
    try:
        if not USER_MODEL_FILE.exists():
            return "No user model built yet."
        model   = _load_user_model()
        updated = (model.get("meta") or {}).get("last_updated") or "unknown"
        lines   = [f"👤 User Model  (last updated: {updated[:10]})"]

        prefs = model.get("preferences", {})
        lines.append(f"\n🔹 Preferences ({len(prefs)}):")
        if prefs:
            for k, v in prefs.items():
                icon = {"user": "👤", "inferred": "🤖", "system": "⚙️"}.get(v.get("source", ""), "•")
                lines.append(f"  {icon} {k}: {v['value']!r}  conf={v.get('confidence', 1.0):.0%}  [{v.get('updated_at', '')[:10]}]")
        else:
            lines.append("  (none set)")

        patterns = model.get("patterns", [])
        lines.append(f"\n🔹 Patterns ({len(patterns)}):")
        if patterns:
            for p in sorted(patterns, key=lambda x: -x.get("evidence_count", 0))[:8]:
                action = f" → {p['suggested_action']}" if p.get("suggested_action") else ""
                lines.append(f"  • {p['pattern']} ({p.get('evidence_count', 1)}×){action}")
        else:
            lines.append("  (none yet)")

        ctx = model.get("context", {})
        lines.append("\n🔹 Current Context:")
        if ctx:
            for k, v in ctx.items():
                lines.append(f"  • {k}: {v}")
        else:
            lines.append("  (none active)")

        rejections = model.get("rejections", [])
        lines.append(f"\n🔹 Dismissed Ideas ({len(rejections)}):")
        if rejections:
            for r in rejections[-5:]:
                reason = f" — {r['reason']}" if r.get("reason") else ""
                lines.append(f"  • {r['idea']}{reason}")
        else:
            lines.append("  (none)")

        insights = model.get("insights", [])
        if insights:
            lines.append(f"\n🔹 Free-form Insights (last 5 of {len(insights)}):")
            for item in insights[-5:]:
                lines.append(f"  [{item['date']}] {item['insight']}")

        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error reading user model: {e}"


def get_context_for_prompt() -> str:
    try:
        model = _load_user_model()
        lines = []

        prefs = {k: v for k, v in model.get("preferences", {}).items()
                 if v.get("confidence", 1.0) >= 0.6}
        if prefs:
            lines.append("User preferences: " + ", ".join(f"{k}={v['value']!r}" for k, v in prefs.items()))

        ctx = model.get("context", {})
        if ctx:
            lines.append("Current context: " + ", ".join(f"{k}={v}" for k, v in ctx.items()))

        strong = [p for p in model.get("patterns", []) if p.get("evidence_count", 0) >= 3]
        if strong:
            parts = [
                p["pattern"] + (f" → {p['suggested_action']}" if p.get("suggested_action") else "")
                for p in strong[-4:]
            ]
            lines.append("Observed patterns: " + "; ".join(parts))

        rejections = model.get("rejections", [])
        if rejections:
            lines.append("Do not suggest: " + ", ".join(r["idea"] for r in rejections[-5:]))

        insights = model.get("insights", [])
        if insights:
            lines.append("Recent user insight: " + insights[-1]["insight"])

        return "\n".join(lines)
    except Exception as exc:
        logger.error(f"get_context_for_prompt error: {exc}")
        return ""


def prune_user_model(days_old: int = 30) -> str:
    try:
        days_old = int(days_old)
        cutoff   = (datetime.now() - timedelta(days=days_old)).isoformat()
        with _file_lock(USER_MODEL_FILE):
            model    = _load_user_model()
            before_p = len(model.get("patterns", []))
            model["patterns"] = [
                p for p in model.get("patterns", [])
                if p.get("last_seen", "") >= cutoff
            ]
            pruned_p = before_p - len(model["patterns"])

            pruned_pref = 0
            for key in list(model.get("preferences", {}).keys()):
                p = model["preferences"][key]
                if (p.get("source") == "inferred"
                        and p.get("confidence", 1.0) < 0.6
                        and p.get("updated_at", "") < cutoff):
                    del model["preferences"][key]
                    pruned_pref += 1

            _save_user_model(model)
        return f"🧹 Pruned: {pruned_p} stale patterns, {pruned_pref} low-confidence preferences"
    except Exception as e:
        return f"❌ prune_user_model error: {e}"


# ── User Facts Card ────────────────────────────────────────────────────────────

def set_user_fact(key: str, value: str, source: str = "user", episode_id: str = "") -> str:
    try:
        k     = _trunc(key.strip().lower(), MAX_KEY_LEN)
        value = _trunc(value.strip(), MAX_VALUE_LEN)
        with _file_lock(USER_FACTS_FILE):
            facts: dict = {}
            if USER_FACTS_FILE.exists():
                try:
                    facts = json.loads(USER_FACTS_FILE.read_text(encoding="utf-8"))
                except Exception:
                    facts = {}

            old = facts.get(k)
            if old is not None and not k.startswith("_"):
                if isinstance(old, dict):
                    old_val = old.get("value", "")
                    old_src = old.get("source", "unknown")
                    old_vf  = old.get("valid_from")
                    old_ep  = old.get("episode_id")
                else:
                    old_val, old_src, old_vf, old_ep = old, "unknown", None, None
                archived = {
                    "key":         k,
                    "value":       old_val,
                    "source":      old_src,
                    "valid_from":  old_vf,
                    "valid_until": date.today().isoformat(),
                    "episode_id":  old_ep,
                    "archived_at": datetime.now().isoformat(),
                }
                USER_FACTS_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
                with USER_FACTS_HISTORY_FILE.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(archived) + "\n")

            facts[k] = {
                "value":      value,
                "source":     source,
                "valid_from": date.today().isoformat(),
                "episode_id": episode_id or None,
            }
            facts["_updated"] = datetime.now().isoformat()
            _atomic_write(USER_FACTS_FILE, json.dumps(facts, indent=2, ensure_ascii=False))
        return f"✅ User fact saved: {key} = {value!r}"
    except Exception as e:
        return f"❌ set_user_fact error: {e}"


def get_user_facts_card() -> str:
    try:
        if not USER_FACTS_FILE.exists():
            return "No user facts saved yet."
        facts = json.loads(USER_FACTS_FILE.read_text(encoding="utf-8"))
        lines = []
        for k, v in facts.items():
            if k.startswith("_"):
                continue
            if isinstance(v, dict):
                src  = v.get("source", "")
                vf   = v.get("valid_from", "")
                meta = f"  [{src}, since {vf}]" if (src or vf) else ""
                lines.append(f"  {k}: {v.get('value', '')}{meta}")
            else:
                lines.append(f"  {k}: {v}")
        if not lines:
            return "No user facts saved yet."
        updated = facts.get("_updated", "")[:10]
        return f"User Facts (last updated {updated}):\n" + "\n".join(lines)
    except Exception as e:
        return f"❌ get_user_facts_card error: {e}"


def get_fact_history(key: str) -> str:
    try:
        k     = key.strip().lower()
        lines = [f"📜 Fact history: '{k}'"]

        if USER_FACTS_FILE.exists():
            facts   = json.loads(USER_FACTS_FILE.read_text(encoding="utf-8"))
            current = facts.get(k)
            if current is not None:
                if isinstance(current, dict):
                    ep_str = f" ep={current['episode_id']}" if current.get("episode_id") else ""
                    lines.append(
                        f"  [current]  {current['value']!r}"
                        f"  since {current.get('valid_from','?')}"
                        f"  ({current.get('source','?')}{ep_str})"
                    )
                else:
                    lines.append(f"  [current]  {current!r}  (no metadata)")

        if USER_FACTS_HISTORY_FILE.exists():
            history = []
            for line in USER_FACTS_HISTORY_FILE.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                    if e.get("key") == k:
                        history.append(e)
                except Exception:
                    continue
            for e in sorted(history, key=lambda x: x.get("valid_from") or "", reverse=True):
                ep_str = f" ep={e['episode_id']}" if e.get("episode_id") else ""
                lines.append(
                    f"  [{e.get('valid_from','?')} → {e.get('valid_until','?')}]"
                    f"  {e['value']!r}  ({e.get('source','?')}{ep_str})"
                )

        if len(lines) == 1:
            return f"No fact found for key '{k}'"
        return "\n".join(lines)
    except Exception as e:
        return f"❌ get_fact_history error: {e}"
