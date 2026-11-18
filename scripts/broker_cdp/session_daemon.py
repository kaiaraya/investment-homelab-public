# -*- coding: utf-8 -*-
"""Shared broker session daemon: launch Chrome (CDP), login once, keep alive.

Run directly (legacy argv form) or via `scripts/broker_probe.py --broker <slug> start-session`.

Usage:
    .venv/bin/python scripts/broker_cdp/session_daemon.py kss 300
    .venv/bin/python scripts/broker_cdp/session_daemon.py liberator 300
"""
import asyncio
import inspect
import json
import logging
import os
import subprocess
import sys
import time
from typing import List, Optional, Tuple

from playwright.async_api import BrowserContext, async_playwright

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _REPO_ROOT)
from app.core.config import settings  # noqa: E402
from app.services.broker_cdp.login_progress import (  # noqa: E402
    get_login_progress,
    set_login_progress,
)
from scripts.broker_cdp.client import cdp_url_for, is_cdp_available  # noqa: E402
from scripts.broker_cdp.brokers import (  # noqa: E402
    BROKERS,
    LIBERATOR_SESSION_FILE,
    is_authenticated_path,
    kss_try_sso_heal,
    liberator_open_tabs,
    liberator_prune_duplicate_tabs,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("broker_cdp_session_daemon")


def _cleanup_stale_singleton_locks(profile_dir: str) -> None:
    """Remove Chromium Singleton* files when the owning local PID is no longer alive."""
    lock_path = os.path.join(profile_dir, "SingletonLock")
    if not os.path.lexists(lock_path):
        return
    try:
        target = os.readlink(lock_path) if os.path.islink(lock_path) else ""
    except OSError:
        return
    host, sep, pid_str = target.rpartition("-")
    if not sep or not pid_str.isdigit():
        return
    if host and host != os.uname().nodename:
        return
    pid = int(pid_str)
    try:
        os.kill(pid, 0)
        return  # Process is still alive; keep lock intact
    except ProcessLookupError:
        pass
    except PermissionError:
        return  # Alive under another uid

    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        p = os.path.join(profile_dir, name)
        try:
            if os.path.lexists(p):
                os.unlink(p)
        except OSError as exc:
            logger.warning(f"Failed to remove stale {p}: {exc}")
    logger.info(f"Removed stale Chromium Singleton locks in {profile_dir} (dead PID {pid})")


def _read_log_tail(log_file: str, max_lines: int = 8) -> str:
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
        return " | ".join(line.strip() for line in lines[-max_lines:] if line.strip())
    except OSError:
        return ""


def launch_chrome_process(broker: str, cdp_port: Optional[int] = None) -> Optional[subprocess.Popen]:
    """Launch Chrome with remote debugging enabled (reuse existing instance if up)."""
    cfg = BROKERS[broker]
    port = cdp_port or cfg["port"]
    profile_dir = cfg["profile_dir"]
    log_file = cfg["log_file"]

    if is_cdp_available(f"http://127.0.0.1:{port}"):
        logger.info(f"Chrome is already running on CDP port {port}. Reusing existing instance.")
        return None

    os.makedirs(profile_dir, exist_ok=True)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    _cleanup_stale_singleton_locks(profile_dir)

    chrome_bin = settings.PLAYWRIGHT_CHROME_EXECUTABLE
    if not chrome_bin or not os.path.exists(chrome_bin):
        for candidate in ["/usr/bin/google-chrome", "/usr/bin/chromium-browser", "/usr/bin/chromium"]:
            if os.path.exists(candidate):
                chrome_bin = candidate
                break

    if not chrome_bin or not os.path.exists(chrome_bin):
        raise FileNotFoundError("Google Chrome or Chromium executable not found on host.")

    cmd = [
        chrome_bin,
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile_dir}",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--no-first-run",
        "--no-default-browser-check",
        "--ozone-platform-hint=auto",
        "--window-size=1440,960",
        "about:blank",
    ]
    logger.info(f"Launching {cfg['label']} Chrome: {' '.join(cmd)}")
    log_fp = open(log_file, "a", encoding="utf-8")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log_fp)

    cdp_url = f"http://127.0.0.1:{port}"
    for _ in range(30):
        if is_cdp_available(cdp_url, timeout=1.0):
            logger.info(f"{cfg['label']} Chrome DevTools Protocol ready at {cdp_url}")
            return proc
        exit_code = proc.poll()
        if exit_code is not None:
            tail = _read_log_tail(log_file)
            raise RuntimeError(
                f"Chrome exited early with code {exit_code} before CDP :{port} became ready"
                + (f": {tail}" if tail else "")
            )
        time.sleep(1)

    raise TimeoutError(f"Chrome CDP did not become responsive on port {port} within 30 seconds.")


