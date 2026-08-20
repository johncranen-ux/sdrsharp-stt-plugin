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

// Mirrors stt_proxy/markup.py's _vessel_link, which the proxy's own /conversations page has
// always had and this panel was built without. The details path rather than the search path:
// this is read where someone is choosing between candidates, and a search results page makes
// them choose twice.
const VESSELFINDER_URL = "https://www.vesselfinder.com/vessels/details/";

/** The MMSI (or `text`, when given) linked to its VesselFinder page. Null when nothing to link.
 *
 * The old proxy page links the vessel NAME, so the list rows here pass the row label as text
 * to match it; everywhere the MMSI itself is on screen, the MMSI is the link text. Either way
 * the href is keyed on the MMSI -- the name is exactly the field that cannot identify a ship. */
function vesselFinderLink(mmsi, className, text) {
  const id = String(mmsi === null || mmsi === undefined ? "" : mmsi).trim();
  // Digits only. An MMSI is nine digits by definition, and a blank or an em dash placeholder
  // would build a details URL for nothing -- a link that goes nowhere useful is worse than the
  // plain text it replaced. Kept as "all digits" rather than exactly nine so a cache entry with
  // an odd-length identifier still links rather than silently losing the affordance.
  if (!/^\d+$/.test(id)) return null;
  const link = element("a", className ? `${className} vf-link` : "vf-link",
                       text === undefined || text === null || text === "" ? id : String(text));
  link.href = VESSELFINDER_URL + encodeURIComponent(id);
  link.target = "_blank";
  // noreferrer as well as noopener: this is the only place the panel reaches the public web,
  // and there is no reason to tell vesselfinder.com which local page sent the reader.
  link.rel = "noopener noreferrer";
  link.title = `Open MMSI ${id} on VesselFinder`;
  return link;
}

/** Fill a table cell with `text`, linked when the MMSI supports it.
 *
 * Both tables reuse their row objects across renders, so a cell has to be able to go back to
 * plain text when the next row in that slot has no MMSI -- setting textContent alone would
 * leave the previous row's link in place. And every row here is itself a click target that
 * opens a detail panel, so the link stops the click propagating: following it should not also
 * open, select and scroll to a detail nobody asked for.
 */
function setLinkedVesselCell(cell, mmsi, text) {
  const link = vesselFinderLink(mmsi, null, text);
  if (!link) {
    setText(cell, text);
    return;
  }
  link.addEventListener("click", (event) => event.stopPropagation());
  cell.replaceChildren(link);
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

/* -- conversations --------------------------------------------------------- */
/*
 * The list is polled and filtered/paged, so it follows the same shape as `logStream`: a
 * generation counter tags every request, and an answer for a filter or page that is no longer
 * current is discarded rather than painted -- otherwise a slow response to an old filter can
 * land after a fast response to a new one and show the wrong page.
 *
 * Rows are a Map keyed by id, built once and updated in place -- exactly `cardViews` and
 * `feedViews` -- so a poll never destroys the row the reader has open or a text selection
 * mid-copy. Re-appending an existing row moves it to the right position without rebuilding it.
 */
const convRows = new Map();
const convState = {
  generation: 0,
  offset: 0,
  limit: 50,
  total: 0,
  rows: new Map(),       // id -> last summary row seen, for the detail header
  selectedId: null,
  detailGeneration: 0,
};
let convFilterTimer = null;

function convParams() {
  const params = new URLSearchParams();
  const identified = $("conv-identified").value;
  if (identified) params.set("identified", identified);
  const channel = $("conv-channel").value.trim();
  if (channel) params.set("channel", channel);
  const text = $("conv-text").value.trim();
  if (text) params.set("text", text);
  params.set("limit", String(convState.limit));
  params.set("offset", String(convState.offset));
  return params;
}

function buildConvRow() {
  const tr = element("tr", "conv-row");
  tr.tabIndex = 0;
  const cells = {};
  for (const key of ["start", "channel", "vessel", "type", "destination",
                      "confidence", "turns", "candidates"]) {
    cells[key] = element("td", "conv-cell");
    tr.append(cells[key]);
  }
  cells.start.classList.add("conv-mono");
  cells.channel.classList.add("conv-mono");
  cells.vessel.classList.add("conv-vessel");
  cells.confidence.classList.add("conv-mono");
  cells.turns.classList.add("conv-mono", "conv-num");
  cells.candidates.classList.add("conv-mono", "conv-num");
  return { root: tr, cells };
}

function updateConvRow(view, row) {
  view.root.classList.toggle("conv-row-selected", row.id === convState.selectedId);
  view.root.classList.toggle("conv-row-unidentified", !row.identified);
  setText(view.cells.start, row.start || "—");
  setText(view.cells.channel, row.channel || "—");
  // Matches the proxy's own /conversations page, which has linked the vessel in every listing
  // row since the AISHub work. This panel had it only in the detail panel, which is where the
  // reader least needs it -- the list is what gets scanned.
  setLinkedVesselCell(view.cells.vessel, row.mmsi, row.label || "unidentified");
  setText(view.cells.type, row.type || "—");
  setText(view.cells.destination, row.destination || "—");
  setText(view.cells.confidence, row.confidence || "—");
  setText(view.cells.turns, String(row.turn_count));
  setText(view.cells.candidates, String(row.candidate_count));
}

function showConvSnapshot(snapshot) {
  const banner = $("conv-stale");
  if (snapshot && snapshot.stale && snapshot.has_data === false) {
    // No fetch has ever succeeded -- there is no "last copy" to report an age for.
    banner.textContent = `nothing has been fetched yet — ${snapshot.error || "unknown error"}`;
    banner.hidden = false;
  } else if (snapshot && snapshot.stale) {
    banner.textContent =
      `showing the last copy, ${elapsed(snapshot.age_sec)} old — ${snapshot.error || "unknown error"}`;
    banner.hidden = false;
  } else {
    banner.hidden = true;
  }
}

// A failed request to OUR OWN api() (network down, webapp itself unreachable) is a different
// claim from the server successfully answering "here are zero rows" -- see requirement 3. The
// stale-snapshot case above is handled server-side and still returns 200 with rows, so this
// path only fires when the panel itself could not be asked at all.
function showConvFetchError(message) {
  const banner = $("conv-fetch-error");
  banner.textContent = message ? `could not reach the panel: ${message}` : "";
  banner.hidden = !message;
}

function renderConvRows(rows) {
  const tbody = $("conv-rows");
  const seen = new Set();
  convState.rows.clear();
  rows.forEach((row, index) => {
    convState.rows.set(row.id, row);
    let view = convRows.get(row.id);
    if (!view) {
      view = buildConvRow();
      view.root.addEventListener("click", () => selectConversation(row.id));
      view.root.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectConversation(row.id);
        }
      });
      convRows.set(row.id, view);
    }
    updateConvRow(view, row);
    // Reorders an existing row into the current page's order, but only when it is not
    // already there -- appending (or inserting) a node already at that DOM position still
    // counts as a move and drops the reader's text selection, which is exactly the poll-time
    // disruption this guard exists to avoid. tbody.children[index] is read fresh each
    // iteration, so an earlier insertBefore in this same pass is reflected immediately.
    if (tbody.children[index] !== view.root) {
      tbody.insertBefore(view.root, tbody.children[index] || null);
    }
    seen.add(row.id);
  });
  for (const [id, view] of convRows) {
    if (!seen.has(id)) {
      view.root.remove();
      convRows.delete(id);
    }
  }
  // Only a genuine "the server answered and there were none" reaches here -- a failed fetch
  // returns before this function is called, so the previous rows (or nothing, on first load)
  // stay exactly as they were rather than being relabelled as "empty".
  $("conv-empty").hidden = rows.length !== 0;
}

