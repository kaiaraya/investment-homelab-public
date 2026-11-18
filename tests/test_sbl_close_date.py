"""Close Date governance for the SBL borrow book.

RCA 2026-11-06: the broker keeps returned/expired contracts in the grid with a
`Close Date` and no return action. Both the ledger scrape and the reconciler read
only `Quantity`, so a returned loan kept reporting its full size as outstanding:

* MINT B494819 was really returned at 13:29 (popup "Save Successfull", Close Date
  06/11/2026) yet `/port kss` still offered `🔄 คืนหุ้น 2,400` — and pressing it
  would have posted a second return for an already closed contract.
* `verify_return_submitted` could never confirm a genuine return for the same
  reason, so every live attempt ended as SBL_RETURN_UNVERIFIED.

Rule pinned here: a row with a non-empty Close Date is CLOSED, remaining_qty 0,
and must never be re-submitted.
"""
from __future__ import annotations

import asyncio

import pytest

from app.brokers.krungsri.sbl_realtime_driver import KrungsriSBLRealtimeDriver

LEDGER_ROWS = [
    # returned today — Close Date filled, no return link any more
    {"ref_no": "B494819", "tran_date": "03/11/2026", "symbol": "MINT",
     "quantity": 2400, "borrow_price": 20.5, "fee_rate": 3.5,
     "close_date": "06/11/2026", "has_return_button": False},
    # still open — no Close Date, return link present
    {"ref_no": "B494759", "tran_date": "02/11/2026", "symbol": "TPIPP",
     "quantity": 32000, "borrow_price": 1.58, "fee_rate": 3.5,
     "close_date": "", "has_return_button": True},
]


def _driver_with(rows, monkeypatch):
    drv = KrungsriSBLRealtimeDriver()

    async def fake_scrape_full_pool(force_browser=False, **_kw):
        return {"success": True, "source": "sbl_read_cdp",
                "active_borrow_transactions": rows}

    monkeypatch.setattr(drv, "scrape_full_pool", fake_scrape_full_pool)
    return drv


def test_closed_contract_reports_zero_remaining(monkeypatch):
    drv = _driver_with(LEDGER_ROWS, monkeypatch)
    lots = asyncio.run(drv.scrape_sbl_lots(force_refresh=True))
    by_ref = {l["broker_tx_id"]: l for l in lots}

    assert by_ref["B494819"]["status"] == "CLOSED"
    assert by_ref["B494819"]["remaining_qty"] == 0
    assert by_ref["B494819"]["borrow_qty"] == 2400       # size is kept for history
    assert by_ref["B494819"]["close_date"] == "06/11/2026"

    assert by_ref["B494759"]["status"] == "ACTIVE"
    assert by_ref["B494759"]["remaining_qty"] == 32000


def test_verify_accepts_closed_contract(monkeypatch):
    drv = _driver_with(LEDGER_ROWS, monkeypatch)
    ok, proof = asyncio.run(
        drv.verify_return_submitted(lot_id="SBL-MINT-B494819", symbol="MINT", return_qty=2400)
    )
    assert ok is True
    assert proof["remaining"] == 0
    assert proof["reduced"] == 2400


def test_verify_rejects_still_open_contract(monkeypatch):
    open_only = [dict(LEDGER_ROWS[1])]           # TPIPP, untouched
    drv = _driver_with(open_only, monkeypatch)
    ok, proof = asyncio.run(
        drv.verify_return_submitted(lot_id="SBL-TPIPP-B494759", symbol="TPIPP", return_qty=32000)
    )
    assert ok is False
    assert proof["remaining"] == 32000


def test_verify_treats_vanished_lot_as_returned(monkeypatch):
    drv = _driver_with([dict(LEDGER_ROWS[1])], monkeypatch)
    ok, proof = asyncio.run(
        drv.verify_return_submitted(lot_id="SBL-MINT-B494819", symbol="MINT", return_qty=2400)
    )
    assert ok is True and proof["gone"] is True


# --- Broker refusal must be quoted, not swallowed -------------------------------
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def test_sbl_return_snippet_surfaces_radalert_reason():
    """A refused return is HTTP 200 + ``radalert('...')`` — the dialog is the truth.

    The first live TIDLOR attempt reported "no success banner in response", which
    hid the actual reason ('จำนวนที่ต้องการคืนมากกว่าจำนวนที่สามารถคืนได้') and made
    it look like a network problem.
    """
    src = (ROOT / "data/bookmarklets/sbl_return.js").read_text(encoding="utf-8")
    assert "radalert" in src
    assert "broker_message" in src
    assert "NOT_RETURNABLE" in src
    # a refused submit is reported, never retried inside the snippet
    assert "notReturnable" in src


def test_handler_reports_not_returnable_with_reason():
    src = (ROOT / "app/services/line_cli/handlers.py").read_text(encoding="utf-8")
    assert '"NOT_RETURNABLE"' in src
    assert "SBL_RETURN_NOT_RETURNABLE" in src
    block = src.split('res.get("status") == "NOT_RETURNABLE"')[1][:600]
    assert "โบรกยังไม่ให้คืน" in block          # names the symbol up front
    assert "ยังไม่มีการส่งคำขอใด ๆ" in block     # tells the human nothing was sent


def test_verify_refuses_symbol_only_ref_fails_closed(monkeypatch):
    """If lot_id is blank or just the ticker symbol, verify_return_submitted must fail closed (never gone=True)."""
    drv = _driver_with(LEDGER_ROWS, monkeypatch)
    ok, proof = asyncio.run(
        drv.verify_return_submitted(lot_id="TPIPP", symbol="TPIPP", return_qty=1000)
    )
    assert ok is False
    assert "missing concrete broker ref_no" in str(proof.get("error", ""))


def test_plan_fifo_return_lots_multi_lot_and_all():
    from app.brokers.krungsri.sbl_realtime_driver import plan_fifo_return_lots

    lots = [
        {"broker_tx_id": "B495100", "symbol": "AAV", "remaining_qty": 30000, "borrow_date": "09/11/2026", "time": "11:00:00", "status": "ACTIVE"},
        {"broker_tx_id": "B495090", "symbol": "AAV", "remaining_qty": 50000, "borrow_date": "09/11/2026", "time": "10:32:28", "status": "ACTIVE"},
        {"broker_tx_id": "B495010", "symbol": "AAV", "remaining_qty": 0, "borrow_date": "05/11/2026", "close_date": "08/11/2026", "status": "CLOSED"},
    ]
    # Partial FIFO spans oldest first (B495090 50k -> B495100 10k)
    p1 = plan_fifo_return_lots(lots, symbol="AAV", return_qty=60000)
    assert [(x["ref_no"], x["qty"]) for x in p1] == [("B495090", 50000), ("B495100", 10000)]

    # All open lots without specifying Ref.no
    p_all = plan_fifo_return_lots(lots, symbol="AAV", return_qty=None)
    assert [(x["ref_no"], x["qty"]) for x in p_all] == [("B495090", 50000), ("B495100", 30000)]

    # Specific lot_id (optional) targets only that contract
    p_ref = plan_fifo_return_lots(lots, symbol="AAV", return_qty=10000, lot_id="B495100")
    assert [(x["ref_no"], x["qty"]) for x in p_ref] == [("B495100", 10000)]

    # Over-request fails loudly
    with pytest.raises(ValueError, match="INSUFFICIENT_SBL_LOT"):
        plan_fifo_return_lots(lots, symbol="AAV", return_qty=90000)
