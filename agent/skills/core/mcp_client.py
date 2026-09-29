"""
MCP Client — connects Trinity to remote MCP servers (Slack, HubSpot, Cloudflare,
Google Workspace, Google Maps, etc.) so their tools become callable without
writing a custom skill per service.

Security notes:
  - Auth tokens are stored as env vars in .env (referenced by name in mcp_servers.json),
    persisted across restarts and never written in plaintext inside the server registry file.
  - Tool results are returned as plain text/JSON for the caller (app.py) to run
    through the same _sanitize_external_content() pipeline used for ChromaDB
    and lessons.jsonl — remote MCP tool output is untrusted external content.
  - Register only the minimum OAuth scopes needed for each service at the
    provider's end (e.g. read-only Slack scopes) — this skill has no way to
    enforce scoping itself; that must be done when you create the token.
"""
import json
import os
import re
import time
import requests
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Union, Any

NAME = "mcp_client"
SHORT_DOC = "Connect to external MCP servers (Slack, HubSpot, Cloudflare, Google, etc.), discover tools, and call them."
DOC = (
    "Generic MCP client — register remote MCP servers once, then discover and call their tools "
    "without writing a custom skill per service. "
    "Functions: "
    "connect_server(name, url, auth_token?, timeout?)→register and test connection to a remote MCP server; "
    "auth_token is stored in .env and loaded into environment, never in the registry file; "
    "list_servers()→show all registered MCP servers and when they last connected; "
    "ping_server(name, timeout?)→test server liveness and measure latency (auto-reinitializes on stale sessions); "
    "list_tools(server_name, timeout?)→discover available tools on a registered server (with cursor pagination and session recovery); "
    "call_tool(server_name, tool_name, arguments?, timeout?)→invoke a tool (accepts JSON string or dict, returns text/images/resources/structuredContent); "
    "remove_server(name)→unregister a server."
)

_CONFIG_FILE = Path(os.getenv("MCP_CONFIG_FILE", "/app/memory/mcp_servers.json"))
if not _CONFIG_FILE.parent.exists() and not Path("/app").exists():
    _CONFIG_FILE = Path("memory/mcp_servers.json")

_TIMEOUT = int(os.getenv("MCP_CLIENT_TIMEOUT", "30"))
_PROTOCOL_VERSION = "2025-06-18"
_RETRY_DELAYS = [1.0, 3.0]
_TRANSIENT_STATUS_CODES = {429, 502, 503, 504}


def _get_env_file() -> Path:
    """Resolve .env path in container or local workspace."""
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
    # Fallback: check .env directly in case environment wasn't reloaded
    env_file = _get_env_file()
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith(f"{env_key}="):
                    val = line.split("=", 1)[1].strip()
                    if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                        val = val[1:-1]
                    os.environ[env_key] = val
                    return val
        except Exception:
            pass
    return ""


def _persist_token(env_key: str, auth_token: str) -> Tuple[bool, str]:
    """Write auth token to .env file and environment so it persists across container/process restarts."""
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
        return True, ""
    except Exception as e:
        return False, f"⚠️ Warning: Token set for current session, but failed to write to .env ({e}). It may vanish on restart."


def _load_servers() -> dict:
    if _CONFIG_FILE.exists():
        try:
            return json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_servers(servers: dict) -> None:
    _CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_FILE.write_text(json.dumps(servers, indent=2), encoding="utf-8")


def _format_content_item(item: Any) -> str:
    """Format an MCP content block (text, image, resource, or generic)."""
    if isinstance(item, str):
        return item
    if not isinstance(item, dict):
        return str(item)

    itype = item.get("type", "")
    if itype == "text":
        return item.get("text", "")
    elif itype == "image":
        mime = item.get("mimeType", "image/png")
        data_len = len(item.get("data", ""))
        return f"[Image: mimeType={mime}, data={data_len} bytes base64]"
    elif itype == "resource":
        res = item.get("resource", {})
        uri = res.get("uri", "")
        text = res.get("text", "")
        blob = res.get("blob", "")
        detail = text if text else (f"{len(blob)} bytes blob" if blob else "empty")
        return f"[Resource uri='{uri}': {detail}]"
    else:
        return f"[{itype or 'content'}: {json.dumps(item, ensure_ascii=False)}]"


def _is_session_error(err: Exception) -> bool:
    """Check if an exception indicates a stale, expired, or invalid Mcp-Session-Id."""
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


