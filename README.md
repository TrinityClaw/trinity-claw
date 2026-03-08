# TrinityClaw AI Agent

A self-modifying AI agent with persistent memory, dynamic skill creation, and intelligent reasoning capabilities.

---

> ## 🖥️ LOCAL USE ONLY
>
> **TrinityClaw is designed for personal local servers — do not deploy it on a VPS or any public-facing server.**
>
> The agent API has no rate limiting and no multi-user authentication layer. The `code_executor` and `terminal` skills run arbitrary code inside Docker, and the single `TRINITY_API_KEY` is not sufficient protection for an internet-exposed host. Running this on a public IP without additional firewall rules and a reverse proxy with proper auth could expose your system to unauthorized access.
>
> **Recommended setup:** run on your home machine or a local network server, accessed via your LAN or a private VPN (e.g. Tailscale, WireGuard). Do **not** open ports 8001, 8080, or 8090 to the public internet.

---

## ⚠️ Security Notice

> **Use at your own risk.**

TrinityClaw is **secure by design** in its original form:
- All skills run inside an isolated Docker container with no host access
- Dynamic skill creation uses AST validation and a module ban-list to block dangerous code
- The agent API is protected by a randomly-generated `TRINITY_API_KEY`
- Telegram integration only responds to your specific Chat ID
- Core skills are read-only inside the container; only `skills/dynamic/` is writable by the agent

However, **any modification to the codebase can introduce risk.** This is an inherent property of self-modifying AI agent systems:

- Editing core skills, relaxing the module ban-list, or adding new file-system access can expand the attack surface
- Prompt injection via untrusted web content or external data sources is a known risk class for all LLM agents
- Credentials in `.env` (API keys, SMTP passwords, Telegram tokens) should be treated as sensitive — never commit `.env` to a public repository
- The `code_executor` and `terminal` skills run code inside the container — review any AI-generated code before executing it on sensitive systems
- Dynamic skills created by the agent should be inspected before being promoted to `skills/core/`

This project follows responsible AI agent design practices, but **no system is unconditionally safe once modified.** If you extend or customize TrinityClaw, you take on responsibility for auditing those changes.

---

## Features

- **Self-Modifying**: Creates new skills dynamically
- **Persistent Memory**: ChromaDB vector storage + JSONL conversation logs
- **Multi-Step Reasoning**: Agent loop for complex tasks
- **Dynamic Skills**: Core system skills + user-created skills
- **Web Browsing**: Fetch and analyze web content, including JS-rendered pages
- **Browser Automation**: Full Playwright-powered browser control — navigate, click, type, screenshot, evaluate JS, fill forms
- **Scheduler**: Run automated tasks
- **Telegram Integration**: Chat with your agent via text, voice messages, and photos
- **Google Calendar**: Read, create, update, and delete calendar events — just ask naturally
- **Gmail Integration**: Read inbox, search emails, download attachments, mark as read — full Gmail API access
- **Business Knowledge Base**: Drop your documents into a folder and the agent learns from them — meeting notes, SOPs, contracts, spreadsheets
- **Voice Messages**: Send audio to Telegram or web UI — transcribed locally via Whisper (no API key needed)
- **Image Vision & OCR**: Send photos via Telegram or web UI — described by a vision-capable LLM; local OCR (Tesseract) extracts text from screenshots, documents, and receipts in English, Italian, Spanish, French, Greek, and Serbian (Cyrillic + Latin)
- **Secure**: AST validation, skill whitelisting, Docker isolation

---

## Quick Start

### Prerequisites

- Docker Desktop installed
- 8GB+ RAM recommended
- 20GB free disk space (50GB+ for local model)

> **Browser automation** (Playwright + Chromium, ~300MB) is installed automatically inside the Docker container during build — no extra steps required.

### One-Line Install

**Windows (PowerShell):**
```powershell
iwr -useb https://raw.githubusercontent.com/TrinityClaw/trinity-claw/main/install.ps1 | iex
```

**Linux:**
```bash
curl -fsSL https://raw.githubusercontent.com/TrinityClaw/trinity-claw/main/install.sh | bash
```

**macOS** (recommended — avoids curl pipe issues with Docker detection):
```bash
git clone https://github.com/TrinityClaw/trinity-claw.git ~/trinity-claw
cd ~/trinity-claw
bash install-mac.sh && bash install.sh
```

> `install-mac.sh` checks/starts Docker Desktop first. `install.sh` then runs the full setup wizard.

During installation you will be asked to choose:
- **Cloud** — use a remote provider (OpenAI, NVIDIA, Anthropic, etc.) — requires API key
- **Local** — use Ollama with `llama3.2-vision` running on your machine — no API key needed

