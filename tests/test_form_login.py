"""Host-scoped form login stays out of classifier state and run artifacts."""

from __future__ import annotations

import json

import pytest

from typesafe_computer_use import config
from typesafe_computer_use.browser import form_login, runner
from typesafe_computer_use.browser.perceive import INTERACTIVE_JS
from typesafe_computer_use.browser.report import RunFolder
from typesafe_computer_use.browser.runner import run_goal

HOST = "eth-portal2-preprod.dev.hk.toppangravity.com"
USERNAME = "synthetic-test-user"
PASSWORD = "synthetic-test-password-canary"


class FakeSession:
    def __init__(
        self, url: str, state: str | list[str] = "login_form", submit_result: str = "submitted", items: list | None = None
    ):
        self.url = url
        self.items = items or []
        self.states = list(state) if isinstance(state, tuple | list) else [state]
        self.submit_result = submit_result
        self.expressions: list[str] = []

    def evaluate(self, expression, **kwargs):
        self.expressions.append(expression)
        if expression == "location.href":
            return self.url
        if expression == form_login.LOGIN_PAGE_STATE_JS:
            return self.states.pop(0) if len(self.states) > 1 else self.states[0]
        if "requestSubmit()" in expression:
            return self.submit_result
        if expression is INTERACTIVE_JS:
            return {
                "url": self.url,
                "title": "Portal",
                "vw": 1200,
                "vh": 800,
                "count": len(self.items),
                "items": self.items,
                "fields": 0,
            }
        if expression == "document.readyState":
            return "complete"
        return None

    def call(self, method, params=None):
        return {}


class FakeClient:
    def __init__(self):
        self.requests = []


def test_form_login_names_its_host_and_both_credentials(monkeypatch):
    monkeypatch.setenv("CLICKER_FORM_LOGIN", f"{HOST.upper()} {USERNAME}:{PASSWORD}:with:colons")
    assert config.form_login() == (HOST, USERNAME, f"{PASSWORD}:with:colons")

    monkeypatch.setenv("CLICKER_FORM_LOGIN", "")
    assert config.form_login() is None


@pytest.mark.parametrize(
    "raw", [f"{HOST} {USERNAME}", f"{HOST} :{PASSWORD}", f"https://{HOST} {USERNAME}:{PASSWORD}", f"{USERNAME}:{PASSWORD}"]
)
def test_malformed_form_login_fails_without_showing_values(monkeypatch, raw):
    monkeypatch.setenv("CLICKER_FORM_LOGIN", raw)
    with pytest.raises(ValueError) as exc:
        config.form_login()
    assert "CLICKER_FORM_LOGIN" in str(exc.value)
    assert PASSWORD not in str(exc.value)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://{HOST}/login", True),
        (f"https://{HOST}:443/login", True),
        (f"http://{HOST}/login", False),
        (f"https://{HOST}.evil.test/login", False),
        ("https://evil.test/", False),
        (f"https://user@{HOST}/login", False),
    ],
)
def test_form_login_is_bound_to_the_exact_https_host(url, expected):
    assert form_login.is_login_url(url, HOST) is expected


def test_mfa_or_captcha_requires_manual_input_before_any_credentials_are_sent():
    session = FakeSession(f"https://{HOST}/verify", state="manual_required")
    assert form_login.auto_login(session, HOST, USERNAME, PASSWORD) == "manual_required"
    assert not any("requestSubmit()" in expression for expression in session.expressions)
    assert PASSWORD not in repr(session.expressions)


def test_non_login_page_is_left_alone():
    session = FakeSession(f"https://{HOST}/home", state="not_needed")
    assert form_login.auto_login(session, HOST, USERNAME, PASSWORD) == "not_needed"
    assert not any("requestSubmit()" in expression for expression in session.expressions)


def test_recognized_login_form_is_submitted_with_credentials_without_returning_them():
    session = FakeSession(f"https://{HOST}/login")
    result = form_login.auto_login(session, HOST, USERNAME, PASSWORD)
    assert result == "submitted"
    submitted = [expression for expression in session.expressions if "requestSubmit()" in expression]
    assert len(submitted) == 1
    assert json.dumps(USERNAME) in submitted[0]
    assert json.dumps(PASSWORD) in submitted[0]
    assert USERNAME not in result and PASSWORD not in result


def test_spa_login_can_finish_after_the_submit_returns():
    session = FakeSession(f"https://{HOST}/login", state=["login_form", "not_needed"])
    assert form_login.auto_login(session, HOST, USERNAME, PASSWORD) == "submitted"
    assert form_login.wait_for_login_result(session, HOST, timeout_ms=0) == "not_needed"


def test_other_host_never_receives_the_credentials():
    session = FakeSession("https://other.example.test/login")
    assert form_login.auto_login(session, HOST, USERNAME, PASSWORD) == "outside_target"
    assert not any("requestSubmit()" in expression for expression in session.expressions)


