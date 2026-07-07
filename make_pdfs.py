#!/usr/bin/env python3
"""Build-to-PDF pipeline for the zensical test site — as a proper document.

Since zensical has no native PDF export, this serves the built site over HTTP
and drives headless Chrome (Playwright) to print each page to PDF, then assembles
a real document:

    [ title page ] [ table of contents ] [ table of figures ] [ numbered body ]

Along the way it handles the two things zensical's headless output gets wrong:

  * mermaid  - the theme bundle empties <pre class="mermaid"> blocks in a headless
               context without producing an <svg>, so we re-render each diagram
               from the source that survives in the on-disk HTML.
  * math     - arithmatex (generic) needs MathJax; we await typesetting.

Front matter (title/TOC/figures) is rendered as themed HTML through the SAME
Chrome pipeline, so it matches the site's typography. Continuous page numbers are
stamped onto the body sheets with reportlab. The TOC and figure page numbers are
computed exactly from per-page sheet counts.

Outputs:
  pdfs/pages/NNN.pdf        one PDF per doc page
  pdfs/zensical-manual.pdf  the assembled document (bookmarked + page-numbered)

Several document conventions are ported from the maintained mkdocs-to-pdf plugin
(github.com/domWalters/mkdocs-to-pdf, a WeasyPrint-based fork of mkdocs-with-pdf),
adapted from its CSS-Paged-Media approach to our headless-Chrome + pypdf pipeline:
hierarchical heading numbers (its _inject_heading_order), a numbered multi-level
table of contents (make_indexes), a running chapter name + "page / total" footer
(_paging.scss @page margin boxes), and page-break hygiene so figures / code /
admonitions don't split across a page (_paging.scss @media print).

Env:
  PDF_CHROME_CHANNEL=chromium   use Playwright's bundled Chromium (CI); default: system Chrome
  FIT_WIDE_TABLES=1             shrink+wrap wide tables so they don't clip
  PDF_LIMIT=N                   only process the first N pages (smoke test)
  TOC_DEPTH=2|3                 2 (default) = section + page; 3 also lists every h2
"""
from __future__ import annotations

import html
import http.server
import io
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
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
OUT = ROOT / "pdfs"
PAGES_DIR = OUT / "pages"
MANIFEST = ROOT / "docs" / "manifest.json"

DOC_TITLE = "Zensical PDF Stress Test"
DOC_SUBTITLE = "A synthetic ~100-page documentation site, rendered end-to-end as a PDF"

# Strip navigation chrome so each printed page is just the article, full width.
# The page-break rules are ported from mkdocs-to-pdf's _paging.scss @media print
# block: keep headings with their following content, and don't split figures /
# code / admonitions / tabs across a page boundary. `.pdf-order` styles the
# injected hierarchical heading numbers (ported from its _heading.scss).
PRINT_CSS = """
.md-header, .md-sidebar, .md-tabs, .md-footer, .md-content__button,
.md-clipboard, .md-top, .md-source, [data-md-component=announce], .md-nav { display:none !important; }
.md-typeset button, .highlight button, .md-typeset pre button { display:none !important; }
.md-main__inner, .md-content { margin:0 !important; max-width:none !important; }
.md-content__inner { padding-top:0 !important; }
.md-grid { max-width:none !important; }
.md-typeset h1, .md-typeset h2, .md-typeset h3, .md-typeset h4 { break-after: avoid; }
.md-typeset img, .md-typeset .mermaid, .md-typeset figure, .md-typeset pre,
.md-typeset .admonition, .md-typeset details, .md-typeset .tabbed-set,
.md-typeset blockquote { break-inside: avoid; }
.md-typeset .pdf-order { color:#4f46e5; font-weight:600; }
"""

# Wide tables (5-6 columns) are wider than an A4 page, so their last column is
# clipped at the page edge (on screen Material scrolls them; print can't).
FIT_TABLES_CSS = """
.md-typeset__scrollwrap, .md-typeset__table { overflow: visible !important; }
.md-typeset table:not([class]) { width:100% !important; font-size:0.62rem !important;
    table-layout: fixed !important; }
.md-typeset table:not([class]) th, .md-typeset table:not([class]) td {
    white-space: normal !important; word-break: break-word !important; }
"""

