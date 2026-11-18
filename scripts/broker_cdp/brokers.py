# -*- coding: utf-8 -*-
"""Per-broker config: CDP port, Chrome profile, login hook, target-tab hook.

The two session daemons shared almost everything except (a) the login flow and
(b) which target tabs to open. This module is the single place those differ.
"""
import asyncio
import inspect
import json
import logging
import os
import re
import time
from typing import Callable, Optional, Tuple
from urllib.parse import urlparse

from playwright.async_api import BrowserContext, Page

from app.core.config import settings
from app.browser.auth_driver import (
    OTP_SUBMIT_SELECTORS,
    click_otp_submit,
    extract_otp_ref_code,
)
from app.brokers.liberator.session_health import session_expired as liberator_session_expired
from app.services.broker_cdp.login_progress import set_login_progress
from app.services.otp_relay_registry import (
    clear_pending_request,
    get_latest_otp_seq,
    mark_pending_submitted,
)
from scripts.broker_cdp.otp_relay import (
    check_line_otp_once,
    latest_otp_db_row_id,
    latest_otp_row_id,
    request_line_otp,
)

logger = logging.getLogger("broker_cdp_brokers")

# ────────────────────────────────────────────────────────────────────────────
# Path Contract & Authenticated URL Specs
# ────────────────────────────────────────────────────────────────────────────

AUTH_PATHS = {
    # Settrade rotates its streaming-edge host prefix (wma1/wme1/wme2.settrade.com).
    # The bare-domain entry matches any node: is_authenticated_path() collapses the
    # `wm[a-z]\d+` prefix to `settrade.com` before prefix-matching.
    ("kss", "streaming"): ["settrade.com/realtime/streaming7/StreamingPage"],
    ("kss", "sbl"):       ["krungsrisecurities.com/sblweb/MainSBL.aspx"],
    ("kss", "member"):    ["krungsrisecurities.com/th/member/"],
    ("lib", "portfolio"): ["webtrade.liberator.co.th/Market",
                           "webtrade.liberator.co.th/Portfolio",
                           "webtrade.liberator.co.th/Trade"],
}

LOGIN_MARKERS = [
    "/login",
    "login.aspx",
    "/main/default.aspx",
    "/signin",
    "/otp",
    "/twofa",
    "/member/login",
    "/auth/",
    "unauthorizedaccess",
    "sessionexpired",
]


def is_authenticated_path(broker: str, purpose: str, url: str) -> bool:
    """Check whether the given URL matches the authenticated path contract for broker/purpose.

    Enforces:
    1. Rejects if any LOGIN_MARKERS match (path or full URL).
    2. Rejects bare root paths (/ or empty).
    3. Matches host and path prefix against AUTH_PATHS for (broker, purpose).
    """
    if not url:
        return False

    url_clean = url.strip().lower()
    parsed = urlparse(url if "://" in url else ("//" + url))
    path_clean = (parsed.path or "").lower()

    # Guard LOGIN_MARKERS first (case-insensitive)
    for marker in LOGIN_MARKERS:
        m = marker.lower()
        if m in path_clean or m in url_clean:
            return False

    if path_clean in ("", "/"):
        return False

    b_key = "lib" if (broker or "").lower() in ("lib", "liberator") else (broker or "").lower()
    p_key = (purpose or "").lower()

    candidates = AUTH_PATHS.get((b_key, p_key))
    if not candidates:
        candidates = AUTH_PATHS.get(((broker or "").lower(), p_key))
    if not candidates:
        return False

    netloc = (parsed.netloc or "").lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    # Settrade streaming rotates its edge host (wma1/wme1/wme2.settrade.com) — collapse
    # the prefix so the canonical bare-domain AUTH_PATHS entry matches any node.
    if netloc.endswith(".settrade.com"):
        bare_host = netloc[: -len(".settrade.com")]
        if re.fullmatch(r"wm[a-z]\d+", bare_host):
            netloc = "settrade.com"
    host_and_path = f"{netloc}{parsed.path}".lower()

    for auth_path in candidates:
        target = auth_path.lower()
        if target.startswith("www."):
            target = target[4:]
        if host_and_path.startswith(target):
            if target.endswith("/"):
                return True
            rem = host_and_path[len(target):]
            if rem == "" or rem[0] in ("/", "?", "#", "."):
                return True

    return False


# ────────────────────────────────────────────────────────────────────────────
# KSS — Settrade Streaming via Krungsri SSO (port 9333)
# ────────────────────────────────────────────────────────────────────────────

SSO_STREAMING_URL = "https://www.krungsrisecurities.com/Put_SSO_Settrade.aspx?Page=streaming"
SSO_SBL_START_URL = "https://www.krungsrisecurities.com/th/SBL/start.aspx"
SSO_SBL_DISCLAIMER_URL = "https://www.krungsrisecurities.com/th/disclaimer-sbl/Default.aspx?start=sbl"


# ────────────────────────────────────────────────────────────────────────────
# OTP prompt detection / fill — shared by every CDP login flow
# (selectors mirrored from app/browser/auth_driver.py::_detect_otp_prompt)
# ────────────────────────────────────────────────────────────────────────────

OTP_INPUT_SELECTORS = [
    "input[placeholder*='OTP' i]",
    "#otp",
    "input#otp",
    "input[name*='otp' i]",
    "input[id*='otp' i]",
    "#login_otp",
    "input#login_otp",
    "input[name='otp']",
    "input[type='text'][maxlength='6']",
    "input[type='tel'][maxlength='6']",
    "input#txtOTP",
    "input[name='txtOTP']",
]


_OTP_REF_STOPWORDS = frozenset({"CODE", "NO", "NUMBER", "SMS", "OTP", "IS", "ID"})

