"""Execute a decision through CDP. Each handler returns a history line.

Every action is a real input event (`Input.dispatchMouseEvent` /
`Input.insertText`), not a JS `.click()`, so frameworks that listen for genuine
pointer events behave exactly as they would for a human.
"""

from __future__ import annotations

import mimetypes
import time
from pathlib import Path
from typing import Any

from .cdp import CDPError, Session
from .perceive import Element, Page, perceive

# CDP expects these key codes; enter is the only one the loop needs beyond typing.
KEYS = {
    "enter": ("Enter", "\r"),
    "escape": ("Escape", "\u001b"),
}


def _select(index: int) -> str:
    return f'[data-tscu="{index}"]'


def element_rect(session: Session, index: int) -> dict | None:
    """Where to press the element: the centre of the first line of it that the page says is really it.

    The centre of the bounding box misses a link that wraps: its box spans both lines, and the
    middle of that box can be blank space past the end of the shorter line. So each line box is
    tried in turn, and a point counts only when `elementFromPoint` lands on the element or inside
    it. When none does, something covers it, and the first line's centre is returned anyway with
    `hit` false: a real click then lands on the cover, as it would for a person.
    """
    return session.evaluate(
        f"""(() => {{
          const el = document.querySelector({_select(index)!r});
          if (!el) return null;
          const vw = innerWidth, vh = innerHeight, box = el.getBoundingClientRect();
          const lines = Array.from(el.getClientRects()).filter(r => r.width >= 1 && r.height >= 1);
          const centre = r => [r.left + Math.min(r.width, 1400) / 2, r.top + r.height / 2];
          const lands = (x, y) => {{
            if (x < 0 || y < 0 || x >= vw || y >= vh) return false;
            const hit = document.elementFromPoint(x, y);
            return !!hit && (hit === el || el.contains(hit));
          }};
          const out = (p, hit) => ({{x: Math.round(p[0]), y: Math.round(p[1]),
                                     w: Math.round(box.width), h: Math.round(box.height), vw, vh, hit}});
          for (const r of lines.length ? lines : [box]) {{
            const p = centre(r);
            if (lands(p[0], p[1])) return out(p, true);
          }}
          return out(centre(lines[0] || box), false);
        }})()"""
    )


def is_gone(session: Session, index: int) -> bool:
    return session.evaluate(f"!document.querySelector({_select(index)!r})") is True


def click(session: Session, index: int, element: Element, page: Page) -> str:
    """Real mouse press/release at the element's centre."""
    if not element.in_view:
        session.evaluate(
            f"""(() => {{
              const el = document.querySelector({_select(index)!r});
              if (el) el.scrollIntoView({{block: "center", inline: "center"}});
              return true;
            }})()"""
        )
        time.sleep(0.05)
    rect = element_rect(session, index)
    if rect is None:
        return f"click {index} FAILED (element gone)"
    if not rect.get("hit", True):
        # Something sits over it where it is, often a bar pinned to the bottom of the window:
        # brought to the middle of the view, it is usually clear of it.
        session.evaluate(
            f"""(() => {{
              const el = document.querySelector({_select(index)!r});
              if (el) el.scrollIntoView({{block: "center", inline: "center"}});
              return true;
            }})()"""
        )
        time.sleep(0.05)
        rect = element_rect(session, index) or rect
    x, y = _press(session, rect)
    retried = ""
    if element.checked is False and _is_checked(session, index) is False:
        # An unticked box or radio that is still unticked missed: the page was likely still
        # moving, as when it scrolls itself to an error. Once more, from where it is now.
        time.sleep(0.15)
        if _is_checked(session, index) is False and (again := element_rect(session, index)):
            rect = again
            x, y = _press(session, rect)
            retried = ", pressed again"
    covered = "" if rect.get("hit", True) else " (something covers it there)"
    return f"click [{index}] {element.name[:60]!r} at ({x},{y}){covered}{retried}"


def _press(session: Session, rect: dict) -> tuple[int, int]:
    x = max(1, min(int(rect["x"]), max(1, int(rect["vw"]) - 2)))
    y = max(1, min(int(rect["y"]), max(1, int(rect["vh"]) - 2)))
    for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
        session.call(
            "Input.dispatchMouseEvent",
            {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1, "buttons": 1 if kind != "mouseReleased" else 0},
        )
    return x, y


def _is_checked(session: Session, index: int) -> bool | None:
    """Whether a box or radio is ticked now; None when it is gone or is not one."""
    value = session.evaluate(
        f"""(() => {{ const el = document.querySelector({_select(index)!r});
                     if (!el) return null;
                     if (typeof el.checked === "boolean") return el.checked;
                     const aria = el.getAttribute("aria-checked");
                     return aria === null ? null : aria === "true"; }})()"""
    )
    return value if isinstance(value, bool) else None


