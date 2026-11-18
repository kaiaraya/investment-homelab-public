"""On-Demand Model 19 Suggest Entry Service (Zero-DB Pure-CDP).

Replaces autonomous entry scheduling with an explicit human-in-the-loop
workflow inside MiniApp Tab Trade:
  1. Fast local hub summary (``<50ms``, zero CDP calls) for the split Trade view.
  2. On-demand live Poll across Top-8 Merge Long / Short pools using live CDP
     quotes (``last`` & ``prior_close``), live broker purchasing power, live
     holdings / same-day orders, and KSS SBL borrow pool / XD checks.
  3. User-confirmed Multiple-Choice execution (``BUY`` Lure/Match or SBL Borrow
     + SET-Uptick ``SHORT`` Lure with 1 uptick re-quote retry).
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
from pathlib import Path
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
import uuid

from app.core.config import settings
from app.services import account_routing, trading_rules
from app.services.broker_cdp import trading_facade
from app.services.entry_ledger_service import (
    _atomic_write_json,
    _now_bkk,
    build_entry_ledger,
    extract_managed_exit_summary,
)
from app.services.kill_switch_service import kill_switch_service
from app.services.miniapp_inbox import miniapp_inbox
from app.services.nego_scheduler import (
    format_error_line_status,
    notify_critical_error_once,
    probe_ready_brokers,
)
from app.services.runbook_exit_service import classify_market_phase, runbook_exit_service
from app.services.trade_dispatcher import trade_dispatcher
from app.services.validator import validator

logger = logging.getLogger(__name__)

_MIN_BUDGET_THB = 1000.0
_MAX_BUDGET_THB = 50_000_000.0
_POLL_CTX_TTL_S = 30.0


def compute_candidate_sizing(
    base_budget_thb: float,
    pool_score: Any,
    sizing_reference: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Scale base budget per slot by Edge Tier (pool_score) and House-Money multiplier."""
    score: Optional[float] = None
    if pool_score is not None:
        try:
            score_val = float(pool_score)
            if score_val == score_val:  # not NaN
                score = score_val
        except (TypeError, ValueError):
            score = None

    if score is None or (100.0 <= score < 180.0):
        tier, tier_label, base_mult = "TIER2", "Normal", 1.00
    elif score >= 180.0:
        tier, tier_label, base_mult = "TIER1", "High-Edge", 1.35
    else:
        tier, tier_label, base_mult = "TIER3", "Defensive", 0.70

    ref = sizing_reference or {}
    hm_active = bool(ref.get("house_money_active", False))
    try:
        raw_hm = float(ref.get("house_money_multiplier") or 1.0)
    except (TypeError, ValueError):
        raw_hm = 1.0
    hm_mult = raw_hm if (hm_active and raw_hm > 0) else 1.00
    eff_mult = round(base_mult * hm_mult, 2)
    eff_budget = round(float(base_budget_thb) * eff_mult, 2)
    return {
        "edge_tier": tier,
        "edge_tier_label": tier_label,
        "base_multiplier": round(base_mult, 2),
        "house_money_active": hm_active,
        "house_money_multiplier": round(hm_mult, 2),
        "effective_multiplier": eff_mult,
        "effective_budget_thb": eff_budget,
    }


def compute_recommended_slot_budget(
    net_equity_thb: Optional[float],
    cap_slots: int = 7,
) -> Optional[int]:
    """Recommend per-slot budget from Net Liquidation Equity ÷ cap_slots, rounded to 1,000 THB."""
    if net_equity_thb is None:
        return None
    try:
        equity = float(net_equity_thb)
    except (TypeError, ValueError):
        return None
    if equity <= 0 or equity != equity:
        return None
    slots = max(1, int(cap_slots or 7))
    raw_per_slot = equity / float(slots)
    rounded = int(round(raw_per_slot / 1000.0) * 1000)
    return max(int(_MIN_BUDGET_THB), rounded)

_EXIT_SCHEDULE_META: Dict[str, Tuple[str, str]] = {
    "DONE": ("ส่ง Exit แล้ว", "green"),
    "NO_EXITS": ("ไม่มี Exit", "green"),
    "AUTO_PENDING": ("รอเวลาสุ่ม", "sky"),
    "PENDING": ("รอเวลาสุ่ม", "sky"),
    "RUNNING": ("กำลังส่ง", "amber"),
    "MISSED_WINDOW": ("เลยช่วง Auto", "muted"),
    "DISABLED": ("ปิด Auto", "muted"),
    "BLOCKED_NOT_READY": ("รอระบบพร้อม", "red"),
    "BLOCKED_KILL_SWITCH": ("Kill-Switch", "red"),
    "SKIPPED_STALE_RUNBOOK": ("Runbook เก่า", "amber"),
    "MARKET_CLOSED": ("ตลาดปิด", "muted"),
    "CANCELLED": ("ยกเลิกคิว", "muted"),
    "ERROR": ("ติด Error", "red"),
    "PARTIAL_ERROR": ("สำเร็จบางส่วน", "amber"),
}


