"""Native dropdown lists are chosen in, not clicked, and a form in its own scrolling box can be scrolled."""

from __future__ import annotations

from types import SimpleNamespace

from test_browser_loop import FakeBrowser, FakeTypeSafe, choice, item, login_page

from typesafe_computer_use.browser.decide import available_actions, base_state
from typesafe_computer_use.browser.perceive import perceive
from typesafe_computer_use.browser.runner import run_goal

LANGUAGES = [[1, "English"], [2, "Amharic"]]


def form_page(**extra):
    page = login_page(url="https://example.test/apply", title="Apply")
    page["items"] = [
        item(0, "Quit Application", tag="button", field=False),
        item(1, "Select language", tag="select", field=False),
    ]
    page.update(count=2, fields=0, **extra)
    return page


class SelectBrowser(FakeBrowser):
    """A page with one <select>: lists its options and records the one set."""

    def __init__(self, page):
        super().__init__(page)
        self.chosen: list[int] = []

    def evaluate(self, expression, **kwargs):
        if "[...el.options]" in expression:
            return LANGUAGES
        if "HTMLSelectElement.prototype" in expression:
            option = int(expression.split("el.options[")[1].split("]")[0])
            self.chosen.append(option)
            return True
        return super().evaluate(expression, **kwargs)


class OptionTypeSafe(FakeTypeSafe):
    def __init__(self, *steps, option: str = "1", confidence: float = 0.9):
        super().__init__(*steps)
        self.option = option
        self.option_confidence = confidence

    def system_one(self, *, state, questions, model=None):
        if set(questions) == {"option"}:
            self.requests.append({"state": state, "questions": questions})
            return SimpleNamespace(answers={"option": choice(self.option, self.option_confidence)})
        return super().system_one(state=state, questions=questions, model=model)


def test_select_option_is_offered_only_with_a_dropdown_list():
    assert "select_option" in available_actions(perceive(FakeBrowser(form_page())))
    assert "select_option" not in available_actions(perceive(FakeBrowser(login_page())))


def test_scrolling_is_offered_when_controls_sit_below_the_fold_of_a_boxed_form():
    boxed = perceive(FakeBrowser(form_page(can_scroll=False, below_fold=3)))
    assert "scroll_down" in available_actions(boxed)
    flat = perceive(FakeBrowser(form_page(can_scroll=False, below_fold=0)))
    assert "scroll_down" not in available_actions(flat)


def test_the_option_the_classifier_names_is_set_and_said(tmp_path):
    browser = SelectBrowser(form_page())
    client = OptionTypeSafe(("select_option", "1"))

    result = run_goal(browser, client, "apply in English", max_steps=2, change_timeout_ms=0, verbose=False)

    assert browser.chosen == [1]
    assert result.steps[0].detail == "select [1] 'Select language' = 'English' (of: English | Amharic)"
    asked = next(r for r in client.requests if "option" in r["questions"])
    assert asked["questions"]["option"].criteria == {"1": "English", "2": "Amharic"}


def test_a_doubtful_option_is_not_set(tmp_path):
    browser = SelectBrowser(form_page())
    client = OptionTypeSafe(("select_option", "1"), confidence=0.1)

    result = run_goal(browser, client, "apply in English", max_steps=1, change_timeout_ms=0, verbose=False)

    assert browser.chosen == []
    assert "no option fits" in result.steps[0].detail
    assert result.steps[0].detail.endswith("options: English | Amharic")


def test_select_option_on_something_that_is_not_a_list_does_nothing(tmp_path):
    browser = SelectBrowser(form_page())
    client = OptionTypeSafe(("select_option", "0"))

    result = run_goal(browser, client, "apply in English", max_steps=1, change_timeout_ms=0, verbose=False)

    assert browser.chosen == []
    assert result.steps[0].detail == "select 0 -> not a dropdown list"


def test_a_saved_choice_named_as_the_list_is_set_without_asking(tmp_path):
    from typesafe_computer_use.formdata import FormData

    browser = SelectBrowser(form_page())
    client = OptionTypeSafe(("wait", None))
    data = FormData(fields={}, choices={"Select language": "amharic"})

    result = run_goal(browser, client, "apply", max_steps=1, change_timeout_ms=0, verbose=False, data=data)

    assert browser.chosen == [2]
    assert client.requests == []
    assert result.steps[0].action == "select_option" and result.steps[0].confidence == 1.0


def test_a_saved_choice_under_another_name_reaches_the_dropdown_question(tmp_path):
    from typesafe_computer_use.formdata import FormData

    browser = SelectBrowser(form_page())
    client = OptionTypeSafe(("select_option", "1"), option="2")
    data = FormData(fields={}, choices={"Language": "Amharic"})

    run_goal(browser, client, "apply", max_steps=1, change_timeout_ms=0, verbose=False, data=data)

    asked = next(r for r in client.requests if "option" in r["questions"])
    assert asked["state"]["saved_choices"] == {"Language": "Amharic"}
    assert "saved_choices" in asked["questions"]["option"].instructions
    assert browser.chosen == [2]


def test_the_option_a_list_shows_reaches_the_classifier():
    page = form_page()
    page["items"][1]["chosen"] = "Government identification number"
    state = base_state("apply", perceive(FakeBrowser(page)), [], url_catalog=None)
    listed = next(e for e in state["elements"] if e["tag"] == "select")
    assert listed["chosen"] == "Government identification number"


def test_a_list_with_nothing_chosen_says_so():
    element = perceive(FakeBrowser(form_page())).items[1]
    assert element.chosen is None and "nothing chosen" in element.label()


def test_choosing_an_option_counts_as_the_page_changing():
    from typesafe_computer_use.browser import act

    before = perceive(FakeBrowser(form_page()))
    page = form_page()
    page["items"][1]["chosen"] = "English"
    assert act.fingerprint(before) != act.fingerprint(perceive(FakeBrowser(page)))


def test_choosing_in_a_text_field_is_typing_into_it(tmp_path):
    from test_browser_loop import FakeWriter

    page = form_page()
    page["items"].append(item(2, "Birth certificate number", role="text", filled=False))
    page.update(fields=1)
    browser = SelectBrowser(page)
    browser.values = {0: "1990004512"}
    client = OptionTypeSafe(("select_option", "2"))

    result = run_goal(
        browser, client, "apply", max_steps=1, change_timeout_ms=0, verbose=False, writer=FakeWriter({"text": "1990004512"})
    )

    assert result.steps[0].action == "type_text"
    assert browser.chosen == []


def test_a_saved_value_missing_from_the_list_is_said_not_guessed(tmp_path):
    from typesafe_computer_use.formdata import FormData

    browser = SelectBrowser(form_page())
    client = OptionTypeSafe(("select_option", "1"), option="none")
    data = FormData(fields={}, choices={"Select language": "Oromo"})

    result = run_goal(browser, client, "apply", max_steps=1, change_timeout_ms=0, verbose=False, data=data)

    assert browser.chosen == []
    assert result.steps[0].detail == "select [1] -> the saved value is not among the options: English | Amharic"
    asked = next(r for r in client.requests if "option" in r["questions"])
    assert "none" in asked["questions"]["option"].criteria
