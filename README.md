# FunderMatch

FunderMatch is human-decided, rule-gated funder matching with searchable case
precedent. It is a separate service from FinDocIQ and accesses document
extraction only through FinDocIQ's versioned HTTP contract.

## Current status

Phases 0–6 and the opt-in resilient agent supervisor are implemented:

- async HTTP-only `FinDocIQClient`;
- local Pydantic copies of development contract `1.0` and application-scoped
  production contract `2.0`;
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
- advisory-only suggestion bundles with no decision field or authority;
- click-to-source borrower and precedent evidence on every normalized figure;
- per-precedent similarity factors, finance/operations comments, and historical
  human outcomes;
- an optional pinned Gemma 3 narrative adapter with temperature 0, fixed seed,
  a prompt file, structured JSON output, and post-generation authority guards.
- an optimistic-versioned Postgres state machine for durable human review;
- JWT-authenticated pipeline and reviewer roles, with actor identity taken from
  signed claims rather than request bodies;
- idempotent commands and append-only audit events containing the actor,
  reason, changes, and timestamp for every transition;
- thin async FastAPI endpoints for intake, pipeline progress, human decisions,
  workflow state, and ordered audit history.
- a confirmed human-decision write-back into Qdrant's `profile_vec` and
  `comments_vec`, followed by the durable `PRECEDENT_WRITTEN` transition;
- explicit conditions and human policy overrides embedded into precedent
  memory, with `send_back` deliberately excluded from lending precedent;
- deterministic Qdrant point IDs, payload-hash receipts, retry-safe writes, and
  a case-one decision to case-two retrieval smoke.
- a responsive, outcome-first review console served directly by FastAPI with
  no separate UI backend or chat interface;
- three panels for borrower evidence, rule-gated funders, and precedent plus
  authoritative human controls;
- click-to-source document/page/bbox provenance, explicit override capture,
  separate reviewer and pipeline credentials, and immutable audit history;
- an invented demo-case bootstrap that issues short-lived local JWTs without
  weakening production authentication.
- a fixed LangGraph supervisor with checkpointed document processing, financial
  extraction, eligibility, eligible-only retrieval, suggestion, guardrail, and
  human-handoff workers;
- PostgreSQL-backed application memory with resume, cancel, sanitized status,
  idempotent commands, and durable human-review interrupts;
- deterministic citation, evidence-ownership, numeric-grounding,
  eligible-funder, authority, receipt, and checkpoint guardrails;
- content-safe JSONL and self-hosted Langfuse OTLP spans, scheduled retention
  maintenance, dependency readiness, held-out synthetic evals, and
  failure-injection coverage.

The synthetic corpus demonstrates the mechanism only. It supports no matching
accuracy, credit-quality, or fair-lending claim.

Graph execution is intentionally disabled by default through
`FUNDERMATCH_AGENT_ORCHESTRATION_ENABLED=false`. The deterministic legacy intake
remains the rollback path until the manual GPU and service-outage release gates
have been exercised in the target environment.

## Runtime architecture

FunderMatch owns workflow authority, eligibility, precedent matching, guardrails,
and the human decision boundary. FinDocIQ remains a separately deployed document
intelligence service accessed only through HTTP.

```text
Browser / pipeline client
  -> FunderMatch FastAPI + review console
  -> fixed LangGraph supervisor
  -> FinDocIQ document processing and cited metric extraction
  -> deterministic eligibility rules
  -> eligible-only Qdrant precedent retrieval
  -> advisory suggestion assembly
  -> deterministic guardrail validation
  -> durable human review
  -> verified precedent write-back after the human decision
```

| Runtime dependency | Local endpoint | Responsibility | Failure behavior |
| --- | --- | --- | --- |
| FunderMatch API/UI | `http://127.0.0.1:8977` | Intake, orchestration, review, recovery | Primary service |
| PostgreSQL | `127.0.0.1:7444` | Authoritative workflow/audit plus isolated LangGraph checkpoints | Fails readiness |
| Qdrant | `http://127.0.0.1:6999` | Eligible-only precedent retrieval and verified write-back | Retryable/needs attention |
| FinDocIQ | `http://127.0.0.1:8989` | PDF ingestion and cited financial extraction | Retryable dependency failure |
| vLLM | `http://127.0.0.1:8900/v1` | Pinned Gemma narrative and send-back routing | Retryable or safe stop |
| Self-hosted Langfuse | `http://127.0.0.1:3000` | Content-safe operational traces | Warning-only; workflow continues |