function renderConvPager() {
  const note = $("conv-page-note");
  if (convState.total === 0) {
    setText(note, "");
  } else {
    const from = convState.offset + 1;
    const to = Math.min(convState.offset + convState.limit, convState.total);
    setText(note, `${from}–${to} of ${convState.total}`);
  }
  $("conv-prev").disabled = convState.offset <= 0;
  $("conv-next").disabled = convState.offset + convState.limit >= convState.total;
}

async function refreshConversations() {
  const generation = ++convState.generation;
  let body;
  try {
    body = await api(`/api/conversations?${convParams().toString()}`);
  } catch (error) {
    if (generation !== convState.generation) return;   // a newer request has already landed
    showConvFetchError(error.message);
    return;
  }
  if (generation !== convState.generation) return;
  showConvFetchError(null);
  showConvSnapshot(body.snapshot);
  convState.total = body.total;
  convState.offset = body.offset;
  renderConvRows(body.rows);
  renderConvPager();
}

function convFiltersChanged(immediate) {
  convState.offset = 0;
  if (convFilterTimer) clearTimeout(convFilterTimer);
  if (immediate) {
    refreshConversations().catch(() => {});
  } else {
    // Free-text and channel filters fire a network request per keystroke; debounced so typing
    // "condor" does not send six of them.
    convFilterTimer = setTimeout(() => refreshConversations().catch(() => {}), 300);
  }
}

/* -- conversation detail --------------------------------------------------- */

function convLabel(row) {
  if (!row) return "Conversation";
  return row.label || (row.identified ? "identified" : "unidentified");
}

function chainStep(label, value, changed) {
  const span = element("span", `turn-step${changed ? " turn-step-changed" : ""}`);
  span.append(element("span", "legend turn-step-label", label));
  span.append(document.createTextNode(value === null || value === undefined ? "—" : value));
  return span;
}

function passLabel(candidate) {
  if (candidate.via_callsign) return "callsign";
  if (candidate.via_live_match) return "live pass";
  if (candidate.via_partial_callsign) return "partial callsign";
  return "name hint";
}

function renderTurn(turn) {
  const item = element("li", "turn");
  item.append(element("span", "turn-time", turn.time || "—"));

  const chain = element("div", "turn-chain");
  chain.append(chainStep("raw", turn.raw, false));
  chain.append(element("span", "turn-arrow", "→"));
  chain.append(chainStep("text", turn.text, turn.changed_by_regex));
  chain.append(element("span", "turn-arrow", "→"));
  // conv: null means the correction pass changed nothing (or never ran) -- the store cannot
  // tell those apart, so this says only what is known, and never invents a third layer of
  // text that was never produced.
  if (turn.conv === null || turn.conv === undefined) {
    chain.append(element("span", "turn-step turn-step-unchanged", "conv: unchanged"));
  } else {
    chain.append(chainStep("conv", turn.conv, turn.changed_by_llm));
  }
  item.append(chain);

  // Words carry the claim; the colour in app.css only underlines it.
  if (turn.live_match === "ais-confirmed") {
    item.append(element("span", "turn-match turn-match-ais-confirmed",
      `AIS-confirmed: ${turn.live_vessel}`));
  } else if (turn.live_match === "heard-only") {
    item.append(element("span", "turn-match turn-match-heard-only",
      `Heard only: “${turn.live_vessel}” — no such ship in the AIS cache`));
  }
  return item;
}

