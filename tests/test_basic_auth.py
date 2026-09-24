"""HTTP basic auth for one host: answered only there, and the password goes nowhere else."""

from __future__ import annotations

import json

import pytest

from typesafe_computer_use import config
from typesafe_computer_use.browser.cdp import MAX_AUTH_ANSWERS, Session, enable_basic_auth

HOST = "portal.example.test"
PASSWORD = "T0p$ecret:with-colon"


class FakeSocket:
    """Chrome's end of the websocket: replies to each call in turn, with events queued before them."""

    def __init__(self):
        self.sent: list[dict] = []
        self.inbox: list[dict] = []

    def send(self, raw: str) -> None:
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg["method"] in ("Fetch.enable", "Runtime.evaluate"):
            self.inbox.append({"id": msg["id"], "result": {}})

    def recv(self) -> str:
        return json.dumps(self.inbox.pop(0))

    def close(self) -> None:
        pass


def session() -> tuple[Session, FakeSocket]:
    s = Session.__new__(Session)
    sock = FakeSocket()
    s.ws_url, s.timeout, s._id, s._ws, s.calls = "ws://fake", 1.0, 0, sock, 0
    s._handlers, s._unawaited = {}, set()
    return s, sock


def challenge(request_id: str, url: str, origin: str | None = None, source: str = "Server") -> dict:
    return {
        "method": "Fetch.authRequired",
        "params": {
            "requestId": request_id,
            "request": {"url": url},
            "authChallenge": {"source": source, "origin": origin or url.rsplit("/", 1)[0], "scheme": "basic"},
        },
    }


def answers(sock: FakeSocket) -> dict[str, str]:
    return {
        m["params"]["requestId"]: m["params"]["authChallengeResponse"]["response"]
        for m in sock.sent
        if m["method"] == "Fetch.continueWithAuth"
    }


def test_a_call_runs_event_handlers_and_skips_replies_nobody_waits_for():
    s, sock = session()
    seen = []
    s.on("Page.loadEventFired", seen.append)
    s.send("Fetch.continueRequest", {"requestId": "r0"})  # its reply arrives later and is dropped
    sock.inbox += [{"method": "Page.loadEventFired", "params": {"t": 1}}, {"id": 1, "result": {}}]
    assert s.evaluate("1") is None
    assert seen == [{"t": 1}] and s._unawaited == set()


def test_only_this_hosts_requests_are_paused_and_each_goes_on_unchanged():
    s, sock = session()
    enable_basic_auth(s, HOST, "admin", PASSWORD)
    enable = next(m for m in sock.sent if m["method"] == "Fetch.enable")
    assert enable["params"] == {"handleAuthRequests": True, "patterns": [{"urlPattern": f"https://{HOST}/*"}]}
    sock.inbox += [{"method": "Fetch.requestPaused", "params": {"requestId": "p1", "request": {"url": f"https://{HOST}/app.js"}}}]
    s.evaluate("1")
    assert {"method": "Fetch.continueRequest", "params": {"requestId": "p1"}} in [
        {k: m[k] for k in ("method", "params")} for m in sock.sent
    ]


def test_credentials_go_only_to_the_host_over_https_for_a_server_challenge():
    s, sock = session()
    enable_basic_auth(s, HOST, "admin", PASSWORD)
    sock.inbox += [
        challenge("mine", f"https://{HOST}/"),
        challenge("other-host", "https://cdn.example.test/font.woff"),
        challenge("lookalike", f"https://{HOST}.evil.test/"),
        challenge("plain-http", f"http://{HOST}/"),
        challenge("proxy", f"https://{HOST}/", source="Proxy"),
        challenge("mismatched-origin", f"https://{HOST}/", origin="https://other.test"),
    ]
    s.evaluate("1")
    assert answers(sock) == {
        "mine": "ProvideCredentials",
        "other-host": "CancelAuth",
        "lookalike": "CancelAuth",
        "plain-http": "CancelAuth",
        "proxy": "CancelAuth",
        "mismatched-origin": "CancelAuth",
    }
    # The password is in the one answer that provides it, and in no other message.
    carrying = [m for m in sock.sent if PASSWORD in json.dumps(m)]
    assert [m["params"]["requestId"] for m in carrying] == ["mine"]


def test_a_wrong_password_is_not_answered_forever():
    s, sock = session()
    enable_basic_auth(s, HOST, "admin", "wrong")
    sock.inbox += [challenge(f"c{i}", f"https://{HOST}/") for i in range(MAX_AUTH_ANSWERS + 2)]
    s.evaluate("1")
    given = list(answers(sock).values())
    assert given.count("ProvideCredentials") == MAX_AUTH_ANSWERS and given[-1] == "CancelAuth"


def test_the_setting_reads_host_user_and_a_password_with_colons_and_dollars(monkeypatch):
    monkeypatch.setenv("CLICKER_BASIC_AUTH", f"  Portal.Example.Test admin:{PASSWORD} ")
    assert config.basic_auth() == (HOST, "admin", PASSWORD)
    monkeypatch.delenv("CLICKER_BASIC_AUTH")
    assert config.basic_auth() is None


@pytest.mark.parametrize(
    "raw",
    [
        "https://portal.example.test admin:pw",
        "portal.example.test/path admin:pw",
        "portal.example.test admin",
        "portal.example.test :pw",
        "portal.example.test",
    ],
)
def test_a_malformed_setting_is_an_error(monkeypatch, raw):
    monkeypatch.setenv("CLICKER_BASIC_AUTH", raw)
    with pytest.raises(ValueError, match="CLICKER_BASIC_AUTH"):
        config.basic_auth()