The system never sends PDFs, extracted text, borrower names, financial values,
prompts, credentials, or raw checkpoint state to Langfuse. PostgreSQL workflow
and audit tables remain authoritative; LangGraph checkpoints are operational
resume state, and Qdrant receives long-term memory only after verified human
review.

## Setup

```powershell
uv sync --extra dev --extra retrieval
uv run pytest
uv run ruff check .
```

## FinDocIQ boundary

In the development profile, FunderMatch sends:

```json
{
  "question": "Extract FY2025 revenue",
  "question_id": "case-1"
}
```

to `POST /extract` and accepts contract version `1.0`. In the production profile,
the same endpoint uses contract version `2.0` with an application-scoped service
JWT and allow-listed `metric_ids` instead of a free-text question. Both versions
return one or more figures carrying full source provenance. FunderMatch does not
install or import the `findociq` package.

FinDocIQ commits a generated public contract bundle at
`contracts/findociq-public-http-contract.json`. FunderMatch validates every mirrored
v1/v2 request and response model against that producer-owned bundle in CI. FinDocIQ's
CI also checks the current FunderMatch consumer, so producer-side schema drift fails
before release without introducing a Python package dependency between the services.

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

This stage returns precedent evidence, not a credit decision. Phase 3 uses the
eligible results to assemble an advisory bundle, but only a human can approve
or reject an application.

## Advisory suggestion assembly

Phase 3 partitions every configured funder into either an eligible advisory
candidate or an excluded funder with explicit failed checks. Each candidate
contains its passed rules, close precedents, similarity factors, historical
finance and operations comments, historical human outcome, and complete source
evidence. A candidate with no close precedent remains visible with an explicit
evidence-gap flag.

Run live BGE/Qdrant retrieval and deterministic assembly:

```powershell
uv run --extra retrieval fundermatch-suggest `
  --case-id SYN-001 `
  --qdrant-url http://127.0.0.1:6999 `
  --model-dir C:\path\to\pinned\bge-m3-snapshot
```

Gemma narrative generation is a separate optional step so the 12 GB laptop GPU
does not need BGE-M3 and Gemma 3 12B resident together. With vLLM running:

```powershell
uv run --extra retrieval fundermatch-narrative-smoke `
  --case-id SYN-001 `
  --base-url http://127.0.0.1:8900/v1
```

The prompt is stored in `prompts/suggestion_narrative_system.txt`. The adapter
uses the pinned Gemma revision, temperature `0`, seed `17`, and a strict JSON
schema. Generated language that attempts to recommend approval or rejection is
rejected after generation. The bundle always declares `authority: advisory_only`
and `requires_human_decision: true`.

## Durable human review and audit

Phase 4 persists `AWAITING_HUMAN`; only a token with the `human_reviewer` role
can transition it to `HUMAN_DECIDED`. The supported actions are `approve`,
`reject`, `approve_with_conditions`, and `send_back`. None is selected by AI.
Each write carries an expected workflow version and a command ID, so stale
review screens fail safely and retries replay the original result.

Start local Postgres on conflict-free host port `7444`, then migrate and run a
complete live transition smoke:

```powershell
docker compose up -d postgres
$env:FUNDERMATCH_DATABASE_URL = `
  "postgresql://fundermatch:fundermatch-local-only@127.0.0.1:7444/fundermatch"
uv run fundermatch-migrate-workflow
uv run fundermatch-workflow-smoke
```

Run the API on host port `8977` after setting a random JWT secret of at least 32
characters:

```powershell
$env:FUNDERMATCH_JWT_SECRET = "replace-with-a-random-secret-of-32-plus-characters"
uv run uvicorn fundermatch.api.app:create_app --factory --port 8977
```

The migration installs a database trigger that rejects `UPDATE` and `DELETE`
on `workflow_audit`; corrections are new events, never rewrites of history.

## Precedent write-back loop

Phase 5 converts the application, its cited evidence, finance and operations
context, and the authoritative human outcome into a `DecidedLoanCase`. It then
embeds the profile separately from comments, rationale, conditions, and human
overrides. Qdrant must return the exact stored payload before the pipeline may
transition Postgres from `HUMAN_DECIDED` to `PRECEDENT_WRITTEN`.

