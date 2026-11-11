"""TrinityClaw one-click updater — tiny HTTP sidecar.

POST /update   (X-API-Key header required, must match TRINITY_API_KEY from .env)
    Runs update.sh: git pull on the host repo + recreate agent/web containers.
POST /restart  (same auth) — runs restart.sh: recreates litellm + trinity-agent
    so .env / litellm_config.yaml changes made from the web UI take effect.
GET  /health  — no auth, lets the UI detect the updater's availability.

Stdlib only. Binds to 127.0.0.1 by default (compose publishes it as
'127.0.0.1:8002:8002') — this service can write the install folder and
talks to the Docker socket, so it must never be exposed to the network.
"""
import hmac
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

UPDATE_SCRIPT = os.getenv("UPDATE_SCRIPT", "/app/update.sh")
RESTART_SCRIPT = os.getenv("RESTART_SCRIPT", "/app/restart.sh")
UPDATE_TIMEOUT = int(os.getenv("UPDATE_TIMEOUT", "900"))
RESTART_TIMEOUT = int(os.getenv("RESTART_TIMEOUT", "300"))

_UPDATE_LOCK = threading.Lock()
_RESTART_LOCK = threading.Lock()


def check_auth(provided: str) -> bool:
    """True only when the key matches TRINITY_API_KEY (never when unset)."""
    expected = os.getenv("TRINITY_API_KEY", "")
    if not expected:
        return False
    return hmac.compare_digest(provided or "", expected)


def run_script(script: str, timeout: int):
    """Run a shell script, return (ok, combined_output)."""
    try:
        proc = subprocess.run(
            ["sh", script],
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode == 0, (proc.stdout + proc.stderr).strip()
    except subprocess.TimeoutExpired:
        return False, f"Script timed out after {timeout}s — check container logs."
    except Exception as e:
        return False, f"Script failed to start: {e}"


def run_update():
    """Run update.sh, return (ok, combined_output)."""
    return run_script(UPDATE_SCRIPT, UPDATE_TIMEOUT)


def run_restart():
    """Run restart.sh, return (ok, combined_output)."""
    return run_script(RESTART_SCRIPT, RESTART_TIMEOUT)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "X-API-Key, Content-Type")

    def _respond(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {"status": "ok", "service": "trinity-updater"})
        else:
            self._respond(404, {"error": "not found"})

    def do_POST(self):
        if self.path not in ("/update", "/restart"):
            self._respond(404, {"error": "not found"})
            return
        if not check_auth(self.headers.get("X-API-Key")):
            self._respond(401, {"ok": False, "output": "Invalid or missing X-API-Key header"})
            return
        lock = _UPDATE_LOCK if self.path == "/update" else _RESTART_LOCK
        action = "update" if self.path == "/update" else "restart"
        article = "An" if action == "update" else "A"
        if not lock.acquire(blocking=False):
            self._respond(409, {"ok": False, "output": f"{article} {action} is already in progress."})
            return
        try:
            ok, output = run_update() if action == "update" else run_restart()
            self._respond(200 if ok else 500, {"ok": ok, "output": output})
        finally:
            lock.release()

    def log_message(self, fmt, *args):
        print(f"[updater] {self.address_string()} {fmt % args}")


def build_server(port: int):
    """Create (but don't start) the server — exposed for tests.
    Returns (httpd, actual_port) so port=0 resolves to the real bound port."""
    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    return httpd, httpd.server_address[1]


if __name__ == "__main__":
    port = int(os.getenv("UPDATER_PORT", "8002"))
    httpd, bound = build_server(port)
    print(f"[updater] listening on 0.0.0.0:{bound}")
    httpd.serve_forever()
