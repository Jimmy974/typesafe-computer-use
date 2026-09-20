"""Scenarios of increasing difficulty, driven through the real `runner.run` loop.

Each test builds a page graph (the world), hands the loop a policy standing in for the classifier,
and asserts three things: the outcome the runner named, the page the world ended on, and the actions
the world received. A scenario that fails says the architecture cannot do that task; it is kept as
written and marked xfail with the reason, never weakened until it passes.
"""

from __future__ import annotations

import json

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


def test_l9_a_dead_end_is_undone_with_go_back(monkeypatch, tmp_path):
    world = World(
        [
            Page(
                name="home",
                items=["Blog", "Tickets"],
                url="https://example.com/",
                on={"click:Blog": "blog", "click:Tickets": "tickets"},
            ),
            Page(name="blog", items=["Our summer recap", "Older posts"], url="https://example.com/blog", on={"back": "home"}),
            Page(name="tickets", items=["Buy", "Terms"], url="https://example.com/tickets"),
        ]
    )
    policy = scripted(("click_item", "Blog"), ("go_back", None), ("click_item", "Tickets"), ("done", None))

    state = drive(world, policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["clicked 'Blog'", "went back", "clicked 'Tickets'"]
    assert world.page.name == "tickets"
    assert world.log == ["click:Blog", "back", "click:Tickets"]


def first_item_policy(state: dict, questions: dict) -> tuple:
    """The naive policy: whatever reads first on screen looks like the way forward."""
    return ("click_item", state["screen_items_in_reading_order"][0]["text"])


def test_l10_a_two_page_cycle_stops_as_stalled(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="a", items=["Next", "Page 1"], url="https://example.com/a", on={"click:Next": "b"}),
            Page(name="b", items=["Back", "Page 2"], url="https://example.com/b", on={"click:Back": "a"}),
        ]
    )

    state = drive(world, first_item_policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "stalled"
    assert len(state.history) <= 5  # the cycle is caught by the repeat rule, long before the step limit
    assert set(state.history) == {"clicked 'Next'", "clicked 'Back'"}


def test_l11_a_ticking_clock_does_not_hide_a_stall(monkeypatch, tmp_path):
    def rows(world: World) -> list[str]:
        """A departures board: one line ticks with the clock, the rest of the page never moves."""
        return [
            f"12:{world.ticks:02d}",
            "Refresh",
            "Departures",
            "Gate A",
            "Gate B",
            "Gate C",
            "Gate D",
            "Gate E",
            "Gate F",
            "Gate G",
        ]

    world = World([Page(name="board", items=rows, url="https://example.com/board")])  # "Refresh" has no transition

    state = drive(world, scripted(*[("click_item", "Refresh")] * 8), goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "stalled"
    assert len(state.history) <= 4
    assert set(state.history) == {"clicked 'Refresh'"}
    clock = [state["screen_items_in_reading_order"][0]["text"] for state in world.fake.states]
    assert len(set(clock)) == len(clock)  # the clock really did tick between the captures


def wizard_policy(state: dict, questions: dict) -> tuple:
    """Click Next while the wizard still offers one, then call it done."""
    texts = [it["text"] for it in state["screen_items_in_reading_order"]]
    return ("click_item", "Next") if "Next" in texts else ("done", None)


def test_l12_a_wizard_repeats_next_across_distinct_pages(monkeypatch, tmp_path):
    wizard = "https://example.com/signup"  # every step of the wizard lives at the same URL
    world = World(
        [
            Page(name="step1", items=["Your name", "Next"], url=wizard, on={"click:Next": "step2"}),
            Page(name="step2", items=["Your address", "Next"], url=wizard, on={"click:Next": "step3"}),
            Page(name="step3", items=["Your payment", "Next"], url=wizard, on={"click:Next": "finished"}),
            Page(name="finished", items=["All set", "Receipt"], url=wizard),
        ]
    )

    state = drive(world, wizard_policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["clicked 'Next'"] * 3  # the same action three times, on three different screens
    assert world.page.name == "finished"
    assert world.log == ["click:Next"] * 3


def test_l13_an_offscreen_control_is_pressed(monkeypatch, tmp_path):
    world = World(
        [
            Page(
                name="home",
                items=["Welcome", "About"],
                url="https://example.com/",
                offscreen=("Register Now",),
                on={"press:Register Now": "registration"},
            ),
            Page(name="registration", items=["Full name", "Submit"], url="https://example.com/register"),
        ]
    )
    policy = scripted(("press_offscreen", "Register Now"), ("done", None))

    state = drive(world, policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["pressed 'Register Now' (off-screen control) via accessibility"]
    assert world.page.name == "registration"
    assert world.log == ["press:Register Now"]
    assert world.mouse == []  # there is no pixel to click: the control is parked above the viewport


def always_type_policy(state: dict, questions: dict) -> tuple:
    return ("type_text", None)


def test_l14_a_field_the_writer_declines_is_never_typed_and_the_run_stalls(monkeypatch, tmp_path):
    world = World(
        [
            Page(
                name="login",
                items=["Password", "Sign in"],
                url="https://example.com/login",
                field="Password",
                on={"click:Sign in": "account"},
            ),
            Page(name="account", items=["Your account"], url="https://example.com/account"),
        ]
    )

    state = drive(
        world,
        always_type_policy,
        goal=GOAL,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=FakeWriter(text=""),  # the writer will not put a password in a password field
    )

    assert state.outcome == "stalled"
    assert world.typed == {}
    assert len(state.history) == 3  # MAX_IDLE refusals, none of which touched the machine
    assert all("refused" in line for line in state.history)
    assert world.page.name == "login"
    assert world.log == []


def test_l15_use_browser_from_another_app_opens_a_catalog_site(monkeypatch, tmp_path):
    world = World(
        [
            Page(
                name="finder",
                items=["Documents", "Downloads"],
                url=None,
                app="Finder",
                on={"open:https://github.com/": "github"},
            ),
            Page(
                name="github",
                items=["Sign in", "Pull requests"],
                url="https://github.com/",
                on={"click:Sign in": "signin"},
            ),
            Page(name="signin", items=["Username", "Password"], url="https://github.com/login"),
        ]
    )
    policy = scripted(("use_browser", "github"), ("click_item", "Sign in"), ("done", None))

    state = drive(world, policy, goal="sign in to github", monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history[0] == "opened https://github.com/"
    assert state.history == ["opened https://github.com/", "clicked 'Sign in'"]
    assert world.page.name == "signin"
    assert world.log == ["open:https://github.com/", "click:Sign in"]
    assert world.fake.states[0]["frontmost_app"] == "Finder"


def test_l16_a_page_that_never_loads_stops_within_the_idle_budget(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="home", items=["Home", "Tickets", "About"], url="https://example.com/", on={"click:Tickets": "tickets"}),
            Page(name="tickets", items=["Buy", "Terms"], url="https://example.com/tickets", loads_in=10),
        ]
    )
    policy = scripted(("click_item", "Tickets"), *[("wait", None)] * 10)

    state = drive(world, policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "stalled"
    assert state.history == ["clicked 'Tickets'"] + ["waited"] * 3
    assert world.page.name == "tickets"
    assert world.log == ["click:Tickets"] + ["wait"] * 3


def test_l17_low_confidence_stops_the_run(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="home", items=["Home", "Tickets", "About"], url="https://example.com/", on={"click:Tickets": "tickets"}),
            Page(name="tickets", items=["Buy", "Terms"], url="https://example.com/tickets"),
        ]
    )

    state = drive(world, scripted(("click_item", "Tickets", 0.3)), goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "low confidence"
    assert state.history == []
    assert world.page.name == "home"
    assert world.log == []
    assert world.mouse == []


def always_scroll_policy(state: dict, questions: dict) -> tuple:
    return ("scroll_down", None)


def test_l18_the_step_limit_ends_an_endless_list(monkeypatch, tmp_path):
    listing = "https://example.com/feed"
    world = World(
        [
            Page(name=f"list{n}", items=[f"Post {2 * n - 1}", f"Post {2 * n}"], url=listing, on={"scroll_down": f"list{n + 1}"})
            for n in range(1, 7)
        ]
    )

    state = drive(world, always_scroll_policy, goal=GOAL, steps=3, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "step limit"
    assert state.history == ["scrolled down"] * 3
    assert world.page.name == "list4"
    assert world.log == ["scroll_down"] * 3


def test_l19_typing_that_fails_verification_is_cleared(monkeypatch, tmp_path):
    world = World(
        [
            Page(
                name="search",
                items=["Search", "Popular tours"],
                url="https://example.com/",
                field="Search",
                on={"enter": "results"},
            ),
            Page(name="results", items=["First result", "Second result"], url="https://example.com/results"),
        ]
    )
    policy = scripted(("type_text", None), ("done", None))

    state = drive(
        world,
        policy,
        goal="search for the bruno mars tour",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=FakeWriter(text="bruno mars tour"),
        noul=0.2,  # the classifier does not believe the field holds what was typed
    )

    assert state.outcome == "done"
    assert "verification failed" in state.history[0] and "cleared it" in state.history[0]
    assert "Search" not in world.typed  # the field was cleared, so nothing was left behind
    assert world.log == ["type:bruno mars tour", "clear_field"]
    assert world.page.name == "search"


def steering_policy(state: dict, questions: dict) -> tuple:
    """Click the first link the state does not list as already tried here; stop when Buy was clicked."""
    if any("Buy" in action for action in state["previous_actions"]):
        return ("done", None)
    tried = state["already_tried_on_this_screen"]
    for it in state["screen_items_in_reading_order"]:
        if f"clicked {it['text']!r}" not in tried:
            return ("click_item", it["text"])
    return ("none", None)


def test_l20_a_dead_link_is_steered_around_with_what_was_already_tried(monkeypatch, tmp_path):
    """The first link leads back to the same screen. The state says so; the policy takes the second."""
    world = World(
        [
            Page(name="home", items=["Sponsors", "Tickets"], url="https://example.com/", on={"click:Tickets": "tickets"}),
            Page(name="tickets", items=["Buy"], url="https://example.com/tickets", on={"click:Buy": "checkout"}),
            Page(name="checkout", items=["Order summary", "Pay now"], url="https://example.com/checkout"),
        ]
    )

    state = drive(world, steering_policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["clicked 'Sponsors'", "clicked 'Tickets'", "clicked 'Buy'"]
    assert world.page.name == "checkout"
    assert world.fake.states[0]["already_tried_on_this_screen"] == []
    assert world.fake.states[1]["already_tried_on_this_screen"] == ["clicked 'Sponsors'"]
    assert world.fake.states[2]["already_tried_on_this_screen"] == []


def test_l21_a_composite_task_runs_the_whole_pipeline(monkeypatch, tmp_path):
    """Every seam in one run: the browser, a modal, a slow page, a scroll, a field, and a result."""

    def searched(world: World) -> str | None:
        return "results" if world.typed.get("Search") == "bruno mars" else None

    listing = "https://shows.example.com/listing"
    world = World(
        [
            Page(
                name="finder",
                items=["Documents", "Downloads"],
                url=None,
                app="Finder",
                on={"open:https://shows.example.com/": "cookies"},
            ),
            Page(
                name="cookies",
                items=["Accept cookies", "Reject"],
                url="https://shows.example.com/",
                on={"escape": "listing"},
            ),
            Page(name="listing", items=["Event A", "Event B"], url=listing, loads_in=2, on={"scroll_down": "listing2"}),
            Page(name="listing2", items=[("Search", "field"), "Event C"], url=listing, on={"click:Search": "search"}),
            Page(
                name="search",
                items=["Search", "Popular tours"],
                url="https://shows.example.com/search",
                field="Search",
                on={"enter": searched},
            ),
            Page(
                name="results",
                items=["First result", "Second result"],
                url="https://shows.example.com/results",
                on={"click:First result": "detail"},
            ),
            Page(name="detail", items=["Bruno Mars", "Buy tickets"], url="https://shows.example.com/detail"),
        ]
    )
    policy = scripted(
        ("use_browser", "other"),
        ("press_escape", None),
        ("wait", None),
        ("wait", None),
        ("scroll_down", None),
        ("click_item", "Search"),
        ("type_text", None),
        ("press_enter", None),
        ("click_item", "First result"),
        ("done", None),
    )

    state = drive(
        world,
        policy,
        goal="find the bruno mars show",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=FakeWriter(text="bruno mars", url="https://shows.example.com/"),
    )

    assert state.outcome == "done"
    assert state.history[:6] == [
        "opened https://shows.example.com/",
        "pressed Escape",
        "waited",
        "waited",
        "scrolled down",
        "pressed 'Search' via accessibility",
    ]
    assert state.history[6] == "typed 'bruno mars' into 'Search' via accessibility (verified 0.95)"
    assert state.history[7:] == ["pressed Return", "clicked 'First result'"]
    assert world.page.name == "detail"
    assert world.log == [
        "open:https://shows.example.com/",
        "escape",
        "wait",
        "wait",
        "scroll_down",
        "click:Search",
        "type:bruno mars",
        "enter",
        "click:First result",
    ]


LINKS = [f"Link {letter}" for letter in "ABCDEFGHIJ"]


def hub_policy(state: dict, questions: dict) -> tuple:
    """Take the first link this screen has not been sent down before; back out of every dead end."""
    texts = [it["text"] for it in state["screen_items_in_reading_order"]]
    if "Buy tickets" in texts:
        return ("done", None)
    if "Nothing here" in texts:
        return ("go_back", None)
    tried = state["already_tried_on_this_screen"]
    for text in texts:
        if f"clicked {text!r}" not in tried:
            return ("click_item", text)
    return ("none", None)


def test_l22_a_hub_of_dead_links_is_searched_beyond_the_history_window(monkeypatch, tmp_path):
    dead = [
        Page(
            name=f"dead{letter}",
            items=["Nothing here", f"Dead {letter}"],
            url=f"https://example.com/{letter.lower()}",
            on={"back": "hub"},
        )
        for letter in "ABCDEFGHI"
    ]
    hub = Page(
        name="hub",
        items=LINKS,
        url="https://example.com/",
        on={**{f"click:Link {letter}": f"dead{letter}" for letter in "ABCDEFGHI"}, "click:Link J": "target"},
    )
    world = World([hub, *dead, Page(name="target", items=["Buy tickets", "Terms"], url="https://example.com/j")])

    state = drive(world, hub_policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert len(state.history) == 19  # nine dead links, nine ways back, and the tenth link
    assert state.history[-1] == "clicked 'Link J'"
    assert world.page.name == "target"
    last_hub = world.fake.states[18]  # the hub as it looked the step Link J was clicked
    assert len(last_hub["previous_actions"]) == 8
    assert not any("Link A" in action for action in last_hub["previous_actions"])  # fallen out of the window
    assert last_hub["already_tried_on_this_screen"] == [f"clicked 'Link {letter}'" for letter in "ABCDEFGHI"]


def modal_policy(state: dict, questions: dict) -> tuple:
    """Escape a modal once; if the screen is still there, the modal wants its own button pressed."""
    texts = [it["text"] for it in state["screen_items_in_reading_order"]]
    if "Sign up for news" in texts:
        if "pressed Escape" in state["already_tried_on_this_screen"]:
            return ("click_item", "Close")
        return ("press_escape", None)
    if "Order summary" in texts:
        return ("done", None)
    return ("click_item", "Buy" if "Buy" in texts else "Tickets")


def test_l23_a_modal_escape_cannot_close_is_closed_by_its_button(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="home", items=["Home", "Tickets"], url="https://example.com/", on={"click:Tickets": "modal"}),
            Page(
                name="modal",
                items=["Sign up for news", "Close"],
                url="https://example.com/tickets",
                on={"click:Close": "tickets"},  # escape does nothing here
            ),
            Page(name="tickets", items=["Buy", "Terms"], url="https://example.com/tickets", on={"click:Buy": "checkout"}),
            Page(name="checkout", items=["Order summary", "Pay now"], url="https://example.com/checkout"),
        ]
    )

    state = drive(world, modal_policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["clicked 'Tickets'", "pressed Escape", "clicked 'Close'", "clicked 'Buy'"]
    assert world.page.name == "checkout"
    assert world.log == ["click:Tickets", "escape", "click:Close", "click:Buy"]


def test_l24_type_email_fills_the_focused_email_field(monkeypatch, tmp_path):
    world = World(
        [
            Page(
                name="login",
                items=["Email", "Sign in"],
                url="https://example.com/login",
                field="Email",
                on={"click:Sign in": "account"},
            ),
            Page(name="account", items=["Your account"], url="https://example.com/account"),
        ]
    )
    policy = scripted(("type_email", None), ("done", None))

    state = drive(world, policy, goal="sign in", monkeypatch=monkeypatch, tmp_path=tmp_path, email="user@example.com")

    assert state.outcome == "done"
    assert state.history == ["typed email via accessibility"]
    assert world.typed["Email"] == "user@example.com"
    assert world.log == ["type:user@example.com"]
    assert "type_email" in world.fake.asked[0]["kind"].criteria  # the action is only offered when an email is known


def test_l25_a_stalled_run_still_answers_from_the_last_screen(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="a", items=["Next", "Page 1"], url="https://example.com/a", on={"click:Next": "b"}),
            Page(name="b", items=["Back", "Page 2"], url="https://example.com/b", on={"click:Back": "a"}),
        ]
    )

    state = drive(world, first_item_policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "stalled"
    assert state.answer is not None
    # The answer leads with the screen the run ended on; what follows is the screens seen before it.
    assert state.answer.text.startswith(" ".join(world.page.items))
    assert len(world.log) == len(state.history)  # re-reading the screen costs no action


def test_l26_a_refused_action_then_a_good_one_does_not_stall(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="home", items=["Home", "Tickets"], url="https://example.com/", on={"click:Tickets": "tickets"}),
            Page(name="tickets", items=["Buy", "Terms"], url="https://example.com/tickets"),
        ]
    )
    policy = scripted(("type_text", None), ("click_item", "Tickets"), ("done", None))

    state = drive(world, policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["type_text refused: no text field is focused", "clicked 'Tickets'"]
    assert world.page.name == "tickets"
    assert world.log == ["click:Tickets"]  # the refusal never reached the machine


def test_l27_scrolling_past_the_end_stops_within_the_idle_budget(monkeypatch, tmp_path):
    listing = "https://example.com/list"
    world = World(
        [
            Page(name="list", items=["Event A", "Event B"], url=listing, on={"scroll_down": "list2"}),
            Page(name="list2", items=["Event C", "Event D"], url=listing),  # the end: scrolling does nothing
        ]
    )

    state = drive(world, always_scroll_policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "stalled"
    assert state.history == ["scrolled down"] * 4  # one that moved, then three at the bottom
    assert world.page.name == "list2"


def answer_packet(writer: FakeWriter) -> dict:
    """The packet of the writer's last call: the one that composed the answer."""
    return json.loads(writer.requests[-1]["messages"][0]["content"][-1]["text"])


def test_l28_the_answer_can_use_a_screen_seen_on_the_way(monkeypatch, tmp_path):
    """The price was two screens back. The run ended on checkout, and the answer still has to carry it."""
    world = World(
        [
            Page(name="home", items=["Home", "Tickets"], url="https://example.com/", on={"click:Tickets": "tickets"}),
            Page(
                name="tickets",
                items=["Standard $45", "VIP $120", "Buy"],
                url="https://example.com/tickets",
                on={"click:Buy": "checkout"},
            ),
            Page(name="checkout", items=["Order summary", "Pay now"], url="https://example.com/checkout"),
        ]
    )
    policy = scripted(("click_item", "Tickets"), ("click_item", "Buy"), ("done", None))
    writer = FakeWriter()

    state = drive(
        world,
        policy,
        goal="find the ticket price and go to checkout",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=writer,
    )

    assert state.outcome == "done"
    assert world.page.name == "checkout"
    assert state.answer is not None and "$45" in state.answer.text
    assert answer_packet(writer)["earlier_screens"][-1]["url"] == "https://example.com/tickets"


def test_l29_keystrokes_replace_what_a_field_already_holds(monkeypatch, tmp_path):
    def searched(world: World) -> str | None:
        """Only the exact query reaches the results: a field typed on top of its old value does not."""
        return "results" if world.typed.get("Search") == "bruno mars tour" else None

    world = World(
        [
            Page(
                name="search",
                items=["Search", "Popular tours"],
                url="https://example.com/",
                field="Search",
                no_ax_value=True,  # this field refuses the value path, so the keystrokes run
                on={"enter": searched},
            ),
            Page(name="results", items=["First result", "Second result"], url="https://example.com/results"),
        ]
    )
    world.typed["Search"] = "old query"  # the field is not empty when the loop first sees it
    policy = scripted(("type_text", None), ("press_enter", None), ("done", None))

    state = drive(
        world,
        policy,
        goal="search for the bruno mars tour",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=FakeWriter(text="bruno mars tour"),
    )

    assert state.outcome == "done"
    assert "via keystrokes" in state.history[0]
    assert world.typed["Search"] == "bruno mars tour"
    assert world.log.index("clear_field") < world.log.index("type:bruno mars tour")
    assert world.page.name == "results"


def test_l30_focus_stolen_by_another_app_is_taken_back_with_use_browser(monkeypatch, tmp_path):
    world = World(
        [
            Page(name="tickets", items=["Buy", "Terms"], url="https://example.com/tickets", on={"click:Buy": "finder"}),
            Page(name="finder", items=["Downloads", "receipt.pdf"], url=None, app="Finder", on={"activate": "checkout"}),
            Page(name="checkout", items=["Order summary", "Pay now"], url="https://example.com/checkout"),
        ]
    )
    policy = scripted(("click_item", "Buy"), ("use_browser", "none"), ("done", None))

    state = drive(world, policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["clicked 'Buy'", "activated Google Chrome"]
    assert world.page.name == "checkout"
    assert world.log == ["click:Buy", "activate"]


def test_l31_an_unusable_writer_url_is_refused_and_the_catalog_is_used_instead(monkeypatch, tmp_path):
    world = World(
        [
            Page(
                name="finder",
                items=["Documents", "Downloads"],
                url=None,
                app="Finder",
                on={"open:https://github.com/": "github"},
            ),
            Page(name="github", items=["Sign in", "Pull requests"], url="https://github.com/"),
        ]
    )
    policy = scripted(("use_browser", "other"), ("use_browser", "github"), ("done", None))

    state = drive(
        world,
        policy,
        goal="open the issue tracker",
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=FakeWriter(url="http://insecure.example.com/"),  # not https: the writer's proposal is dropped
    )

    assert state.outcome == "done"
    assert state.history[0] == "use_browser refused: the writer proposed no usable URL for this goal"
    assert state.history[1] == "opened https://github.com/"
    assert world.page.name == "github"
    assert world.log == ["open:https://github.com/"]
    assert not any("insecure.example.com" in action for action in world.log)


def growing_feed(world: World) -> list[str]:
    """A feed that gains a post every time it is read."""
    return [f"Post {n}" for n in range(1, world.ticks + 4)] + ["Load more"]


def test_l32_a_feed_that_grows_every_step_is_not_mistaken_for_a_stall(monkeypatch, tmp_path):
    world = World([Page(name="feed", items=growing_feed, url="https://example.com/feed")])
    policy = scripted(*[("click_item", "Load more")] * 5)

    state = drive(world, policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["clicked 'Load more'"] * 5  # every capture showed a longer page, so nothing stalled
    assert len(world.fake.states[-1]["screen_items_in_reading_order"]) > len(
        world.fake.states[0]["screen_items_in_reading_order"]
    )


def test_l32b_a_feed_that_never_grows_is_a_stall(monkeypatch, tmp_path):
    world = World([Page(name="feed", items=["Post 1", "Post 2", "Post 3", "Load more"], url="https://example.com/feed")])
    policy = scripted(*[("click_item", "Load more")] * 8)

    state = drive(world, policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "stalled"
    assert state.history == ["clicked 'Load more'"] * 3  # the repeat rule ends it one step before the idle rule would


def coldplay_policy(state: dict, questions: dict) -> tuple:
    """Three buttons read 'Buy'. Only the row they sit in says which show they buy."""
    for it in state["screen_items_in_reading_order"]:
        if it["text"] == "Buy" and "Coldplay" in it.get("beside", []):
            return ("click_item", it["i"])  # by index: the text alone names three items
    return ("done", None)


def test_l33_duplicate_labels_are_told_apart_by_their_row(monkeypatch, tmp_path):
    world = World(
        [
            Page(
                name="listing",
                items=[
                    ["Bruno Mars", "Sep 25", "Buy"],
                    ["Coldplay", "Oct 2", "Buy"],
                    ["Adele", "Oct 9", "Buy"],
                ],
                url="https://example.com/listing",
                on={
                    "click:Buy@0": "bruno_checkout",
                    "click:Buy@1": "coldplay_checkout",
                    "click:Buy@2": "adele_checkout",
                },
            ),
            Page(name="bruno_checkout", items=["Order summary", "Bruno Mars"], url="https://example.com/checkout/bruno"),
            Page(name="coldplay_checkout", items=["Order summary", "Coldplay"], url="https://example.com/checkout/coldplay"),
            Page(name="adele_checkout", items=["Order summary", "Adele"], url="https://example.com/checkout/adele"),
        ]
    )

    state = drive(world, coldplay_policy, goal="buy a ticket to Coldplay", monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["clicked 'Buy'"]
    assert world.page.name == "coldplay_checkout"
    assert world.log == ["click:Buy@1"]

    listing = world.fake.states[0]["screen_items_in_reading_order"]
    chosen = next(it for it in listing if it["text"] == "Buy" and "Coldplay" in it.get("beside", []))
    assert chosen["beside"] == ["Coldplay", "Oct 2"]
    assert "in the row of 'Coldplay', 'Oct 2'" in world.fake.asked[0]["item"].criteria[str(chosen["i"])]
    unique = next(it for it in listing if it["text"] == "Coldplay")
    assert "beside" not in unique  # nothing else on screen reads 'Coldplay', so the row says nothing new


def banner_policy(state: dict, questions: dict) -> tuple:
    texts = [it["text"] for it in state["screen_items_in_reading_order"]]
    return ("done", None) if "Order summary" in texts else ("click_item", "Buy")


def test_l34_a_banner_covering_the_page_takes_the_first_click(monkeypatch, tmp_path):
    tickets = "https://example.com/tickets"
    world = World(
        [
            Page(
                name="tickets",
                items=["Buy", "Terms", "Accept cookies"],
                url=tickets,
                covered_by="Accept cookies",  # the banner is over the page: every click lands on it
                on={"click:Accept cookies": "tickets_clear"},
            ),
            Page(name="tickets_clear", items=["Buy", "Terms"], url=tickets, on={"click:Buy": "checkout"}),
            Page(name="checkout", items=["Order summary", "Pay now"], url="https://example.com/checkout"),
        ]
    )

    state = drive(world, banner_policy, goal=GOAL, monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert state.history == ["clicked 'Buy'", "clicked 'Buy'"]  # the loop aimed at Buy twice
    assert world.log == ["click:Accept cookies", "click:Buy"]  # the banner took the first one
    assert world.page.name == "checkout"


def crawl_policy(state: dict, questions: dict) -> tuple:
    """A depth-first crawl written only against the state: take the first untried link, else back out."""
    texts = [it["text"] for it in state["screen_items_in_reading_order"]]
    if any("Buy" in action for action in state["previous_actions"]):
        return ("done", None)
    if "Buy" in texts:
        return ("click_item", "Buy")
    tried = state["already_tried_on_this_screen"]
    for text in texts:
        if not text.startswith("Page: ") and f"clicked {text!r}" not in tried:
            return ("click_item", text)
    return ("none", None) if "Page: home" in texts else ("go_back", None)


def test_l35_a_small_site_is_searched_exhaustively_for_the_one_page_that_sells(monkeypatch, tmp_path):
    site = "https://example.com"
    world = World(
        [
            Page(
                name="home",
                items=["Page: home", "About", "Contact", "Shows"],
                url=f"{site}/",
                on={"click:About": "about", "click:Contact": "contact", "click:Shows": "shows"},
            ),
            Page(name="about", items=["Page: about"], url=f"{site}/about", on={"back": "home"}),
            Page(name="contact", items=["Page: contact"], url=f"{site}/contact", on={"back": "home"}),
            Page(
                name="shows",
                items=["Page: shows", "Past", "Upcoming"],
                url=f"{site}/shows",
                on={"click:Past": "past", "click:Upcoming": "upcoming", "back": "home"},
            ),
            Page(name="past", items=["Page: past"], url=f"{site}/shows/past", on={"back": "shows"}),
            Page(
                name="upcoming",
                items=["Page: upcoming", "Buy"],
                url=f"{site}/shows/upcoming",
                on={"click:Buy": "checkout", "back": "shows"},
            ),
            Page(name="checkout", items=["Page: checkout", "Order summary"], url=f"{site}/checkout"),
        ]
    )

    state = drive(world, crawl_policy, goal="buy a ticket", monkeypatch=monkeypatch, tmp_path=tmp_path)

    assert state.outcome == "done"
    assert world.page.name == "checkout"
    assert state.history == [
        "clicked 'About'",
        "went back",
        "clicked 'Contact'",
        "went back",
        "clicked 'Shows'",
        "clicked 'Past'",
        "went back",
        "clicked 'Upcoming'",
        "clicked 'Buy'",
    ]
    assert len(state.history) == 9  # nothing stalled: every step of the crawl moved the screen


def long_run_policy(state: dict, questions: dict) -> tuple:
    """One policy for the whole run, reading nothing but the state the loop hands it."""
    texts = [it["text"] for it in state["screen_items_in_reading_order"]]
    tried = state["already_tried_on_this_screen"]
    field = state["focused_field"]
    if "Loading..." in texts or "Verifying" in texts:
        return ("wait", None)
    if state["frontmost_app"] == "Finder":  # the first Finder needs a site; the second only the front
        return ("use_browser", "other" if "Desktop" in texts else "none")
    if "Order summary" in texts:
        return ("done", None)
    if "Sign up for news" in texts:
        return ("click_item", "Close") if "pressed Escape" in tried else ("press_escape", None)
    if field and field["label"] == "Email":
        return ("press_enter", None) if field["current_value"] else ("type_email", None)
    for it in state["screen_items_in_reading_order"]:
        if it["text"] == "Buy" and "Coldplay" in it.get("beside", []):
            return ("click_item", it["i"])
    if "Nothing here" in texts:
        return ("go_back", None)
    if "Older posts" in texts:
        return ("go_back", None) if "clicked 'Older posts'" in tried else ("click_item", "Older posts")
    if "Blog" in texts and "clicked 'Blog'" not in tried:
        return ("click_item", "Blog")
    if state.get("offscreen_controls"):
        return ("press_offscreen", "Show all dates")
    if "Next" in texts:
        return ("click_item", "Next")
    if "Buy" in texts:
        return ("click_item", "Buy")
    return ("scroll_down", None)


def long_run_world() -> World:
    shows = "https://shows.example.com"
    return World(
        [
            Page(name="finder", items=["Desktop", "Documents"], url=None, app="Finder", on={f"open:{shows}/": "front"}),
            Page(
                name="front",
                items=["Buy", "Terms", "Accept cookies"],
                url=f"{shows}/",
                covered_by="Accept cookies",
                on={"click:Accept cookies": "listing"},
            ),
            Page(
                name="listing",
                items=["Listing", "Blog", "Event A"],
                url=f"{shows}/listing",
                loads_in=2,
                offscreen=("Show all dates",),
                on={"click:Blog": "blog", "press:Show all dates": "dates1"},
            ),
            Page(
                name="blog",
                items=["Blog post", "Older posts"],
                url=f"{shows}/blog",
                on={"click:Older posts": "blog2", "back": "listing"},
            ),
            Page(name="blog2", items=["Older posts page", "Nothing here"], url=f"{shows}/blog/older", on={"back": "blog"}),
            Page(name="dates1", items=["All dates", "Event C"], url=f"{shows}/dates", on={"scroll_down": "dates2"}),
            Page(name="dates2", items=["More dates", "Event D"], url=f"{shows}/dates", on={"scroll_down": "dates3"}),
            Page(name="dates3", items=["Even more", "Event E"], url=f"{shows}/dates", on={"scroll_down": "rows"}),
            Page(
                name="rows",
                items=[
                    ["Bruno Mars", "Sep 25", "$45", "Buy"],
                    ["Coldplay", "Oct 2", "$60", "Buy"],
                    ["Adele", "Oct 9", "$80", "Buy"],
                ],
                url=f"{shows}/dates/rows",
                on={"click:Buy@0": "modal", "click:Buy@1": "modal", "click:Buy@2": "modal"},
            ),
            Page(
                name="modal",
                items=["Sign up for news", "Close"],
                url=f"{shows}/dates/rows",
                on={"click:Close": "login"},  # Escape does nothing to this one
            ),
            Page(name="login", items=["Sign in", "Email"], url=f"{shows}/login", field="Email", on={"enter": "verifying"}),
            Page(
                name="verifying",
                items=["Verifying", "Almost done"],
                url=f"{shows}/verify",
                loads_in=3,
                on={"wait": "finder2"},  # the page finishes by itself, into a window that steals the front
            ),
            Page(name="finder2", items=["Downloads", "receipt.pdf"], url=None, app="Finder", on={"activate": "wiz1"}),
            Page(name="wiz1", items=["Step 1 of 3", "Next"], url=f"{shows}/signup", on={"click:Next": "wiz2"}),
            Page(name="wiz2", items=["Step 2 of 3", "Next"], url=f"{shows}/signup", on={"click:Next": "wiz3"}),
            Page(name="wiz3", items=["Step 3 of 3", "Next"], url=f"{shows}/signup", on={"click:Next": "checkout"}),
            Page(name="checkout", items=["Order summary", "Pay now"], url=f"{shows}/checkout"),
        ]
    )


def test_l36_a_long_run_mixes_everything(monkeypatch, tmp_path):
    world = long_run_world()

    state = drive(
        world,
        long_run_policy,
        goal="buy a ticket to Coldplay and tell me the price",
        steps=40,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
        writer=FakeWriter(url="https://shows.example.com/"),
        email="user@example.com",
    )

    assert state.outcome == "done"
    assert world.page.name == "checkout"
    assert state.history == [
        "opened https://shows.example.com/",
        "clicked 'Buy'",
        "waited",
        "waited",
        "clicked 'Blog'",
        "clicked 'Older posts'",
        "went back",
        "went back",
        "pressed 'Show all dates' (off-screen control) via accessibility",
        "scrolled down",
        "scrolled down",
        "scrolled down",
        "clicked 'Buy'",
        "pressed Escape",
        "clicked 'Close'",
        "typed email via accessibility",
        "pressed Return",
        "waited",
        "waited",
        "waited",
        "waited",
        "activated Google Chrome",
        "clicked 'Next'",
        "clicked 'Next'",
        "clicked 'Next'",
    ]
    assert len(state.history) == 25
    assert world.log[world.log.index("click:Buy@1")] == "click:Buy@1"  # the Coldplay row, picked by what sits beside it
    login = next(i for i, s in enumerate(world.fake.states) if s["focused_field"] and s["focused_field"]["label"] == "Email")
    assert "type_email" in world.fake.asked[login]["kind"].criteria
    assert state.answer is not None and "$60" in state.answer.text  # the price was on a screen the run passed through
