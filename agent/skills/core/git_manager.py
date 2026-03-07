# git_manager.py
import subprocess
from pathlib import Path

NAME = "git_manager"
DOC = "Git repository management: status, branches, diff, stage, commit, pull, push, log"

def status(repo_path: str = "/app") -> str:
    git_dir = Path(repo_path) / ".git"
    if not git_dir.exists():
        return f"Not a Git repository: {repo_path}"
    info = [f"Repository: {repo_path}"]
    head_file = git_dir / "HEAD"
    if head_file.exists():
        head_content = head_file.read_text().strip()
        if head_content.startswith("ref:"):
            branch = head_content.split("/")[-1]
            info.append(f"Branch: {branch}")
    return "\n".join(info)

def is_repo(path: str = "/app") -> str:
    git_dir = Path(path) / ".git"
    return "Git repository" if git_dir.exists() else "Not a Git repository"

def list_repos(base_path: str = "/app") -> str:
    base = Path(base_path)
    repos = [str(g.parent) for g in base.rglob(".git")]
    if not repos:
        return f"No repositories in {base_path}"
    return f"Found {len(repos)}:\n" + "\n".join(repos)

def branches(repo_path: str = "/app") -> str:
    git_dir = Path(repo_path) / ".git"
    if not git_dir.exists():
        return f"Not a Git repository: {repo_path}"
    heads_dir = git_dir / "refs" / "heads"
    if not heads_dir.exists():
        return "No branches"
    names = [str(b.relative_to(heads_dir)) for b in heads_dir.rglob("*") if b.is_file()]
    return "Branches:\n" + "\n".join([f"  - {n}" for n in names])

def head(repo_path: str = "/app") -> str:
    git_dir = Path(repo_path) / ".git"
    if not git_dir.exists():
        return f"Not a Git repository: {repo_path}"
    head_file = git_dir / "HEAD"
    return head_file.read_text().strip() if head_file.exists() else "No HEAD"

def _run(args: list, repo_path: str, timeout: int = 30) -> str:
    """Run a git command in repo_path and return output or error."""
    try:
        result = subprocess.run(
            ["git"] + args,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        out = result.stdout.strip()
        err = result.stderr.strip()
        if result.returncode != 0:
            return f"❌ git error:\n{err or out}"
        return out or "✅ Done"
    except subprocess.TimeoutExpired:
        return f"❌ git command timed out after {timeout}s"
    except FileNotFoundError:
        return "❌ git not found in PATH"
    except Exception as e:
        return f"❌ Error: {e}"

def diff(repo_path: str = "/app", staged: str = "false") -> str:
    """Show unstaged changes, or staged changes if staged='true'."""
    if not (Path(repo_path) / ".git").exists():
        return f"Not a Git repository: {repo_path}"
    args = ["diff"] if staged.lower() != "true" else ["diff", "--staged"]
    result = _run(args, repo_path)
    return result if result.strip() else "✅ No changes"

def log(repo_path: str = "/app", n: str = "10") -> str:
    """Show last N commits with hash, author, date, and message."""
    if not (Path(repo_path) / ".git").exists():
        return f"Not a Git repository: {repo_path}"
    return _run(["log", f"-{n}", "--pretty=format:%h  %an  %ar  %s"], repo_path)

def add(repo_path: str = "/app", files: str = ".") -> str:
    """Stage files for commit. files can be '.' for all, or space-separated paths."""
    if not (Path(repo_path) / ".git").exists():
        return f"Not a Git repository: {repo_path}"
    file_list = files.split() if files != "." else ["."]
    return _run(["add"] + file_list, repo_path)

def commit(repo_path: str = "/app", message: str = "Update") -> str:
    """Commit staged changes with a message."""
    if not (Path(repo_path) / ".git").exists():
        return f"Not a Git repository: {repo_path}"
    if not message.strip():
        return "❌ Commit message cannot be empty"
    return _run(["commit", "-m", message], repo_path)

def pull(repo_path: str = "/app", remote: str = "origin", branch: str = "") -> str:
    """Pull latest changes from remote. Optionally specify branch."""
    if not (Path(repo_path) / ".git").exists():
        return f"Not a Git repository: {repo_path}"
    args = ["pull", remote] + ([branch] if branch else [])
    return _run(args, repo_path, timeout=60)

def push(repo_path: str = "/app", remote: str = "origin", branch: str = "") -> str:
    """Push committed changes to remote. Optionally specify branch."""
    if not (Path(repo_path) / ".git").exists():
        return f"Not a Git repository: {repo_path}"
    args = ["push", remote] + ([branch] if branch else [])
    return _run(args, repo_path, timeout=60)