### Manual Install

```bash
# 1. Clone repository
git clone https://github.com/TrinityClaw/trinity-claw.git
cd trinity-claw

# 2. Run installer
./install.ps1    # Windows
./install.sh     # Linux/Mac

# 3. Access TrinityClaw
# Web UI:   http://localhost:8080
# API:      http://localhost:8001
# Docs:     http://localhost:8001/docs
# Preview:  http://localhost:8090  (web_builder live preview)
```

---

## Configuration

### LLM Setup

Choose your model source during installation:

#### Option A — Cloud Provider (requires API key)

| Parameter | Description | Example |
|-----------|-------------|---------|
| Model Name | LLM model identifier | `openai/moonshotai/kimi-k2-instruct` |
| API Base URL | Provider API endpoint | `https://integrate.api.nvidia.com/v1` |
| API Key Name | Environment variable name | `NVIDIA_API_KEY` |
| API Key Value | Your actual API key | `nvapi-xxxxx` |

#### Option B — Local Model via Ollama (no API key needed)

- Model: `qwen3.5:9b` (~6.6GB download on first start)
- Requires: 8GB+ VRAM (GPU) or 16GB+ RAM (CPU-only, slower)
- On **Linux**: pulled automatically by `ollama-entrypoint.sh` inside Docker on first startup
- On **Mac**: pulled during `install.sh` via native Ollama
- Start command: `docker compose --profile local up -d` (Linux) / `docker compose -f docker-compose.mac.yml up -d` (Mac)
- **Mac auto-start**: after running `install.sh`, TrinityClaw starts automatically on every login — no terminal needed. A `Start TrinityClaw.command` shortcut is placed on your Desktop as a manual backup.

```bash
# Linux — start with local Ollama model
docker compose --profile local up -d

# Monitor model download progress (Linux)
docker logs trinity-claw-ollama-1 -f

# If the model was not pulled automatically, pull it manually:

# Mac (native Ollama)
ollama pull qwen3.5:9b

# Linux (inside Docker container)
docker exec trinity-claw-ollama-1 ollama pull qwen3.5:9b
```

### Supported Providers

| Provider | Model Example | API Base |
|----------|---------------|----------|
| NVIDIA | `openai/moonshotai/kimi-k2-instruct` | `https://integrate.api.nvidia.com/v1` |
| OpenAI | `openai/gpt-4o` | `https://api.openai.com/v1` |
| Anthropic | `anthropic/claude-3-5-sonnet-20241022` | `https://api.anthropic.com/v1` |
| Moonshot | `openai/moonshot-v1-8k` | `https://api.moonshot.cn/v1` |
| Local (Ollama) | `ollama/llama3.2-vision` | `http://localhost:11434` |

### Configuration Files

```
trinity-claw/
├── .env                        # Secrets (API keys)
├── identity.md                 # Agent personality, values, and standing orders (editable)
└── config/
    └── litellm_config.yaml     # Model configuration
```

**identity.md** defines who TrinityClaw is — its values, communication style, and standing orders. Edit this file to customize the agent's personality without touching any Python code. Changes take effect on the next chat request (no restart needed).

**.env example:**
```env
NVIDIA_API_KEY=nvapi-xxxxx
LITELLM_MASTER_KEY=sk-trinity-local-key
TRINITY_API_KEY=aB3xK9mPqR7wN2vT5cY8jL1nD4hF6eG0   # auto-generated by installer
TAVILY_API_KEY=tvly-xxxxx                            # optional — get free key at tavily.com
```

**litellm_config.yaml example:**
```yaml
model_list:
  - model_name: trinity-default
    litellm_params:
      model: openai/moonshotai/kimi-k2-instruct
      api_key: os.environ/NVIDIA_API_KEY
      api_base: https://integrate.api.nvidia.com/v1

  # Vision model — used automatically when photos are sent.
  # If your main model supports vision (gpt-4o, claude-3-5-sonnet) set both to the same model.
  # For NVIDIA text-only models, use a dedicated vision model:
  - model_name: trinity-vision
    litellm_params:
      model: openai/meta/llama-3.2-90b-vision-instruct
      api_key: os.environ/NVIDIA_API_KEY
      api_base: https://integrate.api.nvidia.com/v1

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY
```

### Agent Security (API Key)

During installation, a **random `TRINITY_API_KEY` is automatically generated** and written to `.env`.
It is printed at the end of the installer — copy it and enter it **once** in the Web UI.

**How it works:**

| Step | Where | Action |
|------|-------|--------|
| Install | Installer | Generates random key → saves to `.env` |
| First use | Browser → Settings ⚙️ → Agent Security | Paste your key |
| Every use after | Browser `localStorage` | Sent automatically, nothing to do |
| Key changes? | Only if you re-run the installer | Enter new key in Settings once |

