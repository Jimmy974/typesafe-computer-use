from dataclasses import replace

from typesafe_computer_use.models import SAME_SCREEN_OVERLAP, Field, same_screen, signature
from typesafe_computer_use.runner import RunState, tried_here


def texts(*words: str):
    return frozenset(words)


def test_a_signature_names_the_app_the_page_the_focus_and_the_text(screen, make_item):
    field = Field(role="AXTextField", label="Search", placeholder="", value="", x=0, y=0, w=10, h=10)
    focused = replace(screen, field=field, url="https://example.com/")
    assert signature(focused, [make_item(0, "Search"), make_item(1, "Go")]) == (
        "Google Chrome",
        "https://example.com/",
        "AXTextField:Search",
        texts("Search", "Go"),
    )
    assert signature(screen, [])[2] is None


def test_the_same_text_on_another_page_or_app_or_focus_is_another_screen():
    here = ("Google Chrome", "https://a/", None, texts("Home", "Tickets"))
    assert same_screen(here, here)
    assert not same_screen(here, ("Finder", "https://a/", None, texts("Home", "Tickets")))
    assert not same_screen(here, ("Google Chrome", "https://b/", None, texts("Home", "Tickets")))
    assert not same_screen(here, ("Google Chrome", "https://a/", "AXTextField:Search", texts("Home", "Tickets")))


def test_a_clock_or_a_ticker_does_not_make_a_new_screen():
    lines = [f"Gate {n}" for n in range(9)]
    before = ("Google Chrome", None, None, texts(*lines, "12:00"))
    after = ("Google Chrome", None, None, texts(*lines, "12:01"))
    assert same_screen(before, after)
    assert SAME_SCREEN_OVERLAP == 0.9


def test_a_page_that_gained_a_section_is_a_new_screen():
    before = ("Google Chrome", None, None, texts("Home", "Tickets"))
    after = ("Google Chrome", None, None, texts("Home", "Tickets", "Buy", "Terms", "Dates"))
    assert not same_screen(before, after)
    assert same_screen(("Google Chrome", None, None, texts()), ("Google Chrome", None, None, texts()))


def test_tried_here_lists_the_actions_taken_on_the_current_screen_oldest_first():
    home = ("Google Chrome", "https://a/", None, texts("Home", "Tickets", "Blog"))
    blog = ("Google Chrome", "https://a/blog", None, texts("Blog", "Posts"))
    state = RunState(last=home, seen=[(home, "clicked 'Blog'"), (blog, "went back"), (home, "clicked 'Tickets'")])
    assert tried_here(state) == ["clicked 'Blog'", "clicked 'Tickets'"]
    assert tried_here(RunState()) == []
