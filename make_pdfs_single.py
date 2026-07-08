#!/usr/bin/env python3
"""PRODUCTION — single-document render of the zensical site to one PDF.

Where make_pdfs.py prints each page separately and *merges* them (which duplicates
the font subset on every sheet — hence the ~20 MB bloat and the Ghostscript pass —
and forces us to *estimate* TOC page numbers from a heading's fractional position),
this renders ALL pages as ONE concatenated HTML document in a single Chrome print:

  * one print job              -> continuous page numbers; cross-page links resolve
  * exact page numbers         -> read back invisible "§H:1.2.3§" markers from the
                                  rendered text layer (no fractional guessing)

Note (measured, and it corrected a wrong assumption): Chrome re-embeds a font
subset per output page *even in one print job*, so a single render is still ~4 MB
raw — it does NOT dodge the bloat the way WeasyPrint would. Ghostscript would crush
it, but it strips the internal GoTo links (the whole point here), so we compress
with pikepdf instead: <1 MB with every cross-page link intact.

This is the architecture the mature tools (mkdocs-with-pdf / WeasyPrint) use.
Promoted to production 2026-07-08 after full-scale validation (372 sheets, 2.82 MB,
~28s, 238 working cross-page links). Trade-off vs the per-page pipeline: one big
render peaks at ~3.7 GB RAM (fine on a 7 GB CI runner) and loses per-page failure
isolation. The per-page make_pdfs.py is kept as the reference implementation;
iterate locally behind PDF_LIMIT.

How the merge works: each built zensical page is a standalone HTML doc with asset
paths relative to *its own* directory, so we (a) let the browser absolutize every
src/href (reading el.src/el.href), (b) namespace ids per page so footnotes don't
collide, (c) rewrite links that point at other doc pages into intra-PDF #anchors,
then concatenate the article bodies under one shared <head>.

Output: pdfs/zensical-manual.pdf
Env: same as make_pdfs.py (PDF_CHROME_CHANNEL, FIT_WIDE_TABLES, PDF_LIMIT, PDF_DATE),
plus PDF_SITE_URL — the published site's base URL; links that leave the document
(e.g. the cover's "Download PDF" button) are rewritten to it so they don't ship as
dead http://127.0.0.1:<port>/ URIs from the local render server.
"""
from __future__ import annotations

import io
import json
import os
import re
import time

import pikepdf
from playwright.sync_api import sync_playwright
from pypdf import PdfReader, PdfWriter

import make_pdfs as M


def compress_with_pikepdf(src) -> tuple[int, int]:
    """Shrink the merged PDF with pikepdf (qpdf), preserving structure.

    Chrome writes big, weakly-compressed content + font streams, so the raw file
    is ~4 MB even as one render. Ghostscript would crush it further, but it *drops
    the internal GoTo link annotations* — the whole point of a single render. So we
    use pikepdf instead: object streams + flate recompression get it to <1 MB while
    keeping every cross-page link intact. Skip with PDF_NO_GS=1.
    """
    raw = src.stat().st_size
    if os.environ.get("PDF_NO_GS") == "1":
        return raw, raw
    tmp = src.with_suffix(".pk.pdf")
    with pikepdf.open(src) as pdf:
        pdf.save(tmp, compress_streams=True, recompress_flate=True,
                 object_stream_mode=pikepdf.ObjectStreamMode.generate)
    tmp.replace(src)
    return raw, src.stat().st_size

# Extra CSS for the combined document: invisible markers, a page break before each
# page-article (but not the first), and the heading-number accent.
EXTRA_CSS = """
body { margin:0; }
.pgmark { font-size:1px; color:#fff; }
article.pp { break-before: page; }
article.pp:first-of-type { break-before: auto; }
.md-typeset .pdf-order { color:#4f46e5; font-weight:600; }
"""

