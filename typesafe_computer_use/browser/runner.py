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
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from typesafe_sdk import ChoiceAnswer, NoulAnswer, TypeSafeClient

from .. import config
from ..formdata import FormData
from ..sites import Site, site_for
from ..writer import Writer, compose_browser_text, compose_url, looks_credential
from . import act
from .cdp import CDPError
from .decide import (
    NO_SAVED_VALUE,
    Decision,
    available_actions,
    choose_option,
    choose_saved,
    decide,
    field_context,
    verify_typed,
)
from .form_login import auto_login, auto_otp, inspect_login_page, is_manual, wait_for_login_result, wait_for_otp_result
from .hands import CdpHands, Hands
from .perceive import Element, Page, perceive
from .report import RunFolder, render_payload

MAX_REPEATS = 3  # the same action, this many times in a row, each leaving the page as it was, ends the run
UNTICK_CONFIDENCE = 0.9  # how sure a click must be to untick a ticked box
TARGETING_KINDS = ("click", "type_text", "select_option")  # the actions that act on one element
HARMLESS_KINDS = ("scroll_down", "scroll_up", "wait")  # nothing on the site changes: done even when doubtful
MAX_UPLOADS = 20  # files put into inputs in a run, so a page that empties its input cannot loop
WAIT_MS = 5000  # how long the loop's `wait` watches the page for a change
MAX_FORM_OTPS = 2  # fixed codes typed in a run, so a code the site turns down is not typed forever
MAX_FORM_LOGINS = 2  # login forms signed in after the first page, so a form that keeps coming back cannot loop


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

    The field the classifier named, when it named one; otherwise the first empty field
    on the page, unless `strict`. A field that already holds something is never the
    fallback: text that does not fit is cleared, and the clearing takes what was there. A credential field is refused whichever way it was
    reached: the classifier naming it does not make it safe, and falling back to
    another field would put the text somewhere the classifier did not choose.
    Saved values are strict: the first field is often one already filled, and a value
    that does not belong there would be typed over the one that does.
    """
    named = next((e for e in page.items if e.index == chosen and e.field), None)
    target = named if strict else named or next((e for e in page.items if e.field and not e.secret and not e.filled), None)
    if target is None:
        return None, "not a field" if strict else "no field"
    if target.secret or looks_credential(target.label()):
        return None, "refused_credential"
    return target, ""


def _bare(name: str) -> str:
    return " ".join(name.replace("*", " ").split()).casefold()


def saved_by_name(field: Element, saved: dict[str, str]) -> str | None:
    """The one saved field named exactly as this field is, in its section, or None.

    A name may start with a section and a colon, 'Guardian: First Name (English)'; it then fits
    only a field whose heading holds that section. A name without one fits only a field under
    none of those sections. Case, spacing and a required-field star do not count. Asking the
    classifier to match a name to itself only lets it doubt."""
    return _saved_in_section(field, saved, lambda name, value: _bare(name) == _bare(field.name))


def saved_for_radio(field: Element, choices: dict[str, str]) -> str | None:
    """The one saved choice whose value is this radio button's own label, in its section, or None:
    'Gender' = 'Male' is the radio labelled Male; 'Guardian: Born in' = 'Ethiopia' is the one
    labelled Ethiopia under the guardian's heading."""
    fits = lambda name, value: _bare(value) == _bare(field.name)  # noqa: E731
    if field.group:
        # Two saved choices may share a value, 'Born in' and 'Country' both Ethiopia: the one
        # named as the radio group's question is the one it answers.
        asked = _saved_in_section(field, choices, lambda name, value: fits(name, value) and _bare(name) == _bare(field.group))
        if asked:
            return asked
    return _saved_in_section(field, choices, fits)


