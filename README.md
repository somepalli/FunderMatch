# FunderMatch

FunderMatch is human-decided, rule-gated funder matching with searchable case
precedent. It is a separate service from FinDocIQ and accesses document
extraction only through FinDocIQ's versioned HTTP contract.

## Current status

Phase 0 is implemented at the contract-test level:

- async HTTP-only `FinDocIQClient`;
- local Pydantic copy of `/extract` contract version `1.0`;
- mandatory `(document_id, page_number, bbox)` provenance per figure;
- tests for valid responses, missing provenance, upstream failures, and the
  prohibition on importing FinDocIQ internals;
- a live smoke command for a running FinDocIQ service.

A real-model smoke run still requires FinDocIQ's indexed corpus, Qdrant, vLLM,
and configured open-weight models to be running. Phase 1 must not start until
that integration smoke passes.

## Setup

```powershell
uv sync --extra dev
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

## Product controls

- Rules gate eligibility; similarity ranks only eligible candidates.
- AI output is a suggestion. A human decision is authoritative.
- Every human transition will be durable and audited in Postgres.
- Case memory means Qdrant precedent retrieval, not online model training.
- Demo data is synthetic and makes no match-accuracy claim.
