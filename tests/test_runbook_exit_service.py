"""Tests for Pure-CDP Runbook Auto-Exit Bridge V2 (Auto-Daily + All-or-Nothing Gate)."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import random
from types import SimpleNamespace
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

import pytest

from app.services import runbook_exit_service as rb_mod
from app.services.broker_cdp import trading_facade
from app.services.runbook_exit_service import RunbookExitError, RunbookExitService

_BKK = ZoneInfo("Asia/Bangkok")


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _sample_ledger(as_of: str = "2026-11-12") -> Dict[str, Any]:
    return {
        "as_of": as_of,
        "statement_through": as_of,
        "positions": [
            # Valid KSS COVER (should be included)
            {
                "segment": "managed",
                "broker": "KSS",
                "symbol": "BAM",
                "direction": "SHORT",
                "action": "COVER",
                "shares": 12000,
                "entry_price": 7.65,
                "current_price": 7.80,
                "unrealized_pct": -1.96,
                "atr_multiple": 0.4,
                "days_held": 3,
                "exit_reason": "CS_LOSER_FLIP_CUT",
                "recommended_action": "COVER 12,000",
            },
            # Valid LIBERATOR SELL (should be included)
            {
                "segment": "managed",
                "broker": "LIBERATOR",
                "symbol": "PTT",
                "direction": "LONG",
                "action": "SELL",
                "shares": 500,
                "entry_price": 34.0,
                "current_price": 32.5,
                "unrealized_pct": -4.41,
                "atr_multiple": 2.1,
                "days_held": 5,
                "exit_reason": "ATR_STOP",
                "recommended_action": "SELL 500",
            },
            # Excluded: HOLD
            {
                "segment": "managed",
                "broker": "LIBERATOR",
                "symbol": "AOT",
                "direction": "LONG",
                "action": "HOLD",
                "shares": 1000,
                "exit_reason": "",
            },
            # Excluded: WEED_OUT_WATCHLIST
            {
                "segment": "managed",
                "broker": "KSS",
                "symbol": "ADVANC",
                "direction": "SHORT",
                "action": "COVER",
                "shares": 200,
                "exit_reason": "WEED_OUT_WATCHLIST",
            },
            # Excluded: legacy_dormant
            {
                "segment": "legacy_dormant",
                "broker": "KSS",
                "symbol": "KBANK",
                "direction": "SHORT",
                "action": "COVER",
                "shares": 300,
                "exit_reason": "LEGACY_EXIT",
            },
            # Excluded: unmatched_exit
            {
                "segment": "unmatched_exit",
                "broker": "LIBERATOR",
                "symbol": "SCB",
                "direction": "LONG",
                "action": "SELL",
                "shares": 400,
                "exit_reason": "ORPHAN",
            },
            # Excluded: NOMURA broker even if SELL/COVER
            {
                "segment": "managed",
                "broker": "NOMURA",
                "symbol": "CPF",
                "direction": "LONG",
                "action": "SELL",
                "shares": 2000,
                "exit_reason": "STOP",
            },
            # Excluded: 0 shares
            {
                "segment": "managed",
                "broker": "KSS",
                "symbol": "BBL",
                "direction": "SHORT",
                "action": "COVER",
                "shares": 0,
                "exit_reason": "ZERO",
            },
        ],
    }


def _make_svc(
    tmp_path: Path,
    now_dt: datetime,
    ledger: Dict[str, Any] | None = None,
    holidays: Dict[str, str] | None = None,
):
    clock = {"dt": now_dt}
    ledger_path = tmp_path / "positions_ledger.json"
    state_path = tmp_path / "runbook_exit_state.json"
    holidays_path = tmp_path / "holidays_index.json"
    if ledger is not None:
        _write_json(ledger_path, ledger)
    _write_json(holidays_path, {"market": "SET", "dates": holidays or {}})
    svc = RunbookExitService(
        ledger_path=str(ledger_path),
        state_path=str(state_path),
        holidays_path=str(holidays_path),
        clock_fn=lambda: clock["dt"],
        rng=random.Random(17),
    )
    return svc, clock, state_path, ledger_path


def _patch_quotes_and_otp(monkeypatch, *, quote_ok: bool = True):
    async def fake_quote(broker: str, symbol: str, side: str = None, old_price=None):
        if not quote_ok:
            return {"available": False, "error": f"{broker.upper()} Quote ไม่พร้อม", "lure_plan": {"ok": False}}
        px = 7.80 if symbol == "BAM" else 32.50
        return {
            "available": True,
            "bid_1": px - 0.05,
            "offer_1": px,
            "last": px,
            "lure_plan": {"ok": True, "price": px},
        }

    async def fake_otp_check():
        return SimpleNamespace(ready=True, channel="miniapp", reason=None)

    monkeypatch.setattr(trading_facade, "compute_nego_quote", fake_quote)
    monkeypatch.setattr(rb_mod.otp_relay_readiness, "check", fake_otp_check)
    monkeypatch.setattr(rb_mod.pin_guard, "check", lambda b: {"locked": False})


@pytest.mark.asyncio
async def test_runbook_exit_filters_strictly_and_aggregates(monkeypatch, tmp_path):
    now = datetime(2026, 11, 12, 9, 15, tzinfo=_BKK)
    ledger = _sample_ledger("2026-11-12")
    ledger["positions"].append(
        {
            "segment": "managed",
            "broker": "KSS",
            "symbol": "BAM",
            "direction": "SHORT",
            "action": "COVER",
            "shares": 3000,
            "exit_reason": "SECOND_LOT",
        }
    )
    svc, _, _, _ = _make_svc(tmp_path, now, ledger)
    _patch_quotes_and_otp(monkeypatch, quote_ok=True)
    monkeypatch.setattr(rb_mod, "_is_dry_mode", lambda: True)

    async def fake_probe(needed):
        return list(needed), {}, []

    async def fake_port(broker: str = "all"):
        return {
            "brokers": {
                "kss": {"available": True, "rows": [{"symbol": "BAM", "position_type": "SHORT", "volume": 15000}]},
                "lib": {"available": True, "rows": [{"symbol": "PTT", "position_type": "LONG", "volume": 500}]},
            }
        }

    async def fake_orders(broker: str = "all"):
        return {"brokers": {"kss": {"available": True}, "lib": {"available": True}}, "orders": []}

    monkeypatch.setattr(rb_mod, "probe_ready_brokers", fake_probe)
    monkeypatch.setattr(trading_facade, "fetch_portfolio_overview", fake_port)
    monkeypatch.setattr(trading_facade, "fetch_orders_overview", fake_orders)

    prev = await svc.preview_runbook_exits()
    assert prev["ledger_available"] is True
    assert prev["is_today"] is True
    assert prev["actionable_count"] == 2
    assert prev["readiness"]["all_ready"] is True
    by_sym = {it["symbol"]: it for it in prev["items"]}
    assert set(by_sym.keys()) == {"BAM", "PTT"}
    assert by_sym["BAM"]["runbook_volume"] == 15000
    assert by_sym["BAM"]["lure_price"] == pytest.approx(7.60)
    assert by_sym["PTT"]["lure_price"] == pytest.approx(33.50)


@pytest.mark.asyncio
async def test_runbook_exit_stale_date_guard_trade_and_auto(monkeypatch, tmp_path):
    now = datetime(2026, 11, 12, 9, 20, tzinfo=_BKK)
    svc, clock, _, _ = _make_svc(tmp_path, now, _sample_ledger("2026-11-11"))
    _patch_quotes_and_otp(monkeypatch, quote_ok=True)
    monkeypatch.setattr(rb_mod, "_is_dry_mode", lambda: False)

    async def fake_probe(needed):
        return list(needed), {}, []

    async def fake_port(broker: str = "all"):
        return {
            "brokers": {
                "kss": {"available": True, "rows": [{"symbol": "BAM", "position_type": "SHORT", "volume": 12000}]},
                "lib": {"available": True, "rows": [{"symbol": "PTT", "position_type": "LONG", "volume": 500}]},
            }
        }

    async def fake_orders(broker: str = "all"):
        return {"brokers": {"kss": {"available": True}, "lib": {"available": True}}, "orders": []}

    executed_calls = []

    async def fake_exec(**kwargs):
        executed_calls.append(kwargs)
        return {"dry_run": False, "order": {"volume": kwargs["volume"], "price": 7.8}, "execution": {"broker_order_no": "1"}}

    async def fake_notify(state, *, error_key, **kw):
        seen = set(state.get("sent_error_keys") or [])
        if error_key in seen:
            return False
        seen.add(error_key)
        state["sent_error_keys"] = sorted(seen)
        return True

    monkeypatch.setattr(rb_mod, "probe_ready_brokers", fake_probe)
    monkeypatch.setattr(rb_mod, "notify_critical_error_once", fake_notify)
    monkeypatch.setattr(trading_facade, "fetch_portfolio_overview", fake_port)
    monkeypatch.setattr(trading_facade, "fetch_orders_overview", fake_orders)
    monkeypatch.setattr(trading_facade, "execute_trade_with_idempotency", fake_exec)

    # Auto schedule initialized -> triggers at randomized time -> skips stale Runbook with 0 orders
    snap = svc.get_schedule_snapshot(now)
    assert snap["auto_enabled"] is True
    assert snap["queue"]["status"] == "AUTO_PENDING"
    hh, mm = map(int, snap["queue"]["scheduled_time"].split(":"))
    clock["dt"] = datetime(2026, 11, 12, hh, mm, tzinfo=_BKK)
    res_auto = await svc.tick_once()
    assert res_auto is not None
    assert res_auto["queue"]["status"] == "SKIPPED_STALE_RUNBOOK"
    assert executed_calls == []

    # Manual Trade requires allow_stale=True when as_of != today
    clock["dt"] = datetime(2026, 11, 12, 9, 20, tzinfo=_BKK)
    svc2, _, _, _ = _make_svc(tmp_path / "t2", now, _sample_ledger("2026-11-11"))
    with pytest.raises(RunbookExitError) as exc_info:
        await svc2.enqueue_or_execute_exits(allow_stale=False)
    assert exc_info.value.code == "E_STALE_RUNBOOK"

    res = await svc2.enqueue_or_execute_exits(allow_stale=True)
    assert res["mode"] == "SCHEDULED"


@pytest.mark.asyncio
async def test_safe_net_remaining_guard_variants():
    item_sell = {"broker": "lib", "symbol": "PTT", "side": "sell", "runbook_volume": 1000}
    item_cover = {"broker": "kss", "symbol": "BAM", "side": "cover", "runbook_volume": 12000}

    rec = RunbookExitService._reconcile_item(
        item_cover,
        {"brokers": {"kss": {"available": True, "rows": [{"symbol": "BAM", "position_type": "SHORT", "volume": 7000}]}}},
        {
            "brokers": {"kss": {"available": True}},
            "orders": [
                {"broker": "kss", "symbol": "BAM", "side": "COVER", "kind": "open", "balance": 5000, "matched_volume": 0},
                {"broker": "kss", "symbol": "BAM", "side": "COVER", "kind": "matched", "balance": 0, "matched_volume": 7000},
                {"broker": "kss", "symbol": "BAM", "side": "SHORT", "kind": "open", "balance": 9999, "matched_volume": 0},
            ],
        },
    )
    assert rec["guard_status"] == "SKIPPED_ALREADY_ORDERED"
    assert rec["net_send_vol"] == 0

    rec = RunbookExitService._reconcile_item(
        item_sell,
        {"brokers": {"lib": {"available": True, "rows": [{"symbol": "PTT", "position_type": "SHORT", "volume": 1000}]}}},
        {"brokers": {"lib": {"available": True}}, "orders": []},
    )
    assert rec["guard_status"] == "SKIPPED_NOT_HELD"

    rec = RunbookExitService._reconcile_item(
        item_sell,
        {"brokers": {"lib": {"available": True, "rows": [{"symbol": "PTT", "position_type": "LONG", "volume": 850}]}}},
        {
            "brokers": {"lib": {"available": True}},
            "orders": [{"broker": "lib", "symbol": "PTT", "side": "SELL", "kind": "open", "balance": 200}],
        },
    )
    assert rec["guard_status"] == "ADJUSTED_VOL"
    assert rec["net_send_vol"] == 600

    rec = RunbookExitService._reconcile_item(
        item_sell,
        {"brokers": {"lib": {"available": True, "rows": [{"symbol": "PTT", "position_type": "LONG", "volume": 80}]}}},
        {"brokers": {"lib": {"available": True}}, "orders": []},
    )
    assert rec["guard_status"] == "SKIPPED_ODD_LOT"

    # Fail-closed: if KSS stayed on Cash (<REDACTED_KSS_ACCT>) with switch error, never report SKIPPED_NOT_HELD
    item_cover_margin = {**item_cover, "account": "margin"}
    rec_err = RunbookExitService._reconcile_item(
        item_cover_margin,
        {
            "brokers": {
                "kss": {
                    "available": True,
                    "account_no": "<REDACTED_KSS_ACCT>",
                    "error": "สลับบัญชีเป็น <REDACTED_KSS_ACCT> ไม่สำเร็จ",
                    "rows": [],
                }
            }
        },
        {"brokers": {"kss": {"available": True}}, "orders": []},
    )
    assert rec_err["guard_status"] == "BROKER_UNAVAILABLE"

    # Even without explicit error string, Margin cover reading non-margin account fails closed
    rec_wrong_acct = RunbookExitService._reconcile_item(
        item_cover_margin,
        {"brokers": {"kss": {"available": True, "account_no": "<REDACTED_KSS_ACCT>", "error": None, "rows": []}}},
        {"brokers": {"kss": {"available": True}}, "orders": []},
    )
    assert rec_wrong_acct["guard_status"] == "BROKER_UNAVAILABLE"


@pytest.mark.asyncio
async def test_auto_daily_dynamic_load_holiday_missed_and_toggle(monkeypatch, tmp_path):
    # 1. SET Holiday (2026-04-13 Songkran) -> HOLIDAY phase, no queue, toggle/run_now blocked
    holiday_dt = datetime(2026, 4, 13, 9, 10, tzinfo=_BKK)
    svc_h, _, _, _ = _make_svc(
        tmp_path / "h",
        holiday_dt,
        _sample_ledger("2026-04-13"),
        holidays={"2026-04-13": "วันสงกรานต์"},
    )
    snap_h = svc_h.get_schedule_snapshot(holiday_dt)
    assert snap_h["is_holiday"] is True
    assert snap_h["holiday_name"] == "วันสงกรานต์"
    assert snap_h["market_phase"] == "HOLIDAY"
    assert snap_h["queue"] is None

    # 2. Past 10:30 rollover on trading day -> MISSED_WINDOW (never late-fires automatically)
    late_dt = datetime(2026, 11, 12, 11, 15, tzinfo=_BKK)
    svc_late, _, _, _ = _make_svc(tmp_path / "late", late_dt, _sample_ledger("2026-11-12"))
    snap_late = svc_late.get_schedule_snapshot(late_dt)
    assert snap_late["queue"]["status"] == "MISSED_WINDOW"
    assert await svc_late.tick_once() is None

    # 3. Auto ON before 10:00 -> creates AUTO_PENDING even before ledger exists; reads fresh at 10:xx!
    early_dt = datetime(2026, 11, 12, 8, 30, tzinfo=_BKK)
    svc, clock, state_path, ledger_path = _make_svc(tmp_path / "auto", early_dt, ledger=None)
    _patch_quotes_and_otp(monkeypatch, quote_ok=True)
    monkeypatch.setattr(rb_mod, "_is_dry_mode", lambda: False)

    executed_calls: List[Dict[str, Any]] = []

    async def fake_probe(needed):
        return list(needed), {}, []

    async def fake_port(broker: str = "all"):
        return {
            "brokers": {
                "kss": {"available": True, "rows": [{"symbol": "BAM", "position_type": "SHORT", "volume": 12000}]},
                "lib": {"available": True, "rows": [{"symbol": "PTT", "position_type": "LONG", "volume": 500}]},
            }
        }

    async def fake_orders(broker: str = "all"):
        return {"brokers": {"kss": {"available": True}, "lib": {"available": True}}, "orders": []}

    async def fake_exec(**kwargs):
        executed_calls.append(kwargs)
        return {
            "dry_run": False,
            "order": {"volume": kwargs["volume"], "price": 7.8 if kwargs["symbol"] == "BAM" else 32.5},
            "execution": {"broker_order_no": f"AUTO-{kwargs['symbol']}"},
        }

    monkeypatch.setattr(rb_mod, "probe_ready_brokers", fake_probe)
    monkeypatch.setattr(trading_facade, "fetch_portfolio_overview", fake_port)
    monkeypatch.setattr(trading_facade, "fetch_orders_overview", fake_orders)
    monkeypatch.setattr(trading_facade, "execute_trade_with_idempotency", fake_exec)

    snap0 = svc.get_schedule_snapshot(early_dt)
    assert snap0["auto_enabled"] is True
    assert snap0["queue"]["status"] == "AUTO_PENDING"
    sched_t = snap0["queue"]["scheduled_time"]

    # Toggle OFF removes AUTO_PENDING, preserves auto_enabled=False across midnight
    off_snap = svc.set_auto_enabled(False, early_dt)
    assert off_snap["auto_enabled"] is False
    assert off_snap["queue"] is None

    clock["dt"] = datetime(2026, 11, 13, 8, 30, tzinfo=_BKK)
    assert svc.get_schedule_snapshot()["auto_enabled"] is False
    assert svc.get_schedule_snapshot()["queue"] is None

    # Toggle ON restores AUTO_PENDING; Runbook emits fresh ledger at 09:50 before random minute hits
    on_snap = svc.set_auto_enabled(True, clock["dt"])
    assert on_snap["queue"]["status"] == "AUTO_PENDING"
    sched_t = on_snap["queue"]["scheduled_time"]
    _write_json(ledger_path, _sample_ledger("2026-11-13"))

    hh, mm = map(int, sched_t.split(":"))
    clock["dt"] = datetime(2026, 11, 13, hh, mm, tzinfo=_BKK)
    done_snap = await svc.tick_once()
    assert done_snap is not None
    assert done_snap["queue"]["status"] == "DONE"
    assert done_snap["queue"]["line_status"] == "งดส่ง LINE (ดูผลใน Schedule)"
    assert {c["symbol"] for c in executed_calls} == {"BAM", "PTT"}
    px_by_sym = {c["symbol"]: c["price"] for c in executed_calls}
    assert px_by_sym["BAM"] == pytest.approx(7.60)
    assert px_by_sym["PTT"] == pytest.approx(33.50)
    assert all(c["notify_line"] is False for c in executed_calls)
    assert state_path.exists()


@pytest.mark.asyncio
async def test_all_or_nothing_gate_blocks_all_and_recovers_on_run_now(monkeypatch, tmp_path):
    now = datetime(2026, 11, 12, 10, 15, tzinfo=_BKK)
    svc, _, _, _ = _make_svc(tmp_path, now, _sample_ledger("2026-11-12"))
    monkeypatch.setattr(rb_mod, "_is_dry_mode", lambda: False)
    _patch_quotes_and_otp(monkeypatch, quote_ok=True)

    probe_state = {"ready": ["kss"], "errors": {"lib": "Session LIB หลุด (DISCONNECTED)"}}
    executed: List[str] = []
    alerts: List[str] = []

    async def fake_probe(needed):
        return list(probe_state["ready"]), dict(probe_state["errors"]), []

    async def fake_port(broker: str = "all"):
        return {
            "brokers": {
                "kss": {"available": True, "rows": [{"symbol": "BAM", "position_type": "SHORT", "volume": 12000}]},
                "lib": {"available": True, "rows": [{"symbol": "PTT", "position_type": "LONG", "volume": 500}]},
            }
        }

    async def fake_orders(broker: str = "all"):
        return {"brokers": {"kss": {"available": True}, "lib": {"available": True}}, "orders": []}

    async def fake_exec(**kwargs):
        executed.append(kwargs["symbol"])
        return {
            "dry_run": False,
            "order": {"volume": kwargs["volume"], "price": 7.80},
            "execution": {"broker_order_no": f"ORD-{kwargs['symbol']}"},
        }

    async def fake_notify(state, *, error_key, detail, **kw):
        seen = set(state.get("sent_error_keys") or [])
        if error_key in seen:
            return False
        seen.add(error_key)
        state["sent_error_keys"] = sorted(seen)
        alerts.append(detail)
        return True

    monkeypatch.setattr(rb_mod, "probe_ready_brokers", fake_probe)
    monkeypatch.setattr(rb_mod, "notify_critical_error_once", fake_notify)
    monkeypatch.setattr(trading_facade, "fetch_portfolio_overview", fake_port)
    monkeypatch.setattr(trading_facade, "fetch_orders_overview", fake_orders)
    monkeypatch.setattr(trading_facade, "execute_trade_with_idempotency", fake_exec)

    # Set trigger minute to 10:15 so tick_once fires immediately
    st = svc._load_or_init_state(now)
    st["queue"]["scheduled_time"] = "10:15"
    svc._save_state()

    blocked = await svc.tick_once(now)
    assert blocked is not None
    assert blocked["queue"]["status"] == "BLOCKED_NOT_READY"
    # Critical: All-or-Nothing means KSS (even though ready!) placed 0 orders!
    assert executed == []
    assert len(alerts) == 1

    # Quote failure on one item also blocks all 0 orders
    probe_state["ready"] = ["kss", "lib"]
    probe_state["errors"] = {}
    _patch_quotes_and_otp(monkeypatch, quote_ok=False)
    blocked2 = await svc.run_queue_now(now)
    assert blocked2["queue"]["status"] == "BLOCKED_NOT_READY"
    assert executed == []

    # Operator fixes quote/session and presses "รันตอนนี้" -> executes both cleanly
    _patch_quotes_and_otp(monkeypatch, quote_ok=True)
    recovered = await svc.run_queue_now(now)
    assert recovered["queue"]["status"] == "DONE"
    assert set(executed) == {"BAM", "PTT"}


@pytest.mark.asyncio
async def test_all_or_nothing_needed_brokers_only_and_trade_supersedes_auto(monkeypatch, tmp_path):
    now = datetime(2026, 11, 12, 9, 10, tzinfo=_BKK)
    only_kss_ledger = {
        "as_of": "2026-11-12",
        "statement_through": "2026-11-12",
        "positions": [_sample_ledger("2026-11-12")["positions"][0]],
    }
    svc, clock, _, _ = _make_svc(tmp_path, now, only_kss_ledger)
    monkeypatch.setattr(rb_mod, "_is_dry_mode", lambda: False)
    _patch_quotes_and_otp(monkeypatch, quote_ok=True)

    # LIB is stopped, only KSS is ready — since today only has KSS BAM, All-or-Nothing passes!
    async def fake_probe(needed):
        return ["kss"], {"lib": "Stopped"}, []

    async def fake_port(broker: str = "all"):
        return {
            "brokers": {
                "kss": {"available": True, "rows": [{"symbol": "BAM", "position_type": "SHORT", "volume": 12000}]},
            }
        }

    async def fake_orders(broker: str = "all"):
        return {"brokers": {"kss": {"available": True}}, "orders": []}

    executed: List[str] = []

    async def fake_exec(**kwargs):
        executed.append(kwargs["symbol"])
        return {"dry_run": False, "order": {"volume": kwargs["volume"], "price": 7.8}, "execution": {"broker_order_no": "KSS-1"}}

    monkeypatch.setattr(rb_mod, "probe_ready_brokers", fake_probe)
    monkeypatch.setattr(trading_facade, "fetch_portfolio_overview", fake_port)
    monkeypatch.setattr(trading_facade, "fetch_orders_overview", fake_orders)
    monkeypatch.setattr(trading_facade, "execute_trade_with_idempotency", fake_exec)

    # Trade submits manually pre-market -> supersedes AUTO_PENDING cleanly
    sub = await svc.enqueue_or_execute_exits(symbols=["BAM"])
    assert sub["mode"] == "SCHEDULED"
    assert sub["schedule"]["queue"]["queue_kind"] == "manual_trade"
    hh, mm = map(int, sub["scheduled_time"].split(":"))
    clock["dt"] = datetime(2026, 11, 12, hh, mm, tzinfo=_BKK)

    done = await svc.tick_once()
    assert done["queue"]["status"] == "DONE"
    assert executed == ["BAM"]
    # Calling tick_once again never re-triggers Auto
    assert await svc.tick_once() is None
    assert executed == ["BAM"]


def test_compute_runbook_exit_price_4_ticks_and_fallback():
    px_sell, src_sell = rb_mod.compute_runbook_exit_price("SELL", 4.98, 4.98)
    assert src_sell == "last+4t"
    assert px_sell == pytest.approx(5.15)

    px_cover, src_cover = rb_mod.compute_runbook_exit_price("COVER", 5.10, 5.10)
    assert src_cover == "last-4t"
    assert px_cover == pytest.approx(4.96)

    px_fb, src_fb = rb_mod.compute_runbook_exit_price("SELL", None, 10.20)
    assert src_fb == "lure_fallback"
    assert px_fb == pytest.approx(10.20)

    px_none, src_none = rb_mod.compute_runbook_exit_price("COVER", None, None)
    assert src_none == "missing"
    assert px_none is None


@pytest.mark.asyncio
async def test_manual_trade_acted_skips_auto_exit_duplicate_and_logs(monkeypatch, tmp_path, caplog):
    import logging

    now = datetime(2026, 11, 12, 9, 10, tzinfo=_BKK)
    svc, clock, _, _ = _make_svc(tmp_path, now, _sample_ledger("2026-11-12"))
    monkeypatch.setattr(rb_mod, "_is_dry_mode", lambda: False)
    _patch_quotes_and_otp(monkeypatch, quote_ok=True)

    # User manually sent COVER BAM via Trade tab before Auto-Exit runs (even if no open orders show)
    assert svc.record_manual_trade(broker="kss", symbol="BAM", side="cover", now=now) is True
    assert svc.record_manual_trade(broker="kss", symbol="BAM", side="cover", now=now) is False

    async def fake_probe(needed):
        return ["kss", "lib"], {}, []

    async def fake_port(broker: str = "all"):
        return {
            "brokers": {
                "kss": {"available": True, "account_no": "<REDACTED_KSS_ACCT>", "rows": [{"symbol": "BAM", "position_type": "SHORT", "volume": 12000}]},
                "lib": {"available": True, "rows": [{"symbol": "PTT", "position_type": "LONG", "volume": 1000}]},
            }
        }

    async def fake_orders(broker: str = "all"):
        return {"brokers": {"kss": {"available": True}, "lib": {"available": True}}, "orders": []}

    executed: List[str] = []

    async def fake_exec(**kwargs):
        executed.append(kwargs["symbol"])
        return {"dry_run": False, "order": {"volume": kwargs["volume"], "price": 35.0}, "execution": {"broker_order_no": "LIB-1"}}

    monkeypatch.setattr(rb_mod, "probe_ready_brokers", fake_probe)
    monkeypatch.setattr(trading_facade, "fetch_portfolio_overview", fake_port)
    monkeypatch.setattr(trading_facade, "fetch_orders_overview", fake_orders)
    monkeypatch.setattr(trading_facade, "execute_trade_with_idempotency", fake_exec)

    snap_before = svc.get_schedule_snapshot()
    hh, mm = map(int, snap_before["queue"]["scheduled_time"].split(":"))
    clock["dt"] = datetime(2026, 11, 12, hh, mm, tzinfo=_BKK)

    with caplog.at_level(logging.INFO):
        done = await svc.tick_once()

    assert done is not None
    assert done["queue"]["status"] == "DONE"
    # Only PTT (LIB) is sent; BAM (KSS) is skipped because user already acted manually!
    assert executed == ["PTT"]
    bam_item = [x for x in done["queue"]["items"] if x["symbol"] == "BAM"][0]
    assert bam_item["outcome"] == "SKIPPED_ALREADY_ORDERED"
    assert "ผู้ใช้ส่งคำสั่งเองไปแล้ววันนี้" in bam_item["reason"]
    assert any("Triggering Runbook Auto-Exit" in rec.message for rec in caplog.records)
