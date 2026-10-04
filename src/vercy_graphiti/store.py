"""Governed reads and writes over a Graphiti instance.

    gg = GovernedGraphiti(graphiti, host=Host(owners={...}), key=host_secret)
    await gg.write(record, written_by="finance")          # the host's authenticated write path
    decision = await gg.ask(concept="arr-definition",
                            caller=Caller.of("analyst", ["staff"]), as_of=date(2026, 9, 1))
    decision.payload()                                    # safe to hand to a model

The governed read does not depend on search ranking: it loads every edge of the
concept node and decides over all of them. Only `ask(...).payload()` is governed.
Anything else a host exposes from the same graph (search results, graph walks,
`get_by_uuid`, episodes, community summaries) is not filtered by this package.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable, Iterable, Optional

from .enforce import Caller, Decision, Fact, Host, decide
from .mapping import (EDGE_NAME, OK, TAMPERED, edge_fields, fact_without_attestation,
                      fact_without_fields, governed_fact, node_uuid, record_of, verify)

SEARCH_LIMIT = 25


def _recipe(limit: int):
    from graphiti_core.search.search_config_recipes import EDGE_HYBRID_SEARCH_RRF
    config = EDGE_HYBRID_SEARCH_RRF.model_copy(deep=True)
    config.limit = limit
    return config


def time_filter(as_of: date):
    """Graphiti's own bitemporal filter: valid_at <= as_of and (invalid_at is null or >= as_of)."""
    from graphiti_core.search.search_filters import ComparisonOperator as Op, DateFilter, SearchFilters
    at = datetime(as_of.year, as_of.month, as_of.day, tzinfo=timezone.utc)
    return SearchFilters(
        edge_types=[EDGE_NAME],
        valid_at=[[DateFilter(date=at, comparison_operator=Op.less_than_equal)]],
        invalid_at=[[DateFilter(comparison_operator=Op.is_null)],
                    [DateFilter(date=at, comparison_operator=Op.greater_than_equal)]],
    )


class GovernedGraphiti:
    def __init__(self, graphiti: Any, host: Host, key: bytes, group_id: str = "vercy"):
        if not key:
            raise ValueError("a host key is required to attest writers")
        self.graphiti, self.host, self.key, self.group_id = graphiti, host, key, group_id

    async def _node(self, kind: str, name: str):
        from graphiti_core.errors import NodeNotFoundError
        from graphiti_core.nodes import EntityNode
        uid = node_uuid(self.group_id, kind, name)
        try:
            return await EntityNode.get_by_uuid(self.graphiti.driver, uid)
        except NodeNotFoundError:
            node = EntityNode(uuid=uid, name=name, group_id=self.group_id, labels=["Entity"],
                              created_at=datetime.now(timezone.utc), summary="")
            await node.generate_name_embedding(self.graphiti.embedder)
            await node.save(self.graphiti.driver)
            return node

    async def write(self, record: dict[str, Any], written_by: str):
        """Store one overlay record. `written_by` must come from the host's authenticated session."""
        from graphiti_core.edges import EntityEdge
        fields = edge_fields(record, written_by, self.key, self.group_id)     # validates the record
        concept = await self._node("concept", record["concept"])
        source = await self._node("source", str(record.get("source") or "unknown source"))
        edge = EntityEdge(source_node_uuid=concept.uuid, target_node_uuid=source.uuid,
                          created_at=datetime.now(timezone.utc), **fields)
        await edge.generate_embedding(self.graphiti.embedder)
        await edge.save(self.graphiti.driver)
        return edge

    async def _all_for(self, concept: str) -> list:
        """Every edge of the concept node. Raises on a store error: no partial decisions."""
        from graphiti_core.edges import EntityEdge
        edges = await EntityEdge.get_by_node_uuid(self.graphiti.driver, node_uuid(self.group_id, "concept", concept))
        return [e for e in edges if e.name == EDGE_NAME and e.group_id == self.group_id]

    async def _decide(self, concept: str, caller: Caller, as_of: date, scope: Iterable[str],
                      build: Callable[[dict, str], Fact]) -> Decision:
        facts, failures = [], 0
        for e in await self._all_for(concept):
            status, record, writer = verify(e.attributes, self.key, self.group_id, e.uuid)
            if status == OK:
                facts.append(build(record, writer))
            elif status == TAMPERED:
                failures += 1
        return decide(facts, concept=concept, caller=caller, as_of=as_of, host=self.host,
                      scope=scope, integrity_failures=failures)

    async def ask(self, *, concept: str, caller: Caller, as_of: date, scope: Iterable[str] = ()) -> Decision:
        """The governed read over every record of the concept."""
        return await self._decide(concept, caller, as_of, scope, governed_fact)

    async def ask_without_fields(self, *, concept: str, caller: Caller, as_of: date) -> Decision:
        """Ablation: same records, same attestation, overlay fields removed."""
        return await self._decide(concept, caller, as_of, (), fact_without_fields)

    async def ask_without_attestation(self, *, concept: str, caller: Caller, as_of: date) -> Decision:
        """Ablation: same records and fields, writer unknown."""
        return await self._decide(concept, caller, as_of, (), fact_without_attestation)

    async def retrieve_top1(self, question: str, *, concept: str, as_of: date,
                            limit: int = SEARCH_LIMIT) -> tuple[Optional[str], list[dict]]:
        """Baseline host policy, not Graphiti's: search_ with Graphiti's own time filter, take the
        top hit as the answer and forward every returned fact. Returns (answer id, ranked payload)."""
        results = await self.graphiti.search_(question, config=_recipe(limit), group_ids=[self.group_id],
                                              search_filter=time_filter(as_of))
        payload, answer = [], None
        for rank, e in enumerate(results.edges, 1):
            record = record_of(e.attributes) or {}
            if record.get("concept") != concept:
                continue
            payload.append({"rank": rank, "fact": e.fact, "record_id": record.get("record_id"),
                            "source": record.get("source")})
            if answer is None:
                answer = record.get("record_id")
        return answer, payload
