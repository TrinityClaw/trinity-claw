"""
Composio — connect TrinityClaw to 200+ external apps via Composio's tool-calling SDK.

Covers apps that don't have their own skill yet: Slack, Notion, HubSpot, Jira,
Linear, Airtable, Stripe, Salesforce, and many more. Composio handles OAuth
flows and token refreshes so skills don't have to.

Security notes:
- COMPOSIO_API_KEY is read from .env — never printed or returned in results.
- Tool output is untrusted external content; app.py sanitizes composio results
  the same way it sanitizes mcp_client and chromadb output.
- Only the minimum connected-account scopes configured at composio.dev are
  granted; this skill cannot escalate scopes on its own.
"""

import os
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

NAME = "composio"
SHORT_DOC = "Connect to 200+ external apps (Slack, Notion, Jira, Stripe, etc.) via Composio."
DOC = (
    "Composio tool integration — search, execute, and manage tools across 200+ connected apps. "
    "SETUP: When user says 'set up composio', 'connect composio', or 'enable composio' — "
    "call setup() FIRST. It guides them step-by-step (opens browser, gets API key, "
    "writes to .env, installs SDK). After setup succeeds, call status() to verify. "
    "Functions: setup(), status(), list_apps(), search_tools(query), list_tools(app?), "
    "execute_tool(tool_name, params), list_connected_accounts(), "
    "get_auth_url(app), check_connection(app)."
)

SKILL_TIMEOUT = int(os.getenv("COMPOSIO_SKILL_TIMEOUT", "60"))

# The current Composio SDK ties every connection to a "user id". TrinityClaw is
# single-user, so one fixed id is enough. Override with COMPOSIO_USER_ID if needed.
USER_ID = os.getenv("COMPOSIO_USER_ID", "trinityclaw").strip() or "trinityclaw"

__all__ = [
    "NAME",
    "SHORT_DOC",
    "DOC",
    "SKILL_TIMEOUT",
    "setup",
    "status",
    "list_apps",
    "search_tools",
    "list_tools",
    "execute_tool",
    "list_connected_accounts",
    "get_auth_url",
    "check_connection",
]


# ── Lazy SDK import ──────────────────────────────────────────────────────────

_composio = None
_sdk_error = None


def _load_sdk_class():
    """Import ``Composio`` from the real pip-installed SDK.

    This skill file is itself named ``composio.py`` and lives in a folder that can be on
    sys.path, so a plain ``from composio import Composio`` may import THIS file instead of
    the SDK. We temporarily hide the skill's own folder (and any stale module entry) so the
    real package in site-packages is found, whatever the Python version or install path.
    """
    import importlib
    import sys

    this_file = os.path.abspath(__file__)
    here = os.path.dirname(this_file)

    def _is_self(mod) -> bool:
        f = getattr(mod, "__file__", None)
        return bool(f) and os.path.abspath(f) == this_file

    mod = sys.modules.get("composio")
    if mod is not None and not _is_self(mod) and hasattr(mod, "Composio"):
        return mod.Composio  # real SDK already imported

    saved_path = list(sys.path)
    saved_mod = mod
    try:
        sys.path = [p for p in sys.path if os.path.abspath(p or os.getcwd()) != here]
        sys.modules.pop("composio", None)
        sdk = importlib.import_module("composio")
        return sdk.Composio
    except Exception:
        if saved_mod is not None:
            sys.modules["composio"] = saved_mod  # put things back if the SDK truly isn't there
        raise
    finally:
        sys.path = saved_path


def _get_composio():
    """Lazily import and initialize the Composio SDK. Returns client or None."""
    global _composio, _sdk_error

    if _composio is not None:
        return _composio
    if _sdk_error is not None:
        return None

    api_key = os.getenv("COMPOSIO_API_KEY", "").strip()
    if not api_key:
        _sdk_error = "COMPOSIO_API_KEY not set. Get a free key at composio.dev and add it to .env."
        return None

    try:
        Composio = _load_sdk_class()
        _composio = Composio(api_key=api_key)
        return _composio
    except ImportError as e:
        _sdk_error = (
            f"Could not import the Composio SDK ({e}). Add 'composio' to "
            "agent/requirements.txt and rebuild the image."
        )
        return None
    except Exception as e:
        _sdk_error = f"Failed to initialize Composio SDK: {e}"
        return None


def _err(msg: str) -> str:
    return f"❌ {msg}"


