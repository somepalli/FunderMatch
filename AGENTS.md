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
assembly as deterministic, linear Python. Do not add an orchestration framework
to this half.

Use an explicit Postgres state machine for the small HITL branch set unless its
complexity grows enough to justify LangGraph:

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

## Anti-goals

- No FinDocIQ internal imports.
- No LangGraph in the linear pipeline.
- No chat UI.
- No AI approval/rejection authority.
- No Origa production intellectual property.
- No match-accuracy claims based on synthetic data.

