"""What is known about one website, kept as data in `sites/<domain>.toml`, not as actions.

A new action for one site overlaps the generic ones (`click` already opens a tab), and two
options that mean the same thing read as doubt. So a site file adds facts, not choices:

- `notes` join the classifier's state while the page is on that site, as `site_notes`
- `settle_ms` is how long to keep watching after an action changed the page, for a site that
  renders in stages (a client-side route change first repaints the tab, then swaps the page)

    domain = "github.com"
    settle_ms = 1500
    notes = ["The number beside Issues is the count of open issues."]

The file matches its domain and every subdomain of it; the longest match wins, so a file for
`gist.github.com` beats one for `github.com`.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

MAX_SETTLE_MS = 10_000  # a slower site needs `wait`, which the classifier chooses and the history shows
KEYS = {"domain", "notes", "settle_ms"}


@dataclass(frozen=True)
class Site:
    domain: str
    notes: tuple[str, ...] = ()
    settle_ms: int = 0
    source: str = ""  # the file it came from, for the log


def parse_site(data: dict, source: str = "") -> Site:
    """One site file's table, checked. A typo in a key is an error, not a silently ignored setting."""
    unknown = set(data) - KEYS
    if unknown:
        raise ValueError(f"{source}: unknown key(s) {', '.join(sorted(unknown))}; expected {', '.join(sorted(KEYS))}")
    domain = data.get("domain")
    if not isinstance(domain, str) or not domain.strip() or "/" in domain:
        raise ValueError(f"{source}: domain must be a host name such as 'github.com'")
    notes = data.get("notes", [])
    if not isinstance(notes, list) or not all(isinstance(n, str) and n.strip() for n in notes):
        raise ValueError(f"{source}: notes must be a list of non-empty strings")
    settle = data.get("settle_ms", 0)
    if not isinstance(settle, int) or isinstance(settle, bool) or not 0 <= settle <= MAX_SETTLE_MS:
        raise ValueError(f"{source}: settle_ms must be a whole number from 0 to {MAX_SETTLE_MS}")
    return Site(domain=domain.strip().lower(), notes=tuple(n.strip() for n in notes), settle_ms=settle, source=source)


def load_sites(folder: Path) -> list[Site]:
    """Every `*.toml` in the folder. No folder means no site knowledge, which is the default."""
    if not folder.is_dir():
        return []
    sites = []
    for path in sorted(folder.glob("*.toml")):
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"{path}: {e}") from e
        sites.append(parse_site(data, str(path)))
    return sites


def site_for(url: str | None, sites: list[Site]) -> Site | None:
    """The site file for this page's host: the domain itself or any subdomain, longest domain first."""
    host = (urlparse(url or "").hostname or "").lower()
    if not host:
        return None
    matches = [s for s in sites if host == s.domain or host.endswith("." + s.domain)]
    return max(matches, key=lambda s: len(s.domain), default=None)
