#!/bin/bash
# Auto-fix Windows CRLF line endings (skip when piped via curl)
if [ -f "$0" ] && LC_ALL=C grep -q $'\r' "$0"; then
    tmp=$(mktemp); tr -d '\r' < "$0" > "$tmp"; chmod +x "$tmp"; exec bash "$tmp" "$@"
fi

# install.sh - TrinityClaw Installation Wizard (Linux/Mac)

echo ""
echo -e "\033[32m╔══════════════════════════════════════════════════════╗"
echo -e "║        TrinityClaw AI Agent - Installation Wizard    ║"
echo -e "╚══════════════════════════════════════════════════════╝\033[0m"
echo ""

# ─────────────────────────────────────────────────────────
# Detect platform
# ─────────────────────────────────────────────────────────
IS_MAC=false
if [[ "$(uname)" == "Darwin" ]]; then
    IS_MAC=true
    echo -e "   \033[36m🍎 macOS detected\033[0m"
    # Ensure brew bins are in PATH (set up by install-mac.sh)
    if [ -f /opt/homebrew/bin/brew ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [ -f /usr/local/bin/brew ]; then
        eval "$(/usr/local/bin/brew shellenv)"
    fi
    export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
    hash -r
fi

# ─────────────────────────────────────────────────────────
# [1/5] Check prerequisites
# ─────────────────────────────────────────────────────────
echo -e "\033[33m[1/5] Checking prerequisites...\033[0m"

# On Mac: remind user to run install-mac.sh first if Docker missing
if [ "$IS_MAC" = true ]; then
    if ! docker info &>/dev/null 2>&1; then
        # Try to start Docker Desktop if the app is installed
        if [ -d "/Applications/Docker.app" ]; then
            echo "   🚀 Starting Docker Desktop..."
            open /Applications/Docker.app
            echo "   ⏳ Waiting for Docker to start..."
            for i in $(seq 1 30); do
                sleep 2
                docker info &>/dev/null 2>&1 && break
                printf "."
            done
            echo ""
        fi
        if ! docker info &>/dev/null 2>&1; then
            echo ""
            echo "   ❌ Docker Desktop is not running."
            echo "   👉 Run this first: bash install-mac.sh"
            echo ""
            exit 1
        fi
    fi

# On Windows (Git Bash)
elif grep -qi microsoft /proc/version 2>/dev/null; then
    if ! command -v docker &>/dev/null; then
        echo "   ❌ Docker Desktop is not installed."
        echo "   👉 Download from: https://www.docker.com/products/docker-desktop/"
        echo "   Then restart Git Bash and re-run this script."
        exit 1
    fi

# On Linux
else
    if ! command -v docker &>/dev/null; then
        echo "   📥 Installing Docker Engine..."
        curl -fsSL https://get.docker.com | sh
        sudo systemctl enable --now docker
        sudo usermod -aG docker "$USER"
        newgrp docker
    fi
fi

echo "   ✅ Docker installed"

# Find docker-compose — check plugin, standalone, and known brew paths
DC=""
if docker compose version &>/dev/null 2>&1; then
    DC="docker compose"
elif command -v docker-compose &>/dev/null; then
    DC="docker-compose"
elif [ -f /opt/homebrew/bin/docker-compose ]; then
    DC="/opt/homebrew/bin/docker-compose"
elif [ -f /usr/local/bin/docker-compose ]; then
    DC="/usr/local/bin/docker-compose"
fi

# On Mac: install if still not found, then link as plugin
if [ -z "$DC" ] && [ "$IS_MAC" = true ]; then
    echo "   📥 Installing docker-compose..."
    brew install docker-compose
    export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
    hash -r
    DC="/opt/homebrew/bin/docker-compose"
fi

if [ -z "$DC" ]; then
    echo "   ❌ Docker Compose not found."
    exit 1
fi

# Link as Docker CLI plugin if not already working
if ! docker compose version &>/dev/null 2>&1; then
    mkdir -p ~/.docker/cli-plugins
    COMPOSE_PATH="${DC##* }"  # get binary path (strip "docker " if present)
    [ -f "$COMPOSE_PATH" ] && ln -sfn "$COMPOSE_PATH" ~/.docker/cli-plugins/docker-compose 2>/dev/null || true
fi

echo "   ✅ Docker Compose ready"

# ─────────────────────────────────────────────────────────
# [1b/5] Download repository if not already present
# ─────────────────────────────────────────────────────────
if [ ! -f "./docker-compose.yml" ] && [ ! -f "./docker-compose.mac.yml" ]; then
    INSTALL_DIR="$HOME/trinity-claw"
    echo ""
    echo "   📥 Downloading TrinityClaw files to $INSTALL_DIR..."
    mkdir -p "$INSTALL_DIR"
    if curl -fsSL https://github.com/TrinityClaw/trinity-claw/archive/refs/heads/main.tar.gz \
        | tar -xz -C "$INSTALL_DIR" --strip-components=1; then
        echo "   ✅ Files ready at $INSTALL_DIR"
    else
        echo "   ❌ Download failed. Check your internet and try again."
        exit 1
    fi
    cd "$INSTALL_DIR"
    echo "   ✅ Working in $INSTALL_DIR"
fi

# ─────────────────────────────────────────────────────────
# [2/5] Choose model source
# ─────────────────────────────────────────────────────────
echo ""
echo -e "\033[33m[2/5] Model source...\033[0m"
echo ""
echo "   Options:"
echo "   [cloud]  Use a cloud provider (OpenAI, NVIDIA, Anthropic, etc.)"
echo "   [local]  Use a local Ollama model (qwen3.5:9b, ~6.6GB, no API key needed)"
echo ""
read -p "   Choose model source [cloud/local]: " model_source
model_source=$(echo "$model_source" | tr '[:upper:]' '[:lower:]' | xargs)

# ─────────────────────────────────────────────────────────
# [3/5] Configure environment
# ─────────────────────────────────────────────────────────
echo ""
echo -e "\033[33m[3/5] Configuring environment...\033[0m"

two_key=$(python3 -c "import secrets; print(secrets.token_hex(16))" 2>/dev/null)
[ -z "$two_key" ] && two_key=$(LC_ALL=C tr -dc 'a-zA-Z0-9' < /dev/urandom | head -c 32)

if [ "$model_source" = "local" ]; then

    echo ""
    echo -e "   \033[32m✅ Local mode selected — Ollama will be used.\033[0m"

    if [ "$IS_MAC" = true ]; then
        echo -e "   \033[36mℹ️  On Mac, Ollama runs natively for full Metal GPU acceleration.\033[0m"
        echo ""

        if ! command -v ollama &>/dev/null; then
            echo "   📥 Installing Ollama..."
            brew install ollama
            export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
            hash -r
        else
            echo "   ✅ Ollama already installed"
        fi

        echo "   📥 Pulling qwen3.5:9b (~6.6GB, this may take several minutes)..."
        ollama pull qwen3.5:9b

        if ! curl -s http://localhost:11434/api/tags >/dev/null 2>&1; then
            echo "   🚀 Starting Ollama service..."
            ollama serve &>/dev/null &
            sleep 3
        else
            echo "   ✅ Ollama already running"
        fi

        OLLAMA_API_BASE="http://host.docker.internal:11434"
        COMPOSE_FILE="docker-compose.mac.yml"
    else
        echo -e "   \033[33mℹ️  The model 'qwen3.5:9b' (~6.6GB) will be pulled on first startup.\033[0m"
        OLLAMA_API_BASE="http://ollama:11434"
        COMPOSE_FILE="docker-compose.yml"
    fi

    cat > .env << EOF
# TrinityClaw Secrets
LITELLM_MASTER_KEY=sk-trinity-local-key
MODEL_SOURCE=local
OLLAMA_MODEL=qwen3.5:9b
TRINITY_API_KEY=${two_key}
EOF

    mkdir -p config
    cat > config/litellm_config.yaml << EOF
model_list:
  - model_name: trinity-default
    litellm_params:
      model: ollama/qwen3.5:9b
      api_base: ${OLLAMA_API_BASE}

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
EOF

    echo "   ✅ Local configuration created!"
    LOCAL_MODE=true

else

    echo ""
    echo -e "   \033[36mCloud API Configuration\033[0m"
    echo ""
    echo "   Provider examples:"
    echo "   - OpenAI:     openai/gpt-4o"
    echo "   - NVIDIA:     openai/moonshotai/kimi-k2-instruct"
    echo "   - Anthropic:  anthropic/claude-3-5-sonnet-20241022"
    echo ""
    read -p "   1. Model name: " model
    model=${model:-gpt-4o}

    echo ""
    echo "   API Base examples:"
    echo "   - OpenAI:  https://api.openai.com/v1"
    echo "   - NVIDIA:  https://integrate.api.nvidia.com/v1"
    echo ""
    read -p "   2. API Base URL (default: https://api.openai.com/v1): " api_base
    api_base=${api_base:-https://api.openai.com/v1}

    echo ""
    echo "   Example: NVIDIA_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY"
    read -p "   3. API Key variable name: " api_key_name
    api_key_name=${api_key_name:-OPENAI_API_KEY}

    echo ""
    read -s -p "   4. API Key value (hidden): " api_key_value
    echo ""

    cat > .env << EOF
# TrinityClaw Secrets
LITELLM_MASTER_KEY=sk-trinity-local-key
MODEL_SOURCE=cloud
${api_key_name}=${api_key_value}
TRINITY_API_KEY=${two_key}
EOF

    mkdir -p config
    cat > config/litellm_config.yaml << EOF
model_list:
  - model_name: trinity-default
    litellm_params:
      model: ${model}
      api_key: os.environ/${api_key_name}
      api_base: ${api_base}

  - model_name: trinity-vision
    litellm_params:
      model: ${model}
      api_key: os.environ/${api_key_name}
      api_base: ${api_base}

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
EOF

    echo "   ✅ Cloud configuration created!"
    LOCAL_MODE=false
    COMPOSE_FILE="docker-compose.yml"
fi

# ─────────────────────────────────────────────────────────
# [4/5] Building containers
# ─────────────────────────────────────────────────────────
echo ""
echo -e "\033[33m[4/5] Building containers...\033[0m"
$DC -f "$COMPOSE_FILE" build

# ─────────────────────────────────────────────────────────
# [5/5] Starting services
# ─────────────────────────────────────────────────────────
echo ""
echo -e "\033[33m[5/5] Starting services...\033[0m"

if [ "$LOCAL_MODE" = true ] && [ "$IS_MAC" = false ]; then
    $DC -f "$COMPOSE_FILE" --profile local up -d
else
    $DC -f "$COMPOSE_FILE" up -d
fi

# ─────────────────────────────────────────────────────────
# Done
# ─────────────────────────────────────────────────────────
echo ""
echo -e "\033[32m✅ TrinityClaw Installed!\033[0m"
echo ""
echo "   🌐 Web UI:  http://localhost:8080"
echo "   🔌 API:     http://localhost:8001"
echo "   📖 Docs:    http://localhost:8001/docs"
echo ""
# Save the key to a plain text file so it is never lost
echo "${two_key}" > trinity-key.txt

echo "   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "   🔑  AGENT API KEY — copy and save this:"
echo ""
echo "       ${two_key}"
echo ""
echo "   Enter it in: Settings ⚙️ → Agent Security → Agent API Key"
echo "   (also saved to trinity-key.txt in this folder)"
echo "   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

if [ "$LOCAL_MODE" = true ] && [ "$IS_MAC" = false ]; then
    echo -e "   \033[33m⚠️  First startup downloads qwen3.5:9b (~6.6GB).\033[0m"
    echo -e "   \033[33m   Monitor: docker logs trinity-claw-ollama-1 -f\033[0m"
    echo ""
fi

if [ "$IS_MAC" = true ]; then
    echo -e "   \033[36m🍎 Mac: Your agent runs inside Docker Desktop.\033[0m"
    echo -e "   \033[36m       The whale 🐳 icon in the menu bar = Docker is running.\033[0m"
    if [ "$LOCAL_MODE" = true ]; then
        echo -e "   \033[36m       After reboot: open Docker Desktop, then run:\033[0m"
        echo -e "   \033[36m         docker compose -f docker-compose.mac.yml up -d\033[0m"
    else
        echo -e "   \033[36m       After reboot: open Docker Desktop, then run:\033[0m"
        echo -e "   \033[36m         docker compose up -d\033[0m"
    fi
    echo ""
fi

echo -e "   \033[33m🎤 Voice: Whisper (~150MB) downloads on first voice message.\033[0m"
echo -e "   \033[33m📸 Vision: uses trinity-vision model in litellm_config.yaml.\033[0m"
echo -e "   \033[33m🌐 Browser: Playwright + Chromium installed automatically.\033[0m"
echo ""
