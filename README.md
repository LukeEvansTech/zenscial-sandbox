# zenscial-sandbox

A reusable **sandbox** for experimenting with [zensical](https://zensical.org) — standalone, disposable, and deliberately outside the `shared-workflows` docs standard so anything here can be tried without touching the real rollout.

**First experiment (live now): zensical → PDF, end to end.** A ~100-page synthetic docs site that CI builds, publishes to GitHub Pages, and turns into a single PDF — proving whether a zensical site can be delivered as a PDF, and how.

## Why this exists

Zensical (as of 0.0.47) has **no native PDF export** and supports **zero plugins yet** — the plugin/module system is still being built, and `mkdocs-with-pdf` support is only a backlog request. So a PDF has to be produced as a **post-build step**: print the built HTML with headless Chrome and merge the pages. This repo proves that works, including the awkward bits (mermaid, math, wide tables).

## What the CI pipeline does (`.github/workflows/docs-pdf.yml`)

On every push to `main`:

1. **Build** the site with `zensical build` → `site/`.
2. **Generate the document** with `make_pdfs_single.py` (the **single-document render** — promoted to production 2026-07-08 after full-scale validation): serves `site/`, preps every page in headless Chromium (mermaid, heading numbers, figure labels), concatenates them all into **one** HTML document and prints it in a **single** Chrome pass, then assembles **title page → numbered table of contents → table of figures → body** with a nested PDF outline → `pdfs/zensical-manual.pdf`. Because it's one continuous render, TOC/figure page numbers are **exact** (read back from invisible markers in the text layer) and **cross-page links work inside the PDF** (238 of them at full scale). Links that leave the document (the cover's Download button) are retargeted at the published site via `PDF_SITE_URL`, and collapsible (`???`) admonitions are forced open so their content isn't lost in print. The merged file is compressed with **pikepdf** (~17 MB → ~2.8 MB) — not Ghostscript, which would strip the internal links.

   Several **document conventions are ported from the maintained [`mkdocs-to-pdf`](https://github.com/domWalters/mkdocs-to-pdf) plugin** (a WeasyPrint-based fork of `mkdocs-with-pdf`), re-implemented for our headless-Chrome + `pypdf` pipeline since those plugins can't run under zensical:
   - **hierarchical heading numbers** (`1`, `1.1`, `1.1.1`) injected into the body and the TOC — from its `_inject_heading_order`;
   - a **numbered, multi-level TOC** (`TOC_DEPTH=3` also lists every `h2`) — from its `make_indexes`;
   - a **running chapter name** in the top corner + a **`page / total` footer** (stamped with reportlab) — from its `_paging.scss` `@page` margin boxes;
   - **page-break hygiene** so headings stay with their content and figures / code / admonitions don't split across a page — from its `_paging.scss` `@media print` rules.
3. **Publish** `site/` to GitHub Pages, with the merged PDF copied in as `/manual.pdf` so the site's home page has a working **Download PDF** button.
4. **Deliver the PDF** three ways: as a **build artifact**, and attached to a **"latest" Release**.

## What renders in the PDF, and the caveats

Renders well: prose, syntax-highlighted code, admonitions, inline SVG, tabs, lists, blockquotes, tables (headers repeat across page breaks, status emoji show), plus — once wired up — MathJax equations and mermaid diagrams.

The non-obvious fixes baked into this repo:

- **Mermaid** — zensical's theme bundle empties `<pre class="mermaid">` blocks in a headless render without producing an SVG, so the pipeline re-renders each diagram from the source that survives in the on-disk HTML, using the mermaid library the page already loaded.
- **Math** — `arithmatex` (generic) needs MathJax, which zensical doesn't ship, so it's added via `extra_javascript` in `zensical.toml` (config in `docs/javascripts/mathjax.js`).
- **Wide tables** (5-6 columns) overflow A4 and clip at the right edge (print can't scroll). Run with `FIT_WIDE_TABLES=1` to shrink+wrap them to fit.
- Mermaid.js and MathJax load from a CDN, so the PDF step needs network access (fine on GitHub-hosted runners). Vendor them locally to remove that dependency.

## Run it locally

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m playwright install chromium   # or rely on system Chrome

.venv/bin/python gen_site.py            # (re)generate the ~100-page site (random content)
.venv/bin/zensical build                # -> site/
.venv/bin/python make_pdfs_single.py    # -> pdfs/zensical-manual.pdf (uses system Chrome locally)

# variants
PDF_SITE_URL=https://…/  .venv/bin/python make_pdfs_single.py  # retarget outbound links (CI sets this)
FIT_WIDE_TABLES=1        .venv/bin/python make_pdfs_single.py  # fit wide tables
PDF_LIMIT=6              .venv/bin/python make_pdfs_single.py  # quick smoke on first 6 pages
.venv/bin/python make_pdfs.py           # the per-page reference pipeline (same output path)
```

## Files

| Path | What |
|------|------|
| `zensical.toml` | site config (rich extensions, mermaid fence, MathJax) |
| `docs/` | the committed ~100-page content, assets, MathJax config, `manifest.json` (page order) |
| `gen_site.py` | regenerates `docs/` (random content — the committed copy is canonical) |
| `make_pdfs_single.py` | **the production build→PDF pipeline**: single-document render (Playwright + pypdf + reportlab + pikepdf) — see below |
| `make_pdfs.py` | the per-page **reference implementation** (prints each page, merges, Ghostscript-compresses); shared helpers live here and `make_pdfs_single.py` imports them |
| `.github/workflows/docs-pdf.yml` | the end-to-end CI pipeline (runs `make_pdfs_single.py`) |

## The single-document render (`make_pdfs_single.py`) — why it won

`make_pdfs.py` prints each page separately and merges them. `make_pdfs_single.py` instead concatenates every page into **one** HTML document and renders it in a **single** Chrome print pass — the architecture the mature tools (mkdocs-with-pdf / WeasyPrint) use. It was prototyped alongside the per-page pipeline, validated at full scale, and **promoted to production on 2026-07-08**.

What it buys, and what the measurements actually showed:

- ✅ **Exact page numbers** — derived by reading invisible `§H:1.2.3§` markers back out of the rendered text layer, instead of estimating from a heading's fractional position (the per-page approach is ±1 sheet).
- ✅ **Working cross-page links** — every doc-page becomes an in-PDF anchor, so the "In this section" lists and cross-references jump within the PDF (the per-page merge has none).
- ✅ **Faster** — one print pass (~7 s vs ~13 s at 21 pages).
- ❌ **Does *not* dodge the font bloat.** I assumed one render would embed fonts once (as WeasyPrint does); measured, **Chrome re-embeds a subset per output page even in one job**, so the raw file is still ~4 MB. Ghostscript would crush it but it **strips the internal links** — so we compress with **pikepdf** instead (object streams + flate), which keeps every link and lands at ~0.95 MB.
- ⚠️ **Cost of the merge:** each zensical page is standalone with relative asset paths, so we absolutize URLs, namespace per-page ids (so footnotes don't collide), and let MathJax typeset the *whole* combined document at once (CHTML builds its glyph stylesheet incrementally, so per-page math can't be extracted cleanly). One big render is also more memory-hungry than per-page, and loses per-page failure isolation (one broken page fails the whole render).

**Validated at full scale** (all ~100 pages, one render): **~374 sheets, ~2.8 MB, ~23 s, 238 working cross-page links, ~3.7 GB peak RAM** — smaller and faster than the per-page pipeline, at the cost of that peak memory (fine on a 7 GB CI runner) and per-page failure isolation (one broken page fails the whole render).

Production hardening applied at promotion (2026-07-08):

- **No dead localhost links** — anything still pointing at the local render server after cross-page links become anchors (e.g. the cover's Download button) is retargeted at the published site via `PDF_SITE_URL`; a warning fires if the env is unset and such links exist.
- **Collapsible (`???`) admonitions are forced open** before extraction — print can't click, so collapsed content would otherwise be silently lost.
- **Marker misses warn loudly** — a heading/figure marker not found in the rendered text layer used to silently map to page 1; now it prints a CI-visible warning.
- **Crash-safe temp file** — the combined HTML doc is written inside `site/` (which publishes to Pages), so it's removed in a `finally`, not only on success.
