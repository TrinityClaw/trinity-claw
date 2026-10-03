#requires -Version 5.1
<#
  install.ps1 - TrinityClaw Installation Wizard (Windows)

  Run with:
      powershell -ExecutionPolicy Bypass -File install.ps1
#>

$ErrorActionPreference = 'Continue'

# ---------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------
function Write-Utf8NoBom([string]$Path, [string]$Content) {
    [System.IO.File]::WriteAllText(
        (Join-Path (Get-Location).Path $Path), $Content,
        (New-Object System.Text.UTF8Encoding($false)))
}

function Append-Utf8NoBom([string]$Path, [string]$Content) {
    [System.IO.File]::AppendAllText(
        (Join-Path (Get-Location).Path $Path), $Content,
        (New-Object System.Text.UTF8Encoding($false)))
}

function Test-PortInUse([int]$Port) {
    try { return [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop) }
    catch { return $false }
}

# Returns $true as soon as the URL answers with ANY HTTP response
# (even a 404 means the server is up); $false on timeout.
function Wait-ForServer([string]$Url, [int]$TimeoutSec = 180) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        try {
            Invoke-WebRequest $Url -UseBasicParsing -TimeoutSec 4 | Out-Null
            return $true
        } catch {
            $resp = $_.Exception.Response
            if (-not $resp -and $_.Exception.InnerException) {
                $resp = $_.Exception.InnerException.Response
            }
            if ($resp) { return $true }
            Start-Sleep -Seconds 3
        }
    }
    return $false
}

Write-Host ""
Write-Host "   +======================================================+" -ForegroundColor Green
Write-Host "   |        TrinityClaw AI Agent - Installation Wizard    |" -ForegroundColor Green
Write-Host "   +======================================================+" -ForegroundColor Green
Write-Host ""

# ---------------------------------------------------------------
# Step 0: Download repo if not already present
# ---------------------------------------------------------------
if (-not (Test-Path "docker-compose.yml")) {
    $installDir = "$env:USERPROFILE\trinity-claw"
    Write-Host "   Downloading TrinityClaw to $installDir..." -ForegroundColor Yellow
    try {
        if (-not (Test-Path $installDir)) { New-Item -ItemType Directory -Path $installDir | Out-Null }
        $zip = "$env:TEMP\trinity-claw.zip"
        Invoke-WebRequest -Uri "https://github.com/TrinityClaw/trinity-claw/archive/refs/heads/main.zip" `
            -OutFile $zip -UseBasicParsing -ErrorAction Stop
        $extractDir = "$env:TEMP\tc-extract"
        if (Test-Path $extractDir) { Remove-Item $extractDir -Recurse -Force }
        Expand-Archive -Path $zip -DestinationPath $extractDir -Force
        Copy-Item -Path "$extractDir\trinity-claw-main\*" -Destination $installDir -Recurse -Force
        Remove-Item $extractDir -Recurse -Force
        Remove-Item $zip -Force
        Write-Host "   [OK] Files ready at $installDir" -ForegroundColor Green
        Set-Location $installDir
    } catch {
        Write-Host ""
        Write-Host "   [FAIL] Download failed: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "   Check your internet connection and re-run this installer." -ForegroundColor Yellow
        Read-Host "`n   Press Enter to exit"
        exit 1
    }
}

# ---------------------------------------------------------------
# Step 0b: Check Docker
# ---------------------------------------------------------------
Write-Host "   Checking prerequisites..." -ForegroundColor Yellow

# Docker Desktop is installed per-machine OR per-user; try both locations.
$dockerExe = @(
    "$env:ProgramFiles\Docker\Docker\Docker Desktop.exe"
    "$env:LOCALAPPDATA\Programs\Docker\Docker Desktop.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Host "   [FAIL] Docker Desktop is not installed." -ForegroundColor Red
    Write-Host ""
    Write-Host "   +----------------------------------------------------+" -ForegroundColor Yellow
    Write-Host "   |  INSTALL DOCKER DESKTOP (it's free)               |" -ForegroundColor Yellow
    Write-Host "   |                                                    |" -ForegroundColor Yellow
    Write-Host "   |  1. A browser window is opening now               |" -ForegroundColor Yellow
    Write-Host "   |  2. Click 'Download for Windows'                  |" -ForegroundColor Yellow
    Write-Host "   |  3. Run the installer                             |" -ForegroundColor Yellow
    Write-Host "   |  4. Open Docker Desktop from the Start menu       |" -ForegroundColor Yellow
    Write-Host "   |  5. Wait for the whale icon in your taskbar       |" -ForegroundColor Yellow
    Write-Host "   |  6. Re-run this installer                         |" -ForegroundColor Yellow
    Write-Host "   +----------------------------------------------------+" -ForegroundColor Yellow
    Start-Process "https://www.docker.com/products/docker-desktop/"
    Read-Host "`n   Press Enter to exit"
    exit 1
}

