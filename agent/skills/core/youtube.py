import os
import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

NAME = "youtube"
SHORT_DOC = "Search YouTube videos/channels, get video details and transcripts via YouTube Data API."
DOC = (
    "YouTube Data API: search, analyze videos and channels. "
    "Setup: gcal_credentials.json + authorize() or activate(api_key). "
    "Functions: authorize(code?), activate(api_key), status(), "
    "search_videos(query, max_results='5'), search_channels(query, max_results='5'), "
    "get_video(url_or_id), get_channel_videos(url_or_id, max_results='10'), "
    "get_transcript(url_or_id)."
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
    """Check that the required Google API client libraries are installed. Returns (ok, message)."""
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

def get_video(url_or_id: str) -> str:
    """Get full stats for a specific YouTube video by URL or video ID."""
    import re
    # Extract video ID from URL if needed
    video_id = url_or_id.strip()
    patterns = [
        r"(?:v=|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})",
    ]
    for p in patterns:
        m = re.search(p, video_id)
        if m:
            video_id = m.group(1)
            break

    if len(video_id) != 11:
        return f"❌ Could not extract a valid video ID from: {url_or_id}"

    try:
        youtube = _get_service()
        resp = youtube.videos().list(
            id=video_id,
            part="snippet,statistics,contentDetails"
        ).execute()

        items = resp.get("items", [])
        if not items:
            return f"❌ No video found for ID: {video_id}"

        item = items[0]
        snippet = item["snippet"]
        stats = item["statistics"]
        details = item["contentDetails"]

        title = snippet.get("title", "Unknown")
        channel = snippet.get("channelTitle", "Unknown")
        published = snippet.get("publishedAt", "")[:10]
        description = snippet.get("description", "").replace("\n", " ")[:200]
        tags = ", ".join(snippet.get("tags", [])[:10]) or "None"

        views = int(stats.get("viewCount", 0))
        likes = int(stats.get("likeCount", 0))
        comments = int(stats.get("commentCount", 0))

        # Parse ISO 8601 duration (e.g. PT4M13S)
        duration_raw = details.get("duration", "")
        dur_match = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration_raw)
        if dur_match:
            h, m, s = (int(x or 0) for x in dur_match.groups())
            duration = f"{h}h {m}m {s}s" if h else f"{m}m {s}s"
        else:
            duration = duration_raw

        url = f"https://www.youtube.com/watch?v={video_id}"

        return (
            f"📹 Video: {title}\n"
            f"   Channel:     {channel}\n"
            f"   Published:   {published}\n"
            f"   Duration:    {duration}\n"
            f"   Views:       {views:,}\n"
            f"   Likes:       {likes:,}\n"
            f"   Comments:    {comments:,}\n"
            f"   Tags:        {tags}\n"
            f"   Description: {description}{'...' if len(snippet.get('description','')) > 200 else ''}\n"
            f"   URL:         {url}"
        )

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ YouTube NOT setup. Call authorize() first. Error: {e}"
    except Exception as e:
        return f"❌ get_video failed: {e}"