function renderResolverCandidates(list) {
  if (!list || !list.length) {
    return element("p", "conv-note", "No AIS vessels were offered to the resolver.");
  }
  const ul = element("ul", "cand-list");
  for (const c of list) {
    const li = element("li", "cand-item");
    li.append(element("span", "cand-name", c.name || "?"));
    li.append(vesselFinderLink(c.mmsi, "cand-mmsi")
              || element("span", "cand-mmsi", c.mmsi || "—"));
    const bits = [];
    if (c.latitude !== null && c.latitude !== undefined
        && c.longitude !== null && c.longitude !== undefined) {
      bits.push(`${Number(c.latitude).toFixed(3)}, ${Number(c.longitude).toFixed(3)}`);
    }
    if (c.draught !== null && c.draught !== undefined) bits.push(`draught ${c.draught} m`);
    if (c.destination) bits.push(`dest ${c.destination}`);
    if (c.last_seen) bits.push(`seen ${c.last_seen}`);
    li.append(element("span", "cand-meta", bits.join(" · ")));
    li.append(element("span", "cand-pass", passLabel(c)));
    ul.append(li);
  }
  return ul;
}

// The heading text is not decoration -- without it a below-cutoff guess reads as an
// identification, which is the exact misreading this framing exists to prevent.
function renderSuggestions(list) {
  if (!list || !list.length) return null;
  const wrap = element("div", "suggest-block");
  wrap.append(element("p", "legend suggest-heading", "Scored below the identification cutoff"));
  const ol = element("ol", "suggest-list");
  for (const s of list) {
    const li = element("li", "suggest-item");
    li.append(element("span", "cand-name", s.name || "?"));
    li.append(vesselFinderLink(s.mmsi, "cand-mmsi")
              || element("span", "cand-mmsi", s.mmsi || "—"));
    li.append(element("span", "suggest-score",
      s.score === null || s.score === undefined ? "—" : String(Math.round(s.score))));
    if (s.heard) li.append(element("span", "suggest-heard", `heard “${s.heard}”`));
    ol.append(li);
  }
  wrap.append(ol);
  return wrap;
}

function renderConvDetail(detail) {
  // detail.label is computed server-side by conversations_view.detail() the same way
  // summarise() computes a list row's label -- shared-name-safe by construction, never the
  // bare `vessel` field. This used to fall back to the LIST row (convState.rows.get(detail.id))
  // and only then to detail.vessel; that second fallback was reachable whenever the
  // Conversations tab's loaded page didn't include this conversation -- which the Vessels
  // screen's "open this conversation" link can do on the very first click, not just as a
  // stale-cache edge case -- and it showed the bare, ambiguous name, permanently, for that
  // render. Reading detail.label directly removes that path entirely; the fallback here only
  // covers a record that genuinely has neither a vessel name nor an MMSI.
  setText($("conv-detail-title"),
    detail.label || (detail.identified ? "identified" : "unidentified"));

  const body = $("conv-detail-body");
  body.replaceChildren();   // fetched once per selection, not on a poll -- see the API note

  const meta = element("p", "conv-detail-meta");
  const metaBits = [detail.channel, detail.start, detail.end,
                     detail.confidence ? `confidence ${detail.confidence}` : null]
    .filter((bit) => bit);
  setText(meta, metaBits.join(" · ") || "—");
  // The identified vessel's VesselFinder page. In the meta line rather than the title, because
  // the title is a shared-name-safe LABEL computed server-side and turning it into a link
  // would invite reading the label itself as confirmation of the ship.
  const identityLink = vesselFinderLink(detail.mmsi);
  if (identityLink) {
    // The separator is unconditional: with no meta bits the line still reads "—", so the link
    // would otherwise be jammed against the dash.
    meta.append(document.createTextNode(" · "));
    meta.append(identityLink);
  }
  body.append(meta);

  body.append(element("h3", null, "Turns"));
  const turns = detail.turns || [];
  if (turns.length) {
    const list = element("ol", "turn-list");
    for (const turn of turns) list.append(renderTurn(turn));
    body.append(list);
  } else {
    body.append(element("p", "conv-note", "No turns recorded."));
  }

  body.append(element("h3", null, "Resolver candidates"));
  body.append(renderResolverCandidates(detail.resolver_candidates));

  const suggestions = renderSuggestions(detail.suggestions);
  if (suggestions) body.append(suggestions);
}

function showConvDetail(open) {
  $("conv-detail").hidden = !open;
}

// With the list at its default 50 rows, the detail panel opens roughly two and a half screens
// below the fold -- a reader who clicks a row and sees only the row highlight has no reason to
// think anything happened. This is the only place that scrolls: the poll path (refreshConversations
// / renderConvRows) never calls it, so a poll landing while the detail is open cannot move the
// reader's viewport -- only an actual click (here, or the Close handler below) does.
function scrollConvIntoView(el) {
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  el.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
}

