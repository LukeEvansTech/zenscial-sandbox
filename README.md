# zenscial-sandbox

A reusable **sandbox** for experimenting with [zensical](https://zensical.org) — standalone, disposable, and deliberately outside the `shared-workflows` docs standard so anything here can be tried without touching the real rollout.

**First experiment (live now): zensical → PDF, end to end.** A ~100-page synthetic docs site that CI builds, publishes to GitHub Pages, and turns into a single PDF — proving whether a zensical site can be delivered as a PDF, and how.

**Second experiment (live now): the same site as a document *set*.** Alongside the single manual, `make_pdfs_volumes.py` renders each top-level section as its **own standalone volume document** (`vol1.pdf` … `volN.pdf`) — the reverse trip of a document transformation that split a set of per-volume PDFs into markdown. See "The per-volume render" below.

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
3. **Generate the volume documents** with `make_pdfs_volumes.py`: the same single-render recipe run once per section → `vol1.pdf` … `volN.pdf`.
4. **Publish** `site/` to GitHub Pages, with the merged PDF copied in as `/manual.pdf` (the home page's **Download PDF** button) and the volumes under `/volumes/` (a **Download this volume** button on every section index).
5. **Deliver the PDFs** as **build artifacts** and attached to a **"latest" Release** (manual + all volumes).

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

.venv/bin/python gen_site.py            # (re)generate the site (seeded — reproduces the committed content)
.venv/bin/zensical build                # -> site/
.venv/bin/python make_pdfs_single.py    # -> pdfs/zensical-manual.pdf (uses system Chrome locally)
.venv/bin/python make_pdfs_volumes.py   # -> pdfs/vol1.pdf .. volN.pdf (one document per section)

# variants
PDF_SITE_URL=https://…/  .venv/bin/python make_pdfs_single.py  # retarget outbound links (CI sets this)
FIT_WIDE_TABLES=1        .venv/bin/python make_pdfs_single.py  # fit wide tables
PDF_LIMIT=6              .venv/bin/python make_pdfs_single.py  # quick smoke on first 6 pages
PDF_VOLUME=11            .venv/bin/python make_pdfs_volumes.py # build just one volume (smoke test)
.venv/bin/python make_pdfs.py           # the per-page reference pipeline (same output path)
```

## Files

| Path | What |
|------|------|
| `zensical.toml` | site config (rich extensions, mermaid fence, MathJax) |
| `docs/` | the committed content (10 plain + 2 volume-shaped sections), assets, MathJax config, `manifest.json` (page order) |
| `gen_site.py` | regenerates `docs/` (seeded random content — regeneration reproduces the committed copy byte-for-byte) |
| `make_pdfs_single.py` | **the production build→PDF pipeline**: single-document render (Playwright + pypdf + reportlab + pikepdf) — see below |
| `make_pdfs_volumes.py` | **the per-volume pipeline**: the single-render recipe scoped to one section per document → `vol1.pdf` … `volN.pdf` — see below |
| `make_pdfs.py` | the per-page **reference implementation** (prints each page, merges, Ghostscript-compresses); shared helpers live here and the other two pipelines import them |
| `.github/workflows/docs-pdf.yml` | the end-to-end CI pipeline (runs both PDF pipelines) |

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

## The per-volume render (`make_pdfs_volumes.py`) — the site as a document set

The reverse trip of a document transformation: where a classic controlled document set ships **one self-contained volume per subject** (each with its own title page, contents, numbering from 1, and authored furniture like document control and a glossary), this pipeline produces exactly that from the same built site — one standalone PDF per top-level section, alongside (not instead of) the single manual. Design decisions:

- **One document per section**, named `vol1.pdf` … `volN.pdf`. The site cover belongs to the whole-site manual only; each volume opens with its own generated title page.
- **Standalone numbering** — inside a volume the section index is section `1`, its topic pages `2`…`N` (their `h2`s become `2.1`, `2.2`, …), and figures restart at `Figure 1`, so every volume reads like a self-contained document.
- **Pipeline does furniture, content does the rest** — the generated parts are the title page, an exact numbered TOC (topics **and** their `h2`s, read back from the same invisible markers as the single render), a table of figures, running section header + `page/total` footer, and a nested outline. Document-set furniture that carries real information (document control, version history, glossary) is **authored markdown**: sections 11+ of the fixture are generated "volume-shaped" with mock versions of those pages, so both styles can be compared from the same release (`vol1`–`vol10` plain vs `vol11`–`vol12` volume-shaped).
- **Links that leave a volume** are retargeted at the published site via `PDF_SITE_URL` (same-volume links stay working intra-PDF anchors) — a cross-volume reference in the PDF opens the web page.
- **Purely additive** — it imports the helpers from `make_pdfs.py` / `make_pdfs_single.py` and modifies neither; the single manual keeps building unchanged. Per-volume renders are ~10 pages each, so peak RAM is trivial next to the full render's ~3.7 GB, and a broken page fails one volume instead of the whole output. `PDF_VOLUME=N` builds a single volume for local iteration.

Measured at full scale (12 volumes, 2026-08-03): **~45 s total, 496 sheets across 12 documents, ~8.1 MB combined**, every TOC/figure page number exact, per-volume internal links working (e.g. 18 in `vol11.pdf`), zero marker misses.
