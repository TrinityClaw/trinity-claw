# Social Media DM Guidance — TrinityClaw

This guide tells the agent how to read, interpret, and respond to direct messages on
Twitter/X, LinkedIn, Instagram, and Gmail (browser) using the `browser_session` skill.

> **Gmail note:** The `gmail` skill (OAuth API) remains the preferred method for programmatic
> inbox access, search, and draft-approval workflows. The Gmail browser section below is for
> cases where you ask the agent to work inside your actual open Gmail tab — reading and replying
> visually, exactly as you would. Use whichever fits the task. Never mix the two in the same flow.

The agent should always use a **friendly, warm, and informative tone** — like a knowledgeable
team member who genuinely enjoys helping people, not a corporate auto-responder.

---

## General Rules (Apply to All Platforms)

1. **Verify the page first.** Before touching any inbox, call `get_text()` to confirm the right page is open and read the current content.
2. **Read before replying.** Use `get_text()` or `get_html()` to fully read the message thread before composing a response.
3. **Verify before sending.** Always call `get_text()` on the current page to confirm your reply is typed correctly before clicking Send.
4. **One message at a time.** Never batch-reply to multiple conversations in a single flow. Handle one thread completely, then move to the next.
5. **When in doubt, escalate.** If a message is ambiguous, sensitive, urgent, or involves money/contracts — do NOT reply. Flag it to the user instead.
6. **Log all sent replies.** After each reply is confirmed sent, call `notes.write("dm_log", ...)` with the platform, username, timestamp, and a short summary of the reply.
7. **Never impersonate or over-promise.** If something requires the user's personal sign-off (pricing, scheduling, formal agreements), say you'll pass the message along.
8. **Match the energy.** If someone writes one casual sentence, don't reply with five formal paragraphs. Mirror their length and warmth.

---

## Tone & Voice Guidelines

- **Friendly and human** — avoid robotic phrases like "Your inquiry has been received."
- **Informative but concise** — answer what was asked clearly, without overloading with info.
- **Warm opener** — start with the person's name or a short acknowledgment (e.g., "Hey [Name]! 👋" or "Hi [Name], thanks for reaching out!").
- **Clear next step** — end every reply with what happens next (a question, a link, or a CTA).
- **Avoid filler** — no "Hope this email finds you well" equivalents. Jump to the helpful part.

**Example of good tone:**
> "Hey Sarah! Great question — yes, we do offer custom packages. I'll have someone from the team reach out today with details. In the meantime, feel free to check out [link] 😊"

**Example of tone to avoid:**
> "Hello, thank you for your message. We have received your inquiry and will respond in due course."

---

## Twitter / X — DM Workflow

### Selectors

| Action | Selector |
|--------|----------|
| Open DM inbox | `[data-testid="AppTabBar_DirectMessage_Link"]` |
| First/top conversation | `[data-testid="conversation"]` (first result) |
| Message thread text | `[data-testid="messageEntry"]` |
| DM reply input box | `[data-testid="dmComposerTextInput"]` |
| Send button | `[data-testid="dmComposerSendButton"]` |

### Step-by-Step: Read & Reply to a DM

```
1. browser_session.click('[data-testid="AppTabBar_DirectMessage_Link"]')
2. browser_session.get_text()
3. browser_session.click('[data-testid="conversation"]')
4. browser_session.get_text()
5. (Compose reply using tone guidelines above)
6. browser_session.type_text('[data-testid="dmComposerTextInput"]', "Your reply here")
7. browser_session.click('[data-testid="dmComposerSendButton"]')
8. notes.write("dm_log", "Twitter DM sent to [username] — [summary of reply]")
```

### When to Escalate (Do Not Reply — Tell the User)
- Message contains a legal question or complaint
- Message asks for pricing or a quote
- Message appears to be a scam or phishing attempt
- Message is in a language you're not confident responding in
- Message is hostile or confrontational

---

## LinkedIn — DM Workflow

### Selectors

