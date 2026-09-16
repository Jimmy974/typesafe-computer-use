"""Screen state -> TypeSafe Choice -> mouse / keyboard action.

Each step:
  1. screencapture the main display
  2. Apple Vision OCR (via ocrmac) yields text lines + pixel bounding boxes
  3. the accessibility API yields the focused element (role, label, value, frame)
  4. one TypeSafe request with three Choices: what kind of action, which OCR
     item (if clicking), which known site (if navigating)
  5. run the winning action deterministically. Free text is the one exception:
     `type_text` asks a small writing model for the string, types it, then a
     TypeSafe Noul checks the field's new value before the loop continues.

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
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import anthropic
import ApplicationServices as AS
import Quartz
from ocrmac import ocrmac
from PIL import Image, ImageDraw, ImageFont
from typesafe_sdk import Choice, Noul, TypeSafeClient

MIN_OCR_CONFIDENCE = 0.3
MAX_OPTIONS = 255
ABORT_CORNER_PX = 4
BROWSER = "Google Chrome"
WRITER_MODEL = os.environ.get("CLICKER_WRITER_MODEL", "claude-haiku-4-5")

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

KEYCODES = {"return": 36, "tab": 48, "escape": 53, "a": 0, "delete": 51}
TEXT_ROLES = {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"}


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
class Field:
    """The focused accessibility element, in screen points."""

    role: str
    label: str
    placeholder: str
    value: str
    x: float
    y: float
    w: float
    h: float

    @property
    def is_text(self) -> bool:
        return self.role in TEXT_ROLES

    def summary(self) -> dict:
        return {
            "role": self.role,
            "label": self.label,
            "placeholder": self.placeholder,
            "current_value": self.value[:200],
        }


@dataclass(frozen=True)
class Screen:
    image: Image.Image
    scale: float  # physical pixels per point
    app: str
    field: Field | None
    url: str | None

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


def clear_field() -> None:
    press("a", command=True)
    press("delete")


def frontmost_window_center() -> tuple[float, float] | None:
    """Center of the frontmost app's topmost on-screen window, in points. Pure Quartz, no AX needed."""
    pid = int(osascript('tell application "System Events" to get unix id of first application process whose frontmost is true'))
    windows = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID
    )
    for w in windows or []:
        if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer") == 0:
            b = w["kCGWindowBounds"]
            if b["Width"] > 50 and b["Height"] > 50:
                return b["X"] + b["Width"] / 2, b["Y"] + b["Height"] / 2
    return None


def scroll(lines: int) -> None:
    """Scroll events go to the view under the cursor, so park it over the frontmost window first."""
    center = frontmost_window_center()
    if center is not None:
        post(Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, center, Quartz.kCGMouseButtonLeft))
    post(Quartz.CGEventCreateScrollWheelEvent(None, Quartz.kCGScrollEventUnitLine, 1, lines))


def osascript(script: str) -> str:
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, check=True).stdout.strip()


def frontmost_app() -> str:
    return osascript('tell application "System Events" to get name of first application process whose frontmost is true')


def activate(app: str, timeout: float = 3.0) -> bool:
    """Bring an app to the front and confirm it got there."""
    osascript(f'tell application "{app}" to activate')
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if frontmost_app() == app:
            return True
        time.sleep(0.1)
    osascript(f'tell application "System Events" to set frontmost of process "{app}" to true')
    time.sleep(0.3)
    return frontmost_app() == app


def open_url(url: str) -> bool:
    osascript(f'tell application "{BROWSER}" to open location "{url}"')
    return activate(BROWSER)


def browser_url() -> str | None:
    try:
        return osascript(f'tell application "{BROWSER}" to get URL of active tab of front window') or None
    except subprocess.CalledProcessError:
        return None


# --------------------------------------------------------------------------- perception


def ax_attr(element, name: str):
    err, value = AS.AXUIElementCopyAttributeValue(element, name, None)
    return value if err == 0 else None


