"""Ghost Data Still Exists - deletion-audit agent.

Usage (from the repository root):
  python code/main.py                          # all requests in dataset/deletion_requests.csv -> output.csv
  python code/main.py --samples                # the 32 labelled samples -> output_samples.csv
  python code/main.py --cases case_05,case_11  # selected cases
  python code/main.py --custom                 # participant-built evaluation cases -> output_custom.csv
  python code/main.py --samples --limit 5 --run-id dev1
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8")

from ghost.config import REPO_ROOT, path as cfg_path  # noqa: E402
from ghost.contract import OUTPUT_COLUMNS  # noqa: E402
from ghost.data.loader import load_dataset  # noqa: E402
from ghost.llm import usage_summary  # noqa: E402
from ghost.orchestrator import run  # noqa: E402


def case_sort_key(cid: str):
    head, _, num = cid.rpartition("_")
    return (head, int(num)) if num.isdigit() else (cid, 0)


def write_output(rows: list[dict], out: Path):
    rows = sorted(rows, key=lambda r: case_sort_key(r["case_id"]))
    with open(out, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(OUTPUT_COLUMNS)
        for r in rows:
            w.writerow([r["case_id"], r["deletion_status"],
                        json.dumps(r["steps"], ensure_ascii=False, separators=(",", ":"))])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samples", action="store_true")
    ap.add_argument("--custom", action="store_true", help="participant-built cases in code/evaluation/custom_cases")
    ap.add_argument("--cases", help="comma-separated case ids")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--output")
    ap.add_argument("--run-id")
    ap.add_argument("--concurrency", type=int)
    a = ap.parse_args()

    ds = load_dataset()
    reqs = ds.custom_requests if a.custom else ds.sample_requests if a.samples else ds.requests
    ids = [r["case_id"] for r in reqs]
    if a.cases:
        wanted = a.cases.split(",")
        ids = [r["case_id"] for r in ds.sample_requests + ds.requests if r["case_id"] in wanted]
    if a.limit:
        ids = ids[: a.limit]
    run_id = a.run_id or time.strftime("run_%Y%m%d_%H%M%S")
    default = "output_custom.csv" if a.custom else "output_samples.csv" if (a.samples or a.cases) else "output.csv"
    out = Path(a.output) if a.output else REPO_ROOT / default

    t0 = time.time()
    rows = run(ids, run_id, a.concurrency)
    write_output(rows, out)
    ledger = cfg_path("cache") / "usage" / f"{run_id}.jsonl"
    print(f"\nwrote {len(rows)} rows to {out} in {time.time() - t0:.0f}s (run_id={run_id})")
    if ledger.exists():
        print(json.dumps(usage_summary(ledger), indent=1))


if __name__ == "__main__":
    main()
