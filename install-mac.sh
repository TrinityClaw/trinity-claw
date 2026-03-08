#!/bin/bash
# Auto-fix Windows CRLF
if LC_ALL=C grep -q $'\r' "$0"; then
    tmp=$(mktemp); tr -d '\r' < "$0" > "$tmp"; chmod +x "$tmp"; exec bash "$tmp" "$@"
fi

echo ""
echo -e "\033[32m╔══════════════════════════════════════════════════════╗"
echo -e "║     TrinityClaw — Mac Prerequisites Setup            ║"
echo -e "╚══════════════════════════════════════════════════════╝\033[0m"
echo ""

# ── Step 1: Xcode Command Line Tools ─────────────────────
if xcode-select -p &>/dev/null; then
    echo "   ✅ Xcode Command Line Tools already installed"
else
    echo "   📥 Installing Xcode Command Line Tools..."
    echo "   👉 A dialog box will appear on screen — click INSTALL"
    echo "   ⏳ Waiting for you to complete the installation..."
    echo ""
    xcode-select --install 2>/dev/null || true
    # Wait until CLT is installed
    until xcode-select -p &>/dev/null; do
        sleep 5
        printf "."
    done
    echo ""
    echo "   ✅ Xcode Command Line Tools installed"
fi

# ── Step 2: Homebrew ─────────────────────────────────────
if [ -f /opt/homebrew/bin/brew ]; then
    eval "$(/opt/homebrew/bin/brew shellenv)"
elif [ -f /usr/local/bin/brew ]; then
    eval "$(/usr/local/bin/brew shellenv)"
fi

if command -v brew &>/dev/null; then
    echo "   ✅ Homebrew already installed"
else
    echo "   📥 Installing Homebrew (you will be asked for your Mac password)..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Reload PATH after install
    if [ -f /opt/homebrew/bin/brew ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [ -f /usr/local/bin/brew ]; then
        eval "$(/usr/local/bin/brew shellenv)"
    fi
    if ! command -v brew &>/dev/null; then
        echo ""
        echo "   ❌ Homebrew install failed."
        echo "   👉 Run this manually in a new terminal, then re-run this script:"
        echo "      /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
        exit 1
    fi
    echo "   ✅ Homebrew installed"
fi

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
hash -r

# ── Step 3: Colima + Docker CLI ───────────────────────────
if command -v colima &>/dev/null && command -v docker &>/dev/null; then
    echo "   ✅ Colima + Docker already installed"
else
    echo "   📥 Installing Colima + Docker CLI..."
    brew install colima docker docker-compose
    export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
    hash -r
    echo "   ✅ Colima + Docker installed"
fi

# ── Step 4: Start Colima ──────────────────────────────────
if docker info &>/dev/null 2>&1; then
    echo "   ✅ Docker is already running"
else
    echo "   🚀 Starting Colima (Docker runtime)..."
    colima start --cpu 2 --memory 4
    if ! docker info &>/dev/null 2>&1; then
        echo "   ❌ Colima failed to start. Try: colima start"
        exit 1
    fi
    echo "   ✅ Docker is running"
fi

# ── Done ──────────────────────────────────────────────────
echo ""
echo -e "\033[32m✅ Mac prerequisites ready!\033[0m"
echo ""
echo "   Now run the main installer:"
echo "   bash install.sh"
echo ""
