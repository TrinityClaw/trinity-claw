# web_builder.py — Core website building skill
import base64
import functools
import http.server
import io
import json
import os
import re
import shutil
import threading
import zipfile
from pathlib import Path

try:
    from PIL import Image as _PILImage
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

NAME = "web_builder"
SHORT_DOC = "Build and preview HTML/CSS/JS websites. Use design.md files for styling."
DOC = (
    "Build and preview websites. "
    "scaffold(name, template)→create project; templates: 'professional'(RECOMMENDED), 'blank'; "
    "write_file(project, filename, content)→write full file; "
    "patch_file(project, filename, old, new)→targeted edit (PREFERRED after scaffold — whitespace-exact, include 2+ lines context); "
    "read_file(project, filename)→read; delete_file(project, filename)→remove; delete_project(project)→remove all; "
    "list_projects()→all projects; "
    "serve(project, port)→live preview on port 8090; stop_server()→stop; server_status()→check; "
    "export_zip(project)→downloadable zip; "
    "load_design(name)→load design.md from memory/knowledge/designs/, returns CSS vars + section specs to apply via patch_file(); "
    "build_from_design(name)→ONE-STEP: scaffold + parse design.md + apply CSS vars + preview; "
    "load_from_tmp(filename)→read file from /tmp/ (uploaded in chat); "
    "save_to_tmp(filename, content)→save to /tmp/ for download. "
    "⚠️ CRITICAL: After scaffold(), use patch_file() for ALL edits. write_file() on index.html/style.css DESTROYS the CSS template. "
    "WORKFLOW: build_from_design('my-design') → patch_file() × N (refine content) → serve(project). "
)

# ── Path Resolution ──────────────────────────────────────────────────────────
def _get_base_path() -> Path:
    if Path("/app").exists():
        return Path("/app")
    skill_file = Path(__file__).resolve()
    return skill_file.parent.parent.parent

def _get_websites_dir() -> Path:
    base = _get_base_path()
    p = base / "memory" / "websites"
    p.mkdir(parents=True, exist_ok=True)
    return p

def _get_designs_dir() -> Path:
    base = _get_base_path()
    p = base / "memory" / "knowledge" / "designs"
    p.mkdir(parents=True, exist_ok=True)
    return p

WEBSITES_DIR = _get_websites_dir()
DESIGNS_DIR = _get_designs_dir()

ALLOWED_EXTENSIONS = {
    ".html", ".css", ".js", ".json", ".svg", ".txt",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico",
    ".woff", ".woff2", ".ttf", ".otf",
}

# ── Server State ──────────────────────────────────────────────────────────────
_httpd = None
_server_thread = None
_serving_project = None