The Qdrant point ID is derived from the application ID. A command ID makes the
Postgres transition idempotent, and a SHA-256 payload receipt detects later
payload drift. A failed embedding or Qdrant write leaves the durable workflow
at `HUMAN_DECIDED`, ready for a safe retry.

Run the live case-one write and case-two retrieval smoke:

```powershell
$env:FUNDERMATCH_DATABASE_URL = `
  "postgresql://fundermatch:fundermatch-local-only@127.0.0.1:7444/fundermatch"
uv run --extra retrieval fundermatch-writeback-smoke `
  --qdrant-url http://127.0.0.1:6999 `
  --model-dir C:\path\to\pinned\bge-m3-snapshot
```

The August 2026 Windows `uv` smoke resolved PyTorch `2.13.0+cpu`, so this BGE-M3
write-back validation was CPU-bound. It does not report latency. Gemma through
vLLM is the separately validated GPU-serving path; a CUDA-enabled PyTorch build
is required before claiming GPU-backed BGE embeddings.

The smoke uses invented, near-identical cases to prove that newly decided
memory becomes searchable. Its similarity score is not a matching-quality or
credit-accuracy result.

## Three-panel human review console

Phase 6 is served by the same FastAPI process at `/`; API and UI remain
same-origin. The interface is deliberately not conversational:

1. **Borrower evidence** shows normalized financials, finance and operations
   context, and clickable `(document, page, bbox)` provenance.
2. **Funder shortlist** separates eligible candidates from hard-rule
   exclusions and shows the actual-versus-required result for each check.
3. **Precedent and decision** shows historical human outcomes and exposes the
   four authenticated human actions only while the durable state is
   `AWAITING_HUMAN`.

Reviewer and pipeline JWTs are separate. They are stored only in browser-tab
`sessionStorage`, never in a URL or persistent browser storage. A reviewer can
record the human outcome; the internal pipeline identity performs verified,
idempotent Qdrant write-back and completes graph memory only after its receipt is
confirmed. Selecting an excluded funder renders a required justification
field for every failed hard rule.

Start the local services and create an invented review case:

```powershell
docker compose up -d postgres
$env:FUNDERMATCH_DATABASE_URL = `
  "postgresql://fundermatch:fundermatch-local-only@127.0.0.1:7444/fundermatch"
$env:FUNDERMATCH_JWT_SECRET = "replace-with-a-random-secret-of-32-plus-characters"
$env:FUNDERMATCH_QDRANT_URL = "http://127.0.0.1:6999"
$env:FUNDERMATCH_BGE_SNAPSHOT_DIR = "C:\path\to\pinned\bge-m3-snapshot"
uv run fundermatch-demo-review
uv run uvicorn fundermatch.api.app:create_app --factory `
  --host 127.0.0.1 --port 8977
```

The bootstrap prints an application ID, review URL, and short-lived reviewer
and pipeline tokens. Open the URL, choose **Credentials**, and paste the tokens
into their separately labelled fields. All bootstrap data is invented.

The console ships as package data inside the Python wheel and uses no CDN,
external font, analytics script, or Node runtime. A restrictive same-origin
content security policy blocks third-party scripts and framing.

## Product controls

- Rules gate eligibility; similarity ranks only eligible candidates.
- AI output is a suggestion. A human decision is authoritative.
- Every human transition is durable and audited in Postgres.
- Only a confirmed human outcome is eligible for precedent write-back.
- An excluded funder requires explicit human overrides for every failed rule.
- UI controls render workflow authority; they do not replace server-side role,
  transition, or optimistic-version checks.
- Case memory means Qdrant precedent retrieval, not online model training.
- Demo data is synthetic and makes no match-accuracy claim.

## Borrower PDF intake

The review console accepts any number of real borrower PDFs through **New borrower**,
subject to 25 MB per file and 512 MB per batch. Files are
stored outside Git, sent to FinDocIQ over its authenticated HTTP contract, indexed in
Qdrant, and queried only within the uploaded document IDs. FinDocIQ extracts annual
revenue, EBITDA margin, and DSCR with `(document_id, page, bbox)` citations. The form
requires the remaining hard-rule inputs because the system never guesses eligibility
facts.

An upload is sent as one ingestion batch. FinDocIQ sleeps local vLLM once, gives the
GPU to Docling/OCR and BGE-M3 for the complete batch, releases those models, and wakes
vLLM before cited metric extraction. A failure still triggers the wake path, so one bad
PDF does not intentionally leave generation offline.

The review console submits new work to `POST /v1/intake-jobs`, which returns `202`
with a durable job ID after the upload is accepted. It then polls the authenticated
`events_url` and renders the current document, GPU hand-off, parsing, chunking,
embedding, Qdrant indexing, metric extraction, rule gating, and review-queue stages.
Activity events contain filenames and counts but never PDF text, prompts, credentials,
or raw model responses. Reload-safe job state and the append-only timeline live in
Postgres tables created by `migrations/003_intake_activity.sql`; interrupted jobs are
marked failed and retryable on API restart instead of appearing to run forever.

The original synchronous `POST /v1/intake` remains available for compatibility with
local scripts. New browser integrations should use the job endpoint and
`GET /v1/intake-jobs/{job_id}/events?after=<sequence>`.

Use an external directory (do not place borrower files in this repository):

```powershell
$env:FUNDERMATCH_INTAKE_DIR = `
  "$env:USERPROFILE\Documents\FunderMatch_Data\borrower_intake"
$env:FINDOCIQ_BASE_URL = "http://127.0.0.1:8989"
$env:FINDOCIQ_INGEST_TOKEN = "replace-with-a-separate-random-ingestion-token"
```

