"""Identity resolution: current snapshot + historical audit log."""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass, field

from .common import DATASET_DIR, norm_email, norm_phone


@dataclass
class Identity:
    user_id: str
    names: set = field(default_factory=set)
    emails: set = field(default_factory=set)      # normalised (lower-case)
    phones: set = field(default_factory=set)      # digits only
    hashes: set = field(default_factory=set)
    current_email: str = ""
    # audit_ids that evidence each historical identifier (identifier -> audit_id)
    history_audit: dict = field(default_factory=dict)

    def tokens(self) -> set:
        """All exact-match tokens that identify this user in evidence."""
        out = {("user", self.user_id)}
        out |= {("email", e) for e in self.emails}
        out |= {("phone", p) for p in self.phones if len(p) >= 7}
        out |= {("hash", h) for h in self.hashes}
        return out


class IdentityIndex:
    def __init__(self, dataset_dir=DATASET_DIR):
        self.users: dict[str, Identity] = {}
        self.by_token: dict[tuple, set] = defaultdict(set)
        with open(dataset_dir / "user_identities.csv", newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                ident = Identity(user_id=row["user_id"], current_email=norm_email(row["email"]))
                ident.names.add(row["name"].strip())
                ident.emails.add(norm_email(row["email"]))
                if row["phone"]:
                    ident.phones.add(norm_phone(row["phone"]))
                if row["email_hash"]:
                    ident.hashes.add(row["email_hash"].strip().lower())
                self.users[ident.user_id] = ident
        with open(dataset_dir / "user_audit_log.csv", newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                ident = self.users.get(row["user_id"])
                if ident is None:
                    continue
                fname = row["field_name"].strip().lower()
                for value in (row["old_value"], row["new_value"]):
                    if not value:
                        continue
                    if fname == "email":
                        ident.emails.add(norm_email(value))
                        ident.history_audit.setdefault(("email", norm_email(value)), row["audit_id"])
                    elif fname == "phone":
                        ident.phones.add(norm_phone(value))
                        ident.history_audit.setdefault(("phone", norm_phone(value)), row["audit_id"])
                    elif fname == "email_hash":
                        ident.hashes.add(value.strip().lower())
                        ident.history_audit.setdefault(("hash", value.strip().lower()), row["audit_id"])
                    elif fname in ("name", "full_name"):
                        ident.names.add(value.strip())
        for ident in self.users.values():
            for tok in ident.tokens():
                self.by_token[tok].add(ident.user_id)

    def resolve_email(self, email: str) -> Identity | None:
        """Map a request email (current or historical) to exactly one user."""
        users = self.by_token.get(("email", norm_email(email)), set())
        if len(users) == 1:
            return self.users[next(iter(users))]
        if len(users) > 1:
            # Prefer the user whose *current* email matches.
            cur = [u for u in users if self.users[u].current_email == norm_email(email)]
            if len(cur) == 1:
                return self.users[cur[0]]
        return None

    def owners_of(self, tokens) -> set:
        out = set()
        for tok in tokens:
            out |= self.by_token.get(tok, set())
        return out
