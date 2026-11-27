"""Pure rules for human decisions. Used by the graph (enforcement) and decisions.py (fail fast)."""

from typing import Any, Literal

PauseKind = Literal["human_approval", "human_review"]

APPROVAL_DECISIONS = frozenset({"approve", "reject", "hold"})
EXCEPTION_DECISIONS = frozenset({"resolve", "reject"})


def has_block_flag(state: dict[str, Any]) -> bool:
    flags = (state.get("anomaly_report") or {}).get("flags") or []
    return any(f.get("severity") == "block" for f in flags)


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def validate_decision(state: dict[str, Any], kind: PauseKind, decision: Any) -> str | None:
    """Return a human readable refusal reason, or None when the decision is acceptable."""
    if not isinstance(decision, dict):
        return "decision must be an object"
    action = decision.get("decision")
    allowed = APPROVAL_DECISIONS if kind == "human_approval" else EXCEPTION_DECISIONS
    if action not in allowed:
        return f"decision must be one of {sorted(allowed)}"
    if not str(decision.get("user") or "").strip():
        return "user is required"
    note = str(decision.get("note") or "").strip()
    if action == "reject" and not note:
        return "a note is required to reject"
    if action == "approve" and has_block_flag(state) and not note:
        return "this invoice has a block flag: an override reason (note) is required to approve"
    if action == "resolve":
        if not state.get("extraction"):
            return "extraction failed: nothing to re-validate, reject instead"
        vendor_id = _int_or_none(decision.get("vendor_id"))
        po_id = _int_or_none(decision.get("po_id"))
        if vendor_id is None and po_id is None:
            return "resolve needs a vendor_id and/or a po_id"
    return None
