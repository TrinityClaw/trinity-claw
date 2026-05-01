# TrinityClaw — Web Design & Development

> Standards and workflow for all HTML/CSS/JS projects built with `web_builder`.
> For website *cloning* specifically, see [web_clone.md](web_clone.md).

---

## Primary Tool: `web_builder` Skill

- **USE** the `web_builder` skill suite for all web projects. It handles structure, preview server, and CSS enhancement automatically.
- **Workflow (MANDATORY — for new site creation only, NOT for cloning):**
  1. `web_builder.scaffold(project_name, "professional")` → Creates base structure (index.html, style.css, script.js).
  2. `web_builder.patch_file(...)` → Update content, branding, and colors (NEVER rewrite whole files unless necessary).
  3. `web_builder.serve(project_name)` → Start live preview and report the URL.
  4. After serving, **continue building** if the user requested a complete site. Only stop and report the preview URL if the user's request was simply "scaffold and preview" or equivalent. Never stop mid-build on a full site request.

---

## Design Quality Standards

- **Responsiveness:** All sites must work on mobile (320px), tablet (768px), and desktop (1024px+).
- **Interactions:** All buttons/links must have visible `:hover` states (color shift, lift, or underline). Animate **only** `transform` and `opacity` — never `top`, `left`, `width`, or `height`. Use 200–300ms transitions. Add `scale(0.98)` on `:active` press for tactile feedback. Use `will-change: transform` only on elements that actually animate.
- **Whitespace:** Use generous padding/margins. Never crowd elements.
- **Typography:** Ensure high contrast between text and background. Use readable font sizes (16px+ for body, `line-height: 1.6–1.7`). Prefer `Geist`, `Outfit`, `Cabinet Grotesk`, or `Satoshi` for premium designs — avoid `Inter` or `Roboto` as defaults. Apply tight `letter-spacing: -0.02em` to large headlines. Constrain paragraph width to `max-width: 65ch`. Use `text-wrap: balance` on headings. Use monospace fonts for numeric/data content.
- **Accessibility:** All images must have `alt` text. Forms must have labels. Color contrast must meet WCAG AA (4.5:1 for body text, 3:1 for large text/UI). All interactive elements must have visible focus rings for keyboard navigation. Use ARIA roles on custom components (modals, dropdowns, tabs).
- **Customization:** The `professional` template has default colors. **ALWAYS** ask the user for brand preferences (colors, vibe) OR infer them from context. Use `patch_file` to update CSS variables (`--primary`, `--accent`) in `:root` to match the brand.
- **Color Discipline:** Avoid pure `#000000` backgrounds — use off-black or tinted darks (e.g. `#0d0d0d`, `#111111`). Avoid pure `#ffffff` for page backgrounds on premium designs — warm whites like `#F7F6F3` feel more intentional. Use a single accent color; resist adding a second. Desaturate accents below 80% saturation for a less "startup" look. Make `box-shadow` colors tinted to match the background hue, not generic `rgba(0,0,0,0.1)`. Avoid the default AI-aesthetic gradient (purple → blue).
- **Design Tokens:** Beyond colors, define a spacing scale in `:root` (base 8px unit: `--space-1: 8px`, `--space-2: 16px`, `--space-3: 24px`, `--space-4: 32px`, `--space-6: 48px`, `--space-8: 64px`) and a type scale (`--text-sm: 0.875rem`, `--text-base: 1rem`, `--text-lg: 1.125rem`, `--text-xl: 1.25rem`, `--text-2xl: 1.5rem`, `--text-4xl: 2.25rem`). This prevents magic numbers and keeps CSS maintainable.
- **Performance:** Images must be appropriately sized before use — remind the user if they drop in large files. Avoid redundant CSS rules. Design visible loading states for any async content (skeleton screens or spinners). Target sub-3-second page load on a 3G connection and a Lighthouse score above 90.
- **Semantic HTML:** Use proper landmark elements (`<nav>`, `<main>`, `<section>`, `<footer>`, `<article>`) — never a generic `<div>` where a semantic tag applies. Maintain a logical heading hierarchy: one `<h1>` per page, `<h2>` for sections, `<h3>` for subsections. This improves SEO, accessibility, and screen reader navigation.

---

## Content & Branding

