"""
Shared low-level utilities for the store modules.
Not a skill — imported by _journal_store.py, _user_model_store.py, and notes.py.
"""
import fcntl
from contextlib import contextmanager
from pathlib import Path


def _trunc(s, n: int) -> str:
    return str(s)[:n]


@contextmanager
def _file_lock(path: Path):
    lock_path = path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _atomic_write(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding=encoding)
    tmp.replace(path)
