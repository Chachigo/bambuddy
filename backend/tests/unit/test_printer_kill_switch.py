from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.app import main as main_module


@pytest.fixture(autouse=True)
def clear_kill_switch_state():
    main_module._unauthorized_print_kill_sent.clear()
    main_module._kill_switch_notification_tasks.clear()
    main_module._expected_prints.clear()
    main_module._active_prints.clear()
    main_module._expected_print_registered_at.clear()
    main_module._printer_reconciled_since_connect.clear()
    yield
    for task in main_module._kill_switch_notification_tasks.values():
        if not task.done():
            task.cancel()
    main_module._unauthorized_print_kill_sent.clear()
    main_module._kill_switch_notification_tasks.clear()
    main_module._expected_prints.clear()
    main_module._active_prints.clear()
    main_module._expected_print_registered_at.clear()
    main_module._printer_reconciled_since_connect.clear()


def test_gcode_3mf_status_filename_matches_registered_expected_print():
    state = SimpleNamespace(
        current_print=None,
        subtask_name="",
        gcode_file="foreign_job.gcode.3mf",
    )

    keys = main_module._build_status_print_keys(7, state)

    assert (7, "foreign_job.gcode.3mf") in keys
    assert (7, "foreign_job.gcode") in keys


@pytest.mark.asyncio
async def test_unauthorized_active_print_triggers_stop(monkeypatch):
    stop_calls: list[int] = []
    broadcast = AsyncMock()
    provider_notification = AsyncMock(return_value=True)

    async def fake_status(*args, **kwargs):
        return None

    async def kill_switch_enabled(_db):
        return True

    async def unauthorized(*_args):
        return False

    monkeypatch.setattr(main_module.printer_manager, "get_current_print_user", lambda printer_id: None)
    monkeypatch.setattr(
        main_module.printer_manager, "stop_print", lambda printer_id: stop_calls.append(printer_id) or True
    )
    monkeypatch.setattr(main_module.printer_manager, "get_printer", lambda printer_id: None)
    monkeypatch.setattr(main_module.printer_manager, "get_model", lambda printer_id: None)
    monkeypatch.setattr(main_module, "printer_state_to_dict", lambda *args, **kwargs: {})
    monkeypatch.setattr(main_module.mqtt_relay, "on_printer_status", fake_status)
    monkeypatch.setattr(main_module.ws_manager, "send_printer_status", fake_status)
    monkeypatch.setattr(main_module.ws_manager, "broadcast", broadcast)
    monkeypatch.setattr(main_module, "_is_bambuddy_authorized_print", unauthorized)
    monkeypatch.setattr(main_module, "_send_kill_switch_provider_notification", provider_notification)
    monkeypatch.setattr("backend.app.services.finance_budget.is_printer_kill_switch_enabled", kill_switch_enabled)

    state = SimpleNamespace(
        connected=True,
        state="RUNNING",
        progress=0,
        remaining_time=0,
        layer_num=0,
        temperatures={},
        raw_data={},
        stg_cur=0,
        cooling_fan_speed=None,
        big_fan1_speed=None,
        big_fan2_speed=None,
        chamber_light=False,
        active_extruder=0,
        tray_now=255,
        door_open=False,
        ams_filament_backup=False,
        current_print=None,
        subtask_name="foreign_job",
        subtask_id="external-task-1",
        gcode_file="foreign_job.gcode",
    )

    await main_module.on_printer_status_change(7, state)

    assert stop_calls == [7]
    assert 7 in main_module._unauthorized_print_kill_sent
    broadcast.assert_awaited_once_with(
        {
            "type": "kill_switch_triggered",
            "printer_id": 7,
            "printer_name": "Printer 7",
            "filename": "foreign_job",
            "reason": "unauthorized_print",
        }
    )
    notification_task = main_module._kill_switch_notification_tasks[7]
    assert await notification_task is True
    provider_notification.assert_awaited_once_with(
        7,
        "Printer 7",
        {
            "status": "stopped",
            "filename": "foreign_job.gcode",
            "subtask_name": "foreign_job",
            "progress": 0,
            "reason": "unauthorized_print",
        },
    )


