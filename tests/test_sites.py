"""Site files: parsing, matching a page to its file, and what a match changes in the browser loop."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from test_browser_loop import FakeBrowser, FakeTypeSafe, login_page

from typesafe_computer_use.browser import act
from typesafe_computer_use.browser.decide import SITE_NOTES_RULE
from typesafe_computer_use.browser.report import RunFolder
from typesafe_computer_use.browser.runner import run_goal
from typesafe_computer_use.sites import Site, load_sites, parse_site, site_for

REPO_SITES = Path(__file__).resolve().parent.parent / "sites"


def write(folder: Path, name: str, text: str) -> None:
    (folder / name).write_text(text, encoding="utf-8")


def test_a_site_file_is_read_into_notes_and_a_settle_time(tmp_path):
    write(tmp_path, "example.toml", 'domain = "Example.test"\nsettle_ms = 800\nnotes = [" Tabs are links. "]\n')
    [site] = load_sites(tmp_path)
    assert (site.domain, site.notes, site.settle_ms) == ("example.test", ("Tabs are links.",), 800)


def test_no_folder_means_no_site_knowledge(tmp_path):
    assert load_sites(tmp_path / "missing") == []


@pytest.mark.parametrize(
    "data, complaint",
    [
        ({"domain": "github.com", "note": ["typo"]}, "unknown key"),
        ({"domain": "https://github.com/"}, "host name"),
        ({"domain": "github.com", "notes": "one string"}, "list of non-empty strings"),
        ({"domain": "github.com", "settle_ms": 60_000}, "settle_ms"),
        ({"domain": "github.com", "settle_ms": True}, "settle_ms"),
    ],
)
def test_a_bad_site_file_is_an_error_not_a_silent_default(data, complaint):
    with pytest.raises(ValueError, match=complaint):
        parse_site(data, "sites/bad.toml")


def test_a_page_matches_its_domain_and_subdomains_and_the_longest_domain_wins():
    github, gist = Site("github.com"), Site("gist.github.com")
    sites = [github, gist]
    assert site_for("https://github.com/awlevin/x/issues", sites) is github
    assert site_for("https://docs.github.com/en", sites) is github
    assert site_for("https://gist.github.com/someone", sites) is gist
    assert site_for("https://notgithub.com/", sites) is None
    assert site_for("file:///tmp/fixture.html", sites) is None
    assert site_for(None, sites) is None


def test_the_shipped_github_file_parses():
    [site] = [s for s in load_sites(REPO_SITES) if s.domain == "github.com"]
    assert site.notes and 0 < site.settle_ms <= 10_000


def loop(sites, tmp_path: Path) -> FakeTypeSafe:
    client = FakeTypeSafe(("wait", None))
    run_goal(
        FakeBrowser(login_page()),
        client,
        "sign in as alice",
        max_steps=1,
        change_timeout_ms=0,
        verbose=False,
        runfolder=RunFolder.create(tmp_path),
        sites=sites,
    )
    return client


def test_notes_reach_the_classifier_only_on_their_own_site(tmp_path):
    here = loop([Site("example.test", notes=("Sign in is the button under the form.",))], tmp_path / "a")
    state, kind = here.requests[0]["state"], here.requests[0]["questions"]["kind"]
    assert state["site_notes"] == ["Sign in is the button under the form."]
    assert SITE_NOTES_RULE in kind.instructions

    elsewhere = loop([Site("github.com", notes=("Not this page.",))], tmp_path / "b")
    state, kind = elsewhere.requests[0]["state"], elsewhere.requests[0]["questions"]["kind"]
    assert "site_notes" not in state and SITE_NOTES_RULE not in kind.instructions
    assert "Not this page." not in json.dumps(state)


class StagedBrowser(FakeBrowser):
    """A page that repaints in stages: the old page for a few reads, then the new one."""

    def __init__(self, before: dict, after: dict, reads_before: int):
        super().__init__(before)
        self.after, self.reads_before = after, reads_before

    def evaluate(self, expression, **kwargs):
        out = super().evaluate(expression, **kwargs)
        if isinstance(out, dict) and "items" in out:
            self.reads_before -= 1
            if self.reads_before < 0:
                self.page = self.after
                return json.loads(json.dumps(self.after))
        return out


def test_settle_hands_over_the_page_that_arrives_after_the_first_change():
    after = login_page(url="https://example.test/issues", title="Issues")
    page = act.settle(StagedBrowser(login_page(), after, reads_before=2), 2000, quiet_ms=100, poll_ms=10)
    assert page.url == "https://example.test/issues"


def test_settle_stops_at_its_limit_on_a_page_that_never_holds_still():
    class Restless(FakeBrowser):
        n = 0

        def evaluate(self, expression, **kwargs):
            out = super().evaluate(expression, **kwargs)
            if isinstance(out, dict) and "items" in out:
                Restless.n += 1
                out["title"] = f"frame {Restless.n}"
            return out

    page = act.settle(Restless(login_page()), 150, quiet_ms=100, poll_ms=10)
    assert page.title.startswith("frame ")


def test_on_a_site_with_a_settle_time_a_late_change_still_counts(tmp_path):
    """The click lands, the page answers after the usual window: the step reports the change and
    the next decision sees the new page, instead of clicking again on the old one."""
    after = login_page(url="https://example.test/issues/3", title="Issue 3")
    browser = StagedBrowser(login_page(), after, reads_before=4)
    client = FakeTypeSafe(("click", "2"), ("done", None))
    result = run_goal(
        browser,
        client,
        "open issue 3",
        max_steps=2,
        change_timeout_ms=0,
        verbose=False,
        sites=[Site("example.test", settle_ms=2000)],
    )
    assert result.steps[0].changed
    assert client.requests[1]["state"]["page"]["url"] == "https://example.test/issues/3"