def _reinitialize_session(server_name: str, info: dict, timeout: Optional[int] = None) -> str:
    """Re-run initialization handshake to get a fresh session ID when an existing session is expired/invalid."""
    token = _get_token(info.get("auth_token_env", ""))
    url = info["url"]

    # 1. initialize request
    result, resp_session_id = _rpc(
        url=url,
        token=token,
        method="initialize",
        params={
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "TrinityClaw", "version": "1.3"},
        },
        timeout=timeout,
    )

    new_sid = resp_session_id or ""

    # 2. notifications/initialized follow-up
    try:
        _rpc(
            url=url,
            token=token,
            method="notifications/initialized",
            session_id=new_sid,
            is_notification=True,
            timeout=timeout,
        )
    except Exception:
        pass

    info["session_id"] = new_sid
    if "serverInfo" in result:
        info["server_info"] = result["serverInfo"]
    servers = _load_servers()
    servers[server_name] = info
    _save_servers(servers)
    return new_sid


def _rpc(
    url: str,
    token: str = "",
    method: str = "",
    params: Optional[dict] = None,
    session_id: str = "",
    is_notification: bool = False,
    timeout: Optional[int] = None,
) -> Tuple[dict, Optional[str]]:
    """JSON-RPC 2.0 call over MCP's Streamable HTTP transport.

    Handles:
    - Authorization header if token is provided
    - Mcp-Session-Id header forwarding & tracking
    - Retries (2x with 1s -> 3s backoff) for transient errors (connection, timeout, 429/502/503/504)
    - Detailed HTTP error extraction (401/403 token auth errors vs others)
    - Robust SSE event parsing matching request JSON-RPC id
    - JSON-RPC notifications (no id, no response expected)
    - Both JSON and SSE stream responses
    """
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    payload = {"jsonrpc": "2.0", "method": method}
    if not is_notification:
        payload["id"] = 1
    if params is not None:
        payload["params"] = params

    eff_timeout = timeout if timeout is not None else _TIMEOUT

    attempts = [0.0] + _RETRY_DELAYS
    last_err: Optional[Exception] = None

    for attempt_idx, delay in enumerate(attempts):
        if delay > 0:
            time.sleep(delay)
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=eff_timeout)

            # Check transient HTTP status codes (retryable)
            if resp.status_code in _TRANSIENT_STATUS_CODES:
                if attempt_idx < len(attempts) - 1:
                    last_err = RuntimeError(f"Transient HTTP {resp.status_code}: {resp.text.strip()[:200]}")
                    continue
                raise RuntimeError(
                    f"HTTP {resp.status_code} from MCP server after {len(attempts)} attempts: {resp.text.strip()[:300]}"
                )

            # Check non-transient HTTP errors (fail immediately)
            if resp.status_code >= 400:
                body_snippet = resp.text.strip()[:400]
                if resp.status_code in (401, 403):
                    raise RuntimeError(
                        f"Authentication failed (HTTP {resp.status_code}): token may be missing, expired, or lacking required OAuth scopes. "
                        f"Server response: {body_snippet}"
                    )
                raise RuntimeError(f"HTTP {resp.status_code} from MCP server: {body_snippet}")

            # Capture session ID returned by server in headers (Mcp-Session-Id)
            resp_session_id = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")

            # Notifications are one-way; server may return 204 or empty 200/202
            if is_notification:
                return {}, resp_session_id

            content_type = resp.headers.get("content-type", "")
            raw_text = resp.text.strip()

            if not raw_text:
                raise RuntimeError("Empty response body from MCP server")

            if "text/event-stream" in content_type or raw_text.startswith(("event:", "data:")):
                # Parse SSE frames — extract all data: lines
                data_lines = [
                    line[len("data:"):].strip()
                    for line in raw_text.splitlines()
                    if line.startswith("data:")
                ]
                if not data_lines:
                    raise RuntimeError(f"SSE response had no data lines: {raw_text[:200]!r}")

                parsed_frames = []
                for dline in data_lines:
                    try:
                        parsed_frames.append(json.loads(dline))
                    except json.JSONDecodeError:
                        continue

                if not parsed_frames:
                    raise RuntimeError(f"Could not parse any SSE payload as JSON from data lines: {data_lines[-1][:200]!r}")

                expected_id = payload.get("id")
                data = None

                if expected_id is not None:
                    # Prefer frame matching requested JSON-RPC id
                    for frame in reversed(parsed_frames):
                        if isinstance(frame, dict) and frame.get("id") == expected_id:
                            data = frame
                            break

                if data is None:
                    # Fallback to last valid JSON frame
                    data = parsed_frames[-1]
            else:
                try:
                    data = json.loads(raw_text)
                except json.JSONDecodeError as e:
                    raise RuntimeError(
                        f"Non-JSON response (content-type={content_type!r}): {raw_text[:200]!r}"
                    ) from e

            if "error" in data:
                err_msg = data["error"].get("message", "Unknown MCP error")
                err_data = data["error"].get("data")
                if err_data:
                    err_msg = f"{err_msg} ({err_data})"
                raise RuntimeError(err_msg)

            return data.get("result", {}), resp_session_id

        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError) as net_err:
            last_err = net_err
            if attempt_idx < len(attempts) - 1:
                continue
            raise RuntimeError(f"Network error after {len(attempts)} attempts: {net_err}") from net_err

    if last_err:
        raise last_err
    raise RuntimeError("RPC call failed without specific error")


