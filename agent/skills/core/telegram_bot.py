import os
import requests
import threading
import time
import queue
from typing import Optional
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
import logging

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

NAME = "telegram_bot"
DOC = "Telegram integration with connection pooling, retry logic, and async message sending. Credentials (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) are auto-loaded from .env — no setup() needed. Usage: start_polling(), send(message), stop(), status(), test_connection()"

# Configuration — all secrets loaded from .env, never hardcoded
_token: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
_chat_id: str = os.environ.get("TELEGRAM_CHAT_ID", "")
_trinity_url: str = "http://localhost:8001/chat"
_api_key: str = os.environ.get("TRINITY_API_KEY", "")
_polling_thread: Optional[threading.Thread] = None
_message_queue: queue.Queue = queue.Queue()
_send_thread: Optional[threading.Thread] = None

# Thread-safe state
_state_lock = threading.Lock()
_running = False
_last_update_id = 0
_error_count = 0
_max_errors = 5

# Create session with connection pooling
_session = requests.Session()
_adapter = requests.adapters.HTTPAdapter(
    pool_connections=10,
    pool_maxsize=20,
    max_retries=3,
    pool_block=False
)
_session.mount('https://', _adapter)
_session.mount('http://', _adapter)

def setup(*args) -> str:
    """
    Configure the bot with token, chat ID, and optionally the Trinity API key.
    Usage: <skill:telegram_bot.setup>TOKEN, CHAT_ID</skill:telegram_bot.setup>
    Usage: <skill:telegram_bot.setup>TOKEN, CHAT_ID, API_KEY</skill:telegram_bot.setup>
    """
    global _token, _chat_id, _api_key
    if len(args) < 2:
        return "❌ Usage: setup(token, chat_id) or setup(token, chat_id, api_key)"
    _token   = str(args[0]).strip()
    _chat_id = str(args[1]).strip()
    if len(args) >= 3:
        _api_key = str(args[2]).strip()
    key_status = f"API key: {'✅ set' if _api_key else '⚠️ missing — replies to Telegram will fail'}"
    return f"✅ Configured\nToken: {_token[:10]}...\nChat ID: {_chat_id}\n{key_status}"

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((requests.exceptions.Timeout, requests.exceptions.ConnectionError)),
    reraise=True
)
def _send_with_retry(message: str) -> dict:
    """Internal: Send message with retry logic"""
    if not _token or not _chat_id:
        raise ValueError("Bot not configured. Call setup() first.")

    # Truncate message if too long (Telegram limit is 4096)
    if len(message) > 4000:
        message = message[:3997] + "..."

    url = f"https://api.telegram.org/bot{_token}/sendMessage"

    response = _session.post(
        url,
        json={"chat_id": _chat_id, "text": message, "parse_mode": "HTML"},
        timeout=(10, 30),  # (connect_timeout, read_timeout)
        headers={"Content-Type": "application/json"}
    )
    response.raise_for_status()
    return response.json()

def send(message: str) -> str:
    """
    Send a message immediately (blocking) with retry.
    For non-blocking send, use queue_message().
    Usage: <skill:telegram_bot.send>Hello World</skill:telegram_bot.send>
    """
    try:
        result = _send_with_retry(message)
        if result.get("ok"):
            return "✅ Message sent"
        else:
            return f"⚠️ Telegram error: {result.get('description', 'Unknown error')}"
    except Exception as e:
        logger.error(f"Send failed: {e}")
        return f"❌ Send failed: {type(e).__name__}: {str(e)[:100]}"

def queue_message(message: str) -> str:
    """
    Queue a message for async sending (non-blocking).
    Use this in polling loop to avoid blocking.
    """
    if not _token:
        return "❌ Not configured"

    _message_queue.put(message)
    return "✅ Queued"

def _send_worker():
    """Background thread: Process message queue"""
    while True:
        try:
            # Block for 1 second waiting for messages
            message = _message_queue.get(timeout=1)
            if message is None:  # Poison pill to stop
                break

            # Send with retry
            try:
                result = _send_with_retry(message)
                if not result.get("ok"):
                    logger.warning(f"Failed to send: {result}")
            except Exception as e:
                logger.error(f"Send worker error: {e}")
                # Put back in queue to retry later?
                # For now, just log and drop to avoid infinite loop
            finally:
                _message_queue.task_done()
        except queue.Empty:
            continue
        except Exception as e:
            logger.error(f"Worker error: {e}")
            time.sleep(1)

