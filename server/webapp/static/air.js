/* The Airband tab: Approach 4 transmissions grouped per flight.
 *
 * Left: strips, one per flight (plus Needs review, unknown callsigns and Unassigned).
 * Right: the selected strip's transmissions, each with its clue badge, audio and "move to".
 * Uses api(), element(), $() and renderTurnAudio() from app.js.
 */
"use strict";

// gen: bumped by every refresh, so a slow older response never overwrites a newer one.
// threadSig: what the thread showed last render, so an unchanged poll does not rebuild it.
const airState = { range: "live", day: "", hour: "", selected: null, gen: 0, threadSig: null };

const AIR_BADGES = {
  confirmed: ["✔✔", "callsign and autopilot agree"],
  callsign: ["✔", "callsign heard"],
  echo: ["✔", "autopilot change only"],
  conflict: ["⚠", "callsign and autopilot disagree"],
  unknown: ["?", "callsign heard, not in ADS-B"],
  none: ["—", "no clue"],
  moved: ["✎", "moved by hand"],
};

const ADSBFI_GLOBE_URL = "https://globe.adsb.fi/";

/** The callsign linked to the aircraft on globe.adsb.fi, or null when there is no ICAO hex.
 *
 * The Airband counterpart of app.js's vesselFinderLink. `day` (YYYY-MM-DD, UTC) opens that day's
 * recorded track rather than the aircraft's position now -- what a past hour is being
 * reviewed for. Six hex digits only: a strip key can also be "review", "unassigned" or
 * "heard:...", and none of those names an aircraft. */
function adsbFiLink(hex, text, day) {
  const id = String(hex || "").trim().toLowerCase();
  if (!/^[0-9a-f]{6}$/.test(id)) return null;
  const link = element("a", "vf-link", text || id);
  let url = `${ADSBFI_GLOBE_URL}?icao=${encodeURIComponent(id)}`;
  if (day && /^\d{4}-\d{2}-\d{2}$/.test(day)) url += `&showTrace=${day}`;
  link.href = url;
  link.target = "_blank";
  // Same as vesselFinderLink: no reason to tell the outside site which local page sent us.
  link.rel = "noopener noreferrer";
  link.title = `Open ${text || id} (${id}) on globe.adsb.fi` + (url.includes("showTrace") ? `, track of ${day}` : "");
  return link;
}

function airRangeParams() {
  if (airState.range === "live") return "";
  const now = new Date();
  let from;
  let to;
  if (airState.range === "hour") {
    to = now;
    from = new Date(now.getTime() - 3600 * 1000);
  } else {
    const [y, m, d] = airState.day.split("-").map(Number);
    from = new Date(y, m - 1, d, Number(airState.hour) || 0, 0, 0);
    to = new Date(from.getTime() + 3600 * 1000);
  }
  const iso = (dt) => {
    const off = -dt.getTimezoneOffset();
    const sign = off >= 0 ? "+" : "-";
    const pad = (n) => String(Math.floor(Math.abs(n))).padStart(2, "0");
    return `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}T` +
      `${pad(dt.getHours())}:${pad(dt.getMinutes())}:${pad(dt.getSeconds())}` +
      `${sign}${pad(off / 60)}:${pad(off % 60)}`;
  };
  return `from=${encodeURIComponent(iso(from))}&to=${encodeURIComponent(iso(to))}`;
}

function airFeet(value) {
  if (value === null || value === undefined || value === "ground") return value || "—";
  return value >= 10000 ? `FL${Math.round(value / 100)}` : `${Math.round(value)}`;
}

function airStateLine(s) {
  if (!s) return "";
  const parts = [];
  if (s.alt_baro !== undefined && s.alt_baro !== null) {
    parts.push(s.nav_altitude_mcp ? `${airFeet(s.alt_baro)} → set ${airFeet(s.nav_altitude_mcp)}`
                                  : airFeet(s.alt_baro));
  }
  if (typeof s.nav_heading === "number") parts.push(`hdg ${String(Math.round(s.nav_heading)).padStart(3, "0")}`);
  return parts.join(" · ");
}

function airAgo(nowEpoch, epoch) {
  return elapsed(nowEpoch - epoch);
}

