/* Comfort Pilot dashboard card + visual editor. Loaded on every page by the integration. */

import {
  BASE_STYLES, actionMessage, esc, fireMoreInfo, renderHouse, renderHouseCompact, renderRoomRow, renderRoomTile,
} from "./comfort-pilot-common.js?v=0.4.1";

const LAYOUTS = {
  full: { label: "Full", help: "House controls and a tile per room, like the panel's Overview" },
  compact: { label: "Compact", help: "House mode on one line, then a short row per room" },
  room: { label: "Single room", help: "One room's tile – handy on a room dashboard" },
};
const DEFAULTS = {
  full: { show_house: true, show_stats: true },
  compact: { show_house: true, show_stats: false },
  room: { show_house: false, show_stats: false },
};

function withDefaults(config) {
  const layout = LAYOUTS[config.layout] ? config.layout : "compact";
  return { ...DEFAULTS[layout], ...config, layout };
}

// --------------------------------------------------------------------- card

class ComfortPilotCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._data = null;
    this._error = null;
    this._sig = "";
    this.shadowRoot.addEventListener("click", (e) => this._onClick(e));
    this.shadowRoot.addEventListener("change", (e) => this._onChange(e));
  }

  static getConfigElement() {
    return document.createElement("comfort-pilot-card-editor");
  }

  static getStubConfig() {
    return { layout: "compact" };
  }

  setConfig(config) {
    if (!config) throw new Error("Invalid configuration");
    const c = withDefaults(config);
    if (c.layout === "room" && !c.room && this._data) {
      c.room = (this._data.rooms[0] || {}).id;
    }
    this._config = c;
    this._render();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    // Refresh when anything Comfort Pilot shows changes (its own entities or the splits).
    const ids = Object.keys(hass.states).filter((id) => id.includes("comfort_pilot"));
    if (this._data) ids.push(...this._data.rooms.map((r) => r.id));
    const sig = ids.map((id) => (hass.states[id] ? hass.states[id].last_updated : "")).join("|");
    if (first || sig !== this._sig) {
      this._sig = sig;
      this._scheduleLoad(first ? 0 : 800);
    }
  }

  connectedCallback() {
    this._timer = setInterval(() => this._load(), 60000);
  }

  disconnectedCallback() {
    clearInterval(this._timer);
  }

  _scheduleLoad(delay) {
    clearTimeout(this._loadTimer);
    this._loadTimer = setTimeout(() => this._load(), delay);
  }

  async _load() {
    if (!this._hass) return;
    try {
      this._data = await this._hass.callWS({ type: "comfort_pilot/overview" });
      this._error = null;
    } catch (err) {
      this._error = err.message || String(err);
    }
    this._render();
  }

  async _send(msg) {
    if (!msg) return;
    try {
      const result = await this._hass.callWS(msg);
      if (result && result.rooms) this._data = { ...this._data, house: result.house, rooms: result.rooms };
    } catch (err) {
      this._error = err.message || String(err);
    }
    this._render();
  }

  _onClick(e) {
    const el = e.target.closest("[data-action]");
    if (!el || el.tagName === "SELECT") return;
    if (el.dataset.action === "more-info") {
      fireMoreInfo(this, el.dataset.entity);
      return;
    }
    this._send(actionMessage(el, this._data));
  }

  _onChange(e) {
    const el = e.target;
    if (el.tagName === "SELECT" && el.dataset.action) this._send(actionMessage(el, this._data));
  }

  _rooms() {
    const d = this._data;
    const c = this._config;
    if (c.layout === "room") {
      const id = c.room || (d.rooms[0] || {}).id;
      return d.rooms.filter((r) => r.id === id);
    }
    if (Array.isArray(c.rooms) && c.rooms.length) {
      return c.rooms.map((id) => d.rooms.find((r) => r.id === id)).filter(Boolean);
    }
    return d.rooms;
  }

  _render() {
    if (!this._config) return;
    const c = this._config;
    let body;
    if (!this._data) {
      body = this._error
        ? `<div class="msg">Comfort Pilot isn't available: ${esc(this._error)}</div>`
        : `<div class="msg muted">Loading…</div>`;
    } else {
      const rooms = this._rooms();
      const title = c.title ? `<div class="title">${esc(c.title)}</div>` : "";
      if (c.layout === "full") {
        body = `${title}
          ${c.show_house ? `<div class="section">${renderHouse(this._data, { stats: c.show_stats })}</div>` : ""}
          <div class="grid tiles">${rooms.map((r) => `<div class="box">${renderRoomTile(r)}</div>`).join("")}</div>`;
      } else if (c.layout === "compact") {
        body = `${title}
          ${c.show_house ? renderHouseCompact(this._data, { stats: c.show_stats }) : ""}
          <div class="rows ${c.show_house || c.title ? "" : "flush"}">${rooms.map((r) => renderRoomRow(r)).join("")}</div>`;
      } else {
        const room = rooms[0];
        body = `${title}
          ${c.show_house ? `<div class="section">${renderHouseCompact(this._data, { stats: c.show_stats })}</div>` : ""}
          ${room ? renderRoomTile(room) : `<div class="msg muted">Pick a room in the card settings.</div>`}`;
      }
      if (this._error) body += `<div class="msg error">${esc(this._error)}</div>`;
    }
    this.shadowRoot.innerHTML = `<style>${BASE_STYLES}${CARD_STYLES}</style>
      <ha-card><div class="wrap ${esc(c.layout)}">${body}</div></ha-card>`;
  }

  getCardSize() {
    const c = this._config || {};
    const n = this._data ? this._rooms().length : 3;
    if (c.layout === "room") return 3 + (c.show_house ? 1 : 0);
    if (c.layout === "compact") return 1 + n + (c.show_house ? 1 : 0) + (c.show_stats ? 2 : 0);
    return (c.show_house ? 3 : 0) + (c.show_stats ? 1 : 0) + Math.ceil(n / 2) * 3;
  }

  getGridOptions() {
    const c = this._config || {};
    if (c.layout === "room") return { columns: 6, min_columns: 4 };
    if (c.layout === "compact") return { columns: 12, min_columns: 6 };
    return { columns: "full", min_columns: 12 };
  }
}

