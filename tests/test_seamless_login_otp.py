"""Tests for seamless KSS/LIB Login & OTP reliability improvements."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_otp_ref_regex_handles_liberator_ref_code_without_stopwords():
    from app.browser.auth_driver import KrungsriAuthDriver
    from scripts.broker_cdp.brokers import extract_otp_ref_code

    lib_text = "กรุณายืนยันรหัส OTP 6 หลัก (ref. code: KCNX)\nส่งไปยังเบอร์ 08X-XXX-1234"
    kss_text = "กรุณากรอกรหัส OTP\nรหัสอ้างอิง (Ref) : ECLK\nหมดอายุใน 3 นาที"
    ambiguous_text = "OTP Ref. code without value"

    assert extract_otp_ref_code(lib_text) == "KCNX"
    assert extract_otp_ref_code(kss_text) == "ECLK"
    assert extract_otp_ref_code(ambiguous_text) is None

    # Verify auth_driver._detect_otp_prompt uses the exact same stopword-safe extraction
    class _StubPage:
        async def query_selector(self, _sel):
            return None

        async def query_selector_all(self, _sel):
            return []

        async def inner_text(self, _sel):
            return lib_text

    drv = object.__new__(KrungsriAuthDriver)
    is_otp, ref, _ = asyncio.run(drv._detect_otp_prompt(_StubPage()))
    assert is_otp is True
    assert ref == "KCNX"


def test_has_liberator_auth_cookies_rejects_incapsula_only():
    from scripts.broker_cdp.brokers import has_liberator_auth_cookies

    incapsula_only = [
        {"name": "visid_incap_3063321", "value": "abc"},
        {"name": "nlbi_3063321", "value": "xyz"},
        {"name": "api-token", "value": ""},
    ]
    assert has_liberator_auth_cookies(incapsula_only) is False
    assert has_liberator_auth_cookies([{"name": "api-token", "value": "jwt.hdr.sig"}]) is True
    assert has_liberator_auth_cookies([{"name": "ws-token", "value": "jwt.hdr.sig"}]) is True


@pytest.mark.asyncio
async def test_confirm_liberator_authenticated_requires_path_and_cookie():
    from scripts.broker_cdp.brokers import _confirm_liberator_authenticated

    ctx = MagicMock()
    page = MagicMock()

    page.url = "https://webtrade.liberator.co.th/Portfolio"
    ctx.cookies = AsyncMock(return_value=[{"name": "visid_incap_1", "value": "123"}])
    ok, _ = await _confirm_liberator_authenticated(ctx, page)
    assert ok is False

    ctx.cookies = AsyncMock(return_value=[{"name": "api-token", "value": "eyJ.abc.def"}])
    ok2, cookies2 = await _confirm_liberator_authenticated(ctx, page)
    assert ok2 is True
    assert len(cookies2) == 1

    page.url = "https://webtrade.liberator.co.th/otp"
    ok3, _ = await _confirm_liberator_authenticated(ctx, page)
    assert ok3 is False


@pytest.mark.asyncio
async def test_liberator_login_retries_once_on_xhr_timeout_then_fail_fast(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.services.broker_cdp import login_progress
    from scripts.broker_cdp import brokers

    monkeypatch.setattr(login_progress, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(settings, "LIBERATOR_USERNAME", "<REDACTED_LIBERATOR_USERNAME>")
    monkeypatch.setattr(settings, "LIBERATOR_PASSWORD", "dummy-pw")
    monkeypatch.setattr(brokers, "LIBERATOR_SESSION_FILE", str(tmp_path / "liberator_session.json"))

    inbox_errors = []
    monkeypatch.setattr(brokers, "_publish_login_inbox_error", lambda b, t, m: inbox_errors.append((b, t, m)))

    attempts = {"n": 0}

    async def fake_submit(_ctx, _page, _u, _p):
        attempts["n"] += 1
        return "xhr_timeout", "เซิร์ฟเวอร์ Liberator ตอบช้าเกินกำหนด (/va/login timeout 10s)"

    monkeypatch.setattr(brokers, "_submit_liberator_credentials_and_watch", fake_submit)

    async def fast_sleep(_sec):
        return None

    monkeypatch.setattr(brokers.asyncio, "sleep", fast_sleep)

    ctx = MagicMock()
    ctx.cookies = AsyncMock(return_value=[])
    page = MagicMock()
    page.url = "https://webtrade.liberator.co.th/login"
    page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()

    ok = await brokers.liberator_login(ctx, page, timeout_seconds=30, resume_only=False)
    assert ok is False
    assert attempts["n"] == 2  # 1 initial + 1 automatic retry ONLY
    assert len(inbox_errors) == 1
    prog = login_progress.get_login_progress("lib")
    assert prog is not None
    assert prog["phase"] == "error"
    assert prog["attempt"] == 2
    assert "/va/login timeout" in (prog.get("error") or "")


@pytest.mark.asyncio
async def test_liberator_login_never_retries_on_api_error(tmp_path, monkeypatch):
    from app.core.config import settings
    from app.services.broker_cdp import login_progress
    from scripts.broker_cdp import brokers

    monkeypatch.setattr(login_progress, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(settings, "LIBERATOR_USERNAME", "<REDACTED_LIBERATOR_USERNAME>")
    monkeypatch.setattr(settings, "LIBERATOR_PASSWORD", "bad-pw")
    monkeypatch.setattr(brokers, "LIBERATOR_SESSION_FILE", str(tmp_path / "liberator_session.json"))
    monkeypatch.setattr(brokers, "_publish_login_inbox_error", lambda *args: None)

    attempts = {"n": 0}

    async def fake_submit(_ctx, _page, _u, _p):
        attempts["n"] += 1
        return "api_error", "Liberator ปฏิเสธการเข้าสู่ระบบ (HTTP 401): Invalid password"

    monkeypatch.setattr(brokers, "_submit_liberator_credentials_and_watch", fake_submit)

    ctx = MagicMock()
    ctx.cookies = AsyncMock(return_value=[])
    page = MagicMock()
    page.url = "https://webtrade.liberator.co.th/login"
    page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()

    ok = await brokers.liberator_login(ctx, page, timeout_seconds=30, resume_only=False)
    assert ok is False
    assert attempts["n"] == 1  # No retry on explicit credential/API error
    prog = login_progress.get_login_progress("lib")
    assert prog["phase"] == "error"
    assert "401" in (prog.get("error") or "")


@pytest.mark.asyncio
async def test_trigger_cdp_login_skips_when_daemon_login_active(tmp_path, monkeypatch):
    from app.services import otp_relay_registry
    from app.services.broker_cdp import daemon_launcher, login_progress
    from scripts.broker_cdp import brokers as brokers_mod

    monkeypatch.setattr(login_progress, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(otp_relay_registry, "_get_otp_state_file", lambda: tmp_path / "otp_state.json")
    monkeypatch.setattr(otp_relay_registry, "_IN_MEMORY_PENDING_REQUEST", None)
    monkeypatch.setattr(otp_relay_registry, "_IN_MEMORY_LATEST_OTP", None)

    login_progress.set_login_progress(
        "liberator",
        "submitting_credentials",
        "กำลังส่งรหัส Liberator...",
        pid=os.getpid(),
    )
    assert login_progress.is_login_active("liberator") is True

    called = {"login": 0}

    async def _fail_if_called(*_a, **_kw):
        called["login"] += 1
        return False

    monkeypatch.setitem(brokers_mod.BROKERS["liberator"], "login", _fail_if_called)
    monkeypatch.setattr(daemon_launcher, "is_port_open", lambda _p: True)
    monkeypatch.setattr(
        daemon_launcher,
        "daemon_state",
        AsyncMock(return_value={"state": "LOGIN_PAGE", "port": 9334, "pid": os.getpid(), "page_url": "https://webtrade.liberator.co.th/login"}),
    )
    monkeypatch.setattr(daemon_launcher, "_ACTIVE_LOGIN_TASKS", {})
    monkeypatch.setattr("scripts.broker_cdp.client.connect_cdp", AsyncMock())
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    res = await daemon_launcher.trigger_cdp_login(
        "liberator",
        wait_for_prompt_s=0.1,
    )
    assert res["login_in_progress"] is True
    assert res["login_progress"]["phase"] == "submitting_credentials"
    assert called["login"] == 0


@pytest.mark.asyncio
async def test_submit_otp_keeps_pending_in_verifying_mode_and_sse_watcher_emits(tmp_path, monkeypatch):
    from app.api.v1.endpoints import miniapp as miniapp_ep
    from app.core.config import settings
    from app.services import otp_relay_registry
    from app.services.broker_cdp import login_progress
    from app.services.miniapp_inbox import miniapp_inbox

    monkeypatch.setattr(login_progress, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(otp_relay_registry, "_get_otp_state_file", lambda: tmp_path / "otp_state.json")
    monkeypatch.setattr(settings, "MINIAPP_INBOX_STATE_PATH", str(tmp_path / "miniapp_inbox.json"))
    monkeypatch.setattr(otp_relay_registry, "_IN_MEMORY_PENDING_REQUEST", None)
    monkeypatch.setattr(otp_relay_registry, "_IN_MEMORY_LATEST_OTP", None)

    await otp_relay_registry.record_request(broker="LIBERATOR", ref="KCNX")
    p_before = await miniapp_ep._pending_otp_payload()
    assert p_before is not None
    assert p_before["submitted"] is False
    assert p_before["ref"] == "KCNX"

    watcher_state = {
        "otp_file_sig": miniapp_ep._path_stat_sig(otp_relay_registry._get_otp_state_file()),
        "pending_sig": miniapp_ep._pending_sig(p_before),
        "prog_file_sigs": {
            "kss": miniapp_ep._path_stat_sig(login_progress.state_file_for("kss")),
            "lib": miniapp_ep._path_stat_sig(login_progress.state_file_for("liberator")),
        },
        "prog_sigs": {"kss": None, "lib": None},
        "inbox_file_sig": miniapp_ep._path_stat_sig(miniapp_inbox._state_path()),
        "last_inbox_id": None,
    }

    # Submitting OTP must keep pending_otp alive with submitted=True
    submit_res = await miniapp_ep.miniapp_submit_otp(
        miniapp_ep.SubmitOtpPayload(otp="654321", broker="lib"),
        _user={"sub": "U_OP"},
    )
    assert submit_res["submitted"] is True
    assert submit_res["pending_otp"]["submitted"] is True

    events = await miniapp_ep._collect_cross_process_sse_events(watcher_state)
    ev_names = [e[0] for e in events]
    assert "otp_submitted" in ev_names

    # External session_daemon progress write triggers login_progress SSE event
    login_progress.set_login_progress("liberator", "waiting_otp", "รอ OTP", ref="KCNX")
    events2 = await miniapp_ep._collect_cross_process_sse_events(watcher_state)
    assert any(name == "login_progress" and data.get("phase") == "waiting_otp" for name, data in events2)

    # Clearing pending triggers otp_resolved SSE event
    otp_relay_registry.clear_pending_request("LIBERATOR")
    events3 = await miniapp_ep._collect_cross_process_sse_events(watcher_state)
    assert any(name == "otp_resolved" and data.get("broker") == "LIBERATOR" for name, data in events3)



@pytest.mark.asyncio
async def test_resolve_pending_request_invalidates_stale_ram_when_disk_cleared(tmp_path, monkeypatch):
    import json
    from app.services import otp_relay_registry

    state_file = tmp_path / "otp_state.json"
    monkeypatch.setattr(otp_relay_registry, "_get_otp_state_file", lambda: state_file)
    otp_relay_registry.clear_otp_state()

    # Gateway :8000 receives request & marks it submitted in its own RAM + disk
    await otp_relay_registry.record_request("LIBERATOR", ref="XY12", ttl_seconds=180)
    otp_relay_registry.mark_pending_submitted("LIBERATOR", submitted=True)
    assert otp_relay_registry._IN_MEMORY_PENDING_REQUEST is not None

    # External session_daemon clears pending_request directly on disk (gateway RAM still stale)
    state_file.write_text(json.dumps({"latest_otp": {"otp": "<REDACTED_KRUNGSRI_PIN>", "seq": 1}}), encoding="utf-8")

    resolved = await otp_relay_registry.resolve_pending_request()
    assert resolved is None
    assert otp_relay_registry._IN_MEMORY_PENDING_REQUEST is None


@pytest.mark.asyncio
async def test_pending_otp_payload_auto_clears_when_login_progress_authenticated(tmp_path, monkeypatch):
    from app.api.v1.endpoints import miniapp as miniapp_ep
    from app.services import otp_relay_registry
    from app.services.broker_cdp import login_progress

    monkeypatch.setattr(login_progress, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(otp_relay_registry, "_get_otp_state_file", lambda: tmp_path / "otp_state.json")
    otp_relay_registry.clear_otp_state()

    await otp_relay_registry.record_request("LIBERATOR", ref="QWER", ttl_seconds=180)
    otp_relay_registry.mark_pending_submitted("LIBERATOR", submitted=True)

    # Simulate daemon finishing login right after OTP submission
    login_progress.set_login_progress("lib", "authenticated", "เข้าสู่ระบบ Liberator แล้ว")

    payload = await miniapp_ep._pending_otp_payload()
    assert payload is None
    assert await otp_relay_registry.resolve_pending_request() is None


@pytest.mark.asyncio
async def test_opening_tabs_phase_blocks_ensure_kss_tabs_complete(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock, patch
    from app.services.broker_cdp import daemon_launcher, login_progress

    monkeypatch.setattr(login_progress, "_data_dir", lambda: tmp_path)
    login_progress.set_login_progress(
        "kss", "opening_tabs", "กำลังเปิดแท็บ...", pid=os.getpid()
    )
    assert login_progress.is_login_active("kss") is True

    connect_mock = AsyncMock()
    probe = {"state": "AUTHENTICATED", "target_found": True}
    with (
        patch("scripts.broker_cdp.client.connect_cdp", new=connect_mock),
        patch("app.services.broker_cdp.daemon_launcher.cdp_session_status", new=AsyncMock(return_value=probe)),
    ):
        res = await daemon_launcher.ensure_kss_tabs_complete()

    assert connect_mock.await_count == 0
    assert res["sbl_state"] == "AUTHENTICATED"


@pytest.mark.asyncio
async def test_kss_open_tabs_prunes_duplicate_sbl_and_reuses_existing(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock, patch
    from scripts.broker_cdp import brokers

    class FakePage:
        def __init__(self, url: str):
            self.url = url
            self.closed = False

        async def close(self):
            self.closed = True

        async def wait_for_timeout(self, _ms):
            return None

    class FakeCtx:
        def __init__(self, pages):
            self.pages = pages

        async def new_page(self):
            p = FakePage("about:blank")
            self.pages.append(p)
            return p

    streaming = FakePage("https://wme2.settrade.com/realtime/streaming7/StreamingPage.jsp")
    sbl_1 = FakePage("https://www.krungsrisecurities.com/sblweb/MainSBL.aspx")
    sbl_2 = FakePage("https://www.krungsrisecurities.com/sblweb/MainSBL.aspx")
    disclaimer = FakePage("https://www.krungsrisecurities.com/th/trade/disclaimer-sbl.aspx")
    ctx = FakeCtx([streaming, sbl_1, sbl_2, disclaimer])

    with patch("app.services.broker_cdp.sync_service.ensure_kss_active_account", new=AsyncMock(return_value={"active": "<REDACTED_KSS_ACCT>"})):
        await brokers.kss_open_tabs(ctx)

    assert len([p for p in ctx.pages if not p.closed and "MainSBL.aspx" in p.url]) == 1
    assert sbl_2.closed is True
    assert disclaimer.closed is True


@pytest.mark.asyncio
async def test_wait_for_otp_ref_polls_until_spa_renders_ref(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from scripts.broker_cdp import brokers

    results = iter([(True, None, "#otp"), (True, None, "#otp"), (True, "ABCD", "#otp")])
    monkeypatch.setattr(brokers, "_detect_otp_prompt", AsyncMock(side_effect=lambda _p: next(results)))

    page = MagicMock()
    page.wait_for_timeout = AsyncMock()

    ref = await brokers._wait_for_otp_ref(page, None, attempts=4, delay_ms=10)
    assert ref == "ABCD"


@pytest.mark.asyncio
async def test_ensure_kss_active_account_retries_when_dropdown_slow(monkeypatch):
    from app.services.broker_cdp import order_executor, sync_service

    class _SlowEl:
        def __init__(self, on_click=None):
            self._on_click = on_click

        async def is_visible(self):
            return True

        async def click(self, *a, **kw):
            if self._on_click:
                self._on_click()

    class _SlowDropdownPage:
        def __init__(self):
            self.account = "<REDACTED_KSS_ACCT>"
            self.pick_attempts = 0

        async def evaluate(self, js, *args, **kwargs):
            if js is order_executor._ACTIVE_ACCOUNT_JS:
                return self.account
            if js is sync_service._HEADER_AC_TYPE_JS:
                return "Credit Balance" if self.account == "<REDACTED_KSS_ACCT>" else "Cash Balance"
            return ""

        async def query_selector(self, sel):
            if sel in order_executor.KSS_ACCOUNT_TRIGGER:
                return _SlowEl()
            if "<REDACTED_KSS_ACCT>" in sel:
                self.pick_attempts += 1
                if self.pick_attempts <= 4:
                    return None
                return _SlowEl(lambda: setattr(self, "account", "<REDACTED_KSS_ACCT>"))
            return None

        async def wait_for_timeout(self, _ms):
            return None

    page = _SlowDropdownPage()
    info = await sync_service.ensure_kss_active_account(page, target_account="<REDACTED_KSS_ACCT>")
    assert info["switched"] is True
    assert info["active"] == "<REDACTED_KSS_ACCT>"
    assert info["error"] is None


@pytest.mark.asyncio
async def test_kss_switch_account_uses_scoped_trigger_not_more_menu():
    from app.services.broker_cdp import order_executor

    assert "div.position-relative.cursor-pointer" not in order_executor.KSS_ACCOUNT_TRIGGER
    assert "span.outline-none" not in order_executor.KSS_ACCOUNT_TRIGGER

    clicked_selectors = []

    class _El:
        def __init__(self, sel: str, on_click=None):
            self._sel = sel
            self._on_click = on_click

        async def is_visible(self):
            return True

        async def click(self, force=False):
            clicked_selectors.append(self._sel)
            if self._on_click:
                self._on_click()

    class _MoreCollisionPage:
        def __init__(self):
            self.account = "<REDACTED_KSS_ACCT>"
            self.dropdown_open = False

        async def evaluate(self, js, *args, **kwargs):
            if js is order_executor._ACTIVE_ACCOUNT_JS:
                return f"{self.account} (Equity)"
            return ""

        async def query_selector(self, sel):
            # Simulate Streaming7 where bare div.position-relative.cursor-pointer hits 'More'
            if sel in ("div.position-relative.cursor-pointer", "span.outline-none"):
                return _El("More")
            if "<REDACTED_KSS_ACCT>" in sel:
                return _El(sel, lambda: setattr(self, "dropdown_open", True))
            if self.dropdown_open and "<REDACTED_KSS_ACCT>" in sel:
                return _El(sel, lambda: setattr(self, "account", "<REDACTED_KSS_ACCT>"))
            return None

        async def wait_for_timeout(self, _ms):
            return None

    ok = await order_executor._kss_switch_account(_MoreCollisionPage(), "<REDACTED_KSS_ACCT>")
    assert ok is True
    assert "More" not in clicked_selectors
    assert any("<REDACTED_KSS_ACCT>" in s for s in clicked_selectors)
