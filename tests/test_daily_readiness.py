"""
Unit tests for scripts.daily_readiness — Pure-CDP unified daily readiness runner.

Covers the safety gates the skill promises (broker_pdca_rca / root_cause_and_escalation):

  * Invariant 9 — auto re-login must be CANCELLED (status=BRIDGE_DOWN) and must NEVER
    call ensure_daemon / restart_daemon / trigger_cdp_login (or legacy auth_driver.login)
    when the mobile OTP relay bridge is down.
  * Anti-Mock — the GO/NO-GO verdict must reflect the real read-only CDP probe in
    `--no-login` mode (requiring BOTH `streaming` and `sbl` AUTHENTICATED on :9333 for KSS,
    and `portfolio` AUTHENTICATED on :9334 for Liberator).
  * Zero-Push tolerance — when LINE Push quota is exhausted (HTTP 429), proactive ping is
    skipped without failing readiness because incoming OTP webhook remains live.
  * Exit code mapping: GO -> 0, NO-GO -> 1.
"""

import asyncio
import json
from unittest.mock import AsyncMock

import scripts.daily_readiness as dr
from app.browser.auth_driver import auth_driver as krungsri_auth_driver
from app.brokers.liberator.driver import liberator_driver
from app.core.config import settings
from app.services.broker_cdp import daemon_launcher, session_probe


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _pin_bridge(monkeypatch, reachable=True, refreshed=None, push_quota_exhausted=False):
    """
    Pins the LINE-group OTP relay probes so tests never touch the network, `.env`
    or the cloudflared tunnel.
    """
    from app.services.line_service import line_service
    from app.services.otp_relay_readiness import OtpRelayReadiness, otp_relay_readiness

    if refreshed is not None:
        reachable = bool(refreshed[-1]) if isinstance(refreshed, (list, tuple)) else bool(refreshed)

    details = {
        "gateway_up": reachable,
        "line_group_id_configured": True,
        "push_quota_exhausted": push_quota_exhausted,
    }
    if reachable:
        result = OtpRelayReadiness(
            ready=True,
            channel="line_group",
            reason="LINE group relay ready (test)",
            details=details,
        )
    else:
        result = OtpRelayReadiness(
            ready=False,
            channel="none",
            reason="Backend gateway is DOWN on :8000 (test)",
            details=details,
        )

    monkeypatch.setattr(otp_relay_readiness, "check", AsyncMock(return_value=result))
    monkeypatch.setattr(line_service, "_push_quota_exhausted", push_quota_exhausted, raising=False)
    monkeypatch.setattr(line_service, "push_message", AsyncMock(return_value=True))
    monkeypatch.setattr(line_service, "send_system_notification", AsyncMock(return_value=False))


def _pin_cdp_states(
    monkeypatch,
    *,
    kss_streaming="AUTHENTICATED",
    kss_sbl="AUTHENTICATED",
    lib_portfolio="AUTHENTICATED",
):
    async def _fake_probe(broker: str, purpose=None):
        b = (broker or "").lower()
        p = (purpose or "").lower()
        if b in ("kss", "krungsri"):
            st = kss_sbl if p == "sbl" else kss_streaming
            return {"broker": "kss", "purpose": p or "streaming", "state": st, "port": 9333}
        return {"broker": "lib", "purpose": p or "portfolio", "state": lib_portfolio, "port": 9334}

    probe_mock = AsyncMock(side_effect=_fake_probe)
    monkeypatch.setattr(session_probe, "cdp_session_status", probe_mock)
    return probe_mock


