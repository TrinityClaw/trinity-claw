# web_builder.py
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
from html.parser import HTMLParser
from pathlib import Path

try:
    import litellm as _litellm
    HAS_LITELLM = True
except ImportError:
    HAS_LITELLM = False

try:
    from PIL import Image as _PILImage
    HAS_PILLOW = True
except ImportError:
    HAS_PILLOW = False

# Vision LLM calls can take several minutes on local hardware.
# This overrides the global SKILL_TIMEOUT_SECONDS (30s) for this skill only.
SKILL_TIMEOUT = int(os.getenv("WEB_BUILDER_TIMEOUT", "300"))

NAME = "web_builder"
SHORT_DOC = "Build, preview, and manage HTML/CSS/JS website projects with a live preview server on port 8090."
DOC = (
    "Build, preview, and manage HTML/CSS/JS website projects with live preview server on port 8090. "
    "analyze_design_folder(folder,language?,model?,device_frame?)→batch-analyze all design images with vision LLM, returns JSON brief with optional device frame HTML (iPhone|android|macos|browser); "
    "scaffold(name,template)→create project; templates: 'professional'(RECOMMENDED — full styled landing page, 250+ line CSS), 'blank', 'landing', 'dashboard'; "
    "write_file(project,filename,content)→full file write (style.css auto-enhanced if sparse); "
    "patch_file(project,filename,old,new)→targeted edit without rewriting whole file (PREFERRED for content/color updates) — WARNING: match is whitespace-exact, tabs and newlines must match precisely; include 2+ lines of surrounding context to ensure uniqueness; "
    "read_file(project,filename)→read file; delete_file(project,filename)→remove a file; "
    "delete_project(project)→remove entire project; list_projects()→all projects; "
    "serve(project,port)→live preview; stop_server()→stop preview; server_status()→check if server is running and get its URL (use this, NEVER call 'status' — that function does not exist); "
    "validate(project)→HTML structural checks + expanded anti-slop design audit + 5-dimension expert critique with scores; "
    "export_zip(project)→pack project as downloadable zip. "
    "get_design_system(description,project_name?)→generate industry-matched design system (colors, typography, UI style, anti-patterns) using 161 reasoning rules; call BEFORE scaffold() for best results. "
    "get_design_direction(project_name?,industry?,keywords?)→recommend 3 design directions from 5 schools × 20 philosophies with descriptions and CSS variable hints; "
    "set_design_tone(project,tone)→apply design preset: 'soft' (premium, airy, spring motion), 'minimalist' (clean, editorial, Linear/Notion), 'brutalist' (Swiss typography, sharp contrast); returns CSS variables and recommendations. "
    "⚠️ CRITICAL RULE: After scaffold(), ALWAYS use patch_file() — NEVER write_file() on index.html or style.css. write_file() overwrites the full professional template and destroys the layout. "
    "TEXT-ONLY BUILD WORKFLOW: scaffold(name,professional) → patch_file×N (update placeholders + :root colors) → serve(). "
    "DESIGN-AWARE BUILD WORKFLOW (RECOMMENDED): get_design_system(description) → scaffold(name,professional) → patch_file×N (apply design system :root variables, fonts, section text) → serve(). "
    "IMAGE BUILD WORKFLOW: analyze_design_folder(folder,device_frame?) → scaffold(name,professional) → patch_file×N (apply brief colors/text) → serve(). "
    "PREMIUM BUILD WORKFLOW: scaffold(name,professional) → set_design_tone(name,soft|minimalist|brutalist) → patch_file×N → serve(). "
    "DIRECTION WORKFLOW: get_design_direction() → pick a school/philosophy → get_design_system() → scaffold() → patch_file×N → serve()."
)

WEBSITES_DIR = Path("/app/memory/websites")
ALLOWED_EXTENSIONS = {
    ".html", ".css", ".js", ".json", ".svg", ".txt",
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico",
    ".woff", ".woff2", ".ttf", ".otf",
}

# Module-level server state
_httpd = None
_server_thread = None
_serving_project = None

