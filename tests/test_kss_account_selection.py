# -*- coding: utf-8 -*-
"""t_496c6213 — KSS CDP must read the CREDIT account (<REDACTED_KSS_ACCT>), not the empty CASH book.

The witness (CDP-UNIFY 7.2, 2026-11-04 16:54) showed ``/port kss`` answering card
``<REDACTED_KSS_ACCT>`` (CASH_BALANCE, no holdings) while the account actually in use — positions +
SBL short — is ``<REDACTED_KSS_ACCT>`` (CREDIT_BALANCE). Root cause: ``StreamingBrokerPlugin
.switch_account_type()`` was a no-op and the scrape never selected an account, so the
console's default (CASH) was read and rendered as if it were the book.

These tests drive a *fake Streaming page* (no browser) and assert:
1. the scrape selects the configured account (<REDACTED_KSS_ACCT>) before reading it;
2. a failed selection is reported on the card (never a silent empty portfolio);
3. ``/status`` names the same account number ``/port`` uses;
4. the plugin's account switch is real (no-op removed) and verified.
"""
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.brokers.streaming import plugin as streaming_plugin_mod
from app.core.config import settings
from app.services.broker_cdp import order_executor, sync_service
from app.services.line_cli import cards, handlers

#: The dropdown-option selector the driver uses for a raw account number (``text=<REDACTED_KSS_ACCT>``).
_ACCOUNT_SEL_RE = re.compile(r"\b\d{7}\b")


# ── fake Streaming console ─────────────────────────────────────────────────────
class _El:
    """A visible DOM element; clicking it optionally mutates the fake page."""

    def __init__(self, on_click=None):
        self._on_click = on_click

    async def is_visible(self):
        return True

    async def click(self, *a, **kw):
        if self._on_click:
            self._on_click()


class FakeStreamingPage:
    """Account header that moves only when the dropdown option can be clicked."""

    def __init__(self, account="<REDACTED_KSS_ACCT>", switchable=True, holdings=None,
                 account_type="Cash Balance", portfolio_btn=False):
        self.account = account
        self._initial = account
        self.switchable = switchable
        self._holdings = holdings or []
        self._account_type = account_type
        self.portfolio_btn = portfolio_btn
        self.calls = []          # ordered log of what the scraper did

    async def evaluate(self, js, *args, **kwargs):
        if js is order_executor._ACTIVE_ACCOUNT_JS:
            return self.account
        if js is sync_service._HEADER_AC_TYPE_JS:
            return "Credit Balance" if self.account == "<REDACTED_KSS_ACCT>" else self._account_type
        if js is sync_service._KSS_SCRAPE_JS:
            self.calls.append("scrape")
            return {
                "account_no": self.account,
                "balances": {
                    "account_type": "Credit Balance" if self.account == "<REDACTED_KSS_ACCT>" else self._account_type,
                    "credit_limit": 1_200_000.0 if self.account == "<REDACTED_KSS_ACCT>" else None,
                    "line_available": 187_582.07 if self.account == "<REDACTED_KSS_ACCT>" else 0.0,
                    "cash_balance": None,
                    "excess_equity": 187_582.07 if self.account == "<REDACTED_KSS_ACCT>" else None,
                },
                "holdings": self._holdings if self.account == "<REDACTED_KSS_ACCT>" else [],
            }
        raise AssertionError(f"unexpected evaluate: {js[:60]!r}")

    async def query_selector(self, sel):
        if sel in order_executor.KSS_ACCOUNT_TRIGGER:
            self.calls.append("open_dropdown")
            return _El()
        if "Portfolio" in sel:
            if not self.portfolio_btn:
                return None
            self.calls.append("portfolio_tab")
            return _El()
        if sel.startswith("li:has-text(") or _ACCOUNT_SEL_RE.search(sel):
            target = "<REDACTED_KSS_ACCT>" if "<REDACTED_KSS_ACCT>" in sel else "<REDACTED_KSS_ACCT>"

            def _click(target=target):
                self.calls.append(f"pick_{target}")
                if self.switchable:
                    self.account = target

            return _El(_click)
        return None

    async def wait_for_timeout(self, ms):
        return None


