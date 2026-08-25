"use strict";

const state = {
  applicationId: "",
  workflow: null,
  audit: [],
  selectedFunder: "",
  sources: [],
};

const elements = {
  loader: document.querySelector("#case-loader"),
  applicationId: document.querySelector("#application-id"),
  caseTitle: document.querySelector("#case-title"),
  caseSubtitle: document.querySelector("#case-subtitle"),
  workflowState: document.querySelector("#workflow-state"),
  workflowVersion: document.querySelector("#workflow-version"),
  lastActivity: document.querySelector("#last-activity"),
  notice: document.querySelector("#notice"),
  borrower: document.querySelector("#borrower-content"),
  shortlist: document.querySelector("#shortlist-content"),
  decision: document.querySelector("#decision-content"),
  audit: document.querySelector("#audit-list"),
  evidenceCount: document.querySelector("#evidence-count"),
  candidateCount: document.querySelector("#candidate-count"),
  refresh: document.querySelector("#refresh-case"),
  settings: document.querySelector("#settings-dialog"),
  settingsForm: document.querySelector("#settings-form"),
  reviewerToken: document.querySelector("#reviewer-token"),
  pipelineToken: document.querySelector("#pipeline-token"),
  sourceDialog: document.querySelector("#source-dialog"),
  sourceTitle: document.querySelector("#source-title"),
  sourceContent: document.querySelector("#source-content"),
  toastRegion: document.querySelector("#toast-region"),
  intake: document.querySelector("#intake-dialog"),
  intakeForm: document.querySelector("#intake-form"),
  intakeProgress: document.querySelector("#intake-progress"),
};

