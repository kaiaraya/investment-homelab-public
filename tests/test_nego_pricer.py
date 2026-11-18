"""Unit tests for ``app/services/nego_pricer.py`` — the pure price planner."""
import pytest

from app.services.nego_pricer import plan_price


def test_buy_match_hits_offer():
    r = plan_price("BUY", "match", bid=10.0, offer=10.1, last=10.05, old_price=9.9)
    assert r["ok"] is True and r["price"] == 10.1


def test_buy_lure_joins_bid():
    r = plan_price("BUY", "lure", bid=10.0, offer=10.1, last=10.05, old_price=9.9)
    assert r["ok"] is True and r["price"] == 10.0


def test_sell_match_hits_bid():
    r = plan_price("SELL", "match", bid=10.0, offer=10.1, last=10.05, old_price=10.2)
    assert r["ok"] is True and r["price"] == 10.0


def test_sell_lure_joins_offer():
    r = plan_price("SELL", "lure", bid=10.0, offer=10.1, last=10.05, old_price=10.2)
    assert r["ok"] is True and r["price"] == 10.1


def test_cover_is_a_buy_side():
    r = plan_price("COVER", "match", bid=10.0, offer=10.1, last=10.05, old_price=10.2)
    assert r["ok"] is True and r["price"] == 10.1


def test_short_match_at_bid_legal_when_bid_above_last():
    # bid 10.1 > last 10.0 -> legal
    r = plan_price("SHORT", "match", bid=10.1, offer=10.2, last=10.0, old_price=10.3)
    assert r["ok"] is True and r["price"] == 10.1


def test_short_match_refused_when_bid_not_above_last():
    # bid 9.9 <= last 10.0 -> uptick violation
    r = plan_price("SHORT", "match", bid=9.9, offer=10.0, last=10.0, old_price=10.1)
    assert r["ok"] is False and "uptick" in r["reason"]


def test_short_lure_uses_offer_when_above_last():
    r = plan_price("SHORT", "lure", bid=9.9, offer=10.1, last=10.0, old_price=10.2)
    assert r["ok"] is True and r["price"] == 10.1


def test_short_match_at_bid_respects_zero_plus_and_strict_rules():
    # +0 Zero-Plus: bid == last is legal
    r_eq = plan_price("SHORT", "match", bid=10.0, offer=10.05, last=10.0, old_price=10.2, min_ticks=0)
    assert r_eq["ok"] is True and r_eq["price"] == 10.0

    # +1 Strict Uptick: bid == last is refused
    r_strict = plan_price("SHORT", "match", bid=10.0, offer=10.05, last=10.0, old_price=10.2, min_ticks=1)
    assert r_strict["ok"] is False and "uptick" in r_strict["reason"]


def test_short_lure_bumps_above_last_when_offer_not_above():
    # +0 Zero-Plus: offer == last == 10.0 -> legal at 10.0
    r0 = plan_price("SHORT", "lure", bid=9.95, offer=10.0, last=10.0, old_price=10.2, min_ticks=0)
    assert r0["ok"] is True and r0["price"] == 10.0

    # +1 Strict Uptick: offer == last == 10.0 -> must step one tick above last (10.05)
    r1 = plan_price("SHORT", "lure", bid=9.95, offer=10.0, last=10.0, old_price=10.2, min_ticks=1)
    assert r1["ok"] is True and r1["price"] == 10.05


def test_missing_leg_fails_closed():
    assert plan_price("BUY", "match", bid=10.0, offer=None, last=10.0, old_price=9.9)["ok"] is False
    assert plan_price("SELL", "match", bid=None, offer=10.1, last=10.0, old_price=10.2)["ok"] is False
    assert plan_price("BUY", "lure", bid=None, offer=10.1, last=10.0, old_price=9.9)["ok"] is False


def test_short_without_last_fails_closed():
    r = plan_price("SHORT", "match", bid=10.1, offer=10.2, last=None, old_price=10.3)
    assert r["ok"] is False and "last" in r["reason"]


