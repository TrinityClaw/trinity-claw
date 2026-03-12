import os
import hashlib
import logging
from pathlib import Path

NAME = "files"

logger = logging.getLogger(__name__)
DOC = (
    "File operations: read/list anywhere in /app/, write/delete within /app/memory/ or /app/skills/dynamic/. "
    "Returns: cat(path)→full file text; ls(path)→directory listing with names; "
    "write(path, content)→confirmation string; append(path, content)→confirmation string; "
    "patch(path, old_text, new_text)→find-and-replace in a file; "
    "exists(path)→'yes' or 'no'; "
    "size(path)→human-readable size; sha256(path)→hash string; tree(path)→recursive directory layout."
)

_APP        = Path("/app")
_WRITE_ROOTS = (Path("/app/memory"), Path("/app/skills/dynamic"))

# Files that must never be readable via the skill (credentials / tokens)
_BLOCKED_READ_NAMES = frozenset({".env"})
_BLOCKED_READ_SUFFIXES = frozenset({"_token.json", "_credentials.json"})


def _is_sensitive(p: Path) -> bool:
    """Return True if the resolved path is a credential/token file."""
    name = p.name
    if name in _BLOCKED_READ_NAMES:
        return True
    for suffix in _BLOCKED_READ_SUFFIXES:
        if name.endswith(suffix):
            return True
    return False


def _read_path(path: str) -> Path:
    """Resolve path; reads are allowed anywhere inside the container except sensitive files."""
    p = Path(path) if Path(path).is_absolute() else _APP / path
    resolved = p.resolve()
    if _is_sensitive(resolved):
        raise PermissionError(f"🔒 Access denied: '{resolved.name}' is a protected credential file.")
    return resolved


def _write_path(path: str) -> Path:
    """Resolve path; must stay within an allowed write root."""
    p = Path(path) if Path(path).is_absolute() else _WRITE_ROOTS[0] / path
    resolved = p.resolve()
    for root in _WRITE_ROOTS:
        if resolved.is_relative_to(root.resolve()):
            return resolved
    raise ValueError(f"Write path must be under {[str(r) for r in _WRITE_ROOTS]}: {path}")


# ── READ OPS ────────────────────────────────────────────────────────────────