# ── Templates ────────────────────────────────────────────────────────────────
_TEMPLATES = {
    "blank": {
        "index.html": """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{name}</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <h1>{name}</h1>
  <p>Edit this page to get started.</p>
  <script src="script.js"></script>
</body>
</html>""",
        "style.css": """\
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: system-ui, sans-serif; line-height: 1.6; color: #1f2937; background: #fff; padding: 2rem; }
h1 { margin-bottom: 1rem; }""",
        "script.js": "// {name}\ndocument.addEventListener('DOMContentLoaded', () => console.log('{name} loaded'));\n",
    },

    "professional": {
        "index.html": """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{name}</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <nav class="nav">
    <div class="nav__inner">
      <a class="nav__brand" href="#">{name}</a>
      <ul class="nav__links">
        <li><a href="#about">About</a></li>
        <li><a href="#services">Services</a></li>
        <li><a href="#testimonials">Testimonials</a></li>
        <li><a href="#contact">Contact</a></li>
      </ul>
      <a href="#contact" class="btn btn--dark nav__cta">Get Started</a>
    </div>
  </nav>

  <section class="hero" id="home">
    <div class="hero__content">
      <span class="label">Welcome</span>
      <h1 class="hero__title">Your Headline Goes Here</h1>
      <p class="hero__sub">A compelling one-liner that tells visitors what you offer and why they should care.</p>
      <div class="hero__btns">
        <a href="#contact" class="btn btn--accent">Get Started</a>
        <a href="#about" class="btn btn--outline">Learn More</a>
      </div>
    </div>
  </section>

  <section class="about section" id="about">
    <div class="container about__grid">
      <div class="about__media">
        <img src="about.jpg" alt="About {name}">
      </div>
      <div class="about__text">
        <span class="label">About Us</span>
        <h2>Our Story</h2>
        <p>Tell your story here. Explain who you are, what you stand for, and why clients choose you over the competition.</p>
        <p>Add a second paragraph with your mission, history, or unique approach.</p>
        <a href="#services" class="btn btn--dark" style="margin-top:1.5rem">See Our Services</a>
      </div>
    </div>
  </section>

  <section class="services section section--alt" id="services">
    <div class="container">
      <div class="section-header">
        <span class="label">What We Offer</span>
        <h2>Our Services</h2>
        <p class="section-sub">Describe your offering clearly. What problems do you solve for clients?</p>
      </div>
      <div class="cards">
        <div class="card">
          <div class="card__icon">★</div>
          <h3>Service One</h3>
          <p>Describe this service in 2–3 sentences. Who is it for and what outcome does it deliver?</p>
        </div>
        <div class="card">
          <div class="card__icon">◆</div>
          <h3>Service Two</h3>
          <p>Describe this service in 2–3 sentences. Who is it for and what outcome does it deliver?</p>
        </div>
        <div class="card">
          <div class="card__icon">●</div>
          <h3>Service Three</h3>
          <p>Describe this service in 2–3 sentences. Who is it for and what outcome does it deliver?</p>
        </div>
      </div>
    </div>
  </section>

  <section class="stats">
    <div class="stats__row">
      <div class="stat"><span class="stat__n">10+</span><span class="stat__l">Years Experience</span></div>
      <div class="stat"><span class="stat__n">500+</span><span class="stat__l">Happy Clients</span></div>
      <div class="stat"><span class="stat__n">98%</span><span class="stat__l">Satisfaction Rate</span></div>
      <div class="stat"><span class="stat__n">24/7</span><span class="stat__l">Support</span></div>
    </div>
  </section>

  <section class="testimonials section" id="testimonials">
    <div class="container">
      <div class="section-header">
        <span class="label">Reviews</span>
        <h2>What Our Clients Say</h2>
      </div>
      <div class="reviews">
        <div class="review">
          <p class="review__text">"Add your first client testimonial here. Authentic quotes build trust faster than any marketing copy."</p>
          <p class="review__name">Client Name</p>
          <p class="review__role">Title, Company</p>
        </div>
        <div class="review">
          <p class="review__text">"Add your second testimonial. Include specific results — numbers and outcomes resonate with new visitors."</p>
          <p class="review__name">Client Name</p>
          <p class="review__role">Title, Company</p>
        </div>
        <div class="review">
          <p class="review__text">"Add your third testimonial. Social proof is one of your most powerful conversion tools."</p>
          <p class="review__name">Client Name</p>
          <p class="review__role">Title, Company</p>
        </div>
      </div>
    </div>
  </section>

  <section class="cta" id="contact">
    <div class="container cta__inner">
      <h2>Ready to Get Started?</h2>
      <p>Take the next step. Contact us today and let's talk about how we can help you reach your goals.</p>
      <div class="cta__btns">
        <a href="mailto:hello@example.com" class="btn btn--accent">Contact Us</a>
        <a href="tel:+11234567890" class="btn btn--outline">Call Us</a>
      </div>
    </div>
  </section>

  <footer class="footer">
    <div class="footer__row">
      <span class="footer__brand">{name}</span>
      <ul class="footer__links">
        <li><a href="#about">About</a></li>
        <li><a href="#services">Services</a></li>
        <li><a href="#contact">Contact</a></li>
      </ul>
      <p class="footer__copy">&copy; 2026 {name}. All rights reserved.</p>
    </div>
  </footer>

  <script src="script.js" defer></script>
</body>
</html>""",

        "style.css": """\
@import url('https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,400;0,700;1,400&family=Inter:wght@300;400;500;600&display=swap');
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --primary:      #1a2e4a;
  --primary-lt:   #2a4a6b;
  --accent:       #c9a84c;
  --accent-dk:    #a8893a;
  --text:         #1f2937;
  --text-lt:      #6b7280;
  --bg:           #ffffff;
  --surface:      #f8f9fa;
  --surface-2:    #edf2f7;
  --border:       #e2e8f0;
  --hero-bg:      #1a2e4a;
  --hero-overlay: rgba(26,46,74,0.65);
  --radius:       8px;
  --radius-lg:    16px;
  --max-w:        1140px;
  --shadow:       0 4px 6px -1px rgba(0,0,0,0.1),0 2px 4px -1px rgba(0,0,0,0.06);
  --shadow-lg:    0 10px 15px -3px rgba(0,0,0,0.12),0 4px 6px -2px rgba(0,0,0,0.05);
  --ease:         0.25s ease;
  --font-body:    'Inter', system-ui, -apple-system, sans-serif;
  --font-heading: 'Playfair Display', Georgia, serif;
  --nav-bg:       #ffffff;
  --nav-text:     #1f2937;
}

html { scroll-behavior: smooth; }
body { font-family: var(--font-body); line-height: 1.7; color: var(--text); background: var(--bg); font-size: 1rem; -webkit-font-smoothing: antialiased; }
h1, h2, h3, h4 { font-family: var(--font-heading); line-height: 1.2; color: var(--primary); }
h1 { font-size: clamp(2.25rem, 5vw, 3.75rem); }
h2 { font-size: clamp(1.75rem, 4vw, 2.75rem); margin-bottom: 1rem; }
h3 { font-size: 1.25rem; margin-bottom: 0.625rem; }
p  { margin-bottom: 1rem; color: var(--text-lt); }
p:last-child { margin-bottom: 0; }
a  { color: var(--accent); text-decoration: none; transition: color var(--ease); }
a:hover { color: var(--accent-dk); }
img { max-width: 100%; height: auto; display: block; }

/* ── Utilities ── */
.container    { max-width: var(--max-w); margin: 0 auto; padding: 0 2rem; }
.section      { padding: 5.5rem 0; }
.section--alt { background: var(--surface); }
.label { font-size: 0.8125rem; font-weight: 600; letter-spacing: 0.12em; text-transform: uppercase; color: var(--accent); margin-bottom: 0.75rem; display: block; }
.section-header { text-align: center; max-width: 680px; margin: 0 auto 3.5rem; }
.section-sub  { font-size: 1.0625rem; color: var(--text-lt); margin-bottom: 0; }

/* ── Buttons ── */
.btn { display: inline-block; padding: 0.875rem 2rem; border-radius: var(--radius); font-weight: 600; font-size: 0.9375rem; line-height: 1; cursor: pointer; border: 2px solid transparent; transition: background var(--ease), color var(--ease), transform var(--ease), box-shadow var(--ease); text-align: center; white-space: nowrap; }
.btn--accent       { background: var(--accent);    color: #fff; border-color: var(--accent); }
.btn--accent:hover { background: var(--accent-dk); color: #fff; border-color: var(--accent-dk); transform: translateY(-2px); box-shadow: var(--shadow); }
.btn--dark         { background: var(--primary);   color: #fff; border-color: var(--primary); }
.btn--dark:hover   { background: var(--primary-lt);color: #fff; border-color: var(--primary-lt); transform: translateY(-2px); }
.btn--outline      { background: transparent; color: #fff; border-color: rgba(255,255,255,0.75); }
.btn--outline:hover{ background: #fff; color: var(--primary); border-color: #fff; }

/* ── Navigation ── */
.nav { position: sticky; top: 0; z-index: 1000; background: var(--nav-bg); border-bottom: 1px solid var(--border); transition: box-shadow var(--ease); }
.nav__inner { display: flex; align-items: center; justify-content: space-between; padding: 1rem 2rem; max-width: var(--max-w); margin: 0 auto; gap: 2rem; }
.nav__brand { font-family: var(--font-heading); font-size: 1.4rem; font-weight: 700; color: var(--primary); white-space: nowrap; }
.nav__links { display: flex; gap: 2.5rem; list-style: none; }
.nav__links a { font-size: 0.9375rem; font-weight: 500; color: var(--text); transition: color var(--ease); }
.nav__links a:hover { color: var(--accent); }
.nav__cta { margin-left: auto; }

/* ── Hero ── */
.hero { position: relative; min-height: 100vh; display: flex; flex-direction: column; align-items: center; justify-content: center; text-align: center; background-color: var(--hero-bg); background-image: url('hero.jpg'); background-size: cover; background-position: center; overflow: hidden; padding: 4rem 2rem; }
.hero::after { content: ''; position: absolute; inset: 0; background: var(--hero-overlay); }
.hero__content { position: relative; z-index: 1; max-width: 820px; padding: 2rem; display: flex; flex-direction: column; align-items: center; }
.hero > * { position: relative; z-index: 1; }
.hero__content .label, .hero > .label { color: rgba(255,255,255,0.75); }
.hero__title, .hero > h1 { color: #fff; margin-bottom: 1.25rem; }
.hero__sub, .hero > p  { font-size: 1.1875rem; color: rgba(255,255,255,0.82); margin-bottom: 2.5rem; }
.hero__btns  { display: flex; gap: 1rem; justify-content: center; flex-wrap: wrap; }
.hero > a, .hero > .btn  { margin-top: 0.5rem; }

/* ── About ── */
.about__grid { display: grid; grid-template-columns: 1fr 1fr; gap: 4.5rem; align-items: center; }
.about__media { border-radius: var(--radius-lg); overflow: hidden; background: var(--surface-2); min-height: 380px; }
.about__media img { width: 100%; height: 100%; object-fit: cover; }
.about__text h2 { color: var(--primary); }
.about__text p  { color: var(--text-lt); }

/* ── Services / Cards ── */
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1.75rem; }
.card { background: var(--bg); border-radius: var(--radius-lg); padding: 2.25rem 2rem; border: 1px solid var(--border); transition: transform var(--ease), box-shadow var(--ease); }
.card:hover { transform: translateY(-4px); box-shadow: var(--shadow-lg); }
.card__icon { font-size: 2rem; line-height: 1; margin-bottom: 1.25rem; color: var(--accent); }
.card h3 { color: var(--primary); }
.card p  { color: var(--text-lt); font-size: 0.9375rem; line-height: 1.75; }

/* ── Stats bar ── */
.stats { background: var(--primary); padding: 4.5rem 0; }
.stats__row { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 2rem; text-align: center; max-width: var(--max-w); margin: 0 auto; padding: 0 2rem; }
.stat__n { font-family: 'Playfair Display', Georgia, serif; font-size: clamp(2.25rem, 4vw, 3rem); font-weight: 700; color: var(--accent); display: block; line-height: 1; margin-bottom: 0.5rem; }
.stat__l { color: rgba(255,255,255,0.75); font-size: 0.9375rem; }

/* ── Testimonials ── */
.reviews { display: grid; grid-template-columns: repeat(auto-fit, minmax(290px, 1fr)); gap: 1.75rem; margin-top: 3.5rem; }
.review { background: var(--surface); border-radius: var(--radius-lg); padding: 2.25rem; border-left: 4px solid var(--accent); }
.review__text { font-style: italic; color: var(--text); margin-bottom: 1.25rem; line-height: 1.8; }
.review__name { font-weight: 600; color: var(--primary); font-size: 0.9375rem; margin-bottom: 0.125rem; }
.review__role { font-size: 0.8125rem; color: var(--text-lt); margin-bottom: 0; }

/* ── CTA ── */
.cta { background: var(--primary); padding: 5.5rem 0; text-align: center; }
.cta__inner { max-width: 680px; }
.cta h2 { color: #fff; margin-bottom: 1rem; }
.cta p  { color: rgba(255,255,255,0.8); font-size: 1.125rem; margin-bottom: 2.5rem; }
.cta__btns { display: flex; gap: 1rem; justify-content: center; flex-wrap: wrap; }

/* ── Footer ── */
.footer { background: #0f1e2e; padding: 2.75rem 0; }
.footer__row { max-width: var(--max-w); margin: 0 auto; padding: 0 2rem; display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 1.5rem; }
.footer__brand { font-family: 'Playfair Display', Georgia, serif; font-size: 1.25rem; color: #fff; }
.footer__links { display: flex; gap: 2rem; list-style: none; }
.footer__links a { color: rgba(255,255,255,0.55); font-size: 0.875rem; transition: color var(--ease); }
.footer__links a:hover { color: var(--accent); }
.footer__copy { font-size: 0.8125rem; color: rgba(255,255,255,0.45); margin-bottom: 0; }

/* ── Responsive ── */
@media (max-width: 900px) { .about__grid { grid-template-columns: 1fr; gap: 2.5rem; } .about__media { min-height: 260px; } }
@media (max-width: 768px) { .nav__links, .nav__cta { display: none; } .nav__inner { padding: 1rem; } .hero__btns { flex-direction: column; align-items: center; } .footer__row { flex-direction: column; text-align: center; } .footer__links { flex-wrap: wrap; justify-content: center; } }
@media (max-width: 480px) { .section { padding: 3.5rem 0; } .card { padding: 1.75rem 1.5rem; } .cta { padding: 4rem 0; } }""",

        "script.js": """\
// {name}
document.addEventListener('DOMContentLoaded', () => {
  const nav = document.querySelector('.nav');
  if (nav) {
    window.addEventListener('scroll', () => {
      nav.style.boxShadow = window.scrollY > 20 ? '0 2px 20px rgba(0,0,0,0.12)' : 'none';
    }, { passive: true });
  }
  document.querySelectorAll('a[href^="#"]').forEach(a => {
    a.addEventListener('click', e => {
      const id = a.getAttribute('href');
      const target = id.length > 1 ? document.querySelector(id) : null;
      if (target) { e.preventDefault(); target.scrollIntoView({ behavior: 'smooth' }); }
    });
  });
});""",
    },
}


