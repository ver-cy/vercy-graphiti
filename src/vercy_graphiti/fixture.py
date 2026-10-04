"""Load the adversarial fixture and score a payload against its oracle."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional

from .enforce import Caller, Fact, Host


@dataclass
class Case:
    id: str
    question: str
    concept: str
    caller: Caller
    as_of: date
    host: Host
    records: list[tuple[str, dict[str, Any]]]     # (written_by attested by the host, record)
    expect: dict[str, Any]

    def facts(self) -> list[Fact]:
        return [Fact.from_record(r, written_by=w) for w, r in self.records]


def load(path: str | Path) -> list[Case]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    cases = []
    for c in data["cases"]:
        host = Host(owners=data["host"]["owners"], policy=c.get("host_policy", data["host"]["policy"]))
        cases.append(Case(
            id=c["id"], question=c["question"], concept=c["concept"],
            caller=Caller.of(c["caller"]["principal"], c["caller"]["audiences"]),
            as_of=date.fromisoformat(c["as_of"]), host=host,
            records=[(r["written_by"], r["record"]) for r in c["records"]],
            expect=c["expect"]))
    return cases


def score(case: Case, outcome: str, answer_id: Optional[str], payload: Any) -> dict[str, Any]:
    """Correct outcome and answer, and zero bytes of withheld content anywhere in the payload."""
    text = json.dumps(payload, ensure_ascii=False, default=str)
    leaked = [s for s in case.expect.get("forbidden", []) if s in text]
    correct = outcome == case.expect["outcome"] and answer_id == case.expect.get("record_id")
    return {"case": case.id, "outcome": outcome, "answer": answer_id, "correct": correct,
            "leaked": leaked, "pass": correct and not leaked}