def test_mfa_stops_the_runner_before_classifier_or_run_artifacts_can_see_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "form_login", lambda: (HOST, USERNAME, PASSWORD))
    session = FakeSession(f"https://{HOST}/verify", state="manual_required")
    client = FakeClient()
    folder = RunFolder.create(tmp_path)

    result = run_goal(session, client, "continue portal workflow", verbose=False, runfolder=folder)

    assert result.outcome == "form_login_manual_required"
    assert client.requests == []
    artifacts = "\n".join(path.read_text() for path in folder.root.iterdir())
    assert USERNAME not in artifacts
    assert PASSWORD not in artifacts


def test_successful_automatic_login_keeps_credentials_out_of_run_artifacts(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "form_login", lambda: (HOST, USERNAME, PASSWORD))
    session = FakeSession(f"https://{HOST}/login", state=["login_form", "not_needed"])
    client = FakeClient()
    folder = RunFolder.create(tmp_path)

    result = run_goal(session, client, "continue portal workflow", max_steps=0, verbose=False, runfolder=folder)

    assert result.outcome == "max_steps"
    assert any("requestSubmit()" in expression for expression in session.expressions)
    artifacts = "\n".join(path.read_text() for path in folder.root.iterdir())
    assert USERNAME not in artifacts
    assert PASSWORD not in artifacts


def test_manual_mfa_checkpoint_resumes_the_automated_run(monkeypatch):
    monkeypatch.setattr(config, "form_login", lambda: (HOST, USERNAME, PASSWORD))
    session = FakeSession(f"https://{HOST}/login", state=["login_form", "manual_required", "not_needed"])
    client = FakeClient()
    prompts = []

    result = run_goal(
        session,
        client,
        "continue portal workflow",
        max_steps=0,
        verbose=False,
        manual_login=lambda: prompts.append("manual") or True,
    )

    assert result.outcome == "max_steps"
    assert prompts == ["manual"]


def test_manual_captcha_checkpoint_then_automatic_submit(monkeypatch):
    monkeypatch.setattr(config, "form_login", lambda: (HOST, USERNAME, PASSWORD))
    session = FakeSession(
        f"https://{HOST}/login",
        state=["manual_required", "login_form", "login_form", "not_needed"],
    )
    client = FakeClient()

    result = run_goal(
        session,
        client,
        "continue portal workflow",
        max_steps=0,
        verbose=False,
        manual_login=lambda: True,
    )

    assert result.outcome == "max_steps"
    assert any("requestSubmit()" in expression for expression in session.expressions)


PASSWORD_BOX = {"index": 0, "tag": "input", "role": "textbox", "name": "Password", "field": True, "secret": True}


def test_login_form_that_opens_mid_run_is_signed_in_by_the_form_login_path(monkeypatch):
    # The first page has no form; a Login button then opens one on the same page.
    monkeypatch.setattr(config, "form_login", lambda: (HOST, USERNAME, PASSWORD))
    session = FakeSession(
        f"https://{HOST}/", state=["not_needed", "login_form", "login_form", "not_needed"], items=[PASSWORD_BOX]
    )
    decided = []

    def fake_decide(*args, **kwargs):
        decided.append(True)
        raise RuntimeError("stop after the login")

    monkeypatch.setattr(runner, "decide", fake_decide)

    with pytest.raises(RuntimeError, match="stop after the login"):
        run_goal(session, FakeClient(), "log in and continue", max_steps=1, verbose=False)

    assert any("requestSubmit()" in expression for expression in session.expressions)
    assert decided == [True]


def test_login_form_mid_run_that_needs_a_person_stops_before_the_classifier(monkeypatch):
    monkeypatch.setattr(config, "form_login", lambda: (HOST, USERNAME, PASSWORD))
    session = FakeSession(f"https://{HOST}/", state=["not_needed", "manual_required"], items=[PASSWORD_BOX])
    client = FakeClient()

    result = run_goal(session, client, "log in and continue", max_steps=3, verbose=False)

    assert result.outcome == "form_login_manual_required"
    assert client.requests == []
    assert not any("requestSubmit()" in expression for expression in session.expressions)


def test_any_configured_host_gets_its_own_form_login():
    session = FakeSession("https://login.example.test/sign-in")
    assert form_login.auto_login(session, "login.example.test", USERNAME, PASSWORD) == "submitted"
    submitted = [expression for expression in session.expressions if "requestSubmit()" in expression]
    assert json.dumps("login.example.test") in submitted[0]
    assert (
        form_login.auto_login(FakeSession(f"https://{HOST}/login"), "login.example.test", USERNAME, PASSWORD) == "outside_target"
    )


def test_a_form_that_needs_a_person_says_why_in_the_outcome(monkeypatch):
    monkeypatch.setattr(config, "form_login", lambda: (HOST, USERNAME, PASSWORD))
    session = FakeSession(f"https://{HOST}/", state=["not_needed", "manual_required:password_fields_2"], items=[PASSWORD_BOX])

    result = run_goal(session, FakeClient(), "log in and continue", max_steps=3, verbose=False)

    assert result.outcome == "form_login_manual_required(password_fields_2)"


