"""The browser step loop, end to end, with Chrome faked at the session boundary.

No Chrome, no network, no API key. `FakeBrowser` answers the JavaScript the backend sends
from a page it holds, the way a page would, and records every input event; `FakeTypeSafe`
answers the classifier from a script. So these tests run the real `run_goal`, `decide`,
`perceive` and run folder, and assert on what reached the classifier, the writer, the page
and the disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from typesafe_sdk import Choice, ChoiceAnswer, Noul, NoulAnswer

from typesafe_computer_use.browser.decide import base_state, decide, element_criteria
from typesafe_computer_use.browser.perceive import INTERACTIVE_JS, perceive, to_json
from typesafe_computer_use.browser.report import RunFolder, load_step
from typesafe_computer_use.browser.runner import run_goal, typing_target

SECRET = "hunter2-do-not-leak"


def item(index, name, tag="input", **kwargs):
    base = {
        "index": index,
        "tag": tag,
        "role": "",
        "name": name,
        "x": 10,
        "y": 20 + 30 * index,
        "w": 200,
        "h": 24,
        "in_view": True,
        "covered": False,
        "href": "",
        "field": tag in {"input", "textarea"},
        "secret": False,
    }
    base.update(kwargs)
    return base


def login_page(**extra):
    """A sign-in form. The password field carries a value, as the PR's first version of the
    page script reported it, so a test can see whether any of it travels further."""
    items = [
        item(0, "Username", role="text"),
        item(1, "Password", role="password", secret=True, value=SECRET),
        item(2, "Sign in", tag="button", role="submit", field=False),
    ]
    return {
        "url": "https://example.test/login",
        "title": "Sign in",
        "vw": 1200,
        "vh": 800,
        "count": len(items),
        "items": items,
        "scroll_y": 0,
        "scroll_max": 0,
        "candidates": 3,
        "below_fold": 0,
        "can_scroll": False,
        "history_len": 1,
        "fields": 1,
        **extra,
    }


class FakeBrowser:
    """A page behind a CDP session: answers the page script, focus and field reads, and
    records every Input event the loop sends."""

    def __init__(self, page: dict, *, values: dict[int, str] | None = None):
        self.page = page
        self.values = values or {}
        self.inputs: list[tuple[str, dict]] = []
        self.navigations: list[str] = []

    def evaluate(self, expression, **kwargs):
        if expression is INTERACTIVE_JS:
            return json.loads(json.dumps(self.page))
        if expression == "location.href":
            return self.page["url"]
        if expression == "document.readyState":
            return "complete"
        if "el.focus()" in expression:
            return True
        if "String(el.value)" in expression:
            return self.values.get(0, "")
        return None

    def call(self, method, params=None):
        if method == "Page.navigate":
            self.navigations.append(params["url"])
        else:
            self.inputs.append((method, params or {}))
        return {}

    @property
    def typed(self) -> list[str]:
        return [p["text"] for m, p in self.inputs if m == "Input.insertText"]


def choice(key: str, confidence: float = 0.9) -> ChoiceAnswer:
    return ChoiceAnswer(choice=key, probabilities={key: confidence}, confidence=confidence)


class FakeTypeSafe:
    """The classifier, from a script of (kind, element) pairs, one per step, then done."""

    def __init__(self, *steps: tuple[str, str | None]):
        self.steps = list(steps)
        self.requests: list[dict] = []

    def system_one(self, *, state, questions, model=None):
        self.requests.append({"state": state, "questions": questions})
        if set(questions) == {"ok"}:  # verify_typed
            return SimpleNamespace(answers={"ok": NoulAnswer(noul=0.95)})
        kind, element = self.steps.pop(0) if self.steps else ("done", None)
        answers = {"kind": choice(kind), "satisfied": NoulAnswer(noul=0.0)}
        if "element" in questions:
            answers["element"] = choice(element or "0")
        return SimpleNamespace(answers=answers)

    def sent(self) -> str:
        """Everything the classifier was sent, as text."""
        return json.dumps(
            [{"state": r["state"], "questions": {k: repr(q) for k, q in r["questions"].items()}} for r in self.requests],
            default=str,
        )


class FakeWriter:
    """The Anthropic client, stubbed where `_structured` calls it. Records every request."""

    def __init__(self, reply: dict):
        self.reply = reply
        self.requests: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        block = SimpleNamespace(type="text", text=json.dumps(self.reply))
        return SimpleNamespace(content=[block])


def run(browser, client, tmp_path: Path, writer=None, steps: int = 3):
    folder = RunFolder.create(tmp_path)
    result = run_goal(
        browser,
        client,
        "sign in as alice",
        max_steps=steps,
        change_timeout_ms=0,
        verbose=False,
        writer=writer,
        runfolder=folder,
    )
    return result, folder


def folder_text(folder: RunFolder) -> str:
    return "\n".join(p.read_text() for p in sorted(folder.root.iterdir()))


# ------------------------------------------------------------ password values
def test_a_password_value_never_leaves_the_page(tmp_path):
    """Whatever the page script hands back, a field's value reaches nothing: not the element,
    its label, the classifier's state or criteria, the run folder, or the step log."""
    browser = FakeBrowser(login_page())
    client = FakeTypeSafe(("click", "1"))
    result, folder = run(browser, client, tmp_path)

    page = perceive(FakeBrowser(login_page()))
    assert SECRET not in repr(page.items)
    assert SECRET not in to_json(page)
    assert SECRET not in json.dumps(element_criteria(page))
    assert SECRET not in json.dumps(base_state("g", page, [], url_catalog=None))
    assert SECRET not in client.sent()
    assert SECRET not in folder_text(folder)
    assert SECRET not in "\n".join(s.line() for s in result.steps)


