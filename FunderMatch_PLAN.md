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

## Phase 0 — contract

Confirm FinDocIQ `POST /extract`. Implement an async `FinDocIQClient` that calls
it and validates a local Pydantic copy of contract version 1.0. The response must
contain structured figures with `(document_id, page_number, bbox)` provenance.

Exit criteria:

- FinDocIQ endpoint has focused API tests.
- FunderMatch accepts a valid contract-v1 fixture and rejects missing provenance.
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

## Honest risks

- Learning is retrieval, not model training.
- Historical precedent can encode fair-lending bias.
- Human authority must remain visible and technically enforced.
- One cold-start precedent is illustrative, not a pattern.
- Similarity is evidence for judgment, not a verdict.
- Synthetic data demonstrates mechanism and cannot support accuracy claims.