& docker info 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "   Docker is installed but not running. Starting Docker Desktop..." -ForegroundColor Yellow
    if ($dockerExe) { Start-Process $dockerExe }
    Write-Host "   Waiting for Docker to start (up to 90 seconds)..." -ForegroundColor Yellow
    $sw = [Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt 90) {
        Start-Sleep -Seconds 3
        & docker info 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) { break }
    }
    & docker info 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "   [FAIL] Docker did not start in time." -ForegroundColor Red
        Write-Host "   If this is the first launch, open Docker Desktop manually and" -ForegroundColor Yellow
        Write-Host "   accept the terms / finish setup, then re-run the installer." -ForegroundColor Yellow
        Read-Host "   Press Enter to exit"
        exit 1
    }
}

Write-Host "   [OK] Docker installed" -ForegroundColor Green

& docker compose version 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "   [FAIL] Docker Compose not found. Update Docker Desktop." -ForegroundColor Red
    exit 1
}
Write-Host "   [OK] Docker Compose installed" -ForegroundColor Green
Write-Host ""

# ---------------------------------------------------------------
# Step 1: Choose model source (validated, default: cloud)
# ---------------------------------------------------------------
Write-Host "   +-----------------------------------------+" -ForegroundColor Cyan
Write-Host "   |  Model Source                           |" -ForegroundColor Cyan
Write-Host "   +-----------------------------------------+" -ForegroundColor Cyan
Write-Host ""
Write-Host "   Options:" -ForegroundColor Gray
Write-Host "   [cloud]  Use a cloud provider (OpenAI, NVIDIA, Anthropic, etc.)" -ForegroundColor Gray
Write-Host "   [local]  Use a local Ollama model (qwen3.5:9b, ~6.6GB, no API key needed)" -ForegroundColor Gray
Write-Host ""
do {
    $modelSource = (Read-Host "   Choose model source [cloud/local] (default: cloud)").Trim().ToLower()
} while ($modelSource -notin @('cloud', 'local', ''))
if ($modelSource -eq '') { $modelSource = 'cloud' }

# Generate a secure random agent API key
$trinityKey = -join ((65..90) + (97..122) + (48..57) | Get-Random -Count 32 | ForEach-Object { [char]$_ })