| Action | Selector |
|--------|----------|
| Open messaging inbox | `[href="/messaging/"]` or click the chat bubble icon in top nav |
| Conversation list | `.msg-conversations-container__conversations-list` |
| Individual conversation | `.msg-conversation-listitem__link` (first unread) |
| Message thread content | `.msg-s-message-list__event` |
| Reply input box | `.msg-form__contenteditable[contenteditable="true"]` |
| Send button | `.msg-form__send-button` or `button[type="submit"]` |

### Step-by-Step: Read & Reply to a LinkedIn DM

```
1. browser_session.goto("https://www.linkedin.com/messaging/")
2. browser_session.get_text()
3. browser_session.click('.msg-conversation-listitem__link')
4. browser_session.get_text()
5. (Compose reply — slightly more professional than Instagram/Twitter. Use "Hi [Name]," not "Hey")
6. browser_session.type_text('.msg-form__contenteditable[contenteditable="true"]', "Your reply here")
7. browser_session.press_key("Control+Enter")
8. notes.write("dm_log", "LinkedIn DM sent to [name] — [summary of reply]")
```

### LinkedIn-Specific Tone Notes
- Start with "Hi [First Name]," — never "Hey" on LinkedIn
- Keep a slightly more professional register than other platforms
- If someone sent a connection request with a note, acknowledge it warmly before replying
- Great for: partnership inquiries, collaboration requests, professional questions
- Escalate: recruitment spam, sales pitches that need a decision, anything involving contracts

### If LinkedIn Selectors Fail
LinkedIn updates their DOM frequently. Run:
```
browser_session.get_html(selector=".msg-overlay-list-bubble")
```
or
```
browser_session.get_html(selector="main")
```
to inspect current structure and find updated selectors.

---

## Instagram — DM Workflow

### Selectors

| Action | Selector |
|--------|----------|
| Open DM inbox | `[aria-label="Direct messaging"]` or click the paper plane icon |
| Conversation list | `.x9f619` (container — use get_html to verify current class) |
| Individual conversation | `[role="listbox"] > div` (first unread item) |
| Message thread text | Use `get_text()` after opening a conversation |
| Reply input box | `[aria-label="Message..."]` or `[placeholder="Message..."]` |
| Send button | `[type="submit"]` within the DM form, or press Enter |

### Step-by-Step: Read & Reply to an Instagram DM

```
1. browser_session.goto("https://www.instagram.com/direct/inbox/")
2. browser_session.get_text()
3. browser_session.click('[role="listbox"] > div')
4. browser_session.get_text()
5. (Compose reply — most casual and emoji-friendly of the platforms)
6. browser_session.type_text('[aria-label="Message..."]', "Your reply here")
7. browser_session.press_key("Enter")
8. notes.write("dm_log", "Instagram DM sent to [username] — [summary of reply]")
```

### Instagram-Specific Notes
- Emojis are welcome and expected — keep replies warm and visual
- Short replies do well here: 1–3 sentences max for most conversations
- Common message types: product questions, collab requests, fan/follower messages, support issues
- Escalate: complaints about orders or services, influencer/paid partnership inquiries, anything requiring commitment

### If Instagram Selectors Fail
Instagram rebuilds their DOM frequently. Run:
```
browser_session.get_html(selector="section")
```
then scan for `aria-label` attributes to find the current correct selectors.

---

## Gmail — Browser Session Workflow

> **When to use this vs. the OAuth skill:**
> - Use `gmail` skill (OAuth) for: searching, summarizing, bulk reading, draft-approval flows, IMAP access
> - Use `browser_session` Gmail below for: visually reading and replying inside your open Gmail tab,
>   exactly as you would yourself — no API setup required beyond being logged in
>
> **CRITICAL — Do NOT fall back to email_sender or SMTP:**
> If the user asks to send an email via the browser tab, use ONLY `browser_session`.
> If a selector fails, try the fallback selectors listed below or call `get_html` to discover the current DOM.
> Never silently switch to `email_sender.send()` — that bypasses the browser entirely.
> If `browser_session` fails after trying all fallbacks, stop and report the error to the user.

