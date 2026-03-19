import os
import logging
import random
import time
from pathlib import Path
from datetime import datetime

NAME = "browser_session"
DOC = (
    "Control your existing logged-in Chrome browser from the agent via CDP (Chrome DevTools Protocol). "
    "Unlike the 'web' skill which launches a fresh private browser, this skill ATTACHES to your "
    "running Chrome — so it sees all your sessions, logins, cookies, and open tabs. "
    "One-time setup: launch Chrome with --remote-debugging-port=9222. "

    "=== PREFERRED INTERACTION PATTERN (no CSS selectors needed) === "
    "Step 1: goto(url) to navigate. "
    "Step 2: get_snapshot() — see every interactive element with @eN refs (like agent-browser snapshot -i). "
    "Step 3a: click_ref('@e3') / fill_ref('@e7', text) — interact by ref (FASTEST). "
    "Step 3b: click_accessible(role, name) / type_accessible(role, name, text) — interact by role+name. "
    "click_ref / click_accessible automatically return an updated snapshot after every click — "
    "no need to call get_snapshot() again manually. Works on ANY website, no per-site rules. "
    "Works across ALL iframes automatically (Gmail compose, LinkedIn DMs, etc.). "

    "=== ALL FUNCTIONS === "
    "get_snapshot(tab_index?)→READ the page — returns @eN refs + role+name for every interactive element "
    "on main page AND all iframes. ALWAYS call first when you need to interact with a new page; "
    "click_ref(ref, tab_index?)→CLICK by @eN ref from last get_snapshot(). Fastest way to click; "
    "fill_ref(ref, text, tab_index?)→FILL/TYPE into field by @eN ref from last get_snapshot(); "
    "click_accessible(role, name, tab_index?, exact?)→CLICK any element by its ARIA role and label. "
    "role examples: button/link/tab/menuitem/checkbox. Works across iframes. "
    "Use get_snapshot() first to find the correct role and name; "
    "type_accessible(role, name, text, tab_index?, exact?)→TYPE into any field by its ARIA role and label. "
    "role is usually textbox or searchbox. Works across iframes. "
    "Use get_snapshot() first to find the correct role and name; "

    "list_tabs()→list all open tabs with index/title/URL; "
    "screenshot(tab_index?)→capture screenshot; "
    "goto(url, tab_index?)→navigate to a URL; "
    "get_text(tab_index?)→extract visible text (up to 5000 chars); "
    "get_html(tab_index?, selector?)→get HTML of page or a CSS-selected element; "
    "click(target, tab_index?)→click by CSS selector or 'text=Foo' (use click_accessible instead when possible); "
    "type_text(target, text, clear_first?, tab_index?)→type into CSS selector target (use type_accessible instead when possible); "
    "scroll(direction, tab_index?)→scroll: up/down/top/bottom; "
    "press_key(key, tab_index?)→press keyboard key: Enter, Tab, Control+Enter, Escape, etc.; "
    "wait_for(selector, tab_index?, timeout_ms?)→wait for CSS selector to appear; "
    "new_tab(url?)→open a new tab; "
    "close_tab(tab_index?)→close a tab by index (use list_tabs() to see indexes first); "
    "evaluate(js_code, tab_index?)→run JavaScript; "

    "=== SINGLE-CALL HELPERS (use these for specific platforms) === "
    "tweet(text)→POST A TWEET in one step. USE THIS for Twitter — never chain manually; "
    "like_tweet(tweet_url)→LIKE a tweet; "
    "reply_tweet(tweet_url, text)→REPLY to a tweet; "
    "follow_user(username)→FOLLOW a Twitter user; "
    "tiktok_like(video_url)→LIKE a TikTok video; "
    "tiktok_comment(video_url, text)→COMMENT on a TikTok video; "
    "tiktok_follow(username)→FOLLOW a TikTok user; "
    "send_gmail(to, subject, body, tab_index?)→COMPOSE AND SEND a new Gmail email in one step. "
    "⚠️ ALWAYS use send_gmail for Gmail. NEVER use click_accessible/type_accessible to fill compose fields manually — Gmail's compose window is in an iframe and requires this dedicated helper. "
    "If a compose window is already open on screen, still call send_gmail() — it handles that case. "

    "=== STEALTH MODE — autonomous bot sessions with anti-detection + cookie persistence === "
    "Use stealth_* functions when you need to log into a site programmatically (NOT via the user's real Chrome), "
    "when a site detects headless browsers, or when you need saved login state across sessions. "
    "Stealth sessions run their OWN Chromium with playwright-stealth patches — completely separate from CDP mode. "
    "Cookies are saved to /app/memory/stealth_sessions/<session_name>/cookies.json and restored on next start. "

    "stealth_start(session_name?, headless?)→LAUNCH stealth browser, load saved cookies. Call once before anything else; "
    "stealth_goto(url, session_name?)→NAVIGATE to URL with human delay; "
    "stealth_snapshot(session_name?)→GET @eN refs for all interactive elements (same pattern as get_snapshot()); "
    "stealth_click_ref(ref, session_name?)→CLICK by @eN ref with human mouse movement, returns fresh snapshot; "
    "stealth_fill_ref(ref, text, session_name?)→TYPE with human keystroke timing by @eN ref; "
    "stealth_get_text(session_name?)→GET visible page text (up to 5000 chars); "
    "stealth_screenshot(session_name?)→SCREENSHOT saved to /app/memory/browser_screenshots/; "
    "stealth_scroll(direction?, session_name?)→SCROLL: up/down/top/bottom; "
    "stealth_press(key, session_name?)→PRESS keyboard key: Enter, Tab, Control+Enter, etc.; "
    "stealth_handle_captcha(session_name?, captcha_api_key?)→DETECT and solve CAPTCHA "
    "(reCAPTCHA v2 auto-solved via 2captcha if key provided; image CAPTCHA returns screenshot path + instructions); "
    "stealth_save(session_name?)→CHECKPOINT cookies to disk mid-session; "
    "stealth_close(session_name?)→SAVE cookies and shut down session; "
    "stealth_list_sessions()→LIST all active stealth sessions and their current URLs. "

    "STEALTH INTERACTION PATTERN: "
    "stealth_start('mysite') → stealth_goto(url, 'mysite') → stealth_snapshot('mysite') "
    "→ stealth_click_ref('@e3', 'mysite') / stealth_fill_ref('@e7', text, 'mysite') "
    "→ stealth_close('mysite'). "
    "Multiple sessions run in parallel — use different session_name values."
)

SKILL_TIMEOUT = 60  # browser operations can take up to 60s

_SCREENSHOT_DIR = Path("/app/memory/browser_screenshots")
_CDP_URL = os.getenv("BROWSER_CDP_URL", "http://host.docker.internal:9223")

logger = logging.getLogger(__name__)

# Ref cache: populated by get_snapshot(), maps '@e1' → (role, name).
# Lets click_ref / fill_ref target elements without re-specifying role+name.
_ref_cache: dict = {}

# Roles considered "interactive" for get_snapshot output
_INTERACTIVE_ROLES = {
    "button", "link", "textbox", "checkbox", "radio", "combobox",
    "listbox", "menuitem", "tab", "spinbutton", "searchbox", "option",
    "menuitemcheckbox", "menuitemradio", "treeitem", "switch",
}



def _collect_a11y_nodes(node: dict, results: list, limit: int = 250) -> None:
    """Recursively collect (role, name) tuples for interactive nodes."""
    if len(results) >= limit:
        return
    role = (node.get("role") or "").lower()
    name = (node.get("name") or "").strip()
    if role in _INTERACTIVE_ROLES and name:
        results.append((role, name))
    for child in node.get("children") or []:
        _collect_a11y_nodes(child, results, limit)


