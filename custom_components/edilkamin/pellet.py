"""Durable, per-entry pellet estimate; no polling or device commands."""

import asyncio
from copy import deepcopy
import json
import math
from pathlib import Path

from homeassistant.helpers.storage import Store
import voluptuous as vol

ENABLED = "pellet_estimate_enabled"
COEFFICIENTS = tuple(f"pellet_p{power}_kg_h" for power in range(1, 6))


def coefficient(value):
    """Accept a positive finite rate, never bool/zero; blank means unknown."""
    message = "Enter a positive finite kg/h value"
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise vol.Invalid(message)
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as err:
        raise vol.Invalid(message) from err
    if not math.isfinite(result) or result <= 0:
        raise vol.Invalid(message)
    return result


def settings(options):
    return {
        ENABLED: options.get(ENABLED, False) is True,
        **{key: coefficient(options.get(key)) for key in COEFFICIENTS},
    }


class PelletEstimate:
    """Persist amount and baselines together, before exposing each update.

    All mutations are serialized, including option boundaries and unload.
    Missing readings retain the last valid baseline. A settings change instead
    drops all baselines: the first subsequent reading is explicitly unestimated.
    """

    def __init__(self, hass, entry_id):
        self.hass = hass
        self.store = Store(hass, 1, f"edilkamin_pellet_{entry_id}")
        self.lock = asyncio.Lock()
        self.closed = False
        self.state = {
            "kg": 0.0,
            "previous": [None] * 5,
            "settings": settings({}),
            "reasons": [],
        }

    @property
    def enabled(self):
        return self.state["settings"][ENABLED]

    @property
    def incomplete(self):
        return bool(self.state["reasons"])

    async def async_load(self, options):
        """Restore using HA Store; invalid storage must not fabricate a total."""
        # Store normally renames malformed JSON and returns None. For an
        # accumulator that would silently lose history, so fail before loading.
        existing = await self.hass.async_add_executor_job(self._read_existing)
        stored = await self.store.async_load()
        if existing is not None and stored is None:
            message = "Invalid pellet estimate storage; restore backup"
            raise ValueError(message)
        if stored is not None:
            # Store is private to this version; fail setup on corrupt content.
            if (
                not isinstance(stored, dict)
                or type(stored.get("kg")) not in (int, float)
                or not math.isfinite(stored["kg"])
                or stored["kg"] < 0
                or not isinstance(stored.get("previous"), list)
                or len(stored["previous"]) != 5
                or any(
                    v is not None and (type(v) is not int or v < 0)
                    for v in stored["previous"]
                )
                or not isinstance(stored.get("reasons"), list)
                or not all(isinstance(v, str) for v in stored["reasons"])
                or not isinstance(stored.get("settings"), dict)
            ):
                message = "Invalid pellet estimate storage; restore backup"
                raise ValueError(message)
            settings(stored["settings"])
            self.state = stored
        await self.async_configure(options)

    def _read_existing(self):
        try:
            with Path(self.store.path).open(encoding="utf-8") as stored:
                existing = json.load(stored)
        except FileNotFoundError:
            return None
        if (
            not isinstance(existing, dict)
            or not existing
            or existing.get("data") is None
        ):
            message = "Invalid pellet estimate storage; restore backup"
            raise ValueError(message)
        return existing

    async def _commit(self, state):
        # Keep the transaction lock until disk work finishes even if HA cancels
        # the refresh. A detached executor write must not race the next runtime.
        task = asyncio.create_task(self._write_and_commit(state))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _write_and_commit(self, state):
        # A failed disk write leaves the previous in-memory transaction intact.
        await self.store.async_save(state)
        # HA Store logs and swallows WriteError. Confirm the transaction reached
        # disk before publishing it, rather than relying on async_save's return.
        await self.hass.async_add_executor_job(self._verify_saved, state)
        self.state = state

    def _verify_saved(self, expected):
        with Path(self.store.path).open(encoding="utf-8") as stored:
            actual = json.load(stored)
        if actual.get("data") != expected:
            message = "Pellet estimate storage write was not persisted"
            raise OSError(message)

    async def async_configure(self, options):
        """Apply settings via an explicit, conservative unestimated boundary."""
        new = settings(options)
        async with self.lock:
            if self.closed or new == self.state["settings"]:
                return
            state = deepcopy(self.state)
            if self.enabled and new[ENABLED]:
                state["reasons"] = sorted(set(state["reasons"]) | {"calibration_gap"})
            state["settings"] = new
            state["previous"] = [None] * 5
            await self._commit(state)

    async def async_update(self, coordinator):  # noqa: C901
        # One transaction intentionally keeps all five baselines and kg atomic.
        """Consume only validated cumulative hours from this device coordinator."""
        async with self.lock:
            if self.closed or not self.enabled:
                return
            state = deepcopy(self.state)
            reasons = set(state["reasons"])
            for index, key in enumerate(COEFFICIENTS):
                current = coordinator.get_working_hours(index + 1)
                previous = state["previous"][index]
                if current is None:
                    continue
                state["previous"][index] = current
                if previous is None:
                    continue
                delta = current - previous
                if delta < 0:
                    reasons.add(f"p{index + 1}_counter_reset")
                elif delta > 0:
                    rate = state["settings"][key]
                    if rate is None:
                        reasons.add(f"p{index + 1}_uncalibrated")
                    else:
                        try:
                            total = state["kg"] + delta * rate
                        except OverflowError:
                            total = math.inf
                        if math.isfinite(total):
                            state["kg"] = total
                        else:
                            reasons.add("numeric_overflow")
            state["reasons"] = sorted(reasons)
            if state != self.state:
                await self._commit(state)

    async def async_flush(self):
        """Wait for any in-flight transaction before releasing this runtime."""
        async with self.lock:
            self.closed = True  # Ignore late coordinator responses after unload.