const tokenKeys = {
  reviewer: "fundermatch.reviewerToken",
  pipeline: "fundermatch.pipelineToken",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function titleCase(value) {
  return String(value ?? "")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

function formatNumber(value, suffix = "") {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return `${escapeHtml(value)}${suffix}`;
  return `${new Intl.NumberFormat("en-IN", { maximumFractionDigits: 2 }).format(numeric)}${suffix}`;
}

function formatDate(value) {
  if (!value) return "—";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return escapeHtml(value);
  return new Intl.DateTimeFormat("en-IN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(parsed);
}

function token(role) {
  return sessionStorage.getItem(tokenKeys[role]) || "";
}

function authToken(preferred = "reviewer") {
  return token(preferred) || token(preferred === "reviewer" ? "pipeline" : "reviewer");
}

async function api(path, options = {}, preferredRole = "reviewer") {
  const bearer = authToken(preferredRole);
  if (!bearer) throw new Error("Add a reviewer or pipeline token under Credentials.");
  const isForm = options.body instanceof FormData;
  const response = await fetch(path, {
    ...options,
    headers: {
      ...(isForm ? {} : { "Content-Type": "application/json" }),
      Authorization: `Bearer ${bearer}`,
      ...(options.headers || {}),
    },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(payload.detail || `Request failed with status ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

function setLoading(loading) {
  document.querySelector("#workspace").classList.toggle("loading", loading);
  elements.refresh.disabled = loading || !state.applicationId;
}

function notice(message, kind = "info") {
  elements.notice.hidden = !message;
  elements.notice.className = `notice notice-${kind}`;
  elements.notice.textContent = message || "";
}

function toast(message) {
  const item = document.createElement("div");
  item.className = "toast";
  item.textContent = message;
  elements.toastRegion.append(item);
  window.setTimeout(() => item.remove(), 4200);
}

async function loadCase(applicationId, { quiet = false } = {}) {
  const normalized = String(applicationId || "").trim();
  if (!normalized) return;
  state.applicationId = normalized;
  elements.applicationId.value = normalized;
  setLoading(true);
  if (!quiet) notice("Loading the durable workflow and audit history…");
  try {
    const encoded = encodeURIComponent(normalized);
    const [workflow, audit] = await Promise.all([
      api(`/v1/workflows/${encoded}`),
      api(`/v1/workflows/${encoded}/audit`),
    ]);
    state.workflow = workflow;
    state.audit = audit.events || [];
    const suggestion = workflow.suggestion || {};
    const candidates = suggestion.candidates || [];
    const funders = [
      ...candidates.map((item) => item.funder_id),
      ...(suggestion.excluded_funders || []).map((item) => item.funder_id),
    ];
    if (!funders.includes(state.selectedFunder)) state.selectedFunder = funders[0] || "";
    render();
    notice("");
    const url = new URL(window.location.href);
    url.searchParams.set("application", normalized);
    history.replaceState({}, "", url);
  } catch (error) {
    renderLoadError(error);
  } finally {
    setLoading(false);
  }
}

function renderLoadError(error) {
  const hint = error.status === 401
    ? "The token is missing, expired, or signed for another audience."
    : error.status === 404
      ? "No durable workflow exists for that application ID."
      : error.message;
  notice(hint, "error");
  if (error.status === 409) toast("The case changed. Refresh before retrying.");
}

function render() {
  const workflow = state.workflow;
  if (!workflow) return;
  const suggestion = workflow.suggestion || {};
  const application = suggestion.application;
  const stateName = workflow.state || "UNKNOWN";
  elements.caseTitle.textContent = application?.borrower_name || workflow.application_id;
  elements.caseSubtitle.textContent = application
    ? `${application.industry} · ${application.region} · Application ${workflow.application_id}`
    : `Application ${workflow.application_id} has no advisory bundle yet.`;
  elements.workflowState.textContent = titleCase(stateName);
  elements.workflowState.className = `state-badge ${stateClass(stateName)}`;
  elements.workflowVersion.textContent = workflow.version;
  elements.lastActivity.textContent = formatDate(workflow.updated_at);
  elements.refresh.disabled = false;
  renderBorrower(application);
  renderShortlist(suggestion);
  renderDecision(suggestion, workflow);
  renderAudit();
}

function stateClass(value) {
  if (value === "AWAITING_HUMAN") return "state-waiting";
  if (value === "HUMAN_DECIDED") return "state-decided";
  if (value === "PRECEDENT_WRITTEN") return "state-written";
  return "state-neutral";
}

function renderBorrower(application) {
  state.sources = [];
  if (!application) {
    elements.borrower.className = "panel-body empty-state";
    elements.borrower.innerHTML = "<p>The pipeline has not attached an advisory bundle.</p>";
    elements.evidenceCount.textContent = "0 sources";
    return;
  }
  const profile = application.profile || {};
  const facts = [
    ["Annual revenue", `₹${formatNumber(profile.annual_revenue_crore)} cr`],
    ["Requested amount", `₹${formatNumber(profile.requested_amount_crore)} cr`],
    ["EBITDA margin", formatNumber(profile.ebitda_margin_pct, "%")],
    ["DSCR", formatNumber(profile.dscr, "×")],
    ["Debt / EBITDA", formatNumber(profile.debt_to_ebitda, "×")],
    ["Collateral cover", formatNumber(profile.collateral_cover, "×")],
    ["Operating history", formatNumber(profile.years_operating, " years")],
    ["Employees", formatNumber(profile.employee_count)],
  ];
  const evidence = application.evidence || [];
  state.sources = evidence.map((item) => ({
    metric: item.name,
    value: item.value,
    unit: item.unit,
    period: item.period,
    ...item.citation,
  }));
  elements.evidenceCount.textContent = `${evidence.length} source${evidence.length === 1 ? "" : "s"}`;
  elements.borrower.className = "panel-body";
  elements.borrower.innerHTML = `
    <p class="section-label">Normalized profile</p>
    <div class="fact-grid">
      ${facts.map(([label, value]) => `<div class="fact-card"><span>${escapeHtml(label)}</span><strong>${value}</strong></div>`).join("")}
    </div>
    <p class="section-label">Review context</p>
    <div class="context-card"><strong>Finance</strong><p>${escapeHtml(application.finance_context)}</p></div>
    <div class="context-card"><strong>Operations</strong><p>${escapeHtml(application.operations_context)}</p></div>
    <p class="section-label">Document evidence</p>
    <div class="evidence-list">
      ${evidence.map((item, index) => `
        <div class="evidence-row">
          <div><strong>${escapeHtml(titleCase(item.name))}</strong><small>${escapeHtml(item.period)} · ${escapeHtml(item.value)} ${escapeHtml(item.unit)}</small></div>
          <button class="source-button" type="button" data-source-index="${index}">View source</button>
        </div>`).join("")}
    </div>`;
  elements.borrower.querySelectorAll("[data-source-index]").forEach((button) => {
    button.addEventListener("click", () => openSource(Number(button.dataset.sourceIndex)));
  });
}

function renderShortlist(suggestion) {
  const candidates = suggestion.candidates || [];
  const excluded = suggestion.excluded_funders || [];
  elements.candidateCount.textContent = `${candidates.length} eligible`;
  if (!candidates.length && !excluded.length) {
    elements.shortlist.className = "panel-body empty-state";
    elements.shortlist.innerHTML = "<p>Rule gating has not produced a shortlist.</p>";
    return;
  }
  elements.shortlist.className = "panel-body";
  elements.shortlist.innerHTML = `
    <p class="section-label">Eligible after hard rules</p>
    <div class="candidate-list">
      ${candidates.map((candidate) => candidateCard(candidate, false)).join("")}
    </div>
    <p class="section-label section-spaced">Excluded before retrieval</p>
    <div class="candidate-list">
      ${excluded.map((candidate) => candidateCard(candidate, true)).join("") || '<p class="advisory-note">No funders were excluded.</p>'}
    </div>`;
  elements.shortlist.querySelectorAll("[data-funder]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedFunder = button.dataset.funder;
      renderShortlist(suggestion);
      renderDecision(suggestion, state.workflow);
    });
  });
}

function candidateCard(item, excluded) {
  const checks = excluded ? item.failed_checks || [] : item.passed_checks || [];
  const selected = item.funder_id === state.selectedFunder;
  const summary = excluded
    ? `${checks.length} hard rule${checks.length === 1 ? "" : "s"} failed`
    : item.evidence_summary;
  return `
    <div class="candidate-card ${excluded ? "excluded" : ""} ${selected ? "selected" : ""}">
      <button class="candidate-summary" type="button" data-funder="${escapeHtml(item.funder_id)}" aria-pressed="${selected}">
        <span><strong>${escapeHtml(item.display_name)}</strong><small>${escapeHtml(summary)}</small></span>
        <span class="tag ${excluded ? "tag-excluded" : "tag-eligible"}">${excluded ? "Excluded" : "Eligible"}</span>
      </button>
      <div class="candidate-details">
        <ul class="rule-list">
          ${checks.map((check) => `<li><span class="${check.passed ? "rule-pass" : "rule-fail"}">${check.passed ? "✓" : "×"}</span><span><strong>${escapeHtml(titleCase(check.criterion))}</strong><br>${escapeHtml(check.actual)} · ${escapeHtml(check.requirement)}</span></li>`).join("")}
        </ul>
      </div>
    </div>`;
}

function renderDecision(suggestion, workflow) {
  if (!suggestion.application) {
    elements.decision.className = "panel-body empty-state";
    elements.decision.innerHTML = "<p>No advisory evidence is available for review.</p>";
    return;
  }
  const candidates = suggestion.candidates || [];
  const excluded = suggestion.excluded_funders || [];
  const selected = [...candidates, ...excluded].find((item) => item.funder_id === state.selectedFunder);
  elements.decision.className = "panel-body";
  const precedentHtml = selected && !selected.failed_checks
    ? renderPrecedents(selected)
    : '<div class="advisory-note">Excluded funders have no retrieved precedent because hard rules run before similarity ranking.</div>';
  const authority = `<div class="advisory-note">${escapeHtml(suggestion.advisory_notice || "AI evidence is advisory. A human decides.")}</div>`;
  if (workflow.state === "AWAITING_HUMAN") {
    elements.decision.innerHTML = authority + precedentHtml + decisionForm(candidates, excluded, selected);
    wireDecisionForm(suggestion, workflow);
    return;
  }
  if (workflow.decision) {
    elements.decision.innerHTML = authority + precedentHtml + decisionRecord(workflow);
    const writeButton = elements.decision.querySelector("#write-precedent");
    if (writeButton) writeButton.addEventListener("click", writePrecedent);
    return;
  }
  elements.decision.innerHTML = authority + precedentHtml + `<div class="advisory-note">Human controls unlock only in <strong>Awaiting Human</strong>. Current state: ${escapeHtml(titleCase(workflow.state))}.</div>`;
}

function renderPrecedents(candidate) {
  const precedents = candidate.precedents || [];
  if (!precedents.length) {
    return '<div class="advisory-note">No close precedent exceeded the configured threshold. This evidence gap remains visible to the reviewer.</div>';
  }
  return `<p class="section-label">Historical human outcomes</p><div class="precedent-list">${precedents.map((item) => {
    const precedent = item.match.precedent;
    return `<div class="precedent-card">
      <div class="precedent-top"><strong>${escapeHtml(precedent.borrower_name)}</strong><span class="score">${(Number(item.match.score) * 100).toFixed(1)}%</span></div>
      <p><strong>${escapeHtml(titleCase(precedent.decision.outcome))}</strong> by ${escapeHtml(precedent.decision.decided_by)}</p>
      <p>${escapeHtml(precedent.decision.rationale)}</p>
      <p>${(item.factors || []).slice(0, 3).map((factor) => escapeHtml(factor.observation)).join(" · ")}</p>
    </div>`;
  }).join("")}</div>`;
}

function decisionForm(candidates, excluded, selected) {
  const options = [
    ...candidates.map((item) => `<option value="${escapeHtml(item.funder_id)}" ${item.funder_id === state.selectedFunder ? "selected" : ""}>${escapeHtml(item.display_name)} · eligible</option>`),
    ...excluded.map((item) => `<option value="${escapeHtml(item.funder_id)}" ${item.funder_id === state.selectedFunder ? "selected" : ""}>${escapeHtml(item.display_name)} · excluded</option>`),
  ].join("");
  return `<form class="decision-form" id="decision-form">
    <p class="section-label">Authoritative human action</p>
    <label for="selected-funder">Funder under review</label>
    <select id="selected-funder">${options}</select>
    <label for="decision-reason">Reviewer rationale</label>
    <textarea id="decision-reason" rows="3" maxlength="2000" required placeholder="State why the evidence supports your action"></textarea>
    <div id="override-fields">${overrideFields(selected)}</div>
    <label for="decision-conditions">Conditions, one per line</label>
    <textarea id="decision-conditions" rows="2" placeholder="Required only for approve with conditions"></textarea>
    <div class="decision-actions">
      <button class="button button-primary" type="button" data-action="approve">Approve</button>
      <button class="button button-primary" type="button" data-action="approve_with_conditions">Approve with conditions</button>
      <button class="button button-danger" type="button" data-action="reject">Reject</button>
      <button class="button button-warning" type="button" data-action="send_back">Send back</button>
    </div>
  </form>`;
}

function overrideFields(selected) {
  const failures = selected?.failed_checks || [];
  if (!failures.length) return "";
  return `<div class="override-fields"><div class="advisory-note">This funder failed hard rules. Every failure requires an explicit human justification; the exclusion remains visible in history.</div>${failures.map((check) => `
    <div class="override-card" data-override="${escapeHtml(check.criterion)}" data-original="${escapeHtml(`${check.actual} versus ${check.requirement}`)}">
      <strong>${escapeHtml(titleCase(check.criterion))}</strong>
      <small>${escapeHtml(check.actual)} · ${escapeHtml(check.requirement)}</small>
      <textarea rows="2" maxlength="1000" placeholder="Required override justification"></textarea>
    </div>`).join("")}</div>`;
}

function wireDecisionForm(suggestion, workflow) {
  const form = elements.decision.querySelector("#decision-form");
  const select = form.querySelector("#selected-funder");
  select.addEventListener("change", () => {
    state.selectedFunder = select.value;
    renderShortlist(suggestion);
    renderDecision(suggestion, workflow);
  });
  form.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", () => submitDecision(button.dataset.action));
  });
}

async function submitDecision(action) {
  if (!token("reviewer")) {
    notice("A reviewer token is required for human actions.", "error");
    elements.settings.showModal();
    return;
  }
  const form = elements.decision.querySelector("#decision-form");
  const reason = form.querySelector("#decision-reason").value.trim();
  const conditionLines = form.querySelector("#decision-conditions").value
    .split("\n").map((line) => line.trim()).filter(Boolean);
  if (!reason) return notice("Reviewer rationale is required.", "error");
  if (action === "approve_with_conditions" && !conditionLines.length) {
    return notice("Approve with conditions requires at least one condition.", "error");
  }
  const overrides = [];
  if (action !== "send_back") {
    for (const card of form.querySelectorAll("[data-override]")) {
      const justification = card.querySelector("textarea").value.trim();
      if (!justification) return notice("Every failed rule needs an override justification.", "error");
      overrides.push({
        criterion: card.dataset.override,
        original_result: card.dataset.original,
        justification,
      });
    }
  }
  const payload = {
    expected_version: state.workflow.version,
    action,
    funder_id: action === "send_back" ? null : state.selectedFunder,
    reason,
    conditions: action === "approve_with_conditions" ? conditionLines : [],
    overrides: action === "send_back" ? [] : overrides,
  };
  setLoading(true);
  notice("Recording the authoritative human action…");
  try {
    await api(`/v1/workflows/${encodeURIComponent(state.applicationId)}/decision`, {
      method: "POST",
      body: JSON.stringify(payload),
    }, "reviewer");
    toast("Human decision recorded with an immutable audit event.");
    await loadCase(state.applicationId, { quiet: true });
  } catch (error) {
    renderLoadError(error);
  } finally {
    setLoading(false);
  }
}

function decisionRecord(workflow) {
  const decision = workflow.decision;
  const conditions = decision.conditions || [];
  const overrides = decision.overrides || [];
  const receipt = workflow.precedent_receipt;
  const canWrite = workflow.state === "HUMAN_DECIDED" && decision.action !== "send_back";
  return `<div class="decision-form">
    <p class="section-label">Recorded human outcome</p>
    <div class="decision-record">
      <h3>${escapeHtml(titleCase(decision.action))}</h3>
      <p><strong>Funder:</strong> ${escapeHtml(decision.funder_id || "Not selected")}</p>
      <p>${escapeHtml(decision.reason)}</p>
      <p><strong>Actor:</strong> ${escapeHtml(decision.actor_display_name)} · ${formatDate(decision.decided_at)}</p>
      ${conditions.length ? `<p><strong>Conditions:</strong> ${conditions.map(escapeHtml).join(" · ")}</p>` : ""}
      ${overrides.length ? `<p><strong>Policy overrides:</strong> ${overrides.map((item) => escapeHtml(item.criterion)).join(" · ")}</p>` : ""}
    </div>
    ${canWrite ? `<div class="writeback-card"><p>The human outcome is durable. A separately authenticated pipeline action may now embed and confirm it in Qdrant.</p><button id="write-precedent" class="button button-primary button-wide" type="button">Write confirmed precedent</button></div>` : ""}
    ${receipt ? `<div class="writeback-card"><p><strong>Precedent confirmed</strong><br>Collection: ${escapeHtml(receipt.collection)}<br>Payload SHA-256: ${escapeHtml(receipt.payload_sha256)}</p></div>` : ""}
  </div>`;
}

async function writePrecedent() {
  if (!token("pipeline")) {
    notice("A separate pipeline token is required for precedent write-back.", "error");
    elements.settings.showModal();
    return;
  }
  setLoading(true);
  notice("Embedding and verifying the confirmed human precedent…");
  try {
    await api(`/v1/workflows/${encodeURIComponent(state.applicationId)}/precedent`, {
      method: "POST",
      body: JSON.stringify({
        expected_version: state.workflow.version,
        reason: "Reviewer console requested confirmed precedent write-back",
      }),
    }, "pipeline");
    toast("Precedent verified in Qdrant and workflow advanced.");
    await loadCase(state.applicationId, { quiet: true });
  } catch (error) {
    renderLoadError(error);
  } finally {
    setLoading(false);
  }
}

function renderAudit() {
  if (!state.audit.length) {
    elements.audit.innerHTML = '<li class="audit-empty">No workflow events loaded.</li>';
    return;
  }
  elements.audit.innerHTML = state.audit.map((event) => `
    <li class="audit-event">
      <strong>${escapeHtml(titleCase(event.action))}</strong>
      <p>${escapeHtml(event.reason)}</p>
      <small>#${escapeHtml(event.sequence)} · ${escapeHtml(event.actor_display_name)}<br>${formatDate(event.occurred_at)}</small>
    </li>`).join("");
}

function openSource(index) {
  const source = state.sources[index];
  if (!source) return;
  elements.sourceTitle.textContent = titleCase(source.metric);
  const bbox = source.bbox || {};
  elements.sourceContent.innerHTML = `
    <div class="source-grid">
      <div class="source-value"><span>Document ID</span><strong>${escapeHtml(source.document_id)}</strong></div>
      <div class="source-value"><span>Page</span><strong>${escapeHtml(source.page_number)}</strong></div>
      <div class="source-value"><span>Period</span><strong>${escapeHtml(source.period)}</strong></div>
      <div class="source-value"><span>Extracted value</span><strong>${escapeHtml(source.value)} ${escapeHtml(source.unit)}</strong></div>
    </div>
    <div class="bbox-visual" aria-label="Bounding box position preview"><div class="bbox-box"></div></div>
    <div class="source-value source-spaced"><span>Bounding box (x0, y0, x1, y1)</span><strong>${escapeHtml([bbox.x0, bbox.y0, bbox.x1, bbox.y1].join(", "))}</strong></div>`;
  elements.sourceDialog.showModal();
}

elements.loader.addEventListener("submit", (event) => {
  event.preventDefault();
  loadCase(elements.applicationId.value);
});
elements.refresh.addEventListener("click", () => loadCase(state.applicationId));
document.querySelector("#open-settings").addEventListener("click", () => {
  elements.reviewerToken.value = token("reviewer");
  elements.pipelineToken.value = token("pipeline");
  elements.settings.showModal();
});
document.querySelector("#open-intake").addEventListener("click", () => {
  if (!token("pipeline")) {
    notice("Add a pipeline token under Credentials before borrower intake.", "error");
    return elements.settings.showModal();
  }
  elements.intake.showModal();
});
document.querySelector("#close-intake").addEventListener("click", () => elements.intake.close());
document.querySelector("#cancel-intake").addEventListener("click", () => elements.intake.close());
elements.intakeForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const fields = new FormData(elements.intakeForm);
  const files = fields.getAll("files");
  const metadata = {};
  for (const name of ["application_id", "borrower_name", "industry", "region", "requested_amount_crore", "debt_to_ebitda", "collateral_cover", "years_operating", "employee_count", "finance_context", "operations_context"]) {
    metadata[name] = fields.get(name);
  }
  metadata.years_operating = Number(metadata.years_operating);
  metadata.employee_count = Number(metadata.employee_count);
  const body = new FormData();
  body.append("metadata", JSON.stringify(metadata));
  files.forEach((file) => body.append("files", file));
  const submit = elements.intakeForm.querySelector('[type="submit"]');
  submit.disabled = true;
  elements.intakeProgress.hidden = false;
  elements.intakeProgress.textContent = "Parsing PDFs, indexing evidence, extracting cited metrics, and evaluating funders. This can take several minutes on first model load.";
  try {
    const result = await api("/v1/intake", { method: "POST", body }, "pipeline");
    state.applicationId = result.workflow.application_id;
    elements.applicationId.value = state.applicationId;
    elements.intake.close();
    elements.intakeForm.reset();
    toast(`${result.documents.length} document(s) processed; case queued for human review.`);
    await loadCase(state.applicationId, { quiet: true });
  } catch (error) {
    elements.intakeProgress.textContent = error.message;
    elements.intakeProgress.className = "dialog-copy intake-error";
  } finally {
    submit.disabled = false;
  }
});
elements.settingsForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (event.submitter?.value === "cancel") return elements.settings.close();
  sessionStorage.setItem(tokenKeys.reviewer, elements.reviewerToken.value.trim());
  sessionStorage.setItem(tokenKeys.pipeline, elements.pipelineToken.value.trim());
  elements.settings.close();
  toast("Credentials saved for this browser tab.");
});
document.querySelector("#clear-tokens").addEventListener("click", () => {
  Object.values(tokenKeys).forEach((key) => sessionStorage.removeItem(key));
  elements.reviewerToken.value = "";
  elements.pipelineToken.value = "";
  toast("Session credentials cleared.");
});
document.querySelector("#close-source").addEventListener("click", () => elements.sourceDialog.close());

const initialApplication = new URLSearchParams(window.location.search).get("application");
if (initialApplication) {
  elements.applicationId.value = initialApplication;
  if (authToken()) loadCase(initialApplication);
}