_OTP_REJECTED_KEYWORDS = (
    "otp ไม่ถูกต้อง",
    "รหัส otp ไม่ถูกต้อง",
    "otp ผิด",
    "รหัสไม่ถูกต้อง",
    "invalid otp",
    "incorrect otp",
    "otp has expired",
    "otp expired",
    "otp หมดอายุ",
    "รหัสอ้างอิงไม่ถูกต้อง",
)

_KSS_LOGIN_ERROR_KEYWORDS = (
    "username หรือ password ไม่ถูกต้อง",
    "ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง",
    "รหัสผ่านไม่ถูกต้อง",
    "account is locked",
    "บัญชีของท่านถูกระงับ",
)


def _publish_login_inbox_error(broker_slug: str, title: str, body: str) -> None:
    """Best-effort publish login failure into MiniApp Inbox across processes."""
    try:
        from app.services.miniapp_inbox import miniapp_inbox

        miniapp_inbox.add_sync(
            category="broker",
            severity="error",
            title=title,
            body=body,
            broker=broker_slug,
            action_tab="brokers",
            line_pushed=False,
        )
    except Exception as exc:
        logger.debug("Inbox publish skipped: %s", exc)


async def _detect_otp_prompt(page: Page):
    """Return (is_otp, ref_code, otp_sel) — same detection logic as auth_driver."""
    otp_sel = None
    for sel in OTP_INPUT_SELECTORS:
        el = await page.query_selector(sel)
        if el and await el.is_visible():
            otp_sel = sel
            break
    body = await page.inner_text("body") if page else ""
    has_otp_text = any(
        k in body for k in ["รหัสอ้างอิง", "Ref", "OTP", "One Time Password", "SMS OTP"]
    )
    if not otp_sel and not has_otp_text:
        return False, None, None
    ref_code = extract_otp_ref_code(body)
    return True, ref_code, otp_sel or "input[maxlength='6']"


async def _fill_otp_input(page: Page, otp_sel: str, otp_val: str) -> bool:
    """Fill the OTP input + best-effort submit. Never raises."""
    try:
        el = await page.query_selector(otp_sel)
        if not el or not await el.is_visible():
            return False
        await el.fill(otp_val)
        await asyncio.sleep(0.3)
        await click_otp_submit(page)
        return True
    except Exception as e:
        logger.warning(f"[CDP OTP] fill/submit failed: {e}")
        return False


async def _verify_otp_submission(
    broker_key: str,
    broker_tag: str,
    page: Page,
    is_auth_fn: Callable[[], object],
    ref_code: Optional[str] = None,
    wait_seconds: float = 10.0,
) -> str:
    """Wait briefly after submitting OTP so MiniApp reflects real verification or rejection."""
    set_login_progress(
        broker_key,
        "verifying_otp",
        "กำลังส่งรหัส OTP ยืนยันกับโบรกเกอร์...",
        ref=ref_code,
    )
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        res = is_auth_fn()
        if inspect.isawaitable(res):
            res = await res
        if res:
            return "authenticated"
        try:
            body_low = ((await page.inner_text("body")) or "").lower()
            if any(kw in body_low for kw in _OTP_REJECTED_KEYWORDS):
                mark_pending_submitted(broker_tag, submitted=False)
                set_login_progress(
                    broker_key,
                    "otp_rejected",
                    "รหัส OTP ไม่ถูกต้องหรือหมดอายุ กรุณากรอกรหัสใหม่",
                    error="รหัส OTP ไม่ถูกต้องหรือหมดอายุ กรุณากรอกใหม่อีกครั้ง",
                    ref=ref_code,
                )
                return "rejected"
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return "pending"


def _page_url_str(page: Page) -> str:
    val = getattr(page, "url", None)
    return val if isinstance(val, str) else ""


async def _wait_for_otp_ref(
    page: Page,
    initial_ref: Optional[str],
    *,
    attempts: int = 6,
    delay_ms: int = 400,
) -> Optional[str]:
    """Wait briefly for SPA DOM (e.g. Liberator /otp) to render ref_code before firing LINE/MiniApp prompt."""
    if initial_ref:
        return initial_ref
    for _ in range(max(0, attempts)):
        try:
            await page.wait_for_timeout(delay_ms)
        except Exception:
            await asyncio.sleep(delay_ms / 1000.0)
        try:
            still_otp, ref_code, _ = await _detect_otp_prompt(page)
        except Exception:
            return initial_ref
        if ref_code:
            return ref_code
        if not still_otp:
            return None
    return initial_ref


def _set_post_login_phase(
    broker_slug: str,
    phase: str,
    authenticated_msg: str,
    opening_msg: str,
) -> None:
    target_phase = phase if phase in ("opening_tabs", "authenticated") else "authenticated"
    msg = opening_msg if target_phase == "opening_tabs" else authenticated_msg
    set_login_progress(broker_slug, target_phase, msg)


