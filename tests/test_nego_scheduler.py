from __future__ import annotations

from datetime import datetime
import random
from zoneinfo import ZoneInfo

import pytest

from app.services import nego_scheduler as mod
from app.services.broker_cdp import trading_facade

_BKK = ZoneInfo("Asia/Bangkok")


def _bkk(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=_BKK)


def _make_scheduler(tmp_path, clock_box, seed: int = 42):
    state_file = tmp_path / "nego_scheduler_state.json"
    return mod.NegoScheduler(
        state_path=str(state_file),
        check_interval=10,
        clock_fn=lambda: clock_box[0],
        rng=random.Random(seed),
    )


def test_default_schedule_random_times_and_restart_persistence(tmp_path):
    clock = [_bkk(2026, 11, 12, 9, 50)]  # Thursday
    sched = _make_scheduler(tmp_path, clock)
    snap = sched.get_schedule_snapshot()

    assert snap["master_enabled"] is True
    by_id = {w["id"]: w for w in snap["windows"]}
    assert "w1_open" not in by_id
    assert by_id["w2_morning"]["enabled"] is True
    assert by_id["w3_lunch"]["enabled"] is False
    assert by_id["w3_lunch"]["tradable"] is False
    assert by_id["w4_afternoon"]["enabled"] is True
    assert by_id["w5_close"]["enabled"] is True
    assert by_id["w5_close"]["allowed_sides"] == ["BUY", "SELL", "SHORT", "COVER"]
    assert by_id["w6_post_close"]["enabled"] is True
    assert by_id["w6_post_close"]["mode"] == "post_close"

    # Random times strictly inside [start+1m, end-2m] (and 16:40..16:55 for w6_post_close)
    t2 = by_id["w2_morning"]["run"]["scheduled_time"]
    t4 = by_id["w4_afternoon"]["run"]["scheduled_time"]
    t5 = by_id["w5_close"]["run"]["scheduled_time"]
    t6 = by_id["w6_post_close"]["run"]["scheduled_time"]
    assert "10:31" <= t2 <= "12:28"
    assert "14:01" <= t4 <= "15:58"
    assert "16:01" <= t5 <= "16:28"
    assert "16:40" <= t6 <= "16:55"

    # Restart simulation: new instance reading same JSON keeps identical random times
    sched2 = _make_scheduler(tmp_path, clock, seed=999)
    snap2 = sched2.get_schedule_snapshot()
    by_id2 = {w["id"]: w for w in snap2["windows"]}
    assert by_id2["w2_morning"]["run"]["scheduled_time"] == t2
    assert by_id2["w4_afternoon"]["run"]["scheduled_time"] == t4
    assert by_id2["w5_close"]["run"]["scheduled_time"] == t5
    assert by_id2["w6_post_close"]["run"]["scheduled_time"] == t6


def test_toggle_mid_window_lunch_guard_and_day_rollover(tmp_path):
    clock = [_bkk(2026, 11, 12, 10, 15)]
    sched = _make_scheduler(tmp_path, clock)

    # Lunch cannot be enabled
    with pytest.raises(ValueError, match="พักเที่ยง"):
        sched.set_toggle(window_id="w3_lunch", enabled=True)

    # Disabling & re-enabling w4_afternoon works cleanly
    snap_off = sched.set_toggle(window_id="w4_afternoon", enabled=False)
    assert {w["id"]: w for w in snap_off["windows"]}["w4_afternoon"]["enabled"] is False

    clock[0] = _bkk(2026, 11, 12, 14, 15)
    snap = sched.set_toggle(window_id="w4_afternoon", enabled=True)
    by_id = {w["id"]: w for w in snap["windows"]}
    assert by_id["w4_afternoon"]["enabled"] is True
    assert by_id["w4_afternoon"]["run"]["status"] == "PENDING"
    assert "14:16" <= by_id["w4_afternoon"]["run"]["scheduled_time"] <= "15:58"

    # Day rollover preserves toggles and regenerates daily_runs
    clock[0] = _bkk(2026, 11, 13, 9, 45)
    snap_next = sched.get_schedule_snapshot()
    by_id_next = {w["id"]: w for w in snap_next["windows"]}
    assert snap_next["daily_date"] == "2026-11-13"
    assert "w1_open" not in by_id_next
    assert by_id_next["w4_afternoon"]["enabled"] is True
    assert "14:01" <= by_id_next["w4_afternoon"]["run"]["scheduled_time"] <= "15:58"


