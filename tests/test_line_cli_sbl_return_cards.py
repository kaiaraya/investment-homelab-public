"""SBL Return / Short on the LINE portfolio card — the covered-loan case.

RCA 2026-11-06: MINT's Cover matched 2,400 @21.30 and the position went to 0, but the
loan stayed open. The card rendered `MINT (S) · 0 หุ้น` with a Cover button for 0 shares
and no way to Return, and borrowed-only symbols (BCPG 100) never reached the card at all
because they lived only in the broker's borrow book, not in the DGW position list.
These tests pin the four states and the numbers each one is allowed to show.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.line_cli import cards, handlers


LIVE_LOTS = [
    {"broker_tx_id": "B494819", "symbol": "MINT", "borrow_qty": 2400,
     "remaining_qty": 2400, "borrow_fee_rate": 3.5},
    {"broker_tx_id": "B495035", "symbol": "BCPG", "borrow_qty": 100,
     "remaining_qty": 100, "borrow_fee_rate": 3.5},
    {"broker_tx_id": "B494903", "symbol": "JAS", "borrow_qty": 75700,
     "remaining_qty": 75700, "borrow_fee_rate": 3.5},
]

HOLDINGS = [
    {"symbol": "JAS", "position_type": "SHORT", "volume": 75700,
     "avg_cost": 0.66888, "current_price": 0.66},
    {"symbol": "MINT", "position_type": "SHORT", "volume": 0,
     "avg_cost": 20.66522, "current_price": 21.0},
]


def _texts(node, out=None):
    out = [] if out is None else out
    if isinstance(node, dict):
        if node.get("type") == "text":
            out.append(node.get("text", ""))
        if node.get("type") == "button":
            out.append(f"<<{node['action']['label']}={node['action'].get('text')}>>")
        for v in node.values():
            _texts(v, out)
    elif isinstance(node, list):
        for v in node:
            _texts(v, out)
    return out


def _card_text(row, borrowed=None):
    msgs = cards.portfolio_stock_cards(
        brokers_data=[{"broker": "kss", "rows": [row], "borrowed": borrowed or [], "actionable": True}])
    return " | ".join(_texts(msgs))


# --- state machine ---------------------------------------------------------------

@pytest.mark.parametrize("args,expected", [
    (("SHORT", 3000, 3000, 0), "OPEN_SHORT"),
    (("SHORT", 0, 2400, 2400), "COVERED_AWAIT_RETURN"),
    (("SHORT", 0, 0, 0), "CLOSED"),
    (("BORROWED", 0, 100, 100), "BORROW_ONLY"),
    (("LONG", 500, 0, 0), "LONG"),
])
def test_position_state(args, expected):
    assert cards._position_state(*args) == expected


# --- handlers: merge / split / normalise -----------------------------------------

def test_covered_zero_volume_row_survives_normalise():
    rows = handlers._normalise_holdings(HOLDINGS, keep_zero={"MINT"})
    assert [r["symbol"] for r in rows] == ["JAS", "MINT"]


def test_merge_borrow_sets_returnable_and_adds_orphan_symbol():
    summary = handlers._sbl_borrow_summary(LIVE_LOTS)
    rows = handlers._merge_borrow(handlers._normalise_holdings(HOLDINGS, keep_zero=summary), summary)
    by_sym = {r["symbol"]: r for r in rows}
    assert by_sym["MINT"]["returnable_qty"] == 2400        # nothing sold → all returnable
    assert by_sym["JAS"]["returnable_qty"] == 0            # fully sold → must cover first
    assert by_sym["BCPG"]["position_type"] == "BORROWED"   # borrowed but no position
    assert by_sym["BCPG"]["returnable_qty"] == 100


def test_split_borrowed_keeps_covered_loan():
    summary = handlers._sbl_borrow_summary(LIVE_LOTS)
    rows = handlers._merge_borrow(handlers._normalise_holdings(HOLDINGS, keep_zero=summary), summary)
    main, borrowed = handlers._split_borrowed(rows)
    assert "MINT" in [r["symbol"] for r in main]                      # covered row stays visible
    assert [(b["symbol"], b["borrow_qty"], b["returnable_qty"]) for b in borrowed] == [("BCPG", 100, 100)]


def test_merge_borrow_without_book_is_noop():
    rows = handlers._merge_borrow(handlers._normalise_holdings(HOLDINGS), {})
    assert all(r["returnable_qty"] == 0 for r in rows)
    # With no borrow book there is nothing to return, so the covered zero row is
    # dropped as before — only the live short survives.
    assert [r["symbol"] for r in rows] == ["JAS"]


# --- cards -----------------------------------------------------------------------

def test_covered_card_offers_return_and_no_zero_cover():
    txt = _card_text({"symbol": "MINT", "position_type": "SHORT", "volume": 0,
                      "borrow_qty": 2400, "returnable_qty": 2400, "borrow_lots": 1,
                      "broker": "kss"})
    assert "Cover ครบแล้ว" in txt
    assert "<<🔄 คืนหุ้น=/return MINT all>>" in txt
    assert "/cover kss MINT 0" not in txt


def test_open_short_with_sold_shares_has_no_return_button():
    txt = _card_text({"symbol": "JAS", "position_type": "SHORT", "volume": 75700,
                      "borrow_qty": 75700, "returnable_qty": 0, "broker": "kss",
                      "cost": 0.66, "last": 0.64, "pnl_pct": 3.0})
    assert "/cover kss JAS 75700" in txt
    assert "/return JAS" not in txt


def test_borrow_only_card_offers_short_and_return():
    txt = _card_text({"symbol": "BCPG", "position_type": "BORROWED", "volume": 0,
                      "borrow_qty": 100, "returnable_qty": 100, "broker": "kss"})
    assert "/short kss BCPG 100" in txt
    assert "/return BCPG all" in txt


def test_partial_return_button_names_the_quantity():
    txt = _card_text({"symbol": "TPIPP", "position_type": "SHORT", "volume": 10000,
                      "borrow_qty": 32000, "returnable_qty": 22000, "broker": "kss"})
    assert "/cover kss TPIPP 10000" in txt
    assert "/return TPIPP 22000" in txt


# --- driver verification ---------------------------------------------------------

def test_verify_return_requires_broker_grid_to_agree(monkeypatch):
    from app.brokers.krungsri.sbl_realtime_driver import krungsri_sbl_driver as drv

    async def gone(**_kw):
        return []

    async def reduced(**_kw):
        return [{"broker_tx_id": "B494819", "symbol": "MINT", "borrow_qty": 2400, "remaining_qty": 1000}]

    async def unchanged(**_kw):
        return [{"broker_tx_id": "B494819", "symbol": "MINT", "borrow_qty": 2400, "remaining_qty": 2400}]

    monkeypatch.setattr(drv, "scrape_sbl_lots", gone)
    ok, proof = asyncio.run(drv.verify_return_submitted(lot_id="SBL-MINT-B494819", symbol="MINT", return_qty=2400))
    assert ok and proof["gone"] is True

    monkeypatch.setattr(drv, "scrape_sbl_lots", reduced)
    ok, proof = asyncio.run(drv.verify_return_submitted(lot_id="SBL-MINT-B494819", symbol="MINT", return_qty=1400))
    assert ok and proof["reduced"] == 1400

    monkeypatch.setattr(drv, "scrape_sbl_lots", unchanged)
    ok, _ = asyncio.run(drv.verify_return_submitted(lot_id="SBL-MINT-B494819", symbol="MINT", return_qty=2400))
    assert ok is False            # HTTP 200 alone never counts as returned
