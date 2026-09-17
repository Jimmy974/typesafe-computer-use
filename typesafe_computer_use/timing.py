"""Phase stopwatches: seconds per phase in a plain dict."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

PHASE_ORDER = ("capture", "screenshot", "app", "window", "field", "url", "ocr", "ax", "decide", "act", "total")
OCR_REGION_PCT = "ocr_region_pct"  # a percentage, not seconds: printed on the ocr phase rather than as its own


@contextmanager
def phase(timing: dict[str, float] | None, name: str) -> Iterator[None]:
    """Record the seconds spent in the block under `name`. A None dict makes this a no-op."""
    started = time.perf_counter()
    try:
        yield
    finally:
        if timing is not None:
            timing[name] = round(time.perf_counter() - started, 3)


def ordered(timing: dict[str, float]) -> list[tuple[str, float]]:
    """Known phases first, in pipeline order, then anything unexpected."""
    known = [(name, timing[name]) for name in PHASE_ORDER if name in timing]
    return known + [(name, seconds) for name, seconds in timing.items() if name not in PHASE_ORDER]


def format_timing(timing: dict[str, float]) -> str:
    """One log line. A zero `act` means the step never acted, so it is left out.

    The share of the capture that was OCRed rides on the `ocr` phase: `ocr 0.31s (22% of screen)`.
    """
    pct = timing.get(OCR_REGION_PCT)
    shown = [(name, s) for name, s in ordered(timing) if name != OCR_REGION_PCT and not (name == "act" and s == 0)]
    parts = [
        f"{name} {seconds:.2f}s" + (f" ({pct:.0f}% of screen)" if name == "ocr" and pct is not None else "")
        for name, seconds in shown
    ]
    return "  timing: " + "  ".join(parts)


def summarize(timings: list[dict[str, float]]) -> dict:
    """Mean and max per phase over the steps that recorded it."""
    names = [name for name, _ in ordered(dict.fromkeys((k for t in timings for k in t), 0.0))]
    mean, peak = {}, {}
    for name in names:
        seen = [t[name] for t in timings if name in t]
        mean[name] = round(sum(seen) / len(seen), 3)
        peak[name] = round(max(seen), 3)
    return {"steps_timed": len(timings), "mean": mean, "max": peak}
