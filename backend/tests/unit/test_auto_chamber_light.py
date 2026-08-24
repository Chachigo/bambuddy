"""Per-printer auto_chamber_light: light on at print start, off after the end."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from backend.app import main as main_module


@pytest.fixture
def light(monkeypatch):
    """Stub out the DB session and the MQTT client; record the pushes."""
    monkeypatch.setattr(main_module, "_LIGHT_OFF_GRACE_SECONDS", 0)

    pushed: list[bool] = []
    client = SimpleNamespace(set_chamber_light=pushed.append)
    monkeypatch.setattr(main_module.printer_manager, "get_client", lambda pid: client)

    def enable(value: bool | None):
        @asynccontextmanager
        async def _session():
            async def _scalar(stmt):
                return value

            yield SimpleNamespace(scalar=_scalar)

        monkeypatch.setattr(main_module, "async_session", _session)

    return SimpleNamespace(pushed=pushed, enable=enable)


@pytest.mark.asyncio
async def test_follows_the_print_when_enabled(light):
    light.enable(True)
    await main_module._auto_chamber_light(1, True)
    await main_module._auto_chamber_light(1, False)
    assert light.pushed == [True, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, False])
async def test_noop_when_disabled(light, value):
    light.enable(value)
    await main_module._auto_chamber_light(1, True)
    await main_module._auto_chamber_light(1, False)
    assert light.pushed == []