# --------------------------------------------------------------------------- #
# Invariant 9 — OTP Bridge Readiness Gate (Pure CDP + retired legacy guard)
# --------------------------------------------------------------------------- #
def test_krungsri_login_cancelled_when_bridge_down(monkeypatch, capsys):
    """bridge down + unauthenticated CDP -> BRIDGE_DOWN; never touches launcher or legacy TradeMan."""
    _pin_cdp_states(monkeypatch, kss_streaming="LOGIN_PAGE", kss_sbl="DAEMON_DOWN")
    legacy_login_spy = AsyncMock()
    ensure_spy = AsyncMock()
    restart_spy = AsyncMock()
    trigger_spy = AsyncMock()

    monkeypatch.setattr(krungsri_auth_driver, "login", legacy_login_spy)
    monkeypatch.setattr(daemon_launcher, "ensure_daemon", ensure_spy)
    monkeypatch.setattr(daemon_launcher, "restart_daemon", restart_spy)
    monkeypatch.setattr(daemon_launcher, "trigger_cdp_login", trigger_spy)

    args = dr.parse_args([])
    out = asyncio.run(dr.ensure_krungsri(args, bridge_ok=False))

    assert out["status"] == "BRIDGE_DOWN"
    assert out["authenticated"] is None
    assert out["needs_fresh_login"] is True
    legacy_login_spy.assert_not_awaited()
    ensure_spy.assert_not_awaited()
    restart_spy.assert_not_awaited()
    trigger_spy.assert_not_awaited()
    assert "ไม่ยิง SMS OTP" in capsys.readouterr().out


def test_liberator_login_cancelled_when_bridge_down(monkeypatch):
    _pin_cdp_states(monkeypatch, lib_portfolio="LOGIN_PAGE")
    legacy_login_spy = AsyncMock()
    ensure_spy = AsyncMock()
    trigger_spy = AsyncMock()

    monkeypatch.setattr(liberator_driver, "login", legacy_login_spy)
    monkeypatch.setattr(daemon_launcher, "ensure_daemon", ensure_spy)
    monkeypatch.setattr(daemon_launcher, "trigger_cdp_login", trigger_spy)

    args = dr.parse_args([])
    out = asyncio.run(dr.ensure_liberator(args, bridge_ok=False))

    assert out["status"] == "BRIDGE_DOWN"
    assert out["authenticated"] is None
    legacy_login_spy.assert_not_awaited()
    ensure_spy.assert_not_awaited()
    trigger_spy.assert_not_awaited()


def test_no_login_flag_skips_relogin_and_requires_both_kss_tabs(monkeypatch):
    ensure_spy = AsyncMock()
    trigger_spy = AsyncMock()
    monkeypatch.setattr(daemon_launcher, "ensure_daemon", ensure_spy)
    monkeypatch.setattr(daemon_launcher, "trigger_cdp_login", trigger_spy)

    # Only streaming AUTHENTICATED while sbl is NO_TARGET_TAB -> needs_fresh_login=True
    _pin_cdp_states(monkeypatch, kss_streaming="AUTHENTICATED", kss_sbl="NO_TARGET_TAB")
    args = dr.parse_args(["--no-login"])
    out_partial = asyncio.run(dr.ensure_krungsri(args, bridge_ok=True))
    assert out_partial["status"] == "SKIPPED"
    assert out_partial["needs_fresh_login"] is True
    assert dr._krungsri_ready(out_partial) is False

    # Both streaming + sbl AUTHENTICATED -> needs_fresh_login=False
    _pin_cdp_states(monkeypatch, kss_streaming="AUTHENTICATED", kss_sbl="AUTHENTICATED")
    out_ok = asyncio.run(dr.ensure_krungsri(args, bridge_ok=True))
    assert out_ok["status"] == "SKIPPED"
    assert out_ok["needs_fresh_login"] is False
    assert dr._krungsri_ready(out_ok) is True
    ensure_spy.assert_not_awaited()
    trigger_spy.assert_not_awaited()


