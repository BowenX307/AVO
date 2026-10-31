"""Tests for metering API calls (avo/meter.py).

    uv run python -m unittest discover tests
"""

import unittest
from types import SimpleNamespace

from avo.meter import Meter, cost

IMG = {"type": "input_image", "image_url": "data:image/png;base64,"}


class Usage:
    def __init__(self, inp, out, cached=0, reasoning=0):
        self.d = {
            "input_tokens": inp,
            "output_tokens": out,
            "input_tokens_details": {"cached_tokens": cached},
            "output_tokens_details": {"reasoning_tokens": reasoning},
        }

    def model_dump(self):
        return self.d


class FakeResponses:
    def __init__(self, usage=None, fail=False):
        self.usage, self.fail = usage, fail

    def create(self, **kwargs):
        if self.fail:
            raise TimeoutError("slow")
        return SimpleNamespace(usage=self.usage, output_text="")


def client(**kw):
    return SimpleNamespace(responses=FakeResponses(**kw), api_key="k")


class MeterTest(unittest.TestCase):
    def test_purposes_and_cost(self):
        meter = Meter()
        act = meter.wrap(client(usage=Usage(4000, 120, reasoning=30)))
        act.responses.create(model="muse-spark-1.3", tools=[{"type": "computer"}], input=[{"role": "user", "content": [IMG]}])
        act.responses.create(model="muse-spark-1.3", tools=[{"type": "computer"}], input=[])
        review = meter.wrap(client(usage=Usage(2000, 80, cached=1000)))
        review.responses.create(model="muse-spark-1.3", text={"format": {"type": "json_schema", "name": "review"}}, input=[])
        sam = meter.wrap(client(usage=None))
        sam.responses.create(model="sam-3.1", input=[{"role": "user", "content": [{"type": "input_text"}, IMG]}])

        s = meter.summary()
        self.assertEqual(s["calls"], 4)
        by = s["by_purpose"]
        self.assertEqual(by["act"]["calls"], 2)
        self.assertEqual(by["act"]["input_tokens"], 8000)
        self.assertAlmostEqual(by["act"]["usd"], 2 * (4000 * 1.25 + 120 * 4.25) / 1e6, places=6)
        self.assertAlmostEqual(by["review"]["usd"], (1000 * 1.25 + 1000 * 0.15 + 80 * 4.25) / 1e6, places=6)
        self.assertEqual(by["sam"]["images"], 1)
        self.assertAlmostEqual(by["sam"]["usd"], 0.0025)
        self.assertAlmostEqual(s["usd"], sum(v["usd"] for v in by.values()), places=4)

    def test_failed_call_is_counted_and_raised(self):
        meter = Meter()
        c = meter.wrap(client(fail=True))
        with self.assertRaises(TimeoutError):
            c.responses.create(model="muse-spark-1.3", tools=[{"type": "computer"}], input=[])
        self.assertEqual(meter.summary()["by_purpose"]["act"]["errors"], 1)

    def test_other_attributes_pass_through(self):
        self.assertEqual(Meter().wrap(client()).api_key, "k")

    def test_unknown_model_costs_nothing(self):
        self.assertEqual(cost("someone-else", 1000, 0, 1000, 0), 0.0)


if __name__ == "__main__":
    unittest.main()
