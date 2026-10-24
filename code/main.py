"""Ghost Data Still Exists - deletion-audit agent.

Usage (from the repository root):
    python3 code/main.py                       # audit dataset/deletion_requests.csv -> ./output.csv
    python3 code/main.py --input dataset/sample_deletion_requests.csv --output /tmp/sample_pred.csv
    python3 code/main.py --trace traces.json   # also dump per-case reasoning (linked resources, states)
    python3 code/main.py --llm                 # allow an LLM fallback for undecidable request texts

The decision pipeline is deterministic. Two optional model layers, both keyed from the
environment: GROQ_API_KEY adds a Groq vision transcription of each screenshot to local OCR
text; --llm with ANTHROPIC_API_KEY sends request texts the intent rules cannot decide to
claude-haiku-5-5. A full run rewrites code/evaluation/usage_report.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ghost_audit.common import DATASET_DIR, REPO_ROOT  # noqa: E402
from ghost_audit.intent import classify, classify_with_llm  # noqa: E402
from ghost_audit.pipeline import build_auditor, load_requests, run_cases, write_output  # noqa: E402
from ghost_audit import vision  # noqa: E402
from ghost_audit.usage import UsageTracker, render_report  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", default=str(DATASET_DIR / "deletion_requests.csv"))
    ap.add_argument("--output", default=str(REPO_ROOT / "output.csv"))
    ap.add_argument("--trace", default=None, help="optional JSON file with per-case reasoning")
    ap.add_argument("--llm", action="store_true", help="enable LLM fallback for undecided request intent")
    ap.add_argument("--usage", default=None, help="optional JSON file to write model-usage counters")
    ap.add_argument("--usage-report", default=None,
                    help="where to write the usage report (default: code/evaluation/usage_report.md on a full run)")
    args = ap.parse_args(argv)

    t0 = time.time()
    usage = UsageTracker()
    llm = None
    if args.llm:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("--llm requested but ANTHROPIC_API_KEY is not set; continuing rules-only", file=sys.stderr)
        else:
            import anthropic
            client = anthropic.Anthropic()

            def llm(text):
                decided, _ = classify(text)
                if decided is not None:
                    return decided
                return classify_with_llm(text, client, usage)[0]

    auditor = build_auditor()
    cases = load_requests(args.input)
    context = load_requests(DATASET_DIR / "deletion_requests.csv") + load_requests(
        DATASET_DIR / "sample_deletion_requests.csv")
    results = run_cases(auditor, cases, context_cases=context, llm=llm)
    write_output(results, args.output)
    if args.trace:
        Path(args.trace).write_text(json.dumps({k: {"status": v[0], "trace": v[2]} for k, v in results.items()},
                                               indent=1, default=str), encoding="utf-8")
    usage.requests = len(cases)
    for call in vision.CALLS:
        usage.record(call["model"], call["input_tokens"], call["output_tokens"], purpose="screenshot transcription")
    usage.vision_cache_hits = vision.CACHE_HITS
    report = args.usage_report
    if report is None and Path(args.input).resolve() == (DATASET_DIR / "deletion_requests.csv").resolve():
        report = str(REPO_ROOT / "code" / "evaluation" / "usage_report.md")
    if report:
        Path(report).write_text(render_report(usage.summary(), time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())),
                                encoding="utf-8")
    if args.usage:
        Path(args.usage).write_text(json.dumps(usage.summary(), indent=1), encoding="utf-8")
    counts = {}
    for status, _, _ in results.values():
        counts[status] = counts.get(status, 0) + 1
    print(f"wrote {len(results)} rows to {args.output} in {time.time() - t0:.1f}s; status counts {counts}; "
          f"model calls {usage.calls} (vision {'on' if vision.enabled() else 'off'}, "
          f"{vision.CACHE_HITS} cached transcriptions)")


if __name__ == "__main__":
    main()
