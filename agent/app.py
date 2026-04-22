"""
TrinityClaw AI Agent

"""

VERSION = "1.3"

import os
import base64
import tempfile
import threading
import requests
import chromadb
import json
import re
import time
import hashlib
import hmac
import math
import uuid
from datetime import datetime
from fastapi import FastAPI, HTTPException, Depends, Security, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv
import uvicorn
from typing import Optional, Dict, Any, List
import sys
from pathlib import Path
import ast
import importlib
import importlib.util
import subprocess
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import queue as _queue_module
import asyncio
import string as _string

load_dotenv()

# ── File cache: avoid re-reading stable files on every request ────────────────
class _FileCache:
    """Read a file once; return cached content until mtime changes on disk."""
    def __init__(self):
        self._cache: dict = {}  # path -> (mtime, content)

    def read_text(self, path: str, encoding: str = "utf-8", default: str = "") -> str:
        try:
            mtime = os.path.getmtime(path)
            if path in self._cache and self._cache[path][0] == mtime:
                return self._cache[path][1]
            with open(path, encoding=encoding) as _f:
                content = _f.read()
            self._cache[path] = (mtime, content)
            return content
        except Exception:
            return default

_fcache = _FileCache()
# ─────────────────────────────────────────────────────────────────────────────
print("🚀 TrinityClaw Agent is starting up...")

# Add skills directory to path
SKILLS_DIR = Path(__file__).parent / "skills"
sys.path.insert(0, str(SKILLS_DIR))
sys.path.insert(0, str(Path(__file__).parent))

app = FastAPI()

# Enable CORS — open for local use only (agent runs on localhost, not exposed to internet)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── API Key Security ─────────────────────────────────────────────────────────
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

def verify_api_key(api_key: str = Security(_api_key_header)) -> str:
    """Require X-API-Key header on sensitive endpoints.
    Fails closed — if TRINITY_API_KEY is not set, all requests are denied.
    Returns the validated API key so callers can use it for rate-limit keying."""
    expected = os.getenv("TRINITY_API_KEY", "")
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Agent is not configured: TRINITY_API_KEY is not set"
        )
    if not hmac.compare_digest(api_key or "", expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-API-Key header"
        )
    return api_key

# Global skills registry
skills: Dict[str, Any] = {}
skill_metadata: Dict[str, Dict] = {}

# Full tools schema built once at startup; filtered per-request for token efficiency
_TOOLS_SCHEMA_CACHE: List[Dict] = []

# Skills only injected into the tools schema when the message matches these keywords.
# Everything NOT listed here is treated as always-on (lightweight/utility skills).
_SKILL_DOMAIN_MAP: Dict[str, frozenset] = {
    "gmail_reader":    frozenset({"email", "gmail", "mail", "inbox", "send", "draft", "reply", "unread", "compose", "attachment", "emails"}),
    "google_calendar": frozenset({"calendar", "meeting", "schedule", "event", "appointment", "slot", "availability", "book", "rsvp", "reminder"}),
    "google_drive":    frozenset({"drive", "gdrive", "document", "doc", "spreadsheet", "sheet", "folder", "upload", "gdoc", "gsheet", "docs"}),
    "browser_session": frozenset({"browse", "browser", "click", "screenshot", "scrape", "navigate", "url", "website", "page", "login", "fill", "form", "headless", "selenium", "playwright", "web page"}),
    "web_builder":     frozenset({"html", "css", "scaffold", "landing", "frontend", "webpage", "website", "web app", "template", "serve", "deploy site"}),
    "autoimprove":     frozenset({"autoimprove", "auto-improve", "audit code", "refactor", "pattern mining", "improvement loop"}),
}

# ── Session Memory (in-process, cleared on restart) ───────────────────────
SESSION_MAX_MESSAGES = 16        # 8 turns (user + assistant pairs) for cloud models
SESSION_MAX_MESSAGES_LOCAL = 8   # 4 turns for local models — tighter context windows
SESSION_TIMEOUT_MINUTES = 120    # auto-expire after 2h inactivity
SESSION_SUMMARY_KEEP = 4         # Keep only last 4 messages verbatim (reduced from 6)
JSONL_MAX_LINES = 500            # compact session_logs.jsonl when it exceeds this
TOOL_RESULT_PRUNE_CHARS = 200    # prune tool results longer than this in older turns (reduced from 300)
TOOL_RESULT_PROTECT_RECENT = 2   # always keep the last N tool results verbatim (reduced from 4)
MAX_ITERATIONS = int(os.getenv("AGENT_MAX_ITERATIONS", "20"))
session_store: Dict[str, Dict] = {}

# ── ChromaDB query deduplication cache ────────────────────────────────────────
# Keyed by (session_id:query_hash). Skips embed+network call when the same query
# was answered recently. Caches misses too — avoids re-querying known dead-ends.
_chroma_query_cache: Dict[str, tuple] = {}   # key -> (chroma_context_str, timestamp)
CHROMA_CACHE_TTL  = 300   # seconds before a cache entry expires
CHROMA_CACHE_MAX  = 100   # evict oldest entry when cache exceeds this size

# ── Per-session daily-memory cache ────────────────────────────────────────────
# Daily journal + user facts don't change within a session — build once, reuse.
# Keyed by session_id; invalidated when session expires (see get_session_history).
_session_daily_memory: Dict[str, str] = {}  # session_id -> _daily_memory_block
# ─────────────────────────────────────────────────────────────────────────────

# ── Rate Limiting ─────────────────────────────────────────────────────────────
RATE_LIMIT_RPM = int(os.getenv("CHAT_RATE_LIMIT_RPM", "20"))  # requests per minute per API key
_rate_timestamps: Dict[str, list] = {}  # key_id -> list of request timestamps

# ── SSE Streaming support ─────────────────────────────────────────────────────
# Thread-local queue: set by /chat/stream before running chat() in a worker
# thread. All _stream_emit calls are no-ops on normal /chat requests.
_stream_local = threading.local()

def _stream_emit(event: dict) -> None:
    """Put an SSE event into the current thread's stream queue, if one is active."""
    q = getattr(_stream_local, "queue", None)
    if q is not None:
        try:
            q.put_nowait(event)
        except Exception:
            pass

def _check_rate_limit(api_key: str):
    """Raise HTTP 429 if the API key exceeds RATE_LIMIT_RPM requests in the last 60 seconds.
    Keyed on a short hash of the API key — not session_id — to prevent bypass via fake session IDs."""
    import time
    key_id = hashlib.sha256(api_key.encode()).hexdigest()[:16]
    now = time.time()
    window = 60.0
    timestamps = _rate_timestamps.get(key_id, [])
    # Drop entries outside the window
    timestamps = [t for t in timestamps if now - t < window]
    if len(timestamps) >= RATE_LIMIT_RPM:
        oldest = timestamps[0]
        retry_after = int(window - (now - oldest)) + 1
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit exceeded: {RATE_LIMIT_RPM} requests/min. Retry in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )
    timestamps.append(now)
    _rate_timestamps[key_id] = timestamps

def _extract_short_doc(full_doc: str) -> str:
    """Option A fallback: first segment of DOC before the first ';', capped at 120 chars."""
    if not full_doc or full_doc == "No description":
        return full_doc
    first = full_doc.split(";")[0].strip()
    return first[:120] if len(first) > 120 else first


def _est_tokens(text: str) -> int:
    """Estimate token count via char/4 approximation (no tiktoken dependency)."""
    return max(1, len(text) // 4)


def _classify_skill_error(error_msg: str) -> str:
    """Derive a specific error sub-type from an error message string.

    Used when we have no exception object — only the string returned by a skill.
    Produces fine-grained lesson keys instead of the generic 'skill_error' bucket,
    which was collapsing 18-25 distinct daily errors into one entry and preventing
    per-pattern deduplication.
    """
    m = error_msg.lower()
    if "timed out" in m or "timeout" in m:
        return "TimeoutError"
    if "attributeerror" in m or "has no attribute" in m or "'nonetype'" in m:
        return "AttributeError"
    if "typeerror" in m or "argument" in m and ("unexpected" in m or "missing" in m or "takes" in m):
        return "TypeError"
    if "valueerror" in m or "invalid" in m and ("value" in m or "argument" in m):
        return "ValueError"
    if "keyerror" in m or "key not found" in m:
        return "KeyError"
    if "filenotfounderror" in m or "no such file" in m:
        return "FileNotFoundError"
    if "connectionerror" in m or "connection refused" in m or "connect" in m and "fail" in m:
        return "ConnectionError"
    if "permissionerror" in m or "permission denied" in m:
        return "PermissionError"
    return "skill_error"


def load_skills_improved():
    """
    Improved skill loading with cache invalidation and metadata extraction.
    """
    global skills, skill_metadata

    # Clear old skill modules from cache to ensure fresh reloads
    modules_to_remove = [k for k in sys.modules.keys() if k.startswith('skills.') or k in skills]
    for mod in modules_to_remove:
        if mod in sys.modules:
            del sys.modules[mod]

    skills = {}
    skill_metadata = {}

    # Load from skills directory
    skills_path = Path(__file__).parent / "skills"
    if not skills_path.exists():
        print(f"⚠️ Skills directory not found: {skills_path}")
        return skills

    for skill_file in skills_path.rglob("*.py"):
        if skill_file.name.startswith("_"):
            continue

        try:
            # Read file to extract metadata without executing
            content = skill_file.read_text()

            # Extract DOC string using regex (safer than AST for this simple case)
            doc_match = re.search(r'DOC\s*=\s*["\'](.+?)["\']', content, re.DOTALL)
            doc = doc_match.group(1) if doc_match else "No description"

            # Extract functions using AST (safe parsing)
            try:
                tree = ast.parse(content)
                functions = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.FunctionDef):
                        func_name = node.name
                        if not func_name.startswith('_'):
                            # Extract parameters
                            args = [arg.arg for arg in node.args.args]
                            functions.append({"name": func_name, "args": args})
            except Exception:
                functions = []

            # Import the module
            module_name = f"skills.{skill_file.stem}"
            spec = importlib.util.spec_from_file_location(module_name, skill_file)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            # Fallback: if AST found no functions, inspect the loaded module directly
            if not functions:
                import inspect as _inspect
                functions = [
                    {"name": n, "args": list(_inspect.signature(f).parameters.keys())}
                    for n, f in _inspect.getmembers(module, _inspect.isfunction)
                    if not n.startswith('_') and f.__module__ == module_name
                ]

            skill_name = skill_file.stem
            full_doc = getattr(module, 'DOC', doc)
            skills[skill_name] = module
            skill_metadata[skill_name] = {
                "doc": full_doc,
                "short_doc": getattr(module, 'SHORT_DOC', None) or _extract_short_doc(full_doc),
                "functions": functions,
                "path": str(skill_file),
                "loaded_at": datetime.now().isoformat()
            }

            print(f"✅ Loaded skill: {skill_name}")

        except Exception as e:
            print(f"❌ Failed to load skill {skill_file}: {e}")

    return skills

def reload_skills():
    """Reload all skills with full cache clearing."""
    global skills, _TOOLS_SCHEMA_CACHE
    skills = load_skills_improved()
    _TOOLS_SCHEMA_CACHE = _build_tools_schema()
    print(f"✅ Tools schema cache rebuilt ({len(_TOOLS_SCHEMA_CACHE)} tool entries)")
    return f"Reloaded {len(skills)} skills"


def _build_tools_schema() -> list:
    """
    Build an OpenAI-style tools list from all loaded skill functions.
    Each tool name is 'skill_name__func_name' (double underscore separator).
    Called once per chat request for cloud (native function calling) mode.
    """
    import inspect
    tools = []
    for skill_name, module in skills.items():
        for func_info in skill_metadata.get(skill_name, {}).get("functions", []):
            func_name = func_info["name"]
            func = getattr(module, func_name, None)
            if not func or not callable(func):
                continue

            doc = (func.__doc__ or "").strip()
            # First paragraph = description (collapse newlines)
            description = doc.split("\n\n")[0].replace("\n", " ").strip()[:1024]

            # Parse "Args:" block from docstring for per-parameter descriptions
            arg_descs: Dict[str, str] = {}
            if "Args:" in doc:
                args_text = doc.split("Args:")[1].split("\n\n")[0]
                for line in args_text.splitlines():
                    line = line.strip()
                    if ": " in line:
                        aname, adesc = line.split(":", 1)
                        arg_descs[aname.strip()] = adesc.strip()[:256]

            # Build JSON Schema parameters from function signature
            try:
                sig = inspect.signature(func)
            except (ValueError, TypeError):
                continue

            _ANNOTATION_TYPE_MAP = {int: "integer", float: "number", bool: "boolean", str: "string"}

            properties: Dict[str, Any] = {}
            required: List[str] = []
            for pname, param in sig.parameters.items():
                if pname == "self":
                    continue
                if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                    continue  # skip *args / **kwargs — not representable in JSON Schema
                ann = param.annotation
                json_type = _ANNOTATION_TYPE_MAP.get(ann, "string") if ann is not inspect.Parameter.empty else "string"
                prop: Dict[str, Any] = {"type": json_type}
                if pname in arg_descs:
                    prop["description"] = arg_descs[pname]
                if param.default is inspect.Parameter.empty:
                    required.append(pname)
                else:
                    default_str = repr(param.default)
                    existing_desc = prop.get("description", "")
                    prop["description"] = (
                        (existing_desc + f" (default: {default_str})").strip()
                    )
                properties[pname] = prop

            tool_name = f"{skill_name}__{func_name}"[:64]
            tools.append({
                "type": "function",
                "function": {
                    "name": tool_name,
                    "description": description or tool_name,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            })
    return tools


def _build_tools_schema_for_request(message: str, used_skills: Optional[set] = None) -> List[Dict]:
    """
    Return a per-request filtered tools schema from _TOOLS_SCHEMA_CACHE.

    Core strategy:
    - Skills NOT listed in _SKILL_DOMAIN_MAP are always included (lightweight/utility).
    - Skills IN _SKILL_DOMAIN_MAP are only included when the message contains matching
      keywords OR the skill was already called earlier in this request.
    - Falls back to the full cache if the cache is empty (first request before startup
      cache is built, or after a failed reload).
    """
    if not _TOOLS_SCHEMA_CACHE:
        return _build_tools_schema()

    msg_words = set(message.lower().split())
    heavy_skills = set(_SKILL_DOMAIN_MAP.keys())

    # Which heavy skills are relevant to this message?
    relevant_heavy: set = set()
    for skill_name, keywords in _SKILL_DOMAIN_MAP.items():
        if keywords & msg_words:
            relevant_heavy.add(skill_name)

    # Also include every skill that was already used in this request's agentic loop
    if used_skills:
        relevant_heavy.update(used_skills)

    filtered = [
        t for t in _TOOLS_SCHEMA_CACHE
        if t["function"]["name"].split("__")[0] not in heavy_skills
        or t["function"]["name"].split("__")[0] in relevant_heavy
    ]

    _total = len(_TOOLS_SCHEMA_CACHE)
    _kept  = len(filtered)
    if _kept < _total:
        print(f"🔍 Skill preview: {_kept}/{_total} tool entries (saved ~{(_total - _kept) * 80} tokens)")

    return filtered


def call_skill_improved(skill_name: str, function_name: str, /, *args, **kwargs):
    """
    Improved skill calling with better error handling and logging.
    """
    if skill_name not in skills:
        raise ValueError(f"Skill '{skill_name}' not found. Available: {list(skills.keys())}")

    module = skills[skill_name]

    if not function_name:
        # Default to first non-private function
        funcs = [f for f in dir(module) if not f.startswith('_') and callable(getattr(module, f))]
        if not funcs:
            raise ValueError(f"No callable functions in skill '{skill_name}'")
        function_name = funcs[0]

    if not hasattr(module, function_name):
        raise ValueError(f"Function '{function_name}' not found in skill '{skill_name}'")

    func = getattr(module, function_name)

    # Type-coerce args/kwargs based on the function's annotations so the skill-tag
    # path (execute_skill_tags → parse_skill_args) behaves like the native tool-call
    # path (_execute_tool_calls), which already does this coercion. Without it, every
    # argument from parse_skill_args arrives as a string, causing TypeErrors when a
    # skill expects int/bool/float (e.g. run_loop("10") instead of run_loop(10)).
    try:
        import inspect as _ci
        _sig = _ci.signature(func)
        _CMAP = {
            bool:  lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes"),
            int:   lambda v: v if isinstance(v, int) else int(v),
            float: lambda v: v if isinstance(v, float) else float(v),
        }
        _params = list(_sig.parameters.values())
        # Coerce positional args
        _coerced_args = list(args)
        for _i, _val in enumerate(_coerced_args):
            if _i < len(_params) and _params[_i].annotation is not _ci.Parameter.empty:
                _cfn = _CMAP.get(_params[_i].annotation)
                if _cfn:
                    try:
                        _coerced_args[_i] = _cfn(_val)
                    except (ValueError, TypeError):
                        pass
        args = tuple(_coerced_args)
        # Coerce keyword args
        for _kname, _kval in list(kwargs.items()):
            _kparam = _sig.parameters.get(_kname)
            if _kparam and _kparam.annotation is not _ci.Parameter.empty:
                _cfn = _CMAP.get(_kparam.annotation)
                if _cfn:
                    try:
                        kwargs[_kname] = _cfn(_kval)
                    except (ValueError, TypeError):
                        pass
    except (ValueError, TypeError):
        pass  # introspection failed — call with original args

    # Log the call
    print(f"🔧 Executing: {skill_name}.{function_name}(args={args}, kwargs={kwargs})")

    # Skills can declare their own SKILL_TIMEOUT (e.g. web_builder needs ~5 min
    # for vision LLM calls). Fall back to the global env default of 30s.
    _skill_module = skills.get(skill_name)
    _skill_declared_timeout = getattr(_skill_module, "SKILL_TIMEOUT", None)
    timeout_seconds = int(_skill_declared_timeout or os.getenv("SKILL_TIMEOUT_SECONDS", "30"))
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(func, *args, **kwargs)
            result = future.result(timeout=timeout_seconds)
        return {"success": True, "result": result, "skill": skill_name, "function": function_name}
    except FuturesTimeoutError:
        error_msg = f"Skill '{skill_name}.{function_name}' timed out after {timeout_seconds}s"
        print(f"⏱️ {error_msg}")
        return {"success": False, "error": error_msg, "skill": skill_name, "function": function_name}
    except Exception as e:
        error_msg = f"Error in {skill_name}.{function_name}: {str(e)}"
        print(f"❌ {error_msg}")
        try:
            si = skills.get("self_improvement")
            if si:
                si.record_mistake(skill_name, type(e).__name__, str(e))
        except Exception:
            pass
        return {"success": False, "error": error_msg, "skill": skill_name, "function": function_name}

# Load all skills on startup
print("\n🎯 Loading TrinityClaw Skills...")
skills = load_skills_improved()
print(f"✅ Loaded {len(skills)} skill(s)")
_TOOLS_SCHEMA_CACHE = _build_tools_schema()
print(f"✅ Tools schema cached ({len(_TOOLS_SCHEMA_CACHE)} tool entries)\n")

# Load system prompt template once at startup (re-read if file changes via _fcache at request time)
_SYSTEM_PROMPT_TEMPLATE = _string.Template(
    (Path(__file__).parent / "prompts" / "system.md").read_text(encoding="utf-8")
)

# Auto-start Telegram polling if credentials are present in .env
_telegram_mod = skills.get("telegram_bot")
if _telegram_mod:
    _tg_token = getattr(_telegram_mod, "_token", "")
    _tg_chat  = getattr(_telegram_mod, "_chat_id", "")
    if _tg_token and _tg_chat:
        print("📱 Auto-starting Telegram polling...")
        _tg_result = _telegram_mod.start_polling()
        print(f"📱 Telegram: {_tg_result.splitlines()[0]}")
    else:
        print("⚠️  Telegram: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set in .env — polling skipped")

print("🔍 Initializing connections...")

# Try to connect to Docker
docker_client = None
try:
    import docker
    docker_client = docker.from_env()
    print("✅ Docker sandbox enabled")
except Exception as e:
    print(f"⚠️  Docker sandbox disabled: {e}")

LITELLM_BASE = os.getenv("LITELLM_API_BASE", "http://litellm:4000")
CHROMA_HOST = os.getenv("CHROMA_HOST", "chroma")
API_KEY = os.getenv("LITELLM_MASTER_KEY")

# Memory file path
MEMORY_FILE = "/app/memory/session_logs.jsonl"
os.makedirs("/app/memory", exist_ok=True)

# Initialize ChromaDB with error handling
try:
    chroma_client = chromadb.HttpClient(host=CHROMA_HOST, port=8000)
    collection = chroma_client.get_or_create_collection(name="trinity_memory")
    print("✅ ChromaDB connected")
except Exception as e:
    print(f"⚠️  ChromaDB connection failed: {e}")
    chroma_client = None
    collection = None

class PromptRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=10000)
    model: str = "trinity-default"
    image: Optional[str] = None
    document: Optional[str] = None       # base64 dataURL of uploaded document
    document_name: Optional[str] = None  # original filename
    session_id: Optional[str] = None
    require_verification: bool = True

