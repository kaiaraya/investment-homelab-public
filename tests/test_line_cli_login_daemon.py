# -*- coding: utf-8 -*-
"""Unit tests for LINE CLI /login Phase 2 integration with session daemon.

Verifies:
- /login wakes up session_daemon (ensure_daemon) instead of auth_driver.login
- force triggers restart_daemon
- /login zero-OTP attach when already authenticated and no daemon spawn
- concurrent lock protection between /login kss and /login all
"""
import asyncio
from unittest.mock import AsyncMock
import pytest

from app.services.line_cli import handlers
from tests.test_line_cli_flow import FakeService, ATTACHED_KSS, _patch_relay, _run, _text_of


@pytest.fixture
def service():
    return FakeService()


@pytest.fixture(autouse=True)
def _broker_edges(monkeypatch):
    monkeypatch.setattr(handlers, "_broker_session_alive", AsyncMock(return_value=True))
    monkeypatch.setattr(handlers, "_attach_session_cdp", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers, "_detach_session_cdp", AsyncMock(return_value=False))
    monkeypatch.setattr(handlers, "_in_nego_window", lambda now=None: True)
    handlers._LOCKS.clear()
    handlers._STARTED.clear()
    handlers._AWAITING_OTP.clear()
    handlers._PENDING_LOGIN_NEXT.clear()
    handlers._LAST_CHAINED_FROM.clear()


@pytest.mark.asyncio
async def test_login_wakes_daemon_when_not_attached(service, db_session, monkeypatch):
    """When attach returns None, ensure_daemon is called instead of legacy auth_driver."""
    monkeypatch.setattr(handlers, "_attach_session_cdp", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers, "_broker_session_alive", AsyncMock(return_value=False))
    _patch_relay(monkeypatch, ready=True)

    from app.services.broker_cdp import daemon_launcher
    mock_ensure = AsyncMock(return_value={
        "status": "ready",
        "broker": "kss",
        "port": 9333,
        "pid": 999,
        "page_url": "https://wme2.settrade.com/realtime/streaming7/StreamingPage.jsp",
        "state": "AUTHENTICATED",
    })
    monkeypatch.setattr(daemon_launcher, "ensure_daemon", mock_ensure)

    # Legacy auth_driver must NOT be called
    from app.browser.auth_driver import auth_driver
    mock_auth = AsyncMock()
    monkeypatch.setattr(auth_driver, "login", mock_auth)

    res = await _run(service, "/login kss", db=db_session)

    assert res.get("status") == "CLI_LOGIN"
    mock_ensure.assert_awaited_once_with("kss")
    mock_auth.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_force_restarts_daemon(service, db_session, monkeypatch):
    """Force flag triggers restart_daemon."""
    monkeypatch.setattr(handlers, "_attach_session_cdp", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers, "_broker_session_alive", AsyncMock(return_value=False))
    _patch_relay(monkeypatch, ready=True)

    from app.services.broker_cdp import daemon_launcher
    mock_restart = AsyncMock(return_value={
        "status": "ready",
        "broker": "kss",
        "port": 9333,
        "pid": 1001,
        "page_url": "https://wme2.settrade.com/realtime/streaming7/StreamingPage.jsp",
        "state": "AUTHENTICATED",
    })
    monkeypatch.setattr(daemon_launcher, "restart_daemon", mock_restart)

    res = await _run(service, "/login kss force", db=db_session)

    assert res.get("status") == "CLI_LOGIN"
    mock_restart.assert_awaited_once_with("kss")


@pytest.mark.asyncio
async def test_login_zero_otp_when_already_authenticated(service, db_session, monkeypatch):
    """When attach succeeds, no daemon launch or OTP needed."""
    monkeypatch.setattr(handlers, "_attach_session_cdp", AsyncMock(return_value=dict(ATTACHED_KSS)))
    from app.services.broker_cdp import daemon_launcher
    mock_ensure = AsyncMock()
    monkeypatch.setattr(daemon_launcher, "ensure_daemon", mock_ensure)

    res = await _run(service, "/login kss", db=db_session)

    assert res.get("status") == "CLI_LOGIN"
    assert res["results"][0]["status"] == "attached"
    mock_ensure.assert_not_awaited()


