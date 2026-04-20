import asyncio
import hashlib
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional

import aiohttp

NAME = "url_monitor"
SHORT_DOC = "Monitor URLs for uptime, content changes, and Twitter automation state."
DOC = (
    "Monitor URLs for changes and health status. "
    "Functions: add_url(url, friendly_name?, frequency_minutes?, tags?, alert_on_changes?, verify_ssl?) — tags can be list or plain string; "
    "list_urls()→formatted status of all monitored URLs (preferred over get_status_summary/get_url_history/get_recent_changes); "
    "remove_url(url), check_url_now(url), check_all_urls(). "
    "Twitter state helpers for stateful automation (deduplication across scheduler runs): "
    "tw_is_seen(key)→bool — has this tweet_id/username already been liked/followed?; "
    "tw_mark_seen(key, value?)→mark as processed; "
    "tw_last_tweet_time()→ISO timestamp of last posted tweet, empty if never; "
    "tw_log_tweet()→record that a tweet was just posted (call right after browser_session.tweet()); "
    "tw_prune(max_age_days?)→remove old twitter_state entries older than N days (default 90)."
)
__all__ = [
    "NAME", "DOC",
    "add_url", "remove_url", "check_url_now", "check_all_urls",
    "list_urls", "get_status_summary", "get_url_history", "get_recent_changes",
    "tw_is_seen", "tw_mark_seen", "tw_last_tweet_time", "tw_log_tweet", "tw_prune",
]


@dataclass
class UrlStatus:
    url: str
    status_code: int
    content_hash: str
    content_length: int
    last_modified: datetime
    check_time: datetime
    response_time: float
    error: Optional[str] = None