class _FakeTab:
    def __init__(self, page):
        self._page = page

    async def __aenter__(self):
        return self._page

    async def __aexit__(self, *exc):
        return False


HOLDING = {"symbol": "BCPG", "volume": 100, "available_volume": 100, "avg_cost": 6.8,
           "current_price": 6.85, "market_value": 685.0, "unrealized_pnl": 5.0}


# ── /port scrape: account selected first, then read ───────────────────────────
@pytest.mark.asyncio
async def test_port_selects_credit_account_before_scraping(monkeypatch):
    page = FakeStreamingPage(account="<REDACTED_KSS_ACCT>", holdings=[HOLDING], portfolio_btn=True)
    monkeypatch.setattr(sync_service, "get_tab", lambda cdp, target: _FakeTab(page))

    snap = await sync_service.scrape_portfolio_from_cdp("kss")

    assert snap["account_no"] == "<REDACTED_KSS_ACCT>"
    assert snap["account_type"] == "CREDIT_BALANCE"
    assert snap["holdings"] == [HOLDING]
    assert "account_switch_error" not in snap
    # The switch happened BEFORE the balance/holdings were read.
    assert page.calls.index("pick_3024959") < page.calls.index("scrape")


@pytest.mark.asyncio
async def test_port_reports_switch_failure_instead_of_empty_book(monkeypatch):
    """Streaming showing only the CASH account must say so, not show a quiet empty port."""
    page = FakeStreamingPage(account="<REDACTED_KSS_ACCT>", switchable=False)
    monkeypatch.setattr(sync_service, "get_tab", lambda cdp, target: _FakeTab(page))

    snap = await sync_service.scrape_portfolio_from_cdp("kss")

    assert snap["account_no"] == "<REDACTED_KSS_ACCT>"
    assert snap["account_expected"] == "<REDACTED_KSS_ACCT>"
    assert "<REDACTED_KSS_ACCT>" in snap["account_switch_error"]

    note = handlers._account_switch_note(snap)
    assert note and "<REDACTED_KSS_ACCT>" in note


@pytest.mark.asyncio
async def test_port_no_note_when_account_matches(monkeypatch):
    page = FakeStreamingPage(account="<REDACTED_KSS_ACCT>", holdings=[HOLDING])
    monkeypatch.setattr(sync_service, "get_tab", lambda cdp, target: _FakeTab(page))

    snap = await sync_service.scrape_portfolio_from_cdp("kss")

    assert snap["account_no"] == "<REDACTED_KSS_ACCT>"
    assert handlers._account_switch_note(snap) is None


@pytest.mark.asyncio
async def test_sync_portfolio_passes_account_switch_fields(monkeypatch, prepare_test_db):
    page = FakeStreamingPage(account="<REDACTED_KSS_ACCT>", switchable=False)
    monkeypatch.setattr(sync_service, "get_tab", lambda cdp, target: _FakeTab(page))

    summary = await sync_service.sync_portfolio_from_cdp("kss")

    assert summary["account_no"] == "<REDACTED_KSS_ACCT>"
    assert summary["account_expected"] == "<REDACTED_KSS_ACCT>"
    assert summary["account_switch_error"]


# ── /status names the same account /port used ─────────────────────────────────
@pytest.mark.asyncio
async def test_status_reports_active_account_number(monkeypatch):
    sync_service.record_active_kss_account("<REDACTED_KSS_ACCT>", "Credit Balance")

    probe = {"broker": "kss", "purpose": "streaming", "state": "AUTHENTICATED",
             "daemon_up": True, "port": 9333, "pid": 1, "target_found": True,
             "url": "https://wme2.settrade.com/realtime/streaming7/StreamingPage.jsp",
             "url_path": "/realtime/streaming7/StreamingPage.jsp",
             "checked_at": "2026-11-04T09:54:00+00:00", "reason": "ok"}
    with patch("app.services.broker_cdp.session_probe.cdp_session_status",
               new=AsyncMock(return_value=probe)):
        h = await handlers._broker_health("kss")

    assert h["account"] == "<REDACTED_KSS_ACCT> MARGIN" == f"{sync_service.get_active_kss_account()} MARGIN"