MERMAID_RE = re.compile(r'<pre class="mermaid"><code[^>]*>(.*?)</code></pre>', re.S)

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

# Figures = content images (assets/*.svg) + rendered mermaid diagrams, in DOM order.
# Query the kinds/alts of the figures on the page (DOM order).
FIGURE_KINDS_JS = r"""() => {
    const a = document.querySelector('.md-content__inner') || document.body;
    const nodes = [...a.querySelectorAll('img, .mermaid')].filter(n =>
        n.tagName !== 'IMG' || /assets\//.test(n.getAttribute('src')||''));
    return nodes.map(n => ({ kind: n.tagName === 'IMG' ? 'image' : 'mermaid',
                             alt: n.tagName === 'IMG' ? (n.getAttribute('alt')||'') : '' }));
}"""

# Inject "Figure N. caption" captions under each figure (DOM order). For images the
# markdown already renders a "*Figure: ...*" line, so we replace that instead of
# doubling up; mermaid diagrams get a fresh caption inserted.
FIGURE_LABEL_JS = r"""(labels) => {
    const a = document.querySelector('.md-content__inner') || document.body;
    const nodes = [...a.querySelectorAll('img, .mermaid')].filter(n =>
        n.tagName !== 'IMG' || /assets\//.test(n.getAttribute('src')||''));
    const style = 'font-size:.78rem;color:#5b6270;font-style:italic;text-align:center;margin:.35rem 0 1.1rem';
    nodes.forEach((n, i) => {
        if (i >= labels.length) return;
        let existing = null;
        if (n.tagName === 'IMG') {
            const host = n.closest('p') || n;
            const sib = host.nextElementSibling;
            if (sib && /^(P|EM)$/.test(sib.tagName) && /figure/i.test(sib.textContent)) existing = sib;
        }
        if (existing) {
            existing.textContent = labels[i];
            existing.style.cssText = style;
        } else {
            const cap = document.createElement('p');
            cap.textContent = labels[i];
            cap.style.cssText = style;
            const host = (n.tagName === 'IMG') ? (n.closest('p') || n) : n;
            host.insertAdjacentElement('afterend', cap);
        }
    });
}"""

# After labelling, measure each figure's fractional position through the article.
FIGURE_FRACS_JS = r"""() => {
    const a = document.querySelector('.md-content__inner') || document.body;
    const at = a.getBoundingClientRect().top + window.scrollY;
    const ah = a.scrollHeight || 1;
    const nodes = [...a.querySelectorAll('img, .mermaid')].filter(n =>
        n.tagName !== 'IMG' || /assets\//.test(n.getAttribute('src')||''));
    return nodes.map(n => Math.max(0, Math.min(1,
        ((n.getBoundingClientRect().top + window.scrollY) - at) / ah)));
}"""

# Hierarchical heading numbers (ported from mkdocs-to-pdf's _inject_heading_order):
# the page's h1 gets `base` (e.g. "2.3"), h2s get base.N, h3s get base.N.M.
# Injects a <span class="pdf-order"> prefix and returns the headings (text
# captured before injection) so the TOC can descend into them.
NUMBER_HEADINGS_JS = r"""({base, h1Only}) => {
    const a = document.querySelector('.md-content__inner') || document.body;
    const hs = [...a.querySelectorAll(h1Only ? 'h1' : 'h1, h2, h3')];
    let c2 = 0, c3 = 0;
    const out = [];
    for (const h of hs) {
        let num;
        if (h.tagName === 'H1') { num = base; c2 = 0; c3 = 0; }
        else if (h.tagName === 'H2') { c2++; c3 = 0; num = base + '.' + c2; }
        else { c3++; num = base + '.' + c2 + '.' + c3; }
        const text = h.textContent.trim();
        const span = document.createElement('span');
        span.className = 'pdf-order';
        span.textContent = num + ' ';
        h.insertBefore(span, h.firstChild);
        out.push({ tag: h.tagName.toLowerCase(), number: num, text: text });
    }
    return out;
}"""

