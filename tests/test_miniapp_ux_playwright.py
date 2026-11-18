"""Headless Chromium Playwright E2E tests for MiniApp UX Lock, Result Modal, Banner & All 5 Tabs."""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest

from app.core.config import settings
from app.miniapp import router as miniapp_router


def _make_mock_miniapp_server():
    counters = {
        "sbl_action_calls": 0,
        "trade_execute_calls": 0,
        "cancel_calls": 0,
        "nego_execute_calls": 0,
        "schedule_toggle_calls": 0,
        "schedule_run_calls": 0,
        "runbook_toggle_calls": 0,
        "runbook_run_calls": 0,
        "suggest_poll_calls": 0,
        "suggest_submit_calls": 0,
        "nego_quote_err_mode": False,
        "sbl_return_delay_s": 0.35,
    }
    schedule_state = {
        "daily_date": "2026-11-12",
        "now_bkk": "10:15:00",
        "is_weekday": True,
        "in_nego_market_window": True,
        "master_enabled": True,
        "running_now": False,
        "dry_run": True,
        "kill_switch": {"is_active": False},
        "line_quota_exhausted": False,
        "next_run": {"window_id": "w2_morning", "label": "ช่วงเช้า (Lure)", "mode": "lure", "scheduled_time": "11:18", "eta_minutes": 63},
        "runbook_exit": {
            "daily_date": "2026-11-12",
            "auto_enabled": True,
            "market_phase": "MARKET_OPEN_AM",
            "is_holiday": False,
            "holiday_name": None,
            "queue": {
                "status": "AUTO_PENDING",
                "window_start": "10:00",
                "window_end": "10:30",
                "scheduled_time": "10:15",
                "dry_run": True,
                "line_status": "รอทำงานอัตโนมัติ (งดส่ง LINE)",
                "counts": {"total": 0, "executed": 0, "adjusted": 0, "skipped": 0, "failed": 0},
                "items": [],
            },
        },
        "windows": [
            {"id": "w2_morning", "start": "10:30", "end": "12:30", "period": "10:30 - 12:30", "mode": "lure", "allowed_sides": ["BUY", "SELL", "SHORT", "COVER"], "tradable": True, "enabled": True, "label": "ช่วงเช้า (Lure)", "description": "ต่อราคาแบบล่อ", "in_window_now": False, "run": {"window_id": "w2_morning", "status": "PENDING", "scheduled_time": "11:18", "counts": {"checked": 0, "amended": 0, "no_change": 0, "skipped": 0, "failed": 0}, "items": [], "line_status": "รอทำงาน"}},
            {"id": "w3_lunch", "start": "12:30", "end": "14:00", "period": "12:30 - 14:00", "mode": "off", "allowed_sides": [], "tradable": False, "enabled": False, "label": "พักเที่ยง SET", "description": "ตลาดปิดพักเที่ยง", "in_window_now": False, "run": {"window_id": "w3_lunch", "status": "MARKET_CLOSED", "scheduled_time": None, "counts": {"checked": 0, "amended": 0, "no_change": 0, "skipped": 0, "failed": 0}, "items": [], "line_status": "งดส่ง (ตลาดพักเที่ยง)"}},
            {"id": "w4_afternoon", "start": "14:00", "end": "16:00", "period": "14:00 - 16:00", "mode": "lure", "allowed_sides": ["BUY", "SELL", "SHORT", "COVER"], "tradable": True, "enabled": True, "label": "ช่วงบ่าย (Lure)", "description": "ต่อราคาแบบล่อ", "in_window_now": False, "run": {"window_id": "w4_afternoon", "status": "PENDING", "scheduled_time": "14:42", "counts": {"checked": 0, "amended": 0, "no_change": 0, "skipped": 0, "failed": 0}, "items": [], "line_status": "รอทำงาน"}},
            {"id": "w5_close", "start": "16:00", "end": "16:30", "period": "16:00 - 16:30", "mode": "match", "allowed_sides": ["SELL", "COVER"], "tradable": True, "enabled": True, "label": "ก่อนปิดตลาด (Match)", "description": "Match เฉพาะ Sell และ Cover", "in_window_now": False, "run": {"window_id": "w5_close", "status": "PENDING", "scheduled_time": "16:15", "counts": {"checked": 0, "amended": 0, "no_change": 0, "skipped": 0, "failed": 0}, "items": [], "line_status": "รอทำงาน"}},
        ],
    }
    static_dir = Path(miniapp_router.STATIC_DIR)
    asset_ver = miniapp_router._compute_asset_version()
    rendered_index = (
        (static_dir / "index.html")
        .read_text(encoding="utf-8")
        .replace("/miniapp/static/", f"/miniapp/static/v/{asset_ver}/")
        .replace("</head>", f'  <meta name="miniapp-version" content="{asset_ver}">\n</head>', 1)
        .encode("utf-8")
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def _json(self, code: int, data: dict):
            raw = json.dumps(data).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path

            if path in ("/miniapp", "/miniapp/", "/miniapp/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(rendered_index)))
                self.end_headers()
                self.wfile.write(rendered_index)
                return

            if "/static/" in path:
                rel = path.split("/static/", 1)[1]
                m = miniapp_router._VERSIONED_PREFIX_RE.match(rel)
                if m:
                    rel = m.group(1)
                target = (static_dir / rel).resolve()
                if target.is_file():
                    ct = (
                        "application/javascript; charset=utf-8"
                        if target.suffix == ".js"
                        else "text/css; charset=utf-8"
                        if target.suffix == ".css"
                        else "text/plain; charset=utf-8"
                    )
                    raw = target.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", ct)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                    return

            if path == "/miniapp/config.json":
                return self._json(200, {
                    "liffConfigured": False,
                    "liffId": "",
                    "assetVersion": asset_ver,
                })

            if path == "/api/v1/miniapp/me":
                return self._json(200, {
                    "ok": True,
                    "user_id": "U_TEST",
                    "mode": {"is_live": False, "label": "DRY-RUN"},
                    "kill_switch": {"is_active": False},
                })

            if path == "/api/v1/miniapp/brokers/status":
                st = getattr(self.server, "extra_state", {})
                brokers_override = st.get("brokers_override") or {
                    "kss": {"authenticated": True, "cdp": {"state": "AUTHENTICATED", "daemon_up": True}},
                    "lib": {"authenticated": True, "cdp": {"state": "AUTHENTICATED", "daemon_up": True}},
                }
                return self._json(200, {
                    "mode": {"is_live": False, "label": "DRY-RUN"},
                    "kill_switch": {"is_active": False},
                    "brokers": brokers_override,
                    "pending_otp": st.get("pending_otp"),
                    "login_progress": st.get("login_progress") or {"kss": None, "lib": None},
                    "line_quota": st.get("line_quota"),
                    "inbox_unread": st.get("inbox_unread", 0),
                    "eod_status": {"sent_today": False, "push_time": "16:40"},
                    "kss_active_account": st.get("kss_active_account", "1000001"),
                    "kss_accounts": [
                        {"account_no": "1000001", "label": "Margin"},
                        {"account_no": "1000002", "label": "Cash"},
                    ],
                    "account_routing": st.get("account_routing") or {
                        "short_broker": "kss",
                        "short_account": "margin",
                        "long_broker": "lib",
                        "long_account": "cash",
                    },
                })

            if path == "/api/v1/miniapp/portfolio":
                st = getattr(self.server, "extra_state", {})
                if st.get("portfolio_override") is not None:
                    return self._json(200, st["portfolio_override"])
                return self._json(200, {
                    "mode": {"is_live": False, "label": "DRY-RUN"},
                    "dry_run": True,
                    "summary": {"total_market_value": 35000, "total_unrealized_pnl": 1000, "partial": False},
                    "brokers": {
                        "kss": {
                            "available": True,
                            "account_no": "1000001",
                            "account_type": "Margin",
                            "accounts": [{"account_no": "1000001", "label": "1000001 (Margin)"}],
                            "rows": [],
                            "borrowed": [
                                {
                                    "symbol": "AAV",
                                    "broker": "kss",
                                    "volume": 94900,
                                    "cost": 0.79,
                                    "last": 0.79,
                                    "pnl_pct": None,
                                    "borrow_qty": 94900,
                                    "remaining_qty": 94900,
                                    "returnable_qty": 94900,
                                    "refs": ["B495090"],
                                },
                            ],
                            "line_available": 250000,
                            "cash_balance": 250000,
                        },
                        "lib": {
                            "available": True,
                            "account_no": "9000001",
                            "account_type": "Cash",
                            "rows": [
                                {"symbol": "PTT", "broker": "lib", "volume": 1000, "cost": 34.0, "last": 35.0, "unrealized_pnl": 1000, "pnl_pct": 2.94, "position_type": "LONG"},
                            ],
                            "line_available": 100000,
                            "cash_balance": 100000,
                        },
                    },
                    "sbl": {
                        "source": "LIVE",
                        "lots": [
                            {"symbol": "AAV", "lot_id": "B495090", "broker_tx_id": "B495090", "remaining_qty": 94900, "borrow_date": "09/11/2026"},
                        ],
                    },
                })

            if path == "/api/v1/miniapp/orders":
                return self._json(200, {
                    "brokers": {"kss": {"available": True}, "lib": {"available": True}},
                    "counts": {"open": 2, "matched": 0, "total": 2},
                    "orders": [
                        {
                            "broker": "kss",
                            "order_no": "8801",
                            "symbol": "PTT",
                            "side": "BUY",
                            "volume": 500,
                            "matched_volume": 0,
                            "balance": 500,
                            "price": 34.50,
                            "status": "O",
                            "kind": "open",
                            "cancellable": True,
                        },
                        {
                            "broker": "kss",
                            "order_no": "8802",
                            "symbol": "JTS",
                            "side": "SHORT",
                            "volume": 100,
                            "matched_volume": 0,
                            "balance": 100,
                            "price": 12.30,
                            "status": "O",
                            "kind": "open",
                            "cancellable": True,
                        },
                    ],
                })

            if path == "/api/v1/miniapp/nego/quote":
                if counters["nego_quote_err_mode"]:
                    return self._json(502, {"detail": {"code": "QUOTE_UNAVAILABLE", "message": "กระดานราคา KSS ไม่ตอบสนอง"}})
                if "JTS" in (self.path or ""):
                    return self._json(200, {
                        "available": True,
                        "symbol": "JTS",
                        "bid_1": 12.10,
                        "offer_1": 12.20,
                        "last": 12.15,
                        "tick_up": 12.20,
                        "tick_down": 12.10,
                        "short_open_uptick_ticks": 1,
                        "short_uptick_ticks": 1,
                        "strict_uptick_reason": "ปิดร่วง -19.8% (2026-11-18)",
                        "match_allowed": False,
                        "lure_plan": {"ok": True, "price": 12.20},
                        "match_plan": {"ok": False, "reason": "Uptick Rule (+1): SHORT ต่อราคาได้เฉพาะแบบล่อ"},
                    })
                return self._json(200, {
                    "available": True,
                    "symbol": "PTT",
                    "bid_1": 34.75,
                    "offer_1": 35.00,
                    "last": 35.00,
                    "tick_up": 35.25,
                    "tick_down": 34.50,
                    "lure_plan": {"ok": True, "price": 34.50},
                    "match_plan": {"ok": True, "price": 35.00},
                })

            if path == "/api/v1/miniapp/eod/net-equity-history":
                return self._json(200, {
                    "count": 3,
                    "csv_path": "data/net_equity_history.csv",
                    "entries": [
                        {"date": "2026-11-14", "total_net_equity": 615000.0, "kss_margin_net_equity": 395000.0, "lib_cash_net_equity": 220000.0},
                        {"date": "2026-11-17", "total_net_equity": 628500.0, "kss_margin_net_equity": 405000.0, "lib_cash_net_equity": 223500.0},
                        {"date": "2026-11-18", "total_net_equity": 635402.67, "kss_margin_net_equity": 410741.58, "lib_cash_net_equity": 224661.09},
                    ],
                })

            if path == "/api/v1/miniapp/nego/schedule":
                return self._json(200, schedule_state)

            if path == "/api/v1/miniapp/runbook/exits":
                return self._json(200, {
                    "ok": True,
                    "ledger_available": True,
                    "as_of": "2026-11-12",
                    "is_today": True,
                    "dry_run": True,
                    "actionable_count": 2,
                    "needed_brokers": ["kss", "lib"],
                    "readiness": {
                        "all_ready": True,
                        "market_phase": "MARKET_OPEN_AM",
                        "is_trading_day": True,
                        "is_market_open": True,
                        "is_holiday": False,
                        "is_weekday": True,
                        "kill_switch": False,
                        "otp_ready": True,
                        "otp_channel": "miniapp",
                        "needed_brokers": ["kss", "lib"],
                        "brokers": {
                            "kss": {"cdp_ready": True, "pin_locked": False},
                            "lib": {"cdp_ready": True, "pin_locked": False},
                        },
                        "errors": [],
                    },
                    "items": [
                        {
                            "broker": "kss",
                            "account": "margin",
                            "symbol": "BAM",
                            "direction": "SHORT",
                            "side": "cover",
                            "action": "COVER",
                            "runbook_volume": 12000,
                            "live_held_vol": 12000,
                            "open_order_vol": 0,
                            "net_send_vol": 12000,
                            "guard_status": "READY",
                            "lure_price": 7.80,
                            "quote_ok": True,
                            "exit_reason": "CS_LOSER_FLIP_CUT",
                        },
                        {
                            "broker": "lib",
                            "account": "cash",
                            "symbol": "PTT",
                            "direction": "LONG",
                            "side": "sell",
                            "action": "SELL",
                            "runbook_volume": 500,
                            "live_held_vol": 500,
                            "open_order_vol": 0,
                            "net_send_vol": 500,
                            "guard_status": "READY",
                            "lure_price": 32.50,
                            "quote_ok": True,
                            "exit_reason": "ATR_STOP",
                        },
                    ],
                })

            if path == "/api/v1/miniapp/trade/hub-summary":
                st = getattr(self.server, "extra_state", {})
                return self._json(200, {
                    "daily_date": "2026-11-16",
                    "dry_run": True,
                    "auto_exit": {
                        "ledger_available": True,
                        "as_of": "2026-11-16",
                        "is_today": True,
                        "count": 2,
                        "items": [
                            {"broker": "kss", "symbol": "BAM", "action": "COVER", "shares": 12000},
                            {"broker": "lib", "symbol": "PTT", "action": "SELL", "shares": 500},
                        ],
                        "schedule": schedule_state["runbook_exit"],
                        "schedule_status_label": "เลยช่วง Auto",
                        "schedule_status_tone": "muted",
                    },
                    "suggest_entry": {
                        "ledger_available": True,
                        "as_of": "2026-11-16",
                        "is_today": True,
                        "budget_per_slot_thb": counters.get("se_budget", 150000.0),
                        "budget_mode": counters.get("se_budget_mode", "recommended"),
                        "is_hold_cash": False,
                        "remaining_long": 1,
                        "remaining_short": 1,
                        "remaining_total": 2,
                        "regime": {"mode": "BOTH", "regime_10": "Neutral"},
                        "quota": {"model_long": 1, "model_short": 2, "free_slots": 2, "remaining_unique_tickers": 5, "cap_unique_tickers": 7},
                        "sizing_reference": st.get("sizing_reference_override") or {"margin_multiplier": 1.5, "house_money_multiplier": 1.0},
                    },
                })

            if path in ("/api/v1/miniapp/suggest-entry/poll", "/api/v1/miniapp/suggest-entry/quotes", "/api/v1/miniapp/suggest-entry/stream"):
                is_live = path in ("/api/v1/miniapp/suggest-entry/quotes", "/api/v1/miniapp/suggest-entry/stream")
                if path == "/api/v1/miniapp/suggest-entry/poll":
                    counters["suggest_poll_calls"] += 1
                elif path == "/api/v1/miniapp/suggest-entry/quotes":
                    counters["suggest_quotes_calls"] = counters.get("suggest_quotes_calls", 0) + 1
                else:
                    counters["suggest_stream_calls"] = counters.get("suggest_stream_calls", 0) + 1

                ckp_last = 2.34 if is_live else 2.30
                poll_payload = {
                    "ledger_available": True,
                    "daily_date": "2026-11-16",
                    "now_bkk": "10:35:02" if is_live else "10:35:00",
                    "as_of": "2026-11-16",
                    "is_today": True,
                    "dry_run": True,
                    "is_hold_cash": False,
                    "budget_per_slot_thb": counters.get("se_budget", 150000.0),
                    "quota": {"remaining_long": 1, "remaining_short": 1, "remaining_total": 2, "free_slots": 2},
                    "brokers": {
                        "kss": {"ready": True, "available": True, "line_available": 450000.0},
                        "lib": {"ready": True, "available": True, "line_available": 320000.0},
                    },
                    "candidates": [
                        {
                            "rank": 2,
                            "ticker": "CKP",
                            "side": "SHORT",
                            "broker": "kss",
                            "account": "margin",
                            "last": ckp_last,
                            "prior_close": 2.28,
                            "live_pct_change": 2.63 if is_live else 0.88,
                            "lure_price": 2.36 if is_live else 2.32,
                            "match_price": None,
                            "match_allowed": False,
                            "planned_shares": 64600,
                            "planned_value": 149872.0,
                            "sbl_available_vol": 219800,
                            "atr20_pct": 1.99,
                            "high_atr_warning": False,
                            "selectable": True,
                            "disabled_reason": None,
                        },
                        {
                            "rank": 3,
                            "ticker": "ORI",
                            "side": "SHORT",
                            "broker": "kss",
                            "account": "margin",
                            "last": 1.72,
                            "prior_close": 1.70,
                            "live_pct_change": 1.18,
                            "lure_price": 1.73,
                            "match_price": None,
                            "match_allowed": False,
                            "planned_shares": 86700,
                            "planned_value": 149991.0,
                            "sbl_available_vol": 518900,
                            "atr20_pct": 2.13,
                            "high_atr_warning": True,
                            "selectable": True,
                            "disabled_reason": None,
                        },
                        {
                            "rank": 1,
                            "ticker": "JMT",
                            "side": "LONG",
                            "broker": "lib",
                            "account": "cash",
                            "last": 10.05,
                            "prior_close": 10.40,
                            "live_pct_change": -3.37,
                            "lure_price": 10.05,
                            "match_price": 10.10,
                            "match_allowed": True,
                            "planned_shares": 14900,
                            "planned_value": 149745.0,
                            "atr20_pct": 1.85,
                            "high_atr_warning": False,
                            "selectable": True,
                            "disabled_reason": None,
                        },
                        {
                            "rank": 4,
                            "ticker": "JAS",
                            "side": "SHORT",
                            "broker": "kss",
                            "account": "margin",
                            "last": 0.63,
                            "prior_close": 0.64,
                            "live_pct_change": -1.56,
                            "lure_price": 0.64,
                            "match_allowed": False,
                            "planned_shares": 0,
                            "planned_value": 0,
                            "selectable": False,
                            "disabled_reason": "ถืออยู่แล้วในพอร์ต",
                        },
                    ],
                }
                if path == "/api/v1/miniapp/suggest-entry/stream":
                    body_bytes = json.dumps(poll_payload).encode("utf-8")
                    frame = b"event: quotes\ndata: " + body_bytes + b"\n\n"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Content-Length", str(len(frame)))
                    self.end_headers()
                    self.wfile.write(frame)
                    self.wfile.flush()
                    return
                return self._json(200, poll_payload)

            if path == "/api/v1/miniapp/quote":
                return self._json(200, {
                    "available": True,
                    "symbol": "PTT",
                    "bid_1": 34.75,
                    "offer_1": 35.00,
                    "last": 35.00,
                })

            if path.startswith("/api/v1/miniapp/sbl/pool/"):
                return self._json(200, {
                    "symbol": "PTT",
                    "available_shares": 100000,
                    "borrow_fee_pct": 3.5,
                    "xd_risk": {"xd_risk": False},
                })

            if path == "/api/v1/miniapp/inbox":
                st = getattr(self.server, "extra_state", {})
                return self._json(200, {
                    "items": st.get("inbox_items", []),
                    "unread_count": st.get("inbox_unread", 0),
                    "quota": st.get("line_quota", {}),
                })

            if path == "/api/v1/miniapp/events":
                st = getattr(self.server, "extra_state", {})
                payload = json.dumps({
                    "unread_count": st.get("inbox_unread", 0),
                    "quota": st.get("line_quota", {}),
                    "pending_otp": st.get("pending_otp"),
                }).encode("utf-8")
                frame = b"event: connected\ndata: " + payload + b"\n\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(frame)))
                self.end_headers()
                self.wfile.write(frame)
                self.wfile.flush()
                return

            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            parsed = urlparse(self.path)
            path = parsed.path
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            body = json.loads(raw or "{}")

            if path == "/api/v1/miniapp/sbl/action":
                counters["sbl_action_calls"] += 1
                if counters["sbl_return_delay_s"]:
                    time.sleep(counters["sbl_return_delay_s"])
                return self._json(200, {
                    "ok": True,
                    "action": body.get("action", "return"),
                    "symbol": body.get("symbol", "AAV"),
                    "volume": body.get("volume") or 94900,
                    "lots_returned": [{"lot_id": "B495090", "ref_no": "B495090", "qty": 94900}],
                    "dry_run": True,
                })

            if path == "/api/v1/miniapp/trade/preview":
                sym = body.get("symbol") or "PTT"
                vol = int(body.get("volume") or 100)
                px = float(body.get("price") or 35.0)
                return self._json(200, {
                    "ok": True,
                    "mode_label": "DRY-RUN",
                    "dry_run": True,
                    "cmd": body.get("side") or "buy",
                    "symbol": sym,
                    "broker": body.get("broker") or "lib",
                    "account": body.get("account") or "cash",
                    "account_no": "9000001",
                    "resolved_volume": vol,
                    "resolved_price": px,
                    "total_thb": round(vol * px, 2),
                })

            if path == "/api/v1/miniapp/trade/execute":
                counters["trade_execute_calls"] += 1
                return self._json(200, {
                    "ok": True,
                    "dry_run": True,
                    "draft_id": "d_dry_1",
                    "order": {
                        "cmd": body.get("side") or "buy",
                        "symbol": body.get("symbol") or "PTT",
                        "volume": body.get("volume") or 100,
                        "price": body.get("price") or 35.0,
                        "broker": body.get("broker") or "lib",
                        "account": body.get("account") or "cash",
                    },
                    "execution": {"broker_order_no": "DRY-8802"},
                })

            if path == "/api/v1/miniapp/orders/cancel":
                counters["cancel_calls"] += 1
                return self._json(200, {"ok": True, "order_no": body.get("order_no"), "dry_run": True})

            if path == "/api/v1/miniapp/nego/execute":
                counters["nego_execute_calls"] += 1
                return self._json(200, {
                    "ok": True,
                    "no_change": False,
                    "dry_run": True,
                    "message": "🧪 [DRY-RUN] ต่อราคา PTT #8801 ฿34.50 → ฿35.00",
                })

            if path == "/api/v1/miniapp/nego/schedule/toggle":
                counters["schedule_toggle_calls"] += 1
                wid = body.get("window_id")
                en = bool(body.get("enabled"))
                if wid is None:
                    schedule_state["master_enabled"] = en
                else:
                    for w in schedule_state["windows"]:
                        if w["id"] == wid:
                            w["enabled"] = en
                            w["run"]["status"] = "PENDING" if en else "DISABLED"
                            w["run"]["scheduled_time"] = "10:18" if en else None
                            w["run"]["line_status"] = "รอทำงาน" if en else "งดส่ง (ปิดอยู่)"
                return self._json(200, schedule_state)

            if path == "/api/v1/miniapp/nego/schedule/reroll":
                return self._json(200, schedule_state)

            if path == "/api/v1/miniapp/nego/schedule/run-now":
                counters["schedule_run_calls"] += 1
                wid = body.get("window_id")
                for w in schedule_state["windows"]:
                    if w["id"] == wid:
                        w["run"]["status"] = "AMENDED"
                        w["run"]["finished_at"] = "11:18:02"
                        w["run"]["counts"] = {"checked": 1, "amended": 1, "no_change": 0, "skipped": 0, "failed": 0}
                        w["run"]["items"] = [{"broker": "kss", "order_no": "8801", "symbol": "PTT", "side": "BUY", "old_price": 34.50, "new_price": 34.75, "outcome": "DRY_RUN", "reason": "จำลองแก้ราคา (DRY-RUN)"}]
                        w["run"]["line_status"] = "งดส่ง LINE (ประหยัดโควต้า)"
                return self._json(200, schedule_state)

            if path == "/api/v1/miniapp/runbook/exits/schedule/toggle":
                counters["runbook_toggle_calls"] += 1
                en = bool(body.get("enabled"))
                rb = schedule_state["runbook_exit"]
                rb["auto_enabled"] = en
                rb["queue"] = {
                    "status": "AUTO_PENDING",
                    "window_start": "10:00",
                    "window_end": "10:30",
                    "scheduled_time": "10:15",
                    "dry_run": True,
                    "line_status": "รอทำงานอัตโนมัติ (งดส่ง LINE)",
                    "counts": {"total": 0, "executed": 0, "adjusted": 0, "skipped": 0, "failed": 0},
                    "items": [],
                } if en else None
                return self._json(200, schedule_state)

            if path == "/api/v1/miniapp/runbook/exits/schedule/run-now":
                counters["runbook_run_calls"] += 1
                schedule_state["runbook_exit"]["queue"] = {
                    "status": "DONE",
                    "window_start": "10:00",
                    "window_end": "10:30",
                    "scheduled_time": "10:15",
                    "finished_at": "10:16:00",
                    "dry_run": True,
                    "line_status": "งดส่ง LINE (ดูผลใน Schedule)",
                    "counts": {"total": 2, "executed": 2, "adjusted": 0, "skipped": 0, "failed": 0},
                    "items": [
                        {
                            "broker": "kss",
                            "symbol": "BAM",
                            "action": "COVER",
                            "runbook_volume": 12000,
                            "net_send_vol": 12000,
                            "lure_price": 7.80,
                            "outcome": "DRY_RUN",
                            "broker_order_no": "DRY-RB-1",
                        }
                    ],
                }
                return self._json(200, schedule_state)

            if path in ("/api/v1/miniapp/runbook/exits/schedule/reroll", "/api/v1/miniapp/runbook/exits/schedule/cancel"):
                return self._json(200, schedule_state)

            if path == "/api/v1/miniapp/suggest-entry/config":
                st = getattr(self.server, "extra_state", {})
                use_rec = bool(body.get("use_recommended"))
                if use_rec:
                    counters["se_budget_mode"] = "recommended"
                    sref = st.get("sizing_reference_override") or {}
                    counters["se_budget"] = float(sref.get("recommended_budget_thb") or body.get("budget_per_slot_thb") or 150000.0)
                else:
                    counters["se_budget_mode"] = "manual"
                    counters["se_budget"] = float(body.get("budget_per_slot_thb") or 150000.0)
                return self._json(200, {
                    "ok": True,
                    "budget_mode": counters["se_budget_mode"],
                    "budget_per_slot_thb": counters["se_budget"],
                })

            if path == "/api/v1/miniapp/suggest-entry/submit":
                counters["suggest_submit_calls"] += 1
                items = body.get("items") or []
                return self._json(200, {
                    "ok": True,
                    "dry_run": True,
                    "submitted_count": len(items),
                    "failed_count": 0,
                    "line_status": "งดส่ง LINE (ดูผลใน MiniApp)",
                    "results": [
                        {
                            "symbol": it["symbol"],
                            "side": it["side"],
                            "ok": True,
                            "dry_run": True,
                            "status": "DRY_RUN",
                            "shares": 64600,
                            "price": 2.32,
                            "price_mode": it.get("price_mode") or "lure",
                            "order_no": "DRY-SE-1",
                        }
                        for it in items
                    ],
                })

            if path == "/api/v1/miniapp/portfolio/kss-account":
                counters["kss_switch_calls"] = counters.get("kss_switch_calls", 0) + 1
                acct = str((body or {}).get("account_no") or "1000001")
                st = getattr(self.server, "extra_state", {})
                st["kss_active_account"] = acct
                return self._json(200, {
                    "brokers": {
                        "kss": {
                            "broker": "kss",
                            "available": True,
                            "error": None,
                            "account_no": acct,
                            "account_type": "CREDIT_BALANCE" if acct == "1000001" else "CASH_BALANCE",
                        }
                    },
                    "kss_active_account": acct,
                })

            if path == "/api/v1/miniapp/brokers/account-routing":
                counters["routing_save_calls"] = counters.get("routing_save_calls", 0) + 1
                st = getattr(self.server, "extra_state", {})
                saved = {
                    "short_broker": body.get("short_broker", "kss"),
                    "short_account": body.get("short_account", "margin"),
                    "long_broker": body.get("long_broker", "lib"),
                    "long_account": body.get("long_account", "cash"),
                }
                st["account_routing"] = saved
                return self._json(200, {"ok": True, "account_routing": saved})

            if path == "/api/v1/miniapp/otp":
                counters["otp_calls"] = counters.get("otp_calls", 0) + 1
                st = getattr(self.server, "extra_state", {})
                cur_p = st.get("pending_otp")
                if cur_p:
                    cur_p = {**cur_p, "submitted": True}
                    st["pending_otp"] = cur_p
                return self._json(200, {
                    "ok": True,
                    "submitted": True,
                    "pending_otp": cur_p,
                    "message": "ส่ง OTP เข้าสู่ระบบแล้ว กำลังรอโบรกเกอร์ยืนยัน...",
                })

            if path == "/api/v1/miniapp/inbox/read":
                counters["inbox_read_calls"] = counters.get("inbox_read_calls", 0) + 1
                st = getattr(self.server, "extra_state", {})
                item_id = body.get("item_id") if isinstance(body, dict) else None
                for it in st.get("inbox_items", []):
                    if not item_id or it.get("id") == item_id:
                        it["read"] = True
                st["inbox_unread"] = sum(1 for it in st.get("inbox_items", []) if not it.get("read"))
                return self._json(200, {"ok": True, "unread_count": st["inbox_unread"]})

            if path in ("/api/v1/miniapp/brokers/kss/login", "/api/v1/miniapp/brokers/lib/login"):
                brk = "kss" if "/kss/" in path else "lib"
                counters.setdefault("login_seq", []).append(brk)
                st = getattr(self.server, "extra_state", {})
                if callable(st.get("on_broker_login")):
                    st["on_broker_login"](brk, st)
                return self._json(200, {"ok": True, "broker": brk, "result": {"state": "AUTHENTICATED"}})

            return self._json(404, {"detail": "not found"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.extra_state = {
        "pending_otp": {
            "broker": "kss",
            "broker_label": "Krungsri (KSS)",
            "ref": "TEST",
            "expires_in_seconds": 180,
        },
        "line_quota": {
            "used": 12,
            "limit": 300,
            "pct": 4.0,
            "exhausted": False,
            "push_mode": "eod_only",
            "eod_push_time": "16:40",
        },
        "inbox_unread": 1,
        "inbox_items": [
            {
                "id": "msg-1",
                "ts": "2026-11-12T10:30:00+07:00",
                "category": "nego",
                "severity": "info",
                "title": "Auto-Nego triggered",
                "body": "Window 10:30-12:30 started checking unmatched orders.",
                "read": False,
                "action_tab": "schedule",
                "line_pushed": False,
            }
        ],
    }
    return server, counters


@pytest.mark.asyncio
async def test_miniapp_playwright_ui_locks_modals_banners_and_trade_flow():
    """Verify UI Lock prevents double-submit, 2-stage result modal & banner persist, Trade doesn't auto-jump, and Tick errors surface."""
    from playwright.async_api import async_playwright

    server, counters = _make_mock_miniapp_server()
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        async with async_playwright() as pw:
            launch_kw = {"headless": True}
            if os.path.exists(settings.PLAYWRIGHT_CHROME_EXECUTABLE):
                launch_kw["executable_path"] = settings.PLAYWRIGHT_CHROME_EXECUTABLE
            browser = await pw.chromium.launch(**launch_kw)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.route("https://static.line-scdn.net/**", lambda route: route.fulfill(status=200, content_type="application/javascript", body=""))
            page_errors = []
            page.on("pageerror", lambda err: page_errors.append(str(err)))

            base_url = f"http://127.0.0.1:{port}/miniapp/?tab=portfolio"
            await page.goto(base_url, wait_until="networkidle")

            # 1) Portfolio SBL Return: verify borrowed AAV shows amber (B) badge (not L) & Short quick button
            await page.wait_for_selector("[data-sym='AAV'] [data-expand-row]")
            assert (await page.inner_text("[data-sym='AAV'] .tag-b")).strip() == "B"
            assert await page.locator("[data-sym='AAV'] .tag-l").count() == 0
            assert await page.is_visible("[data-sym='AAV'] .act-short")
            await page.click("[data-sym='AAV'] [data-expand-row]")
            await page.wait_for_selector("[data-sbl-return]")
            await page.click("[data-sbl-return]")
            await page.wait_for_selector("#sheet-confirm")

            # Double click confirm rapidly & try backdrop click mid-flight
            await page.click("#sheet-confirm")
            await page.evaluate("document.getElementById('sheet-confirm').click()")
            await page.evaluate("document.getElementById('sheet-backdrop').click()")

            # Immediately modal must be in busy stage & bottom-nav locked
            title_mid = await page.inner_text("#sheet-title")
            assert "กำลัง" in title_mid
            assert await page.evaluate("document.getElementById('app').classList.contains('app-busy')")

            # Wait for Result Modal transition & verify only 1 backend POST fired!
            await page.wait_for_selector("#sheet-res-close", timeout=5000)
            assert counters["sbl_action_calls"] == 1
            res_body = await page.inner_text("#sheet-body")
            assert "AAV" in res_body and "94,900" in res_body and "B495090" in res_body

            # Persistent banner is visible right after modal closes
            await page.click("#sheet-res-close")
            assert await page.is_visible("#last-result-banner")
            banner_txt = await page.inner_text("#last-result-banner")
            assert "คืนหุ้น SBL สำเร็จ" in banner_txt

            # 2) Trade -> Top-Bar Non-Overlap + Full-Width Stacked Hub (Suggest Entry -> Auto Exit) + Collapsed Advance
            await page.click(".tab-btn[data-tab='trade']")
            await page.wait_for_selector("#trade-status-strip", timeout=3000)
            await page.wait_for_selector("#hub-suggest-entry-card", timeout=3000)
            await page.wait_for_selector("#hub-auto-exit-card", timeout=3000)

            # Verify full-width vertical stacking (Suggest Entry above Auto Exit) on 390px mobile viewport
            stacked_ok = await page.evaluate("""() => {
              const s = document.getElementById('hub-suggest-entry-card')?.getBoundingClientRect();
              const e = document.getElementById('hub-auto-exit-card')?.getBoundingClientRect();
              return s && e ? (s.width > 300 && e.width > 300 && s.bottom <= e.top + 2) : false;
            }""")
            assert stacked_ok is True, "Tab Trade cards must stack full-width vertically with Suggest Entry on top"

            exit_card_txt = await page.inner_text("#hub-auto-exit-card")
            assert "เลยช่วง Auto" in exit_card_txt
            assert "MISSED_WINDOW" not in exit_card_txt

            # Verify Top-Bar elements do not overlap on 390px mobile viewport
            overlap = await page.evaluate("""() => {
              const b = document.querySelector('.brand')?.getBoundingClientRect();
              const k = document.getElementById('top-kss-pill')?.getBoundingClientRect();
              return b && k ? b.right > k.left + 1 : true;
            }""")
            assert overlap is False, "Top-bar .brand overlaps #top-kss-pill on mobile viewport"

            # Manual Advance (สั่งปกติ · Quick · SBL) is collapsed by default
            assert await page.locator("#tr-manual-advance-panel").count() == 0

            # Auto Exit card: Runbook Exit preview works directly from primary hub
            async with page.expect_response("**/api/v1/miniapp/runbook/exits"):
                await page.click("#tr-runbook-fetch-btn")
            await page.wait_for_selector("#tr-runbook-close-btn", timeout=3000)
            assert "BAM" in await page.inner_text("#runbook-exit-card")
            await page.click("#tr-runbook-close-btn")

            # Manual custom budget save (preset chips 50k/100k/150k/200k removed) -> Open Multitrade Poll Modal
            assert await page.locator("[data-se-budget-preset]").count() == 0
            await page.fill("#se-budget-input", "100000")
            async with page.expect_response("**/api/v1/miniapp/suggest-entry/config"):
                await page.click("#se-budget-save-btn")
            assert await page.input_value("#se-budget-input") == "100000"
            await page.fill("#se-budget-input", "200000")
            async with page.expect_response("**/api/v1/miniapp/suggest-entry/config"):
                await page.click("#se-budget-save-btn")
            async with page.expect_response("**/api/v1/miniapp/suggest-entry/poll*"):
                await page.click("#tr-suggest-poll-btn")
            await page.wait_for_selector("#se-poll-backdrop", timeout=3000)
            assert await page.is_visible("#se-modal-close")
            assert "Live 2s" in await page.inner_text("#se-live-toggle")
            # Wait for SSE live quote tick updating CKP from 2.30 -> 2.34 in-place
            await page.wait_for_function(
                "() => (document.querySelector(\"[data-se-row='SHORT:CKP'] [data-se-last]\")?.textContent || '').includes('2.34')",
                timeout=3000,
            )
            assert counters.get("suggest_stream_calls", 0) >= 1
            # Starts unchecked & Lure/Match hidden until LONG checked
            assert await page.locator("[data-se-chk]:checked").count() == 0
            assert await page.locator("[data-se-pmode^='LONG:JMT']").count() == 0
            # Filter pills work cleanly
            await page.click("[data-se-filter='short']")
            assert await page.locator("[data-se-row='LONG:JMT']").count() == 0
            await page.click("[data-se-filter='all']")
            await page.wait_for_selector("[data-se-row='LONG:JMT']", timeout=2000)
            # Check CKP -> reaches remaining_short=1 so ORI becomes disabled; pause live toggle works
            await page.click("[data-se-chk='SHORT:CKP']")
            assert await page.is_disabled("[data-se-chk='SHORT:ORI']")
            await page.click("[data-se-chk='LONG:JMT']")
            assert await page.locator("[data-se-pmode='LONG:JMT']").count() == 2
            await page.click("#se-live-toggle")
            assert "หยุด" in await page.inner_text("#se-live-toggle")
            assert await page.is_checked("[data-se-chk='SHORT:CKP']") is True
            async with page.expect_response("**/api/v1/miniapp/suggest-entry/submit"):
                await page.click("#se-modal-submit-btn")
            await page.wait_for_selector("#se-res-close", timeout=5000)
            assert counters["suggest_submit_calls"] == 1
            assert "DRY_RUN" in await page.inner_text("#se-poll-backdrop")
            await page.click("#se-res-close")

            # Expand Manual Advance -> สั่งปกติ (with its own collapsed #tr-advance-toggle) + 2-Click Preview->Confirm Execute
            await page.click("#tr-manual-advance-toggle")
            await page.wait_for_selector("#tr-advance-toggle", timeout=3000)
            assert await page.locator("#tr-advance-panel").count() == 0
            await page.click("#tr-advance-toggle")
            await page.fill("#tr-symbol", "PTT")
            async with page.expect_response("**/api/v1/miniapp/quote*"):
                await page.click("#tr-quote-btn")
            await page.fill("#tr-price", "35.00")

            # Inject 502 error on tick step — must surface error toast & unlock button, zero pageerror
            counters["nego_quote_err_mode"] = True
            async with page.expect_response("**/api/v1/miniapp/nego/quote*"):
                await page.click("#tr-px-down")
            await page.wait_for_selector(".toast-err", timeout=3000)
            await page.wait_for_function("!document.getElementById('tr-px-down').disabled")
            counters["nego_quote_err_mode"] = False

            # 2-click submit: #tr-preview-btn previews and opens #sheet-confirm directly
            await page.click("#tr-preview-btn")
            await page.wait_for_selector("#sheet-confirm", timeout=5000)
            await page.click("#sheet-confirm")
            await page.wait_for_selector("#sheet-res-cta-0", timeout=5000)
            assert counters["trade_execute_calls"] == 1

            # Verify Trade tab is STILL active (Q3: did NOT auto-navigate away!)
            assert await page.evaluate("document.querySelector('.tab-btn.active')?.dataset.tab") == "trade"
            # Clicking '📋 ไปดู Orders' CTA navigates to Orders tab
            await page.click("#sheet-res-cta-0")
            await page.wait_for_selector("[data-cancel-order]", timeout=3000)
            assert await page.evaluate("document.querySelector('.tab-btn.active')?.dataset.tab") == "orders"

            # 3) Orders -> Compact 2-Row Filter + Cancel -> Result Modal
            assert await page.locator("#orders-broker-select").count() == 1
            assert await page.locator(".seg-bar.seg-compact [data-sfilter]").count() == 3
            await page.select_option("#orders-broker-select", "kss")
            await page.select_option("#orders-broker-select", "all")
            await page.click("[data-cancel-order]")
            await page.click("#sheet-confirm")
            await page.wait_for_selector("#sheet-res-close", timeout=5000)
            assert counters["cancel_calls"] == 1
            assert "8801" in await page.inner_text("#sheet-body")
            await page.click("#sheet-res-close")

            # 4) Orders -> Integrated Fast Nego Sheet -> Tick error safe + Match Amend Result Modal
            await page.click("[data-nego-order]")
            await page.wait_for_selector("#nego-match-btn:not([disabled])", timeout=3000)
            counters["nego_quote_err_mode"] = True
            async with page.expect_response("**/api/v1/miniapp/nego/quote*"):
                await page.click("#nego-tick-up")
            await page.wait_for_function("!document.getElementById('nego-tick-up').disabled")
            await page.wait_for_selector(".toast-err", timeout=3000)
            counters["nego_quote_err_mode"] = False

            await page.click("#nego-match-btn")
            await page.click("#sheet-confirm")
            await page.wait_for_selector("#sheet-res-close", timeout=5000)
            assert counters["nego_execute_calls"] == 1
            nego_title = await page.inner_text("#sheet-title")
            assert ("ต่อราคาแล้ว" in nego_title) or ("แก้ราคาสำเร็จ" in nego_title)
            await page.click("#sheet-res-close")

            # SHORT order with -10% (+1) removes #nego-match-btn completely instead of rendering disabled Match
            await page.locator("[data-nego-order]").nth(1).click()
            await page.wait_for_selector("#nego-lure-btn:not([disabled])", timeout=3000)
            assert await page.locator("#nego-match-btn").count() == 0
            await page.click("#sheet-cancel")
            await page.locator("#sheet-backdrop").wait_for(state="hidden", timeout=3000)

            # 5) Nego (Schedule) tab -> Unified Timeline (Runbook + 3 Nego slots, no w1_open & no lunch), Direct Toggle & Expandable Run-Now
            await page.click(".tab-btn[data-tab='nego']")
            await page.wait_for_selector("#nego-master-toggle", timeout=3000)
            await page.wait_for_selector("[data-timeline-row]", timeout=3000)
            assert await page.locator("[data-timeline-row]").count() == 4
            assert await page.locator("[data-timeline-row='w1_open']").count() == 0
            assert await page.locator("[data-timeline-row='w3_lunch']").count() == 0
            assert await page.locator("#schedule-timeline-card").count() == 1

            async with page.expect_response("**/api/v1/miniapp/nego/schedule/toggle"):
                await page.click("[data-win-toggle='w4_afternoon']")
            assert counters["schedule_toggle_calls"] == 1
            assert await page.is_checked("[data-win-toggle='w4_afternoon']") is False

            # Expand w2_morning and run directly without confirmation modal
            await page.click("[data-toggle-items='w2_morning']")
            await page.wait_for_selector("[data-win-run='w2_morning']", timeout=3000)
            async with page.expect_response("**/api/v1/miniapp/nego/schedule/run-now"):
                await page.click("[data-win-run='w2_morning']")
            assert counters["schedule_run_calls"] == 1
            await page.wait_for_function(
                "() => (document.getElementById('schedule-timeline-card')?.innerText || '').includes('PTT')"
            )
            # Once run (AMENDED), toggle is locked (disabled)
            assert await page.is_disabled("[data-win-toggle='w2_morning']") is True

            # 5b) Runbook Auto-Exit row in Unified Timeline (Auto Toggle, Expand for Preview & Direct Run-Now)
            await page.wait_for_selector("#rb-auto-toggle", timeout=3000)
            assert await page.is_checked("#rb-auto-toggle") is True
            await page.click("[data-toggle-items='runbook_exit']")
            await page.wait_for_selector("#rb-preview-btn", timeout=3000)
            async with page.expect_response("**/api/v1/miniapp/runbook/exits"):
                await page.click("#rb-preview-btn")
            await page.wait_for_selector("#rb-preview-close-btn", timeout=3000)
            rb_card_txt = await page.inner_text("[data-timeline-row='runbook_exit']")
            assert "BAM" in rb_card_txt and "PTT" in rb_card_txt
            assert await page.locator("[data-timeline-row='runbook_exit'] [data-rb-sym]").count() == 0

            async with page.expect_response("**/api/v1/miniapp/runbook/exits/schedule/toggle"):
                await page.click("#rb-auto-toggle")
            assert counters["runbook_toggle_calls"] == 1
            assert await page.is_checked("#rb-auto-toggle") is False
            async with page.expect_response("**/api/v1/miniapp/runbook/exits/schedule/toggle"):
                await page.click("#rb-auto-toggle")
            assert counters["runbook_toggle_calls"] == 2
            assert await page.is_checked("#rb-auto-toggle") is True

            async with page.expect_response("**/api/v1/miniapp/runbook/exits/schedule/run-now"):
                await page.click("#rb-run-now-btn")
            assert counters["runbook_run_calls"] == 1
            await page.wait_for_function(
                "() => (document.querySelector('[data-timeline-row=\"runbook_exit\"]')?.innerText || '').includes('BAM')"
            )
            rb_after = await page.inner_text("[data-timeline-row='runbook_exit']")
            assert "BAM" in rb_after and "COVER" in rb_after

            # 6) Top-Bar Quota & Inbox badge + Global Sticky OTP Banner + Inbox Drawer
            assert "12/300" in (await page.inner_text("#top-quota-pill"))
            assert (await page.inner_text("#top-inbox-badge")).strip() == "1"

            await page.wait_for_selector("#sticky-otp-banner:not(.hidden)", timeout=5000)
            banner_txt = await page.inner_text("#sticky-otp-banner")
            assert "Ref: TEST" in banner_txt
            # Inline 6-box segmented OTP directly inside sticky banner on any tab + SMS AutoFill attr
            assert await page.locator("#sticky-otp-banner .otp-digit").count() == 6
            assert await page.locator("#sticky-otp-digit-0[autocomplete='one-time-code']").count() == 1
            # Filling 6 digits into box 0 (SMS AutoFill / paste behavior) auto-submits immediately
            async with page.expect_response("**/api/v1/miniapp/otp"):
                await page.fill("#sticky-otp-digit-0", "654321")
            assert counters.get("otp_calls") == 1
            await page.wait_for_selector("[data-testid='sticky-otp-verifying']", timeout=5000)
            assert "กำลังตรวจสอบ OTP" in (await page.inner_text("#sticky-otp-banner"))
            # Switch to brokers tab — sticky banner hides while brokers tab renders
            await page.click(".tab-btn[data-tab='brokers']")
            await page.locator("#sticky-otp-banner").wait_for(state="hidden", timeout=5000)
            await page.wait_for_selector("#brokers-otp-card [data-testid='card-otp-verifying']", timeout=5000)

            # Verify Brokers progressive disclosure, KSS account switcher & Short/Long routing card
            assert await page.locator("#brokers-kss-acct").count() == 1
            assert await page.locator("#brokers-routing-card").count() == 1
            await page.select_option("#routing-long-select", "kss:cash")
            async with page.expect_response("**/api/v1/miniapp/brokers/account-routing"):
                await page.click("#routing-save-btn")
            assert counters.get("routing_save_calls") == 1
            assert await page.locator("#brokers-sys-adv-panel").count() == 0
            assert await page.locator("[data-broker-adv-panel='kss']").count() == 0
            await page.click("[data-broker-adv='kss']")
            await page.wait_for_selector("[data-broker-adv-panel='kss']", timeout=3000)
            await page.click("#brokers-sys-adv-toggle")
            await page.wait_for_selector("#brokers-sys-adv-panel [data-action='eod-preview']", timeout=3000)

            await page.click("#top-inbox-btn")
            await page.wait_for_selector("#inbox-drawer:not(.hidden)", timeout=5000)
            assert "12 / 300" in (await page.inner_text("#inbox-quota-summary"))
            assert "Auto-Nego triggered" in (await page.inner_text("#inbox-list"))
            await page.click("#inbox-mark-all-btn")
            await page.locator("#top-inbox-badge").wait_for(state="hidden", timeout=5000)
            assert counters.get("inbox_read_calls") == 1
            await page.click("#inbox-close-btn")
            await page.locator("#inbox-drawer").wait_for(state="hidden", timeout=5000)

            # 7) Portfolio progressive disclosure & row-tap expand
            await page.click(".tab-btn[data-tab='portfolio']")
            await page.wait_for_selector("#port-toggle-details-btn", timeout=5000)
            assert await page.locator(".port-summary-drawer").count() == 0
            await page.click("#port-toggle-details-btn")
            await page.wait_for_selector(".port-summary-drawer", timeout=3000)
            drawer_txt = await page.inner_text(".port-summary-drawer")
            assert "MARGIN" in drawer_txt and "CASH" in drawer_txt
            assert await page.locator("#port-kss-acct").count() == 0
            before_drawers = await page.locator(".port-row-drawer").count()
            await page.locator("[data-row-toggle]").first.click()
            assert await page.locator(".port-row-drawer").count() != before_drawers

            assert page_errors == [], f"Unexpected browser JS errors: {page_errors}"
            await browser.close()
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_miniapp_playwright_live_stepper_and_connect_all_queue():
    """Verify Live 4-step Login Stepper error state and 1-tap KSS -> LIB sequential queue."""
    from playwright.async_api import async_playwright

    server, counters = _make_mock_miniapp_server()
    server.extra_state["pending_otp"] = None
    server.extra_state["brokers_override"] = {
        "kss": {"slug": "kss", "label": "Krungsri (KSS)", "port": 9333, "authenticated": False, "cdp": {"state": "LOGIN_PAGE", "daemon_up": True}},
        "lib": {"slug": "lib", "label": "Liberator", "port": 9334, "authenticated": False, "cdp": {"state": "LOGIN_PAGE", "daemon_up": True}},
    }
    server.extra_state["login_progress"] = {
        "kss": None,
        "lib": {
            "broker": "lib",
            "phase": "error",
            "step": 2,
            "attempt": 2,
            "is_active": False,
            "error": "เซิร์ฟเวอร์ Liberator ตอบช้าเกินกำหนด (/va/login timeout 10s)",
            "message": "เซิร์ฟเวอร์ Liberator ตอบช้าเกินกำหนด (/va/login timeout 10s)",
        },
    }

    def on_login(brk, st):
        st["brokers_override"][brk]["authenticated"] = True
        st["brokers_override"][brk]["cdp"]["state"] = "AUTHENTICATED"
        st["login_progress"][brk] = {"broker": brk, "phase": "authenticated", "step": 4, "is_active": False, "message": "พร้อมเทรด"}

    server.extra_state["on_broker_login"] = on_login

    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        async with async_playwright() as pw:
            launch_kw = {"headless": True}
            if os.path.exists(settings.PLAYWRIGHT_CHROME_EXECUTABLE):
                launch_kw["executable_path"] = settings.PLAYWRIGHT_CHROME_EXECUTABLE
            browser = await pw.chromium.launch(**launch_kw)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.route("https://static.line-scdn.net/**", lambda route: route.fulfill(status=200, content_type="application/javascript", body=""))
            page_errors = []
            page.on("pageerror", lambda err: page_errors.append(str(err)))

            await page.goto(f"http://127.0.0.1:{port}/miniapp/?tab=brokers", wait_until="networkidle")

            # Live Stepper renders Liberator's error + attempt count + Retry button
            await page.wait_for_selector("[data-login-stepper='lib']", timeout=5000)
            stepper_txt = await page.inner_text("[data-login-stepper='lib']")
            assert "/va/login timeout 10s" in stepper_txt
            assert "ลอง Login ใหม่" in (await page.inner_text("[data-broker-card='lib']"))

            # 1-tap sequential queue executes KSS -> LIB in order
            await page.wait_for_selector("#connect-all-btn", timeout=5000)
            await page.click("#connect-all-btn")
            await page.wait_for_function(
                "() => (document.querySelector(\"[data-broker-card='kss']\")?.innerText || '').includes('AUTH') && (document.querySelector(\"[data-broker-card='lib']\")?.innerText || '').includes('AUTH')",
                timeout=5000,
            )
            assert counters.get("login_seq") == ["kss", "lib"]
            assert await page.locator("#connect-all-btn").count() == 0
            assert page_errors == [], f"Unexpected browser JS errors: {page_errors}"
            await browser.close()
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_miniapp_playwright_all_7_trello_inbox_fixes():
    """End-to-end Playwright verification across all 7 Trello Inbox backlog fixes."""
    from playwright.async_api import async_playwright

    server, counters = _make_mock_miniapp_server()
    asset_ver = miniapp_router._compute_asset_version()
    base_handler_cls = server.RequestHandlerClass

    custom_state = {
        "auth_seq": 0,
        "force_401_on_token": None,
        "port_delay_s": 0.0,
        "orders_delay_s": 0.0,
        "order_matched": False,
        "runbook_done": False,
        "budget_thb": 50000.0,
    }
    counters["auth_session_calls"] = 0
    counters["schedule_401_sent"] = 0
    counters["port_sync_calls"] = 0
    counters["orders_sync_calls"] = 0
    counters["inbox_clear_calls"] = 0

    server.extra_state["inbox_unread"] = 2
    server.extra_state["inbox_items"] = [
        {
            "id": "ord-ori-1",
            "ts": "2026-11-17T10:08:00+07:00",
            "category": "order",
            "severity": "success",
            "title": "Filled ORI",
            "body": "KSS COVER ORI 43,800 matched.",
            "read": False,
            "action_tab": "portfolio",
            "line_pushed": False,
            "meta": {},
        },
        {
            "id": "gw-1",
            "ts": "2026-11-17T09:50:00+07:00",
            "category": "system",
            "severity": "info",
            "title": "🚀 InvestmentHomelab Gateway ออนไลน์",
            "body": "Gateway restarted cleanly.",
            "read": True,
            "action_tab": "",
            "line_pushed": False,
            "meta": {"dedup_key": "gateway_online", "is_heartbeat": True, "repeat_count": 3},
        },
    ]

    class All7Handler(base_handler_cls):
        def do_GET(self):
            path = urlparse(self.path).path

            if path == "/miniapp/config.json":
                return self._json(200, {
                    "liffConfigured": True,
                    "liffId": "2012540531-S5Ozs4US",
                    "assetVersion": asset_ver,
                })

            if path == "/api/v1/miniapp/nego/schedule":
                auth_hdr = self.headers.get("Authorization") or ""
                forced_tok = custom_state.get("force_401_on_token")
                if forced_tok and auth_hdr == f"Bearer {forced_tok}":
                    custom_state["force_401_on_token"] = None
                    counters["schedule_401_sent"] += 1
                    return self._json(401, {"detail": "LIFF/Session token expired"})

                rb_queue = (
                    {
                        "status": "DONE",
                        "window_start": "10:00",
                        "window_end": "10:30",
                        "finished_at": "10:07:12",
                        "counts": {"done": 2, "total": 2},
                        "items": [],
                    }
                    if custom_state["runbook_done"]
                    else {
                        "status": "PENDING",
                        "window_start": "10:00",
                        "window_end": "10:30",
                        "scheduled_time": "10:06",
                        "counts": {"done": 0, "total": 2},
                        "items": [],
                    }
                )
                return self._json(200, {
                    "daily_date": "2026-11-17",
                    "is_weekday": True,
                    "master_enabled": True,
                    "dry_run": False,
                    "kill_switch": {"is_active": False},
                    "next_run": None,
                    "runbook_exit": {
                        "auto_enabled": True,
                        "is_holiday": False,
                        "market_phase": "AFTERNOON",
                        "queue": rb_queue,
                    },
                    "windows": [
                        {
                            "id": "w2_morning",
                            "label": "Morning Nego",
                            "period": "10:30–12:30",
                            "mode": "lure",
                            "enabled": True,
                            "tradable": True,
                            "in_window_now": False,
                            "run": {"status": "DONE", "finished_at": "11:02:00", "counts": {}, "items": []},
                        },
                        {
                            "id": "w4_afternoon",
                            "label": "Afternoon Nego",
                            "period": "14:30–16:00",
                            "mode": "lure",
                            "enabled": True,
                            "tradable": True,
                            "in_window_now": False,
                            "run": {"status": "PENDING", "scheduled_time": "15:12", "counts": {}, "items": []},
                        },
                        {
                            "id": "w5_close",
                            "label": "Close Match / Best Short",
                            "period": "16:00–16:30",
                            "mode": "match",
                            "enabled": True,
                            "tradable": True,
                            "in_window_now": False,
                            "run": {"status": "PENDING", "scheduled_time": "16:06", "counts": {}, "items": []},
                        },
                        {
                            "id": "w6_post_close",
                            "label": "Post-Close SBL Return & Cancel",
                            "period": "16:30–17:30",
                            "mode": "post_close",
                            "enabled": True,
                            "tradable": True,
                            "in_window_now": False,
                            "run": {"status": "PENDING", "scheduled_time": "16:40", "counts": {}, "items": []},
                        },
                    ],
                    "eod": {
                        "enabled": True,
                        "line_push_enabled": False,
                        "push_time": "17:05",
                        "last_sent_date": "2026-11-17",
                        "csv_path": "data/net_equity_history.csv",
                        "today_net_equity": {
                            "date": "2026-11-17",
                            "total_net_equity": 634428.67,
                            "kss_margin_net_equity": 409767.58,
                            "lib_cash_net_equity": 224661.09,
                            "matched_orders": 2,
                            "open_orders": 0,
                            "recorded_at": "2026-11-17T17:05:10+07:00",
                        },
                        "last_result": {"date": "2026-11-17", "sent": False, "counts": {"matched": 2, "open": 0}, "net_equity": 634428.67},
                    },
                    "daily_pipeline": {
                        "scheduled_time": "17:30",
                        "timer": "trading-daily-shadow.timer",
                        "available": True,
                        "status": "OK",
                        "ran_post_close_today": True,
                        "started": "2026-11-17T17:30:02+07:00",
                        "seconds": 18.4,
                        "failed": False,
                        "stages": [{"id": "PRE-SBL", "status": "ok"}, {"id": "MAIN", "status": "ok"}],
                    },
                })

            if path == "/api/v1/miniapp/portfolio":
                if custom_state["port_delay_s"] > 0:
                    time.sleep(custom_state["port_delay_s"])
                counters["port_sync_calls"] += 1
                return self._json(200, {
                    "summary": {
                        "total_unrealized_pnl": 12500.0,
                        "partial": False,
                    },
                    "kss_active_account": "1000001",
                    "kss_accounts": [
                        {"account_no": "1000001", "label": "Margin"},
                        {"account_no": "1000002", "label": "Cash"},
                    ],
                    "sbl": {
                        "source": "LIVE",
                        "lots": [
                            {"symbol": "ORI", "remaining_qty": 43800, "borrow_fee_rate": 3.5, "lot_id": "SBL-ORI-1"},
                            {"symbol": "TPIPP", "remaining_qty": 23900, "borrow_fee_rate": 4.0, "lot_id": "SBL-TPIPP-1"},
                        ],
                    },
                    "brokers": {
                        "kss": {
                            "available": True,
                            "account_no": "1000001",
                            "account_type": "CREDIT_BALANCE",
                            "line_available": 250000.0,
                            "cash_balance": 120000.0,
                            "rows": [],
                            "borrowed": [
                                {
                                    "symbol": "ORI",
                                    "position_type": "BORROWED",
                                    "borrow_qty": 43800,
                                    "returnable_qty": 43800,
                                    "covered_from_short": True,
                                    "borrow_fee_rate": 3.5,
                                    "broker": "kss",
                                    "account_no": "1000001",
                                },
                                {
                                    "symbol": "TPIPP",
                                    "position_type": "BORROWED",
                                    "borrow_qty": 23900,
                                    "returnable_qty": 23900,
                                    "covered_from_short": False,
                                    "borrow_fee_rate": 4.0,
                                    "broker": "kss",
                                    "account_no": "1000001",
                                },
                            ],
                        },
                        "lib": {
                            "available": True,
                            "account_no": "9000001",
                            "account_type": "CASH_BALANCE",
                            "line_available": 180000.0,
                            "cash_balance": 180000.0,
                            "rows": [],
                            "borrowed": [],
                        },
                    },
                })

            if path == "/api/v1/miniapp/orders":
                if custom_state["orders_delay_s"] > 0:
                    time.sleep(custom_state["orders_delay_s"])
                counters["orders_sync_calls"] += 1
                matched = bool(custom_state["order_matched"])
                order_obj = (
                    {
                        "broker": "kss",
                        "order_no": "ORD-8899",
                        "symbol": "PTTEP",
                        "side": "COVER",
                        "volume": 300,
                        "matched_volume": 300,
                        "balance": 0,
                        "price": 145.5,
                        "kind": "matched",
                        "status": "Matched(M)",
                        "cancellable": False,
                    }
                    if matched
                    else {
                        "broker": "kss",
                        "order_no": "ORD-8899",
                        "symbol": "PTTEP",
                        "side": "COVER",
                        "volume": 300,
                        "matched_volume": 0,
                        "balance": 300,
                        "price": 145.5,
                        "kind": "open",
                        "status": "Queuing(O)",
                        "cancellable": True,
                    }
                )
                return self._json(200, {
                    "orders": [order_obj],
                    "counts": {
                        "open": 0 if matched else 1,
                        "matched": 1 if matched else 0,
                        "total": 1,
                    },
                    "brokers": {
                        "kss": {"available": True},
                        "lib": {"available": True},
                    },
                })

            if path == "/api/v1/miniapp/trade/hub-summary":
                st = getattr(self.server, "extra_state", {})
                return self._json(200, {
                    "daily_date": "2026-11-17",
                    "now_bkk": "14:30:00",
                    "dry_run": False,
                    "kill_switch": {"is_active": False},
                    "auto_exit": {"items": []},
                    "suggest_entry": {
                        "ledger_available": True,
                        "budget_per_slot_thb": custom_state["budget_thb"],
                        "remaining_long": 1,
                        "remaining_short": 1,
                        "remaining_total": 2,
                        "is_hold_cash": False,
                        "candidate_sides": {"ORI": "SHORT", "MINT": "LONG"},
                        "candidates": [],
                    },
                })

            return super().do_GET()

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/api/v1/miniapp/auth/session":
                n = int(self.headers.get("Content-Length") or "0")
                if n > 0:
                    self.rfile.read(n)
                custom_state["auth_seq"] += 1
                counters["auth_session_calls"] += 1
                tok = f"hl_mock_{custom_state['auth_seq']}"
                return self._json(200, {
                    "ok": True,
                    "session_token": tok,
                    "expires_at": int(time.time()) + 3600,
                    "ttl_seconds": 3600,
                    "user_id": "U_TEST",
                })

            if path == "/api/v1/miniapp/inbox/clear":
                n = int(self.headers.get("Content-Length") or "0")
                if n > 0:
                    self.rfile.read(n)
                counters["inbox_clear_calls"] += 1
                cleared = len(server.extra_state.get("inbox_items", []))
                server.extra_state["inbox_items"] = []
                server.extra_state["inbox_unread"] = 0
                return self._json(200, {
                    "ok": True,
                    "cleared": cleared,
                    "unread_count": 0,
                    "archived_file": "logs/notifications_archive_2026_11.jsonl",
                })

            return super().do_POST()

    server.RequestHandlerClass = All7Handler
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        async with async_playwright() as pw:
            launch_kw = {"headless": True}
            if os.path.exists(settings.PLAYWRIGHT_CHROME_EXECUTABLE):
                launch_kw["executable_path"] = settings.PLAYWRIGHT_CHROME_EXECUTABLE
            browser = await pw.chromium.launch(**launch_kw)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.route(
                "https://static.line-scdn.net/**",
                lambda route: route.fulfill(
                    status=200,
                    content_type="application/javascript",
                    body="window.liff = { init: async () => {}, isLoggedIn: () => true, getIDToken: () => 'fake_liff_jwt', login: () => {} };",
                ),
            )
            page_errors = []
            page.on("pageerror", lambda err: page_errors.append(str(err)))

            # Card 7 + Card 5: Boot on Schedule with LIFF->session token exchange + 401 auto-refresh + w6_post_close
            await page.goto(f"http://127.0.0.1:{port}/miniapp/?tab=nego", wait_until="networkidle")
            await page.wait_for_selector("#schedule-timeline-card", timeout=5000)
            assert counters["auth_session_calls"] >= 1
            assert "10:00 – 17:30" in (await page.inner_text("#schedule-timeline-card"))
            w6_text = await page.inner_text("[data-window-row='w6_post_close']")
            assert "16:30–17:30" in w6_text
            assert "POST-CLOSE" in w6_text
            eod_row_text = await page.inner_text("[data-timeline-row='eod_summary']")
            assert "17:05" in eod_row_text
            assert "Net Equity" in eod_row_text
            assert "634,428" in eod_row_text
            pipe_row_text = await page.inner_text("[data-timeline-row='daily_pipeline']")
            assert "17:30" in pipe_row_text
            assert "Daily Pipeline" in pipe_row_text
            assert "OK" in pipe_row_text

            # Simulate expired session token hl_mock_1 -> 401 recovery -> sync DONE @ 10:07
            custom_state["force_401_on_token"] = "hl_mock_1"
            custom_state["runbook_done"] = True
            await page.click("#nego-reload-btn")
            await page.wait_for_function(
                "() => (document.querySelector(\"[data-timeline-row='runbook_exit']\")?.innerText || '').includes('10:07')",
                timeout=5000,
            )
            assert counters["schedule_401_sent"] == 1
            assert counters["auth_session_calls"] >= 2

            # Card 4 + Card 6: Portfolio hides covered ORI(S), tags ORI(B) Covered + TPIPP(B) fresh SBL, and locks #port-sync-btn
            await page.click(".tab-btn[data-tab='portfolio']")
            await page.wait_for_selector("[data-sym='ORI']", timeout=5000)
            assert await page.locator("[data-sym='ORI'][data-ptype='SHORT']").count() == 0
            ori_text = await page.inner_text("[data-sym='ORI']")
            assert "Covered" in ori_text
            assert "รอคืน SBL" in ori_text
            assert await page.locator("[data-sym='ORI'] [data-sbl-return]").count() >= 1
            tpipp_text = await page.inner_text("[data-sym='TPIPP']")
            assert "SBL (B)" in tpipp_text
            assert await page.locator("[data-sym='TPIPP'] .act-short").count() >= 1

            custom_state["port_delay_s"] = 0.45
            await page.click("#port-sync-btn")
            await page.wait_for_selector("#port-sync-btn.is-spinning[disabled]", timeout=2000)
            await page.wait_for_selector("#port-sync-btn:not([disabled])", timeout=5000)
            assert counters["port_sync_calls"] >= 2
            custom_state["port_delay_s"] = 0.0

            # Card 3: Orders sync spin/lock, Queuing(O)->Matched(M), and bottom nav race guard
            await page.click(".tab-btn[data-tab='orders']")
            await page.wait_for_selector("#orders-sync-btn", timeout=5000)
            assert await page.locator(".tab-btn[data-tab='orders'].active").count() == 1
            assert "Queuing(O)" in (await page.inner_text("#main-content"))

            custom_state["orders_delay_s"] = 0.45
            custom_state["order_matched"] = True
            await page.click("#orders-sync-btn")
            await page.wait_for_selector("#orders-sync-btn.is-loading[disabled]", timeout=2000)
            assert "กำลังซิงค์..." in (await page.inner_text("#orders-sync-btn"))
            await page.wait_for_selector("#orders-sync-btn:not([disabled])", timeout=5000)
            assert "อัปเดตล่าสุด" in (await page.inner_text("#main-content"))
            await page.click("[data-sfilter='matched']")
            assert "Matched(M)" in (await page.inner_text("#main-content"))

            # Async race test: slow Orders -> immediate Schedule switch must keep Schedule active
            custom_state["orders_delay_s"] = 0.55
            await page.click(".tab-btn[data-tab='orders']")
            await page.wait_for_timeout(50)
            await page.click(".tab-btn[data-tab='nego']")
            await page.wait_for_timeout(750)
            assert await page.locator(".tab-btn[data-tab='nego'].active").count() == 1
            assert await page.locator(".tab-btn[data-tab='orders'].active").count() == 0
            assert await page.locator("#schedule-timeline-card").count() == 1
            custom_state["orders_delay_s"] = 0.0

            # Card 2: Trade hub SSOT (50k -> 1x/1.5x/2x), SBL return auto-fill, Quick Order dynamic label & hint
            await page.click(".tab-btn[data-tab='trade']")
            await page.wait_for_function(
                "() => document.querySelector('#se-budget-input')?.value === '50000'",
                timeout=5000,
            )
            await page.wait_for_selector("#tr-manual-advance-toggle", timeout=5000)
            await page.click("#tr-manual-advance-toggle")
            await page.wait_for_selector("#tr-advance-toggle", timeout=5000)
            await page.click("#tr-advance-toggle")

            await page.click("[data-amode='value']")
            assert (await page.input_value("#tr-budget")) == "50000"
            assert "1x (50,000 ฿)" in (await page.inner_text("[data-set-budget='50000']"))
            assert "1.5x (75,000 ฿)" in (await page.inner_text("[data-set-budget='75000']"))
            assert "2x (100,000 ฿)" in (await page.inner_text("[data-set-budget='100000']"))

            await page.click("[data-subtab='sbl']")
            await page.click("[data-sblact='return']")
            await page.wait_for_selector("[data-sbl-symchip='ORI']", timeout=5000)
            assert (await page.input_value("#sbl-vol")) == ""
            await page.click("[data-sbl-symchip='ORI']")
            assert (await page.input_value("#sbl-vol")) == "43800"

            await page.click("[data-subtab='quick']")
            assert (await page.input_value("#quick-budget-input")) == "50000"
            assert "50k (50,000 ฿)" in (await page.inner_text("[data-qbudget='50000']"))
            assert "100k (100,000 ฿)" in (await page.inner_text("[data-qbudget='100000']"))
            assert "150k (150,000 ฿)" in (await page.inner_text("[data-qbudget='150000']"))
            await page.fill("#quick-sym-input", "MINT")
            await page.wait_for_selector("#quick-suggest-hint", state="visible", timeout=3000)
            assert "LONG" in (await page.inner_text("#quick-suggest-hint"))
            await page.fill("#quick-sym-input", "ORI")
            assert "SHORT" in (await page.inner_text("#quick-suggest-hint"))
            assert "ยืม SBL เข้า (B)" in (await page.inner_text("#quick-borrow-btn"))
            await page.check("#quick-auto-short")
            assert "ยืม SBL + Short ทันที" in (await page.inner_text("#quick-borrow-btn"))
            await page.uncheck("#quick-auto-short")
            assert "ยืม SBL เข้า (B)" in (await page.inner_text("#quick-borrow-btn"))

            # Card 1: Inbox hides gateway heartbeat in 'ทั้งหมด', shows ×3 in 'EOD/ระบบ', and clears via /inbox/clear
            await page.click("#top-inbox-btn")
            await page.wait_for_selector("#inbox-drawer:not(.hidden)", timeout=5000)
            await page.wait_for_function(
                "() => (document.querySelector('#inbox-list')?.innerText || '').includes('Filled ORI')",
                timeout=5000,
            )
            all_inbox_txt = await page.inner_text("#inbox-list")
            assert "Filled ORI" in all_inbox_txt
            assert "Gateway ออนไลน์" not in all_inbox_txt

            await page.click("#inbox-filters [data-ifilter='eod']")
            eod_inbox_txt = await page.inner_text("#inbox-list")
            assert "Gateway ออนไลน์" in eod_inbox_txt
            assert "×3" in eod_inbox_txt

            await page.click("#inbox-filters [data-ifilter='all']")
            await page.click("#inbox-clear-btn")
            await page.wait_for_function(
                "() => (document.querySelector('#inbox-list')?.innerText || '').includes('ยังไม่มีข้อความแจ้งเตือน')",
                timeout=5000,
            )
            assert counters["inbox_clear_calls"] == 1
            assert page_errors == [], f"Unexpected browser JS errors: {page_errors}"
            await browser.close()
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.asyncio
async def test_miniapp_triage_fixes_brokers_dropdown_inbox_icons_net_equity_and_recommend_size():
    """Verify all 3 Trello Triage fixes in Chromium:
    1) Brokers tab <select> stays mounted during 3s polls while #brokers-otp-card still updates in-place.
    2) Inbox drawer header uses SVG icon buttons + separated filter row + LINE quota reset label.
    3) Port tab shows Net Equity across 3 accounts (excl. Margin line/BORROWED) and Trade tab offers Recommend Size.
    """
    from playwright.async_api import async_playwright

    server, counters = _make_mock_miniapp_server()
    server.extra_state["pending_otp"] = None
    server.extra_state["portfolio_override"] = {
        "mode": {"is_live": False, "label": "DRY-RUN"},
        "dry_run": True,
        "accounts": [
            {
                "key": "kss_margin",
                "broker": "kss",
                "label": "Krungsri · Margin",
                "available": True,
                "account_no": "1000001",
                "account_type": "CREDIT_BALANCE",
                "cash_balance": 200000.0,
                "line_available": 900000.0,
                "net_equity": 260000.0,
                "unrealized_pnl": 5000.0,
                "rows": [
                    {"symbol": "AOT", "broker": "kss", "account_no": "1000001", "volume": 1000, "cost": 95.0, "last": 100.0, "market_value": 100000.0, "unrealized_pnl": 5000.0, "pnl_pct": 5.26, "position_type": "LONG"},
                    {"symbol": "ORI", "broker": "kss", "account_no": "1000001", "volume": 10000, "cost": 4.0, "last": 4.0, "market_value": 40000.0, "unrealized_pnl": 0.0, "pnl_pct": 0.0, "position_type": "SHORT"},
                ],
            },
            {
                "key": "kss_cash",
                "broker": "kss",
                "label": "Krungsri · Cash",
                "available": True,
                "account_no": "1000002",
                "account_type": "CASH_BALANCE",
                "cash_balance": 100000.0,
                "line_available": 100000.0,
                "net_equity": 100000.0,
                "unrealized_pnl": 0.0,
                "rows": [],
            },
            {
                "key": "lib_cash",
                "broker": "lib",
                "label": "Liberator · Cash",
                "available": True,
                "account_no": "9000001",
                "account_type": "CASH_BALANCE",
                "cash_balance": 150000.0,
                "line_available": 150000.0,
                "net_equity": 200000.0,
                "unrealized_pnl": 2000.0,
                "rows": [
                    {"symbol": "PTT", "broker": "lib", "account_no": "9000001", "volume": 1000, "cost": 48.0, "last": 50.0, "market_value": 50000.0, "unrealized_pnl": 2000.0, "pnl_pct": 4.17, "position_type": "LONG"},
                ],
            },
        ],
        "brokers": {
            "kss": {
                "available": True,
                "account_no": "1000001",
                "account_type": "CREDIT_BALANCE",
                "cash_balance": 200000.0,
                "line_available": 900000.0,
                "net_equity": 260000.0,
                "rows": [],
                "borrowed": [{"symbol": "AAV", "broker": "kss", "volume": 50000, "last": 4.0, "borrow_qty": 50000, "returnable_qty": 50000}],
            },
            "lib": {
                "available": True,
                "account_no": "9000001",
                "account_type": "CASH_BALANCE",
                "cash_balance": 150000.0,
                "line_available": 150000.0,
                "net_equity": 200000.0,
                "rows": [],
            },
        },
        "summary": {
            "available_count": 2,
            "accounts_count": 3,
            "partial": False,
            "net_equity_complete": True,
            "total_net_equity": 560000.0,
            "total_cash_balance": 450000.0,
            "total_line_available": 1150000.0,
            "total_unrealized_pnl": 7000.0,
        },
        "sbl": {"source": "LIVE", "lots": []},
    }
    server.extra_state["sizing_reference_override"] = {
        "margin_multiplier": 1.0,
        "house_money_multiplier": 1.0,
        "net_equity_thb": 560000.0,
        "recommended_slots": 7,
        "recommended_budget_thb": 80000,
    }

    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        async with async_playwright() as pw:
            launch_kw = {"headless": True}
            if os.path.exists(settings.PLAYWRIGHT_CHROME_EXECUTABLE):
                launch_kw["executable_path"] = settings.PLAYWRIGHT_CHROME_EXECUTABLE
            browser = await pw.chromium.launch(**launch_kw)
            page = await browser.new_page(viewport={"width": 390, "height": 844})
            await page.route("https://static.line-scdn.net/**", lambda route: route.fulfill(status=200, content_type="application/javascript", body=""))
            page_errors = []
            page.on("pageerror", lambda err: page_errors.append(str(err)))

            await page.goto(f"http://127.0.0.1:{port}/miniapp/?tab=brokers", wait_until="networkidle")
            await page.wait_for_selector("#brokers-kss-acct", timeout=5000)

            # 1) Focus <select> on Brokers tab and wait across >3.4s (past 3000ms pollTimer)
            await page.evaluate("() => { window.__kssSelRef = document.querySelector('#brokers-kss-acct'); window.__kssSelRef.focus(); }")
            await page.wait_for_timeout(3400)
            same_node = await page.evaluate("() => window.__kssSelRef === document.querySelector('#brokers-kss-acct')")
            assert same_node is True, "Brokers <select> was destroyed and recreated during 3s poll!"

            # Even while <select> is focused, an incoming OTP still mounts in #brokers-otp-card and resolves cleanly
            server.extra_state["pending_otp"] = {"broker": "KRUNGSRI", "broker_label": "Krungsri", "ref": "T123", "expires_in_seconds": 120, "submitted": False}
            await page.wait_for_selector("#brokers-otp-card:not(.hidden) #card-otp-digit-0", timeout=5000)
            assert await page.evaluate("() => window.__kssSelRef === document.querySelector('#brokers-kss-acct')") is True
            server.extra_state["pending_otp"] = None
            await page.locator("#brokers-otp-card").wait_for(state="hidden", timeout=5000)

            # 2) Inbox drawer header uses SVG icon buttons + separated filter row + LINE quota reset label
            await page.click("#top-inbox-btn")
            await page.wait_for_selector("#inbox-drawer:not(.hidden)", timeout=5000)
            assert await page.locator(".inbox-head-actions #inbox-mark-all-btn svg").count() == 1
            assert await page.locator(".inbox-head-actions #inbox-export-btn svg").count() == 1
            assert await page.locator(".inbox-head-actions #inbox-clear-btn svg").count() == 1
            assert "รีเซ็ต" in (await page.inner_text("#inbox-quota-summary"))
            await page.click("#inbox-close-btn")
            await page.locator("#inbox-drawer").wait_for(state="hidden", timeout=5000)

            # 2.5) Brokers Advance panel exposes split Open/Nego uptick + auto -10% (without manual symbol box)
            await page.click("#brokers-sys-adv-toggle")
            await page.wait_for_selector("#rule-open-uptick-select", timeout=3000)
            assert await page.locator("#rule-uptick-select").count() == 1
            assert await page.locator("#rule-auto-minus10").count() == 1
            assert await page.locator("#rule-strict-symbols").count() == 0

            # 3) Port tab shows Net Equity 560,000.00 + EOD chart card (7D/1M/3M only, NO ALL) & interactive points
            await page.click(".tab-btn[data-tab='portfolio']")
            await page.wait_for_selector("#port-net-equity-val", timeout=5000)
            assert "560,000.00" in (await page.inner_text("#port-net-equity-val"))
            await page.wait_for_selector("#port-eod-chart-card svg polyline", timeout=5000)
            assert await page.locator("[data-chart-range='7D']").count() == 1
            assert await page.locator("[data-chart-range='1M']").count() == 1
            assert await page.locator("[data-chart-range='3M']").count() == 1
            assert await page.locator("[data-chart-range='ALL']").count() == 0
            await page.locator("[data-eq-idx='0']").click()
            assert "2026-11-14" in (await page.inner_text("#port-eod-point-detail"))
            await page.click("#port-toggle-details-btn")
            await page.wait_for_selector(".port-summary-drawer", timeout=3000)
            brk_txt = await page.inner_text(".port-summary-drawer")
            assert "1000001" in brk_txt and "1000002" in brk_txt and "9000001" in brk_txt

            # 4) Trade tab removes 50k/100k/150k/200k presets from Suggest Entry, keeping Recommend + manual input,
            #    while Quick Order keeps 50k / 100k / 150k untouched
            await page.click(".tab-btn[data-tab='trade']")
            await page.wait_for_selector("#se-apply-recommend-btn", timeout=5000)
            assert await page.locator("[data-se-budget-preset]").count() == 0
            first_chip_id = await page.evaluate("() => document.querySelector('.se-budget-chips')?.firstElementChild?.id || ''")
            assert first_chip_id == "se-apply-recommend-btn"
            assert "80k" in (await page.inner_text("#se-apply-recommend-btn"))
            assert (await page.input_value("#se-budget-input")) == "80000"
            await page.fill("#se-budget-input", "50000")
            async with page.expect_response("**/api/v1/miniapp/suggest-entry/config"):
                await page.click("#se-budget-save-btn")
            assert (await page.input_value("#se-budget-input")) == "50000"
            async with page.expect_response("**/api/v1/miniapp/suggest-entry/config"):
                await page.click("#se-apply-recommend-btn")
            assert (await page.input_value("#se-budget-input")) == "80000"
            await page.click("#tr-manual-advance-toggle")
            await page.wait_for_selector("[data-subtab='quick']", timeout=5000)
            await page.click("[data-subtab='quick']")
            assert await page.locator("[data-qbudget='50000']").count() == 1
            assert await page.locator("[data-qbudget='100000']").count() == 1
            assert await page.locator("[data-qbudget='150000']").count() == 1

            assert page_errors == [], f"Unexpected browser JS errors: {page_errors}"
            await browser.close()
    finally:
        server.shutdown()
        server.server_close()
