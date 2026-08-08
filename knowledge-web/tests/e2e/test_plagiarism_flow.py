"""Submit, watch, and read a plagiarism report in a real browser."""

from __future__ import annotations

import pytest

from .stub_backend import _CHECK_STATE

pytestmark = pytest.mark.e2e


def _reset_state(*, stream_fail: bool = False) -> None:
    _CHECK_STATE.update(
        check_id="chk-e2e",
        done=False,
        fallback_ready=False,
        stream_fail=stream_fail,
    )


def _assert_no_horizontal_overflow(page) -> None:
    assert page.evaluate(
        "document.documentElement.scrollWidth === document.documentElement.clientWidth"
    )


def test_submit_then_read_the_report_and_responsive_layout(page, live_server):
    _reset_state()
    try:
        page.goto(f"{live_server}/plagiarism/")
        page.fill("textarea[name=text]", "夫天地者，万物之逆旅也。古人秉烛夜游。")
        page.click("button[type=submit]")

        page.wait_for_url("**/plagiarism/checks/chk-e2e")
        page.wait_for_selector(".verdict__figure", timeout=15000)
        assert "57.9%" in page.inner_text(".verdict__figure")
        assert "春夜宴从弟桃花园序" in page.content()
        assert page.locator("mark.hit").count() >= 1

        page.click(".passage__summary")
        assert "夫天地者，万物之逆旅也" in page.inner_text(".passage__body")

        page.set_viewport_size({"width": 1280, "height": 900})
        page.reload()
        page.wait_for_selector(".report-layout")
        desktop_columns = page.locator(".report-layout").evaluate(
            "el => getComputedStyle(el).gridTemplateColumns"
        )
        assert len(desktop_columns.split()) >= 2
        assert (
            page.locator(".report-layout__sources").evaluate("el => getComputedStyle(el).position")
            == "sticky"
        )
        _assert_no_horizontal_overflow(page)

        page.set_viewport_size({"width": 390, "height": 844})
        page.reload()
        page.wait_for_selector(".report-layout")
        mobile_columns = page.locator(".report-layout").evaluate(
            "el => getComputedStyle(el).gridTemplateColumns"
        )
        assert len(mobile_columns.split()) == 1
        assert (
            page.locator(".report-layout__sources").evaluate("el => getComputedStyle(el).position")
            == "static"
        )
        _assert_no_horizontal_overflow(page)
    finally:
        _reset_state()


def test_sse_failure_falls_back_to_the_report(page, live_server):
    _reset_state(stream_fail=True)
    page.add_init_script(
        """
        (() => {
          const originalSetTimeout = window.setTimeout;
          window.setTimeout = (callback, delay, ...args) =>
            originalSetTimeout(callback, delay === 15000 ? 100 : delay, ...args);
        })();
        """
    )
    try:
        page.goto(f"{live_server}/plagiarism/")
        page.fill("textarea[name=text]", "夫天地者，万物之逆旅也。古人秉烛夜游。")
        page.click("button[type=submit]")
        page.wait_for_url("**/plagiarism/checks/chk-e2e")
        page.wait_for_selector(".verdict__figure", timeout=5000)
        assert "57.9%" in page.inner_text(".verdict__figure")
    finally:
        _reset_state()