@pytest.mark.parametrize(
    "raw", ["manual_required:" + PASSWORD, "manual_required:captcha extra", "manual_required:password_fields_999"]
)
def test_a_reason_the_script_did_not_write_never_reaches_the_log(raw):
    session = FakeSession(f"https://{HOST}/login", state=raw)
    assert form_login.inspect_login_page(session, HOST) == "manual_required"


def test_fields_switched_off_while_signing_in_are_waited_on_not_taken_for_success():
    session = FakeSession(f"https://{HOST}/login", state=["busy", "busy", "login_form"])
    assert form_login.wait_for_login_result(session, HOST, timeout_ms=0) == "login_failed"

    session = FakeSession(f"https://{HOST}/login", state=["busy", "not_needed"])
    assert form_login.wait_for_login_result(session, HOST, timeout_ms=1000, poll_ms=1) == "not_needed"


CODE = "123456"


class OtpSession(FakeSession):
    """A page that asks for a one-time code until one is sent, then leaves the code boxes behind."""

    def __init__(self, url: str, otp_states: list[str], submit: str = "submitted"):
        super().__init__(url, state="not_needed")
        self.otp_states = otp_states
        self.otp_submit = submit

    def evaluate(self, expression, **kwargs):
        if "otp_form" in expression and "async function(host, code)" in expression:
            self.expressions.append(expression)
            return self.otp_submit
        if "otp_form" in expression:
            self.expressions.append(expression)
            return self.otp_states.pop(0) if len(self.otp_states) > 1 else self.otp_states[0]
        return super().evaluate(expression, **kwargs)


def test_fixed_code_is_read_only_with_its_login_host(monkeypatch):
    monkeypatch.setenv("CLICKER_FORM_OTP", CODE)
    monkeypatch.setenv("CLICKER_FORM_LOGIN", f"{HOST} {USERNAME}:{PASSWORD}")
    assert config.form_otp() == CODE

    monkeypatch.setenv("CLICKER_FORM_LOGIN", "")
    with pytest.raises(ValueError, match="CLICKER_FORM_LOGIN"):
        config.form_otp()


@pytest.mark.parametrize("raw", ["123", "12 34 56", "\uff11\uff12\uff13\uff14\uff15\uff16", "12345678901"])
def test_a_malformed_fixed_code_is_refused(monkeypatch, raw):
    monkeypatch.setenv("CLICKER_FORM_LOGIN", f"{HOST} {USERNAME}:{PASSWORD}")
    monkeypatch.setenv("CLICKER_FORM_OTP", raw)
    with pytest.raises(ValueError, match="CLICKER_FORM_OTP"):
        config.form_otp()


def test_the_code_is_sent_only_to_the_page_that_asks_for_it():
    session = OtpSession(f"https://{HOST}/auth", otp_states=["otp_form"])
    assert form_login.auto_otp(session, HOST, CODE) == "submitted"
    looks = [e for e in session.expressions if "async function(host, code)" not in e and "otp_form" in e]
    assert looks and all(CODE not in e for e in looks)

    elsewhere = OtpSession("https://other.example.test/auth", otp_states=["otp_form"])
    assert form_login.auto_otp(elsewhere, HOST, CODE) == "outside_target"
    assert not any("async function(host, code)" in e for e in elsewhere.expressions)


def test_the_run_types_the_code_and_keeps_it_out_of_run_artifacts(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "form_login", lambda: (HOST, USERNAME, PASSWORD))
    monkeypatch.setattr(config, "form_otp", lambda: CODE)
    session = OtpSession(f"https://{HOST}/auth", otp_states=["otp_form", "not_needed"])
    folder = RunFolder.create(tmp_path)
    histories = []

    def fake_decide(goal, page, history):
        histories.append(list(history))
        raise RuntimeError("stop after the code")

    monkeypatch.setattr(runner, "decide", lambda client, goal, page, history, **kw: fake_decide(goal, page, history))

    with pytest.raises(RuntimeError, match="stop after the code"):
        run_goal(session, FakeClient(), "sign in", max_steps=1, verbose=False, runfolder=folder)

    assert histories == [["form otp: typed"]]
    assert all(CODE not in str(h) for h in histories)
    artifacts = "\n".join(path.read_text() for path in folder.root.iterdir())
    assert CODE not in artifacts


def test_a_code_the_site_turns_down_stops_the_run(monkeypatch):
    monkeypatch.setattr(config, "form_login", lambda: (HOST, USERNAME, PASSWORD))
    monkeypatch.setattr(config, "form_otp", lambda: CODE)
    session = OtpSession(f"https://{HOST}/auth", otp_states=["otp_form"])

    result = run_goal(session, FakeClient(), "sign in", max_steps=3, verbose=False)

    assert result.outcome == "form_otp_failed"
