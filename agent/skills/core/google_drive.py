import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

NAME = "google_drive"
DOC = (
    "Google Drive — list, search, upload, download, create folders, and delete files. "
    "One-time setup: authorize() returns a consent URL; authorize(CODE) saves the token. After that, fully automatic. "
    "Returns: "
    "upload_to_folder(local_path, folder_name)→confirmation with folder ID and shareable Drive link (PREFERRED for uploads — finds/creates folder and uploads in one call); "
    "list_files(folder_id='')→formatted list of files with names and IDs; "
    "search_files(query)→matching files with IDs and links; "
    "create_folder(name)→confirmation containing the new folder ID; "
    "upload_file(local_path, parent_id='')→confirmation with file ID and link; "
    "download_file(file_id, local_path)→confirmation with saved local path; "
    "get_file_info(file_id)→metadata including name, size, link; "
    "status()→auth/token status."
)

_MEMORY_DIR = Path(os.getenv("MEMORY_DIR", "/app/memory"))
_CREDENTIALS_FILE = _MEMORY_DIR / "gcal_credentials.json"   # same creds as Calendar
_TOKEN_FILE = _MEMORY_DIR / "gdrive_token.json"              # separate Drive token
_SCOPES = ["https://www.googleapis.com/auth/drive"]


# ── Internal helpers ──────────────────────────────────────────────────────────

