import os
import hashlib
import json
import logging
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

__all__ = [
    "ls", "list_images", "cat", "read", "pwd", "exists", "size", "sha256",
    "write", "append", "patch", "patch_all", "mkdir", "delete",
    "tree", "checkpoint", "restore", "checkpoints", "find_duplicates",
]

NAME = "files"

logger = logging.getLogger(__name__)
SHORT_DOC = "Read/write/patch files in /app/; checkpoint and rollback file changes."
DOC = (
    "File operations: read/list anywhere in /app/, write/delete within /app/memory/ or /app/skills/dynamic/. "
    "Returns: cat(path)→full file text; ls(path)→directory listing with names; "
    "list_images(path)→list image files in a directory with sizes; "
    "write(path, content)→confirmation string; append(path, content)→confirmation string; "
    "patch(path, old_text, new_text)→find-and-replace in a file; "
    "exists(path)→'yes' or 'no'; "
    "size(path)→human-readable size; sha256(path)→hash string; tree(path)→recursive directory layout. "
    "Checkpoint/rollback: checkpoint(path, label)→snapshot a file or dir before destructive ops, returns checkpoint_id; "
    "restore(checkpoint_id)→rollback to a saved checkpoint; checkpoints()→list all saved checkpoints."
)

_CHECKPOINT_ROOT = Path("/app/memory/checkpoints")

_APP        = Path("/app")
_WRITE_ROOTS = (Path("/app/memory"), Path("/app/skills/dynamic"))

# Files that must never be readable via the skill (credentials / tokens)
_BLOCKED_READ_NAMES = frozenset({".env"})
_BLOCKED_READ_SUFFIXES = frozenset({"_token.json", "_credentials.json"})
_IMAGE_EXTENSIONS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif",
    ".svg", ".bmp", ".tiff", ".tif", ".ico",
})


def _is_sensitive(p: Path) -> bool:
    """Return True if the resolved path is a credential/token file."""
    name = p.name
    if name in _BLOCKED_READ_NAMES:
        return True
    if name.startswith(".env"):   # covers .env.local, .env.production, etc.
        return True
    for suffix in _BLOCKED_READ_SUFFIXES:
        if name.endswith(suffix):
            return True
    return False


def _read_path(path: str) -> Path:
    """Resolve path; reads are confined to /app except for sensitive files."""
    p = Path(path) if Path(path).is_absolute() else _APP / path
    resolved = p.resolve()
    if not resolved.is_relative_to(_APP.resolve()):
        raise PermissionError(f"🔒 Access denied: path escapes /app: {path}")
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
        lines = [f"{path} ({len(items)} items):"]
        for item in items:
            lines.append(f"  {'[D]' if item.is_dir() else '[F]'} {item.name}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


def list_images(path: str = "/app/memory") -> str:
    """List image files in a directory (recursively) with their sizes."""
    import mimetypes
    try:
        p = _read_path(path)
        if not p.exists():
            return f"Not found: {path}"
        images = []
        for f in sorted(p.rglob("*")):
            if f.is_file():
                mime, _ = mimetypes.guess_type(f.name)
                if (mime and mime.startswith("image/")) or f.suffix.lower() in _IMAGE_EXTENSIONS:
                    images.append(f"  {f.relative_to(p)}  ({f.stat().st_size / 1024 / 1024:.2f} MB)")
        return "\n".join(images) if images else "No image files found."
    except Exception as e:
        return f"Error: {e}"


_MAX_READ_SIZE = 10 * 1024 * 1024  # 10 MB


def cat(path: str = "", **kwargs) -> str:
    """Read and return file contents (max 10 MB).
    Accepts 'path' or 'args' as keyword argument for compatibility."""
    if not path:
        path = kwargs.get("path", "") or kwargs.get("args", "")
    try:
        p = _read_path(path)
        if p.stat().st_size > _MAX_READ_SIZE:
            return f"❌ File too large to read: {p.stat().st_size / 1024 / 1024:.1f} MB (limit 10 MB)"
        return p.read_text(encoding="utf-8")
    except Exception as e:
        return f"Error: {e}"


def read(path: str) -> str:
    """Alias for cat(). Read and return file contents (max 10 MB)."""
    return cat(path)


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
    """Return SHA-256 hash of a file (streamed, no size limit)"""
    try:
        p = _read_path(path)
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        return f"Error: {e}"


# ── WRITE OPS ────────────────────────────────────────────────────────────────

def write(path: str = None, content: str = None, *, name: str = None) -> str:
    """Write content to a file (path relative to /app/memory/ if not absolute)"""
    # Accept 'name' as alias for 'path' (LLM sometimes uses write(name=..., content=...))
    if path is None and name is not None:
        path = name
    if not path:
        return "Error: write() requires a 'path' argument"
    if content is None:
        return "Error: write() requires a 'content' argument"
    try:
        p = _write_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=p.parent, encoding="utf-8", delete=False, suffix=".tmp") as tf:
            tf.write(content)
            tmp = Path(tf.name)
        try:
            tmp.replace(p)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        return f"✅ Wrote {p} ({p.stat().st_size} bytes)"
    except Exception as e:
        return f"Error: {e}"