async function selectConversation(id) {
  convState.selectedId = id;
  for (const [rowId, view] of convRows) {
    view.root.classList.toggle("conv-row-selected", rowId === id);
  }
  showConvDetail(true);
  const detailEl = $("conv-detail");
  // Scrolled immediately, before the detail fetch resolves: the click itself needs a visible
  // response, and "Loading..." arriving in view is that response -- the reader should not have
  // to wait on the network to learn the click registered.
  scrollConvIntoView(detailEl);
  setText($("conv-detail-title"), convLabel(convState.rows.get(id)));
  const body = $("conv-detail-body");
  body.replaceChildren(element("p", "conv-note", "Loading…"));

  const generation = ++convState.detailGeneration;
  try {
    const detail = await api(`/api/conversations/${encodeURIComponent(id)}`);
    if (generation !== convState.detailGeneration) return;   // a later selection replaced this
    renderConvDetail(detail);
    // The panel is much taller full of turns and candidates than it was showing "Loading...",
    // so the scroll aimed at the placeholder undershoots once the real content lands -- observed
    // settling with only the title bar in view. Re-aim now that the final height is known.
    scrollConvIntoView(detailEl);
  } catch (error) {
    if (generation !== convState.detailGeneration) return;
    body.replaceChildren(element("p", "conv-note note-port", `could not load: ${error.message}`));
    scrollConvIntoView(detailEl);
  }
}

/* -- vessels ---------------------------------------------------------------- */
/*
 * "Is this vessel in the cache, and when was it last seen?" -- currently a Python one-liner,
 * asked repeatedly enough to be worth a screen. Same shape as conversations above: a
 * generation counter tags every request so a slow answer to an old search cannot overwrite a
 * fast answer to a new one (`refreshVessels`), rows are a Map built once and updated in place
 * (`vesselRows`), and the search box is debounced -- 250ms per the brief, since ~6000 rows means
 * every keystroke is otherwise its own request against the whole cache.
 *
 * The one thing this screen adds that conversations does not: `name_shared` on a row is not
 * decoration, it is the reason the screen exists alongside the search box -- a name carried by
 * two MMSIs is not an identification, and it has to be visible without opening detail. See
 * `updateVesselRow`.
 */
const vesselRows = new Map();
const vesselState = {
  generation: 0,
  offset: 0,
  limit: 50,
  total: 0,
  rows: new Map(),       // mmsi -> last row seen, for the detail header
  selectedMmsi: null,
  detailGeneration: 0,
};
let vesselFilterTimer = null;

function vesselParams() {
  const params = new URLSearchParams();
  const text = $("vessel-text").value.trim();
  if (text) params.set("text", text);
  params.set("limit", String(vesselState.limit));
  params.set("offset", String(vesselState.offset));
  return params;
}

function buildVesselRow() {
  const tr = element("tr", "conv-row");
  tr.tabIndex = 0;
  const cells = {};
  for (const key of ["name", "mmsi", "callsign", "type", "destination", "draught", "last_seen"]) {
    cells[key] = element("td", "conv-cell");
    tr.append(cells[key]);
  }
  cells.mmsi.classList.add("conv-mono");
  cells.callsign.classList.add("conv-mono");
  cells.draught.classList.add("conv-mono", "conv-num");
  cells.last_seen.classList.add("conv-mono");

  // The name cell carries both the name text and the shared-name mark, as two nodes kept
  // separate so updateVesselRow can toggle the badge without touching the name's text node.
  const nameText = element("span", "vessel-name");
  const badge = element("span", "vessel-shared-badge", "shared name");
  badge.hidden = true;
  cells.name.append(nameText, badge);
  return { root: tr, cells, nameText, badge };
}

function updateVesselRow(view, row) {
  view.root.classList.toggle("conv-row-selected", row.mmsi === vesselState.selectedMmsi);
  setText(view.nameText, row.name || "—");
  // The mark itself: a name two MMSIs share cannot be trusted alone, and it has to read that
  // way right here, not only after opening detail.
  view.badge.hidden = !row.name_shared;
  setLinkedVesselCell(view.cells.mmsi, row.mmsi, row.mmsi || "—");
  setText(view.cells.callsign, row.callsign || "—");
  setText(view.cells.type, row.type || "—");
  setText(view.cells.destination, row.destination || "—");
  setText(view.cells.draught,
    row.draught === null || row.draught === undefined ? "—" : `${row.draught} m`);
  // "never", not "—": this vessel's entry has genuinely never carried a last_seen, which is a
  // different claim from a field the row just doesn't have room to show.
  setText(view.cells.last_seen, row.last_seen || "never");
}

function showVesselSnapshot(snapshot) {
  const banner = $("vessel-stale");
  if (snapshot && snapshot.stale && snapshot.has_data === false) {
    // No fetch has ever succeeded -- there is no "last copy" to report an age for.
    banner.textContent = `nothing has been fetched yet — ${snapshot.error || "unknown error"}`;
    banner.hidden = false;
  } else if (snapshot && snapshot.stale) {
    banner.textContent =
      `showing the last copy, ${elapsed(snapshot.age_sec)} old — ${snapshot.error || "unknown error"}`;
    banner.hidden = false;
  } else {
    banner.hidden = true;
  }
}