def _poll_updates():
    """Main polling loop with exponential backoff"""
    global _last_update_id, _error_count, _running

    consecutive_errors = 0
    max_backoff = 30  # Max seconds between retries

    while _running:
        try:
            # Use short polling with adaptive timeout
            # Short timeout = more responsive, less blocking
            timeout = min(10 + consecutive_errors * 2, 25)  # Adaptive: 10s -> 25s

            url = f"https://api.telegram.org/bot{_token}/getUpdates"
            params = {
                "offset": _last_update_id + 1,
                "limit": 10,  # Process max 10 at a time
                "timeout": timeout
            }

            # Make request with connection timeout separate from read timeout
            response = _session.get(
                url,
                params=params,
                timeout=(10, timeout + 10)  # (connect, read) - read must be > long_polling timeout
            )
            response.raise_for_status()

            data = response.json()
            updates = data.get("result", [])

            # Reset error counter on success
            if consecutive_errors > 0:
                logger.info(f"Connection restored after {consecutive_errors} errors")
                consecutive_errors = 0

            # Process updates
            for update in updates:
                _last_update_id = update["update_id"]

                if "message" in update:
                    message = update["message"]
                    text = message.get("text", "")
                    chat_id = message.get("chat", {}).get("id")

                    if text and str(chat_id) == _chat_id:
                        logger.info(f"Received: {text[:50]}")
                        _handle_incoming_message(text, from_chat_id=str(chat_id))
                    elif text:
                        logger.warning(
                            f"Message dropped: chat_id={chat_id} doesn't match "
                            f"TELEGRAM_CHAT_ID={_chat_id!r}. Check your .env file."
                        )

                    # Handle photos from the authorised chat only
                    photo_list = message.get("photo")
                    if photo_list and str(chat_id) == _chat_id:
                        caption = message.get("caption", "")
                        logger.info(f"Received photo (caption: {caption[:30] if caption else 'none'})")
                        _handle_incoming_photo(photo_list, caption, str(chat_id))

                    # Handle voice messages from the authorised chat only
                    voice_obj = message.get("voice")
                    if voice_obj and str(chat_id) == _chat_id:
                        logger.info(f"Received voice ({voice_obj.get('duration', 0)}s)")
                        _handle_incoming_voice(voice_obj, str(chat_id))

            # Small delay to prevent CPU spinning if no updates
            if not updates:
                time.sleep(0.5)

        except requests.exceptions.Timeout:
            consecutive_errors += 1
            logger.warning(f"Timeout #{consecutive_errors}")
            # Don't sleep too long on timeout - try again quickly
            time.sleep(min(2 ** consecutive_errors, max_backoff))

        except requests.exceptions.ConnectionError as e:
            consecutive_errors += 1
            logger.error(f"Connection error #{consecutive_errors}: {e}")
            time.sleep(min(2 ** consecutive_errors, max_backoff))

        except Exception as e:
            consecutive_errors += 1
            logger.error(f"Polling error #{consecutive_errors}: {e}")
            time.sleep(min(2 ** consecutive_errors, max_backoff))

        # Too many consecutive errors — back off hard then reset and keep trying.
        # We never permanently kill polling; the agent should always be reachable.
        if consecutive_errors >= _max_errors:
            logger.critical(
                f"Too many consecutive errors ({consecutive_errors}). "
                f"Backing off {max_backoff}s then retrying — polling will NOT stop."
            )
            time.sleep(max_backoff)
            consecutive_errors = 0