@pytest.mark.asyncio
async def test_status_falls_back_to_configured_default(monkeypatch):
    """With nothing scraped yet, /status must still name the configured working account."""
    probe = {"broker": "kss", "purpose": "streaming", "state": "AUTHENTICATED",
             "daemon_up": True, "port": 9333, "pid": 1, "target_found": True,
             "url": None, "url_path": None, "checked_at": None, "reason": "ok"}
    with patch("app.services.broker_cdp.session_probe.cdp_session_status",
               new=AsyncMock(return_value=probe)):
        h = await handlers._broker_health("kss")

    assert h["account"] == f"{settings.STREAMING_ACCOUNT_NO_MARGIN or settings.STREAMING_ACCOUNT_NO} MARGIN"


# ── the plugin switch is real (the old no-op) ─────────────────────────────────
@pytest.mark.asyncio
async def test_plugin_switch_margin_is_real(monkeypatch):
    calls = {}

    async def _fake_switch(account_no, target="streaming"):
        calls["account_no"] = account_no
        return {"ok": True, "expected": account_no, "active": account_no, "switched": True, "error": None}

    monkeypatch.setattr(sync_service, "switch_kss_account", _fake_switch)
    ok = await streaming_plugin_mod.streaming_plugin.switch_account_type("MARGIN")

    assert ok is True
    assert calls["account_no"] == "<REDACTED_KSS_ACCT>"


@pytest.mark.asyncio
async def test_plugin_switch_cash(monkeypatch):
    calls = {}

    async def _fake_switch(account_no, target="streaming"):
        calls["account_no"] = account_no
        return {"ok": True, "expected": account_no, "active": account_no, "switched": True, "error": None}

    monkeypatch.setattr(sync_service, "switch_kss_account", _fake_switch)
    assert await streaming_plugin_mod.streaming_plugin.switch_account_type("cash") is True
    assert calls["account_no"] == "<REDACTED_KSS_ACCT>"


