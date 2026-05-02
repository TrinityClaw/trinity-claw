# TrinityClaw — Web Design & Development

Standards and workflow for HTML/CSS/JS projects built with `web_builder`.
For website *cloning* specifically, see [web_clone.md](web_clone.md).

---

## Workflow

### Using a design.md file (RECOMMENDED)

Design files go in `/app/memory/knowledge/designs/<name>.md`.

```
# 1. Build in one step
build_from_design("your-design-name")  # slugified, no spaces

# 2. Refine content with patch_file()
patch_file(project, "index.html", old_text, new_text)
patch_file(project, "style.css", old_css, new_css)

# 3. Preview
serve(project)
```

### Manual build (no design.md)

```
scaffold("my-site", "professional")    # positional args only
patch_file("my-site", "index.html", old, new)   # edit content
patch_file("my-site", "style.css", old_root, new_root)  # change colors
serve("my-site")
```

### web_builder functions

| Function | When to use |
|---|---|
| `scaffold(name, template)` | Create project. Use `"professional"` template. |
| `patch_file(project, filename, old, new)` | Edit any file. old/new must match exactly (whitespace matters). |
| `write_file(project, filename, content)` | Create new files (NOT index.html/style.css after scaffold). |
| `read_file(project, filename)` | Read a file |
| `load_design(name)` | Load design.md, returns CSS vars + sections to apply manually |
| `build_from_design(name)` | Scaffold + apply design colors in one call |
| `serve(project)` | Start preview server on port 8090 |
| `export_zip(project)` | Download as zip |
| `delete_project(project)` | Delete project |

---

## Design Quality Standards

- **Responsive:** Mobile (320px), tablet (768px), desktop (1024px+).
- **Hover states:** All buttons/links must have visible `:hover` feedback. Animate only `transform` and `opacity`. 200–300ms transitions.
- **Typography:** High contrast. 16px+ body. `line-height: 1.6–1.7`. Constrain paragraphs to `max-width: 65ch`.
- **Colors:** Avoid pure `#000000` / `#ffffff`. Use off-blacks (`#111111`) and warm whites (`#F7F6F3`). Single accent color. Tinted shadows.
- **Spacing:** Use an 8px base unit scale in `:root` (`--space-1: 8px`, `--space-2: 16px`, `--space-4: 32px`, etc.).
- **Images:** `alt` text required. Appropriately sized before use.
- **Forms:** Labels required. Visible focus states.

---

## Content Rules

- **NO Lorem Ipsum.** Write relevant placeholder content.
- **NO clichéd copy:** "Elevate", "Seamless", "Unleash", "Next-Gen", exclamation marks in success messages.
- **NO fake round numbers.** Use specific plausible figures or omit.
- **NO emojis in code/UI.** Use an icon system (Phosphor, Radix, Heroicons).
- **Sentence case headers.** Active voice.
- **Never download images autonomously.** Tell the user what's needed.

---

## Anti-Generic Patterns

| Avoid | Instead |
|---|---|
| Centered hero + two-button CTA | Offset layout, full-bleed image, split-screen |
| 3-column card grid as default | Zig-zag rows, bento grids, masonry |
| Purple → blue gradient | Brand-specific single hue palette |
| Inter / Roboto defaults | Geist, Outfit, Satoshi, DM Sans |
| Lucide/Feather exclusively | Phosphor, Radix, custom SVGs |
| Generic `box-shadow: 0 4px 12px` | Tinted shadows matching background hue |
| Pill-shaped everything | Vary border-radius by component |

---

## design.md Format

Place in `/app/memory/knowledge/designs/<name>.md`:

```markdown
# My Project Name
## Colors
- Primary: #1a2e4a
- Accent: #c9a84c
- Background: #ffffff
- Text: #1f2937

## Typography
- Headings: Playfair Display
- Body: Inter

## Sections
- Hero (headline: "...", cta: "Get Started")
- About
- Services
- Contact

## Theme
light  (or: dark)
```

The parser also handles markdown tables for colors:
```
| Name     | Value   |
|----------|---------|
| Primary  | #1a2e4a |
```

---

## Pre-Build Checklist

- [ ] Did I slugify the design name? (`"Raycast Style"` → `"raycast-style"`)
- [ ] Did I use `patch_file()`, NOT `write_file()`, on index.html or style.css?
- [ ] Did I call `serve()` and provide the preview URL?
- [ ] Did I update template colors to match the brand (or ask)?
- [ ] Are all links/buttons functional?
- [ ] Is the site responsive?