const state = {
  dashboard: null,
  sites: [],
  selectedBatch: null,
  filter: "active",
  uploadToken: null,
  uploadSuggestion: null,
  integrityRequiresOverride: false,
  preparationId: 0,
  reviewView: "photos",
  machineReviewConfig: null,
  machineReviewLoading: false,
  appView: "dashboard",
  publishedData: null,
  publishedLoading: false,
  dashboardLoading: false,
  dashboardSiteFilter: "",
  dashboardRefreshTimer: null,
};

const photoTypes = [
  ["", "Choose a role"],
  ["wide", "Wide"],
  ["quadrat", "Quadrat"],
  ["close_up", "Close-up"],
  ["context_left", "Context left"],
  ["context_right", "Context right"],
  ["extra", "Extra"],
];

function iconRefresh() {
  if (window.lucide) window.lucide.createIcons();
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let body = {};
  try { body = await response.json(); } catch (_) { body = {}; }
  if (!response.ok) throw new Error(body.detail || `Request failed (${response.status})`);
  return body;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function showToast(message, error = false) {
  const toast = document.querySelector("#toast");
  toast.textContent = message;
  toast.classList.toggle("error", error);
  toast.classList.add("visible");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("visible"), 3300);
}

function siteLabel(siteId) {
  const site = state.sites.find((item) => String(item.site_id) === String(siteId));
  return site ? `Site ${site.site_id}: ${site.site_name}` : `Site ${siteId}`;
}

function statusLabel(status) {
  return {
    needs_review: "Needs review",
    observation_review: "Observation review",
    ready: "Ready to publish",
    published: "Published",
    failed: "Failed",
    staged: "Staging",
  }[status] || status;
}

