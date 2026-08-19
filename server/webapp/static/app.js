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
  processes: [],        // the last answer from /api/processes, for the popup's header
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
  const resolved = health.paths.filter((p) => p.resolves).length;
  // "Last AIS poll" used to sit here as a bare age nobody could interpret. The AISHub lamp
  // says the same thing and says what it means, so the gauge would only repeat it.
  const rows = [
    ["STT backend", proxy.stt_backend || "—"],
    ["AIS source", proxy.ais_source || "—"],
    ["Vessels cached", proxy.ais_cache_size === undefined ? "—" : String(proxy.ais_cache_size)],
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

/* -- the annunciator ------------------------------------------------------ */

/* Lamps are built once and updated in place, for the same reason the cards are: a rebuild on
 * every three-second poll throws away whatever the reader was doing. */
const feedViews = new Map();

const LAMP_TEST_SEQUENCE = ["green", "amber", "red"];
// Slow enough to be unmistakable. At 420ms the whole sequence was over in 1.26s and a lamp
// that was already green appeared not to react at all for the first step.
const LAMP_TEST_STEP_MS = 600;
let lampTestUntil = 0;

function buildFeed() {
  const root = element("article", "feed");
  const lamp = element("span", "lamp");
  const body = element("div", "feed-body");
  const name = element("span", "legend feed-name");
  const reading = element("span", "feed-reading");
  const note = element("span", "feed-note");
  body.append(name, reading, note);
  root.append(lamp, body);
  return { root, name, reading, note };
}

/* A lamp is unlit when its process is not running: a feed the operator stopped on purpose has
 * not failed, and only the browser knows which processes are up. */
function lampFor(feed, running) {
  return running === false ? "unlit" : feed.lamp;
}

function processLabel(processes, name) {
  return processes.find((p) => p.name === name)?.label || name;
}

function renderFeeds(health, processes) {
  const running = new Map(processes.map((p) => [p.name, p.state === "running"]));
  const tiles = $("feed-tiles");
  const seen = new Set();

  for (const feed of health.feeds || []) {
    let view = feedViews.get(feed.key);
    if (!view) {
      view = buildFeed();
      feedViews.set(feed.key, view);
      tiles.append(view.root);
    }
    seen.add(feed.key);

    const stopped = running.get(feed.owner) === false;
    const lamp = lampFor(feed, running.get(feed.owner));
    view.root.dataset.truth = lamp;           // what the lamp test restores
    // A poll landing mid-test must not snap the lamps back; it updates the truth underneath
    // and the test puts it on screen when it finishes.
    if (Date.now() >= lampTestUntil) view.root.dataset.lamp = lamp;
    setText(view.name, feed.label);
    setText(view.reading, feed.since_sec === null || feed.since_sec === undefined
      ? "—"
      : `${feed.verb} ${elapsed(feed.since_sec)} ago`);
    // The note wins over the vessel count: it only appears when something is off, and then it
    // is the more useful of the two. A dark lamp always says why it is dark -- an unexplained
    // one is the thing the lamp test exists to rule out.
    setText(view.note, stopped
      ? `${processLabel(processes, feed.owner)} is not running`
      : (feed.note || feed.detail || ""));
  }
  for (const [key, view] of feedViews) {
    if (!seen.has(key)) {
      view.root.remove();
      feedViews.delete(key);
    }
  }
}

/* Prove the bulbs work. A dark annunciator is only reassuring if a dark lamp can be told
 * apart from a dead one -- the same reason a real bridge panel carries this button. */
function lampTest() {
  const tiles = [...feedViews.values()];
  const button = $("lamp-test");
  if (!tiles.length || button.disabled) return;   // disabled also blocks overlapping runs

  // Feedback on the control itself. Without it the only evidence a click registered is the
  // lamps, and a lamp already showing the first test colour appears not to react.
  button.disabled = true;
  setText(button, "Testing…");

  lampTestUntil = Date.now() + LAMP_TEST_SEQUENCE.length * LAMP_TEST_STEP_MS + 200;
  LAMP_TEST_SEQUENCE.forEach((colour, index) => {
    setTimeout(() => {
      for (const view of tiles) view.root.dataset.lamp = colour;
      setText(button, `Testing… ${colour}`);
    }, index * LAMP_TEST_STEP_MS);
  });
  setTimeout(() => {
    for (const view of tiles) view.root.dataset.lamp = view.root.dataset.truth || "unlit";
    lampTestUntil = 0;
    button.disabled = false;
    setText(button, "Lamp test");
  }, LAMP_TEST_SEQUENCE.length * LAMP_TEST_STEP_MS);
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

async function act(name, action, button) {
  const card = button.closest(".card");
  // Only the buttons that ACT on the process. Locking the whole card also caught the Log
  // button, which updateCard does not re-enable -- so one Start or Restart left it greyed out
  // for good. Reading the log during a restart is also precisely when you want it.
  for (const control of card.querySelectorAll(".button-action")) control.disabled = true;
  try {
    await api(`/api/processes/${encodeURIComponent(name)}/${action}`, { method: "POST" });
    showBanner(null);
  } catch (error) {
    showBanner(error.message);
  } finally {
    await refreshDashboard();
  }
}

/* Cards are built once and then updated in place.
 *
 * The first version rebuilt both cards on every poll, which threw away the scroll position
 * inside each log pane three seconds later: a reader scrolling down through the proxy's output
 * was thrown back to the top, over and over. Rebuilding also discarded text selections and
 * button focus. Nothing here replaces a node that is already on the page. */
const cardViews = new Map();

function setText(node, value) {
  if (node.textContent !== value) node.textContent = value;
}

function buildCard(process) {
  const root = element("article", "card");
  const head = element("div", "card-head");
  const dot = element("span", "dot dot-idle");
  const stateLabel = element("span", "card-state");
  head.append(dot, element("h2", "card-name", process.label), stateLabel);
  root.append(head, element("p", "card-note", process.description));

  // Every readout exists from the start, including "Holding port", so that a process starting
  // or stopping changes text rather than adding and removing cells under the reader.
  const readouts = element("dl", "readouts");
  const cells = {};
  for (const [key, label] of [["uptime", "Uptime"], ["pid", "PID"],
                              ["port", "Port"], ["portOk", "Holding port"]]) {
    const cell = element("div", "readout");
    const value = element("dd", null, "—");
    cell.append(element("dt", "legend", label), value);
    cells[key] = { cell, value };
    readouts.append(cell);
  }

  const actions = element("div", "card-actions");
  const buttons = {};
  for (const [label, action] of [["Start", "start"], ["Stop", "stop"], ["Restart", "restart"]]) {
    const button = element("button", "button button-action", label);
    button.type = "button";
    button.addEventListener("click", () => act(process.name, action, button));
    buttons[action] = button;
    actions.append(button);
  }

  // The output used to sit glued under every card, which made the dashboard mostly log and
  // pushed everything else off a phone screen. It opens over the page instead.
  const logButton = element("button", "button button-quiet button-log", "Log");
  logButton.type = "button";
  logButton.addEventListener("click", () => openLog(process.name, process.label));
  actions.append(logButton);

  const foot = element("div", "card-foot");
  foot.append(readouts, actions);
  root.append(foot);
  return { root, dot, stateLabel, cells, buttons };
}

function updateCard(view, process) {
  const running = process.state === "running";
  view.dot.className = `dot ${running ? "dot-live" : "dot-idle"}`;
  view.stateLabel.className = `card-state state-${process.state}`;
  setText(view.stateLabel, process.state);

  setText(view.cells.uptime.value, running ? elapsed(process.uptime_sec) : "—");
  setText(view.cells.pid.value, process.pid === null ? "—" : String(process.pid));
  setText(view.cells.port.value, process.port === null ? "n/a" : String(process.port));
  const holding = process.port === null ? "n/a"
    : running ? (process.port_ok ? "yes" : "no") : "—";
  setText(view.cells.portOk.value, holding);
  view.cells.portOk.cell.className =
    running && process.port !== null && !process.port_ok ? "readout readout-warn" : "readout";

  view.buttons.start.disabled = running || process.state === "disabled";
  view.buttons.stop.disabled = !running;
  view.buttons.restart.disabled = process.state === "disabled";
}

async function refreshDashboard() {
  const [health, processes] = await Promise.all([
    api("/api/health"),
    api("/api/processes"),
  ]);
  state.processes = processes.processes;
  renderWatch(health);
  renderGauges(health);
  renderFeeds(health, processes.processes);
  renderPaths(health);

  const cards = $("cards");
  const seen = new Set();
  for (const process of processes.processes) {
    let view = cardViews.get(process.name);
    if (!view) {
      view = buildCard(process);
      cardViews.set(process.name, view);
      cards.append(view.root);
    }
    updateCard(view, process);
    seen.add(process.name);
  }
  for (const [name, view] of cardViews) {
    if (!seen.has(name)) {
      view.root.remove();
      cardViews.delete(name);
    }
  }

  const picker = $("log-process");
  if (picker.options.length !== processes.processes.length) {
    picker.replaceChildren();
    for (const process of processes.processes) {
      const option = element("option", null, process.label);
      option.value = process.name;
      picker.append(option);
    }
    picker.value = tabLog.name || processes.processes[0]?.name || "";
    if (!tabLog.name && picker.value) tabLog.select(picker.value);
  }
  updateDialogMeta();
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

/* One reader, two places to show it: the Logs tab and the popup a card's Log button opens.
 * They keep separate offsets on purpose -- sharing one would make each poll steal the other's
 * new bytes, and whichever pane asked second would show nothing. */
function logStream(view, filterInput, followInput) {
  // `generation` counts selections. An answer that arrives for an earlier one is discarded,
  // which is what lets a new selection start a request immediately instead of waiting for the
  // one in flight -- see select().
  const own = { name: null, offset: null, text: "", rotated: false, busy: false,
                generation: 0, asked: false };

  function paint() {
    const filter = filterInput.value.trim();
    const stick = followInput.checked && atBottom(view);
    const previous = view.scrollTop;
    const text = filter
      ? own.text.split("\n").filter((line) => line.includes(filter)).join("\n")
      : own.text;

    view.replaceChildren();
    if (own.rotated) {
      view.append(element("div", "log-rotated", "— log rotated, reading from the start —"));
    }
    if (text) {
      view.append(document.createTextNode(text));
    } else {
      // "Nothing logged yet" is a claim about the file, and it must not be made before the
      // file has been read -- otherwise every process switch libels the new process.
      view.append(element("span", "log-empty",
        filter ? "No lines match that filter."
          : own.asked ? "Nothing logged yet." : "Reading…"));
    }
    // Restoring `previous` is the half that was missing: sticking to the bottom was handled,
    // but a reader who had scrolled anywhere else was returned to the top on the next poll.
    view.scrollTop = stick ? view.scrollHeight : previous;
  }

  return {
    get name() { return own.name; },

    select(name) {
      own.name = name;
      own.offset = null;
      own.text = "";
      own.rotated = false;
      own.asked = false;
      own.generation += 1;
      // Abandon rather than wait for whatever is in flight. Leaving `busy` set here made the
      // immediate fetch below a no-op whenever a poll happened to be running, so switching
      // process left the pane blank until the next timer tick two seconds later.
      own.busy = false;
      paint();
      this.pull().catch(() => {});
    },

    paint,

    async pull() {
      if (!own.name || own.busy) return;            // one request in flight per selection
      own.busy = true;
      const generation = own.generation;
      const asked = own.name;
      try {
        const query = own.offset === null ? "" : `?offset=${own.offset}`;
        const tail = await api(`/api/logs/${encodeURIComponent(asked)}${query}`);
        // The reader may have switched process while this was in flight; that answer belongs
        // to a selection that no longer exists.
        if (own.generation !== generation) return;
        own.asked = true;
        if (tail.restarted) {
          own.text = "";
          own.rotated = true;
        }
        if (tail.text) own.text += tail.text;
        if (own.text.length > LOG_BUFFER_MAX) {
          own.text = own.text.slice(-LOG_BUFFER_MAX / 2);
        }
        own.offset = tail.next_offset;
        // Repainted even when nothing arrived if this was the first answer for a selection --
        // otherwise an empty log keeps showing "Reading…" for ever. Otherwise only when
        // something changed, because an unconditional re-render every two seconds would wipe
        // out a text selection the reader was making.
        if (tail.text || tail.restarted || !own.text) paint();
      } finally {
        // Only if this call still owns the stream: a superseded request must not clear the
        // flag out from under the selection that replaced it.
        if (own.generation === generation) own.busy = false;
      }
    },
  };
}

const tabLog = logStream($("log-view"), $("log-filter"), $("log-follow"));
const popupLog = logStream($("dialog-log"), $("dialog-filter"), $("dialog-follow"));

/* -- the log popup -------------------------------------------------------- */

const dialog = $("log-dialog");
let popupTimer = null;

function openLog(name, label) {
  setText($("dialog-title"), label);
  updateDialogMeta();
  popupLog.select(name);
  if (!dialog.open) dialog.showModal();
  clearInterval(popupTimer);
  popupTimer = setInterval(() => {
    if (!document.hidden) popupLog.pull().catch(() => {});
  }, 2000);
}

/* The header repeats the card's own readings so the popup can be read on its own, and so a
 * process that dies while its log is open says so where the reader is looking. */
function updateDialogMeta() {
  if (!dialog.open && !popupLog.name) return;
  const process = (state.processes || []).find((p) => p.name === popupLog.name);
  const lamp = $("dialog-lamp");
  if (!process) {
    lamp.className = "dot dot-idle";
    setText($("dialog-meta"), "");
    return;
  }
  const running = process.state === "running";
  lamp.className = `dot ${running ? "dot-live" : "dot-idle"}`;
  setText($("dialog-meta"), running
    ? `${process.state} · ${elapsed(process.uptime_sec)} · pid ${process.pid}`
    : process.state);
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
  else tabLog.pull().catch(() => {});
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
  clearInterval(popupTimer);
  popupTimer = null;
}

function showGate(passwordSet = true) {
  stopPolling();
  // A signed-out session must not leave someone's log on screen behind the sign-in card.
  if (dialog.open) dialog.close();
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

$("log-process").addEventListener("change", (event) => tabLog.select(event.target.value));
// Filtering is a render, not a request: no debounce needed and no race to lose.
$("log-filter").addEventListener("input", () => tabLog.paint());
$("dialog-filter").addEventListener("input", () => popupLog.paint());

$("lamp-test").addEventListener("click", lampTest);

$("dialog-close").addEventListener("click", () => dialog.close());
// Escape closes a <dialog> without going through the button, so the polling is stopped on the
// event rather than in the handler -- otherwise the popup keeps requesting after it is gone.
dialog.addEventListener("close", () => {
  clearInterval(popupTimer);
  popupTimer = null;
});

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
