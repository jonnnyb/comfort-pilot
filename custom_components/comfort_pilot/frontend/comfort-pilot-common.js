/* Shared rendering for the Comfort Pilot panel's Overview and the dashboard card. */

export const HOUSE_LABELS = { auto: "Auto", home: "Home", away: "Away", boost: "Boost", off: "Off", paused: "Paused" };
export const HOUSE_HELP = {
  auto: "Follows who's home",
  home: "Assume someone's home",
  away: "Everything off",
  boost: "Comfortable end, minus 1°",
  off: "All units off",
  paused: "Hands off – units left alone",
};
export const ROOM_LABELS = { auto: "Auto", comfort: "Comfort", eco: "Eco", off: "Off" };
export const LEVEL_LABELS = {
  comfort: "Comfort", eco: "Eco", boost: "Boost", rule: "Rule", safety: "Safety",
  off: "Off", hold: "Manual", paused: "Paused", precondition: "Pre-cooling",
};

export const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
export const fmtT = (v) => (v === null || v === undefined ? "–" : `${Number(v).toFixed(1).replace(/\.0$/, "")}°`);
export const fmtW = (v) =>
  v === null || v === undefined ? "–" : Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(1)} kW` : `${Math.round(v)} W`;

export function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === new Date().toDateString()) return `Today ${time}`;
  return `${d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })} ${time}`;
}

const cap = (t) => (t ? t.charAt(0).toUpperCase() + t.slice(1) : t);

function gridText(grid) {
  if (grid === null || grid === undefined) return "–";
  return grid < 0 ? `Exporting ${fmtW(-grid)}` : `Importing ${fmtW(grid)}`;
}

function adviceBanner(h) {
  if (h.advice === "open")
    return `<div class="banner">It's ${fmtT(h.outside)} outside and ${fmtT(h.inside)} inside – a good time for the evap on fan or open windows.</div>`;
  if (h.advice === "close")
    return `<div class="banner warn">Outside (${fmtT(h.outside)}) is now warmer than inside (${fmtT(h.inside)}) – time to close up.</div>`;
  return "";
}

const stat = (label, value) =>
  `<div class="stat"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div></div>`;

export function renderStats(h) {
  return `<div class="stats">
    ${stat("Solar for comfort", fmtW(h.solar_budget))}
    ${stat("Grid", gridText(h.grid_power))}
    ${stat("Outside", fmtT(h.outside))}
    ${stat("Inside (avg)", fmtT(h.inside))}
    ${stat("Forecast", `${fmtT(h.forecast_high)} / ${fmtT(h.forecast_low)}`)}
    ${stat("Season", cap(h.season))}
  </div>`;
}

/** House mode chips, fresh air switch, advice and (optionally) stats. */
export function renderHouse(d, { stats = true } = {}) {
  const h = d.house;
  const modes = d.choices.house_modes.map((m) =>
    `<button class="chip ${h.mode === m ? "on" : ""}" data-action="house-mode" data-value="${m}" title="${esc(HOUSE_HELP[m])}">${HOUSE_LABELS[m]}</button>`).join("");
  return `<div class="row space">
      <div>
        <div class="label">House mode</div>
        <div class="chips">${modes}</div>
        <div class="muted small">${esc(HOUSE_HELP[h.mode])}</div>
      </div>
      ${freshAirSwitch(h)}
    </div>
    ${adviceBanner(h)}
    ${stats ? renderStats(h) : ""}`;
}

/** One-line house controls for the compact layout. */
export function renderHouseCompact(d, { stats = false } = {}) {
  const h = d.house;
  const options = d.choices.house_modes.map((m) =>
    `<option value="${m}" ${h.mode === m ? "selected" : ""}>${HOUSE_LABELS[m]}</option>`).join("");
  return `<div class="row space chead">
      <label class="house-select"><span class="muted small">House</span>
        <select data-action="house-mode" aria-label="House mode">${options}</select></label>
      ${freshAirSwitch(h, true)}
    </div>
    ${adviceBanner(h)}
    ${stats ? renderStats(h) : ""}`;
}

function freshAirSwitch(h, small = false) {
  return `<button class="switch ${small ? "small" : ""} ${h.fresh_air ? "on" : ""}" data-action="fresh-air" role="switch" aria-checked="${h.fresh_air}">
    <span class="knob"></span><span class="switch-label">Fresh air${small ? "" : " mode"}</span></button>`;
}

