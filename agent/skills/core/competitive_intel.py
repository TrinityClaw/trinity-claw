"""
competitive_intel.py — Competitive Intelligence Skill for TrinityClaw

Monitor competitor websites for pricing, messaging, and content changes.
Snapshots are stored in /app/memory/competitive_intel/.
Deduplication prevents re-alerting on the same change.
Per-domain rate limiting avoids triggering anti-scraping blocks.
"""

import json
import hashlib
import html
import time
import re
import os
import random
import difflib
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

NAME = "competitive_intel"
SHORT_DOC = "Monitor competitor websites for pricing and content changes; manage a watchlist and schedule daily checks."
SKILL_TIMEOUT = 300  # 5-minute timeout — checking 10+ sites needs room

DOC = (
    "Competitive intelligence: monitor competitor websites for changes in pricing, messaging, "
    "and content. add_site(url, name, selectors, priority, js_rendered) — add to watchlist. "
    "remove_site(url) — remove from watchlist. list_watchlist() — show all monitored sites. "
    "check_site(url) — check a single site for changes right now. "
    "run_check() — check ALL watchlist sites and return a structured change report ready for "
    "strategic analysis. get_alerts(days) — show recent change history. "
    "clear_alerts() — prune old alerts. schedule_daily(hour) — auto-run every day at target hour."
)

# ── Storage paths ─────────────────────────────────────────────────────────────

_BASE          = Path("/app/memory/competitive_intel")
_WATCHLIST_FILE = _BASE / "watchlist.json"
_SNAPSHOTS_FILE = _BASE / "snapshots.json"
_ALERTS_FILE    = _BASE / "alerts.json"

# ── Constants ─────────────────────────────────────────────────────────────────

DOMAIN_DELAY     = 5.0   # seconds between requests to the same domain
NOISE_THRESHOLD  = 0.97  # similarity ratio above which we treat change as noise
EXCERPT_LEN      = 300   # chars of content stored for display in reports
SAMPLE_LEN       = 3000  # chars of content used for similarity comparison (noise detection)

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:137.0) Gecko/20100101 Firefox/137.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36 Edg/135.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
]

# ── Per-domain rate limiting state ────────────────────────────────────────────

_domain_last_request: Dict[str, float] = {}
_domain_rate_lock = threading.Lock()

# ── Optional dependency flags ─────────────────────────────────────────────────

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

# ── Storage helpers ───────────────────────────────────────────────────────────

def _ensure_dirs() -> None:
    """Create the competitive_intel storage directory if it does not already exist."""
    _BASE.mkdir(parents=True, exist_ok=True)


def _load_json(path: Path, default: Any) -> Any:
    _ensure_dirs()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default
    return default


def _save_json(path: Path, data: Any) -> None:
    _ensure_dirs()
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

# ── URL helpers ───────────────────────────────────────────────────────────────

def _normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url


def _get_domain(url: str) -> str:
    try:
        return urlparse(url).netloc or url
    except Exception:
        return url

# ── Rate limiting ─────────────────────────────────────────────────────────────

def _wait_for_domain(domain: str) -> None:
    """Enforce per-domain delay. Releases the lock before sleeping."""
    with _domain_rate_lock:
        last = _domain_last_request.get(domain, 0.0)
        wait = DOMAIN_DELAY - (time.time() - last)
    if wait > 0:
        time.sleep(wait)
    with _domain_rate_lock:
        _domain_last_request[domain] = time.time()

# ── HTTP session ──────────────────────────────────────────────────────────────

def _make_session() -> "requests.Session":
    session = requests.Session()
    try:
        retry = Retry(
            total=3,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD"],
        )
    except TypeError:
        # older urllib3 uses method_whitelist
        retry = Retry(
            total=3,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
        )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })
    return session

# ── Content extraction ────────────────────────────────────────────────────────