def _snapshot_page(page) -> str:
    """Build a snapshot using Playwright CSS locators (proven reliable on any site).

    For each visible element, a single evaluate() call both extracts the name AND
    stamps data-tc-ref="@eN" directly onto the DOM node.

    click_ref / fill_ref locate elements with frame.locator('[data-tc-ref="@eN"]') —
    a plain CSS selector on the tag we just stamped. No re-searching, no accessibility
    tree, no name matching at click time. Same node the snapshot found = same node clicked.
    """
    global _ref_cache

    _CSS_MAP = [
        ("button:not([disabled])",                          "button"),
        ("[role='button']:not([disabled])",                 "button"),
        ("input[type='submit']:not([disabled])",            "button"),
        ("input[type='button']:not([disabled])",            "button"),
        ("a[href]",                                         "link"),
        ("input[type='text']:not([disabled])",              "textbox"),
        ("input[type='email']:not([disabled])",             "textbox"),
        ("input[type='password']:not([disabled])",          "textbox"),
        ("input[type='url']:not([disabled])",               "textbox"),
        ("textarea:not([disabled])",                        "textbox"),
        ("[role='textbox']",                                "textbox"),
        ("[contenteditable='true']:not([role])",            "textbox"),
        ("input[type='search']:not([disabled])",            "searchbox"),
        ("[role='searchbox']",                              "searchbox"),
        ("select:not([disabled])",                          "combobox"),
        ("[role='combobox']",                               "combobox"),
        ("input[type='checkbox']:not([disabled])",          "checkbox"),
        ("[role='checkbox']",                               "checkbox"),
        ("input[type='radio']:not([disabled])",             "radio"),
        ("[role='radio']",                                  "radio"),
        ("[role='tab']",                                    "tab"),
        ("[role='menuitem']",                               "menuitem"),
        ("[role='switch']",                                 "switch"),
        ("[role='spinbutton']",                             "spinbutton"),
    ]

    # Single evaluate() per element: stamps data-tc-ref AND returns the name.
    _TAG_AND_NAME_JS = (
        "(el, ref) => {"
        "  el.setAttribute('data-tc-ref', ref);"
        "  const a = el.getAttribute('aria-label');"
        "  if (a && a.trim()) return a.trim();"
        "  const t = el.getAttribute('title');"
        "  if (t && t.trim()) return t.trim();"
        "  const p = el.getAttribute('placeholder');"
        "  if (p && p.trim()) return p.trim();"
        "  if (el.labels && el.labels[0]) {"
        "    const l = el.labels[0].innerText.trim();"
        "    if (l) return l;"
        "  }"
        "  const tx = el.innerText ? el.innerText.trim().slice(0,120) : '';"
        "  if (tx) return tx;"
        "  const v = el.value ? String(el.value).trim().slice(0,120) : '';"
        "  return v;"
        "}"
    )

    _ref_cache.clear()
    counter = 1
    sections = []

    frames_to_check = [(page.main_frame, "main", None)]
    for i, frame in enumerate(page.frames):
        if frame is page.main_frame:
            continue
        # Include ALL frames — Gmail compose iframes often have no/blank URL
        frames_to_check.append((frame, f"iframe[{i}]", i))

    for frame, label, frame_idx in frames_to_check:
        # Remove stale tags from the previous snapshot in this frame
        try:
            frame.evaluate(
                "() => document.querySelectorAll('[data-tc-ref]')"
                ".forEach(el => el.removeAttribute('data-tc-ref'))"
            )
        except Exception:
            pass

        items = []
        seen = set()
        count = 0
        for css, role in _CSS_MAP:
            if count >= 200:
                break
            try:
                locs = frame.locator(css).all()
            except Exception:
                continue
            for loc in locs:
                if count >= 200:
                    break
                try:
                    if not loc.is_visible():
                        continue
                    ref = f"@e{counter}"
                    name = (loc.evaluate(_TAG_AND_NAME_JS, ref) or "").strip()
                    if name and (role, name) not in seen:
                        seen.add((role, name))
                        _ref_cache[ref] = {
                            "role": role, "name": name,
                            "frame_is_main": frame_idx is None,
                            "frame_idx": frame_idx,
                        }
                        items.append((ref, role, name))
                        counter += 1
                        count += 1
                    else:
                        # Dup or no name — remove the tag we just stamped
                        try:
                            loc.evaluate("el => el.removeAttribute('data-tc-ref')")
                        except Exception:
                            pass
                except Exception:
                    continue
        if items:
            sections.append((label, items))

    if not sections:
        # Fallback: Playwright's accessibility.snapshot() — catches elements that have ARIA
        # roles but no standard CSS hook (some SPA widgets, shadow DOM fragments, etc.)
        try:
            tree = page.accessibility.snapshot()
            if tree:
                a11y_items = []
                _collect_a11y_nodes(tree, a11y_items)
                if a11y_items:
                    return (
                        f"📋 Page Snapshot (a11y fallback) — {page.url}\n"
                        f"Title: {page.title()}\n"
                        + "─" * 50 + "\n"
                        + "\n".join(f"  {role} \"{name}\"" for role, name in a11y_items) + "\n"
                        + "─" * 50 + "\n"
                        "⚠️ CSS scan returned empty — no @eN refs available.\n"
                        "Use click_accessible(role, name) / type_accessible(role, name, text) to interact."
                    )
        except Exception:
            pass
        return "⚠️ Snapshot empty — page may still be loading. Try scroll() or wait a moment and retry."

    output_parts = []
    for label, items in sections:
        if len(sections) > 1:
            output_parts.append(f"\n[{label}]")
        for ref, role, name in items:
            output_parts.append(f"  {ref}  {role} \"{name}\"")

    body = "\n".join(output_parts)
    return (
        f"📋 Page Snapshot — {page.url}\n"
        f"Title: {page.title()}\n"
        + "─" * 50 + "\n"
        + body + "\n"
        "Use click_ref('@eN') / fill_ref('@eN', text)"
    )


def _filter_snapshot_lines(snapshot_text: str, limit: int = 250) -> list:
    """Extract only interactive element lines from Playwright's aria_snapshot() YAML output.

    Skips structural roles (banner, heading, list, group, /url, /text, etc.)
    and property lines so the agent only sees clickable/typeable elements.
    """
    result = []
    for line in snapshot_text.splitlines():
        stripped = line.strip().lstrip("- ").strip()
        if not stripped:
            continue
        # Property lines start with / (e.g. /url: "", /text: "…") — skip
        if stripped.startswith("/"):
            continue
        # Extract the first word as the role (strip trailing colon or bracket)
        first_word = stripped.split()[0].lower().rstrip(":[")
        if first_word in _INTERACTIVE_ROLES:
            result.append(line)
            if len(result) >= limit:
                break
    return result


# ─────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────

def _find_in_frames(page, role: str, name: str, exact: bool = False):
    """Find an element by ARIA role+name across the main page and all child frames.

    Search order:
      1. Main page via get_by_role() (3 s fast try)
      2. Each child frame via get_by_role() (2 s each)
      3. Fuzzy first-word fallback with get_by_role() on main page + frames
      4. CSS-based fallback — same strategy as _snapshot_page(), works on sites
         where Chrome's accessibility tree is broken/partial (Gmail, LinkedIn, etc.)

    Returns the first visible Playwright Locator, or raises TimeoutError with a helpful message.
    """
    # CSS selectors used by _snapshot_page, grouped by role.
    # Tier 4 iterates these when get_by_role() fails entirely.
    _ROLE_CSS = {
        "button":    ["button:not([disabled])", "[role='button']:not([disabled])",
                      "input[type='submit']:not([disabled])", "input[type='button']:not([disabled])"],
        "link":      ["a[href]"],
        "textbox":   ["input[type='text']:not([disabled])", "input[type='email']:not([disabled])",
                      "input[type='password']:not([disabled])", "input[type='url']:not([disabled])",
                      "textarea:not([disabled])", "[role='textbox']",
                      "[contenteditable='true']:not([role])"],
        "searchbox": ["input[type='search']:not([disabled])", "[role='searchbox']"],
        "combobox":  ["select:not([disabled])", "[role='combobox']"],
        "checkbox":  ["input[type='checkbox']:not([disabled])", "[role='checkbox']"],
        "radio":     ["input[type='radio']:not([disabled])", "[role='radio']"],
        "tab":       ["[role='tab']"],
        "menuitem":  ["[role='menuitem']"],
        "switch":    ["[role='switch']"],
        "spinbutton":["[role='spinbutton']"],
    }

    # Name extraction JS — same priority order as _snapshot_page.
    _NAME_JS = (
        "el => {"
        "  const a = el.getAttribute('aria-label');"
        "  if (a && a.trim()) return a.trim();"
        "  const t = el.getAttribute('title');"
        "  if (t && t.trim()) return t.trim();"
        "  const p = el.getAttribute('placeholder');"
        "  if (p && p.trim()) return p.trim();"
        "  if (el.labels && el.labels[0]) {"
        "    const l = el.labels[0].innerText.trim();"
        "    if (l) return l;"
        "  }"
        "  const tx = el.innerText ? el.innerText.trim().slice(0,120) : '';"
        "  if (tx) return tx;"
        "  const v = el.value ? String(el.value).trim().slice(0,120) : '';"
        "  return v;"
        "}"
    )

    def _css_find_in_frame(frame, role, name, exact):
        """CSS-based element search for a single frame — mirrors _snapshot_page logic."""
        css_selectors = _ROLE_CSS.get(role, [])
        name_lower = name.lower().strip()
        for css in css_selectors:
            try:
                locs = frame.locator(css).all()
            except Exception:
                continue
            for loc in locs:
                try:
                    if not loc.is_visible():
                        continue
                    el_name = (loc.evaluate(_NAME_JS) or "").strip()
                    el_lower = el_name.lower()
                    match = (el_lower == name_lower) if exact else (name_lower in el_lower)
                    if match:
                        return loc
                except Exception:
                    continue
        return None

    # 1. Main page fast try
    try:
        loc = page.get_by_role(role, name=name, exact=exact).first
        loc.wait_for(state="visible", timeout=3000)
        return loc
    except Exception:
        pass

    # 2. Child frames (Gmail compose, LinkedIn messaging, etc. live in iframes)
    for frame in page.frames:
        if frame is page.main_frame:
            continue
        try:
            loc = frame.get_by_role(role, name=name, exact=exact).first
            loc.wait_for(state="visible", timeout=2000)
            return loc
        except Exception:
            continue

    # 3. Fuzzy fallback: try just the first word (e.g. "Subject field" → "Subject")
    first_word = name.split()[0] if name.split() else name
    if first_word != name:
        try:
            loc = page.get_by_role(role, name=first_word, exact=False).first
            loc.wait_for(state="visible", timeout=2000)
            return loc
        except Exception:
            pass
        for frame in page.frames:
            if frame is page.main_frame:
                continue
            try:
                loc = frame.get_by_role(role, name=first_word, exact=False).first
                loc.wait_for(state="visible", timeout=1500)
                return loc
            except Exception:
                continue

    # 4. CSS-based fallback — same strategy as _snapshot_page().
    # Handles sites where Chrome's a11y tree is partial (Gmail, LinkedIn, etc.).
    # The snapshot found the element via CSS; clicking must be able to find it the same way.
    loc = _css_find_in_frame(page.main_frame, role, name, exact)
    if loc is not None:
        return loc
    for frame in page.frames:
        if frame is page.main_frame:
            continue
        loc = _css_find_in_frame(frame, role, name, exact)
        if loc is not None:
            return loc
    # Fuzzy CSS fallback: partial name match on first word
    if first_word != name:
        loc = _css_find_in_frame(page.main_frame, role, first_word, exact=False)
        if loc is not None:
            return loc
        for frame in page.frames:
            if frame is page.main_frame:
                continue
            loc = _css_find_in_frame(frame, role, first_word, exact=False)
            if loc is not None:
                return loc

    raise TimeoutError(
        f"Could not find {role} '{name}' on the page or any frame. "
        f"Call get_snapshot() to see the correct element names."
    )



