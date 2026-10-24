"""Model-usage accounting for the usage report."""

from __future__ import annotations

from collections import defaultdict

# USD per million tokens (input, output); used only for cost estimates in the report.
PRICES = {"claude-haiku-5-5": (0.10, 0.50)}


class UsageTracker:
    def __init__(self):
        self.per_model = defaultdict(lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0})
        self.requests = 0

    def record(self, model, input_tokens, output_tokens):
        m = self.per_model[model]
        m["calls"] += 1
        m["input_tokens"] += input_tokens
        m["output_tokens"] += output_tokens

    @property
    def calls(self):
        return sum(m["calls"] for m in self.per_model.values())

    def summary(self):
        out = {"requests": self.requests, "models": {}, "total_calls": self.calls}
        total_in = total_out = cost = 0.0
        for model, m in self.per_model.items():
            pin, pout = PRICES.get(model, (0.0, 0.0))
            c = m["input_tokens"] / 1e6 * pin + m["output_tokens"] / 1e6 * pout
            out["models"][model] = dict(m, estimated_cost_usd=round(c, 6))
            total_in += m["input_tokens"]
            total_out += m["output_tokens"]
            cost += c
        out.update(input_tokens=int(total_in), output_tokens=int(total_out), estimated_cost_usd=round(cost, 6))
        return out