class UrlMonitor:
    """Advanced URL monitoring system with change detection and response tracking."""

    def __init__(self, db_path: str = "/app/memory/url_monitor.db"):
        self.db_path = db_path
        self.setup_database()
        self.setup_logging()

    def setup_database(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS url_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                status_code INTEGER,
                content_hash TEXT,
                content_length INTEGER,
                last_modified TEXT,
                check_time TEXT,
                response_time REAL,
                error TEXT,
                change_detected BOOLEAN DEFAULT FALSE
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS url_metadata (
                url TEXT PRIMARY KEY,
                friendly_name TEXT,
                check_frequency_minutes INTEGER DEFAULT 60,
                alert_on_changes BOOLEAN DEFAULT TRUE,
                tags TEXT,
                created_at TEXT,
                last_check TEXT,
                next_check TEXT,
                is_active BOOLEAN DEFAULT TRUE,
                verify_ssl BOOLEAN DEFAULT TRUE
            )
        ''')

        # Migration: add verify_ssl for existing databases
        try:
            cursor.execute('ALTER TABLE url_metadata ADD COLUMN verify_ssl BOOLEAN DEFAULT TRUE')
        except sqlite3.OperationalError:
            pass

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS url_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                old_hash TEXT,
                new_hash TEXT,
                change_type TEXT,
                detected_at TEXT,
                changes_summary TEXT
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS twitter_state (
                key TEXT PRIMARY KEY,
                value TEXT,
                logged_at TEXT
            )
        ''')

        # Index for efficient per-URL latest-check lookups (fix #4)
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_url_checks_url_time
            ON url_checks(url, check_time DESC)
        ''')

        conn.commit()
        conn.close()

    def setup_logging(self):
        log_path = "/app/memory/url_monitor.log"
        logging.basicConfig(
            filename=log_path,
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger('UrlMonitor')

    def add_url(self, url: str, friendly_name: str = "", frequency_minutes: int = 60,
                tags: List[str] = None, alert_on_changes: bool = True,
                frequency: int = None, verify_ssl: bool = True) -> Dict:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        if frequency is not None:
            frequency_minutes = frequency

        try:
            frequency_minutes = int(frequency_minutes)
        except (ValueError, TypeError):
            m = re.search(r'\d+', str(frequency_minutes))
            frequency_minutes = int(m.group()) if m else 60

        # Strip and drop empty/whitespace-only tags (fix #7)
        cleaned_tags = [t.strip() for t in (tags or []) if t and t.strip()]
        tags_str = ",".join(cleaned_tags)

        try:
            cursor.execute('''
                INSERT OR REPLACE INTO url_metadata
                (url, friendly_name, check_frequency_minutes, alert_on_changes,
                 tags, created_at, last_check, next_check, is_active, verify_ssl)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (url, friendly_name, frequency_minutes, alert_on_changes,
                  tags_str, datetime.now().isoformat(), None,
                  (datetime.now() + timedelta(minutes=frequency_minutes)).isoformat(),
                  True, verify_ssl))
            conn.commit()
            conn.close()
            return {"success": True, "message": f"Successfully added {url}"}
        except Exception as e:
            conn.close()
            return {"success": False, "error": str(e)}

    async def _fetch(self, session: aiohttp.ClientSession, url: str,
                     headers: dict, ssl_ctx) -> tuple:
        """Execute one GET; returns (status_code, headers_dict, content_bytes)."""
        async with session.get(url, headers=headers, ssl=ssl_ctx) as response:
            content = await response.read()
            return response.status, dict(response.headers), content

    async def check_url(self, url: str, verify_ssl: bool = True) -> UrlStatus:
        """Check a single URL and return all status information."""
        headers = {'User-Agent': 'TrinityClaw-UrlMonitor/1.0'}
        ssl_ctx = None if verify_ssl else False  # fix #13
        start_time = time.time()

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                status_code, resp_headers, content = await self._fetch(
                    session, url, headers, ssl_ctx
                )

                # Retry once on 429, honouring Retry-After (fix #12)
                if status_code == 429:
                    retry_after = min(int(resp_headers.get('Retry-After', '10')), 60)
                    self.logger.info(f"429 on {url}, retrying after {retry_after}s")
                    await asyncio.sleep(retry_after)
                    status_code, resp_headers, content = await self._fetch(
                        session, url, headers, ssl_ctx
                    )

                content_hash = hashlib.sha256(content).hexdigest()

                last_modified = None
                if 'Last-Modified' in resp_headers:
                    try:
                        last_modified = parsedate_to_datetime(resp_headers['Last-Modified'])
                    except Exception:
                        last_modified = datetime.now()

                return UrlStatus(
                    url=url,
                    status_code=status_code,
                    content_hash=content_hash,
                    content_length=len(content),
                    last_modified=last_modified or datetime.now(),
                    check_time=datetime.now(),
                    response_time=time.time() - start_time,
                )

        except Exception as e:
            return UrlStatus(
                url=url,
                status_code=0,
                content_hash="",
                content_length=0,
                last_modified=datetime.now(),
                check_time=datetime.now(),
                response_time=time.time() - start_time,
                error=str(e),
            )

    def save_check_result(self, status: UrlStatus) -> bool:
        """Save check result to database. Returns True if a change was detected."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Read this URL's configured frequency so next_check is correct (fix #1)
        cursor.execute(
            'SELECT check_frequency_minutes, alert_on_changes, friendly_name FROM url_metadata WHERE url = ?',
            (status.url,)
        )
        meta = cursor.fetchone()
        freq_minutes = meta[0] if meta else 60
        alert_on_changes = meta[1] if meta else False
        friendly_name = (meta[2] if meta else None) or status.url

        # Detect content changes
        cursor.execute(
            'SELECT content_hash, content_length FROM url_checks WHERE url = ? ORDER BY check_time DESC LIMIT 1',
            (status.url,)
        )
        last_result = cursor.fetchone()
        change_detected = False

        if last_result:
            old_hash, old_length = last_result
            if status.content_hash and old_hash != status.content_hash:
                change_detected = True
                # Populate changes_summary with a human-readable diff summary (fix #14)
                length_delta = status.content_length - (old_length or 0)
                changes_summary = (
                    f"Content changed; length delta: {length_delta:+d} bytes "
                    f"({old_length or 0} → {status.content_length})"
                )
                cursor.execute('''
                    INSERT INTO url_changes (url, old_hash, new_hash, change_type, detected_at, changes_summary)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (status.url, old_hash, status.content_hash, "content_changed",
                      datetime.now().isoformat(), changes_summary))

                # Honour alert_on_changes flag (fix #11)
                if alert_on_changes:
                    self.logger.warning(
                        f"ALERT change detected on {friendly_name}: {changes_summary}"
                    )

        cursor.execute('''
            INSERT INTO url_checks
            (url, status_code, content_hash, content_length, last_modified,
             check_time, response_time, error, change_detected)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (status.url, status.status_code, status.content_hash, status.content_length,
              status.last_modified.isoformat(), status.check_time.isoformat(),
              status.response_time, status.error, change_detected))

        # Use the URL's own frequency for next_check (fix #1)
        cursor.execute('''
            UPDATE url_metadata SET last_check = ?, next_check = ? WHERE url = ?
        ''', (
            status.check_time.isoformat(),
            (datetime.now() + timedelta(minutes=freq_minutes)).isoformat(),
            status.url,
        ))

        conn.commit()
        conn.close()
        return change_detected

    async def check_all_urls(self) -> List[Dict]:
        """Monitor all active URLs concurrently."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT url, friendly_name, verify_ssl FROM url_metadata
            WHERE is_active = 1 AND next_check <= ?
        ''', (datetime.now().isoformat(),))
        urls = cursor.fetchall()
        conn.close()

        if not urls:
            return []

        # Check all URLs concurrently instead of sequentially (fix #6)
        statuses = await asyncio.gather(
            *[self.check_url(url, verify_ssl=bool(verify_ssl)) for url, _, verify_ssl in urls]
        )

        results = []
        for (url, name, _), status in zip(urls, statuses):
            change_detected = self.save_check_result(status)
            results.append({
                "url": url,
                "name": name,
                "status_code": status.status_code,
                "response_time": status.response_time,
                "change_detected": change_detected,
                "error": status.error,
            })
        return results

    def get_url_history(self, url: str, limit: int = 10) -> List[Dict]:
        """Get monitoring history for a specific URL."""
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 10

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # access by column name, not position (fix #8)
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, url, status_code, content_hash, content_length, last_modified,
                   check_time, response_time, error, change_detected
            FROM url_checks
            WHERE url = ?
            ORDER BY check_time DESC LIMIT ?
        ''', (url, limit))

        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_status_summary(self) -> Dict:
        """Get comprehensive status summary."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute('SELECT COUNT(*) FROM url_metadata WHERE is_active = 1')
        total_urls = cursor.fetchone()[0]

        cursor.execute('''
            SELECT COUNT(*) FROM url_checks
            WHERE check_time > datetime('now', '-1 day') AND status_code < 400
        ''')
        recent_success = cursor.fetchone()[0]

        # Fixed: parens ensure OR doesn't escape the 24-hour time window (fix #2)
        cursor.execute('''
            SELECT COUNT(*) FROM url_checks
            WHERE check_time > datetime('now', '-1 day')
              AND (status_code >= 400 OR error IS NOT NULL)
        ''')
        recent_failures = cursor.fetchone()[0]

        cursor.execute('''
            SELECT COUNT(*) FROM url_changes
            WHERE detected_at > datetime('now', '-1 day')
        ''')
        recent_changes = cursor.fetchone()[0]

        cursor.execute('''
            SELECT AVG(response_time) FROM url_checks
            WHERE check_time > datetime('now', '-1 day')
        ''')
        avg_response_time = cursor.fetchone()[0] or 0.0  # fix #16

        # MAX(id) as tiebreaker prevents duplicate rows when timestamps collide (fix #3)
        cursor.execute('''
            SELECT url_checks.url, url_checks.status_code,
                   url_checks.check_time, url_checks.error
            FROM url_checks
            WHERE url_checks.id IN (
                SELECT MAX(id) FROM url_checks
                WHERE check_time > datetime('now', '-1 day')
                GROUP BY url
            )
        ''')
        latest_checks = cursor.fetchall()
        conn.close()

        return {
            "total_monitored": total_urls,
            "recent_success": recent_success,
            "recent_failures": recent_failures,
            "recent_changes": recent_changes,
            "avg_response_time": avg_response_time,
            "latest_checks": [
                {"url": row[0], "status": row[1], "checked": row[2], "error": row[3]}
                for row in latest_checks
            ],
        }

    def get_recent_changes(self, limit: int = 10) -> List[Dict]:
        """Get recent URL changes."""
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 10

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, url, old_hash, new_hash, change_type, detected_at, changes_summary
            FROM url_changes
            ORDER BY detected_at DESC LIMIT ?
        ''', (limit,))

        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def remove_url(self, url: str) -> Dict:
        """Remove a URL from monitoring."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('UPDATE url_metadata SET is_active = 0 WHERE url = ?', (url,))
        conn.commit()
        conn.close()
        return {"success": True, "message": f"Removed {url} from monitoring"}


