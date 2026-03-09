import re
import json
import html
import time
import random
import os
import asyncio
import base64
import threading
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
from urllib.parse import urlparse, urljoin

SKILL_TIMEOUT = 90  # seconds — browser ops need room beyond the default 30s

NAME = "web"
DOC = (
    "Web operations: fetch, search (multiple engines), scrape, download binary files, "
    "find and download images, and full browser automation via Playwright (browser_* functions). "
    "Key functions: search(query) — auto-tries Tavily (if key set) → DuckDuckGo → Bing and "
    "returns the first good result. fetch(url), download(url, filename) — saves binary file to "
    "/app/memory/filename. "
    "find_and_download_image(query, save_as) — ALWAYS use this for image tasks. "
    "Call it directly with just the subject name, e.g. find_and_download_image('Zeus') or "
    "find_and_download_image('sunset beach'). Do NOT search first — this function handles "
    "searching, finding a direct image URL, and downloading in one step. Returns the local path. "
    "Rate limiting and URL validation built-in."
)

# Try to import BeautifulSoup
try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

# Try to import requests
try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# Try to import Playwright
try:
    from playwright.async_api import async_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ============================================
# RATE LIMITING & USER-AGENT ROTATION
# ============================================

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0 Safari/537.36",
]

_last_request_time = None
_min_delay = 2.0
_max_delay = 5.0
_consecutive_errors = 0
_max_backoff = 20  # Must stay under SKILL_TIMEOUT_SECONDS (default 30s)

def _get_random_user_agent() -> str:
    return random.choice(USER_AGENTS)

def _apply_rate_limit():
    global _last_request_time, _consecutive_errors
    if _last_request_time is not None:
        base_delay = min(_min_delay * (1.5 ** _consecutive_errors), _max_backoff)
        jitter = random.uniform(0, _max_delay)
        total_delay = base_delay + jitter
        elapsed = (datetime.now() - _last_request_time).total_seconds()
        if elapsed < total_delay:
            sleep_time = total_delay - elapsed
            time.sleep(sleep_time)
    _last_request_time = datetime.now()

def _record_success():
    global _consecutive_errors
    if _consecutive_errors > 0:
        _consecutive_errors = 0

def _record_error():
    global _consecutive_errors
    _consecutive_errors += 1

_session = None

# ============================================
# BROWSER SINGLETON STATE (Playwright)
# ============================================

_bw_loop: Optional[asyncio.AbstractEventLoop] = None
_bw_thread: Optional[threading.Thread] = None
_bw_playwright = None
_bw_browser = None
_bw_context = None
_bw_page = None
_bw_lock = threading.Lock()

# Hard cap: reset by browser_goto() each navigation, enforced in browser_screenshot()
_bw_screenshot_count = 0
_SCREENSHOT_LIMIT = 3


def _bw_ensure_loop() -> asyncio.AbstractEventLoop:
    """Return the shared background event loop, creating it if needed."""
    global _bw_loop, _bw_thread
    with _bw_lock:
        if _bw_loop is None or _bw_loop.is_closed():
            _bw_loop = asyncio.new_event_loop()
            _bw_thread = threading.Thread(
                target=_bw_loop.run_forever,
                name="trinity-browser-loop",
                daemon=True,
            )
            _bw_thread.start()
    return _bw_loop


def _bw_run(coro, timeout: int = 30) -> Any:
    """Submit an async coroutine to the background loop and block for result."""
    if not HAS_PLAYWRIGHT:
        raise RuntimeError(
            "Playwright not installed. Run: pip install playwright && playwright install chromium"
        )
    loop = _bw_ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


def _get_session():
    global _session
    if _session is None and HAS_REQUESTS:
        _session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=2,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy, pool_maxsize=10)
        _session.mount("https://", adapter)
        _session.mount("http://", adapter)
    return _session

# ============================================
# URL VALIDATION (NEW)
# ============================================

def _is_valid_url(url: str) -> bool:
    """Check if URL is valid"""
    if not url or not isinstance(url, str):
        return False
    
    url = url.strip()
    
    # Check scheme
    if not url.startswith(('http://', 'https://')):
        return False
    
    try:
        parsed = urlparse(url)
        # Must have scheme and netloc
        if not parsed.scheme or not parsed.netloc:
            return False
        # Check for special characters in netloc
        if '..' in parsed.netloc or '//' in parsed.netloc:
            return False
        return True
    except Exception:
        return False

def _normalize_url(url: str) -> str:
    """Normalize URL - adds https:// if missing"""
    url = url.strip()
    if not url.startswith(('http://', 'https://')):
        url = 'https://' + url
    return url

# ============================================
# BASIC FETCH OPERATIONS (with validation)
# ============================================

def fetch(url: str, timeout: int = 30) -> str:
    """Fetch webpage with URL validation"""
    if not HAS_REQUESTS:
        return "❌ requests library not installed"
    
    # NEW: URL validation
    if not _is_valid_url(url):
        url = _normalize_url(url)
        if not _is_valid_url(url):
            return f"❌ Invalid URL: {url}"
    
    try:
        _apply_rate_limit()
        session = _get_session()
        
        headers = {
            'User-Agent': _get_random_user_agent(),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'DNT': '1',
            'Connection': 'keep-alive',
        }
        
        response = session.get(url, headers=headers, timeout=timeout)
        
        if response.status_code == 429:
            _record_error()
            return "❌ Rate limited (429). Try again later."
        
        response.raise_for_status()
        _record_success()
        return response.text
        
    except requests.exceptions.Timeout:
        return f"❌ Request timed out after {timeout}s"
    except requests.exceptions.ConnectionError:
        return f"❌ Connection failed for {url}"
    except requests.exceptions.HTTPError as e:
        return f"❌ HTTP Error: {e}"
    except Exception as e:
        return f"❌ Error: {str(e)}"

