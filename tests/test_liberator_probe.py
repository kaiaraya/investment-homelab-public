# -*- coding: utf-8 -*-
"""Unit tests for Liberator Persistent Session & Probe Toolkit (via shared broker_cdp)."""
import argparse
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from scripts.broker_cdp.client import (
    is_cdp_available,
    resolve_target_tab,
)
from scripts.broker_probe import cmd_status, cmd_handoff


def test_liberator_is_cdp_available_offline():
    assert not is_cdp_available("http://127.0.0.1:59998", timeout=0.2)


def test_liberator_is_cdp_available_mock_online():
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"Browser": "Chrome/120.0", "webSocketDebuggerUrl": "ws://..."}).encode()
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        assert is_cdp_available("http://127.0.0.1:9334")


@pytest.mark.asyncio
async def test_liberator_resolve_target_tab():
    ctx = MagicMock()
    page_portfolio = MagicMock()
    page_portfolio.url = "https://webtrade.liberator.co.th/Portfolio"
    page_portfolio.title = AsyncMock(return_value="Liberator Portfolio")

    page_trade = MagicMock()
    page_trade.url = "https://webtrade.liberator.co.th/Trade"
    page_trade.title = AsyncMock(return_value="Liberator Trade & Order")

    page_market = MagicMock()
    page_market.url = "https://webtrade.liberator.co.th/Market"
    page_market.title = AsyncMock(return_value="Liberator Market Overview")

    ctx.pages = [page_portfolio, page_trade, page_market]

    # Resolve Portfolio
    assert await resolve_target_tab(ctx, "portfolio", "liberator") == page_portfolio
    # Resolve Trade
    assert await resolve_target_tab(ctx, "trade", "liberator") == page_trade
    # Resolve Market
    assert await resolve_target_tab(ctx, "market", "liberator") == page_market


@pytest.mark.asyncio
async def test_liberator_cmd_status_offline():
    args = argparse.Namespace(broker="liberator", cdp_url="http://127.0.0.1:59998")
    rc = await cmd_status(args)
    assert rc == 1


@pytest.mark.asyncio
async def test_liberator_cmd_handoff_generation(tmp_path, monkeypatch):
    probes_dir = tmp_path / "probes"
    handoff_dir = tmp_path / "handoff"
    probes_dir.mkdir()
    handoff_dir.mkdir()

    monkeypatch.setattr("scripts.broker_probe.PROBE_DATA_DIR", str(probes_dir))
    monkeypatch.setattr("scripts.broker_probe.HANDOFF_DIR", str(handoff_dir))

    # Create dummy Liberator probe json
    dummy_probe = {
        "timestamp": "20261020_174000",
        "broker": "liberator",
        "target": "trade",
        "url": "https://webtrade.liberator.co.th/Trade",
        "title": "Liberator Trade",
        "element_count": 2,
        "elements": [
            {
                "tag": "button",
                "suggested_selector": "#btn-buy",
                "is_visible": True,
                "text": "Buy Stock",
                "role": "button",
            },
            {
                "tag": "input",
                "suggested_selector": "input[name='pin']",
                "is_visible": True,
                "text": "",
                "name": "pin",
            },
        ],
    }
    probe_file = probes_dir / "probe_liberator_trade_20261020_174000.json"
    probe_file.write_text(json.dumps(dummy_probe, ensure_ascii=False))

    args = argparse.Namespace(broker="liberator", target="trade", cdp_url="http://127.0.0.1:9334")
    rc = await cmd_handoff(args)
    assert rc == 0

    # Verify generated handoff markdown
    generated = list(handoff_dir.glob("PROBE-LIBERATOR-TRADE-*.md"))
    assert len(generated) == 1
    content = generated[0].read_text(encoding="utf-8")
    assert "# Handoff: DOM Probe Report — LIBERATOR (TRADE)" in content
    assert "#btn-buy" in content
    assert "Buy Stock" in content


def test_liberator_inspect_dom_or_splits_comma_keywords():
    """Comma-separated --focus keywords must be OR'd, not one literal regex."""
    import inspect
    from scripts.broker_cdp.client import inspect_dom_elements

    src = inspect.getsource(inspect_dom_elements)
    assert "split(',')" in src
    assert ".join('|')" in src


@pytest.mark.asyncio
async def test_liberator_inspect_dom_passes_focus_through():
    from scripts.broker_cdp.client import inspect_dom_elements

    page = MagicMock()
    page.evaluate = AsyncMock(return_value=[])

    result = await inspect_dom_elements(page, keyword_filter="line,cash,balance", max_elements=10)

    assert result == []
    page.evaluate.assert_awaited_once()
    assert page.evaluate.call_args[0][1][1] == "line,cash,balance"
    assert page.evaluate.call_args[0][1][2] == 10


def test_liberator_is_authenticated_landing():
    """The OTP page must never be mistaken for an authenticated landing (webtrade/trade bug)."""
    from scripts.broker_cdp.brokers import _is_authenticated_landing

    # OTP page (the exact false-positive from start-session) is NOT authenticated.
    assert not _is_authenticated_landing("https://webtrade.liberator.co.th/otp?telNo=061XXX6363")
    # Login page is NOT authenticated.
    assert not _is_authenticated_landing("https://webtrade.liberator.co.th/login")
    # A bare root redirect must not win.
    assert not _is_authenticated_landing("https://webtrade.liberator.co.th/")
    # Real authenticated pages pass.
    assert _is_authenticated_landing("https://webtrade.liberator.co.th/Portfolio")
    assert _is_authenticated_landing("https://webtrade.liberator.co.th/Trade")


