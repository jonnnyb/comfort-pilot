"""Tests for the panel's WebSocket API."""

from __future__ import annotations

import json
import os

from pathlib import Path
import re

from custom_components.comfort_pilot.const import OPT_RULES, VERSION

from .test_integration import BEDROOM, OFFICE, make_entry, setup, world  # noqa: F401


async def _ws(client, msg_id, **msg):
    await client.send_json({"id": msg_id, **msg})
    return await client.receive_json()


async def test_get_payload(hass, world, hass_ws_client):
    await setup(hass, make_entry())
    client = await hass_ws_client(hass)
    res = await _ws(client, 1, type="comfort_pilot/get")
    assert res["success"], res
    p = res["result"]
    assert p["house"]["mode"] == "auto"
    assert p["house"]["solar_budget"] > 0
    office = next(r for r in p["rooms"] if r["id"] == OFFICE)
    assert office["decision"]["level"] == "comfort"
    assert office["config"]["cool_min"] == 23.0
    assert office["state"]["occupied"] is True
    assert p["settings"]["hold_minutes"] == 120
    assert p["settings"]["quiet_start"] == "22:00"
    if os.environ.get("CP_DUMP_PAYLOAD"):
        with open(os.environ["CP_DUMP_PAYLOAD"], "w") as f:
            json.dump(p, f, indent=1, default=str)


async def test_controls(hass, world, hass_ws_client):
    c = await setup(hass, make_entry())
    client = await hass_ws_client(hass)
    res = await _ws(client, 1, type="comfort_pilot/set_house_mode", mode="away")
    assert res["success"] and res["result"]["house"]["mode"] == "away"
    assert hass.states.get(OFFICE).state == "off"
    res = await _ws(client, 2, type="comfort_pilot/set_house_mode", mode="sideways")
    assert not res["success"]
    res = await _ws(client, 3, type="comfort_pilot/set_room_mode", room=BEDROOM, mode="eco")
    assert res["success"] and c.room_modes[BEDROOM] == "eco"
    res = await _ws(client, 4, type="comfort_pilot/set_fresh_air", on=True)
    assert res["success"] and c.fresh_air
    c.holds[OFFICE] = c.holds.get(OFFICE) or __import__("homeassistant.util.dt", fromlist=["now"]).now()
    res = await _ws(client, 5, type="comfort_pilot/resume", room=OFFICE)
    assert res["success"] and OFFICE not in c.holds


async def test_save_room_applies_live(hass, world, hass_ws_client):
    entry = make_entry()
    c = await setup(hass, entry)
    client = await hass_ws_client(hass)
    cfg = {"name": "Study", "occupancy_sensors": ["binary_sensor.office_motion"], "window_sensors": [],
           "cool_min": 22, "cool_max": 25, "heat_min": 18, "heat_max": 21, "priority": "high",
           "est_power": 750, "occupancy_timeout": 15}
    res = await _ws(client, 1, type="comfort_pilot/save_room", room=OFFICE, config=cfg)
    assert res["success"], res
    await hass.async_block_till_done()
    # Same coordinator object: applied live, no reload.
    assert entry.runtime_data is c
    assert c.rooms[OFFICE].cool_min == 22 and c.rooms[OFFICE].name == "Study"
    assert hass.states.get(OFFICE).attributes["temperature"] == 22
    from homeassistant.helpers import device_registry as dr
    device = dr.async_get(hass).async_get_device(identifiers={("comfort_pilot", f"{entry.entry_id}_{OFFICE}")})
    assert device.name == "Comfort Pilot Study"

    bad = {**cfg, "cool_min": 27}
    res = await _ws(client, 2, type="comfort_pilot/save_room", room=OFFICE, config=bad)
    assert not res["success"] and "wrong way round" in res["error"]["message"]


async def test_rules_crud(hass, world, hass_ws_client):
    entry = make_entry()
    c = await setup(hass, entry)
    client = await hass_ws_client(hass)
    rule = {"room": BEDROOM, "name": "Bedroom night", "days": [0, 1, 2, 3, 4, 5, 6],
            "start": "21:00", "end": "06:30", "action": "comfort", "target": 24}
    res = await _ws(client, 1, type="comfort_pilot/save_rule", rule=rule)
    assert res["success"], res
    saved = res["result"]["rules"][0]
    assert saved["description"] == "Bedroom night: keep comfortable at 24°, every day 21:00–06:30"
    assert entry.options[OPT_RULES][0]["start"] == "21:00:00"
    assert len(c.rules) == 1

    res = await _ws(client, 2, type="comfort_pilot/save_rule", rule={**rule, "id": saved["id"], "enabled": False, "action": "off"})
    assert res["success"]
    assert len(entry.options[OPT_RULES]) == 1
    assert entry.options[OPT_RULES][0]["enabled"] is False
    assert entry.options[OPT_RULES][0]["target"] is None

    res = await _ws(client, 3, type="comfort_pilot/save_rule", rule={**rule, "end": "21:00"})
    assert not res["success"]
    res = await _ws(client, 4, type="comfort_pilot/save_rule", rule={**rule, "days": []})
    assert not res["success"]

    res = await _ws(client, 5, type="comfort_pilot/delete_rule", rule_id=saved["id"])
    assert res["success"] and res["result"]["rules"] == []
    assert c.rules == []


