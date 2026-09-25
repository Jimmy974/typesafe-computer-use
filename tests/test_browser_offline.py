"""Offline tests for the browser backend. No Chrome, no network, no API key.

A stub session replays canned JS results, so the perception parsing, the action
set filtering, change detection and the step loop are all testable in CI.
"""

from __future__ import annotations

import pytest

from typesafe_computer_use.browser import act
from typesafe_computer_use.browser.cdp import CDPError, local_debugger_url
from typesafe_computer_use.browser.decide import available_actions
from typesafe_computer_use.browser.perceive import INTERACTIVE_JS, Element, perceive


def page_dict(*, items=(), scroll_y=0, can_scroll=True, history_len=1, fields=0, below_fold=800):
    return {
        "url": "https://example.test/",
        "title": "Example",
        "vw": 1200,
        "vh": 800,
        "count": len(items),
        "items": list(items),
        "scroll_y": scroll_y,
        "scroll_max": 4000,
        "candidates": 900,
        "below_fold": below_fold,
        "can_scroll": can_scroll,
        "history_len": history_len,
        "fields": fields,
    }


def element_dict(index=0, name="Sign in", tag="a", **kwargs):
    base = {
        "index": index,
        "tag": tag,
        "role": "",
        "name": name,
        "x": 10,
        "y": 20,
        "w": 80,
        "h": 24,
        "in_view": True,
        "covered": False,
        "href": "",
    }
    base.update(kwargs)
    return base


class StubSession:
    """Replays a queue of JS results. Counts calls so tests can assert on traffic."""

    def __init__(self, results):
        self.results = list(results)
        self.evaluates = 0
        self.calls = []

    def evaluate(self, expression, **kwargs):
        assert expression is INTERACTIVE_JS or isinstance(expression, str)
        self.evaluates += 1
        return self.results.pop(0) if self.results else None

    def call(self, method, params=None):
        self.calls.append((method, params))
        return {}


# ------------------------------------------------------------------ parsing
def test_perceive_parses_page_facts():
    session = StubSession([page_dict(items=[element_dict(0), element_dict(1, "Search", "button")], fields=1)])
    page = perceive(session)
    assert [e.name for e in page.items] == ["Sign in", "Search"]
    assert page.field_count == 1 and page.has_field
    assert page.can_scroll and page.history_len == 1
    assert page.candidates == 900 and page.below_fold == 800


def test_perceive_honours_budget():
    items = [element_dict(i, f"item {i}") for i in range(50)]
    session = StubSession([page_dict(items=items)])
    assert len(perceive(session, budget=10).items) == 10


def test_element_label_carries_the_context_that_matters():
    e = Element(0, "a", "", "Docs", 5, 5, 40, 20, True, True, "https://x.test/docs")
    label = e.label()
    assert "<a>" in label and "'Docs'" in label
    assert "x.test/docs" in label
    assert "covered by an overlay" in label

    off = Element(1, "button", "", "More", 5, 900, 40, 20, False, False, "")
    assert "off-screen" in off.label()


def test_perceive_handles_active_element_absent():
    assert perceive(StubSession([None])).items == []


# ------------------------------------------------------- action availability
def test_type_text_not_offered_without_a_field():
    page = perceive(StubSession([page_dict(items=[element_dict()], fields=0)]))
    actions = available_actions(page, can_write=True)
    assert "type_text" not in actions
    assert "press_enter" not in actions
    assert "click" in actions


def test_free_text_actions_need_a_writer():
    """Typed text and an address to open only come from the writer, so without one neither
    action is offered: an action the caller cannot execute is a guaranteed stall."""
    page = perceive(StubSession([page_dict(items=[element_dict()], fields=1)]))
    without = available_actions(page, can_write=False)
    assert "type_text" not in without and "navigate" not in without
    with_writer = available_actions(page, can_write=True)
    assert "type_text" in with_writer and "navigate" in with_writer


def test_scroll_and_back_are_offered_only_when_they_can_do_something():
    flat_start = perceive(StubSession([page_dict(items=[element_dict()], can_scroll=False, history_len=1, below_fold=0)]))
    actions = available_actions(flat_start)
    assert "scroll_down" not in actions and "scroll_up" not in actions
    assert "back" not in actions

    rich = perceive(StubSession([page_dict(items=[element_dict()], can_scroll=True, history_len=4)]))
    actions = available_actions(rich)
    assert "scroll_down" in actions and "back" in actions


def test_no_click_when_there_is_nothing_to_click():
    empty = perceive(StubSession([page_dict(items=[])]))
    assert "click" not in available_actions(empty)


def test_action_terminators_always_present():
    empty = perceive(StubSession([page_dict(items=[])]))
    actions = available_actions(empty)
    for key in ("done", "none", "wait"):
        assert key in actions


def test_option_count_stays_under_the_api_limit():
    items = [element_dict(i, f"link {i}") for i in range(120)]
    page = perceive(StubSession([page_dict(items=items, fields=1)]))
    assert len(page.items) + len(available_actions(page)) < 255


# ------------------------------------------------------------ change detection
def test_fingerprint_tracks_scroll_position():
    a = perceive(StubSession([page_dict(items=[element_dict()], scroll_y=0)]))
    b = perceive(StubSession([page_dict(items=[element_dict()], scroll_y=600)]))
    assert act.fingerprint(a) != act.fingerprint(b)


