/* The control panel, in plain browser JavaScript. No build step, no framework, no CDN.
 *
 * Three rules run through all of it:
 *   - every URL is relative, because this page is reached by LAN address or Tailscale name and
 *     never by localhost;
 *   - every mutating request carries the CSRF token, and any 401 drops straight back to the
 *     sign-in view rather than leaving a dead panel on screen;
 *   - every age is measured against the PROXY's clock, which travels in each status payload.
 *     A phone with a wrong clock must not be able to invent a four-hour silence.
 */
"use strict";

const state = {
  csrf: null,
  tab: "dashboard",
  logProcess: null,
  logOffset: null,
  logText: "",
  logRotated: false,
  logBusy: false,
  timers: [],
};

const $ = (id) => document.getElementById(id);

/* -- talking to the server ---------------------------------------------- */

async function api(path, options = {}) {
  const opts = Object.assign({ headers: {} }, options);
  if (opts.method && opts.method !== "GET") {
    opts.headers["X-CSRF-Token"] = state.csrf || "";
    if (opts.body) opts.headers["Content-Type"] = "application/json";
  }
  const response = await fetch(path, opts);
  if (response.status === 401) {
    showGate();
    throw new Error("signed out");
  }
  const text = await response.text();
  const body = text ? JSON.parse(text) : {};
  if (!response.ok) throw new Error(body.detail || `${response.status}`);
  return body;
}

/* -- formatting ---------------------------------------------------------- */

function elapsed(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const total = Math.max(0, Math.round(seconds));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  if (minutes) return `${minutes}m ${String(secs).padStart(2, "0")}s`;
  return `${secs}s`;
}

// Ages are computed against the proxy's own `now`, never Date.now().
function ageFrom(proxy, key) {
  if (!proxy || proxy[key] === null || proxy[key] === undefined) return null;
  return proxy.now - proxy[key];
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;   // textContent: server strings are data
  return node;
}

/* -- the watch and the gauges -------------------------------------------- */

function renderWatch(health) {
  const proxy = health.proxy || {};
  const dot = $("watch-dot");
  const value = $("watch-value");
  const note = $("watch-note");

  if (health.proxy_error) {
    dot.className = "dot dot-down";
    // "no reading", not "—" and never "0s": the instrument is unpowered, not reporting silence.
    value.textContent = "no reading";
    note.textContent = health.proxy_error;
    return;
  }
  const age = ageFrom(proxy, "last_chunk_at");
  if (age === null) {
    dot.className = "dot dot-idle";
    value.textContent = "never";
    note.textContent = "no audio since the proxy started — is the play button pressed?";
    return;
  }
  value.textContent = elapsed(age) + " ago";
  if (age < 900) {
    dot.className = "dot dot-live";
    note.textContent = "audio is arriving";
  } else {
    dot.className = "dot dot-stale";
    note.textContent = "quiet — a silent channel and an unpressed play button look alike";
  }
}

function renderGauges(health) {
  const proxy = health.proxy || {};
  const pollAge = ageFrom(proxy, "ais_last_poll_at");
  const resolved = health.paths.filter((p) => p.resolves).length;
  const rows = [
    ["STT backend", proxy.stt_backend || "—"],
    ["AIS source", proxy.ais_source || "—"],
    ["Vessels cached", proxy.ais_cache_size === undefined ? "—" : String(proxy.ais_cache_size)],
    ["Last AIS poll", pollAge === null ? "—" : elapsed(pollAge) + " ago"],
    ["Conversations", proxy.conversations === undefined ? "—" : String(proxy.conversations)],
    ["Paths resolving", `${resolved}/${health.paths.length}`],
  ];
  const list = $("gauges");
  list.replaceChildren();
  for (const [label, reading] of rows) {
    const cell = element("div", "gauge");
    cell.append(element("dt", "legend", label), element("dd", null, reading));
    list.append(cell);
  }
}

