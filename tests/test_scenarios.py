"""Scenarios of increasing difficulty, driven through the real `runner.run` loop.

Each test builds a page graph (the world), hands the loop a policy standing in for the classifier,
and asserts three things: the outcome the runner named, the page the world ended on, and the actions
the world received. A scenario that fails says the architecture cannot do that task; it is kept as
written and marked xfail with the reason, never weakened until it passes.
"""

from __future__ import annotations

import pytest
from world import FakeWriter, Page, World, drive, scripted

GOAL = "buy a ticket to the next show"

# The click point of row N, in screen points: the center of (100, 100+40N, 600, 130+40N) halved.
FIRST_ROW = (175.0, 57.5)
SECOND_ROW = (175.0, 77.5)


def test_l1_goal_already_achieved(monkeypatch, tmp_path):
    world = World([Page(name="confirmation", items=["Checkout complete", "Order 4821"], url="https://example.com/done")])

    state = drive(world, scripted(("done", None)), goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.answer is not None and state.answer.achieved
    assert "Order 4821" in state.answer.text
    assert state.history == []
    assert world.page.name == "confirmation"
    assert world.log == []


def test_l2_one_click_reaches_the_target(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="home", items=["Home", "Tickets", "About"], url="https://example.com/", on={"click:Tickets": "tickets"}),
            Page(name="tickets", items=["Buy", "Terms"], url="https://example.com/tickets"),
        ]
    )

    state = drive(
        world, scripted(("click_item", "Tickets"), ("done", None)), goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path
    )

    assert state.outcome == "done"
    assert state.history == ["clicked 'Tickets'"]
    assert world.page.name == "tickets"
    assert world.log == ["click:Tickets"]
    assert world.mouse == [SECOND_ROW]  # an OCR-only item has no element, so the mouse does the work


def test_l3_accessibility_controls_are_pressed_not_clicked(monkeypatch, tmp_path):
    world = World(
        [
            Page(
                name="home",
                items=["Home", ("Tickets", "link"), "About"],
                url="https://example.com/",
                on={"click:Tickets": "tickets"},
            ),
            Page(name="tickets", items=["Buy", "Terms"], url="https://example.com/tickets"),
        ]
    )

    state = drive(
        world, scripted(("click_item", "Tickets"), ("done", None)), goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path
    )

    assert state.outcome == "done"
    assert state.history == ["pressed 'Tickets' via accessibility"]
    assert world.page.name == "tickets"
    assert world.log == ["click:Tickets"]
    assert world.mouse == []  # the press went to the control itself, so no pixel was clicked
    assert world.fake.states[0]["screen_items_in_reading_order"][1]["role"] == "link"


