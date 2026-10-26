// conductor-desktop dashboard — vanilla JS, no framework.
// Polls /api/state and /api/tools/recent every 1.5s. Config loads on
// first visit to its tab and refreshes on click.

const POLL_MS = 1500;
const $ = (id) => document.getElementById(id);

let activeTab = "overview";
let pollTimer = null;

// ── Tab switching ──────────────────────────────────────────────────

document.querySelectorAll("nav .tab").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll("nav .tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    activeTab = b.dataset.tab;
    document.getElementById(`tab-${activeTab}`).classList.add("active");
    onTabEnter(activeTab);
  });
});

function onTabEnter(name) {
  if (name === "config") loadConfig();
}

// ── Live state polling ─────────────────────────────────────────────

async function pollState() {
  try {
    const [stateRes, recentRes] = await Promise.all([
      fetch("/dashboard/api/state").then((r) => r.json()),
      fetch("/dashboard/api/tools/recent").then((r) => r.json()),
    ]);
    renderState(stateRes);
    if (activeTab === "tools" || activeTab === "overview") {
      renderRecent(recentRes.calls || []);
    }
  } catch (e) {
    console.warn("poll error", e);
  }
}

function renderState(s) {
  // State pill — server-side current_state already reflects paused
  // and the errored>paused>acting>observing>idle precedence.
  const pill = $("state-pill");
  const label = s.state || "idle";
  pill.textContent = label;
  pill.className = "pill state-" + label;

  // Overview list
  const dl = $("state-list");
  const row = (k, v) => `<dt>${k}</dt><dd>${escapeHtml(String(v))}</dd>`;
  dl.innerHTML = [
    row("Role",         s.role),
    row("Backend",      s.backend ?? "—"),
    row("Grounder",     s.grounder_url ?? "—"),
    row("Transport",    `${s.transport ?? "—"} (${s.listening ?? "—"})`),
    row("Input paused", s.input_paused ? "yes" : "no"),
    row("PID",          s.pid),
    row("Uptime",       fmtUptime(s.uptime_s)),
  ].join("");
}

function renderRecent(calls) {
  const tbody = document.querySelector("#tools-table tbody");
  if (!tbody) return;
  if (calls.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" class="hint">No tool calls yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = calls.map((c) => {
    const klassTag = c.klass === "acting"
      ? `<span class="tag tag-acting">act</span>`
      : `<span class="tag tag-observing">obs</span>`;
    let statusTag, dur;
    if (c.in_flight) {
      statusTag = `<span class="tag tag-flight">in flight</span>`;
      dur = "—";
    } else if (c.success) {
      statusTag = `<span class="tag tag-ok">ok</span>`;
      dur = c.duration_ms != null ? `${c.duration_ms}ms` : "—";
    } else {
      statusTag = `<span class="tag tag-err">err</span>`;
      dur = c.duration_ms != null ? `${c.duration_ms}ms` : "—";
    }
    return `<tr>
      <td class="tool-name">${escapeHtml(c.tool)}</td>
      <td>${klassTag}</td>
      <td>${statusTag}</td>
      <td>${dur}</td>
      <td>${fmtAgo(c.started_at)}</td>
      <td class="input-summary" title="${escapeHtml(c.input_summary || "")}">
        ${escapeHtml(c.input_summary || "")}
      </td>
    </tr>`;
  }).join("");
}

// ── Config tab ─────────────────────────────────────────────────────

async function loadConfig() {
  const target = $("config-content");
  target.innerHTML = `<p class="hint">Loading…</p>`;
  try {
    const res = await fetch("/dashboard/api/config").then((r) => r.json());
    target.innerHTML = [
      renderConfigGroup("Agent output", res.output, "What tool replies carry."),
      renderConfigGroup("Connection", res.connection, "Backend, grounder and transport."),
      renderConfigGroup("Other", res.other, "Less commonly tuned."),
    ].join("");
  } catch (e) {
    target.innerHTML = `<p class="hint">Error: ${escapeHtml(e.message)}</p>`;
  }
}

function renderConfigGroup(title, dict, hint) {
  const keys = Object.keys(dict || {}).sort();
  if (keys.length === 0) return "";
  const rows = keys.map((k) => `<div class="config-row">
    <span class="ck">${escapeHtml(k)}</span>
    <span class="cv">${escapeHtml(formatVal(dict[k]))}</span>
  </div>`).join("");
  return `<section class="config-group">
    <h3>${escapeHtml(title)}</h3>
    <p class="hint">${escapeHtml(hint)}</p>
    ${rows}
  </section>`;
}

// ── Permission test button ────────────────────────────────────────

$("perm-test-btn").addEventListener("click", async () => {
  const out = $("perm-test-result");
  out.textContent = "Running…";
  try {
    const res = await fetch("/dashboard/api/perm-test", { method: "POST" })
      .then((r) => r.json());
    out.textContent = res.ok
      ? `OK · ${res.detail}`
      : `FAIL · ${res.detail}`;
    out.style.color = res.ok ? "var(--idle)" : "var(--errored)";
  } catch (e) {
    out.textContent = `Error: ${e.message}`;
    out.style.color = "var(--errored)";
  }
});

// ── Helpers ────────────────────────────────────────────────────────

function fmtAgo(ts) {
  if (ts == null) return "—";
  const s = Math.max(0, Math.round((Date.now() / 1000) - ts));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ago` : `${Math.floor(m / 60)}h ago`;
}

function fmtUptime(s) {
  if (s == null) return "—";
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

function formatVal(v) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "string") return v === "" ? "(empty)" : v;
  return JSON.stringify(v);
}

function escapeHtml(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

// ── Boot ───────────────────────────────────────────────────────────

pollState();
pollTimer = setInterval(pollState, POLL_MS);
