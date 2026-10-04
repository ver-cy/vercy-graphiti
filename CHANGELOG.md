# Changelog

## 0.1.0 - unreleased

- `enforce`: the Vercy enforcement contract draft 0.3 as a store-agnostic engine.
- `GovernedGraphiti`: overlay records as Graphiti edges, writer attested by a host HMAC,
  governed `ask` over `search_`, plus the native and field-stripped arms for comparison.
- Offline harness: Graphiti on embedded Kuzu with no LLM, no network and no telemetry.
- Adversarial fixture (8 cases) and `bench/run.py` with four arms: retrieval top hit, without fields, without attestation, governed.
