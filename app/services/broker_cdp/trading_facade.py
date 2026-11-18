"""Shared Pure-CDP Trading & Portfolio Facade for LINE MiniApp & LINE CLI (Phases 2–5).

Consolidates Zero-DB CDP reads, pre-trade validation, amend/cancel guards,
SBL actions, single-flight order idempotency, and best-effort LINE receipts
without duplicating broker logic across endpoints and chat handlers.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import time
import uuid
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx

from app.brokers.krungsri.sbl_realtime_driver import krungsri_sbl_driver, plan_fifo_return_lots
from app.core.config import settings
from app.mcp.pillars.pillar2_portfolio import _classify_order_category
from app.services import account_routing, trading_rules
from app.services.broker_cdp.order_executor import execute_confirmed_order
from app.services.broker_cdp.sync_service import (
    get_active_kss_account,
    get_active_kss_account_type,
    map_ledger_side,
    scrape_orders_from_cdp,
    scrape_portfolio_from_cdp,
    switch_kss_account,
)
from app.services.kill_switch_service import kill_switch_service
from app.services.line_cli import cards
from app.services.line_cli.handlers import (
    _CAT_KIND,
    _broker_cancel,
    _broker_modify,
    _broker_quote,
    _get_active_sbl_lots,
    _merge_borrow,
    _normalise_holdings,
    _portfolio_amount,
    _portfolio_totals,
    _pre_trade_check,
    _sbl_borrow_summary,
    _split_borrowed,
    clear_cdp_portfolio_cache,
)
from app.services.line_cli.parser import TradeOrder
from app.services.nego_pricer import plan_price
from app.services.sbl_intelligence_service import sbl_intelligence_service
from app.services.trade_dispatcher import trade_dispatcher
from app.services.validator import validator

logger = logging.getLogger(__name__)

_IDEMPOTENCY_TTL_S = 300.0
_IDEMPOTENCY_MAX = 128
_TRADE_IDEMPOTENCY: Dict[str, Dict[str, Any]] = {}
_TRADE_LOCKS: Dict[str, asyncio.Lock] = {}

_COOKIE_CACHE_TTL_S = 180.0
_BROKER_COOKIE_CACHE: Dict[str, Dict[str, Any]] = {}
_LIB_ORDERBOOK_MAP: Dict[str, int] = {}
_BATCH_QUOTE_LOCKS: Dict[str, asyncio.Lock] = {}


class FacadeHttpError(Exception):
    """Structured error mapped cleanly to HTTPException by the FastAPI endpoint."""

    def __init__(self, status_code: int, code: str, message: str, extra: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}


def allowed_kss_accounts() -> set[str]:
    """Return allowed Krungsri account numbers (margin + cash) from .env settings."""
    accounts = {
        str(getattr(settings, "STREAMING_ACCOUNT_NO_MARGIN", "") or "").strip(),
        str(getattr(settings, "STREAMING_ACCOUNT_NO_CASH", "") or "").strip(),
        str(getattr(settings, "STREAMING_ACCOUNT_NO", "") or "").strip(),
    }
    return {a for a in accounts if a}


def build_miniapp_url(tab: str = "brokers", params: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """Build deep-link URL to LINE MiniApp (e.g. https://miniapp.line.me/<LIFF_ID>?tab=...)."""
    liff_id = (getattr(settings, "LIFF_ID", None) or "").strip()
    if not liff_id:
        return None
    base = (getattr(settings, "MINIAPP_PUBLIC_BASE_URL", None) or "https://miniapp.line.me").rstrip("/")
    query: Dict[str, Any] = {"tab": tab}
    if params:
        for k, v in params.items():
            if v is not None and str(v).strip() != "":
                query[k] = v
    return f"{base}/{liff_id}?{urlencode(query)}"


async def best_effort_push_card(
    card: Dict[str, Any],
    *,
    category: str = "trade",
    action_tab: str = "orders",
    inbox_title: Optional[str] = None,
) -> bool:
    """Route a trading card notification via line_service (MiniApp Inbox in eod_only mode)."""
    try:
        from app.services.line_service import line_service, push_context

        mode = (getattr(settings, "LINE_PUSH_MODE", "eod_only") or "eod_only").strip().lower()
        target = (settings.LINE_GROUP_ID or settings.LINE_ADMIN_USER_ID or "").strip()
        if mode == "all" and not target:
            return False
        if not hasattr(line_service, "push_message"):
            return False
        with push_context(category=category, inbox_title=inbox_title, action_tab=action_tab):
            if target:
                return bool(await line_service.push_message(card, to_user_id=target))
            return bool(await line_service.push_message(card))
    except Exception as exc:
        logger.debug("Best-effort LINE push skipped/throttled: %s", exc)
        return False


def _bkk_ts() -> str:
    return datetime.now(timezone(timedelta(hours=7))).strftime("%H:%M:%S")


def _is_dry_mode() -> bool:
    return (not bool(trade_dispatcher.is_live)) or bool(getattr(settings, "DRY_RUN_MODE", False))


def _ensure_not_killed() -> None:
    if kill_switch_service.is_active():
        raise FacadeHttpError(
            423,
            "CLI_KILL_SWITCH",
            "KILL-SWITCH ACTIVE — ระงับคำสั่งซื้อขาย/แก้ราคา/SBL ทั้งหมด กรุณา RESET ก่อน",
        )


def _prune_idempotency() -> None:
    now = time.monotonic()
    expired = [k for k, v in _TRADE_IDEMPOTENCY.items() if now - float(v.get("ts", 0.0)) > _IDEMPOTENCY_TTL_S]
    for k in expired:
        _TRADE_IDEMPOTENCY.pop(k, None)
        _TRADE_LOCKS.pop(k, None)
    if len(_TRADE_IDEMPOTENCY) >= _IDEMPOTENCY_MAX:
        oldest = sorted(_TRADE_IDEMPOTENCY.items(), key=lambda kv: kv[1].get("ts", 0.0))
        for k, _ in oldest[: len(_TRADE_IDEMPOTENCY) // 2]:
            _TRADE_IDEMPOTENCY.pop(k, None)
            _TRADE_LOCKS.pop(k, None)


def _to_float_or_none(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _compute_account_net_equity(
    cash_balance: Any,
    main_rows: List[Dict[str, Any]],
    *,
    account_equity: Any = None,
) -> Dict[str, Optional[float]]:
    """Compute Net Liquidation Equity = cash_balance + long_mv - short_liability.

    Excludes BORROWED ('(B)') rows, Line Available (margin buying power), and Credit Limit.
    Falls back to ``account_equity`` (Streaming Account Info Equity) when cash_balance is
    unavailable, else returns ``net_equity: None`` (Anti-Mock).
    """
    long_mv = 0.0
    short_liab = 0.0
    for r in main_rows or []:
        pt = str(r.get("position_type") or "LONG").strip().upper()
        if pt == "BORROWED":
            continue
        mv = _to_float_or_none(r.get("market_value"))
        if mv is None:
            vol = _to_float_or_none(r.get("volume"))
            last = _to_float_or_none(r.get("last"))
            if vol is not None and last is not None:
                mv = vol * last
        if mv is None:
            continue
        if pt == "SHORT":
            short_liab += abs(mv)
        else:
            long_mv += mv
    cb = _to_float_or_none(cash_balance)
    acct_eq = _to_float_or_none(account_equity)
    if cb is not None:
        net_equity: Optional[float] = round(cb + long_mv - short_liab, 2)
    elif acct_eq is not None:
        net_equity = round(acct_eq, 2)
    else:
        net_equity = None
    return {
        "net_equity": net_equity,
        "long_market_value": round(long_mv, 2),
        "short_liability": round(short_liab, 2),
    }


def _selected_accounts_from_routing() -> tuple[List[str], Dict[str, List[str]]]:
    """Return ordered unique account keys and role tags selected in account_routing."""
    cfg = account_routing.get_account_routing()
    roles_by_key: Dict[str, List[str]] = {}
    ordered_keys: List[str] = []
    pairs = [
        ("SHORT", str(cfg.get("short_broker") or "kss").lower(), str(cfg.get("short_account") or "margin").lower()),
        ("LONG", str(cfg.get("long_broker") or "lib").lower(), str(cfg.get("long_account") or "cash").lower()),
    ]
    for role, brk, acct in pairs:
        if brk == "kss":
            key = "kss_margin" if acct == "margin" else "kss_cash"
        elif brk == "lib":
            key = "lib_cash"
        else:
            continue
        if key not in roles_by_key:
            roles_by_key[key] = []
            ordered_keys.append(key)
        if role not in roles_by_key[key]:
            roles_by_key[key].append(role)
    return ordered_keys, roles_by_key


def _kss_account_key_and_label(account_no: Optional[str], account_type: Optional[str], margin_acct: str) -> tuple[str, str]:
    acct_s = str(account_no or "").strip()
    atype_s = str(account_type or "").strip().upper()
    is_margin = acct_s == margin_acct or "CREDIT" in atype_s or "MARGIN" in atype_s
    if is_margin:
        return "kss_margin", "Krungsri · Margin"
    return "kss_cash", "Krungsri · Cash"


def _build_kss_secondary_book(
    snap: Dict[str, Any],
    *,
    expected_acct: str,
    margin_acct: str,
    selected_in_routing: bool = False,
    routing_roles: Optional[List[str]] = None,
) -> Dict[str, Any]:
    acct_no = str(snap.get("account_no") or "").strip() or None
    acct_type = str(snap.get("account_type") or "CASH_BALANCE").upper()
    key, label = _kss_account_key_and_label(expected_acct, acct_type, margin_acct)
    err = snap.get("account_switch_error")
    if acct_no != expected_acct:
        return {
            "key": key,
            "broker": "kss",
            "label": label,
            "available": False,
            "selected_in_routing": selected_in_routing,
            "routing_roles": list(routing_roles or []),
            "error": err or f"สลับเป็นบัญชี {expected_acct} ไม่สำเร็จ (อ่านได้ {acct_no or '-'})",
            "account_no": expected_acct,
            "account_type": "CREDIT_BALANCE" if expected_acct == margin_acct else "CASH_BALANCE",
            "cash_balance": None,
            "line_available": None,
            "excess_equity": None,
            "margin_required": None,
            "total_equity": None,
            "long_market_value": None,
            "short_liability": None,
            "net_equity": None,
            "unrealized_pnl": None,
            "rows": [],
        }

    rows = _normalise_holdings(snap.get("holdings", []))
    for r in rows:
        r["broker"] = "kss"
        r["account_no"] = acct_no
        r["account_type"] = acct_type
    main_rows, _borrowed = _split_borrowed(rows)
    _, pnl = _portfolio_totals(main_rows)
    eq = _compute_account_net_equity(
        snap.get("cash_balance"),
        main_rows,
        account_equity=snap.get("total_equity"),
    )
    return {
        "key": key,
        "broker": "kss",
        "label": label,
        "available": True,
        "selected_in_routing": selected_in_routing,
        "routing_roles": list(routing_roles or []),
        "error": err,
        "account_no": acct_no,
        "account_type": acct_type,
        "cash_balance": _to_float_or_none(snap.get("cash_balance")),
        "line_available": _to_float_or_none(snap.get("line_available")),
        "excess_equity": _to_float_or_none(snap.get("excess_equity")),
        "margin_required": _to_float_or_none(snap.get("margin_required")),
        "total_equity": _to_float_or_none(snap.get("total_equity")),
        "long_market_value": eq["long_market_value"],
        "short_liability": eq["short_liability"],
        "net_equity": eq["net_equity"],
        "unrealized_pnl": pnl,
        "rows": main_rows,
    }


async def fetch_portfolio_overview(
    broker: str = "all",
    kss_account: Optional[str] = None,
    force_sbl: bool = False,
) -> Dict[str, Any]:
    """Read live CDP portfolios across selected accounts without mocking unavailable brokers."""
    norm = (broker or "all").strip().lower()
    if norm not in ("all", "kss", "lib"):
        raise FacadeHttpError(400, "E_BAD_BROKER", f"Unsupported broker '{broker}'.")

    if kss_account is not None:
        acct = str(kss_account).strip()
        allowed = allowed_kss_accounts()
        if allowed and acct not in allowed:
            raise FacadeHttpError(
                400,
                "E_BAD_ACCOUNT",
                f"KSS account '{acct}' is invalid.",
            )
        sw = await switch_kss_account(acct, target="streaming")
        if not sw.get("ok"):
            raise FacadeHttpError(
                502,
                "KSS_ACCOUNT_SWITCH_FAILED",
                str(sw.get("error") or f"ไม่สามารถสลับเป็นบัญชี KSS {acct} ได้"),
            )

    margin_acct = str(getattr(settings, "STREAMING_ACCOUNT_NO_MARGIN", "") or "").strip()
    cash_acct = str(getattr(settings, "STREAMING_ACCOUNT_NO_CASH", "") or "").strip()
    selected_keys, roles_by_key = _selected_accounts_from_routing()
    selected_set = set(selected_keys)

    if norm == "all":
        targets: List[str] = []
        if "kss_margin" in selected_set or "kss_cash" in selected_set:
            targets.append("kss")
        if "lib_cash" in selected_set:
            targets.append("lib")
        if not targets:
            targets = ["kss", "lib"]
    else:
        targets = [norm]

    brokers_out: Dict[str, Dict[str, Any]] = {}
    accounts_out: List[Dict[str, Any]] = []
    sbl_lots_out: List[Dict[str, Any]] = []
    sbl_source = "none"
    kss_primary_live_acct: Optional[str] = None
    kss_primary_key: Optional[str] = None

    for b in targets:
        try:
            snap = await scrape_portfolio_from_cdp(b)
            if not isinstance(snap, dict):
                raise RuntimeError("empty portfolio snapshot")
        except Exception as exc:
            brokers_out[b] = {
                "broker": b,
                "label": "Krungsri (KSS)" if b == "kss" else "Liberator",
                "available": False,
                "error": str(exc),
                "account_no": None,
                "account_type": None,
                "cash_balance": None,
                "line_available": None,
                "excess_equity": None,
                "margin_required": None,
                "total_equity": None,
                "long_market_value": None,
                "short_liability": None,
                "net_equity": None,
                "amount": None,
                "market_value": None,
                "unrealized_pnl": None,
                "rows": [],
                "borrowed": [],
            }
            if b == "lib":
                accounts_out.append(
                    {
                        "key": "lib_cash",
                        "broker": "lib",
                        "label": "Liberator · Cash",
                        "available": False,
                        "selected_in_routing": "lib_cash" in selected_set,
                        "routing_roles": list(roles_by_key.get("lib_cash") or []),
                        "error": str(exc),
                        "account_no": None,
                        "account_type": "CASH_BALANCE",
                        "cash_balance": None,
                        "line_available": None,
                        "excess_equity": None,
                        "margin_required": None,
                        "total_equity": None,
                        "long_market_value": None,
                        "short_liability": None,
                        "net_equity": None,
                        "unrealized_pnl": None,
                        "rows": [],
                    }
                )
            else:
                for k_key in [k for k in selected_keys if k.startswith("kss_")]:
                    k_acct = margin_acct if k_key == "kss_margin" else cash_acct
                    _, k_lbl = _kss_account_key_and_label(k_acct, None, margin_acct)
                    accounts_out.append(
                        {
                            "key": k_key,
                            "broker": "kss",
                            "label": k_lbl,
                            "available": False,
                            "selected_in_routing": True,
                            "routing_roles": list(roles_by_key.get(k_key) or []),
                            "error": str(exc),
                            "account_no": k_acct,
                            "account_type": "CREDIT_BALANCE" if k_key == "kss_margin" else "CASH_BALANCE",
                            "cash_balance": None,
                            "line_available": None,
                            "excess_equity": None,
                            "margin_required": None,
                            "total_equity": None,
                            "long_market_value": None,
                            "short_liability": None,
                            "net_equity": None,
                            "unrealized_pnl": None,
                            "rows": [],
                        }
                    )
            continue

        from_live_cdp = bool(snap.get("_from_live_cdp"))
        acct_no_str = str(snap.get("account_no") or "") or None
        acct_type_str = str(snap.get("account_type") or "CASH_BALANCE").upper()

        borrow_summary: Dict[str, Dict[str, Any]] = {}
        if b == "kss":
            if from_live_cdp and acct_no_str:
                kss_primary_live_acct = acct_no_str
            try:
                raw_lots = await krungsri_sbl_driver.scrape_sbl_lots(force_refresh=force_sbl)
                borrow_summary = _sbl_borrow_summary(raw_lots or [])
                sbl_source = "live" if force_sbl else "cache"
                for lot in raw_lots or []:
                    rem = int(lot.get("remaining_qty") or 0)
                    if lot.get("status") in ("ACTIVE", "PARTIAL", "PARTIAL_RETURNED") and rem > 0:
                        sym = str(lot.get("symbol") or "").upper()
                        sbl_lots_out.append(
                            {
                                "lot_id": lot.get("lot_id") or lot.get("broker_tx_id"),
                                "symbol": sym,
                                "borrow_qty": int(lot.get("borrow_qty") or rem),
                                "remaining_qty": rem,
                                "fee_rate": float(lot.get("borrow_fee_rate") or lot.get("fee_rate") or 3.50),
                                "borrow_date": str(lot.get("borrow_date") or ""),
                                "status": str(lot.get("status") or "ACTIVE"),
                            }
                        )
            except Exception as exc:
                sbl_source = "unavailable"
                logger.warning("MiniApp SBL lots read skipped: %s", exc)

        rows = _normalise_holdings(snap.get("holdings", []), keep_zero=borrow_summary.keys())
        if borrow_summary:
            rows = _merge_borrow(rows, borrow_summary)
        for r in rows:
            r["broker"] = r.get("broker") or b
            if acct_no_str and not r.get("account_no"):
                r["account_no"] = acct_no_str
            if acct_type_str and not r.get("account_type"):
                r["account_type"] = acct_type_str

        main_rows, borrowed = _split_borrowed(rows)
        if b == "kss":
            covered_shorts = {
                str(r.get("symbol") or "").strip().upper(): r
                for r in main_rows
                if str(r.get("position_type") or "").upper() == "SHORT"
                and int(r.get("volume") or 0) == 0
            }
            borrowed_by_sym = {
                str(br.get("symbol") or "").strip().upper(): br
                for br in borrowed
            }
            # Promote zero-vol covered SHORT that still owes SBL return when Streaming omitted (B)
            for sym, sr in covered_shorts.items():
                ret_qty = int(sr.get("returnable_qty") or 0)
                if sym not in borrowed_by_sym and ret_qty > 0:
                    promoted = {
                        "symbol": sym,
                        "position_type": "BORROWED",
                        "volume": int(sr.get("borrow_qty") or ret_qty),
                        "available_volume": int(sr.get("borrow_qty") or ret_qty),
                        "borrow_qty": int(sr.get("borrow_qty") or ret_qty),
                        "returnable_qty": ret_qty,
                        "lots": int(sr.get("borrow_lots") or 0),
                        "fee_rate": sr.get("borrow_fee_rate"),
                        "broker": str(sr.get("broker") or "kss").lower(),
                        "last": sr.get("last"),
                        "cost": None,
                        "pnl_pct": None,
                        "covered_from_short": True,
                    }
                    borrowed.append(promoted)
                    borrowed_by_sym[sym] = promoted
            borrowed.sort(key=lambda br: br["symbol"])
            for br in borrowed:
                sym = str(br.get("symbol") or "").strip().upper()
                br["covered_from_short"] = bool(br.get("covered_from_short")) or (sym in covered_shorts)
            # Drop redundant zero-vol covered SHORT rows when BORROWED card handles the return
            main_rows = [
                r
                for r in main_rows
                if not (
                    str(r.get("position_type") or "").upper() == "SHORT"
                    and int(r.get("volume") or 0) == 0
                    and str(r.get("symbol") or "").strip().upper() in borrowed_by_sym
                )
            ]
        mkt_val, pnl = _portfolio_totals(main_rows)
        amount = _portfolio_amount(main_rows)
        eq = _compute_account_net_equity(
            snap.get("cash_balance"),
            main_rows,
            account_equity=snap.get("total_equity"),
        )

        if b == "kss":
            acct_key, acct_label = _kss_account_key_and_label(acct_no_str, acct_type_str, margin_acct)
            kss_primary_key = acct_key
        else:
            acct_key, acct_label = "lib_cash", "Liberator · Cash"
        is_selected = acct_key in selected_set

        book_dict = {
            "broker": b,
            "label": "Krungsri (KSS)" if b == "kss" else "Liberator",
            "available": True,
            "selected_in_routing": is_selected,
            "routing_roles": list(roles_by_key.get(acct_key) or []),
            "error": snap.get("account_switch_error"),
            "account_no": acct_no_str,
            "account_type": acct_type_str,
            "cash_balance": _to_float_or_none(snap.get("cash_balance")),
            "line_available": _to_float_or_none(snap.get("line_available")),
            "excess_equity": _to_float_or_none(snap.get("excess_equity")),
            "margin_required": _to_float_or_none(snap.get("margin_required")),
            "total_equity": _to_float_or_none(snap.get("total_equity")),
            "long_market_value": eq["long_market_value"],
            "short_liability": eq["short_liability"],
            "net_equity": eq["net_equity"],
            "amount": amount,
            "market_value": mkt_val,
            "unrealized_pnl": pnl,
            "rows": main_rows,
            "borrowed": borrowed,
        }
        brokers_out[b] = book_dict

        accounts_out.append(
            {
                "key": acct_key,
                "broker": b,
                "label": acct_label,
                "available": True,
                "selected_in_routing": is_selected,
                "routing_roles": list(roles_by_key.get(acct_key) or []),
                "error": book_dict["error"],
                "account_no": acct_no_str,
                "account_type": acct_type_str,
                "cash_balance": book_dict["cash_balance"],
                "line_available": book_dict["line_available"],
                "excess_equity": book_dict["excess_equity"],
                "margin_required": book_dict["margin_required"],
                "total_equity": book_dict["total_equity"],
                "long_market_value": eq["long_market_value"],
                "short_liability": eq["short_liability"],
                "net_equity": eq["net_equity"],
                "unrealized_pnl": pnl,
                "rows": main_rows,
            }
        )

    # Read any other selected KSS account (e.g. if both Margin and Cash are selected in routing,
    # or if Streaming was left on an unselected KSS account) without switching unnecessarily.
    needed_other_kss: List[tuple[str, str]] = []
    if norm == "all" and kss_account is None and kss_primary_live_acct:
        if "kss_margin" in selected_set and kss_primary_key != "kss_margin":
            needed_other_kss.append(("kss_margin", margin_acct))
        if "kss_cash" in selected_set and kss_primary_key != "kss_cash":
            needed_other_kss.append(("kss_cash", cash_acct))

    if needed_other_kss and kss_primary_live_acct:
        try:
            for sec_key, other_acct in needed_other_kss:
                _, sec_label = _kss_account_key_and_label(other_acct, None, margin_acct)
                try:
                    sec_snap = await scrape_portfolio_from_cdp("kss", target_account=other_acct)
                    if isinstance(sec_snap, dict):
                        accounts_out.append(
                            _build_kss_secondary_book(
                                sec_snap,
                                expected_acct=other_acct,
                                margin_acct=margin_acct,
                                selected_in_routing=True,
                                routing_roles=roles_by_key.get(sec_key),
                            )
                        )
                except Exception as exc:
                    accounts_out.append(
                        {
                            "key": sec_key,
                            "broker": "kss",
                            "label": sec_label,
                            "available": False,
                            "selected_in_routing": True,
                            "routing_roles": list(roles_by_key.get(sec_key) or []),
                            "error": str(exc),
                            "account_no": other_acct,
                            "account_type": "CREDIT_BALANCE" if other_acct == margin_acct else "CASH_BALANCE",
                            "cash_balance": None,
                            "line_available": None,
                            "excess_equity": None,
                            "margin_required": None,
                            "total_equity": None,
                            "long_market_value": None,
                            "short_liability": None,
                            "net_equity": None,
                            "unrealized_pnl": None,
                            "rows": [],
                        }
                    )
        finally:
            try:
                await switch_kss_account(kss_primary_live_acct, target="streaming")
            except Exception as restore_exc:
                logger.debug("Restore active KSS account %s skipped: %s", kss_primary_live_acct, restore_exc)

    # Sort accounts deterministically: kss_margin -> kss_cash -> lib_cash
    _acct_order = {"kss_margin": 0, "kss_cash": 1, "lib_cash": 2}
    accounts_out.sort(key=lambda a: _acct_order.get(str(a.get("key") or ""), 9))

    available_books = [v for v in brokers_out.values() if v.get("available")]
    selected_accounts = [a for a in accounts_out if a.get("selected_in_routing")]
    if norm == "all":
        scope_accounts = selected_accounts
    else:
        scope_accounts = accounts_out

    available_scope = [a for a in scope_accounts if a.get("available")]
    eq_source = available_scope if available_scope else available_books
    has_line = any(a.get("line_available") is not None for a in eq_source)
    has_cash = any(a.get("cash_balance") is not None for a in eq_source)
    has_net_eq = any(a.get("net_equity") is not None for a in eq_source)

    total_line = sum(a["line_available"] for a in eq_source if a.get("line_available") is not None)
    total_cash = sum(a["cash_balance"] for a in eq_source if a.get("cash_balance") is not None)
    total_net_equity = round(
        sum(a["net_equity"] for a in eq_source if a.get("net_equity") is not None), 2
    ) if has_net_eq else None
    total_long_mv = round(
        sum(a["long_market_value"] for a in eq_source if a.get("long_market_value") is not None), 2
    ) if eq_source else None
    total_short_liab = round(
        sum(a["short_liability"] for a in eq_source if a.get("short_liability") is not None), 2
    ) if eq_source else None
    total_pnl = sum(a["unrealized_pnl"] for a in eq_source if a.get("unrealized_pnl") is not None)

    partial_flag = any(not a.get("available") for a in scope_accounts) if scope_accounts else any(
        not v.get("available") for v in brokers_out.values()
    )
    net_eq_complete = bool(scope_accounts) and not partial_flag and all(
        a.get("net_equity") is not None for a in scope_accounts
    )
    selected_labels = [
        f"{a['label']} ({a['account_no']})" if a.get("account_no") else str(a.get("label") or a.get("key"))
        for a in selected_accounts
    ]

    if norm == "all" and total_net_equity is not None:
        try:
            from app.services.suggest_entry_service import suggest_entry_service

            suggest_entry_service.record_portfolio_equity_snapshot(
                total_net_equity=total_net_equity,
                partial=partial_flag,
                accounts=selected_accounts,
            )
        except Exception as exc:
            logger.debug("record_portfolio_equity_snapshot skipped: %s", exc)

    return {
        "brokers": brokers_out,
        "accounts": accounts_out,
        "sbl": {"source": sbl_source, "lots": sbl_lots_out},
        "kss_active_account": get_active_kss_account(),
        "kss_active_type": get_active_kss_account_type(),
        "kss_accounts": [
            {"account_no": margin_acct, "label": "Margin"},
            {"account_no": cash_acct, "label": "Cash"},
        ],
        "summary": {
            "available_count": len(available_books),
            "accounts_count": len(available_scope),
            "selected_accounts": [
                {
                    "key": a.get("key"),
                    "label": a.get("label"),
                    "account_no": a.get("account_no"),
                    "roles": a.get("routing_roles") or [],
                }
                for a in selected_accounts
            ],
            "selected_account_labels": selected_labels,
            "partial": partial_flag,
            "net_equity_complete": net_eq_complete,
            "total_net_equity": total_net_equity,
            "total_long_market_value": total_long_mv,
            "total_short_liability": total_short_liab,
            "total_line_available": total_line if has_line else None,
            "total_cash_balance": total_cash if has_cash else None,
            "total_unrealized_pnl": total_pnl if eq_source else None,
        },
        "dry_run": _is_dry_mode(),
    }


async def fetch_sbl_pool_quote(symbol: str) -> Dict[str, Any]:
    """Read SBL borrowable pool inventory, fee rate, and XD/XR dividend risk for a symbol."""
    clean_sym = (symbol or "").strip().upper()
    if not clean_sym:
        raise FacadeHttpError(400, "E_BAD_SYMBOL", "กรุณาระบุชื่อหุ้น")

    avail_qty = await krungsri_sbl_driver.scrape_sbl_available_inventory(clean_sym)
    cache_st = krungsri_sbl_driver.cache_status() if hasattr(krungsri_sbl_driver, "cache_status") else {}
    risk_info: Dict[str, Any] = {}
    if hasattr(krungsri_sbl_driver, "check_symbol_risk"):
        try:
            risk_info = krungsri_sbl_driver.check_symbol_risk(clean_sym) or {}
        except Exception as exc:
            logger.debug("check_symbol_risk skipped for %s: %s", clean_sym, exc)
    xd_res = await sbl_intelligence_service.check_xd_risk(clean_sym)
    fee_rate = _to_float_or_none(risk_info.get("borrow_fee_pct"))
    return {
        "symbol": clean_sym,
        "available_qty": int(avail_qty or 0),
        "fee_rate": fee_rate if fee_rate is not None else 3.50,
        "cache": cache_st,
        "risk": risk_info,
        "xd_risk": xd_res,
    }


async def fetch_orders_overview(broker: str = "all") -> Dict[str, Any]:
    """Read live order ledgers across KSS and LIB without mocking missing brokers."""
    norm = (broker or "all").strip().lower()
    if norm not in ("all", "kss", "lib"):
        raise FacadeHttpError(400, "E_BAD_BROKER", f"Unsupported broker '{broker}'.")

    targets = ["kss", "lib"] if norm == "all" else [norm]
    brokers_out: Dict[str, Dict[str, Any]] = {}
    all_orders: List[Dict[str, Any]] = []

    async def _scrape_one(b: str) -> Any:
        try:
            snap = await scrape_orders_from_cdp(b)
            if not isinstance(snap, dict) or "orders" not in snap:
                raise RuntimeError("unreadable order ledger")
            return snap
        except Exception as exc:
            return exc

    snaps = await asyncio.gather(*(_scrape_one(b) for b in targets))

    for b, snap in zip(targets, snaps):
        if isinstance(snap, Exception):
            brokers_out[b] = {"broker": b, "available": False, "error": str(snap)}
            continue

        brokers_out[b] = {"broker": b, "available": True, "error": None}
        for o in snap.get("orders") or []:
            side = map_ledger_side(o.get("side"))
            if side is None:
                continue
            vol = int(o.get("volume", 0) or 0)
            mv = int(o.get("matched_volume", 0) or 0)
            bal = int(o.get("balance", 0) or 0)
            status_raw = str(o.get("status", ""))
            category = _classify_order_category(vol, mv, bal, status_raw)
            kind = _CAT_KIND.get(category, "unknown")
            cancellable = kind in ("open", "partial") and bal > 0
            all_orders.append(
                {
                    "broker": b,
                    "order_no": str(o.get("broker_order_no") or o.get("order_no") or ""),
                    "symbol": str(o.get("symbol") or "").upper(),
                    "side": side,
                    "volume": vol,
                    "matched_volume": mv,
                    "balance": bal,
                    "price": float(o.get("price") or 0.0),
                    "status": status_raw,
                    "kind": kind,
                    "cancellable": cancellable,
                    "nego_match_allowed": cancellable and side != "SHORT",
                    "time": str(o.get("time") or o.get("order_time") or ""),
                }
            )

    open_cnt = sum(1 for o in all_orders if o["kind"] in ("open", "partial"))
    matched_cnt = sum(1 for o in all_orders if o["kind"] == "matched")
    return {
        "brokers": brokers_out,
        "orders": all_orders,
        "counts": {"total": len(all_orders), "open": open_cnt, "matched": matched_cnt},
        "partial": any(not v.get("available") for v in brokers_out.values()),
        "dry_run": _is_dry_mode(),
    }


def _quote_to_nego_envelope(
    b_norm: str,
    clean_sym: str,
    quote: Dict[str, Any],
    *,
    side: Optional[str] = None,
    old_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Normalize a raw quote dict into the compute_nego_quote / Suggest Entry contract."""
    available = not (quote.get("success") is False or quote.get("source") == "unavailable")
    bid_1 = _to_float_or_none(quote.get("bid_1"))
    offer_1 = _to_float_or_none(quote.get("offer_1"))
    last = _to_float_or_none(quote.get("last"))
    prior_close = _to_float_or_none(quote.get("prior_close"))

    ref_px = _to_float_or_none(old_price) or last or offer_1 or bid_1
    tick_down = validator.get_next_tick(ref_px, steps=-1) if ref_px and ref_px > 0 else None
    tick_up = validator.get_next_tick(ref_px, steps=1) if ref_px and ref_px > 0 else None

    open_plan: Optional[Dict[str, Any]] = None
    lure_plan: Optional[Dict[str, Any]] = None
    match_plan: Optional[Dict[str, Any]] = None
    clean_side = (side or "").strip().upper()
    uptick_ticks, strict_reason = trading_rules.get_short_uptick_detail(clean_sym, context="nego")
    open_uptick_ticks, _open_reason = trading_rules.get_short_uptick_detail(clean_sym, context="open")
    if clean_side in ("BUY", "SELL", "SHORT", "COVER") and available:
        lure_plan = plan_price(
            clean_side, "lure", bid_1, offer_1, last, old_price=old_price, prior_close=prior_close, symbol=clean_sym
        )
        if clean_side == "SHORT":
            open_mode = "uptick_best" if open_uptick_ticks > 0 else "lure"
            open_plan = plan_price(
                "SHORT",
                open_mode,
                bid_1,
                offer_1,
                last,
                old_price=old_price,
                prior_close=prior_close,
                symbol=clean_sym,
                min_ticks=open_uptick_ticks,
            )
        if clean_side == "SHORT" and uptick_ticks > 0:
            reason_tag = f" · {strict_reason}" if strict_reason else ""
            match_plan = {"ok": False, "reason": f"Uptick Rule (+1{reason_tag}): SHORT ต่อราคาได้เฉพาะแบบล่อ (Lure)"}
        else:
            match_plan = plan_price(
                clean_side, "match", bid_1, offer_1, last, old_price=old_price, prior_close=prior_close, symbol=clean_sym
            )

    match_allowed = bool(match_plan and match_plan.get("ok")) if clean_side == "SHORT" else True

    return {
        "symbol": clean_sym,
        "broker": b_norm,
        "available": available,
        "error": quote.get("error") if not available else None,
        "source": quote.get("source"),
        "bid_1": bid_1,
        "offer_1": offer_1,
        "last": last,
        "prior_close": prior_close,
        "tick_down": tick_down,
        "tick_up": tick_up,
        "short_open_uptick_ticks": open_uptick_ticks,
        "short_uptick_ticks": uptick_ticks,
        "strict_uptick_reason": strict_reason,
        "match_allowed": match_allowed,
        "open_plan": open_plan,
        "lure_plan": lure_plan,
        "match_plan": match_plan,
    }


def _build_httpx_cookies(raw_cookies: List[Dict[str, Any]]) -> httpx.Cookies:
    jar = httpx.Cookies()
    for c in raw_cookies or []:
        if not isinstance(c, dict):
            continue
        name = str(c.get("name") or "")
        value = str(c.get("value") or "")
        if not name:
            continue
        domain = str(c.get("domain") or "")
        path = str(c.get("path") or "/")
        try:
            jar.set(name, value, domain=domain, path=path)
        except Exception:
            jar.set(name, value)
    return jar


def _unavailable_raw_quote(symbol: str, error: str) -> Dict[str, Any]:
    return {
        "symbol": symbol,
        "success": False,
        "status": "QUOTE_UNAVAILABLE",
        "source": "unavailable",
        "error": error,
        "bid_1": None,
        "offer_1": None,
        "last": None,
        "prior_close": None,
    }


async def _fetch_batch_quotes_via_httpx(
    b_norm: str,
    symbols: List[str],
) -> Optional[Dict[str, Dict[str, Any]]]:
    """Fast direct HTTP quote batch using cached CDP session cookies (~80-150ms)."""
    cached = _BROKER_COOKIE_CACHE.get(b_norm)
    if not cached:
        return None
    if (time.monotonic() - float(cached.get("ts") or 0.0)) > _COOKIE_CACHE_TTL_S:
        _BROKER_COOKIE_CACHE.pop(b_norm, None)
        return None

    cookies_list = cached.get("cookies") or []
    if not cookies_list:
        return None
    jar = _build_httpx_cookies(cookies_list)

    try:
        async with httpx.AsyncClient(cookies=jar, timeout=4.0, follow_redirects=False) as client:
            if b_norm == "lib":
                missing = [s for s in symbols if s not in _LIB_ORDERBOOK_MAP]
                if missing:
                    cfg_r = await client.get("https://webtrade.liberator.co.th/vb/config")
                    if cfg_r.status_code in (401, 403):
                        _BROKER_COOKIE_CACHE.pop(b_norm, None)
                        return None
                    if cfg_r.status_code != 200:
                        return None
                    cfg = (cfg_r.json().get("result") or {}).get("data", {}).get("config") or {}
                    ordered = ["set", "set50if", "tfex", *[k for k in cfg.keys() if k not in ("set", "set50if", "tfex")]]
                    for grp in ordered:
                        node = cfg.get(grp) or {}
                        for key in ("securities", "indexes"):
                            for row in node.get(key) or []:
                                if isinstance(row, list) and row and row[0]:
                                    sym = str(row[0]).strip().upper()
                                    if sym not in _LIB_ORDERBOOK_MAP:
                                        try:
                                            _LIB_ORDERBOOK_MAP[sym] = int(row[1])
                                        except (TypeError, ValueError):
                                            pass

                ids = [_LIB_ORDERBOOK_MAP[s] for s in symbols if s in _LIB_ORDERBOOK_MAP]
                if not ids:
                    return None
                r = await client.post(
                    "https://webtrade.liberator.co.th/vb/stock/info",
                    json={"orderBookIds": ids},
                )
                if r.status_code in (401, 403):
                    _BROKER_COOKIE_CACHE.pop(b_norm, None)
                    return None
                if r.status_code not in (200, 201):
                    return None
                rows = ((r.json().get("result") or {}).get("list")) or []
                by_sym: Dict[str, Dict[str, Any]] = {}
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    sym = str(row.get("n") or "").strip().upper()
                    if not sym:
                        continue
                    bid_1 = _to_float_or_none(row.get("bp"))
                    offer_1 = _to_float_or_none(row.get("op"))
                    last = _to_float_or_none(row.get("l"))
                    if bid_1 is None and offer_1 is None and last is None:
                        by_sym[sym] = _unavailable_raw_quote(sym, "No Bid/Offer/Last")
                    else:
                        by_sym[sym] = {
                            "symbol": sym,
                            "success": True,
                            "status": "OK",
                            "source": "liberator_httpx",
                            "bid_1": bid_1,
                            "offer_1": offer_1,
                            "last": last,
                            "prior_close": _to_float_or_none(row.get("prior")),
                        }
                return {
                    s: by_sym.get(s) or _unavailable_raw_quote(s, f"{s} not in Liberator /vb/stock/info")
                    for s in symbols
                }

            # KSS static-data concurrent HTTP
            async def _fetch_kss_one(sym: str) -> tuple[str, int, Optional[Dict[str, Any]]]:
                url = f"https://apigwcld.settrade.com/api/marketdata/v4/029/static-data/{sym}?market=E"
                resp = await client.get(url)
                if resp.status_code != 200:
                    return sym, resp.status_code, None
                return sym, 200, resp.json()

            res_list = await asyncio.gather(*[_fetch_kss_one(s) for s in symbols], return_exceptions=True)
            out: Dict[str, Dict[str, Any]] = {}
            for sym, item in zip(symbols, res_list):
                if isinstance(item, Exception):
                    return None
                _s, code, payload = item
                if code in (401, 403):
                    _BROKER_COOKIE_CACHE.pop(b_norm, None)
                    return None
                if code != 200 or not isinstance(payload, dict):
                    out[sym] = _unavailable_raw_quote(sym, f"HTTP {code}")
                    continue
                bid_1 = _to_float_or_none(payload.get("firstBid"))
                offer_1 = _to_float_or_none(payload.get("firstAsk"))
                last = _to_float_or_none(payload.get("last"))
                if bid_1 is None and offer_1 is None and last is None:
                    out[sym] = _unavailable_raw_quote(sym, "No Bid/Offer/Last")
                else:
                    out[sym] = {
                        "symbol": sym,
                        "success": True,
                        "status": "OK",
                        "source": "streaming_httpx",
                        "bid_1": bid_1,
                        "offer_1": offer_1,
                        "last": last,
                        "prior_close": _to_float_or_none(payload.get("close")),
                    }
            return out
    except Exception as exc:
        logger.debug("Fast httpx batch quote skipped for %s (%s); falling back to CDP", b_norm, exc)
        return None


async def fetch_batch_quotes(
    broker: str,
    symbols: List[str],
    *,
    allow_cookie_httpx: bool = True,
) -> Dict[str, Dict[str, Any]]:
    """Fetch raw quotes for many symbols using cookie-cached httpx or 1 batch CDP call."""
    b_norm = "lib" if broker in ("lib", "liberator") else "kss"
    clean_syms = list(dict.fromkeys(str(s or "").strip().upper() for s in symbols if str(s or "").strip()))
    if not clean_syms:
        return {}

    if allow_cookie_httpx:
        fast = await _fetch_batch_quotes_via_httpx(b_norm, clean_syms)
        if fast is not None:
            return fast

    lock = _BATCH_QUOTE_LOCKS.setdefault(b_norm, asyncio.Lock())
    async with lock:
        if allow_cookie_httpx:
            fast = await _fetch_batch_quotes_via_httpx(b_norm, clean_syms)
            if fast is not None:
                return fast

        from scripts.broker_cdp.js_runner import extract_broker_cookies_and_batch_quotes

        batch = await extract_broker_cookies_and_batch_quotes(b_norm, clean_syms, timeout_s=8.0)
        cookies = batch.get("cookies") or []
        if cookies:
            _BROKER_COOKIE_CACHE[b_norm] = {"cookies": cookies, "ts": time.monotonic()}
        for sym, ob_id in (batch.get("ob_map") or {}).items():
            try:
                _LIB_ORDERBOOK_MAP[str(sym).upper()] = int(ob_id)
            except (TypeError, ValueError):
                pass
        return batch.get("quotes") or {
            s: _unavailable_raw_quote(s, str(batch.get("error") or "Batch quote failed"))
            for s in clean_syms
        }


async def fetch_batch_nego_quotes(
    broker: str,
    symbols: List[str],
    *,
    side: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """Batch version of compute_nego_quote for fast Suggest Entry / Multitrade polling."""
    b_norm = "lib" if broker in ("lib", "liberator") else "kss"
    clean_syms = list(dict.fromkeys(str(s or "").strip().upper() for s in symbols if str(s or "").strip()))
    raw_quotes = await fetch_batch_quotes(b_norm, clean_syms)
    return {
        sym: _quote_to_nego_envelope(
            b_norm,
            sym,
            raw_quotes.get(sym) or _unavailable_raw_quote(sym, "Missing quote"),
            side=side,
        )
        for sym in clean_syms
    }


async def compute_nego_quote(
    broker: str,
    symbol: str,
    side: Optional[str] = None,
    old_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Read live quote for symbol and compute Match / Lure / Tick plans."""
    b_norm = "lib" if broker in ("lib", "liberator") else "kss"
    clean_sym = (symbol or "").strip().upper()
    if not clean_sym:
        raise FacadeHttpError(400, "E_BAD_SYMBOL", "กรุณาระบุชื่อหุ้น")

    quote = await _broker_quote(b_norm, clean_sym)
    return _quote_to_nego_envelope(b_norm, clean_sym, quote, side=side, old_price=old_price)


async def execute_nego_amend(
    *,
    broker: str,
    order_no: str,
    symbol: str,
    side: str,
    volume: int,
    old_price: Optional[float],
    mode: str,
    custom_price: Optional[float] = None,
    notify_line: bool = True,
) -> Dict[str, Any]:
    """Validate tick/uptick/kill-switch guardrails and amend an open order."""
    _ensure_not_killed()
    b_norm = "lib" if broker in ("lib", "liberator") else "kss"
    clean_sym = (symbol or "").strip().upper()
    clean_side = (side or "").strip().upper()
    clean_mode = (mode or "").strip().lower()

    if clean_side not in ("BUY", "SELL", "SHORT", "COVER"):
        raise FacadeHttpError(400, "E_BAD_SIDE", f"ไม่รู้จักฝั่งคำสั่ง {side!r}")
    if clean_side == "SHORT" and clean_mode == "match":
        raise FacadeHttpError(
            422,
            "NEGO_SHORT_MATCH_NOT_ALLOWED",
            f"ออเดอร์ SHORT {clean_sym} #{order_no} ต่อราคาแบบ Match ไม่ได้ (กฎ Uptick — รองรับเฉพาะ Lure หรือแก้ราคา > Last)",
        )

    if clean_mode == "custom":
        if custom_price is None or float(custom_price) <= 0:
            raise FacadeHttpError(422, "E_BAD_PRICE", "กรุณาระบุราคาใหม่ที่ถูกต้อง")
        new_price = float(custom_price)
        ok_tick, err_tick = validator.validate_tick_size(new_price)
        if not ok_tick:
            raise FacadeHttpError(422, "E_BAD_TICK", err_tick or "ราคาไม่ตรงช่วง Tick SET")
        if clean_side == "SHORT":
            quote = await _broker_quote(b_norm, clean_sym)
            last = _to_float_or_none(quote.get("last"))
            if quote.get("success") is False or quote.get("source") == "unavailable" or last is None:
                raise FacadeHttpError(
                    422,
                    "E_NO_QUOTE",
                    f"อ่านราคาล่าสุด (last) ของ {clean_sym} ไม่ได้ — ปฏิเสธ SHORT amend (G6 Uptick)",
                )
            ok_up, err_up = validator.validate_short_uptick(new_price, last, symbol=clean_sym)
            if not ok_up:
                raise FacadeHttpError(422, "E_UPTICK_VIOLATION", err_up or "ราคา SHORT ไม่ผ่านเกณฑ์ Short Sell")
    elif clean_mode in ("match", "lure", "uptick_best"):
        quote = await _broker_quote(b_norm, clean_sym)
        if quote.get("success") is False or quote.get("source") == "unavailable":
            raise FacadeHttpError(
                422,
                "QUOTE_UNAVAILABLE",
                str(quote.get("error") or f"อ่านราคาสด {clean_sym} จาก {b_norm.upper()} ไม่ได้"),
            )
        plan = plan_price(
            clean_side,
            clean_mode,
            quote.get("bid_1"),
            quote.get("offer_1"),
            quote.get("last"),
            old_price=old_price,
            prior_close=quote.get("prior_close"),
            symbol=clean_sym,
        )
        if not plan.get("ok"):
            reason = str(plan.get("reason") or "คำนวณราคาไม่สำเร็จ")
            if reason == "NO_CHANGE":
                is_dry = _is_dry_mode()
                pushed = (
                    await best_effort_push_card(
                        cards.negotiate_card(
                            order_no=str(order_no),
                            symbol=clean_sym,
                            side=clean_side,
                            outcome="NO_CHANGE",
                            current_price=old_price,
                            target_price=old_price,
                            volume=int(volume or 0) if volume is not None else None,
                            reason="ออเดอร์อยู่ที่ราคาเป้าหมายแล้ว",
                            submitted=None,
                            ts=_bkk_ts(),
                            is_test=not bool(trade_dispatcher.is_live),
                            dry_run=is_dry,
                        )
                    )
                    if notify_line
                    else False
                )
                return {
                    "ok": True,
                    "no_change": True,
                    "broker": b_norm,
                    "order_no": order_no,
                    "symbol": clean_sym,
                    "side": clean_side,
                    "new_price": old_price,
                    "dry_run": is_dry,
                    "line_pushed": pushed,
                    "message": f"ออเดอร์ #{order_no} {clean_sym} อยู่ที่ราคาเป้าหมายแล้ว",
                }
            raise FacadeHttpError(422, "NEGO_PLAN_FAILED", reason)
        new_price = float(plan["price"])
    else:
        raise FacadeHttpError(400, "E_BAD_MODE", f"Unsupported nego mode '{mode}'.")

    ok_tick, err_tick = validator.validate_tick_size(new_price)
    if not ok_tick:
        raise FacadeHttpError(422, "E_BAD_TICK", err_tick or "ราคาไม่ผ่าน SET tick size")

    res = await _broker_modify(
        b_norm,
        order_no=str(order_no),
        new_price=new_price,
        symbol=clean_sym,
        side=clean_side,
        volume=int(volume or 0),
    )
    status_str = str(res.get("status") or "")
    ok = bool(res.get("success")) or status_str in ("OPEN", "MATCHED", "MODIFIED", "SUBMITTED", "DRY_RUN")
    retried_uptick = False
    if (
        not ok
        and clean_side == "SHORT"
        and clean_mode in ("lure", "uptick_best")
        and trading_rules.is_short_uptick_rejection(
            res.get("error_code") or status_str,
            str(res.get("error") or res.get("message") or ""),
        )
    ):
        trading_rules.mark_runtime_strict_symbol(clean_sym)
        try:
            fresh_q = await _broker_quote(b_norm, clean_sym)
            fresh_last = _to_float_or_none(fresh_q.get("last"))
            if fresh_last and fresh_last > 0:
                retry_px = round(validator.get_short_min_price(fresh_last, symbol=clean_sym, min_ticks=1), 2)
                if retry_px > new_price and (old_price is None or abs(retry_px - float(old_price)) >= 1e-9):
                    retry_res = await _broker_modify(
                        b_norm,
                        order_no=str(order_no),
                        new_price=retry_px,
                        symbol=clean_sym,
                        side=clean_side,
                        volume=int(volume or 0),
                    )
                    retry_st = str(retry_res.get("status") or "")
                    if bool(retry_res.get("success")) or retry_st in ("OPEN", "MATCHED", "MODIFIED", "SUBMITTED", "DRY_RUN"):
                        res = retry_res
                        status_str = retry_st
                        new_price = retry_px
                        ok = True
                        retried_uptick = True
                    else:
                        res = retry_res
                        status_str = retry_st
        except Exception as retry_exc:
            logger.warning("SHORT amend uptick retry failed for %s #%s: %s", clean_sym, order_no, retry_exc)

    is_dry = _is_dry_mode()
    if not ok:
        err_msg = str(res.get("error") or res.get("message") or "แก้ราคาไม่สำเร็จ")
        if notify_line:
            await best_effort_push_card(
                cards.negotiate_card(
                    order_no=str(order_no),
                    symbol=clean_sym,
                    side=clean_side,
                    outcome="FAILED",
                    current_price=old_price,
                    target_price=new_price,
                    volume=int(volume or 0) if volume is not None else None,
                    reason=err_msg,
                    submitted=None,
                    ts=_bkk_ts(),
                    is_test=not bool(trade_dispatcher.is_live),
                    dry_run=is_dry,
                )
            )
        raise FacadeHttpError(
            502,
            str(res.get("error_code") or status_str or "NEGO_FAILED"),
            err_msg,
        )

    tag = "🧪 [DRY-RUN]" if is_dry else "✅"
    retry_note = " [Auto-Uptick +1]" if retried_uptick else ""
    old_txt = f"฿{float(old_price):.2f} → " if old_price is not None else ""
    msg = f"{tag} ต่อราคา {clean_side} {clean_sym} #{order_no} {old_txt}฿{new_price:.2f}{retry_note}"
    pushed = (
        await best_effort_push_card(
            cards.negotiate_card(
                order_no=str(order_no),
                symbol=clean_sym,
                side=clean_side,
                outcome="OK",
                current_price=old_price,
                target_price=new_price,
                volume=int(volume or 0) if volume is not None else None,
                reason=msg,
                submitted=not is_dry,
                ts=_bkk_ts(),
                is_test=not bool(trade_dispatcher.is_live),
                dry_run=is_dry,
            )
        )
        if notify_line
        else False
    )
    return {
        "ok": True,
        "no_change": False,
        "broker": b_norm,
        "order_no": str(order_no),
        "symbol": clean_sym,
        "side": clean_side,
        "old_price": old_price,
        "new_price": new_price,
        "dry_run": is_dry,
        "line_pushed": pushed,
        "message": msg,
        "result": res,
    }


async def execute_order_cancel(
    *,
    broker: str,
    order_no: str,
    symbol: str,
    notify_line: bool = True,
) -> Dict[str, Any]:
    """Fail-closed cancel for an open resting order."""
    _ensure_not_killed()
    b_norm = "lib" if broker in ("lib", "liberator") else "kss"
    clean_sym = (symbol or "").strip().upper()
    clean_no = str(order_no or "").strip()
    if not clean_no:
        raise FacadeHttpError(400, "E_MISSING_ORDER_NO", "กรุณาระบุเลขที่ออเดอร์")

    # Anti-Mock: verify order is actually still open/resting on the ledger before cancelling
    snap = await scrape_orders_from_cdp(b_norm)
    if not isinstance(snap, dict) or "orders" not in snap:
        raise FacadeHttpError(502, "ORDER_LEDGER_UNAVAILABLE", "อ่านตารางออเดอร์สดจาก CDP ไม่ได้ (fail-closed)")

    matched_row: Optional[Dict[str, Any]] = None
    for row in snap.get("orders") or []:
        row_no = str(row.get("broker_order_no") or row.get("order_no") or "").strip()
        if row_no == clean_no:
            matched_row = row
            break

    if matched_row is None:
        raise FacadeHttpError(
            409,
            "ORDER_NOT_OPEN",
            f"ไม่พบออเดอร์ค้าง #{clean_no} ในตาราง {b_norm.upper()} (อาจแมตช์หรือปิดไปแล้ว)",
        )

    bal = int(matched_row.get("balance", 0) or 0)
    vol = int(matched_row.get("volume", 0) or 0)
    mv = int(matched_row.get("matched_volume", 0) or 0)
    cat = _classify_order_category(vol, mv, bal, str(matched_row.get("status") or ""))
    if _CAT_KIND.get(cat, "unknown") not in ("open", "partial") or bal <= 0:
        raise FacadeHttpError(
            409,
            "ORDER_ALREADY_CLOSED",
            f"ออเดอร์ #{clean_no} สถานะ {matched_row.get('status') or cat} ไม่สามารถยกเลิกได้",
        )

    eff_sym = clean_sym or str(matched_row.get("symbol") or "").upper()
    res = await _broker_cancel(
        b_norm,
        order_no=clean_no,
        symbol=eff_sym,
    )
    is_dry = _is_dry_mode()
    if not res.get("success"):
        err_msg = str(res.get("error") or res.get("message") or f"ยกเลิกออเดอร์ #{clean_no} ไม่สำเร็จ")
        if notify_line:
            await best_effort_push_card(
                cards.order_cancel_result_card(
                    broker=b_norm,
                    symbol=eff_sym,
                    order_no=clean_no,
                    side=str(matched_row.get("side") or "?"),
                    volume=bal,
                    success=False,
                    verified=False,
                    detail=err_msg,
                    ts=_bkk_ts(),
                )
            )
        raise FacadeHttpError(
            409,
            str(res.get("status") or "CANCEL_FAILED"),
            err_msg,
        )
    pushed = (
        await best_effort_push_card(
            cards.order_cancel_result_card(
                broker=b_norm,
                symbol=eff_sym,
                order_no=clean_no,
                side=str(matched_row.get("side") or "?"),
                volume=bal,
                success=True,
                verified=True,
                detail="DRY-RUN (จำลอง)" if is_dry else None,
                ts=_bkk_ts(),
            )
        )
        if notify_line
        else False
    )
    return {
        "ok": True,
        "broker": b_norm,
        "order_no": clean_no,
        "symbol": eff_sym,
        "dry_run": is_dry,
        "line_pushed": pushed,
        "result": res,
    }


def build_trade_order_from_input(
    *,
    side: str,
    symbol: str,
    broker: Optional[str] = None,
    account: Optional[str] = None,
    amount_mode: str = "volume",
    volume: int = 0,
    target_value: Optional[float] = None,
    price: Optional[float] = None,
) -> TradeOrder:
    """Resolve broker/account defaults cleanly matching LINE CLI routing."""
    clean_side = (side or "").strip().lower()
    if clean_side not in ("buy", "sell", "short", "cover"):
        raise FacadeHttpError(400, "E_BAD_SIDE", f"side '{side}' ไม่ถูกต้อง (ต้องเป็น buy/sell/short/cover)")

    clean_sym = (symbol or "").strip().upper()
    if not clean_sym:
        raise FacadeHttpError(400, "E_BAD_SYMBOL", "กรุณาระบุชื่อหุ้น")

    clean_mode = (amount_mode or "volume").strip().lower()
    if clean_mode not in ("volume", "value", "all"):
        raise FacadeHttpError(400, "E_BAD_AMOUNT_MODE", f"amount_mode '{amount_mode}' ไม่ถูกต้อง")

    def_broker, def_account = account_routing.default_route_for_side(clean_side)
    b_raw = (broker or def_broker).strip().lower()
    b_norm = "lib" if b_raw in ("lib", "liberator") else ("kss" if b_raw in ("kss", "krungsri", "trademan") else "")
    if not b_norm:
        raise FacadeHttpError(400, "E_BAD_BROKER", f"ไม่รองรับโบรกเกอร์ '{broker}'")

    default_acct = "margin" if clean_side in ("short", "cover") else (def_account if b_norm == def_broker else ("margin" if b_norm == "kss" else "cash"))
    if b_norm == "lib":
        default_acct = "cash"
    acct_norm = (account or default_acct).strip().lower()
    if acct_norm not in ("cash", "margin"):
        raise FacadeHttpError(400, "E_BAD_ACCOUNT", f"account '{account}' ไม่ถูกต้อง (cash|margin)")

    return TradeOrder(
        cmd=clean_side,
        symbol=clean_sym,
        volume=int(volume or 0),
        price=_to_float_or_none(price),
        broker=b_norm,
        account=acct_norm,
        amount_mode=clean_mode,
        target_value=_to_float_or_none(target_value),
    )


async def preview_trade_order(**kwargs: Any) -> Dict[str, Any]:
    """Run shared Phase-B pre-trade check and return computed shares, price & guardrails."""
    order = build_trade_order_from_input(**kwargs)
    if kill_switch_service.is_active():
        return {
            "ok": False,
            "code": "CLI_KILL_SWITCH",
            "message": "⛔ KILL-SWITCH ACTIVE — ระงับคำสั่งซื้อขายทั้งหมด · ต้อง RESET ก่อน",
            "broker": order.broker,
            "account": order.account,
            "dry_run": _is_dry_mode(),
        }

    check = await _pre_trade_check(order, db=None)
    if not check.get("ok"):
        return {
            "ok": False,
            "code": check.get("code") or "E_PRE_TRADE_FAILED",
            "message": check.get("message") or "ไม่ผ่านเกณฑ์ตรวจสอบก่อนส่งคำสั่ง",
            "quick_reply": check.get("quick_reply") or [],
            "broker": order.broker,
            "account": order.account,
            "dry_run": _is_dry_mode(),
        }

    vol = int(check["resolved_volume"])
    px = float(check["resolved_price"])
    return {
        "ok": True,
        "cmd": order.cmd,
        "side_letter": order.side_letter,
        "symbol": order.symbol,
        "broker": order.broker,
        "account": order.account,
        "account_no": check.get("account_no"),
        "amount_mode": order.amount_mode,
        "target_value": order.target_value,
        "resolved_volume": vol,
        "resolved_price": px,
        "total_thb": round(vol * px, 2),
        "dry_run": _is_dry_mode(),
        "mode_label": "DRY-RUN" if _is_dry_mode() else "LIVE",
    }


async def execute_trade_with_idempotency(
    *,
    idempotency_key: str,
    notify_line: bool = True,
    **order_kwargs: Any,
) -> Dict[str, Any]:
    """Single-flight trade execution with pre-trade recheck, G6 uptick guard, and idempotency."""
    _ensure_not_killed()
    key = (idempotency_key or "").strip()
    if not key:
        raise FacadeHttpError(400, "E_MISSING_IDEMPOTENCY_KEY", "กรุณาส่ง idempotency_key เพื่อกันคำสั่งซ้ำ")

    _prune_idempotency()
    lock = _TRADE_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        cached = _TRADE_IDEMPOTENCY.get(key)
        if cached and (time.monotonic() - float(cached.get("ts", 0.0))) <= _IDEMPOTENCY_TTL_S:
            replay = dict(cached["response"])
            replay["idempotent_replay"] = True
            return replay

        preview = await preview_trade_order(**order_kwargs)
        if not preview.get("ok"):
            raise FacadeHttpError(
                422,
                str(preview.get("code") or "E_PRE_TRADE_FAILED"),
                str(preview.get("message") or "คำสั่งไม่ผ่านเกณฑ์"),
                {"quick_reply": preview.get("quick_reply") or []},
            )

        draft_id = uuid.uuid4().hex[:8]
        draft: Dict[str, Any] = {
            "draft_id": draft_id,
            "cmd": preview["cmd"],
            "side_letter": preview["side_letter"],
            "symbol": preview["symbol"],
            "volume": preview["resolved_volume"],
            "price": preview["resolved_price"],
            "total_thb": preview["total_thb"],
            "broker": preview["broker"],
            "account": preview["account"],
            "account_no": preview["account_no"],
        }

        exec_kwargs: Dict[str, Any] = {"dry_run": _is_dry_mode()}
        if draft["cmd"] == "short":
            quote = await _broker_quote(draft["broker"], draft["symbol"])
            last = _to_float_or_none(quote.get("last") or quote.get("bid_1"))
            if not last:
                raise FacadeHttpError(
                    422,
                    "E_NO_QUOTE",
                    "อ่านราคาล่าสุด (last) ไม่ได้ — ไม่ส่ง SHORT เพื่อรักษากฎ uptick (G6)",
                )
            ok_up, err_up = validator.validate_short_uptick(
                float(draft["price"]), last, symbol=draft["symbol"], context="open"
            )
            if not ok_up:
                raise FacadeHttpError(422, "E_UPTICK_VIOLATION", err_up or "ราคา SHORT ไม่ผ่านเกณฑ์เปิด +1 Strict Uptick")
            exec_kwargs["last_trade_price"] = last

        exec_res = await execute_confirmed_order(draft, **exec_kwargs)
        is_dry = _is_dry_mode()
        retried_uptick = False
        if (
            not exec_res.get("success")
            and draft["cmd"] == "short"
            and trading_rules.is_short_uptick_rejection(
                exec_res.get("error_code") or exec_res.get("status"),
                str(exec_res.get("error") or exec_res.get("status") or ""),
            )
        ):
            trading_rules.mark_runtime_strict_symbol(draft["symbol"])
            try:
                fresh_q = await _broker_quote(draft["broker"], draft["symbol"])
                fresh_last = _to_float_or_none(fresh_q.get("last") or fresh_q.get("bid_1"))
                if fresh_last and fresh_last > 0:
                    retry_px = round(
                        validator.get_short_min_price(fresh_last, symbol=draft["symbol"], min_ticks=1),
                        2,
                    )
                    if retry_px > float(draft["price"]):
                        retry_draft = {
                            **draft,
                            "draft_id": f"{draft_id}-u1",
                            "price": retry_px,
                            "total_thb": round(int(draft["volume"]) * retry_px, 2),
                        }
                        retry_exec = await execute_confirmed_order(
                            retry_draft, dry_run=is_dry, last_trade_price=fresh_last
                        )
                        if retry_exec.get("success"):
                            draft = retry_draft
                            exec_res = retry_exec
                            retried_uptick = True
                        else:
                            exec_res = retry_exec
            except Exception as retry_exc:
                logger.warning("Auto-retry SHORT uptick failed for %s: %s", draft["symbol"], retry_exc)

        if not exec_res.get("success"):
            err_msg = str(exec_res.get("error") or exec_res.get("status") or "ส่งคำสั่งไม่สำเร็จ")
            if notify_line:
                await best_effort_push_card(
                    cards.trade_receipt_card(
                        draft_id=draft_id,
                        outcome="failed",
                        cmd=draft["cmd"],
                        symbol=draft["symbol"],
                        volume=draft["volume"],
                        price=draft["price"],
                        broker=draft["broker"],
                        account=draft["account"],
                        account_no=draft["account_no"],
                        dry_run=is_dry,
                        error=err_msg,
                    )
                )
            raise FacadeHttpError(
                422 if str(exec_res.get("error_code") or "").startswith("E_") else 502,
                str(exec_res.get("error_code") or exec_res.get("status") or "ORDER_EXECUTION_FAILED"),
                err_msg,
            )

        pushed = False
        if notify_line:
            receipt_card = cards.trade_receipt_card(
                draft_id=draft_id,
                outcome="confirmed",
                cmd=draft["cmd"],
                symbol=draft["symbol"],
                volume=draft["volume"],
                price=draft["price"],
                broker=draft["broker"],
                account=draft["account"],
                account_no=draft["account_no"],
                broker_order_no=str(exec_res.get("broker_order_no") or "") or None,
                dry_run=is_dry,
            )
            pushed = await best_effort_push_card(receipt_card)

        response_payload = {
            "ok": True,
            "idempotent_replay": False,
            "idempotency_key": key,
            "draft_id": draft_id,
            "order": draft,
            "execution": exec_res,
            "dry_run": is_dry,
            "retried_uptick": retried_uptick,
            "line_pushed": pushed,
        }
        _TRADE_IDEMPOTENCY[key] = {"ts": time.monotonic(), "response": response_payload}
        if draft["cmd"] in ("sell", "cover") and not key.startswith("runbook-exit:"):
            try:
                from app.services.runbook_exit_service import runbook_exit_service
                runbook_exit_service.record_manual_trade(
                    broker=draft["broker"],
                    symbol=draft["symbol"],
                    side=draft["cmd"],
                )
            except Exception as exc:
                logger.debug("record_manual_trade skipped: %s", exc)
        return response_payload


async def _resolve_sbl_borrow_volume(
    symbol: str,
    *,
    volume: Optional[int],
    amount_mode: str,
    target_value: Optional[float],
    ref_price: Optional[float],
) -> tuple[int, Optional[float]]:
    """Resolve SBL borrow share count in board lots from explicit shares or THB budget."""
    clean_mode = (amount_mode or "volume").strip().lower()
    if clean_mode == "value":
        budget = _to_float_or_none(target_value)
        if budget is None or budget <= 0:
            raise FacadeHttpError(422, "E_BAD_AMOUNT", "กรุณาระบุจำนวนเงินเป้าหมาย (บาท) ที่มากกว่า 0")
        px = _to_float_or_none(ref_price)
        if px is None or px <= 0:
            quote = await _broker_quote("kss", symbol)
            px = _to_float_or_none(quote.get("offer_1") or quote.get("last") or quote.get("bid_1"))
        if px is None or px <= 0:
            raise FacadeHttpError(422, "E_NO_QUOTE", f"อ่านราคาสด {symbol} สำหรับคำนวณจำนวนหุ้นยืมไม่ได้")
        from decimal import Decimal

        vol = int((Decimal(str(budget)) / Decimal(str(px))) // 100) * 100
        if vol < 100:
            raise FacadeHttpError(
                422,
                "E_BAD_LOT",
                f"งบ ฿{budget:,.0f} @ ฿{px:,.2f} คำนวณได้ไม่ถึง 100 หุ้น (Board Lot)",
            )
        return vol, float(px)

    vol = int(volume or 0)
    if vol <= 0 or vol % 100 != 0:
        raise FacadeHttpError(422, "E_BAD_LOT", "จำนวนหุ้นยืมต้องเป็นทวีคูณของ 100 หุ้น (Board Lot)")
    return vol, _to_float_or_none(ref_price)


async def _persist_live_sbl_borrow_lot(
    symbol: str,
    volume: int,
    fee_rate: float,
    borrow_res: Dict[str, Any],
    borrow_price: Optional[float],
) -> bool:
    """Persist ACTIVE SBLBorrowLot on LIVE borrow so portfolio (B) and Short guard stay synced."""
    try:
        from app.db.models import SBLBorrowLot
        from app.db.session import AsyncSessionLocal

        lot = SBLBorrowLot(
            lot_id=f"SBL-{symbol}-{int(time.time() * 1000)}",
            broker_tx_id=str(borrow_res.get("transaction_id") or ""),
            symbol=symbol,
            borrow_qty=volume,
            remaining_qty=volume,
            borrow_price=borrow_price,
            borrow_fee_rate=float(fee_rate),
            borrow_date=datetime.now().date(),
            status="ACTIVE",
        )
        async with AsyncSessionLocal() as session:
            session.add(lot)
            await session.commit()
        return True
    except Exception as exc:
        logger.warning("LIVE SBLBorrowLot persistence skipped for %s: %s", symbol, exc)
        return False


async def _validate_dry_run_follow_up_short(
    symbol: str,
    volume: int,
    short_price: Optional[float],
) -> Dict[str, Any]:
    """Validate tick/board-lot/uptick for a simulated DRY-RUN SHORT without faking an SBLBorrowLot row."""
    quote_data = await compute_nego_quote(broker="kss", symbol=symbol, side="SHORT")
    last_px = _to_float_or_none(quote_data.get("last"))
    open_plan = quote_data.get("open_plan") or {}
    lure = quote_data.get("lure_plan") or {}
    px = _to_float_or_none(short_price)
    if px is None or px <= 0:
        px = _to_float_or_none(
            open_plan.get("price") or lure.get("price") or lure.get("recommended_price")
        )
    if px is None or px <= 0:
        return {
            "attempted": True,
            "ok": False,
            "dry_run": True,
            "code": "E_NO_QUOTE",
            "error": f"อ่านราคาอ้างอิง Short ของ {symbol} จาก KSS ไม่ได้ — กรุณาระบุราคา Limit",
        }
    ok, err = validator.validate_all(
        symbol=symbol,
        side="SHORT",
        volume=volume,
        price=px,
        last_trade_price=last_px,
        record_idempotency=False,
    )
    if ok and last_px is not None:
        ok, err = validator.validate_short_uptick(px, last_px, symbol=symbol, context="open")
    if not ok:
        return {"attempted": True, "ok": False, "dry_run": True, "code": "E_VALIDATOR", "error": err}
    return {
        "attempted": True,
        "ok": True,
        "dry_run": True,
        "status": "SHORT_DRY_RUN_SIMULATED",
        "symbol": symbol,
        "volume": volume,
        "price": px,
        "last": last_px,
        "total_thb": round(volume * px, 2),
    }


async def _run_borrow_follow_up_short(
    *,
    symbol: str,
    volume: int,
    is_dry: bool,
    short_price: Optional[float],
    idempotency_key: Optional[str],
) -> Dict[str, Any]:
    """Optionally chain a KSS Margin SHORT right after borrow without rolling back the borrow."""
    idem = (idempotency_key or "").strip() or f"sblshort-{uuid.uuid4().hex[:12]}"
    try:
        if is_dry:
            return await _validate_dry_run_follow_up_short(symbol, volume, short_price)

        res = await execute_trade_with_idempotency(
            idempotency_key=idem,
            notify_line=False,
            side="short",
            symbol=symbol,
            broker="kss",
            account="margin",
            amount_mode="volume",
            volume=volume,
            price=short_price,
        )
        return {"attempted": True, "ok": True, "dry_run": False, **res}
    except FacadeHttpError as exc:
        return {"attempted": True, "ok": False, "dry_run": is_dry, "code": exc.code, "error": str(exc)}
    except Exception as exc:
        return {
            "attempted": True,
            "ok": False,
            "dry_run": is_dry,
            "code": type(exc).__name__,
            "error": str(exc),
        }


async def _fetch_active_sbl_lots(symbol: str, *, force_refresh: bool = True) -> List[Dict[str, Any]]:
    try:
        return await _get_active_sbl_lots(symbol, db=None, force_refresh=force_refresh)
    except TypeError:
        return await _get_active_sbl_lots(symbol, db=None)


async def execute_sbl_action(
    *,
    action: str,
    symbol: str,
    volume: Optional[int] = None,
    amount_mode: str = "volume",
    target_value: Optional[float] = None,
    ref_price: Optional[float] = None,
    auto_short: bool = False,
    short_price: Optional[float] = None,
    idempotency_key: Optional[str] = None,
    lot_id: Optional[str] = None,
    remark: Optional[str] = None,
    notify_line: bool = True,
) -> Dict[str, Any]:
    """Borrow (by volume or THB budget, with optional auto-Short) or return SBL shares on KSS Margin."""
    _ensure_not_killed()
    clean_action = (action or "").strip().lower()
    clean_sym = (symbol or "").strip().upper()
    if not clean_sym:
        raise FacadeHttpError(400, "E_BAD_SYMBOL", "กรุณาระบุชื่อหุ้น")

    if clean_action == "borrow":
        vol, used_ref_px = await _resolve_sbl_borrow_volume(
            clean_sym,
            volume=volume,
            amount_mode=amount_mode,
            target_value=target_value,
            ref_price=ref_price,
        )
        risk_res = await sbl_intelligence_service.check_xd_risk(clean_sym)
        if risk_res.get("xd_risk"):
            xd_err = str(
                risk_res.get("reason")
                or f"⛔ ห้ามยืมหุ้น {clean_sym} เนื่องจากติดเครื่องหมาย {risk_res.get('type', 'XD')}"
            )
            await best_effort_push_card(
                cards.sbl_result_card(
                    action="borrow",
                    symbol=clean_sym,
                    volume=vol,
                    dry_run=_is_dry_mode(),
                    success=False,
                    error=xd_err,
                    ts=_bkk_ts(),
                )
            )
            raise FacadeHttpError(
                422,
                "E_SBL_XD_BLOCKED",
                xd_err,
            )
        res = await krungsri_sbl_driver.execute_sbl_borrow(clean_sym, vol)
        ok = bool(res.get("success")) or res.get("status") in ("SUBMITTED", "SBL_BORROW_DRY_RUN")
        if not ok:
            status_val = str(res.get("status") or "")
            code = (
                "E_SBL_XD_BLOCKED"
                if status_val == "BLOCKED_BY_XD_XR"
                else ("E_SBL_STALE_CACHE" if status_val == "BLOCKED_BY_STALE_CACHE" else "SBL_BORROW_FAILED")
            )
            err_msg = str(res.get("error") or "ขอยืมหุ้น SBL ไม่สำเร็จ")
            await best_effort_push_card(
                cards.sbl_result_card(
                    action="borrow",
                    symbol=clean_sym,
                    volume=vol,
                    dry_run=_is_dry_mode(),
                    success=False,
                    error=err_msg,
                    ts=_bkk_ts(),
                )
            )
            raise FacadeHttpError(422, code, err_msg)
        krungsri_sbl_driver.invalidate_cache()
        is_dry = _is_dry_mode() or res.get("status") == "SBL_BORROW_DRY_RUN"

        fee_rate = _to_float_or_none(res.get("fee_rate"))
        if fee_rate is None and hasattr(krungsri_sbl_driver, "check_symbol_risk"):
            try:
                fee_rate = _to_float_or_none((krungsri_sbl_driver.check_symbol_risk(clean_sym) or {}).get("borrow_fee_pct"))
            except Exception:
                fee_rate = None
        persisted = False
        if not is_dry:
            persisted = await _persist_live_sbl_borrow_lot(
                clean_sym, vol, fee_rate if fee_rate is not None else 3.50, res, used_ref_px
            )

        out: Dict[str, Any] = {
            "ok": True,
            "action": "borrow",
            "symbol": clean_sym,
            "volume": vol,
            "ref_price": used_ref_px,
            "target_value": _to_float_or_none(target_value),
            "dry_run": is_dry,
            "persisted": persisted,
            "result": res,
        }
        if auto_short:
            short_outcome = await _run_borrow_follow_up_short(
                symbol=clean_sym,
                volume=vol,
                is_dry=is_dry,
                short_price=short_price,
                idempotency_key=idempotency_key,
            )
            out["auto_short"] = short_outcome
            out["partial_short_failed"] = not bool(short_outcome.get("ok"))
        out["line_pushed"] = await best_effort_push_card(
            cards.sbl_result_card(
                action="borrow",
                symbol=clean_sym,
                volume=vol,
                dry_run=is_dry,
                success=True,
                auto_short=out.get("auto_short"),
                ts=_bkk_ts(),
            )
        )
        return out

    if clean_action == "return":
        lots = await _fetch_active_sbl_lots(clean_sym, force_refresh=True)
        if not lots:
            raise FacadeHttpError(404, "CLI_SBL_NO_LOT", f"ไม่พบหุ้นยืม SBL ของ {clean_sym} ที่สามารถคืนได้")

        if amount_mode == "all" or volume is None:
            ret_qty_arg: Optional[int] = None
        else:
            ret_qty_arg = int(volume)
            if ret_qty_arg <= 0 or ret_qty_arg % 100 != 0:
                raise FacadeHttpError(422, "E_BAD_LOT", "จำนวนหุ้นคืนต้องเป็นทวีคูณของ 100 หุ้น")

        try:
            plan = plan_fifo_return_lots(
                lots,
                symbol=clean_sym,
                return_qty=ret_qty_arg,
                lot_id=lot_id,
            )
        except ValueError as exc:
            err_code = str(exc)
            if err_code.startswith("INSUFFICIENT_SBL_LOT:"):
                parts = err_code.split(":")
                avail_qty = int(parts[1]) if len(parts) > 1 else sum(int(l.get("remaining_qty") or 0) for l in lots)
                raise FacadeHttpError(
                    422,
                    "CLI_SBL_INSUFFICIENT_LOT",
                    f"มีหุ้นยืม {clean_sym} คงเหลือ {avail_qty:,} หุ้น (สั่งคืน {ret_qty_arg or 0:,})",
                )
            if err_code == "NO_ACTIVE_SBL_LOT" and lot_id:
                raise FacadeHttpError(
                    404,
                    "CLI_SBL_LOT_NOT_FOUND",
                    f"ไม่พบสัญญา SBL {lot_id} ของ {clean_sym} ที่เปิดอยู่",
                )
            raise FacadeHttpError(404, "CLI_SBL_NO_LOT", f"ไม่พบหุ้นยืม SBL ของ {clean_sym} ที่สามารถคืนได้")

        ret_qty = sum(int(item["qty"]) for item in plan)
        res = await krungsri_sbl_driver.return_sbl_shares_for_symbol(
            symbol=clean_sym,
            lots=lots,
            return_qty=ret_qty,
            lot_id=lot_id,
            remark=(remark or "").strip(),
        )
        ok = bool(res.get("success")) or res.get("status") in ("SUBMITTED", "SBL_RETURN_DRY_RUN", "DRY_RUN_ARMED")
        if not ok:
            status_str = str(res.get("status") or "SBL_RETURN_FAILED")
            http_status = 422 if status_str in ("NOT_RETURNABLE", "INSUFFICIENT_SBL_LOT", "NO_ACTIVE_SBL_LOT") else 502
            err_msg = str(res.get("error") or "ส่งคำขอคืนหุ้น SBL ไม่สำเร็จ")
            extra: Dict[str, Any] = {}
            for key in ("broker_message", "proof", "trace", "completed_slices", "completed_qty", "failed_slice"):
                if res.get(key) is not None:
                    extra[key] = res[key]
            if notify_line:
                await best_effort_push_card(
                    cards.sbl_result_card(
                        action="return",
                        symbol=clean_sym,
                        volume=int(res.get("completed_qty") or ret_qty or 0),
                        lots_returned=res.get("completed_slices") or [
                            {"lot_id": p["lot_id"], "ref_no": p["ref_no"], "qty": p["qty"]} for p in plan
                        ],
                        dry_run=_is_dry_mode(),
                        success=False,
                        error=err_msg,
                        ts=_bkk_ts(),
                    )
                )
            raise FacadeHttpError(
                http_status,
                status_str,
                err_msg,
                extra=extra or None,
            )

        krungsri_sbl_driver.invalidate_cache()
        clear_cdp_portfolio_cache()
        is_dry = (
            _is_dry_mode()
            or bool(res.get("dry_run"))
            or res.get("status") in ("SBL_RETURN_DRY_RUN", "DRY_RUN_ARMED")
        )
        primary_lot_id = res.get("lot_id") or (plan[0]["lot_id"] if plan else clean_sym)
        lots_returned = res.get("lots_returned") or [
            {"lot_id": p["lot_id"], "ref_no": p["ref_no"], "qty": p["qty"]} for p in plan
        ]
        pushed = (
            await best_effort_push_card(
                cards.sbl_result_card(
                    action="return",
                    symbol=clean_sym,
                    volume=ret_qty,
                    lots_returned=lots_returned,
                    dry_run=is_dry,
                    success=True,
                    ts=_bkk_ts(),
                )
            )
            if notify_line
            else False
        )
        return {
            "ok": True,
            "action": "return",
            "symbol": clean_sym,
            "lot_id": primary_lot_id,
            "lots_returned": lots_returned,
            "volume": ret_qty,
            "dry_run": is_dry,
            "line_pushed": pushed,
            "result": res,
        }

    raise FacadeHttpError(400, "E_BAD_ACTION", f"Unsupported SBL action '{action}'.")