Each development intake is stored under `<intake-dir>/<application-id>/`. With
production guardrails enabled, PDFs and revealable workspace artifacts are instead
AES-256-GCM envelope-encrypted under that application directory; checkpoints retain
only references and hashes. The pipeline
then runs hard rules before precedent retrieval and stops at `AWAITING_HUMAN`; no AI
output approves or rejects a borrower.

## Production guardrails

`FUNDERMATCH_PRODUCTION_GUARDRAILS_ENABLED=true` enables the fail-closed production
profile. It adds scoped five-minute FinDocIQ service JWTs, encrypted application
workspace storage, PostgreSQL rate and concurrency limits, signed worker receipts,
claim-ledger validation, masked normal responses, audited field-level reveal,
active/unexpired precedent filtering, reviewer-authorized revoke/supersede, and an
outbox-backed 30-day terminal deletion workflow. The legacy behavior remains available
only when the flag is false.

The reviewed policy is `configs/guardrails/production.yaml`. Both services calculate
the same SHA-256 policy identity at startup; FunderMatch readiness fails if FinDocIQ
reports a different hash. The production Compose file reads JWT, receipt, database,
Langfuse, reviewer, and AES keys from files in `secrets/`; those files are ignored by
Git. It publishes only Caddy at `https://127.0.0.1:8977`, while FinDocIQ, PostgreSQL,
Qdrant, ClamAV, and vLLM remain on private Docker networks.

Before starting, place the pinned BGE-M3 snapshot at `models/bge-m3` and FinDocIQ's
pinned BGE-M3/reranker snapshots under `models/findociq` using the paths documented in
FinDocIQ's index config. Create the secret files named by
`docker-compose.production.yml`; `document_master_key.b64` must decode to exactly 32
bytes, and `database_url.txt` must use the password in `postgres_password.txt`.
Fresh clones use the canonical sibling directory `../FinDocs_Analysis_Evals`. Existing
local clones that still use the former typo can set
`FINDOCIQ_REPO_PATH=../FinDocs_Analysi_Evals` without changing the Compose file.

```powershell
docker compose -f docker-compose.production.yml config
docker compose -f docker-compose.production.yml up --build
```

Suspicious PDFs are blocked and retained only as encrypted quarantine objects. Clean
documents must return scan, DLP, encrypted-storage, and ownership receipt hashes before
the document worker can checkpoint. Prompt-like document instructions stop at
`needs_attention`; malware, active content, embedded files, ownership violations,
invalid worker receipts, and ineligible-funder leakage fail closed. Langfuse receives
structural identifiers, hashes, counts, timing, and safe codes only; its outage never
changes workflow execution.

Use `fundermatch-rotate-workspace-keys` and FinDocIQ's
`findociq-rotate-document-keys` during an offline maintenance window. Both commands
rewrap per-object data keys, preserve ciphertext, and write policy-linked rotation
audit events. The additional invented adversarial gate is
`evals/datasets/production_security_cases.jsonl` (`n=8`), kept separate from the
existing `n=24` matching/recovery evaluation.