def focused_field() -> Field | None:
    system = AS.AXUIElementCreateSystemWide()
    element = ax_attr(system, AS.kAXFocusedUIElementAttribute)
    if element is None:
        return None
    pos = ax_attr(element, AS.kAXPositionAttribute)
    size = ax_attr(element, AS.kAXSizeAttribute)
    x = y = w = h = 0.0
    if pos is not None and size is not None:
        _, pt = AS.AXValueGetValue(pos, AS.kAXValueCGPointType, None)
        _, sz = AS.AXValueGetValue(size, AS.kAXValueCGSizeType, None)
        x, y, w, h = pt.x, pt.y, sz.width, sz.height
    value = ax_attr(element, AS.kAXValueAttribute)
    return Field(
        role=str(ax_attr(element, AS.kAXRoleAttribute) or ""),
        label=str(ax_attr(element, AS.kAXTitleAttribute) or ax_attr(element, AS.kAXDescriptionAttribute) or ""),
        placeholder=str(ax_attr(element, AS.kAXPlaceholderValueAttribute) or ""),
        value=value if isinstance(value, str) else "",
        x=x, y=y, w=w, h=h,
    )


def capture(image_path: Path | None = None, app: str | None = None) -> Screen:
    if image_path is None:
        image_path = Path(tempfile.mkdtemp()) / "screen.png"
        subprocess.run(["screencapture", "-x", "-D", "1", str(image_path)], check=True, capture_output=True)
    image = Image.open(image_path).convert("RGB")
    points_wide = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID()).size.width
    return Screen(
        image=image,
        scale=image.width / points_wide,
        app=app or frontmost_app(),
        field=None if image_path and app else focused_field(),
        url=None if image_path and app else browser_url(),
    )


def ocr(screen: Screen, budget: int, goal: str) -> list[Item]:
    raw = ocrmac.OCR(screen.image, recognition_level="accurate").recognize(px=True)
    norm = " ".join(goal.lower().split())
    echoes = {norm[:24], norm[-24:]} if len(norm) >= 24 else {norm}
    kept = [
        (t.strip(), c, b)
        for t, c, b in raw
        if t.strip() and c >= MIN_OCR_CONFIDENCE
        and not any(e in " ".join(t.lower().split()) for e in echoes)
    ]
    kept = merge_blocks(kept)
    heights = sorted(b[3] - b[1] for _, _, b in kept) or [1.0]
    row_h = max(1.0, heights[len(heights) // 2])
    kept.sort(key=lambda r: (round((r[2][1] + r[2][3]) / 2 / row_h), r[2][0]))
    return [Item(i, t, c, *b) for i, (t, c, b) in enumerate(kept[:budget])]


def merge_blocks(lines: list[tuple[str, float, tuple[float, float, float, float]]]):
    """Join lines that continue a block above them: aligned left edge, small gap, similar height."""
    lines = sorted(lines, key=lambda r: (r[2][1], r[2][0]))
    blocks: list[list] = []
    for text, conf, (x1, y1, x2, y2) in lines:
        h = y2 - y1
        best = None
        for block in blocks:
            bx1, by1, bx2, by2 = block[2]
            bh = block[3]
            gap = y1 - by2
            if abs(x1 - bx1) < 0.6 * bh and -0.2 * bh < gap < 0.8 * bh and 0.7 < h / max(bh, 1) < 1.4:
                if best is None or gap < best[0]:
                    best = (gap, block)
        if best is not None:
            block = best[1]
            bx1, by1, bx2, by2 = block[2]
            block[0] = f"{block[0]} {text}"
            block[1] = min(block[1], conf)
            block[2] = (min(bx1, x1), by1, max(bx2, x2), y2)
            block[3] = h
            continue
        blocks.append([text, conf, (x1, y1, x2, y2), h])
    return [(t, c, b) for t, c, b, _ in blocks]


def near_field(screen: Screen, items: list[Item], radius_pt: float = 160) -> list[str]:
    f = screen.field
    if f is None:
        return []
    out = []
    for it in items:
        cx, cy = it.center
        cx, cy = cx / screen.scale, cy / screen.scale
        if abs(cx - (f.x + f.w / 2)) < radius_pt + f.w / 2 and abs(cy - (f.y + f.h / 2)) < radius_pt:
            out.append(it.text)
    return out


# --------------------------------------------------------------------------- decisions


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
        "type_text": (
            "Type free text into the focused text field. A writing model composes the text from the "
            "goal and the field's label. Only valid when a text field is focused and needs content."
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
        actions["type_email"] = (
            "Type the user's email address into the focused text field. Use this, not type_text, "
            "whenever the field wants an email or username."
        )
    return actions


def base_state(goal: str, screen: Screen, items: list[Item], history: list[str]) -> dict:
    return {
        "goal": goal,
        "frontmost_app": screen.app,
        "browser_active_tab_url": screen.url,
        "focused_field": screen.field.summary() if screen.field else None,
        "previous_actions": history[-8:],
        "screen_text_in_reading_order": [
            {"i": it.index, "text": it.text, "where": screen.region(it)} for it in items
        ],
    }


def decide(client: TypeSafeClient, goal: str, screen: Screen, items: list[Item], history: list[str], email: str | None):
    kinds = {"click_item": "Click one of the on-screen text items (chosen in the item question)."}
    kinds.update(fixed_actions(email))
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
    answers = client.system_one(state=base_state(goal, screen, items, history), questions=questions).answers
    return answers["kind"], answers.get("item"), answers["site"]


def compose_text(writer: anthropic.Anthropic, goal: str, screen: Screen, items: list[Item], history: list[str]) -> str:
    """Ask the writing model for the exact string to type into the focused field. Empty means decline."""
    packet = {
        "goal": goal,
        "frontmost_app": screen.app,
        "previous_actions": history[-8:],
        "focused_field": screen.field.summary() if screen.field else None,
        "text_near_field": near_field(screen, items),
        "all_screen_text": [it.text for it in items][:120],
    }
    response = writer.messages.create(
        model=WRITER_MODEL,
        max_tokens=256,
        system=(
            "You fill in one text field on a user's screen. You receive the user's goal, recent "
            "actions, the focused field's label and placeholder, and nearby screen text. Decide the "
            "exact string to type. Never invent credentials, passwords, or personal data; for such "
            "fields, or when the field should not be filled, set fill to false."
        ),
        messages=[{"role": "user", "content": json.dumps(packet)}],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {
                        "fill": {"type": "boolean"},
                        "text": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["fill", "text", "reason"],
                    "additionalProperties": False,
                },
            }
        },
    )
    raw = "".join(b.text for b in response.content if b.type == "text")
    data = json.loads(raw)
    return data["text"].strip() if data["fill"] else ""


def compose_url(writer: anthropic.Anthropic, goal: str, history: list[str]) -> str:
    """Ask the writing model which URL to open for this goal. Empty means no sensible site."""
    response = writer.messages.create(
        model=WRITER_MODEL,
        max_tokens=200,
        system=(
            "Given a user's goal for their web browser, give the single best https URL to open first. "
            "Prefer the site's homepage or the most direct public page. If no website is implied, set ok to false."
        ),
        messages=[{"role": "user", "content": json.dumps({"goal": goal, "previous_actions": history[-8:]})}],
        output_config={
            "format": {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {"ok": {"type": "boolean"}, "url": {"type": "string"}, "reason": {"type": "string"}},
                    "required": ["ok", "url", "reason"],
                    "additionalProperties": False,
                },
            }
        },
    )
    data = json.loads("".join(b.text for b in response.content if b.type == "text"))
    url = data["url"].strip() if data["ok"] else ""
    parsed = urlparse(url)
    if parsed.scheme != "https" or "." not in parsed.netloc or any(ch.isspace() for ch in url):
        return ""
    return url


