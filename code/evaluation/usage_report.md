# Usage Report — final full-dataset run

Generated 2026-10-24 18:08 UTC by `python3 code/main.py` (the run that wrote `output.csv`).

Deterministic code decides every status and step. Models are optional and narrow: a Groq
vision model transcribes screenshot evidence when `GROQ_API_KEY` is set (its text is added to
local RapidOCR text, never replacing it), and `claude-haiku-5-5` classifies request texts the
intent rules cannot decide when `--llm` and `ANTHROPIC_API_KEY` are set.

## Per provider and model

| Provider | Model | Purpose | Calls | Input tokens | Output tokens | Price in/out per 1M | Cost (USD) |
|---|---|---|---|---|---|---|---|
| — | — | — | 0 | 0 | 0 | — | 0.000000 |
| **Overall** | | | **0** | **0** | **0** | | **0.000000** |

## Totals

| Item | Value |
|---|---|
| Requests processed | 518 |
| Model calls | 0 |
| Screenshot transcriptions served from cache (no tokens) | 0 |
| Input / output tokens | 0 / 0 |
| Total tokens | 0 |
| Average tokens per request | 0.0 |
| Estimated total cost | $0.000000 |
| Estimated cost per request | $0.00000000 |

Prices are list-price assumptions (`ghost_audit/usage.py`, `ghost_audit/vision.py`).
A re-run with a warm vision cache makes no calls; delete `code/cache/vision/` to re-measure.
