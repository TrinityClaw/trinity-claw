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
if docker compose --project-name "$PROJECT" --project-directory "$REPO_DIR" \
        -f "$REPO_DIR/$CFG_NAME" up -d $SERVICES; then
    log "Restart complete — new settings are live."
    exit 0
fi

die "docker compose up failed — run 'docker compose up -d litellm trinity-agent' manually in the install folder."
