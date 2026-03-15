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
    "Functions: "
    "list_tabs()→list all open tabs with index/title/URL; "
    "screenshot(tab_index?)→capture screenshot of current tab, saved to /app/memory/browser_screenshots/; "
    "goto(url, tab_index?)→navigate to a URL in specified tab; "
    "get_text(tab_index?)→extract visible text content from current page (up to 5000 chars); "
    "get_html(tab_index?, selector?)→get full HTML or HTML of a specific element by CSS selector; "
    "click(target, tab_index?)→click element — supports CSS selector, 'text=Foo', 'aria-label=Bar'; "
    "type_text(target, text, clear_first?, tab_index?)→type into any field including contenteditable (Twitter/LinkedIn); "
    "scroll(direction, tab_index?)→scroll page: up / down / top / bottom; "
    "press_key(key, tab_index?)→press keyboard key: Enter, Escape, Tab, Control+Enter, etc.; "
    "wait_for(selector, tab_index?, timeout_ms?)→wait for element to appear before next action; "
    "new_tab(url?)→open a new tab, optionally navigate to URL; "
    "evaluate(js_code, tab_index?)→run JavaScript and return result."
)

_SCREENSHOT_DIR = Path("/app/memory/browser_screenshots")
_CDP_URL = os.getenv("BROWSER_CDP_URL", "http://host.docker.internal:9223")

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
# INTERNAL HELPERS
# ─────────────────────────────────────────────

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
    tab_index = int(tab_index)  # coerce string args from LLM function calling
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
        return f"✅ Typed {len(text)} characters into: {target}"
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