def test_the_page_script_never_reads_what_was_typed():
    """The script reads `el.value` twice: for a button input's label, and to say whether a field
    holds anything, as one boolean. It never names a text control after its own contents."""
    assert INTERACTIVE_JS.count("el.value") == 2
    assert "BUTTON_TYPES.has(type) ? el.value" in INTERACTIVE_JS
    assert 'typedInto && !secret ? String(el.value ?? el.innerText ?? "").length > 0 : null' in INTERACTIVE_JS
    assert "value:" not in INTERACTIVE_JS


def test_a_credential_field_is_marked_in_what_the_classifier_reads():
    page = perceive(FakeBrowser(login_page()))
    assert "credential field" in element_criteria(page)["1"]
    assert base_state("g", page, [], url_catalog=None)["elements"][1]["credential_field"] is True


# ------------------------------------------------------------- typing guard
def test_no_writer_offers_no_typing_and_types_nothing(tmp_path):
    browser = FakeBrowser(login_page())
    client = FakeTypeSafe(("type_text", "0"))
    result, _ = run(browser, client, tmp_path)

    offered = client.requests[0]["questions"]["kind"].criteria
    assert "type_text" not in offered and "navigate" not in offered
    assert browser.typed == []
    assert result.steps[0].text_source == "no_writer"


def test_the_named_password_field_is_refused_before_the_writer_is_asked(tmp_path):
    browser = FakeBrowser(login_page())
    client = FakeTypeSafe(("type_text", "1"))
    writer = FakeWriter({"fill": True, "text": "hunter2", "reason": "the goal"})
    result, folder = run(browser, client, tmp_path, writer=writer, steps=1)

    assert browser.typed == []
    assert writer.requests == []
    assert result.steps[0].text_source == "refused_credential"
    assert json.loads((folder.root / "run.json").read_text())["steps"][0]["text_source"] == "refused_credential"


def test_a_password_field_is_never_the_fallback_target():
    """With no field named, typing goes to the first field that is not a credential field,
    and a page whose only field asks for a password gets nothing."""
    page = perceive(FakeBrowser(login_page()))
    target, _ = typing_target(page, None)
    assert target is not None and target.name == "Username"

    only_password = login_page()
    only_password["items"] = [only_password["items"][1]]
    target, why = typing_target(perceive(FakeBrowser(only_password)), None)
    assert target is None and why == "no field"