@pytest.mark.asyncio
async def test_plugin_switch_unknown_target_is_refused(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(sync_service, "switch_kss_account", mock)

    assert await streaming_plugin_mod.streaming_plugin.switch_account_type("derivatives") is False
    mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_plugin_switch_reports_failure(monkeypatch):
    async def _fake_switch(account_no, target="streaming"):
        return {"ok": False, "expected": account_no, "active": "<REDACTED_KSS_ACCT>", "switched": False,
                "error": f"สลับบัญชีเป็น {account_no} ไม่สำเร็จ"}

    monkeypatch.setattr(sync_service, "switch_kss_account", _fake_switch)

    assert await streaming_plugin_mod.streaming_plugin.switch_account_type("margin") is False


# ── the card renders the warning ──────────────────────────────────────────────
def test_port_card_renders_account_note():
    card = cards.port_card(broker="KSS", account="<REDACTED_KSS_ACCT>", amount=0.0, pnl_value=0.0,
                           pnl_pct=0.0, rows=[], snapshot_label="16:54", stale=False, live=True,
                           note="⚠️ แสดงบัญชี <REDACTED_KSS_ACCT> ไม่ใช่บัญชีที่ตั้งไว้")

    import json
    blob = json.dumps(card, ensure_ascii=False)
    assert "ไม่ใช่บัญชีที่ตั้งไว้" in blob


def test_port_card_without_note_has_no_warning():
    card = cards.port_card(broker="KSS", account="<REDACTED_KSS_ACCT>", amount=685.0, pnl_value=5.0,
                           pnl_pct=0.73, rows=[], snapshot_label="16:54", stale=False, live=True)
    import json
    assert "ไม่ใช่บัญชีที่ตั้งไว้" not in json.dumps(card, ensure_ascii=False)


# ── selection persistence & label across commands (F1, F2) ───────────────────
@pytest.mark.asyncio
async def test_port_persists_cash_selection_across_commands(monkeypatch):
    """AC2: selecting CASH persists so subsequent /port kss does not snap back to margin."""
    page = FakeStreamingPage(account="<REDACTED_KSS_ACCT>", holdings=[HOLDING], portfolio_btn=True)
    monkeypatch.setattr(sync_service, "get_tab", lambda cdp, target: _FakeTab(page))

    # 1. Switch to cash
    ok = await streaming_plugin_mod.streaming_plugin.switch_account_type("cash")
    assert ok is True
    assert page.account == "<REDACTED_KSS_ACCT>"
    assert sync_service.get_active_kss_account() == "<REDACTED_KSS_ACCT>"
    assert sync_service.get_active_kss_account_type() == "CASH_BALANCE"

    # 2. Scrape portfolio — must remain on CASH (<REDACTED_KSS_ACCT>), not flip back to <REDACTED_KSS_ACCT>
    snap = await sync_service.scrape_portfolio_from_cdp("kss")
    assert snap["account_no"] == "<REDACTED_KSS_ACCT>"
    assert snap["account_type"] == "CASH_BALANCE"
    assert page.account == "<REDACTED_KSS_ACCT>"

    # 3. Status also reports <REDACTED_KSS_ACCT> CASH (F2: not MARGIN)
    probe = {"broker": "kss", "purpose": "streaming", "state": "AUTHENTICATED",
             "daemon_up": True, "port": 9333, "pid": 1, "target_found": True,
             "url": "https://wme2.settrade.com/realtime/streaming7/StreamingPage.jsp",
             "url_path": "/realtime/streaming7/StreamingPage.jsp",
             "checked_at": "2026-11-04T09:54:00+00:00", "reason": "ok"}
    with patch("app.services.broker_cdp.session_probe.cdp_session_status",
               new=AsyncMock(return_value=probe)):
        h = await handlers._broker_health("kss")
    assert h["account"] == "<REDACTED_KSS_ACCT> CASH"


@pytest.mark.asyncio
async def test_status_reports_cash_label_after_switching_to_cash():
    sync_service.record_active_kss_account("<REDACTED_KSS_ACCT>", "Cash")
    assert sync_service.get_active_kss_account() == "<REDACTED_KSS_ACCT>"
    assert sync_service.get_active_kss_account_type() == "CASH_BALANCE"

    probe = {"broker": "kss", "purpose": "streaming", "state": "AUTHENTICATED",
             "daemon_up": True, "port": 9333, "pid": 1, "target_found": True,
             "url": "https://wme2.settrade.com/realtime/streaming7/StreamingPage.jsp",
             "url_path": "/realtime/streaming7/StreamingPage.jsp",
             "checked_at": "2026-11-04T09:54:00+00:00", "reason": "ok"}
    with patch("app.services.broker_cdp.session_probe.cdp_session_status",
               new=AsyncMock(return_value=probe)):
        h = await handlers._broker_health("kss")
    assert h["account"] == "<REDACTED_KSS_ACCT> CASH"



@pytest.mark.asyncio
async def test_ensure_kss_active_account_retries_when_dropdown_slow(monkeypatch):
    class SlowDropdownPage(FakeStreamingPage):
        def __init__(self):
            super().__init__(account="<REDACTED_KSS_ACCT>", switchable=True)
            self.pick_attempts = 0

        async def query_selector(self, sel):
            if sel in order_executor.KSS_ACCOUNT_TRIGGER:
                self.calls.append("open_dropdown")
                return _El()
            if "<REDACTED_KSS_ACCT>" in sel:
                self.pick_attempts += 1
                # First cycle: dropdown list hasn't hydrated <REDACTED_KSS_ACCT> yet
                if self.pick_attempts <= 4:
                    return None
                return await super().query_selector(sel)
            return None

    page = SlowDropdownPage()
    info = await sync_service.ensure_kss_active_account(page, target_account="<REDACTED_KSS_ACCT>")
    assert info["switched"] is True
    assert info["active"] == "<REDACTED_KSS_ACCT>"
    assert info["error"] is None
