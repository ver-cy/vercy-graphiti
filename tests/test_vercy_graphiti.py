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
from vercy_graphiti.mapping import OK, TAMPERED, edge_fields, sign, validate, verify  # noqa: E402

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


def codes(d):
    return {r["code"] for r in d.reasons}


class Engine(unittest.TestCase):
    def test_fixture_oracle(self):
        for case in CASES:
            with self.subTest(case.id):
                d = decide(case.facts(), concept=case.concept, caller=case.caller, as_of=case.as_of, host=case.host)
                s = score(case, d.outcome, d.answer.record_id if d.answer else None, d.payload(),
                          [f.record_id for f in d.conflict])
                self.assertTrue(s["pass"], s)
                self.assertTrue(set(case.expect["reasons"]) <= codes(d), codes(d))

    def test_writer_comes_from_the_host_not_the_record(self):
        forged = Fact.from_record({"record_id": "x", "concept": "c", "value": "v", "valid_from": "2026-01-01",
                                   "valid_to": None, "written_by": "owner", "concept_owner": "owner"})
        self.assertIsNone(forged.written_by)

    def test_mutual_supersession_is_a_conflict(self):
        a, b = fact("a", "one", supersedes=["b"]), fact("b", "two", supersedes=["a"])
        d = decide([a, b], concept="c", caller=Caller.of("x"), as_of=ASOF, host=Host({"c": "owner"}, "abstain"))
        self.assertEqual(d.outcome, "abstained")
        self.assertEqual([f.record_id for f in d.conflict], ["a", "b"])

    def test_three_record_cycle_never_empties_the_answer(self):
        a, b, c = fact("a", "one", supersedes=["b"]), fact("b", "two", supersedes=["c"]), fact("c", "three", supersedes=["a"])
        d = decide([a, b, c], concept="c", caller=Caller.of("x"), as_of=ASOF, host=Host({"c": "owner"}, "abstain"))
        self.assertEqual(d.outcome, "abstained")
        self.assertEqual(len(d.conflict), 3)
        self.assertIn("supersession_cycle", codes(d))

    def test_unknown_validity_cannot_supersede_and_loses_ties(self):
        known = fact("a", "one")
        unknown = Fact.from_record({"record_id": "b", "concept": "c", "value": "two", "valid_from": "2026-01-01",
                                    "supersedes": ["a"]}, written_by="owner")       # no valid_to key
        for policy in ("latest_valid_from", "abstain"):
            d = decide([known, unknown], concept="c", caller=Caller.of("x"), as_of=ASOF, host=Host({"c": "owner"}, policy))
            self.assertEqual(d.answer.record_id if d.answer else None, "a", policy)
            self.assertNotIn("superseded", codes(d))

    def test_no_authoritative_record_is_flagged(self):
        d = decide([fact("a", "one", written_by="someone")], concept="c", caller=Caller.of("x"), as_of=ASOF, host=HOST)
        self.assertEqual(d.outcome, "answered")
        self.assertIn("no_authoritative_record", codes(d))

    def test_restricted_without_release_is_released_to_nobody(self):
        d = decide([fact("a", "one", classification="internal")], concept="c",
                   caller=Caller.of("owner"), as_of=ASOF, host=HOST)
        self.assertEqual(d.outcome, "refused")
        self.assertIn("restricted_without_release", codes(d))
        self.assertNotIn('"a"', json.dumps(d.payload()))

    def test_hidden_reasons_carry_no_counts_or_ids(self):
        visible_owner = fact("a", "one")
        hidden = fact("h", "two", written_by="intruder", supersedes=["a", "a"], release_to=["board"])
        d = decide([visible_owner, hidden], concept="c", caller=Caller.of("x"), as_of=ASOF, host=HOST)
        self.assertEqual(d.answer.record_id, "a")
        hidden_reasons = [r for r in d.reasons if r["code"] == "unauthorized_supersession"]
        self.assertEqual(hidden_reasons, [{"code": "unauthorized_supersession", "record_id": None}])
        self.assertEqual(d.withheld, 1)
        self.assertNotIn('"h"', json.dumps(d.payload()))

    def test_scope(self):
        d = decide([fact("a", "one", does_not_apply_to=["EU"])], concept="c", caller=Caller.of("x"),
                   as_of=ASOF, host=HOST, scope=["EU"])
        self.assertEqual(d.outcome, "empty")


