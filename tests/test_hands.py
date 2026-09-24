"""The hands behind the browser loop, and the task list the comparison runs."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from typesafe_computer_use.browser.bench import load_tasks
from typesafe_computer_use.browser.hands import PlaywrightHands

TASKS = __import__("pathlib").Path(__file__).resolve().parent.parent / "bench" / "tasks.toml"


class FakeLocator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    def click(self, timeout):
        if self.page.fail:
            raise TimeoutError("Timeout 3000ms exceeded.\nwaiting for element to be visible")
        self.page.log.append(("click", self.selector, timeout))

    def fill(self, text, timeout):
        self.page.log.append(("fill", self.selector, text))


class FakePage:
    def __init__(self, fail=False):
        self.fail, self.log = fail, []
        self.keyboard = SimpleNamespace(press=lambda key: self.log.append(("key", key)))

    def locator(self, selector):
        return FakeLocator(self, selector)


def hands(page) -> PlaywrightHands:
    h = PlaywrightHands("http://127.0.0.1:1")
    h.page = page
    return h


def test_playwright_acts_on_the_element_by_its_tag():
    page = FakePage()
    element = SimpleNamespace(name="Issues 6")
    h = hands(page)
    assert h.click(16, element, None) == "click [16] 'Issues 6' via playwright"
    assert h.type_text(3, "Singapore") == "type 'Singapore' into [3] via playwright"
    assert h.press("enter") == "press enter"
    assert page.log == [("click", '[data-tscu="16"]', 3000), ("fill", '[data-tscu="3"]', "Singapore"), ("key", "Enter")]


def test_a_click_playwright_cannot_make_is_a_failed_action_not_a_crash():
    detail = hands(FakePage(fail=True)).click(16, SimpleNamespace(name="x"), None)
    assert detail == "click [16] FAILED (Timeout 3000ms exceeded.)"


def test_the_shipped_task_list_is_valid():
    tasks = load_tasks(TASKS)
    assert len({t["name"] for t in tasks}) == len(tasks)


def test_a_task_with_a_typo_fails_before_any_browser_starts(tmp_path):
    bad = tmp_path / "tasks.toml"
    bad.write_text('[[task]]\nname = "x"\nurl = "fixture"\ngoal = "g"\nexpect = "#y"\n')
    with pytest.raises(ValueError, match="missing \\['expect_url'\\], unknown \\['expect'\\]"):
        load_tasks(bad)
