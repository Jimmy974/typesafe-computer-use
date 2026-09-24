"""Scenario files: TOML, CSV, and TOML pointing at a CSV, merged and checked before anything runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from typesafe_computer_use.scenarios import load_scenarios

REPO = Path(__file__).resolve().parent.parent


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_the_example_batch_merges_defaults_rows_and_its_own_scenario():
    scenarios = {s.name: s for s in load_scenarios(REPO / "examples" / "pizza-orders.toml")}
    assert list(scenarios) == ["amy-no-toppings", "jimmy-medium", "ken-small"]
    amy, jimmy = scenarios["amy-no-toppings"], scenarios["jimmy-medium"]
    assert "leave every topping unticked" in amy.goal and amy.data.fields["email"] == "amy@example.com"
    assert jimmy.goal.startswith("Fill the pizza order form") and jimmy.expect_url == "httpbin.org/post"
    assert jimmy.data.fields == {"email": "orders@example.com", "customer_name": "Jimmy Wong", "telephone": "12345678"}
    assert jimmy.data.choices == {"size": "Medium", "topping": "Bacon"}


def test_a_csv_alone_takes_its_settings_from_the_command_line_and_keeps_cells_as_text(tmp_path):
    path = write(tmp_path / "o.csv", "﻿name,phone,choice:size,goal\na,0123,Medium,\nb,,Large,Just open it\n")
    a, b = load_scenarios(path, defaults={"url": "https://x.test", "goal": "Fill it", "expect_url": None})
    assert a.data.fields == {"phone": "0123"} and a.goal == "Fill it" and a.expect_url is None
    assert b.data.fields == {} and b.data.choices == {"size": "Large"} and b.goal == "Just open it"


def test_a_scenario_with_no_values_runs_without_a_data_file(tmp_path):
    path = write(tmp_path / "s.toml", 'url = "https://x.test"\n[[scenario]]\nname = "look"\ngoal = "Open the status page"\n')
    [look] = load_scenarios(path)
    assert look.data is None


def test_rows_are_named_by_position_when_the_csv_has_no_name_column(tmp_path):
    path = write(tmp_path / "o.csv", "phone\n1\n2\n")
    assert [s.name for s in load_scenarios(path, defaults={"url": "u", "goal": "g"})] == ["row-1", "row-2"]


@pytest.mark.parametrize(
    "text, complaint",
    [
        ('url = "u"\ngoal = "g"\n', "no scenarios"),
        ('url = "u"\n[[scenario]]\nname = "a"\n', "has no goal"),
        ('url = "u"\ngoal = "g"\n[[scenario]]\nname = "a"\n[[scenario]]\nname = "a"\n', "two scenarios are named 'a'"),
        ('url = "u"\ngoal = "g"\n[[scenario]]\nname = "a"\nfeilds = {x = "1"}\n', "unknown key"),
        ('url = "u"\ngoal = "g"\nretry = 3\n[[scenario]]\nname = "a"\n', "unknown key"),
        ('url = "u"\ngoal = "g"\n[[scenario]]\nname = "a"\nfields = {password = "x"}\n', "credential"),
        ('url = "u"\ngoal = "g"\n[[scenario]]\nname = "a"\nsteps = "many"\n', "whole number"),
    ],
)
def test_a_bad_scenario_file_stops_before_any_browser(tmp_path, text, complaint):
    with pytest.raises(ValueError, match=complaint):
        load_scenarios(write(tmp_path / "s.toml", text))


def test_a_csv_row_with_more_cells_than_the_header_is_an_error(tmp_path):
    with pytest.raises(ValueError, match="more cells than the header"):
        load_scenarios(write(tmp_path / "o.csv", "phone\n1,2\n"), defaults={"url": "u", "goal": "g"})