def _describe_exit_schedule(rb_sched: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    if not rb_sched:
        return "อัตโนมัติ", "muted"
    q = rb_sched.get("queue")
    raw_st = str(
        (q.get("status") if isinstance(q, dict) else None)
        or ("DISABLED" if rb_sched.get("auto_enabled") is False else "PENDING")
    ).upper()
    if raw_st in ("AUTO_PENDING", "PENDING") and isinstance(q, dict) and q.get("scheduled_time"):
        return f"นัด {q['scheduled_time']}", "sky"
    return _EXIT_SCHEDULE_META.get(raw_st, (raw_st, "muted"))


class SuggestEntryError(Exception):
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


def _is_dry_mode() -> bool:
    return (not bool(trade_dispatcher.is_live)) or bool(getattr(settings, "DRY_RUN_MODE", False))


def _default_budget_thb() -> float:
    raw = getattr(settings, "SUGGEST_ENTRY_DEFAULT_BUDGET_THB", 150000.0)
    try:
        val = float(raw)
        return val if val >= _MIN_BUDGET_THB else 150000.0
    except (TypeError, ValueError):
        return 150000.0


class SuggestEntryService:
    """On-demand Top-8 Suggest Entry planner, live poller, and executor."""

    def __init__(
        self,
        *,
        state_path: Optional[str] = None,
        scanner_dir: Optional[str] = None,
        positions_ledger_path: Optional[str] = None,
        entry_ledger_path: Optional[str] = None,
        clock_fn: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._state_path_override = state_path
        self._scanner_dir_override = scanner_dir
        self._positions_ledger_override = positions_ledger_path
        self._entry_ledger_override = entry_ledger_path
        self._clock_fn = clock_fn or (lambda: _now_bkk())
        self._exec_lock = asyncio.Lock()
        self._state: Optional[Dict[str, Any]] = None
        self._last_poll_ctx: Optional[Dict[str, Any]] = None

    @property
    def state_path(self) -> Path:
        raw = self._state_path_override or getattr(
            settings, "SUGGEST_ENTRY_STATE_PATH", "data/suggest_entry_state.json"
        )
        return Path(raw)

    @property
    def equity_snapshot_path(self) -> Path:
        return self.state_path.parent / "portfolio_equity_snapshot.json"

    def record_portfolio_equity_snapshot(
        self,
        *,
        total_net_equity: Optional[float],
        partial: bool = False,
        accounts: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Persist latest verified portfolio Net Equity snapshot for instant Trade sizing hints."""
        if total_net_equity is None:
            return None
        try:
            equity_val = round(float(total_net_equity), 2)
        except (TypeError, ValueError):
            return None
        cur = self._now()
        payload = {
            "total_net_equity": equity_val,
            "partial": bool(partial),
            "as_of": cur.isoformat(timespec="seconds"),
            "accounts": [
                {
                    "key": a.get("key"),
                    "broker": a.get("broker"),
                    "account_no": a.get("account_no"),
                    "net_equity": a.get("net_equity"),
                }
                for a in (accounts or [])
                if isinstance(a, dict)
            ],
        }
        try:
            _atomic_write_json(self.equity_snapshot_path, payload)
        except Exception as exc:
            logger.debug("Failed to persist portfolio equity snapshot: %s", exc)
        return payload

    def get_portfolio_equity_snapshot(self) -> Optional[Dict[str, Any]]:
        """Load cached portfolio Net Equity snapshot without making CDP calls."""
        path = self.equity_snapshot_path
        if not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("total_net_equity") is not None:
                return raw
        except Exception:
            return None
        return None

    def _now(self) -> datetime:
        return _now_bkk(self._clock_fn())

    def _default_state(self, cur: datetime, *, budget: Optional[float] = None, budget_mode: str = "recommended") -> Dict[str, Any]:
        return {
            "schema_version": 1,
            "daily_date": cur.strftime("%Y-%m-%d"),
            "budget_per_slot_thb": float(budget if budget is not None else _default_budget_thb()),
            "budget_mode": budget_mode if budget_mode in ("recommended", "manual") else "recommended",
            "submissions": [],
            "sent_error_keys": [],
        }

    def _load_state(self, cur: Optional[datetime] = None) -> Dict[str, Any]:
        now = cur or self._now()
        today_str = now.strftime("%Y-%m-%d")
        dirty = False

        if self._state is None:
            path = self.state_path
            if path.is_file():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        self._state = raw
                except Exception as exc:
                    logger.warning("Failed to read %s, reinitializing: %s", path, exc)
            if self._state is None:
                self._state = self._default_state(now)
                dirty = True

        state = self._state
        state.setdefault("schema_version", 1)
        try:
            budget = float(state.get("budget_per_slot_thb") or _default_budget_thb())
        except (TypeError, ValueError):
            budget = _default_budget_thb()
            dirty = True
        if budget < _MIN_BUDGET_THB:
            budget = _default_budget_thb()
            dirty = True
        state["budget_per_slot_thb"] = budget
        if state.get("budget_mode") not in ("recommended", "manual"):
            state["budget_mode"] = "recommended"
        state.setdefault("submissions", [])
        state.setdefault("sent_error_keys", [])

        if state.get("daily_date") != today_str:
            self._state = self._default_state(now, budget=budget, budget_mode=str(state.get("budget_mode") or "recommended"))
            state = self._state
            dirty = True

        if dirty:
            _atomic_write_json(self.state_path, state)
        return state

    def _save_state(self) -> None:
        if self._state is not None:
            _atomic_write_json(self.state_path, self._state)

    def _build_ledger(self, cur: datetime) -> Dict[str, Any]:
        return build_entry_ledger(
            now=cur,
            scanner_dir=self._scanner_dir_override,
            positions_ledger_path=self._positions_ledger_override,
            entry_ledger_path=self._entry_ledger_override,
            persist=True,
        )

    def _resolve_effective_budget(
        self,
        state: Dict[str, Any],
        ledger: Optional[Dict[str, Any]] = None,
    ) -> Tuple[float, str, Optional[int], Optional[Dict[str, Any]], int]:
        quota = (ledger or {}).get("quota") or {}
        cap_slots = int(quota.get("cap_unique_tickers") or 7)
        eq_snap = self.get_portfolio_equity_snapshot()
        net_eq = eq_snap.get("total_net_equity") if eq_snap else None
        rec_budget = compute_recommended_slot_budget(net_eq, cap_slots=cap_slots)
        mode = str(state.get("budget_mode") or "recommended").lower()
        if mode not in ("recommended", "manual"):
            mode = "recommended"
        if mode == "recommended" and rec_budget is not None:
            return float(rec_budget), mode, rec_budget, eq_snap, cap_slots
        return float(state["budget_per_slot_thb"]), mode, rec_budget, eq_snap, cap_slots

    def save_config(
        self,
        budget_per_slot_thb: Optional[float] = None,
        *,
        use_recommended: bool = False,
    ) -> Dict[str, Any]:
        cur = self._now()
        state = self._load_state(cur)
        if use_recommended:
            ledger = self._build_ledger(cur)
            cap_slots = int((ledger.get("quota") or {}).get("cap_unique_tickers") or 7)
            eq_snap = self.get_portfolio_equity_snapshot()
            net_eq = eq_snap.get("total_net_equity") if eq_snap else None
            rec_budget = compute_recommended_slot_budget(net_eq, cap_slots=cap_slots)
            state["budget_mode"] = "recommended"
            if rec_budget is not None:
                state["budget_per_slot_thb"] = float(rec_budget)
            elif budget_per_slot_thb is not None:
                val = float(budget_per_slot_thb)
                if val < _MIN_BUDGET_THB or val > _MAX_BUDGET_THB:
                    raise SuggestEntryError(
                        422,
                        "E_BAD_BUDGET",
                        f"งบต่อ 1 ไม้ต้องอยู่ระหว่าง ฿{_MIN_BUDGET_THB:,.0f} – ฿{_MAX_BUDGET_THB:,.0f}",
                    )
                state["budget_per_slot_thb"] = round(val, 2)
            state["updated_at"] = cur.isoformat(timespec="seconds")
            self._save_state()
            return {
                "ok": True,
                "budget_mode": "recommended",
                "budget_per_slot_thb": float(state["budget_per_slot_thb"]),
                "recommended_budget_thb": rec_budget,
                "updated_at": state["updated_at"],
            }

        try:
            val = float(budget_per_slot_thb)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise SuggestEntryError(422, "E_BAD_BUDGET", "กรุณาระบุงบต่อ 1 ไม้เป็นตัวเลขที่ถูกต้อง") from exc
        if val < _MIN_BUDGET_THB or val > _MAX_BUDGET_THB:
            raise SuggestEntryError(
                422,
                "E_BAD_BUDGET",
                f"งบต่อ 1 ไม้ต้องอยู่ระหว่าง ฿{_MIN_BUDGET_THB:,.0f} – ฿{_MAX_BUDGET_THB:,.0f}",
            )
        state["budget_mode"] = "manual"
        state["budget_per_slot_thb"] = round(val, 2)
        state["updated_at"] = cur.isoformat(timespec="seconds")
        self._save_state()
        return {
            "ok": True,
            "budget_mode": "manual",
            "budget_per_slot_thb": state["budget_per_slot_thb"],
            "updated_at": state["updated_at"],
        }

    def _today_submitted_entries(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """Count today's successful/dry-run Suggest Entry submissions by side and ticker."""
        by_side = {"LONG": set(), "SHORT": set()}
        for sub in state.get("submissions") or []:
            if not isinstance(sub, dict):
                continue
            if sub.get("status") not in ("EXECUTED", "DRY_RUN"):
                continue
            sym = str(sub.get("symbol") or "").strip().upper()
            side = str(sub.get("side") or "").strip().upper()
            if sym and side in by_side:
                by_side[side].add(sym)
        return {
            "long_symbols": by_side["LONG"],
            "short_symbols": by_side["SHORT"],
            "total_count": len(by_side["LONG"]) + len(by_side["SHORT"]),
        }

    def get_hub_summary(self) -> Dict[str, Any]:
        """Fast local-only summary for Tab Trade split view (no CDP calls)."""
        cur = self._now()
        today_str = cur.strftime("%Y-%m-%d")
        is_holiday, holiday_name, _cal_err = runbook_exit_service._read_holiday_info(today_str)
        phase = classify_market_phase(cur, holiday_name=holiday_name)
        is_trading_day = phase not in ("WEEKEND", "HOLIDAY")

        state = self._load_state(cur)
        ledger = self._build_ledger(cur)
        exit_summary = extract_managed_exit_summary(
            positions_ledger_path=self._positions_ledger_override,
            now=cur,
        )
        rb_schedule = runbook_exit_service.get_schedule_snapshot(cur)

        quota = ledger.get("quota") or {}
        eff_long = int(quota.get("effective_long") or 0)
        eff_short = int(quota.get("effective_short") or 0)
        eff_total = int(quota.get("effective_total") or 0)
        submitted = self._today_submitted_entries(state)

        rem_long = max(0, eff_long - len(submitted["long_symbols"]))
        rem_short = max(0, eff_short - len(submitted["short_symbols"]))
        rem_total = max(0, eff_total - submitted["total_count"])
        is_hold_cash = int(quota.get("model_long") or 0) == 0 and int(quota.get("model_short") or 0) == 0

        sched_label, sched_tone = _describe_exit_schedule(rb_schedule)
        pools = ledger.get("pools") or {}
        candidate_sides: Dict[str, str] = {}
        for c in (pools.get("long_top8") or []):
            t = str(c.get("ticker") or "").strip().upper()
            if t and t not in candidate_sides:
                candidate_sides[t] = "LONG"
        for c in (pools.get("short_top8") or []):
            t = str(c.get("ticker") or "").strip().upper()
            if t:
                candidate_sides[t] = "SHORT"

        sizing_ref = dict(ledger.get("sizing_reference") or {})
        eff_budget, budget_mode, rec_budget, eq_snap, cap_slots = self._resolve_effective_budget(state, ledger)
        net_eq = eq_snap.get("total_net_equity") if eq_snap else None
        sizing_ref["net_equity_thb"] = net_eq
        sizing_ref["net_equity_as_of"] = eq_snap.get("as_of") if eq_snap else None
        sizing_ref["net_equity_partial"] = bool(eq_snap.get("partial")) if eq_snap else None
        sizing_ref["selected_accounts"] = eq_snap.get("accounts") if eq_snap else []
        sizing_ref["recommended_slots"] = cap_slots
        sizing_ref["recommended_budget_thb"] = rec_budget

        return {
            "daily_date": today_str,
            "now_bkk": cur.strftime("%H:%M:%S"),
            "market_phase": phase,
            "is_holiday": is_holiday,
            "holiday_name": holiday_name,
            "is_trading_day": is_trading_day,
            "dry_run": _is_dry_mode(),
            "kill_switch": kill_switch_service.get_status(),
            "trading_rules": trading_rules.get_rules(),
            "auto_exit": {
                **exit_summary,
                "schedule": rb_schedule,
                "schedule_status_label": sched_label,
                "schedule_status_tone": sched_tone,
            },
            "suggest_entry": {
                "ledger_available": bool(ledger.get("ledger_available")),
                "as_of": ledger.get("as_of"),
                "is_today": bool(ledger.get("is_today")),
                "error": ledger.get("error"),
                "budget_per_slot_thb": eff_budget,
                "budget_mode": budget_mode,
                "candidate_sides": candidate_sides,
                "regime": ledger.get("regime") or {},
                "quota": quota,
                "sizing_reference": sizing_ref,
                "is_hold_cash": is_hold_cash,
                "today_submitted_count": submitted["total_count"],
                "remaining_long": rem_long,
                "remaining_short": rem_short,
                "remaining_total": min(rem_total, rem_long + rem_short) if not is_hold_cash else 0,
            },
        }

    @staticmethod
    def _extract_live_broker_state(
        port_snap: Dict[str, Any],
        ord_snap: Dict[str, Any],
        broker: str,
    ) -> Dict[str, Any]:
        port_b = ((port_snap or {}).get("brokers") or {}).get(broker) or {}
        ord_b = ((ord_snap or {}).get("brokers") or {}).get(broker) or {}
        available = bool(port_b.get("available")) and bool(ord_b.get("available"))
        error = port_b.get("error") or ord_b.get("error")

        held_symbols = {
            str(r.get("symbol") or "").strip().upper()
            for r in (port_b.get("rows") or [])
            if int(r.get("volume") or 0) > 0 and str(r.get("symbol") or "").strip()
        }

        line_avail = port_b.get("line_available")
        if line_avail is None:
            line_avail = port_b.get("cash_balance")
        try:
            line_val = float(line_avail) if line_avail is not None else None
        except (TypeError, ValueError):
            line_val = None

        entry_orders_by_side: Dict[str, set[str]] = {"LONG": set(), "SHORT": set()}
        for o in (ord_snap or {}).get("orders") or []:
            if str(o.get("broker") or "").strip().lower() != broker:
                continue
            side = str(o.get("side") or "").strip().upper()
            sym = str(o.get("symbol") or "").strip().upper()
            if not sym:
                continue
            if side == "BUY":
                entry_orders_by_side["LONG"].add(sym)
            elif side in ("SHORT", "OPEN_SHORT"):
                entry_orders_by_side["SHORT"].add(sym)

        return {
            "available": available,
            "error": error,
            "line_available": line_val,
            "held_symbols": held_symbols,
            "entry_orders_by_side": entry_orders_by_side,
        }

    @staticmethod
    def _build_candidate_row(
        cand: Dict[str, Any],
        *,
        route_broker: str,
        route_account: str,
        broker_ready: bool,
        broker_state: Dict[str, Any],
        quote_data: Dict[str, Any],
        sbl_data: Dict[str, Any],
        budget_thb: float,
        ledger_is_today: bool,
        allow_stale: bool,
        kill_active: bool,
        is_hold_cash: bool,
        override_hold_cash: bool,
        remaining_for_side: int,
        submitted_today: set[str],
        free_slots: int,
        sizing_reference: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        ticker = cand["ticker"]
        side = cand["side"]  # "LONG" | "SHORT"
        order_side = "BUY" if side == "LONG" else "SHORT"
        high_atr = bool(cand.get("atr20_pct") is not None and float(cand["atr20_pct"]) >= 2.0)

        if not broker_ready:
            quote_data = {"available": False, "error": broker_state.get("error") or f"Session {route_broker.upper()} ไม่พร้อม"}

        quote_ok = bool(quote_data.get("available"))
        last_px = quote_data.get("last")
        prior_close = quote_data.get("prior_close")
        bid_1 = quote_data.get("bid_1")
        offer_1 = quote_data.get("offer_1")

        live_pct: Optional[float] = None
        if last_px is not None and prior_close is not None and float(prior_close) > 0:
            live_pct = round((float(last_px) - float(prior_close)) / float(prior_close) * 100.0, 2)

        open_plan = quote_data.get("open_plan") if isinstance(quote_data.get("open_plan"), dict) else {}
        lure_plan = quote_data.get("lure_plan") if isinstance(quote_data.get("lure_plan"), dict) else {}
        match_plan = quote_data.get("match_plan") if isinstance(quote_data.get("match_plan"), dict) else {}
        lure_px = (
            float(open_plan["price"])
            if side == "SHORT" and open_plan.get("ok") and open_plan.get("price")
            else float(lure_plan["price"]) if lure_plan.get("ok") and lure_plan.get("price") else None
        )
        open_uptick = (
            int(quote_data["short_open_uptick_ticks"])
            if quote_data.get("short_open_uptick_ticks") is not None
            else 0
        )
        can_match = (
            (side == "LONG" or (open_uptick == 0 and bool(quote_data.get("match_allowed"))))
            and match_plan.get("ok")
            and match_plan.get("price")
        )
        match_px = float(match_plan["price"]) if can_match else None

        sizing = compute_candidate_sizing(budget_thb, cand.get("pool_score"), sizing_reference)
        ref_px = lure_px or (float(last_px) if last_px else None) or cand.get("close")
        line_avail = broker_state.get("line_available")
        slot_budget = float(sizing["effective_budget_thb"])
        effective_budget = min(slot_budget, line_avail) if (line_avail is not None and line_avail >= 0) else slot_budget
        planned_shares = (
            int((effective_budget / float(ref_px)) // 100) * 100
            if ref_px and float(ref_px) > 0
            else 0
        )
        planned_value = round(planned_shares * float(ref_px), 2) if (planned_shares > 0 and ref_px) else 0.0

        sbl_avail: Optional[int] = None
        if side == "SHORT":
            raw_avail = sbl_data.get("available_qty")
            if raw_avail is None:
                raw_avail = sbl_data.get("available_vol")
            if raw_avail is not None:
                sbl_avail = int(raw_avail)
            elif not sbl_data.get("error"):
                sbl_avail = cand.get("sbl_vol")
        xd_risk = bool((sbl_data.get("xd_risk") or {}).get("xd_risk")) if side == "SHORT" else False
        xd_reason = str((sbl_data.get("xd_risk") or {}).get("reason") or "") if xd_risk else None

        price_signal_met = (
            live_pct is not None
            and ((side == "SHORT" and live_pct >= 0.0) or (side == "LONG" and live_pct <= 0.0))
        )

        is_held = bool(cand.get("already_held")) or (ticker in (broker_state.get("held_symbols") or set()))
        ordered_today = (ticker in submitted_today) or (
            ticker in ((broker_state.get("entry_orders_by_side") or {}).get(side) or set())
        )

        reason_code: Optional[str] = None
        reason_text: Optional[str] = None
        if kill_active:
            reason_code, reason_text = "KILL_SWITCH", "Kill-Switch เปิดอยู่"
        elif not ledger_is_today and not allow_stale:
            reason_code, reason_text = "STALE_LEDGER", "ข้อมูล Report ไม่ใช่วันนี้"
        elif is_hold_cash and not override_hold_cash:
            reason_code, reason_text = "HOLD_CASH_LOCKED", "โหมด Hold Cash (0L:0S)"
        elif is_hold_cash and override_hold_cash and free_slots <= 0:
            reason_code, reason_text = "QUOTA_FULL", "พอร์ตเต็ม 7-Slot แล้ว"
        elif not is_hold_cash and remaining_for_side <= 0:
            reason_code, reason_text = "QUOTA_FULL", "โควตาฝั่งนี้ครบแล้ว"
        elif not broker_ready or not broker_state.get("available"):
            reason_code = "BROKER_UNAVAILABLE"
            reason_text = str(broker_state.get("error") or f"Session {route_broker.upper()} ไม่พร้อม")
        elif is_held:
            reason_code, reason_text = "ALREADY_HELD", "ถืออยู่แล้วในพอร์ต"
        elif ordered_today:
            reason_code, reason_text = "ORDERED_TODAY", "มีคำสั่งเข้าวันนี้แล้ว"
        elif not quote_ok or live_pct is None or lure_px is None:
            reason_code = "QUOTE_UNAVAILABLE"
            reason_text = str(quote_data.get("error") or lure_plan.get("reason") or "อ่านราคาสดไม่ได้")
        elif not price_signal_met:
            cond_txt = ">= 0%" if side == "SHORT" else "<= 0%"
            reason_code = "PCT_NOT_MET"
            reason_text = f"%Change {live_pct:+.2f}% ยังไม่เข้าเกณฑ์ ({cond_txt})"
        elif line_avail is not None and line_avail < (100 * float(ref_px or 0)):
            reason_code = "INSUFFICIENT_LINE"
            reason_text = f"วงเงิน {route_broker.upper()} คงเหลือ ฿{line_avail:,.0f} ไม่พอ"
        elif planned_shares < 100:
            reason_code = "ODD_LOT"
            reason_text = f"งบคำนวณได้ {planned_shares} หุ้น (< 100)"
        elif side == "SHORT" and xd_risk:
            reason_code = "XD_RISK"
            reason_text = xd_reason or "ติดเครื่องหมาย XD/XR ห้ามยืม"
        elif side == "SHORT" and (sbl_avail is None or int(sbl_avail) < planned_shares):
            avail_txt = f"{int(sbl_avail):,}" if sbl_avail is not None else "0"
            reason_code = "SBL_INSUFFICIENT"
            reason_text = f"คลัง SBL เหลือ {avail_txt} หุ้น (ไม่พอ {planned_shares:,})"

        selectable = reason_code is None
        return {
            "rank": cand["rank"],
            "ticker": ticker,
            "side": side,
            "order_side": order_side,
            "broker": route_broker,
            "account": route_account,
            "origin_pool": cand.get("origin_pool"),
            "pool_score": cand.get("pool_score"),
            "report_close": cand.get("close"),
            "report_pct_change": cand.get("report_pct_change"),
            "atr20_pct": cand.get("atr20_pct"),
            "atr_range_str": cand.get("atr_range_str"),
            "high_atr_warning": high_atr,
            "prob_pct": cand.get("prob_pct"),
            "last": last_px,
            "prior_close": prior_close,
            "bid_1": bid_1,
            "offer_1": offer_1,
            "live_pct_change": live_pct,
            "price_signal_met": price_signal_met,
            "lure_price": lure_px,
            "match_price": match_px,
            "match_allowed": match_px is not None,
            "edge_tier": sizing["edge_tier"],
            "edge_tier_label": sizing["edge_tier_label"],
            "base_multiplier": sizing["base_multiplier"],
            "house_money_multiplier": sizing["house_money_multiplier"],
            "effective_multiplier": sizing["effective_multiplier"],
            "effective_budget_thb": sizing["effective_budget_thb"],
            "planned_shares": planned_shares,
            "planned_value": planned_value,
            "sbl_available_vol": sbl_avail,
            "sbl_im_pct": cand.get("sbl_im_pct"),
            "xd_risk": xd_risk,
            "already_held": is_held,
            "ordered_today": ordered_today,
            "selectable": selectable,
            "disabled_reason_code": reason_code,
            "disabled_reason": reason_text,
        }

    @staticmethod
    def _assemble_poll_response(
        *,
        cur: datetime,
        today_str: str,
        phase: str,
        is_holiday: bool,
        holiday_name: Optional[str],
        is_trading_day: bool,
        is_dry: bool,
        budget_thb: float,
        ledger: Dict[str, Any],
        allow_stale: bool,
        override_hold_cash: bool,
        kill_active: bool,
        active_sides: List[str],
        long_route: Tuple[str, str],
        short_route: Tuple[str, str],
        ready_set: set[str],
        broker_states: Dict[str, Dict[str, Any]],
        submitted: Dict[str, Any],
        quotes_by_side: Dict[str, Dict[str, Dict[str, Any]]],
        sbl_by_sym: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Any]:
        quota = ledger.get("quota") or {}
        model_long = int(quota.get("model_long") or 0)
        model_short = int(quota.get("model_short") or 0)
        free_slots = int(quota.get("free_slots") or 0)
        eff_long = int(quota.get("effective_long") or 0)
        eff_short = int(quota.get("effective_short") or 0)
        eff_total = int(quota.get("effective_total") or 0)
        is_hold_cash = model_long == 0 and model_short == 0
        pools = ledger.get("pools") or {}

        live_ord_short: set[str] = set()
        live_ord_long: set[str] = set()
        for bs in broker_states.values():
            live_ord_short |= bs["entry_orders_by_side"]["SHORT"]
            live_ord_long |= bs["entry_orders_by_side"]["LONG"]
        used_short = submitted["short_symbols"] | live_ord_short
        used_long = submitted["long_symbols"] | live_ord_long
        used_total = len(used_short) + len(used_long)

        if is_hold_cash and override_hold_cash:
            rem_total = max(0, free_slots - used_total)
            rem_long = rem_total
            rem_short = rem_total
        else:
            rem_long = max(0, eff_long - len(used_long))
            rem_short = max(0, eff_short - len(used_short))
            rem_total = max(0, eff_total - used_total)
            rem_long = min(rem_long, rem_total)
            rem_short = min(rem_short, rem_total)

        probed: List[Dict[str, Any]] = []
        for s in active_sides:
            pool_rows = pools.get("short_top8" if s == "SHORT" else "long_top8") or []
            r_broker, r_account = short_route if s == "SHORT" else long_route
            b_state = broker_states[r_broker]
            rem_side = rem_short if s == "SHORT" else rem_long
            sub_side = submitted["short_symbols"] if s == "SHORT" else submitted["long_symbols"]
            side_quotes = quotes_by_side.get(s) or {}
            for cand in pool_rows:
                sym = cand["ticker"]
                probed.append(
                    SuggestEntryService._build_candidate_row(
                        cand,
                        route_broker=r_broker,
                        route_account=r_account,
                        broker_ready=r_broker in ready_set,
                        broker_state=b_state,
                        quote_data=side_quotes.get(sym) or {"available": False, "error": "ไม่มีข้อมูล quote"},
                        sbl_data=sbl_by_sym.get(sym) or {} if s == "SHORT" else {},
                        budget_thb=budget_thb,
                        ledger_is_today=bool(ledger.get("is_today")),
                        allow_stale=allow_stale,
                        kill_active=kill_active,
                        is_hold_cash=is_hold_cash,
                        override_hold_cash=override_hold_cash,
                        remaining_for_side=rem_side,
                        submitted_today=sub_side,
                        free_slots=free_slots,
                        sizing_reference=ledger.get("sizing_reference") or {},
                    )
                )

        def _sort_key(row: Dict[str, Any]) -> Tuple[int, float, int]:
            if row["selectable"]:
                pct = float(row.get("live_pct_change") or 0.0)
                strength = -pct if row["side"] == "SHORT" else pct
                return (0, strength, int(row.get("rank") or 99))
            side_order = 0 if row["side"] == "SHORT" else 1
            return (1 + side_order, 0.0, int(row.get("rank") or 99))

        probed.sort(key=_sort_key)

        return {
            "ledger_available": True,
            "error": None,
            "daily_date": today_str,
            "now_bkk": cur.strftime("%H:%M:%S"),
            "as_of": ledger.get("as_of"),
            "is_today": bool(ledger.get("is_today")),
            "allow_stale": bool(allow_stale),
            "market_phase": phase,
            "is_holiday": is_holiday,
            "holiday_name": holiday_name,
            "is_trading_day": is_trading_day,
            "dry_run": is_dry,
            "kill_switch": kill_switch_service.get_status(),
            "budget_per_slot_thb": budget_thb,
            "is_hold_cash": is_hold_cash,
            "override_hold_cash": bool(override_hold_cash),
            "regime": ledger.get("regime") or {},
            "quota": {
                **quota,
                "remaining_long": rem_long,
                "remaining_short": rem_short,
                "remaining_total": rem_total,
                "used_today": used_total,
            },
            "sizing_reference": ledger.get("sizing_reference") or {},
            "active_sides": active_sides,
            "brokers": {
                b: {
                    "ready": b in ready_set,
                    "available": bs["available"],
                    "line_available": bs["line_available"],
                    "error": bs["error"],
                }
                for b, bs in broker_states.items()
            },
            "candidates": probed,
            "selectable_count": sum(1 for r in probed if r["selectable"]),
        }

    async def _fetch_batch_quotes_for_sides(
        self,
        *,
        active_sides: List[str],
        pools: Dict[str, Any],
        ready_set: set[str],
        long_route: Tuple[str, str],
        short_route: Tuple[str, str],
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        """Fetch quotes in 1 batch per active side (instead of N separate CDP calls)."""
        tasks: Dict[str, Any] = {}
        for s in active_sides:
            r_broker = short_route[0] if s == "SHORT" else long_route[0]
            if r_broker not in ready_set:
                continue
            pool_rows = pools.get("short_top8" if s == "SHORT" else "long_top8") or []
            syms = [str(c.get("ticker") or "").upper() for c in pool_rows if c.get("ticker")]
            if syms:
                order_side = "SHORT" if s == "SHORT" else "BUY"
                tasks[s] = trading_facade.fetch_batch_nego_quotes(r_broker, syms, side=order_side)

        if not tasks:
            return {}
        keys = list(tasks.keys())
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        out: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for k, res in zip(keys, results):
            if isinstance(res, dict):
                out[k] = res
            else:
                out[k] = {}
        return out

    async def _fetch_sbl_for_short_pool(
        self,
        *,
        active_sides: List[str],
        pools: Dict[str, Any],
        short_broker: str,
        ready_set: set[str],
    ) -> Dict[str, Dict[str, Any]]:
        if "SHORT" not in active_sides or short_broker not in ready_set:
            return {}
        pool_rows = pools.get("short_top8") or []
        syms = [str(c.get("ticker") or "").upper() for c in pool_rows if c.get("ticker")]
        if not syms:
            return {}
        results = await asyncio.gather(
            *[trading_facade.fetch_sbl_pool_quote(sym) for sym in syms],
            return_exceptions=True,
        )
        out: Dict[str, Dict[str, Any]] = {}
        for sym, res in zip(syms, results):
            out[sym] = res if isinstance(res, dict) else {"error": str(res)}
        return out

    async def poll_candidates(
        self,
        *,
        allow_stale: bool = False,
        override_hold_cash: bool = False,
    ) -> Dict[str, Any]:
        """Run on-demand fast Batch Poll across Top-8 Merge Pool candidates."""
        cur = self._now()
        today_str = cur.strftime("%Y-%m-%d")
        is_holiday, holiday_name, _cal_err = runbook_exit_service._read_holiday_info(today_str)
        phase = classify_market_phase(cur, holiday_name=holiday_name)
        is_trading_day = phase not in ("WEEKEND", "HOLIDAY")

        state = self._load_state(cur)
        ledger = self._build_ledger(cur)
        budget_thb, _, _, _, _ = self._resolve_effective_budget(state, ledger)
        kill_active = kill_switch_service.is_active()
        is_dry = _is_dry_mode()

        if not ledger.get("ledger_available"):
            return {
                "ledger_available": False,
                "error": ledger.get("error") or "ไม่พบข้อมูล candidates_<DATE>.json",
                "daily_date": today_str,
                "now_bkk": cur.strftime("%H:%M:%S"),
                "market_phase": phase,
                "is_holiday": is_holiday,
                "holiday_name": holiday_name,
                "is_trading_day": is_trading_day,
                "dry_run": is_dry,
                "kill_switch": kill_switch_service.get_status(),
                "budget_per_slot_thb": budget_thb,
                "candidates": [],
            }

        quota = ledger.get("quota") or {}
        model_long = int(quota.get("model_long") or 0)
        model_short = int(quota.get("model_short") or 0)
        is_hold_cash = model_long == 0 and model_short == 0

        submitted = self._today_submitted_entries(state)
        long_route = account_routing.default_route_for_side("buy")
        short_route = account_routing.default_route_for_side("short")
        pools = ledger.get("pools") or {}

        if is_hold_cash:
            active_sides = ["SHORT", "LONG"]
        else:
            active_sides = [s for s, q in (("SHORT", model_short), ("LONG", model_long)) if q > 0]

        needed_brokers = sorted({(short_route[0] if s == "SHORT" else long_route[0]) for s in active_sides})
        ready_brokers, broker_errors, _crit = await probe_ready_brokers(needed_brokers)
        ready_set = set(ready_brokers)
        snap_brokers = [b for b in needed_brokers if b in ready_set]

        # Run portfolio+orders, batch quotes, and SBL checks concurrently!
        (port_snap, ord_snap), quotes_by_side, sbl_by_sym = await asyncio.gather(
            runbook_exit_service._fetch_live_snapshots(snap_brokers),
            self._fetch_batch_quotes_for_sides(
                active_sides=active_sides,
                pools=pools,
                ready_set=ready_set,
                long_route=long_route,
                short_route=short_route,
            ),
            self._fetch_sbl_for_short_pool(
                active_sides=active_sides,
                pools=pools,
                short_broker=short_route[0],
                ready_set=ready_set,
            ),
        )

        broker_states: Dict[str, Dict[str, Any]] = {}
        for b in needed_brokers:
            if b not in ready_set:
                broker_states[b] = {
                    "available": False,
                    "error": broker_errors.get(b) or f"Session {b.upper()} ไม่พร้อม",
                    "line_available": None,
                    "held_symbols": set(),
                    "entry_orders_by_side": {"LONG": set(), "SHORT": set()},
                }
            else:
                broker_states[b] = self._extract_live_broker_state(port_snap, ord_snap, b)

        self._last_poll_ctx = {
            "ts": time.monotonic(),
            "key": (today_str, bool(allow_stale), bool(override_hold_cash)),
            "ledger": ledger,
            "active_sides": active_sides,
            "long_route": long_route,
            "short_route": short_route,
            "ready_set": ready_set,
            "broker_states": broker_states,
            "sbl_by_sym": sbl_by_sym,
            "is_holiday": is_holiday,
            "holiday_name": holiday_name,
            "is_trading_day": is_trading_day,
        }

        return self._assemble_poll_response(
            cur=cur,
            today_str=today_str,
            phase=phase,
            is_holiday=is_holiday,
            holiday_name=holiday_name,
            is_trading_day=is_trading_day,
            is_dry=is_dry,
            budget_thb=budget_thb,
            ledger=ledger,
            allow_stale=allow_stale,
            override_hold_cash=override_hold_cash,
            kill_active=kill_active,
            active_sides=active_sides,
            long_route=long_route,
            short_route=short_route,
            ready_set=ready_set,
            broker_states=broker_states,
            submitted=submitted,
            quotes_by_side=quotes_by_side,
            sbl_by_sym=sbl_by_sym,
        )

    async def poll_live_quotes(
        self,
        *,
        allow_stale: bool = False,
        override_hold_cash: bool = False,
    ) -> Dict[str, Any]:
        """Lightweight ~150ms live quote tick reusing recent portfolio/order/SBL context."""
        cur = self._now()
        today_str = cur.strftime("%Y-%m-%d")
        ctx = self._last_poll_ctx
        if (
            not ctx
            or ctx.get("key") != (today_str, bool(allow_stale), bool(override_hold_cash))
            or (time.monotonic() - float(ctx.get("ts") or 0.0)) > _POLL_CTX_TTL_S
        ):
            return await self.poll_candidates(
                allow_stale=allow_stale,
                override_hold_cash=override_hold_cash,
            )

        state = self._load_state(cur)
        submitted = self._today_submitted_entries(state)
        ledger = ctx["ledger"]
        budget_thb, _, _, _, _ = self._resolve_effective_budget(state, ledger)
        pools = ledger.get("pools") or {}
        quotes_by_side = await self._fetch_batch_quotes_for_sides(
            active_sides=ctx["active_sides"],
            pools=pools,
            ready_set=ctx["ready_set"],
            long_route=ctx["long_route"],
            short_route=ctx["short_route"],
        )

        phase = classify_market_phase(cur, holiday_name=ctx["holiday_name"])
        return self._assemble_poll_response(
            cur=cur,
            today_str=today_str,
            phase=phase,
            is_holiday=ctx["is_holiday"],
            holiday_name=ctx["holiday_name"],
            is_trading_day=ctx["is_trading_day"],
            is_dry=_is_dry_mode(),
            budget_thb=budget_thb,
            ledger=ledger,
            allow_stale=allow_stale,
            override_hold_cash=override_hold_cash,
            kill_active=kill_switch_service.is_active(),
            active_sides=ctx["active_sides"],
            long_route=ctx["long_route"],
            short_route=ctx["short_route"],
            ready_set=ctx["ready_set"],
            broker_states=ctx["broker_states"],
            submitted=submitted,
            quotes_by_side=quotes_by_side,
            sbl_by_sym=ctx["sbl_by_sym"],
        )

    async def submit_selected(
        self,
        items: List[Dict[str, Any]],
        *,
        allow_stale: bool = False,
        override_hold_cash: bool = False,
    ) -> Dict[str, Any]:
        """Execute user-confirmed Multiple-Choice Suggest Entry selections."""
        if not items:
            raise SuggestEntryError(422, "E_EMPTY_SELECTION", "กรุณาเลือกหุ้นอย่างน้อย 1 ตัว")

        normalized_req: List[Dict[str, str]] = []
        seen_keys: set[Tuple[str, str]] = set()
        for raw in items:
            sym = str(raw.get("symbol") or "").strip().upper()
            side = str(raw.get("side") or "").strip().upper()
            pmode = str(raw.get("price_mode") or "lure").strip().lower()
            if not sym or side not in ("LONG", "SHORT"):
                raise SuggestEntryError(422, "E_BAD_ITEM", f"ข้อมูลรายการไม่ถูกต้อง: {raw}")
            if side == "SHORT":
                pmode = "lure"
            elif pmode not in ("lure", "match"):
                pmode = "lure"
            key = (sym, side)
            if key in seen_keys:
                raise SuggestEntryError(422, "E_DUPLICATE_SELECTION", f"เลือก {sym} ({side}) ซ้ำกัน")
            seen_keys.add(key)
            normalized_req.append({"symbol": sym, "side": side, "price_mode": pmode})

        async with self._exec_lock:
            cur = self._now()
            today_str = cur.strftime("%Y-%m-%d")
            is_dry = _is_dry_mode()

            if kill_switch_service.is_active():
                raise SuggestEntryError(423, "CLI_KILL_SWITCH", "KILL-SWITCH ACTIVE — ระงับคำสั่งซื้อขายทั้งหมด")

            is_holiday, holiday_name, _cal = runbook_exit_service._read_holiday_info(today_str)
            phase = classify_market_phase(cur, holiday_name=holiday_name)
            if not is_dry and phase in ("WEEKEND", "HOLIDAY"):
                msg = f"วันหยุดตลาด SET ({holiday_name})" if is_holiday else "ตลาดปิด (เสาร์–อาทิตย์)"
                raise SuggestEntryError(422, "E_MARKET_CLOSED", msg)

            poll = await self.poll_candidates(
                allow_stale=allow_stale,
                override_hold_cash=override_hold_cash,
            )
            if not poll.get("ledger_available"):
                raise SuggestEntryError(422, "E_NO_LEDGER", str(poll.get("error") or "ไม่พบ entry_ledger"))
            if not poll.get("is_today") and not allow_stale:
                raise SuggestEntryError(
                    422,
                    "E_STALE_REPORT",
                    f"ข้อมูล Report เป็นวันที่ {poll.get('as_of') or 'ไม่ทราบ'} (ไม่ใช่ {today_str})",
                )

            quota = poll["quota"]
            rem_total = int(quota.get("remaining_total") or 0)
            rem_long = int(quota.get("remaining_long") or 0)
            rem_short = int(quota.get("remaining_short") or 0)

            req_long = sum(1 for x in normalized_req if x["side"] == "LONG")
            req_short = sum(1 for x in normalized_req if x["side"] == "SHORT")
            if len(normalized_req) > rem_total or req_long > rem_long or req_short > rem_short:
                raise SuggestEntryError(
                    422,
                    "E_QUOTA_EXCEEDED",
                    f"เลือก {len(normalized_req)} ตัว เกินโควตาคงเหลือ ({rem_total} ช่อง: {rem_long}L / {rem_short}S)",
                )

            cand_map = {(c["ticker"], c["side"]): c for c in poll["candidates"]}
            for req in normalized_req:
                cand = cand_map.get((req["symbol"], req["side"]))
                if cand is None:
                    raise SuggestEntryError(
                        422,
                        "E_NOT_IN_TOP8",
                        f"{req['symbol']} ({req['side']}) ไม่อยู่ใน Top 8 ของ Merge Pool",
                    )
                if not cand.get("selectable"):
                    raise SuggestEntryError(
                        422,
                        str(cand.get("disabled_reason_code") or "E_NOT_ELIGIBLE"),
                        f"{req['symbol']}: {cand.get('disabled_reason') or 'ไม่ผ่านเกณฑ์'}",
                    )

            state = self._load_state(cur)
            budget_thb, _, _, _, _ = self._resolve_effective_budget(state, poll)
            remaining_line: Dict[str, Optional[float]] = {
                b: (info.get("line_available") if isinstance(info, dict) else None)
                for b, info in (poll.get("brokers") or {}).items()
            }

            batch_id = uuid.uuid4().hex[:8]
            results_by_idx: Dict[int, Dict[str, Any]] = {}
            stranded_borrows: List[str] = []

            # Group by broker so distinct brokers execute concurrently while same-broker orders stay serialized
            broker_groups: Dict[str, List[Tuple[int, Dict[str, str]]]] = {}
            for idx, req in enumerate(normalized_req):
                b = cand_map[(req["symbol"], req["side"])]["broker"]
                broker_groups.setdefault(b, []).append((idx, req))

            async def _run_broker_group(broker: str, group: List[Tuple[int, Dict[str, str]]]) -> None:
                b_line = remaining_line.get(broker)
                for idx, req in group:
                    sym = req["symbol"]
                    side = req["side"]
                    pmode = req["price_mode"]
                    cand = cand_map[(sym, side)]
                    account = cand["account"]
                    target_px = (
                        cand.get("match_price")
                        if (cand.get("match_allowed") and pmode == "match" and cand.get("match_price"))
                        else cand.get("lure_price")
                    )
                    if not target_px or float(target_px) <= 0:
                        results_by_idx[idx] = {
                            "symbol": sym,
                            "side": side,
                            "ok": False,
                            "status": "FAILED",
                            "error": "ไม่มีราคาเป้าหมายจาก quote สด",
                        }
                        continue

                    slot_budget = float(cand.get("effective_budget_thb") or budget_thb)
                    eff_budget = min(slot_budget, float(b_line)) if (b_line is not None and b_line >= 0) else slot_budget
                    shares = int((eff_budget / float(target_px)) // 100) * 100
                    if shares < 100:
                        results_by_idx[idx] = {
                            "symbol": sym,
                            "side": side,
                            "ok": False,
                            "status": "FAILED",
                            "error": f"วงเงินคงเหลือ {broker.upper()} ไม่พอ 100 หุ้น",
                        }
                        continue

                    idem = f"suggest:{today_str}:{batch_id}:{sym}:{side}"
                    if side == "LONG":
                        item_res = await self._submit_long_order(
                            sym=sym,
                            broker=broker,
                            account=account,
                            shares=shares,
                            price=float(target_px),
                            pmode=pmode,
                            idem=idem,
                        )
                    else:
                        item_res = await self._submit_short_order(
                            sym=sym,
                            shares=shares,
                            price=float(target_px),
                            idem=idem,
                        )
                        if item_res.get("status") == "BORROWED_SHORT_FAILED":
                            stranded_borrows.append(f"{sym} {shares:,} หุ้น: {item_res.get('error')}")

                    if item_res.get("ok") and b_line is not None:
                        spent = float(item_res.get("shares") or shares) * float(item_res.get("price") or target_px)
                        b_line = max(0.0, float(b_line) - spent)
                        remaining_line[broker] = b_line

                    results_by_idx[idx] = item_res

            await asyncio.gather(*[_run_broker_group(b, grp) for b, grp in broker_groups.items()])
            results = [results_by_idx[i] for i in range(len(normalized_req))]

            for req, item_res in zip(normalized_req, results):
                cand = cand_map[(req["symbol"], req["side"])]
                state["submissions"].append(
                    {
                        "ts": self._now().isoformat(timespec="seconds"),
                        "symbol": req["symbol"],
                        "side": req["side"],
                        "broker": cand["broker"],
                        "account": cand["account"],
                        "shares": item_res.get("shares") or cand.get("planned_shares") or 0,
                        "price": item_res.get("price") or cand.get("lure_price"),
                        "price_mode": req["price_mode"],
                        "status": item_res.get("status"),
                        "order_no": item_res.get("order_no"),
                        "error": item_res.get("error"),
                    }
                )

            self._last_poll_ctx = None
            self._save_state()

            ok_cnt = sum(1 for r in results if r.get("ok"))
            fail_cnt = len(results) - ok_cnt
            line_pushed = False
            if stranded_borrows:
                line_pushed = await notify_critical_error_once(
                    state,
                    error_key=f"se_stranded_sbl_{batch_id}",
                    title="🚨 Suggest Entry: ยืม SBL แล้วแต่ส่ง SHORT ไม่สำเร็จ",
                    detail="\n".join(stranded_borrows[:5]),
                    category="trade",
                    action_tab="trade",
                    schedule_label="Suggest Entry",
                )
                self._save_state()

            summary_lines = [
                f"{r['symbol']} {r['side']} {int(r.get('shares') or 0):,} @ ฿{r.get('price') or '-'}: {r['status']}"
                for r in results
            ]
            try:
                await miniapp_inbox.publish(
                    category="trade",
                    severity="warning" if fail_cnt > 0 else "info",
                    title=f"🎯 Suggest Entry: สำเร็จ {ok_cnt}/{len(results)} ไม้",
                    body="\n".join(summary_lines),
                    action_tab="orders",
                    line_pushed=line_pushed,
                )
            except Exception as exc:
                logger.debug("MiniApp inbox publish skipped: %s", exc)

            return {
                "ok": fail_cnt == 0,
                "dry_run": is_dry,
                "batch_id": batch_id,
                "submitted_count": ok_cnt,
                "failed_count": fail_cnt,
                "line_status": format_error_line_status(line_pushed) if stranded_borrows else "งดส่ง LINE (ดูผลใน MiniApp)",
                "results": results,
            }

    @staticmethod
    async def _submit_long_order(
        *,
        sym: str,
        broker: str,
        account: str,
        shares: int,
        price: float,
        pmode: str,
        idem: str,
    ) -> Dict[str, Any]:
        try:
            res = await trading_facade.execute_trade_with_idempotency(
                idempotency_key=idem,
                notify_line=False,
                side="buy",
                symbol=sym,
                broker=broker,
                account=account,
                amount_mode="volume",
                volume=shares,
                price=price,
            )
            ord_info = res.get("order") or {}
            exec_info = res.get("execution") or {}
            is_dry = bool(res.get("dry_run"))
            return {
                "symbol": sym,
                "side": "LONG",
                "broker": broker,
                "account": account,
                "price_mode": pmode,
                "ok": True,
                "dry_run": is_dry,
                "status": "DRY_RUN" if is_dry else "EXECUTED",
                "shares": int(ord_info.get("volume") or shares),
                "price": float(ord_info.get("price") or price),
                "order_no": str(exec_info.get("broker_order_no") or res.get("draft_id") or "") or None,
                "error": None,
            }
        except trading_facade.FacadeHttpError as exc:
            return {
                "symbol": sym,
                "side": "LONG",
                "broker": broker,
                "account": account,
                "price_mode": pmode,
                "ok": False,
                "dry_run": _is_dry_mode(),
                "status": "FAILED",
                "shares": shares,
                "price": price,
                "order_no": None,
                "error": f"[{exc.code}] {exc.message}",
            }
        except Exception as exc:
            return {
                "symbol": sym,
                "side": "LONG",
                "broker": broker,
                "account": account,
                "price_mode": pmode,
                "ok": False,
                "dry_run": _is_dry_mode(),
                "status": "FAILED",
                "shares": shares,
                "price": price,
                "order_no": None,
                "error": str(exc),
            }

    @staticmethod
    async def _submit_short_order(
        *,
        sym: str,
        shares: int,
        price: float,
        idem: str,
    ) -> Dict[str, Any]:
        try:
            res = await trading_facade.execute_sbl_action(
                action="borrow",
                symbol=sym,
                volume=shares,
                amount_mode="volume",
                auto_short=True,
                short_price=price,
                idempotency_key=idem,
            )
        except trading_facade.FacadeHttpError as exc:
            return {
                "symbol": sym,
                "side": "SHORT",
                "broker": "kss",
                "account": "margin",
                "price_mode": "lure",
                "ok": False,
                "dry_run": _is_dry_mode(),
                "status": "FAILED",
                "shares": shares,
                "price": price,
                "order_no": None,
                "error": f"[{exc.code}] {exc.message}",
            }
        except Exception as exc:
            return {
                "symbol": sym,
                "side": "SHORT",
                "broker": "kss",
                "account": "margin",
                "price_mode": "lure",
                "ok": False,
                "dry_run": _is_dry_mode(),
                "status": "FAILED",
                "shares": shares,
                "price": price,
                "order_no": None,
                "error": str(exc),
            }

        follow = res.get("auto_short") or {}
        if follow.get("ok"):
            ord_info = follow.get("order") or {}
            exec_info = follow.get("execution") or {}
            is_dry = bool(res.get("dry_run") or follow.get("dry_run"))
            return {
                "symbol": sym,
                "side": "SHORT",
                "broker": "kss",
                "account": "margin",
                "price_mode": "lure",
                "ok": True,
                "dry_run": is_dry,
                "status": "DRY_RUN" if is_dry else "EXECUTED",
                "shares": int(ord_info.get("volume") or follow.get("volume") or shares),
                "price": float(ord_info.get("price") or follow.get("price") or price),
                "order_no": str(exec_info.get("broker_order_no") or follow.get("draft_id") or "") or None,
                "sbl_borrowed": True,
                "error": None,
            }

        # Borrow succeeded, follow-up SHORT failed -> self-healing retry +1 on any broker/validator uptick rejection
        err_code = str(follow.get("code") or "")
        err_text = str(follow.get("error") or "")
        from app.services import trading_rules

        is_uptick_fail = trading_rules.is_short_uptick_rejection(err_code, err_text)
        if is_uptick_fail:
            trading_rules.mark_runtime_strict_symbol(sym)
            try:
                q = await trading_facade.compute_nego_quote(broker="kss", symbol=sym, side="SHORT")
                fresh_last = q.get("last")
                if fresh_last and float(fresh_last) > 0:
                    retry_px = validator.get_short_min_price(float(fresh_last), symbol=sym, min_ticks=1)
                    retry_res = await trading_facade.execute_trade_with_idempotency(
                        idempotency_key=f"{idem}:retry1",
                        notify_line=False,
                        side="short",
                        symbol=sym,
                        broker="kss",
                        account="margin",
                        amount_mode="volume",
                        volume=shares,
                        price=float(retry_px),
                    )
                    ord_info = retry_res.get("order") or {}
                    exec_info = retry_res.get("execution") or {}
                    is_dry = bool(retry_res.get("dry_run"))
                    return {
                        "symbol": sym,
                        "side": "SHORT",
                        "broker": "kss",
                        "account": "margin",
                        "price_mode": "lure",
                        "ok": True,
                        "dry_run": is_dry,
                        "status": "DRY_RUN" if is_dry else "EXECUTED",
                        "shares": int(ord_info.get("volume") or shares),
                        "price": float(ord_info.get("price") or retry_px),
                        "order_no": str(exec_info.get("broker_order_no") or retry_res.get("draft_id") or "") or None,
                        "sbl_borrowed": True,
                        "retried_uptick": True,
                        "error": None,
                    }
            except Exception as retry_exc:
                follow = {"code": "E_UPTICK_RETRY_FAILED", "error": str(retry_exc)}

        return {
            "symbol": sym,
            "side": "SHORT",
            "broker": "kss",
            "account": "margin",
            "price_mode": "lure",
            "ok": False,
            "dry_run": bool(res.get("dry_run")),
            "status": "BORROWED_SHORT_FAILED",
            "shares": shares,
            "price": price,
            "order_no": None,
            "sbl_borrowed": True,
            "error": str(follow.get("error") or follow.get("code") or "ยืมหุ้นสำเร็จแต่ส่ง SHORT ไม่สำเร็จ"),
        }


suggest_entry_service = SuggestEntryService()
