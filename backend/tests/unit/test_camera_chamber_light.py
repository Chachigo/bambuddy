"""Per-printer camera_chamber_light: light while a viewer watches the camera."""

from types import SimpleNamespace

import pytest

from backend.app.api.routes import camera as camera_module


@pytest.fixture
def light(monkeypatch):
    """Fake MQTT client whose chamber_light state tracks what was pushed."""
    state = SimpleNamespace(chamber_light=False)
    pushed: list[bool] = []

    def set_chamber_light(on: bool) -> bool:
        pushed.append(on)
        state.chamber_light = on
        return True

    client = SimpleNamespace(state=state, set_chamber_light=set_chamber_light)
    monkeypatch.setattr(
        "backend.app.services.printer_manager.printer_manager.get_client",
        lambda pid: client,
    )
    camera_module._camera_lit_printers.discard(1)
    yield SimpleNamespace(state=state, pushed=pushed)
    camera_module._camera_lit_printers.discard(1)


def test_on_for_the_viewer_off_when_they_leave(light):
    camera_module._camera_light_on(1)
    camera_module._camera_light_off(1, keep_on=False)
    assert light.pushed == [True, False]


def test_a_light_already_on_is_left_alone(light):
    light.state.chamber_light = True
    camera_module._camera_light_on(1)
    camera_module._camera_light_off(1, keep_on=False)
    assert light.pushed == []


def test_a_running_print_keeps_the_light(light):
    camera_module._camera_light_on(1)
    camera_module._camera_light_off(1, keep_on=True)
    assert light.pushed == [True]


def test_second_viewer_does_not_re_push(light):
    camera_module._camera_light_on(1)
    camera_module._camera_light_on(1)
    assert light.pushed == [True]
