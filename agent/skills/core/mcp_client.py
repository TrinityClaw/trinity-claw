"""
MCP Client — connects TrinityClaw to remote and local MCP servers.

Designed specifically for the current TrinityClaw app.py dispatcher:
- Public API is synchronous.
- Persistent MCP connections are managed internally with threads/subprocesses.
- No asyncio public functions are exposed, avoiding asyncio.run() loop issues.

Supported transports:
- Streamable HTTP (default, modern MCP HTTP transport)
- Legacy HTTP+SSE (transport: "sse")
- Local stdio subprocess servers (transport: "stdio")

Security notes:
- Auth tokens are stored in .env and referenced by env-var name in mcp_servers.json.
- MCP tool output is untrusted external content. app.py sanitizes mcp_client results.
- Workspace-discovered stdio servers are registered disabled by default.
"""

import os
import re
import json
import time
import uuid
import atexit
import shlex
import threading
import subprocess
import requests
from collections import deque
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Union, Any
from urllib.parse import urljoin

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass


NAME = "mcp_client"
SHORT_DOC = "Connect to MCP servers, discover tools, filter tools, and call them."
DOC = (
    "Generic MCP client — register remote/local MCP servers once, then discover, test, filter, and call their tools. "
    "Supports Streamable HTTP, legacy HTTP+SSE, and stdio MCP servers. "
    "Functions: connect_server, list_servers, ping_server, test_server, list_tools, list_all_mcp_tools, "
    "enable_tool, disable_tool, set_server_enabled, call_tool, remove_server, discover_workspace_servers, "
    "close_connection, shutdown."
)

# app.py reads SKILL_TIMEOUT from the skill module and uses it for execution timeout.
SKILL_TIMEOUT = int(os.getenv("MCP_SKILL_TIMEOUT", "120"))

__all__ = [
    "NAME",
    "SHORT_DOC",
    "DOC",
    "SKILL_TIMEOUT",
    "connect_server",
    "list_servers",
    "ping_server",
    "test_server",
    "list_tools",
    "list_all_mcp_tools",
    "enable_tool",
    "disable_tool",
    "set_server_enabled",
    "call_tool",
    "remove_server",
    "discover_workspace_servers",
    "close_connection",
    "shutdown",
    "list_resources",
    "read_resource",
    "list_prompts",
    "get_prompt",
]


_CONFIG_FILE = Path(os.getenv("MCP_CONFIG_FILE", "/app/memory/mcp_servers.json"))
if not _CONFIG_FILE.parent.exists() and not Path("/app").exists():
    _CONFIG_FILE = Path("memory/mcp_servers.json")

_TIMEOUT = int(os.getenv("MCP_CLIENT_TIMEOUT", "30"))
_PROTOCOL_VERSION = "2025-06-18"
_RETRY_DELAYS = [1.0, 3.0]
_TRANSIENT_STATUS_CODES = {429, 502, 503, 504}

_TLS_VERIFY_DEFAULT = os.getenv("MCP_TLS_VERIFY", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)

_WORKSPACE_CONFIG_CANDIDATES = (
    ".trinity/mcp.json",
    ".mcp.json",
    ".vscode/mcp.json",
)

_DANGEROUS_ENV_KEYS = {
    "PATH",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES",
    "NODE_OPTIONS",
    "BASH_ENV",
    "ENV",
    "HOME",
    "SHELL",
}

# Only these variables (plus a server's explicitly configured env) are passed
# to stdio MCP servers. Everything else - including every secret in .env - is
# withheld. ${VAR} references in a server's env config still resolve at start
# time, so a server that needs a specific secret can receive it explicitly.
_SAFE_ENV_BASELINE = (
    "PATH", "HOME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "TMPDIR", "TEMP", "TMP",
    "SystemRoot", "COMSPEC", "PATHEXT", "WINDIR", "PROGRAMFILES",
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class MCPError(RuntimeError):
    """Generic MCP / JSON-RPC error."""

    def __init__(self, message: str, code: Optional[int] = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class MCPHTTPError(MCPError):
    """HTTP-level MCP error with status code."""

    def __init__(self, message: str, status_code: Optional[int] = None):
        super().__init__(message, code=status_code)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Env / token helpers
# ---------------------------------------------------------------------------

def _get_env_file() -> Path:
    p = Path("/app/.env")
    if p.exists() or Path("/app").exists():
        return p
    return Path(".env")


def _get_token(env_key: str) -> str:
    """Retrieve auth token from environment, falling back to reading .env if not loaded."""
    if not env_key:
        return ""

    token = os.getenv(env_key, "")
    if token:
        return token

    env_file = _get_env_file()
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith(f"{env_key}="):
                    val = line.split("=", 1)[1].strip()
                    if len(val) >= 2 and (
                        (val.startswith('"') and val.endswith('"'))
                        or (val.startswith("'") and val.endswith("'"))
                    ):
                        val = val[1:-1]
                    os.environ[env_key] = val
                    return val
        except Exception:
            pass

    return ""


def _persist_token(env_key: str, auth_token: str) -> Tuple[bool, str]:
    """Write auth token to .env file and environment so it persists across restarts."""
    if not env_key or not auth_token:
        return True, ""

    os.environ[env_key] = auth_token
    env_file = _get_env_file()

    try:
        content = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
        pattern = f"^{re.escape(env_key)}=.*$"

        if re.search(pattern, content, re.MULTILINE):
            content = re.sub(pattern, f"{env_key}={auth_token}", content, flags=re.MULTILINE)
        else:
            if content and not content.endswith("\n"):
                content += "\n"
            content += f"{env_key}={auth_token}\n"

        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text(content, encoding="utf-8")

        try:
            env_file.chmod(0o600)
        except Exception:
            pass

        return True, ""
    except Exception as e:
        return False, f"⚠️ Warning: Token set for current session, but failed to write to .env ({e})."


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------

def _load_servers() -> dict:
    if _CONFIG_FILE.exists():
        try:
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
    return {}


def _save_servers(servers: dict) -> None:
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_FILE.write_text(json.dumps(servers, indent=2, ensure_ascii=False), encoding="utf-8")


def _save_server_info(server_name: str, info: dict) -> None:
    servers = _load_servers()
    info = _ensure_server_defaults(info)
    servers[server_name] = info
    _save_servers(servers)


def _default_tool_policy() -> dict:
    return {
        "default": "enabled",
        "enabled": [],
        "disabled": [],
    }


def _ensure_server_defaults(info: dict) -> dict:
    if not isinstance(info, dict):
        info = {}

    info.setdefault("enabled", True)
    info.setdefault("transport", "http")

    if not isinstance(info.get("tool_policy"), dict):
        info["tool_policy"] = _default_tool_policy()

    policy = info["tool_policy"]
    policy.setdefault("default", "enabled")
    policy.setdefault("enabled", [])
    policy.setdefault("disabled", [])

    return info


def _is_tool_enabled(info: dict, tool_name: str) -> bool:
    if not info.get("enabled", True):
        return False

    policy = info.get("tool_policy", {})
    disabled = policy.get("disabled", [])
    enabled = policy.get("enabled", [])

    if tool_name in disabled:
        return False

    if tool_name in enabled:
        return True

    return policy.get("default", "enabled") == "enabled"


def _set_tool_state(server_name: str, tool_name: str, enabled: bool) -> str:
    servers = _load_servers()

    if server_name not in servers:
        return f"❌ Server '{server_name}' not registered."

    info = _ensure_server_defaults(servers[server_name])
    policy = info["tool_policy"]

    enabled_set = set(policy.get("enabled", []))
    disabled_set = set(policy.get("disabled", []))

    if enabled:
        disabled_set.discard(tool_name)

        if policy.get("default", "enabled") == "disabled":
            enabled_set.add(tool_name)
        else:
            enabled_set.discard(tool_name)
    else:
        enabled_set.discard(tool_name)

        if policy.get("default", "enabled") == "enabled":
            disabled_set.add(tool_name)
        else:
            disabled_set.discard(tool_name)

    policy["enabled"] = sorted(enabled_set)
    policy["disabled"] = sorted(disabled_set)

    servers[server_name] = info
    _save_servers(servers)

    state = "enabled" if enabled else "disabled"
    return f"✅ Tool '{tool_name}' on MCP server '{server_name}' is now {state}."


def _tool_cache_from_tools(tools: List[dict]) -> List[dict]:
    cache = []
    for t in tools:
        if isinstance(t, dict):
            cache.append(
                {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "inputSchema": t.get("inputSchema", {}),
                }
            )
    return cache


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def _expand_env(value: Any) -> Any:
    """Expand ${ENV_VAR} patterns in strings, lists, and dicts."""
    if isinstance(value, str):
        m = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value.strip())
        if m:
            return os.getenv(m.group(1), "")

        return re.sub(
            r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}",
            lambda mm: os.getenv(mm.group(1), ""),
            value,
        )

    if isinstance(value, list):
        return [_expand_env(v) for v in value]

    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}

    return value


