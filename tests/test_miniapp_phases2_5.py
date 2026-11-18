"""Tests for LINE MiniApp Phases 2–5: Portfolio/SBL, Orders/Nego, Trade/Idempotency, Rich Menu 2x3 & Flex Cards."""

from types import SimpleNamespace
import pytest
from httpx import ASGITransport, AsyncClient

from app.api.v1.endpoints import miniapp as miniapp_ep
from app.main import app
from app.services.broker_cdp import trading_facade
from app.services.line_cli import cards, handlers, rich_menu


@pytest.fixture(autouse=True)
def _miniapp_test_env(monkeypatch):
    miniapp_ep._TOKEN_CACHE.clear()
    trading_facade._TRADE_IDEMPOTENCY.clear()
    trading_facade._TRADE_LOCKS.clear()
    monkeypatch.setattr(miniapp_ep.settings, "MINIAPP_DEV_BYPASS_LOCAL", True)
    monkeypatch.setattr(miniapp_ep.settings, "LIFF_ID", "2012540531-S5Ozs4US")
    monkeypatch.setattr(miniapp_ep.settings, "MINIAPP_PUBLIC_BASE_URL", "https://miniapp.line.me")
    monkeypatch.setattr(miniapp_ep.settings, "LINE_ADMIN_USER_ID", "U_ADMIN_1")
    monkeypatch.setattr(trading_facade.kill_switch_service, "is_active", lambda: False)
    monkeypatch.setattr(trading_facade.trade_dispatcher, "_current_mode", "TEST")
    yield
    trading_facade._TRADE_IDEMPOTENCY.clear()
    trading_facade._TRADE_LOCKS.clear()


# --------------------------------------------------------------------------- #
# Phase 2 — Portfolio, Cash, KSS Account Switch & SBL Pool
# --------------------------------------------------------------------------- #
def test_p2_t01_merge_borrow_unshorted_bcpg():
    summary = handlers._sbl_borrow_summary([
        {"symbol": "BCPG", "remaining_qty": 100, "borrow_fee_rate": 3.5, "status": "ACTIVE"},
    ])
    merged = handlers._merge_borrow([], summary)
    assert len(merged) == 1
    assert merged[0]["symbol"] == "BCPG"
    assert merged[0]["position_type"] == "BORROWED"
    assert merged[0]["borrow_qty"] == 100
    assert merged[0]["returnable_qty"] == 100


@pytest.mark.asyncio
async def test_p2_t02_portfolio_partial_anti_mock(monkeypatch):
    async def fake_scrape_port(broker: str):
        if broker == "lib":
            raise RuntimeError("LIBERATOR DAEMON_DOWN")
        return {
            "account_no": "1000001",
            "account_type": "MARGIN",
            "cash_balance": 50000.0,
            "line_available": 150000.0,
            "excess_equity": 120000.0,
            "holdings": [
                {"symbol": "PTT", "position_type": "SHORT", "volume": 1000, "avg_cost": 35.0, "current_price": 34.0},
            ],
        }

    async def fake_sbl_lots(force_refresh: bool = False):
        return [{"symbol": "PTT", "remaining_qty": 1000, "borrow_qty": 1000, "status": "ACTIVE", "borrow_fee_rate": 3.5}]

    monkeypatch.setattr(trading_facade, "scrape_portfolio_from_cdp", fake_scrape_port)
    monkeypatch.setattr(trading_facade.krungsri_sbl_driver, "scrape_sbl_lots", fake_sbl_lots)
    monkeypatch.setattr(trading_facade, "get_active_kss_account", lambda: "1000001")
    monkeypatch.setattr(trading_facade, "get_active_kss_account_type", lambda: "MARGIN")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/v1/miniapp/portfolio?broker=all")
        assert res.status_code == 200
        body = res.json()
        assert body["brokers"]["kss"]["available"] is True
        assert body["brokers"]["kss"]["line_available"] == 150000.0
        assert body["brokers"]["lib"]["available"] is False
        assert body["brokers"]["lib"]["line_available"] is None
        assert body["summary"]["partial"] is True
        assert len(body["brokers"]["kss"]["rows"]) == 1
        assert body["brokers"]["kss"]["rows"][0]["symbol"] == "PTT"


@pytest.mark.asyncio
async def test_p2_t03_switch_kss_account_validation(monkeypatch):
    switched = []

    async def fake_switch(acct: str, target: str = "streaming"):
        switched.append((acct, target))
        return {"ok": True, "active": acct, "switched": True}

    async def fake_scrape_port(broker: str):
        return {"account_no": "1000002", "account_type": "CASH", "line_available": 25000.0, "holdings": []}

    monkeypatch.setattr(trading_facade, "switch_kss_account", fake_switch)
    monkeypatch.setattr(trading_facade, "scrape_portfolio_from_cdp", fake_scrape_port)
    monkeypatch.setattr(trading_facade.krungsri_sbl_driver, "scrape_sbl_lots", lambda force_refresh=False: _async_val([]))
    monkeypatch.setattr(trading_facade.settings, "STREAMING_ACCOUNT_NO_MARGIN", "1000001")
    monkeypatch.setattr(trading_facade.settings, "STREAMING_ACCOUNT_NO_CASH", "1000002")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res_bad = await client.post("/api/v1/miniapp/portfolio/kss-account", json={"account_no": "9999999"})
        assert res_bad.status_code == 400
        assert switched == []

        res_ok = await client.post("/api/v1/miniapp/portfolio/kss-account", json={"account_no": "1000002"})
        assert res_ok.status_code == 200
        assert switched == [("1000002", "streaming")]
        assert res_ok.json()["brokers"]["kss"]["account_no"] == "1000002"


@pytest.mark.asyncio
async def test_p2_t03b_account_routing_and_trade_defaults(monkeypatch, tmp_path):
    routing_file = tmp_path / "account_routing.json"
    monkeypatch.setenv("ACCOUNT_ROUTING_PATH", str(routing_file))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r_get = await client.get("/api/v1/miniapp/brokers/account-routing")
        assert r_get.status_code == 200
        rt = r_get.json()["account_routing"]
        assert (rt["short_broker"], rt["short_account"]) == ("kss", "margin")
        assert (rt["long_broker"], rt["long_account"]) == ("lib", "cash")

        # Short cannot be routed to Liberator Cash
        r_bad = await client.post(
            "/api/v1/miniapp/brokers/account-routing",
            json={"short_broker": "lib", "short_account": "cash", "long_broker": "lib", "long_account": "cash"},
        )
        assert r_bad.status_code == 400

        # Configure Long primary to KSS Cash
        r_save = await client.post(
            "/api/v1/miniapp/brokers/account-routing",
            json={"short_broker": "kss", "short_account": "margin", "long_broker": "kss", "long_account": "cash"},
        )
        assert r_save.status_code == 200
        assert r_save.json()["account_routing"]["long_broker"] == "kss"
        assert r_save.json()["account_routing"]["long_account"] == "cash"

    # build_trade_order_from_input picks up persisted default routing when broker/account omitted
    order_buy = trading_facade.build_trade_order_from_input(
        side="buy",
        symbol="PTT",
        amount_mode="volume",
        volume=100,
        price=35.0,
    )
    assert (order_buy.broker, order_buy.account) == ("kss", "cash")

    order_short = trading_facade.build_trade_order_from_input(
        side="short",
        symbol="CHG",
        amount_mode="volume",
        volume=1000,
        price=1.60,
    )
    assert (order_short.broker, order_short.account) == ("kss", "margin")