def append(path: str, content: str) -> str:
    """Append content to a file"""
    try:
        p = _write_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        existing = p.read_text(encoding="utf-8") if p.exists() else ""
        updated = existing + content
        with tempfile.NamedTemporaryFile("w", dir=p.parent, encoding="utf-8", delete=False, suffix=".tmp") as tf:
            tf.write(updated)
            tmp = Path(tf.name)
        try:
            tmp.replace(p)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
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
            snippet = old_text[:40] + ("..." if len(old_text) > 40 else "")
            return f"❌ Text not found in {path}:\n  '{snippet}'"
        updated = content.replace(old_text, new_text, 1)
        with tempfile.NamedTemporaryFile("w", dir=p.parent, encoding="utf-8", delete=False, suffix=".tmp") as tf:
            tf.write(updated)
            tmp = Path(tf.name)
        try:
            tmp.replace(p)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
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
            snippet = old_text[:40] + ("..." if len(old_text) > 40 else "")
            return f"❌ Text not found in {path}:\n  '{snippet}'"
        updated = content.replace(old_text, new_text)
        with tempfile.NamedTemporaryFile("w", dir=p.parent, encoding="utf-8", delete=False, suffix=".tmp") as tf:
            tf.write(updated)
            tmp = Path(tf.name)
        try:
            tmp.replace(p)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
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
    """Delete a file or directory within allowed write roots"""
    try:
        p = _write_path(path)
        if not p.exists():
            return f"Not found: {path}"
        if p.is_dir():
            shutil.rmtree(p)
        else:
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

        largest_path = Path(largest[0])
        largest_display = (
            f"(name redacted) ({largest[1]} bytes)"
            if _is_sensitive(largest_path)
            else f"{largest[0]} ({largest[1]} bytes)"
        )
        return (
            f"📂 {p}\n"
            f"  Files: {total_files}  Dirs: {total_dirs}  "
            f"Size: {total_size / 1024 / 1024:.2f} MB\n\n"
            f"File types:\n{type_lines}\n\n"
            f"Largest: {largest_display}"
        )
    except Exception as e:
        return f"Error: {e}"


# ── CHECKPOINT / ROLLBACK ────────────────────────────────────────────────────