## Agent memory and recovery

LangGraph is an operational supervisor around the deterministic pipeline; it does
not contain extraction, eligibility, ranking, or lending-decision logic. Each
application uses its `application_id` as an isolated LangGraph `thread_id`.
Checkpoints live in a dedicated `langgraph` schema in the same Postgres database,
while `workflow_cases` and the append-only workflow audit remain authoritative.

Checkpoint state is a strict 256 KiB allow-list of IDs, SHA-256 hashes, cited
findings, eligibility outcomes, guardrail results, stable command IDs, and write
receipts. PDFs, base64 payloads, document chunks, prompts, raw model responses, and
ReAct message histories cannot be serialized into it. Worker scratch state is
ephemeral and defaults to an eight-tool-call budget.

Run the additional migration once before enabling the supervisor:

```powershell
uv run fundermatch-migrate-memory
```

Retryable failures and human-review waits are retained until resolved or explicitly
cancelled. `completed`, `cancelled`, and `failed_terminal` checkpoints become
eligible for cleanup after 30 days. Stale non-terminal cases are flagged for an
operator and are never deleted by retention cleanup. Qdrant long-term memory still
accepts only finalized human-reviewed precedent through the existing write-back
service.

## Fixed agent flow and release gates

The opt-in supervisor executes one code-owned order that a model or request cannot
rearrange:

```text
Document processing -> Financial metric extraction -> Deterministic eligibility
-> Eligible-only precedent retrieval -> Advisory suggestion assembly
-> Deterministic guardrails -> Human-review handoff
```

Revenue, EBITDA margin, and DSCR have separate durable substep artifacts. A retry
therefore resumes the missing metric without repeating completed FinDocIQ calls.
Reviewer send-back can select an exact worker; `supervisor` uses pinned Gemma 3 at
temperature 0 and stops at `needs_attention` when its route is ambiguous. Eligibility
and guardrails can never be skipped. Approve, reject, and approve-with-conditions
remain human-only actions and trigger verified precedent write-back; send-back is
never precedent memory.

Enable graph execution only for synthetic/demo runs until the manual GPU gate passes:

```powershell
$env:FUNDERMATCH_AGENT_ORCHESTRATION_ENABLED = "true"
$env:FUNDERMATCH_VLLM_BASE_URL = "http://127.0.0.1:8900/v1"
```

The legacy intake remains the rollback path when the flag is false. Recovery exposes
`POST /v1/intake-jobs/{job_id}/resume`,
`POST /v1/intake-jobs/{job_id}/cancel`, and the sanitized
`GET /v1/applications/{application_id}/memory`; no endpoint returns checkpoint
payloads. The live activity panel shows worker, attempt, resume, guardrail, and
checkpoint events separately from the authoritative workflow audit.

FunderMatch emits content-safe local JSONL spans and optionally exports the same typed
supervisor/worker spans to an existing self-hosted Langfuse OTLP endpoint. Traces
contain identifiers, counts, timing, hashes, and safe error codes only--never borrower
names, financial values, PDFs, extracted text, prompts, answers, credentials, raw
exceptions, or checkpoint state. Langfuse failure is warning-only and cannot stop the
workflow. `/ready` reports PostgreSQL, Qdrant, FinDocIQ, vLLM, and Langfuse separately;
only Langfuse is non-blocking.

Prompt attribution is content-safe: supervisor and generation records contain a
template ID, content-derived version, prompt SHA-256, pinned model revision, and a
configuration hash that incorporates the prompt hash. Rendered prompt bodies and model
responses remain excluded. Controlled evaluation reports carry the held-out dataset
SHA-256, allowing scores to be tied to an exact dataset and prompt/configuration version.

The held-out release dataset is `evals/datasets/agent_release_cases.jsonl` with `n=24`
invented applications: 8 eligible/aligned, 8 hard-rule boundary or ineligible, 4 with
no close precedent, and 4 adversarial guardrail cases. Send-back routing has a separate
dataset. Retrieval quality must be reported independently as eligible-only Recall@3,
MRR, and nDCG with `n`; there is no aggregate credit-accuracy score. CPU/GPU p50 and
p95 results are release outputs and are not claimed until the respective manual runs
are executed.