def test_no_change_when_price_equals_old():
    r = plan_price("BUY", "match", bid=10.0, offer=10.1, last=10.05, old_price=10.1)
    assert r["ok"] is False and r["reason"] == "NO_CHANGE"


def test_unknown_mode_or_side():
    assert plan_price("BUY", "weird", bid=10.0, offer=10.1, last=10.05, old_price=9.9)["ok"] is False
    assert plan_price("HOLD", "match", bid=10.0, offer=10.1, last=10.05, old_price=9.9)["ok"] is False


def test_buy_lure_clamps_to_prior_close():
    # If market moves up (bid 10.2 > prior_close 10.0), Buy lure does not chase above prior_close
    r = plan_price("BUY", "lure", bid=10.2, offer=10.3, last=10.25, old_price=9.9, prior_close=10.0)
    assert r["ok"] is True and r["price"] == 10.0

    # If bid is below prior_close, standard lure at bid
    r2 = plan_price("BUY", "lure", bid=9.8, offer=9.9, last=9.85, old_price=9.5, prior_close=10.0)
    assert r2["ok"] is True and r2["price"] == 9.8


def test_cover_lure_clamps_to_prior_close():
    # Cover is buy-side; should not chase above prior_close
    r = plan_price("COVER", "lure", bid=10.2, offer=10.3, last=10.25, old_price=9.9, prior_close=10.0)
    assert r["ok"] is True and r["price"] == 10.0


def test_sell_lure_clamps_to_prior_close():
    # If market moves down (offer 9.8 < prior_close 10.0), Sell lure does not chase below prior_close
    r = plan_price("SELL", "lure", bid=9.7, offer=9.8, last=9.75, old_price=10.2, prior_close=10.0)
    assert r["ok"] is True and r["price"] == 10.0

    # If offer is above prior_close, standard lure at offer
    r2 = plan_price("SELL", "lure", bid=10.1, offer=10.2, last=10.15, old_price=10.5, prior_close=10.0)
    assert r2["ok"] is True and r2["price"] == 10.2


def test_short_lure_clamps_to_prior_close_and_obeys_uptick():
    # If market drops (offer 9.8 < prior_close 10.0), Short lure does not drop below prior_close
    # last=9.75 < prior_close 10.0 -> target clamped to 10.0
    r = plan_price("SHORT", "lure", bid=9.7, offer=9.8, last=9.75, old_price=10.5, prior_close=10.0)
    assert r["ok"] is True and r["price"] == 10.0

    # Under +0 Zero-Plus, prior_close=9.9, last=10.0 -> floor is 10.0
    r0 = plan_price("SHORT", "lure", bid=9.95, offer=10.0, last=10.0, old_price=10.5, prior_close=9.9, min_ticks=0)
    assert r0["ok"] is True and r0["price"] == 10.0

    # Under +1 Strict Uptick, prior_close=9.9, last=10.0 -> price must be > 10.0 (10.05 under new 0.05 tick)
    r2 = plan_price("SHORT", "lure", bid=9.95, offer=10.0, last=10.0, old_price=10.5, prior_close=9.9, min_ticks=1)
    assert r2["ok"] is True and r2["price"] == 10.05


def test_short_rule_3_existing_order_stability():
    # Rule 3: If old_price == offer or old_price == last, keep it resting; return NO_CHANGE
    # Case BTS: last=1.81, offer=1.82, old_price=1.82 (Offer 1 or 2 queue), prior_close=1.82
    r = plan_price("SHORT", "lure", bid=1.80, offer=1.82, last=1.81, old_price=1.82, prior_close=1.82)
    assert r["ok"] is False and r["reason"] == "NO_CHANGE"