@pytest.mark.asyncio
async def test_p2_t03c_trading_rules_advance_setting_and_short_match(monkeypatch, tmp_path):
    rules_file = tmp_path / "trading_rules.json"
    monkeypatch.setenv("TRADING_RULES_PATH", str(rules_file))
    monkeypatch.setattr(
        trading_facade,
        "_broker_quote",
        lambda broker, sym: _async_val({"success": True, "bid_1": 10.0, "offer_1": 10.05, "last": 10.0, "prior_close": 10.0}),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r_get = await client.get("/api/v1/miniapp/system/trading-rules")
        assert r_get.status_code == 200
        rules = r_get.json()["trading_rules"]
        assert rules["short_open_uptick_ticks"] == 1
        assert rules["short_uptick_ticks"] == 0
        assert rules["auto_minus10_strict"] is True
        assert rules["strict_uptick_symbols"] == []

        # Under default policy: Opening uses +1 (10.05), while Nego uses +0 Zero-Plus (can match at bid1 == last 10.0)
        q0 = await client.get("/api/v1/miniapp/nego/quote?broker=kss&symbol=PTT&side=SHORT&old_price=10.20")
        assert q0.status_code == 200
        qd0 = q0.json()
        assert qd0["short_open_uptick_ticks"] == 1
        assert qd0["short_uptick_ticks"] == 0
        assert qd0["open_plan"]["ok"] is True
        assert qd0["open_plan"]["price"] == 10.05
        assert qd0["match_allowed"] is True
        assert qd0["match_plan"]["ok"] is True
        assert qd0["match_plan"]["price"] == 10.0

        # Add DELTA as per-symbol +1 override while keeping global +0
        r_save = await client.post(
            "/api/v1/miniapp/system/trading-rules",
            json={"short_uptick_ticks": 0, "strict_uptick_symbols": "delta, kbank"},
        )
        assert r_save.status_code == 200
        saved = r_save.json()["trading_rules"]
        assert saved["strict_uptick_symbols"] == ["DELTA", "KBANK"]

        q_delta = await client.get("/api/v1/miniapp/nego/quote?broker=kss&symbol=DELTA&side=SHORT&old_price=10.20")
        assert q_delta.status_code == 200
        assert q_delta.json()["short_uptick_ticks"] == 1
        assert q_delta.json()["match_allowed"] is False

        # Global +1 Strict Uptick blocks SHORT match when bid == last
        r_strict = await client.post(
            "/api/v1/miniapp/system/trading-rules",
            json={"short_uptick_ticks": 1, "strict_uptick_symbols": []},
        )
        assert r_strict.status_code == 200
        q1 = await client.get("/api/v1/miniapp/nego/quote?broker=kss&symbol=PTT&side=SHORT&old_price=10.20")
        assert q1.status_code == 200
        assert q1.json()["short_uptick_ticks"] == 1
        assert q1.json()["match_allowed"] is False


@pytest.mark.asyncio
async def test_p2_t04_sbl_pool_quote(monkeypatch):
    monkeypatch.setattr(
        trading_facade.krungsri_sbl_driver,
        "scrape_sbl_available_inventory",
        lambda sym: _async_val(45000),
    )
    monkeypatch.setattr(
        trading_facade.krungsri_sbl_driver,
        "cache_status",
        lambda: {"state": "CACHE_FRESH", "age_seconds": 12},
    )
    monkeypatch.setattr(
        trading_facade.krungsri_sbl_driver,
        "check_symbol_risk",
        lambda sym: {"is_blocked": False, "verdict": "BORROW_ALLOWED", "borrow_fee_pct": 4.25},
    )
    monkeypatch.setattr(
        trading_facade.sbl_intelligence_service,
        "check_xd_risk",
        lambda sym: _async_val({"xd_risk": False, "type": None}),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/v1/miniapp/sbl/pool/ptt")
        assert res.status_code == 200
        data = res.json()
        assert data["symbol"] == "PTT"
        assert data["available_qty"] == 45000
        assert data["fee_rate"] == 4.25
        assert data["xd_risk"]["xd_risk"] is False


@pytest.mark.asyncio
async def test_p2_t05_static_js_views_served():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for file_path in (
            "/miniapp/static/js/utils.js",
            "/miniapp/static/js/views/portfolio.js",
            "/miniapp/static/js/views/orders.js",
            "/miniapp/static/js/views/nego.js",
            "/miniapp/static/js/views/trade.js",
        ):
            res = await client.get(file_path)
            assert res.status_code == 200

        res_utils = await client.get("/miniapp/static/js/utils.js")
        assert "nextSetTick" in res_utils.text
        assert "calcBoardLotsFromBudget" in res_utils.text
        assert "renderQuickQuoteBox" in res_utils.text

        res_port = await client.get("/miniapp/static/js/views/portfolio.js")
        assert "no-store" in res_port.headers.get("cache-control", "")
        for token in ("row-borrowed", "row-short", "row-long", "port-table", "port-kpi-3", "data-expand-row", "Array.isArray(sbl.lots)"):
            assert token in res_port.text
        assert "50,000 (1x)" not in res_port.text

        res_trade = await client.get("/miniapp/static/js/views/trade.js")
        for token in ('data-subtab="quick"', "BUDGET_MULTIPLIERS", "getSavedBaseBudget", "getMultiplierPresets", "openBudgetNegotiationSheet", "tr-advance-toggle"):
            assert token in res_trade.text

        res_app = await client.get("/miniapp/static/js/app.js")
        assert res_app.status_code == 200
        for sym in ("renderPortfolioView", "renderOrdersView", "renderNegoView", "renderTradeView", "renderBrokersView"):
            assert sym in res_app.text


@pytest.mark.asyncio
async def test_p2_t06_sbl_borrow_budget_value_and_dry_run_auto_short(monkeypatch):
    borrow_calls = []
    persist_calls = []

    monkeypatch.setattr(
        trading_facade.sbl_intelligence_service,
        "check_xd_risk",
        lambda sym: _async_val({"xd_risk": False}),
    )
    monkeypatch.setattr(
        trading_facade.krungsri_sbl_driver,
        "execute_sbl_borrow",
        lambda sym, vol: _async_val(borrow_calls.append((sym, vol)) or {"success": True, "status": "SBL_BORROW_DRY_RUN"}),
    )
    monkeypatch.setattr(trading_facade.krungsri_sbl_driver, "invalidate_cache", lambda: None)
    monkeypatch.setattr(
        trading_facade,
        "_persist_live_sbl_borrow_lot",
        lambda *a, **kw: _async_val(persist_calls.append(a) or True),
    )
    monkeypatch.setattr(
        trading_facade,
        "_broker_quote",
        lambda broker, sym: _async_val({"success": True, "bid_1": 34.0, "offer_1": 34.20, "last": 34.0, "prior_close": 34.0}),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 75,000 / 34.20 = 2,192.98 -> board-lot floor = 2,100 shares
        res_ok = await client.post(
            "/api/v1/miniapp/sbl/action",
            json={
                "action": "borrow",
                "symbol": "PTT",
                "amount_mode": "value",
                "target_value": 75000.0,
                "ref_price": 34.20,
                "auto_short": True,
                "short_price": 34.20,
            },
        )
        assert res_ok.status_code == 200
        body = res_ok.json()
        assert body["ok"] is True
        assert body["volume"] == 2100
        assert body["dry_run"] is True
        assert body["persisted"] is False
        assert persist_calls == []
        assert body["auto_short"]["ok"] is True
        assert body["partial_short_failed"] is False
        assert borrow_calls[-1] == ("PTT", 2100)

        # Short price below last (33.70 < last 34.00) keeps borrow OK but marks auto_short failed
        res_bad_uptick = await client.post(
            "/api/v1/miniapp/sbl/action",
            json={
                "action": "borrow",
                "symbol": "PTT",
                "amount_mode": "value",
                "target_value": 50000.0,
                "ref_price": 34.0,
                "auto_short": True,
                "short_price": 33.70,
            },
        )
        assert res_bad_uptick.status_code == 200
        b2 = res_bad_uptick.json()
        assert b2["ok"] is True
        assert b2["volume"] == 1400
        assert b2["auto_short"]["ok"] is False
        assert b2["partial_short_failed"] is True


@pytest.mark.asyncio
async def test_p2_t07_live_borrow_persists_lot_and_auto_short_partial_graceful(monkeypatch):
    persist_calls = []

    monkeypatch.setattr(trading_facade.settings, "DRY_RUN_MODE", False)
    monkeypatch.setattr(trading_facade.trade_dispatcher, "_current_mode", "LIVE")
    monkeypatch.setattr(
        trading_facade.sbl_intelligence_service,
        "check_xd_risk",
        lambda sym: _async_val({"xd_risk": False}),
    )
    monkeypatch.setattr(
        trading_facade.krungsri_sbl_driver,
        "execute_sbl_borrow",
        lambda sym, vol: _async_val({"success": True, "status": "SUBMITTED", "transaction_id": "TX-LIVE-1", "fee_rate": 3.75}),
    )
    monkeypatch.setattr(trading_facade.krungsri_sbl_driver, "invalidate_cache", lambda: None)
    monkeypatch.setattr(
        trading_facade,
        "_persist_live_sbl_borrow_lot",
        lambda *a, **kw: _async_val(persist_calls.append(a) or True),
    )

    async def fail_short(**_kw):
        raise trading_facade.FacadeHttpError(422, "E_VALIDATOR", "Uptick blocked")

    monkeypatch.setattr(trading_facade, "execute_trade_with_idempotency", fail_short)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/v1/miniapp/sbl/action",
            json={
                "action": "borrow",
                "symbol": "BCPG",
                "amount_mode": "value",
                "target_value": 100000.0,
                "ref_price": 10.0,
                "auto_short": True,
                "short_price": 10.10,
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["dry_run"] is False
        assert body["persisted"] is True
        assert body["volume"] == 10000
        assert body["partial_short_failed"] is True
        assert body["auto_short"]["code"] == "E_VALIDATOR"
        assert len(persist_calls) == 1
        assert persist_calls[0][0:3] == ("BCPG", 10000, 3.75)


@pytest.mark.asyncio
async def test_p2_t08_quick_buy_liberator_budget_route(monkeypatch):
    exec_calls = []

    async def fake_pre(order, db=None):
        return {
            "ok": True,
            "account_no": "LIB-777",
            "resolved_volume": 2900,
            "resolved_price": float(order.price or 34.25),
        }

    async def fake_exec(draft, **_kw):
        exec_calls.append(draft)
        return {"success": True, "status": "DRY_RUN", "broker_order_no": None}

    monkeypatch.setattr(trading_facade, "_pre_trade_check", fake_pre)
    monkeypatch.setattr(trading_facade, "execute_confirmed_order", fake_exec)
    monkeypatch.setattr(trading_facade, "best_effort_push_card", lambda _c: _async_val(False))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/v1/miniapp/trade/execute",
            json={
                "side": "buy",
                "symbol": "PTT",
                "broker": "lib",
                "account": "cash",
                "amount_mode": "value",
                "target_value": 100000.0,
                "price": 34.25,
                "idempotency_key": "quick-lib-2x-001",
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["order"]["broker"] == "lib"
        assert body["order"]["account"] == "cash"
        assert len(exec_calls) == 1
        assert exec_calls[0]["broker"] == "lib"
        assert exec_calls[0]["cmd"] == "buy"


# --------------------------------------------------------------------------- #
# Phase 3 — Live Orders, Cancel & Fast Nego
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_p3_t01_nego_short_match_rejected(monkeypatch):
    modify_calls = []

    async def fake_modify(*_args, **_kwargs):
        modify_calls.append(True)
        return {"success": True}

    monkeypatch.setattr(trading_facade, "_broker_modify", fake_modify)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/v1/miniapp/nego/execute",
            json={
                "broker": "kss",
                "order_no": "11007385",
                "symbol": "PTT",
                "side": "SHORT",
                "volume": 500,
                "old_price": 35.0,
                "mode": "match",
            },
        )
        assert res.status_code == 422
        assert res.json()["detail"]["code"] == "NEGO_SHORT_MATCH_NOT_ALLOWED"
        assert modify_calls == []


@pytest.mark.asyncio
async def test_p3_t02_nego_and_cancel_kill_switch_locked(monkeypatch):
    monkeypatch.setattr(trading_facade.kill_switch_service, "is_active", lambda: True)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r_nego = await client.post(
            "/api/v1/miniapp/nego/execute",
            json={
                "broker": "kss",
                "order_no": "11007385",
                "symbol": "PTT",
                "side": "BUY",
                "volume": 100,
                "old_price": 34.0,
                "mode": "lure",
            },
        )
        assert r_nego.status_code == 423

        r_cancel = await client.post(
            "/api/v1/miniapp/orders/cancel",
            json={"broker": "kss", "order_no": "11007385", "symbol": "PTT"},
        )
        assert r_cancel.status_code == 423


@pytest.mark.asyncio
async def test_p3_t03_nego_bad_tick_rejected(monkeypatch):
    modify_calls = []
    monkeypatch.setattr(trading_facade, "_broker_modify", lambda *a, **k: _async_val(modify_calls.append(True)))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/v1/miniapp/nego/execute",
            json={
                "broker": "kss",
                "order_no": "8841201",
                "symbol": "PTT",
                "side": "BUY",
                "volume": 500,
                "old_price": 34.0,
                "mode": "custom",
                "custom_price": 34.05,  # 0.10 tick required in [25, 50)
            },
        )
        assert res.status_code == 422
        assert res.json()["detail"]["code"] == "E_BAD_TICK"
        assert modify_calls == []


@pytest.mark.asyncio
async def test_p3_t04_nego_execute_dry_run_success(monkeypatch):
    modified = []

    async def fake_quote(broker: str, symbol: str):
        return {"success": True, "symbol": symbol, "bid_1": 34.20, "offer_1": 34.50, "last": 34.20, "prior_close": 34.70}

    async def fake_modify(broker: str, **kwargs):
        modified.append((broker, kwargs))
        return {"success": True, "status": "DRY_RUN"}

    monkeypatch.setattr(trading_facade, "_broker_quote", fake_quote)
    monkeypatch.setattr(trading_facade, "_broker_modify", fake_modify)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/v1/miniapp/nego/execute",
            json={
                "broker": "kss",
                "order_no": "8841201",
                "symbol": "PTT",
                "side": "BUY",
                "volume": 500,
                "old_price": 33.70,
                "mode": "lure",
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["dry_run"] is True
        assert body["new_price"] == 34.20
        assert len(modified) == 1
        assert modified[0][1]["new_price"] == 34.20


@pytest.mark.asyncio
async def test_p3_t05_order_cancel_fail_closed_when_not_open(monkeypatch):
    cancel_calls = []

    async def fake_orders(broker: str):
        return {
            "orders": [
                {"broker_order_no": "1001", "symbol": "PTT", "side": "BUY", "volume": 100, "matched_volume": 100, "balance": 0, "price": 34.0, "status": "MATCHED"},
                {"broker_order_no": "1002", "symbol": "AOT", "side": "BUY", "volume": 100, "matched_volume": 0, "balance": 100, "price": 60.0, "status": "OPEN"},
            ]
        }

    async def fake_cancel(broker: str, **kwargs):
        cancel_calls.append((broker, kwargs))
        return {"success": True}

    monkeypatch.setattr(trading_facade, "scrape_orders_from_cdp", fake_orders)
    monkeypatch.setattr(trading_facade, "_broker_cancel", fake_cancel)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res_closed = await client.post(
            "/api/v1/miniapp/orders/cancel",
            json={"broker": "lib", "order_no": "1001", "symbol": "PTT"},
        )
        assert res_closed.status_code == 409
        assert cancel_calls == []

        res_open = await client.post(
            "/api/v1/miniapp/orders/cancel",
            json={"broker": "lib", "order_no": "1002", "symbol": "AOT"},
        )
        assert res_open.status_code == 200
        assert res_open.json()["ok"] is True
        assert len(cancel_calls) == 1


# --------------------------------------------------------------------------- #
# Phase 4 — Trade Preview, Budget Sizing, Idempotency & SBL Borrow/Return
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_p4_t01_trade_preview_odd_lot_rejected():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/v1/miniapp/trade/preview",
            json={"side": "buy", "symbol": "PTT", "volume": 150, "price": 34.25},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is False
        assert body["code"] == "E_BAD_LOT"


@pytest.mark.asyncio
async def test_p4_t02_trade_preview_short_sbl_xd_and_missing_lot_rejected(monkeypatch):
    from app.brokers.krungsri.sbl_realtime_driver import krungsri_sbl_driver

    monkeypatch.setattr(
        krungsri_sbl_driver,
        "check_symbol_risk",
        lambda sym: {"is_blocked": True, "verdict": "HARD_BLOCKED_XD_XR", "reason": "PTT ติด XD"},
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res_xd = await client.post(
            "/api/v1/miniapp/trade/preview",
            json={"side": "short", "symbol": "PTT", "volume": 1000, "price": 34.50},
        )
        assert res_xd.status_code == 200
        assert res_xd.json()["ok"] is False
        assert res_xd.json()["code"] == "E_SBL_XD_BLOCKED"

        monkeypatch.setattr(
            krungsri_sbl_driver,
            "check_symbol_risk",
            lambda sym: {"is_blocked": False, "verdict": "SAFE"},
        )
        monkeypatch.setattr(handlers, "_get_active_sbl_lots", lambda sym, db=None: _async_val([]))
        monkeypatch.setattr(
            trading_facade,
            "_pre_trade_check",
            handlers._pre_trade_check,
        )
        # patch _get_active_sbl_lots inside handlers directly so _pre_trade_check sees 0 lots
        res_no_lot = await client.post(
            "/api/v1/miniapp/trade/preview",
            json={"side": "short", "symbol": "PTT", "volume": 1000, "price": 34.50},
        )
        assert res_no_lot.status_code == 200
        assert res_no_lot.json()["ok"] is False
        assert res_no_lot.json()["code"] == "E_NO_SBL"


@pytest.mark.asyncio
async def test_p4_t03_trade_execute_idempotency_and_quota_resilience(monkeypatch):
    exec_calls = []

    async def fake_pre(order, db=None):
        return {
            "ok": True,
            "account_no": "9000001",
            "resolved_volume": int(order.volume or 200),
            "resolved_price": float(order.price or 34.25),
        }

    async def fake_exec(draft, **_kwargs):
        exec_calls.append(draft)
        return {"success": True, "status": "DRY_RUN", "broker_order_no": None}

    async def fail_push(_card):
        return False

    monkeypatch.setattr(trading_facade, "_pre_trade_check", fake_pre)
    monkeypatch.setattr(trading_facade, "execute_confirmed_order", fake_exec)
    monkeypatch.setattr(trading_facade, "best_effort_push_card", fail_push)

    payload = {
        "side": "buy",
        "symbol": "PTT",
        "volume": 200,
        "price": 34.25,
        "idempotency_key": "idem-ptt-001",
    }
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r1 = await client.post("/api/v1/miniapp/trade/execute", json=payload)
        r2 = await client.post("/api/v1/miniapp/trade/execute", json=payload)

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["idempotent_replay"] is False
        assert r2.json()["idempotent_replay"] is True
        assert r1.json()["line_pushed"] is False
        assert len(exec_calls) == 1


@pytest.mark.asyncio
async def test_p4_t04_trade_preview_budget_value_sizing(monkeypatch):
    monkeypatch.setattr(
        handlers,
        "_get_broker_portfolio",
        lambda broker, account_no=None, db=None: _async_val({"line_available": 100000.0, "holdings": []}),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.post(
            "/api/v1/miniapp/trade/preview",
            json={
                "side": "buy",
                "symbol": "PTT",
                "amount_mode": "value",
                "target_value": 10000.0,
                "price": 34.20,
            },
        )
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert body["resolved_volume"] == 200
        assert body["total_thb"] == 6840.0


@pytest.mark.asyncio
async def test_p4_t05_sbl_borrow_xd_blocked_and_return_flow(monkeypatch):
    borrow_calls = []
    return_calls = []

    monkeypatch.setattr(
        trading_facade.krungsri_sbl_driver,
        "execute_sbl_borrow",
        lambda sym, vol: _async_val(borrow_calls.append((sym, vol)) or {"success": True, "status": "SBL_BORROW_DRY_RUN"}),
    )
    monkeypatch.setattr(
        trading_facade.krungsri_sbl_driver,
        "return_sbl_shares",
        lambda **kw: _async_val(return_calls.append(kw) or {"success": True, "status": "SBL_RETURN_DRY_RUN"}),
    )
    monkeypatch.setattr(trading_facade.krungsri_sbl_driver, "invalidate_cache", lambda: None)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(
            trading_facade.sbl_intelligence_service,
            "check_xd_risk",
            lambda sym: _async_val({"xd_risk": True, "reason": "XD tomorrow"}),
        )
        r_blk = await client.post(
            "/api/v1/miniapp/sbl/action",
            json={"action": "borrow", "symbol": "PTT", "volume": 1000},
        )
        assert r_blk.status_code == 422
        assert r_blk.json()["detail"]["code"] == "E_SBL_XD_BLOCKED"
        assert borrow_calls == []

        monkeypatch.setattr(
            trading_facade.sbl_intelligence_service,
            "check_xd_risk",
            lambda sym: _async_val({"xd_risk": False}),
        )
        monkeypatch.setattr(
            trading_facade,
            "_get_active_sbl_lots",
            lambda sym, db=None: _async_val([{"lot_id": "L1", "symbol": "PTT", "remaining_qty": 1000}]),
        )
        r_ret = await client.post(
            "/api/v1/miniapp/sbl/action",
            json={"action": "return", "symbol": "PTT", "amount_mode": "all"},
        )
        assert r_ret.status_code == 200
        assert r_ret.json()["volume"] == 1000
        assert len(return_calls) == 1


@pytest.mark.asyncio
async def test_p4_t06_trade_execute_kill_switch_locked(monkeypatch):
    monkeypatch.setattr(trading_facade.kill_switch_service, "is_active", lambda: True)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r_tr = await client.post(
            "/api/v1/miniapp/trade/execute",
            json={"side": "buy", "symbol": "PTT", "volume": 100, "price": 34.0, "idempotency_key": "key-kill-1"},
        )
        assert r_tr.status_code == 423


# --------------------------------------------------------------------------- #
# Phase 5 — Rich Menu 1x3 Compact + Upgraded Flex Cards
# --------------------------------------------------------------------------- #
def test_p5_t01_rich_menu_2x3_spec_geometry_and_uris():
    spec = rich_menu.get_homelab_rich_menu_spec("2012540531-S5Ozs4US")
    assert spec["size"] == {"width": 2500, "height": 843}
    areas = spec["areas"]
    assert len(areas) == 1
    act = areas[0]["action"]
    assert act["type"] == "uri"
    assert act["uri"] == "https://miniapp.line.me/2012540531-S5Ozs4US"


def test_p5_t02_flex_cards_carry_miniapp_deep_links():
    st = cards.status_card(
        brokers=[{"key": "kss", "name": "KSS", "emoji": "🟢", "line1": "alive"}],
        relay_ready=True,
        relay_label="line_group",
        mode_badge="DRY-RUN",
        kill_active=False,
        updated="09:20",
    )
    assert "tab=brokers" in str(st)

    pt = cards.port_card(
        broker="kss",
        account="1000001",
        amount=10000.0,
        pnl_value=500.0,
        pnl_pct=5.0,
        rows=[],
        snapshot_label="09:20",
        stale=False,
        live=True,
    )
    assert "tab=portfolio" in str(pt)

    od = cards.orders_card(
        broker="kss",
        total=1,
        open_count=1,
        rows=[],
        snapshot_label="09:20",
        live=True,
        show_nego=True,
    )
    assert "tab=orders" in str(od)

    un = cards.unavailable_card(broker="KSS", login_cmd="/login kss")
    assert "focus=otp" in str(un)

    rc = cards.trade_receipt_card(draft_id="d1", outcome="confirmed", cmd="buy", symbol="PTT", volume=100, price=34.0)
    assert "tab=orders" in str(rc)

    hub = cards.quick_hub_card()
    assert "tab=trade" in str(hub)
    assert "action=nego_menu" in str(hub)


@pytest.mark.asyncio
async def test_p5_t03_handle_postback_quick_cards_menu_uses_free_reply():
    replies = []
    pushes = []

    class DummyService:
        async def _send_reply(self, token, msg):
            replies.append((token, msg))

        async def push_message(self, msg, to_user_id=None):
            pushes.append((to_user_id, msg))

    res = await handlers.handle_postback(
        "action=quick_cards_menu",
        raw_event={"replyToken": "rt-free-1", "source": {"type": "user", "userId": "U_ADMIN_1"}},
        service=DummyService(),
    )
    assert res == {"status": "QUICK_CARDS_MENU_DISPLAYED"}
    assert len(replies) == 1
    assert replies[0][0] == "rt-free-1"
    assert replies[0][1]["type"] == "flex"
    assert pushes == []


async def _async_val(val):
    return val


@pytest.mark.asyncio
async def test_p2_webview_dynamic_asset_version_prefix_and_no_cache():
    import re

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res_html = await client.get("/miniapp")
        assert res_html.status_code == 200
        assert "no-store" in (res_html.headers.get("cache-control") or "")
        assert res_html.headers.get("cdn-cache-control") == "no-store"
        html = res_html.text

        meta_match = re.search(r'<meta name="miniapp-version" content="([0-9a-f]{10})">', html)
        assert meta_match is not None
        ver = meta_match.group(1)

        assert f'/miniapp/static/v/{ver}/css/app.css"' in html
        assert f'/miniapp/static/v/{ver}/js/app.js"' in html

        res_cfg = await client.get("/miniapp/config.json")
        assert res_cfg.status_code == 200
        assert res_cfg.json()["assetVersion"] == ver

        res_ver_api = await client.get(f"/miniapp/static/v/{ver}/js/api.js")
        assert res_ver_api.status_code == 200
        assert "no-store" in (res_ver_api.headers.get("cache-control") or "")
        assert "getPortfolio" in res_ver_api.text
        assert "AbortController" in res_ver_api.text

        res_unver_api = await client.get("/miniapp/static/js/api.js")
        assert res_unver_api.status_code == 200
        assert "getPortfolio" in res_unver_api.text


@pytest.mark.asyncio
async def test_p2_webview_versioned_static_blocks_traversal_and_clean_esm_imports():
    from pathlib import Path

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        for bad_path in (
            "/miniapp/static/v/deadbeef00/../../main.py",
            "/miniapp/static/v/deadbeef00/..%2f..%2fmain.py",
            "/miniapp/static/v/deadbeef00/../router.py",
        ):
            res_bad = await client.get(bad_path)
            assert res_bad.status_code in (400, 404)

    static_root = Path("app/miniapp/static")
    for p in sorted(static_root.rglob("*")):
        if p.is_file() and p.suffix in {".html", ".js"}:
            content = p.read_text(encoding="utf-8")
            assert "?v=20261111p2" not in content, f"stale query suffix in {p}"

    app_js = (static_root / "js/app.js").read_text(encoding="utf-8")
    assert 'from "./api.js"' in app_js
    assert 'from "./auth.js"' in app_js
    assert "clearToast" in app_js

    api_js = (static_root / "js/api.js").read_text(encoding="utf-8")
    assert "DEFAULT_TIMEOUT_MS = 20000" in api_js
    assert "E_TIMEOUT" in api_js

    for view_name in ("portfolio.js", "orders.js", "nego.js"):
        view_js = (static_root / "js/views" / view_name).read_text(encoding="utf-8")
        assert "renderErrorRetryCard" in view_js


@pytest.mark.asyncio
async def test_p4_sbl_return_fifo_multi_lot_lot_id_and_remark(monkeypatch):
    from app.services.broker_cdp import trading_facade

    lots = [
        {
            "symbol": "AAV",
            "lot_id": "B495095",
            "broker_tx_id": "B495095",
            "borrow_date": "10/11/2026",
            "time": "11:00:00",
            "remaining_qty": 3000,
            "status": "ACTIVE",
        },
        {
            "symbol": "AAV",
            "lot_id": "B495090",
            "broker_tx_id": "B495090",
            "borrow_date": "09/11/2026",
            "time": "10:32:28",
            "remaining_qty": 2000,
            "status": "ACTIVE",
        },
    ]
    scrape_flags = []

    async def fake_scrape(force_refresh: bool = False):
        scrape_flags.append(force_refresh)
        return list(lots)

    calls = []

    async def fake_return_shares(lot_id, return_qty, symbol=None, remark=""):
        calls.append((lot_id, return_qty, symbol, remark))
        return {
            "success": True,
            "status": "SBL_RETURN_DRY_RUN",
            "lot_id": lot_id,
            "return_qty": return_qty,
            "dry_run": True,
        }

    monkeypatch.setattr(trading_facade.krungsri_sbl_driver, "scrape_sbl_lots", fake_scrape)
    monkeypatch.setattr(trading_facade.krungsri_sbl_driver, "return_sbl_shares", fake_return_shares)
    monkeypatch.setattr(trading_facade.krungsri_sbl_driver, "invalidate_cache", lambda: None)
    monkeypatch.setattr(trading_facade, "clear_cdp_portfolio_cache", lambda: None)

    res_fifo = await trading_facade.execute_sbl_action(
        action="return",
        symbol="AAV",
        volume=3500,
        remark="close pair",
    )
    assert scrape_flags == [True]
    assert calls == [
        ("B495090", 2000, "AAV", "close pair"),
        ("B495095", 1500, "AAV", "close pair"),
    ]
    assert res_fifo["ok"] is True
    assert res_fifo["volume"] == 3500
    assert res_fifo["lot_id"] == "B495090"
    assert len(res_fifo["lots_returned"]) == 2
    assert res_fifo["dry_run"] is True

    calls.clear()
    res_explicit = await trading_facade.execute_sbl_action(
        action="return",
        symbol="AAV",
        volume=1000,
        lot_id="B495095",
    )
    assert calls == [("B495095", 1000, "AAV", "")]
    assert res_explicit["lot_id"] == "B495095"


@pytest.mark.asyncio
async def test_p4_sbl_return_propagates_502_unverified_with_trace_and_proof(monkeypatch):
    from unittest.mock import AsyncMock
    from app.services.broker_cdp import trading_facade

    monkeypatch.setattr(
        trading_facade.krungsri_sbl_driver,
        "scrape_sbl_lots",
        AsyncMock(
            return_value=[
                {
                    "symbol": "AAV",
                    "lot_id": "B495090",
                    "broker_tx_id": "B495090",
                    "borrow_date": "09/11/2026",
                    "remaining_qty": 94900,
                    "status": "ACTIVE",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        trading_facade.krungsri_sbl_driver,
        "return_sbl_shares",
        AsyncMock(
            return_value={
                "success": False,
                "status": "SBL_RETURN_UNVERIFIED",
                "error": "still open on MainSBL.aspx",
                "broker_message": "RadAlert test",
                "proof": {"gone": False},
                "trace": [{"step": "save"}],
            }
        ),
    )

    with pytest.raises(trading_facade.FacadeHttpError) as exc_info:
        await trading_facade.execute_sbl_action(action="return", symbol="AAV", amount_mode="all")

    assert exc_info.value.status_code == 502
    assert exc_info.value.code == "SBL_RETURN_UNVERIFIED"
    assert exc_info.value.extra["broker_message"] == "RadAlert test"
    assert exc_info.value.extra["proof"] == {"gone": False}


@pytest.mark.asyncio
async def test_facade_line_push_parity_all_executions_success_and_failure(monkeypatch):
    """Verify Q2: SBL, Cancel, Nego, Trade all best-effort push LINE cards on both success and failure."""
    from unittest.mock import AsyncMock

    pushed_cards = []

    async def _fake_push(card):
        pushed_cards.append(card)
        return True

    monkeypatch.setattr(trading_facade, "best_effort_push_card", _fake_push)
    monkeypatch.setattr(trading_facade.sbl_intelligence_service, "check_xd_risk", AsyncMock(return_value={"xd_risk": False}))
    monkeypatch.setattr(
        trading_facade.krungsri_sbl_driver,
        "execute_sbl_borrow",
        AsyncMock(return_value={"success": True, "status": "SBL_BORROW_DRY_RUN"}),
    )

    # 1) SBL Borrow success + single combined card on borrow+auto_short
    monkeypatch.setattr(
        trading_facade,
        "compute_nego_quote",
        AsyncMock(return_value={"last": 10.0, "bid_1": 9.95, "offer_1": 10.0, "lure_plan": {"price": 10.1}}),
    )
    res_borrow = await trading_facade.execute_sbl_action(
        action="borrow", symbol="PTT", amount_mode="volume", volume=100, auto_short=True, short_price=10.1
    )
    assert res_borrow["line_pushed"] is True
    assert len(pushed_cards) == 1
    assert pushed_cards[-1]["altText"].startswith("🧪 [DRY-RUN] ขอยืมหุ้น SBL สำเร็จ PTT")

    # 2) SBL Return failure pushes failed card before raising
    monkeypatch.setattr(
        trading_facade.krungsri_sbl_driver,
        "scrape_sbl_lots",
        AsyncMock(return_value=[{"symbol": "AAV", "lot_id": "B495090", "broker_tx_id": "B495090", "remaining_qty": 1000, "status": "ACTIVE"}]),
    )
    monkeypatch.setattr(
        trading_facade.krungsri_sbl_driver,
        "return_sbl_shares",
        AsyncMock(return_value={"success": False, "status": "NOT_RETURNABLE", "error": "มากกว่าจำนวนที่สามารถคืนได้"}),
    )
    pushed_cards.clear()
    with pytest.raises(trading_facade.FacadeHttpError):
        await trading_facade.execute_sbl_action(action="return", symbol="AAV", amount_mode="all")
    assert len(pushed_cards) == 1
    assert "❌" in pushed_cards[0]["altText"]

    # 3) Order Cancel success & failure both push LINE cards
    monkeypatch.setattr(
        trading_facade,
        "scrape_orders_from_cdp",
        AsyncMock(return_value={"orders": [{"broker_order_no": "9001", "symbol": "PTT", "side": "BUY", "volume": 100, "balance": 100, "status": "O"}]}),
    )
    monkeypatch.setattr(trading_facade, "_broker_cancel", AsyncMock(return_value={"success": True}))
    pushed_cards.clear()
    res_cancel = await trading_facade.execute_order_cancel(broker="kss", order_no="9001", symbol="PTT")
    assert res_cancel["line_pushed"] is True
    assert len(pushed_cards) == 1 and "ยกเลิก" in pushed_cards[0]["altText"]

    monkeypatch.setattr(trading_facade, "_broker_cancel", AsyncMock(return_value={"success": False, "error": "cancel rejected"}))
    pushed_cards.clear()
    with pytest.raises(trading_facade.FacadeHttpError):
        await trading_facade.execute_order_cancel(broker="kss", order_no="9001", symbol="PTT")
    assert len(pushed_cards) == 1 and "❌" in pushed_cards[0]["altText"]

    # 4) Nego Amend success & failure both push LINE cards
    monkeypatch.setattr(
        trading_facade,
        "_broker_quote",
        AsyncMock(return_value={"available": True, "bid_1": 34.75, "offer_1": 35.0, "last": 35.0, "prior_close": 34.5}),
    )
    monkeypatch.setattr(trading_facade, "_broker_modify", AsyncMock(return_value={"success": True, "status": "MODIFIED"}))
    pushed_cards.clear()
    res_nego = await trading_facade.execute_nego_amend(
        broker="kss", order_no="9001", symbol="PTT", side="BUY", volume=100, old_price=34.5, mode="match"
    )
    assert res_nego["line_pushed"] is True
    assert len(pushed_cards) == 1 and "ต่อราคา" in pushed_cards[0]["altText"]

    monkeypatch.setattr(trading_facade, "_broker_modify", AsyncMock(return_value={"success": False, "error": "amend rejected"}))
    pushed_cards.clear()
    with pytest.raises(trading_facade.FacadeHttpError):
        await trading_facade.execute_nego_amend(
            broker="kss", order_no="9001", symbol="PTT", side="BUY", volume=100, old_price=34.5, mode="match"
        )
    assert len(pushed_cards) == 1 and "ต่อราคาไม่สำเร็จ" in pushed_cards[0]["altText"]

    # 5) Trade execution failure also pushes failed receipt card
    monkeypatch.setattr(
        trading_facade,
        "preview_trade_order",
        AsyncMock(return_value={
            "ok": True, "cmd": "buy", "side_letter": "B", "symbol": "PTT",
            "resolved_volume": 100, "resolved_price": 35.0, "total_thb": 3500.0,
            "broker": "lib", "account": "cash", "account_no": "9000001",
        }),
    )
    monkeypatch.setattr(
        trading_facade,
        "execute_confirmed_order",
        AsyncMock(return_value={"success": False, "error": "Insufficient cash"}),
    )
    pushed_cards.clear()
    with pytest.raises(trading_facade.FacadeHttpError):
        await trading_facade.execute_trade_with_idempotency(
            idempotency_key="fail-push-1", side="buy", symbol="PTT", broker="lib", account="cash", amount_mode="volume", volume=100, price=35.0
        )
    assert len(pushed_cards) == 1 and "❌" in pushed_cards[0]["altText"]


@pytest.mark.asyncio
async def test_fetch_portfolio_overview_marks_borrowed_position_type(monkeypatch):
    """Borrowed SBL lots split into brokers['kss']['borrowed'] must explicitly carry position_type='BORROWED'."""
    from unittest.mock import AsyncMock

    async def _fake_scrape(broker):
        if broker == "kss":
            return {
                "account_no": "1000001",
                "account_type": "CREDIT_BALANCE",
                "cash_balance": 200000.0,
                "line_available": 200000.0,
                "excess_equity": 200000.0,
                "holdings": [
                    {
                        "symbol": "LH",
                        "position_type": "SHORT",
                        "volume": 21300,
                        "available_volume": 21300,
                        "avg_cost": 3.51,
                        "current_price": 3.48,
                        "unrealized_pnl": 726.05,
                    },
                    {
                        "symbol": "AAV",
                        "position_type": "BORROWED",
                        "volume": 94900,
                        "available_volume": 94900,
                        "borrow_qty": 94900,
                        "avg_cost": 0.79,
                        "current_price": 0.79,
                        "unrealized_pnl": None,
                    },
                ],
            }
        return {
            "account_no": "9000001",
            "account_type": "CASH_BALANCE",
            "cash_balance": 100000.0,
            "line_available": 100000.0,
            "holdings": [],
        }

    monkeypatch.setattr(trading_facade, "scrape_portfolio_from_cdp", _fake_scrape)
    monkeypatch.setattr(
        trading_facade.krungsri_sbl_driver,
        "scrape_sbl_lots",
        AsyncMock(return_value=[{"symbol": "AAV", "lot_id": "B495090", "broker_tx_id": "B495090", "remaining_qty": 94900, "status": "ACTIVE"}]),
    )

    data = await trading_facade.fetch_portfolio_overview("kss")
    kss = data["brokers"]["kss"]
    assert [r["symbol"] for r in kss["rows"]] == ["LH"]
    assert [b["symbol"] for b in kss["borrowed"]] == ["AAV"]
    assert kss["borrowed"][0]["position_type"] == "BORROWED"
    assert kss["borrowed"][0]["returnable_qty"] == 94900
    assert kss["borrowed"][0]["covered_from_short"] is False


@pytest.mark.asyncio
async def test_fetch_portfolio_overview_reconciles_covered_short_and_fresh_borrow(monkeypatch):
    """Covered ORI(S) vol=0 must hide from active rows and tag ORI(B) covered_from_short=True, while fresh TPIPP(B) stays False."""
    from unittest.mock import AsyncMock

    async def _fake_scrape(broker):
        return {
            "account_no": "1000001",
            "account_type": "CREDIT_BALANCE",
            "cash_balance": 300000.0,
            "line_available": 300000.0,
            "excess_equity": 300000.0,
            "holdings": [
                {"symbol": "JAS", "position_type": "SHORT", "volume": 75700, "available_volume": 75700, "avg_cost": 0.60, "current_price": 0.55, "unrealized_pnl": 3785.0},
                {"symbol": "ORI", "position_type": "SHORT", "volume": 0, "available_volume": 0, "avg_cost": 1.71, "current_price": 1.46, "unrealized_pnl": 0.0},
                {"symbol": "ORI", "position_type": "BORROWED", "volume": 43800, "available_volume": 43800, "borrow_qty": 43800, "avg_cost": 0.0, "current_price": 1.46, "unrealized_pnl": None},
                {"symbol": "TPIPP", "position_type": "BORROWED", "volume": 48700, "available_volume": 48700, "borrow_qty": 48700, "avg_cost": 0.0, "current_price": 1.58, "unrealized_pnl": None},
            ],
        }

    monkeypatch.setattr(trading_facade, "scrape_portfolio_from_cdp", _fake_scrape)
    monkeypatch.setattr(
        trading_facade.krungsri_sbl_driver,
        "scrape_sbl_lots",
        AsyncMock(return_value=[
            {"symbol": "JAS", "lot_id": "B495101", "broker_tx_id": "B495101", "remaining_qty": 75700, "status": "ACTIVE"},
            {"symbol": "ORI", "lot_id": "B495103", "broker_tx_id": "B495103", "remaining_qty": 43800, "status": "ACTIVE"},
            {"symbol": "TPIPP", "lot_id": "B495104", "broker_tx_id": "B495104", "remaining_qty": 48700, "status": "ACTIVE"},
        ]),
    )

    data = await trading_facade.fetch_portfolio_overview("kss")
    kss = data["brokers"]["kss"]
    assert [r["symbol"] for r in kss["rows"]] == ["JAS"]
    by_b = {b["symbol"]: b for b in kss["borrowed"]}
    assert set(by_b) == {"ORI", "TPIPP"}
    assert by_b["ORI"]["covered_from_short"] is True
    assert by_b["ORI"]["returnable_qty"] == 43800
    assert by_b["TPIPP"]["covered_from_short"] is False
    assert by_b["TPIPP"]["returnable_qty"] == 48700


@pytest.mark.asyncio
async def test_p3_t05_nego_execute_notify_line_false(monkeypatch):
    from unittest.mock import AsyncMock

    pushed = []
    monkeypatch.setattr(trading_facade, "best_effort_push_card", AsyncMock(side_effect=lambda c: pushed.append(c) or True))
    monkeypatch.setattr(
        trading_facade,
        "_broker_quote",
        AsyncMock(return_value={"available": True, "bid_1": 34.75, "offer_1": 35.0, "last": 35.0, "prior_close": 34.5}),
    )
    monkeypatch.setattr(trading_facade, "_broker_modify", AsyncMock(return_value={"success": True, "status": "MODIFIED"}))

    res = await trading_facade.execute_nego_amend(
        broker="kss",
        order_no="9001",
        symbol="PTT",
        side="BUY",
        volume=100,
        old_price=34.5,
        mode="match",
        notify_line=False,
    )
    assert res["ok"] is True
    assert res["line_pushed"] is False
    assert pushed == []


@pytest.mark.asyncio
async def test_p3_t06_nego_schedule_endpoints_and_line_schedule_cmd(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from app.services.line_cli import parser as cli_parser
    from app.services.nego_scheduler import NegoScheduler

    sched = NegoScheduler(state_path=str(tmp_path / "sched.json"))
    monkeypatch.setattr(miniapp_ep, "nego_scheduler", sched)
    monkeypatch.setattr("app.services.nego_scheduler.nego_scheduler", sched)
    monkeypatch.setattr(trading_facade, "fetch_orders_overview", AsyncMock(return_value={"brokers": {"kss": {"available": True}, "lib": {"available": True}}, "orders": []}))

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r_get = await client.get("/api/v1/miniapp/nego/schedule")
        assert r_get.status_code == 200
        data = r_get.json()
        assert len(data["windows"]) == 5
        assert "w6_post_close" in {w["id"] for w in data["windows"]}
        assert "w1_open" not in {w["id"] for w in data["windows"]}
        assert data["master_enabled"] is True

        # Lunch cannot be enabled
        r_lunch = await client.post("/api/v1/miniapp/nego/schedule/toggle", json={"window_id": "w3_lunch", "enabled": True})
        assert r_lunch.status_code == 400

        # Toggle w4_afternoon OFF then ON & reroll w2_morning
        r_tog = await client.post("/api/v1/miniapp/nego/schedule/toggle", json={"window_id": "w4_afternoon", "enabled": False})
        assert r_tog.status_code == 200
        assert {w["id"]: w for w in r_tog.json()["windows"]}["w4_afternoon"]["enabled"] is False

        r_reroll = await client.post("/api/v1/miniapp/nego/schedule/reroll", json={"window_id": "w2_morning"})
        assert r_reroll.status_code == 200

        r_run = await client.post("/api/v1/miniapp/nego/schedule/run-now", json={"window_id": "w2_morning"})
        assert r_run.status_code == 200
        w2 = {w["id"]: w for w in r_run.json()["windows"]}["w2_morning"]
        assert w2["run"]["status"] == "NO_ORDERS"

    # LINE /schedule uses reply_token (0 push quota)
    parsed = cli_parser.parse("/schedule")
    assert parsed is not None and parsed.cmd == "schedule"

    replies = []
    pushes = []

    class _FakeLineSvc:
        async def _send_reply(self, reply_token, msg):
            replies.append((reply_token, msg))
            return True

        async def push_message(self, msg, to_user_id=None):
            pushes.append(msg)
            return True

    monkeypatch.setattr("app.services.conversation_state.conversation_state.is_authorized", lambda *a, **kw: True)
    res = await handlers.handle(
        parsed,
        raw_event={"replyToken": "rt-sched-1", "source": {"type": "group", "groupId": "G1", "userId": "U1"}},
        service=_FakeLineSvc(),
    )
    assert res["status"] == "CLI_SCHEDULE"
    assert len(replies) == 1 and replies[0][0] == "rt-sched-1"
    assert "Auto-Nego Schedule" in replies[0][1]["text"]
    assert pushes == []


@pytest.mark.asyncio
async def test_runbook_exits_miniapp_endpoints(monkeypatch, tmp_path):
    from datetime import datetime
    import json
    import random
    from zoneinfo import ZoneInfo
    from app.services import runbook_exit_service as rb_mod
    from app.services.nego_scheduler import NegoScheduler
    from app.services.runbook_exit_service import RunbookExitService

    bkk = ZoneInfo("Asia/Bangkok")
    ledger_path = tmp_path / "positions_ledger.json"
    ledger_path.write_text(
        json.dumps(
            {
                "as_of": "2026-11-12",
                "statement_through": "2026-11-12",
                "positions": [
                    {
                        "segment": "managed",
                        "broker": "KSS",
                        "symbol": "BAM",
                        "direction": "SHORT",
                        "action": "COVER",
                        "shares": 12000,
                        "exit_reason": "CS_LOSER_FLIP_CUT",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    clock = {"dt": datetime(2026, 11, 12, 9, 15, tzinfo=bkk)}
    holidays_path = tmp_path / "holidays_index.json"
    holidays_path.write_text(json.dumps({"dates": {}}), encoding="utf-8")
    rb_svc = RunbookExitService(
        ledger_path=str(ledger_path),
        state_path=str(tmp_path / "rb_state.json"),
        holidays_path=str(holidays_path),
        clock_fn=lambda: clock["dt"],
        rng=random.Random(7),
    )
    sched = NegoScheduler(state_path=str(tmp_path / "sched.json"))

    monkeypatch.setattr(miniapp_ep, "runbook_exit_service", rb_svc)
    monkeypatch.setattr(miniapp_ep, "nego_scheduler", sched)
    monkeypatch.setattr(rb_mod, "_is_dry_mode", lambda: True)

    async def fake_probe(needed):
        return list(needed), {}, []

    async def fake_port(broker: str = "all"):
        return {
            "brokers": {
                "kss": {"available": True, "rows": [{"symbol": "BAM", "position_type": "SHORT", "volume": 12000}]},
            }
        }

    async def fake_orders(broker: str = "all"):
        return {"brokers": {"kss": {"available": True}}, "orders": []}

    async def fake_quote(broker: str, symbol: str, side=None, old_price=None):
        return {"available": True, "bid_1": 7.75, "offer_1": 7.80, "last": 7.80, "lure_plan": {"ok": True, "price": 7.80}}

    async def fake_otp():
        return SimpleNamespace(ready=True, channel="miniapp", reason=None)

    async def fake_exec(**kwargs):
        return {
            "dry_run": True,
            "order": {"volume": kwargs["volume"], "price": 7.80},
            "execution": {"broker_order_no": "DRY-BAM-99"},
        }

    monkeypatch.setattr(rb_mod, "probe_ready_brokers", fake_probe)
    monkeypatch.setattr(rb_mod.otp_relay_readiness, "check", fake_otp)
    monkeypatch.setattr(rb_mod.pin_guard, "check", lambda b: {"locked": False})
    monkeypatch.setattr(trading_facade, "fetch_portfolio_overview", fake_port)
    monkeypatch.setattr(trading_facade, "fetch_orders_overview", fake_orders)
    monkeypatch.setattr(trading_facade, "compute_nego_quote", fake_quote)
    monkeypatch.setattr(trading_facade, "execute_trade_with_idempotency", fake_exec)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. Preview includes readiness + quote + ledger items + Auto-daily slot
        r_prev = await client.get("/api/v1/miniapp/runbook/exits")
        assert r_prev.status_code == 200
        prev = r_prev.json()
        assert prev["is_today"] is True
        assert prev["actionable_count"] == 1
        assert prev["readiness"]["all_ready"] is True
        assert prev["items"][0]["symbol"] == "BAM"
        assert prev["items"][0]["lure_price"] == pytest.approx(7.72)
        assert prev["schedule"]["auto_enabled"] is True
        assert prev["schedule"]["queue"]["status"] == "AUTO_PENDING"

        # 2. Schedule Toggle OFF -> ON
        r_tog_off = await client.post("/api/v1/miniapp/runbook/exits/schedule/toggle", json={"enabled": False})
        assert r_tog_off.status_code == 200
        assert r_tog_off.json()["runbook_exit"]["auto_enabled"] is False
        assert r_tog_off.json()["runbook_exit"]["queue"] is None

        r_tog_on = await client.post("/api/v1/miniapp/runbook/exits/schedule/toggle", json={"enabled": True})
        assert r_tog_on.status_code == 200
        assert r_tog_on.json()["runbook_exit"]["queue"]["status"] == "AUTO_PENDING"

        # 3. Trade Submit pre-market -> PENDING manual_trade supersedes AUTO_PENDING
        r_sub = await client.post("/api/v1/miniapp/runbook/exits/submit", json={"allow_stale": False})
        assert r_sub.status_code == 200
        sub = r_sub.json()
        assert sub["mode"] == "SCHEDULED"

        r_sched = await client.get("/api/v1/miniapp/nego/schedule")
        assert r_sched.status_code == 200
        s_data = r_sched.json()
        assert len(s_data["windows"]) == 5
        assert s_data["runbook_exit"]["queue"]["status"] == "PENDING"

        # 4. Reroll
        r_reroll = await client.post("/api/v1/miniapp/runbook/exits/schedule/reroll")
        assert r_reroll.status_code == 200
        assert r_reroll.json()["runbook_exit"]["queue"]["status"] == "PENDING"

        # 5. Run-now
        r_now = await client.post("/api/v1/miniapp/runbook/exits/schedule/run-now")
        assert r_now.status_code == 200
        q_done = r_now.json()["runbook_exit"]["queue"]
        assert q_done["status"] == "DONE"
        assert q_done["items"][0]["outcome"] == "DRY_RUN"

        # 6. Re-enqueue + cancel
        r_sub2 = await client.post("/api/v1/miniapp/runbook/exits/submit", json={})
        assert r_sub2.status_code == 200
        r_cancel = await client.post("/api/v1/miniapp/runbook/exits/schedule/cancel")
        assert r_cancel.status_code == 200
        assert r_cancel.json()["runbook_exit"]["queue"]["status"] == "CANCELLED"


@pytest.mark.asyncio
async def test_p5_inbox_clear_dedup_and_trade_hub_summary_ssot(monkeypatch, tmp_path):
    """Verify Card 1 (/inbox/clear + dedup + JSONL archive) and Card 2 (hub-summary budget SSOT & candidate_sides)."""
    import json
    import random
    from datetime import datetime
    from pathlib import Path
    from zoneinfo import ZoneInfo

    from app.services.miniapp_inbox import MiniAppInboxService
    from app.services.runbook_exit_service import RunbookExitService
    from app.services.suggest_entry_service import SuggestEntryService

    bkk = ZoneInfo("Asia/Bangkok")
    inbox_state = tmp_path / "miniapp_inbox.json"
    archive_dir = tmp_path / "logs"
    monkeypatch.setattr(miniapp_ep.settings, "MINIAPP_INBOX_STATE_PATH", str(inbox_state))
    monkeypatch.setattr(miniapp_ep.settings, "MINIAPP_INBOX_ARCHIVE_DIR", str(archive_dir))

    inbox_svc = MiniAppInboxService()
    monkeypatch.setattr(miniapp_ep, "miniapp_inbox", inbox_svc)

    await inbox_svc.publish(
        category="order",
        title="Filled ORI",
        body="KSS COVER Filled",
    )
    await inbox_svc.publish(
        category="system",
        title="🚀 InvestmentHomelab Gateway ออนไลน์",
        body="Gateway boot #1",
        meta={"dedup_key": "gateway_online", "is_heartbeat": True},
    )
    await inbox_svc.publish(
        category="system",
        title="🚀 InvestmentHomelab Gateway ออนไลน์",
        body="Gateway boot #2",
        meta={"dedup_key": "gateway_online", "is_heartbeat": True},
    )

    pos_ledger = tmp_path / "positions_ledger.json"
    pos_ledger.write_text(
        json.dumps({"as_of": "2026-11-17", "statement_through": "2026-11-17", "positions": []}),
        encoding="utf-8",
    )
    holidays_path = tmp_path / "holidays_index.json"
    holidays_path.write_text(json.dumps({"dates": {}}), encoding="utf-8")

    cur_dt = datetime(2026, 11, 17, 10, 15, tzinfo=bkk)
    rb_svc = RunbookExitService(
        ledger_path=str(pos_ledger),
        state_path=str(tmp_path / "rb_state.json"),
        holidays_path=str(holidays_path),
        clock_fn=lambda: cur_dt,
        rng=random.Random(1),
    )
    se_svc = SuggestEntryService(
        state_path=str(tmp_path / "suggest_entry_state.json"),
        scanner_dir=str(tmp_path / "scanner"),
        positions_ledger_path=str(pos_ledger),
        entry_ledger_path=str(tmp_path / "entry_ledger.json"),
        clock_fn=lambda: cur_dt,
    )
    se_svc.save_config(50000.0)
    monkeypatch.setattr(
        se_svc,
        "_build_ledger",
        lambda _cur: {
            "ledger_available": True,
            "quota": {"effective_long": 1, "effective_short": 1, "effective_total": 2, "model_long": 1, "model_short": 1},
            "pools": {
                "long_top8": [{"ticker": "MINT", "final_score": 0.91}],
                "short_top8": [{"ticker": "ORI", "final_score": 0.88}],
            },
        },
    )
    monkeypatch.setattr(miniapp_ep, "runbook_exit_service", rb_svc)
    monkeypatch.setattr(miniapp_ep, "suggest_entry_service", se_svc)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        r_inbox = await client.get("/api/v1/miniapp/inbox")
        assert r_inbox.status_code == 200
        items = r_inbox.json()["items"]
        assert len(items) == 2
        gw_items = [it for it in items if (it.get("meta") or {}).get("dedup_key") == "gateway_online"]
        assert len(gw_items) == 1
        assert gw_items[0]["meta"]["repeat_count"] == 2

        r_clear = await client.post("/api/v1/miniapp/inbox/clear", json={"archive": True})
        assert r_clear.status_code == 200
        clear_data = r_clear.json()
        assert clear_data["ok"] is True
        assert clear_data["cleared"] == 2
        assert clear_data["unread_count"] == 0
        arch_path = Path(clear_data["archived_file"])
        assert arch_path.is_file()
        assert arch_path.parent == archive_dir
        archived_rows = [json.loads(line) for line in arch_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(archived_rows) == 2
        assert archived_rows[0]["title"] == "Filled ORI"

        r_inbox_after = await client.get("/api/v1/miniapp/inbox")
        assert r_inbox_after.status_code == 200
        assert r_inbox_after.json()["items"] == []
        assert r_inbox_after.json()["unread_count"] == 0

        r_hub = await client.get("/api/v1/miniapp/trade/hub-summary")
        assert r_hub.status_code == 200
        hub = r_hub.json()
        assert hub["suggest_entry"]["budget_per_slot_thb"] == 50000.0
        assert hub["suggest_entry"]["candidate_sides"] == {"MINT": "LONG", "ORI": "SHORT"}

        r_cfg = await client.post("/api/v1/miniapp/suggest-entry/config", json={"budget_per_slot_thb": 75000.0})
        assert r_cfg.status_code == 200
        assert r_cfg.json()["budget_per_slot_thb"] == 75000.0
        r_hub2 = await client.get("/api/v1/miniapp/trade/hub-summary")
        assert r_hub2.json()["suggest_entry"]["budget_per_slot_thb"] == 75000.0


@pytest.mark.asyncio
async def test_portfolio_net_equity_excludes_margin_line_and_borrowed_and_feeds_recommend_size(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from app.services import suggest_entry_service as se_mod
    from app.services.broker_cdp import trading_facade
    from app.services.suggest_entry_service import SuggestEntryService

    monkeypatch.setattr(trading_facade.settings, "STREAMING_ACCOUNT_NO_MARGIN", "1000001")
    monkeypatch.setattr(trading_facade.settings, "STREAMING_ACCOUNT_NO_CASH", "1000002")
    se_svc = SuggestEntryService(state_path=str(tmp_path / "se_state.json"))
    monkeypatch.setattr(se_mod, "suggest_entry_service", se_svc)
    monkeypatch.setattr(
        se_svc,
        "_build_ledger",
        lambda _cur: {
            "ledger_available": True,
            "quota": {"cap_unique_tickers": 7, "effective_long": 2, "effective_short": 1, "effective_total": 3, "model_long": 2, "model_short": 1},
            "pools": {"long_top8": [], "short_top8": []},
        },
    )

    async def fake_scrape(broker: str):
        if broker == "kss":
            return {
                "account_no": "1000001",
                "account_type": "CREDIT_BALANCE",
                "cash_balance": 250000.0,
                "line_available": 950000.0,  # margin buying power must NOT inflate net equity
                "excess_equity": 310000.0,
                "holdings": [
                    {"symbol": "AOT", "position_type": "LONG", "volume": 1000, "current_price": 100.0, "market_value": 100000.0},
                    {"symbol": "ORI(S)", "position_type": "SHORT", "volume": 10000, "current_price": 4.0, "market_value": 40000.0},
                    {"symbol": "AAV(B)", "position_type": "BORROWED", "volume": 50000, "current_price": 4.0, "market_value": 200000.0},
                ],
            }
        return {
            "account_no": "9000001",
            "account_type": "CASH_BALANCE",
            "cash_balance": 100000.0,
            "line_available": 100000.0,
            "holdings": [
                {"symbol": "PTT", "position_type": "LONG", "volume": 5000, "current_price": 30.0, "market_value": 150000.0},
            ],
        }

    monkeypatch.setattr(trading_facade, "scrape_portfolio_from_cdp", fake_scrape)
    monkeypatch.setattr(trading_facade.krungsri_sbl_driver, "scrape_sbl_lots", AsyncMock(return_value=[]))
    monkeypatch.setattr(trading_facade, "get_active_kss_account", lambda: "1000001")
    monkeypatch.setattr(trading_facade, "get_active_kss_account_type", lambda: "CREDIT_BALANCE")
    monkeypatch.setattr(
        trading_facade.account_routing,
        "get_account_routing",
        lambda: {"short_broker": "kss", "short_account": "margin", "long_broker": "lib", "long_account": "cash"},
    )

    overview = await trading_facade.fetch_portfolio_overview(broker="all")
    # KSS Margin net_equity = 250000 + 100000 - 40000 = 310000 (BORROWED 200k & Line 950k excluded)
    assert overview["brokers"]["kss"]["net_equity"] == 310000.0
    # LIB Cash net_equity = 100000 + 150000 = 250000
    assert overview["brokers"]["lib"]["net_equity"] == 250000.0
    assert overview["summary"]["total_net_equity"] == 560000.0
    assert overview["summary"]["total_line_available"] == 1050000.0
    assert len(overview["summary"]["selected_accounts"]) == 2

    hub = se_svc.get_hub_summary()
    assert hub["suggest_entry"]["budget_mode"] == "recommended"
    assert hub["suggest_entry"]["budget_per_slot_thb"] == 80000.0
    sref = hub["suggest_entry"]["sizing_reference"]
    assert sref["net_equity_thb"] == 560000.0
    assert sref["recommended_slots"] == 7
    assert sref["recommended_budget_thb"] == 80000

    # Switching manual override and back to recommended
    manual_res = se_svc.save_config(100000.0)
    assert manual_res["budget_mode"] == "manual"
    assert se_svc.get_hub_summary()["suggest_entry"]["budget_per_slot_thb"] == 100000.0
    rec_res = se_svc.save_config(use_recommended=True)
    assert rec_res["budget_mode"] == "recommended"
    assert rec_res["budget_per_slot_thb"] == 80000.0

    # Selected-ports-only: routing KSS Margin only excludes LIB Cash completely
    monkeypatch.setattr(
        trading_facade.account_routing,
        "get_account_routing",
        lambda: {"short_broker": "kss", "short_account": "margin", "long_broker": "kss", "long_account": "margin"},
    )
    overview_kss_only = await trading_facade.fetch_portfolio_overview(broker="all")
    assert "lib" not in overview_kss_only["brokers"]
    assert overview_kss_only["summary"]["total_net_equity"] == 310000.0
    assert len(overview_kss_only["summary"]["selected_accounts"]) == 1


@pytest.mark.asyncio
async def test_scrape_kss_reads_account_info_subtab_when_header_omits_cash_balance(monkeypatch):
    from app.services.broker_cdp import sync_service

    class _FakeKssCreditPage:
        def __init__(self):
            self.clicked_info = False
            self.clicked_port = False

        async def evaluate(self, js, *args, **kwargs):
            if js is sync_service._KSS_SCRAPE_JS:
                return {
                    "account_no": "1000001",
                    "balances": {
                        "account_type": "Credit Balance",
                        "credit_limit": 1200000.0,
                        "line_available": 702655.16,
                        "excess_equity": 351327.58,
                        "cash_balance": None,
                        "margin_required": None,
                        "total_equity": None,
                    },
                    "holdings": [
                        {
                            "symbol": "TPIPP (S)",
                            "position_type": "SHORT",
                            "volume": 48700,
                            "available_volume": 48700,
                            "avg_cost": 1.53741,
                            "current_price": 1.5,
                            "market_value": 73050.0,
                            "unrealized_pnl": 1822.01,
                            "unrealized_pnl_pct": 2.43,
                        }
                    ],
                }
            if "Account Info" in str(js):
                self.clicked_info = True
                return True
            if js is sync_service._KSS_ACCOUNT_INFO_JS:
                return {
                    "cash_balance": 482817.58,
                    "margin_required": 58440.0,
                    "total_equity": 409767.58,
                }
            if "'Portfolio'" in str(js):
                self.clicked_port = True
                return True
            return None

        async def wait_for_timeout(self, _ms):
            return None

    page = _FakeKssCreditPage()
    monkeypatch.setattr(sync_service, "record_active_kss_account", lambda *_a, **_kw: None)
    snap = await sync_service._scrape_kss(page)
    assert page.clicked_info is True
    assert page.clicked_port is True
    assert snap["cash_balance"] == 482817.58
    assert snap["total_equity"] == 409767.58
    assert snap["margin_required"] == 58440.0


@pytest.mark.asyncio
async def test_p6_schedule_includes_eod_pipeline_and_net_equity_history(monkeypatch, tmp_path):
    import json
    from app.core.config import settings
    from app.api.v1.endpoints import miniapp as miniapp_ep
    from app.services.nego_scheduler import NegoScheduler
    from app.services.eod_summary_push import EodSummaryPushService
    from app.services.net_equity_history import net_equity_history

    monkeypatch.setattr(settings, "LINE_EOD_STATE_PATH", str(tmp_path / "eod_state.json"))
    monkeypatch.setattr(settings, "NET_EQUITY_HISTORY_JSON_PATH", str(tmp_path / "eq.json"))
    monkeypatch.setattr(settings, "NET_EQUITY_HISTORY_CSV_PATH", str(tmp_path / "eq.csv"))
    monkeypatch.setattr(settings, "NET_EQUITY_MIRROR_CSV_PATH", "")
    pipe_file = tmp_path / "run_daily_latest.json"
    monkeypatch.setattr(settings, "DAILY_PIPELINE_STATUS_PATH", str(pipe_file))

    sched = NegoScheduler(state_path=tmp_path / "nego_sched.json")
    monkeypatch.setattr(miniapp_ep, "nego_scheduler", sched)
    eod_svc = EodSummaryPushService()
    monkeypatch.setattr(miniapp_ep, "eod_summary_service", eod_svc)
    monkeypatch.setattr(miniapp_ep, "require_liff_user", lambda: {"user_id": "U_TEST"})
    monkeypatch.setattr(miniapp_ep.runbook_exit_service, "get_schedule_snapshot", lambda: {"auto_enabled": True, "queue": None})
    app.dependency_overrides[miniapp_ep.require_liff_user] = lambda: {"user_id": "U_TEST"}

    today_str = sched.get_schedule_snapshot()["daily_date"]
    rec_eq = net_equity_history.record_daily_net_equity(
        date_str=today_str,
        overview={
            "accounts": [],
            "summary": {
                "total_net_equity": 634428.67,
                "selected_accounts": [{"key": "kss_margin"}, {"key": "lib_cash"}],
            },
        },
        counts={"matched": 1, "open": 0},
    )
    assert rec_eq["selected_accounts"] == ["kss_margin", "lib_cash"]
    pipe_file.write_text(
        json.dumps(
            {
                "target_date": today_str,
                "started": f"{today_str}T17:30:02+07:00",
                "seconds": 14.2,
                "failed": False,
                "stages": [{"id": "PRE-SBL", "status": "ok", "code": 0, "seconds": 1.1}],
            }
        ),
        encoding="utf-8",
    )

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            r_sched = await client.get("/api/v1/miniapp/nego/schedule")
            assert r_sched.status_code == 200
            body = r_sched.json()
            assert body["eod"]["push_time"] == "17:05"
            assert body["eod"]["today_net_equity"]["total_net_equity"] == 634428.67
            assert body["daily_pipeline"]["status"] == "OK"
            assert body["daily_pipeline"]["stages"][0]["id"] == "PRE-SBL"

            r_tog = await client.post("/api/v1/miniapp/eod/toggle", json={"enabled": False})
            assert r_tog.status_code == 200
            assert r_tog.json()["eod"]["enabled"] is False

            r_hist = await client.get("/api/v1/miniapp/eod/net-equity-history?limit=10")
            assert r_hist.status_code == 200
            hist = r_hist.json()
            assert hist["count"] == 1
            assert hist["entries"][0]["total_net_equity"] == 634428.67
    finally:
        app.dependency_overrides.clear()