def get_channel_videos(url_or_id: str, max_results: str = "10") -> str:
    """List recent uploads from a specific channel by URL, handle (@name), or channel ID."""
    import re
    try:
        max_res = int(max_results)
    except ValueError:
        max_res = 10

    raw = url_or_id.strip()

    try:
        youtube = _get_service()

        # Resolve channel ID from URL / handle / bare ID
        channel_id = None

        # Direct channel ID (UC...)
        if re.match(r"^UC[A-Za-z0-9_-]{22}$", raw):
            channel_id = raw
        else:
            # Try forUsername (legacy)
            username_match = re.search(r"/user/([^/?]+)", raw)
            handle_match = re.search(r"/@([^/?]+)", raw) or (re.match(r"^@(.+)$", raw))

            if username_match:
                resp = youtube.channels().list(
                    forUsername=username_match.group(1), part="id"
                ).execute()
                items = resp.get("items", [])
                if items:
                    channel_id = items[0]["id"]

            if not channel_id and handle_match:
                handle = handle_match.group(1)
                resp = youtube.search().list(
                    q=handle, type="channel", part="id", maxResults=1
                ).execute()
                items = resp.get("items", [])
                if items:
                    channel_id = items[0]["id"]["channelId"]

            # /channel/UC... in URL
            if not channel_id:
                ch_match = re.search(r"/channel/(UC[A-Za-z0-9_-]{22})", raw)
                if ch_match:
                    channel_id = ch_match.group(1)

        if not channel_id:
            return f"❌ Could not resolve a channel ID from: {url_or_id}"

        # Get channel name
        ch_resp = youtube.channels().list(id=channel_id, part="snippet").execute()
        ch_name = ch_resp["items"][0]["snippet"]["title"] if ch_resp.get("items") else channel_id

        # Fetch recent uploads
        search_resp = youtube.search().list(
            channelId=channel_id,
            part="id,snippet",
            maxResults=max_res,
            order="date",
            type="video"
        ).execute()

        video_ids = [item["id"]["videoId"] for item in search_resp.get("items", [])]
        if not video_ids:
            return f"No recent videos found for channel: {ch_name}"

        stats_resp = youtube.videos().list(
            id=",".join(video_ids),
            part="snippet,statistics"
        ).execute()

        lines = [f"📺 Recent videos from '{ch_name}' (latest {max_res}):\n"]
        for item in stats_resp.get("items", []):
            snippet = item["snippet"]
            stats = item["statistics"]
            title = snippet.get("title", "Unknown")
            published = snippet.get("publishedAt", "")[:10]
            views = int(stats.get("viewCount", 0))
            likes = int(stats.get("likeCount", 0))
            vid_url = f"https://www.youtube.com/watch?v={item['id']}"
            lines.append(
                f"- {title}\n"
                f"  Published: {published} | Views: {views:,} | Likes: {likes:,}\n"
                f"  URL: {vid_url}\n"
            )

        return "\n".join(lines)

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ YouTube NOT setup. Call authorize() first. Error: {e}"
    except Exception as e:
        return f"❌ get_channel_videos failed: {e}"


def get_transcript(url_or_id: str) -> str:
    """Fetch the full transcript/captions for a YouTube video. No API quota used."""
    import re
    try:
        from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled
    except ImportError:
        return (
            "❌ Missing package: youtube-transcript-api\n"
            "Install it: pip install youtube-transcript-api"
        )

    # Extract video ID
    video_id = url_or_id.strip()
    m = re.search(r"(?:v=|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})", video_id)
    if m:
        video_id = m.group(1)

    if len(video_id) != 11:
        return f"❌ Could not extract a valid video ID from: {url_or_id}"

    try:
        transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)

        # Prefer manual English, fall back to auto-generated, then any available
        try:
            transcript = transcript_list.find_manually_created_transcript(["en", "en-US", "en-GB"])
        except NoTranscriptFound:
            try:
                transcript = transcript_list.find_generated_transcript(["en", "en-US", "en-GB"])
            except NoTranscriptFound:
                # Take whatever is available, translate if possible
                transcript = next(iter(transcript_list))

        entries = transcript.fetch()
        full_text = " ".join(e["text"] for e in entries).strip()

        word_count = len(full_text.split())
        duration_s = entries[-1]["start"] + entries[-1].get("duration", 0) if entries else 0
        duration_str = f"{int(duration_s // 60)}m {int(duration_s % 60)}s"

        url = f"https://www.youtube.com/watch?v={video_id}"
        return (
            f"📝 Transcript for {url}\n"
            f"   Language: {transcript.language} ({'manual' if not transcript.is_generated else 'auto-generated'})\n"
            f"   Duration: {duration_str} | Words: {word_count:,}\n\n"
            f"{full_text}"
        )

    except TranscriptsDisabled:
        return f"❌ Transcripts are disabled for video: {video_id}"
    except NoTranscriptFound:
        return f"❌ No transcript available for video: {video_id}"
    except Exception as e:
        return f"❌ get_transcript failed: {e}"


__all__ = [
    "NAME", "DOC", "activate", "authorize", "status",
    "search_videos", "search_channels",
    "get_video", "get_channel_videos", "get_transcript",
]
