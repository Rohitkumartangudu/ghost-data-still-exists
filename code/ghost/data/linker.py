"""Link resources to a deletion subject. Grounded links first; workflow-job timing only as candidates."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from functools import lru_cache

from ghost.config import settings
from ghost.contract import OP_PHASE
from ghost.data.facts import FactStore, load_store, prefix_of
from ghost.data.identity import IdentityIndex, Subject


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _minutes(a: str, b: str) -> int:
    return int((_t(a) - _t(b)).total_seconds() // 60)


class Linker:
    def __init__(self, store: FactStore):
        self.store = store
        self.ids = IdentityIndex(store.ds)
        self.user_ids = set(self.ids.current)
        # Prefixes whose observed record type is the primary profile category of the rules table.
        self.profile_prefixes = {p for p, types in store.prefix_types.items()
                                 if types and types[0][0] == self._profile_category()}
        self._owners = self._resource_owners()
        self.file_subjects: dict[str, int] = defaultdict(int)  # evidence file -> number of distinct people in it
        per_file: dict[str, set] = defaultdict(set)
        for f in store.facts:
            per_file[f.evidence_id] |= self._owners.get(f.resource_id, set())
        for fid, us in per_file.items():
            self.file_subjects[fid] = max(1, len(us))

    def _profile_category(self) -> str:
        cat = settings()["linking"]["primary_record_category"]
        assert cat in self.store.categories, f"{cat} not in deletion_rules.csv"
        return cat

    # ------------------------------------------------------------ ownership
    def _resource_owners(self) -> dict[str, set[str]]:
        """resource_id -> user_ids whose strong identifiers appear in any fact about that resource."""
        owners: dict[str, set[str]] = defaultdict(set)
        for f in self.store.facts + self.store.activity_facts:
            for tok in f.tokens:
                owners[f.resource_id] |= self.ids.users_for(tok, strong_only=True)
        return owners

    def owners(self, rid: str) -> set[str]:
        return self._owners.get(rid, set())

    # ------------------------------------------------------------ grounded links
    def grounded(self, subj: Subject, as_of: str) -> dict[str, list[dict]]:
        """resource_id -> list of link reasons, using only facts visible at as_of."""
        links: dict[str, list[dict]] = defaultdict(list)
        strong = subj.strong_tokens
        for tok in strong:
            for f in self.store.by_token.get(tok, []):
                if not self.store.visible(f, as_of):
                    continue
                kind = subj.identifiers[tok]["kind"]
                if f.resource_id:
                    links[f.resource_id].append({"via": kind, "value": tok, "evidence_id": f.evidence_id})
                # Explicit catalogue links / repopulation messages name further resources.
                for ref in self.store.resource_refs(f):
                    if ref not in self.user_ids:
                        links[ref].append({"via": f"reference_in:{f.evidence_id}", "value": tok,
                                           "evidence_id": f.evidence_id})
        # One hop: records that reference a grounded record (e.g. index document -> source profile).
        for rid in list(links):
            for f in self.store.by_token.get(rid, []):
                if f.resource_id and f.resource_id != rid and self.store.visible(f, as_of) \
                        and not (self.owners(f.resource_id) - {subj.user_id}):
                    links[f.resource_id].append({"via": f"references:{rid}", "evidence_id": f.evidence_id})
        return dict(links)

    def evidence_window_hits(self, rid: str, start: str, end: str) -> list[str]:
        """Evidence files that observed the resource between the request and the audit cut-off."""
        return sorted({f.evidence_id for f in self.store.by_resource.get(rid, []) if start <= f.observed_at <= end})

    # ------------------------------------------------------------ workflow-job candidates
    def job_candidates(self, req: dict, subj: Subject) -> dict:
        lo, hi = settings()["linking"]["job_account_window"]
        flo, fhi = settings()["linking"]["job_followup_window"]
        rep = req["reported_at"]
        accounts, followups = [], []
        grounded = self.grounded(subj, req["audit_at"])
        subj_files = {f.evidence_id for r in grounded for f in self.store.by_resource.get(r, [])
                      if f.observed_at <= req["audit_at"]}
        for f in self.store.activity_facts:
            a = f.fields
            if OP_PHASE.get(a["event_type"]) != "REMEDIATION":
                continue
            off = _minutes(a["event_time"], rep)
            others = sorted(self.owners(a["resource_id"]) - {subj.user_id})
            seen = self.evidence_window_hits(a["resource_id"], req["requested_at"], req["audit_at"])
            item = {"activity_id": a["activity_id"], "resource_id": a["resource_id"], "system": a["target_system"],
                    "event_type": a["event_type"], "reported_result": a["reported_result"],
                    "offset_min_vs_reported_at": off, "claimed_by_other_users": others,
                    "linked_to_subject": subj.user_id in self.owners(a["resource_id"]),
                    "evidence_between_request_and_audit": seen,
                    "shares_evidence_file_with_subject_records": sorted(
                        {f.evidence_id for f in self.store.by_resource.get(a["resource_id"], [])} & subj_files)}
            item["cooccurrence_weight"] = round(sum(1 / self.file_subjects[e]
                                                    for e in item["shares_evidence_file_with_subject_records"]), 3)
            if prefix_of(a["resource_id"]) in self.profile_prefixes and lo <= off <= hi:
                accounts.append(item)
            elif flo <= off <= fhi and not prefix_of(a["resource_id"]) in self.profile_prefixes:
                followups.append(item)
        return {"profile_deletion_candidates": accounts, "followup_actions": followups}


@lru_cache(maxsize=1)
def load_linker() -> Linker:
    return Linker(load_store())


def candidate_score(c: dict) -> float:
    """Preference for a primary-profile deletion candidate (higher is better)."""
    if c["claimed_by_other_users"] and not c["linked_to_subject"]:
        return float("-inf")
    return (1000 * c["linked_to_subject"] + 100 * c["cooccurrence_weight"]
            + 10 * bool(c["evidence_between_request_and_audit"]) - abs(c["offset_min_vs_reported_at"] + 60) / 100)


@lru_cache(maxsize=1)
def global_profile_assignment() -> dict[str, str]:
    """Greedy best-score one-to-one assignment of primary-profile deletions to requests across the whole
    dataset (every request needs its own profile). Returns case_id -> resource_id."""
    lk = load_linker()
    ds = lk.store.ds
    edges = []
    for req in ds.sample_requests + ds.requests:
        subj = lk.ids.resolve(req["email"], req["audit_at"])
        for c in lk.job_candidates(req, subj)["profile_deletion_candidates"]:
            sc = candidate_score(c)
            if sc != float("-inf"):
                edges.append((-sc, req["case_id"], c["resource_id"]))
    out, taken = {}, set()
    for _, cid, rid in sorted(edges):
        if cid not in out and rid not in taken:
            out[cid] = rid
            taken.add(rid)
    return out


@lru_cache(maxsize=1)
def global_followup_assignment() -> dict[str, list[dict]]:
    """Assign unowned follow-up remediation actions (e.g. anonymised records that no longer carry identifiers)
    to the single request whose workflow job best explains them. Ties stay unassigned. case_id -> [resource_id]."""
    lk = load_linker()
    ds = lk.store.ds
    best: dict[str, list[tuple[float, str]]] = defaultdict(list)
    grounded_prefixes: dict[str, set[str]] = {}
    for req in ds.sample_requests + ds.requests:
        subj = lk.ids.resolve(req["email"], req["audit_at"])
        grounded_prefixes[req["case_id"]] = {prefix_of(r) for r in lk.grounded(subj, req["audit_at"])}
        for c in lk.job_candidates(req, subj)["followup_actions"]:
            if c["claimed_by_other_users"] or c["linked_to_subject"]:
                continue
            sc = round(100 * c["cooccurrence_weight"] + 10 * bool(c["evidence_between_request_and_audit"]), 3)
            if sc > 0:
                best[c["resource_id"]].append((sc, req["case_id"]))
    out: dict[str, list[dict]] = defaultdict(list)
    for rid, opts in best.items():
        # A request whose subject already has its own grounded record of this kind is a weaker owner.
        opts = [(sc - (50 if prefix_of(rid) in grounded_prefixes.get(c, set()) else 0), c) for sc, c in opts]
        opts.sort(reverse=True)
        top = [c for sc, c in opts if sc == opts[0][0]]
        for cid in top:
            out[cid].append({"resource_id": rid, "exclusive": len(top) == 1,
                             "competing_cases": [c for c in top if c != cid]})
    return dict(out)