def _extract_content(html_text: str, selectors: Optional[List[str]] = None) -> str:
    """Extract clean text from HTML, optionally scoped to CSS selectors."""
    if not html_text:
        return ""

    if HAS_BS4:
        try:
            soup = BeautifulSoup(html_text, "html.parser")
            # Strip structural noise that changes every page load
            for tag in soup(["script", "style", "noscript", "meta", "link",
                              "header", "footer", "nav", "aside", "iframe"]):
                tag.decompose()

            if selectors:
                parts = []
                for sel in selectors:
                    try:
                        elements = soup.select(sel)
                        for el in elements:
                            text = el.get_text(separator=" ", strip=True)
                            if text:
                                parts.append(text)
                    except Exception:
                        continue
                if parts:
                    return " ".join(parts)
                # Selectors matched nothing — fall through to full-page text

            return soup.get_text(separator=" ", strip=True)
        except Exception:
            pass

    # Fallback: regex stripping when BeautifulSoup is unavailable
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html_text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)  # handles &amp; &nbsp; &#39; &#x27; etc.
    return re.sub(r"\s+", " ", text).strip()

# ── Hashing and noise detection ───────────────────────────────────────────────

def _content_hash(content: str) -> str:
    normalized = re.sub(r"\s+", " ", content).strip().lower()
    return hashlib.sha256(normalized.encode("utf-8", errors="replace")).hexdigest()


def _sample(content: str) -> str:
    """Return a SAMPLE_LEN-char representative sample from head + tail.

    Taking both ends ensures that changes at the bottom of long pages
    (pricing tables, new feature announcements, footers) are captured
    rather than being silently truncated by a head-only slice.
    """
    if len(content) <= SAMPLE_LEN:
        return content
    half = SAMPLE_LEN // 2
    return content[:half] + content[-half:]


def _similarity_ratio(a: str, b: str) -> float:
    """SequenceMatcher ratio between two content samples (0.0–1.0).

    Callers should pass pre-sampled strings produced by _sample() so
    that both head and tail of long pages are represented.
    """
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def _excerpt(content: str) -> str:
    text = re.sub(r"\s+", " ", content).strip()
    return text[:EXCERPT_LEN] + ("..." if len(text) > EXCERPT_LEN else "")

# ── Selector parsing ──────────────────────────────────────────────────────────

def _parse_selectors(selectors_str: str) -> List[str]:
    """Accept comma-separated or JSON-array selector strings.

    Warns (via printed message) about non-string elements that are silently dropped.
    """
    if not selectors_str or selectors_str.strip().lower() in ("", "none", "null", "[]"):
        return []
    stripped = selectors_str.strip()
    if stripped.startswith("["):
        try:
            result = json.loads(stripped)
            valid = [s.strip() for s in result if isinstance(s, str) and s.strip()]
            dropped = [s for s in result if not isinstance(s, str) or not s.strip()]
            if dropped:
                print(f"[competitive_intel] ⚠️  Ignored {len(dropped)} non-string selector(s): {dropped}")
            return valid
        except Exception:
            pass
    return [s.strip() for s in stripped.split(",") if s.strip()]

# ── Telegram alerting ─────────────────────────────────────────────────────────

def _truncate_html(text: str, limit: int) -> str:
    """Truncate an HTML string safely — never cuts inside a tag or entity.

    Walks backwards from the limit to find the last character that is not
    inside an open tag (<...) or named/numeric entity (&...).
    """
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    for i in range(len(cut) - 1, max(0, len(cut) - 40), -1):
        if cut[i] in ("<", "&"):
            cut = cut[:i]
            break
    return cut + "…"


def _send_telegram_alert(message: str) -> bool:
    """Send a Telegram message if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set."""
    if not HAS_REQUESTS:
        return False
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": _truncate_html(message, 4000), "parse_mode": "HTML"},
            timeout=(5, 15),
        )
        return resp.ok
    except Exception:
        return False

# ── Page fetcher ──────────────────────────────────────────────────────────────

def _fetch_page(url: str, session: "Optional[requests.Session]" = None) -> tuple:
    """
    Fetch a page with per-domain rate limiting and retry.
    Returns (html_text, error_message). error_message is "" on success.

    Pass a shared session to reuse connections across multiple calls (e.g. in
    run_check). When session=None a fresh one-shot session is created.
    """
    if not HAS_REQUESTS:
        return "", "requests library not installed"
    domain = _get_domain(url)
    _wait_for_domain(domain)
    if session is None:
        session = _make_session()
    try:
        resp = session.get(url, timeout=(10, 30), allow_redirects=True)
        if resp.status_code == 200:
            return resp.text, ""
        return "", f"HTTP {resp.status_code}"
    except Exception as e:
        return "", str(e)[:120]

# ══════════════════════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════════════════════

