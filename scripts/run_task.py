"""Run the computer-tool agent on a task given on the command line.

    uv run scripts/run_task.py "In the open Chrome window, go to example.com"
"""

import argparse

from avo.agent import run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task", help="what the agent should do")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="ask before every action, including routine safety checks",
    )
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
        "--no-snap",
        action="store_true",
        help="don't move clicks that land just off a button or field onto it",
    )
    args = parser.parse_args()
    print("Agent is taking over the mouse. Fling it into a screen corner to abort.")
    run(
        args.task,
        max_steps=args.max_steps,
        auto_ack=not args.confirm,
        effort=None if args.effort == "default" else args.effort,
        history=args.history,
        gate=not args.no_gate,
        snap_to_controls=not args.no_snap,
    )


if __name__ == "__main__":
    main()
