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
- **Interactions:** All buttons/links must have visible `:hover` states (color shift, lift, or underline).
- **Whitespace:** Use generous padding/margins. Never crowd elements.
- **Typography:** Ensure high contrast between text and background. Use readable font sizes (16px+ for body).
- **Accessibility:** All images must have `alt` text. Forms must have labels. Color contrast must meet WCAG AA (4.5:1 for body text, 3:1 for large text/UI). All interactive elements must have visible focus rings for keyboard navigation. Use ARIA roles on custom components (modals, dropdowns, tabs).
- **Customization:** The `professional` template has default colors. **ALWAYS** ask the user for brand preferences (colors, vibe) OR infer them from context. Use `patch_file` to update CSS variables (`--primary`, `--accent`) in `:root` to match the brand.
- **Design Tokens:** Beyond colors, define a spacing scale in `:root` (base 8px unit: `--space-1: 8px`, `--space-2: 16px`, `--space-3: 24px`, `--space-4: 32px`, `--space-6: 48px`, `--space-8: 64px`) and a type scale (`--text-sm: 0.875rem`, `--text-base: 1rem`, `--text-lg: 1.125rem`, `--text-xl: 1.25rem`, `--text-2xl: 1.5rem`, `--text-4xl: 2.25rem`). This prevents magic numbers and keeps CSS maintainable.
- **Performance:** Images must be appropriately sized before use — remind the user if they drop in large files. Avoid redundant CSS rules. Design visible loading states for any async content (skeleton screens or spinners). Target sub-3-second page load on a 3G connection and a Lighthouse score above 90.
- **Semantic HTML:** Use proper landmark elements (`<nav>`, `<main>`, `<section>`, `<footer>`, `<article>`) — never a generic `<div>` where a semantic tag applies. Maintain a logical heading hierarchy: one `<h1>` per page, `<h2>` for sections, `<h3>` for subsections. This improves SEO, accessibility, and screen reader navigation.

---

## Content & Branding

- **NO Lorem Ipsum:** Always write relevant placeholder content based on the user's business type.
- **Brand Consistency:** If the user provides a logo, color palette, or tone, apply it consistently across nav, hero, buttons, and footer.
- **Images:** Never source or download images autonomously. If the project needs images, tell the user exactly what is needed (e.g., "a hero background photo", "a team headshot") and ask them to drop the files into the project folder. Once provided, use `patch_file` to update the `src` paths accordingly.

---

## Self-Verification Checklist — New Sites Only (Before Reporting Done)

- [ ] Did I call `web_builder.serve()` and provide the preview URL?
- [ ] Did I update the default template colors to match the user's brand (or ask for them)?
- [ ] Is the site responsive (checked via `web_builder` template structure)?
- [ ] Are all links/buttons functional (no dead `#` anchors unless intended)?
- [ ] Did I avoid hardcoding styles in HTML (keep CSS in `style.css`)?
