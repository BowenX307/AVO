"""What a run cost and where its time went: every Meta Model API call, metered.

    meter = Meter()
    client = meter.wrap(make_client())    # use it as the client; each call is recorded
    meter.summary()                        # per purpose: calls, seconds, tokens, images, USD

A call's purpose is read off the request: "act" (the computer tool), a
structured-output call by its schema name ("review", "plan"), or "sam".
Prices come from pricing.json (Meta's published standard-mode prices).
"""

from __future__ import annotations

import json
import statistics
import threading
import time
from pathlib import Path

PRICING = json.loads((Path(__file__).with_name("pricing.json")).read_text())["models"]


def purpose(kwargs: dict) -> str:
    if kwargs.get("model", "").startswith("sam"):
        return "sam"
    if any(t.get("type") == "computer" for t in kwargs.get("tools") or []):
        return "act"
    return ((kwargs.get("text") or {}).get("format") or {}).get("name") or "other"


def _images(kwargs: dict) -> int:
    n = 0
    for item in kwargs.get("input") or []:
        content = item.get("content") if isinstance(item, dict) else None
        if isinstance(content, list):
            n += sum(1 for part in content if part.get("type") == "input_image")
    return n


def cost(model: str, input_tokens: int, cached: int, output_tokens: int, images: int) -> float:
    p = PRICING.get(model)
    if not p:
        return 0.0
    if "per_1k_images" in p:
        return images * p["per_1k_images"] / 1000
    return (
        (input_tokens - cached) * p["input_per_m"] + cached * p["cached_input_per_m"] + output_tokens * p["output_per_m"]
    ) / 1e6


class Meter:
    def __init__(self):
        self.calls: list[dict] = []
        self._lock = threading.Lock()  # the gate runs review and SAM in parallel

    def wrap(self, client):
        return _MeteredClient(client, self)

    def record(self, kwargs: dict, seconds: float, response=None, error: str | None = None) -> None:
        usage = getattr(response, "usage", None)
        u = usage.model_dump() if usage is not None else {}
        model = kwargs.get("model", "")
        call = {
            "purpose": purpose(kwargs),
            "model": model,
            "seconds": round(seconds, 2),
            "input_tokens": u.get("input_tokens") or 0,
            "cached_tokens": (u.get("input_tokens_details") or {}).get("cached_tokens") or 0,
            "output_tokens": u.get("output_tokens") or 0,
            "reasoning_tokens": (u.get("output_tokens_details") or {}).get("reasoning_tokens") or 0,
            "images": _images(kwargs),
        }
        call["usd"] = cost(model, call["input_tokens"], call["cached_tokens"], call["output_tokens"], call["images"])
        if error:
            call["error"] = error
        with self._lock:
            self.calls.append(call)

    def summary(self) -> dict:
        """Per purpose: model, calls, total and median seconds, tokens, images, USD; plus the total."""
        out: dict = {}
        with self._lock:
            calls = list(self.calls)
        for c in calls:
            s = out.setdefault(c["purpose"], {"model": c["model"], "calls": 0, "seconds": [], "errors": 0})
            s["calls"] += 1
            s["seconds"].append(c["seconds"])
            s["errors"] += 1 if "error" in c else 0
            for k in ("input_tokens", "cached_tokens", "output_tokens", "reasoning_tokens", "images", "usd"):
                s[k] = s.get(k, 0) + c[k]
        for s in out.values():
            secs = s.pop("seconds")
            s["seconds_total"] = round(sum(secs), 1)
            s["seconds_median"] = round(statistics.median(secs), 2)
            s["usd"] = round(s["usd"], 5)
        return {"by_purpose": out, "calls": len(calls), "usd": round(sum(c["usd"] for c in calls), 5)}


class _MeteredResponses:
    def __init__(self, inner, meter: Meter):
        self._inner = inner
        self._meter = meter

    def create(self, **kwargs):
        t0 = time.time()
        try:
            response = self._inner.create(**kwargs)
        except Exception as e:
            self._meter.record(kwargs, time.time() - t0, error=f"{type(e).__name__}: {e}"[:200])
            raise
        self._meter.record(kwargs, time.time() - t0, response)
        return response

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _MeteredClient:
    """The OpenAI client with responses.create metered; everything else passes through."""

    def __init__(self, client, meter: Meter):
        self._client = client
        self.responses = _MeteredResponses(client.responses, meter)

    def __getattr__(self, name):
        return getattr(self._client, name)