def _handle_incoming_message(text: str, from_chat_id: str = ""):
    """Handle incoming message - forward to Trinity with auth, queue response"""
    try:
        if not _api_key:
            logger.error("TRINITY_API_KEY not set — cannot forward message to Trinity")
            queue_message("⚠️ Bot not fully configured: Trinity API key missing.")
            return

        # Use the Telegram chat_id as a stable session so Trinity keeps context
        session_id = f"telegram_{from_chat_id or _chat_id}"
        
        queue_message("🤔 <i>Thinking... (this may take a moment)</i>")
        
        response = _session.post(
            _trinity_url,
            json={"message": text, "session_id": session_id},
            headers={"X-API-Key": _api_key, "Content-Type": "application/json"},
            timeout=(5, 300),  # 5s connect, 300s read (AI can be slow)
        )

        if response.status_code == 200:
            reply = response.json().get("reply", "No response")
            queue_message(reply)
        elif response.status_code == 429:
            queue_message("⏳ I'm being rate-limited by the AI provider. Please wait a few seconds and try again.")
        elif response.status_code in (401, 403):
            logger.error(f"Trinity rejected request: {response.status_code} — check TRINITY_API_KEY")
            queue_message("⚠️ Auth error: check TRINITY_API_KEY is correct.")
        else:
            queue_message(f"⚠️ Trinity returned HTTP {response.status_code}")

    except requests.exceptions.Timeout:
        queue_message("⏱️ Trinity is taking too long. Try again later.")
    except Exception as e:
        logger.error(f"Error handling message: {e}")
        queue_message(f"❌ Error: {str(e)[:100]}")

def start_polling() -> str:
    """
    Start polling for messages in background thread.
    Messages are forwarded to TrinityClaw and responses sent back.
    Usage: <skill:telegram_bot.start_polling></skill:telegram_bot.start_polling>
    """
    global _running, _polling_thread, _send_thread

    with _state_lock:
        if _running:
            return "⚠️ Already running"

        if not _token or not _chat_id:
            return "❌ Not configured. Call setup(token, chat_id) first."

        _running = True

    # Start message sender worker thread
    _send_thread = threading.Thread(target=_send_worker, daemon=True)
    _send_thread.start()

    # Start polling thread
    _polling_thread = threading.Thread(target=_poll_updates, daemon=True)
    _polling_thread.start()

    logger.info("Polling started")
    return "✅ Polling started\n- Messages will be forwarded to TrinityClaw\n- Responses sent back to Telegram\n- Connection pooling enabled\n- Retry logic active"



def send_photo(file_path: str, caption: str = "") -> str:
    """
    Send a photo/image to Telegram.

    Args:
        file_path: Path to image file (e.g., /app/memory/chart.png)
        caption: Optional caption text (max 1024 characters)

    Returns:
        Success/error message

    Usage: <skill:telegram_bot.send_photo>/app/memory/chart.png, caption=Sales Chart</skill:telegram_bot.send_photo>
    """
    if not _token or not _chat_id:
        return "❌ Not configured. Call setup() first."

    # Resolve path
    resolved_path = file_path if os.path.isabs(file_path) else f"/app/{file_path}"

    if not os.path.exists(resolved_path):
        return f"❌ File not found: {resolved_path}"

    try:
        url = f"https://api.telegram.org/bot{_token}/sendPhoto"

        with open(resolved_path, 'rb') as photo:
            files = {'photo': photo}
            data = {'chat_id': _chat_id}
            if caption:
                data['caption'] = caption[:1024]  # Telegram caption limit
                data['parse_mode'] = 'HTML'

            response = _session.post(
                url,
                files=files,
                data=data,
                timeout=(10, 60)
            )

        result = response.json()

        if result.get("ok"):
            return f"✅ Photo sent: {os.path.basename(resolved_path)}"
        else:
            return f"⚠️ Telegram error: {result.get('description', 'Unknown error')}"

    except FileNotFoundError:
        return f"❌ File not found: {resolved_path}"
    except requests.exceptions.Timeout:
        return "❌ Upload timed out (file too large or slow connection)"
    except Exception as e:
        return f"❌ Send photo error: {type(e).__name__}: {str(e)[:100]}"


