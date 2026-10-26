"""Format-generic evidence parsers. Each returns a list of Fact records; no case- or id-specific logic."""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ghost.data.normalize import REF_RE, is_iso

TIME_KEYS = ("observed_at", "recorded_at", "captured_at", "updated_at", "indexed_at", "created_at", "exported_at")
KV_RE = re.compile(r'([A-Za-z_][\w.]*)=("((?:[^"\\]|\\.)*)"|[^\s"|]+)')
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s+(.*)$")


@dataclass
class Fact:
    evidence_id: str            # file_id (or activity_id for activity-derived facts)
    observed_at: str            # when the state was observed
    resource_id: str | None
    system: str | None
    fields: dict = field(default_factory=dict)   # flattened record fields
    context: dict = field(default_factory=dict)  # table / collection / data_store / origin ...
    kind: str = ""              # sql | json | log | text | pdf | image | activity

    def compact(self) -> dict:
        d = {"evidence_id": self.evidence_id, "observed_at": self.observed_at, "kind": self.kind,
             "system": self.system, "resource_id": self.resource_id}
        d["fields"] = {k: v for k, v in self.fields.items() if k not in ("resource_id",)}
        if self.context:
            d["context"] = self.context
        return d


def flatten(obj, prefix: str = "") -> dict:
    out: dict = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}{k}."))
    elif isinstance(obj, list) and any(isinstance(x, (dict, list)) for x in obj):
        for i, x in enumerate(obj):
            out.update(flatten(x, f"{prefix}{i}."))
    elif prefix:
        out[prefix[:-1]] = obj
    return out


def _first_time(d: dict, default: str) -> str:
    for k in TIME_KEYS:
        v = d.get(k)
        if is_iso(v):
            return v
    return default


# ---------------------------------------------------------------- SQL
def parse_sql(path: Path, file_id: str, captured_at: str, systems: set[str]) -> tuple[list[Fact], dict]:
    """Returns (facts, scope). scope describes which systems/tables the snapshot covers, so that the
    absence of a row can be used as evidence that a record is absent at snapshot time."""
    con = sqlite3.connect(":memory:")
    con.executescript(path.read_text(encoding="utf-8"))
    header = "\n".join(l for l in path.read_text(encoding="utf-8").splitlines()[:8] if l.startswith("--"))
    topology = (re.search(r"topology:\s*(\S+)", header) or [None, None])[1]
    facts = []
    scope = {"file_id": file_id, "captured_at": captured_at, "topology": topology, "tables": {}, "systems": {}}
    for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
        cur = con.execute(f'SELECT * FROM "{table}"')
        cols = [c[0] for c in cur.description]
        if "resource_id" not in cols:
            if {"origin_system", "captured_at"} <= set(cols):
                scope["systems"].update({r[cols.index("origin_system")]: r[cols.index("captured_at")] for r in cur})
            continue
        scope["tables"][table] = 0
        scope.setdefault("table_schema", {})[table] = tuple(sorted(cols))
        scope.setdefault("table_resources", {})[table] = set()
        for row in cur:
            rec = dict(zip(cols, row))
            for k, v in list(rec.items()):  # expand embedded JSON documents
                if isinstance(v, str) and v.startswith("{"):
                    try:
                        rec.update(flatten(json.loads(v)))
                        del rec[k]
                    except json.JSONDecodeError:
                        pass
            system = rec.get("origin_system") or rec.get("system") or _system_from_name(table, systems)
            scope["tables"][table] += 1
            scope["table_resources"][table].add(rec.get("resource_id"))
            if system:
                scope["systems"].setdefault(system, captured_at)
            facts.append(Fact(file_id, _first_time(rec, captured_at), rec.get("resource_id"), system,
                              rec, {"table": table, "topology": topology, "snapshot_captured_at": captured_at}, "sql"))
    con.close()
    return facts, scope


def _system_from_name(name: str, systems: set[str]) -> str | None:
    for s in sorted(systems, key=len, reverse=True):
        if s in name:
            return s
    return None


# ---------------------------------------------------------------- JSON
def parse_json(path: Path, file_id: str, captured_at: str, systems: set[str]) -> list[Fact]:
    data = json.loads(path.read_text(encoding="utf-8"))
    facts: list[Fact] = []

    def walk(o, ctx: dict, keys: list[str]):
        if isinstance(o, dict):
            if "resource_id" in o:
                rec = flatten(o)
                system = o.get("system") or ctx.get("system") or (
                    ctx.get("data_store") if ctx.get("data_store") in systems else None)
                facts.append(Fact(file_id, _first_time(o, ctx.get("_time", captured_at)), o["resource_id"],
                                  system, rec, {k: v for k, v in ctx.items() if not k.startswith("_")}, "json"))
                return
            sub = dict(ctx)
            for k, v in o.items():
                if not isinstance(v, (dict, list)):
                    if k in TIME_KEYS and is_iso(v):
                        sub["_time"] = v
                    elif k in ("source", "origin", "data_store", "collection", "region"):
                        sub[k] = v
            for k, v in o.items():
                if isinstance(v, (dict, list)):
                    nxt = dict(sub)
                    if k in systems:
                        nxt["system"] = k
                    elif keys and keys[-1] in systems:
                        nxt["collection"] = k
                    walk(v, nxt, keys + [k])
        elif isinstance(o, list):
            for x in o:
                walk(x, ctx, keys)

    walk(data, {}, [])
    return facts


