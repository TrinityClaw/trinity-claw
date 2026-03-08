#!/bin/bash
# Auto-fix Windows CRLF line endings — safe to run from any upload method
if LC_ALL=C grep -q $'\r' "$0"; then
    tmp=$(mktemp)
    tr -d '\r' < "$0" > "$tmp"
    chmod +x "$tmp"
    exec bash "$tmp" "$@"
fi

# install.sh - TrinityClaw Installation Wizard (Linux/Mac)

echo ""
echo -e "\033[32m╔══════════════════════════════════════════════════════╗"
echo -e "║        TrinityClaw AI Agent - Installation Wizard    ║"
echo -e "╚══════════════════════════════════════════════════════╝\033[0m"
echo ""

# ─────────────────────────────────────────────────────────
# Helper: ensure Homebrew is installed and in PATH
# ─────────────────────────────────────────────────────────
ensure_brew() {
    if ! command -v brew &>/dev/null; then
        echo -e "   📥 Installing Homebrew..."
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    fi
    # Add to PATH for Apple Silicon AND Intel
    if [ -f /opt/homebrew/bin/brew ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
        export PATH="/opt/homebrew/bin:$PATH"
    elif [ -f /usr/local/bin/brew ]; then
        eval "$(/usr/local/bin/brew shellenv)"
        export PATH="/usr/local/bin:$PATH"
    fi
}

# ─────────────────────────────────────────────────────────
# Detect platform
# ─────────────────────────────────────────────────────────
IS_MAC=false
if [[ "$(uname)" == "Darwin" ]]; then
    IS_MAC=true
    echo -e "   \033[36m🍎 macOS detected\033[0m"
fi

# ─────────────────────────────────────────────────────────
# [1/5] Check prerequisites
# ─────────────────────────────────────────────────────────
echo -e "\033[33m[1/5] Checking prerequisites...\033[0m"

if ! command -v docker &>/dev/null; then
    echo -e "   Docker not found. Installing..."

    if [ "$IS_MAC" = true ]; then
        ensure_brew
        echo -e "   📥 Installing Colima + Docker CLI..."
        brew install colima docker docker-compose
        # Refresh PATH so docker is found immediately
        export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
        echo -e "   🚀 Starting Colima..."
        colima start --cpu 2 --memory 4
        echo -e "   ✅ Docker ready"

    elif grep -qi microsoft /proc/version 2>/dev/null; then
        echo -e "   ℹ️  Windows detected."
        echo -e "   ❌ Docker Desktop is not installed."
        echo -e "   👉 Download from: https://www.docker.com/products/docker-desktop/"
        echo -e "   Then restart Git Bash and re-run this script."
        exit 1

    else
        echo -e "   📥 Installing Docker Engine (Linux)..."
        curl -fsSL https://get.docker.com | sh
        sudo systemctl enable --now docker
        sudo usermod -aG docker "$USER"
        newgrp docker
    fi
fi

# Ensure PATH includes brew bins even if docker was already installed
if [ "$IS_MAC" = true ]; then
    export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
fi

# Start Colima if installed but not running
if command -v colima &>/dev/null && ! docker info &>/dev/null 2>&1; then
    echo -e "   🚀 Starting Colima..."
    colima start --cpu 2 --memory 4
fi

if ! command -v docker &>/dev/null; then
    echo -e "   ❌ Docker install failed. Visit: https://docs.docker.com/get-docker/"
    exit 1
fi
echo "   ✅ Docker installed"

if ! docker compose version &>/dev/null; then
    echo -e "   ❌ Docker Compose not found. Update Docker Desktop or install the plugin."
    exit 1
fi
echo "   ✅ Docker Compose installed"

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

# Generate a secure random agent API key
two_key=$(cat /dev/urandom 2>/dev/null | tr -dc 'a-zA-Z0-9' | fold -w 32 | head -n 1 || \
    python3 -c "import secrets; print(secrets.token_urlsafe(24))")

if [ "$model_source" = "local" ]; then

    # ── LOCAL (Ollama) path ──────────────────────────────
    echo ""
    echo -e "   \033[32m✅ Local mode selected — Ollama will be used.\033[0m"

    if [ "$IS_MAC" = true ]; then
        echo -e "   \033[36mℹ️  On Mac, Ollama runs natively for full Metal GPU acceleration.\033[0m"
        echo ""

        ensure_brew
        if ! command -v ollama &>/dev/null; then
            echo -e "   📥 Installing Ollama..."
            brew install ollama
            export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
        else
            echo "   ✅ Ollama already installed"
        fi

        echo -e "   📥 Pulling qwen3.5:9b (~6.6GB, this may take several minutes)..."
        ollama pull qwen3.5:9b

        if ! curl -s http://localhost:11434/api/tags >/dev/null 2>&1; then
            echo -e "   🚀 Starting Ollama service..."
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

    # ── CLOUD path ───────────────────────────────────────
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
docker compose -f "$COMPOSE_FILE" build

# ─────────────────────────────────────────────────────────
# [5/5] Starting services
# ─────────────────────────────────────────────────────────
echo ""
echo -e "\033[33m[5/5] Starting services...\033[0m"

if [ "$LOCAL_MODE" = true ] && [ "$IS_MAC" = false ]; then
    echo "   Starting with Ollama local model profile (Linux)..."
    docker compose -f "$COMPOSE_FILE" --profile local up -d
else
    docker compose -f "$COMPOSE_FILE" up -d
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
echo -e "   \033[33m─────────────────────────────────────────────────────\033[0m"
echo -e "   \033[33m🔑 Your Agent API Key (save this!):\033[0m"
echo -e "   \033[97m${two_key}\033[0m"
echo    "   Enter it in: Settings ⚙️ → Agent Security → Agent API Key"
echo -e "   \033[33m─────────────────────────────────────────────────────\033[0m"
echo ""

if [ "$LOCAL_MODE" = true ] && [ "$IS_MAC" = false ]; then
    echo -e "   \033[33m⚠️  Note: First startup will download qwen3.5:9b (~6.6GB).\033[0m"
    echo -e "   \033[33m   Monitor: docker logs trinity-claw-ollama-1 -f\033[0m"
    echo ""
fi

if [ "$IS_MAC" = true ] && [ "$LOCAL_MODE" = true ]; then
    echo -e "   \033[36m🍎 Mac tip: To keep Ollama running after reboot:\033[0m"
    echo -e "   \033[36m   brew services start ollama\033[0m"
    echo ""
fi

echo -e "   \033[33m🎤 Voice messages: Whisper (~150MB) downloads on first voice message.\033[0m"
echo -e "   \033[33m📸 Image vision: uses trinity-vision model in litellm_config.yaml.\033[0m"
echo -e "   \033[33m🌐 Browser automation: Playwright + Chromium installed automatically.\033[0m"
echo ""
