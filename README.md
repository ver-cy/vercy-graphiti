# vercy-graphiti

Graphiti already keeps the time half of a governed fact: every edge carries `valid_at` and
`invalid_at`. This package adds the other half, as edge attributes and a read path:

- **who owns the concept**, checked against the host's ownership register, with the writer of each
  edge attested by a host-keyed HMAC so a record cannot make itself authoritative;
- **which record wins** when records disagree, by a written rule, or an explicit abstention;
- **who may see it**, applied last, with no fallback to an older value and nothing from a withheld
  record in the payload.

It implements the [Vercy enforcement contract](https://github.com/ver-cy/vercy-py/blob/main/ENFORCEMENT-CONTRACT.md),
draft 0.3. Graphiti itself is unchanged: the package uses its public `EntityEdge` and `search_` APIs.

## Result

Eight adversarial cases, each with a fixed expected outcome. Three arms over the same Graphiti graph:
Graphiti's own retrieval with its own bitemporal filter; the governed read with the overlay fields
stripped; the governed read with the fields.

| Case | Graphiti native | Governed, fields stripped | Governed |
|---|---|---|---|
| Forged authority | wrong answer | wrong answer | pass |
| Forged supersession | wrong answer | wrong answer | pass |
| Unauthorized retrieval | **leak** | **leak** | pass |
| No fallback to a superseded value | **leak** | **leak** | pass |
| Hidden side of a conflict | **leak** | **leak** | pass |
| Unresolved conflict | silent pick | pass | pass |
| Expired truth | pass | pass | pass |
| Laundered fact | pass | wrong answer | pass |
| **Total** | **2 of 8, 3 leaks** | **2 of 8, 3 leaks** | **8 of 8, 0 leaks** |

Reproduce, with no API key and no network after install:

```bash
pip install "vercy-graphiti[offline]"
python bench/run.py
```

Read this table for what it is:

- **It is a conformance fixture, not a quality benchmark.** We wrote the cases from the contract.
  Graphiti does not claim to enforce ownership or disclosure, so its native column shows what a host
  gets without these fields, not a defect in Graphiti.
- **Ranking matters for the native column only.** The harness uses a deterministic hashing embedder
  so the run is reproducible. With a real embedder the native top hit can change, for example on
  "laundered fact". The governed column does not depend on ranking, only on which records retrieval
  returns.
- **One retrieval path is covered**: `Graphiti.search_` with the edge RRF recipe. Graph walks,
  `get_by_uuid`, episode reads and community summaries are not filtered by this package.

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

decision = await gg.ask("What does ARR mean?", concept="arr-definition",
                        caller=Caller.of("analyst", ["staff"]), as_of=date(2026, 9, 1))
decision.payload()   # outcome, answer, reason codes; safe to give to a model
```

Outcomes are `answered`, `abstained`, `refused` or `empty`, with reason codes such as
`unauthorized_precedence`, `superseded`, `not_released` and `expired`. A refusal says how many records
were withheld and why, never which ones.

The engine in `vercy_graphiti.enforce` has no dependencies and no Graphiti import; it can sit over any
store that returns candidate records.

## Notes for Graphiti on Kuzu

Two things the offline harness works around, both in graphiti-core 0.30.2: the Kuzu driver creates
its schema but not its full-text indices, so `search_` fails until they are created; and
`graphiti_core.llm_client` imports `httpx`, which the package does not declare.

Apache-2.0. No telemetry: the harness sets `GRAPHITI_TELEMETRY_ENABLED=false`, and this package sends
nothing anywhere.