class Envelope(unittest.TestCase):
    record = {"record_id": "a", "concept": "c", "value": "v", "valid_from": "2026-01-01", "valid_to": None,
              "release_to": ["board"]}

    def test_verifies_only_in_its_own_group_and_edge(self):
        f = edge_fields(self.record, "owner", b"k", "g")
        self.assertEqual(verify(f["attributes"], b"k", "g", f["uuid"])[0], OK)
        self.assertEqual(verify(f["attributes"], b"other key", "g", f["uuid"])[0], TAMPERED)
        self.assertEqual(verify(f["attributes"], b"k", "other group", f["uuid"])[0], TAMPERED)
        self.assertEqual(verify(f["attributes"], b"k", "g", "another-edge")[0], TAMPERED)

    def test_shedding_release_to_rejects_the_whole_record(self):
        f = edge_fields(self.record, "owner", b"k", "g")
        bare = {k: v for k, v in self.record.items() if k != "release_to"}
        tampered = dict(f["attributes"], vercy_record=json.dumps(bare))
        status, record, writer = verify(tampered, b"k", "g", f["uuid"])
        self.assertEqual((status, record, writer), (TAMPERED, None, None))

    def test_writer_is_signed(self):
        self.assertNotEqual(sign(b"k", "g", "e", self.record, "owner"), sign(b"k", "g", "e", self.record, "owner2"))

    def test_strict_input(self):
        for bad in ({**self.record, "valid_from": "1 Jan 2026"}, {**self.record, "release_to": [1]},
                    {**self.record, "value": 3.5}, {k: v for k, v in self.record.items() if k != "concept"}):
            with self.assertRaises(ValueError):
                validate(bad)


