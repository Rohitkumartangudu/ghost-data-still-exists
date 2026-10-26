"""JSON schemas for structured agent outputs. Enumerations come from the contract and the dataset, not literals."""
from __future__ import annotations

from ghost.contract import OPERATIONS, PHASES


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


STR = {"type": "string"}
BOOL = {"type": "boolean"}

INTAKE = _obj({
    "authorizes_deletion": BOOL,
    "request_type": {"type": "string", "enum": [
        "ACCOUNT_DELETION", "TARGETED_DELETION", "ACCESS_REQUEST", "DATA_PORTABILITY", "CORRECTION",
        "MARKETING_OPT_OUT", "ACCOUNT_SUPPORT", "BILLING_OR_DOCUMENT_REQUEST", "OTHER"]},
    "language": STR,
    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    "rationale": STR,
})

EXTRACTION = _obj({
    "screen_or_document_type": STR,
    "displayed_time": STR,
    "subject_identifiers": {"type": "array", "items": STR},
    "records": {"type": "array", "items": _obj({
        "resource_id": STR, "system": STR, "record_type": STR, "status": STR, "subject_reference": STR,
        "details": {"type": "array", "items": _obj({"key": STR, "value": STR})},
    })},
    "other_facts": {"type": "array", "items": STR},
})

RESOURCE_STATES = ["PRESENT_IDENTIFIABLE", "PRESENT_REDACTED", "PRESENT_ANONYMIZED", "ABSENT",
                   "FLOW_ENABLED", "FLOW_DISABLED", "RESTORE_ALLOWED", "RESTORE_BLOCKED", "UNKNOWN"]


def investigator(categories: list[str]) -> dict:
    return _obj({
        "subject_user_id": STR,
        "resources": {"type": "array", "items": _obj({
            "resource_id": STR,
            "system_id": STR,
            "belongs_to_subject": BOOL,
            "link_basis": STR,
            "data_category": {"type": "string", "enum": sorted(set(categories)) + ["UNKNOWN"]},
            "state_at_audit": {"type": "string", "enum": RESOURCE_STATES},
            "state_evidence_id": STR,
            "rationale": STR,
        })},
        "notes": STR,
    })


PLANNER = _obj({
    "deletion_status": {"type": "string", "enum": ["COMPLETE", "INCOMPLETE"]},
    "steps": {"type": "array", "items": _obj({
        "phase": {"type": "string", "enum": list(PHASES)},
        "system_id": STR,
        "resource_id": STR,
        "operation": {"type": "string", "enum": [op for _, op, _ in OPERATIONS]},
        "evidence_id": STR,
    })},
    "rationale": STR,
})

REVIEWER = _obj({
    "approved": BOOL,
    "issues": {"type": "array", "items": _obj({"resource_id": STR, "problem": STR, "fix": STR})},
    "summary": STR,
})
