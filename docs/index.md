# Zensical PDF Stress Test

A synthetic documentation site with **~100 pages** of representative content, generated to test HTML→PDF conversion of a zensical build.

<p><a class="md-button md-button--primary" href="manual.pdf" download>⬇ Download this whole site as a PDF</a></p>

*(The PDF is generated in CI by printing this built site with headless Chrome and merging every page — see the `Docs + PDF` workflow.)*

![chart illustration](assets/chart.svg)

*Figure: a generated chart rendered inline.*

## What this exercises

- Wide tables, fenced code (5 languages), admonitions & collapsibles
- Mermaid diagrams (flowchart, sequence, class, state, pie)
- Inline SVG images, math (arithmatex), content tabs, task/def lists
- Long pages that span multiple printed sheets


## Sections

1. [Getting Started](section-01/index.md)
2. [Core Concepts](section-02/index.md)
3. [Configuration Reference](section-03/index.md)
4. [Build Pipeline](section-04/index.md)
5. [Theming & Palette](section-05/index.md)
6. [Markdown Extensions](section-06/index.md)
7. [Diagrams & Visuals](section-07/index.md)
8. [Automation & CI](section-08/index.md)
9. [Troubleshooting](section-09/index.md)
10. [Appendix](section-10/index.md)
