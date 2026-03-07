# knowledge_base.py
"""
Core skill: business knowledge base backed by ChromaDB.

Drop any document (PDF, DOCX, XLSX, CSV, TXT, MD, …) into
/app/memory/knowledge/ and call ingest_folder() to chunk and store it
in a dedicated ChromaDB collection ('business_knowledge').

The agent can then answer questions grounded in your actual business
documents rather than general knowledge.
"""

import os
import sys
import json
import hashlib
from datetime import datetime
from pathlib import Path

# Ensure the core skills directory is on the path so sibling skills can be
# imported directly instead of executed dynamically via importlib.
_CORE_DIR = str(Path(__file__).parent)
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)

import document_parser as _document_parser

try:
    import litellm as _litellm
    _HAS_LITELLM = True
except ImportError:
    _HAS_LITELLM = False

NAME = "knowledge_base"
DOC = (
    "Business knowledge base backed by ChromaDB. "
    "Drop files into /app/memory/knowledge/ then call ingest_folder() to index them. "
    "Functions: "
    "ingest_folder(folder_path?)→parse & chunk all new/changed docs into ChromaDB, auto-generates LLM summary per doc; "
    "ingest_file(path)→ingest a single file; "
    "search(query, n_results?)→semantic search over business documents; "
    "get_summary(filename)→retrieve the stored LLM summary for an ingested document; "
    "list_ingested()→show all indexed documents with chunk/word counts; "
    "delete_document(filename)→remove a document and its chunks from the index; "
    "status()→show collection stats and ChromaDB health."
)

# ── Config ─────────────────────────────────────────────────────────────────────

_KNOWLEDGE_DIR = Path("/app/memory/knowledge")
_INDEX_FILE    = _KNOWLEDGE_DIR / ".index.json"
_CHROMA_HOST   = os.getenv("CHROMA_HOST", "chroma")
_COLLECTION    = "business_knowledge"
_CHUNK_SIZE    = 800   # characters per chunk
_CHUNK_OVERLAP = 100   # overlap between consecutive chunks

# ── LLM config (for auto-summarization) ────────────────────────────────────────
_LLM_MODEL = os.getenv("LITELLM_DEFAULT_MODEL", "trinity-default")
_LLM_BASE  = os.getenv("LITELLM_API_BASE",      "http://litellm:4000")
_LLM_KEY   = os.getenv("LITELLM_MASTER_KEY",    os.getenv("API_KEY", "sk-1234567890"))

_SUPPORTED = {
    "pdf", "docx", "doc", "xlsx", "xls", "csv",
    "txt", "md", "markdown", "json", "jsonl",
    "yaml", "yml", "log", "xml", "html", "htm",
    "png", "jpg", "jpeg", "webp", "gif", "bmp", "tiff", "tif",
}

# ── ChromaDB ───────────────────────────────────────────────────────────────────

def _get_collection():
    try:
        import chromadb
        client = chromadb.HttpClient(host=_CHROMA_HOST, port=8000)
        return client.get_or_create_collection(name=_COLLECTION)
    except Exception as e:
        raise RuntimeError(f"ChromaDB connection failed ({_CHROMA_HOST}:8000): {e}")

# ── Index (tracks ingested files by content hash) ─────────────────────────────