def test_legacy_w1_open_pruned_from_state_on_load(tmp_path):
    import json

    state_file = tmp_path / "nego_scheduler_state.json"
    state_file.write_text(
        json.dumps(
            {
                "master_enabled": True,
                "windows": {"w1_open": {"enabled": True}, "w2_morning": {"enabled": True}},
                "daily_date": "2026-11-16",
                "daily_runs": {
                    "w1_open": {"window_id": "w1_open", "status": "PENDING", "scheduled_time": "10:23"},
                    "w2_morning": {"window_id": "w2_morning", "status": "PENDING", "scheduled_time": "10:57"},
                },
            }
        ),
        encoding="utf-8",
    )
    clock = [_bkk(2026, 11, 16, 9, 45)]
    sched = _make_scheduler(tmp_path, clock)
    snap = sched.get_schedule_snapshot()
    assert [w["id"] for w in snap["windows"]] == ["w2_morning", "w3_lunch", "w4_afternoon", "w5_close", "w6_post_close"]
    by_id = {w["id"]: w for w in snap["windows"]}
    assert by_id["w6_post_close"]["run"]["status"] == "PENDING"
    assert "16:40" <= by_id["w6_post_close"]["run"]["scheduled_time"] <= "16:55"
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert "w1_open" not in saved["windows"]
    assert "w1_open" not in saved["daily_runs"]


@pytest.mark.asyncio
async def test_close_window_matches_sell_cover_buy_and_uptick_short_with_zero_line_push(tmp_path, monkeypatch):
    clock = [_bkk(2026, 11, 12, 16, 15)]
    sched = _make_scheduler(tmp_path, clock)

    # Force w5_close scheduled_time to 16:15
    state = sched._load_or_init_state()
    state["daily_runs"]["w5_close"]["scheduled_time"] = "16:15"
    state["daily_runs"]["w5_close"]["status"] = "PENDING"
    sched._save_state()

    monkeypatch.setattr(mod.kill_switch_service, "is_active", lambda: False)
    monkeypatch.setattr(type(mod.trade_dispatcher), "is_live", property(lambda self: False))
    monkeypatch.setattr(mod.pin_guard, "check", lambda broker: {"locked": False})

    pushed_msgs = []

    async def fake_push(msg, to_user_id=None):
        pushed_msgs.append(msg)
        return True

    from app.services import line_service as ls_mod
    monkeypatch.setattr(ls_mod.line_service, "push_message", fake_push)
    monkeypatch.setattr(ls_mod.line_service, "_push_quota_exhausted", False)

    async def fake_orders(broker="all"):
        if broker == "kss":
            return {
                "brokers": {"kss": {"available": True}},
                "orders": [
                    {"broker": "kss", "order_no": "101", "symbol": "PTT", "side": "SELL", "price": 32.5, "balance": 100, "cancellable": True},
                    {"broker": "kss", "order_no": "102", "symbol": "AOT", "side": "BUY", "price": 60.0, "balance": 100, "cancellable": True},
                    {"broker": "kss", "order_no": "103", "symbol": "KBANK", "side": "SHORT", "price": 150.0, "balance": 100, "cancellable": True},
                ],
            }
        return {
            "brokers": {"lib": {"available": True}},
            "orders": [
                {"broker": "lib", "order_no": "201", "symbol": "SCB", "side": "COVER", "price": 110.0, "balance": 200, "cancellable": True},
            ],
        }

    amend_calls = []

    async def fake_amend(**kwargs):
        amend_calls.append(kwargs)
        return {"ok": True, "no_change": False, "new_price": kwargs["old_price"] - 0.25, "line_pushed": False}

    monkeypatch.setattr(trading_facade, "fetch_orders_overview", fake_orders)
    monkeypatch.setattr(trading_facade, "execute_nego_amend", fake_amend)

    results = await sched.tick_once()
    assert len(results) == 1
    assert len(amend_calls) == 4
    by_sym = {c["symbol"]: c for c in amend_calls}
    assert by_sym["PTT"]["mode"] == "match"
    assert by_sym["SCB"]["mode"] == "match"
    assert by_sym["AOT"]["mode"] == "lure"
    assert by_sym["KBANK"]["mode"] == "lure"
    assert all(c["notify_line"] is False for c in amend_calls)
    assert pushed_msgs == []  # Zero LINE push on normal runs!

    run = [w["run"] for w in results[0]["windows"] if w["id"] == "w5_close"][0]
    assert run["status"] == "AMENDED"
    assert run["counts"] == {"checked": 4, "amended": 4, "no_change": 0, "skipped": 0, "failed": 0}
    assert "งดส่ง LINE" in run["line_status"]

    # Second tick at same minute must NOT re-execute
    assert await sched.tick_once() == []


