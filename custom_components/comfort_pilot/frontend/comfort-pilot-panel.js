/* Comfort Pilot sidebar panel. Plain web component, no external dependencies. */

import {
  BASE_STYLES, LEVEL_LABELS, actionMessage, esc, fireMoreInfo, fmtT, fmtW, renderHouse, renderRoomTile, when,
} from "./comfort-pilot-common.js?v=0.4.1";

const TABS = [
  ["overview", "Overview"],
  ["rooms", "Rooms"],
  ["rules", "Rules"],
  ["settings", "Settings"],
  ["log", "Log"],
];
const LOG_CATS = [
  ["all", "All"], ["action", "Actions"], ["decision", "Decisions"], ["occupancy", "Occupancy"],
  ["manual", "Manual changes"], ["notify", "Notifications"], ["mode", "Modes"], ["system", "System"], ["error", "Errors"],
];
const CAT_LABELS = Object.fromEntries(LOG_CATS);
const DETAIL_LABELS = {
  level: "Level", room_temp: "Room temp", occupied: "Occupied", solar_budget: "Solar budget",
  grid_power: "Grid", outside: "Outside", forecast_high: "Forecast high", service: "Service", data: "Sent",
};
const ACTION_LABELS = { comfort: "Keep comfortable", eco: "Economy only", off: "Keep off" };
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const OCC_CLASSES = ["motion", "occupancy", "presence"];
const WINDOW_CLASSES = ["window", "door", "opening", "garage_door"];

const SETTINGS_GROUPS = [
  ["Seasons", "Decided each day from the forecast high.", [
    ["cooling_season_above", "Cooling season when the forecast high is at least", "°C", 0.5],
    ["heating_season_below", "Heating season when the forecast high is at most", "°C", 0.5],
  ]],
  ["Starting and stopping", "So a unit doesn't come on for a room that's already comfortable.", [
    ["start_margin", "Start cooling once a room is this far above its setpoint (heating: below)", "°C", 0.5],
    ["stop_margin", "Stop cooling once a room is this far below its setpoint (heating: above)", "°C", 0.5],
  ]],
  ["Solar", "How eagerly Comfort Pilot uses spare solar.", [
    ["solar_margin", "Grid import tolerated before a room drops out of comfort", "W", 50],
    ["precondition_hours", "Pre-cool on spare solar this long before a rule starts", "h", 0.5],
    ["hot_day", "On a hot day (forecast at least) pre-cool 2 hours earlier", "°C", 0.5],
  ]],
  ["Safety", "Applies even when nobody is home.", [
    ["safety_max", "Always cool a room hotter than", "°C", 0.5],
    ["safety_min", "Always heat a room colder than", "°C", 0.5],
  ]],
  ["Fresh air", "Notifications suggesting the evap or open windows.", [
    ["fresh_air_delta", "Suggest when outside is cooler than inside by", "°C", 0.5],
    ["fresh_air_min_indoor", "…and inside is at least", "°C", 0.5],
    ["quiet_start", "No notifications from", "time"],
    ["quiet_end", "…until", "time"],
  ]],
  ["Protecting the units", "Avoids switching a split on and off too often. Windows, fresh air, Off and safety still act straight away.", [
    ["min_run_minutes", "Once on, keep a unit running for at least", "min", 1],
    ["min_off_minutes", "Once off, keep a unit off for at least", "min", 1],
  ]],
  ["Manual changes", "When you change a unit yourself.", [
    ["hold_minutes", "Leave the room alone for", "min", 10],
  ]],
];

const clone = (o) => JSON.parse(JSON.stringify(o));

class ComfortPilotPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._tab = "overview";
    this._data = null;
    this._error = null;
    this._toast = null;
    this._editRoom = null;
    this._roomDraft = null;
    this._ruleDraft = null;
    this._settingsDraft = null;
    this._sourcesDraft = null;
    this._busy = false;
    this._logRoom = "all";
    this._logCat = "all";
    this._logOpen = new Set();
    this.shadowRoot.addEventListener("click", (e) => this._onClick(e));
    this.shadowRoot.addEventListener("input", (e) => this._onInput(e));
    this.shadowRoot.addEventListener("change", (e) => this._onInput(e));
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) this._load();
  }
  get hass() { return this._hass; }
  set narrow(v) { this._narrow = v; if (this._data) this._render(); }
  set panel(v) { this._panel = v; }

  connectedCallback() {
    this._timer = setInterval(() => {
      if (!this._editing()) this._load(true);
    }, 15000);
    if (this._hass && !this._data) this._load();
    this._render();
  }
  disconnectedCallback() { clearInterval(this._timer); }

  _editing() {
    return Boolean(this._roomDraft || this._ruleDraft || this._settingsDraft || this._sourcesDraft);
  }

  async _load(quiet = false) {
    if (!this._hass) return;
    try {
      this._data = await this._hass.callWS({ type: "comfort_pilot/get" });
      this._error = null;
    } catch (err) {
      this._error = err.message || String(err);
    }
    if (!quiet || !this._editing()) this._render();
  }

  async _ws(msg, okText) {
    this._busy = true;
    this._render();
    try {
      const result = await this._hass.callWS(msg);
      if (result && result.rooms) this._data = result;
      if (okText) this._showToast(okText);
      return true;
    } catch (err) {
      this._showToast(err.message || String(err), true);
      return false;
    } finally {
      this._busy = false;
      this._render();
    }
  }

  _showToast(text, error = false) {
    this._toast = { text, error };
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => { this._toast = null; this._render(); }, error ? 6000 : 2500);
  }

  // ------------------------------------------------------------- events

  async _onClick(e) {
    const el = e.target.closest("[data-action]");
    if (!el || this._busy) return;
    const a = el.dataset.action;
    const v = el.dataset.value;
    const room = el.dataset.room;
    switch (a) {
      case "menu":
        this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true }));
        break;
      case "tab":
        if (this._editing() && !confirm("Discard unsaved changes?")) return;
        this._clearDrafts();
        this._tab = v;
        this._render();
        break;
      case "more-info":
        fireMoreInfo(this, el.dataset.entity);
        break;
      case "house-mode":
      case "fresh-air":
      case "room-mode":
      case "resume":
        await this._ws(actionMessage(el, this._data), a === "resume" ? "Back on auto" : undefined);
        break;
      case "log-cat":
        this._logCat = v;
        this._render();
        break;
      case "log-toggle":
        this._logOpen.has(v) ? this._logOpen.delete(v) : this._logOpen.add(v);
        this._render();
        break;
      case "toggle-rule-room": {
        const set = new Set(this._ruleDraft.rooms);
        set.has(v) ? set.delete(v) : set.add(v);
        this._ruleDraft.rooms = [...set];
        this._render();
        break;
      }
      case "edit-room": {
        const r = this._data.rooms.find((x) => x.id === room);
        this._editRoom = room;
        this._roomDraft = clone(r.config);
        this._render();
        break;
      }
      case "cancel":
        this._clearDrafts();
        this._render();
        break;
      case "save-room":
        if (await this._ws({ type: "comfort_pilot/save_room", room: this._editRoom, config: this._roomDraft }, "Room saved")) {
          this._clearDrafts();
          this._render();
        }
        break;
      case "toggle-list": {
        const key = el.dataset.key;
        const list = new Set(this._roomDraft[key] || []);
        list.has(v) ? list.delete(v) : list.add(v);
        this._roomDraft[key] = [...list];
        this._render();
        break;
      }
      case "new-rule":
        this._ruleDraft = {
          rooms: [], name: "", action: "comfort",
          days: [0, 1, 2, 3, 4, 5, 6], start: "21:00", end: "06:30", target: 24, enabled: true,
        };
        this._render();
        break;
      case "edit-rule": {
        const r = this._data.rules.find((x) => x.id === v);
        this._ruleDraft = clone({
          id: r.id, rooms: r.rooms, name: r.name, action: r.action, days: r.days,
          start: r.start, end: r.end, target: r.target, enabled: r.enabled, source: r.source,
        });
        this._render();
        break;
      }
      case "toggle-day": {
        const d = Number(v);
        const days = new Set(this._ruleDraft.days);
        days.has(d) ? days.delete(d) : days.add(d);
        this._ruleDraft.days = [...days].sort();
        this._render();
        break;
      }
      case "day-preset":
        this._ruleDraft.days = { all: [0, 1, 2, 3, 4, 5, 6], weekdays: [0, 1, 2, 3, 4], weekends: [5, 6] }[v];
        this._render();
        break;
      case "rule-action":
        this._ruleDraft.action = v;
        this._render();
        break;
      case "save-rule": {
        const rule = { ...this._ruleDraft };
        const hasTarget = rule.action === "comfort" && rule.target !== "" && rule.target !== null && rule.target !== undefined;
        rule.target = hasTarget ? Number(rule.target) : null;
        if (!rule.rooms.length) {
          this._showToast("Pick at least one room", true);
          this._render();
          break;
        }
        if (!rule.name || !rule.name.trim()) {
          const names = rule.rooms.map((id) => (this._data.rooms.find((x) => x.id === id) || {}).name || id);
          rule.name = `${names.length > 2 ? `${names.length} rooms` : names.join(" & ")} – ${ACTION_LABELS[rule.action].toLowerCase()}`;
        }
        if (await this._ws({ type: "comfort_pilot/save_rule", rule }, "Rule saved")) {
          this._ruleDraft = null;
          this._render();
        }
        break;
      }
      case "toggle-rule": {
        const r = this._data.rules.find((x) => x.id === v);
        await this._ws({
          type: "comfort_pilot/save_rule",
          rule: { id: r.id, rooms: r.rooms, name: r.name, action: r.action, days: r.days, start: r.start, end: r.end, target: r.target, enabled: !r.enabled, source: r.source },
        }, r.enabled ? "Rule paused" : "Rule enabled");
        break;
      }
      case "delete-rule": {
        const r = this._data.rules.find((x) => x.id === v);
        if (confirm(`Delete "${r.name}"?`)) await this._ws({ type: "comfort_pilot/delete_rule", rule_id: v }, "Rule deleted");
        break;
      }
      case "edit-settings":
        this._settingsDraft = clone(this._data.settings);
        this._render();
        break;
      case "save-settings":
        if (await this._ws({ type: "comfort_pilot/save_settings", settings: this._settingsDraft }, "Settings saved")) {
          this._settingsDraft = null;
          this._render();
        }
        break;
      case "edit-sources":
        this._sourcesDraft = clone(this._data.sources);
        this._sourcesDraft.climates = this._sourcesDraft.climates || [];
        this._render();
        break;
      case "toggle-climate": {
        const set = new Set(this._sourcesDraft.climates);
        set.has(v) ? set.delete(v) : set.add(v);
        this._sourcesDraft.climates = [...set];
        this._render();
        break;
      }
      case "save-sources": {
        const ok = await this._ws({ type: "comfort_pilot/save_sources", sources: this._sourcesDraft }, "Saved – restarting Comfort Pilot");
        if (ok) {
          this._sourcesDraft = null;
          setTimeout(() => this._load(), 2500);
        }
        break;
      }
    }
  }

  _onInput(e) {
    const el = e.target;
    if (el.dataset.logRoom !== undefined) {
      this._logRoom = el.value;
      this._render();
      return;
    }
    const field = el.dataset.field;
    if (!field) return;
    const [scope, key] = field.split(".");
    let value = el.type === "checkbox" ? el.checked : el.value;
    if (el.type === "number") value = value === "" ? "" : Number(value);
    const draft = { room: this._roomDraft, rule: this._ruleDraft, settings: this._settingsDraft, sources: this._sourcesDraft }[scope];
    if (draft) draft[key] = value;
  }

  _clearDrafts() {
    this._editRoom = null;
    this._roomDraft = null;
    this._ruleDraft = null;
    this._settingsDraft = null;
    this._sourcesDraft = null;
  }

  // ---------------------------------------------------------- helpers

  _name(entityId) {
    const s = this._hass && this._hass.states[entityId];
    return (s && s.attributes.friendly_name) || entityId;
  }

  _entities(domain, filter) {
    const states = (this._hass && this._hass.states) || {};
    return Object.keys(states)
      .filter((id) => id.startsWith(domain + ".") && (!filter || filter(states[id])))
      .sort((a, b) => this._name(a).localeCompare(this._name(b)));
  }

  _areaOf(entityId) {
    const ent = this._hass.entities && this._hass.entities[entityId];
    if (!ent) return null;
    if (ent.area_id) return ent.area_id;
    const dev = ent.device_id && this._hass.devices && this._hass.devices[ent.device_id];
    return dev ? dev.area_id : null;
  }

  // ----------------------------------------------------------- render

  _render() {
    const d = this._data;
    let body;
    if (this._error && !d) {
      body = `<div class="card empty"><h3>Can't reach Comfort Pilot</h3><p>${esc(this._error)}</p>
        <p class="muted">Check the integration is set up under Settings → Devices &amp; services.</p></div>`;
    } else if (!d) {
      body = `<div class="card empty"><p class="muted">Loading…</p></div>`;
    } else {
      body = {
        overview: () => this._overview(d),
        rooms: () => this._rooms(d),
        rules: () => this._rules(d),
        settings: () => this._settings(d),
        log: () => this._log(d),
      }[this._tab]();
    }
    const tabs = TABS.map(([k, label]) =>
      `<button class="tab ${k === this._tab ? "active" : ""}" data-action="tab" data-value="${k}">${label}</button>`).join("");
    this.shadowRoot.innerHTML = `<style>${BASE_STYLES}${STYLES}</style>
      <div class="toolbar">
        ${this._narrow ? `<button class="icon" data-action="menu" aria-label="Menu">☰</button>` : ""}
        <div class="title">Comfort Pilot</div>
      </div>
      <nav class="tabs">${tabs}</nav>
      <main class="${this._busy ? "busy" : ""}">${body}</main>
      ${this._toast ? `<div class="toast ${this._toast.error ? "error" : ""}">${esc(this._toast.text)}</div>` : ""}`;
  }

  // Overview -----------------------------------------------------------

  _overview(d) {
    const house = `<section class="card">${renderHouse(d)}</section>`;
    const rooms = d.rooms.map((r) => `<section class="card">${renderRoomTile(r)}</section>`).join("");
    return house + `<div class="grid">${rooms}</div>`;
  }

  // Rooms --------------------------------------------------------------

  _rooms(d) {
    return `<p class="intro">Each room works within a comfort band. With spare solar it sits at the comfortable end; on grid power, the economy end.</p>` +
      d.rooms.map((r) => (this._editRoom === r.id ? this._roomForm(r) : this._roomSummary(r))).join("");
  }

  _roomSummary(r) {
    const c = r.config;
    const occ = (c.occupancy_sensors || []).map((e) => esc(this._name(e))).join(", ") || "none";
    const win = (c.window_sensors || []).map((e) => esc(this._name(e))).join(", ") || "none";
    return `<section class="card">
      <div class="row space">
        <h3>${esc(r.name)}</h3>
        <button class="btn" data-action="edit-room" data-room="${esc(r.id)}" ${this._editing() ? "disabled" : ""}>Edit</button>
      </div>
      <div class="facts">
        <div><span class="label">Cooling</span> ${fmtT(c.cool_min)} – ${fmtT(c.cool_max)}</div>
        <div><span class="label">Heating</span> ${fmtT(c.heat_min)} – ${fmtT(c.heat_max)}</div>
        <div><span class="label">Solar priority</span> ${esc(c.priority)}</div>
        <div><span class="label">Typical power</span> ${fmtW(c.est_power)}</div>
        <div><span class="label">Occupied after</span> ${c.occupancy_delay ? `${esc(c.occupancy_delay)} min of motion` : "first motion"}</div>
        <div><span class="label">Stays occupied</span> ${esc(c.occupancy_timeout)} min after last motion</div>
        <div><span class="label">Occupancy</span> ${occ}</div>
        <div><span class="label">Windows</span> ${win}</div>
      </div>
    </section>`;
  }

  _sensorPicker(key, classes, roomId) {
    const selected = new Set(this._roomDraft[key] || []);
    const area = this._areaOf(roomId);
    const ids = this._entities("binary_sensor", (s) => classes.includes(s.attributes.device_class) || selected.has(s.entity_id));
    ids.sort((a, b) => {
      const sa = selected.has(a) ? 0 : area && this._areaOf(a) === area ? 1 : 2;
      const sb = selected.has(b) ? 0 : area && this._areaOf(b) === area ? 1 : 2;
      return sa - sb;
    });
    if (!ids.length) return `<div class="muted small">No matching sensors found.</div>`;
    return `<div class="picker">${ids.map((id) => {
      const st = this._hass.states[id];
      const live = st ? (st.state === "on" ? "on" : "off") : "";
      return `<button class="pick ${selected.has(id) ? "on" : ""}" data-action="toggle-list" data-key="${key}" data-value="${esc(id)}">
        <span class="tick">${selected.has(id) ? "✓" : ""}</span>
        <span class="pick-name">${esc(this._name(id))}<span class="muted small"> ${esc(id)}</span></span>
        <span class="muted small">${live}</span></button>`;
    }).join("")}</div>`;
  }

  _num(scope, key, value, step, unit, min, max) {
    return `<div class="num"><input type="number" data-field="${scope}.${key}" value="${esc(value)}" step="${step}"
      ${min !== undefined ? `min="${min}"` : ""} ${max !== undefined ? `max="${max}"` : ""}><span class="unit-label">${unit}</span></div>`;
  }

  _roomForm(r) {
    const c = this._roomDraft;
    const pri = ["high", "normal", "low"].map((p) => `<option value="${p}" ${c.priority === p ? "selected" : ""}>${p[0].toUpperCase() + p.slice(1)}</option>`).join("");
    return `<section class="card editing">
      <h3>Edit ${esc(r.name)}</h3>
      <label class="field"><span>Name</span><input type="text" data-field="room.name" value="${esc(c.name)}"></label>
      <div class="two">
        <div class="field"><span>Cooling band</span>
          <div class="band">${this._num("room", "cool_min", c.cool_min, 0.5, "°C", 10, 35)}<span class="to">to</span>${this._num("room", "cool_max", c.cool_max, 0.5, "°C", 10, 35)}</div>
          <div class="muted small">Comfortable end → economy end</div></div>
        <div class="field"><span>Heating band</span>
          <div class="band">${this._num("room", "heat_min", c.heat_min, 0.5, "°C", 10, 35)}<span class="to">to</span>${this._num("room", "heat_max", c.heat_max, 0.5, "°C", 10, 35)}</div>
          <div class="muted small">Economy end → comfortable end</div></div>
      </div>
      <div class="three">
        <label class="field"><span>Priority for spare solar</span><select data-field="room.priority">${pri}</select></label>
        <div class="field"><span>Typical power while running</span>${this._num("room", "est_power", c.est_power, 50, "W", 100, 5000)}</div>
      </div>
      <div class="two">
        <div class="field"><span>Confirm occupancy after</span>${this._num("room", "occupancy_delay", c.occupancy_delay ?? 3, 1, "min", 0, 30)}
          <div class="muted small">Motion must continue this long before the AC starts, so walking through doesn't trigger it. 0 = straight away.</div></div>
        <div class="field"><span>Stay occupied after last motion</span>${this._num("room", "occupancy_timeout", c.occupancy_timeout, 1, "min", 1, 240)}
          <div class="muted small">Covers sitting still. Presence sensors use 5 minutes at most.</div></div>
      </div>
      <div class="field"><span>Motion / presence sensors</span>${this._sensorPicker("occupancy_sensors", OCC_CLASSES, r.id)}</div>
      <div class="field"><span>Window sensors</span>
        <div class="muted small">The unit turns off if any is open for 2 minutes.</div>
        ${this._sensorPicker("window_sensors", WINDOW_CLASSES, r.id)}</div>
      <div class="actions">
        <button class="btn" data-action="cancel">Cancel</button>
        <button class="btn primary" data-action="save-room">Save</button>
      </div>
    </section>`;
  }

  // Rules --------------------------------------------------------------

  _rules(d) {
    const form = this._ruleDraft ? this._ruleForm(d) : "";
    const add = this._ruleDraft ? "" : `<button class="btn primary" data-action="new-rule">Add rule</button>`;
    const order = Object.fromEntries(d.rooms.map((r, i) => [r.id, i]));
    const sorted = [...d.rules].sort((a, b) => (order[a.rooms[0]] ?? 99) - (order[b.rooms[0]] ?? 99) || a.start.localeCompare(b.start));
    const byRoom = sorted.length ? `<section class="card">${sorted.map((r) => this._ruleRow(r)).join("")}</section>` : "";
    const none = d.rules.length ? "" : `<section class="card empty"><p>No rules yet.</p>
      <p class="muted">Motion sensors can't see people asleep, so start with one for each bedroom:<br>
      <strong>Keep comfortable, every day, 21:00 – 06:30, 24°</strong>. One rule can cover all the bedrooms.</p></section>`;
    return `<div class="row space"><p class="intro">Rules cover what sensors can't see. Times that end earlier than they start run past midnight.</p>${add}</div>${form}${none}${byRoom}`;
  }

  _ruleRow(r) {
    const what = r.action === "comfort" && r.target !== null && r.target !== undefined
      ? `${ACTION_LABELS[r.action]} at ${fmtT(r.target)}` : ACTION_LABELS[r.action];
    return `<div class="rule ${r.enabled ? "" : "disabled"}">
      <button class="switch mini ${r.enabled ? "on" : ""}" data-action="toggle-rule" data-value="${esc(r.id)}" role="switch" aria-checked="${r.enabled}" aria-label="Enabled"><span class="knob"></span></button>
      <div class="rule-text">
        <div><strong>${esc(r.name)}</strong> ${r.active && r.enabled ? `<span class="badge comfort">Active now</span>` : ""}</div>
        <div class="room-tags">${(r.room_names || []).map((n) => `<span class="tag">${esc(n)}</span>`).join("")}</div>
        <div class="muted small">${esc(what)} · ${esc(r.days_text)} · ${esc(r.start)} – ${esc(r.end)}</div>
      </div>
      <button class="btn" data-action="edit-rule" data-value="${esc(r.id)}" ${this._editing() ? "disabled" : ""}>Edit</button>
      <button class="btn danger" data-action="delete-rule" data-value="${esc(r.id)}" ${this._editing() ? "disabled" : ""}>Delete</button>
    </div>`;
  }

  _ruleForm(d) {
    const r = this._ruleDraft;
    const rooms = d.rooms.map((x) =>
      `<button class="chip ${r.rooms.includes(x.id) ? "on" : ""}" data-action="toggle-rule-room" data-value="${esc(x.id)}">${esc(x.name)}</button>`).join("");
    const actions = Object.keys(ACTION_LABELS).map((a) =>
      `<button class="chip ${r.action === a ? "on" : ""}" data-action="rule-action" data-value="${a}">${ACTION_LABELS[a]}</button>`).join("");
    const days = DAYS.map((name, i) =>
      `<button class="chip small ${r.days.includes(i) ? "on" : ""}" data-action="toggle-day" data-value="${i}">${name}</button>`).join("");
    const target = r.action === "comfort"
      ? `<div class="field"><span>Fixed temperature (optional)</span>
          ${this._num("rule", "target", r.target ?? "", 0.5, "°C", 16, 30)}
          <div class="muted small">Leave empty to let solar choose between the comfortable and economy ends.</div></div>`
      : "";
    return `<section class="card editing">
      <h3>${r.id ? "Edit rule" : "New rule"}</h3>
      <div class="field"><span>Rooms</span><div class="chips">${rooms}</div></div>
      <label class="field"><span>Name</span><input type="text" data-field="rule.name" value="${esc(r.name)}" placeholder="e.g. Bedrooms at night"></label>
      <div class="field"><span>What to do</span><div class="chips">${actions}</div></div>
      <div class="field"><span>Days</span><div class="chips">${days}
        <span class="sep"></span>
        <button class="link" data-action="day-preset" data-value="all">Every day</button>
        <button class="link" data-action="day-preset" data-value="weekdays">Weekdays</button>
        <button class="link" data-action="day-preset" data-value="weekends">Weekends</button></div></div>
      <div class="two">
        <label class="field"><span>From</span><input type="time" data-field="rule.start" value="${esc(r.start)}"></label>
        <label class="field"><span>Until</span><input type="time" data-field="rule.end" value="${esc(r.end)}"></label>
      </div>
      ${target}
      <div class="actions">
        <button class="btn" data-action="cancel">Cancel</button>
        <button class="btn primary" data-action="save-rule">Save rule</button>
      </div>
    </section>`;
  }

  // Settings -----------------------------------------------------------

  _settings(d) {
    const editing = Boolean(this._settingsDraft);
    const s = this._settingsDraft || d.settings;
    const groups = SETTINGS_GROUPS.map(([title, help, fields]) => `
      <div class="group"><h4>${title}</h4><div class="muted small">${help}</div>
      ${fields.map(([key, label, unit, step]) => {
        const value = s[key];
        let input;
        if (!editing) input = `<span class="value">${unit === "time" ? esc(value) : `${esc(value)} ${unit}`}</span>`;
        else if (unit === "time") input = `<input type="time" data-field="settings.${key}" value="${esc(value)}">`;
        else input = this._num("settings", key, value, step, unit);
        return `<div class="setting"><span>${label}</span>${input}</div>`;
      }).join("")}</div>`).join("");
    const actions = editing
      ? `<div class="actions"><button class="btn" data-action="cancel">Cancel</button><button class="btn primary" data-action="save-settings">Save settings</button></div>`
      : `<div class="actions"><button class="btn primary" data-action="edit-settings" ${this._editing() ? "disabled" : ""}>Edit settings</button></div>`;
    return `<section class="card">${groups}${actions}</section>${this._sources(d)}`;
  }

  _sources(d) {
    const editing = Boolean(this._sourcesDraft);
    const s = this._sourcesDraft || d.sources;
    if (!editing) {
      const line = (label, id) => `<div class="setting"><span>${label}</span><span class="value">${id ? esc(this._name(id)) : "–"}</span></div>`;
      return `<section class="card"><h3>Split systems &amp; data sources</h3>
        <div class="setting"><span>Split systems</span><span class="value">${(s.climates || []).map((c) => esc(this._name(c))).join(", ")}</span></div>
        ${line("Grid power", s.grid_sensor)}${line("Solar production", s.solar_sensor)}
        ${line("Weather", s.weather)}${line("Outdoor temperature", s.outdoor_sensor)}
        <div class="setting"><span>Notifications</span><span class="value">${esc(s.notify || "Home Assistant notifications")}</span></div>
        <div class="actions"><button class="btn" data-action="edit-sources" ${this._editing() ? "disabled" : ""}>Change</button></div></section>`;
    }
    const climates = this._entities("climate").filter((id) => !id.startsWith("climate.comfort_pilot"));
    const select = (key, ids, optional) => `<select data-field="sources.${key}">
      ${optional ? `<option value="">None</option>` : ""}
      ${ids.map((id) => `<option value="${esc(id)}" ${s[key] === id ? "selected" : ""}>${esc(this._name(id))}</option>`).join("")}</select>`;
    const power = this._entities("sensor", (st) => st.attributes.device_class === "power" || ["W", "kW"].includes(st.attributes.unit_of_measurement));
    const temps = this._entities("sensor", (st) => st.attributes.device_class === "temperature");
    const weather = this._entities("weather");
    const notifyServices = Object.keys((this._hass.services || {}).notify || {})
      .filter((n) => !["send_message", "persistent_notification"].includes(n)).map((n) => `notify.${n}`);
    return `<section class="card editing"><h3>Split systems &amp; data sources</h3>
      <div class="muted small">Saving restarts Comfort Pilot (takes a second). Rooms for new splits are detected automatically.</div>
      <div class="field"><span>Split systems</span><div class="picker">${climates.map((id) => `
        <button class="pick ${s.climates.includes(id) ? "on" : ""}" data-action="toggle-climate" data-value="${esc(id)}">
          <span class="tick">${s.climates.includes(id) ? "✓" : ""}</span><span class="pick-name">${esc(this._name(id))}<span class="muted small"> ${esc(id)}</span></span></button>`).join("")}</div></div>
      <div class="two">
        <label class="field"><span>Grid power (negative when exporting)</span>${select("grid_sensor", power, false)}</label>
        <label class="field"><span>Solar production (optional)</span>${select("solar_sensor", power, true)}</label>
        <label class="field"><span>Weather</span>${select("weather", weather, true)}</label>
        <label class="field"><span>Outdoor temperature (optional)</span>${select("outdoor_sensor", temps, true)}</label>
      </div>
      <label class="field"><span>Notifications</span>
        <select data-field="sources.notify"><option value="">Home Assistant notifications</option>
        ${notifyServices.map((n) => `<option value="${esc(n)}" ${s.notify === n ? "selected" : ""}>${esc(n)}</option>`).join("")}</select></label>
      <div class="actions"><button class="btn" data-action="cancel">Cancel</button><button class="btn primary" data-action="save-sources">Save</button></div>
    </section>`;
  }

  // Log ----------------------------------------------------------------

  _log(d) {
    const rooms = [["all", "All rooms"], ["house", "House only"], ...d.rooms.map((r) => [r.id, r.name])];
    const roomSel = `<select data-log-room>${rooms.map(([v, n]) => `<option value="${esc(v)}" ${this._logRoom === v ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>`;
    const cats = LOG_CATS.map(([k, n]) =>
      `<button class="chip small ${this._logCat === k ? "on" : ""}" data-action="log-cat" data-value="${k}">${n}</button>`).join("");
    const entries = (d.log || []).filter((e) =>
      (this._logCat === "all" || e.category === this._logCat) &&
      (this._logRoom === "all" || (this._logRoom === "house" ? !e.room : e.room === this._logRoom)));
    const rows = entries.map((e) => {
      const id = `${e.at}|${e.category}|${e.room || ""}|${e.message}`;
      const open = this._logOpen.has(id);
      const details = e.details ? Object.entries(e.details).filter(([, v]) => v !== null && v !== undefined).map(([k, v]) => {
        let val = v;
        if (["room_temp", "outside", "forecast_high"].includes(k)) val = fmtT(v);
        else if (["solar_budget", "grid_power"].includes(k)) val = fmtW(v);
        else if (typeof v === "boolean") val = v ? "Yes" : "No";
        else if (typeof v === "object") val = JSON.stringify(v);
        return `<span class="detail"><span class="muted">${esc(DETAIL_LABELS[k] || k)}</span> ${esc(val)}</span>`;
      }).join("") : "";
      return `<div class="log-row ${details ? "has-details" : ""}" ${details ? `data-action="log-toggle" data-value="${esc(id)}"` : ""}>
        <div class="log-time">${esc(when(e.at))}</div>
        <span class="cat ${esc(e.category)}">${esc(CAT_LABELS[e.category] || e.category)}</span>
        <div class="log-msg">${e.room_name ? `<strong>${esc(e.room_name)}</strong> · ` : ""}${esc(e.message)}
          ${open && details ? `<div class="details">${details}</div>` : ""}</div>
        ${details ? `<span class="chev">${open ? "▾" : "▸"}</span>` : ""}
      </div>`;
    }).join("");
    return `<p class="intro">What Comfort Pilot decided, what it sent to the units, and why. Click a line for the readings at the time.
      Updates every 15 seconds. ${d.override_count} manual change${d.override_count === 1 ? "" : "s"} recorded so far for learning.</p>
      <section class="card">
        <div class="row space log-filters"><div class="chips">${cats}</div>${roomSel}</div>
        ${rows || `<p class="muted">Nothing logged ${this._logCat === "all" && this._logRoom === "all" ? "yet" : "for this filter"}.</p>`}
      </section>`;
  }
}

const STYLES = `
  :host { display:block; min-height:100vh; background:var(--primary-background-color); color:var(--primary-text-color);
    font-family:var(--paper-font-body1_-_font-family, Roboto, sans-serif); --cp-radius:var(--ha-card-border-radius, 12px); }
  .toolbar { display:flex; align-items:center; gap:8px; height:56px; padding:0 16px; background:var(--app-header-background-color, var(--primary-color));
    color:var(--app-header-text-color, var(--text-primary-color, #fff)); }
  .toolbar .title { font-size:20px; font-weight:400; }
  .icon { background:none; border:none; color:inherit; font-size:22px; cursor:pointer; padding:8px; }
  .tabs { display:flex; gap:4px; padding:0 12px; background:var(--app-header-background-color, var(--primary-color)); overflow-x:auto; }
  .tab { background:none; border:none; color:var(--app-header-text-color, #fff); opacity:.75; padding:12px 14px; font-size:14px; cursor:pointer;
    border-bottom:2px solid transparent; white-space:nowrap; font-family:inherit; }
  .tab.active { opacity:1; border-bottom-color:var(--app-header-text-color, #fff); }
  main { max-width:1100px; margin:0 auto; padding:16px; box-sizing:border-box; }
  main.busy { opacity:.6; pointer-events:none; }
  .card { background:var(--card-background-color, #fff); border-radius:var(--cp-radius); padding:16px; margin-bottom:16px;
    box-shadow:var(--ha-card-box-shadow, 0 1px 3px rgba(0,0,0,.12)); border:1px solid var(--divider-color, transparent); box-sizing:border-box; }
  .card.editing { border:1px solid var(--primary-color); }
  .card.empty { text-align:center; }
  h3 { margin:0 0 8px; font-size:18px; font-weight:500; }
  h4 { margin:16px 0 2px; font-size:15px; font-weight:500; }
  .group:first-child h4 { margin-top:0; }
  .intro { color:var(--secondary-text-color); margin:4px 0 16px; line-height:1.5; }
  .grid .card { margin-bottom:0; }
  .tab:focus-visible, .chip:focus-visible, .btn:focus-visible { outline:2px solid var(--primary-color); outline-offset:2px; }
  .room-tags { display:flex; flex-wrap:wrap; gap:4px; margin:4px 0 2px; }
  .tag { font-size:11px; padding:1px 8px; border-radius:10px; border:1px solid var(--divider-color); color:var(--secondary-text-color); }
  .log-filters { margin-bottom:8px; }
  .log-filters select { width:auto; min-width:160px; }
  .log-row { display:grid; grid-template-columns:130px 110px 1fr 16px; gap:10px; align-items:start; padding:8px 4px;
    border-top:1px solid var(--divider-color); font-size:14px; }
  .log-row.has-details { cursor:pointer; }
  .log-row.has-details:hover { background:rgba(127,127,127,.06); }
  .log-time { color:var(--secondary-text-color); font-size:13px; white-space:nowrap; }
  .log-msg { line-height:1.4; word-break:break-word; }
  .chev { color:var(--secondary-text-color); }
  .details { display:flex; flex-wrap:wrap; gap:4px 14px; margin-top:6px; font-size:12px; }
  .cat { font-size:11px; padding:2px 8px; border-radius:10px; background:var(--secondary-background-color, #eee); justify-self:start; white-space:nowrap; }
  .cat.action { background:rgba(3,169,244,.15); color:#0288d1; }
  .cat.decision { background:rgba(103,58,183,.13); color:#7e57c2; }
  .cat.occupancy { background:rgba(67,160,71,.15); color:var(--success-color, #2e7d32); }
  .cat.manual { background:rgba(33,150,243,.15); color:#1e88e5; }
  .cat.notify { background:rgba(255,152,0,.15); color:var(--warning-color, #e65100); }
  .cat.error { background:rgba(244,67,54,.15); color:var(--error-color, #c62828); }
  @media (max-width: 600px) {
    .log-row { grid-template-columns:1fr auto; }
    .log-row .log-msg { grid-column:1 / -1; }
    .log-row .chev { display:none; }
  }
  .facts { display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:8px 16px; font-size:14px; }
  .facts .label { display:inline; margin-right:4px; }
  .btn { border:1px solid var(--divider-color); background:none; color:var(--primary-color); border-radius:8px; padding:8px 14px; cursor:pointer;
    font-size:14px; font-family:inherit; font-weight:500; }
  .btn.primary { background:var(--primary-color); color:var(--text-primary-color, #fff); border-color:var(--primary-color); }
  .btn.danger { color:var(--error-color, #c62828); }
  .btn:disabled { opacity:.4; cursor:default; }
  .sep { width:8px; }
  .actions { display:flex; justify-content:flex-end; gap:8px; margin-top:16px; }
  .field { display:flex; flex-direction:column; gap:4px; margin-top:12px; font-size:14px; }
  .field > span:first-child { color:var(--secondary-text-color); font-size:12px; }
  .two, .three { display:grid; gap:0 16px; }
  .two { grid-template-columns:repeat(auto-fit, minmax(240px, 1fr)); }
  .three { grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); }
  input, select { font:inherit; font-size:14px; padding:8px 10px; border-radius:6px; border:1px solid var(--divider-color);
    background:var(--secondary-background-color, transparent); color:var(--primary-text-color); box-sizing:border-box; }
  input[type=text], select { width:100%; }
  .num { display:flex; align-items:center; gap:6px; }
  .num input { width:90px; }
  .unit-label { color:var(--secondary-text-color); font-size:13px; min-width:24px; }
  .band { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .to { color:var(--secondary-text-color); font-size:13px; }
  .picker { max-height:220px; overflow-y:auto; border:1px solid var(--divider-color); border-radius:8px; }
  .pick { display:flex; align-items:center; gap:10px; width:100%; text-align:left; background:none; border:none; border-bottom:1px solid var(--divider-color);
    padding:8px 10px; cursor:pointer; color:var(--primary-text-color); font:inherit; font-size:14px; }
  .pick:last-child { border-bottom:none; }
  .pick.on { background:rgba(3,169,244,.08); }
  .pick-name { flex:1; }
  .tick { width:18px; height:18px; border:1px solid var(--divider-color); border-radius:4px; display:inline-flex; align-items:center;
    justify-content:center; color:var(--primary-color); font-size:13px; flex:none; }
  .pick.on .tick { border-color:var(--primary-color); }
  .rule { display:flex; align-items:center; gap:12px; padding:10px 0; border-top:1px solid var(--divider-color); flex-wrap:wrap; }
  .rule:first-of-type { border-top:none; }
  .rule.disabled .rule-text { opacity:.5; }
  .rule-text { flex:1; min-width:200px; }
  .switch.mini { padding:0; }
  .setting { display:flex; justify-content:space-between; align-items:center; gap:16px; padding:8px 0; border-bottom:1px solid var(--divider-color); font-size:14px; flex-wrap:wrap; }
  .setting .value { font-weight:500; }
  .table-wrap { overflow-x:auto; }
  table { width:100%; border-collapse:collapse; font-size:14px; }
  th { text-align:left; color:var(--secondary-text-color); font-weight:500; font-size:12px; padding:6px 8px; border-bottom:1px solid var(--divider-color); }
  td { padding:8px; border-bottom:1px solid var(--divider-color); white-space:nowrap; text-transform:none; }
  .toast { position:fixed; bottom:24px; left:50%; transform:translateX(-50%); background:#323232; color:#fff; padding:12px 20px; border-radius:8px;
    font-size:14px; box-shadow:0 2px 8px rgba(0,0,0,.3); z-index:10; max-width:90vw; }
  .toast.error { background:var(--error-color, #c62828); }
`;

customElements.define("comfort-pilot-panel", ComfortPilotPanel);