def _connect():
    """Start playwright and connect to existing Chrome via CDP.
    Returns (pw, browser). Caller MUST call pw.stop() in a finally block.
    Chrome itself keeps running after pw.stop() — only the control channel closes.

    Uses a Host-header spoof to bypass Chrome's DNS-rebinding protection when
    connecting through the netsh portproxy (host.docker.internal:9223 → 127.0.0.1:9222).
    """
    import requests as _requests
    from playwright.sync_api import sync_playwright

    # Step 1: fetch /json/version with Host spoofed to what Chrome expects.
    # Chrome rejects requests where Host != localhost:9222 (DNS-rebinding guard).
    # Spoofing the header makes Chrome accept and return the real WebSocket URL.
    ws_url = None
    try:
        resp = _requests.get(
            f"{_CDP_URL}/json/version",
            headers={"Host": "localhost:9222"},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            ws_url = data.get("webSocketDebuggerUrl", "")
            if ws_url:
                # Rewrite the localhost URL so Playwright routes through our proxy
                ws_url = ws_url.replace("ws://127.0.0.1:9222", "ws://host.docker.internal:9223")
                ws_url = ws_url.replace("ws://localhost:9222", "ws://host.docker.internal:9223")
    except Exception:
        pass  # fall through to direct connect attempt

    pw = sync_playwright().start()
    try:
        endpoint = ws_url if ws_url else _CDP_URL
        browser = pw.chromium.connect_over_cdp(endpoint)
        return pw, browser
    except Exception as e:
        pw.stop()
        raise ConnectionError(
            f"Could not connect to Chrome.\n"
            f"Make sure Chrome is running with: --remote-debugging-port=9222\n"
            f"And the netsh portproxy is active: netsh interface portproxy add v4tov4 "
            f"listenaddress=0.0.0.0 listenport=9223 connectaddress=127.0.0.1 connectport=9222\n"
            f"Error: {e}"
        )


def _get_page(browser, tab_index: int = 0):
    """Get a specific tab by index from the connected browser."""
    # Coerce tab_index — LLM sometimes passes 'False'/'True'/'None'/selector string instead of 0
    if isinstance(tab_index, str):
        stripped = tab_index.strip()
        if stripped.lower() in ("false", "true", "none", ""):
            tab_index = 0
        else:
            try:
                tab_index = int(stripped)
            except ValueError:
                tab_index = 0  # unrecognizable string — default to tab 0
    else:
        tab_index = int(tab_index)
    contexts = browser.contexts
    if not contexts:
        raise ValueError("No browser contexts found. Open Chrome and visit a page first.")
    pages = contexts[0].pages
    if not pages:
        raise ValueError("No open tabs found. Open at least one tab in Chrome.")
    if tab_index >= len(pages):
        raise IndexError(
            f"Tab index {tab_index} is out of range — only {len(pages)} tab(s) open. "
            f"Use list_tabs() to see available tabs."
        )
    page = pages[tab_index]
    try:
        page.bring_to_front()  # ensure this tab is active before any action
    except Exception:
        pass
    return page


# ─────────────────────────────────────────────
# OUTCOME VERIFICATION HELPER
# ─────────────────────────────────────────────

def _gmail_compose_warning(page) -> str:
    """Return a redirect string when the agent navigated into a Gmail compose window.

    If the agent manually clicked Compose instead of calling send_gmail(), this warning
    stops it from continuing to fill fields one by one and redirects to the helper.
    Returns a non-empty string only when on mail.google.com with compose= in the URL.
    """
    url = page.url or ""
    if "mail.google.com" in url and "compose" in url:
        return (
            "\n\n🚨 Gmail compose window is now open."
            "\nDo NOT fill fields manually — call send_gmail(to, subject, body) instead."
            "\nsend_gmail() handles To / Subject / Body / Send in one step reliably."
            "\nExample: send_gmail('user@example.com', 'Subject here', 'Body here')"
        )
    return ""


def _page_state(page) -> str:
    """Return current URL, title, and a short text snippet from the page.

    Appended to every action function's return so the agent can verify the
    outcome on ANY website without trusting a blind ✅. Lets the agent see
    whether the page is in the expected state after a click, form submit, etc.
    """
    try:
        url = page.url or "(blank)"
        title = (page.title() or "(no title)")[:80]
        try:
            raw = page.locator("body").inner_text(timeout=3000)
            lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
            snippet = "\n".join(lines[:12])  # first ~12 non-empty lines
        except Exception:
            snippet = "(could not read page text)"
        return (
            f"\n── Page state after action ──\n"
            f"URL  : {url}\n"
            f"Title: {title}\n"
            f"Text : {snippet}"
        )
    except Exception:
        return ""


# ─────────────────────────────────────────────
# PUBLIC FUNCTIONS
# ─────────────────────────────────────────────

def list_tabs() -> str:
    """List all open browser tabs with their index, title, and URL."""
    pw = None
    try:
        pw, browser = _connect()
        rows = []
        for ctx in browser.contexts:
            for i, page in enumerate(ctx.pages):
                try:
                    title = (page.title() or "(no title)")[:55]
                    url = (page.url or "(blank)")[:80]
                    rows.append(f"  [{i}] {title}\n      {url}")
                except Exception:
                    rows.append(f"  [{i}] (loading...)")
        if not rows:
            return "📭 No open tabs found."
        return "🗂️  Open Tabs\n" + "─" * 50 + "\n" + "\n".join(rows)
    except Exception as e:
        return f"❌ {e}"
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def screenshot(tab_index: int = 0) -> str:
    """Take a screenshot of the specified tab.
    Returns the saved file path so you can use image_viewer.view_image() to inspect it.
    """
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, tab_index)
        _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = _SCREENSHOT_DIR / f"session_{ts}.png"
        page.screenshot(path=str(path), full_page=False)
        return (
            f"✅ Screenshot saved: {path}\n"
            f"Title : {page.title()}\n"
            f"URL   : {page.url}\n"
            f"Tip   : Use image_viewer.view_image('{path}') to inspect it."
        )
    except Exception as e:
        return f"❌ {e}"
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def goto(url: str, tab_index: int = 0) -> str:
    """Navigate to a URL in the specified tab.
    Waits for the DOM to load before returning.
    """
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, tab_index)
        if url and not url.startswith(("http://", "https://", "file://", "about:")):
            url = "https://" + url
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        return f"✅ Navigated to: {page.url}\nTitle: {page.title()}"
    except Exception as e:
        return f"❌ {e}"
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def get_text(tab_index: int = 0) -> str:
    """Get the visible text content of the current page.
    Strips empty lines and collapses whitespace. Returns up to 5000 chars.
    """
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, tab_index)
        raw = page.locator("body").inner_text()
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        result = "\n".join(lines)
        total = len(result)
        preview = result[:5000]
        suffix = f"\n\n... [{total - 5000} more chars — use get_html() for full content]" if total > 5000 else ""
        return (
            f"📄 Page text — {page.url}\n"
            f"({total} chars)\n"
            + "─" * 50 + "\n"
            + preview + suffix
        )
    except Exception as e:
        return f"❌ {e}"
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def get_html(tab_index: int = 0, selector: str = "") -> str:
    """Get the HTML of the current page or a specific element.
    selector: optional CSS selector to narrow to a specific element.
    Returns up to 4000 chars. Use selector to target specific sections.
    """
    # LLM sometimes passes selector as first positional arg instead of tab_index
    if isinstance(tab_index, str) and not str(tab_index).strip().lstrip("-").isdigit():
        selector = tab_index
        tab_index = 0
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, tab_index)
        if selector.strip():
            html = page.locator(selector.strip()).first.inner_html(timeout=20000)
        else:
            html = page.content()
        total = len(html)
        preview = html[:4000]
        suffix = f"\n... [truncated — {total - 4000} more chars]" if total > 4000 else ""
        return (
            f"🔍 HTML — {page.url}\n"
            f"Selector: {selector or 'full page'} ({total} chars)\n"
            + "─" * 50 + "\n"
            + preview + suffix
        )
    except Exception as e:
        return f"❌ {e}"
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def click(target: str = "", tab_index: int = 0, **kwargs) -> str:
    """Click an element in the browser.

    target examples:
      CSS selector  : '[data-testid=tweetButtonInline]'
      Text match    : 'text=Sign in'   ← pass as positional string, NOT as text= keyword arg
      Aria label    : '[aria-label="Tweet"]'
      Role          : 'button:has-text("Post")'
    Take a screenshot() first if you're unsure which selector to use.
    """
    # Recover gracefully if LLM passes text= or selector= as keyword args
    if not target:
        if "text" in kwargs:
            target = f"text={kwargs['text']}"
        elif "selector" in kwargs:
            target = kwargs["selector"]
        else:
            return "❌ click() requires a target selector as the first argument."
    # Guard: reject bare integers — LLM sometimes passes tab index as target by mistake
    if str(target).strip().lstrip("-").isdigit():
        return (
            f"❌ click() target '{target}' is a number, not a CSS selector. "
            f"Provide a CSS selector like '[data-testid=\"SideNav_NewTweet_Button\"]'. "
            f"To target a specific tab, use the tab_index parameter instead."
        )
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, tab_index)
        # Try main page first, then search all child iframes.
        # Gmail compose, LinkedIn DMs, etc. live inside iframes —
        # page.locator() alone never reaches them.
        locator = None
        try:
            loc = page.locator(target).first
            loc.wait_for(state="visible", timeout=3000)
            locator = loc
        except Exception:
            pass
        if locator is None:
            for frame in page.frames:
                if frame is page.main_frame:
                    continue
                try:
                    loc = frame.locator(target).first
                    loc.wait_for(state="visible", timeout=1500)
                    locator = loc
                    break
                except Exception:
                    continue
        if locator is None:
            return f"❌ Could not click '{target}': element not found on page or in any iframe."
        locator.click()
        return f"✅ Clicked: {target}\nURL after click: {page.url}"
    except Exception as e:
        return f"❌ Could not click '{target}': {e}"
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def type_text(target: str = "", text: str = "", clear_first: bool = True, tab_index: int = 0, **kwargs) -> str:
    """Type text into any field — works for both <input> and contenteditable divs (Twitter, LinkedIn).

    target: CSS selector of the field. Must be the first positional argument.
    text: the string to type. Must be the second positional argument.
    clear_first: if True (default), clears existing content before typing.
    Uses a 50ms per-character delay to simulate natural human typing speed.
    """
    # Recover gracefully if LLM passes args as keyword arguments
    if not target:
        target = kwargs.get("selector", kwargs.get("css_selector", ""))
    if not text:
        text = kwargs.get("content", kwargs.get("value", kwargs.get("message", "")))
    if not target:
        return "❌ type_text() requires a target CSS selector as the first argument."
    if not text:
        return "❌ type_text() requires the text to type as the second argument."
    # Coerce clear_first — LLM sometimes passes the string 'False' which is truthy
    if isinstance(clear_first, str):
        clear_first = clear_first.strip().lower() not in ("false", "0", "no")
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, tab_index)
        # Try main page first, then all child iframes.
        # Gmail compose fields, LinkedIn DMs etc. live inside iframes —
        # page.locator() alone never reaches them.
        el = None
        try:
            loc = page.locator(target).first
            loc.wait_for(state="visible", timeout=3000)
            el = loc
        except Exception:
            pass
        if el is None:
            for frame in page.frames:
                if frame is page.main_frame:
                    continue
                try:
                    loc = frame.locator(target).first
                    loc.wait_for(state="visible", timeout=1500)
                    el = loc
                    break
                except Exception:
                    continue
        if el is None:
            return f"❌ Could not type into '{target}': element not found on page or in any iframe."
        el.click()  # focus the element first
        if clear_first:
            # Use JavaScript editing commands — keyboard events (even element-level) still bubble
            # up to document/window and trigger page shortcuts (e.g. Twitter theme toggle on Ctrl+A).
            # execCommand('selectAll') + execCommand('delete') are editing commands, not keyboard events.
            try:
                el.evaluate("""(el) => {
                    el.focus();
                    if (el.isContentEditable) {
                        document.execCommand('selectAll', false, null);
                        document.execCommand('delete', false, null);
                    } else {
                        el.setRangeText('', 0, (el.value || '').length, 'end');
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    }
                }""")
            except Exception:
                # Fallback if JS clear fails
                el.press("Control+a")
                el.press("Delete")
        el.press_sequentially(text, delay=50)  # 50ms delay simulates human typing
        # Tell the agent what to do next so it doesn't stop early
        next_step = ""
        if "tweetTextarea" in target:
            post_btn = (
                '[data-testid="tweetButton"]'
                if "compose/post" in page.url
                else '[data-testid="tweetButtonInline"]'
            )
            next_step = (
                f"\n⚠️ Text entered but NOT posted yet — tweet is still a draft."
                f"\nMANDATORY next step: click('{post_btn}') to publish."
            )
        elif target == '[name="to"]':
            next_step = (
                "\n⚠️ To field filled — email NOT sent yet."
                "\nMANDATORY: continue to next steps:"
                "\n  press_key('Tab')"
                "\n  type_text('[name=\"subjectbox\"]', \"<subject>\")"
                "\n  type_text('div[contenteditable=\"true\"][aria-multiline=\"true\"]', \"<body>\")"
                "\n  press_key('Control+Enter')"
            )
        elif target == '[name="subjectbox"]':
            next_step = (
                "\n⚠️ Subject filled — email NOT sent yet."
                "\nMANDATORY next step: type_text('div[contenteditable=\"true\"][aria-multiline=\"true\"]', \"<body text>\")"
            )
        elif 'contenteditable="true"' in target and 'aria-multiline="true"' in target:
            next_step = (
                "\n⚠️ Body filled — email NOT sent yet."
                "\nMANDATORY next step: press_key('Control+Enter') to send."
            )
        return f"✅ Typed {len(text)} characters into: {target}{next_step}"
    except Exception as e:
        return f"❌ Could not type into '{target}': {e}"
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def scroll(direction: str = "down", tab_index: int = 0) -> str:
    """Scroll the page.
    direction: 'up' | 'down' (600px step) | 'top' | 'bottom'
    """
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, tab_index)
        direction = direction.strip().lower()
        scroll_map = {
            "down":   "window.scrollBy(0, 600)",
            "up":     "window.scrollBy(0, -600)",
            "bottom": "window.scrollTo(0, document.body.scrollHeight)",
            "top":    "window.scrollTo(0, 0)",
        }
        if direction not in scroll_map:
            return f"❌ Unknown direction '{direction}'. Use: up / down / top / bottom"
        page.evaluate(scroll_map[direction])
        return f"✅ Scrolled {direction}"
    except Exception as e:
        return f"❌ {e}"
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def press_key(key: str, tab_index: int = 0) -> str:
    """Press a keyboard key or key combination.

    Examples: 'Enter', 'Escape', 'Tab', 'Control+Enter', 'ArrowDown', 'Backspace'
    Useful for submitting forms, dismissing dialogs, or navigating dropdowns.
    """
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, tab_index)
        page.keyboard.press(key.strip())
        page.wait_for_timeout(400)  # let the action settle before reading state
        return f"✅ Pressed: {key}" + _page_state(page)
    except Exception as e:
        return f"❌ {e}"
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def wait_for(selector: str, tab_index: int = 0, timeout_ms: int = 10000) -> str:
    """Wait for an element to appear on the page.
    Use before click() or type_text() when the page needs time to load dynamic content.

    selector: CSS selector to wait for.
    timeout_ms: max wait time in milliseconds (default 10000 = 10 seconds).
    """
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, tab_index)
        page.locator(selector).first.wait_for(state="visible", timeout=int(timeout_ms))
        return f"✅ Element is visible: {selector}"
    except Exception as e:
        return f"❌ Element '{selector}' did not appear within {timeout_ms}ms: {e}"
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def new_tab(url: str = "") -> str:
    """Open a new tab in your existing Chrome browser.
    Optionally navigate to a URL immediately.
    """
    pw = None
    try:
        pw, browser = _connect()
        if not browser.contexts:
            return "❌ No browser contexts found."
        context = browser.contexts[0]
        page = context.new_page()
        if url.strip():
            page.goto(url.strip(), wait_until="domcontentloaded", timeout=30000)
            return f"✅ New tab opened: {page.url}\nTitle: {page.title()}"
        return "✅ New blank tab opened (tab index will be the last in list_tabs())"
    except Exception as e:
        return f"❌ {e}"
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def close_tab(tab_index: int = 0) -> str:
    """Close a browser tab by its index.
    Use list_tabs() first to confirm which tab you want to close.
    Warning: closing the wrong tab can lose unsaved work — always check with list_tabs() first.
    """
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, tab_index)
        title = (page.title() or "(no title)")[:55]
        url = page.url or "(blank)"
        page.close()
        return f"✅ Closed tab [{tab_index}]: {title}\n{url}"
    except Exception as e:
        return f"❌ {e}"
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def tweet(text: str) -> str:
    """Post a tweet to Twitter/X — complete workflow in one call.

    Navigates to x.com/home, opens the compose box, types the text,
    and clicks the Post button. Returns ✅ with confirmation or ❌ with error.
    Use this instead of chaining goto + click + type_text + click separately.
    """
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, 0)

        # Step 1: navigate to home
        page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=30000)

        # Step 2: open compose box
        compose_btn = page.locator('[data-testid="SideNav_NewTweet_Button"]').first
        compose_btn.wait_for(state="visible", timeout=10000)
        compose_btn.click()

        # Step 3: type the tweet text
        textarea = page.locator('[data-testid="tweetTextarea_0"]').first
        textarea.wait_for(state="visible", timeout=10000)
        textarea.click()
        # Wait for focus to settle before typing — prevents stray keystrokes hitting
        # Twitter's page-level keyboard listeners and triggering shortcuts (theme toggle etc.)
        page.wait_for_timeout(400)
        textarea.press_sequentially(text, delay=50)

        # Step 4: click Post button (on x.com/compose/post the button is tweetButton)
        post_btn = page.locator('[data-testid="tweetButton"]').first
        post_btn.wait_for(state="visible", timeout=10000)
        post_btn.click()

        # Wait for the compose page to close (URL leaves compose/post → tweet was sent)
        try:
            page.wait_for_url(
                lambda url: "compose/post" not in url,
                timeout=10000,
            )
        except Exception:
            # If navigation doesn't happen, give it a moment anyway
            page.wait_for_timeout(2000)

        return (
            f"✅ Tweet posted successfully!\n"
            f"Text ({len(text)} chars): {text[:120]}{'...' if len(text) > 120 else ''}\n"
            f"Current page: {page.url}"
        )
    except Exception as e:
        return f"❌ Could not post tweet: {e}"
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def like_tweet(tweet_url: str) -> str:
    """Like a tweet — complete workflow in one call.

    tweet_url: full URL of the tweet (https://x.com/user/status/ID).
    Navigates to the tweet and clicks the Like button.
    """
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, 0)
        if not tweet_url.startswith(("http://", "https://")):
            tweet_url = "https://" + tweet_url
        page.goto(tweet_url, wait_until="domcontentloaded", timeout=30000)
        like_btn = page.locator('[data-testid="like"]').first
        like_btn.wait_for(state="visible", timeout=10000)
        like_btn.click()
        page.wait_for_timeout(1000)
        return f"✅ Liked tweet: {tweet_url}"
    except Exception as e:
        return f"❌ Could not like tweet: {e}"
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def reply_tweet(tweet_url: str, text: str) -> str:
    """Reply to a tweet — complete workflow in one call.

    tweet_url: full URL of the tweet (https://x.com/user/status/ID).
    text: the reply text to post.
    Navigates to the tweet, clicks Reply, types the text, and posts.
    """
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, 0)
        if not tweet_url.startswith(("http://", "https://")):
            tweet_url = "https://" + tweet_url
        page.goto(tweet_url, wait_until="domcontentloaded", timeout=30000)

        # Click the reply button
        reply_btn = page.locator('[data-testid="reply"]').first
        reply_btn.wait_for(state="visible", timeout=10000)
        reply_btn.click()

        # Type the reply text
        textarea = page.locator('[data-testid="tweetTextarea_0"]').first
        textarea.wait_for(state="visible", timeout=10000)
        textarea.click()
        page.wait_for_timeout(400)  # Let focus settle before typing
        textarea.press_sequentially(text, delay=50)

        # Post the reply (inline reply box uses tweetButtonInline)
        post_btn = page.locator('[data-testid="tweetButtonInline"]').first
        post_btn.wait_for(state="visible", timeout=10000)
        post_btn.click()
        page.wait_for_timeout(2000)

        return (
            f"✅ Reply posted on: {tweet_url}\n"
            f"Reply ({len(text)} chars): {text[:120]}{'...' if len(text) > 120 else ''}"
        )
    except Exception as e:
        return f"❌ Could not reply to tweet: {e}"
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def follow_user(username: str) -> str:
    """Follow a Twitter/X user — complete workflow in one call.

    username: Twitter handle with or without @ (e.g. 'elonmusk' or '@elonmusk').
    Navigates to their profile and clicks Follow. Skips if already following.
    """
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, 0)
        username = username.strip().lstrip("@")
        page.goto(f"https://x.com/{username}", wait_until="domcontentloaded", timeout=30000)

        follow_btn = page.locator('[data-testid="placementTracking"]').first
        follow_btn.wait_for(state="visible", timeout=10000)

        # Check current state — avoid accidentally unfollowing
        btn_text = follow_btn.inner_text().strip().lower()
        if "following" in btn_text or "unfollow" in btn_text:
            return f"ℹ️ Already following @{username} — no action taken."

        follow_btn.click()
        page.wait_for_timeout(1000)
        return f"✅ Now following @{username} (https://x.com/{username})"
    except Exception as e:
        return f"❌ Could not follow @{username}: {e}"
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def tiktok_like(video_url: str) -> str:
    """Like a TikTok video — complete workflow in one call.

    video_url: full URL of the video (https://www.tiktok.com/@user/video/ID).
    Navigates to the video, waits for SPA hydration, and clicks the Like button.
    """
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, 0)
        if not video_url.startswith(("http://", "https://")):
            video_url = "https://" + video_url
        page.goto(video_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)  # TikTok SPA needs time to hydrate
        like_btn = page.locator('[data-e2e="browse-like-icon"]').first
        like_btn.wait_for(state="visible", timeout=10000)
        like_btn.click()
        page.wait_for_timeout(1000)
        return f"✅ Liked TikTok video: {video_url}"
    except Exception as e:
        return (
            f"❌ Could not like TikTok video: {e}\n"
            f"Tip: run get_html(selector=\"main\") to find the current like button selector."
        )
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def tiktok_comment(video_url: str, text: str) -> str:
    """Comment on a TikTok video — complete workflow in one call.

    video_url: full URL of the video (https://www.tiktok.com/@user/video/ID).
    text: the comment text to post.
    Navigates to the video, types the comment in the input, and posts it.
    """
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, 0)
        if not video_url.startswith(("http://", "https://")):
            video_url = "https://" + video_url
        page.goto(video_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)  # TikTok SPA hydration

        comment_input = page.locator('[data-e2e="comment-input"]').first
        comment_input.wait_for(state="visible", timeout=10000)
        comment_input.click()
        page.wait_for_timeout(300)
        comment_input.type(text, delay=50)

        post_btn = page.locator('[data-e2e="comment-post"]').first
        post_btn.wait_for(state="visible", timeout=10000)
        post_btn.click()
        page.wait_for_timeout(1500)

        return (
            f"✅ Comment posted on: {video_url}\n"
            f"Comment ({len(text)} chars): {text[:120]}{'...' if len(text) > 120 else ''}"
        )
    except Exception as e:
        return (
            f"❌ Could not post TikTok comment: {e}\n"
            f"Tip: run get_html(selector=\"main\") to find the current comment selector."
        )
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def tiktok_follow(username: str) -> str:
    """Follow a TikTok user — complete workflow in one call.

    username: TikTok handle with or without @ (e.g. 'username' or '@username').
    Navigates to their profile and clicks Follow. Skips if already following.
    """
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, 0)
        username = username.strip().lstrip("@")
        page.goto(f"https://www.tiktok.com/@{username}", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2000)  # TikTok SPA hydration

        follow_btn = page.locator('[data-e2e="follow-button"]').first
        follow_btn.wait_for(state="visible", timeout=10000)

        btn_text = follow_btn.inner_text().strip().lower()
        if "following" in btn_text or "friends" in btn_text:
            return f"ℹ️ Already following @{username} on TikTok — no action taken."

        follow_btn.click()
        page.wait_for_timeout(1000)
        return f"✅ Now following @{username} on TikTok (https://www.tiktok.com/@{username})"
    except Exception as e:
        return f"❌ Could not follow @{username} on TikTok: {e}"
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def _find_gmail_compose(page):
    """Return the frame (or page) that contains the Gmail compose window.

    Gmail renders its compose dialog inside an <iframe>. This function checks
    the main page first, then searches all child frames for the subjectbox input.
    Returns the frame/page object on success, or None if compose is not found.
    """
    # Try main page first (some Gmail versions don't use an iframe)
    try:
        page.locator('[name="subjectbox"]').first.wait_for(state="visible", timeout=2000)
        return page
    except Exception:
        pass
    # Search all child frames (Gmail compose iframe)
    for frame in page.frames:
        try:
            frame.locator('[name="subjectbox"]').first.wait_for(state="visible", timeout=800)
            return frame
        except Exception:
            continue
    return None


