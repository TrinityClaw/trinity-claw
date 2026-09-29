"""
MCP Client — connects Trinity to remote MCP servers (Slack, HubSpot, Cloudflare,
Google Workspace, Google Maps, etc.) so their tools become callable without
writing a custom skill per service.

Security notes:
- Auth tokens are stored as env vars in .env (referenced by name in
  mcp_servers.json), persisted across restarts and never written in plaintext
  inside the server registry file.
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
SHORT_DOC = "Connect to external MCP servers, discover tools, filter tools, and call them."
DOC = (
    "Generic MCP client — register remote MCP servers once, then discover, test, filter, and call their tools. "
    "Functions: "
    "connect_server(name, url, auth_token?, timeout?, verify_tools?)→register and test connection to a remote MCP server; "
    "auth_token is stored in .env and loaded into environment, never in the registry file; "
    "list_servers()→show all registered MCP servers, auth state, and cached tool counts; "
    "ping_server(name, timeout?, full?)→quick liveness check with auth/tool summary, or full test when full=True; "
    "test_server(name, timeout?, include_tools?, as_dict?)→detailed diagnostics: auth, latency, tools discovered, enabled/disabled counts; "
    "list_tools(server_name, timeout?)→discover available tools on a registered server and show enabled/disabled state; "
    "list_all_mcp_tools(refresh?, timeout?)→list MCP tools across all enabled servers with source tags; "
    "enable_tool(server_name, tool_name)→enable one tool; "
    "disable_tool(server_name, tool_name)→disable one tool; "
    "set_server_enabled(server_name, enabled)→enable/disable an entire MCP server; "
    "call_tool(server_name, tool_name, arguments?, arguments_json?, timeout?)→invoke a tool if enabled; "
    "remove_server(name)→unregister a server; "
    "discover_workspace_servers(root?, register?, trust?)→discover workspace-local HTTP MCP config safely."
)


_CONFIG_FILE = Path(os.getenv("MCP_CONFIG_FILE", "/app/memory/mcp_servers.json"))
if not _CONFIG_FILE.parent.exists() and not Path("/app").exists():
    _CONFIG_FILE = Path("memory/mcp_servers.json")

_TIMEOUT = int(os.getenv("MCP_CLIENT_TIMEOUT", "30"))
_PROTOCOL_VERSION = "2025-06-18"
_RETRY_DELAYS = [1.0, 3.0]
_TRANSIENT_STATUS_CODES = {429, 502, 503, 504}
_WORKSPACE_CONFIG_CANDIDATES = (
    ".trinity/mcp.json",
    ".mcp.json",
    ".vscode/mcp.json",
)


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
        return False, f"⚠️ Warning: Token set for current session, but failed to write to .env ({e}). It may vanish on restart."


# ---------------------------------------------------------------------------
# Server registry helpers
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
    """Ensure a server entry has enabled flag, transport, and tool policy."""
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
    """Return True if a tool is allowed for this MCP server."""
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
        if not isinstance(t, dict):
            continue
        cache.append(
            {
                "name": t.get("name", ""),
                "description": t.get("description", ""),
            }
        )
    return cache


# ---------------------------------------------------------------------------
# MCP formatting / session helpers
# ---------------------------------------------------------------------------

def _format_content_item(item: Any) -> str:
    """Format an MCP content block (text, image, resource, or generic)."""
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
    """Re-run initialization handshake to get a fresh session ID."""
    token = _get_token(info.get("auth_token_env", ""))
    url = info.get("url", "")

    if not url:
        raise MCPError(f"Server '{server_name}' has no URL configured.")

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
    if isinstance(result, dict) and "serverInfo" in result:
        info["server_info"] = result.get("serverInfo", {})

    _save_server_info(server_name, info)
    return new_sid


def _rpc_with_session_recovery(
    server_name: str,
    info: dict,
    method: str,
    params: Optional[dict] = None,
    timeout: Optional[int] = None,
    is_notification: bool = False,
) -> Tuple[dict, Optional[str]]:
    """RPC helper that recovers from stale session errors once."""
    token = _get_token(info.get("auth_token_env", ""))
    url = info.get("url", "")

    if not url:
        raise MCPError(f"Server '{server_name}' has no URL configured.")

    session_id = info.get("session_id", "")

    try:
        result, new_sid = _rpc(
            url=url,
            token=token,
            method=method,
            params=params,
            session_id=session_id,
            is_notification=is_notification,
            timeout=timeout,
        )
    except Exception as e:
        if session_id and _is_session_error(e):
            new_sid = _reinitialize_session(server_name, info, timeout)
            result, retry_sid = _rpc(
                url=url,
                token=token,
                method=method,
                params=params,
                session_id=new_sid,
                is_notification=is_notification,
                timeout=timeout,
            )
            new_sid = retry_sid or new_sid
        else:
            raise

    if new_sid and new_sid != session_id:
        info["session_id"] = new_sid
        _save_server_info(server_name, info)

    return result, new_sid


def _fetch_all_tools(server_name: str, info: dict, timeout: Optional[int] = None) -> List[dict]:
    """Fetch all tools from a server, with pagination and session recovery."""
    all_tools: List[dict] = []
    cursor: Optional[str] = None
    max_pages = 20

    for _ in range(max_pages):
        params: Optional[dict] = {"cursor": cursor} if cursor else None

        result, _ = _rpc_with_session_recovery(
            server_name=server_name,
            info=info,
            method="tools/list",
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


# ---------------------------------------------------------------------------
# Core JSON-RPC / MCP Streamable HTTP transport
# ---------------------------------------------------------------------------

def _rpc(
    url: str,
    token: str = "",
    method: str = "",
    params: Optional[dict] = None,
    session_id: str = "",
    is_notification: bool = False,
    timeout: Optional[int] = None,
) -> Tuple[dict, Optional[str]]:
    """
    JSON-RPC 2.0 call over MCP's Streamable HTTP transport.

    Handles:
    - Authorization header if token is provided
    - Mcp-Session-Id header forwarding & tracking
    - Retries for transient errors
    - Detailed HTTP error extraction
    - SSE event parsing
    - JSON-RPC notifications
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    if token:
        headers["Authorization"] = f"Bearer {token}"

    if session_id:
        headers["Mcp-Session-Id"] = session_id

    payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}

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

            if resp.status_code in _TRANSIENT_STATUS_CODES:
                if attempt_idx < len(attempts) - 1:
                    last_err = MCPHTTPError(
                        f"Transient HTTP {resp.status_code}: {resp.text.strip()[:200]}",
                        status_code=resp.status_code,
                    )
                    continue

                raise MCPHTTPError(
                    f"HTTP {resp.status_code} from MCP server after {len(attempts)} attempts: {resp.text.strip()[:300]}",
                    status_code=resp.status_code,
                )

            if resp.status_code >= 400:
                body_snippet = resp.text.strip()[:400]

                if resp.status_code in (401, 403):
                    raise MCPHTTPError(
                        f"Authentication failed (HTTP {resp.status_code}): token may be missing, expired, or lacking required OAuth scopes. "
                        f"Server response: {body_snippet}",
                        status_code=resp.status_code,
                    )

                raise MCPHTTPError(
                    f"HTTP {resp.status_code} from MCP server: {body_snippet}",
                    status_code=resp.status_code,
                )

            resp_session_id = resp.headers.get("Mcp-Session-Id") or resp.headers.get("mcp-session-id")

            if is_notification:
                return {}, resp_session_id

            if resp.status_code == 204:
                return {}, resp_session_id

            content_type = resp.headers.get("content-type", "")
            raw_text = resp.text.strip()

            if not raw_text:
                if resp.status_code in (200, 202, 204):
                    return {}, resp_session_id
                raise MCPError("Empty response body from MCP server")

            if "text/event-stream" in content_type or raw_text.startswith(("event:", "data:")):
                data_lines = [
                    line[len("data:"):].strip()
                    for line in raw_text.splitlines()
                    if line.startswith("data:")
                ]

                if not data_lines:
                    raise MCPError(f"SSE response had no data lines: {raw_text[:200]!r}")

                parsed_frames = []
                for dline in data_lines:
                    try:
                        parsed_frames.append(json.loads(dline))
                    except json.JSONDecodeError:
                        continue

                if not parsed_frames:
                    raise MCPError(
                        f"Could not parse any SSE payload as JSON from data lines: {data_lines[-1][:200]!r}"
                    )

                expected_id = payload.get("id")
                data = None

                if expected_id is not None:
                    for frame in reversed(parsed_frames):
                        if isinstance(frame, dict) and frame.get("id") == expected_id:
                            data = frame
                            break

                if data is None:
                    data = parsed_frames[-1]
            else:
                try:
                    data = json.loads(raw_text)
                except json.JSONDecodeError as e:
                    raise MCPError(
                        f"Non-JSON response (content-type={content_type!r}): {raw_text[:200]!r}"
                    ) from e

            if not isinstance(data, dict):
                return {}, resp_session_id

            if "error" in data:
                err = data.get("error") or {}
                if not isinstance(err, dict):
                    err = {}

                err_msg = err.get("message", "Unknown MCP error")
                err_code = err.get("code")
                err_data = err.get("data")

                if err_data:
                    err_msg = f"{err_msg} ({err_data})"

                raise MCPError(err_msg, code=err_code, data=err_data)

            result = data.get("result", {})
            if result is None:
                result = {}
            if not isinstance(result, dict):
                result = {"value": result}

            return result, resp_session_id

        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ) as net_err:
            last_err = net_err
            if attempt_idx < len(attempts) - 1:
                continue
            raise MCPError(f"Network error after {len(attempts)} attempts: {net_err}") from net_err

    if last_err:
        raise last_err

    raise MCPError("RPC call failed without specific error")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def connect_server(
    name: str,
    url: str,
    auth_token: str = "",
    timeout: Optional[int] = None,
    verify_tools: bool = True,
) -> str:
    """
    Register a remote MCP server and verify the connection with an initialize handshake.

    If auth_token is provided, it is persisted to .env and loaded into the environment.
    If omitted, an existing stored token for this server is reused if present.
    """
    try:
        servers = _load_servers()
        existing = _ensure_server_defaults(servers.get(name, {}))

        safe_name = re.sub(r"[^A-Z0-9_]", "_", name.upper()) or "SERVER"
        default_env_key = f"MCP_{safe_name}_TOKEN"

        existing_env_key = existing.get("auth_token_env", "")
        env_key = existing_env_key or default_env_key

        token_to_use = auth_token
        persist_warning = ""

        if auth_token:
            env_key = default_env_key
            _, persist_warning = _persist_token(env_key, auth_token)
            token_to_use = auth_token
        else:
            token_to_use = _get_token(env_key) if env_key else ""

        result, resp_session_id = _rpc(
            url=url,
            token=token_to_use,
            method="initialize",
            params={
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "TrinityClaw", "version": "1.3"},
            },
            timeout=timeout,
        )

        session_id = resp_session_id or ""

        try:
            _rpc(
                url=url,
                token=token_to_use,
                method="notifications/initialized",
                session_id=session_id,
                is_notification=True,
                timeout=timeout,
            )
        except Exception:
            pass

        auth_token_env = env_key if (auth_token or token_to_use) else ""

        info = _ensure_server_defaults(existing)
        info.update(
            {
                "url": url,
                "transport": "http",
                "auth_token_env": auth_token_env,
                "connected_at": datetime.now().isoformat(),
                "server_info": result.get("serverInfo", {}),
                "session_id": session_id,
                "protocol_version": result.get("protocolVersion", _PROTOCOL_VERSION),
            }
        )

        servers[name] = info
        _save_servers(servers)

        tools_line = ""
        if verify_tools:
            try:
                tools = _fetch_all_tools(name, info, timeout=timeout)
                info["tools_cache"] = _tool_cache_from_tools(tools)
                info["tools_cache_at"] = datetime.now().isoformat()

                enabled_count = sum(
                    1 for t in tools if _is_tool_enabled(info, t.get("name", ""))
                )

                _save_server_info(name, info)
                tools_line = f"\n🛠️ Tools discovered: {len(tools)} ({enabled_count} enabled)"
            except Exception as tool_err:
                tools_line = f"\n⚠️ Connected, but tools/list failed: {tool_err}"

        server_label = result.get("serverInfo", {}).get("name", name)
        res_str = f"✅ Connected to MCP server '{name}' ({server_label}) at {url}{tools_line}"

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
        lines.append(
            f"  {state} {has_auth} {name} — {info.get('url', '?')} (connected {connected}){tool_summary}"
        )

    return "\n".join(lines)


