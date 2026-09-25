"""Saved form values: read from a file, typed exactly, and never shown to the classifier."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_browser_loop import FakeBrowser, FakeTypeSafe, choice, item, login_page

from typesafe_computer_use.browser.decide import SAVED_DATA_RULE, saved_criteria
from typesafe_computer_use.browser.report import RunFolder
from typesafe_computer_use.browser.runner import run_goal
from typesafe_computer_use.formdata import FormData, load_data, parse_data

PHONE = "5550-1234-secret-ish"
REPO = Path(__file__).resolve().parent.parent


def test_a_data_file_reads_fields_and_choices_as_text(tmp_path):
    path = tmp_path / "d.toml"
    path.write_text('[fields]\nname = "Jimmy"\nphone = 12345678\n[choices]\nsize = "Medium"\n')
    data = load_data(path)
    assert data.fields == {"name": "Jimmy", "phone": "12345678"} and data.choices == {"size": "Medium"}


@pytest.mark.parametrize(
    "raw, complaint",
    [
        ({"feilds": {"a": "b"}}, "unknown section"),
        ({"fields": {"password": "hunter2"}}, "credential"),
        ({"fields": {"a": ["x"]}}, "text or a number"),
        ({"fields": {"a": True}}, "text or a number"),
        ({}, "no values"),
    ],
)
def test_a_bad_data_file_is_an_error_before_anything_runs(raw, complaint):
    with pytest.raises(ValueError, match=complaint):
        parse_data(raw, "d.toml")


def test_the_classifier_sees_field_names_and_whether_they_are_typed_never_the_values():
    data = FormData(fields={"name": "Jimmy", "phone": PHONE}, choices={"size": "Medium"})
    state = data.state({"name"})
    assert state == {
        "saved_fields": [{"name": "name", "typed": True}, {"name": "phone", "typed": False}],
        "saved_choices": {"size": "Medium"},
    }
    assert PHONE not in json.dumps(state) and PHONE not in json.dumps(saved_criteria(data, set()))
    assert FormData.from_state(state) == (FormData(fields={"name": "", "phone": ""}, choices={"size": "Medium"}), {"name"})


def test_the_shipped_pizza_data_parses():
    assert load_data(REPO / "bench" / "data" / "pizza.toml").fields["customer_name"] == "Jimmy Wong"


def form_page():
    items = [item(0, "Telephone", role="tel", filled=False), item(1, "Submit order", tag="button", field=False)]
    return {**login_page(), "url": "https://example.test/order", "items": items, "count": 2}


class Saving(FakeTypeSafe):
    """The classifier types into `element`, answers `key` when asked which saved field, then is done."""

    def __init__(self, key: str, element: str = "0", confidence: float = 0.9):
        super().__init__(("type_text", element))
        self.key, self.confidence = key, confidence

    def system_one(self, *, state, questions, model=None):
        if set(questions) == {"saved"}:
            self.requests.append({"state": state, "questions": questions})
            return SimpleNamespace(answers={"saved": choice(self.key, self.confidence)})
        return super().system_one(state=state, questions=questions, model=model)

    def asked_saved(self) -> list[dict]:
        return [r for r in self.requests if set(r["questions"]) == {"saved"}]


class Holding(FakeBrowser):
    """A field that holds what was typed into it, read back through the value probe."""

    def call(self, method, params=None):
        if method == "Input.insertText" and params and params.get("text"):
            self.values[0] = params["text"]
        return super().call(method, params)


def run(browser, client, tmp_path, data, writer=None):
    folder = RunFolder.create(tmp_path)
    result = run_goal(
        browser,
        client,
        "fill the order",
        max_steps=2,
        change_timeout_ms=0,
        verbose=False,
        runfolder=folder,
        data=data,
        writer=writer,
    )
    return result, folder


def test_a_saved_field_is_typed_exactly_and_checked_here_not_by_the_classifier(tmp_path):
    browser, client = Holding(form_page()), Saving("phone")
    result, folder = run(browser, client, tmp_path, FormData(fields={"phone": PHONE}))

    assert browser.typed == [PHONE]
    assert result.steps[0].detail == "type <phone> -> matches" and result.steps[0].text_source == "data:phone"
    assert all(set(r["questions"]) != {"ok"} for r in client.requests)  # no verify_typed call
    # The saved field was asked about the chosen field alone, by its label.
    [asked] = client.asked_saved()
    assert asked["state"]["field"] == "<input> tel 'Telephone' empty"
    # Nothing the classifier was sent carries the value, including the history of the next step.
    assert PHONE not in client.sent()
    assert client.requests[-1]["state"]["saved_fields"] == [{"name": "phone", "typed": True}]
    assert SAVED_DATA_RULE in client.requests[0]["questions"]["kind"].instructions
    # The run folder's record of what was sent and what happened says the name, not the value.
    for name in ("state", "payload", "history", "answers"):
        for path in folder.root.glob(f"step-*-{name}.*"):
            assert PHONE not in path.read_text(), path.name


def test_a_saved_value_the_field_did_not_keep_is_cleared(tmp_path):
    browser = FakeBrowser(form_page())  # never holds what was typed
    result, _ = run(browser, Saving("phone"), tmp_path, FormData(fields={"phone": PHONE}))
    assert result.steps[0].detail == "type <phone> -> does not match: the field is empty, cleared"


def test_with_a_data_file_typing_is_offered_even_without_a_writer(tmp_path):
    client = Saving("phone")
    run(Holding(form_page()), client, tmp_path, FormData(fields={"phone": PHONE}))
    assert "type_text" in client.requests[0]["questions"]["kind"].criteria


def test_when_no_saved_field_fits_nothing_is_typed_and_no_writer_is_asked(tmp_path):
    """A writer asked for a value the file does not hold makes one up, so with saved values it is not asked."""

    class Inventing:
        def __getattr__(self, name):
            raise AssertionError("the writer was asked")

    browser = Holding(form_page())
    result, _ = run(browser, Saving("none"), tmp_path, FormData(fields={"phone": PHONE}), writer=Inventing())
    assert browser.typed == [] and result.steps[0].text_source == "no_saved_fit(none 0.90)"


def test_a_doubtful_saved_field_is_not_typed(tmp_path):
    browser = Holding(form_page())
    result, _ = run(browser, Saving("phone", confidence=0.3), tmp_path, FormData(fields={"phone": PHONE}))
    assert browser.typed == [] and result.steps[0].text_source == "no_saved_fit(phone 0.30)"


def test_a_saved_value_goes_only_into_the_field_named_never_the_first_one(tmp_path):
    """The classifier named the submit button: the value is not typed into the first field instead."""
    browser, client = Holding(form_page()), Saving("phone", element="1")
    result, _ = run(browser, client, tmp_path, FormData(fields={"phone": PHONE}))
    assert browser.typed == [] and result.steps[0].detail == "type -> not a field"
    assert client.asked_saved() == []


def test_without_a_data_file_nothing_about_saved_data_is_asked_or_said(tmp_path):
    client = FakeTypeSafe(("wait", None))
    run(FakeBrowser(form_page()), client, tmp_path, None)
    assert "saved" not in client.requests[0]["questions"] and "saved_fields" not in client.requests[0]["state"]
    assert SAVED_DATA_RULE not in client.requests[0]["questions"]["kind"].instructions


def test_a_choice_only_file_asks_no_saved_field_question(tmp_path):
    client = FakeTypeSafe(("wait", None))
    run(FakeBrowser(form_page()), client, tmp_path, FormData(choices={"size": "Medium"}))
    assert "saved" not in client.requests[0]["questions"]
    assert client.requests[0]["state"]["saved_choices"] == {"size": "Medium"}


@pytest.mark.parametrize(
    ("now", "verdict"),
    [
        ("Abebe", "matches"),
        ("ABEBE", "matches, cased by the page"),
        ("", "does not match: the field is empty"),
        ("AbebeAbebe", "does not match: the field holds more than the value"),
        ("Abe", "does not match: the field holds only part of the value"),
        ("Kebede", "does not match: the field holds something else"),
    ],
)
def test_how_a_saved_value_differs_is_said_without_either_value(now, verdict):
    from typesafe_computer_use.browser.runner import saved_match

    said = saved_match(now, "Abebe")
    assert said == verdict
    assert "Abebe" not in said and (not now or now not in said)


def _field(name, section=""):
    from typesafe_computer_use.browser.perceive import Element

    return Element(0, "input", "text", name, 0, 0, 1, 1, True, False, "", field=True, section=section)


def test_a_saved_field_named_as_the_field_is_taken_in_its_own_section():
    from typesafe_computer_use.browser.runner import saved_by_name

    saved = {
        "First Name (English)": "Dawit",
        "Guardian: First Name (English)": "Kebede",
        "Home Address *": "House 12",
    }
    assert saved_by_name(_field("First Name (English) *", "Applicant details"), saved) == "First Name (English)"
    assert saved_by_name(_field("First Name (English) *", "Guardian details"), saved) == "Guardian: First Name (English)"
    assert saved_by_name(_field("Home Address", "Main address detail"), saved) == "Home Address *"
    assert saved_by_name(_field("Gregorian date", "Applicant details"), saved) is None
    # A name with a colon and no heading to match is left to the classifier, never guessed at.
    assert saved_by_name(_field("Time: start"), {"Time: start": "9"}) is None


def test_what_the_data_settles_is_done_top_to_bottom_without_asking():
    from typesafe_computer_use.browser.perceive import Element, Page
    from typesafe_computer_use.browser.runner import next_from_data

    def el(i, name, section, **kw):
        return Element(
            i, kw.pop("tag", "input"), kw.pop("role", "text"), name, 0, 0, 1, 1, True, False, "", section=section, **kw
        )

    page = Page(
        url="https://x.test/",
        title="",
        vw=1,
        vh=1,
        elapsed_ms=0,
        raw_count=4,
        items=[
            el(0, "First Name (English) *", "Applicant details", field=True, filled=True),
            el(1, "Male", "Applicant details", role="radio", checked=False),
            el(2, "Male", "Guardian details", role="radio", checked=False),
            el(3, "First Name (English) *", "Guardian details", field=True, filled=False),
        ],
    )
    data = FormData(
        fields={"First Name (English)": "Dawit", "Guardian: First Name (English)": "Kebede"},
        choices={"Gender": "Male", "Guardian: Gender": "Male"},
    )
    tries: dict = {}
    first = next_from_data(None, page, data, tries)
    assert (first.kind, first.index, first.key) == ("click", 1, "Gender")
    assert next_from_data(None, page, data, tries).index == 1  # tried again once
    third = next_from_data(None, page, data, tries)
    assert (third.kind, third.index, third.key) == ("click", 2, "Guardian: Gender")


def test_a_card_that_opens_with_a_saved_choice_is_pressed_once():
    from typesafe_computer_use.browser.perceive import Element, Page
    from typesafe_computer_use.browser.runner import next_from_data

    card = Element(0, "div", "button", "NORMAL Service Appointment availability: T+45", 0, 0, 1, 1, True, False, "")
    other = Element(1, "div", "button", "NORMALIZE", 0, 0, 1, 1, True, False, "")
    page = Page(url="https://x.test/", title="", vw=1, vh=1, elapsed_ms=0, raw_count=2, items=[other, card])
    data = FormData(choices={"Service type": "NORMAL Service"})
    tries: dict = {}
    first = next_from_data(None, page, data, tries)
    assert (first.kind, first.index) == ("click", 0)
    assert next_from_data(None, page, data, tries) is None
    # The page still asking for that choice by name earns it one more press.
    asking = Page(
        url="https://x.test/",
        title="",
        vw=1,
        vh=1,
        elapsed_ms=0,
        raw_count=2,
        items=[other, card],
        messages=("Please select a service type",),
    )
    assert next_from_data(None, asking, data, tries).index == 0
    assert next_from_data(None, asking, data, tries) is None


def test_a_doubtful_none_waits_instead_of_ending_the_run(tmp_path):
    from test_browser_loop import FakeBrowser, Unsure, login_page, run

    result, _ = run(FakeBrowser(login_page()), Unsure(0.31, ("none", None)), tmp_path, steps=1)
    assert result.steps[0].action == "wait" and result.outcome != "blocked"


def test_none_on_an_empty_page_waits_even_when_sure(tmp_path):
    from test_browser_loop import FakeBrowser, FakeTypeSafe, login_page, run

    empty = login_page()
    empty["items"] = []
    result, _ = run(FakeBrowser(empty), FakeTypeSafe(("none", None)), tmp_path, steps=1)
    assert result.steps[0].action == "wait" and result.outcome != "blocked"


def test_a_radio_goes_to_the_saved_choice_its_group_asks_about():
    from typesafe_computer_use.browser.perceive import Element
    from typesafe_computer_use.browser.runner import saved_for_radio

    radio = Element(
        0, "input", "radio", "Ethiopia", 0, 0, 1, 1, True, False, "", checked=False, section="Applicant details", group="Born in*"
    )
    choices = {"Born in": "Ethiopia", "Country": "ETHIOPIA", "Guardian: Born in": "Ethiopia"}
    assert saved_for_radio(radio, choices) == "Born in"
    # Without the question, two choices fit and neither is taken.
    assert saved_for_radio(Element(**{**radio.__dict__, "group": ""}), choices) is None