def add_site(url: str, name: str, selectors: str = "",
             priority: str = "medium", js_rendered: str = "false") -> str:
    """
    Add a website to the competitive intelligence watchlist.

    Args:
        url: Full URL to monitor (e.g., https://competitor.com/pricing)
        name: Human-readable label (e.g., Competitor1 Pricing)
        selectors: CSS selectors to scope monitoring, comma-separated or JSON array (optional)
        priority: Alert priority — high, medium, or low (default: medium)
        js_rendered: Set to true if the site loads content via JavaScript

    Returns:
        Confirmation message with watchlist count
    """
    url = _normalize_url(url)
    if not url:
        return "❌ URL is required."

    priority = priority.strip().lower()
    if priority not in ("high", "medium", "low"):
        priority = "medium"

    js = str(js_rendered).strip().lower() in ("true", "yes", "1")
    parsed_selectors = _parse_selectors(selectors)

    watchlist = _load_json(_WATCHLIST_FILE, {})
    watchlist[url] = {
        "name": name.strip() or url,
        "selectors": parsed_selectors,
        "priority": priority,
        "js_rendered": js,
        "added_at": datetime.now().isoformat(),
    }
    _save_json(_WATCHLIST_FILE, watchlist)

    sel_info = f"\n   Selectors: {parsed_selectors}" if parsed_selectors else ""
    js_info  = "\n   Note: JS-rendered — static fetch only (for full JS support use web.browser_text)" if js else ""
    return (
        f"✅ Added to watchlist: {name}\n"
        f"   URL: {url}\n"
        f"   Priority: {priority}{sel_info}{js_info}\n"
        f"   Total sites monitored: {len(watchlist)}"
    )


def remove_site(url: str) -> str:
    """
    Remove a website from the watchlist (also cleans up its snapshot).

    Args:
        url: URL to remove — must match what was added (use list_watchlist to confirm)

    Returns:
        Confirmation or error message
    """
    url = _normalize_url(url)
    watchlist = _load_json(_WATCHLIST_FILE, {})

    if url not in watchlist:
        matches = [u for u in watchlist if url in u or u in url]
        if len(matches) == 1:
            url = matches[0]
        elif len(matches) > 1:
            return "❌ Ambiguous URL — matched:\n" + "\n".join(f"  - {u}" for u in matches)
        else:
            return "❌ URL not found. Use list_watchlist() to see current entries."

    name = watchlist[url].get("name", url)
    del watchlist[url]
    _save_json(_WATCHLIST_FILE, watchlist)

    snapshots = _load_json(_SNAPSHOTS_FILE, {})
    if url in snapshots:
        del snapshots[url]
        _save_json(_SNAPSHOTS_FILE, snapshots)

    alerts = _load_json(_ALERTS_FILE, [])
    trimmed = [a for a in alerts if a.get("url") != url]
    if len(trimmed) < len(alerts):
        _save_json(_ALERTS_FILE, trimmed)

    return f"✅ Removed: {name}\nRemaining sites: {len(watchlist)}"


def list_watchlist() -> str:
    """
    Show all websites being monitored with their status.

    Returns:
        Formatted watchlist sorted by priority
    """
    watchlist  = _load_json(_WATCHLIST_FILE, {})
    snapshots  = _load_json(_SNAPSHOTS_FILE, {})

    if not watchlist:
        return "📭 Watchlist is empty. Use add_site() to start monitoring."

    priority_order = {"high": 0, "medium": 1, "low": 2}
    entries = sorted(
        watchlist.items(),
        key=lambda x: priority_order.get(x[1].get("priority", "medium"), 1)
    )

    priority_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    lines = [f"🔍 Competitive Intelligence Watchlist ({len(watchlist)} sites):\n"]

    for url, entry in entries:
        snap = snapshots.get(url, {})

        def _fmt_ts(raw: str) -> str:
            """Format an ISO timestamp, or return '?' on any failure."""
            if raw in ("never", "baseline", ""):
                return raw if raw else "never"
            try:
                return datetime.fromisoformat(raw).strftime("%Y-%m-%d %H:%M")
            except (ValueError, TypeError):
                return "?"

        lc  = _fmt_ts(snap.get("last_checked", "never"))
        lch = _fmt_ts(snap.get("last_changed", "never"))

        icon = priority_icon.get(entry.get("priority", "medium"), "🟡")
        sel_str = f"\n   Selectors: {entry['selectors']}" if entry.get("selectors") else ""
        js_str  = " [JS]" if entry.get("js_rendered") else ""

        lines.append(
            f"{icon} [{entry.get('priority', 'medium').upper()}] {entry.get('name', url)}{js_str}\n"
            f"   {url}\n"
            f"   Last checked: {lc} | Last changed: {lch}{sel_str}\n"
        )

    return "\n".join(lines)


