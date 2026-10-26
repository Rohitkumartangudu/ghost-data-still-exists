"""Per-case pipeline: Intake -> Dossier -> Investigator -> Planner -> contract guardrail -> Reviewer -> (one revision)."""
from __future__ import annotations

import json
import mimetypes
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

from ghost.agents import core
from ghost.config import path as cfg_path, settings
from ghost.data import facts as facts_mod
from ghost.data import linker as linker_mod
from ghost.data.facts import load_store
from ghost.llm import LLM
from ghost.steps import evidence_times, normalize
from ghost.tools import dossier as dossier_mod


def ensure_image_extractions(llm: LLM) -> int:
    """Run the extraction agent once per image evidence file (cached on disk), then rebuild the fact store."""
    st = load_store()
    out_dir = cfg_path("cache") / "vision"
    out_dir.mkdir(parents=True, exist_ok=True)
    done = 0
    for fid, m in sorted(st.ds.manifest.items()):
        media = mimetypes.guess_type(m["file_path"])[0] or ""
        if not media.startswith("image/"):
            continue
        target = out_dir / f"{fid}.json"
        if not target.exists():
            payload = core.extract_image(llm, fid, st.ds.evidence_path(fid), media)
            target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            done += 1
    # Rebuild caches so image facts participate in linking.
    facts_mod.load_store.cache_clear()
    linker_mod.load_linker.cache_clear()
    linker_mod.global_profile_assignment.cache_clear()
    linker_mod.global_followup_assignment.cache_clear()
    return done


def lexical_deletion(text: str) -> bool:
    t = text.casefold()
    return any(stem.casefold() in t for stem in settings()["intake_lexicon"])


def process_case(llm: LLM, case_id: str) -> tuple[dict, dict]:
    st = load_store()
    req = st.ds.request(case_id)
    trace: dict = {"case_id": case_id}

    # 1. Intake (+ second opinion on disagreement with the lexical check or low confidence)
    it = core.intake(llm, req)
    trace["intake"] = it
    lex = lexical_deletion(req["request_text"])
    if it["authorizes_deletion"] != lex or it["confidence"] == "low":
        it2 = core.intake(llm, req, agent="intake_review", first=it, lexical=lex)
        trace["intake_review"] = {"lexical": lex, **it2}
        it = it2
    if not it["authorizes_deletion"]:
        return {"case_id": case_id, "deletion_status": "NOT_APPLICABLE", "steps": []}, trace

    # 2. Dossier (deterministic tools)
    dossier = dossier_mod.build_dossier(case_id)

    # 3. Investigator
    inv = core.investigate(llm, req, dossier)
    trace["investigation"] = inv

    # 4. Planner + guardrail
    known = st.resources | {r["resource_id"] for r in dossier["resources"]} |         {t for d in st.documents for t in st.resource_refs(d)}

    def mentions(ev: str, rid: str):
        """Does evidence `ev` observe resource `rid`? Activity rows whose type is a remediation operation are claims."""
        from ghost.contract import OP_PHASE
        if ev in st.ds.manifest:
            return any(f.evidence_id == ev for f in st.by_resource.get(rid, [])) or                 any(d.evidence_id == ev and rid in d.tokens for d in st.documents)
        acts = [f for f in st.activity_facts if f.evidence_id == ev]
        if acts:
            a = acts[0]
            if OP_PHASE.get(a.fields["event_type"]) == "REMEDIATION":
                return False
            return a.resource_id == rid or rid in a.tokens
        return None

    def guard(p: dict):
        return normalize(p["deletion_status"], p["steps"], audit_at=req["audit_at"],
                         evidence=evidence_times(st.ds), known_resources=known, mentions=mentions)

    pl = core.plan(llm, req, dossier, inv)
    steps, issues = guard(pl)
    trace["plan"] = {**pl, "validation_issues": issues}

    # 5. Reviewer, then at most N revision rounds
    review_mode = settings()["llm"].get("review_mode", "always")
    rounds = settings()["llm"]["max_review_rounds"] if (review_mode == "always" or issues) else 0
    for _ in range(rounds):
        rv = core.review(llm, req, dossier, inv, pl["deletion_status"], steps, issues)
        trace.setdefault("reviews", []).append(rv)
        if rv["approved"] and not issues:
            break
        todo = rv["issues"] + [{"resource_id": "", "problem": i, "fix": "resolve contract issue"} for i in issues]
        revised = core.plan(llm, req, dossier, inv, review_issues=todo,
                            previous={"deletion_status": pl["deletion_status"], "steps": steps})
        r_steps, r_issues = guard(revised)
        trace.setdefault("revisions", []).append({**revised, "validation_issues": r_issues})
        if not r_steps and steps:  # a revision that removes every step is not a valid plan; keep the previous one
            trace["revision_rejected"] = "empty step list"
            break
        pl, steps, issues = revised, r_steps, r_issues

    # Escalation: if the plan still violates the contract after review, redo investigation + planning once
    # with the stronger model (config models.*_escalated). Rare by design; keeps cost low.
    if issues and settings()["models"].get("planner_escalated"):
        e_inv = core.investigate(llm, req, dossier, agent="investigator_escalated")
        e_pl = core.plan(llm, req, dossier, e_inv, agent="planner_escalated")
        e_steps, e_issues = guard(e_pl)
        accepted = bool(e_steps) and len(e_issues) <= len(issues)
        trace["escalation"] = {"investigation": e_inv, "plan": e_pl, "validation_issues": e_issues,
                               "accepted": accepted}
        if accepted:  # only replace the plan when the stronger model's plan is not worse
            inv, pl, steps, issues = e_inv, e_pl, e_steps, e_issues

    # Contract consistency: an INCOMPLETE case must report at least one evidenced violation. Without one,
    # every listed resource is compliant, so the case is COMPLETE; COMPLETE verifications need evidence,
    # taken from the investigator's cited state evidence when the planner left it empty.
    status = pl["deletion_status"]
    if status == "INCOMPLETE" and not any(s["phase"] == "FINDING" for s in steps):
        status = "COMPLETE"
        trace["consistency_override"] = "INCOMPLETE without findings -> COMPLETE"
    if status == "COMPLETE":
        state_ev = {r["resource_id"]: r.get("state_evidence_id", "") for r in inv.get("resources", [])}
        filled = [{**s, "evidence_id": s["evidence_id"] or state_ev.get(s["resource_id"], "")}
                  for s in steps if s["phase"] == "VERIFICATION"]
        steps, issues = guard({"deletion_status": status, "steps": filled})
    trace["final_issues"] = issues
    return {"case_id": case_id, "deletion_status": status, "steps": steps}, trace


