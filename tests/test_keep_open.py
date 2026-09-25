"""--keep-open leaves the headed browser up after a loop run, until the person is done with it."""

from __future__ import annotations

import io

import pytest

from typesafe_computer_use.browser import bench


class FakeProc:
    def __init__(self, polls_until_quit: int):
        self.left = polls_until_quit

    def poll(self):
        self.left -= 1
        return 0 if self.left <= 0 else None


class FakeChrome:
    def __init__(self, proc):
        self.proc = proc


def test_without_a_terminal_it_waits_until_chrome_is_quit(monkeypatch):
    monkeypatch.setattr(bench.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(bench.time, "sleep", lambda _: None)
    proc = FakeProc(polls_until_quit=3)

    bench.wait_for_close(FakeChrome(proc))

    assert proc.left == 0


def test_from_a_terminal_enter_closes_it(monkeypatch):
    stdin = io.StringIO("\n")
    stdin.isatty = lambda: True
    monkeypatch.setattr(bench.sys, "stdin", stdin)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt="": prompts.append(prompt) or "")

    bench.wait_for_close(FakeChrome(FakeProc(polls_until_quit=99)))

    assert prompts and "Enter" in prompts[0]


def test_keep_open_is_offered_on_loop(capsys):
    with pytest.raises(SystemExit):
        bench.main(["loop", "--help"])
    assert "--keep-open" in capsys.readouterr().out