function formatDateTime(value) {
  if (!value) return "Not yet";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function formatShortDate(value) {
  if (!value) return "-";
  const parsed = new Date(`${value}T12:00:00`);
  return Number.isNaN(parsed.getTime())
    ? value
    : parsed.toLocaleDateString(undefined, {month: "short", day: "numeric"});
}

function publicationStatus(status) {
  return {
    succeeded: "Succeeded",
    failed: "Failed",
    running: "Running",
    legacy: "Pre-tracking",
    passed: "Passed",
    not_recorded: "Not recorded",
  }[status] || status || "Not recorded";
}

function exportLabel(filename) {
  return filename
    .replace(/\.csv$/, "")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function setAppView(view) {
  state.appView = view;
  document.querySelector("#dashboard-shell").classList.toggle("hidden", view !== "dashboard");
  document.querySelector("#ingestion-shell").classList.toggle("hidden", view !== "ingestion");
  document.querySelector("#published-shell").classList.toggle("hidden", view !== "published");
  document.querySelectorAll(".app-nav-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.appView === view);
  });
  if (view === "dashboard") {
    loadDashboard({silent: Boolean(state.dashboard)});
    scheduleDashboardRefresh();
  } else {
    clearTimeout(state.dashboardRefreshTimer);
  }
  if (view === "published") loadPublishedData();
  iconRefresh();
}

async function loadPublishedData() {
  if (state.publishedLoading) return;
  state.publishedLoading = true;
  const refresh = document.querySelector("#refresh-published-button");
  refresh.disabled = true;
  try {
    state.publishedData = await api("/api/published-data");
    renderPublishedData();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    state.publishedLoading = false;
    refresh.disabled = false;
    iconRefresh();
  }
}

function renderPublishedData() {
  const data = state.publishedData;
  if (!data) return;
  const summary = data.summary || {};
  const sheets = data.google_sheets || {};
  document.querySelector("#data-observation-count").textContent = summary.observation_count || 0;
  document.querySelector("#data-photo-count").textContent = summary.photo_count || 0;
  document.querySelector("#data-run-count").textContent = summary.publication_run_count || 0;
  document.querySelector("#data-published-at").textContent = summary.last_published_at
    ? `Last published ${formatDateTime(summary.last_published_at)}`
    : "No local publication timestamp";

  const syncButton = document.querySelector("#sync-sheets-button");
  const latestRun = (data.publication_runs || []).find((run) => ["succeeded", "legacy"].includes(run.status));
  syncButton.disabled = !sheets.available || !latestRun;
  syncButton.dataset.runId = latestRun?.run_id || "";
  document.querySelector("#data-sheets-state").textContent = sheets.available ? "Ready" : "Off";
  const sheetsNote = document.querySelector("#sheets-config-note");
  if (sheets.available) {
    sheetsNote.innerHTML = sheets.spreadsheet_url
      ? `<a href="${escapeHtml(sheets.spreadsheet_url)}" target="_blank" rel="noreferrer">Open Google Sheets mirror</a>`
      : "Google Sheets mirror configured";
  } else {
    sheetsNote.textContent = sheets.reason || "Google Sheets mirror is not configured.";
  }

  document.querySelector("#export-list").innerHTML = (data.exports || []).map((filename) => `
    <a class="export-link" href="/api/exports/${encodeURIComponent(filename)}" download>
      <i data-lucide="download" aria-hidden="true"></i>
      <span><strong>${escapeHtml(exportLabel(filename))}</strong><small>${escapeHtml(filename)}</small></span>
    </a>
  `).join("") || '<p class="empty-state">No public exports found.</p>';

  document.querySelector("#published-observations").innerHTML = (data.observations || []).map((row) => `
    <tr>
      <td>${escapeHtml(row.observation_date)}</td>
      <td>${escapeHtml(siteLabel(row.site_id))}</td>
      <td><span class="status-badge status-${row.review_status === "reviewed" ? "published" : "observation_review"}">${escapeHtml(row.review_status || "unknown")}</span></td>
      <td>${escapeHtml(row.source_photo_count || "0")}</td>
      <td>${row.estimated_total_vegetation_cover_pct !== "" ? `${escapeHtml(row.estimated_total_vegetation_cover_pct)}%` : "-"}</td>
      <td>${escapeHtml(row.dominant_species || "-")}</td>
      <td>${escapeHtml(row.record_origin || "-")}</td>
    </tr>
  `).join("") || '<tr><td colspan="7">No published observations.</td></tr>';

  document.querySelector("#publication-runs").innerHTML = (data.publication_runs || []).map((run) => {
    const sync = run.latest_sync;
    const syncState = sync ? publicationStatus(sync.status) : (sheets.available ? "Not synced" : "Not configured");
    return `
      <tr>
        <td>${escapeHtml(formatDateTime(run.completed_at || run.started_at))}</td>
        <td>${escapeHtml(run.observation_id)}</td>
        <td><span class="run-state run-${escapeHtml(run.status)}">${escapeHtml(publicationStatus(run.status))}</span></td>
        <td>${escapeHtml(publicationStatus(run.validation_status))}</td>
        <td>${escapeHtml(run.photo_rows_written)}</td>
        <td>${escapeHtml(syncState)}${sync?.error_message ? `<small class="cell-note">${escapeHtml(sync.error_message)}</small>` : ""}</td>
      </tr>`;
  }).join("") || '<tr><td colspan="6">Older published data predates pipeline-run tracking.</td></tr>';
  iconRefresh();
}

async function syncGoogleSheets() {
  const button = document.querySelector("#sync-sheets-button");
  const runId = button.dataset.runId;
  if (!runId) return;
  button.disabled = true;
  button.innerHTML = '<i data-lucide="loader-circle" aria-hidden="true"></i> Syncing...';
  iconRefresh();
  try {
    await api(`/api/publication-runs/${encodeURIComponent(runId)}/sync/google-sheets`, {method: "POST"});
    await loadPublishedData();
    showToast("Published data mirrored to Google Sheets.");
  } catch (error) {
    await loadPublishedData();
    showToast(error.message, true);
  } finally {
    button.innerHTML = '<i data-lucide="file-spreadsheet" aria-hidden="true"></i> Sync to Google Sheets';
    renderPublishedData();
  }
}

async function loadSites() {
  state.sites = await api("/api/sites");
  const select = document.querySelector("#site-id");
  select.innerHTML = '<option value="">Choose a site</option>' + state.sites.map((site) =>
    `<option value="${escapeHtml(site.site_id)}">Site ${escapeHtml(site.site_id)}: ${escapeHtml(site.site_name)}</option>`
  ).join("");
}

function provenanceLabel(value) {
  return {
    human: "Human",
    ai_assistant: "Assistant",
    model: "Model",
    legacy_unknown: "Pre-tracking",
    rule: "Rule",
  }[value] || value.replaceAll("_", " ");
}

function bindDashboardBatchLinks() {
  document.querySelectorAll("[data-open-batch]").forEach((element) => {
    element.addEventListener("click", () => openBatchFromDashboard(element.dataset.openBatch));
  });
}

async function openBatchFromDashboard(batchId) {
  if (!batchId) return;
  setAppView("ingestion");
  await selectBatch(batchId);
}

function renderQualityDashboard(data) {
  const quality = data.quality || {};
  const breakdown = [
    ["Partial reviews", quality.partial_reviews || 0],
    ["Missing cover", quality.missing_vegetation_cover || 0],
    ["Missing taxon", quality.missing_dominant_taxon || 0],
    ["Integrity flags", quality.integrity_attention || 0],
  ];
  document.querySelector("#quality-breakdown").innerHTML = breakdown.map(([label, count]) => `
    <div><strong>${escapeHtml(count)}</strong><span>${escapeHtml(label)}</span></div>
  `).join("");
  document.querySelector("#dashboard-issue-count").textContent = `${quality.issue_batch_count || 0} batches`;

  const issues = (data.issues || []).slice(0, 8);
  document.querySelector("#dashboard-issues").innerHTML = issues.map((issue) => `
    <button class="issue-row issue-${escapeHtml(issue.severity)}" data-open-batch="${escapeHtml(issue.batch_id)}" type="button">
      <span class="issue-marker" aria-hidden="true"></span>
      <span class="issue-identity">
        <strong>Site ${escapeHtml(issue.site_id)} · ${escapeHtml(formatShortDate(issue.observation_date))}</strong>
        <small>${escapeHtml(issue.site_name)}</small>
      </span>
      <span class="issue-reasons">${escapeHtml(issue.reasons.join(" · "))}</span>
      <i data-lucide="arrow-up-right" aria-hidden="true"></i>
    </button>
  `).join("") || '<div class="empty-state">No data-quality exceptions.</div>';
}

function renderPipelineDashboard(data) {
  const counts = data.counts || {};
  const statuses = [
    ["needs_review", "Photo review"],
    ["observation_review", "Observation review"],
    ["ready", "Ready"],
    ["published", "Published"],
    ["failed", "Failed"],
  ];
  const total = Math.max(1, statuses.reduce((sum, [key]) => sum + (counts[key] || 0), 0));
  document.querySelector("#pipeline-breakdown").innerHTML = statuses.map(([key, label]) => `
    <div class="pipeline-row">
      <span>${escapeHtml(label)}</span>
      <div><i class="pipeline-fill pipeline-${escapeHtml(key)}" style="width: ${((counts[key] || 0) / total) * 100}%"></i></div>
      <strong>${escapeHtml(counts[key] || 0)}</strong>
    </div>
  `).join("");

  const provenance = Object.entries(data.review_provenance_counts || {});
  const provenanceTotal = Math.max(1, provenance.reduce((sum, [, count]) => sum + count, 0));
  document.querySelector("#provenance-bar").innerHTML = provenance.map(([actor, count]) => `
    <i class="provenance-${escapeHtml(actor)}" style="width: ${(count / provenanceTotal) * 100}%" title="${escapeHtml(provenanceLabel(actor))}: ${escapeHtml(count)}"></i>
  `).join("");
  document.querySelector("#provenance-legend").innerHTML = provenance.map(([actor, count]) => `
    <span><i class="provenance-key provenance-${escapeHtml(actor)}"></i>${escapeHtml(provenanceLabel(actor))} <strong>${escapeHtml(count)}</strong></span>
  `).join("") || '<span>No review provenance recorded</span>';

  const summary = data.summary || {};
  document.querySelector("#dashboard-latest-batch").textContent = formatDateTime(summary.latest_batch_created_at);
  const sync = summary.latest_sync;
  document.querySelector("#dashboard-latest-sync").textContent = sync
    ? `${publicationStatus(sync.status)} · ${formatDateTime(sync.completed_at || sync.started_at)}`
    : "Not configured";
}

function matrixCellClass(cell) {
  if (!cell) return "matrix-empty";
  if (!["published", "ready"].includes(cell.pipeline_status)) return "matrix-open";
  return cell.completion_status === "reviewed" ? "matrix-reviewed" : "matrix-partial";
}

function matrixCellIcon(cell) {
  if (!cell) return "minus";
  if (!["published", "ready"].includes(cell.pipeline_status)) return "clock-3";
  return cell.completion_status === "reviewed" ? "check" : "circle-dot";
}

function renderSiteMatrix(data) {
  const matrix = data.site_matrix || {dates: [], rows: []};
  const header = matrix.dates.map((date) => `<th scope="col" title="${escapeHtml(date)}">${escapeHtml(formatShortDate(date))}</th>`).join("");
  const rows = matrix.rows.map((site) => `
    <tr>
      <th scope="row"><strong>Site ${escapeHtml(site.site_id)}</strong><small>${escapeHtml(site.site_name)}</small></th>
      ${site.cells.map((cell) => {
        if (!cell) return '<td><span class="matrix-cell matrix-empty"><i data-lucide="minus" aria-hidden="true"></i></span></td>';
        const title = `${cell.date}: ${cell.completion_status}; ${cell.verified_photo_count}/${cell.photo_count} photos verified; ${cell.missing_field_count} key fields missing`;
        return `<td><button class="matrix-cell ${matrixCellClass(cell)}" data-open-batch="${escapeHtml(cell.batch_id)}" type="button" title="${escapeHtml(title)}" aria-label="${escapeHtml(title)}"><i data-lucide="${matrixCellIcon(cell)}" aria-hidden="true"></i><small>${escapeHtml(cell.photo_count)}</small></button></td>`;
      }).join("")}
    </tr>
  `).join("");
  document.querySelector("#site-matrix").innerHTML = matrix.dates.length
    ? `<table class="matrix-table"><thead><tr><th scope="col">Site</th>${header}</tr></thead><tbody>${rows}</tbody></table>`
    : '<div class="empty-state">No observation batches registered.</div>';
}

function chartX(date, minTime, maxTime, left, width) {
  if (maxTime === minTime) return left + width / 2;
  return left + ((new Date(`${date}T12:00:00`).getTime() - minTime) / (maxTime - minTime)) * width;
}

function renderVegetationTrend(data) {
  const sites = data.longitudinal?.sites || [];
  const select = document.querySelector("#dashboard-site-filter");
  if (!state.dashboardSiteFilter || !sites.some((site) => site.site_id === state.dashboardSiteFilter)) {
    state.dashboardSiteFilter = sites.find((site) => site.points.length)?.site_id || sites[0]?.site_id || "";
  }
  select.innerHTML = sites.map((site) => `
    <option value="${escapeHtml(site.site_id)}" ${site.site_id === state.dashboardSiteFilter ? "selected" : ""}>Site ${escapeHtml(site.site_id)}: ${escapeHtml(site.site_name)}</option>
  `).join("");
  const site = sites.find((item) => item.site_id === state.dashboardSiteFilter);
  const points = site?.points || [];
  const valid = points.filter((point) => point.vegetation_cover_pct !== null);
  if (!valid.length) {
    document.querySelector("#vegetation-chart").innerHTML = '<div class="empty-state">No vegetation-cover estimates for this site.</div>';
    document.querySelector("#vegetation-table").innerHTML = "";
    return;
  }

  const width = 820;
  const height = 250;
  const plot = {left: 48, top: 18, width: 748, height: 190};
  const times = points.map((point) => new Date(`${point.date}T12:00:00`).getTime());
  const minTime = Math.min(...times);
  const maxTime = Math.max(...times);
  const coordinates = valid.map((point) => ({
    ...point,
    x: chartX(point.date, minTime, maxTime, plot.left, plot.width),
    y: plot.top + ((100 - point.vegetation_cover_pct) / 100) * plot.height,
  }));
  const grid = [0, 25, 50, 75, 100].map((value) => {
    const y = plot.top + ((100 - value) / 100) * plot.height;
    return `<line x1="${plot.left}" y1="${y}" x2="${plot.left + plot.width}" y2="${y}"></line><text x="${plot.left - 9}" y="${y + 4}" text-anchor="end">${value}%</text>`;
  }).join("");
  const line = coordinates.map((point) => `${point.x},${point.y}`).join(" ");
  const circles = coordinates.map((point) => `
    <circle cx="${point.x}" cy="${point.y}" r="5" tabindex="0" data-open-batch="${escapeHtml(point.batch_id)}">
      <title>${escapeHtml(point.date)}: ${escapeHtml(point.vegetation_cover_pct)}% vegetation cover</title>
    </circle>
  `).join("");
  const dateLabels = [points[0], points[Math.floor((points.length - 1) / 2)], points[points.length - 1]]
    .filter((point, index, values) => values.findIndex((value) => value.date === point.date) === index)
    .map((point) => {
      const x = chartX(point.date, minTime, maxTime, plot.left, plot.width);
      return `<text x="${x}" y="${height - 13}" text-anchor="middle">${escapeHtml(formatShortDate(point.date))}</text>`;
    }).join("");
  document.querySelector("#vegetation-chart").innerHTML = `
    <svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Vegetation cover over time for Site ${escapeHtml(site.site_id)}">
      <g class="chart-grid">${grid}</g>
      <polyline class="cover-line" points="${line}"></polyline>
      <g class="cover-points">${circles}</g>
      <g class="chart-labels">${dateLabels}</g>
    </svg>`;
  document.querySelector("#vegetation-table").innerHTML = `
    <table class="trend-table">
      <thead><tr><th>Date</th><th>Vegetation</th><th>Bare ground</th><th>Gravel / rock</th><th>Review</th><th><span class="visually-hidden">Open batch</span></th></tr></thead>
      <tbody>${points.map((point) => `
        <tr>
          <td>${escapeHtml(point.date)}</td>
          <td>${point.vegetation_cover_pct === null ? "-" : `${escapeHtml(point.vegetation_cover_pct)}%`}</td>
          <td>${point.bare_ground_pct === null ? "-" : `${escapeHtml(point.bare_ground_pct)}%`}</td>
          <td>${point.gravel_rock_pct === null ? "-" : `${escapeHtml(point.gravel_rock_pct)}%`}</td>
          <td>${escapeHtml(point.completion_status.replaceAll("_", " "))}</td>
          <td><button class="table-icon-button" data-open-batch="${escapeHtml(point.batch_id)}" type="button" title="Open batch" aria-label="Open ${escapeHtml(point.date)} batch"><i data-lucide="arrow-up-right" aria-hidden="true"></i></button></td>
        </tr>`).join("")}
      </tbody>
    </table>`;
}

function renderOperationalDashboard() {
  const data = state.dashboard;
  if (!data) return;
  const summary = data.summary || {};
  const counts = data.counts || {};
  const reviewCounts = data.review_status_counts || {};
  const active = (counts.needs_review || 0) + (counts.observation_review || 0) + (counts.ready || 0) + (counts.failed || 0);
  document.querySelector("#dashboard-observations").textContent = summary.observation_count || 0;
  document.querySelector("#dashboard-sites").textContent = `${summary.site_count || 0} sites`;
  document.querySelector("#dashboard-photos").textContent = summary.photo_count || 0;
  document.querySelector("#dashboard-verified").textContent = `${summary.verified_photo_count || 0} verified`;
  document.querySelector("#dashboard-partial").textContent = reviewCounts.partially_reviewed || 0;
  document.querySelector("#dashboard-reviewed").textContent = `${reviewCounts.reviewed || 0} fully reviewed`;
  document.querySelector("#dashboard-unresolved").textContent = counts.unresolved_photos || 0;
  document.querySelector("#dashboard-active").textContent = `${active} active batches`;
  document.querySelector("#dashboard-published").textContent = counts.published || 0;
  document.querySelector("#dashboard-last-published").textContent = summary.last_published_at
    ? formatDateTime(summary.last_published_at)
    : "Not yet";
  document.querySelector("#dashboard-freshness").textContent = `Updated ${formatDateTime(data.generated_at)}`;
  renderQualityDashboard(data);
  renderPipelineDashboard(data);
  renderSiteMatrix(data);
  renderVegetationTrend(data);
  bindDashboardBatchLinks();
  iconRefresh();
}

function scheduleDashboardRefresh() {
  clearTimeout(state.dashboardRefreshTimer);
  if (state.appView !== "dashboard") return;
  const seconds = state.dashboard?.refresh_interval_seconds || 15;
  state.dashboardRefreshTimer = setTimeout(async () => {
    if (!document.hidden) await loadDashboard({silent: true});
    scheduleDashboardRefresh();
  }, seconds * 1000);
}

async function loadDashboard({silent = false} = {}) {
  if (state.dashboardLoading) return;
  state.dashboardLoading = true;
  const refresh = document.querySelector("#refresh-dashboard-button");
  if (refresh) refresh.disabled = true;
  try {
    state.dashboard = await api("/api/dashboard");
    const counts = state.dashboard.counts;
    document.querySelector("#metric-review").textContent = counts.needs_review;
    document.querySelector("#metric-observations").textContent = counts.observation_review;
    document.querySelector("#metric-ready").textContent = counts.ready;
    document.querySelector("#metric-published").textContent = counts.published;
    renderBatches();
    renderOperationalDashboard();
  } catch (error) {
    if (!silent) showToast(error.message, true);
  } finally {
    state.dashboardLoading = false;
    if (refresh) refresh.disabled = false;
  }
}

async function loadMachineReviewConfig() {
  state.machineReviewConfig = await api("/api/machine-review/config");
}

function filteredBatches() {
  const batches = state.dashboard?.batches || [];
  if (state.filter === "published") return batches.filter((batch) => batch.status === "published");
  if (state.filter === "active") return batches.filter((batch) => batch.status !== "published");
  return batches;
}

function renderBatches() {
  const target = document.querySelector("#batch-list");
  const batches = filteredBatches();
  if (!batches.length) {
    const message = state.filter === "active"
      ? "No open batches. Stage a photo batch to begin."
      : "No batches in this view.";
    target.innerHTML = `<div class="empty-state">${message}</div>`;
    return;
  }
  target.innerHTML = batches.map((batch) => {
    const selected = state.selectedBatch?.batch_id === batch.batch_id ? " selected" : "";
    return `
      <button class="batch-row${selected}" data-batch-id="${escapeHtml(batch.batch_id)}" type="button">
        <span>
          <strong>${escapeHtml(siteLabel(batch.site_id))}</strong>
          <small>${escapeHtml(batch.batch_id)}</small>
        </span>
        <span class="batch-date">${escapeHtml(batch.observation_date)}</span>
        <span class="batch-count">${batch.reviewed_count || 0} / ${batch.photo_count} reviewed</span>
        <span class="status-badge status-${escapeHtml(batch.status)}">${escapeHtml(statusLabel(batch.status))}</span>
        <i data-lucide="chevron-right" aria-hidden="true"></i>
      </button>`;
  }).join("");
  target.querySelectorAll(".batch-row").forEach((row) => {
    row.addEventListener("click", () => selectBatch(row.dataset.batchId));
  });
  iconRefresh();
}

async function selectBatch(batchId) {
  try {
    state.selectedBatch = await api(`/api/batches/${encodeURIComponent(batchId)}`);
    state.reviewView = ["observation_review", "ready", "published"].includes(state.selectedBatch.status)
      ? "observation"
      : "photos";
    renderBatches();
    renderReview();
    document.querySelector("#review-workspace").scrollIntoView({behavior: "smooth", block: "start"});
  } catch (error) {
    showToast(error.message, true);
  }
}

function renderReview() {
  const batch = state.selectedBatch;
  const workspace = document.querySelector("#review-workspace");
  workspace.classList.remove("hidden");
  document.querySelector("#review-title").textContent = siteLabel(batch.site_id);
  document.querySelector("#review-subtitle").textContent = `${batch.observation_date} | ${batch.photo_count} photos | ${statusLabel(batch.status)}`;
  const reviewed = batch.photos.filter((photo) => photo.review_status === "verified").length;
  const percent = batch.photos.length ? Math.round((reviewed / batch.photos.length) * 100) : 0;
  document.querySelector("#progress-bar").style.width = `${percent}%`;
  document.querySelector("#progress-label").textContent = `${reviewed} of ${batch.photos.length} reviewed`;
  document.querySelector("#accept-button").disabled = batch.status === "published";
  const publish = document.querySelector("#publish-button");
  publish.disabled = batch.status !== "ready";
  publish.innerHTML = batch.status === "published"
    ? '<i data-lucide="check" aria-hidden="true"></i> Published'
    : '<i data-lucide="database" aria-hidden="true"></i> Publish metadata';

  document.querySelector("#photo-grid").innerHTML = batch.photos.map(photoCard).join("");
  document.querySelectorAll(".save-photo").forEach((button) => {
    button.addEventListener("click", () => savePhoto(button.dataset.photoId));
  });
  renderObservationReview();
  renderReviewView();
  iconRefresh();
}

function renderReviewView() {
  const photosActive = state.reviewView === "photos";
  document.querySelector("#photos-tab").classList.toggle("active", photosActive);
  document.querySelector("#photos-tab").setAttribute("aria-selected", String(photosActive));
  document.querySelector("#observation-tab").classList.toggle("active", !photosActive);
  document.querySelector("#observation-tab").setAttribute("aria-selected", String(!photosActive));
  document.querySelector("#photo-review-panel").classList.toggle("hidden", !photosActive);
  document.querySelector("#observation-review-panel").classList.toggle("hidden", photosActive);
  document.querySelector("#accept-button").classList.toggle("hidden", !photosActive);
}

function setObservationField(form, name, value) {
  const field = form.elements.namedItem(name);
  if (field) field.value = value ?? "";
}

function fieldLabel(name) {
  return String(name || "")
    .replaceAll("_pct", " %")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function applyMachineDraft(result) {
  const values = result?.draft?.values || {};
  const form = document.querySelector("#observation-review-form");
  form.querySelectorAll(".machine-suggested").forEach((field) => field.classList.remove("machine-suggested"));
  Object.entries(values).forEach(([name, value]) => {
    if (value === null || value === undefined || name === "completion_status") return;
    const field = form.elements.namedItem(name);
    if (!field) return;
    field.value = typeof value === "boolean" ? String(value) : value;
    field.classList.add("machine-suggested");
  });
}

function renderMachineReview() {
  const batch = state.selectedBatch;
  const config = state.machineReviewConfig || {};
  const button = document.querySelector("#machine-review-button");
  const configState = document.querySelector("#machine-config-state");
  const photoReviewComplete = batch.photos.every((photo) => photo.review_status === "verified");
  const available = Boolean(config.available);
  button.disabled = !available || !photoReviewComplete || state.machineReviewLoading;
  button.innerHTML = state.machineReviewLoading
    ? '<i data-lucide="loader-circle" aria-hidden="true"></i> Reviewing photos...'
    : '<i data-lucide="scan-search" aria-hidden="true"></i> Generate machine draft';
  if (!available) {
    configState.textContent = config.reason || "Machine review is not configured.";
  } else if (!photoReviewComplete) {
    configState.textContent = "Verify every photo role before requesting a draft.";
  } else {
    configState.textContent = `${config.provider} | ${config.model}`;
  }

  const result = batch.latest_machine_review;
  const panel = document.querySelector("#machine-result");
  panel.classList.toggle("hidden", !result);
  if (!result) return;
  document.querySelector("#machine-summary").textContent = result.draft?.overall_summary || "No summary returned.";
  const priorities = result.draft?.review_priority_fields || [];
  document.querySelector("#machine-priority").textContent = priorities.length
    ? `Check carefully: ${priorities.map(fieldLabel).join(", ")}.`
    : "No fields were specially flagged, but human review is still required.";
  const assessments = result.draft?.assessments || [];
  document.querySelector("#machine-assessments").innerHTML = assessments.length
    ? assessments.map((item) => `<li><strong>${escapeHtml(fieldLabel(item.field_name))}</strong> <span>${escapeHtml(item.confidence)}</span><p>${escapeHtml(item.evidence)}</p></li>`).join("")
    : "<li>No field-level assessments were returned.</li>";
}

function renderObservationReview() {
  const batch = state.selectedBatch;
  const form = document.querySelector("#observation-review-form");
  const review = batch.observation_review || {};
  [...form.elements].forEach((field) => {
    if (field.name) setObservationField(form, field.name, review[field.name]);
  });
  if (!review.notes && batch.observation_notes) {
    setObservationField(form, "notes", batch.observation_notes);
  }
  const photoReviewComplete = batch.photos.every((photo) => photo.review_status === "verified");
  const published = batch.status === "published";
  [...form.elements].forEach((field) => {
    field.disabled = !photoReviewComplete;
  });
  const stageNote = document.querySelector("#observation-stage-note");
  if (!photoReviewComplete) {
    stageNote.textContent = "Finish classifying every photo before reviewing this observation.";
    stageNote.className = "observation-stage-note warning";
  } else if (published) {
    stageNote.textContent = "This observation is published. Saving a new review opens a metadata revision without duplicating its photos.";
    stageNote.className = "observation-stage-note complete";
  } else if (review.is_final) {
    stageNote.textContent = "Observation review complete. This batch is ready to publish.";
    stageNote.className = "observation-stage-note complete";
  } else {
    stageNote.textContent = "Record only what the photos or field notes support. Blank values remain null.";
    stageNote.className = "observation-stage-note";
  }
  const events = batch.observation_review_events || [];
  const humanEvents = events.filter((event) => ["draft_saved", "finalized", "corrected"].includes(event.action));
  document.querySelector("#observation-save-state").textContent = humanEvents.length
    ? `${humanEvents.length} saved human review ${humanEvents.length === 1 ? "version" : "versions"}`
    : (published ? "Legacy publication; no local review history" : "Not saved yet");
  renderMachineReview();
}

async function generateMachineReview() {
  if (!state.selectedBatch || state.machineReviewLoading) return;
  state.machineReviewLoading = true;
  renderMachineReview();
  iconRefresh();
  try {
    const result = await api(
      `/api/batches/${encodeURIComponent(state.selectedBatch.batch_id)}/machine-review`,
      {method: "POST"},
    );
    state.selectedBatch.latest_machine_review = result;
    applyMachineDraft(result);
    renderMachineReview();
    showToast("Machine draft applied. Review and edit it before saving.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    state.machineReviewLoading = false;
    renderMachineReview();
    iconRefresh();
  }
}

function photoCard(photo) {
  const selected = photo.reviewed_type || (photo.candidate_type !== "unclassified" ? photo.candidate_type : "");
  const options = photoTypes.map(([value, label]) =>
    `<option value="${value}" ${value === selected ? "selected" : ""}>${label}</option>`
  ).join("");
  const confidence = Math.round(Number(photo.candidate_confidence) * 100);
  const preview = photo.preview_filename
    ? `<img src="/api/photos/${encodeURIComponent(photo.photo_id)}/preview" alt="Preview of ${escapeHtml(photo.source_filename)}" loading="lazy">`
    : '<div class="no-preview">Preview unavailable</div>';
  const warning = photo.date_warning
    ? `<span class="warning-badge" title="EXIF capture date differs from the batch date">Date check</span>`
    : "";
  const verified = photo.review_status === "verified"
    ? '<span class="verified-badge" title="Role verified"><i data-lucide="check" aria-hidden="true"></i></span>'
    : "";
  return `
    <article class="photo-card">
      <div class="photo-preview">${preview}${warning}${verified}</div>
      <div class="photo-fields">
        <p class="filename" title="${escapeHtml(photo.source_filename)}">${escapeHtml(photo.source_filename)}</p>
        <p class="candidate"><strong>Suggestion: ${escapeHtml(photo.candidate_type)}</strong> (${confidence}%)<br>${escapeHtml(photo.candidate_reason)}</p>
        <label for="type-${escapeHtml(photo.photo_id)}">Photo role</label>
        <select id="type-${escapeHtml(photo.photo_id)}" class="photo-type">${options}</select>
        <label for="notes-${escapeHtml(photo.photo_id)}">Review note</label>
        <input id="notes-${escapeHtml(photo.photo_id)}" class="photo-note" value="${escapeHtml(photo.review_notes)}" placeholder="Optional">
        <div class="save-row">
          <button class="secondary-button save-photo" data-photo-id="${escapeHtml(photo.photo_id)}" type="button">
            <i data-lucide="check" aria-hidden="true"></i> Verify role
          </button>
        </div>
      </div>
    </article>`;
}

async function savePhoto(photoId) {
  const type = document.querySelector(`#type-${CSS.escape(photoId)}`).value;
  const notes = document.querySelector(`#notes-${CSS.escape(photoId)}`).value;
  if (!type) {
    showToast("Choose a photo role first.", true);
    return;
  }
  try {
    state.selectedBatch = await api(`/api/photos/${encodeURIComponent(photoId)}`, {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({photo_type: type, notes}),
    });
    if (state.selectedBatch.status === "observation_review") {
      state.reviewView = "observation";
    }
    await loadDashboard();
    renderReview();
    showToast("Photo role verified.");
  } catch (error) {
    showToast(error.message, true);
  }
}

function observationPayload(form, finalize) {
  const payload = {};
  const numberFields = new Set([
    "estimated_total_vegetation_cover_pct",
    "bare_ground_pct",
    "gravel_rock_pct",
    "litter_mulch_woodchip_pct",
    "standing_dead_pct",
  ]);
  for (const [name, value] of new FormData(form).entries()) {
    const normalized = String(value).trim();
    payload[name] = numberFields.has(name)
      ? (normalized === "" ? null : Number(normalized))
      : normalized;
  }
  payload.finalize = finalize;
  return payload;
}

async function saveObservationReview(event) {
  event.preventDefault();
  const button = event.submitter;
  const finalize = button?.dataset.observationAction === "complete";
  const form = event.currentTarget;
  const buttons = form.querySelectorAll("button");
  buttons.forEach((item) => { item.disabled = true; });
  try {
    state.selectedBatch = await api(
      `/api/batches/${encodeURIComponent(state.selectedBatch.batch_id)}/observation-review`,
      {
        method: "PUT",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(observationPayload(form, finalize)),
      },
    );
    state.reviewView = "observation";
    await loadDashboard();
    renderReview();
    showToast(finalize ? "Observation review complete." : "Observation draft saved.");
  } catch (error) {
    showToast(error.message, true);
  } finally {
    if (state.selectedBatch) renderObservationReview();
  }
}

async function uploadBatch(event) {
  event.preventDefault();
  const button = document.querySelector("#upload-button");
  const files = document.querySelector("#files").files;
  if (!files.length) {
    showToast("Choose at least one photo.", true);
    return;
  }
  if (!state.uploadToken) {
    showToast("Wait for photo metadata preparation to finish.", true);
    return;
  }
  if (state.integrityRequiresOverride && !document.querySelector("#integrity-override-confirmed").checked) {
    showToast("Review and acknowledge the batch-integrity exceptions first.", true);
    return;
  }
  button.disabled = true;
  button.innerHTML = '<i data-lucide="loader-circle" aria-hidden="true"></i> Staging photos...';
  iconRefresh();
  try {
    const form = new FormData(event.target);
    form.delete("files");
    form.append("upload_token", state.uploadToken);
    state.selectedBatch = await api("/api/batches/from-suggestion", {method: "POST", body: form});
    state.reviewView = "photos";
    state.uploadToken = null;
    state.uploadSuggestion = null;
    state.integrityRequiresOverride = false;
    event.target.reset();
    document.querySelector("#observation-date").valueAsDate = new Date();
    document.querySelector("#file-summary").textContent = "No files selected";
    document.querySelector("#metadata-suggestion").classList.add("hidden");
    document.querySelector("#batch-integrity").classList.add("hidden");
    await loadDashboard();
    renderReview();
    showToast(`Staged ${state.selectedBatch.photo_count} photos.`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.innerHTML = '<i data-lucide="upload" aria-hidden="true"></i> Stage batch';
    updateStageButton();
    iconRefresh();
  }
}

function renderMetadataSuggestion(result) {
  const target = document.querySelector("#metadata-suggestion");
  const parts = [];
  if (result.site_id) {
    const distance = result.median_distance_meters === null
      ? ""
      : `, median ${result.median_distance_meters} m from anchor`;
    parts.push(`${siteLabel(result.site_id)} (${result.site_confidence} confidence${distance})`);
    document.querySelector("#site-id").value = String(result.site_id);
  }
  if (result.observation_date) {
    parts.push(`${result.observation_date} (${result.date_confidence} confidence)`);
    document.querySelector("#observation-date").value = result.observation_date;
  }
  const warningText = result.warnings.length ? ` ${result.warnings.join(" ")}` : "";
  target.textContent = parts.length
    ? `Suggested ${parts.join(" and ")}. Confirm before staging.${warningText}`
    : `No site or date could be suggested.${warningText}`;
  target.classList.remove("hidden");
  target.classList.toggle("warning", Boolean(result.warnings.length) || result.site_confidence === "low" || result.date_confidence === "low");
}

function integrityResult(file, siteId, observationDate) {
  const problems = [];
  let blocking = false;
  if (!file.gps_present || !file.nearest_site_id) {
    problems.push("GPS metadata unavailable");
    blocking = true;
  } else if (Number(file.distance_meters) > 150) {
    problems.push(`location is ${file.distance_meters} m from the nearest site anchor`);
    blocking = true;
  } else if (String(file.nearest_site_id) !== String(siteId)) {
    problems.push(`nearest to Site ${file.nearest_site_id} (${file.distance_meters} m)`);
    blocking = true;
  } else if (file.runner_up_margin_meters !== null && Number(file.runner_up_margin_meters) < 15) {
    problems.push("location is close to another site anchor");
  }

  const captureDate = file.captured_at ? String(file.captured_at).slice(0, 10) : null;
  if (!captureDate) {
    problems.push("capture date unavailable");
    blocking = true;
  } else if (captureDate !== observationDate) {
    problems.push(`captured ${captureDate}`);
    blocking = true;
  }
  return {problems, blocking};
}

function updateStageButton() {
  const button = document.querySelector("#upload-button");
  const siteId = document.querySelector("#site-id").value;
  const observationDate = document.querySelector("#observation-date").value;
  const override = document.querySelector("#integrity-override-confirmed").checked;
  button.disabled = !state.uploadToken || !siteId || !observationDate
    || (state.integrityRequiresOverride && !override);
}

function renderBatchIntegrity() {
  const panel = document.querySelector("#batch-integrity");
  const summary = document.querySelector("#integrity-summary");
  const list = document.querySelector("#integrity-issues");
  const overrideRow = document.querySelector("#integrity-override");
  const override = document.querySelector("#integrity-override-confirmed");
  if (!state.uploadSuggestion) {
    state.integrityRequiresOverride = false;
    panel.classList.add("hidden");
    updateStageButton();
    return;
  }

  const siteId = document.querySelector("#site-id").value;
  const observationDate = document.querySelector("#observation-date").value;
  panel.classList.remove("hidden");
  if (!siteId || !observationDate) {
    state.integrityRequiresOverride = false;
    summary.textContent = "Confirm a site and date to check every selected photo.";
    list.innerHTML = "";
    overrideRow.classList.add("hidden");
    updateStageButton();
    return;
  }

  const exceptions = state.uploadSuggestion.files.map((file) => ({
    file,
    ...integrityResult(file, siteId, observationDate),
  })).filter((item) => item.problems.length);
  const blocking = exceptions.filter((item) => item.blocking);
  state.integrityRequiresOverride = blocking.length > 0;
  panel.classList.toggle("attention", exceptions.length > 0);
  if (!exceptions.length) {
    summary.textContent = `${state.uploadSuggestion.files.length} photos match ${siteLabel(siteId)} and ${observationDate}.`;
    list.innerHTML = "";
    override.checked = false;
    overrideRow.classList.add("hidden");
  } else {
    summary.textContent = blocking.length
      ? `${blocking.length} of ${state.uploadSuggestion.files.length} photos need attention before staging.`
      : `${exceptions.length} photos have location ambiguity; review the details below.`;
    list.innerHTML = exceptions.map(({file, problems, blocking: isBlocking}) => `
      <li><strong>${escapeHtml(file.filename)}</strong>: ${escapeHtml(problems.join("; "))}${isBlocking ? "" : " (warning)"}</li>
    `).join("");
    overrideRow.classList.toggle("hidden", !state.integrityRequiresOverride);
    if (!state.integrityRequiresOverride) override.checked = false;
  }
  updateStageButton();
  iconRefresh();
}

async function prepareUpload(files) {
  const button = document.querySelector("#upload-button");
  const target = document.querySelector("#metadata-suggestion");
  const preparationId = ++state.preparationId;
  state.uploadToken = null;
  state.uploadSuggestion = null;
  state.integrityRequiresOverride = false;
  document.querySelector("#batch-integrity").classList.add("hidden");
  document.querySelector("#integrity-override-confirmed").checked = false;
  target.textContent = "Reading capture time and location metadata...";
  target.classList.remove("hidden", "warning");
  button.disabled = true;
  button.innerHTML = '<i data-lucide="loader-circle" aria-hidden="true"></i> Preparing photos...';
  iconRefresh();
  try {
    const form = new FormData();
    files.forEach((file) => form.append("files", file));
    const result = await api("/api/upload-suggestions", {method: "POST", body: form});
    if (preparationId !== state.preparationId) return;
    state.uploadToken = result.upload_token;
    state.uploadSuggestion = result;
    renderMetadataSuggestion(result);
    renderBatchIntegrity();
  } catch (error) {
    if (preparationId !== state.preparationId) return;
    target.textContent = error.message;
    target.classList.remove("hidden");
    target.classList.add("warning");
    showToast(error.message, true);
  } finally {
    if (preparationId === state.preparationId) {
      button.innerHTML = '<i data-lucide="upload" aria-hidden="true"></i> Stage batch';
      updateStageButton();
      iconRefresh();
    }
  }
}

async function acceptSuggestions() {
  if (!state.selectedBatch) return;
  try {
    state.selectedBatch = await api(`/api/batches/${encodeURIComponent(state.selectedBatch.batch_id)}/accept-suggestions`, {method: "POST"});
    await loadDashboard();
    renderReview();
    showToast("Explicit filename suggestions accepted.");
  } catch (error) {
    showToast(error.message, true);
  }
}

async function publishBatch() {
  if (!state.selectedBatch) return;
  try {
    state.selectedBatch = await api(`/api/batches/${encodeURIComponent(state.selectedBatch.batch_id)}/publish`, {method: "POST"});
    await loadDashboard();
    if (state.publishedData) await loadPublishedData();
    renderReview();
    showToast("Validated metadata published to the public dataset.");
  } catch (error) {
    showToast(error.message, true);
  }
}

function setupFilePicker() {
  const input = document.querySelector("#files");
  const summary = document.querySelector("#file-summary");
  const zone = document.querySelector("#drop-zone");
  input.addEventListener("change", async () => {
    const total = [...input.files].reduce((sum, file) => sum + file.size, 0);
    summary.textContent = input.files.length
      ? `${input.files.length} files | ${(total / 1024 / 1024).toFixed(1)} MB`
      : "No files selected";
    if (input.files.length) {
      await prepareUpload([...input.files]);
    } else {
      state.uploadToken = null;
      state.uploadSuggestion = null;
      state.integrityRequiresOverride = false;
      state.preparationId += 1;
      document.querySelector("#metadata-suggestion").classList.add("hidden");
      document.querySelector("#batch-integrity").classList.add("hidden");
      updateStageButton();
    }
  });
  ["dragenter", "dragover"].forEach((name) => zone.addEventListener(name, () => zone.classList.add("dragging")));
  ["dragleave", "drop"].forEach((name) => zone.addEventListener(name, () => zone.classList.remove("dragging")));
}

async function initialize() {
  document.querySelector("#observation-date").valueAsDate = new Date();
  document.querySelector("#upload-form").addEventListener("submit", uploadBatch);
  document.querySelector("#site-id").addEventListener("change", renderBatchIntegrity);
  document.querySelector("#observation-date").addEventListener("change", renderBatchIntegrity);
  document.querySelector("#integrity-override-confirmed").addEventListener("change", updateStageButton);
  document.querySelector("#accept-button").addEventListener("click", acceptSuggestions);
  document.querySelector("#publish-button").addEventListener("click", publishBatch);
  document.querySelector("#machine-review-button").addEventListener("click", generateMachineReview);
  document.querySelector("#observation-review-form").addEventListener("submit", saveObservationReview);
  document.querySelector("#refresh-dashboard-button").addEventListener("click", async () => {
    await loadDashboard();
    scheduleDashboardRefresh();
  });
  document.querySelector("#dashboard-site-filter").addEventListener("change", (event) => {
    state.dashboardSiteFilter = event.target.value;
    renderVegetationTrend(state.dashboard);
    bindDashboardBatchLinks();
    iconRefresh();
  });
  document.querySelector("#refresh-published-button").addEventListener("click", loadPublishedData);
  document.querySelector("#sync-sheets-button").addEventListener("click", syncGoogleSheets);
  document.querySelectorAll(".app-nav-button").forEach((button) => button.addEventListener("click", () => {
    setAppView(button.dataset.appView);
  }));
  document.querySelectorAll(".review-tab").forEach((button) => button.addEventListener("click", () => {
    state.reviewView = button.dataset.reviewView;
    renderReviewView();
  }));
  document.querySelectorAll(".segment").forEach((button) => button.addEventListener("click", () => {
    state.filter = button.dataset.filter;
    document.querySelectorAll(".segment").forEach((segment) => segment.classList.toggle("active", segment === button));
    renderBatches();
  }));
  setupFilePicker();
  iconRefresh();
  try {
    await Promise.all([loadSites(), loadMachineReviewConfig()]);
    await loadDashboard();
    scheduleDashboardRefresh();
  } catch (error) {
    document.querySelector("#service-state").classList.add("offline");
    document.querySelector("#service-state").lastChild.textContent = " Offline";
    showToast(error.message, true);
  }
}

window.addEventListener("DOMContentLoaded", initialize);
