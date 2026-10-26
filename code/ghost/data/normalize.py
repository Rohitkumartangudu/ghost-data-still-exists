"""Identifier normalisation and token extraction shared by identity resolution and the fact store."""
from __future__ import annotations

import hashlib
import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
HEX64_RE = re.compile(r"\b[0-9a-fA-F]{64}\b")
PHONE_RE = re.compile(r"\+\d[\d\s().-]{6,}\d")
# Generic "<word>_<number>" token, e.g. internal user references or resource ids.
REF_RE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z][a-z0-9]*)*_\d+\b")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")


def norm_email(v: str) -> str:
    return v.strip().lower()


def norm_phone(v: str) -> str:
    return "+" + re.sub(r"\D", "", v)


def norm_name(v: str) -> str:
    return " ".join(v.casefold().split())


def email_hash(email: str) -> str:
    return hashlib.sha256(norm_email(email).encode("utf-8")).hexdigest()


def tokens(value) -> set[str]:
    """Normalised identifier-like tokens found inside an arbitrary value."""
    if value is None:
        return set()
    s = str(value)
    out: set[str] = set()
    out.update(norm_email(m) for m in EMAIL_RE.findall(s))
    out.update(m.lower() for m in HEX64_RE.findall(s))
    out.update(norm_phone(m) for m in PHONE_RE.findall(s))
    out.update(REF_RE.findall(s))
    return out


def is_iso(v) -> bool:
    return isinstance(v, str) and bool(ISO_RE.match(v))