@pytest.mark.asyncio
async def test_liberator_open_target_tabs_skips_when_trade_open():
    from scripts.broker_cdp.brokers import liberator_open_tabs

    ctx = MagicMock()
    page_trade = MagicMock()
    page_trade.url = "https://webtrade.liberator.co.th/Trade"
    ctx.pages = [page_trade]
    ctx.new_page = AsyncMock()

    await liberator_open_tabs(ctx)
    ctx.new_page.assert_not_called()


@pytest.mark.asyncio
async def test_liberator_open_target_tabs_skips_when_market_or_portfolio_open():
    """Liberator WebTrade is a single SPA (/Market, /Portfolio, /Trade); never open duplicate tabs."""
    from scripts.broker_cdp.brokers import liberator_open_tabs

    for url in (
        "https://webtrade.liberator.co.th/Portfolio",
        "https://webtrade.liberator.co.th/Market",
    ):
        ctx = MagicMock()
        page = MagicMock()
        page.url = url
        ctx.pages = [page]
        ctx.new_page = AsyncMock()

        await liberator_open_tabs(ctx)
        ctx.new_page.assert_not_called()


@pytest.mark.asyncio
async def test_liberator_open_tabs_creates_one_tab_when_empty():
    from scripts.broker_cdp.brokers import liberator_open_tabs

    ctx = MagicMock()
    ctx.pages = []
    new_page = MagicMock()
    new_page.url = "about:blank"
    new_page.goto = AsyncMock()
    ctx.new_page = AsyncMock(return_value=new_page)

    await liberator_open_tabs(ctx)
    ctx.new_page.assert_awaited_once()
    new_page.goto.assert_awaited_once_with(
        "https://webtrade.liberator.co.th/Portfolio", wait_until="domcontentloaded", timeout=20000
    )


@pytest.mark.asyncio
async def test_liberator_prune_duplicate_tabs_keeps_one_and_closes_login():
    from scripts.broker_cdp.brokers import liberator_prune_duplicate_tabs

    login_page = MagicMock()
    login_page.url = "https://webtrade.liberator.co.th/login"
    login_page.close = AsyncMock()

    m1 = MagicMock()
    m1.url = "https://webtrade.liberator.co.th/Market"
    m1.close = AsyncMock()

    m2 = MagicMock()
    m2.url = "https://webtrade.liberator.co.th/Market"
    m2.close = AsyncMock()

    blank = MagicMock()
    blank.url = "about:blank"
    blank.close = AsyncMock()

    ctx = MagicMock()
    ctx.pages = [login_page, m1, m2, blank]

    closed = await liberator_prune_duplicate_tabs(ctx)
    assert closed == 3
    m1.close.assert_not_called()
    login_page.close.assert_awaited_once()
    m2.close.assert_awaited_once()
    blank.close.assert_awaited_once()

    # Protect active login/otp flow when zero authenticated tabs exist
    ctx_login_only = MagicMock()
    login_only = MagicMock()
    login_only.url = "https://webtrade.liberator.co.th/login"
    login_only.close = AsyncMock()
    ctx_login_only.pages = [login_only]
    assert await liberator_prune_duplicate_tabs(ctx_login_only) == 0
    login_only.close.assert_not_called()


@pytest.mark.asyncio
async def test_liberator_resolve_target_tab_skips_stale_login_for_market_spa():
    login_page = MagicMock()
    login_page.url = "https://webtrade.liberator.co.th/login"
    login_page.title = AsyncMock(return_value="Liberator Login")

    market_page = MagicMock()
    market_page.url = "https://webtrade.liberator.co.th/Market"
    market_page.title = AsyncMock(return_value="Liberator")

    ctx = MagicMock()
    ctx.pages = [login_page, market_page]

    assert await resolve_target_tab(ctx, "portfolio", "liberator") is market_page
    assert await resolve_target_tab(ctx, "trade", "liberator") is market_page
    assert await resolve_target_tab(ctx, "market", "liberator") is market_page

    # Still surfaces login page when no authenticated tab exists
    ctx.pages = [login_page]
    assert await resolve_target_tab(ctx, "portfolio", "liberator") is login_page


@pytest.mark.asyncio
async def test_session_daemon_liberator_keepalive_prunes_and_never_spawns(tmp_path, monkeypatch):
    import asyncio
    from scripts.broker_cdp import session_daemon

    calls = {"sleep": 0}

    class _DoneLoop(BaseException):
        pass

    async def fake_sleep(_sec):
        calls["sleep"] += 1
        if calls["sleep"] >= 2:
            raise _DoneLoop()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    session_file = tmp_path / "liberator_session.json"
    monkeypatch.setattr(session_daemon, "LIBERATOR_SESSION_FILE", str(session_file))

    m1 = MagicMock()
    m1.url = "https://webtrade.liberator.co.th/Market"
    m1.title = AsyncMock(return_value="Liberator")
    m1.evaluate = AsyncMock(return_value={})
    m1.close = AsyncMock()

    m2 = MagicMock()
    m2.url = "https://webtrade.liberator.co.th/Market"
    m2.title = AsyncMock(return_value="Liberator")
    m2.evaluate = AsyncMock(return_value={})
    m2.close = AsyncMock()

    ctx = MagicMock()
    ctx.pages = [m1, m2]
    ctx.cookies = AsyncMock(return_value=[{"name": "sid", "value": "ok"}])
    ctx.new_page = AsyncMock()

    with pytest.raises(_DoneLoop):
        await session_daemon.run_keepalive_loop(ctx, interval_seconds=300, label="Liberator", broker="liberator")

    ctx.new_page.assert_not_called()
    m1.close.assert_not_called()
    m2.close.assert_awaited_once()
    assert session_file.exists()
