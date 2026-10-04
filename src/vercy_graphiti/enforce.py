"""Enforcement of the Vercy Governance Overlay, store-agnostic.

Implements ENFORCEMENT-CONTRACT.md draft 0.3 (https://github.com/ver-cy/vercy-py):
validity, applicability, supersession, precedence, then disclosure last, with
stable reason codes and a disclosure boundary that never names a withheld record.

Everything about who wrote a record and who owns a concept comes from the host
(`Host`), never from the record itself. A record's own `concept_owner` is only a
claim; it is checked against the host's ownership register.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Iterable, Optional

# outcomes
ANSWERED, ABSTAINED, REFUSED, EMPTY = "answered", "abstained", "refused", "empty"

# reason codes
EXPIRED = "expired"
VALIDITY_UNKNOWN = "validity_unknown"
OUT_OF_SCOPE = "out_of_scope"
SUPERSEDED = "superseded"
UNAUTHORIZED_SUPERSESSION = "unauthorized_supersession"
UNAUTHORIZED_PRECEDENCE = "unauthorized_precedence"
CONFLICT_UNRESOLVED = "conflict_unresolved"
NO_AUTHORITATIVE_RECORD = "no_authoritative_record"
NOT_RELEASED = "not_released"
RESTRICTED_WITHOUT_RELEASE = "restricted_without_release"

POLICIES = ("latest_valid_from", "abstain")
RESTRICTED_MARKERS = ("release_to", "classification", "confidential", "restricted")


def _empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or \
        (isinstance(value, (list, tuple, dict, set)) and not value)


def _tuple(value: Any) -> tuple[str, ...]:
    if _empty(value):
        return ()
    if isinstance(value, (list, tuple, set)):
        return tuple(str(v) for v in value)
    return (str(value),)


def _date(value: Any) -> Optional[date]:
    if _empty(value):
        return None
    if isinstance(value, date):
        return value if not hasattr(value, "date") else value.date()  # datetime -> date
    return date.fromisoformat(str(value)[:10])


@dataclass(frozen=True)
class Fact:
    """One record, as the engine sees it. `written_by` is host-attested, or None."""

    record_id: str
    concept: str
    value: str
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    has_valid_to: bool = True
    applies_to: tuple[str, ...] = ()
    does_not_apply_to: tuple[str, ...] = ()
    release_to: tuple[str, ...] = ()
    restricted: bool = False
    supersedes: tuple[str, ...] = ()
    written_by: Optional[str] = None
    claims_priority: bool = False
    source: Optional[str] = None

    @classmethod
    def from_record(cls, record: dict[str, Any], written_by: Optional[str] = None) -> "Fact":
        """Build from an overlay record. `written_by` must come from the host, not the record."""
        return cls(
            record_id=str(record["record_id"]),
            concept=str(record["concept"]),
            value=str(record.get("value", "")),
            valid_from=_date(record.get("valid_from")),
            valid_to=_date(record.get("valid_to")),
            has_valid_to="valid_to" in record,
            applies_to=_tuple(record.get("applies_to")),
            does_not_apply_to=_tuple(record.get("does_not_apply_to")),
            release_to=_tuple(record.get("release_to")),
            restricted=any(not _empty(record.get(m)) for m in RESTRICTED_MARKERS),
            supersedes=_tuple(record.get("supersedes")),
            written_by=written_by,
            claims_priority=not _empty(record.get("priority")) or not _empty(record.get("conflict_policy")),
            source=None if _empty(record.get("source")) else str(record.get("source")),
        )


@dataclass(frozen=True)
class Caller:
    principal: str
    audiences: frozenset[str] = frozenset()

    @classmethod
    def of(cls, principal: str, audiences: Iterable[str] = ()) -> "Caller":
        return cls(principal, frozenset(audiences) | {principal})


@dataclass(frozen=True)
class Host:
    """What only the host may supply: the ownership register and the conflict policy."""

    owners: dict[str, str]
    policy: str = "latest_valid_from"

    def __post_init__(self):
        if self.policy not in POLICIES:
            raise ValueError(f"unknown conflict policy {self.policy!r}; one of {POLICIES}")


@dataclass
class Decision:
    outcome: str
    answer: Optional[Fact] = None
    conflict: list[Fact] = field(default_factory=list)
    reasons: list[dict[str, Any]] = field(default_factory=list)
    withheld: int = 0

    def payload(self) -> dict[str, Any]:
        """What may be returned to the caller. Nothing from a withheld record is in here."""
        out: dict[str, Any] = {"outcome": self.outcome, "withheld_records": self.withheld,
                               "reasons": self.reasons}
        if self.answer is not None:
            out["answer"] = {"record_id": self.answer.record_id, "value": self.answer.value,
                             "source": self.answer.source,
                             "valid_from": self.answer.valid_from.isoformat() if self.answer.valid_from else None}
        if self.conflict:
            out["conflict"] = [{"record_id": f.record_id, "value": f.value} for f in self.conflict]
        return out


def visible(fact: Fact, caller: Caller) -> bool:
    if not fact.restricted:
        return True
    return bool(fact.release_to) and bool(caller.audiences & set(fact.release_to))


def decide(facts: Iterable[Fact], *, concept: str, caller: Caller, as_of: date,
           host: Host, scope: Iterable[str] = ()) -> Decision:
    """Answer one concept for one caller at one date. Steps 1 to 5 of the contract, in order."""
    facts = [f for f in facts if f.concept == concept]
    scope = set(scope)
    owner = host.owners.get(concept)
    raw_reasons: list[tuple[str, Optional[Fact]]] = []

    def authoritative(f: Fact) -> bool:
        return owner is not None and f.written_by == owner

    # 1. validity
    kept = []
    for f in facts:
        if f.valid_from is None or not f.has_valid_to:
            raw_reasons.append((VALIDITY_UNKNOWN, f))
            kept.append(f)
        elif f.valid_from <= as_of and (f.valid_to is None or as_of <= f.valid_to):
            kept.append(f)
        else:
            raw_reasons.append((EXPIRED, f))

    # 2. applicability (exact match on host-supplied scope values)
    applicable = []
    for f in kept:
        if scope & set(f.does_not_apply_to) or (f.applies_to and not scope & set(f.applies_to)):
            raw_reasons.append((OUT_OF_SCOPE, f))
        else:
            applicable.append(f)
    if not applicable:
        return _finish(Decision(EMPTY), raw_reasons, caller)

    # 3. supersession, only from authoritative and valid writers; mutual cancels out
    by_id = {f.record_id: f for f in applicable}
    dropped: set[str] = set()
    for f in applicable:
        for target in f.supersedes:
            if target not in by_id:
                continue
            if not authoritative(f):
                raw_reasons.append((UNAUTHORIZED_SUPERSESSION, f))
                continue
            if f.record_id in by_id[target].supersedes and authoritative(by_id[target]):
                continue          # two authoritative records supersede each other: a conflict
            dropped.add(target)
    for rid in dropped:
        raw_reasons.append((SUPERSEDED, by_id[rid]))
    remaining = [f for f in applicable if f.record_id not in dropped]

    # 4. precedence: authoritative records outrank everything else
    auth = [f for f in remaining if authoritative(f)]
    if auth:
        for f in remaining:
            if not authoritative(f) and f.claims_priority:
                raw_reasons.append((UNAUTHORIZED_PRECEDENCE, f))
        pool = auth
    else:
        raw_reasons.append((NO_AUTHORITATIVE_RECORD, None))
        pool = remaining
    winner, conflict = _resolve(pool, host.policy)

    # 5. disclosure, last, with no fallback to a lower-ranked record
    if winner is not None:
        if visible(winner, caller):
            return _finish(Decision(ANSWERED, answer=winner), raw_reasons, caller)
        code = NOT_RELEASED if winner.release_to else RESTRICTED_WITHOUT_RELEASE
        raw_reasons.append((code, winner))
        return _finish(Decision(REFUSED), raw_reasons, caller)
    raw_reasons.append((CONFLICT_UNRESOLVED, None))
    if all(visible(f, caller) for f in conflict):
        return _finish(Decision(ABSTAINED, conflict=sorted(conflict, key=lambda f: f.record_id)),
                       raw_reasons, caller)
    for f in conflict:
        if not visible(f, caller):
            raw_reasons.append((NOT_RELEASED if f.release_to else RESTRICTED_WITHOUT_RELEASE, f))
    return _finish(Decision(REFUSED), raw_reasons, caller)


def _resolve(pool: list[Fact], policy: str) -> tuple[Optional[Fact], list[Fact]]:
    """One winner, or the records that remain in conflict."""
    values = {f.value for f in pool}
    known = [f for f in pool if f.valid_from is not None]
    if len(values) == 1:
        ranked = sorted(known or pool, key=lambda f: (f.valid_from or date.min, f.record_id))
        return ranked[-1], []
    if policy == "latest_valid_from" and known:
        latest = max(f.valid_from for f in known)
        top = [f for f in known if f.valid_from == latest]
        if len({f.value for f in top}) == 1:
            return sorted(top, key=lambda f: f.record_id)[-1], []
        return None, top
    return None, pool


def _finish(decision: Decision, raw: list[tuple[str, Optional[Fact]]], caller: Caller) -> Decision:
    """Reasons name a record only when the caller may see it; the rest are counted."""
    seen, anonymous = set(), {}
    withheld_ids = set()
    for code, f in raw:
        if f is not None and not visible(f, caller):
            withheld_ids.add(f.record_id)
            anonymous[code] = anonymous.get(code, 0) + 1
            continue
        key = (code, f.record_id if f else None)
        if key in seen:
            continue
        seen.add(key)
        decision.reasons.append({"code": code, "record_id": f.record_id if f else None})
    for code, count in sorted(anonymous.items()):
        decision.reasons.append({"code": code, "record_id": None, "count": count})
    decision.withheld = len(withheld_ids)
    return decision
