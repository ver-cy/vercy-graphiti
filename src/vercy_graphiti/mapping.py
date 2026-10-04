"""Overlay records <-> Graphiti entity edges.

Graphiti already carries the time half of the overlay on every edge (`valid_at`,
`invalid_at`). This module adds the authority and disclosure half as edge
attributes, and attests the writer with an HMAC the host keys, so that a record
cannot make itself authoritative by writing a field.

    (concept) -[VERCY_FACT {fact, valid_at, invalid_at, attributes}]-> (source)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import date, datetime, timezone
from typing import Any, Optional

from .enforce import Fact, _date

EDGE_NAME = "VERCY_FACT"
NS = uuid.UUID("6f1d2c1e-6b0e-5c3a-9d55-7665726379a1")   # uuid5 namespace for vercy record ids


def canonical(record: dict[str, Any]) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=str).encode("utf-8")


def sign(key: bytes, record: dict[str, Any], written_by: str) -> str:
    return hmac.new(key, canonical(record) + b"\x00" + written_by.encode("utf-8"), hashlib.sha256).hexdigest()


def edge_uuid(group_id: str, record_id: str) -> str:
    return str(uuid.uuid5(NS, f"{group_id}:{record_id}"))


def _dt(value: Any) -> Optional[datetime]:
    d = _date(value)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc) if d else None


def edge_fields(record: dict[str, Any], written_by: str, key: bytes, group_id: str) -> dict[str, Any]:
    """The keyword arguments for an EntityEdge, minus the node uuids."""
    return {
        "uuid": edge_uuid(group_id, str(record["record_id"])),
        "name": EDGE_NAME,
        "fact": str(record.get("value", "")),
        "group_id": group_id,
        "valid_at": _dt(record.get("valid_from")),
        "invalid_at": _dt(record.get("valid_to")),
        "attributes": {
            "vercy_record": json.dumps(record, ensure_ascii=False, sort_keys=True, default=str),
            "vercy_written_by": written_by,
            "vercy_sig": sign(key, record, written_by),
        },
    }


def record_of(attributes: dict[str, Any]) -> Optional[dict[str, Any]]:
    raw = (attributes or {}).get("vercy_record")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def fact_of(attributes: dict[str, Any], key: bytes) -> Optional[Fact]:
    """A Fact whose writer is trusted only if the host's signature verifies."""
    record = record_of(attributes)
    if record is None:
        return None
    claimed = str(attributes.get("vercy_written_by") or "")
    sig = str(attributes.get("vercy_sig") or "")
    trusted = bool(claimed) and hmac.compare_digest(sig, sign(key, record, claimed))
    return Fact.from_record(record, written_by=claimed if trusted else None)


STRIPPED = ("concept_owner", "owner_role", "conflict_policy", "priority", "release_to",
            "classification", "confidential", "restricted", "supersedes", "applies_to",
            "does_not_apply_to")


def stripped_fact(attributes: dict[str, Any]) -> Optional[Fact]:
    """The ablation arm: the same record with the authority and disclosure fields removed.

    Time stays, because Graphiti has it natively. Writer attestation is dropped too, since
    without the fields there is no ownership to check it against.
    """
    record = record_of(attributes)
    if record is None:
        return None
    return Fact.from_record({k: v for k, v in record.items() if k not in STRIPPED}, written_by=None)