def verify_typed(client: TypeSafeClient, goal: str, field_before: Field, typed: str) -> float:
    """Noul: probability that the field now holds a sensible value for its purpose."""
    after = focused_field()
    state = {
        "goal": goal,
        "field": field_before.summary(),
        "text_typed": typed,
        "field_value_now": after.value[:300] if after else None,
        "field_still_focused": bool(after and after.role == field_before.role and after.label == field_before.label),
    }
    answer = client.system_one(
        state=state,
        questions={
            "ok": Noul(
                instructions=(
                    "Did the typing succeed: does the field now contain the typed text, and is that "
                    "text a sensible value for what this field asks for, given the goal?"
                ),
            )
        },
    ).answers["ok"]
    return answer.noul


# --------------------------------------------------------------------------- actions


def perform(key: str, site_key: str, screen: Screen, items: list[Item], email: str | None, clients) -> str:
    """Execute one action and return a short description for the history."""
    typesafe, writer, goal, history = clients
    by_index = {str(it.index): it for it in items}
    if key in by_index:
        it = by_index[key]
        cx, cy = it.center
        click_at((cx / screen.scale, cy / screen.scale))
        return f"clicked {it.text!r}"
    if key == "switch_to_browser":
        return f"activated {BROWSER}" if activate(BROWSER) else f"switch_to_browser failed: {BROWSER} did not come to the front"
    if key == "open_site":
        url = SITES.get(site_key) or (compose_url(writer, goal, history) if writer else "")
        if not url:
            return "open_site refused: no known site matches and no writer available to propose a URL"
        return f"opened {url}" if open_url(url) else f"open_site failed: opened {url} but {BROWSER} did not come to the front"
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
        if not (screen.field and screen.field.is_text):
            return "type_email refused: no text field is focused"
        type_text(email)
        return "typed email"
    if key == "type_text":
        if not (screen.field and screen.field.is_text):
            return "type_text refused: no text field is focused"
        if writer is None:
            return "type_text refused: ANTHROPIC_API_KEY not set"
        text = compose_text(writer, goal, screen, items, history)
        if not text:
            return "type_text: writer declined to fill this field"
        type_text(text)
        time.sleep(0.3)
        p = verify_typed(typesafe, goal, screen.field, text)
        if p < 0.5:
            clear_field()
            return f"typed {text!r} into {screen.field.label!r} but verification failed ({p:.2f}); cleared it"
        return f"typed {text!r} into {screen.field.label!r} (verified {p:.2f})"
    if key == "wait":
        return "waited"
    raise ValueError(key)