const CARD_STYLES = `
  :host { display:block; }
  ha-card { display:block; height:100%; box-sizing:border-box; }
  .wrap { padding:16px; color:var(--primary-text-color); }
  .title { font-size:18px; font-weight:500; margin-bottom:12px; }
  .section { margin-bottom:16px; }
  .tiles { grid-template-columns:repeat(auto-fill, minmax(220px, 1fr)); gap:12px; }
  .box { border:1px solid var(--divider-color); border-radius:var(--ha-card-border-radius, 12px); padding:12px; }
  .rows { margin-top:8px; }
  .rows.flush { margin-top:-10px; }
  .rows.flush .crow:first-child { border-top:none; }
  .compact .stats { margin-top:10px; }
  .compact .banner { margin-top:8px; }
  .msg { padding:4px 0; font-size:14px; }
  .msg.error { color:var(--error-color, #c62828); font-size:12px; margin-top:8px; }
`;

// ------------------------------------------------------------------- editor

class ComfortPilotCardEditor extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._rooms = null;
    this.shadowRoot.addEventListener("click", (e) => this._onClick(e));
    this.shadowRoot.addEventListener("change", (e) => this._onChange(e));
    this.shadowRoot.addEventListener("input", (e) => this._onInput(e));
  }

  setConfig(config) {
    const incoming = JSON.stringify(config);
    this._config = { ...config };
    // Don't redraw (and steal focus) when HA echoes back what we just sent.
    if (incoming !== this._lastSent) this._render();
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._rooms && !this._loading) {
      this._loading = true;
      hass.callWS({ type: "comfort_pilot/overview" })
        .then((d) => { this._rooms = d.rooms.map((r) => ({ id: r.id, name: r.name })); })
        .catch(() => { this._rooms = []; })
        .finally(() => { this._loading = false; this._render(); });
    }
  }

  _emit(config) {
    // Drop values that match the layout's defaults, so the YAML stays short.
    const clean = { ...config };
    const defs = DEFAULTS[clean.layout] || {};
    for (const k of ["show_house", "show_stats"]) if (clean[k] === defs[k]) delete clean[k];
    if (clean.layout !== "room") delete clean.room;
    if (clean.layout === "room" || !(clean.rooms || []).length) delete clean.rooms;
    if (!clean.title) delete clean.title;
    this._config = clean;
    this._lastSent = JSON.stringify(clean);
    this.dispatchEvent(new CustomEvent("config-changed", { detail: { config: clean }, bubbles: true, composed: true }));
  }

  _onClick(e) {
    const el = e.target.closest("[data-set]");
    if (!el) return;
    const c = withDefaults(this._config || {});
    const key = el.dataset.set;
    if (key === "layout") {
      const layout = el.dataset.value;
      const next = { ...this._config, layout };
      delete next.show_house;
      delete next.show_stats;
      if (layout === "room" && !next.room && this._rooms && this._rooms[0]) next.room = this._rooms[0].id;
      this._emit(next);
    } else if (key === "room-toggle") {
      const all = (this._rooms || []).map((r) => r.id);
      const current = (c.rooms && c.rooms.length) ? c.rooms : all;
      const set = new Set(current);
      set.has(el.dataset.value) ? set.delete(el.dataset.value) : set.add(el.dataset.value);
      // Keep the original room order; ticking everything means "all rooms".
      const chosen = all.filter((id) => set.has(id));
      this._emit({ ...this._config, rooms: chosen.length === all.length ? [] : chosen });
    } else if (key === "show_house" || key === "show_stats") {
      this._emit({ ...this._config, [key]: !c[key] });
    }
    this._render();
  }

  _onChange(e) {
    const el = e.target;
    if (el.dataset.field === "room") {
      this._emit({ ...this._config, room: el.value });
      this._render();
    }
  }

  _onInput(e) {
    const el = e.target;
    if (el.dataset.field === "title") this._emit({ ...this._config, title: el.value });
  }

  _render() {
    if (!this._config) return;
    const c = withDefaults(this._config);
    const rooms = this._rooms;
    const layouts = Object.entries(LAYOUTS).map(([k, v]) => `
      <button class="option ${c.layout === k ? "on" : ""}" data-set="layout" data-value="${k}">
        <span class="radio"></span><span><strong>${v.label}</strong><br><span class="muted small">${v.help}</span></span>
      </button>`).join("");
    let roomPart = `<div class="muted small">Loading rooms…</div>`;
    if (rooms && !rooms.length) roomPart = `<div class="muted small">Couldn't load rooms – is Comfort Pilot set up?</div>`;
    else if (rooms && c.layout === "room") {
      roomPart = `<label class="field"><span>Room</span><select data-field="room">
        ${rooms.map((r) => `<option value="${esc(r.id)}" ${c.room === r.id ? "selected" : ""}>${esc(r.name)}</option>`).join("")}</select></label>`;
    } else if (rooms) {
      const shown = c.rooms && c.rooms.length ? new Set(c.rooms) : new Set(rooms.map((r) => r.id));
      roomPart = `<div class="field"><span>Rooms to show</span><div class="picks">
        ${rooms.map((r) => `<button class="pick ${shown.has(r.id) ? "on" : ""}" data-set="room-toggle" data-value="${esc(r.id)}">
          <span class="tick">${shown.has(r.id) ? "✓" : ""}</span>${esc(r.name)}</button>`).join("")}</div></div>`;
    }
    const toggle = (key, label, help) => `
      <button class="toggle ${c[key] ? "on" : ""}" data-set="${key}" role="switch" aria-checked="${!!c[key]}">
        <span class="knob"></span><span><span>${label}</span><br><span class="muted small">${help}</span></span></button>`;
    this.shadowRoot.innerHTML = `<style>${EDITOR_STYLES}</style>
      <div class="field"><span>Layout</span><div class="options">${layouts}</div></div>
      ${roomPart}
      ${toggle("show_house", "House controls", "House mode and fresh air")}
      ${toggle("show_stats", "Energy and weather", "Solar, grid, outside, forecast and season")}
      <label class="field"><span>Title (optional)</span><input type="text" data-field="title" value="${esc(c.title || "")}" placeholder="e.g. Air conditioning"></label>`;
  }
}