def _get_verify(info: dict) -> Union[bool, str]:
    """Return requests verify setting: True, False, or CA bundle path."""
    if "verify" in info:
        v = info["verify"]

        if isinstance(v, bool):
            return v

        if isinstance(v, str):
            lv = v.strip().lower()
            if lv in ("0", "false", "no", "off"):
                return False
            if lv in ("1", "true", "yes", "on"):
                return _TLS_VERIFY_DEFAULT
            return v  # assume path to CA bundle

    if "ca_bundle" in info:
        return str(info["ca_bundle"])

    return _TLS_VERIFY_DEFAULT


def _build_headers(info: dict, extra: Optional[dict] = None) -> dict:
    headers = {
        "Accept": "application/json, text/event-stream",
    }

    token = _get_token(info.get("auth_token_env", ""))
    if token:
        headers["Authorization"] = f"Bearer {token}"

    custom = info.get("headers")
    if isinstance(custom, dict):
        expanded = _expand_env(custom)
        for k, v in expanded.items():
            if v is None:
                continue
            headers[str(k)] = str(v)

    if extra:
        headers.update(extra)

    return headers


def _parse_sse_frames(text: str) -> List[Tuple[str, str]]:
    """Parse simple SSE text into (event, data) frames."""
    frames: List[Tuple[str, str]] = []
    event: Optional[str] = None
    data_lines: List[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()

        if not line:
            if data_lines:
                frames.append((event or "message", "\n".join(data_lines)))
                event = None
                data_lines = []
            continue

        if line.startswith(":"):
            continue

        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())

    if data_lines:
        frames.append((event or "message", "\n".join(data_lines)))

    return frames


def _extract_jsonrpc_from_sse(raw_text: str, expected_id: Optional[str]) -> dict:
    frames = _parse_sse_frames(raw_text)

    parsed_frames = []
    for _event, data in frames:
        try:
            parsed_frames.append(json.loads(data))
        except Exception:
            continue

    if not parsed_frames:
        raise MCPError(f"Could not parse JSON-RPC payload from SSE response: {raw_text[:200]!r}")

    if expected_id is not None:
        for frame in reversed(parsed_frames):
            if isinstance(frame, dict) and frame.get("id") == expected_id:
                return frame

    return parsed_frames[-1]


def _result_from_data(data: Any) -> dict:
    if not isinstance(data, dict):
        return {"value": data}

    if "error" in data:
        err = data.get("error") or {}
        if not isinstance(err, dict):
            err = {}

        msg = err.get("message", "Unknown MCP error")
        code = err.get("code")
        err_data = err.get("data")

        if err_data:
            msg = f"{msg} ({err_data})"

        raise MCPError(msg, code=code, data=err_data)

    result = data.get("result", {})
    if result is None:
        result = {}

    if not isinstance(result, dict):
        result = {"value": result}

    return result


def _is_session_error(err: Exception) -> bool:
    s = str(err).lower()
    session_keywords = (
        "session not found",
        "invalid session",
        "session expired",
        "unknown session",
        "session_id",
        "mcp-session-id",
    )
    return any(k in s for k in session_keywords)


def _is_method_not_found(err: Exception) -> bool:
    code = getattr(err, "code", None)
    if code == -32601:
        return True

    s = str(err).lower()
    return "method not found" in s or "method not supported" in s


def _should_recreate_connection(err: Exception, method: str) -> bool:
    if isinstance(err, MCPHTTPError) and err.status_code in (401, 403):
        return False

    if _is_method_not_found(err):
        return False

    s = str(err).lower()

    # Do not silently retry potentially non-idempotent tool calls on timeout.
    if method == "tools/call" and "timeout" in s:
        return False

    keywords = (
        "process not running",
        "transport not ready",
        "not connected",
        "connection reset",
        "broken pipe",
        "eof occurred",
        "timeout",
        "sse",
        "session closed",
        "network error",
        "failed to connect",
        "connection aborted",
        "connection refused",
    )

    return any(k in s for k in keywords)


def _format_content_item(item: Any) -> str:
    if isinstance(item, str):
        return item

    if not isinstance(item, dict):
        return str(item)

    itype = item.get("type", "")

    if itype == "text":
        return item.get("text", "")

    if itype == "image":
        mime = item.get("mimeType", "image/png")
        data_len = len(item.get("data", ""))
        return f"[Image: mimeType={mime}, data={data_len} bytes base64]"

    if itype == "resource":
        res = item.get("resource", {})
        uri = res.get("uri", "")
        text = res.get("text", "")
        blob = res.get("blob", "")
        detail = text if text else (f"{len(blob)} bytes blob" if blob else "empty")
        return f"[Resource uri='{uri}': {detail}]"

    return f"[{itype or 'content'}: {json.dumps(item, ensure_ascii=False)}]"


def _stderr_tail(lines: deque) -> str:
    if not lines:
        return ""
    return "\n".join(list(lines)[-5:])


# ---------------------------------------------------------------------------
# Streamable HTTP transport
# ---------------------------------------------------------------------------

