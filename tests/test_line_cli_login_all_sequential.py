import asyncio
from unittest.mock import AsyncMock, patch
import pytest

from app.services.line_cli import handlers
from app.services.line_cli.parser import parse
from tests.test_line_cli_flow import FakeService, _patch_relay, _text_of


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    handlers._STARTED.clear()
    handlers._AWAITING_OTP.clear()
    handlers._LOCKS.clear()
    _patch_relay(monkeypatch, ready=True)
    yield
    handlers._STARTED.clear()
    handlers._AWAITING_OTP.clear()
    handlers._LOCKS.clear()


@pytest.mark.asyncio
async def test_login_all_sequential_success():
    cmd = parse("/login all")
    svc = FakeService()
    launch_order = []

    async def fake_attach(b):
        return None

    async def fake_login_daemon(b, force=False):
        launch_order.append(b)
        return {"ok": True, "status": "spawned", "state": "OTP_PAGE"}

    async def fake_wait_auth(b, timeout_seconds=None, poll_interval=None):
        return {"ok": True, "state": "AUTHENTICATED", "status": "ok"}

    with (
        patch.object(handlers, "_attach_session_cdp", side_effect=fake_attach),
        patch.object(handlers, "_broker_session_alive", AsyncMock(return_value=False)),
        patch.object(handlers, "_login_via_daemon", side_effect=fake_login_daemon),
        patch.object(handlers, "_wait_for_daemon_authenticated", side_effect=fake_wait_auth),
    ):
        res = await handlers._handle_login(cmd, svc, "tok-1", "usr-1")

    assert res["status"] == "CLI_LOGIN"
    assert launch_order == ["kss", "lib"]
    push_texts = [_text_of(p[0]) for p in svc.pushes]
    assert any("1/2 KSS" in t for t in push_texts)
    assert any("2/2 LIB" in t for t in push_texts)


@pytest.mark.asyncio
async def test_login_all_aborts_liberator_when_kss_fails():
    cmd = parse("/login all")
    svc = FakeService()
    launch_order = []

    async def fake_attach(b):
        return None

    async def fake_login_daemon(b, force=False):
        launch_order.append(b)
        return {"ok": True, "status": "spawned", "state": "OTP_PAGE"}

    async def fake_wait_auth(b, timeout_seconds=None, poll_interval=None):
        return {"ok": False, "state": "TIMEOUT", "status": "timeout", "detail": "OTP timeout (180s)"}

    with (
        patch.object(handlers, "_attach_session_cdp", side_effect=fake_attach),
        patch.object(handlers, "_broker_session_alive", AsyncMock(return_value=False)),
        patch.object(handlers, "_login_via_daemon", side_effect=fake_login_daemon),
        patch.object(handlers, "_wait_for_daemon_authenticated", side_effect=fake_wait_auth),
    ):
        res = await handlers._handle_login(cmd, svc, "tok-1", "usr-1")

    assert res["status"] == "CLI_LOGIN"
    # Crucial invariant: Liberator must NEVER be launched if Krungsri timed out
    assert launch_order == ["kss"]
    results = res["results"]
    assert len(results) == 2
    assert results[0]["broker"] == "kss" and results[0]["status"] == "timeout"
    assert results[1]["broker"] == "lib" and results[1]["status"] == "aborted"

    push_texts = [_text_of(p[0]) for p in svc.pushes]
    assert any("ขอยกเลิกคิวที่เหลือ (LIB) และไม่ส่ง SMS" in t for t in push_texts)


@pytest.mark.asyncio
async def test_single_flight_otp_guard():
    # Simulate KSS OTP already in-flight
    handlers._AWAITING_OTP["kss"] = asyncio.get_event_loop().time()

    cmd = parse("/login lib")
    svc = FakeService()

    with patch("app.services.broker_cdp.daemon_launcher.daemon_state", AsyncMock(return_value={"state": "OTP_PAGE"})):
        res = await handlers._handle_login(cmd, svc, "tok-1", "usr-1")

    assert res["status"] == "CLI_BUSY"
    assert res["detail"] == "otp_in_flight"
    assert res["active_broker"] == "kss"

    reply_texts = [_text_of(r) for r in svc.replies]
    assert any("มี OTP ของ KSS รอการยืนยันอยู่" in t for t in reply_texts)
