"""Model-usage accounting and the generated usage report."""

from __future__ import annotations

from collections import defaultdict

from .vision import PRICES as VISION_PRICES

# USD per million tokens (input, output); list-price assumptions used only for cost estimates.
PRICES = {"claude-haiku-5-5": (0.10, 0.50), **VISION_PRICES}
PROVIDER = {"claude-haiku-5-5": "anthropic", **{m: "groq" for m in VISION_PRICES}}


class UsageTracker:
    def __init__(self):
        self.per_model = defaultdict(lambda: {"calls": 0, "input_tokens": 0, "output_tokens": 0, "purpose": set()})
        self.requests = 0
        self.vision_cache_hits = 0

    def record(self, model, input_tokens, output_tokens, purpose="intent"):
        m = self.per_model[model]
        m["calls"] += 1
        m["input_tokens"] += input_tokens
        m["output_tokens"] += output_tokens
        m["purpose"].add(purpose)

    @property
    def calls(self):
        return sum(m["calls"] for m in self.per_model.values())

    def summary(self):
        out = {"requests": self.requests, "models": {}, "total_calls": self.calls,
               "vision_cache_hits": self.vision_cache_hits}
        total_in = total_out = cost = 0.0
        for model, m in sorted(self.per_model.items()):
            pin, pout = PRICES.get(model, (0.0, 0.0))
            c = m["input_tokens"] / 1e6 * pin + m["output_tokens"] / 1e6 * pout
            out["models"][model] = {"provider": PROVIDER.get(model, "unknown"), "purpose": sorted(m["purpose"]),
                                    "calls": m["calls"], "input_tokens": m["input_tokens"],
                                    "output_tokens": m["output_tokens"], "price_per_m": [pin, pout],
                                    "estimated_cost_usd": round(c, 6)}
            total_in += m["input_tokens"]
            total_out += m["output_tokens"]
            cost += c
        out.update(input_tokens=int(total_in), output_tokens=int(total_out), estimated_cost_usd=round(cost, 6))
        return out


def render_report(s: dict, when: str) -> str:
    n = max(s["requests"], 1)
    total = s["input_tokens"] + s["output_tokens"]
    lines = [
        "# Usage Report — final full-dataset run",
        "",
        f"Generated {when} by `python3 code/main.py` (the run that wrote `output.csv`).",
        "",
        "Deterministic code decides every status and step. Models are optional and narrow: a Groq",
        "vision model transcribes screenshot evidence when `GROQ_API_KEY` is set (its text is added to",
        "local RapidOCR text, never replacing it), and `claude-haiku-5-5` classifies request texts the",
        "intent rules cannot decide when `--llm` and `ANTHROPIC_API_KEY` are set.",
        "",
        "## Per provider and model",
        "",
        "| Provider | Model | Purpose | Calls | Input tokens | Output tokens | Price in/out per 1M | Cost (USD) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for model, m in s["models"].items():
        lines.append(f"| {m['provider']} | `{model}` | {', '.join(m['purpose'])} | {m['calls']} | {m['input_tokens']} | "
                     f"{m['output_tokens']} | ${m['price_per_m'][0]} / ${m['price_per_m'][1]} | "
                     f"{m['estimated_cost_usd']:.6f} |")
    if not s["models"]:
        lines.append("| — | — | — | 0 | 0 | 0 | — | 0.000000 |")
    lines.append(f"| **Overall** | | | **{s['total_calls']}** | **{s['input_tokens']}** | **{s['output_tokens']}** | "
                 f"| **{s['estimated_cost_usd']:.6f}** |")
    lines += [
        "",
        "## Totals",
        "",
        "| Item | Value |",
        "|---|---|",
        f"| Requests processed | {s['requests']} |",
        f"| Model calls | {s['total_calls']} |",
        f"| Screenshot transcriptions served from cache (no tokens) | {s['vision_cache_hits']} |",
        f"| Input / output tokens | {s['input_tokens']} / {s['output_tokens']} |",
        f"| Total tokens | {total} |",
        f"| Average tokens per request | {total / n:.1f} |",
        f"| Estimated total cost | ${s['estimated_cost_usd']:.6f} |",
        f"| Estimated cost per request | ${s['estimated_cost_usd'] / n:.8f} |",
        "",
        "Prices are list-price assumptions (`ghost_audit/usage.py`, `ghost_audit/vision.py`).",
        "A re-run with a warm vision cache makes no calls; delete `code/cache/vision/` to re-measure.",
        "",
    ]
    return "\n".join(lines)