# Number headings (as in make_pdfs) AND drop an invisible "§H:<num>§" marker we can
# later find in the rendered text layer to learn each heading's exact page.
NUMBER_AND_MARK_JS = r"""({base, h1Only}) => {
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
        const order = document.createElement('span');
        order.className = 'pdf-order';
        order.textContent = num + ' ';
        const mark = document.createElement('span');
        mark.className = 'pgmark';
        mark.textContent = '§H:' + num + '§';
        h.insertBefore(mark, h.firstChild);
        h.insertBefore(order, h.firstChild);
        out.push({ tag: h.tagName.toLowerCase(), number: num, text: text });
    }
    return out;
}"""

# Prepare a page for concatenation: absolutize asset URLs (so they still resolve
# from the combined doc's location), namespace every id + intra-page #href with a
# per-page prefix (so duplicate footnote ids don't collide), expand collapsible
# ("???") admonitions — print can't click, so collapsed content would be lost —
# then hand back the article's outerHTML.
REWRITE_JS = r"""(i) => {
    const abs = u => { try { return new URL(u, location.href).href; } catch (e) { return u; } };
    document.querySelectorAll('details').forEach(e => e.setAttribute('open', ''));
    document.querySelectorAll('img[src]').forEach(e => e.setAttribute('src', abs(e.getAttribute('src'))));
    document.querySelectorAll('[id]').forEach(e => { e.id = 'p' + i + '-' + e.id; });
    document.querySelectorAll('a[href]').forEach(e => {
        const h = e.getAttribute('href');
        if (!h) return;
        if (h.startsWith('#')) e.setAttribute('href', '#p' + i + '-' + h.slice(1));
        else e.setAttribute('href', abs(h));
    });
    const art = document.querySelector('.md-content__inner')
              || document.querySelector('.md-content') || document.body;
    return art.outerHTML;
}"""

A4_MARGIN = {"top": "1.5cm", "bottom": "1.5cm", "left": "1.4cm", "right": "1.4cm"}


