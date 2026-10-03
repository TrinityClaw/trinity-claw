"""
mcp_universal - server-agnostic helpers that make ANY registered MCP tool
callable by a text-tag agent (<skill:mcp_server_tool>args</skill:...>).

Nothing here knows about a specific server. Everything is derived from what
each MCP server publishes about itself: tool name, description, inputSchema.

Registry shape (same as app._get_mcp_tool_registry()):
    { "mcp_cloudflare_search": (server, tool, description, inputSchema), ... }

Three entry points:
    render_mcp_docs(registry)          -> prompt text with exact call signatures
    normalize_mcp_tags(text, registry) -> repairs wrong tag shapes the model emits
    parse_mcp_args(entry, raw)         -> (args_dict, error_or_None); the error
                                          is written so the model can self-correct
"""

import os
import re
import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

__all__ = ["render_mcp_docs", "normalize_mcp_tags", "parse_mcp_args", "signature"]

_SAFE = re.compile(r"[^A-Za-z0-9_]")

MCP_RULES = (
    "MCP tools - each is ONE tag whose body is a JSON object matching its signature:\n"
    '  <skill:TOOLNAME>{"arg": value}</skill:TOOLNAME>\n'
    "Rules: use the exact TOOLNAME shown; never write mcp_client.<server>.<tool>; "
    "argument names and types come from the signature (* = required), never invent them; "
    "read each tool's description - if it says to call another tool first, do that first; "
    "put credentials nowhere in arguments (servers are already authenticated); "
    "if a result says an argument is missing or invalid, fix it and retry once."
)


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

def _props(schema: Any) -> Dict[str, dict]:
    if isinstance(schema, dict) and isinstance(schema.get("properties"), dict):
        return {k: (v if isinstance(v, dict) else {}) for k, v in schema["properties"].items()}
    return {}


def _required(schema: Any) -> list:
    if isinstance(schema, dict) and isinstance(schema.get("required"), list):
        return [r for r in schema["required"] if isinstance(r, str)]
    return []


def _type_label(spec: dict) -> str:
    if isinstance(spec.get("enum"), list) and spec["enum"]:
        return "|".join(str(e) for e in spec["enum"][:8])
    t = spec.get("type")
    if isinstance(t, list):
        t = "|".join(str(x) for x in t)
    return str(t or "any")


def signature(tool_name: str, schema: Any, desc_chars: int = 90) -> str:
    """One-line signature, e.g.  name({"code"*: string, "limit": integer})"""
    props, req = _props(schema), set(_required(schema))
    parts = [f'"{k}"{"*" if k in req else ""}: {_type_label(v)}' for k, v in props.items()]
    return "{" + ", ".join(parts) + "}"



# ---------------------------------------------------------------------------
# Optional per-server usage notes (data, not code)
# ---------------------------------------------------------------------------
# Some servers (e.g. "code mode" servers) need the model to know things the
# JSON schema cannot express, such as sandbox globals. Put that text in the
# server's entry in mcp_servers.json under "usage_notes". It is re-read when the
# file changes and shown once, right before that server's tools.

_NOTES_CACHE: Dict[str, Any] = {"key": None, "data": {}}


def _config_path() -> Path:
    p = Path(os.getenv("MCP_CONFIG_FILE", "/app/memory/mcp_servers.json"))
    if not p.exists() and Path("memory/mcp_servers.json").exists():
        p = Path("memory/mcp_servers.json")
    return p