def get(url: str) -> str:
    return fetch(url)

def head(url: str) -> str:
    if not HAS_REQUESTS:
        return "❌ requests library not installed"
    
    if not _is_valid_url(url):
        url = _normalize_url(url)
        if not _is_valid_url(url):
            return f"❌ Invalid URL: {url}"
    
    try:
        _apply_rate_limit()
        session = _get_session()
        response = session.head(url, timeout=10, allow_redirects=True)
        
        info = [
            f"📡 {url}",
            f"Status: {response.status_code}",
            f"Content-Type: {response.headers.get('Content-Type', 'unknown')}",
            f"Content-Length: {response.headers.get('Content-Length', 'unknown')}",
        ]
        return "\n".join(info)
    except Exception as e:
        return f"❌ Error: {str(e)}"

# ============================================
# TEXT EXTRACTION
# ============================================

def read(url: str) -> str:
    content = fetch(url)
    if content.startswith("❌"):
        return content
    return extract_text(content)

def extract_text(html_content: str) -> str:
    if HAS_BS4:
        soup = BeautifulSoup(html_content, 'html.parser')
        for element in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
            element.decompose()
        text = soup.get_text(separator='\n')
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        return '\n'.join(chunk for chunk in chunks if chunk)
    else:
        text = re.sub(r'<script[^>]*>.*?</script>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = html.unescape(text)
        return text.strip()

# ============================================
# SCRAPING OPERATIONS (FIXED)
# ============================================

def scrape(url: str, selector: str = "body") -> str:
    content = fetch(url)
    if content.startswith("❌"):
        return content
    return extract_elements(content, selector)

def extract_elements(html_content: str, selector: str) -> str:
    if HAS_BS4:
        soup = BeautifulSoup(html_content, 'html.parser')
        elements = soup.select(selector)
        if not elements:
            return f"No elements found for selector: {selector}"
        results = [f"Found {len(elements)} element(s):"]
        for i, el in enumerate(elements[:20]):
            text = el.get_text(strip=True)
            if text:
                results.append(f"[{i+1}] {text[:500]}")
        return "\n".join(results)
    else:
        return "❌ BeautifulSoup not installed"

def scrape_links(url: str, absolute: bool = True) -> str:
    """
    FIXED: Better link extraction with option for absolute URLs
    """
    content = fetch(url)
    if content.startswith("❌"):
        return content
    
    if not HAS_BS4:
        # Fallback regex - FIXED pattern
        pattern = r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>'
        matches = re.findall(pattern, content, re.IGNORECASE | re.DOTALL)
        
        links = []
        for href, text in matches:
            text = re.sub(r'<[^>]+>', '', text).strip()[:50]
            # Filtering
            if href and not href.startswith(('javascript:', '#', 'mailto:', 'tel:')):
                if absolute and not href.startswith('http'):
                    href = urljoin(url, href)
                links.append(f"{text}: {href}" if text else href)
        
        return f"Found {len(links)} links:\n" + "\n".join(links[:50]) if links else "No links found"
    
    # BeautifulSoup version - BETTER
    soup = BeautifulSoup(content, 'html.parser')
    links = []
    
    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        text = a.get_text(strip=True)[:50]
        
        # FIX: Better filtering
        if not href:
            continue
        if href.startswith(('javascript:', '#', 'mailto:', 'tel:', 'data:')):
            continue
        if 'javascript:void(0)' in href:
            continue
        
        # Convert to absolute URL if needed
        if absolute:
            href = urljoin(url, href)
        
        # Remove fragments (#)
        href = href.split('#')[0]
        
        if href:  # Add only if not empty
            links.append({
                'url': href,
                'text': text,
                'title': a.get('title', '')
            })
    
    if not links:
        return "No links found on page"
    
    # Format results
    results = [f"🔗 Found {len(links)} links:\n"]
    for i, link in enumerate(links[:50], 1):
        display_text = link['text'] or link['title'] or f"Link {i}"
        results.append(f"{i}. {display_text}")
        results.append(f"   {link['url']}")
    
    return "\n".join(results)

def scrape_images(url: str, absolute: bool = True) -> str:
    """FIXED: Better image extraction"""
    content = fetch(url)
    if content.startswith("❌"):
        return content
    
    if not HAS_BS4:
        pattern = r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>'
        matches = re.findall(pattern, content, re.IGNORECASE)
        images = [m for m in matches if m and not m.startswith('data:')]
        return f"Found {len(images)} images:\n" + "\n".join(images[:30]) if images else "No images found"
    
    soup = BeautifulSoup(content, 'html.parser')
    images = []
    
    for img in soup.find_all('img'):
        src = img.get('src', '').strip()
        if not src or src.startswith('data:'):
            continue
        
        if absolute:
            src = urljoin(url, src)
        
        images.append({
            'src': src,
            'alt': img.get('alt', '')[:50],
            'title': img.get('title', ''),
            'width': img.get('width', ''),
            'height': img.get('height', '')
        })
    
    if not images:
        return "No images found on page"
    
    results = [f"🖼️ Found {len(images)} images:\n"]
    for i, img in enumerate(images[:30], 1):
        desc = img['alt'] or img['title'] or f"Image {i}"
        dims = f"({img['width']}x{img['height']})" if img['width'] and img['height'] else ""
        results.append(f"{i}. {desc} {dims}")
        results.append(f"   {img['src']}")
    
    return "\n".join(results)

def scrape_table(url: str, table_index: int = 0) -> str:
    content = fetch(url)
    if content.startswith("❌"):
        return content
    
    if not HAS_BS4:
        return "❌ BeautifulSoup not installed"
    
    soup = BeautifulSoup(content, 'html.parser')
    tables = soup.find_all('table')
    
    if not tables:
        return "No tables found on page"
    
    if table_index >= len(tables):
        return f"Table {table_index} not found. Total: {len(tables)}"
    
    table = tables[table_index]
    rows = []
    
    for tr in table.find_all('tr'):
        cells = [cell.get_text(strip=True) for cell in tr.find_all(['th', 'td'])]
        if cells:
            rows.append(cells)
    
    if not rows:
        return "Table is empty"
    
    # Format as text table
    result = [f"📊 Table {table_index + 1} ({len(rows)} rows):\n"]
    
    # Find max width for each column
    col_widths = []
    for col_idx in range(max(len(row) for row in rows)):
        max_width = max(
            len(str(row[col_idx])) if col_idx < len(row) else 0 
            for row in rows
        )
        col_widths.append(min(max_width + 2, 30))  # Max 30 chars
    
    for row in rows[:20]:
        formatted_row = []
        for i, cell in enumerate(row[:5]):  # Max 5 columns
            formatted_row.append(str(cell)[:col_widths[i]].ljust(col_widths[i]))
        result.append(" | ".join(formatted_row))
    
    return "\n".join(result)

# ============================================
# SEARCH - FIXED (multiple search engines)
# ============================================

_TIME_SENSITIVE_KEYWORDS = [
    "weather", "temperature", "forecast", "rain", "humidity", "wind",
    "current", "now", "today", "tonight", "live", "latest", "breaking",
    "stock", "price", "market", "score", "game", "match", "result",
    "news", "update", "happening",
]

def _is_time_sensitive(query: str) -> bool:
    """Return True if the query is likely asking for real-time or same-day data."""
    q = query.lower()
    return any(kw in q for kw in _TIME_SENSITIVE_KEYWORDS)


def search(query: str, engine: str = "auto", num: str = "5") -> str:
    """
    Search the web. Tries engines in priority order and returns the first good result.

    Priority for "auto" (default):
      1. Tavily API  — if TAVILY_API_KEY env var is set (fast, accurate, free tier 1000/mo)
      2. DuckDuckGo  — HTML scraping, no key needed
      3. Bing        — HTML scraping with rich answer box support, no key needed

    Args:
        query:  Search query
        engine: "auto" (default) | "tavily" | "duckduckgo" | "bing"
        num:    Number of results (max 10)

    Returns:
        Search results string
    """
    if not HAS_REQUESTS:
        return "❌ requests library not installed"

    if not query or not query.strip():
        return "❌ Query cannot be empty"

    n = min(int(num) if str(num).isdigit() else 10, 15)
    query = query.strip()
    eng = engine.lower()

    # For time-sensitive queries, append today's date so ranking favours fresh results
    if _is_time_sensitive(query):
        today = datetime.now().strftime("%Y-%m-%d")
        if today not in query:
            query = f"{query} {today}"

    # ── Explicit engine selection ──────────────────────────────────────────────
    if eng == "tavily":
        result = _search_tavily(query, n)
        if result.startswith("❌") or result.startswith("No results"):
            result = _search_duckduckgo(query, n)
        if result.startswith("❌") or result.startswith("No results"):
            result = _search_bing(query, n)
        return result

    if eng == "duckduckgo":
        result = _search_duckduckgo(query, n)
        if result.startswith("❌") or result.startswith("No results"):
            result = _search_bing(query, n)
        return result

    if eng == "bing":
        result = _search_bing(query, n)
        if result.startswith("❌") or result.startswith("No results"):
            result = _search_duckduckgo(query, n)
        return result

    if eng != "auto":
        return f"❌ Unknown engine: {engine}. Use: auto, tavily, duckduckgo, bing"

    # ── Auto: priority order ───────────────────────────────────────────────────
    # 1. Tavily (if API key is configured)
    if os.environ.get("TAVILY_API_KEY"):
        result = _search_tavily(query, n)
        if result and not result.startswith("❌") and not result.startswith("No results"):
            return result

    # 2. DuckDuckGo
    result = _search_duckduckgo(query, n)
    if result and not result.startswith("❌") and not result.startswith("No results"):
        return result

    # 3. Bing
    result = _search_bing(query, n)
    return result


def _search_tavily(query: str, n: int) -> str:
    """Internal: Tavily API search (highest quality, requires TAVILY_API_KEY)."""
    api_key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not api_key:
        return "❌ TAVILY_API_KEY not set"

    try:
        live = _is_time_sensitive(query)
        payload = {
            "api_key": api_key,
            "query": query,
            "max_results": n,
            "include_answer": True,
            "search_depth": "advanced",
        }
        if live:
            payload["topic"] = "news"
            payload["days"] = 1
        response = requests.post(
            "https://api.tavily.com/search",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )

        if response.status_code == 401:
            return "❌ Tavily: Invalid API key"
        if response.status_code == 429:
            _record_error()
            return "❌ Tavily: Rate limit reached"
        response.raise_for_status()
        _record_success()

        data = response.json()
        results = [f"🔍 Tavily: {query}\n"]

        # Optional direct answer (when Tavily is confident)
        answer = data.get("answer", "")
        if answer:
            results.append(f"💡 Answer: {answer}\n")

        for i, r in enumerate(data.get("results", [])[:n], 1):
            title = r.get("title", "No title")
            url = r.get("url", "")
            content = r.get("content", "")
            results.append(f"\n{i}. {title}")
            if content:
                results.append(f"   {content[:600]}...")
            if url:
                results.append(f"   🔗 {url}")

        return "\n".join(results) if len(results) > 1 else f"No results found for: {query}"

    except requests.exceptions.Timeout:
        return "❌ Tavily timeout"
    except Exception as e:
        return f"❌ Tavily error: {type(e).__name__}: {str(e)[:100]}"

def _search_duckduckgo(query: str, n: int) -> str:
    """Internal: DuckDuckGo search (HTML scraping)"""
    try:
        _apply_rate_limit()
        # DuckDuckGo HTML endpoint
        url = "https://html.duckduckgo.com/html/"
        
        headers = {
            'User-Agent': _get_random_user_agent(),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Content-Type': 'application/x-www-form-urlencoded',
        }
        
        # Direct request (not the shared session) to avoid retry-backoff hangs
        response = requests.post(
            url,
            data={'q': query, 'kl': 'us-en', 'df': 'd' if _is_time_sensitive(query) else ''},
            headers=headers,
            timeout=10,
        )
        
        if response.status_code == 429:
            _record_error()
            return "❌ DuckDuckGo rate limit. Please try again in 60 seconds."
        
        response.raise_for_status()
        _record_success()
        
        if HAS_BS4:
            soup = BeautifulSoup(response.text, 'html.parser')
            results = [f"🔍 DuckDuckGo: {query}\n"]
            
            # DuckDuckGo results - FIXED selector
            result_divs = soup.find_all('div', class_='result') or soup.find_all('div', class_='web-result')
            
            for i, result in enumerate(result_divs[:n], 1):
                # Try multiple selectors for title
                title_elem = (
                    result.find('a', class_='result__a') or 
                    result.find('h2', class_='result__title')
                )
                
                # Try multiple selectors for snippet
                snippet_elem = (
                    result.find('a', class_='result__snippet') or
                    result.find('div', class_='result__snippet') or
                    result.find('div', class_='web-result__snippet')
                )
                
                # URL
                url_elem = result.find('a', class_='result__url') or result.find('a', href=True)
                
                if title_elem:
                    title = title_elem.get_text(strip=True)
                    link = url_elem['href'] if url_elem and url_elem.get('href') else 'N/A'
                    snippet = snippet_elem.get_text(strip=True) if snippet_elem else ''
                    
                    results.append(f"\n{i}. {title}")
                    if snippet:
                        results.append(f"   {snippet[:200]}...")
                    results.append(f"   🔗 {link}")
            
            if len(results) == 1:
                return f"No results found for: {query}"
            
            return "\n".join(results)
        else:
            # Regex fallback
            pattern = r'<a[^>]+class="result__a"[^>]*>(.*?)</a>.*?<a[^>]+class="result__snippet"[^>]*>(.*?)</a>'
            matches = re.findall(pattern, response.text, re.DOTALL | re.IGNORECASE)
            
            if matches:
                results = [f"🔍 DuckDuckGo: {query}\n"]
                for i, (title, snippet) in enumerate(matches[:n], 1):
                    title = re.sub(r'<[^>]+>', '', title)
                    snippet = re.sub(r'<[^>]+>', '', snippet)
                    results.append(f"\n{i}. {title}")
                    results.append(f"   {snippet[:150]}...")
                return "\n".join(results)
            
            return f"Search completed. BeautifulSoup not available for parsing."
            
    except requests.exceptions.Timeout:
        return "❌ DuckDuckGo timeout"
    except Exception as e:
        return f"❌ DuckDuckGo error: {type(e).__name__}: {str(e)[:100]}"

def _search_bing(query: str, n: int) -> str:
    """Internal: Bing search (fallback)"""
    try:
        _apply_rate_limit()
        url = "https://www.bing.com/search"
        headers = {
            'User-Agent': _get_random_user_agent(),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        # Direct request (not the shared session) to avoid retry-backoff hangs
        response = requests.get(
            url,
            params={'q': query, 'count': n},
            headers=headers,
            timeout=10,
        )
        
        if response.status_code == 429:
            return "❌ Bing rate limit"
        
        response.raise_for_status()
        
        if HAS_BS4:
            soup = BeautifulSoup(response.text, 'html.parser')
            results = [f"🔍 Bing: {query}\n"]
            
            # Bing results
            for result in soup.find_all('li', class_='b_algo')[:n]:
                title_elem = result.find('h2')
                link_elem = title_elem.find('a') if title_elem else None
                snippet_elem = result.find('p')
                
                if title_elem and link_elem:
                    title = link_elem.get_text(strip=True)
                    link = link_elem.get('href', '')
                    snippet = snippet_elem.get_text(strip=True) if snippet_elem else ''
                    
                    results.append(f"\n• {title}")
                    if snippet:
                        results.append(f"  {snippet[:200]}...")
                    results.append(f"  🔗 {link}")
            
            return "\n".join(results) if len(results) > 1 else f"No results on Bing for: {query}"
        
        return "Bing search requires BeautifulSoup"
        
    except Exception as e:
        return f"❌ Bing error: {str(e)[:100]}"

# ============================================
# OTHER FUNCTIONS
# ============================================

def find_text(url: str, pattern: str) -> str:
    content = fetch(url)
    if content.startswith("❌"):
        return content
    
    text = extract_text(content)
    
    try:
        matches = re.findall(pattern, text, re.IGNORECASE)
        if matches:
            return f"Found {len(matches)} matches:\n" + "\n".join(str(m)[:200] for m in matches[:10])
    except re.error:
        pass
    
    if pattern.lower() in text.lower():
        idx = text.lower().find(pattern.lower())
        context = text[max(0, idx-100):idx+100]
        return f"Found at position {idx}:\n...{context}..."
    
    return f"Pattern '{pattern}' not found"

def grep(url: str, pattern: str) -> str:
    return find_text(url, pattern)

def download(url: str, filename: str = "") -> str:
    if not HAS_REQUESTS:
        return "❌ requests library not installed"
    
    if not _is_valid_url(url):
        url = _normalize_url(url)
    
    try:
        # Direct request, no retry machinery — avoids multi-minute hangs on slow hosts
        response = requests.get(
            url,
            timeout=15,
            stream=True,
            headers={
                "User-Agent": _get_random_user_agent(),
                "Referer": "https://www.google.com/",
            },
        )
        response.raise_for_status()
        
        if not filename:
            filename = url.split('/')[-1] or "downloaded_file"
            filename = filename.split('?')[0]
        
        filepath = f"/app/memory/{filename}"
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        
        with open(filepath, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        size = os.path.getsize(filepath)
        return f"✅ Downloaded: {filepath} ({size} bytes)"
    except Exception as e:
        return f"❌ Download error: {str(e)}"


def find_and_download_image(query: str, save_as: str = "") -> str:
    """
    One-step: search the web for an image matching the query, find a direct
    image URL (.jpg/.png/.gif/.webp), download it to /app/memory/, and return
    the local file path so it can be uploaded directly to Google Drive or used
    elsewhere.

    Args:
        query:   What to search for, e.g. "small dog", "sunset beach"
        save_as: Filename to save as (e.g. "dog.jpg"). If empty, auto-derived
                 from the URL.

    Returns:
        Local path string on success, e.g. "/app/memory/dog.jpg"
        Error string starting with ❌ on failure.
    """
    if not HAS_REQUESTS:
        return "❌ requests library not installed"

    IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp")
    direct_url = ""

    _headers = {
        "User-Agent": _get_random_user_agent(),
        "Referer": "https://en.wikipedia.org/",
    }

    # ── Strategy 1: Wikipedia article thumbnail (most reliable, no 403) ───────
    # Wikipedia's REST summary API returns the article's lead thumbnail — a
    # human-curated, high-quality image. Works well for animals, places, objects.
    words = query.strip().split()
    # Build candidate article titles: full query, drop leading words, drop trailing
    seen: set = set()
    wp_candidates: list = []
    for i in range(len(words)):
        c = " ".join(words[i:])
        if c not in seen:
            seen.add(c)
            wp_candidates.append(c)
    for i in range(len(words) - 1, 0, -1):
        c = " ".join(words[:i])
        if c not in seen:
            seen.add(c)
            wp_candidates.append(c)

    for candidate in wp_candidates:
        try:
            # First: search Wikipedia for the best article title
            search_url = (
                "https://en.wikipedia.org/w/api.php?action=query&list=search"
                f"&srsearch={requests.utils.quote(candidate)}&srlimit=1&format=json"
            )
            sr = requests.get(search_url, timeout=6, headers=_headers)
            sr_data = sr.json()
            hits = sr_data.get("query", {}).get("search", [])
            if not hits:
                continue
            title = hits[0]["title"]
            # Then: fetch REST summary for the thumbnail URL
            summary_url = (
                f"https://en.wikipedia.org/api/rest_v1/page/summary/"
                f"{requests.utils.quote(title)}"
            )
            sm = requests.get(summary_url, timeout=6, headers=_headers)
            sm_data = sm.json()
            thumb = sm_data.get("thumbnail", {}).get("source", "")
            orig = sm_data.get("originalimage", {}).get("source", "")
            # Prefer original full-size; fall back to thumbnail
            for img_url in [orig, thumb]:
                if not img_url:
                    continue
                lower = img_url.lower().split("?")[0]
                if any(lower.endswith(ext) for ext in IMAGE_EXTS):
                    direct_url = img_url
                    break
            if direct_url:
                break
        except Exception:
            continue

    # ── Strategy 2: DuckDuckGo filetype search, extract direct image URLs ─────
    if not direct_url:
        try:
            search_result = search(
                f"{query} filetype:jpg OR filetype:png", engine="duckduckgo", num="10"
            )
            url_candidates = re.findall(r'https?://[^\s\)\]"\'<>]+', search_result)
            for candidate in url_candidates:
                lower = candidate.lower().split("?")[0]
                if any(lower.endswith(ext) for ext in IMAGE_EXTS):
                    direct_url = candidate
                    break
        except Exception:
            pass

    if not direct_url:
        return (
            f"❌ Could not find a direct image URL for '{query}'. "
            "Try a more specific query or provide a direct image URL to web.download."
        )

    # ── Download the image ─────────────────────────────────────────────────────
    # Reject placeholder values (LLMs sometimes pass the param name literally)
    if save_as and '.' not in save_as:
        save_as = ""
    if not save_as:
        # Auto-derive filename from URL, keep it clean
        raw_name = direct_url.split("/")[-1].split("?")[0]
        save_as = re.sub(r"[^\w.\-]", "_", raw_name)[:80] or "image.jpg"

    result = download(direct_url, save_as)
    if result.startswith("❌"):
        return result

    local_path = f"/app/memory/{save_as}"
    return f"✅ Image ready at: {local_path}\nSource: {direct_url}\n{result}"


def extract_meta(url: str) -> str:
    content = fetch(url)
    if content.startswith("❌"):
        return content
    
    if HAS_BS4:
        soup = BeautifulSoup(content, 'html.parser')
        meta = []
        
        if soup.title:
            meta.append(f"📄 Title: {soup.title.string}")
        
        desc = soup.find('meta', attrs={'name': 'description'})
        if desc:
            meta.append(f"📝 Description: {desc.get('content', '')}")
        
        keywords = soup.find('meta', attrs={'name': 'keywords'})
        if keywords:
            meta.append(f"🏷️ Keywords: {keywords.get('content', '')}")
        
        og_title = soup.find('meta', property='og:title')
        if og_title:
            meta.append(f"OG Title: {og_title.get('content', '')}")
        
        return "\n".join(meta) if meta else "No metadata found"
    else:
        meta = []
        title = re.search(r'<title[^>]*>(.*?)</title>', content, re.IGNORECASE | re.DOTALL)
        if title:
            meta.append(f"Title: {title.group(1).strip()}")
        return "\n".join(meta) if meta else "No metadata found"

def status() -> str:
    bw_status = "❌ (not installed)"
    if HAS_PLAYWRIGHT:
        bw_status = "✅ launched" if _bw_browser is not None else "✅ installed (idle)"
    info = [
        "🌐 Web Skill Status",
        f"Requests: {'✅' if HAS_REQUESTS else '❌'}",
        f"BeautifulSoup: {'✅' if HAS_BS4 else '❌'}",
        f"Playwright browser: {bw_status}",
        f"Rate limiting: ✅ ({_min_delay}s)",
        f"User-Agents: {len(USER_AGENTS)}"
    ]
    return "\n".join(info)


# ============================================
# BROWSER AUTOMATION — async internals
# ============================================

async def _bw_async_launch(headless: bool = True, slow_mo: int = 0) -> Dict:
    global _bw_playwright, _bw_browser, _bw_context, _bw_page
    if _bw_browser is not None:
        return {"ok": True, "msg": "Browser already running"}
    _bw_playwright = await async_playwright().start()
    _bw_browser = await _bw_playwright.chromium.launch(
        headless=headless,
        slow_mo=slow_mo,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    _bw_context = await _bw_browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent=_get_random_user_agent(),
        locale="en-US",
    )
    _bw_page = await _bw_context.new_page()
    return {"ok": True, "msg": f"Browser launched (headless={headless})"}


async def _bw_async_goto(url: str, wait_until: str = "networkidle", timeout: int = 30000) -> Dict:
    global _bw_page
    if _bw_page is None:
        await _bw_async_launch()
    resp = await _bw_page.goto(url, wait_until=wait_until, timeout=timeout)
    return {
        "ok": True,
        "url": _bw_page.url,
        "title": await _bw_page.title(),
        "status": resp.status if resp else None,
    }


async def _bw_async_screenshot(path: str = "", full_page: bool = False) -> Dict:
    if _bw_page is None:
        return {"ok": False, "error": "No page open. Call browser_goto() first."}
    raw = await _bw_page.screenshot(full_page=full_page, type="png")
    b64 = base64.b64encode(raw).decode()
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "wb") as f:
            f.write(raw)
    return {"ok": True, "base64": b64, "saved_to": path or None, "size_kb": round(len(raw) / 1024, 1)}


async def _bw_async_get_text() -> Dict:
    if _bw_page is None:
        return {"ok": False, "error": "No page open."}
    text = await _bw_page.evaluate("() => document.body.innerText")
    return {"ok": True, "text": text, "url": _bw_page.url}


async def _bw_async_get_html() -> Dict:
    if _bw_page is None:
        return {"ok": False, "error": "No page open."}
    content = await _bw_page.content()
    return {"ok": True, "html": content, "url": _bw_page.url}


async def _bw_async_click(selector: str, timeout: int = 5000) -> Dict:
    if _bw_page is None:
        return {"ok": False, "error": "No page open."}
    await _bw_page.click(selector, timeout=timeout)
    return {"ok": True, "clicked": selector}


async def _bw_async_type(selector: str, text: str, delay: int = 40, clear_first: bool = True) -> Dict:
    if _bw_page is None:
        return {"ok": False, "error": "No page open."}
    await _bw_page.click(selector)
    if clear_first:
        await _bw_page.fill(selector, "")
    await _bw_page.type(selector, text, delay=delay)
    return {"ok": True, "typed": text, "into": selector}


async def _bw_async_fill(selector: str, value: str) -> Dict:
    if _bw_page is None:
        return {"ok": False, "error": "No page open."}
    await _bw_page.fill(selector, value)
    return {"ok": True, "filled": selector, "value": value}


async def _bw_async_select(selector: str, value: str) -> Dict:
    if _bw_page is None:
        return {"ok": False, "error": "No page open."}
    selected = await _bw_page.select_option(selector, value=value)
    return {"ok": True, "selector": selector, "selected": selected}


async def _bw_async_evaluate(js: str) -> Dict:
    if _bw_page is None:
        return {"ok": False, "error": "No page open."}
    result = await _bw_page.evaluate(js)
    return {"ok": True, "result": result}


async def _bw_async_wait_for(selector: str, state: str = "visible", timeout: int = 5000) -> Dict:
    if _bw_page is None:
        return {"ok": False, "error": "No page open."}
    await _bw_page.wait_for_selector(selector, state=state, timeout=timeout)
    return {"ok": True, "found": selector}


async def _bw_async_new_tab(url: str = "") -> Dict:
    global _bw_page
    if _bw_context is None:
        return {"ok": False, "error": "Browser not launched. Call browser_launch() or browser_goto() first."}
    _bw_page = await _bw_context.new_page()
    if url:
        await _bw_page.goto(url, wait_until="networkidle")
    return {"ok": True, "url": _bw_page.url, "title": await _bw_page.title()}


async def _bw_async_close_tab() -> Dict:
    global _bw_page
    if _bw_page is None:
        return {"ok": False, "error": "No page open."}
    await _bw_page.close()
    pages = _bw_context.pages if _bw_context else []
    _bw_page = pages[-1] if pages else None
    return {"ok": True, "remaining_tabs": len(pages)}


async def _bw_async_go_back(timeout: int = 5000) -> Dict:
    if _bw_page is None:
        return {"ok": False, "error": "No page open."}
    await _bw_page.go_back(timeout=timeout)
    return {"ok": True, "url": _bw_page.url, "title": await _bw_page.title()}


async def _bw_async_go_forward(timeout: int = 5000) -> Dict:
    if _bw_page is None:
        return {"ok": False, "error": "No page open."}
    await _bw_page.go_forward(timeout=timeout)
    return {"ok": True, "url": _bw_page.url, "title": await _bw_page.title()}


async def _bw_async_scroll(x: int = 0, y: int = 500) -> Dict:
    if _bw_page is None:
        return {"ok": False, "error": "No page open."}
    await _bw_page.evaluate(f"window.scrollBy({x}, {y})")
    return {"ok": True, "scrolled": {"x": x, "y": y}}


async def _bw_async_hover(selector: str) -> Dict:
    if _bw_page is None:
        return {"ok": False, "error": "No page open."}
    await _bw_page.hover(selector)
    return {"ok": True, "hovered": selector}


async def _bw_async_press(key: str) -> Dict:
    if _bw_page is None:
        return {"ok": False, "error": "No page open."}
    await _bw_page.keyboard.press(key)
    return {"ok": True, "pressed": key}


async def _bw_async_get_links() -> Dict:
    if _bw_page is None:
        return {"ok": False, "error": "No page open."}
    links = await _bw_page.evaluate("""
        () => Array.from(document.querySelectorAll('a[href]'))
              .map(a => ({ text: a.innerText.trim(), href: a.href }))
              .filter(l => l.href.startsWith('http'))
              .slice(0, 100)
    """)
    return {"ok": True, "links": links, "count": len(links)}


async def _bw_async_get_inputs() -> Dict:
    if _bw_page is None:
        return {"ok": False, "error": "No page open."}
    inputs = await _bw_page.evaluate("""
        () => Array.from(document.querySelectorAll('input,textarea,select,button'))
              .map(el => ({
                  tag: el.tagName.toLowerCase(),
                  type: el.type || '',
                  name: el.name || '',
                  id: el.id || '',
                  placeholder: el.placeholder || '',
                  value: el.value || '',
                  text: el.innerText?.trim() || ''
              }))
    """)
    return {"ok": True, "inputs": inputs, "count": len(inputs)}


async def _bw_async_close() -> Dict:
    global _bw_playwright, _bw_browser, _bw_context, _bw_page
    if _bw_browser:
        await _bw_browser.close()
    if _bw_playwright:
        await _bw_playwright.stop()
    _bw_browser = None
    _bw_context = None
    _bw_page = None
    _bw_playwright = None
    return {"ok": True, "msg": "Browser closed"}


async def _bw_async_bstatus() -> Dict:
    return {
        "ok": True,
        "launched": _bw_browser is not None,
        "url": _bw_page.url if _bw_page else None,
        "title": await _bw_page.title() if _bw_page else None,
        "tabs": len(_bw_context.pages) if _bw_context else 0,
    }


# ============================================
# BROWSER AUTOMATION — public sync API
# ============================================

def browser_launch(headless: bool = True, slow_mo: int = 0) -> Dict:
    """
    Launch the browser. browser_goto() auto-launches if you skip this.
    headless=False shows the window (useful for debugging).
    slow_mo adds ms delay between actions so you can watch execution.
    """
    return _bw_run(_bw_async_launch(headless=headless, slow_mo=slow_mo))


def browser_goto(url: str, wait_until: str = "domcontentloaded", timeout: int = 30000) -> Dict:
    """
    Navigate to a URL. Auto-launches browser on first call.
    MUST be called before browser_screenshot() or any other browser action.
    wait_until: 'domcontentloaded' (recommended) | 'load' | 'networkidle' | 'commit'
    timeout: milliseconds to wait (default: 30000 = 30 seconds). Do not pass values under 5000.
    Returns: {ok, url, title, status}
    """
    try:
        timeout = int(timeout)
    except (ValueError, TypeError):
        timeout = 30000
    if timeout < 5000:
        timeout = timeout * 1000  # auto-scale: LLM passed seconds instead of ms
    _valid_wait = {"networkidle", "load", "domcontentloaded", "commit"}
    if wait_until not in _valid_wait:
        wait_until = "domcontentloaded"
    global _bw_screenshot_count
    _bw_screenshot_count = 0  # reset cap on each new navigation
    return _bw_run(_bw_async_goto(url, wait_until=wait_until, timeout=timeout), timeout=timeout // 1000 + 5)


def browser_screenshot(path: str = "", full_page: bool = False) -> Dict:
    """
    Screenshot the current page. Requires browser_goto() to have been called first.
    Saves to /app/memory/screenshots/<timestamp>.png by default — do NOT pass a path
    inside /app/memory/ directly (that folder is for JSON data files, not images).
    full_page=True captures the ENTIRE scrollable page in ONE call — no need to scroll
    and call this multiple times. Limit: 3 screenshots per navigation maximum.
    Returns: {ok, saved_to, size_kb} — base64 is omitted to keep output concise.
    """
    global _bw_screenshot_count
    if _bw_screenshot_count >= _SCREENSHOT_LIMIT:
        return {
            "ok": False,
            "error": (
                f"Screenshot limit ({_SCREENSHOT_LIMIT}) reached for this page. "
                "Use full_page=True in a single call to capture the whole page, "
                "or call browser_goto() to navigate before taking more screenshots."
            ),
        }
    if isinstance(full_page, str):
        full_page = full_page.lower() in ("true", "1", "yes")
    if not path:
        from datetime import datetime as _dt
        ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        path = f"/app/memory/screenshots/screenshot_{ts}.png"
    result = _bw_run(_bw_async_screenshot(path=path, full_page=full_page))
    if isinstance(result, dict) and result.get("ok"):
        _bw_screenshot_count += 1
    # Strip base64 blob — it's hundreds of KB and the LLM cannot use raw base64.
    # The saved file path is all that's needed.
    if isinstance(result, dict):
        result.pop("base64", None)
    return result


def browser_text() -> Dict:
    """
    Get visible text from the current page (works on JS-rendered pages).
    Returns: {ok, text, url}
    """
    return _bw_run(_bw_async_get_text())


def browser_html() -> Dict:
    """
    Get full HTML source after JS execution.
    Returns: {ok, html, url}
    """
    return _bw_run(_bw_async_get_html())


def browser_click(selector: str, timeout: int = 5000) -> Dict:
    """
    Click an element. Accepts CSS selectors, text selectors, or XPath.
    Examples: browser_click('#submit'), browser_click('text=Sign In')
    """
    return _bw_run(_bw_async_click(selector, timeout=timeout))


def browser_type(selector: str, text: str, delay: int = 40, clear_first: bool = True) -> Dict:
    """
    Type into a field with realistic keystroke delay.
    clear_first=True clears existing content before typing.
    """
    return _bw_run(_bw_async_type(selector, text, delay=delay, clear_first=clear_first))


def browser_fill(selector: str, value: str) -> Dict:
    """
    Instantly fill a form field (no keystroke delay). Faster than browser_type().
    """
    return _bw_run(_bw_async_fill(selector, value))


def browser_select(selector: str, value: str) -> Dict:
    """
    Select a <select> dropdown option by value.
    Example: browser_select('#country', 'US')
    """
    return _bw_run(_bw_async_select(selector, value))


def browser_evaluate(js: str) -> Dict:
    """
    Execute JavaScript on the page and return the result.
    Example: browser_evaluate("document.title")
    """
    return _bw_run(_bw_async_evaluate(js))


def browser_wait(selector: str, state: str = "visible", timeout: int = 5000) -> Dict:
    """
    Wait for an element state: 'visible' | 'hidden' | 'attached' | 'detached'.
    Useful after clicks that trigger dynamic content.
    """
    return _bw_run(_bw_async_wait_for(selector, state=state, timeout=timeout))


def browser_new_tab(url: str = "") -> Dict:
    """
    Open a new tab, optionally navigating to a URL.
    The new tab becomes active for subsequent commands.
    """
    return _bw_run(_bw_async_new_tab(url))


def browser_close_tab() -> Dict:
    """Close the current tab. Switches focus to previous tab if available."""
    return _bw_run(_bw_async_close_tab())


def browser_back(timeout: int = 5000) -> Dict:
    """Navigate to the previous page in browser history."""
    return _bw_run(_bw_async_go_back(timeout=timeout))


def browser_forward(timeout: int = 5000) -> Dict:
    """Navigate to the next page in browser history."""
    return _bw_run(_bw_async_go_forward(timeout=timeout))


def browser_scroll(x: int = 0, y: int = 500) -> Dict:
    """
    Scroll the page by (x, y) pixels.
    browser_scroll(y=500) scrolls down, browser_scroll(y=-500) scrolls up.
    """
    return _bw_run(_bw_async_scroll(x=x, y=y))


def browser_hover(selector: str) -> Dict:
    """Move mouse over an element to trigger hover states / tooltips."""
    return _bw_run(_bw_async_hover(selector))


def browser_press(key: str) -> Dict:
    """
    Press a keyboard key.
    Examples: browser_press('Enter'), browser_press('Tab'), browser_press('Control+a')
    """
    return _bw_run(_bw_async_press(key))


def browser_links() -> Dict:
    """
    Extract all hyperlinks from the current page.
    Returns: {ok, links: [{text, href}], count}
    """
    return _bw_run(_bw_async_get_links())


def browser_inputs() -> Dict:
    """
    Inspect all form inputs, buttons, and interactive elements on the page.
    Useful for understanding what actions are available before interacting.
    Returns: {ok, inputs: [{tag, type, name, id, placeholder, value, text}], count}
    """
    return _bw_run(_bw_async_get_inputs())


def browser_close() -> Dict:
    """Close the browser and release all resources."""
    return _bw_run(_bw_async_close())


def browser_status() -> Dict:
    """
    Check browser state: launched, current URL, title, open tab count.
    Returns: {ok, launched, url, title, tabs}
    """
    if not HAS_PLAYWRIGHT:
        return {"ok": False, "error": "Playwright not installed."}
    return _bw_run(_bw_async_bstatus())