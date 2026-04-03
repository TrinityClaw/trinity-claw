"""
website_cloner.py — Trinity Claw core skill

Scrapes a live website's CSS/HTML to extract design tokens (CSS variables,
colors, fonts, section structure) and optionally clones them into a
web_builder project.

Pipeline:
  1. Fetch page HTML + linked CSS files
  2. Parse :root variables, @import fonts, colors, section headings
  3. Scaffold a web_builder 'professional' project
  4. Patch :root and @import with extracted tokens
  5. Optionally patch hero heading/subtitle/nav brand (fidelity='full')
  6. Serve for live preview
"""

import datetime
import importlib.util
import json
import os
import re
import time
import random
from pathlib import Path
from urllib.parse import urlparse, urljoin

# Web fetches can take a few seconds; bump past the default 30s timeout.
SKILL_TIMEOUT = int(os.getenv("WEBSITE_CLONER_TIMEOUT", "60"))

NAME = "website_cloner"
DOC = (
    "Clone a live website's design system into a web_builder project. "
    "extract_tokens(url) → scrape URL and return JSON with CSS variables, colors, fonts, nav links, per-section heading/CTA colors, interaction types, and full section structure. "
    "clone(url, project_name?, fidelity?) → extract design tokens, scaffold blank project, write extracted colors/fonts into :root, serve. "
    "Returns extracted structure data (sections, nav links, colors, per-section bg/heading/CTA colors) for the agent to use when writing HTML+CSS. "
    "clone() provides the data foundation only — no HTML is generated. "
    "After clone() returns: follow web_clone.md Phases 3+4 to write style.css and index.html from scratch. "
    "Works best with beautifulsoup4 + Chrome/CDP running. requests required."
)

# ── Optional dependencies ─────────────────────────────────────────────────────

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

try:
    from bs4 import BeautifulSoup as _BS4
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

# ── Constants ─────────────────────────────────────────────────────────────────

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]

# Private/internal network prefixes — blocked to prevent SSRF
_BLOCKED_PREFIXES = (
    "localhost", "127.", "0.0.0.", "::1", "169.254.",
    "10.", "172.16.", "172.17.", "172.18.", "172.19.",
    "172.20.", "172.21.", "172.22.", "172.23.", "172.24.",
    "172.25.", "172.26.", "172.27.", "172.28.", "172.29.",
    "172.30.", "172.31.", "192.168.",
)

# ── Browser-session helpers ───────────────────────────────────────────────────
# browser_session.py (Playwright/CDP) is loaded lazily — if Chrome isn't running
# or Playwright isn't installed the whole browser path silently returns None and
# the skill falls back to pure HTTP extraction.
#
# _STRUCTURAL_JS — deeper pass that captures layout topology: section count,
# grid columns, nav links, background images, card patterns, stats format,
# hero type.  Run AFTER _COMPUTED_STYLE_JS so the page is fully settled.

