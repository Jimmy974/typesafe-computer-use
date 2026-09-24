"""Benchmark: browser-only computer use, DOM perception vs the original OCR path.

    uv run python -m typesafe_computer_use.browser.bench perception --url https://news.ycombinator.com
    uv run python -m typesafe_computer_use.browser.bench perception --fixture --n 8
    uv run python -m typesafe_computer_use.browser.bench loop --fixture

`perception` is the headline: the same page, the same machine, the same moment.
One path reads the DOM over CDP. The other does what the original does —
`Page.captureScreenshot` -> Apple Vision OCR -> merge blocks -> reading order.
The TypeSafe decision that follows is identical in both, so the difference is
purely perception.

`loop` runs the full end-to-end step loop and reports per-stage latency.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import os
import statistics
import sys
import time
import tomllib
import traceback
from pathlib import Path

from typesafe_sdk import TypeSafeClient

from .. import config
from ..formdata import FormData, load_data
from ..sites import load_sites
from ..writer import make_writer, provider
from . import act
from .cdp import Chrome
from .decide import decide
from .hands import HANDS, CdpHands, PlaywrightHands
from .perceive import perceive
from .report import RunFolder
from .runner import run_goal, save

FIXTURE = Path(__file__).resolve().parents[2] / "bench" / "fixture.html"
TASKS = Path(__file__).resolve().parents[2] / "bench" / "tasks.toml"
TASK_KEYS = {"name", "url", "goal", "expect_url", "window", "steps", "data"}

TASK = (
    "Search for 'invoice automation' in the search box and submit the search. "
    "Then open the guide whose title mentions automation."
)


def _pct(values: list[float], p: float) -> float:
    s = sorted(values)
    return round(s[min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))], 1)


def _norm(text: str) -> str:
    return " ".join(text.lower().split())[:60]


# --------------------------------------------------------------------------- #
def ocr_perception(session, *, budget: int = 120) -> dict:
    """The original pipeline, isolated and timed: screenshot -> Vision OCR -> blocks."""
    from ocrmac import ocrmac
    from PIL import Image

    t0 = time.perf_counter()
    shot = session.call("Page.captureScreenshot", {"format": "png"})
    capture_ms = (time.perf_counter() - t0) * 1000
    image = Image.open(io.BytesIO(base64.b64decode(shot["data"]))).convert("RGB")

    t0 = time.perf_counter()
    raw = ocrmac.OCR(image, recognition_level="accurate").recognize(px=True)
    ocr_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    from ..perception import merge_blocks, to_items

    lines = [(t.strip(), c, b) for t, c, b in raw if t.strip() and c >= 0.0]
    items = to_items(merge_blocks(lines), budget)
    post_ms = (time.perf_counter() - t0) * 1000

    return {
        "capture_ms": capture_ms,
        "ocr_ms": ocr_ms,
        "post_ms": post_ms,
        "total_ms": capture_ms + ocr_ms + post_ms,
        "lines": len(raw),
        "items": len(items),
        "names": [it.text for it in items],
    }


def benchmark_perception(args: argparse.Namespace) -> int:
    url = FIXTURE.as_uri() if args.fixture else args.url
    with Chrome(headed=args.headed) as chrome, chrome.attach() as session:
        act.navigate(session, url)
        act.wait_for_load(session)

        dom, ocr = [], []
        dom_names, ocr_names = [], []
        for i in range(args.n):
            p = perceive(session)
            dom.append(p.elapsed_ms)
            if i == 0:
                dom_names = [it.name for it in p.items]
            if args.with_ocr:
                r = ocr_perception(session)
                ocr.append(r)
                if i == 0:
                    ocr_names = r["names"]
            print(
                f"  pass {i + 1}/{args.n}: dom={p.elapsed_ms:7.1f}ms "
                f"({len(p.items)} elements)"
                + (f"  ocr={ocr[-1]['total_ms']:7.1f}ms ({ocr[-1]['items']} blocks)" if args.with_ocr else ""),
                flush=True,
            )

        dom_p50 = statistics.median(dom)
        out: dict = {
            "url": url,
            "dom": {
                "p50_ms": round(dom_p50, 1),
                "p95_ms": _pct(dom, 95),
                "min_ms": round(min(dom), 1),
                "items": len(dom_names),
            },
            "title": session.evaluate("document.title"),
            "dom_elapsed_for_decision": round(dom_p50, 1),
        }

        print(
            f"\nDOM perception:   p50 {out['dom']['p50_ms']:>7.1f}ms  p95 {out['dom']['p95_ms']:>7.1f}ms  "
            f"({out['dom']['items']} elements)"
        )

        if args.with_ocr and ocr:
            totals = [r["total_ms"] for r in ocr]
            out["ocr"] = {
                "p50_ms": round(statistics.median(totals), 1),
                "p95_ms": _pct(totals, 95),
                "min_ms": round(min(totals), 1),
                "capture_ms": round(statistics.median([r["capture_ms"] for r in ocr]), 1),
                "vision_ocr_ms": round(statistics.median([r["ocr_ms"] for r in ocr]), 1),
                "merge_ms": round(statistics.median([r["post_ms"] for r in ocr]), 1),
                "items": ocr[0]["items"],
                "raw_lines": ocr[0]["lines"],
            }
            o = out["ocr"]
            print(f"OCR perception:   p50 {o['p50_ms']:>7.1f}ms  p95 {o['p95_ms']:>7.1f}ms  ({o['items']} blocks)")
            print(
                f"                  = capture {o['capture_ms']}ms + Vision OCR {o['vision_ocr_ms']}ms + merge {o['merge_ms']}ms"
            )
            speedup = o["p50_ms"] / out["dom"]["p50_ms"] if out["dom"]["p50_ms"] else 0
            out["speedup"] = round(speedup, 1)
            print(f"                  DOM is {speedup:.1f}x faster")

            # Accuracy: does each path even surface the text the task needs?
            probe = args.probe
            dom_hit = any(probe.lower() in _norm(n) for n in dom_names)
            ocr_joined = " | ".join(_norm(n) for n in ocr_names)
            ocr_hit = probe.lower() in ocr_joined
            out["probe"] = {"text": probe, "dom_found": dom_hit, "ocr_found": ocr_hit}
            print(f"\nprobe {probe!r}: DOM found={dom_hit}   OCR found={ocr_hit}")

            dom_text = {_norm(n) for n in dom_names}
            ocr_text = _norm(ocr_joined)
            covered = sum(1 for n in dom_text if n and n in ocr_text)
            out["recall"] = {
                "dom_elements_found_verbatim_by_ocr": covered,
                "dom_elements": len(dom_text),
                "fraction": round(covered / max(1, len(dom_text)), 3),
            }
            print(f"OCR reproduces {covered}/{len(dom_text)} DOM labels verbatim ({out['recall']['fraction'] * 100:.0f}%)")
    print("\n" + json.dumps(out, indent=2, default=str))
    return 0


def benchmark_loop(args: argparse.Namespace) -> int:
    url = FIXTURE.as_uri() if args.fixture else args.url
    goal = args.goal or TASK

    _require_key()
    try:
        writer = make_writer()
    except ValueError as e:
        sys.exit(str(e))
    if writer is None:
        print("writer disabled: no ANTHROPIC_API_KEY or CLICKER_WRITER_BASE_URL; type_text and navigate need one")
    else:
        print(f"writer: {provider(writer)}")

    try:
        sites = load_sites(Path(args.sites))
    except ValueError as e:
        sys.exit(str(e))
    if sites:
        print(f"sites: {', '.join(s.domain for s in sites)} (from {args.sites})")
    try:
        data = load_data(Path(args.data)) if args.data else None
    except (OSError, ValueError) as e:
        sys.exit(str(e))
    if data:
        print(f"data: {len(data.fields)} field(s), {len(data.choices)} choice(s) from {args.data}")

    runfolder = RunFolder.create(args.runs) if args.runs else None
    if runfolder is not None:
        print(f"run folder: {runfolder.root}")

    with (
        Chrome(headed=args.headed) as chrome,
        chrome.attach() as session,
        TypeSafeClient() as client,
        hands_for(args.hands, chrome, session) as hands,
    ):
        print(f"goal: {goal}\nurl:  {url}\nhands: {hands.name}\n")
        print(f"{'#':>3}  {'action':<12} {'detail':<40} {'conf':<9} {'perceive':>8}  {'decide':>8}  {'act':>8}  {'total':>9}")
        print("-" * 118)
        result = run_goal(
            session,
            client,
            goal,
            start_url=url,
            max_steps=args.steps,
            min_confidence=args.min_confidence,
            model=args.model,
            writer=writer,
            runfolder=runfolder,
            sites=sites,
            hands=hands,
            data=data,
        )

    s = result.summary()
    print("-" * 118)
    print(f"outcome: {result.outcome}   steps: {s.get('steps')}   wall: {result.wall_ms:.0f}ms")
    if s.get("steps"):
        print(
            f"per-step  perceive p50 {s['perceive_ms']['p50']}ms   decide p50 {s['decide_ms']['p50']}ms   "
            f"act p50 {s['act_ms']['p50']}ms   TOTAL p50 {s['total_ms']['p50']}ms"
        )
        print(
            f"          -> {s['steps_per_sec_p50']} steps/sec (p50)   p95 {s['total_ms']['p95']}ms   min {s['total_ms']['min']}ms"
        )
        print(f"          -> {s['steps_per_sec_excluding_cold_start']} steps/sec excluding the cold-start step")
    if args.out:
        save(result, Path(args.out))
        print(f"saved: {args.out}")
    return 0


@contextlib.contextmanager
def hands_for(name: str, chrome: Chrome, session):
    """The hands a run acts with. Playwright attaches to this Chrome and lets go when the run ends."""
    if name == "playwright":
        with PlaywrightHands(chrome.origin) as hands:
            yield hands
    else:
        yield CdpHands(session)


def load_tasks(path: Path) -> list[dict]:
    """The task list for `compare`, checked, so a typo fails before any browser starts."""
    tasks = tomllib.loads(path.read_text(encoding="utf-8")).get("task", [])
    if not tasks:
        raise ValueError(f"{path}: no [[task]] entries")
    for t in tasks:
        missing = {"name", "url", "goal", "expect_url"} - set(t)
        unknown = set(t) - TASK_KEYS
        if missing or unknown:
            raise ValueError(f"{path}: task {t.get('name', '?')!r}: missing {sorted(missing)}, unknown {sorted(unknown)}")
    return tasks


def run_task(task: dict, hands: str, *, headed: bool, writer, sites, runs: str | None, data: FormData | None = None) -> dict:
    """One task with one set of hands, in a fresh Chrome. Everything but the hands is shared."""
    url = FIXTURE.as_uri() if task["url"] == "fixture" else task["url"]
    window = tuple(task["window"]) if "window" in task else None
    runfolder = RunFolder.create(runs) if runs else None
    try:
        with (
            Chrome(headed=headed, window=window) as chrome,
            chrome.attach() as session,
            TypeSafeClient() as client,
            hands_for(hands, chrome, session) as h,
        ):
            result = run_goal(
                session,
                client,
                task["goal"],
                start_url=url,
                max_steps=int(task.get("steps", 12)),
                verbose=False,
                writer=writer,
                runfolder=runfolder,
                sites=sites,
                hands=h,
                data=data,
            )
    except Exception as e:  # a crash is a failed run, and the comparison goes on
        return {
            "task": task["name"],
            "hands": hands,
            "passed": False,
            "outcome": f"crashed: {e}"[:120],
            "steps": 0,
            "traceback": traceback.format_exc(),
        }
    clicks = [s.act_ms for s in result.steps if s.action == "click"]
    return {
        "task": task["name"],
        "hands": hands,
        "passed": task["expect_url"] in result.url_after,
        "outcome": result.outcome,
        "steps": len(result.steps),
        "wall_ms": round(result.wall_ms),
        "click_act_ms_p50": _pct(clicks, 50) if clicks else None,
        "url_after": result.url_after,
        "run": str(runfolder.root) if runfolder else None,
    }


def cmd_compare(args: argparse.Namespace) -> int:
    """The same tasks with each set of hands, alternating, so a slow minute on a site costs both alike."""
    _require_key()
    try:
        tasks = load_tasks(Path(args.tasks))
        sites = load_sites(Path(args.sites))
        writer = make_writer()
    except ValueError as e:
        sys.exit(str(e))
    hands = [h.strip() for h in args.hands.split(",")]
    if bad := [h for h in hands if h not in HANDS]:
        sys.exit(f"unknown hands {bad}; choose from {', '.join(HANDS)}")
    if args.only:
        tasks = [t for t in tasks if t["name"] in args.only.split(",")]
    try:  # a task's data file is relative to the task list, and read before any browser starts
        datas = {t["name"]: load_data(Path(args.tasks).parent / t["data"]) for t in tasks if "data" in t}
    except (OSError, ValueError) as e:
        sys.exit(str(e))
    print(
        f"{len(tasks)} tasks x {len(hands)} hands x {args.repeat} repeat(s); writer: {provider(writer) if writer else 'none'}\n"
    )

    rows: list[dict] = []
    for task in tasks:
        for r in range(args.repeat):
            for h in hands if r % 2 == 0 else list(reversed(hands)):
                row = run_task(
                    task, h, headed=args.headed, writer=writer, sites=sites, runs=args.runs, data=datas.get(task["name"])
                )
                rows.append(row)
                mark = "PASS" if row["passed"] else "fail"
                click = f"{row['click_act_ms_p50']:.0f}ms" if row.get("click_act_ms_p50") is not None else "-"
                print(
                    f"  {task['name']:<22} {h:<11} {mark}  {row['outcome']:<20} steps={row['steps']:<3} "
                    f"wall={row.get('wall_ms', 0) / 1000:5.1f}s  click p50={click}",
                    flush=True,
                )

    print("\nSUMMARY")
    print(f"  {'hands':<11} {'passed':>9}  {'wall p50':>9}  {'click p50':>10}  {'steps p50':>9}")
    summary = {}
    for h in hands:
        mine = [r for r in rows if r["hands"] == h]
        passed = sum(r["passed"] for r in mine)
        walls = [r["wall_ms"] for r in mine if "wall_ms" in r]
        clicks = [r["click_act_ms_p50"] for r in mine if r.get("click_act_ms_p50") is not None]
        steps = [r["steps"] for r in mine if r["passed"]]
        summary[h] = {
            "passed": passed,
            "runs": len(mine),
            "wall_ms_p50": _pct(walls, 50) if walls else None,
            "click_act_ms_p50": _pct(clicks, 50) if clicks else None,
            "steps_p50_when_passed": _pct(steps, 50) if steps else None,
        }
        s = summary[h]
        print(
            f"  {h:<11} {passed:>4}/{len(mine):<4}  {(s['wall_ms_p50'] or 0) / 1000:8.1f}s  "
            f"{s['click_act_ms_p50'] or 0:8.0f}ms  {s['steps_p50_when_passed'] or 0:>9}"
        )

    out = Path(args.runs or ".") / time.strftime("compare-%Y%m%d-%H%M%S.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "runs": rows}, indent=2), encoding="utf-8")
    print(f"\nsaved: {out}")
    return 0


def _saved_data(state: dict) -> dict:
    if "saved_fields" not in state and "saved_choices" not in state:
        return {}
    data, used = FormData.from_state(state)
    return {"data": data, "used": used}


def cmd_replay(args: argparse.Namespace) -> int:
    """Re-decide a saved step without touching a browser.

    This is the run-folder contract: same input, and you can see whether the
    decision changed. It also proves the replay is faithful by rebuilding the
    state from the saved elements and comparing it to what was originally sent.
    """
    from .report import load_step, render_answers

    _require_key()
    step = load_step(args.run, args.step)
    page = step["page"]
    print(f"run:  {args.run}")
    print(f"step: {args.step}   goal: {step['goal']!r}")
    print(f"page: {page.title!r}  {page.url}")
    print(f"      {len(page.items)} elements, scroll_y={page.scroll_y}, {page.below_fold} below the fold")
    print(f"      history: {len(step['history'])} prior action(s)\n")

    with TypeSafeClient() as client:
        # Mirror the runner exactly: the history is part of the state that was sent, and
        # whether a writer was there decides the action set offered.
        decision = decide(
            client,
            step["goal"],
            page,
            history=step["history"],
            can_write=step["can_write"],
            model=args.model,
            # From the saved state, not today's site files, so an edited file cannot make the replay unfaithful.
            site_notes=tuple(step["state"].get("site_notes") or ()),
            # The saved state holds field names only, which is all the decision reads.
            **_saved_data(step["state"]),
        )

    if step["state"] and decision.state != step["state"]:
        print("WARNING: reconstructed state differs from the saved one — replay is NOT faithful\n")
    else:
        print("replay is faithful: reconstructed state == saved state\n")

    print("SAVED ANSWERS")
    print(render_answers(step["answers"]))
    print("REPLAYED NOW")
    print(render_answers(decision.answers))
    saved_kind = (step["answers"].get("kind") or {}).get("choice")
    verdict = "identical" if saved_kind == decision.kind.choice else "CHANGED"
    print(f"decision: saved={saved_kind}  now={decision.kind.choice}  {verdict}")
    return 0


DOTENV = Path.cwd() / ".env"


def _require_key() -> None:
    """The TypeSafe key, resolved the way `clicker` resolves it: the environment, then ./.env."""
    if not os.environ.get("TYPESAFE_API_KEY"):
        sys.exit("TYPESAFE_API_KEY is not set (export it or put it in .env)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="clicker-bench",
        description=(
            "Browser-only computer-use benchmarks: DOM perception vs the OCR path, the "
            "end-to-end step loop, and offline replay of a saved step."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  clicker-bench perception --url https://news.ycombinator.com\n"
            "  clicker-bench perception --fixture --n 8\n"
            "  clicker-bench loop --fixture --runs runs\n"
            "  clicker-bench compare --repeat 2\n"
            "  clicker-bench replay --run runs/<ts> --step 2"
        ),
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("perception", help="DOM vs OCR perception, same page")
    p.add_argument("--url", default="https://news.ycombinator.com")
    p.add_argument("--fixture", action="store_true", help="use the local deterministic fixture page")
    p.add_argument("--n", type=int, default=5)
    p.add_argument("--with-ocr", action="store_true", default=True)
    p.add_argument("--no-ocr", dest="with_ocr", action="store_false")
    p.add_argument("--probe", default="Search", help="text the task needs; checked in both paths")
    p.add_argument("--headed", action="store_true")
    p.set_defaults(func=benchmark_perception)

    q = sub.add_parser("loop", help="end-to-end step loop")
    q.add_argument("--url", default="https://en.wikipedia.org/wiki/Singapore")
    q.add_argument("--fixture", action="store_true")
    q.add_argument("--goal", default=None)
    q.add_argument("--steps", type=int, default=10)
    q.add_argument("--min-confidence", type=float, default=0.4)
    q.add_argument("--model", default=None)
    q.add_argument("--headed", action="store_true")
    q.add_argument("--out", default=None)
    q.add_argument("--runs", default=None, help="write a replayable run folder under this directory")
    q.add_argument("--sites", default="sites", help="folder of <domain>.toml site files (default: ./sites)")
    q.add_argument("--hands", default="cdp", choices=HANDS, help="what carries out clicks, typing and keys")
    q.add_argument("--data", default=None, help="a .toml of [fields] to type and [choices] to pick, for a form")
    q.set_defaults(func=benchmark_loop)

    c = sub.add_parser("compare", help="the same tasks with each set of hands, side by side")
    c.add_argument("--tasks", default=str(TASKS), help="task list (default: bench/tasks.toml)")
    c.add_argument("--hands", default=",".join(HANDS), help="comma-separated, from: " + ", ".join(HANDS))
    c.add_argument("--only", default=None, help="comma-separated task names to run")
    c.add_argument("--repeat", type=int, default=1)
    c.add_argument("--sites", default="sites")
    c.add_argument("--runs", default="runs", help="where run folders and the results JSON go")
    c.add_argument("--headed", action="store_true")
    c.set_defaults(func=cmd_compare)

    r = sub.add_parser("replay", help="re-decide a saved step offline, from a run folder")
    r.add_argument("--run", required=True, help="runs/<timestamp> directory")
    r.add_argument("--step", type=int, required=True)
    r.add_argument("--model", default=None)
    r.set_defaults(func=cmd_replay)

    args = ap.parse_args(argv)
    config.load_dotenv(DOTENV)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
