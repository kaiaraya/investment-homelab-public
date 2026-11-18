"""Tests for Phase 1 LINE MiniApp (LIFF) auth, mobile web shell, and broker/OTP/kill-switch API."""

from types import SimpleNamespace
import time

import pytest
from httpx import ASGITransport, AsyncClient

from app.api.v1.endpoints import miniapp as miniapp_ep
from app.main import app
from app.miniapp import router as miniapp_web


@pytest.fixture(autouse=True)
def _reset_miniapp_state(monkeypatch):
    miniapp_ep._TOKEN_CACHE.clear()
    monkeypatch.setattr(miniapp_ep.settings, "MINIAPP_DEV_BYPASS_LOCAL", False)
    monkeypatch.setattr(miniapp_ep.settings, "LIFF_ID", "2000000000-AbCdEfGh")
    monkeypatch.setattr(miniapp_ep.settings, "LINE_LOGIN_CHANNEL_ID", "2000000000")
    monkeypatch.setattr(miniapp_ep.settings, "LINE_ALLOWED_USER_IDS", "U_ALLOWED_1")
    monkeypatch.setattr(miniapp_ep.settings, "LINE_ADMIN_USER_ID", "U_ADMIN_1")
    yield
    miniapp_ep._TOKEN_CACHE.clear()


@pytest.mark.asyncio
async def test_p1_t01_unauthenticated_fail_closed_and_public_health():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res_health = await client.get("/api/v1/miniapp/health")
        assert res_health.status_code == 200
        body = res_health.json()
        assert body["status"] == "ok"
        assert body["liff_configured"] is True
        assert "LINE_CHANNEL_SECRET" not in res_health.text

        res_me = await client.get("/api/v1/miniapp/me")
        assert res_me.status_code == 401

        res_status = await client.get("/api/v1/miniapp/brokers/status")
        assert res_status.status_code == 401


@pytest.mark.asyncio
async def test_p1_t02_liff_token_verification_and_allow_list(monkeypatch):
    monkeypatch.setattr(
        miniapp_ep.kill_switch_service,
        "get_status",
        lambda: {"is_active": False, "reason": None},
    )
    monkeypatch.setattr(miniapp_ep.trade_dispatcher, "get_mode_badge", lambda: "DRY-RUN")

    async def fake_verify(id_token: str, client_id: str):
        assert client_id == "2000000000"
        if id_token == "stranger-token":
            return {"sub": "U_STRANGER", "name": "Stranger", "exp": time.time() + 120}
        if id_token == "expired-token":
            return {"sub": "U_ALLOWED_1", "name": "Owner", "exp": time.time() - 10}
        return {"sub": "U_ALLOWED_1", "name": "Owner", "exp": time.time() + 300}

    monkeypatch.setattr(miniapp_ep, "_verify_line_id_token_remote", fake_verify)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res_forbidden = await client.get(
            "/api/v1/miniapp/me",
            headers={"Authorization": "Bearer stranger-token"},
        )
        assert res_forbidden.status_code == 403

        res_expired = await client.get(
            "/api/v1/miniapp/me",
            headers={"Authorization": "Bearer expired-token"},
        )
        assert res_expired.status_code == 401

        res_ok = await client.get(
            "/api/v1/miniapp/me",
            headers={"Authorization": "Bearer valid-token"},
        )
        assert res_ok.status_code == 200
        data = res_ok.json()
        assert data["user"]["sub"] == "U_ALLOWED_1"
        assert data["user"]["auth_mode"] == "liff"
        assert data["kill_switch"]["is_active"] is False

        # Exchange LIFF id_token for 24h Homelab session token
        res_sess = await client.post(
            "/api/v1/miniapp/auth/session",
            headers={"Authorization": "Bearer valid-token"},
        )
        assert res_sess.status_code == 200
        sess_data = res_sess.json()
        hl_tok = sess_data["session_token"]
        assert hl_tok.startswith("hl_")
        assert sess_data["ttl_seconds"] == 86400

        # Session token authenticates without hitting LINE remote verify again
        async def fail_if_remote_called(*_a, **_kw):
            raise AssertionError("session token must verify locally")

        monkeypatch.setattr(miniapp_ep, "_verify_line_id_token_remote", fail_if_remote_called)
        res_me_sess = await client.get(
            "/api/v1/miniapp/me",
            headers={"Authorization": f"Bearer {hl_tok}"},
        )
        assert res_me_sess.status_code == 200
        assert res_me_sess.json()["user"]["auth_mode"] == "session"

        # Tampered or expired session token fails closed (401)
        res_tamper = await client.get(
            "/api/v1/miniapp/me",
            headers={"Authorization": f"Bearer {hl_tok}x"},
        )
        assert res_tamper.status_code == 401
        expired_hl, _ = miniapp_ep.issue_miniapp_session_token({"sub": "U_ALLOWED_1"}, ttl_seconds=60)
        future_now = time.time() + 120
        monkeypatch.setattr(miniapp_ep.time, "time", lambda: future_now)
        res_hl_exp = await client.get(
            "/api/v1/miniapp/me",
            headers={"Authorization": f"Bearer {expired_hl}"},
        )
        assert res_hl_exp.status_code == 401