def ping_server(name: str, timeout: int = 10, full: bool = False) -> str:
    """
    Ping a registered MCP server.

    If full=True, runs a fuller test_server() diagnostic instead of a quick ping.
    """
    if full:
        return test_server(name, timeout=timeout, include_tools=True, as_dict=False)

    servers = _load_servers()

    if name not in servers:
        return f"❌ Server '{name}' not registered. Use connect_server() first."

    info = _ensure_server_defaults(servers[name])
    token_env = info.get("auth_token_env", "")
    token = _get_token(token_env)

    t0 = time.perf_counter()

    try:
        try:
            _, _ = _rpc_with_session_recovery(
                server_name=name,
                info=info,
                method="ping",
                timeout=timeout,
            )
        except MCPError as e:
            # Some servers may not implement ping; fall back to tools/list.
            if e.code == -32601 or "method not found" in str(e).lower():
                _, _ = _rpc_with_session_recovery(
                    server_name=name,
                    info=info,
                    method="tools/list",
                    timeout=timeout,
                )
            else:
                raise

        latency_ms = (time.perf_counter() - t0) * 1000

        if not token_env:
            auth_state = "no auth configured"
        elif not token:
            auth_state = "token missing"
        else:
            auth_state = "token configured"

        tool_state = ""
        cached = info.get("tools_cache")
        if isinstance(cached, list):
            enabled_count = sum(
                1 for t in cached if _is_tool_enabled(info, t.get("name", ""))
            )
            tool_state = f" | Tools: {enabled_count}/{len(cached)} enabled"
        else:
            tool_state = " | Tools: not cached"

        return (
            f"✅ Server '{name}' is healthy and responsive ({latency_ms:.1f}ms latency) "
            f"at {info.get('url', '?')} | Auth: {auth_state}{tool_state}"
        )

    except Exception as e:
        return f"❌ Server '{name}' ping failed: {e}"