class StreamableHTTPTransport:
    """Modern MCP Streamable HTTP transport: POST JSON-RPC to one endpoint."""

    def __init__(self, info: dict):
        self.info = info
        self.url = info.get("url", "")
        self.verify = _get_verify(info)
        self.session_id = ""
        self.server_info = {}
        self.lock = threading.Lock()
        self.closed = False

        if not self.url:
            raise MCPError("Streamable HTTP server has no URL configured.")

    def _headers(self) -> dict:
        headers = _build_headers(
            self.info,
            {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )

        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        return headers

    def _capture_session_id(self, resp: requests.Response) -> None:
        sid = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")
        if sid:
            self.session_id = sid

    def _post(self, payload: dict, timeout: Optional[int]) -> requests.Response:
        attempts = [0.0] + _RETRY_DELAYS
        last_err: Optional[Exception] = None
        eff_timeout = timeout if timeout is not None else _TIMEOUT

        for attempt_idx, delay in enumerate(attempts):
            if delay > 0:
                time.sleep(delay)

            try:
                resp = requests.post(
                    self.url,
                    json=payload,
                    headers=self._headers(),
                    timeout=eff_timeout,
                    verify=self.verify,
                )

                if resp.status_code in _TRANSIENT_STATUS_CODES:
                    last_err = MCPHTTPError(
                        f"Transient HTTP {resp.status_code}: {resp.text.strip()[:200]}",
                        status_code=resp.status_code,
                    )
                    continue

                return resp

            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError,
            ) as net_err:
                last_err = net_err
                continue

        raise MCPError(f"Network error after {len(attempts)} attempts: {last_err}")

    def _parse_body(self, resp: requests.Response, req_id: Optional[str]) -> dict:
        raw_text = resp.text.strip()

        if not raw_text:
            return {}

        content_type = resp.headers.get("content-type", "")

        if "text/event-stream" in content_type or raw_text.startswith(("event:", "data:")):
            data = _extract_jsonrpc_from_sse(raw_text, req_id)
        else:
            try:
                data = json.loads(raw_text)
            except json.JSONDecodeError as e:
                raise MCPError(
                    f"Non-JSON response (content-type={content_type!r}): {raw_text[:200]!r}"
                ) from e

        return _result_from_data(data)

    def request(self, method: str, params: Optional[dict] = None, timeout: Optional[int] = None) -> dict:
        with self.lock:
            req_id = str(uuid.uuid4())
            payload: Dict[str, Any] = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
            }

            if params is not None:
                payload["params"] = params

            resp = self._post(payload, timeout)
            self._capture_session_id(resp)

            if resp.status_code in (401, 403):
                raise MCPHTTPError(
                    f"Authentication failed (HTTP {resp.status_code}): {resp.text.strip()[:300]}",
                    status_code=resp.status_code,
                )

            if resp.status_code >= 400:
                raise MCPHTTPError(
                    f"HTTP {resp.status_code} from MCP server: {resp.text.strip()[:400]}",
                    status_code=resp.status_code,
                )

            return self._parse_body(resp, req_id)

    def notify(self, method: str, params: Optional[dict] = None, timeout: Optional[int] = None) -> None:
        with self.lock:
            payload: Dict[str, Any] = {
                "jsonrpc": "2.0",
                "method": method,
            }

            if params is not None:
                payload["params"] = params

            resp = self._post(payload, timeout)
            self._capture_session_id(resp)

            if resp.status_code in (401, 403):
                raise MCPHTTPError(
                    f"Authentication failed (HTTP {resp.status_code}): {resp.text.strip()[:300]}",
                    status_code=resp.status_code,
                )

            if resp.status_code >= 400:
                raise MCPHTTPError(
                    f"HTTP {resp.status_code} from MCP server: {resp.text.strip()[:400]}",
                    status_code=resp.status_code,
                )

    def initialize(self, timeout: Optional[int] = None) -> dict:
        result = self.request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "TrinityClaw", "version": "1.3"},
            },
            timeout=timeout,
        )

        try:
            self.notify("notifications/initialized", {}, timeout=timeout)
        except Exception:
            pass

        if isinstance(result, dict) and "serverInfo" in result:
            self.server_info = result.get("serverInfo", {})

        return result

    def is_alive(self) -> bool:
        return not self.closed and bool(self.url)

    def close(self) -> None:
        self.closed = True


# ---------------------------------------------------------------------------
# Legacy HTTP+SSE transport
# ---------------------------------------------------------------------------

def _iter_sse_response(resp: requests.Response):
    """Yield (event, data) tuples from a streaming requests response."""
    event: Optional[str] = None
    data_lines: List[str] = []

    for raw_line in resp.iter_lines(decode_unicode=True):
        if raw_line is None:
            continue

        line = raw_line.strip()

        if not line:
            if data_lines:
                yield (event or "message", "\n".join(data_lines))
                event = None
                data_lines = []
            continue

        if line.startswith(":"):
            continue

        if line.startswith("event:"):
            event = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())

    if data_lines:
        yield (event or "message", "\n".join(data_lines))


class SSETransport:
    """Legacy MCP HTTP+SSE transport: GET SSE stream, then POST to endpoint."""

    def __init__(self, info: dict):
        self.info = info
        self.sse_url = info.get("url", "")
        self.verify = _get_verify(info)

        self.post_url: Optional[str] = None
        self.endpoint_event = threading.Event()
        self.start_error: Optional[Exception] = None

        self.pending: Dict[str, dict] = {}
        self.pending_lock = threading.Lock()

        self.listener: Optional[threading.Thread] = None
        self.closed = False

        if not self.sse_url:
            raise MCPError("SSE server has no URL configured.")

    def start(self, timeout: Optional[int] = None) -> None:
        self.listener = threading.Thread(target=self._listen, daemon=True, name="mcp-sse-listener")
        self.listener.start()

        eff_timeout = timeout if timeout is not None else _TIMEOUT
        got_endpoint = self.endpoint_event.wait(eff_timeout)

        if self.start_error:
            raise MCPError(f"SSE connection failed: {self.start_error}")

        if not got_endpoint or not self.post_url:
            raise MCPError("Timeout waiting for SSE 'endpoint' event from server.")

    def _fail_pending(self, err: Exception) -> None:
        with self.pending_lock:
            for req_id, entry in list(self.pending.items()):
                entry["error"] = err
                entry["event"].set()
            self.pending.clear()

    def _handle_message(self, msg: dict) -> None:
        if not isinstance(msg, dict):
            return

        req_id = msg.get("id")

        if req_id is not None:
            with self.pending_lock:
                entry = self.pending.get(str(req_id))

            if entry:
                if "error" in msg:
                    err = msg.get("error") or {}
                    entry["error"] = MCPError(
                        err.get("message", "MCP error"),
                        code=err.get("code"),
                        data=err.get("data"),
                    )
                else:
                    entry["result"] = msg.get("result", {})

                entry["event"].set()
                return

        # Respond to unsupported server-initiated requests.
        if "method" in msg and req_id is not None and "result" not in msg and "error" not in msg:
            try:
                self._send_raw(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32601,
                            "message": "Method not supported by TrinityClaw MCP client",
                        },
                    }
                )
            except Exception:
                pass

    def _listen(self) -> None:
        try:
            headers = _build_headers(self.info, {"Accept": "text/event-stream"})

            with requests.get(
                self.sse_url,
                headers=headers,
                stream=True,
                verify=self.verify,
                timeout=(10, None),
            ) as resp:
                if resp.status_code >= 400:
                    self.start_error = MCPHTTPError(
                        f"SSE GET failed with HTTP {resp.status_code}: {resp.text.strip()[:300]}",
                        status_code=resp.status_code,
                    )
                    self.endpoint_event.set()
                    return

                for event, data in _iter_sse_response(resp):
                    if self.closed:
                        break

                    if event == "endpoint":
                        self.post_url = urljoin(str(self.sse_url), data.strip())
                        self.endpoint_event.set()
                    elif event in ("message", ""):
                        try:
                            msg = json.loads(data)
                            self._handle_message(msg)
                        except Exception:
                            continue

        except Exception as e:
            self.start_error = e
            self.endpoint_event.set()
            self._fail_pending(e)

    def _send_raw(self, payload: dict) -> None:
        if not self.post_url:
            raise MCPError("SSE transport not ready: no POST endpoint received.")

        headers = _build_headers(self.info, {"Content-Type": "application/json"})

        resp = requests.post(
            self.post_url,
            json=payload,
            headers=headers,
            timeout=_TIMEOUT,
            verify=self.verify,
        )

        if resp.status_code in (401, 403):
            raise MCPHTTPError(
                f"Authentication failed (HTTP {resp.status_code}): {resp.text.strip()[:300]}",
                status_code=resp.status_code,
            )

        if resp.status_code >= 400:
            raise MCPHTTPError(
                f"HTTP {resp.status_code} from MCP SSE endpoint: {resp.text.strip()[:400]}",
                status_code=resp.status_code,
            )

    def request(self, method: str, params: Optional[dict] = None, timeout: Optional[int] = None) -> dict:
        if self.closed or not self.post_url:
            raise MCPError("SSE transport is not ready.")

        req_id = str(uuid.uuid4())
        event = threading.Event()

        with self.pending_lock:
            self.pending[req_id] = {
                "event": event,
                "result": None,
                "error": None,
            }

        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }

        if params is not None:
            payload["params"] = params

        try:
            self._send_raw(payload)
        except Exception as e:
            with self.pending_lock:
                self.pending.pop(req_id, None)
            raise e

        eff_timeout = timeout if timeout is not None else _TIMEOUT
        completed = event.wait(eff_timeout)

        with self.pending_lock:
            entry = self.pending.pop(req_id, None)

        if not completed:
            if not self.is_alive():
                raise MCPError("SSE transport closed while waiting for response.")
            raise MCPError(f"MCP request '{method}' timed out after {eff_timeout}s")

        if entry is None:
            raise MCPError("MCP response entry disappeared unexpectedly.")

        if entry["error"] is not None:
            raise entry["error"]

        result = entry["result"]
        if result is None:
            result = {}

        if not isinstance(result, dict):
            result = {"value": result}

        return result

    def notify(self, method: str, params: Optional[dict] = None, timeout: Optional[int] = None) -> None:
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }

        if params is not None:
            payload["params"] = params

        self._send_raw(payload)

    def is_alive(self) -> bool:
        return (
            not self.closed
            and bool(self.listener)
            and self.listener.is_alive()
            and bool(self.post_url)
        )

    def close(self) -> None:
        self.closed = True
        self.endpoint_event.set()
        self._fail_pending(MCPError("MCP session closed"))