# --------------------------------------------------------------------------- reporting


LOG_FILE: Path | None = None


def log(msg: str = "") -> None:
    print(msg)
    if LOG_FILE is not None:
        with LOG_FILE.open("a") as f:
            f.write(msg + "\n")


def render_payload(goal: str, screen: Screen, items: list[Item], history: list[str], email: str | None) -> str:
    """Human-readable dump of exactly what goes to TypeSafe for this screen, plus the OCR block table."""
    kinds = {"click_item": "Click one of the on-screen text items (chosen in the item question)."}
    kinds.update(fixed_actions(email))
    item_criteria = {str(it.index): f"{it.text!r} ({screen.region(it)})" for it in items}
    rule = "=" * 78
    parts = [
        rule, "STATE  (sent as `state`)", rule, json.dumps(base_state(goal, screen, items, history), indent=2), "",
        rule, "QUESTION kind  (Choice criteria)", rule, json.dumps(kinds, indent=2), "",
        rule, "QUESTION item  (Choice criteria)", rule, json.dumps(item_criteria, indent=2), "",
        rule, "QUESTION site  (Choice criteria)", rule, json.dumps({**SITES, "none": "No website is needed."}, indent=2), "",
        rule,
        f"OCR BLOCKS  ({len(items)} after merge/filter; pixel boxes on the {screen.image.width}x{screen.image.height} capture, scale {screen.scale:g})",
        rule,
    ]
    for it in items:
        cx, cy = it.center
        parts.append(
            f"[{it.index:3d}] conf={it.ocr_confidence:.2f} box=({it.x1:.0f},{it.y1:.0f})-({it.x2:.0f},{it.y2:.0f}) "
            f"click_pt=({cx / screen.scale:.0f},{cy / screen.scale:.0f}) {screen.region(it):13} {it.text!r}"
        )
    if screen.field:
        parts += ["", "FOCUSED FIELD", json.dumps(asdict(screen.field), indent=2)]
    return "\n".join(parts) + "\n"


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
    f = screen.field
    if f is not None:
        s = screen.scale
        draw.rectangle((f.x * s, f.y * s, (f.x + f.w) * s, (f.y + f.h) * s), outline=(0, 200, 0), width=3)
    im.save(out)


def top(answer, n: int = 5) -> list[tuple[str, float]]:
    return sorted(answer.probabilities.items(), key=lambda kv: -kv[1])[:n]


# --------------------------------------------------------------------------- loop


