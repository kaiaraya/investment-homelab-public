"""Unit tests for Pure-CDP 16:40 Daily EOD Matched Order LINE Summary Push Service."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
import pytest

from app.core.config import settings
from app.services import eod_summary_push as mod
from app.services.eod_summary_push import EodSummaryPushService
from app.services.miniapp_inbox import miniapp_inbox

_BKK = ZoneInfo("Asia/Bangkok")


def _bkk(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=_BKK)


@pytest.fixture
def isolated_eod(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LINE_EOD_STATE_PATH", str(tmp_path / "eod_state.json"))
    monkeypatch.setattr(settings, "MINIAPP_INBOX_STATE_PATH", str(tmp_path / "inbox.json"))
    monkeypatch.setattr(settings, "NET_EQUITY_HISTORY_JSON_PATH", str(tmp_path / "net_equity_history.json"))
    monkeypatch.setattr(settings, "NET_EQUITY_HISTORY_CSV_PATH", str(tmp_path / "net_equity_history.csv"))
    monkeypatch.setattr(settings, "NET_EQUITY_MIRROR_CSV_PATH", str(tmp_path / "mirror" / "net_equity_history.csv"))
    monkeypatch.setattr(settings, "EOD_SUMMARY_ENABLED", True)
    monkeypatch.setattr(settings, "LINE_EOD_PUSH_ENABLED", True)
    monkeypatch.setattr(settings, "LINE_EOD_PUSH_TIME", "16:40")
    monkeypatch.setattr(settings, "LINE_PUSH_MODE", "eod_only")
    monkeypatch.setattr(settings, "LINE_ADMIN_USER_ID", "U_ADMIN_TEST")
    monkeypatch.setattr(settings, "LINE_GROUP_ID", "C_GROUP_IGNORED")


def _stub_brokers(monkeypatch, *, kss_auth=True, lib_auth=True, orders=None, net_equity=634428.67):
    async def fake_probe(broker, purpose=None):
        ok = kss_auth if broker == "kss" else lib_auth
        return {
            "broker": broker,
            "purpose": purpose,
            "state": "AUTHENTICATED" if ok else "DISCONNECTED",
            "error": None if ok else "Session Down",
        }

    async def fake_overview(broker="all"):
        rows = list(orders or [])
        return {
            "brokers": {
                "kss": {"broker": "kss", "available": kss_auth, "error": None if kss_auth else "down"},
                "lib": {"broker": "lib", "available": lib_auth, "error": None if lib_auth else "down"},
            },
            "orders": rows,
            "counts": {
                "total": len(rows),
                "open": sum(1 for r in rows if r.get("kind") in ("open", "partial")),
                "matched": sum(1 for r in rows if r.get("kind") == "matched"),
            },
            "partial": not (kss_auth and lib_auth),
            "dry_run": False,
        }

    async def fake_portfolio_overview(broker="all"):
        any_ok = kss_auth or lib_auth
        kss_val = round(net_equity * 0.65, 2) if (any_ok and net_equity is not None and kss_auth) else None
        lib_val = round(net_equity - (kss_val or 0.0), 2) if (any_ok and net_equity is not None and lib_auth) else None
        total = round((kss_val or 0.0) + (lib_val or 0.0), 2) if (kss_val is not None or lib_val is not None) else None
        return {
            "accounts": [
                {"key": "kss_margin", "broker": "kss", "available": kss_auth, "selected_in_routing": True, "net_equity": kss_val},
                {"key": "lib_cash", "broker": "lib", "available": lib_auth, "selected_in_routing": True, "net_equity": lib_val},
            ],
            "summary": {
                "total_net_equity": total,
                "total_cash_balance": total,
                "total_long_market_value": 100000.0 if total is not None else None,
                "total_short_liability": 50000.0 if total is not None else None,
                "selected_accounts": ["kss_margin", "lib_cash"],
                "partial": not (kss_auth and lib_auth),
            },
        }

    monkeypatch.setattr(mod.session_probe, "cdp_session_status", fake_probe)
    monkeypatch.setattr(mod.trading_facade, "fetch_orders_overview", fake_overview)
    monkeypatch.setattr(mod.trading_facade, "fetch_portfolio_overview", fake_portfolio_overview)


@pytest.mark.asyncio
async def test_eod_push_matched_orders_and_daily_dedup(isolated_eod, monkeypatch):
    clock = [_bkk(2026, 11, 12, 16, 40)]  # Thursday
    _stub_brokers(
        monkeypatch,
        orders=[
            {"broker": "kss", "order_no": "101", "symbol": "PTT", "side": "BUY", "volume": 500, "matched_volume": 500, "balance": 0, "price": 34.5, "status": "Matched", "kind": "matched"},
            {"broker": "lib", "order_no": "202", "symbol": "KBANK", "side": "SELL", "volume": 200, "matched_volume": 0, "balance": 200, "price": 152.0, "status": "Open", "kind": "open"},
        ],
    )

    from app.services.line_service import line_service

    pushed = []

    async def fake_push(msg, to_user_id=None, **kwargs):
        pushed.append({"msg": msg, "to": to_user_id, "kwargs": kwargs})
        return True

    monkeypatch.setattr(line_service, "push_message", fake_push)

    svc = EodSummaryPushService(clock_fn=lambda: clock[0])
    res1 = await svc.tick_once()
    assert res1 is not None
    assert res1["sent"] is True
    assert res1["status_kind"] == "matched"
    assert res1["counts"] == {"matched": 1, "open": 1}
    assert len(pushed) == 1
    assert pushed[0]["to"] == "U_ADMIN_TEST"
    assert pushed[0]["kwargs"].get("category") == "eod_summary"
    assert "PTT" in str(pushed[0]["msg"])
    assert "MATCHED 1" in str(pushed[0]["msg"])

    # Second tick same day -> deduplicated strictly (0 extra push)
    clock[0] = _bkk(2026, 11, 12, 16, 45)
    assert await svc.tick_once() is None
    assert len(pushed) == 1

    # Also recorded into MiniApp Inbox
    inbox = await miniapp_inbox.list_items()
    assert inbox["items"][0]["category"] == "eod"
    assert inbox["items"][0]["line_pushed"] is True


@pytest.mark.asyncio
async def test_eod_skips_before_time_and_on_weekends(isolated_eod, monkeypatch):
    _stub_brokers(monkeypatch, orders=[])
    # 16:39 weekday -> skip
    svc_early = EodSummaryPushService(clock_fn=lambda: _bkk(2026, 11, 12, 16, 39))
    assert await svc_early.tick_once() is None
    # Saturday 16:40 -> skip
    svc_sat = EodSummaryPushService(clock_fn=lambda: _bkk(2026, 11, 14, 16, 40))
    assert await svc_sat.tick_once() is None


@pytest.mark.asyncio
async def test_eod_open_only_no_orders_and_cdp_unavailable(isolated_eod, monkeypatch):
    from app.services.line_service import line_service

    async def fake_push(msg, to_user_id=None, **kwargs):
        return True

    monkeypatch.setattr(line_service, "push_message", fake_push)
    svc = EodSummaryPushService(clock_fn=lambda: _bkk(2026, 11, 12, 16, 41))

    # Open only (0 matched)
    _stub_brokers(
        monkeypatch,
        orders=[{"broker": "kss", "order_no": "301", "symbol": "AOT", "side": "SELL", "volume": 100, "balance": 100, "price": 62.0, "status": "Open", "kind": "open"}],
    )
    r_open = await svc.send_now(force=True)
    assert r_open["status_kind"] == "open_only"
    assert "OPEN 1" in str(r_open["summary"]["card"])

    # Honest no orders
    _stub_brokers(monkeypatch, orders=[])
    r_none = await svc.send_now(force=True)
    assert r_none["status_kind"] == "no_orders"
    assert "NO ORDERS" in str(r_none["summary"]["card"])

    # Honest CDP unavailable (Anti-Mock: never claims 0 orders when both sessions down)
    _stub_brokers(monkeypatch, kss_auth=False, lib_auth=False, orders=[])
    r_down = await svc.send_now(force=True)
    assert r_down["status_kind"] == "session_unavailable"
    assert "CDP DOWN" in str(r_down["summary"]["card"])


@pytest.mark.asyncio
async def test_eod_1705_auto_record_inbox_only_and_net_equity_upsert(isolated_eod, tmp_path, monkeypatch):
    import csv
    from pathlib import Path
    from app.services.line_service import line_service
    from app.services.net_equity_history import net_equity_history

    mirror_dir = tmp_path / "mirror"
    mirror_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(settings, "LINE_EOD_PUSH_ENABLED", False)
    monkeypatch.setattr(settings, "LINE_EOD_PUSH_TIME", "17:05")
    monkeypatch.setattr(settings, "EOD_SUMMARY_ENABLED", True)

    pushed = []

    async def fake_push(msg, to_user_id=None, **kwargs):
        pushed.append(msg)
        return True

    monkeypatch.setattr(line_service, "push_message", fake_push)
    _stub_brokers(monkeypatch, orders=[], net_equity=634428.67)

    # 17:04 -> before 17:05 target -> skip
    svc_early = EodSummaryPushService(clock_fn=lambda: _bkk(2026, 11, 18, 17, 4))
    assert await svc_early.tick_once() is None

    # 17:05 -> auto-records Inbox + Net Equity CSV/JSON/Mirror, 0 LINE push
    svc = EodSummaryPushService(clock_fn=lambda: _bkk(2026, 11, 18, 17, 5))
    res = await svc.tick_once()
    assert res is not None
    assert res["sent"] is False
    assert res["recorded"] is True
    assert res["skip_reason"] == "line_push_disabled"
    assert res["net_equity"] == 634428.67
    assert pushed == []

    # Second tick same day -> deduplicated
    assert await svc.tick_once() is None

    # Verify CSV + Mirror CSV + JSON + Inbox
    csv_path = Path(settings.NET_EQUITY_HISTORY_CSV_PATH)
    mirror_path = Path(settings.NET_EQUITY_MIRROR_CSV_PATH)
    assert csv_path.exists()
    assert mirror_path.exists()
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-11-18"
    assert float(rows[0]["total_net_equity"]) == 634428.67
    assert list(csv.DictReader(mirror_path.open(encoding="utf-8"))) == rows

    # Re-run manually later with updated value -> upsert keeps 1 row for 2026-11-18
    _stub_brokers(monkeypatch, orders=[], net_equity=640100.50)
    res2 = await svc.send_now(force=True)
    assert res2["net_equity"] == 640100.50
    hist = net_equity_history.get_history(limit=30)
    assert hist["count"] == 1
    assert hist["entries"][0]["total_net_equity"] == 640100.50

    # Later re-run when CDP is down preserves existing valid 2026-11-18 snapshot instead of wiping it with None
    _stub_brokers(monkeypatch, kss_auth=False, lib_auth=False, orders=[])
    await svc.send_now(force=True)
    assert net_equity_history.get_entry_for_date("2026-11-18")["total_net_equity"] == 640100.50

    # Schedule toggle OFF skips tick_once
    svc.set_enabled(False)
    svc_next = EodSummaryPushService(clock_fn=lambda: _bkk(2026, 11, 19, 17, 5))
    assert await svc_next.tick_once() is None
