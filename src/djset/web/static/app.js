/* The four panes, driven from the API.
 *
 * Two rules carried over from the desktop build, both of which were bugs there
 * before they were rules:
 *   - No source is selected for you. Pre-ticking Liked Songs meant choosing a
 *     playlist silently produced the union of the two.
 *   - No genre selected means NO FILTER, never "no tracks". They are distinct
 *     states and the code keeps them distinct.
 */

const $ = (id) => document.getElementById(id);
const state = {
  sources: [],          // [{id, name, tracks}]
  selected: new Set(),
  genres: new Set(),    // empty => no filter
  picked: null,         // hand-picked subset, or null
  set: [],              // current result rows
  playlistId: null,
  dirty: false,
};

// ---------------------------------------------------------------------------

async function api(path, body) {
  const res = await fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try { detail = (await res.json()).detail ?? detail; } catch { /* keep status */ }
    throw new Error(detail);
  }
  return res.json();
}

let toastTimer;
function toast(message, bad = false) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("bad", bad);
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, bad ? 8000 : 3500);
}

const selection = () => ({
  sources: [...state.selected],
  genres: state.genres.size ? [...state.genres] : null,
  picked: state.picked,
});

// ---------------------------------------------------------------------------
// pane 1
// ---------------------------------------------------------------------------

function renderSources() {
  const needle = $("source-filter").value.trim().toLowerCase();
  const box = $("sources");
  box.innerHTML = "";
  for (const s of state.sources) {
    if (needle && !s.name.toLowerCase().includes(needle)) continue;
    const row = document.createElement("label");
    row.className = "item";
    row.innerHTML =
      `<input type="checkbox" ${state.selected.has(s.id) ? "checked" : ""}>` +
      `<span>${escapeHtml(s.name)}</span><span class="count">${s.tracks}</span>`;
    row.querySelector("input").addEventListener("change", (e) => {
      e.target.checked ? state.selected.add(s.id) : state.selected.delete(s.id);
      state.picked = null;          // a changed source invalidates a subset
      refreshSelection();
    });
    box.appendChild(row);
  }
}

function sourcesSummary(pool) {
  // Name them rather than counting them. "2 source(s)" is easy to read past,
  // and a set drawn from a playlist you forgot was ticked is this app's most
  // confusing possible output.
  const names = state.sources.filter((s) => state.selected.has(s.id)).map((s) => s.name);
  if (!names.length) return "No source selected";
  const head = names.slice(0, 2).join(", ");
  const more = names.length > 2 ? ` +${names.length - 2} more` : "";
  return `${head}${more} · ${pool} tracks`;
}

// ---------------------------------------------------------------------------
// pane 2
// ---------------------------------------------------------------------------

function renderGenres(data) {
  const notice = $("genre-notice");
  const box = $("genres");
  box.innerHTML = "";

  const a = data.availability;
  if (!a.usable && a.headline) {
    // With nothing tagged every track falls into Unknown, so the list would
    // otherwise offer one bucket covering everything: a control that filters
    // nothing while looking like a working filter.
    notice.innerHTML = `<b>${escapeHtml(a.headline)}</b>${escapeHtml(a.detail || "")}`;
    notice.hidden = false;
    box.hidden = true;
    $("genre-all").disabled = $("genre-none").disabled = true;
    $("genre-toggle").textContent = expanded() ? "Genres unavailable ▾" : "Genres unavailable ▸";
    return;
  }

  notice.hidden = true;
  box.hidden = false;
  $("genre-all").disabled = $("genre-none").disabled = false;
  $("genre-toggle").textContent = expanded() ? "Hide genres ▾" : "Show genres ▸";

  for (const g of data.genres) {
    const row = document.createElement("label");
    row.className = "item";
    row.innerHTML =
      `<input type="checkbox" ${state.genres.has(g.name) ? "checked" : ""}>` +
      `<span>${escapeHtml(g.name)}</span>` +
      `<span class="count">${g.tracks} · ${g.enriched} ready</span>`;
    row.querySelector("input").addEventListener("change", (e) => {
      e.target.checked ? state.genres.add(g.name) : state.genres.delete(g.name);
      refreshSelection();
    });
    box.appendChild(row);
  }
}

const expanded = () => $("genre-toggle").getAttribute("aria-expanded") === "true";

