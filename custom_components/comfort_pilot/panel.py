"""Sidebar panel registration."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

from .const import DOMAIN, NAME, VERSION

_LOGGER = logging.getLogger(__name__)

PANEL_URL_PATH = "comfort-pilot"
STATIC_URL = "/comfort_pilot_static"
STATIC_KEY = f"{DOMAIN}_static_registered"
PANEL_KEY = f"{DOMAIN}_panel_registered"
WS_KEY = f"{DOMAIN}_ws_registered"


async def async_register_panel(hass: HomeAssistant) -> None:
    """Serve the panel JS and add Comfort Pilot to the sidebar."""
    if hass.http is None:  # e.g. in tests without the web server
        return
    try:
        if not hass.data.get(STATIC_KEY):
            from homeassistant.components.http import StaticPathConfig

            await hass.http.async_register_static_paths(
                [StaticPathConfig(STATIC_URL, str(Path(__file__).parent / "frontend"), False)]
            )
            hass.data[STATIC_KEY] = True
        if not hass.data.get(PANEL_KEY):
            from homeassistant.components import panel_custom

            await panel_custom.async_register_panel(
                hass,
                frontend_url_path=PANEL_URL_PATH,
                webcomponent_name="comfort-pilot-panel",
                sidebar_title=NAME,
                sidebar_icon="mdi:home-thermometer",
                module_url=f"{STATIC_URL}/comfort-pilot-panel.js?v={VERSION}",
                require_admin=False,
                config={},
            )
            hass.data[PANEL_KEY] = True
    except Exception as err:  # noqa: BLE001 - the integration still works without the panel
        _LOGGER.warning("Could not add the Comfort Pilot panel to the sidebar: %s", err)


def async_unregister_panel(hass: HomeAssistant) -> None:
    if hass.data.pop(PANEL_KEY, None):
        from homeassistant.components import frontend

        frontend.async_remove_panel(hass, PANEL_URL_PATH, warn_if_unknown=False)