async def test_save_settings(hass, world, hass_ws_client):
    c = await setup(hass, make_entry())
    client = await hass_ws_client(hass)
    res = await _ws(client, 1, type="comfort_pilot/get")
    settings = res["result"]["settings"]
    settings.update(safety_max=30, hold_minutes=60, quiet_start="23:00")
    res = await _ws(client, 2, type="comfort_pilot/save_settings", settings=settings)
    assert res["success"], res
    assert c.settings.safety_max == 30 and c.hold_minutes == 60
    assert c.quiet_start.hour == 23
    res = await _ws(client, 3, type="comfort_pilot/save_settings", settings={"safety_max": 99})
    assert not res["success"]


async def test_save_sources_reloads(hass, world, hass_ws_client):
    entry = make_entry()
    c = await setup(hass, entry)
    client = await hass_ws_client(hass)
    sources = {"climates": [OFFICE], "grid_sensor": "sensor.grid_power", "weather": "weather.home",
               "solar_sensor": "", "notify": "notify.mobile_app_phone"}
    res = await _ws(client, 1, type="comfort_pilot/save_sources", sources=sources)
    assert res["success"], res
    await hass.async_block_till_done()
    assert entry.runtime_data is not c
    assert list(entry.runtime_data.rooms) == [OFFICE]
    assert "solar_sensor" not in entry.data
    res = await _ws(client, 2, type="comfort_pilot/save_sources", sources={**sources, "weather": None})
    assert not res["success"] and "weather" in res["error"]["message"]


async def test_non_admin_cannot_change_config(hass, world, hass_ws_client, hass_read_only_access_token):
    await setup(hass, make_entry())
    client = await hass_ws_client(hass, hass_read_only_access_token)
    res = await _ws(client, 1, type="comfort_pilot/delete_rule", rule_id="x")
    assert not res["success"] and res["error"]["code"] == "unauthorized"
    res = await _ws(client, 2, type="comfort_pilot/get")
    assert res["success"]


async def test_panel_registered_and_removed(hass, world):
    from unittest.mock import AsyncMock, MagicMock, patch

    from custom_components.comfort_pilot import panel

    hass.http = MagicMock()
    hass.http.async_register_static_paths = AsyncMock()
    with patch("homeassistant.components.panel_custom.async_register_panel", AsyncMock()) as reg, \
         patch("homeassistant.components.frontend.async_remove_panel") as remove, \
         patch("homeassistant.components.frontend.add_extra_js_url") as add_js, \
         patch("homeassistant.components.frontend.remove_extra_js_url") as remove_js:
        entry = make_entry()
        await setup(hass, entry)
        reg.assert_awaited_once()
        kwargs = reg.await_args.kwargs
        assert kwargs["frontend_url_path"] == "comfort-pilot"
        assert kwargs["webcomponent_name"] == "comfort-pilot-panel"
        assert kwargs["module_url"].startswith("/comfort_pilot_static/comfort-pilot-panel.js?v=")
        static = hass.http.async_register_static_paths.await_args.args[0][0]
        assert static.path.endswith("frontend")
        assert add_js.call_args.args[1] == f"/comfort_pilot_static/comfort-pilot-card.js?v={VERSION}"
        assert await hass.config_entries.async_unload(entry.entry_id)
        remove.assert_called_once()
        remove_js.assert_called_once()
    hass.http = None
    hass.data.pop(panel.STATIC_KEY, None)


async def test_overview_command(hass, world, hass_ws_client, hass_read_only_access_token):
    await setup(hass, make_entry())
    client = await hass_ws_client(hass, hass_read_only_access_token)
    res = await _ws(client, 1, type="comfort_pilot/overview")
    assert res["success"], res
    assert set(res["result"]) == {"house", "rooms", "choices"}
    assert res["result"]["rooms"][0]["id"] == OFFICE
    # Non-admins can still use the card's buttons.
    res = await _ws(client, 2, type="comfort_pilot/set_room_mode", room=OFFICE, mode="eco")
    assert res["success"]


def test_js_imports_pin_current_version():
    """Browsers cache the shared module by URL, so every import must carry the release version."""
    frontend = Path(__file__).parent.parent / "custom_components/comfort_pilot/frontend"
    for js in frontend.glob("*.js"):
        for ver in re.findall(r"comfort-pilot-common\.js\?v=([\d.]+)", js.read_text()):
            assert ver == VERSION, f"{js.name} imports common v{ver}, expected {VERSION}"
