"""
Core skill: find_tools — always-on, doc-derived skill discovery.

Builds a weighted token index from every skill's OWN files (name, SHORT_DOC,
DOC, function names, function docstrings) so the agent can locate capabilities
mid-task without hand-written keyword dictionaries. app.py reads the
"UNLOCKED:" line of find_tools results to expand the per-request tool schema.
"""
import ast
import os
import re
from pathlib import Path

NAME = "tool_finder"
SHORT_DOC = "Search ALL skills' docs; returns matching skills + exact calling syntax."
DOC = (
    "Find skills by capability, even ones not visible in your tool list. "
    "Functions: find_tools(query, n=5) -> matching skills with calling syntax. "
    "Call this BEFORE asking the user or giving up whenever the task needs a "
    "capability you cannot see above."
)
__all__ = ["find_tools", "build_tool_index", "trigger_tokens", "match_tools"]

_STOP = {"the", "and", "for", "with", "your", "this", "that", "from", "have",
         "are", "you", "can", "how", "what", "when", "use", "using", "into",
         "via", "per", "all", "any", "new", "not"}

_CACHE = {"key": None, "index": None, "meta": None}


def _norm(word: str) -> str:
    word = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", word).lower()
    return word


def _tokens(text: str, min_len: int = 3) -> set:
    out = set()
    for raw in re.findall(r"[A-Za-z0-9]+", text or ""):
        parts = [p for p in _norm(raw).split("_") if p]
        for p in parts:
            if len(p) >= min_len and p not in _STOP:
                out.add(p)
            if len(p) > 3 and p.endswith("s"):
                out.add(p[:-1])
    return out


def _skill_dirs() -> list:
    base = Path(os.getenv("TRINITY_SKILLS_DIR", "/app/skills"))
    if not base.exists():
        base = Path(__file__).resolve().parent.parent  # host checkout fallback
    return [base / "core", base / "dynamic"]


def _extract(path: Path) -> dict:
    meta = {"short_doc": "", "doc": "", "functions": {}}
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return meta
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in ("SHORT_DOC", "DOC"):
                    try:
                        meta[t.id.lower()] = ast.literal_eval(node.value)
                    except Exception:
                        pass
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_") or node.col_offset > 0:
                continue  # skip private and nested (closure) functions
            doc = ""
            if node.body and isinstance(node.body[0], ast.Expr) \
                    and isinstance(node.body[0].value, ast.Constant) \
                    and isinstance(node.body[0].value.value, str):
                doc = node.body[0].value.value
            meta["functions"][node.name] = " ".join(doc.split()[:24])
    return meta


def build_tool_index(force: bool = False) -> dict:
    """token -> {skill_name: weight}. Cached by (path, mtime) signature."""
    files = {}
    for d in _skill_dirs():
        if d.exists():
            for f in d.glob("*.py"):
                if not f.name.startswith("_"):
                    files[f] = f.stat().st_mtime
    key = tuple(sorted((str(f), m) for f, m in files.items()))
    if not force and _CACHE["key"] == key and _CACHE["index"] is not None:
        return _CACHE["index"]
    index, meta = {}, {}
    for f in files:
        name = f.stem
        m = _extract(f)
        meta[name] = m

        def add(tokens: set, weight: int) -> None:
            for t in tokens:
                bucket = index.setdefault(t, {})
                bucket[name] = max(bucket.get(name, 0), weight)

        add(_tokens(name), 3)
        add(_tokens(m["short_doc"]), 2)
        add(_tokens(m["doc"]), 1)
        for fn, fdoc in m["functions"].items():
            add(_tokens(fn), 2)
            add(_tokens(fdoc), 1)
    _CACHE.update(key=key, index=index, meta=meta)
    return index


def match_tools(query: str, n: int = 5) -> list:
    index = build_tool_index()
    scores = {}
    for t in _tokens(query):
        for name, w in index.get(t, {}).items():
            scores[name] = scores.get(name, 0) + w
    return [name for name, _ in sorted(scores.items(), key=lambda kv: -kv[1])[:max(1, int(n))]]


def find_tools(query: str = "", n: int = 5) -> str:
    query = (query or "").strip().strip("'\"")
    if not query:
        return "Usage: find_tools('<what you need>') e.g. find_tools('convert pdf to text')"
    matches = match_tools(query, n)
    if not matches:
        return f"No skills matched '{query}'. Try broader words, or use web.search."
    meta = _CACHE["meta"] or {}
    lines = []
    for name in matches:
        m = meta.get(name, {})
        funcs = ", ".join(sorted(m.get("functions", {}))[:10])
        short = (m.get("short_doc") or m.get("doc", "") or "")[:110]
        lines.append(
            f"[SKILL: {name}] {short} (functions: {funcs})\n"
            f"  call: <skill:{name}.FUNCTION>args</skill:{name}.FUNCTION> or {name}__FUNCTION"
        )
    lines.append("UNLOCKED: " + ", ".join(matches))
    return "\n".join(lines)


def trigger_tokens(meta_by_name: dict) -> dict:
    """Per-skill trigger token sets derived from name + function names + SHORT_DOC.
    app.py uses this to replace its hand-written keyword dictionaries."""
    out = {}
    for name, m in (meta_by_name or {}).items():
        toks = _tokens(name) | _tokens(m.get("short_doc", ""))
        for fn in (m.get("functions") or {}):
            toks |= _tokens(fn)
        if toks:
            out[name] = toks
    return out
