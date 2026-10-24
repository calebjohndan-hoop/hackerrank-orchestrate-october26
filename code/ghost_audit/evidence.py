"""Evidence extraction.

Every participant-visible evidence file is turned into a flat list of
``Observation`` objects: one per (file, resource, point in time).  Each
observation records which identifiers were seen next to the resource (used to
link the resource to a person) and what the evidence says about the resource's
state (exists / exposes PII / is an active flow or restorable backup).

Formats handled: SQLite SQL dumps (typed tables and generic JSON-payload replica
tables), JSON exports and snapshot bundles, many log dialects (key=value,
JSON-lines, syslog, bracketed traces, pipe-separated streams, escaped
``message="..."`` payloads), plain-text handovers, PDFs (pdftotext/pdfplumber)
and screenshots (OCR via RapidOCR, cached on disk).
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import vision
from .common import (DATASET_DIR, EMAIL_RE, HASH_RE, REPO_ROOT, RESOURCE_RE, USER_RE, is_null, norm_email,
                     norm_phone, parse_ts, system_for)

OCR_CACHE = REPO_ROOT / "code" / "cache" / "ocr_text.json"

# Field names whose values identify a person (used both for linking and to
# decide whether a record still exposes direct identifiers).
DIRECT_ID_FIELDS = {
    "email", "owner_email", "requester_email", "customer_email", "uploaded_by", "phone", "owner_phone",
    "requester_phone", "email_hash", "full_name", "name", "display_name", "requester_name",
}
REFERENCE_FIELDS = {"customer_reference", "subject_reference", "user_id", "subject_selector", "subject_ref"}
LOOKUP_FIELDS = {"lookup_key", "subject_key", "match_key", "scan_key", "lookup_value", "correlation_key",
                 "matched_value", "query", "subject_reference", "subject_ref", "subject_selector"}


@dataclass
class Observation:
    file_id: str
    captured: datetime            # manifest capture time of the file
    ts: datetime                  # time the observation describes
    resource_id: str
    system: str
    tokens: set = field(default_factory=set)       # identifier tokens linked to the record
    exists: bool | None = None                     # record present?
    pii: bool | None = None                        # direct identifiers still exposed?
    active: bool | None = None                     # flow enabled / restore allowed?
    record_type: str | None = None                 # explicit category when the evidence names one
    kind: str = ""                                 # free-form description of the evidence shape
    detail: dict = field(default_factory=dict)
    copy: str = ""                                 # table / replica the row came from


# --------------------------------------------------------------------------- tokens

def value_tokens(value) -> set:
    """Classify a raw identifier value into typed tokens."""
    out = set()
    if value is None:
        return out
    if isinstance(value, (list, tuple, set)):
        for v in value:
            out |= value_tokens(v)
        return out
    s = str(value).strip()
    if is_null(s):
        return out
    for m in EMAIL_RE.findall(s):
        out.add(("email", norm_email(m)))
    for m in USER_RE.findall(s):
        out.add(("user", m))
    for m in HASH_RE.findall(s.lower()):
        out.add(("hash", m))
    if not out:
        digits = norm_phone(s)
        if len(digits) >= 8 and re.fullmatch(r"[+\d\s().\-]+", s):
            out.add(("phone", digits))
    return out


def record_tokens(rec: dict) -> set:
    out = set()
    for k, v in rec.items():
        if isinstance(v, dict):
            out |= record_tokens(v)
        elif k in DIRECT_ID_FIELDS or k in REFERENCE_FIELDS or k in LOOKUP_FIELDS or k in ("message_text", "message_excerpt", "profile"):
            out |= value_tokens(v)
    return out


def record_has_pii(rec: dict) -> bool:
    for k, v in rec.items():
        if isinstance(v, dict):
            if record_has_pii(v):
                return True
        elif k in DIRECT_ID_FIELDS and not is_null(v):
            return True
        elif k in ("message_text", "message_excerpt") and isinstance(v, str) and EMAIL_RE.search(v):
            return True
    return False


def truthy(v) -> bool | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "enabled", "allowed", "active"):
        return True
    if s in ("0", "false", "no", "disabled", "denied", "stopped"):
        return False
    return None


RESULT_STATE = {
    # result -> (exists, pii, active)
    "FOUND": (True, None, None),
    "NOT_FOUND": (False, False, None),
    "IDENTIFIABLE": (True, True, None),
    "ANONYMIZED": (True, False, None),
    "REDACTED": (True, False, None),
    "ACTIVE": (True, None, True),
    "DISABLED": (True, None, False),
    "BLOCKED": (True, None, False),
    "RETAINED": (True, None, None),
    "PRESENT": (True, None, None),
    "ABSENT": (False, False, None),
}


def obs_from_record(rec: dict, file_id, captured, ts, kind, copy="", system=None) -> Observation | None:
    rid = rec.get("resource_id")
    if not rid or not RESOURCE_RE.fullmatch(str(rid)):
        return None
    system = system_for(rid) or system
    ob = Observation(file_id=file_id, captured=captured, ts=ts, resource_id=rid, system=system, kind=kind, copy=copy)
    ob.detail = rec
    result = rec.get("result")
    if result:
        # Operational result records: explicit category + state, identifier in lookup_value.
        ob.record_type = rec.get("record_type")
        ob.exists, ob.pii, ob.active = RESULT_STATE.get(str(result).upper(), (None, None, None))
        ob.tokens = value_tokens(rec.get("lookup_value")) | value_tokens(rec.get("subject_reference"))
        return ob
    ob.tokens = record_tokens(rec)
    ob.exists = True
    if "status_code" in rec:          # account lookups in service exports
        ob.exists = str(rec.get("status_code")) == "200"
        prof = rec.get("profile") or {}
        ob.pii = bool(prof) and record_has_pii(prof)
        ob.tokens |= record_tokens(prof) if prof else set()
        return ob
    if "hits" in rec:                 # search queries in service exports
        hits = rec.get("hits")
        if isinstance(hits, dict):
            total = hits.get("total")
            docs = hits.get("documents") or []
            ob.exists = bool(docs) if total is None else int(total) > 0
            ob.pii = any(record_has_pii(d) for d in docs if isinstance(d, dict))
            for d in docs:
                if isinstance(d, dict):
                    ob.tokens |= record_tokens(d)
        elif isinstance(hits, list):
            ob.exists = len(hits) > 0
        else:
            ob.exists = hits is not None and str(hits).isdigit() and int(hits) > 0
        ob.tokens |= value_tokens(rec.get("query"))
        if isinstance(hits, list):
            for h in hits:
                if isinstance(h, dict):
                    ob.tokens |= record_tokens(h)
        return ob
    if "download_available" in rec and rec.get("download_available") is False:
        ob.exists = True  # metadata row still present; treat the export as remaining
    ob.pii = record_has_pii(rec)
    if system == "analytics_platform":
        ob.pii = ob.pii or not is_null(rec.get("user_id")) or not is_null(rec.get("customer_reference"))
    if "enabled" in rec:
        ob.active = truthy(rec.get("enabled"))
    if "restore_allowed" in rec:
        ob.active = truthy(rec.get("restore_allowed"))
    if system == "support_platform" and ("file_name" in rec or rec.get("storage_class") == "ticket_attachment"):
        ob.record_type = "SUPPORT_ATTACHMENT"
    return ob


# --------------------------------------------------------------------------- SQL

def parse_sql(path: Path, file_id, captured):
    text = path.read_text(encoding="utf-8")
    con = sqlite3.connect(":memory:")
    try:
        con.executescript(text)
    except sqlite3.Error:
        con.close()
        return [], []
    obs, coverage = [], []
    tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    scope = {}
    if "snapshot_scope" in tables:
        scope = {r[0]: parse_ts(r[1]) for r in con.execute("SELECT origin_system, captured_at FROM snapshot_scope")}
    for t in tables:
        if t == "snapshot_scope":
            continue
        cur = con.execute(f'SELECT * FROM "{t}"')
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        systems_seen = set()
        for row in rows:
            if "record_json" in row:
                rec = json.loads(row["record_json"])
                ts = parse_ts(row.get("updated_at")) or captured
                ob = obs_from_record(rec, file_id, captured, ts, f"sql_replica:{row.get('collection_name')}", copy=t,
                                     system=row.get("origin_system"))
            else:
                rec = dict(row)
                ts = parse_ts(rec.get("updated_at")) or captured
                ob = obs_from_record(rec, file_id, captured, ts, f"sql:{t}", copy=t)
                if ob and "account_status" in rec:
                    status = str(rec.get("account_status") or "").upper()
                    ob.exists = status not in ("DELETED", "PURGED")
                if ob and t == "security_audit_records":
                    ob.pii = False
            if ob:
                ob.copy = t
                obs.append(ob)
                systems_seen.add(ob.system)
        # Coverage: a typed table (or a scoped inventory) is a full listing of its system.
        typed_system = TABLE_SYSTEM.get(t)
        if typed_system and scope and typed_system not in scope:
            continue  # inventory table outside the snapshot's declared scope proves nothing
        if typed_system:
            coverage.append((file_id, scope.get(typed_system, captured), typed_system, t))
        elif "record_json" in cols:
            for s in ALL_SYSTEMS:
                coverage.append((file_id, captured, s, t))
        else:
            for s in systems_seen:
                coverage.append((file_id, captured, s, t))
    con.close()
    return obs, coverage


TABLE_SYSTEM = {
    "customer_profiles": "account_service",
    "customer_profiles_primary": "account_service",
    "customer_profiles_failover_replica": "account_service",
    "customer_search_documents": "search_service",
    "search_documents_primary": "search_service",
    "customer_analytics_profiles": "analytics_platform",
    "support_records": "support_platform",
    "customer_exports": "export_service",
    "connector_subscriptions": "integration_service",
    "backup_restore_entries": "backup_service",
    "security_audit_records": "security_service",
}
ALL_SYSTEMS = ["account_service", "search_service", "analytics_platform", "support_platform", "export_service",
               "integration_service", "backup_service", "security_service", "identity_service", "legal_service",
               "marketing_platform", "email_delivery_service"]


# --------------------------------------------------------------------------- JSON

def parse_json(path: Path, file_id, captured):
    data = json.loads(path.read_text(encoding="utf-8"))
    obs, coverage = [], []

    def walk(node, ts, ctx_system, kind):
        if isinstance(node, dict):
            local_ts = parse_ts(node.get("observed_at") or node.get("recorded_at") or node.get("captured_at")) or ts
            if "resource_id" in node and RESOURCE_RE.fullmatch(str(node["resource_id"])):
                ob = obs_from_record(node, file_id, captured, local_ts, kind, system=ctx_system)
                if ob:
                    obs.append(ob)
                return
            store = node.get("data_store")
            if store and isinstance(node.get("records"), list):
                coverage.append((file_id, local_ts, store, node.get("collection", "")))
                for r in node["records"]:
                    walk(r, local_ts, store, f"bundle:{store}/{node.get('collection')}")
                return
            for k, v in node.items():
                sys_hint = k if k in ALL_SYSTEMS else ctx_system
                walk(v, local_ts, sys_hint, kind if sys_hint == ctx_system else f"export:{k}")
        elif isinstance(node, list):
            for x in node:
                walk(x, ts, ctx_system, kind)

    walk(data, parse_ts(data.get("exported_at") or data.get("captured_at")) or captured, None, "json")
    # Service exports list every looked-up record per service; collections with a
    # data_store are complete listings (coverage recorded above).
    return obs, coverage


# --------------------------------------------------------------------------- logs

KV_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)=("(?:[^"\\]|\\.)*"|[^\s|]+)')
TRACE_RE = re.compile(r"\b((?:tr|rb)-[0-9][0-9-]*)\b")
SERVICE_NAMES = ["account_service", "search_service", "analytics_platform", "support_platform", "export_service",
                 "integration_service", "backup_service", "security_service", "identity_service", "legal_service",
                 "marketing_platform", "support_replica", "downstream_delivery_service", "search_index_service",
                 "readback_collector", "profile_service"]


def parse_kv(text: str) -> dict:
    out = {}
    for k, v in KV_RE.findall(text):
        if v.startswith('"') and v.endswith('"'):
            v = v[1:-1].replace('\\"', '"')
        out[k] = v
    msg = out.get("message")
    if msg and "=" in msg:
        out.update(parse_kv(msg))
    return out


def parse_log_line(line: str) -> dict | None:
    line = line.strip()
    if not line:
        return None
    ts = parse_ts(line.split(" ", 1)[0])
    if ts is None:
        return None
    rest = line.split(" ", 1)[1] if " " in line else ""
    fields = {}
    j = rest.find("{")
    if rest.startswith("{") or (j >= 0 and rest[j:].startswith('{"')):
        try:
            fields = {k: str(v) if v is not None else "null" for k, v in json.loads(rest[j:]).items()}
        except json.JSONDecodeError:
            fields = {}
    if not fields:
        fields = parse_kv(rest)
    # event from bracket / pipe dialects
    if "event" not in fields:
        # "[LEVEL] [service] [trace] event: k=v"  or  "| LEVEL | service | trace | event | k=v"
        m = (re.search(r"\]\s+([a-z_]+):", rest)
             or re.match(r"\|\s*[A-Z]+\s*\|\s*[a-z_]+\s*\|\s*\S+\s*\|\s*([a-z_]+)\s*\|", rest))
        if m:
            fields["event"] = m.group(1)
    trace = fields.get("trace_id") or fields.get("requestId")
    if not trace:
        m = TRACE_RE.search(rest)
        trace = m.group(1) if m else None
    service = fields.get("service") or fields.get("function") or fields.get("logger")
    if not service or service not in SERVICE_NAMES:
        for name in SERVICE_NAMES:
            if re.search(rf"(?<![a-z_]){name}(?![a-z_])", rest):
                service = name
                break
    return {"ts": ts, "fields": fields, "trace": trace, "service": service, "raw": line}


def log_state(ev: str, f: dict, system: str):
    """Return (exists, pii, active, record_type) for one resource-bearing log line."""
    g = lambda k: f.get(k)
    if ev == "record_lookup" or g("result") in RESULT_STATE and g("record_type"):
        e, p, a = RESULT_STATE.get(str(g("result")).upper(), (None, None, None))
        return e, p, a, g("record_type")
    if ev in ("profile_lookup_completed",):
        absent = g("rows") == "0" or g("http_status") == "404"
        return (not absent), (not absent), None, None
    if ev in ("index_query_completed",):
        hit = g("result") == "hit" or (g("total_hits") not in (None, "0"))
        if g("result") == "miss":
            hit = False
        return hit, hit, None, None
    if ev in ("analytics_profile_read_completed", "analytics_profile_read"):
        ids = [g(k) for k in ("email", "phone", "email_hash", "customer_reference")]
        pii = any(not is_null(x) for x in ids) or (g("direct_identifiers") not in (None, "0"))
        return True, pii, None, None
    if ev == "analytics_identity_scan":
        return True, True, None, None
    if ev == "contact_lookup_completed":
        return g("rows") != "0", True, None, "SECONDARY_IDENTIFIER"
    if ev == "support_ticket_read_completed":
        pii = any(not is_null(g(k)) for k in ("requester_name", "requester_email", "requester_phone"))
        return True, pii, None, None
    if ev == "message_pii_scan":
        return True, True, None, "SUPPORT_MESSAGE"
    if ev == "message_read":
        txt = g("message_text") or ""
        return True, bool(EMAIL_RE.search(txt)), None, "SUPPORT_MESSAGE"
    if ev == "attachment_pii_scan":
        return True, True, None, "SUPPORT_ATTACHMENT"
    if ev in ("contact_query_completed",):
        return g("rows") != "0", True, None, None
    if ev == "deletion_marker_check":
        return (truthy(g("marker_found")) is False), True, None, None
    if ev in ("connector_config_read_completed", "connector_heartbeat", "connector_config_loaded"):
        return True, None, truthy(g("enabled")), None
    if ev in ("restore_policy_read_completed",):
        return truthy(g("archive_present")) is not False, None, truthy(g("restore_allowed")), None
    if ev == "restore_candidate_evaluated":
        active = truthy(g("eligible")) and truthy(g("deletion_block")) is False
        return True, None, bool(active), None
    if ev == "restore_authorization_completed":
        return True, None, (g("decision") or "").lower() == "allow", None
    if ev in ("audit_record_read_completed",):
        return g("rows") != "0", False, None, None
    if ev in ("export_download_check_completed", "export_metadata_read_completed"):
        avail = truthy(g("available"))
        return avail is not False, True, None, None
    if ev == "retained_order_loaded":
        return True, None, None, None
    return None


def parse_log(path: Path, file_id, captured):
    lines = [parse_log_line(l) for l in path.read_text(encoding="utf-8").splitlines()]
    lines = [l for l in lines if l]
    trace_tokens = defaultdict(set)
    for l in lines:
        f = l["fields"]
        if l["trace"]:
            for k in LOOKUP_FIELDS | DIRECT_ID_FIELDS | REFERENCE_FIELDS:
                if k in f:
                    trace_tokens[l["trace"]] |= value_tokens(f[k])
    obs = []
    for l in lines:
        f = l["fields"]
        rid = f.get("resource_id")
        if not rid or not RESOURCE_RE.fullmatch(rid):
            continue
        system = f.get("system") or system_for(rid)
        ev = f.get("event", "")
        st = log_state(ev, f, system)
        if st is None:
            continue
        tokens = set()
        for k in LOOKUP_FIELDS | DIRECT_ID_FIELDS | REFERENCE_FIELDS:
            if k in f:
                tokens |= value_tokens(f[k])
        if l["trace"]:
            tokens |= trace_tokens[l["trace"]]
        ob = Observation(file_id=file_id, captured=captured, ts=l["ts"], resource_id=rid, system=system_for(rid) or system,
                         tokens=tokens, kind=f"log:{ev}", detail=f)
        ob.exists, ob.pii, ob.active, ob.record_type = st
        obs.append(ob)
    return obs, []


# --------------------------------------------------------------------------- text / pdf / image

PIPE_ROW_RE = re.compile(r"^\s*-?\s*(\S+Z)\s*\|\s*([a-z_]+)\s*\|\s*(\S+)\s*\|\s*([A-Z_]+)\s*\|\s*([A-Z_]+)\s*\|\s*(.+?)\s*$")


def parse_text(path: Path, file_id, captured):
    obs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = PIPE_ROW_RE.match(line)
        if not m:
            continue
        ts, system, rid, rtype, result, subject = m.groups()
        rec = {"resource_id": rid, "system": system, "record_type": rtype, "result": result,
               "lookup_value": subject, "recorded_at": ts}
        ob = obs_from_record(rec, file_id, captured, parse_ts(ts) or captured, "text_handover")
        if ob:
            obs.append(ob)
    return obs, []


def pdf_text(path: Path) -> str:
    try:
        return subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True,
                              check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        import pdfplumber  # fallback when poppler is unavailable
        with pdfplumber.open(str(path)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)


def image_text(path: Path, rel: str) -> str:
    """Local OCR text, plus a vision-model transcription when GROQ_API_KEY is set."""
    text = ocr_text(path, rel)
    extra = vision.transcribe(path)
    return f"{text}\n{extra}" if extra else text


def ocr_text(path: Path, rel: str) -> str:
    cache = {}
    if OCR_CACHE.exists():
        cache = json.loads(OCR_CACHE.read_text(encoding="utf-8"))
    if rel in cache:
        return cache[rel]
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return ""
    engine = RapidOCR()
    result, _ = engine(str(path))
    text = "\n".join(r[1] for r in (result or []))
    cache[rel] = text
    OCR_CACHE.parent.mkdir(parents=True, exist_ok=True)
    OCR_CACHE.write_text(json.dumps(cache, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return text


def normalise_ocr(text: str) -> str:
    # OCR commonly confuses o/0 inside identifiers (audit_record_oo3) and drops underscores' neighbours.
    text = re.sub(r"(audit_record_|account_|support_|search_|analytics_|backup_|export_|data_flow_|marketing_)([0-9oO]+)",
                  lambda m: m.group(1) + m.group(2).replace("o", "0").replace("O", "0"), text)
    return text


def document_tokens(text: str) -> set:
    tokens = value_tokens(text)
    for p in re.findall(r"\+\d[\d\s().\-]{7,}\d", text):
        tokens.add(("phone", norm_phone(p)))
    return tokens


def document_observation(rid, text, file_id, captured, kind) -> Observation:
    """Interpret a business artefact (screenshot / PDF) that shows ``rid``.

    Artefacts never state a deletion verdict; we read the facts they show: a live
    console row means the record exists; a page that still prints an email or phone
    means direct identifiers are exposed; a connector shown as healthy/continuous is an
    active flow; an archive listed in a recovery console is restorable."""
    tokens = document_tokens(text)
    low = text.lower()
    ob = Observation(file_id=file_id, captured=captured, ts=captured, resource_id=rid, system=system_for(rid),
                     tokens=tokens, kind=kind, detail={"text": text})
    ob.exists = True
    ob.pii = any(t[0] in ("email", "phone") for t in tokens)
    if ob.system == "integration_service":
        ob.active = ("healthy" in low or "continuous" in low) and "disabled" not in low and "paused" not in low
    elif ob.system == "backup_service":
        ob.active = not any(w in low for w in ("restore blocked", "restore denied", "deletion block"))
    elif ob.system == "security_service":
        ob.pii = False
    return ob


def parse_document(text: str, file_id, captured, kind):
    """One observation per resource id printed on the page (linking to people is decided
    later, because one page can show several customers)."""
    text = normalise_ocr(text)
    return [document_observation(rid, text, file_id, captured, kind)
            for rid in sorted(set(RESOURCE_RE.findall(text)))]


# --------------------------------------------------------------------------- driver

@dataclass
class EvidenceFile:
    file_id: str
    path: Path
    origin: str
    captured: datetime
    text: str = ""


class EvidenceStore:
    def __init__(self, dataset_dir=DATASET_DIR):
        self.files: dict[str, EvidenceFile] = {}
        self.observations: list[Observation] = []
        self.coverage: list[tuple] = []     # (file_id, ts, system, collection)
        with open(dataset_dir / "evidence_files.csv", newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                ef = EvidenceFile(row["file_id"], dataset_dir / row["file_path"], row["origin"],
                                  parse_ts(row["captured_at"]))
                self.files[ef.file_id] = ef
        for ef in self.files.values():
            obs, cov = self._parse(ef, dataset_dir)
            self.observations.extend(obs)
            self.coverage.extend(cov)
        self.reindex()

    def reindex(self):
        self.by_resource = defaultdict(list)
        for ob in self.observations:
            self.by_resource[ob.resource_id].append(ob)

    def add_alias_observations(self, aliases):
        """Artefacts often show a business alias (file name, statement number, customer
        key) instead of the resource id; ``aliases`` maps resource_id -> alias text."""
        for ef in self.files.values():
            if not ef.text:
                continue
            text = normalise_ocr(ef.text)
            for rid, alias in aliases:
                if alias and alias != rid and re.search(rf"(?<![\w.-]){re.escape(alias)}(?![\w-])", text):
                    kind = ("image:" if ef.path.suffix.lower() == ".png" else "document:") + ef.origin
                    ob = document_observation(rid, text, ef.file_id, ef.captured, kind)
                    ob.detail = {"alias": alias, "text": text}
                    self.observations.append(ob)
        self.reindex()

    def _parse(self, ef: EvidenceFile, dataset_dir):
        suffix = ef.path.suffix.lower()
        if suffix == ".sql":
            return parse_sql(ef.path, ef.file_id, ef.captured)
        if suffix == ".json":
            return parse_json(ef.path, ef.file_id, ef.captured)
        if suffix == ".log":
            return parse_log(ef.path, ef.file_id, ef.captured)
        if suffix == ".txt":
            return parse_text(ef.path, ef.file_id, ef.captured)
        if suffix == ".pdf":
            ef.text = pdf_text(ef.path)
            return parse_document(ef.text, ef.file_id, ef.captured, f"document:{ef.origin}"), []
        if suffix in (".png", ".jpg", ".jpeg"):
            rel = ef.path.relative_to(dataset_dir).as_posix()
            ef.text = image_text(ef.path, rel)
            return parse_document(ef.text, ef.file_id, ef.captured, f"image:{ef.origin}"), []
        return [], []