**What it protects:**
- `POST /config/update` — changing your LLM model/provider
- `POST /config/update-api-key` — writing API keys to `.env`
- `POST /config/telegram/update` — Telegram bot credentials
- `POST /config/model-source/set` — switching local/cloud

> **Note:** If `TRINITY_API_KEY` is not set in `.env`, the check is silently skipped (backward compatible). Read-only endpoints like `/health`, `/skills`, `/chat` are not protected by this key.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    TRINITYCLAW ARCHITECTURE                  │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   ┌─────────────────────────────────────────────────────┐  │
│   │                    app.py                            │  │
│   │         (Nervous System - Coordination)              │  │
│   │  ┌──────────┐ ┌──────────┐ ┌──────────┐            │  │
│   │  │Chat API  │ │  Skills  │ │  Memory  │            │  │
│   │  │          │ │ Registry │ │ Manager  │            │  │
│   │  └──────────┘ └────┬─────┘ └──────────┘            │  │
│   └────────────────────┼────────────────────────────────┘  │
│                        │                                   │
│              ┌─────────┴─────────┐                        │
│              ▼                   ▼                        │
│   ┌─────────────────┐  ┌─────────────────┐               │
│   │  skills/core/   │  │ skills/dynamic/ │               │
│   │  (Read-only)    │  │  (Read-write)   │               │
│   │  - notes        │  │  - (AI creates) │               │
│   │  - files        │  │                 │               │
│   │  - web          │  │                 │               │
│   │  - code_executor│  │                 │               │
│   │  - create_skill │  │                 │               │
│   │  - git_manager  │  │                 │               │
│   │  - scheduler    │  │                 │               │
│   │  - telegram_bot │  │                 │               │
│   └─────────────────┘  └─────────────────┘               │
│                                                             │
│   ┌─────────────────────────────────────────────────────┐  │
│   │    LiteLLM ◄────────► External LLM Provider         │  │
│   └─────────────────────────────────────────────────────┘  │
│                                                             │
│   ┌─────────────────────────────────────────────────────┐  │
│   │    ChromaDB + JSONL ◄─── Persistent Memory          │  │
│   └─────────────────────────────────────────────────────┘  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## Core Skills (20)

| Skill | Description | Functions |
|-------|-------------|-----------|
| `notes` | Persistent note storage | `save`, `load`, `list_notes`, `delete`, `search`, `export_all`, `get_last_logs`, `search_logs` |
| `files` | File operations (read anywhere, write to memory/dynamic) | `ls`, `cat`, `pwd`, `exists`, `size`, `sha256`, `write`, `append`, `patch`, `patch_all`, `mkdir`, `delete`, `tree`, `find_duplicates` |
| `web` | Web browsing + browser automation. `search()` auto-tries Tavily → DuckDuckGo → Bing (set `TAVILY_API_KEY` for best results) | `fetch`, `get`, `head`, `read`, `search`, `download`, `scrape`, `scrape_links`, `scrape_images`, `scrape_table`, `extract_meta`, `browser_goto`, `browser_click`, `browser_type`, `browser_screenshot`, `browser_text`, `browser_fill`, `browser_evaluate`, `browser_wait`, `browser_links`, `browser_inputs`, `browser_scroll`, `browser_press`, `browser_close`, `status` |
| `code_executor` | Safe Python sandbox + math evaluator | `run_snippet`, `test_skill`, `run_bash`, `calc`, `status` |
| `create_skill` | Create new skills dynamically | `create_new_skill`, `reload` |
| `scheduler` | Schedule one-time and recurring tasks | `schedule`, `schedule_recurring`, `remove`, `list_tasks`, `clear`, `status`, `parse_preview` |
| `git_manager` | Git repository management (read + write) | `status`, `is_repo`, `list_repos`, `branches`, `head`, `diff`, `log`, `add`, `commit`, `pull`, `push` |
| `document_parser` | Parse PDF, DOCX, XLSX, CSV, TXT, JSON, YAML | `read`, `extract_tables`, `metadata`, `summarize`, `convert_to_text`, `list_supported` |
| `data_science` | EDA, ML prediction, NLP, charts | `analyze_dataset`, `predict_column`, `analyze_text_column`, `create_chart`, `status` |
| `image_viewer` | View and inspect local images | `view_image`, `list_images`, `inspect_image` |
| `url_monitor` | Monitor URLs for changes and health | `add_url`, `get_status_summary`, `get_url_history`, `get_recent_changes`, `check_all_urls`, `check_url_now`, `remove_url` |
| `self_improvement` | Audit, auto-fix, and learn from skill errors | `audit`, `fix`, `prevent`, `report`, `suggest_tests`, `learn_from_feedback` |
| `dashboard` | System health and monitoring | `health`, `alerts`, `processes` |
| `terminal` | Whitelisted shell commands inside container | `run` |
| `google_calendar` | Google Calendar read/write | `authorize`, `list_events`, `create_event`, `delete_event`, `update_event`, `find_free_slots`, `status` |
| `telegram_bot` | Telegram messaging and polling | `setup`, `send`, `queue_message`, `send_photo`, `send_document`, `start_polling`, `stop`, `status`, `test_connection` |
| `email_sender` | Email via SMTP or SendGrid | `send`, `send_html`, `send_with_attachment`, `test`, `status` |
| `web_builder` | Build & preview HTML/CSS/JS websites | `scaffold`, `write_file`, `patch_file`, `read_file`, `delete_file`, `delete_project`, `export_zip`, `list_projects`, `serve`, `stop_server`, `server_status`, `validate` |
| `knowledge_base` | Business knowledge base backed by ChromaDB | `ingest_folder`, `ingest_file`, `search`, `list_ingested`, `delete_document`, `status` |
| `gmail` | Read Gmail inbox via Gmail API | `authorize`, `list_inbox`, `read_email`, `search_emails`, `get_attachments`, `mark_read`, `summarize_inbox`, `status` |

