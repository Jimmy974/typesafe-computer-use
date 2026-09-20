"""Scenarios of increasing difficulty, driven through the real `runner.run` loop.

Each test builds a page graph (the world), hands the loop a policy standing in for the classifier,
and asserts three things: the outcome the runner named, the page the world ended on, and the actions
the world received. A scenario that fails says the architecture cannot do that task; it is kept as
written and marked xfail with the reason, never weakened until it passes.
"""

from __future__ import annotations

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
