"""Tests for Model 19 Entry Ledger Builder & Local Managed Exit Summary."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from app.services.entry_ledger_service import (
    build_entry_ledger,
    extract_managed_exit_summary,
)

_BKK = ZoneInfo("Asia/Bangkok")


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _make_candidates(side: str, count: int = 10) -> List[Dict[str, Any]]:
    rows = []
    for i in range(1, count + 1):
        rows.append(
            {
                "ticker": f"{side[0]}STK{i}",
                "side": side,
                "origin_pool": "Pure Bear Breakdown" if side == "SHORT" else "Transition Long",
                "rank_in_table": i,
                "close": 10.0 + i,
                "pct_change": -1.0 if side == "SHORT" else -2.0,
                "atr20_pct": 1.8 if i % 2 == 0 else 3.2,
                "atr_range_str": f"[{side}] SL/TP",
                "prob_pct": 12.0 - i * 0.5,
                "weight": 100.0,
                "_extra": {
                    "pool_score": 160.0 if side == "SHORT" else 110.0,
                    "guardrail_warning": "⚠️ ALREADY HELD" if i == 1 and side == "SHORT" else None,
                    "sbl_vol": 200000 if side == "SHORT" else None,
                    "sbl_safe_val": 500000.0 if side == "SHORT" else None,
                    "sbl_im_pct": 70 if side == "SHORT" else None,
                },
            }
        )
    return rows


def test_build_entry_ledger_active_short_caps_by_free_slots(tmp_path: Path):
    scanner_dir = tmp_path / "scanner"
    pl_path = tmp_path / "positions_ledger.json"
    out_path = tmp_path / "entry_ledger.json"

    _write_json(
        scanner_dir / "candidates_2026-11-11.json",
        {
            "date": "2026-11-11",
            "generated_at": "2026-11-11T08:41:00",
            "sqn": {"score": 0.42, "classification": "Bearish"},
            "fear_and_greed": {"score": 38.0, "tier": "Fear"},
            "quota": {"long": 0, "short": 4},
            "decision": {
                "mode": "SHORT",
                "label": "ACTIVE MERGED SHORT: 4 Short [Margin 1.5x | ATR HIGH]",
                "regime_10": "Bearish Quiet",
            },
            "tournament": {"chosen_details": [{"ticker": "S1", "zone_margin_multiplier": 1.5}]},
            "candidates": _make_candidates("SHORT", 10) + _make_candidates("LONG", 10),
        },
    )
    _write_json(
        pl_path,
        {
            "as_of": "2026-11-11",
            "house_money": {"active": True, "multiplier": 1.25, "open_ret_sum_pct": 6.4, "be_locked_count": 2},
            "slot_guardrail": {
                "cap_unique_tickers": 7,
                "held_unique_tickers": 5,
                "exiting_unique_tickers": 1,
                "remaining_unique_tickers": 4,
                "free_slots": 3,
                "is_over_cap": False,
                "held_ticker_map": {
                    "CHG": {"is_exiting_today": True},
                    "SSTK2": {"is_exiting_today": False},
                },
            },
            "positions": [],
        },
    )

    ledger = build_entry_ledger(
        now=datetime(2026, 11, 11, 9, 30, tzinfo=_BKK),
        scanner_dir=str(scanner_dir),
        positions_ledger_path=str(pl_path),
        entry_ledger_path=str(out_path),
    )

    assert ledger["ledger_available"] is True
    assert ledger["is_today"] is True
    assert ledger["quota"]["model_short"] == 4
    assert ledger["quota"]["free_slots"] == 3
    assert ledger["quota"]["effective_short"] == 3
    assert ledger["quota"]["effective_long"] == 0
    assert ledger["sizing_reference"]["margin_multiplier"] == 1.5
    assert ledger["sizing_reference"]["house_money_multiplier"] == 1.25
    assert len(ledger["pools"]["short_top8"]) == 8
    assert len(ledger["pools"]["long_top8"]) == 8
    assert ledger["pools"]["short_top8"][0]["already_held"] is True  # SSTK1 guardrail warning
    assert ledger["pools"]["short_top8"][1]["already_held"] is True  # SSTK2 in held_ticker_map
    assert "CHG" in ledger["exiting_tickers"]
    assert out_path.is_file()
    persisted = json.loads(out_path.read_text(encoding="utf-8"))
    assert persisted["quota"]["effective_short"] == 3


def test_build_entry_ledger_hold_cash_and_stale_fallback(tmp_path: Path):
    scanner_dir = tmp_path / "scanner"
    pl_path = tmp_path / "positions_ledger.json"
    out_path = tmp_path / "entry_ledger.json"

    _write_json(
        scanner_dir / "candidates_2026-11-13.json",
        {
            "date": "2026-11-13",
            "quota": {"long": 0, "short": 0},
            "decision": {
                "mode": "HOLD CASH",
                "label": "HOLD CASH / STAND ASIDE: 0 Long + 0 Short [Margin 1.0x]",
                "house_money": {"active": False, "multiplier": 1.0},
            },
            "slot_guardrail": {"cap_unique_tickers": 7, "free_slots": 0, "is_over_cap": True, "held_ticker_map": {}},
            "candidates": _make_candidates("SHORT", 4),
        },
    )

    ledger = build_entry_ledger(
        now=datetime(2026, 11, 16, 9, 30, tzinfo=_BKK),
        scanner_dir=str(scanner_dir),
        positions_ledger_path=str(pl_path),
        entry_ledger_path=str(out_path),
    )

    assert ledger["ledger_available"] is True
    assert ledger["is_today"] is False
    assert ledger["as_of"] == "2026-11-13"
    assert ledger["sizing_reference"]["margin_multiplier"] == 1.0
    assert ledger["quota"]["free_slots"] == 0
    assert ledger["quota"]["effective_total"] == 0


def test_extract_managed_exit_summary_filters_strictly(tmp_path: Path):
    pl_path = tmp_path / "positions_ledger.json"
    _write_json(
        pl_path,
        {
            "as_of": "2026-11-16",
            "positions": [
                {"segment": "managed", "broker": "KSS", "symbol": "CHG", "action": "COVER", "shares": 47700, "exit_reason": "SL"},
                {"segment": "managed", "broker": "KSS", "symbol": "JAS", "action": "HOLD", "shares": 75700, "exit_reason": "HOLD"},
                {"segment": "managed", "broker": "KSS", "symbol": "LH", "action": "COVER", "shares": 21300, "exit_reason": "WEED_OUT_WATCHLIST"},
                {"segment": "legacy_dormant", "broker": "LIBERATOR", "symbol": "PTT", "action": "SELL", "shares": 500, "exit_reason": "SL"},
            ],
        },
    )

    summary = extract_managed_exit_summary(
        positions_ledger_path=str(pl_path),
        now=datetime(2026, 11, 16, 9, 30, tzinfo=_BKK),
    )
    assert summary["ledger_available"] is True
    assert summary["is_today"] is True
    assert summary["count"] == 1
    assert summary["items"][0]["symbol"] == "CHG"
    assert summary["items"][0]["action"] == "COVER"
    assert summary["items"][0]["shares"] == 47700