@pytest.mark.asyncio
async def test_login_kss_and_login_all_mutual_lock(service, db_session):
    """Gate lock prevents concurrent /login kss and /login all."""
    gate_lock = handlers._lock_for("login", "gate")
    assert not gate_lock.locked()

    async with gate_lock:
        res = await _run(service, "/login kss", db=db_session)
        assert res.get("status") == "CLI_BUSY"
        assert "กำลังทำงานอยู่" in service.last_text()


@pytest.mark.asyncio
async def test_login_awaiting_otp_when_login_page_does_not_fail(service, db_session, monkeypatch):
    """When launcher returns LOGIN_PAGE or OTP_PAGE, handler reports awaiting OTP, NOT failed/❌."""
    monkeypatch.setattr(handlers, "_attach_session_cdp", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers, "_broker_session_alive", AsyncMock(return_value=False))
    _patch_relay(monkeypatch, ready=True)

    from app.services.broker_cdp import daemon_launcher
    mock_ensure = AsyncMock(return_value={
        "status": "LOGIN_PAGE",
        "broker": "kss",
        "port": 9333,
        "pid": 999,
        "page_url": "https://auth.settrade.com/auth/login",
        "state": "LOGIN_PAGE",
        "log_tail": "Waiting for OTP confirmation...",
    })
    monkeypatch.setattr(daemon_launcher, "ensure_daemon", mock_ensure)

    res = await _run(service, "/login kss", db=db_session)

    assert res.get("status") == "CLI_LOGIN"
    assert res["results"][0]["status"] == "awaiting_otp"
    assert res["results"][0]["state"] == "LOGIN_PAGE"
    pushes = [_text_of(m) for m, _ in service.pushes]
    assert not any("❌" in p for p in pushes)
    assert any("กำลังรอ OTP" in p for p in pushes)


@pytest.mark.asyncio
async def test_login_lib_calls_ensure_daemon_lib(service, db_session, monkeypatch):
    """Handler path for /login lib routes to ensure_daemon with 'lib'."""
    monkeypatch.setattr(handlers, "_attach_session_cdp", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers, "_broker_session_alive", AsyncMock(return_value=False))
    _patch_relay(monkeypatch, ready=True)

    from app.services.broker_cdp import daemon_launcher
    mock_ensure = AsyncMock(return_value={
        "status": "spawned",
        "broker": "liberator",
        "port": 9334,
        "pid": 1002,
        "page_url": "https://webtrade.liberator.co.th/portfolio",
        "state": "AUTHENTICATED",
    })
    monkeypatch.setattr(daemon_launcher, "ensure_daemon", mock_ensure)

    res = await _run(service, "/login lib", db=db_session)

    assert res.get("status") == "CLI_LOGIN"
    mock_ensure.assert_awaited_once_with("lib")
    assert res["results"][0]["status"] == "ok"


@pytest.mark.asyncio
async def test_login_daemon_down_fails_with_log_tail(service, db_session, monkeypatch):
    """When daemon is down / crashes, handler reports failure with 5-line log tail."""
    monkeypatch.setattr(handlers, "_attach_session_cdp", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers, "_broker_session_alive", AsyncMock(return_value=False))
    _patch_relay(monkeypatch, ready=True)

    from app.services.broker_cdp import daemon_launcher
    mock_ensure = AsyncMock(return_value={
        "status": "crashed",
        "broker": "kss",
        "port": 9333,
        "pid": 999,
        "page_url": None,
        "state": "DAEMON_DOWN",
        "log_tail": "line1\nline2\nline3\nline4\nline5",
    })
    monkeypatch.setattr(daemon_launcher, "ensure_daemon", mock_ensure)

    res = await _run(service, "/login kss", db=db_session)

    assert res.get("status") == "CLI_LOGIN"
    assert res["results"][0]["status"] == "failed"
    assert res["results"][0]["state"] == "DAEMON_DOWN"
    pushes = [_text_of(m) for m, _ in service.pushes]
    assert any("❌" in p and "log tail" in p and "line5" in p for p in pushes)