@pytest.mark.asyncio
async def test_p1_t03_local_dev_bypass_blocked_on_proxy_headers(monkeypatch):
    monkeypatch.setattr(miniapp_ep.settings, "MINIAPP_DEV_BYPASS_LOCAL", True)
    monkeypatch.setattr(
        miniapp_ep.kill_switch_service,
        "get_status",
        lambda: {"is_active": False},
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res_local = await client.get("/api/v1/miniapp/me")
        assert res_local.status_code == 200
        assert res_local.json()["user"]["auth_mode"] == "local_dev"
        assert res_local.json()["user"]["sub"] == "U_ADMIN_1"

        for proxy_header in ("CF-Connecting-IP", "X-Forwarded-For", "X-Real-IP"):
            res_tunnel = await client.get(
                "/api/v1/miniapp/me",
                headers={proxy_header: "203.0.113.42"},
            )
            assert res_tunnel.status_code == 401


@pytest.mark.asyncio
async def test_p1_t04_brokers_status_reports_daemon_down_without_spawning(monkeypatch):
    monkeypatch.setattr(miniapp_ep.settings, "MINIAPP_DEV_BYPASS_LOCAL", True)

    async def fail_if_spawned(*_args, **_kwargs):
        raise AssertionError("status endpoint must not launch broker daemons")

    monkeypatch.setattr(miniapp_ep.daemon_launcher, "ensure_daemon", fail_if_spawned)

    async def fake_health(broker: str):
        return {
            "authenticated": False,
            "account": None,
            "heartbeat": None,
            "source": "cdp",
            "cdp": {"state": "DAEMON_DOWN"},
        }

    async def fake_relay_check():
        return SimpleNamespace(ready=True, channel="group", reason="live")

    async def fake_pending():
        return None

    monkeypatch.setattr(miniapp_ep, "_broker_health", fake_health)
    monkeypatch.setattr(miniapp_ep.otp_relay_readiness, "check", fake_relay_check)
    monkeypatch.setattr(miniapp_ep.otp_relay_registry, "resolve_pending_request", fake_pending)
    monkeypatch.setattr(miniapp_ep.kill_switch_service, "get_status", lambda: {"is_active": False})

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/v1/miniapp/brokers/status")
        assert res.status_code == 200
        data = res.json()
        assert data["brokers"]["kss"]["port"] == 9333
        assert data["brokers"]["lib"]["port"] == 9334
        assert data["brokers"]["kss"]["cdp"]["state"] == "DAEMON_DOWN"
        assert data["otp_relay"]["ready"] is True
        assert data["pending_otp"] is None


@pytest.mark.asyncio
async def test_p1_t05_login_invariant_9_and_kill_switch_gates_and_stop(monkeypatch):
    monkeypatch.setattr(miniapp_ep.settings, "MINIAPP_DEV_BYPASS_LOCAL", True)
    login_calls = []
    tab_calls = []
    stop_calls = []

    async def fake_login(broker: str, force: bool = False):
        login_calls.append((broker, force))
        return {"state": "AUTHENTICATED", "account": "01025689"}

    async def fake_ensure_tabs():
        tab_calls.append(True)

    async def fake_detach(broker: str):
        stop_calls.append(("detach", broker))

    async def fake_stop_daemon(broker: str):
        stop_calls.append(("stop", broker))
        return {"stopped": True}

    async def fake_pending():
        return None

    monkeypatch.setattr(miniapp_ep, "_login_via_daemon", fake_login)
    monkeypatch.setattr(miniapp_ep.daemon_launcher, "ensure_kss_tabs_complete", fake_ensure_tabs)
    monkeypatch.setattr(miniapp_ep, "_detach_session_cdp", fake_detach)
    monkeypatch.setattr(miniapp_ep.daemon_launcher, "stop_daemon", fake_stop_daemon)
    monkeypatch.setattr(miniapp_ep.otp_relay_registry, "resolve_pending_request", fake_pending)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Gate 1: Kill-Switch active -> 423
        monkeypatch.setattr(miniapp_ep.kill_switch_service, "is_active", lambda: True)
        res_locked = await client.post("/api/v1/miniapp/brokers/kss/login", json={"force": False})
        assert res_locked.status_code == 423
        assert login_calls == []

        # Gate 2: OTP relay not ready -> 412 (Invariant 9)
        monkeypatch.setattr(miniapp_ep.kill_switch_service, "is_active", lambda: False)

        async def relay_down():
            return SimpleNamespace(ready=False, channel="none", reason="LINE webhook idle")

        monkeypatch.setattr(miniapp_ep.otp_relay_readiness, "check", relay_down)
        res_precond = await client.post("/api/v1/miniapp/brokers/kss/login", json={"force": True})
        assert res_precond.status_code == 412
        assert login_calls == []

        # Gate open -> succeeds & syncs KSS tabs
        async def relay_up():
            return SimpleNamespace(ready=True, channel="group", reason="ok")

        monkeypatch.setattr(miniapp_ep.otp_relay_readiness, "check", relay_up)
        res_ok = await client.post("/api/v1/miniapp/brokers/kss/login", json={"force": True})
        assert res_ok.status_code == 200
        assert res_ok.json()["ok"] is True
        assert login_calls == [("kss", True)]
        assert tab_calls == [True]

        # Stop daemon detaches CDP then stops browser
        res_stop = await client.post("/api/v1/miniapp/brokers/lib/stop")
        assert res_stop.status_code == 200
        assert stop_calls == [("detach", "lib"), ("stop", "lib")]


@pytest.mark.asyncio
async def test_p1_t06_otp_validation_and_relay_submission(monkeypatch):
    monkeypatch.setattr(miniapp_ep.settings, "MINIAPP_DEV_BYPASS_LOCAL", True)
    published = []
    recorded = []

    async def fake_pending():
        return SimpleNamespace(broker="KRUNGSRI", ref="AB12", is_expired=False)

    async def fake_publish_otp(payload):
        published.append(payload)

    async def fake_record_otp(otp: str, ref=None, broker=None):
        recorded.append((otp, ref, broker))

    monkeypatch.setattr(miniapp_ep.otp_relay_registry, "resolve_pending_request", fake_pending)
    monkeypatch.setattr(miniapp_ep.event_bus, "publish_otp", fake_publish_otp)
    monkeypatch.setattr(miniapp_ep.otp_relay_registry, "record_otp_received", fake_record_otp)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res_bad_len = await client.post("/api/v1/miniapp/otp", json={"otp": "12345"})
        assert res_bad_len.status_code == 422

        res_bad_alpha = await client.post("/api/v1/miniapp/otp", json={"otp": "12ab56"})
        assert res_bad_alpha.status_code == 422
        assert recorded == []

        res_ok = await client.post("/api/v1/miniapp/otp", json={"otp": "654321"})
        assert res_ok.status_code == 200
        body = res_ok.json()
        assert body["ok"] is True
        assert body["broker"] == "KRUNGSRI"
        assert body["ref"] == "AB12"
        assert len(published) == 1
        assert published[0].otp == "654321"
        assert recorded == [("654321", "AB12", "KRUNGSRI")]


@pytest.mark.asyncio
async def test_p1_t07_kill_switch_toggle(monkeypatch):
    monkeypatch.setattr(miniapp_ep.settings, "MINIAPP_DEV_BYPASS_LOCAL", True)
    calls = []
    state = {"is_active": False, "reason": None}

    async def fake_activate(reason: str, triggered_by: str):
        calls.append(("activate", reason, triggered_by))
        state["is_active"] = True
        state["reason"] = reason
        return {"status": "activated"}

    async def fake_reset(authorized_by: str):
        calls.append(("reset", authorized_by))
        state["is_active"] = False
        state["reason"] = None
        return {"status": "reset"}

    monkeypatch.setattr(miniapp_ep.kill_switch_service, "activate", fake_activate)
    monkeypatch.setattr(miniapp_ep.kill_switch_service, "reset", fake_reset)
    monkeypatch.setattr(miniapp_ep.kill_switch_service, "get_status", lambda: dict(state))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res_on = await client.post(
            "/api/v1/miniapp/kill-switch",
            json={"action": "activate", "reason": "Panic stop"},
        )
        assert res_on.status_code == 200
        assert res_on.json()["kill_switch"]["is_active"] is True
        assert calls[0] == ("activate", "Panic stop", "MINIAPP:U_ADMIN_1")

        res_off = await client.post(
            "/api/v1/miniapp/kill-switch",
            json={"action": "reset"},
        )
        assert res_off.status_code == 200
        assert res_off.json()["kill_switch"]["is_active"] is False
        assert calls[1] == ("reset", "MINIAPP:U_ADMIN_1")


@pytest.mark.asyncio
async def test_p1_t08_mobile_shell_config_and_static_assets(monkeypatch):
    monkeypatch.setattr(miniapp_web.settings, "LIFF_ID", "2000000000-AbCdEfGh")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for path in ("/miniapp", "/miniapp/", "/liff"):
            res = await client.get(path)
            assert res.status_code == 200
            assert "text/html" in res.headers["content-type"]
            html = res.text
            assert 'name="viewport"' in html
            assert "width=device-width" in html
            for tab in ("portfolio", "orders", "nego", "trade", "brokers"):
                assert f'data-tab="{tab}"' in html

        res_cfg = await client.get("/miniapp/config.json")
        assert res_cfg.status_code == 200
        cfg = res_cfg.json()
        assert cfg["liffId"] == "2000000000-AbCdEfGh"
        assert cfg["liffConfigured"] is True
        assert cfg["phase"] >= 1

        res_css = await client.get("/miniapp/static/css/app.css")
        assert res_css.status_code == 200
        assert "overflow-x: hidden;" in res_css.text
        assert "min-height: 44px;" in res_css.text

        res_js = await client.get("/miniapp/static/js/app.js")
        assert res_js.status_code == 200
        assert "renderBrokersView" in res_js.text


@pytest.mark.asyncio
async def test_p1_t09_broker_login_crashed_returns_502_and_publishes_inbox(monkeypatch):
    monkeypatch.setattr(miniapp_ep.settings, "MINIAPP_DEV_BYPASS_LOCAL", True)
    monkeypatch.setattr(miniapp_ep.kill_switch_service, "is_active", lambda: False)

    async def relay_up():
        return SimpleNamespace(ready=True, channel="group", reason="ok")

    async def fake_login_crashed(broker, force=False):
        return {
            "status": "crashed",
            "state": "DAEMON_DOWN",
            "port": 9333,
            "pid": 99999,
            "page_url": None,
            "log_tail": "RuntimeError: Chrome exited early with code 1: Missing X server",
        }

    inbox_calls = []

    async def fake_publish(**kwargs):
        inbox_calls.append(kwargs)
        return {"id": "err-1", **kwargs}

    monkeypatch.setattr(miniapp_ep.otp_relay_readiness, "check", relay_up)
    monkeypatch.setattr(miniapp_ep, "_login_via_daemon", fake_login_crashed)
    monkeypatch.setattr(miniapp_ep.miniapp_inbox, "publish", fake_publish)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post("/api/v1/miniapp/brokers/kss/login", json={"force": False})
        assert res.status_code == 502
        detail = res.json()["detail"]
        assert "KSS" in detail
        assert "Missing X server" in detail
        assert len(inbox_calls) == 1
        assert inbox_calls[0]["severity"] == "error"
        assert inbox_calls[0]["broker"] == "kss"


def test_p1_t10_cleanup_stale_singleton_locks_and_resolve_gui_env(tmp_path, monkeypatch):
    import os
    from app.services.broker_cdp import daemon_launcher
    from scripts.broker_cdp import session_daemon

    # Dead PID symlink should be removed
    host = os.uname().nodename
    (tmp_path / "SingletonLock").symlink_to(f"{host}-99999999")
    (tmp_path / "SingletonCookie").symlink_to("12345")
    (tmp_path / "SingletonSocket").symlink_to("/tmp/nonexistent")

    def fake_kill(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(session_daemon.os, "kill", fake_kill)
    session_daemon._cleanup_stale_singleton_locks(str(tmp_path))
    assert not (tmp_path / "SingletonLock").exists()
    assert not (tmp_path / "SingletonCookie").exists()
    assert not (tmp_path / "SingletonSocket").exists()

    # _resolve_gui_env should pick up .mutter-Xwaylandauth.* from XDG_RUNTIME_DIR
    xauth_file = tmp_path / ".mutter-Xwaylandauth.TEST123"
    xauth_file.write_text("dummy")
    (tmp_path / "wayland-0").write_text("")
    monkeypatch.setattr(
        daemon_launcher.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stdout=""),
    )
    resolved = daemon_launcher._resolve_gui_env({"XDG_RUNTIME_DIR": str(tmp_path)})
    assert resolved["XAUTHORITY"] == str(xauth_file)
    assert resolved["WAYLAND_DISPLAY"] == "wayland-0"
    assert resolved["DISPLAY"] == ":0"
