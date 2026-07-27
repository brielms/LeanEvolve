from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_builder():
    """Import the site builder, which ships as a script rather than a module."""
    spec = importlib.util.spec_from_file_location(
        "build_docs", ROOT / "scripts" / "build_docs.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_docs"] = module
    spec.loader.exec_module(module)
    return module


build_docs = _load_builder()
COMMIT = "0" * 40


# ---------------------------------------------------------------------------
# Link rewriting: the site is served under a repository prefix, so a
# repo-relative link that works in a clone must not be published unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("href", "expected"),
    [
        ("../README.md", "index.html"),
        ("README.md", "index.html"),
        ("docs/workflows.md", "workflows.html"),
        ("workflows.md", "workflows.html"),
        ("docs/architecture.html", "architecture.html"),
        ("../architecture.html", "architecture.html"),
        ("../README.md#validate", "index.html#validate"),
    ],
)
def test_site_relative_links_resolve_to_sibling_pages(href: str, expected: str) -> None:
    assert build_docs.rewrite_link(href, COMMIT) == expected


@pytest.mark.parametrize("target", ["../SECURITY.md", "CONTRIBUTING.md", "LICENSE"])
def test_repository_files_outside_the_site_become_commit_pinned_urls(
    target: str,
) -> None:
    rewritten = build_docs.rewrite_link(target, COMMIT)
    assert rewritten.startswith(f"https://github.com/{build_docs.REPO_SLUG}/blob/")
    assert COMMIT in rewritten
    assert rewritten.endswith(target.removeprefix("../"))


@pytest.mark.parametrize(
    "href",
    [
        "https://lean-lang.org/",
        "http://example.invalid/x",
        "mailto:someone@example.invalid",
        "#anchor",
    ],
)
def test_external_and_fragment_links_are_left_alone(href: str) -> None:
    assert build_docs.rewrite_link(href, COMMIT) == href


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_code_spans_are_literal_and_escaped() -> None:
    rendered = build_docs.render_markdown(
        "Use `mise run replay -- --run-dir runs/<id>` next.", COMMIT
    )
    # The angle brackets must survive as text, and the flag must not be eaten
    # by inline emphasis or link syntax.
    assert "runs/&lt;id&gt;" in rendered
    assert "--run-dir" in rendered
    assert "<strong>" not in rendered


def test_fenced_code_keeps_its_language_and_escapes_markup() -> None:
    rendered = build_docs.render_markdown(
        "```bash\nmise run docs\necho '<b>'\n```", COMMIT
    )
    assert '<pre><code class="language-bash">' in rendered
    assert "&lt;b&gt;" in rendered


def test_unterminated_code_fence_is_a_build_error() -> None:
    with pytest.raises(build_docs.BuildError):
        build_docs.render_markdown("```bash\nmise run docs\n", COMMIT)


def test_headings_lists_and_quotes_render() -> None:
    rendered = build_docs.render_markdown(
        "# Title\n\n- first\n- second\n\n> a claim\n", COMMIT
    )
    assert "<h1>Title</h1>" in rendered
    assert "<ul><li>first</li><li>second</li></ul>" in rendered
    assert "<blockquote><p>a claim</p></blockquote>" in rendered


def test_angle_bracket_autolinks_become_links() -> None:
    rendered = build_docs.render_markdown(
        "Documentation: <https://brielms.github.io/LeanEvolve/>", COMMIT
    )
    assert (
        '<a href="https://brielms.github.io/LeanEvolve/">'
        "https://brielms.github.io/LeanEvolve/</a>" in rendered
    )
    assert "&lt;https" not in rendered


def test_angle_brackets_that_are_not_autolinks_stay_escaped() -> None:
    rendered = build_docs.render_markdown("Compare <b> and <id> here.", COMMIT)
    assert "&lt;b&gt;" in rendered
    assert "&lt;id&gt;" in rendered


def test_badge_links_survive_rewriting() -> None:
    rendered = build_docs.render_markdown(
        "[![License](https://img.shields.io/badge/x)](LICENSE)", COMMIT
    )
    assert '<img src="https://img.shields.io/badge/x" alt="License">' in rendered
    assert f"/blob/{COMMIT}/LICENSE" in rendered


# ---------------------------------------------------------------------------
# The gate CI runs before deployment
# ---------------------------------------------------------------------------


# Every test builds into its own directory. The real `_site/` is the artifact
# CI uploads to Pages, and a test that mutated it -- or left a deliberately
# failed build behind -- would publish a broken site.


@pytest.fixture
def site(tmp_path: Path) -> Path:
    return tmp_path / "_site"


def test_repository_site_builds_and_every_link_resolves(site: Path) -> None:
    built = build_docs.build(site)
    assert "index.html" in built.pages, "no index.html means the site root 404s"
    assert sorted(built.pages) == ["architecture.html", "index.html", "workflows.html"]
    assert (site / ".nojekyll").is_file()
    assert build_docs.check(built) == []


def test_every_declared_page_reaches_the_built_site(site: Path) -> None:
    # The artifact is uploaded from the directory, not from the page list, so
    # each declared page must exist on disk when the build returns.
    built = build_docs.build(site)
    for name in built.pages:
        assert (site / name).is_file(), f"{name} would be missing from the artifact"


def test_building_does_not_touch_the_published_site_directory(site: Path) -> None:
    marker = build_docs.SITE / "sentinel.txt"
    build_docs.SITE.mkdir(parents=True, exist_ok=True)
    marker.write_text("kept", encoding="utf-8")
    try:
        build_docs.build(site)
        assert marker.is_file(), "a build into another directory cleared _site/"
    finally:
        marker.unlink(missing_ok=True)


def test_site_records_the_commit_it_was_built_from(site: Path) -> None:
    built = build_docs.build(site)
    markup = (site / "index.html").read_text(encoding="utf-8")
    assert f'content="{built.commit}"' in markup


def test_check_rejects_a_link_that_would_404(site: Path) -> None:
    built = build_docs.build(site)
    page = site / "index.html"
    page.write_text(
        page.read_text(encoding="utf-8").replace(
            '<a href="workflows.html">', '<a href="missing-page.html">', 1
        ),
        encoding="utf-8",
    )
    problems = build_docs.check(built)
    assert any("missing-page.html" in problem for problem in problems)


def test_check_rejects_a_published_raw_markdown_link(site: Path) -> None:
    built = build_docs.build(site)
    page = site / "index.html"
    markup = page.read_text(encoding="utf-8")
    page.write_text(
        markup.replace("<body>", '<body><a href="workflows.md">drifted</a>', 1),
        encoding="utf-8",
    )
    problems = build_docs.check(built)
    assert any("workflows.md" in problem for problem in problems)


def test_check_rejects_a_site_without_an_index(site: Path) -> None:
    built = build_docs.build(site)
    (site / "index.html").unlink()
    problems = build_docs.check(built)
    assert any("404" in problem for problem in problems)


def test_check_rejects_a_page_missing_from_the_site(site: Path) -> None:
    built = build_docs.build(site)
    (site / "architecture.html").unlink()
    problems = build_docs.check(built)
    assert any("architecture.html" in problem for problem in problems)


def test_missing_documentation_source_is_a_build_error(
    site: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(build_docs.RENDERED, "gone.html", "docs/not-a-real-file.md")
    with pytest.raises(build_docs.BuildError):
        build_docs.build(site)
