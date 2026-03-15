# Browser Session — Platform Selectors & Usage Patterns

Reference file for using `browser_session` skill with real logged-in Chrome.

---

## Twitter / X (x.com)

| Action | Selector |
|--------|----------|
| Open compose box | `[data-testid="SideNav_NewTweet_Button"]` |
| Tweet textarea | `[data-testid="tweetTextarea_0"]` |
| Post button (home feed inline composer) | `[data-testid="tweetButtonInline"]` |
| Post button (dedicated compose modal at x.com/compose/post) | `[data-testid="tweetButton"]` |
| Reply button (on a tweet) | `[data-testid="reply"]` |
| Like button | `[data-testid="like"]` |
| Retweet button | `[data-testid="retweet"]` |
| Follow button (on profile) | `[data-testid="placementTracking"]` |
| DM / Messages (nav) | `[data-testid="AppTabBar_DirectMessage_Link"]` |
| DM conversation (first) | `[data-testid="conversation"]` |
| DM reply input | `[data-testid="dmComposerTextInput"]` |
| DM send button | `[data-testid="dmComposerSendButton"]` |

### Post a Tweet (step-by-step)
```
1. browser_session.screenshot()                                         — see current state
2. browser_session.click('[data-testid="SideNav_NewTweet_Button"]')     — open compose
3. browser_session.type_text('[data-testid="tweetTextarea_0"]', "text") — type content
4. browser_session.screenshot()                                         — verify before posting
5. browser_session.click('[data-testid="tweetButtonInline"]')           — post
6. browser_session.screenshot()                                         — confirm posted
```

### Reply to a Tweet
```
1. browser_session.goto("https://x.com/user/status/TWEET_ID")
2. browser_session.click('[data-testid="reply"]')
3. browser_session.type_text('[data-testid="tweetTextarea_0"]', "reply text")
4. browser_session.screenshot()
5. browser_session.click('[data-testid="tweetButtonInline"]')
```

---

## LinkedIn (linkedin.com)

| Action | Selector |
|--------|----------|
| Start a post | `button.share-box-feed-entry__trigger` or `[data-control-name="share.sharebox_trigger"]` |
| Post text area | `.ql-editor[contenteditable="true"]` |
| Post button | `button.share-actions__primary-action` |
| Open DM inbox | `browser_session.goto("https://www.linkedin.com/messaging/")` |
| Conversation list | `.msg-conversations-container__conversations-list` |
| First unread conversation | `.msg-conversation-listitem__link` |
| Message thread content | `.msg-s-message-list__event` |
| DM reply input | `.msg-form__contenteditable[contenteditable="true"]` |
| DM send button | `.msg-form__send-button` — or `press_key("Control+Enter")` |

### If LinkedIn selectors fail
LinkedIn updates their DOM frequently. Run `browser_session.get_html(selector="main")` to inspect current structure, then identify the correct selector.

---

## Instagram (instagram.com)

| Action | Selector / Method |
|--------|------------------|
| New post | `browser_session.click('[aria-label="New post"]')` |
| Caption field | `textarea[aria-label="Write a caption..."]` |
| Open DM inbox | `browser_session.goto("https://www.instagram.com/direct/inbox/")` |
| Conversation list item | `[role="listbox"] > div` (first = most recent) |
| DM reply input | `[aria-label="Message..."]` or `[placeholder="Message..."]` |
| DM send | `press_key("Enter")` |

### If Instagram selectors fail
Run `browser_session.get_html(selector="section")` and look for `aria-label` attributes.

---

## Gmail (mail.google.com)

> **Note:** The `gmail` OAuth skill is preferred for search/summarize/bulk tasks.
> Use `browser_session` for Gmail when you need to work inside the user's open tab visually.
> For full workflows see `social_media_dm_guide.md`.

| Action | Selector |
|--------|----------|
| Go to inbox | `browser_session.goto("https://mail.google.com/mail/u/0/#inbox")` |
| First unread email row | `tr.zA.zE` |
| Reply button (inside email) | `[data-tooltip^="Odg"]` (Serbian) or `[data-tooltip="Reply"]` (English) — use `get_html` to confirm |
| Reply compose area | `div[contenteditable="true"][aria-label]` — or just `div[contenteditable="true"]` inside reply panel |
| Send button | `[data-tooltip^="Pošalji"]` (Serbian) or `[data-tooltip="Send"]` (English) — or `[jsaction*="send"]` |
| Search bar | `input[name="q"]` — language-independent |
| **Compose new (use this)** | `[gh="cm"]` — language-independent internal Gmail attribute |
| To field (new email) | `[name="to"]` — language-independent |
| Subject field (new email) | `[name="subjectbox"]` — language-independent |
| Body (new compose) | `div[contenteditable="true"][aria-multiline="true"]` or `div[g_editable="true"]` |