class TranscribeRequest(BaseModel):
    audio: str          # base64-encoded audio bytes
    filename: str = "audio.ogg"  # hint for file extension / mime type

class SkillRequest(BaseModel):
    skill: str
    function: str
    args: Optional[List] = None
    kwargs: Optional[Dict] = None

class VerificationRequest(BaseModel):
    action_description: str
    verification_skill: str
    verification_args: List[str]

# ============================================================================
# 🔧 CONFIG ENDPOINTS (For UI Settings Panel)
# ============================================================================

class ConfigUpdateRequest(BaseModel):
    skill: str  # "model" or "api"
    function: str  # "model_name", "key", or "base"
    args: Optional[List[str]] = None

class TelegramConfigRequest(BaseModel):
    token: Optional[str] = None
    chat_id: Optional[str] = None

@app.get("/config/get-all", dependencies=[Depends(verify_api_key)])
def get_all_config():
    """
    Get all LiteLLM configuration values from litellm_config.yaml and .env
    """
    try:
        try:
            import yaml
        except ImportError:
            raise HTTPException(status_code=500, detail="pyyaml not installed")
        
        config_path = "/app/litellm_config.yaml"
        
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Config file not found: {config_path}")
        
        # Read LiteLLM config
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        if not config or 'model_list' not in config or not config['model_list']:
            raise ValueError("Invalid config structure: missing model_list")
        
        model_entry = config['model_list'][0]
        params = model_entry.get('litellm_params', {})
        
        # Parse api_key to get env var name
        api_key_env = params.get('api_key', 'MOONSHOT_API_KEY')
        if api_key_env.startswith('os.environ/'):
            api_key_env = api_key_env.replace('os.environ/', '')
        
        return {
            "success": True,
            "data": {
                "model": params.get('model', 'unknown'),
                "api_base": params.get('api_base', ''),
                "api_key_env": api_key_env
            }
        }
        
    except Exception as e:
        print(f"⚠️ Error reading config: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to read config: {str(e)}")

@app.post("/config/update", dependencies=[Depends(verify_api_key)])
def update_config(req: ConfigUpdateRequest):
    """
    Update LiteLLM configuration from UI.
    
    Expected format:
    - skill: "model" or "api"
    - function: "model_name", "key", or "base"
    - args: [new_value]
    """
    try:
        try:
            import yaml
        except ImportError:
            return _update_config_fallback(req)
        
        config_path = "/app/litellm_config.yaml"
        
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Config file not found: {config_path}")
        
        with open(config_path, 'r', encoding='utf-8') as f:
            config = yaml.safe_load(f)
        
        if not config or 'model_list' not in config or not config['model_list']:
            raise ValueError("Invalid config structure: missing model_list")
        
        config_type = req.skill
        config_key = req.function
        new_value = (req.args or [None])[0]
        
        if not new_value:
            raise ValueError("No new value provided")
        
        model_entry = config['model_list'][0]
        params = model_entry.setdefault('litellm_params', {})
        
        if config_type == "model" and config_key == "model_name":
            params['model'] = new_value
        elif config_type == "api" and config_key == "key":
            params['api_key'] = f"os.environ/{new_value}"
        elif config_type == "api" and config_key == "base":
            params['api_base'] = new_value.rstrip()
        else:
            raise ValueError(f"Unknown config update: {config_type}.{config_key}")
        
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        
        return {
            "success": True, 
            "message": f"Updated {config_type}.{config_key}",
            "note": "Config saved. Restart LiteLLM container for changes to take effect: docker-compose restart litellm"
        }
        
    except Exception as e:
        print(f"⚠️ Config update error: {e}")
        raise HTTPException(status_code=400, detail=f"Config update failed: {str(e)}")