def _load_browser_session():
    """
    Dynamically import browser_session from the same skills/core directory.
    Returns the module or None if not found/loadable.
    """
    try:
        bs_path = Path(__file__).parent / "browser_session.py"
        if not bs_path.exists():
            return None
        spec = importlib.util.spec_from_file_location("_bs_for_cloner", bs_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


# JS injected via _evaluate() to grab real computed colours/fonts.
# Returns JSON.stringify() so page.evaluate() gives us a plain Python string.
_COMPUTED_STYLE_JS = """
JSON.stringify((function() {
  function s(el) {
    if (!el) return null;
    var cs = window.getComputedStyle(el);
    return {
      bg:         cs.backgroundColor,
      color:      cs.color,
      font:       cs.fontFamily,
      fontSize:   cs.fontSize,
      fontWeight: cs.fontWeight,
      transition: cs.transition,
      animation:  cs.animation
    };
  }
  var h1  = document.querySelector('h1');
  var h2  = document.querySelector('h2');
  var nav = document.querySelector('nav, header');
  var btn = document.querySelector(
    'a[class*="btn"]:not([class*="close"]):not([class*="cookie"]), ' +
    'button:not([class*="close"]):not([class*="cookie"]):not([type="submit"])'
  );
  var footer = document.querySelector('footer');
  // First link that looks like a brand/nav link (color is often the brand accent)
  var link = document.querySelector('nav a, header a, .nav a, .header a, a[class*="nav"]');
  if (!link) link = document.querySelector('a[href]:not([href^="#"]):not([href^="mailto"]):not([href^="tel"])');
  // First section/div with a non-white, non-transparent background (hero or CTA candidates)
  var coloredSection = null;
  var sectionCandidates = document.querySelectorAll(
    'section, [class*="hero"], [class*="banner"], [class*="cta"], [class*="intro"], [class*="jumbotron"]'
  );
  for (var si = 0; si < Math.min(sectionCandidates.length, 20); si++) {
    var csBg = window.getComputedStyle(sectionCandidates[si]).backgroundColor;
    if (csBg && csBg !== 'rgba(0, 0, 0, 0)' && csBg !== 'rgb(255, 255, 255)' && csBg !== 'rgb(0, 0, 0)') {
      coloredSection = sectionCandidates[si];
      break;
    }
  }

  // Collect <img> srcs and CSS background-image URLs (capped to avoid huge payloads)
  var imgSrcs  = [];
  var bgImages = [];
  try {
    var imgs = document.querySelectorAll('img[src]');
    for (var i = 0; i < Math.min(imgs.length, 30); i++) {
      var src = imgs[i].currentSrc || imgs[i].src;
      if (src && src.indexOf('data:') !== 0) imgSrcs.push(src);
    }
    var allEls = document.querySelectorAll('*');
    for (var j = 0; j < Math.min(allEls.length, 300); j++) {
      var bg = window.getComputedStyle(allEls[j]).backgroundImage;
      if (bg && bg !== 'none') {
        var matches = bg.match(/url\\(["']?([^"')]+)["']?\\)/g) || [];
        for (var k = 0; k < matches.length; k++) {
          var clean = matches[k].replace(/^url\\(["']?/, '').replace(/["']?\\)$/, '');
          if (clean && clean.indexOf('data:') !== 0 && bgImages.indexOf(clean) === -1)
            bgImages.push(clean);
        }
      }
    }
  } catch(e) {}

  return {
    body:           s(document.body),
    h1:             s(h1),
    h2:             s(h2),
    nav:            s(nav),
    btn:            s(btn),
    footer:         s(footer),
    link:           s(link),
    coloredSection: s(coloredSection),
    h1Text:   h1 ? (h1.innerText || '').trim().slice(0, 120) : '',
    h2Text:   h2 ? (h2.innerText || '').trim().slice(0, 120) : '',
    title:    document.title,
    metaDesc: ((document.querySelector('meta[name="description"]') || {}).content || ''),
    imgSrcs:  imgSrcs,
    bgImages: bgImages
  };
})())
"""


_STRUCTURAL_JS = """
JSON.stringify((function() {
  function rgb2hex(rgb) {
    var m = rgb && rgb.match(/rgba?\\(\\s*(\\d+)\\s*,\\s*(\\d+)\\s*,\\s*(\\d+)/);
    if (!m) return '';
    var r=+m[1], g=+m[2], b=+m[3];
    if (r===0&&g===0&&b===0) return '';
    if (r===255&&g===255&&b===255) return '';
    return '#'+[r,g,b].map(function(x){return ('0'+x.toString(16)).slice(-2)}).join('');
  }
  function gridCols(el) {
    var cs = window.getComputedStyle(el);
    if (cs.display === 'grid') {
      var gtc = cs.gridTemplateColumns;
      if (!gtc || gtc === 'none') return 0;
      return gtc.trim().split(/\\s+(?=\\d|minmax|repeat|auto|fr)/).filter(Boolean).length;
    }
    if (cs.display === 'flex') {
      var children = Array.from(el.children).filter(function(c){
        return window.getComputedStyle(c).display !== 'none';
      });
      return children.length > 0 ? children.length : 1;
    }
    return 0;
  }
  function bgImageUrl(el) {
    var bi = window.getComputedStyle(el).backgroundImage;
    if (!bi || bi === 'none') return '';
    var m = bi.match(/url\\(["']?([^"')]+)["']?\\)/);
    return m ? m[1] : '';
  }
  function elText(el, max) {
    if (!el) return '';
    return (el.innerText || el.textContent || '').replace(/\\s+/g,' ').trim().slice(0, max||80);
  }

  // ── Nav links ──────────────────────────────────────────────────────────────
  var navLinks = [];
  var navEl = document.querySelector('nav, header nav, [role="navigation"]');
  if (navEl) {
    navEl.querySelectorAll('a').forEach(function(a){
      var t = elText(a, 40);
      if (t && navLinks.indexOf(t) === -1) navLinks.push(t);
    });
  }

  // ── Section inventory ──────────────────────────────────────────────────────
  var sectionEls = document.querySelectorAll(
    'main > section, main > div[class], body > section, body > div[class], ' +
    '[class*="section"], [class*="block"], [class*="row"]:not(tr):not(td)'
  );
  var sections = [];
  var seen = new Set();
  for (var i = 0; i < sectionEls.length && sections.length < 16; i++) {
    var el = sectionEls[i];
    // skip if ancestor already captured
    var skip = false;
    for (var p = el.parentElement; p; p = p.parentElement) {
      if (seen.has(p)) { skip = true; break; }
    }
    if (skip) continue;
    seen.add(el);

    var cs = window.getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    var rect = el.getBoundingClientRect();
    if (rect.height < 60) continue; // skip tiny elements

    var bgCol  = rgb2hex(cs.backgroundColor);
    var bgImg  = bgImageUrl(el);
    var h = el.querySelector('h1,h2,h3');
    var p = el.querySelector('p');
    var cta = el.querySelector('a[class*="btn"],button,[class*="cta"],[class*="button"]');

    // Grid/card count: look for immediate grid/flex child wrappers
    var gridEl = el.querySelector(
      '[class*="grid"],[class*="cards"],[class*="row"],[class*="list"],[class*="items"],[class*="flex"]'
    ) || el;
    var cols = gridCols(gridEl);

    // Per-element computed styles within this section (GitHub-style per-component extraction)
    var headingStyle = null;
    if (h) {
      var hcs2 = window.getComputedStyle(h);
      var hColor = rgb2hex(hcs2.color);
      headingStyle = {
        color:      hColor || null,
        fontSize:   hcs2.fontSize || null,
        fontFamily: hcs2.fontFamily ? hcs2.fontFamily.split(',')[0].trim().replace(/['"]/g,'') : null,
        fontWeight: hcs2.fontWeight || null
      };
    }
    var bodyStyle = null;
    if (p) {
      var bodyColor = rgb2hex(window.getComputedStyle(p).color);
      if (bodyColor) bodyStyle = { color: bodyColor };
    }
    var ctaStyle = null;
    if (cta) {
      var ccs2 = window.getComputedStyle(cta);
      ctaStyle = {
        bg:           rgb2hex(ccs2.backgroundColor) || null,
        color:        rgb2hex(ccs2.color) || null,
        borderRadius: ccs2.borderRadius || null
      };
    }
    var interactionType = 'static';
    if (el.querySelector('[data-aos],[data-wow],[data-scroll],[data-animate]') ||
        el.querySelector('.aos-init,.wow,.animated,.fade-in,.slide-in')) {
      interactionType = 'scroll-animated';
    } else if (el.querySelector('.carousel,.slider,.swiper,.splide,[data-slick]')) {
      interactionType = 'carousel';
    } else if (el.querySelector('[role="tabpanel"],.tabs,.tab-content,.accordion,.collapse')) {
      interactionType = 'tabbed';
    }

    sections.push({
      tag:             el.tagName.toLowerCase(),
      id:              el.id || '',
      cls:             (el.className || '').toString().replace(/\\s+/g,' ').trim().slice(0,80),
      bg:              bgCol,
      hasBgImg:        !!bgImg,
      bgImg:           bgImg.slice(0,120),
      display:         cs.display,
      cols:            cols,
      heading:         h  ? elText(h,  100) : '',
      subtext:         p  ? elText(p,  160) : '',
      ctaText:         cta ? elText(cta, 50) : '',
      textAlign:       cs.textAlign,
      minH:            Math.round(rect.height),
      headingStyle:    headingStyle,
      bodyStyle:       bodyStyle,
      ctaStyle:        ctaStyle,
      interactionType: interactionType
    });
  }

  // ── Hero type ─────────────────────────────────────────────────────────────
  var heroEl = document.querySelector(
    '[class*="hero"],[class*="banner"],[class*="jumbotron"],[class*="intro"],main > section:first-of-type,body > section:first-of-type'
  );
  var hero = null;
  if (heroEl) {
    var hcs = window.getComputedStyle(heroEl);
    hero = {
      hasBgImg:  !!bgImageUrl(heroEl),
      bg:        rgb2hex(hcs.backgroundColor),
      textAlign: hcs.textAlign,
      minH:      Math.round(heroEl.getBoundingClientRect().height)
    };
  }

  // ── Unique background color palette ──────────────────────────────────────
  var palette = [];
  document.querySelectorAll('section,header,footer,[class*="section"],[class*="hero"],[class*="cta"]').forEach(function(el){
    var c = rgb2hex(window.getComputedStyle(el).backgroundColor);
    if (c && palette.indexOf(c) === -1 && palette.length < 10) palette.push(c);
  });

  return {
    navLinks:    navLinks.slice(0, 12),
    sections:    sections,
    hero:        hero,
    palette:     palette,
    totalVisible: sections.length
  };
})())
"""


_HOVER_STATES_JS = """
JSON.stringify((function() {
  // Read :hover CSS rules directly from stylesheets — more reliable than simulating mouseenter.
  var rules = [];
  try {
    for (var si = 0; si < document.styleSheets.length; si++) {
      try {
        var cssRules = document.styleSheets[si].cssRules || [];
        for (var ri = 0; ri < cssRules.length; ri++) {
          var rule = cssRules[ri];
          if (rule.selectorText && rule.selectorText.indexOf(':hover') !== -1) {
            var text = rule.cssText || '';
            if (/background|color|border|transform|opacity|box-shadow/.test(text)) {
              rules.push({ sel: rule.selectorText.slice(0, 100), css: text.slice(0, 300) });
            }
          }
        }
      } catch(e) {}  // cross-origin sheets throw SecurityError
    }
  } catch(e) {}
  return rules.slice(0, 30);
})())
"""


def _rgb_to_hex(rgb: str) -> str:
    """Convert 'rgb(r, g, b)' / 'rgba(r, g, b, a)' to '#rrggbb'. Returns '' for default black."""
    m = re.match(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", rgb or "")
    if not m:
        return ""
    r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if r == 0 and g == 0 and b == 0:
        return ""   # skip default UA black — not a useful design token
    return f"#{r:02x}{g:02x}{b:02x}"


def _adjust_brightness(hex_color: str, factor: float) -> str:
    """Lighten (factor > 1) or darken (factor < 1) a #rrggbb color."""
    h = hex_color.lstrip("#")
    if len(h) != 6:
        return ""
    r = min(255, int(int(h[0:2], 16) * factor))
    g = min(255, int(int(h[2:4], 16) * factor))
    b = min(255, int(int(h[4:6], 16) * factor))
    return f"#{r:02x}{g:02x}{b:02x}"


def _computed_to_vars(computed: dict) -> dict:
    """
    Map getComputedStyle() results to semantic CSS variable names ready
    to merge into :root.  Covers the 'professional' web_builder template's
    full variable set so colors actually take effect visually.
    Skips empty/transparent/black defaults.
    """
    if not computed:
        return {}
    out = {}
    nav            = computed.get("nav")            or {}
    body           = computed.get("body")           or {}
    h1             = computed.get("h1")             or {}
    btn            = computed.get("btn")            or {}
    footer         = computed.get("footer")         or {}
    link           = computed.get("link")           or {}
    colored_sec    = computed.get("coloredSection") or {}

    # ── Primary brand color: nav bg → footer bg → first colored section bg ──
    nav_bg  = _rgb_to_hex(nav.get("bg", ""))
    primary = (
        nav_bg
        or _rgb_to_hex(footer.get("bg", ""))
        or _rgb_to_hex(colored_sec.get("bg", ""))
    )

    # ── Accent/CTA color: button bg → link color ──────────────────────────────
    btn_bg = _rgb_to_hex(btn.get("bg", ""))
    accent = btn_bg or _rgb_to_hex(link.get("color", ""))

    # ── Body colors ───────────────────────────────────────────────────────────
    body_bg   = _rgb_to_hex(body.get("bg", ""))
    body_text = _rgb_to_hex(body.get("color", ""))
    nav_text  = _rgb_to_hex(nav.get("color", ""))

    # ── Fonts ─────────────────────────────────────────────────────────────────
    body_font = body.get("font", "").strip()
    h1_font   = h1.get("font", "").strip()

    if primary:
        out["--primary"]    = primary
        out["--primary-lt"] = _adjust_brightness(primary, 1.4)
        out["--hero-bg"]    = primary
        h = primary.lstrip("#")
        if len(h) == 6:
            out["--hero-overlay"] = (
                f"rgba({int(h[0:2],16)},{int(h[2:4],16)},{int(h[4:6],16)},0.65)"
            )

    if accent:
        out["--accent"]    = accent
        out["--accent-dk"] = _adjust_brightness(accent, 0.8)

    if body_bg:   out["--bg"]        = body_bg
    if body_text: out["--text"]      = body_text
    if nav_bg:    out["--nav-bg"]    = nav_bg
    if nav_text:  out["--nav-text"]  = nav_text
    if body_font: out["--font-body"] = body_font
    if h1_font and h1_font != body_font:
        out["--font-heading"] = h1_font

    return out


def _browser_extract(url: str) -> "dict | None":
    """
    Navigate to url with browser_session (real Chrome / CDP), take a
    screenshot, and run getComputedStyle() on key elements.

    Returns a dict with:
      computed_vars   — CSS variable dict ready to merge into :root
      screenshot_path — path to the saved PNG (or '')
      h1_text         — innerText of first <h1> (or '')
      title           — document.title (or '')
      meta_desc       — <meta name="description"> content (or '')

    Returns None if browser_session is unavailable, Chrome isn't running,
    or any step fails — the caller falls back to HTTP-only extraction.
    """
    mod = _load_browser_session()
    if mod is None:
        return None

    try:
        nav_result = mod.goto(url)
        if "❌" in nav_result:
            return None
        time.sleep(1.5)   # let JS / web fonts settle

        # Scroll to bottom so lazy-loaded sections (footer, CTAs) render,
        # then back to top before screenshot so we capture the hero.
        try:
            mod.scroll("bottom")
            time.sleep(0.5)
            mod.scroll("top")
            time.sleep(0.3)
        except Exception:
            pass

        # Screenshot ──────────────────────────────────────────────────────────
        screenshot_path = ""
        try:
            ss = mod.screenshot()
            m = re.search(r"Screenshot saved:\s*(.+\.png)", ss)
            if m:
                screenshot_path = m.group(1).strip()
        except Exception:
            pass

        # Computed styles via private _evaluate ──────────────────────────────
        computed: dict = {}
        try:
            js_result = mod._evaluate(_COMPUTED_STYLE_JS)
            if "✅" in js_result and "\n" in js_result:
                raw = js_result.split("\n", 1)[1].strip()
                if raw:
                    computed = json.loads(raw)
        except Exception:
            pass

        # Structural layout extraction (second JS pass) ──────────────────────
        structure: dict = {}
        try:
            struct_result = mod._evaluate(_STRUCTURAL_JS)
            if "✅" in struct_result and "\n" in struct_result:
                raw_s = struct_result.split("\n", 1)[1].strip()
                if raw_s:
                    structure = json.loads(raw_s)
        except Exception:
            pass

        # Hover CSS rules (third JS pass — reads :hover from all stylesheets)
        hover_rules: list = []
        try:
            hover_result = mod._evaluate(_HOVER_STATES_JS)
            if "✅" in hover_result and "\n" in hover_result:
                raw_h = hover_result.split("\n", 1)[1].strip()
                if raw_h:
                    hover_rules = json.loads(raw_h)
        except Exception:
            pass

        return {
            "computed_vars":   _computed_to_vars(computed),
            "screenshot_path": screenshot_path,
            "h1_text":   (computed.get("h1Text")  or "").strip(),
            "title":     (computed.get("title")    or "").strip(),
            "meta_desc": (computed.get("metaDesc") or "").strip(),
            "img_srcs":  computed.get("imgSrcs",  []),
            "bg_images": computed.get("bgImages", []),
            "structure": structure,   # section layouts, nav links, palette
            "hover_rules": hover_rules,  # :hover CSS rules for buttons/links
        }

    except Exception:
        return None


# ── Private helpers ───────────────────────────────────────────────────────────

def _validate_url(url: str):
    """
    Return (ok: bool, normalized_url_or_error: str).
    Auto-prepends https:// if scheme is missing.
    Blocks access to private/internal hosts.
    """
    if not url or not isinstance(url, str):
        return False, "URL must be a non-empty string"
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        parsed = urlparse(url)
    except Exception:
        return False, f"Could not parse URL: {url!r}"
    if not parsed.netloc:
        return False, f"Invalid URL — no domain found in: {url!r}"
    hostname = (parsed.hostname or "").lower()
    for prefix in _BLOCKED_PREFIXES:
        if hostname == prefix.rstrip(".") or hostname.startswith(prefix):
            return False, f"Access to internal/private host '{hostname}' is blocked"
    return True, url


def _fetch(url: str, timeout: int = 15, is_css: bool = False):
    """Fetch URL with browser-like headers. Returns (text, final_url)."""
    headers = {
        "User-Agent": random.choice(_USER_AGENTS),
        "Accept": (
            "text/css,*/*;q=0.8"
            if is_css
            else "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    }
    resp = _requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    return resp.text, resp.url


def _extract_css_vars(css: str) -> dict:
    """Parse all CSS custom properties from :root { } blocks."""
    out = {}
    for block in re.findall(r":root\s*\{([^}]+)\}", css, re.DOTALL):
        for m in re.finditer(r"(--[\w-]+)\s*:\s*([^;]+);", block):
            out[m.group(1).strip()] = m.group(2).strip()
    return out


def _extract_font_imports(css: str) -> list:
    """Find all @import url(...) lines (typically Google Fonts)."""
    return re.findall(r"@import\s+url\(['\"]?[^'\")\s]+['\"]?\)[^;]*;", css)


def _color_vars(vars_dict: dict) -> dict:
    """Filter CSS variables whose values look like colors."""
    color_kw = (
        "color", "bg", "background", "text", "primary", "secondary",
        "accent", "surface", "border", "dark", "light", "muted",
        "subtle", "fill", "stroke", "heading", "hero",
    )
    color_val = re.compile(
        r"^(#[0-9a-fA-F]{3,8}|rgb[a]?\([^)]+\)|hsl[a]?\([^)]+\)"
        r"|oklch\([^)]+\)|oklab\([^)]+\)|color\([^)]+\))$"
    )
    return {
        k: v
        for k, v in vars_dict.items()
        if any(kw in k.lower() for kw in color_kw) and color_val.match(v.strip())
    }


def _extract_fonts(css: str) -> dict:
    """Extract font-family for body and headings from CSS rules and variables."""
    fonts = {}
    for selector, key in [
        (r"body\s*\{[^}]*font-family\s*:\s*([^;]+);", "body_font"),
        (r"h[12]\s*\{[^}]*font-family\s*:\s*([^;]+);", "heading_font"),
        (r"--font-body[^:]*:\s*([^;]+);", "body_font"),
        (r"--font-head[^:]*:\s*([^;]+);", "heading_font"),
        (r"--body-font[^:]*:\s*([^;]+);", "body_font"),
        (r"--heading-font[^:]*:\s*([^;]+);", "heading_font"),
    ]:
        m = re.search(selector, css, re.DOTALL)
        if m and key not in fonts:
            fonts[key] = m.group(1).strip()
    return fonts


def _parse_html_bs4(html: str, base_url: str) -> dict:
    """Full HTML parse using BeautifulSoup."""
    soup = _BS4(html, "html.parser")

    result = {
        "site_name": "",
        "meta_description": "",
        "nav_brand": "",
        "nav_links": [],
        "sections": [],
        "inline_css": "",
        "linked_css_urls": [],
    }

    tag = soup.find("title")
    if tag:
        result["site_name"] = tag.get_text(strip=True)

    meta = soup.find("meta", attrs={"name": re.compile("description", re.I)})
    if meta:
        result["meta_description"] = meta.get("content", "")

    for link in soup.find_all("link", rel=lambda r: r and "stylesheet" in r):
        href = link.get("href", "")
        if href and not href.startswith("data:"):
            result["linked_css_urls"].append(urljoin(base_url, href))

    result["inline_css"] = "\n".join(
        s.get_text() for s in soup.find_all("style")
    )

    nav = soup.find("nav") or soup.find(
        attrs={"class": re.compile(r"\bnav\b|\bheader\b", re.I)}
    )
    if nav:
        brand = nav.find(
            attrs={"class": re.compile(r"brand|logo|site.?name|title", re.I)}
        )
        if brand:
            result["nav_brand"] = brand.get_text(strip=True)[:80]
        result["nav_links"] = [
            a.get_text(strip=True)
            for a in nav.find_all("a")
            if a.get_text(strip=True)
        ][:8]

    for el in soup.find_all(["section", "header", "main", "footer", "article"]):
        sec = {
            "tag": el.name,
            "id": el.get("id", ""),
            "class": " ".join((el.get("class") or [])[:3]),
        }
        h = el.find(["h1", "h2", "h3"])
        if h:
            sec["heading"] = h.get_text(strip=True)[:120]
        p = el.find("p")
        if p:
            sec["text"] = p.get_text(strip=True)[:200]
        result["sections"].append(sec)

    return result


def _parse_structure_bs4(html: str, soup=None) -> dict:
    """
    Extract structural layout data from HTML using BeautifulSoup.
    Returns a dict in the same format as _STRUCTURAL_JS output so both
    paths (browser CDP and HTTP-only) feed the same downstream code.

    This is the HTTP fallback for when Chrome/CDP is unavailable.
    """
    if not HAS_BS4:
        return {}

    if soup is None:
        soup = _BS4(html, "html.parser")

    # ── Nav links ─────────────────────────────────────────────────────────────
    nav_links = []
    nav_el = soup.find("nav") or soup.find(attrs={"role": "navigation"})
    if nav_el:
        for a in nav_el.find_all("a"):
            t = a.get_text(strip=True)
            if t and t not in nav_links:
                nav_links.append(t[:40])

    # ── Section inventory ─────────────────────────────────────────────────────
    _SEC_TAGS = ["section", "article"]
    _SEC_CLS  = re.compile(
        r"section|block|hero|banner|cta|features|about|stats|statistic"
        r"|testimonial|contact|pricing|team|gallery|service|why|benefit"
        r"|intro|jumbotron|showcase|highlight|process|how",
        re.I,
    )
    _COL_HINTS = [
        (re.compile(r"(?:grid|col(?:umn)?s?)[_\- ]?4|four[-_ ]col|quad", re.I), 4),
        (re.compile(r"(?:grid|col(?:umn)?s?)[_\- ]?3|three[-_ ]col|trio|triple", re.I), 3),
        (re.compile(r"(?:grid|col(?:umn)?s?)[_\- ]?2|two[-_ ]col|dual|split|half", re.I), 2),
    ]

    sections = []
    seen = set()

    def _process(el):
        eid = id(el)
        if eid in seen:
            return
        seen.add(eid)

        cls_str = " ".join(el.get("class") or [])
        h = el.find(["h1", "h2", "h3"])
        p_tag = el.find("p")
        cta = el.find(attrs={"class": re.compile(r"btn|button|cta", re.I)})

        cols = 0
        # Try numeric class hint first (e.g. "grid-4", "col-3")
        m = re.search(r"(?:grid|col(?:umn)?)[_\- ](\d)", cls_str, re.I)
        if m:
            cols = int(m.group(1))
        else:
            for pat, n in _COL_HINTS:
                if pat.search(cls_str):
                    cols = n
                    break

        # If no class hint, count immediate children that look like cards
        if cols == 0:
            child_cls_re = re.compile(r"card|item|feature|col|box|tile|service", re.I)
            card_children = [
                c for c in el.find_all(True, recursive=False)
                if child_cls_re.search(" ".join(c.get("class") or []))
            ]
            if len(card_children) >= 2:
                cols = len(card_children)

        has_bg_img = bool(
            re.search(r"background-image\s*:|bg-img|bg-image", el.get("style", ""), re.I)
        )

        sections.append({
            "tag":       el.name,
            "id":        el.get("id", ""),
            "cls":       cls_str[:80],
            "bg":        "",
            "hasBgImg":  has_bg_img,
            "bgImg":     "",
            "display":   "block",
            "cols":      cols,
            "heading":   h.get_text(strip=True)[:100] if h else "",
            "subtext":   p_tag.get_text(strip=True)[:160] if p_tag else "",
            "ctaText":   cta.get_text(strip=True)[:50] if cta else "",
            "textAlign": "",
            "minH":      0,
        })

    # Collect semantic sections first
    for el in soup.find_all(_SEC_TAGS):
        _process(el)

    # Then class-matched divs not already covered
    for el in soup.find_all("div"):
        if len(sections) >= 16:
            break
        cls_str = " ".join(el.get("class") or [])
        if not _SEC_CLS.search(cls_str):
            continue
        # skip if a parent was already captured
        skip = any(id(p) in seen for p in el.parents)
        if skip:
            continue
        _process(el)

    return {
        "navLinks":     nav_links[:12],
        "sections":     sections,
        "hero":         None,
        "palette":      [],
        "totalVisible": len(sections),
        "_source":      "html_parse",
    }


def _parse_html_regex(html: str, base_url: str) -> dict:
    """Lightweight HTML parse using regex (fallback when BS4 unavailable)."""
    result = {
        "site_name": "",
        "meta_description": "",
        "nav_brand": "",
        "nav_links": [],
        "sections": [],
        "inline_css": "",
        "linked_css_urls": [],
    }

    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
    if m:
        result["site_name"] = m.group(1).strip()

    m = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)', html, re.I
    )
    if not m:
        m = re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']description["\']', html, re.I
        )
    if m:
        result["meta_description"] = m.group(1)

    # Linked CSS — two common attribute orderings
    for href in re.findall(
        r'<link[^>]+rel=["\']stylesheet["\'][^>]+href=["\']([^"\']+)["\']', html, re.I
    ):
        result["linked_css_urls"].append(urljoin(base_url, href))
    for href in re.findall(r'<link[^>]+href=["\']([^"\']+\.css)["\']', html, re.I):
        full = urljoin(base_url, href)
        if full not in result["linked_css_urls"]:
            result["linked_css_urls"].append(full)

    result["inline_css"] = "\n".join(
        re.findall(r"<style[^>]*>(.*?)</style>", html, re.DOTALL | re.I)
    )

    for h in re.findall(r"<h[12][^>]*>([^<]+)</h[12]>", html, re.I)[:6]:
        result["sections"].append({"heading": h.strip()})

    return result


def _clean_brand(text: str) -> str:
    """
    Strip SEO suffixes from a page title to get a short brand name.
    'Adventure.com | Travel Media Website of the Year 2024' → 'Adventure.com'
    """
    for sep in (" | ", " - ", " – ", " — ", " · ", " :: ", ": "):
        if sep in text:
            text = text.split(sep)[0].strip()
    return text[:60]


def _build_tokens(parsed: dict, all_css: str, url: str) -> dict:
    """Combine parsed HTML data and aggregated CSS into a structured token dict."""
    vars_dict = _extract_css_vars(all_css)
    font_imports = _extract_font_imports(all_css)
    fonts = _extract_fonts(all_css)

    root_block = ""
    if vars_dict:
        root_block = (
            ":root {\n"
            + "".join(f"  {k}: {v};\n" for k, v in vars_dict.items())
            + "}"
        )

    raw_brand = parsed.get("nav_brand", "") or parsed.get("site_name", "")
    return {
        "url": url,
        "site_name": parsed.get("site_name", ""),
        "meta_description": parsed.get("meta_description", ""),
        "nav_brand": _clean_brand(raw_brand),
        "nav_links": parsed.get("nav_links", []),
        "sections": parsed.get("sections", []),
        "design_tokens": {
            "css_variables_dict": vars_dict,
            "css_variables": "; ".join(f"{k}: {v}" for k, v in vars_dict.items()),
            "font_import": font_imports[0] if font_imports else "",
            "all_font_imports": font_imports,
            "heading_font": fonts.get("heading_font", ""),
            "body_font": fonts.get("body_font", ""),
            "color_vars": _color_vars(vars_dict),
        },
        "web_builder_patch": {
            "root_block": root_block,
            "font_import": font_imports[0] if font_imports else "",
        },
    }


def _load_web_builder():
    """
    Dynamically import web_builder from the same skills/core directory.
    Returns the module or None if not found/loadable.
    """
    try:
        wb_path = Path(__file__).parent / "web_builder.py"
        if not wb_path.exists():
            return None
        spec = importlib.util.spec_from_file_location("_wb_for_cloner", wb_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


def _read_project_css(wb, project: str) -> str:
    """
    Read style.css from a web_builder project, stripping the
    '📄 style.css (N chars):' header that read_file() prepends.
    """
    raw = wb.read_file(project, "style.css")
    idx = raw.find("\n\n")
    return raw[idx + 2:] if idx >= 0 else raw


# ── Structure-based HTML generation ──────────────────────────────────────────
# These functions build a custom index.html from the extracted structure data
# so that clone() produces a page that actually mirrors the source layout
# instead of always scaffolding the same fixed professional template.

def _el_style_attr(style_dict: "dict | None", *extra_props: str) -> str:
    """
    Build an inline style= attribute string from a per-section element style dict
    (headingStyle, ctaStyle, etc.) plus any additional CSS property strings.

    >>> _el_style_attr({"color": "#fff", "fontSize": "48px"})
    ' style="color:#fff;font-size:48px"'
    """
    parts = []
    if style_dict:
        if style_dict.get("color"):
            parts.append(f"color:{style_dict['color']}")
        if style_dict.get("bg"):
            parts.append(f"background-color:{style_dict['bg']}")
        if style_dict.get("borderRadius"):
            parts.append(f"border-radius:{style_dict['borderRadius']}")
        if style_dict.get("fontSize"):
            parts.append(f"font-size:{style_dict['fontSize']}")
        if style_dict.get("fontFamily"):
            parts.append(f"font-family:{style_dict['fontFamily']}")
    parts.extend(p for p in extra_props if p)
    return f' style="{";".join(parts)}"' if parts else ""

def _slug(text: str) -> str:
    """Convert text to a lowercase URL slug."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower().strip()).strip("-") or "section"


def _classify_section(sec: dict, position: int) -> str:
    """Return a section type string used to pick the right HTML block."""
    cls = (sec.get("cls", "") + " " + sec.get("id", "")).lower()
    cols = sec.get("cols", 0)
    if position == 0 or sec.get("hasBgImg") or any(
        k in cls for k in ["hero", "banner", "jumbotron", "splash", "masthead"]
    ):
        return "hero"
    if any(k in cls for k in ["stat", "metric", "number", "count", "figure", "impact", "achievement"]):
        return "stats"
    if any(k in cls for k in ["testimonial", "review", "quote", "feedback", "client"]):
        return "testimonials"
    if any(k in cls for k in ["cta", "call-to-action", "action-section", "contact-cta"]):
        return "cta"
    if any(k in cls for k in ["about", "story", "mission", "vision", "who-we", "team"]):
        return "about"
    if cols >= 2:
        return "features"
    return "generic"


def _section_html(sec: dict, position: int) -> str:
    """
    Generate HTML for one section using the professional template's CSS class names
    so the existing style.css styles it correctly without extra CSS.
    Per-section headingStyle/ctaStyle (from _STRUCTURAL_JS) are applied as inline
    styles so colors actually match the source site rather than just global tokens.
    """
    stype      = _classify_section(sec, position)
    heading    = sec.get("heading", "").strip()
    subtext    = sec.get("subtext", "").strip()
    cta_text   = sec.get("ctaText", "").strip()
    cols       = max(sec.get("cols", 0), 1)
    bg         = sec.get("bg", "")
    has_bg_img = sec.get("hasBgImg", False)
    sec_id     = sec.get("id", "") or _slug(heading or stype)
    style_attr = f' style="background-color:{bg}"' if bg else ""
    # Per-section element style attributes (from _STRUCTURAL_JS per-component extraction)
    h_style    = _el_style_attr(sec.get("headingStyle"))
    body_style = _el_style_attr(sec.get("bodyStyle"))
    cta_style  = _el_style_attr(sec.get("ctaStyle"))
    interaction = sec.get("interactionType", "static")
    interact_attr = f' data-interaction="{interaction}"' if interaction != "static" else ""

    if stype == "hero":
        h1  = heading or "Your Headline Goes Here"
        sub = subtext or "A compelling one-liner that tells visitors what you offer and why they should care."
        btn = cta_text or "Get Started"
        bg_cls = " hero--bg-img" if has_bg_img else ""
        return (
            f'  <section class="hero{bg_cls}" id="{sec_id}"{style_attr}{interact_attr}>\n'
            f'    <div class="hero__content">\n'
            f'      <h1 class="hero__title"{h_style}>{h1}</h1>\n'
            f'      <p class="hero__sub"{body_style}>{sub}</p>\n'
            f'      <div class="hero__btns">\n'
            f'        <a href="#contact" class="btn btn--accent"{cta_style}>{btn}</a>\n'
            f'        <a href="#about" class="btn btn--outline">Learn More</a>\n'
            f'      </div>\n'
            f'    </div>\n'
            f'  </section>'
        )

    if stype == "about":
        h2   = heading or "About Us"
        body = subtext or "Tell your story here. Explain who you are, what you stand for, and why clients choose you."
        btn  = cta_text or "Learn More"
        return (
            f'  <section class="about section" id="{sec_id}"{style_attr}{interact_attr}>\n'
            f'    <div class="container">\n'
            f'      <div class="about__grid">\n'
            f'        <div class="about__media"><img src="about.jpg" alt="{h2}"></div>\n'
            f'        <div class="about__text">\n'
            f'          <h2{h_style}>{h2}</h2>\n'
            f'          <p{body_style}>{body}</p>\n'
            f'          <a href="#" class="btn btn--dark"{cta_style}>{btn}</a>\n'
            f'        </div>\n'
            f'      </div>\n'
            f'    </div>\n'
            f'  </section>'
        )

    if stype == "features":
        col_count = min(cols, 4)
        h2  = heading or "Our Services"
        sub = subtext or "Describe your offering clearly. What problems do you solve for clients?"
        icons = ["◆", "★", "●", "▲"]
        cards = "\n        ".join(
            f'<div class="card">\n'
            f'          <div class="card__icon">{icons[j % 4]}</div>\n'
            f'          <h3>{"Feature " + str(j + 1)}</h3>\n'
            f'          <p>{"Key benefit or feature description for this offering." if j > 0 else sub[:120]}</p>\n'
            f'        </div>'
            for j in range(col_count)
        )
        col_style = f' style="grid-template-columns:repeat({col_count},1fr)"' if col_count != 3 else ""
        return (
            f'  <section class="services section section--alt" id="{sec_id}"{style_attr}{interact_attr}>\n'
            f'    <div class="container">\n'
            f'      <div class="section-header">\n'
            f'        <h2{h_style}>{h2}</h2>\n'
            f'        <p class="section-sub"{body_style}>{sub[:160]}</p>\n'
            f'      </div>\n'
            f'      <div class="cards"{col_style}>\n'
            f'        {cards}\n'
            f'      </div>\n'
            f'    </div>\n'
            f'  </section>'
        )

    if stype == "stats":
        nums = re.findall(r"\b\d[\d,\.%kxK]*\+?\b", heading + " " + subtext)
        defaults = [("10+", "Years Experience"), ("500+", "Happy Clients"), ("98%", "Satisfaction Rate"), ("24/7", "Support")]
        stat_items = "\n        ".join(
            f'<div class="stat">\n'
            f'          <span class="stat__n">{nums[j] if j < len(nums) else dnum}</span>\n'
            f'          <span class="stat__l">{dlabel}</span>\n'
            f'        </div>'
            for j, (dnum, dlabel) in enumerate(defaults[:4])
        )
        h_line = f'\n      <h2{h_style} style="text-align:center;margin-bottom:2rem">{heading}</h2>' if heading else ""
        return (
            f'  <section class="stats" id="{sec_id}"{style_attr}{interact_attr}>\n'
            f'    <div class="container">{h_line}\n'
            f'      <div class="stats__row">\n'
            f'        {stat_items}\n'
            f'      </div>\n'
            f'    </div>\n'
            f'  </section>'
        )

    if stype == "testimonials":
        h2 = heading or "What Our Clients Say"
        return (
            f'  <section class="testimonials section" id="{sec_id}"{style_attr}{interact_attr}>\n'
            f'    <div class="container">\n'
            f'      <div class="section-header"><h2{h_style}>{h2}</h2></div>\n'
            f'      <div class="reviews">\n'
            f'        <div class="review">\n'
            f'          <p class="review__text">"Add your first client testimonial here. Authentic quotes build trust."</p>\n'
            f'          <p class="review__name">Client Name</p>\n'
            f'          <p class="review__role">Title, Company</p>\n'
            f'        </div>\n'
            f'        <div class="review">\n'
            f'          <p class="review__text">"Include specific results — numbers and outcomes resonate with visitors."</p>\n'
            f'          <p class="review__name">Client Name</p>\n'
            f'          <p class="review__role">Title, Company</p>\n'
            f'        </div>\n'
            f'        <div class="review">\n'
            f'          <p class="review__text">"Social proof is one of the most powerful conversion tools."</p>\n'
            f'          <p class="review__name">Client Name</p>\n'
            f'          <p class="review__role">Title, Company</p>\n'
            f'        </div>\n'
            f'      </div>\n'
            f'    </div>\n'
            f'  </section>'
        )

    if stype == "cta":
        h2   = heading or "Ready to Get Started?"
        body = subtext or "Take the next step. Contact us today and let's talk about how we can help."
        btn  = cta_text or "Contact Us"
        return (
            f'  <section class="cta" id="{sec_id}"{style_attr}{interact_attr}>\n'
            f'    <div class="container">\n'
            f'      <div class="cta__inner">\n'
            f'        <h2{h_style}>{h2}</h2>\n'
            f'        <p{body_style}>{body}</p>\n'
            f'        <div class="cta__btns">\n'
            f'          <a href="mailto:hello@example.com" class="btn btn--accent"{cta_style}>{btn}</a>\n'
            f'          <a href="tel:+11234567890" class="btn btn--outline">Call Us</a>\n'
            f'        </div>\n'
            f'      </div>\n'
            f'    </div>\n'
            f'  </section>'
        )

    # generic
    h2   = heading or "Section"
    body = subtext or "Content goes here."
    btn_html = f'\n      <a href="#" class="btn btn--dark"{cta_style}>{cta_text}</a>' if cta_text else ""
    return (
        f'  <section class="section" id="{sec_id}"{style_attr}{interact_attr}>\n'
        f'    <div class="container">\n'
        f'      <h2{h_style}>{h2}</h2>\n'
        f'      <p{body_style}>{body}</p>{btn_html}\n'
        f'    </div>\n'
        f'  </section>'
    )


def _generate_section_css(structure: dict) -> str:
    """
    Generate per-section CSS rules from structure data using the same section-dedup
    logic as _build_clone_html(), so IDs match the generated HTML.

    Applies the source site's actual background, heading, and CTA button colors to
    each section — the key step that makes clones look visually distinct instead of
    always using the same navy-blue professional template colors.
    """
    sections = structure.get("sections", [])
    if not sections:
        return ""

    rules = []
    type_counts: dict = {}
    limits = {"hero": 1, "about": 1, "features": 2, "stats": 1,
              "testimonials": 1, "cta": 1, "generic": 3}

    for raw_i, sec in enumerate(sections[:14]):
        stype = _classify_section(sec, raw_i)
        if type_counts.get(stype, 0) >= limits.get(stype, 2):
            continue  # same skip logic as _build_clone_html
        type_counts[stype] = type_counts.get(stype, 0) + 1

        heading = (sec.get("heading") or "").strip()
        sec_id  = sec.get("id", "") or _slug(heading or stype)   # matches _section_html
        if not sec_id:
            continue

        bg         = sec.get("bg") or ""
        h_style    = sec.get("headingStyle") or {}
        body_style = sec.get("bodyStyle")    or {}
        cta_style  = sec.get("ctaStyle")     or {}

        if bg:
            rules.append(f"#{sec_id} {{ background-color: {bg}; }}")

        h_color = h_style.get("color")
        if h_color:
            rules.append(
                f"#{sec_id} h1, #{sec_id} h2, #{sec_id} h3 {{ color: {h_color}; }}"
            )

        p_color = body_style.get("color")
        if p_color and p_color != h_color:
            rules.append(f"#{sec_id} p {{ color: {p_color}; }}")

        cta_bg = cta_style.get("bg")
        if cta_bg:
            parts = [f"background-color:{cta_bg}"]
            if cta_style.get("color"):
                parts.append(f"color:{cta_style['color']}")
            if cta_style.get("borderRadius"):
                parts.append(f"border-radius:{cta_style['borderRadius']}")
            rules.append(f"#{sec_id} .btn {{ {'; '.join(parts)}; }}")

    if not rules:
        return ""
    return (
        "\n/* ── Per-section color overrides (auto-extracted from source) ── */\n"
        + "\n".join(rules)
    )


def _build_clone_html(structure: dict, tokens: dict, project_name: str) -> str:
    """
    Build a complete index.html that mirrors the source site's section structure.
    Uses the professional template's CSS class names so the existing style.css applies.
    Each section type maps to a known CSS class, column counts drive grid layout.
    """
    nav_links = (
        structure.get("navLinks", [])
        or tokens.get("nav_links", [])
        or ["Home", "About", "Services", "Contact"]
    )
    sections   = structure.get("sections", [])
    brand      = (
        tokens.get("nav_brand", "")
        or _clean_brand(tokens.get("site_name", "") or project_name)
    )
    title      = tokens.get("site_name", project_name)
    font_import = tokens.get("web_builder_patch", {}).get("font_import", "")
    year       = datetime.datetime.now().year

    nav_li = "\n        ".join(
        f'<li><a href="#{_slug(lnk)}">{lnk}</a></li>' for lnk in nav_links[:7]
    )
    footer_li = "\n        ".join(
        f'<li><a href="#{_slug(lnk)}">{lnk}</a></li>' for lnk in nav_links[:6]
    )

    # Build section HTML — honour source order, cap duplicates per type
    section_parts = []
    type_counts: dict = {}
    limits = {"hero": 1, "about": 1, "features": 2, "stats": 1, "testimonials": 1, "cta": 1, "generic": 3}

    if sections:
        for raw_i, sec in enumerate(sections[:14]):
            stype = _classify_section(sec, raw_i)
            if type_counts.get(stype, 0) < limits.get(stype, 2):
                # pass rendered position so hero detection works after dedup
                section_parts.append(_section_html(sec, len(section_parts)))
                type_counts[stype] = type_counts.get(stype, 0) + 1
    else:
        # Minimal fallback when nothing was extracted
        section_parts = [
            _section_html({"heading": brand, "subtext": tokens.get("meta_description", ""), "hasBgImg": False}, 0),
            _section_html({"heading": "About Us", "cols": 0}, 1),
            _section_html({"heading": "Our Services", "cols": 3}, 2),
        ]

    font_line  = f"  {font_import}\n" if font_import else ""
    sections_html = "\n\n".join(section_parts)

    return (
        f"<!DOCTYPE html>\n"
        f"<html lang=\"en\">\n"
        f"<head>\n"
        f"  <meta charset=\"UTF-8\">\n"
        f"  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n"
        f"  <title>{title[:80]}</title>\n"
        f"{font_line}"
        f"  <link rel=\"stylesheet\" href=\"style.css\">\n"
        f"</head>\n"
        f"<body>\n"
        f"  <nav class=\"nav\">\n"
        f"    <div class=\"nav__inner container\">\n"
        f"      <a class=\"nav__brand\" href=\"#\">{brand[:60]}</a>\n"
        f"      <ul class=\"nav__links\">\n"
        f"        {nav_li}\n"
        f"      </ul>\n"
        f"      <a href=\"#contact\" class=\"btn btn--dark nav__cta\">Contact</a>\n"
        f"    </div>\n"
        f"  </nav>\n\n"
        f"{sections_html}\n\n"
        f"  <footer class=\"footer\">\n"
        f"    <div class=\"container footer__row\">\n"
        f"      <span class=\"footer__brand\">{brand[:60]}</span>\n"
        f"      <ul class=\"footer__links\">\n"
        f"        {footer_li}\n"
        f"      </ul>\n"
        f"      <p class=\"footer__copy\">&copy; {year} {brand[:60]}. All rights reserved.</p>\n"
        f"    </div>\n"
        f"  </footer>\n"
        f"  <script src=\"script.js\"></script>\n"
        f"</body>\n"
        f"</html>"
    )


# ── Public API ─────────────────────────────────────────────────────────────────

def extract_tokens(url: str) -> str:
    """
    Scrape a live URL and extract its design tokens as JSON.

    Fetches the page HTML and up to 4 linked CSS files, then extracts:
    - CSS custom properties from :root { } blocks
    - Color variables (--primary, --bg, --accent, etc.)
    - Google Fonts @import URLs and font-family declarations
    - Navigation brand text and link labels
    - Page section structure (tag, id, class, heading, text snippet)

    The returned JSON includes a 'web_builder_patch' key with ready-to-use
    strings for patch_file() calls.

    Args:
        url: Page URL to scrape (http:// or https://). Scheme auto-added if missing.

    Returns:
        JSON string with design_tokens, sections, nav info, and web_builder_patch.

    Example:
        <skill:website_cloner.extract_tokens>https://example.com</skill:website_cloner.extract_tokens>
    """
    ok, url_or_err = _validate_url(url)
    if not ok:
        return json.dumps({"error": url_or_err})
    url = url_or_err

    if not HAS_REQUESTS:
        return json.dumps({
            "error": "requests library not installed",
            "fix": "pip install requests",
        })

    html = ""
    final_url = url
    _http_error = None
    try:
        html, final_url = _fetch(url)
    except Exception as e:
        _http_error = str(e)
        # Don't bail yet — browser_session navigates real Chrome and bypasses
        # 403s / bot-detection. We proceed with empty HTML and let the browser
        # path fill in computed styles. If that also fails we return the error below.

    if HAS_BS4:
        parsed = _parse_html_bs4(html, final_url)
    else:
        parsed = _parse_html_regex(html, final_url)

    # Collect CSS: inline styles first, then up to 4 linked CSS files
    all_css = parsed.get("inline_css", "")
    fetched_css = 0
    for css_url in parsed.get("linked_css_urls", [])[:4]:
        try:
            css_text, _ = _fetch(css_url, timeout=10, is_css=True)
            if css_text:
                all_css += "\n" + css_text
                fetched_css += 1
            time.sleep(0.25)  # polite rate-limiting
        except Exception:
            pass

    tokens = _build_tokens(parsed, all_css, final_url)

    # ── Real-browser enhancement (additive — never breaks HTTP path) ─────────
    browser_data = _browser_extract(final_url)
    if browser_data:
        computed_vars = browser_data.get("computed_vars", {})
        if computed_vars:
            all_vars = tokens["design_tokens"]["css_variables_dict"]
            all_vars.update(computed_vars)   # browser values override stylesheet vars
            tokens["design_tokens"]["css_variables"] = "; ".join(
                f"{k}: {v}" for k, v in all_vars.items()
            )
            tokens["design_tokens"]["color_vars"] = _color_vars(all_vars)
            # Rebuild root_block with merged vars so clone() picks them up
            tokens["web_builder_patch"]["root_block"] = (
                ":root {\n"
                + "".join(f"  {k}: {v};\n" for k, v in all_vars.items())
                + "}"
            )
            # Font fields used by clone() step 6
            dt = tokens["design_tokens"]
            if "--font-heading" in computed_vars and not dt.get("heading_font"):
                dt["heading_font"] = computed_vars["--font-heading"]
            if "--font-body" in computed_vars and not dt.get("body_font"):
                dt["body_font"] = computed_vars["--font-body"]

        if browser_data.get("screenshot_path"):
            tokens["screenshot_path"] = browser_data["screenshot_path"]

        # If HTML parsing found no headings, use browser h1Text (more reliable)
        h1_text = browser_data.get("h1_text", "")
        if h1_text and not any(s.get("heading") for s in tokens.get("sections", [])):
            tokens.setdefault("sections", []).insert(0, {"heading": h1_text})

        # Asset URLs discovered via browser JS (img srcs + CSS background-images)
        img_srcs  = browser_data.get("img_srcs",  [])
        bg_images = browser_data.get("bg_images", [])
        if img_srcs or bg_images:
            tokens["assets"] = {"img_srcs": img_srcs, "bg_images": bg_images}

        # Hover CSS rules — useful for patching button/link :hover in style.css
        hover_rules = browser_data.get("hover_rules", [])
        if hover_rules:
            tokens["hover_rules"] = hover_rules

        # Structural layout data (section topologies, nav links, palette)
        structure = browser_data.get("structure", {})
        if structure:
            tokens["structure"] = structure
            # Override nav_links with real browser-extracted ones (more reliable)
            nav_links_live = structure.get("navLinks", [])
            if nav_links_live:
                tokens["nav_links"] = nav_links_live

    # ── HTTP fallback: build structure from BS4 when browser had no structure ─
    # Runs when Chrome/CDP is unavailable OR when _STRUCTURAL_JS returned empty.
    if not tokens.get("structure") and html and HAS_BS4:
        try:
            # Re-use the already-parsed soup if we have it, else re-parse
            _soup = _BS4(html, "html.parser")
            http_structure = _parse_structure_bs4(html, soup=_soup)
            if http_structure.get("sections") or http_structure.get("navLinks"):
                tokens["structure"] = http_structure
                # Use nav links from BS4 if not already set
                if not tokens.get("nav_links") and http_structure.get("navLinks"):
                    tokens["nav_links"] = http_structure["navLinks"]
        except Exception:
            pass

    # If HTTP failed AND browser also got nothing, return the original error
    if _http_error and not browser_data:
        return json.dumps({"error": f"Failed to fetch {url}: {_http_error}"})

    tokens["_meta"] = {
        "parser": "browser_only" if _http_error else ("beautifulsoup" if HAS_BS4 else "regex"),
        "browser_used": browser_data is not None,
        "http_error": _http_error,
        "css_files_fetched": fetched_css,
        "inline_css_chars": len(parsed.get("inline_css", "")),
        "linked_css_found": len(parsed.get("linked_css_urls", [])),
        "assets_found": len(tokens.get("assets", {}).get("img_srcs", []))
                        + len(tokens.get("assets", {}).get("bg_images", [])),
    }

    return json.dumps(tokens, indent=2, ensure_ascii=False)


def clone(url: str, project_name: str = "", fidelity: str = "full") -> str:
    """
    CSS foundation setup for cloning: extract design tokens, scaffold a blank
    project, write extracted colors/fonts into :root, and serve.

    Steps:
      1. extract_tokens(url) — fetch HTML/CSS, run browser JS, collect design tokens
      2. web_builder.scaffold(project_name, 'blank') — clean slate, no template CSS
      3. Write base style.css: font @import + :root vars (extracted colors/fonts) + reset
      4. web_builder.serve(project_name) — start preview server

    After clone() returns, the agent MUST:
      - Write complete style.css (layout, sections, nav, buttons) from scratch
      - Write complete index.html matching the source site's actual structure
      Follow web_clone.md Phase 3 for exact instructions.

    Args:
        url:          Page URL to clone. Scheme auto-added if missing.
        project_name: Slug for the new project (auto-derived from domain if omitted).
        fidelity:     'tokens' — extract and print tokens JSON only, no project built.
                      'light'  — same as full (distinction removed, kept for compatibility).
                      'full'   — scaffold blank + write :root CSS + serve (DEFAULT).

    Returns:
        Status report with patches applied, warnings, and live preview URL.

    Example:
        <skill:website_cloner.clone>https://example.com</skill:website_cloner.clone>
        <skill:website_cloner.clone>https://stripe.com,stripe-clone,full</skill:website_cloner.clone>
    """
    ok, url_or_err = _validate_url(url)
    if not ok:
        return f"❌ {url_or_err}"
    url = url_or_err

    if fidelity not in ("tokens", "light", "full"):
        return (
            f"❌ fidelity must be 'tokens', 'light', or 'full'. Got: '{fidelity}'\n"
            "  tokens — extract design tokens only (no project built)\n"
            "  light  — scaffold + apply :root colors and font import\n"
            "  full   — light + patch hero heading, subtitle, title, nav brand"
        )

    # ── Step 1: extract tokens ───────────────────────────────────────────────
    tokens_json = extract_tokens(url)
    try:
        tokens = json.loads(tokens_json)
    except json.JSONDecodeError:
        return f"❌ Token extraction returned invalid JSON.\n{tokens_json}"

    if "error" in tokens:
        return f"❌ Token extraction failed: {tokens['error']}"

    if fidelity == "tokens":
        dt = tokens.get("design_tokens", {})
        summary_lines = [f"✅ Tokens extracted from: {url}", ""]
        color_vars = dt.get("color_vars", {})
        if color_vars:
            summary_lines.append(f"🎨 Color variables ({len(color_vars)}):")
            for k, v in list(color_vars.items())[:8]:
                summary_lines.append(f"   {k}: {v}")
        if dt.get("font_import"):
            summary_lines.append(f"\n🔤 Font import: {dt['font_import'][:100]}")
        if not color_vars and not dt.get("font_import"):
            summary_lines.append("ℹ️  No CSS variables or font imports found.")
        summary_lines.append(f"\nFull JSON:\n{tokens_json}")
        return "\n".join(summary_lines)

    # ── Step 2: derive project name from domain ──────────────────────────────
    if not project_name:
        domain = urlparse(url).netloc.replace("www.", "").split(".")[0]
        project_name = re.sub(r"[^a-z0-9-]", "-", domain.lower()).strip("-") + "-clone"

    # ── Step 3: load web_builder ─────────────────────────────────────────────
    wb = _load_web_builder()
    if wb is None:
        return (
            "❌ Could not load web_builder skill "
            "(expected at skills/core/web_builder.py).\n"
            f"Tokens were extracted — use them manually:\n\n{tokens_json}"
        )

    # ── Step 4: scaffold blank project ───────────────────────────────────────
    scaffold_result = wb.scaffold(project_name, "blank")
    if scaffold_result.startswith("❌"):
        return f"❌ Scaffold failed: {scaffold_result}"

    dt = tokens.get("design_tokens", {})
    font_import = tokens.get("web_builder_patch", {}).get("font_import", "")
    extracted_vars = dt.get("css_variables_dict", {})
    structure = tokens.get("structure", {})
    patches_ok = []
    patches_warn = []

    # ── Step 5: write base style.css — :root vars + font import + reset only ──
    # No layout CSS. The agent writes all layout rules from scratch in Phase 3.
    css_parts = []
    if font_import:
        css_parts.append(font_import)
        css_parts.append("")
    css_parts.append("*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }")
    css_parts.append("")
    if extracted_vars:
        css_parts.append(":root {")
        for k, v in extracted_vars.items():
            css_parts.append(f"  {k}: {v};")
        css_parts.append("}")
        css_parts.append("")
        patches_ok.append(f":root with {len(extracted_vars)} extracted vars")
    else:
        patches_warn.append("no CSS vars extracted — add colors manually to :root in Phase 3")
    css_parts.append("body { font-family: var(--font-body, system-ui, sans-serif); color: var(--text, #1f2937); background: var(--bg, #fff); line-height: 1.65; }")
    css_parts.append("img { max-width: 100%; display: block; }")
    css_parts.append(".container { max-width: 1140px; margin: 0 auto; padding: 0 2rem; }")
    wb.write_file(project_name, "style.css", "\n".join(css_parts))

    # ── Step 6: serve ────────────────────────────────────────────────────────
    serve_result = wb.serve(project_name)
    _url_m = re.search(r"https?://[^\s]+", serve_result)
    _preview_url = _url_m.group(0).rstrip(".,)") if _url_m else "http://localhost:8090"

    # ── Build report ─────────────────────────────────────────────────────────
    lines = [
        f"✅ clone() complete — CSS foundation ready, no HTML yet",
        f"   Source:  {url}",
        f"   Project: {project_name}",
        f"   Preview: {_preview_url}",
        f"   CSS:     {', '.join(patches_ok) if patches_ok else 'reset only'}",
    ]
    if patches_warn:
        lines.append(f"   ⚠️  {'; '.join(patches_warn)}")
    if tokens.get("screenshot_path"):
        lines.append(f"   Screenshot: {tokens['screenshot_path']}")
    lines.append("")

    # ── Extracted data summary — for agent use in Phase 3 ────────────────────
    color_vars = dt.get("color_vars", {})
    if extracted_vars:
        lines.append(f"🔧 :root vars ready ({len(extracted_vars)}): {', '.join(list(extracted_vars.keys())[:8])}")
    if color_vars:
        lines.append(f"🎨 Colors ({len(color_vars)}): {', '.join(f'{k}={v}' for k,v in list(color_vars.items())[:6])}")
    if font_import:
        lines.append(f"🔤 Font: {font_import[:80]}")

    secs = structure.get("sections", []) if structure else []
    nav_links_live = structure.get("navLinks", []) if structure else []
    palette = structure.get("palette", []) if structure else []

    if nav_links_live or secs:
        lines.append("")
        lines.append("🗂  Source structure (use this in Phase 3 to write the HTML):")
        if nav_links_live:
            lines.append(f"   Nav links: {' | '.join(nav_links_live[:8])}")
        if palette:
            lines.append(f"   Color palette: {', '.join(palette[:8])}")
        if secs:
            lines.append(f"   Sections ({len(secs)}) — in order:")
            for raw_i, sec in enumerate(secs[:14]):
                heading   = (sec.get("heading") or "").strip()[:50]
                bg        = sec.get("bg", "")
                cols      = sec.get("cols", 0)
                h_style   = sec.get("headingStyle") or {}
                cta_style = sec.get("ctaStyle") or {}
                interact  = sec.get("interactionType", "static")
                parts = []
                if heading: parts.append(f'"{heading}"')
                if bg:      parts.append(f"bg:{bg}")
                if cols > 1: parts.append(f"{cols}-col")
                if sec.get("hasBgImg"): parts.append("bg-img")
                if h_style.get("color"): parts.append(f"h-color:{h_style['color']}")
                if cta_style.get("bg"):  parts.append(f"btn:{cta_style['bg']}")
                if interact != "static": parts.append(f"[{interact}]")
                sec_id = sec.get("id", "") or _slug(heading or str(raw_i))
                lines.append(f"     {raw_i+1}. #{sec_id}  {' | '.join(parts)}")

    lines.append("")
    lines.append("🚨 DO NOT STOP. Follow web_clone.md Phase 3 NOW:")
    lines.append("   Write style.css and index.html from scratch using the structure data above.")

    return "\n".join(lines)


__all__ = [
    "NAME", "DOC",
    "extract_tokens",
    "clone",
]
