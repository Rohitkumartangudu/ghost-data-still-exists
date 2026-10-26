"""Scorer: compare a predictions CSV against labelled answers (default: dataset/sample_output.csv).

Metrics (our definitions; the official grader's formula is not published):
  status_accuracy     share of cases whose deletion_status matches.
  step P/R/F1         multiset match of full steps (phase, system_id, resource_id, operation, state, evidence_id).
  step P/R/F1 (no ev) same, ignoring evidence_id.
  resource P/R/F1     multiset match of (system_id, resource_id) pairs mentioned in steps.
  field_accuracy      position-aligned comparison of each field (step_no..evidence_id); missing/extra steps count as wrong.
  sequence_accuracy   share of cases whose full ordered step list matches exactly.
  evidence_accuracy   among steps matched without evidence_id, share whose evidence_id also matches.
  contract_errors     rows violating the output contract (JSON, enums, op/state pairs, numbering, phase order).

Usage:
  python code/evaluation/score.py --pred output_samples.csv [--gold dataset/sample_output.csv]
                                  [--split all|dev|holdout] [--details]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from ghost.config import REPO_ROOT, dataset_path  # noqa: E402
from ghost.contract import OP_PHASE, OP_STATE, PHASE_RANK, STATUSES, STEP_FIELDS  # noqa: E402

KEY_FULL = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")
KEY_NOEV = KEY_FULL[:-1]


def read_rows(path: Path) -> dict[str, dict]:
    with open(path, encoding="utf-8", newline="") as fh:
        return {r["case_id"]: r for r in csv.DictReader(fh)}


def split_cases(gold: dict[str, dict]) -> dict[str, set[str]]:
    """Deterministic split stratified by status: within each status (case_id order) alternate dev / holdout."""
    dev, hold = set(), set()
    by_status: dict[str, list[str]] = {}
    for cid in sorted(gold, key=lambda c: int(c.rsplit("_", 1)[1])):
        by_status.setdefault(gold[cid]["deletion_status"], []).append(cid)
    for cids in by_status.values():
        for i, cid in enumerate(cids):
            (dev if i % 2 == 0 else hold).add(cid)
    return {"all": set(gold), "dev": dev, "holdout": hold}


def contract_errors(row: dict) -> list[str]:
    errs = []
    if row.get("deletion_status") not in STATUSES:
        errs.append(f"bad status {row.get('deletion_status')!r}")
    try:
        steps = json.loads(row.get("steps_json", ""))
    except json.JSONDecodeError as e:
        return errs + [f"invalid JSON: {e}"]
    if not isinstance(steps, list):
        return errs + ["steps_json is not an array"]
    if row.get("deletion_status") == "NOT_APPLICABLE" and steps:
        errs.append("NOT_APPLICABLE must have []")
    last_phase = -1
    for i, s in enumerate(steps, 1):
        if set(s) != set(STEP_FIELDS):
            errs.append(f"step {i}: fields {sorted(s)}")
            continue
        if s["step_no"] != i:
            errs.append(f"step {i}: step_no {s['step_no']}")
        op = s["operation"]
        if op not in OP_PHASE or OP_PHASE[op] != s["phase"] or OP_STATE[op] != s["state"]:
            errs.append(f"step {i}: invalid phase/operation/state {s['phase']}/{op}/{s['state']}")
        elif PHASE_RANK[s["phase"]] < last_phase:
            errs.append(f"step {i}: phase order")
        else:
            last_phase = PHASE_RANK[s["phase"]]
    return errs


def steps_of(row: dict | None) -> list[dict]:
    if not row:
        return []
    try:
        v = json.loads(row["steps_json"])
        return v if isinstance(v, list) else []
    except (json.JSONDecodeError, KeyError):
        return []


def _prf(tp: int, n_pred: int, n_gold: int) -> dict:
    p = tp / n_pred if n_pred else (1.0 if not n_gold else 0.0)
    r = tp / n_gold if n_gold else (1.0 if not n_pred else 0.0)
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(2 * p * r / (p + r), 4) if p + r else 0.0}


def _multiset_tp(a: list[tuple], b: list[tuple]) -> int:
    return sum((Counter(a) & Counter(b)).values())


def score(pred: dict[str, dict], gold: dict[str, dict], cases: set[str]) -> tuple[dict, list[dict]]:
    agg = Counter()
    field_hits, field_total = Counter(), Counter()
    details = []
    for cid in sorted(cases, key=lambda c: int(c.rsplit("_", 1)[1])):
        g, p = gold[cid], pred.get(cid)
        gs, ps = steps_of(g), steps_of(p)
        status_ok = bool(p) and p["deletion_status"] == g["deletion_status"]
        agg["cases"] += 1
        agg["status_ok"] += status_ok
        agg["missing_rows"] += p is None
        cerr = contract_errors(p) if p else ["missing row"]
        agg["contract_error_rows"] += bool(cerr)

        full_g = [tuple(s.get(k, "") for k in KEY_FULL) for s in gs]
        full_p = [tuple(s.get(k, "") for k in KEY_FULL) for s in ps]
        noev_g = [t[:-1] for t in full_g]
        noev_p = [t[:-1] for t in full_p]
        res_g = [(s.get("system_id"), s.get("resource_id")) for s in gs]
        res_p = [(s.get("system_id"), s.get("resource_id")) for s in ps]
        tp_full, tp_noev = _multiset_tp(full_p, full_g), _multiset_tp(noev_p, noev_g)
        agg["tp_full"] += tp_full
        agg["tp_noev"] += tp_noev
        agg["tp_res"] += _multiset_tp(list(set(res_p)), list(set(res_g)))
        agg["n_res_p"] += len(set(res_p))
        agg["n_res_g"] += len(set(res_g))
        agg["n_pred"] += len(ps)
        agg["n_gold"] += len(gs)
        seq_ok = status_ok and [tuple(s.get(k) for k in STEP_FIELDS) for s in ps] == \
            [tuple(s.get(k) for k in STEP_FIELDS) for s in gs]
        agg["seq_ok"] += seq_ok

        for i in range(max(len(gs), len(ps))):
            for k in STEP_FIELDS:
                field_total[k] += 1
                if i < len(gs) and i < len(ps) and gs[i].get(k) == ps[i].get(k):
                    field_hits[k] += 1

        details.append({
            "case_id": cid,
            "status": {"pred": p["deletion_status"] if p else None, "gold": g["deletion_status"]},
            "sequence_exact": seq_ok,
            "missing_steps": [dict(zip(KEY_FULL, t)) for t in (Counter(full_g) - Counter(full_p)).elements()],
            "extra_steps": [dict(zip(KEY_FULL, t)) for t in (Counter(full_p) - Counter(full_g)).elements()],
            "contract_errors": cerr,
        })

    n = agg["cases"] or 1
    metrics = {
        "cases": agg["cases"],
        "status_accuracy": round(agg["status_ok"] / n, 4),
        "sequence_accuracy": round(agg["seq_ok"] / n, 4),
        "step": _prf(agg["tp_full"], agg["n_pred"], agg["n_gold"]),
        "step_ignoring_evidence": _prf(agg["tp_noev"], agg["n_pred"], agg["n_gold"]),
        "resource": _prf(agg["tp_res"], agg["n_res_p"], agg["n_res_g"]),
        "evidence_accuracy": round(agg["tp_full"] / agg["tp_noev"], 4) if agg["tp_noev"] else 0.0,
        "field_accuracy": {k: round(field_hits[k] / field_total[k], 4) if field_total[k] else 1.0 for k in STEP_FIELDS},
        "contract_error_rows": agg["contract_error_rows"],
        "missing_rows": agg["missing_rows"],
    }
    return metrics, details


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pred", required=True)
    ap.add_argument("--gold", default=str(dataset_path("sample_output.csv")))
    ap.add_argument("--split", default="all", choices=["all", "dev", "holdout"])
    ap.add_argument("--details", action="store_true", help="print per-case differences")
    ap.add_argument("--json", help="write metrics + details to this JSON file")
    a = ap.parse_args()
    pred = read_rows(Path(a.pred) if Path(a.pred).is_absolute() else REPO_ROOT / a.pred)
    gold = read_rows(Path(a.gold))
    cases = split_cases(gold)[a.split]
    metrics, details = score(pred, gold, cases)
    print(json.dumps({"split": a.split, **metrics}, indent=1))
    if a.details:
        for d in details:
            if not d["sequence_exact"] or d["contract_errors"]:
                print(f"\n== {d['case_id']} status pred={d['status']['pred']} gold={d['status']['gold']}")
                if not (d["missing_steps"] or d["extra_steps"]) and d["status"]["pred"] == d["status"]["gold"]:
                    print("  ORDER   same steps, different order or step_no")
                for s in d["missing_steps"]:
                    print("  MISSING", s)
                for s in d["extra_steps"]:
                    print("  EXTRA  ", s)
                for e in d["contract_errors"]:
                    print("  CONTRACT", e)
    if a.json:
        Path(a.json).write_text(json.dumps({"metrics": metrics, "details": details}, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