# Measure each h1/h2/h3's fractional position (same technique as figures) so the
# multi-level TOC can compute an accurate page number for every heading.
HEADING_FRACS_JS = r"""() => {
    const a = document.querySelector('.md-content__inner') || document.body;
    const at = a.getBoundingClientRect().top + window.scrollY;
    const ah = a.scrollHeight || 1;
    return [...a.querySelectorAll('h1, h2, h3')].map(n => Math.max(0, Math.min(1,
        ((n.getBoundingClientRect().top + window.scrollY) - at) / ah)));
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


def mermaid_caption(src: str) -> str:
    line = (src.strip().splitlines() or [""])[0].strip()
    low = line.lower()
    if low.startswith("pie"):
        title = line.split("title", 1)[1].strip().strip('"') if "title" in low else "Pie chart"
        return f"{title} (pie chart)"
    for key, label in (("sequencediagram", "Sequence diagram"), ("classdiagram", "Class diagram"),
                       ("statediagram", "State diagram"), ("erdiagram", "ER diagram"),
                       ("gantt", "Gantt chart"), ("flowchart", "Flowchart"), ("graph", "Flowchart")):
        if low.startswith(key):
            return label
    return "Diagram"


def strip_section_prefix(title: str, section: str | None) -> str:
    """TOC/figure sub-entries are nested under their section, so drop a leading
    "<Section> — " prefix (Getting Started — Topic 3  ->  Topic 3)."""
    if section:
        for sep in (" — ", " – ", " - ", ": "):
            pre = section + sep
            if title.startswith(pre):
                return title[len(pre):]
    return title


def theme_head() -> str:
    """Reuse the built site's stylesheet/font tags so front matter matches."""
    raw = (SITE / "index.html").read_text()
    head = re.search(r"<head[^>]*>(.*?)</head>", raw, re.S)
    if not head:
        return ""
    inner = head.group(1)
    tags = re.findall(r"<link\b[^>]*>", inner) + re.findall(r"<style\b[^>]*>.*?</style>", inner, re.S)
    return "\n".join(tags)


FRONT_CSS = """
body { margin:0; }
.page { padding: 2.4cm 2.2cm; box-sizing:border-box; }
.title-page { min-height: 100vh; display:flex; flex-direction:column;
    justify-content:center; align-items:center; text-align:center; }
.title-page .kicker { text-transform:uppercase; letter-spacing:.18em; font-size:.72rem;
    color:#4f46e5; font-weight:700; margin-bottom:1.2rem; }
.title-page h1 { font-size:2.7rem; line-height:1.15; margin:.1em 0 .3em; }
.title-page .subtitle { font-size:1.15rem; color:#5b6270; max-width:26em; }
.title-page img { width:58%; max-width:340px; margin:2.4rem 0; }
.title-page .rule { width:64px; height:4px; background:#4f46e5; border-radius:2px; margin:1.4rem 0; }
.title-page .meta { margin-top:1.8rem; color:#6b7280; font-size:.86rem; line-height:1.7; }
h2.fm-h { font-size:1.6rem; border-bottom:2px solid #4f46e5; padding-bottom:.3rem; margin:0 0 1rem; }
.entry { display:flex; align-items:baseline; margin:.07rem 0; font-size:.92rem; line-height:1.35; }
.entry.section { font-weight:700; margin-top:.6rem; margin-bottom:.02rem; font-size:.97rem; }
.entry.section .ti { color:#111827; }
.entry.sub { padding-left:1.4rem; color:#374151; }
.entry.sub2 { padding-left:3rem; color:#6b7280; font-size:.84rem; }
.entry .ti { flex:0 1 auto; }
.entry .no { flex:0 0 auto; color:#4f46e5; font-weight:600; margin-right:.5rem;
    font-variant-numeric:tabular-nums; }
.entry.sub2 .no { color:#9ca3af; font-weight:500; }
.entry .lead { flex:1 1 auto; min-width:1.4rem; border-bottom:1.5px dotted #c7ccd4;
    margin:0 .5rem; position:relative; top:-.2rem; }
.entry .pg { flex:0 0 auto; min-width:1.6rem; text-align:right;
    font-variant-numeric:tabular-nums; color:#4b5563; }
.entry .fn { flex:0 0 3.4rem; font-weight:600; color:#4f46e5;
    font-variant-numeric:tabular-nums; }
"""