def send_gmail(to: str, subject: str, body: str, tab_index: int = 0) -> str:
    """Compose and send a new email via Gmail browser tab — complete workflow in one call.

    to: recipient email address (e.g. "someone@gmail.com")
    subject: email subject line
    body: email body text
    tab_index: which tab has Gmail open (default 0 — or pass the correct index from list_tabs())

    Navigates to Gmail inbox, clicks Compose, fills To/Subject/Body, then sends with Ctrl+Enter.
    Use this instead of chaining goto + click + type_text + press_key for Gmail compose.
    """
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, tab_index)

        # Step 1: ensure we're on Gmail
        current_url = page.url or ""
        if "mail.google.com" not in current_url:
            page.goto("https://mail.google.com/mail/u/0/#inbox", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1000)

        # Step 2: click Compose to open a new compose window.
        # [gh="cm"] is language-independent and always present in Gmail.
        compose_btn = page.locator('[gh="cm"]').first
        compose_btn.wait_for(state="visible", timeout=10000)
        compose_btn.click()
        page.wait_for_timeout(800)  # let compose dialog open and iframe load

        # Step 3: find the compose context — Gmail renders compose inside an <iframe>.
        # _find_gmail_compose() searches main page + all child frames for [name="subjectbox"].
        ctx = _find_gmail_compose(page)
        if ctx is None:
            return (
                "❌ Could not find Gmail compose window after clicking Compose.\n"
                "The compose dialog did not open or the subjectbox is not reachable.\n"
                "Tip: call browser_session.screenshot() to see the current state."
            )

        # Step 4: fill To field via get_by_role — works across iframes, language-independent.
        # The To field is always the first textbox in the compose dialog.
        # get_by_role() on the detected frame (ctx) handles iframe scope automatically.
        to_typed = False
        try:
            to_el = ctx.get_by_role("textbox").first
            to_el.wait_for(state="visible", timeout=5000)
            to_el.click()
            to_el.fill(to)
            page.keyboard.press("Tab")
            to_typed = True
        except Exception:
            pass
        if not to_typed:
            # Last resort: compose opens with focus on To — type via keyboard directly
            page.keyboard.type(to, delay=30)
            page.keyboard.press("Tab")
        page.wait_for_timeout(200)

        # Step 5: fill Subject — [name="subjectbox"] is an HTML attribute, language-independent
        subject_el = ctx.locator('[name="subjectbox"]').first
        subject_el.wait_for(state="visible", timeout=5000)
        subject_el.click()
        subject_el.fill(subject)
        page.wait_for_timeout(200)

        # Step 6: fill Body — Gmail body is a contenteditable div with aria-multiline=true.
        # Do NOT use nth(1) — that grabs the Subject input, not the body.
        body_el = ctx.locator('div[contenteditable="true"][aria-multiline="true"]').first
        try:
            body_el.wait_for(state="visible", timeout=5000)
        except Exception:
            # fallback: last textbox role in compose (To=0, Subject=1, Body=2+)
            body_el = ctx.get_by_role("textbox").last
            body_el.wait_for(state="visible", timeout=5000)
        body_el.click()
        page.wait_for_timeout(200)
        try:
            body_el.fill(body)
        except Exception:
            body_el.type(body, delay=40)
        page.wait_for_timeout(200)

        # Step 7: send via Ctrl+Enter
        page.keyboard.press("Control+Enter")

        # Wait for compose window to close (URL leaves compose=new when sent)
        try:
            page.wait_for_url(
                lambda url: "compose" not in url,
                timeout=8000,
            )
        except Exception:
            page.wait_for_timeout(2000)

        return (
            f"✅ Email sent!\n"
            f"To: {to}\n"
            f"Subject: {subject}\n"
            f"Body preview: {body[:120]}{'...' if len(body) > 120 else ''}"
        )
    except Exception as e:
        return (
            f"❌ Could not send Gmail: {e}\n"
            f"Tip: call browser_session.screenshot() to see what's on screen, "
            f"or browser_session.get_html(selector=\"[role='main']\") to inspect the DOM."
        )
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def get_snapshot(tab_index: int = 0) -> str:
    """Get a semantic accessibility snapshot of the current page.

    Returns a filtered list of interactive elements (buttons, links, textboxes, etc.)
    with @eN refs — same idea as `agent-browser snapshot -i`.
    Covers the main page AND every iframe automatically (Gmail compose, LinkedIn DMs, etc.).

    Workflow (preferred — no CSS selectors needed):
      1. get_snapshot()                             → see '@e3 button "Compose"', '@e7 textbox "To"'
      2. click_ref("@e3")                           → click it — snapshot returned automatically
      3. fill_ref("@e7", "user@example.com")        → fill by ref
      — or use role+name directly —
      2. click_accessible("button", "Compose")
      3. type_accessible("textbox", "To", "user@example.com")
    """
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, tab_index)
        snapshot = _snapshot_page(page)
        # Twitter: remind agent to use single-call helpers instead of manual snapshot clicks
        url = page.url or ""
        if "x.com" in url or "twitter.com" in url:
            snapshot = (
                "⚠️ On Twitter/X — use single-call helpers instead of manual snapshot interactions:\n"
                "  tweet(text) → post  |  like_tweet(url) → like  |  reply_tweet(url, text) → reply  |  follow_user(username) → follow\n"
                "Only use snapshot/click_ref for actions NOT covered by those helpers.\n\n"
            ) + snapshot
        return snapshot
    except Exception as e:
        return f"❌ {e}"
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def click_accessible(role: str, name: str = "", tab_index: int = 0, exact: bool = False) -> str:
    """Click a page element by its ARIA role and visible label — no CSS selector needed.

    role: ARIA role such as 'button', 'link', 'tab', 'menuitem', 'checkbox', etc.
    name: the visible label or aria-label of the element (partial match by default).
    exact: set True to require an exact name match (default False = partial match).

    Works across iframes automatically.
    Get role and name values from get_snapshot() first if unsure.

    Examples:
      click_accessible("button", "Compose")
      click_accessible("link", "Inbox")
      click_accessible("button", "Send")
    """
    # Guard: auto-correct swapped or missing arguments.
    # Agents sometimes call click_accessible("Compose") or click_accessible("Compose","button").
    if role not in _INTERACTIVE_ROLES:
        if not name:
            # click_accessible("Compose") — name passed as role, default role to button
            name = role
            role = "button"
        elif name in _INTERACTIVE_ROLES:
            # click_accessible("Compose", "button") — args swapped
            role, name = name, role
        else:
            # Both args provided but role is invalid — try button as a safe default
            name = name or role
            role = "button"
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, tab_index)
        locator = _find_in_frames(page, role, name, exact=exact)
        # Read back the actual element label before clicking for verification
        try:
            actual_label = locator.evaluate(
                "el => el.getAttribute('aria-label') || el.getAttribute('name') "
                "|| el.getAttribute('placeholder') || el.textContent?.trim() || ''"
            ) or name
        except Exception:
            actual_label = name
        locator.click()
        # Wait for any dialog/iframe triggered by the click to render
        page.wait_for_timeout(700)
        header = f"✅ Clicked {role} \"{actual_label}\"\nURL after click: {page.url}"
        if actual_label.lower().strip() != name.lower().strip():
            header += f"\n⚠️  Requested \"{name}\", matched \"{actual_label}\"."
        # Gmail compose redirect — stop agent from filling fields manually
        gmail_warn = _gmail_compose_warning(page)
        if gmail_warn:
            return header + gmail_warn
        # Auto-snapshot after every click — universal, works on any site
        snapshot = _snapshot_page(page)
        return header + "\n\n" + snapshot
    except Exception as e:
        return (
            f"❌ Could not click {role} \"{name}\": {e}\n"
            f"Tip: call get_snapshot() to see the correct element names on this page."
        )
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def type_accessible(role: str, name: str = "", text: str = "", tab_index: int = 0, exact: bool = False) -> str:
    """Type text into any input field by its ARIA role and label — no CSS selector needed.

    role: usually 'textbox', 'searchbox', or 'spinbutton'.
    name: the visible label or placeholder of the field (partial match by default).
    text: the text to type into the field. Clears existing content first.
    exact: set True for exact name match (default False = partial match).

    Works across iframes automatically — no iframe handling needed.
    Get role and name values from get_snapshot() first if unsure.

    Examples:
      type_accessible("textbox", "To", "user@example.com")
      type_accessible("textbox", "Subject", "Hello there")
      type_accessible("searchbox", "Search", "my query")
    """
    # Guard: auto-correct swapped or missing arguments.
    if role not in _INTERACTIVE_ROLES:
        if name in _INTERACTIVE_ROLES:
            # type_accessible("Subject", "textbox", "text") — args swapped
            role, name = name, role
        elif not name:
            # type_accessible("Subject", "", "text") — name passed as role
            name = role
            role = "textbox"
        else:
            name = name or role
            role = "textbox"
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, tab_index)
        locator = _find_in_frames(page, role, name, exact=exact)
        # Read back the actual element label before typing for verification
        try:
            actual_label = locator.evaluate(
                "el => el.getAttribute('aria-label') || el.getAttribute('name') "
                "|| el.getAttribute('placeholder') || ''"
            ) or name
        except Exception:
            actual_label = name
        locator.click()
        # fill() clears and sets value — works on <input>, <textarea>, and contenteditable
        try:
            locator.fill(text)
        except Exception:
            # fallback for non-standard inputs (contenteditable without value)
            locator.press("Control+a")
            locator.type(text, delay=40)
        result = f"✅ Typed into {role} \"{actual_label}\": {text[:100]}{'...' if len(text) > 100 else ''}"
        if actual_label.lower().strip() != name.lower().strip():
            result += f"\n⚠️  You requested \"{name}\" but matched \"{actual_label}\" — verify this is the correct field. Call get_snapshot() to check field names."
        # Gmail: one-time warning to use the dedicated helper instead of manual steps
        if "mail.google.com" in (page.url or ""):
            result += (
                "\n⚠️ On Gmail — if you are composing an email, call send_gmail(to, subject, body) "
                "instead of filling fields manually. It handles the full flow and sends reliably."
            )
        # Universal outcome check: let the agent see actual page state after every type action
        result += _page_state(page)
        return result
    except Exception as e:
        return (
            f"❌ Could not type into {role} \"{name}\": {e}\n"
            f"Tip: call get_snapshot() to see the correct field names on this page."
        )
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def click_ref(ref: str, tab_index: int = 0) -> str:
    """Click an element by its @eN reference from the last get_snapshot() call.

    Uses the data-tc-ref tag set on the DOM element during snapshot.
    No re-searching by name or accessibility tree — finds the exact same node.
    Works on any site: Gmail, LinkedIn, Twitter, anything.

    Example:
      get_snapshot()          → see '@e3 button "Compose"'
      click_ref('@e3')        → clicks Compose
    """
    if ref not in _ref_cache:
        return (
            f"❌ Unknown ref '{ref}'. Call get_snapshot() first to refresh element refs.\n"
            f"Known refs: {', '.join(sorted(_ref_cache.keys())[:10]) or 'none yet'}"
        )
    ref_data = _ref_cache[ref]
    role = ref_data["role"]
    name = ref_data["name"]
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, tab_index)
        # Locate by the data-tc-ref tag stamped onto the DOM node during snapshot
        if ref_data.get("frame_is_main", True):
            frame = page.main_frame
        else:
            fidx = ref_data.get("frame_idx")
            frame = page.frames[fidx] if fidx is not None and fidx < len(page.frames) else page.main_frame
        locator = frame.locator(f'[data-tc-ref="{ref}"]')
        try:
            locator.wait_for(state="visible", timeout=3000)
            locator.click()
        except Exception:
            # data-tc-ref tag expired (DOM re-rendered) — fall back to role+name search.
            # Use exact=False: cached name may have changed (e.g. Twitter "20 Likes. Like"
            # → "21 Likes. Like" after DOM refresh). Partial match is more resilient.
            try:
                locator = _find_in_frames(page, role, name, exact=False)
            except Exception:
                # Last-word fallback: "20 Likes. Like" → "Like", handles Twitter-style labels
                words = [w.strip(".,!?;:") for w in name.split() if w.strip(".,!?;:")]
                last_word = words[-1] if words else name
                locator = _find_in_frames(page, role, last_word, exact=False)
            locator.click()
        page.wait_for_timeout(700)
        header = f"✅ Clicked {ref} ({role} \"{name}\")\nURL after click: {page.url}"
        # Gmail compose redirect — stop agent from filling fields manually
        gmail_warn = _gmail_compose_warning(page)
        if gmail_warn:
            return header + gmail_warn
        snapshot = _snapshot_page(page)
        return header + "\n\n" + snapshot
    except Exception as e:
        return (
            f"❌ Could not click ref {ref} ({role} \"{name}\"): {e}\n"
            f"Tip: call get_snapshot() to refresh refs — the DOM may have changed since last snapshot."
        )
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def fill_ref(ref: str, text: str, tab_index: int = 0) -> str:
    """Fill a field by its @eN reference from the last get_snapshot() call.

    Uses the data-tc-ref tag set on the DOM element during snapshot.
    No re-searching by name or accessibility tree — finds the exact same node.
    Works on any site: Gmail compose, LinkedIn DMs, anything.

    Example:
      get_snapshot()                      → see '@e7 textbox "To"'
      fill_ref('@e7', 'user@example.com') → fills To field
    """
    if ref not in _ref_cache:
        return (
            f"❌ Unknown ref '{ref}'. Call get_snapshot() first to refresh element refs.\n"
            f"Known refs: {', '.join(sorted(_ref_cache.keys())[:10]) or 'none yet'}"
        )
    ref_data = _ref_cache[ref]
    role = ref_data["role"]
    name = ref_data["name"]
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, tab_index)
        # Locate by the data-tc-ref tag stamped onto the DOM node during snapshot
        if ref_data.get("frame_is_main", True):
            frame = page.main_frame
        else:
            fidx = ref_data.get("frame_idx")
            frame = page.frames[fidx] if fidx is not None and fidx < len(page.frames) else page.main_frame
        locator = frame.locator(f'[data-tc-ref="{ref}"]')
        try:
            locator.wait_for(state="visible", timeout=3000)
        except Exception:
            # data-tc-ref tag expired (DOM re-rendered) — partial match, same as click_ref
            try:
                locator = _find_in_frames(page, role, name, exact=False)
            except Exception:
                words = [w.strip(".,!?;:") for w in name.split() if w.strip(".,!?;:")]
                last_word = words[-1] if words else name
                locator = _find_in_frames(page, role, last_word, exact=False)
        locator.click()
        try:
            locator.fill(text)
        except Exception:
            # contenteditable fallback (Gmail body, rich-text editors)
            locator.press("Control+a")
            locator.type(text, delay=40)
        return f"✅ Filled {ref} ({role} \"{name}\"): {text[:100]}{'...' if len(text) > 100 else ''}" + _page_state(page)
    except Exception as e:
        return (
            f"❌ Could not fill ref {ref} ({role} \"{name}\"): {e}\n"
            f"Tip: call get_snapshot() to refresh refs — the DOM may have changed since last snapshot."
        )
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def evaluate(js_code: str, tab_index: int = 0) -> str:
    """Execute JavaScript in the current page and return the result.

    Useful for reading DOM values, triggering events, or extracting structured data.
    Example: evaluate('document.title')
    Example: evaluate('document.querySelectorAll("article").length')
    """
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, tab_index)
        result = page.evaluate(js_code)
        return f"✅ JS result:\n{result}"
    except Exception as e:
        return f"❌ JavaScript error: {e}"
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════
# STEALTH BROWSER — autonomous Playwright sessions with anti-detection,
# cookie persistence, human-like behaviour, and CAPTCHA handling.
#
# These are SEPARATE from the CDP functions above. Use them when:
#   • You need to log into a site programmatically (not via the user's Chrome)
#   • The target site detects headless/automated browsers
#   • You need to save and restore login state across agent sessions
#   • You need CAPTCHA handling
#
# Typical flow:
#   stealth_start('instagram')        → launch browser, load saved cookies
#   stealth_goto(url, 'instagram')    → navigate
#   stealth_snapshot('instagram')     → see @eN refs
#   stealth_click_ref('@e3', 'instagram')  → click
#   stealth_fill_ref('@e7', 'text', 'instagram')  → type
#   stealth_close('instagram')        → save cookies, shut down
# ═══════════════════════════════════════════════════════════════════════════