def test_a_field_labelled_like_a_credential_is_refused_even_when_not_a_password_input():
    page = login_page()
    page["items"][0] = item(0, "One-time code", role="text")
    target, why = typing_target(perceive(FakeBrowser(page)), 0)
    assert target is None and why == "refused_credential"


def test_the_writer_types_into_an_ordinary_field_and_the_step_says_so(tmp_path):
    browser = FakeBrowser(login_page(), values={0: "alice"})
    client = FakeTypeSafe(("type_text", "0"))
    writer = FakeWriter({"fill": True, "text": "alice", "reason": "the goal"})
    result, folder = run(browser, client, tmp_path, writer=writer, steps=1)

    assert browser.typed == ["alice"]
    assert result.steps[0].text_source == "writer"
    assert "text=writer" in result.steps[0].line()
    assert load_step(folder.root, 1)["can_write"] is True


def test_navigate_opens_only_the_writers_https_address(tmp_path):
    browser = FakeBrowser(login_page())
    client = FakeTypeSafe(("navigate", None))
    writer = FakeWriter({"ok": True, "url": "https://example.test/", "reason": "the site"})
    result, _ = run(browser, client, tmp_path, writer=writer, steps=1)
    assert browser.navigations == ["https://example.test/"]
    assert result.steps[0].text_source == "writer"

    browser = FakeBrowser(login_page())
    writer = FakeWriter({"ok": True, "url": "file:///etc/passwd", "reason": "no"})
    result, _ = run(browser, FakeTypeSafe(("navigate", None)), tmp_path, writer=writer, steps=1)
    assert browser.navigations == []
    assert result.steps[0].text_source == "writer_declined"


# ---------------------------------------------------------------- decide
def test_decide_survives_an_answer_without_a_satisfied_noul():
    """The fallback for a missing `satisfied` answer builds a NoulAnswer the SDK accepts."""

    class Partial(FakeTypeSafe):
        def system_one(self, *, state, questions, model=None):
            assert isinstance(questions["kind"], Choice) and isinstance(questions["satisfied"], Noul)
            return SimpleNamespace(answers={"kind": choice("wait"), "satisfied": None})

    decision = decide(Partial(), "g", perceive(FakeBrowser(login_page())), [])
    assert decision.satisfied.noul == 0.0 and decision.kind.choice == "wait"


def test_a_doubtful_done_stops_as_low_confidence_not_success(tmp_path):
    class Doubtful(FakeTypeSafe):
        def system_one(self, *, state, questions, model=None):
            self.requests.append({"state": state, "questions": questions})
            return SimpleNamespace(answers={"kind": choice("done", 0.26), "satisfied": NoulAnswer(noul=0.25)})

    result, _ = run(FakeBrowser(login_page()), Doubtful(), tmp_path, steps=1)
    assert result.outcome == "low_confidence(0.26)"


def test_a_confident_done_still_finishes_the_run(tmp_path):
    result, _ = run(FakeBrowser(login_page()), FakeTypeSafe(("done", None)), tmp_path, steps=1)
    assert result.outcome == "done"


def test_the_same_click_that_changes_nothing_three_times_ends_the_run(tmp_path):
    """A click can land and do nothing: the page is the same after it. Repeating it is a stall."""
    client = FakeTypeSafe(*[("click", "2")] * 8)
    result, _ = run(FakeBrowser(login_page()), client, tmp_path, steps=8)
    assert result.outcome == "stalled" and len(result.steps) == 3


def test_different_actions_that_change_nothing_are_not_a_repeat(tmp_path):
    client = FakeTypeSafe(("click", "2"), ("scroll_down", None), ("click", "2"), ("scroll_down", None))
    result, _ = run(FakeBrowser(login_page()), client, tmp_path, steps=4)
    assert result.outcome != "stalled"