def run_step(args, typesafe: TypeSafeClient, writer, step: int, history: list[str], email: str | None, noops: list[int]) -> bool:
    check_abort()
    screen = capture(args.image, args.app)
    items = ocr(screen, MAX_OPTIONS, args.goal)
    prefix = args.out / f"step-{step:02d}"
    screen.image.save(prefix.with_name(prefix.name + "-raw.png"))
    prefix.with_name(prefix.name + "-payload.txt").write_text(render_payload(args.goal, screen, items, history, email))

    kind, item, site = decide(typesafe, args.goal, screen, items, history, email)
    by_index = {str(it.index): it for it in items}
    clicking = kind.choice == "click_item" and item is not None
    chosen = item.choice if clicking else kind.choice
    confidence = min(kind.confidence, item.confidence) if clicking else kind.confidence

    out = prefix.with_suffix(".png")
    annotate(screen, items, chosen, out)
    prefix.with_name(prefix.name + "-answers.json").write_text(json.dumps({
        "kind": kind.choice, "kind_confidence": kind.confidence, "kind_probabilities": kind.probabilities,
        "item": item.choice if item else None, "item_confidence": item.confidence if item else None,
        "item_probabilities": item.probabilities if item else None,
        "site": site.choice, "site_probabilities": site.probabilities,
        "chosen": chosen, "confidence": confidence,
        "items": [asdict(it) for it in items],
        "field": asdict(screen.field) if screen.field else None,
        "app": screen.app, "url": screen.url,
    }, indent=2))

    field = f" field={screen.field.role}:{screen.field.label!r}" if screen.field else ""
    log(f"\nstep {step}: app={screen.app!r}{field} url={screen.url!r} items={len(items)} kind={kind.choice} ({kind.confidence:.2f}) site={site.choice}")
    for key, p in top(kind, 4):
        log(f"  {p:5.2f}  {key}")
    if item is not None:
        log(f"  item ({item.confidence:.2f}):")
        for key, p in top(item, 4):
            log(f"  {p:5.2f}  [{key}] {by_index[key].text!r}")
    log(f"  files: {prefix.name}-raw.png, {prefix.name}.png, {prefix.name}-payload.txt, {prefix.name}-answers.json")

    if kind.choice in ("done", "none"):
        log(f"  model says {kind.choice!r}; stopping")
        return False
    if confidence < args.min_confidence:
        log(f"  confidence {confidence:.2f} below {args.min_confidence}; stopping")
        return False
    if not args.act or args.image:
        log(f"  would do: {chosen}. dry run (pass --act without --image to drive the machine)")
        return False

    what = perform(chosen, site.choice, screen, items, email, (typesafe, writer, args.goal, history))
    history.append(what)
    log(f"  did: {what}")
    if "refused" in what or "failed" in what or what == "waited":
        noops[0] += 1
        if noops[0] >= 2:
            log("  two consecutive no-ops; stopping")
            return False
    else:
        noops[0] = 0
    sleep_watching(args.delay)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("goal", help="what you want done on this computer")
    parser.add_argument("--act", action="store_true", help="actually click/type (default: dry run)")
    parser.add_argument("--steps", type=int, default=12, help="max actions before stopping")
    parser.add_argument("--min-confidence", type=float, default=0.4)
    parser.add_argument("--delay", type=float, default=2.0, help="seconds to wait after each action")
    parser.add_argument("--out", type=Path, default=Path("runs") / time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--image", type=Path, help="replay against a saved screenshot instead of capturing (never acts)")
    parser.add_argument("--app", help="frontmost app name to report to the model (for --image replays)")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    global LOG_FILE
    LOG_FILE = args.out / "run.log"
    log(f"run folder: {args.out}")

    email = os.environ.get("CLICKER_EMAIL")
    writer = anthropic.Anthropic() if os.environ.get("ANTHROPIC_API_KEY") else None
    if args.act and not AS.AXIsProcessTrusted():
        sys.exit("this terminal lacks Accessibility permission; grant it in System Settings > Privacy & Security")
    if args.act:
        log("driving the machine. abort: Ctrl-C, or slam the mouse into the top-left corner.")
        if writer is None:
            log("ANTHROPIC_API_KEY not set: type_text will refuse; type_email still works.")

    history: list[str] = []
    noops = [0]
    outcome = "completed"
    started = time.time()
    try:
        with TypeSafeClient() as typesafe:
            for step in range(1, args.steps + 1):
                if not run_step(args, typesafe, writer, step, history, email, noops):
                    break
            else:
                log(f"\nstopped after {args.steps} steps")
                outcome = "step limit"
    except (KeyboardInterrupt, Abort) as e:
        outcome = f"aborted ({e or 'Ctrl-C'})"
        log(f"\naborted ({e or 'Ctrl-C'}) after {len(history)} actions")
    finally:
        (args.out / "run.json").write_text(json.dumps({
            "goal": args.goal, "act": args.act, "steps_taken": len(history), "outcome": outcome,
            "seconds": round(time.time() - started, 1), "history": history,
            "args": {k: str(v) for k, v in vars(args).items()},
        }, indent=2))
        log(f"run folder: {args.out}")
    if outcome.startswith("aborted"):
        sys.exit(130)


if __name__ == "__main__":
    main()
