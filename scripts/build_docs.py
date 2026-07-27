#!/usr/bin/env python3
"""Build the published documentation site from the repository's own sources.

The site is generated, never hand-maintained: `index.html` is rendered from
`README.md` and every other page from its `docs/` source, so the public site
cannot drift from the repository it documents. GitHub Pages serves the result
under a repository prefix (`/LeanEvolve/`), so repo-relative links such as
`../README.md` are rewritten here -- to a sibling page when the target is part
of the site, and to a commit-pinned GitHub URL otherwise.

`--check` verifies that every internal link resolves inside the built site and
is the gate CI runs before deployment. Building proves that the documentation
links resolve; it proves nothing about whether the prose is true.
"""

from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SITE = ROOT / "_site"

REPO_SLUG = "brielms/LeanEvolve"
SITE_URL = "https://brielms.github.io/LeanEvolve/"

# Exit classes mirror leanevolve.workflow.errors.Exit so this script fails the
# same way as the rest of the task surface.
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_VALIDATION = 4

# Rendered from repository sources. The value is the source path relative to the
# repository root; the key is the page it becomes in the built site.
RENDERED: dict[str, str] = {
    "index.html": "README.md",
    "workflows.html": "docs/workflows.md",
}

# Copied verbatim apart from link rewriting.
COPIED: tuple[str, ...] = ("architecture.html",)

NAV: tuple[tuple[str, str], ...] = (
    ("index.html", "Overview"),
    ("workflows.html", "Workflows"),
    ("architecture.html", "Architecture"),
)


class BuildError(Exception):
    """A build input is missing or malformed."""


# --------------------------------------------------------------------------
# Repository facts
# --------------------------------------------------------------------------


def source_commit() -> str:
    """Return the commit the site is built from, for the deployment receipt."""
    for name in ("GITHUB_SHA", "LEANEVOLVE_SOURCE_COMMIT"):
        value = os.environ.get(name)
        if value:
            return value.strip()
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def blob_url(target: str, commit: str) -> str:
    """Return a commit-pinned GitHub URL for a repository path."""
    ref = commit if commit != "unknown" else "master"
    return f"https://github.com/{REPO_SLUG}/blob/{ref}/{target}"


# --------------------------------------------------------------------------
# Link rewriting
# --------------------------------------------------------------------------

EXTERNAL = ("http://", "https://", "mailto:", "//", "#")


def rewrite_link(href: str, commit: str) -> str:
    """Map a repo-relative link onto the published site.

    A link whose target is itself part of the site becomes a sibling page. Any
    other repository path becomes a commit-pinned GitHub URL, because the site
    publishes only `docs/` and the README.
    """
    if not href or href.startswith(EXTERNAL):
        return href

    anchor = ""
    if "#" in href:
        href, _, fragment = href.partition("#")
        anchor = f"#{fragment}"
        if not href:
            return anchor

    # Normalize away the leading traversal that repo-relative sources use to
    # climb out of docs/.
    target = href
    while target.startswith("../"):
        target = target[3:]
    target = target.removeprefix("./")

    # A source that becomes a page in this site.
    for page, source in RENDERED.items():
        if target in (source, Path(source).name):
            return f"{page}{anchor}"
    for page in COPIED:
        if target in (page, f"docs/{page}"):
            return f"{page}{anchor}"

    return f"{blob_url(target, commit)}{anchor}"


HREF_PATTERN = re.compile(r'(href|src)="([^"]*)"')


def rewrite_html_links(markup: str, commit: str) -> str:
    def replace(match: re.Match[str]) -> str:
        attribute, value = match.group(1), match.group(2)
        return f'{attribute}="{rewrite_link(value, commit)}"'

    return HREF_PATTERN.sub(replace, markup)


# --------------------------------------------------------------------------
# Markdown rendering
# --------------------------------------------------------------------------
#
# A deliberately small renderer for the constructs the repository's own
# Markdown actually uses: headings, fenced code, bullet lists, blockquotes,
# paragraphs, and the inline set below. It is not a general CommonMark
# implementation, and `--check` will not catch prose that uses something else.