// Same distinction as showConvFetchError: this fires only when the panel itself could not be
// asked at all, never for the server successfully answering "here are zero rows".
function showVesselFetchError(message) {
  const banner = $("vessel-fetch-error");
  banner.textContent = message ? `could not reach the panel: ${message}` : "";
  banner.hidden = !message;
}

function renderVesselRows(rows) {
  const tbody = $("vessel-rows");
  const seen = new Set();
  vesselState.rows.clear();
  rows.forEach((row, index) => {
    vesselState.rows.set(row.mmsi, row);
    let view = vesselRows.get(row.mmsi);
    if (!view) {
      view = buildVesselRow();
      view.root.addEventListener("click", () => selectVessel(row.mmsi));
      view.root.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          selectVessel(row.mmsi);
        }
      });
      vesselRows.set(row.mmsi, view);
    }
    updateVesselRow(view, row);
    // See renderConvRows: only move a row when it is not already at this DOM position, so a
    // 20s poll does not drop a reader's in-progress text selection by re-appending everything.
    if (tbody.children[index] !== view.root) {
      tbody.insertBefore(view.root, tbody.children[index] || null);
    }
    seen.add(row.mmsi);
  });
  for (const [mmsi, view] of vesselRows) {
    if (!seen.has(mmsi)) {
      view.root.remove();
      vesselRows.delete(mmsi);
    }
  }
  // Only reached on a genuine "the server answered and there were none" -- a failed fetch
  // returns before this runs, so the previous rows are never relabelled as "no vessels match".
  $("vessel-empty").hidden = rows.length !== 0;
}

function renderVesselPager() {
  const note = $("vessel-page-note");
  if (vesselState.total === 0) {
    setText(note, "");
  } else {
    const from = vesselState.offset + 1;
    const to = Math.min(vesselState.offset + vesselState.limit, vesselState.total);
    setText(note, `${from}–${to} of ${vesselState.total}`);
  }
  $("vessel-prev").disabled = vesselState.offset <= 0;
  $("vessel-next").disabled = vesselState.offset + vesselState.limit >= vesselState.total;
}

async function refreshVessels() {
  const generation = ++vesselState.generation;
  let body;
  try {
    body = await api(`/api/vessels?${vesselParams().toString()}`);
  } catch (error) {
    if (generation !== vesselState.generation) return;   // a newer request already landed
    showVesselFetchError(error.message);
    return;
  }
  if (generation !== vesselState.generation) return;
  showVesselFetchError(null);
  showVesselSnapshot(body.snapshot);
  vesselState.total = body.total;
  vesselState.offset = body.offset;
  renderVesselRows(body.rows);
  renderVesselPager();
}

function vesselFiltersChanged() {
  vesselState.offset = 0;
  if (vesselFilterTimer) clearTimeout(vesselFilterTimer);
  // 250ms: ~6000 cached vessels means every keystroke is a search over all of them if this
  // fires unthrottled.
  vesselFilterTimer = setTimeout(() => refreshVessels().catch(() => {}), 250);
}

/* -- vessel detail ------------------------------------------------------------ */

const VESSEL_FIELD_LABELS = {
  mmsi: "MMSI", name: "Name", callsign: "Callsign", type: "Type", imo: "IMO",
  length: "Length", beam: "Beam", draught: "Draught", destination: "Destination",
  latitude: "Latitude", longitude: "Longitude", last_seen: "Last seen", source: "Source",
};
// Fixed order for the fields known to appear on a cache entry; anything else the entry
// happens to carry is still shown (see renderVesselFields), just after these.
const VESSEL_FIELD_ORDER = ["name", "mmsi", "callsign", "type", "destination", "draught",
                            "imo", "length", "beam", "latitude", "longitude",
                            "last_seen", "source"];

function vesselFieldValue(key, value) {
  if (value === null || value === undefined || value === "") return "—";
  if ((key === "draught" || key === "length" || key === "beam") && typeof value === "number") {
    return `${value} m`;
  }
  if ((key === "latitude" || key === "longitude") && typeof value === "number") {
    return value.toFixed(4);
  }
  return String(value);
}

function renderVesselFields(detail) {
  const dl = element("dl", "vessel-fields");
  const keys = VESSEL_FIELD_ORDER.filter((key) => key in detail);
  for (const key of Object.keys(detail)) {
    // "the full cached entry" -- a field this screen did not anticipate is still shown, not
    // silently dropped, only "conversations" and its snapshot (rendered separately below,
    // resp. consulted by it) are excluded.
    if (key !== "conversations" && key !== "conversations_snapshot" &&
        !VESSEL_FIELD_ORDER.includes(key)) {
      keys.push(key);
    }
  }
  for (const key of keys) {
    const row = element("div", "vessel-field");
    row.append(element("dt", "legend", VESSEL_FIELD_LABELS[key] || key));
    // The MMSI row carries the VesselFinder link, since the MMSI is what the lookup is keyed
    // on -- the name is exactly the field that cannot be trusted to identify a ship here.
    const link = key === "mmsi" ? vesselFinderLink(detail[key]) : null;
    if (link) {
      const dd = element("dd", null);
      dd.append(link);
      row.append(dd);
    } else {
      row.append(element("dd", null, vesselFieldValue(key, detail[key])));
    }
    dl.append(row);
  }
  return dl;
}

