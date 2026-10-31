# AVO

**Website:** https://bowenx307.github.io/AVO/ · **Demo video (3 min):** *coming soon*

Agents ask for permission. AVO asks for understanding.

Point at anything on your screen. When AVO isn't sure, it points back. You box what a task is about (A, and B if something goes somewhere) and say it in one line; a computer-use agent does the work, and before each uncertain click AVO rings the target on your screen and asks.

Built solo for the Meta Global AI Developer Hackathon 2026 (Indie track), on Meta's models: Muse Spark acts and checks each click, SAM 3.1 finds and rings targets.

## How it works

Computer-use models don't say how sure they are. Before every click, AVO runs a confidence check:

- **Model review:** Muse Spark looks at the planned click (with a zoomed-in crop) and says whether it hits what it means to, and whether several things fit the task.
- **Target clarity:** SAM 3.1 checks that the click lands on one clearly bounded element.
- **Repeat check:** the same action again and again means the agent is stuck.

If the check passes, AVO clicks. If not, it points back: it rings the target, or every candidate, and you answer with one click. It also pauses the moment you touch the mouse or keyboard, and looks at the screen again before each click in case something moved.

## Privacy & safety

- Screenshots, your task and the boxes you draw go to the Meta Model API, in Standard mode. When you speak to AVO, your voice (only while it shows it's listening) and names read from the front window go to Meta's speech recognition.
- Meta attaches a routine safety check, `computer_action_requires_confirmation`, to almost every action. AVO doesn't acknowledge it silently: it rings the target on screen and counts down one second, and you can say "no" to call it off. That counts only if the ring was really shown; otherwise AVO asks for a yes.
- Actions that can't be undone (send, submit, delete, pay, book) and every other safety check show a preview and wait for your "yes". Every confirmation is logged in `runs/<time>/`.

## Run it

macOS 15 on Apple silicon, Python 3.11+ with [uv](https://docs.astral.sh/uv/), Node.js, and a Meta Model API key.

```sh
uv sync
cp .env.example .env              # put your key after MODEL_API_KEY=
cd overlay && npm install && npm start
```

Give the terminal you start it from Screen Recording, Accessibility and Input Monitoring permission. Then click the capsule at the top of the screen and type a task, or press ⌥ Space to box something first. Full guide: [site/docs.html](site/docs.html).

## Layout

| Path | What's there |
| --- | --- |
| `avo/` | the agent: model loop, confidence check, input, screen capture |
| `overlay/` | the on-screen layer (Electron): cursor, selection, rings, status island |
| `brand/` | design tokens, fonts (Geist, SIL OFL), the logo and its drawing animation |
| `site/` | the website (static); preview with `python3 -m http.server -d site 8000` |
| `demo/` | practice pages with made-up data |
| `scripts/`, `eval/`, `tests/` | running tasks, the baseline and its labels, unit tests |

All demo data is fictional.