_stealth_sessions: dict = {}       # session_name → {pw, browser, context, page}
_stealth_ref_caches: dict = {}     # session_name → ref_cache dict (per-session)
_STEALTH_SESSIONS_DIR = Path("/app/memory/stealth_sessions")


def _stealth_human_delay(min_s: float = 0.5, max_s: float = 1.8):
    time.sleep(random.uniform(min_s, max_s))


def _stealth_get(session_name: str) -> dict:
    """Return the active session dict or raise a clear error."""
    if session_name not in _stealth_sessions:
        raise RuntimeError(
            f"No active stealth session '{session_name}'. "
            f"Call stealth_start('{session_name}') first."
        )
    return _stealth_sessions[session_name]


def stealth_start(session_name: str = "default", headless: bool = True) -> str:
    """Launch a stealth Playwright browser with saved-cookie persistence.

    Starts its own Chromium — completely separate from the user's real Chrome.
    Cookies are loaded from /app/memory/stealth_sessions/<session_name>/cookies.json
    so the next run starts already logged in (no re-login needed).

    Args:
        session_name: Name for this session (e.g. 'instagram', 'linkedin_bot').
                      Use different names to run multiple independent sessions.
        headless:     True = no visible window (default). False = show browser for debugging.

    Returns:
        str: Confirmation with stealth status and cookie load status.
    """
    if session_name in _stealth_sessions:
        return f"✅ Stealth session '{session_name}' is already active."

    try:
        from playwright.sync_api import sync_playwright
        try:
            from playwright_stealth import stealth_sync
            _has_stealth = True
        except ImportError:
            _has_stealth = False

        session_dir = _STEALTH_SESSIONS_DIR / session_name
        session_dir.mkdir(parents=True, exist_ok=True)
        cookie_file = session_dir / "cookies.json"

        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=headless,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )

        # Load saved cookies if they exist
        cookies_loaded = False
        if cookie_file.exists():
            import json as _json
            cookies = _json.loads(cookie_file.read_text())
            if cookies:
                context.add_cookies(cookies)
                cookies_loaded = True

        page = context.new_page()
        if _has_stealth:
            stealth_sync(page)

        _stealth_sessions[session_name] = {
            "pw": pw, "browser": browser, "context": context, "page": page,
        }
        _stealth_ref_caches[session_name] = {}

        stealth_note = "" if _has_stealth else " ⚠️ playwright-stealth not installed — basic mode"
        cookies_note = f"cookies loaded from {cookie_file}" if cookies_loaded else "no saved cookies (fresh session)"
        return f"✅ Stealth session '{session_name}' started ({cookies_note}){stealth_note}"

    except Exception as e:
        return f"❌ stealth_start failed: {e}"


