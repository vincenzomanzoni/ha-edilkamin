"""The Edilkamin integration."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.helpers import device_registry as dr

from custom_components.edilkamin.api.edilkamin_async_api import EdilkaminAsyncApi

from .const import DOMAIN, MAC_ADDRESS, PASSWORD, USERNAME
from .coordinator import EdilkaminCoordinator
from .pellet import PelletEstimate

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.FAN,
    Platform.SENSOR,
    Platform.SWITCH,
]


_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Edilkamin from a config entry."""

    mac_address = entry.data[MAC_ADDRESS]
    username = entry.data[USERNAME]
    password = entry.data[PASSWORD]

    coordinator = EdilkaminCoordinator(hass, username, password, mac_address)

    coordinator.pellet = PelletEstimate(hass, entry.entry_id)
    await coordinator.pellet.async_load(entry.options)

    # First refresh
    await coordinator.async_refresh()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = EdilkaminAsyncApi(
        mac_address=mac_address,
        username=username,
        password=password,
        hass=hass,
        token_provider=coordinator.refresh_token,
    )
    hass.data[DOMAIN].setdefault("coordinators", {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    register_device(hass, entry, mac_address)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator = hass.data[DOMAIN]["coordinators"].pop(entry.entry_id)
        await coordinator.async_shutdown()
        await coordinator.pellet.async_flush()
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def async_update_options(hass, entry):
    """Persist the option boundary before reloading entities."""
    coordinator = hass.data[DOMAIN]["coordinators"][entry.entry_id]
    await coordinator.pellet.async_configure(entry.options)
    await hass.config_entries.async_reload(entry.entry_id)


def register_device(hass: HomeAssistant, config_entry, mac_address):
    """Register a device in the Home Assistant device registry."""
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=config_entry.entry_id,
        identifiers={("edilkamin", mac_address)},
        manufacturer="Edilkamin",
        name="Edilkamin Stove",
        model="The Mind",
        suggested_area="Living Room",
        connections={(dr.CONNECTION_NETWORK_MAC, mac_address)},
    )