# Create global instance
url_monitor_instance = UrlMonitor()


# ── Skill interface functions ─────────────────────────────────────────────────

def add_url(url: str, friendly_name: str = "", frequency_minutes: int = 60,
            tags=None, alert_on_changes: bool = True,
            frequency=None, verify_ssl: bool = True) -> Dict:
    """Add a new URL to monitor."""
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    return url_monitor_instance.add_url(
        url, friendly_name, frequency_minutes, tags, alert_on_changes,
        frequency=frequency, verify_ssl=verify_ssl,
    )


def get_status_summary() -> Dict:
    try:
        return url_monitor_instance.get_status_summary()
    except Exception as e:
        return {"error": str(e)}


def get_url_history(url: str, limit: int = 10) -> List[Dict]:
    try:
        return url_monitor_instance.get_url_history(url, int(limit))
    except Exception as e:
        return [{"error": str(e)}]


def get_recent_changes(limit: int = 10) -> List[Dict]:
    try:
        return url_monitor_instance.get_recent_changes(int(limit))
    except Exception as e:
        return [{"error": str(e)}]


def list_urls() -> str:
    """List all monitored URLs with their latest status. Preferred over get_status_summary."""
    try:
        conn = sqlite3.connect(url_monitor_instance.db_path)
        cursor = conn.cursor()
        # CTE avoids correlated per-row subquery; index on (url, check_time) makes it fast (fix #4)
        cursor.execute('''
            WITH latest AS (
                SELECT url, MAX(id) AS max_id FROM url_checks GROUP BY url
            )
            SELECT m.url, m.friendly_name, m.tags, m.check_frequency_minutes,
                   m.last_check, m.is_active,
                   c.status_code, c.response_time, c.error
            FROM url_metadata m
            LEFT JOIN latest l ON l.url = m.url
            LEFT JOIN url_checks c ON c.id = l.max_id
            ORDER BY m.created_at DESC
        ''')
        rows = cursor.fetchall()
        conn.close()
        if not rows:
            return "📭 No URLs being monitored."
        lines = [f"🔍 Monitored URLs ({len(rows)}):"]
        for r in rows:
            url, name, tags, freq, last_check, active, status_code, resp_time, error = r
            if status_code and int(status_code) < 400:
                icon = "✅"
            elif status_code:
                icon = "❌"
            else:
                icon = "—"
            line = f"\n  {icon} {url}" + (f"  ({name})" if name else "")
            line += f"\n     status: {status_code or 'never checked'}"
            if resp_time:
                line += f"  |  resp: {float(resp_time):.2f}s"
            line += f"  |  freq: {freq}min  |  {'active' if active else 'paused'}"
            if tags:
                line += f"\n     tags: {tags}"
            if last_check:
                line += f"\n     last check: {last_check}"
            if error:
                line += f"\n     error: {error}"
            lines.append(line)
        return "\n".join(lines)
    except Exception as e:
        return f"❌ list_urls error: {e}"


