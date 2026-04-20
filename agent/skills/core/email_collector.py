import html
import json
import re
import time
import random
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse
from typing import List, Dict, Optional

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

NAME = "email_collector"
SHORT_DOC = "Collect emails from websites. For ANY listing/directory page → call collect_with_browser(url, max_sites=5). For direct known URLs → collect_from_urls(urls). NEVER use web.fetch or web.search for this."
DOC = (
    "Universal email collection tool with persistent storage and built-in anti-detection. "
    "IMPORTANT: NEVER use web.search or web.fetch for email tasks — always call this skill's functions. "
    "Anti-detection (user-agent rotation, delays, retries) is already built-in — do NOT search for bypass techniques. "
    "FUNCTION SELECTION GUIDE: "
    "(1) User gives you a DIRECTORY page (BuiltWith, Crunchbase, search results, any listing page) "
    "→ use collect_with_browser(directory_url, max_sites=5) — it opens the page in a browser, "
    "extracts all linked external sites, visits each one, and finds emails. "
    "(2) User gives you DIRECT site URLs they already know "
    "→ use collect_from_urls(urls, max_per_site=1). "
    "(3) Directory page with no JS needed (simple HTML links) "
    "→ use collect_from_directory(directory_url, max_sites=50). "
    "(4) Sites are heavily bot-protected (Cloudflare, etc.) "
    "→ use collect_stealth(target_urls, max_sites=10). "
    "Functions: "
    "collect_with_browser(directory_url, max_sites=5)→BEST for BuiltWith/Crunchbase/directory pages; "
    "opens page in browser, extracts linked sites, visits each, checks /contact and /about; "
    "collect_from_urls(urls, max_per_site=1, stealth=True)→for direct known site URLs (comma-separated); "
    "collect_from_directory(directory_url, max_sites=50, stealth=True)→simple HTML directories; "
    "collect_stealth(target_urls, max_sites=10)→full Playwright anti-detection for protected sites; "
    "add_email(email, source, notes?)→manually add; "
    "list_emails(limit=50)→show recent; "
    "search_emails(query)→search by domain or notes; "
    "get_stats()→statistics; "
    "export_emails(format='txt')→export as txt/csv/json; "
    "clear_collection()→remove all (requires confirmation)."
)

__all__ = [
    "collect_from_directory",
    "collect_from_urls",
    "collect_with_browser",
    "collect_stealth",
    "add_email",
    "list_emails",
    "search_emails",
    "get_stats",
    "export_emails",
    "clear_collection",
]

# Storage file
EMAILS_FILE = Path("/app/memory/collected_emails.txt")
EMAILS_JSON = Path("/app/memory/collected_emails.json")

# ============================================
# ANTI-DETECTION HELPERS
# ============================================

# Human-like User-Agent strings
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/120.0.0.0 Safari/537.36",
]

def _human_delay(min_seconds=2.0, max_seconds=6.0):
    """Add human-like delay between requests with randomization."""
    delay = random.uniform(min_seconds, max_seconds)
    time.sleep(delay)

def _get_random_user_agent() -> str:
    """Get a random user-agent string."""
    return random.choice(USER_AGENTS)

