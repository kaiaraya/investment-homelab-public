"""Pure-CDP Runbook Daily Auto-Exit Bridge & Random-Time Scheduler (Zero-DB).

Reads daily ``positions_ledger.json`` produced by ``trading-runbook`` (filtering
strictly to ``segment == 'managed'``, ``broker in ('KSS', 'LIBERATOR')``, and
``action in ('SELL', 'COVER')``), verifies market calendar + system readiness,
reconciles against live broker holdings and same-day orders via ``trading_facade``
(Safe Net-Remaining Guard), and supports two complementary workflows:
  1. Schedule Auto-Daily (default ON): automatically schedules a random minute
     within ``10:01-10:28`` on SET trading days (when Trade has not already
     acted today), performs an All-or-Nothing readiness check at trigger time,
     and submits Lure Exit orders (``Offer1`` for ``SELL``, ``Bid1`` for ``COVER``)
     without requiring manual confirmation. Also provides a read-only Preview
     & System Readiness drawer on the Schedule page.
  2. Trade Step-by-Step: interactive preview, checkbox filtering, explicit
     ``allow_stale`` override, and confirmation sheet from the Trade tab.

Maintains atomic JSON state in ``data/runbook_exit_state.json`` and preserves
monthly LINE push quota during normal runs (``notify_line=False``), sending
LINE alerts only on deduped critical/blocking errors.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, time
import json
import logging
import os
from pathlib import Path
import random
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.services.broker_cdp import trading_facade
from app.services.kill_switch_service import kill_switch_service
from app.services.nego_scheduler import (
    NegoWindowSpec,
    _now_bkk,
    _pick_random_minute,
    _to_minutes,
    format_error_line_status,
    notify_critical_error_once,
    probe_ready_brokers,
)
from app.services.otp_relay_readiness import otp_relay_readiness
from app.services import pin_guard
from app.services.trade_dispatcher import trade_dispatcher
from app.services.validator import validator

logger = logging.getLogger(__name__)
_BKK = ZoneInfo("Asia/Bangkok")
RUNBOOK_EXIT_LAST_TICK_OFFSET = 4

_BROKER_ROUTE: Dict[str, Tuple[str, str]] = {
    "KSS": ("kss", "margin"),
    "LIBERATOR": ("lib", "cash"),
}
_VALID_ACTIONS = {"SELL", "COVER"}
_ALL_BROKERS: Tuple[str, ...] = ("kss", "lib")
_RERUNNABLE_STATUSES = {
    "PENDING",
    "AUTO_PENDING",
    "BLOCKED_NOT_READY",
    "BLOCKED_KILL_SWITCH",
    "SKIPPED_STALE_RUNBOOK",
    "MISSED_WINDOW",
}

EXIT_WINDOW_OPEN = NegoWindowSpec(
    id="runbook_open",
    start="10:00",
    end="10:30",
    default_enabled=True,
    mode="lure",
    allowed_sides=("SELL", "COVER"),
    tradable=True,
    label="เปิดตลาดเช้า (Runbook Auto-Exit)",
    description="สุ่มเวลา 10:00–10:30 อ่าน positions_ledger.json ยิง Exit อัตโนมัติ (last ± 4 ช่อง)",
)

EXIT_WINDOW_AFTERNOON = NegoWindowSpec(
    id="runbook_afternoon",
    start="14:00",
    end="14:15",
    default_enabled=True,
    mode="lure",
    allowed_sides=("SELL", "COVER"),
    tradable=True,
    label="เปิดตลาดบ่าย (Runbook Auto-Exit)",
    description="สุ่มเวลา 14:00–14:15 ยิงคำสั่ง Exit รอบบ่าย",
)


class RunbookExitError(Exception):
    """Structured error mapped cleanly to HTTPException by MiniApp endpoints."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        extra: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}


def classify_market_phase(
    now: Optional[datetime] = None,
    *,
    holiday_name: Optional[str] = None,
) -> str:
    """Return SET session phase in Asia/Bangkok local time."""
    cur = _now_bkk(now)
    if cur.weekday() >= 5:
        return "WEEKEND"
    if holiday_name:
        return "HOLIDAY"
    t = cur.time()
    if t < time(10, 0):
        return "PRE_MARKET"
    if t < time(12, 30):
        return "MARKET_OPEN_AM"
    if t < time(14, 0):
        return "LUNCH_BREAK"
    if t < time(16, 30):
        return "MARKET_OPEN_PM"
    return "AFTER_CLOSE"


def _is_dry_mode() -> bool:
    return (not bool(trade_dispatcher.is_live)) or bool(getattr(settings, "DRY_RUN_MODE", False))


def _empty_counts(total: int = 0) -> Dict[str, int]:
    return {
        "total": total,
        "executed": 0,
        "adjusted": 0,
        "skipped": 0,
        "failed": 0,
    }


def compute_runbook_exit_price(
    side: str,
    last: Any,
    fallback_lure_price: Any,
) -> Tuple[Optional[float], str]:
    """Compute Runbook Exit target price at ``last ± 4`` SET ticks (SELL +4, COVER -4).

    Falls back to ``fallback_lure_price`` (Offer1/Bid1) when ``last`` is unavailable.
    """
    clean_side = (side or "").strip().upper()
    try:
        last_px = float(last) if last is not None else None
    except (TypeError, ValueError):
        last_px = None
    if last_px is not None and last_px > 0 and clean_side in _VALID_ACTIONS:
        steps = RUNBOOK_EXIT_LAST_TICK_OFFSET if clean_side == "SELL" else -RUNBOOK_EXIT_LAST_TICK_OFFSET
        return validator.get_next_tick(last_px, steps=steps), f"last{'+4' if steps > 0 else '-4'}t"

    try:
        fb_px = float(fallback_lure_price) if fallback_lure_price is not None else None
    except (TypeError, ValueError):
        fb_px = None
    if fb_px is not None and fb_px > 0:
        return fb_px, "lure_fallback"
    return None, "missing"


def _default_auto_enabled() -> bool:
    return bool(getattr(settings, "RUNBOOK_AUTO_EXIT_DEFAULT_ENABLED", True))


