"""Overlay records <-> Graphiti entity edges.

Graphiti already carries the time half of the overlay on every edge (`valid_at`,
`invalid_at`). This module adds the authority and disclosure half as edge
attributes inside a signed envelope:

    (concept) -[VERCY_FACT {fact, valid_at, invalid_at, attributes}]-> (source)

The host signs (group, edge uuid, record, writer) with its own key. On read, an
envelope whose signature fails is rejected whole: a record cannot make itself
authoritative, move to another concept or group, or shed its `release_to`.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .enforce import Fact, _date

EDGE_NAME = "VERCY_FACT"
NS = uuid.UUID("6f1d2c1e-6b0e-5c3a-9d55-7665726379a1")   # uuid5 namespace for vercy ids
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SIG = re.compile(r"^[0-9a-f]{64}$")
DATE_FIELDS = ("valid_from", "valid_to")

OK, UNSIGNED, TAMPERED = "ok", "unsigned", "tampered"


def validate(record: dict[str, Any]) -> None:
    """JSON-native values only, so the signed bytes are the same on every machine."""
    for key, value in record.items():
        if not isinstance(key, str):
            raise ValueError("record keys must be strings")
        if key in DATE_FIELDS:
            if value is not None and not (isinstance(value, str) and ISO_DATE.match(value)):
                raise ValueError(f"{key} must be YYYY-MM-DD or null")
        elif isinstance(value, list):
            if not all(isinstance(v, str) for v in value):
                raise ValueError(f"{key} must be a list of strings")
        elif value is not None and not isinstance(value, (str, int, bool)):
            raise ValueError(f"{key} must be a string, integer, boolean, list of strings or null")
    for required in ("record_id", "concept", "value"):
        if not isinstance(record.get(required), str) or not record[required].strip():
            raise ValueError(f"record needs a non-empty string {required!r}")


def canonical(record: dict[str, Any]) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(key: bytes, group_id: str, edge_id: str, record: dict[str, Any], written_by: str) -> str:
    message = b"\x00".join([b"vercy-envelope/1", group_id.encode(), edge_id.encode(),
                            canonical(record), written_by.encode("utf-8")])
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def node_uuid(group_id: str, kind: str, name: str) -> str:
    return str(uuid.uuid5(NS, f"{group_id}:{kind}:{name}"))


def edge_uuid(group_id: str, record_id: str) -> str:
    return str(uuid.uuid5(NS, f"{group_id}:edge:{record_id}"))


def _dt(value: Any) -> Optional[datetime]:
    d = _date(value)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) if d else None


def edge_fields(record: dict[str, Any], written_by: str, key: bytes, group_id: str) -> dict[str, Any]:
    """The keyword arguments for an EntityEdge, minus the node uuids."""
    validate(record)
    eid = edge_uuid(group_id, record["record_id"])
    return {
        "uuid": eid,
        "name": EDGE_NAME,
        "fact": record["value"],
        "group_id": group_id,
        "valid_at": _dt(record.get("valid_from")),
        "invalid_at": _dt(record.get("valid_to")),
        "attributes": {
            "vercy_record": canonical(record).decode("utf-8"),
            "vercy_written_by": written_by,
            "vercy_sig": sign(key, group_id, eid, record, written_by),
        },
    }


def record_of(attributes: dict[str, Any]) -> Optional[dict[str, Any]]:
    raw = (attributes or {}).get("vercy_record")
    if not raw:
        return None
    try:
        record = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def verify(attributes: dict[str, Any], key: bytes, group_id: str, edge_id: str) -> tuple[str, Optional[dict], Optional[str]]:
    """(status, record, attested writer). Only `ok` envelopes may take part in a decision."""
    record = record_of(attributes)
    if record is None:
        return UNSIGNED, None, None
    writer = attributes.get("vercy_written_by")
    sig = attributes.get("vercy_sig")
    if not isinstance(writer, str) or not writer or not isinstance(sig, str) or not SIG.match(sig):
        return TAMPERED, None, None
    if not hmac.compare_digest(sig, sign(key, group_id, edge_id, record, writer)):
        return TAMPERED, None, None
    return OK, record, writer


STRIPPED = ("concept_owner", "owner_role", "conflict_policy", "priority", "release_to",
            "classification", "confidential", "restricted", "supersedes", "applies_to",
            "does_not_apply_to")


def governed_fact(record: dict[str, Any], writer: str) -> Fact:
    return Fact.from_record(record, written_by=writer)


def fact_without_fields(record: dict[str, Any], writer: str) -> Fact:
    """Ablation: the overlay fields removed, the host's attestation and register kept."""
    return Fact.from_record({k: v for k, v in record.items() if k not in STRIPPED}, written_by=writer)


def fact_without_attestation(record: dict[str, Any], writer: str) -> Fact:
    """Ablation: the fields kept, the writer unknown, so the ownership register matches nobody."""
    return Fact.from_record(record, written_by=None)
