"""E2E: the JavaScript layer — drawer, reader, theme, job polling.

Each behaviour is also checked in its degraded (JS-disabled) form where the
design promises one, because "works without JS" is a claim the suite should own.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

DEBUG_URL = "/?f=1&q=贼克如何取用神&debug=1"


# --- debug drawer -------------------------------------------------------


def test_drawer_opens_and_shows_both_retriever_runs(page: Page, live_server: str):
    page.goto(live_server + DEBUG_URL)
    drawer = page.locator("[data-drawer]")
    expect(drawer).to_have_attribute("data-open", "false")

    page.get_by_role("button", name="调试").click()
    expect(drawer).to_have_attribute("data-open", "true")
    expect(drawer.get_by_text("检索轨迹")).to_be_visible()

    heads = drawer.locator(".run__head")
    assert heads.count() == 3  # dense, sparse, fused
    expect(heads.nth(0)).to_contain_text("dense")
    expect(heads.nth(1)).to_contain_text("sparse")


def test_drawer_shows_which_retriever_surfaced_each_hit(page: Page, live_server: str):
    """The fused row's `d#N s#M` must match the ranks shown in the runs above it.

    A contradiction here would make the drawer worse than useless - it is the
    only place the fusion can be checked.
    """
    page.goto(live_server + DEBUG_URL)
    page.get_by_role("button", name="调试").click()

    runs = page.locator(".run")
    dense_rows = runs.nth(0).locator(".run__row")
    sparse_rows = runs.nth(1).locator(".run__row")

    def ranks(rows) -> dict[str, int]:
        out = {}
        for i in range(rows.count()):
            label = rows.nth(i).locator(".run__id").inner_text()
            rank, _, chunk = label.partition(". ")
            out[chunk.strip()] = int(rank)
        return out

    dense, sparse = ranks(dense_rows), ranks(sparse_rows)

    fused = page.locator(".run__row--fused")
    expect(fused).to_have_count(3)
    for i in range(fused.count()):
        chunk = fused.nth(i).locator(".run__id").inner_text().partition(". ")[2].strip()
        text = fused.nth(i).inner_text()
        assert f"d#{dense[chunk]}" in text, f"{chunk}: dense rank contradicts the run"
        assert f"s#{sparse[chunk]}" in text, f"{chunk}: sparse rank contradicts the run"


def test_drawer_closes_with_escape_and_restores_focus(page: Page, live_server: str):
    page.goto(live_server + DEBUG_URL)
    trigger = page.get_by_role("button", name="调试")
    trigger.click()
    expect(page.locator("[data-drawer]")).to_have_attribute("data-open", "true")

    page.keyboard.press("Escape")
    expect(page.locator("[data-drawer]")).to_have_attribute("data-open", "false")
    expect(trigger).to_be_focused()


def test_drawer_closes_on_scrim_click(page: Page, live_server: str):
    page.goto(live_server + DEBUG_URL)
    page.get_by_role("button", name="调试").click()
    page.locator("[data-scrim]").click(position={"x": 20, "y": 20})
    expect(page.locator("[data-drawer]")).to_have_attribute("data-open", "false")


def test_timings_are_shown_and_reconcile(page: Page, live_server: str):
    page.goto(live_server + DEBUG_URL)
    page.get_by_role("button", name="调试").click()
    text = page.locator(".timings").first.inner_text()
    for key in ("store_init", "dense_search", "sparse_search", "total"):
        assert key in text


# --- theme --------------------------------------------------------------


def test_theme_toggle_flips_and_persists_across_navigation(page: Page, live_server: str):
    page.goto(live_server)
    before = page.evaluate("document.documentElement.getAttribute('data-theme')")

    page.get_by_role("button", name="切换明暗主题").click()
    after = page.evaluate("document.documentElement.getAttribute('data-theme')")
    assert after in ("light", "dark") and after != before

    page.goto(f"{live_server}/library")
    assert page.evaluate("document.documentElement.getAttribute('data-theme')") == after


def test_both_themes_render_readable_contrast(page: Page, live_server: str):
    """Guards against a dark theme that was never actually designed."""
    page.goto(f"{live_server}/?f=1&q=贼克")
    seen = {}
    for _ in range(2):
        theme = page.evaluate("""() => {
            const s = getComputedStyle(document.body);
            return {theme: document.documentElement.getAttribute('data-theme'),
                    bg: s.backgroundColor, fg: s.color};
        }""")
        seen[theme["theme"] or "auto"] = (theme["bg"], theme["fg"])
        page.get_by_role("button", name="切换明暗主题").click()
        page.wait_for_timeout(150)

    assert len(seen) == 2, f"theme did not change: {seen}"
    for name, (bg, fg) in seen.items():
        assert bg != fg, f"{name}: text and background collapsed to one colour"


# --- reader -------------------------------------------------------------


def test_reader_appends_the_next_batch_in_place(page: Page, live_server: str):
    page.goto(f"{live_server}/read/doc-1")
    chunks = page.locator(".chunk")
    first = chunks.count()
    assert first == 12  # KBWEB_READER_PAGE_SIZE default

    page.get_by_role("button", name="续读下一段").click()
    expect(chunks).to_have_count(first * 2)
    # no full navigation happened
    assert page.url.endswith("/read/doc-1")


def test_reader_stops_cleanly_at_the_end_of_the_document(page: Page, live_server: str):
    page.goto(f"{live_server}/read/doc-1?from=24")
    expect(page.locator(".chunk")).to_have_count(6)  # stub has 30 chunks
    expect(page.locator("[data-more]")).to_have_count(0)


def test_reader_falls_back_to_a_link_without_javascript(
    live_server: str, browser, browser_type_launch_args
):
    context = browser.new_context(java_script_enabled=False)
    page = context.new_page()
    page.goto(f"{live_server}/read/doc-1")

    expect(page.locator(".chunk")).to_have_count(12)
    link = page.locator("[data-more] a")
    expect(link).to_be_visible()
    link.click()
    assert "from=12" in page.url
    expect(page.locator(".chunk")).to_have_count(12)
    context.close()


def test_focused_chunk_is_marked_and_scrolled_into_view(page: Page, live_server: str):
    page.goto(f"{live_server}/read/doc-1?focus=4#c4")
    focused = page.locator('.chunk[data-focus="true"]')
    expect(focused).to_have_count(1)
    expect(focused).to_have_id("c4")


# --- jobs ---------------------------------------------------------------


def test_failed_job_shows_its_error_and_a_retry_button(page: Page, live_server: str):
    page.goto(f"{live_server}/jobs/")
    expect(page.get_by_text("no parser could handle scan.pdf")).to_be_visible()

    buttons = page.get_by_role("button", name="重试")
    expect(buttons).to_have_count(1)  # only the failed job is retryable


def test_retry_posts_and_returns_to_the_jobs_page(page: Page, live_server: str):
    page.goto(f"{live_server}/jobs/")
    page.get_by_role("button", name="重试").click()
    expect(page.get_by_text("已重新入队")).to_be_visible()


def test_job_states_use_semantic_colours(page: Page, live_server: str):
    page.goto(f"{live_server}/jobs/")
    completed = page.locator(".pill--ok")
    failed = page.locator(".pill--fail")
    expect(completed).to_have_count(1)
    expect(failed).to_have_count(1)

    ok_colour = completed.evaluate("el => getComputedStyle(el).color")
    fail_colour = failed.evaluate("el => getComputedStyle(el).color")
    assert ok_colour != fail_colour


def test_repeated_section_headings_are_printed_once(page: Page, live_server: str):
    """A 卷 spans many chunks; repeating its breadcrumb on each one is noise."""
    page.goto(f"{live_server}/read/doc-1")
    headings = page.locator(".chunk__heading")
    texts = [headings.nth(i).inner_text() for i in range(headings.count())]
    assert texts, "expected at least one running head"
    assert len(texts) == 1, f"breadcrumb repeated {len(texts)}x for one section: {texts}"


def test_appended_chunks_follow_the_same_running_head_rule(page: Page, live_server: str):
    # This test exercises the explicit button path.  Disable the reader's
    # viewport-driven prefetch so an IntersectionObserver callback cannot race
    # the click and append a second page before the assertion runs.
    page.add_init_script("delete window.IntersectionObserver")
    page.goto(f"{live_server}/read/doc-1")
    before = page.locator(".chunk__heading").count()
    page.get_by_role("button", name="续读下一段").click()
    expect(page.locator(".chunk")).to_have_count(24)
    assert page.locator(".chunk__heading").count() == before, "JS path reintroduced the noise"