# ── Templates ─────────────────────────────────────────────────────────────────

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
*, *::before, *::after {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

body {
  font-family: system-ui, -apple-system, sans-serif;
  line-height: 1.6;
  color: #1f2937;
  background: #ffffff;
  padding: 2rem;
}

h1 { margin-bottom: 1rem; }""",
        "script.js": """\
// {name}
document.addEventListener('DOMContentLoaded', () => {
  console.log('{name} loaded');
});""",
    },

    "landing": {
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
    <div class="nav__brand">{name}</div>
    <ul class="nav__links">
      <li><a href="#features">Features</a></li>
      <li><a href="#about">About</a></li>
      <li><a href="#contact">Contact</a></li>
    </ul>
  </nav>

  <section class="hero">
    <h1 class="hero__title">Welcome to {name}</h1>
    <p class="hero__subtitle">A great place to start your project.</p>
    <a href="#features" class="btn btn--primary">Get Started</a>
  </section>

  <section id="features" class="features">
    <h2>Features</h2>
    <div class="features__grid">
      <div class="card"><h3>Feature One</h3><p>Description of your first feature.</p></div>
      <div class="card"><h3>Feature Two</h3><p>Description of your second feature.</p></div>
      <div class="card"><h3>Feature Three</h3><p>Description of your third feature.</p></div>
    </div>
  </section>

  <footer class="footer">
    <p>&copy; 2026 {name}. All rights reserved.</p>
  </footer>

  <script src="script.js"></script>
</body>
</html>""",
        "style.css": """\
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --primary: #2563eb;
  --primary-dark: #1d4ed8;
  --text: #1f2937;
  --subtle: #6b7280;
  --bg: #ffffff;
  --surface: #f9fafb;
  --border: #e5e7eb;
  --radius: 8px;
  --max-w: 1100px;
}

body { font-family: system-ui, -apple-system, sans-serif; line-height: 1.6; color: var(--text); background: var(--bg); }

/* Nav */
.nav { display: flex; align-items: center; justify-content: space-between; padding: 1rem 2rem; border-bottom: 1px solid var(--border); position: sticky; top: 0; background: var(--bg); z-index: 100; }
.nav__brand { font-weight: 700; font-size: 1.25rem; }
.nav__links { display: flex; gap: 2rem; list-style: none; }
.nav__links a { text-decoration: none; color: var(--subtle); }
.nav__links a:hover { color: var(--primary); }

/* Hero */
.hero { text-align: center; padding: 6rem 2rem; background: var(--surface); }
.hero__title { font-size: clamp(2rem, 5vw, 3.5rem); font-weight: 800; margin-bottom: 1rem; }
.hero__subtitle { font-size: 1.25rem; color: var(--subtle); margin-bottom: 2rem; }

/* Buttons */
.btn { display: inline-block; padding: 0.75rem 1.75rem; border-radius: var(--radius); text-decoration: none; font-weight: 600; transition: background 0.2s; }
.btn--primary { background: var(--primary); color: #fff; }
.btn--primary:hover { background: var(--primary-dark); }

/* Features */
.features { max-width: var(--max-w); margin: 0 auto; padding: 5rem 2rem; }
.features h2 { text-align: center; font-size: 2rem; margin-bottom: 3rem; }
.features__grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 1.5rem; }
.card { background: var(--surface); border-radius: var(--radius); padding: 2rem; border: 1px solid var(--border); }
.card h3 { margin-bottom: 0.5rem; }

/* Footer */
.footer { text-align: center; padding: 2rem; border-top: 1px solid var(--border); color: var(--subtle); font-size: 0.875rem; }""",
        "script.js": """\
// {name}
document.addEventListener('DOMContentLoaded', () => {
  console.log('{name} ready');
});""",
    },

    "dashboard": {
        "index.html": """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{name} Dashboard</title>
  <link rel="stylesheet" href="style.css">
</head>
<body>
  <aside class="sidebar">
    <div class="sidebar__brand">{name}</div>
    <nav class="sidebar__nav">
      <a href="#" class="sidebar__link sidebar__link--active">Dashboard</a>
      <a href="#" class="sidebar__link">Analytics</a>
      <a href="#" class="sidebar__link">Settings</a>
    </nav>
  </aside>

  <main class="main">
    <header class="topbar">
      <h1 class="topbar__title">Dashboard</h1>
    </header>

    <div class="stats">
      <div class="stat-card"><p class="stat-card__label">Total Users</p><p class="stat-card__value">1,284</p></div>
      <div class="stat-card"><p class="stat-card__label">Revenue</p><p class="stat-card__value">$12,400</p></div>
      <div class="stat-card"><p class="stat-card__label">Active Sessions</p><p class="stat-card__value">42</p></div>
      <div class="stat-card"><p class="stat-card__label">Uptime</p><p class="stat-card__value">99.9%</p></div>
    </div>

    <section class="content">
      <h2>Recent Activity</h2>
      <table class="table">
        <thead><tr><th>Event</th><th>User</th><th>Time</th></tr></thead>
        <tbody>
          <tr><td>Login</td><td>alice@example.com</td><td>2 min ago</td></tr>
          <tr><td>Purchase</td><td>bob@example.com</td><td>15 min ago</td></tr>
          <tr><td>Signup</td><td>carol@example.com</td><td>1 hr ago</td></tr>
        </tbody>
      </table>
    </section>
  </main>

  <script src="script.js"></script>
</body>
</html>""",
        "style.css": """\
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --sidebar-w: 220px;
  --primary: #2563eb;
  --bg: #f3f4f6;
  --surface: #ffffff;
  --text: #1f2937;
  --subtle: #6b7280;
  --border: #e5e7eb;
  --radius: 8px;
}

body { font-family: system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); display: flex; min-height: 100vh; }

/* Sidebar */
.sidebar { width: var(--sidebar-w); background: var(--surface); border-right: 1px solid var(--border); display: flex; flex-direction: column; padding: 1.5rem 1rem; position: fixed; top: 0; left: 0; bottom: 0; }
.sidebar__brand { font-weight: 700; font-size: 1.25rem; margin-bottom: 2rem; padding: 0 0.5rem; }
.sidebar__nav { display: flex; flex-direction: column; gap: 0.25rem; }
.sidebar__link { display: block; padding: 0.6rem 0.75rem; border-radius: var(--radius); text-decoration: none; color: var(--subtle); font-size: 0.9rem; }
.sidebar__link:hover { background: var(--bg); color: var(--text); }
.sidebar__link--active { background: #eff6ff; color: var(--primary); font-weight: 600; }

/* Main */
.main { margin-left: var(--sidebar-w); flex: 1; display: flex; flex-direction: column; }

/* Topbar */
.topbar { background: var(--surface); border-bottom: 1px solid var(--border); padding: 1rem 2rem; }
.topbar__title { font-size: 1.25rem; font-weight: 700; }

/* Stats */
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1rem; padding: 2rem; }
.stat-card { background: var(--surface); border-radius: var(--radius); padding: 1.5rem; border: 1px solid var(--border); }
.stat-card__label { font-size: 0.75rem; color: var(--subtle); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 0.5rem; }
.stat-card__value { font-size: 1.75rem; font-weight: 700; }

/* Content */
.content { padding: 0 2rem 2rem; }
.content h2 { margin-bottom: 1rem; font-size: 1.1rem; }

/* Table */
.table { width: 100%; border-collapse: collapse; background: var(--surface); border-radius: var(--radius); overflow: hidden; border: 1px solid var(--border); }
.table th, .table td { padding: 0.75rem 1rem; text-align: left; border-bottom: 1px solid var(--border); font-size: 0.9rem; }
.table th { background: var(--bg); font-weight: 600; color: var(--subtle); text-transform: uppercase; font-size: 0.75rem; letter-spacing: 0.05em; }
.table tr:last-child td { border-bottom: none; }""",
        "script.js": """\
// {name} Dashboard
document.addEventListener('DOMContentLoaded', () => {
  console.log('{name} dashboard ready');
});""",
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

body {
  font-family: var(--font-body);
  line-height: 1.7;
  color: var(--text);
  background: var(--bg);
  font-size: 1rem;
  -webkit-font-smoothing: antialiased;
}

h1, h2, h3, h4 {
  font-family: var(--font-heading);
  line-height: 1.2;
  color: var(--primary);
}

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
.btn {
  display: inline-block;
  padding: 0.875rem 2rem;
  border-radius: var(--radius);
  font-weight: 600;
  font-size: 0.9375rem;
  line-height: 1;
  cursor: pointer;
  border: 2px solid transparent;
  transition: background var(--ease), color var(--ease), transform var(--ease), box-shadow var(--ease);
  text-align: center;
  white-space: nowrap;
}
.btn--accent       { background: var(--accent);    color: #fff; border-color: var(--accent); }
.btn--accent:hover { background: var(--accent-dk); color: #fff; border-color: var(--accent-dk); transform: translateY(-2px); box-shadow: var(--shadow); }
.btn--dark         { background: var(--primary);   color: #fff; border-color: var(--primary); }
.btn--dark:hover   { background: var(--primary-lt);color: #fff; border-color: var(--primary-lt); transform: translateY(-2px); }
.btn--outline      { background: transparent; color: #fff; border-color: rgba(255,255,255,0.75); }
.btn--outline:hover{ background: #fff; color: var(--primary); border-color: #fff; }

/* ── Navigation ── */
.nav {
  position: sticky;
  top: 0;
  z-index: 1000;
  background: var(--nav-bg);
  border-bottom: 1px solid var(--border);
  transition: box-shadow var(--ease);
}
.nav__inner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 1rem 2rem;
  max-width: var(--max-w);
  margin: 0 auto;
  gap: 2rem;
}
.nav__brand {
  font-family: var(--font-heading);
  font-size: 1.4rem;
  font-weight: 700;
  color: var(--primary);
  white-space: nowrap;
}
.nav__links { display: flex; gap: 2.5rem; list-style: none; }
.nav__links a { font-size: 0.9375rem; font-weight: 500; color: var(--text); transition: color var(--ease); }
.nav__links a:hover { color: var(--accent); }
.nav__cta { margin-left: auto; }

/* ── Hero ── */
.hero {
  position: relative;
  min-height: 100vh;
  display: flex;
  flex-direction: column;   /* stack children vertically even without wrapper div */
  align-items: center;
  justify-content: center;
  text-align: center;
  background-color: var(--hero-bg);
  background-image: url('hero.jpg');
  background-size: cover;
  background-position: center;
  overflow: hidden;
  padding: 4rem 2rem;
}
.hero::after {
  content: '';
  position: absolute;
  inset: 0;
  background: var(--hero-overlay);
}
/* Wrapper div when model uses it */
.hero__content {
  position: relative;
  z-index: 1;
  max-width: 820px;
  padding: 2rem;
  display: flex;
  flex-direction: column;
  align-items: center;
}
/* Direct children of hero (model skips wrapper) also sit above the overlay */
.hero > * { position: relative; z-index: 1; }
.hero__content .label, .hero > .label { color: rgba(255,255,255,0.75); }
.hero__title, .hero > h1 { color: #fff; margin-bottom: 1.25rem; }
.hero__sub,   .hero > p  { font-size: 1.1875rem; color: rgba(255,255,255,0.82); margin-bottom: 2.5rem; }
.hero__btns  { display: flex; gap: 1rem; justify-content: center; flex-wrap: wrap; }
.hero > a, .hero > .btn  { margin-top: 0.5rem; }

/* ── About ── */
.about__grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4.5rem;
  align-items: center;
}
.about__media {
  border-radius: var(--radius-lg);
  overflow: hidden;
  background: var(--surface-2);
  min-height: 380px;
}
.about__media img { width: 100%; height: 100%; object-fit: cover; }
.about__text h2 { color: var(--primary); }
.about__text p  { color: var(--text-lt); }

/* ── Services / Cards ── */
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 1.75rem;
}
.card {
  background: var(--bg);
  border-radius: var(--radius-lg);
  padding: 2.25rem 2rem;
  border: 1px solid var(--border);
  transition: transform var(--ease), box-shadow var(--ease);
}
.card:hover { transform: translateY(-4px); box-shadow: var(--shadow-lg); }
.card__icon { font-size: 2rem; line-height: 1; margin-bottom: 1.25rem; color: var(--accent); }
.card h3 { color: var(--primary); }
.card p  { color: var(--text-lt); font-size: 0.9375rem; line-height: 1.75; }

/* ── Stats bar ── */
.stats { background: var(--primary); padding: 4.5rem 0; }
.stats__row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 2rem;
  text-align: center;
  max-width: var(--max-w);
  margin: 0 auto;
  padding: 0 2rem;
}
.stat__n {
  font-family: 'Playfair Display', Georgia, serif;
  font-size: clamp(2.25rem, 4vw, 3rem);
  font-weight: 700;
  color: var(--accent);
  display: block;
  line-height: 1;
  margin-bottom: 0.5rem;
}
.stat__l { color: rgba(255,255,255,0.75); font-size: 0.9375rem; }

/* ── Testimonials ── */
.reviews {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(290px, 1fr));
  gap: 1.75rem;
  margin-top: 3.5rem;
}
.review {
  background: var(--surface);
  border-radius: var(--radius-lg);
  padding: 2.25rem;
  border-left: 4px solid var(--accent);
}
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
.footer__row {
  max-width: var(--max-w);
  margin: 0 auto;
  padding: 0 2rem;
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 1.5rem;
}
.footer__brand { font-family: 'Playfair Display', Georgia, serif; font-size: 1.25rem; color: #fff; }
.footer__links { display: flex; gap: 2rem; list-style: none; }
.footer__links a { color: rgba(255,255,255,0.55); font-size: 0.875rem; transition: color var(--ease); }
.footer__links a:hover { color: var(--accent); }
.footer__copy { font-size: 0.8125rem; color: rgba(255,255,255,0.45); margin-bottom: 0; }

/* ── Generic fallback styles ──────────────────────────────────────────────────
   These fire when the model writes custom HTML without the template class names.
   They ensure a reasonable baseline without overriding specific classes above.  */
section { padding: 4.5rem 2rem; }
section + section { border-top: 1px solid var(--border); }
section h2 { color: var(--primary); margin-bottom: 1rem; }
section h3 { color: var(--primary); margin-bottom: 0.5rem; }
section p  { color: var(--text-lt); }
section ul, section ol { padding-left: 1.5rem; color: var(--text-lt); line-height: 2; }
section img { border-radius: var(--radius); }
section > div { max-width: var(--max-w); margin: 0 auto; }

/* Generic nav fallback */
nav { background: var(--bg); border-bottom: 1px solid var(--border); padding: 1rem 2rem; display: flex; align-items: center; justify-content: space-between; position: sticky; top: 0; z-index: 1000; }
nav a { color: var(--text); font-weight: 500; font-size: 0.9375rem; margin-right: 2rem; }
nav a:hover { color: var(--accent); }

/* Generic footer fallback */
footer { background: #0f1e2e; color: rgba(255,255,255,0.6); padding: 2.5rem 2rem; text-align: center; }
footer a { color: rgba(255,255,255,0.6); margin: 0 1rem; }
footer a:hover { color: var(--accent); }

/* ── Fade-in animations (JS-activated) ── */
.fade-up { opacity: 0; transform: translateY(22px); transition: opacity 0.6s ease, transform 0.6s ease; }
.fade-up.visible { opacity: 1; transform: translateY(0); }

/* ── Responsive ── */
@media (max-width: 900px) {
  .about__grid { grid-template-columns: 1fr; gap: 2.5rem; }
  .about__media { min-height: 260px; }
}
@media (max-width: 768px) {
  .nav__links, .nav__cta { display: none; }
  .nav__inner { padding: 1rem; }
  .hero__btns { flex-direction: column; align-items: center; }
  .footer__row { flex-direction: column; text-align: center; }
  .footer__links { flex-wrap: wrap; justify-content: center; }
}
@media (max-width: 480px) {
  .section { padding: 3.5rem 0; }
  .card    { padding: 1.75rem 1.5rem; }
  .cta     { padding: 4rem 0; }
}""",

        "script.js": """\
// {name}
document.addEventListener('DOMContentLoaded', () => {
  // Sticky nav shadow on scroll
  const nav = document.querySelector('.nav');
  if (nav) {
    window.addEventListener('scroll', () => {
      nav.style.boxShadow = window.scrollY > 20
        ? '0 2px 20px rgba(0,0,0,0.12)'
        : 'none';
    }, { passive: true });
  }

  // Smooth scroll for anchor links
  document.querySelectorAll('a[href^="#"]').forEach(a => {
    a.addEventListener('click', e => {
      const id = a.getAttribute('href');
      const target = id.length > 1 ? document.querySelector(id) : null;
      if (target) { e.preventDefault(); target.scrollIntoView({ behavior: 'smooth' }); }
    });
  });

  // Fade-in on scroll (cards, reviews, about panels, stats)
  const els = document.querySelectorAll('.card, .review, .about__media, .about__text, .stat');
  if ('IntersectionObserver' in window) {
    els.forEach(el => el.classList.add('fade-up'));
    const io = new IntersectionObserver(entries => {
      entries.forEach(e => {
        if (e.isIntersecting) { e.target.classList.add('visible'); io.unobserve(e.target); }
      });
    }, { threshold: 0.12 });
    els.forEach(el => io.observe(el));
  }
});""",
    },
}

# ── Professional CSS base used for auto-enhancement ───────────────────────────
# When the model writes sparse CSS (< 50 meaningful lines), write_file() merges
# the model's :root / @import into this base so the page still looks professional.
_CSS_PROFESSIONAL_BASE = _TEMPLATES["professional"]["style.css"]


def _auto_enhance_css(model_css: str) -> str:
    """
    If model_css is sparse (< 50 meaningful lines), merge it with the
    professional CSS base template. The model's :root variables and @import
    take precedence so custom brand colors / fonts are preserved.
    Returns the enhanced CSS string.
    """
    meaningful = [
        l for l in model_css.split("\n")
        if l.strip()
        and not l.strip().startswith("/*")
        and not l.strip().startswith("//")
        and l.strip() not in ("*/", "* {", "*{")
    ]
    if len(meaningful) >= 50:
        return model_css  # model wrote enough — trust it

    base = _CSS_PROFESSIONAL_BASE

    # Inject model's @import (Google Fonts override) if present
    import_match = re.search(r"@import[^;]+;", model_css)
    if import_match:
        base = re.sub(r"@import[^;]+;", import_match.group(0), base, count=1)

    # Inject model's :root block (custom brand colors) if present
    root_match = re.search(r":root\s*\{.*?\}", model_css, re.DOTALL)
    if root_match:
        base = re.sub(r":root\s*\{.*?\}", root_match.group(0), base, count=1, flags=re.DOTALL)

    # Strip already-merged pieces from model CSS and append the rest as overrides
    remaining = re.sub(r"@import[^;]+;", "", model_css)
    remaining = re.sub(r":root\s*\{.*?\}", "", remaining, flags=re.DOTALL)
    remaining = remaining.strip()

    if remaining:
        return base + "\n\n/* ── Custom overrides ── */\n" + remaining
    return base


# ── Internal helpers ──────────────────────────────────────────────────────────

def _slugify(name: str) -> str:
    """Convert a project name to a lowercase, URL-safe slug (hyphens replace special chars)."""
    name = name.strip().lower()
    name = re.sub(r"[^a-z0-9\-_]", "-", name)
    name = re.sub(r"-+", "-", name).strip("-")
    return name or "project"

def _safe_filename(filename: str) -> bool:
    """Return True if filename has an allowed extension and contains no path traversal sequences."""
    p = Path(filename)
    return p.suffix in ALLOWED_EXTENSIONS and p.name == str(p) and ".." not in filename

# ── UI/UX Design System ───────────────────────────────────────────────────────

_UI_UX_SCRIPTS = Path("/app/tools/ui_ux/scripts")

def get_design_system(description: str, project_name: str = None) -> str:
    """
    Generate an industry-matched design system for a web project.

    Uses the UI/UX Pro Max reasoning engine (161 rules, 67 styles, 161 color
    palettes, 57 font pairings) to recommend colors, typography, UI style,
    anti-patterns to avoid, and a pre-delivery checklist.

    Call this BEFORE scaffold() to get design context, then use patch_file()
    to apply the recommended colors and fonts to the scaffolded project.

    Args:
        description:  Natural language description of the project
                      (e.g. "beauty spa landing page", "SaaS dashboard fintech")
        project_name: Optional display name for the output header

    Returns:
        Formatted design system string with colors, fonts, style, anti-patterns.

    Example:
        <skill:web_builder.get_design_system>beauty spa landing page,Serenity Spa</skill:web_builder.get_design_system>
    """
    import sys
    scripts_path = str(_UI_UX_SCRIPTS)
    if scripts_path not in sys.path:
        sys.path.insert(0, scripts_path)
    try:
        from design_system import generate_design_system
        return generate_design_system(description, project_name, output_format="markdown")
    except ImportError:
        return (
            "❌ UI/UX Pro Max tools not found at /app/tools/ui_ux/scripts/. "
            "Ensure agent/tools/ui_ux/ is present in the repo and the container has been rebuilt."
        )
    except Exception as e:
        return f"❌ Design system error: {e}"


# ── Public skill functions ────────────────────────────────────────────────────

def scaffold(name: str, template: str = "blank") -> str:
    """
    Create a new website project with boilerplate files.

    Args:
        name:     Project name (e.g. "my-portfolio")
        template: "professional" | "blank" | "landing" | "dashboard"  (default: blank)
                  Use "professional" for all client websites — it includes a complete
                  styled template (hero, about, services, stats, testimonials, CTA, footer)
                  with 250+ lines of CSS. Then use patch_file() to update content & colors.

    Returns:
        Status string listing created files.

    Example:
        <skill:web_builder.scaffold>my-site,landing</skill:web_builder.scaffold>
    """
    if template not in _TEMPLATES:
        return f"❌ Unknown template '{template}'. Available: {list(_TEMPLATES.keys())}"

    slug = _slugify(name)
    project_dir = WEBSITES_DIR / slug

    if project_dir.exists():
        return f"⚠️ Project '{slug}' already exists. Use write_file() to edit files."

    project_dir.mkdir(parents=True, exist_ok=True)
    created = []
    for filename, content in _TEMPLATES[template].items():
        (project_dir / filename).write_text(content.replace("{name}", name), encoding="utf-8")
        created.append(f"  ✅ {filename}")

    return (
        f"🚀 Project '{slug}' created ({template} template):\n"
        + "\n".join(created)
        + f"\n\n⚠️ IMPORTANT: Do NOT use write_file() on index.html or style.css — this would overwrite the complete professional template and break the layout."
        + f"\n\nNext step: Use patch_file() to update content and apply design system colors/fonts."
        + f"\n  • patch_file({slug}, style.css, old_css_block, new_css_block) — swap :root color/font variables"
        + f"\n  • patch_file({slug}, index.html, placeholder_text, real_content) — replace each placeholder section"
        + f"\n  • patch_file({slug}, script.js, old_js, new_js) — only if JS changes needed"
        + f"\nFinish with: serve({slug})"
    )


def write_file(project: str, filename: str, content: str) -> str:
    """
    Write or overwrite a file in a project.

    Args:
        project:  Project slug (e.g. "my-site")
        filename: File to write (e.g. "index.html", "style.css", "app.js")
        content:  Full file content

    Returns:
        Status string.

    Example:
        <skill:web_builder.write_file>my-site,style.css,body { background: red; }</skill:web_builder.write_file>
    """
    if not _safe_filename(filename):
        return f"❌ Disallowed filename '{filename}'. Allowed extensions: {sorted(ALLOWED_EXTENSIONS)}"

    if not content or not content.strip():
        return (
            f"❌ write_file called with empty content for '{filename}'. "
            "You MUST provide the full file content as the third argument: "
            f"<skill:web_builder.write_file>{project},{filename},YOUR FULL CONTENT HERE</skill:web_builder.write_file>"
        )

    slug = _slugify(project)
    project_dir = WEBSITES_DIR / slug
    if not project_dir.exists():
        return f"❌ Project '{slug}' not found. Run scaffold() first."

    # For index.html: silently inject the CSS link and JS script tag if the
    # model forgot them — this is the most common cause of "no styling" results.
    if filename == "index.html":
        if 'href="style.css"' not in content and "href='style.css'" not in content:
            if "</head>" in content:
                content = content.replace(
                    "</head>",
                    '  <link rel="stylesheet" href="style.css">\n</head>'
                )
        if 'src="script.js"' not in content and "src='script.js'" not in content:
            if "</body>" in content:
                content = content.replace(
                    "</body>",
                    '  <script src="script.js" defer></script>\n</body>'
                )

    # Strip markdown code fences that coding models (like Qwen3.5) often wrap
    # content in even inside skill tags.  e.g. ```css\nbody{}\n``` becomes body{}
    _stripped = content.strip()
    _fence_match = re.match(r"^```[a-zA-Z]*\n?(.*?)\n?```\s*$", _stripped, re.DOTALL)
    if _fence_match:
        content = _fence_match.group(1).strip()
    elif _stripped.startswith("```"):
        # Partial fence (opening only, no closing — rare but possible)
        content = re.sub(r"^```[a-zA-Z]*\n?", "", _stripped, count=1).strip()

    # Auto-enhance sparse CSS so the page always looks professional.
    if filename == "style.css":
        content = _auto_enhance_css(content)

    filepath = project_dir / filename
    filepath.write_text(content, encoding="utf-8")
    return f"✅ {filename} written ({len(content)} chars)"


def read_file(project: str, filename: str) -> str:
    """
    Read a file from a project for review or editing.

    Args:
        project:  Project slug
        filename: File to read (e.g. "index.html")

    Returns:
        File content string.
    """
    if not _safe_filename(filename):
        return f"❌ Disallowed filename '{filename}'"

    slug = _slugify(project)
    filepath = WEBSITES_DIR / slug / filename
    if not filepath.exists():
        return f"❌ {filename} not found in project '{slug}'"

    content = filepath.read_text(encoding="utf-8")
    return f"📄 {filename} ({len(content)} chars):\n\n{content}"


def list_projects() -> str:
    """List all website projects with their files and sizes."""
    WEBSITES_DIR.mkdir(parents=True, exist_ok=True)
    projects = sorted([p for p in WEBSITES_DIR.iterdir() if p.is_dir()])
    if not projects:
        return "📭 No projects yet. Use scaffold() to create one."

    lines = [f"🌐 Website Projects ({len(projects)}):"]
    for p in projects:
        serving_tag = " 🟢 SERVING" if p.name == _serving_project else ""
        lines.append(f"\n  📁 {p.name}{serving_tag}")
        for f in sorted(p.iterdir()):
            if f.is_file():
                lines.append(f"    - {f.name} ({f.stat().st_size} bytes)")
    return "\n".join(lines)


def serve(project: str, port: str = "8090") -> str:
    """
    Start a live preview HTTP server for a project.
    Open http://localhost:<port> in your browser to see it.

    Args:
        project: Project slug to serve
        port:    Port number (default: 8090)

    Returns:
        Status with preview URL.

    Example:
        <skill:web_builder.serve>my-site</skill:web_builder.serve>
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

    # Shut down any existing server
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
            f"Edit files with write_file() then refresh. Call stop_server() when done."
        )
    except OSError as e:
        return f"❌ Could not start server on port {port_int}: {e}"


def stop_server() -> str:
    """Stop the running preview server."""
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
    """Check if the preview server is running."""
    if _httpd and _serving_project and _server_thread and _server_thread.is_alive():
        port = _httpd.server_address[1]
        return f"🟢 Serving '{_serving_project}' at http://localhost:{port}"
    return "🔴 No server running."


def patch_file(project: str, filename: str, old_string: str, new_string: str) -> str:
    """
    Make a targeted edit to a file — replace old_string with new_string.
    Use this instead of write_file() when changing a specific section.

    Args:
        project:    Project slug
        filename:   File to edit (e.g. "style.css")
        old_string: Exact text to find (must be unique in the file)
        new_string: Text to replace it with

    Returns:
        Status string, or error if old_string not found / not unique.
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
        return f"❌ Text not found in {filename} — check for exact match including whitespace."
    if count > 1:
        return (
            f"❌ Found {count} matches for that text in {filename}. "
            "Provide more surrounding context to make it unique."
        )

    updated = content.replace(old_string, new_string, 1)
    filepath.write_text(updated, encoding="utf-8")
    return f"✅ {filename} patched — 1 replacement made."


def delete_file(project: str, filename: str) -> str:
    """
    Delete a file from a project.

    Args:
        project:  Project slug
        filename: File to delete
    """
    if not _safe_filename(filename):
        return f"❌ Disallowed filename '{filename}'"

    slug = _slugify(project)
    filepath = WEBSITES_DIR / slug / filename
    if not filepath.exists():
        return f"❌ {filename} not found in project '{slug}'"

    filepath.unlink()
    return f"🗑️ {filename} deleted from '{slug}'."


def delete_project(project: str) -> str:
    """
    Permanently delete an entire project and all its files.

    Args:
        project: Project slug to delete
    """
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
    """
    Pack a project into a zip file for download or handoff.
    The zip is saved to /app/memory/websites/<project>.zip

    Args:
        project: Project slug to export
    """
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


# ── 5-Dimension Expert Critique Framework ────────────────────────────────────
#
# Based on the huashu-design 5-dimension evaluation system.
# Each dimension is scored 1-5 and contributes to a radar-score output.
#

_5D_DIMENSIONS = {
    "philosophical_coherence": {
        "name": "Philosophical Coherence",
        "description": "Does every design choice serve a clear concept? Consistent mood, voice, and visual logic throughout.",
        "weights": [
            "Color palette maps to brand personality",
            "Typography choices reinforce hierarchy and tone",
            "Spacing rhythm is intentional and consistent",
            "No jarring style shifts between sections",
        ],
    },
    "visual_hierarchy": {
        "name": "Visual Hierarchy",
        "description": "Is the eye guided naturally from most to least important? Primary CTA is unmissable.",
        "weights": [
            "Hero or entry point has clear dominant element",
            "Section order follows user attention flow",
            "Contrast between heading/body levels is distinct",
            "CTAs are visually differentiated from body text",
        ],
    },
    "execution_craft": {
        "name": "Execution Craft",
        "description": "Is the code clean, production-quality? No shortcuts, no placeholder feel.",
        "weights": [
            "CSS is semantic and maintainable (:root vars used)",
            "No inline styles on layout elements",
            "Responsive breakpoints are logical and complete",
            "Accessibility: alt text, ARIA labels, focus states",
        ],
    },
    "functionality": {
        "name": "Functionality",
        "description": "Does it work correctly across devices? Interactions behave as expected.",
        "weights": [
            "Navigation is usable and consistent across pages",
            "Forms and CTAs are functional (not decorative)",
            "No layout shift on load (CLS-friendly)",
            "Touch/click targets meet 44px minimum",
        ],
    },
    "innovation": {
        "name": "Innovation",
        "description": "Does the design have personality? It avoids generic patterns and brings something ownable.",
        "weights": [
            "Avoids generic AI-slop aesthetic (see anti-slop list)",
            "Has a distinctive visual hook or memorable element",
            "Motion design adds meaning, not just decoration",
            "Copy is specific, not templated or vague",
        ],
    },
}

# ── Expanded Anti-Slop Design Vocabulary (huashu-design inspired) ─────────────
#
# Based on huashu-design's Anti AI-Slop Rules. Covers font choices,
# layout anti-patterns, color anti-patterns, copy anti-patterns, and
# interaction anti-patterns. Also includes 20 design vocabulary signals.
#

_ANTI_SLOP = [
    # ── Font anti-patterns ──────────────────────────────────────────────────
    ("'inter'",                          "Avoid 'Inter' — use Geist, Outfit, Satoshi, DM Sans, or Plus Jakarta Sans for a less-generic feel"),
    ('font-family: "Inter"',              "Avoid 'Inter' — use Geist, Outfit, Satoshi, DM Sans, or Plus Jakarta Sans"),
    ("'open sans'",                       "Avoid 'Open Sans' — overused in generic templates"),
    ("'roboto'",                          "Avoid 'Roboto' — too common in default Material Design usage"),
    ("'lato'",                            "Avoid 'Lato' — consider DM Sans, Nunito Sans, or Plus Jakarta Sans"),
    ("font-family: arial",                "Avoid Arial — system-ui or a distinctive Google Font ages better"),
    ("font-family: 'helvetica neue'",      "Avoid Helvetica Neue defaults — pair it with a purpose-chosen sans"),
    # ── Layout anti-patterns ────────────────────────────────────────────────
    ("justify-content: center",           "Centered layouts feel generic — try asymmetric balance (left-heavy or grid-offset)"),
    ("justify-content: space-between",    "Overused 'space-between' nav — try flex-start with deliberate spacing"),
    ("display: flex; flex-direction: column; align-items: center",
     "Column-center flex is a template signal — use grid or directional asymmetry"),
    ("text-align: center",                 "Excessive centering kills visual rhythm — center only hero text"),
    ("max-width: 1200px; margin: 0 auto",  "Generic container centering — add personality with offset grids"),
    # ── Color anti-patterns ────────────────────────────────────────────────
    ("#000000",                            "Pure black #000000 — use off-black like #09090b (zinc-950) or #0a0a0a"),
    ("#ffffff",                            "Pure white surfaces — off-white (#fafaf9 stone-50 or #f8fafc slate-50) feels warmer"),
    ("linear-gradient",                    "Gradients must be used intentionally — avoid the purple-blue AI glow"),
    ("background: linear-gradient(135deg", "135deg purple→blue gradient is the #1 AI-slop signal — replace with brand-appropriate palette"),
    ("box-shadow: 0 0 20px",               "Outer glow = AI-slop — use inner borders or tinted layered shadows"),
    ("box-shadow: 0 0 30px",               "Heavy outer glow — replace with subtle depth (2-4px blur, low opacity)"),
    ("box-shadow: 0 4px 6px",              "Generic Bootstrap shadow — use custom shadow tokens for your brand"),
    ("opacity: 0.8",                       "Generic opacity overlay — use a tinted rgba or blend mode instead"),
    # ── Copy anti-patterns ────────────────────────────────────────────────
    ("john doe",                           "Placeholder name 'John Doe' — use realistic names (e.g. Maria Chen, Aiden Park)"),
    ("sarah chan",                         "Generic name 'Sarah Chan' — use names fitting your audience and tone"),
    ("jack su",                            "Generic name 'Jack Su' — use contextually appropriate names"),
    ("99%",                                "Stat '99%' is too round — use organic values like 97.3%, 94.7%"),
    ("100%",                               "Stat '100%' is a trust-signal red flag — use realistic, specific percentages"),
    ("lorem ipsum",                        "Lorem ipsum present — replace with real content before evaluation"),
    ("placeholder text",                   "Placeholder text found — use real copy that serves the design"),
    ("coming soon",                        "'Coming Soon' placeholder — ensure all sections have real or designed content"),
    # ── Interaction anti-patterns ─────────────────────────────────────────
    ("h-screen",                           "Tailwind h-screen causes mobile address-bar issues — use min-h-[100dvh]"),
    ("transition: all 0.3s",              "Unspecific 'transition: all' is wasteful — target specific properties"),
    ("cursor: pointer",                    "Check: are all cursor:pointer elements actually clickable?"),
    # ── Structure anti-patterns ───────────────────────────────────────────
    ("<div><div><div>",                    "Deep nesting (3+ divs) — consider semantic HTML or flat flex/grid"),
    # ── AI-slop visual signals ────────────────────────────────────────────
    ("neon",                               "'Neon' aesthetic — use muted, sophisticated color palettes"),
    ("glassmorphism",                     "Glassmorphism overuse — only use when it genuinely adds depth"),
    ("background-blend-mode: multiply",    "Blend mode detected — verify it looks correct on all backgrounds"),
]

# ── Design vocabulary signals (positive — tell us what IS done right) ────────
_DESIGN_VOCABULARY_SIGNALS = [
    # Unique/quality fonts
    ("'outfit'",          "OUTFIT"),
    ("'satoshi'",         "SATOSHI"),
    ("'geist'",          "GEIST"),
    ("'plus jakarta sans'", "PLUS_JAKARTA"),
    ("'dm sans'",         "DM_SANS"),
    ("'nunito sans'",     "NUNITO_SANS"),
    ("'sora'",            "SORA"),
    ("'space grotesk'",   "SPACE_GROTESK"),
    # Distinctive color choices
    ("--stone-",          "STONE_PALETTE"),
    ("--zinc-",           "ZINC_PALETTE"),
    ("--amber-",          "WARM_ACCENT"),
    ("--teal-",           "COOL_TEAL_ACCENT"),
    ("--indigo-",         "DEEP_INDIGO"),
    # Intentional spacing
    ("--space-y-",        "TAILWIND_SPACE_SCALE"),
    ("--spacing-",        "NAMED_SPACING_SYSTEM"),
    # Motion
    ("cubic-bezier",      "CUSTOM_EASING"),
    ("ease-",             "NAMED_EASING"),
    # Layout
    ("grid-template",     "CSS_GRID_LAYOUT"),
    ("aspect-ratio",      "ASPECT_RATIO"),
]

def _score_5d(css_content: str, html_content: str, warnings: list) -> dict:
    """
    Score the project on 5 dimensions.
    Each dimension gets a score 1-5 and pass/fail items from its checklist.
    """
    combined = (css_content + " " + html_content).lower()

    def check_positive(signals: list) -> int:
        """Count how many positive vocabulary signals fire (max 5)."""
        return min(5, sum(1 for s, _ in signals if s in combined))

    def check_negative(patterns: list) -> int:
        """Count how many anti-slop patterns fire (max 10)."""
        return min(10, sum(1 for pat, _ in patterns if pat.lower() in combined))

    scores = {}
    for dim_key, dim in _5D_DIMENSIONS.items():
        base = 3  # start at 3 (adequate)
        # Penalise anti-patterns
        base -= check_negative(_ANTI_SLOP) * 0.3
        # Reward vocabulary signals
        base += check_positive(_DESIGN_VOCABULARY_SIGNALS) * 0.2
        # Reward: semantic CSS vars
        if "--" in css_content and ":root" in css_content:
            base += 0.1
        # Reward: custom easing (not generic ease)
        if "cubic-bezier" in css_content or "ease-" in css_content:
            base += 0.1
        # Penalty: generic shadow
        if "box-shadow: 0 4px 6px" in css_content.lower():
            base -= 0.3
        # Penalty: pure black/white
        if "#000000" in css_content.lower() or "#ffffff" in css_content.lower():
            base -= 0.2
        # Clamp 1-5
        score = max(1, min(5, round(base, 1)))
        scores[dim_key] = score

    avg = round(sum(scores.values()) / len(scores), 1)
    return {
        "scores": scores,
        "average": avg,
        "grade": "A" if avg >= 4.5 else "B" if avg >= 3.5 else "C" if avg >= 2.5 else "D" if avg >= 1.5 else "F",
        "dimensions": _5D_DIMENSIONS,
    }


def _render_radar_ascii(scores: dict) -> str:
    """Render a simple ASCII radar chart for the 5 dimensions."""
    dim_names = {
        "philosophical_coherence": "Coherence ",
        "visual_hierarchy":         "Hierarchy  ",
        "execution_craft":          "Craft      ",
        "functionality":            "Functionality",
        "innovation":                "Innovation  ",
    }
    labels = []
    for key, score in scores.items():
        bar = "■" * int(score) + "□" * (5 - int(score))
        labels.append(f"  {dim_names[key]}| {bar} {score}/5")
    return "\n".join(labels)


def validate(project: str) -> str:
    """
    Check index.html for common structural issues, run an expanded anti-slop
    design audit, and produce a 5-dimension expert critique with ASCII radar chart.

    Args:
        project: Project slug

    Returns:
        Validation report with HTML checks, anti-slop warnings, and 5D critique.

    Example:
        <skill:web_builder.validate>my-site</skill:web_builder.validate>
    """
    slug = _slugify(project)
    html_path = WEBSITES_DIR / slug / "index.html"
    if not html_path.exists():
        return f"❌ No index.html found in project '{slug}'"

    content = html_path.read_text(encoding="utf-8")
    lower = content.lower()
    issues = []
    warnings = []

    required = [
        ("<!doctype html",          "Missing DOCTYPE declaration"),
        ('<meta charset=',          "Missing charset meta tag"),
        ('<meta name="viewport"',   "Missing viewport meta (mobile responsiveness)"),
        ('<title>',                 "Missing <title> tag"),
        ('<html',                   "Missing <html> element"),
        ('<head',                   "Missing <head> element"),
        ('<body',                   "Missing <body> element"),
    ]
    for check, msg in required:
        if check not in lower:
            issues.append(f"  ❌ {msg}")

    if "style.css" not in content and "<style" not in lower:
        warnings.append("  ⚠️  No stylesheet linked (style.css or <style>)")
    if "script.js" not in content and "<script" not in lower:
        warnings.append("  ⚠️  No script linked (script.js or <script>)")

    # ── Expanded Anti-Slop Design Audit (huashu-design inspired) ───────────
    css_path = WEBSITES_DIR / slug / "style.css"
    css_content = ""
    if css_path.exists():
        css_content = css_path.read_text(encoding="utf-8")

    for pattern, msg in _ANTI_SLOP:
        if pattern.lower() in (content + css_content).lower():
            warnings.append(f"  ⚠️  {msg}")

    # Emoji detection (huashu-design anti-emoji policy)
    emoji_pattern = re.compile(r'[\U0001F300-\U0001F9FF]')
    if emoji_pattern.search(content):
        warnings.append("  ⚠️  Emojis detected — replace with high-quality icons (Radix, Phosphor) or clean SVG primitives")

    # ── 5-Dimension Expert Critique ─────────────────────────────────────────
    result_5d = _score_5d(css_content, content, warnings)

    # Try HTML parsing
    class _Checker(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parse_errors = []
        def handle_error(self, msg):
            self.parse_errors.append(msg)

    checker = _Checker()
    try:
        checker.feed(content)
        for err in checker.parse_errors:
            issues.append(f"  ❌ Parse: {err}")
    except Exception as e:
        issues.append(f"  ❌ Parse failed: {e}")

    # ── Assemble report ────────────────────────────────────────────────────
    lines = [f"🔍 Validating {slug}/index.html ({len(content)} chars)"]
    if issues:
        lines.append("\nErrors:")
        lines.extend(issues)
    if warnings:
        lines.append("\nWarnings (Anti-Slop):")
        lines.extend(warnings)

    lines.append("\n" + "─" * 56)
    lines.append(f"  🎯 5-DIMENSION EXPERT CRITIQUE  (Grade: {result_5d['grade']})")
    lines.append("─" * 56)
    lines.append(_render_radar_ascii(result_5d["scores"]))
    lines.append(f"  {'─' * 48}")
    lines.append(f"  Average score: {result_5d['average']}/5.0")
    lines.append("")
    lines.append("  Dimension guidance:")
    for dim_key, dim in result_5d["dimensions"].items():
        score = result_5d["scores"][dim_key]
        quality = "✅ Strong" if score >= 4 else "⚠️  Needs work" if score >= 2.5 else "❌ Weak"
        lines.append(f"    • {dim['name']}: {score}/5 — {quality}")
        lines.append(f"      {dim['description']}")

    if not issues and not warnings:
        lines.append("\n  ✅ All checks passed!")
    elif not issues:
        lines.append("\n  ✅ No errors (review warnings above)")

    return "\n".join(lines)


# ── Design Tone Presets (taste-skill inspired) ─────────────────────────────────

_DESIGN_TONES = {
    "soft": {
        "description": "Premium, airy, expensive-looking with softer contrast, more whitespace, and smooth spring motion",
        "css_vars": {
            "--radius": "1.5rem",
            "--shadow-sm": "0 2px 8px rgba(0,0,0,0.04)",
            "--shadow-md": "0 8px 24px rgba(0,0,0,0.06)",
            "--shadow-lg": "0 16px 48px rgba(0,0,0,0.08)",
            "--ease-spring": "cubic-bezier(0.34, 1.56, 0.64, 1)",
            "--ease-smooth": "cubic-bezier(0.16, 1, 0.3, 1)",
            "--transition": "all 0.4s cubic-bezier(0.16, 1, 0.3, 1)",
        },
        "anti_patterns": [
            "Avoid harsh borders — use subtle shadows instead",
            "Avoid tight padding — favor generous whitespace",
            "Avoid saturated colors — keep accents muted and elegant",
        ],
    },
    "minimalist": {
        "description": "Clean editorial product UI inspired by Notion/Linear — restrained palette, crisp structure",
        "css_vars": {
            "--radius": "0.5rem",
            "--shadow-sm": "none",
            "--shadow-md": "0 1px 2px rgba(0,0,0,0.05)",
            "--shadow-lg": "none",
            "--ease-spring": "cubic-bezier(0.25, 0.46, 0.45, 0.94)",
            "--ease-smooth": "cubic-bezier(0.25, 1, 0.5, 1)",
            "--transition": "all 0.2s ease",
        },
        "anti_patterns": [
            "Avoid decorative elements — every visual must have purpose",
            "Avoid multiple accent colors — use only black and one highlight",
            "Avoid heavy shadows — let content breathe through spacing",
        ],
    },
    "brutalist": {
        "description": "Swiss typography, sharp contrast, raw structure, experimental composition",
        "css_vars": {
            "--radius": "0px",
            "--shadow-sm": "4px 4px 0px #000",
            "--shadow-md": "6px 6px 0px #000",
            "--shadow-lg": "8px 8px 0px #000",
            "--ease-spring": "cubic-bezier(0.68, -0.55, 0.265, 1.55)",
            "--ease-smooth": "cubic-bezier(0.19, 1, 0.22, 1)",
            "--transition": "all 0.15s step-end",
        },
        "anti_patterns": [
            "Avoid rounded corners — sharp edges only",
            "Avoid subtle shadows — use hard offset shadows",
            "Avoid gradient text — solid colors, high contrast",
        ],
    },
}


def set_design_tone(project: str, tone: str) -> str:
    """
    Apply a design tone preset to an existing project.

    Injects CSS variables and provides anti-pattern guidance to achieve
    a specific visual style: soft (premium), minimalist (clean), or brutalist (bold).

    Args:
        project: Project slug
        tone:    "soft" | "minimalist" | "brutalist"

    Returns:
        Status with injected CSS variables and recommendations.

    Example:
        <skill:web_builder.set_design_tone>my-site,soft</skill:web_builder.set_design_tone>
    """
    tone = tone.lower().strip()
    if tone not in _DESIGN_TONES:
        return f"❌ Unknown tone '{tone}'. Available: {list(_DESIGN_TONES.keys())}"

    slug = _slugify(project)
    css_path = WEBSITES_DIR / slug / "style.css"
    if not css_path.exists():
        return f"❌ Project '{slug}' has no style.css"

    preset = _DESIGN_TONES[tone]
    css_vars_block = "\n".join(f"  {k}: {v};" for k, v in preset["css_vars"].items())

    existing = css_path.read_text(encoding="utf-8")

    # Find :root block with proper brace matching
    root_start = re.search(r":root\s*\{", existing)
    if not root_start:
        return "❌ Could not find :root in style.css"

    brace_start = root_start.end() - 1
    depth = 0
    root_end = brace_start
    for i, char in enumerate(existing[brace_start:], start=brace_start):
        if char == '{':
            depth += 1
        elif char == '}':
            depth -= 1
            if depth == 0:
                root_end = i
                break

    root_match = existing[brace_start:root_end + 1]

    new_root = root_match.rstrip()
    if not new_root.endswith(";"):
        new_root += ";"
    new_root += "\n" + css_vars_block

    new_css = existing[:brace_start] + new_root + "\n" + existing[root_end + 1:]

    css_path.write_text(new_css, encoding="utf-8")

    lines = [
        f"✅ Applied '{tone}' design tone to '{slug}'",
        f"\nStyle: {preset['description']}",
        f"\nInjected CSS variables:",
    ]
    for k, v in preset["css_vars"].items():
        lines.append(f"  --{k}: {v}")

    lines.append("\nAnti-patterns to avoid:")
    for ap in preset["anti_patterns"]:
        lines.append(f"  • {ap}")

    return "\n".join(lines)


_MEMORY_DIR = Path("/app/memory")
_IMAGE_EXTS  = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff", ".tif"}
_MAX_IMG_W   = 800


def _img_to_b64(path: Path) -> tuple:
    """Resize image to _MAX_IMG_W and return (base64_str, mime_type)."""
    mime_map = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".webp": "image/webp", ".gif": "image/gif",  ".bmp": "image/bmp",
        ".tiff": "image/tiff", ".tif": "image/tiff",
    }
    mime = mime_map.get(path.suffix.lower(), "image/jpeg")
    if HAS_PILLOW:
        img = _PILImage.open(path)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGB")
        w, h = img.size
        if w > _MAX_IMG_W:
            img = img.resize((_MAX_IMG_W, int(h * _MAX_IMG_W / w)), _PILImage.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode(), mime


def analyze_design_folder(folder: str, language: str = "English", model: str = None, device_frame: str = None) -> str:
    """
    Analyze all design images in a folder with ONE batched vision LLM call.
    Returns a JSON brief with section descriptions, colors, layout and typography.
    Optionally wraps the preview in a device frame (iPhone, android, macos, browser).
    Use this as the FIRST step of any website build — it replaces N separate image_viewer calls.

    Args:
        folder:       path to design images (absolute, or relative to /app/memory/knowledge/)
        language:     language for website content (default: English)
        model:        vision model override (default: OLLELLM_MODEL env var)
        device_frame: optional — wrap server URL in a device frame:
                      "iphone" | "android" | "macos" | "browser"
                      Saves the frame HTML to [folder]/device-frame-[type].html
                      and includes it in the JSON under `device_frame_html`.
    """
    if not HAS_LITELLM:
        return json.dumps({"error": "litellm not available"})

    p = Path(folder)
    if not p.is_absolute():
        candidate = _MEMORY_DIR / "knowledge" / p
        p = candidate if candidate.exists() else _MEMORY_DIR / p

    if not p.exists():
        # List what IS available so the model can self-correct immediately.
        available = []
        for _search in [_MEMORY_DIR / "knowledge", _MEMORY_DIR]:
            if _search.exists():
                available += [
                    str(_search / d.name)
                    for d in sorted(_search.iterdir())
                    if d.is_dir() and not d.name.startswith(".")
                ]
        hint = (
            f"Call analyze_design_folder with one of these paths: {available[:8]}"
            if available else
            "No folders found under /app/memory/knowledge/. Ask the user for the correct path."
        )
        return json.dumps({
            "error": f"Folder not found: {folder}",
            "available_folders": available[:8],
            "action_required": hint,
        })

    images = sorted([
        f for f in p.iterdir()
        if f.is_file() and f.suffix.lower() in _IMAGE_EXTS and not f.name.startswith(".")
    ])
    texts = sorted([
        f for f in p.iterdir()
        if f.is_file() and f.suffix.lower() in {".txt", ".md"} and not f.name.startswith(".")
    ])

    if not images:
        return json.dumps({"error": f"No images found in {p}"})

    # Local models have limited VRAM — cap at 6 images to avoid OOM / timeout.
    # Cloud vision APIs handle larger batches fine, so no cap there.
    model_source_check = os.getenv("MODEL_SOURCE", "cloud")
    local_img_cap = int(os.getenv("LOCAL_VISION_IMAGE_CAP", "6"))
    if model_source_check == "local" and len(images) > local_img_cap:
        print(f"⚠️  Capping images from {len(images)} → {local_img_cap} for local model")
        images = images[:local_img_cap]

    # Determine model, api_base, and api_key based on MODEL_SOURCE so that
    # analyze_design_folder works whether the agent is in local (Ollama) or
    # cloud (LiteLLM proxy) mode.
    model_source = os.getenv("MODEL_SOURCE", "cloud")
    api_base: str | None = None
    api_key:  str | None = None

    if not model:
        if model_source == "local":
            raw   = os.getenv("OLLAMA_MODEL", "llava")
            model = f"ollama/{raw}" if not raw.startswith("ollama/") else raw
        else:
            # Route through the LiteLLM proxy (trinity-default alias in litellm_config.yaml)
            model = os.getenv("LITELLM_DEFAULT_MODEL", "trinity-default")

    if model_source == "local":
        api_base = os.getenv("OLLAMA_API_BASE", "http://ollama:11434")
    else:
        api_base = os.getenv("LITELLM_API_BASE", "http://litellm:4000")
        api_key  = os.getenv("LITELLM_MASTER_KEY", os.getenv("API_KEY", "sk-1234567890"))

    # Build the analysis prompt (text only — images sent separately)
    img_labels = "\n".join(f"  Image {i+1}: {img.name}" for i, img in enumerate(images))
    prompt_text = (
        f"You are analyzing {len(images)} website design mockup image(s) for a site in {language}.\n"
        f"Images provided:\n{img_labels}\n\n"
        "Extract EXACT hex colors, fonts, and layout details so a developer can copy-paste them.\n\n"
        "Output ONLY a JSON object — no markdown fences, no explanation, just raw JSON.\n"
        "Use this EXACT structure (fill in real values from the images):\n"
        "{\n"
        '  "site_name": "Site Name",\n'
        '  "overall_style": "warm rustic lodge",\n'
        '  "overall_palette": "#2b3e50, #c4a882, #ffffff",\n'
        '  "css_variables": "--hero-bg: #2b3e50; --accent: #c4a882; --text: #333333; --bg: #fdf8f0; --surface: #f0e8d8;",\n'
        '  "font_import": "@import url(\'https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700&family=Lato:wght@300;400&display=swap\');",\n'
        '  "heading_font": "Playfair Display, Georgia, serif",\n'
        '  "body_font": "Lato, system-ui, sans-serif",\n'
        '  "sections": [\n'
        '    {\n'
        '      "name": "hero",\n'
        '      "bg_color": "#2b3e50",\n'
        '      "text_color": "#ffffff",\n'
        '      "accent_color": "#c4a882",\n'
        '      "content": "exact headline and subtitle from the image",\n'
        '      "css_hint": ".hero { background: linear-gradient(rgba(0,0,0,0.45),rgba(0,0,0,0.45)), url(hero.jpg) center/cover; }"\n'
        '    }\n'
        '  ]\n'
        "}\n\n"
        "Rules:\n"
        "1. One section object per visible section.\n"
        "2. EXACT hex colors from the images — no generic placeholders.\n"
        "3. css_variables must list ALL key colors as CSS custom properties.\n"
        "4. font_import must be a valid Google Fonts @import URL."
    )

    # Append any text/markdown context files
    for txt_path in texts:
        try:
            prompt_text += f"\n\n[Context: {txt_path.name}]\n{txt_path.read_text(encoding='utf-8', errors='replace')[:1500]}"
        except Exception:
            pass

    # Collect raw base64 strings (without data-URL prefix) for each image
    image_b64s = []
    for img_path in images:
        try:
            b64, _mime = _img_to_b64(img_path)
            image_b64s.append(b64)
        except Exception as e:
            prompt_text += f"\n[Could not load {img_path.name}: {e}]"

    try:
        if model_source == "local":
            # ── Direct Ollama call ───────────────────────────────────────────
            # Bypasses litellm entirely — same code path as _call_llm() in
            # app.py.  litellm adds format-conversion overhead and has its own
            # fixed 180-second timeout that we cannot override reliably.
            import requests as _req
            raw_model = model.replace("ollama/", "")
            ollama_payload = {
                "model": raw_model,
                "messages": [{
                    "role": "user",
                    "content": prompt_text,
                    "images": image_b64s,
                }],
                "stream": False,
                "options": {
                    "temperature": 0.1,
                    "num_ctx": int(os.getenv("OLLAMA_NUM_CTX", "32768")),
                    "num_predict": -1,
                },
            }
            resp = _req.post(
                f"{api_base}/api/chat",
                json=ollama_payload,
                timeout=SKILL_TIMEOUT,
            )
            resp.raise_for_status()
            raw = resp.json().get("message", {}).get("content", "").strip()
        else:
            # ── Cloud mode: litellm proxy ────────────────────────────────────
            content = [{"type": "text", "text": prompt_text}]
            for b64 in image_b64s:
                content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
            response = _litellm.completion(
                model=model,
                messages=[{"role": "user", "content": content}],
                timeout=SKILL_TIMEOUT,
                api_base=api_base,
                api_key=api_key,
            )
            raw = response.choices[0].message.content.strip()
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            try:
                brief = json.loads(m.group())
                brief["source_folder"] = str(p)
                brief["image_files"]   = [f.name for f in images]
                brief["language"]      = language
                # Attach device frame HTML if requested (saves to folder + includes in JSON)
                if device_frame:
                    frame_path = p / f"device-frame-{device_frame}.html"
                    frame_html = _device_frame(device_frame.lower(), "http://localhost:8090")
                    try:
                        frame_path.write_text(frame_html, encoding="utf-8")
                        brief["device_frame"]       = device_frame
                        brief["device_frame_file"]  = str(frame_path)
                        brief["device_frame_types"] = ["iphone", "android", "macos", "browser"]
                    except Exception:
                        pass
                return json.dumps(brief, indent=2, ensure_ascii=False)
            except json.JSONDecodeError:
                pass
        raw_output = {"raw_analysis": raw, "source_folder": str(p), "image_files": [f.name for f in images], "language": language}
        if device_frame:
            frame_path = p / f"device-frame-{device_frame}.html"
            frame_html = _device_frame(device_frame.lower(), "http://localhost:8090")
            try:
                frame_path.write_text(frame_html, encoding="utf-8")
                raw_output["device_frame"]       = device_frame
                raw_output["device_frame_file"]  = str(frame_path)
                raw_output["device_frame_types"] = ["iphone", "android", "macos", "browser"]
            except Exception:
                pass
        return json.dumps(raw_output, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e), "model_used": model})


# ── Device Frames (huashu-design inspired) ────────────────────────────────────
#
# Self-contained HTML/CSS frames for presenting websites in realistic contexts.
# Each frame includes the bezel, status bar, and a scrollable content area.
#

def _device_frame(frame_type: str, content_url: str) -> str:
    """
    Return a self-contained HTML page that shows content_url inside a
    realistic device frame (iPhone, Android, macOS, browser).

    Args:
        frame_type: "iphone" | "android" | "macos" | "browser"
        content_url: URL or path to the website (e.g. http://localhost:8090)

    Returns:
        HTML string for the framed device.
    """
    frames = {
        "iphone": {
            "width": "390",
            "height": "844",
            "css": """
  .frame {
    background: #1a1a1a;
    border-radius: 54px;
    padding: 12px;
    display: inline-block;
    box-shadow: 0 40px 80px rgba(0,0,0,0.35), inset 0 0 0 1px rgba(255,255,255,0.08);
    position: relative;
  }
  .screen {
    background: #fff;
    border-radius: 44px;
    overflow: hidden;
    width: 366px;
    height: 820px;
    position: relative;
  }
  .screen iframe { border: none; width: 100%; height: 100%; }
  .status-bar {
    position: absolute; top: 0; left: 0; right: 0;
    height: 54px; background: transparent;
    display: flex; justify-content: space-between; align-items: center;
    padding: 14px 28px 0;
    font-family: -apple-system, sans-serif;
    font-size: 14px; font-weight: 600; color: #fff;
    z-index: 10;
  }
  .dynamic-island {
    position: absolute; top: 12px; left: 50%; transform: translateX(-50%);
    width: 126px; height: 36px;
    background: #000;
    border-radius: 20px;
    z-index: 20;
  }
  .home-indicator {
    position: absolute; bottom: 8px; left: 50%; transform: translateX(-50%);
    width: 134px; height: 5px;
    background: rgba(255,255,255,0.35);
    border-radius: 3px;
  }
  .label { text-align:center; color:#6b7280; font-size:12px; margin-top:8px; font-family: -apple-system, sans-serif; }
            """,
            "html": """
    <div class="frame">
      <div class="screen">
        <div class="status-bar">
          <span>9:41</span><span>●●●●●</span><span>86%</span>
        </div>
        <div class="dynamic-island"></div>
        <iframe src="{url}" allow="fullscreen"></iframe>
        <div class="home-indicator"></div>
      </div>
    </div>
    <div class="label">iPhone 15 Pro</div>""",
        },
        "android": {
            "width": "412",
            "height": "915",
            "css": """
  .frame {
    background: #1f1f1f;
    border-radius: 44px;
    padding: 8px;
    display: inline-block;
    box-shadow: 0 40px 80px rgba(0,0,0,0.35);
    position: relative;
  }
  .screen {
    background: #fff;
    border-radius: 36px;
    overflow: hidden;
    width: 396px;
    height: 899px;
  }
  .screen iframe { border: none; width: 100%; height: 100%; }
  .status-bar {
    position: absolute; top: 0; left: 0; right: 0;
    height: 24px; background: transparent;
    display: flex; justify-content: flex-end; align-items: center;
    padding: 0 16px;
    font-size: 12px; color: #fff;
    z-index: 10;
  }
  .camera-dot {
    position: absolute; top: 6px; left: 50%; transform: translateX(-50%);
    width: 10px; height: 10px; background: #111; border-radius: 50%;
    z-index: 20;
  }
  .label { text-align:center; color:#6b7280; font-size:12px; margin-top:8px; font-family: sans-serif; }
            """,
            "html": """
    <div class="frame">
      <div class="screen">
        <div class="status-bar"><span style="font-size:11px">11:52</span><span>●●●●</span><span>92%</span></div>
        <div class="camera-dot"></div>
        <iframe src="{url}" allow="fullscreen"></iframe>
      </div>
    </div>
    <div class="label">Android</div>""",
        },
        "macos": {
            "width": "960",
            "height": "680",
            "css": """
  .frame {
    background: #2a2a2a;
    border-radius: 12px 12px 0 0;
    padding: 0;
    display: inline-block;
    box-shadow: 0 40px 80px rgba(0,0,0,0.35);
    overflow: hidden;
    width: 960px;
  }
  .titlebar {
    background: #3a3a3a;
    height: 28px;
    display: flex; align-items: center; padding: 0 12px;
    gap: 6px;
  }
  .dot { width: 12px; height: 12px; border-radius: 50%; }
  .dot-r { background: #ff5f57; }
  .dot-y { background: #febc2e; }
  .dot-g { background: #28c840; }
  .toolbar {
    background: #3a3a3a;
    height: 38px;
    border-top: 1px solid #2a2a2a;
    display: flex; align-items: center; padding: 0 12px; gap: 12px;
    font-size: 12px; color: #aaa;
    font-family: -apple-system, sans-serif;
  }
  .screen {
    background: #fff;
    height: 614px;
    overflow: hidden;
  }
  .screen iframe { border: none; width: 100%; height: 100%; }
  .label { text-align:center; color:#6b7280; font-size:12px; margin-top:8px; font-family: -apple-system, sans-serif; background:#1a1a1a; padding:6px; border-radius:0 0 12px 12px; }
            """,
            "html": """
    <div class="frame">
      <div class="titlebar">
        <div class="dot dot-r"></div>
        <div class="dot dot-y"></div>
        <div class="dot dot-g"></div>
      </div>
      <div class="toolbar">‹ › ↺ &nbsp;&nbsp; ─ &nbsp;&nbsp; macOS Desktop</div>
      <div class="screen">
        <iframe src="{url}" allow="fullscreen"></iframe>
      </div>
    </div>
    <div class="label">macOS Safari</div>""",
        },
        "browser": {
            "width": "1280",
            "height": "760",
            "css": """
  .frame {
    background: #e5e5e5;
    border-radius: 8px;
    padding: 16px 16px 0;
    display: inline-block;
    box-shadow: 0 20px 60px rgba(0,0,0,0.2);
    width: 1280px;
  }
  .toolbar {
    background: #f3f3f3;
    border: 1px solid #d0d0d0;
    border-bottom: none;
    border-radius: 6px 6px 0 0;
    height: 42px;
    display: flex; align-items: center; padding: 0 8px; gap: 8px;
  }
  .url-bar {
    flex: 1;
    background: #fff;
    border: 1px solid #ccc;
    border-radius: 6px;
    height: 26px;
    display: flex; align-items: center;
    padding: 0 10px;
    font-size: 12px; color: #666;
    font-family: system-ui, sans-serif;
  }
  .screen {
    background: #fff;
    border: 1px solid #d0d0d0;
    border-top: none;
    height: 718px;
    overflow: hidden;
  }
  .screen iframe { border: none; width: 100%; height: 100%; }
  .label { text-align:center; color:#6b7280; font-size:12px; margin-top:8px; font-family: system-ui, sans-serif; }
            """,
            "html": """
    <div class="frame">
      <div class="toolbar">
        <div style="color:#888; font-size:16px;">‹ ›</div>
        <div class="url-bar">{url}</div>
        <div style="color:#888; font-size:14px;">↻</div>
      </div>
      <div class="screen">
        <iframe src="{url}" allow="fullscreen"></iframe>
      </div>
    </div>
    <div class="label">Browser Window</div>""",
        },
    }

    if frame_type not in frames:
        return json.dumps({
            "error": f"Unknown frame type '{frame_type}'",
            "available": list(frames.keys()),
        })

    frame = frames[frame_type]
    html_body = frame["html"].replace("{url}", content_url)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Device Frame — {frame_type}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #f0f0f0;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      padding: 2rem;
      font-family: -apple-system, BlinkMacSystemFont, sans-serif;
    }}
{frame['css']}
  </style>
</head>
<body>
{html_body}
</body>
</html>"""


# ── Design Direction Advisor (huashu-design inspired) ─────────────────────────
#
# Based on the huashu-design 5 schools × 20 philosophies framework.
# Recommends 3 distinct design directions from different schools.
#

_DESIGN_SCHOOLS = {
    "swiss_international": {
        "name": "Swiss International",
        "description": "Grid-based, typographic precision, mathematical layout. Helvetica-era hierarchy refined for the web. Clean, trustworthy, institutional.",
        "philosophies": {
            "grid_rigor": {
                "name": "Grid Rigor",
                "description": "12-column grid, consistent gutters, mathematical whitespace. Everything aligns. The grid is the design.",
                "css_hints": "--cols: repeat(12,1fr); --gap: 1.5rem; --max-w: 1200px;",
                "mood_keywords": ["structured", "precise", "professional", "academic"],
            },
            "typographic_hierarchy": {
                "name": "Typographic Hierarchy",
                "description": "Type is the hero. Bold headlines, generous leading, restrained body. Numbers and headings do the visual work.",
                "css_hints": "--font-display: 'Playfair Display', serif; --font-body: 'DM Sans', sans-serif; --fs-h1: clamp(3rem,6vw,6rem); --lh-heading: 1.05;",
                "mood_keywords": ["editorial", "authoritative", "refined"],
            },
            "neutral_functional": {
                "name": "Neutral Functional",
                "description": "Colors are tools, not decoration. Black, white, one accent. Form follows function. Bauhaus meets modern SaaS.",
                "css_hints": "--color-primary: #000; --color-accent: #e63946; --color-bg: #fff; --radius: 0; --shadow: none;",
                "mood_keywords": ["minimal", "functional", "no-nonsense"],
            },
            "corporate_clarity": {
                "name": "Corporate Clarity",
                "description": "High-trust visual language. Blue primary, clear hierarchy, generous white space. Enterprise-quality feel without being boring.",
                "css_hints": "--color-primary: #1d4ed8; --color-bg: #f8fafc; --radius: 0.375rem; --shadow-sm: 0 1px 3px rgba(0,0,0,0.08);",
                "mood_keywords": ["trustworthy", "enterprise", "stable", "serious"],
            },
        },
    },
    "warm_organic": {
        "name": "Warm Organic",
        "description": "Curves, warmth, handcrafted feeling. Earth tones, natural textures, soft light. Feels human, approachable, and crafted by hand.",
        "philosophies": {
            "earthy_rustic": {
                "name": "Earthy Rustic",
                "description": "Forest greens, warm wood tones, natural linen textures. Feels like a craft shop, a farmers market, a warm cabin.",
                "css_hints": "--color-primary: #4a6741; --color-accent: #c8956c; --color-bg: #faf6f0; --font-display: 'Fraunces', serif; --radius: 1rem;",
                "mood_keywords": ["natural", "handcrafted", "earthy", "cozy"],
            },
            "soft_pastoral": {
                "name": "Soft Pastoral",
                "description": "Sage green, warm cream, dusty rose. Soft shadows, rounded forms, organic shapes. Calm like a countryside morning.",
                "css_hints": "--color-primary: #7c9a6e; --color-accent: #d4a574; --color-bg: #fdf8f0; --color-surface: #f5ede0; --radius: 1.5rem; --shadow-md: 0 8px 24px rgba(0,0,0,0.05);",
                "mood_keywords": ["gentle", "calm", "pastoral", "warm"],
            },
            "artisan_handcrafted": {
                "name": "Artisan Handcrafted",
                "description": "Visible imperfection as a feature. Texture overlays, slightly irregular shapes, warm photography. Digital craft with analog soul.",
                "css_hints": "--color-primary: #8b4513; --color-accent: #d2691e; --color-bg: #faf7f2; --radius: 0.25rem; --shadow: 4px 4px 0px rgba(0,0,0,0.1); --texture: url(grain.png);",
                "mood_keywords": ["handmade", "artisanal", "tactile", "unique"],
            },
            "boutique_luxury": {
                "name": "Boutique Luxury",
                "description": "Understated elegance. Cream backgrounds, gold accents, serif headlines, generous negative space. The quiet confidence of high-end.",
                "css_hints": "--color-primary: #1a1a1a; --color-accent: #c9a84c; --color-bg: #faf9f5; --font-display: 'Cormorant Garamond', serif; --radius: 0; --shadow: 0 0 40px rgba(0,0,0,0.06);",
                "mood_keywords": ["elegant", "sophisticated", "premium", "quiet"],
            },
        },
    },
    "digital_modern": {
        "name": "Digital Modern",
        "description": "Dark mode by default, neon accents, glassmorphism, futuristic. Feels like the next-generation product. Bold, confident, tech-forward.",
        "philosophies": {
            "dark_glass": {
                "name": "Dark Glass",
                "description": "Dark surfaces with frosted glass layers. Subtle glow, depth through blur. The UI feels like it exists in 3D space.",
                "css_hints": "--color-bg: #0a0a0f; --color-surface: rgba(255,255,255,0.05); --color-accent: #7c3aed; --shadow-md: 0 8px 32px rgba(124,58,237,0.15); --radius: 1rem;",
                "mood_keywords": ["futuristic", "sleek", "tech", "dark"],
            },
            "cyberpunk_neon": {
                "name": "Cyberpunk Neon",
                "description": "High contrast, electric color pops. Glow effects, sharp edges. Bold, loud, unapologetically digital.",
                "css_hints": "--color-bg: #050505; --color-primary: #00f5d4; --color-accent: #ff2d78; --shadow-glow: 0 0 20px rgba(0,245,212,0.4); --radius: 0; --font-display: 'Orbitron', monospace;",
                "mood_keywords": ["edgy", "electric", "bold", "urban"],
            },
            "neo_bauhaus": {
                "name": "Neo-Bauhaus",
                "description": "Red, yellow, blue primaries on white. Geometric shapes, strong black type. Reimagined modernist logic for the screen.",
                "css_hints": "--color-primary: #0050ef; --color-accent: #e63329; --color-accent2: #f5c400; --color-bg: #ffffff; --radius: 0; --shadow: 4px 4px 0 #000; --font-display: 'Archivo Black', sans-serif;",
                "mood_keywords": ["geometric", "bold", "primary", "structured"],
            },
            "motion_forward": {
                "name": "Motion Forward",
                "description": "Animation-first design. Scrolling reveals, parallax, spring physics. The page feels alive. Interaction is the aesthetic.",
                "css_hints": "--ease-spring: cubic-bezier(0.34,1.56,0.64,1); --ease-smooth: cubic-bezier(0.16,1,0.3,1); --transition: all 0.5s cubic-bezier(0.16,1,0.3,1); --shadow-md: 0 16px 48px rgba(0,0,0,0.12);",
                "mood_keywords": ["dynamic", "energetic", "alive", "playful"],
            },
        },
    },
    "editorial_storytelling": {
        "name": "Editorial Storytelling",
        "description": "Magazine-quality layout. Photography dominates, text breathes, sections feel like pages. Long-form content is a feature, not a bug.",
        "philosophies": {
            "editorial_magazine": {
                "name": "Editorial Magazine",
                "description": "Large hero photography, multi-column text, pull quotes, issue-style section breaks. The layout tells the story before you read.",
                "css_hints": "--font-display: 'Playfair Display', serif; --font-body: 'Lora', serif; --fs-h1: clamp(3rem,7vw,7rem); --max-w: 1400px; --color-bg: #fafaf8; --lh-body: 1.8;",
                "mood_keywords": ["magazine", "journalistic", "prestige", "long-form"],
            },
            "narrative_scroll": {
                "name": "Narrative Scroll",
                "description": "Single-column, long-scroll storytelling. Large type on dark backgrounds alternates with image sections. Like a beautifully printed annual report.",
                "css_hints": "--font-display: 'DM Serif Display', serif; --color-bg: #1a1a1a; --color-text: #f5f5f5; --fs-h1: clamp(2.5rem,6vw,6rem); --max-w: 680px; --lh-body: 1.9;",
                "mood_keywords": ["cinematic", "immersive", "story-driven", "evocative"],
            },
            "photo_first": {
                "name": "Photo First",
                "description": "Photography is 80% of the layout. Text is minimal, integrated into images via overlays and captions. The photo does the heavy lifting.",
                "css_hints": "--font-display: 'Space Grotesk', sans-serif; --font-weight: 700; --text-on-image: rgba(0,0,0,0.65); --overlay-gradient: linear-gradient(to top, rgba(0,0,0,0.8) 0%, transparent 60%);",
                "mood_keywords": ["visual", "immersive", "photo-driven", "bold"],
            },
            "vintage_print": {
                "name": "Vintage Print",
                "description": "Serif everything, two-column body, drop caps, ornate section dividers. Feels like a beautifully printed book. Tactile nostalgia for the screen.",
                "css_hints": "--font-display: 'EB Garamond', serif; --font-body: 'EB Garamond', serif; --color-bg: #f5f0e8; --color-text: #2c2416; --max-w: 700px; --lh-body: 1.85; --drop-cap: 3;",
                "mood_keywords": ["classic", "literary", "nostalgic", "timeless"],
            },
        },
    },
    "neo_botanical": {
        "name": "Neo-Botanical",
        "description": "Nature meets technology. Deep greens, organic shapes, botanical illustration, living texture. The digital space feels rooted, alive, and growing.",
        "philosophies": {
            "forest_tech": {
                "name": "Forest Tech",
                "description": "Deep forest greens paired with precise sans-serif type. Organic layouts with sharp typography. Nature's logic applied to UI.",
                "css_hints": "--color-primary: #1a3a2a; --color-accent: #4ade80; --color-bg: #0d1f15; --font-display: 'Space Grotesk', sans-serif; --radius: 0.5rem; --shadow-glow: 0 0 30px rgba(74,222,128,0.1);",
                "mood_keywords": ["deep", "rooted", "tech-nature", "premium"],
            },
            "botanical_illustration": {
                "name": "Botanical Illustration",
                "description": "Hand-drawn botanical elements, natural color palette, textured backgrounds. Each section has an illustrated accent — a leaf, a branch, a vine.",
                "css_hints": "--color-primary: #3d6b4f; --color-accent: #8b6914; --color-bg: #f8f4ef; --font-display: 'Cormorant Garamond', serif; --border: 1px solid rgba(61,107,79,0.2); --radius: 0;",
                "mood_keywords": ["illustrated", "botanical", "organic", "crafted"],
            },
            "verdant_minimal": {
                "name": "Verdant Minimal",
                "description": "Generous whitespace with a single botanical focal point. Restraint meets nature. The emptiness is the design.",
                "css_hints": "--color-primary: #2d5a3f; --color-bg: #fafcfb; --color-surface: #f0f7f2; --radius: 2rem; --shadow-sm: 0 2px 8px rgba(45,90,63,0.06); --font-body: 'DM Sans', sans-serif;",
                "mood_keywords": ["spacious", "clean", "natural", "tranquil"],
            },
            "living_texture": {
                "name": "Living Texture",
                "description": "Grain, paper, linen, living moss textures layered under clean layouts. Tactile surfaces in a digital space. Everything feels made, not generated.",
                "css_hints": "--color-primary: #4a6741; --color-bg: #f9f6f0; --texture-noise: url(noise.png); --border: 1px solid rgba(74,103,65,0.15); --radius: 0.75rem; --shadow-md: 0 12px 32px rgba(0,0,0,0.07);",
                "mood_keywords": ["textured", "tactile", "layered", "material"],
            },
        },
    },
}


def get_design_direction(project_name: str = None, industry: str = None, keywords: str = None) -> str:
    """
    Recommend 3 design directions from 5 schools × 20 philosophies.
    Picks the most relevant directions based on project name, industry, and keywords.

    Each recommendation includes: school name, philosophy name, description,
    CSS variable hints, and mood keywords.

    Args:
        project_name:  Name of the project (used for relevance scoring)
        industry:     Optional industry hint (e.g. "education", "saas", "restaurant")
        keywords:     Optional comma-separated style keywords (e.g. "warm, minimal, premium")

    Returns:
        Markdown-formatted recommendation with 3 directions and CSS hints.

    Example:
        <skill:web_builder.get_design_direction>Carden School,education,warm,premium,trustworthy</skill:web_builder.get_design_direction>
    """
    # Score each philosophy by relevance
    all_philosophies = []
    for school_key, school in _DESIGN_SCHOOLS.items():
        for phil_key, phil in school["philosophies"].items():
            score = 0
            # Project name scoring
            if project_name:
                pn = project_name.lower()
                for kw in phil.get("mood_keywords", []):
                    if kw in pn:
                        score += 2
                for word in phil["name"].lower().split():
                    if word in pn:
                        score += 1
            # Industry keyword scoring
            if industry:
                ind = industry.lower()
                for kw in phil.get("mood_keywords", []):
                    if kw in ind:
                        score += 3
            # Explicit keywords scoring
            if keywords:
                for kw in keywords.lower().replace(",", " ").split():
                    kw = kw.strip()
                    if kw in phil.get("mood_keywords", []):
                        score += 3
                    if kw in phil["name"].lower():
                        score += 2
                    if kw in phil.get("description", "").lower():
                        score += 1
            # Boost by school (favor diversity)
            score += hash(school_key) % 3
            all_philosophies.append({
                "school": school_key,
                "school_name": school["name"],
                "school_description": school["description"],
                "philosophy_key": phil_key,
                **phil,
                "score": score,
            })

    # Sort by score, pick top 3 from different schools
    all_philosophies.sort(key=lambda x: x["score"], reverse=True)
    selected = []
    used_schools = set()
    for p in all_philosophies:
        if p["school"] not in used_schools:
            selected.append(p)
            used_schools.add(p["school"])
        if len(selected) >= 3:
            break

    # Render
    lines = [f"## 🎨 Design Direction Advisor"]
    if project_name:
        lines.append(f"**Project:** {project_name}")
    if industry:
        lines.append(f"**Industry:** {industry}")
    if keywords:
        lines.append(f"**Keywords:** {keywords}")
    lines.append("")
    lines.append("Recommended 3 directions (from different schools):\n")

    for i, p in enumerate(selected, 1):
        emoji = ["🌿", "📐", "✨", "📖", "🌱"][list(_DESIGN_SCHOOLS.keys()).index(p["school"])]
        lines.append(f"### {i}. {emoji} {p['name']}  _(from {p['school_name']})_")
        lines.append(f"> {p['description']}")
        lines.append(f"- **Mood keywords:** {', '.join(p.get('mood_keywords', []))}")
        lines.append(f"- **CSS variable hints:**\n  ```css\n  {p.get('css_hints', '# no hints available')}\n  ```")
        lines.append("")

    lines.append("─" * 56)
    lines.append("**Next step:** Pick a direction → call `get_design_system()` with the mood keywords")
    lines.append("or your project description → scaffold() → build with the CSS hints above.")

    return "\n".join(lines)


__all__ = [
    "NAME", "DOC",
    "get_design_system",
    "get_design_direction",
    "analyze_design_folder",
    "scaffold", "write_file", "patch_file", "read_file",
    "delete_file", "delete_project", "export_zip",
    "list_projects", "serve", "stop_server",
    "server_status", "validate", "set_design_tone",
]
