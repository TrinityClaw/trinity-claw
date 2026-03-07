# install.ps1 - TrinityClaw Installation Wizard (Windows)

Write-Host ""
Write-Host "   ╔══════════════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "   ║        TrinityClaw AI Agent - Installation Wizard    ║" -ForegroundColor Green
Write-Host "   ╚══════════════════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""

# ─────────────────────────────────────────────
# Step 1: Choose model source
# ─────────────────────────────────────────────
Write-Host "   ┌─────────────────────────────────────────┐" -ForegroundColor Cyan
Write-Host "   │  Model Source                           │" -ForegroundColor Cyan
Write-Host "   └─────────────────────────────────────────┘" -ForegroundColor Cyan
Write-Host ""
Write-Host "   Options:" -ForegroundColor Gray
Write-Host "   [cloud]  Use a cloud provider (OpenAI, NVIDIA, Anthropic, etc.)" -ForegroundColor Gray
Write-Host "   [local]  Use a local Ollama model (qwen3.5:9b, ~6.6GB, no API key needed)" -ForegroundColor Gray
Write-Host ""
$modelSource = Read-Host "   Choose model source [cloud/local]"
$modelSource = $modelSource.Trim().ToLower()

# Generate a secure random agent API key
$trinityKey = -join ((65..90) + (97..122) + (48..57) | Get-Random -Count 32 | ForEach-Object { [char]$_ })

if ($modelSource -eq "local") {

  # ── LOCAL (Ollama) path ──────────────────────────────────────────────
  Write-Host ""
  Write-Host "   ✅ Local mode selected — Ollama will be used." -ForegroundColor Green
  Write-Host "   ℹ️  The model 'qwen3.5:9b' (~6.6GB) will be pulled automatically" -ForegroundColor Yellow
  Write-Host "      on first startup. This may take several minutes." -ForegroundColor Yellow
  Write-Host ""

  $model = "ollama/qwen3.5:9b"
  $apiBase = "http://ollama:11434"
  $apiKeyName = "LOCAL_MODE"
  $apiKeyPlain = "not-required"

  # Create .env
  @"
# TrinityClaw Secrets
LITELLM_MASTER_KEY=sk-trinity-local-key
MODEL_SOURCE=local
OLLAMA_MODEL=qwen3.5:9b
TRINITY_API_KEY=$trinityKey
"@ | Out-File -FilePath ".env" -Encoding utf8

  # Create litellm_config.yaml
  @"
model_list:
  - model_name: trinity-default
    litellm_params:
      model: $model
      api_base: $apiBase

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
"@ | Out-File -FilePath "config\litellm_config.yaml" -Encoding utf8

  Write-Host "   ✅ Local configuration created!" -ForegroundColor Green
  Write-Host ""
  Write-Host "   ─────────────────────────────────────────────────────" -ForegroundColor DarkGray
  Write-Host "   ⚠️  To start TrinityClaw with local Ollama model, run:" -ForegroundColor Yellow
  Write-Host "       docker compose --profile local up -d" -ForegroundColor White
  Write-Host "   ─────────────────────────────────────────────────────" -ForegroundColor DarkGray

}
else {

  # ── CLOUD path ──────────────────────────────────────────────────────
  Write-Host ""
  Write-Host "   ┌─────────────────────────────────────────┐" -ForegroundColor Cyan
  Write-Host "   │  LLM Cloud Configuration                │" -ForegroundColor Cyan
  Write-Host "   └─────────────────────────────────────────┘" -ForegroundColor Cyan
  Write-Host ""

  # Parameter 1: Model name
  Write-Host "   Provider examples:" -ForegroundColor Gray
  Write-Host "   - OpenAI:     openai/gpt-4o" -ForegroundColor Gray
  Write-Host "   - NVIDIA:     openai/moonshotai/kimi-k2-instruct" -ForegroundColor Gray
  Write-Host "   - Anthropic:  anthropic/claude-3-5-sonnet-20241022" -ForegroundColor Gray
  Write-Host ""
  $model = Read-Host "   1. Model name"

  # Parameter 2: API Base URL
  Write-Host ""
  Write-Host "   API Base examples:" -ForegroundColor Gray
  Write-Host "   - NVIDIA:  https://integrate.api.nvidia.com/v1" -ForegroundColor Gray
  Write-Host "   - OpenAI:  https://api.openai.com/v1" -ForegroundColor Gray
  Write-Host ""
  $apiBase = Read-Host "   2. API Base URL"

  # Parameter 3: API Key name
  Write-Host ""
  Write-Host "   This is the ENVIRONMENT VARIABLE name (not the key itself)" -ForegroundColor Gray
  Write-Host "   Example: NVIDIA_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY" -ForegroundColor Gray
  Write-Host ""
  $apiKeyName = Read-Host "   3. API Key variable name"

  # Parameter 4: API Key value
  Write-Host ""
  $apiKeyValue = Read-Host "   4. API Key value (hidden)" -AsSecureString
  $apiKeyPlain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($apiKeyValue)
  )

  # Create .env
  @"
