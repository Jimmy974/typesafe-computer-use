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
import subprocess
import sys
import time
from pathlib import Path

import clicker


def countdown(seconds: int) -> None:
    for n in range(seconds, 0, -1):
        print(f"{n}...", end=" ", flush=True)
        time.sleep(1)
    print("capture")


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
    text.write_text(clicker.render_payload(args.goal, screen, items, [], None))

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
