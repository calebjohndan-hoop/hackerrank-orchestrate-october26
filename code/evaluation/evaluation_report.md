# Evaluation Report — Ghost Data Still Exists

Run: `python3 code/evaluation/main.py --custom --verbose`

The only labelled data is the 32 public samples (`dataset/sample_output.csv`); the 518
evaluation requests have hidden answers. All metrics below are measured, not estimated.
The samples are audited inside the same batch as the 518 test requests (the activity-timing
fallback needs to see every request sharing a workflow report time), exactly as in the
production run.

## Metrics on the public samples (32 cases)

| Metric | Value |
|---|---|
| Case-level `deletion_status` accuracy | **1.000** (32/32) |
| Step-level precision (all 7 fields must match) | **0.995** |
| Step-level recall | **0.969** |
| Step-level F1 | **0.982** |
| Sequence accuracy (entire `steps_json` identical) | **0.812** (26/32) |
| Structurally invalid cases | 0 |

Field-level accuracy over position-aligned steps (a missing step counts as a miss for
every field, so these are lower than step precision):

| phase | system_id | resource_id | operation | state | evidence_id |
|---|---|---|---|---|---|
| 0.974 | 0.948 | 0.943 | 0.948 | 0.948 | 0.969 |

Structural checks on the full 518-row `output.csv`: exactly 518 rows, header exact,
sorted by case number, every `steps_json` valid JSON, consecutive `step_no`, phase order,
operation/state/phase pairs from the allowed table, operation → system → resource ordering,
empty `evidence_id` on proposed work, evidence on every finding/COMPLETE verification, and no
cited file captured after the case's `audit_at`: **0 violations**. Two consecutive runs
produce byte-identical files.

### Remaining sample misses (6 cases) and why

| Case | Miss | Cause |
|---|---|---|
| case_01, case_02, case_04 | `account_00N` verify-absent missing | Hand-made sample ids: no participant-visible file ties `account_001/002/004` to the person; I do not hard-code a `user_0N → account_0N` pattern. |
| case_18, case_20 | `audit_record_6645` / `audit_record_5569` missing | These ids appear in **no** evidence file or activity row. Unreachable. |
| case_23 | picks `account_7015` instead of `account_410` | Both are identifier-free FAILED deletes in the same report window for three NOT_STARTED cases; the evidence offers no tie-breaker (activity-timing fallback). |

## Custom evaluation cases (4 cases, `code/evaluation/custom_cases.py`)

All pass (status 1.000, step F1 1.000, sequence 1.000).

| Id | What it probes | Expected |
|---|---|---|
| custom_01 | Request sent from a **historical** email (`user_audit_log` audit_282); an analytics row keyed only by the **old email hash** (audit_283) in a log | INCOMPLETE: remaining `analytics_9914` → delete; verify account absent, audit record retained |
| custom_02 | Korean request for a **copy** of the data, explicitly keeping the account | NOT_APPLICABLE, `[]` |
| custom_03 | Upper-cased request email; the only live profile copy is in the **failover replica** (replication drift) | INCOMPLETE: `account_005` remaining (evidence_54) → delete → verify |
| custom_04 | "Please **do not delete** anything …" — negation must beat the keyword | NOT_APPLICABLE, `[]` |

## Failures found and improvements made (chronological)

| Step | Sample step-F1 | Sequence acc. | Change |
|---|---|---|---|
| 1 | 0.788 | 0.219 | First end-to-end run. Intent rules too narrow ("remove", "gone" not recognised) → 7 cases wrongly NOT_APPLICABLE. |
| 2 | 0.865 | 0.281 | Broadened multilingual deletion terms; made negative patterns specific ("send me a copy of", not "copy of"). Status accuracy 0.78 → 0.97. |
| 3 | 0.907 | 0.375 | **Coverage scoping.** A snapshot that omits a record was treated as proof of absence for everyone; batch-scoped captures made retained security records "absent". Now a capture only proves absence for subjects it mentions. |
| 4 | 0.947 | 0.562 | **Two-pass batch audit.** Identifier-free records (404 account lookups, fully anonymised analytics rows) are attributed from deletion-worker actions just before `reported_at`, only to cases still lacking such a record; anonymised analytics only to otherwise-COMPLETE cases (an INCOMPLETE peer stole `analytics_1895` before this). |
| 5 | 0.957 | 0.594 | Linking from later captures allowed (resource ownership is immutable) while **state and citations stay ≤ audit_at**; fixes `account_3866`, which is only named in a file captured after the audit. |
| 6 | 0.971 | 0.719 | Cited evidence must *agree* with the decided state (e.g. a coverage "absent" can't be cited for a record a later log proves present); unknown PII flags no longer veto a citation. |
| 7 | 0.982 | 0.812 | Matched the worker result to the workflow claim (`COMPLETE↔SUCCESS`, `PARTIALLY_COMPLETE↔PARTIAL`, `NOT_STARTED↔FAILED`) when several cases share a report time; +10 min grace after `reported_at`. |
| 8 | (same) | (same) | Screenshot/PDF fixes found by reviewing the 11 single-artefact test cases: pages listing several customers no longer link every id on the page (only catalog-linked or single-record pages); catalog aliases (`delivery_confirmation_19.pdf → object_19`, `NST-2026-0718-1842 → export_1842`, `user_17 → tombstone_17`) resolved; console semantics (healthy connector = active flow, archive in a recovery console = restorable, printed email = exposed PII). |

| 9 | (same) | (same) | **Attribution audit** (`attribution_audit.py`) found 6 anonymised analytics rows each attributed to *two* cases: neighbouring report windows overlap. A first fix ("nearest report the action precedes") shifted 38 accounts to the previous case, because account deletes lead their report by ~57 min and report times are often an hour apart; samples did not move, so this was caught only by diffing `output.csv`. Final fix: each worker action belongs to the one report time whose gap best matches that action type's measured median lead (57 min delete, 28 min anonymise). Net effect: 23 rows changed, all one anonymised-analytics verification step in COMPLETE cases; no status changed; no record is named in two cases. |

## Dependence on the activity-timing fallback (evaluation set)

`python3 code/evaluation/attribution_audit.py --list`

| Measure | Count |
|---|---|
| Deletion cases | 508 of 518 |
| Cases whose steps name a timing-attributed record | **316** |
| … weakest record `high` (only candidate, only case waiting) | 173 |
| … weakest record `medium` (status match or citeable sighting decides) | 70 |
| … weakest record `low` (tie broken by time / case order, or result contradicts the reported status) | **73** |
| Cases where an attributed record drives a FINDING (could change the status) | 0 |

Attributed records only ever add a VERIFY step (account absent, analytics anonymised), so
the fallback never changes a `deletion_status`; the risk is wrong/missing verification steps.
Calibration on the public samples (attributed record appears in the sample answer): high
8/8, medium 6/6, low 6/7 — small numbers, so treat `low` as roughly a coin flip on the
hidden set.

Known limitations: identifier-free records sharing one report window are a genuine
ambiguity in the evidence (≈1 in 3 sample groups); security audit records that exist only
in organiser truth cannot be produced.
