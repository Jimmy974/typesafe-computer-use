# AGENTS.md

Rules for agents working in this repository. Humans: see [CONTRIBUTING.md](CONTRIBUTING.md).

## Ground rules

Everything in [CONTRIBUTING.md](CONTRIBUTING.md) applies: the action set stays mutually
exclusive, code computes facts and the classifier only picks, free text comes only from
`writer.py`, platform calls live only in `macos.py`, and nothing ever types a password.

Before a pull request:

```
uv run ruff check . && uv run ruff format .
uv run pytest -q
```

## Evaluation suite

`evals/` is the regression suite for the loop itself. It has two tiers:

- **Goldens** (`evals/goldens/<name>/`): one frozen step, replayed offline. Cheap, fast,
  runs in CI. Scored on whether the classifier still picks the expected action.
- **Live cases** (`evals/cases.py`): one line per task. Drives the real Mac and scores the
  outcome. Runs only on a developer machine.

### Every ad hoc goal is a candidate test case

Whenever a goal is tried outside the suite, live (`clicker "..." --act`) or as a replay
(`--image`), end the work by proposing how to capture it, in this form:

1. A `Case(...)` line for `evals/cases.py`, with the goal, the start state, the check that
   proves the outcome, and the step budget the run needed.
2. For any step whose decision was wrong, stalled, or was just fixed, the freeze command
   that turns that step into a golden, with the expected action:

   ```
   uv run clicker-eval freeze runs/<ts> <step> --name <short-name> --expect <action>
   ```

Propose; do not add silently. A case the user rejects is not added. A case the user
accepts is added in the same change as the fix it protects.

### When to run which tier

- Any change to `perception.py`, `decide.py`, `dates.py`, or `config.py` changes what the
  model sees. Run the goldens before and after, and put the score delta in the PR.
- Any change to `actions.py`, `macos.py`, or `runner.py` changes what happens on the
  machine. Run the live cases that touch the changed path, at least three repetitions.
- A lower score is a finding, not a failure to hide. Report it with the run folder.
