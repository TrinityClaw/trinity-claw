# install.ps1 - TrinityClaw Installation Wizard (Windows)

Write-Host ""
Write-Host "   +======================================================+" -ForegroundColor Green
Write-Host "   |        TrinityClaw AI Agent - Installation Wizard    |" -ForegroundColor Green
Write-Host "   +======================================================+" -ForegroundColor Green
Write-Host ""

# ---------------------------------------------
# Step 0: Check Docker
# ---------------------------------------------
Write-Host "   Checking prerequisites..." -ForegroundColor Yellow
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "   [FAIL] Docker not found. Installing Docker Desktop via winget..." -ForegroundColor Red
    winget install Docker.DockerDesktop -e --silent
    Write-Host ""
    Write-Host "   [OK] Docker Desktop installed." -ForegroundColor Green
    Write-Host "   [!]  Please restart your terminal and re-run this script." -ForegroundColor Yellow
    exit 0
}
Write-Host "   [OK] Docker installed" -ForegroundColor Green

& docker compose version 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "   [FAIL] Docker Compose not found. Update Docker Desktop." -ForegroundColor Red
    exit 1
}
Write-Host "   [OK] Docker Compose installed" -ForegroundColor Green
Write-Host ""

# ---------------------------------------------
# Step 1: Choose model source
# ---------------------------------------------
Write-Host "   +-----------------------------------------+" -ForegroundColor Cyan
Write-Host "   |  Model Source                           |" -ForegroundColor Cyan
Write-Host "   +-----------------------------------------+" -ForegroundColor Cyan
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

  # -- LOCAL (Ollama) path --
  Write-Host ""
  Write-Host "   [OK] Local mode selected - Ollama will be used." -ForegroundColor Green
  Write-Host "   [INFO] The model 'qwen3.5:9b' (~6.6GB) will be pulled automatically" -ForegroundColor Yellow
  Write-Host "      on first startup. This may take several minutes." -ForegroundColor Yellow
  Write-Host ""

  $model = "ollama/qwen3.5:9b"
  $apiBase = "http://ollama:11434"
  $apiKeyName = "LOCAL_MODE"
  $apiKeyPlain = "not-required"

  # Create .env
  $envContent = @"
# TrinityClaw Secrets
LITELLM_MASTER_KEY=sk-trinity-local-key
MODEL_SOURCE=local
OLLAMA_MODEL=qwen3.5:9b
TRINITY_API_KEY=$trinityKey
"@
  $envContent | Out-File -FilePath ".env" -Encoding utf8

  # Create litellm_config.yaml
  $litellmContent = @"
model_list:
  - model_name: trinity-default
    litellm_params:
      model: $model
      api_base: $apiBase

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
"@
  $litellmContent | Out-File -FilePath "config\litellm_config.yaml" -Encoding utf8

  Write-Host "   [OK] Local configuration created!" -ForegroundColor Green
  Write-Host ""
} else {

  # -- CLOUD path --
  Write-Host ""
  Write-Host "   +-----------------------------------------+" -ForegroundColor Cyan
  Write-Host "   |  LLM Cloud Configuration                |" -ForegroundColor Cyan
  Write-Host "   +-----------------------------------------+" -ForegroundColor Cyan
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
  $envContent = @"
# TrinityClaw Secrets
LITELLM_MASTER_KEY=sk-trinity-local-key
MODEL_SOURCE=cloud
$apiKeyName=$apiKeyPlain
TRINITY_API_KEY=$trinityKey
"@
  $envContent | Out-File -FilePath ".env" -Encoding utf8

  # Create litellm_config.yaml
  $litellmContent = @"
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
"@
  $litellmContent | Out-File -FilePath "config\litellm_config.yaml" -Encoding utf8

  Write-Host ""
  Write-Host "   [OK] Cloud configuration created!" -ForegroundColor Green
}

# -- Append optional integrations template to .env (both modes) --
$optionalKeys = @"

# -- Optional integrations (uncomment and fill in to enable) --

# Web search -- Tavily (best results, free tier: 1000 searches/month)
# Get key at: https://tavily.com -> Dashboard -> API Keys
# Without this, search falls back to DuckDuckGo -> Bing (may be rate-limited)
# TAVILY_API_KEY=tvly-xxxxx

# Telegram -- chat with your agent via text, voice, and photos
# 1. Create bot: Telegram -> @BotFather -> /newbot -> copy token
# 2. Get your Chat ID: Telegram -> @userinfobot -> send any message
# TELEGRAM_BOT_TOKEN=xxxxxx:xxxxxxxxxxxxxxxxxxxxxxxxx
# TELEGRAM_CHAT_ID=123456789

# Email sending -- Gmail SMTP (needs an App Password, not your regular password)
# Enable 2FA at myaccount.google.com/security, then:
# Generate App Password at myaccount.google.com/apppasswords
# EMAIL_PROVIDER=smtp
# EMAIL_FROM=you@gmail.com
# SMTP_HOST=smtp.gmail.com
# SMTP_PORT=587
# SMTP_USER=you@gmail.com
# SMTP_PASSWORD=xxxx xxxx xxxx xxxx