def test_krungsri_auto_heals_missing_sbl_tab_without_otp_even_if_bridge_down(monkeypatch):
    """When streaming is already AUTHENTICATED, ensure_kss_tabs_complete heals SBL with zero OTP."""
    state = {"sbl": "NO_TARGET_TAB"}

    async def _probe(broker, purpose=None):
        if purpose == "sbl":
            return {"state": state["sbl"], "port": 9333}
        return {"state": "AUTHENTICATED", "port": 9333}

    async def _heal():
        state["sbl"] = "AUTHENTICATED"
        return {"status": "AUTHENTICATED", "streaming_state": "AUTHENTICATED", "sbl_state": "AUTHENTICATED"}

    trigger_spy = AsyncMock()
    monkeypatch.setattr(session_probe, "cdp_session_status", AsyncMock(side_effect=_probe))
    monkeypatch.setattr(daemon_launcher, "ensure_kss_tabs_complete", AsyncMock(side_effect=_heal))
    monkeypatch.setattr(daemon_launcher, "trigger_cdp_login", trigger_spy)

    out = asyncio.run(dr.ensure_krungsri(dr.parse_args([]), bridge_ok=False))
    assert out["status"] == "AUTHENTICATED"
    assert out["authenticated"] is True
    assert out["sbl_state"] == "AUTHENTICATED"
    trigger_spy.assert_not_awaited()


# --------------------------------------------------------------------------- #
# OTP relay step (Invariant 9 — LINE variant + Zero-Push 429 handling)
# --------------------------------------------------------------------------- #
def test_relay_ready_reports_line_channel(monkeypatch):
    _pin_bridge(monkeypatch, reachable=True)
    args = dr.parse_args([])

    out = asyncio.run(dr.ensure_mobile_bridge(args))

    assert out["reachable"] is True
    assert out["channel"] == "line_group"


def test_relay_ready_skips_proactive_ping_when_push_quota_exhausted(monkeypatch):
    _pin_bridge(monkeypatch, reachable=True, push_quota_exhausted=True)
    from app.services.line_service import line_service

    push_spy = AsyncMock(return_value=False)
    monkeypatch.setattr(line_service, "push_message", push_spy)

    out = asyncio.run(dr.ensure_mobile_bridge(dr.parse_args([])))
    assert out["reachable"] is True
    assert out["pinged"] is False
    push_spy.assert_not_awaited()


def test_relay_down_escalates_to_hitl_without_pinging(monkeypatch):
    """When the relay is not ready we escalate (LINE system notification) — never fake readiness."""
    _pin_bridge(monkeypatch, reachable=False)
    from app.services.line_service import line_service

    help_spy = AsyncMock(return_value=True)
    push_spy = AsyncMock(return_value=True)
    monkeypatch.setattr(line_service, "send_system_notification", help_spy)
    monkeypatch.setattr(line_service, "push_message", push_spy)

    args = dr.parse_args([])
    out = asyncio.run(dr.ensure_mobile_bridge(args))

    assert out["reachable"] is False
    assert out["pinged"] is False
    help_spy.assert_awaited_once()
    push_spy.assert_not_awaited()


def test_relay_ready_sends_line_group_confirmation(monkeypatch):
    """Ready relay -> push the human-confirm message into the LINE group (stepwise-approval §5)."""
    _pin_bridge(monkeypatch, reachable=True)
    from app.services.line_service import line_service

    push_spy = AsyncMock(return_value=True)
    monkeypatch.setattr(line_service, "push_message", push_spy)

    args = dr.parse_args([])
    out = asyncio.run(dr.ensure_mobile_bridge(args))

    assert out["reachable"] is True and out["pinged"] is True
    push_spy.assert_awaited_once()


# --------------------------------------------------------------------------- #
# GO / NO-GO verdict logic (Anti-Mock: no false NO-GO from a deliberate skip)
# --------------------------------------------------------------------------- #
def test_krungsri_ready_matrix():
    assert dr._krungsri_ready({"status": "AUTHENTICATED"}) is True
    assert dr._krungsri_ready({"status": "FAILED"}) is False
    assert dr._krungsri_ready({"status": "ERROR"}) is False
    assert dr._krungsri_ready({"status": "BRIDGE_DOWN"}) is False
    assert dr._krungsri_ready({"status": "SKIPPED", "needs_fresh_login": False}) is True
    assert dr._krungsri_ready({"status": "SKIPPED", "needs_fresh_login": True}) is False