CODE_SPAN = re.compile(r"`([^`]+)`")
AUTOLINK = re.compile(r"<((?:https?|mailto):[^>\s]+)>")
BADGE = re.compile(r"\[!\[([^\]]*)\]\(([^)]+)\)\]\(([^)]+)\)")
IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
BOLD = re.compile(r"\*\*([^*]+)\*\*")
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def render_inline(text: str, commit: str) -> str:
    """Render inline Markdown, protecting code spans from further markup."""
    placeholders: list[str] = []

    def stash(markup: str) -> str:
        placeholders.append(markup)
        return f"\x00{len(placeholders) - 1}\x00"

    def on_code(match: re.Match[str]) -> str:
        return stash(f"<code>{html.escape(match.group(1))}</code>")

    def on_autolink(match: re.Match[str]) -> str:
        url = match.group(1)
        return stash(f'<a href="{url}">{html.escape(url)}</a>')

    # Code spans first: their contents are literal, not markup. Autolinks next,
    # while their angle brackets are still distinguishable from escaped text.
    text = CODE_SPAN.sub(on_code, text)
    text = AUTOLINK.sub(on_autolink, text)
    text = html.escape(text, quote=False)

    def on_badge(match: re.Match[str]) -> str:
        alt, src, href = match.groups()
        return stash(
            f'<a href="{rewrite_link(href, commit)}">'
            f'<img src="{rewrite_link(src, commit)}" alt="{alt}"></a>'
        )

    def on_image(match: re.Match[str]) -> str:
        alt, src = match.groups()
        return stash(f'<img src="{rewrite_link(src, commit)}" alt="{alt}">')

    def on_link(match: re.Match[str]) -> str:
        label, href = match.groups()
        return stash(f'<a href="{rewrite_link(href, commit)}">{label}</a>')

    text = BADGE.sub(on_badge, text)
    text = IMAGE.sub(on_image, text)
    text = LINK.sub(on_link, text)
    text = BOLD.sub(r"<strong>\1</strong>", text)

    # Restore protected markup, including any nested inside a link label.
    for _ in range(3):
        if "\x00" not in text:
            break
        text = re.sub(
            r"\x00(\d+)\x00", lambda m: placeholders[int(m.group(1))], text
        )
    return text


def render_markdown(text: str, commit: str) -> str:
    """Render a Markdown document body to HTML."""
    out: list[str] = []
    paragraph: list[str] = []
    quote: list[str] = []
    items: list[str] = []
    mode: str | None = None

    def close() -> None:
        nonlocal mode
        if mode == "p" and paragraph:
            out.append(f"<p>{render_inline(' '.join(paragraph), commit)}</p>")
            paragraph.clear()
        elif mode == "ul" and items:
            rendered = "".join(
                f"<li>{render_inline(item, commit)}</li>" for item in items
            )
            out.append(f"<ul>{rendered}</ul>")
            items.clear()
        elif mode == "quote" and quote:
            body = render_inline(" ".join(quote), commit)
            out.append(f"<blockquote><p>{body}</p></blockquote>")
            quote.clear()
        mode = None

    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]

        if line.startswith("```"):
            close()
            language = line[3:].strip()
            block: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].startswith("```"):
                block.append(lines[index])
                index += 1
            if index >= len(lines):
                raise BuildError("unterminated fenced code block")
            attribute = f' class="language-{language}"' if language else ""
            body = html.escape("\n".join(block))
            out.append(f"<pre><code{attribute}>{body}</code></pre>")
            index += 1
            continue

        heading = HEADING.match(line)
        if heading:
            close()
            level = len(heading.group(1))
            out.append(
                f"<h{level}>{render_inline(heading.group(2), commit)}</h{level}>"
            )
            index += 1
            continue

        if not line.strip():
            close()
            index += 1
            continue

        if line.startswith("> "):
            if mode != "quote":
                close()
                mode = "quote"
            quote.append(line[2:].strip())
            index += 1
            continue

        if line.startswith("- "):
            if mode != "ul":
                close()
                mode = "ul"
            items.append(line[2:].strip())
            index += 1
            continue

        # An indented continuation belongs to the open list item or paragraph.
        if mode == "ul" and line.startswith("  ") and items:
            items[-1] += " " + line.strip()
            index += 1
            continue

        if mode != "p":
            close()
            mode = "p"
        paragraph.append(line.strip())
        index += 1

    close()
    return "\n".join(out)


