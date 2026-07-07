#!/usr/bin/env python3
"""Build-to-PDF pipeline for the zensical test site.

Since zensical has no native PDF export, this serves the built site over HTTP
and drives headless Chrome (system Chrome, via Playwright) to print each page
to PDF. Two things need help along the way:

  * mermaid  - zensical 0.0.47's theme bundle empties the <pre class="mermaid">
               blocks in a headless context without producing an <svg>, so we
               re-render each diagram ourselves from the source that survives in
               the on-disk HTML, using the mermaid library the page already
               loaded.
  * math     - arithmatex (generic) needs MathJax; we await MathJax typesetting
               before printing.

Outputs:
  pdfs/pages/NNN.pdf        one PDF per doc page
  pdfs/zensical-manual.pdf  all pages merged, one bookmark per page
"""
from __future__ import annotations

import html
import http.server
import json
import os
import re
import shutil
import socketserver
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright
from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
OUT = ROOT / "pdfs"
PAGES_DIR = OUT / "pages"
MANIFEST = ROOT / "docs" / "manifest.json"

# Strip navigation chrome so each printed page is just the article, full width.
PRINT_CSS = """
.md-header, .md-sidebar, .md-tabs, .md-footer, .md-content__button,
.md-top, .md-source, [data-md-component=announce], .md-nav { display:none !important; }
.md-main__inner, .md-content { margin:0 !important; max-width:none !important; }
.md-content__inner { padding-top:0 !important; }
.md-grid { max-width:none !important; }
"""

# Wide tables (5-6 columns) are wider than an A4 page, so their last column is
# clipped at the page edge (on screen Material scrolls them; print can't).
# Set FIT_WIDE_TABLES=1 to force every table to fit the page by shrinking the
# font and wrapping cells. Off by default so the PDF shows the true default look.
FIT_TABLES_CSS = """
.md-typeset__scrollwrap, .md-typeset__table { overflow: visible !important; }
.md-typeset table:not([class]) { width:100% !important; font-size:0.62rem !important;
    table-layout: fixed !important; }
.md-typeset table:not([class]) th, .md-typeset table:not([class]) td {
    white-space: normal !important; word-break: break-word !important; }
"""

MERMAID_RE = re.compile(r'<pre class="mermaid"><code[^>]*>(.*?)</code></pre>', re.S)

# Re-render mermaid from extracted source using the page's own mermaid library.
MERMAID_JS = r"""async (defs) => {
    if (!window.mermaid) return 'no-lib';
    try { window.mermaid.initialize({startOnLoad:false, securityLevel:'loose'}); } catch(e){}
    const nodes = [...document.querySelectorAll('.mermaid')];
    let ok = 0;
    for (let i=0; i<nodes.length; i++){
        const src = defs[i] || defs[defs.length-1] || '';
        try {
            const {svg} = await window.mermaid.render('pdfm'+i, src);
            nodes[i].innerHTML = svg;
            const s = nodes[i].querySelector('svg');
            const vb = (s.getAttribute('viewBox')||'0 0 700 400').split(/\s+/).map(Number);
            const w = Math.min(680, vb[2] || 680);
            s.setAttribute('width', w);
            s.setAttribute('height', Math.round(w*(vb[3]||400)/(vb[2]||700)));
            s.style.maxWidth = '100%';
            ok++;
        } catch(e){
            nodes[i].innerHTML = '<pre style="color:#b00">mermaid error: '+e.message+'</pre>';
        }
    }
    return ok;
}"""

MATHJAX_JS = r"""async () => {
    if (window.MathJax && MathJax.startup && MathJax.startup.promise) {
        await MathJax.startup.promise;
        if (MathJax.typesetPromise) await MathJax.typesetPromise();
        return 'typeset';
    }
    return 'no-mathjax';
}"""


def start_server() -> socketserver.TCPServer:
    os.chdir(SITE)
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    httpd.RequestHandlerClass.log_message = lambda *a, **k: None  # silence
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def extract_mermaid(url: str) -> list[str]:
    raw = (SITE / url / "index.html").read_text()
    return [html.unescape(m) for m in MERMAID_RE.findall(raw)]


def main() -> None:
    manifest = json.loads(MANIFEST.read_text())
    limit = int(os.environ.get("PDF_LIMIT", "0"))
    if limit:
        manifest = manifest[:limit]
    if OUT.exists():
        shutil.rmtree(OUT)
    PAGES_DIR.mkdir(parents=True)

    httpd = start_server()
    port = httpd.server_address[1]
    printed: list[tuple[Path, str]] = []
    stats = {"mermaid_pages": 0, "mermaid_diagrams": 0, "math_pages": 0}
    t0 = time.time()

    # Local dev defaults to system Google Chrome; CI sets PDF_CHROME_CHANNEL=chromium
    # to use Playwright's bundled Chromium (installed via `playwright install chromium`).
    channel = os.environ.get("PDF_CHROME_CHANNEL", "chrome").strip().lower()
    launch_kwargs: dict[str, object] = {"headless": True}
    if channel and channel not in ("chromium", "bundled", "none", ""):
        launch_kwargs["channel"] = channel

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch_kwargs)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        for i, item in enumerate(manifest):
            url = item["url"]
            page.goto(f"http://127.0.0.1:{port}/{url}", wait_until="load", timeout=60000)

            math_state = page.evaluate(MATHJAX_JS)
            if math_state == "typeset" and page.query_selector(".arithmatex"):
                stats["math_pages"] += 1

            defs = extract_mermaid(url)
            if defs:
                page.wait_for_function("() => window.mermaid", timeout=25000)
                n = page.evaluate(MERMAID_JS, defs)
                if isinstance(n, int) and n:
                    stats["mermaid_pages"] += 1
                    stats["mermaid_diagrams"] += n

            page.add_style_tag(content=PRINT_CSS)
            if os.environ.get("FIT_WIDE_TABLES") == "1":
                page.add_style_tag(content=FIT_TABLES_CSS)
            page.wait_for_timeout(120)  # let layout/fonts settle

            out = PAGES_DIR / f"{i:03d}.pdf"
            page.pdf(
                path=str(out),
                format="A4",
                print_background=True,
                margin={"top": "1.5cm", "bottom": "1.5cm", "left": "1.4cm", "right": "1.4cm"},
            )
            printed.append((out, item["title"]))
            if (i + 1) % 10 == 0 or i == len(manifest) - 1:
                print(f"  printed {i + 1}/{len(manifest)}")
        browser.close()

    httpd.shutdown()

    # Merge into one manual, one bookmark per source page.
    writer = PdfWriter()
    for path, title in printed:
        reader = PdfReader(str(path))
        start = len(writer.pages)
        for pg in reader.pages:
            writer.add_page(pg)
        writer.add_outline_item(title, start)
    manual = OUT / "zensical-manual.pdf"
    with open(manual, "wb") as f:
        writer.write(f)

    dt = time.time() - t0
    sheets = len(writer.pages)
    print("\n=== PDF pipeline complete ===")
    print(f"time:              {dt:.1f}s ({dt / len(manifest):.2f}s/page)")
    print(f"doc pages printed: {len(printed)}")
    print(f"mermaid:           {stats['mermaid_diagrams']} diagrams across {stats['mermaid_pages']} pages")
    print(f"math typeset:      {stats['math_pages']} pages")
    print(f"per-page PDFs:     {PAGES_DIR}/ ({len(printed)} files)")
    print(f"combined manual:   {manual} ({sheets} sheets, {manual.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
