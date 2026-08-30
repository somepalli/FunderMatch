# FunderMatch implementation plan

FunderMatch is a separate repository from FinDocIQ and consumes only its public
HTTP contract. The product is decision support: deterministic policy rules
exclude ineligible funders, precedent similarity ranks the eligible set, and a
human makes and audits the authoritative decision.

## Core loop

**Teach:** extract borrower A -> gate eligibility -> collect finance and ops
feedback -> record a human decision -> write the profile, comments, and decision
to Qdrant as a searchable precedent.

**Suggest:** extract similar borrower B -> gate eligibility -> retrieve case A ->
show its comments and outcome as precedent -> wait for a human decision -> write
the new decision back as precedent.

The borrower intake boundary accepts only requested amount and loan type. Borrower
identity, industry, sub-industry, region, PAT, DSCR, leverage, employee count, and all
other eligibility facts must come from cited FinDocIQ document evidence; no manual
fallback is permitted.

## Phase 0 — contract

Confirm FinDocIQ `POST /extract`. Implement an async `FinDocIQClient` that calls it
and validates local Pydantic copies of development contract `1.0` and
application-scoped production contract `2.0`. Keep those copies compatible with
FinDocIQ's generated public contract bundle through bidirectional CI. Every response
must contain structured figures with `(document_id, page_number, bbox)` provenance.

Exit criteria:

- FinDocIQ endpoint has focused API tests.
- FunderMatch accepts valid contract-v1 and contract-v2 fixtures and rejects missing
  provenance, ownership scope, policy identity, or required security receipts.
- No `findociq` import exists anywhere under FunderMatch `src/`.
- A smoke command can validate a running FinDocIQ instance.

## Phase 1 — synthetic corpus and schema

Define the approved-loan record schema and author 15–25 invented, internally
consistent cases. Load each case into Qdrant using `profile_vec` and
`comments_vec` named vectors.

## Phase 2 — retrieve and rule-gate

Evaluate hard eligibility rules before ranking Qdrant results. Return eligible
precedents with per-criterion rule results. A similar but ineligible funder must
be absent, not merely low-ranked.

## Phase 3 — suggestion assembly

Assemble candidate funders, precedent cases, finance/ops comments, past outcomes,
similarity explanations, and click-to-source figures. Produce a suggestion only;
never an approval or rejection.

## Phase 4 — HITL state machine

Implement durable states in Postgres. `AWAITING_HUMAN` resumes only through one
of four authenticated human actions: approve, reject, approve with conditions,
or send back. Every transition writes an audit row.

## Phase 5 — precedent write-back

Embed the human decision, overrides, and conditions and write the decided case
to Qdrant. Demonstrate case 1 being decided and case 2 retrieving it.

## Phase 6 — UI

Build three panels for borrower evidence, shortlist, and precedent. Add explicit
finance/ops feedback and human decision controls. Do not build a chat interface.

## Phase 7 — durable agent orchestration

Wrap the deterministic pipeline in a fixed LangGraph supervisor/worker sequence:
document processing, financial extraction, eligibility, precedent retrieval,
suggestion assembly, guardrail validation, and human-review handoff. Persist compact
application-scoped checkpoints in PostgreSQL and keep `workflow_cases` authoritative.

Add resume, cancel, and memory-status APIs; checkpoint after each worker and financial
metric substep; preserve stable command IDs and idempotent side-effect receipts. Human
review is a durable interrupt. Send-back may restart an explicitly selected worker, but
the graph cannot skip eligibility or guardrails and cannot make a lending decision.

Record content-safe Langfuse spans and local JSONL diagnostics for supervisor/worker
status, attempts, latency, model/config hashes, safe error codes, checkpoint identity,
interrupts, and resumes. Never export documents, financial text, rendered prompts,
answers, credentials, or checkpoint payloads.

Exit criteria:

- Restart and dependency-failure tests resume from every worker boundary.
- Completed workers and external side effects are not repeated.
- Cross-application checkpoint and evidence access is impossible.
- Human waiting survives API restart and all decisions remain role-protected.
- Trace and checkpoint leak scans find no prohibited content.

## Phase 8 — production security and release qualification

Publish and validate FinDocIQ development contract `1.0` and application-scoped
production contract `2.0` without importing FinDocIQ internals. Require short-lived
service JWTs, correlation IDs, document ownership, allow-listed metrics, matching policy
hashes, scan/DLP/encryption receipts, and provenance on every financial claim.

Use the integrated production Compose deployment as the local trust-boundary source:
only Caddy publishes a host port; FinDocIQ, PostgreSQL, Qdrant, ClamAV, and vLLM remain
on private networks. Provision shared service identity through Docker secrets and verify
issuer, audience, policy hash, and content-safe secret fingerprint before release.

Quarantine untrusted PDFs, scan before GPU acquisition, redact protected data before
embedding, enforce bounded tool execution with signed receipts, gate retrieval by hard
eligibility, validate every narrative claim, and preserve human authority. Use a
transactional outbox for Qdrant write-back and retention deletion. Delete terminal
operational data after 30 days while preserving minimized audit and verified precedents
under their own retention policies.

Exit criteria:

- Producer snapshots and consumer models pass the bidirectional contract CI gate.
- Production topology and shared service identity pass executable validation.
- The held-out `n=24` release evaluation and adversarial security cases pass mandatory
  ownership, grounding, idempotency, human-authority, and trace-leak gates.
- Restart/outage/write-back failure injection reaches 100% resume correctness.
- Retention and key-rotation operations produce policy-linked receipts.
- Orchestration is enabled for normal intake only after all gates pass.

## Honest risks

- Learning is retrieval, not model training.
- Historical precedent can encode fair-lending bias.
- Human authority must remain visible and technically enforced.
- One cold-start precedent is illustrative, not a pattern.
- Similarity is evidence for judgment, not a verdict.
- Synthetic data demonstrates mechanism and cannot support accuracy claims.