def main() -> None:
    manifest = json.loads(M.MANIFEST.read_text())
    limit = int(os.environ.get("PDF_LIMIT", "0"))
    if limit:
        manifest = manifest[:limit]
    M.OUT.mkdir(parents=True, exist_ok=True)

    httpd = M.start_server()  # chdirs to SITE and serves
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}/"
    t0 = time.time()

    channel = os.environ.get("PDF_CHROME_CHANNEL", "chrome").strip().lower()
    launch: dict[str, object] = {"headless": True}
    if channel and channel not in ("chromium", "bundled", "none", ""):
        launch["channel"] = channel

    fragments: list[str] = []
    records: list[dict] = []
    toc_pre: list[tuple[str, str, str]] = []   # (kind, number, title) — page filled later
    fig_pre: list[tuple[int, str]] = []        # (figno, caption)      — page filled later
    stats = {"mermaid": 0, "math_pages": 0}
    chap = mno = figno = 0
    current_section: str | None = None

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch)
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        for i, item in enumerate(manifest):
            url, title = item["url"], item["title"]
            is_cover = url == ""
            is_section = bool(re.fullmatch(r"section-\d+/", url))
            if is_section:
                current_section = title
                chap += 1
                mno = 0
                basen: str | None = str(chap)
            elif is_cover:
                basen = None
            else:
                mno += 1
                basen = f"{chap}.{mno}"

            page.goto(base + url, wait_until="load", timeout=60000)
            # Leave math as raw \(...\) source (MathJax CHTML builds its glyph
            # stylesheet incrementally per page, so a per-page render can't be
            # extracted cleanly); the combined document typesets it all at once.
            if page.query_selector(".arithmatex"):
                stats["math_pages"] += 1
            defs = M.extract_mermaid(url)
            if defs:
                page.wait_for_function("() => window.mermaid", timeout=25000)
                n = page.evaluate(M.MERMAID_JS, defs)
                if isinstance(n, int):
                    stats["mermaid"] += n

            page.add_style_tag(content=M.PRINT_CSS)
            page.add_style_tag(content=EXTRA_CSS)
            if os.environ.get("FIT_WIDE_TABLES") == "1":
                page.add_style_tag(content=M.FIT_TABLES_CSS)

            headings = (page.evaluate(NUMBER_AND_MARK_JS, {"base": basen, "h1Only": is_section})
                        if basen else [])
            _ = headings  # (numbers are read back from the rendered markers)

            kinds = page.evaluate(M.FIGURE_KINDS_JS)
            labels, di = [], 0
            for f in kinds:
                figno += 1
                if f["kind"] == "image":
                    short = (f["alt"] or "Illustration").strip()
                else:
                    short = M.mermaid_caption(defs[di] if di < len(defs) else "")
                    di += 1
                labels.append(f"Figure {figno}. {short}")
                where = title if is_cover else M.strip_section_prefix(title, current_section)
                fig_pre.append((figno, f"{where} — {short}"))
            if labels:
                page.evaluate(M.FIGURE_LABEL_JS, labels)

            fragments.append(page.evaluate(REWRITE_JS, i))
            short = title if (is_cover or is_section) else M.strip_section_prefix(title, current_section)
            records.append({"i": i, "url": url, "number": basen or "", "title": title,
                            "short": short, "chapter": "" if is_cover else (current_section or ""),
                            "is_cover": is_cover, "is_section": is_section})
            if is_cover:
                toc_pre.append(("cover", "", "Overview"))
            elif is_section:
                toc_pre.append(("section", basen or "", title))
            else:
                toc_pre.append(("page", basen or "", short))
            if (i + 1) % 10 == 0 or i == len(manifest) - 1:
                print(f"  prepped {i + 1}/{len(manifest)}")

        # Rewrite links that point at another doc page into intra-PDF #anchors.
        # Anything still pointing at the local render server afterwards (e.g. the
        # cover's "Download PDF" button) leaves the document, so retarget it at the
        # published site — otherwise it ships as a dead http://127.0.0.1/ URI.
        # Only hrefs: img src must keep resolving locally for the combined render.
        site_url = os.environ.get("PDF_SITE_URL", "").strip()
        if site_url and not site_url.endswith("/"):
            site_url += "/"
        url_to_anchor = {f'"{base}{it["url"]}"': f'"#pg-{j}"' for j, it in enumerate(manifest)}

        def relink(frag: str) -> str:
            for src, dst in url_to_anchor.items():
                frag = frag.replace(src, dst)
            if site_url:
                frag = frag.replace(f'href="{base}', f'href="{site_url}')
            return frag

        articles = "".join(f'<article id="pg-{i}" class="pp">{relink(f)}</article>'
                           for i, f in enumerate(fragments))
        leftover = articles.count(f'href="{base}')
        if leftover and not site_url:
            print(f"  WARNING: {leftover} link(s) still point at the local render server "
                  "and will be dead in the PDF — set PDF_SITE_URL to retarget them")
        head = M.theme_head() + f"<style>{M.PRINT_CSS}{EXTRA_CSS}</style>"
        if os.environ.get("FIT_WIDE_TABLES") == "1":
            head += f"<style>{M.FIT_TABLES_CSS}</style>"
        # MathJax typesets the WHOLE document once (one complete glyph stylesheet).
        mj_config = (M.SITE / "javascripts" / "mathjax.js").read_text()
        mathjax = (f"<script>{mj_config}</script>"
                   '<script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js"></script>')
        body_doc = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                    f'{head}</head><body class="md-typeset">{articles}{mathjax}</body></html>')

        # Single print pass over the whole document. try/finally so a failed render
        # can't leave the temp doc behind inside site/ (which publishes to Pages).
        tmp = M.SITE / "__single_body.html"
        tmp.write_text(body_doc)
        try:
            print("  rendering combined document (single print pass)…")
            page.goto(base + "__single_body.html", wait_until="load", timeout=180000)
            page.wait_for_function("() => window.MathJax", timeout=30000)
            page.evaluate(M.MATHJAX_JS)  # await typesetting of every equation
            page.wait_for_timeout(400)
            body_bytes = page.pdf(format="A4", print_background=True, margin=A4_MARGIN)
        finally:
            tmp.unlink(missing_ok=True)

        # Exact page numbers: find each marker / figure caption in the rendered text.
        body_reader = PdfReader(io.BytesIO(body_bytes))
        page_texts = [(pg.extract_text() or "") for pg in body_reader.pages]
        total = len(page_texts)

        misses: list[str] = []

        def find_page(token: str) -> int:
            for n, txt in enumerate(page_texts):
                if token in txt:
                    return n + 1
            misses.append(token)  # fall back to 1, but say so — silent = wrong TOC
            return 1

        head_page = {r["number"]: find_page(f"§H:{r['number']}§")
                     for r in records if r["number"]}
        toc_entries = [(kind, number, title, (1 if kind == "cover" else head_page.get(number, 1)))
                       for (kind, number, title) in toc_pre]
        fig_entries = [(n, cap, find_page(f"Figure {n}.")) for (n, cap) in fig_pre]
        if misses:
            print(f"  WARNING: {len(misses)} marker(s) not found in the rendered text "
                  f"layer (TOC entries default to page 1): {misses[:8]}")

        # Front matter (rendered through the same themed pipeline as make_pdfs).
        head_fm = M.theme_head()
        date_str = os.environ.get("PDF_DATE", "").strip() or "Generated in CI"
        title_pdf = M.render_html_to_pdf(page, port, "__stitle.html",
                                         M.title_html(head_fm, len(records), figno, date_str))
        toc_pdf = M.render_html_to_pdf(page, port, "__stoc.html", M.contents_html(head_fm, toc_entries))
        tof_pdf = M.render_html_to_pdf(page, port, "__stof.html", M.figures_html(head_fm, fig_entries))
        browser.close()

    httpd.shutdown()

    # Assemble: title + contents + figures + the single-render body.
    writer = PdfWriter()

    def add(reader: PdfReader) -> int:
        start = len(writer.pages)
        for pg in reader.pages:
            writer.add_page(pg)
        return start

    title_at = add(title_pdf)
    toc_at = add(toc_pdf)
    tof_at = add(tof_pdf)
    body_at = add(body_reader)

    # Running header + page/total footer, using exact per-page chapter ranges.
    starts = sorted((1 if r["is_cover"] else head_page.get(r["number"], 1), r["chapter"])
                    for r in records)
    chapters = [""] * total
    for k, (sp, ch) in enumerate(starts):
        end = (starts[k + 1][0] - 1) if k + 1 < len(starts) else total
        for s in range(sp, end + 1):
            if 1 <= s <= total:
                chapters[s - 1] = ch
    size = (float(writer.pages[body_at].mediabox.width), float(writer.pages[body_at].mediabox.height))
    overlay = M.page_furniture_overlay(chapters, size)
    for k in range(total):
        writer.pages[body_at + k].merge_page(overlay.pages[k])

    # Bookmarks: front matter + a nested, numbered section/page tree.
    writer.add_outline_item("Title page", title_at)
    writer.add_outline_item("Table of Contents", toc_at)
    writer.add_outline_item("Table of Figures", tof_at)
    section_parent = None
    for r in records:
        pg = 1 if r["is_cover"] else head_page.get(r["number"], 1)
        idx = body_at + pg - 1
        label = f'{r["number"]} {r["short"]}'.strip()
        if r["is_cover"]:
            writer.add_outline_item(r["short"], idx)
        elif r["is_section"]:
            section_parent = writer.add_outline_item(label, idx)
        else:
            writer.add_outline_item(label, idx, parent=section_parent)

    out = M.OUT / "zensical-manual.pdf"  # production name — CI's downstream steps expect it
    with open(out, "wb") as f:
        writer.write(f)
    raw, final = compress_with_pikepdf(out)

    dt = time.time() - t0
    print("\n=== single-document render assembled ===")
    print(f"time:            {dt:.1f}s")
    print(f"doc pages:       {len(records)}   figures: {figno}   math pages: {stats['math_pages']}")
    print(f"mermaid:         {stats['mermaid']} diagrams")
    print(f"body:            {total} sheets (one continuous render)")
    if final < raw:
        print(f"compression:     {raw // 1024} KB -> {final // 1024} KB via pikepdf (links preserved)")
    print(f"single manual:   {out} ({len(writer.pages)} sheets, {final // 1024} KB)")


if __name__ == "__main__":
    main()