def _update_config_fallback(req: ConfigUpdateRequest):
    """Fallback config updater if pyyaml is not available"""
    config_path = "/app/litellm_config.yaml"
    
    if not os.path.exists(config_path):
        raise FileNotFoundError(f"Config file not found: {config_path}")
    
    with open(config_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    config_type = req.skill
    config_key = req.function
    new_value = (req.args or [None])[0]
    
    if not new_value:
        raise ValueError("No new value provided")
    
    if config_type == "model" and config_key == "model_name":
        content = re.sub(r'(^\s*model:\s*).+$', rf'\1{new_value}', content, flags=re.MULTILINE)
    elif config_type == "api" and config_key == "key":
        content = re.sub(r'(^\s*api_key:\s*).+$', rf'\1os.environ/{new_value}', content, flags=re.MULTILINE)
    elif config_type == "api" and config_key == "base":
        content = re.sub(r'(^\s*api_base:\s*).+$', rf'\1{new_value.rstrip()}', content, flags=re.MULTILINE)
    else:
        raise ValueError(f"Unknown config update: {config_type}.{config_key}")
    
    with open(config_path, 'w', encoding='utf-8') as f:
        f.write(content)
    
    return {
        "success": True, 
        "message": f"Updated {config_type}.{config_key} (fallback mode)",
        "note": "Config saved. Restart LiteLLM for changes to take effect."
    }

@app.post("/config/update-api-key", dependencies=[Depends(verify_api_key)])
def update_api_key(req: ConfigUpdateRequest):
    """
    Update actual API key value in .env file.

    Expects:
    - skill: API key variable name (e.g., "MOONSHOT_API_KEY")
    - args: ["actual_key_value"]
    """
    # Only these keys may be updated via the API — prevents overwriting auth credentials
    _ALLOWED_ENV_KEYS = frozenset({
        "MOONSHOT_API_KEY", "NVIDIA_API_KEY", "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY", "TAVILY_API_KEY", "OLLAMA_MODEL",
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_FROM",
        "WHISPER_MODEL", "CHAT_RATE_LIMIT_RPM",
    })
    try:
        env_path = "/app/.env"
        api_key_name = req.skill
        api_key_value = (req.args or [None])[0]

        if not api_key_value:
            raise ValueError("No API key value provided")

        if api_key_name not in _ALLOWED_ENV_KEYS:
            raise ValueError(f"'{api_key_name}' is not an allowed key. Permitted keys: {sorted(_ALLOWED_ENV_KEYS)}")
        
        if os.path.exists(env_path):
            with open(env_path, 'r', encoding='utf-8') as f:
                env_content = f.read()
        else:
            env_content = ""
        
        pattern = f'^{re.escape(api_key_name)}=.*$'
        
        if re.search(pattern, env_content, re.MULTILINE):
            env_content = re.sub(pattern, f'{api_key_name}={api_key_value}', env_content, flags=re.MULTILINE)
        else:
            if env_content and not env_content.endswith('\n'):
                env_content += '\n'
            env_content += f'{api_key_name}={api_key_value}\n'
        
        with open(env_path, 'w', encoding='utf-8') as f:
            f.write(env_content)
        
        return {
            "success": True,
            "message": f"Updated {api_key_name}",
            "note": "API key updated in .env file"
        }
        
    except Exception as e:
        print(f"⚠️ API key update error: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to update API key: {str(e)}")

# ============================================================================
# 🔧 TELEGRAM CONFIG ENDPOINTS
# ============================================================================

@app.get("/config/telegram/get", dependencies=[Depends(verify_api_key)])
def get_telegram_config():
    """
    Get Telegram bot configuration from telegram_bot.py
    """
    try:
        telegram_file = "/app/skills/core/telegram_bot.py"
        
        if not os.path.exists(telegram_file):
            return {
                "success": True,
                "data": {
                    "token": "",
                    "chat_id": ""
                },
                "note": "telegram_bot.py not found"
            }
        
        with open(telegram_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Extract token (look for patterns like TOKEN = "xxx" or _token = "xxx")
        token_match = re.search(r'(?:TOKEN|_token|telegram_token)\s*=\s*["\']([^"\']+)["\']', content, re.IGNORECASE)
        token = token_match.group(1) if token_match else ""
        
        # Extract chat_id
        chat_id_match = re.search(r'(?:CHAT_ID|_chat_id|telegram_chat_id)\s*=\s*["\']([^"\']+)["\']', content, re.IGNORECASE)
        chat_id = chat_id_match.group(1) if chat_id_match else ""
        
        return {
            "success": True,
            "data": {
                "token": "",  # Never return actual token for security
                "chat_id": chat_id,
                "token_exists": bool(token)
            }
        }
        
    except Exception as e:
        print(f"⚠️ Error reading Telegram config: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to read Telegram config: {str(e)}")

@app.post("/config/telegram/update", dependencies=[Depends(verify_api_key)])
def update_telegram_config(req: TelegramConfigRequest):
    """
    Update Telegram bot configuration in telegram_bot.py
    
    Expects JSON body:
    {
        "token": "1234567890:ABCdef...",  # Optional - leave blank to keep existing
        "chat_id": "-1234567890"          # Optional - leave blank to keep existing
    }
    """
    try:
        telegram_file = "/app/skills/core/telegram_bot.py"
        
        if not os.path.exists(telegram_file):
            raise FileNotFoundError(f"telegram_bot.py not found: {telegram_file}")
        
        new_token = req.token.strip() if req.token else ""
        new_chat_id = req.chat_id.strip() if req.chat_id else ""
        
        with open(telegram_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Update token if provided
        if new_token:
            if re.search(r'(?:TOKEN|_token|telegram_token)\s*=', content, re.IGNORECASE):
                content = re.sub(
                    r'((?:TOKEN|_token|telegram_token)\s*=\s*)["\'][^"\']*["\']',
                    rf'\1"{new_token}"',
                    content,
                    flags=re.IGNORECASE
                )
            else:
                content = f'TOKEN = "{new_token}"\n' + content
        
        # Update chat_id if provided
        if new_chat_id:
            if re.search(r'(?:CHAT_ID|_chat_id|telegram_chat_id)\s*=', content, re.IGNORECASE):
                content = re.sub(
                    r'((?:CHAT_ID|_chat_id|telegram_chat_id)\s*=\s*)["\'][^"\']*["\']',
                    rf'\1"{new_chat_id}"',
                    content,
                    flags=re.IGNORECASE
                )
            else:
                if 'TOKEN' in content:
                    content = re.sub(
                        r'(TOKEN\s*=\s*["\'][^"\']*["\'])',
                        rf'\1\nCHAT_ID = "{new_chat_id}"',
                        content
                    )
                else:
                    content = f'CHAT_ID = "{new_chat_id}"\n' + content
        
        with open(telegram_file, 'w', encoding='utf-8') as f:
            f.write(content)
        
        return {
            "success": True,
            "message": "Telegram configuration updated",
            "note": "Restart agent for changes to take effect"
        }
        
    except Exception as e:
        print(f"⚠️ Error updating Telegram config: {e}")
        raise HTTPException(status_code=400, detail=f"Failed to update Telegram config: {str(e)}")

# ============================================================================

class ModelSourceRequest(BaseModel):
    source: str  # "local" or "cloud"

@app.get("/config/model-source/get", dependencies=[Depends(verify_api_key)])
def get_model_source():
    """Get current model source (local Ollama or cloud API)."""
    current_source = os.getenv("MODEL_SOURCE", "cloud")
    return {
        "success": True,
        "model_source": current_source,
        "available_sources": ["cloud", "local"],
        "info": {
            "cloud": f"{LITELLM_BASE} (LiteLLM API)",
            "local": f"{os.getenv('OLLAMA_API_BASE', 'http://ollama:11434')} (Ollama)"
        }
    }

@app.post("/config/model-source/set", dependencies=[Depends(verify_api_key)])
def set_model_source(req: ModelSourceRequest):
    """Switch between local Ollama model and cloud LiteLLM API."""
    try:
        source = req.source.lower().strip()
        
        if source not in ["local", "cloud"]:
            raise ValueError("Invalid source. Use 'local' (Ollama) or 'cloud' (LiteLLM)")
        
        env_path = "/app/.env"
        
        if os.path.exists(env_path):
            with open(env_path, 'r', encoding='utf-8') as f:
                env_content = f.read()
        else:
            env_content = ""
        
        if "MODEL_SOURCE=" in env_content:
            env_content = re.sub(
                r'^MODEL_SOURCE=.*$',
                f'MODEL_SOURCE={source}',
                env_content,
                flags=re.MULTILINE
            )
        else:
            if env_content and not env_content.endswith('\n'):
                env_content += '\n'
            env_content += f'MODEL_SOURCE={source}\n'
        
        with open(env_path, 'w', encoding='utf-8') as f:
            f.write(env_content)
        
        os.environ['MODEL_SOURCE'] = source
        
        return {
            "success": True,
            "message": f"Model source switched to {source}",
            "new_source": source,
            "note": f"Next chat will use {source} model"
        }
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to update model source: {str(e)}")


# ── Ollama local-model config ─────────────────────────────────────────────────

class OllamaModelRequest(BaseModel):
    model: str

@app.get("/config/ollama/get", dependencies=[Depends(verify_api_key)])
def get_ollama_config():
    """Return the currently configured local Ollama model name."""
    return {
        "success": True,
        "model": os.getenv("OLLAMA_MODEL", "llama3.2-vision"),
        "base": os.getenv("OLLAMA_API_BASE", "http://ollama:11434"),
    }

@app.get("/config/ollama/models", dependencies=[Depends(verify_api_key)])
def list_ollama_models():
    """Return models currently pulled in Ollama (calls /api/tags)."""
    try:
        ollama_base = os.getenv("OLLAMA_API_BASE", "http://ollama:11434")
        resp = requests.get(f"{ollama_base}/api/tags", timeout=5)
        resp.raise_for_status()
        models = [m["name"] for m in resp.json().get("models", [])]
        return {"success": True, "models": models}
    except Exception as e:
        return {"success": False, "models": [], "error": str(e)}

@app.post("/config/ollama/set", dependencies=[Depends(verify_api_key)])
def set_ollama_model(req: OllamaModelRequest):
    """Persist a new OLLAMA_MODEL value to .env and apply it immediately."""
    try:
        model = req.model.strip()
        if not model:
            raise ValueError("Model name cannot be empty")

        env_path = "/app/.env"
        env_content = ""
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                env_content = f.read()

        if "OLLAMA_MODEL=" in env_content:
            env_content = re.sub(
                r"^OLLAMA_MODEL=.*$", f"OLLAMA_MODEL={model}",
                env_content, flags=re.MULTILINE
            )
        else:
            if env_content and not env_content.endswith("\n"):
                env_content += "\n"
            env_content += f"OLLAMA_MODEL={model}\n"

        with open(env_path, "w", encoding="utf-8") as f:
            f.write(env_content)

        os.environ["OLLAMA_MODEL"] = model
        return {"success": True, "model": model, "note": "Active immediately — no restart needed"}

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to set Ollama model: {str(e)}")


def save_to_jsonl(user_message: str, ai_reply: str, metadata: Optional[Dict] = None, session_id: Optional[str] = None):
    """Save conversation to JSONL file with metadata and unique entry ID."""
    try:
        entry = {
            "timestamp": datetime.now().isoformat(),
            "user": user_message,
            "assistant": ai_reply,
            "metadata": metadata or {},
            "id": str(uuid.uuid4()),
            "session_id": session_id
        }
        with open(MEMORY_FILE, "a") as f:
            f.write(json.dumps(entry) + "\n")
        # Index into FTS5 for episodic recall via notes.search_logs()
        try:
            _notes_mod = skills.get("notes")
            if _notes_mod and hasattr(_notes_mod, "fts_index_entry"):
                _notes_mod.fts_index_entry(
                    entry["id"], entry.get("session_id") or "",
                    entry["timestamp"], user_message, ai_reply,
                )
        except Exception:
            pass
        # Compact if the file has grown large (by size) or line count is high.
        # Both checks are O(1)/cheap and must agree with the guard inside compact_jsonl().
        try:
            _mf_size = os.path.getsize(MEMORY_FILE)
            if _mf_size > 2_000_000:
                compact_jsonl()
            else:
                # Fast line-count check: avoids reading the whole file on every save.
                with open(MEMORY_FILE, "rb") as _mf:
                    _mf_lines = _mf.read().count(b"\n")
                if _mf_lines > JSONL_MAX_LINES:
                    compact_jsonl()
        except OSError:
            pass
    except Exception as e:
        print(f"⚠️  Error saving to JSONL: {e}")

def load_past_context(n_messages: int = 5) -> str:
    """Load recent conversations from JSONL file."""
    try:
        if not os.path.exists(MEMORY_FILE):
            return "No prior conversations yet."

        with open(MEMORY_FILE, "r") as f:
            lines = f.readlines()

        if not lines:
            return "No prior conversations yet."

        recent = lines[-n_messages:] if len(lines) >= n_messages else lines
        context_parts = []
        for line in recent:
            try:
                entry = json.loads(line)
                context_parts.append(f"User: {entry['user']}\nAssistant: {entry['assistant']}")
            except json.JSONDecodeError:
                continue

        return "\n\n".join(context_parts) if context_parts else "No prior conversations yet."
    except Exception as e:
        print(f"⚠️  Error loading JSONL: {e}")
        return "No prior conversations yet."

def _sanitize_external_content(text: str, source: str = "unknown") -> str:
    """Scan externally-sourced text for prompt injection patterns before injecting
    it into the system prompt.

    Only applied to content that originates outside the agent's own logic:
    - ChromaDB results (may contain scraped web / document content)
    - lessons.jsonl fix_applied field (auto-written from skill error messages)

    Does NOT touch user messages, identity.md, notes, journal, or skill results —
    those are either trusted sources or user-controlled by design.

    Returns the original text unchanged if clean, or a safe placeholder if a
    pattern is detected, and prints a warning so the issue is visible in logs.
    """
    import unicodedata

    if not text or not isinstance(text, str):
        return text

    # Strip invisible / directional Unicode characters that can hide injections.
    _INVISIBLE = {
        "\u200b", "\u200c", "\u200d", "\u200e", "\u200f",  # zero-width / LTR / RTL marks
        "\u202a", "\u202b", "\u202c", "\u202d", "\u202e",  # directional overrides
        "\u2060", "\u2061", "\u2062", "\u2063", "\u2064",  # word joiner & invisible ops
        "\ufeff",                                           # BOM / zero-width no-break
    }
    cleaned = "".join(ch for ch in text if ch not in _INVISIBLE)

    # Classic prompt injection trigger phrases (case-insensitive).
    _INJECTION_PATTERNS = [
        r"ignore\s+(all\s+)?previous\s+instructions",
        r"disregard\s+(all\s+)?previous",
        r"new\s+instructions\s*:",
        r"system\s+prompt\s+override",
        r"you\s+are\s+now\s+(?!trinityclaw)",   # allow "you are now TrinityClaw"
        r"forget\s+(everything|all)\s+(you|above)",
        r"act\s+as\s+(?!an?\s+assistant)",       # allow "act as an assistant"
        r"jailbreak",
        r"dan\s+mode",
        r"prompt\s+injection",
        r"override\s+your\s+(instructions|programming|rules)",
        r"reveal\s+(your\s+)?(system\s+prompt|instructions|api\s+key)",
        r"print\s+(your\s+)?(system\s+prompt|instructions)",
        r"exfiltrate",
    ]

    import re
    for pattern in _INJECTION_PATTERNS:
        if re.search(pattern, cleaned, re.IGNORECASE):
            print(f"🛡️  Injection pattern blocked in [{source}]: matched /{pattern}/")
            return f"[content blocked: injection pattern detected in {source}]"

    # Return the invisible-char-stripped version (harmless cleanup even if no pattern matched).
    return cleaned


def _prune_tool_results(messages: List[Dict]) -> List[Dict]:
    """Replace content of old, large tool results with a short placeholder.

    Always preserves the last TOOL_RESULT_PROTECT_RECENT tool results verbatim so
    the model retains full context for the most recent actions. Older results that
    exceed TOOL_RESULT_PRUNE_CHARS are replaced to save tokens without losing
    the fact that the tool was called.
    """
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    protect_set = set(tool_indices[-TOOL_RESULT_PROTECT_RECENT:])
    pruned = []
    for i, msg in enumerate(messages):
        if (
            msg.get("role") == "tool"
            and i not in protect_set
            and isinstance(msg.get("content"), str)
            and len(msg["content"]) > TOOL_RESULT_PRUNE_CHARS
        ):
            msg = {**msg, "content": f"[tool result pruned — {len(msg['content'])} chars]"}
        # ALSO: Cap even protected tool results to prevent bloat
        elif (
            msg.get("role") == "tool"
            and isinstance(msg.get("content"), str)
            and len(msg["content"]) > 1000  # Hard cap for ANY tool result
        ):
            msg = {**msg, "content": msg["content"][:1000] + f"\n...[truncated, {len(msg['content'])} chars total]"}
        pruned.append(msg)
    return pruned


def _sanitize_orphaned_tool_pairs(messages: List[Dict]) -> List[Dict]:
    """Remove tool-result messages that have no matching assistant tool_call.

    When the rolling buffer trims old messages, assistant messages that carried
    tool_calls can end up in the summarized head while the corresponding tool-result
    messages land in the kept tail. Sending an orphaned tool-result to the API
    causes an error. This pass removes them.
    """
    # Collect all tool_call_ids that are referenced by assistant messages in the list.
    valid_call_ids: set = set()
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in (msg.get("tool_calls") or []):
                tc_id = tc.get("id") if isinstance(tc, dict) else None
                if tc_id:
                    valid_call_ids.add(tc_id)

    cleaned = []
    for msg in messages:
        if msg.get("role") == "tool":
            call_id = msg.get("tool_call_id")
            if call_id and call_id not in valid_call_ids:
                continue  # drop orphan
        cleaned.append(msg)
    return cleaned


def summarize_messages(messages: List[Dict], existing_summary: str = "") -> str:
    """Call the LLM to produce a structured summary of a message list.

    If *existing_summary* is provided the model updates it incrementally rather
    than regenerating from scratch, preserving information from earlier compressions.
    """
    if not messages:
        return ""

    transcript_parts = []
    for msg in messages:
        role = msg.get("role", "unknown")
        if role == "system":
            continue  # skip injected system context, not real conversation
        content = msg.get("content", "")
        if isinstance(content, list):  # multimodal content blocks
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        transcript_parts.append(f"{role.upper()}: {content[:800]}")

    if not transcript_parts:
        return ""

    transcript_text = "\n\n".join(transcript_parts)

    if existing_summary:
        # Iterative update: give the model the previous summary + new turns so it
        # can extend/correct rather than regenerate from scratch.
        system_instruction = (
            "You are updating an existing conversation summary with new turns.\n"
            "Rewrite the summary under these five headings — keep it concise:\n"
            "**Goal** | **Progress** | **Decisions** | **Files/Resources** | **Next Steps**\n\n"
            "RULES:\n"
            "- Keep the ENTIRE summary under 400 tokens total\n"
            "- Condense old information aggressively\n"
            "- Only preserve decisions and facts that are still relevant\n"
            "- Drop completed tasks and resolved items\n"
            "- Merge similar items together\n"
            "- Use bullet points, not paragraphs\n"
        )
        user_content = (
            f"PREVIOUS SUMMARY:\n{existing_summary}\n\n"
            f"NEW TURNS TO INCORPORATE:\n{transcript_text}"
        )
    else:
        # First compression: generate a structured summary from scratch.
        system_instruction = (
            "Summarize the following conversation under these five headings — keep it concise:\n"
            "**Goal** | **Progress** | **Decisions** | **Files/Resources** | **Next Steps**\n\n"
            "RULES:\n"
            "- Keep the ENTIRE summary under 300 tokens\n"
            "- Use bullet points, not paragraphs\n"
            "- Only include key decisions and current state\n"
            "- Skip minor details and completed steps\n"
        )
        user_content = transcript_text

    prompt = [
        {"role": "system", "content": system_instruction},
        {"role": "user",   "content": user_content},
    ]

    try:
        model_source = os.getenv("MODEL_SOURCE", "cloud")
        result = _call_llm(prompt, model_source, "trinity-default")
        summary = result.get("content") or "\n".join(transcript_parts[-4:])
        
        # HARD CAP: Truncate if summary is still too large
        if len(summary) > 1500:
            summary = summary[:1500] + "\n...[truncated]"
        
        return summary
    except Exception as e:
        print(f"⚠️  Memory summarization failed: {e}")
        return "\n".join(transcript_parts[-4:])  # graceful fallback


def compact_jsonl():
    """When session_logs.jsonl exceeds JSONL_MAX_LINES, summarize the oldest half and archive it."""
    if not os.path.exists(MEMORY_FILE):
        return
    try:
        with open(MEMORY_FILE, "r") as f:
            lines = f.readlines()

        if len(lines) <= JSONL_MAX_LINES and os.path.getsize(MEMORY_FILE) <= 10_000_000:
            return

        cutoff = len(lines) // 2
        old_lines = lines[:cutoff]
        keep_lines = lines[cutoff:]

        # Build message list from old entries for summarization
        old_messages = []
        for line in old_lines:
            try:
                entry = json.loads(line)
                if entry.get("user"):
                    old_messages.append({"role": "user", "content": entry["user"]})
                if entry.get("assistant"):
                    old_messages.append({"role": "assistant", "content": entry["assistant"]})
            except json.JSONDecodeError:
                continue

        print(f"📦 Compacting JSONL: summarizing {len(old_lines)} old entries...")
        summary_text = summarize_messages(old_messages)

        # Archive the raw old entries
        archive_path = MEMORY_FILE.replace(".jsonl", "_archive.jsonl")
        with open(archive_path, "a") as f:
            for line in old_lines:
                f.write(line)

        # Rewrite main file: summary entry + recent lines
        summary_entry = {
            "timestamp": datetime.now().isoformat(),
            "user": "[ARCHIVE SUMMARY]",
            "assistant": summary_text,
            "metadata": {"type": "archive_summary", "archived_count": len(old_lines)},
            "id": str(uuid.uuid4()),
            "session_id": "system"
        }
        with open(MEMORY_FILE, "w") as f:
            f.write(json.dumps(summary_entry) + "\n")
            for line in keep_lines:
                f.write(line)

        print(f"✅ JSONL compacted: {len(old_lines)} entries archived to {archive_path}")
    except Exception as e:
        print(f"⚠️  JSONL compact error: {e}")


# ── Correction Detection ─────────────────────────────────────────────────────
_CORRECTION_SIGNALS = [
    "that's wrong", "thats wrong", "that is wrong",
    "you're wrong", "youre wrong", "you are wrong",
    "incorrect", "that's not right", "thats not right",
    "no, ", "not quite", "try again",
    "you missed", "you forgot", "you should have",
    "that didn't work", "it didn't work", "that failed",
    "fix that", "fix this", "wrong answer",
]

_SKILL_RESULT_RE = re.compile(r'✅\s*([\w]+)\.([\w]+)')

def _detect_correction(user_msg: str) -> bool:
    """True if this message looks like a correction of the previous response."""
    msg = user_msg.lower()
    return any(signal in msg for signal in _CORRECTION_SIGNALS)

def _extract_skill_context(ai_reply: str, fallback: str) -> str:
    """Pull skill name from a ✅ result block in a previous AI reply."""
    match = _SKILL_RESULT_RE.search(ai_reply)
    return match.group(1) if match else fallback

# ─────────────────────────────────────────────────────────────────────────────

_IDENTITY_SECTION_RE = re.compile(
    r'<!-- TRINITY_START:(\w+) -->(.*?)<!-- TRINITY_END:\1 -->',
    re.DOTALL
)
_IDENTITY_TRIGGERS: dict = {
    "web_clone":       {"clone", "cloner", "cloning", "website_cloner", "scrape", "replicate", "copy site", "copy website"},
    "email":           {"email", "mail", "gmail", "compose", "inbox", "draft", "reply", "send message"},
    "web_design":      {"website", "html", "css", "site", "build", "scaffold", "design", "web", "page", "ui", "frontend", "webpage", "landing", "preview"},
    "decision_support": {"choose", "decide", "decision", "tradeoff", "option", "compare", "recommend", "evaluate", "which", "vs", "versus", "better", "should i"},
    "business_kb":     {"knowledge", "business", "kb", "client", "company", "brand", "knowled"},
}

def _build_identity(content: str, msg_words: set) -> str:
    """Strip conditional sections from identity.md that are irrelevant to the current message.

    Sections wrapped in <!-- TRINITY_START:name --> / <!-- TRINITY_END:name --> are only
    injected when the message contains at least one trigger keyword for that section.
    Everything outside those markers is always included.
    Saves ~2,500–3,000 tokens on non-clone, non-email conversations.
    """
    def _replacer(m: re.Match) -> str:
        name = m.group(1)
        body = m.group(2).strip()
        triggers = _IDENTITY_TRIGGERS.get(name, set())
        if not triggers or (msg_words & triggers):
            return "\n\n" + body + "\n\n"
        return "\n"  # section omitted — leave a single newline so surrounding text stays clean

    result = _IDENTITY_SECTION_RE.sub(_replacer, content)
    return re.sub(r'\n{3,}', '\n\n', result).strip()


def _detect_task_type(user_msg: str, skills_used: list) -> str:
    """Infer task category from message keywords and skills called."""
    msg = user_msg.lower()
    skills = {s.lower() for s in skills_used}
    if any(s.startswith("scheduler") for s in skills) or any(k in msg for k in ("schedule", "remind", "every day", "cron", "recurring", "at 9", "at 8", "daily", "weekly")):
        return "scheduler"
    if any(s.startswith("telegram") for s in skills) or any(k in msg for k in ("telegram", "message me", "notify me", "send me", "alert me")):
        return "telegram"
    if any(s.startswith("web") or s.startswith("url_monitor") for s in skills) or any(k in msg for k in ("fetch", "browse", "website", "url", "monitor", "http")):
        return "web"
    if any(s.startswith("files") or s.startswith("notes") for s in skills) or any(k in msg for k in ("file", "save", "note", "write to", "read", "delete")):
        return "files"
    return "chat"

def _compute_memory_score(dist: float, hit_count: int, timestamp_str: str,
                          alpha: float = 0.6, beta: float = 0.2, gamma: float = 0.2,
                          decay_rate: float = 0.05) -> float:
    """Combined memory relevance score: cosine similarity + usage frequency + recency.

    alpha:      weight for relevance  (cosine similarity, 1 - distance)
    beta:       weight for frequency  (log-scaled hit count)
    gamma:      weight for recency    (exponential decay over days)
    decay_rate: daily decay constant  (0.05 → ~70% retained after 7 days)
    Returns a value in [0, 1] — higher means more worth injecting.
    """
    relevance = max(0.0, 1.0 - dist)
    frequency = math.log1p(min(hit_count, 100)) / math.log1p(100)
    try:
        stored_dt = datetime.fromisoformat(timestamp_str)
        days_old = (datetime.now() - stored_dt).total_seconds() / 86400
    except Exception:
        days_old = 30
    recency = math.exp(-decay_rate * max(0.0, days_old))
    return alpha * relevance + beta * frequency + gamma * recency


def store_memory_separate(user_msg: str, ai_reply: str, task_type: str = "general", session_id: Optional[str] = None):
    """Store user query and AI response separately in ChromaDB for better retrieval."""
    if not collection:
        return

    try:
        timestamp = datetime.now().isoformat()

        # Store user query
        user_id = f"user_{str(uuid.uuid4())[:12]}"
        collection.add(
            documents=[user_msg],
            ids=[user_id],
            metadatas=[{
                "type": "user_query",
                "timestamp": timestamp,
                "task_type": task_type,
                "session_id": session_id,
                "hit_count": 0,
                "last_accessed": timestamp,
            }]
        )

        # Store AI response
        ai_id = f"ai_{str(uuid.uuid4())[:12]}"
        collection.add(
            documents=[ai_reply],
            ids=[ai_id],
            metadatas=[{
                "type": "ai_response",
                "timestamp": timestamp,
                "task_type": task_type,
                "parent_query_id": user_id,
                "session_id": session_id,
                "hit_count": 0,
                "last_accessed": timestamp,
            }]
        )
    except Exception as e:
        print(f"⚠️  Error storing in ChromaDB: {e}")

def load_session_from_disk(session_id: str) -> List[Dict]:
    """Parse session_logs.jsonl, convert to chat format, and hydrate session_store."""
    if not os.path.exists(MEMORY_FILE):
        return []

    messages = []
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line: continue
                try:
                    entry = json.loads(line)
                    entry_sid = entry.get("session_id") or "default"
                    if entry_sid != session_id:
                        continue
                    # Skip system archive summaries to avoid polluting recent context
                    if entry.get("user") == "[ARCHIVE SUMMARY]":
                        continue
                    if entry.get("user"):
                        messages.append({"role": "user", "content": entry["user"]})
                    if entry.get("assistant"):
                        messages.append({"role": "assistant", "content": entry["assistant"]})
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"⚠️  Disk session load failed for {session_id[:12]}: {e}")
        return []

    # Enforce context cap
    if len(messages) > SESSION_MAX_MESSAGES:
        messages = messages[-SESSION_MAX_MESSAGES:]

    # Hydrate in-memory store so subsequent turns don't re-parse JSONL
    session_store[session_id] = {
        "messages": messages,
        "last_active": datetime.now()
    }
    print(f"💾 Restored {len(messages)//2} turn(s) for session {session_id[:12]} from disk")
    return messages


def get_session_history(session_id: str) -> List[Dict]:
    """Return active session message history, auto-expiring stale sessions."""
    if not session_id:
        return []

    # ← Fallback to disk if not in RAM
    if session_id not in session_store:
        return load_session_from_disk(session_id)

    entry = session_store[session_id]
    elapsed_minutes = (datetime.now() - entry["last_active"]).total_seconds() / 60
    if elapsed_minutes > SESSION_TIMEOUT_MINUTES:
        msgs = [m for m in entry["messages"] if m.get("role") != "system"]
        if msgs and collection:
            try:
                summary = summarize_messages(msgs)
                if summary:
                    now_iso = datetime.now().isoformat()
                    collection.add(
                        documents=[summary],
                        ids=[f"session_summary_{str(uuid.uuid4())[:12]}"],
                        metadatas=[{"type": "session_summary", "session_id": session_id, "timestamp": now_iso, "turn_count": len(msgs) // 2, "hit_count": 0, "last_accessed": now_iso}]
                    )
                    print(f"💾 Stored expiry summary for session {session_id[:12]}...")
            except Exception as e:
                print(f"⚠️  Session expiry summary failed: {e}")
        del session_store[session_id]
        _session_daily_memory.pop(session_id, None)
        print(f"🕐 Session {session_id[:12]}... expired after {elapsed_minutes:.0f}min")
        return []
    return entry["messages"]

def save_session_history(session_id: str, messages: List[Dict], max_messages: int = SESSION_MAX_MESSAGES):
    """Persist updated history for a session, compressing oldest turns if over the cap.

    Compression pipeline:
      1. Prune large tool results in older turns to placeholders.
      2. Split into head (to summarise) + tail (to keep verbatim).
      3. Summarise the head — iteratively updating any existing summary so
         information is never lost across multiple compression cycles.
      4. Sanitise orphaned tool-call/result pairs so the API never sees a
         tool-result message whose matching assistant tool_call was trimmed away.
    """
    if not session_id:
        return

    # Phase 1 — prune large tool results in old turns before anything else.
    messages = _prune_tool_results(messages)

    if len(messages) > max_messages:
        tail = messages[-SESSION_SUMMARY_KEEP:]
        head = messages[:-SESSION_SUMMARY_KEEP]

        # Phase 3 — check whether the head already starts with a prior summary so
        # we can do an iterative update instead of a full regeneration.
        existing_summary = ""
        head_conv = []
        for m in head:
            if (
                m.get("role") == "system"
                and isinstance(m.get("content"), str)
                and m["content"].startswith("[EARLIER CONVERSATION SUMMARY]")
            ):
                existing_summary = m["content"].replace("[EARLIER CONVERSATION SUMMARY]\n", "", 1)
            else:
                head_conv.append(m)

        if head_conv:
            print(f"📝 Compressing {len(head_conv)} messages for session {session_id[:12]}..."
                  + (" (iterative update)" if existing_summary else ""))
            summary_text = summarize_messages(head_conv, existing_summary=existing_summary)
            summary_msg = {
                "role": "system",
                "content": f"[EARLIER CONVERSATION SUMMARY]\n{summary_text}"
            }
            messages = [summary_msg] + tail
        else:
            messages = tail

        # Phase 4 — drop tool-result messages orphaned by the trim.
        messages = _sanitize_orphaned_tool_pairs(messages)

    session_store[session_id] = {
        "messages": messages,
        "last_active": datetime.now()
    }

# ============================================================================
# VERIFICATION SYSTEM
# ============================================================================

def verify_action(description: str, skill_name: str, skill_args: List[str]) -> Dict:
    """
    Verify that an action was completed successfully by calling a verification skill.
    """
    print(f"🔍 Verifying: {description}")

    result = call_skill_improved(skill_name, skill_args[0] if skill_args else "ls", *skill_args[1:])

    success = result.get("success", False)
    if success:
        print(f"✅ Verification passed: {description}")
    else:
        print(f"❌ Verification failed: {description} - {result.get('error', 'Unknown error')}")

    return {
        "verified": success,
        "description": description,
        "check_result": result,
        "timestamp": datetime.now().isoformat()
    }

# ============================================================================
# SKILL TAG PARSING - IMPROVED
# ============================================================================

def parse_skill_args(content: str, max_positional: int = None) -> tuple:
    """
    Parse skill arguments intelligently.
    Handles: simple args, JSON objects, comma-separated values.

    max_positional: when provided, limits comma splitting to (max_positional - 1)
    splits so that content with commas (HTML, CSS, JS) is not shredded when
    passed as the last argument (e.g. write_file(project, filename, <html>...)).
    """
    content = content.strip()

    if not content:
        return [], {}

    # Try to detect if it's JSON
    if content.startswith('{') and content.endswith('}'):
        try:
            data = json.loads(content)
            return [], data
        except json.JSONDecodeError:
            pass

    # Try to detect if it's a list
    if content.startswith('[') and content.endswith(']'):
        try:
            data = json.loads(content)
            return data, {}
        except json.JSONDecodeError:
            pass

    # Parse as comma-separated with smart quoting.
    # If we know the function's parameter count, only split on the first
    # (max_positional - 1) commas so that commas inside the last argument
    # (e.g. HTML/CSS content) are preserved intact.
    args = []
    kwargs = {}

    if max_positional is not None and max_positional >= 1:
        raw_parts = content.split(',', max_positional - 1)
    else:
        raw_parts = content.split(',')

    parts = [p.strip().strip('"\'') for p in raw_parts]

    # Check for key=value pairs — only treat as kwarg if key is a valid Python identifier.
    # Strip leading punctuation (e.g. "+tags=DDR5" → key "tags") before checking,
    # but only if the stripped key is a valid identifier and the original isn't a URL
    # query param (which contains "?" before "=").
    for part in parts:
        if '=' in part and not part.startswith('=') and '?' not in part.split('=', 1)[0]:
            key, val = part.split('=', 1)
            key = key.strip().lstrip('+-')  # strip LLM-added prefixes like +tags or -tags
            if key.isidentifier():
                kwargs[key] = val.strip().strip('"\'')
            else:
                args.append(part)
        else:
            args.append(part)

    return args, kwargs

def _strip_fake_result_blocks(text: str) -> str:
    """
    Remove any [✅ skill.func Result: ...] or [❌ skill.func Error: ...] blocks
    that the LLM wrote itself (hallucinations). Real blocks are injected only by
    execute_skill_tags / _execute_tool_calls after actual skill execution.
    """
    lines = text.split('\n')
    out = []
    in_block = False
    for line in lines:
        stripped = line.strip()
        if not in_block and re.match(r'\[(?:✅|❌)\s+\w+\.\w+\s+(?:Result|Error):', stripped):
            in_block = True
            continue
        if in_block:
            # Block ends when a line ends with bare ]
            if stripped == ']' or stripped.endswith('\n]') or (stripped.endswith(']') and not stripped.startswith('[')):
                in_block = False
            continue
        out.append(line)
    return '\n'.join(out)


def _normalize_gemma_skill_tags(text: str) -> str:
    """Convert Gemma 4's native <skill_call:name.func(kwarg='val')> to
    <skill:name.func>val</skill:name.func> so execute_skill_tags can parse it.
    Handles both single and double quoted kwarg values."""
    def _convert(m):
        skill_fn = m.group(1)   # e.g. "weather_api.get_weather"
        args_str = m.group(2)   # e.g. "city='Niš'"
        # Extract values from key="val", key='val', or key=bare_val pairs
        values = []
        for groups in re.findall(r'\w+\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([\w./\-\u0080-\uffff]+))', args_str):
            v = groups[0] or groups[1] or groups[2]
            if v:
                values.append(v)
        content = ",".join(values) if values else args_str.strip()
        parts = skill_fn.split(".", 1)
        skill_name = parts[0]
        func_name = parts[1] if len(parts) > 1 else ""
        tag = f"{skill_name}.{func_name}" if func_name else skill_name
        return f"<skill:{tag}>{content}</skill:{tag}>"
    return re.sub(r'<skill_call:([\w.]+)\(([^)]*)\)>', _convert, text)


def _normalize_plain_skill_calls(text: str, known_skills: set) -> str:
    """Convert bare function-call style output like weather_api.get_weather(Nis) to
    <skill:weather_api.get_weather>Nis</skill:weather_api.get_weather>.
    Only fires when no <skill:...> tags are already present, and only for known skills."""
    if not known_skills or re.search(r'<skill:', text):
        return text  # real tags already exist — don't interfere
    def _convert(m):
        skill_fn = m.group(1)   # e.g. "weather_api.get_weather"
        args_str = m.group(2).strip()
        parts = skill_fn.split(".", 1)
        if len(parts) != 2:
            return m.group(0)
        skill_name = parts[0]
        if skill_name not in known_skills:
            return m.group(0)  # not a real skill — leave as-is
        # Extract kwarg values (city='Niš') or bare positional (Nis)
        kv_groups = re.findall(r'\w+\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([\w./\-\u0080-\uffff]+))', args_str)
        if kv_groups:
            values = []
            for groups in kv_groups:
                v = groups[0] or groups[1] or groups[2]
                if v:
                    values.append(v)
            content = ",".join(values)
        else:
            content = args_str.strip("'\"")
        return f"<skill:{skill_fn}>{content}</skill:{skill_fn}>"
    return re.sub(r'\b([\w]+\.[\w]+)\(([^)]*)\)', _convert, text)


def execute_skill_tags(response_text: str) -> tuple:
    """
    Parse and execute <skill:name.function>args</skill:name.function> tags.
    Returns: (processed_text, execution_log)

    IMPROVEMENTS:
    - Better argument parsing (JSON support)
    - Automatic verification
    - Execution logging
    - Support for multi-line arguments
    """
    # Strip any [✅ ...] / [❌ ...] blocks the LLM hallucinated before we inject real ones
    response_text = _strip_fake_result_blocks(response_text)

    # Rescue unclosed skill tags: if the model's output was truncated before the
    # closing tag (common with large file content), append the expected closing tag
    # so the regex can match it and execute the skill normally.
    _unclosed = re.search(r'<skill:(\w+)\.(\w+)>(?:(?!</skill:).)*$', response_text, flags=re.DOTALL)
    if _unclosed:
        _sn, _fn = _unclosed.group(1), _unclosed.group(2)
        response_text = response_text + f"</skill:{_sn}.{_fn}>"
        print(f"🔧 Rescued unclosed skill tag: <skill:{_sn}.{_fn}>")

    # Pattern matches: <skill:name.function>content</skill:name.function>
    # Supports multi-line content
    pattern = r'<skill:(\w+)\.?(\w*)>(.*?)</skill:\1\.?\2>'

    execution_log = []

    def replace_tag(match):
        skill_name = match.group(1)
        func_name = match.group(2)
        content = match.group(3).strip()

        try:
            # Security: Only allow skills that are actually loaded
            if skill_name not in skills:
                error_msg = f"[⚠️ Skill '{skill_name}' not found. Available: {list(skills.keys())[:5]}... Call /skills/reload if just created.]"
                execution_log.append({"skill": skill_name, "function": func_name, "status": "not_found", "error": error_msg})
                return error_msg

            # --- SMART PARSING FOR CREATE_SKILL ---
            if skill_name == 'create_skill' and func_name == 'create_new_skill':
                # Strategy 1: Standard comma split
                parts = content.split(',', 1)
                filename = ""
                code = ""
                
                if len(parts) == 2:
                    filename = parts[0].strip().strip('"\'')
                    code = parts[1].strip()
                else:
                    # Strategy 2: If no comma, try splitting by the first newline
                    # Usually: "filename.py\nimport..."
                    lines = content.split('\n', 1)
                    if len(lines) == 2 and (lines[0].strip().endswith('.py') or '.' not in lines[0]):
                        filename = lines[0].strip().strip('"\'')
                        code = lines[1].strip()
                        print(f"🛠️ [DEBUG] No comma found, but successfully split by newline for {filename}")
                    else:
                        print(f"🛠️ [DEBUG] create_skill call missing clear separator in content: {content[:100]}...")
                        error_msg = "[❌ create_skill.create_new_skill Error: Cannot parse filename — provide: filename.py,<code here>]"
                        execution_log.append({"skill": skill_name, "function": func_name, "status": "parse_error", "error": error_msg})
                        return error_msg

                print(f"🛠️ [DEBUG] Final parsed filename: '{filename}', code length: {len(code)}")
                
                # 💡 MD STRIPPING: Remove markdown code fences if present
                md_match = re.search(r'```(?:python|py)?\s*(.*?)\s*```', code, re.DOTALL)
                if md_match:
                    code = md_match.group(1).strip()
                    print(f"🛠️ [DEBUG] Extracted MD code block. New length: {len(code)}")
                elif code.startswith('```'):
                    code = code.strip('`').strip()
                    if code.startswith('python'): code = code[6:].strip()
                    print(f"🛠️ [DEBUG] Cleaned broken MD block. New length: {len(code)}")
                        
                if "{content}" in code and len(code) < 50:
                    print(f"⚠️ [DEBUG] Placeholder detected in code.")
                    error_msg = "[❌ Error: The model provided a placeholder instead of the actual skill code. This usually happens when the request is too large or the API times out. Try asking for a simpler version first.]"
                    execution_log.append({"skill": skill_name, "function": func_name, "status": "placeholder_error", "error": error_msg})
                    return error_msg
                    
                args = [filename, code]
                kwargs = {}
            else:
                # Standard argument parsing for all other skills.
                # Look up the function's parameter count so parse_skill_args can
                # limit comma splitting and preserve content that contains commas
                # (e.g. HTML/CSS passed as the last arg to write_file).
                _max_pos = None
                _func = getattr(skills.get(skill_name), func_name, None) if func_name else None
                if callable(_func):
                    try:
                        import inspect as _inspect
                        _sig = _inspect.signature(_func)
                        _max_pos = sum(
                            1 for p in _sig.parameters.values()
                            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                        )
                    except (ValueError, TypeError):
                        pass
                args, kwargs = parse_skill_args(content, max_positional=_max_pos)

                # Resolve positional/kwarg conflicts: if a plain positional arg lands on
                # a param slot already filled by a named kwarg, reassign it to the next
                # uncovered param instead. Prevents "got multiple values for argument X".
                if args and kwargs and callable(_func):
                    try:
                        import inspect as _insp2
                        _pnames = [
                            p.name for p in _insp2.signature(_func).parameters.values()
                            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
                        ]
                        _used = set(kwargs.keys())
                        _new_kw = dict(kwargs)
                        _pi = 0
                        for _a in args:
                            while _pi < len(_pnames) and _pnames[_pi] in _used:
                                _pi += 1
                            if _pi < len(_pnames):
                                _new_kw[_pnames[_pi]] = _a
                                _used.add(_pnames[_pi])
                                _pi += 1
                        args = []
                        kwargs = _new_kw
                    except Exception:
                        pass  # keep original args/kwargs if introspection fails

            # Pre-dispatch lesson check: warn the model if this call has failed before
            if "self_improvement" in skills:
                try:
                    _lesson_warning = skills["self_improvement"].check_lessons(skill_name, func_name)
                    if _lesson_warning:
                        output = _lesson_warning + "\n"
                        execution_log.append({
                            "skill": skill_name, "function": func_name,
                            "status": "lesson_warning", "warning": _lesson_warning
                        })
                        # Inject warning and let the model decide whether to proceed.
                        # We still fall through and execute — the model sees the lesson
                        # in context on the next iteration if it loops.
                        print(f"📚 Lesson fired: {_lesson_warning[:100]}")
                except Exception:
                    pass

            # Execute the skill
            result = call_skill_improved(skill_name, func_name, *args, **kwargs)

            # Format result
            if result["success"]:
                result_str = str(result["result"])

                # Auto-reload when a new skill is created
                if skill_name == 'create_skill' and func_name == 'create_new_skill':
                    new_skill_name = args[0].split('\n')[0].replace('.py', '').strip() if args else None
                    try:
                        reload_skills()
                        if new_skill_name:
                            result_str += (
                                f"\n[✅ Skill '{new_skill_name}' created and loaded. "
                                f"Inspect it at: /app/skills/dynamic/{new_skill_name}.py — "
                                f"move it to /app/skills/core/ when satisfied, then tell me to activate it.]"
                            )
                        else:
                            result_str += "\n[✅ Skills reloaded — new skill is active.]"
                    except Exception as reload_err:
                        result_str += f"\n[⚠️ Auto-reload failed: {reload_err}. Call /skills/reload manually.]"

                # Inject the skill's full DOC so the model has complete function
                # details in context right when it needs them (on-demand), without
                # paying the token cost on every request upfront.
                _full_doc = skill_metadata.get(skill_name, {}).get("doc", "")
                _doc_header = f"[📖 {skill_name}: {_full_doc}]\n" if _full_doc else ""
                output = f"\n[✅ {skill_name}.{func_name} Result:\n{_doc_header}{result_str}]\n"
                execution_log.append({
                    "skill": skill_name,
                    "function": func_name,
                    "status": "success",
                    "result": result_str
                })

            else:
                error_msg = result.get("error", "Unknown error")
                output = f"\n[❌ {skill_name}.{func_name} Error: {error_msg}]\n"
                execution_log.append({
                    "skill": skill_name,
                    "function": func_name,
                    "status": "error",
                    "error": error_msg
                })
                # Auto-record into lessons.jsonl so the agent never repeats this mistake
                if "self_improvement" in skills:
                    try:
                        skills["self_improvement"].record_mistake(
                            skill_name=f"{skill_name}.{func_name}",
                            error_type=_classify_skill_error(error_msg),
                            error_msg=error_msg,
                        )
                    except Exception:
                        pass

            return output

        except Exception as e:
            error_msg = f"[❌ Exception in {skill_name}.{func_name}: {str(e)}]"
            execution_log.append({"skill": skill_name, "function": func_name, "status": "exception", "error": str(e)})
            # Auto-record unhandled exceptions as lessons
            if "self_improvement" in skills:
                try:
                    skills["self_improvement"].record_mistake(
                        skill_name=f"{skill_name}.{func_name}",
                        error_type=type(e).__name__,
                        error_msg=str(e),
                    )
                except Exception:
                    pass
            return error_msg

    # Execute ALL skill tags found in the response, in sequence.
    # Results are injected inline so the agent sees every outcome in one pass.
    # This lets the model output the full website build sequence in a single
    # generation (analyze → scaffold → write_file × 3 → serve) without the
    # overhead of per-step continuation messages that confuse small models.
    # For skills that genuinely need result-chaining (where tag B depends on
    # the runtime output of tag A), the model should still emit one tag at a
    # time — but for write_file / serve there is no such dependency.
    all_matches = list(re.finditer(pattern, response_text, flags=re.DOTALL))

    if not all_matches:
        return response_text, execution_log, ""

    processed_text = re.sub(pattern, replace_tag, response_text, flags=re.DOTALL)
    return processed_text, execution_log, ""


def _execute_tool_calls(tool_calls: list) -> tuple:
    """
    Execute a list of tool_call objects from the LLM response (native function calling).

    Each tool_call has the shape:
        {"id": "call_xxx", "type": "function",
         "function": {"name": "skill__func", "arguments": "{...json...}"}}

    Tool names use double-underscore: 'google_drive__upload_to_folder'
    → skill_name='google_drive', func_name='upload_to_folder'

    Returns:
        tool_result_messages: list of {"role":"tool","tool_call_id":...,"content":...}
        execution_log:        same format as execute_skill_tags uses
    """
    tool_result_messages = []
    execution_log = []

    for tc in tool_calls:
        tool_call_id = tc.get("id", str(uuid.uuid4()))
        func_info    = tc.get("function", {})
        tool_name    = func_info.get("name", "")

        # Parse "skill_name__func_name" → split on first __
        # Also handle LLMs that use ":" or "." as separator (e.g. youtube:search_videos)
        if "__" in tool_name:
            skill_name, func_name = tool_name.split("__", 1)
        elif ":" in tool_name:
            skill_name, func_name = tool_name.split(":", 1)
        elif "." in tool_name:
            skill_name, func_name = tool_name.split(".", 1)
        else:
            skill_name, func_name = tool_name, ""

        # Arguments arrive as a JSON string
        try:
            arguments = json.loads(func_info.get("arguments", "{}"))
        except json.JSONDecodeError:
            arguments = {}

        # Coerce string "true"/"false" → bool, "1"/"2" → int, etc., based on
        # the actual function signature. Some LLMs send "true" instead of true
        # even when the schema says boolean (e.g. Playwright headless param).
        _coerce_func = getattr(skills.get(skill_name), func_name, None) if func_name else None
        if callable(_coerce_func):
            try:
                import inspect as _ci
                _csig = _ci.signature(_coerce_func)
                _CMAP = {
                    bool:  lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("true", "1", "yes"),
                    int:   lambda v: v if isinstance(v, int) else int(v),
                    float: lambda v: v if isinstance(v, float) else float(v),
                }
                for _cpname, _cparam in _csig.parameters.items():
                    if _cpname in arguments and _cparam.annotation is not _ci.Parameter.empty:
                        _cfn = _CMAP.get(_cparam.annotation)
                        if _cfn:
                            try:
                                arguments[_cpname] = _cfn(arguments[_cpname])
                            except (ValueError, TypeError):
                                pass
            except (ValueError, TypeError):
                pass

        # Pre-dispatch lesson check — same guard as the local execute_skill_tags path.
        # Warn the model before the call fires so it can correct arguments or skip.
        _cloud_lesson_prefix = ""
        if "self_improvement" in skills:
            try:
                _cl_warn = skills["self_improvement"].check_lessons(skill_name, func_name)
                if _cl_warn:
                    _cloud_lesson_prefix = _cl_warn + "\n"
                    execution_log.append({
                        "skill": skill_name, "function": func_name,
                        "status": "lesson_warning", "warning": _cl_warn,
                    })
                    print(f"📚 [cloud] Lesson fired: {_cl_warn[:100]}")
            except Exception:
                pass

        result = call_skill_improved(skill_name, func_name, **arguments)

        # Treat skill-level ok:False as an error even if no exception was raised
        inner = result.get("result") if result["success"] else None
        skill_ok = not (isinstance(inner, dict) and inner.get("ok") is False)

        if result["success"] and skill_ok:
            # Strip base64 blobs before echoing back to the LLM — they are hundreds of
            # KB, the model cannot render them as images from a tool result string, and
            # they cause the context to balloon and the model to loop unnecessarily.
            if isinstance(inner, dict) and "base64" in inner:
                inner = {k: v for k, v in inner.items() if k != "base64"}
            content = _cloud_lesson_prefix + str(inner)
            log_entry = {
                "skill":    skill_name,
                "function": func_name,
                "status":   "success",
                "result":   content,
            }
        else:
            if not result["success"]:
                err = result.get("error", "Unknown error")
            else:
                err = inner.get("error", str(inner))
            content = _cloud_lesson_prefix + f"Error in {skill_name}.{func_name}: {err}"
            log_entry = {
                "skill":    skill_name,
                "function": func_name,
                "status":   "error",
                "error":    err,
            }
            # Auto-record lesson so the agent learns from failures
            if "self_improvement" in skills:
                try:
                    skills["self_improvement"].record_mistake(
                        skill_name=f"{skill_name}.{func_name}",
                        error_type=_classify_skill_error(err),
                        error_msg=err,
                    )
                except Exception:
                    pass

        execution_log.append(log_entry)
        tool_result_messages.append({
            "role":         "tool",
            "tool_call_id": tool_call_id,
            "content":      content,
        })

    return tool_result_messages, execution_log


# ============================================================================
# API ENDPOINTS
# ============================================================================

@app.get("/health")
def health():
    return {
        "status": "online",
        "service": "trinity-agent",
        "version": VERSION,
        "sandbox": "enabled" if docker_client else "disabled",
        "skills_loaded": len(skills),
        "skills": list(skills.keys()),
        "chroma_connected": collection is not None
    }

@app.get("/version")
def version():
    """Return the current deployed git commit SHA for update checking."""
    import subprocess
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd="/app",
            stderr=subprocess.DEVNULL,
            timeout=5
        ).decode().strip()
    except Exception:
        sha = "unknown"
    return {"version": VERSION, "sha": sha}

@app.get("/memory/inspect", dependencies=[Depends(verify_api_key)])
def memory_inspect(limit: int = 20):
    """Return the most recently stored ChromaDB entries so you can see what's in long-term memory."""
    if not collection:
        return {"error": "ChromaDB not connected"}
    try:
        result = collection.get(limit=limit, include=["documents", "metadatas"])
        entries = []
        for i, doc in enumerate(result.get("documents") or []):
            meta = (result.get("metadatas") or [])[i] if result.get("metadatas") else {}
            entries.append({"id": (result.get("ids") or [])[i], "type": meta.get("type"), "preview": doc[:200]})
        return {"count": len(entries), "entries": entries}
    except Exception as e:
        return {"error": str(e)}

@app.delete("/memory/clear", dependencies=[Depends(verify_api_key)])
def memory_clear():
    """Wipe the entire ChromaDB trinity_memory collection and recreate it empty."""
    global collection, chroma_client
    if not chroma_client:
        return {"error": "ChromaDB not connected"}
    try:
        chroma_client.delete_collection("trinity_memory")
        collection = chroma_client.get_or_create_collection(name="trinity_memory")
        return {"success": True, "message": "ChromaDB collection cleared and recreated"}
    except Exception as e:
        return {"error": str(e)}

@app.delete("/memory/entry/{entry_id}", dependencies=[Depends(verify_api_key)])
def memory_delete_entry(entry_id: str):
    """Delete a single entry from ChromaDB by its ID."""
    if not collection:
        return {"error": "ChromaDB not connected"}
    try:
        collection.delete(ids=[entry_id])
        return {"success": True, "deleted": entry_id}
    except Exception as e:
        return {"error": str(e)}

@app.get("/skills", dependencies=[Depends(verify_api_key)])
def get_skills():
    """List available skills with detailed metadata."""
    return {
        "skills": skill_metadata,
        "count": len(skills),
        "reload_endpoint": "/skills/reload"
    }

@app.get("/scheduler/list", dependencies=[Depends(verify_api_key)])
def list_scheduled_tasks():
    """Return all scheduled tasks as structured JSON for the command center UI."""
    tasks_file = Path("/app/memory/scheduled_tasks.json")
    if not tasks_file.exists():
        return {"tasks": {}, "count": 0}
    try:
        tasks = json.loads(tasks_file.read_text())
        return {"tasks": tasks, "count": len(tasks)}
    except Exception as e:
        return {"tasks": {}, "count": 0, "error": str(e)}

@app.delete("/scheduler/task/{name}", dependencies=[Depends(verify_api_key)])
def delete_scheduled_task(name: str):
    """Remove a scheduled task by name (for command center UI)."""
    tasks_file = Path("/app/memory/scheduled_tasks.json")
    if not tasks_file.exists():
        raise HTTPException(status_code=404, detail=f"Task '{name}' not found")
    try:
        tasks = json.loads(tasks_file.read_text())
        if name not in tasks:
            raise HTTPException(status_code=404, detail=f"Task '{name}' not found")
        del tasks[name]
        tasks_file.write_text(json.dumps(tasks, indent=2, default=str))
        return {"success": True, "message": f"Task '{name}' removed"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/skills/reload", dependencies=[Depends(verify_api_key)])
def reload_skills_endpoint():
    """Reload all skills — call this after moving a skill to core/ to activate it."""
    try:
        result = reload_skills()
        return {
            "success": True,
            "message": result,
            "skills": list(skills.keys())
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/skill/call", dependencies=[Depends(verify_api_key)])
def call_skill_endpoint(req: SkillRequest):
    """Call a skill function with improved error handling."""
    try:
        args = req.args or []
        kwargs = req.kwargs or {}
        result = call_skill_improved(req.skill, req.function, *args, **kwargs)

        if not result["success"]:
            raise HTTPException(status_code=400, detail=result.get("error"))

        return {"success": True, "result": result["result"]}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/verify", dependencies=[Depends(verify_api_key)])
def verify_endpoint(req: VerificationRequest):
    """Verify an action was completed."""
    result = verify_action(req.action_description, req.verification_skill, req.verification_args)
    return result

@app.delete("/session/clear", dependencies=[Depends(verify_api_key)])
def clear_session(session_id: str):
    """Clear the in-memory conversation history for a session (start fresh)."""
    if session_id in session_store:
        del session_store[session_id]
        return {"success": True, "message": f"Session cleared — next message starts fresh"}
    return {"success": True, "message": "Session not found (already empty)"}

@app.get("/session/info", dependencies=[Depends(verify_api_key)])
def session_info(session_id: str):
    """Show turn count and last active time for a session."""
    entry = session_store.get(session_id)
    if not entry:
        return {"session_id": session_id, "turns": 0, "last_active": None, "active": False}
    return {
        "session_id": session_id,
        "turns": len(entry["messages"]) // 2,
        "messages_in_memory": len(entry["messages"]),
        "last_active": entry["last_active"].isoformat(),
        "active": True
    }

@app.get("/session/debug", dependencies=[Depends(verify_api_key)])
def session_debug(session_id: str):
    """Diagnose session state: what's in RAM vs disk for a given session_id.
    Use this when TC seems to have lost memory — compare the RAM and disk counts.
    """
    normalized = session_id or "default"

    # RAM state
    ram_entry = session_store.get(normalized)
    ram_turns = len(ram_entry["messages"]) // 2 if ram_entry else 0
    ram_preview = [
        {"role": m["role"], "preview": str(m.get("content", ""))[:80]}
        for m in (ram_entry["messages"][:4] if ram_entry else [])
    ]

    # Disk state — scan JSONL for matching entries
    disk_matches = []
    disk_session_ids_seen = set()
    if os.path.exists(MEMORY_FILE):
        try:
            with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        sid = entry.get("session_id") or "default"
                        disk_session_ids_seen.add(sid)
                        if sid == normalized:
                            disk_matches.append({
                                "ts": entry.get("timestamp", ""),
                                "user_preview": str(entry.get("user", ""))[:60],
                                "assistant_preview": str(entry.get("assistant", ""))[:60],
                            })
                    except Exception:
                        continue
        except Exception as e:
            return {"error": str(e)}

    return {
        "session_id_queried": normalized,
        "ram": {"turns": ram_turns, "preview": ram_preview},
        "disk": {
            "turns": len(disk_matches),
            "matches": disk_matches[-5:],  # last 5 turns
            "all_session_ids_in_jsonl": sorted(disk_session_ids_seen),
        },
        "diagnosis": (
            "OK — RAM and disk in sync" if ram_turns == len(disk_matches)
            else f"MISMATCH — RAM has {ram_turns} turns, disk has {len(disk_matches)} turns"
        ),
    }

# ============================================================================
# LLM CALL HELPER
# ============================================================================

def _resize_image_for_llm(data_url: str, max_px: int = 768) -> str:
    """
    Resize and recompress an image data-URL so it's safe for NVIDIA NIM vision API.
    NVIDIA NIM silently drops base64 images over ~180 KB raw, causing hallucinations.
    Always recompresses to JPEG and uses multi-pass quality reduction to guarantee
    the payload stays under 180 KB. Falls back to original if PIL is unavailable.
    """
    try:
        from PIL import Image
        import io as _io

        header, b64 = data_url.split(",", 1)
        img_bytes = base64.b64decode(b64)

        img = Image.open(_io.BytesIO(img_bytes)).convert("RGB")

        # Downscale if larger than max_px on the longest side
        if max(img.size) > max_px:
            img.thumbnail((max_px, max_px), Image.LANCZOS)

        # Multi-pass: reduce quality until raw bytes are under 180 KB
        # (180 KB raw ≈ 240 KB base64 — safely within NVIDIA NIM limits)
        LIMIT = 180_000
        for quality in (75, 60, 45, 30):
            buf = _io.BytesIO()
            img.save(buf, format="JPEG", quality=quality)
            if len(buf.getvalue()) <= LIMIT:
                break

        final_size = len(buf.getvalue())
        print(f"🖼️  Image resized: {img.size} px, {final_size // 1024} KB raw (quality={quality})")
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception as e:
        print(f"⚠️  Image resize failed ({e}), sending original — may exceed NIM limits")
        return data_url  # safe fallback — use original if PIL is unavailable

def _is_claude_model(model_name: str = "trinity-default") -> bool:
    """Return True if the active model is Anthropic Claude (supports prompt caching).

    Checks the model_name string first, then falls back to reading
    litellm_config.yaml when the name is the generic 'trinity-default' alias.
    """
    name_lower = (model_name or "").lower()
    if "claude" in name_lower or "anthropic" in name_lower:
        return True
    if "trinity-default" in name_lower or not model_name:
        try:
            import yaml
            with open("/app/litellm_config.yaml", "r", encoding="utf-8") as _f:
                _cfg = yaml.safe_load(_f)
            configured = _cfg["model_list"][0]["litellm_params"].get("model", "")
            return "claude" in configured.lower() or "anthropic" in configured.lower()
        except Exception:
            return False
    return False


def _call_llm(
    messages: list,
    model_source: str,
    model_name: str,
    cloud_image_content=None,
    ollama_images=None,
    tools: Optional[list] = None,
) -> Dict[str, Any]:
    """
    Make a single LLM call and return a dict with:
        {"content": str|None, "tool_calls": list|None}

    cloud_image_content: replaces the last user message's content for multi-modal cloud calls.
    ollama_images: attaches base64 images to the last user message for Ollama calls.
    tools: OpenAI-style tool definitions for native function calling (cloud only).
    """
    if model_source == "local":
        ollama_base = os.getenv("OLLAMA_API_BASE", "http://ollama:11434")
        ollama_messages = []
        for i, msg in enumerate(messages):
            m = dict(msg)
            # Ollama requires content to be a string, not None
            if m.get("content") is None:
                m["content"] = ""
            # Convert stored OpenAI-format tool_calls (arguments as JSON string)
            # back to Ollama's native format (arguments as dict, no id/type fields)
            if m.get("role") == "assistant" and m.get("tool_calls"):
                _ollama_tcs = []
                for _tc in m["tool_calls"]:
                    _fn = _tc.get("function", {})
                    _args = _fn.get("arguments", {})
                    if isinstance(_args, str):
                        try:
                            _args = json.loads(_args)
                        except Exception:
                            _args = {}
                    _ollama_tcs.append({"function": {"name": _fn.get("name", ""), "arguments": _args}})
                m["tool_calls"] = _ollama_tcs
            if ollama_images and i == len(messages) - 1 and m.get("role") == "user":
                m["images"] = ollama_images
            ollama_messages.append(m)
            
        _tgt_model = model_name
        if not _tgt_model or "trinity-default" in _tgt_model or _tgt_model.startswith("placeholder"):
            _tgt_model = os.getenv("OLLAMA_MODEL", "llama3.2-vision")
        else:
            _tgt_model = _tgt_model.replace("ollama/", "")
            
        _thinking_enabled = os.getenv("OLLAMA_THINK", "false").lower() == "true"
        # When thinking is on, cap token generation to prevent runaway reasoning
        # chains on ambiguous requests. OLLAMA_NUM_PREDICT_THINK defaults to 4000
        # (enough for think block + response). When thinking is off, keep unlimited
        # so skill tags are never cut mid-generation.
        if _thinking_enabled:
            _num_predict = int(os.getenv("OLLAMA_NUM_PREDICT_THINK", "12000"))
        else:
            _num_predict = int(os.getenv("OLLAMA_NUM_PREDICT", "-1"))
        payload = {
            "model": _tgt_model,
            "messages": ollama_messages,
            "stream": False,
            "think": _thinking_enabled,
            "options": {
                "temperature": 1.0,
                "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "12288")),
                "num_predict": _num_predict,
            }
        }
        _native_tools_enabled = os.getenv("OLLAMA_NATIVE_TOOLS", "false").lower() == "true"
        if tools and _native_tools_enabled:
            payload["tools"] = tools
        _ollama_timeout = int(os.getenv("OLLAMA_TIMEOUT", "600"))
        print(f"🔄 Calling Ollama at {ollama_base} (think={_thinking_enabled}, num_predict={_num_predict}, timeout={_ollama_timeout}s)...")
        resp = requests.post(f"{ollama_base}/api/chat", json=payload, timeout=_ollama_timeout)
        resp.raise_for_status()
        _raw_msg = resp.json().get("message", {})
        raw = _raw_msg.get("content") or ""
        # Normalize Ollama tool_calls → OpenAI format expected by _execute_tool_calls
        # Ollama: arguments is already a dict; OpenAI: arguments is a JSON string + needs id
        _ollama_tool_calls = _raw_msg.get("tool_calls") or []
        _normalized_tool_calls = []
        for _tc in _ollama_tool_calls:
            _fn = _tc.get("function", {})
            _args = _fn.get("arguments", {})
            if isinstance(_args, str):
                try:
                    _args = json.loads(_args)
                except Exception:
                    _args = {}
            _normalized_tool_calls.append({
                "id": str(uuid.uuid4()),
                "type": "function",
                "function": {"name": _fn.get("name", ""), "arguments": json.dumps(_args)},
            })
        return {
            "content": raw.strip(),
            "tool_calls": _normalized_tool_calls or None,
        }
    else:
        headers = {"Authorization": f"Bearer {API_KEY}"}
        cloud_messages = list(messages)
        if cloud_image_content is not None:
            # Swap plain text last user message for the multi-modal version
            cloud_messages = cloud_messages[:-1] + [
                {**cloud_messages[-1], "content": cloud_image_content}
            ]
        # Anthropic prompt caching: mark the system message with cache_control so
        # the large static system prompt is cached server-side across iterations of
        # the agent loop.  The system message is identical on every iteration (only
        # the conversation history appended after it grows), so iterations 2+ get a
        # cache hit and skip re-tokenising thousands of tokens.
        # Safe to skip for non-Claude models — they ignore unknown content fields.
        if _is_claude_model(model_name):
            cloud_messages = [
                {
                    **m,
                    "content": [
                        {
                            "type": "text",
                            "text": m["content"],
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                }
                if m.get("role") == "system" and isinstance(m.get("content"), str)
                else m
                for m in cloud_messages
            ]
        payload = {"model": model_name, "messages": cloud_messages, "temperature": 0.2}
        if tools:
            payload["tools"]       = tools
            payload["tool_choice"] = "auto"
        # Retry on 429 (rate limit) with exponential backoff: 10s → 20s → give up
        for _attempt in range(3):
            resp = requests.post(
                f"{LITELLM_BASE}/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=600
            )
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 10 * (2 ** _attempt)))
                if _attempt < 2:
                    print(f"⏳ Rate limited (429), retrying in {retry_after}s (attempt {_attempt + 1}/3)...")
                    time.sleep(retry_after)
                    continue
            if not resp.ok:
                print(f"❌ LiteLLM {resp.status_code}: {resp.text[:800]}")
            resp.raise_for_status()
            break
        message = resp.json()["choices"][0]["message"]
        return {
            "content":    message.get("content"),
            "tool_calls": message.get("tool_calls"),
        }


# ============================================================================
# LOCAL WHISPER TRANSCRIPTION
# ============================================================================

# Lazy-loaded faster-whisper model (downloaded once to memory volume on first use)
_whisper_model = None
_whisper_lock = threading.Lock()

def _get_whisper_model():
    """Load (or return cached) faster-whisper model. Thread-safe."""
    global _whisper_model
    if _whisper_model is None:
        with _whisper_lock:
            if _whisper_model is None:
                from faster_whisper import WhisperModel
                model_size = os.getenv("WHISPER_MODEL", "base")
                model_dir = "/app/memory/whisper_models"
                os.makedirs(model_dir, exist_ok=True)
                print(f"🎤 Loading Whisper model '{model_size}' (downloads once to {model_dir})...")
                _whisper_model = WhisperModel(
                    model_size, device="cpu", compute_type="int8", download_root=model_dir
                )
                print("✅ Whisper model ready")
    return _whisper_model

# Pre-warm Whisper in background — must be after _get_whisper_model is defined
def _prewarm_whisper():
    try:
        _get_whisper_model()
    except Exception as e:
        print(f"⚠️  Whisper pre-warm failed (will retry on first voice message): {e}")

threading.Thread(target=_prewarm_whisper, daemon=True, name="whisper-prewarm").start()

@app.post("/transcribe", dependencies=[Depends(verify_api_key)])
def transcribe(req: TranscribeRequest):
    """
    Transcribe base64-encoded audio using local faster-whisper (no API key needed).

    Body: { "audio": "<base64>", "filename": "voice.ogg" }
    Returns: { "text": "transcribed speech" }

    Model size is controlled by WHISPER_MODEL env var (default: "base").
    Options: tiny, base, small, medium, large-v2, large-v3
    Models are downloaded once to /app/memory/whisper_models/ and reused.
    """
    try:
        audio_bytes = base64.b64decode(req.audio)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 audio data")

    ext = req.filename.rsplit(".", 1)[-1].lower() if "." in req.filename else "ogg"

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        model = _get_whisper_model()
        segments, _ = model.transcribe(tmp_path)
        text = " ".join(seg.text.strip() for seg in segments)
        return {"text": text}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Transcription failed: {str(e)[:200]}")
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

# ============================================================================

@app.post("/chat")
def chat(req: PromptRequest, api_key: str = Depends(verify_api_key)):
    _check_rate_limit(api_key)

    # Load in-memory session history first — needed to decide whether ChromaDB is useful.
    session_id = req.session_id or "default"
    history = get_session_history(session_id)
    print(f"📖 Session {session_id[:12]}... — {len(history) // 2} prior turn(s) in memory")

    # 1. Retrieve long-term memory from ChromaDB.
    # Skip entirely when:
    #   a) Message is too short/conversational (embedding noise)
    #   b) We already have an active session — the session IS the context. Injecting
    #      semantically-similar-but-unrelated old answers causes the model to answer
    #      the PREVIOUS question instead of the current one.
    # Combined score floor — memories below this are not injected.
    # Score = 0.6*relevance + 0.2*frequency + 0.2*recency (see _compute_memory_score).
    # CHROMA_MIN_RELEVANCE is a hard gate on cosine similarity alone (1 - dist).
    # This prevents frequency/recency from rescuing an irrelevant memory — a memory
    # that was accessed 50 times but is about a completely different topic should never
    # be injected. Frequency/recency only affect ranking among memories that already
    # clear the relevance gate.
    _req_m_early = (req.model or "").lower()
    _is_local_chroma = (
        os.getenv("MODEL_SOURCE", "cloud") == "local"
        or any(k in _req_m_early for k in ("ollama", "qwen", "llama", "deepseek", "phi", "gemma"))
    )
    # Local models: strict thresholds to prevent topic drift on small context windows.
    # Cloud models: relaxed thresholds for broader recall.
    if _is_local_chroma:
        CHROMA_MIN_RELEVANCE = 0.75     # very high cosine similarity required
        CHROMA_MIN_SCORE = 0.72         # combined score must also be high
        CHROMA_SKIP_IF_TURNS = 1        # only inject on the very first user turn
        CHROMA_MAX_CHARS = 150          # hard cap on injected text length
    else:
        CHROMA_MIN_RELEVANCE = 0.60     # hard floor: dist must be <= 0.40
        CHROMA_MIN_SCORE = 0.65         # combined score floor
        CHROMA_SKIP_IF_TURNS = 1        # only inject on the very first user turn
        CHROMA_MAX_CHARS = 600          # cap injected text to keep prompt lean
    chroma_context = ""
    active_turns = len([m for m in history if m.get("role") == "user"])
    if collection and len(req.message.strip()) > 40 and active_turns < CHROMA_SKIP_IF_TURNS:
        # Query dedup: skip embed+network call if same query was answered recently
        _qhash = hashlib.md5(req.message.strip().lower().encode()).hexdigest()[:16]
        _cache_key = f"{session_id}:{_qhash}"
        _cached_entry = _chroma_query_cache.get(_cache_key)
        if _cached_entry and time.time() - _cached_entry[1] < CHROMA_CACHE_TTL:
            chroma_context = _cached_entry[0]
            print(f"🧠 ChromaDB: cache hit (hash={_qhash})")
        else:
            try:
                # Fetch 5 candidates so scoring has enough to choose from
                memories = collection.query(query_texts=[req.message], n_results=5,
                                            include=["documents", "metadatas", "distances"])
                if memories['documents'] and memories['documents'][0]:
                    scored = []
                    ids = memories.get('ids', [[]])[0]
                    distances = memories.get('distances', [[]])[0]

                    for i, doc in enumerate(memories['documents'][0]):
                        meta = memories['metadatas'][0][i] if memories['metadatas'] else {}
                        dist = distances[i] if i < len(distances) else 1.0
                        doc_id = ids[i] if i < len(ids) else None
                        # Never inject raw user queries — only AI responses and summaries.
                        # Raw past queries look like commands and cause the model to answer
                        # the OLD question instead of the current one.
                        if meta.get('type') == 'user_query':
                            continue
                        # Hard relevance gate first — frequency cannot rescue a poor match
                        relevance = max(0.0, 1.0 - dist)
                        if relevance < CHROMA_MIN_RELEVANCE:
                            continue
                        hit_count = int(meta.get('hit_count', 0))
                        timestamp = meta.get('timestamp', datetime.now().isoformat())
                        score = _compute_memory_score(dist, hit_count, timestamp)
                        if score >= CHROMA_MIN_SCORE:
                            scored.append((score, doc, doc_id))

                    # Best matches first; inject top 1 only — less noise in the prompt
                    scored.sort(key=lambda x: x[0], reverse=True)
                    ai_responses = [doc for _, doc, _ in scored[:1]]
                    used_ids = [doc_id for _, _, doc_id in scored[:1] if doc_id]

                    # Increment hit_count + refresh last_accessed for retrieved memories
                    if used_ids:
                        now_iso = datetime.now().isoformat()
                        for doc_id in used_ids:
                            try:
                                existing = collection.get(ids=[doc_id], include=["metadatas"])
                                if existing and existing['metadatas']:
                                    cur_meta = existing['metadatas'][0].copy()
                                    cur_meta['hit_count'] = int(cur_meta.get('hit_count', 0)) + 1
                                    cur_meta['last_accessed'] = now_iso
                                    collection.update(ids=[doc_id], metadatas=[cur_meta])
                            except Exception as upd_err:
                                print(f"⚠️  Memory hit_count update failed: {upd_err}")

                    if ai_responses:
                        _safe_responses = [_sanitize_external_content(r, source="chromadb") for r in ai_responses]
                        _joined = " | ".join(_safe_responses)
                        if CHROMA_MAX_CHARS is not None:
                            _joined = _joined[:CHROMA_MAX_CHARS]
                        chroma_context = "Archived context (use only if directly relevant): " + _joined
                        top_score = scored[0][0] if scored else 0
                        print(f"🧠 ChromaDB injecting {len(ai_responses)} memory fragment(s) (top score: {top_score:.2f})")
                    else:
                        print(f"🧠 ChromaDB: no sufficiently relevant past answers (min score {CHROMA_MIN_SCORE})")
            except Exception as e:
                print(f"⚠️  ChromaDB query error: {e}")
            # Cache fresh result — including empty string (known miss avoids re-querying)
            _chroma_query_cache[_cache_key] = (chroma_context, time.time())
            if len(_chroma_query_cache) > CHROMA_CACHE_MAX:
                _evict = min(_chroma_query_cache, key=lambda k: _chroma_query_cache[k][1])
                del _chroma_query_cache[_evict]

    # 2. Build message content (vision support)
    # Resize first — NVIDIA NIM rejects large base64 payloads silently, causing hallucinations
    image_for_llm = _resize_image_for_llm(req.image) if req.image else None

    # Save incoming image to /tmp so file-based skills (image_viewer__extract_text, etc.)
    # can access it by path. Only inject the path note when the user is asking for OCR /
    # text extraction — for regular "describe this" queries the vision LLM is used directly
    # and the path note would confuse the model into trying to call a skill unnecessarily.
    _OCR_KEYWORDS = {
        "extract", "text", "read", "ocr", "characters", "words",
        "written", "printed", "letters", "numbers", "code", "sign", "copy", "transcribe"
    }
    _wants_ocr = bool(set(req.message.lower().split()) & _OCR_KEYWORDS)

    user_message = req.message
    if image_for_llm:
        try:
            _, b64_data = image_for_llm.split(",", 1)
            img_bytes = base64.b64decode(b64_data)
            safe_sid = session_id.replace("/", "_").replace(":", "_")[-20:]
            img_tmp_path = f"/tmp/trinity_img_{safe_sid}.jpg"
            with open(img_tmp_path, "wb") as _f:
                _f.write(img_bytes)
            print(f"🖼️  Image cached: {img_tmp_path} (OCR hint: {_wants_ocr})")
            if _wants_ocr:
                user_message = req.message + f"\n[Image saved at: {img_tmp_path} — use this path with image_viewer__extract_text for OCR]"
        except Exception as _e:
            print(f"⚠️  Could not cache image to disk: {_e}")

    # Save uploaded document to /tmp so document_parser / files skills can access it
    if req.document:
        try:
            _doc_data = req.document
            if "," in _doc_data:
                _doc_data = _doc_data.split(",", 1)[1]
            doc_bytes = base64.b64decode(_doc_data)
            safe_sid = session_id.replace("/", "_").replace(":", "_")[-20:]
            doc_filename = req.document_name or "uploaded_document"
            doc_tmp_path = f"/tmp/trinity_doc_{safe_sid}_{doc_filename}"
            with open(doc_tmp_path, "wb") as _f:
                _f.write(doc_bytes)
            print(f"📄  Document cached: {doc_tmp_path}")
            user_message = user_message + f"\n[Document uploaded: '{doc_filename}' — saved at {doc_tmp_path}. Use document_parser__read or files__cat to read its contents before responding.]"
        except Exception as _e:
            print(f"⚠️  Could not cache document to disk: {_e}")

    # OpenAI/LiteLLM format
    if image_for_llm:
        message_content_cloud = [
            {"type": "text", "text": user_message},
            {"type": "image_url", "image_url": {"url": image_for_llm}}
        ]
    else:
        message_content_cloud = user_message

    # Ollama format (separate - needs base64 images array)
    ollama_images = None
    if image_for_llm:
        raw_b64 = image_for_llm.split(",", 1)[1] if "," in image_for_llm else image_for_llm
        ollama_images = [raw_b64]

    # 3. Build system prompt with skills info
    _req_m = req.model.lower() if req.model else ""
    _is_local_model = (
        os.getenv("MODEL_SOURCE", "cloud") == "local"
        or "ollama" in _req_m or "qwen" in _req_m or "llama" in _req_m
        or "deepseek" in _req_m or "phi" in _req_m or "gemma" in _req_m
    )
    _local_model = _is_local_model

    # Heavy skills: large docs with many functions. Only inject verbose XML format
    # when the current message is actually about that skill domain. Otherwise use
    # compact one-liner so they don't eat context budget on unrelated requests.
    _HEAVY_SKILL_KEYWORDS = {
        "web_builder":       {"website", "html", "css", "site", "build", "scaffold", "design",
                              "web", "page", "ui", "frontend", "webpage", "landing", "preview"},
        "gmail_reader":      {"email", "gmail", "mail", "inbox", "message", "send", "compose"},
        "google_drive":      {"drive", "google", "upload", "download", "sheet", "docs", "spreadsheet"},
        "competitive_intel": {"competitor", "competitive", "intel", "intelligence", "watchlist",
                              "monitor", "monitoring", "rival", "rivals", "market", "surveillance",
                              "track", "tracking", "pricing", "check_site", "run_check"},
        "web":               {"search", "google", "find", "look", "browse", "fetch", "scrape",
                              "lookup", "research", "internet", "online", "url", "link", "news"},
        "self_improvement":  {"improve", "fix", "review", "debug", "refactor", "optimize",
                              "self", "upgrade", "enhance", "audit", "analyze"},
        "document_parser":   {"pdf", "document", "parse", "extract", "docx", "doc", "word",
                              "file", "read", "contract", "report"},
        "telegram_bot":      {"telegram", "bot", "notify", "notification", "alert",
                              "broadcast"},
        "data_science":      {"data", "csv", "chart", "plot", "statistics", "pandas",
                              "graph", "dataset", "visualize", "correlation", "regression"},
        "google_calendar":   {"calendar", "schedule", "event", "meeting", "appointment",
                              "reminder", "invite", "availability", "book"},
        "url_monitor":       {"monitor", "uptime", "ping", "status", "down",
                              "watchlist", "tw_last"},
        "youtube":           {"youtube", "video", "channel", "playlist", "subscribe",
                              "yt"},
        "scheduler":         {"cron", "recurring", "automate", "repeat", "interval",
                              "daily", "weekly"},
        "knowledge_base":    {"knowledge", "recall", "kb", "semantic", "embed"},
        "notes":             {"note", "notes", "note:", "jot", "memo", "lesson", "remember"},
        "google_maps":       {"map", "maps", "location", "directions", "place",
                              "address", "navigate", "distance", "route", "nearby"},
        "database":          {"database", "sql", "query", "db", "table", "postgres",
                              "insert", "select", "delete", "record"},
        "browser_session":   {"browser", "chrome", "cdp", "click", "screenshot",
                              "navigate", "tab", "scrape", "login", "session", "webpage",
                              "fill", "form", "stealth", "automate", "selenium"},
        "autoimprove":       {"improve", "design", "spec", "autoimprove", "overnight",
                              "research", "loop", "proposal", "approach", "plan"},
        # Previously ungated — now compact when message is off-topic
        "git_manager":       {"git", "commit", "push", "pull", "branch", "merge", "repo",
                              "clone", "diff", "stash", "rebase", "checkout"},
        "meeting_notes":     {"meeting", "transcript", "minutes", "attendees", "agenda",
                              "summary", "action items"},
        "email_sender":      {"send", "smtp", "outbox", "compose", "draft", "reply",
                              "forward", "cc", "bcc"},
        "terminal":          {"terminal", "bash", "shell", "command", "cmd", "exec",
                              "ls", "pwd", "mkdir", "rm", "cp", "mv", "chmod",
                              "execute", "python", "script", "calculate", "compute", "eval"},
        "dashboard":         {"dashboard", "status", "health", "uptime", "metrics"},
        "weather":           {"weather", "forecast", "temperature", "rain", "wind",
                              "humidity", "snow", "sunny", "cloudy", "celsius", "fahrenheit"},
        "email_collector":   {"collect", "harvest", "scrape", "emails", "email list",
                              "find emails", "extract emails", "contacts from", "email collector",
                              "directory", "listing"},
    }
    _msg_words = set(req.message.lower().split())

    available_skills = []
    skill_names_list = []
    for name, meta in skill_metadata.items():
        funcs_meta = meta.get("functions", [])
        skill_names_list.append(name)
        # Universal compact format for all models (local and cloud):
        # short_doc keeps upfront token cost low; arg names let any model form a
        # valid call on the first try without guessing. Full docs are injected
        # on-demand inside the [✅ result] block when the skill is actually invoked
        # (execute_skill_tags path). Native tool-calling models (Gemma4, Claude,
        # GPT-4o) also receive full per-function schemas via _build_tools_schema().
        # skills_doc is a discovery index only — args are already in the tools schema
        # sent to the model on every request. Cap at 8 names to keep heavy skills
        # (browser_session, web, notes) from bloating the system prompt.
        _MAX_FUNCS_IN_DOC = 8
        func_names = [f["name"] for f in funcs_meta]
        if len(func_names) > _MAX_FUNCS_IN_DOC:
            func_sigs = ", ".join(func_names[:_MAX_FUNCS_IN_DOC]) + f" +{len(func_names) - _MAX_FUNCS_IN_DOC} more"
        else:
            func_sigs = ", ".join(func_names)
        available_skills.append(
            f"[SKILL: {name}] {meta.get('short_doc', meta.get('doc', 'No doc'))} (functions: {func_sigs})"
        )

    skills_doc = "\n".join(available_skills)
    _skill_index_line = "Skills you have: " + ", ".join(skill_names_list)

    # Detect whether the current message is web-dev related so we can gate the
    # website build workflow block (60+ lines). Injecting it on every request
    # wastes context budget when the user is just chatting or using other skills.
    _is_web_task = bool(_msg_words & _HEAVY_SKILL_KEYWORDS.get("web_builder", set()))

    _local_model_reminder = ""
    if _local_model:
        # Always inject the core skill-usage reminder for local models.
        _local_model_reminder = (
            "\n## SKILL USAGE REMINDER\n\n"
            "You CANNOT read files, save data, browse the web, or perform any external action without a skill tag.\n"
            "Every action needs a tag. Syntax: <skill:SKILLNAME.FUNCTIONNAME>arg1,arg2</skill:SKILLNAME.FUNCTIONNAME>\n"
            "CRITICAL RULE: Never say 'I will do X' — just DO it. Output the skill tag immediately.\n"
            "For multi-step tasks: after each skill result, output the NEXT skill tag without waiting.\n"
            "Only write a text reply when ALL steps are done and all results are in context.\n"
        )
        # Only inject the website build workflow when it's actually relevant.
        if _is_web_task:
            _local_model_reminder += (
                "\n## WEBSITE BUILD — MANDATORY WORKFLOW (no skipping, no files.ls first)\n\n"
                "TEXT-ONLY build (user describes site in words — NO design images):\n"
                "  Step 1: <skill:web_builder.scaffold>PROJECT_NAME,professional</skill:web_builder.scaffold>\n"
                "  Step 2: <skill:web_builder.patch_file>PROJECT_NAME,index.html,{name},REAL_SITE_NAME</skill:web_builder.patch_file>  ← repeat for each content change\n"
                "  Step 3: <skill:web_builder.patch_file>PROJECT_NAME,style.css,--primary:      #1a2e4a,--primary:      #BRAND_HEX</skill:web_builder.patch_file>  ← update brand color\n"
                "  Step 4: <skill:web_builder.serve>PROJECT_NAME</skill:web_builder.serve>\n"
                "\n"
                "IMAGE-BASED build (user has design mockup images in a folder):\n"
                "  Step 1: <skill:web_builder.analyze_design_folder>FOLDER_PATH,en</skill:web_builder.analyze_design_folder>\n"
                "  Step 2: <skill:web_builder.scaffold>PROJECT_NAME,professional</skill:web_builder.scaffold>\n"
                "  Step 3: <skill:web_builder.patch_file>PROJECT_NAME,style.css,--primary:      #1a2e4a,--primary:      #BRIEF_COLOR</skill:web_builder.patch_file>  ← apply brief colors\n"
                "  Step 4: <skill:web_builder.patch_file>PROJECT_NAME,index.html,placeholder_text,real_content</skill:web_builder.patch_file>  ← apply brief content\n"
                "  Step 5: <skill:web_builder.serve>PROJECT_NAME</skill:web_builder.serve>\n"
                "\n"
                "CRITICAL RULES:\n"
                "- ALWAYS use 'professional' template — NEVER 'blank' or 'landing'\n"
                "- Use patch_file() for updates — NEVER write_file() for style.css (generated CSS will be sparse and unstyled)\n"
                "- NEVER stop between steps — keep emitting tags until serve() returns a URL\n"
                "\n## WEBSITE DESIGN QUALITY — CRITICAL RULES\n\n"
                "When applying design brief after analyze_design_folder:\n"
                "- SCROLL UP and find the analyze_design_folder ✅ result.\n"
                "- Use patch_file to replace :root CSS variables with EXACT hex colors from brief.\n"
                "- Use patch_file to replace placeholder text with real content from the brief or user description.\n"
                "- NEVER write placeholder text like 'Your content here' — always use real content.\n"
                "- After serve() completes, report the PREVIEW_URL from the ✅ result — never write 'undefined'.\n"
            )

    # ── Mode-conditional system prompt sections ───────────────────────────────
    if _local_model:
        _skill_usage_section = """\
## HOW TO USE SKILLS

Format: <skill:name.function>args</skill:name.function>
Args: comma-separated. No args: <skill:dashboard.status></skill:dashboard.status>
"""

        _search_example = "<skill:web.search>current weather New York NY</skill:web.search>"

        _rules_section = """\
## RULES

1. Emit skill tag to act. Never describe results before the tag runs.
2. Never write [✅] or [❌] blocks yourself — only the execution engine produces these.
3. Never claim you created/saved/ran something without seeing a real [✅ Result:] block first.
4. On [❌ Error]: acknowledge failure. Never continue as if it worked.
5. Only skills in "YOUR TOOLS" exist. If not listed → say so. EXCEPTION: web.search always exists.
6. New skills saved to /app/skills/dynamic/ are immediately usable.
7. Multi-step tasks: brief plan first, then first skill. Single-step commands → emit skill tag immediately.
8. VERBATIM OUTPUT: When a result contains a URL, token, or exact string to copy — reproduce word-for-word.
9. SCREENSHOTS: Call web.browser_screenshot AT MOST 2× per task (full_page=True captures everything). Report saved_to path."""

        _read_skill_example = "  <skill:files.cat>/app/skills/core/skillname.py</skill:files.cat>"

        _pref_save_instruction = (
            "immediately call <skill:notes.set_preference>key,value,user</skill:notes.set_preference> for each specific preference detected. "
            "Also keep the human-readable summary current: <skill:notes.save>user_preferences,[updated full preferences list]</skill:notes.save>"
        )
    else:
        # Cloud mode: native function calling — no XML tags needed or wanted
        _skill_usage_section = """\
## HOW TO USE SKILLS

Call tools directly via function-calling interface. Tool names: skill_name__function_name.
Example: web__search, google_drive__upload_to_folder.

NEVER write fake [✅] or [❌] blocks — only real tool runs produce these.
CRITICAL: Call tools IN THE SAME RESPONSE. Never write "I will do X" and stop — that produces no action."""

        _search_example = "Call the web__search tool with query='current weather New York NY'"

        _rules_section = """\
## RULES

1. Call tools to act — never describe action as done before tool result arrives.
2. On tool error: tell user what failed. Never continue as if it worked.
3. Only skills in "YOUR TOOLS" exist. If not listed → say so.
4. New skills (create_skill__create_new_skill) saved to /app/skills/dynamic/.
5. Multi-step tasks: brief plan, then call first tool.
6. VERBATIM OUTPUT: When a result contains URL, token, or exact string to copy — reproduce word-for-word.
7. SCREENSHOTS: Call web__browser_screenshot AT MOST 2× (full_page=True captures everything). Report saved_to."""

        _read_skill_example = "  Call files__cat with path='/app/skills/core/skillname.py'"

        _pref_save_instruction = (
            "immediately call notes__set_preference for each specific preference detected (key=preference_name, value=preference_value, source='user'). "
            "Also keep the human-readable summary current by calling notes__save with title='user_preferences' and the updated full preferences list."
        )

    # Load user facts card (always injected, works for every model — no branching)
    _user_facts_card = ""
    try:
        _facts_raw = _fcache.read_text("/app/memory/user_facts.json")
        if _facts_raw:
            _facts = json.loads(_facts_raw)
            _fact_lines = [
                f"  {k}: {v['value'] if isinstance(v, dict) else v}"
                for k, v in _facts.items() if not k.startswith("_")
            ]
            if _fact_lines:
                _updated = _facts.get("_updated", "")[:10]
                _user_facts_card = f"User Facts (last updated {_updated}):\n" + "\n".join(_fact_lines)
    except Exception:
        pass

    # Auto-load user preferences from notes.json and build notes index (cached)
    _pref_content = ""
    _notes_index = ""
    try:
        _notes_raw = _fcache.read_text("/app/memory/notes.json")
        _notes_data = json.loads(_notes_raw) if _notes_raw else {}
        if "user_preferences" in _notes_data:
            _pref_content = _notes_data["user_preferences"].get("content", "")
        if _notes_data:
            _note_titles = list(_notes_data.keys())
            _notes_index = f"Saved notes ({len(_note_titles)} total): " + ", ".join(f'"{t}"' for t in _note_titles[:30])
            if len(_note_titles) > 30:
                _notes_index += f" ... and {len(_note_titles) - 30} more"
    except Exception:
        pass

    # Load identity file (cached)
    _identity_raw = (
        _fcache.read_text("/app/identity.md").strip()
        or _fcache.read_text("/app/../identity.md").strip()
    )
    # Append external capability files so _build_identity's trigger logic can
    # conditionally inject them exactly as when they lived inside identity.md.
    # web_clone and email are keyword-gated (TRINITY_START/END tags).
    # web_design is keyword-gated (TRINITY_START/END) — only injected on web/design requests.
    for _ext_name, _ext_paths in (
        ("web_clone", ("/app/web_clone.md", "/app/../web_clone.md")),
        ("email",     ("/app/email.md",     "/app/../email.md")),
    ):
        _ext_content = _fcache.read_text(_ext_paths[0]).strip() or _fcache.read_text(_ext_paths[1]).strip()
        if _ext_content:
            _identity_raw += (
                f"\n\n<!-- TRINITY_START:{_ext_name} -->\n"
                f"{_ext_content}\n"
                f"<!-- TRINITY_END:{_ext_name} -->"
            )
    _web_design = (
        _fcache.read_text("/app/web_design.md").strip()
        or _fcache.read_text("/app/../web_design.md").strip()
    )
    if _web_design:
        _identity_raw += (
            f"\n\n<!-- TRINITY_START:web_design -->\n"
            f"{_web_design}\n"
            f"<!-- TRINITY_END:web_design -->"
        )
    _identity_content = _build_identity(_identity_raw, _msg_words)

    # Load lessons: deduplicated by skill+error_type (most recent fix wins), sorted newest first (cached)
    _lessons_lines = []
    try:
        _raw_lessons_text = _fcache.read_text("/app/memory/lessons.jsonl")
        _raw_lessons = [l.strip() for l in _raw_lessons_text.splitlines() if l.strip()]
        _seen_keys: dict = {}
        for _raw in reversed(_raw_lessons):  # reverse so first-seen = most recent
            try:
                _l = json.loads(_raw)
                if _l.get("fix_applied"):
                    # For user corrections, use a content hash so different mistakes get separate slots
                    if _l.get("type") == "correction":
                        import hashlib as _hl
                        _key = "correction:" + _hl.md5(_l.get("bad_reply_preview", "")[:100].encode()).hexdigest()[:12]
                    else:
                        _key = f"{_l.get('skill','?')}:{_l.get('error_type','?')}"
                    if _key not in _seen_keys:
                        _seen_keys[_key] = _l
            except Exception:
                pass
        # Sort by timestamp descending, cap at 10 for local (tight ctx), 20 for cloud
        _lessons_cap = 10 if _is_local_model else 20
        _sorted_all = sorted(_seen_keys.values(), key=lambda x: x.get("timestamp", ""), reverse=True)
        # Reorder: lessons for skills matching the current message's keywords come first.
        # Same cap, better signal — skills the user is about to use get their warnings up front.
        _lessons_match, _lessons_rest = [], []
        for _lx in _sorted_all:
            _lx_skill = _lx.get("skill", "").split(".")[0]
            _lx_kw = _HEAVY_SKILL_KEYWORDS.get(_lx_skill, set())
            if _lx_kw and (_msg_words & _lx_kw):
                _lessons_match.append(_lx)
            else:
                _lessons_rest.append(_lx)
        _deduped = (_lessons_match + _lessons_rest)[:_lessons_cap]
        for _l in _deduped:
            _safe_fix = _sanitize_external_content(_l["fix_applied"][:150], source="lessons.jsonl")
            if _l.get("type") == "correction" and _l.get("bad_reply_preview"):
                _safe_bad = _sanitize_external_content(_l["bad_reply_preview"][:100], source="lessons.jsonl")
                _lessons_lines.append(
                    f"- [user_correction] I said: \"{_safe_bad}\" → {_safe_fix}"
                )
            else:
                _lessons_lines.append(
                    f"- [{_l.get('skill', '?')}] {_l.get('error_type', '?')}: fix → {_safe_fix}"
                )
    except Exception:
        pass
    _lessons_block = "\n".join(_lessons_lines) if _lessons_lines else "None yet."

    # Load daily journal and user model for system prompt injection.
    # The journal and user facts are static within a session — build once on
    # the first turn and return the cached string on every subsequent turn.
    # The "parked idea" block is dynamic per-message and appended separately.
    _is_new_session = len(history) == 0
    from datetime import date as _date, timedelta as _timedelta
    _today_str = _date.today().isoformat()
    if _is_new_session or session_id not in _session_daily_memory:
        _daily_memory_block = ""
        try:
            _journal_days = 3 if _is_local_model else 7
            _cutoff_str = (_date.today() - _timedelta(days=_journal_days)).isoformat()
            _journal_entries = {}
            _journal_raw = _fcache.read_text("/app/memory/daily_journal.jsonl")
            for _jline in _journal_raw.splitlines():
                _jline = _jline.strip()
                if _jline:
                    try:
                        _je = json.loads(_jline)
                        if _je.get("date", "") >= _cutoff_str:
                            _journal_entries[_je["date"]] = _je
                    except Exception:
                        pass
            _journal_lines = []
            for _je in sorted(_journal_entries.values(), key=lambda x: x["date"], reverse=True):
                _label = "TODAY" if _je["date"] == _today_str else _je["date"]
                _journal_lines.append(f"[{_label}] {_je.get('summary', '')}")
                if _je.get("learned"):
                    _learned_lines = [l for l in _je["learned"].splitlines() if l.strip()]
                    _learned_cap   = 3
                    _learned_shown = _learned_lines[:_learned_cap]
                    _learned_rest  = len(_learned_lines) - _learned_cap
                    _learned_str   = " | ".join(_learned_shown)
                    if _learned_rest > 0:
                        _learned_str += f" (+{_learned_rest} more)"
                    _journal_lines.append(f"  Learned: {_learned_str}")
                if _je.get("user_insights"):
                    _journal_lines.append(f"  User: {_je['user_insights']}")
                if _je.get("next_steps"):
                    _ns = _je["next_steps"]
                    # Skip next_steps that are purely about scheduled/recurring tasks —
                    # they run automatically and don't need to occupy the model's attention.
                    _sched_kw = ("schedul", "recurring", "cron", "every day", "every hour",
                                 "nightly", "daily review", "autoimprove", "run loop")
                    if not any(k in _ns.lower() for k in _sched_kw):
                        _journal_lines.append(f"  Next steps promised: {_ns}")
            _user_model_block = ""
            try:
                _notes_skill = skills.get("notes")
                if _notes_skill and hasattr(_notes_skill, "get_context_for_prompt"):
                    # Inject user model only at session start — preferences don't change
                    # mid-session and reloading them every turn wastes context budget.
                    if _is_new_session:
                        _user_model_block = _notes_skill.get_context_for_prompt()
            except Exception:
                pass
            _dm_parts = []
            if _user_facts_card:
                _dm_parts.append(_user_facts_card)
            if _journal_lines:
                _dm_parts.append(f"Recent Journal (last {_journal_days} days):\n" + "\n".join(_journal_lines))
            if _user_model_block:
                _dm_parts.append("User Profile:\n" + _user_model_block)
            _daily_memory_block = "\n\n".join(_dm_parts) if _dm_parts else "No journal entries yet."
            # Cache for subsequent turns in this session
            _session_daily_memory[session_id] = _daily_memory_block
        except Exception:
            _daily_memory_block = "No journal entries yet."
            _session_daily_memory[session_id] = _daily_memory_block
    else:
        _daily_memory_block = _session_daily_memory[session_id]

    # Surface the single most-recent open improvement idea when the message is
    # improvement-related. One line only — always dynamic, never cached.
    _IDEA_KEYWORDS = {"improve", "idea", "suggestion", "better", "enhance",
                      "optimize", "proposal", "upgrade", "autoimprove"}
    if _msg_words & _IDEA_KEYWORDS:
        try:
            _ai_skill = skills.get("autoimprove")
            if _ai_skill and hasattr(_ai_skill, "get_latest_idea"):
                _latest_idea = _ai_skill.get_latest_idea()
                if _latest_idea:
                    _daily_memory_block += f"\n\n💡 Most recent parked idea: {_latest_idea}"
        except Exception:
            pass

    # On new sessions (first message): run daily_review in background — never blocks the response
    _skill_health_section = ""
    if len(history) == 0:
        def _bg_daily_review():
            try:
                from skills.core import self_improvement as _si
                _dr_result = _si.daily_review()
                _safe_types = ("bare_except", "missing_timeout")
                _dyn_dir = Path("/app/skills/dynamic")
                if _dyn_dir.exists():
                    for _dp in sorted(_dyn_dir.glob("*.py")):
                        if _dp.name.startswith("_"):
                            continue
                        _sn = _dp.stem
                        try:
                            _analysis = _si.analyze_skill_code(_sn)
                            _fixed_types = set()
                            for _issue in _analysis.get("issues", []):
                                if _issue["type"] in _safe_types and _issue["type"] not in _fixed_types:
                                    _si.fix(_sn, _issue["type"], "all")
                                    _fixed_types.add(_issue["type"])
                        except Exception:
                            pass
                print(f"🏥 Background daily_review complete: {_dr_result[:80]}")
            except Exception as _dr_err:
                print(f"⚠️  daily_review failed: {_dr_err}")
        threading.Thread(target=_bg_daily_review, daemon=True, name="daily-review").start()

        # On new sessions: check if recurring failures warrant autoimprove — park result, never blocks
        def _bg_should_improve():
            try:
                from skills.core import self_improvement as _si
                result = _si.should_self_improve(threshold=3, window_days=7)
                if result.get("improve"):
                    _ai = skills.get("autoimprove")
                    if _ai and hasattr(_ai, "park_idea"):
                        top = result["patterns"][0]
                        idea = (
                            f"[auto] should_self_improve triggered: '{top['error_type']}' "
                            f"occurred {top['count']}x in last 7 days — "
                            f"run autoimprove.run_loop('{result['suggested_loop']}')"
                        )
                        _ai.park_idea(idea, source="should_self_improve")
                    print(f"🔁 should_self_improve: {result['reason'][:120]}")
            except Exception as _si_err:
                print(f"⚠️  should_self_improve failed: {_si_err}")
        threading.Thread(target=_bg_should_improve, daemon=True, name="should-improve").start()

    # If the user's message contains a URL, prepend a hard directive at the very top
    # of the system prompt so even a small local model sees it before anything else.
    import re as _re
    _url_in_message = _re.search(r'https?://\S+', req.message)
    _url_directive = ""
    if _url_in_message:
        _found_url = _url_in_message.group(0).rstrip('.,;)')
        if _is_local_model:
            _fetch_instruction = f"Output this tag immediately as your first action: <skill:web.fetch>{_found_url}</skill:web.fetch>"
        else:
            _fetch_instruction = f"You MUST call web__fetch with url='{_found_url}' as your FIRST tool call."
        _url_directive = (
            f"⚠️ FIRST ACTION — MANDATORY: The user's message contains a URL: {_found_url}\n"
            f"{_fetch_instruction}\n"
            f"Do NOT call web__search. Do NOT respond with news, prices, or any other topic.\n"
            f"Fetch the URL, read the result, then answer the user's question about it.\n\n"
        )

    # Detect "write a note:" / "this is your lesson" / correction patterns and inject
    # a top-of-prompt directive so the model sees the instruction BEFORE identity.md,
    # bypassing any context-window truncation that may hide the Quick Reference table.
    _msg_lower = req.message.lower()
    _note_directive = ""
    _NOTE_SAVE_PATTERNS = (
        "write a note:", "save a note:", "add to notes:", "remember this:",
        "don't forget:", "note this:", "note:", "jot this:",
    )
    _LESSON_PATTERNS = (
        "this is your lesson", "this should be your lesson",
        "don't do this again", "remember for next time",
        "remember this for next time", "this is a lesson",
        "save this as a lesson", "write this as a lesson",
    )
    _is_note_command   = any(_msg_lower.startswith(p) or (p in _msg_lower and len(req.message) < 600) for p in _NOTE_SAVE_PATTERNS)
    _is_lesson_command = any(p in _msg_lower for p in _LESSON_PATTERNS)
    if (_is_note_command or _is_lesson_command) and _is_local_model:
        if _is_lesson_command:
            # Lesson pattern: save note + update user model
            _note_directive = (
                "⚠️ IMMEDIATE ACTION — NO PLAN NEEDED:\n"
                "The user is correcting your behavior. This is a lesson to save.\n"
                "Step 1 — save a note (derive a short title, use today's date):\n"
                f"<skill:notes.save>lesson-{_today_str},{req.message[:300]}</skill:notes.save>\n"
                "Step 2 — after the save confirms, update your user model:\n"
                "<skill:notes.update_user_model>USER LESSON: [one-sentence summary of correction]</skill:notes.update_user_model>\n"
                "Do NOT write a plan. Do NOT ask for clarification. Emit the skill tags now.\n\n"
            )
        else:
            # Plain note-save pattern
            _note_directive = (
                "⚠️ IMMEDIATE ACTION — NO PLAN NEEDED:\n"
                "The user wants to save a note. Derive a short title from the content.\n"
                "Output this skill tag immediately as your FIRST and ONLY action:\n"
                f"<skill:notes.save>SHORT_TITLE_HERE,{req.message}</skill:notes.save>\n"
                "Replace SHORT_TITLE_HERE with a 2-5 word title derived from the note content.\n"
                "Do NOT write a plan. Do NOT ask for clarification. Emit the tag now.\n\n"
            )

    # Build system prompt from template (loaded at startup from prompts/system.md)
    _identity_prefix    = (_identity_content + "\n\n") if _identity_content else ""
    _notes_index_str    = _notes_index if _notes_index else "no notes saved yet"
    _pref_content_str   = _pref_content if _pref_content else "None saved yet."
    _chroma_context_str = chroma_context if chroma_context else "None yet."

    # Load scheduled tasks for system prompt injection
    _scheduled_tasks_block = ""
    try:
        _sched_raw = _fcache.read_text("/app/memory/scheduled_tasks.json")
        if _sched_raw:
            _sched_data = json.loads(_sched_raw)
            if _sched_data:
                _sched_lines = [f"Active scheduled tasks ({len(_sched_data)}):"]
                for _sn, _st in _sched_data.items():
                    _kind = "recurring" if _st.get("type") == "recurring" else "once"
                    _ivl = f" every {_st['interval_seconds']}s" if _st.get("interval_seconds") else ""
                    _sched_lines.append(
                        f'  - "{_sn}" [{_kind}{_ivl}] next: {_st.get("next_run","?")[:16]}'
                        f' | prompt: "{str(_st.get("prompt",""))[:80]}"'
                    )
                _scheduled_tasks_block = "\n".join(_sched_lines)
    except Exception:
        pass

    # Prepend note/lesson directive to url_directive so both share the top-of-prompt slot
    _url_directive = _note_directive + _url_directive
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.safe_substitute(
        _url_directive       = _url_directive,
        _identity_prefix     = _identity_prefix,
        _today_str           = _today_str,
        _notes_index_str     = _notes_index_str,
        _skill_index_line    = _skill_index_line,
        skills_doc           = skills_doc,
        _skill_usage_section = _skill_usage_section,
        _search_example      = _search_example,
        _rules_section       = _rules_section,
        _read_skill_example  = _read_skill_example,
        _pref_content_str    = _pref_content_str,
        _pref_save_instruction = _pref_save_instruction,
        _lessons_block       = _lessons_block,
        _skill_health_section = _skill_health_section,
        _daily_memory_block  = _daily_memory_block,
        _chroma_context_str  = _chroma_context_str,
        _local_model_reminder = _local_model_reminder,
        _scheduled_tasks_block = _scheduled_tasks_block,
    )

    # ── Token budget accounting (char/4 ≈ tokens, no tiktoken needed) ────────
    _tk_identity  = _est_tokens(_identity_content)
    _tk_skills    = _est_tokens(skills_doc)
    _tk_usage     = _est_tokens(_skill_usage_section)
    _tk_rules     = _est_tokens(_rules_section)
    _tk_lessons   = _est_tokens(_lessons_block)
    _tk_daily     = _est_tokens(_daily_memory_block)
    _tk_chroma    = _est_tokens(_chroma_context_str)
    _tk_hint      = _est_tokens(_local_model_reminder)
    _tk_sys       = _est_tokens(system_prompt)
    _tk_hist      = sum(_est_tokens(m.get("content") or "") for m in history)
    _tk_user      = _est_tokens(user_message)
    print(
        f"📊 Token est | sys={_tk_sys} "
        f"(identity={_tk_identity} skills={_tk_skills} usage={_tk_usage} "
        f"rules={_tk_rules} lessons={_tk_lessons} daily={_tk_daily} "
        f"chroma={_tk_chroma} hint={_tk_hint}) "
        f"| hist={_tk_hist} user={_tk_user} "
        f"| TOTAL={_tk_sys + _tk_hist + _tk_user}"
    )
    _token_est = {
        "total": _tk_sys + _tk_hist + _tk_user,
        "system": _tk_sys,
        "history": _tk_hist,
        "user": _tk_user,
        "breakdown": {
            "identity": _tk_identity,
            "skills": _tk_skills,
            "usage": _tk_usage,
            "rules": _tk_rules,
            "lessons": _tk_lessons,
            "daily": _tk_daily,
            "chroma": _tk_chroma,
            "hint": _tk_hint,
        },
    }
    # ─────────────────────────────────────────────────────────────────────────

    try:
        model_source = "local" if _is_local_model else "cloud"

        # 4. Build the initial message list (plain-text user content always stored here;
        #    multi-modal content is injected per-call inside _call_llm via cloud_image_content)
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        # If user is correcting a wrong answer, force the model to search before answering.
        # IMPORTANT: For local/Ollama models, Gemma4's chat template ignores system-role
        # messages that appear mid-conversation (only position-0 system is rendered).
        # So we embed the correction directly in the user turn instead of appending a
        # dead system message. For cloud models, tool-calling handles this differently.
        if _detect_correction(req.message):
            if _is_local_model:
                _correction_prefix = (
                    "[CORRECTION REQUIRED] Your previous answer was flagged as WRONG. "
                    "Do NOT repeat it.\n"
                    "Your VERY FIRST output must be a web search tag — replace TOPIC with "
                    "the actual subject from the conversation history:\n"
                    "<skill:web.search>TOPIC</skill:web.search>\n"
                    "After the search result comes back, verify the facts and give the correct answer.\n\n"
                )
                # Overwrite the last user turn — Gemma4 WILL read this as part of its prompt
                messages[-1] = {"role": "user", "content": _correction_prefix + user_message}
            else:
                messages.append({
                    "role": "system",
                    "content": (
                        "⚠️ CORRECTION: Your previous response was flagged as wrong by the user. "
                        "DO NOT repeat it. You MUST: "
                        "1) Call web__search with a query about the topic, or autoimprove__research. "
                        "2) Check <LEARNED_LESSONS> for what you got wrong. "
                        "3) Only respond after you have verified the new answer with a live source. "
                        "Acknowledge the mistake in one sentence, then immediately search."
                    ),
                })

        all_execution_logs = []
        ai_reply = ""
        # Holds the design brief JSON after analyze_design_folder runs so it can
        # be injected into the very next continuation message (keeping it fresh
        # in context when the model writes HTML/CSS rather than hoping it scrolls back).
        _pending_design_brief = ""

        # 5. Agentic loop: Reason → Act → Observe → Reason …
        # Cloud mode uses native function calling (structured tool_calls).
        # Local/Ollama mode falls back to the legacy XML skill-tag system.
        #
        # Skill preview: start with a message-filtered schema to save tokens.
        # After each iteration, any newly-called skills are added so the LLM
        # can always call a skill it has already used (multi-step safety net).
        _used_skills_this_req: set = set()
        tools = _build_tools_schema_for_request(req.message)

        _continuation_pushes = 0        # cloud: how many "stop describing, act!" pushes sent so far
        _local_continuation_pushes = 0  # local: same, for Ollama/tag path
        _consecutive_error_iters = 0    # how many consecutive iterations had only failed skill calls
        _skill_failure_counts: dict = {}  # (skill, func) -> cumulative failure count this request

        for iteration in range(1, MAX_ITERATIONS + 1):
            print(f"🔁 Agent iteration {iteration}/{MAX_ITERATIONS}")
            _stream_emit({"type": "iteration", "n": iteration, "max": MAX_ITERATIONS})

            is_first = (iteration == 1)
            # Always use the requested model — no separate vision model needed.
            # Configure trinity-default in litellm_config.yaml to be vision-capable;
            # switching providers (Claude, GPT-4o, etc.) then requires no code changes.
            effective_model = req.model

            # Don't send tool definitions on the vision call: combining tools + a
            # base64 image in one request makes the JSON payload enormous and causes
            # NVIDIA NIM to return 502 or silently drop the image (→ hallucinations).
            # The vision model only needs to see the image; tool calls happen on
            # later iterations using the regular model.
            is_vision_call = is_first and bool(req.image)
            llm_response = _call_llm(
                messages,
                model_source,
                effective_model,
                cloud_image_content=message_content_cloud if is_vision_call else None,
                ollama_images=ollama_images if is_first else None,
                tools=tools if (tools and not is_vision_call) else None,
            )

            if model_source == "local":
                # ── Local/Ollama path ─────────────────────────────────────────
                # Gemma 4 and other agentic Ollama models return structured tool_calls
                # when tools are provided. Use them directly — no text parsing needed.
                if llm_response.get("tool_calls"):
                    tool_calls = llm_response["tool_calls"]
                    ai_reply   = llm_response.get("content") or ""
                    print(f"⚙️  Iteration {iteration}: {len(tool_calls)} Ollama tool call(s), looping…")
                    tool_result_messages, execution_log = _execute_tool_calls(tool_calls)
                    all_execution_logs.extend(execution_log)
                    for _l in execution_log:
                        _stream_emit({"type": "skill", "skill": _l.get("skill"), "function": _l.get("function"), "status": _l.get("status")})
                    messages.append({
                        "role": "assistant",
                        "content": ai_reply or None,
                        "tool_calls": tool_calls,
                    })
                    messages.extend(tool_result_messages)
                    ai_reply = "\n".join(
                        f"[✅ {l['skill']}.{l.get('function','')} Result: {l.get('result','')}]"
                        if l["status"] == "success"
                        else f"[❌ {l['skill']}.{l.get('function','')} Error: {l.get('error','')}]"
                        for l in execution_log
                    )
                    # Expand schema to include any newly-called skills
                    for _tc in tool_calls:
                        _tc_skill = (_tc.get("function", {}).get("name") or "").split("__")[0]
                        if _tc_skill:
                            _used_skills_this_req.add(_tc_skill)
                    tools = _build_tools_schema_for_request(req.message, used_skills=_used_skills_this_req)
                    continue

                # ── Fallback: XML tag-based path (non-agentic Ollama models) ──
                # Guard against None content (common when the model just processed
                # tool results and returns an empty turn instead of a text summary).
                ai_reply = llm_response.get("content") or ""
                # If the LLM returned nothing but we already have tool results from a
                # previous iteration, surface them directly — no need to re-prompt.
                if not ai_reply.strip() and all_execution_logs:
                    parts = []
                    for log in all_execution_logs:
                        skill_label = f"{log['skill']}.{log.get('function', '')}"
                        if log.get("status") == "success":
                            res = str(log.get("result", "")).strip()
                            short = (res[:600] + "…") if len(res) > 600 else res
                            parts.append(short)
                        else:
                            parts.append(f"❌ {skill_label} — {log.get('error', 'failed')}")
                    ai_reply = "\n\n".join(parts)
                    break

                # Strip <think>...</think> blocks BEFORE tag execution.
                # But first: rescue any skill tags that Qwen placed inside <think>.
                # Thinking models (Qwen3.5, DeepSeek-R1) often emit skill tags inside
                # <think> as part of their reasoning — those would silently vanish.
                # We extract them and prepend to the response body so they still execute.
                # Normalize Gemma 4's native <skill_call:name.func(args)> → <skill:name.func>args</skill:...>
                ai_reply = _normalize_gemma_skill_tags(ai_reply)

                _rescued_tags = []
                for _think_match in re.finditer(r"<think>.*?</think>", ai_reply, flags=re.DOTALL):
                    _rescued_tags.extend(
                        re.findall(r"<skill:\w+\.\w+>.*?</skill:\w+\.\w+>", _think_match.group(), flags=re.DOTALL)
                    )
                ai_reply = re.sub(
                    r"<think>.*?</think>", "", ai_reply, flags=re.DOTALL
                ).strip()
                # Only prepend rescued tags if the body has none (avoid double-execution).
                if _rescued_tags and not re.search(r"<skill:\w+\.\w+>", ai_reply):
                    ai_reply = "\n".join(_rescued_tags) + ("\n" + ai_reply if ai_reply else "")
                # Last resort: if model output bare calls like weather_api.get_weather(London)
                # with no XML tags at all, convert them now (only touches known skills).
                ai_reply = _normalize_plain_skill_calls(ai_reply, set(skills.keys()))

                executed_reply, execution_log, pending_summary = execute_skill_tags(
                    ai_reply
                )
                all_execution_logs.extend(execution_log)
                for _l in execution_log:
                    _stream_emit({"type": "skill", "skill": _l.get("skill"), "function": _l.get("function"), "status": _l.get("status")})

                # Capture design brief immediately after analyze_design_folder so
                # it can be injected into the very next continuation message.
                for _log in execution_log:
                    if (
                        _log.get("skill") == "web_builder"
                        and _log.get("function") == "analyze_design_folder"
                        and _log.get("status") == "success"
                    ):
                        _raw_brief = str(_log.get("result", ""))
                        # Only use the result if it is NOT an error response.
                        # An error JSON has an "error" key — reject it.
                        try:
                            _brief_check = json.loads(_raw_brief)
                            _is_error_result = "error" in _brief_check
                        except Exception:
                            _is_error_result = False
                        if not _is_error_result:
                            _pending_design_brief = _raw_brief[:1800]
                        # If it IS an error, leave _pending_design_brief empty so the
                        # retry instruction fires instead of injecting a fake brief.

                if not execution_log:
                    # If the local model wrote a plan instead of a skill tag, push it to act.
                    # Fires on ANY iteration — including the first — so "I will check X" on
                    # iteration 1 (all_execution_logs still empty) still gets a push.
                    _LOCAL_PLAN_SIGNALS = (
                        # English
                        "i will", "i'll", "let me", "i need to", "i'm going to",
                        "i should", "next i", "will call", "now i'll",
                        "i'll now", "i'll call", "then i'll", "step 2", "step 3",
                        "next step", "going to call", "i am going to",
                        "first i", "first, i", "to do this",
                        # Serbian (Latin)
                        "provjerit", "pogledat", "učinit", "hajde da",
                        "trebam", "moram", "sada ću", "sad ću", "ću da",
                        "prvo ću", "zatim ću", "korak", "proveri", "pogledam",
                        "iskoristit", "koristit", "pozovem", "pozvaću",
                    )
                    _looks_like_local_plan = (
                        _local_continuation_pushes < 3
                        and len(ai_reply.strip()) > 30
                        and any(sig in ai_reply.lower() for sig in _LOCAL_PLAN_SIGNALS)
                    )
                    if _looks_like_local_plan:
                        _local_continuation_pushes += 1
                        print(f"⚠️  Iteration {iteration}: local model described plan without tag — push #{_local_continuation_pushes}")
                        messages.append({"role": "assistant", "content": ai_reply})
                        messages.append({
                            "role": "user",
                            "content": (
                                "You described a plan but did not execute it. "
                                "Output the skill tag NOW — no introduction, no explanation.\n"
                                "Example: <skill:notes.list_notes></skill:notes.list_notes>\n"
                                "Example: <skill:web.search>current gold price</skill:web.search>\n"
                                "Write ONLY the tag. Nothing else."
                            ),
                        })
                        continue

                    print(f"✅ Agent loop complete after {iteration} iteration(s)")
                    ai_reply = executed_reply
                    # Fallback: Qwen and other local models sometimes produce an empty
                    # final turn (only <think> reasoning, no user-facing text). Rather
                    # than returning a blank response, synthesise a summary from the
                    # execution log so the user always gets a meaningful reply.
                    if not ai_reply.strip() and all_execution_logs:
                        # Check if we have successful web search results — if so, synthesize
                        # from the snippets rather than dumping raw execution log headers.
                        _search_results = [
                            l for l in all_execution_logs
                            if l.get("skill") == "web" and l.get("function") == "search"
                            and l.get("status") == "success"
                        ]
                        if _search_results:
                            # Use only the first search result (the most relevant one)
                            _best = _search_results[0]
                            _raw = str(_best.get("result", "")).strip()
                            # Strip the engine header line (e.g. "🔍 Bing: ...") and present clean content
                            _lines = _raw.splitlines()
                            _content_lines = [ln for ln in _lines if not ln.startswith("🔍")]
                            _content = "\n".join(_content_lines).strip()
                            _short = (_content[:1200] + "…") if len(_content) > 1200 else _content
                            ai_reply = _short if _short else _raw[:600]
                        else:
                            parts = []
                            for log in all_execution_logs:
                                skill_label = f"{log['skill']}.{log.get('function', '')}"
                                if log.get("status") == "success":
                                    res = str(log.get("result", "")).strip()
                                    short = (res[:300] + "…") if len(res) > 300 else res
                                    parts.append(f"✅ {skill_label} — {short}")
                                else:
                                    parts.append(f"❌ {skill_label} — {log.get('error', 'failed')}")
                            ai_reply = "All steps completed:\n\n" + "\n".join(parts)
                    break

                print(f"⚙️  Iteration {iteration}: {len(execution_log)} skill(s) executed, looping…")
                # Dead-end detection: track consecutive all-error iterations
                _all_failed_this_iter = bool(execution_log) and all(
                    l.get("status") == "error" for l in execution_log
                )
                if _all_failed_this_iter:
                    _consecutive_error_iters += 1
                else:
                    _consecutive_error_iters = 0
                for _l in execution_log:
                    _sfc_key = (_l.get("skill", ""), _l.get("function", ""))
                    if _l.get("status") == "error":
                        _skill_failure_counts[_sfc_key] = _skill_failure_counts.get(_sfc_key, 0) + 1
                    else:
                        _skill_failure_counts.pop(_sfc_key, None)
                messages.append({"role": "assistant", "content": executed_reply})
                # Compute remaining website steps from the full execution log.
                _wf_written = set()
                for _l in all_execution_logs:
                    if (
                        _l.get("skill") == "web_builder"
                        and _l.get("function") == "write_file"
                        and _l.get("status") == "success"
                    ):
                        for _fn in ["index.html", "style.css", "script.js"]:
                            if _fn in str(_l.get("result", "")):
                                _wf_written.add(_fn)
                _wf_served = any(
                    _l.get("skill") == "web_builder"
                    and _l.get("function") == "serve"
                    and _l.get("status") == "success"
                    for _l in all_execution_logs
                )
                _wf_has_scaffold = any(
                    _l.get("skill") == "web_builder"
                    and _l.get("function") == "scaffold"
                    for _l in all_execution_logs
                )

                if _wf_has_scaffold:
                    # Active website build — tell the model exactly what's left.
                    _wf_remaining = [
                        f for f in ["index.html", "style.css", "script.js"]
                        if f not in _wf_written
                    ]
                    _wf_steps = [
                        f"write_file(project,{_fn},<COMPLETE CONTENT>)"
                        for _fn in _wf_remaining
                    ]
                    if not _wf_served:
                        _wf_steps.append("serve(project)")
                    if _wf_steps:
                        pending_note = (
                            f" WEBSITE BUILD INCOMPLETE. Next steps in order: "
                            + " THEN ".join(_wf_steps)
                            + ". OUTPUT THE NEXT SKILL TAG NOW."
                            " No text. No explanation. Just the tag with FULL file content."
                        )
                    else:
                        pending_note = (
                            " All files written and served."
                            " Report the PREVIEW_URL from the serve ✅ result to the user."
                        )
                elif pending_summary:
                    pending_note = (
                        f" You still need to call: [{pending_summary}]."
                        " OUTPUT THE NEXT SKILL TAG NOW. No text. No explanation. Just the tag."
                    )
                else:
                    _success_steps = ", ".join(
                        f"{l['skill']}.{l.get('function', '')}"
                        for l in execution_log
                        if l.get("status") == "success"
                    )
                    _failed_steps = ", ".join(
                        f"{l['skill']}.{l.get('function', '')}"
                        for l in execution_log
                        if l.get("status") != "success"
                    )
                    _done_note = (
                        f" Completed successfully: [{_success_steps}]. DO NOT repeat these exact steps."
                        if _success_steps else ""
                    )
                    _fail_note = (
                        f" Failed: [{_failed_steps}] — try a DIFFERENT approach (different query, different function, etc.)."
                        if _failed_steps else ""
                    )
                    # Count successful web searches in this request's execution log
                    _successful_web_searches = sum(
                        1 for l in all_execution_logs
                        if l.get("skill") == "web" and l.get("function") == "search"
                        and l.get("status") == "success"
                    )
                    # Always embed the original request so the model can answer
                    # even if context overflow truncated it from the message history.
                    _orig_q = f'"{req.message[:200].replace(chr(34), chr(39))}"'
                    if _successful_web_searches >= 1:
                        pending_note = (
                            f"{_done_note}"
                            f" User asked: {_orig_q}."
                            " You have web search results above."
                            " If the results directly answer the question, synthesize your answer now in plain text."
                            " If results are off-topic or outdated, try ONE different query. Do not repeat queries."
                        )
                    else:
                        pending_note = (
                            f"{_done_note}{_fail_note}"
                            f" User asked: {_orig_q}."
                            " Is the user's full request now answered by the ✅ results above?"
                            " YES → write your final text answer now, using the results."
                            " NO → output the next skill tag immediately. No intro text."
                        )
                # Build brief injection for the continuation message.
                brief_injection = ""
                # Check if analyze_design_folder just ran but FAILED (error result)
                _just_ran_analyze = any(
                    _l.get("skill") == "web_builder"
                    and _l.get("function") == "analyze_design_folder"
                    for _l in execution_log
                )
                if _pending_design_brief:
                    # Successful brief — inject it with explicit CSS instructions
                    brief_injection = (
                        f"\n\nDESIGN BRIEF — COPY THESE EXACT VALUES INTO YOUR CSS AND HTML:\n"
                        f"{_pending_design_brief}\n\n"
                        "MANDATORY CSS rules when writing style.css:\n"
                        "1. First line = font_import from brief (Google Fonts @import)\n"
                        "2. :root { } = ALL css_variables from brief\n"
                        "3. body { font-family: <body_font from brief> }\n"
                        "4. h1,h2,h3 { font-family: <heading_font from brief> }\n"
                        "5. Each section's css_hint = paste it as the base rule for that section\n"
                        "6. Write MINIMUM 200 lines of CSS. Sparse CSS = broken site.\n"
                        "7. index.html MUST contain ALL sections listed in the 'sections' array."
                    )
                    _pending_design_brief = ""  # inject once only
                elif _just_ran_analyze:
                    # analyze_design_folder ran but returned an error (wrong path).
                    # Force the model to retry with a correct path before writing files.
                    brief_injection = (
                        "\n\nERROR: analyze_design_folder failed — wrong folder path."
                        " The ✅ result above shows available_folders. "
                        "RETRY analyze_design_folder with the correct path from that list."
                        " DO NOT write HTML or CSS until the brief succeeds."
                    )
                _deadend_nudge = ""
                if _consecutive_error_iters >= 2:
                    _sfc_blocked = [f"{s}.{f}" for (s, f), n in _skill_failure_counts.items() if n >= 3]
                    _sfc_msg = (f" DO NOT call {', '.join(_sfc_blocked)} again — failed 3+ times this session." if _sfc_blocked else "")
                    _deadend_nudge = (
                        f" ⚠️ DEAD-END: {_consecutive_error_iters} consecutive iterations"
                        " all failed. Do NOT repeat the same approach and do NOT give up."
                        f"{_sfc_msg}"
                        " Pivot completely: try a different skill, different query,"
                        " or use create_skill to build a custom tool for this problem."
                    )
                    _consecutive_error_iters = 0  # reset after nudge
                # Local models (Gemma, Qwen, etc.) only attend to the initial
                # system message — mid-conversation "system" role injections are
                # silently ignored by most Ollama chat templates.  Use "user" role
                # instead so the continuation instruction actually reaches the model.
                _continuation_role = "user" if model_source == "local" else "system"
                messages.append({
                    "role": _continuation_role,
                    "content": (
                        f"Step done.{brief_injection}{pending_note}{_deadend_nudge}"
                        f" Skill syntax: <skill:NAME.FUNC>args</skill:NAME.FUNC>"
                    ),
                })
                ai_reply = executed_reply

            else:
                # ── Native function calling (cloud) ──────────────────────────
                tool_calls = llm_response.get("tool_calls")
                ai_reply   = llm_response.get("content") or ""

                if not tool_calls:
                    # No tool calls — either LLM gave a final answer, or it described
                    # a plan without acting (e.g. "I will click Compose...").
                    # Detect planning language and push up to 2 times total to force
                    # the LLM to actually call a tool instead of describing future actions.
                    _PLAN_SIGNALS = (
                        "i will", "i'll", "let me", "i need to", "i'm going to",
                        "i should", "step 1", "first i", "first,", "navigate to",
                        "will click", "will open", "will send", "will type",
                        "going to", "i'll start", "now i", "next i", "then i",
                        "i can see", "i'll now", "i'll click", "i'll type",
                    )
                    _looks_like_plan = (
                        _continuation_pushes < 2
                        and len(ai_reply) > 40
                        and any(sig in ai_reply.lower() for sig in _PLAN_SIGNALS)
                    )
                    if _looks_like_plan:
                        _continuation_pushes += 1
                        print(f"⚠️  Iteration {iteration}: LLM described a plan without calling tools — injecting continuation push #{_continuation_pushes}")
                        messages.append({"role": "assistant", "content": ai_reply})
                        messages.append({
                            "role": "user",
                            "content": (
                                "Stop describing — call the tool now using the function-calling interface. "
                                "Do not write any more text. Just call the tool."
                            ),
                        })
                        continue
                    print(f"✅ Agent loop complete after {iteration} iteration(s)")
                    break

                print(f"⚙️  Iteration {iteration}: {len(tool_calls)} tool call(s), looping…")
                tool_result_messages, execution_log = _execute_tool_calls(tool_calls)
                all_execution_logs.extend(execution_log)
                for _l in execution_log:
                    _stream_emit({"type": "skill", "skill": _l.get("skill"), "function": _l.get("function"), "status": _l.get("status")})

                # Append assistant turn (must include tool_calls for API continuity)
                messages.append({
                    "role":       "assistant",
                    "content":    ai_reply or None,
                    "tool_calls": tool_calls,
                })
                # Append one tool result message per call
                messages.extend(tool_result_messages)

                # Expand preview schema with any newly-called skills so they remain
                # callable on subsequent iterations without a full schema rebuild.
                for _tc in tool_calls:
                    _tc_skill = (_tc.get("function", {}).get("name") or "").split("__")[0]
                    if _tc_skill:
                        _used_skills_this_req.add(_tc_skill)
                tools = _build_tools_schema_for_request(req.message, used_skills=_used_skills_this_req)

                # Dead-end detection: track consecutive all-error iterations
                _all_failed_this_iter = bool(execution_log) and all(
                    l.get("status") == "error" for l in execution_log
                )
                if _all_failed_this_iter:
                    _consecutive_error_iters += 1
                else:
                    _consecutive_error_iters = 0
                for _l in execution_log:
                    _sfc_key = (_l.get("skill", ""), _l.get("function", ""))
                    if _l.get("status") == "error":
                        _skill_failure_counts[_sfc_key] = _skill_failure_counts.get(_sfc_key, 0) + 1
                    else:
                        _skill_failure_counts.pop(_sfc_key, None)

                if _consecutive_error_iters >= 2:
                    _sfc_blocked = [f"{s}.{f}" for (s, f), n in _skill_failure_counts.items() if n >= 3]
                    _sfc_msg = (f" DO NOT call {', '.join(_sfc_blocked)} again — failed 3+ times this session." if _sfc_blocked else "")
                    messages.append({
                        "role": "user",
                        "content": (
                            f"⚠️ You have failed {_consecutive_error_iters} consecutive"
                            " iterations. Do NOT give up or report failure."
                            f"{_sfc_msg}"
                            " Pivot completely: try a different skill, different approach,"
                            " or use create_skill to build a custom tool for this problem."
                            " Keep going."
                        ),
                    })
                    _consecutive_error_iters = 0  # reset after nudge

                # Keep a text summary as fallback ai_reply in case the loop exhausts
                ai_reply = "\n".join(
                    (
                        f"[✅ {l['skill']}.{l.get('function', '')} Result: {l.get('result', '')}]"
                        if l["status"] == "success"
                        else f"[❌ {l['skill']}.{l.get('function', '')} Error: {l.get('error', '')}]"
                    )
                    for l in execution_log
                )

        else:
            # Hit the iteration ceiling
            print(f"⚠️  Agent loop reached max iterations ({MAX_ITERATIONS})")
            ai_reply += f"\n\n[⚠️ Agent reached max reasoning steps ({MAX_ITERATIONS}). Task may be incomplete.]"

        # In cloud mode the LLM's final text should never contain [✅ ...] blocks
        # (real results come through tool messages). Strip any the LLM hallucinated.
        # In local mode, [✅ ...] blocks are the real injected results — leave them alone.
        if model_source != "local":
            ai_reply = _strip_fake_result_blocks(ai_reply)

        # Replace "undefined" URL placeholders the model hallucinated with the
        # real preview URL extracted from the web_builder.serve execution log.
        if "undefined" in ai_reply:
            for _log in all_execution_logs:
                if (
                    _log.get("skill") == "web_builder"
                    and _log.get("function") == "serve"
                    and _log.get("status") == "success"
                ):
                    _url_match = re.search(r'PREVIEW_URL:\s*(https?://\S+)', str(_log.get("result", "")))
                    if _url_match:
                        ai_reply = ai_reply.replace("undefined", _url_match.group(1))
                    break

        # 6. Update in-memory session history (synchronous — next request needs fresh history)
        updated_history = list(history)
        updated_history.append({"role": "user", "content": req.message})
        _reply_for_history = _strip_fake_result_blocks(ai_reply)
        updated_history.append({"role": "assistant", "content": _reply_for_history})
        _hist_cap = SESSION_MAX_MESSAGES_LOCAL if _is_local_model else SESSION_MAX_MESSAGES
        save_session_history(session_id, updated_history, max_messages=_hist_cap)

        # 7. Store long-term memory (ChromaDB + JSONL) — deferred to background so
        #    the response is returned immediately without waiting for I/O.
        _exec_logs_snap = list(all_execution_logs)
        _req_message    = req.message
        _req_model      = req.model
        _req_session_id = session_id  # use the normalized value (req.session_id or "default"), not the raw req.session_id
        task_type = _detect_task_type(
            _req_message, [log["skill"] for log in _exec_logs_snap if log.get("skill")]
        )
        _ai_reply_snap  = ai_reply

        def _persist_async():
            try:
                # ── Correction detection ──────────────────────────────────
                if _detect_correction(_req_message):
                    prev_ai = ""
                    hist = get_session_history(session_id)
                    for msg in reversed(hist[:-1]):  # skip current assistant turn
                        if msg.get("role") == "assistant":
                            prev_ai = msg.get("content", "")
                            break

                    skill_ctx = _extract_skill_context(prev_ai, fallback=task_type)
                    lesson = {
                        "timestamp": datetime.now().isoformat(),
                        "type": "correction",
                        "skill": skill_ctx,
                        "error_type": "user_correction",
                        "skill_context": skill_ctx,
                        "task_type": task_type,
                        "session_id": _req_session_id,
                        "correction_msg": _req_message[:500],
                        "bad_reply_preview": prev_ai[:300],
                        "fix_applied": _ai_reply_snap[:400] if _ai_reply_snap else f"User flagged this reply as wrong: {_req_message[:300]}",
                    }
                    try:
                        _lessons_path = Path("/app/memory/lessons.jsonl")
                        _lessons_path.parent.mkdir(parents=True, exist_ok=True)
                        with open(_lessons_path, "a", encoding="utf-8") as _lf:
                            _lf.write(json.dumps(lesson, ensure_ascii=False) + "\n")
                        print(f"📝 Correction recorded — skill_context: {skill_ctx}")
                    except Exception as _le:
                        print(f"⚠️  Failed to record correction: {_le}")
                # ─────────────────────────────────────────────────────────

                store_memory_separate(
                    _req_message, _ai_reply_snap,
                    task_type=task_type, session_id=_req_session_id
                )
                save_to_jsonl(
                    _req_message, _ai_reply_snap,
                    session_id=_req_session_id,
                    metadata={
                        "model": _req_model,
                        "execution_log": _exec_logs_snap,
                        "skills_used": [log["skill"] for log in _exec_logs_snap if log.get("skill")],
                    },
                )
            except Exception as _e:
                print(f"⚠️  Async persist error: {_e}")

        threading.Thread(target=_persist_async, daemon=True, name="persist-memory").start()

        # Post-loop: detect skill creation opportunity — non-blocking, never delays response
        _exec_logs_snap2  = list(all_execution_logs)
        _iter_snap        = iteration
        _user_msg_snap    = req.message

        def _maybe_suggest_skill():
            try:
                if "autoimprove" not in skills:
                    return
                result = skills["autoimprove"].should_create_skill(
                    _exec_logs_snap2, _iter_snap, _user_msg_snap
                )
                if isinstance(result, dict) and result.get("create"):
                    skills["autoimprove"].park_idea(
                        f"Skill opportunity [{result.get('trigger')}]: {result.get('reason')} "
                        f"— Task: {_user_msg_snap[:200]}",
                        source="auto_detect",
                    )
                    print(f"💡 Skill opportunity parked (trigger={result.get('trigger')}): {result.get('reason')}")
            except Exception as _se:
                print(f"⚠️  Skill detection error: {_se}")

        threading.Thread(target=_maybe_suggest_skill, daemon=True, name="skill-detect").start()

        _stream_emit({"type": "reply", "reply": ai_reply, "skills_called": len(all_execution_logs), "token_est": _token_est})
        return {
            "reply": ai_reply,
            "execution_log": all_execution_logs,
            "skills_called": len(all_execution_logs),
            "token_est": _token_est,
        }

    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="LLM request timed out")
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 429:
            raise HTTPException(
                status_code=429,
                detail="Rate limit reached on the AI provider. Please wait a moment and try again."
            )
        raise HTTPException(status_code=502, detail=f"LLM service error: {str(e)}")
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=f"LLM service error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))



@app.post("/chat/stream")
async def chat_stream(req: PromptRequest, api_key: str = Depends(verify_api_key)):
    """Streaming version of /chat — emits Server-Sent Events as each iteration and
    skill execution completes. Drop-in companion to /chat; same request body.

    Event types:
      iteration  {"type":"iteration","n":1,"max":20}
      skill      {"type":"skill","skill":"web","function":"search","status":"success"}
      reply      {"type":"reply","reply":"...","skills_called":3}
      error      {"type":"error","message":"..."}
      heartbeat  {"type":"heartbeat"}   (keep-alive every ~1 s while waiting)
      done       {"type":"done"}        (always the last event)
    """
    _check_rate_limit(api_key)
    stream_q: _queue_module.Queue = _queue_module.Queue()

    def _run() -> None:
        _stream_local.queue = stream_q
        try:
            chat(req, api_key)
        except HTTPException as exc:
            stream_q.put({"type": "error", "status": exc.status_code, "message": exc.detail})
        except Exception as exc:
            stream_q.put({"type": "error", "message": str(exc)[:400]})
        finally:
            _stream_local.queue = None
            stream_q.put({"type": "done"})  # sentinel — always last

    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _run)

    async def generate():
        while True:
            try:
                event = await loop.run_in_executor(
                    None, lambda: stream_q.get(timeout=1)
                )
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("done", "error"):
                    break
            except _queue_module.Empty:
                yield 'data: {"type":"heartbeat"}\n\n'

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/download/{filename}", dependencies=[Depends(verify_api_key)])
def download_file(filename: str):
    """Serve a file from /app/memory/knowledge/ with appropriate MIME type."""
    safe_name = Path(filename).name  # strip any path traversal
    knowledge_root = Path("/app/memory/knowledge").resolve()
    file_path = (knowledge_root / safe_name).resolve()
    # Guard against symlink traversal — resolved path must stay inside knowledge root
    if not file_path.is_relative_to(knowledge_root):
        raise HTTPException(status_code=403, detail="Access denied")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {safe_name}")
    _mime_map = {
        ".pdf":  "application/pdf",
        ".txt":  "text/plain; charset=utf-8",
        ".md":   "text/markdown; charset=utf-8",
        ".csv":  "text/csv; charset=utf-8",
        ".json": "application/json",
        ".html": "text/html; charset=utf-8",
    }
    suffix = Path(safe_name).suffix.lower()
    media_type = _mime_map.get(suffix, "application/octet-stream")
    return FileResponse(
        path=str(file_path),
        filename=safe_name,
        media_type=media_type,
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)