# -------------------------------------------------------------------------------
"@
Add-Content -Path ".env" -Value $optionalKeys -Encoding utf8
$installPath = (Get-Location).Path
Write-Host "   [OK] .env created at $installPath\.env" -ForegroundColor Green
Write-Host "      -> Add Tavily, Telegram, SMTP and other optional keys there anytime." -ForegroundColor Gray
Write-Host ""

# ---------------------------------------------
# Build and start containers
# ---------------------------------------------
if ($modelSource -eq "local") { $composeArgs = @("--profile", "local") } else { $composeArgs = @() }

Write-Host ""
Write-Host "   Building containers (this may take a few minutes on first run)..." -ForegroundColor Yellow
& docker compose @composeArgs up -d --build

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "   [FAIL] Docker failed to start containers." -ForegroundColor Red
    Write-Host "      Make sure Docker Desktop is running (whale icon in taskbar)," -ForegroundColor Gray
    Write-Host "      then run: docker compose up -d" -ForegroundColor Gray
} else {
    Write-Host "   [OK] Containers started!" -ForegroundColor Green
}

# ---------------------------------------------
# Enable Docker Desktop start at login
# ---------------------------------------------
Write-Host "   Enabling Docker Desktop auto-start at login..." -ForegroundColor Yellow
$dockerSettingsPath = "$env:APPDATA\Docker\settings-store.json"
if (-not (Test-Path $dockerSettingsPath)) {
    $dockerSettingsPath = "$env:APPDATA\Docker\settings.json"
}
if (Test-Path $dockerSettingsPath) {
    try {
        $settings = Get-Content $dockerSettingsPath -Raw | ConvertFrom-Json
        $settings | Add-Member -NotePropertyName "autoStart" -NotePropertyValue $true -Force
        $settings | ConvertTo-Json -Depth 10 | Set-Content $dockerSettingsPath -Encoding utf8
        Write-Host "   [OK] Docker Desktop set to start at login" -ForegroundColor Green
    } catch {
        Write-Host "   [!] Could not auto-configure. Enable manually: Docker Desktop -> Settings -> General -> Start at login" -ForegroundColor Yellow
    }
} else {
    Write-Host "   [!] Enable manually: Docker Desktop -> Settings -> General -> Start Docker Desktop when you log in" -ForegroundColor Yellow
}

# ---------------------------------------------
# Create Desktop launcher (.bat double-click)
# ---------------------------------------------
if ($modelSource -eq "local") { $composeFlag = "--profile local " } else { $composeFlag = "" }
$launcher = "$env:USERPROFILE\Desktop\Start TrinityClaw.bat"
@"
@echo off
echo.
echo    Starting TrinityClaw...
docker info >nul 2>&1
if errorlevel 1 (
    echo    Opening Docker Desktop -- please wait...
    start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    :waitloop
    timeout /t 3 /nobreak >nul
    docker info >nul 2>&1
    if errorlevel 1 goto waitloop
    echo    Docker is ready!
)
cd /d "$installPath"
docker compose ${composeFlag}up -d
echo.
echo    TrinityClaw is running! Opening browser...
timeout /t 2 /nobreak >nul
start http://localhost:8080
"@ | Out-File -FilePath $launcher -Encoding ascii
Write-Host "   [OK] Desktop launcher created: Start TrinityClaw.bat" -ForegroundColor Green

# ---------------------------------------------
# Save key to file and display it clearly
# ---------------------------------------------
$trinityKey | Out-File -FilePath "trinity-key.txt" -Encoding utf8

Write-Host ""
Write-Host "   [OK] TrinityClaw Installed!" -ForegroundColor Green
Write-Host ""
Write-Host "   Web UI:  http://localhost:8080" -ForegroundColor Cyan
Write-Host "   API:     http://localhost:8001" -ForegroundColor Cyan
Write-Host "   Docs:    http://localhost:8001/docs" -ForegroundColor Cyan
Write-Host ""
Write-Host "   -------------------------------------------------------"
Write-Host "   AGENT API KEY - copy and save this:" -ForegroundColor Yellow
Write-Host ""
Write-Host "       $trinityKey" -ForegroundColor Cyan
Write-Host ""
Write-Host "   Enter it in: Settings -> Agent Security -> Agent API Key" -ForegroundColor Gray
Write-Host "   (also saved to trinity-key.txt in this folder)" -ForegroundColor Gray
Write-Host "   -------------------------------------------------------"
Write-Host ""
Write-Host "   After every Windows restart, TrinityClaw starts AUTOMATICALLY!" -ForegroundColor Green
Write-Host "      Docker Desktop starts -> containers start -> ready in ~30 sec" -ForegroundColor Cyan
Write-Host "      Need a manual restart? Double-click 'Start TrinityClaw' on your Desktop." -ForegroundColor Cyan
Write-Host ""
Write-Host "   To add Tavily, Telegram, email or other optional integrations:" -ForegroundColor Yellow
Write-Host "      Edit: $installPath\.env" -ForegroundColor Yellow
Write-Host "      Then: docker compose $($composeFlag.Trim()) restart" -ForegroundColor Yellow
Write-Host ""
Write-Host "   Voice: Whisper (~150MB) downloads on first voice message." -ForegroundColor Yellow
Write-Host "   Vision: uses trinity-vision model in litellm_config.yaml." -ForegroundColor Yellow
Write-Host "   Browser: Playwright + Chromium installed automatically." -ForegroundColor Yellow
Write-Host ""
