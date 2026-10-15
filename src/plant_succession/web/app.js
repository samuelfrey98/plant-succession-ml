const state = {
  dashboard: null,
  sites: [],
  selectedBatch: null,
  filter: "active",
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
    ready: "Ready to publish",
    published: "Published",
    failed: "Failed",
    staged: "Staging",
  }[status] || status;
}

async function loadSites() {
  state.sites = await api("/api/sites");
  const select = document.querySelector("#site-id");
  select.innerHTML = '<option value="">Choose a site</option>' + state.sites.map((site) =>
    `<option value="${escapeHtml(site.site_id)}">Site ${escapeHtml(site.site_id)}: ${escapeHtml(site.site_name)}</option>`
  ).join("");
}

async function loadDashboard() {
  state.dashboard = await api("/api/dashboard");
  const counts = state.dashboard.counts;
  document.querySelector("#metric-review").textContent = counts.needs_review;
  document.querySelector("#metric-photos").textContent = counts.unresolved_photos;
  document.querySelector("#metric-ready").textContent = counts.ready;
  document.querySelector("#metric-published").textContent = counts.published;
  renderBatches();
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
  iconRefresh();
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
    await loadDashboard();
    renderReview();
    showToast("Photo role verified.");
  } catch (error) {
    showToast(error.message, true);
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
  button.disabled = true;
  button.innerHTML = '<i data-lucide="loader-circle" aria-hidden="true"></i> Staging photos...';
  iconRefresh();
  try {
    const form = new FormData(event.target);
    state.selectedBatch = await api("/api/batches", {method: "POST", body: form});
    event.target.reset();
    document.querySelector("#observation-date").valueAsDate = new Date();
    document.querySelector("#file-summary").textContent = "No files selected";
    await loadDashboard();
    renderReview();
    showToast(`Staged ${state.selectedBatch.photo_count} photos.`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.innerHTML = '<i data-lucide="upload" aria-hidden="true"></i> Stage batch';
    iconRefresh();
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
  input.addEventListener("change", () => {
    const total = [...input.files].reduce((sum, file) => sum + file.size, 0);
    summary.textContent = input.files.length
      ? `${input.files.length} files | ${(total / 1024 / 1024).toFixed(1)} MB`
      : "No files selected";
  });
  ["dragenter", "dragover"].forEach((name) => zone.addEventListener(name, () => zone.classList.add("dragging")));
  ["dragleave", "drop"].forEach((name) => zone.addEventListener(name, () => zone.classList.remove("dragging")));
}

async function initialize() {
  document.querySelector("#observation-date").valueAsDate = new Date();
  document.querySelector("#upload-form").addEventListener("submit", uploadBatch);
  document.querySelector("#accept-button").addEventListener("click", acceptSuggestions);
  document.querySelector("#publish-button").addEventListener("click", publishBatch);
  document.querySelectorAll(".segment").forEach((button) => button.addEventListener("click", () => {
    state.filter = button.dataset.filter;
    document.querySelectorAll(".segment").forEach((segment) => segment.classList.toggle("active", segment === button));
    renderBatches();
  }));
  setupFilePicker();
  iconRefresh();
  try {
    await loadSites();
    await loadDashboard();
  } catch (error) {
    document.querySelector("#service-state").classList.add("offline");
    document.querySelector("#service-state").lastChild.textContent = " Offline";
    showToast(error.message, true);
  }
}

window.addEventListener("DOMContentLoaded", initialize);