@pytest.mark.asyncio
async def test_stop_command_stops_cdp_daemons_and_replies_via_reply_token(service, db_session, monkeypatch):
    """/stop detaches and calls daemon_launcher.stop_daemon for kss and lib, replying via _send_reply."""
    from app.services.broker_cdp import daemon_launcher

    mock_stop = AsyncMock(side_effect=lambda b: {"status": "stopped", "state": "DAEMON_DOWN", "broker": b})
    monkeypatch.setattr(daemon_launcher, "stop_daemon", mock_stop)

    res = await _run(service, "/stop", db=db_session)

    assert res == {"status": "CLI_STOP", "brokers": ["kss", "lib"]}
    assert [c.args[0] for c in mock_stop.await_args_list] == ["kss", "lib"]
    assert len(service.pushes) == 0
    assert "ปิดพักบราวเซอร์ CDP" in service.last_text()
    assert "Gateway :8000" in service.last_text()


@pytest.mark.asyncio
async def test_zero_push_login_reserves_reply_token_for_otp_ref(service, db_session, monkeypatch, tmp_path):
    """When _push_quota_exhausted=True, /login kss skips burning replyToken on progress and replies with Ref."""
    from app.services import otp_relay_registry
    from app.services.broker_cdp import daemon_launcher

    monkeypatch.setattr(otp_relay_registry, "_get_otp_state_file", lambda: tmp_path / "otp_state.json")
    otp_relay_registry.clear_otp_state()

    monkeypatch.setattr(handlers, "_attach_session_cdp", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers, "_broker_session_alive", AsyncMock(return_value=False))
    _patch_relay(monkeypatch, ready=True)

    service._push_quota_exhausted = True

    monkeypatch.setattr(
        daemon_launcher,
        "ensure_daemon",
        AsyncMock(return_value={"status": "LOGIN_PAGE", "state": "LOGIN_PAGE", "port": 9333, "pid": 4321}),
    )
    monkeypatch.setattr(
        daemon_launcher,
        "trigger_cdp_login",
        AsyncMock(return_value={"status": "OTP_PAGE", "state": "OTP_PAGE", "port": 9333, "pid": 4321, "ref": "ABCD"}),
    )

    res = await _run(service, "/login kss", db=db_session)

    assert res["status"] == "CLI_LOGIN"
    assert res["results"][0]["status"] == "awaiting_otp"
    assert res["results"][0]["ref"] == "ABCD"
    # Zero pushes needed; the single replyToken carries the awaiting-OTP + Ref message!
    assert len(service.pushes) == 0
    assert len(service.replies) == 1
    reply_txt = service.last_text()
    assert "กำลังเข้าระบบ" not in reply_txt
    assert "กำลังรอ OTP" in reply_txt
    assert "Ref: ABCD" in reply_txt


@pytest.mark.asyncio
async def test_stale_otp_page_without_pending_request_does_not_deadlock_login(service, db_session, monkeypatch, tmp_path):
    """A stale OTP_PAGE tab on daemon with no unexpired pending request must NOT block /login kss with CLI_BUSY."""
    from contextlib import asynccontextmanager
    from datetime import datetime, timedelta, timezone
    from app.services import otp_relay_registry
    from app.services.broker_cdp import daemon_launcher

    @asynccontextmanager
    async def _isolated_primary():
        yield db_session

    monkeypatch.setattr(otp_relay_registry, "_primary_session", _isolated_primary)
    monkeypatch.setattr(otp_relay_registry, "_get_otp_state_file", lambda: tmp_path / "otp_state.json")
    otp_relay_registry.clear_otp_state()
    # Seed an already-expired request (> 300s old)
    otp_relay_registry._IN_MEMORY_PENDING_REQUEST = otp_relay_registry.PendingOtpRequest(
        broker="KRUNGSRI",
        ref="OLD1",
        label="Krungsri",
        created_at=datetime.now(timezone.utc) - timedelta(seconds=600),
    )

    monkeypatch.setattr(handlers, "_attach_session_cdp", AsyncMock(return_value=None))
    monkeypatch.setattr(handlers, "_broker_session_alive", AsyncMock(return_value=False))
    _patch_relay(monkeypatch, ready=True)

    monkeypatch.setattr(daemon_launcher, "daemon_state", AsyncMock(return_value={"state": "OTP_PAGE", "port": 9333}))
    monkeypatch.setattr(
        daemon_launcher,
        "ensure_daemon",
        AsyncMock(return_value={"status": "OTP_PAGE", "state": "OTP_PAGE", "port": 9333, "pid": 4321}),
    )
    mock_trigger = AsyncMock(
        return_value={"status": "OTP_PAGE", "state": "OTP_PAGE", "port": 9333, "pid": 4321, "ref": "NEW9"}
    )
    monkeypatch.setattr(daemon_launcher, "trigger_cdp_login", mock_trigger)

    res = await _run(service, "/login kss", db=db_session)

    assert res["status"] == "CLI_LOGIN"
    assert res["results"][0]["status"] == "awaiting_otp"
    assert res["results"][0]["ref"] == "NEW9"
    mock_trigger.assert_awaited_once()
    otp_relay_registry.clear_otp_state()


