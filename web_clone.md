# TrinityClaw — Website Cloning Workflow

> **Full 4-phase cloning pipeline. Every phase is mandatory — never skip.**
> This file is the authoritative reference for all website cloning tasks.
> `identity.md` contains the pointer; this file contains the procedure.

---

## Inspect Only

When the user asks "what colors does X use" / "get fonts from Y" — no project needed:

```
website_cloner.extract_tokens(URL)
```

Report palette, fonts, structure. Done.

---

## Full Clone — 4 Phases

> **CRITICAL RULE:** You are **recreating a design**, not filling a template. The source site defines the layout. You observe it and reproduce it. Do not force the source into your own fixed section order, card layout, or spacing. If the source has a full-width image banner, write a full-width image banner. If it has a 5-column grid, write 5 columns. If it has a dark sticky nav, write a dark sticky nav. Your job is to match what you see.

---

### Phase 1 — INSPECT (do this first, before any skill calls)

Perform a thorough visual audit of the source site.

```
browser_session.goto(SOURCE_URL)
browser_session.screenshot()           # screenshot 1: above the fold
browser_session.scroll("down")
browser_session.screenshot()           # screenshot 2: mid-page
browser_session.scroll("bottom")
browser_session.screenshot()           # screenshot 3: footer
```

After reviewing all 3 screenshots, write a **Design Brief** (in your response, before any further skill calls). This is your single source of truth for Phase 3.

```
DESIGN BRIEF for [URL]:

NAV
  - bg color: [hex]   text color: [hex]   sticky: yes/no
  - brand: "[text]"   style: [logo / wordmark / icon+text]
  - links: [list every label]   CTA button: [label, color]

SECTIONS (in order top to bottom):
  [N]. [section name]
       layout:  [e.g. "full-width centered text", "2-col image+text", "3-col card grid", "dark bg band", "full-width bg photo with text overlay"]
       bg:      [hex or "image" or "transparent"]
       heading: "[exact text]"   color: [hex]
       body:    "[first sentence or summary]"   color: [hex]
       CTA:     "[button label]"   bg: [hex]   text: [hex]
       special: [icon style, card border, image position, badge, etc.]

FOOTER
  - bg: [hex]   text: [hex]
  - content: [columns / single row / links list]

GLOBAL DESIGN LANGUAGE
  - primary color: [hex]   accent: [hex]   page bg: [hex]
  - body font: [name]   heading font: [name]
  - card style: [flat / shadowed / bordered / glass]
  - button style: [rounded / pill / square / ghost]
  - spacing feel: [tight / moderate / generous / airy]
  - overall vibe: [e.g. "modern SaaS", "luxury brand", "educational", "startup energy"]
```

**Do not proceed until this brief is written.** It is your build contract. Every decision in Phase 3 must trace back to an observation in this brief.

---

### Phase 2 — EXTRACT

```
website_cloner.clone(SOURCE_URL, "project-name")
```

`clone()` does three things and nothing more:
1. Extracts design tokens from the source (colors, fonts, section structure, nav links, per-section bg/heading/CTA colors)
2. Scaffolds a **blank** project with just a CSS reset + extracted `:root` vars
3. Starts the preview server

Read the output carefully. It contains:
- The extracted `:root` vars already written to `style.css` — use these directly in Phase 3
- A structured section list with heading text, bg color, column count, heading color, CTA color for each section
- Nav links list

The project preview will be blank until you write HTML in Phase 3. Save the preview URL — you need it for Phase 4.

After `clone()` returns ✅, proceed immediately to Phase 3 without pausing.

---

### Phase 3 — BUILD

You are writing `style.css` and `index.html` from scratch. The `:root` vars are already in `style.css` from Phase 2 — do not overwrite them, extend the file. Your class names are your own. Do not use the professional template's class names.

#### 3A — Extend style.css

Call `web_builder.read_file("project-name", "style.css")` first to see what the `:root` already has. Then call `web_builder.write_file("project-name", "style.css", FULL_CSS)` with the complete file: existing `:root` block + everything below.

> **Exception note**: This `write_file` with FULL_CSS is the one deliberate override of the general "never rewrite whole files" rule. Cloning requires assembling a coherent stylesheet from scratch; piecemeal patches on a near-empty file would leave gaps. In all other contexts — editing existing sites, patching content — use `patch_file` exclusively.