function renderVesselConversations(list, snapshot) {
  // "we could not ask" must never print as "there is nothing" -- see showConvSnapshot's
  // has_data distinction. Nothing else polls conversations from this tab, so this fetch is
  // live on every click and can fail with no earlier copy to fall back on.
  if (!list || !list.length) {
    if (snapshot && snapshot.error && snapshot.has_data === false) {
      return element("p", "conv-note note-port",
        `conversations could not be fetched — ${snapshot.error}`);
    }
    // Requirement 5: a vessel with no conversations must say so, not show an empty area.
    return element("p", "conv-note", "No conversations recorded for this vessel.");
  }
  const ul = element("ul", "vessel-conv-list");
  for (const c of list) {
    const li = element("li", "vessel-conv-item");
    const label = c.label || (c.identified ? "identified" : "unidentified");
    const button = element("button", "vessel-conv-link",
      `${c.start || "—"} · ${c.channel || "—"} · ${label}`);
    button.type = "button";
    button.addEventListener("click", () => openConversationFromVessel(c.id));
    li.append(button);
    ul.append(li);
  }
  return ul;
}

function renderVesselDetail(detail) {
  const body = $("vessel-detail-body");
  body.replaceChildren();   // fetched once per selection, not on a poll -- same contract as
                             // conversation detail

  body.append(element("h3", null, "Cached entry"));
  body.append(renderVesselFields(detail));

  body.append(element("h3", null, "Conversations"));
  body.append(renderVesselConversations(detail.conversations, detail.conversations_snapshot));
}

function showVesselDetail(open) {
  $("vessel-detail").hidden = !open;
}

// Jumping into the Conversations screen from here, by id -- never by name, which is exactly
// what cannot be trusted on this screen. selectConversation() already tolerates an id that
// is not on the currently loaded page (convLabel falls back to "Conversation"/detail fields),
// so this works regardless of what filters or page the Conversations tab was last left on.
function openConversationFromVessel(id) {
  showTab("conversations");
  selectConversation(id).catch(() => {});
}

async function selectVessel(mmsi) {
  vesselState.selectedMmsi = mmsi;
  for (const [rowMmsi, view] of vesselRows) {
    view.root.classList.toggle("conv-row-selected", rowMmsi === mmsi);
  }
  showVesselDetail(true);
  const detailEl = $("vessel-detail");
  // Scrolled immediately, before the fetch resolves -- see scrollConvIntoView's own comment:
  // the click needs a visible response before the network round-trip, not after it.
  scrollConvIntoView(detailEl);
  const row = vesselState.rows.get(mmsi);
  setText($("vessel-detail-title"), row && row.name ? `${row.name} (${mmsi})` : mmsi);
  const body = $("vessel-detail-body");
  body.replaceChildren(element("p", "conv-note", "Loading…"));

  const generation = ++vesselState.detailGeneration;
  try {
    const detail = await api(`/api/vessels/${encodeURIComponent(mmsi)}`);
    if (generation !== vesselState.detailGeneration) return;   // a later selection replaced this
    renderVesselDetail(detail);
    // Re-aim now the final height is known -- the placeholder undershoots the real content,
    // same reasoning as conversation detail.
    scrollConvIntoView(detailEl);
  } catch (error) {
    if (generation !== vesselState.detailGeneration) return;
    body.replaceChildren(element("p", "conv-note note-port", `could not load: ${error.message}`));
    scrollConvIntoView(detailEl);
  }
}

/* -- settings ----------------------------------------------------------------- */
/*
 * The schema (39 settings across 9 groups) is fixed for the life of the page, so the form is
 * built once from the first GET /api/settings answer and every later load -- including after a
 * save -- only updates values in place, the same cardViews/feedViews shape used everywhere else
 * here. Unlike Conversations and Vessels, this screen is never polled: a timer overwriting a
 * field the operator is mid-edit would be worse than a stale read, so it loads once per visit
 * and only Reload or a completed Save re-fetches it.
 *
 * The one rule that matters: settings_api.form() never sends a secret's value, only whether one
 * is set (spec.set), and nothing here invents a way around that -- there is no "reveal" control.
 * An empty password box submits nothing for that key at all, which the server reads as "leave
 * it alone"; Clear stages the CLEAR_SENTINEL, which must stay byte-for-byte equal to
 * settings_api.CLEAR on the server.
 */
const CLEAR_SENTINEL = "__CLEAR__";
const settingsFieldViews = new Map();   // key -> field view
const settingsState = { loaded: false, built: false, fields: {}, paths: new Map(), processes: [] };