def form_page():
    """A form as a page script would report it: labelled radios, a checkbox, a filled field."""
    items = [
        item(0, "Customer name", role="text", filled=True),
        item(1, "Medium", role="radio", field=False, checked=False),
        item(2, "Bacon", role="checkbox", field=False, checked=True),
    ]
    return {**login_page(), "url": "https://example.test/form", "items": items, "count": 3}


def test_the_classifier_is_told_how_each_control_is_used_and_where_it_stands():
    state = base_state("g", perceive(FakeBrowser(form_page())), [], url_catalog=None)
    name, medium, bacon = state["elements"]
    assert (name["type"], name["filled"]) == ("text", True) and "checked" not in name
    assert (medium["type"], medium["text"], medium["checked"]) == ("radio", "Medium", False)
    assert bacon["checked"] is True and "filled" not in bacon
    assert element_criteria(perceive(FakeBrowser(form_page())))["1"] == "<input> radio 'Medium' not checked"


def test_a_credential_field_says_nothing_about_what_it_holds():
    page = login_page()
    page["items"][1]["filled"] = True  # whatever a page script claims
    password = base_state("g", perceive(FakeBrowser(page)), [], url_catalog=None)["elements"][1]
    assert "filled" not in password


def test_a_load_check_asked_mid_navigation_is_asked_again_not_a_crash():
    from typesafe_computer_use.browser import act
    from typesafe_computer_use.browser.cdp import CDPError

    class Navigating(FakeBrowser):
        asks = 0

        def evaluate(self, expression, **kwargs):
            if expression == "document.readyState":
                Navigating.asks += 1
                if Navigating.asks == 1:
                    raise CDPError("JS error: Uncaught")
            return super().evaluate(expression, **kwargs)

    act.wait_for_load(Navigating(login_page()), timeout_ms=1000, settle_ms=0)
    assert Navigating.asks == 2


class Unsure(FakeTypeSafe):
    """A classifier that is unsure of its first action, then sure of the next ones."""

    def __init__(self, first: float, *steps):
        super().__init__(*steps)
        self.first = first

    def system_one(self, *, state, questions, model=None):
        answer = super().system_one(state=state, questions=questions, model=model)
        if self.first is not None and "kind" in answer.answers:
            kind = answer.answers["kind"].choice
            answer.answers["kind"] = choice(kind, self.first)
            self.first = None
        return answer


def test_a_doubtful_click_is_held_not_carried_out(tmp_path):
    browser = FakeBrowser(login_page())
    result, _ = run(browser, Unsure(0.37, ("click", "2")), tmp_path, steps=3)

    assert result.outcome == "low_confidence(0.37)"
    assert not [p for m, p in browser.inputs if m == "Input.dispatchMouseEvent"]
    assert result.steps[0].detail == "held click (0.37), waited 0s, nothing changed"


def test_a_held_action_asks_again_when_the_page_moves_on(tmp_path, monkeypatch):
    from typesafe_computer_use.browser import runner

    browser = FakeBrowser(login_page())
    arrived = login_page(url="https://example.test/home", title="Home")
    arrived["items"] = [item(0, "Continue", tag="button", field=False)]
    real = runner.act.observe_until_changed

    def page_arrives(session, before, **kwargs):
        browser.page = arrived
        return real(session, before, **kwargs)

    monkeypatch.setattr(runner.act, "observe_until_changed", page_arrives)
    result, _ = run(browser, Unsure(0.37, ("click", "2"), ("click", "0")), tmp_path, steps=2)

    assert result.steps[0].detail.startswith("held click (0.37), waited")
    # Asked again on the new page, and this time sure: the click is carried out, not held.
    assert result.steps[1].action == "click" and not result.steps[1].detail.startswith("held")


