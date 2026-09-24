"""What carries out a click, a typed string, or a key: the only part of an action that differs by backend.

The loop decides, perceives, scrolls, goes back, and waits for the page the same way whichever
hands it has, so a comparison between two of them measures the hands and nothing else.

- `CdpHands` is the original: real input events at a point the page reports, through our own CDP
  session (`act.py`).
- `PlaywrightHands` connects Playwright to the same Chrome over CDP and acts through a locator on
  the element's `data-tscu` tag. Playwright checks the element is visible, stable, enabled and the
  one that would receive the click before it presses, then sends the same kind of real input
  event. It is optional: `uv sync --extra playwright` installs it, and it drives the Chrome that
  is already running, so nothing else is downloaded.
"""

from __future__ import annotations

from typing import Any, Protocol

from . import act
from .cdp import Session
from .perceive import Element, Page

HANDS = ("cdp", "playwright")
PLAYWRIGHT_KEYS = {"enter": "Enter", "escape": "Escape"}


class Hands(Protocol):
    name: str

    def click(self, index: int, element: Element, page: Page) -> str: ...
    def type_text(self, index: int, text: str) -> str: ...
    def press(self, key: str) -> str: ...


class CdpHands:
    name = "cdp"

    def __init__(self, session: Session):
        self.session = session

    def click(self, index: int, element: Element, page: Page) -> str:
        return act.click(self.session, index, element, page)

    def type_text(self, index: int, text: str) -> str:
        return act.type_text(self.session, index, text)

    def press(self, key: str) -> str:
        return act.press(self.session, key)


class PlaywrightHands:
    """Playwright on the page the CDP session already drives. Use as a context manager."""

    name = "playwright"

    def __init__(self, origin: str, *, timeout_ms: int = 3000):
        # A short timeout: a control that is covered or never becomes clickable is a failed action
        # the loop can recover from, not thirty seconds of waiting.
        self.origin = origin
        self.timeout_ms = timeout_ms
        self._pw: Any = None
        self._browser: Any = None
        self.page: Any = None

    def __enter__(self) -> PlaywrightHands:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError("Playwright is not installed: run `uv sync --extra playwright`") from e
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.connect_over_cdp(self.origin)
        pages = [p for ctx in self._browser.contexts for p in ctx.pages]
        if not pages:
            self.__exit__()
            raise RuntimeError("Playwright found no page in this Chrome")
        self.page = pages[0]
        return self

    def __exit__(self, *exc: object) -> None:
        # Disconnect only: closing a browser Playwright connected to over CDP leaves Chrome to its owner.
        if self._browser is not None:
            self._browser.close()
        if self._pw is not None:
            self._pw.stop()
        self._browser = self._pw = self.page = None

    def _locator(self, index: int):
        return self.page.locator(f'[data-tscu="{index}"]')

    def click(self, index: int, element: Element, page: Page) -> str:
        try:
            self._locator(index).click(timeout=self.timeout_ms)
        except Exception as e:  # Playwright's TimeoutError and Error both end the same way here
            return f"click [{index}] FAILED ({_first_line(e)})"
        return f"click [{index}] {element.name[:60]!r} via playwright"

    def type_text(self, index: int, text: str) -> str:
        try:
            self._locator(index).fill(text, timeout=self.timeout_ms)
        except Exception as e:
            return f"type FAILED ({_first_line(e)})"
        return f"type {text!r} into [{index}] via playwright"

    def press(self, key: str) -> str:
        self.page.keyboard.press(PLAYWRIGHT_KEYS[key])
        return f"press {key}"


def _first_line(e: Exception) -> str:
    return (str(e).strip().splitlines() or [type(e).__name__])[0][:120]