@unittest.skipUnless(HAVE_GRAPHITI, "graphiti-core[kuzu] not installed")
class OnGraphiti(unittest.TestCase):
    def setUp(self):
        from vercy_graphiti.offline import make_graphiti
        from vercy_graphiti.store import GovernedGraphiti
        self.make = lambda host, key=b"k", group="t": GovernedGraphiti(make_graphiti(), host=host, key=key, group_id=group)

    def test_fixture_through_graphiti(self):
        async def go():
            out = []
            for case in CASES:
                gg = self.make(case.host)
                for written_by, record in case.records:
                    await gg.write(record, written_by=written_by)
                d = await gg.ask(concept=case.concept, caller=case.caller, as_of=case.as_of)
                out.append(score(case, d.outcome, d.answer.record_id if d.answer else None, d.payload(),
                                 [f.record_id for f in d.conflict]))
            return out
        for s in asyncio.run(go()):
            self.assertTrue(s["pass"], s)

    def test_governed_read_does_not_depend_on_ranking(self):
        """The restricted successor is outranked by 40 decoys and still governs the answer."""
        async def go():
            gg = self.make(Host(owners={"discount": "ops", "noise": "ops"}))
            await gg.write({"record_id": "old", "concept": "discount", "value": "renewal discount up to 15 percent",
                            "valid_from": "2025-01-01", "valid_to": None}, written_by="ops")
            await gg.write({"record_id": "new", "concept": "discount", "value": "frozen",
                            "valid_from": "2026-07-01", "valid_to": None, "supersedes": ["old"],
                            "release_to": ["ops"]}, written_by="ops")
            for i in range(40):
                await gg.write({"record_id": f"n{i}", "concept": "noise", "value": f"renewal discount up to 15 percent note {i}",
                                "valid_from": "2025-01-01", "valid_to": None}, written_by="ops")
            top, ranked = await gg.retrieve_top1("renewal discount up to 15 percent", concept="discount",
                                                 as_of=date(2026, 9, 1), limit=25)
            d = await gg.ask(concept="discount", caller=Caller.of("rep"), as_of=date(2026, 9, 1))
            return ranked, d
        ranked, d = asyncio.run(go())
        self.assertNotIn("new", [r["record_id"] for r in ranked])     # outside the top 25
        self.assertEqual(d.outcome, "refused")                         # yet it still decides

    def test_nobody_can_overwrite_a_record(self):
        from vercy_graphiti.store import RecordExists

        async def go():
            gg = self.make(Host(owners={"c": "owner"}))
            await gg.write({"record_id": "a", "concept": "c", "value": "owner value",
                            "valid_from": "2026-01-01", "valid_to": None}, written_by="owner")
            with self.assertRaises(RecordExists):
                await gg.write({"record_id": "a", "concept": "c", "value": "intruder value",
                                "valid_from": "2026-01-01", "valid_to": None}, written_by="intruder")
            return await gg.ask(concept="c", caller=Caller.of("x"), as_of=date(2026, 9, 1))
        d = asyncio.run(go())
        self.assertEqual(d.payload()["answer"]["value"], "owner value")

    def test_corrupted_successor_fails_closed_without_fallback(self):
        """Corrupt the restricted successor's envelope: the public predecessor must not answer."""
        from graphiti_core.edges import EntityEdge
        from vercy_graphiti.mapping import edge_uuid

        async def go():
            gg = self.make(Host(owners={"d": "ops"}))
            await gg.write({"record_id": "old", "concept": "d", "value": "up to 15 percent",
                            "valid_from": "2025-01-01", "valid_to": None}, written_by="ops")
            await gg.write({"record_id": "new", "concept": "d", "value": "frozen", "valid_from": "2026-07-01",
                            "valid_to": None, "supersedes": ["old"], "release_to": ["ops"]}, written_by="ops")
            edge = await EntityEdge.get_by_uuid(gg.graphiti.driver, edge_uuid("t", "new"))
            edge.attributes = {**edge.attributes, "vercy_record": "{}"}
            await edge.save(gg.graphiti.driver)
            return await gg.ask(concept="d", caller=Caller.of("rep"), as_of=date(2026, 9, 1))
        d = asyncio.run(go())
        self.assertEqual(d.outcome, "refused")
        self.assertIn("integrity_failed", codes(d))
        self.assertNotIn("15 percent", json.dumps(d.payload()))

    def test_edge_written_around_the_adapter_fails_closed(self):
        """A writer with raw graph access forges the owner's name. The envelope fails; nothing answers."""
        from datetime import datetime, timezone

        from graphiti_core.edges import EntityEdge
        from vercy_graphiti.mapping import node_uuid

        async def go():
            gg = self.make(Host(owners={"arr-definition": "finance"}), key=b"host key")
            await gg.write({"record_id": "ARR-1", "concept": "arr-definition", "value": "ARR means committed subscription value",
                            "valid_from": "2026-04-01", "valid_to": None, "source": "FIN-POL-07"}, written_by="finance")
            forged = {"record_id": "ARR-9", "concept": "arr-definition", "value": "ARR means bookings",
                      "valid_from": "2026-08-01", "valid_to": None, "priority": "high"}
            fields = edge_fields(forged, "finance", b"attacker guess", "t")
            concept = node_uuid("t", "concept", "arr-definition")
            edge = EntityEdge(source_node_uuid=concept, target_node_uuid=concept,
                              created_at=datetime.now(timezone.utc), **fields)
            await edge.generate_embedding(gg.graphiti.embedder)
            await edge.save(gg.graphiti.driver)
            return await gg.ask(concept="arr-definition", caller=Caller.of("analyst"), as_of=date(2026, 9, 1))
        d = asyncio.run(go())
        self.assertEqual(d.outcome, "refused")
        self.assertEqual(codes(d), {"integrity_failed"})


if __name__ == "__main__":
    unittest.main()