// ---------------------------------------------------------------------------

async function refreshSelection() {
  renderSources();
  const data = await api("/api/selection", selection());
  $("sources-summary").textContent = sourcesSummary(data.pool);
  $("eligible").textContent = data.eligible.label;
  renderGenres(data);
  // Enabled by source selection alone — never by genre selection.
  $("generate").disabled = state.selected.size === 0;
  refreshName();
}

function refreshName() {
  const mode = document.querySelector('input[name=mode]:checked').value.toUpperCase();
  const kind = $("target-kind").value;
  const n = $("target-value").value;
  const tail = kind === "all" ? "whole selection" : `${n} ${kind}`;
  const genres = state.genres.size ? [...state.genres].join(" / ") + " · " : "";
  $("playlist-name").placeholder = `${genres}${mode} · ${tail}`;
}

// ---------------------------------------------------------------------------
// pane 4
// ---------------------------------------------------------------------------

function renderSet(rows) {
  const body = document.querySelector("#result tbody");
  body.innerHTML = "";
  rows.forEach((t, i) => {
    const tr = document.createElement("tr");
    tr.draggable = true;
    tr.dataset.index = i;
    const label = t.transition || "—";
    tr.innerHTML =
      `<td class="num">${i + 1}</td>` +
      `<td>${escapeHtml(t.title)}</td>` +
      `<td>${escapeHtml(t.artist)}</td>` +
      `<td class="num">${t.bpm ? t.bpm.toFixed(0) : "—"}</td>` +
      `<td class="num">${t.key || "—"}</td>` +
      `<td class="t-${label.replace(/\s/g, "-")}">${label}</td>` +
      `<td><button class="drop" title="Remove">✕</button></td>`;
    tr.querySelector(".drop").addEventListener("click", () => {
      state.set.splice(i, 1);
      markDirty();
      renderSet(state.set);
    });
    wireDrag(tr);
    body.appendChild(tr);
  });
}

let dragFrom = null;
function wireDrag(tr) {
  tr.addEventListener("dragstart", () => {
    dragFrom = Number(tr.dataset.index);
    tr.classList.add("dragging");
  });
  tr.addEventListener("dragend", () => tr.classList.remove("dragging"));
  tr.addEventListener("dragover", (e) => { e.preventDefault(); tr.classList.add("over"); });
  tr.addEventListener("dragleave", () => tr.classList.remove("over"));
  tr.addEventListener("drop", (e) => {
    e.preventDefault();
    tr.classList.remove("over");
    const to = Number(tr.dataset.index);
    if (dragFrom === null || dragFrom === to) return;
    const [moved] = state.set.splice(dragFrom, 1);
    state.set.splice(to, 0, moved);
    dragFrom = null;
    markDirty();
    renderSet(state.set);
  });
}

function markDirty() {
  state.dirty = true;
  $("update").disabled = !state.playlistId;
  // The transition labels came from the server for the original order, so
  // after an edit they describe a set that no longer exists. Say so rather
  // than leaving stale text that reads as fact.
  $("result-summary").textContent =
    `${state.set.length} tracks · edited — regenerate or save to re-score transitions`;
}

// ---------------------------------------------------------------------------

async function generate() {
  const btn = $("generate");
  btn.disabled = true;
  btn.textContent = "Sequencing…";
  try {
    const data = await api("/api/generate", {
      ...selection(),
      mode: document.querySelector('input[name=mode]:checked').value,
      tolerance: Number($("tolerance").value),
      half_double: $("half-double").checked,
      energy_boost: $("energy-boost").checked,
      target_kind: $("target-kind").value,
      target_value: Number($("target-value").value),
    });
    state.set = data.tracks;
    state.playlistId = null;
    state.dirty = false;
    renderSet(state.set);

    const mins = Math.round(data.duration_ms / 60000);
    $("result-summary").textContent =
      `${data.count} tracks · ${Math.floor(mins / 60)}h ${String(mins % 60).padStart(2, "0")}m` +
      ` · average transition quality ${Math.round(data.average_quality * 100)}%` +
      (data.compromises ? ` · ${data.compromises} forced` : "");
    $("result-status").textContent = data.reached_target
      ? "Set ready. Nothing has been written to Spotify yet."
      : data.explain;
    $("save").disabled = data.count === 0;
    $("copy").disabled = $("open").disabled = $("update").disabled = true;
    $("link").innerHTML = "";
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = state.selected.size === 0;
    btn.textContent = "Generate set";
  }
}

