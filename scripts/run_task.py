"""Run the computer-tool agent on a task given on the command line.

    uv run scripts/run_task.py "In the open Chrome window, go to example.com"
"""

import argparse

from avo.agent import run
from avo.driver import FRESH_THRESHOLD


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task", help="what the agent should do")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument(
        "--effort",
        default="minimal",
        help="reasoning effort: minimal, low, medium, high, max, or 'default' for the model's own",
    )
    parser.add_argument(
        "--history",
        choices=["text", "full"],
        default="text",
        help="text: latest screenshot + text log of past steps (fast); "
        "full: every past screenshot via previous_response_id (slower each step)",
    )
    parser.add_argument(
        "--no-gate",
        action="store_true",
        help="skip the confidence check and on-screen confirmation before clicks",
    )
    parser.add_argument(
        "--region",
        action="append",
        default=[],
        metavar="A=l,t,r,b",
        help="a box the user selected, in screen points (the overlay passes these)",
    )
    parser.add_argument(
        "--no-snap",
        action="store_true",
        help="don't move clicks that land just off a button or field onto it",
    )
    parser.add_argument(
        "--no-human-guard",
        action="store_true",
        help="don't pause when you touch the mouse or keyboard or switch apps mid-task",
    )
    parser.add_argument(
        "--human-resume",
        choices=["manual", "auto"],
        default="manual",
        help="after you moved: wait for Resume (manual) or resume once you keep still for 3 s (auto)",
    )
    parser.add_argument(
        "--no-freshness",
        action="store_true",
        help="don't re-check the screen around a target right before acting on it",
    )
    parser.add_argument(
        "--fresh-threshold",
        type=float,
        default=FRESH_THRESHOLD,
        help="how much the screen around a target may change (0-1) before a click is called off",
    )
    parser.add_argument(
        "--app",
        metavar="BUNDLE_ID",
        help="the app to work in, e.g. com.google.Chrome (the overlay passes the one you were in); "
        "default: the front app",
    )
    parser.add_argument(
        "--no-tools",
        action="store_true",
        help="don't offer the model open_url / open_app; it opens pages and apps with the keyboard and mouse",
    )
    parser.add_argument(
        "--plan",
        choices=["auto", "always", "never"],
        default="auto",
        help="plan first (start, steps, what done looks like): auto = unless you boxed what the task is about",
    )
    parser.add_argument(
        "--voice",
        action="store_true",
        help="the task was spoken (the overlay passes this): only clicks count as you taking over, "
        "not mouse moves, scrolls or keys",
    )
    args = parser.parse_args()
    print("Agent is taking over the mouse. Move it to pause; fling it into a screen corner to abort.")
    run(
        args.task,
        max_steps=args.max_steps,
        effort=None if args.effort == "default" else args.effort,
        history=args.history,
        gate=not args.no_gate,
        snap_to_controls=not args.no_snap,
        regions={label: tuple(int(v) for v in box.split(",")) for label, box in (r.split("=", 1) for r in args.region)},
        human_guard=not args.no_human_guard,
        human_resume=args.human_resume,
        freshness=None if args.no_freshness else args.fresh_threshold,
        app=args.app,
        tools=not args.no_tools,
        plan=args.plan,
        voice=args.voice,
    )


if __name__ == "__main__":
    main()
