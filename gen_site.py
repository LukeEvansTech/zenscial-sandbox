#!/usr/bin/env python3
"""Generate a ~100-page zensical test site with representative, varied content.

Layout (native zensical `new` shape):
  pdf-test/
    zensical.toml        <- config (rich extensions + mermaid fence)
    docs/
      index.md           <- cover / table of contents (home "/")
      assets/*.svg       <- local images (SVG renders crisply in print)
      section-NN/
        index.md         <- section overview
        page-MM.md       <- topic pages
    site/                <- build output (zensical build)

Every page cycles through content "archetypes" so the PDF renderer is
exercised on tables, code blocks, admonitions, mermaid diagrams, images,
math, tabs, lists, footnotes and long multi-page prose.

Writes docs/manifest.json describing page order + output URLs so the PDF
pipeline knows exactly what to print and in what order.
"""
from __future__ import annotations

import json
import random
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
ASSETS = DOCS / "assets"

SECTIONS = 10          # number of sections
PAGES_PER_SECTION = 9  # topic pages per section (+1 section index each)
# total pages = 1 cover + SECTIONS * (1 index + PAGES_PER_SECTION)

WORDS = (
    "system architecture pipeline module interface latency throughput cache "
    "deploy rollout drift baseline palette workflow renovate lint schema "
    "config template idempotent digest artifact registry token permission "
    "scope canonical fixture assertion coverage gateway threshold heuristic "
    "observability telemetry entropy manifest checksum immutable ephemeral "
    "orchestrate provision reconcile converge topology namespace boundary "
    "invariant contract propagate serialize deterministic validate render "
    "document publish annotate migrate upstream downstream throttle backoff"
).split()

SECTION_TITLES = [
    "Getting Started",
    "Core Concepts",
    "Configuration Reference",
    "Build Pipeline",
    "Theming & Palette",
    "Markdown Extensions",
    "Diagrams & Visuals",
    "Automation & CI",
    "Troubleshooting",
    "Appendix",
]


def rng(seed: int) -> random.Random:
    return random.Random(seed)


def sentence(r: random.Random, n: int | None = None) -> str:
    n = n or r.randint(8, 20)
    words = [r.choice(WORDS) for _ in range(n)]
    words[0] = words[0].capitalize()
    return " ".join(words) + r.choice([".", ".", ".", "?", ";"])


def paragraph(r: random.Random, sentences: int | None = None) -> str:
    sentences = sentences or r.randint(3, 6)
    return " ".join(sentence(r) for _ in range(sentences))


# ---------------------------------------------------------------------------
# Content blocks. Each returns a markdown string.
# ---------------------------------------------------------------------------
def block_prose(r, big=False):
    n = r.randint(3, 6) if not big else r.randint(8, 14)
    return "\n\n".join(paragraph(r) for _ in range(n))


def block_table(r):
    cols = r.choice([3, 4, 5, 6])
    rows = r.choice([4, 6, 9, 14])
    headers = ["Key", "Type", "Default", "Scope", "Status", "Notes"][:cols]
    head = "| " + " | ".join(headers) + " |"
    sep = "| " + " | ".join("---" for _ in range(cols)) + " |"
    body = []
    for i in range(rows):
        cells = []
        for c in range(cols):
            if c == 0:
                cells.append(f"`{r.choice(WORDS)}_{i}`")
            elif headers[c] == "Type":
                cells.append(r.choice(["string", "bool", "int", "list", "table"]))
            elif headers[c] == "Status":
                cells.append(r.choice(["✅ stable", "⚠️ beta", "🚧 wip"]))
            else:
                cells.append(" ".join(r.choice(WORDS) for _ in range(r.randint(1, 4))))
        body.append("| " + " | ".join(cells) + " |")
    return "\n".join([head, sep, *body])


CODE = {
    "python": '''from pathlib import Path

def check_pin(requirements: Path, expected: str) -> bool:
    """Fail drift if the zensical pin is not exact."""
    for line in requirements.read_text().splitlines():
        if line.startswith("zensical=="):
            return line.strip() == f"zensical=={expected}"
    return False
''',
    "bash": '''#!/usr/bin/env bash
set -euo pipefail
for repo in "${REPOS[@]}"; do
  gh api "repos/$OWNER/$repo/contents/docs/zensical.toml" \\
    --jq '.sha' > /dev/null && echo "ok: $repo"
done
''',
    "yaml": '''jobs:
  docs:
    permissions:
      contents: read
      pages: write
    uses: LukeEvansTech/shared-workflows/.github/workflows/zensical.yml@v1
    with:
      publish: true
      link-check: true
''',
    "json": '''{
  "extends": ["config:recommended", "helpers:pinGitHubActionDigests"],
  "packageRules": [
    { "matchManagers": ["pip_requirements"], "groupName": "python deps" }
  ]
}
''',
    "toml": '''[[project.theme.palette]]
media = "(prefers-color-scheme: dark)"
scheme = "slate"
primary = "indigo"
accent = "indigo"
''',
}


