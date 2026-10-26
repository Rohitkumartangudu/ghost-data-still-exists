"""Contract guardrail: normalise agent-proposed steps into the exact output contract and report violations."""
from __future__ import annotations

from ghost.contract import OP_PHASE, OP_RANK, OP_STATE, PHASE_RANK


def evidence_times(ds) -> dict[str, str]:
    """Every citable evidence reference allowed by the README (file_id, activity_id, audit_id) -> its time."""
    t = {fid: m["captured_at"] for fid, m in ds.manifest.items()}
    t.update({a["activity_id"]: a["event_time"] for a in ds.activity})
    t.update({a["audit_id"]: a["event_time"] for a in ds.audit_log})
    return t


def normalize(status: str, raw_steps: list[dict], *, audit_at: str, evidence: dict[str, str],
              known_resources: set[str], mentions=None) -> tuple[list[dict], list[str]]:
    """mentions(evidence_id, resource_id) -> True/False/None: whether the cited evidence observes the resource
    as an observation (not a workflow claim). None means unknown (not checked)."""
    """Return (steps, issues). Deterministic: dedupe, derive phase/state from the operation, apply evidence
    rules, sort by phase -> operation table order -> system_id -> resource_id, renumber from 1."""
    issues: list[str] = []
    seen, steps = set(), []
    for s in raw_steps:
        op = s.get("operation")
        if op not in OP_PHASE:
            issues.append(f"unknown operation {op!r} dropped")
            continue
        phase = OP_PHASE[op]
        if s.get("phase") and s["phase"] != phase:
            issues.append(f"{s.get('resource_id')}: phase {s['phase']} does not match {op}; corrected to {phase}")
        rid, sys_id = (s.get("resource_id") or "").strip(), (s.get("system_id") or "").strip()
        if not rid or not sys_id:
            issues.append(f"step without resource_id/system_id dropped: {s}")
            continue
        if rid not in known_resources:
            issues.append(f"{rid}: resource id not found in any participant data; step dropped (never invent ids)")
            continue
        ev = (s.get("evidence_id") or "").strip()
        key = (phase, sys_id, rid, op)
        if key in seen:
            continue
        seen.add(key)
        steps.append({"phase": phase, "system_id": sys_id, "resource_id": rid, "operation": op,
                      "state": OP_STATE[op], "evidence_id": ev})

    def citable(ev: str) -> bool:
        return ev in evidence and evidence[ev] <= audit_at

    if status == "COMPLETE":
        kept = []
        for s in steps:
            if s["phase"] != "VERIFICATION":
                issues.append(f"{s['resource_id']}: COMPLETE case contains {s['operation']}; removed")
                continue
            if not citable(s["evidence_id"]):
                issues.append(f"{s['resource_id']}: COMPLETE verification needs citable evidence "
                              f"(got {s['evidence_id']!r})")
            kept.append(s)
        steps = kept
        if not steps:
            issues.append("COMPLETE case has no verification steps")
    else:
        for s in steps:
            if s["phase"] == "FINDING":
                if not citable(s["evidence_id"]):
                    issues.append(f"{s['resource_id']}: finding needs citable evidence (got {s['evidence_id']!r})")
                elif mentions and mentions(s["evidence_id"], s["resource_id"]) is False:
                    issues.append(f"{s['resource_id']}: cited evidence {s['evidence_id']} does not observe this resource "
                                  f"(or is only a workflow claim); cite evidence that shows the violation")
            elif s["evidence_id"]:
                s["evidence_id"] = ""  # remediation and post-remediation checks describe future work
        if not any(s["phase"] == "FINDING" for s in steps):
            issues.append("INCOMPLETE case has no findings")
        fixed = {s["resource_id"] for s in steps if s["phase"] == "REMEDIATION"}
        found = {s["resource_id"] for s in steps if s["phase"] == "FINDING"}
        verified = {s["resource_id"] for s in steps if s["phase"] == "VERIFICATION"}
        for rid in sorted(found - fixed):
            issues.append(f"{rid}: finding without remediation")
        for rid in sorted((found | fixed) - verified):
            issues.append(f"{rid}: remediated resource without verification")

    steps.sort(key=lambda s: (PHASE_RANK[s["phase"]], OP_RANK[s["operation"]], s["system_id"], s["resource_id"]))
    out = [{"step_no": i, **s} for i, s in enumerate(steps, 1)]
    return out, list(dict.fromkeys(issues))