**Structure of style.css:**
```
[font @import — already there from clone()]
[reset — already there]
[existing :root block — keep it, add any missing vars]

body { font-family: var(--font-body, system-ui, sans-serif); color: var(--text); background: var(--bg); line-height: 1.65; }
h1, h2, h3, h4 { font-family: var(--font-heading, system-ui, sans-serif); color: var(--primary); line-height: 1.2; }
p { color: var(--text-lt, #6b7280); margin-bottom: 1rem; }
a { color: var(--accent); text-decoration: none; }
img { max-width: 100%; display: block; }
.container { max-width: 1140px; margin: 0 auto; padding: 0 2rem; }

[nav CSS — match source nav: bg, text color, sticky/absolute, padding]

[one CSS block per section — named .s-SECTIONSLUG]
  Each block has: background, padding, heading color/size, grid layout if columned, card style

[footer CSS]

[button CSS — match source: rounded/pill/outlined/filled]
```

**Rules for section CSS:**
- Use the bg colors from Phase 2 `clone()` output (section list with `bg:HEX` per section)
- Match the column count from the section list (`3-col` → `grid-template-columns: repeat(3, 1fr)`)
- Cards: add `border-radius`, `box-shadow`, or `border` only if the source uses them (check Brief)
- Every image placeholder div needs a `min-height` and `background` so it's visible

For a page with 6+ sections, write the CSS in **2 `write_file()` / `patch_file()` calls** to avoid truncation.

**🚨 MANDATORY CSS COMPLETENESS CHECK — do this before moving to 3B:**

After writing style.css, call `web_builder.read_file("project-name", "style.css")` and verify ALL of the following are present as actual CSS rules (not just `:root` variables):

| Required class | Must have |
|---|---|
| `.nav` or `nav` | `background`, `color` or child `a { color }`, `padding` |
| `.btn` or button selector | `background`, `color`, `padding`, `border-radius` |
| `.s-[first-section]` | `background` with the actual hex from Phase 2, `padding` |
| `.s-[each remaining section]` | `background`, `padding`, `color` on headings |
| `.footer` or `footer` | `background`, `color`, `padding` |

**If any row is missing from the file → write it before proceeding. Do not move to 3B with a half-written stylesheet.**

> ⚠️ `:root` variables alone are NOT CSS. The page is unstyled until selectors use them. Writing only `:root { --primary: #e63946; }` and nothing else means the page renders white. Every color, font, and layout value extracted in Phase 2 MUST appear in a concrete selector rule — `background: var(--primary)`, `color: var(--text)`, `grid-template-columns: repeat(3,1fr)`, etc. — or the clone has failed Phase 3.

**🚨 ANTI-SLOP CSS QUALITY CHECK — run this scan before Phase 3B:**

Before writing any HTML, grep_search your style.css output for these patterns. If any fire, patch them out first:

| Pattern | Replace with |
|---|---|
| `font-family: "Inter"` or `'Inter'` | `font-family: 'Outfit', 'Satoshi', 'DM Sans', or 'Plus Jakarta Sans'` |
| `font-family: arial` or `'Open Sans'` | a distinctive Google Font matching the source style |
| `justify-content: center` (on containers) | `justify-content: flex-start` + directional asymmetry |
| `justify-content: space-between` (on nav) | deliberate gap values or `space-around` |
| `#000000` | `#09090b` (zinc-950) or `#0a0a0a` |
| `#ffffff` (on body/surface bg) | `#fafaf9` (stone-50) or `#f8fafc` (slate-50) |
| `linear-gradient(135deg` | remove or replace with a solid brand color from the source |
| `box-shadow: 0 0 20px` or `0 0 30px` | remove or use subtle inner border `border: 1px solid rgba(0,0,0,0.08)` |
| `box-shadow: 0 4px 6px` | use the source's actual shadow depth from Phase 2 |
| `transition: all 0.3s` | `transition: background-color 0.2s ease, transform 0.2s ease` (specific props only) |
| `text-align: center` (body text) | left-align body text; center only hero headings |
| `John Doe` / `99%` / `lorem ipsum` | real names and specific values from source |
| Emojis in HTML | Radix/Phosphor SVG icons or clean SVG primitives |