async def kss_login(
    ctx: BrowserContext,
    page: Page,
    timeout_seconds: int = 300,
    resume_only: bool = False,
    post_login_phase: str = "authenticated",
) -> bool:
    """Handle initial Krungsri SSO login and wait for OTP completion."""
    u, p = settings.KRUNGSRI_USERNAME, settings.KRUNGSRI_PASSWORD
    login_url = settings.KRUNGSRI_LOGIN_URL or "https://www.krungsrisecurities.com"

    set_login_progress("kss", "checking_session", "กำลังตรวจเซสชัน Krungsri SSO...")
    logger.info(f"Navigating to {login_url}...")
    await page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(3000)

    def _kss_body_is_in(body_txt: str, cur_url: str) -> bool:
        u_low = cur_url.lower()
        return (
            any(k in body_txt for k in ["ยินดีต้อนรับ", "creditbalance"])
            or ("member" in u_low and not any(m in u_low for m in LOGIN_MARKERS))
        )

    body = await page.inner_text("body")
    if _kss_body_is_in(body, _page_url_str(page)):
        try:
            clear_pending_request("KRUNGSRI")
        except Exception:
            pass
        _set_post_login_phase(
            "kss",
            post_login_phase,
            "เข้าสู่ระบบ Krungsri SSO แล้ว",
            "เข้าสู่ระบบ Krungsri สำเร็จ — กำลังเปิดแท็บเทรดและสลับบัญชี...",
        )
        logger.info("Already logged in to Krungsri SSO!")
        return True

    if resume_only:
        logger.info(
            "KSS is in --resume-only mode: parking on login page without submitting "
            "credentials or firing SMS OTP (Invariant 9)."
        )
        set_login_progress("kss", "idle", "จอดรอที่หน้า Login (--resume-only)")
        return False

    set_login_progress("kss", "submitting_credentials", "กำลังกรอกชื่อผู้ใช้และรหัสผ่าน Krungsri...")
    logger.info("Filling Krungsri credentials...")
    filled_user = False
    for sel in ["#login_username", "input[name*='user' i]", "#txtUsername"]:
        el = await page.query_selector(sel)
        if el and await el.is_visible():
            await el.fill(u)
            filled_user = True
            break

    for sel in ["#login_password", "input[name*='pass' i]", "#txtPassword"]:
        el = await page.query_selector(sel)
        if el and await el.is_visible():
            await el.fill(p)
            break

    if filled_user:
        for submit_sel in ["#login", "button[type='submit']", "input[type='submit']"]:
            btn = await page.query_selector(submit_sel)
            if btn and await btn.is_visible():
                await btn.click()
                logger.info("Clicked login button. Waiting for OTP (SMS / LINE Relay / RDP)...")
                break

    async def _kss_is_auth() -> bool:
        try:
            b_txt = await page.inner_text("body")
            return _kss_body_is_in(b_txt, _page_url_str(page))
        except Exception:
            return False

    waited = 0
    otp_requested = False
    otp_since_seq = 0
    otp_since_id = 0
    last_ref: Optional[str] = None
    while waited < timeout_seconds:
        body = await page.inner_text("body")
        if _kss_body_is_in(body, _page_url_str(page)):
            logger.info(f"Login successful! Landed on: {page.url}")
            try:
                clear_pending_request("KRUNGSRI")
            except Exception:
                pass
            _set_post_login_phase(
                "kss",
                post_login_phase,
                "เข้าสู่ระบบ Krungsri สำเร็จ!",
                "เข้าสู่ระบบ Krungsri สำเร็จ — กำลังเปิดแท็บเทรดและสลับบัญชี...",
            )
            return True

        body_low = (body or "").lower()
        if any(err_kw in body_low for err_kw in _KSS_LOGIN_ERROR_KEYWORDS):
            msg = "ชื่อผู้ใช้หรือรหัสผ่าน Krungsri ไม่ถูกต้อง หรือบัญชีถูกล็อก"
            set_login_progress("kss", "error", msg, error=msg)
            _publish_login_inbox_error("kss", "❌ Login Krungsri ไม่สำเร็จ", msg)
            return False

        # --- LINE OTP relay (แบบเดิม, non-blocking) ---
        is_otp, ref_code, otp_sel = await _detect_otp_prompt(page)
        if is_otp and not otp_requested:
            ref_code = await _wait_for_otp_ref(page, ref_code, attempts=3, delay_ms=400)
            otp_requested = True
            last_ref = ref_code
            otp_since_seq = get_latest_otp_seq()
            otp_since_id = await latest_otp_db_row_id()
            logger.info(
                f"OTP prompt detected (ref={ref_code}, seq_watermark={otp_since_seq}, "
                f"db_watermark={otp_since_id}). Requesting via LINE relay..."
            )
            set_login_progress("kss", "waiting_otp", "รอรับรหัส OTP 6 หลัก (Krungsri)", ref=ref_code)
            await request_line_otp("KRUNGSRI", ref=ref_code, timeout_seconds=timeout_seconds)
        elif is_otp:
            if ref_code:
                last_ref = ref_code
            match = await check_line_otp_once(
                "KRUNGSRI",
                since_id=otp_since_id,
                since_seq=otp_since_seq,
                max_age_seconds=300,
            )
            if match and otp_sel:
                if await _fill_otp_input(page, otp_sel, match.otp):
                    # Never replay a code: the broker counts every wrong
                    # submission as a failed 2FA attempt.
                    otp_since_seq = get_latest_otp_seq()
                    otp_since_id = max(otp_since_id, match.row_id, await latest_otp_db_row_id())
                    verify_status = await _verify_otp_submission(
                        "kss", "KRUNGSRI", page, _kss_is_auth, ref_code=last_ref
                    )
                    if verify_status == "authenticated":
                        try:
                            clear_pending_request("KRUNGSRI")
                        except Exception:
                            pass
                        _set_post_login_phase(
                            "kss",
                            post_login_phase,
                            "เข้าสู่ระบบ Krungsri สำเร็จ!",
                            "เข้าสู่ระบบ Krungsri สำเร็จ — กำลังเปิดแท็บเทรดและสลับบัญชี...",
                        )
                        return True

        if waited % 15 == 0:
            if otp_requested:
                set_login_progress("kss", "waiting_otp", "รอรับรหัส OTP 6 หลัก (Krungsri)", ref=last_ref)
            logger.info(f"Waiting for OTP confirmation... ({waited}s / {timeout_seconds}s) [URL: {page.url}]")
        await page.wait_for_timeout(3000)
        waited += 3

    msg = "หมดเวลารอ OTP / Login Krungsri (300 วินาที)"
    set_login_progress("kss", "error", msg, error=msg, ref=last_ref)
    _publish_login_inbox_error("kss", "⏱️ หมดเวลา Login Krungsri", msg)
    logger.warning("Timed out waiting for login/OTP completion.")
    return False


