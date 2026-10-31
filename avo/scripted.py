"""A script that plays the user, for repeatable runs (scripts/run_demo.py).

It stands where the overlay stands (the agent's `overlay`), sees no screen,
and answers AVO's questions from the task's spec:

  - "Which one did you mean?" / the planner's question: the option whose text
    has the words the user means (spec "means"), e.g. "Physics" for the
    professor teaching Physics 212. None matches: "none of these" plus those
    words, as a person would say it.
  - "OK to click here?", "Send — OK?", an unusual safety check: yes.
  - the countdown before an ordinary step: stays silent, so it runs. The run's
    settings say answerer=script, so a log never passes for a person's.

Every question and answer is kept in `asked`, for the run's results.
"""

from __future__ import annotations

import re

from avo.overlay import Overlay


def _score(label: str, means: list[str]) -> int:
    text = label.lower()
    return sum(1 for m in means if re.search(rf"(?<!\w){re.escape(m.lower())}(?!\w)", text))


class ScriptedUser(Overlay):
    answerer = "script"

    def __init__(self, means: list[str]):
        super().__init__(url=None)
        self.means = means
        self.asked: list[dict] = []

    @property
    def active(self) -> bool:
        return True

    def send(self, type: str, **fields) -> None:
        pass

    def pick(self, labels: list[str]) -> int | None:
        scores = [_score(label, self.means) for label in labels]
        best = max(scores, default=0)
        return scores.index(best) if best > 0 and scores.count(best) == 1 else None

    def ask(self, **fields) -> dict | None:
        kind = fields.get("kind")
        labels = [c["label"] for c in fields.get("candidates") or []] or fields.get("options") or []
        if kind == "countdown":
            answer = {"choice": "go", "shown": True, "how": "script"}
        elif labels:
            i = self.pick(labels)
            answer = (
                {"choice": "pick", "index": i, "how": "script"} if i is not None
                else {"choice": "none", "how": "script", "said": "none of these, I mean the " + " ".join(self.means[:2])}
            )
        else:  # "OK to click here?", "Send — OK?", a safety check
            answer = {"choice": "yes", "how": "script"}
        if kind != "countdown":
            self.asked.append({"kind": kind or ("which" if labels else "ok"), "labels": labels,
                               "title": fields.get("title") or fields.get("question") or "", "answer": answer})
        return answer

    def pause_requested(self) -> bool:
        return False

    def wait_while_paused(self, tick=None, every: float = 0.25) -> bool:
        self._control = None  # nobody to press Resume: go on
        return True

    def close(self) -> None:
        pass