def test_liberator_ready_matrix():
    assert dr._liberator_ready({"status": "AUTHENTICATED"}) is True
    assert dr._liberator_ready({"status": "SKIPPED", "exists": True, "expired": False}) is True
    assert dr._liberator_ready({"status": "SKIPPED", "exists": True, "expired": True}) is False
    assert dr._liberator_ready({"status": "SKIPPED", "exists": False, "expired": False}) is False
    assert dr._liberator_ready({"status": "BRIDGE_DOWN"}) is False


def test_run_report_only_is_go_when_both_sessions_healthy(monkeypatch):
    """--no-login + healthy Pure-CDP probes -> GO (no browser launch, no OTP)."""
    _pin_bridge(monkeypatch, reachable=True)
    _pin_cdp_states(
        monkeypatch,
        kss_streaming="AUTHENTICATED",
        kss_sbl="AUTHENTICATED",
        lib_portfolio="AUTHENTICATED",
    )
    monkeypatch.setattr(dr, "check_backend", lambda: {"up": True})

    summary = asyncio.run(dr.run(dr.parse_args(["--no-login", "--no-ping"])))

    assert summary["verdict"] == "GO"
    assert summary["reasons"] == []


def test_run_report_only_no_go_when_krungsri_session_missing(monkeypatch):
    _pin_bridge(monkeypatch, reachable=True)
    _pin_cdp_states(
        monkeypatch,
        kss_streaming="LOGIN_PAGE",
        kss_sbl="NO_TARGET_TAB",
        lib_portfolio="AUTHENTICATED",
    )
    monkeypatch.setattr(dr, "check_backend", lambda: {"up": True})

    summary = asyncio.run(dr.run(dr.parse_args(["--no-login", "--no-ping"])))

    assert summary["verdict"] == "NO-GO"
    assert any("Krungsri" in r for r in summary["reasons"])


def test_run_no_go_when_bridge_down(monkeypatch, capsys):
    _pin_bridge(monkeypatch, refreshed=[False, False])
    _pin_cdp_states(
        monkeypatch,
        kss_streaming="AUTHENTICATED",
        kss_sbl="AUTHENTICATED",
        lib_portfolio="AUTHENTICATED",
    )
    monkeypatch.setattr(dr, "check_backend", lambda: {"up": True})

    summary = asyncio.run(dr.run(dr.parse_args(["--no-login", "--no-ping"])))

    assert summary["verdict"] == "NO-GO"
    assert any("bridge" in r.lower() for r in summary["reasons"])
    assert "SMS OTP" in capsys.readouterr().out


def test_run_skips_liberator_when_flagged(monkeypatch):
    """--skip-liberator: Liberator must not block the verdict."""
    _pin_bridge(monkeypatch, reachable=True)
    _pin_cdp_states(
        monkeypatch,
        kss_streaming="AUTHENTICATED",
        kss_sbl="AUTHENTICATED",
        lib_portfolio="DAEMON_DOWN",
    )
    monkeypatch.setattr(dr, "check_backend", lambda: {"up": True})

    summary = asyncio.run(dr.run(dr.parse_args(["--no-login", "--no-ping", "--skip-liberator"])))

    assert summary["verdict"] == "GO"
    assert summary["liberator"]["status"] == "SKIPPED"


