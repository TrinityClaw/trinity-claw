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
| DM / Messages | `[data-testid="AppTabBar_DirectMessage_Link"]` |

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

### If LinkedIn selectors fail
LinkedIn updates their DOM frequently. Run `browser_session.get_html(selector="main")` to inspect current structure, then identify the correct selector.

---

## Instagram (instagram.com)

| Action | Selector / Method |
|--------|------------------|
| New post | Click `+` icon in nav or use `browser_session.click('[aria-label="New post"]')` |
| Caption field | `textarea[aria-label="Write a caption..."]` |

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