def test_observe_until_changed_returns_as_soon_as_the_page_moves():
    """First poll shows the new page, so the wait costs one round trip."""
    session = StubSession([page_dict(items=[element_dict(0, "after")])])
    fps = act.fingerprint(perceive(StubSession([page_dict(items=[element_dict(0, "before")])])))
    page, ms, changed = act.observe_until_changed(session, fps, timeout_ms=200, poll_ms=1)
    assert changed and page.items[0].name == "after"
    assert session.evaluates == 1
    assert ms < 200


def test_observe_until_changed_times_out_when_nothing_moves():
    same = page_dict(items=[element_dict(0, "same")])
    session = StubSession([page_dict(items=[element_dict(0, "same")]) for _ in range(80)])
    fps = act.fingerprint(perceive(StubSession([same])))
    _, ms, changed = act.observe_until_changed(session, fps, timeout_ms=60, poll_ms=5)
    assert not changed
    assert 55 <= ms < 400


def test_observe_with_no_baseline_just_perceives():
    session = StubSession([page_dict(items=[element_dict()])])
    page, _, changed = act.observe_until_changed(session, None)
    assert changed and page.items


# ------------------------------------------------------------------ cdp
def test_the_session_only_connects_to_this_chromes_loopback_port():
    url = "ws://127.0.0.1:9222/devtools/page/ABC"
    assert local_debugger_url(url, 9222) == url
    for other in (
        "ws://127.0.0.1:9333/devtools/page/ABC",
        "ws://10.0.0.5:9222/devtools/page/ABC",
        "ws://evil.test:9222/devtools/page/ABC",
        "wss://127.0.0.1:9222/devtools/page/ABC",
    ):
        with pytest.raises(CDPError):
            local_debugger_url(other, 9222)


def test_a_page_read_that_fails_mid_navigation_is_asked_once_more(monkeypatch):
    import sys

    monkeypatch.setattr(
        sys.modules["typesafe_computer_use.browser.perceive"],
        "time",
        type("T", (), {"sleep": staticmethod(lambda _: None), "perf_counter": staticmethod(__import__("time").perf_counter)}),
    )

    class Replacing:
        def __init__(self):
            self.calls = 0

        def evaluate(self, expression, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise CDPError("JS error: Uncaught")
            return page_dict(items=[element_dict()])

    session = Replacing()
    page = perceive(session)
    assert session.calls == 2 and len(page.items) == 1


def test_the_message_scan_cannot_take_the_element_list_down_with_it():
    assert "} catch (e) { messages.length = 0; }" in INTERACTIVE_JS


def test_red_text_counts_as_a_message_even_without_a_telltale_word():
    # "New passport applications from outside Ethiopia are only available for applicants under 18."
    # carries no error word; the page shows it only in red.
    assert "!MESSAGE.test(n.nodeValue) && !reddish(el) && !inDialog" in INTERACTIVE_JS


def test_a_covered_element_is_brought_to_the_middle_before_it_is_pressed(monkeypatch):
    monkeypatch.setattr(act.time, "sleep", lambda _: None)
    rects = [
        {"x": 1220, "y": 811, "vw": 1440, "vh": 900, "hit": False},
        {"x": 1220, "y": 450, "vw": 1440, "vh": 900, "hit": True},
    ]

    class Session:
        def __init__(self):
            self.scrolled, self.pressed = 0, []

        def evaluate(self, expression, **kwargs):
            if "scrollIntoView" in expression:
                self.scrolled += 1
                return True
            return rects.pop(0)

        def call(self, method, params=None):
            self.pressed.append((params or {}).get("y"))
            return {}

    session = Session()
    element = perceive(StubSession([page_dict(items=[element_dict(name="Next", tag="button")])])).items[0]
    detail = act.click(session, 0, element, None)

    assert session.scrolled == 1
    assert set(session.pressed) == {450}
    assert "covers" not in detail


def test_an_unticked_radio_that_stays_unticked_is_pressed_once_more(monkeypatch):
    monkeypatch.setattr(act.time, "sleep", lambda _: None)
    ticks = [False, False, True]

    class Session:
        def __init__(self):
            self.presses = 0

        def evaluate(self, expression, **kwargs):
            if "el.checked" in expression:
                return ticks.pop(0)
            return {"x": 486, "y": 279, "vw": 1440, "vh": 900, "hit": True}

        def call(self, method, params=None):
            if (params or {}).get("type") == "mousePressed":
                self.presses += 1
            return {}

    session = Session()
    radio = perceive(
        StubSession([page_dict(items=[element_dict(name="Ethiopia", tag="input", role="radio", checked=False)])])
    ).items[0]
    detail = act.click(session, 0, radio, None)

    assert session.presses == 2 and detail.endswith(", pressed again")


def test_a_control_drawn_as_switched_off_is_not_offered():
    assert 'el.getAttribute("aria-disabled") === "true" || st.cursor === "not-allowed"' in INTERACTIVE_JS


def test_a_slot_or_toggle_says_whether_it_is_the_chosen_one():
    assert '["aria-pressed", "aria-selected", "aria-current"]' in INTERACTIVE_JS