# ---------------------------------------------------------------------------
# Stdio transport
# ---------------------------------------------------------------------------

class StdioTransport:
    """Local stdio subprocess transport."""

    def __init__(self, info: dict):
        self.info = info
        self.command = info.get("command", "")
        self.args = info.get("args", [])
        self.env = info.get("env", {})

        self.proc: Optional[subprocess.Popen] = None
        self.reader: Optional[threading.Thread] = None
        self.err_reader: Optional[threading.Thread] = None

        self.pending: Dict[str, dict] = {}
        self.pending_lock = threading.Lock()
        self.write_lock = threading.Lock()

        self.stderr_tail: deque = deque(maxlen=30)
        self.closed = False

        if not self.command:
            raise MCPError("stdio server has no command configured.")

    def _build_cmd(self) -> List[str]:
        args = self.args if isinstance(self.args, list) else []

        if isinstance(self.command, str) and not args and " " in self.command.strip():
            return shlex.split(self.command)

        return [str(self.command)] + [str(a) for a in args]

    def start(self, timeout: Optional[int] = None) -> None:
        cmd = self._build_cmd()
        # Security: do NOT pass the full process environment to stdio servers -
        # it contains every secret in .env (API keys, tokens). Pass only a safe
        # baseline plus the explicitly configured env; ${VAR} references in the
        # server config still resolve from the process environment.
        merged_env = {k: os.environ[k] for k in _SAFE_ENV_BASELINE if k in os.environ}

        expanded_env = _expand_env(self.env)
        if isinstance(expanded_env, dict):
            for k, v in expanded_env.items():
                if k and isinstance(k, str):
                    merged_env[k] = str(v)

        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=merged_env,
            )
        except Exception as e:
            raise MCPError(f"Failed to start stdio MCP server '{self.command}': {e}")

        self.reader = threading.Thread(target=self._read_stdout, daemon=True, name="mcp-stdio-reader")
        self.err_reader = threading.Thread(target=self._read_stderr, daemon=True, name="mcp-stderr-reader")

        self.reader.start()
        self.err_reader.start()

    def _fail_pending(self, err: Exception) -> None:
        with self.pending_lock:
            for req_id, entry in list(self.pending.items()):
                entry["error"] = err
                entry["event"].set()
            self.pending.clear()

    def _handle_message(self, msg: dict) -> None:
        if not isinstance(msg, dict):
            return

        req_id = msg.get("id")

        if req_id is not None:
            with self.pending_lock:
                entry = self.pending.get(str(req_id))

            if entry:
                if "error" in msg:
                    err = msg.get("error") or {}
                    entry["error"] = MCPError(
                        err.get("message", "MCP error"),
                        code=err.get("code"),
                        data=err.get("data"),
                    )
                else:
                    entry["result"] = msg.get("result", {})

                entry["event"].set()
                return

        if "method" in msg and req_id is not None and "result" not in msg and "error" not in msg:
            try:
                self._send_raw(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {
                            "code": -32601,
                            "message": "Method not supported by TrinityClaw MCP client",
                        },
                    }
                )
            except Exception:
                pass

    def _read_stdout(self) -> None:
        try:
            if not self.proc or not self.proc.stdout:
                return

            while True:
                line = self.proc.stdout.readline()
                if not line:
                    break

                if self.closed:
                    break

                try:
                    msg = json.loads(line.decode("utf-8").strip())
                    self._handle_message(msg)
                except Exception:
                    continue

            if not self.closed:
                self._fail_pending(
                    MCPError(
                        "stdio MCP server exited unexpectedly.\n"
                        f"stderr tail:\n{_stderr_tail(self.stderr_tail)}"
                    )
                )
        except Exception as e:
            if not self.closed:
                self._fail_pending(e)

    def _read_stderr(self) -> None:
        try:
            if not self.proc or not self.proc.stderr:
                return

            while True:
                line = self.proc.stderr.readline()
                if not line:
                    break

                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self.stderr_tail.append(text)
        except Exception:
            pass

    def _send_raw(self, payload: dict) -> None:
        if not self.proc or not self.proc.stdin:
            raise MCPError("stdio process is not running.")

        if self.proc.poll() is not None:
            raise MCPError(
                "stdio process exited.\n"
                f"stderr tail:\n{_stderr_tail(self.stderr_tail)}"
            )

        data = (json.dumps(payload) + "\n").encode("utf-8")

        with self.write_lock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def request(self, method: str, params: Optional[dict] = None, timeout: Optional[int] = None) -> dict:
        if self.closed or not self.is_alive():
            raise MCPError("stdio transport is not running.")

        req_id = str(uuid.uuid4())
        event = threading.Event()

        with self.pending_lock:
            self.pending[req_id] = {
                "event": event,
                "result": None,
                "error": None,
            }

        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
        }

        if params is not None:
            payload["params"] = params

        try:
            self._send_raw(payload)
        except Exception as e:
            with self.pending_lock:
                self.pending.pop(req_id, None)
            raise e

        eff_timeout = timeout if timeout is not None else _TIMEOUT
        completed = event.wait(eff_timeout)

        with self.pending_lock:
            entry = self.pending.pop(req_id, None)

        if not completed:
            if not self.is_alive():
                raise MCPError(
                    "stdio process exited while waiting for response.\n"
                    f"stderr tail:\n{_stderr_tail(self.stderr_tail)}"
                )
            raise MCPError(f"MCP request '{method}' timed out after {eff_timeout}s")

        if entry is None:
            raise MCPError("MCP response entry disappeared unexpectedly.")

        if entry["error"] is not None:
            raise entry["error"]

        result = entry["result"]
        if result is None:
            result = {}

        if not isinstance(result, dict):
            result = {"value": result}

        return result

    def notify(self, method: str, params: Optional[dict] = None, timeout: Optional[int] = None) -> None:
        payload: Dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
        }

        if params is not None:
            payload["params"] = params

        self._send_raw(payload)

    def is_alive(self) -> bool:
        return not self.closed and bool(self.proc) and self.proc.poll() is None

    def close(self) -> None:
        self.closed = True
        self._fail_pending(MCPError("MCP session closed"))

        if self.proc:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Connection wrapper / registry
