# Usage Report — final full-dataset run

Command: `python3 code/main.py` (produces `output.csv`, 518 rows, ~0.6 s wall clock after
the OCR cache exists; ~20 s on a cold run that OCRs the 11 screenshots locally).

## Summary

| Item | Value |
|---|---|
| Requests processed | 518 |
| Model providers | none called (Anthropic configured as optional fallback) |
| Model calls | **0** |
| Input tokens | 0 |
| Output tokens | 0 |
| Total tokens | 0 |
| Average tokens per request | 0 |
| Estimated total cost | **$0.00** |
| Estimated cost per request | $0.00 |

## Per model

| Provider | Model | Calls | Input tokens | Output tokens | Cost |
|---|---|---|---|---|---|
| Anthropic | `claude-haiku-5-5` (optional `--llm` intent fallback) | 0 | 0 | 0 | $0.00 |
| local | RapidOCR (ONNX, on-device) for screenshots | 11 images | n/a | n/a | $0.00 |
| **Overall** | | **0 LLM calls** | **0** | **0** | **$0.00** |

## Why zero model calls

Every decision in this task is grounded in structured evidence: identity resolution, record
linking, point-in-time state, policy and step ordering are deterministic code, which keeps
the output reproducible and auditable. The only step where language understanding matters is
"does this request authorise deletion?". Multilingual rules (EN/ES/PT/FR/JA/KO, with
negation handling) decide all 550 request texts in the dataset, so the LLM fallback in
`ghost_audit/intent.py` is never reached.

If `--llm` is passed with `ANTHROPIC_API_KEY` set, undecided texts go to `claude-haiku-5-5`
(thinking disabled, `max_tokens=16`, one-word answer). Expected cost if it were used:
~150 input + ~2 output tokens per call at $0.10 / $0.50 per MTok ≈ **$0.000016 per call**;
even calling it for all 518 requests would cost ≈ $0.008. Counters are written with
`python3 code/main.py --usage usage.json`.