def test_a_doubtful_scroll_is_carried_out_and_the_run_goes_on_when_the_page_moves(tmp_path, monkeypatch):
    from typesafe_computer_use.browser import runner

    browser = FakeBrowser(login_page(can_scroll=True))
    moved = login_page(can_scroll=True, scroll_y=300)
    moved["items"] = [item(0, "Next", tag="button", field=False)]
    real = runner.act.observe_until_changed

    def page_scrolls(session, before, **kwargs):
        if any(m == "Input.dispatchMouseEvent" and p.get("type") == "mouseWheel" for m, p in browser.inputs):
            browser.page = moved
            return runner.perceive(session), 0.0, True
        return real(session, before, **kwargs)

    monkeypatch.setattr(runner.act, "observe_until_changed", page_scrolls)
    result, _ = run(browser, Unsure(0.30, ("scroll_down", None), ("done", None)), tmp_path, steps=3)

    assert result.steps[0].action == "scroll_down" and not result.steps[0].detail.startswith("held")
    assert len(result.steps) == 2


def test_the_pages_own_error_line_reaches_the_classifier_and_the_run_folder(tmp_path):
    notice = "Error! Please wait 600 seconds before requesting a new code."
    browser = FakeBrowser(login_page(messages=[notice]))
    client = FakeTypeSafe(("wait", None))

    _, folder = run(browser, client, tmp_path, steps=1)

    assert client.requests[0]["state"]["page_messages"] == [notice]
    assert notice in folder_text(folder)


def test_a_quiet_page_sends_no_messages(tmp_path):
    client = FakeTypeSafe(("wait", None))
    run(FakeBrowser(login_page()), client, tmp_path, steps=1)
    assert "page_messages" not in client.requests[0]["state"]


def test_a_message_appearing_counts_as_the_page_changing():
    from typesafe_computer_use.browser import act

    before = perceive(FakeBrowser(login_page()))
    after = perceive(FakeBrowser(login_page(messages=["Invalid code, try again."])))
    assert act.fingerprint(before) != act.fingerprint(after)


def test_a_filled_field_is_never_the_fallback_target():
    page = login_page()
    page["items"] = [
        item(0, "Identification number", role="text", filled=True),
        item(1, "Father's Name", role="text", filled=False),
        item(2, "Male", role="radio", field=False),
    ]
    target, _ = typing_target(perceive(FakeBrowser(page)), 2)
    assert target is not None and target.name == "Father's Name"

    page["items"][1]["filled"] = True
    target, why = typing_target(perceive(FakeBrowser(page)), 2)
    assert target is None and why == "no field"


def test_only_steps_in_a_row_that_do_nothing_end_the_run(tmp_path):
    # A wait that sees nothing is a noop; the click between them does something, so the second
    # wait is the first of a new run of them, and the classifier then finishes.
    client = FakeTypeSafe(("wait", None), ("click", "2"), ("wait", None), ("done", None))
    result, _ = run(FakeBrowser(login_page()), client, tmp_path, steps=5)
    assert result.outcome == "done"

    client = FakeTypeSafe(("wait", None), ("wait", None), ("done", None))
    result, _ = run(FakeBrowser(login_page()), client, tmp_path, steps=5)
    assert result.outcome == "stuck"


def test_click_and_type_split_on_one_empty_field_is_typing(tmp_path):
    class Split(FakeTypeSafe):
        def system_one(self, *, state, questions, model=None):
            answer = super().system_one(state=state, questions=questions, model=model)
            if "kind" in answer.answers and len(self.requests) == 1:
                answer.answers["kind"] = ChoiceAnswer(
                    choice="type_text", probabilities={"type_text": 0.44, "click": 0.41, "scroll_down": 0.15}, confidence=0.38
                )
                answer.answers["element"] = choice("0", 0.86)
            return answer

    writer = FakeWriter({"text": "alice"})
    result, _ = run(FakeBrowser(login_page(), values={0: "alice"}), Split(), tmp_path, writer=writer, steps=1)

    assert result.steps[0].action == "type_text"
    assert not result.steps[0].detail.startswith("held")
    assert result.steps[0].confidence == 0.85