### Selectors

> **Language note:** This Gmail account uses the Serbian interface (`Примљено` = Inbox).
> `aria-label` and `data-tooltip` values are translated — do NOT use English text in selectors.
> Always prefer the language-independent selectors listed below (`[gh]`, `[name]`, `[jsaction]`).

| Action | Selector |
|--------|----------|
| Inbox (primary tab) | `browser_session.goto("https://mail.google.com/mail/u/0/#inbox")` |
| First unread email row | `tr.zA.zE` (unread = class `zE`; read = class `zO`) — class-based, language-safe |
| Open an email | `browser_session.click('tr.zA.zE')` |
| Email subject / body content | `get_text()` after opening |
| Reply button (inside open email) | `[data-tooltip^="Odg"]` — Serbian prefix; fallback: `[jsaction*="reply"]` |
| Reply compose area | `div[contenteditable="true"][aria-multiline="true"]` or `div[g_editable="true"]` |
| Send button (in reply) | `[jsaction*="send"]` or `[data-tooltip^="Pošalji"]` |
| Search inbox | `input[name="q"]` — language-independent |
| **Compose new email** | `[gh="cm"]` — language-independent, always works |
| To field (new compose) | `[name="to"]` — language-independent |
| Subject field (new compose) | `[name="subjectbox"]` — language-independent |
| Body (new compose) | `div[contenteditable="true"][aria-multiline="true"]` |

### Step-by-Step: Read & Reply to an Email

```
1. browser_session.goto("https://mail.google.com/mail/u/0/#inbox")
2. browser_session.get_text()
3. browser_session.click('tr.zA.zE')
4. browser_session.get_text()
5. (Compose reply using tone guidelines — see below)
6. browser_session.click('[data-tooltip^="Odg"]')
   → Fallback: browser_session.get_html(selector="[jsaction*='reply']") to find the button
7. browser_session.type_text('div[contenteditable="true"][aria-multiline="true"]', "Your reply here")
8. browser_session.press_key("Control+Enter")
9. notes.write("dm_log", "Gmail reply sent to [sender] — Subject: [subject] — [summary]")
```

### Step-by-Step: Compose a New Email

```
1. browser_session.goto("https://mail.google.com/mail/u/0/#inbox")
2. browser_session.click('[gh="cm"]')
3. browser_session.type_text('[name="to"]', "recipient@email.com")
4. browser_session.press_key("Tab")
5. browser_session.type_text('[name="subjectbox"]', "Subject line here")
6. browser_session.type_text('div[contenteditable="true"][aria-multiline="true"]', "Email body here")
7. browser_session.press_key("Control+Enter")
```

### Multiple Gmail Accounts

If you have more than one Gmail account open in Chrome, the URL path determines which one:
- Account 1 (primary): `https://mail.google.com/mail/u/0/`
- Account 2: `https://mail.google.com/mail/u/1/`
- Account 3: `https://mail.google.com/mail/u/2/`

Always ask the user which account to use if multiple are open.