@pytest.mark.asyncio
async def test_failed_immediate_notification_allows_completion_retry():
    task = main_module.spawn_background_task(_return_false(), name="test-kill-switch-notification-failure")

    assert await main_module._kill_switch_notification_already_sent(task) is False


async def _return_false():
    return False


@pytest.mark.asyncio
async def test_bambuddy_authorized_print_is_not_stopped(monkeypatch):
    monkeypatch.setitem(main_module._expected_prints, (7, "foreign_job"), 123)

    stop_calls: list[int] = []

    async def fake_status(*args, **kwargs):
        return None

    async def kill_switch_enabled(_db):
        return True

    monkeypatch.setattr(main_module.printer_manager, "get_current_print_user", lambda printer_id: None)
    monkeypatch.setattr(
        main_module.printer_manager, "stop_print", lambda printer_id: stop_calls.append(printer_id) or True
    )
    monkeypatch.setattr(main_module.printer_manager, "get_printer", lambda printer_id: None)
    monkeypatch.setattr(main_module.printer_manager, "get_model", lambda printer_id: None)
    monkeypatch.setattr(main_module, "printer_state_to_dict", lambda *args, **kwargs: {})
    monkeypatch.setattr(main_module.mqtt_relay, "on_printer_status", fake_status)
    monkeypatch.setattr(main_module.ws_manager, "send_printer_status", fake_status)
    monkeypatch.setattr("backend.app.services.finance_budget.is_printer_kill_switch_enabled", kill_switch_enabled)

    state = SimpleNamespace(
        connected=True,
        state="RUNNING",
        progress=0,
        remaining_time=0,
        layer_num=0,
        temperatures={},
        raw_data={},
        stg_cur=0,
        cooling_fan_speed=None,
        big_fan1_speed=None,
        big_fan2_speed=None,
        chamber_light=False,
        active_extruder=0,
        tray_now=255,
        door_open=False,
        ams_filament_backup=False,
        current_print=None,
        subtask_name="foreign_job",
        gcode_file="foreign_job.gcode",
    )

    await main_module.on_printer_status_change(7, state)

    assert stop_calls == []
    assert 7 not in main_module._unauthorized_print_kill_sent


@pytest.mark.asyncio
async def test_unauthorized_print_state_is_cleared_when_print_ends(monkeypatch):
    stop_calls: list[int] = []

    async def fake_status(*args, **kwargs):
        return None

    async def kill_switch_enabled(_db):
        return True

    async def unauthorized(*_args):
        return False

    monkeypatch.setattr(main_module.printer_manager, "get_current_print_user", lambda printer_id: None)
    monkeypatch.setattr(
        main_module.printer_manager, "stop_print", lambda printer_id: stop_calls.append(printer_id) or True
    )
    monkeypatch.setattr(main_module.printer_manager, "get_printer", lambda printer_id: None)
    monkeypatch.setattr(main_module.printer_manager, "get_model", lambda printer_id: None)
    monkeypatch.setattr(main_module, "printer_state_to_dict", lambda *args, **kwargs: {})
    monkeypatch.setattr(main_module.mqtt_relay, "on_printer_status", fake_status)
    monkeypatch.setattr(main_module.ws_manager, "send_printer_status", fake_status)
    monkeypatch.setattr(main_module, "_is_bambuddy_authorized_print", unauthorized)
    monkeypatch.setattr("backend.app.services.finance_budget.is_printer_kill_switch_enabled", kill_switch_enabled)

    active_state = SimpleNamespace(
        connected=True,
        state="RUNNING",
        progress=0,
        remaining_time=0,
        layer_num=0,
        temperatures={},
        raw_data={},
        stg_cur=0,
        cooling_fan_speed=None,
        big_fan1_speed=None,
        big_fan2_speed=None,
        chamber_light=False,
        active_extruder=0,
        tray_now=255,
        door_open=False,
        ams_filament_backup=False,
        current_print=None,
        subtask_name="foreign_job",
        subtask_id="external-task-1",
        gcode_file="foreign_job.gcode",
    )

    idle_state = SimpleNamespace(
        connected=True,
        state="IDLE",
        progress=0,
        remaining_time=0,
        layer_num=0,
        temperatures={},
        raw_data={},
        stg_cur=0,
        cooling_fan_speed=None,
        big_fan1_speed=None,
        big_fan2_speed=None,
        chamber_light=False,
        active_extruder=0,
        tray_now=255,
        door_open=False,
        ams_filament_backup=False,
        current_print=None,
        subtask_name="",
        subtask_id=None,
        gcode_file=None,
    )

    await main_module.on_printer_status_change(7, active_state)
    assert stop_calls == [7]
    assert 7 in main_module._unauthorized_print_kill_sent

    await main_module.on_printer_status_change(7, idle_state)

    assert 7 not in main_module._unauthorized_print_kill_sent


