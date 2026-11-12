#!/bin/sh
# One-click updater: git pull the host repo, refresh the recorded SHA, and
# recreate the agent + web containers. Runs inside the updater container.
#
# Contract (used by updater/test_update_sh.sh):
#   REPO_DIR      — mount point of the host repo (default /repo)
#   docker must be on PATH (real docker CLI in production, stub in tests)
set -u

REPO_DIR="${REPO_DIR:-/repo}"
SERVICES="trinity-agent web"

log() { echo "[updater] $*"; }
die() { echo "[updater] ERROR: $*" >&2; exit 1; }

# ── 1. The repo must be a git clone (ZIP installs have no history) ───────────
if [ ! -e "$REPO_DIR/.git" ]; then
    die "This installation has no git history (probably downloaded as a ZIP). \
One-click update is unavailable. Reinstall with 'git clone https://github.com/TrinityClaw/trinity-claw.git' \
or download a fresh ZIP and unzip it over this folder (your .env and memory/ are preserved)."
fi

git config --global --add safe.directory "$REPO_DIR" 2>/dev/null

cd "$REPO_DIR" || die "cannot enter $REPO_DIR"

# ── 2. Fast-forward pull; never merge/rebase over local changes ──────────────
log "Fetching latest version..."
git fetch origin --prune || die "could not reach GitHub — check the host's internet connection"
OLD_SHA=$(git rev-parse HEAD 2>/dev/null || echo unknown)
if ! git pull --ff-only 2>&1; then
    die "git pull failed — your local copy has changes that conflict with upstream \
(local changes / diverged history). Update manually: run 'git pull' in the install folder \
and resolve, or reinstall. Nothing was changed or restarted."
fi
NEW_SHA=$(git rev-parse HEAD)
if [ "$NEW_SHA" = "$OLD_SHA" ]; then
    log "Already up to date ($NEW_SHA)."
else
    log "Updated $OLD_SHA -> $NEW_SHA"
fi

# ── 3. Record the new SHA so GET /version reports it after the restart ──────
if grep -q '^TRINITY_GIT_SHA=' .env 2>/dev/null; then
    sed -i "s/^TRINITY_GIT_SHA=.*/TRINITY_GIT_SHA=$NEW_SHA/" .env
else
    printf '\nTRINITY_GIT_SHA=%s\n' "$NEW_SHA" >> .env
fi

# ── 4. Find the RUNNING compose project (never create a new one) ─────────────
AGENT_CTR=$(docker ps --filter label=com.docker.compose.service=trinity-agent --format '{{.Names}}' | head -n 1)
[ -n "$AGENT_CTR" ] || die "no running trinity-agent container found — is the stack up? \
Start it first with 'docker compose up -d' in the install folder."

PROJECT=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$AGENT_CTR" 2>/dev/null)
[ -n "$PROJECT" ] || die "could not read the compose project name from $AGENT_CTR"

CFG_FILES=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$AGENT_CTR" 2>/dev/null)
CFG_NAME=$(printf '%s' "$CFG_FILES" | sed 's/.*[\\/:]//')
if [ -z "$CFG_NAME" ] || [ ! -f "$REPO_DIR/$CFG_NAME" ]; then
    CFG_NAME="docker-compose.yml"
    [ -f "$REPO_DIR/$CFG_NAME" ] || CFG_NAME="docker-compose.mac.yml"
fi

# ── 4b. Resolve the HOST path of the repo (bind source of our /repo mount) ───
# docker compose resolves RELATIVE bind mounts against --project-directory and
# passes the resulting absolute paths to the HOST daemon. /repo exists only
# inside this container, so --project-directory /repo makes the daemon look for
# /repo/agent/app.py on the host — which does not exist ("error mounting
# /repo/agent/app.py ... not a directory"). Fix: read the bind-mount SOURCE
# (the real host path), symlink it to /repo here so the CLI can read the
# files, and use it as the project directory.
HOST_DIR=""
SELF_CTR=$(docker ps --filter label=com.docker.compose.service=updater --format '{{.ID}}' 2>/dev/null | head -n 1)
[ -n "$SELF_CTR" ] || SELF_CTR=$(hostname 2>/dev/null || true)
if [ -n "$SELF_CTR" ]; then
    HOST_DIR=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/repo"}}{{.Source}}{{end}}{{end}}' "$SELF_CTR" 2>/dev/null | head -n 1)
fi
if [ -n "$HOST_DIR" ] && [ "$HOST_DIR" != "$REPO_DIR" ] && [ ! -e "$HOST_DIR" ]; then
    mkdir -p "$(dirname "$HOST_DIR")" 2>/dev/null || true
    ln -s "$REPO_DIR" "$HOST_DIR" 2>/dev/null || true
fi
if [ -z "$HOST_DIR" ] || [ ! -d "$HOST_DIR" ]; then
    HOST_DIR="$REPO_DIR"   # fallback: old behavior (single-host setups only)
fi

# ── 5. Rebuild + recreate agent & web (skips db/chroma/litellm/ollama) ───────
log "Recreating containers (project: $PROJECT, compose file: $CFG_NAME)..."
if docker compose --project-name "$PROJECT" --project-directory "$HOST_DIR" \
        -f "$HOST_DIR/$CFG_NAME" up -d --build $SERVICES; then
    log "Update complete — agent is restarting with the new version."
    exit 0
fi

# ── 6. Fallback: compose unavailable/broken → plain restart ──────────────────
log "docker compose failed — falling back to a plain container restart."
log "(Note: pip/system dependency changes may need a manual 'docker compose up -d --build'.)"
docker restart "$AGENT_CTR" || die "restart of $AGENT_CTR failed — run 'docker compose up -d --build' manually."
log "Update complete — agent restarted with the new code."
