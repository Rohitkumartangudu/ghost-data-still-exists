"""Case dossier: everything the agents need for one case, pre-assembled from the deterministic tools."""
from __future__ import annotations

import json

from ghost.data.facts import prefix_of
from ghost.data.linker import (candidate_score, global_followup_assignment, global_profile_assignment,
                               load_linker)

MAX_OBS_PER_RESOURCE = 6


def _dedupe_obs(obs: list[dict]) -> list[dict]:
    seen, out = set(), []
    for o in obs:
        key = (o["evidence_id"], json.dumps(o["fields"], sort_keys=True, default=str))
        if key not in seen:
            seen.add(key)
            out.append(o)
    return out


def resource_view(rid: str, as_of: str, link_reasons: list[dict] | None = None, note: str | None = None,
                  subject_user: str | None = None) -> dict:
    lk = load_linker()
    st = lk.store
    obs = []
    for f in sorted(st.by_resource.get(rid, []), key=lambda f: f.observed_at):
        if not st.visible(f, as_of):
            continue
        d = f.compact()
        d["citable"] = st.citable(f, as_of)
        obs.append(d)
    obs = _dedupe_obs(obs)[-MAX_OBS_PER_RESOURCE:]
    acts = [{"activity_id": f.fields["activity_id"], "time": f.observed_at, "event_type": f.fields["event_type"],
             "source": f.fields["source_system"], "target": f.fields["target_system"],
             "reported_result": f.fields["reported_result"], "details": f.fields["details"]}
            for f in sorted(st.activity_by_resource.get(rid, []), key=lambda f: f.observed_at)
            if st.visible(f, as_of)]
    # Activity rows elsewhere that mention this resource in their details (e.g. "repopulated downstream resource X").
    mentions = [{"activity_id": f.fields["activity_id"], "time": f.observed_at, "event_type": f.fields["event_type"],
                 "resource_id": f.resource_id, "details": f.fields["details"]}
                for f in st.by_token.get(rid, []) if f.kind == "activity" and f.resource_id != rid
                and st.visible(f, as_of)]
    reasons = []
    for r in link_reasons or []:
        if r not in reasons:
            reasons.append(r)
    view = {
        "resource_id": rid,
        "system_id": st.resource_system(rid),
        "observed_record_types_for_prefix": st.prefix_types.get(prefix_of(rid), [])[:3],
        "other_users_linked": sorted(lk.owners(rid) - {subject_user}),
        "link_reasons": reasons[:6],
        "observations": obs,
        "activity": acts,
        "activity_mentions": mentions,
        "absence_in_snapshots": [a for a in st.absence_snapshots(rid, as_of)][-2:],
        "replica_drift": st.replica_drift(rid, as_of),
    }
    if note:
        view["note"] = note
    return view


def build_dossier(case_id: str) -> dict:
    lk = load_linker()
    st = lk.store
    req = st.ds.request(case_id)
    as_of = req["audit_at"]
    subj = lk.ids.resolve(req["email"], as_of)
    grounded = lk.grounded(subj, as_of)
    resources: dict[str, dict] = {rid: {"reasons": rs} for rid, rs in grounded.items()}

    jobs = lk.job_candidates(req, subj)
    assigned = global_profile_assignment().get(case_id)
    if assigned and assigned not in resources:
        resources[assigned] = {"reasons": [{"via": "workflow_job_assignment"}],
                               "note": "Linked only by workflow-job timing and global one-to-one assignment; "
                                       "no identifier in evidence."}
    for fu in global_followup_assignment().get(case_id, []):
        if fu["resource_id"] not in resources:
            resources[fu["resource_id"]] = {
                "reasons": [{"via": "workflow_job_followup"}],
                "note": "Follow-up action of a workflow job near reported_at; carries no identifier." +
                        ("" if fu["exclusive"] else f" AMBIGUOUS: equally consistent with {fu['competing_cases']}."),
            }

    docs = []
    for d in st.documents:
        if st.visible(d, as_of) and d.tokens & subj.strong_tokens:
            docs.append({"evidence_id": d.evidence_id, "kind": d.kind, "citable": st.citable(d, as_of),
                         "content": d.fields})

    profile_cands = sorted(jobs["profile_deletion_candidates"], key=candidate_score, reverse=True)[:6]
    for c in profile_cands:
        c["globally_assigned_to"] = next((k for k, v in global_profile_assignment().items()
                                          if v == c["resource_id"]), None)
    late = sorted({f.evidence_id for rid in resources for f in st.by_resource.get(rid, [])
                   if not st.citable(f, as_of)})
    return {
        "request": req,
        "subject": subj.to_dict(),
        "resources": [resource_view(rid, as_of, meta["reasons"], meta.get("note"), subj.user_id)
                      for rid, meta in sorted(resources.items())],
        "documents": docs,
        "workflow_job": {"profile_deletion_candidates": profile_cands,
                         "followup_actions": jobs["followup_actions"][:15]},
        "not_citable_files_seen": late,
        "deletion_rules": st.ds.rules,
    }
