"""The auto_chamber_light setting: light on at print start, off after the end."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from backend.app import main as main_module
from backend.app.api.routes import settings as settings_module


@asynccontextmanager
async def _fake_session():
    yield SimpleNamespace()


@pytest.fixture
def light(monkeypatch):
    """Stub out the DB session, the setting and the MQTT client; record the pushes."""
    monkeypatch.setattr(main_module, "async_session", _fake_session)
    monkeypatch.setattr(main_module, "_LIGHT_OFF_GRACE_SECONDS", 0)

    pushed: list[bool] = []
    client = SimpleNamespace(set_chamber_light=pushed.append)
    monkeypatch.setattr(main_module.printer_manager, "get_client", lambda pid: client)

    def enable(value: str | None):
        async def _get_setting(db, key):
            return value if key == "auto_chamber_light" else None

        monkeypatch.setattr(settings_module, "get_setting", _get_setting)

    return SimpleNamespace(pushed=pushed, enable=enable)


@pytest.mark.asyncio
async def test_follows_the_print_when_enabled(light):
    light.enable("true")
    await main_module._auto_chamber_light(1, True)
    await main_module._auto_chamber_light(1, False)
    assert light.pushed == [True, False]


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [None, "false"])
async def test_noop_when_disabled(light, value):
    light.enable(value)
    await main_module._auto_chamber_light(1, True)
    await main_module._auto_chamber_light(1, False)
    assert light.pushed == []