def _ok(msg: str) -> str:
    return f"✅ {msg}"


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Read a field from either a dict or an object."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _toolkit_slug(obj: Any) -> str:
    """Return the toolkit/app slug of a tool or connected-account record."""
    tk = _get(obj, "toolkit")
    if tk is None:
        return str(_get(obj, "app", "") or "")
    if isinstance(tk, str):
        return tk
    return str(_get(tk, "slug", "") or "")


def _accounts(client, toolkit: str = "") -> list:
    """Connected accounts for this TrinityClaw user (optionally one toolkit)."""
    kwargs: Dict[str, Any] = {"user_ids": [USER_ID], "limit": 100}
    if toolkit:
        kwargs["toolkit_slugs"] = [toolkit.lower()]
    resp = client.connected_accounts.list(**kwargs)
    return list(_get(resp, "items", []) or [])


# ── Public functions ─────────────────────────────────────────────────────────

def setup(api_key: str = "") -> str:
    """Guided Composio setup — call when the user says 'set up composio'.

    Pass api_key directly if the user already pasted their key.
    Otherwise returns step-by-step instructions and opens the signup page.

    Args:
        api_key: Optional Composio API key. If provided, saves it to .env.
    """
    # Step 1: If key provided, save it
    if api_key:
        return _save_api_key(api_key.strip())

    # Step 2: Check if already configured
    existing = os.getenv("COMPOSIO_API_KEY", "").strip()
    if existing:
        client = _get_composio()
        if client is not None:
            try:
                count = len(_accounts(client))
                return (
                    f"✅ Composio is already set up (key: ...{existing[-4:]}, "
                    f"{count} connected accounts).\n"
                    f"You're ready to go! Try: 'list composio apps' or "
                    f"'connect to slack'."
                )
            except Exception:
                pass  # key exists but SDK fails — fall through to repair

    # Step 3: Not configured — guide the user
    import webbrowser

    signup_url = "https://composio.dev"
    try:
        webbrowser.open(signup_url)
        opened = "🌐 Browser opened to composio.dev"
    except Exception:
        opened = f"🌐 Open {signup_url} in your browser"

    sdk_installed = False
    try:
        _load_sdk_class()
        sdk_installed = True
    except Exception:
        pass

    steps = [
        f"🔧 **Composio Setup** (free — ~2 minutes)\n",
        opened,
        "",
        "**Step 1** — Create a free account at composio.dev",
        "**Step 2** — Go to Dashboard → API Keys → Copy your key",
        "**Step 3** — Paste the key here and I'll save it for you",
    ]

    if not sdk_installed:
        steps.append("")
        steps.append("Note: the SDK is not installed in this container. Add 'composio' to agent/requirements.txt and rebuild the image.")

    steps.append("")
    steps.append("Just paste your API key and I'll handle the rest.")

    return "\n".join(steps)


def _save_api_key(api_key: str) -> str:
    """Save COMPOSIO_API_KEY to .env and verify it works."""
    if not api_key or len(api_key) < 10:
        return _err("That doesn't look like a valid API key. It should be a long string from composio.dev.")

    # Find and update .env
    env_paths = [
        Path(".env"),
        Path(__file__).parent.parent.parent / ".env",
        Path("/app/.env"),
    ]

    env_path = None
    for p in env_paths:
        if p.exists():
            env_path = p
            break

    if env_path is None:
        # Create .env in the project root
        env_path = Path(".env")
        try:
            env_path.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    try:
        content = env_path.read_text(encoding="utf-8") if env_path.exists() else ""

        if "COMPOSIO_API_KEY=" in content:
            # Replace existing line
            lines = content.splitlines()
            for i, line in enumerate(lines):
                if line.strip().startswith("COMPOSIO_API_KEY="):
                    lines[i] = f"COMPOSIO_API_KEY={api_key}"
                    break
            content = "\n".join(lines)
            if not content.endswith("\n"):
                content += "\n"
        else:
            content += f"\nCOMPOSIO_API_KEY={api_key}\n"

        env_path.write_text(content, encoding="utf-8")
    except Exception as e:
        return _err(f"Could not write to {env_path}: {e}")

    # Set in current process so it works immediately (no restart needed)
    os.environ["COMPOSIO_API_KEY"] = api_key

    # Reset cached SDK so next call picks up the new key
    global _composio, _sdk_error
    _composio = None
    _sdk_error = None

    # The SDK must be baked into the Docker image (runtime pip installs are lost on rebuild)
    sdk_msg = ""
    try:
        _load_sdk_class()
    except Exception:
        sdk_msg = (
            "\n⚠️ The composio package is not installed in this container. "
            "Add 'composio' to agent/requirements.txt, then run: "
            "docker-compose build --no-cache trinity-agent"
        )

    # Verify the key works
    client = _get_composio()
    if client is None:
        return _err(
            f"Key saved to {env_path} but SDK initialization failed: {_sdk_error}\n"
            f"{sdk_msg}"
        )

    try:
        count = len(_accounts(client))
        return (
            f"✅ Composio connected! (key: ...{api_key[-4:]}){sdk_msg}\n"
            f"   Connected accounts: {count}\n\n"
            f"**Next steps:**\n"
            f"  • 'connect to slack' — link your first app\n"
            f"  • 'list composio apps' — see all 200+ apps\n"
            f"  • 'search composio tools for notion' — find tools"
        )
    except Exception as e:
        return (
            f"✅ Key saved to {env_path}{sdk_msg}\n"
            f"⚠️ But verification failed: {e}\n"
            f"Double-check your key at composio.dev/dashboard"
        )