### Gmail-Specific Tone Notes
- Email allows more length than DMs — 2–4 sentences is normal for a reply
- Start with "Hi [First Name]," — match formality to what was received
- Sign off as "Trinity" (or the user's preferred sign-off — check `notes.read()`)
- For business emails: professional but warm, never stiff
- For personal/casual emails: match the sender's energy
- **Never reply to:** newsletters, automated notifications, receipts, marketing emails — these are not real conversations

### If Gmail Selectors Fail
Gmail's DOM uses obfuscated class names that can change. If a selector stops working:
```
browser_session.get_html(selector="[role='main']")
```
Look for `aria-label` and `data-tooltip` attributes — Google uses these consistently and they
are more stable than class names.

---

## Viber — Browser Session Workflow

> **Setup required:** Viber desktop app must be installed on this machine and paired with your phone.
> Open `https://web.viber.com` in Chrome and scan the QR code once — after that it stays connected
> as long as the desktop app is running. Works exactly like WhatsApp Web.

### Selectors

| Action | Selector |
|--------|----------|
| Go to Viber Web | `browser_session.goto("https://web.viber.com")` |
| Conversation list | `[data-qa="conversation-list"]` or `.conversation-list` |
| First conversation | `[data-qa="conversation-item"]:first-child` |
| Search conversations | `[data-qa="search-input"]` or `input[placeholder*="Search"]` |
| Open specific contact | Search by name, then click matching result |
| Message thread content | `get_text()` after opening a conversation |
| Message input | `[data-qa="message-input"]` or `div[contenteditable="true"]` |
| Send button | `[data-qa="send-button"]` or `button[aria-label="Send"]` |
| Send via keyboard | `press_key("Enter")` |

> **Selector stability note:** Viber Web uses `data-qa` attributes which tend to be stable.
> If they fail, use `browser_session.get_html(selector="main")` to inspect and find current hooks.

### Step-by-Step: Read & Reply to a Viber Message

```
1. browser_session.goto("https://web.viber.com")
2. browser_session.get_text()
3. browser_session.click('[data-qa="conversation-item"]:first-child')
4. browser_session.get_text()
5. (Compose reply — friendly and warm, Viber is personal/informal)
6. browser_session.type_text('[data-qa="message-input"]', "Your reply here")
7. browser_session.press_key("Enter")
8. notes.write("dm_log", "Viber reply sent to [contact name] — [summary of reply]")
```

### Searching for a Specific Contact

```
1. browser_session.click('[data-qa="search-input"]')
2. browser_session.type_text('[data-qa="search-input"]', "Contact Name")
3. browser_session.get_text()
   → Read filtered results to confirm correct contact
4. browser_session.click('[data-qa="conversation-item"]:first-child')
   → Open their conversation
```

### Viber-Specific Tone Notes
- Viber is personal and informal — treat it like texting, not emailing
- Short replies work best: 1–2 sentences for most messages
- Emojis are natural and welcome here
- Many Viber contacts are existing relationships — acknowledge the familiarity
- **Be extra careful:** Viber often contains personal/family contacts — always confirm who you're replying to before sending
- Escalate anything that feels personal, emotional, or sensitive — always flag to user first

### If Viber Web Selectors Fail
Viber Web uses `data-qa` attributes as its primary hooks. If those fail:
```
browser_session.get_html(selector="[class*='conversation']")
browser_session.get_html(selector="[class*='chat']")
browser_session.get_html(selector="main")
```
Look for `data-qa` attributes — they are the most stable selectors in Viber Web.

---

## Reply Templates

> **These are placeholders for you to fill in.**
> Add your own templates below for the most common DM types you receive.
> The agent will search this section when deciding how to reply to similar messages.

### How to Add Templates

Copy the block format below for each template you want to add.
Place them under the correct platform section.
The agent will use keyword matching and context to pick the most relevant one.

---

### Twitter / X — Reply Templates

```
[TEMPLATE: General Inquiry]
Trigger: Someone asks a general question about your services or brand
Reply:
[ADD YOUR REPLY TEMPLATE HERE]

[TEMPLATE: Collaboration Request]
Trigger: Someone proposes a partnership, collab, or brand deal
Reply:
[ADD YOUR REPLY TEMPLATE HERE]

[TEMPLATE: Complaint or Negative Feedback]
Trigger: Someone expresses frustration or reports a problem
Reply:
[ADD YOUR REPLY TEMPLATE HERE]

[TEMPLATE: Spam / Irrelevant Outreach]
Trigger: Generic sales pitch, follow-for-follow, or obvious spam
Reply: Do not reply. Flag to user.
```

---

### LinkedIn — Reply Templates

```
[TEMPLATE: Connection + Introduction]
Trigger: New connection who introduced themselves professionally
Reply:
[ADD YOUR REPLY TEMPLATE HERE]

[TEMPLATE: Job or Hiring Inquiry]
Trigger: Recruiter or candidate asking about open roles
Reply:
[ADD YOUR REPLY TEMPLATE HERE]

[TEMPLATE: Partnership or Business Opportunity]
Trigger: Someone proposing a collaboration or business discussion
Reply:
[ADD YOUR REPLY TEMPLATE HERE]

[TEMPLATE: Cold Sales Pitch]
Trigger: Someone selling software, services, or generic outreach
Reply: Do not reply. Flag to user if it looks relevant.
```

---

### Viber — Reply Templates

```
[TEMPLATE: General Message / Check-In]
Trigger: Someone reaching out casually to ask how things are going or just saying hi
Reply:
[ADD YOUR REPLY TEMPLATE HERE]

[TEMPLATE: Business or Service Inquiry via Viber]
Trigger: Someone asking about your work, services, or pricing through Viber
Reply:
[ADD YOUR REPLY TEMPLATE HERE]

[TEMPLATE: Scheduling or Meeting Request]
Trigger: Someone asking to set up a call, meeting, or appointment
Reply: Escalate to user — do not commit to dates/times without confirmation.

[TEMPLATE: Personal / Sensitive Message]
Trigger: Message feels personal, emotional, or involves relationships
Reply: Escalate to user. Do not reply without explicit approval.

[TEMPLATE: Quick Confirmation or Yes/No Question]
Trigger: Someone asking a simple factual question you can confirm (e.g., "Are you available Friday?")
Reply: Escalate to user for confirmation before replying.
```

---

### Gmail (Browser) — Reply Templates

```
[TEMPLATE: General Inquiry Email]
Trigger: Someone asking about your services, offering, or how to get started
Reply:
[ADD YOUR REPLY TEMPLATE HERE]

[TEMPLATE: Follow-Up / Check-In]
Trigger: Someone following up on a previous conversation or proposal
Reply:
[ADD YOUR REPLY TEMPLATE HERE]

[TEMPLATE: Partnership or Collaboration]
Trigger: Someone proposing a business collaboration, referral, or joint project
Reply:
[ADD YOUR REPLY TEMPLATE HERE]

[TEMPLATE: Support or Problem Report]
Trigger: Someone reporting an issue, bug, or bad experience
Reply: Escalate to user. Do not reply without user approval.

[TEMPLATE: Cold Outreach / Sales Pitch]
Trigger: Unsolicited vendor, software, or service pitch
Reply: Do not reply. Flag to user if it looks genuinely relevant.

[TEMPLATE: Thank You / Positive Feedback]
Trigger: Someone writing to say thank you or share a compliment
Reply:
[ADD YOUR REPLY TEMPLATE HERE]
```

---

### Instagram — Reply Templates

```
[TEMPLATE: Product or Service Question]
Trigger: Someone asking about what you offer, pricing, or availability
Reply:
[ADD YOUR REPLY TEMPLATE HERE]

[TEMPLATE: Compliment or Fan Message]
Trigger: Someone expressing appreciation or support
Reply:
[ADD YOUR REPLY TEMPLATE HERE]

[TEMPLATE: Influencer / UGC Collab Request]
Trigger: Content creator asking about partnerships or gifting
Reply:
[ADD YOUR REPLY TEMPLATE HERE]

[TEMPLATE: Customer Support Issue]
Trigger: Someone reporting a problem or bad experience
Reply: Escalate to user. Do not reply without user approval.
```

---

## Quick Escalation Reference

When the agent encounters any of these, it should stop and notify the user instead of replying:

| Signal | Action |
|--------|--------|
| Message mentions legal, lawsuit, refund, or dispute | Escalate immediately |
| Message asks for pricing, quote, or custom deal | Escalate |
| Message is in a foreign language (unless templates exist for it) | Escalate |
| Message is hostile, threatening, or harassing | Escalate + screenshot |
| Message is clearly a spam bot | Do not reply, log it |
| Message contains a link asking you to click | Do not click, flag it |
| Message requests personal info | Escalate immediately |

---

*Last updated: 2026-03-15 — Covers Twitter/X, LinkedIn, Instagram, Gmail (browser), and Viber. Add your reply templates above before asking the agent to handle messages autonomously.*
