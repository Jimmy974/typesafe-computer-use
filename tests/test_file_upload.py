"""Saved files go into a page's file inputs, hidden or not, and a loop of one action is caught."""

from __future__ import annotations

import pytest
from test_browser_loop import FakeBrowser, FakeTypeSafe, login_page

from typesafe_computer_use.browser import act
from typesafe_computer_use.browser.runner import run_goal
from typesafe_computer_use.formdata import FormData, parse_data
from typesafe_computer_use.scenarios import load_scenarios


@pytest.fixture
def photo(tmp_path):
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"\xff\xd8\xff\xe0 not really a jpeg")
    return path


class UploadSession:
    def __init__(self, spots):
        self.spots = spots
        self.set_files: list[tuple[str, list[str]]] = []

    def evaluate(self, expression, **kwargs):
        return self.spots if expression == act.FILE_INPUTS_JS else None

    def call(self, method, params=None):
        if method == "Runtime.evaluate":
            index = params["expression"].split("[")[-1].rstrip("]")
            return {"result": {"objectId": f"input-{index}"}}
        if method == "DOM.setFileInputFiles":
            self.set_files.append((params["objectId"], params["files"]))
        return {}


def test_a_files_table_names_files_that_exist(photo, tmp_path):
    data = parse_data({"files": {"Applicant": str(photo)}})
    assert data.files == {"Applicant": str(photo.resolve())}
    with pytest.raises(ValueError, match="no file at"):
        parse_data({"files": {"Applicant": str(tmp_path / "missing.jpg")}})


def test_a_csv_file_column_is_a_file(photo, tmp_path):
    rows = tmp_path / "rows.csv"
    rows.write_text(f"name,url,goal,file:Applicant\na,https://x.test/,g,{photo}\n", encoding="utf-8")
    (scenario,) = load_scenarios(rows)
    assert scenario.data.files == {"Applicant": str(photo.resolve())}


def test_one_saved_file_goes_into_every_empty_input_that_takes_it(photo):
    session = UploadSession(
        [
            {"i": 0, "empty": True, "accept": "image/*", "about": "Applicant 0/1 Document uploaded"},
            {"i": 1, "empty": False, "accept": "", "about": "Guardian 1/1"},
            {"i": 2, "empty": True, "accept": ".pdf", "about": "Guardian ID"},
        ]
    )
    done = act.upload_files(session, {"photo": str(photo)})
    assert session.set_files == [("input-0", [str(photo)])]
    assert done == ["photo into 'Applicant 0/1 Document uploaded'"]


def test_several_saved_files_go_where_their_names_are_mentioned(photo, tmp_path):
    other = tmp_path / "id.jpg"
    other.write_bytes(b"x")
    session = UploadSession(
        [
            {"i": 0, "empty": True, "accept": "", "about": "Guardian ID copy"},
            {"i": 1, "empty": True, "accept": "", "about": "Something else"},
        ]
    )
    act.upload_files(session, {"Applicant": str(photo), "Guardian": str(other)})
    assert session.set_files == [("input-0", [str(other)])]


def test_the_run_uploads_before_it_decides(photo, monkeypatch):
    placed = []
    monkeypatch.setattr(act, "upload_files", lambda session, files, skip=None: placed.append(files) or ["photo into 'Applicant'"])
    client = FakeTypeSafe(("wait", None))
    run_goal(
        FakeBrowser(login_page()),
        client,
        "g",
        max_steps=1,
        change_timeout_ms=0,
        verbose=False,
        data=FormData(files={"photo": str(photo)}),
    )
    assert placed and "uploaded photo into 'Applicant'" in client.requests[0]["state"]["previous_actions"]


def test_the_same_action_on_the_same_page_three_times_is_a_loop(monkeypatch):
    # Each click opens or closes a section, so the page changes every time and never stalls.
    pages = [login_page(), login_page(title="Open")]
    browser = FakeBrowser(pages[0])
    real = act.observe_until_changed

    def flip(session, before, **kwargs):
        browser.page = pages[1] if browser.page is pages[0] else pages[0]
        page, ms, _ = real(session, before, **kwargs)
        return page, ms, True

    monkeypatch.setattr(act, "observe_until_changed", flip)
    client = FakeTypeSafe(*[("click", "2")] * 10)
    result = run_goal(browser, client, "g", max_steps=10, change_timeout_ms=0, verbose=False)
    assert result.outcome == "looping" and len(result.steps) == 5


def test_scrolling_a_page_that_looks_the_same_is_not_a_loop(monkeypatch):
    browser = FakeBrowser(login_page(can_scroll=True))
    real = act.observe_until_changed

    def moved(session, before, **kwargs):
        page, ms, _ = real(session, before, **kwargs)
        return page, ms, True

    monkeypatch.setattr(act, "observe_until_changed", moved)
    client = FakeTypeSafe(*[("scroll_down", None)] * 4, ("done", None))
    result = run_goal(browser, client, "g", max_steps=6, change_timeout_ms=0, verbose=False)
    assert result.outcome == "done"


def test_a_place_already_filled_or_naming_the_file_is_not_filled_again(photo):
    spots = [
        {"i": 0, "empty": True, "accept": "", "about": "Government ID"},
        {"i": 1, "empty": True, "accept": "", "about": f"Birth Certificate {photo.name}"},
    ]
    session = UploadSession(spots)
    filled: set[str] = set()
    act.upload_files(session, {"doc": str(photo)}, skip=filled)
    act.upload_files(session, {"doc": str(photo)}, skip=filled)
    assert session.set_files == [("input-0", [str(photo)])]
    assert filled == {"Government ID"}


def test_a_dialog_is_closed_as_it_opens_and_what_it_said_is_kept():
    from typesafe_computer_use.browser.cdp import answer_dialogs

    class Events:
        def __init__(self):
            self.handlers, self.sent = {}, []

        def on(self, event, handler):
            self.handlers[event] = handler

        def send(self, method, params=None):
            self.sent.append((method, params))

    session = Events()
    answer_dialogs(session)
    session.handlers["Page.javascriptDialogOpening"]({"type": "confirm", "message": "Submit now?"})
    session.handlers["Page.javascriptDialogOpening"]({"type": "alert", "message": "Saved"})
    assert session.dialogs == ["confirm: Submit now?", "alert: Saved"]
    assert [p["accept"] for _, p in session.sent] == [False, True]


def test_a_preview_of_our_own_upload_is_never_offered(photo):
    page = login_page()
    page["items"].append({**page["items"][2], "index": 3, "name": photo.name})
    client = FakeTypeSafe(("wait", None))
    monkey_files = FormData(files={"doc": str(photo)})
    run_goal(FakeBrowser(page), client, "g", max_steps=1, change_timeout_ms=0, verbose=False, data=monkey_files)
    names = [e["text"] for e in client.requests[0]["state"]["elements"]]
    assert photo.name not in names and "Sign in" in names