async def check_all_urls() -> List[Dict]:
    return await url_monitor_instance.check_all_urls()


def remove_url(url: str) -> Dict:
    return url_monitor_instance.remove_url(url)


def check_url_now(url: str) -> Dict:
    """Check a single URL immediately."""
    async def _check():
        status = await url_monitor_instance.check_url(url)
        return {
            "url": url,
            "status_code": status.status_code,
            "response_time": status.response_time,
            "content_length": status.content_length,
            "content_hash": status.content_hash,
            "error": status.error,
        }

    return asyncio.run(_check())


# ── Twitter State Helpers ─────────────────────────────────────────────────────
# Lightweight SQLite key/value store for deduplication in scheduler-driven
# Twitter automation. Stored in url_monitor.db (twitter_state table).

_TW_DB = url_monitor_instance.db_path


def tw_is_seen(key: str) -> bool:
    """Check if a tweet_id or username has already been processed (liked, followed, etc.).

    key — a unique identifier, e.g. tweet URL, tweet ID, or @username.
    Returns True if already seen, False if new.

    Use this before like_tweet() or follow_user() to prevent double-actions:
      if not url_monitor.tw_is_seen(tweet_url):
          browser_session.like_tweet(tweet_url)
          url_monitor.tw_mark_seen(tweet_url, "liked")
    """
    conn = sqlite3.connect(_TW_DB)
    c = conn.cursor()
    c.execute("SELECT 1 FROM twitter_state WHERE key = ?", (key,))
    result = c.fetchone() is not None
    conn.close()
    return result