# TrinityClaw Secrets
LITELLM_MASTER_KEY=sk-trinity-local-key
MODEL_SOURCE=cloud
$apiKeyName=$apiKeyPlain
TRINITY_API_KEY=$trinityKey
"@ | Out-File -FilePath ".env" -Encoding utf8

  # Create litellm_config.yaml
  @"
model_list:
  - model_name: trinity-default
    litellm_params:
      model: $model
      api_key: os.environ/$apiKeyName
      api_base: $apiBase

  # Vision model -- used automatically when photos are sent.
  # If your main model already supports vision (e.g. gpt-4o, claude-3-5-sonnet) you can
  # set this to the same model. For NVIDIA, use a dedicated vision model:
  #   openai/meta/llama-3.2-90b-vision-instruct
  - model_name: trinity-vision
    litellm_params:
      model: $model
      api_key: os.environ/$apiKeyName
      api_base: $apiBase

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
"@ | Out-File -FilePath "config\litellm_config.yaml" -Encoding utf8

  Write-Host ""
  Write-Host "   ✅ Cloud configuration created!" -ForegroundColor Green
  Write-Host ""
  Write-Host "   ─────────────────────────────────────────────────────" -ForegroundColor DarkGray
  Write-Host "   To start TrinityClaw, run:" -ForegroundColor Gray
  Write-Host "       docker compose up -d" -ForegroundColor White
  Write-Host "   ─────────────────────────────────────────────────────" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "   🎤 Voice messages: Whisper model (~150MB) downloads on first voice message." -ForegroundColor Yellow
Write-Host "   📸 Image vision:   uses the trinity-vision model in litellm_config.yaml." -ForegroundColor Yellow
Write-Host "      NVIDIA users: set trinity-vision to openai/meta/llama-3.2-90b-vision-instruct" -ForegroundColor DarkGray
Write-Host "   🌐 Browser automation: Playwright + Chromium (~300MB) installed automatically" -ForegroundColor Yellow
Write-Host "      inside the container during build. No action needed." -ForegroundColor DarkGray
Write-Host "      Use browser_* functions in the web skill (browser_goto, browser_click, etc.)" -ForegroundColor DarkGray
Write-Host ""
Write-Host "   🌐 Web UI:  http://localhost:8080" -ForegroundColor Cyan
Write-Host "   🔌 API:     http://localhost:8001" -ForegroundColor Cyan
Write-Host "   📖 Docs:    http://localhost:8001/docs" -ForegroundColor Cyan
Write-Host ""
Write-Host "   ─────────────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host "   🔑 Your Agent API Key (save this!)" -ForegroundColor Yellow
Write-Host "   $trinityKey" -ForegroundColor White
Write-Host "   Enter it in: Settings ⚙️ → Agent Security → Agent API Key" -ForegroundColor Gray
Write-Host "   ─────────────────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""