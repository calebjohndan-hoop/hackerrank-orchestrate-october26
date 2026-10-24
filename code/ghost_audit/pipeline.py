"""End-to-end runner shared by main.py and the evaluation harness."""

from __future__ import annotations

import csv
import json

from .activity import ActivityStream
from .auditor import Auditor
from .common import DATASET_DIR
from .evidence import EvidenceStore
from .identity import IdentityIndex
from .policy import Policy


def load_requests(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def build_auditor(dataset_dir=DATASET_DIR):
    store = EvidenceStore(dataset_dir)
    acts = ActivityStream(dataset_dir)
    store.add_alias_observations([(rid, alias) for rid, links in acts.catalog.items() for _, alias, _, _ in links])
    return Auditor(store, IdentityIndex(dataset_dir), acts, Policy(dataset_dir))


def run_cases(auditor, cases, context_cases=None, llm=None):
    """Audit ``cases``. ``context_cases`` (all known requests) are only used to see which
    other requests share a workflow report time, for the activity-timing fallback."""
    wanted = {c["case_id"] for c in cases}
    batch = {c["case_id"]: c for c in (context_cases or [])}
    batch.update({c["case_id"]: c for c in cases})
    works = auditor.audit_batch(list(batch.values()), llm_decide=llm)
    return {w.case["case_id"]: (w.status, w.steps, w.trace) for w in works if w.case["case_id"] in wanted}


def case_sort_key(case_id):
    tail = case_id.split("_")[-1]
    return (0, int(tail)) if tail.isdigit() else (1, case_id)


def write_output(results, path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["case_id", "deletion_status", "steps_json"])
        for cid in sorted(results, key=case_sort_key):
            status, steps, _ = results[cid]
            w.writerow([cid, status, json.dumps(steps, ensure_ascii=False, separators=(",", ":"))])
