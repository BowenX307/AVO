"""Run the computer-tool agent on a task given on the command line.

    uv run scripts/run_task.py "在已经打开的 Chrome 窗口里，打开 example.com"
"""

import argparse

from avo.agent import run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("task", help="what the agent should do")
    parser.add_argument("--max-steps", type=int, default=20)
    args = parser.parse_args()
    print("Agent is taking over the mouse. Fling it into a screen corner to abort.")
    run(args.task, max_steps=args.max_steps)


if __name__ == "__main__":
    main()
