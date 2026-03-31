import asyncio
import json
import logging
import sqlite3
import time
import hashlib
import aiohttp
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
import os
import re
from urllib.parse import urlparse, urljoin
import xml.etree.ElementTree as ET
from pathlib import Path
NAME = "url_monitor"
DOC = (
    "Monitor URLs for changes and health status. "
    "Also provides Twitter state helpers for scheduler-driven automation: "
    "tw_is_seen(key)→bool — check if a tweet_id or username was already processed; "
    "tw_mark_seen(key, value?)→str — mark as processed (prevents double-likes/follows); "
    "tw_last_tweet_time()→str — ISO timestamp of last posted tweet, empty if never; "
    "tw_log_tweet()→str — record that a tweet was just posted (call right after browser_session.tweet()). "
    "Use these in scheduler prompts to build stateful Twitter automation without extra skills."
)
__all__ = [
    "NAME", "DOC",
    "add_url", "remove_url", "check_url_now", "check_all_urls",
    "get_status_summary", "get_url_history", "get_recent_changes",
    "tw_is_seen", "tw_mark_seen", "tw_last_tweet_time", "tw_log_tweet",
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
        """Initialize SQLite database for URL monitoring."""
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
                is_active BOOLEAN DEFAULT TRUE
            )
        ''')
        
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

        conn.commit()
        conn.close()
    
    def setup_logging(self):
        """Setup logging for URL monitor."""
        log_path = "/app/memory/url_monitor.log"
        logging.basicConfig(
            filename=log_path,
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        self.logger = logging.getLogger('UrlMonitor')
    
    def add_url(self, url: str, friendly_name: str = "", frequency_minutes: int = 60,
                tags: List[str] = None, alert_on_changes: bool = True, frequency: int = None) -> Dict:
        """Add a new URL to monitor."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        # Accept 'frequency' as alias for 'frequency_minutes'
        if frequency is not None:
            frequency_minutes = frequency

        # Robustly parse to int — extract first number, default 60 on failure
        try:
            frequency_minutes = int(frequency_minutes)
        except (ValueError, TypeError):
            m = re.search(r'\d+', str(frequency_minutes))
            frequency_minutes = int(m.group()) if m else 60

        tags_str = ",".join(tags) if tags else ""

        try:
            cursor.execute('''
                INSERT OR REPLACE INTO url_metadata
                (url, friendly_name, check_frequency_minutes, alert_on_changes,
                 tags, created_at, last_check, next_check, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (url, friendly_name, frequency_minutes, alert_on_changes,
                  tags_str, datetime.now().isoformat(), None,
                  (datetime.now() + timedelta(minutes=frequency_minutes)).isoformat(), True))
            
            conn.commit()
            conn.close()
            return {"success": True, "message": f"Successfully added {url}"}
            
        except Exception as e:
            conn.close()
            return {"success": False, "error": str(e)}
    
    async def check_url(self, url: str) -> UrlStatus:
        """Check a single URL and returl all status information."""
        headers = {
            'User-Agent': 'TrinityClaw-UrlMonitor/1.0'
        }
        
        start_time = time.time()
        
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                async with session.get(url, headers=headers) as response:
                    content = await response.read()
                    content_hash = hashlib.sha256(content).hexdigest()
                    
                    last_modified = None
                    if 'Last-Modified' in response.headers:
                        try:
                            last_modified = parsedate_to_datetime(response.headers['Last-Modified'])
                        except Exception as e:
                            last_modified = datetime.now()
                    
                    response_time = time.time() - start_time
                    
                    return UrlStatus(
                        url=url,
                        status_code=response.status,
                        content_hash=content_hash,
                        content_length=len(content),
                        last_modified=last_modified or datetime.now(),
                        check_time=datetime.now(),
                        response_time=response_time
                    )
                    
        except Exception as e:
            response_time = time.time() - start_time
            return UrlStatus(
                url=url,
                status_code=0,
                content_hash="",
                content_length=0,
                last_modified=datetime.now(),
                check_time=datetime.now(),
                response_time=response_time,
                error=str(e)
            )
    
    def save_check_result(self, status: UrlStatus):
        """Save check result to database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Check for changes
        cursor.execute('''
            SELECT content_hash FROM url_checks 
            WHERE url = ? ORDER BY check_time DESC LIMIT 1
        ''', (status.url,))
        
        last_result = cursor.fetchone()
        change_detected = False
        
        if last_result:
            old_hash = last_result[0]
            if status.content_hash and old_hash != status.content_hash:
                change_detected = True
                
                cursor.execute('''
                    INSERT INTO url_changes (url, old_hash, new_hash, change_type, detected_at)
                    VALUES (?, ?, ?, ?, ?)
                ''', (status.url, old_hash, status.content_hash, "content_changed", 
                      datetime.now().isoformat()))
        
        cursor.execute('''
            INSERT INTO url_checks 
            (url, status_code, content_hash, content_length, last_modified, 
             check_time, response_time, error, change_detected)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (status.url, status.status_code, status.content_hash, status.content_length,
              status.last_modified.isoformat(), status.check_time.isoformat(),
              status.response_time, status.error, change_detected))
        
        cursor.execute('''
            UPDATE url_metadata 
            SET last_check = ?, next_check = ?
            WHERE url = ?
        ''', (status.check_time.isoformat(),
              (datetime.now() + timedelta(minutes=60)).isoformat(),
              status.url))
        
        conn.commit()
        conn.close()
        
        return change_detected
    
    async def check_all_urls(self) -> List[Dict]:
        """Monitor all active URLs."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT url, friendly_name FROM url_metadata 
            WHERE is_active = 1 AND next_check <= ?
        ''', (datetime.now().isoformat(),))
        
        urls = cursor.fetchall()
        conn.close()
        
        results = []
        for url, name in urls:
            status = await self.check_url(url)
            change_detected = self.save_check_result(status)
            
            results.append({
                "url": url,
                "name": name,
                "status_code": status.status_code,
                "response_time": status.response_time,
                "change_detected": change_detected,
                "error": status.error
            })
        
        return results
    
    def get_url_history(self, url: str, limit: int = 10) -> List[Dict]:
        """Get monitoring history for a specific URL."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 10

        cursor.execute('''
            SELECT * FROM url_checks
            WHERE url = ?
            ORDER BY check_time DESC LIMIT ?
        ''', (url, limit))
        
        rows = cursor.fetchall()
        conn.close()
        
        history = []
        for row in rows:
            history.append({
                "id": row[0],
                "url": row[1],
                "status_code": row[2],
                "content_hash": row[3],
                "content_length": row[4],
                "last_modified": row[5],
                "check_time": row[6],
                "response_time": row[7],
                "error": row[8],
                "change_detected": row[9]
            })
        
        return history
    
    def get_status_summary(self) -> Dict:
        """Get comprehensive status summary."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Total active URLs
        cursor.execute('SELECT COUNT(*) FROM url_metadata WHERE is_active = 1')
        total_urls = cursor.fetchone()[0]
        
        # Recent checks (last 24 hours)
        cursor.execute('''
            SELECT COUNT(*) FROM url_checks 
            WHERE check_time > datetime('now', '-1 day') AND status_code < 400
        ''')
        recent_success = cursor.fetchone()[0]
        
        # Recent failures
        cursor.execute('''
            SELECT COUNT(*) FROM url_checks 
            WHERE check_time > datetime('now', '-1 day') AND status_code >= 400 OR error IS NOT NULL
        ''')
        recent_failures = cursor.fetchone()[0]
        
        # Recent changes
        cursor.execute('''
            SELECT COUNT(*) FROM url_changes 
            WHERE detected_at > datetime('now', '-1 day')
        ''')
        recent_changes = cursor.fetchone()[0]
        
        # Average response time
        cursor.execute('''
            SELECT AVG(response_time) FROM url_checks 
            WHERE check_time > datetime('now', '-1 day')
        ''')
        avg_response_time = cursor.fetchone()[0]
        
        # Get latest checks for each URL
        cursor.execute('''
            SELECT url_checks.url, url_checks.status_code, url_checks.check_time, url_checks.error
            FROM url_checks
            JOIN (
                SELECT url, MAX(check_time) as latest FROM url_checks
                WHERE check_time > datetime('now', '-1 day')
                GROUP BY url
            ) latest_checks ON url_checks.url = latest_checks.url AND url_checks.check_time = latest_checks.latest
        ''')
        
        latest_checks = cursor.fetchall()
        conn.close()
        
        return {
            "total_monitored": total_urls,
            "recent_success": recent_success,
            "recent_failures": recent_failures,
            "recent_changes": recent_changes,
            "avg_response_time": avg_response_time,
            "latest_checks": [{"url": row[0], "status": row[1], "checked": row[2], "error": row[3]} 
                             for row in latest_checks]
        }
    
    def get_recent_changes(self, limit: int = 10) -> List[Dict]:
        """Get recent URL changes."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 10

        cursor.execute('''
            SELECT * FROM url_changes
            ORDER BY detected_at DESC LIMIT ?
        ''', (limit,))
        
        changes = cursor.fetchall()
        conn.close()
        
        return [{
            "url": change[1],
            "old_hash": change[2],
            "new_hash": change[3],
            "change_type": change[4],
            "detected_at": change[5]
        } for change in changes]
    
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

# Skill interface functions
def add_url(url: str, friendly_name: str = "", frequency_minutes: int = 60, 
           tags: List[str] = None, alert_on_changes: bool = True) -> Dict:
    """Add a new URL to monitor."""
    return url_monitor_instance.add_url(url, friendly_name, frequency_minutes, 
                                      tags, alert_on_changes)

def get_status_summary() -> Dict:
    """Get comprehensive monitoring status."""
    return url_monitor_instance.get_status_summary()

def get_url_history(url: str, limit: int = 10) -> List[Dict]:
    """Get history for a specific URL."""
    return url_monitor_instance.get_url_history(url, limit)

def get_recent_changes(limit: int = 10) -> List[Dict]:
    """Get recent changes across all monitored URLs."""
    return url_monitor_instance.get_recent_changes(limit)

async def check_all_urls() -> List[Dict]:
    """Check all active URLs."""
    return await url_monitor_instance.check_all_urls()

def remove_url(url: str) -> Dict:
    """Remove a URL from monitoring."""
    return url_monitor_instance.remove_url(url)

def check_url_now(url: str) -> Dict:
    """Check a single URL immediately (async wrapper)."""
    import asyncio

    async def _check():
        status = await url_monitor_instance.check_url(url)
        return {
            "url": url,
            "status_code": status.status_code,
            "response_time": status.response_time,
            "content_length": status.content_length,
            "content_hash": status.content_hash,
            "error": status.error
        }

    return asyncio.run(_check())


# ── Twitter State Helpers ─────────────────────────────────────────────────────
# Lightweight key/value store for scheduler-driven Twitter automation.
# Prevents double-liking and double-following across sessions.
# Used by scheduler prompts together with browser_session Twitter functions.

_TW_DB = url_monitor_instance.db_path


def tw_is_seen(key: str) -> bool:
    """Check if a tweet_id or username has already been processed.

    key examples:
      "1234567890"          — a tweet ID (to avoid double-liking)
      "follow:elonmusk"     — a username (to avoid double-following)

    Returns True if already seen, False if new.
    """
    conn = sqlite3.connect(_TW_DB)
    c = conn.cursor()
    c.execute("SELECT 1 FROM twitter_state WHERE key = ?", (key,))
    found = c.fetchone() is not None
    conn.close()
    return found


def tw_mark_seen(key: str, value: str = "done") -> str:
    """Mark a tweet_id or username as processed so it won't be acted on again.

    key   — same key you checked with tw_is_seen()
    value — optional label describing what was done (e.g. "liked", "followed")

    Returns ✅ confirmation.
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
    Returns ✅ confirmation with the logged timestamp.
    """
    now = datetime.now().isoformat()
    conn = sqlite3.connect(_TW_DB)
    c = conn.cursor()
    c.execute(
        "INSERT OR REPLACE INTO twitter_state (key, value, logged_at) VALUES (?, ?, ?)",
        ("last_tweet_time", now, now),
    )
    conn.commit()
    conn.close()
    return f"✅ Tweet time logged: {now}"