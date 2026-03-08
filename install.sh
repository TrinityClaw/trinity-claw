#!/bin/bash

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
    echo -e "   \033[36m🍎 macOS detected (Apple Silicon optimised setup)\033[0m"
fi

# ─────────────────────────────────────────────────────────
# [1/5] Check prerequisites
# ─────────────────────────────────────────────────────────
echo -e "\033[33m[1/5] Checking prerequisites...\033[0m"

if ! command -v docker &> /dev/null; then
    echo -e "   ❌ Docker not found. Attempting to install..."
    if [[ "$(uname)" == "Darwin" ]]; then
        if command -v brew &> /dev/null; then
            brew install --cask docker
            open /Applications/Docker.app
            echo -e "   ⏳ Waiting for Docker Desktop to start..."
            for i in $(seq 1 30); do
                if docker info &> /dev/null; then break; fi
                sleep 2
            done
        else
            echo -e "   ❌ Homebrew not found. Install Docker Desktop manually: https://docs.docker.com/desktop/mac/install/"
            exit 1
        fi
    elif grep -qi microsoft /proc/version 2>/dev/null; then
        echo -e "   ℹ️  Windows detected. Installing Docker Desktop via winget..."
        winget install Docker.DockerDesktop -e --silent
        echo -e "   ✅ Docker Desktop installed. Please restart your terminal and re-run this script."
        exit 0
    else
        echo -e "   📥 Installing Docker Engine (Linux)..."
        curl -fsSL https://get.docker.com | sh
        sudo systemctl enable --now docker
        sudo usermod -aG docker "$USER"
        echo -e "   ✅ Docker installed. You may need to log out and back in for group changes."
    fi
fi

if ! command -v docker &> /dev/null; then
    echo -e "   ❌ Docker install failed. Visit: https://docs.docker.com/get-docker/"
    exit 1
fi
echo "   ✅ Docker installed"

if ! docker compose version &> /dev/null; then
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

        # Install Ollama natively on Mac
        if ! command -v ollama &> /dev/null; then
            echo -e "   📥 Installing Ollama via Homebrew..."
            if ! command -v brew &> /dev/null; then
                echo -e "   ❌ Homebrew not found. Install it first: https://brew.sh"
                exit 1
            fi
            brew install ollama
        else
            echo "   ✅ Ollama already installed"
        fi

        # Pull the model
        echo -e "   📥 Pulling qwen3.5:9b (~6.6GB, this may take several minutes)..."
        ollama pull qwen3.5:9b

        # Start Ollama in background if not already running
        if ! curl -s http://localhost:11434/api/tags > /dev/null 2>&1; then
            echo -e "   🚀 Starting Ollama service..."
            ollama serve &>/dev/null &
            sleep 3
        else
            echo "   ✅ Ollama already running"
        fi

        OLLAMA_API_BASE="http://host.docker.internal:11434"
        COMPOSE_FILE="docker-compose.mac.yml"
    else
        # Linux — Ollama runs inside Docker
        echo -e "   \033[33mℹ️  The model 'qwen3.5:9b' (~6.6GB) will be pulled automatically\033[0m"
        echo -e "   \033[33m   on first startup. This may take several minutes.\033[0m"
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
    echo -e "   \033[33m   Monitor progress: docker logs trinity-claw-ollama-1 -f\033[0m"
    echo ""
fi

if [ "$IS_MAC" = true ] && [ "$LOCAL_MODE" = true ]; then
    echo -e "   \033[36m🍎 Mac tip: Ollama is running natively. To keep it running after reboot:\033[0m"
    echo -e "   \033[36m   brew services start ollama\033[0m"
    echo ""
fi

echo -e "   \033[33m🎤 Voice messages: Whisper model (~150MB) downloads automatically\033[0m"
echo -e "   \033[33m   on your first voice message and is cached for future use.\033[0m"
echo -e "   \033[33m📸 Image vision: qwen3.5:9b supports vision natively\033[0m"
echo -e "   \033[33m🌐 Browser automation: Playwright + Chromium (~300MB) installed\033[0m"
echo -e "   \033[33m   automatically inside the container during build. No action needed.\033[0m"
echo ""
