# Browser Session — How to Interact With Any Website

## ✅ GENERAL PATTERN (most sites: LinkedIn, Twitter, TikTok, Viber, etc.)

```
1. browser_session.goto("https://...")
2. browser_session.get_snapshot()         ← reads the page, tags every element with @eN
3. browser_session.click_ref("@e5")       ← click by ref
4. browser_session.fill_ref("@e12", "text") ← type by ref
```

`get_snapshot()` finds every visible interactive element and stamps each with `data-tc-ref`.
Returns `@e1 button "Compose"`, `@e5 textbox "To"`, etc.
`click_ref("@eN")` and `fill_ref("@eN", text)` find the exact same node.

**Always call `get_snapshot()` again after a click that opens a new panel/dialog.**
The refs change when the DOM changes.

---

## ⚠️ GMAIL EXCEPTION — completely different workflow

Gmail is NOT compatible with get_snapshot/click_ref/fill_ref.
Use CSS selectors directly. Do NOT call get_snapshot() at any point during Gmail compose.

### Compose and send a new email — follow ALL 7 steps without stopping:

```
1. browser_session.goto("https://mail.google.com/mail/u/0/#inbox")
2. browser_session.click('[gh="cm"]')
3. browser_session.type_text('[name="to"]', "recipient@example.com")
4. browser_session.press_key("Tab")
5. browser_session.type_text('[name="subjectbox"]', "Subject here")
6. browser_session.type_text('div[contenteditable="true"][aria-multiline="true"]', "Body text here")
7. browser_session.press_key("Control+Enter")
```

STRICT RULES:
- Do NOT call get_snapshot() — not before step 2, not between any steps.
- Do NOT use fill_ref, click_ref, or type_accessible.
- Do NOT stop after step 3 — typing the To field does NOT send the email.
- Steps 4, 5, 6, 7 are ALL MANDATORY before the task is complete.
- Step 7 (Control+Enter) sends the email. The task is done only after step 7.

### Gmail CSS selectors (all language-independent)
- Compose button: `[gh="cm"]`
- To field: `[name="to"]`
- Subject: `[name="subjectbox"]`
- Body: `div[contenteditable="true"][aria-multiline="true"]`
- Send: `press_key("Control+Enter")` while body is focused

### Multiple Gmail accounts
- Account 1: `https://mail.google.com/mail/u/0/`
- Account 2: `https://mail.google.com/mail/u/1/`

---

## Fallbacks (use only if get_snapshot returns empty or ref click fails)

### click_accessible / type_accessible
```
browser_session.click_accessible("button", "Compose")
browser_session.type_accessible("textbox", "To", "user@example.com")
```
Use role + visible label. Works across iframes. Slower than refs.

### CSS selector click
```
browser_session.click('[data-testid="tweetButton"]') ← Twitter post button
```

---

## Single-call helpers (Twitter and TikTok only)

| Function | Use for |
|----------|---------|
| `tweet(text)` | Post to Twitter in one call |
| `like_tweet(url)` | Like a tweet |
| `reply_tweet(url, text)` | Reply to a tweet |
| `follow_user(username)` | Follow on Twitter |
| `tiktok_like(url)` | Like a TikTok |
| `tiktok_comment(url, text)` | Comment on a TikTok |
| `tiktok_follow(username)` | Follow on TikTok |

---

## Platform notes

### Twitter / X
- Compose sidebar: `[data-testid="SideNav_NewTweet_Button"]`
- Post button: `[data-testid="tweetButton"]` (compose page) or `[data-testid="tweetButtonInline"]` (inline)
- **Never call `get_html()` without a selector** — page is 3MB+

### LinkedIn
- DOM changes frequently — always use `get_snapshot()` first
- DM input: `.msg-form__contenteditable[contenteditable="true"]`
- Send: `press_key("Control+Enter")`

### TikTok
- Heavy SPA — wait 2s after `goto()` before interacting
- Uses `data-e2e` attributes as stable hooks
- **Never `get_html()` without selector** — page is large

### Viber
- Uses `data-qa` attributes as stable hooks
- Requires desktop app installed and paired
