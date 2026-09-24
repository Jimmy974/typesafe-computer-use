"""Saved values for a form, from a file, typed exactly as written.

    [fields]    # typed into text fields; the classifier sees only the names
    name = "Jimmy Wong"
    phone = "12345678"

    [choices]   # options to click; the classifier sees these, to match them to the page
    size = "Medium"

For a text field the classifier chooses which saved value fits, by name, and the code types the
value itself: no model composes it, so it arrives unchanged, and it never leaves this machine.
The check after typing compares the field with the value here, instead of asking the classifier.
A choice has to be matched to a radio or a checkbox on the page, which takes its value, so those
values are shown. A name that looks like a credential is refused when the file is read: nothing
is ever typed into a password field, and a file should not hold one.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .writer import looks_credential

SECTIONS = {"fields", "choices"}


@dataclass(frozen=True)
class FormData:
    fields: dict[str, str] = field(default_factory=dict)
    choices: dict[str, str] = field(default_factory=dict)

    def state(self, used: set[str]) -> dict:
        """What the classifier reads: every field's name and whether it is typed in yet, never its
        value; every choice in full."""
        out: dict = {}
        if self.fields:
            out["saved_fields"] = [{"name": k, "typed": k in used} for k in self.fields]
        if self.choices:
            out["saved_choices"] = dict(self.choices)
        return out

    @classmethod
    def from_state(cls, state: dict) -> tuple[FormData, set[str]]:
        """The same view back, for a replay: names without values, which is all a decision needs."""
        saved = state.get("saved_fields") or []
        fields = {entry["name"]: "" for entry in saved}
        used = {entry["name"] for entry in saved if entry.get("typed")}
        return cls(fields=fields, choices=dict(state.get("saved_choices") or {})), used


def parse_data(data: dict, source: str = "") -> FormData:
    unknown = set(data) - SECTIONS
    if unknown:
        raise ValueError(f"{source}: unknown section(s) {', '.join(sorted(unknown))}; expected [fields] and [choices]")
    out = {}
    for section in SECTIONS:
        table = data.get(section, {})
        if not isinstance(table, dict):
            raise ValueError(f"{source}: [{section}] must be a table of name = value")
        values = {}
        for key, value in table.items():
            if isinstance(value, bool) or not isinstance(value, str | int | float):
                raise ValueError(f"{source}: {section}.{key} must be text or a number")
            if looks_credential(key):
                raise ValueError(f"{source}: {section}.{key} looks like a credential; passwords and codes are never typed")
            values[key] = str(value)
        out[section] = values
    if not out["fields"] and not out["choices"]:
        raise ValueError(f"{source}: no values in [fields] or [choices]")
    return FormData(fields=out["fields"], choices=out["choices"])


def load_data(path: Path) -> FormData:
    try:
        return parse_data(tomllib.loads(path.read_text(encoding="utf-8")), str(path))
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path}: {e}") from e