function renderAirStrips(body) {
  const list = $("air-strips");
  list.replaceChildren();
  if (!body.strips.length) {
    list.append(element("li", "conv-empty",
      body.live ? "Nothing on Approach 4 in the last 15 minutes." : "Nothing in this period."));
    return;
  }
  for (const strip of body.strips) {
    const li = element("li", `air-strip air-${strip.kind}`);
    li.tabIndex = 0;
    li.setAttribute("aria-selected", String(strip.key === airState.selected));
    const head = element("div", "air-strip-head");
    head.append(element("b", "", strip.kind === "unknown" ? `? “${strip.label}”` : strip.label));
    const meta = [strip.airline, strip.type].filter(Boolean).join(" · ");
    if (meta) head.append(element("span", "air-meta", ` ${meta}`));
    head.append(element("span", "air-ago", airAgo(body.now, strip.last_epoch)));
    li.append(head);
    const line = strip.kind === "unknown" ? `heard ${strip.count}× · not in ADS-B`
      : [airStateLine(strip.state), `${strip.count} tx`].filter(Boolean).join(" · ");
    li.append(element("div", "air-sub", line));
    const pick = () => { airState.selected = strip.key; refreshAirband().catch(() => {}); };
    li.addEventListener("click", pick);
    li.addEventListener("keydown", (e) => { if (e.key === "Enter") pick(); });
    list.append(li);
  }
}

const AIR_NEW_FLIGHT = "__new__";

/* "New flight…": every aircraft in ADS-B range at this transmission's time, nearest first,
 * filtered as you type. Picking one moves the row there; the server re-checks that the
 * aircraft was really in range and names the new strip from it. */
async function openAirPicker(row, line) {
  for (const open of document.querySelectorAll("#air-thread .air-pick")) open.remove();
  const panel = element("div", "air-pick");
  const head = element("div", "air-pick-head", `New flight at ${row.t.slice(11, 19)}`);
  const close = element("button", "air-pick-close", "✕");
  close.type = "button";
  close.title = "Close";
  close.addEventListener("click", () => panel.remove());
  head.append(close);
  const input = element("input", "air-pick-input");
  input.type = "search";
  input.placeholder = "callsign, type or registration";
  input.setAttribute("aria-label", "Filter the aircraft in range");
  const list = element("ul", "air-pick-list");
  panel.append(head, input, list);
  panel.addEventListener("keydown", (e) => { if (e.key === "Escape") panel.remove(); });
  line.after(panel);
  list.append(element("li", "air-pick-note", "Loading aircraft in range…"));
  input.focus();

  const body = await api(`/api/air/aircraft?at=${encodeURIComponent(row.t)}`);
  const render = () => {
    list.replaceChildren();
    if (body.error) {
      list.append(element("li", "air-pick-note", body.error));
      return;
    }
    const want = input.value.trim().toUpperCase();
    const hits = body.aircraft.filter((a) => !want ||
      [a.flight, a.type, a.reg].some((v) => String(v || "").toUpperCase().includes(want)));
    if (!hits.length) {
      list.append(element("li", "air-pick-note", "No aircraft in range matches."));
      return;
    }
    for (const a of hits) {
      const item = element("li");
      const pick = element("button", "air-pick-item");
      pick.type = "button";
      pick.append(element("b", "", a.flight),
        element("span", "air-meta", ` ${[a.type, a.reg].filter(Boolean).join(" · ")}`),
        element("span", "air-pick-alt", `${airFeet(a.alt)}${a.km !== null ? ` · ${a.km} km` : ""}`));
      pick.addEventListener("click", () => {
        panel.remove();
        moveAirRow(row, a.hex).catch((e) => showAirError(e.message));
      });
      item.append(pick);
      list.append(item);
    }
  };
  input.addEventListener("input", render);
  render();
}

async function moveAirRow(row, toKey) {
  await api("/api/air/moves", { method: "POST",
    body: JSON.stringify({ transmission_id: row.id, to_key: toKey }) });
  await refreshAirband();
}

