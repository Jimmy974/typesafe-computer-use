"""Screen OCR -> TypeSafe Choice -> mouse click.

Pipeline per step:
  1. screencapture the main display
  2. Apple Vision OCR (via ocrmac) yields text + pixel bounding boxes
  3. one TypeSafe Choice question, criteria keyed by item index
  4. the winning index maps back to a box; its center becomes the click point

Dry-run by default: prints the ranked candidates and writes an annotated
screenshot. Pass --click to actually move the mouse and click.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import Quartz
from ocrmac import ocrmac
from PIL import Image, ImageDraw, ImageFont
from typesafe_sdk import Choice, TypeSafeClient

NONE_KEY = "none"
DONE_KEY = "done"
MIN_OCR_CONFIDENCE = 0.3
MAX_OPTIONS = 253  # 255 minus the two sentinel options


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

    def region(self, item: Item) -> str:
        cx, cy = item.center
        col = ["left", "center", "right"][min(2, int(3 * cx / self.image.width))]
        row = ["top", "middle", "bottom"][min(2, int(3 * cy / self.image.height))]
        return f"{row}-{col}"


def capture(image_path: Path | None = None) -> Screen:
    if image_path is None:
        image_path = Path(tempfile.mkdtemp()) / "screen.png"
        subprocess.run(
            ["screencapture", "-x", "-D", "1", str(image_path)], check=True, capture_output=True
        )
    image = Image.open(image_path).convert("RGB")
    points_wide = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID()).size.width
    return Screen(image=image, scale=image.width / points_wide)


def ocr(screen: Screen) -> list[Item]:
    raw = ocrmac.OCR(screen.image, recognition_level="accurate").recognize(px=True)
    kept = [
        (text.strip(), conf, box)
        for text, conf, box in raw
        if text.strip() and conf >= MIN_OCR_CONFIDENCE
    ]
    # Reading order: bucket rows by the median line height, then left to right.
    heights = sorted(b[3] - b[1] for _, _, b in kept) or [1.0]
    row_h = max(1.0, heights[len(heights) // 2])
    kept.sort(key=lambda r: (round((r[2][1] + r[2][3]) / 2 / row_h), r[2][0]))
    kept = kept[:MAX_OPTIONS]
    return [
        Item(i, text, conf, *box) for i, (text, conf, box) in enumerate(kept)
    ]


def decide(
    client: TypeSafeClient, goal: str, screen: Screen, items: list[Item], history: list[str]
):
    criteria = {
        str(it.index): f"{it.text!r} ({screen.region(it)})" for it in items
    }
    criteria[DONE_KEY] = "The goal is already achieved; nothing more to click."
    criteria[NONE_KEY] = "Nothing on screen helps with the goal right now."

    state = {
        "goal": goal,
        "previous_clicks": history,
        "screen_text_in_reading_order": [
            {"i": it.index, "text": it.text, "where": screen.region(it)} for it in items
        ],
    }
    response = client.system_one(
        state=state,
        questions={
            "click": Choice(
                instructions=(
                    "You control a mouse on this screen. Which item should be clicked "
                    "next to make the most progress toward the goal? Pick exactly one "
                    "item index, or 'done' / 'none'."
                ),
                criteria=criteria,
            )
        },
    )
    return response.answers["click"]


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


def click(screen: Screen, item: Item) -> None:
    px, py = item.center
    point = (px / screen.scale, py / screen.scale)
    for kind in (
        Quartz.kCGEventMouseMoved,
        Quartz.kCGEventLeftMouseDown,
        Quartz.kCGEventLeftMouseUp,
    ):
        event = Quartz.CGEventCreateMouseEvent(None, kind, point, Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)
        time.sleep(0.05)


def ranked(answer, n: int = 5) -> list[tuple[str, float]]:
    return sorted(answer.probabilities.items(), key=lambda kv: -kv[1])[:n]


def run_step(args, client: TypeSafeClient, step: int, history: list[str]) -> bool:
    screen = capture(args.image)
    screen.image.save(args.out / f"step-{step:02d}-raw.png")
    items = ocr(screen)
    if not items:
        print("no text found on screen", file=sys.stderr)
        return False

    answer = decide(client, args.goal, screen, items, history)
    by_index = {str(it.index): it for it in items}

    out = args.out / f"step-{step:02d}.png"
    annotate(screen, items, answer.choice, out)

    print(f"\nstep {step}: {len(items)} OCR items, choice={answer.choice} confidence={answer.confidence:.2f}")
    for key, p in ranked(answer):
        label = by_index[key].text if key in by_index else key
        print(f"  {p:5.2f}  [{key}] {label}")
    print(f"  annotated: {out}")
    if args.json:
        payload = {
            "items": [it.__dict__ for it in items],
            "choice": answer.choice,
            "confidence": answer.confidence,
            "probabilities": answer.probabilities,
        }
        out.with_suffix(".json").write_text(json.dumps(payload, indent=2))

    if answer.choice in (DONE_KEY, NONE_KEY):
        print(f"  model says {answer.choice!r}; stopping")
        return False
    if answer.confidence < args.min_confidence:
        print(f"  confidence below {args.min_confidence}; not clicking")
        return False

    target = by_index[answer.choice]
    cx, cy = target.center
    print(f"  target: {target.text!r} at ({cx / screen.scale:.0f}, {cy / screen.scale:.0f}) pt")
    if not args.click or args.image:
        print("  dry run (pass --click without --image to act)")
        return False

    click(screen, target)
    history.append(target.text)
    time.sleep(args.delay)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("goal", help="what you want done on screen")
    parser.add_argument("--click", action="store_true", help="actually click (default: dry run)")
    parser.add_argument("--steps", type=int, default=1, help="max click steps")
    parser.add_argument("--min-confidence", type=float, default=0.5)
    parser.add_argument("--delay", type=float, default=1.5, help="seconds to wait after a click")
    parser.add_argument("--out", type=Path, default=Path("runs") / time.strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--json", action="store_true", help="also dump OCR items as JSON per step")
    parser.add_argument("--image", type=Path, help="replay against a saved screenshot instead of capturing (never clicks)")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    history: list[str] = []
    with TypeSafeClient() as client:
        for step in range(1, args.steps + 1):
            if not run_step(args, client, step, history):
                break


if __name__ == "__main__":
    main()
