"""Adversarial run on Graphiti: native, governed without the fields, governed with them.

    python bench/run.py                     # prints the table, writes bench/result.json

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

KEY = b"vercy-bench-host-key"   # a fixed key: the run must be reproducible byte for byte
ARMS = ("graphiti-native", "governed-without-fields", "governed")


async def run_case(case) -> dict:
    gg = GovernedGraphiti(make_graphiti(), host=case.host, key=KEY, group_id="bench")
    for written_by, record in case.records:
        await gg.write(record, written_by=written_by)

    answer, payload = await gg.ask_native(case.question, concept=case.concept, as_of=case.as_of)
    native = score(case, "answered" if answer else "empty", answer, payload)

    d = await gg.ask_stripped(case.question, concept=case.concept, caller=case.caller, as_of=case.as_of)
    stripped = score(case, d.outcome, d.answer.record_id if d.answer else None, d.payload())

    d = await gg.ask(case.question, concept=case.concept, caller=case.caller, as_of=case.as_of)
    governed = score(case, d.outcome, d.answer.record_id if d.answer else None, d.payload())
    governed["reasons"] = sorted({r["code"] for r in d.reasons})
    governed["reasons_expected_present"] = all(r in governed["reasons"] for r in case.expect["reasons"])
    governed["pass"] = governed["pass"] and governed["reasons_expected_present"]
    return {"case": case.id, "expect": case.expect, "graphiti-native": native,
            "governed-without-fields": stripped, "governed": governed}


async def main() -> int:
    cases = load(ROOT / "fixtures" / "adversarial.json")
    rows = [await run_case(c) for c in cases]
    totals = {arm: {"pass": sum(r[arm]["pass"] for r in rows),
                    "leaks": sum(bool(r[arm]["leaked"]) for r in rows),
                    "cases": len(rows)} for arm in ARMS}
    result = {
        "benchmark": "vercy-graphiti-adversarial/1",
        "fixture": "fixtures/adversarial.json",
        "environment": {"graphiti-core": version("graphiti-core"), "kuzu": version("kuzu"),
                        "python": platform.python_version(), "llm": "none", "embedder": "sha256 token hashing, 256 dims",
                        "retrieval_path_covered": "Graphiti.search_ with EDGE_HYBRID_SEARCH_RRF"},
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
            mark = "pass" if s["pass"] else ("LEAK " if s["leaked"] else "") + f"fail ({s['outcome']}/{s['answer']})"
            cells.append(f"{mark:>24}")
        print(f"{r['case']:{width}}  " + "  ".join(cells))
    print(f"{'total':{width}}  " + "  ".join(
        f"{str(totals[a]['pass']) + '/' + str(len(rows)) + ', leaks ' + str(totals[a]['leaks']):>24}" for a in ARMS))
    return 0 if totals["governed"]["pass"] == len(rows) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