def test_l4_a_slow_page_needs_waiting(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="home", items=["Home", "Tickets", "About"], url="https://example.com/", on={"click:Tickets": "tickets"}),
            Page(
                name="tickets",
                items=["Buy", "Terms"],
                url="https://example.com/tickets",
                loads_in=1,
                on={"click:Buy": "checkout"},
            ),
            Page(name="checkout", items=["Order summary", "Pay now"], url="https://example.com/checkout"),
        ]
    )
    policy = scripted(("click_item", "Tickets"), ("wait", None), ("click_item", "Buy"), ("done", None))

    state = drive(world, policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["clicked 'Tickets'", "waited", "clicked 'Buy'"]
    assert world.page.name == "checkout"
    assert world.log == ["click:Tickets", "wait", "click:Buy"]
    assert world.fake.states[1]["screen_items_in_reading_order"][0]["text"] == "Loading..."


def test_l5_a_dialog_is_dismissed_with_escape(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="home", items=["Home", "Tickets", "About"], url="https://example.com/", on={"click:Tickets": "cookies"}),
            Page(
                name="cookies",
                items=["Accept cookies", "Reject"],
                url="https://example.com/tickets",
                on={"escape": "tickets"},
            ),
            Page(name="tickets", items=["Buy", "Terms"], url="https://example.com/tickets", on={"click:Buy": "checkout"}),
            Page(name="checkout", items=["Order summary", "Pay now"], url="https://example.com/checkout"),
        ]
    )
    policy = scripted(("click_item", "Tickets"), ("press_escape", None), ("click_item", "Buy"), ("done", None))

    state = drive(world, policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["clicked 'Tickets'", "pressed Escape", "clicked 'Buy'"]
    assert world.page.name == "checkout"
    assert world.log == ["click:Tickets", "escape", "click:Buy"]


def test_l6_a_form_is_filled_and_submitted(monkeypatch, tmp_path):
    def submitted(world: World) -> str | None:
        """Return only takes the page to the results the field actually holds."""
        return "results" if world.typed.get("Search") == "bruno mars tour" else None

    world = World(
        [
            Page(
                name="search",
                items=["Search", "Popular tours"],
                url="https://example.com/",
                field="Search",
                on={"enter": submitted},
            ),
            Page(
                name="results",
                items=["First result", "Second result"],
                url="https://example.com/results",
                on={"click:First result": "detail"},
            ),
            Page(name="detail", items=["Bruno Mars", "Buy tickets"], url="https://example.com/detail"),
        ]
    )
    policy = scripted(("type_text", None), ("press_enter", None), ("click_item", "First result"), ("done", None))

    state = drive(
        world,
        policy,
        goal="search for the bruno mars tour",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=FakeWriter(text="bruno mars tour"),
    )

    assert state.outcome == "done"
    assert "typed 'bruno mars tour'" in state.history[0]
    assert state.history[1:] == ["pressed Return", "clicked 'First result'"]
    assert world.typed["Search"] == "bruno mars tour"
    assert world.page.name == "detail"
    assert world.log == ["type:bruno mars tour", "enter", "click:First result"]


def long_list_policy(state: dict, questions: dict) -> tuple:
    """Scroll until Buy is on screen, click it, and stop once it has been clicked."""
    texts = [it["text"] for it in state["screen_items_in_reading_order"]]
    if "Buy" in texts:
        return ("click_item", "Buy")
    if any("Buy" in action for action in state["previous_actions"]):
        return ("done", None)
    return ("scroll_down", None)


@pytest.mark.xfail(
    strict=True,
    reason="a repeated history line on an unchanged URL counts as a no-op, so the third scroll of one long page stalls the run",
)
def test_l7_a_long_page_is_scrolled_three_times(monkeypatch, tmp_path):
    listing = "https://example.com/list"  # scrolling one page never changes the URL
    world = World(
        [
            Page(name="list", items=["Event A", "Event B"], url=listing, on={"scroll_down": "list2"}),
            Page(name="list2", items=["Event C", "Event D"], url=listing, on={"scroll_down": "list3"}),
            Page(name="list3", items=["Event E", "Event F"], url=listing, on={"scroll_down": "list4"}),
            Page(name="list4", items=["Buy"], url=listing, on={"click:Buy": "checkout"}),
            Page(name="checkout", items=["Order summary", "Pay now"], url="https://example.com/checkout"),
        ]
    )

    state = drive(world, long_list_policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["scrolled down", "scrolled down", "scrolled down", "clicked 'Buy'"]
    assert world.page.name == "checkout"
    assert world.log == ["scroll_down", "scroll_down", "scroll_down", "click:Buy"]


@pytest.mark.xfail(
    strict=True,
    reason="'waited' reads as a no-op, so two waits in a row stop the run and a page needing three never finishes loading",
)
def test_l8_a_slow_page_needs_three_waits(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="home", items=["Home", "Tickets", "About"], url="https://example.com/", on={"click:Tickets": "tickets"}),
            Page(
                name="tickets",
                items=["Buy", "Terms"],
                url="https://example.com/tickets",
                loads_in=3,
                on={"click:Buy": "checkout"},
            ),
            Page(name="checkout", items=["Order summary", "Pay now"], url="https://example.com/checkout"),
        ]
    )
    policy = scripted(
        ("click_item", "Tickets"),
        ("wait", None),
        ("wait", None),
        ("wait", None),
        ("click_item", "Buy"),
        ("done", None),
    )

    state = drive(world, policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["clicked 'Tickets'", "waited", "waited", "waited", "clicked 'Buy'"]
    assert world.page.name == "checkout"
    assert world.log == ["click:Tickets", "wait", "wait", "wait", "click:Buy"]
    assert world.mouse == [SECOND_ROW, FIRST_ROW]