def _saved_in_section(field: Element, saved: dict[str, str], fits_name) -> str | None:
    sections = {_bare(k.split(":", 1)[0]) for k in saved if ":" in k}
    heading = _bare(field.section)
    own = next((s for s in sections if s and s in heading), None)
    found = []
    for key, value in saved.items():
        prefix, sep, rest = key.partition(":")
        if sep and _bare(prefix) in sections:
            fits = own == _bare(prefix) and fits_name(rest, value)
        else:
            fits = own is None and fits_name(key, value)
        if fits:
            found.append(key)
    return found[0] if len(found) == 1 else None


@dataclass(frozen=True)
class FromData:
    """A step the data file settles by itself: the element, and for a list, the option."""

    kind: str
    index: int
    key: str
    option: int | None = None


FROM_DATA_TRIES = 2  # one element filled from the data this often at most, so a page that refuses cannot loop


def next_from_data(session, page: Page, data: FormData, tries: dict[tuple, int]) -> FromData | None:
    """The first control on screen, top to bottom, that the data says what to do with and that
    does not show it yet: an empty field named as a saved field, a list not set to its saved
    choice, a radio button whose label is a saved choice and is not chosen. The classifier is not
    asked about these: what goes where is written down, and each question is a chance to slip."""
    for e in page.items:
        if not e.in_view or e.covered or tries.get((e.section, e.name), 0) >= FROM_DATA_TRIES:
            continue
        found = None
        if e.typeable and not e.filled and data.fields and (key := saved_by_name(e, data.fields)):
            found = FromData("type_text", e.index, key)
        elif e.tag == "select" and data.choices and (key := saved_by_name(e, data.choices)):
            want = _bare(data.choices[key])
            if not (e.chosen and _bare(e.chosen) == want):
                option = next((i for i, text in act.select_options(session, e.index) if _bare(text) == want), None)
                if option is not None:
                    found = FromData("select_option", e.index, key, option)
        elif e.role == "radio" and e.checked is False and data.choices and (key := saved_for_radio(e, data.choices)):
            found = FromData("click", e.index, key)
        elif _is_card(e) and data.choices and (key := saved_for_card(e, data.choices)):
            # A card shows no chosen state to read, and a second press may unchoose it: once, and
            # once more only while the page still asks for that choice by name.
            pressed = tries.get((e.section, e.name), 0)
            asked = _bare(key.rpartition(":")[2]) in " ".join(_bare(m) for m in page.messages)
            if pressed == 0 or (pressed < FROM_DATA_TRIES and asked):
                tries[(e.section, e.name)] = pressed + 1
                return FromData("click", e.index, key)
        if found:
            tries[(e.section, e.name)] = tries.get((e.section, e.name), 0) + 1
            return found
    return None


def _is_card(e: Element) -> bool:
    """A choice drawn as a pressable box, not a real radio: its caption comes first in its name."""
    return e.checked is None and not e.field and e.tag != "select" and not e.href and (e.role == "button" or e.tag == "div")


def saved_for_card(field: Element, choices: dict[str, str]) -> str | None:
    """The one saved choice whose value opens this card's name, in its section: 'Service type' =
    'NORMAL Service' is the card 'NORMAL Service Processing time: ...'."""
    return _saved_in_section(
        field, choices, lambda name, value: bool(_bare(value)) and _bare(field.name).startswith(_bare(value) + " ")
    )


def page_says(session, text: str) -> bool:
    """Whether the page's visible text holds `text`, ignoring case and spacing."""
    wanted = json.dumps(" ".join(text.split()).casefold())
    try:
        return (
            session.evaluate(
                f"(document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').toLowerCase().includes({wanted})"
            )
            is True
        )
    except CDPError:
        return False


def saved_match(value_now: str | None, saved: str) -> str:
    """How a field compares with the saved value typed into it, in words that hold neither.

    A page that capitalises a name as it is typed has taken it, so a difference of case alone is
    a match. Anything else says only what kind of difference it is."""
    now, want = (value_now or "").strip(), saved.strip()
    if now == want:
        return "matches"
    if now.casefold() == want.casefold():
        return "matches, cased by the page"
    if not now:
        return "does not match: the field is empty"
    if want in now:
        return "does not match: the field holds more than the value"
    if now in want:
        return "does not match: the field holds only part of the value"
    return "does not match: the field holds something else"


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


