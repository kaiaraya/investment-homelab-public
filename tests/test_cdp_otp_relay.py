# -*- coding: utf-8 -*-
"""Tests for scripts/broker_cdp/otp_relay.py — LINE OTP relay (แบบเดิม) in the CDP path."""
import asyncio

import pytest

from scripts.broker_cdp import otp_relay
from scripts.broker_cdp.otp_relay import (
    OTP_SESSION_LOG_PATTERN,
    OtpMatch,
    check_line_otp_once,
)

VALID_UID = "U" + "a" * 32


class _FakeRow:
    def __init__(self, details, row_id):
        self.details = details
        self.id = row_id


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeSession:
    def __init__(self, rows, captured=None):
        self._rows = rows
        self._captured = captured if captured is not None else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt):
        self._captured["stmt"] = stmt
        return _FakeResult(self._rows)


@pytest.fixture
def fake_session_factory(monkeypatch):
    """Patch AsyncSessionLocal; returns (make, captured) with the last SELECT."""
    captured: dict = {}

    def make(rows):
        def factory():
            return _FakeSession(rows, captured)

        monkeypatch.setattr(
            "scripts.broker_cdp.otp_relay.AsyncSessionLocal", factory
        )
        return captured

    return make


def test_otp_regex_matches_line_row():
    m = OTP_SESSION_LOG_PATTERN.search(
        "Received OTP 6-digits from LINE: 922412 (Ref: N/A) (Broker: KRUNGSRI)"
    )
    assert m and m.group(1) == "922412"


def test_check_line_otp_once_returns_match_for_matching_broker(fake_session_factory):
    fake_session_factory(
        [_FakeRow("Received OTP 6-digits from LINE: 111222 (Ref: N/A) (Broker: KRUNGSRI)", 77)]
    )
    match = asyncio.run(check_line_otp_once("KRUNGSRI"))
    assert match == OtpMatch(otp="111222", row_id=77)


def test_check_line_otp_once_ignores_other_broker(fake_session_factory):
    fake_session_factory(
        [_FakeRow("Received OTP 6-digits from LINE: 333444 (Ref: N/A) (Broker: LIBERATOR)", 78)]
    )
    assert asyncio.run(check_line_otp_once("KRUNGSRI")) is None


def test_check_line_otp_once_returns_none_when_empty(fake_session_factory):
    fake_session_factory([])
    assert asyncio.run(check_line_otp_once("KRUNGSRI")) is None


def test_poll_is_watermarked_to_avoid_stale_otp(fake_session_factory):
    """Stale rows (a code from an earlier login/test) must be filtered in SQL,
    otherwise the loop re-types an old code into a fresh OTP form."""
    captured = fake_session_factory([])
    asyncio.run(check_line_otp_once("KRUNGSRI", since_id=4242))
    sql = str(captured["stmt"].compile(compile_kwargs={"literal_binds": True}))
    assert "session_logs.id > 4242" in sql, sql
    assert "LINE_OTP_RECEIVED" in sql


