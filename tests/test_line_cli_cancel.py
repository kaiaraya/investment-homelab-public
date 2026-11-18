"""``/cancel <order_no>`` + the ❌ buttons on the per-stock ``/orders`` cards.

Covers the parser grammar, the card builder (a cancel button exists **only** on a
resting order), and the three-step postback flow. The CDP scrape and the cancel
executor are mocked at the ``handlers`` boundary; no broker is touched.
"""
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.services.line_cli.handlers as H
from app.services.line_cli import cards
from app.services.line_cli.parser import E_BAD_BROKER, E_BAD_TARGET, E_MISSING_ARG, CliCommand, CliError, parse


def _service():
    s = MagicMock()
    s._send_reply = AsyncMock()
    s.push_message = AsyncMock()
    return s


def _row(**over):
    row = {
        "broker": "kss", "order_no": "04050", "symbol": "TPIPP", "side": "SELL",
        "volume": 100, "matched_volume": 0, "balance": 100,
        "price": 3.20, "status": "Queuing", "kind": "open", "cancellable": True,
    }
    row.update(over)
    return row


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def test_parse_cancel_order_only():
    cmd = parse("/cancel 04050")
    assert isinstance(cmd, CliCommand)
    assert cmd.cmd == "cancel" and cmd.target == "04050" and cmd.broker is None


def test_parse_cancel_with_broker():
    cmd = parse("/cancel 04050 lib")
    assert cmd.broker == "lib" and cmd.target == "04050"


def test_parse_cancel_hash_prefix_and_thai_alias():
    cmd = parse("/ยกเลิก #04050 kss")
    assert cmd.cmd == "cancel" and cmd.target == "04050" and cmd.broker == "kss"


def test_parse_cancel_requires_order_no():
    err = parse("/cancel")
    assert isinstance(err, CliError) and err.code == E_MISSING_ARG


def test_parse_cancel_rejects_non_numeric_target():
    err = parse("/cancel PTT")
    assert isinstance(err, CliError) and err.code == E_BAD_TARGET


def test_parse_cancel_rejects_all_brokers():
    err = parse("/cancel 04050 all")
    assert isinstance(err, CliError) and err.code == E_BAD_BROKER


# --------------------------------------------------------------------------- #
# Cards
# --------------------------------------------------------------------------- #
def _bubbles(msg):
    return msg["contents"]["contents"]


def _footer(bubble):
    return (bubble.get("footer") or {}).get("contents") or []


def test_orders_cards_show_cancel_button_only_for_resting_orders():
    msgs = cards.orders_cancel_cards(
        broker="kss",
        orders=[_row(), _row(order_no="04051", symbol="TPIPP", kind="matched",
                             status="Matched", balance=0, cancellable=False)],
        snapshot_label="10:41:23",
    )
    assert len(msgs) == 1
    bubbles = _bubbles(msgs[0])
    assert len(bubbles) == 1                      # one card per stock, not per order
    buttons = _footer(bubbles[0])
    assert len(buttons) == 1                      # matched order gets no button
    assert buttons[0]["action"]["type"] == "postback"
    assert "order_no=04050" in buttons[0]["action"]["data"]
    assert buttons[0]["action"]["data"].startswith("action=order_cancel_request")


def test_orders_cards_group_by_symbol():
    msgs = cards.orders_cancel_cards(
        broker="lib",
        orders=[_row(broker="lib", symbol="BCPG"), _row(broker="lib", symbol="PTT", order_no="7")],
        snapshot_label="10:41:23",
    )
    bubbles = _bubbles(msgs[0])
    assert len(bubbles) == 2                       # BCPG + PTT, one bubble each
    titles = [b["header"]["contents"][0]["contents"][0]["text"] for b in bubbles]
    assert any("BCPG" in t for t in titles) and any("PTT" in t for t in titles)


def test_orders_cards_empty_ledger_falls_back_to_empty_orders_card():
    msgs = cards.orders_cancel_cards(broker="kss", orders=[], snapshot_label="10:41:23")
    assert len(msgs) == 1 and "altText" in msgs[0]


