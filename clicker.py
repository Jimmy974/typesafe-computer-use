"""Screen OCR -> TypeSafe Choice -> mouse / keyboard action.

Each step:
  1. screencapture the main display
  2. Apple Vision OCR (via ocrmac) yields text lines + pixel bounding boxes
  3. one TypeSafe Choice: every OCR line is an option keyed by index, plus a
     fixed set of deterministic actions (switch app, open a known site, type
     email, press enter, scroll, wait, done, none)
  4. run the winning action; an OCR index clicks the center of that box

Dry-run by default. Pass --act to actually drive the machine.

Escape hatches while --act is running:
  * Ctrl-C in the terminal (when the terminal has focus)
  * slam the mouse into the top-left corner of the screen (works from any app)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import ApplicationServices
import Quartz
from ocrmac import ocrmac
from PIL import Image, ImageDraw, ImageFont
from typesafe_sdk import Choice, TypeSafeClient

MIN_OCR_CONFIDENCE = 0.3
MAX_OPTIONS = 255
ABORT_CORNER_PX = 4
BROWSER = "Google Chrome"

# Sites the "open_site" action can navigate to. Extend freely; keys are what
# the classifier picks from, so keep them recognisable.
SITES = {
    "launchdarkly": "https://app.launchdarkly.com/",
    "github": "https://github.com/",
    "linear": "https://linear.app/",
    "gmail": "https://mail.google.com/",
    "google_calendar": "https://calendar.google.com/",
    "slack": "https://app.slack.com/",
    "notion": "https://www.notion.so/",
    "typesafe_console": "https://console.typesafe.ai/",
}

KEYCODES = {"return": 36, "tab": 48, "escape": 53, "l": 37}


class Abort(Exception):
    pass


@dataclass(frozen=True)
class Item:
    index: int
    text: str
    ocr_confidence: float
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def center(self) -> tuple[float, float]:
        return (self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2


@dataclass(frozen=True)
class Screen:
    image: Image.Image
    scale: float  # physical pixels per point
    app: str

    def region(self, item: Item) -> str:
        cx, cy = item.center
        col = ["left", "center", "right"][min(2, int(3 * cx / self.image.width))]
        row = ["top", "middle", "bottom"][min(2, int(3 * cy / self.image.height))]
        return f"{row}-{col}"


# --------------------------------------------------------------------------- input


def mouse_location() -> tuple[float, float]:
    loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return loc.x, loc.y


def check_abort() -> None:
    x, y = mouse_location()
    if x <= ABORT_CORNER_PX and y <= ABORT_CORNER_PX:
        raise Abort("mouse in top-left corner")


def sleep_watching(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        check_abort()
        time.sleep(0.1)


def post(event) -> None:
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
    time.sleep(0.04)


def click_at(point: tuple[float, float]) -> None:
    for kind in (Quartz.kCGEventMouseMoved, Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        post(Quartz.CGEventCreateMouseEvent(None, kind, point, Quartz.kCGMouseButtonLeft))


def press(key: str, command: bool = False) -> None:
    code = KEYCODES[key]
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
        if command:
            Quartz.CGEventSetFlags(ev, Quartz.kCGEventFlagMaskCommand)
        post(ev)


def type_text(text: str) -> None:
    for ch in text:
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(ev, len(ch), ch)
            post(ev)


def scroll(lines: int) -> None:
    post(Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, lines))


def activate(app: str) -> None:
    subprocess.run(["open", "-a", app], check=True)


def frontmost_app() -> str:
    script = 'tell application "System Events" to get name of first application process whose frontmost is true'
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True).stdout.strip()


# --------------------------------------------------------------------------- perception


def capture(image_path: Path | None = None, app: str | None = None) -> Screen:
    if image_path is None:
        image_path = Path(tempfile.mkdtemp()) / "screen.png"
        subprocess.run(["screencapture", "-x", "-D", "1", str(image_path)], check=True, capture_output=True)
    image = Image.open(image_path).convert("RGB")
    points_wide = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID()).size.width
    return Screen(image=image, scale=image.width / points_wide, app=app or frontmost_app())


def ocr(screen: Screen, budget: int, goal: str) -> list[Item]:
    raw = ocrmac.OCR(screen.image, recognition_level="accurate").recognize(px=True)
    needle = " ".join(goal.lower().split())
    kept = [
        (t.strip(), c, b)
        for t, c, b in raw
        if t.strip() and c >= MIN_OCR_CONFIDENCE and needle not in " ".join(t.lower().split())
    ]
    heights = sorted(b[3] - b[1] for _, _, b in kept) or [1.0]
    row_h = max(1.0, heights[len(heights) // 2])
    kept.sort(key=lambda r: (round((r[2][1] + r[2][3]) / 2 / row_h), r[2][0]))
    return [Item(i, t, c, *b) for i, (t, c, b) in enumerate(kept[:budget])]


# --------------------------------------------------------------------------- actions


def fixed_actions(email: str | None) -> dict[str, str]:
    actions = {
        "switch_to_browser": (
            f"Bring {BROWSER} to the front to continue with whatever page is already open there. "
            "Not for reaching a specific website: open_site does that on its own, even from another app."
        ),
        "open_site": (
            "Navigate the browser to a known website (chosen in the follow-up question). "
            "This is the only way to go to a site: never click the address bar, a URL, or a search box to get there."
        ),
        "press_enter": "Press Return to submit the focused form or field.",
        "press_escape": "Press Escape to dismiss a dialog, menu, or popup.",
        "scroll_down": "Scroll down to reveal more of the page.",
        "scroll_up": "Scroll up.",
        "wait": "Nothing to do yet; the screen is still loading or changing.",
        "done": "The goal is already achieved.",
        "none": "Nothing on screen or in this list helps with the goal.",
    }
    if email:
        actions["type_email"] = "Type the user's email address into the currently focused text field."
    return actions


def decide(client: TypeSafeClient, goal: str, screen: Screen, items: list[Item], history: list[str], email: str | None):
    kinds = {"click_item": "Click one of the on-screen text items (chosen in the item question)."}
    kinds.update(fixed_actions(email))
    state = {
        "goal": goal,
        "frontmost_app": screen.app,
        "previous_actions": history[-8:],
        "screen_text_in_reading_order": [
            {"i": it.index, "text": it.text, "where": screen.region(it)} for it in items
        ],
    }
    questions = {
        "kind": Choice(
            instructions=(
                "You are driving this computer one action at a time. Which kind of action "
                "makes the most progress toward the goal right now? Do not repeat an action "
                "that was just taken unless the screen changed."
            ),
            criteria=kinds,
        ),
        "site": Choice(
            instructions="If a website must be opened to progress the goal, which one?",
            criteria={**SITES, "none": "No website is needed."},
        ),
    }
    if items:
        questions["item"] = Choice(
            instructions="If clicking an on-screen item is the right move, which item?",
            criteria={str(it.index): f"{it.text!r} ({screen.region(it)})" for it in items},
        )
    answers = client.system_one(state=state, questions=questions).answers
    return answers["kind"], answers.get("item"), answers["site"]


def perform(key: str, site_key: str, screen: Screen, items: list[Item], email: str | None) -> str:
    """Execute one action and return a short description for the history."""
    by_index = {str(it.index): it for it in items}
    if key in by_index:
        it = by_index[key]
        cx, cy = it.center
        click_at((cx / screen.scale, cy / screen.scale))
        return f"clicked {it.text!r}"
    if key == "switch_to_browser":
        activate(BROWSER)
        return f"activated {BROWSER}"
    if key == "open_site":
        url = SITES.get(site_key)
        if not url:
            return "open_site chosen but no site selected"
        activate(BROWSER)
        time.sleep(0.6)
        press("l", command=True)
        time.sleep(0.2)
        type_text(url)
        press("return")
        return f"opened {url}"
    if key == "press_enter":
        press("return")
        return "pressed Return"
    if key == "press_escape":
        press("escape")
        return "pressed Escape"
    if key == "scroll_down":
        scroll(-10)
        return "scrolled down"
    if key == "scroll_up":
        scroll(10)
        return "scrolled up"
    if key == "type_email" and email:
        type_text(email)
        return "typed email"
    if key == "wait":
        return "waited"
    raise ValueError(key)


# --------------------------------------------------------------------------- reporting


def annotate(screen: Screen, items: list[Item], chosen: str, out: Path) -> None:
    im = screen.image.copy()
    draw = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", int(11 * screen.scale))
    except OSError:
        font = ImageFont.load_default()
    for it in items:
        hit = str(it.index) == chosen
        color = (255, 0, 0) if hit else (0, 160, 255)
        draw.rectangle((it.x1, it.y1, it.x2, it.y2), outline=color, width=3 if hit else 1)
        draw.text((it.x1, max(0, it.y1 - 12 * screen.scale)), str(it.index), fill=color, font=font)
    im.save(out)


def top(answer, n: int = 5) -> list[tuple[str, float]]:
    return sorted(answer.probabilities.items(), key=lambda kv: -kv[1])[:n]


# --------------------------------------------------------------------------- loop


def run_step(args, client: TypeSafeClient, step: int, history: list[str], email: str | None) -> bool:
    check_abort()
    screen = capture(args.image, args.app)
    screen.image.save(args.out / f"step-{step:02d}-raw.png")
    items = ocr(screen, MAX_OPTIONS, args.goal)

    kind, item, site = decide(client, args.goal, screen, items, history, email)
    by_index = {str(it.index): it for it in items}
    clicking = kind.choice == "click_item" and item is not None
    chosen = item.choice if clicking else kind.choice
    confidence = min(kind.confidence, item.confidence) if clicking else kind.confidence

    out = args.out / f"step-{step:02d}.png"
    annotate(screen, items, chosen, out)

    print(f"\nstep {step}: app={screen.app!r} items={len(items)} kind={kind.choice} ({kind.confidence:.2f}) site={site.choice}")
    for key, p in top(kind, 4):
        print(f"  {p:5.2f}  {key}")
    if item is not None:
        print(f"  item ({item.confidence:.2f}):")
        for key, p in top(item, 4):
            print(f"  {p:5.2f}  [{key}] {by_index[key].text!r}")
    print(f"  annotated: {out}")
    if args.json:
        out.with_suffix(".json").write_text(json.dumps({
            "items": [it.__dict__ for it in items],
            "kind": kind.choice, "kind_probabilities": kind.probabilities,
            "item": item.choice if item else None,
            "item_probabilities": item.probabilities if item else None,
            "site": site.choice,
        }, indent=2))

    if kind.choice in ("done", "none"):
        print(f"  model says {kind.choice!r}; stopping")
        return False
    if confidence < args.min_confidence:
        print(f"  confidence {confidence:.2f} below {args.min_confidence}; stopping")
        return False
    if not args.act or args.image:
        print(f"  would do: {chosen}. dry run (pass --act without --image to drive the machine)")
        return False

    what = perform(chosen, site.choice, screen, items, email)
    history.append(what)
    print(f"  did: {what}")
    sleep_watching(args.delay)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("goal", help="what you want done on this computer")
    parser.add_argument("--act", action="store_true", help="actually click/type (default: dry run)")
    parser.add_argument("--steps", type=int, default=12, help="max actions before stopping")
    parser.add_argument("--min-confidence", type=float, default=0.5)
    parser.add_argument("--delay", type=float, default=2.0, help="seconds to wait after each action")
    parser.add_argument("--out", type=Path, default=Path("runs") / time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--json", action="store_true", help="dump OCR items + probabilities per step")
    parser.add_argument("--image", type=Path, help="replay against a saved screenshot instead of capturing (never acts)")
    parser.add_argument("--app", help="frontmost app name to report to the model (for --image replays)")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    email = os.environ.get("CLICKER_EMAIL")
    if args.act and not ApplicationServices.AXIsProcessTrusted():
        sys.exit("this terminal lacks Accessibility permission; grant it in System Settings > Privacy & Security")
    if args.act:
        print("driving the machine. abort: Ctrl-C, or slam the mouse into the top-left corner.")

    history: list[str] = []
    try:
        with TypeSafeClient() as client:
            for step in range(1, args.steps + 1):
                if not run_step(args, client, step, history, email):
                    break
            else:
                print(f"\nstopped after {args.steps} steps")
    except (KeyboardInterrupt, Abort) as e:
        print(f"\naborted ({e or 'Ctrl-C'}) after {len(history)} actions")
        sys.exit(130)


if __name__ == "__main__":
    main()
