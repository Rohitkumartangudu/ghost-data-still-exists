"""Load the participant-facing CSV files from dataset/."""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from ghost.config import dataset_path


def read_csv(name: str) -> list[dict]:
    with open(dataset_path(name), encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


@dataclass
class Dataset:
    requests: list[dict]
    sample_requests: list[dict]
    sample_output: list[dict]
    identities: list[dict]
    audit_log: list[dict]
    activity: list[dict]
    rules: list[dict]
    manifest: dict[str, dict] = field(default_factory=dict)
    custom_requests: list[dict] = field(default_factory=list)  # participant-built evaluation cases

    def evidence_path(self, file_id: str) -> Path:
        return dataset_path(self.manifest[file_id]["file_path"])

    def request(self, case_id: str) -> dict:
        for r in self.requests + self.sample_requests + self.custom_requests:
            if r["case_id"] == case_id:
                return r
        raise KeyError(case_id)


@lru_cache(maxsize=1)
def load_dataset() -> Dataset:
    return Dataset(
        requests=read_csv("deletion_requests.csv"),
        sample_requests=read_csv("sample_deletion_requests.csv"),
        sample_output=read_csv("sample_output.csv"),
        identities=read_csv("user_identities.csv"),
        audit_log=read_csv("user_audit_log.csv"),
        activity=read_csv("system_activity.csv"),
        rules=read_csv("deletion_rules.csv"),
        manifest={r["file_id"]: r for r in read_csv("evidence_files.csv")},
        custom_requests=_custom_requests(),
    )


def _custom_requests() -> list[dict]:
    from ghost.config import CODE_DIR
    f = CODE_DIR / "evaluation" / "custom_cases" / "requests.csv"
    if not f.exists():
        return []
    with open(f, encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))
