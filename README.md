# typesafe-clicker

Screen OCR -> TypeSafe `Choice` -> mouse click. macOS only.

```
uv sync
export TYPESAFE_API_KEY=...
uv run clicker.py "open the Settings menu"            # dry run, writes runs/<ts>/step-01.png
uv run clicker.py "open the Settings menu" --click    # actually clicks
uv run clicker.py "log in" --click --steps 5 --json   # multi-step loop
```

Permissions the terminal needs (System Settings > Privacy & Security):

- Screen Recording, or `screencapture` returns only the wallpaper.
- Accessibility, or the synthetic click is silently dropped.

How it works: every OCR line becomes one Choice option keyed by its index.
The answer is an index plus a probability over every option plus a confidence.
Below `--min-confidence` (default 0.5) nothing is clicked. The chosen box's
center, divided by the Retina scale, is the click point.