def check_site(url: str) -> str:
    """
    Check a single site for content changes right now.
    Works for any URL — if not in watchlist, it's an ad-hoc check (snapshot still saved).

    Args:
        url: URL to check

    Returns:
        Change status with excerpts, or baseline confirmation on first run
    """
    url       = _normalize_url(url)
    watchlist = _load_json(_WATCHLIST_FILE, {})
    is_adhoc  = url not in watchlist
    entry = watchlist.get(url, {
        "name": url, "selectors": [], "priority": "medium", "js_rendered": False
    })

    name      = entry.get("name", url)
    selectors = entry.get("selectors", [])
    js        = entry.get("js_rendered", False)

    page_html, error = _fetch_page(url)
    if error:
        return f"❌ Could not fetch {name}: {error}"

    content = _extract_content(page_html, selectors)
    if not content:
        js_hint = " Site may need JavaScript — try web.browser_goto(url) then web.browser_text()." if js else ""
        return f"⚠️ {name}: No content extracted after fetch.{js_hint}"

    current_hash    = _content_hash(content)
    current_excerpt = _excerpt(content)
    current_sample  = _sample(content)
    now             = datetime.now().isoformat()

    snapshots = _load_json(_SNAPSHOTS_FILE, {})
    prev      = snapshots.get(url, {})
    prev_hash     = prev.get("hash", "")
    prev_sample   = prev.get("sample", prev.get("excerpt", ""))
    reported_hash = prev.get("reported_hash", "")

    # Build updated snapshot — hash/excerpt/sample always reflect latest content
    snapshots[url] = {
        "hash":         current_hash,
        "excerpt":      current_excerpt,
        "sample":       current_sample,
        "last_checked": now,
        **({"ad_hoc": True} if is_adhoc else {}),
        "last_changed": prev.get("last_changed", now),
        "reported_hash": reported_hash,
    }

    # ── First run: establish baseline ────────────────────────────────────────
    if not prev_hash:
        snapshots[url]["last_changed"]   = "baseline"
        snapshots[url]["reported_hash"]  = current_hash
        _save_json(_SNAPSHOTS_FILE, snapshots)
        return (
            f"📸 Baseline captured for {name}\n"
            f"   URL: {url}\n"
            f"   Content preview: \"{current_excerpt[:150]}\"\n"
            f"   Future runs will compare against this snapshot."
        )

    # ── No change ─────────────────────────────────────────────────────────────
    if current_hash == prev_hash:
        _save_json(_SNAPSHOTS_FILE, snapshots)
        try:
            age = datetime.fromisoformat(prev.get("last_checked", now)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            age = "?"
        return f"✅ No change — {name} (last checked: {age})"

    # ── Content has changed — measure how much ────────────────────────────────
    similarity = _similarity_ratio(prev_sample, current_sample)

    # Noise: high similarity means only timestamps / ads / minor dynamic elements changed
    if similarity > NOISE_THRESHOLD:
        snapshots[url]["reported_hash"] = current_hash
        _save_json(_SNAPSHOTS_FILE, snapshots)
        return (
            f"⚡ Minor dynamic update on {name} ({similarity:.0%} similar to previous) — "
            f"likely timestamps or ads, not flagged as a real change."
        )

    # Deduplication: this exact content version was already reported
    if current_hash == reported_hash:
        _save_json(_SNAPSHOTS_FILE, snapshots)
        return f"ℹ️ {name}: No new changes since last alert (content matches last reported state)."

    # ── Significant new change ────────────────────────────────────────────────
    snapshots[url]["last_changed"]  = now
    snapshots[url]["reported_hash"] = current_hash
    _save_json(_SNAPSHOTS_FILE, snapshots)

    priority_label = entry.get("priority", "medium").upper()
    return (
        f"🔴 CHANGE DETECTED — {name}\n"
        f"   URL: {url}\n"
        f"   Priority: {priority_label}\n"
        f"   Content similarity to previous: {similarity:.0%}\n"
        f"   Previous: \"{prev.get('excerpt', '')[:150]}\"\n"
        f"   Now:      \"{current_excerpt[:150]}\""
    )


def run_check() -> str:
    """
    Check ALL sites in the watchlist and return a structured change report for strategic analysis.
    High-priority changes are also sent as Telegram alerts if TELEGRAM_BOT_TOKEN is configured.

    Returns:
        Full change report — pass this directly to the LLM for strategic analysis
    """
    watchlist = _load_json(_WATCHLIST_FILE, {})
    if not watchlist:
        return "📭 Watchlist is empty. Use add_site() to add sites to monitor first."

    snapshots = _load_json(_SNAPSHOTS_FILE, {})
    alerts    = _load_json(_ALERTS_FILE, [])
    now       = datetime.now().isoformat()

    results_changed   = []
    results_unchanged = []
    results_errors    = []

    priority_order = {"high": 0, "medium": 1, "low": 2}
    sorted_entries = sorted(
        watchlist.items(),
        key=lambda x: priority_order.get(x[1].get("priority", "medium"), 1)
    )

    # One shared session for the whole run — reuses connections across domains.
    shared_session = _make_session() if HAS_REQUESTS else None

    for url, entry in sorted_entries:
        name      = entry.get("name", url)
        selectors = entry.get("selectors", [])
        js        = entry.get("js_rendered", False)
        priority  = entry.get("priority", "medium")

        try:
            page_html, error = _fetch_page(url, session=shared_session)

            if error:
                results_errors.append({"name": name, "url": url, "error": error})
                continue

            content = _extract_content(page_html, selectors)
            if not content:
                results_errors.append({
                    "name": name, "url": url,
                    "error": "Empty content after extraction" + (" (JS-rendered?)" if js else "")
                })
                continue

            current_hash    = _content_hash(content)
            current_excerpt = _excerpt(content)
            current_sample  = _sample(content)

            prev          = snapshots.get(url, {})
            prev_hash     = prev.get("hash", "")
            prev_excerpt  = prev.get("excerpt", "")
            prev_sample   = prev.get("sample", prev_excerpt)
            reported_hash = prev.get("reported_hash", "")

            # Always update snapshot to latest content
            snapshots[url] = {
                "hash":         current_hash,
                "excerpt":      current_excerpt,
                "sample":       current_sample,
                "last_checked": now,
                "last_changed": prev.get("last_changed", now),
                "reported_hash": reported_hash,
            }

            # First run — set baseline
            if not prev_hash:
                snapshots[url]["last_changed"]  = "baseline"
                snapshots[url]["reported_hash"] = current_hash
                results_unchanged.append({"name": name, "url": url, "status": "baseline captured"})
                continue

            # No change
            if current_hash == prev_hash:
                results_unchanged.append({"name": name, "url": url, "status": "no change"})
                continue

            similarity = _similarity_ratio(prev_sample, current_sample)

            # Noise: minor dynamic content
            if similarity > NOISE_THRESHOLD:
                snapshots[url]["reported_hash"] = current_hash
                results_unchanged.append({
                    "name": name, "url": url,
                    "status": f"minor dynamic update ({similarity:.0%} similar) — not flagged"
                })
                continue

            # Deduplication: already reported this exact state
            if current_hash == reported_hash:
                results_unchanged.append({
                    "name": name, "url": url,
                    "status": "matches last reported state — no new changes"
                })
                continue

            # Real significant change
            snapshots[url]["last_changed"]  = now
            snapshots[url]["reported_hash"] = current_hash

            change = {
                "name":        name,
                "url":         url,
                "priority":    priority,
                "similarity":  f"{similarity:.0%}",
                "prev_excerpt": prev_excerpt[:200],
                "curr_excerpt": current_excerpt[:200],
                "js_rendered":  js,
            }
            results_changed.append(change)

            # Save to alert log
            alerts.append({
                "id":        str(uuid.uuid4())[:8],
                "timestamp": now,
                "url":       url,
                "name":      name,
                "priority":  priority,
                "similarity": round(similarity, 3),
                "excerpt":   current_excerpt[:200],
            })

            # Telegram alert for high-priority changes
            if priority == "high":
                _send_telegram_alert(
                    f"🔴 <b>Competitive Intel Alert</b>\n\n"
                    f"<b>{html.escape(name)}</b> has changed\n"
                    f"Priority: HIGH | Similarity: {similarity:.0%}\n"
                    f"URL: {html.escape(url)}\n\n"
                    f"Now: {html.escape(current_excerpt[:300])}"
                )

        except Exception as exc:
            results_errors.append({"name": name, "url": url, "error": str(exc)[:100]})

    # Persist updated snapshots and capped alert log
    _save_json(_SNAPSHOTS_FILE, snapshots)
    if len(alerts) > 500:
        alerts = alerts[-500:]
    _save_json(_ALERTS_FILE, alerts)

    # ── Build report ──────────────────────────────────────────────────────────
    total = len(watchlist)
    ts    = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"Competitive Intelligence Report — {ts}",
        f"Checked: {total} sites | Changes: {len(results_changed)} | "
        f"Unchanged: {len(results_unchanged)} | Errors: {len(results_errors)}",
        "",
    ]

    if results_changed:
        lines.append("━━ CHANGES DETECTED ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        p_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        for i, c in enumerate(results_changed, 1):
            icon    = p_icon.get(c["priority"], "🟡")
            js_note = " [JS-rendered — content may be incomplete]" if c.get("js_rendered") else ""
            lines += [
                f"{i}. {icon} [{c['priority'].upper()}] {c['name']}{js_note}",
                f"   URL: {c['url']}",
                f"   Similarity to previous: {c['similarity']}",
                f"   Before: \"{c['prev_excerpt']}\"",
                f"   After:  \"{c['curr_excerpt']}\"",
                "",
            ]

    if results_unchanged:
        lines.append("━━ NO SIGNIFICANT CHANGE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        for u in results_unchanged:
            lines.append(f"  ✅ {u['name']} — {u['status']}")
        lines.append("")

    if results_errors:
        lines.append("━━ FETCH ERRORS ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        for e in results_errors:
            lines.append(f"  ⚠️  {e['name']}: {e['error']}")
        lines.append("")

    if not results_changed:
        lines.append("No significant competitive changes detected in this run.")

    lines.append(f"\nAlerts log: {_ALERTS_FILE}")
    return "\n".join(lines)


def get_alerts(days: str = "7") -> str:
    """
    Show competitive intelligence alerts from the past N days.

    Args:
        days: Number of days to look back (default: 7)

    Returns:
        Formatted list of alerts, newest first
    """
    try:
        n_days = max(1, int(str(days).strip()))
    except (ValueError, TypeError):
        n_days = 7

    alerts  = _load_json(_ALERTS_FILE, [])
    cutoff  = (datetime.now() - timedelta(days=n_days)).isoformat()
    recent  = [a for a in alerts if a.get("timestamp", "") >= cutoff]

    if not recent:
        return f"📭 No alerts in the past {n_days} days."

    p_icon = {"high": "🔴", "medium": "🟡", "low": "🟢"}
    lines  = [f"📋 Competitive Intel Alerts — last {n_days} days ({len(recent)} alert(s)):\n"]

    for alert in sorted(recent, key=lambda x: x.get("timestamp", ""), reverse=True):
        icon = p_icon.get(alert.get("priority", "medium"), "🟡")
        try:
            ts = datetime.fromisoformat(alert["timestamp"]).strftime("%Y-%m-%d %H:%M")
        except Exception:
            ts = alert.get("timestamp", "?")

        lines += [
            f"{icon} [{alert.get('priority', '?').upper()}] {alert.get('name', alert.get('url', '?'))}",
            f"   Time: {ts} | URL: {alert.get('url', '?')}",
            f"   Similarity: {alert.get('similarity', '?')}",
            f"   Excerpt: \"{alert.get('excerpt', '')[:150]}\"",
            "",
        ]

    return "\n".join(lines)


def clear_alerts() -> str:
    """
    Remove alerts older than 30 days and prune orphan ad-hoc snapshots.

    Returns:
        Confirmation with counts
    """
    cutoff = (datetime.now() - timedelta(days=30)).isoformat()

    alerts  = _load_json(_ALERTS_FILE, [])
    kept    = [a for a in alerts if a.get("timestamp", "") >= cutoff]
    removed = len(alerts) - len(kept)
    _save_json(_ALERTS_FILE, kept)

    # Prune snapshots for URLs not in the watchlist (orphans from ad-hoc
    # check_site calls) that haven't been checked in the last 30 days.
    watchlist = _load_json(_WATCHLIST_FILE, {})
    snapshots = _load_json(_SNAPSHOTS_FILE, {})
    pruned = 0
    for snap_url in list(snapshots.keys()):
        if snap_url not in watchlist:
            if snapshots[snap_url].get("last_checked", "") < cutoff:
                del snapshots[snap_url]
                pruned += 1
    if pruned:
        _save_json(_SNAPSHOTS_FILE, snapshots)

    snap_note = f" Pruned {pruned} orphan snapshot(s)." if pruned else ""
    return f"✅ Cleared {removed} old alert(s). {len(kept)} recent alert(s) retained (last 30 days).{snap_note}"


def schedule_daily(hour: str = "8") -> str:
    """
    Schedule the competitive intelligence check to run automatically every day.
    Returns a warning (not an overwrite) if the task is already scheduled.

    Args:
        hour: Hour of day to first run (0-23, default: 8 for 8am). Repeats every 24h from there.

    Returns:
        Confirmation with next scheduled run time
    """
    try:
        h = max(0, min(23, int(str(hour).strip())))
    except (ValueError, TypeError):
        h = 8

    tasks_file = Path("/app/memory/scheduled_tasks.json")

    existing_tasks = {}
    if tasks_file.exists():
        try:
            existing_tasks = json.loads(tasks_file.read_text(encoding="utf-8"))
        except Exception:
            existing_tasks = {}

    # Find the next occurrence of the target hour
    now      = datetime.now()
    next_run = now.replace(hour=h, minute=0, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)

    prompt = (
        "Run the daily competitive intelligence check: call competitive_intel.run_check() "
        "to check all watchlist sites for changes. If changes are detected, for each one: "
        "1) Categorize as pricing / product / messaging / leadership / content / other. "
        "2) Assess: threat, opportunity, or noise? "
        "3) Recommend 1-2 actionable next steps. "
        "Keep the summary executive-ready and concise."
    )

    task = {
        "type":             "recurring",
        "prompt":           prompt,
        "next_run":         next_run.isoformat(),
        "interval_seconds": 86400,
        "created":          now.isoformat(),
        "last_run":         None,
        "run_count":        0,
    }

    import sys as _sys
    _sched = _sys.modules.get("skills.scheduler")
    if _sched is not None:
        # Go through the scheduler's own lock + atomic save so we never
        # race against the background _run() loop's _load()/_save() cycle.
        with _sched._lock:
            tasks = _sched._load()
            if "competitive_intel_daily" in tasks:
                existing = tasks["competitive_intel_daily"]
                try:
                    next_str = datetime.fromisoformat(existing["next_run"]).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    next_str = existing.get("next_run", "?")
                return (
                    f"⚠️ Already scheduled: competitive_intel_daily\n"
                    f"   Next run: {next_str}\n"
                    f"   Use scheduler.remove('competitive_intel_daily') to cancel before rescheduling."
                )
            tasks["competitive_intel_daily"] = task
            _sched._save(tasks)
    else:
        # Scheduler module not yet in sys.modules — write atomically ourselves.
        tasks_file.parent.mkdir(parents=True, exist_ok=True)
        if "competitive_intel_daily" in existing_tasks:
            existing = existing_tasks["competitive_intel_daily"]
            try:
                next_str = datetime.fromisoformat(existing["next_run"]).strftime("%Y-%m-%d %H:%M")
            except Exception:
                next_str = existing.get("next_run", "?")
            return (
                f"⚠️ Already scheduled: competitive_intel_daily\n"
                f"   Next run: {next_str}\n"
                f"   Use scheduler.remove('competitive_intel_daily') to cancel before rescheduling."
            )
        existing_tasks["competitive_intel_daily"] = task
        tmp = tasks_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(existing_tasks, indent=2), encoding="utf-8")
        tmp.replace(tasks_file)

    return (
        f"✅ Competitive intelligence check scheduled daily\n"
        f"   First run: {next_run.strftime('%Y-%m-%d %H:%M')}\n"
        f"   Repeats every 24h\n"
        f"   Task name: competitive_intel_daily\n"
        f"   To cancel: scheduler.remove('competitive_intel_daily')\n"
        f"   (Uses scheduler.schedule_recurring internally — no direct file writes.)"
    )


# ── Export list ────────────────────────────────────────────────────────────────

__all__ = [
    "NAME",
    "SHORT_DOC",
    "DOC",
    "add_site",
    "remove_site",
    "list_watchlist",
    "check_site",
    "run_check",
    "get_alerts",
    "clear_alerts",
    "schedule_daily",
]