---

## Skill Invocation

TrinityClaw uses XML-style skill tags:

```
<skill:skillname.function>arg1,arg2</skill:skillname.function>
```

### Examples

```
# List files
<skill:files.ls>/app/memory</skill:files.ls>

# Save a note
<skill:notes.save>MyNote,This is important information</skill:notes.save>

# Read a note
<skill:notes.load>MyNote</skill:notes.load>

# Create a skill
<skill:create_skill.create_new_skill>greeter,def greet(name): return f"Hello {name}!"</skill:create_skill.create_new_skill>

# Quick math (no subprocess)
<skill:code_executor.calc>sqrt(144) + 2**8</skill:code_executor.calc>

# Run a Python snippet
<skill:code_executor.run_snippet>print(sum(range(100)))</skill:code_executor.run_snippet>

# Git: check status
<skill:git_manager.status>/app/myrepo</skill:git_manager.status>

# Git: show last 5 commits
<skill:git_manager.log>/app/myrepo,5</skill:git_manager.log>

# Git: stage all and commit
<skill:git_manager.add>/app/myrepo,.</skill:git_manager.add>
<skill:git_manager.commit>/app/myrepo,Fix bug in parser</skill:git_manager.commit>

# Git: pull latest
<skill:git_manager.pull>/app/myrepo</skill:git_manager.pull>

# Fetch webpage (static)
<skill:web.fetch>https://example.com</skill:web.fetch>

# Navigate browser to a JS-rendered page
<skill:web.browser_goto>https://example.com</skill:web.browser_goto>

# Take a screenshot
<skill:web.browser_screenshot>/app/memory/shot.png,true</skill:web.browser_screenshot>

# Extract visible text (after JS execution)
<skill:web.browser_text></skill:web.browser_text>

# Click a button
<skill:web.browser_click>#submit-btn</skill:web.browser_click>

# Type into a search field
<skill:web.browser_type>input[name=q],hello world</skill:web.browser_type>

# Run JavaScript on the page
<skill:web.browser_evaluate>document.title</skill:web.browser_evaluate>

# Close browser when done
<skill:web.browser_close></skill:web.browser_close>

# Schedule a one-time task
<skill:scheduler.schedule>morning_check,tomorrow at 9am,check the news</skill:scheduler.schedule>

# Schedule a recurring task
<skill:scheduler.schedule_recurring>daily_report,24h,summarize today's notes</skill:scheduler.schedule_recurring>

# Create a landing page project
<skill:web_builder.scaffold>my-portfolio,landing</skill:web_builder.scaffold>

# Write/update a file in the project
<skill:web_builder.write_file>my-portfolio,style.css,body { background: #1a1a2e; color: #eee; }</skill:web_builder.write_file>

# Start the live preview server (open http://localhost:8090)
<skill:web_builder.serve>my-portfolio</skill:web_builder.serve>

# Validate index.html structure
<skill:web_builder.validate>my-portfolio</skill:web_builder.validate>

# List all projects
<skill:web_builder.list_projects></skill:web_builder.list_projects>

# Stop the preview server
<skill:web_builder.stop_server></skill:web_builder.stop_server>

# Google Calendar — list next 7 days
<skill:google_calendar.list_events>7</skill:google_calendar.list_events>

# Google Calendar — create an event
<skill:google_calendar.create_event>Team Meeting,2026-03-01,09:00,60,Weekly sync</skill:google_calendar.create_event>

# Google Calendar — find free slots
<skill:google_calendar.find_free_slots>tomorrow,30</skill:google_calendar.find_free_slots>

# Google Calendar — update event title
<skill:google_calendar.update_event>EVENT_ID,title,New Title</skill:google_calendar.update_event>

# Google Calendar — delete an event
<skill:google_calendar.delete_event>EVENT_ID</skill:google_calendar.delete_event>
```

