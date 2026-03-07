import os
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

NAME = "youtube"
DOC = (
    "YouTube Data API: search for videos and channels with metrics. "
    "Setup: gcal_credentials.json + authorize() or activate(api_key). "
    "Functions: authorize(code?), activate(api_key), status(), "
    "search_videos(query, max_results='5'), search_channels(query, max_results='5')."
)

# Robust Path Resolution (mirroring Senior Developer patterns)
_MEMORY_DIR = Path(os.getenv("MEMORY_DIR", "/app/memory"))
if not _MEMORY_DIR.exists():
    # Fallback for local development
    if Path("memory").exists():
        _MEMORY_DIR = Path("memory")
    else:
        _MEMORY_DIR = Path(".")

_CREDENTIALS_FILE = _MEMORY_DIR / "gcal_credentials.json"
if not _CREDENTIALS_FILE.exists() and Path("gcal_credentials.json").exists():
    _CREDENTIALS_FILE = Path("gcal_credentials.json")

_TOKEN_FILE = _MEMORY_DIR / "youtube_token.json"
_SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]

_api_key = os.getenv("YOUTUBE_API_KEY", "")
_last_authorize_msg = {"timestamp": 0}

def _check_deps() -> tuple[bool, str]:
    try:
        import googleapiclient  # noqa
        import google_auth_oauthlib  # noqa
        import google.auth  # noqa
        return True, "ok"
    except ImportError as e:
        return False, (
            f"Missing package: {e}. "
            "Add to requirements.txt:\n"
            "  google-api-python-client\n"
            "  google-auth-httplib2\n"
            "  google-auth-oauthlib"
        )

