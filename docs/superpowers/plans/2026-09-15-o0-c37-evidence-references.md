# O0-C37 Evidence References Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist evidence once and transmit only stable ID/SHA-256 references bound to the active audit.

**Architecture:** Canonical JSON envelopes live under `reports/evidence`. The runner creates them exclusively and resolves every reference against state before an actor runs.

**Tech Stack:** Python standard library, JSON Schema 2020-12, unittest.

**Spec:** `docs/superpowers/specs/2026-09-15-o0-c37-evidence-references-design.md`

## Global Constraints

- Base SHA: `8a98790b241247baf271559b7a9a42609e82109b`.
- Keep O0 partial; do not start O0-C38, Gate S1 or S2.
- Store locally; no remote retention, garbage collection or signatures.
- Produce one isolated functional commit after all tests pass.

---

### Task 1: Canonical evidence store

**Files:**
- Create: `schemas/evidence-envelope.schema.json`
- Modify: `scripts/o0_runner.py`
- Modify: `scripts/orchestrate_handoffs.py`
- Modify: `scripts/validate_standard.py`
- Test: `scripts/test_o0_runner.py`

**Interfaces:**
- Produces: `store_evidence(root, *, evidence_id, content, current) -> dict[str, str]`
- Produces: `resolve_evidence(root, reference, current) -> dict`

- [x] Write tests proving deterministic creation/resolution, exclusive IDs, missing files, changed bytes, unsafe IDs and wrong `run_id`/round/SHA.
- [x] Run targeted tests; expect failure because the store functions/schema do not exist.
- [x] Implement canonical serialization and digest:

```python
canonical = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
digest = hashlib.sha256(canonical).hexdigest()
reference = {"evidence_id": evidence_id, "sha256": digest}
```

- [x] Resolve only `<root>/<evidence_id>.json`; validate schema, canonical bytes, digest and all state bindings.
- [x] Run targeted tests; expect PASS.

### Task 2: Reference-only handoffs

**Files:**
- Modify: `schemas/builder-findings.schema.json`
- Modify: `schemas/reaudit-handoff.schema.json`
- Modify: `scripts/o0_runner.py`
- Test: `scripts/test_o0_runner.py`

**Interfaces:**
- Consumes: `store_evidence(...)`, `resolve_evidence(...)`
- Produces: handoff evidence objects shaped only as `{"evidence_id": str, "sha256": str}`

- [x] Write tests proving handoffs omit evidence content, preserve critical references, and reject absent/divergent/substituted/cross-run evidence before actor execution without state mutation.
- [x] Run targeted tests; expect failure because handoffs still embed evidence text.
- [x] Externalize finding/check evidence while building handoffs; resolve all references during pre-actor validation.
- [x] Keep findings/check obligations intact and transmit additional context only through existing `context_mode`/reasons.
- [x] Run targeted tests; expect PASS.

### Task 3: State and verification

**Files:**
- Modify: `PROJECT_STATE.md`
- Modify: `ROADMAP.md`

- [x] Record the reported O0-C36 PASS at its exact SHA; mark only O0-C37 implemented and awaiting audit.
- [x] Run `wsl python3 -m unittest scripts.test_o0_runner scripts.test_orchestrate_handoffs`.
- [x] Run `python -m unittest scripts.test_validate_standard` and `python scripts/validate_standard.py --self-check`.
- [x] Run `git diff --check`; confirm O0-C38, Gate S1 and S2 unchanged.
- [x] Commit all O0-C37 files once with `git commit -m "feat(o0): add verifiable evidence references"` and publish the branch SHA.
