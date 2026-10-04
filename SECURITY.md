# Security

## 🖥️ Local Use Only

**TrinityClaw is designed for personal local servers — do not deploy it on a VPS or any public-facing server.**

The agent API has no rate limiting and no multi-user authentication layer. The `code_executor` and `terminal` skills run arbitrary code inside Docker, and the single `TRINITY_API_KEY` is not sufficient protection for an internet-exposed host. Running this on a public IP without additional firewall rules and a reverse proxy with proper auth could expose your system to unauthorized access.

**Recommended setup:** run on your home machine or a local network server, accessed via your LAN or a private VPN (e.g. Tailscale, WireGuard). Do **not** open ports 8001, 8080, or 8090 to the public internet.

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

## Reporting a Vulnerability

Please open a private security advisory on the repository (GitHub → **Security** → **Advisories** → **Report a vulnerability**) rather than a public issue. Do not disclose exploitable details publicly until a fix is available.
