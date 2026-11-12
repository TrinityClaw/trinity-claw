#!/bin/sh
# Restart core services so .env / litellm config changes take effect.
# Runs inside the updater container (same trust model as update.sh).
#
# Uses `up -d` (recreate), never `docker restart`: env_file values and the
# mounted litellm_config.yaml are only re-read when containers are recreated.
#
# Contract (mirrors update.sh):
#   REPO_DIR      — mount point of the host repo (default /repo)
#   docker must be on PATH (real docker CLI in production, stub in tests)
set -u

REPO_DIR="${REPO_DIR:-/repo}"
SERVICES="${RESTART_SERVICES:-litellm trinity-agent}"

log() { echo "[updater] $*"; }
die() { echo "[updater] ERROR: $*" >&2; exit 1; }

# ── 0. Resolve the HOST path of the repo (bind source of our /repo mount) ────
# docker compose resolves the compose file's RELATIVE bind mounts against
# --project-directory and passes those absolute paths to the HOST daemon.
# /repo exists only inside this container, so running compose with
# --project-directory /repo makes the daemon look for /repo/agent/app.py on
# the host — which does not exist ("error mounting /repo/agent/app.py ...
# not a directory"). Fix: read the bind-mount SOURCE (the real host path,
# e.g. /data/home/trinity-claw), symlink it to /repo here so the CLI can
# read the files, and use it as the project directory.
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

# ── 1. Find the RUNNING compose project (never create a new one) ─────────────
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

# ── 2. Recreate the services that read .env / litellm config ─────────────────
log "Recreating $SERVICES (project: $PROJECT, compose file: $CFG_NAME)..."
if docker compose --project-name "$PROJECT" --project-directory "$HOST_DIR" \
        -f "$HOST_DIR/$CFG_NAME" up -d $SERVICES; then
    log "Restart complete — new settings are live."
else
    die "docker compose up failed — run 'docker compose up -d litellm trinity-agent' manually in the install folder."
fi

# ── 3. Bring up Ollama when local mode is configured ─────────────────────────
# The ollama service lives in the 'local' compose profile, so it is not part of
# a normal 'up -d'. Only start it when the user actually switched to local mode.
# On Mac, Ollama runs natively (brew services), not in Docker — skip there.
if [ "$CFG_NAME" = "docker-compose.yml" ] \
   && grep -q '^MODEL_SOURCE=local' "$REPO_DIR/.env" 2>/dev/null; then
    log "Local mode configured — starting Ollama container..."
    if docker compose --profile local \
            --project-name "$PROJECT" --project-directory "$HOST_DIR" \
            -f "$HOST_DIR/$CFG_NAME" up -d ollama; then
        log "Ollama is running."
    else
        log "Ollama start failed — run: docker compose --profile local up -d ollama"
    fi
fi