def _is_kss_sbl_url(url: str) -> bool:
    u_low = (url or "").lower()
    return "sblweb" in u_low or "mainsbl" in u_low


def _is_kss_sbl_pending_url(url: str) -> bool:
    u_low = (url or "").lower()
    return "disclaimer-sbl" in u_low or ("sbl" in u_low and "start.aspx" in u_low)


async def kss_prune_duplicate_sbl_tabs(ctx: BrowserContext) -> int:
    """Keep a single authenticated SBL Real Time tab and close duplicate/disclaimer SBL tabs."""
    pages = list(getattr(ctx, "pages", []) or [])
    auth_sbl_pages = [
        p for p in pages
        if is_authenticated_path("kss", "sbl", _page_url_str(p)) or _is_kss_sbl_url(_page_url_str(p))
    ]
    if not auth_sbl_pages:
        return 0

    keeper = next(
        (p for p in auth_sbl_pages if "mainsbl.aspx" in _page_url_str(p).lower()),
        auth_sbl_pages[0],
    )
    closed = 0
    for page in pages:
        if page is keeper:
            continue
        url = _page_url_str(page)
        if page in auth_sbl_pages or _is_kss_sbl_pending_url(url):
            if await _safe_close_page(page):
                closed += 1
    if closed:
        logger.info(f"Pruned {closed} duplicate/stale KSS SBL tab(s).")
    return closed