# ── Helpers ──────────────────────────────────────────────────────────────────
def _slugify(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r"[^a-z0-9\-_]", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    return name or "project"

def _safe_filename(filename: str) -> bool:
    p = Path(filename)
    return p.suffix in ALLOWED_EXTENSIONS and p.name == str(p) and ".." not in filename


# ── Public Functions ──────────────────────────────────────────────────────────

def scaffold(name: str, template: str = "blank") -> str:
    """
    Create a new website project.

    Args:
        name:     Project name (e.g. "my-portfolio")
        template: "professional" | "blank" (default: blank)

    Use "professional" for all client websites — full styled template.
    After scaffolding, use patch_file() to update content and colors.
    NEVER use write_file() on index.html or style.css after scaffold().
    """
    if template not in _TEMPLATES:
        return f"❌ Unknown template '{template}'. Available: {list(_TEMPLATES.keys())}"

    slug = _slugify(name)
    project_dir = WEBSITES_DIR / slug

    if project_dir.exists():
        return f"⚠️ Project '{slug}' already exists. Use patch_file() to edit it."

    project_dir.mkdir(parents=True, exist_ok=True)
    created = []
    for filename, content in _TEMPLATES[template].items():
        (project_dir / filename).write_text(content.replace("{name}", name), encoding="utf-8")
        created.append(f"  ✅ {filename}")

    return (
        f"🚀 Project '{slug}' created ({template} template):\n"
        + "\n".join(created)
        + f"\n\n⚠️ IMPORTANT: Do NOT use write_file() on index.html or style.css — "
        f"this destroys the CSS template. Use patch_file() instead."
        f"\n\nNext step: Use patch_file() to update content and colors."
        f"\nFinish with: serve('{slug}')"
    )


def patch_file(project: str, filename: str, old_string: str, new_string: str) -> str:
    """
    Targeted edit — replace old_string with new_string.
    Use this INSTEAD of write_file() for all changes after scaffold().

    Args:
        project:    Project slug
        filename:   File to edit (e.g. "style.css", "index.html")
        old_string: Exact text to find (must be unique in the file)
        new_string: Replacement text

    ⚠️ Match is whitespace-exact. Include 2+ lines of context to ensure uniqueness.
    """
    if not _safe_filename(filename):
        return f"❌ Disallowed filename '{filename}'"

    slug = _slugify(project)
    filepath = WEBSITES_DIR / slug / filename
    if not filepath.exists():
        return f"❌ {filename} not found in project '{slug}'"

    content = filepath.read_text(encoding="utf-8")
    count = content.count(old_string)

    if count == 0:
        return f"❌ Text not found in {filename} — check exact whitespace match."
    if count > 1:
        return f"❌ Found {count} matches. Provide more surrounding context to make it unique."

    updated = content.replace(old_string, new_string, 1)
    filepath.write_text(updated, encoding="utf-8")
    return f"✅ {filename} patched — 1 replacement made."


def write_file(project: str, filename: str, content: str) -> str:
    """
    Write or overwrite a file in a project.
    WARNING: After scaffold() with 'professional', use patch_file() instead.
    write_file() on index.html/style.css destroys the CSS template.
    """
    if not _safe_filename(filename):
        return f"❌ Disallowed filename '{filename}'"

    if not content or not content.strip():
        return f"❌ Empty content for '{filename}'. Provide full file content."

    slug = _slugify(project)
    project_dir = WEBSITES_DIR / slug
    if not project_dir.exists():
        return f"❌ Project '{slug}' not found. Run scaffold() first."

    # Auto-inject CSS/JS links into index.html if model forgot them
    if filename == "index.html":
        if 'href="style.css"' not in content and "href='style.css'" not in content:
            if "</head>" in content:
                content = content.replace("</head>", '  <link rel="stylesheet" href="style.css">\n</head>')
        if 'src="script.js"' not in content and "src='script.js'" not in content:
            if "</body>" in content:
                content = content.replace("</body>", '  <script src="script.js" defer></script>\n</body>')

    # Strip markdown fences that models sometimes wrap content in
    stripped = content.strip()
    fence = re.match(r"^```[a-zA-Z]*\n?(.*?)\n?```\s*$", stripped, re.DOTALL)
    if fence:
        content = fence.group(1).strip()
    elif stripped.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\n?", "", stripped, count=1).strip()

    (project_dir / filename).write_text(content, encoding="utf-8")
    return f"✅ {filename} written ({len(content)} chars)"


def read_file(project: str, filename: str) -> str:
    """Read a file from a project."""
    if not _safe_filename(filename):
        return f"❌ Disallowed filename '{filename}'"

    slug = _slugify(project)
    filepath = WEBSITES_DIR / slug / filename
    if not filepath.exists():
        return f"❌ {filename} not found in project '{slug}'"

    return f"📄 {filename} ({filepath.stat().st_size} bytes):\n\n{filepath.read_text(encoding='utf-8')}"


def list_projects() -> str:
    """List all website projects."""
    WEBSITES_DIR.mkdir(parents=True, exist_ok=True)
    projects = sorted([p for p in WEBSITES_DIR.iterdir() if p.is_dir()])
    if not projects:
        return "📭 No projects yet. Use scaffold() to create one."

    lines = [f"🌐 Website Projects ({len(projects)}):"]
    for p in projects:
        tag = " 🟢 SERVING" if p.name == _serving_project else ""
        lines.append(f"\n  📁 {p.name}{tag}")
        for f in sorted(p.iterdir()):
            if f.is_file():
                lines.append(f"    - {f.name} ({f.stat().st_size} bytes)")
    return "\n".join(lines)


def serve(project: str, port: str = "8090") -> str:
    """
    Start live preview server. Open http://localhost:<port> to view.
    """
    global _httpd, _server_thread, _serving_project

    slug = _slugify(project)
    project_dir = WEBSITES_DIR / slug
    if not project_dir.exists():
        return f"❌ Project '{slug}' not found"

    try:
        port_int = int(port)
        if not (1024 <= port_int <= 65535):
            return "❌ Port must be between 1024 and 65535"
    except ValueError:
        return f"❌ Invalid port: '{port}'"

    if _httpd:
        try:
            _httpd.shutdown()
        except Exception:
            pass
        _httpd = None
        _server_thread = None
        _serving_project = None

    class _NoListHandler(http.server.SimpleHTTPRequestHandler):
        def list_directory(self, _path):
            self.send_error(403, "Directory listing disabled")
            return None

    handler = functools.partial(_NoListHandler, directory=str(project_dir))
    try:
        _httpd = http.server.HTTPServer(("0.0.0.0", port_int), handler)
        _server_thread = threading.Thread(target=_httpd.serve_forever, daemon=True)
        _server_thread.start()
        _serving_project = slug
        return (
            f"PREVIEW_URL: http://localhost:{port_int}\n"
            f"🟢 Project '{slug}' is live on port {port_int}.\n"
            f"Open http://localhost:{port_int} in your browser.\n"
            f"Edit files with patch_file() then refresh. Call stop_server() when done."
        )
    except OSError as e:
        return f"❌ Could not start server on port {port_int}: {e}"


def stop_server() -> str:
    """Stop the preview server."""
    global _httpd, _server_thread, _serving_project
    if _httpd:
        try:
            _httpd.shutdown()
        except Exception:
            pass
        _httpd = None
        _server_thread = None
        _serving_project = None
        return "🔴 Preview server stopped."
    return "ℹ️ No server is currently running."


def server_status() -> str:
    """Check preview server status."""
    if _httpd and _serving_project and _server_thread and _server_thread.is_alive():
        port = _httpd.server_address[1]
        return f"🟢 Serving '{_serving_project}' at http://localhost:{port}"
    return "🔴 No server running."


def delete_file(project: str, filename: str) -> str:
    """Delete a file from a project."""
    if not _safe_filename(filename):
        return f"❌ Disallowed filename '{filename}'"
    slug = _slugify(project)
    filepath = WEBSITES_DIR / slug / filename
    if not filepath.exists():
        return f"❌ {filename} not found in project '{slug}'"
    filepath.unlink()
    return f"🗑️ {filename} deleted from '{slug}'."


def delete_project(project: str) -> str:
    """Delete an entire project."""
    global _serving_project
    slug = _slugify(project)
    project_dir = WEBSITES_DIR / slug
    if not project_dir.exists():
        return f"❌ Project '{slug}' not found."
    if _serving_project == slug:
        stop_server()
    shutil.rmtree(project_dir)
    return f"🗑️ Project '{slug}' deleted."


def export_zip(project: str) -> str:
    """Export a project as a zip file."""
    slug = _slugify(project)
    project_dir = WEBSITES_DIR / slug
    if not project_dir.exists():
        return f"❌ Project '{slug}' not found."
    zip_path = WEBSITES_DIR / f"{slug}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in sorted(project_dir.rglob("*")):
            if file.is_file():
                zf.write(file, file.relative_to(project_dir))
    size_kb = zip_path.stat().st_size / 1024
    return f"📦 Exported '{slug}' → {zip_path} ({size_kb:.1f} KB)"


# ── Design.md Loading ─────────────────────────────────────────────────────────

def _get_design_search_paths() -> list:
    base = _get_base_path()
    return [
        DESIGNS_DIR,
        base / "memory" / "knowledge" / "designs",
        base / "knowledge" / "designs",
        base / "designs",
    ]


def load_design(design_name: str = None) -> str:
    """
    Load a design.md file from memory/knowledge/designs/.

    Args:
        design_name: Name of the design file (without .md).
                     e.g. "raycast-style" → loads "raycast-style.md"
                     If None, lists all available designs.

    Returns:
        Parsed design specs with CSS variables ready for patch_file().

    Example:
        <skill:web_builder.load_design>raycast-style</skill:web_builder.load_design>
    """
    search_paths = _get_design_search_paths()

    if design_name is None:
        available = []
        for p in search_paths:
            if p.exists():
                available.extend([f.stem for f in p.glob("*.md")])
        if not available:
            return "📁 No design files found.\n\nSearched:\n" + "\n".join(f"  - {p}" for p in search_paths)
        return "📐 Available designs:\n" + "\n".join(f"  - {d}" for d in sorted(set(available)))

    design_file = None
    for p in search_paths:
        candidate = p / f"{design_name}.md"
        if candidate.exists():
            design_file = candidate
            break

    if not design_file:
        return f"❌ Design '{design_name}' not found.\n\nSearched:\n" + "\n".join(f"  - {p}" for p in search_paths)

    content = design_file.read_text(encoding="utf-8")
    specs = _parse_design_md(content)

    primary = specs['colors'].get('primary', '#1a2e4a')
    accent  = specs['colors'].get('accent',  '#c9a84c')
    bg      = specs['colors'].get('background', '#ffffff')
    text    = specs['colors'].get('text', '#1f2937')
    font_h  = specs['fonts'].get('heading', 'Playfair Display')
    font_b  = specs['fonts'].get('body', 'Inter')

    css_vars = f"""\
  --primary:      {primary};
  --accent:       {accent};
  --text:         {text};
  --bg:           {bg};
  --font-heading: '{font_h}', Georgia, serif;
  --font-body:    '{font_b}', system-ui, sans-serif;"""

    return f"""✅ Design loaded: {design_file.name}

---

## 📐 Design Specs

### Colors
| Role | Value |
|------|-------|
| Primary | {primary} |
| Accent | {accent} |
| Background | {bg} |
| Text | {text} |

### Typography
- **Headings:** {font_h}
- **Body:** {font_b}

### Theme
{specs['theme']}

### Sections
""" + "\n".join(f"- {s}" for s in specs['sections']) + f"""

---

## 🎨 CSS Variables (patch into :root)

```css
:root {{
{css_vars}
}}
```

---

## 🚀 Build Steps

1. `scaffold('{specs['name']}', 'professional')`
2. Patch :root in style.css with the CSS vars above
3. Patch section content in index.html (hero headline, about text, etc.)
4. `serve('{specs['name']}')`

"""


def build_from_design(design_name: str) -> str:
    """
    ONE-STEP build: scaffold + parse design.md + apply CSS vars + ready for refinement.

    Args:
        design_name: Slugified name of design.md in memory/knowledge/designs/

    Returns:
        Project created with design colors applied. Use patch_file() to refine content.
        Then serve(project).

    Example:
        <skill:web_builder.build_from_design>raycast-style</skill:web_builder.build_from_design>
    """
    search_paths = _get_design_search_paths()
    design_file = None
    for p in search_paths:
        candidate = p / f"{design_name}.md"
        if candidate.exists():
            design_file = candidate
            break

    if not design_file:
        return f"❌ Design '{design_name}' not found.\n\nSearched:\n" + "\n".join(f"  - {p}" for p in search_paths)

    content = design_file.read_text(encoding="utf-8")
    specs = _parse_design_md(content)
    project_name = _slugify(specs['name'])

    if (WEBSITES_DIR / project_name).exists():
        return f"⚠️ Project '{project_name}' already exists. Use patch_file() to edit it."

    # Scaffold
    (WEBSITES_DIR / project_name).mkdir(parents=True, exist_ok=True)
    for filename, file_content in _TEMPLATES["professional"].items():
        (WEBSITES_DIR / project_name / filename).write_text(
            file_content.replace("{name}", specs['name']), encoding="utf-8"
        )

    # Inject CSS variables
    primary = specs['colors'].get('primary', '#1a2e4a')
    accent  = specs['colors'].get('accent',  '#c9a84c')
    bg      = specs['colors'].get('background', '#ffffff')
    text    = specs['colors'].get('text', '#1f2937')
    font_h  = specs['fonts'].get('heading', 'Playfair Display')
    font_b  = specs['fonts'].get('body', 'Inter')

    if specs['theme'] == 'dark':
        new_root = f""":root {{
  --primary:      {primary};
  --accent:       {accent};
  --text:         {bg};
  --bg:           {primary};
  --surface:      #111;
  --border:       #2a2a2a;
  --font-heading: '{font_h}', Georgia, serif;
  --font-body:    '{font_b}', system-ui, sans-serif;
  --hero-bg:      #040506;
  --hero-overlay: rgba(0,0,0,0.7);
  --nav-bg:       #0d0d0d;
  --nav-text:     {bg};
}}"""
    else:
        new_root = f""":root {{
  --primary:      {primary};
  --accent:       {accent};
  --text:         {text};
  --bg:           {bg};
  --font-heading: '{font_h}', Georgia, serif;
  --font-body:    '{font_b}', system-ui, sans-serif;
  --hero-bg:      {primary};
  --hero-overlay: rgba(0,0,0,0.55);
  --nav-bg:       {bg};
  --nav-text:     {text};
}}"""

    # Inject Google Fonts
    font_import = f"@import url('https://fonts.googleapis.com/css2?family={font_h.replace(' ', '+')}:wght@400;700&family={font_b.replace(' ', '+')}:wght@300;400;500;600&display=swap');\n\n"

    try:
        style_path = WEBSITES_DIR / project_name / "style.css"
        style_content = style_path.read_text(encoding="utf-8")

        # Replace :root block
        root_pat = re.compile(r":root\s*\{[^}]+\}", re.DOTALL)
        if root_pat.search(style_content):
            style_content = root_pat.sub(new_root, style_content, count=1)
        else:
            style_content = new_root + "\n\n" + style_content

        # Add font import
        if "@import" not in style_content:
            style_content = font_import + style_content

        style_path.write_text(style_content, encoding="utf-8")
    except Exception as e:
        pass

    results = [
        f"🚀 Project '{project_name}' built from design '{design_name}'",
        f"✅ Theme: {specs['theme']} | {len(specs['colors'])} colors | {len(specs['fonts'])} fonts",
        f"📄 Sections: {', '.join(specs['sections'][:5]) if specs['sections'] else 'default'}",
        f"\n⚠️ Use patch_file() to refine content and colors",
        f"Finish with: serve('{project_name}')",
    ]

    return "\n".join(results)


def _parse_design_md(content: str) -> dict:
    """Parse a design.md file into structured specs dict."""
    specs = {
        'name': 'My Website',
        'theme': 'light',
        'colors': {},
        'fonts': {},
        'sections': [],
    }

    lines = content.split('\n')
    current_section = None
    in_code_block = False
    code_block_lang = None
    code_block_lines = []

    for line in lines:
        # Track code blocks
        if line.strip().startswith('```'):
            if in_code_block:
                if code_block_lang in ('css', 'css-variables'):
                    specs['colors'].setdefault('__css_vars', line for line in code_block_lines)
                code_block_lines = []
                in_code_block = False
                code_block_lang = None
            else:
                in_code_block = True
                code_block_lang = line.strip()[3:].strip()
                code_block_lines = []
            continue

        if in_code_block:
            code_block_lines.append(line)
            continue

        # Project name
        if line.startswith('# ') and current_section is None:
            specs['name'] = line[2:].strip()
            continue

        # Theme
        if '**Theme:**' in line:
            specs['theme'] = line.split('**Theme:**')[1].strip().lower()
            continue

        # Section headers
        if line.startswith('## '):
            section_name = line[3:].strip().lower()
            if 'color' in section_name: current_section = 'colors'
            elif 'typograph' in section_name: current_section = 'fonts'
            elif 'section' in section_name: current_section = 'sections'
            else: current_section = section_name
            continue

        # Parse content
        if current_section == 'colors' and line.strip().startswith('- '):
            parts = line.strip()[2:].split(':', 1)
            if len(parts) == 2:
                specs['colors'][parts[0].strip().lower()] = parts[1].strip()
        elif current_section == 'fonts' and line.strip().startswith('- '):
            parts = line.strip()[2:].split(':', 1)
            if len(parts) == 2:
                specs['fonts'][parts[0].strip().lower()] = parts[1].strip()
        elif current_section == 'sections' and line.strip().startswith('- '):
            specs['sections'].append(line.strip()[2:].strip())

        # Also parse markdown tables for colors
        if '|' in line and line.strip().startswith('|'):
            parts = [p.strip() for p in line.split('|') if p.strip()]
            if all(set(p.strip()) <= {'-', ':', '|', ' '} for p in parts):
                continue
            if len(parts) >= 2 and parts[0].lower() in ('name', 'role') and parts[1].lower() in ('value', 'hex', 'color'):
                continue
            if len(parts) >= 3 and parts[1].startswith('#'):
                name = parts[0].strip().lower().replace(' ', '-')
                val = parts[1].strip()
                specs['colors'][name] = val

    return specs


# ── Temp Files ────────────────────────────────────────────────────────────────

def _get_tmp_dir() -> Path:
    tmp = Path("/tmp")
    tmp.mkdir(exist_ok=True)
    return tmp

def load_from_tmp(filename: str) -> str:
    """Read a file from /tmp/ (files uploaded in chat)."""
    tmp = _get_tmp_dir()
    file_path = tmp / filename
    if not file_path.exists():
        return f"❌ File '{filename}' not found in /tmp/\n\nFiles in /tmp/:\n" + "\n".join(f"  - {f.name}" for f in tmp.iterdir())
    if file_path.suffix.lower() in {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'}:
        return f"📷 Image: {filename}\nPath: {file_path}\n\nUse: <img src='{file_path}'>"
    try:
        return f"📄 {filename}:\n\n{file_path.read_text(encoding='utf-8')}"
    except Exception as e:
        return f"❌ Could not read {filename}: {e}"

def save_to_tmp(filename: str, content: str) -> str:
    """Save content to /tmp/ for easy download."""
    tmp = _get_tmp_dir()
    file_path = tmp / filename
    file_path.write_text(content, encoding="utf-8")
    return f"✅ Saved to /tmp/{filename} ({len(content)} bytes)"