@pytest.mark.asyncio
async def test_broker_health_marks_source_cdp_when_daemon_up_on_login_page(monkeypatch):
    """When daemon_up=True on LOGIN_PAGE, _broker_health reports source='CDP' rather than 'flag'."""
    from unittest.mock import PropertyMock, patch

    probe_login = {
        "broker": "kss",
        "purpose": "streaming",
        "state": "LOGIN_PAGE",
        "daemon_up": True,
        "port": 9333,
        "pid": 2222,
    }
    probe_sbl = {
        "broker": "kss",
        "purpose": "sbl",
        "state": "NO_TARGET_TAB",
        "daemon_up": True,
        "port": 9333,
        "pid": 2222,
    }

    async def _fake_probe(broker, purpose=None):
        return probe_sbl if purpose == "sbl" else probe_login

    monkeypatch.setattr("app.services.broker_cdp.session_probe.cdp_session_status", _fake_probe)
    with patch("app.browser.auth_driver.KrungsriAuthDriver.is_authenticated", new_callable=PropertyMock, return_value=False):
        h = await handlers._broker_health("kss")

    assert h["authenticated"] is False
    assert h["source"] == "CDP"
    assert h["cdp"]["state"] == "LOGIN_PAGE"
    assert h["cdp"]["sbl"]["state"] == "NO_TARGET_TAB"


@pytest.mark.asyncio
async def test_await_otp_login_and_reply_sends_verified_reply_via_reply_token(monkeypatch, tmp_path):
    """_await_otp_login_and_reply polls CDP status and sends verified ✅ reply over replyToken (0 pushes)."""
    from app.services import otp_relay_registry
    from app.services.line_service import LineMessagingService

    monkeypatch.setattr(otp_relay_registry, "_get_otp_state_file", lambda: tmp_path / "otp_state.json")
    otp_relay_registry.clear_otp_state()

    svc = LineMessagingService()
    sent_replies = []
    sent_pushes = []

    async def _capture_reply(tok, msg):
        sent_replies.append((tok, msg))
        return True

    async def _capture_push(msg, to_user_id=None):
        sent_pushes.append((msg, to_user_id))
        return False

    monkeypatch.setattr(svc, "_send_reply", _capture_reply)
    monkeypatch.setattr(svc, "push_message", _capture_push)

    calls = {"streaming": 0}

    async def _fake_probe(broker, purpose=None):
        if broker == "kss" and purpose == "streaming":
            calls["streaming"] += 1
            if calls["streaming"] < 2:
                return {"state": "OTP_PAGE", "daemon_up": True, "port": 9333}
            return {"state": "AUTHENTICATED", "daemon_up": True, "port": 9333}
        if broker == "kss" and purpose == "sbl":
            return {"state": "AUTHENTICATED", "daemon_up": True, "port": 9333}
        return {"state": "UNKNOWN", "daemon_up": False}

    monkeypatch.setattr("app.services.broker_cdp.session_probe.cdp_session_status", _fake_probe)

    await svc._await_otp_login_and_reply(
        reply_token="tok-otp-2",
        broker_tag="KRUNGSRI",
        ref_code="ABCD",
        masked_otp="12**56",
        target_label="Krungsri (KSS)",
        timeout_s=3.0,
        poll_s=0.05,
    )

    assert len(sent_pushes) == 0
    assert len(sent_replies) == 1
    tok, msg = sent_replies[0]
    assert tok == "tok-otp-2"
    assert "✅ ยืนยัน OTP 12**56 สำเร็จ" in msg["text"]
    assert "Streaming + SBL พร้อม" in msg["text"]

