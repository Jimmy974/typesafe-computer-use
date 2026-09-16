# typesafe-clicker

Efficient computer use on macOS: deterministic screen capture, a TypeSafe
classifier that picks the next action, and a small writing model that is only
consulted when free text must be produced.

```
cd ~/Projects/typesafe-clicker
uv sync

cp .env.example .env                   # then fill it in; or export the same variables
# TYPESAFE_API_KEY   required
# ANTHROPIC_API_KEY  optional: enables type_text and writer-proposed URLs
# CLICKER_EMAIL      optional: enables type_email

uv run clicker.py "go to cnn and click onto something related to AI on the homepage"        # dry run
uv run clicker.py "go to cnn and click onto something related to AI on the homepage" --act  # drives the machine
uv run inspect_screen.py "same goal"                                                         # see what the model sees
```

Clear the terminal before an `--act` run. The terminal is on screen, so its
text becomes OCR input.

## How it works

Each step:

1. `screencapture` grabs the main display.
2. Apple Vision OCR (`ocrmac`) returns text lines with pixel boxes. Lines that
   continue a block above them (aligned left edge, small gap, similar height)
   merge into one block, so a four-line headline is one target. Lines that echo
   the goal itself are dropped; those are the terminal command that started
   the run.
3. The accessibility API reports the focused element: role, label,
   placeholder, current value, frame. AppleScript reports the frontmost app
   and the browser's active tab URL. State also carries the local date and
   time.
4. Dates in OCR text are parsed in code (month-name, numeric, ISO forms) and
   each dated block gets `dated 2026-10-13 (in 27 days)`. Blocks within a
   short vertical distance of a dated line get `near a line dated ...`, so a
   "Register Now" button inherits its event's date. The classifier does no
   calendar math, so the comparison is handed to it as a fact.
5. One TypeSafe request carries three Choices:
   - `kind`: what sort of action, from the fixed set below plus `click_item`.
   - `item`: which OCR block, used only when `kind` is `click_item`.
   - `site`: which catalog site, used only when `kind` is `open_site`.
6. The winning action runs deterministically. An OCR index clicks the block's
   center, converted from Retina pixels to screen points.

The classifier never generates text. The only free text comes from the writer
model (`CLICKER_WRITER_MODEL`, default `claude-haiku-4-5`), in two places:

- `type_text`: given the goal, recent actions, the focused field's label and
  placeholder, and nearby screen text, it returns `{fill, text}`. Credential
  fields come back `fill: false` and nothing is typed. After typing, a
  TypeSafe Noul scores whether the field now holds a sensible value; under
  0.5 the field is cleared.
- `open_site` with no catalog match: it returns `{ok, url}`. Code rejects
  anything that is not a clean https URL with a hostname.

## Action space

| key | does |
|---|---|
| `click_item` | click the center of the OCR block chosen by the `item` question |
| `open_site` | AppleScript `open location` in Chrome, for a `SITES` catalog entry or a writer-proposed URL |
| `switch_to_browser` | bring Chrome to the front for a page already open; not for reaching a new site |
| `type_text` | writer composes the string; refused unless a text field is focused |
| `type_email` | types `$CLICKER_EMAIL`; refused unless a text field is focused |
| `press_enter`, `press_escape` | keyboard |
| `scroll_down`, `scroll_up` | 10 lines, after parking the cursor over the frontmost window (macOS scrolls whatever is under the cursor) |
| `wait` | screen still loading |
| `done`, `none` | stop |

Keep actions mutually exclusive. Every stall so far came from two options that
meant the same thing: the vote splits, and confidence, which measures
concentration, reads as doubt.

## Stopping

- Ctrl-C in the terminal, when it has focus.
- Slam the mouse into the top-left corner. Checked before every step and every
  100 ms during the post-action delay. Works from any app.
- On its own: `done` or `none`, confidence under `--min-confidence` (0.4),
  two consecutive no-ops (a refused or failed action, `wait`, or the same
  click repeated on the same URL), or `--steps` (12).

## Run folder

Every run writes `runs/<timestamp>/`:

| file | contents |
|---|---|
| `run.log` | everything printed |
| `run.json` | goal, outcome, seconds, every action taken, arguments |
| `step-NN-raw.png` | the capture |
| `step-NN.png` | OCR blocks numbered in blue, the chosen one in red, focused field in green |
| `step-NN-payload.txt` | the exact `state` and the three criteria maps sent to TypeSafe, then a table of every block with confidence, pixel box, click point, region, text |
| `step-NN-answers.json` | every probability the classifier returned, plus items, field, app, URL |

`inspect_screen.py` produces the same capture, annotated image, and payload
for the current screen after a 3-2-1 countdown, into `inspections/<timestamp>/`,
and opens the image and text file. `--no-open` only writes them.

## Flags

```
--act                 drive the machine (default: dry run, one step)
--steps N             max actions (12)
--min-confidence X    gate on the winning question's confidence (0.4)
--delay S             seconds to wait after each action (2.0)
--out DIR             run folder (runs/<timestamp>)
--image PNG --app A --url U   replay a saved capture as if app A were in front at URL U; never acts
```

Replay is how to iterate on prompts and criteria without touching the screen:
take `step-NN-raw.png` from a run that stalled, change the code, replay it.

## Permissions

System Settings > Privacy & Security, for your terminal app:

- Screen Recording, or `screencapture` returns the wallpaper.
- Accessibility, or synthetic clicks and keystrokes are dropped. `--act`
  refuses to start without it.

## Known limits

- OCR only sees text. Icon-only buttons and text over photos are invisible or
  garbled. The accessibility tree of the page is the next layer to add.
- Two identical labels on screen get only a coarse region hint.
- Passwords are never typed. Rely on Chrome's password manager or an SSO
  button the OCR can read.
- Using the machine during an `--act` run fights it for focus and the cursor.
- Multi-display: only the main display is captured.