def _safe_url(page) -> str:
    val = getattr(page, "url", None)
    return val if isinstance(val, str) else ""


async def run_keepalive_loop(
    ctx: BrowserContext,
    interval_seconds: int = 180,
    label: str = "",
    broker: str = "",
) -> None:
    """Periodic subtle activity + zero-OTP tab self-healing to keep the session alive."""
    logger.info(f"=== {label} Session Keep-Alive active (ping every {interval_seconds}s) ===")
    cycle = 0
    while True:
        await asyncio.sleep(interval_seconds)
        cycle += 1
        active_tabs = len(ctx.pages)
        logger.info(f"[Heartbeat #{cycle}] {label} active tabs: {active_tabs}")
        for idx, page in enumerate(ctx.pages):
            try:
                title = await page.title()
                await page.evaluate("() => ({ time: Date.now(), title: document.title })")
                logger.debug(f"  Tab {idx}: {title[:30]} ({_safe_url(page)[:50]})")
            except Exception as e:
                logger.debug(f"  Tab {idx} ping error: {e}")

        # Self-healing check (Zero-OTP — never submits credentials or triggers SMS)
        try:
            if broker == "kss":
                has_streaming = any(
                    is_authenticated_path("kss", "streaming", _safe_url(p))
                    for p in ctx.pages
                )
                has_sbl = any(
                    is_authenticated_path("kss", "sbl", _safe_url(p))
                    for p in ctx.pages
                )
                has_member = any(
                    is_authenticated_path("kss", "member", _safe_url(p))
                    for p in ctx.pages
                )
                has_broken_tab = any(
                    "unauthorizedaccess" in _safe_url(p).lower()
                    or "disclaimer-sbl" in _safe_url(p).lower()
                    for p in ctx.pages
                )
                if not (has_streaming and has_sbl) and (
                    has_streaming or has_sbl or has_member or has_broken_tab
                ):
                    logger.info(
                        f"[Heartbeat #{cycle}] KSS tab heal check "
                        f"(streaming={has_streaming}, sbl={has_sbl}, broken={has_broken_tab})"
                    )
                    await kss_try_sso_heal(ctx)
            elif broker == "liberator":
                has_portfolio = any(
                    is_authenticated_path("lib", "portfolio", _safe_url(p))
                    for p in ctx.pages
                )
                if has_portfolio:
                    try:
                        cookies = await ctx.cookies()
                        if cookies:
                            os.makedirs(os.path.dirname(LIBERATOR_SESSION_FILE), exist_ok=True)
                            with open(LIBERATOR_SESSION_FILE, "w", encoding="utf-8") as f:
                                json.dump({"cookies": cookies}, f, indent=2)
                    except Exception as e:
                        logger.debug(f"Liberator keepalive cookie persist notice: {e}")
                    # Keep Liberator on a single SPA tab (/Market or /Portfolio); /Trade
                    # redirects to /Market so checking "/trade" leaked +1 tab per cycle.
                    closed = await liberator_prune_duplicate_tabs(ctx)
                    if closed:
                        logger.info(
                            f"[Heartbeat #{cycle}] Pruned {closed} duplicate/stale Liberator tab(s)"
                        )
        except Exception as e:
            logger.warning(f"[Heartbeat #{cycle}] {label} self-heal skipped: {e}")


