# FunderMatch contributor rules

FunderMatch is human-in-the-loop funder/loan-matching decision support. It
extracts borrower financials through FinDocIQ, applies policy rules, retrieves
past decided cases, and presents a suggestion that a human may approve, reject,
approve with conditions, or send back.

## Non-negotiable constraints

- FinDocIQ is a black box behind HTTP `POST /extract`. Never import the
  `findociq` package. Keep a versioned local Pydantic copy of the public response
  contract under `src/fundermatch/clients/`.
- The AI suggests; a human decides. No code path may auto-approve or auto-reject.
- Rules gate eligibility before similarity ranking. Ineligible funders must not
  enter the shortlist.
- Every human transition records who acted, what changed, why, and when.
- Learning means precedent retrieval and write-back, never online training.
- Use only invented, internally consistent synthetic data. Do not copy Origa
  production code, rules, or data.
- Rule evaluation and retrieval must be reproducible. Narrative model calls use
  Gemma 3 through vLLM with temperature 0 and pinned revisions.

## Architecture boundary

Keep extraction, vectorization, rule evaluation, retrieval, and suggestion
assembly as deterministic, linear Python. LangGraph may coordinate these modules
as bounded workers, but it must not move financial logic into graph routes or
make eligibility or lending decisions.

Use the explicit Postgres state machine as the authoritative business workflow.
LangGraph is the durable, checkpointed orchestration layer around the fixed worker
sequence; it does not replace workflow authority:

```text
INTAKE -> EXTRACTED -> RULE_GATED -> AI_SUGGESTED -> AWAITING_HUMAN
  -> (approve | reject | approve_with_conditions | send_back)
  -> HUMAN_DECIDED -> PRECEDENT_WRITTEN
```

`AWAITING_HUMAN` is durable and only a human action can leave it.

## Stack and conventions

- FinDocIQ `/extract` over HTTP
- Qdrant named vectors: `profile_vec`, `comments_vec`
- Drools or a deliberately selected rules engine for eligibility
- Postgres for durable state and audit
- Async FastAPI endpoints
- Open-weight Gemma 3 through vLLM; no proprietary model APIs under `src/`
- Three-panel, outcome-first UI; never a chat UI
- `uv` and `pyproject.toml`; no `requirements.txt`
- Pydantic models at every module boundary
- Prompt files instead of inline prompt strings
- pytest fixtures for an aligned precedent, a similar but hard-rule-ineligible
  case, and a no-close-precedent case

## Build order

Work in order and do not begin a phase until the prior phase's tests pass:

0. FinDocIQ HTTP contract and client
1. Synthetic corpus and schema
2. Rule gating and precedent retrieval
3. Suggestion assembly
4. Durable HITL state machine and audit
5. Precedent write-back loop
6. Three-panel UI
7. Durable LangGraph supervisor/workers, recovery, guardrails, and observability
8. Production security, cross-repository contracts, retention, and release qualification

## Production integration

- Development contract `1.0` permits a free-text extraction question; production
  contract `2.0` requires application scope, allow-listed metric IDs, service JWTs,
  correlation IDs, and verified security receipts.
- Keep the local Pydantic consumer copy synchronized through the generated FinDocIQ
  public contract bundle and the bidirectional GitHub Actions compatibility gate.
- The integrated production Compose file is authoritative for local production:
  only Caddy publishes a host port; FinDocIQ, PostgreSQL, Qdrant, ClamAV, and vLLM
  remain on internal networks.
- LangGraph routes only operational status. Financial extraction, eligibility,
  suggestion assembly, guardrail checks, and human decisions remain in their owned
  deterministic modules.

## Anti-goals

- No FinDocIQ internal imports.
- No LangGraph inside the deterministic financial pipeline; checkpointed
  orchestration may wrap it.
- No chat UI.
- No AI approval/rejection authority.
- No Origa production intellectual property.
- No match-accuracy claims based on synthetic data.