def send_document(file_path: str, caption: str = "") -> str:
    """
    Send a document/file to Telegram (PDF, CSV, TXT, etc.).

    Args:
        file_path: Path to document file
        caption: Optional caption text (max 1024 characters)

    Returns:
        Success/error message

    Usage: <skill:telegram_bot.send_document>/app/data/report.pdf, caption=Monthly Report</skill:telegram_bot.send_document>
    """
    if not _token or not _chat_id:
        return "❌ Not configured. Call setup() first."

    # Resolve path
    resolved_path = file_path if os.path.isabs(file_path) else f"/app/{file_path}"

    if not os.path.exists(resolved_path):
        return f"❌ File not found: {resolved_path}"

    # Check file size (Telegram limit: 50MB for documents)
    file_size = os.path.getsize(resolved_path)
    if file_size > 50 * 1024 * 1024:  # 50MB
        return f"❌ File too large: {file_size / (1024*1024):.1f}MB (max 50MB)"

    try:
        url = f"https://api.telegram.org/bot{_token}/sendDocument"

        with open(resolved_path, 'rb') as doc:
            files = {'document': doc}
            data = {'chat_id': _chat_id}
            if caption:
                data['caption'] = caption[:1024]
                data['parse_mode'] = 'HTML'

            response = _session.post(
                url,
                files=files,
                data=data,
                timeout=(10, 120)  # Longer timeout for documents
            )

        result = response.json()

        if result.get("ok"):
            size_mb = file_size / (1024 * 1024)
            return f"✅ Document sent: {os.path.basename(resolved_path)} ({size_mb:.2f}MB)"
        else:
            return f"⚠️ Telegram error: {result.get('description', 'Unknown error')}"

    except FileNotFoundError:
        return f"❌ File not found: {resolved_path}"
    except requests.exceptions.Timeout:
        return "❌ Upload timed out (file too large or slow connection)"
    except Exception as e:
        return f"❌ Send document error: {type(e).__name__}: {str(e)[:100]}"


def stop() -> str:
    """
    Stop polling.
    Usage: <skill:telegram_bot.stop></skill:telegram_bot.stop>
    """
    global _running, _polling_thread, _send_thread

    with _state_lock:
        if not _running:
            return "⚠️ Not running"
        _running = False

    # Signal send worker to stop
    _message_queue.put(None)

    # Wait for threads to finish (with timeout)
    if _polling_thread and _polling_thread.is_alive():
        _polling_thread.join(timeout=5)
    if _send_thread and _send_thread.is_alive():
        _send_thread.join(timeout=5)

    logger.info("Polling stopped")
    return "✅ Stopped"

def status() -> str:
    """
    Get current status.
    Usage: <skill:telegram_bot.status></skill:telegram_bot.status>
    """
    with _state_lock:
        running = _running

    if not _token:
        return "❌ Not configured"

    status_msg  = "🟢 Running" if running else "🔴 Stopped"
    key_status  = "✅ set" if _api_key else "⚠️ MISSING — two-way replies won't work"
    queue_size  = _message_queue.qsize()

    return (
        f"{status_msg}\n"
        f"Last update ID: {_last_update_id}\n"
        f"Message queue:  {queue_size} pending\n"
        f"Trinity API key: {key_status}"
    )

def test_connection() -> str:
    """
    Test Telegram connection without starting polling.
    Usage: <skill:telegram_bot.test_connection></skill:telegram_bot.test_connection>
    """
    if not _token:
        return "❌ Not configured"

    try:
        url = f"https://api.telegram.org/bot{_token}/getMe"
        response = _session.get(url, timeout=10)
        data = response.json()

        if data.get("ok"):
            bot_info = data.get("result", {})
            return f"✅ Connected\nBot: @{bot_info.get('username')}\nName: {bot_info.get('first_name')}"
        else:
            return f"❌ API error: {data.get('description')}"
    except Exception as e:
        return f"❌ Connection failed: {type(e).__name__}: {str(e)[:100]}"