---

## Creating Custom Skills

### Skill Lifecycle

1. **Agent creates skill** — saved to `skills/dynamic/` and immediately usable
2. **You inspect** the generated file at `agent/skills/dynamic/your_skill.py`
3. **You move it** to `agent/skills/core/your_skill.py` once satisfied (optional — for permanent core status)
4. **You tell the agent** `"activate your_skill"` — the agent calls `create_skill.reload` which reloads all skills from both folders

> Skills in `dynamic/` are callable right after creation. Moving to `core/` just makes them part of the read-only system layer that persists across container rebuilds.

### Skill Template

```python
# skills/dynamic/my_skill.py

NAME = "my_skill"
DOC = "Description of what this skill does"

def my_function(arg1: str, arg2: str = "default") -> str:
    """
    Function description.
    
    Args:
        arg1: First argument
        arg2: Second argument (optional)
    
    Returns:
        Result string
    """
    result = f"Processed {arg1} with {arg2}"
    return result

# Add more functions as needed
def another_function():
    return "Another result"
```

### Security Restrictions

**Banned modules:**
- `shutil`, `threading`, `multiprocessing`
- `subprocess`, `socket`, `ctypes`
- Any module with system-level access

**Banned functions:**
- `eval()`, `exec()`, `compile()`
- `fork()`, `spawn()`

### Skill Best Practices

1. **Return strings** - All functions should return str
2. **Handle errors** - Use try/except blocks
3. **Keep it simple** - One skill = one purpose
4. **Document** - Use clear function docstrings
5. **Validate input** - Check arguments before processing

---

## Telegram Integration

### Setup

1. **Create Telegram Bot:**
   - Open Telegram, search `@BotFather`
   - Send `/newbot`, follow instructions
   - Copy the bot token

2. **Get Your Chat ID:**
   - Search `@userinfobot` on Telegram
   - Send any message
   - Copy your Chat ID number

3. **Configure TrinityClaw:**
```
<skill:telegram_bot.setup>YOUR_BOT_TOKEN,YOUR_CHAT_ID</skill:telegram_bot.setup>
<skill:telegram_bot.start_polling></skill:telegram_bot.start_polling>
```

4. **Test:** Message your bot on Telegram!

### Supported Message Types

| Type | Telegram | Web UI |
|------|----------|--------|
| Text | ✅ | ✅ |
| Voice / audio | ✅ Transcribed by local Whisper | ✅ Record with 🎤 button |
| Photos / images | ✅ Described by vision model | ✅ Attach with 📎 button |

