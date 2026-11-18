"""Integration tests verifying LINE Ecosystem Sunset, EOD-Only Quota Policy, MiniApp OTP & API/SSE."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings, settings
from app.core.events import event_bus
from app.main import app
from app.services.broker_cdp import trading_facade
from app.services.kill_switch_service import kill_switch_service
from app.services.line_service import line_service
from app.services.miniapp_inbox import miniapp_inbox
from app.services.otp_relay_readiness import CHANNEL_MINIAPP, CHANNEL_NONE, otp_relay_readiness


def test_production_settings_defaults_without_env():
    s = Settings(_env_file=None)
    assert s.LINE_CHAT_COMMANDS_ENABLED is False
    assert s.LINE_PUSH_MODE == "eod_only"
    assert s.EOD_SUMMARY_ENABLED is True
    assert s.LINE_EOD_PUSH_ENABLED is False
    assert s.LINE_EOD_PUSH_TIME == "17:05"
    assert s.LINE_OTP_RELAY_CHANNEL == "miniapp"


@pytest.mark.asyncio
async def test_webhook_sunset_blocks_all_chat_commands_and_replies_free(client: AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "LINE_CHAT_COMMANDS_ENABLED", False)
    monkeypatch.setattr(settings, "LIFF_ID", "2012540531-S5Ozs4US")

    replies = []
    pushes = []
    kill_called = []

    async def fake_reply(tok, msg):
        replies.append((tok, msg))
        return True

    async def fake_push(msg, to_user_id=None, **kwargs):
        pushes.append(msg)
        return True

    async def fake_kill(*a, **kw):
        kill_called.append((a, kw))
        return {}

    monkeypatch.setattr(line_service, "_send_reply", fake_reply)
    monkeypatch.setattr(line_service, "push_message", fake_push)
    monkeypatch.setattr(kill_switch_service, "activate", fake_kill)

    before_otp_q = event_bus.otp_queue.qsize()
    events = [
        {"type": "message", "replyToken": "rt-halt", "source": {"type": "user", "userId": "U1"}, "message": {"type": "text", "id": "m1", "text": "/halt"}},
        {"type": "message", "replyToken": "rt-otp", "source": {"type": "user", "userId": "U1"}, "message": {"type": "text", "id": "m2", "text": "849201"}},
        {"type": "message", "replyToken": "rt-cmd", "source": {"type": "user", "userId": "U1"}, "message": {"type": "text", "id": "m3", "text": "/port all sync"}},
        {"type": "message", "replyToken": "rt-img", "source": {"type": "user", "userId": "U1"}, "message": {"type": "image", "id": "m4"}},
        {"type": "postback", "replyToken": "rt-pb", "source": {"type": "user", "userId": "U1"}, "postback": {"data": "action=nego_menu"}},
    ]

    resp = await client.post("/api/v1/webhook/line", json={"events": events}, headers={"X-Line-Signature": "mock_signature"})
    assert resp.status_code == 200
    body = resp.json()
    assert all(r["status"] == "CHAT_RETIRED_MINIAPP_ONLY" for r in body["results"])
    assert kill_called == []
    assert event_bus.otp_queue.qsize() == before_otp_q
    assert pushes == []
    assert len(replies) == 5
    for _, card in replies:
        assert "miniapp.line.me/2012540531-S5Ozs4US" in json.dumps(card)


@pytest.mark.asyncio
async def test_eod_only_push_gate_mutes_intraday_and_funnels_to_inbox(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LINE_PUSH_MODE", "eod_only")
    monkeypatch.setattr(settings, "LINE_SIMULATION_MODE", False)
    monkeypatch.setattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "live_token_test")
    monkeypatch.setattr(settings, "LINE_ADMIN_USER_ID", "U_ADMIN")
    monkeypatch.setattr(settings, "LINE_GROUP_ID", None)
    monkeypatch.setattr(settings, "MINIAPP_INBOX_STATE_PATH", str(tmp_path / "inbox.json"))

    api_calls = []

    class _FakeResp:
        def __init__(self, code=200):
            self.status_code = code
            self.text = "ok"

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            api_calls.append({"url": url, "json": json})
            return _FakeResp(200)

    monkeypatch.setattr("app.services.line_service.httpx.AsyncClient", _FakeClient)

    # Intraday general / OTP / System / Facade card must ALL mute LINE Push & write Inbox
    assert await line_service.push_message("Intraday test msg") is False
    assert await line_service.push_otp_request(ref="ABCD", broker="KRUNGSRI", timeout_seconds=120) is False
    await line_service.send_system_notification("System Warn", "Detail")
    await trading_facade.best_effort_push_card({"type": "text", "text": "Trade Receipt PTT"})
    assert api_calls == []

    inbox = await miniapp_inbox.list_items()
    assert inbox["total"] >= 4
    cats = {it["category"] for it in inbox["items"]}
    assert {"general", "otp", "system", "trade"}.issubset(cats)

    # Only category="eod_summary" reaches api.line.me push
    ok = await line_service.push_message({"type": "text", "text": "EOD summary"}, to_user_id="U_ADMIN", category="eod_summary", record_inbox=False)
    assert ok is True
    assert len(api_calls) == 1
    assert api_calls[0]["json"]["to"] == "U_ADMIN"


@pytest.mark.asyncio
async def test_otp_relay_readiness_miniapp_primary(monkeypatch):
    monkeypatch.setattr(settings, "LINE_OTP_RELAY_CHANNEL", "miniapp")
    monkeypatch.setattr(settings, "LINE_OTP_RELAY_ENABLED", True)
    monkeypatch.setattr(settings, "LIFF_ID", "2012540531-S5Ozs4US")
    monkeypatch.setattr(settings, "LINE_GROUP_ID", None)
    monkeypatch.setattr(settings, "LINE_ADMIN_USER_ID", None)
    monkeypatch.setattr("app.services.otp_relay_readiness._port_open", lambda *a, **k: True)

    res_ok = await otp_relay_readiness.check()
    assert res_ok.ready is True
    assert res_ok.channel == CHANNEL_MINIAPP

    # Fails closed when gateway is down
    monkeypatch.setattr("app.services.otp_relay_readiness._port_open", lambda *a, **k: False)
    res_down = await otp_relay_readiness.check()
    assert res_down.ready is False
    assert res_down.channel == CHANNEL_NONE

    # Fails closed when LIFF_ID missing
    monkeypatch.setattr("app.services.otp_relay_readiness._port_open", lambda *a, **k: True)
    monkeypatch.setattr(settings, "LIFF_ID", "")
    res_noliff = await otp_relay_readiness.check()
    assert res_noliff.ready is False


@pytest.mark.asyncio
async def test_miniapp_inbox_quota_eod_and_sse_endpoints(tmp_path, monkeypatch):
    from app.api.v1.endpoints import miniapp as miniapp_ep

    monkeypatch.setattr(settings, "MINIAPP_INBOX_STATE_PATH", str(tmp_path / "inbox.json"))
    monkeypatch.setattr(settings, "LINE_EOD_STATE_PATH", str(tmp_path / "eod.json"))

    # Fail-closed unauthenticated check
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as anon:
        for path in ("/api/v1/miniapp/inbox", "/api/v1/miniapp/quota", "/api/v1/miniapp/eod/preview", "/api/v1/miniapp/events"):
            r = await anon.get(path)
            assert r.status_code == 401

    app.dependency_overrides[miniapp_ep.require_liff_user] = lambda: {"sub": "U_OP", "name": "Operator", "verified_via": "test"}
    try:
        await miniapp_inbox.publish(category="otp", severity="warning", title="OTP Test", body="Ref X", action_tab="brokers")
        monkeypatch.setattr(miniapp_ep.eod_summary_service, "build_summary", AsyncMock(return_value={"date": "2026-11-12", "status_kind": "no_orders", "counts": {"matched": 0, "open": 0}, "card": {}}))

        async with AsyncClient(transport=transport, base_url="http://test") as client:
            r_in = await client.get("/api/v1/miniapp/inbox")
            assert r_in.status_code == 200
            d_in = r_in.json()
            assert d_in["unread_count"] == 1
            assert d_in["items"][0]["title"] == "OTP Test"

            r_rd = await client.post("/api/v1/miniapp/inbox/read", json={})
            assert r_rd.status_code == 200
            assert r_rd.json()["unread_count"] == 0

            r_qt = await client.get("/api/v1/miniapp/quota")
            assert r_qt.status_code == 200
            assert "quota" in r_qt.json()
            assert "eod_status" in r_qt.json()

            r_pv = await client.get("/api/v1/miniapp/eod/preview")
            assert r_pv.status_code == 200
            assert r_pv.json()["status_kind"] == "no_orders"

            # Direct generator smoke test for authenticated SSE /events
            req = SimpleNamespace(is_disconnected=AsyncMock(return_value=False))
            sse_resp = await miniapp_ep.miniapp_events(request=req, _user={"sub": "U_OP"})
            first_chunk = await sse_resp.body_iterator.__anext__()
            await sse_resp.body_iterator.aclose()
            assert "event: connected" in first_chunk
            assert "MINIAPP_STREAM_READY" in first_chunk
    finally:
        app.dependency_overrides.pop(miniapp_ep.require_liff_user, None)
