"""
MCP Client — connects Trinity to remote MCP servers (Slack, HubSpot, Cloudflare,
Google Workspace, Google Maps, etc.) so their tools become callable without
writing a custom skill per service.

Security notes:
  - Auth tokens are stored as env vars (referenced by name in mcp_servers.json),
    never written to disk in plaintext alongside the server registry.
  - Tool results are returned as plain text/JSON for the caller (app.py) to run
    through the same _sanitize_external_content() pipeline used for ChromaDB
    and lessons.jsonl — remote MCP tool output is untrusted external content.
  - Register only the minimum OAuth scopes needed for each service at the
    provider's end (e.g. read-only Slack scopes) — this skill has no way to
    enforce scoping itself; that must be done when you create the token.
"""
import json
import os
import requests
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

NAME = "mcp_client"
SHORT_DOC = "Connect to external MCP servers (Slack, HubSpot, Cloudflare, Google, etc.) and call their tools."
DOC = (
    "Generic MCP client — register remote MCP servers once, then discover and call their tools "
    "without writing a custom skill per service. "
    "Functions: "
    "connect_server(name, url, auth_token?)→register and test a connection to a remote MCP server; "
    "auth_token is stored as an env var, never in the registry file; "
    "list_servers()→show all registered MCP servers and when they last connected; "
    "list_tools(server_name)→discover available tools on a registered server; "
    "call_tool(server_name, tool_name, arguments_json)→invoke a tool on a remote MCP server; "
    "arguments_json is a JSON string of the tool's parameters, e.g. '{\"channel\":\"general\",\"text\":\"hi\"}'; "
    "remove_server(name)→unregister a server."
)

_CONFIG_FILE = Path("/app/memory/mcp_servers.json")
_TIMEOUT = int(os.getenv("MCP_CLIENT_TIMEOUT", "30"))
_PROTOCOL_VERSION = "2025-06-18"


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


def _rpc(url: str, token: str, method: str, params: Optional[dict] = None) -> dict:
    """Minimal JSON-RPC 2.0 call over MCP's Streamable HTTP transport.

    Streamable HTTP allows a server to answer either as plain JSON or as an
    SSE stream (lines prefixed with 'data:') — some servers pick SSE even for
    a single-response call. Handle both instead of assuming resp.json() works.
    """
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    resp = requests.post(url, json=payload, headers=headers, timeout=_TIMEOUT)
    resp.raise_for_status()

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
    return data.get("result", {})


def connect_server(name: str, url: str, auth_token: str = "") -> str:
    """Register a remote MCP server and verify the connection with an initialize handshake.
    auth_token (if the service requires one) is stored as an env var, not in the registry file."""
    try:
        env_key = f"MCP_{name.upper()}_TOKEN"
        if auth_token:
            os.environ[env_key] = auth_token

        result = _rpc(url, auth_token, "initialize", {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "TrinityClaw", "version": "1.3"},
        })

        servers = _load_servers()
        servers[name] = {
            "url": url,
            "auth_token_env": env_key if auth_token else "",
            "connected_at": datetime.now().isoformat(),
            "server_info": result.get("serverInfo", {}),
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
    token = os.getenv(info.get("auth_token_env", ""), "")
    try:
        result = _rpc(info["url"], token, "tools/list")
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
    token = os.getenv(info.get("auth_token_env", ""), "")
    try:
        args = json.loads(arguments_json) if arguments_json else {}
    except json.JSONDecodeError:
        return f"❌ Invalid JSON in arguments_json: {arguments_json}"
    try:
        result = _rpc(info["url"], token, "tools/call", {"name": tool_name, "arguments": args})
        content = result.get("content", [])
        text_parts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        return "\n".join(text_parts) if text_parts else json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return f"❌ Tool call to '{tool_name}' on '{server_name}' failed: {e}"


def remove_server(name: str) -> str:
    """Unregister an MCP server. Does not revoke the token at the provider — do that separately."""
    servers = _load_servers()
    if name in servers:
        del servers[name]
        _save_servers(servers)
        return f"✅ Removed server '{name}' from registry (revoke its token at the provider separately)"
    return f"❌ Server '{name}' not found"


__all__ = [
    "NAME", "DOC", "SHORT_DOC",
    "connect_server", "list_servers", "list_tools", "call_tool", "remove_server",
]