function occupancyDot(st) {
  if (!st.has_occupancy_sensor) return "";
  const cls = st.occupied ? "occ" : st.occupancy_pending ? "pending" : "";
  const title = st.occupied ? "Occupied" : st.occupancy_pending ? "Motion – confirming" : "No one detected";
  return `<span class="dot ${cls}" title="${title}"></span>`;
}

function unitText(st) {
  if (st.available === false) return "Unavailable";
  const running = st.hvac_mode && st.hvac_mode !== "off";
  return running ? `${cap(st.hvac_mode.replace("_", " "))} ${fmtT(st.target)}` : "Off";
}

/** A room tile. Tapping the top half opens the thermostat. */
export function renderRoomTile(r, { buttons = true } = {}) {
  const st = r.state || {};
  const dec = r.decision || {};
  const level = dec.level || "off";
  const modes = Object.keys(ROOM_LABELS).map((m) =>
    `<button class="chip small ${r.mode === m ? "on" : ""}" data-action="room-mode" data-room="${esc(r.id)}" data-value="${m}">${ROOM_LABELS[m]}</button>`).join("");
  const hold = r.hold_until
    ? `<div class="hold">Manual change – resumes ${esc(when(r.hold_until))}
         <button class="link" data-action="resume" data-room="${esc(r.id)}">Resume now</button></div>`
    : "";
  return `<div class="tile">
    <div class="tap" data-action="more-info" data-entity="${esc(r.id)}" title="Open thermostat">
      <div class="row space">
        <div class="room-name">${occupancyDot(st)}${esc(r.name)}</div>
        <span class="badge ${level}">${LEVEL_LABELS[level] || level}</span>
      </div>
      <div class="row temps">
        <div class="big">${fmtT(st.current_temp)}</div>
        <div class="unit">${esc(unitText(st))}</div>
      </div>
      ${hold ? "" : `<div class="reason">${esc(dec.reason || "")}</div>`}
    </div>
    ${hold}
    ${buttons ? `<div class="chips">${modes}</div>` : ""}
  </div>`;
}

/** A single compact row for a room (no buttons; tap opens the thermostat). */
export function renderRoomRow(r) {
  const st = r.state || {};
  const dec = r.decision || {};
  const level = r.hold_until ? "hold" : dec.level || "off";
  const reason = r.hold_until ? `Manual change – resumes ${when(r.hold_until)}` : dec.reason || "";
  return `<div class="crow" data-action="more-info" data-entity="${esc(r.id)}" title="Open thermostat">
    <span class="cdot">${occupancyDot(st)}</span>
    <div class="cmain"><div class="cname">${esc(r.name)}</div><div class="creason">${esc(reason)}</div></div>
    <div class="ctemp"><div>${fmtT(st.current_temp)}</div><div class="cunit">${esc(unitText(st))}</div></div>
    <span class="badge ${level}">${LEVEL_LABELS[level] || level}</span>
  </div>`;
}

/** Turn a clicked/changed element into a WebSocket message (or null). */
export function actionMessage(el, data) {
  const a = el.dataset.action;
  if (a === "house-mode") return { type: "comfort_pilot/set_house_mode", mode: el.dataset.value || el.value };
  if (a === "fresh-air") return { type: "comfort_pilot/set_fresh_air", on: !data.house.fresh_air };
  if (a === "room-mode") return { type: "comfort_pilot/set_room_mode", room: el.dataset.room, mode: el.dataset.value };
  if (a === "resume") return { type: "comfort_pilot/resume", room: el.dataset.room };
  return null;
}

export function fireMoreInfo(node, entityId) {
  node.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId }, bubbles: true, composed: true }));
}