@pytest.mark.asyncio
async def test_post_close_window_cancels_open_orders_and_returns_unshorted_sbl(tmp_path, monkeypatch):
    clock = [_bkk(2026, 11, 12, 16, 48)]
    sched = _make_scheduler(tmp_path, clock)

    state = sched._load_or_init_state()
    state["daily_runs"]["w6_post_close"]["scheduled_time"] = "16:48"
    state["daily_runs"]["w6_post_close"]["status"] = "PENDING"
    sched._save_state()

    monkeypatch.setattr(mod.kill_switch_service, "is_active", lambda: False)
    monkeypatch.setattr(type(mod.trade_dispatcher), "is_live", property(lambda self: False))
    monkeypatch.setattr(mod.pin_guard, "check", lambda broker: {"locked": False})

    async def fake_orders(broker="all"):
        if broker == "kss":
            return {
                "brokers": {"kss": {"available": True}},
                "orders": [
                    {"broker": "kss", "order_no": "301", "symbol": "TPIPP", "side": "SHORT", "price": 1.60, "balance": 48700, "cancellable": True},
                ],
            }
        return {"brokers": {"lib": {"available": True}}, "orders": []}

    async def fake_portfolio(broker="all", kss_account=None, force_sbl=False):
        return {
            "brokers": {
                "kss": {
                    "available": True,
                    "rows": [
                        # JAS still holds 75,700 SHORT overnight -> do NOT return JAS
                        {"symbol": "JAS", "position_type": "SHORT", "volume": 75700},
                    ],
                    "borrowed": [],
                }
            },
            "sbl": {
                "source": "live",
                "lots": [
                    {"symbol": "JAS", "remaining_qty": 75700},
                    {"symbol": "ORI", "remaining_qty": 43800},     # Covered -> return 43,800
                    {"symbol": "TPIPP", "remaining_qty": 48700},   # Unmatched SHORT cancelled -> return 48,700
                ],
            },
        }

    cancel_calls = []
    sbl_calls = []

    async def fake_cancel(**kwargs):
        cancel_calls.append(kwargs)
        return {"ok": True, "dry_run": True}

    async def fake_sbl(**kwargs):
        sbl_calls.append(kwargs)
        return {"ok": True, "lot_id": f"SBL-{kwargs['symbol']}-1", "volume": kwargs["volume"], "dry_run": True}

    monkeypatch.setattr(trading_facade, "fetch_orders_overview", fake_orders)
    monkeypatch.setattr(trading_facade, "fetch_portfolio_overview", fake_portfolio)
    monkeypatch.setattr(trading_facade, "execute_order_cancel", fake_cancel)
    monkeypatch.setattr(trading_facade, "execute_sbl_action", fake_sbl)

    results = await sched.tick_once()
    assert len(results) == 1
    assert len(cancel_calls) == 1
    assert cancel_calls[0]["order_no"] == "301" and cancel_calls[0]["notify_line"] is False
    assert {c["symbol"]: c["volume"] for c in sbl_calls} == {"ORI": 43800, "TPIPP": 48700}
    assert all(c["action"] == "return" and c["notify_line"] is False for c in sbl_calls)

    run = [w["run"] for w in results[0]["windows"] if w["id"] == "w6_post_close"][0]
    assert run["status"] == "DONE"
    assert run["post_close_summary"] == {"cancelled": 1, "returned": 2, "returned_shares": 92500}


@pytest.mark.asyncio
async def test_live_session_down_invariant9_and_error_dedup(tmp_path, monkeypatch):
    clock = [_bkk(2026, 11, 12, 11, 10)]
    sched = _make_scheduler(tmp_path, clock)

    monkeypatch.setattr(mod.kill_switch_service, "is_active", lambda: False)
    monkeypatch.setattr(type(mod.trade_dispatcher), "is_live", property(lambda self: True))
    monkeypatch.setattr(mod.pin_guard, "check", lambda broker: {"locked": False})

    async def fake_probe(broker, purpose="streaming"):
        return {"broker": broker, "state": "DAEMON_DOWN", "daemon_up": False}

    monkeypatch.setattr(mod, "cdp_session_status", fake_probe)

    scrape_called = False

    async def fail_if_called(*args, **kwargs):
        nonlocal scrape_called
        scrape_called = True
        return {}

    monkeypatch.setattr(trading_facade, "fetch_orders_overview", fail_if_called)

    pushed = []

    async def fake_push(msg, to_user_id=None):
        pushed.append(msg)
        return True

    from app.services import line_service as ls_mod
    monkeypatch.setattr(ls_mod.line_service, "push_message", fake_push)
    monkeypatch.setattr(ls_mod.line_service, "_push_quota_exhausted", False)

    snap1 = await sched.run_window_now("w2_morning", source="scheduler")
    assert scrape_called is False  # Invariant 9: never scrapes/launches when unauthenticated
    assert len(pushed) == 1
    run_w2 = {w["id"]: w for w in snap1["windows"]}["w2_morning"]["run"]
    assert run_w2["status"] == "ERROR"
    assert "แจ้ง Error LINE แล้ว" in run_w2["line_status"]

    # Same day second error (w4_afternoon) dedups no_ready_brokers push
    clock[0] = _bkk(2026, 11, 12, 14, 30)
    snap2 = await sched.run_window_now("w4_afternoon", source="scheduler")
    assert len(pushed) == 1
    run_w4 = {w["id"]: w for w in snap2["windows"]}["w4_afternoon"]["run"]
    assert "Dedup" in run_w4["line_status"]


