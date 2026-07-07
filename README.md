# zenscial-sandbox

A reusable **sandbox** for experimenting with [zensical](https://zensical.org) — standalone, disposable, and deliberately outside the `shared-workflows` docs standard so anything here can be tried without touching the real rollout.

**First experiment (live now): zensical → PDF, end to end.** A ~100-page synthetic docs site that CI builds, publishes to GitHub Pages, and turns into a single PDF — proving whether a zensical site can be delivered as a PDF, and how.

## Why this exists

Zensical (as of 0.0.47) has **no native PDF export** and supports **zero plugins yet** — the plugin/module system is still being built, and `mkdocs-with-pdf` support is only a backlog request. So a PDF has to be produced as a **post-build step**: print the built HTML with headless Chrome and merge the pages. This repo proves that works, including the awkward bits (mermaid, math, wide tables).

## What the CI pipeline does (`.github/workflows/docs-pdf.yml`)

On every push to `main`:

1. **Build** the site with `zensical build` → `site/`.
2. **Generate the document** with `make_pdfs.py`: serves `site/`, drives headless Chromium (Playwright) to print each page, then assembles a real document — **title page → numbered table of contents → table of figures → body** — with a nested PDF outline → `pdfs/zensical-manual.pdf` (+ per-page PDFs in `pdfs/pages/`). Front matter is rendered as themed HTML through the same pipeline; TOC/figure page numbers are computed exactly from per-page sheet counts.

   Several **document conventions are ported from the maintained [`mkdocs-to-pdf`](https://github.com/domWalters/mkdocs-to-pdf) plugin** (a WeasyPrint-based fork of `mkdocs-with-pdf`), re-implemented for our headless-Chrome + `pypdf` pipeline since those plugins can't run under zensical:
   - **hierarchical heading numbers** (`1`, `1.1`, `1.1.1`) injected into the body and the TOC — from its `_inject_heading_order`;
   - a **numbered, multi-level TOC** (`TOC_DEPTH=3` also lists every `h2`) — from its `make_indexes`;
   - a **running chapter name** in the top corner + a **`page / total` footer** (stamped with reportlab) — from its `_paging.scss` `@page` margin boxes;
   - **page-break hygiene** so headings stay with their content and figures / code / admonitions don't split across a page — from its `_paging.scss` `@media print` rules.

   Finally it **compresses the merged PDF with Ghostscript** (`/prepress`). Chrome embeds a font subset on every page, so a 370-sheet merge is ~20 MB of duplicated fonts; Ghostscript rebuilds one shared subset and re-packs the streams for a **~6× smaller file (~20 MB → ~3 MB)** with no visible quality loss — text stays vector, and mermaid/math SVG + the PDF outline are preserved. It's a no-op if `gs` isn't installed (skip with `PDF_NO_GS=1`).
3. **Publish** `site/` to GitHub Pages, with the merged PDF copied in as `/manual.pdf` so the site's home page has a working **Download PDF** button.
4. **Deliver the PDF** three ways: as a **build artifact**, and attached to a **"latest" Release**.

## What renders in the PDF, and the caveats

Renders well: prose, syntax-highlighted code, admonitions, inline SVG, tabs, lists, blockquotes, tables (headers repeat across page breaks, status emoji show), plus — once wired up — MathJax equations and mermaid diagrams.

The non-obvious fixes baked into this repo:

- **Mermaid** — zensical's theme bundle empties `<pre class="mermaid">` blocks in a headless render without producing an SVG, so `make_pdfs.py` re-renders each diagram from the source that survives in the on-disk HTML, using the mermaid library the page already loaded.
- **Math** — `arithmatex` (generic) needs MathJax, which zensical doesn't ship, so it's added via `extra_javascript` in `zensical.toml` (config in `docs/javascripts/mathjax.js`).
- **Wide tables** (5-6 columns) overflow A4 and clip at the right edge (print can't scroll). Run with `FIT_WIDE_TABLES=1` to shrink+wrap them to fit.
- Mermaid.js and MathJax load from a CDN, so the PDF step needs network access (fine on GitHub-hosted runners). Vendor them locally to remove that dependency.

## Run it locally

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m playwright install chromium   # or rely on system Chrome

.venv/bin/python gen_site.py     # (re)generate the ~100-page site (random content)
.venv/bin/zensical build         # -> site/
.venv/bin/python make_pdfs.py    # -> pdfs/  (uses system Chrome locally)

# variants
FIT_WIDE_TABLES=1 .venv/bin/python make_pdfs.py   # fit wide tables
PDF_LIMIT=6       .venv/bin/python make_pdfs.py   # quick smoke on first 6 pages
```

## Files

| Path | What |
|------|------|
| `zensical.toml` | site config (rich extensions, mermaid fence, MathJax) |
| `docs/` | the committed ~100-page content, assets, MathJax config, `manifest.json` (page order) |
| `gen_site.py` | regenerates `docs/` (random content — the committed copy is canonical) |
| `make_pdfs.py` | the production build→PDF pipeline: prints each page, merges (Playwright + pypdf + reportlab + Ghostscript) |
| `make_pdfs_single.py` | **prototype** of the alternative architecture — see below |
| `.github/workflows/docs-pdf.yml` | the end-to-end CI pipeline (runs `make_pdfs.py`) |

## Prototype: single-document render (`make_pdfs_single.py`)

`make_pdfs.py` prints each page separately and merges them. `make_pdfs_single.py` instead concatenates every page into **one** HTML document and renders it in a **single** Chrome print pass — the architecture the mature tools (mkdocs-with-pdf / WeasyPrint) use. Run it the same way (`PDF_LIMIT=21 python make_pdfs_single.py` → `pdfs/single-manual.pdf`).

What it buys, and what the measurements actually showed:

- ✅ **Exact page numbers** — derived by reading invisible `§H:1.2.3§` markers back out of the rendered text layer, instead of estimating from a heading's fractional position (the per-page approach is ±1 sheet).
- ✅ **Working cross-page links** — every doc-page becomes an in-PDF anchor, so the "In this section" lists and cross-references jump within the PDF (the per-page merge has none).
- ✅ **Faster** — one print pass (~7 s vs ~13 s at 21 pages).
- ❌ **Does *not* dodge the font bloat.** I assumed one render would embed fonts once (as WeasyPrint does); measured, **Chrome re-embeds a subset per output page even in one job**, so the raw file is still ~4 MB. Ghostscript would crush it but it **strips the internal links** — so we compress with **pikepdf** instead (object streams + flate), which keeps every link and lands at ~0.95 MB.
- ⚠️ **Cost of the merge:** each zensical page is standalone with relative asset paths, so we absolutize URLs, namespace per-page ids (so footnotes don't collide), and let MathJax typeset the *whole* combined document at once (CHTML builds its glyph stylesheet incrementally, so per-page math can't be extracted cleanly). One big render is also more memory-hungry than per-page.