function buildSettingsControl(spec) {
  const wrap = element("div", "settings-control");

  if (spec.type === "secret") {
    const input = element("input", "settings-input");
    input.type = "password";
    input.autocomplete = "new-password";
    input.placeholder = "leave blank to keep the current value";
    const status = element("span", "settings-secret-status");
    const clearButton = element("button", "button button-quiet settings-clear", "Clear");
    clearButton.type = "button";
    const control = { wrap, input, status, clearButton, clearRequested: false };
    // Toggle, not fire-and-forget: Clear only STAGES the sentinel, so a save still reports what
    // changed and whether anything needs a restart, exactly like every other field here.
    clearButton.addEventListener("click", () => {
      control.clearRequested = !control.clearRequested;
      input.value = "";
      setText(clearButton, control.clearRequested ? "Undo clear" : "Clear");
      wrap.closest(".settings-field")
        ?.classList.toggle("settings-field-pending-clear", control.clearRequested);
    });
    wrap.append(input, status, clearButton);
    return control;
  }

  if (spec.type === "bool") {
    const label = element("label", "check");
    const input = element("input");
    input.type = "checkbox";
    label.append(input, element("span", null, "on"));
    wrap.append(label);
    return { wrap, input };
  }

  if (spec.type === "enum") {
    const input = element("select", "settings-select");
    for (const choice of spec.choices || []) {
      const option = element("option", null, choice);
      option.value = choice;
      input.append(option);
    }
    wrap.append(input);
    return { wrap, input };
  }

  const input = element("input", "settings-input");
  input.type = spec.type === "int" ? "number" : "text";
  if (spec.type === "int") {
    if (spec.minimum !== undefined && spec.minimum !== null) input.min = String(spec.minimum);
    if (spec.maximum !== undefined && spec.maximum !== null) input.max = String(spec.maximum);
  }
  wrap.append(input);
  // Only a PATH field carries a resolve mark -- the same fact the Dashboard's path strip shows,
  // reused rather than recomputed client-side (see updateSettingsField).
  let mark = null;
  if (spec.type === "path") {
    mark = element("span", "settings-mark");
    mark.hidden = true;
    wrap.append(mark);
  }
  return { wrap, input, mark };
}

function buildSettingsField(key, spec) {
  const row = element("div", "settings-field");
  row.dataset.key = key;
  const head = element("div", "settings-field-head");
  head.append(element("span", "legend settings-field-label", spec.label));
  head.append(element("p", "settings-desc", spec.description));
  row.append(head);

  const control = buildSettingsControl(spec);
  row.append(control.wrap);
  return { root: row, ...control };
}

function buildSettingsForm(groups, fields) {
  const root = $("settings-groups");
  root.replaceChildren();
  settingsFieldViews.clear();
  for (const group of groups) {
    const section = element("section", "settings-group card");
    section.append(element("h2", "legend settings-group-title", group.name));
    for (const key of group.keys) {
      const view = buildSettingsField(key, fields[key]);
      settingsFieldViews.set(key, view);
      section.append(view.root);
    }
    root.append(section);
  }
}

function updateSettingsField(key, view, spec) {
  if (spec.type === "secret") {
    view.input.value = "";
    view.clearRequested = false;
    setText(view.status, spec.set ? "set" : "not set");
    view.status.className = `settings-secret-status ${spec.set ? "is-set" : "is-unset"}`;
    setText(view.clearButton, "Clear");
    view.clearButton.disabled = !spec.set;
    view.root.classList.remove("settings-field-pending-clear");
    return;
  }
  if (spec.type === "bool") {
    view.input.checked = (spec.value || "off").toLowerCase() === "on";
    return;
  }
  view.input.value = spec.value ?? "";
  if (view.mark) {
    const check = settingsState.paths.get(key);
    view.mark.hidden = !check;
    if (check) {
      setText(view.mark, check.resolves ? "✓" : "✗");
      view.mark.className = `settings-mark ${check.resolves ? "settings-mark-ok" : "settings-mark-bad"}`;
    }
  }
}

function collectSettingsSubmission() {
  const body = {};
  for (const [key, view] of settingsFieldViews) {
    const spec = settingsState.fields[key];
    if (!spec) continue;
    if (spec.type === "secret") {
      if (view.clearRequested) body[key] = CLEAR_SENTINEL;
      else if (view.input.value !== "") body[key] = view.input.value;
      // Otherwise omitted entirely: an untouched secret must not even be named in the request,
      // let alone carry a value -- see the module comment.
    } else if (spec.type === "bool") {
      body[key] = view.input.checked ? "on" : "off";
    } else {
      body[key] = view.input.value;
    }
  }
  return body;
}

async function refreshSettings() {
  let form, health, processes;
  try {
    [form, health, processes] = await Promise.all([
      api("/api/settings"),
      api("/api/health"),
      api("/api/processes"),
    ]);
  } catch (error) {
    $("settings-fetch-error").textContent = `could not reach the panel: ${error.message}`;
    $("settings-fetch-error").hidden = false;
    return;
  }
  $("settings-fetch-error").hidden = true;
  settingsState.processes = processes.processes;
  settingsState.paths = new Map((health.paths || []).map((p) => [p.key, p]));
  settingsState.fields = form.fields;

  if (!settingsState.built) {
    buildSettingsForm(form.groups, form.fields);
    settingsState.built = true;
  }
  for (const [key, view] of settingsFieldViews) {
    updateSettingsField(key, view, form.fields[key]);
  }
  settingsState.loaded = true;
}

async function restartFromSettings(name, button) {
  button.disabled = true;
  try {
    await api(`/api/processes/${encodeURIComponent(name)}/restart`, { method: "POST" });
    setText(button, "Restarted");
  } catch (error) {
    showBanner(error.message);
    setText(button, `Restart ${processLabel(settingsState.processes, name)}`);
    button.disabled = false;
  }
}