def stealth_list_sessions() -> str:
    """List all currently active stealth browser sessions.

    Returns:
        str: Session names and their current URLs.
    """
    if not _stealth_sessions:
        return "No active stealth sessions. Call stealth_start(session_name) to begin."
    lines = ["Active stealth sessions:"]
    for name, sess in _stealth_sessions.items():
        try:
            url = sess["page"].url
        except Exception:
            url = "unknown"
        lines.append(f"  • {name} — {url}")
    return "\n".join(lines)


def stealth_goto(url: str, session_name: str = "default") -> str:
    """Navigate the stealth browser to a URL.

    Args:
        url:          Full URL including https://.
        session_name: Target session (default: 'default').

    Returns:
        str: Page title and final URL after navigation.
    """
    try:
        sess = _stealth_get(session_name)
        page = sess["page"]
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        _stealth_human_delay(0.8, 2.0)
        return f"✅ Navigated to {page.url}\nTitle: {page.title()}"
    except Exception as e:
        return f"❌ stealth_goto failed: {e}"


def stealth_snapshot(session_name: str = "default") -> str:
    """Get an @eN ref snapshot of all interactive elements in the stealth browser.

    Works exactly like get_snapshot() for CDP sessions — call this first when
    arriving on a new page, then use stealth_click_ref / stealth_fill_ref with
    the returned refs. No CSS selectors needed.

    Args:
        session_name: Target session (default: 'default').

    Returns:
        str: Snapshot listing every interactive element with its @eN ref, role, and label.
    """
    global _ref_cache
    try:
        sess = _stealth_get(session_name)
        page = sess["page"]
        result = _snapshot_page(page)
        # Copy global ref cache into the per-session cache so CDP and stealth
        # sessions do not overwrite each other's refs.
        _stealth_ref_caches[session_name] = dict(_ref_cache)
        return result
    except Exception as e:
        return f"❌ stealth_snapshot failed: {e}"