**Why this matters:** Clones inherit generic patterns from scaffolding. The anti-slop scan catches the 80% of CSS quality problems that come from copying the blank template rather than the source's design intent.

#### 3B — Write index.html

Call `web_builder.write_file("project-name", "index.html", HTML)`.

**Rules:**
- One `<section class="s-SLUG" id="SLUG">` per section, **in the same order as the source**
- Class names must match what you wrote in 3A
- Fill in the real heading text and body text from Phase 2 `clone()` output and Phase 1 screenshots
- For image areas: `<div class="s-SLUG__img" aria-label="image description"></div>` — the CSS gives it height
- For card grids: write the exact number of cards seen in screenshots, with real content per card
- No placeholder text, no generic filler

**Page structure:**
```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SITE TITLE</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <nav class="nav"> ... </nav>
  [sections in source order]
  <footer class="footer"> ... </footer>
  <script src="script.js"></script>
</body>
</html>
```

For long pages, write HTML in **2 calls**: nav + first half of sections, then remaining sections + footer using `patch_file()`.

**Completeness protocol:** Never truncate with `<!-- ... rest of sections -->`, `// TODO`, or "the pattern repeats". Every section must be written in full. If approaching output limits, stop at a clean section boundary and output: `[PAUSED — N of M sections complete. Send "continue" to resume from: next-section-name]`. The next response picks up exactly there without repeating prior output.

After 3B returns ✅, proceed immediately to Phase 4 without pausing.

---

### Phase 4 — QA (mandatory — task is not done until this is complete)

```
browser_session.goto(PREVIEW_URL)
browser_session.screenshot()           # screenshot A: above the fold
browser_session.scroll("bottom")
browser_session.screenshot()           # screenshot B: footer
```

**Run the 5-Dimension Expert Critique alongside visual QA:**

```
web_builder.validate("project-name")
```

The `validate()` call produces:
- **HTML structural checks** — DOCTYPE, viewport, accessibility tags
- **Anti-slop warnings** — generic fonts, centered layouts, pure black/white, AI glow effects
- **5-Dimension scores (1–5 each) with ASCII radar chart:** Coherence · Hierarchy · Craft · Functionality · Innovation → overall A–F grade

Use the scores to prioritize fixes. A "D" or "F" grade means the clone reads as generic — focus on Craft and Innovation dimensions first. Address every `⚠️  Anti-Slop` warning before presenting to the user.

Compare against your Phase 1 screenshots. Check each category:

**Layout & Structure**
- Does section order match the source? ✅/❌
- Does column count match per section (3-col, 2-col, single, etc.)? ✅/❌
- Does image position match (left/right/background/above)? ✅/❌
- Does the nav look right (bg color, position, sticky/static)? ✅/❌

**Color & Surfaces**
- Does each section's background color match? ✅/❌
- Do button colors (bg + text) match? ✅/❌
- Does the footer bg match? ✅/❌

**Typography**
- Do heading and body fonts match (serif vs sans, weight, size)? ✅/❌
- Are heading sizes proportionate to the source? ✅/❌
- Is text alignment correct per section? ✅/❌

**Interactivity & States**
- Do all nav links and buttons have hover states? ✅/❌
- Are transitions smooth (not instant)? ✅/❌
- Are focus rings visible on keyboard navigation? ✅/❌

**Content**
- Is all heading/body text from the source (no filler, no Lorem Ipsum)? ✅/❌
- Do card counts match what was seen in Phase 1 screenshots? ✅/❌

Fix priority order for any ❌: (1) font swap, (2) color/bg, (3) hover states, (4) layout/spacing, (5) component details.

Fix any ❌ with `web_builder.patch_file()`. Re-screenshot to confirm.

**Final report to user:**
- Preview URL
- What matches (layout, colors, fonts, sections present)
- What still differs (e.g. needs real images, JS carousel)

---

## Never
- Stop after Phase 2 — `clone()` sets up the data, it doesn't build the site
- Stop after writing files — Phase 4 QA is mandatory
- Use the professional template class names (`.hero`, `.cards`, `.reviews`, etc.) — write your own
- Force the source into a fixed section order — the source's order is the order
- Leave image placeholder divs without a `min-height` in CSS — they collapse to 0 and disappear
- Generate placeholder or filler text — use real content from the source
- Download images from the source (copyright)