def _check_deps() -> tuple[bool, str]:
    """Returns (ok, error_message). Checks that Google client libraries are installed."""
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
    """
    Build and return an authenticated Google Drive API service object.
    Raises FileNotFoundError if credentials.json is missing.
    Raises PermissionError if token is missing or expired with no refresh token.
    Auto-refreshes an expired token if a refresh token is available.
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    if not _CREDENTIALS_FILE.exists():
        raise FileNotFoundError(
            f"credentials.json not found at {_CREDENTIALS_FILE}. "
            "Please complete the setup steps first."
        )

    creds = None
    if _TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE), _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            _TOKEN_FILE.write_text(creds.to_json())
        else:
            raise PermissionError(
                "Not authorized yet. Call authorize() to get the Google consent URL, "
                "then call authorize(YOUR_CODE) with the code Google gives you."
            )

    return build("drive", "v3", credentials=creds)


def _human_size(size_bytes) -> str:
    """Convert bytes to a human-readable size string."""
    try:
        b = int(size_bytes)
    except (TypeError, ValueError):
        return "unknown size"
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"


def _mime_label(mime: str) -> str:
    """Return a short readable label for common MIME types."""
    labels = {
        "application/vnd.google-apps.folder":       "📁 folder",
        "application/vnd.google-apps.document":     "📝 Google Doc",
        "application/vnd.google-apps.spreadsheet":  "📊 Google Sheet",
        "application/vnd.google-apps.presentation": "📽 Google Slides",
        "application/vnd.google-apps.form":         "📋 Google Form",
        "application/pdf":                          "📄 PDF",
        "image/jpeg":                               "🖼 JPEG",
        "image/png":                                "🖼 PNG",
        "text/plain":                               "📃 text",
        "application/zip":                          "🗜 ZIP",
    }
    return labels.get(mime, mime)


# ── Public skill functions ─────────────────────────────────────────────────────

def authorize(code: str = "") -> str:
    """
    One-time Google Drive authorization (two-step).

    Step 1 — call with no args to get the Google consent URL.
    Step 2 — call with the code Google gives you to save the token.

    Args:
        code: The authorization code from the Google consent page (leave blank for step 1)
    """
    import json as _json
    from urllib.parse import urlencode as _urlencode
    import requests as _req

    ok, reason = _check_deps()
    if not ok:
        return f"❌ {reason}"

    if not _CREDENTIALS_FILE.exists():
        return (
            f"❌ credentials.json not found at {_CREDENTIALS_FILE}\n\n"
            "Please complete setup:\n"
            "  → Go to console.cloud.google.com\n"
            "  → Select your project (trinityclaw)\n"
            "  → Enable the Google Drive API\n"
            "  → Add drive scope to OAuth consent screen\n"
            "  → The same gcal_credentials.json file is used — no new download needed"
        )

    try:
        secrets = _json.loads(_CREDENTIALS_FILE.read_text())
        cfg = secrets.get("installed") or secrets.get("web") or {}
        client_id     = cfg["client_id"]
        client_secret = cfg["client_secret"]
        auth_uri      = cfg.get("auth_uri",  "https://accounts.google.com/o/oauth2/auth")
        token_uri     = cfg.get("token_uri", "https://oauth2.googleapis.com/token")
        redirect_uri  = "urn:ietf:wg:oauth:2.0:oob"
    except (KeyError, Exception) as e:
        return f"❌ Could not read credentials.json: {e}"

    if not code.strip():
        # ── Step 1: build auth URL manually ──────────────────────────────────
        params = {
            "client_id":     client_id,
            "redirect_uri":  redirect_uri,
            "response_type": "code",
            "scope":         " ".join(_SCOPES),
            "access_type":   "offline",
            "prompt":        "consent",
        }
        auth_url = auth_uri + "?" + _urlencode(params)
        return (
            "🔐 Google Drive — Authorization Step 1 of 2\n\n"
            "Open this URL in your browser:\n"
            f"{auth_url}\n\n"
            "Then:\n"
            "  1. Sign in with your Google account\n"
            "  2. Click Allow\n"
            "  3. Copy the code Google shows you\n"
            "  4. Paste ONLY the code:\n"
            "     authorize(PASTE_CODE_HERE)\n\n"
            "The code is valid for 10 minutes."
        )

    # ── Step 2: exchange code for token ──────────────────────────────────────
    try:
        resp = _req.post(token_uri, data={
            "code":          code.strip(),
            "client_id":     client_id,
            "client_secret": client_secret,
            "redirect_uri":  redirect_uri,
            "grant_type":    "authorization_code",
        }, timeout=15)
        token_data = resp.json()
    except Exception as e:
        return f"❌ Token exchange request failed: {e}"

    if "error" in token_data:
        err_code = token_data.get("error", "")
        err_desc = token_data.get("error_description", "")
        if err_code == "invalid_grant":
            return (
                "❌ Google rejected the code.\n\n"
                "This means:\n"
                "  • The code was already used (each code is single-use)\n"
                "  • OR more than 10 minutes passed before pasting it\n\n"
                "Call authorize() again (no args) to get a fresh URL."
            )
        return f"❌ Token error: {err_code} — {err_desc}"

    try:
        from google.oauth2.credentials import Credentials
        creds = Credentials(
            token=token_data["access_token"],
            refresh_token=token_data.get("refresh_token"),
            token_uri=token_uri,
            client_id=client_id,
            client_secret=client_secret,
            scopes=_SCOPES,
        )
        _TOKEN_FILE.write_text(creds.to_json())
    except Exception as e:
        return f"❌ Failed to save token: {e}"

    refresh_note = ""
    if not token_data.get("refresh_token"):
        refresh_note = (
            "\n\n⚠️  No refresh token received — token will expire in ~1 hour.\n"
            "To fix: go to myaccount.google.com/permissions, revoke this app, "
            "then call authorize() again."
        )

    return (
        "✅ Google Drive authorized and ready!\n"
        f"Token saved to {_TOKEN_FILE}\n"
        "You can now use list_files, search_files, upload_file, and all other functions."
        + refresh_note
    )


def list_files(folder_id: str = "", max_results: str = "20") -> str:
    """
    List files in Google Drive, optionally inside a specific folder.

    Args:
        folder_id:   ID of a folder to list contents of (leave blank for root/all recent)
        max_results: Maximum number of files to return (default: 20)
    """
    ok, reason = _check_deps()
    if not ok:
        return f"❌ {reason}"

    try:
        service = _get_service()
        limit = max(1, min(int(max_results), 100))

        if folder_id.strip():
            query = f"'{folder_id.strip()}' in parents and trashed = false"
        else:
            query = "trashed = false"

        result = service.files().list(
            q=query,
            pageSize=limit,
            fields="files(id, name, mimeType, size, modifiedTime, webViewLink)",
            orderBy="modifiedTime desc"
        ).execute()

        files = result.get("files", [])
        if not files:
            location = f"folder {folder_id.strip()}" if folder_id.strip() else "Drive"
            return f"📁 No files found in {location}."

        location_label = f"folder `{folder_id.strip()}`" if folder_id.strip() else "Drive (recent)"
        lines = [f"📁 {len(files)} file(s) in {location_label}:\n"]
        for f in files:
            name     = f.get("name", "(unnamed)")
            fid      = f.get("id", "")
            mime     = f.get("mimeType", "")
            size     = _human_size(f.get("size")) if f.get("size") else ""
            modified = f.get("modifiedTime", "")[:10]
            type_label = _mime_label(mime)
            size_str = f"  {size}" if size else ""
            lines.append(f"• {type_label} — {name}{size_str}\n  Modified: {modified}  ID: {fid}")

        return "\n".join(lines)

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ Failed to list files: {type(e).__name__}: {e}"


def search_files(query: str) -> str:
    """
    Search for files in Google Drive by name.

    Args:
        query: Text to search for in file names (e.g. 'report', 'budget 2026')
    """
    ok, reason = _check_deps()
    if not ok:
        return f"❌ {reason}"

    if not query.strip():
        return "❌ query cannot be empty. Provide a search term."

    try:
        service = _get_service()

        safe_query = query.strip().replace("'", "\\'")
        drive_query = f"name contains '{safe_query}' and trashed = false"

        result = service.files().list(
            q=drive_query,
            pageSize=25,
            fields="files(id, name, mimeType, size, modifiedTime, webViewLink)",
            orderBy="modifiedTime desc"
        ).execute()

        files = result.get("files", [])
        if not files:
            return f"🔍 No files found matching '{query}'."

        lines = [f"🔍 {len(files)} result(s) for '{query}':\n"]
        for f in files:
            name     = f.get("name", "(unnamed)")
            fid      = f.get("id", "")
            mime     = f.get("mimeType", "")
            size     = _human_size(f.get("size")) if f.get("size") else ""
            modified = f.get("modifiedTime", "")[:10]
            type_label = _mime_label(mime)
            size_str = f"  {size}" if size else ""
            lines.append(f"• {type_label} — {name}{size_str}\n  Modified: {modified}  ID: {fid}")

        return "\n".join(lines)

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ Search failed: {type(e).__name__}: {e}"


def get_file_info(file_id: str) -> str:
    """
    Get detailed metadata about a file or folder.

    Args:
        file_id: The file ID (shown by list_files or search_files)
    """
    ok, reason = _check_deps()
    if not ok:
        return f"❌ {reason}"

    if not file_id.strip():
        return "❌ file_id cannot be empty."

    try:
        service = _get_service()
        f = service.files().get(
            fileId=file_id.strip(),
            fields="id, name, mimeType, size, createdTime, modifiedTime, "
                   "webViewLink, parents, description, owners"
        ).execute()

        name        = f.get("name", "(unnamed)")
        mime        = f.get("mimeType", "unknown")
        size        = _human_size(f.get("size")) if f.get("size") else "N/A (Google file)"
        created     = f.get("createdTime", "")[:10]
        modified    = f.get("modifiedTime", "")[:10]
        link        = f.get("webViewLink", "no link")
        parents     = ", ".join(f.get("parents", [])) or "root"
        owners      = ", ".join(o.get("emailAddress", "?") for o in f.get("owners", []))
        type_label  = _mime_label(mime)
        desc        = f.get("description", "")

        lines = [
            f"📄 File Info",
            f"Name:     {name}",
            f"Type:     {type_label}",
            f"Size:     {size}",
            f"Created:  {created}",
            f"Modified: {modified}",
            f"Owner:    {owners}",
            f"Parent:   {parents}",
            f"ID:       {file_id.strip()}",
            f"Link:     {link}",
        ]
        if desc:
            lines.append(f"Desc:     {desc}")

        return "\n".join(lines)

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ {e}"
    except Exception as e:
        if "404" in str(e):
            return f"❌ File not found: {file_id.strip()}"
        return f"❌ Failed to get file info: {type(e).__name__}: {e}"


def create_folder(name: str, parent_id: str = "") -> str:
    """
    Create a new folder in Google Drive.

    Args:
        name:      Folder name
        parent_id: ID of the parent folder (leave blank to create in root)
    """
    ok, reason = _check_deps()
    if not ok:
        return f"❌ {reason}"

    if not name.strip():
        return "❌ name cannot be empty."

    try:
        service = _get_service()

        metadata = {
            "name":     name.strip(),
            "mimeType": "application/vnd.google-apps.folder",
        }
        if parent_id.strip():
            metadata["parents"] = [parent_id.strip()]

        folder = service.files().create(
            body=metadata,
            fields="id, name, webViewLink"
        ).execute()

        fid  = folder.get("id", "")
        link = folder.get("webViewLink", "")
        location = f"inside folder {parent_id.strip()}" if parent_id.strip() else "in Drive root"

        return (
            f"✅ Folder created: {name.strip()}\n"
            f"Location: {location}\n"
            f"ID:       {fid}\n"
            f"Link:     {link}"
        )

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ Failed to create folder: {type(e).__name__}: {e}"


def upload_file(local_path: str, parent_id: str = "") -> str:
    """
    Upload a local file to Google Drive.

    Args:
        local_path: Absolute path to the file on the server (e.g. /app/memory/report.pdf)
        parent_id:  ID of the destination folder (leave blank to upload to root)
    """
    ok, reason = _check_deps()
    if not ok:
        return f"❌ {reason}"

    path = Path(local_path.strip())
    if not path.exists():
        return f"❌ File not found: {local_path}"
    if not path.is_file():
        return f"❌ Path is not a file: {local_path}"

    try:
        import mimetypes
        from googleapiclient.http import MediaFileUpload

        service = _get_service()

        mime_type, _ = mimetypes.guess_type(str(path))
        mime_type = mime_type or "application/octet-stream"

        metadata = {"name": path.name}
        if parent_id.strip():
            metadata["parents"] = [parent_id.strip()]

        media = MediaFileUpload(str(path), mimetype=mime_type, resumable=True)
        uploaded = service.files().create(
            body=metadata,
            media_body=media,
            fields="id, name, size, webViewLink"
        ).execute()

        fid   = uploaded.get("id", "")
        size  = _human_size(uploaded.get("size"))
        link  = uploaded.get("webViewLink", "")
        dest  = f"folder {parent_id.strip()}" if parent_id.strip() else "Drive root"

        return (
            f"✅ Uploaded: {path.name}\n"
            f"Size:     {size}\n"
            f"Location: {dest}\n"
            f"ID:       {fid}\n"
            f"Link:     {link}"
        )

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ Upload failed: {type(e).__name__}: {e}"


def upload_to_folder(local_path: str, folder_name: str) -> str:
    """
    Upload a local file to a named Google Drive folder in one step.

    Finds an existing folder by name, or creates it if it doesn't exist,
    then uploads the file into it. Use this instead of calling
    create_folder + upload_file separately.

    Args:
        local_path:  Absolute path to the file on the server (e.g. /app/memory/cat.jpg)
        folder_name: Name of the destination folder (e.g. 'images', 'reports')
    """
    ok, reason = _check_deps()
    if not ok:
        return f"❌ {reason}"

    path = Path(local_path.strip())
    if not path.exists():
        return f"❌ File not found: {local_path}"
    if not path.is_file():
        return f"❌ Path is not a file: {local_path}"

    fname = folder_name.strip()
    if not fname:
        return "❌ folder_name cannot be empty."

    try:
        import mimetypes
        from googleapiclient.http import MediaFileUpload

        service = _get_service()

        # ── Step 1: find an existing folder with this name ────────────────────
        safe_name = fname.replace("'", "\\'")
        q = (
            f"name = '{safe_name}' "
            f"and mimeType = 'application/vnd.google-apps.folder' "
            f"and trashed = false"
        )
        result = service.files().list(
            q=q, pageSize=1, fields="files(id, name)"
        ).execute()
        existing = result.get("files", [])

        if existing:
            folder_id = existing[0]["id"]
            folder_note = f"existing folder '{fname}'"
        else:
            # ── Step 2: create the folder ─────────────────────────────────────
            folder_meta = {
                "name":     fname,
                "mimeType": "application/vnd.google-apps.folder",
            }
            folder = service.files().create(
                body=folder_meta, fields="id"
            ).execute()
            folder_id = folder["id"]
            folder_note = f"new folder '{fname}' (just created)"

        # ── Step 3: upload the file into the folder ───────────────────────────
        mime_type, _ = mimetypes.guess_type(str(path))
        mime_type = mime_type or "application/octet-stream"

        metadata = {"name": path.name, "parents": [folder_id]}
        media = MediaFileUpload(str(path), mimetype=mime_type, resumable=True)
        uploaded = service.files().create(
            body=metadata,
            media_body=media,
            fields="id, name, size, webViewLink"
        ).execute()

        fid  = uploaded.get("id", "")
        size = _human_size(uploaded.get("size"))
        link = uploaded.get("webViewLink", "")

        return (
            f"✅ Uploaded: {path.name}\n"
            f"Destination: {folder_note}\n"
            f"Folder ID:  {folder_id}\n"
            f"Size:       {size}\n"
            f"File ID:    {fid}\n"
            f"Link:       {link}"
        )

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ {e}"
    except Exception as e:
        return f"❌ upload_to_folder failed: {type(e).__name__}: {e}"


def download_file(file_id: str, local_path: str) -> str:
    """
    Download a file from Google Drive to the server.

    Note: Google Docs/Sheets/Slides cannot be downloaded directly — use get_file_info
    to get the webViewLink instead, or export them via the Drive UI.

    Args:
        file_id:    The file ID (shown by list_files or search_files)
        local_path: Absolute path to save the file (e.g. /app/memory/report.pdf)
    """
    ok, reason = _check_deps()
    if not ok:
        return f"❌ {reason}"

    if not file_id.strip():
        return "❌ file_id cannot be empty."
    if not local_path.strip():
        return "❌ local_path cannot be empty."

    try:
        from googleapiclient.http import MediaIoBaseDownload
        import io

        service = _get_service()

        # Check if it's a Google Workspace file (can't be downloaded as-is)
        meta = service.files().get(
            fileId=file_id.strip(),
            fields="name, mimeType"
        ).execute()
        mime = meta.get("mimeType", "")
        name = meta.get("name", file_id)

        if mime.startswith("application/vnd.google-apps."):
            return (
                f"❌ '{name}' is a Google Workspace file ({_mime_label(mime)}).\n"
                "Google Docs/Sheets/Slides cannot be downloaded directly.\n"
                "Use get_file_info() to get the webViewLink and open it in the browser."
            )

        dest = Path(local_path.strip())
        dest.parent.mkdir(parents=True, exist_ok=True)

        request = service.files().get_media(fileId=file_id.strip())
        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()

        dest.write_bytes(buf.getvalue())
        size = _human_size(dest.stat().st_size)

        return (
            f"✅ Downloaded: {name}\n"
            f"Saved to: {dest}\n"
            f"Size:     {size}"
        )

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ {e}"
    except Exception as e:
        if "404" in str(e):
            return f"❌ File not found: {file_id.strip()}"
        return f"❌ Download failed: {type(e).__name__}: {e}"


def delete_file(file_id: str) -> str:
    """
    Move a file or folder to the Drive trash.

    Args:
        file_id: The file ID (shown by list_files or search_files)
    """
    ok, reason = _check_deps()
    if not ok:
        return f"❌ {reason}"

    if not file_id.strip():
        return "❌ file_id cannot be empty."

    try:
        service = _get_service()

        # Get name before deleting for a better confirmation message
        try:
            meta = service.files().get(
                fileId=file_id.strip(), fields="name"
            ).execute()
            name = meta.get("name", file_id.strip())
        except Exception:
            name = file_id.strip()

        # Move to trash (reversible) rather than permanently deleting
        service.files().update(
            fileId=file_id.strip(),
            body={"trashed": True}
        ).execute()

        return (
            f"🗑 '{name}' moved to trash.\n"
            f"ID: {file_id.strip()}\n"
            "You can restore it from Google Drive trash within 30 days."
        )

    except (PermissionError, FileNotFoundError) as e:
        return f"❌ {e}"
    except Exception as e:
        if "404" in str(e):
            return f"❌ File not found: {file_id.strip()}"
        return f"❌ Failed to delete file: {type(e).__name__}: {e}"


def status() -> str:
    """Show Google Drive skill configuration and auth token status."""
    ok, dep_reason = _check_deps()
    creds_exists = _CREDENTIALS_FILE.exists()
    token_exists = _TOKEN_FILE.exists()

    lines = [
        "📁 Google Drive Skill Status",
        f"Dependencies:     {'✅ installed' if ok else '❌ ' + dep_reason}",
        f"credentials.json: {'✅ found' if creds_exists else '❌ not found at ' + str(_CREDENTIALS_FILE)}",
        f"Token:            {'✅ saved' if token_exists else '❌ not authorized — call authorize()'}",
    ]

    if token_exists and ok:
        try:
            from google.oauth2.credentials import Credentials
            from google.auth.transport.requests import Request
            creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE), _SCOPES)
            if creds.valid:
                lines.append("Auth status:      ✅ valid and ready")
            elif creds.expired and creds.refresh_token:
                creds.refresh(Request())
                _TOKEN_FILE.write_text(creds.to_json())
                lines.append("Auth status:      ✅ token was expired — refreshed automatically")
            else:
                lines.append("Auth status:      ❌ token expired, no refresh token — call authorize() again")
        except Exception as e:
            lines.append(f"Auth status:      ❌ could not read token: {e}")

    return "\n".join(lines)
