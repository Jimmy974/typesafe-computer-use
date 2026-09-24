"""The step loop, with per-stage timing. This is the thing being benchmarked.

Stage accounting is deliberate and literal:

* `perceive_ms` — cost of producing the element list for this step's decision.
  Zero when the previous action already handed us a fresh observation.
* `decide_ms` — the TypeSafe call.
* `act_ms` — the input event, the post-type verification call if any, **and** the
  observation-until-changed that waits for the action to land.

That last part is why waiting is nearly free: the observation the next decision
needs is the same observation that tells us the action worked. No extra round
trip, and no fixed sleep.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from typesafe_sdk import TypeSafeClient

from ..formdata import FormData
from ..sites import Site, site_for
from ..writer import Writer, compose_browser_text, compose_url, looks_credential
from . import act
from .decide import Decision, available_actions, choose_saved, decide, field_context, verify_typed
from .hands import CdpHands, Hands
from .perceive import Element, Page, perceive
from .report import RunFolder, render_payload

MAX_REPEATS = 3  # the same action, this many times in a row, each leaving the page as it was, ends the run


@dataclass
class Step:
    n: int
    action: str
    detail: str
    confidence: float
    satisfied: float
    elements: int
    changed: bool
    perceive_ms: float
    decide_ms: float
    act_ms: float
    total_ms: float
    text_source: str = ""

    def line(self) -> str:
        ch = "yes" if self.changed else "no "
        return (
            f"{self.n:>3}  {self.action:<12} {self.detail[:40]:<40} "
            f"conf={self.confidence:.2f} chg={ch} el={self.elements:>3}  "
            f"per={self.perceive_ms:>5.1f}  dec={self.decide_ms:>6.1f}  "
            f"act={self.act_ms:>6.1f}  tot={self.total_ms:>6.1f}ms" + (f"  text={self.text_source}" if self.text_source else "")
        )


@dataclass
class RunResult:
    goal: str
    url: str
    outcome: str
    steps: list[Step] = field(default_factory=list)
    wall_ms: float = 0.0
    url_after: str = ""

    def summary(self) -> dict:
        if not self.steps:
            return {"steps": 0}

        def pct(vals: list[float], p: float) -> float:
            s = sorted(vals)
            return round(s[min(len(s) - 1, max(0, round(p / 100 * (len(s) - 1))))], 1)

        totals = [s.total_ms for s in self.steps]
        hot = [s.total_ms for s in self.steps[1:]] or totals  # drop cold-start step
        return {
            "steps": len(self.steps),
            "wall_ms": round(self.wall_ms, 1),
            "perceive_ms": {"p50": pct([s.perceive_ms for s in self.steps], 50)},
            "decide_ms": {"p50": pct([s.decide_ms for s in self.steps], 50), "p95": pct([s.decide_ms for s in self.steps], 95)},
            "act_ms": {"p50": pct([s.act_ms for s in self.steps], 50), "p95": pct([s.act_ms for s in self.steps], 95)},
            "total_ms": {"p50": pct(totals, 50), "p95": pct(totals, 95), "min": round(min(totals), 1)},
            "steps_per_sec_p50": round(1000 / pct(totals, 50), 2) if pct(totals, 50) else None,
            "steps_per_sec_excluding_cold_start": round(1000 / pct(hot, 50), 2) if pct(hot, 50) else None,
        }


def typing_target(page: Page, chosen: int | None, *, strict: bool = False) -> tuple[Element | None, str]:
    """The field to type into, or None and why not.

    The field the classifier named, when it named one; otherwise the first field on
    the page, unless `strict`. A credential field is refused whichever way it was
    reached: the classifier naming it does not make it safe, and falling back to
    another field would put the text somewhere the classifier did not choose.
    Saved values are strict: the first field is often one already filled, and a value
    that does not belong there would be typed over the one that does.
    """
    named = next((e for e in page.items if e.index == chosen and e.field), None)
    target = named if strict else named or next((e for e in page.items if e.field and not e.secret), None)
    if target is None:
        return None, "not a field" if strict else "no field"
    if target.secret or looks_credential(target.label()):
        return None, "refused_credential"
    return target, ""


def resolve_text(writer: Writer | None, goal: str, page: Page, target: Element, history: list[str]) -> tuple[str, str]:
    """Free text for a field, and where it came from. Only the writer composes it.

    The provenance is recorded per step, so a run folder shows whether the writer
    filled the field, declined, or failed.
    """
    if writer is None:
        return "", "no_writer"
    try:
        composed = compose_browser_text(
            writer,
            goal,
            field_label=target.label(),
            page_title=page.title,
            url=page.url,
            nearby_text=field_context(page, int(target.index)),
            history=history,
        )
    except Exception as exc:
        return "", f"writer_error({type(exc).__name__})"
    return (composed, "writer") if composed else ("", "writer_declined")


def resolve_url(writer: Writer | None, goal: str, history: list[str]) -> tuple[str, str]:
    """The address to open for this goal, and where it came from. Only the writer proposes one,
    and `compose_url` returns only a valid https URL."""
    if writer is None:
        return "", "no_writer"
    try:
        url = compose_url(writer, goal, history)
    except Exception as exc:
        return "", f"writer_error({type(exc).__name__})"
    return (url, "writer") if url else ("", "writer_declined")


def run_goal(
    session,
    client: TypeSafeClient,
    goal: str,
    *,
    start_url: str | None = None,
    max_steps: int = 12,
    min_confidence: float = 0.4,
    allow_type: bool = True,
    change_timeout_ms: int = 300,
    verbose: bool = True,
    model: str | None = None,
    writer: Writer | None = None,
    runfolder: RunFolder | None = None,
    sites: list[Site] | None = None,
    hands: Hands | None = None,
    data: FormData | None = None,
) -> RunResult:
    hands = hands or CdpHands(session)
    result = RunResult(goal=goal, url=str(session.evaluate("location.href") or ""), outcome="incomplete")
    if start_url:
        act.navigate(session, start_url)
        act.wait_for_load(session)
        result.url = start_url

    pending: Page | None = None
    history: list[str] = []
    noops = 0
    used: set[str] = set()  # saved fields typed in and matched, so the classifier knows what is left
    repeats = 0  # the same action, in a row, each leaving the page as it was
    last_action: tuple | None = None
    started = time.perf_counter()

    for n in range(1, max_steps + 1):
        step_started = time.perf_counter()

        if pending is None:
            t0 = time.perf_counter()
            page: Page = perceive(session)
            perceive_ms = (time.perf_counter() - t0) * 1000
        else:
            page, perceive_ms = pending, 0.0
            pending = None

        site = site_for(page.url, sites or [])
        # Typing and opening an address need free text, which only the writer composes.
        can_write = writer is not None
        has_data = bool(data and data.fields)
        action_criteria = available_actions(page, allow_type=allow_type, can_write=can_write, has_data=has_data)
        element_criteria_map = {str(e.index): e.label() for e in page.items}

        t0 = time.perf_counter()
        decision: Decision = decide(
            client,
            goal,
            page,
            history,
            allow_type=allow_type,
            can_write=can_write,
            model=model,
            site_notes=site.notes if site else (),
            data=data,
            used=used,
        )
        decide_ms = (time.perf_counter() - t0) * 1000

        if runfolder is not None:
            runfolder.step_payload(
                n,
                render_payload(
                    goal=goal,
                    page=page,
                    history=history,
                    state=decision.state,
                    actions=action_criteria,
                    elements=element_criteria_map,
                ),
            )
            runfolder.step_history(n, history)
            runfolder.step_state(n, decision.state)
            runfolder.step_answers(n, decision.answers)
            runfolder.step_elements(n, page, can_write=can_write)

        fp_before = act.fingerprint(page)
        kind = decision.kind.choice
        detail = ""
        changed = False
        text_source = ""

        t0 = time.perf_counter()
        touched = kind in {"click", "type_text", "press_enter", "press_escape", "back", "navigate", "scroll_down", "scroll_up"}
        if kind == "click":
            idx = decision.chosen_element
            element = next((e for e in page.items if e.index == idx), None)
            if element is None:
                detail = f"click {idx} -> element missing"
                noops += 1
            else:
                detail = hands.click(int(element.index), element, page)
        elif kind == "type_text":
            # The classifier picked the action and the field; the writer supplies the
            # text. The field is checked before the writer is asked, so a credential
            # field is refused whatever the text would have been.
            saving = data is not None and bool(data.fields)
            target, refusal = typing_target(page, decision.chosen_element, strict=saving)
            key = None
            if target is None:
                text, text_source = "", refusal
            elif saving:
                # With saved values, only they are typed, into the field the classifier named: a
                # writer asked for a phone number the file does not hold makes one up. A saved field
                # is typed as written, and named, never shown: the history goes to the classifier.
                saved = choose_saved(client, goal, target.label(), data, used, model=model)
                if saved.choice in data.fields and saved.confidence >= min_confidence:
                    key = saved.choice
                    text, text_source = data.fields[key], f"data:{key}"
                else:
                    text, text_source = "", f"no_saved_fit({saved.choice} {saved.confidence:.2f})"
            else:
                text, text_source = resolve_text(writer, goal, page, target, history)
            if target is None or not text:
                detail = f"type -> {text_source}"
                noops += 1
                touched = False
            else:
                typed = hands.type_text(int(target.index), text)
                value_now = act.field_value(session, int(target.index))
                if key is not None:
                    # Checked here, against the file: the value never goes to the classifier.
                    ok = 1.0 if value_now is not None and value_now.strip() == text.strip() else 0.0
                    shown, verdict = f"<{key}>", "matches" if ok else "does not match"
                else:
                    ok = verify_typed(client, goal, target.label(), text, value_now, model=model)
                    shown, verdict = repr(text), f"verify {ok:.2f}"
                if ok < 0.5:
                    act.clear_field(session, int(target.index))
                    detail = f"type {shown} -> {verdict}, cleared" + (f" ({typed})" if "FAILED" in typed else "")
                    noops += 1
                else:
                    detail = f"type {shown} -> {verdict}"
                    if key is not None:
                        used.add(key)
        elif kind == "press_enter":
            detail = hands.press("enter")
        elif kind == "press_escape":
            detail = hands.press("escape")
        elif kind == "scroll_down":
            detail = act.scroll(session, 3, page)
        elif kind == "scroll_up":
            detail = act.scroll(session, -3, page)
        elif kind == "back":
            detail = act.go_back(session)
        elif kind == "navigate":
            target_url, text_source = resolve_url(writer, goal, history)
            if target_url:
                detail = act.navigate(session, target_url)
            else:
                detail = f"navigate -> {text_source}"
                noops += 1
                touched = False
        elif kind == "wait":
            detail = "waited"
            noops += 1
            touched = False
        else:
            detail = kind
            touched = False

        if touched:
            # The wait for this action and the observation for the next decision
            # are the same call, so this costs nothing extra.
            next_page, _, changed = act.observe_until_changed(session, fp_before, timeout_ms=change_timeout_ms)
            if site and site.settle_ms:
                # A staged site may not answer within the usual window, and its first change is often
                # the tab repainting, not the new page: wait longer for a change, then for it to finish.
                if not changed:
                    next_page, _, changed = act.observe_until_changed(session, fp_before, timeout_ms=site.settle_ms)
                if changed:
                    next_page = act.settle(session, site.settle_ms)
            pending = next_page
        act_ms = (time.perf_counter() - t0) * 1000

        total_ms = (time.perf_counter() - step_started) * 1000
        step = Step(
            n=n,
            action=kind,
            detail=detail,
            confidence=round(decision.confidence, 3),
            satisfied=round(float(decision.satisfied.noul), 3),
            elements=len(page.items),
            changed=changed,
            perceive_ms=perceive_ms,
            decide_ms=decide_ms,
            act_ms=act_ms,
            total_ms=total_ms,
            text_source=text_source,
        )
        result.steps.append(step)
        if verbose:
            print(step.line(), flush=True)
        history.append(f"{kind}: {detail}" + ("" if changed else " (page unchanged)"))
        action = (kind, decision.chosen_element if kind in ("click", "type_text") else None)
        repeats = repeats + 1 if touched and not changed and action == last_action else (1 if touched and not changed else 0)
        last_action = action

        # A doubtful `done` is doubt, not success, so it falls through to the confidence check.
        if (kind == "done" and decision.confidence >= min_confidence) or decision.satisfied.noul >= 0.5:
            result.outcome = "done"
            break
        if kind == "none":
            result.outcome = "blocked"
            break
        if decision.confidence < min_confidence:
            result.outcome = f"low_confidence({decision.confidence:.2f})"
            break
        if noops >= 2:
            result.outcome = "stuck"
            break
        if repeats >= MAX_REPEATS:
            # A click that lands and does nothing is not a noop to the action, only to the page.
            result.outcome = "stalled"
            break
    else:
        result.outcome = "max_steps"

    result.wall_ms = (time.perf_counter() - started) * 1000
    result.url_after = str(session.evaluate("location.href") or "")
    if runfolder is not None:
        runfolder.finish(
            {
                "goal": goal,
                "outcome": result.outcome,
                "url": result.url,
                "url_after": result.url_after,
                "wall_ms": result.wall_ms,
                "summary": result.summary(),
                "steps": [asdict(s) for s in result.steps],
            }
        )
    return result


def save(result: RunResult, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "goal": result.goal,
                "outcome": result.outcome,
                "url_after": result.url_after,
                "wall_ms": result.wall_ms,
                "summary": result.summary(),
                "steps": [asdict(s) for s in result.steps],
            },
            indent=2,
        )
    )