def test_confirm_card_carries_both_buttons():
    c = cards.order_cancel_confirm_card(
        draft_id="DRAFT-X", broker="kss", symbol="TPIPP", side="SELL", order_no="04050",
        volume=100, price=3.2, status_label="Queuing", ttl_seconds=180)
    labels = [b["action"]["label"] for b in _footer(c["contents"])]
    assert labels == ["✅ ยืนยันยกเลิก", "↩️ ไม่ยกเลิก"]


# --------------------------------------------------------------------------- #
# /cancel handler
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cancel_drafts_then_confirms_once(monkeypatch):
    monkeypatch.setattr(H, "_broker_session_alive", AsyncMock(return_value=True))
    monkeypatch.setattr(H, "_scrape_order_rows", AsyncMock(return_value=[_row()]))
    cancel = AsyncMock(return_value={"success": True, "verified": True, "path": "STREAMING_CDP"})
    monkeypatch.setattr(H, "_broker_cancel", cancel)

    svc = _service()
    parsed = parse("/cancel 04050 kss")
    res = await H._handle_cancel(parsed, svc, "rt", None)
    assert res["status"] == "ORDER_CANCEL_DRAFTED"
    draft_id = res["draft_id"]
    assert H._DRAFTS[draft_id]["kind"] == "cancel"
    cancel.assert_not_awaited()                 # nothing pressed until the ✅ tap

    res2 = await H._handle_order_cancel_postback(
        "order_cancel_confirm", {"draft_id": draft_id}, svc, "rt", None)
    assert res2["status"] == "ORDER_CANCELLED"
    cancel.assert_awaited_once_with("kss", order_no="04050", symbol="TPIPP")

    # replay: the draft is popped, so a second tap can never cancel twice
    res3 = await H._handle_order_cancel_postback(
        "order_cancel_confirm", {"draft_id": draft_id}, svc, "rt", None)
    assert res3["status"] == "ORDER_CANCEL_DRAFT_EXPIRED"
    assert cancel.await_count == 1


@pytest.mark.asyncio
async def test_cancel_refuses_when_session_down(monkeypatch):
    monkeypatch.setattr(H, "_broker_session_alive", AsyncMock(return_value=False))
    scrape = AsyncMock()
    monkeypatch.setattr(H, "_scrape_order_rows", scrape)

    res = await H._handle_cancel(parse("/cancel 04050 kss"), _service(), "rt", None)
    assert res["status"] == "CLI_SESSION_DOWN"
    scrape.assert_not_awaited()                 # Invariant 7 — never auto-login, never guess


@pytest.mark.asyncio
async def test_cancel_reports_unknown_order(monkeypatch):
    monkeypatch.setattr(H, "_broker_session_alive", AsyncMock(return_value=True))
    monkeypatch.setattr(H, "_scrape_order_rows", AsyncMock(return_value=[]))
    res = await H._handle_cancel(parse("/cancel 99999"), _service(), "rt", None)
    assert res["status"] == "ORDER_CANCEL_NOT_FOUND"


@pytest.mark.asyncio
async def test_cancel_refuses_matched_order(monkeypatch):
    monkeypatch.setattr(H, "_broker_session_alive", AsyncMock(return_value=True))
    monkeypatch.setattr(H, "_scrape_order_rows", AsyncMock(return_value=[
        _row(kind="matched", status="Matched", balance=0, cancellable=False)]))
    res = await H._handle_cancel(parse("/cancel 04050 kss"), _service(), "rt", None)
    assert res["status"] == "ORDER_CANCEL_NOT_CANCELLABLE"
    assert not H._DRAFTS


@pytest.mark.asyncio
async def test_cancel_ambiguous_across_brokers(monkeypatch):
    monkeypatch.setattr(H, "_broker_session_alive", AsyncMock(return_value=True))
    monkeypatch.setattr(H, "_scrape_order_rows", AsyncMock(return_value=[_row()]))
    res = await H._handle_cancel(parse("/cancel 04050"), _service(), "rt", None)
    assert res["status"] == "ORDER_CANCEL_AMBIGUOUS"


