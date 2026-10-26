"""The five agents. Each is one Claude role with its own prompt, model (config.yaml) and output schema."""
from __future__ import annotations

import base64
import json

from ghost.agents import schemas
from ghost.config import CODE_DIR
from ghost.data.facts import load_store
from ghost.llm import LLM
from ghost.tools.registry import TOOLS, dispatch_json

PROMPT_DIR = CODE_DIR / "prompts"


def prompt(name: str) -> str:
    return (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")


def _j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


# ------------------------------------------------------------------ 1. Intake
def intake(llm: LLM, req: dict, agent: str = "intake", first: dict | None = None,
           lexical: bool | None = None) -> dict:
    payload: dict = {"request_text": req["request_text"]}
    if agent == "intake_review":
        payload.update(first_classifier=first, keyword_check_says_deletion=lexical)
    return llm.run_agent(agent, system=prompt(agent), user=_j(payload), schema=schemas.INTAKE,
                         case_id=req["case_id"])["output"]


# ------------------------------------------------------------------ 2. Evidence extraction (vision)
def extract_image(llm: LLM, file_id: str, path, media_type: str) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    user = [{"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}},
            {"type": "text", "text": f"Artifact file_id={file_id}. Extract the facts."}]
    out = llm.run_agent("extraction", system=prompt("extraction"), user=user, schema=schemas.EXTRACTION,
                        case_id=None)["output"]
    records = []
    for r in out.get("records", []):
        rec = {k: v for k, v in r.items() if k != "details" and v}
        for d in r.get("details", []):
            if d.get("key") and d.get("value"):
                rec.setdefault(d["key"], d["value"])
        records.append(rec)
    return {**{k: v for k, v in out.items() if k != "records"}, "records": records}


# ------------------------------------------------------------------ 3. Investigator (tool use)
def _strict(tool: dict) -> dict:
    t = json.loads(json.dumps(tool))
    t["input_schema"]["additionalProperties"] = False
    t["strict"] = True
    return t


INVESTIGATOR_TOOLS = [_strict(t) for t in TOOLS if t["name"] != "get_case_dossier"]


def investigate(llm: LLM, req: dict, dossier: dict, agent: str = "investigator") -> dict:
    st = load_store()
    categories = list(st.categories) + [t for types in st.prefix_types.values() for t, _ in types]

    def handler(name: str, args: dict) -> str:
        if "as_of" in args:  # never let the agent look past the case's cut-off
            args = {**args, "as_of": req["audit_at"]}
        return dispatch_json(name, args)

    user = "Case dossier:\n" + _j(dossier)
    res = llm.run_agent(agent, system=prompt("investigator"), user=user,
                        schema=schemas.investigator(categories), tools=INVESTIGATOR_TOOLS,
                        tool_handler=handler, case_id=req["case_id"])
    return res["output"]


# ------------------------------------------------------------------ 4. Policy & Planner
def evidence_index(dossier: dict) -> dict:
    """Per resource: the citable evidence files and when they observed it (for choosing evidence_id)."""
    idx = {}
    for r in dossier["resources"]:
        obs = [{"evidence_id": o["evidence_id"], "observed_at": o["observed_at"]}
               for o in r["observations"] if o.get("citable")]
        obs += [{"evidence_id": a["evidence_id"], "observed_at": a["captured_at"], "kind": "absent_from_snapshot"}
                for a in r.get("absence_in_snapshots", [])]
        idx[r["resource_id"]] = {"system_id": r["system_id"], "citable_evidence": obs}
    return idx


def plan(llm: LLM, req: dict, dossier: dict, investigation: dict, review_issues: list | None = None,
         previous: dict | None = None, agent: str = "planner") -> dict:
    payload = {
        "request": {k: req[k] for k in ("case_id", "reported_status", "reported_at", "audit_at", "request_text")},
        "subject": dossier["subject"]["user_id"],
        "investigation": investigation,
        "evidence_index": evidence_index(dossier),
        "deletion_rules": dossier["deletion_rules"],
    }
    if review_issues:
        payload["previous_plan"] = previous
        payload["issues_to_fix"] = review_issues
    return llm.run_agent(agent, system=prompt("planner"), user=_j(payload), schema=schemas.PLANNER,
                         case_id=req["case_id"])["output"]


# ------------------------------------------------------------------ 5. Reviewer
def review(llm: LLM, req: dict, dossier: dict, investigation: dict, status: str, steps: list,
           validation_issues: list[str]) -> dict:
    payload = {"dossier": dossier, "investigation": investigation,
               "proposed": {"deletion_status": status, "steps": steps},
               "contract_validation_issues": validation_issues}
    return llm.run_agent("reviewer", system=prompt("reviewer"), user=_j(payload), schema=schemas.REVIEWER,
                         case_id=req["case_id"])["output"]