def stealth_click_ref(ref: str, session_name: str = "default") -> str:
    """Click an element by @eN ref in the stealth browser with human-like mouse movement.

    Use stealth_snapshot() first to get refs. Returns a fresh snapshot after
    clicking so you can see what changed without calling stealth_snapshot() again.

    Args:
        ref:          The @eN ref from stealth_snapshot() (e.g. '@e3').
        session_name: Target session (default: 'default').

    Returns:
        str: Confirmation + updated snapshot.
    """
    global _ref_cache
    try:
        sess = _stealth_get(session_name)
        page = sess["page"]
        cache = _stealth_ref_caches.get(session_name, {})

        if ref not in cache:
            return (
                f"❌ Ref {ref} not found in session '{session_name}'. "
                f"Call stealth_snapshot('{session_name}') to refresh refs."
            )

        info = cache[ref]
        locator = page.locator(f'[data-tc-ref="{ref}"]').first
        locator.scroll_into_view_if_needed()

        box = locator.bounding_box()
        if box:
            page.mouse.move(
                box["x"] + box["width"] * random.uniform(0.3, 0.7),
                box["y"] + box["height"] * random.uniform(0.3, 0.7),
            )
            _stealth_human_delay(0.1, 0.4)

        locator.click()
        _stealth_human_delay(0.4, 1.2)

        # Return fresh snapshot
        result = _snapshot_page(page)
        _stealth_ref_caches[session_name] = dict(_ref_cache)
        role = info.get("role", "")
        name = info.get("name", "")
        return f"✅ Clicked {ref} ({role} \"{name}\")\n\n{result}"
    except Exception as e:
        return f"❌ stealth_click_ref failed: {e}"


