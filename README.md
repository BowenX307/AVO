# AVO

**Say what you want done. AVO handles the clicks—and asks when your meaning is unclear.**

[Watch the 3-minute demo](https://bowenx307.github.io/AVO/#demo) · [Try the interaction, no key needed](https://bowenx307.github.io/AVO/try.html) · [Install on Mac](https://bowenx307.github.io/AVO/docs.html#install) · [Evaluation](docs/eval.md)

I broke my hand while climbing during a summer software engineering internship at Lenovo. I still needed to use my computer. AVO began with that experience: a Mac app for people who can speak clearly but find precise mouse or trackpad use difficult.

## The interaction

Say “Reply to Professor Smith, say I’ll be ten minutes late.” If two Smiths fit, AVO rings both and asks. Say “the physics one,” and it continues. Before a detected send or other irreversible action, it shows a preview and asks for explicit approval.

**“Which person do you mean?” and “Can I send this?” are different questions. AVO is designed to ask both.**

AVO Remote brings this exchange to a paired phone on the same Wi-Fi. Send a task to your Mac, receive a question with a cropped, numbered view of the relevant screen area, and answer without returning to the desk. Remote is off by default; Control–Option–R opens pairing. Losing the phone connection pauses the task.

## Review in three minutes

1. Watch the [submitted demo](https://bowenx307.github.io/AVO/#demo).
2. Explore the [no-install interaction walkthrough](https://bowenx307.github.io/AVO/try.html). It is an explicitly scripted illustration, not a live agent or benchmark.
3. Inspect the [27 E3 run records](eval/e3/results.json) and [methodology](docs/eval.md). To run the actual agent, use the setup guide below.

## How it is built

| Component | Role |
| --- | --- |
| Muse Spark 1.3 | Plans tasks and proposes computer-use actions; a separate structured-output call reviews proposed pointer batches for ambiguity and target location. |
| SAM 3.1 | Segments targets from points or rough boxes and supplies screen regions for highlights. |
| macOS accessibility | Reads control names and bounds, checks locations, and snaps near-miss clicks when the nearest target is unambiguous. |
| Muse Wonder 1.0 | Replans after repeated actions fail to change the screen. |
| Muse Voice Transcribe 1.0 | Realtime speech recognition, biased with visible names and the current answer choices. |
| Python + Electron | Local execution, visual questions, interruption handling, and the paired phone interface. |

All runtime AI calls use Meta models. Spark, Wonder and SAM use Meta’s Responses API; speech uses its realtime ASR endpoint. The OpenAI Python package is the API-compatible client, not an OpenAI model dependency. [Architecture and design decisions](docs/architecture.md).

## Evidence, with limits

E3 tested three deliberately ambiguous tasks, three times per setup. A silent error means a wrong result reported as completed without a clarification or target-check question. A yes/no send or booking confirmation is not counted as clarification.

| Setup | Silent errors | Correct outcomes |
| --- | --- | --- |
| Agent alone | 7/9 (78%) | 1/9 |
| Confirmation before irreversible actions only | 5/9 (56%) | 3/9 |
| Full AVO | 0/9 (0%) | 9/9 |

These are small controlled tests on fictional pages with scripted answers, not a user study or a general reliability estimate. All setups include the initial planner. Results were collected November 1; later fixes have not been re-benchmarked. [Raw results](eval/e3/results.json).

E2 measures simulated pointing offsets, not human tremor: 30 trials per condition. At 12 px targets and an 8 px offset standard deviation, direct pointing hit 33%, rough-box SAM 47%, and SAM plus name/known-target matching 100%. The last result depends on correctly recognized names and available target bounds; it is not SAM-only performance. E1 is an exploratory comparison with a first-time Voice Control user and scripted AVO answers; it does not establish superiority over experienced Voice Control users. [Full methods and limitations](docs/eval.md).

## Run on your Mac

Tested on macOS 15.5, Apple silicon; Python 3.11+, uv, Node.js 24 and a Meta Model API key are required. There is no packaged DMG yet.

```sh
git clone https://github.com/BowenX307/AVO.git
cd AVO
./scripts/setup.sh
```

Setup installs the locked dependencies and prompts once for your key without echoing it. The key stays in your git-ignored `.env`; never share it. To launch later:

```sh
./scripts/start.sh
```

Grant Screen Recording, Accessibility and Input Monitoring to the terminal you launch from, then quit and reopen that terminal. Microphone permission is requested when speaking. Tap right Option to speak, or click the top capsule to type. Start with `demo/mail.html` in Chrome and the Smith task above. Use fictional data while trying it. [Permissions, shortcuts and troubleshooting](https://bowenx307.github.io/AVO/docs.html).

**Actual computer control requires a key.** The browser walkthrough needs none because it runs no model and controls no computer. An author-funded live trial would require a separately hosted, authenticated, quota-limited service; no shared key is embedded in this repository.

## Current limits

- Working prototype; not yet evaluated with the intended users and not a screen-reader interface.
- Browser tasks are more reliable than complex Finder workflows. A November 4 file-packaging rehearsal navigated in circles and was stopped; it is not presented as a success.
- Main display only. Keep the working app visible and avoid moving windows during execution.
- Requires internet and Meta API access. Default task limits are 20 turns and approximately $0.50; an in-flight batch may exceed the spending threshold, and realtime speech is not included in that meter.
- Irreversible-action detection uses control labels, keyboard shortcuts and review output; it is not a guarantee. Supervise consequential actions.
- Remote uses local HTTP/WebSocket, not encrypted transport. Use only a trusted private network; do not port-forward it. Phone dictation uses the phone keyboard, not AVO’s Mac speech endpoint.
- macOS Voice Control currently conflicts with the overlay; do not run both together.

## Validation and provenance

```sh
uv run python -m unittest discover -s tests
(cd overlay && node test-remote.js)
python3 scripts/check_site.py
python3 scripts/check_evidence.py
```

Unit tests mock model responses and do not prove live model reliability. Remote tests require a private-network interface. See the [submission audit](docs/submission-audit.md) for the final checks and remaining gaps.

Built by Bowen for the Meta Global AI Developer Hackathon 2026, Indie track / Build for Your World. Development began October 28 within the event window; commit dates are preserved. [MIT license](LICENSE); bundled Geist fonts retain their [SIL OFL license](brand/fonts/OFL.txt).