def _format_test_report(diag: dict) -> str:
    lines = []

    if diag.get("ok"):
        lines.append(f"✅ MCP test for '{diag.get('server')}' succeeded")
    else:
        lines.append(f"❌ MCP test for '{diag.get('server')}' failed")

    if diag.get("url"):
        lines.append(f"URL: {diag['url']}")

    auth = diag.get("auth", {})
    if auth:
        lines.append(f"Auth: {auth.get('state', 'unknown')}")

    latency = diag.get("latency_ms", {})
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


def test_server(
    name: str,
    timeout: int = 15,
    include_tools: bool = True,
    as_dict: bool = False,
) -> Union[dict, str]:
    """
    Detailed MCP server health check.

    Reports:
    - auth state
    - ping latency
    - tools/list latency
    - tools discovered
    - enabled/disabled tool counts
    """
    servers = _load_servers()

    if name not in servers:
        diag = {
            "server": name,
            "ok": False,
            "url": "",
            "auth": {"state": "unknown"},
            "latency_ms": {},
            "tools": {"total": None, "enabled": None, "disabled": None, "disabled_tools": []},
            "errors": [f"Server '{name}' not registered."],
        }
        return diag if as_dict else f"❌ Server '{name}' not registered. Use connect_server() first."

    info = _ensure_server_defaults(servers[name])
    url = info.get("url", "")
    token_env = info.get("auth_token_env", "")
    token = _get_token(token_env)

    diag: Dict[str, Any] = {
        "server": name,
        "ok": False,
        "url": url,
        "transport": info.get("transport", "http"),
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

    if not url:
        diag["errors"].append("Server has no URL configured.")
        return diag if as_dict else _format_test_report(diag)

    # Ping / fallback liveness check
    t0 = time.perf_counter()

    try:
        try:
            _, _ = _rpc_with_session_recovery(
                server_name=name,
                info=info,
                method="ping",
                timeout=timeout,
            )
        except MCPError as e:
            if e.code == -32601 or "method not found" in str(e).lower():
                _, _ = _rpc_with_session_recovery(
                    server_name=name,
                    info=info,
                    method="tools/list",
                    timeout=timeout,
                )
            else:
                raise

        diag["ok"] = True
        diag["latency_ms"]["ping"] = (time.perf_counter() - t0) * 1000

    except MCPHTTPError as e:
        diag["errors"].append(str(e))
        if e.status_code in (401, 403):
            diag["auth"]["state"] = "token rejected"
    except Exception as e:
        diag["errors"].append(str(e))

    # Tools discovery
    if include_tools:
        t1 = time.perf_counter()

        try:
            tools = _fetch_all_tools(name, info, timeout=timeout)

            diag["latency_ms"]["tools_list"] = (time.perf_counter() - t1) * 1000
            diag["tools"]["total"] = len(tools)

            enabled_names = []
            disabled_names = []

            for t in tools:
                tname = t.get("name", "")
                if _is_tool_enabled(info, tname):
                    enabled_names.append(tname)
                else:
                    disabled_names.append(tname)

            diag["tools"]["enabled"] = len(enabled_names)
            diag["tools"]["disabled"] = len(disabled_names)
            diag["tools"]["disabled_tools"] = disabled_names[:50]

            info["tools_cache"] = _tool_cache_from_tools(tools)
            info["tools_cache_at"] = datetime.now().isoformat()

            # If ping failed but tools/list succeeded, server is still reachable.
            diag["ok"] = True

        except MCPHTTPError as e:
            diag["errors"].append(str(e))
            if e.status_code in (401, 403):
                diag["auth"]["state"] = "token rejected"
        except Exception as e:
            diag["errors"].append(str(e))

    # Final auth state
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
        "latency_ms": diag["latency_ms"],
        "tools_total": diag["tools"]["total"],
        "tools_enabled": diag["tools"]["enabled"],
        "errors": diag["errors"][:3],
    }

    _save_server_info(name, info)

    return diag if as_dict else _format_test_report(diag)


