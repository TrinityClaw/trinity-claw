import os
import mimetypes
import base64
from typing import Dict, Any

# Optional PIL for metadata
try:
    from PIL import Image as PILImage
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


NAME = "image_viewer"
SHORT_DOC = "View, inspect metadata, list, and OCR-extract text from local image files."
DOC = "Load and analyse local image files. Functions: view_image(path) — render inline HTML preview, inspect_image(path) — metadata/EXIF, list_images(dir) — list images in a folder, extract_text(path, lang='eng') — OCR: extract all text from any image (screenshots, documents, receipts, photos). Supported lang codes: eng (English), ita (Italian), spa (Spanish), fra (French), ell (Greek), srp (Serbian Cyrillic), srp_latn (Serbian Latin). Combine with '+' e.g. lang='eng+srp_latn'. Returns the raw text string."
VERSION = "1.1.0"


def view_image(path: str, max_w: int = 600, max_h: int = 400) -> Dict[str, Any]:
    """
    Render a local image file inline in the chat. Returns HTML preview + metadata.
    """
    res = {"html": "", "info": {}, "error": None}
    if not os.path.isfile(path):
        res["error"] = f"File not found: {path}"
        return res

    mime, _ = mimetypes.guess_type(path)
    if not mime or not mime.startswith("image/"):
        res["error"] = "Not an image file"
        return res

    try:
        with open(path, "rb") as f:
            raw = f.read()

        b64 = base64.b64encode(raw).decode()
        sz   = len(raw) / 1024 ** 2

        info = {"size_mb": round(sz, 2), "mime": mime}
        dims = ""
        attrs = f'width="{max_w}"'

        if PIL_AVAILABLE:
            img = PILImage.open(path)
            w, h = img.size
            info.update({"width": w, "height": h, "format": img.format})
            # maintain aspect ratio
            ratio = min(max_w / w, max_h / h)
            new_w, new_h = int(w * ratio), int(h * ratio)
            attrs = f'width="{new_w}" height="{new_h}"'

        html = f"""
        <div style="border:1px solid #ccc;padding:8px;margin:8px;">
          <img src="data:{mime};base64,{b64}" {attrs} style="max-width:100%;height:auto;">
          <div style="font-size:12px;font-family:mono;margin-top:8px;">
            <b>Path:</b> {os.path.basename(path)}<br>
            <b>Size:</b> {info["size_mb"]} MB
            {f'<br><b>Dimensions:</b> {w}×{h}' if 'width' in info else ''}
          </div>
        </div>"""
        res.update({"html": html, "info": info})

    except Exception as e:
        res["error"] = str(e)
    return res


def list_images(d: str = ".") -> str:
    """List readable image files in a directory."""
    good = []
    for f in os.listdir(d):
        full = os.path.join(d, f)
        if os.path.isfile(full):
            mime, _ = mimetypes.guess_type(full)
            if mime and mime.startswith("image/"):
                good.append(f"{f} ({os.path.getsize(full)/1024/1024:.2f} MB)")
    return "\n".join(good) if good else "No images found."


def inspect_image(path: str) -> Dict:
    """Return detailed metadata of an image (EXIF, etc.)"""
    meta = {"path": path, "error": None}
    if not os.path.isfile(path):
        meta["error"] = "Missing file"
        return meta
    try:
        from PIL import Image
        img = Image.open(path)
        meta.update({
            "width": img.width,
            "height": img.height,
            "format": img.format,
            "mode": img.mode,
            "size_bytes": os.path.getsize(path),
            "exif": img.getexif() or {}
        })
    except Exception as e:
        meta["error"] = str(e)
    return meta


def extract_text(path: str, lang: str = "eng") -> str:
    """
    Extract all text from an image using Tesseract OCR.

    Works on screenshots, scanned documents, receipts, signage, and
    any image that contains printed or typed text.

    Args:
        path: Path to the image file (JPEG, PNG, TIFF, BMP, WebP, etc.)
        lang: Tesseract language code. Default "eng" (English).
              Use "eng+fra" for English + French, "spa" for Spanish, etc.
              Full list: https://tesseract-ocr.github.io/tessdoc/Data-Files

    Returns:
        Extracted text as a plain string, or an error message prefixed with ❌.

    Examples:
        extract_text("/app/memory/screenshot.png")
        extract_text("/app/memory/receipt.jpg", lang="eng")
        extract_text("/app/memory/doc.png", lang="eng+deu")
    """
    if not os.path.isfile(path):
        return f"❌ File not found: {path}"

    mime, _ = mimetypes.guess_type(path)
    if not mime or not mime.startswith("image/"):
        return f"❌ Not an image file: {path}"

    try:
        import pytesseract
        from PIL import Image as PILImage
    except ImportError as exc:
        return (
            f"❌ OCR unavailable ({exc}). "
            "Ensure pytesseract and Pillow are installed and tesseract-ocr is on PATH."
        )

    try:
        img = PILImage.open(path)

        # Convert to RGB so Tesseract handles all modes (RGBA, P, L, etc.)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        text = pytesseract.image_to_string(img, lang=lang).strip()
        return text if text else "(no text detected in image)"
    except pytesseract.TesseractNotFoundError:
        return (
            "❌ Tesseract binary not found. "
            "Install it with: apt-get install -y tesseract-ocr"
        )
    except Exception as exc:
        return f"❌ OCR error: {exc}"