def _download_telegram_file(file_id: str):
    """
    Download any Telegram file by file_id.
    Returns (bytes, extension) or raises on error.
    """
    # Step 1: resolve the file path
    url = f"https://api.telegram.org/bot{_token}/getFile"
    resp = _session.get(url, params={"file_id": file_id}, timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise ValueError(f"getFile error: {data.get('description')}")
    file_path = data["result"]["file_path"]

    # Step 2: download the bytes
    download_url = f"https://api.telegram.org/file/bot{_token}/{file_path}"
    resp = _session.get(download_url, timeout=30)
    resp.raise_for_status()

    ext = file_path.rsplit(".", 1)[-1].lower() if "." in file_path else "bin"
    return resp.content, ext


def _handle_incoming_photo(photo_list: list, caption: str, from_chat_id: str):
    """
    Handle a Telegram photo message: download the highest-resolution variant,
    base64-encode it, and forward to the /chat endpoint with the image field.
    """
    import base64
    try:
        largest = photo_list[-1]  # Telegram orders sizes ascending; last = largest
        file_bytes, ext = _download_telegram_file(largest["file_id"])

        mime_map = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                    "gif": "image/gif", "webp": "image/webp"}
        mime_type = mime_map.get(ext, "image/jpeg")
        image_data_url = f"data:{mime_type};base64,{base64.b64encode(file_bytes).decode()}"

        message_text = caption if caption else "What is in this image?"
        session_id = f"telegram_{from_chat_id}"
        
        queue_message("👁️ <i>Analyzing image... this may take a moment</i>")

        payload = {"message": message_text, "image": image_data_url, "session_id": session_id}
        headers = {"X-API-Key": _api_key, "Content-Type": "application/json"}

        # Vision calls are slow; retry once on transient 502/503 from NVIDIA NIM
        resp = None
        for attempt in range(2):
            resp = _session.post(_trinity_url, json=payload, headers=headers, timeout=(10, 120))
            if resp.status_code not in (502, 503):
                break
            if attempt == 0:
                logger.warning(f"Vision call got HTTP {resp.status_code}, retrying in 3s...")
                time.sleep(3)

        if resp.status_code == 200:
            queue_message(resp.json().get("reply", "No response"))
        elif resp.status_code == 429:
            queue_message("⏳ I'm being rate-limited by the AI provider. Please wait a few seconds and try again.")
        else:
            queue_message(f"⚠️ Error processing image: HTTP {resp.status_code}")
    except Exception as e:
        logger.error(f"Photo handling error: {e}")
        queue_message(f"❌ Could not process image: {str(e)[:80]}")


def _handle_incoming_voice(voice_obj: dict, from_chat_id: str):
    """
    Handle a Telegram voice message: download the OGG file, send it to /transcribe,
    echo the transcript back, then forward it to /chat.
    """
    import base64
    try:
        file_bytes, ext = _download_telegram_file(voice_obj["file_id"])
        b64 = base64.b64encode(file_bytes).decode()

        # Transcribe via Trinity /transcribe endpoint.
        # Timeout is long (5 min) because the first call downloads the Whisper model (~150 MB).
        # Subsequent calls are fast (model is cached on disk).
        transcribe_url = _trinity_url.replace("/chat", "/transcribe")
        resp = _session.post(
            transcribe_url,
            json={"audio": b64, "filename": f"voice.{ext}"},
            headers={"X-API-Key": _api_key, "Content-Type": "application/json"},
            timeout=(10, 300),
        )
        if resp.status_code == 503:
            queue_message("⚠️ Voice transcription unavailable: OPENAI_API_KEY not set on the server.")
            return
        if resp.status_code != 200:
            queue_message(f"⚠️ Transcription failed: HTTP {resp.status_code}")
            return

        text = resp.json().get("text", "").strip()
        if not text:
            queue_message("⚠️ Voice message was empty or could not be transcribed.")
            return

        # Echo transcript so user can see what was understood
        queue_message(f"🎤 <i>{text}</i>")
        
        queue_message("🤔 <i>Thinking about your voice message...</i>")

        # Forward transcribed text to Trinity
        session_id = f"telegram_{from_chat_id}"
        chat_resp = _session.post(
            _trinity_url,
            json={"message": text, "session_id": session_id},
            headers={"X-API-Key": _api_key, "Content-Type": "application/json"},
            timeout=(5, 90),
        )
        if chat_resp.status_code == 200:
            queue_message(chat_resp.json().get("reply", "No response"))
        else:
            queue_message(f"⚠️ Trinity returned HTTP {chat_resp.status_code}")
    except Exception as e:
        logger.error(f"Voice handling error: {e}")
        queue_message(f"❌ Could not process voice message: {str(e)[:80]}")


def send_to_trinity(message: str) -> str:
    """
    Send a message to TrinityClaw and return the response (for testing).
    Usage: <skill:telegram_bot.send_to_trinity>Hello</skill:telegram_bot.send_to_trinity>
    """
    try:
        response = _session.post(
            _trinity_url,
            json={"message": message},
            timeout=(5, 60)
        )

        if response.status_code == 200:
            return response.json().get("reply", "No response")
        else:
            return f"Error: HTTP {response.status_code}"
    except Exception as e:
        return f"Error: {type(e).__name__}: {str(e)[:100]}"