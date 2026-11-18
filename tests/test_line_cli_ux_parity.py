"""End-to-end LINE CLI & Webhook parity tests covering commands, postbacks, and error boundaries."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1.endpoints import webhook_line
from app.core.config import settings
from app.services.line_cli import cards, handlers


@pytest.fixture(autouse=True)
def _enable_legacy_line_cli(monkeypatch):
    monkeypatch.setattr(settings, "LINE_CHAT_COMMANDS_ENABLED", True)
    monkeypatch.setattr(settings, "LINE_PUSH_MODE", "all")


def _make_sig(body: bytes, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


@pytest.mark.asyncio
async def test_line_webhook_error_boundary_never_drops_silently(client, monkeypatch):
    """Any unexpected exception in a command handler must reply honestly instead of returning HTTP 500."""
    from app.services.line_service import line_service

    monkeypatch.setattr(settings, "LINE_CHANNEL_SECRET", "test_line_secret")
    monkeypatch.setattr(settings, "LINE_ALLOWED_USER_IDS", "U_ALLOWED_1")
    monkeypatch.setattr(settings, "LINE_GROUP_ID", "C_GROUP_1")

    replies = []
    pushes = []

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("CDP websocket disconnected")

    monkeypatch.setattr(handlers, "handle", _boom)
    monkeypatch.setattr(
        line_service,
        "_send_reply",
        AsyncMock(side_effect=lambda tok, msg: replies.append((tok, msg)) or True),
    )
    monkeypatch.setattr(
        line_service,
        "push_message",
        AsyncMock(side_effect=lambda msg, to_user_id=None: pushes.append((to_user_id, msg)) or True),
    )

    body_dict = {
        "events": [
            {
                "type": "message",
                "replyToken": "rtok_err_1",
                "source": {"type": "group", "groupId": "C_GROUP_1", "userId": "U_ALLOWED_1"},
                "message": {"type": "text", "id": "m1", "text": "/return AAV all"},
            }
        ]
    }
    raw = json.dumps(body_dict).encode("utf-8")
    resp = await client.post(
        "/api/v1/webhook/line",
        content=raw,
        headers={"X-Line-Signature": _make_sig(raw, "test_line_secret"), "Content-Type": "application/json"},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert payload["results"][0]["status"] == "ERROR"
    assert len(replies) == 1
    assert "คำสั่งขัดข้อง" in replies[0][1]
    assert "CDP websocket disconnected" in replies[0][1]


@pytest.mark.asyncio
async def test_line_cli_command_matrix_replies_and_error_safety(monkeypatch):
    """Verify /port, /orders, /cancel, /nego, /buy, /short, /borrow, /return and postback flows always respond."""
    handlers._DRAFTS.clear()

    sent = []

    class _FakeService:
        async def _send_reply(self, _tok, msg):
            sent.append(("reply", msg))
            return True

        async def push_message(self, msg, to_user_id=None):
            sent.append(("push", to_user_id, msg))
            return True

    from app.services.conversation_state import conversation_state
    from app.services.kill_switch_service import kill_switch_service
    from app.services.line_cli.parser import parse

    raw_event = {
        "replyToken": "rtok",
        "source": {"type": "group", "groupId": "C_GRP", "userId": "U_TEST"},
    }
    svc = _FakeService()
    monkeypatch.setattr(conversation_state, "is_authorized", lambda *a, **kw: True)
    monkeypatch.setattr(kill_switch_service, "is_active", lambda: False)
    monkeypatch.setattr(settings, "DRY_RUN_MODE", True)
    monkeypatch.setattr(handlers, "_in_nego_window", lambda *a, **kw: True)
    monkeypatch.setattr(handlers, "_broker_session_alive", AsyncMock(return_value=True))
    monkeypatch.setattr(
        handlers,
        "_broker_open_orders",
        AsyncMock(return_value=[{"order_no": "1001", "symbol": "PTT", "side": "BUY", "volume": 100, "balance": 100, "price": 34.75, "cancellable": True, "broker": "kss"}]),
    )
    monkeypatch.setattr(
        handlers,
        "_live_card",
        AsyncMock(return_value=cards.quick_trade_selector_card()),
    )
    monkeypatch.setattr(
        handlers,
        "_live_cards",
        AsyncMock(return_value=[cards.orders_empty_card(broker="kss")]),
    )
    monkeypatch.setattr(
        handlers,
        "_find_cancel_target",
        AsyncMock(return_value=[("kss", {"order_no": "1001", "symbol": "PTT", "side": "BUY", "balance": 100, "price": 35.0, "cancellable": True})]),
    )
    monkeypatch.setattr(
        handlers,
        "_broker_cancel",
        AsyncMock(return_value={"success": True, "verified": True}),
    )
    monkeypatch.setattr(
        handlers,
        "_broker_quote",
        AsyncMock(return_value={"available": True, "bid_1": 34.75, "offer_1": 35.0, "last": 35.0, "prior_close": 34.5}),
    )
    monkeypatch.setattr(
        handlers,
        "_pre_trade_check",
        AsyncMock(return_value={"ok": True, "resolved_volume": 100, "resolved_price": 35.0, "account_no": "<REDACTED_LIBERATOR_USERNAME>"}),
    )
    from app.services.broker_cdp import order_executor

    monkeypatch.setattr(
        order_executor,
        "execute_confirmed_order",
        AsyncMock(return_value={"success": True, "status": "DRY_RUN", "broker_order_no": "DRY-99"}),
    )

    from app.brokers.krungsri.sbl_realtime_driver import krungsri_sbl_driver

    monkeypatch.setattr(
        krungsri_sbl_driver,
        "check_symbol_risk",
        lambda sym: {"is_blocked": False, "available_shares": 100000, "borrow_fee_pct": 3.5},
    )
    monkeypatch.setattr(
        krungsri_sbl_driver,
        "execute_sbl_borrow",
        AsyncMock(return_value={"success": True, "status": "SBL_BORROW_DRY_RUN"}),
    )
    monkeypatch.setattr(
        handlers,
        "_get_active_sbl_lots",
        AsyncMock(return_value=[{"symbol": "AAV", "lot_id": "B495090", "broker_tx_id": "B495090", "remaining_qty": 94900, "borrow_date": "09/11/2026"}]),
    )
    monkeypatch.setattr(
        handlers,
        "_fetch_sbl_lots_safe",
        AsyncMock(return_value=[{"symbol": "AAV", "lot_id": "B495090", "broker_tx_id": "B495090", "remaining_qty": 94900, "borrow_date": "09/11/2026"}]),
    )
    monkeypatch.setattr(
        krungsri_sbl_driver,
        "return_sbl_shares_for_symbol",
        AsyncMock(return_value={"success": True, "status": "DRY_RUN_ARMED", "return_qty": 94900, "lots_returned": [{"lot_id": "B495090", "ref_no": "B495090", "qty": 94900}], "dry_run": True}),
    )

    try:
        # 1. Text commands
        for cmd in ("/port", "/orders", "/nego", "/buy PTT 100 35.0", "/short PTT 100 35.25", "/borrow AAV 1000", "/return AAV all", "/cancel 1001 kss"):
            sent.clear()
            res = await handlers.handle(parse(cmd), db=None, raw_event=raw_event, service=svc)
            assert res is not None, f"Command returned None: {cmd}"
            assert len(sent) >= 1, f"Command produced no reply: {cmd}"

        # 2. Find drafts created by /borrow, /return, /cancel and execute postbacks
        # Re-issue explicit drafts so we can assert postback confirm path
        sent.clear()
        r_ret = await handlers.handle(parse("/return AAV all"), db=None, raw_event=raw_event, service=svc)
        ret_draft_id = r_ret["draft_id"]
        res_ret_ok = await handlers.handle_postback(
            f"action=sbl_return_confirm&draft_id={ret_draft_id}",
            db=None,
            raw_event=raw_event,
            service=svc,
        )
        assert res_ret_ok["status"] == "SBL_RETURNED"
        assert res_ret_ok["dry_run"] is True
        assert any("94,900" in str(m) for m in sent)

        # 3. Simulate SBL return raising RuntimeError -> handled safely & replied honestly
        r_ret2 = await handlers.handle(parse("/return AAV all"), db=None, raw_event=raw_event, service=svc)
        monkeypatch.setattr(
            krungsri_sbl_driver,
            "return_sbl_shares_for_symbol",
            AsyncMock(side_effect=RuntimeError("MainSBL crashed")),
        )
        sent.clear()
        res_ret_err = await handlers.handle_postback(
            f"action=sbl_return_confirm&draft_id={r_ret2['draft_id']}",
            db=None,
            raw_event=raw_event,
            service=svc,
        )
        assert res_ret_err["status"] == "SBL_RETURN_FAILED"
        assert any("MainSBL crashed" in str(m) for m in sent)

        # 4. Nego postback _broker_modify raising -> caught & replied via _send_or_push
        handlers._store_draft({
            "draft_id": "nego_err_1",
            "created_at": 9e9,
            "broker": "kss",
            "order_no": "777",
            "symbol": "PTT",
            "side": "BUY",
            "volume": 100,
            "old_price": 34.75,
            "new_price": 35.0,
            "mode": "match",
        })
        monkeypatch.setattr(handlers, "_broker_modify", AsyncMock(side_effect=TimeoutError("streaming timeout")))
        sent.clear()
        res_nego_err = await handlers.handle_postback(
            "action=nego_confirm&draft_id=nego_err_1",
            db=None,
            raw_event=raw_event,
            service=svc,
        )
        assert res_nego_err["status"] == "NEGO_FAILED"
        assert any("streaming timeout" in str(m) for m in sent)
    finally:
        handlers._DRAFTS.clear()
