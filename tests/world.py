"""A deterministic simulated computer, so the real step loop can be driven end to end.

The package is one loop over four seams: capture the screen, perceive items, ask the classifier,
act on the machine. Every unit test here covers one seam; nothing covered the loop itself, where
the stop rules live. So this module fakes the two outer seams -- the screen and the machine -- and
leaves `runner.run`, `decide`, and `actions` exactly as they ship. A scenario then reads as a page
graph plus a policy, and a failure means the architecture, not the harness.

The screen is a page of rows: item N occupies the pixel box (100, 100+40N, 600, 130+40N) on a
2000x1200 capture at scale 2.0, which is what `tests/conftest.py`'s `screen` fixture describes.
Clicks come back as screen points, so the world halves them to find the row they hit.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace

from PIL import Image
from typesafe_sdk import Noul

from typesafe_computer_use import actions, macos, runner
from typesafe_computer_use.actions import Context
from typesafe_computer_use.models import AxNode, Field, Item, Screen
from typesafe_computer_use.runner import RunConfig, RunState, run

CAPTURE = (2000, 1200)
SCALE = 2.0
ROW_TOP = 100.0  # first row's top edge, in capture pixels
ROW_HEIGHT = 40.0
ROW_TEXT_HEIGHT = 30.0  # a row's text is shorter than its pitch, so there is a gap between rows
ROW_X1, ROW_X2 = 100.0, 600.0
LOADING = "Loading..."

# An off-screen control is parked far above the display, the way Chromium parks a scrolled-out link.
OFFSCREEN_Y = -4200.0

Step = tuple  # (kind, target) or (kind, target, confidence)
Policy = Callable[[dict, dict], Step]
Rows = list  # of str, or (text, role)


def row_box(n: int) -> tuple[float, float, float, float]:
    """The pixel box of the Nth row, top to bottom."""
    top = ROW_TOP + ROW_HEIGHT * n
    return (ROW_X1, top, ROW_X2, top + ROW_TEXT_HEIGHT)


@dataclass
class Page:
    """One screen of the simulated computer, and what each action does to it.

    `items` are rows of text, or (text, role) for a control the app declares through accessibility.
    A page whose rows change on their own -- a clock, a ticker -- passes a callable(world) instead,
    which is asked again on every capture.
    `on` maps an action as the world names it ("click:Tickets", "enter", "type:hello", "open:<url>")
    to the next page's name, or to a callable(world) returning a name or None to stay. An action
    with no entry leaves the page alone, which is the "nothing happened" case the runner must cope
    with.
    """

    name: str
    items: Rows | Callable[[World], Rows] = dataclasses.field(default_factory=list)
    url: str | None = None  # browser pages have one; app pages do not
    app: str = "Google Chrome"
    field: str | None = None  # label of the text field focused on this page, if any
    offscreen: tuple[str, ...] = ()  # labels the app exposes without showing
    loads_in: int = 0  # steps of "wait" before the items appear
    on: dict[str, str | Callable[[World], str | None]] = dataclasses.field(default_factory=dict)


class World:
    """The pages, which one is showing, what was typed, and every action the world received.

    `log` holds the world's own action names in order. Mouse clicks also land in `mouse` as the raw
    point, because a press through accessibility and a click on the pixel under the item both read
    as "click:<text>" here, and a scenario needs to tell the two apart.
    """

    def __init__(self, pages: list[Page], start: str | None = None):
        self.pages = {p.name: p for p in pages}
        self.page = self.pages[start or pages[0].name]
        self.typed: dict[str, str] = {}
        self.log: list[str] = []
        self.mouse: list[tuple[float, float]] = []
        self.fake: FakeTypeSafe | None = None  # the classifier `drive` built, for fake.states
        self.loading = {p.name: p.loads_in for p in pages}
        self.ticks = 0  # captures taken so far, so a page can show something that moves on its own
        self._actions: dict[int, str] = {}  # id(ref) -> the action pressing that element applies
        self._refs: dict[str, object] = {}  # action -> the element, so a ref stays the same object
        self._labels: dict[int, str] = {}  # id(field ref) -> the field's label
        self._fields: dict[str, object] = {}

    # ----- the world's own state ------------------------------------------------------------

    @property
    def loading_now(self) -> bool:
        return self.loading[self.page.name] > 0

    def rows(self) -> list[tuple[str, str]]:
        """(text, role) per row of the current page. A page still loading shows one line."""
        if self.loading_now:
            return [(LOADING, "")]
        items = self.page.items(self) if callable(self.page.items) else self.page.items
        return [(it, "") if isinstance(it, str) else it for it in items]

    def apply(self, action: str, label: str | None = None) -> None:
        """Receive one action: record it, then follow the current page's transition for it.

        A page that is still loading answers nothing but `wait`: the tick is spent, the action is
        dropped. That is what a real app does to a click on a spinner.
        """
        self.log.append(action)
        if self.loading_now:
            if action == "wait":
                self.loading[self.page.name] -= 1
            return
        if action.startswith("type:"):
            self.typed[label or self.page.field or ""] = action[len("type:") :]
        nxt = self.page.on.get(action)
        if callable(nxt):
            nxt = nxt(self)
        if nxt is not None:
            self.page = self.pages[nxt]

    def _ref(self, action: str) -> object:
        """The element that applies `action` when pressed, the same object every capture."""
        if action not in self._refs:
            ref = object()
            self._refs[action] = ref
            self._actions[id(ref)] = action
        return self._refs[action]

    def _field_ref(self, label: str) -> object:
        if label not in self._fields:
            ref = object()
            self._fields[label] = ref
            self._labels[id(ref)] = label
        return self._fields[label]

    # ----- the screen ----------------------------------------------------------------------

    def capture(self) -> Screen:
        """What `perception.capture` would return for the page now showing.

        Each capture is a tick, so a page whose rows are a callable can move between steps without
        moving inside one: every other read of the screen in the same step sees the same rows.
        """
        nodes = [
            AxNode(role="AXLink", label=lbl, x=0.0, y=OFFSCREEN_Y, w=120.0, h=32.0, pressable=True, ref=self._ref(f"press:{lbl}"))
            for lbl in self.page.offscreen
        ]
        screen = Screen(
            image=Image.new("RGB", CAPTURE),
            scale=SCALE,
            app=self.page.app,
            field=self.focused_field(),
            url=self.page.url,
            pid=1,
            window=None,
            offscreen=nodes,
        )
        self.ticks += 1
        return screen

    def perceive(self, screen: Screen) -> list[Item]:
        """The rows as items, filling `screen.ax_refs` for the ones the app declared."""
        screen.ax_refs.clear()
        items = []
        for n, (text, role) in enumerate(self.rows()):
            items.append(Item(n, text, 1.0, *row_box(n), role=role, source="ax" if role else "ocr"))
            if role:
                screen.ax_refs[n] = self._ref(f"click:{text}")
        return items

    def focused_field(self) -> Field | None:
        """The page's text field, at the row that carries its label when one does."""
        label = self.page.field
        if label is None:
            return None
        texts = [t for t, _ in self.rows()]
        if label in texts:
            _, top, _, _ = row_box(texts.index(label))
            box = (ROW_X1 / SCALE, top / SCALE, (ROW_X2 - ROW_X1) / SCALE, ROW_TEXT_HEIGHT / SCALE)
        else:
            box = (50.0, 50.0, 300.0, 30.0)
        return Field(
            role="AXTextField",
            label=label,
            placeholder="",
            value=self.typed.get(label, ""),
            x=box[0],
            y=box[1],
            w=box[2],
            h=box[3],
            ref=self._field_ref(label),
        )

    # ----- the machine ---------------------------------------------------------------------

    def click_at(self, point: tuple[float, float]) -> None:
        """A synthetic click, in screen points: find the row it lands on and press that item."""
        self.mouse.append(point)
        px, py = point[0] * SCALE, point[1] * SCALE
        rows = self.rows()
        n = int((py - ROW_TOP) // ROW_HEIGHT)
        hit = ROW_X1 <= px <= ROW_X2 and 0 <= n < len(rows) and py <= ROW_TOP + ROW_HEIGHT * n + ROW_TEXT_HEIGHT
        self.apply(f"click:{rows[n][0]}" if hit else "click:nothing")

    def ax_press(self, ref: object) -> bool:
        action = self._actions.get(id(ref))
        if action is None:
            return False
        self.apply(action)
        return True

    def press(self, name: str, command: bool = False) -> None:
        if command and name == "[":
            self.apply("back")
            return
        self.apply("enter" if name == "return" else name)

    def scroll(self, lines: int) -> None:
        self.apply("scroll_down" if lines < 0 else "scroll_up")

    def type_text(self, text: str) -> None:
        self.apply(f"type:{text}")

    def ax_set_value(self, ref: object, text: str) -> bool:
        self.apply(f"type:{text}", label=self._labels.get(id(ref)))
        return True

    def ax_value(self, ref: object) -> str | None:
        return self.typed.get(self._labels.get(id(ref), ""), "")

    def clear_field(self) -> None:
        self.apply("clear_field")
        self.typed.pop(self.page.field or "", None)

    def activate(self, app: str) -> bool:
        self.apply("activate")
        return True

    def open_url(self, browser: str, url: str) -> bool:
        self.apply(f"open:{url}")
        return True

    # ----- wiring --------------------------------------------------------------------------

    def install(self, monkeypatch) -> None:
        """Replace the two outer seams: the screen the loop reads and the machine it drives."""
        monkeypatch.setattr(runner, "capture", lambda *a, **k: self.capture())
        monkeypatch.setattr(runner, "perceive", lambda screen, *a, **k: self.perceive(screen))
        monkeypatch.setattr(macos, "check_abort", lambda: None)
        monkeypatch.setattr(macos, "sleep_watching", lambda seconds: None)
        monkeypatch.setattr(macos, "click_at", self.click_at)
        monkeypatch.setattr(macos, "ax_press", self.ax_press)
        monkeypatch.setattr(macos, "press", self.press)
        monkeypatch.setattr(macos, "scroll", self.scroll)
        monkeypatch.setattr(macos, "type_text", self.type_text)
        monkeypatch.setattr(macos, "ax_focus", lambda ref: True)
        monkeypatch.setattr(macos, "ax_set_value", self.ax_set_value)
        monkeypatch.setattr(macos, "ax_value", self.ax_value)
        monkeypatch.setattr(macos, "clear_field", self.clear_field)
        monkeypatch.setattr(macos, "focused_field", self.focused_field)
        monkeypatch.setattr(macos, "activate", self.activate)
        monkeypatch.setattr(macos, "open_url", self.open_url)
        # `wait` is the one action that touches no machine call, so the only way the world hears
        # about it is the handler itself. Without this a loading page would never finish loading.
        monkeypatch.setitem(actions._HANDLERS, "wait", lambda decision, screen, items, ctx: (self.apply("wait"), "waited")[1])


DEFAULT_CONFIDENCE = 0.9  # comfortably over the runner's 0.4 floor, so a scenario stops for a reason


class FakeTypeSafe:
    """The classifier, replaced by a policy over the state the real `decide` builds.

    A policy returns (kind, target) and this maps the target to whatever key the question wants:
    the item's index for click_item, the off-screen control's key for press_offscreen, the site key
    for use_browser. So a scenario never writes an index, and renumbering the page cannot break it.

    Every decision state lands in `states`, so a test can assert what the model was shown. The
    `verify_typed` Noul that `actions._type_text` makes is a different question about a different
    state, so it goes to `verify_states` and leaves `states` one entry per step.
    """

    def __init__(self, policy: Policy, noul: float = 0.95):
        self.policy = policy
        self.noul = noul
        self.states: list[dict] = []
        self.asked: list[dict] = []  # the questions each decision was asked, so a test can read the criteria offered
        self.verify_states: list[dict] = []
        self.steps: list[Step] = []

    def __enter__(self) -> FakeTypeSafe:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def system_one(self, state: dict, questions: dict) -> SimpleNamespace:
        if any(isinstance(q, Noul) for q in questions.values()):
            self.verify_states.append(state)
            return SimpleNamespace(answers={name: SimpleNamespace(noul=self.noul) for name in questions})
        self.states.append(state)
        self.asked.append(questions)
        step = self.policy(state, questions)
        self.steps.append(step)
        kind, target = step[0], step[1]
        confidence = step[2] if len(step) > 2 else DEFAULT_CONFIDENCE
        return SimpleNamespace(answers=self._answers(state, questions, kind, target, confidence))

    def _answers(self, state: dict, questions: dict, kind: str, target, confidence: float) -> dict:
        answers = {"kind": _answer(kind, confidence), "site": _answer(target if kind == "use_browser" else "none", confidence)}
        if "item" in questions:
            answers["item"] = _answer(self._item_key(state, target) if kind == "click_item" else "0", confidence)
        if "offscreen" in questions:
            key = self._offscreen_key(state, target) if kind == "press_offscreen" else "0"
            answers["offscreen"] = _answer(key, confidence)
        return answers

    @staticmethod
    def _item_key(state: dict, text: str) -> str:
        for it in state["screen_items_in_reading_order"]:
            if it["text"] == text:
                return str(it["i"])
        raise AssertionError(
            f"no item reads {text!r}: the screen shows {[it['text'] for it in state['screen_items_in_reading_order']]}"
        )

    @staticmethod
    def _offscreen_key(state: dict, label: str) -> str:
        for node in state.get("offscreen_controls", []):
            if node["label"] == label:
                return str(node["k"])
        raise AssertionError(f"no off-screen control is labelled {label!r}")


def _answer(choice: str, confidence: float) -> SimpleNamespace:
    """One ChoiceAnswer. The distribution names only real keys, since the runner logs them by key."""
    return SimpleNamespace(choice=choice, confidence=confidence, probabilities={choice: confidence})


def scripted(*steps: Step) -> Policy:
    """A policy that replays the steps in order, one per decision, and says `done` once spent."""
    taken = []

    def policy(state: dict, questions: dict) -> Step:
        taken.append(state)
        return steps[len(taken) - 1] if len(taken) <= len(steps) else ("done", None)

    return policy


class FakeWriter:
    """Stands in for the Anthropic client, answering by which properties the request asks for.

    The three writer calls are told apart by their schemas, exactly as `writer.py` builds them:
    a field fill, a proposed URL, and the final answer. The answer is the text of the screen the
    run stopped on, so a scenario can assert the run ended on the right page through the answer.
    """

    def __init__(self, text: str = "", url: str = ""):
        self.requests: list[dict] = []
        self.text = text
        self.url = url
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **request):
        self.requests.append(request)
        asked = set(request["output_config"]["format"]["schema"]["properties"])
        packet = json.loads(request["messages"][0]["content"][-1]["text"])
        if asked == {"fill", "text", "reason"}:
            reply = {"fill": bool(self.text), "text": self.text, "reason": "the goal names what to type"}
        elif asked == {"ok", "url", "reason"}:
            reply = {"ok": bool(self.url), "url": self.url, "reason": "the goal names the site"}
        elif asked == {"achieved", "answer"}:
            reply = {"achieved": True, "answer": " ".join(packet["screen_text_in_reading_order"])}
        else:
            raise AssertionError(f"the writer was asked for {sorted(asked)}")
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(reply))])


def drive(
    world: World,
    policy: Policy,
    goal: str = "do the thing",
    steps: int = 20,
    monkeypatch=None,
    tmp_path=None,
    writer: FakeWriter | None = None,
    email: str | None = None,
    noul: float = 0.95,
) -> RunState:
    """Run the real loop against the world until it stops itself. `world.fake` holds the classifier."""
    world.install(monkeypatch)
    fake = FakeTypeSafe(policy, noul)
    world.fake = fake
    monkeypatch.setattr(runner, "TypeSafeClient", lambda: fake)
    cfg = RunConfig(goal=goal, out=tmp_path / "run", act=True, steps=steps, delay=0)
    client = writer or FakeWriter()
    return run(
        cfg,
        lambda typesafe, history: Context(
            goal=goal, browser="Google Chrome", email=email, typesafe=typesafe, writer=client, history=history
        ),
    )