async def _kss_open_tabs_unlocked(ctx: BrowserContext):
    """Ensure both Streaming and SBL Real Time tabs are opened from the authenticated session."""
    has_streaming = any(
        is_authenticated_path("kss", "streaming", _page_url_str(p))
        for p in ctx.pages
    )
    if not has_streaming:
        logger.info("Opening Settrade Streaming tab via direct SSO URL...")
        page_streaming = await ctx.new_page()
        try:
            await page_streaming.goto(SSO_STREAMING_URL, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            logger.warning(f"Streaming SSO navigation error: {e}")
        for _ in range(15):
            await asyncio.sleep(2)
            if any(is_authenticated_path("kss", "streaming", _page_url_str(p)) for p in ctx.pages):
                logger.info("Streaming tab opened successfully.")
                break
        # Close stale unauthenticated settrade tabs (e.g. UnauthorizedAccess.html) so
        # resolve_target_tab resolves the fresh StreamingPage tab.
        if any(is_authenticated_path("kss", "streaming", _page_url_str(p)) for p in ctx.pages):
            for p in list(ctx.pages):
                u_low = _page_url_str(p).lower()
                if "settrade" in u_low and not is_authenticated_path("kss", "streaming", _page_url_str(p)):
                    if len(ctx.pages) > 1 and hasattr(p, "close"):
                        try:
                            await p.close()
                        except Exception:
                            pass
    else:
        logger.info("Streaming tab already open.")

    # t_496c6213: after login the console opens on the CASH account (<REDACTED_KSS_ACCT>); select the
    # configured working account (CREDIT <REDACTED_KSS_ACCT>) so /port, /orders and the order ticket
    # all see the same book, and it survives a re-login.
    for p in ctx.pages:
        path = urlparse(_page_url_str(p)).path.lower()
        if "streamingpage" in path:
            try:
                from app.services.broker_cdp.sync_service import ensure_kss_active_account
                info = await ensure_kss_active_account(p)
                if info.get("error"):
                    logger.warning(f"Post-login account selection: {info['error']}")
                else:
                    logger.info(f"Post-login active account: {info.get('active')}")
            except Exception as e:  # noqa: BLE001 — never fail login over an account select
                logger.warning(f"Post-login account selection skipped: {e}")
            break

    has_sbl = any(
        is_authenticated_path("kss", "sbl", _page_url_str(p)) or _is_kss_sbl_url(_page_url_str(p))
        for p in ctx.pages
    )
    if not has_sbl:
        pending_sbl = next(
            (p for p in ctx.pages if _is_kss_sbl_pending_url(_page_url_str(p))),
            None,
        )
        if pending_sbl is not None:
            logger.info("Reusing in-flight SBL tab instead of opening a second one...")
            page_sbl = pending_sbl
        else:
            logger.info("Opening SBL Real Time tab via start endpoint...")
            page_sbl = await ctx.new_page()
            try:
                await page_sbl.goto(SSO_SBL_START_URL, wait_until="domcontentloaded", timeout=45000)
                await page_sbl.wait_for_timeout(3000)
            except Exception as e:
                logger.warning(f"SBL SSO navigation error: {e}")

        if "disclaimer-sbl" in _page_url_str(page_sbl).lower():
            logger.info("Landed on SBL Disclaimer page. Clicking SBL Real Time button...")
            for sel in [
                "a:has-text('SBL Real Time')",
                "a[href*='start.aspx']",
                "img[src*='sbl' i]",
                "#sbls",
            ]:
                try:
                    btn = await page_sbl.query_selector(sel)
                    if btn and await btn.is_visible():
                        await btn.click()
                        logger.info(f"Clicked SBL trigger: {sel}")
                        break
                except Exception as e:
                    logger.debug(f"SBL trigger click notice: {e}")

        for _ in range(15):
            await asyncio.sleep(2)
            sbl_tabs = [
                p for p in ctx.pages
                if is_authenticated_path("kss", "sbl", _page_url_str(p)) or _is_kss_sbl_url(_page_url_str(p))
            ]
            if sbl_tabs:
                logger.info(f"SBL Real Time tab opened: {_page_url_str(sbl_tabs[0])}")
                await kss_prune_duplicate_sbl_tabs(ctx)
                return
        logger.info("SBL tab opened (or disclaimer page active).")
    else:
        logger.info("SBL Real Time tab already open.")
        await kss_prune_duplicate_sbl_tabs(ctx)


async def kss_open_tabs(ctx: BrowserContext):
    """Serialized cross-process wrapper ensuring Streaming + single SBL tab without duplicates."""
    from app.services.broker_cdp.daemon_launcher import _DaemonFileLock

    async with _DaemonFileLock("kss", timeout=25.0, lock_name="kss-tabs"):
        await _kss_open_tabs_unlocked(ctx)


async def kss_try_sso_heal(ctx: BrowserContext) -> bool:
    """Zero-OTP tab healer: verify active Krungsri SSO without submitting credentials, then heal tabs."""
    sso_check_url = "https://www.krungsrisecurities.com/th/main/default.aspx"

    async def _check_page_sso(p: Page) -> bool:
        u_low = _page_url_str(p).lower()
        if "member" in u_low and not any(m in u_low for m in LOGIN_MARKERS):
            return True
        if "krungsrisecurities.com" in u_low and not any(m in u_low for m in LOGIN_MARKERS):
            try:
                body = await p.inner_text("body")
                if any(k in body for k in ["ยินดีต้อนรับ", "creditbalance"]):
                    return True
            except Exception:
                pass
        return False

    for existing in list(ctx.pages):
        if await _check_page_sso(existing):
            await kss_open_tabs(ctx)
            return True

    temp_page = None
    try:
        temp_page = await ctx.new_page()
        await temp_page.goto(sso_check_url, wait_until="domcontentloaded", timeout=30000)
        if hasattr(temp_page, "wait_for_timeout"):
            await temp_page.wait_for_timeout(2000)
        is_in = await _check_page_sso(temp_page)
        if is_in:
            if len(ctx.pages) > 1 and hasattr(temp_page, "close"):
                try:
                    await temp_page.close()
                    temp_page = None
                except Exception:
                    pass
            await kss_open_tabs(ctx)
            if temp_page is not None and len(ctx.pages) > 1 and hasattr(temp_page, "close"):
                try:
                    await temp_page.close()
                except Exception:
                    pass
            return True
    except Exception as e:
        logger.debug(f"kss_try_sso_heal probe notice: {e}")

    if temp_page is not None and len(ctx.pages) > 1 and hasattr(temp_page, "close"):
        try:
            await temp_page.close()
        except Exception:
            pass
    return False


# ────────────────────────────────────────────────────────────────────────────
# Liberator — WebTrade (port 9334)
# ────────────────────────────────────────────────────────────────────────────

LIBERATOR_LOGIN_URL = "https://webtrade.liberator.co.th/login"
LIBERATOR_PORTFOLIO_URL = "https://webtrade.liberator.co.th/Portfolio"
LIBERATOR_TRADE_URL = "https://webtrade.liberator.co.th/Trade"
LIBERATOR_SESSION_FILE = os.path.abspath("data/liberator_session.json")


def _is_authenticated_landing(url: str) -> bool:
    """True when a Liberator URL is an authenticated page (off /login and off /otp).

    Matches the URL PATH, not the full URL: "trade" is a substring of the "webtrade"
    host, so a naive substring match would treat the OTP page (/otp) as authenticated.
    A bare root ("/" or "") is also rejected so an in-flight redirect never wins.
    """
    path = urlparse(url).path.lower()
    if path in ("", "/"):
        return False
    return "login" not in path and "otp" not in path


def has_liberator_auth_cookies(cookies) -> bool:
    """True only when Liberator auth cookies (`api-token` or `ws-token`) are present."""
    if not isinstance(cookies, list):
        return False
    for c in cookies:
        if isinstance(c, dict) and c.get("name") in ("api-token", "ws-token") and c.get("value"):
            return True
    return False


def _save_liberator_cookies(cookies) -> None:
    if not has_liberator_auth_cookies(cookies):
        return
    try:
        os.makedirs(os.path.dirname(LIBERATOR_SESSION_FILE), exist_ok=True)
        with open(LIBERATOR_SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump({"cookies": cookies, "saved_at": time.time()}, f, indent=2)
    except Exception as exc:
        logger.debug("Could not persist Liberator cookies: %s", exc)


async def _confirm_liberator_authenticated(ctx: BrowserContext, page: Page):
    """Require BOTH an authenticated Liberator path AND real `api-token`/`ws-token` cookies."""
    if not _is_authenticated_landing(_page_url_str(page)):
        return False, []
    try:
        cookies = await ctx.cookies()
    except Exception:
        cookies = []
    if not has_liberator_auth_cookies(cookies):
        return False, cookies
    return True, cookies


async def _submit_liberator_credentials_and_watch(
    ctx: BrowserContext,
    page: Page,
    user: str,
    pwd: str,
) -> Tuple[str, Optional[str]]:
    """Fill Liberator credentials, click ยืนยัน, and watch POST /va/login outcome."""
    for user_sel in ["input[type='text']", "input[name*='user' i]", "#username"]:
        el = await page.query_selector(user_sel)
        if el and await el.is_visible():
            await el.fill(user)
            break

    for pass_sel in ["input[type='password']", "input[name*='pass' i]", "#password"]:
        el = await page.query_selector(pass_sel)
        if el and await el.is_visible():
            await el.fill(pwd)
            break

    outcome = {"status": None, "error_text": None}
    done = asyncio.Event()

    async def _on_response(resp) -> None:
        try:
            url = (resp.url or "").lower()
            req = getattr(resp, "request", None)
            method = (getattr(req, "method", "") or "").upper()
            if "/va/login" not in url or method != "POST":
                return
            if resp.status >= 400:
                body_msg = ""
                try:
                    raw_txt = await resp.text()
                    body_msg = raw_txt[:200].strip()
                    try:
                        parsed = json.loads(raw_txt)
                        if isinstance(parsed, dict):
                            body_msg = str(
                                parsed.get("message")
                                or parsed.get("error")
                                or parsed.get("detail")
                                or body_msg
                            )
                    except Exception:
                        pass
                except Exception:
                    pass
                outcome["status"] = "api_error"
                outcome["error_text"] = (
                    f"Liberator ปฏิเสธการเข้าสู่ระบบ (HTTP {resp.status})"
                    + (f": {body_msg}" if body_msg else "")
                )
            else:
                outcome["status"] = "ok"
            done.set()
        except Exception:
            pass

    def _on_request_failed(req) -> None:
        try:
            url = (getattr(req, "url", "") or "").lower()
            method = (getattr(req, "method", "") or "").upper()
            if "/va/login" not in url or method != "POST":
                return
            failure = str(getattr(req, "failure", "") or "")
            low = failure.lower()
            if any(tok in low for tok in ("aborted", "timed_out", "timeout")):
                outcome["status"] = "xhr_timeout"
                outcome["error_text"] = (
                    "เซิร์ฟเวอร์ Liberator ตอบช้าเกินกำหนด (/va/login timeout 10s)"
                )
            else:
                outcome["status"] = "network_error"
                outcome["error_text"] = f"เชื่อมต่อ Liberator ไม่สำเร็จ ({failure or 'network error'})"
            done.set()
        except Exception:
            pass

    can_listen = hasattr(page, "on") and hasattr(page, "remove_listener")
    if can_listen:
        page.on("response", _on_response)
        page.on("requestfailed", _on_request_failed)

    try:
        clicked = False
        for submit_sel in [
            "button:has-text('ยืนยัน')",
            "button:has-text('เข้าสู่ระบบ')",
            "button:has-text('Login')",
            "button[type='submit']",
        ]:
            btn = await page.query_selector(submit_sel)
            if btn and await btn.is_visible():
                await btn.click()
                clicked = True
                logger.info("Clicked Liberator login (%s). Watching /va/login response...", submit_sel)
                break

        if not clicked:
            return "api_error", "ไม่พบปุ่มยืนยัน Login บนหน้า Liberator"

        deadline = time.monotonic() + 12.5
        while time.monotonic() < deadline:
            if outcome["status"] == "xhr_timeout":
                return "xhr_timeout", outcome["error_text"]
            if outcome["status"] in ("api_error", "network_error"):
                return str(outcome["status"]), outcome["error_text"]

            ok_auth, _ = await _confirm_liberator_authenticated(ctx, page)
            if ok_auth:
                return "ok", None

            is_otp, _, _ = await _detect_otp_prompt(page)
            if is_otp:
                return "ok", None

            try:
                err_el = await page.query_selector("[role='alert'], .el-message--error, .text-red-500")
                if err_el and await err_el.is_visible():
                    err_txt = ((await err_el.inner_text()) or "").strip()
                    if err_txt and len(err_txt) >= 3:
                        return "api_error", err_txt
            except Exception:
                pass

            if outcome["status"] == "ok":
                await asyncio.sleep(0.5)
                return "ok", None

            try:
                await asyncio.wait_for(done.wait(), timeout=0.35)
            except asyncio.TimeoutError:
                pass

        return "submitted", None
    finally:
        if can_listen:
            try:
                page.remove_listener("response", _on_response)
            except Exception:
                pass
            try:
                page.remove_listener("requestfailed", _on_request_failed)
            except Exception:
                pass


async def liberator_login(
    ctx: BrowserContext,
    page: Page,
    timeout_seconds: int = 300,
    resume_only: bool = False,
    post_login_phase: str = "authenticated",
) -> bool:
    """Handle initial login or session resume for Liberator."""
    set_login_progress("lib", "checking_session", "กำลังตรวจเซสชัน Liberator...")

    # 1a. Fast path: test persistent Chrome user-data-dir profile
    try:
        await page.goto(LIBERATOR_PORTFOLIO_URL, wait_until="domcontentloaded", timeout=20000)
        await page.wait_for_timeout(3000)
        ok_auth, cookies = await _confirm_liberator_authenticated(ctx, page)
        if ok_auth:
            logger.info(f"Existing Liberator Chrome profile session is VALID! Landed on {page.url}")
            _save_liberator_cookies(cookies)
            try:
                clear_pending_request("LIBERATOR")
            except Exception:
                pass
            _set_post_login_phase(
                "lib",
                post_login_phase,
                "เข้าสู่ระบบ Liberator แล้ว",
                "เข้าสู่ระบบ Liberator แล้ว — กำลังเตรียมแท็บ...",
            )
            return True
    except Exception as e:
        logger.debug(f"Profile resume probe notice: {e}")

    # 1b. Fallback: only test saved cookies if real api-token/ws-token exists and is unexpired
    if os.path.exists(LIBERATOR_SESSION_FILE):
        try:
            with open(LIBERATOR_SESSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            cookies = data.get("cookies", [])
            if has_liberator_auth_cookies(cookies) and not liberator_session_expired(LIBERATOR_SESSION_FILE):
                await ctx.add_cookies(cookies)
                logger.info(f"Loaded {len(cookies)} cookies from {LIBERATOR_SESSION_FILE}. Testing /Portfolio...")
                await page.goto(LIBERATOR_PORTFOLIO_URL, wait_until="domcontentloaded", timeout=20000)
                await page.wait_for_timeout(3000)
                ok_auth, live_cookies = await _confirm_liberator_authenticated(ctx, page)
                if ok_auth:
                    logger.info(f"Existing Liberator session is VALID! Landed on {page.url}")
                    _save_liberator_cookies(live_cookies)
                    try:
                        clear_pending_request("LIBERATOR")
                    except Exception:
                        pass
                    _set_post_login_phase(
                        "lib",
                        post_login_phase,
                        "เข้าสู่ระบบ Liberator แล้ว",
                        "เข้าสู่ระบบ Liberator แล้ว — กำลังเตรียมแท็บ...",
                    )
                    return True
                logger.info("Saved cookies expired. Proceeding to fresh login...")
            else:
                logger.info("Saved Liberator cookie file lacks valid/unexpired auth token; skipping cookie resume.")
        except Exception as e:
            logger.debug(f"Cookie resume notice: {e}")

    # 2. Fresh login
    user = settings.LIBERATOR_USERNAME
    pwd = settings.LIBERATOR_PASSWORD

    logger.info(f"Navigating to {LIBERATOR_LOGIN_URL}...")
    await page.goto(LIBERATOR_LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(3000)

    ok_auth, cookies = await _confirm_liberator_authenticated(ctx, page)
    if ok_auth:
        logger.info(f"Already logged in! Landed on {page.url}")
        _save_liberator_cookies(cookies)
        try:
            clear_pending_request("LIBERATOR")
        except Exception:
            pass
        _set_post_login_phase(
            "lib",
            post_login_phase,
            "เข้าสู่ระบบ Liberator แล้ว",
            "เข้าสู่ระบบ Liberator แล้ว — กำลังเตรียมแท็บ...",
        )
        return True

    if resume_only:
        logger.info(
            "Liberator is in --resume-only mode: parking on login page without submitting "
            "credentials or firing SMS OTP (Invariant 9)."
        )
        set_login_progress("lib", "idle", "จอดรอที่หน้า Login (--resume-only)")
        return False

    # Submit credentials with at most 1 automatic retry ONLY on XHR timeout
    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        if attempt == 1:
            set_login_progress(
                "lib",
                "submitting_credentials",
                "กำลังส่งชื่อผู้ใช้และรหัสผ่าน Liberator...",
                attempt=1,
            )
        else:
            set_login_progress(
                "lib",
                "retrying_submit",
                "เซิร์ฟเวอร์ Liberator ตอบช้า กำลังลองส่งใหม่ (ครั้งที่ 2)...",
                attempt=2,
            )
            await asyncio.sleep(2.0)

        status, err_detail = await _submit_liberator_credentials_and_watch(ctx, page, user, pwd)
        if status == "xhr_timeout" and attempt < max_attempts:
            logger.warning("Liberator /va/login XHR timed out on attempt 1; retrying once...")
            continue
        if status in ("xhr_timeout", "api_error", "network_error"):
            msg = err_detail or "เข้าสู่ระบบ Liberator ไม่สำเร็จ"
            logger.warning("Liberator login fail-fast (%s attempt=%d): %s", status, attempt, msg)
            set_login_progress("lib", "error", msg, error=msg, attempt=attempt)
            _publish_login_inbox_error("lib", "❌ Login Liberator ไม่สำเร็จ", msg)
            return False
        break

    waited = 0
    otp_requested = False
    last_ref: Optional[str] = None
    otp_since_seq = 0
    otp_since_id = 0
    silent_reset_ticks = 0

    async def _lib_is_auth() -> bool:
        ok, _ = await _confirm_liberator_authenticated(ctx, page)
        return ok

    while waited < timeout_seconds:
        ok_auth, cookies = await _confirm_liberator_authenticated(ctx, page)
        if ok_auth:
            logger.info(f"Liberator Login successful! Landed on: {page.url}")
            _save_liberator_cookies(cookies)
            logger.info(f"Persisted {len(cookies)} cookies to {LIBERATOR_SESSION_FILE}")
            try:
                clear_pending_request("LIBERATOR")
            except Exception:
                pass
            _set_post_login_phase(
                "lib",
                post_login_phase,
                "เข้าสู่ระบบ Liberator สำเร็จ",
                "เข้าสู่ระบบ Liberator สำเร็จ — กำลังเตรียมแท็บ...",
            )
            return True

        # --- LINE OTP relay (แบบเดิม, non-blocking) ---
        is_otp, ref_code, otp_sel = await _detect_otp_prompt(page)
        if is_otp and not otp_requested:
            ref_code = await _wait_for_otp_ref(page, ref_code, attempts=6, delay_ms=400)
            ok_auth, cookies = await _confirm_liberator_authenticated(ctx, page)
            if ok_auth:
                continue
            otp_requested = True
            last_ref = ref_code
            otp_since_seq = get_latest_otp_seq()
            otp_since_id = await latest_otp_db_row_id()
            logger.info(
                f"OTP prompt detected (ref={ref_code}, seq_watermark={otp_since_seq}, "
                f"db_watermark={otp_since_id}). Requesting via LINE relay..."
            )
            set_login_progress(
                "lib",
                "waiting_otp",
                f"รอคุณกรอกรหัส OTP Liberator (Ref: {ref_code or '-'})",
                ref=ref_code,
            )
            await request_line_otp("LIBERATOR", ref=ref_code, timeout_seconds=timeout_seconds)
        elif is_otp:
            if ref_code and not last_ref:
                last_ref = ref_code
                set_login_progress(
                    "lib",
                    "waiting_otp",
                    f"รอคุณกรอกรหัส OTP Liberator (Ref: {ref_code})",
                    ref=ref_code,
                )
                try:
                    from app.services.otp_relay_registry import record_request
                    await record_request("LIBERATOR", ref=ref_code, timeout_seconds=timeout_seconds)
                except Exception:
                    pass
            match = await check_line_otp_once(
                "LIBERATOR",
                since_id=otp_since_id,
                since_seq=otp_since_seq,
                max_age_seconds=300,
            )
            if match and otp_sel:
                if await _fill_otp_input(page, otp_sel, match.otp):
                    otp_since_seq = get_latest_otp_seq()
                    otp_since_id = max(otp_since_id, match.row_id, await latest_otp_db_row_id())
                    verdict = await _verify_otp_submission(
                        "lib",
                        "LIBERATOR",
                        page,
                        _lib_is_auth,
                        ref_code=last_ref or ref_code,
                    )
                    if verdict == "authenticated":
                        continue
        elif "/login" in _page_url_str(page).lower():
            # Liberator Nuxt clears #pwd silently on unhandled API/timeout errors
            try:
                pwd_el = await page.query_selector("#pwd, input[type='password']")
                pwd_val = await pwd_el.input_value() if pwd_el else None
                if pwd_val == "":
                    silent_reset_ticks += 1
                    if silent_reset_ticks >= 3:
                        msg = "หน้า Login Liberator รีเซ็ตช่องรหัสผ่านโดยไม่เข้าหน้า OTP (API/เครือข่ายขัดข้อง)"
                        set_login_progress("lib", "error", msg, error=msg)
                        _publish_login_inbox_error("lib", "❌ Login Liberator ไม่สำเร็จ", msg)
                        return False
                else:
                    silent_reset_ticks = 0
            except Exception:
                pass

        if waited % 15 == 0:
            logger.info(f"Waiting for Liberator OTP confirmation... ({waited}s / {timeout_seconds}s) [URL: {page.url}]")
        await page.wait_for_timeout(3000)
        waited += 3

    msg = "หมดเวลารอ OTP Liberator"
    logger.warning("Timed out waiting for Liberator login/OTP.")
    set_login_progress("lib", "error", msg, error=msg)
    _publish_login_inbox_error("lib", "⏰ หมดเวลารอ OTP Liberator", msg)
    return False


def _safe_page_url(page) -> str:
    val = getattr(page, "url", None)
    return val if isinstance(val, str) else ""


async def _safe_close_page(page) -> bool:
    close_fn = getattr(page, "close", None)
    if close_fn is None:
        return False
    try:
        res = close_fn()
        if inspect.isawaitable(res):
            await res
        return True
    except Exception:
        return False


async def liberator_prune_duplicate_tabs(ctx: BrowserContext) -> int:
    """Keep a single authenticated Liberator SPA tab and close duplicate/stale tabs.

    Never closes tabs when zero authenticated Liberator pages exist so active /login
    or /otp user interactions are never interrupted.
    """
    pages = list(getattr(ctx, "pages", []) or [])
    auth_pages = [
        p for p in pages if is_authenticated_path("lib", "portfolio", _safe_page_url(p))
    ]
    if not auth_pages:
        return 0

    keeper = next(
        (p for p in auth_pages if "/portfolio" in _safe_page_url(p).lower()),
        auth_pages[0],
    )
    closed = 0
    for page in pages:
        if page is keeper:
            continue
        url_low = _safe_page_url(page).strip().lower()
        is_blank = url_low in ("", "about:blank", "chrome://newtab/")
        is_lib = "liberator.co.th" in url_low
        if page in auth_pages or is_blank or is_lib:
            if await _safe_close_page(page):
                closed += 1
    return closed


async def liberator_open_tabs(ctx: BrowserContext):
    """Ensure Liberator keeps a single active authenticated SPA tab (/Market, /Portfolio, /Trade).

    Liberator WebTrade serves Portfolio, Market, Quote and the Place-Order ticket within one
    SPA (navigating to /Trade redirects to /Market). Reusing any existing authenticated tab and
    pruning extras prevents duplicate /Market tabs from accumulating across keepalive cycles.
    """
    pages = list(getattr(ctx, "pages", []) or [])
    auth_pages = [
        p for p in pages if is_authenticated_path("lib", "portfolio", _safe_page_url(p))
    ]
    if auth_pages:
        await liberator_prune_duplicate_tabs(ctx)
        return

    logger.info("Opening Liberator Portfolio/Market tab...")
    reusable = [
        p for p in pages
        if _safe_page_url(p).strip().lower() in ("", "about:blank", "chrome://newtab/")
        or "liberator.co.th" in _safe_page_url(p).lower()
    ]
    target_page = reusable[0] if reusable else await ctx.new_page()
    try:
        await target_page.goto(LIBERATOR_PORTFOLIO_URL, wait_until="domcontentloaded", timeout=20000)
    except Exception as e:
        logger.debug(f"Liberator tab open notice: {e}")
    await liberator_prune_duplicate_tabs(ctx)


# ────────────────────────────────────────────────────────────────────────────
# Registry
# ────────────────────────────────────────────────────────────────────────────

BROKERS = {
    "kss": {
        "label": "KSS",
        "port": 9333,
        "profile_dir": os.path.abspath("data/kss_chrome_profile"),
        "log_file": os.path.abspath("data/kss_chrome.log"),
        "login": kss_login,
        "open_tabs": kss_open_tabs,
        "targets": ["streaming", "sbl", "sso", "any"],
        "default_target": "streaming",
        "probe_prefix": "probe",  # data/probes/probe_<target>_<stamp>.json
        "handoff_prefix": "PROBE",  # docs/handoff/PROBE-<TARGET>-<stamp>.md
    },
    "liberator": {
        "label": "Liberator",
        "port": 9334,
        "profile_dir": os.path.abspath("data/liberator_chrome_profile"),
        "log_file": os.path.abspath("data/liberator_chrome.log"),
        "login": liberator_login,
        "open_tabs": liberator_open_tabs,
        "targets": ["portfolio", "trade", "market", "any"],
        "default_target": "portfolio",
        "probe_prefix": "probe_liberator",
        "handoff_prefix": "PROBE-LIBERATOR",
    },
}