# ---------------------------------------------------------------- logs
def _parse_kv(s: str) -> dict:
    out = {}
    for k, raw, quoted in KV_RE.findall(s):
        v = quoted.replace('\\"', '"') if raw.startswith('"') else raw
        if "=" in v and k in ("message", "msg"):
            out.update(_parse_kv(v))
        else:
            out[k] = None if v == "null" else v
    return out


def parse_log_line(line: str, systems: set[str]) -> dict | None:
    m = TS_RE.match(line.strip())
    if not m:
        return None
    ts, rest = m.groups()
    rec: dict = {"_ts": ts}
    if rest.startswith("{"):
        try:
            rec.update({k: (None if v in ("null", None) else v) for k, v in json.loads(rest).items()})
        except json.JSONDecodeError:
            pass
    elif rest.startswith("["):  # [LEVEL] [service] [trace] event: kv...
        parts = re.findall(r"\[([^\]]*)\]", rest)
        tail = rest[rest.rfind("]") + 1:].strip()
        ev, _, kv = tail.partition(":")
        rec.update(level=parts[0] if parts else None, service=parts[1] if len(parts) > 1 else None,
                   trace_id=parts[2] if len(parts) > 2 else None, event=ev.strip())
        rec.update(_parse_kv(kv))
    elif " | " in rest:  # LEVEL | service | trace | event | kv...
        parts = [p.strip() for p in rest.split(" | ")]
        rec.update(level=parts[0], service=parts[1], trace_id=parts[2], event=parts[3])
        rec.update(_parse_kv(" ".join(parts[4:])))
    elif rest.startswith("<"):  # syslog: <pri>ver host app procid msgid sd kv...
        parts = rest.split(" ", 6)
        rec.update(host=parts[1], service=parts[2], trace_id=parts[3])
        rec.update(_parse_kv(parts[6] if len(parts) > 6 else ""))
    else:
        rec.update(_parse_kv(rest))
    rec.setdefault("trace_id", rec.get("requestId"))
    if not rec.get("service"):
        rec["service"] = rec.get("logger") or rec.get("function")
    return rec


def parse_log(path: Path, file_id: str, captured_at: str, systems: set[str]) -> list[Fact]:
    traces: dict[str, list[dict]] = {}
    order: list[str] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        rec = parse_log_line(line, systems)
        if not rec:
            continue
        key = rec.get("trace_id") or f"line{i}"
        if key not in traces:
            traces[key] = []
            order.append(key)
        traces[key].append(rec)
    facts = []
    for key in order:
        lines = traces[key]
        rids = [r["resource_id"] for r in lines if r.get("resource_id")]
        shared = {}
        for r in lines:
            if not r.get("resource_id"):
                shared.update({k: v for k, v in r.items() if not k.startswith("_")})
        for rid in dict.fromkeys(rids):
            merged = dict(shared)
            events = []
            last_ts = None
            for r in lines:
                if r.get("resource_id") in (rid, None):
                    merged.update({k: v for k, v in r.items() if not k.startswith("_")})
                    events.append(r.get("event"))
                    last_ts = r["_ts"]
            merged["resource_id"] = rid
            merged["events"] = [e for e in events if e]
            system = merged.get("system") or merged.get("service")
            facts.append(Fact(file_id, last_ts or captured_at, rid, system, merged,
                              {"trace_id": key if not key.startswith("line") else None}, "log"))
    return facts


# ---------------------------------------------------------------- plain text
def parse_text(path: Path, file_id: str, captured_at: str, systems: set[str], known_types: set[str]) -> list[Fact]:
    facts = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = [p.strip() for p in line.strip().lstrip("-* ").split("|")]
        if len(parts) < 4:
            continue
        rec: dict = {}
        upper = []
        for p in parts:
            if is_iso(p):
                rec["observed_at"] = p
            elif p in systems and "system" not in rec:
                rec["system"] = p
            elif REF_RE.fullmatch(p) and "resource_id" not in rec:
                rec["resource_id"] = p
            elif re.fullmatch(r"[A-Z][A-Z_]+", p):
                upper.append(p)
            else:
                rec.setdefault("subject_reference", p)
        if len(upper) >= 2:
            rec["record_type"], rec["result"] = upper[0], upper[-1]
        elif upper:
            rec["record_type" if upper[0] in known_types else "result"] = upper[0]
        if rec.get("resource_id"):
            facts.append(Fact(file_id, rec.get("observed_at", captured_at), rec["resource_id"],
                              rec.get("system"), rec, {}, "text"))
    return facts


# ---------------------------------------------------------------- PDF (text layer; images handled by the extraction agent)
def pdf_text(path: Path) -> str:
    import pdfplumber
    with pdfplumber.open(path) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)