@pytest.mark.asyncio
async def test_already_matched_is_not_critical_error_and_kill_switch_blocks(tmp_path, monkeypatch):
    clock = [_bkk(2026, 11, 12, 11, 20)]
    sched = _make_scheduler(tmp_path, clock)
    monkeypatch.setattr(type(mod.trade_dispatcher), "is_live", property(lambda self: False))
    monkeypatch.setattr(mod.pin_guard, "check", lambda broker: {"locked": False})

    pushed = []
    from app.services import line_service as ls_mod

    async def fake_push(m, to_user_id=None):
        pushed.append(m)
        return True

    monkeypatch.setattr(ls_mod.line_service, "push_message", fake_push)

    # 1. Kill switch active
    monkeypatch.setattr(mod.kill_switch_service, "is_active", lambda: True)
    snap_ks = await sched.run_window_now("w2_morning", source="manual")
    assert {w["id"]: w for w in snap_ks["windows"]}["w2_morning"]["run"]["status"] == "BLOCKED_KILL_SWITCH"
    assert pushed == []

    # 2. ALREADY_MATCHED from facade is recorded as skipped, not critical error
    monkeypatch.setattr(mod.kill_switch_service, "is_active", lambda: False)

    async def fake_orders(broker="all"):
        if broker == "kss":
            return {
                "brokers": {"kss": {"available": True}},
                "orders": [{"broker": "kss", "order_no": "999", "symbol": "BDMS", "side": "BUY", "price": 25.0, "balance": 100, "cancellable": True}],
            }
        return {"brokers": {"lib": {"available": True}}, "orders": []}

    async def fake_amend_matched(**kwargs):
        raise trading_facade.FacadeHttpError(502, "ORDER_ALREADY_MATCHED", "Order already matched")

    monkeypatch.setattr(trading_facade, "fetch_orders_overview", fake_orders)
    monkeypatch.setattr(trading_facade, "execute_nego_amend", fake_amend_matched)

    snap_ok = await sched.run_window_now("w2_morning", source="manual")
    run_w2 = {w["id"]: w for w in snap_ok["windows"]}["w2_morning"]["run"]
    assert run_w2["status"] == "NO_CHANGE"
    assert run_w2["items"][0]["outcome"] == "ALREADY_MATCHED"
    assert pushed == []


@pytest.mark.asyncio
async def test_weekend_skips_tick(tmp_path):
    clock = [_bkk(2026, 11, 14, 11, 0)]  # Saturday
    sched = _make_scheduler(tmp_path, clock)
    assert await sched.tick_once() == []


@pytest.mark.asyncio
async def test_mixed_broker_pin_lock_preserves_line_status(tmp_path, monkeypatch):
    clock = [_bkk(2026, 11, 12, 11, 15)]
    sched = _make_scheduler(tmp_path, clock)

    monkeypatch.setattr(mod.kill_switch_service, "is_active", lambda: False)
    monkeypatch.setattr(type(mod.trade_dispatcher), "is_live", property(lambda self: False))

    def fake_pin_check(broker):
        if broker == "kss":
            return {"locked": True, "reason": "E_PIN_REJECTED"}
        return {"locked": False}

    monkeypatch.setattr(mod.pin_guard, "check", fake_pin_check)

    async def fake_orders(broker="all"):
        return {"brokers": {broker: {"available": True}}, "orders": []}

    monkeypatch.setattr(trading_facade, "fetch_orders_overview", fake_orders)

    pushed = []
    from app.services import line_service as ls_mod

    async def fake_push(msg, to_user_id=None):
        pushed.append(msg)
        return True

    monkeypatch.setattr(ls_mod.line_service, "push_message", fake_push)
    monkeypatch.setattr(ls_mod.line_service, "_push_quota_exhausted", False)

    snap1 = await sched.run_window_now("w2_morning", source="manual")
    run_w2 = {w["id"]: w for w in snap1["windows"]}["w2_morning"]["run"]
    assert run_w2["status"] == "NO_ORDERS"
    assert "PIN" in run_w2["broker_errors"]["kss"]
    assert run_w2["line_status"] == "📨 แจ้ง Error LINE แล้ว"
    assert len(pushed) == 1

    clock[0] = _bkk(2026, 11, 12, 14, 15)
    snap2 = await sched.run_window_now("w4_afternoon", source="manual")
    run_w4 = {w["id"]: w for w in snap2["windows"]}["w4_afternoon"]["run"]
    assert run_w4["status"] == "NO_ORDERS"
    assert "Dedup" in run_w4["line_status"]
    assert len(pushed) == 1