def test_repair_line_uid_restores_malformed_value_from_env_file(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(f'LINE_ADMIN_USER_ID="{VALID_UID}"\n', encoding="utf-8")
    monkeypatch.setattr(otp_relay, "_REPO_ROOT", str(tmp_path))

    from app.core.config import settings

    monkeypatch.setattr(settings, "LINE_ADMIN_USER_ID", "U_OP", raising=False)
    otp_relay._repair_line_uid()
    assert settings.LINE_ADMIN_USER_ID == VALID_UID


def test_repair_line_uid_leaves_valid_value_alone(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text('LINE_ADMIN_USER_ID="Udeadbeef"\n', encoding="utf-8")
    monkeypatch.setattr(otp_relay, "_REPO_ROOT", str(tmp_path))

    from app.core.config import settings

    monkeypatch.setattr(settings, "LINE_ADMIN_USER_ID", VALID_UID, raising=False)
    otp_relay._repair_line_uid()
    assert settings.LINE_ADMIN_USER_ID == VALID_UID


# ────────────────────────────────────────────────────────────────────────────
# _fill_otp_input & submit button drift guard tests (t_0e05fe62)
# ────────────────────────────────────────────────────────────────────────────

class FakeElement:
    def __init__(self, visible: bool = True):
        self.visible = visible
        self.filled = None
        self.clicked = False

    async def is_visible(self) -> bool:
        return self.visible

    async def fill(self, val: str) -> None:
        self.filled = val

    async def click(self) -> None:
        self.clicked = True


class FakeKeyboard:
    def __init__(self):
        self.pressed = []

    async def press(self, key: str) -> None:
        self.pressed.append(key)


class FakePage:
    def __init__(self, elements=None):
        self.elements = elements or {}
        self.keyboard = FakeKeyboard()

    async def query_selector(self, sel: str):
        return self.elements.get(sel)


@pytest.mark.asyncio
async def test_fill_otp_input_clicks_btn_confirm():
    from scripts.broker_cdp.brokers import _fill_otp_input

    otp_input = FakeElement()
    btn_confirm = FakeElement()
    page = FakePage({
        "#otp": otp_input,
        "input#btnConfirm": btn_confirm,
    })

    res = await _fill_otp_input(page, "#otp", "<REDACTED_KRUNGSRI_PIN>")
    assert res is True
    assert otp_input.filled == "<REDACTED_KRUNGSRI_PIN>"
    assert btn_confirm.clicked is True
    assert page.keyboard.pressed == []


@pytest.mark.asyncio
async def test_fill_otp_input_fallback_enter_when_no_button():
    from scripts.broker_cdp.brokers import _fill_otp_input

    otp_input = FakeElement()
    page = FakePage({"#otp": otp_input})

    res = await _fill_otp_input(page, "#otp", "654321")
    assert res is True
    assert otp_input.filled == "654321"
    assert page.keyboard.pressed == ["Enter"]


@pytest.mark.asyncio
async def test_fill_otp_input_returns_false_if_input_missing():
    from scripts.broker_cdp.brokers import _fill_otp_input

    page = FakePage({})
    res = await _fill_otp_input(page, "#missing", "<REDACTED_KRUNGSRI_PIN>")
    assert res is False
    assert page.keyboard.pressed == []


def test_otp_submit_selectors_drift_guard():
    from scripts.broker_cdp.brokers import OTP_SUBMIT_SELECTORS as BROKER_SELECTORS
    from app.browser.auth_driver import OTP_SUBMIT_SELECTORS as AUTH_SELECTORS

    assert BROKER_SELECTORS == AUTH_SELECTORS
    assert "input#btnConfirm" in BROKER_SELECTORS
    assert "input#btnSubmit" in BROKER_SELECTORS
    assert "button:has-text('ยืนยัน')" in BROKER_SELECTORS


# ────────────────────────────────────────────────────────────────────────────
# Watermark isolation (Zero-DB seq vs SQLite row id) & LOGIN_MARKERS tests
# ────────────────────────────────────────────────────────────────────────────

def test_check_line_otp_once_isolates_zero_db_seq_from_sqlite_row_id(
    tmp_path, monkeypatch, fake_session_factory
):
    """When Zero-DB seq=1 and SQLite max id=3194, check_line_otp_once(since_id=3194, since_seq=1)
    must NOT leak seq=1 into `SessionLog.id > 1` or replay stale row 3194."""
    from app.services import otp_relay_registry as registry

    monkeypatch.setattr(registry, "_get_otp_state_file", lambda: tmp_path / "otp_state.json")
    registry.clear_otp_state()
    asyncio.run(registry.record_otp_received("888777", ref="OLD1", broker="KRUNGSRI"))
    assert registry.get_latest_otp_seq() == 1

    # Simulate fake_session_factory respecting the WHERE id > 3194 filter via SQL check
    captured = fake_session_factory([])
    match = asyncio.run(
        check_line_otp_once("KRUNGSRI", since_id=3194, since_seq=1, max_age_seconds=300)
    )
    assert match is None

    sql = str(captured["stmt"].compile(compile_kwargs={"literal_binds": True}))
    assert "session_logs.id > 3194" in sql, sql
    assert "session_logs.id > 1 " not in sql

    # Fresh Zero-DB OTP (seq=2 > since_seq=1) IS returned immediately
    asyncio.run(registry.record_otp_received("999000", ref="NEW2", broker="KRUNGSRI"))
    fresh = asyncio.run(
        check_line_otp_once("KRUNGSRI", since_id=3194, since_seq=1, max_age_seconds=300)
    )
    assert fresh == OtpMatch(otp="999000", row_id=2)
    registry.clear_otp_state()


def test_record_otp_received_monotonic_seq_across_counter_reset(tmp_path, monkeypatch):
    """Simulates gateway restart (_OTP_COUNTER reset to 0 in memory while otp_state.json has seq=5)."""
    from app.services import otp_relay_registry as registry

    monkeypatch.setattr(registry, "_get_otp_state_file", lambda: tmp_path / "otp_state.json")
    registry.clear_otp_state()

    for i in range(5):
        asyncio.run(registry.record_otp_received(f"10000{i}", ref="R1", broker="KRUNGSRI"))
    assert registry.get_latest_otp_seq() == 5

    # Simulate process restart: in-memory state resets to 0 / None, file remains on disk
    registry._OTP_COUNTER = 0
    registry._IN_MEMORY_LATEST_OTP = None

    asyncio.run(registry.record_otp_received("654321", ref="R2", broker="KRUNGSRI"))
    assert registry.get_latest_otp_seq() == 6

    # Also verify clear_pending_request removes matching broker request from file & memory
    asyncio.run(registry.record_request("KRUNGSRI", ref="ABCD"))
    assert asyncio.run(registry.resolve_pending_request()) is not None
    registry.clear_pending_request("LIBERATOR")
    assert asyncio.run(registry.resolve_pending_request()) is not None
    registry.clear_pending_request("kss")
    assert asyncio.run(registry.resolve_pending_request()) is None
    registry.clear_otp_state()


def test_login_markers_match_unauthorized_access_and_session_expired():
    from scripts.broker_cdp.brokers import LOGIN_MARKERS, is_authenticated_path

    unauth_url = "https://wme2.settrade.com/realtime/streaming7/pages/UnauthorizedAccess.html"
    expired_url = "https://wme2.settrade.com/realtime/streaming7/SessionExpired.jsp"

    assert any(m in unauth_url.lower() for m in LOGIN_MARKERS)
    assert any(m in expired_url.lower() for m in LOGIN_MARKERS)
    assert is_authenticated_path("kss", "streaming", unauth_url) is False
    assert is_authenticated_path("kss", "streaming", expired_url) is False