def focus(session: Session, index: int) -> bool:
    return (
        session.evaluate(
            f"""(() => {{
          const el = document.querySelector({_select(index)!r});
          if (!el) return false;
          el.focus();
          if (el.select) {{ try {{ el.select(); }} catch (e) {{}} }}
          return document.activeElement === el;
        }})()"""
        )
        is True
    )


def type_text(session: Session, index: int, text: str) -> str:
    if not focus(session, index):
        return f"type FAILED (could not focus [{index}])"
    session.call("Input.insertText", {"text": text})
    return f"type {text!r} into [{index}]"


def field_value(session: Session, index: int) -> str | None:
    """What a field holds after the loop typed into it, for the verify check. A password field
    is never read: the loop never types into one, and this makes sure of it a second time."""
    value = session.evaluate(
        f"""(() => {{ const el = document.querySelector({_select(index)!r});
                     if (!el || el.value === undefined || String(el.type).toLowerCase() === "password") return null;
                     return String(el.value); }})()"""
    )
    return None if value is None else str(value)


def select_options(session: Session, index: int) -> list[tuple[int, str]]:
    """The choices a <select> offers, by the text the page wrote for each: (option index, text).

    A disabled option and a placeholder with no value, such as "Select language", are left out."""
    options = session.evaluate(
        f"""(() => {{ const el = document.querySelector({_select(index)!r});
                     if (!el || el.tagName !== "SELECT") return [];
                     return [...el.options].map((o, i) => [i, o.text.replace(/\\s+/g, " ").trim(), o.disabled || o.value === ""])
                       .filter(o => !o[2] && o[1]).map(o => [o[0], o[1].slice(0, 120)]); }})()"""
    )
    return [(int(i), str(text)) for i, text in options or []]


def select_option(session: Session, index: int, option: int) -> bool:
    """Pick one option of a <select> the way a person's choice lands: the page's own listeners hear it.

    A click opens the browser's own list, which the page cannot draw and a click cannot reach; the
    value is set instead, through the setter a framework watches, and announced."""
    return (
        session.evaluate(
            f"""(() => {{ const el = document.querySelector({_select(index)!r});
                         if (!el || el.tagName !== "SELECT" || !el.options[{int(option)}]) return false;
                         const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value").set;
                         el.focus();
                         setter.call(el, el.options[{int(option)}].value);
                         el.dispatchEvent(new Event("input", {{bubbles: true}}));
                         el.dispatchEvent(new Event("change", {{bubbles: true}}));
                         return el.selectedIndex === {int(option)}; }})()"""
        )
        is True
    )


# Every empty file input on the page, shown or not: a page usually hides the real input behind a
# button or a drop area of its own. `about` is the nearest short text around it, to match a name.
FILE_INPUTS_JS = r"""
(() => [...document.querySelectorAll('input[type="file"]')].map((el, i) => {
  let about = "", box = el;
  for (let up = 0; up < 6 && box && !about; up++, box = box.parentElement) {
    const text = (box.innerText || "").replace(/\s+/g, " ").trim();
    if (text && text.length <= 160) about = text;
  }
  return {i, empty: !(el.files && el.files.length), accept: el.getAttribute("accept") || "", about: about.slice(0, 120)};
}))()
"""


def _accepts(accept: str, path: str) -> bool:
    if not accept.strip():
        return True
    suffix = Path(path).suffix.lower()
    mime = mimetypes.guess_type(path)[0] or ""
    for token in (t.strip().lower() for t in accept.split(",")):
        if token in (suffix, mime) or (token.endswith("/*") and mime.startswith(token[:-1])):
            return True
    return False


def upload_files(session: Session, files: dict[str, str], *, skip: set[str] | None = None) -> list[str]:
    """Put a saved file into every empty file input it fits, and say what went where.

    With one saved file, it goes into each empty input that accepts it. With several, an input
    takes the one whose name its surrounding text mentions. The page hears it as a person's choice:
    the browser fills the input and fires its events. Many pages empty the input once they have
    the file, so an input is skipped when its surroundings already name a saved file, or when its
    surroundings are in `skip`, the places this run has filled; each place filled is added there."""
    skip = skip if skip is not None else set()
    names = [Path(p).name.casefold() for p in files.values()]
    done = []
    for spot in session.evaluate(FILE_INPUTS_JS) or []:
        if not spot.get("empty"):
            continue
        about = str(spot.get("about") or "")
        if about in skip or any(name in about.casefold() for name in names):
            continue
        named = (k for k in files if k.casefold() in about.casefold())
        key = next(iter(files)) if len(files) == 1 else next(named, None)
        if key is None or not _accepts(str(spot.get("accept") or ""), files[key]):
            continue
        found = session.call(
            "Runtime.evaluate",
            {"expression": f"document.querySelectorAll('input[type=\"file\"]')[{int(spot['i'])}]", "returnByValue": False},
        )
        object_id = ((found or {}).get("result") or {}).get("objectId")
        if not object_id:
            continue
        session.call("DOM.setFileInputFiles", {"files": [files[key]], "objectId": object_id})
        skip.add(about)
        done.append(f"{key} into {about[:50]!r}" if about else key)
    return done