# --------------------------------------------------------------------------
# Page shell
# --------------------------------------------------------------------------

STYLE_PATTERN = re.compile(r"<style>.*?</style>", re.DOTALL)
TITLE_PATTERN = re.compile(r"<title>(.*?)</title>", re.DOTALL)


def shared_style() -> str:
    """Reuse the architecture page's stylesheet so every page shares a design."""
    source = DOCS / "architecture.html"
    if not source.is_file():
        raise BuildError(f"missing {source.relative_to(ROOT)}")
    match = STYLE_PATTERN.search(source.read_text(encoding="utf-8"))
    if match is None:
        raise BuildError("architecture.html has no <style> block to share")
    return match.group(0)


def navigation(current: str) -> str:
    links = []
    for page, label in NAV:
        if page == current:
            links.append(f'<strong aria-current="page">{label}</strong>')
        else:
            links.append(f'<a href="{page}">{label}</a>')
    return '<nav class="sitenav">' + " &middot; ".join(links) + "</nav>"


NAV_STYLE = """
<style>
  .sitenav {
    margin: 0 0 2rem;
    padding-bottom: .75rem;
    border-bottom: 1px solid var(--line);
    font-size: .9rem;
  }
  .sitenav strong { color: var(--ink); font-weight: 600; }
  .buildinfo { font-size: .8rem; }
  .buildinfo code { font-size: .95em; }

  /* This documentation is mostly command lines. A monospace ligature that
     draws `--flag` as an em dash would misreport every documented command. */
  code, pre, pre code {
    font-variant-ligatures: none;
    font-feature-settings: "liga" 0, "clig" 0, "calt" 0;
  }

  blockquote {
    margin: 1.5rem 0;
    padding: .1rem 0 .1rem 1.1rem;
    border-left: 3px solid var(--accent);
  }
  blockquote p { margin: .5rem 0; color: var(--muted); }

  img { max-width: 100%; }
  ul { padding-left: 1.2rem; }
  li { margin: .3rem 0; }
</style>
"""