def test_ensure_krungsri_and_liberator_trigger_cdp_login_when_needed(monkeypatch):
    """When bridge_ok=True and daemon is on LOGIN_PAGE, triggers Pure-CDP login (never legacy driver)."""
    state = {"kss_str": "LOGIN_PAGE", "kss_sbl": "NO_TARGET_TAB", "lib": "LOGIN_PAGE"}

    async def _probe(broker, purpose=None):
        if broker in ("kss", "krungsri"):
            return {"state": state["kss_sbl"] if purpose == "sbl" else state["kss_str"], "port": 9333}
        return {"state": state["lib"], "port": 9334}

    async def _trigger(broker, wait_until_complete=False, **kw):
        assert wait_until_complete is True
        if broker == "kss":
            state["kss_str"] = "AUTHENTICATED"
            state["kss_sbl"] = "AUTHENTICATED"
            return {"state": "AUTHENTICATED", "sbl_state": "AUTHENTICATED", "port": 9333}
        state["lib"] = "AUTHENTICATED"
        return {"state": "AUTHENTICATED", "port": 9334}

    legacy_kss_spy = AsyncMock()
    legacy_lib_spy = AsyncMock()
    monkeypatch.setattr(krungsri_auth_driver, "login", legacy_kss_spy)
    monkeypatch.setattr(liberator_driver, "login", legacy_lib_spy)
    monkeypatch.setattr(session_probe, "cdp_session_status", AsyncMock(side_effect=_probe))
    monkeypatch.setattr(daemon_launcher, "ensure_daemon", AsyncMock(return_value={"state": "LOGIN_PAGE"}))
    monkeypatch.setattr(daemon_launcher, "trigger_cdp_login", AsyncMock(side_effect=_trigger))

    kss_out = asyncio.run(dr.ensure_krungsri(dr.parse_args([]), bridge_ok=True))
    lib_out = asyncio.run(dr.ensure_liberator(dr.parse_args([]), bridge_ok=True))

    assert kss_out["status"] == "AUTHENTICATED" and kss_out["authenticated"] is True
    assert lib_out["status"] == "AUTHENTICATED" and lib_out["authenticated"] is True
    legacy_kss_spy.assert_not_awaited()
    legacy_lib_spy.assert_not_awaited()


# --------------------------------------------------------------------------- #
# Exit-code mapping + Click2Win / backend probes
# --------------------------------------------------------------------------- #
def test_main_returns_1_on_no_go(monkeypatch):
    _pin_bridge(monkeypatch, refreshed=[False, False])
    _pin_cdp_states(monkeypatch, kss_streaming="DAEMON_DOWN", kss_sbl="DAEMON_DOWN", lib_portfolio="DAEMON_DOWN")
    monkeypatch.setattr(dr, "check_backend", lambda: {"up": False})

    assert dr.main(["--no-login", "--no-ping"]) == 1


def test_main_returns_0_on_go(monkeypatch):
    _pin_bridge(monkeypatch, reachable=True)
    _pin_cdp_states(
        monkeypatch,
        kss_streaming="AUTHENTICATED",
        kss_sbl="AUTHENTICATED",
        lib_portfolio="AUTHENTICATED",
    )
    monkeypatch.setattr(dr, "check_backend", lambda: {"up": True})

    assert dr.main(["--no-login", "--no-ping"]) == 0


def test_click2win_never_claims_a_green_check(tmp_path, monkeypatch, capsys):
    """Click2Win is a sandbox with no JWT -> presence only, never a false ✅."""
    p = tmp_path / "click2win_session.json"
    p.write_text(json.dumps({"cookies": [{"name": "a"}, {"name": "b"}]}), encoding="utf-8")
    monkeypatch.setattr(settings, "CLICK2WIN_SESSION_PATH", str(p))

    out = dr.inspect_click2win(dr.parse_args([]))
    printed = capsys.readouterr().out

    assert out["exists"] is True and out["cookies"] == 2
    assert "✅" not in printed


def test_check_backend_uses_tcp_probe(monkeypatch):
    monkeypatch.setattr(dr, "_port_open", lambda host, port, timeout=0.6: True)
    assert dr.check_backend() == {"up": True}
    monkeypatch.setattr(dr, "_port_open", lambda host, port, timeout=0.6: False)
    assert dr.check_backend() == {"up": False}