def connect_server(name: str, url: str, auth_token: str = "", timeout: Optional[int] = None) -> str:
    """Register a remote MCP server and verify the connection with an initialize handshake.
    auth_token (if provided) is persisted to .env and loaded into the environment."""
    try:
        env_key = f"MCP_{name.upper()}_TOKEN"
        persist_warning = ""
        if auth_token:
            _, persist_warning = _persist_token(env_key, auth_token)

        # 1. initialize request
        result, resp_session_id = _rpc(
            url=url,
            token=auth_token,
            method="initialize",
            params={
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "TrinityClaw", "version": "1.3"},
            },
            timeout=timeout,
        )

        session_id = resp_session_id or ""

        # 2. notifications/initialized follow-up (required by MCP specification)
        try:
            _rpc(
                url=url,
                token=auth_token,
                method="notifications/initialized",
                session_id=session_id,
                is_notification=True,
                timeout=timeout,
            )
        except Exception:
            # Spec requires sending initialized notification; continue even if server is stateless
            pass

        servers = _load_servers()
        servers[name] = {
            "url": url,
            "auth_token_env": env_key if auth_token else "",
            "connected_at": datetime.now().isoformat(),
            "server_info": result.get("serverInfo", {}),
            "session_id": session_id,
        }
        _save_servers(servers)
        server_label = result.get("serverInfo", {}).get("name", name)
        res_str = f"✅ Connected to MCP server '{name}' ({server_label}) at {url}"
        if persist_warning:
            res_str += f"\n{persist_warning}"
        return res_str
    except Exception as e:
        return f"❌ Failed to connect to '{name}': {e}"


def list_servers() -> str:
    """List all registered MCP servers and when they last connected successfully."""
    servers = _load_servers()
    if not servers:
        return "📭 No MCP servers registered yet. Use connect_server(name, url, auth_token)."
    lines = ["🔌 Registered MCP servers:"]
    for name, info in servers.items():
        has_auth = "🔐" if info.get("auth_token_env") else "🔓"
        lines.append(f"  {has_auth} {name} — {info['url']} (connected {info.get('connected_at', '?')[:10]})")
    return "\n".join(lines)


def ping_server(name: str, timeout: int = 10) -> str:
    """Ping a registered MCP server to verify responsiveness and measure latency. Auto-reinitializes stale sessions."""
    servers = _load_servers()
    if name not in servers:
        return f"❌ Server '{name}' not registered. Use connect_server() first."
    info = servers[name]
    token = _get_token(info.get("auth_token_env", ""))
    session_id = info.get("session_id", "")
    t0 = time.perf_counter()
    try:
        try:
            _, new_sid = _rpc(
                url=info["url"],
                token=token,
                method="ping",
                session_id=session_id,
                timeout=timeout,
            )
        except Exception as e:
            if session_id and _is_session_error(e):
                new_sid_recon = _reinitialize_session(name, info, timeout)
                _, new_sid = _rpc(
                    url=info["url"],
                    token=token,
                    method="ping",
                    session_id=new_sid_recon,
                    timeout=timeout,
                )
            else:
                raise e

        latency_ms = (time.perf_counter() - t0) * 1000
        if new_sid and new_sid != session_id:
            info["session_id"] = new_sid
            _save_servers(servers)
        return f"✅ Server '{name}' is healthy and responsive ({latency_ms:.1f}ms latency) at {info['url']}"
    except Exception as e:
        return f"❌ Server '{name}' ping failed: {e}"


