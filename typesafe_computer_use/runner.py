"""The step loop and the run folder."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import anthropic
from typesafe_sdk import TypeSafeClient

from . import macos
from .actions import Context, perform
from .config import DEFAULT_DELAY, DEFAULT_MIN_CONFIDENCE, DEFAULT_STEPS, MAX_OPTIONS
from .decide import Decision, decide, offscreen_records
from .models import Abort, Item, Screen, Signature, same_screen, signature
from .perception import OcrCache, capture, perceive
from .report import Log, annotate, ax_count, render_payload, top
from .timing import format_timing, phase, summarize
from .writer import Answer, compose_answer

# Two ways a run stalls, both read off the screen rather than off the history line, because an
# action's description says what was attempted and only the next capture says what came of it.
MAX_IDLE = 3  # consecutive actions that left the screen as it was: refusals, waits on a spinner, scrolls at the bottom
MAX_REPEATS = 2  # consecutive actions already taken on the same screen earlier in the run: a cycle, or a click that does nothing
EARLIER_SCREENS = 2  # screens before the last one that the answer may also be read from

# The outcomes that end with an answer, each in words the writer can pass on. A dry run took no
# action and an abort is the user's own stop, so neither has anything to report.
STOPPED = {
    "done": "the classifier judged the goal already achieved on this screen",
    "nothing helps": "the classifier found nothing on this screen that helps with the goal",
    "low confidence": "the classifier was not confident enough in any next action",
    "stalled": "the last actions changed nothing",
    "step limit": "the run used every step it was allowed",
}


@dataclass
class RunConfig:
    goal: str
    out: Path
    act: bool = False
    steps: int = DEFAULT_STEPS
    min_confidence: float = DEFAULT_MIN_CONFIDENCE
    delay: float = DEFAULT_DELAY
    image: Path | None = None  # replay a saved capture (never acts)
    app: str | None = None  # frontmost app to report during replay
    url: str | None = None  # browser URL to report during replay

    @property
    def replay(self) -> bool:
        return self.image is not None


@dataclass
class RunState:
    history: list[str] = field(default_factory=list)
    timings: list[dict[str, float]] = field(default_factory=list)
    idle: int = 0  # actions in a row that changed nothing on screen
    repeats: int = 0  # actions in a row already taken on the same screen
    last: Signature | None = None  # the screen the last action was taken on
    seen: list[tuple[Signature, str]] = field(default_factory=list)  # every (screen, action) pair so far
    outcome: str = "crashed"  # every way out of the loop names its own; only an exception leaves this
    ocr_cache: OcrCache = field(default_factory=OcrCache)  # carries one step's OCR into the next
    view: tuple[Screen, list[Item]] | None = None  # the latest capture, until an action makes it stale
    answer: Answer | None = None


def run(cfg: RunConfig, ctx_factory) -> RunState:
    """Drive the loop. ctx_factory(typesafe, history) builds the action Context."""
    cfg.out.mkdir(parents=True, exist_ok=True)
    log = Log(cfg.out / "run.log")
    log(f"run folder: {cfg.out}")
    if cfg.act:
        log("driving the machine. abort: Ctrl-C, or slam the mouse into the top-left corner.")

    state = RunState()
    started = time.time()
    try:
        with TypeSafeClient() as typesafe:
            ctx = ctx_factory(typesafe, state.history)
            for step in range(1, cfg.steps + 1):
                if not run_step(cfg, ctx, state, step, log):
                    break
            else:
                log(f"\nstopped after {cfg.steps} steps")
                state.outcome = "step limit"
            conclude(cfg, ctx, state, log)
    except (KeyboardInterrupt, Abort) as e:
        state.outcome = f"aborted ({e or 'Ctrl-C'})"
        log(f"\n{state.outcome} after {len(state.history)} actions")
    finally:
        summary = {
            "goal": cfg.goal,
            "act": cfg.act,
            "steps_taken": len(state.history),
            "outcome": state.outcome,
            "answer": state.answer.text if state.answer else None,
            "goal_achieved": state.answer.achieved if state.answer else None,
            "seconds": round(time.time() - started, 1),
            "timing": summarize(state.timings),
            "history": state.history,
            "config": {k: str(v) for k, v in asdict(cfg).items()},
        }
        (cfg.out / "run.json").write_text(json.dumps(summary, indent=2))
        log(f"run folder: {cfg.out}")
    return state


def conclude(cfg: RunConfig, ctx: Context, state: RunState, log: Log) -> None:
    """Hand the screen the run ended on to the writer, for the answer the classifier cannot put into words.

    The last step's capture serves when nothing acted after it. An action makes it stale, so the
    screen is captured again, and saved so the answer can be checked against what it was read from.
    """
    stopped = STOPPED.get(state.outcome)
    if stopped is None:
        return
    if ctx.writer is None:
        log("\nno answer: the writer is disabled (set ANTHROPIC_API_KEY)")
        return
    started = time.perf_counter()
    if state.view is None:
        macos.check_abort()
        screen = capture(cfg.image, cfg.app, cfg.url, ctx.browser)
        screen.image.save(cfg.out / "answer-raw.png")
        state.view = (screen, perceive(screen, MAX_OPTIONS, cfg.goal))
    screen, items = state.view
    earlier = earlier_screens(state, signature(screen, items))
    try:
        state.answer = compose_answer(ctx.writer, cfg.goal, screen, items, state.history, stopped, earlier)
    except anthropic.APIError as e:
        log(f"\nno answer: the writer failed ({e})")
        return
    verdict = "goal achieved" if state.answer.achieved else "goal not achieved"
    log(f"\nanswer ({verdict}, {time.perf_counter() - started:.1f}s):\n  {state.answer.text}")


def earlier_screens(state: RunState, final: Signature, limit: int = EARLIER_SCREENS) -> list[dict]:
    """The last few distinct screens the run passed through before the one it ended on, oldest first.

    The goal may ask for something that was on the way (a price on the listing, not on the checkout),
    and the run's own captures are the only place the answer may come from.
    """
    out: list[Signature] = []
    for seen, _ in reversed(state.seen):
        if len(out) == limit:
            break
        if not same_screen(seen, final) and not any(same_screen(seen, kept) for kept in out):
            out.append(seen)
    return [{"app": app, "url": url, "text": [text for text, _ in lines]} for app, url, _, lines in reversed(out)]


def run_step(cfg: RunConfig, ctx: Context, state: RunState, step: int, log: Log) -> bool:
    macos.check_abort()
    timing: dict[str, float] = {}
    started = time.perf_counter()
    with phase(timing, "capture"):
        screen = capture(cfg.image, cfg.app, cfg.url, ctx.browser, timing)
    items = perceive(screen, MAX_OPTIONS, cfg.goal, timing, None if cfg.replay else state.ocr_cache)
    state.view = (screen, items)
    if not screen_moved(state, screen, items, log):
        return False
    tried = tried_here(state)
    prefix = cfg.out / f"step-{step:03d}"  # three digits, so a run of 100 steps still lists in order
    screen.image.save(prefix.with_name(prefix.name + "-raw.png"))
    prefix.with_name(prefix.name + "-payload.txt").write_text(
        render_payload(cfg.goal, screen, items, state.history, ctx.browser, ctx.email, tried)
    )

    with phase(timing, "decide"):
        decision = decide(ctx.typesafe, cfg.goal, screen, items, state.history, ctx.browser, ctx.email, tried)
    by_index = {str(it.index): it for it in items}
    annotate(screen, items, decision.chosen, prefix.with_suffix(".png"))

    field_desc = f" field={screen.field.role}:{screen.field.label!r}" if screen.field else ""
    log(
        f"\nstep {step}: app={screen.app!r}{field_desc} url={screen.url!r} items={len(items)} ax={ax_count(items)} "
        f"offscreen={len(screen.offscreen)} kind={decision.kind.choice} ({decision.kind.confidence:.2f}) "
        f"site={decision.site.choice}"
    )
    for key, p in top(decision.kind, 4):
        log(f"  {p:5.2f}  {key}")
    if decision.item is not None:
        log(f"  item ({decision.item.confidence:.2f}):")
        for key, p in top(decision.item, 4):
            log(f"  {p:5.2f}  [{key}] {by_index[key].text!r}")
    if decision.offscreen is not None:
        log(f"  offscreen ({decision.offscreen.confidence:.2f}):")
        for key, p in top(decision.offscreen, 3):
            log(f"  {p:5.2f}  [{key}] {screen.offscreen[int(key)].label!r}")

    keep_going = resolve(cfg, ctx, state, screen, items, decision, timing, log)
    timing.setdefault("act", 0.0)
    timing["total"] = round(time.perf_counter() - started, 3)
    state.timings.append(timing)

    prefix.with_name(prefix.name + "-answers.json").write_text(json.dumps(answers(decision, screen, items, timing), indent=2))
    log(f"  files: {prefix.name}-raw.png, {prefix.name}.png, {prefix.name}-payload.txt, {prefix.name}-answers.json")
    log(format_timing(timing))

    if state.view is None:  # an action ran: let the screen settle before the next step, or the answer, reads it
        macos.sleep_watching(cfg.delay)
    return keep_going


def resolve(
    cfg: RunConfig,
    ctx: Context,
    state: RunState,
    screen: Screen,
    items: list[Item],
    decision: Decision,
    timing: dict[str, float],
    log: Log,
) -> bool:
    """Apply the stop rules, then the action. True to keep looping."""
    if decision.stops:
        log(f"  model says {decision.kind.choice!r}; stopping")
        state.outcome = "done" if decision.kind.choice == "done" else "nothing helps"
        return False
    if decision.confidence < cfg.min_confidence:
        log(f"  confidence {decision.confidence:.2f} below {cfg.min_confidence}; stopping")
        state.outcome = "low confidence"
        return False
    if not cfg.act or cfg.replay:
        log(f"  would do: {decision.chosen}. dry run (pass --act without --image to drive the machine)")
        state.outcome = "dry run"
        return False

    with phase(timing, "act"):
        what = perform(decision, screen, items, ctx)
    state.view = None
    state.history.append(what)
    log(f"  did: {what}")
    return not repeating(state, what, log)


def screen_moved(state: RunState, screen: Screen, items: list[Item], log: Log) -> bool:
    """Count the actions that left the screen as it was, and stop once too many did in a row.

    The capture is the only witness to what an action did. Compared with the one the action was
    taken on, an unchanged screen means a refused action, a wait on a page still loading, or a
    scroll that has run out of page; any of them is fine a couple of times.
    """
    now = signature(screen, items)
    if state.last is not None:
        state.idle = state.idle + 1 if same_screen(now, state.last) else 0
    state.last = now
    if state.idle >= MAX_IDLE:
        log(f"  the last {MAX_IDLE} actions changed nothing on screen; stopping")
        state.outcome = "stalled"
        return False
    return True


def tried_here(state: RunState) -> list[str]:
    """The actions already taken on the screen now showing, oldest first, for the classifier to steer around."""
    return [what for seen, what in state.seen if state.last is not None and same_screen(seen, state.last)]


def repeating(state: RunState, what: str, log: Log) -> bool:
    """Count the actions already taken on the same screen earlier, and stop once too many run in a row.

    The same action on the same screen led somewhere once, and this is where it led: back here.
    That is a cycle through two pages as much as a button that does nothing. A wait is exempt,
    since waiting is repeating by design; the idle count bounds it instead.
    """
    if what != "waited":
        state.repeats = state.repeats + 1 if what in tried_here(state) else 0
    state.seen.append((state.last, what))
    if state.repeats >= MAX_REPEATS:
        log(f"  {MAX_REPEATS} actions in a row already taken on the same screen; stopping")
        state.outcome = "stalled"
        return True
    return False


def answers(decision: Decision, screen: Screen, items: list[Item], timing: dict[str, float]) -> dict:
    """What the classifier returned for this step, plus what it cost."""
    return {
        "kind": decision.kind.choice,
        "kind_confidence": decision.kind.confidence,
        "kind_probabilities": decision.kind.probabilities,
        "item": decision.item.choice if decision.item else None,
        "item_confidence": decision.item.confidence if decision.item else None,
        "item_probabilities": decision.item.probabilities if decision.item else None,
        "site": decision.site.choice,
        "site_probabilities": decision.site.probabilities,
        "offscreen": decision.offscreen.choice if decision.offscreen else None,
        "offscreen_probabilities": decision.offscreen.probabilities if decision.offscreen else None,
        "offscreen_controls": offscreen_records(screen.offscreen),
        "chosen": decision.chosen,
        "confidence": decision.confidence,
        "timing": timing,
        "items": [asdict(it) for it in items],
        "field": screen.field.record() if screen.field else None,
        "app": screen.app,
        "url": screen.url,
    }
