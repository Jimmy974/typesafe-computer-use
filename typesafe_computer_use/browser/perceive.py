"""Perception from the DOM instead of from pixels.

Replaces: screencapture -> Apple Vision OCR -> merge blocks -> reading order.
With:      one Runtime.evaluate that returns the element list already ordered.

The trade is explicit. OCR sees anything painted, including canvases and text
baked into images. The DOM sees only real elements — but it sees them *exactly*:
correct text, correct role, correct label, and a click point in viewport
coordinates, which is what CDP Input wants. The original had to convert Retina
capture pixels back into screen points.

Each collected element is stamped with `data-tscu="<index>"` so later steps have
a stable handle to click, focus and scroll. Stamps from the previous snapshot are
cleared first, so an element that vanished cannot be clicked by mistake.

What a user has typed is never read. An input's current value is not collected,
and no element takes its name from it, so a password, a one-time code or a card
number cannot reach the classifier, the writer, the log or the run folder. The
only value used is a button input's, which is the button's own label. Fields
that ask for a credential are marked `secret`; the normal action loop never
types into them. The dedicated portal login path does not use perception.
What does leave a field is one bit, `filled`: whether it holds anything, so a
form's progress can be read without its contents. A credential field has none.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from .cdp import CDPError

INTERACTIVE_JS = r"""
(() => {
  document.querySelectorAll("[data-tscu]").forEach(el => el.removeAttribute("data-tscu"));
  const ROLES = new Set(["button","link","menuitem","menuitemcheckbox","menuitemradio",
                         "tab","checkbox","radio","switch","combobox","option","searchbox",
                         "textbox","slider","spinbutton"]);
  const TAGS = new Set(["A","BUTTON","INPUT","SELECT","TEXTAREA","SUMMARY","OPTION"]);
  const SEL = "a,button,input,select,textarea,summary,[role],[onclick],[tabindex]";
  // Inputs that take free text. Everything else (checkbox, radio, file, range...) is clicked.
  const TEXT_TYPES = new Set(["text","search","email","url","tel","number","password"]);
  // An input of these types shows its value as its label, and the page wrote that value.
  const BUTTON_TYPES = new Set(["submit","button","reset"]);
  // Autocomplete tokens for credentials and payment data (WHATWG autofill field names).
  const SECRET_AUTOCOMPLETE = /(^|\s)(current-password|new-password|one-time-code|cc-number|cc-csc|cc-exp|cc-exp-month|cc-exp-year)(\s|$)/;
  const CHECKABLE_ROLES = new Set(["checkbox","radio","switch","menuitemcheckbox","menuitemradio"]);
  // The caption the page wrote for a control: what aria-labelledby points at, else its <label>s,
  // wrapping it or naming it with for=. The controls inside a label are taken out first, so a
  // caption never carries a field's text or a list's options.
  const caption = (el) => {
    const parts = (el.getAttribute("aria-labelledby") || "").split(/\s+/).filter(Boolean)
      .map(id => document.getElementById(id)).filter(Boolean).map(n => n.innerText || "");
    if (!parts.join("").trim() && el.labels) {
      for (const lab of el.labels) {
        const copy = lab.cloneNode(true);
        copy.querySelectorAll("input,select,textarea,button").forEach(n => n.remove());
        parts.push(copy.textContent || "");
      }
    }
    return parts.join(" ").replace(/\s+/g, " ").trim();
  };
  // The headings in reading order: a control's section is the last one before it, so two fields
  // with the same caption, such as a child's name and a guardian's, can be told apart.
  const heads = [...document.querySelectorAll("h1,h2,h3,h4,h5,h6,legend")]
    .map(h => [h, (h.innerText || "").replace(/\s+/g, " ").trim().slice(0, 80)]).filter(h => h[1]);
  const sectionOf = el => {
    let found = "";
    for (const [h, text] of heads) {
      if (h.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING) found = text; else break;
    }
    return found;
  };
  // A radio button's question, such as "Born in", which its own label ("Ethiopia") does not say:
  // the group's legend or label, else the text just before the smallest box holding all its buttons.
  const groupOf = el => {
    const set = el.closest('fieldset,[role="radiogroup"]');
    const named = set && (set.getAttribute("aria-label") || (set.querySelector("legend") || {}).innerText);
    if (named) return named.replace(/\s+/g, " ").trim().slice(0, 60);
    const mates = el.name ? document.querySelectorAll('input[type="radio"][name="' + CSS.escape(el.name) + '"]') : [el];
    let box = el.parentElement;
    while (box && ![...mates].every(m => box.contains(m))) box = box.parentElement;
    for (let up = 0; box && up < 3; up++, box = box.parentElement) {
      let before = box.previousElementSibling;
      while (before && !(before.innerText || "").trim()) before = before.previousElementSibling;
      const text = before ? before.innerText.replace(/\s+/g, " ").trim() : "";
      if (text && text.length <= 60) return text;
    }
    return "";
  };
  const checkableEarly = (el, type, role) => type === "checkbox" || type === "radio" || CHECKABLE_ROLES.has(role);
  const vw = window.innerWidth, vh = window.innerHeight;
  const out = [];
  const seen = new Set();
  let sid = 0, belowFold = 0, total = 0;
  for (const el of document.querySelectorAll(SEL)) {
    total++;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    // Viewport test BEFORE any style resolution. On a heavy page most candidates
    // are off-screen, and getComputedStyle is the expensive call in this loop —
    // skipping it for the ones we would discard anyway is the whole optimisation.
    const onScreen = r.top < vh + 8 && r.bottom > -8 && r.left < vw + 8 && r.right > -8;
    if (!onScreen) { belowFold++; continue; }
    const st = getComputedStyle(el);
    if (st.visibility === "hidden" || st.display === "none" || parseFloat(st.opacity || "1") === 0) continue;
    if (el.disabled || el.getAttribute("aria-hidden") === "true") continue;
    // A control drawn as switched off, with no disabled attribute to say so, such as a card that
    // waits for an earlier choice: offered, it is pressed again and again for nothing.
    if (el.getAttribute("aria-disabled") === "true" || st.cursor === "not-allowed") continue;
    const role = (el.getAttribute("role") || "").toLowerCase();
    if (!(TAGS.has(el.tagName) || ROLES.has(role) || el.hasAttribute("onclick") || el.hasAttribute("tabindex"))) continue;
    const type = el.tagName === "INPUT" ? String(el.type || "text").toLowerCase() : "";
    const secret = type === "password" ||
      SECRET_AUTOCOMPLETE.test((el.getAttribute("autocomplete") || "").toLowerCase());
    const field = el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && TEXT_TYPES.has(type));
    let name = (el.getAttribute("aria-label") || caption(el) || el.getAttribute("placeholder") ||
                el.getAttribute("title") || el.getAttribute("alt") || "").trim();
    // Never a text control's value or its own text: that is what the user typed.
    const typedInto = field || el.isContentEditable || role === "textbox" || role === "searchbox";
    if (!name && !typedInto) name = (el.innerText || (BUTTON_TYPES.has(type) ? el.value : "") || "");
    if (!name) name = el.getAttribute("name") || "";
    name = name.replace(/\s+/g, " ").trim();
    if (!name) continue;
    name = name.slice(0, 120);
    const key = [name.toLowerCase(), Math.round(r.left), Math.round(r.top), el.tagName].join("|");
    if (seen.has(key)) continue;
    seen.add(key);
    const x = Math.round(r.left + Math.min(r.width, 1400) / 2);
    const y = Math.round(r.top + r.height / 2);
    const top = document.elementFromPoint(Math.max(0, Math.min(x, vw - 1)), Math.max(0, Math.min(y, vh - 1)));
    const covered = !!(top && !el.contains(top) && !top.contains(el));
    const checkable = type === "checkbox" || type === "radio" || CHECKABLE_ROLES.has(role);
    // A toggle button, a tab or a slot in a list says it is the chosen one with aria-pressed,
    // aria-selected or aria-current; without one of those, it says nothing.
    const toggled = ["aria-pressed", "aria-selected", "aria-current"].map(a => el.getAttribute(a)).find(v => v !== null && v !== undefined);
    const checked = checkable ? (el.checked === true || el.getAttribute("aria-checked") === "true")
      : toggled !== undefined ? (toggled !== "false") : null;
    // Whether a text control holds anything; never what. Not even that much for a credential field.
    const filled = typedInto && !secret ? String(el.value ?? el.innerText ?? "").length > 0 : null;
    // A dropdown list's current option, by the text the page wrote for it; a placeholder with no
    // value, such as "Select language", is nothing chosen.
    // Whether the page marks the control as wrong or missing, as a form does after a failed Next.
    const invalid = el.getAttribute("aria-invalid") === "true" || (typeof el.matches === "function" && el.matches(":user-invalid"));
    const section = field || el.tagName === "SELECT" || checkableEarly(el, type, role) ? sectionOf(el) : "";
    const group = type === "radio" ? groupOf(el) : "";
    const pick = el.tagName === "SELECT" ? el.selectedOptions[0] : null;
    const chosen = pick && pick.value !== "" ? pick.text.replace(/\s+/g, " ").trim().slice(0, 80) : null;
    el.setAttribute("data-tscu", String(sid));
    out.push({sid, tag: el.tagName.toLowerCase(), role: role || type,
              name, x, y, w: Math.round(r.width), h: Math.round(r.height),
              in_view: true, covered,
              href: el.tagName === "A" ? (el.href || "") : "",
              field, secret, checked, filled, chosen, invalid, section, group});
    sid++;
  }
  out.sort((a, b) => (Math.abs(a.y - b.y) > 8 ? a.y - b.y : a.x - b.x));
  out.forEach((o, i) => {
    const el = document.querySelector('[data-tscu="' + o.sid + '"]');
    if (el) el.setAttribute("data-tscu", String(i));
    o.index = i;
  });
  const sc = document.scrollingElement || document.documentElement;
  // What the page says about itself: its own visible text that reads as an error, a warning or a
  // wait, such as "Please wait 600 seconds before requesting a new code". Text inside anything a
  // person types into is skipped, and a control's value is not text on the page: nothing typed
  // is among these.
  const MESSAGE = /\b(error|errors|failed|failure|invalid|incorrect|required|not allowed|expired|try again|please wait|too many|wrong|unable|cannot|can't|success|successfully)\b/i;
  // Headings and prose are the page's content, not something it is saying about what just happened.
  const TYPED = '[contenteditable]:not([contenteditable="false"]),textarea,select,option,script,style,noscript,[aria-hidden="true"],h1,h2,h3,h4,h5,h6';
  const reddish = el => {
    const m = /rgba?\((\d+), *(\d+), *(\d+)/.exec(getComputedStyle(el).color);
    return !!m && +m[1] >= 150 && +m[2] <= 90 && +m[3] <= 90;
  };
  const messages = [];
  // Extra to the element list, so a page mid-change that trips this loses its messages, not its elements.
  try {
  const walker = document.createTreeWalker(document.body || document.documentElement, NodeFilter.SHOW_TEXT);
  for (let n = walker.nextNode(); n && messages.length < 5; n = walker.nextNode()) {
    const el = n.parentElement;
    if (!el || !n.nodeValue.trim() || el.closest(TYPED)) continue;
    // A field's own error is often only coloured red, with no telltale word in it; and whatever a
    // dialog over the page says is said to whoever is at it.
    const inDialog = !!el.closest('[role="dialog"],[role="alertdialog"],[aria-modal="true"],dialog[open]');
    if (!MESSAGE.test(n.nodeValue) && !reddish(el) && !inDialog) continue;
    const box = el.getBoundingClientRect();
    if (box.width < 1 || box.height < 1) continue;
    // The element's whole text is the whole message when it is short: "Error!" and the reason
    // often sit in separate pieces of it.
    const whole = (el.innerText || "").replace(/\s+/g, " ").trim();
    const text = whole.length >= 4 && whole.length <= 120 ? whole : n.nodeValue.replace(/\s+/g, " ").trim();
    if (text.length < 4 || text.length > 120 || messages.includes(text)) continue;
    messages.push(text);
  }
  } catch (e) { messages.length = 0; }
  return {url: location.href, title: document.title, vw, vh, count: out.length, items: out, messages,
          scroll_y: Math.round(sc.scrollTop), scroll_max: Math.round(sc.scrollHeight - vh),
          candidates: total, below_fold: belowFold,
          can_scroll: sc.scrollHeight > vh + 4,
          history_len: history.length,
          fields: out.filter(o => o.field && !o.secret).length};
})()
"""


@dataclass(frozen=True)
class Element:
    index: int
    tag: str
    role: str
    name: str
    x: int
    y: int
    w: int
    h: int
    in_view: bool
    covered: bool
    href: str
    field: bool = False  # takes free text
    secret: bool = False  # asks for a password, a one-time code or card data: normal loop refuses it
    checked: bool | None = None  # a checkbox, radio or switch: whether it is on; None for anything else
    filled: bool | None = None  # a text control: whether it holds anything, never what; None otherwise
    chosen: str | None = None  # a dropdown list: the option it shows, in the page's words; None otherwise
    invalid: bool = False  # the page marks it as wrong or missing
    section: str = ""  # a field, list or box: the heading it sits under, in the page's words
    group: str = ""  # a radio button: the question its group answers, such as "Born in"

    @property
    def typeable(self) -> bool:
        return self.field and not self.secret

    def label(self) -> str:
        bits = [f"<{self.tag}>"]
        if self.role and self.role != self.tag:
            bits.append(self.role)  # an <input> says nothing; radio, checkbox or email says how to use it
        bits.append(repr(self.name))
        if self.group:
            bits.append(f"answering {self.group!r}")
        if self.section:
            bits.append(f"under {self.section!r}")
        if self.checked is not None:
            bits.append("checked" if self.checked else "not checked")
        if self.filled is not None:
            bits.append("filled" if self.filled else "empty")
        if self.tag == "select":
            bits.append(f"set to {self.chosen!r}" if self.chosen else "nothing chosen")
        if self.invalid:
            bits.append("marked invalid by the page")
        if self.href:
            bits.append(self.href[:70])
        if self.secret:
            bits.append("credential field")
        if not self.in_view:
            bits.append("off-screen")
        if self.covered:
            bits.append("covered by an overlay")
        return " ".join(bits)


@dataclass
class Page:
    url: str
    title: str
    vw: int
    vh: int
    items: list[Element]
    elapsed_ms: float
    raw_count: int
    can_scroll: bool = True
    history_len: int = 1
    field_count: int = 0
    scroll_y: int = 0
    scroll_max: int = 0
    candidates: int = 0
    below_fold: int = 0
    messages: tuple[str, ...] = ()  # the page's own error, warning or wait lines, from its visible text

    @property
    def has_field(self) -> bool:
        return self.field_count > 0


def perceive(session: Any, *, budget: int = 120) -> Page:
    """One CDP round trip -> an ordered, labelled element list. No pixels."""
    start = time.perf_counter()
    try:
        data = session.evaluate(INTERACTIVE_JS) or {}
    except CDPError:
        # Asked while the page was being replaced, as right after a sign-in: the new one answers.
        time.sleep(0.3)
        data = session.evaluate(INTERACTIVE_JS) or {}
    elapsed = (time.perf_counter() - start) * 1000
    items = [element_from(it, i) for i, it in enumerate((data.get("items") or [])[:budget])]
    return Page(
        url=str(data.get("url", "")),
        title=str(data.get("title", "")),
        vw=int(data.get("vw", 0)),
        vh=int(data.get("vh", 0)),
        items=items,
        elapsed_ms=elapsed,
        raw_count=int(data.get("count", len(items))),
        can_scroll=bool(data.get("can_scroll", True)),
        history_len=int(data.get("history_len", 1)),
        field_count=int(data.get("fields", 0)),
        scroll_y=int(data.get("scroll_y", 0)),
        scroll_max=int(data.get("scroll_max", 0)),
        candidates=int(data.get("candidates", 0)),
        below_fold=int(data.get("below_fold", 0)),
        messages=tuple(str(m)[:200] for m in (data.get("messages") or [])[:5]),
    )


def _maybe_bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


def element_from(it: dict, i: int = 0) -> Element:
    """One element from the page script's record, or from a saved run folder's."""
    return Element(
        index=int(it.get("index", i)),
        tag=str(it.get("tag", "")),
        role=str(it.get("role", "")),
        name=str(it.get("name", "")),
        x=int(it.get("x", 0)),
        y=int(it.get("y", 0)),
        w=int(it.get("w", 0)),
        h=int(it.get("h", 0)),
        in_view=bool(it.get("in_view", True)),
        covered=bool(it.get("covered", False)),
        href=str(it.get("href", "")),
        field=bool(it.get("field", False)),
        secret=bool(it.get("secret", False)),
        checked=_maybe_bool(it.get("checked")),
        filled=None if it.get("secret") else _maybe_bool(it.get("filled")),
        chosen=str(it["chosen"])[:80] if it.get("chosen") else None,
        invalid=bool(it.get("invalid", False)),
        section=str(it.get("section") or "")[:80],
        group=str(it.get("group") or "")[:60],
    )


def to_json(page: Page) -> str:
    return json.dumps(
        {
            "url": page.url,
            "title": page.title,
            "elapsed_ms": round(page.elapsed_ms, 2),
            "items": [it.__dict__ for it in page.items],
        },
        indent=2,
    )