async function save() {
  const btn = $("save");
  btn.disabled = true;
  btn.textContent = "Saving…";
  try {
    const data = await api("/api/export", {
      name: $("playlist-name").value.trim() || $("playlist-name").placeholder,
      uris: state.set.map((t) => t.uri),
      public: $("public").checked,
    });
    state.playlistId = data.playlist_id;
    state.dirty = false;
    $("link").innerHTML = `<a href="${data.url}" target="_blank" rel="noopener">${data.url}</a>`;
    $("copy").disabled = $("open").disabled = false;
    $("update").disabled = true;
    $("result-status").textContent = data.message;
    toast(data.message);
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = state.set.length === 0;
    btn.textContent = "Save to Spotify";
  }
}

async function update() {
  try {
    await api(`/api/reorder/${state.playlistId}`, {
      name: "", uris: state.set.map((t) => t.uri),
    });
    state.dirty = false;
    $("update").disabled = true;
    toast("Playlist updated");
  } catch (err) {
    toast(err.message, true);
  }
}

// ---------------------------------------------------------------------------

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------------------------------------------------------------------------
// long jobs
// ---------------------------------------------------------------------------

let jobTimer = null;

function renderJob(j) {
  const box = $("job");
  const idle = !j.running && !j.kind;
  if (idle) { box.hidden = true; return; }

  box.hidden = false;
  box.classList.toggle("done", !j.running && !j.error);
  box.classList.toggle("failed", Boolean(j.error));
  if (!j.running) box.classList.remove("indeterminate");
  $("job-kind").textContent = j.kind;
  $("job-cancel").disabled = !j.running;

  if (j.error) {
    $("job-label").textContent = j.error;
    $("job-eta").textContent = "";
    $("job-bar").style.width = "100%";
    return;
  }
  if (!j.running) {
    $("job-label").textContent = j.summary || (j.cancelled ? "cancelled" : "done");
    $("job-eta").textContent = `${Math.round(j.elapsed)}s`;
    $("job-bar").style.width = "100%";
    return;
  }

  $("job-label").textContent = j.label || "working…";
  // A job with no total is indeterminate: sync reports by line, and enrich has
  // not finished counting yet. Filling the bar there reads as *finished*, so
  // it gets a moving stripe instead of a width.
  const indeterminate = !j.total;
  $("job").classList.toggle("indeterminate", indeterminate);
  $("job-bar").style.width = indeterminate ? "100%" : `${j.percent}%`;
  const counter = j.total ? `${j.done}/${j.total}` : `${j.done}`;
  $("job-eta").textContent = j.eta_seconds !== null
    ? `${counter} · ${humanise(j.eta_seconds)} left`
    : counter;
}

