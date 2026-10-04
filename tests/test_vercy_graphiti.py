"""Run: python -m unittest discover -s tests

The engine tests need nothing. The Graphiti tests run when graphiti-core and kuzu are installed.
"""
import asyncio
import json
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vercy_graphiti.enforce import Caller, Fact, Host, decide  # noqa: E402
from vercy_graphiti.fixture import load, score  # noqa: E402
from vercy_graphiti.mapping import edge_fields, fact_of, sign  # noqa: E402

CASES = load(ROOT / "fixtures" / "adversarial.json")
HOST = Host(owners={"c": "owner"})
ASOF = date(2026, 9, 1)

try:
    import graphiti_core  # noqa: F401
    import kuzu  # noqa: F401
    HAVE_GRAPHITI = True
except ImportError:
    HAVE_GRAPHITI = False


def fact(rid, value, written_by="owner", **kw):
    record = {"record_id": rid, "concept": "c", "value": value, "valid_from": "2026-01-01", "valid_to": None, **kw}
    return Fact.from_record(record, written_by=written_by)


class Engine(unittest.TestCase):
    def test_fixture_oracle(self):
        for case in CASES:
            with self.subTest(case.id):
                d = decide(case.facts(), concept=case.concept, caller=case.caller, as_of=case.as_of, host=case.host)
                s = score(case, d.outcome, d.answer.record_id if d.answer else None, d.payload())
                self.assertTrue(s["pass"], s)
                codes = {r["code"] for r in d.reasons}
                self.assertTrue(set(case.expect["reasons"]) <= codes, codes)
                if "conflict" in case.expect:
                    self.assertEqual([f.record_id for f in d.conflict], case.expect["conflict"])

    def test_writer_comes_from_the_host_not_the_record(self):
        forged = Fact.from_record({"record_id": "x", "concept": "c", "value": "v", "valid_from": "2026-01-01",
                                   "valid_to": None, "written_by": "owner", "concept_owner": "owner"})
        self.assertIsNone(forged.written_by)

    def test_mutual_supersession_is_a_conflict(self):
        a = fact("a", "one", supersedes=["b"])
        b = fact("b", "two", supersedes=["a"])
        d = decide([a, b], concept="c", caller=Caller.of("x"), as_of=ASOF, host=Host({"c": "owner"}, "abstain"))
        self.assertEqual(d.outcome, "abstained")

    def test_no_authoritative_record_is_flagged(self):
        d = decide([fact("a", "one", written_by="someone")], concept="c", caller=Caller.of("x"), as_of=ASOF, host=HOST)
        self.assertEqual(d.outcome, "answered")
        self.assertIn("no_authoritative_record", {r["code"] for r in d.reasons})

    def test_restricted_without_release_is_released_to_nobody(self):
        d = decide([fact("a", "one", classification="internal")], concept="c",
                   caller=Caller.of("owner"), as_of=ASOF, host=HOST)
        self.assertEqual(d.outcome, "refused")
        self.assertEqual(d.reasons[-1]["code"], "restricted_without_release")
        self.assertNotIn('"a"', json.dumps(d.payload()))

    def test_scope(self):
        d = decide([fact("a", "one", does_not_apply_to=["EU"])], concept="c", caller=Caller.of("x"),
                   as_of=ASOF, host=HOST, scope=["EU"])
        self.assertEqual(d.outcome, "empty")

    def test_validity_unknown_loses_ties(self):
        known = fact("a", "one")
        unknown = Fact.from_record({"record_id": "b", "concept": "c", "value": "two"}, written_by="owner")
        d = decide([known, unknown], concept="c", caller=Caller.of("x"), as_of=ASOF, host=HOST)
        self.assertEqual(d.answer.record_id, "a")

    def test_signature_is_checked(self):
        record = {"record_id": "a", "concept": "c", "value": "v"}
        good = edge_fields(record, "owner", b"k", "g")["attributes"]
        self.assertEqual(fact_of(good, b"k").written_by, "owner")
        self.assertIsNone(fact_of(good, b"other key").written_by)
        tampered = dict(good, vercy_record=json.dumps({**record, "value": "changed"}))
        self.assertIsNone(fact_of(tampered, b"k").written_by)
        self.assertNotEqual(sign(b"k", record, "owner"), sign(b"k", record, "owner2"))


@unittest.skipUnless(HAVE_GRAPHITI, "graphiti-core[kuzu] not installed")
class OnGraphiti(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def test_fixture_through_graphiti(self):
        from vercy_graphiti.offline import make_graphiti
        from vercy_graphiti.store import GovernedGraphiti

        async def go():
            out = []
            for case in CASES:
                gg = GovernedGraphiti(make_graphiti(), host=case.host, key=b"k", group_id="t")
                for written_by, record in case.records:
                    await gg.write(record, written_by=written_by)
                d = await gg.ask(case.question, concept=case.concept, caller=case.caller, as_of=case.as_of)
                out.append(score(case, d.outcome, d.answer.record_id if d.answer else None, d.payload()))
            return out
        for s in self.run_async(go()):
            self.assertTrue(s["pass"], s)

    def test_edge_written_around_the_adapter_is_not_authoritative(self):
        """A writer with raw access to the graph forges the owner's name. The signature fails."""
        from datetime import datetime, timezone

        from graphiti_core.edges import EntityEdge
        from vercy_graphiti.offline import make_graphiti
        from vercy_graphiti.store import GovernedGraphiti

        async def go():
            host = Host(owners={"arr-definition": "finance"})
            gg = GovernedGraphiti(make_graphiti(), host=host, key=b"host key", group_id="t")
            owner = {"record_id": "ARR-1", "concept": "arr-definition", "value": "ARR means committed subscription value",
                     "valid_from": "2026-04-01", "valid_to": None, "source": "FIN-POL-07"}
            await gg.write(owner, written_by="finance")
            forged = {"record_id": "ARR-9", "concept": "arr-definition", "value": "ARR means committed subscription bookings",
                      "valid_from": "2026-08-01", "valid_to": None, "source": "FIN-POL-07", "priority": "high"}
            fields = edge_fields(forged, "finance", b"attacker guess", "t")
            concept = await gg._node("arr-definition")
            edge = EntityEdge(source_node_uuid=concept.uuid, target_node_uuid=concept.uuid,
                              created_at=datetime.now(timezone.utc), **fields)
            await edge.generate_embedding(gg.graphiti.embedder)
            await edge.save(gg.graphiti.driver)
            return await gg.ask("What does ARR mean?", concept="arr-definition",
                                caller=Caller.of("analyst"), as_of=date(2026, 9, 1))
        d = self.run_async(go())
        self.assertEqual(d.answer.record_id, "ARR-1")
        self.assertIn("unauthorized_precedence", {r["code"] for r in d.reasons})


if __name__ == "__main__":
    unittest.main()