def block_code(r):
    lang = r.choice(list(CODE))
    return f"```{lang}\n{CODE[lang]}```"


def block_admonition(r):
    kind = r.choice(["note", "tip", "warning", "danger", "info", "example"])
    collapse = r.random() < 0.4
    marker = "???" if collapse else "!!!"
    title = r.choice(["Heads up", "Remember", "Gotcha", "Rationale", "Constraint"])
    body = "\n".join("    " + sentence(r) for _ in range(r.randint(2, 4)))
    return f'{marker} {kind} "{title}"\n{body}'


MERMAID = [
    """```mermaid
graph TD
    A[Caller repo] -->|uses @v1| B(zensical.yml)
    B --> C{publish?}
    C -->|true| D[Deploy Pages]
    C -->|false| E[Build only]
    D --> F[Drift check]
    E --> F
```""",
    """```mermaid
sequenceDiagram
    participant Dev
    participant CI
    participant Pages
    Dev->>CI: push to main
    CI->>CI: zensical build
    CI->>Pages: deploy artifact
    Pages-->>Dev: published URL
```""",
    """```mermaid
classDiagram
    class DriftCheck {
      +check_pin()
      +check_palette()
      +check_workflows()
    }
    class Template
    DriftCheck --> Template : validates
```""",
    """```mermaid
stateDiagram-v2
    [*] --> SoftLaunch
    SoftLaunch --> Blocking : graduate
    Blocking --> [*]
```""",
    """```mermaid
pie title Repos by mode
    "Publishing" : 12
    "Build-only" : 6
```""",
]


def block_mermaid(r):
    return r.choice(MERMAID)


def block_image(r, prefix):
    name = r.choice(["diagram", "chart", "screenshot"])
    return f"![{name} illustration]({prefix}assets/{name}.svg)\n\n*Figure: a generated {name} rendered inline.*"


def block_lists(r):
    kind = r.choice(["task", "def", "nested"])
    if kind == "task":
        return "\n".join(
            f"- [{'x' if r.random() < 0.5 else ' '}] {sentence(r, r.randint(4, 9))}"
            for _ in range(r.randint(4, 7))
        )
    if kind == "def":
        out = []
        for _ in range(r.randint(2, 4)):
            out.append(f"`{r.choice(WORDS)}`")
            out.append(f":   {sentence(r)}")
            out.append("")
        return "\n".join(out)
    # nested
    out = []
    for _ in range(r.randint(2, 4)):
        out.append(f"1. {sentence(r, 6)}")
        for _ in range(r.randint(2, 3)):
            out.append(f"    - {sentence(r, 5)}")
    return "\n".join(out)


def block_quote_footnote(r):
    q = f"> {sentence(r, 14)}\n>\n> — {r.choice(WORDS).capitalize()} {r.choice(WORDS)}"
    fn = f"\nThis claim needs a source.[^{r.randint(1, 999)}]\n"
    return q + "\n" + fn + f"\n[^{r.randint(1000, 1999)}]: {sentence(r)}"


def block_math(r):
    return (
        "The build cost scales roughly as:\n\n"
        r"$$ T(n) = \sum_{i=1}^{n} \frac{c_i}{\log(1 + d_i)} + O(n \log n) $$"
        "\n\nwhere inline $\\alpha = \\frac{p}{q}$ bounds the drift tolerance."
    )


def block_tabs(r):
    return (
        '=== "Python"\n\n    ```python\n    print("hello")\n    ```\n\n'
        '=== "Bash"\n\n    ```bash\n    echo hello\n    ```\n\n'
        '=== "TOML"\n\n    ```toml\n    key = "hello"\n    ```'
    )


BLOCKS = [
    ("prose", block_prose),
    ("table", block_table),
    ("code", block_code),
    ("admonition", block_admonition),
    ("mermaid", block_mermaid),
    ("image", None),   # needs prefix, handled inline
    ("lists", block_lists),
    ("quote", block_quote_footnote),
    ("math", block_math),
    ("tabs", block_tabs),
]