def test_short_uptick_best_moves_down_to_last_plus_one_tick():
    # +0 Zero-Plus: moves down to Last (10.00)
    r0 = plan_price("SHORT", "uptick_best", bid=9.95, offer=10.2, last=10.0, old_price=10.5, min_ticks=0)
    assert r0["ok"] is True and r0["price"] == 10.0

    # +1 Strict Uptick: moves down to tightest legal uptick 10.05 (last=10.00, tick=0.05)
    r = plan_price("SHORT", "uptick_best", bid=9.95, offer=10.2, last=10.0, old_price=10.5, prior_close=10.4, min_ticks=1)
    assert r["ok"] is True and r["price"] == 10.05

    # Already at or tighter than last + 1 tick -> NO_CHANGE (never moves away from market)
    r_same = plan_price("SHORT", "uptick_best", bid=9.95, offer=10.2, last=10.0, old_price=10.05, min_ticks=1)
    assert r_same["ok"] is False and r_same["reason"] == "NO_CHANGE"

    # Case where offer dropped to 1.81 (same as last=1.81), but old_price is 1.82:
    # Under +1 uptick -> next tick is 1.82. Since old_price is 1.82 -> NO_CHANGE
    r2 = plan_price("SHORT", "lure", bid=1.80, offer=1.81, last=1.81, old_price=1.82, prior_close=1.82, min_ticks=1)
    assert r2["ok"] is False and r2["reason"] == "NO_CHANGE"


def test_short_rule_3_bam_case_resting_at_offer_1():
    # BAM case: old_price=6.35, offer=6.35, last=6.35, bid=6.30.
    # Order is already queuing at Offer 1. Must NOT bump up to 6.40; must return NO_CHANGE.
    r = plan_price("SHORT", "lure", bid=6.30, offer=6.35, last=6.35, old_price=6.35, prior_close=6.30)
    assert r["ok"] is False and r["reason"] == "NO_CHANGE"


def test_short_rule_3_tpipp_case():
    # TPIPP case: old_price=1.57, offer=1.57, last=1.56, bid=1.56
    r = plan_price("SHORT", "lure", bid=1.56, offer=1.57, last=1.56, old_price=1.57, prior_close=1.56)
    assert r["ok"] is False and r["reason"] == "NO_CHANGE"


def test_short_lure_steps_down_towards_market_legally():
    # Under +0 Zero-Plus: offer=6.35, last=6.35, old_price=6.45 -> steps down to 6.35
    r0 = plan_price("SHORT", "lure", bid=6.30, offer=6.35, last=6.35, old_price=6.45, prior_close=6.30, min_ticks=0)
    assert r0["ok"] is True and r0["price"] == 6.35

    # Under +1 Strict Uptick (tick=0.02 at 6.35): lowest legal uptick is 6.37 (or 6.38? Wait: 6.35 + 0.02 = 6.37)
    r1 = plan_price("SHORT", "lure", bid=6.30, offer=6.34, last=6.34, old_price=6.44, prior_close=6.30, min_ticks=1)
    assert r1["ok"] is True and r1["price"] == 6.36


def test_short_lure_guardrail_prevents_upward_amend():
    # If order is resting at 6.30 (or any price < calculated floor),
    # never bump it upwards away from the market.
    r = plan_price("SHORT", "lure", bid=6.30, offer=6.36, last=6.36, old_price=6.30, prior_close=6.30, min_ticks=1)
    assert r["ok"] is False and r["reason"] == "NO_CHANGE"


def test_short_lure_new_order_still_obeys_uptick():
    # Brand-new order (old_price is None): obeys +0 vs +1
    r0 = plan_price("SHORT", "lure", bid=6.30, offer=6.34, last=6.34, old_price=None, prior_close=6.30, min_ticks=0)
    assert r0["ok"] is True and r0["price"] == 6.34
    r1 = plan_price("SHORT", "lure", bid=6.30, offer=6.34, last=6.34, old_price=None, prior_close=6.30, min_ticks=1)
    assert r1["ok"] is True and r1["price"] == 6.36


