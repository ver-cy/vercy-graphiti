"""Governed reads and writes over a Graphiti instance.

    gg = GovernedGraphiti(graphiti, host=Host(owners={...}), key=host_secret)
    await gg.write(record, written_by="finance")          # the host's authenticated write path
    decision = await gg.ask("What does ARR mean?", concept="arr-definition",
                            caller=Caller.of("analyst", ["staff"]), as_of=date(2026, 9, 1))
    decision.payload()                                    # safe to hand to a model

Covered retrieval path: `Graphiti.search_` with the edge RRF recipe. Other paths (graph
walks, `get_by_uuid`, episode reads, community summaries) are not filtered by this
wrapper; a host that exposes them to a caller is not enforcing the overlay on them.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

from .enforce import Caller, Decision, Host, decide
from .mapping import EDGE_NAME, edge_fields, fact_of, record_of, stripped_fact

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
        self._nodes: dict[str, Any] = {}

    async def _node(self, name: str):
        from graphiti_core.nodes import EntityNode
        if name not in self._nodes:
            node = EntityNode(name=name, group_id=self.group_id, labels=["Entity"],
                              created_at=datetime.now(timezone.utc), summary="")
            await node.generate_name_embedding(self.graphiti.embedder)
            await node.save(self.graphiti.driver)
            self._nodes[name] = node
        return self._nodes[name]

    async def write(self, record: dict[str, Any], written_by: str):
        """Store one overlay record. `written_by` must come from the host's authenticated session."""
        from graphiti_core.edges import EntityEdge
        for required in ("record_id", "concept", "value"):
            if not record.get(required):
                raise ValueError(f"record needs {required!r}")
        concept = await self._node(str(record["concept"]))
        source = await self._node(str(record.get("source") or "unknown source"))
        edge = EntityEdge(source_node_uuid=concept.uuid, target_node_uuid=source.uuid,
                          created_at=datetime.now(timezone.utc),
                          **edge_fields(record, written_by, self.key, self.group_id))
        await edge.generate_embedding(self.graphiti.embedder)
        await edge.save(self.graphiti.driver)
        return edge

    async def search(self, question: str, search_filter: Any = None, limit: int = SEARCH_LIMIT) -> list:
        results = await self.graphiti.search_(question, config=_recipe(limit), group_ids=[self.group_id],
                                              search_filter=search_filter)
        return list(results.edges)

    async def ask(self, question: str, *, concept: str, caller: Caller, as_of: date,
                  scope: Iterable[str] = ()) -> Decision:
        """The governed read. Retrieval is unfiltered on purpose: rules decide, then disclosure."""
        edges = await self.search(question)
        facts = [f for f in (fact_of(e.attributes, self.key) for e in edges) if f is not None]
        return decide(facts, concept=concept, caller=caller, as_of=as_of, host=self.host, scope=scope)

    async def ask_stripped(self, question: str, *, concept: str, caller: Caller, as_of: date) -> Decision:
        """Ablation: the same engine over the same edges with the overlay fields removed."""
        edges = await self.search(question)
        facts = [f for f in (stripped_fact(e.attributes) for e in edges) if f is not None]
        return decide(facts, concept=concept, caller=caller, as_of=as_of, host=self.host)

    async def ask_native(self, question: str, *, concept: str, as_of: date) -> tuple[Optional[str], list[dict]]:
        """Baseline: Graphiti's own retrieval with its own time filter, top hit as the answer.

        Returns (answer record id, payload). The payload is what a host would hand a model:
        every returned fact, as Graphiti returns it.
        """
        edges = await self.search(question, search_filter=time_filter(as_of))
        payload = []
        answer = None
        for e in edges:
            record = record_of(e.attributes) or {}
            if record.get("concept") != concept:
                continue
            payload.append({"fact": e.fact, "record_id": record.get("record_id"), "source": record.get("source"),
                            "valid_at": e.valid_at, "invalid_at": e.invalid_at})
            if answer is None:
                answer = record.get("record_id")
        return answer, payload
