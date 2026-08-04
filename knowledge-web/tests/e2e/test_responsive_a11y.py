"""E2E: responsive behaviour, accessibility, and visual capture.

Screenshots land in tests/e2e/screenshots/ for human review; they are artefacts,
not assertions — the assertions here are the machine-checkable properties
(no horizontal overflow, reachable by keyboard, one h1, visible focus).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e

SHOTS = Path(__file__).parent / "screenshots"
BREAKPOINTS = [(320, 720), (768, 900), (1024, 900), (1440, 1000)]

SEARCH_URL = "/?f=1&q=贼克如何取用神&rerank=1&rewrite=1"


@pytest.fixture(autouse=True, scope="module")
def _shots_dir():
    SHOTS.mkdir(parents=True, exist_ok=True)


# --- responsive ---------------------------------------------------------


@pytest.mark.parametrize(("width", "height"), BREAKPOINTS)
def test_no_horizontal_overflow_at_any_breakpoint(
    page: Page, live_server: str, width: int, height: int
):
    page.set_viewport_size({"width": width, "height": height})
    for path in ("/", SEARCH_URL, "/library", "/jobs/", "/read/doc-1"):
        page.goto(live_server + path)
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 1, f"{path} overflows by {overflow}px at {width}w"


def test_rail_stacks_above_results_on_narrow_screens(page: Page, live_server: str):
    page.set_viewport_size({"width": 320, "height": 720})
    page.goto(live_server + SEARCH_URL)

    rail = page.locator(".rail").bounding_box()
    results = page.locator(".results").bounding_box()
    assert rail["y"] + rail["height"] <= results["y"] + 1, "rail should stack, not sit beside"


def test_rail_sits_beside_results_on_wide_screens(page: Page, live_server: str):
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(live_server + SEARCH_URL)

    rail = page.locator(".rail").bounding_box()
    results = page.locator(".results").bounding_box()
    assert results["x"] > rail["x"] + rail["width"] - 1, "expected a two-column layout"


def test_long_error_text_scrolls_inside_its_container(page: Page, live_server: str):
    page.set_viewport_size({"width": 320, "height": 720})
    page.goto(f"{live_server}/jobs/")
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    assert overflow <= 1


# --- accessibility ------------------------------------------------------


def test_each_page_has_exactly_one_h1(page: Page, live_server: str):
    for path in (SEARCH_URL, "/library", "/ingest/", "/jobs/", "/read/doc-1"):
        page.goto(live_server + path)
        count = page.locator("h1").count()
        assert count == 1, f"{path} has {count} h1 elements"


def test_search_is_reachable_and_submittable_by_keyboard(page: Page, live_server: str):
    page.goto(live_server)
    expect(page.get_by_label("检索词")).to_be_focused()  # autofocus lands on the field
    page.keyboard.type("贼克如何取用神")
    page.keyboard.press("Enter")
    expect(page.locator(".result")).to_have_count(3)


def test_focus_is_visible_on_interactive_elements(page: Page, live_server: str):
    page.goto(live_server + SEARCH_URL)
    page.get_by_label("检索词").focus()
    outline = page.evaluate("""() => {
        const s = getComputedStyle(document.activeElement);
        return {w: s.outlineWidth, style: s.outlineStyle, shadow: s.boxShadow};
    }""")
    assert outline["style"] != "none" or outline["shadow"] != "none", "no visible focus indicator"


def test_nav_marks_the_current_page(page: Page, live_server: str):
    page.goto(f"{live_server}/library")
    expect(page.locator('.masthead__link[aria-current="page"]')).to_have_text("书库")


def test_theme_toggle_has_an_accessible_name(page: Page, live_server: str):
    page.goto(live_server)
    expect(page.get_by_role("button", name="切换明暗主题")).to_be_visible()


def test_rank_numerals_are_hidden_from_assistive_tech(page: Page, live_server: str):
    """They are decoration; the heading carries the real information."""
    page.goto(live_server + SEARCH_URL)
    expect(page.locator(".result__rank").first).to_have_attribute("aria-hidden", "true")


def test_reduced_motion_is_respected(page: Page, live_server: str, browser):
    context = browser.new_context(reduced_motion="reduce")
    reduced = context.new_page()
    reduced.goto(live_server + SEARCH_URL)
    duration = reduced.evaluate(
        "() => getComputedStyle(document.querySelector('.result__rank')).transitionDuration"
    )
    seconds = float(duration.rstrip("s"))
    assert seconds < 0.001, f"transition not neutralised: {duration}"
    context.close()


# --- visual capture -----------------------------------------------------


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_capture_search_page(page: Page, live_server: str, theme: str):
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(live_server + SEARCH_URL)
    page.evaluate(f"document.documentElement.setAttribute('data-theme', '{theme}')")
    page.wait_for_timeout(200)
    page.screenshot(path=str(SHOTS / f"search-{theme}.png"), full_page=True)


def test_capture_debug_drawer(page: Page, live_server: str):
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{live_server}/?f=1&q=贼克如何取用神&debug=1")
    page.get_by_role("button", name="调试").click()
    page.wait_for_timeout(400)
    page.screenshot(path=str(SHOTS / "debug-drawer.png"))


def test_capture_reader(page: Page, live_server: str):
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.goto(f"{live_server}/read/doc-1?focus=4")
    page.wait_for_timeout(200)
    page.screenshot(path=str(SHOTS / "reader.png"))


def test_capture_library_and_jobs(page: Page, live_server: str):
    page.set_viewport_size({"width": 1440, "height": 1000})
    for path, name in (("/library", "library"), ("/jobs/", "jobs"), ("/ingest/", "ingest")):
        page.goto(live_server + path)
        page.wait_for_timeout(150)
        page.screenshot(path=str(SHOTS / f"{name}.png"))


def test_capture_mobile(page: Page, live_server: str):
    page.set_viewport_size({"width": 320, "height": 720})
    page.goto(live_server + SEARCH_URL)
    page.wait_for_timeout(200)
    page.screenshot(path=str(SHOTS / "search-mobile.png"), full_page=True)


@pytest.mark.parametrize(("width", "height"), BREAKPOINTS)
def test_nav_labels_never_break_between_characters(
    page: Page, live_server: str, width: int, height: int
):
    """CJK labels wrap per glyph when squeezed, turning the nav into columns."""
    page.set_viewport_size({"width": width, "height": height})
    page.goto(live_server)

    links = page.locator(".masthead__link")
    for i in range(links.count()):
        box = links.nth(i).bounding_box()
        line_height = links.nth(i).evaluate(
            "el => parseFloat(getComputedStyle(el).lineHeight)"
            " || parseFloat(getComputedStyle(el).fontSize) * 1.75"
        )
        assert box["height"] < line_height * 1.8, (
            f"nav link {i} is {box['height']}px tall at {width}w — it wrapped"
        )


def test_masthead_stays_compact_on_mobile(page: Page, live_server: str):
    page.set_viewport_size({"width": 320, "height": 720})
    page.goto(live_server)
    height = page.locator(".masthead").bounding_box()["height"]
    assert height < 130, f"masthead is {height}px tall on mobile"