@pytest.mark.asyncio
async def test_confirm_fails_closed_when_order_disappeared(monkeypatch):
    monkeypatch.setattr(H, "_broker_session_alive", AsyncMock(return_value=True))
    monkeypatch.setattr(H, "_scrape_order_rows", AsyncMock(return_value=[_row()]))
    cancel = AsyncMock(return_value={"success": True, "verified": True})
    monkeypatch.setattr(H, "_broker_cancel", cancel)

    svc = _service()
    res = await H._handle_cancel(parse("/cancel 04050 kss"), svc, "rt", None)
    # order matched between the card and the tap
    monkeypatch.setattr(H, "_scrape_order_rows", AsyncMock(return_value=[]))
    res2 = await H._handle_order_cancel_postback(
        "order_cancel_confirm", {"draft_id": res["draft_id"]}, svc, "rt", None)
    assert res2["status"] == "ORDER_CANCEL_ALREADY_GONE"
    cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_unverified_cancel_is_reported_as_failure(monkeypatch):
    monkeypatch.setattr(H, "_broker_session_alive", AsyncMock(return_value=True))
    monkeypatch.setattr(H, "_scrape_order_rows", AsyncMock(return_value=[_row()]))
    monkeypatch.setattr(H, "_broker_cancel", AsyncMock(return_value={
        "success": True, "verified": False, "error": "anti-mock re-read failed"}))

    svc = _service()
    res = await H._handle_cancel(parse("/cancel 04050 kss"), svc, "rt", None)
    res2 = await H._handle_order_cancel_postback(
        "order_cancel_confirm", {"draft_id": res["draft_id"]}, svc, "rt", None)
    assert res2["status"] == "ORDER_CANCEL_FAILED"


@pytest.mark.asyncio
async def test_abort_keeps_the_order(monkeypatch):
    H._store_draft({"draft_id": "DRAFT-AB", "created_at": time.time(), "kind": "cancel",
                    "broker": "kss", "order_no": "04050", "symbol": "TPIPP",
                    "side": "SELL", "volume": 100, "price": 3.2})
    cancel = AsyncMock()
    monkeypatch.setattr(H, "_broker_cancel", cancel)
    res = await H._handle_order_cancel_postback(
        "order_cancel_abort", {"draft_id": "DRAFT-AB"}, _service(), "rt", None)
    assert res["status"] == "ORDER_CANCEL_ABORTED"
    cancel.assert_not_awaited()


@pytest.mark.asyncio
async def test_order_cancel_request_postback_opens_confirm_card(monkeypatch):
    monkeypatch.setattr(H, "_broker_session_alive", AsyncMock(return_value=True))
    monkeypatch.setattr(H, "_scrape_order_rows", AsyncMock(return_value=[_row()]))
    svc = _service()
    res = await H._handle_order_cancel_postback(
        "order_cancel_request", {"broker": "kss", "order_no": "04050"}, svc, "rt", None)
    assert res["status"] == "ORDER_CANCEL_DRAFTED"
    assert svc.pushes or svc._send_reply.await_count


# --------------------------------------------------------------------------- #
# /orders → per-stock cards
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_live_cards_orders_returns_per_stock_carousel(monkeypatch):
    monkeypatch.setattr(H, "_scrape_order_rows", AsyncMock(return_value=[
        _row(), _row(order_no="7", symbol="PTT", side="BUY")]))
    msgs = await H._live_cards("orders", "kss")
    assert len(msgs) >= 1
    assert len(_bubbles(msgs[0])) == 2


@pytest.mark.asyncio
async def test_live_cards_orders_failclosed_on_scrape_error(monkeypatch):
    monkeypatch.setattr(H, "_scrape_order_rows", AsyncMock(side_effect=RuntimeError("cdp down")))
    msgs = await H._live_cards("orders", "kss")
    assert len(msgs) == 1 and "altText" in msgs[0]
