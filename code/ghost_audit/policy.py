"""Policy layer: data category -> required treatment (from deletion_rules.csv) -> step plan."""

from __future__ import annotations

import csv

from .common import DATASET_DIR

SYSTEM_CATEGORY = {
    "account_service": "USER_PROFILE",
    "search_service": "SEARCH_INDEX_ENTRY",
    "cache_service": "CACHE_ENTRY",
    "export_service": "USER_EXPORT",
    "marketing_platform": "MARKETING_PROFILE",
    "analytics_platform": "ANALYTICS_PROFILE",
    "support_platform": "SUPPORT_MESSAGE",
    "object_storage": "SUPPORT_ATTACHMENT",
    "integration_service": "DATA_FLOW",
    "backup_service": "BACKUP_ARCHIVE",
    "security_service": "SECURITY_AUDIT_LOG",
    "legal_service": "LEGAL_HOLD_RECORD",
    "identity_service": "ACCOUNT_TOMBSTONE",
    "email_delivery_service": "EMAIL_DELIVERY_LOG",
}

# Evidence shapes that describe an attachment / a contact-index copy rather than a
# conversation that must be kept.
ATTACHMENT_KINDS = ("log:attachment_pii_scan", "document:support_platform")
CONTACT_INDEX_KINDS = ("log:contact_query_completed", "log:deletion_marker_check")


class Policy:
    def __init__(self, dataset_dir=DATASET_DIR):
        self.rules = {}
        with open(dataset_dir / "deletion_rules.csv", newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                self.rules[row["data_category"]] = row
        # DATA_FLOW is not a stored-data category: an enabled flow must be disabled.
        self.rules.setdefault("DATA_FLOW", {"rule_id": "derived", "required_action": "DISABLE"})

    # -------------------------------------------------------------- categorisation

    def category(self, view, store):
        st = view.state
        rtypes = [o.record_type for o in store.by_resource.get(view.resource_id, []) if o.record_type]
        kinds = {o.kind for o in store.by_resource.get(view.resource_id, [])}
        if view.system == "support_platform":
            if "SUPPORT_ATTACHMENT" in rtypes or kinds & set(ATTACHMENT_KINDS):
                return "SUPPORT_ATTACHMENT"
            if kinds & set(CONTACT_INDEX_KINDS):
                return "SUPPORT_ATTACHMENT"  # whole-record copy in a legacy contact index -> delete
            return "SUPPORT_MESSAGE"
        if view.system == "analytics_platform" and "SECONDARY_IDENTIFIER" in rtypes:
            return "SECONDARY_IDENTIFIER"
        return SYSTEM_CATEGORY.get(view.system, "")

    def action(self, category):
        rule = self.rules.get(category)
        return rule["required_action"] if rule else None

    # -------------------------------------------------------------- selection

    def select(self, views):
        """Drop resources we cannot ground, and keep one primary account."""
        out = {}
        accounts = [v for v in views.values() if v.system == "account_service"]
        if len(accounts) > 1:
            # Prefer content-linked over timing-attributed, then the most recent sighting.
            def key(v):
                ts = v.state.ts if v.state else None
                return (not v.attributed, ts.timestamp() if ts else 0)
            keep = max(accounts, key=key)
            for v in accounts:
                if v is not keep and (v.state is None or not v.state.exists):
                    views = {k: x for k, x in views.items() if x is not v}
        for rid, v in views.items():
            if v.state is None and v.system != "account_service":
                continue
            out[rid] = v
        return out

    # -------------------------------------------------------------- decision

    def decide(self, view):
        st = view.state
        cat = view.category
        action = self.action(cat)
        ev = st.file_id if st is not None and view.citeable else ""
        base = {"system": view.system, "resource_id": view.resource_id, "category": cat, "evidence": ev,
                "violation": False, "finding": None, "remediation": None, "verify": None}
        exists = st.exists if st is not None else False
        if action == "DELETE":
            if exists:
                base.update(violation=True, finding="REPORT_REMAINING_RECORD", remediation="DELETE_RECORD")
            base["verify"] = "VERIFY_RECORD_ABSENT"
        elif action == "DISABLE":
            if st is not None and st.active:
                base.update(violation=True, finding="REPORT_ACTIVE_DATA_FLOW", remediation="DISABLE_DATA_FLOW")
            base["verify"] = "VERIFY_DATA_FLOW_DISABLED"
        elif action == "RETAIN" and cat == "BACKUP_ARCHIVE":
            if st is not None and st.active:
                base.update(violation=True, finding="REPORT_RESTORE_RISK", remediation="BLOCK_RESTORE")
            base["verify"] = "VERIFY_RESTORE_BLOCKED"
        elif action == "RETAIN":
            if not exists:
                return None
            base["verify"] = "VERIFY_RECORD_PRESENT"
        elif action == "REDACT":
            if not exists:
                return None
            if st.pii:
                base.update(violation=True, finding="REPORT_EXPOSED_PII", remediation="REDACT_RECORD")
            base["verify"] = "VERIFY_RECORD_REDACTED"
        elif action == "ANONYMIZE":
            if not exists:
                return None
            if st.pii:
                base.update(violation=True, finding="REPORT_EXPOSED_PII", remediation="ANONYMIZE_RECORD")
            base["verify"] = "VERIFY_RECORD_ANONYMIZED"
        else:
            return None
        return base