def status() -> str:
    """Check Composio setup health: API key, SDK installed, connection state."""
    api_key = os.getenv("COMPOSIO_API_KEY", "").strip()
    if not api_key:
        return (
            "❌ Composio NOT configured.\n"
            "  1. Create a free account at https://composio.dev\n"
            "  2. Copy your API key from the dashboard\n"
            "  3. Add to .env: COMPOSIO_API_KEY=your-key-here\n"
            "  4. Restart TrinityClaw"
        )

    client = _get_composio()
    if client is None:
        return f"❌ {_sdk_error}"

    try:
        count = len(_accounts(client))
        return (
            f"✅ Composio configured (key: ...{api_key[-4:]})\n"
            f"   SDK: installed | User id: {USER_ID} | Connected accounts: {count}"
        )
    except Exception as e:
        return f"⚠️ Composio key set but connection failed: {e}"


def _all_toolkits(client, max_pages: int = 10) -> list:
    """Fetch toolkits (apps), following pagination."""
    items: list = []
    cursor = None
    for _ in range(max_pages):
        kwargs: Dict[str, Any] = {"limit": 100, "sort_by": "alphabetically"}
        if cursor:
            kwargs["cursor"] = cursor
        resp = client.toolkits.list(**kwargs)
        items.extend(_get(resp, "items", []) or [])
        cursor = _get(resp, "next_cursor")
        if not cursor:
            break
    return items


def list_apps() -> str:
    """List all apps (toolkits) available through Composio (200+)."""
    client = _get_composio()
    if client is None:
        return _err(_sdk_error)

    try:
        slugs = sorted({str(_get(t, "slug", "")) for t in _all_toolkits(client) if _get(t, "slug")})
        if not slugs:
            return "No apps found. Check your COMPOSIO_API_KEY."
        lines = [f"Available apps ({len(slugs)}):\n"]
        for i in range(0, len(slugs), 6):
            lines.append("  " + ", ".join(slugs[i:i + 6]))
        return "\n".join(lines)
    except Exception as e:
        return _err(f"Failed to list apps: {e}")


def _format_tools(tools: list, limit: int, desc_len: int) -> list:
    lines = []
    for t in tools[:limit]:
        slug = _get(t, "slug", "?")
        app = _toolkit_slug(t) or "?"
        desc = (_get(t, "description", "") or "")[:desc_len]
        lines.append(f"  [{app}] {slug}: {desc}")
    return lines


def search_tools(query: str = "") -> str:
    """Search Composio tools by name or description.

    Args:
        query: Search term (e.g. 'slack send message', 'create issue').
               Empty query returns the list of apps instead.
    """
    client = _get_composio()
    if client is None:
        return _err(_sdk_error)

    if not query.strip():
        return list_apps()

    try:
        tools = client.tools.get_raw_composio_tools(search=query.strip(), limit=20)
        if not tools:
            return f"No tools matching '{query}'. Try list_apps() to see available apps."
        lines = [f"Found {len(tools)} tool(s) for '{query}':\n"]
        lines.extend(_format_tools(tools, 20, 100))
        return "\n".join(lines)
    except Exception as e:
        return _err(f"Search failed: {e}")


