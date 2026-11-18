"""Tests for On-Demand Model 19 Suggest Entry Service & MiniApp Endpoints."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from httpx import ASGITransport, AsyncClient
import pytest

from app.core.config import settings
from app.main import app
from app.services import suggest_entry_service as se_mod
from app.services.broker_cdp import trading_facade
from app.services.suggest_entry_service import SuggestEntryError, SuggestEntryService

_BKK = ZoneInfo("Asia/Bangkok")


def _async_val(val: Any):
    async def _inner(*_a, **_kw):
        return val
    return _inner


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _seed_fixtures(
    tmp_path: Path,
    *,
    date_str: str = "2026-11-16",
    quota: Dict[str, int] | None = None,
    mode: str = "SHORT",
    free_slots: int = 3,
    held_map: Dict[str, Any] | None = None,
) -> Dict[str, Path]:
    scanner_dir = tmp_path / "scanner"
    pl_path = tmp_path / "positions_ledger.json"
    el_path = tmp_path / "entry_ledger.json"
    state_path = tmp_path / "suggest_state.json"

    q_obj = quota if quota is not None else {"long": 0, "short": 4}
    held = held_map if held_map is not None else {"JAS": {"is_exiting_today": False}}

    short_cands = []
    for idx, sym in enumerate(["JAS", "CKP", "ORI", "DOHOME", "SPALI", "TPIPP", "THAI", "AEONTS"], start=1):
        short_cands.append(
            {
                "ticker": sym,
                "side": "SHORT",
                "origin_pool": "Pure Bear Breakdown",
                "rank_in_table": idx,
                "close": 2.0 + idx,
                "pct_change": -1.0,
                "atr20_pct": 1.8 if sym == "CKP" else 3.1,
                "atr_range_str": "[SHORT] SL/TP",
                "prob_pct": 10.0,
                "weight": 500.0,
                "_extra": {
                    "pool_score": 150.9,
                    "guardrail_warning": "⚠️ ALREADY HELD" if sym == "JAS" else None,
                    "sbl_vol": 500000,
                    "sbl_safe_val": 1000000.0,
                    "sbl_im_pct": 70,
                },
            }
        )
    long_cands = []
    for idx, sym in enumerate(["JMART", "JMT", "STPI", "GUNKUL", "DITTO", "OR", "SUPER", "TIPH"], start=1):
        long_cands.append(
            {
                "ticker": sym,
                "side": "LONG",
                "origin_pool": "Transition Long",
                "rank_in_table": idx,
                "close": 10.0,
                "pct_change": -2.0,
                "atr20_pct": 2.5,
                "atr_range_str": "[LONG] SL/TP",
                "prob_pct": 11.0,
                "weight": 150.0,
                "_extra": {"pool_score": 120.0, "guardrail_warning": None},
            }
        )

    _write_json(
        scanner_dir / f"candidates_{date_str}.json",
        {
            "date": date_str,
            "quota": q_obj,
            "decision": {"mode": mode, "label": f"{mode} [Margin 1.5x]", "regime_10": "Bearish Quiet"},
            "tournament": {"chosen_details": [{"zone_margin_multiplier": 1.5}] if (q_obj["long"] + q_obj["short"] > 0) else []},
            "candidates": short_cands + long_cands,
        },
    )
    _write_json(
        pl_path,
        {
            "as_of": date_str,
            "house_money": {"active": False, "multiplier": 1.0, "open_ret_sum_pct": 2.0, "be_locked_count": 1},
            "slot_guardrail": {
                "cap_unique_tickers": 7,
                "held_unique_tickers": 7 - free_slots,
                "exiting_unique_tickers": 0,
                "remaining_unique_tickers": 7 - free_slots,
                "free_slots": free_slots,
                "is_over_cap": free_slots <= 0,
                "held_ticker_map": held,
            },
            "positions": [
                {"segment": "managed", "broker": "KSS", "symbol": "CHG", "action": "COVER", "shares": 10000, "exit_reason": "SL"}
            ],
        },
    )
    return {
        "scanner_dir": scanner_dir,
        "pl_path": pl_path,
        "el_path": el_path,
        "state_path": state_path,
    }


def _patch_live_mocks(
    monkeypatch,
    *,
    quotes: Dict[str, Dict[str, Any]],
    sbl_pools: Dict[str, Dict[str, Any]] | None = None,
    kss_line: float = 500000.0,
    lib_line: float = 400000.0,
    open_orders: List[Dict[str, Any]] | None = None,
    live_held_kss: List[Dict[str, Any]] | None = None,
) -> None:
    monkeypatch.setattr(se_mod, "probe_ready_brokers", lambda needed: _async_val((list(needed), {}, []))())
    monkeypatch.setattr(se_mod.runbook_exit_service, "_read_holiday_info", lambda dt: (False, None, None))
    monkeypatch.setattr(se_mod.kill_switch_service, "is_active", lambda: False)
    monkeypatch.setattr(se_mod.kill_switch_service, "get_status", lambda: {"is_active": False})
    monkeypatch.setattr(se_mod.trade_dispatcher, "_current_mode", "TEST")
    monkeypatch.setattr(settings, "DRY_RUN_MODE", True)

    async def fake_port(broker="all", **_kw):
        return {
            "brokers": {
                "kss": {"available": True, "line_available": kss_line, "rows": live_held_kss or []},
                "lib": {"available": True, "line_available": lib_line, "rows": []},
            }
        }

    async def fake_orders(broker="all"):
        return {
            "brokers": {"kss": {"available": True}, "lib": {"available": True}},
            "orders": open_orders or [],
        }

    async def fake_quote(broker, symbol, side=None, old_price=None):
        q = quotes.get(symbol)
        if q is None:
            return {"available": False, "error": "NO_QUOTE"}
        return q

    async def fake_batch_quotes(broker, symbols, side=None):
        return {sym: await fake_quote(broker, sym, side=side) for sym in symbols}

    async def fake_sbl_pool(symbol):
        return (sbl_pools or {}).get(
            symbol,
            {"symbol": symbol, "available_qty": 200000, "xd_risk": {"xd_risk": False}},
        )

    monkeypatch.setattr(trading_facade, "fetch_portfolio_overview", fake_port)
    monkeypatch.setattr(trading_facade, "fetch_orders_overview", fake_orders)
    monkeypatch.setattr(trading_facade, "compute_nego_quote", fake_quote)
    monkeypatch.setattr(trading_facade, "fetch_batch_nego_quotes", fake_batch_quotes)
    monkeypatch.setattr(trading_facade, "fetch_sbl_pool_quote", fake_sbl_pool)
    monkeypatch.setattr(se_mod.miniapp_inbox, "publish", lambda **_kw: _async_val({"id": "m1"})())


@pytest.mark.asyncio
async def test_poll_short_filters_ranks_and_checks_sbl(monkeypatch, tmp_path: Path):
    paths = _seed_fixtures(tmp_path, quota={"long": 0, "short": 4}, mode="SHORT", free_slots=3)
    svc = SuggestEntryService(
        state_path=str(paths["state_path"]),
        scanner_dir=str(paths["scanner_dir"]),
        positions_ledger_path=str(paths["pl_path"]),
        entry_ledger_path=str(paths["el_path"]),
        clock_fn=lambda: datetime(2026, 11, 16, 10, 35, tzinfo=_BKK),
    )

    quotes = {
        "CKP": {"available": True, "last": 2.30, "prior_close": 2.30, "bid_1": 2.28, "offer_1": 2.32, "lure_plan": {"ok": True, "price": 2.32}, "match_plan": {"ok": False}},
        "ORI": {"available": True, "last": 1.75, "prior_close": 1.70, "bid_1": 1.74, "offer_1": 1.76, "lure_plan": {"ok": True, "price": 1.76}, "match_plan": {"ok": False}},
        "DOHOME": {"available": True, "last": 3.40, "prior_close": 3.50, "bid_1": 3.38, "offer_1": 3.42, "lure_plan": {"ok": True, "price": 3.42}, "match_plan": {"ok": False}},
        "SPALI": {"available": True, "last": 16.0, "prior_close": 15.0, "bid_1": 15.9, "offer_1": 16.1, "lure_plan": {"ok": True, "price": 16.1}, "match_plan": {"ok": False}},
        "TPIPP": {"available": True, "last": 1.60, "prior_close": 1.50, "bid_1": 1.59, "offer_1": 1.61, "lure_plan": {"ok": True, "price": 1.61}, "match_plan": {"ok": False}},
        "THAI": {"available": True, "last": 5.20, "prior_close": 5.00, "bid_1": 5.15, "offer_1": 5.25, "lure_plan": {"ok": True, "price": 5.25}, "match_plan": {"ok": False}},
        "AEONTS": {"available": True, "last": 90.0, "prior_close": 89.0, "bid_1": 89.5, "offer_1": 90.5, "lure_plan": {"ok": True, "price": 90.5}, "match_plan": {"ok": False}},
        "JAS": {"available": True, "last": 0.70, "prior_close": 0.65, "bid_1": 0.69, "offer_1": 0.71, "lure_plan": {"ok": True, "price": 0.71}, "match_plan": {"ok": False}},
    }
    sbl_pools = {
        # SPALI fails XD risk, TPIPP fails insufficient SBL (needs >90k shares on 150k budget)
        "SPALI": {"symbol": "SPALI", "available_qty": 200000, "xd_risk": {"xd_risk": True, "reason": "XD tomorrow"}},
        "TPIPP": {"symbol": "TPIPP", "available_qty": 1000, "xd_risk": {"xd_risk": False}},
    }
    _patch_live_mocks(
        monkeypatch,
        quotes=quotes,
        sbl_pools=sbl_pools,
        open_orders=[{"broker": "kss", "symbol": "AEONTS", "side": "SHORT", "kind": "open"}],
    )

    res = await svc.poll_candidates()
    assert res["ledger_available"] is True
    assert res["quota"]["remaining_total"] == 2  # effective 3 minus 1 live open order (AEONTS)

    by_sym = {c["ticker"]: c for c in res["candidates"]}
    assert by_sym["JAS"]["disabled_reason_code"] == "ALREADY_HELD"
    assert by_sym["AEONTS"]["disabled_reason_code"] == "ORDERED_TODAY"
    assert by_sym["DOHOME"]["disabled_reason_code"] == "PCT_NOT_MET"
    assert by_sym["SPALI"]["disabled_reason_code"] == "XD_RISK"
    assert by_sym["TPIPP"]["disabled_reason_code"] == "SBL_INSUFFICIENT"
    assert by_sym["CKP"]["selectable"] is True and by_sym["CKP"]["live_pct_change"] == 0.0
    assert by_sym["CKP"]["high_atr_warning"] is False
    assert by_sym["ORI"]["high_atr_warning"] is True

    # Eligible items sorted first by strongest positive %Change: THAI (+4.00) > ORI (+2.94) > CKP (0.00)
    eligible_order = [c["ticker"] for c in res["candidates"] if c["selectable"]]
    assert eligible_order == ["THAI", "ORI", "CKP"]


@pytest.mark.asyncio
async def test_poll_hold_cash_readonly_and_override(monkeypatch, tmp_path: Path):
    paths = _seed_fixtures(tmp_path, quota={"long": 0, "short": 0}, mode="HOLD CASH", free_slots=3, held_map={})
    svc = SuggestEntryService(
        state_path=str(paths["state_path"]),
        scanner_dir=str(paths["scanner_dir"]),
        positions_ledger_path=str(paths["pl_path"]),
        entry_ledger_path=str(paths["el_path"]),
        clock_fn=lambda: datetime(2026, 11, 16, 10, 35, tzinfo=_BKK),
    )
    quotes = {
        "CKP": {"available": True, "last": 2.30, "prior_close": 2.25, "bid_1": 2.28, "offer_1": 2.32, "lure_plan": {"ok": True, "price": 2.32}, "match_plan": {"ok": False}},
        "JMART": {"available": True, "last": 9.80, "prior_close": 10.0, "bid_1": 9.75, "offer_1": 9.85, "lure_plan": {"ok": True, "price": 9.75}, "match_plan": {"ok": True, "price": 9.85}},
    }
    _patch_live_mocks(monkeypatch, quotes=quotes)

    ro = await svc.poll_candidates(override_hold_cash=False)
    assert ro["is_hold_cash"] is True
    assert ro["selectable_count"] == 0
    assert all(c["disabled_reason_code"] in ("HOLD_CASH_LOCKED", "QUOTE_UNAVAILABLE") for c in ro["candidates"])

    ov = await svc.poll_candidates(override_hold_cash=True)
    by_key = {(c["side"], c["ticker"]): c for c in ov["candidates"]}
    assert by_key[("SHORT", "CKP")]["selectable"] is True
    assert by_key[("LONG", "JMART")]["selectable"] is True


@pytest.mark.asyncio
async def test_submit_long_multiple_choices_and_line_clamp(monkeypatch, tmp_path: Path):
    paths = _seed_fixtures(tmp_path, quota={"long": 3, "short": 0}, mode="LONG", free_slots=2, held_map={})
    svc = SuggestEntryService(
        state_path=str(paths["state_path"]),
        scanner_dir=str(paths["scanner_dir"]),
        positions_ledger_path=str(paths["pl_path"]),
        entry_ledger_path=str(paths["el_path"]),
        clock_fn=lambda: datetime(2026, 11, 16, 10, 40, tzinfo=_BKK),
    )
    svc.save_config(100000.0)

    quotes = {
        "JMART": {"available": True, "last": 9.50, "prior_close": 10.0, "bid_1": 9.45, "offer_1": 9.55, "lure_plan": {"ok": True, "price": 9.45}, "match_plan": {"ok": True, "price": 9.55}},
        "JMT": {"available": True, "last": 9.90, "prior_close": 10.0, "bid_1": 9.85, "offer_1": 9.95, "lure_plan": {"ok": True, "price": 9.85}, "match_plan": {"ok": True, "price": 9.95}},
        "STPI": {"available": True, "last": 5.00, "prior_close": 5.20, "bid_1": 4.98, "offer_1": 5.05, "lure_plan": {"ok": True, "price": 4.98}, "match_plan": {"ok": True, "price": 5.05}},
    }
    _patch_live_mocks(monkeypatch, quotes=quotes, lib_line=150000.0)

    executed_calls: List[Dict[str, Any]] = []

    async def fake_exec(**kwargs):
        executed_calls.append(kwargs)
        return {
            "ok": True,
            "dry_run": True,
            "draft_id": f"dr_{kwargs['symbol']}",
            "order": {
                "cmd": kwargs["side"],
                "symbol": kwargs["symbol"],
                "volume": kwargs["volume"],
                "price": kwargs["price"],
            },
            "execution": {"broker_order_no": f"ORD-{kwargs['symbol']}"},
        }

    monkeypatch.setattr(trading_facade, "execute_trade_with_idempotency", fake_exec)

    # Rejects exceeding free_slots cap (2)
    with pytest.raises(SuggestEntryError) as exc_info:
        await svc.submit_selected(
            [
                {"symbol": "JMART", "side": "LONG", "price_mode": "lure"},
                {"symbol": "JMT", "side": "LONG", "price_mode": "match"},
                {"symbol": "STPI", "side": "LONG", "price_mode": "lure"},
            ]
        )
    assert exc_info.value.code == "E_QUOTA_EXCEEDED"

    # Submit 2 items: JMART uses Lure 9.45 (100k -> 10,500 sh = 99,225 THB), leaving ~50,775 THB for JMT Match 9.95 -> 5,100 sh
    res = await svc.submit_selected(
        [
            {"symbol": "JMART", "side": "LONG", "price_mode": "lure"},
            {"symbol": "JMT", "side": "LONG", "price_mode": "match"},
        ]
    )
    assert res["ok"] is True
    assert res["submitted_count"] == 2
    assert executed_calls[0]["price"] == 9.45
    assert executed_calls[0]["volume"] == 10500
    assert executed_calls[1]["price"] == 9.95
    assert executed_calls[1]["volume"] == 5100

    # Subsequent poll sees 0 remaining quota and both marked ORDERED_TODAY
    after = await svc.poll_candidates()
    assert after["quota"]["remaining_total"] == 0


@pytest.mark.asyncio
async def test_submit_short_uptick_retry_and_stranded_alert(monkeypatch, tmp_path: Path):
    paths = _seed_fixtures(tmp_path, quota={"long": 0, "short": 2}, mode="SHORT", free_slots=2, held_map={})
    svc = SuggestEntryService(
        state_path=str(paths["state_path"]),
        scanner_dir=str(paths["scanner_dir"]),
        positions_ledger_path=str(paths["pl_path"]),
        entry_ledger_path=str(paths["el_path"]),
        clock_fn=lambda: datetime(2026, 11, 16, 10, 45, tzinfo=_BKK),
    )

    quotes = {
        "CKP": {"available": True, "last": 2.30, "prior_close": 2.26, "bid_1": 2.28, "offer_1": 2.32, "lure_plan": {"ok": True, "price": 2.32}, "match_plan": {"ok": False}},
        "ORI": {"available": True, "last": 1.72, "prior_close": 1.70, "bid_1": 1.71, "offer_1": 1.73, "lure_plan": {"ok": True, "price": 1.73}, "match_plan": {"ok": False}},
    }
    _patch_live_mocks(monkeypatch, quotes=quotes)

    async def fake_sbl_action(**kwargs):
        return {
            "ok": True,
            "dry_run": True,
            "volume": kwargs["volume"],
            "auto_short": {
                "attempted": True,
                "ok": False,
                "dry_run": True,
                "code": "E_UPTICK_VIOLATION",
                "error": "ราคา SHORT ต้องสูงกว่า Last",
            },
        }

    retry_calls = []

    async def fake_retry_exec(**kwargs):
        retry_calls.append(kwargs)
        if kwargs["symbol"] == "ORI":
            raise trading_facade.FacadeHttpError(422, "E_UPTICK_VIOLATION", "still below last")
        return {
            "ok": True,
            "dry_run": True,
            "draft_id": "dr_ckp",
            "order": {"symbol": "CKP", "volume": kwargs["volume"], "price": kwargs["price"]},
            "execution": {"broker_order_no": "S-101"},
        }

    alerts = []
    monkeypatch.setattr(trading_facade, "execute_sbl_action", fake_sbl_action)
    monkeypatch.setattr(trading_facade, "execute_trade_with_idempotency", fake_retry_exec)
    monkeypatch.setattr(se_mod, "notify_critical_error_once", lambda *a, **kw: _async_val(alerts.append(kw) or True)())

    res = await svc.submit_selected(
        [
            {"symbol": "CKP", "side": "SHORT"},
            {"symbol": "ORI", "side": "SHORT"},
        ]
    )
    assert res["submitted_count"] == 1
    assert res["failed_count"] == 1
    assert res["results"][0]["ok"] is True and res["results"][0]["retried_uptick"] is True
    assert res["results"][1]["status"] == "BORROWED_SHORT_FAILED"
    assert len(alerts) == 1
    assert "ORI" in alerts[0]["detail"]


@pytest.mark.asyncio
async def test_miniapp_suggest_entry_endpoints(monkeypatch, tmp_path: Path):
    paths = _seed_fixtures(tmp_path, quota={"long": 0, "short": 0}, mode="HOLD CASH", free_slots=3)
    test_svc = SuggestEntryService(
        state_path=str(paths["state_path"]),
        scanner_dir=str(paths["scanner_dir"]),
        positions_ledger_path=str(paths["pl_path"]),
        entry_ledger_path=str(paths["el_path"]),
        clock_fn=lambda: datetime(2026, 11, 16, 10, 50, tzinfo=_BKK),
    )
    _patch_live_mocks(monkeypatch, quotes={})
    monkeypatch.setattr(settings, "MINIAPP_DEV_BYPASS_LOCAL", True)
    monkeypatch.setattr("app.api.v1.endpoints.miniapp.suggest_entry_service", test_svc)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        cfg_res = await client.post("/api/v1/miniapp/suggest-entry/config", json={"budget_per_slot_thb": 200000})
        assert cfg_res.status_code == 200
        assert cfg_res.json()["budget_per_slot_thb"] == 200000.0

        hub_res = await client.get("/api/v1/miniapp/trade/hub-summary")
        assert hub_res.status_code == 200
        hub = hub_res.json()
        assert hub["auto_exit"]["count"] == 1
        assert hub["auto_exit"]["items"][0]["symbol"] == "CHG"
        assert hub["suggest_entry"]["budget_per_slot_thb"] == 200000.0

        poll_res = await client.get("/api/v1/miniapp/suggest-entry/poll")
        assert poll_res.status_code == 200
        assert poll_res.json()["is_hold_cash"] is True

        quotes_res = await client.get("/api/v1/miniapp/suggest-entry/quotes")
        assert quotes_res.status_code == 200
        assert quotes_res.json()["is_hold_cash"] is True

        got_events: List[str] = []
        async with client.stream(
            "GET",
            "/api/v1/miniapp/suggest-entry/stream",
            params={"interval_s": 1.0, "max_ticks": 1},
        ) as s_resp:
            assert s_resp.status_code == 200
            async for line in s_resp.aiter_lines():
                if line:
                    got_events.append(line)
                if line.startswith("data: "):
                    break
        assert any(l == "event: quotes" for l in got_events)
        data_line = next(l for l in got_events if l.startswith("data: "))
        parsed = json.loads(data_line[len("data: "):])
        assert "candidates" in parsed


def test_hub_summary_maps_missed_window_to_thai_label(monkeypatch, tmp_path: Path):
    paths = _seed_fixtures(tmp_path, quota={"long": 0, "short": 0}, mode="HOLD CASH", free_slots=3)
    svc = SuggestEntryService(
        state_path=str(paths["state_path"]),
        scanner_dir=str(paths["scanner_dir"]),
        positions_ledger_path=str(paths["pl_path"]),
        entry_ledger_path=str(paths["el_path"]),
        clock_fn=lambda: datetime(2026, 11, 16, 10, 50, tzinfo=_BKK),
    )
    monkeypatch.setattr(se_mod.runbook_exit_service, "_read_holiday_info", lambda dt: (False, None, None))
    monkeypatch.setattr(
        se_mod.runbook_exit_service,
        "get_schedule_snapshot",
        lambda dt: {"auto_enabled": True, "queue": {"status": "MISSED_WINDOW"}},
    )
    hub = svc.get_hub_summary()
    assert hub["auto_exit"]["schedule_status_label"] == "เลยช่วง Auto"
    assert hub["auto_exit"]["schedule_status_tone"] == "muted"


@pytest.mark.asyncio
async def test_poll_live_quotes_reuses_context_and_reflects_new_prices(monkeypatch, tmp_path: Path):
    paths = _seed_fixtures(tmp_path, quota={"long": 1, "short": 1}, mode="BOTH", free_slots=3)
    now_dt = [datetime(2026, 11, 16, 10, 35, 0, tzinfo=_BKK)]
    svc = SuggestEntryService(
        state_path=str(paths["state_path"]),
        scanner_dir=str(paths["scanner_dir"]),
        positions_ledger_path=str(paths["pl_path"]),
        entry_ledger_path=str(paths["el_path"]),
        clock_fn=lambda: now_dt[0],
    )
    quotes = {
        "ORI": {"available": True, "last": 3.04, "prior_close": 3.00, "bid_1": 3.02, "offer_1": 3.04, "lure_price": 3.06, "match_price": 3.02},
        "CKP": {"available": True, "last": 3.50, "prior_close": 3.60, "bid_1": 3.50, "offer_1": 3.52, "lure_price": 3.50, "match_price": 3.52},
    }
    _patch_live_mocks(monkeypatch, quotes=quotes)

    calls = {"port": 0, "batch": 0}
    orig_port = trading_facade.fetch_portfolio_overview
    orig_batch = trading_facade.fetch_batch_nego_quotes

    async def count_port(*args, **kwargs):
        calls["port"] += 1
        return await orig_port(*args, **kwargs)

    async def count_batch(*args, **kwargs):
        calls["batch"] += 1
        return await orig_batch(*args, **kwargs)

    monkeypatch.setattr(trading_facade, "fetch_portfolio_overview", count_port)
    monkeypatch.setattr(trading_facade, "fetch_batch_nego_quotes", count_batch)

    p1 = await svc.poll_candidates()
    assert calls["port"] == 1
    assert calls["batch"] == 2
    ckp1 = next(c for c in p1["candidates"] if c["ticker"] == "CKP")
    assert ckp1["last"] == pytest.approx(3.50)

    # Update quote and call fast poll_live_quotes() -> should NOT re-scrape portfolio
    quotes["CKP"]["last"] = 3.54
    quotes["CKP"]["lure_price"] = 3.54
    now_dt[0] = datetime(2026, 11, 16, 10, 35, 2, tzinfo=_BKK)
    p2 = await svc.poll_live_quotes()
    assert calls["port"] == 1  # Context reused!
    assert calls["batch"] == 4
    ckp2 = next(c for c in p2["candidates"] if c["ticker"] == "CKP")
    assert ckp2["last"] == pytest.approx(3.54)
    assert p2["now_bkk"] == "10:35:02"


@pytest.mark.asyncio
async def test_trading_facade_fetch_batch_quotes_httpx_201_and_401_fallback(monkeypatch):
    from scripts.broker_cdp import js_runner

    trading_facade._BROKER_COOKIE_CACHE.clear()
    trading_facade._LIB_ORDERBOOK_MAP.clear()
    trading_facade._BROKER_COOKIE_CACHE["lib"] = {
        "ts": 10**12,
        "cookies": [{"name": "sid", "value": "abc", "domain": "webtrade.liberator.co.th", "path": "/"}],
    }
    trading_facade._LIB_ORDERBOOK_MAP["CKP"] = 101

    httpx_calls = []

    class FakeResp:
        def __init__(self, status_code, data):
            self.status_code = status_code
            self._data = data

        def json(self):
            return self._data

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self._post_idx = len(httpx_calls)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json=None):
            httpx_calls.append((url, json))
            if len(httpx_calls) == 1:
                return FakeResp(
                    201,
                    {"result": {"list": [{"n": "CKP", "bp": 3.50, "op": 3.52, "l": 3.50, "prior": 3.60}]}},
                )
            return FakeResp(401, {})

    cdp_calls = []

    async def fake_extract(broker, symbols, timeout_s=8.0):
        cdp_calls.append((broker, list(symbols)))
        return {
            "ok": True,
            "cookies": [{"name": "sid", "value": "new_sid", "domain": "webtrade.liberator.co.th", "path": "/"}],
            "ob_map": {"CKP": 101},
            "quotes": {
                "CKP": {
                    "symbol": "CKP",
                    "success": True,
                    "status": "OK",
                    "source": "liberator_cdp_batch",
                    "bid_1": 3.48,
                    "offer_1": 3.50,
                    "last": 3.50,
                    "prior_close": 3.60,
                }
            },
        }

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(js_runner, "extract_broker_cookies_and_batch_quotes", fake_extract)

    try:
        # 1) Cookie httpx HTTP 201 Created works with zero CDP spawn
        res1 = await trading_facade.fetch_batch_quotes("lib", ["CKP"])
        assert res1["CKP"]["source"] == "liberator_httpx"
        assert res1["CKP"]["last"] == pytest.approx(3.50)
        assert len(cdp_calls) == 0

        # 2) HTTP 401 invalidates cache and transparently falls back to 1 CDP batch extract
        res2 = await trading_facade.fetch_batch_quotes("lib", ["CKP"])
        assert len(cdp_calls) == 1
        assert res2["CKP"]["source"] == "liberator_cdp_batch"
        assert trading_facade._BROKER_COOKIE_CACHE["lib"]["cookies"][0]["value"] == "new_sid"
    finally:
        trading_facade._BROKER_COOKIE_CACHE.clear()
        trading_facade._LIB_ORDERBOOK_MAP.clear()


@pytest.mark.asyncio
async def test_submit_selected_concurrent_cross_broker_batch(monkeypatch, tmp_path: Path):
    paths = _seed_fixtures(tmp_path, quota={"long": 1, "short": 1}, mode="BOTH", free_slots=3)
    svc = SuggestEntryService(
        state_path=str(paths["state_path"]),
        scanner_dir=str(paths["scanner_dir"]),
        positions_ledger_path=str(paths["pl_path"]),
        entry_ledger_path=str(paths["el_path"]),
        clock_fn=lambda: datetime(2026, 11, 16, 10, 45, tzinfo=_BKK),
    )
    quotes = {
        "JMT": {
            "available": True,
            "last": 3.50,
            "prior_close": 3.60,
            "bid_1": 3.50,
            "offer_1": 3.52,
            "lure_plan": {"ok": True, "price": 3.50},
            "match_plan": {"ok": True, "price": 3.52},
        },
        "ORI": {
            "available": True,
            "last": 3.04,
            "prior_close": 3.00,
            "bid_1": 3.02,
            "offer_1": 3.04,
            "lure_plan": {"ok": True, "price": 3.06},
            "match_plan": {"ok": False},
        },
    }
    _patch_live_mocks(monkeypatch, quotes=quotes)

    async def fake_buy(**kw):
        return {
            "ok": True,
            "dry_run": True,
            "draft_id": "dr_jmt",
            "order": {"symbol": kw["symbol"], "volume": kw["volume"], "price": kw["price"]},
            "execution": {"broker_order_no": "LIB-BUY-1"},
        }

    async def fake_sbl(**kw):
        return {
            "ok": True,
            "dry_run": True,
            "volume": kw["volume"],
            "auto_short": {
                "attempted": True,
                "ok": True,
                "dry_run": True,
                "broker_order_no": "KSS-SHORT-1",
                "price": kw.get("auto_short_price"),
            },
        }

    monkeypatch.setattr(trading_facade, "execute_trade_with_idempotency", fake_buy)
    monkeypatch.setattr(trading_facade, "execute_sbl_action", fake_sbl)

    res = await svc.submit_selected(
        items=[
            {"side": "LONG", "symbol": "JMT", "price_mode": "match"},
            {"side": "SHORT", "symbol": "ORI", "price_mode": "lure"},
        ]
    )
    assert res["ok"] is True
    assert res["submitted_count"] == 2
    assert res["failed_count"] == 0
    by_sym = {r["symbol"]: r for r in res["results"]}
    assert by_sym["JMT"]["broker"] == "lib"
    assert by_sym["JMT"]["price"] == pytest.approx(3.52)
    assert by_sym["ORI"]["broker"] == "kss"
    assert by_sym["ORI"]["price"] == pytest.approx(3.06)


def test_hub_summary_exposes_saved_budget_and_candidate_sides(tmp_path, monkeypatch):
    fx = _seed_fixtures(tmp_path, date_str="2026-11-16", quota={"long": 1, "short": 2}, mode="BOTH")
    monkeypatch.setattr(se_mod.runbook_exit_service, "_read_holiday_info", lambda dt: (False, None, None))
    svc = SuggestEntryService(
        scanner_dir=str(fx["scanner_dir"]),
        positions_ledger_path=str(fx["pl_path"]),
        entry_ledger_path=str(fx["el_path"]),
        state_path=str(fx["state_path"]),
        clock_fn=lambda: datetime(2026, 11, 16, 10, 30, tzinfo=_BKK),
    )
    svc.save_config(50000.0)
    hub = svc.get_hub_summary()
    se = hub["suggest_entry"]
    assert se["budget_per_slot_thb"] == pytest.approx(50000.0)
    sides = se["candidate_sides"]
    assert sides["CKP"] == "SHORT"
    assert sides["JMT"] == "LONG"


@pytest.mark.asyncio
async def test_edge_tier_and_house_money_sizing(tmp_path: Path, monkeypatch):
    from app.services.suggest_entry_service import compute_candidate_sizing

    # Standby (House-Money inactive)
    assert compute_candidate_sizing(50000.0, 195.0, {"house_money_active": False, "house_money_multiplier": 1.25})["effective_budget_thb"] == 67500.0
    assert compute_candidate_sizing(50000.0, 120.0, {"house_money_active": False, "house_money_multiplier": 1.25})["effective_budget_thb"] == 50000.0
    assert compute_candidate_sizing(50000.0, 53.33, {"house_money_active": False, "house_money_multiplier": 1.25})["effective_budget_thb"] == 35000.0
    assert compute_candidate_sizing(50000.0, None, {"house_money_active": False, "house_money_multiplier": 1.0})["effective_budget_thb"] == 50000.0

    # Active House-Money (1.25x -> rounded 2 decimals: 1.69x / 1.25x / 0.88x)
    t1 = compute_candidate_sizing(50000.0, 180.0, {"house_money_active": True, "house_money_multiplier": 1.25})
    assert t1["effective_multiplier"] == 1.69 and t1["effective_budget_thb"] == 84500.0
    t2 = compute_candidate_sizing(50000.0, 100.0, {"house_money_active": True, "house_money_multiplier": 1.25})
    assert t2["effective_multiplier"] == 1.25 and t2["effective_budget_thb"] == 62500.0
    t3 = compute_candidate_sizing(50000.0, 99.9, {"house_money_active": True, "house_money_multiplier": 1.25})
    assert t3["effective_multiplier"] == 0.88 and t3["effective_budget_thb"] == 44000.0

    # End-to-end poll + submit uses scaled budget per candidate
    fx = _seed_fixtures(tmp_path, date_str="2026-11-16", quota={"long": 3, "short": 0}, mode="LONG ONLY")
    cand_path = fx["scanner_dir"] / "candidates_2026-11-16.json"
    cand_raw = json.loads(cand_path.read_text(encoding="utf-8"))
    cand_raw["decision"]["house_money"] = {"active": True, "multiplier": 1.25}
    for c in cand_raw["candidates"]:
        if c["ticker"] == "JMT":
            c["_extra"]["pool_score"] = 200.0  # Tier 1 + HM -> 1.69x = 84,500
    cand_path.write_text(json.dumps(cand_raw, ensure_ascii=False, indent=2), encoding="utf-8")

    pl_path = fx["pl_path"]
    pl_raw = json.loads(pl_path.read_text(encoding="utf-8"))
    pl_raw["house_money"] = {"active": True, "multiplier": 1.25}
    pl_path.write_text(json.dumps(pl_raw, ensure_ascii=False, indent=2), encoding="utf-8")

    svc = SuggestEntryService(
        scanner_dir=str(fx["scanner_dir"]),
        positions_ledger_path=str(fx["pl_path"]),
        entry_ledger_path=str(fx["el_path"]),
        state_path=str(fx["state_path"]),
        clock_fn=lambda: datetime(2026, 11, 16, 10, 30, tzinfo=_BKK),
    )
    svc.save_config(50000.0)
    quotes = {
        "JMT": {"available": True, "last": 10.0, "prior_close": 10.2, "bid_1": 9.95, "offer_1": 10.0, "lure_plan": {"ok": True, "price": 10.0}, "match_plan": {"ok": True, "price": 10.0}},
    }
    _patch_live_mocks(monkeypatch, quotes=quotes, lib_line=500000.0)
    poll = await svc.poll_candidates()
    jmt = next(c for c in poll["candidates"] if c["ticker"] == "JMT")
    assert jmt["edge_tier"] == "TIER1"
    assert jmt["effective_multiplier"] == 1.69
    assert jmt["effective_budget_thb"] == 84500.0
    assert jmt["planned_shares"] == 8400  # 84,500 / 10.0 -> 8,400 shares

    submitted_vols = []
    async def fake_exec(**kw):
        submitted_vols.append((kw["symbol"], kw["volume"]))
        return {"ok": True, "dry_run": True, "draft_id": "dr_1", "order": {"symbol": kw["symbol"], "volume": kw["volume"], "price": kw["price"]}, "execution": {"broker_order_no": "B-1"}}

    monkeypatch.setattr(trading_facade, "execute_trade_with_idempotency", fake_exec)
    res = await svc.submit_selected([{"side": "LONG", "symbol": "JMT", "price_mode": "lure"}])
    assert res["ok"] is True
    assert submitted_vols == [("JMT", 8400)]


@pytest.mark.asyncio
async def test_short_opening_uses_open_plan_plus1_and_retries_broker_short_reject(monkeypatch, tmp_path: Path):
    fx = _seed_fixtures(tmp_path, quota={"long": 0, "short": 2}, mode="SHORT", free_slots=3)
    svc = SuggestEntryService(
        scanner_dir=str(fx["scanner_dir"]),
        positions_ledger_path=str(fx["pl_path"]),
        entry_ledger_path=str(fx["el_path"]),
        state_path=str(fx["state_path"]),
        clock_fn=lambda: datetime(2026, 11, 16, 10, 45, tzinfo=_BKK),
    )
    svc.save_config(50000.0)
    quotes = {
        "CKP": {
            "available": True,
            "last": 2.30,
            "prior_close": 2.28,
            "bid_1": 2.28,
            "offer_1": 2.30,
            "short_open_uptick_ticks": 1,
            "short_uptick_ticks": 0,
            "open_plan": {"ok": True, "price": 2.32},
            "lure_plan": {"ok": True, "price": 2.30},
            "match_plan": {"ok": True, "price": 2.28},
            "match_allowed": True,
        }
    }
    _patch_live_mocks(monkeypatch, quotes=quotes, kss_line=200000.0)
    poll = await svc.poll_candidates()
    ckp = next(c for c in poll["candidates"] if c["ticker"] == "CKP")
    assert ckp["lure_price"] == 2.32
    assert ckp["match_allowed"] is False

    # Simulate broker returning non-E_UPTICK_VIOLATION error text -> still retries +1 cleanly
    seen_prices = []

    async def fake_sbl_action(**kw):
        return {
            "ok": True,
            "dry_run": False,
            "auto_short": {
                "ok": False,
                "code": "BROKER_REJECTED",
                "error": "Short Sale order price must be higher than last price",
            },
        }

    async def fake_retry_exec(**kw):
        seen_prices.append(kw["price"])
        return {
            "ok": True,
            "dry_run": False,
            "draft_id": "dr_retry",
            "order": {"symbol": kw["symbol"], "volume": kw["volume"], "price": kw["price"]},
            "execution": {"broker_order_no": "KSS-R1"},
        }

    async def fake_compute_nego(broker="kss", symbol="CKP", side="SHORT"):
        return {"available": True, "last": 2.32}

    monkeypatch.setattr(trading_facade, "execute_sbl_action", fake_sbl_action)
    monkeypatch.setattr(trading_facade, "execute_trade_with_idempotency", fake_retry_exec)
    monkeypatch.setattr(trading_facade, "compute_nego_quote", fake_compute_nego)

    sub = await svc.submit_selected([{"side": "SHORT", "symbol": "CKP", "price_mode": "match"}])
    assert sub["ok"] is True
    assert sub["results"][0]["retried_uptick"] is True
    assert seen_prices == [2.34]  # fresh last 2.32 + 1 tick (0.02) = 2.34
