"""Evaluation harness.

Runs the auditor on labelled cases and reports:
  * case-level deletion_status accuracy
  * step-level precision / recall / F1 (a step matches when every field matches)
  * field-level accuracy over position-aligned steps (phase, system_id, resource_id,
    operation, state, evidence_id)
  * sequence accuracy (the whole steps_json array is identical)
  * structural validity (consecutive step numbers, phase order, allowed op/state pairs,
    empty evidence on proposed work, no evidence captured after audit_at)

Usage:
    python3 code/evaluation/main.py                 # public samples vs dataset/sample_output.csv
    python3 code/evaluation/main.py --custom        # also the hand-written cases in custom_cases.py
    python3 code/evaluation/main.py --verbose       # print per-case diffs
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from ghost_audit.common import DATASET_DIR, OP_PHASE, OP_RANK, OP_STATE, parse_ts  # noqa: E402
from ghost_audit.pipeline import build_auditor, load_requests, run_cases  # noqa: E402

FIELDS = ["phase", "system_id", "resource_id", "operation", "state", "evidence_id"]


def step_key(s):
    return tuple(s[f] for f in FIELDS)


def validate(steps, audit_at, manifest, status):
    errors = []
    phases = ["FINDING", "REMEDIATION", "VERIFICATION"]
    last_phase = 0
    for i, s in enumerate(steps, 1):
        if s["step_no"] != i:
            errors.append(f"step_no {s['step_no']} != {i}")
        if OP_STATE.get(s["operation"]) != s["state"] or OP_PHASE.get(s["operation"]) != s["phase"]:
            errors.append(f"bad op/state/phase {s['operation']}/{s['state']}/{s['phase']}")
        p = phases.index(s["phase"])
        if p < last_phase:
            errors.append("phase order")
        last_phase = p
        proposed = status == "INCOMPLETE" and s["phase"] in ("REMEDIATION", "VERIFICATION")
        if proposed and s["evidence_id"]:
            errors.append("proposed step cites evidence")
        if not proposed and not s["evidence_id"]:
            errors.append("finding/verification without evidence")
        ev = s["evidence_id"]
        if ev in manifest and manifest[ev] > audit_at:
            errors.append(f"evidence {ev} captured after audit_at")
    if status == "NOT_APPLICABLE" and steps:
        errors.append("NOT_APPLICABLE with steps")
    order = [(OP_RANK[s["operation"]], s["system_id"], s["resource_id"]) for s in steps]
    if order != sorted(order):
        errors.append("steps not in operation/system/resource order")
    return errors


def evaluate(pred, gold, cases, manifest, verbose=False, title="samples"):
    n = len(gold)
    status_ok = seq_ok = 0
    tp = fp = fn = 0
    field_hits = {f: 0 for f in FIELDS}
    field_total = 0
    invalid = 0
    failures = []
    for cid, (gstatus, gsteps) in gold.items():
        pstatus, psteps, _ = pred[cid]
        status_ok += pstatus == gstatus
        g = [step_key(s) for s in gsteps]
        p = [step_key(s) for s in psteps]
        seq_ok += g == p
        gs, ps = set(g), set(p)
        tp += len(gs & ps)
        fp += len(ps - gs)
        fn += len(gs - ps)
        for i in range(max(len(g), len(p))):
            field_total += 1
            if i < len(g) and i < len(p):
                for j, f in enumerate(FIELDS):
                    field_hits[f] += g[i][j] == p[i][j]
        errs = validate(psteps, parse_ts(cases[cid]["audit_at"]), manifest, pstatus)
        invalid += bool(errs)
        if g != p or pstatus != gstatus or errs:
            failures.append(cid)
            if verbose:
                print(f"--- {cid}: gold {gstatus} / pred {pstatus} {errs if errs else ''}")
                for s in sorted(gs - ps, key=lambda x: OP_RANK[x[3]]):
                    print("   missing ", s)
                for s in sorted(ps - gs, key=lambda x: OP_RANK[x[3]]):
                    print("   extra   ", s)
    prec = tp / (tp + fp) if tp + fp else 1.0
    rec = tp / (tp + fn) if tp + fn else 1.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    report = {
        "set": title,
        "cases": n,
        "status_accuracy": status_ok / n,
        "step_precision": prec,
        "step_recall": rec,
        "step_f1": f1,
        "field_accuracy": {f: field_hits[f] / field_total if field_total else 1.0 for f in FIELDS},
        "sequence_accuracy": seq_ok / n,
        "structurally_invalid_cases": invalid,
        "failures": failures,
    }
    return report


def load_gold(path):
    gold = {}
    with open(path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            gold[r["case_id"]] = (r["deletion_status"], json.loads(r["steps_json"]))
    return gold


def print_report(rep):
    print(f"\n== {rep['set']} ({rep['cases']} cases)")
    print(f"status accuracy     {rep['status_accuracy']:.3f}")
    print(f"step P / R / F1     {rep['step_precision']:.3f} / {rep['step_recall']:.3f} / {rep['step_f1']:.3f}")
    print("field accuracy      " + ", ".join(f"{k}={v:.3f}" for k, v in rep["field_accuracy"].items()))
    print(f"sequence accuracy   {rep['sequence_accuracy']:.3f}")
    print(f"invalid structure   {rep['structurally_invalid_cases']}")
    print(f"failing cases       {' '.join(rep['failures']) or '-'}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--custom", action="store_true", help="also run custom_cases.py")
    ap.add_argument("--json", default=None, help="write metrics JSON here")
    args = ap.parse_args(argv)

    auditor = build_auditor()
    manifest = {}
    with open(DATASET_DIR / "evidence_files.csv", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            manifest[r["file_id"]] = parse_ts(r["captured_at"])
    context = load_requests(DATASET_DIR / "deletion_requests.csv") + load_requests(
        DATASET_DIR / "sample_deletion_requests.csv")
    samples = load_requests(DATASET_DIR / "sample_deletion_requests.csv")
    pred = run_cases(auditor, samples, context_cases=context)
    gold = load_gold(DATASET_DIR / "sample_output.csv")
    reports = [evaluate(pred, gold, {c["case_id"]: c for c in samples}, manifest, args.verbose, "public samples")]
    if args.custom:
        from custom_cases import CUSTOM_CASES
        cases = [c["request"] for c in CUSTOM_CASES]
        cpred = run_cases(auditor, cases, context_cases=context + cases)
        cgold = {c["request"]["case_id"]: (c["expected_status"], c["expected_steps"]) for c in CUSTOM_CASES}
        reports.append(evaluate(cpred, cgold, {c["case_id"]: c for c in cases}, manifest, args.verbose,
                                "custom cases"))
    for rep in reports:
        print_report(rep)
    if args.json:
        Path(args.json).write_text(json.dumps(reports, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
