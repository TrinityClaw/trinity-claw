#!/bin/bash
# Auto-fix Windows CRLF line endings
if LC_ALL=C grep -q $'\r' "$0"; then
    tmp=$(mktemp); tr -d '\r' < "$0" > "$tmp"; chmod +x "$tmp"; exec bash "$tmp" "$@"
fi

echo ""
echo "╔══════════════════════════════════════════════════════╗"
echo "║     TrinityClaw — Mac Setup (Step 1 of 2)           ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""
echo "   This script installs Docker Desktop — the only"
echo "   prerequisite for running TrinityClaw."
echo ""

# ─────────────────────────────────────────────────────────
# Check if Docker is already running
# ─────────────────────────────────────────────────────────
if docker info &>/dev/null 2>&1; then
    echo "   ✅ Docker Desktop is already installed and running."
    echo ""
    echo "════════════════════════════════════════════════════════"
    echo "   ✅ Ready! Now run Step 2:"
    echo ""
    echo "      bash install.sh"
    echo ""
    echo "════════════════════════════════════════════════════════"
    echo ""
    exit 0
fi

# ─────────────────────────────────────────────────────────
# Docker Desktop is installed but not running — start it
# ─────────────────────────────────────────────────────────
if [ -d "/Applications/Docker.app" ]; then
    echo "   ✅ Docker Desktop is installed but not running."
    echo "   🚀 Starting Docker Desktop..."
    open /Applications/Docker.app

    echo ""
    echo "   ⏳ Waiting for Docker to start (up to 90 seconds)..."
    echo "      Watch for the whale 🐳 icon to appear in your Mac menu bar."
    echo ""

    for i in $(seq 1 45); do
        sleep 2
        if docker info &>/dev/null 2>&1; then
            echo "   ✅ Docker Desktop is running!"
            break
        fi
        printf "   [%02d/45] still starting...\r" "$i"
    done
    echo ""

    if ! docker info &>/dev/null 2>&1; then
        echo ""
        echo "   ❌ Docker did not start in time."
        echo "   👉 Open Docker Desktop manually from your Applications folder."
        echo "      Wait for the whale 🐳 icon in the menu bar, then run:"
        echo ""
        echo "      bash install.sh"
        echo ""
        exit 1
    fi

    echo ""
    echo "════════════════════════════════════════════════════════"
    echo "   ✅ Ready! Now run Step 2:"
    echo ""
    echo "      bash install.sh"
    echo ""
    echo "════════════════════════════════════════════════════════"
    echo ""
    exit 0
fi

# ─────────────────────────────────────────────────────────
# Docker Desktop is NOT installed — guide the user
# ─────────────────────────────────────────────────────────
echo "   ❌ Docker Desktop is not installed."
echo ""
echo "════════════════════════════════════════════════════════"
echo "   📥  INSTALL DOCKER DESKTOP (it's free)"
echo ""
echo "   1. Go to:  https://www.docker.com/products/docker-desktop/"
echo "   2. Click 'Download for Mac'"
echo "   3. Open the .dmg file → drag Docker to Applications"
echo "   4. Open Docker Desktop from Applications"
echo "   5. Wait for the whale 🐳 icon in your menu bar"
echo "   6. Come back here and run:"
echo ""
echo "         bash install-mac.sh"
echo ""
echo "════════════════════════════════════════════════════════"
echo ""

# Try to open the download page automatically
open "https://www.docker.com/products/docker-desktop/" 2>/dev/null || true

exit 1