def _write_daemon_pidfile(broker: str) -> None:
    try:
        pid_dir = os.path.join(_REPO_ROOT, "data")
        os.makedirs(pid_dir, exist_ok=True)
        pid_file = os.path.join(pid_dir, f"daemon-{broker}.pid")
        with open(pid_file, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        logger.warning(f"Could not write pidfile for {broker}: {e}")


async def _invoke_login_hook(
    login_fn,
    ctx: BrowserContext,
    page,
    resume_only: bool,
    *,
    post_login_phase: str = "opening_tabs",
) -> bool:
    try:
        sig = inspect.signature(login_fn)
        params = sig.parameters
        has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
        accepts_resume = "resume_only" in params or has_varkw
        accepts_phase = "post_login_phase" in params or has_varkw
    except (TypeError, ValueError):
        accepts_resume = True
        accepts_phase = False

    kwargs = {}
    if accepts_resume:
        kwargs["resume_only"] = resume_only
    if accepts_phase:
        kwargs["post_login_phase"] = post_login_phase

    if kwargs:
        try:
            return await login_fn(ctx, page, **kwargs)
        except TypeError:
            kwargs.pop("post_login_phase", None)
            if kwargs:
                try:
                    return await login_fn(ctx, page, **kwargs)
                except TypeError:
                    pass
    return await login_fn(ctx, page)


async def start_daemon(
    broker: str,
    keepalive_interval: int = 180,
    resume_only: bool = False,
) -> None:
    """Main daemon runner: launch Chrome, login once (or resume-only), open tabs, keep alive."""
    _write_daemon_pidfile(broker)
    cfg = BROKERS[broker]
    if not resume_only:
        set_login_progress(
            broker,
            "launching_chrome",
            f"กำลังเปิด Chrome {cfg['label']}...",
            pid=os.getpid(),
        )
    launch_chrome_process(broker)

    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(cdp_url_for(broker))
    ctx = browser.contexts[0]
    initial_page = ctx.pages[0] if ctx.pages else await ctx.new_page()

    logged_in = await _invoke_login_hook(
        cfg["login"],
        ctx,
        initial_page,
        resume_only=resume_only,
        post_login_phase="opening_tabs",
    )
    if logged_in:
        set_login_progress(
            broker,
            "opening_tabs",
            f"เข้าสู่ระบบ {cfg['label']} สำเร็จ — กำลังเปิดแท็บเทรด...",
            pid=os.getpid(),
        )
        await cfg["open_tabs"](ctx)
        set_login_progress(
            broker,
            "authenticated",
            f"เข้าสู่ระบบ {cfg['label']} และเปิดแท็บพร้อมเทรดแล้ว",
            pid=os.getpid(),
        )
        # B.3 post-login hook: persist balance + holdings into broker_accounts/positions
        # as soon as the session is up. Best-effort — a sync failure must never kill
        # the keep-alive loop (the next /port sync or pre-trade call will retry).
        try:
            from app.services.broker_cdp.sync_service import sync_portfolio_from_cdp
            result = await sync_portfolio_from_cdp(broker)
            logger.info(
                f"Post-login portfolio sync OK: account={result.get('account_no')} "
                f"line_available={result.get('line_available')} positions={result.get('positions')}"
            )
        except Exception as e:
            logger.warning(f"Post-login portfolio sync skipped: {type(e).__name__}: {e}")

        # B.7 post-login hook: diff the order ledger against `orders` so any trade the
        # human placed manually on the frontend is recorded as source='manual_sync'.
        # Best-effort — an empty ledger or a broken tab must never kill the keep-alive.
        try:
            from app.services.broker_cdp.sync_service import sync_orders_from_cdp
            ord_result = await sync_orders_from_cdp(broker)
            if ord_result.get("new_manual_count"):
                logger.info(
                    f"Post-login manual-order sync: {ord_result['new_manual_count']} "
                    f"new manual order(s) for {ord_result.get('account_no')}"
                )
        except Exception as e:
            logger.warning(f"Post-login order sync skipped: {type(e).__name__}: {e}")
    elif not resume_only:
        prog = get_login_progress(broker)
        if not prog or prog.get("phase") not in ("error", "otp_rejected"):
            err = f"เข้าสู่ระบบ {cfg['label']} ไม่สำเร็จ"
            set_login_progress(broker, "error", err, error=err, pid=os.getpid())

    await run_keepalive_loop(
        ctx,
        interval_seconds=keepalive_interval,
        label=cfg["label"],
        broker=broker,
    )


def parse_daemon_argv(argv: List[str]) -> Tuple[str, int, bool]:
    """Parse CLI argv: `session_daemon.py [broker] [interval] [--resume-only]`."""
    resume_only = False
    positional: List[str] = []
    for arg in argv[1:]:
        if arg == "--resume-only":
            resume_only = True
        elif not arg.startswith("-"):
            positional.append(arg)
    broker = positional[0] if len(positional) > 0 else "kss"
    interval = int(positional[1]) if len(positional) > 1 and positional[1].isdigit() else 180
    return broker, interval, resume_only


if __name__ == "__main__":
    broker, interval, resume_only = parse_daemon_argv(sys.argv)
    if broker not in BROKERS:
        print(f"Unknown broker '{broker}'. Choose from: {', '.join(BROKERS)}")
        sys.exit(1)
    try:
        asyncio.run(start_daemon(broker, keepalive_interval=interval, resume_only=resume_only))
    except KeyboardInterrupt:
        logger.info(f"{BROKERS[broker]['label']} daemon stopped by user.")
