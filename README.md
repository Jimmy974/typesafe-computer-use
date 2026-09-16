# typesafe-clicker

Screen OCR -> TypeSafe `Choice` -> mouse/keyboard action. macOS only.

```
cd ~/Projects/typesafe-clicker
export TYPESAFE_API_KEY=...          # or: export $(grep TYPESAFE_API_KEY path/to/.env)
export ANTHROPIC_API_KEY=...         # optional; enables the type_text action (writer model)
export CLICKER_EMAIL=you@example.com # optional; enables the type_email action

uv run clicker.py "please get to launchdarkly and log me in"          # dry run: one step, no input
uv run clicker.py "please get to launchdarkly and log me in" --act    # drives the machine, up to 12 steps
uv run clicker.py "goal" --act --steps 20 --delay 3 --json            # longer, slower, with per-step dumps
uv run clicker.py "goal" --image runs/<ts>/step-03-raw.png --app "Google Chrome"   # replay a saved screen
```

## Stopping it

- Ctrl-C in the terminal, when the terminal has focus.
- Slam the mouse into the top-left corner of the screen. Checked before every
  step and every 100 ms during the post-action delay. Works from any app.
- It also stops on its own when the model answers `done` or `none`, when
  confidence drops under `--min-confidence` (0.5), or after `--steps`.

## How a step is decided

One TypeSafe request carries three Choices: `kind` (what sort of action, ten
options), `item` (which OCR line, only used when kind is `click_item`), and
`site` (which known website, only used for `open_site`). Splitting them keeps
screen noise from diluting the action decision. State includes the frontmost
app, the focused accessibility element (role, label, placeholder, value), the
last eight actions, and every OCR line in reading order.

## Action space

Every OCR line on screen is one option, keyed by its index. Alongside those,
a fixed set of deterministic actions is always offered:

| key | does |
|---|---|
| `go_to_browser` | activate Chrome; if a site is needed, Cmd-L, type the URL picked by a second Choice over `SITES`, Return |
| `type_email` | types `$CLICKER_EMAIL` (only offered when set) |
| `press_enter`, `press_escape` | keyboard |
| `scroll_down`, `scroll_up` | 10 lines |
| `wait` | screen still loading |
| `done`, `none` | stop |

Add sites to `SITES` in `clicker.py`. Passwords are never typed: the writer
is told to decline credential fields, and it returns a structured
`{fill: false}` for them. Rely on Chrome's password manager or SSO buttons
that OCR can see.

The classifier never generates text. Free text exists only through
`type_text`, where the writer sees a small packet: goal, recent actions, the
focused field's label and placeholder, and the OCR lines near the field.

## Permissions

System Settings > Privacy & Security, for your terminal app:

- Screen Recording, or `screencapture` returns only the wallpaper.
- Accessibility, or synthetic clicks and keystrokes are dropped. `--act`
  refuses to start without it.

## Output

Each run writes `runs/<timestamp>/step-NN-raw.png` (what the model saw) and
`step-NN.png` (OCR boxes numbered, chosen box in red). `--json` adds the OCR
items and the full probability distribution per step.

## Inspecting what the model sees

```
uv run inspect_screen.py "your goal"     # 3-2-1 countdown, then capture
```

Opens two things: the screenshot with every OCR block outlined and numbered
(green = focused field), and `state.txt` with the exact `state` and the three
Choice criteria maps that would go to TypeSafe for that goal, followed by a
per-block table of pixel boxes, click points, and confidences. Files land in
`inspections/<timestamp>/`. Pass `--no-open` to only write them.