> **Non-English Gmail:** If Gmail is in Serbian (or any non-English language), `aria-label` values are
> translated and text selectors will fail. Always prefer `[gh]`, `[name]`, `[data-testid]`, or
> `[jsaction]` attributes — they are language-independent. Run `get_html(selector="[gh='cm']")` to verify.

### Multiple Gmail accounts
- Account 1: `https://mail.google.com/mail/u/0/`
- Account 2: `https://mail.google.com/mail/u/1/`
- Account 3: `https://mail.google.com/mail/u/2/`

### Reply to an email (quick reference)
```
1. browser_session.goto("https://mail.google.com/mail/u/0/#inbox")
2. browser_session.click('tr.zA.zE')                                                    — open first unread
3. browser_session.get_text()                                                           — read email
4. browser_session.click('[data-tooltip^="Odg"]')                                      — reply (Serbian UI)
   Fallback: browser_session.click('[jsaction*="reply"]')                              — language-independent
5. browser_session.type_text('div[contenteditable="true"][aria-multiline="true"]', "reply text")
6. browser_session.screenshot()                                                         — verify before send
7. browser_session.press_key("Control+Enter")                                           — send (universal)
```

### Compose a new email (quick reference)
```
1. browser_session.click('[gh="cm"]')                                                   — compose (language-independent)
2. browser_session.type_text('[name="to"]', "email@example.com")                        — recipient
3. browser_session.type_text('[name="subjectbox"]', "Subject here")                     — subject
4. browser_session.type_text('div[contenteditable="true"][aria-multiline="true"]', "Body here")
5. browser_session.screenshot()                                                         — verify
6. browser_session.press_key("Control+Enter")                                           — send
```

### If Gmail selectors fail
Gmail uses obfuscated class names. Prefer `aria-label` and `data-tooltip` — they are stable.
Run `browser_session.get_html(selector="[role='main']")` to inspect current DOM.

---

## Viber (web.viber.com)

> **Requirement:** Viber desktop app must be installed and paired with your phone.
> Viber Web at `https://web.viber.com` mirrors your account — like WhatsApp Web.
> Must be logged in via QR code scan first. After that, stays connected while desktop app is running.

| Action | Selector |
|--------|----------|
| Go to Viber Web | `browser_session.goto("https://web.viber.com")` |
| Conversation list | `[data-qa="conversation-list"]` or `.conversation-list` |
| First conversation item | `[data-qa="conversation-item"]:first-child` or `.conversation-item` |
| Search conversations | `[data-qa="search-input"]` or `input[placeholder*="Search"]` |
| Message input area | `[data-qa="message-input"]` or `div[contenteditable="true"]` |
| Send button | `[data-qa="send-button"]` or `button[aria-label="Send"]` |
| Send via keyboard | `press_key("Enter")` |

> **Note:** Viber Web uses `data-qa` attributes which are relatively stable.
> If selectors above fail, run `browser_session.get_html(selector="[class*='conversation']")`
> to discover the current structure.

### Reply to a Viber Message (quick reference)
```
1. browser_session.goto("https://web.viber.com")
2. browser_session.screenshot()                                    — confirm logged in and connected
3. browser_session.get_text()                                      — read conversation list previews
4. browser_session.click('[data-qa="conversation-item"]:first-child') — open first conversation
5. browser_session.screenshot()                                    — see the message thread
6. browser_session.get_text()                                      — read full thread
7. browser_session.click('[data-qa="message-input"]')              — focus input
8. browser_session.type_text('[data-qa="message-input"]', "reply") — type reply
9. browser_session.screenshot()                                    — verify before sending
10. browser_session.press_key("Enter")                             — send
11. browser_session.screenshot()                                   — confirm sent
```

### If Viber Web selectors fail
```
browser_session.get_html(selector="main")
browser_session.get_html(selector="[class*='chat']")
```
Look for `data-qa` attributes first — they are Viber's most stable hook.

---

## General Patterns

### When selectors are unknown
```
browser_session.screenshot()           — see the page
browser_session.get_html(selector="nav")  — inspect navigation
browser_session.get_text()             — read all visible text
```

### Always verify before destructive actions
Always take a `browser_session.screenshot()` after composing and BEFORE clicking post/submit on any platform. Show it to the user for confirmation if possible.

### Selector fallbacks
If a `data-testid` selector fails (platform updated their DOM):
1. Try `text=Button Label` — e.g. `text=Post`
2. Try `[aria-label="Button Label"]`
3. Use `browser_session.get_html()` to find the current selector
