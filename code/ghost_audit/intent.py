"""Does the request text authorise deletion?

Deterministic multilingual rules come first.  They cover every phrasing in the
dataset (English, Spanish, Portuguese, French, Japanese, Korean) and are
explainable.  An optional LLM fallback (``--llm``) is consulted only when the
rules are undecided, so a full run normally makes zero model calls.
"""

from __future__ import annotations

import re

DELETE_TERMS = [
    r"\bdelet", r"\berase", r"\berasure", r"\bremove (?:my|the) (?:account|profile)", r"\bclose (?:my|this|the) account",
    r"\bpermanently deleted", r"\bdeletion\b", r"account-deletion", r"\bremov", r"\bgone\b", r"\bclosed? (?:my|this|the)? ?account",
    r"\bwipe", r"\bforget me\b", r"\bclean my personal details\b", r"\bfinish the deletion\b",
    r"elimina", r"borra", r"exclus[ãa]o", r"excluir", r"apagar", r"suppression", r"supprim", r"effacer",
    r"削除", r"消去", r"삭제", r"탈퇴",
]
# Explicit statements that the person is NOT asking for deletion, or is asking for
# a different privacy right.
NEGATIVE_PATTERNS = [
    r"not a request to (?:erase|delete)", r"(?:don't|do not) delete", r"marketing lists only", r"stop the promotional",
    r"\b(?:send me|need|get|receive|want) a copy of\b", r"\bportable format\b", r"\bdownload my account information\b", r"\bwhat personal information\b",
    r"\bmisspelled\b", r"\bout of date\b", r"\bchange the email\b", r"\bresetting the login\b", r"\breset(?:ting)? (?:my )?password\b",
    r"\binvoices for my records\b", r"\bunsubscribe\b",
]


def classify(text: str) -> tuple[bool | None, str]:
    """Return (authorises_deletion, reason). None means undecided."""
    t = text.lower()
    neg = [p for p in NEGATIVE_PATTERNS if re.search(p, t)]
    pos = [p for p in DELETE_TERMS if re.search(p, t)]
    if neg:
        return False, f"non-deletion request ({neg[0]})"
    if pos:
        return True, f"deletion requested ({pos[0]})"
    return None, "no deletion intent found"


LLM_PROMPT = """You are a privacy-operations triage assistant.
Decide whether the customer message below explicitly asks the company to DELETE / ERASE
their account or personal data. Requests for access, a copy/export, correction, password
help, invoices, or marketing opt-out are NOT deletion requests, even if they mention data.
Answer with exactly one word: DELETE or OTHER.

Customer message:
{text}
"""


def classify_with_llm(text: str, client, usage) -> tuple[bool, str]:
    """Optional fallback; only used when the rules return None and --llm is set."""
    model = "claude-haiku-5-5"
    # One-word classification: thinking off (allowed on Haiku 5.5 at effort <= high), tiny output.
    msg = client.messages.create(model=model, max_tokens=16, thinking={"type": "disabled"},
                                 messages=[{"role": "user", "content": LLM_PROMPT.format(text=text)}])
    usage.record(model, msg.usage.input_tokens, msg.usage.output_tokens)
    if msg.stop_reason == "refusal":
        return False, "llm:refusal"
    answer = "".join(getattr(b, "text", "") for b in msg.content).strip().upper()
    return answer.startswith("DELETE"), f"llm:{answer}"