def build_page(seed: int, title: str, prefix: str, long_page: bool) -> str:
    r = rng(seed)
    parts = [f"# {title}\n", block_prose(r)]
    # Choose a rotating set of block types so coverage is even across the site.
    order = [b for b in BLOCKS]
    r.shuffle(order)
    n_blocks = r.randint(6, 9) if long_page else r.randint(3, 5)
    used = 0
    i = 0
    while used < n_blocks:
        name, fn = order[i % len(order)]
        i += 1
        heading = f"\n## {sentence(r, 3).rstrip('.?;')}\n"
        if name == "image":
            body = block_image(r, prefix)
        elif name == "prose":
            body = block_prose(r, big=long_page and r.random() < 0.5)
        else:
            body = fn(r)
        parts.append(heading)
        parts.append(body)
        used += 1
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------------------
# SVG assets (text -> crisp vector in the PDF)
# ---------------------------------------------------------------------------
def write_assets():
    ASSETS.mkdir(parents=True, exist_ok=True)
    (ASSETS / "diagram.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="520" height="200" viewBox="0 0 520 200">'
        '<rect width="520" height="200" fill="#eef2ff"/>'
        '<rect x="20" y="70" width="120" height="60" rx="8" fill="#4f46e5"/>'
        '<rect x="200" y="70" width="120" height="60" rx="8" fill="#6366f1"/>'
        '<rect x="380" y="70" width="120" height="60" rx="8" fill="#818cf8"/>'
        '<line x1="140" y1="100" x2="200" y2="100" stroke="#334155" stroke-width="3" marker-end="url(#a)"/>'
        '<line x1="320" y1="100" x2="380" y2="100" stroke="#334155" stroke-width="3" marker-end="url(#a)"/>'
        '<defs><marker id="a" markerWidth="10" markerHeight="10" refX="6" refY="3" orient="auto">'
        '<path d="M0,0 L6,3 L0,6 Z" fill="#334155"/></marker></defs>'
        '<text x="80" y="105" fill="#fff" font-family="sans-serif" font-size="14" text-anchor="middle">source</text>'
        '<text x="260" y="105" fill="#fff" font-family="sans-serif" font-size="14" text-anchor="middle">build</text>'
        '<text x="440" y="105" fill="#fff" font-family="sans-serif" font-size="14" text-anchor="middle">deploy</text>'
        '</svg>'
    )
    bars = ""
    vals = [40, 90, 60, 120, 75, 100]
    for i, v in enumerate(vals):
        x = 30 + i * 80
        bars += f'<rect x="{x}" y="{170 - v}" width="50" height="{v}" fill="#4f46e5"/>'
        bars += f'<text x="{x + 25}" y="188" font-family="sans-serif" font-size="12" text-anchor="middle" fill="#334155">Q{i + 1}</text>'
    (ASSETS / "chart.svg").write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="520" height="200" viewBox="0 0 520 200">'
        f'<rect width="520" height="200" fill="#f8fafc"/>'
        f'<line x1="20" y1="170" x2="500" y2="170" stroke="#94a3b8" stroke-width="2"/>{bars}</svg>'
    )
    (ASSETS / "screenshot.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="520" height="240" viewBox="0 0 520 240">'
        '<rect width="520" height="240" rx="10" fill="#0f172a"/>'
        '<rect x="0" y="0" width="520" height="34" rx="10" fill="#1e293b"/>'
        '<circle cx="22" cy="17" r="6" fill="#ef4444"/><circle cx="44" cy="17" r="6" fill="#f59e0b"/>'
        '<circle cx="66" cy="17" r="6" fill="#22c55e"/>'
        '<text x="20" y="80" fill="#38bdf8" font-family="monospace" font-size="14">$ zensical build</text>'
        '<text x="20" y="110" fill="#e2e8f0" font-family="monospace" font-size="14">Build started</text>'
        '<text x="20" y="140" fill="#e2e8f0" font-family="monospace" font-size="14">No issues found</text>'
        '<text x="20" y="170" fill="#22c55e" font-family="monospace" font-size="14">Build finished in 0.6s</text>'
        '</svg>'
    )


