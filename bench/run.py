"""Adversarial conformance run on Graphiti.

    python bench/run.py                     # prints the table, writes bench/result.json

Arms, all over the same Graphiti graph:
  retrieval-top1             search_ with Graphiti's own time filter; the harness takes the top hit
                             as the answer and forwards every returned fact. A host policy, not
                             something Graphiti claims to do.
  governed-without-fields    the governed read, overlay fields removed, attestation kept
  governed-without-attest    the governed read, fields kept, writer attribution dropped
                             (envelope integrity is still verified)
  governed                   the governed read

Every case gets a fresh in-memory Graphiti (Kuzu). No LLM, no network, no telemetry.
"""
from __future__ import annotations

import asyncio
import json
import platform
import sys
from importlib.metadata import version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vercy_graphiti.fixture import load, score  # noqa: E402
from vercy_graphiti.offline import make_graphiti  # noqa: E402
from vercy_graphiti.store import GovernedGraphiti  # noqa: E402

KEY = b"vercy-bench-host-key"   # a fixed key: the run must be reproducible
ARMS = ("retrieval-top1", "governed-without-fields", "governed-without-attest", "governed")


def judged(case, decision):
    return score(case, decision.outcome, decision.answer.record_id if decision.answer else None,
                 decision.payload(), [f.record_id for f in decision.conflict])


async def run_case(case) -> dict:
    gg = GovernedGraphiti(make_graphiti(), host=case.host, key=KEY, group_id="bench")
    for written_by, record in case.records:
        await gg.write(record, written_by=written_by)
    kw = dict(concept=case.concept, caller=case.caller, as_of=case.as_of)

    answer, ranked = await gg.retrieve_top1(case.question, concept=case.concept, as_of=case.as_of)
    row = {"case": case.id, "expect": case.expect,
           "retrieval-top1": {**score(case, "answered" if answer else "empty", answer, ranked), "ranked": ranked},
           "governed-without-fields": judged(case, await gg.ask_without_fields(**kw)),
           "governed-without-attest": judged(case, await gg.ask_without_attestation(**kw))}
    d = await gg.ask(**kw)
    governed = judged(case, d)
    governed["reasons"] = sorted({r["code"] for r in d.reasons})
    governed["reasons_expected_present"] = all(r in governed["reasons"] for r in case.expect["reasons"])
    governed["pass"] = governed["pass"] and governed["reasons_expected_present"]
    row["governed"] = governed
    return row


async def main() -> int:
    cases = load(ROOT / "fixtures" / "adversarial.json")
    rows = [await run_case(c) for c in cases]
    totals = {arm: {"pass": sum(r[arm]["pass"] for r in rows),
                    "exposed": sum(bool(r[arm]["leaked"]) for r in rows),
                    "cases": len(rows)} for arm in ARMS}
    result = {
        "benchmark": "vercy-graphiti-adversarial/2",
        "fixture": "fixtures/adversarial.json",
        "what_it_is": "A conformance fixture written from the Vercy enforcement contract. Not a quality "
                      "benchmark of Graphiti, which does not claim to enforce ownership or disclosure.",
        "exposed_means": "a listed withheld string appears in the payload the arm would forward to a model",
        "environment": {"graphiti-core": version("graphiti-core"), "kuzu": version("kuzu"),
                        "python": platform.python_version(), "llm": "none",
                        "embedder": "sha256 token hashing, 256 dims (retrieval-top1 ranking depends on it)",
                        "governed_read": "every edge of the concept node, EntityEdge.get_by_node_uuid"},
        "totals": totals,
        "cases": rows,
    }
    (ROOT / "bench" / "result.json").write_text(json.dumps(result, indent=1, default=str, ensure_ascii=False) + "\n",
                                                encoding="utf-8")
    width = max(len(r["case"]) for r in rows)
    print(f"{'case':{width}}  " + "  ".join(f"{a:>24}" for a in ARMS))
    for r in rows:
        cells = []
        for arm in ARMS:
            s = r[arm]
            mark = "pass" if s["pass"] else ("EXPOSED " if s["leaked"] else "") + f"{s['outcome']}/{s['answer']}"
            cells.append(f"{mark:>24}")
        print(f"{r['case']:{width}}  " + "  ".join(cells))
    print(f"{'total':{width}}  " + "  ".join(
        f"{str(totals[a]['pass']) + '/' + str(len(rows)) + ', exposed ' + str(totals[a]['exposed']):>24}" for a in ARMS))
    return 0 if totals["governed"]["pass"] == len(rows) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
