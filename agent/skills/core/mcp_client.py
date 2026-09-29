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
import requests
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple

NAME = "mcp_client"
SHORT_DOC = "Connect to external MCP servers (Slack, HubSpot, Cloudflare, Google, etc.) and call their tools."
DOC = (
    "Generic MCP client — register remote MCP servers once, then discover and call their tools "
    "without writing a custom skill per service. "
    "Functions: "
    "connect_server(name, url, auth_token?)→register and test a connection to a remote MCP server; "
    "auth_token is stored in .env and loaded into environment, never in the registry file; "
    "list_servers()→show all registered MCP servers and when they last connected; "
    "list_tools(server_name)→discover available tools on a registered server; "
    "call_tool(server_name, tool_name, arguments_json)→invoke a tool on a remote MCP server; "
    "arguments_json is a JSON string of the tool's parameters, e.g. '{\"channel\":\"general\",\"text\":\"hi\"}'; "
    "remove_server(name)→unregister a server."
)

_CONFIG_FILE = Path(os.getenv("MCP_CONFIG_FILE", "/app/memory/mcp_servers.json"))
if not _CONFIG_FILE.parent.exists() and not Path("/app").exists():
    _CONFIG_FILE = Path("memory/mcp_servers.json")

_TIMEOUT = int(os.getenv("MCP_CLIENT_TIMEOUT", "30"))
_PROTOCOL_VERSION = "2025-06-18"


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


def _persist_token(env_key: str, auth_token: str) -> None:
    """Write auth token to .env file and environment so it persists across container/process restarts."""
    if not env_key or not auth_token:
        return
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
    except Exception:
        pass


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


def _rpc(
    url: str,
    token: str = "",
    method: str = "",
    params: Optional[dict] = None,
    session_id: str = "",
    is_notification: bool = False,
) -> Tuple[dict, Optional[str]]:
    """Minimal JSON-RPC 2.0 call over MCP's Streamable HTTP transport.

    Handles:
    - Authorization header if token is provided
    - Mcp-Session-Id header forwarding & tracking
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

    resp = requests.post(url, json=payload, headers=headers, timeout=_TIMEOUT)
    resp.raise_for_status()

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
        # Parse SSE frames — take the last 'data:' line as the JSON-RPC payload
        # (the final message in the stream is the actual result for a single call).
        data_lines = [
            line[len("data:"):].strip()
            for line in raw_text.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            raise RuntimeError(f"SSE response had no data lines: {raw_text[:200]!r}")
        try:
            data = json.loads(data_lines[-1])
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Could not parse SSE payload as JSON: {data_lines[-1][:200]!r}") from e
    else:
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"Non-JSON response (content-type={content_type!r}): {raw_text[:200]!r}"
            ) from e

    if "error" in data:
        raise RuntimeError(data["error"].get("message", "Unknown MCP error"))
    return data.get("result", {}), resp_session_id


def connect_server(name: str, url: str, auth_token: str = "") -> str:
    """Register a remote MCP server and verify the connection with an initialize handshake.
    auth_token (if provided) is persisted to .env and loaded into the environment."""
    try:
        env_key = f"MCP_{name.upper()}_TOKEN"
        if auth_token:
            _persist_token(env_key, auth_token)

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
        return f"✅ Connected to MCP server '{name}' ({server_label}) at {url}"
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


def list_tools(server_name: str) -> str:
    """Discover tools available on a registered MCP server."""
    servers = _load_servers()
    if server_name not in servers:
        return f"❌ Server '{server_name}' not registered. Use connect_server() first."
    info = servers[server_name]
    token = _get_token(info.get("auth_token_env", ""))
    session_id = info.get("session_id", "")
    try:
        result, new_sid = _rpc(
            url=info["url"],
            token=token,
            method="tools/list",
            session_id=session_id,
        )
        if new_sid and new_sid != session_id:
            info["session_id"] = new_sid
            _save_servers(servers)
        tools = result.get("tools", [])
        if not tools:
            return f"📭 No tools exposed by '{server_name}'"
        lines = [f"🛠️ Tools on '{server_name}' ({len(tools)} total):"]
        for t in tools:
            desc = (t.get("description") or "").strip()[:100]
            lines.append(f"  • {t['name']}: {desc}")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Failed to list tools on '{server_name}': {e}"


def call_tool(server_name: str, tool_name: str, arguments_json: str = "{}") -> str:
    """Call a tool on a registered MCP server. arguments_json is a JSON string of parameters."""
    servers = _load_servers()
    if server_name not in servers:
        return f"❌ Server '{server_name}' not registered."
    info = servers[server_name]
    token = _get_token(info.get("auth_token_env", ""))
    session_id = info.get("session_id", "")
    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError:
        return f"❌ Invalid JSON in arguments_json: {arguments_json}"
    try:
        result, new_sid = _rpc(
            url=info["url"],
            token=token,
            method="tools/call",
            params={"name": tool_name, "arguments": args},
            session_id=session_id,
        )
        if new_sid and new_sid != session_id:
            info["session_id"] = new_sid
            _save_servers(servers)
        content = result.get("content", [])
        text_parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        return "\n".join(text_parts) if text_parts else json.dumps(result, ensure_ascii=False)
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
    "connect_server", "list_servers", "list_tools", "call_tool", "remove_server",
]