ZENSICAL_TOML = '''[project]
site_name = "Zensical PDF Stress Test"
site_description = "A 100-page synthetic site for testing HTML-to-PDF conversion."
site_author = "shared-workflows test rig"
copyright = "Copyright 2026 — test fixture, not a real product."
extra_javascript = [
    "javascripts/mathjax.js",
    "https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js",
]

[project.theme]
name = "material"
language = "en"
features = [
    "navigation.sections",
    "navigation.indexes",
    "navigation.top",
    "content.code.copy",
    "toc.follow",
]

[[project.theme.palette]]
media = "(prefers-color-scheme: light)"
scheme = "default"
primary = "indigo"
accent = "indigo"
[project.theme.palette.toggle]
icon = "material/brightness-7"
name = "Switch to dark mode"

[[project.theme.palette]]
media = "(prefers-color-scheme: dark)"
scheme = "slate"
primary = "indigo"
accent = "indigo"
[project.theme.palette.toggle]
icon = "material/brightness-4"
name = "Switch to light mode"

[project.markdown_extensions.abbr]
[project.markdown_extensions.admonition]
[project.markdown_extensions.attr_list]
[project.markdown_extensions.def_list]
[project.markdown_extensions.footnotes]
[project.markdown_extensions.md_in_html]
[project.markdown_extensions.tables]
[project.markdown_extensions.toc]
permalink = true
[project.markdown_extensions.pymdownx.arithmatex]
generic = true
[project.markdown_extensions.pymdownx.betterem]
[project.markdown_extensions.pymdownx.caret]
[project.markdown_extensions.pymdownx.details]
[project.markdown_extensions.pymdownx.highlight]
anchor_linenums = true
line_spans = "__span"
pygments_lang_class = true
[project.markdown_extensions.pymdownx.inlinehilite]
[project.markdown_extensions.pymdownx.keys]
[project.markdown_extensions.pymdownx.superfences]
custom_fences = [
  { name = "mermaid", class = "mermaid", format = "pymdownx.superfences.fence_code_format" }
]
[project.markdown_extensions.pymdownx.tabbed]
alternate_style = true
[project.markdown_extensions.pymdownx.tasklist]
custom_checkbox = true
'''


def main():
    # Clean docs (keep nothing stale) and rebuild.
    if DOCS.exists():
        shutil.rmtree(DOCS)
    DOCS.mkdir(parents=True)
    write_assets()
    # MathJax config so arithmatex (generic) math actually renders.
    js = DOCS / "javascripts"
    js.mkdir(parents=True, exist_ok=True)
    (js / "mathjax.js").write_text(
        "window.MathJax = {\n"
        '  tex: { inlineMath: [["\\\\(", "\\\\)"]], displayMath: [["\\\\[", "\\\\]"]] },\n'
        '  options: { ignoreHtmlClass: ".*", processHtmlClass: "arithmatex" }\n'
        "};\n"
    )
    (ROOT / "zensical.toml").write_text(ZENSICAL_TOML)

    manifest = []  # ordered list of {url, title, md}

    # --- Cover / home page ---
    r = rng(0)
    cover = ["# Zensical PDF Stress Test\n",
             "A synthetic documentation site with **~100 pages** of representative "
             "content, generated to test HTML→PDF conversion of a zensical build.\n",
             block_image(r, ""),
             "\n## What this exercises\n",
             "- Wide tables, fenced code (5 languages), admonitions & collapsibles\n"
             "- Mermaid diagrams (flowchart, sequence, class, state, pie)\n"
             "- Inline SVG images, math (arithmatex), content tabs, task/def lists\n"
             "- Long pages that span multiple printed sheets\n",
             "\n## Sections\n"]
    for s in range(1, SECTIONS + 1):
        cover.append(f"{s}. [{SECTION_TITLES[s - 1]}](section-{s:02d}/index.md)")
    (DOCS / "index.md").write_text("\n".join(cover) + "\n")
    manifest.append({"url": "", "title": "Zensical PDF Stress Test", "md": "index.md"})

    # --- Sections ---
    for s in range(1, SECTIONS + 1):
        sdir = DOCS / f"section-{s:02d}"
        sdir.mkdir()
        stitle = SECTION_TITLES[s - 1]
        idx = [f"# {stitle}\n",
               paragraph(rng(s * 1000)),
               "\n## In this section\n"]
        for p in range(1, PAGES_PER_SECTION + 1):
            idx.append(f"- [{stitle} — Topic {p}](page-{p:02d}.md)")
        (sdir / "index.md").write_text("\n".join(idx) + "\n")
        manifest.append({"url": f"section-{s:02d}/", "title": stitle,
                         "md": f"section-{s:02d}/index.md"})

        for p in range(1, PAGES_PER_SECTION + 1):
            seed = s * 1000 + p
            long_page = (s + p) % 4 == 0  # ~25% long pages -> multi-sheet PDFs
            title = f"{stitle} — Topic {p}"
            content = build_page(seed, title, prefix="../", long_page=long_page)
            (sdir / f"page-{p:02d}.md").write_text(content)
            manifest.append({"url": f"section-{s:02d}/page-{p:02d}/",
                             "title": title,
                             "md": f"section-{s:02d}/page-{p:02d}.md"})

    (DOCS / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"Generated {len(manifest)} pages "
          f"({SECTIONS} sections x {PAGES_PER_SECTION} + {SECTIONS} indexes + 1 cover).")
    print(f"docs dir: {DOCS}")


if __name__ == "__main__":
    main()
