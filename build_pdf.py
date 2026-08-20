#!/usr/bin/env python3
"""
Build report.pdf from report.md.

Markdown -> styled HTML -> Chrome headless print-to-PDF. Chrome is used because
it is already on the machine and renders the print CSS faithfully; no LaTeX or
pandoc install is needed.

    python build_pdf.py                 # report.md -> report.pdf
    python build_pdf.py notes.md out.pdf
"""

from __future__ import annotations

import base64
import mimetypes
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import markdown

HERE = Path(__file__).resolve().parent

CSS = """
@page { size: A4; margin: 13mm 15mm 12mm 15mm; }

html { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
body {
  font-family: "Source Sans 3", "Segoe UI", Helvetica, Arial, sans-serif;
  font-size: 9.3pt; line-height: 1.34; color: #14161a; margin: 0;
  hyphens: auto; text-align: justify;
}

h1 {
  font-size: 17pt; line-height: 1.2; margin: 0 0 3mm 0; color: #0b0b0b;
  letter-spacing: -0.2pt;
}
h2 {
  font-size: 10.8pt; margin: 4.2mm 0 1.6mm 0; color: #14161a;
  padding-bottom: 1.1mm; border-bottom: 0.6pt solid #d8dade;
  break-after: avoid; page-break-after: avoid;
}
h3 { font-size: 10pt; margin: 4mm 0 1.5mm 0; break-after: avoid; }

p { margin: 0 0 1.9mm 0; orphans: 3; widows: 3; }
strong { color: #000; font-weight: 650; }

hr { border: 0; border-top: 0.6pt solid #d8dade; margin: 3mm 0; }

/* Figures ---------------------------------------------------------------- */
img {
  display: block; max-width: 100%; height: auto; margin: 1.5mm auto 0.8mm auto;
  break-inside: avoid; page-break-inside: avoid;
}
.figure {
  break-inside: avoid; page-break-inside: avoid;
  margin: 0 auto 2.2mm auto; max-width: 67%;
}
.caption {
  font-size: 8pt; color: #52514e; text-align: center; margin: 0 0 2mm 0;
  font-style: italic;
}

/* Tables ----------------------------------------------------------------- */
table {
  border-collapse: collapse; width: 100%; margin: 2mm 0 2.5mm 0;
  font-size: 8.5pt; break-inside: avoid; page-break-inside: avoid;
}
th, td { padding: 1.3mm 2mm; text-align: left; border-bottom: 0.5pt solid #e3e2de; }
th {
  background: #f3f4f6; font-weight: 650; color: #14161a;
  border-bottom: 0.9pt solid #c9ccd1;
}
tbody tr:last-child td { border-bottom: 0.9pt solid #c9ccd1; }

/* Lists ------------------------------------------------------------------ */
ol, ul { margin: 0 0 2mm 0; padding-left: 5.5mm; }
li { margin-bottom: 0.9mm; }

/* Inline code ------------------------------------------------------------ */
code {
  font-family: "DejaVu Sans Mono", Consolas, monospace; font-size: 8.4pt;
  background: #f3f4f6; padding: 0.3mm 1mm; border-radius: 1.5pt; color: #1f2937;
}

/* Wide figures (multi-panel comparisons) get the full text column ---------- */
.figure.wide { max-width: 100%; }
"""


def embed_images(html: str, base: Path) -> str:
    """Inline every local image as a data URI so the PDF is self-contained."""
    def repl(m):
        src = m.group(1)
        if src.startswith(("http://", "https://", "data:")):
            return m.group(0)
        path = (base / src).resolve()
        if not path.is_file():
            print(f"  WARNING: image not found: {src}")
            return m.group(0)
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        b64 = base64.b64encode(path.read_bytes()).decode()
        return f'src="data:{mime};base64,{b64}"'

    return re.sub(r'src="([^"]+)"', repl, html)


def wrap_figures(html: str) -> str:
    """Turn <p><img alt="..."></p> into a figure block with a caption."""
    def repl(m):
        img, alt = m.group(0), m.group(1)
        cap = f'<div class="caption">{alt}</div>' if alt.strip() else ""
        # Multi-panel comparison figures are wide and short -- give them the
        # full text column or the panel labels become unreadable.
        wide = " wide" if "compare" in img else ""
        return f'<div class="figure{wide}">{img}{cap}</div>'

    return re.sub(r'<p>\s*(?:<img[^>]*alt="([^"]*)"[^>]*>)\s*</p>',
                  lambda m: repl(m), html)


def build(md_path: Path, pdf_path: Path) -> None:
    text = md_path.read_text(encoding="utf-8")
    body = markdown.markdown(text, extensions=["tables", "attr_list", "sane_lists"])
    body = wrap_figures(body)
    body = embed_images(body, md_path.parent)

    html = (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{md_path.stem}</title><style>{CSS}</style></head>"
            f"<body>{body}</body></html>")

    chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable") \
        or shutil.which("chromium")
    if not chrome:
        sys.exit("No Chrome/Chromium found to render the PDF.")

    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "report.html"
        src.write_text(html, encoding="utf-8")
        subprocess.run([
            chrome, "--headless", "--disable-gpu", "--no-sandbox",
            f"--user-data-dir={tmp}/profile",
            "--no-pdf-header-footer",
            f"--print-to-pdf={pdf_path}",
            src.as_uri(),
        ], check=True, capture_output=True)

    print(f"  wrote {pdf_path}  ({pdf_path.stat().st_size/1024:.0f} kB)")


if __name__ == "__main__":
    md = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "report.md"
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else md.with_suffix(".pdf")
    build(md.resolve(), out.resolve())