# ---------------------------------------------------------------------------

class MCPConnection:
    def __init__(self, server_name: str, info: dict, timeout: Optional[int] = None):
        self.server_name = server_name
        self.info = info
        self.transport_type = str(info.get("transport", "http")).lower()
        self.server_info = {}

        if self.transport_type == "stdio":
            self.transport = StdioTransport(info)
            self.transport.start(timeout=timeout)
            result = self._initialize(timeout)
        elif self.transport_type in ("sse", "http_sse", "http+sse"):
            self.transport = SSETransport(info)
            self.transport.start(timeout=timeout)
            result = self._initialize(timeout)
        else:
            self.transport = StreamableHTTPTransport(info)
            result = self.transport.initialize(timeout=timeout)

        if isinstance(result, dict) and "serverInfo" in result:
            self.server_info = result.get("serverInfo", {})

        info["server_info"] = self.server_info

    def _initialize(self, timeout: Optional[int]) -> dict:
        result = self.transport.request(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "TrinityClaw", "version": "1.3"},
            },
            timeout=timeout,
        )

        try:
            self.transport.notify("notifications/initialized", {}, timeout=timeout)
        except Exception:
            pass

        return result

    def request(self, method: str, params: Optional[dict] = None, timeout: Optional[int] = None) -> dict:
        try:
            return self.transport.request(method, params, timeout)
        except MCPError as e:
            # Streamable HTTP sessions can expire; reinitialize once.
            if self.transport_type not in ("stdio", "sse", "http_sse", "http+sse") and _is_session_error(e):
                self.transport.initialize(timeout)
                return self.transport.request(method, params, timeout)
            raise

    def is_alive(self) -> bool:
        try:
            return self.transport.is_alive()
        except Exception:
            return False

    def close(self) -> None:
        try:
            self.transport.close()
        except Exception:
            pass


_CONNECTIONS: Dict[str, MCPConnection] = {}
_CONNECTION_LOCKS: Dict[str, threading.Lock] = {}
_GLOBAL_CONN_LOCK = threading.Lock()


def _get_conn_lock(server_name: str) -> threading.Lock:
    with _GLOBAL_CONN_LOCK:
        if server_name not in _CONNECTION_LOCKS:
            _CONNECTION_LOCKS[server_name] = threading.Lock()
        return _CONNECTION_LOCKS[server_name]


def _close_connection_unlocked(server_name: str) -> None:
    conn = _CONNECTIONS.pop(server_name, None)
    if conn:
        conn.close()


def close_connection(server_name: str) -> str:
    """Close an active MCP connection if one exists."""
    lock = _get_conn_lock(server_name)
    with lock:
        existed = server_name in _CONNECTIONS
        _close_connection_unlocked(server_name)

    if existed:
        return f"✅ Closed active connection for MCP server '{server_name}'."
    return f"📭 No active connection for MCP server '{server_name}'."


def _get_connection(server_name: str, info: dict, timeout: Optional[int] = None) -> MCPConnection:
    lock = _get_conn_lock(server_name)

    with lock:
        conn = _CONNECTIONS.get(server_name)

        if conn and conn.is_alive():
            return conn

        _close_connection_unlocked(server_name)

        conn = MCPConnection(server_name, info, timeout=timeout)
        _CONNECTIONS[server_name] = conn
        return conn


def _request_server(
    server_name: str,
    info: dict,
    method: str,
    params: Optional[dict] = None,
    timeout: Optional[int] = None,
) -> dict:
    try:
        conn = _get_connection(server_name, info, timeout=timeout)
        return conn.request(method, params, timeout)
    except MCPHTTPError as e:
        if e.status_code in (401, 403):
            raise

        if _should_recreate_connection(e, method):
            close_connection(server_name)
            conn = _get_connection(server_name, info, timeout=timeout)
            return conn.request(method, params, timeout)

        raise
    except Exception as e:
        if _should_recreate_connection(e, method):
            close_connection(server_name)
            conn = _get_connection(server_name, info, timeout=timeout)
            return conn.request(method, params, timeout)

        raise


def list_resources(server_name: str, timeout: Optional[int] = None) -> str:
    """List resources exposed by a registered MCP server."""
    servers = _load_servers()
    if server_name not in servers:
        return f"❌ Server '{server_name}' not registered."
    info = _ensure_server_defaults(servers[server_name])
    if not info.get("enabled", True):
        return f"❌ MCP server '{server_name}' is disabled."
    try:
        result = _request_server(server_name, info, "resources/list", None, timeout=timeout)
        resources = result.get("resources", [])
        if not resources:
            return f"📭 No resources on '{server_name}'."
        lines = [f"📄 Resources on '{server_name}' ({len(resources)}):"]
        for r in resources[:50]:
            if isinstance(r, dict):
                lines.append(f"  {r.get('uri', '?')} — {r.get('name', '')}: {str(r.get('description', ''))[:80]}")
        return "\n".join(lines)
    except MCPError as e:
        if _is_method_not_found(e):
            return f"⚠️ Server '{server_name}' does not support resources."
        return f"❌ resources/list failed: {e}"
    except Exception as e:
        return f"❌ resources/list failed: {e}"


def read_resource(server_name: str, uri: str, timeout: Optional[int] = None) -> str:
    """Read a resource by URI from a registered MCP server."""
    servers = _load_servers()
    if server_name not in servers:
        return f"❌ Server '{server_name}' not registered."
    info = _ensure_server_defaults(servers[server_name])
    if not info.get("enabled", True):
        return f"❌ MCP server '{server_name}' is disabled."
    try:
        result = _request_server(server_name, info, "resources/read", {"uri": uri}, timeout=timeout)
        contents = result.get("contents", [])
        parts = []
        for c in contents if isinstance(contents, list) else [contents]:
            parts.append(_format_content_item(c))
        if not parts:
            return f"📭 Empty resource '{uri}'."
        return "\n".join(parts)
    except MCPError as e:
        if _is_method_not_found(e):
            return f"⚠️ Server '{server_name}' does not support resources."
        return f"❌ resources/read failed: {e}"
    except Exception as e:
        return f"❌ resources/read failed: {e}"


def list_prompts(server_name: str, timeout: Optional[int] = None) -> str:
    """List prompts exposed by a registered MCP server."""
    servers = _load_servers()
    if server_name not in servers:
        return f"❌ Server '{server_name}' not registered."
    info = _ensure_server_defaults(servers[server_name])
    if not info.get("enabled", True):
        return f"❌ MCP server '{server_name}' is disabled."
    try:
        result = _request_server(server_name, info, "prompts/list", None, timeout=timeout)
        prompts = result.get("prompts", [])
        if not prompts:
            return f"📭 No prompts on '{server_name}'."
        lines = [f"💬 Prompts on '{server_name}' ({len(prompts)}):"]
        for p in prompts[:50]:
            if isinstance(p, dict):
                lines.append(f"  {p.get('name', '?')}: {str(p.get('description', ''))[:80]}")
        return "\n".join(lines)
    except MCPError as e:
        if _is_method_not_found(e):
            return f"⚠️ Server '{server_name}' does not support prompts."
        return f"❌ prompts/list failed: {e}"
    except Exception as e:
        return f"❌ prompts/list failed: {e}"