function humanise(seconds) {
  if (seconds < 90) return `${Math.round(seconds)}s`;
  if (seconds < 5400) return `${Math.round(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

async function pollJob() {
  try {
    const j = await api("/api/job");
    renderJob(j);
    if (j.running) return;                     // keep polling
    clearInterval(jobTimer);
    jobTimer = null;
    setJobButtons(true);
    // The database has moved on, so the held library and the panes must too.
    const h = await api("/api/health");
    showHealth(h);
    state.sources = await api("/api/sources");
    await refreshSelection();
    if (j.summary) toast(`${j.kind}: ${j.summary}`);
    if (j.error) toast(`${j.kind} failed: ${j.error}`, true);
  } catch (err) {
    clearInterval(jobTimer);
    jobTimer = null;
    setJobButtons(true);
    toast(err.message, true);
  }
}

function watchJob() {
  setJobButtons(false);
  if (jobTimer) clearInterval(jobTimer);
  jobTimer = setInterval(pollJob, 1000);
  pollJob();
}

function setJobButtons(enabled) {
  $("do-sync").disabled = !enabled;
  $("do-enrich").disabled = !enabled;
}

async function startJob(path, body) {
  try {
    renderJob(await api(path, body ?? {}));
    watchJob();
  } catch (err) {
    // A refusal means one is already running — started from another tab, or
    // before this page was opened. Attach to it rather than going quiet and
    // re-enabling the buttons, which left a running job invisible.
    toast(err.message, true);
    const j = await api("/api/job").catch(() => null);
    if (j && j.running) { renderJob(j); watchJob(); } else { setJobButtons(true); }
  }
}

function showHealth(h) {
  $("health").textContent =
    `${h.tracks.toLocaleString()} tracks · ${h.playlists} playlists · ` +
    `${h.sequenceable.toLocaleString()} with BPM+key`;
}

// ---------------------------------------------------------------------------
// authorization
// ---------------------------------------------------------------------------

const AUTH_ERRORS = {
  access_denied: "You declined the Spotify authorization.",
  missing_code: "Spotify came back without an authorization code.",
  stale_or_unknown_state: "That sign-in attempt expired. Try again.",
  exchange_failed:
    "Spotify refused the authorization code. The usual cause is a redirect " +
    "URI that does not exactly match the one registered in the dashboard.",
};

async function refreshAuth() {
  const a = await api("/api/auth/status");
  const state = $("auth-state");
  const login = $("auth-login");
  const logout = $("auth-logout");

  if (!a.configured) {
    state.textContent = "not configured — see .env";
    state.classList.add("bad");
    login.hidden = logout.hidden = true;
    return a;
  }
  state.classList.toggle("bad", !a.authorized);
  if (a.authorized) {
    state.textContent = `signed in as ${a.user}`;
    login.hidden = true;
    logout.hidden = false;
  } else {
    state.textContent = a.detail ? "sign-in expired" : "not signed in";
    login.hidden = false;
    logout.hidden = true;
  }
  return a;
}

function reportAuthRedirect() {
  // The callback lands here with a query string. Say what happened, then
  // clean the URL so a refresh does not replay a stale message.
  const params = new URLSearchParams(location.search);
  const err = params.get("auth_error");
  if (err) toast(AUTH_ERRORS[err] || `Spotify sign-in failed: ${err}`, true);
  else if (params.get("authorized")) toast("Connected to Spotify");
  if (err || params.get("authorized")) {
    history.replaceState({}, "", location.pathname);
  }
}

async function boot() {
  try {
    reportAuthRedirect();
    await refreshAuth();
    showHealth(await api("/api/health"));
    state.sources = await api("/api/sources");
    await refreshSelection();
    // A job may already be running — started before this page was opened, or
    // by another tab. Pick it up rather than pretending nothing is happening.
    const j = await api("/api/job");
    renderJob(j);
    if (j.running) watchJob();
  } catch (err) {
    $("health").textContent = "could not reach the server";
    toast(err.message, true);
  }
}

$("source-filter").addEventListener("input", renderSources);
$("genre-toggle").addEventListener("click", () => {
  const now = !expanded();
  $("genre-toggle").setAttribute("aria-expanded", String(now));
  $("genre-body").hidden = !now;
  refreshSelection();
});
$("genre-all").addEventListener("click", async () => {
  const data = await api("/api/selection", { ...selection(), genres: null });
  data.genres.forEach((g) => state.genres.add(g.name));
  refreshSelection();
});
$("genre-none").addEventListener("click", () => { state.genres.clear(); refreshSelection(); });
$("generate").addEventListener("click", generate);
$("save").addEventListener("click", save);
$("update").addEventListener("click", update);
$("copy").addEventListener("click", async () => {
  await navigator.clipboard.writeText($("link").textContent);
  toast("Link copied");
});
$("open").addEventListener("click", () => window.open($("link").textContent, "_blank"));
for (const el of ["target-kind", "target-value", "tolerance"]) {
  $(el).addEventListener("change", refreshName);
}
document.querySelectorAll('input[name=mode]').forEach((r) =>
  r.addEventListener("change", refreshName));

boot();

$("do-sync").addEventListener("click", () => startJob("/api/job/sync"));
$("do-enrich").addEventListener("click", () =>
  startJob("/api/job/enrich", { retry_misses: true }));
$("job-cancel").addEventListener("click", async () => {
  $("job-cancel").disabled = true;
  await api("/api/job/cancel", {});
  toast("Stopping at the next checkpoint — progress is kept.");
});

$("auth-logout").addEventListener("click", async () => {
  await api("/api/auth/logout", {});
  await refreshAuth();
  toast("Disconnected. Your library stays on disk.");
});
