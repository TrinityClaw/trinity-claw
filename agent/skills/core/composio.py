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
        from composio import ComposioToolSet
        _composio = ComposioToolSet(api_key=api_key)
        return _composio
    except ImportError:
        _sdk_error = (
            "composio package not installed. Run: pip install composio"
        )
        return None
    except Exception as e:
        _sdk_error = f"Failed to initialize Composio SDK: {e}"
        return None


def _err(msg: str) -> str:
    return f"❌ {msg}"


def _ok(msg: str) -> str:
    return f"✅ {msg}"


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
                accounts = client.get_connected_accounts()
                count = len(accounts) if accounts else 0
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
        import composio  # noqa
        sdk_installed = True
    except ImportError:
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
        steps.append("I'll also install the SDK automatically once you paste your key.")

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

    # Install SDK if missing
    sdk_msg = ""
    try:
        import composio  # noqa
    except ImportError:
        import subprocess
        import sys
        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "composio", "-q"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=120,
            )
            sdk_msg = "\n📦 SDK installed (composio package)"
        except Exception as e:
            sdk_msg = (
                f"\n⚠️ Could not auto-install SDK. Run manually:\n"
                f"   pip install composio\n   Error: {e}"
            )

    # Verify the key works
    client = _get_composio()
    if client is None:
        return _err(
            f"Key saved to {env_path} but SDK initialization failed: {_sdk_error}\n"
            f"{sdk_msg}"
        )

    try:
        accounts = client.get_connected_accounts()
        count = len(accounts) if accounts else 0
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
        accounts = client.get_connected_accounts()
        count = len(accounts) if accounts else 0
        return (
            f"✅ Composio configured (key: ...{api_key[-4:]})\n"
            f"   SDK: installed | Connected accounts: {count}"
        )
    except Exception as e:
        return f"⚠️ Composio key set but connection failed: {e}"


def list_apps() -> str:
    """List all apps available through Composio (200+)."""
    client = _get_composio()
    if client is None:
        return _err(_sdk_error)

    try:
        tools = client.get_tools()
        apps = sorted({t.app for t in tools if hasattr(t, "app") and t.app})
        if not apps:
            return "No apps found. Check your COMPOSIO_API_KEY."
        lines = [f"Available apps ({len(apps)}):\n"]
        for i in range(0, len(apps), 6):
            lines.append("  " + ", ".join(apps[i:i+6]))
        return "\n".join(lines)
    except Exception as e:
        return _err(f"Failed to list apps: {e}")


def search_tools(query: str = "") -> str:
    """Search Composio tools by name or description.

    Args:
        query: Search term (e.g. 'slack', 'send email', 'create issue').
                Empty query returns a summary of all tools.
    """
    client = _get_composio()
    if client is None:
        return _err(_sdk_error)

    try:
        tools = client.get_tools()
        if not query:
            apps = {}
            for t in tools:
                app = getattr(t, "app", "unknown")
                apps[app] = apps.get(app, 0) + 1
            lines = [f"Total tools: {len(tools)} across {len(apps)} apps\n"]
            for app, count in sorted(apps.items()):
                lines.append(f"  {app}: {count} tools")
            return "\n".join(lines)

        q = query.lower()
        matches = [
            t for t in tools
            if q in getattr(t, "name", "").lower()
            or q in getattr(t, "description", "").lower()
            or q in getattr(t, "app", "").lower()
        ]
        if not matches:
            return f"No tools matching '{query}'. Try list_apps() to see available apps."

        lines = [f"Found {len(matches)} tool(s) for '{query}':\n"]
        for t in matches[:20]:
            name = getattr(t, "name", "?")
            app = getattr(t, "app", "?")
            desc = getattr(t, "description", "")[:100]
            lines.append(f"  [{app}] {name}: {desc}")
        if len(matches) > 20:
            lines.append(f"  ... and {len(matches) - 20} more")
        return "\n".join(lines)
    except Exception as e:
        return _err(f"Search failed: {e}")


def list_tools(app: str = "") -> str:
    """List tools for a specific app, or all tools if app is empty.

    Args:
        app: App name (e.g. 'slack', 'gmail', 'notion'). Empty = all.
    """
    client = _get_composio()
    if client is None:
        return _err(_sdk_error)

    try:
        tools = client.get_tools()
        if app:
            q = app.lower()
            tools = [t for t in tools if q in getattr(t, "app", "").lower()]
            if not tools:
                return f"No tools for '{app}'. Use list_apps() to see available apps."

        lines = [f"Tools ({len(tools)}):\n"]
        for t in tools[:50]:
            name = getattr(t, "name", "?")
            app_name = getattr(t, "app", "?")
            desc = getattr(t, "description", "")[:80]
            lines.append(f"  [{app_name}] {name}: {desc}")
        if len(tools) > 50:
            lines.append(f"  ... and {len(tools) - 50} more")
        return "\n".join(lines)
    except Exception as e:
        return _err(f"Failed to list tools: {e}")


def execute_tool(tool_name: str, params: str = "{}") -> str:
    """Execute a Composio tool by name with JSON parameters.

    Args:
        tool_name: The Composio tool name (e.g. 'SLACK_SEND_MESSAGE').
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
        result = client.execute_tool(tool_name, params_dict)
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
        accounts = client.get_connected_accounts()
        if not accounts:
            return (
                "No connected accounts yet.\n"
                "Use get_auth_url(app) to start an OAuth flow, "
                "then check_connection(app) to verify."
            )
        lines = [f"Connected accounts ({len(accounts)}):\n"]
        for acc in accounts:
            app = getattr(acc, "app_name", getattr(acc, "app", "?"))
            status_val = getattr(acc, "status", "unknown")
            lines.append(f"  {app}: {status_val}")
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

    if not app:
        return _err("app name is required. Use list_apps() to see available apps.")

    try:
        url = client.get_auth_url(app)
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
        accounts = client.get_connected_accounts()
        if not app:
            count = len(accounts) if accounts else 0
            return f"Connected accounts: {count}. Use list_connected_accounts() for details."

        q = app.lower()
        for acc in accounts:
            acc_app = getattr(acc, "app_name", getattr(acc, "app", ""))
            if q in acc_app.lower():
                st = getattr(acc, "status", "unknown")
                if st == "active":
                    return _ok(f"{acc_app}: connected and active")
                return f"⚠️ {acc_app}: status={st}"

        return (
            f"❌ {app} not connected.\n"
            f"Call get_auth_url('{app}') to start the OAuth flow."
        )
    except Exception as e:
        return _err(f"Connection check failed: {e}")
