# vercy-graphiti

Graphiti already keeps the time half of a governed fact: every edge carries `valid_at` and
`invalid_at`. This package adds the other half, as edge attributes and a read path:

- **who owns the concept**, checked against the host's ownership register. Each edge carries a signed
  envelope: the host signs the group, the edge id, the record and the writer with its own key, so a
  record cannot make itself authoritative, move to another concept, or shed its `release_to`.
- **which record wins** when records disagree, by a written rule, or an explicit abstention.
- **who may see it**, applied last, with no fallback to an older value and nothing from a withheld
  record in the payload.

It implements the [Vercy enforcement contract](https://github.com/ver-cy/vercy-py/blob/main/ENFORCEMENT-CONTRACT.md),
draft 0.4, using only Graphiti's public `EntityEdge`, `EntityNode` and `search_` APIs.

## Result

Eight adversarial cases written from the contract, each with a fixed expected outcome. Four arms over
the same Graphiti graph.

| Case | Retrieval, top hit | Without fields | Without writer attribution | Governed |
|---|---|---|---|---|
| Forged authority | other answer | pass | other answer | pass |
| Forged supersession | other answer | pass | other answer | pass |
| Unauthorized retrieval | exposed | exposed | pass | pass |
| No fallback to a superseded value | exposed | exposed | pass | pass |
| Hidden side of a conflict | exposed | exposed | pass | pass |
| Unresolved conflict | picks one | pass | pass | pass |
| Expired truth | pass | pass | pass | pass |
| Laundered fact | pass | pass | other answer | pass |
| **Cases passed** | **2 of 8** | **5 of 8** | **5 of 8** | **8 of 8** |
| **Restricted content exposed** | **3** | **3** | **0** | **0** |

The table is the embedded-Kuzu run. CI repeats it on Neo4j, Graphiti's primary backend: the three
governed columns are identical (5, 5 and 8 of 8; 3, 3 and 0 exposed), and the top-hit column passes
3 of 8 with 3 exposed, on different cases, because its answer depends on ranking. On both backends
every exposure in the top-hit column is in the chosen answer itself, not further down the list.

- **Retrieval, top hit** is a host policy, not Graphiti behaviour: `search_` with Graphiti's own
  bitemporal filter, the top hit taken as the answer and every returned fact forwarded to the model.
  Graphiti does not claim to enforce ownership or disclosure; this column shows what a host gets
  without something that does.
- **Without fields** keeps the host's ownership register and writer attestation and removes the overlay
  fields. Attestation alone settles the authority cases and exposes restricted content.
- **Without writer attribution** keeps the fields and drops who wrote each record; envelope integrity
  is still verified. The fields alone stop the exposure and lose the authority cases.
- On these eight authored cases, both configurations with one component removed pass 5 of 8; the
  complete configuration passes 8 of 8.

"Exposed" means one of the case's listed withheld strings appears in the payload that arm would forward.
The listed strings are the oracle; this is not a proof that nothing else could leak.

Reproduce, with no API key and no network after install (Python 3.10 to 3.13):

```bash
git clone --branch v0.1.0 https://github.com/ver-cy/vercy-graphiti && cd vercy-graphiti
pip install ".[offline]"
python bench/run.py
```

The adapter alone is `pip install vercy-graphiti`.

The run writes `bench/result.json`, including the ranked candidates the top-hit arm saw. The top-hit
arm reads a ranked list from `search_`; the three governed arms read every edge of the concept node.
The harness uses a deterministic hashing embedder, so the top-hit column can change with a real
embedder. The governed columns cannot.

## Use

```python
from datetime import date
from vercy_graphiti import Caller, Host
from vercy_graphiti.store import GovernedGraphiti

gg = GovernedGraphiti(graphiti, host=Host(owners={"arr-definition": "finance"}), key=HOST_SECRET)

# The host's authenticated write path decides written_by, never the record.
await gg.write({"record_id": "ARR-1", "concept": "arr-definition",
                "value": "ARR means committed subscription value over the next twelve months",
                "valid_from": "2026-04-01", "valid_to": None, "source": "FIN-POL-07"},
               written_by="finance")

decision = await gg.ask(concept="arr-definition",
                        caller=Caller.of("analyst", ["staff"]), as_of=date(2026, 9, 1))
decision.payload()   # outcome, answer, reason codes; safe to give to a model
```

Outcomes are `answered`, `abstained`, `refused` or `empty`, with reason codes such as
`unauthorized_precedence`, `superseded`, `not_released`, `expired` and `integrity_failed`. A refusal
says how many relevant records were withheld and which rule applied, never which records.

**Records are immutable.** Writing an existing `record_id` raises `RecordExists`; a new version is a
new record that `supersedes` the old one. This holds for **one `GovernedGraphiti` writer instance per
group**: the check and the save run under that instance's lock. Graphiti's edge save is an upsert, so
several writer processes or instances need a uniqueness guarantee from the host. **Integrity fails closed.** If any governance edge of a
concept has an envelope that does not verify, the concept answers `refused` with `integrity_failed`
rather than deciding over the rest.

**What is governed.** Only `ask(...).payload()`. Anything else a host exposes from the same graph
(search results, graph walks, `get_by_uuid`, episodes, community summaries) is not filtered by this
package. The engine in `vercy_graphiti.enforce` has no dependencies and no Graphiti import; it can sit
over any store that can return every record for a concept.

## Notes for Graphiti on Kuzu

Two things the offline harness works around in graphiti-core 0.30.2: the Kuzu driver creates its
schema but not its full-text indices, so `search_` fails until they are created; and
`graphiti_core.llm_client` imports `httpx`, which the package does not declare.

Apache-2.0. No telemetry: the harness sets `GRAPHITI_TELEMETRY_ENABLED=false`, and this package sends
nothing anywhere.