def _server_notes() -> Dict[str, str]:
    try:
        p = _config_path()
        key = (str(p), p.stat().st_mtime)
    except OSError:
        return {}
    if _NOTES_CACHE["key"] == key:
        return _NOTES_CACHE["data"]
    notes: Dict[str, str] = {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        for name, info in (data or {}).items():
            if isinstance(info, dict) and isinstance(info.get("usage_notes"), str):
                txt = " ".join(info["usage_notes"].split())
                if txt:
                    notes[name] = txt
    except Exception:
        notes = {}
    _NOTES_CACHE.update(key=key, data=notes)
    return notes


# ---------------------------------------------------------------------------
# 1. Prompt docs
# ---------------------------------------------------------------------------

def render_mcp_docs(registry: Dict[str, tuple], per_tool_chars: int = 320,
                    total_chars: int = 7000,
                    notes: Optional[Dict[str, str]] = None) -> str:
    """Prompt block: generic rules + per-tool exact signature and description."""
    if not registry:
        return ""
    lines = [MCP_RULES]
    used = len(MCP_RULES)
    notes = _server_notes() if notes is None else notes
    noted = set()
    for name in sorted(registry):
        _srv, _tool, desc, schema = registry[name]
        if _srv in notes and _srv not in noted:
            noted.add(_srv)
            note_line = f"[{_srv} server notes] {notes[_srv][:1500]}"
            lines.append(note_line)
            used += len(note_line)
        sig = signature(_tool, schema)
        desc = " ".join((desc or name).split())
        arg_notes = []
        for k, v in _props(schema).items():
            d = " ".join(str(v.get("description", "")).split())
            if d:
                arg_notes.append(f"{k}: {d[:110]}")
        full = f"- <skill:{name}>{sig}</skill:{name}>\n    {desc[:per_tool_chars]}"
        if arg_notes:
            full += "\n    args: " + " | ".join(arg_notes)
        # Over budget: degrade to a compact line so every tool stays visible.
        if used + len(full) > total_chars:
            full = f"- <skill:{name}>{sig}</skill:{name}> {desc[:80]}"
        lines.append(full)
        used += len(full)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 2. Tag normalization
# ---------------------------------------------------------------------------

def _alias_map(registry: Dict[str, tuple]) -> Dict[str, str]:
    """All the wrong-but-guessable names a model might emit -> real tag name."""
    aliases: Dict[str, str] = {}
    for reg_name, (srv, tool, _d, _s) in registry.items():
        s, t = _SAFE.sub("_", srv), _SAFE.sub("_", tool)
        for a in (
            f"mcp_client.{srv}.{tool}", f"mcp_client.{s}.{t}",
            f"{srv}.{tool}", f"{s}.{t}",
            f"{s}_{t}", f"mcp.{s}.{t}",
        ):
            if a != reg_name:
                aliases.setdefault(a, reg_name)
    return aliases


def normalize_mcp_tags(text: str, registry: Dict[str, tuple]) -> str:
    """Rewrite <skill:cloudflare.search> / <skill:mcp_client.cloudflare.search>
    to the registered name, on both opening and closing tags."""
    if not text or not registry or "skill:" not in text:
        return text
    aliases = _alias_map(registry)
    if not aliases:
        return text

    def fix(m: "re.Match") -> str:
        slash, name = m.group(1), m.group(2)
        real = aliases.get(name)
        return f"<{slash}skill:{real}>" if real else m.group(0)

    return re.sub(r"<(/?)skill:([\w.]+)>", fix, text)


# ---------------------------------------------------------------------------
# 3. Argument parsing + validation
# ---------------------------------------------------------------------------

def _coerce(value: str, spec: dict) -> Any:
    t = spec.get("type")
    v = value.strip()
    try:
        if t == "integer":
            return int(v)
        if t == "number":
            return float(v)
        if t == "boolean":
            return v.lower() in ("1", "true", "yes", "on")
        if t in ("object", "array"):
            return json.loads(v)
    except (ValueError, json.JSONDecodeError):
        return value
    return value


def _strip_fences(raw: str) -> str:
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", raw, flags=re.DOTALL)
    return m.group(1) if m else raw


def _help(tool: str, schema: Any) -> str:
    return f"Signature: {tool}({signature(tool, schema)})  (* = required)"


def parse_mcp_args(entry: tuple, raw: str) -> Tuple[Dict[str, Any], Optional[str]]:
    """Turn a tag body into an arguments dict, or an error the model can act on.

    - JSON object body        -> used as-is (invalid JSON is an ERROR, not silent {})
    - tool has 1 property     -> the whole body is that argument (commas safe)
    - tool has N properties   -> split on commas into the first N-1, remainder
                                 goes to the last (so code/text last stays intact)
    """
    _srv, tool, _desc, schema = entry
    props = _props(schema)
    names = list(props)
    req = _required(schema)
    raw = _strip_fences((raw or "").strip())

    args: Dict[str, Any] = {}
    if raw.startswith("{"):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            return {}, (f"Argument error for '{tool}': body is not valid JSON ({e.msg} at "
                        f"char {e.pos}). Escape quotes/newlines inside strings. "
                        + _help(tool, schema))
        if not isinstance(parsed, dict):
            return {}, f"Argument error for '{tool}': expected a JSON object. " + _help(tool, schema)
        args = parsed
    elif raw:
        if len(names) == 1:
            args = {names[0]: _coerce(raw, props[names[0]])}
        elif len(names) > 1:
            vals = [v.strip().strip("\"'") if i < len(names) - 1 else v.strip()
                    for i, v in enumerate(raw.split(",", len(names) - 1))]
            args = {names[i]: _coerce(v, props[names[i]]) for i, v in enumerate(vals)}
        else:
            return {}, (f"Argument error for '{tool}': this tool takes no named arguments "
                        "but a non-JSON body was given. Send a JSON object.")

    missing = [r for r in req if r not in args or args[r] in (None, "")]
    if missing:
        unknown = [k for k in args if props and k not in props]
        hint = f" Unknown argument(s) given: {', '.join(unknown)}." if unknown else ""
        return {}, (f"Argument error for '{tool}': missing required argument(s): "
                    f"{', '.join(missing)}.{hint} " + _help(tool, schema))
    return args, None