function renderPaths(health) {
  const list = $("paths");
  list.replaceChildren();
  for (const check of health.paths) {
    const item = element("li", `path-item ${check.resolves ? "path-ok" : "path-bad"}`);
    item.append(
      element("span", "path-mark", check.resolves ? "✓" : "✗"),
      element("span", "path-key", check.key),
      element("span", "path-value", check.value || "(default)"));
    list.append(item);
  }
}

/* -- process cards -------------------------------------------------------- */

function readout(label, reading, warn) {
  const cell = element("div", warn ? "readout readout-warn" : "readout");
  cell.append(element("dt", "legend", label), element("dd", null, reading));
  return cell;
}

async function act(name, action, button) {
  const card = button.closest(".card");
  for (const control of card.querySelectorAll("button")) control.disabled = true;
  try {
    await api(`/api/processes/${encodeURIComponent(name)}/${action}`, { method: "POST" });
    showBanner(null);
  } catch (error) {
    showBanner(error.message);
  } finally {
    await refreshDashboard();
  }
}

function renderCard(process, logText) {
  const card = element("article", "card");
  const head = element("div", "card-head");
  head.append(
    element("span", `dot ${process.state === "running" ? "dot-live" : "dot-idle"}`),
    element("h2", "card-name", process.label),
    element("span", `card-state state-${process.state}`, process.state));
  card.append(head, element("p", "card-note", process.description));

  const readouts = element("dl", "readouts");
  readouts.append(
    readout("Uptime", process.state === "running" ? elapsed(process.uptime_sec) : "—"),
    readout("PID", process.pid === null ? "—" : String(process.pid)),
    readout("Port", process.port === null ? "n/a" : String(process.port)));
  if (process.port !== null && process.state === "running") {
    readouts.append(readout("Holding port", process.port_ok ? "yes" : "no", !process.port_ok));
  }
  card.append(readouts);

  const actions = element("div", "card-actions");
  const running = process.state === "running";
  const buttons = [
    ["Start", "start", process.state === "disabled" || running],
    ["Stop", "stop", !running],
    ["Restart", "restart", process.state === "disabled"],
  ];
  for (const [label, action, disabled] of buttons) {
    const button = element("button", "button", label);
    button.type = "button";
    button.disabled = disabled;
    button.addEventListener("click", () => act(process.name, action, button));
    actions.append(button);
  }
  card.append(actions);

  const log = element("pre", "card-log", logText || "No log yet. Start it and output appears here.");
  if (!logText) log.classList.add("log-empty");
  card.append(log);
  return card;
}

async function refreshDashboard() {
  const [health, processes] = await Promise.all([
    api("/api/health"),
    api("/api/processes"),
  ]);
  renderWatch(health);
  renderGauges(health);
  renderPaths(health);

  const tails = await Promise.all(processes.processes.map((process) =>
    api(`/api/logs/${encodeURIComponent(process.name)}?limit=8192`)
      .then((window) => window.text.split("\n").slice(-50).join("\n").trim())
      .catch(() => "")));

  const cards = $("cards");
  cards.replaceChildren();
  processes.processes.forEach((process, index) => cards.append(renderCard(process, tails[index])));

  const picker = $("log-process");
  if (picker.options.length !== processes.processes.length) {
    picker.replaceChildren();
    for (const process of processes.processes) {
      const option = element("option", null, process.label);
      option.value = process.name;
      picker.append(option);
    }
    state.logProcess = state.logProcess || processes.processes[0]?.name || null;
    picker.value = state.logProcess;
  }
}

/* -- the log tail --------------------------------------------------------- */

function atBottom(view) {
  return view.scrollHeight - view.scrollTop - view.clientHeight < 40;
}

/* The tail is kept as text and re-rendered, rather than appended to the DOM as it arrives.
 * Filtering is then pure rendering and costs no request -- which is also what fixes the bug
 * the first version had: typing a four-letter filter fired four resets, each clearing the view
 * and each appending its own answer, so lines appeared two and three times over. */
const LOG_BUFFER_MAX = 400_000;

