"""Show exactly what the clicker perceives.

Counts down 3-2-1, captures the main display, then:
  * opens the screenshot with every OCR block outlined and numbered
    (chosen-style red is unused here; blue = OCR block, green = focused field)
  * writes and opens a text file with the state and criteria that would be
    sent to TypeSafe for the given goal

Usage:
  uv run inspect_screen.py                       # goal defaults to a placeholder
  uv run inspect_screen.py "log in to launchdarkly"
  uv run inspect_screen.py --no-open             # just write the files
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

import clicker


def countdown(seconds: int) -> None:
    for n in range(seconds, 0, -1):
        print(f"{n}...", end=" ", flush=True)
        time.sleep(1)
    print("capture")


def render_text(goal: str, screen: clicker.Screen, items: list[clicker.Item]) -> str:
    state = clicker.base_state(goal, screen, items, [])
    kinds = {"click_item": "Click one of the on-screen text items (chosen in the item question)."}
    kinds.update(clicker.fixed_actions(email=None))
    item_criteria = {str(it.index): f"{it.text!r} ({screen.region(it)})" for it in items}
    parts = [
        "=" * 78,
        "STATE  (sent as `state`)",
        "=" * 78,
        json.dumps(state, indent=2),
        "",
        "=" * 78,
        "QUESTION kind  (Choice criteria)",
        "=" * 78,
        json.dumps(kinds, indent=2),
        "",
        "=" * 78,
        "QUESTION item  (Choice criteria)",
        "=" * 78,
        json.dumps(item_criteria, indent=2),
        "",
        "=" * 78,
        "QUESTION site  (Choice criteria)",
        "=" * 78,
        json.dumps({**clicker.SITES, "none": "No website is needed."}, indent=2),
        "",
        "=" * 78,
        f"OCR BLOCKS  ({len(items)} after merge/filter; pixel boxes on the {screen.image.width}x{screen.image.height} capture, scale {screen.scale:g})",
        "=" * 78,
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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("goal", nargs="?", default="(no goal given)")
    parser.add_argument("--countdown", type=int, default=3)
    parser.add_argument("--no-open", action="store_true", help="write files without opening them")
    parser.add_argument("--out", type=Path, default=Path("inspections") / time.strftime("%Y%m%d-%H%M%S"))
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    countdown(args.countdown)
    screen = clicker.capture()
    items = clicker.ocr(screen, clicker.MAX_OPTIONS, args.goal)

    raw = args.out / "raw.png"
    annotated = args.out / "annotated.png"
    text = args.out / "state.txt"
    screen.image.save(raw)
    clicker.annotate(screen, items, chosen="", out=annotated)
    text.write_text(render_text(args.goal, screen, items))

    print(f"app={screen.app!r} url={screen.url!r} blocks={len(items)} field={screen.field.role if screen.field else None}")
    print(f"  {annotated}\n  {text}")
    if not args.no_open:
        subprocess.run(["open", str(annotated)], check=False)
        subprocess.run(["open", "-t", str(text)], check=False)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