def front_page(head: str, body_html: str) -> str:
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"{head}<style>{FRONT_CSS}</style></head>"
        f'<body class="md-typeset">{body_html}</body></html>'
    )


def title_html(head: str, n_pages: int, n_figs: int, date_str: str) -> str:
    body = (
        '<div class="page title-page">'
        '<div class="kicker">Zensical · Documentation</div>'
        f"<h1>{html.escape(DOC_TITLE)}</h1>"
        '<div class="rule"></div>'
        f'<p class="subtitle">{html.escape(DOC_SUBTITLE)}</p>'
        '<img src="assets/diagram.svg" alt="">'
        '<div class="meta">'
        f"{n_pages} pages &nbsp;·&nbsp; {n_figs} figures<br>"
        f"Generated by the <strong>zenscial-sandbox</strong> CI pipeline<br>"
        f"{html.escape(date_str)}"
        "</div></div>"
    )
    return front_page(head, body)


def contents_html(head: str, entries: list[tuple[str, str, str, int]]) -> str:
    rows = ['<div class="page"><h2 class="fm-h">Table of Contents</h2>']
    for kind, number, title, pg in entries:
        cls = {"section": "section", "page": "sub", "sub2": "sub2"}.get(kind, "")
        no = f'<span class="no">{html.escape(number)}</span>' if number else ""
        rows.append(
            f'<div class="entry {cls}">{no}<span class="ti">{html.escape(title)}</span>'
            f'<span class="lead"></span><span class="pg">{pg}</span></div>'
        )
    rows.append("</div>")
    return front_page(head, "".join(rows))


def figures_html(head: str, figs: list[tuple[int, str, int]]) -> str:
    rows = ['<div class="page"><h2 class="fm-h">Table of Figures</h2>']
    if not figs:
        rows.append('<p style="color:#6b7280">No figures.</p>')
    for num, caption, pg in figs:
        rows.append(
            f'<div class="entry"><span class="fn">Fig&nbsp;{num}</span>'
            f'<span class="ti">{html.escape(caption)}</span>'
            f'<span class="lead"></span><span class="pg">{pg}</span></div>'
        )
    rows.append("</div>")
    return front_page(head, "".join(rows))


def render_html_to_pdf(page, port: int, name: str, doc_html: str) -> PdfReader:
    """Write a front-matter HTML file into the site, print it, then remove it."""
    tmp = SITE / name
    tmp.write_text(doc_html)
    try:
        page.goto(f"http://127.0.0.1:{port}/{name}", wait_until="load", timeout=60000)
        page.wait_for_timeout(150)
        data = page.pdf(
            format="A4", print_background=True,
            margin={"top": "1.5cm", "bottom": "1.5cm", "left": "1.4cm", "right": "1.4cm"},
        )
    finally:
        tmp.unlink(missing_ok=True)
    return PdfReader(io.BytesIO(data))