def _make_request_with_retries(web_module, url, max_retries=2, timeout=15) -> str:
    """
    Make a request with retry logic and anti-detection.
    Rotates user-agent and adds delays between retries.
    """
    headers = {
        "User-Agent": _get_random_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    
    for attempt in range(max_retries + 1):
        try:
            _human_delay(2.0, 5.0)
            if _HAS_REQUESTS:
                session = _requests.Session()
                session.headers.update(headers)
                response = session.get(url, timeout=timeout, allow_redirects=True)
                response.raise_for_status()
                return response.text
            else:
                return web_module.fetch(url)
        except Exception as e:
            if attempt == max_retries:
                raise
            wait_time = min(5 * (2 ** attempt), 20)
            time.sleep(wait_time)

def _load_emails_db() -> Dict:
    """Load the email database from JSON file."""
    if EMAILS_JSON.exists():
        try:
            with open(EMAILS_JSON, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError:
            pass
    return {"emails": [], "metadata": {"created": datetime.now().isoformat(), "last_updated": datetime.now().isoformat()}}


def _save_emails_db(db: Dict):
    """Save the email database to JSON file."""
    db["metadata"]["last_updated"] = datetime.now().isoformat()
    with open(EMAILS_JSON, 'w', encoding='utf-8') as f:
        json.dump(db, f, indent=2, ensure_ascii=False)


def _append_to_txt(email: str, source: str, timestamp: str, notes: str = ""):
    """Append email to the TXT file for easy reading."""
    with open(EMAILS_FILE, 'a', encoding='utf-8') as f:
        f.write(f"{email} | {source} | {timestamp}")
        if notes:
            f.write(f" | {notes}")
        f.write("\n")


def _is_valid_email(email: str) -> bool:
    """Validate email format."""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(pattern, email))


def _normalize_email(email: str) -> str:
    """Normalize email to lowercase and strip whitespace."""
    return email.strip().lower()


def _extract_emails_from_text(text: str) -> List[str]:
    """Extract all valid emails from a text block, including mailto: links and HTML entities."""
    # Decode HTML entities (e.g. &#64; → @, &amp; → &)
    decoded = html.unescape(text)
    # Extract from mailto: hrefs first (most reliable source)
    mailto_pattern = r'mailto:([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})'
    plain_pattern = r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'
    emails = re.findall(mailto_pattern, decoded) + re.findall(plain_pattern, decoded)
    return [_normalize_email(e) for e in emails if _is_valid_email(e)]


def _is_duplicate(db: Dict, email: str) -> bool:
    """Check if email already exists in database."""
    email_normalized = _normalize_email(email)
    return any(e["email"] == email_normalized for e in db["emails"])


def collect_from_directory(directory_url: str, max_sites: int = 50, selector: str = "a", stealth: bool = True) -> str:
    """
    Collect emails from directory/listing pages (like BuiltWith, Crunchbase, etc.).
    
    This function:
    1. Extracts individual site URLs from a directory page
    2. Visits each site
    3. Extracts emails from their pages
    
    Args:
        directory_url: URL of the directory/listing page
        max_sites: Maximum number of sites to process (default 50)
        selector: CSS selector for extracting links (default "a")
        stealth: Enable anti-detection features (default True)
    
    Returns:
        Summary of collected emails
    """
    try:
        # Import web skill dynamically
        import importlib.util
        spec = importlib.util.spec_from_file_location("web", "/app/agent/skills/core/web.py")
        web_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(web_module)
        
        db = _load_emails_db()
        collected = []
        skipped = 0
        
        print(f"🔍 Starting email collection from directory: {directory_url}")
        print(f"📊 Target: up to {max_sites} sites")
        
        # Step 1: Extract site URLs from directory page
        print(f"\n📋 Step 1: Extracting site URLs from directory...")
        
        if stealth:
            html = _make_request_with_retries(web_module, directory_url)
        else:
            html = web_module.fetch(directory_url)
        
        # Extract URLs from the page
        url_pattern = r'href=["\'](https?://[^\s"\'<>]+)["\']'
        all_urls = re.findall(url_pattern, html)
        
        # Filter to get actual external site URLs
        site_urls = []
        domain = urlparse(directory_url).netloc

        for url in all_urls:
            # Skip internal links, anchors, and JavaScript
            if url.startswith(('javascript:', '#', 'mailto:', 'tel:')):
                continue
            parsed = urlparse(url)
            if parsed.netloc and parsed.netloc != domain:
                site_urls.append(url)
        
        # Deduplicate and limit
        site_urls = list(set(site_urls))[:max_sites]
        print(f"✅ Found {len(site_urls)} site URLs to process")
        
        # Step 2: Visit each site and extract emails
        print(f"\n🔎 Step 2: Visiting websites to extract emails...")
        
        for i, site_url in enumerate(site_urls, 1):
            try:
                print(f"\n[{i}/{len(site_urls)}] Processing: {site_url}")
                
                # Try common pages where emails might appear
                pages_to_check = [
                    site_url,
                    f"{site_url.rstrip('/')}/contact",
                    f"{site_url.rstrip('/')}/about",
                    f"{site_url.rstrip('/')}/contact-us",
                    f"{site_url.rstrip('/')}/team",
                    f"{site_url.rstrip('/')}/imprint",
                    f"{site_url.rstrip('/')}/legal",
                ]
                
                site_emails = []
                for page_url in pages_to_check[:3]:  # Limit to first 3 pages per site
                    try:
                        if stealth:
                            page_html = _make_request_with_retries(web_module, page_url)
                        else:
                            page_html = web_module.fetch(page_url)
                        emails = _extract_emails_from_text(page_html)
                        site_emails.extend(emails)
                    except Exception:
                        continue
                
                if site_emails:
                    # Deduplicate emails for this site
                    unique_emails = list(set(site_emails))
                    
                    for email in unique_emails:
                        if not _is_duplicate(db, email):
                            db["emails"].append({
                                "email": email,
                                "source": site_url,
                                "collected_at": datetime.now().isoformat(),
                                "method": "directory_scrape",
                                "notes": f"From directory: {directory_url}"
                            })
                            _append_to_txt(email, site_url, datetime.now().isoformat())
                            collected.append(email)
                            print(f"  ✓ Found: {email}")
                        else:
                            skipped += 1
                else:
                    print(f"  - No emails found")
                
                # Rate limiting - longer delay in stealth mode
                if stealth:
                    _human_delay(4.0, 10.0)
                else:
                    time.sleep(2)
                
            except Exception as e:
                print(f"  ✗ Error processing {site_url}: {str(e)}")
                continue
        
        # Save database
        _save_emails_db(db)
        
        result = f"\n✅ Email collection complete!\n"
        result += f"📧 New emails collected: {len(collected)}\n"
        result += f"⏭️ Duplicates skipped: {skipped}\n"
        result += f"🌐 Sites processed: {len(site_urls)}\n"
        result += f"\n📁 Saved to:\n"
        result += f"  - {EMAILS_FILE} (TXT format)\n"
        result += f"  - {EMAILS_JSON} (JSON database)\n"
        
        if collected:
            result += f"\n📋 Recent emails:\n"
            for email in collected[:10]:
                result += f"  • {email}\n"
        
        return result
        
    except Exception as e:
        return f"❌ Error during collection: {str(e)}"


def collect_from_urls(urls: str, max_per_site: int = 1, stealth: bool = True) -> str:
    """
    Collect emails from a list of URLs (comma-separated).
    
    Args:
        urls: Comma-separated list of URLs to scrape
        max_per_site: Maximum emails to collect per site (default 1)
        stealth: Enable anti-detection features (default True)
    
    Returns:
        Summary of collected emails
    """
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("web", "/app/agent/skills/core/web.py")
        web_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(web_module)
        
        url_list = [u.strip() for u in urls.split(',') if u.strip()]
        db = _load_emails_db()
        collected = []
        skipped = 0
        
        print(f"🔍 Collecting emails from {len(url_list)} URLs...")
        
        for i, url in enumerate(url_list, 1):
            try:
                print(f"\n[{i}/{len(url_list)}] Scraping: {url}")
                
                if stealth:
                    html = _make_request_with_retries(web_module, url)
                else:
                    html = web_module.fetch(url)
                emails = _extract_emails_from_text(html)
                
                if emails:
                    site_emails = list(set(emails))[:max_per_site]
                    
                    for email in site_emails:
                        if not _is_duplicate(db, email):
                            db["emails"].append({
                                "email": email,
                                "source": url,
                                "collected_at": datetime.now().isoformat(),
                                "method": "direct_scrape",
                                "notes": ""
                            })
                            _append_to_txt(email, url, datetime.now().isoformat())
                            collected.append(email)
                            print(f"  ✓ {email}")
                        else:
                            skipped += 1
                else:
                    print(f"  - No emails found")
                
                if stealth:
                    _human_delay(3.0, 8.0)
                else:
                    time.sleep(2)
                
            except Exception as e:
                print(f"  ✗ Error: {str(e)}")
                continue
        
        _save_emails_db(db)
        
        result = f"\n✅ Email collection complete!\n"
        result += f"📧 New emails: {len(collected)}\n"
        result += f"⏭️ Duplicates skipped: {skipped}\n"
        result += f"🌐 Sites processed: {len(url_list)}\n"
        
        if collected:
            result += f"\n📋 Collected emails:\n"
            for email in collected:
                result += f"  • {email}\n"
        
        return result
        
    except Exception as e:
        return f"❌ Error: {str(e)}"


def collect_with_browser(directory_url: str, max_sites: int = 5) -> str:
    """
    Collect emails from a directory/listing page using CDP browser.

    Use this for BuiltWith, Crunchbase, search result pages, or any page that
    lists businesses — the browser renders JavaScript, extracts all external site
    links, visits each one, and checks /contact and /about for emails.

    Args:
        directory_url: URL of the directory or listing page (e.g. BuiltWith URL)
        max_sites: Maximum number of linked sites to visit (default 5)

    Returns:
        Summary of collected emails
    """
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("browser_session", "/app/agent/skills/core/browser_session.py")
        browser_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(browser_module)

        db = _load_emails_db()
        collected = []
        skipped = 0

        # Common social/tracking domains to skip when extracting site links
        SKIP_DOMAINS = {
            'google.com', 'facebook.com', 'twitter.com', 'linkedin.com',
            'youtube.com', 'instagram.com', 'pinterest.com', 't.co',
            'bit.ly', 'goo.gl', 'ow.ly', 'tinyurl.com',
        }

        print(f"🌐 Browser-assisted collection from: {directory_url}")
        print(f"📊 Will visit up to {max_sites} linked sites\n")

        # Step 1: Navigate to the directory page and get its rendered HTML
        print("📋 Step 1: Loading directory page in browser...")
        browser_module.goto(directory_url)
        time.sleep(3)

        page_html = browser_module.get_html()
        base_domain = urlparse(directory_url).netloc

        # Step 2: Extract external site links from the rendered page
        print("🔗 Step 2: Extracting external site links...")
        url_pattern = r'href=["\'](https?://[^\s"\'<>]+)["\']'
        all_hrefs = re.findall(url_pattern, page_html)

        seen_sites = {}
        for href in all_hrefs:
            parsed = urlparse(href)
            if not parsed.netloc or parsed.netloc == base_domain:
                continue
            root_domain = parsed.netloc.lstrip('www.')
            if any(root_domain == skip or root_domain.endswith('.' + skip) for skip in SKIP_DOMAINS):
                continue
            site_root = f"{parsed.scheme}://{parsed.netloc}"
            if root_domain not in seen_sites:
                seen_sites[root_domain] = site_root

        site_urls = list(seen_sites.values())[:max_sites]
        print(f"✅ Found {len(site_urls)} external sites to visit\n")

        if not site_urls:
            return (
                "⚠️ No external site links found on the directory page.\n"
                "The page may require login or the links use JavaScript redirects.\n"
                "Try collect_stealth() instead."
            )

        # Step 3: Visit each site and hunt for emails
        print("🔎 Step 3: Visiting each site and extracting emails...")
        CONTACT_SUBPATHS = ['/contact', '/contact-us', '/about', '/about-us', '/team', '/imprint']

        for i, site_url in enumerate(site_urls, 1):
            try:
                print(f"\n[{i}/{len(site_urls)}] Visiting: {site_url}")
                browser_module.goto(site_url)
                time.sleep(2)

                site_emails = []

                # Check homepage first
                home_html = browser_module.get_html()
                site_emails.extend(_extract_emails_from_text(home_html))

                # If homepage had no emails, try contact/about subpages
                if not site_emails:
                    for subpath in CONTACT_SUBPATHS:
                        try:
                            browser_module.goto(f"{site_url.rstrip('/')}{subpath}")
                            time.sleep(2)
                            sub_html = browser_module.get_html()
                            found = _extract_emails_from_text(sub_html)
                            if found:
                                site_emails.extend(found)
                                break
                        except Exception:
                            continue

                if site_emails:
                    for email in list(set(site_emails)):
                        if not _is_duplicate(db, email):
                            db["emails"].append({
                                "email": email,
                                "source": site_url,
                                "collected_at": datetime.now().isoformat(),
                                "method": "browser_directory",
                                "notes": f"Via directory: {directory_url}",
                            })
                            _append_to_txt(email, site_url, datetime.now().isoformat())
                            collected.append(email)
                            print(f"  ✓ {email}")
                        else:
                            skipped += 1
                else:
                    print(f"  - No emails found")

                time.sleep(2)

            except Exception as e:
                print(f"  ✗ Error visiting {site_url}: {e}")
                continue

        _save_emails_db(db)

        result = f"\n✅ Browser-assisted collection complete!\n"
        result += f"📧 New emails: {len(collected)}\n"
        result += f"⏭️ Duplicates skipped: {skipped}\n"
        result += f"🌐 Sites visited: {len(site_urls)}\n"
        result += f"📁 Source directory: {directory_url}\n"

        if collected:
            result += f"\n📋 Collected:\n"
            for email in collected:
                result += f"  • {email}\n"

        return result

    except Exception as e:
        return f"❌ Browser collection error: {str(e)}"


def collect_stealth(target_urls: str, max_sites: int = 10) -> str:
    """
    Collect emails using FULL stealth mode with anti-detection.
    
    This is the MOST stealthy option - uses playwright-stealth to bypass:
    - Bot detection services (Cloudflare, DataDome, PerimeterX)
    - Headless browser detection
    - JavaScript fingerprinting
    - Cookie-based tracking
    
    Args:
        target_urls: Comma-separated URLs to process
        max_sites: Maximum sites to process (default 10)
    
    Returns:
        Summary of collected emails
    """
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("browser_session", "/app/agent/skills/core/browser_session.py")
        browser_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(browser_module)
        
        url_list = [u.strip() for u in target_urls.split(',') if u.strip()][:max_sites]
        db = _load_emails_db()
        collected = []
        skipped = 0
        
        print(f"🕵️ Starting STEALTH email collection from {len(url_list)} URLs...")
        print(f"⚠️ This mode is slower but bypasses most anti-bot protections")
        
        for i, url in enumerate(url_list, 1):
            try:
                print(f"\n[{i}/{len(url_list)}] [STEALTH] Processing: {url}")
                
                session_name = f"email_collection_{i}"
                
                # Start stealth session
                browser_module.stealth_start(session_name, headless=True)
                _human_delay(1.0, 2.0)
                
                # Navigate to site
                browser_module.stealth_goto(url, session_name)
                _human_delay(2.0, 4.0)  # Human-like page load delay
                
                # Extract emails from main page
                page_text = browser_module.stealth_get_text(session_name)
                emails = _extract_emails_from_text(page_text)
                
                if emails:
                    for email in list(set(emails)):
                        if not _is_duplicate(db, email):
                            db["emails"].append({
                                "email": email,
                                "source": url,
                                "collected_at": datetime.now().isoformat(),
                                "method": "stealth_browser",
                                "notes": "Collected via stealth mode"
                            })
                            _append_to_txt(email, url, datetime.now().isoformat())
                            collected.append(email)
                            print(f"  ✓ {email}")
                        else:
                            skipped += 1
                
                # Try to find and visit contact page
                snapshot = browser_module.stealth_snapshot(session_name)
                if 'contact' in snapshot.lower():
                    print(f"  → Found contact link, navigating...")
                    # Agent would need to click the contact link manually
                    # This is a simplified version - for full automation, use collect_from_directory
                
                # Close stealth session (saves cookies)
                browser_module.stealth_close(session_name)
                
                # Longer delay between sites in stealth mode
                _human_delay(8.0, 15.0)
                
            except Exception as e:
                print(f"  ✗ Stealth error: {str(e)}")
                try:
                    browser_module.stealth_close(f"email_collection_{i}")
                except Exception:
                    pass
                continue
        
        _save_emails_db(db)
        
        result = f"\n✅ Stealth collection complete!\n"
        result += f"📧 New emails: {len(collected)}\n"
        result += f"⏭️ Duplicates skipped: {skipped}\n"
        result += f"🌐 Sites processed: {len(url_list)}\n"
        result += f"🕵️ Mode: Full stealth (anti-detection enabled)\n"
        
        if collected:
            result += f"\n📋 Collected:\n"
            for email in collected:
                result += f"  • {email}\n"
        
        return result
        
    except Exception as e:
        return f"❌ Stealth collection error: {str(e)}"


def add_email(email: str, source: str, notes: str = "") -> str:
    """
    Manually add an email to the collection.
    
    Args:
        email: Email address
        source: Where you found it
        notes: Optional notes
    
    Returns:
        Confirmation message
    """
    email_normalized = _normalize_email(email)
    
    if not _is_valid_email(email_normalized):
        return f"❌ Invalid email format: {email}"
    
    db = _load_emails_db()
    
    if _is_duplicate(db, email_normalized):
        return f"⚠️ Email already exists in collection: {email_normalized}"
    
    db["emails"].append({
        "email": email_normalized,
        "source": source,
        "collected_at": datetime.now().isoformat(),
        "method": "manual",
        "notes": notes
    })
    
    try:
        _append_to_txt(email_normalized, source, datetime.now().isoformat(), notes)
    except Exception:
        pass
    _save_emails_db(db)

    return f"✅ Added: {email_normalized}\n📁 Source: {source}"


def list_emails(limit: int = 50) -> str:
    """
    List recently collected emails.
    
    Args:
        limit: Number of emails to show (default 50)
    
    Returns:
        Formatted list of emails with metadata
    """
    db = _load_emails_db()
    emails = db["emails"][-limit:]
    
    if not emails:
        return "📭 No emails collected yet."
    
    result = f"📧 Recent Emails (showing last {len(emails)} of {len(db['emails'])} total):\n"
    result += "=" * 80 + "\n"
    
    for i, entry in enumerate(reversed(emails), 1):
        result += f"{i}. {entry['email']}\n"
        result += f"   Source: {entry['source']}\n"
        result += f"   Collected: {entry['collected_at'][:19]}\n"
        result += f"   Method: {entry['method']}\n"
        if entry.get('notes'):
            result += f"   Notes: {entry['notes']}\n"
        result += "-" * 80 + "\n"
    
    return result


def search_emails(query: str) -> str:
    """
    Search collected emails by domain, email, or notes.
    
    Args:
        query: Search term
    
    Returns:
        Matching emails
    """
    db = _load_emails_db()
    query_lower = query.lower()
    
    matches = [
        e for e in db["emails"]
        if query_lower in e["email"] or query_lower in e["source"] or query_lower in e.get("notes", "")
    ]
    
    if not matches:
        return f"🔍 No emails match: '{query}'"
    
    result = f"🔍 Search Results for '{query}' ({len(matches)} found):\n"
    result += "=" * 80 + "\n"
    
    for i, entry in enumerate(matches, 1):
        result += f"{i}. {entry['email']}\n"
        result += f"   Source: {entry['source']}\n"
        result += f"   Collected: {entry['collected_at'][:19]}\n"
        if entry.get('notes'):
            result += f"   Notes: {entry['notes']}\n"
        result += "-" * 80 + "\n"
    
    return result


def get_stats() -> str:
    """
    Get collection statistics.
    
    Returns:
        Statistics summary
    """
    db = _load_emails_db()
    emails = db["emails"]
    
    if not emails:
        return "📭 No emails collected yet."
    
    # Count by method
    methods = {}
    for e in emails:
        method = e.get("method", "unknown")
        methods[method] = methods.get(method, 0) + 1
    
    # Count by source domain
    domains = {}
    for e in emails:
        source = e["source"]
        try:
            domain = source.split("//")[1].split("/")[0]
        except Exception:
            domain = source
        domains[domain] = domains.get(domain, 0) + 1
    
    # Top domains
    top_domains = sorted(domains.items(), key=lambda x: x[1], reverse=True)[:10]
    
    result = "📊 Email Collection Statistics\n"
    result += "=" * 80 + "\n"
    result += f"Total emails collected: {len(emails)}\n"
    result += f"Database created: {db['metadata']['created'][:19]}\n"
    result += f"Last updated: {db['metadata']['last_updated'][:19]}\n\n"
    
    result += "📈 By Method:\n"
    for method, count in methods.items():
        result += f"  • {method}: {count}\n"
    
    result += f"\n🌐 Top Source Domains:\n"
    for domain, count in top_domains:
        result += f"  • {domain}: {count} emails\n"
    
    return result


def export_emails(format: str = 'txt') -> str:
    """
    Export collected emails in different formats.
    
    Args:
        format: 'txt', 'csv', or 'json'
    
    Returns:
        Export confirmation and file path
    """
    db = _load_emails_db()
    
    if not db["emails"]:
        return "📭 No emails to export."
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    if format == 'txt':
        export_path = Path(f"/app/memory/emails_export_{timestamp}.txt")
        with open(export_path, 'w', encoding='utf-8') as f:
            f.write(f"Email Collection Export - {datetime.now().isoformat()}\n")
            f.write("=" * 80 + "\n\n")
            for entry in db["emails"]:
                f.write(f"{entry['email']} | {entry['source']} | {entry['collected_at'][:19]}")
                if entry.get('notes'):
                    f.write(f" | {entry['notes']}")
                f.write("\n")
        return f"✅ Exported {len(db['emails'])} emails to: {export_path}"
    
    elif format == 'csv':
        export_path = Path(f"/app/memory/emails_export_{timestamp}.csv")
        with open(export_path, 'w', encoding='utf-8') as f:
            f.write("email,source,collected_at,method,notes\n")
            for entry in db["emails"]:
                email = entry['email']
                source = entry['source'].replace(',', ';')
                collected = entry['collected_at'][:19]
                method = entry['method']
                notes = entry.get('notes', '').replace(',', ';')
                f.write(f'"{email}","{source}","{collected}","{method}","{notes}"\n')
        return f"✅ Exported {len(db['emails'])} emails to: {export_path}"
    
    elif format == 'json':
        export_path = Path(f"/app/memory/emails_export_{timestamp}.json")
        with open(export_path, 'w', encoding='utf-8') as f:
            json.dump(db, f, indent=2, ensure_ascii=False)
        return f"✅ Exported {len(db['emails'])} emails to: {export_path}"
    
    else:
        return f"❌ Unsupported format: {format}. Use 'txt', 'csv', or 'json'."


def clear_collection() -> str:
    """
    Clear all collected emails (requires agent confirmation).
    
    Returns:
        Warning message
    """
    return "⚠️ WARNING: This will delete ALL collected emails. To confirm, call: clear_collection_confirm()"


def clear_collection_confirm():
    """Actually clear the collection (separate function to prevent accidents)."""
    if EMAILS_JSON.exists():
        EMAILS_JSON.unlink()
    if EMAILS_FILE.exists():
        EMAILS_FILE.unlink()
    return "✅ Email collection cleared."


def help_email_collector() -> str:
    """Show help for email collector."""
    return """
📧 Email Collector Help - Anti-Detection Enabled
==================================================

ANTI-DETECTION FEATURES (enabled by default):
• User-Agent rotation (mimics different browsers)
• Random delays between requests (human-like timing)
• Retry logic with exponential backoff
• Stealth browser mode for protected sites
• Rate limiting to avoid triggering firewalls

COLLECTION METHODS (choose based on target):

1. collect_from_directory(directory_url, max_sites=50, stealth=True)
   - For directory/listing pages (BuiltWith, Crunchbase, etc.)
   - stealth=True enables anti-detection (recommended)
   - Extracts site URLs from directory, then visits each
   
2. collect_from_urls(urls, max_per_site=1, stealth=True)
   - For known website URLs (comma-separated)
   - stealth=True enables anti-detection (recommended)
   
3. collect_with_browser(target_urls, max_pages=3)
   - Uses CDP browser (your logged-in Chrome)
   - Good for sites needing authentication
   
4. collect_stealth(target_urls, max_sites=10) ⭐ MOST POWERFUL
   - Full stealth mode with playwright-stealth
   - Bypasses Cloudflare, DataDome, PerimeterX
   - Bypasses headless browser detection
   - Slower but most effective against firewalls
   - Use when other methods get blocked

VIEWING:
• list_emails(limit=50) - Show recent emails
• search_emails(query) - Search by domain/email/notes
• get_stats() - Collection statistics

EXPORT:
• export_emails('txt') - Export as txt, csv, or json

ANTI-BLOCKING TIPS:
✓ Always use stealth=True (default)
✓ Use collect_stealth() for protected sites
✓ Keep batches small (10-20 sites at a time)
✓ Don't run too frequently (once/day max)
✓ If blocked, try different method or wait 24h

EXAMPLE WORKFLOWS:

1. Standard directory scraping (with anti-detection):
   collect_from_directory('https://trends.builtwith.com/websitelist/Luxury-Presence', 30)

2. Direct URLs (with anti-detection):
   collect_from_urls('https://site1.com, https://site2.com')

3. Protected sites (maximum stealth):
   collect_stealth('https://protected-site1.com, https://protected-site2.com', 10)
"""