def page(title: str, body: str, current: str, commit: str) -> str:
    short = commit[:12] if commit != "unknown" else commit
    receipt = (
        f'<p class="buildinfo note">Built from '
        f'<a href="https://github.com/{REPO_SLUG}/commit/{commit}">'
        f"<code>{short}</code></a> of "
        f'<a href="https://github.com/{REPO_SLUG}">{REPO_SLUG}</a>.</p>'
        if commit != "unknown"
        else '<p class="buildinfo note">Built from an unrecorded commit.</p>'
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="leanevolve-source-commit" content="{commit}">
  <title>{html.escape(title)}</title>
  {shared_style()}
  {NAV_STYLE}
</head>
<body>
{navigation(current)}
{body}
<footer>
  LeanEvolve &middot; kernel-scored proof discovery &middot;
  <a href="https://github.com/{REPO_SLUG}">Repository</a> &middot;
  <a href="{blob_url('SECURITY.md', commit)}">Security</a> &middot;
  <a href="{blob_url('CONTRIBUTING.md', commit)}">Contributing</a>
  {receipt}
</footer>
</body>
</html>
"""


def page_title(source: Path, body_markdown: str) -> str:
    for line in body_markdown.splitlines():
        heading = HEADING.match(line)
        if heading:
            title = re.sub(r"[`*]", "", heading.group(2)).strip()
            if source.name == "README.md":
                return f"{title} · kernel-scored proof discovery"
            return f"LeanEvolve · {title}"
    return "LeanEvolve"


# --------------------------------------------------------------------------
# Build and check
# --------------------------------------------------------------------------


@dataclass
class Built:
    pages: list[str]
    commit: str


def build() -> Built:
    commit = source_commit()
    if SITE.exists():
        shutil.rmtree(SITE)
    SITE.mkdir(parents=True)

    # GitHub Pages runs Jekyll over an artifact unless told otherwise; this
    # site is already complete HTML.
    (SITE / ".nojekyll").write_text("", encoding="utf-8")

    pages: list[str] = []

    for target, relative in RENDERED.items():
        source = ROOT / relative
        if not source.is_file():
            raise BuildError(f"missing documentation source {relative}")
        text = source.read_text(encoding="utf-8")
        body = render_markdown(text, commit)
        markup = page(page_title(source, text), body, target, commit)
        (SITE / target).write_text(markup, encoding="utf-8")
        pages.append(target)

    for name in COPIED:
        source = DOCS / name
        if not source.is_file():
            raise BuildError(f"missing documentation source docs/{name}")
        markup = rewrite_html_links(source.read_text(encoding="utf-8"), commit)
        # Give the copied page the same navigation and build receipt.
        markup = markup.replace("<body>", f"<body>\n{navigation(name)}", 1)
        markup = markup.replace("</head>", f"{NAV_STYLE}</head>", 1)
        markup = markup.replace(
            "</head>",
            f'<meta name="leanevolve-source-commit" content="{commit}"></head>',
            1,
        )
        (SITE / name).write_text(markup, encoding="utf-8")
        pages.append(name)

    return Built(pages=sorted(pages), commit=commit)


def check(built: Built) -> list[str]:
    """Verify that every internal link resolves inside the built site."""
    problems: list[str] = []

    if not (SITE / "index.html").is_file():
        problems.append("no index.html: the site root would return 404")

    for name in built.pages:
        page_file = SITE / name
        if not page_file.is_file():
            problems.append(f"{name}: expected page is missing from the site")
            continue
        markup = page_file.read_text(encoding="utf-8")
        for attribute, value in HREF_PATTERN.findall(markup):
            if not value or value.startswith(EXTERNAL):
                continue
            target = value.split("#", 1)[0]
            if not target:
                continue
            if not (SITE / target).exists():
                problems.append(
                    f"{name}: {attribute}=\"{value}\" does not resolve in the site"
                )
            elif target.endswith(".md"):
                problems.append(
                    f"{name}: {attribute}=\"{value}\" publishes raw Markdown"
                )

    if built.commit == "unknown":
        problems.append(
            "no source commit: the deployed site could not identify its origin"
        )

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the LeanEvolve documentation site into _site/."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if any internal link does not resolve in the built site",
    )
    arguments = parser.parse_args(argv)

    try:
        built = build()
    except BuildError as error:
        print(f"documentation build failed: {error}", file=sys.stderr)
        print("recovery: fix the reported source, then rerun mise run docs")
        return EXIT_USAGE

    relative = SITE.relative_to(ROOT)
    print(f"built {len(built.pages)} pages into {relative}/ at {built.commit[:12]}")
    for name in built.pages:
        print(f"  {relative}/{name}")

    if arguments.check:
        problems = check(built)
        if problems:
            print("\ndocumentation link check failed:", file=sys.stderr)
            for problem in problems:
                print(f"  {problem}", file=sys.stderr)
            print("\nrecovery: fix the link in its source, then rerun mise run docs")
            return EXIT_VALIDATION
        print("link check: every internal link resolves")

    print(f"preview: python -m http.server --directory {relative} 8000")
    print(f"published: {SITE_URL}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
