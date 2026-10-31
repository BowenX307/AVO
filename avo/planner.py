"""A short plan before acting: where to start, the steps, and what "done" looks like.

Without one, an open task ("help me find a Meta internship") starts from
whatever is on screen and wanders. One muse-spark call with structured output,
on the task and the first screenshot, returns:

    start      "url" (open this address), "app" (open this app) or "here"
    steps      at most MAX_STEPS short steps
    done_when  what is on screen when the task is complete, so the agent stops
    question   only for a real ambiguity, asked before anything is done

The agent opens the start itself (no turn spent on the address bar) and puts
the plan in front of the model on every turn. The first plan is Muse Spark's:
it is a few lines, and Wonder costs more and was no better at it. A re-plan,
after the agent got stuck, is Muse Wonder's (REPLAN_MODEL): on the stuck "open
the math folder" run, Spark planned the same Finder search again, while
Wonder saw the search was scoped to the wrong folder (5.2 s, about $0.02).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from openai import OpenAI

from avo.llm import MODEL, data_url

MAX_STEPS = 6
REPLAN_MODEL = "muse-wonder-1.0"

PLAN_PROMPT = """\
You plan a computer task for an agent that works a Mac through screenshots. \
The screenshot shows the screen now.

Task: {task}
{context}
Return:
- start_kind / start_target: where the agent should begin. "url" with a full web address when \
the task is on a website (the official site when one is obvious, e.g. https://www.metacareers.com/jobs \
for jobs at Meta; a search page with the query in the address is fine). "app" with the app's name \
when the task is in a desktop app. "here" (target empty) when the screen already shows the right place.
- steps: 1 to {max_steps} short steps, in order, as a person would do them.
- done_when: what will be on screen when the task is complete, concretely.
- question: empty, unless the task can be read in clearly different ways that lead to different \
results and the screen doesn't settle it. Then one short question for the user. Don't ask about \
anything the agent can find out by looking, or where a sensible default exists.
- options: 2 to 4 short answers to the question; empty when there is no question.
Write in English."""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "start_kind": {"type": "string", "enum": ["url", "app", "here"]},
        "start_target": {"type": "string"},
        "steps": {"type": "array", "items": {"type": "string"}},
        "done_when": {"type": "string"},
        "question": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["start_kind", "start_target", "steps", "done_when", "question", "options"],
    "additionalProperties": False,
}


@dataclass
class Plan:
    start_kind: str
    start_target: str
    steps: list[str]
    done_when: str
    question: str = ""
    options: list[str] = field(default_factory=list)
    answer: str = ""  # the user's answer to the question, if one was asked
    opened: bool = False  # the agent has opened the start itself
    seconds: float = 0.0

    @classmethod
    def parse(cls, raw: dict, seconds: float = 0.0) -> Plan:
        kind = raw.get("start_kind") if raw.get("start_kind") in ("url", "app", "here") else "here"
        target = (raw.get("start_target") or "").strip()
        options = [o.strip() for o in raw.get("options") or [] if o.strip()][:4]
        question = (raw.get("question") or "").strip()
        return cls(
            start_kind=kind if target or kind == "here" else "here",
            start_target=target if kind != "here" else "",
            steps=[s.strip() for s in raw.get("steps") or [] if s.strip()][:MAX_STEPS],
            done_when=(raw.get("done_when") or "").strip(),
            question=question if len(options) >= 2 else "",
            options=options if question and len(options) >= 2 else [],
            seconds=seconds,
        )

    def start_action(self) -> dict | None:
        """The driver action that opens the start, or None to begin where the screen is."""
        if self.start_kind == "url":
            return {"type": "open_url", "url": self.start_target}
        if self.start_kind == "app":
            return {"type": "open_app", "name": self.start_target}
        return None

    def brief(self) -> str:
        """The plan as the model sees it on each turn."""
        lines = ["AVO's plan (a guide; trust the screen if it says otherwise):"]
        if self.start_kind != "here" and not self.opened:
            lines.append(f"Begin at: {self.start_target}")
        lines += [f"{i}. {s}" for i, s in enumerate(self.steps, 1)]
        if self.done_when:
            lines.append(f"Done when: {self.done_when} When it is, stop and reply.")
        if self.answer:
            lines.append(f"Asked “{self.question}”, the user answered: {self.answer}")
        return "\n".join(lines)

    def to_log(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


def make_plan(
    client: OpenAI, task: str, png: bytes, past: list[str] = (), effort: str | None = "minimal", model: str = MODEL
) -> Plan:
    """Plan the task from the current screen. past: steps already taken, when re-planning."""
    context = ""
    if past:
        context = (
            "\nThe agent already took these steps and got stuck (its actions stopped changing the screen); "
            "plan the rest from the screen as it is now:\n" + "\n".join(past[-8:]) + "\n"
        )
    prompt = PLAN_PROMPT.format(task=task, context=context, max_steps=MAX_STEPS)
    kwargs = {"reasoning": {"effort": effort}} if effort else {}
    t0 = time.time()
    response = client.responses.create(
        model=model,
        input=[
            {
                "role": "user",
                "content": [{"type": "input_text", "text": prompt}, {"type": "input_image", "image_url": data_url(png)}],
            }
        ],
        # "replan" and "plan" are metered apart (avo/meter.py reads the schema name).
        text={"format": {"type": "json_schema", "name": "replan" if past else "plan", "strict": True, "schema": PLAN_SCHEMA}},
        **kwargs,
    )
    return Plan.parse(json.loads(response.output_text), time.time() - t0)
