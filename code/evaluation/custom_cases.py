"""Hand-written evaluation cases (not from the organiser's labels).

Each case probes one behaviour the public samples only touch lightly. Expected outputs
were derived by hand from the participant-visible evidence cited in each comment.
"""

from __future__ import annotations


def _steps(*rows):
    return [{"step_no": i, "phase": p, "system_id": s, "resource_id": r, "operation": o, "state": st,
             "evidence_id": e} for i, (p, s, r, o, st, e) in enumerate(rows, 1)]


CUSTOM_CASES = [
    {
        # Historical identifier: the request uses Min-jun Kim's *old* email
        # (user_audit_log audit_282). The analytics row is keyed by the old email hash
        # (audit_283) in evidence_01, so it must still be found.
        "request": {
            "case_id": "custom_01", "email": "minjun.kim90@gmail.com",
            "requested_at": "2026-08-24T12:00:00Z", "reported_status": "COMPLETE",
            "reported_at": "2026-08-25T12:00:00Z", "audit_at": "2026-09-15T12:00:00Z",
            "request_text": "I used this older address on my account. Please delete the account and anything "
                            "still tied to it.",
        },
        "expected_status": "INCOMPLETE",
        "expected_steps": _steps(
            ("FINDING", "analytics_platform", "analytics_9914", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_01"),
            ("REMEDIATION", "analytics_platform", "analytics_9914", "DELETE_RECORD", "ABSENT", ""),
            ("VERIFICATION", "account_service", "account_9797", "VERIFY_RECORD_ABSENT", "ABSENT", ""),
            ("VERIFICATION", "analytics_platform", "analytics_9914", "VERIFY_RECORD_ABSENT", "ABSENT", ""),
            ("VERIFICATION", "security_service", "audit_record_2945", "VERIFY_RECORD_PRESENT", "PRESENT", ""),
        ),
    },
    {
        # Non-deletion request in Korean asking for a copy of the data -> no deletion authorised,
        # even though the subject (user_51) has live records in evidence_05.
        "request": {
            "case_id": "custom_02", "email": "seoyeon-han@gmail.com",
            "requested_at": "2026-08-31T10:00:00Z", "reported_status": "NOT_STARTED",
            "reported_at": "2026-09-01T10:00:00Z", "audit_at": "2026-09-21T10:00:00Z",
            "request_text": "제 계정 정보 사본을 보내 주세요. 계정은 계속 사용할 예정입니다.",
        },
        "expected_status": "NOT_APPLICABLE",
        "expected_steps": [],
    },
    {
        # Upper-cased request email must resolve to the same person (user_05). The only copy
        # of the profile lives in the failover replica (replication drift, evidence_54).
        "request": {
            "case_id": "custom_03", "email": "LEILA.HADDAD@YAHOO.COM",
            "requested_at": "2026-08-27T14:17:00Z", "reported_status": "COMPLETE",
            "reported_at": "2026-08-28T09:03:00Z", "audit_at": "2026-09-08T13:40:00Z",
            "request_text": "Please delete my account; the email address is still recognised after deletion.",
        },
        "expected_status": "INCOMPLETE",
        "expected_steps": _steps(
            ("FINDING", "account_service", "account_005", "REPORT_REMAINING_RECORD", "PRESENT", "evidence_54"),
            ("REMEDIATION", "account_service", "account_005", "DELETE_RECORD", "ABSENT", ""),
            ("VERIFICATION", "account_service", "account_005", "VERIFY_RECORD_ABSENT", "ABSENT", ""),
        ),
    },
    {
        # Negated deletion wording ("do not delete") must win over the word "delete".
        "request": {
            "case_id": "custom_04", "email": "keiko.nakamura@gmail.com",
            "requested_at": "2026-09-01T12:00:00Z", "reported_status": "NOT_STARTED",
            "reported_at": "2026-09-02T12:00:00Z", "audit_at": "2026-09-15T12:00:00Z",
            "request_text": "Please do not delete anything - I only need to know which marketing lists I am on.",
        },
        "expected_status": "NOT_APPLICABLE",
        "expected_steps": [],
    },
]
