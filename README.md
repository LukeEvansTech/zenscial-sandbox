# zenscial-sandbox

A reusable **sandbox** for experimenting with [zensical](https://zensical.org) — standalone, disposable, and deliberately outside the `shared-workflows` docs standard so anything here can be tried without touching the real rollout.

**First experiment (live now): zensical → PDF, end to end.** A ~100-page synthetic docs site that CI builds, publishes to GitHub Pages, and turns into a single PDF — proving whether a zensical site can be delivered as a PDF, and how.

## Why this exists

Zensical (as of 0.0.47) has **no native PDF export** and supports **zero plugins yet** — the plugin/module system is still being built, and `mkdocs-with-pdf` support is only a backlog request. So a PDF has to be produced as a **post-build step**: print the built HTML with headless Chrome and merge the pages. This repo proves that works, including the awkward bits (mermaid, math, wide tables).

## What the CI pipeline does (`.github/workflows/docs-pdf.yml`)

On every push to `main`:

1. **Build** the site with `zensical build` → `site/`.
2. **Generate the document** with `make_pdfs.py`: serves `site/`, drives headless Chromium (Playwright) to print each page, then assembles a real document — **title page → table of contents (exact page numbers) → table of figures (numbered figures) → page-numbered body** — with a nested PDF outline → `pdfs/zensical-manual.pdf` (+ per-page PDFs in `pdfs/pages/`). Front matter is rendered as themed HTML through the same pipeline; page numbers are stamped with reportlab; TOC/figure page numbers are computed exactly from per-page sheet counts.
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
| `make_pdfs.py` | the build→PDF pipeline (Playwright + pypdf) |
| `.github/workflows/docs-pdf.yml` | the end-to-end CI pipeline |
