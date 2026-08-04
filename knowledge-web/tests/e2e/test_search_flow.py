"""E2E: the search page — what a reader actually does."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def test_landing_page_invites_a_search(page: Page, live_server: str):
    page.goto(live_server)
    expect(page.get_by_role("heading", name="在典籍中查找")).to_be_visible()
    expect(page.get_by_label("检索词")).to_be_focused()


def test_submitting_the_form_renders_ranked_results(page: Page, live_server: str):
    page.goto(live_server)
    page.get_by_label("检索词").fill("贼克如何取用神")
    page.get_by_role("button", name="检索").click()

    results = page.locator(".result")
    expect(results).to_have_count(3)
    expect(page.locator(".results__count")).to_contain_text("3")

    # rank numerals are sequential and rendered as display figures
    ranks = page.locator(".result__rank")
    expect(ranks.nth(0)).to_have_text("1")
    expect(ranks.nth(2)).to_have_text("3")


def test_query_hits_are_highlighted_from_backend_offsets(page: Page, live_server: str):
    page.goto(f"{live_server}/?f=1&q=贼克如何取用神")
    marks = page.locator(".result mark")
    expect(marks.first).to_be_visible()
    # the stub marks 贼克 in every snippet
    for i in range(marks.count()):
        assert marks.nth(i).inner_text() == "贼克"


def test_result_links_through_to_the_focused_chunk(page: Page, live_server: str):
    page.goto(f"{live_server}/?f=1&q=贼克如何取用神")
    page.locator(".result__title").first.click()

    expect(page).to_have_url(re.compile(r"/read/[^?]+\?focus=25#c25"))
    expect(page.locator('.chunk[data-focus="true"]')).to_have_count(1)


def test_mode_switch_survives_a_round_trip(page: Page, live_server: str):
    page.goto(f"{live_server}/?f=1&q=贼克")
    page.get_by_text("字面", exact=True).click()
    page.get_by_role("button", name="检索").click()

    expect(page).to_have_url(re.compile(r"mode=sparse"))
    expect(page.locator("#mode-sparse")).to_be_checked()


def test_unchecking_rerank_persists_through_submit(page: Page, live_server: str):
    """The hidden form marker exists precisely so this works."""
    page.goto(f"{live_server}/?f=1&q=贼克&rerank=1&rewrite=1")
    checkbox = page.get_by_role("checkbox", name="重排")
    expect(checkbox).to_be_checked()

    checkbox.uncheck()
    page.get_by_role("button", name="检索").click()

    expect(page.get_by_role("checkbox", name="重排")).not_to_be_checked()


def test_shared_link_without_the_marker_keeps_defaults_on(page: Page, live_server: str):
    page.goto(f"{live_server}/?q=贼克")
    expect(page.get_by_role("checkbox", name="重排")).to_be_checked()
    expect(page.get_by_role("checkbox", name="查询改写")).to_be_checked()


def test_empty_result_set_shows_a_real_empty_state(page: Page, live_server: str):
    page.goto(f"{live_server}/?f=1&q=nothing")
    expect(page.get_by_role("heading", name="未检索到相关段落")).to_be_visible()
    expect(page.locator(".result")).to_have_count(0)


def test_source_filter_is_populated_from_the_backend(page: Page, live_server: str):
    page.goto(live_server)
    options = page.locator("#source_id option")
    expect(options).to_have_count(3)  # 全部 + two sources
    expect(options.nth(1)).to_contain_text("guji")
