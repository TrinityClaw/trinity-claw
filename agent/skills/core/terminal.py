import subprocess
import shlex

NAME = "terminal"
DOC = (
    "Execute whitelisted shell commands (ls, git, touch, cat) inside the container. "
    "Returns: run(command)→stdout/stderr text output of the command, or rejection message if "
    "the command is not whitelisted or contains dangerous characters."
)

# 🛡️ ONLY these base commands are allowed
# NOTE: 'pip' removed — allows arbitrary package installs which breaks sandbox isolation
WHITELISTED = ["ls", "git", "touch", "cat"]

def run(command: str) -> str:
    """Executes a command inside the container if it passes security checks."""
    try:
        # 1. Block dangerous shell chaining (no ; && | etc)
        forbidden = [";", "&", "|", ">", "<", "$", "(", ")", "`", "\n"]
        if any(char in command for char in forbidden):
            return "❌ REJECTED: Dangerous characters detected."

        # 2. Parse into a list (safer than raw strings)
        args = shlex.split(command)
        if not args:
            return "❌ REJECTED: Empty command."

        # 3. Strict Whitelist: Must START with an approved command
        if args[0] not in WHITELISTED:
            return f"❌ REJECTED: '{args[0]}' is not whitelisted."

        # 4. Execute (No shell=True for maximum security)
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=30
        )

        output = result.stdout if result.returncode == 0 else result.stderr
        return f"✅ Success (Code {result.returncode}):\n{output}"

    except subprocess.TimeoutExpired:
        return "⌛ ERROR: Command timed out."
    except Exception as e:
        return f"❌ SYSTEM ERROR: {str(e)}"