function showSettingsResult(result) {
  const panel = $("settings-result");
  const summary = $("settings-result-summary");
  const restartList = $("settings-restart-list");
  restartList.replaceChildren();

  if (result.error) {
    setText(summary, result.error);
    summary.className = "settings-result-summary settings-result-error";
    panel.hidden = false;
    scrollConvIntoView(panel);
    return;
  }

  const changed = result.changed || [];
  setText(summary, changed.length ? `Saved. Changed: ${changed.join(", ")}.` : "Saved. Nothing changed.");
  summary.className = "settings-result-summary";

  // Said explicitly, one line per process: a setting a process only reads at its own startup is
  // not in effect until that process restarts, and this is where a reader would otherwise never
  // be told that.
  for (const name of result.restart_needed || []) {
    if (name === "panel") {
      restartList.append(element("p", "settings-restart-line",
        "Restart the control panel itself for this to take effect -- there is no button for "
        + "that here; stop and start py -m webapp by hand."));
      continue;
    }
    const label = processLabel(settingsState.processes, name);
    const row = element("div", "settings-restart-row");
    row.append(element("p", "settings-restart-line", `Restart ${label} for this to take effect.`));
    const button = element("button", "button button-primary", `Restart ${label}`);
    button.type = "button";
    button.addEventListener("click", () => restartFromSettings(name, button));
    row.append(button);
    restartList.append(row);
  }

  panel.hidden = false;
  scrollConvIntoView(panel);
}

$("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const saveButton = $("settings-save");
  saveButton.disabled = true;
  try {
    const body = collectSettingsSubmission();
    const result = await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
    showSettingsResult(result);
    await refreshSettings();   // reflect exactly what is now stored, including any Clear
  } catch (error) {
    showSettingsResult({ error: error.message });
  } finally {
    saveButton.disabled = false;
  }
});

$("settings-reload").addEventListener("click", () => { refreshSettings().catch(() => {}); });
$("settings-result-close").addEventListener("click", () => { $("settings-result").hidden = true; });

/* -- views and polling ---------------------------------------------------- */

function showBanner(message) {
  const banner = $("banner");
  banner.textContent = message || "";
  banner.hidden = !message;
}

function showTab(name) {
  state.tab = name;
  $("dashboard").hidden = name !== "dashboard";
  $("conversations").hidden = name !== "conversations";
  $("vessels").hidden = name !== "vessels";
  $("logs").hidden = name !== "logs";
  $("settings").hidden = name !== "settings";
  for (const tab of document.querySelectorAll(".tab")) {
    tab.setAttribute("aria-selected", String(tab.dataset.tab === name));
  }
  tick();
}

function tick() {
  if (document.hidden) return;
  if (state.tab === "dashboard") refreshDashboard().catch(() => {});
  else if (state.tab === "conversations") refreshConversations().catch(() => {});
  else if (state.tab === "vessels") refreshVessels().catch(() => {});
  else if (state.tab === "logs") tabLog.pull().catch(() => {});
  // Settings is deliberately not polled -- see the module comment above refreshSettings --
  // so it loads once per visit rather than on every tick.
  else if (state.tab === "settings" && !settingsState.loaded) refreshSettings().catch(() => {});
}

function startPolling() {
  stopPolling();
  // The dashboard is cheap and answers "is it still running?"; the log is the faster of the
  // two because it is being read while something is happening. Conversations sit between them:
  // the webapp itself only refreshes its copy every 15s (CONVERSATIONS_TTL_SEC), so polling
  // faster than that would just re-fetch the same cached answer. Vessels changes only when the
  // AIS feed polls -- the webapp's own copy is good for 60s (VESSELS_TTL_SEC) -- so it polls
  // slower still; a search itself still answers immediately through vesselFiltersChanged.
  state.timers.push(setInterval(() => { if (state.tab === "dashboard") tick(); }, 3000));
  state.timers.push(setInterval(() => { if (state.tab === "conversations") tick(); }, 5000));
  state.timers.push(setInterval(() => { if (state.tab === "vessels") tick(); }, 20000));
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

$("conv-identified").addEventListener("change", () => convFiltersChanged(true));
$("conv-channel").addEventListener("input", () => convFiltersChanged(false));
$("conv-text").addEventListener("input", () => convFiltersChanged(false));
$("conv-prev").addEventListener("click", () => {
  convState.offset = Math.max(0, convState.offset - convState.limit);
  refreshConversations().catch(() => {});
});
$("conv-next").addEventListener("click", () => {
  convState.offset += convState.limit;
  refreshConversations().catch(() => {});
});
$("conv-detail-close").addEventListener("click", () => {
  convState.selectedId = null;
  convState.detailGeneration += 1;   // abandon whatever detail fetch might still be in flight
  for (const view of convRows.values()) view.root.classList.remove("conv-row-selected");
  showConvDetail(false);
  // Hiding the (possibly long) detail panel collapses the page under the reader's scroll
  // position, which can leave them staring at whatever now-empty space took its place. Land
  // back on the pager, right above where the detail was, rather than nowhere in particular.
  scrollConvIntoView($("conv-pager"));
});

$("vessel-text").addEventListener("input", vesselFiltersChanged);
$("vessel-prev").addEventListener("click", () => {
  vesselState.offset = Math.max(0, vesselState.offset - vesselState.limit);
  refreshVessels().catch(() => {});
});
$("vessel-next").addEventListener("click", () => {
  vesselState.offset += vesselState.limit;
  refreshVessels().catch(() => {});
});
$("vessel-detail-close").addEventListener("click", () => {
  vesselState.selectedMmsi = null;
  vesselState.detailGeneration += 1;   // abandon whatever detail fetch might still be in flight
  for (const view of vesselRows.values()) view.root.classList.remove("conv-row-selected");
  showVesselDetail(false);
  scrollConvIntoView($("vessel-pager"));
});

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
