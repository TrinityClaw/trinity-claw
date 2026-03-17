import os
import logging
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
    "Step 2: get_snapshot() to see all interactive elements by role and name. "
    "Step 3: click_accessible(role, name) to click, or type_accessible(role, name, text) to type. "
    "These three functions work on ANY website without needing to know CSS selectors or DOM structure. "
    "Works across iframes automatically. "

    "=== ALL FUNCTIONS === "
    "get_snapshot(tab_index?)→READ the page — returns all buttons, links, textboxes etc. by role+name. "
    "ALWAYS call this first when you need to interact with a page you haven't seen before; "
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
    "USE THIS for Gmail — never chain manually."
)

SKILL_TIMEOUT = 60  # browser operations can take up to 60s

_SCREENSHOT_DIR = Path("/app/memory/browser_screenshots")
_CDP_URL = os.getenv("BROWSER_CDP_URL", "http://host.docker.internal:9223")

logger = logging.getLogger(__name__)

# Roles considered "interactive" for get_snapshot output
_INTERACTIVE_ROLES = {
    "button", "link", "textbox", "checkbox", "radio", "combobox",
    "listbox", "menuitem", "tab", "spinbutton", "searchbox", "option",
    "menuitemcheckbox", "menuitemradio", "treeitem", "switch",
}


def _format_a11y_tree(node: dict, depth: int = 0, lines: list = None, count: list = None) -> list:
    """Recursively format an accessibility tree node into readable lines.
    Only includes interactive roles with non-empty names. Max 120 items.
    """
    if lines is None:
        lines = []
    if count is None:
        count = [0]
    if count[0] >= 120:
        return lines
    role = (node.get("role") or "").lower()
    name = (node.get("name") or "").strip()
    if role in _INTERACTIVE_ROLES and name:
        indent = "  " * depth
        lines.append(f"{indent}{role} \"{name}\"")
        count[0] += 1
    for child in node.get("children") or []:
        _format_a11y_tree(child, depth + 1, lines, count)
    return lines


# ─────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────

def _find_in_frames(page, role: str, name: str, exact: bool = False):
    """Find an element by ARIA role+name across the main page and all child frames.

    Search order:
      1. Main page (3 s fast try)
      2. Each child frame (2 s each) — covers Gmail compose, LinkedIn/Instagram iframes, etc.
      3. Fuzzy first-word fallback on main page + frames (handles "Subject field" → "Subject")

    Returns the first visible Playwright Locator, or raises TimeoutError with a helpful message.
    """
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
    return pages[tab_index]


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
        locator = page.locator(target).first
        locator.wait_for(state="visible", timeout=10000)
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
        el = page.locator(target).first
        el.wait_for(state="visible", timeout=10000)
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
        el.type(text, delay=50)  # 50ms delay simulates human typing
        # For tweet compose box: tell the agent exactly what to do next so it doesn't stop early
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
        return f"✅ Pressed: {key}"
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
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.connect_over_cdp(_CDP_URL)
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
        textarea.type(text, delay=50)

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
        textarea.type(text, delay=50)

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

    Returns a list of interactive elements (buttons, links, textboxes, etc.)
    with their ARIA role and visible name — works across iframes automatically.
    Use this to understand page structure WITHOUT needing CSS selectors.
    Then use click_accessible() or type_accessible() to interact with elements.

    Example workflow:
      1. get_snapshot()          → see 'button "Compose"', 'textbox "To"', etc.
      2. click_accessible("button", "Compose")
      3. type_accessible("textbox", "To", "user@example.com")
    """
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, tab_index)

        # Try Playwright's newer aria_snapshot (1.35+) first; fall back to older API
        snapshot_text = None
        try:
            snapshot_text = page.locator("body").aria_snapshot()
        except Exception:
            pass

        if snapshot_text:
            # aria_snapshot returns YAML-like text already formatted
            lines = [l for l in snapshot_text.splitlines() if l.strip()][:120]
            body = "\n".join(lines)
        else:
            # Older Playwright: use accessibility.snapshot() dict + format it
            tree = page.accessibility.snapshot()
            if not tree:
                return "⚠️ Accessibility snapshot is empty — page may still be loading."
            lines = _format_a11y_tree(tree)
            body = "\n".join(lines) if lines else "(No interactive elements found)"

        # Also snapshot child frames — Gmail compose, LinkedIn/Instagram message boxes
        # live inside iframes and may not appear in the main body aria_snapshot.
        frame_sections = []
        for i, frame in enumerate(page.frames):
            if frame is page.main_frame:
                continue
            try:
                fsnapshot = frame.locator("body").aria_snapshot()
                if not fsnapshot:
                    continue
                flines = [l for l in fsnapshot.splitlines() if l.strip()]
                # Only include if the frame has meaningful interactive content
                interactive = [l for l in flines if any(
                    l.lstrip().startswith(r) for r in
                    ("button", "textbox", "link", "combobox", "checkbox", "tab", "menuitem", "searchbox")
                )]
                if interactive:
                    frame_sections.append(
                        f"\n[iframe {i}]\n" + "\n".join(flines[:60])
                    )
            except Exception:
                continue

        if frame_sections:
            body += "\n" + "\n".join(frame_sections)

        return (
            f"📋 Page Snapshot — {page.url}\n"
            f"Title: {page.title()}\n"
            + "─" * 50 + "\n"
            + body + "\n\n"
            "Use click_accessible(role, name) or type_accessible(role, name, text) "
            "to interact with any element above."
        )
    except Exception as e:
        return f"❌ {e}"
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def click_accessible(role: str, name: str, tab_index: int = 0, exact: bool = False) -> str:
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
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, tab_index)
        locator = _find_in_frames(page, role, name, exact=exact)
        locator.click()
        return f"✅ Clicked {role} \"{name}\"\nURL after click: {page.url}"
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


def type_accessible(role: str, name: str, text: str, tab_index: int = 0, exact: bool = False) -> str:
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
    pw = None
    try:
        pw, browser = _connect()
        page = _get_page(browser, tab_index)
        locator = _find_in_frames(page, role, name, exact=exact)
        locator.click()
        # fill() clears and sets value — works on <input>, <textarea>, and contenteditable
        try:
            locator.fill(text)
        except Exception:
            # fallback for non-standard inputs (contenteditable without value)
            locator.press("Control+a")
            locator.type(text, delay=40)
        return f"✅ Typed into {role} \"{name}\": {text[:100]}{'...' if len(text) > 100 else ''}"
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