def test_a_held_action_scrolls_when_more_of_the_page_is_below(tmp_path):
    browser = FakeBrowser(login_page(can_scroll=False, below_fold=3))
    result, _ = run(browser, Unsure(0.21, ("click", "2")), tmp_path, steps=1)

    assert result.steps[0].action == "wait"
    assert result.steps[0].detail.startswith("held click (0.21), scroll down 3 lines, waited")
    assert not [p for m, p in browser.inputs if p.get("type") == "mousePressed"]


def test_a_field_the_page_marks_invalid_says_so_to_the_classifier():
    page = login_page()
    page["items"][0]["invalid"] = True
    state = base_state("sign in", perceive(FakeBrowser(page)), [], url_catalog=None)
    assert state["elements"][0]["invalid"] is True
    assert "invalid" not in state["elements"][2]


def test_a_fields_section_heading_reaches_the_classifier_and_its_label():
    page = login_page()
    page["items"][0]["section"] = "Guardian information"
    parsed = perceive(FakeBrowser(page))
    assert "under 'Guardian information'" in parsed.items[0].label()
    state = base_state("sign in", parsed, [], url_catalog=None)
    assert state["elements"][0]["section"] == "Guardian information"
    assert "section" not in state["elements"][1]


def test_an_action_the_browser_does_not_answer_is_a_failed_step_not_a_crash(tmp_path):
    from typesafe_computer_use.browser.cdp import CDPError

    class Stuck(FakeBrowser):
        def call(self, method, params=None):
            if method == "Input.dispatchMouseEvent" and (params or {}).get("type") == "mouseWheel":
                raise CDPError("Input.dispatchMouseEvent: no answer in 30s")
            return super().call(method, params)

    result, _ = run(Stuck(login_page(can_scroll=True)), FakeTypeSafe(("scroll_down", None), ("done", None)), tmp_path, steps=3)
    assert result.steps[0].detail.startswith("scroll_down -> the browser did not answer")
    assert result.outcome == "done"


def test_a_browser_that_stops_answering_ends_the_run_with_its_record(tmp_path):
    from typesafe_computer_use.browser.cdp import CDPError

    class Gone(FakeBrowser):
        """Answers where the run starts, then nothing: the page has handed over to another site."""

        asked = 0

        def evaluate(self, expression, **kwargs):
            if expression == "location.href" and not self.asked:
                self.asked += 1
                return super().evaluate(expression, **kwargs)
            if expression is INTERACTIVE_JS or expression == "location.href":
                raise CDPError("Runtime.evaluate: no answer in 30s")
            return super().evaluate(expression, **kwargs)

    session = Gone(login_page())
    result = run_goal(session, FakeTypeSafe(), "g", max_steps=2, change_timeout_ms=0, verbose=False)
    assert result.outcome == "browser_unresponsive" and result.url_after == ""


def test_the_run_is_done_once_the_page_shows_the_done_text(tmp_path):
    class Paying(FakeBrowser):
        def evaluate(self, expression, **kwargs):
            if "innerText" in expression and ".includes(" in expression:
                return "not completed until payment" in expression
            return super().evaluate(expression, **kwargs)

    client = FakeTypeSafe(*[("scroll_down", None)] * 5)
    result = run_goal(
        Paying(login_page()),
        client,
        "g",
        max_steps=5,
        change_timeout_ms=0,
        verbose=False,
        done_text="The application is  NOT completed until payment",
    )
    assert result.outcome == "done" and client.requests == []


def test_a_ticked_box_is_not_unticked_on_a_middling_answer(tmp_path):
    page = login_page()
    page["items"].append(item(3, "I Accept", role="checkbox", field=False, checked=True))
    browser = FakeBrowser(page)
    result, _ = run(browser, Unsure(0.7, ("click", "3")), tmp_path, steps=1)
    assert result.steps[0].action == "wait"
    assert not [p for m, p in browser.inputs if p.get("type") == "mousePressed"]
