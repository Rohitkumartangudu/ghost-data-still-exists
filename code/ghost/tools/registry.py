"""Tool definitions (Claude tool-use schema) and handlers exposed to the agents. All read-only."""
from __future__ import annotations

import json
import re
import sqlite3

from ghost.data.linker import load_linker
from ghost.data.normalize import email_hash, norm_email, norm_phone, tokens
from ghost.tools.dossier import build_dossier, resource_view

MAX_RESULTS = 25

TOOLS = [
    {
        "name": "get_case_dossier",
        "description": "Pre-assembled investigation dossier for a case: request, resolved identity (current and "
                       "historical identifiers), linked resources with observations (each marked citable or not), "
                       "activity, absence-in-snapshot evidence, workflow-job candidates, documents, and the rules.",
        "input_schema": {"type": "object", "properties": {"case_id": {"type": "string"}}, "required": ["case_id"]},
    },
    {
        "name": "search_evidence",
        "description": "Find facts (evidence records, activity rows, documents) mentioning an identifier (email, "
                       "phone, email hash, user id) or a resource id, observed at or before as_of.",
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string"}, "as_of": {"type": "string", "description": "ISO-8601 cut-off (audit_at)"}},
            "required": ["query", "as_of"]},
    },
    {
        "name": "get_resource_history",
        "description": "All observations, activity and absence evidence for one resource up to as_of.",
        "input_schema": {"type": "object", "properties": {
            "resource_id": {"type": "string"}, "as_of": {"type": "string"}}, "required": ["resource_id", "as_of"]},
    },
    {
        "name": "query_snapshot",
        "description": "Run a read-only SQL SELECT against one SQL snapshot evidence file (SQLite dialect).",
        "input_schema": {"type": "object", "properties": {
            "file_id": {"type": "string"}, "sql": {"type": "string"}}, "required": ["file_id", "sql"]},
    },
    {
        "name": "lookup_identifier_owner",
        "description": "Which users have ever held an identifier (email, phone, email hash, user id).",
        "input_schema": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
    },
    {
        "name": "get_evidence_file_info",
        "description": "Manifest entry for an evidence file: path, origin, captured_at.",
        "input_schema": {"type": "object", "properties": {"file_id": {"type": "string"}}, "required": ["file_id"]},
    },
]


def _search(query: str, as_of: str) -> list[dict]:
    lk = load_linker()
    st = lk.store
    q = query.strip()
    keys = tokens(q) | {q.lower()}
    if "@" in q:
        keys.add(email_hash(norm_email(q)))
    if q.startswith("+"):
        keys.add(norm_phone(q))
    out, seen = [], set()
    for k in keys:
        for f in st.by_token.get(k, []) + st.by_resource.get(k, []) + st.activity_by_resource.get(k, []):
            if id(f) in seen or not st.visible(f, as_of):
                continue
            seen.add(id(f))
            d = f.compact()
            d["citable"] = st.citable(f, as_of)
            out.append(d)
    return sorted(out, key=lambda d: d["observed_at"])[-MAX_RESULTS:]


def _query_snapshot(file_id: str, sql: str):
    if not re.match(r"^\s*select\b", sql, re.I) or ";" in sql.strip().rstrip(";"):
        return {"error": "only a single SELECT statement is allowed"}
    lk = load_linker()
    path = lk.store.ds.evidence_path(file_id)
    if not str(path).endswith(".sql"):
        return {"error": "not a SQL snapshot"}
    con = sqlite3.connect(":memory:")
    con.executescript(path.read_text(encoding="utf-8"))
    try:
        cur = con.execute(sql)
        cols = [c[0] for c in cur.description]
        return {"columns": cols, "rows": [list(r) for r in cur.fetchmany(MAX_RESULTS)]}
    except sqlite3.Error as e:
        return {"error": str(e)}
    finally:
        con.close()


def _owner(value: str):
    lk = load_linker()
    v = value.strip()
    cands = tokens(v) | {v, v.lower()}
    return {c: sorted(lk.ids.users_for(c)) for c in cands if lk.ids.users_for(c)}


def dispatch(name: str, args: dict):
    lk = load_linker()
    if name == "get_case_dossier":
        return build_dossier(args["case_id"])
    if name == "search_evidence":
        return _search(args["query"], args["as_of"])
    if name == "get_resource_history":
        return resource_view(args["resource_id"], args["as_of"])
    if name == "query_snapshot":
        return _query_snapshot(args["file_id"], args["sql"])
    if name == "lookup_identifier_owner":
        return _owner(args["value"])
    if name == "get_evidence_file_info":
        return lk.store.ds.manifest.get(args["file_id"], {"error": "unknown file_id"})
    return {"error": f"unknown tool {name}"}


def dispatch_json(name: str, args: dict) -> str:
    return json.dumps(dispatch(name, args), ensure_ascii=False, default=str)