const EDITOR_STYLES = `
  :host { display:block; color:var(--primary-text-color); }
  .muted { color:var(--secondary-text-color); }
  .small { font-size:12px; }
  .field { display:flex; flex-direction:column; gap:6px; margin-bottom:16px; font-size:14px; }
  .field > span:first-child { color:var(--secondary-text-color); font-size:12px; }
  .options { display:flex; flex-direction:column; gap:6px; }
  .option, .pick, .toggle { display:flex; align-items:center; gap:10px; text-align:left; background:none; color:var(--primary-text-color);
    font:inherit; font-size:14px; cursor:pointer; border:1px solid var(--divider-color); border-radius:8px; padding:10px 12px; }
  .option.on { border-color:var(--primary-color); background:rgba(3,169,244,.06); }
  .radio { width:16px; height:16px; border-radius:50%; border:2px solid var(--secondary-text-color); flex:none; box-sizing:border-box; }
  .option.on .radio { border:5px solid var(--primary-color); }
  .picks { display:flex; flex-direction:column; border:1px solid var(--divider-color); border-radius:8px; overflow:hidden; }
  .pick { border:none; border-radius:0; border-bottom:1px solid var(--divider-color); padding:8px 12px; }
  .pick:last-child { border-bottom:none; }
  .tick { width:18px; height:18px; border:1px solid var(--divider-color); border-radius:4px; display:inline-flex; align-items:center;
    justify-content:center; color:var(--primary-color); font-size:13px; flex:none; }
  .pick.on .tick { border-color:var(--primary-color); }
  .toggle { border:none; padding:6px 0; margin-bottom:12px; width:100%; }
  .toggle .knob { width:36px; height:20px; border-radius:10px; background:var(--disabled-color, #bdbdbd); position:relative; flex:none; transition:background .2s; }
  .toggle .knob::after { content:""; position:absolute; top:2px; left:2px; width:16px; height:16px; border-radius:50%; background:#fff; transition:transform .2s; }
  .toggle.on .knob { background:var(--primary-color); }
  .toggle.on .knob::after { transform:translateX(16px); }
  select, input { font:inherit; font-size:14px; padding:8px 10px; border-radius:6px; border:1px solid var(--divider-color);
    background:var(--secondary-background-color, transparent); color:var(--primary-text-color); width:100%; box-sizing:border-box; }
`;

if (!customElements.get("comfort-pilot-card")) customElements.define("comfort-pilot-card", ComfortPilotCard);
if (!customElements.get("comfort-pilot-card-editor")) customElements.define("comfort-pilot-card-editor", ComfortPilotCardEditor);

window.customCards = window.customCards || [];
if (!window.customCards.some((c) => c.type === "comfort-pilot-card")) {
  window.customCards.push({
    type: "comfort-pilot-card",
    name: "Comfort Pilot",
    description: "Comfort Pilot's overview: house mode, fresh air and what each room is doing.",
    preview: true,
    documentationURL: "https://github.com/jonnnyb/comfort-pilot",
  });
}
