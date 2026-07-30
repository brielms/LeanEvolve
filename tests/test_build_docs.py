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


def test_repository_identity_comes_from_the_build_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "example-org/example-project")
    assert build_docs.repository_slug() == "example-org/example-project"


def test_pages_url_has_a_project_config_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LEANEVOLVE_SITE_URL", "https://docs.example.invalid/site")
    assert (
        build_docs.site_url("example-org/example-project")
        == "https://docs.example.invalid/site/"
    )


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
        ("docs/ledger.html", "ledger.html"),
        ("ledger.html", "ledger.html"),
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


def test_bold_and_emphasis_render_without_leaking_markers() -> None:
    rendered = build_docs.render_markdown(
        "It is **evident**, not *proof*.\n", COMMIT
    )
    assert "<strong>evident</strong>" in rendered
    assert "<em>proof</em>" in rendered
    assert "*" not in rendered


def test_emphasis_inside_a_word_renders() -> None:
    # `tamper-*evident*` is the shape the documentation actually writes.
    rendered = build_docs.render_markdown("tamper-*evident* only\n", COMMIT)
    assert "tamper-<em>evident</em> only" in rendered


def test_asterisks_in_code_spans_are_not_emphasis() -> None:
    # A glob in a filename must survive verbatim.
    rendered = build_docs.render_markdown(
        "See `candidate_audit_*.lean` here.\n", COMMIT
    )
    assert "candidate_audit_*.lean" in rendered
    assert "<em>" not in rendered


def test_ordered_lists_render_as_ordered_lists() -> None:
    # A numbered list that degrades to a paragraph publishes a run-on sentence
    # where the source stated a sequence, so the numbering must survive.
    rendered = build_docs.render_markdown("1. first\n2. second\n", COMMIT)
    assert "<ol><li>first</li><li>second</li></ol>" in rendered
    assert "<p>1. first" not in rendered


def test_ordered_list_items_take_indented_continuations() -> None:
    rendered = build_docs.render_markdown("1. first\n   continued\n", COMMIT)
    assert "<li>first continued</li>" in rendered


def test_tables_render_with_a_header_and_scroll_container() -> None:
    rendered = build_docs.render_markdown(
        "| Code | Meaning |\n|---|---|\n| `0` | success |\n", COMMIT
    )
    assert '<div class="scroll"><table>' in rendered
    assert "<thead><tr><th>Code</th><th>Meaning</th></tr></thead>" in rendered
    assert "<tbody><tr><td><code>0</code></td><td>success</td></tr></tbody>" in rendered


def test_table_delimiter_sets_column_alignment() -> None:
    rendered = build_docs.render_markdown(
        "| L | C | R |\n|:---|:---:|---:|\n| a | b | c |\n", COMMIT
    )
    assert '<th style="text-align:center">C</th>' in rendered
    assert '<th style="text-align:right">R</th>' in rendered
    # An unmarked column inherits the stylesheet default rather than an inline style.
    assert "<th>L</th>" in rendered


def test_prose_containing_a_pipe_is_not_a_table() -> None:
    # Without a delimiter row there is no table, and a shell pipeline in prose
    # must not be swallowed into one.
    rendered = build_docs.render_markdown("Run `a | b` to filter.\n", COMMIT)
    assert "<table>" not in rendered
    assert "<p>" in rendered


def test_angle_bracket_autolinks_become_links() -> None:
    rendered = build_docs.render_markdown(
        "Documentation: <https://example.invalid/LeanEvolve/>", COMMIT
    )
    assert (
        '<a href="https://example.invalid/LeanEvolve/">'
        "https://example.invalid/LeanEvolve/</a>" in rendered
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
# One design system
# ---------------------------------------------------------------------------


def test_hand_written_pages_share_one_stylesheet() -> None:
    """Every hand-written page must carry the same design system.

    `shared_style()` reuses `architecture.html`'s stylesheet for the rendered
    Markdown pages, but a copied page keeps its own so that it still styles
    correctly when read as a file in a clone. That duplication is only safe if
    it cannot drift, so pin it here rather than trusting review to catch it.
    """
    blocks = {}
    for name in build_docs.COPIED:
        markup = (build_docs.DOCS / name).read_text(encoding="utf-8")
        match = build_docs.STYLE_PATTERN.search(markup)
        assert match is not None, f"docs/{name} has no <style> block"
        blocks[name] = match.group(0)

    reference = build_docs.shared_style()
    for name, block in blocks.items():
        assert block == reference, (
            f"docs/{name} has drifted from the shared stylesheet; "
            "copy architecture.html's <style> block verbatim"
        )


def test_every_copied_page_is_reachable_from_the_navigation() -> None:
    # A page that builds but is not linked is a page nobody finds.
    navigated = {page for page, _ in build_docs.NAV}
    for name in build_docs.COPIED:
        assert name in navigated, f"{name} is built but missing from NAV"
    for page in build_docs.RENDERED:
        assert page in navigated, f"{page} is built but missing from NAV"


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
    assert sorted(built.pages) == [
        "architecture.html",
        "index.html",
        "ledger.html",
        "workflows.html",
    ]
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