def get_prompt(server_name: str, prompt_name: str, arguments: Optional[dict] = None, timeout: Optional[int] = None) -> str:
    """Get a prompt (rendered messages) from a registered MCP server."""
    servers = _load_servers()
    if server_name not in servers:
        return f"❌ Server '{server_name}' not registered."
    info = _ensure_server_defaults(servers[server_name])
    if not info.get("enabled", True):
        return f"❌ MCP server '{server_name}' is disabled."
    params = {"name": prompt_name}
    if arguments:
        params["arguments"] = arguments
    try:
        result = _request_server(server_name, info, "prompts/get", params, timeout=timeout)
        messages = result.get("messages", [])
        parts = []
        for m in messages if isinstance(messages, list) else []:
            if isinstance(m, dict):
                c = m.get("content", "")
                if isinstance(c, dict):
                    parts.append(f"{m.get('role', '?')}: {c.get('text', json.dumps(c, ensure_ascii=False))[:400]}")
                else:
                    parts.append(f"{m.get('role', '?')}: {str(c)[:400]}")
        if not parts:
            return f"📭 Empty prompt '{prompt_name}'."
        return "\n".join(parts)
    except MCPError as e:
        if _is_method_not_found(e):
            return f"⚠️ Server '{server_name}' does not support prompts."
        return f"❌ prompts/get failed: {e}"
    except Exception as e:
        return f"❌ prompts/get failed: {e}"


def shutdown() -> str:
    """Close all active MCP connections. Called automatically on process exit."""
    with _GLOBAL_CONN_LOCK:
        names = list(_CONNECTIONS.keys())

    for name in names:
        close_connection(name)

    return "✅ Closed all active MCP connections."


atexit.register(shutdown)


# ---------------------------------------------------------------------------
# Tool helpers
# ---------------------------------------------------------------------------

def _fetch_all_tools(server_name: str, info: dict, timeout: Optional[int] = None) -> List[dict]:
    all_tools: List[dict] = []
    cursor: Optional[str] = None
    max_pages = 20

    for _ in range(max_pages):
        params = {"cursor": cursor} if cursor else None

        result = _request_server(
            server_name,
            info,
            "tools/list",
            params=params,
            timeout=timeout,
        )

        tools = result.get("tools", [])
        if isinstance(tools, list):
            all_tools.extend(tools)

        next_cursor = result.get("nextCursor")
        if not next_cursor or next_cursor == cursor:
            break

        cursor = next_cursor

    return all_tools


def _format_test_report(diag: dict) -> str:
    lines = []

    if diag.get("ok"):
        lines.append(f"✅ MCP test for '{diag.get('server')}' succeeded")
    else:
        lines.append(f"❌ MCP test for '{diag.get('server')}' failed")

    if diag.get("destination"):
        lines.append(f"Destination: {diag['destination']}")

    lines.append(f"Transport: {diag.get('transport', 'http')}")

    auth = diag.get("auth", {})
    if auth:
        lines.append(f"Auth: {auth.get('state', 'unknown')}")

    latency = diag.get("latency_ms", {})

    if latency.get("connect") is not None:
        lines.append(f"Connect/init latency: {latency['connect']:.1f}ms")

    if latency.get("ping") is not None:
        lines.append(f"Ping latency: {latency['ping']:.1f}ms")

    if latency.get("tools_list") is not None:
        lines.append(f"Tools/list latency: {latency['tools_list']:.1f}ms")

    tools = diag.get("tools", {})
    if tools.get("total") is not None:
        lines.append(
            f"Tools discovered: {tools.get('enabled', 0)}/{tools.get('total', 0)} enabled"
        )

        disabled = tools.get("disabled_tools", [])
        if disabled:
            lines.append("Disabled tools: " + ", ".join(disabled[:20]))

    errors = diag.get("errors", [])
    if errors:
        lines.append("Errors: " + " | ".join(errors[:5]))

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def connect_server(
    name: str,
    url: str = "",
    auth_token: str = "",
    timeout: Optional[int] = None,
    verify_tools: bool = True,
    config: Optional[dict] = None,
) -> str:
    """
    Register and connect to an MCP server.

    Examples:
      connect_server("example", "https://example.com/mcp", "token")
      connect_server("fs", config={"transport":"stdio","command":"npx","args":["-y","@modelcontextprotocol/server-filesystem"]})
      connect_server("legacy", config={"transport":"sse","url":"https://example.com/sse"})
    """
    try:
        servers = _load_servers()
        existing = _ensure_server_defaults(servers.get(name, {}))

        if config:
            cleaned = {k: v for k, v in config.items() if v is not None}
            existing.update(cleaned)
        elif url:
            existing["url"] = url
            existing.setdefault("transport", "http")

        transport = str(existing.get("transport", "http")).lower()

        if transport != "stdio" and not existing.get("url"):
            return f"❌ Server '{name}' has no URL configured. Provide url or config."

        safe_name = re.sub(r"[^A-Z0-9_]", "_", name.upper()) or "SERVER"
        default_env_key = f"MCP_{safe_name}_TOKEN"

        persist_warning = ""

        if auth_token:
            _, persist_warning = _persist_token(default_env_key, auth_token)
            existing["auth_token_env"] = default_env_key

        # Force a fresh connection with the new config/token.
        close_connection(name)

        conn = _get_connection(name, existing, timeout=timeout)

        existing["connected_at"] = datetime.now().isoformat()
        existing["server_info"] = conn.server_info

        tools_line = ""

        if verify_tools:
            try:
                tools = _fetch_all_tools(name, existing, timeout=timeout)
                existing["tools_cache"] = _tool_cache_from_tools(tools)
                existing["tools_cache_at"] = datetime.now().isoformat()

                enabled_count = sum(
                    1 for t in tools if _is_tool_enabled(existing, t.get("name", ""))
                )

                tools_line = f"\n🛠️ Tools discovered: {len(tools)} ({enabled_count} enabled)"
            except Exception as tool_err:
                tools_line = f"\n⚠️ Connected, but tools/list failed: {tool_err}"

        servers[name] = existing
        _save_servers(servers)

        server_label = existing.get("server_info", {}).get("name", name)

        if transport == "stdio":
            dest = existing.get("command", "stdio")
        else:
            dest = existing.get("url", "?")

        res_str = f"✅ Connected to MCP server '{name}' ({server_label}) at {dest}{tools_line}"

        if persist_warning:
            res_str += f"\n{persist_warning}"

        return res_str

    except Exception as e:
        return f"❌ Failed to connect to '{name}': {e}"


def list_servers() -> str:
    """List all registered MCP servers, auth state, and cached tool counts."""
    servers = _load_servers()

    if not servers:
        return "📭 No MCP servers registered yet. Use connect_server(name, url, auth_token)."

    lines = ["🔌 Registered MCP servers:"]

    for name, info in servers.items():
        info = _ensure_server_defaults(info)

        state = "✅" if info.get("enabled", True) else "⛔"
        has_auth = "🔐" if info.get("auth_token_env") else "🔓"

        tool_summary = ""
        cached = info.get("tools_cache")

        if isinstance(cached, list):
            enabled_count = sum(
                1 for t in cached if _is_tool_enabled(info, t.get("name", ""))
            )
            tool_summary = f" | tools {enabled_count}/{len(cached)} enabled"

        connected = info.get("connected_at", "?")[:10]
        transport = info.get("transport", "http")

        if transport == "stdio":
            dest = info.get("command", "stdio")
        else:
            dest = info.get("url", "?")

        lines.append(
            f"  {state} {has_auth} {name} — {transport}:{dest} (connected {connected}){tool_summary}"
        )

    return "\n".join(lines)


