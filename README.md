# FunderMatch

FunderMatch is human-decided, rule-gated funder matching with searchable case
precedent. It is a separate service from FinDocIQ and accesses document
extraction only through FinDocIQ's versioned HTTP contract.

## Current status

Phases 0–2 are implemented:

- async HTTP-only `FinDocIQClient`;
- local Pydantic copy of `/extract` contract version `1.0`;
- mandatory `(document_id, page_number, bbox)` provenance per figure;
- tests for valid responses, missing provenance, upstream failures, and the
  prohibition on importing FinDocIQ internals;
- a live smoke command for a running FinDocIQ service.
- a typed, wholly invented corpus of 20 human-decided loan cases;
- internally consistent financial evidence with synthetic
  `(document_id, page_number, bbox)` provenance;
- separate finance and operations comments plus an explicit human outcome;
- Qdrant loading through `profile_vec` and `comments_vec` named vectors;
- pinned BGE-M3 vectorization for the real seed command;
- fixtures for aligned precedent, similar-but-hard-rule-ineligible, and
  no-close-precedent scenarios.
- typed YAML funder policies with seven independently reported hard checks;
- deterministic eligibility evaluation before any vector query;
- eligible-only Qdrant payload filtering across `profile_vec` and
  `comments_vec`;
- deterministic weighted ranking and an explicit no-close-precedent threshold.

The synthetic corpus demonstrates the mechanism only. It supports no matching
accuracy, credit-quality, or fair-lending claim.

## Setup

```powershell
uv sync --extra dev --extra retrieval
uv run pytest
uv run ruff check .
```

## FinDocIQ boundary

FunderMatch sends:

```json
{
  "question": "Extract FY2025 revenue",
  "question_id": "case-1"
}
```

to `POST /extract`. It accepts only contract version `1.0`, with one or more
figures carrying full source provenance. FunderMatch does not install or import
the `findociq` package.

With FinDocIQ running locally:

```powershell
$env:FINDOCIQ_BASE_URL = "http://127.0.0.1:8989"
uv run python scripts/smoke_findociq.py `
  --question "Extract FY2025 revenue" `
  --question-id "phase0-smoke"
```

## Synthetic precedent corpus

Regenerate and validate the deterministic corpus:

```powershell
uv run python scripts/generate_synthetic_corpus.py
uv run pytest
```

With Qdrant available on the shared conflict-free host port `6999`, load the
20 cases with pinned BGE-M3 embeddings:

```powershell
uv run --extra retrieval fundermatch-seed-precedents `
  --qdrant-url http://127.0.0.1:6999 `
  --collection fundermatch_precedents `
  --recreate
```

Each Qdrant point stores the complete typed case as payload and two cosine
vectors: `profile_vec` for normalized borrower facts and `comments_vec` for
finance/operations reviews plus the authoritative human outcome.

## Eligibility-first precedent retrieval

Funder policies live in `configs/funder_policies.yaml` and are validated through
Pydantic before use. The project deliberately uses a small typed Python rules
engine instead of Drools: the current policy language is seven fixed membership
or numeric comparisons, and adding a JVM would not improve expressiveness or
auditability. Each comparison returns its actual value, requirement, and pass
state.

The matching pipeline always follows this order:

1. Evaluate every configured funder policy.
2. Build an allow-list containing only eligible funder IDs.
3. Pass that allow-list into Qdrant as a payload filter.
4. Query `profile_vec` and `comments_vec` and combine their cosine scores.
5. Return only results above the configured close-precedent threshold.

Run the live aligned-precedent smoke against the shared Qdrant service:

```powershell
uv run --extra retrieval fundermatch-match-precedents `
  --case-id SYN-001 `
  --qdrant-url http://127.0.0.1:6999 `
  --model-dir C:\path\to\pinned\bge-m3-snapshot
```

This stage returns precedent evidence, not a credit decision. Phase 3 may use
the eligible results to assemble a suggestion, but only a human can approve or
reject an application.

## Product controls

- Rules gate eligibility; similarity ranks only eligible candidates.
- AI output is a suggestion. A human decision is authoritative.
- Every human transition will be durable and audited in Postgres.
- Case memory means Qdrant precedent retrieval, not online model training.
- Demo data is synthetic and makes no match-accuracy claim.
