"""How much of output.csv rests on the activity-timing fallback?

Audits every request (samples + evaluation set in one batch, as main.py does), then
reports which output steps name a record linked *only* by deletion-worker timing, with
the confidence tier assigned in ``Auditor.attribute``. On the public samples the tiers are
checked against sample_output.csv to show how far each tier can be trusted.

Usage: python3 code/evaluation/attribution_audit.py [--list]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ghost_audit.common import DATASET_DIR  # noqa: E402
from ghost_audit.pipeline import build_auditor, load_requests  # noqa: E402

TIERS = ("high", "medium", "low")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="print every low-confidence case")
    args = ap.parse_args(argv)

    tests = load_requests(DATASET_DIR / "deletion_requests.csv")
    samples = load_requests(DATASET_DIR / "sample_deletion_requests.csv")
    works = {w.case["case_id"]: w for w in build_auditor().audit_batch(tests + samples)}
    gold = {}
    with open(DATASET_DIR / "sample_output.csv", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            gold[r["case_id"]] = {s["resource_id"] for s in json.loads(r["steps_json"])}

    def dependent(case_ids):
        rows = defaultdict(list)   # case -> [(resource, system, tier)]
        for cid in case_ids:
            w = works[cid]
            named = {s["resource_id"] for s in w.steps}
            for v in w.views.values():
                if v.attributed and v.resource_id in named:
                    rows[cid].append((v.resource_id, v.system, v.confidence))
        return rows

    test_ids = [c["case_id"] for c in tests]
    dep = dependent(test_ids)
    deletion = [c for c in test_ids if works[c].status != "NOT_APPLICABLE"]
    per_tier = Counter(t for recs in dep.values() for _, _, t in recs)
    per_system = Counter((s, t) for recs in dep.values() for _, s, t in recs)
    case_tier = {c: min((t for _, _, t in recs), key=lambda t: -TIERS.index(t)) for c, recs in dep.items()}

    print(f"Evaluation set: {len(test_ids)} rows, {len(deletion)} deletion cases")
    print(f"Cases whose steps name a timing-attributed record: {len(dep)}")
    print("  by weakest tier in the case:", dict(Counter(case_tier.values())))
    print("  attributed records by tier:  ", dict(per_tier))
    for (system, tier), n in sorted(per_system.items()):
        print(f"    {system:20} {tier:6} {n}")
    # Could an attribution flip the status? Only if the attributed record is a violation.
    flips = [c for c, recs in dep.items()
             if any(s["resource_id"] == r and s["phase"] == "FINDING" for r, _, _ in recs for s in works[c].steps)]
    print(f"  cases where an attributed record drives a FINDING (status-relevant): {len(flips)} {flips}")

    sdep = dependent([c["case_id"] for c in samples])
    calib = defaultdict(lambda: [0, 0])
    for cid, recs in sdep.items():
        for rid, _, tier in recs:
            calib[tier][0] += rid in gold[cid]
            calib[tier][1] += 1
    print("Calibration on public samples (attributed record appears in the sample answer):")
    for tier in TIERS:
        ok, n = calib[tier]
        print(f"  {tier:6} {ok}/{n}" + (f" = {ok / n:.2f}" if n else ""))

    low = sorted((c for c, t in case_tier.items() if t == "low"), key=lambda c: int(c.split("_")[1]))
    print(f"Low-confidence cases ({len(low)}):")
    if args.list:
        for c in low:
            recs = ", ".join(f"{r} [{t}]" for r, _, t in dep[c])
            print(f"  {c:9} {works[c].status:10} reported={works[c].case['reported_status']:18} {recs}")
    else:
        print("  " + " ".join(low))


if __name__ == "__main__":
    main()
