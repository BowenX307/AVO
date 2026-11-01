# AVO evaluation

Real measurements only. Anything not yet measured says so. Every number here comes from a file in this repo or from a run log, named next to it.

## How tuning and reporting are kept apart

AVO's thresholds were tuned on one set of tasks, and results are reported on others it never saw while tuning.

**Used for tuning (development):**
- The 10/28 baseline: 10 tasks in `eval/tasks.json`, with 39 clicking steps labelled right or wrong by hand (`runs/baseline-20261028-232423/labels.json`, not in the repo). The gate's rule and its SAM threshold (`SAM_CLEAR = 0.8`) come from these.
- The live runs of 10/29–10/31 on the board, expense and moving demo pages and on metacareers.com. The answers given to AVO's questions in those runs were used to check the gate rule, so they are development data too.

**Used only for reporting:**
- **E2** (simulated tremor, `demo/targets.html`). The parameters were fixed before the one and only run, and nothing was changed after seeing the results.
- **The demo tasks** (`eval/demos.json`: mail, room, course). These were built after the gate's thresholds were set, and no threshold was changed after their runs. One task wording did change after a run: room's "this Friday" became "next Friday", because nobody reads "this Friday" (said on a Saturday) as ambiguous. That changes what the task tests, not how AVO decides.
- **E1** tasks will be new tasks, not used for anything else.

## E2 · Simulated tremor: clicking alone vs AVO

`node scripts/e2_capture.mjs`, then `uv run scripts/run_e2.py`. Results are in `eval/e2/results.json`; the chart is `docs/images/e2-tremor.svg`.

![Hit rate by button size, with a simulated tremor](images/e2-tremor.svg)

Each aim lands off by a Gaussian offset with standard deviation σ on each axis. There are 30 clicks per cell, on one 1280×800 screenshot of `demo/targets.html`: five rows of eight buttons, 4 px apart, with the same eight names in every row. That was 600 SAM 3.1 calls, $1.50, median SAM time 0.44 s.

- **Alone:** the click lands at the button's centre plus the offset. This is computed, not clicked.
- **AVO, SAM only:** a rough box MARGIN = 8 px beyond the button, each corner off by the offset. SAM 3.1 segments it, and AVO clicks the middle of the mask.
- **AVO, SAM + name:** the user also says the button's name. AVO takes the button of that name within 16 px of the box, using the page's own boxes, which is what macOS accessibility reports for Chrome. If no such button exists, it uses the SAM-only click.

| Button | Tremor σ | Alone | AVO, SAM only | AVO, SAM + name |
|---|---|---|---|---|
| 12 px | 4 px | 77% | 83% | 100% |
| 12 px | 8 px | 33% | 47% | 100% |
| 12 px | 12 px | 23% | 40% | 100% |
| 18 px | 4 px | 90% | 100% | 100% |
| 18 px | 8 px | 50% | 77% | 100% |
| 18 px | 12 px | 37% | 50% | 100% |
| 24 px | 4 px | 97% | 97% | 100% |
| 24 px | 8 px | 60% | 73% | 100% |
| 24 px | 12 px | 57% | 53% | 100% |
| 32 px | 4 px | 100% | 100% | 100% |
| 32 px | 8 px | 93% | 80% | 100% |
| 32 px | 12 px | 57% | 83% | 100% |
| 44 px | 4 px | 100% | 100% | 100% |
| 44 px | 8 px | 100% | 100% | 100% |
| 44 px | 12 px | 93% | 100% | 100% |

At σ = 0 every group hits 100%.

**Read with care:**
- The 100% for "SAM + name" assumes the app exposes its buttons' names and that AVO heard the name right. The rows on the test page sit further apart than in most apps.
- SAM alone sometimes does worse than clicking alone, for example a 32 px button at σ 8 px: 93% → 80%.
- The time a click takes is not modelled.

## Demo tasks · first scripted run

`uv run scripts/run_demo.py` serves the demo pages, checks each run against `eval/demos.json` from what the page reports, and uses a script to answer for the user (`avo/scripted.py`). These are single runs, so they only show that the tasks work; they are not a success rate.

| Task | Done right | Things said | Steps | Time | Cost |
|---|---|---|---|---|---|
| mail-smith | yes | 3 | 5 | 46 s | $0.049 |
| room-friday | yes | 3 | 5 | 49 s | $0.050 |
| course-week3 | yes | 2 | 2 | 22 s | $0.018 |

The room run above used the old wording, "this Friday"; it must be re-run with "next Friday".

## E1 · What it takes: Voice Control vs AVO

*In progress.* There will be about 12 everyday tasks, including 2–3 on real websites, each marked real or mock. For Voice Control: the fewest commands a practised user needs, done by hand and timed. For AVO: 3 runs per task, counting what was said (the task, every answer, every yes/no), success, time and cost.

## E3 · Silent errors

*In progress:* `uv run scripts/run_demo.py --modes full,irreversible,bare --runs 3`. On the tasks with a real ambiguity, this counts how often each setup finishes wrong without having asked:
- the agent alone;
- asking only before irreversible steps;
- AVO as shipped.