def checkpoint(path: str, label: str = "") -> str:
    """
    Snapshot a file or directory before destructive operations.
    Saves to /app/memory/checkpoints/<id>/ with a manifest.
    Returns the checkpoint_id to pass to restore() if needed.
    Call this before: delete, patch, write (overwrite), or any bulk file change.
    """
    try:
        src = _read_path(path)
        if not src.exists():
            return f"❌ Cannot checkpoint — path not found: {path}"

        checkpoint_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
        dest_dir = _CHECKPOINT_ROOT / checkpoint_id
        dest_dir.mkdir(parents=True, exist_ok=True)

        dest = dest_dir / src.name
        if src.is_dir():
            shutil.copytree(src, dest)
            size_bytes = sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
        else:
            shutil.copy2(src, dest)
            size_bytes = dest.stat().st_size

        manifest = {
            "checkpoint_id": checkpoint_id,
            "created": datetime.now().isoformat(),
            "label": label or "(no label)",
            "original_path": str(src),
            "is_dir": src.is_dir(),
            "size_bytes": size_bytes,
        }
        (dest_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        return (
            f"✅ Checkpoint saved: {checkpoint_id}\n"
            f"   Source : {src}\n"
            f"   Label  : {manifest['label']}\n"
            f"   Size   : {size_bytes / 1024:.1f} KB\n"
            f"   Restore: files.restore('{checkpoint_id}')"
        )
    except Exception as e:
        return f"❌ Checkpoint failed: {e}"


def restore(checkpoint_id: str) -> str:
    """
    Restore a file or directory from a previously saved checkpoint.
    The original_path in the manifest must be within an allowed write root
    (/app/memory/ or /app/skills/dynamic/).
    """
    try:
        checkpoint_dir = _CHECKPOINT_ROOT / checkpoint_id
        if not checkpoint_dir.exists():
            return f"❌ Checkpoint not found: {checkpoint_id}"

        manifest_path = checkpoint_dir / "manifest.json"
        if not manifest_path.exists():
            return f"❌ Checkpoint manifest missing in: {checkpoint_id}"

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        original_path = manifest["original_path"]

        # Enforce write-root safety on restore target
        dest = _write_path(original_path)

        # Find the backed-up file/dir using the original filename from the manifest
        original_name = Path(original_path).name
        src = checkpoint_dir / original_name
        if not src.exists():
            # Fallback: pick the sole non-manifest entry if name changed
            contents = [p for p in checkpoint_dir.iterdir() if p.name != "manifest.json"]
            if not contents:
                return f"❌ Checkpoint directory is empty: {checkpoint_id}"
            if len(contents) > 1:
                return f"❌ Ambiguous checkpoint contents (expected 1 entry, found {len(contents)}): {checkpoint_id}"
            src = contents[0]

        if dest.exists():
            if dest.is_dir():
                shutil.rmtree(dest)
            else:
                dest.unlink()

        if src.is_dir():
            shutil.copytree(src, dest)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)

        return (
            f"✅ Restored from checkpoint: {checkpoint_id}\n"
            f"   Label    : {manifest.get('label', '(no label)')}\n"
            f"   Saved at : {manifest.get('created', '?')}\n"
            f"   Restored : {dest}"
        )
    except ValueError as e:
        return f"❌ Restore blocked — target is outside allowed write roots: {e}"
    except Exception as e:
        return f"❌ Restore failed: {e}"


def checkpoints(limit: int = 50) -> str:
    """List saved checkpoints with their labels, dates, and source paths (newest first, default limit 50)."""
    try:
        if not _CHECKPOINT_ROOT.exists():
            return "No checkpoints found."

        entries = sorted(
            [d for d in _CHECKPOINT_ROOT.iterdir() if d.is_dir()],
            reverse=True,
        )
        if not entries:
            return "No checkpoints found."

        total = len(entries)
        entries = entries[:limit]

        lines = [f"{'ID':<30}  {'Created':<20}  {'Label':<25}  Source"]
        lines.append("─" * 100)
        for entry in entries:
            manifest_path = entry / "manifest.json"
            if not manifest_path.exists():
                continue
            try:
                m = json.loads(manifest_path.read_text(encoding="utf-8"))
                created = m.get("created", "")[:19]
                label = m.get("label", "(no label)")[:25]
                source = m.get("original_path", "?")
                lines.append(f"{entry.name:<30}  {created:<20}  {label:<25}  {source}")
            except Exception:
                lines.append(f"{entry.name:<30}  (unreadable manifest)")

        if total > limit:
            lines.append(f"\n  (showing {limit} of {total} — pass limit=N to see more)")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Could not list checkpoints: {e}"


def find_duplicates(path: str) -> str:
    """Find duplicate files in a directory by comparing SHA-256 hashes"""
    try:
        p = _read_path(path)
        if not p.exists():
            return f"Not found: {path}"

        _MAX_HASH_SIZE = 100 * 1024 * 1024  # skip files over 100 MB
        hashes: dict = {}
        skipped = 0
        for item in p.rglob("*"):
            if item.is_file():
                try:
                    if item.stat().st_size > _MAX_HASH_SIZE:
                        skipped += 1
                        continue
                    h = hashlib.sha256(item.read_bytes()).hexdigest()
                    hashes.setdefault(h, []).append(str(item))
                except Exception as e:
                    logger.warning(f"Could not hash {item}: {e}")
                    skipped += 1
                    continue

        dupes = {h: files for h, files in hashes.items() if len(files) > 1}
        result_lines = []
        if not dupes:
            result_lines.append("✅ No duplicate files found")
        else:
            result_lines.append(f"Found {len(dupes)} set(s) of duplicates:\n")
            for h, files in dupes.items():
                result_lines.append(f"  {h[:8]}...")
                for f in files:
                    result_lines.append(f"    {f}")
        if skipped:
            result_lines.append(f"\n  ({skipped} file(s) skipped — unreadable or over 100 MB)")
        return "\n".join(result_lines)
    except Exception as e:
        return f"Error: {e}"