@pytest.mark.asyncio
@pytest.mark.parametrize("printer_state", ["RUNNING", "PAUSE"])
async def test_persisted_print_is_authorized_after_restart(monkeypatch, printer_state):
    archive = SimpleNamespace(id=123, filename="owned_job.gcode.3mf")
    query_result = SimpleNamespace(scalar_one_or_none=lambda: archive)
    db = SimpleNamespace(execute=AsyncMock(return_value=query_result))

    class FakeSessionContext:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_args):
            return False

    stop_calls: list[int] = []

    async def fake_status(*args, **kwargs):
        return None

    async def kill_switch_enabled(_db):
        return True

    def discard_background_task(coro, **_kwargs):
        coro.close()

    monkeypatch.setattr(main_module, "async_session", FakeSessionContext)
    monkeypatch.setattr(main_module, "spawn_background_task", discard_background_task)
    monkeypatch.setattr(main_module.printer_manager, "get_current_print_user", lambda printer_id: None)
    monkeypatch.setattr(
        main_module.printer_manager, "stop_print", lambda printer_id: stop_calls.append(printer_id) or True
    )
    monkeypatch.setattr(main_module.printer_manager, "get_printer", lambda printer_id: None)
    monkeypatch.setattr(main_module.printer_manager, "get_model", lambda printer_id: None)
    monkeypatch.setattr(main_module, "printer_state_to_dict", lambda *args, **kwargs: {})
    monkeypatch.setattr(main_module.mqtt_relay, "on_printer_status", fake_status)
    monkeypatch.setattr(main_module.ws_manager, "send_printer_status", fake_status)
    monkeypatch.setattr("backend.app.services.finance_budget.is_printer_kill_switch_enabled", kill_switch_enabled)

    state = SimpleNamespace(
        connected=True,
        state=printer_state,
        progress=42,
        remaining_time=600,
        layer_num=50,
        temperatures={},
        raw_data={},
        stg_cur=0,
        cooling_fan_speed=None,
        big_fan1_speed=None,
        big_fan2_speed=None,
        chamber_light=False,
        active_extruder=0,
        tray_now=255,
        door_open=False,
        ams_filament_backup=False,
        current_print=None,
        subtask_name="owned_job",
        subtask_id="bambuddy-task-123",
        gcode_file="owned_job.gcode.3mf",
    )

    await main_module.on_printer_status_change(7, state)

    assert stop_calls == []
    assert (7, "owned_job.gcode.3mf") in main_module._active_prints
    assert main_module._active_prints[(7, "owned_job.gcode.3mf")] == 123
    assert 7 not in main_module._unauthorized_print_kill_sent


@pytest.mark.asyncio
async def test_kill_switch_defers_when_restart_identity_is_not_available(monkeypatch):
    state = SimpleNamespace(
        current_print=None,
        subtask_name="owned_job",
        subtask_id=None,
        gcode_file="owned_job.gcode.3mf",
    )
    db = SimpleNamespace(execute=AsyncMock())

    monkeypatch.setattr(main_module.printer_manager, "get_current_print_user", lambda printer_id: None)

    authorization = await main_module._is_bambuddy_authorized_print(7, state, db)

    assert authorization is None
    db.execute.assert_not_awaited()
