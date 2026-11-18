"""Entry Ledger Builder & Local Runbook Exit Summary (Zero-DB).

Reads local ``candidates_<DATE>.json`` produced by DailyScanner and
``positions_ledger.json`` produced by InvestorPerformance, materializing a
normalized ``data/entry_ledger.json`` snapshot (paralleling
``positions_ledger.json``) without scraping HTML reports.
"""

from __future__ import annotations

from datetime import datetime
import json
import logging
import math
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.core.config import settings

logger = logging.getLogger(__name__)
_BKK = ZoneInfo("Asia/Bangkok")

_CANDIDATES_GLOB = "candidates_*.json"
_DATE_RE = re.compile(r"^candidates_(\d{4}-\d{2}-\d{2})\.json$")
_MARGIN_RE = re.compile(r"Margin\s+([\d.]+)x", re.IGNORECASE)
_EXIT_BROKER_ROUTE: Dict[str, Tuple[str, str]] = {
    "KSS": ("kss", "margin"),
    "LIBERATOR": ("lib", "cash"),
}
_VALID_EXIT_ACTIONS = {"SELL", "COVER"}


def _now_bkk(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return datetime.now(_BKK)
    if now.tzinfo is None:
        return now.replace(tzinfo=_BKK)
    return now.astimezone(_BKK)


def _safe_float(val: Any, default: Optional[float] = None) -> Optional[float]:
    if val is None:
        return default
    try:
        out = float(val)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(out) or math.isinf(out) else out


def _safe_int(val: Any, default: int = 0) -> int:
    f = _safe_float(val)
    return int(f) if f is not None else default


def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _resolve_candidates_file(scanner_dir: Path, today_str: str) -> Tuple[Optional[Path], Optional[str]]:
    """Return today's candidates JSON if present, else the latest dated file."""
    exact = scanner_dir / f"candidates_{today_str}.json"
    if exact.is_file():
        return exact, None
    if not scanner_dir.is_dir():
        return None, f"ไม่พบโฟลเดอร์ DailyScanner output: {scanner_dir}"

    dated: List[Tuple[str, Path]] = []
    for child in scanner_dir.iterdir():
        if not child.is_file():
            continue
        m = _DATE_RE.match(child.name)
        if m:
            dated.append((m.group(1), child))
    if not dated:
        return None, f"ไม่พบไฟล์ candidates_<DATE>.json ใน {scanner_dir}"
    dated.sort(key=lambda item: item[0], reverse=True)
    return dated[0][1], None


def _extract_margin_multiplier(cand_data: Dict[str, Any]) -> float:
    chosen = ((cand_data.get("tournament") or {}).get("chosen_details")) or []
    if isinstance(chosen, list):
        for item in chosen:
            if isinstance(item, dict):
                mult = _safe_float(item.get("zone_margin_multiplier"))
                if mult is not None and mult > 0:
                    return mult
    label = str(((cand_data.get("decision") or {}).get("label")) or "")
    match = _MARGIN_RE.search(label)
    if match:
        mult = _safe_float(match.group(1))
        if mult is not None and mult > 0:
            return mult
    return 1.0


def _extract_pool_top8(
    candidates: List[Dict[str, Any]],
    side: str,
    held_tickers: set[str],
    exiting_tickers: set[str],
) -> List[Dict[str, Any]]:
    rows = [
        c
        for c in candidates
        if isinstance(c, dict) and str(c.get("side") or "").strip().upper() == side
    ]
    rows.sort(key=lambda c: _safe_int(c.get("rank_in_table"), 9999))
    out: List[Dict[str, Any]] = []
    for row in rows[:8]:
        extra = row.get("_extra") if isinstance(row.get("_extra"), dict) else {}
        ticker = str(row.get("ticker") or "").strip().upper()
        if not ticker:
            continue
        guard_warn = str(extra.get("guardrail_warning") or "").strip() or None
        is_held = ticker in held_tickers or (
            guard_warn is not None and "ALREADY HELD" in guard_warn.upper()
        )
        out.append(
            {
                "rank": _safe_int(row.get("rank_in_table"), len(out) + 1),
                "ticker": ticker,
                "side": side,
                "origin_pool": str(row.get("origin_pool") or extra.get("pool_origin") or "").strip(),
                "pool_score": _safe_float(extra.get("pool_score")),
                "close": _safe_float(row.get("close")),
                "report_pct_change": _safe_float(row.get("pct_change")),
                "atr20_pct": _safe_float(row.get("atr20_pct")),
                "atr_range_str": str(row.get("atr_range_str") or "").strip(),
                "prob_pct": _safe_float(row.get("prob_pct")),
                "weight": _safe_float(row.get("weight")),
                "already_held": is_held,
                "exiting_today": ticker in exiting_tickers,
                "guardrail_warning": guard_warn,
                "sbl_vol": _safe_int(extra.get("sbl_vol"), 0) if side == "SHORT" else None,
                "sbl_safe_val": _safe_float(extra.get("sbl_safe_val")) if side == "SHORT" else None,
                "sbl_im_pct": _safe_int(extra.get("sbl_im_pct"), 0) if side == "SHORT" else None,
            }
        )
    return out


def extract_managed_exit_summary(
    *,
    positions_ledger_path: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Read local positions_ledger.json exits cleanly without invoking CDP."""
    cur = _now_bkk(now)
    today_str = cur.strftime("%Y-%m-%d")
    path = Path(
        positions_ledger_path
        or getattr(
            settings,
            "RUNBOOK_LEDGER_PATH",
            "/srv/antigravity/shadow/InvestorPerformance/data/positions_ledger.json",
        )
    )
    if not path.is_file():
        return {
            "ledger_available": False,
            "as_of": None,
            "is_today": False,
            "count": 0,
            "items": [],
            "error": f"ไม่พบ positions_ledger.json: {path}",
        }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "ledger_available": False,
            "as_of": None,
            "is_today": False,
            "count": 0,
            "items": [],
            "error": f"อ่าน positions_ledger.json ไม่ได้: {exc}",
        }

    as_of = str(raw.get("as_of") or "").strip() or None
    grouped: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for row in raw.get("positions") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("segment") or "").strip().lower() != "managed":
            continue
        route = _EXIT_BROKER_ROUTE.get(str(row.get("broker") or "").strip().upper())
        if route is None:
            continue
        action = str(row.get("action") or "").strip().upper()
        if action not in _VALID_EXIT_ACTIONS:
            continue
        if str(row.get("exit_reason") or "").strip().upper() == "WEED_OUT_WATCHLIST":
            continue
        shares = _safe_int(row.get("shares") or row.get("volume"), 0)
        symbol = str(row.get("symbol") or "").strip().upper()
        if shares <= 0 or not symbol:
            continue
        broker, _acct = route
        key = (broker, symbol, action)
        if key not in grouped:
            grouped[key] = {
                "broker": broker,
                "symbol": symbol,
                "action": action,
                "shares": shares,
                "exit_reason": str(row.get("exit_reason") or action).strip(),
            }
        else:
            grouped[key]["shares"] += shares

    items = sorted(grouped.values(), key=lambda x: (x["broker"], x["symbol"], x["action"]))
    return {
        "ledger_available": True,
        "as_of": as_of,
        "is_today": as_of == today_str,
        "count": len(items),
        "items": items,
        "error": None,
    }


def build_entry_ledger(
    *,
    now: Optional[datetime] = None,
    scanner_dir: Optional[str] = None,
    positions_ledger_path: Optional[str] = None,
    entry_ledger_path: Optional[str] = None,
    persist: bool = True,
) -> Dict[str, Any]:
    """Build normalized entry_ledger.json from DailyScanner & InvestorPerformance JSONs."""
    cur = _now_bkk(now)
    today_str = cur.strftime("%Y-%m-%d")

    s_dir = Path(
        scanner_dir
        or getattr(settings, "DAILY_SCANNER_OUTPUT_DIR", "/srv/antigravity/shadow/DailyScanner/output")
    )
    pl_path = Path(
        positions_ledger_path
        or getattr(
            settings,
            "RUNBOOK_LEDGER_PATH",
            "/srv/antigravity/shadow/InvestorPerformance/data/positions_ledger.json",
        )
    )
    out_path = Path(
        entry_ledger_path
        or getattr(settings, "ENTRY_LEDGER_PATH", "data/entry_ledger.json")
    )

    cand_file, cand_err = _resolve_candidates_file(s_dir, today_str)
    if cand_file is None:
        return {
            "schema_version": 1,
            "ledger_available": False,
            "as_of": None,
            "is_today": False,
            "error": cand_err,
            "sources": {
                "candidates_path": None,
                "positions_ledger_path": str(pl_path),
                "entry_ledger_path": str(out_path),
            },
        }

    try:
        cand_data = json.loads(cand_file.read_text(encoding="utf-8"))
    except Exception as exc:
        return {
            "schema_version": 1,
            "ledger_available": False,
            "as_of": None,
            "is_today": False,
            "error": f"อ่าน {cand_file.name} ไม่ได้: {exc}",
            "sources": {
                "candidates_path": str(cand_file),
                "positions_ledger_path": str(pl_path),
                "entry_ledger_path": str(out_path),
            },
        }

    pl_data: Dict[str, Any] = {}
    if pl_path.is_file():
        try:
            loaded_pl = json.loads(pl_path.read_text(encoding="utf-8"))
            if isinstance(loaded_pl, dict):
                pl_data = loaded_pl
        except Exception as exc:
            logger.warning("Failed to parse positions_ledger %s: %s", pl_path, exc)

    as_of = str(cand_data.get("date") or "").strip() or None
    if as_of is None:
        m = _DATE_RE.match(cand_file.name)
        as_of = m.group(1) if m else None

    decision = cand_data.get("decision") if isinstance(cand_data.get("decision"), dict) else {}
    sqn = cand_data.get("sqn") if isinstance(cand_data.get("sqn"), dict) else {}
    fg = cand_data.get("fear_and_greed") if isinstance(cand_data.get("fear_and_greed"), dict) else {}
    raw_quota = cand_data.get("quota") if isinstance(cand_data.get("quota"), dict) else {}

    # Prefer positions_ledger guardrail/house_money when both match today or candidates lacks them.
    pl_sg = pl_data.get("slot_guardrail") if isinstance(pl_data.get("slot_guardrail"), dict) else {}
    cand_sg = (
        cand_data.get("slot_guardrail")
        if isinstance(cand_data.get("slot_guardrail"), dict)
        else decision.get("slot_guardrail")
    )
    cand_sg = cand_sg if isinstance(cand_sg, dict) else {}
    sg = pl_sg if (pl_sg and (pl_data.get("as_of") == as_of or not cand_sg)) else cand_sg

    pl_hm = pl_data.get("house_money") if isinstance(pl_data.get("house_money"), dict) else {}
    cand_hm = decision.get("house_money") if isinstance(decision.get("house_money"), dict) else {}
    hm = pl_hm if (pl_hm and (pl_data.get("as_of") == as_of or not cand_hm)) else cand_hm

    cap_unique = _safe_int(sg.get("cap_unique_tickers"), 7)
    held_unique = _safe_int(sg.get("held_unique_tickers"), 0)
    exiting_unique = _safe_int(sg.get("exiting_unique_tickers"), 0)
    remaining_unique = _safe_int(sg.get("remaining_unique_tickers"), max(0, held_unique - exiting_unique))
    is_over_cap = bool(sg.get("is_over_cap", False))
    free_slots = 0 if is_over_cap else max(0, _safe_int(sg.get("free_slots"), max(0, cap_unique - remaining_unique)))

    model_long = max(0, _safe_int(raw_quota.get("long"), 0))
    model_short = max(0, _safe_int(raw_quota.get("short"), 0))
    effective_long = min(model_long, free_slots)
    effective_short = min(model_short, free_slots)
    effective_total = min(model_long + model_short, free_slots)

    held_map = sg.get("held_ticker_map") if isinstance(sg.get("held_ticker_map"), dict) else {}
    held_tickers = sorted({str(k).strip().upper() for k in held_map.keys() if str(k).strip()})
    exiting_tickers = sorted(
        {
            str(k).strip().upper()
            for k, v in held_map.items()
            if isinstance(v, dict) and v.get("is_exiting_today")
        }
    )

    label_text = str(decision.get("label") or "")
    atr_policy = "HIGH_ATR" if "HIGH ATR" in label_text.upper() else "LOW_ATR"

    candidates = cand_data.get("candidates") if isinstance(cand_data.get("candidates"), list) else []
    held_set = set(held_tickers)
    exiting_set = set(exiting_tickers)
    long_top8 = _extract_pool_top8(candidates, "LONG", held_set, exiting_set)
    short_top8 = _extract_pool_top8(candidates, "SHORT", held_set, exiting_set)

    ledger = {
        "schema_version": 1,
        "ledger_available": True,
        "as_of": as_of,
        "is_today": as_of == today_str,
        "generated_at": str(cand_data.get("generated_at") or "").strip() or None,
        "built_at": cur.isoformat(timespec="seconds"),
        "error": None,
        "sources": {
            "candidates_path": str(cand_file),
            "positions_ledger_path": str(pl_path) if pl_path.is_file() else None,
            "positions_ledger_as_of": pl_data.get("as_of"),
            "entry_ledger_path": str(out_path),
        },
        "regime": {
            "mode": str(decision.get("mode") or "HOLD CASH").strip().upper(),
            "label": label_text,
            "regime_10": decision.get("regime_10") or sqn.get("classification"),
            "sqn_score": _safe_float(sqn.get("score")),
            "fg_score": _safe_float(fg.get("score")),
            "fg_tier": fg.get("tier"),
            "atr_policy": atr_policy,
        },
        "quota": {
            "model_long": model_long,
            "model_short": model_short,
            "cap_unique_tickers": cap_unique,
            "held_unique_tickers": held_unique,
            "exiting_unique_tickers": exiting_unique,
            "remaining_unique_tickers": remaining_unique,
            "free_slots": free_slots,
            "is_over_cap": is_over_cap,
            "effective_long": effective_long,
            "effective_short": effective_short,
            "effective_total": effective_total,
            "verdict_label": sg.get("verdict_label"),
        },
        "sizing_reference": {
            "margin_multiplier": _extract_margin_multiplier(cand_data),
            "house_money_active": bool(hm.get("active", False)),
            "house_money_multiplier": _safe_float(hm.get("multiplier"), 1.0),
            "open_ret_sum_pct": _safe_float(hm.get("open_ret_sum_pct")),
            "be_locked_count": _safe_int(hm.get("be_locked_count"), 0),
            "status_label": hm.get("status_label"),
        },
        "held_tickers": held_tickers,
        "exiting_tickers": exiting_tickers,
        "pools": {
            "long_top8": long_top8,
            "short_top8": short_top8,
        },
    }

    if persist:
        try:
            _atomic_write_json(out_path, ledger)
        except Exception as exc:
            logger.warning("Failed to persist entry_ledger %s: %s", out_path, exc)

    return ledger