def ping_server(name: str, timeout: int = 10, full: bool = False) -> str:
    """Ping an MCP server. If full=True, run test_server() instead."""
    if full:
        return test_server(name, timeout=timeout, include_tools=True, as_dict=False)

    servers = _load_servers()

    if name not in servers:
        return f"❌ Server '{name}' not registered. Use connect_server() first."

    info = _ensure_server_defaults(servers[name])

    t0 = time.perf_counter()

    try:
        conn = _get_connection(name, info, timeout=timeout)

        try:
            conn.request("ping", timeout=timeout)
        except MCPError as e:
            if _is_method_not_found(e):
                conn.request("tools/list", timeout=timeout)
            else:
                raise

        latency_ms = (time.perf_counter() - t0) * 1000

        token_env = info.get("auth_token_env", "")
        token = _get_token(token_env)

        if not token_env:
            auth_state = "no auth configured"
        elif token:
            auth_state = "token configured"
        else:
            auth_state = "token missing"

        tool_state = ""
        cached = info.get("tools_cache")

        if isinstance(cached, list):
            enabled_count = sum(
                1 for t in cached if _is_tool_enabled(info, t.get("name", ""))
            )
            tool_state = f" | Tools: {enabled_count}/{len(cached)} enabled"
        else:
            tool_state = " | Tools: not cached"

        return f"✅ Server '{name}' is healthy ({latency_ms:.1f}ms latency) | Auth: {auth_state}{tool_state}"

    except Exception as e:
        return f"❌ Server '{name}' ping failed: {e}"


def test_server(
    name: str,
    timeout: int = 15,
    include_tools: bool = True,
    as_dict: bool = False,
) -> Union[dict, str]:
    """Detailed MCP server health check."""
    servers = _load_servers()

    if name not in servers:
        diag = {
            "server": name,
            "ok": False,
            "destination": "",
            "transport": "unknown",
            "auth": {"state": "unknown"},
            "latency_ms": {},
            "tools": {"total": None, "enabled": None, "disabled": None, "disabled_tools": []},
            "errors": [f"Server '{name}' not registered."],
        }
        return diag if as_dict else f"❌ Server '{name}' not registered. Use connect_server() first."

    info = _ensure_server_defaults(servers[name])
    token_env = info.get("auth_token_env", "")
    token = _get_token(token_env)
    transport = str(info.get("transport", "http")).lower()

    destination = info.get("command", "") if transport == "stdio" else info.get("url", "")

    diag: Dict[str, Any] = {
        "server": name,
        "ok": False,
        "destination": destination,
        "transport": transport,
        "auth": {
            "token_env": token_env,
            "configured": bool(token_env),
            "token_present": bool(token),
            "state": "unknown",
        },
        "latency_ms": {},
        "tools": {
            "total": None,
            "enabled": None,
            "disabled": None,
            "disabled_tools": [],
        },
        "errors": [],
    }

    try:
        t0 = time.perf_counter()
        conn = _get_connection(name, info, timeout=timeout)
        diag["latency_ms"]["connect"] = (time.perf_counter() - t0) * 1000
        diag["ok"] = True

        t1 = time.perf_counter()

        try:
            conn.request("ping", timeout=timeout)
            diag["latency_ms"]["ping"] = (time.perf_counter() - t1) * 1000
        except MCPError as e:
            if _is_method_not_found(e):
                conn.request("tools/list", timeout=timeout)
                diag["latency_ms"]["ping"] = (time.perf_counter() - t1) * 1000
            else:
                raise

    except MCPHTTPError as e:
        diag["errors"].append(str(e))
        if e.status_code in (401, 403):
            diag["auth"]["state"] = "token rejected"
    except Exception as e:
        diag["errors"].append(str(e))

    if include_tools:
        t2 = time.perf_counter()

        try:
            tools = _fetch_all_tools(name, info, timeout=timeout)

            diag["latency_ms"]["tools_list"] = (time.perf_counter() - t2) * 1000
            diag["tools"]["total"] = len(tools)

            enabled = []
            disabled = []

            for t in tools:
                tname = t.get("name", "")

                if _is_tool_enabled(info, tname):
                    enabled.append(tname)
                else:
                    disabled.append(tname)

            diag["tools"]["enabled"] = len(enabled)
            diag["tools"]["disabled"] = len(disabled)
            diag["tools"]["disabled_tools"] = disabled[:50]

            info["tools_cache"] = _tool_cache_from_tools(tools)
            info["tools_cache_at"] = datetime.now().isoformat()

            diag["ok"] = True

        except Exception as e:
            diag["errors"].append(str(e))

    if diag["auth"]["state"] == "unknown":
        if not token_env:
            diag["auth"]["state"] = "no auth configured"
        elif not token:
            diag["auth"]["state"] = "token missing"
        elif diag["ok"]:
            diag["auth"]["state"] = "token configured and accepted"
        else:
            diag["auth"]["state"] = "token configured but health check failed"

    info["last_test"] = {
        "at": datetime.now().isoformat(),
        "ok": diag["ok"],
        "auth_state": diag["auth"]["state"],
        "errors": diag["errors"][:3],
    }

    _save_server_info(name, info)

    return diag if as_dict else _format_test_report(diag)


def list_tools(server_name: str, timeout: Optional[int] = None) -> str:
    """Discover tools available on a registered MCP server."""
    servers = _load_servers()

    if server_name not in servers:
        return f"❌ Server '{server_name}' not registered."

    info = _ensure_server_defaults(servers[server_name])

    try:
        tools = _fetch_all_tools(server_name, info, timeout=timeout)
    except Exception as e:
        return f"❌ Failed to list tools on '{server_name}': {e}"

    if not tools:
        info["tools_cache"] = []
        info["tools_cache_at"] = datetime.now().isoformat()
        _save_server_info(server_name, info)
        return f"📭 No tools exposed by '{server_name}'"

    enabled_count = sum(1 for t in tools if _is_tool_enabled(info, t.get("name", "")))

    lines = [
        f"🛠️ Tools on '{server_name}' ({enabled_count}/{len(tools)} enabled):"
    ]

    for t in tools:
        tname = t.get("name", "")
        desc = (t.get("description") or "").strip()[:100]
        mark = "✅" if _is_tool_enabled(info, tname) else "⛔"
        lines.append(f"  {mark} {tname}: {desc}")

    info["tools_cache"] = _tool_cache_from_tools(tools)
    info["tools_cache_at"] = datetime.now().isoformat()
    _save_server_info(server_name, info)

    return "\n".join(lines)


def list_all_mcp_tools(refresh: bool = False, timeout: Optional[int] = None) -> str:
    """List MCP tools across all enabled MCP servers with source tags."""
    servers = _load_servers()

    if not servers:
        return "📭 No MCP servers registered."

    lines: List[str] = []
    total_tools = 0
    total_enabled = 0

    for name, info in servers.items():
        info = _ensure_server_defaults(info)

        if not info.get("enabled", True):
            continue

        cached = info.get("tools_cache")

        if refresh or not isinstance(cached, list):
            try:
                tools = _fetch_all_tools(name, info, timeout=timeout)
                cached = _tool_cache_from_tools(tools)
                info["tools_cache"] = cached
                info["tools_cache_at"] = datetime.now().isoformat()
                _save_server_info(name, info)
            except Exception as e:
                lines.append(f"⚠️ Failed to fetch tools from '{name}': {e}")
                cached = info.get("tools_cache") if isinstance(info.get("tools_cache"), list) else []

        if not cached:
            continue

        for tool in cached:
            tool_name = tool.get("name", "")
            if not tool_name:
                continue

            enabled = _is_tool_enabled(info, tool_name)
            total_tools += 1

            if enabled:
                total_enabled += 1

            mark = "✅" if enabled else "⛔"
            lines.append(f"{mark} mcp.{name}.{tool_name} [mcp:{name}]")

    if not lines:
        return "📭 No MCP tools found."

    header = f"🧰 MCP tools ({total_enabled}/{total_tools} enabled):"
    return "\n".join([header] + lines)


def enable_tool(server_name: str, tool_name: str) -> str:
    """Enable a specific MCP tool for a server."""
    return _set_tool_state(server_name, tool_name, True)


