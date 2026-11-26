"""Vendor lookup: exact name, then sender email domain, then fuzzy name. Read-only."""

import re
from typing import Any, Literal

import psycopg
from pydantic import BaseModel
from rapidfuzz import fuzz

from ap_agent.tools._common import norm_key

FUZZY_MATCH = 90
FUZZY_CANDIDATE = 80

_LEGAL_SUFFIXES = frozenset(
    {"inc", "incorporated", "ltd", "limited", "llc", "corp", "corporation", "co", "company", "ltee"}
)
_ADDR = re.compile(r"[\w.+\-']+@([\w\-]+(?:\.[\w\-]+)+)")


class VendorCandidate(BaseModel):
    vendor_id: int
    name: str
    score: float


class VendorMatch(BaseModel):
    vendor_id: int | None
    method: Literal["exact_name", "email_domain", "fuzzy", "none"]
    score: float = 0.0
    candidates: list[VendorCandidate] = []


class VendorRef(BaseModel):
    id: int
    name: str
    email_domain: str | None


def sender_domain(from_address: str | None) -> str | None:
    """'Billing <ar@Acme.example>' -> 'acme.example'."""
    m = _ADDR.search(from_address or "")
    return m.group(1).casefold() if m else None


def _domain_matches(sender: str, vendor_domain: str | None) -> bool:
    if not vendor_domain:
        return False
    vd = vendor_domain.strip().casefold().lstrip("@")
    return sender == vd or sender.endswith("." + vd)


def pick_vendor(
    vendors: list[VendorRef], vendor_name: str, from_address: str | None
) -> VendorMatch:
    """Pure decision logic (no DB) so it can be unit-tested."""
    key = norm_key(vendor_name)
    exact = [v for v in vendors if key and norm_key(v.name) == key]
    if len(exact) == 1:
        return VendorMatch(vendor_id=exact[0].id, method="exact_name", score=100.0)

    sender = sender_domain(from_address)
    by_domain = [v for v in vendors if sender and _domain_matches(sender, v.email_domain)]
    # With several same-name vendors, the sender domain only breaks the tie if it is one of them.
    if len(by_domain) == 1 and (not exact or by_domain[0] in exact):
        return VendorMatch(vendor_id=by_domain[0].id, method="email_domain", score=100.0)

    ambiguous = exact or by_domain
    if ambiguous:  # several vendors fit: a human has to choose
        return VendorMatch(
            vendor_id=None,
            method="none",
            candidates=[
                VendorCandidate(vendor_id=v.id, name=v.name, score=100.0) for v in ambiguous
            ][:3],
        )

    scored = sorted(
        (
            VendorCandidate(
                vendor_id=v.id,
                name=v.name,
                score=round(fuzz.token_set_ratio(key_words(vendor_name), key_words(v.name)), 1),
            )
            for v in vendors
        ),
        key=lambda c: (-c.score, c.name),
    )
    top3 = [c for c in scored[:3] if c.score >= FUZZY_CANDIDATE]
    if top3 and top3[0].score >= FUZZY_MATCH and (len(top3) == 1 or top3[1].score < top3[0].score):
        return VendorMatch(
            vendor_id=top3[0].vendor_id, method="fuzzy", score=top3[0].score, candidates=top3
        )
    return VendorMatch(
        vendor_id=None, method="none", score=top3[0].score if top3 else 0.0, candidates=top3
    )


def key_words(name: str) -> str:
    """Lower-case words without punctuation or legal suffixes, for fuzzy comparison."""
    words = re.sub(r"[\W_]+", " ", name.casefold()).split()
    kept = [w for w in words if w not in _LEGAL_SUFFIXES]
    return " ".join(kept or words)


def find_vendor(
    conn: psycopg.Connection[Any], vendor_name: str, from_address: str | None
) -> VendorMatch:
    rows = conn.execute(
        "SELECT id, name, email_domain FROM trex.vendors WHERE active ORDER BY id"
    ).fetchall()
    vendors = [VendorRef(id=r[0], name=r[1], email_domain=r[2]) for r in rows]
    return pick_vendor(vendors, vendor_name, from_address)