def clear_field(session: Session, index: int) -> None:
    focus(session, index)
    session.call(
        "Input.dispatchKeyEvent",
        {"type": "keyDown", "modifiers": 4, "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65},
    )
    session.call(
        "Input.dispatchKeyEvent",
        {"type": "keyUp", "modifiers": 4, "key": "a", "code": "KeyA", "windowsVirtualKeyCode": 65},
    )
    session.call("Input.insertText", {"text": ""})
    session.call(
        "Input.dispatchKeyEvent", {"type": "keyDown", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8}
    )
    session.call("Input.dispatchKeyEvent", {"type": "keyUp", "key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8})


def press(session: Session, key: str) -> str:
    k, text = KEYS[key]
    for event in ("keyDown", "keyUp"):
        session.call(
            "Input.dispatchKeyEvent",
            {
                "type": event,
                "key": k,
                "text": text if event == "keyDown" else "",
                "unmodifiedText": text,
                "windowsVirtualKeyCode": 13 if key == "enter" else 27,
            },
        )
    return f"press {key}"


def scroll(session: Session, lines: int, page: Page) -> str:
    session.call(
        "Input.dispatchMouseEvent",
        {"type": "mouseWheel", "x": max(1, page.vw // 2), "y": max(1, page.vh // 2), "deltaX": 0, "deltaY": lines * 120},
    )
    return f"scroll {'down' if lines > 0 else 'up'} {abs(lines)} lines"


def navigate(session: Session, url: str, *, timeout_ms: int = 15000) -> str:
    session.call("Page.navigate", {"url": url})
    return f"navigate {url}"


def wait_for_load(session: Session, *, timeout_ms: int = 15000, settle_ms: int = 120) -> float:
    """Fixed-sleep variant, kept for callers that want deterministic pacing."""
    start = time.perf_counter()
    deadline = start + timeout_ms / 1000
    while time.perf_counter() < deadline:
        try:
            if session.evaluate("document.readyState") == "complete":
                break
        except CDPError:
            pass  # asked while the old page was being torn down: the new one answers next time
        time.sleep(0.03)
    time.sleep(settle_ms / 1000)
    return (time.perf_counter() - start) * 1000


def fingerprint(page: Page) -> tuple:
    """Cheap identity for 'is this still the same page?'."""
    # Checking a box or filling a field changes the page as much as a new link does.
    # So does a message appearing, such as an error after a button press.
    return (
        page.url,
        page.title,
        page.scroll_y,
        tuple((e.index, e.name, e.x, e.y, e.checked, e.filled, e.chosen, e.invalid) for e in page.items),
        page.messages,
    )


def observe_until_changed(
    session: Session,
    before: tuple | None,
    *,
    timeout_ms: int = 300,
    poll_ms: int = 8,
    budget: int = 120,
) -> tuple[Page, float, bool]:
    """Perceive until the page differs from `before`. Returns the post-action page.

    This replaces the settle sleep entirely. The observation we need for the
    *next* decision doubles as the wait for *this* action, so waiting costs no
    extra round trips: in the common case the change lands within ~20-40ms and
    we detect it on the first or second poll.

    The timeout is deliberately short. A page that has not changed in 300ms is
    better handled by the loop's own `wait` action than by stalling here.
    """
    start = time.perf_counter()
    if before is None:
        page = perceive(session, budget=budget)
        return page, (time.perf_counter() - start) * 1000, True
    deadline = start + timeout_ms / 1000
    page = perceive(session, budget=budget)
    while time.perf_counter() < deadline:
        if fingerprint(page) != before:
            return page, (time.perf_counter() - start) * 1000, True
        time.sleep(poll_ms / 1000)
        page = perceive(session, budget=budget)
    return page, (time.perf_counter() - start) * 1000, False


def settle(session: Session, limit_ms: int, *, quiet_ms: int = 300, poll_ms: int = 50, budget: int = 120) -> Page:
    """Perceive until the page has held still for `quiet_ms`, or `limit_ms` has passed. Returns the last page.

    For a site that renders in stages, where the first change after a click is the tab
    repainting and the new page arrives a moment later. A page that is already still costs
    `quiet_ms`; a page that never settles costs `limit_ms` and is handed over as it stands.
    """
    deadline = time.perf_counter() + limit_ms / 1000
    page = perceive(session, budget=budget)
    still_since = time.perf_counter()
    while time.perf_counter() < deadline:
        time.sleep(poll_ms / 1000)
        now = perceive(session, budget=budget)
        if fingerprint(now) != fingerprint(page):
            page, still_since = now, time.perf_counter()
        elif (time.perf_counter() - still_since) * 1000 >= quiet_ms:
            break
    return page


def go_back(session: Session) -> str:
    session.evaluate("history.back(); true")
    return "back"


def snapshot(session: Session, **kwargs: Any) -> Page:
    return perceive(session, **kwargs)