def _load_index() -> dict:
    _KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    if _INDEX_FILE.exists():
        try:
            return json.loads(_INDEX_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def _save_index(index: dict):
    _KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    _INDEX_FILE.write_text(json.dumps(index, indent=2), encoding="utf-8")

def _file_hash(path: Path) -> str:
    h = hashlib.md5()
    h.update(path.read_bytes())
    return h.hexdigest()

# ── Text extraction (delegates to document_parser) ────────────────────────────

def _extract_text(path: Path) -> str:
    return _document_parser.read(str(path))

# ── LLM summarization (optional — skipped gracefully if LLM unavailable) ───────

def _summarize_text(text: str, filename: str) -> str:
    """Call the LLM proxy to produce a concise summary. Returns '' on any failure."""
    if not _HAS_LITELLM:
        return ""
    snippet = text[:6000]
    prompt = (
        f"Summarize this document concisely. Document name: {filename}\n\n"
        f"---\n{snippet}\n---\n\n"
        "Provide:\n"
        "1. A 2-3 sentence overview\n"
        "2. 3-5 key takeaways as bullet points\n"
        "Keep total response under 250 words."
    )
    try:
        response = _litellm.completion(
            model=_LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            timeout=60,
            api_base=_LLM_BASE,
            api_key=_LLM_KEY,
        )
        return response.choices[0].message.content.strip()
    except Exception:
        return ""

# ── Chunking ──────────────────────────────────────────────────────────────────

def _chunk_text(text: str, rel_path: str, project: str) -> list:
    """Split text into overlapping chunks. Returns list of (chunk_text, metadata)."""
    chunks = []
    start  = 0
    idx    = 0
    filename = Path(rel_path).name
    while start < len(text):
        end   = start + _CHUNK_SIZE
        chunk = text[start:end]
        if chunk.strip():
            chunks.append((chunk, {
                "filename":    filename,
                "project":     project,
                "rel_path":    rel_path,
                "chunk_index": idx,
                "source":      f"{rel_path}#chunk{idx}",
            }))
            idx += 1
        start = end - _CHUNK_OVERLAP
        if start >= len(text):
            break
    return chunks

# ── Core ingestion logic ───────────────────────────────────────────────────────

def _ingest_one(path: Path, col, index: dict) -> str:
    # Use path relative to knowledge dir as the unique key — avoids same-name collisions
    try:
        rel_path = str(path.relative_to(_KNOWLEDGE_DIR))
    except ValueError:
        rel_path = path.name
    project  = Path(rel_path).parts[0] if len(Path(rel_path).parts) > 1 else "_root"
    ext      = path.suffix.lower().lstrip(".")

    if ext not in _SUPPORTED:
        return f"  ⏭  {rel_path} — skipped (unsupported type .{ext})"

    file_hash = _file_hash(path)
    if index.get(rel_path, {}).get("hash") == file_hash:
        return f"  ✅ {rel_path} — already current, skipped"

    # Remove old chunks for this file before re-ingesting
    if rel_path in index:
        try:
            col.delete(where={"rel_path": rel_path})
        except Exception:
            pass

    text = _extract_text(path)
    if "[" in text[:20] and "error" in text.lower():
        return f"  ❌ {rel_path} — extraction failed: {text[:120]}"

    word_count = len(text.split())
    if word_count < 5:
        return f"  ⚠️  {rel_path} — too short to index ({word_count} words)"

    chunks = _chunk_text(text, rel_path, project)
    if not chunks:
        return f"  ⚠️  {rel_path} — no chunks produced"

    safe_id  = rel_path.replace("/", "__").replace("\\", "__")
    col.add(
        ids       =[f"{safe_id}__chunk{i}" for i in range(len(chunks))],
        documents =[c[0] for c in chunks],
        metadatas =[c[1] for c in chunks],
    )

    # ── Optional LLM summary ─────────────────────────────────────────────────
    summary = _summarize_text(text, path.name)
    if summary:
        sum_id = f"{safe_id}__summary"
        try:
            col.delete(ids=[sum_id])
        except Exception:
            pass
        col.add(
            ids       =[sum_id],
            documents =[summary],
            metadatas =[{
                "filename":    filename,
                "project":     project,
                "rel_path":    rel_path,
                "chunk_index": -1,
                "chunk_type":  "summary",
                "source":      f"{rel_path}#summary",
            }],
        )

    index[rel_path] = {
        "hash":        file_hash,
        "chunks":      len(chunks),
        "words":       word_count,
        "project":     project,
        "ingested_at": datetime.now().isoformat(),
        "path":        str(path),
        "summary":     summary,
    }
    suffix = " + summary" if summary else ""
    return f"  ✅ {rel_path} — {len(chunks)} chunks, {word_count} words{suffix}"

# ══════════════════════════════════════════════════════════════════════════════
# PUBLIC SKILL FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def ingest_folder(*args) -> str:
    """
    Scan /app/memory/knowledge/ (or a custom path) and ingest any new or
    changed documents into the business_knowledge ChromaDB collection.
    Already-current files are skipped automatically.
    Usage: ingest_folder()  or  ingest_folder('/app/memory/knowledge/')
    """
    folder = Path(str(args[0]).strip()) if args else _KNOWLEDGE_DIR

    if not folder.exists():
        folder.mkdir(parents=True, exist_ok=True)
        return (
            f"📁 Created knowledge folder at {folder}\n"
            f"Drop your documents there and call ingest_folder() again."
        )

    files = [f for f in folder.rglob("*") if f.is_file() and not f.name.startswith(".")]
    if not files:
        return (
            f"📭 No files found in {folder}\n"
            f"Drop PDF, DOCX, TXT, CSV, XLSX, or MD files there and try again."
        )

    try:
        col = _get_collection()
    except RuntimeError as e:
        return f"❌ {e}"

    index = _load_index()
    lines = [f"📚 Ingesting {len(files)} file(s) from {folder} (recursive):\n"]

    for f in sorted(files):
        lines.append(_ingest_one(f, col, index))

    _save_index(index)

    ingested = sum(1 for l in lines if "✅" in l and "already current" not in l)
    skipped  = sum(1 for l in lines if "already current" in l)
    lines.append(
        f"\nDone — {ingested} ingested, {skipped} already up to date. "
        f"Collection: '{_COLLECTION}'"
    )
    return "\n".join(lines)


def ingest_file(*args) -> str:
    """
    Ingest a single file into the business knowledge base.
    Path can be absolute or relative to /app/memory/knowledge/.
    Usage: ingest_file('report.pdf')  or  ingest_file('/app/memory/knowledge/report.pdf')
    """
    if not args:
        return "❌ Usage: ingest_file(path)"

    path = Path(str(args[0]).strip())
    if not path.is_absolute():
        path = _KNOWLEDGE_DIR / path
    if not path.exists():
        return f"❌ File not found: {path}"

    try:
        col = _get_collection()
    except RuntimeError as e:
        return f"❌ {e}"

    index  = _load_index()
    result = _ingest_one(path, col, index)
    _save_index(index)
    return result


def search(*args) -> str:
    """
    Semantic search over all ingested business documents.
    Returns the most relevant chunks with source filenames and relevance scores.
    Usage: search(query)  or  search(query, n_results)
    Default n_results: 5
    """
    if not args:
        return "❌ Usage: search(query)  or  search(query, 10)"

    query = str(args[0]).strip()
    n     = int(args[1]) if len(args) > 1 else 5

    try:
        col = _get_collection()
    except RuntimeError as e:
        return f"❌ {e}"

    try:
        results = col.query(query_texts=[query], n_results=n)
    except Exception as e:
        return f"❌ ChromaDB query error: {e}"

    docs      = results.get("documents",  [[]])[0]
    metas     = results.get("metadatas",  [[]])[0]
    distances = results.get("distances",  [[]])[0]

    if not docs:
        return f"🔍 No results for: '{query}'\nHave you run ingest_folder() yet?"

    lines = [f"🔍 Top {len(docs)} results for: '{query}'\n"]
    for i, (doc, meta, dist) in enumerate(zip(docs, metas, distances), 1):
        filename = meta.get("filename", "unknown")
        chunk    = meta.get("chunk_index", "?")
        score    = round(1 - dist, 3) if dist is not None else "?"
        preview  = doc[:400].replace("\n", " ")
        lines.append(
            f"[{i}] {filename} (chunk {chunk}, relevance {score})\n"
            f"    {preview}{'…' if len(doc) > 400 else ''}\n"
        )
    return "\n".join(lines)


def list_ingested(*args) -> str:
    """
    List all documents currently indexed in the business knowledge base.
    Usage: list_ingested()
    """
    index = _load_index()
    if not index:
        return (
            "📭 No documents ingested yet.\n"
            f"Drop files into {_KNOWLEDGE_DIR} and call ingest_folder()."
        )

    lines = [f"📚 Business knowledge base — {len(index)} document(s):\n"]
    for filename, info in sorted(index.items()):
        ingested = info.get("ingested_at", "unknown")[:19].replace("T", " ")
        chunks   = info.get("chunks", "?")
        words    = info.get("words",  "?")
        has_summary = "· ✓ summary" if info.get("summary") else ""
        lines.append(
            f"  • {filename}\n"
            f"    {chunks} chunks · {words:,} words · indexed {ingested} {has_summary}"
        )

    try:
        col   = _get_collection()
        count = col.count()
        lines.append(f"\nTotal chunks in ChromaDB '{_COLLECTION}': {count}")
    except Exception:
        pass

    return "\n".join(lines)


def delete_document(*args) -> str:
    """
    Remove a document and all its chunks from the knowledge base.
    Usage: delete_document('old-report.pdf')
    """
    if not args:
        return "❌ Usage: delete_document(filename)"

    filename = Path(str(args[0]).strip()).name  # strip any path prefix
    index    = _load_index()

    if filename not in index:
        return f"❌ '{filename}' not in index. Use list_ingested() to see what's indexed."

    try:
        col = _get_collection()
        col.delete(where={"filename": filename})
    except Exception as e:
        return f"❌ ChromaDB delete error: {e}"

    del index[filename]
    _save_index(index)
    return f"✅ Removed '{filename}' from the business knowledge base."


def get_summary(*args) -> str:
    """
    Retrieve the stored LLM summary for an ingested document.
    Usage: get_summary('report.pdf')
    """
    if not args:
        return "❌ Usage: get_summary(filename)"

    filename = Path(str(args[0]).strip()).name
    index    = _load_index()

    # Try exact match first, then match on filename portion of rel_path
    entry = index.get(filename) or next(
        (v for k, v in index.items() if Path(k).name == filename), None
    )
    if not entry:
        return f"❌ '{filename}' not in index. Use list_ingested() to see what's indexed."

    summary = entry.get("summary", "")
    if not summary:
        return (
            f"⚠️  No summary stored for '{filename}'.\n"
            "Re-ingest the file to generate one: delete it first with delete_document(), "
            "then call ingest_file() again."
        )

    ingested = entry.get("ingested_at", "")[:19].replace("T", " ")
    return f"📋 Summary — {filename}\n(Indexed: {ingested})\n\n{summary}"


def status(*args) -> str:
    """
    Show knowledge base health: folder path, document count, ChromaDB stats.
    Usage: status()
    """
    index = _load_index()
    lines = [
        "📊 Knowledge Base Status",
        f"  Folder    : {_KNOWLEDGE_DIR}",
        f"  Documents : {len(index)} indexed",
    ]

    try:
        col   = _get_collection()
        count = col.count()
        lines.append(f"  ChromaDB  : ✅ connected — {count} chunks in '{_COLLECTION}'")
    except Exception as e:
        lines.append(f"  ChromaDB  : ❌ {e}")

    if index:
        total_words  = sum(v.get("words",  0) for v in index.values())
        total_chunks = sum(v.get("chunks", 0) for v in index.values())
        lines.append(f"  Words     : {total_words:,}")
        lines.append(f"  Chunks    : {total_chunks:,}")

    return "\n".join(lines)