def page_furniture_overlay(chapters: list[str], size: tuple[float, float]) -> PdfReader:
    """One overlay sheet per body page, carrying the page furniture ported from
    mkdocs-to-pdf's _paging.scss: a running chapter name in the top-right corner
    (its `@top-right { content: string(chapter) }`) and a `page / total` footer
    centred at the bottom (its `@bottom-center counter(page)/counter(pages)`)."""
    buf = io.BytesIO()
    w, h = size
    total = len(chapters)
    c = canvas.Canvas(buf, pagesize=size)
    for i, chapter in enumerate(chapters):
        if chapter:  # running header: current chapter, top-right, muted
            c.setFont("Helvetica", 7.5)
            c.setFillGray(0.5)
            c.drawRightString(w - 40, h - 34, chapter)
        c.setFont("Helvetica", 8)  # footer: page / total, centred
        c.setFillGray(0.45)
        c.drawCentredString(w / 2, 24, f"{i + 1} / {total}")
        c.showPage()
    c.save()
    buf.seek(0)
    return PdfReader(buf)


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
    stats = {"mermaid_pages": 0, "mermaid_diagrams": 0, "math_pages": 0}
    t0 = time.time()

    channel = os.environ.get("PDF_CHROME_CHANNEL", "chrome").strip().lower()
    launch_kwargs: dict[str, object] = {"headless": True}
    if channel and channel not in ("chromium", "bundled", "none", ""):
        launch_kwargs["channel"] = channel

    # Per-doc-page records + running body page counter give exact TOC page numbers.
    records: list[dict] = []          # {title, short, number, chapter, url, page_start, sheets}
    toc_entries: list[tuple[str, str, str, int]] = []   # (kind, number, title, page)
    fig_entries: list[tuple[int, str, int]] = []
    running_page = 1
    figno = 0
    current_section: str | None = None
    chap = mno = 0                    # chapter / page-within-chapter counters
    toc_depth = int(os.environ.get("TOC_DEPTH", "2"))   # 2 = section+page; 3 = +h2

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch_kwargs)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        for i, item in enumerate(manifest):
            url, title = item["url"], item["title"]
            is_cover = url == ""
            is_section = bool(re.fullmatch(r"section-\d+/", url))
            # Hierarchical number for this page's headings: chapter "N" for a
            # section index, "N.M" for a page within it, none for the cover.
            if is_section:
                current_section = title
                chap += 1
                mno = 0
                base = str(chap)
            elif is_cover:
                base = None
            else:
                mno += 1
                base = f"{chap}.{mno}"
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

            # Hierarchical heading numbers (ported from mkdocs-to-pdf): inject the
            # "N.M[.K]" prefixes and keep the heading list for a multi-level TOC.
            # A section-index page is a chapter opener, so number only its h1
            # ("1") — numbering its "In this section" nav h2 would clash with the
            # first content page (also "1.1").
            headings = (page.evaluate(NUMBER_HEADINGS_JS, {"base": base, "h1Only": is_section})
                        if base else [])

            # Figure numbering: derive captions, inject labels, measure positions.
            kinds = page.evaluate(FIGURE_KINDS_JS)
            labels, local_figs, di = [], [], 0
            for f in kinds:
                figno += 1
                if f["kind"] == "image":
                    short = (f["alt"] or "Illustration").strip()
                else:
                    short = mermaid_caption(defs[di] if di < len(defs) else "")
                    di += 1
                labels.append(f"Figure {figno}. {short}")
                where = strip_section_prefix(title, current_section) if not is_cover else title
                local_figs.append((figno, f"{where} — {short}"))
            if labels:
                page.evaluate(FIGURE_LABEL_JS, labels)
            fracs = page.evaluate(FIGURE_FRACS_JS) if local_figs else []
            head_fracs = page.evaluate(HEADING_FRACS_JS) if headings else []

            page.wait_for_timeout(120)  # let layout/fonts settle
            out = PAGES_DIR / f"{i:03d}.pdf"
            page.pdf(
                path=str(out), format="A4", print_background=True,
                margin={"top": "1.5cm", "bottom": "1.5cm", "left": "1.4cm", "right": "1.4cm"},
            )
            sheets = len(PdfReader(str(out)).pages)
            page_start = running_page

            # TOC entry: cover -> top level; section index -> section; else -> page,
            # then descend into that page's h2 headings (ported multi-level TOC).
            short = title if (is_cover or is_section) else strip_section_prefix(title, current_section)
            if is_cover:
                toc_entries.append(("cover", "", "Overview", page_start))
            elif is_section:
                toc_entries.append(("section", base or "", title, page_start))
            else:
                toc_entries.append(("page", base or "", short, page_start))
            if toc_depth >= 3:
                for h, hfrac in zip(headings, head_fracs):
                    if h["tag"] != "h2":
                        continue
                    h2_pg = page_start + min(sheets - 1, int(hfrac * sheets))
                    toc_entries.append(("sub2", h["number"], h["text"], h2_pg))

            # Figure entries: page = page_start + fractional position * sheets.
            for (num, caption), frac in zip(local_figs, fracs):
                fig_pg = page_start + min(sheets - 1, int(frac * sheets))
                fig_entries.append((num, caption, fig_pg))

            records.append({"title": title, "short": short, "number": base or "",
                            "chapter": "" if is_cover else (current_section or ""),
                            "url": url, "page_start": page_start, "sheets": sheets})
            running_page += sheets
            if (i + 1) % 10 == 0 or i == len(manifest) - 1:
                print(f"  printed {i + 1}/{len(manifest)}")

        body_sheet_total = running_page - 1

        # --- Front matter (themed HTML through the same pipeline) ---
        head = theme_head()
        # Date passed in via env to stay deterministic; fall back to a fixed label.
        date_str = os.environ.get("PDF_DATE", "").strip() or "Generated in CI"
        title_pdf = render_html_to_pdf(page, port, "__title.html",
                                       title_html(head, len(records), figno, date_str))
        toc_pdf = render_html_to_pdf(page, port, "__toc.html", contents_html(head, toc_entries))
        tof_pdf = render_html_to_pdf(page, port, "__tof.html", figures_html(head, fig_entries))
        browser.close()

    httpd.shutdown()

    # --- Assemble: title + contents + figures + numbered body ---
    writer = PdfWriter()

    def add_all(reader: PdfReader) -> int:
        start = len(writer.pages)
        for pg in reader.pages:
            writer.add_page(pg)
        return start

    title_at = add_all(title_pdf)
    toc_at = add_all(toc_pdf)
    tof_at = add_all(tof_pdf)
    body_at = len(writer.pages)
    for i, rec in enumerate(records):
        add_all(PdfReader(str(PAGES_DIR / f"{i:03d}.pdf")))

    # Stamp page furniture onto the body sheets (front matter stays unnumbered):
    # a running chapter name + a "page / total" footer. Build the per-sheet
    # chapter list by expanding each doc-page's chapter over its sheet span.
    chapters = [rec["chapter"] for rec in records for _ in range(rec["sheets"])]
    size = (float(writer.pages[body_at].mediabox.width), float(writer.pages[body_at].mediabox.height))
    overlay = page_furniture_overlay(chapters, size)
    for k in range(body_sheet_total):
        writer.pages[body_at + k].merge_page(overlay.pages[k])

    # Bookmarks: front matter + a nested, numbered section/page tree.
    writer.add_outline_item("Title page", title_at)
    writer.add_outline_item("Table of Contents", toc_at)
    writer.add_outline_item("Table of Figures", tof_at)
    section_parent = None
    for rec in records:
        idx = body_at + rec["page_start"] - 1
        label = f'{rec["number"]} {rec["short"]}'.strip()
        if rec["url"] == "":
            writer.add_outline_item(rec["short"], idx)
        elif re.fullmatch(r"section-\d+/", rec["url"]):
            section_parent = writer.add_outline_item(label, idx)
        else:
            writer.add_outline_item(label, idx, parent=section_parent)

    # Merged per-page PDFs repeat identical font/resource objects; de-dup ~-30%.
    try:
        writer.compress_identical_objects()
    except Exception:
        pass

    manual = OUT / "zensical-manual.pdf"
    with open(manual, "wb") as f:
        writer.write(f)

    dt = time.time() - t0
    fm = len(title_pdf.pages) + len(toc_pdf.pages) + len(tof_pdf.pages)
    print("\n=== document assembled ===")
    print(f"time:            {dt:.1f}s")
    print(f"doc pages:       {len(records)}   figures: {figno}   math pages: {stats['math_pages']}")
    print(f"mermaid:         {stats['mermaid_diagrams']} diagrams across {stats['mermaid_pages']} pages")
    print(f"front matter:    {fm} sheets (title {len(title_pdf.pages)}, "
          f"contents {len(toc_pdf.pages)}, figures {len(tof_pdf.pages)})")
    print(f"body:            {body_sheet_total} numbered sheets")
    print(f"combined manual: {manual} ({len(writer.pages)} sheets, {manual.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
