"""Case auditor: identity -> linked resources -> state at audit_at -> policy -> steps.

Cases are audited as a batch in two passes:

1. Content pass - every resource that evidence ties to one of the subject's current or
   historical identifiers (email, phone, email hash, user_id), plus records that
   reference or were recreated from those resources, plus data-catalog links.
2. Attribution pass - identifier-free records (an account lookup that only returned
   404, an analytics row whose identifiers were all nulled) cannot be linked by
   content. For those we use the deletion worker's own actions, which run shortly
   before the workflow reports its status: an unowned account-delete / anonymise
   action in that window is attributed to the case(s) with that ``reported_at`` that
   still lack such a record. This is a weaker link and is recorded as such in the trace.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import timedelta

from .activity import ActivityStream
from .common import OP_PHASE, OP_RANK, OP_STATE, RESOURCE_RE, parse_ts, system_for
from .evidence import EvidenceStore, Observation
from .identity import IdentityIndex
from .intent import classify
from .policy import Policy

LINK_LOOKBACK = timedelta(days=45)     # older sightings belong to earlier, separate requests
LINK_LOOKAHEAD = timedelta(days=14)    # ownership of an immutable resource id may be learned later
CATALOG_LOOKBACK = timedelta(days=7)   # catalog links are made while a request is being processed
WORKER_WINDOW = timedelta(minutes=95)  # deletion-worker actions precede the workflow report
WORKER_GRACE = timedelta(minutes=10)   # ...or land just after it
# Median lead of the worker action before the workflow report, measured on actions that
# fall in exactly one report window (account delete ~57 min, anonymise ~28 min).
TYPICAL_LEAD = {"DELETE_RECORD": timedelta(minutes=57), "ANONYMIZE_RECORD": timedelta(minutes=28)}
# The workflow's reported status mirrors the worker's result for the primary profile.
STATUS_RESULT = {"COMPLETE": "SUCCESS", "PARTIALLY_COMPLETE": "PARTIAL", "NOT_STARTED": "FAILED"}


@dataclass
class ResourceView:
    resource_id: str
    system: str
    link_reasons: list = field(default_factory=list)
    state: Observation | None = None   # latest observation describing the resource at audit_at
    citeable: bool = False             # state comes from a file captured at/before audit_at
    category: str = ""
    attributed: bool = False           # linked only through activity timing
    confidence: str = ""               # for attributed records: high / medium / low


@dataclass
class CaseWork:
    case: dict
    user: object = None
    audit_at: object = None
    views: dict = field(default_factory=dict)
    status: str = ""
    steps: list = field(default_factory=list)
    trace: dict = field(default_factory=dict)


def case_num(case):
    tail = case["case_id"].split("_")[-1]
    return int(tail) if tail.isdigit() else 10 ** 9


class Auditor:
    def __init__(self, store: EvidenceStore, ids: IdentityIndex, acts: ActivityStream, policy: Policy):
        self.store, self.ids, self.acts, self.policy = store, ids, acts, policy
        self.obs_by_token = defaultdict(list)
        for ob in store.observations:
            for tok in ob.tokens:
                self.obs_by_token[tok].append(ob)
        # Users each resource is tied to by content; used to keep other people's
        # records out of the timing attribution.
        self.content_owners = defaultdict(set)
        for ob in store.observations:
            if ob.kind.startswith(("image:", "document:")):
                continue
            for u in ids.owners_of(ob.tokens):
                self.content_owners[ob.resource_id].add(u)
        for rid, links in acts.catalog.items():
            for _, _, toks, _ in links:
                for u in ids.owners_of(toks):
                    self.content_owners[rid].add(u)
        self.file_resources = defaultdict(set)
        self.file_users = defaultdict(set)
        for ob in store.observations:
            self.file_resources[ob.file_id].add(ob.resource_id)
            self.file_users[ob.file_id] |= ids.owners_of(ob.tokens)
        self.collection_of = {}
        for ob in store.observations:
            if ob.kind.startswith(("bundle:", "sql")):
                self.collection_of.setdefault(ob.resource_id, set()).add(ob.kind.split(":", 1)[1].split("/")[-1])

    # ------------------------------------------------------------------ linking

    def linked_resources(self, user, case, audit_at):
        requested = parse_ts(case["requested_at"])
        earliest = requested - LINK_LOOKBACK
        views: dict[str, ResourceView] = {}
        tokens = user.tokens()

        def add(rid, reason):
            v = views.get(rid)
            if v is None:
                v = views[rid] = ResourceView(rid, system_for(rid))
            v.link_reasons.append(reason)

        for tok in tokens:
            for ob in self.obs_by_token.get(tok, []):
                if ob.ts > audit_at + LINK_LOOKAHEAD or ob.ts < earliest:
                    continue
                owners = self.ids.owners_of(ob.tokens)
                if ob.kind.startswith(("image:", "document:")):
                    # A page can show several records and people. Trust it for ownership only
                    # when the catalog ties the record to this person, or the page is about
                    # a single record of a single known person.
                    page_ids = set(RESOURCE_RE.findall(ob.detail.get("text", "")))
                    catalog_ok = any(toks & tokens and requested - CATALOG_LOOKBACK <= act.ts <= audit_at + timedelta(days=1)
                                     for act, _, toks, _ in self.acts.catalog.get(ob.resource_id, []))
                    if not catalog_ok and not (owners == {user.user_id} and len(page_ids) <= 1):
                        continue
                if len(owners) > 1 and ob.kind.startswith("log:"):
                    # A trace mixing several users' identifiers is only a link when the
                    # resource line itself names this user.
                    own = self.ids.owners_of(_line_tokens(ob))
                    if own != {user.user_id}:
                        continue
                add(ob.resource_id, ("content", ob.file_id, tok))
        for rid, links in self.acts.catalog.items():
            for act, alias, toks, exclusive in links:
                if toks & tokens and requested - CATALOG_LOOKBACK <= act.ts <= audit_at + timedelta(days=1):
                    add(rid, ("catalog", act.activity_id, alias))
        # Records referencing a linked record (search doc -> source account), and
        # flows / backups that recreated a linked record.
        for rid in list(views):
            for ob in self.store.by_resource.get(rid, []):
                src = ob.detail.get("source_resource_id") if isinstance(ob.detail, dict) else None
                if src and src not in views and ob.ts <= audit_at:
                    add(src, ("reference", ob.file_id, rid))
            for other, act in self.acts.pairs.get(rid, []):
                if other not in views and earliest <= act.ts <= audit_at:
                    add(other, ("activity_pair", act.activity_id, rid))
        return views

    # ------------------------------------------------------------------ state

    def state_at(self, rid, audit_at, user):
        """State of ``rid`` at ``audit_at``.

        The state is the latest observation describing a time at/before audit_at
        (copies inside one file - primary and replicas - are merged so replication
        drift keeps a record 'present'). Explicit observations outrank coverage-based
        absence. The cited evidence must come from a file captured at/before audit_at
        and agree with that state; if none does, the state is used for planning only."""
        obs = [o for o in self.store.by_resource.get(rid, []) if o.ts <= audit_at
               and (o.exists is not None or o.active is not None)]
        all_absences = self._coverage_absences(rid, audit_at, user)
        explicit_latest = max((o.ts for o in obs), default=None)
        absences = [a for a in all_absences if explicit_latest is None or a.ts > explicit_latest]
        cands = obs + absences
        if not cands:
            return None, False
        state = self._merge(rid, cands)
        citeable = [o for o in obs + all_absences if o.captured <= audit_at]
        agreeing = [o for o in citeable if _agrees(o, state)]
        if agreeing:
            best_ts = max(o.ts for o in agreeing)
            state.file_id = sorted(o.file_id for o in agreeing if o.ts == best_ts)[0]
            return state, True
        return state, False

    def _coverage_absences(self, rid, audit_at, user):
        out = []
        system = system_for(rid)
        cols = self.collection_of.get(rid, set())
        for file_id, ts, cov_system, collection in self.store.coverage:
            if cov_system != system or ts is None or ts > audit_at:
                continue
            ef = self.store.files[file_id]
            if ef.captured > audit_at or rid in self.file_resources[file_id]:
                continue
            if cols and collection and collection not in cols and not collection.startswith("customer_data_"):
                continue
            if user.user_id not in self.file_users[file_id]:
                continue  # captures are batch-scoped: they only speak for the subjects they mention
            out.append(Observation(file_id=file_id, captured=ef.captured, ts=ts, resource_id=rid, system=system,
                                   exists=False, pii=False, kind="coverage_absent"))
        return out

    def _merge(self, rid, cands):
        latest_ts = max(o.ts for o in cands)
        latest = [o for o in cands if o.ts == latest_ts]
        explicit = [o for o in latest if o.kind != "coverage_absent"]
        pool = explicit or latest
        best = max(pool, key=lambda o: (bool(o.exists), bool(o.pii), bool(o.active), o.file_id))
        merged = Observation(**{**best.__dict__})
        known = [o.exists for o in pool if o.exists is not None]
        merged.exists = any(known) if known else None
        merged.pii = any(bool(o.pii) for o in pool)
        acts = [o.active for o in pool if o.active is not None]
        merged.active = any(acts) if acts else None
        rts = [o.record_type for o in self.store.by_resource.get(rid, []) if o.record_type]
        merged.record_type = best.record_type or (rts[-1] if rts else None)
        return merged

    def refresh(self, work: CaseWork):
        for v in work.views.values():
            v.state, v.citeable = self.state_at(v.resource_id, work.audit_at, work.user)
        work.views = self.policy.select(work.views)
        for v in work.views.values():
            v.category = self.policy.category(v, self.store)

    # ------------------------------------------------------------------ attribution

    def _admissible_sighting(self, rid, work):
        """An explicit observation of rid in a file the case could cite."""
        req = parse_ts(work.case["requested_at"])
        return any(o.captured <= work.audit_at and o.ts >= req for o in self.store.by_resource.get(rid, []))

    def attribute(self, works):
        groups = defaultdict(list)
        for w in works:
            if w.user is not None:
                groups[w.case["reported_at"]].append(w)
        # Windows of neighbouring report times overlap; give each worker action to exactly
        # one report time - the one whose lead best matches that action's usual lead - so a
        # record can never be attributed to two cases.
        report_times = sorted({parse_ts(r) for r in groups})
        owner = {}
        for a in self.acts.worker_events(report_times[0] - WORKER_WINDOW, report_times[-1] + WORKER_GRACE) \
                if report_times else []:
            fits = [r for r in report_times if r - WORKER_WINDOW <= a.ts <= r + WORKER_GRACE]
            if fits:
                lead = TYPICAL_LEAD.get(a.event_type, timedelta(0))
                owner[a.activity_id] = min(fits, key=lambda r: (abs((r - a.ts) - lead), r))
        for reported_at, group in groups.items():
            group.sort(key=lambda w: case_num(w.case))
            reported = parse_ts(reported_at)
            events = [a for a in self.acts.worker_events(reported - WORKER_WINDOW, reported + WORKER_GRACE)
                      if owner.get(a.activity_id) == reported]
            for system, etype, only_complete in (("account_service", "DELETE_RECORD", False),
                                                  ("analytics_platform", "ANONYMIZE_RECORD", True)):
                needy = [w for w in group if not any(v.system == system for v in w.views.values())]
                if only_complete:
                    # Every analytics row tied to a person by content is already linked;
                    # an anonymised one is only proof of a finished deletion.
                    needy = [w for w in needy if w.status == "COMPLETE"]
                if not needy:
                    continue
                cands = [a for a in events if a.target == system and a.event_type == etype
                         and not self.content_owners.get(a.resource_id)
                         and (a.result == "SUCCESS" or not only_complete)]
                cands.sort(key=lambda a: a.ts)
                taken = set()
                for i, w in enumerate(needy):
                    want = STATUS_RESULT.get(w.case["reported_status"]) if system == "account_service" else None

                    def rank(a):
                        return (want is not None and a.result != want, not self._admissible_sighting(a.resource_id, w))

                    ordered = sorted((a for a in cands if a.resource_id not in taken), key=lambda a: (*rank(a), a.ts))
                    if not ordered:
                        break
                    a = ordered[0]
                    taken.add(a.resource_id)
                    v = w.views.setdefault(a.resource_id, ResourceView(a.resource_id, system))
                    v.link_reasons.append(("worker_timing", a.activity_id, reported_at))
                    v.attributed = True
                    v.confidence = _attribution_confidence(a, ordered, rank, want, competitors=len(needy) - i)

    # ------------------------------------------------------------------ planning

    def plan(self, work: CaseWork):
        decisions = [self.policy.decide(v) for v in work.views.values()]
        decisions = [d for d in decisions if d]
        incomplete = any(d["violation"] for d in decisions)
        steps = []
        if incomplete:
            for d in decisions:
                if d["violation"]:
                    steps.append((d["finding"], d["system"], d["resource_id"], d["evidence"]))
                    steps.append((d["remediation"], d["system"], d["resource_id"], ""))
            for d in decisions:
                steps.append((d["verify"], d["system"], d["resource_id"], ""))
        else:
            for d in decisions:
                if d["evidence"]:
                    steps.append((d["verify"], d["system"], d["resource_id"], d["evidence"]))
        steps.sort(key=lambda s: (OP_RANK[s[0]], s[1], s[2]))
        work.status = "INCOMPLETE" if incomplete else "COMPLETE"
        work.steps = [{"step_no": i, "phase": OP_PHASE[op], "system_id": system, "resource_id": rid,
                       "operation": op, "state": OP_STATE[op], "evidence_id": ev}
                      for i, (op, system, rid, ev) in enumerate(steps, 1)]
        work.trace["resources"] = {
            v.resource_id: {"links": v.link_reasons[:4], "category": v.category, "citeable": v.citeable,
                            "attribution_confidence": v.confidence or None, "state": _state_dict(v.state)}
            for v in work.views.values()}

    def audit_batch(self, cases, llm_decide=None):
        works = []
        for case in cases:
            w = CaseWork(case=case)
            ok, reason = classify(case["request_text"])
            if ok is None and llm_decide is not None:
                ok, reason = llm_decide(case["request_text"]), "llm fallback"
            w.trace["intent"] = reason
            if not ok:
                w.status = "NOT_APPLICABLE"
                works.append(w)
                continue
            w.user = self.ids.resolve_email(case["email"])
            if w.user is None:
                w.status, w.trace["error"] = "NOT_APPLICABLE", "unresolved identity"
                works.append(w)
                continue
            w.trace["user_id"] = w.user.user_id
            w.audit_at = parse_ts(case["audit_at"])
            w.views = self.linked_resources(w.user, case, w.audit_at)
            self.refresh(w)
            self.plan(w)
            works.append(w)
        self.attribute(works)
        for w in works:
            if w.user is not None:
                self.refresh(w)
                self.plan(w)
        return works


def _attribution_confidence(chosen, ordered, rank, want, competitors):
    """How much the evidence, rather than ordering, decided a timing attribution.

    high   - the only candidate left and the only case still waiting for one;
    medium - the workflow-status match or a citeable sighting separates it from the rest;
    low    - a tie broken by event time / case order, or the worker result contradicts
             the workflow's reported status."""
    if want is not None and chosen.result != want:
        return "low"
    if len(ordered) == 1:
        # Uncontested record; contested only if other cases were still waiting for one.
        return "high" if competitors == 1 else "low"
    if rank(chosen) < rank(ordered[1]):
        return "medium"
    return "low"


PII_SYSTEMS = {"analytics_platform", "support_platform", "identity_service", "email_delivery_service"}


def _agrees(o, state):
    """Does observation ``o`` support the decision-relevant parts of ``state``?"""
    if bool(o.exists) != bool(state.exists) and o.exists is not None:
        return False
    if o.exists is None and o.active is None:
        return False
    if state.active is not None and o.active != state.active:
        return False
    if state.system in PII_SYSTEMS and state.exists and o.pii is not None and bool(o.pii) != bool(state.pii):
        return False
    return True


def _line_tokens(ob):
    from .evidence import value_tokens
    out = set()
    for k, v in ob.detail.items():
        if k in ("lookup_key", "subject_key", "match_key", "scan_key", "subject_ref", "correlation_key",
                 "matched_value", "email", "customer_email", "customer_reference", "requester_email",
                 "owner_email", "subject_reference"):
            out |= value_tokens(v)
    return out or ob.tokens


def _state_dict(ob):
    if ob is None:
        return None
    return {"file": ob.file_id, "ts": ob.ts.isoformat(), "exists": ob.exists, "pii": ob.pii, "active": ob.active,
            "record_type": ob.record_type, "kind": ob.kind}