function renderAirThread(strip, rows) {
  const box = $("air-thread");
  box.replaceChildren();
  if (!strip) {
    box.append(element("p", "conv-empty", "Pick a flight on the left."));
    return;
  }
  const head = element("p", "air-thread-head");
  // tar1090's showTrace takes a UTC date (README-query.md), so it comes from the epoch, not from
  // last_t's local date -- 00:30 in Amsterdam is still the previous day's trace.
  const traceDay = airState.range === "live" || !strip.last_epoch ? null
    : new Date(strip.last_epoch * 1000).toISOString().slice(0, 10);
  const link = strip.kind === "flight" ? adsbFiLink(strip.key, strip.label, traceDay) : null;
  const name = element("b");
  name.append(link || document.createTextNode(strip.label));
  head.append(name);
  const extra = [strip.airline, strip.type, strip.reg, airStateLine(strip.state)].filter(Boolean);
  if (extra.length) head.append(element("span", "air-meta", ` · ${extra.join(" · ")}`));
  box.append(head);

  for (const row of rows) {
    const [mark, word] = AIR_BADGES[row.badge] || AIR_BADGES.none;
    const line = element("div", `air-row air-badge-${row.badge}`);
    line.append(element("span", "air-time", row.t.slice(11, 19)));
    const badge = element("span", "air-badge", mark);
    badge.title = `${word}: ${row.evidence}`;
    line.append(badge);
    line.append(element("span", "air-text", row.text));
    const controls = element("span", "air-controls");
    const audio = renderTurnAudio(row);
    if (audio) controls.append(audio);
    const select = document.createElement("select");
    select.className = "air-move";
    select.setAttribute("aria-label", "Move this transmission to another flight");
    select.append(new Option("move to…", ""));
    for (const target of row.targets) select.append(new Option(target.label, target.key));
    const rule = new Option("──────────", "");
    rule.disabled = true;
    select.append(rule, new Option("New flight…", AIR_NEW_FLIGHT));
    select.addEventListener("change", () => {
      if (select.value === AIR_NEW_FLIGHT) {
        select.value = "";
        openAirPicker(row, line).catch((e) => showAirError(e.message));
      } else if (select.value) {
        moveAirRow(row, select.value).catch((e) => showAirError(e.message));
      }
    });
    controls.append(select);
    line.append(controls);
    box.append(line);
  }
}

function showAirError(message) {
  const note = $("air-error");
  note.textContent = message || "";
  note.hidden = !message;
}

function renderAirStatus(body) {
  const feed = body.adsb;
  const feedText = !feed ? "ADS-B: proxy not answering"
    : feed.consecutive_failures ? `ADS-B: failing (${feed.consecutive_failures})`
    : `ADS-B: OK · ${feed.last_count ?? "?"} aircraft`;
  $("air-status").textContent =
    `${feedText} · autopilot clue: ${body.echo_enabled ? "on" : "recording, not shown"}`;
}

// True while the reader is using the thread: a clip is playing, a "move to" is open or the
// "New flight…" picker is showing. Rebuilding it then would stop the audio or snatch it away.
function airThreadInUse() {
  const playing = [...document.querySelectorAll("#air-thread audio")].some((a) => !a.paused);
  const active = document.activeElement;
  return playing || Boolean(document.querySelector("#air-thread .air-pick"))
    || Boolean(active && active.classList && active.classList.contains("air-move"));
}

function airThreadSignature(strip, rows) {
  return strip ? `${strip.key}|${rows.map((r) => `${r.id}:${r.badge}`).join(",")}` : "";
}

/* auto: true for the 15 s poll. It only refreshes Live (a past hour does not change), and it
 * leaves the thread alone while the reader is using it or when nothing in it changed. A user
 * action (a pill, a day, a strip, a move) passes nothing and always renders. */
async function refreshAirband({ auto = false } = {}) {
  if (auto && airState.range !== "live") return;
  const gen = ++airState.gen;
  const params = airRangeParams();
  let body;
  try {
    body = await api(`/api/air/flights${params ? `?${params}` : ""}`);
  } catch (error) {
    if (gen !== airState.gen) return;   // a newer request has already landed
    throw error;
  }
  if (gen !== airState.gen) return;
  showAirError(body.error);
  renderAirStatus(body);
  if (airState.selected && !body.strips.some((s) => s.key === airState.selected)) {
    airState.selected = null;
  }
  renderAirStrips(body);
  const strip = body.strips.find((s) => s.key === airState.selected);
  if (!strip) {
    airState.threadSig = airThreadSignature(null, []);
    renderAirThread(null, []);
    return;
  }
  const extra = params ? `&${params}` : "";
  let thread;
  try {
    thread = await api(`/api/air/thread?key=${encodeURIComponent(strip.key)}${extra}`);
  } catch (error) {
    if (gen !== airState.gen) return;
    throw error;
  }
  if (gen !== airState.gen) return;
  if (thread.error) showAirError(thread.error);
  const sig = airThreadSignature(strip, thread.rows);
  if (auto && (airThreadInUse() || sig === airState.threadSig)) return;
  airState.threadSig = sig;
  renderAirThread(strip, thread.rows);
}

function setAirRange(range) {
  airState.range = range;
  for (const pill of document.querySelectorAll(".air-pill")) {
    pill.setAttribute("aria-pressed", String(pill.dataset.range === range));
  }
  refreshAirband().catch((e) => showAirError(e.message));
}

for (const pill of document.querySelectorAll(".air-pill")) {
  pill.addEventListener("click", () => setAirRange(pill.dataset.range));
}
for (const id of ["air-day", "air-hour"]) {
  $(id).addEventListener("change", () => {
    airState.day = $("air-day").value;
    airState.hour = $("air-hour").value;
    if (airState.day) setAirRange("pick");
  });
}