def tw_mark_seen(key: str, value: str = "done") -> str:
    """Mark a tweet_id or username as processed so it won't be acted on again.

    key   — same key you checked with tw_is_seen()
    value — optional label describing what was done (e.g. "liked", "followed")

    Returns confirmation string.
    """
    conn = sqlite3.connect(_TW_DB)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO twitter_state (key, value, logged_at) VALUES (?, ?, ?)",
        (key, value, datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()
    return f"✅ Marked seen: {key} ({value})"


def tw_last_tweet_time() -> str:
    """Return the ISO timestamp of the last tweet posted by the agent.

    Returns empty string if no tweet has been logged yet.
    Use this to decide whether it's time to post again:
      last = tw_last_tweet_time()
      if not last or (datetime.now() - datetime.fromisoformat(last)).total_seconds() >= 8*3600:
          # post a tweet
    """
    conn = sqlite3.connect(_TW_DB)
    c = conn.cursor()
    c.execute("SELECT value FROM twitter_state WHERE key = 'last_tweet_time'")
    row = c.fetchone()
    conn.close()
    return row[0] if row else ""


def tw_log_tweet() -> str:
    """Record that a tweet was just posted. Call immediately after browser_session.tweet().

    Saves the current timestamp so tw_last_tweet_time() can enforce tweet frequency.
    Returns confirmation string, or an error message if the write failed (fix #10).
    """
    now = datetime.now().isoformat()
    try:
        conn = sqlite3.connect(_TW_DB)
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO twitter_state (key, value, logged_at) VALUES (?, ?, ?)",
            ("last_tweet_time", now, now),
        )
        conn.commit()
        conn.close()
        return f"✅ Tweet logged at {now}"
    except Exception as e:
        return f"❌ ERROR: failed to log tweet timestamp: {e}"


def tw_prune(max_age_days: int = 90) -> str:
    """Remove twitter_state entries older than max_age_days (default: 90).

    Call periodically to prevent unbounded table growth (fix #9).
    The 'last_tweet_time' sentinel key is never pruned.
    """
    cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
    try:
        conn = sqlite3.connect(_TW_DB)
        c = conn.cursor()
        c.execute(
            "DELETE FROM twitter_state WHERE key != 'last_tweet_time' AND logged_at < ?",
            (cutoff,),
        )
        deleted = c.rowcount
        conn.commit()
        conn.close()
        return f"✅ Pruned {deleted} twitter_state entries older than {max_age_days} days."
    except Exception as e:
        return f"❌ ERROR: tw_prune failed: {e}"