> **Voice transcription** uses [faster-whisper](https://github.com/SYSTRAN/faster-whisper) running locally inside Docker — no API key required. The model (~150 MB) downloads automatically on the first voice message and is cached in the `memory/` volume.

> **Image vision** requires a vision-capable model in `trinity-vision` (see Configuration above). For local Ollama mode, `llama3.2-vision` handles images automatically.

### Security

- Only YOUR Chat ID can interact with the bot
- No open ports required (polling mode)
- All traffic over HTTPS

---

## Google Calendar Integration

### Prerequisites

- A Google account
- Docker container running (standard setup — no extra ports needed)

### Setup (one-time, ~10 minutes)

#### Step 1 — Google Cloud Console

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (name it anything, e.g. "TrinityClaw")
3. **APIs & Services → Library** → search **Google Calendar API** → Enable
4. **APIs & Services → Credentials → + Create Credentials → OAuth client ID**
5. If prompted to configure the consent screen:
   - Choose **External**
   - Fill in an app name → Save
   - Skip Scopes → Save
   - **Audience tab → Test users → + Add users** → add your Gmail address → Save
6. Back at Create Credentials: select **User data**, click Next
7. Under credential type choose **Desktop app** → Create
8. **Download JSON** → rename the file to `gcal_credentials.json`

> **Important:** On the "What data will you be accessing?" screen, choose **User data** (not Application data). Application data creates a service account which won't work for personal calendars.

#### Step 2 — Copy credentials into the container

```powershell
# Run from the trinity-claw folder
docker cp gcal_credentials.json trinity-claw-trinity-agent-1:/app/memory/gcal_credentials.json
```

#### Step 3 — Authorize (one-time browser step)

Tell your agent:
> "Authorize google calendar"

The agent will give you a URL. Open it in your browser, sign in with the same Google account you added as a test user, click **Allow**, copy the code Google shows you, and paste it back:
> "authorize(PASTE_CODE_HERE)"

The token is saved to `/app/memory/gcal_token.json` and **auto-refreshes forever** — you never need to do this again.

### Usage Examples

Just talk to the agent naturally:

| Say to the agent | What happens |
|---|---|
| "What's on my calendar this week?" | Lists next 7 days of events |
| "Schedule a meeting tomorrow at 2pm for 30 minutes" | Creates the event |
| "When am I free on Friday for 1 hour?" | Finds open slots (8am–8pm window) |
| "Move that meeting to 3pm" | Updates the event time |
| "Delete the standup event" | Deletes by event ID |
| "Check google calendar status" | Shows auth and token health |

### Token Notes

- The token auto-refreshes as long as `refresh_token` is present in `/app/memory/gcal_token.json`
- If you ever see auth errors after a long time, revoke the app at [myaccount.google.com/permissions](https://myaccount.google.com/permissions) and re-run `authorize()`
- The `gcal_credentials.json` file stays in `/app/memory/` — it survives container rebuilds because `/app/memory/` is a Docker volume

---

## Gmail Integration

TrinityClaw supports Gmail in two modes — **sending** via SMTP (simple, works immediately) and **reading** via the Gmail API (full inbox access).

---

### Part 1 — Sending Email via Gmail SMTP

This is the quickest way to get email sending working.

#### Step 1 — Generate a Gmail App Password

Regular Gmail passwords won't work with SMTP. You need an App Password:

1. Make sure **2-Factor Authentication** is enabled on your Google account:
   → https://myaccount.google.com/security

2. Generate an App Password:
   → https://myaccount.google.com/apppasswords
   - App name: type anything (e.g. `TrinityClaw`)
   - Click **Create**
   - Copy the **16-character code** Google shows you

#### Step 2 — Add to .env

```env
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your@gmail.com
SMTP_PASSWORD=abcdefghijklmnop
```

Then restart:
```bash
docker-compose restart trinity-agent
```

#### Usage Examples

Just tell the agent naturally:

| Say to the agent | What happens |
|---|---|
| "Send an email to john@example.com about the meeting" | Drafts and sends via Gmail SMTP |
| "Email me a summary of today's tasks" | Sends to your configured address |
| "Send the report.pdf to client@company.com" | Sends with attachment |

---

### Part 2 — Reading Gmail Inbox (Gmail API)

Full read access to your inbox — list, search, read, download attachments.

#### Prerequisites

- Google Cloud project already set up (same one used for Calendar)
- `gcal_credentials.json` already in `/app/memory/`

#### Step 1 — Enable Gmail API

Go to: https://console.cloud.google.com/apis/library/gmail.googleapis.com

Click **Enable** (same project as Calendar — no new credentials needed).

#### Step 2 — Authorize (one-time)

Tell your agent:
> "authorize gmail"

The agent will give you a URL. Open it, sign in, click **Allow**, copy the code, and paste it back:
> "authorize gmail with CODE"

Token saved to `/app/memory/gmail_token.json` — auto-refreshes forever.

#### Usage Examples

| Say to the agent | What happens |
|---|---|
| "Summarize my inbox" | Shows last 10 emails with sender, subject, preview |
| "Do I have any unread emails from John?" | Searches `from:john is:unread` |
| "Read the email about the invoice" | Fetches and displays full body |
| "Download the attachments from that email" | Saves to `memory/email_attachments/` |
| "Mark that as read" | Removes UNREAD label |

#### Gmail Search Operators

The agent understands all standard Gmail search operators:

```
from:john@example.com
subject:invoice
is:unread
has:attachment
after:2026/1/1
from:boss@company.com is:unread has:attachment
```

#### Token Notes

- Token stored at `/app/memory/gmail_token.json` — separate from Calendar token
- Auto-refreshes as long as a `refresh_token` is present
- If expired: revoke at myaccount.google.com/permissions then call `authorize gmail` again

---

## Business Knowledge Base

Teach TrinityClaw about your business by dropping documents into a folder. Once ingested, the agent searches your documents automatically before answering business questions — no manual prompting needed.

### How It Works

1. Drop files into `memory/knowledge/` on your host machine
2. Tell the agent: *"ingest my knowledge folder"* (or call it directly)
3. Ask questions naturally — the agent searches your docs first

Documents are chunked, embedded, and stored in a dedicated ChromaDB collection (`business_knowledge`) that is **separate from conversation memory**.

### Supported File Types

PDF, DOCX, DOC, XLSX, XLS, CSV, TXT, MD, JSON, YAML, XML, HTML and more.

### Usage Examples

```
# Index all files in the knowledge folder
<skill:knowledge_base.ingest_folder></skill:knowledge_base.ingest_folder>

# Index a custom folder path
<skill:knowledge_base.ingest_folder>/app/memory/knowledge/</skill:knowledge_base.ingest_folder>

# Ingest a single file
<skill:knowledge_base.ingest_file>meeting-notes-jan.docx</skill:knowledge_base.ingest_file>

# Search your business documents
<skill:knowledge_base.search>refund policy</skill:knowledge_base.search>

# Search with more results
<skill:knowledge_base.search>Q1 sales targets,10</skill:knowledge_base.search>

# List everything that has been indexed
<skill:knowledge_base.list_ingested></skill:knowledge_base.list_ingested>

# Remove a document from the index
<skill:knowledge_base.delete_document>old-contract.pdf</skill:knowledge_base.delete_document>

# Check collection health
<skill:knowledge_base.status></skill:knowledge_base.status>
```

### Re-ingestion & Change Detection

`ingest_folder()` uses **MD5 hash comparison** — if a file hasn't changed since the last ingest, it is skipped automatically. Updated files are re-chunked and replaced in ChromaDB. You can safely run `ingest_folder()` as often as you want.

### Automatic Behavior

The agent's identity is configured to:
- **Always search the knowledge base first** when asked about meetings, SOPs, clients, policies, or any business-specific topic
- **Proactively remind you** to ingest when you mention dropping or uploading files
- **Never say "I don't have that information"** for business questions without searching first

### Scheduling Automatic Re-ingestion

Use the `scheduler` skill to keep the knowledge base fresh automatically:

```
# Re-ingest every morning at 8am
<skill:scheduler.schedule_recurring>kb_sync,24h,ingest my knowledge folder</skill:scheduler.schedule_recurring>
```

---

## API Reference

### Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/health` | System health check |
| `GET` | `/skills` | List all loaded skills |
| `POST` | `/skills/reload` | Reload skills from core/ and dynamic/ (no auth required) |
| `POST` | `/skill/call` | Call a skill function |
| `POST` | `/chat` | Send message to agent (supports `image` field for vision) |
| `POST` | `/transcribe` | Transcribe base64 audio via local Whisper |

### Examples

**Health check:**
```bash
curl http://localhost:8001/health
```

**List skills:**
```bash
curl http://localhost:8001/skills
```

**Chat with agent:**
```bash
curl -X POST http://localhost:8001/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello TrinityClaw!"}'
```

**Call skill directly:**
```bash
curl -X POST http://localhost:8001/skill/call \
  -H "Content-Type: application/json" \
  -d '{"skill": "notes", "function": "list", "args": []}'
```

---

## Project Structure

```
trinity-claw/
├── docker-compose.yml          # Docker orchestration
├── install.ps1                 # Windows installer
├── install.sh                  # Linux/Mac installer
├── .env                        # Secrets (not in git)
├── .env.example                # Secrets template
├── identity.md                 # Agent personality and standing orders (editable)
├── README.md                   # This file
│
├── agent/                      # TrinityClaw agent
│   ├── Dockerfile
│   ├── app.py                  # Main application
│   ├── requirements.txt
│   ├── ollama-entrypoint.sh    # Auto-pulls local model on startup
│   └── skills/
│       ├── __init__.py
│       ├── core/               # System skills (read-only)
│       │   ├── notes.py
│       │   ├── files.py
│       │   ├── web.py
│       │   ├── code_executor.py
│       │   ├── create_skill.py
│       │   ├── git_manager.py
│       │   ├── scheduler.py
│       │   ├── document_parser.py
│       │   ├── data_science.py
│       │   ├── image_viewer.py
│       │   ├── url_monitor.py
│       │   ├── self_improvement.py
│       │   ├── dashboard.py
│       │   ├── terminal.py
│       │   ├── telegram_bot.py
│       │   ├── email_sender.py
│       │   ├── web_builder.py
│       │   ├── google_calendar.py
│       │   ├── knowledge_base.py
│       │   └── gmail_reader.py
│       └── dynamic/            # User skills (AI agent can read-write)
│
├── config/
│   └── litellm_config.yaml     # LLM configuration
│
├── memory/                     # Persistent storage
│   ├── session_logs.jsonl      # Conversation history
│   ├── knowledge/              # Drop business documents here for ingestion
│   └── websites/               # web_builder projects (served on port 8090)
│
└── web/
    └── index.html              # Web UI
```

---

## Maintenance

### Basic Commands

```bash
# Start services
docker-compose up -d

# Stop services
docker-compose down

# Restart services
docker-compose restart

# View logs
docker logs trinity-claw-trinity-agent-1 --tail 50

# Rebuild after adding core skills
docker-compose down
docker-compose build --no-cache trinity-agent
docker-compose up -d
```

### Backup

```bash
# Backup memory
cp -r memory/ backup_$(date +%Y%m%d)/

# Backup everything
tar -czvf trinity_backup_$(date +%Y%m%d).tar.gz memory/ config/ .env
```

### Reset

```bash
# Soft reset (keeps memory)
docker-compose down
docker-compose up -d

# Hard reset (deletes everything!)
docker-compose down -v
rm -rf memory/
docker-compose up -d
```

---

## Troubleshooting

### Common Issues

| Issue | Solution |
|-------|----------|
| **Port 8001 in use** | Change port in `docker-compose.yml` |
| **Skill not found** | Run `POST /skills/reload` or rebuild |
| **Memory errors** | Increase Docker memory limit to 4GB+ |
| **API errors** | Check `.env` has correct API key |
| **Telegram not responding** | Verify token and chat ID, check logs |
| **Google Calendar auth loop** | Run `docker compose build trinity-agent` then `docker compose up -d` to pick up the latest skill code, then re-authorize |
| **Google Calendar invalid_grant** | Each code is single-use and expires in 10 min. Call `authorize()` again for a fresh URL |
| **Google Calendar token expired** | Revoke at myaccount.google.com/permissions, then call `authorize()` again |
| **Google Calendar Access blocked** | Add your Gmail to Test Users: Google Cloud Console → APIs & Services → OAuth consent screen → Audience → Test users |
| **Ollama model not pulled / missing** | Mac: run `ollama pull qwen3.5:9b`. Linux: run `docker exec trinity-claw-ollama-1 ollama pull qwen3.5:9b`. On Linux the pull happens inside Docker on first startup (~6.6GB) — check progress with `docker logs trinity-claw-ollama-1 -f` |
| **Voice not transcribed** | First use downloads Whisper model — wait up to 2 min. Check logs: `docker logs trinity-claw-trinity-agent-1 \| grep -i whisper` |
| **Image described incorrectly** | Ensure `trinity-vision` in `litellm_config.yaml` points to a vision-capable model, then `docker compose restart litellm` |
| **Browser skill not working** | Rebuild the container: `docker-compose build --no-cache trinity-agent && docker-compose up -d`. Chromium installs during build. |
| **Browser times out on JS pages** | Try `browser_goto` with `wait_until=load` instead of `networkidle` for heavy SPAs |
| **Gmail not authorized** | Run `gmail.authorize()`, open the URL, click Allow, paste the code back. |
| **Gmail token expired** | Revoke at myaccount.google.com/permissions then call `gmail.authorize()` again. |
| **Gmail API not enabled** | Go to Google Cloud Console → APIs & Services → Library → Gmail API → Enable. |
| **Knowledge base not finding results** | Run `ingest_folder()` first. Check `status()` to confirm ChromaDB is connected. |
| **Search returns no results / blocked** | Set `TAVILY_API_KEY` in `.env` for reliable search. Without it, `search()` falls back to DuckDuckGo → Bing scraping which can be rate-limited. |
| **Knowledge base ChromaDB error** | Ensure ChromaDB container is running: `docker-compose ps`. Restart with `docker-compose restart chroma`. |
| **File not ingested** | Check the file extension is supported (`list_supported` via `document_parser`). PDF needs `pdfplumber` — it's in `requirements.txt`. |

### Debug Commands

```bash
# Check container status
docker-compose ps

# View agent logs
docker logs trinity-claw-trinity-agent-1 --tail 100

# Check LiteLLM logs
docker logs trinity-claw-litellm-1 --tail 100

# Test API
curl http://localhost:8001/health

# List loaded skills
curl http://localhost:8001/skills
```

---

## Contributing

1. Fork the repository
2. Create feature branch (`git checkout -b feature/amazing-feature`)
3. Commit changes (`git commit -m 'Add amazing feature'`)
4. Push to branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

## License

MIT License - see LICENSE file for details.

---

## Support

- **Issues**: [GitHub Issues](https://github.com/TriniClaw/trinity-claw/issues)
- **Discussions**: [GitHub Discussions](https://github.com/TrinityClaw/trinity-claw/discussions)

---

**Version**: TrinityClaw v1.3.0
**Last Updated**: 2026-03-08