def _get_service():
    """Build authenticated YouTube service (reusing Calendar/Gmail app logic)."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    # Priority 1: Run-time API Key
    if _api_key:
        return build("youtube", "v3", developerKey=_api_key)

    # Priority 2: OAuth Token
    if not _CREDENTIALS_FILE.exists():
        raise FileNotFoundError(f"Credentials not found at {_CREDENTIALS_FILE}. Place gcal_credentials.json in root or use activate(key).")

    creds = None
    if _TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE), _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _TOKEN_FILE.write_text(creds.to_json())
        else:
            raise PermissionError("Not authorized. Call authorize() for link or activate(key) for API key.")

    return build("youtube", "v3", credentials=creds)

def activate(api_key: str) -> str:
    """Setup YouTube skill instantly using a static API Key."""
    global _api_key
    if not api_key: return "❌ Provide an API key."
    _api_key = api_key.strip()
    try:
        service = _get_service()
        service.search().list(q="test", maxResults=1, part="id").execute()
        return "✅ YouTube activated via API Key."
    except Exception as e:
        return f"❌ Activation failed: {e}"

def authorize(code: str = "") -> str:
    """Standard Google OAuth 2.0 flow (Step 1: Link, Step 2: Code)."""
    import requests as _req
    from urllib.parse import urlencode as _urlencode

    ok, reason = _check_deps()
    if not ok: return f"❌ {reason}"

    # Use resolved path
    if not _CREDENTIALS_FILE.exists():
        return f"❌ Credentials not found at {_CREDENTIALS_FILE}."

    try:
        secrets = json.loads(_CREDENTIALS_FILE.read_text())
        cfg = secrets.get("installed") or secrets.get("web") or {}
        client_id     = cfg["client_id"]
        client_secret = cfg["client_secret"]
        auth_uri      = cfg.get("auth_uri",  "https://accounts.google.com/o/oauth2/auth")
        token_uri     = cfg.get("token_uri", "https://oauth2.googleapis.com/token")
        redirect_uri  = "urn:ietf:wg:oauth:2.0:oob"
    except Exception as e:
        return f"❌ Config error: {e}"

    if not code.strip():
        # Step 1: Link
        params = {
            "client_id": client_id, "redirect_uri": redirect_uri,
            "response_type": "code", "scope": " ".join(_SCOPES),
            "access_type": "offline", "prompt": "consent",
        }
        url = auth_uri + "?" + _urlencode(params)
        
        # Loop protection
        now = time.time()
        if now - _last_authorize_msg["timestamp"] < 5:
             return "⚠️ Please use the link above to continue."
        _last_authorize_msg["timestamp"] = now

        return (
            "🔐 YouTube Authorization\n\n1. Open link:\n"
            f"```\n{url}\n```\n"
            "2. Paste code: authorize(CODE)"
        )

    # Step 2: Exchange
    try:
        resp = _req.post(token_uri, data={
            "code": code.strip(), "client_id": client_id,
            "client_secret": client_secret, "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }, timeout=15).json()
        
        if "access_token" not in resp:
            return f"❌ Error: {resp.get('error_description', resp.get('error'))}"

        from google.oauth2.credentials import Credentials
        creds = Credentials(
            token=resp["access_token"], refresh_token=resp.get("refresh_token"),
            token_uri=token_uri, client_id=client_id,
            client_secret=client_secret, scopes=_SCOPES,
        )
        _TOKEN_FILE.write_text(creds.to_json())
        return "✅ YouTube authorized!"
    except Exception as e:
        return f"❌ Setup failed: {e}"

def status() -> str:
    """Check auth health and paths."""
    ok, _ = _check_deps()
    token_exists = _TOKEN_FILE.exists()
    
    state = "Ready (API Key)" if _api_key else ("Ready (OAuth)" if token_exists else "Not Setup")
    return (
        f"📺 YouTube Status: {state}\n"
        f"   - Credentials: {_CREDENTIALS_FILE}\n"
        f"   - Token File:  {_TOKEN_FILE}"
    )

def search_videos(query: str, max_results: str = "5") -> str:
    """Searches for videos matching the query and returns their titles, URLs, and exact view counts."""
    try:
        max_res = int(max_results)
    except ValueError:
        max_res = 5

    try:
        youtube = _get_service()
        
        # 1. Search for videos to get their IDs
        search_response = youtube.search().list(
            q=query,
            part="id,snippet",
            maxResults=max_res,
            type="video"
        ).execute()

        video_ids = [item["id"]["videoId"] for item in search_response.get("items", [])]
        
        if not video_ids:
            return f"No YouTube videos found for query: '{query}'"

        # 2. Fetch detailed statistics for those specific videos
        stats_response = youtube.videos().list(
            id=",".join(video_ids),
            part="snippet,statistics"
        ).execute()

        results = [f"YouTube Video Search Results for '{query}':\n"]
        for item in stats_response.get("items", []):
            snippet = item["snippet"]
            stats = item["statistics"]
            
            title = snippet.get("title", "Unknown Title")
            channel = snippet.get("channelTitle", "Unknown Channel")
            published = snippet.get("publishedAt", "")[:10]
            
            views = int(stats.get("viewCount", 0))
            likes = int(stats.get("likeCount", 0))
            
            video_id = item.get("id", "")
            url = f"https://www.youtube.com/watch?v={video_id}"
            
            results.append(
                f"- {title}\n"
                f"  Channel: {channel} | Published: {published}\n"
                f"  Views: {views:,} | Likes: {likes:,}\n"
                f"  URL: {url}\n"
            )

        return "\n".join(results)

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ YouTube NOT setup. Call authorize() first. Error: {e}"
    except Exception as e:
        return f"❌ YouTube search failed: {e}"

def search_channels(query: str, max_results: str = "5") -> str:
    """Searches for channels and returns their exact subscriber counts, video counts, and total views."""
    try:
        max_res = int(max_results)
    except ValueError:
        max_res = 5

    try:
        youtube = _get_service()
        
        # 1. Search for channels to get their IDs
        search_response = youtube.search().list(
            q=query,
            part="id,snippet",
            maxResults=max_res,
            type="channel"
        ).execute()

        channel_ids = [item["id"]["channelId"] for item in search_response.get("items", [])]
        
        if not channel_ids:
            return f"No YouTube channels found for query: '{query}'"

        # 2. Fetch detailed statistics for those specific channels
        stats_response = youtube.channels().list(
            id=",".join(channel_ids),
            part="snippet,statistics"
        ).execute()

        items = stats_response.get("items", [])
        
        # Sort channels by subscriber count (descending)
        items.sort(key=lambda x: int(x["statistics"].get("subscriberCount", 0)), reverse=True)

        results = [f"YouTube Channel Search Results for '{query}' (Ranked by Subscribers):\n"]
        for item in items:
            snippet = item["snippet"]
            stats = item["statistics"]
            
            title = snippet.get("title", "Unknown Channel")
            raw_desc = snippet.get("description", "").replace("\n", " ")
            desc = raw_desc[:100] + ("..." if len(raw_desc) > 100 else "")
                
            subs = int(stats.get("subscriberCount", 0))
            videos = int(stats.get("videoCount", 0))
            views = int(stats.get("viewCount", 0))
            
            channel_id = item.get("id", "")
            url = f"https://www.youtube.com/channel/{channel_id}"
            
            results.append(
                f"- {title}\n"
                f"  Subscribers: {subs:,} | Videos: {videos:,} | Total Views: {views:,}\n"
                f"  Description: {desc}\n"
                f"  URL: {url}\n"
            )

        return "\n".join(results)

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ YouTube NOT setup. Call authorize() first. Error: {e}"
    except Exception as e:
        return f"❌ YouTube search failed: {e}"

__all__ = ["NAME", "DOC", "activate", "authorize", "status", "search_videos", "search_channels"]