def stealth_fill_ref(ref: str, text: str, session_name: str = "default") -> str:
    """Type text into a field by @eN ref in the stealth browser with human-like keystroke timing.

    Use stealth_snapshot() first to get refs.

    Args:
        ref:          The @eN ref from stealth_snapshot() (e.g. '@e5').
        text:         The text to type.
        session_name: Target session (default: 'default').

    Returns:
        str: Confirmation showing the first 100 chars typed.
    """
    try:
        sess = _stealth_get(session_name)
        page = sess["page"]
        cache = _stealth_ref_caches.get(session_name, {})

        if ref not in cache:
            return (
                f"❌ Ref {ref} not found in session '{session_name}'. "
                f"Call stealth_snapshot('{session_name}') to refresh refs."
            )

        info = cache[ref]
        locator = page.locator(f'[data-tc-ref="{ref}"]').first
        locator.scroll_into_view_if_needed()
        locator.click()
        _stealth_human_delay(0.2, 0.5)

        # Clear then type character-by-character with human timing
        locator.fill("")
        for char in text:
            page.keyboard.type(char, delay=random.randint(60, 150))

        _stealth_human_delay(0.3, 0.8)
        role = info.get("role", "")
        name = info.get("name", "")
        preview = text[:100] + ("..." if len(text) > 100 else "")
        return f"✅ Typed into {ref} ({role} \"{name}\"): {preview}"
    except Exception as e:
        return f"❌ stealth_fill_ref failed: {e}"


def stealth_get_text(session_name: str = "default") -> str:
    """Get visible text from the stealth browser page (up to 5000 chars).

    Args:
        session_name: Target session (default: 'default').

    Returns:
        str: Visible page text.
    """
    try:
        sess = _stealth_get(session_name)
        page = sess["page"]
        text = page.inner_text("body")
        text = " ".join(text.split())[:5000]
        return f"✅ Page text ({page.url}):\n{text}"
    except Exception as e:
        return f"❌ stealth_get_text failed: {e}"


def stealth_screenshot(session_name: str = "default") -> str:
    """Take a screenshot of the stealth browser page.

    Saved to /app/memory/browser_screenshots/stealth_<session>_<timestamp>.png

    Args:
        session_name: Target session (default: 'default').

    Returns:
        str: Path to the saved screenshot.
    """
    try:
        sess = _stealth_get(session_name)
        page = sess["page"]
        _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = _SCREENSHOT_DIR / f"stealth_{session_name}_{ts}.png"
        page.screenshot(path=str(path))
        return f"✅ Screenshot saved: {path}"
    except Exception as e:
        return f"❌ stealth_screenshot failed: {e}"


def stealth_scroll(direction: str = "down", session_name: str = "default") -> str:
    """Scroll the stealth browser page.

    Args:
        direction:    'up', 'down', 'top', or 'bottom'.
        session_name: Target session (default: 'default').

    Returns:
        str: Confirmation.
    """
    try:
        sess = _stealth_get(session_name)
        page = sess["page"]
        d = direction.lower()
        if d == "down":
            page.mouse.wheel(0, 500)
        elif d == "up":
            page.mouse.wheel(0, -500)
        elif d == "bottom":
            page.keyboard.press("End")
        elif d == "top":
            page.keyboard.press("Home")
        _stealth_human_delay(0.3, 0.7)
        return f"✅ Scrolled {direction}"
    except Exception as e:
        return f"❌ stealth_scroll failed: {e}"


def stealth_press(key: str, session_name: str = "default") -> str:
    """Press a keyboard key in the stealth browser.

    Examples: 'Enter', 'Tab', 'Escape', 'Control+Enter', 'Control+a'.

    Args:
        key:          Key or key combination to press.
        session_name: Target session (default: 'default').

    Returns:
        str: Confirmation.
    """
    try:
        sess = _stealth_get(session_name)
        page = sess["page"]
        page.keyboard.press(key)
        _stealth_human_delay(0.2, 0.6)
        return f"✅ Pressed {key}"
    except Exception as e:
        return f"❌ stealth_press failed: {e}"


def stealth_handle_captcha(session_name: str = "default", captcha_api_key: str = "") -> str:
    """Detect and attempt to solve a CAPTCHA in the stealth browser.

    Handles two types:
    - reCAPTCHA v2: auto-solved via 2captcha if captcha_api_key is provided.
    - Image CAPTCHA (text/characters): saves a screenshot and tells you the
      input ref to use — read the image and call stealth_fill_ref() with the answer.

    Args:
        session_name:     Target session (default: 'default').
        captcha_api_key:  Your 2captcha.com API key (optional, only for reCAPTCHA v2).

    Returns:
        str: Result or step-by-step instructions to complete the solve.
    """
    try:
        sess = _stealth_get(session_name)
        page = sess["page"]

        # reCAPTCHA v2
        if page.locator("iframe[src*='recaptcha']").count() > 0:
            if captcha_api_key:
                site_key_el = page.locator("[data-sitekey]").first
                site_key = site_key_el.get_attribute("data-sitekey") if site_key_el else None
                if site_key:
                    token = _stealth_solve_recaptcha(captcha_api_key, site_key, page.url)
                    if token:
                        page.evaluate(
                            f"document.getElementById('g-recaptcha-response').value='{token}'"
                        )
                        return "✅ reCAPTCHA v2 solved via 2captcha and token injected."
                    return "❌ 2captcha solve timed out. Try again or solve manually."
            return (
                "⚠️ reCAPTCHA v2 detected. "
                "Provide captcha_api_key='your_2captcha_key' to auto-solve, "
                "or ask the user to solve it manually."
            )

        # Image CAPTCHA
        if page.locator("img[src*='captcha'], img[alt*='captcha'], img[id*='captcha']").count() > 0:
            _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = _SCREENSHOT_DIR / f"captcha_{session_name}_{ts}.png"
            page.screenshot(path=str(path))
            return (
                f"⚠️ Image CAPTCHA detected. Screenshot saved: {path}\n"
                f"1. Read the CAPTCHA characters from the screenshot.\n"
                f"2. Call stealth_snapshot('{session_name}') to find the input field ref.\n"
                f"3. Call stealth_fill_ref(ref, 'captcha_answer', '{session_name}') to submit."
            )

        return "✅ No CAPTCHA detected on this page."
    except Exception as e:
        return f"❌ stealth_handle_captcha failed: {e}"


def _stealth_solve_recaptcha(api_key: str, site_key: str, page_url: str) -> str:
    """Submit reCAPTCHA v2 to 2captcha.com and poll for the token."""
    try:
        import requests as _req
        r = _req.post("http://2captcha.com/in.php", data={
            "key": api_key, "method": "userrecaptcha",
            "googlekey": site_key, "pageurl": page_url, "json": 1,
        }, timeout=15).json()
        captcha_id = r.get("request")
        if not captcha_id or r.get("status") != 1:
            return None
        time.sleep(15)
        for _ in range(12):
            res = _req.get(
                f"http://2captcha.com/res.php?key={api_key}"
                f"&action=get&id={captcha_id}&json=1", timeout=10
            ).json()
            if res.get("status") == 1:
                return res["request"]
            time.sleep(5)
    except Exception:
        pass
    return None


def stealth_save(session_name: str = "default") -> str:
    """Save the stealth session cookies to disk (checkpoint mid-session).

    Cookies are also saved automatically on stealth_close(). Call this
    during a long session to checkpoint login state in case of a crash.

    Args:
        session_name: Target session to save.

    Returns:
        str: Confirmation with cookie count and file path.
    """
    try:
        sess = _stealth_get(session_name)
        cookies = sess["context"].cookies()
        session_dir = _STEALTH_SESSIONS_DIR / session_name
        session_dir.mkdir(parents=True, exist_ok=True)
        cookie_file = session_dir / "cookies.json"
        import json as _json
        cookie_file.write_text(_json.dumps(cookies, indent=2))
        return f"✅ Saved {len(cookies)} cookies for session '{session_name}' → {cookie_file}"
    except Exception as e:
        return f"❌ stealth_save failed: {e}"


def stealth_close(session_name: str = "default") -> str:
    """Save cookies and shut down the stealth browser session.

    Always call this when finished — it persists the login state so the next
    stealth_start() with the same session_name begins already logged in.

    Args:
        session_name: Target session to close.

    Returns:
        str: Confirmation.
    """
    if session_name not in _stealth_sessions:
        return f"No active stealth session '{session_name}' to close."
    try:
        sess = _stealth_sessions[session_name]
        # Save cookies before closing
        try:
            cookies = sess["context"].cookies()
            session_dir = _STEALTH_SESSIONS_DIR / session_name
            session_dir.mkdir(parents=True, exist_ok=True)
            import json as _json
            (session_dir / "cookies.json").write_text(_json.dumps(cookies, indent=2))
        except Exception:
            pass
        try:
            sess["browser"].close()
        except Exception:
            pass
        try:
            sess["pw"].stop()
        except Exception:
            pass
        del _stealth_sessions[session_name]
        _stealth_ref_caches.pop(session_name, None)
        return f"✅ Stealth session '{session_name}' closed and cookies saved."
    except Exception as e:
        return f"❌ stealth_close failed: {e}"
