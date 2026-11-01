# AVO

**Website:** https://bowenx307.github.io/AVO/ · **Demo video (3 min):** *coming soon*

**Say what you want done. AVO handles the clicks — and asks when it isn't sure.**

For people who find using a mouse or trackpad difficult: who can speak clearly, but pay a price for every precise move of the hand (it can't be done, can't be done accurately, hurts, tires, or takes long). Tap right ⌥, say the task — *"Reply to Professor Smith, say I'll be ten minutes late"* — and a computer-use agent does the clicking. When more than one thing fits (two Professor Smiths), AVO rings each on screen with a number and you answer with a word: *"two"*. Before anything that can't be undone, it shows what it will do and waits for *"yes"*.

Agents ask for permission. AVO asks for understanding.

Not the right tool if speaking is hard for you (AVO's main input is your voice), or if you rely on a screen reader (AVO asks by drawing on the screen).

Built solo for the Meta Global AI Developer Hackathon 2026 (Indie track), on Meta's models: Muse Voice Transcribe hears you, Muse Spark plans, acts and checks each step, Muse Wonder re-plans when AVO gets stuck, SAM 3.1 finds the exact target inside a rough box.

## How it works

Computer-use models don't say how sure they are, so AVO checks each step before it happens:

- **Several things fit?** Muse Spark reviews the planned click; if it picks one of several options that all match the task, AVO rings them all and asks which.
- **A clear target?** macOS accessibility says whether a control is under the click; where it can't tell, SAM 3.1 must be sure the point is on one clearly bounded element. If neither is sure, the review decides whether to ask "OK to click here?".
- **Can it be undone?** An ordinary step gets a ring on its target and a one-second countdown (say "no" to stop it). Send, submit, delete, pay, book: a preview, and nothing happens until you say "yes".

It also stops a click that already did nothing from running again, makes a fresh plan when it gets stuck, and asks when it wanders into your mail or messages for a task that doesn't mention them. Say "stop", or tap right ⌥, any time.

Measured so far — simulated tremor on buttons of five sizes (E2): a 12 px button with 12 px of tremor is hit 23% of the time clicking alone, 40% with a rough box and SAM 3.1, 100% with a rough box and the button's name ([details](eval/e2/), [chart](docs/images/e2-tremor.svg)). The Voice Control comparison (E1) and the silent-error test (E3) are in progress; see [docs/eval.md](docs/eval.md).

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

Give the terminal you start it from Screen Recording, Accessibility and Input Monitoring permission (macOS asks for the Microphone the first time you speak). Then tap right ⌥ and say a task — or click the capsule at the top of the screen and type it, or press ⌥ Space to box something first. `demo/mail.html` is a safe place to start. Full guide: [site/docs.html](site/docs.html).

## Known limits

- Not yet tried by the people it's for. When it has been, this will say what they said.
- Relies on your voice; typing works as a fallback (click the capsule).
- Needs a network connection and the Meta Model API.
- macOS only, on the main display only: a second monitor isn't supported yet.
- Turn on Do Not Disturb before a task: a notification that pops up lands in AVO's screenshots and can get in the way.
- Best in the browser; desktop apps such as Finder are harder for it.
- Each task stops at 20 steps or $0.50 of API calls (`--max-steps`, `--max-usd`).

## Written for the hackathon

All code here was written during the hackathon window (it opened October 26, 2026; the first commit is from October 28): the git history is the record. The fonts (Geist, SIL OFL) are the only third-party assets, and every demo page uses made-up data.

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