def run(case_ids: list[str], run_id: str, concurrency: int | None = None, log=print) -> list[dict]:
    llm = LLM(run_id)
    n = ensure_image_extractions(llm)
    if n:
        log(f"extracted {n} image evidence files")
    # Warm deterministic caches once before threading.
    linker_mod.global_profile_assignment()
    linker_mod.global_followup_assignment()
    trace_dir = cfg_path("cache") / "traces" / run_id
    trace_dir.mkdir(parents=True, exist_ok=True)
    rows: dict[str, dict] = {}
    workers = concurrency or settings()["llm"]["concurrency"]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(process_case, llm, cid): cid for cid in case_ids}
        for i, fut in enumerate(as_completed(futs), 1):
            cid = futs[fut]
            try:
                row, trace = fut.result()
            except Exception as e:  # a failed case must still produce a valid row
                trace = {"case_id": cid, "error": repr(e), "traceback": traceback.format_exc()}
                row = {"case_id": cid, "deletion_status": "INCOMPLETE", "steps": [], "error": repr(e)}
            rows[cid] = row
            (trace_dir / f"{cid}.json").write_text(json.dumps(trace, ensure_ascii=False, indent=1, default=str),
                                                   encoding="utf-8")
            log(f"[{i}/{len(case_ids)}] {cid} -> {row['deletion_status']} ({len(row['steps'])} steps)"
                + (f" ERROR {row['error']}" if row.get("error") else ""))
    # Retry failed cases sequentially; every completed LLM call is reused from the disk cache.
    for attempt in range(settings()["llm"].get("case_retries", 0)):
        failed = [c for c in case_ids if rows[c].get("error")]
        if not failed:
            break
        log(f"retry {attempt + 1}: {len(failed)} failed case(s)")
        for cid in failed:
            try:
                row, trace = process_case(llm, cid)
            except Exception as e:
                trace = {"case_id": cid, "error": repr(e), "traceback": traceback.format_exc()}
                row = {"case_id": cid, "deletion_status": "INCOMPLETE", "steps": [], "error": repr(e)}
            rows[cid] = row
            (trace_dir / f"{cid}.json").write_text(json.dumps(trace, ensure_ascii=False, indent=1, default=str),
                                                   encoding="utf-8")
            log(f"  {cid} -> {row['deletion_status']}" + (f" ERROR {row['error']}" if row.get("error") else ""))
    still = [c for c in case_ids if rows[c].get("error")]
    if still:
        log(f"WARNING: {len(still)} case(s) still failed: {still}")
    return [rows[c] for c in case_ids]
