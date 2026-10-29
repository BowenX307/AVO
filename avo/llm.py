"""Shared Meta Model API helpers used by the agent loop and the confidence signals."""

from __future__ import annotations

import base64
import json
import os

from dotenv import load_dotenv
from openai import OpenAI

MODEL = "muse-spark-1.3"
BASE_URL = "https://api.meta.ai/v1"


def make_client() -> OpenAI:
    load_dotenv()
    key = os.environ.get("MODEL_API_KEY")
    if not key:
        raise SystemExit("MODEL_API_KEY is not set (put it in .env or the environment)")
    return OpenAI(base_url=BASE_URL, api_key=key)


def data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


def describe(actions: list[dict]) -> str:
    """Compact one-line form of an action batch, for the text history."""
    parts = []
    for a in actions:
        kind = a["type"]
        if kind in ("click", "double_click", "move"):
            button = a.get("button")
            extra = f" {button}" if button and button != "left" else ""
            parts.append(f"{kind}({a['x']},{a['y']}{extra})")
        elif kind == "keypress":
            parts.append(f"keypress({'+'.join(a['keys'])})")
        elif kind == "type":
            parts.append(f"type({json.dumps(a['text'], ensure_ascii=False)})")
        elif kind == "scroll":
            parts.append(f"scroll({a['x']},{a['y']} dy={a.get('scroll_y', 0)})")
        elif kind == "drag":
            path = a.get("path") or []
            if path:
                parts.append(f"drag({path[0]['x']},{path[0]['y']}->{path[-1]['x']},{path[-1]['y']})")
        else:
            parts.append(kind)
    return ", ".join(parts)
