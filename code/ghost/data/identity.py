"""Subject identity resolution: current snapshot + historical values from the audit log."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from ghost.data.loader import Dataset
from ghost.data.normalize import email_hash, norm_email, norm_name, norm_phone

FIELD_NORMALISERS = {
    "email": norm_email,
    "phone": norm_phone,
    "email_hash": str.lower,
    "name": norm_name,
}


@dataclass
class Subject:
    user_id: str | None
    request_email: str
    identifiers: dict[str, dict] = field(default_factory=dict)  # token -> {kind, current, source}

    @property
    def strong_tokens(self) -> set[str]:
        """Identifiers that can ground a link on their own (names are excluded: decoys share names)."""
        return {t for t, m in self.identifiers.items() if m["kind"] != "name"}

    @property
    def names(self) -> set[str]:
        return {t for t, m in self.identifiers.items() if m["kind"] == "name"}

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "request_email": self.request_email,
            "identifiers": [{"value": t, **m} for t, m in sorted(self.identifiers.items())],
        }


class IdentityIndex:
    def __init__(self, ds: Dataset):
        self.current = {u["user_id"]: u for u in ds.identities}
        self.history = defaultdict(list)
        for a in sorted(ds.audit_log, key=lambda r: r["event_time"]):
            self.history[a["user_id"]].append(a)
        # token -> user_ids that ever held it (used to detect resources claimed by other people)
        self.owner: dict[str, set[str]] = defaultdict(set)
        self.kind: dict[str, str] = {}
        for uid in self.current:
            for tok, meta in self._all_tokens(uid, as_of=None).items():
                self.owner[tok].add(uid)
                self.kind.setdefault(tok, meta["kind"])

    def _all_tokens(self, uid: str, as_of: str | None) -> dict[str, dict]:
        out: dict[str, dict] = {}
        cur = self.current.get(uid)
        if cur:
            out[uid] = {"kind": "user_id", "current": True, "source": "user_identities"}
            for f, fn in FIELD_NORMALISERS.items():
                if cur.get(f):
                    out[fn(cur[f])] = {"kind": f, "current": True, "source": "user_identities"}
        for a in self.history.get(uid, []):
            if as_of and a["event_time"] > as_of:
                continue
            fn = FIELD_NORMALISERS.get(a["field_name"])
            if not fn:
                continue
            for v, is_cur in ((a["old_value"], False), (a["new_value"], True)):
                if v:
                    tok = fn(v)
                    out.setdefault(tok, {"kind": a["field_name"], "current": is_cur, "source": a["audit_id"]})
        # Derived hashes of every known email (evidence may carry hashes of historical addresses).
        for tok, meta in list(out.items()):
            if meta["kind"] == "email":
                out.setdefault(email_hash(tok), {"kind": "email_hash", "current": meta["current"],
                                                 "source": f"sha256({meta['source']})"})
        return out

    def users_for(self, token: str, strong_only: bool = False) -> set[str]:
        if strong_only and self.kind.get(token) == "name":
            return set()
        return self.owner.get(token, set())

    def resolve(self, email: str, as_of: str | None = None) -> Subject:
        e = norm_email(email)
        uids = self.users_for(e) or self.users_for(email_hash(e))
        uid = sorted(uids)[0] if len(uids) == 1 else (sorted(uids)[0] if uids else None)
        subj = Subject(user_id=uid, request_email=e)
        if uid:
            subj.identifiers = self._all_tokens(uid, as_of)
        else:
            subj.identifiers = {e: {"kind": "email", "current": True, "source": "request"},
                                email_hash(e): {"kind": "email_hash", "current": True, "source": "sha256(request)"}}
        return subj
