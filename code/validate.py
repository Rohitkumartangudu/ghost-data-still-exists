"""Submission validator: checks output.csv against the README "Before submitting" checklist and the
problem-statement ordering rules.

Usage: python code/validate.py [output.csv]
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

from ghost.config import REPO_ROOT  # noqa: E402
from ghost.contract import OP_PHASE, OP_RANK, OP_STATE, OUTPUT_COLUMNS, PHASE_RANK, STATUSES, STEP_FIELDS  # noqa: E402
from ghost.data.loader import load_dataset  # noqa: E402
from ghost.steps import evidence_times  # noqa: E402

SECRET_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}")


def validate(path: Path) -> list[str]:
    ds = load_dataset()
    ev_time = evidence_times(ds)
    audit = {r["case_id"]: r["audit_at"] for r in ds.requests}
    expected = [r["case_id"] for r in ds.requests]
    errs: list[str] = []
    raw = path.read_text(encoding="utf-8")
    if SECRET_RE.search(raw):
        errs.append("output contains something that looks like an API key")
    with open(path, encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        rows = list(reader)
    if header != OUTPUT_COLUMNS:
        errs.append(f"header must be exactly {','.join(OUTPUT_COLUMNS)} (got {header})")
    ids = [r[0] for r in rows if r]
    if len(rows) != len(expected):
        errs.append(f"expected {len(expected)} data rows, found {len(rows)}")
    if sorted(set(ids)) != sorted(expected) or len(set(ids)) != len(ids):
        errs.append("case_ids do not match deletion_requests.csv one-to-one")
    if ids != expected:
        errs.append("rows are not in the case_id order of deletion_requests.csv")

    for r in rows:
        if len(r) != 3:
            errs.append(f"row with {len(r)} columns: {r[:1]}")
            continue
        cid, status, sj = r
        p = f"{cid}:"
        if status not in STATUSES:
            errs.append(f"{p} invalid deletion_status {status!r}")
        try:
            steps = json.loads(sj)
        except json.JSONDecodeError as e:
            errs.append(f"{p} steps_json is not valid JSON ({e})")
            continue
        if not isinstance(steps, list):
            errs.append(f"{p} steps_json is not an array")
            continue
        if status == "NOT_APPLICABLE" and steps:
            errs.append(f"{p} NOT_APPLICABLE must use []")
        if status in ("COMPLETE", "INCOMPLETE") and not steps:
            errs.append(f"{p} {status} with no steps")
        prev = None
        for i, s in enumerate(steps, 1):
            if not isinstance(s, dict) or set(s) != set(STEP_FIELDS):
                errs.append(f"{p} step {i} must have exactly the fields {STEP_FIELDS}")
                continue
            if s["step_no"] != i:
                errs.append(f"{p} step numbers must be consecutive from 1 (step {i} has {s['step_no']})")
            op = s["operation"]
            if op not in OP_PHASE or OP_PHASE[op] != s["phase"] or OP_STATE[op] != s["state"]:
                errs.append(f"{p} step {i} invalid phase/operation/state {s['phase']}/{op}/{s['state']}")
                continue
            key = (PHASE_RANK[s["phase"]], OP_RANK[op], s["system_id"], s["resource_id"])
            if prev and key < prev:
                errs.append(f"{p} step {i} breaks the required order (phase, operation, system_id, resource_id)")
            prev = key
            ev = s["evidence_id"]
            future = status == "INCOMPLETE" and s["phase"] in ("REMEDIATION", "VERIFICATION")
            if future and ev != "":
                errs.append(f"{p} step {i} proposed remediation/future verification must use evidence_id \"\"")
            if status == "COMPLETE" and s["phase"] != "VERIFICATION":
                errs.append(f"{p} step {i} COMPLETE cases contain only verification steps")
            if s["phase"] == "FINDING" or status == "COMPLETE":
                if ev not in ev_time:
                    errs.append(f"{p} step {i} cites unknown evidence {ev!r}")
                elif cid in audit and ev_time[ev] > audit[cid]:
                    errs.append(f"{p} step {i} cites evidence {ev} from after audit_at")
    return errs


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "output.csv"
    problems = validate(target)
    for e in problems[:200]:
        print("ERROR", e)
    print(f"{target.name}: {'VALID' if not problems else f'{len(problems)} problem(s)'}")
    sys.exit(1 if problems else 0)