function renderLog() {
  const view = $("log-view");
  const filter = $("log-filter").value.trim();
  const stick = $("log-follow").checked && atBottom(view);
  const text = filter
    ? state.logText.split("\n").filter((line) => line.includes(filter)).join("\n")
    : state.logText;

  view.replaceChildren();
  if (state.logRotated) {
    view.append(element("div", "log-rotated", "— log rotated, reading from the start —"));
  }
  if (text) {
    view.append(document.createTextNode(text));
  } else {
    view.append(element("span", "log-empty",
      filter ? "No lines match that filter." : "Nothing logged yet."));
  }
  if (stick) view.scrollTop = view.scrollHeight;
}

async function refreshLog() {
  if (!state.logProcess || state.logBusy) return;   // one request in flight at a time
  state.logBusy = true;
  try {
    const query = state.logOffset === null ? "" : `?offset=${state.logOffset}`;
    const tail = await api(`/api/logs/${encodeURIComponent(state.logProcess)}${query}`);
    if (tail.restarted) {
      state.logText = "";
      state.logRotated = true;
    }
    if (tail.text) state.logText += tail.text;
    if (state.logText.length > LOG_BUFFER_MAX) {
      state.logText = state.logText.slice(-LOG_BUFFER_MAX / 2);
    }
    state.logOffset = tail.next_offset;
    renderLog();
  } finally {
    state.logBusy = false;
  }
}

function selectLog(name) {
  state.logProcess = name;
  state.logOffset = null;
  state.logText = "";
  state.logRotated = false;
  refreshLog().catch(() => {});
}

/* -- views and polling ---------------------------------------------------- */

function showBanner(message) {
  const banner = $("banner");
  banner.textContent = message || "";
  banner.hidden = !message;
}

function showTab(name) {
  state.tab = name;
  $("dashboard").hidden = name !== "dashboard";
  $("logs").hidden = name !== "logs";
  for (const tab of document.querySelectorAll(".tab")) {
    tab.setAttribute("aria-selected", String(tab.dataset.tab === name));
  }
  tick();
}

function tick() {
  if (document.hidden) return;
  if (state.tab === "dashboard") refreshDashboard().catch(() => {});
  else refreshLog().catch(() => {});
}

function startPolling() {
  stopPolling();
  // The dashboard is cheap and answers "is it still running?"; the log is the faster of the
  // two because it is being read while something is happening.
  state.timers.push(setInterval(() => { if (state.tab === "dashboard") tick(); }, 3000));
  state.timers.push(setInterval(() => { if (state.tab === "logs") tick(); }, 2000));
}

function stopPolling() {
  state.timers.forEach(clearInterval);
  state.timers = [];
}

function showGate(passwordSet = true) {
  stopPolling();
  state.csrf = null;
  $("panel").hidden = true;
  $("login").hidden = false;
  $("login-form").hidden = !passwordSet;
  $("no-password").hidden = passwordSet;
  if (passwordSet) $("password").focus();
}

function showPanel() {
  $("login").hidden = true;
  $("panel").hidden = false;
  showTab("dashboard");
  startPolling();
}

/* -- wiring --------------------------------------------------------------- */

$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = $("login-error");
  error.hidden = true;
  $("login-button").disabled = true;
  try {
    const body = await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ password: $("password").value }),
    });
    state.csrf = body.csrf_token;
    $("password").value = "";
    showPanel();
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
  } finally {
    $("login-button").disabled = false;
  }
});

$("signout").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch (ignored) { /* already gone */ }
  showGate();
});

for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => showTab(tab.dataset.tab));
}

$("log-process").addEventListener("change", (event) => selectLog(event.target.value));
// Filtering is a render, not a request: no debounce needed and no race to lose.
$("log-filter").addEventListener("input", renderLog);
document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });

(async function boot() {
  try {
    const probe = await api("/api/session");
    if (probe.authenticated) {
      state.csrf = probe.csrf_token;
      showPanel();
    } else {
      showGate(probe.password_set);
    }
  } catch (error) {
    showGate();
  }
})();