class RunbookExitService:
    """Zero-DB Pure-CDP Runbook Exit planner, readiness gate, and auto-scheduler."""

    def __init__(
        self,
        *,
        ledger_path: Optional[str] = None,
        state_path: Optional[str] = None,
        holidays_path: Optional[str] = None,
        check_interval: Optional[int] = None,
        clock_fn: Optional[Callable[[], datetime]] = None,
        rng: Optional[random.Random] = None,
    ):
        self._ledger_path_override = ledger_path
        self._state_path_override = state_path
        self._holidays_path_override = holidays_path
        self._check_interval_override = check_interval
        self._clock_fn = clock_fn or (lambda: datetime.now(_BKK))
        self._rng = rng or random.Random()

        self._exec_lock = asyncio.Lock()
        self._state: Optional[Dict[str, Any]] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_tick_log_key: Optional[str] = None

    @property
    def ledger_path(self) -> Path:
        raw = self._ledger_path_override or getattr(
            settings,
            "RUNBOOK_LEDGER_PATH",
            "/srv/antigravity/shadow/InvestorPerformance/data/positions_ledger.json",
        )
        return Path(raw)

    @property
    def state_path(self) -> Path:
        raw = self._state_path_override or getattr(
            settings, "RUNBOOK_EXIT_STATE_PATH", "data/runbook_exit_state.json"
        )
        return Path(raw)

    @property
    def holidays_path(self) -> Path:
        raw = self._holidays_path_override or getattr(
            settings,
            "SET_HOLIDAYS_PATH",
            "/home/araya-sawasdichai/trading-runbook/data/reference/holidays/holidays_index.json",
        )
        return Path(raw)

    @property
    def check_interval(self) -> int:
        if self._check_interval_override is not None:
            return max(1, int(self._check_interval_override))
        return max(5, int(getattr(settings, "RUNBOOK_EXIT_CHECK_INTERVAL_SEC", 20) or 20))

    def _now(self) -> datetime:
        return _now_bkk(self._clock_fn())

    # ------------------------------------------------------------------
    # SET Holiday Calendar
    # ------------------------------------------------------------------

    def _read_holiday_info(self, today_str: str) -> Tuple[bool, Optional[str], Optional[str]]:
        """Check whether ``today_str`` is a SET holiday in holidays_index.json."""
        path = self.holidays_path
        if not path.is_file():
            return False, None, f"ไม่พบไฟล์วันหยุด SET: {path}"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return False, None, f"อ่านไฟล์วันหยุด SET ไม่ได้: {exc}"
        dates = raw.get("dates") if isinstance(raw, dict) else None
        if not isinstance(dates, dict):
            return False, None, "รูปแบบ holidays_index.json ไม่ถูกต้อง"
        name = dates.get(today_str)
        if name:
            return True, str(name), None
        return False, None, None

    # ------------------------------------------------------------------
    # State Persistence & Auto-Daily Provisioning
    # ------------------------------------------------------------------

    @staticmethod
    def _default_state(now_bkk: datetime, *, auto_enabled: bool) -> Dict[str, Any]:
        return {
            "schema_version": 2,
            "auto_enabled": bool(auto_enabled),
            "daily_date": now_bkk.strftime("%Y-%m-%d"),
            "runbook_as_of": None,
            "statement_through": None,
            "queue": None,
            "history": [],
            "sent_error_keys": [],
            "manual_acted_keys": [],
        }

    def _save_state(self) -> None:
        if self._state is None:
            return
        path = self.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _build_auto_queue_payload(self, now_bkk: datetime, sched_time: Optional[str]) -> Dict[str, Any]:
        now_m = now_bkk.hour * 60 + now_bkk.minute
        missed = sched_time is None or now_m >= _to_minutes(EXIT_WINDOW_OPEN.end)
        return {
            "status": "MISSED_WINDOW" if missed else "AUTO_PENDING",
            "queue_kind": "auto_daily",
            "allow_stale": False,
            "window_id": EXIT_WINDOW_OPEN.id,
            "window_start": EXIT_WINDOW_OPEN.start,
            "window_end": EXIT_WINDOW_OPEN.end,
            "scheduled_time": sched_time,
            "queued_at": now_bkk.isoformat(timespec="seconds"),
            "trigger_source": "schedule_auto",
            "started_at": None,
            "finished_at": now_bkk.strftime("%H:%M:%S") if missed else None,
            "dry_run": _is_dry_mode(),
            "counts": _empty_counts(0),
            "items": [],
            "broker_errors": {},
            "readiness_failures": [],
            "error": (
                f"เลยช่วง Auto ({EXIT_WINDOW_OPEN.start}–{EXIT_WINDOW_OPEN.end}) แล้ว — ไม่ยิงย้อนหลังอัตโนมัติ"
                if missed
                else None
            ),
            "line_status": "งดส่ง LINE (ดูผลใน Schedule)",
        }

    def _expire_missed_queue(self, state: Dict[str, Any], now_bkk: datetime) -> bool:
        queue = state.get("queue")
        if not isinstance(queue, dict) or queue.get("status") not in ("PENDING", "AUTO_PENDING"):
            return False
        end_str = queue.get("window_end") or EXIT_WINDOW_OPEN.end
        today_str = now_bkk.strftime("%Y-%m-%d")
        is_holiday, holiday_name, _ = self._read_holiday_info(today_str)
        now_m = now_bkk.hour * 60 + now_bkk.minute
        if now_bkk.weekday() >= 5 or is_holiday:
            queue["status"] = "MARKET_CLOSED"
            queue["finished_at"] = now_bkk.strftime("%H:%M:%S")
            queue["error"] = f"วันหยุดตลาด SET ({holiday_name or 'เสาร์–อาทิตย์'})"
            queue["line_status"] = "งดส่ง LINE"
            for it in queue.get("items") or []:
                if it.get("outcome") in ("PENDING", "AUTO_PENDING"):
                    it["outcome"] = "MARKET_CLOSED"
                    it["reason"] = queue["error"]
                    it["updated_at"] = now_bkk.isoformat(timespec="seconds")
            return True
        if now_m >= _to_minutes(end_str):
            queue["status"] = "MISSED_WINDOW"
            queue["finished_at"] = now_bkk.strftime("%H:%M:%S")
            queue["error"] = f"เลยช่วงเวลาคิว ({queue.get('window_start')}-{end_str}) แล้ว — ไม่ยิงย้อนหลัง"
            queue["line_status"] = "งดส่ง LINE"
            for it in queue.get("items") or []:
                if it.get("outcome") in ("PENDING", "AUTO_PENDING"):
                    it["outcome"] = "MISSED_WINDOW"
                    it["reason"] = queue["error"]
                    it["updated_at"] = now_bkk.isoformat(timespec="seconds")
            return True
        return False

    def _ensure_daily_auto_queue(self, state: Dict[str, Any], now_bkk: datetime) -> bool:
        """Provision daily 10:01-10:28 Auto-Exit slot when Auto is ON and Trade has not acted."""
        if not state.get("auto_enabled"):
            return False
        if state.get("queue") is not None or state.get("history"):
            return False

        today_str = now_bkk.strftime("%Y-%m-%d")
        is_holiday, _, _ = self._read_holiday_info(today_str)
        if now_bkk.weekday() >= 5 or is_holiday:
            return False

        now_m = now_bkk.hour * 60 + now_bkk.minute
        sched_time = (
            _pick_random_minute(EXIT_WINDOW_OPEN, now_mins=now_m, rng=self._rng)
            if now_m < _to_minutes(EXIT_WINDOW_OPEN.end)
            else None
        )
        state["queue"] = self._build_auto_queue_payload(now_bkk, sched_time)
        logger.info(
            "Provisioned daily Runbook Auto-Exit slot date=%s status=%s scheduled=%s",
            today_str,
            state["queue"].get("status"),
            sched_time,
        )
        return True

    def _log_tick_once(self, key: str, message: str, *args: Any) -> None:
        if self._last_tick_log_key != key:
            self._last_tick_log_key = key
            logger.info(message, *args)

    def record_manual_trade(
        self,
        *,
        broker: str,
        symbol: str,
        side: str,
        now: Optional[datetime] = None,
    ) -> bool:
        """Record a manual SELL/COVER order placed today so Auto-Exit will not double-send."""
        clean_side = (side or "").strip().lower()
        clean_sym = (symbol or "").strip().upper()
        b_raw = (broker or "").strip().lower()
        clean_broker = "kss" if b_raw in ("kss", "krungsri", "trademan") else ("lib" if b_raw in ("lib", "liberator") else "")
        if clean_side not in ("sell", "cover") or not clean_broker or not clean_sym:
            return False

        # Never let ad-hoc pytest calls without a custom state path mutate production state.
        if (
            "PYTEST_CURRENT_TEST" in os.environ
            and self._state_path_override is None
            and getattr(settings, "RUNBOOK_EXIT_STATE_PATH", "data/runbook_exit_state.json") == "data/runbook_exit_state.json"
        ):
            return False

        cur = _now_bkk(now) if now is not None else self._now()
        state = self._load_or_init_state(cur)
        acted = state.setdefault("manual_acted_keys", [])
        acted_key = f"{clean_broker}:{clean_sym}:{clean_side}"
        if acted_key not in acted:
            acted.append(acted_key)
            self._save_state()
            logger.info(
                "Recorded manual %s for %s (%s) — Runbook Auto-Exit will skip duplicate today",
                clean_side.upper(),
                clean_sym,
                clean_broker.upper(),
            )
            return True
        return False

    def _load_or_init_state(self, now_bkk: Optional[datetime] = None) -> Dict[str, Any]:
        cur = _now_bkk(now_bkk) if now_bkk is not None else self._now()
        today_str = cur.strftime("%Y-%m-%d")
        dirty = False

        if self._state is None:
            path = self.state_path
            if path.exists():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        self._state = raw
                except Exception as exc:
                    logger.warning("Failed to read %s, reinitializing: %s", path, exc)
            if self._state is None:
                self._state = self._default_state(cur, auto_enabled=_default_auto_enabled())
                dirty = True

        state = self._state
        state.setdefault("schema_version", 2)
        if "auto_enabled" not in state:
            state["auto_enabled"] = _default_auto_enabled()
            dirty = True
        state.setdefault("history", [])
        state.setdefault("sent_error_keys", [])
        state.setdefault("manual_acted_keys", [])

        if state.get("daily_date") != today_str:
            preserved_auto = bool(state.get("auto_enabled", _default_auto_enabled()))
            self._state = self._default_state(cur, auto_enabled=preserved_auto)
            state = self._state
            dirty = True

        if self._expire_missed_queue(state, cur):
            dirty = True
        if self._ensure_daily_auto_queue(state, cur):
            dirty = True
        if dirty:
            self._save_state()

        return state

    # ------------------------------------------------------------------
    # Runbook Ledger Extraction & Safe Net-Remaining Guard
    # ------------------------------------------------------------------

    def _read_ledger_exits(self, now_bkk: datetime) -> Dict[str, Any]:
        path = self.ledger_path
        today_str = now_bkk.strftime("%Y-%m-%d")
        if not path.is_file():
            return {
                "ledger_available": False,
                "ledger_path": str(path),
                "error": f"ไม่พบไฟล์ Runbook: {path}",
                "as_of": None,
                "statement_through": None,
                "is_today": False,
                "items": [],
            }

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {
                "ledger_available": False,
                "ledger_path": str(path),
                "error": f"อ่านไฟล์ Runbook ไม่ได้: {exc}",
                "as_of": None,
                "statement_through": None,
                "is_today": False,
                "items": [],
            }

        as_of = str(raw.get("as_of") or "").strip() or None
        stmt_through = str(raw.get("statement_through") or "").strip() or None
        is_today = as_of == today_str

        # Aggregate by (broker, account, symbol, side) so split lots share one safe net check.
        grouped: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
        for row in raw.get("positions") or []:
            if not isinstance(row, dict):
                continue
            if str(row.get("segment") or "").strip().lower() != "managed":
                continue
            broker_raw = str(row.get("broker") or "").strip().upper()
            route = _BROKER_ROUTE.get(broker_raw)
            if route is None:
                continue
            action = str(row.get("action") or "").strip().upper()
            if action not in _VALID_ACTIONS:
                continue
            exit_reason = str(row.get("exit_reason") or "").strip()
            if exit_reason.upper() == "WEED_OUT_WATCHLIST":
                continue

            shares = int(row.get("shares") or row.get("volume") or 0)
            if shares <= 0:
                continue

            broker, account = route
            symbol = str(row.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            side = action.lower()
            key = (broker, account, symbol, side)

            existing = grouped.get(key)
            if existing is None:
                grouped[key] = {
                    "broker": broker,
                    "broker_label": "KSS Margin" if broker == "kss" else "LIB Cash",
                    "account": account,
                    "symbol": symbol,
                    "side": side,
                    "action": action,
                    "runbook_volume": shares,
                    "entry_price": row.get("entry_price"),
                    "current_price": row.get("current_price"),
                    "unrealized_pct": row.get("unrealized_pct"),
                    "atr_multiple": row.get("atr_multiple"),
                    "days_held": row.get("days_held"),
                    "exit_reason": exit_reason or action,
                    "recommended_action": row.get("recommended_action") or "",
                }
            else:
                existing["runbook_volume"] += shares
                if exit_reason and exit_reason not in existing["exit_reason"]:
                    existing["exit_reason"] = f"{existing['exit_reason']}, {exit_reason}"

        items = sorted(grouped.values(), key=lambda x: (x["broker"], x["symbol"], x["side"]))
        return {
            "ledger_available": True,
            "ledger_path": str(path),
            "error": None,
            "as_of": as_of,
            "statement_through": stmt_through,
            "is_today": is_today,
            "items": items,
        }

    @staticmethod
    def _reconcile_item(
        item: Dict[str, Any],
        port_snap: Optional[Dict[str, Any]],
        orders_snap: Optional[Dict[str, Any]],
        *,
        manual_acted_keys: Optional[Sequence[str]] = None,
    ) -> Dict[str, Any]:
        """Apply Safe Net-Remaining Guard against live holdings and orders."""
        broker = item["broker"]
        symbol = item["symbol"]
        side = item["side"]
        runbook_vol = int(item["runbook_volume"])
        expected_side = "SELL" if side == "sell" else "COVER"
        expected_ptype = "LONG" if side == "sell" else "SHORT"

        if manual_acted_keys and f"{broker}:{symbol}:{side}" in set(manual_acted_keys):
            return {
                "broker_ready": True,
                "live_held_vol": None,
                "open_order_vol": None,
                "matched_order_vol": None,
                "net_send_vol": 0,
                "guard_status": "SKIPPED_ALREADY_ORDERED",
                "guard_note": "ผู้ใช้ส่งคำสั่งเองไปแล้ววันนี้ — ข้ามเพื่อกันซ้ำ",
            }

        port_b = ((port_snap or {}).get("brokers") or {}).get(broker) or {}
        ord_b = ((orders_snap or {}).get("brokers") or {}).get(broker) or {}
        expected_kss_margin = str(getattr(settings, "STREAMING_ACCOUNT_NO_MARGIN", "") or "<REDACTED_KSS_ACCT>").strip()
        kss_account_mismatch = (
            broker == "kss"
            and str(item.get("account") or "").lower() == "margin"
            and bool(port_b.get("account_no"))
            and str(port_b.get("account_no")).strip() != expected_kss_margin
        )
        if not (port_b.get("available") and ord_b.get("available")) or port_b.get("error") or kss_account_mismatch:
            err = (
                port_b.get("error")
                or ord_b.get("error")
                or (
                    f"KSS อ่านบัญชีผิด ({port_b.get('account_no')} แทน {expected_kss_margin} Margin)"
                    if kss_account_mismatch
                    else f"Session {broker.upper()} ยังไม่พร้อม"
                )
            )
            return {
                "broker_ready": False,
                "live_held_vol": None,
                "open_order_vol": None,
                "matched_order_vol": None,
                "net_send_vol": runbook_vol,
                "guard_status": "BROKER_UNAVAILABLE",
                "guard_note": f"ยังไม่ได้ตรวจพอร์ตสด ({err})",
            }

        port_rows = port_b.get("rows") or []
        live_held_vol = sum(
            int(r.get("volume") or 0)
            for r in port_rows
            if str(r.get("symbol") or "").upper() == symbol
            and str(r.get("position_type") or "").upper() == expected_ptype
        )

        orders = (orders_snap or {}).get("orders") or []
        open_order_vol = sum(
            int(o.get("balance") or 0)
            for o in orders
            if str(o.get("broker") or "").lower() == broker
            and str(o.get("symbol") or "").upper() == symbol
            and str(o.get("side") or "").upper() == expected_side
            and str(o.get("kind") or "") in ("open", "partial")
        )
        matched_order_vol = sum(
            int(o.get("matched_volume") or 0)
            for o in orders
            if str(o.get("broker") or "").lower() == broker
            and str(o.get("symbol") or "").upper() == symbol
            and str(o.get("side") or "").upper() == expected_side
            and str(o.get("kind") or "") in ("matched", "partial")
        )

        remaining_runbook = max(0, runbook_vol - matched_order_vol)
        target_cap = min(remaining_runbook, live_held_vol)
        raw_net = max(0, target_cap - open_order_vol)

        if raw_net == 0:
            if open_order_vol > 0 or matched_order_vol > 0:
                parts = []
                if open_order_vol > 0:
                    parts.append(f"ค้าง {open_order_vol:,}")
                if matched_order_vol > 0:
                    parts.append(f"Match {matched_order_vol:,}")
                return {
                    "broker_ready": True,
                    "live_held_vol": live_held_vol,
                    "open_order_vol": open_order_vol,
                    "matched_order_vol": matched_order_vol,
                    "net_send_vol": 0,
                    "guard_status": "SKIPPED_ALREADY_ORDERED",
                    "guard_note": f"มีออเดอร์ {expected_side} แล้ว ({' · '.join(parts)} หุ้น) — ข้ามเพื่อกันซ้ำ",
                }
            return {
                "broker_ready": True,
                "live_held_vol": live_held_vol,
                "open_order_vol": open_order_vol,
                "matched_order_vol": matched_order_vol,
                "net_send_vol": 0,
                "guard_status": "SKIPPED_NOT_HELD",
                "guard_note": f"ไม่พบสถานะ {expected_ptype} {symbol} ในพอร์ต {broker.upper()} (คงเหลือ 0 หุ้น)",
            }

        if raw_net < 100:
            return {
                "broker_ready": True,
                "live_held_vol": live_held_vol,
                "open_order_vol": open_order_vol,
                "matched_order_vol": matched_order_vol,
                "net_send_vol": 0,
                "guard_status": "SKIPPED_ODD_LOT",
                "guard_note": f"ยอดคงเหลือสุทธิ {raw_net:,} หุ้น น้อยกว่า 100 หุ้น (Board Lot)",
            }

        send_vol = (raw_net // 100) * 100
        if send_vol < runbook_vol:
            return {
                "broker_ready": True,
                "live_held_vol": live_held_vol,
                "open_order_vol": open_order_vol,
                "matched_order_vol": matched_order_vol,
                "net_send_vol": send_vol,
                "guard_status": "ADJUSTED_VOL",
                "guard_note": (
                    f"ปรับยอดเป็น {send_vol:,} หุ้น (Runbook {runbook_vol:,} · "
                    f"พอร์ตจริง {live_held_vol:,} · ค้าง {open_order_vol:,} · Match {matched_order_vol:,})"
                ),
            }

        return {
            "broker_ready": True,
            "live_held_vol": live_held_vol,
            "open_order_vol": open_order_vol,
            "matched_order_vol": matched_order_vol,
            "net_send_vol": send_vol,
            "guard_status": "READY",
            "guard_note": f"พร้อมส่ง {send_vol:,} หุ้น (พอร์ตจริง {live_held_vol:,} หุ้น)",
        }

    async def _fetch_live_snapshots(
        self, brokers: List[str]
    ) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        """Best-effort live portfolio & order reads without crashing preview."""
        if not brokers:
            return {"brokers": {}}, {"brokers": {}, "orders": []}
        scope = "all" if len(brokers) > 1 else brokers[0]
        port_res, ord_res = await asyncio.gather(
            trading_facade.fetch_portfolio_overview(broker=scope),
            trading_facade.fetch_orders_overview(broker=scope),
            return_exceptions=True,
        )
        port_snap = port_res if isinstance(port_res, dict) else {"brokers": {}}
        ord_snap = ord_res if isinstance(ord_res, dict) else {"brokers": {}, "orders": []}
        if isinstance(port_res, Exception):
            for b in brokers:
                port_snap["brokers"][b] = {"available": False, "error": str(port_res)}
        if isinstance(ord_res, Exception):
            for b in brokers:
                ord_snap["brokers"][b] = {"available": False, "error": str(ord_res)}
        return port_snap, ord_snap

    async def _fetch_item_quote(
        self, broker: str, symbol: str, side: str
    ) -> Dict[str, Any]:
        """Best-effort live quote & Lure plan resolution without raising."""
        try:
            q = await trading_facade.compute_nego_quote(broker, symbol, side=side)
            lure = q.get("lure_plan") if isinstance(q, dict) else None
            raw_lure_px = lure.get("price") if isinstance(lure, dict) else None
            target_px, price_source = compute_runbook_exit_price(side, q.get("last"), raw_lure_px)
            quote_ok = bool(q.get("available")) and target_px is not None and target_px > 0
            return {
                "quote_ok": quote_ok,
                "bid_1": q.get("bid_1"),
                "offer_1": q.get("offer_1"),
                "last": q.get("last"),
                "lure_price": target_px,
                "price_source": price_source,
                "quote_error": (
                    None
                    if quote_ok
                    else (
                        (lure.get("reason") if isinstance(lure, dict) else None)
                        or q.get("error")
                        or f"อ่านราคา Last/Bid/Offer {symbol} ({broker.upper()}) ไม่ได้"
                    )
                ),
            }
        except Exception as exc:
            return {
                "quote_ok": False,
                "bid_1": None,
                "offer_1": None,
                "last": None,
                "lure_price": None,
                "quote_error": str(exc),
            }

    async def _probe_otp_relay(self) -> Dict[str, Any]:
        try:
            res = await otp_relay_readiness.check()
            return {
                "ready": bool(getattr(res, "ready", False)),
                "channel": getattr(res, "channel", None),
                "reason": getattr(res, "reason", None),
            }
        except Exception as exc:
            return {"ready": False, "channel": None, "reason": str(exc)}

    async def _evaluate_readiness(
        self,
        *,
        cur: datetime,
        allow_stale: bool = False,
        symbols: Optional[List[str]] = None,
        require_market_open: bool = False,
    ) -> Dict[str, Any]:
        """Evaluate Market, Ledger, Kill-Switch, Brokers/PIN, Portfolio/Orders & Quotes."""
        today_str = cur.strftime("%Y-%m-%d")
        is_holiday, holiday_name, calendar_err = self._read_holiday_info(today_str)
        phase = classify_market_phase(cur, holiday_name=holiday_name)
        is_trading_day = (cur.weekday() < 5) and (not is_holiday)
        is_market_open_now = phase in ("MARKET_OPEN_AM", "MARKET_OPEN_PM")

        ledger = self._read_ledger_exits(cur)
        items_raw = list(ledger["items"])
        if symbols is not None:
            sym_set = {str(s).strip().upper() for s in symbols if str(s).strip()}
            items_raw = [it for it in items_raw if it["symbol"] in sym_set]

        needed_brokers = sorted({it["broker"] for it in items_raw})
        kill_status = kill_switch_service.get_status()
        kill_active = bool(kill_status.get("is_active"))
        is_dry = _is_dry_mode()

        (ready_brokers, broker_errors, criticals), otp_status = await asyncio.gather(
            probe_ready_brokers(_ALL_BROKERS),
            self._probe_otp_relay(),
        )
        ready_set = set(ready_brokers)

        brokers_detail: Dict[str, Any] = {}
        for b in _ALL_BROKERS:
            pg = pin_guard.check(b)
            brokers_detail[b] = {
                "needed": b in needed_brokers,
                "ready": b in ready_set,
                "pin_locked": bool(pg.get("locked")),
                "error": broker_errors.get(b),
            }

        snap_brokers = [b for b in needed_brokers if b in ready_set]
        port_snap, ord_snap = await self._fetch_live_snapshots(snap_brokers)

        acted_set = set((self._state or {}).get("manual_acted_keys") or [])
        reconciled: List[Dict[str, Any]] = []
        sendable: List[Dict[str, Any]] = []
        for it in items_raw:
            item_id = f"{today_str}:{it['broker']}:{it['symbol']}:{it['side']}"
            acted_key = f"{it['broker']}:{it['symbol']}:{it['side']}"
            if acted_key in acted_set:
                rec = self._reconcile_item(it, port_snap, ord_snap, manual_acted_keys=acted_set)
            elif it["broker"] not in ready_set:
                rec = {
                    "broker_ready": False,
                    "live_held_vol": None,
                    "open_order_vol": None,
                    "matched_order_vol": None,
                    "net_send_vol": int(it["runbook_volume"]),
                    "guard_status": "BROKER_UNAVAILABLE",
                    "guard_note": broker_errors.get(it["broker"])
                    or f"Session {it['broker'].upper()} ไม่พร้อม",
                }
            else:
                rec = self._reconcile_item(it, port_snap, ord_snap, manual_acted_keys=acted_set)
            merged = {**it, "item_id": item_id, **rec}
            reconciled.append(merged)
            if rec["guard_status"] in ("READY", "ADJUSTED_VOL"):
                sendable.append(merged)

        # Live Bid/Offer & Lure quote check for all sendable items
        if sendable:
            q_results = await asyncio.gather(
                *[self._fetch_item_quote(it["broker"], it["symbol"], it["side"]) for it in sendable]
            )
            for it, q_info in zip(sendable, q_results):
                it.update(q_info)
        for it in reconciled:
            it.setdefault("quote_ok", None)
            it.setdefault("bid_1", None)
            it.setdefault("offer_1", None)
            it.setdefault("last", None)
            it.setdefault("lure_price", None)
            it.setdefault("quote_error", None)

        # Build All-or-Nothing blocking reasons
        blocking_reasons: List[str] = []
        if not is_trading_day:
            msg = (
                f"วันหยุดตลาด SET ({holiday_name})"
                if is_holiday
                else "วันหยุดเสาร์–อาทิตย์ (ตลาดปิด)"
            )
            blocking_reasons.append(msg)
        elif require_market_open and not is_market_open_now:
            blocking_reasons.append(f"อยู่นอกเวลาตลาดเปิด ({phase})")

        if not ledger["ledger_available"]:
            blocking_reasons.append(ledger["error"] or "ไม่พบไฟล์ positions_ledger.json")
        elif not ledger["is_today"] and not allow_stale:
            blocking_reasons.append(
                f"ข้อมูล Runbook เป็นวันที่ {ledger['as_of'] or 'ไม่ทราบ'} (ไม่ใช่ {today_str})"
            )

        if kill_active:
            blocking_reasons.append("KILL-SWITCH ACTIVE")

        active_brokers = sorted({
            it["broker"] for it in items_raw
            if f"{it['broker']}:{it['symbol']}:{it['side']}" not in acted_set
        })
        for b in active_brokers:
            if b not in ready_set:
                blocking_reasons.append(
                    f"{b.upper()}: {broker_errors.get(b) or 'Session/PIN ไม่พร้อม'}"
                )
            else:
                port_b = ((port_snap or {}).get("brokers") or {}).get(b) or {}
                ord_b = ((orders_snap_b := (ord_snap or {}).get("brokers") or {}).get(b)) or {}
                if not port_b.get("available") or not ord_b.get("available"):
                    err = port_b.get("error") or ord_b.get("error") or "อ่านพอร์ต/ออเดอร์ไม่ได้"
                    blocking_reasons.append(f"{b.upper()}: {err}")

        quotes_ready = True
        for it in sendable:
            if not it.get("quote_ok"):
                quotes_ready = False
                blocking_reasons.append(
                    f"{it['broker'].upper()} {it['symbol']}: {it.get('quote_error') or 'Quote/Lure ไม่พร้อม'}"
                )

        readiness = {
            "daily_date": today_str,
            "now_bkk": cur.strftime("%H:%M:%S"),
            "market_phase": phase,
            "is_trading_day": is_trading_day,
            "is_market_open_now": is_market_open_now,
            "is_holiday": is_holiday,
            "holiday_name": holiday_name,
            "calendar_error": calendar_err,
            "dry_run": is_dry,
            "kill_switch": kill_active,
            "ledger_available": ledger["ledger_available"],
            "runbook_is_today": ledger["is_today"],
            "as_of": ledger["as_of"],
            "needed_brokers": needed_brokers,
            "brokers": brokers_detail,
            "otp_relay": otp_status,
            "quotes_ready": quotes_ready if sendable else True,
            "all_ready": len(blocking_reasons) == 0,
            "blocking_reasons": blocking_reasons,
        }
        return {
            "cur": cur,
            "phase": phase,
            "is_holiday": is_holiday,
            "holiday_name": holiday_name,
            "is_trading_day": is_trading_day,
            "is_market_open_now": is_market_open_now,
            "ledger": ledger,
            "items": reconciled,
            "sendable": sendable,
            "needed_brokers": needed_brokers,
            "broker_errors": broker_errors,
            "criticals": criticals,
            "readiness": readiness,
        }

    # ------------------------------------------------------------------
    # Preview, Schedule Snapshot & Controls
    # ------------------------------------------------------------------

    async def preview_runbook_exits(self) -> Dict[str, Any]:
        """Read today's Runbook exits, readiness checks, and reconcile with live brokers."""
        cur = self._now()
        state = self._load_or_init_state(cur)
        ev = await self._evaluate_readiness(cur=cur, allow_stale=False, require_market_open=False)

        queue_items_by_id: Dict[str, Dict[str, Any]] = {}
        queue = state.get("queue")
        if isinstance(queue, dict):
            for qi in queue.get("items") or []:
                if qi.get("item_id"):
                    queue_items_by_id[qi["item_id"]] = qi

        enriched: List[Dict[str, Any]] = []
        for it in ev["items"]:
            q_match = queue_items_by_id.get(it["item_id"])
            enriched.append(
                {
                    **it,
                    "queued_outcome": q_match.get("outcome") if q_match else None,
                    "queued_order_no": q_match.get("broker_order_no") if q_match else None,
                }
            )

        actionable_count = sum(
            1 for x in enriched if x["guard_status"] in ("READY", "ADJUSTED_VOL", "BROKER_UNAVAILABLE")
        )
        ledger = ev["ledger"]
        return {
            "daily_date": cur.strftime("%Y-%m-%d"),
            "now_bkk": cur.strftime("%H:%M:%S"),
            "market_phase": ev["phase"],
            "is_holiday": ev["is_holiday"],
            "holiday_name": ev["holiday_name"],
            "dry_run": _is_dry_mode(),
            "kill_switch": kill_switch_service.get_status(),
            "ledger_available": ledger["ledger_available"],
            "ledger_path": ledger["ledger_path"],
            "ledger_error": ledger["error"],
            "as_of": ledger["as_of"],
            "statement_through": ledger["statement_through"],
            "is_today": ledger["is_today"],
            "actionable_count": actionable_count,
            "readiness": ev["readiness"],
            "items": enriched,
            "schedule": self.get_schedule_snapshot(cur),
        }

    def get_schedule_snapshot(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Return compact schedule snapshot for MiniApp Schedule tab."""
        cur = _now_bkk(now) if now is not None else self._now()
        state = self._load_or_init_state(cur)
        today_str = cur.strftime("%Y-%m-%d")
        is_holiday, holiday_name, _ = self._read_holiday_info(today_str)
        phase = classify_market_phase(cur, holiday_name=holiday_name)
        return {
            "daily_date": state["daily_date"],
            "auto_enabled": bool(state.get("auto_enabled", _default_auto_enabled())),
            "runbook_as_of": state.get("runbook_as_of"),
            "statement_through": state.get("statement_through"),
            "now_bkk": cur.strftime("%H:%M:%S"),
            "market_phase": phase,
            "is_holiday": is_holiday,
            "holiday_name": holiday_name,
            "running_now": self._exec_lock.locked(),
            "dry_run": _is_dry_mode(),
            "kill_switch": kill_switch_service.get_status(),
            "queue": deepcopy(state.get("queue")),
            "history_count": len(state.get("history") or []),
        }

    def set_auto_enabled(self, enabled: bool, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Toggle daily Auto-Exit 10:00-10:30 mode without touching manual Trade queues."""
        cur = _now_bkk(now) if now is not None else self._now()
        state = self._load_or_init_state(cur)
        next_val = bool(enabled)
        state["auto_enabled"] = next_val
        q = state.get("queue")
        if not next_val:
            if isinstance(q, dict) and q.get("queue_kind") == "auto_daily" and q.get("status") in (
                "AUTO_PENDING",
                "MISSED_WINDOW",
            ):
                state["queue"] = None
        else:
            if isinstance(q, dict) and q.get("queue_kind") == "auto_daily" and q.get("status") == "CANCELLED":
                # Re-enabling today after cancelling auto allows creating a fresh auto slot if pre-10:30.
                now_m = cur.hour * 60 + cur.minute
                if cur.weekday() < 5 and now_m < _to_minutes(EXIT_WINDOW_OPEN.end):
                    state["queue"] = None
            self._ensure_daily_auto_queue(state, cur)
        self._save_state()
        return self.get_schedule_snapshot(cur)

    async def enqueue_or_execute_exits(
        self,
        *,
        allow_stale: bool = False,
        symbols: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Trade step-by-step submit: enqueue pre-market/lunch or execute now when market is open."""
        if kill_switch_service.is_active():
            raise RunbookExitError(
                423,
                "CLI_KILL_SWITCH",
                "⛔ KILL-SWITCH ACTIVE — ระงับคำสั่งซื้อขายทั้งหมด · ต้อง RESET ก่อน",
            )

        cur = self._now()
        today_str = cur.strftime("%Y-%m-%d")
        is_holiday, holiday_name, _ = self._read_holiday_info(today_str)
        phase = classify_market_phase(cur, holiday_name=holiday_name)
        if phase == "HOLIDAY":
            raise RunbookExitError(
                422,
                "E_MARKET_CLOSED",
                f"วันนี้เป็นวันหยุดตลาดหลักทรัพย์ ({holiday_name}) ไม่สามารถสร้างคิว/ยิงคำสั่งได้",
            )
        if phase in ("WEEKEND", "AFTER_CLOSE"):
            raise RunbookExitError(
                422,
                "E_MARKET_CLOSED",
                "ตลาดปิดทำการแล้ว (นอกเวลา 10:00–16:30 จันทร์–ศุกร์) ไม่สามารถสร้างคิว/ยิงคำสั่งได้",
            )

        ledger = self._read_ledger_exits(cur)
        if not ledger["ledger_available"]:
            raise RunbookExitError(404, "E_LEDGER_MISSING", ledger["error"] or "ไม่พบไฟล์ Runbook")
        if not ledger["is_today"] and not allow_stale:
            raise RunbookExitError(
                422,
                "E_STALE_RUNBOOK",
                f"ข้อมูล Runbook เป็นวันที่ {ledger['as_of'] or 'ไม่ทราบ'} (ไม่ใช่ {today_str}) — กดยืนยันใช้ข้อมูลเก่าหากต้องการดำเนินการต่อ",
                {"as_of": ledger["as_of"], "today": today_str},
            )

        selected = ledger["items"]
        if symbols is not None:
            sym_set = {str(s).strip().upper() for s in symbols if str(s).strip()}
            selected = [it for it in selected if it["symbol"] in sym_set]
        if not selected:
            raise RunbookExitError(
                422,
                "E_NO_EXIT_ITEMS",
                "ไม่มีรายการ Exit (SELL/COVER) ที่เข้าเกณฑ์ใน Runbook วันนี้",
            )

        queue_items = [
            {
                "item_id": f"{today_str}:{it['broker']}:{it['symbol']}:{it['side']}",
                "broker": it["broker"],
                "account": it["account"],
                "symbol": it["symbol"],
                "side": it["side"],
                "action": it["action"],
                "runbook_volume": int(it["runbook_volume"]),
                "executed_volume": None,
                "resolved_price": None,
                "exit_reason": it["exit_reason"],
                "outcome": "PENDING",
                "broker_order_no": None,
                "idempotency_key": f"runbook-exit:{today_str}:{it['broker']}:{it['symbol']}:{it['side']}",
                "reason": "รอส่งคำสั่ง",
                "updated_at": cur.isoformat(timespec="seconds"),
            }
            for it in selected
        ]

        if phase in ("PRE_MARKET", "LUNCH_BREAK"):
            spec = EXIT_WINDOW_OPEN if phase == "PRE_MARKET" else EXIT_WINDOW_AFTERNOON
            sched_time = _pick_random_minute(spec, now_mins=cur.hour * 60 + cur.minute, rng=self._rng)
            if not sched_time:
                raise RunbookExitError(422, "E_NO_WINDOW", "ไม่สามารถสุ่มเวลาในรอบถัดไปได้")

            async with self._exec_lock:
                state = self._load_or_init_state(cur)
                self._archive_previous_queue(state)
                for qi in queue_items:
                    qi["reason"] = f"รอสุ่มเวลายิงอัตโนมัติ ({sched_time})"

                state["runbook_as_of"] = ledger["as_of"]
                state["statement_through"] = ledger["statement_through"]
                state["queue"] = {
                    "status": "PENDING",
                    "queue_kind": "manual_trade",
                    "allow_stale": bool(allow_stale),
                    "symbols": [it["symbol"] for it in selected],
                    "window_id": spec.id,
                    "window_start": spec.start,
                    "window_end": spec.end,
                    "scheduled_time": sched_time,
                    "queued_at": cur.isoformat(timespec="seconds"),
                    "trigger_source": "miniapp_premarket" if phase == "PRE_MARKET" else "miniapp_lunch",
                    "started_at": None,
                    "finished_at": None,
                    "dry_run": _is_dry_mode(),
                    "counts": _empty_counts(len(queue_items)),
                    "items": queue_items,
                    "broker_errors": {},
                    "readiness_failures": [],
                    "error": None,
                    "line_status": "งดส่ง LINE (ดูผลใน Schedule)",
                }
                self._save_state()

            return {
                "mode": "SCHEDULED",
                "scheduled_time": sched_time,
                "window": f"{spec.start}-{spec.end}",
                "message": f"ตั้งคิว Auto-Exit {len(queue_items)} รายการแล้ว (สุ่มยิงเวลา {sched_time})",
                "schedule": self.get_schedule_snapshot(cur),
            }

        async with self._exec_lock:
            state = self._load_or_init_state(cur)
            self._archive_previous_queue(state)
            state["runbook_as_of"] = ledger["as_of"]
            state["statement_through"] = ledger["statement_through"]
            state["queue"] = {
                "status": "PENDING",
                "queue_kind": "manual_trade",
                "allow_stale": bool(allow_stale),
                "symbols": [it["symbol"] for it in selected],
                "window_id": EXIT_WINDOW_OPEN.id if phase == "MARKET_OPEN_AM" else EXIT_WINDOW_AFTERNOON.id,
                "window_start": cur.strftime("%H:%M"),
                "window_end": "12:30" if phase == "MARKET_OPEN_AM" else "16:30",
                "scheduled_time": cur.strftime("%H:%M"),
                "queued_at": cur.isoformat(timespec="seconds"),
                "trigger_source": "miniapp_live",
                "started_at": None,
                "finished_at": None,
                "dry_run": _is_dry_mode(),
                "counts": _empty_counts(len(queue_items)),
                "items": queue_items,
                "broker_errors": {},
                "readiness_failures": [],
                "error": None,
                "line_status": "งดส่ง LINE (ดูผลใน Schedule)",
            }
            self._save_state()

        snapshot = await self._execute_queue(source="miniapp_live", now=cur)
        q_done = snapshot.get("queue") or {}
        cnt = q_done.get("counts") or {}
        return {
            "mode": "EXECUTED_NOW",
            "scheduled_time": q_done.get("scheduled_time"),
            "message": q_done.get("error")
            or (
                f"ดำเนินการ Exit เสร็จสิ้น: ส่ง {cnt.get('executed', 0) + cnt.get('adjusted', 0)} · "
                f"ข้าม {cnt.get('skipped', 0)} · ไม่สำเร็จ {cnt.get('failed', 0)}"
            ),
            "schedule": snapshot,
        }

    @staticmethod
    def _archive_previous_queue(state: Dict[str, Any]) -> None:
        prev_q = state.get("queue")
        if not isinstance(prev_q, dict):
            return
        st = prev_q.get("status")
        is_empty_auto = prev_q.get("queue_kind") == "auto_daily" and not (prev_q.get("items") or [])
        if st not in (None, "PENDING", "AUTO_PENDING") and not is_empty_auto:
            state.setdefault("history", []).append(deepcopy(prev_q))

    def reroll_scheduled_time(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Re-roll random HH:MM for a PENDING or AUTO_PENDING queue within its window."""
        cur = _now_bkk(now) if now is not None else self._now()
        state = self._load_or_init_state(cur)
        queue = state.get("queue")
        if not isinstance(queue, dict) or queue.get("status") not in ("PENDING", "AUTO_PENDING"):
            raise RunbookExitError(400, "E_NO_PENDING_QUEUE", "ไม่มีคิว Runbook Exit ที่รอทำงานอยู่ในขณะนี้")

        spec = EXIT_WINDOW_AFTERNOON if queue.get("window_id") == EXIT_WINDOW_AFTERNOON.id else EXIT_WINDOW_OPEN
        new_t = _pick_random_minute(spec, now_mins=cur.hour * 60 + cur.minute, rng=self._rng)
        if not new_t:
            raise RunbookExitError(422, "E_WINDOW_PASSED", "เลยช่วงเวลาสุ่มของรอบนี้แล้ว")

        queue["scheduled_time"] = new_t
        for it in queue.get("items") or []:
            if it.get("outcome") in ("PENDING", "AUTO_PENDING"):
                it["reason"] = f"รอสุ่มเวลายิงอัตโนมัติ ({new_t})"
                it["updated_at"] = cur.isoformat(timespec="seconds")
        self._save_state()
        return self.get_schedule_snapshot(cur)

    def cancel_scheduled_queue(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Cancel a PENDING or AUTO_PENDING Runbook Exit queue before execution."""
        cur = _now_bkk(now) if now is not None else self._now()
        state = self._load_or_init_state(cur)
        queue = state.get("queue")
        if not isinstance(queue, dict) or queue.get("status") not in ("PENDING", "AUTO_PENDING"):
            raise RunbookExitError(400, "E_NO_PENDING_QUEUE", "ไม่มีคิว Runbook Exit ที่รอทำงานให้ยกเลิก")

        queue["status"] = "CANCELLED"
        queue["finished_at"] = cur.strftime("%H:%M:%S")
        queue["error"] = "ผู้ใช้ยกเลิกคิวผ่าน MiniApp"
        for it in queue.get("items") or []:
            if it.get("outcome") in ("PENDING", "AUTO_PENDING"):
                it["outcome"] = "CANCELLED"
                it["reason"] = "ยกเลิกโดยผู้ใช้"
                it["updated_at"] = cur.isoformat(timespec="seconds")
        self._save_state()
        return self.get_schedule_snapshot(cur)

    # ------------------------------------------------------------------
    # Background Loop & All-or-Nothing Execution
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._load_or_init_state()
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("RunbookExitService scheduler started (check every %ss).", self.check_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("RunbookExitService scheduler stopped.")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.tick_once()
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("RunbookExitService loop error: %s", exc, exc_info=True)
                await asyncio.sleep(self.check_interval)

    async def tick_once(self, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
        """Execute today's PENDING or AUTO_PENDING queue once its randomized time arrives."""
        cur = _now_bkk(now) if now is not None else self._now()
        today_str = cur.strftime("%Y-%m-%d")
        state = self._load_or_init_state(cur)
        queue = state.get("queue")
        if not isinstance(queue, dict) or queue.get("status") not in ("PENDING", "AUTO_PENDING"):
            q_status = queue.get("status") if isinstance(queue, dict) else "NONE"
            self._log_tick_once(
                f"{today_str}:idle:{q_status}:{state.get('auto_enabled')}",
                "RunbookExit tick idle (date=%s, auto_enabled=%s, queue_status=%s)",
                today_str,
                state.get("auto_enabled"),
                q_status,
            )
            return None

        is_holiday, holiday_name, _ = self._read_holiday_info(today_str)
        if cur.weekday() >= 5 or is_holiday:
            self._log_tick_once(
                f"{today_str}:closed:{holiday_name or 'weekend'}",
                "RunbookExit tick skipped — market closed (%s)",
                holiday_name or "weekend",
            )
            return None

        sched = queue.get("scheduled_time")
        end_str = queue.get("window_end") or EXIT_WINDOW_OPEN.end
        if not sched:
            self._log_tick_once(
                f"{today_str}:nosched:{queue.get('status')}",
                "RunbookExit tick skipped — missing scheduled_time (status=%s)",
                queue.get("status"),
            )
            return None

        now_m = cur.hour * 60 + cur.minute
        if _to_minutes(sched) <= now_m < _to_minutes(end_str):
            src = "schedule_auto" if queue.get("status") == "AUTO_PENDING" else "scheduler"
            logger.info(
                "Triggering Runbook Auto-Exit (date=%s, source=%s, sched=%s, now=%s, kind=%s)",
                today_str,
                src,
                sched,
                cur.strftime("%H:%M:%S"),
                queue.get("queue_kind"),
            )
            return await self._execute_queue(source=src, now=cur)
        self._log_tick_once(
            f"{today_str}:wait:{sched}:{queue.get('status')}",
            "RunbookExit waiting for scheduled_time=%s (window_end=%s, status=%s)",
            sched,
            end_str,
            queue.get("status"),
        )
        return None

    async def run_queue_now(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """Manually trigger Runbook Exit from Schedule immediately (All-or-Nothing)."""
        cur = _now_bkk(now) if now is not None else self._now()
        today_str = cur.strftime("%Y-%m-%d")
        is_holiday, holiday_name, _ = self._read_holiday_info(today_str)
        phase = classify_market_phase(cur, holiday_name=holiday_name)

        if phase == "HOLIDAY":
            raise RunbookExitError(
                422,
                "E_MARKET_CLOSED",
                f"วันนี้เป็นวันหยุดตลาดหลักทรัพย์ ({holiday_name})",
            )
        if not _is_dry_mode() and phase not in ("MARKET_OPEN_AM", "MARKET_OPEN_PM"):
            raise RunbookExitError(
                422,
                "E_MARKET_CLOSED",
                f"ตลาดปิดทำการอยู่ ({phase}) — กดรันสดได้เฉพาะช่วงตลาดเปิด 10:00–12:30 / 14:00–16:30",
            )

        state = self._load_or_init_state(cur)
        queue = state.get("queue")
        if queue is None:
            now_m = cur.hour * 60 + cur.minute
            queue = self._build_auto_queue_payload(cur, cur.strftime("%H:%M"))
            queue["status"] = "AUTO_PENDING"
            queue["scheduled_time"] = cur.strftime("%H:%M")
            queue["finished_at"] = None
            queue["error"] = None
            queue["window_end"] = "16:30" if now_m >= _to_minutes(EXIT_WINDOW_OPEN.end) else EXIT_WINDOW_OPEN.end
            state["queue"] = queue
            self._save_state()
        elif queue.get("status") not in _RERUNNABLE_STATUSES:
            raise RunbookExitError(
                400,
                "E_NO_PENDING_QUEUE",
                f"ไม่มีคิว Runbook Exit สถานะที่รันได้ในขณะนี้ (สถานะปัจจุบัน: {queue.get('status')})",
            )
        return await self._execute_queue(source="manual_schedule", now=cur)

    @staticmethod
    def _build_queue_item_entry(item: Dict[str, Any], today_str: str, cur: datetime) -> Dict[str, Any]:
        return {
            "item_id": item.get("item_id") or f"{today_str}:{item['broker']}:{item['symbol']}:{item['side']}",
            "broker": item["broker"],
            "account": item["account"],
            "symbol": item["symbol"],
            "side": item["side"],
            "action": item.get("action") or item["side"].upper(),
            "runbook_volume": int(item["runbook_volume"]),
            "executed_volume": 0,
            "resolved_price": item.get("lure_price"),
            "exit_reason": item.get("exit_reason") or "",
            "outcome": "PENDING",
            "broker_order_no": None,
            "idempotency_key": f"runbook-exit:{today_str}:{item['broker']}:{item['symbol']}:{item['side']}",
            "reason": "รอตรวจสอบความพร้อม",
            "updated_at": cur.isoformat(timespec="seconds"),
        }

    async def _abort_batch(
        self,
        *,
        state: Dict[str, Any],
        queue: Dict[str, Any],
        cur: datetime,
        status: str,
        error_msg: str,
        error_key: str,
        alert_title: str,
        readiness_failures: List[str],
        send_alert: bool = True,
    ) -> Dict[str, Any]:
        queue["status"] = status
        queue["finished_at"] = cur.strftime("%H:%M:%S")
        queue["error"] = error_msg
        queue["readiness_failures"] = list(readiness_failures)
        for it in queue.get("items") or []:
            if it.get("outcome") in ("PENDING", "AUTO_PENDING", None):
                it["outcome"] = status
                it["executed_volume"] = 0
                it["reason"] = error_msg
                it["updated_at"] = cur.isoformat(timespec="seconds")

        if send_alert:
            pushed = await notify_critical_error_once(
                state,
                error_key=error_key,
                title=alert_title,
                detail=error_msg,
                category="trade",
                action_tab="nego",
                schedule_label="Schedule",
            )
            queue["line_status"] = format_error_line_status(pushed)
        else:
            queue["line_status"] = "งดส่ง LINE (ดูผลใน Schedule)"

        self._save_state()
        return self.get_schedule_snapshot(cur)

    async def _execute_queue(
        self,
        *,
        source: str,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Single-flight All-or-Nothing batch execution with Safe Net-Remaining Guard."""
        async with self._exec_lock:
            cur = _now_bkk(now) if now is not None else self._now()
            today_str = cur.strftime("%Y-%m-%d")
            state = self._load_or_init_state(cur)
            queue = state.get("queue")
            if not isinstance(queue, dict) or queue.get("status") not in _RERUNNABLE_STATUSES:
                return self.get_schedule_snapshot(cur)

            is_auto_dynamic = queue.get("queue_kind") == "auto_daily"
            allow_stale = bool(queue.get("allow_stale")) and (not is_auto_dynamic)
            symbols_filter = None if is_auto_dynamic else queue.get("symbols")
            if not symbols_filter and queue.get("items") and not is_auto_dynamic:
                symbols_filter = [it["symbol"] for it in queue.get("items") or [] if it.get("symbol")]

            is_dry = _is_dry_mode()
            queue["status"] = "RUNNING"
            queue["trigger_source"] = source
            queue["started_at"] = cur.strftime("%H:%M:%S")
            queue["dry_run"] = is_dry
            queue["error"] = None
            queue["readiness_failures"] = []
            self._save_state()

            # Check Kill-Switch first
            if kill_switch_service.is_active():
                return await self._abort_batch(
                    state=state,
                    queue=queue,
                    cur=cur,
                    status="BLOCKED_KILL_SWITCH",
                    error_msg="KILL-SWITCH ACTIVE — ระงับการยิง Exit อัตโนมัติทั้งหมด",
                    error_key="rb_kill_switch",
                    alert_title="🚨 Runbook Auto-Exit ถูกระงับ (Kill-Switch)",
                    readiness_failures=["KILL-SWITCH ACTIVE"],
                    send_alert=True,
                )

            # Evaluate Market Calendar, Fresh Ledger, Broker Sessions/PIN, Portfolio/Orders & Quotes
            ev = await self._evaluate_readiness(
                cur=cur,
                allow_stale=allow_stale,
                symbols=symbols_filter,
                require_market_open=False,
            )
            ledger = ev["ledger"]
            state["runbook_as_of"] = ledger.get("as_of")
            state["statement_through"] = ledger.get("statement_through")
            queue["broker_errors"] = dict(ev["broker_errors"])

            # Rebuild queue items cleanly from current ledger snapshot
            existing_by_id = {
                qi["item_id"]: qi for qi in (queue.get("items") or []) if isinstance(qi, dict) and qi.get("item_id")
            }
            fresh_items: List[Dict[str, Any]] = []
            for rec_it in ev["items"]:
                entry = existing_by_id.get(rec_it["item_id"])
                if entry is None or is_auto_dynamic:
                    entry = self._build_queue_item_entry(rec_it, today_str, cur)
                else:
                    entry = dict(entry)
                    entry["outcome"] = "PENDING"
                    entry["executed_volume"] = 0
                fresh_items.append(entry)
            queue["items"] = fresh_items
            queue["counts"] = _empty_counts(len(fresh_items))

            # Gate 1: Holiday / Weekend
            if not ev["is_trading_day"]:
                msg = (
                    f"วันหยุดตลาด SET ({ev['holiday_name']})"
                    if ev["is_holiday"]
                    else "วันหยุดเสาร์–อาทิตย์ — ตลาดปิดทำการ"
                )
                return await self._abort_batch(
                    state=state,
                    queue=queue,
                    cur=cur,
                    status="MARKET_CLOSED",
                    error_msg=msg,
                    error_key="rb_market_closed",
                    alert_title="🌙 Runbook Auto-Exit ข้ามวันหยุด",
                    readiness_failures=[msg],
                    send_alert=False,
                )

            # Gate 2: Ledger available & Fresh (as_of == today)
            if not ledger["ledger_available"]:
                err = ledger["error"] or "ไม่พบไฟล์ positions_ledger.json"
                return await self._abort_batch(
                    state=state,
                    queue=queue,
                    cur=cur,
                    status="BLOCKED_NOT_READY",
                    error_msg=err,
                    error_key="rb_ledger_missing",
                    alert_title="🚨 Runbook Auto-Exit ไม่พบไฟล์ข้อมูล",
                    readiness_failures=[err],
                    send_alert=True,
                )

            if not ledger["is_today"] and not allow_stale:
                stale_msg = f"ข้อมูล Runbook เป็นวันที่ {ledger['as_of'] or 'ไม่ทราบ'} (ไม่ใช่ {today_str}) — ข้ามเพื่อความปลอดภัย"
                return await self._abort_batch(
                    state=state,
                    queue=queue,
                    cur=cur,
                    status="SKIPPED_STALE_RUNBOOK",
                    error_msg=stale_msg,
                    error_key="rb_stale_runbook",
                    alert_title="⚠️ Runbook Auto-Exit ข้ามข้อมูลเก่า (Stale)",
                    readiness_failures=[stale_msg],
                    send_alert=True,
                )

            # Gate 3: Zero Exit items today -> clean NO_EXITS (0% LINE)
            if not fresh_items:
                queue["status"] = "NO_EXITS"
                queue["finished_at"] = cur.strftime("%H:%M:%S")
                queue["error"] = None
                queue["line_status"] = "งดส่ง LINE (วันนี้ไม่มีรายการ Exit)"
                self._save_state()
                return self.get_schedule_snapshot(cur)

            # Gate 4: All-or-Nothing Readiness (Brokers CDP/PIN, Port/Orders snapshot, Live Quotes)
            readiness = ev["readiness"]
            blocking = [
                r for r in readiness.get("blocking_reasons") or []
                if not r.startswith("อยู่นอกเวลา")
            ]
            if blocking:
                detail = " | ".join(blocking[:4])
                return await self._abort_batch(
                    state=state,
                    queue=queue,
                    cur=cur,
                    status="BLOCKED_NOT_READY",
                    error_msg=f"All-or-Nothing ระงับทั้งชุด: {detail}",
                    error_key=f"rb_not_ready_{queue.get('window_id', 'open')}",
                    alert_title="🚨 Runbook Auto-Exit ระบบยังไม่พร้อม (0 orders)",
                    readiness_failures=blocking,
                    send_alert=True,
                )

            # Gate 5: Reconcile and execute READY / ADJUSTED_VOL items
            rec_by_id = {x["item_id"]: x for x in ev["items"]}
            counts = _empty_counts(len(fresh_items))
            failed_msgs: List[str] = []

            for it in fresh_items:
                rec = rec_by_id[it["item_id"]]
                gs = rec["guard_status"]
                it["updated_at"] = self._now().isoformat(timespec="seconds")

                if gs in ("SKIPPED_ALREADY_ORDERED", "SKIPPED_NOT_HELD", "SKIPPED_ODD_LOT"):
                    it["outcome"] = gs
                    it["executed_volume"] = 0
                    it["reason"] = rec["guard_note"]
                    counts["skipped"] += 1
                    continue

                send_vol = int(rec["net_send_vol"])
                broker = str(it["broker"]).lower()
                symbol = str(it["symbol"]).upper()
                side = str(it["side"]).lower()
                try:
                    res = await trading_facade.execute_trade_with_idempotency(
                        idempotency_key=str(it["idempotency_key"]),
                        notify_line=False,
                        side=side,
                        symbol=symbol,
                        broker=broker,
                        account=str(it.get("account") or ("margin" if broker == "kss" else "cash")),
                        amount_mode="volume",
                        volume=send_vol,
                        price=rec.get("lure_price"),
                    )
                    ord_info = res.get("order") or {}
                    exec_info = res.get("execution") or {}
                    it["executed_volume"] = int(ord_info.get("volume") or send_vol)
                    it["resolved_price"] = ord_info.get("price") or rec.get("lure_price")
                    it["broker_order_no"] = str(exec_info.get("broker_order_no") or res.get("draft_id") or "") or None
                    if res.get("dry_run"):
                        it["outcome"] = "DRY_RUN"
                        it["reason"] = f"จำลองส่ง {send_vol:,} หุ้น @ ฿{it['resolved_price'] or '-'} (DRY-RUN)"
                        counts["executed"] += 1
                    elif gs == "ADJUSTED_VOL":
                        it["outcome"] = "ADJUSTED_EXECUTED"
                        it["reason"] = rec["guard_note"]
                        counts["adjusted"] += 1
                    else:
                        it["outcome"] = "EXECUTED"
                        it["reason"] = f"ส่งคำสั่งสำเร็จ #{it['broker_order_no'] or '-'}"
                        counts["executed"] += 1
                except trading_facade.FacadeHttpError as exc:
                    it["outcome"] = "FAILED"
                    it["executed_volume"] = 0
                    it["reason"] = f"[{exc.code}] {exc.message}"
                    counts["failed"] += 1
                    failed_msgs.append(f"{broker.upper()} {symbol}: {it['reason']}")
                except Exception as exc:
                    it["outcome"] = "FAILED"
                    it["executed_volume"] = 0
                    it["reason"] = str(exc)
                    counts["failed"] += 1
                    failed_msgs.append(f"{broker.upper()} {symbol}: {exc}")

            queue["counts"] = counts
            queue["finished_at"] = self._now().strftime("%H:%M:%S")
            sent_total = counts["executed"] + counts["adjusted"]

            if failed_msgs:
                queue["status"] = "ERROR" if sent_total == 0 else "PARTIAL_ERROR"
                queue["error"] = " | ".join(failed_msgs[:3])
                pushed = await notify_critical_error_once(
                    state,
                    error_key=f"rb_exec_fail_{queue.get('window_id', 'open')}",
                    title="🚨 Runbook Auto-Exit ส่งคำสั่งไม่สำเร็จ",
                    detail="\n".join(failed_msgs[:5]),
                    category="trade",
                    action_tab="nego",
                    schedule_label="Schedule",
                )
                queue["line_status"] = format_error_line_status(pushed)
            elif sent_total > 0:
                queue["status"] = "DONE"
                queue["error"] = None
                queue["line_status"] = "งดส่ง LINE (ดูผลใน Schedule)"
            else:
                queue["status"] = "SKIPPED"
                queue["error"] = None
                queue["line_status"] = "งดส่ง LINE (ดูผลใน Schedule)"

            self._save_state()
            return self.get_schedule_snapshot(cur)


runbook_exit_service = RunbookExitService()