def list_tools(app: str = "") -> str:
    """List tools for a specific app.

    Args:
        app: App name (e.g. 'slack', 'gmail', 'notion'). Required by the Composio API.
    """
    client = _get_composio()
    if client is None:
        return _err(_sdk_error)

    if not app.strip():
        return _err("app is required (e.g. 'slack'). Use list_apps() to see available apps.")

    try:
        tools = client.tools.get_raw_composio_tools(toolkits=[app.strip().lower()], limit=50)
        if not tools:
            return f"No tools for '{app}'. Use list_apps() to see available apps."
        lines = [f"Tools for {app} ({len(tools)}):\n"]
        lines.extend(_format_tools(tools, 50, 80))
        return "\n".join(lines)
    except Exception as e:
        return _err(f"Failed to list tools: {e}")


def execute_tool(tool_name: str, params: str = "{}") -> str:
    """Execute a Composio tool by name with JSON parameters.

    Args:
        tool_name: The Composio tool slug (e.g. 'SLACK_SEND_MESSAGE').
        params: JSON string of parameters (e.g. '{"channel":"#general","text":"hello"}').
    """
    client = _get_composio()
    if client is None:
        return _err(_sdk_error)

    if not tool_name:
        return _err("tool_name is required. Use search_tools() to find tool names.")

    try:
        if isinstance(params, str):
            params_dict = json.loads(params) if params.strip() else {}
        else:
            params_dict = params or {}
    except json.JSONDecodeError as e:
        return _err(f"Invalid JSON in params: {e}")

    try:
        result = client.tools.execute(
            tool_name.strip().upper(),
            params_dict,
            user_id=USER_ID,
            dangerously_skip_version_check=True,
        )
        if isinstance(result, (dict, list)):
            return json.dumps(result, indent=2, ensure_ascii=False, default=str)
        return str(result)
    except Exception as e:
        return _err(f"Tool '{tool_name}' failed: {e}")


def list_connected_accounts() -> str:
    """List all connected accounts and their status."""
    client = _get_composio()
    if client is None:
        return _err(_sdk_error)

    try:
        accounts = _accounts(client)
        if not accounts:
            return (
                "No connected accounts yet.\n"
                "Use get_auth_url(app) to start an OAuth flow, "
                "then check_connection(app) to verify."
            )
        lines = [f"Connected accounts ({len(accounts)}):\n"]
        for acc in accounts:
            lines.append(f"  {_toolkit_slug(acc) or '?'}: {_get(acc, 'status', 'unknown')}")
        return "\n".join(lines)
    except Exception as e:
        return _err(f"Failed to list accounts: {e}")


def get_auth_url(app: str = "") -> str:
    """Get an OAuth authorization URL for connecting an app.

    Args:
        app: App name (e.g. 'slack', 'gmail', 'notion').
    """
    client = _get_composio()
    if client is None:
        return _err(_sdk_error)

    if not app.strip():
        return _err("app name is required. Use list_apps() to see available apps.")

    app = app.strip().lower()
    try:
        req = client.toolkits.authorize(user_id=USER_ID, toolkit=app)
        url = _get(req, "redirect_url")
        if not url:
            return _err(f"No auth URL available for '{app}'. It may use API key auth instead.")
        return (
            f"OAuth URL for {app}:\n"
            f"  {url}\n\n"
            f"Open the URL in a browser, approve access, then call "
            f"check_connection('{app}') to verify."
        )
    except Exception as e:
        return _err(f"Failed to get auth URL for '{app}': {e}")


def check_connection(app: str = "") -> str:
    """Check if an app has a valid connected account.

    Args:
        app: App name (e.g. 'slack', 'gmail'). Empty checks all.
    """
    client = _get_composio()
    if client is None:
        return _err(_sdk_error)

    try:
        if not app.strip():
            return f"Connected accounts: {len(_accounts(client))}. Use list_connected_accounts() for details."

        accounts = _accounts(client, app.strip())
        if not accounts:
            return (
                f"❌ {app} not connected.\n"
                f"Call get_auth_url('{app}') to start the OAuth flow."
            )
        for acc in accounts:
            if str(_get(acc, "status", "")).upper() == "ACTIVE":
                return _ok(f"{_toolkit_slug(acc) or app}: connected and active")
        st = _get(accounts[0], "status", "unknown")
        return f"⚠️ {_toolkit_slug(accounts[0]) or app}: status={st}"
    except Exception as e:
        return _err(f"Connection check failed: {e}")