def disable_tool(server_name: str, tool_name: str) -> str:
    """Disable a specific MCP tool for a server."""
    return _set_tool_state(server_name, tool_name, False)


def set_server_enabled(server_name: str, enabled: bool) -> str:
    """Enable or disable an entire MCP server."""
    servers = _load_servers()

    if server_name not in servers:
        return f"❌ Server '{server_name}' not registered."

    info = _ensure_server_defaults(servers[server_name])
    info["enabled"] = bool(enabled)

    servers[server_name] = info
    _save_servers(servers)

    if not enabled:
        close_connection(server_name)

    return f"✅ MCP server '{server_name}' is now {'enabled' if enabled else 'disabled'}."


def call_tool(
    server_name: str,
    tool_name: str,
    arguments: Union[str, dict] = "{}",
    arguments_json: Optional[Union[str, dict]] = None,
    timeout: Optional[int] = None,
) -> str:
    """Call a tool on a registered MCP server."""
    servers = _load_servers()

    if server_name not in servers:
        return f"❌ Server '{server_name}' not registered."

    info = _ensure_server_defaults(servers[server_name])

    if not info.get("enabled", True):
        return f"❌ MCP server '{server_name}' is disabled."

    if not _is_tool_enabled(info, tool_name):
        return (
            f"❌ Tool '{tool_name}' is disabled on MCP server '{server_name}'. "
            f"Use enable_tool('{server_name}', '{tool_name}') to allow it."
        )

    raw_args = arguments_json if arguments_json is not None else arguments

    if isinstance(raw_args, dict):
        args = raw_args
    elif isinstance(raw_args, str):
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError as e:
            return f"❌ Invalid JSON in arguments: {raw_args} ({e})"
    else:
        return f"❌ Arguments must be a dict or JSON string, got {type(raw_args).__name__}"

    try:
        result = _request_server(
            server_name,
            info,
            "tools/call",
            {"name": tool_name, "arguments": args},
            timeout=timeout,
        )

        output_parts: List[str] = []

        content = result.get("content", [])
        if isinstance(content, list):
            for c in content:
                formatted = _format_content_item(c)
                if formatted:
                    output_parts.append(formatted)
        elif content:
            output_parts.append(_format_content_item(content))

        structured = result.get("structuredContent")
        if structured is not None:
            output_parts.append(json.dumps(structured, indent=2, ensure_ascii=False))

        if output_parts:
            return "\n".join(output_parts)

        return json.dumps(result, ensure_ascii=False)

    except Exception as e:
        return f"❌ Tool call to '{tool_name}' on '{server_name}' failed: {e}"


def remove_server(name: str) -> str:
    """Unregister an MCP server and close any active connection."""
    close_connection(name)

    servers = _load_servers()

    if name in servers:
        info = servers.pop(name)
        _save_servers(servers)

        env_key = info.get("auth_token_env")

        if env_key:
            os.environ.pop(env_key, None)

            env_file = _get_env_file()
            if env_file.exists():
                try:
                    content = env_file.read_text(encoding="utf-8")
                    pattern = rf"^{re.escape(env_key)}=.*(\r?\n)?"
                    content = re.sub(pattern, "", content, flags=re.MULTILINE)
                    env_file.write_text(content, encoding="utf-8")
                except Exception:
                    pass

        return f"✅ Removed server '{name}' from registry."

    return f"❌ Server '{name}' not found."


# ---------------------------------------------------------------------------
# Workspace discovery
# ---------------------------------------------------------------------------

def _find_workspace_config(root: str = ".") -> Optional[Path]:
    root_path = Path(root).resolve()

    if not root_path.exists():
        return None

    dirs = [root_path] + list(root_path.parents)

    for d in dirs:
        for rel in _WORKSPACE_CONFIG_CANDIDATES:
            p = d / rel
            if p.exists():
                return p

    return None


def _sanitize_workspace_env(env: Any) -> dict:
    if not isinstance(env, dict):
        return {}

    clean = {}

    for k, v in env.items():
        if not isinstance(k, str):
            continue

        key = k.strip()
        upper = key.upper()

        if not key or upper in _DANGEROUS_ENV_KEYS:
            continue

        if isinstance(v, (str, int, float, bool)):
            clean[key] = str(v)

    return clean


def discover_workspace_servers(root: str = ".", register: bool = False, trust: bool = False) -> str:
    """
    Discover workspace-local MCP config files.

    Safe defaults:
    - register=False, trust=False → dry-run discovery only
    - register=True, trust=False → refuses to register
    - register=True, trust=True → registers discovered servers as disabled
    """
    path = _find_workspace_config(root)

    if not path:
        return "📭 No workspace MCP config found. Looked for: " + ", ".join(_WORKSPACE_CONFIG_CANDIDATES)

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return f"❌ Failed to parse workspace MCP config {path}: {e}"

    raw_servers = data.get("servers") or data.get("mcpServers") or {}

    if not isinstance(raw_servers, dict):
        return f"❌ Workspace MCP config {path} must contain a 'servers' or 'mcpServers' object."

    discovered: List[Tuple[str, dict]] = []
    unsupported: List[str] = []

    for sname, cfg in raw_servers.items():
        if isinstance(cfg, str):
            cfg = {"url": cfg}

        if not isinstance(cfg, dict):
            unsupported.append(f"{sname} (invalid config)")
            continue

        command = cfg.get("command")
        url = cfg.get("url")

        if command:
            command_str = str(command)

            if re.search(r"[;&|<>$`]", command_str):
                unsupported.append(f"{sname} (unsafe command)")
                continue

            discovered.append(
                (
                    sname,
                    {
                        "transport": "stdio",
                        "command": command_str,
                        "args": cfg.get("args", []),
                        "env": _sanitize_workspace_env(cfg.get("env", {})),
                    },
                )
            )
        elif url:
            transport = str(cfg.get("transport", "http")).lower()

            if transport not in ("http", "streamable-http", "streamable_http", "sse", "http_sse", "http+sse"):
                transport = "http"

            discovered.append(
                (
                    sname,
                    {
                        "transport": transport,
                        "url": str(url),
                        "auth_token_env": str(cfg.get("auth_token_env", "")),
                    },
                )
            )
        else:
            unsupported.append(f"{sname} (no command or url)")

    lines = [f"📁 Workspace MCP config: {path}"]

    if discovered:
        lines.append("Discovered MCP servers:")
        for sname, cfg in discovered:
            dest = cfg.get("url") or cfg.get("command")
            lines.append(f"  • {sname} — {cfg.get('transport')}: {dest}")
    else:
        lines.append("📭 No usable MCP servers found in workspace config.")

    if unsupported:
        lines.append("Unsupported entries: " + ", ".join(unsupported))

    if not register:
        lines.append(
            "Dry-run only. Use discover_workspace_servers(register=True, trust=True) "
            "to register discovered servers as disabled."
        )
        return "\n".join(lines)

    if not trust:
        lines.append("⚠️ Not registering because trust=False. Re-run with trust=True if you trust this workspace.")
        return "\n".join(lines)

    servers = _load_servers()
    added: List[str] = []
    skipped: List[str] = []

    for sname, cfg in discovered:
        if sname in servers:
            skipped.append(sname)
            continue

        info = _ensure_server_defaults(cfg)
        info.update(
            {
                "enabled": False,
                "source": "workspace",
                "workspace_config": str(path),
                "discovered_at": datetime.now().isoformat(),
            }
        )

        servers[sname] = info
        added.append(sname)

    _save_servers(servers)

    if added:
        lines.append("Registered servers (disabled by default): " + ", ".join(added))
        lines.append("Use set_server_enabled(name, True) to activate them.")

    if skipped:
        lines.append("Skipped existing servers: " + ", ".join(skipped))

    return "\n".join(lines)