def _finish_result(result: RunResult, session, started: float, runfolder: RunFolder | None) -> RunResult:
    result.wall_ms = (time.perf_counter() - started) * 1000
    try:
        result.url_after = str(session.evaluate("location.href") or "")
    except CDPError:
        result.url_after = ""  # the browser stopped answering; the run's record still stands
    if runfolder is not None:
        runfolder.finish(
            {
                "goal": result.goal,
                "outcome": result.outcome,
                "url": result.url,
                "url_after": result.url_after,
                "wall_ms": result.wall_ms,
                "summary": result.summary(),
                "steps": [asdict(s) for s in result.steps],
            }
        )
    return result


def _manual_outcome(state: str) -> str:
    """form_login_manual_required, with the page script's reason when it gave one: (captcha), (password_fields_2)."""
    reason = state.partition(":")[2]
    return f"form_login_manual_required({reason})" if reason else "form_login_manual_required"


def _prepare_form_login(session, login: tuple[str, str, str], manual_login: Callable[[], bool] | None) -> str:
    host = login[0]
    state = auto_login(session, *login)
    if state == "submitted":
        act.wait_for_load(session)
        state = wait_for_login_result(session, host)
    if is_manual(state) and manual_login is not None and manual_login():
        state = inspect_login_page(session, host)
        if state == "login_form":
            state = auto_login(session, *login)
            if state == "submitted":
                act.wait_for_load(session)
                state = wait_for_login_result(session, host)
    return state


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
    manual_login: Callable[[], bool] | None = None,
    done_text: str | None = None,
) -> RunResult:
    hands = hands or CdpHands(session)
    result = RunResult(goal=goal, url=str(session.evaluate("location.href") or ""), outcome="incomplete")
    if start_url:
        act.navigate(session, start_url)
        act.wait_for_load(session)
        result.url = start_url

    pending: Page | None = None
    history: list[str] = []
    noops = 0  # steps in a row that did nothing: two end the run
    used: set[str] = set()  # saved fields typed in and matched, so the classifier knows what is left
    repeats = 0  # the same action, in a row, each leaving the page as it was
    last_action: tuple | None = None
    started = time.perf_counter()

    logins = 0  # login forms met after the first page, each signed in by the form login path
    otps = 0  # one-time codes typed from CLICKER_FORM_OTP
    uploads = 0  # saved files put into file inputs
    uploaded: set[str] = set()  # where they went, by the text around the input: once each
    from_data_tries: dict[tuple, int] = {}  # (section, name) -> steps the data file settled for it
    dialogs_seen = 0
    tried: dict[tuple, int] = {}  # (page as it was, action) -> how often: a loop comes back to both
    otp = config.form_otp()
    if login := config.form_login():
        login_state = _prepare_form_login(session, login, manual_login)
        if is_manual(login_state):
            result.outcome = _manual_outcome(login_state)
            return _finish_result(result, session, started, runfolder)
        if login_state == "login_failed":
            result.outcome = "form_login_failed"
            return _finish_result(result, session, started, runfolder)

    for n in range(1, max_steps + 1):
        step_started = time.perf_counter()
        noops_before = noops

        if pending is None:
            t0 = time.perf_counter()
            try:
                page: Page = perceive(session)
            except CDPError:
                # Asked twice and no answer: the run ends here, with its steps and times kept.
                result.outcome = "browser_unresponsive"
                break
            perceive_ms = (time.perf_counter() - t0) * 1000
        else:
            page, perceive_ms = pending, 0.0
            pending = None

        # A one-time code the site takes as fixed, as a preprod one can, is typed from .env: its boxes
        # often have no name, so the classifier would not see them, and it never sees the code.
        if login and otp and otps < MAX_FORM_OTPS:
            otp_state = auto_otp(session, login[0], otp)
            if otp_state == "submitted":
                otps += 1
                act.wait_for_load(session)
                otp_state = wait_for_otp_result(session, login[0], len(otp))
                if otp_state == "otp_failed":
                    result.outcome = "form_otp_failed"
                    break
                history.append("form otp: typed")
                t0 = time.perf_counter()
                page = perceive(session)
                perceive_ms += (time.perf_counter() - t0) * 1000
            elif is_manual(otp_state):
                result.outcome = _manual_outcome(otp_state).replace("form_login", "form_otp", 1)
                if runfolder is not None:
                    runfolder.step_elements(n, page, can_write=writer is not None)
                break

        # A login form can appear mid-run, such as a dialog a Login button opens. It is signed in
        # here, as on the first page: the classifier never types into a credential field.
        if login and logins < MAX_FORM_LOGINS and any(e.secret for e in page.items):
            logins += 1
            login_state = _prepare_form_login(session, login, manual_login)
            if is_manual(login_state) or login_state == "login_failed":
                result.outcome = _manual_outcome(login_state) if is_manual(login_state) else "form_login_failed"
                if runfolder is not None:
                    # The page the login stopped on, labels only, as every step records it: why it
                    # needed a person is then there to read.
                    runfolder.step_elements(n, page, can_write=writer is not None)
                break
            if login_state != "outside_target":
                history.append(f"form login: {login_state}")
                t0 = time.perf_counter()
                page = perceive(session)
                perceive_ms += (time.perf_counter() - t0) * 1000

        # The page saying the words the run was told to wait for is the end, whatever the
        # classifier would make of it: a finish line kept out of the element list, such as a pay
        # button, leaves it nothing to recognise.
        if done_text and page_says(session, done_text):
            result.outcome = "done"
            history.append(f"page shows {done_text!r}")
            break

        # A dialog the page opened was closed as it came; what it said is news to the classifier.
        said = getattr(session, "dialogs", None) or []
        while dialogs_seen < len(said):
            history.append(f"page dialog closed, {said[dialogs_seen]}")
            dialogs_seen += 1

        # A saved file goes into any empty file input it fits, as a fixed code does: the input is
        # usually hidden behind the page's own button, and a file is not a choice to put to anyone.
        if data and data.files and uploads < MAX_UPLOADS:
            placed = act.upload_files(session, data.files, skip=uploaded)
            if placed:
                uploads += len(placed)
                history.extend(f"uploaded {p}" for p in placed)
                t0 = time.perf_counter()
                page, _, _ = act.observe_until_changed(session, act.fingerprint(page), timeout_ms=WAIT_MS)
                perceive_ms += (time.perf_counter() - t0) * 1000

        if data and data.files:
            # A preview of a file this run uploaded is named after it; opening one only shows what
            # is already known, and some pages hold the browser while it is open.
            ours = {Path(p).name.casefold() for p in data.files.values()}
            page = replace(page, items=[e for e in page.items if e.name.strip().casefold() not in ours])

        site = site_for(page.url, sites or [])
        if site and site.avoid:
            # Controls the site file rules out are not in the list at all, so no step can pick one.
            page = replace(page, items=[e for e in page.items if not site.avoids(e.name)])
        # Typing and opening an address need free text, which only the writer composes.
        can_write = writer is not None
        has_data = bool(data and data.fields)
        action_criteria = available_actions(page, allow_type=allow_type, can_write=can_write, has_data=has_data)
        element_criteria_map = {str(e.index): e.label() for e in page.items}

        t0 = time.perf_counter()
        auto = next_from_data(session, page, data, from_data_tries) if data else None
        if auto is not None:
            sure = ChoiceAnswer(choice=auto.kind, probabilities={auto.kind: 1.0}, confidence=1.0)
            named = ChoiceAnswer(choice=str(auto.index), probabilities={str(auto.index): 1.0}, confidence=1.0)
            record = {"from_data": {"kind": auto.kind, "element": auto.index, "saved": auto.key}}
            decision = Decision(kind=sure, element=named, satisfied=NoulAnswer(noul=0.0), state=record, answers=record)
        else:
            decision = decide(
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
        confidence = decision.confidence
        # The element answer settles how to act on it when it takes only one of the ways offered: an
        # empty text field is typed into and a dropdown list is chosen in, whichever of clicking,
        # typing or choosing the kind answer leaned to. Split between those, each can fall short
        # while together they are sure.
        chosen = next((e for e in page.items if e.index == decision.chosen_element), None)
        fits = None
        if chosen is not None and kind in TARGETING_KINDS:
            if chosen.typeable and not chosen.filled:
                fits = "type_text"
            elif chosen.tag == "select":
                fits = "select_option"
        if fits and fits in action_criteria:
            kind = fits
            together = sum(decision.kind.probabilities.get(k, 0.0) for k in TARGETING_KINDS)
            confidence = min(max(confidence, together), decision.element.confidence if decision.element else together)
        # A doubtful action is not carried out, as on the desktop. A page still arriving often shows
        # only what is left of the last one, such as a Quit button: it is watched instead, and asked
        # about again if it changes.
        # Scrolling and waiting change nothing on the site, so a doubtful one is carried out.
        # A doubtful "none" is doubt too, not a verdict that nothing can be done: it waits like the
        # rest. So does any "none" on a page with nothing on it yet, which is a page still arriving.
        held = (confidence < min_confidence and kind != "done" and kind not in HARMLESS_KINDS) or (
            kind == "none" and not page.items
        )
        # Pressing a ticked box unticks it, which a form rarely wants; a page between sections often
        # still shows the box just ticked, and it gets pressed again and again. Only a sure answer does it.
        if (
            kind == "click"
            and chosen is not None
            and chosen.role == "checkbox"
            and chosen.checked
            and confidence < UNTICK_CONFIDENCE
        ):
            held = True
        if held:
            kind = "wait"
        detail = ""
        changed = False
        text_source = ""

        t0 = time.perf_counter()
        touched = kind in {
            "click",
            "type_text",
            "select_option",
            "press_enter",
            "press_escape",
            "back",
            "navigate",
            "scroll_down",
            "scroll_up",
        }
        try:
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
                    if exact := saved_by_name(target, data.fields):
                        key = exact
                        text, text_source = data.fields[key], f"data:{key} (same name)"
                    else:
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
                        how = saved_match(value_now, text)
                        ok = 1.0 if how in ("matches", "matches, cased by the page") else 0.0
                        shown, verdict = f"<{key}>", how
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
            elif kind == "select_option":
                idx = decision.chosen_element
                element = next((e for e in page.items if e.index == idx and e.tag == "select"), None)
                options = act.select_options(session, int(element.index)) if element is not None else []
                waited_for = time.perf_counter() + WAIT_MS / 1000
                while element is not None and not options and time.perf_counter() < waited_for:
                    # A list that depends on the one above fills in a moment after it is set.
                    time.sleep(0.2)
                    options = act.select_options(session, int(element.index))
                if element is None or not options:
                    detail = f"select {idx} -> " + ("not a dropdown list" if element is None else "no options")
                    noops += 1
                    touched = False
                else:
                    if auto is not None and auto.option is not None:
                        picked = ChoiceAnswer(choice=str(auto.option), probabilities={str(auto.option): 1.0}, confidence=1.0)
                    else:
                        picked = choose_option(
                            client, goal, element.label(), options, history, choices=data.choices if data else None, model=model
                        )
                    text = dict(options).get(int(picked.choice)) if str(picked.choice).isdigit() else None
                    if picked.choice == NO_SAVED_VALUE and picked.confidence >= min_confidence:
                        # Said so the classifier sees it: an earlier answer on the page may need changing.
                        offered = " | ".join(t[:30] for _, t in options[:8])
                        detail = f"select [{idx}] -> the saved value is not among the options: {offered}"
                        noops += 1
                        touched = False
                    elif text is None or picked.confidence < min_confidence:
                        # The options are the page's own words: naming them says what the goal or the data should pick.
                        offered = " | ".join(t[:30] for _, t in options[:8])
                        detail = f"select [{idx}] -> no option fits ({picked.confidence:.2f}); options: {offered}"
                        noops += 1
                        touched = False
                    elif act.select_option(session, int(element.index), int(picked.choice)):
                        offered = " | ".join(t[:30] for _, t in options[:8])
                        detail = f"select [{idx}] {element.name[:40]!r} = {text[:40]!r} (of: {offered})"
                    else:
                        detail = f"select [{idx}] {text[:40]!r} FAILED"
                        noops += 1
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
                # The page is busy, such as sending a code: watch it for a while rather than an instant.
                # A wait that saw nothing change is a noop; one that saw the page move on is not.
                scrolled = ""
                if held and "scroll_down" in action_criteria and page.below_fold:
                    # With more of the page below, the doubt is often what is not in view yet, such as
                    # the button under a long notice: scrolling shows it and changes nothing on the site.
                    scrolled = act.scroll(session, 3, page) + ", "
                next_page, _, changed = act.observe_until_changed(session, fp_before, timeout_ms=WAIT_MS)
                pending = next_page
                detail = scrolled + ("waited" if changed else f"waited {WAIT_MS // 1000}s, nothing changed")
                if held:
                    detail = f"held {decision.kind.choice} ({confidence:.2f}), {detail}"
                if not changed:
                    noops += 1
                touched = False
            else:
                detail = kind
                touched = False
        except CDPError as e:
            # The browser did not answer the action, as while a page hands over to another site:
            # the step did nothing that is known, and the run goes on from what the page shows next.
            detail = f"{kind} -> the browser did not answer ({e})"
            noops += 1
            touched = False
            pending = None

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
            confidence=round(confidence, 3),
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
        action = (kind, decision.chosen_element if kind in ("click", "type_text", "select_option") else None)
        repeats = repeats + 1 if touched and not changed and action == last_action else (1 if touched and not changed else 0)
        last_action = action
        # The same action on the same page, again and again, even when each changes it: a section
        # opened and closed, or a Next the page turns back.
        # Scrolling down a long page that shows no control yet looks the same each time, and a
        # scroll that truly does nothing is already caught as a stall.
        seen = (fp_before, action)
        tried[seen] = tried.get(seen, 0) + 1 if touched and kind not in HARMLESS_KINDS else 0

        # A doubtful `done` is doubt, not success, so it falls through to the confidence check.
        if (kind == "done" and confidence >= min_confidence) or decision.satisfied.noul >= 0.5:
            result.outcome = "done"
            break
        if kind == "none":
            result.outcome = "blocked"
            break
        doubtful_but_moving = changed and (held or kind in HARMLESS_KINDS)
        if confidence < min_confidence and not doubtful_but_moving:
            result.outcome = f"low_confidence({confidence:.2f})"
            break
        if noops >= 2:
            result.outcome = "stuck"
            break
        if noops == noops_before:
            # A step that did something clears the count: a long form meets a slip now and then.
            noops = 0
        if repeats >= MAX_REPEATS:
            # A click that lands and does nothing is not a noop to the action, only to the page.
            result.outcome = "stalled"
            break
        if tried.get(seen, 0) >= MAX_REPEATS:
            result.outcome = "looping"
            break
    else:
        result.outcome = "max_steps"

    return _finish_result(result, session, started, runfolder)


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
