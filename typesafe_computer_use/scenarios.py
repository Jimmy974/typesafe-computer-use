"""Many runs of one use case: the same flow with different data, or a different goal per case.

A scenario file is TOML, a CSV, or TOML that points at a CSV:

    url = "https://httpbin.org/forms/post"          # defaults for every scenario
    goal = "Fill the order form with the saved data, then submit it."
    expect_url = "httpbin.org/post"                  # a run passes when its final address has this
    rows = "orders.csv"                              # optional: one scenario per CSV row

    [defaults.fields]                                # shared values; a scenario's own win
    email = "orders@example.com"

    [[scenario]]
    name = "amy-pickup"
    goal = "Fill the order form with the saved data, choose no toppings, then submit it."
    fields = { customer_name = "Amy Chan" }
    choices = { size = "Large" }

In a CSV the header names the columns. `name`, `url`, `goal`, `expect_url` and `steps` set those
for the row; a column headed `choice:<key>` is a choice; every other column is a field. An empty
cell sets nothing, so the default stands. Cells are read as text, so a phone number keeps its
leading zero (if the spreadsheet that saved the file kept it).

Every scenario is checked before any browser starts: a missing url or goal, a duplicate name, an
unknown key, or a name that looks like a credential stops the batch at once.
"""

from __future__ import annotations

import csv
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .formdata import FormData, parse_data

SETTINGS = ("url", "goal", "expect_url", "steps")
FILE_KEYS = {*SETTINGS, "rows", "defaults", "scenario"}
SCENARIO_KEYS = {"name", *SETTINGS, "fields", "choices"}
CHOICE_PREFIX = "choice:"
DEFAULT_STEPS = 16


@dataclass(frozen=True)
class Scenario:
    name: str
    url: str
    goal: str
    expect_url: str | None
    steps: int
    data: FormData | None


def load_scenarios(path: Path, *, defaults: dict | None = None) -> list[Scenario]:
    """Every scenario in a .toml or .csv file, merged with its defaults and checked.

    `defaults` holds settings from the command line; the file's own override them."""
    base = {k: v for k, v in (defaults or {}).items() if v is not None}
    if path.suffix.lower() == ".csv":
        raw = csv_rows(path)
        shared: dict = {"fields": {}, "choices": {}}
    else:
        try:
            doc = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise ValueError(f"{path}: {e}") from e
        unknown = set(doc) - FILE_KEYS
        if unknown:
            raise ValueError(f"{path}: unknown key(s) {', '.join(sorted(unknown))}")
        base.update({k: doc[k] for k in SETTINGS if k in doc})
        shared = {"fields": {}, "choices": {}, **(doc.get("defaults") or {})}
        if set(shared) - {"fields", "choices"}:
            raise ValueError(f"{path}: [defaults] takes only fields and choices")
        raw = list(doc.get("scenario") or [])
        if "rows" in doc:
            raw += csv_rows(path.parent / doc["rows"])
    if not raw:
        raise ValueError(f"{path}: no scenarios")

    out, seen = [], set()
    for i, entry in enumerate(raw, 1):
        unknown = set(entry) - SCENARIO_KEYS
        if unknown:
            raise ValueError(f"{path}: scenario {i}: unknown key(s) {', '.join(sorted(unknown))}")
        name = str(entry.get("name") or f"row-{i}")
        if name in seen:
            raise ValueError(f"{path}: two scenarios are named {name!r}")
        seen.add(name)
        merged = {**base, **{k: entry[k] for k in SETTINGS if k in entry}}
        missing = [k for k in ("url", "goal") if not merged.get(k)]
        if missing:
            raise ValueError(f"{path}: scenario {name!r} has no {' or '.join(missing)}")
        values = {section: {**(shared.get(section) or {}), **(entry.get(section) or {})} for section in ("fields", "choices")}
        data = parse_data(values, f"{path}: scenario {name!r}") if values["fields"] or values["choices"] else None
        try:
            steps = int(merged.get("steps", DEFAULT_STEPS))
        except ValueError as e:
            raise ValueError(f"{path}: scenario {name!r}: steps must be a whole number") from e
        out.append(
            Scenario(
                name=name,
                url=str(merged["url"]),
                goal=str(merged["goal"]),
                expect_url=str(merged["expect_url"]) if merged.get("expect_url") else None,
                steps=steps,
                data=data,
            )
        )
    return out


def csv_rows(path: Path) -> list[dict]:
    """One scenario entry per row. utf-8-sig, because Excel starts a UTF-8 CSV with a BOM."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            raise ValueError(f"{path}: no header row")
        rows = []
        for row in reader:
            entry: dict = {"fields": {}, "choices": {}}
            for column, cell in row.items():
                if column is None:
                    raise ValueError(f"{path}: row {reader.line_num} has more cells than the header")
                column, cell = column.strip(), (cell or "").strip()
                if not cell:
                    continue
                if column in ("name", *SETTINGS):
                    entry[column] = cell
                elif column.lower().startswith(CHOICE_PREFIX):
                    entry["choices"][column[len(CHOICE_PREFIX) :].strip()] = cell
                else:
                    entry["fields"][column] = cell
            rows.append(entry)
        return rows
