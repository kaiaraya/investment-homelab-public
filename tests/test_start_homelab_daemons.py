# -*- coding: utf-8 -*-
"""Regression and integration tests for start_homelab.py Stage 2/3 CDP unification.

Verifies:
1. stage2_verify_krungsri_session delegates to ensure_daemon('kss') and checks cdp_session_status('kss').
2. When KSS CDP session is AUTHENTICATED -> returns True.
3. When KSS CDP session is not AUTHENTICATED -> returns False and triggers push_krungsri_login_alert.
4. stage3_verify_liberator_session delegates to ensure_daemon('liberator') and checks cdp_session_status('liberator').
5. When Liberator CDP session is AUTHENTICATED -> returns True; else returns False (non-fatal, optional).
6. Sequential execution contract: ensure_daemon is called sequentially per broker, never closing running browsers.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

import scripts.start_homelab as sh


@pytest.mark.asyncio
async def test_stage2_krungsri_authenticated():
    with (
        patch("scripts.start_homelab.ensure_daemon", new_callable=AsyncMock) as mock_ensure,
        patch("scripts.start_homelab.cdp_session_status", new_callable=AsyncMock) as mock_probe,
        patch("scripts.start_homelab.line_service.push_message", new_callable=AsyncMock) as mock_push,
        patch("scripts.start_homelab.push_krungsri_login_alert", new_callable=AsyncMock) as mock_alert,
    ):
        mock_ensure.return_value = {"status": "already_up", "state": "AUTHENTICATED", "port": 9333, "pid": 1234}
        mock_probe.return_value = {"state": "AUTHENTICATED", "port": 9333, "pid": 1234, "url": "https://wme2.settrade.com/realtime/streaming7/StreamingPage.jsp"}

        ok = await sh.stage2_verify_krungsri_session()
        assert ok is True
        mock_ensure.assert_awaited_once_with("kss", wait_s=90.0)
        mock_probe.assert_awaited_once_with("kss")
        mock_push.assert_awaited_once()
        assert "✅ [Krungsri KSS] ล็อกอินสำเร็จ" in mock_push.call_args[0][0]
        mock_alert.assert_not_called()


@pytest.mark.asyncio
async def test_stage2_krungsri_not_authenticated_escalates_line():
    with (
        patch("scripts.start_homelab.ensure_daemon", new_callable=AsyncMock) as mock_ensure,
        patch("scripts.start_homelab.cdp_session_status", new_callable=AsyncMock) as mock_probe,
        patch("scripts.start_homelab.push_krungsri_login_alert", new_callable=AsyncMock) as mock_alert,
        patch("scripts.start_homelab.resolve_pending_request", new_callable=AsyncMock, return_value=None),
    ):
        mock_ensure.return_value = {"status": "DAEMON_DOWN", "state": "DAEMON_DOWN", "port": 9333, "pid": 1234}
        mock_probe.return_value = {"state": "DAEMON_DOWN", "port": 9333, "pid": 1234, "reason": "CDP daemon down"}

        ok = await sh.stage2_verify_krungsri_session()
        assert ok is False
        mock_ensure.assert_awaited_once_with("kss", wait_s=90.0)
        mock_probe.assert_awaited_once_with("kss")
        mock_alert.assert_awaited_once()
        assert "CDP session not authenticated" in mock_alert.call_args[0][0]


@pytest.mark.asyncio
async def test_stage2_krungsri_login_page_no_pending_escalates_line():
    with (
        patch("scripts.start_homelab.ensure_daemon", new_callable=AsyncMock) as mock_ensure,
        patch("scripts.start_homelab.cdp_session_status", new_callable=AsyncMock) as mock_probe,
        patch("scripts.start_homelab.push_krungsri_login_alert", new_callable=AsyncMock) as mock_alert,
        patch("scripts.start_homelab.resolve_pending_request", new_callable=AsyncMock, return_value=None),
    ):
        mock_ensure.return_value = {"status": "LOGIN_PAGE", "state": "LOGIN_PAGE", "port": 9333, "pid": 1234}
        mock_probe.return_value = {"state": "LOGIN_PAGE", "port": 9333, "pid": 1234, "reason": "Target tab is at login page"}

        ok = await sh.stage2_verify_krungsri_session()
        assert ok is False
        mock_alert.assert_awaited_once()
        assert "CDP session not authenticated" in mock_alert.call_args[0][0]


@pytest.mark.asyncio
async def test_stage2_krungsri_daemon_down_with_fresh_pending_still_fails():
    """R1 regression: if daemon is dead, having a fresh pending OTP request in DB
    must NOT count as waiting for OTP and must return False with failure alert.
    """
    mock_req = MagicMock(broker="KRUNGSRI", ref="GHJP", is_expired=False)
    with (
        patch("scripts.start_homelab.ensure_daemon", new_callable=AsyncMock) as mock_ensure,
        patch("scripts.start_homelab.cdp_session_status", new_callable=AsyncMock) as mock_probe,
        patch("scripts.start_homelab.push_krungsri_login_alert", new_callable=AsyncMock) as mock_alert,
        patch("scripts.start_homelab.resolve_pending_request", new_callable=AsyncMock, return_value=mock_req),
        patch("scripts.start_homelab.line_service.push_message", new_callable=AsyncMock) as mock_push,
    ):
        mock_ensure.return_value = {"status": "DAEMON_DOWN", "state": "DAEMON_DOWN", "port": 9333, "pid": 1234}
        mock_probe.return_value = {"state": "DAEMON_DOWN", "port": 9333, "pid": 1234, "reason": "daemon down"}

        ok = await sh.stage2_verify_krungsri_session()
        assert ok is False
        mock_alert.assert_awaited_once()
        mock_push.assert_not_called()


@pytest.mark.asyncio
async def test_stage2_krungsri_waiting_for_otp_state():
    with (
        patch("scripts.start_homelab.ensure_daemon", new_callable=AsyncMock) as mock_ensure,
        patch("scripts.start_homelab.cdp_session_status", new_callable=AsyncMock) as mock_probe,
        patch("scripts.start_homelab.push_krungsri_login_alert", new_callable=AsyncMock) as mock_alert,
        patch("scripts.start_homelab.resolve_pending_request", new_callable=AsyncMock, return_value=None),
    ):
        mock_ensure.return_value = {"status": "OTP_PAGE", "state": "OTP_PAGE", "port": 9333, "pid": 1234}
        mock_probe.return_value = {"state": "OTP_PAGE", "port": 9333, "pid": 1234, "reason": "Target tab is at otp marker"}

        ok = await sh.stage2_verify_krungsri_session()
        assert ok is True
        mock_ensure.assert_awaited_once_with("kss", wait_s=90.0)
        mock_probe.assert_awaited_once_with("kss")
        mock_alert.assert_not_called()


@pytest.mark.asyncio
async def test_stage2_krungsri_waiting_for_otp_pending_request():
    mock_req = MagicMock(broker="kss", ref="GHJP", is_expired=False)
    with (
        patch("scripts.start_homelab.ensure_daemon", new_callable=AsyncMock) as mock_ensure,
        patch("scripts.start_homelab.cdp_session_status", new_callable=AsyncMock) as mock_probe,
        patch("scripts.start_homelab.push_krungsri_login_alert", new_callable=AsyncMock) as mock_alert,
        patch("scripts.start_homelab.resolve_pending_request", new_callable=AsyncMock, return_value=mock_req),
    ):
        mock_ensure.return_value = {"status": "OTP_PAGE", "state": "LOGIN_PAGE", "port": 9333, "pid": 302952}
        mock_probe.return_value = {"state": "LOGIN_PAGE", "port": 9333, "pid": 302952, "reason": "Target tab is at login/otp marker"}

        ok = await sh.stage2_verify_krungsri_session()
        assert ok is True
        mock_ensure.assert_awaited_once_with("kss", wait_s=90.0)
        mock_probe.assert_awaited_once_with("kss")
        mock_alert.assert_not_called()


@pytest.mark.asyncio
async def test_stage2_krungsri_exception_handled():
    with (
        patch("scripts.start_homelab.ensure_daemon", side_effect=RuntimeError("CDP port conflict")),
        patch("scripts.start_homelab.push_krungsri_login_alert", new_callable=AsyncMock) as mock_alert,
    ):
        ok = await sh.stage2_verify_krungsri_session()
        assert ok is False
        mock_alert.assert_awaited_once()
        assert "CDP exception" in mock_alert.call_args[0][0]


@pytest.mark.asyncio
async def test_stage3_liberator_authenticated():
    with (
        patch("scripts.start_homelab.ensure_daemon", new_callable=AsyncMock) as mock_ensure,
        patch("scripts.start_homelab.cdp_session_status", new_callable=AsyncMock) as mock_probe,
    ):
        mock_ensure.return_value = {"status": "spawned", "state": "AUTHENTICATED", "port": 9334, "pid": 5678}
        mock_probe.return_value = {"state": "AUTHENTICATED", "port": 9334, "pid": 5678, "url": "https://webtrade.liberator.co.th/Portfolio"}

        ok = await sh.stage3_verify_liberator_session()
        assert ok is True
        mock_ensure.assert_awaited_once_with("liberator", wait_s=90.0)
        mock_probe.assert_awaited_once_with("liberator")


@pytest.mark.asyncio
async def test_stage3_liberator_not_authenticated_returns_false_without_raising():
    with (
        patch("scripts.start_homelab.ensure_daemon", new_callable=AsyncMock) as mock_ensure,
        patch("scripts.start_homelab.cdp_session_status", new_callable=AsyncMock) as mock_probe,
    ):
        mock_ensure.return_value = {"status": "DAEMON_DOWN", "state": "DAEMON_DOWN", "port": 9334, "pid": None}
        mock_probe.return_value = {"state": "DAEMON_DOWN", "port": 9334, "pid": None, "reason": "CDP daemon on port 9334 not reachable"}

        ok = await sh.stage3_verify_liberator_session()
        assert ok is False
        mock_ensure.assert_awaited_once_with("liberator", wait_s=90.0)
        mock_probe.assert_awaited_once_with("liberator")


@pytest.mark.asyncio
async def test_stage3_liberator_exception_handled():
    with patch("scripts.start_homelab.ensure_daemon", side_effect=Exception("Liberator network failure")):
        ok = await sh.stage3_verify_liberator_session()
        assert ok is False


@pytest.mark.asyncio
async def test_stage2_and_stage3_force_fresh_calls_restart_daemon():
    with (
        patch("scripts.start_homelab.restart_daemon", new_callable=AsyncMock) as mock_restart,
        patch("scripts.start_homelab.cdp_session_status", new_callable=AsyncMock) as mock_probe,
    ):
        mock_restart.return_value = {"status": "spawned", "state": "AUTHENTICATED", "port": 9333, "pid": 1111}
        mock_probe.return_value = {"state": "AUTHENTICATED", "port": 9333, "pid": 1111, "url": "https://wme2.settrade.com"}

        ok_kss = await sh.stage2_verify_krungsri_session(force_fresh=True)
        assert ok_kss is True
        mock_restart.assert_awaited_once_with("kss", wait_s=90)

    with (
        patch("scripts.start_homelab.restart_daemon", new_callable=AsyncMock) as mock_restart,
        patch("scripts.start_homelab.cdp_session_status", new_callable=AsyncMock) as mock_probe,
    ):
        mock_restart.return_value = {"status": "spawned", "state": "AUTHENTICATED", "port": 9334, "pid": 2222}
        mock_probe.return_value = {"state": "AUTHENTICATED", "port": 9334, "pid": 2222, "url": "https://webtrade.liberator.co.th"}

        ok_lib = await sh.stage3_verify_liberator_session(force_fresh=True)
        assert ok_lib is True
        mock_restart.assert_awaited_once_with("liberator", wait_s=90)


def test_skip_login_plan_does_not_execute_launcher():
    plan = sh.resolve_startup_plan(kde_ok=True, skip_login=True, skip_liberator=True)
    assert plan["run_krungsri"] is False
    assert plan["run_liberator"] is False


@pytest.mark.asyncio
async def test_stage2_otp_waiting_allows_stage3_to_run():
    """Acceptance test: daemon status=OTP_PAGE / state=LOGIN_PAGE does not abort,
    avoids login-failure alert, pushes waiting notice, and allows Stage 3 (Liberator) to run.
    """
    mock_req = MagicMock(broker="KRUNGSRI", ref="GHJP", is_expired=False)
    with (
        patch("scripts.start_homelab.ensure_daemon", new_callable=AsyncMock) as mock_ensure,
        patch("scripts.start_homelab.cdp_session_status", new_callable=AsyncMock) as mock_probe,
        patch("scripts.start_homelab.push_krungsri_login_alert", new_callable=AsyncMock) as mock_alert,
        patch("scripts.start_homelab.resolve_pending_request", new_callable=AsyncMock, return_value=mock_req),
        patch("scripts.start_homelab.line_service.push_message", new_callable=AsyncMock) as mock_line_push,
    ):
        # KSS daemon at OTP_PAGE / LOGIN_PAGE; Liberator at AUTHENTICATED
        mock_ensure.side_effect = [
            {"status": "OTP_PAGE", "state": "LOGIN_PAGE", "port": 9333, "pid": 302952},
            {"status": "spawned", "state": "AUTHENTICATED", "port": 9334, "pid": 302955},
        ]
        mock_probe.side_effect = [
            {"state": "LOGIN_PAGE", "port": 9333, "pid": 302952, "reason": "Target tab is at login/otp marker"},
            {"state": "AUTHENTICATED", "port": 9334, "pid": 302955, "url": "https://webtrade.liberator.co.th/Portfolio"},
        ]

        # Stage 2 executes: should return True without aborting or killing daemon
        kss_ok = await sh.stage2_verify_krungsri_session()
        assert kss_ok is True
        mock_alert.assert_not_called()
        mock_line_push.assert_awaited_once()
        assert "รอ OTP ref=GHJP" in mock_line_push.call_args[0][0]

        # Stage 3 executes: Liberator session launches
        lib_ok = await sh.stage3_verify_liberator_session()
        assert lib_ok is True
        assert mock_ensure.await_count == 2


def test_detached_child_survives_parent_exit(tmp_path):
    """R3 / Test 3: Verifies that a child process spawned with start_new_session=True
    survives when its parent process exits (does not receive SIGHUP/SIGTERM from parent exit).
    """
    import os
    import signal
    import subprocess
    import sys
    import time

    child_script = tmp_path / "child_stub.py"
    parent_script = tmp_path / "parent_stub.py"
    pid_file = tmp_path / "child.pid"

    child_script.write_text("import time\ntime.sleep(30)\n")
    parent_script.write_text(f"""
import subprocess
import sys
import time

proc = subprocess.Popen(
    [sys.executable, {repr(str(child_script))}],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)
with open({repr(str(pid_file))}, 'w') as f:
    f.write(str(proc.pid))
sys.exit(0)
""")

    res = subprocess.run([sys.executable, str(parent_script)], capture_output=True, text=True, timeout=5)
    assert res.returncode == 0
    assert pid_file.exists()
    child_pid = int(pid_file.read_text().strip())

    try:
        time.sleep(0.3)
        # Check if child is still running (kill with signal 0 checks existence)
        os.kill(child_pid, 0)
    finally:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except OSError:
            pass

