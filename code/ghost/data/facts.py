"""Unified fact store over all evidence files and activity rows, with identifier and resource indexes."""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from functools import lru_cache

from ghost.config import path as cfg_path
from ghost.data.loader import Dataset, load_dataset
from ghost.data.normalize import REF_RE, tokens
from ghost.data.parsers import Fact, parse_json, parse_log, parse_sql, parse_text, pdf_text


ALIAS_RE = re.compile(r"\b([a-z][a-z0-9_]*_\d+) \(([^)]+)\)")


def prefix_of(resource_id: str) -> str:
    return resource_id.rsplit("_", 1)[0]


class FactStore:
    def __init__(self, ds: Dataset):
        self.ds = ds
        self.systems = {a["source_system"] for a in ds.activity} | {a["target_system"] for a in ds.activity}
        self.categories = {r["data_category"] for r in ds.rules}
        self.facts: list[Fact] = []
        self.documents: list[Fact] = []   # PDF/image facts without a single resource
        self._load_evidence()
        self._load_activity()
        # Resource-id prefixes observed in structured sources (activity + parsed records). Records extracted
        # from images only count as resources when their id has one of these prefixes.
        self.base_prefixes = {prefix_of(f.resource_id) for f in self.facts + self.activity_facts}
        # Aliases published by catalogue activity, e.g. "linked object_19 (delivery_confirmation_19.pdf) to ...":
        # lets image records that show a display name map onto the resource id.
        self.aliases: dict[str, str] = {}
        for f in self.activity_facts:
            for rid, alias in re.findall(ALIAS_RE, f.fields.get("details", "")):
                if alias != rid and alias not in {u["user_id"] for u in self.ds.identities}:
                    self.aliases[alias.strip().lower()] = rid
        self._load_images()
        self._index()
        self.file_owners_cache: dict[str, int] = {}

    # ------------------------------------------------------------ loading
    def _load_evidence(self):
        by_kind = defaultdict(list)
        for fid, m in self.ds.manifest.items():
            by_kind[m["file_path"].rsplit(".", 1)[-1].lower()].append(fid)
        for fid in by_kind["json"]:
            self._add(parse_json(self.ds.evidence_path(fid), fid, self._cap(fid), self.systems))
        self.snapshots: dict[str, dict] = {}
        for fid in by_kind["sql"]:
            facts, scope = parse_sql(self.ds.evidence_path(fid), fid, self._cap(fid), self.systems)
            scope["resources"] = {f.resource_id for f in facts}
            self.snapshots[fid] = scope
            self._add(facts)
        for fid in by_kind["log"]:
            self._add(parse_log(self.ds.evidence_path(fid), fid, self._cap(fid), self.systems))
        known_types = self.categories | {str(f.fields.get("record_type")) for f in self.facts if f.fields.get("record_type")}
        for fid in by_kind["txt"]:
            self._add(parse_text(self.ds.evidence_path(fid), fid, self._cap(fid), self.systems, known_types))
        for fid in by_kind["pdf"]:
            self._add_document(fid, "pdf", {"text": pdf_text(self.ds.evidence_path(fid))})
        self._image_ids = by_kind["png"] + by_kind["jpg"] + by_kind["jpeg"]

    def _load_images(self):
        for fid in self._image_ids:
            cached = cfg_path("cache") / "vision" / f"{fid}.json"
            if cached.exists():  # produced by the evidence-extraction agent
                self._add_document(fid, "image", json.loads(cached.read_text(encoding="utf-8")))

    def _cap(self, fid: str) -> str:
        return self.ds.manifest[fid]["captured_at"]

    def _add(self, facts: list[Fact]):
        self.facts.extend(f for f in facts if f.resource_id)

    def _add_document(self, fid: str, kind: str, payload: dict):
        doc = Fact(fid, self._cap(fid), None, None, payload, {"origin": self.ds.manifest[fid]["origin"]}, kind)
        self.documents.append(doc)
        # Structured extraction may list per-resource records.
        for rec in payload.get("records", []) if isinstance(payload.get("records"), list) else []:
            rid = str(rec.get("resource_id") or "")
            if rid.strip().lower() in getattr(self, "aliases", {}):
                rec = {**rec, "display_name": rid, "resource_id": self.aliases[rid.strip().lower()]}
                rid = rec["resource_id"]
            if REF_RE.fullmatch(rid) and prefix_of(rid) in getattr(self, "base_prefixes", set()):
                self.facts.append(Fact(fid, rec.get("observed_at") or self._cap(fid), rec["resource_id"],
                                       rec.get("system"), rec, doc.context, kind))

    def _load_activity(self):
        self.activity_facts = []
        for a in self.ds.activity:
            self.activity_facts.append(Fact(a["activity_id"], a["event_time"], a["resource_id"], a["target_system"],
                                            dict(a), {}, "activity"))

    # ------------------------------------------------------------ indexes
    def _index(self):
        self.by_resource: dict[str, list[Fact]] = defaultdict(list)
        self.by_token: dict[str, list[Fact]] = defaultdict(list)
        self.activity_by_resource: dict[str, list[Fact]] = defaultdict(list)
        self.resources: set[str] = set()
        for f in self.facts:
            self.by_resource[f.resource_id].append(f)
            self.resources.add(f.resource_id)
        for f in self.activity_facts:
            self.activity_by_resource[f.resource_id].append(f)
            self.resources.add(f.resource_id)
        for f in self.facts + self.activity_facts + self.documents:
            f.tokens = set()
            for k, v in f.fields.items():
                if k in ("activity_id",):
                    continue
                f.tokens |= tokens(v)
            for t in f.tokens:
                self.by_token[t].append(f)
        self.resource_prefixes = set(self.base_prefixes)
        self._learn_resource_profiles()
        for sc in self.snapshots.values():  # tables that name no system cover the systems of their rows
            for rs in sc.get("table_resources", {}).values():
                for rid in rs:
                    sysname = self.resource_system(rid)
                    if sysname:
                        sc["systems"].setdefault(sysname, sc["captured_at"])
        for f in self.facts:  # facts whose source names no known system inherit the resource's system
            if f.system not in self.systems:
                if f.system:
                    f.context["raw_system"] = f.system
                f.system = self.resource_system(f.resource_id)

    def _learn_resource_profiles(self):
        """Learn, per resource-id prefix, which systems and record types it is observed with."""
        sys_c, type_c = defaultdict(Counter), defaultdict(Counter)
        for f in self.facts:
            p = prefix_of(f.resource_id)
            if f.system in self.systems:
                sys_c[p][f.system] += 1
            if f.fields.get("record_type"):
                type_c[p][str(f.fields["record_type"])] += 1
        for f in self.activity_facts:
            sys_c[prefix_of(f.resource_id)][f.fields["target_system"]] += 1
        self.prefix_systems = {p: c.most_common() for p, c in sys_c.items()}
        self.prefix_types = {p: c.most_common() for p, c in type_c.items()}

    # ------------------------------------------------------------ queries
    def visible(self, f: Fact, as_of: str | None) -> bool:
        """Usable for reasoning: the observation itself happened at or before audit_at."""
        return as_of is None or f.observed_at <= as_of

    def citable(self, f: Fact, as_of: str | None) -> bool:
        """Citable as evidence: observed and captured (file capture time) at or before audit_at."""
        if not self.visible(f, as_of):
            return False
        if f.kind == "activity" or as_of is None:
            return True
        return self.ds.manifest.get(f.evidence_id, {}).get("captured_at", "") <= as_of

    def absence_snapshots(self, rid: str, as_of: str | None) -> list[dict]:
        """SQL snapshots (<= as_of) that cover the resource's system but contain no row for it."""
        system = self.resource_system(rid)
        out = []
        for fid, sc in self.snapshots.items():
            if as_of and sc["captured_at"] > as_of:
                continue
            # An empty snapshot (no rows at all) proves nothing about any single record.
            if system in sc["systems"] and rid not in sc["resources"] and sum(sc["tables"].values()) > 0:
                out.append({"evidence_id": fid, "captured_at": sc["captured_at"], "topology": sc["topology"],
                            "covers": system, "rows_in_snapshot": sum(sc["tables"].values())})
        return sorted(out, key=lambda x: x["captured_at"])

    def replica_drift(self, rid: str, as_of: str | None) -> list[dict]:
        """For SQL snapshots holding the resource: which tables (primary/replicas) contain it and which do not."""
        out = []
        for fid, sc in self.snapshots.items():
            if rid not in sc["resources"] or (as_of and sc["captured_at"] > as_of):
                continue
            tr = sc.get("table_resources", {})
            schema = sc.get("table_schema", {})
            present = sorted(t for t, rs in tr.items() if rid in rs)
            shapes = {schema.get(t) for t in present}
            # Tables with the same schema are copies of one another (primary / replica / regional).
            missing = sorted(t for t, rs in tr.items() if rid not in rs and schema.get(t) in shapes)
            if missing:
                out.append({"evidence_id": fid, "captured_at": sc["captured_at"], "present_in": present, "absent_from": missing})
        return out

    def resource_system(self, rid: str) -> str | None:
        # Prefer the system the remediation workflow targets (activity event types that are contract operations).
        from ghost.contract import OP_PHASE
        acts = Counter(f.fields["target_system"] for f in self.activity_by_resource.get(rid, [])
                       if OP_PHASE.get(f.fields["event_type"]) == "REMEDIATION")
        if not acts:
            acts = Counter(f.fields["target_system"] for f in self.activity_by_resource.get(rid, []))
        if acts:
            return acts.most_common(1)[0][0]
        evid = Counter(f.system for f in self.by_resource.get(rid, []) if f.system in self.systems)
        if evid:
            return evid.most_common(1)[0][0]
        prof = self.prefix_systems.get(prefix_of(rid))
        return prof[0][0] if prof else None

    def resource_refs(self, f: Fact) -> set[str]:
        """Other known resources referenced from within a fact (e.g. a search document's source record)."""
        user_ids = {u["user_id"] for u in self.ds.identities}
        return {t for t in f.tokens if REF_RE.fullmatch(t) and t != f.resource_id and t not in user_ids
                and (t in self.resources or prefix_of(t) in self.resource_prefixes)}


@lru_cache(maxsize=1)
def load_store() -> FactStore:
    return FactStore(load_dataset())