if ($modelSource -eq "local") {

    # -- LOCAL (Ollama) path --
    $ollamaModel = "qwen3.5:9b"
    Write-Host ""
    Write-Host "   [OK] Local mode selected - Ollama will be used." -ForegroundColor Green
    Write-Host "   [INFO] Model '$ollamaModel' (~6.6GB). Make sure this tag exists in the" -ForegroundColor Yellow
    Write-Host "      Ollama library - the pull step below will fail fast if it doesn't." -ForegroundColor Yellow
    Write-Host ""

    $model      = "ollama/$ollamaModel"
    $apiBase    = "http://ollama:11434"
    $apiKeyName = "LOCAL_MODE"

    $envContent = @"
# TrinityClaw Secrets
LITELLM_MASTER_KEY=sk-trinity-local-key
MODEL_SOURCE=local
OLLAMA_MODEL=$ollamaModel
TRINITY_API_KEY=$trinityKey
"@

    if (-not (Test-Path "config")) { New-Item -ItemType Directory -Path "config" | Out-Null }
    $litellmContent = @"
model_list:
  - model_name: trinity-default
    litellm_params:
      model: $model
      api_base: $apiBase

  - model_name: trinity-vision
    litellm_params:
      model: $model
      api_base: $apiBase

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
"@
} else {

    # -- CLOUD path: pick a provider, sensible defaults pre-filled --
    Write-Host ""
    Write-Host "   +-----------------------------------------+" -ForegroundColor Cyan
    Write-Host "   |  LLM Cloud Provider                     |" -ForegroundColor Cyan
    Write-Host "   +-----------------------------------------+" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "   [1] OpenAI      (gpt-4o, vision-capable)" -ForegroundColor Gray
    Write-Host "   [2] NVIDIA      (kimi-k2-instruct + llama vision)" -ForegroundColor Gray
    Write-Host "   [3] Anthropic   (claude-3-5-sonnet, vision-capable)" -ForegroundColor Gray
    Write-Host "   [4] Custom      (enter model, base URL and key variable manually)" -ForegroundColor Gray
    Write-Host ""
    do {
        $provider = (Read-Host "   Choose provider [1-4] (default: 1)").Trim()
    } while ($provider -notin @('1', '2', '3', '4', ''))
    if ($provider -eq '') { $provider = '1' }

    switch ($provider) {
        '1' {
            $modelDefault   = "openai/gpt-4o"
            $visionDefault  = "openai/gpt-4o"
            $apiBaseDefault = "https://api.openai.com/v1"
            $apiKeyName     = "OPENAI_API_KEY"
        }
        '2' {
            $modelDefault   = "openai/moonshotai/kimi-k2-instruct"
            $visionDefault  = "openai/meta/llama-3.2-90b-vision-instruct"
            $apiBaseDefault = "https://integrate.api.nvidia.com/v1"
            $apiKeyName     = "NVIDIA_API_KEY"
        }
        '3' {
            $modelDefault   = "anthropic/claude-3-5-sonnet-20241022"
            $visionDefault  = "anthropic/claude-3-5-sonnet-20241022"
            $apiBaseDefault = "https://api.anthropic.com"
            $apiKeyName     = "ANTHROPIC_API_KEY"
        }
        '4' {
            $modelDefault   = ""
            $visionDefault  = ""
            $apiBaseDefault = ""
            $apiKeyName     = ""
        }
    }

    Write-Host ""
    Write-Host "   Press Enter to accept each default." -ForegroundColor Gray
    Write-Host ""

    # Model name
    if ($modelDefault) {
        $in = (Read-Host "   1. Model name [default: $modelDefault]").Trim()
        $model = if ($in) { $in } else { $modelDefault }
    } else {
        $model = (Read-Host "   1. Model name (required, e.g. openai/gpt-4o)").Trim()
        if (-not $model) {
            Write-Host "   [FAIL] Model name is required." -ForegroundColor Red
            Read-Host "   Press Enter to exit"; exit 1
        }
    }

    # Vision model
    if (-not $visionDefault) { $visionDefault = $model }
    $vin = (Read-Host "   2. Vision model (for photos) [default: $visionDefault]").Trim()
    $visionModel = if ($vin) { $vin } else { $visionDefault }

    # API base URL
    if ($apiBaseDefault) {
        $in = (Read-Host "   3. API Base URL [default: $apiBaseDefault]").Trim()
        $apiBase = if ($in) { $in } else { $apiBaseDefault }
    } else {
        $apiBase = (Read-Host "   3. API Base URL (required, e.g. https://api.openai.com/v1)").Trim()
        if (-not $apiBase) {
            Write-Host "   [FAIL] API Base URL is required." -ForegroundColor Red
            Read-Host "   Press Enter to exit"; exit 1
        }
    }
    if (-not [uri]::IsWellFormedUriString($apiBase, [System.UriKind]::Absolute)) {
        Write-Host "   [FAIL] '$apiBase' is not a valid URL." -ForegroundColor Red
        Read-Host "   Press Enter to exit"; exit 1
    }

    # API key variable name (custom providers only)
    if (-not $apiKeyName) {
        $apiKeyName = (Read-Host "   4. API Key environment variable name (e.g. MYPROVIDER_API_KEY)").Trim()
        if (-not $apiKeyName) {
            Write-Host "   [FAIL] Key variable name is required." -ForegroundColor Red
            Read-Host "   Press Enter to exit"; exit 1
        }
    }

    # API key value (hidden), with proper BSTR cleanup
    $apiKeySecure = Read-Host "   5. API Key value (hidden)" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($apiKeySecure)
    try {
        $apiKeyPlain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr).Trim()
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    if (-not $apiKeyPlain) {
        Write-Host "   [FAIL] API key is required." -ForegroundColor Red
        Read-Host "   Press Enter to exit"; exit 1
    }

    # Fail fast: verify the key against the provider before writing any config
    Write-Host ""
    Write-Host "   Verifying API key against $apiBase ..." -ForegroundColor Yellow
    try {
        Invoke-RestMethod "$apiBase/models" -Headers @{ Authorization = "Bearer $apiKeyPlain" } `
            -TimeoutSec 20 -ErrorAction Stop | Out-Null
        Write-Host "   [OK] API key verified" -ForegroundColor Green
    } catch {
        Write-Host "   [WARN] Could not verify the key: $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host "   Continuing anyway - check the key in .env if the agent fails to start." -ForegroundColor Yellow
    }

    $envContent = @"
# TrinityClaw Secrets
LITELLM_MASTER_KEY=sk-trinity-local-key
MODEL_SOURCE=cloud
$apiKeyName=$apiKeyPlain
TRINITY_API_KEY=$trinityKey
"@

    if (-not (Test-Path "config")) { New-Item -ItemType Directory -Path "config" | Out-Null }
    $litellmContent = @"
model_list:
  - model_name: trinity-default
    litellm_params:
      model: $model
      api_key: os.environ/$apiKeyName
      api_base: $apiBase

  # Vision model -- used automatically when photos are sent.
  - model_name: trinity-vision
    litellm_params:
      model: $visionModel
      api_key: os.environ/$apiKeyName
      api_base: $apiBase

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
"@
}

# ---------------------------------------------------------------
# Back up existing config before overwriting (re-run safety)
# ---------------------------------------------------------------
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
foreach ($f in @(".env", "trinity-key.txt")) {
    if (Test-Path $f) {
        Copy-Item $f "${f}.bak-$timestamp"
        Write-Host "   [!] Existing $f backed up to ${f}.bak-$timestamp" -ForegroundColor Yellow
    }
}

Write-Utf8NoBom ".env" $envContent
Write-Utf8NoBom "config\litellm_config.yaml" $litellmContent

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
Append-Utf8NoBom ".env" $optionalKeys
$installPath = (Get-Location).Path
Write-Host "   [OK] .env created at $installPath\.env" -ForegroundColor Green
Write-Host "      -> Add Tavily, Telegram, SMTP and other optional keys there anytime." -ForegroundColor Gray
Write-Host ""

# ---------------------------------------------------------------
# Port conflict warning (advisory only - Trinity may own them on re-run)
# ---------------------------------------------------------------
$portsToCheck = @(8080, 8001)
if ($modelSource -eq "local") { $portsToCheck += 11434 }
foreach ($p in $portsToCheck) {
    if (Test-PortInUse $p) {
        Write-Host "   [!] Port $p is already in use." -ForegroundColor Yellow
        Write-Host "      If TrinityClaw isn't already running, free this port or the containers will fail." -ForegroundColor Yellow
    }
}

# ---------------------------------------------------------------
# Build and start containers
# ---------------------------------------------------------------
if ($modelSource -eq "local") {
    $composeArgs = @("--profile", "local")
    $composeCmd  = "docker compose --profile local"
    $batCompose  = "--profile local "
} else {
    $composeArgs = @()
    $composeCmd  = "docker compose"
    $batCompose  = ""
}

Write-Host ""
Write-Host "   Building containers (this may take a few minutes on first run)..." -ForegroundColor Yellow
& docker compose @composeArgs up -d --build

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "   [FAIL] Docker failed to start containers." -ForegroundColor Red
    Write-Host "      Make sure Docker Desktop is running (whale icon in taskbar)," -ForegroundColor Gray
    Write-Host "      then run: $composeCmd up -d" -ForegroundColor Gray
    Read-Host "   Press Enter to exit"
    exit 1
}
Write-Host "   [OK] Containers started!" -ForegroundColor Green

# Guarantee the "starts automatically after reboot" promise:
# containers get an explicit restart policy, not just Docker autoStart.
$containerIds = & docker compose @composeArgs ps -q 2>$null
if ($containerIds) {
    $containerIds | ForEach-Object { & docker update --restart unless-stopped $_ | Out-Null }
    Write-Host "   [OK] Containers set to auto-restart (unless-stopped)" -ForegroundColor Green
}

# ---------------------------------------------------------------
# Local mode: pre-pull the Ollama model (with progress) so the
# first chat message isn't a surprise multi-minute download.
# ---------------------------------------------------------------
if ($modelSource -eq "local") {
    $pull = (Read-Host "   Pull the Ollama model now (~6.6GB, several minutes)? [Y/n]").Trim().ToLower()
    if ($pull -notmatch '^n') {
        $ollamaId = & docker compose --profile local ps -q ollama 2>$null
        if ($ollamaId) {
            Write-Host "   Pulling $ollamaModel..." -ForegroundColor Yellow
            & docker exec $ollamaId ollama pull $ollamaModel
            if ($LASTEXITCODE -ne 0) {
                Write-Host "   [WARN] Model pull failed - it will be retried on first use." -ForegroundColor Yellow
            } else {
                Write-Host "   [OK] Model ready!" -ForegroundColor Green
            }
        } else {
            Write-Host "   [WARN] Ollama container not found (service name may differ) - skipping pre-pull." -ForegroundColor Yellow
        }
    }
}

# ---------------------------------------------------------------
# Wait for services before declaring victory
# ---------------------------------------------------------------
Write-Host ""
Write-Host "   Waiting for TrinityClaw services to come up..." -ForegroundColor Yellow
$backendReady = Wait-ForServer "http://localhost:8001/health" 180
$uiReady      = Wait-ForServer "http://localhost:8080" 180
if ($backendReady -and $uiReady) {
    Write-Host "   [OK] TrinityClaw is up and responding!" -ForegroundColor Green
} else {
    Write-Host "   [!] Services are still starting - they can take a few minutes on first run." -ForegroundColor Yellow
    Write-Host "      Check status with: $composeCmd ps" -ForegroundColor Yellow
}

# ---------------------------------------------------------------
# Enable Docker Desktop start at login
# ---------------------------------------------------------------
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

# ---------------------------------------------------------------
# Create Desktop launcher (.bat double-click)
# NOTE: no 'goto' inside parenthesized blocks - that pattern
# intermittently breaks batch files.
# ---------------------------------------------------------------
$launcher = "$env:USERPROFILE\Desktop\Start TrinityClaw.bat"
$batContent = @"
@echo off
echo.
echo    Starting TrinityClaw...
docker info >nul 2>&1
if errorlevel 1 (
    echo    Opening Docker Desktop -- please wait...
    start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
)
:wait
docker info >nul 2>&1
if not errorlevel 1 goto ready
timeout /t 3 /nobreak >nul
goto wait
:ready
cd /d "$installPath"
docker compose ${batCompose}up -d
echo.
echo    TrinityClaw is running! Opening browser...
timeout /t 2 /nobreak >nul
start http://localhost:8080
"@
$batContent | Out-File -FilePath $launcher -Encoding ascii
Write-Host "   [OK] Desktop launcher created: Start TrinityClaw.bat" -ForegroundColor Green

# ---------------------------------------------------------------
# Save key to file, copy to clipboard, display it clearly
# ---------------------------------------------------------------
Write-Utf8NoBom "trinity-key.txt" "$trinityKey`r`n"
$clipboardNote = ""
try {
    Set-Clipboard -Value $trinityKey -ErrorAction Stop
    $clipboardNote = "  (also copied to your clipboard)"
} catch {}

Write-Host ""
Write-Host "   [OK] TrinityClaw Installed!" -ForegroundColor Green
Write-Host ""
Write-Host "   Web UI:  http://localhost:8080" -ForegroundColor Cyan
Write-Host "   API:     http://localhost:8001" -ForegroundColor Cyan
Write-Host "   Docs:    http://localhost:8001/docs" -ForegroundColor Cyan
Write-Host "   Status:  backend $(if ($backendReady) {'reachable'} else {'still starting...'}), web UI $(if ($uiReady) {'reachable'} else {'still starting...'})"
Write-Host ""
Write-Host "   -------------------------------------------------------"
Write-Host "   AGENT API KEY - copy and save this:" -ForegroundColor Yellow
Write-Host ""
Write-Host "       $trinityKey" -ForegroundColor Cyan
Write-Host ""
Write-Host "   Enter it in: Settings -> Agent Security -> Agent API Key" -ForegroundColor Gray
Write-Host "   (saved to trinity-key.txt in this folder$clipboardNote)" -ForegroundColor Gray
Write-Host "   -------------------------------------------------------"
Write-Host ""
Write-Host "   After every Windows restart, TrinityClaw starts AUTOMATICALLY!" -ForegroundColor Green
Write-Host "      Docker Desktop starts -> containers start -> ready in ~30 sec" -ForegroundColor Cyan
Write-Host "      Need a manual restart? Double-click 'Start TrinityClaw' on your Desktop." -ForegroundColor Cyan
Write-Host ""
Write-Host "   To add Tavily, Telegram, email or other optional integrations:" -ForegroundColor Yellow
Write-Host "      Edit: $installPath\.env" -ForegroundColor Yellow
Write-Host "      Then: $composeCmd restart" -ForegroundColor Yellow
Write-Host ""
Write-Host "   Voice: Whisper (~150MB) downloads on first voice message." -ForegroundColor Yellow
Write-Host "   Vision: uses the trinity-vision model in litellm_config.yaml." -ForegroundColor Yellow
Write-Host "   Browser: Playwright + Chromium installed automatically." -ForegroundColor Yellow
Write-Host ""

# Open the Web UI if it's ready
if ($uiReady) { Start-Process "http://localhost:8080" }
