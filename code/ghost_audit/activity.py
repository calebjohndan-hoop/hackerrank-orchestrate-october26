"""System-activity stream: parsed once, used as claims plus a few grounded links.

Activity rows are *claims* (a SUCCESS result does not prove a state), but three
kinds of rows carry facts we can use:

* ``LINK_RESOURCE_IDENTITY`` rows from the data catalog tie a resource (and an
  alias such as a file name or statement number) to an email and user_id.
* ``SYNC_RECORD`` / ``RESTORE_BACKUP`` rows that "repopulated downstream
  resource X" tie a data flow or backup to the record it recreated.
* deletion-worker rows are emitted shortly before the workflow reported its
  status, which lets us attribute identifier-free records (an account lookup
  that returned 404, an analytics row with every identifier nulled) to the case
  whose ``reported_at`` they precede.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass

from .common import DATASET_DIR, EMAIL_RE, RESOURCE_RE, USER_RE, norm_email, parse_ts


@dataclass
class Activity:
    activity_id: str
    ts: object
    event_type: str
    source: str
    target: str
    resource_id: str
    result: str
    details: str


class ActivityStream:
    def __init__(self, dataset_dir=DATASET_DIR):
        self.rows: list[Activity] = []
        with open(dataset_dir / "system_activity.csv", newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                self.rows.append(Activity(r["activity_id"], parse_ts(r["event_time"]), r["event_type"],
                                          r["source_system"], r["target_system"], r["resource_id"],
                                          r["reported_result"], r["details"]))
        self.by_resource = defaultdict(list)
        for a in self.rows:
            self.by_resource[a.resource_id].append(a)
        # catalog links: resource -> (activity, alias, tokens)
        self.catalog = defaultdict(list)
        # co-ownership links between resources (flow/backup -> recreated record)
        self.pairs = defaultdict(list)
        self.non_deletion_accounts = {}
        for a in self.rows:
            if a.event_type == "LINK_RESOURCE_IDENTITY":
                m = re.search(r"\(([^)]+)\)", a.details)
                alias = m.group(1) if m else a.resource_id
                tokens = {("email", norm_email(e)) for e in EMAIL_RE.findall(a.details)}
                tokens |= {("user", u) for u in USER_RE.findall(a.details)}
                exclusive = "belongs only to" in a.details or "no other subject" in a.details
                self.catalog[a.resource_id].append((a, alias, tokens, exclusive))
            elif "repopulated downstream resource" in a.details:
                m = RESOURCE_RE.findall(a.details)
                for other in m:
                    if other != a.resource_id:
                        self.pairs[a.resource_id].append((other, a))
                        self.pairs[other].append((a.resource_id, a))
            elif a.event_type == "CLASSIFY_REQUEST":
                self.non_deletion_accounts[a.resource_id] = a

    def worker_events(self, start, end):
        return [a for a in self.rows if a.source == "deletion_worker" and start <= a.ts <= end]
