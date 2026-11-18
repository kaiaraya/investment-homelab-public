import pytest
from app.services.validator import PreTradeValidator


def test_odd_lot_enforcement():
    v = PreTradeValidator()
    
    # Valid Board lots (multiple of 100)
    ok, err = v.validate_board_lot(100)
    assert ok is True
    assert err is None
    
    ok, err = v.validate_board_lot(5000)
    assert ok is True
    assert err is None

    # Invalid Odd-lots (e.g. 96 shares from video 2)
    ok, err = v.validate_board_lot(96)
    assert ok is False
    assert "Odd-lot rejected" in err

    ok, err = v.validate_board_lot(150)
    assert ok is False
    assert "Odd-lot rejected" in err

    ok, err = v.validate_board_lot(0)
    assert ok is False


def test_set_tick_sizes():
    v = PreTradeValidator()
    
    # Bracket < 2.00 (Tick = 0.01)
    ok, _ = v.validate_tick_size(1.55)
    assert ok is True

    # Bracket 2.00 - 4.98 (Tick = 0.02)
    ok, _ = v.validate_tick_size(2.36)
    assert ok is True
    ok, err = v.validate_tick_size(2.35)
    assert ok is False
    assert "Tick size violation" in err

    # Bracket 5.00 - 9.98 (Tick = 0.02, SET 16 Nov 2569)
    ok, _ = v.validate_tick_size(6.80)
    assert ok is True
    ok, _ = v.validate_tick_size(5.88)
    assert ok is True
    ok, _ = v.validate_tick_size(6.82)
    assert ok is True
    ok, err = v.validate_tick_size(6.83)
    assert ok is False

    # Bracket 10.00 - 24.95 (Tick = 0.05, SET 16 Nov 2569)
    ok, _ = v.validate_tick_size(11.10)
    assert ok is True
    ok, _ = v.validate_tick_size(11.15)
    assert ok is True
    ok, err = v.validate_tick_size(11.12)
    assert ok is False

    # Bracket 25.00 - 49.90 (Tick = 0.10, SET 16 Nov 2569)
    ok, _ = v.validate_tick_size(49.80)
    assert ok is True
    ok, _ = v.validate_tick_size(42.80)
    assert ok is True
    ok, err = v.validate_tick_size(46.25)
    assert ok is False

    # Bracket 50.00 - 99.75 (Tick = 0.25)
    ok, _ = v.validate_tick_size(62.50)
    assert ok is True
    ok, err = v.validate_tick_size(62.60)
    assert ok is False

    # Bracket 200.00 - 399.00 (Tick = 1.00)
    ok, _ = v.validate_tick_size(276.00)
    assert ok is True


def test_max_order_value():
    v = PreTradeValidator()
    
    # Within 1M THB limit
    ok, _ = v.validate_order_value(1000, 50.0)  # 50,000 THB
    assert ok is True

    # Exceeding 1M THB limit
    ok, err = v.validate_order_value(100000, 50.0)  # 5,000,000 THB
    assert ok is False
    assert "Risk ceiling exceeded" in err


def test_idempotency_guard():
    v = PreTradeValidator()
    
    # First submission
    ok, _ = v.check_idempotency("AP", "BUY", 1000, 8.20)
    assert ok is True

    # Immediate duplicate submission
    ok, err = v.check_idempotency("AP", "BUY", 1000, 8.20)
    assert ok is False
    assert "Duplicate order blocked" in err

    # Different symbol or side is allowed
    ok, _ = v.check_idempotency("TRUE", "BUY", 1000, 8.20)
    assert ok is True