export const BASE_STYLES = `
  .muted { color:var(--secondary-text-color); }
  .small { font-size:12px; }
  .label { color:var(--secondary-text-color); font-size:12px; margin-bottom:4px; }
  .row { display:flex; align-items:center; gap:12px; }
  .row.space { justify-content:space-between; flex-wrap:wrap; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(260px, 1fr)); gap:16px; }
  .stats { display:grid; grid-template-columns:repeat(auto-fit, minmax(130px, 1fr)); gap:12px; margin-top:16px;
    border-top:1px solid var(--divider-color); padding-top:12px; }
  .stat .value { font-size:18px; }
  .chips { display:flex; flex-wrap:wrap; gap:6px; align-items:center; margin:4px 0; }
  .chip { border:1px solid var(--divider-color); background:none; color:var(--primary-text-color); border-radius:16px; padding:6px 12px;
    font-size:13px; cursor:pointer; font-family:inherit; }
  .chip.small { padding:4px 10px; font-size:12px; }
  .chip.on { background:var(--primary-color); border-color:var(--primary-color); color:var(--text-primary-color, #fff); }
  .switch { display:flex; align-items:center; gap:10px; background:none; border:none; cursor:pointer; color:var(--primary-text-color);
    font-family:inherit; font-size:14px; padding:4px; }
  .switch.small { font-size:13px; gap:8px; }
  .switch .knob { width:36px; height:20px; border-radius:10px; background:var(--disabled-color, #bdbdbd); position:relative; transition:background .2s; flex:none; }
  .switch .knob::after { content:""; position:absolute; top:2px; left:2px; width:16px; height:16px; border-radius:50%; background:#fff; transition:transform .2s; }
  .switch.on .knob { background:var(--primary-color); }
  .switch.on .knob::after { transform:translateX(16px); }
  .banner { margin-top:12px; padding:10px 12px; border-radius:8px; background:rgba(3,169,244,.12); font-size:14px; }
  .banner.warn { background:rgba(255,152,0,.15); }
  .room-name { font-size:16px; font-weight:500; display:flex; align-items:center; gap:8px; }
  .dot { display:inline-block; width:8px; height:8px; border-radius:50%; background:var(--disabled-color, #bdbdbd); flex:none; }
  .dot.occ { background:var(--success-color, #43a047); }
  .dot.pending { background:var(--warning-color, #ffa000); }
  .tap { cursor:pointer; border-radius:8px; margin:-6px; padding:6px; }
  .tap:hover { background:rgba(127,127,127,.07); }
  .temps { align-items:baseline; margin:8px 0 4px; }
  .big { font-size:32px; font-weight:300; }
  .unit { color:var(--secondary-text-color); }
  .reason { color:var(--secondary-text-color); font-size:13px; min-height:18px; margin-bottom:2px; }
  .hold { font-size:13px; background:rgba(33,150,243,.12); padding:6px 8px; border-radius:6px; margin:8px 0 4px; }
  .tile .chips { margin-top:10px; }
  .link { background:none; border:none; color:var(--primary-color); cursor:pointer; font-size:13px; padding:2px 4px; font-family:inherit; }
  .badge { font-size:11px; padding:2px 8px; border-radius:10px; background:var(--secondary-background-color, #eee); color:var(--primary-text-color); white-space:nowrap; }
  .badge.comfort, .badge.precondition { background:rgba(67,160,71,.18); color:var(--success-color, #2e7d32); }
  .badge.eco { background:rgba(255,152,0,.18); color:var(--warning-color, #e65100); }
  .badge.boost, .badge.rule { background:rgba(103,58,183,.15); color:#7e57c2; }
  .badge.safety { background:rgba(244,67,54,.15); color:var(--error-color, #c62828); }
  .badge.hold { background:rgba(33,150,243,.15); color:#1e88e5; }
  .chead { gap:8px; }
  .house-select { display:flex; align-items:center; gap:8px; }
  .house-select select { font:inherit; font-size:14px; padding:6px 8px; border-radius:6px; border:1px solid var(--divider-color);
    background:var(--secondary-background-color, transparent); color:var(--primary-text-color); }
  .crow { display:grid; grid-template-columns:12px 1fr auto auto; gap:10px; align-items:center; padding:10px 6px; margin:0 -6px;
    border-top:1px solid var(--divider-color); cursor:pointer; border-radius:6px; }
  .crow:hover { background:rgba(127,127,127,.07); }
  .cname { font-weight:500; font-size:15px; }
  .cmain { min-width:0; }
  .creason { color:var(--secondary-text-color); font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .ctemp { text-align:right; font-size:18px; line-height:1.15; }
  .cunit { color:var(--secondary-text-color); font-size:11px; white-space:nowrap; }
`;