def list_tools(server_name: str, timeout: Optional[int] = None) -> str:
    """Discover tools available on a registered MCP server, handling pagination cursors and stale session recovery."""
    servers = _load_servers()
    if server_name not in servers:
        return f"❌ Server '{server_name}' not registered. Use connect_server() first."
    info = servers[server_name]
    token = _get_token(info.get("auth_token_env", ""))
    session_id = info.get("session_id", "")

    def _fetch_all_tools(cur_sid: str) -> Tuple[List[dict], str]:
        all_tools: List[dict] = []
        cursor: Optional[str] = None
        max_pages = 20

        for _ in range(max_pages):
            params: dict = {}
            if cursor:
                params["cursor"] = cursor

            result, new_sid = _rpc(
                url=info["url"],
                token=token,
                method="tools/list",
                params=params if params else None,
                session_id=cur_sid,
                timeout=timeout,
            )
            if new_sid and new_sid != cur_sid:
                cur_sid = new_sid
                info["session_id"] = new_sid
                _save_servers(servers)

            tools = result.get("tools", [])
            if isinstance(tools, list):
                all_tools.extend(tools)

            next_cursor = result.get("nextCursor")
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return all_tools, cur_sid

    try:
        try:
            tools, _ = _fetch_all_tools(session_id)
        except Exception as e:
            if session_id and _is_session_error(e):
                new_sid = _reinitialize_session(server_name, info, timeout)
                tools, _ = _fetch_all_tools(new_sid)
            else:
                raise e

        if not tools:
            return f"📭 No tools exposed by '{server_name}'"
        lines = [f"🛠️ Tools on '{server_name}' ({len(tools)} total):"]
        for t in tools:
            desc = (t.get("description") or "").strip()[:100]
            lines.append(f"  • {t['name']}: {desc}")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Failed to list tools on '{server_name}': {e}"


def call_tool(
    server_name: str,
    tool_name: str,
    arguments: Union[str, dict] = "{}",
    arguments_json: Optional[Union[str, dict]] = None,
    timeout: Optional[int] = None,
) -> str:
    """Call a tool on a registered MCP server.

    arguments can be a JSON string or a python dict of tool parameters.
    timeout overrides the default per-call timeout in seconds.
    Auto-reinitializes handshake if the session is stale or expired.
    """
    servers = _load_servers()
    if server_name not in servers:
        return f"❌ Server '{server_name}' not registered."
    info = servers[server_name]
    token = _get_token(info.get("auth_token_env", ""))
    session_id = info.get("session_id", "")

    # Handle arguments parameter (or arguments_json alias)
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
        try:
            result, new_sid = _rpc(
                url=info["url"],
                token=token,
                method="tools/call",
                params={"name": tool_name, "arguments": args},
                session_id=session_id,
                timeout=timeout,
            )
        except Exception as e:
            if session_id and _is_session_error(e):
                new_sid_recon = _reinitialize_session(server_name, info, timeout)
                result, new_sid = _rpc(
                    url=info["url"],
                    token=token,
                    method="tools/call",
                    params={"name": tool_name, "arguments": args},
                    session_id=new_sid_recon,
                    timeout=timeout,
                )
            else:
                raise e

        if new_sid and new_sid != session_id:
            info["session_id"] = new_sid
            _save_servers(servers)

        output_parts: List[str] = []

        # 1. Process content items (text, image, resource, etc.)
        content = result.get("content", [])
        if isinstance(content, list):
            for c in content:
                formatted = _format_content_item(c)
                if formatted:
                    output_parts.append(formatted)
        elif content:
            output_parts.append(_format_content_item(content))

        # 2. Process structuredContent (newer MCP specification)
        structured = result.get("structuredContent")
        if structured is not None:
            output_parts.append(json.dumps(structured, indent=2, ensure_ascii=False))

        if output_parts:
            return "\n".join(output_parts)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return f"❌ Tool call to '{tool_name}' on '{server_name}' failed: {e}"


def remove_server(name: str) -> str:
    """Unregister an MCP server. Does not revoke the token at the provider — do that separately."""
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
                    pattern = f"^{re.escape(env_key)}=.*(\n|\r\n)?"
                    content = re.sub(pattern, "", content, flags=re.MULTILINE)
                    env_file.write_text(content, encoding="utf-8")
                except Exception:
                    pass
        return f"✅ Removed server '{name}' from registry (revoke its token at the provider separately)"
    return f"❌ Server '{name}' not found"


__all__ = [
    "NAME", "DOC", "SHORT_DOC",
    "connect_server", "list_servers", "ping_server", "list_tools", "call_tool", "remove_server",
]