def test_short_uptick_rule(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING_RULES_PATH", str(tmp_path / "trading_rules.json"))
    v = PreTradeValidator()

    # Default (+0 Zero-Plus): equal to or above last trade price is allowed
    ok, err = v.validate_short_uptick(price=8.22, last_trade_price=8.20)
    assert ok is True
    assert err is None

    ok, err = v.validate_short_uptick(price=8.20, last_trade_price=8.20)
    assert ok is True
    assert err is None

    # Short below last trade price is always rejected
    ok, err = v.validate_short_uptick(price=8.18, last_trade_price=8.20)
    assert ok is False
    assert "SET Short Sale Rule Violation" in err

    # Strict +1 mode rejects equal to last and requires >= next tick
    ok, err = v.validate_short_uptick(price=8.20, last_trade_price=8.20, min_ticks=1)
    assert ok is False
    assert "SET Short Sale Rule Violation" in err

    ok, err = v.validate_short_uptick(price=8.22, last_trade_price=8.20, min_ticks=1)
    assert ok is True

    # Persisted per-symbol +1 override applies to that ticker only
    from app.services import trading_rules
    trading_rules.save_rules(short_uptick_ticks=0, strict_uptick_symbols=["DELTA"])
    ok_delta, _ = v.validate_short_uptick(price=100.0, last_trade_price=100.0, symbol="DELTA")
    assert ok_delta is False
    ok_ptt, _ = v.validate_short_uptick(price=34.0, last_trade_price=34.0, symbol="PTT")
    assert ok_ptt is True


def test_get_next_tick():
    v = PreTradeValidator()

    # Tick bracket 5.00 - 9.98 (Tick = 0.02)
    assert v.get_next_tick(8.20, steps=1) == 8.22
    assert v.get_next_tick(8.20, steps=2) == 8.24
    assert v.get_next_tick(8.20, steps=-1) == 8.18

    # Tick bracket crossing boundary: 9.98 -> 10.00 -> 10.05; 24.95 -> 25.00 -> 25.10; 49.90 -> 50.00 -> 50.25
    assert v.get_next_tick(9.98, steps=1) == 10.00
    assert v.get_next_tick(10.00, steps=1) == 10.05
    assert v.get_next_tick(10.05, steps=-1) == 10.00
    assert v.get_next_tick(25.00, steps=1) == 25.10
    assert v.get_next_tick(50.00, steps=1) == 50.25


def test_split_open_vs_nego_short_policy_and_minus10_cache(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.services import trading_rules

    monkeypatch.setenv("TRADING_RULES_PATH", str(tmp_path / "rules.json"))
    prices_csv = tmp_path / "prices_2026_cache.csv"
    prices_csv.write_text(
        "Date,AOT,DELTA,JTS,PRIME\n"
        "2026-11-17,60.0,100.0,15.15,0.05\n"
        "2026-11-18,59.0,90.10,12.15,0.04\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "DAILY_PRICES_CACHE_CSV_PATH", str(prices_csv))
    trading_rules.clear_runtime_strict_symbols()

    v = PreTradeValidator()
    # Default policy: Open = +1, Nego = +0 (auto -10% on)
    # JTS (-19.8%) & PRIME (-20.0%) promoted to +1 for Nego; DELTA (-9.9%) stays +0 for Nego
    assert trading_rules.get_short_uptick_ticks("PTT", context="open") == 1
    assert trading_rules.get_short_uptick_ticks("PTT", context="nego") == 0
    assert trading_rules.get_short_uptick_ticks("DELTA", context="nego") == 0
    assert trading_rules.get_short_uptick_ticks("JTS", context="nego") == 1
    assert trading_rules.get_short_uptick_ticks("PRIME", context="nego") == 1

    ok_open, _ = v.validate_short_uptick(price=10.0, last_trade_price=10.0, symbol="PTT", context="open")
    assert ok_open is False
    ok_open_plus1, _ = v.validate_short_uptick(price=10.05, last_trade_price=10.0, symbol="PTT", context="open")
    assert ok_open_plus1 is True
    ok_nego_jts, _ = v.validate_short_uptick(price=12.15, last_trade_price=12.15, symbol="JTS", context="nego")
    assert ok_nego_jts is False

    # Runtime broker reject promotes symbol to +1 for Nego immediately today
    trading_rules.mark_runtime_strict_symbol("DELTA")
    assert trading_rules.get_short_uptick_ticks("DELTA", context="nego") == 1
    assert trading_rules.is_short_uptick_rejection("BROKER_REJECTED", "Short Sell order must be higher than last price") is True
    assert trading_rules.is_short_uptick_rejection("E_PIN", "Wrong PIN 6 digits") is False
    trading_rules.clear_runtime_strict_symbols()