- **NO Lorem Ipsum:** Always write relevant placeholder content based on the user's business type.
- **Content Quality:** Avoid clichéd AI copywriting: never use "Elevate", "Seamless", "Unleash", "Next-Gen", "Cutting-edge", or exclamation marks in success messages. Don't use fake round numbers ("99.99% uptime", "10,000+ customers") — use specific plausible figures or omit them. Use diverse, realistic placeholder names — not "John Doe" repeated. Write in active voice, sentence case headers (not Title Case Every Word).
- **No Emojis in UI:** Never use emojis in code, markup, headings, or button labels. Use a proper icon system (Phosphor, Radix, Heroicons) instead.
- **Brand Consistency:** If the user provides a logo, color palette, or tone, apply it consistently across nav, hero, buttons, and footer.
- **Images:** Never source or download images autonomously. If the project needs images, tell the user exactly what is needed (e.g., "a hero background photo", "a team headshot") and ask them to drop the files into the project folder. Once provided, use `patch_file` to update the `src` paths accordingly.

---

## Anti-Generic AI Patterns

These are the exact patterns that make AI-built sites look identical. Avoid them by default:

| Ban | Instead |
|---|---|
| Centered hero + subheading + two-button CTA as the *only* hero option | Offset layouts, full-bleed image, split-screen, bold typographic hero |
| Default 3-column card grid for every features section | Zig-zag rows, bento grids, masonry, numbered list with large type |
| Purple → blue gradient as the "premium" look | Considered single-hue palette or brand-specific gradient |
| `Inter` or `Roboto` for everything | `Geist`, `Outfit`, `Cabinet Grotesk`, `Satoshi`, or the brand's own font |
| Lucide/Feather icons exclusively | Phosphor, Radix, or custom SVGs; match stroke weight consistently |
| Generic `box-shadow: 0 4px 12px rgba(0,0,0,0.1)` | Tinted shadows matching the background hue |
| Pill-shaped cards and containers everywhere | Vary border-radius by component: sharp on data tables, rounded on cards, pill on badges only |
| Accordion FAQ as the default Q&A pattern | Consider inline disclosure, tabbed panels, or definition list styles |

---

## Design.md Workflow

You can use pre-made design specifications to build websites faster. Upload a `design.md` file to the knowledgebase and the agent will parse it automatically.

### Folder Location
```
agent/memory/knowledgebase/designs/your-design-name.md
```

### Design.md Format
Your design.md can be simple or complex. Examples:

**Simple format:**
```markdown
# Project Name
## Colors
- Primary: #1a2e4a
- Accent: #c9a84c
- Background: #ffffff
## Typography
- Headings: Playfair Display
- Body: Inter
## Sections
- Hero (headline: "...", cta: "Get Started")
- About
- Services
- Contact
## Layout
Modern, clean, professional
```

**Complex format (like Raycast style):**
Supports markdown tables, CSS code blocks, component specs, typography scales, spacing tokens, shadows, gradients, and agent prompt guides.

### Functions

- **`load_design(name)`** → Load and parse a design.md, returns CSS variables + section specs to apply via `patch_file()`. Use when you want manual control.

- **`build_from_design(name)`** → ONE-STEP auto-apply: scaffolds project + parses design + applies CSS variables automatically. Fastest option when you have a design.md ready.

### Workflows

```
# BEST FOR COMPLEX DESIGNS (LLM generates matching HTML/CSS)
build_from_design("your-design-name")

# STEP-BY-STEP (more control)
load_design("your-design-name")
scaffold(project_name, "professional")
patch_file()... (apply colors/content)
serve(project_name)

# MANUAL (no design.md)
scaffold(project_name, "professional")
patch_file()... (build from scratch)
serve(project_name)
```

### Auto-Apply Behavior (LLM-Powered)

When using `build_from_design()` with a **complex design.md** (like Raycast-style):

1. Parses the full design.md including all tokens, tables, and CSS
2. **Sends complete design spec to LLM** (Claude/GPT-4)
3. LLM **intelligently generates HTML/CSS** that properly applies:
   - Dark theme backgrounds (#040506, #07080a, etc.)
   - Radial gradients as atmosphere backdrops
   - Layered shadows for depth
   - Correct typography scale and tracking
   - Component styles
4. Overwrites template with LLM-generated code
5. Starts preview server (optional: add `true` as third parameter)

**Fallback:** If LLM is unavailable or fails, falls back to basic CSS variable injection.

---

## Self-Verification Checklist — New Sites Only (Before Reporting Done)

- [ ] Did I use the appropriate workflow? (`build_from_design()` for design.md, `scaffold()` + `patch_file()` for manual, `analyze_design_folder()` for images)
- [ ] Did I call `web_builder.serve()` and provide the preview URL?
- [ ] Did I update the default template colors to match the user's brand (or ask for them)?
- [ ] Is the site responsive (checked via `web_builder` template structure)?
- [ ] Are all links/buttons functional (no dead `#` anchors unless intended)?
- [ ] Did I avoid hardcoding styles in HTML (keep CSS in `style.css`)?