def list_tools(server_name: str, timeout: Optional[int] = None) -> str:
    """Discover tools available on a registered MCP server and show enabled/disabled state."""
    servers = _load_servers()

    if server_name not in servers:
        return f"❌ Server '{server_name}' not registered. Use connect_server() first."

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
        servers[name] = info

        if not info.get("enabled", True):
            continue

        cached = info.get("tools_cache")

        if refresh or not isinstance(cached, list):
            try:
                tools = _fetch_all_tools(name, info, timeout=timeout)
                cached = _tool_cache_from_tools(tools)
                info["tools_cache"] = cached
                info["tools_cache_at"] = datetime.now().isoformat()
                servers[name] = info
                _save_servers(servers)
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
        return "📭 No MCP tools found. Register servers and run list_tools() or list_all_mcp_tools(refresh=True)."

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

    state = "enabled" if enabled else "disabled"
    return f"✅ MCP server '{server_name}' is now {state}."


def call_tool(
    server_name: str,
    tool_name: str,
    arguments: Union[str, dict] = "{}",
    arguments_json: Optional[Union[str, dict]] = None,
    timeout: Optional[int] = None,
) -> str:
    """
    Call a tool on a registered MCP server.

    arguments can be a JSON string or a Python dict of tool parameters.
    timeout overrides the default per-call timeout in seconds.
    """
    servers = _load_servers()

    if server_name not in servers:
        return f"❌ Server '{server_name}' not registered."

    info = _ensure_server_defaults(servers[server_name])

    if not info.get("enabled", True):
        return f"❌ MCP server '{server_name}' is disabled. Use set_server_enabled('{server_name}', True) to enable it."

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
        result, _ = _rpc_with_session_recovery(
            server_name=server_name,
            info=info,
            method="tools/call",
            params={"name": tool_name, "arguments": args},
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
                    pattern = rf"^{re.escape(env_key)}=.*(\r?\n)?"
                    content = re.sub(pattern, "", content, flags=re.MULTILINE)
                    env_file.write_text(content, encoding="utf-8")
                except Exception:
                    pass

        return f"✅ Removed server '{name}' from registry (revoke its token at the provider separately)."

    return f"❌ Server '{name}' not found."


def _find_workspace_config(root: str = ".") -> Optional[Path]:
    """Find the first workspace MCP config file in root or its parents."""
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


def discover_workspace_servers(root: str = ".", register: bool = False, trust: bool = False) -> str:
    """
    Discover workspace-local MCP config files.

    This currently supports HTTP MCP servers only. Stdio/command-based servers
    are reported as unsupported.

    Safe default:
    - register=False, trust=False → dry-run discovery only
    - register=True, trust=False → still refuses to register
    - register=True, trust=True → registers discovered servers as disabled
    """
    path = _find_workspace_config(root)

    if not path:
        return (
            "📭 No workspace MCP config found. Looked for: "
            + ", ".join(_WORKSPACE_CONFIG_CANDIDATES)
        )

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

        if cfg.get("command") or cfg.get("args"):
            unsupported.append(f"{sname} (stdio not supported yet)")
            continue

        url = cfg.get("url")
        if not url:
            unsupported.append(f"{sname} (no url)")
            continue

        discovered.append(
            (
                sname,
                {
                    "url": url,
                    "auth_token_env": cfg.get("auth_token_env", ""),
                    "transport": "http",
                },
            )
        )

    lines = [f"📁 Workspace MCP config: {path}"]

    if discovered:
        lines.append("Discovered HTTP MCP servers:")
        for sname, cfg in discovered:
            auth_note = f" (auth_token_env={cfg['auth_token_env']})" if cfg.get("auth_token_env") else ""
            lines.append(f"  • {sname} — {cfg['url']}{auth_note}")
    else:
        lines.append("📭 No usable HTTP MCP servers found in workspace config.")

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

        info = _ensure_server_defaults({})
        info.update(
            {
                "url": cfg["url"],
                "transport": "http",
                "auth_token_env": cfg.get("auth_token_env", ""),
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


__all__ = [
    "NAME",
    "SHORT_DOC",
    "DOC",
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
]