def ls(path: str = "/app") -> str:
    """List files and directories at path"""
    try:
        p = _read_path(path)
        if not p.exists():
            return f"Not found: {path}"
        items = sorted(p.iterdir())
        lines = [f"{p} ({len(items)} items):"]
        for item in items:
            lines.append(f"  {'[D]' if item.is_dir() else '[F]'} {item.name}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def cat(path: str) -> str:
    """Read and return file contents"""
    try:
        return _read_path(path).read_text(encoding="utf-8")
    except Exception as e:
        return f"Error: {e}"


def pwd() -> str:
    """Return current working directory"""
    return os.getcwd()


def exists(path: str) -> str:
    """Check whether a file or directory exists"""
    try:
        p = _read_path(path)
        return f"Exists: {p}" if p.exists() else f"Not found: {path}"
    except Exception as e:
        return f"Error: {e}"


def size(path: str) -> str:
    """Return file size in bytes"""
    try:
        return f"{_read_path(path).stat().st_size} bytes"
    except Exception as e:
        return f"Error: {e}"


def sha256(path: str) -> str:
    """Return SHA-256 hash of a file"""
    try:
        h = hashlib.sha256()
        h.update(_read_path(path).read_bytes())
        return h.hexdigest()
    except Exception as e:
        return f"Error: {e}"


# ── WRITE OPS ────────────────────────────────────────────────────────────────

def write(path: str, content: str) -> str:
    """Write content to a file (path relative to /app/memory/ if not absolute)"""
    try:
        p = _write_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"✅ Wrote {p} ({p.stat().st_size} bytes)"
    except Exception as e:
        return f"Error: {e}"


def append(path: str, content: str) -> str:
    """Append content to a file"""
    try:
        p = _write_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(content)
        return f"✅ Appended to {p}"
    except Exception as e:
        return f"Error: {e}"


def patch(path: str, old_text: str, new_text: str) -> str:
    """
    Find and replace text inside a file (first occurrence).
    File must be within /app/memory/ or /app/skills/dynamic/.
    Usage: patch(path, old_text, new_text)
    """
    try:
        p = _write_path(path)
        if not p.exists():
            return f"File not found: {path}"
        content = p.read_text(encoding="utf-8")
        if old_text not in content:
            return f"❌ Text not found in {path}:\n  '{old_text[:80]}'"
        updated = content.replace(old_text, new_text, 1)
        p.write_text(updated, encoding="utf-8")
        return f"✅ Patched {path} — replaced {len(old_text)} chars with {len(new_text)} chars"
    except Exception as e:
        return f"Error: {e}"


def patch_all(path: str, old_text: str, new_text: str) -> str:
    """
    Find and replace ALL occurrences of text inside a file.
    File must be within /app/memory/ or /app/skills/dynamic/.
    Usage: patch_all(path, old_text, new_text)
    """
    try:
        p = _write_path(path)
        if not p.exists():
            return f"File not found: {path}"
        content = p.read_text(encoding="utf-8")
        count = content.count(old_text)
        if count == 0:
            return f"❌ Text not found in {path}:\n  '{old_text[:80]}'"
        updated = content.replace(old_text, new_text)
        p.write_text(updated, encoding="utf-8")
        return f"✅ Patched {path} — replaced {count} occurrence(s)"
    except Exception as e:
        return f"Error: {e}"


def mkdir(path: str) -> str:
    """Create a directory (and parents) within allowed write roots"""
    try:
        p = _write_path(path)
        p.mkdir(parents=True, exist_ok=True)
        return f"✅ Created: {p}"
    except Exception as e:
        return f"Error: {e}"


def delete(path: str) -> str:
    """Delete a file within allowed write roots"""
    try:
        p = _write_path(path)
        if not p.exists():
            return f"Not found: {path}"
        p.unlink()
        return f"✅ Deleted: {path}"
    except Exception as e:
        return f"Error: {e}"


# ── ANALYSIS OPS ─────────────────────────────────────────────────────────────

def tree(path: str = "/app") -> str:
    """Summarise a directory tree: file count by type, total size, largest file"""
    try:
        p = _read_path(path)
        if not p.exists():
            return f"Not found: {path}"
        if p.is_file():
            st = p.stat()
            return f"{p.name}  {st.st_size} bytes"

        type_counts: dict = {}
        total_size = 0
        total_files = 0
        total_dirs = 0
        largest = ("", 0)

        for item in p.rglob("*"):
            if item.is_dir():
                total_dirs += 1
            elif item.is_file():
                total_files += 1
                sz = item.stat().st_size
                total_size += sz
                ext = item.suffix.lower() or "(no ext)"
                type_counts[ext] = type_counts.get(ext, 0) + 1
                if sz > largest[1]:
                    largest = (str(item), sz)

        top_types = sorted(type_counts.items(), key=lambda x: x[1], reverse=True)[:8]
        type_lines = "\n".join(f"  {ext}: {n}" for ext, n in top_types)
        if len(type_counts) > 8:
            type_lines += f"\n  ... and {len(type_counts) - 8} more extensions"

        return (
            f"📂 {p}\n"
            f"  Files: {total_files}  Dirs: {total_dirs}  "
            f"Size: {total_size / 1024 / 1024:.2f} MB\n\n"
            f"File types:\n{type_lines}\n\n"
            f"Largest: {largest[0]} ({largest[1]} bytes)"
        )
    except Exception as e:
        return f"Error: {e}"


def find_duplicates(path: str) -> str:
    """Find duplicate files in a directory by comparing MD5 hashes"""
    try:
        p = _read_path(path)
        if not p.exists():
            return f"Not found: {path}"

        hashes: dict = {}
        for item in p.rglob("*"):
            if item.is_file():
                try:
                    h = hashlib.md5(item.read_bytes()).hexdigest()
                    hashes.setdefault(h, []).append(str(item))
                except Exception as e:
                    logger.warning(f"Could not hash {item}: {e}")
                    continue

        dupes = {h: files for h, files in hashes.items() if len(files) > 1}
        if not dupes:
            return "✅ No duplicate files found"

        lines = [f"Found {len(dupes)} set(s) of duplicates:\n"]
        for h, files in dupes.items():
            lines.append(f"  {h[:8]}...")
            for f in files:
                lines.append(f"    {f}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"
