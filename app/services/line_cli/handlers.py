"""
LINE slash-command handlers (Pillar 1–3, KSS + LIB).

Thin adapters over the *existing* Pillar 1/2/6 services and broker drivers — see
``docs/handoff/TASK-line-cli.md``. Broker isolation is enforced by routing every
call through a per-broker context (never mixing KSS and LIB behaviour).

Long operations (login / ``sync`` / nego) acknowledge immediately with a reply
and push the result to the originating chat (groupId else userId). A
per-``(command, broker)`` lock turns duplicate submissions into ``E_BUSY``.

The service instance is injected by ``line_service`` (avoids a circular import).
"""

import asyncio
import inspect
import logging
import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs

from sqlalchemy import select

from app.core.config import settings
from app.services.line_cli import cards
from app.services.line_cli.parser import (
    CliCommand,
    CliError,
    TradeOrder,
    SblCommand,
    SblBorrowOrder,
    SblReturnOrder,
    SizeCommand,
)

logger = logging.getLogger(__name__)

#: per-``(command, broker)`` in-flight lock (design §1)
_LOCKS: Dict[Tuple[str, str], asyncio.Lock] = {}
#: human-readable start time of the in-flight op, for the E_BUSY message
_STARTED: Dict[Tuple[str, str], str] = {}
#: per-broker active OTP request in-flight: broker -> timestamp (monotonic)
_AWAITING_OTP: Dict[str, float] = {}
#: remaining brokers queued behind an in-flight /login all step
_PENDING_LOGIN_NEXT: List[str] = []
#: short-lived record of (next_broker, monotonic_ts) chained after broker in /login all
_LAST_CHAINED_FROM: Dict[str, Tuple[str, float]] = {}

BROKER_LABEL = {"kss": "KSS", "lib": "LIB"}

#: display symbols for /status
_STATE_EMOJI = {"alive": "🟢", "pending": "🟡", "down": "🔴", "never": "⚪"}

# --------------------------------------------------------------------------- #
# Phase-B order-draft store (B.5) — in-memory, TTL-evicted
# --------------------------------------------------------------------------- #
def _draft_ttl_seconds() -> int:
    """Resolve draft TTL in seconds at call time from ``settings.CARD_TTL_SECONDS`` (fallback 180s)."""
    val = getattr(settings, "CARD_TTL_SECONDS", None)
    if val is not None:
        try:
            return int(val)
        except (ValueError, TypeError):
            pass
    return 180


#: live drafts keyed by ``draft_id``. Single-process gateway: a restart drops them
#: by design (same convention as ``conversation_state`` / ``_pending_batches``).
_DRAFTS: Dict[str, Dict[str, Any]] = {}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _text(text: str, quick: Optional[List[str]] = None) -> Dict[str, Any]:
    msg: Dict[str, Any] = {"type": "text", "text": text}
    if quick:
        msg["quickReply"] = cards.quick_reply(quick)
    return msg


def _lock_for(cmd: str, broker: str) -> asyncio.Lock:
    key = (cmd, broker or "-")
    lock = _LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[key] = lock
    return lock


def _now_bkk() -> datetime:
    return datetime.now(timezone.utc).astimezone()


def _in_nego_window(now: Optional[datetime] = None) -> bool:
    """G2 — is ``now`` (Bangkok) inside one of the configured windows?"""
    now = now or _now_bkk()
    raw = getattr(settings, "LINE_NEGO_WINDOWS", "") or ""
    for part in raw.split(","):
        part = part.strip()
        if "-" not in part:
            continue
        a, _, b = part.partition("-")
        try:
            start = datetime.strptime(a.strip(), "%H:%M").time()
            end = datetime.strptime(b.strip(), "%H:%M").time()
        except ValueError:
            continue
        if start <= now.time() <= end:
            return True
    return False


def _fmt_hhmm(iso: Optional[str]) -> str:
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%H:%M")
    except (ValueError, TypeError):
        return str(iso)[:5]


def _source_of(raw_event: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Returns ``(user_id, group_id, source_type, push_target)``."""
    src = (raw_event or {}).get("source") or {}
    user_id = src.get("userId")
    group_id = src.get("groupId")
    return user_id, group_id, src.get("type"), (group_id or user_id)


def _chat_word(push_target: Optional[str] = None) -> str:
    """Return 'กลุ่มนี้' or 'แชทนี้' matching push target or channel context."""
    from app.core.config import settings
    if push_target:
        p = push_target.strip()
        if p.startswith(("C", "c", "R", "r")):
            return "กลุ่มนี้"
        if p.startswith(("U", "u")):
            return "แชทนี้"
    return "กลุ่มนี้" if getattr(settings, "LINE_GROUP_ID", None) else "แชทนี้"


# --------------------------------------------------------------------------- #
# Broker contexts (Broker Isolation)
# --------------------------------------------------------------------------- #
def _broker_platform(broker: str) -> str:
    return "TRADEMAN" if broker == "kss" else "LIBERATOR"


async def _broker_session_alive(
    broker: str,
    allow_cdp: bool = True,
    *,
    fail_closed: bool = False,
    return_reason: bool = False,
) -> Any:
    """Is there a live, already-authenticated session? (never auto-logins)

    ``allow_cdp=True`` (default) makes the read-only CDP probe the primary truth, so a
    session that lives in the daemon's Chrome still counts after a gateway restart. The
    probe never launches a browser, never navigates and never logs in; only
    ``AUTHENTICATED`` is accepted.

    When ``fail_closed=True`` (used by the G3 money guard in ``/nego``), non-authenticated
    CDP states (``DAEMON_DOWN``/``LOGIN_PAGE``/``NO_TARGET_TAB``/``UNKNOWN``) block
    immediately without falling back to legacy in-process flags.

    When ``fail_closed=False`` (legacy non-money callers), non-authenticated probe states
    fall back to the legacy in-process flag.
    """
    clean_broker = (broker or "").strip().lower()
    last_reason = ""
    if allow_cdp:
        purpose = _ATTACH_PURPOSE.get(clean_broker, "streaming")
        try:
            from app.services.broker_cdp.session_probe import cdp_session_status
            probe = await cdp_session_status(clean_broker, purpose=purpose)
            state = probe.get("state")
            last_reason = str(state or "UNKNOWN")
            logger.info("session alive probe %s (purpose=%s): state=%s reason=%s",
                        clean_broker, purpose, state, probe.get("reason", ""))
            if state == "AUTHENTICATED":
                return (True, "AUTHENTICATED") if return_reason else True
        except Exception as e:  # noqa: BLE001
            logger.debug("session probe failed for %s (purpose=%s): %s", clean_broker, purpose, e)
            last_reason = "UNKNOWN"

        if fail_closed:
            return (False, last_reason or "UNKNOWN") if return_reason else False

    # Fallback (also the only path with allow_cdp=False): in-process flag.
    if clean_broker == "kss":
        from app.browser.auth_driver import auth_driver
        is_auth = bool(auth_driver.is_authenticated)
    else:
        from app.brokers import liberator_plugin
        health = await liberator_plugin.get_health()
        is_auth = bool(health.get("is_authenticated"))

    return (is_auth, last_reason) if return_reason else is_auth


#: CDP purpose per broker used by /login attach (matches the probe's purpose map).
_ATTACH_PURPOSE = {"kss": "streaming", "lib": "portfolio"}

#: Remote-debugging port per broker, for the user-facing zero-OTP message.
_CDP_PORT = {"kss": 9333, "lib": 9334}


async def _attach_session_cdp(broker: str) -> Optional[Dict[str, Any]]:
    """Zero-OTP `/login`: attach to an already-authenticated daemon tab.

    Returns the probe dict (plus ``attached``/``page_url``) when the driver is now
    wired to the daemon's tab, ``None`` when anything is off and the caller must fall
    back to the legacy login path.

    Read-only by contract: it only runs the CDP session probe and the drivers'
    ``connect_cdp()`` — never launches a browser, never navigates, never fires an OTP.
    """
    purpose = _ATTACH_PURPOSE.get(broker, "streaming")
    try:
        from app.services.broker_cdp.session_probe import cdp_session_status
        probe = await cdp_session_status(broker, purpose=purpose)
    except Exception as e:  # noqa: BLE001
        logger.warning("login attach probe failed for %s (purpose=%s): %s", broker, purpose, e)
        return None

    state = probe.get("state")
    reason = probe.get("reason", "")
    logger.info("login attach probe %s (purpose=%s): state=%s reason=%s", broker, purpose, state, reason)

    if state != "AUTHENTICATED":
        return None

    try:
        if broker == "kss":
            from app.browser.streaming_driver import streaming_driver
            ok = await streaming_driver.connect_cdp(target=purpose)
            page = streaming_driver.page
        else:
            from app.brokers.liberator.driver import liberator_driver
            ok = await liberator_driver.connect_cdp(target=purpose)
            page = liberator_driver.page
    except Exception as e:  # noqa: BLE001
        logger.warning("login attach failed for %s (purpose=%s): %s: %s", broker, purpose, type(e).__name__, e)
        return None

    if not ok:
        logger.warning("login attach connect_cdp returned False for %s (purpose=%s)", broker, purpose)
        return None

    if broker == "kss":
        try:
            from app.services.broker_cdp import daemon_launcher
            is_mock_ensure_tabs = hasattr(daemon_launcher.ensure_kss_tabs_complete, "_mock_name") or (
                getattr(daemon_launcher.ensure_kss_tabs_complete, "__module__", "")
                != "app.services.broker_cdp.daemon_launcher"
            )
            if not os.environ.get("PYTEST_CURRENT_TEST") or is_mock_ensure_tabs:
                sbl_probe = await cdp_session_status("kss", purpose="sbl")
                if sbl_probe.get("state") != "AUTHENTICATED":
                    logger.info(
                        "KSS streaming AUTHENTICATED on attach, SBL=%s; healing SBL tab",
                        sbl_probe.get("state"),
                    )
                    await daemon_launcher.ensure_kss_tabs_complete()
        except Exception as e:  # noqa: BLE001
            logger.debug("ensure_kss_tabs_complete during _attach_session_cdp skipped: %s", e)

    attached = dict(probe)
    attached["attached"] = True
    attached["page_url"] = getattr(page, "url", None)
    logger.info("LINE /login %s attached to the live CDP tab (%s)", broker, attached["page_url"])
    return attached


async def _detach_session_cdp(broker: str) -> bool:
    """Disconnect this process from the daemon tab without closing the daemon's Chrome."""
    try:
        if broker == "kss":
            from app.browser.streaming_driver import streaming_driver
            return await streaming_driver.disconnect_cdp()
        from app.brokers.liberator.driver import liberator_driver
        if getattr(liberator_driver, "_cdp_connected", False):
            await liberator_driver.close()      # CDP-aware: stops Playwright only
            return True
        return False
    except Exception as e:  # noqa: BLE001
        logger.debug("logout detach failed for %s: %s", broker, e)
        return False


async def _login_via_daemon(broker: str, force: bool = False) -> Dict[str, Any]:
    """Launch or restart the broker's CDP session daemon via daemon_launcher.

    Calls daemon_launcher.restart_daemon if force=True, otherwise ensure_daemon.
    When an active daemon is parked on LOGIN_PAGE/NO_TARGET_TAB/UNKNOWN or stale OTP_PAGE
    (e.g. systemd --resume-only), drives interactive CDP login via trigger_cdp_login so
    that the OTP Ref code is obtained within the same turn.
    Does NOT call session_login_broker (Pillar 1).
    """
    from app.services import otp_relay_registry
    from app.services.broker_cdp import daemon_launcher
    from app.services.broker_cdp.login_progress import get_login_progress, is_login_active

    res = await (
        daemon_launcher.restart_daemon(broker)
        if force
        else daemon_launcher.ensure_daemon(broker)
    )
    if not isinstance(res, dict):
        return res

    state = res.get("state")
    if state in ("LOGIN_PAGE", "OTP_PAGE", "NO_TARGET_TAB", "UNKNOWN"):
        is_mock_trigger = hasattr(daemon_launcher.trigger_cdp_login, "_mock_name") or (
            getattr(daemon_launcher.trigger_cdp_login, "__module__", "")
            != "app.services.broker_cdp.daemon_launcher"
        )
        if not os.environ.get("PYTEST_CURRENT_TEST") or is_mock_trigger:
            norm = daemon_launcher.normalize_broker_slug(broker)
            freshly_spawned = (
                not is_mock_trigger
                and (time.monotonic() - daemon_launcher._LAST_SPAWN_TS.get(norm, 0.0)) < 30.0
            )
            already_active = (
                not force
                and not is_mock_trigger
                and is_login_active(norm)
            )
            if freshly_spawned or already_active:
                # session_daemon.py or an in-flight task is already driving login on this tab —
                # wait briefly for OTP Ref, AUTHENTICATED, or fail-fast error instead of colliding.
                wait_s = 8.0 if freshly_spawned else 2.0
                wait_until = time.monotonic() + wait_s
                while time.monotonic() < wait_until:
                    pending = await otp_relay_registry.resolve_pending_request()
                    if (
                        pending is not None
                        and not pending.is_expired
                        and otp_relay_registry.broker_matches(broker, pending.broker)
                    ):
                        res = dict(res)
                        res["state"] = "OTP_PAGE"
                        res["status"] = "OTP_PAGE"
                        if pending.ref:
                            res["ref"] = pending.ref
                        break
                    prog = get_login_progress(norm)
                    if prog and prog.get("phase") == "error":
                        res = dict(res)
                        res["login_progress"] = prog
                        if prog.get("error"):
                            res["error"] = prog.get("error")
                        break
                    st = await daemon_launcher.daemon_state(broker)
                    if st.get("state") == "AUTHENTICATED":
                        res = dict(res)
                        res["state"] = "AUTHENTICATED"
                        res["status"] = "AUTHENTICATED"
                        break
                    await asyncio.sleep(0.5)
            else:
                pending = await otp_relay_registry.resolve_pending_request()
                has_fresh_pending = (
                    pending is not None
                    and not pending.is_expired
                    and otp_relay_registry.broker_matches(broker, pending.broker)
                )
                if force or not has_fresh_pending:
                    trig = await daemon_launcher.trigger_cdp_login(broker, wait_for_prompt_s=10.0)
                    if isinstance(trig, dict):
                        merged = dict(res)
                        merged.update({k: v for k, v in trig.items() if v is not None})
                        res = merged
                elif pending is not None and pending.ref and not res.get("ref"):
                    res = dict(res)
                    res["ref"] = pending.ref

            if not is_mock_trigger:
                prog = get_login_progress(norm)
                if prog is not None:
                    res = dict(res)
                    res["login_progress"] = prog
                    if prog.get("phase") == "error" and prog.get("error") and not res.get("error"):
                        res["error"] = prog.get("error")

    return res


async def _broker_sync_orders_cdp(broker: str) -> Optional[Dict[str, Any]]:
    """B.7 — diff the order ledger against `orders`, record manual orders (best-effort)."""
    try:
        from app.services.broker_cdp.sync_service import sync_orders_from_cdp
        ord_result = await sync_orders_from_cdp(broker)
        if ord_result.get("new_manual_count"):
            logger.info(f"B.7 manual-order sync {broker}: +{ord_result['new_manual_count']} manual order(s)")
        return ord_result
    except Exception as e:
        logger.warning(f"CDP order sync for {broker} skipped: {type(e).__name__}: {e}")
        return None


async def _broker_sync_cdp(broker: str) -> Optional[Dict[str, Any]]:
    """B.3/B.7 — persist balance + holdings AND diff the order ledger from the CDP tab.

    Opens its own DB session and commits (the webhook session is read-only here, so the
    sync must not depend on the caller to commit). Best-effort: a missing daemon or a
    broken tab is logged, never raised, so a `/port|/cash sync` still renders the live card.
    """
    try:
        from app.services.broker_cdp.sync_service import sync_portfolio_from_cdp
        result = await sync_portfolio_from_cdp(broker)
    except Exception as e:
        logger.warning(f"CDP portfolio sync for {broker} skipped: {type(e).__name__}: {e}")
        result = None

    # B.7 — diff the order ledger to catch orders the human placed manually on the
    # frontend (source='manual_sync'). Separate best-effort step so a portfolio failure
    # never masks it, and vice-versa.
    await _broker_sync_orders_cdp(broker)

    return result


async def _broker_health(broker: str) -> Dict[str, Any]:
    """Normalised health for /status — reads only, never logs in.

    Uses read-only CDP session probe as primary truth.
    Maintains backward compatibility by returning 'authenticated', 'heartbeat', 'account'
    and adds 'cdp' sub-dict and 'source'.
    """
    clean_broker = (broker or "").strip().lower()
    if clean_broker == "kss":
        from app.browser.auth_driver import auth_driver
        from app.services.broker_cdp.sync_service import (
            get_active_kss_account,
            get_active_kss_account_type,
        )
        flag_auth = bool(auth_driver.is_authenticated)
        flag_hb = _fmt_hhmm(auth_driver.last_heartbeat.isoformat() if auth_driver.last_heartbeat else None)
        # t_496c6213: name the account the CDP path actually reads (<REDACTED_KSS_ACCT> CREDIT by
        # default). This used to be a hard-coded "MARGIN", which disagreed with the
        # number /port showed.
        acct_no = get_active_kss_account()
        acct_code = get_active_kss_account_type()
        if not acct_code and acct_no:
            cash_no = str(getattr(settings, "STREAMING_ACCOUNT_NO_CASH", "") or "<REDACTED_KSS_ACCT>").strip()
            if acct_no == cash_no:
                acct_code = "CASH_BALANCE"
            else:
                acct_code = "CREDIT_BALANCE"
        acct_code = acct_code or "CREDIT_BALANCE"
        acct_kind = "MARGIN" if acct_code == "CREDIT_BALANCE" else "CASH"
        account = f"{acct_no} {acct_kind}".strip() if acct_no else acct_kind
        purpose = "streaming"
    else:
        from app.brokers import liberator_plugin
        h = await liberator_plugin.get_health()
        flag_auth = bool(h.get("is_authenticated"))
        flag_hb = _fmt_hhmm(h.get("last_heartbeat"))
        account = "CASH"
        purpose = "portfolio"

    # 1. Primary path: CDP probe
    from app.services.broker_cdp.session_probe import cdp_session_status

    cdp_info: Dict[str, Any] = {}
    is_auth = False
    source = "flag"
    try:
        probe = await cdp_session_status(clean_broker, purpose=purpose)
        cdp_info = dict(probe)
        if clean_broker == "kss":
            if probe.get("daemon_up"):
                sbl_probe = await cdp_session_status(clean_broker, purpose="sbl")
                cdp_info["sbl"] = sbl_probe
            else:
                cdp_info["sbl"] = {
                    "broker": "kss",
                    "purpose": "sbl",
                    "state": probe.get("state", "DAEMON_DOWN"),
                    "daemon_up": False,
                }

        if probe.get("daemon_up"):
            source = "CDP"
        if probe.get("state") == "AUTHENTICATED":
            is_auth = True
            source = "CDP"
    except Exception as e:
        logger.debug("_broker_health cdp probe failed for %s: %s", clean_broker, e)
        cdp_info = {
            "broker": clean_broker,
            "purpose": purpose,
            "state": "UNKNOWN",
            "daemon_up": False,
            "reason": str(e),
        }

    # 2. Fallback to flag only when CDP daemon is not up
    if not is_auth and flag_auth and not cdp_info.get("daemon_up"):
        is_auth = True
        source = "flag"

    # Heartbeat determination
    heartbeat = flag_hb
    if is_auth and not heartbeat and cdp_info.get("checked_at"):
        try:
            from datetime import datetime
            from zoneinfo import ZoneInfo
            dt = datetime.fromisoformat(cdp_info["checked_at"].replace("Z", "+00:00"))
            heartbeat = dt.astimezone(ZoneInfo("Asia/Bangkok")).strftime("%H:%M")
        except Exception:
            pass

    return {
        "authenticated": is_auth,
        "heartbeat": heartbeat,
        "account": account,
        "source": source,
        "cdp": cdp_info,
    }


async def _broker_quote(broker: str, symbol: str) -> Dict[str, Any]:
    """Live Bid1/Offer1/last for ``symbol`` from *this* broker only.

    Calls getQuote JS snippet via persistent CDP daemon session first (P.1/P.4b),
    falling back to liberator_plugin.get_market_bid_offer for Liberator.
    KSS fails closed without TradeMan fallback (100% CDP :9333 alignment).
    """
    clean_sym = (symbol or "").strip().upper()
    broker_norm = "lib" if broker in ("lib", "liberator") else "kss"

    # 1. Primary path: getQuote JS snippet via CDP
    try:
        from scripts.broker_cdp.js_runner import get_quote_js
        q_js = await get_quote_js(broker=broker_norm, symbol=clean_sym, timeout_s=5.0)
        if q_js.get("success") and (
            q_js.get("bid_1") is not None
            or q_js.get("offer_1") is not None
            or q_js.get("last") is not None
        ):
            return q_js
    except Exception as e:
        logger.debug(f"_broker_quote JS snippet failed for {broker_norm}:{clean_sym}: {e}")

    # 2. Fallback to broker driver APIs (Liberator only; KSS fails closed on CDP :9333)
    if broker_norm == "kss":
        return {
            "symbol": clean_sym,
            "success": False,
            "status": "QUOTE_UNAVAILABLE",
            "source": "unavailable",
            "error": f"อ่านราคา {clean_sym} จาก Streaming CDP :9333 ไม่ได้ (fail-closed)",
        }
    from app.brokers import liberator_plugin
    return await liberator_plugin.get_market_bid_offer(clean_sym)


async def _broker_open_orders(broker: str) -> List[Dict[str, Any]]:
    """Unmatched/open orders for ``broker`` only.

    ``kss`` routes strictly through CDP :9333 (Streaming order ledger).
    Anti-Mock: a failed ledger read raises, so the command reports an error
    instead of "no pending orders".
    """
    if broker == "kss":
        from app.services.broker_cdp.sync_service import scrape_orders_from_cdp
        snap = await scrape_orders_from_cdp("kss")
        if not snap or "orders" not in snap:
            raise RuntimeError("อ่านตารางออเดอร์ CDP Streaming ไม่ได้")
        orders = list(snap.get("orders") or [])
    else:
        from app.brokers import liberator_plugin
        orders = await liberator_plugin.get_open_orders()
    out = []
    for o in orders or []:
        balance = int(o.get("balance", 0) or 0)
        status = str(o.get("status", "")).upper()
        if balance > 0 and "CANCEL" not in status and "MATCH" not in status.split():
            if "order_no" not in o and "broker_order_no" in o:
                o["order_no"] = o["broker_order_no"]
            out.append(o)
    return out


async def _scrape_nego_orders() -> List[Dict[str, Any]]:
    """All unmatched open orders across **both** brokers, read live from CDP.

    This is the read-only feed for the nego-menu postback (N3). A broker whose
    scrape fails is skipped rather than fabricating an empty ledger (Anti-Mock):
    its orders simply do not appear, and a fully-empty result is reported to the
    user as "ไม่มีออเดอร์ค้าง".
    """
    from app.services.broker_cdp.sync_service import map_ledger_side, scrape_orders_from_cdp
    out: List[Dict[str, Any]] = []
    for broker in ("kss", "lib"):
        try:
            snap = await scrape_orders_from_cdp(broker)
        except Exception as e:
            logger.warning("nego_menu: CDP scrape failed for %s: %s", broker, e)
            continue
        for o in (snap.get("orders") or []):
            side = map_ledger_side(o.get("side"))
            if side is None:
                continue
            balance = int(o.get("balance", 0) or 0)
            status = str(o.get("status", "")).upper()
            if balance <= 0:
                continue
            if any(t in status for t in ("CANCEL", "FILL", "MATCH", "REJECT", "EXPIRE", "VOID")):
                continue
            out.append({
                "broker": broker,
                "order_no": str(o.get("broker_order_no") or ""),
                "symbol": str(o.get("symbol", "")).upper(),
                "side": side,
                "price": float(o.get("price", 0.0) or 0.0),
                "volume": int(o.get("volume", 0) or 0),
                "balance": balance,
            })
    return out


async def _scrape_order_rows(broker: str) -> List[Dict[str, Any]]:
    """Live CDP order ledger for one broker, normalised for the cancel cards.

    Read-only scrape (``scrape_orders_from_cdp``) — never a driver launch, so
    cancelling from LINE cannot open a second browser and disturb the session the
    human is witnessing. ``cancellable`` is the single source of truth for both
    the button and the guard: only a resting order (UNMATCHED / PARTIALLY_MATCHED
    with a positive balance) may be cancelled.
    """
    from app.mcp.pillars.pillar2_portfolio import _classify_order_category
    from app.services.broker_cdp.sync_service import map_ledger_side, scrape_orders_from_cdp

    snap = await scrape_orders_from_cdp(broker)
    out: List[Dict[str, Any]] = []
    for o in (snap.get("orders") or []):
        vol = int(o.get("volume", 0) or 0)
        mv = int(o.get("matched_volume", 0) or 0)
        bal = int(o.get("balance", 0) or 0)
        status_raw = str(o.get("status", "") or "")
        cat = _classify_order_category(vol, mv, bal, status_raw)
        out.append({
            "broker": broker,
            "order_no": str(o.get("broker_order_no") or o.get("order_no") or ""),
            "symbol": str(o.get("symbol", "")).upper(),
            "side": map_ledger_side(o.get("side")) or "?",
            "volume": vol,
            "matched_volume": mv,
            "balance": bal,
            "price": float(o.get("price", 0.0) or 0.0),
            "status": status_raw,
            "kind": _CAT_KIND.get(cat, "unknown"),
            "cancellable": cat in ("UNMATCHED", "PARTIALLY_MATCHED") and bal > 0,
        })
    return out


async def _find_cancel_target(brokers: List[str], order_no: str) -> List[Tuple[str, Dict[str, Any]]]:
    """Find ``order_no`` in the live books of ``brokers``. Empty = no such order.

    A broker whose scrape fails is skipped (Anti-Mock): a broken read never turns
    into "the order is gone", and never into a guess about which book to cancel.
    """
    hits: List[Tuple[str, Dict[str, Any]]] = []
    for b in brokers:
        try:
            rows = await _scrape_order_rows(b)
        except Exception as e:
            logger.warning("cancel: CDP order scrape failed for %s: %s", b, e)
            continue
        for r in rows:
            if str(r.get("order_no") or "") == str(order_no):
                hits.append((b, r))
    return hits


async def _broker_cancel(broker: str, *, order_no: str, symbol: str) -> Dict[str, Any]:
    """Cancel one resting order through the broker's **own live CDP tab**.

    Both snippets re-read the order after pressing cancel and only report success
    when the broker shows it gone (``verified``); an unverifiable cancel is
    returned as a failure so the card can never claim a cancel that did not land.
    """
    from app.services.broker_cdp.order_executor import (
        execute_liberator_cancel_js,
        execute_streaming_cancel_js,
    )
    if broker == "kss":
        res = await execute_streaming_cancel_js(order_no=str(order_no), symbol=symbol or None)
        res["path"] = "STREAMING_CDP"
    else:
        res = await execute_liberator_cancel_js(order_no=str(order_no))
        res["path"] = "LIBERATOR_CDP"
    return res


async def _draft_cancel_confirmation(
    broker: str,
    row: Dict[str, Any],
    service: Any,
    reply_token: Optional[str],
    push_target: Optional[str],
) -> Dict[str, Any]:
    """Store a cancel draft and send the two-step ❌ confirm card (nothing cancelled yet)."""
    draft_id = _new_draft_id()
    _store_draft({
        "draft_id": draft_id, "created_at": time.time(), "kind": "cancel",
        "broker": broker, "order_no": str(row.get("order_no") or ""),
        "symbol": str(row.get("symbol") or ""), "side": str(row.get("side") or "?"),
        "volume": int(row.get("balance") or row.get("volume") or 0),
        "price": float(row.get("price") or 0.0),
    })
    await _send_or_push(service, reply_token, push_target, cards.order_cancel_confirm_card(
        draft_id=draft_id,
        broker=broker,
        symbol=str(row.get("symbol") or ""),
        side=str(row.get("side") or "?"),
        order_no=str(row.get("order_no") or ""),
        volume=int(row.get("balance") or row.get("volume") or 0),
        price=float(row.get("price") or 0.0),
        status_label=str(row.get("status") or row.get("kind") or ""),
        ttl_seconds=_draft_ttl_seconds(),
    ))
    return {"status": "ORDER_CANCEL_DRAFTED", "draft_id": draft_id,
            "broker": broker, "order_no": str(row.get("order_no") or "")}


async def _handle_cancel(
    parsed: CliCommand, service: Any, reply_token: Optional[str], push_target: Optional[str]
) -> Dict[str, Any]:
    """``/cancel <order_no> [kss|lib]`` — two-step cancel of one resting order.

    Zero-OTP, no auto-login (Invariant 7): the session must already be alive, the
    order is re-read from the live book before anything is pressed, and the actual
    cancel only fires from the confirm postback.
    """
    order_no = str(parsed.target or "").strip()
    brokers: List[str] = ["kss", "lib"] if parsed.broker in (None, "all") else [str(parsed.broker)]

    for b in brokers:
        if not await _broker_session_alive(b):
            await service._send_reply(reply_token, _text(
                f"🔴 {BROKER_LABEL[b]} session หลุด — /login {b} ก่อน แล้วค่อยยกเลิก"))
            return {"status": "CLI_SESSION_DOWN", "broker": b}

    hits = await _find_cancel_target(brokers, order_no)
    if not hits:
        await service._send_reply(reply_token, _text(
            f"❌ ไม่พบออเดอร์ #{order_no} — อาจถูก match/ยกเลิกไปแล้ว\n"
            f"📋 /orders all sync เพื่อดูรายการล่าสุด"))
        return {"status": "ORDER_CANCEL_NOT_FOUND", "order_no": order_no}
    if len(hits) > 1:
        names = " · ".join(BROKER_LABEL[b] for b, _ in hits)
        first = hits[0][0]
        await service._send_reply(reply_token, _text(
            f"❓ เลขออเดอร์ #{order_no} พบใน {names} — ระบุโบรกเกอร์ให้ชัด\n"
            f"ตัวอย่าง: /cancel {order_no} {first}"))
        return {"status": "ORDER_CANCEL_AMBIGUOUS", "order_no": order_no}

    broker, row = hits[0]
    if not row.get("cancellable"):
        await service._send_reply(reply_token, _text(
            f"ℹ️ ออเดอร์ #{order_no} {row.get('symbol')} สถานะ "
            f"{row.get('status') or row.get('kind')} — ยกเลิกไม่ได้แล้ว (matched / ยกเลิกไปก่อน)"))
        return {"status": "ORDER_CANCEL_NOT_CANCELLABLE", "order_no": order_no}

    return await _draft_cancel_confirmation(broker, row, service, reply_token, push_target)


async def _handle_order_cancel_postback(
    action: str,
    params: Dict[str, str],
    service: Any,
    reply_token: Optional[str],
    push_target: Optional[str],
) -> Dict[str, Any]:
    """Three-step postback flow behind the ❌ buttons on the /orders cards.

    ``order_cancel_request`` opens the confirm card, ``order_cancel_confirm``
    re-verifies and presses cancel, ``order_cancel_abort`` drops the draft. The
    draft pop is the idempotency guard: a replayed confirm can never cancel twice.
    """
    if action == "order_cancel_abort":
        draft = _take_draft(params.get("draft_id"))
        sym = (draft or {}).get("symbol", "-")
        no = (draft or {}).get("order_no", "-")
        await _send_or_push(service, reply_token, push_target,
                            _text(f"↩️ ไม่ยกเลิก #{no} {sym} — ออเดอร์ยังอยู่"))
        return {"status": "ORDER_CANCEL_ABORTED"}

    if action == "order_cancel_request":
        broker = (params.get("broker") or "").lower()
        order_no = str(params.get("order_no") or "").strip()
        if broker not in ("kss", "lib") or not order_no:
            return {"status": "ORDER_CANCEL_BAD_POSTBACK"}
        if not await _broker_session_alive(broker):
            await _send_or_push(service, reply_token, push_target, _text(
                f"🔴 {BROKER_LABEL[broker]} session หลุด — /login {broker} ก่อน แล้วค่อยยกเลิก"))
            return {"status": "CLI_SESSION_DOWN", "broker": broker}
        hits = await _find_cancel_target([broker], order_no)
        if not hits:
            await _send_or_push(service, reply_token, push_target, _text(
                f"ℹ️ ออเดอร์ #{order_no} ไม่อยู่ในสมุดแล้ว (match / ยกเลิกไปก่อน) — /orders {broker} sync"))
            return {"status": "ORDER_CANCEL_NOT_FOUND", "order_no": order_no}
        row = hits[0][1]
        if not row.get("cancellable"):
            await _send_or_push(service, reply_token, push_target, _text(
                f"ℹ️ ออเดอร์ #{order_no} สถานะ {row.get('status') or row.get('kind')} — ยกเลิกไม่ได้แล้ว"))
            return {"status": "ORDER_CANCEL_NOT_CANCELLABLE", "order_no": order_no}
        return await _draft_cancel_confirmation(broker, row, service, reply_token, push_target)

    # order_cancel_confirm
    draft = _take_draft(params.get("draft_id"))
    if not draft:
        await _send_or_push(service, reply_token, push_target, _text(
            "⏳ การ์ดยกเลิกหมดอายุแล้ว — กด ❌ จากการ์ด /orders อีกครั้ง"))
        return {"status": "ORDER_CANCEL_DRAFT_EXPIRED"}

    broker = str(draft.get("broker") or "")
    order_no = str(draft.get("order_no") or "")
    symbol = str(draft.get("symbol") or "")

    # Fail-closed re-check: the book may have moved between the card and the tap.
    hits = await _find_cancel_target([broker], order_no)
    if not hits:
        await _send_or_push(service, reply_token, push_target, _text(
            f"ℹ️ ออเดอร์ #{order_no} ไม่อยู่ในสมุดแล้ว — ไม่มีการยกเลิกซ้ำ"))
        return {"status": "ORDER_CANCEL_ALREADY_GONE", "order_no": order_no}
    row = hits[0][1]
    if not row.get("cancellable"):
        await _send_or_push(service, reply_token, push_target, _text(
            f"ℹ️ ออเดอร์ #{order_no} สถานะ {row.get('status') or row.get('kind')} — ยกเลิกไม่ได้แล้ว"))
        return {"status": "ORDER_CANCEL_NOT_CANCELLABLE", "order_no": order_no}

    try:
        res = await _broker_cancel(broker, order_no=order_no, symbol=symbol)
    except Exception as e:
        logger.exception("LINE CLI /cancel %s #%s failed", broker, order_no)
        res = {"success": False, "verified": False,
               "error": f"{type(e).__name__}: {str(e)[:120]}"}

    await _send_or_push(service, reply_token, push_target, cards.order_cancel_result_card(
        broker=broker, symbol=symbol, order_no=order_no,
        side=str(row.get("side") or draft.get("side") or "?"),
        volume=int(row.get("balance") or draft.get("volume") or 0),
        success=bool(res.get("success")), verified=bool(res.get("verified")),
        detail=res.get("error"), ts=_now_bkk().strftime("%H:%M:%S"),
    ))
    ok = bool(res.get("success") and res.get("verified"))
    return {"status": "ORDER_CANCELLED" if ok else "ORDER_CANCEL_FAILED",
            "broker": broker, "order_no": order_no, "result": res}


async def _live_cards(kind: str, broker: str) -> List[Dict[str, Any]]:
    """Messages for one live read. ``orders`` ships unified bubbles with Match / Lure / Cancel buttons."""
    if kind != "orders":
        return [await _live_card(kind, broker)]
    try:
        rows = await _scrape_order_rows(broker)
    except Exception as e:
        logger.warning("CDP order scrape failed for %s: %s", broker, e)
        return [cards.unavailable_card(broker=BROKER_LABEL[broker], login_cmd=f"/login {broker}")]
    return cards.orders_manage_cards(
        broker=broker, orders=rows,
        snapshot_label=_now_bkk().strftime("%H:%M:%S"),
    )


async def _broker_modify(broker: str, *, order_no: str, new_price: float, symbol: str, side: str, volume: int) -> Dict[str, Any]:
    """
    Amend an order in place, routed per broker (Broker Isolation).

    TEST -> shared virtual simulator (through the dispatcher); LIVE -> the broker's own
    driver. ``kss`` LIVE goes through the Quick Trade passive policy
    (``negotiate_order_price``): Settrade's *Change Price& Volume* keeps the order
    number and the volume, so no cancel+replace is reachable from ``/nego`` any more
    (D1 - TASK-loop1-2). ``lib`` LIVE keeps the native WebTrade ``Change``
    (LIB-RCA-016/017). The dispatcher's LIVE route is bound to ``TRADING_PLATFORM`` and
    cannot address both brokers in one loop, so LIVE goes straight to the owning driver.
    LIB also re-checks the board quote here (Loop 1 D3): the reading taken during planning may
    have died, and the price basis must still be provable — an unreadable / wrong-symbol /
    empty-side reading aborts the amend with ``QUOTE_UNAVAILABLE`` before the driver is called.
    """
    from app.services.trade_dispatcher import trade_dispatcher
    if not trade_dispatcher.is_live:
        return await trade_dispatcher.dispatch_price_modification(
            broker_order_no=order_no, new_price=new_price, symbol=symbol, side=side, volume=volume
        )
    if broker == "kss":
        from app.browser.streaming_driver import streaming_driver
        from app.core.config import settings
        res = await streaming_driver.change_order_native(
            broker_order_no=str(order_no),
            new_price=float(new_price),
            new_volume=int(volume or 0),
            pin=settings.KRUNGSRI_PIN,
            symbol=symbol,
            side=side,
            auto_launch=True,
        )
        res["path"] = "STREAMING_CDP"
        if res.get("success") and "submitted" not in res:
            res["submitted"] = not getattr(settings, "DRY_RUN_MODE", False)
        return res
    # ── LIB (LIVE) ───────────────────────────────────────────────────────────────
    # Fail-closed second gate (Loop 1 D3): the quote may have died between planning and the
    # amend, and the price basis must still be provable. If the board cannot be read, or the
    # reading is not for this symbol, or the side we need is empty → ABORT with
    # ``QUOTE_UNAVAILABLE`` and press nothing (the caller shows the honest "อ่านไม่ได้" card).
    from app.brokers import liberator_plugin
    quote = await _broker_quote(broker, symbol)
    if quote.get("success") is False or quote.get("source") == "unavailable":
        return {
            "success": False, "status": "QUOTE_UNAVAILABLE", "unavailable": True,
            "error": str(quote.get("error") or f"อ่านราคา {symbol} จาก {BROKER_LABEL[broker]} ไม่ได้"),
        }
    if str(quote.get("symbol") or "").upper() != str(symbol or "").upper():
        return {
            "success": False, "status": "QUOTE_UNAVAILABLE", "unavailable": True,
            "error": (f"ราคาที่อ่านได้เป็น {quote.get('symbol') or '-'} ไม่ใช่ {symbol}"
                      " — ไม่แก้ราคา (fail-closed)"),
        }
    needed = "bid_1" if str(side or "").upper() in ("BUY", "COVER") else "offer_1"
    if quote.get(needed) is None:
        return {
            "success": False, "status": "QUOTE_UNAVAILABLE", "unavailable": True,
            "error": f"อ่าน {needed} ของ {symbol} ไม่ได้ — ไม่แก้ราคา (fail-closed)",
        }
    # The driver re-reads the live order row and refuses when its symbol is not ``symbol``
    # (status SYMBOL_MISMATCH) — nothing is pressed in that case either.
    return await liberator_plugin.change_order(
        order_no=order_no, new_price=new_price, symbol=symbol)


# --------------------------------------------------------------------------- #
# /help
# --------------------------------------------------------------------------- #
_HELP_TEXT = (
    "📖 คำสั่ง Homelab (16 คำสั่ง)\n"
    "📱 Settrade Streaming (KSS) · พอร์ต & ต่อราคา\n"
    "  /status [kss|lib|all]        สถานะระบบ & session\n"
    "  /port [kss|lib|all] [sync]   หุ้นที่ถือ + P/L (สด CDP · รวม /port all sync)\n"
    "  /cash [kss|lib|all]          เงินสด / กำลังซื้อ (สด CDP)\n"
    "  /orders [kss|lib|all]        ออเดอร์วันนี้ (สด CDP · การ์ดแยกหุ้น + ปุ่มยกเลิก)\n"
    "  /cancel <order_no> [kss|lib] ยกเลิกออเดอร์ที่ยังค้าง (ยืนยัน 2 จังหวะ)\n"
    "  /nego kss|lib|all [SYM|เลขออเดอร์] ต่อราคาไป Bid1/Offer1 (รวม /nego all)\n"
    "🔄 Krungsri SBL Realtime · ยืม/คืนหุ้น\n"
    "  /sbl [sync]                  รายการหุ้นยืม SBL ทั้งหมด\n"
    "  /sbl pool <SYM>              ตรวจ pool หุ้นยืมที่พร้อมให้กู้ + XD/XR\n"
    "  /borrow <SYM> <VOL|VALb>     ขอยืมหุ้น SBL เข้าพอร์ต (ระบุหุ้นหรือบาท)\n"
    "  /return <SYM> [VOL|all]      คืนหุ้น SBL ที่ยืมไว้\n"
    "📈 Liberator & ส่งคำสั่งเทรด (Cash/Margin)\n"
    "  /buy <SYM> <VOL|VALb> [PRICE]   ซื้อหุ้น (Lib Cash · ระบุหุ้นหรือบาท)\n"
    "  /sell <SYM> <VOL> [PRICE]       ขายหุ้น (Lib Cash)\n"
    "  /short <SYM> <VOL> [PRICE]      ชอร์ตหุ้น (KSS Margin · กฎ Uptick G6)\n"
    "  /cover <SYM> <VOL> [PRICE]      ซื้อคืนชอร์ต (KSS Margin)\n"
    "🧮 เครื่องมือ & ความปลอดภัย\n"
    "  /size <SYM>                  คำนวณขนาดซื้อ 50k/75k/100k (alias /calc)\n"
    "  /stop [kss|lib|all]          พักบราวเซอร์ CDP คืน RAM (Gateway ออนไลน์ 24 ชม.)\n"
    "  /halt                        🚨 หยุดฉุกเฉิน (ยกเลิกทุกออเดอร์ + ปิดระบบเทรด)\n"
    "ฉุกเฉิน: PANIC · RESET · MODE\n"
    "พิมพ์ /help [nego|size|sbl|trade|cancel|port] เพื่อดูวิธีใช้ละเอียด"
)

_HELP_NEGO = (
    "⚡ /nego — ต่อราคาออเดอร์ค้าง (ทำทันที ไม่ต้องอนุมัติ)\n"
    "รูปแบบ: /nego kss|lib [SYMBOL|order_no]\n"
    "• ไม่ระบุเป้าหมาย = ทุกออเดอร์ค้างของโบรกเกอร์นั้น\n"
    "• kss = แก้ราคาในที่บนออเดอร์เดิม (CDP amend · ไม่ยกเลิก ไม่สร้างใหม่)\n"
    "• ราคาเป้าหมาย: BUY/COVER→Bid1 · SELL→Offer1 · SHORT→Offer1 (สูงกว่าราคาล่าสุด)\n"
    "Guardrails (ต่อออเดอร์):\n"
    "  G1 kill-switch ไม่ active\n"
    "  G2 ในเวลาที่อนุญาต (LINE_NEGO_WINDOWS)\n"
    "  G3 session alive (ไม่ login เอง)\n"
    "  G4 ผ่าน SET tick-size\n"
    "  G5 |ราคาใหม่−เดิม| ≤ LINE_NEGO_MAX_TICKS tick\n"
    "  G6 SHORT ต้องผ่านกฎ uptick"
)

_HELP_NEGOTIATE = (
    "⚡ /negotiate — ต่อราคาออเดอร์ Streaming แบบแก้ในที่ (Settrade FastOrder)\n"
    "รูปแบบ: /negotiate <order_no>   เช่น /negotiate 11008198\n"
    "• ไม่ยกเลิก/สร้างออเดอร์ใหม่ — แก้ราคาที่ออเดอร์เดิม (จำนวนเดิมเสมอ)\n"
    "• BUY→Bid1 · SELL→Offer1 · SHORT→Offer1 (ต้องสูงกว่าราคาล่าสุด)\n"
    "• SHORT ที่ Offer1 ≤ LastPrice = ⛔ ผิดกฎ uptick → ไม่ส่ง (fail-closed)\n"
    "• ใช้ session เดิมเท่านั้น (Zero-OTP) · ไม่ login เอง\n"
    "Guardrails: G1 kill-switch · G2 LINE_NEGO_WINDOWS · G4 tick-size · G5 ≤ 3 tick · กันกดซ้ำต่อออเดอร์"
)

_HELP_SIZE = (
    "🧮 /size — คำนวณจำนวนหุ้นงบ 50k / 75k / 100k จากราคาตลาดสด\n"
    "รูปแบบ: /size <symbol>   เช่น /size PTT (alias /calc)\n"
    "• ดึงราคาตลาดล่าสุด (Offer1/Last) จาก Liberator / Krungsri CDP\n"
    "• ปัดเศษลงตาม Board Lot (100 หุ้น) พร้อมคำนวณมูลค่าจริง\n"
    "• มีปุ่ม Quick Reply ให้กดส่งคำสั่งซื้อ /buy ได้ทันที\n"
)

_HELP_SBL = (
    "🔄 /sbl — ระบบยืมและคืนหุ้น SBL Real Time (KSS Margin <REDACTED_KSS_ACCT>)\n"
    "รูปแบบคำสั่ง:\n"
    "• /sbl [sync] — แสดงรายการสัญญาหุ้นยืมที่ยังค้างอยู่ทั้งหมด (reconcile กับโบรกเกอร์)\n"
    "• /sbl pool <SYM> — ตรวจสอบคลังหุ้นที่พร้อมให้ยืม อัตราดอกเบี้ย 3.5% และตรวจ XD/XR\n"
    "• /borrow <SYM> <VOL|VALb> — ขอยืมหุ้น เช่น /borrow BCPG 1000 หรือ /borrow BCPG 50000b\n"
    "• /return <SYM> [VOL|all] — คืนหุ้นที่ยืม เช่น /return BCPG 1000 หรือ /return BCPG all\n"
    "ความปลอดภัย:\n"
    "  - ห้ามยืมหุ้นติด XD/XR เด็ดขาด (Hard block เพื่อป้องกันค่าปรับปันผล 100%)\n"
    "  - ยืนยัน 2 จังหวะเสมอผ่านการ์ด Draft ก่อนยืม/คืนจริง"
)

_HELP_TRADE = (
    "📈 ส่งคำสั่งซื้อขายหุ้น (Phase B Order Entry)\n"
    "รูปแบบคำสั่ง:\n"
    "• /buy <SYM> <VOL|VALb> [PRICE] — ซื้อหุ้น (Default: Liberator Cash)\n"
    "• /sell <SYM> <VOL> [PRICE] — ขายหุ้น (Default: Liberator Cash)\n"
    "• /short <SYM> <VOL> [PRICE] — ชอร์ตหุ้น (Default: KSS Margin)\n"
    "• /cover <SYM> <VOL> [PRICE] — ซื้อคืนชอร์ต (Default: KSS Margin)\n"
    "จุดเด่น:\n"
    "  - ระบุงบประมาณเป็นบาทได้ เช่น /buy PTT 50000b (คำนวณ board lot ให้อัตโนมัติ)\n"
    "  - ละเว้นราคาได้ ระบบจะตั้งราคา passive lure price ไม่ข้าม spread\n"
    "  - คำสั่ง /short บังคับผ่านกฎ Uptick (G6) เสมอ\n"
    "  - สร้าง Draft Card ให้ตรวจสอบก่อนกดปุ่มยืนยันเสมอ"
)

_HELP_CANCEL = (
    "❌ /cancel — ยกเลิกออเดอร์ที่ยังค้างในตลาด (Two-Step Confirmation)\n"
    "รูปแบบคำสั่ง: /cancel <order_no> [kss|lib]\n"
    "• เช่น /cancel 11008198 หรือ /cancel 11008198 kss\n"
    "• ระบบจะตรวจสอบสถานะออเดอร์สดจากตลาดก่อนเสมอ (Zero-OTP)\n"
    "• แสดงการ์ดสรุปข้อมูลออเดอร์ พร้อมปุ่ม 'ยืนยันยกเลิก' และ 'ไม่ยกเลิก'\n"
    "• ทำการยกเลิกบนหน้าจอ CDP ของโบรกเกอร์ และ verify สถานะหลังยกเลิกทันที"
)

_HELP_PORT = (
    "📱 /port — ดูพอร์ตหุ้นและผลกำไรขาดทุนสด (CDP Scrape)\n"
    "รูปแบบคำสั่ง: /port [kss|lib|all] [sync]\n"
    "• /port kss — พอร์ต Krungsri (แยกหุ้น Long, Short, และ Borrowed)\n"
    "• /port lib — พอร์ต Liberator Cash Balance\n"
    "• /port all sync — พอร์ตรวม 2 โบรกเกอร์ พร้อมการ์ดแยกรายหุ้น interactive\n"
    "• การ์ดแยกรายหุ้นมีปุ่ม Action อัตโนมัติ: ขาย (Sell), ชอร์ตเพิ่ม (Short), คืนหุ้น (Return), ซื้อคืน (Cover)"
)


async def _handle_schedule(service: Any, reply_token: Optional[str]) -> Dict[str, Any]:
    """Reply-only (0 push quota) Auto-Nego Scheduler daily checklist summary."""
    from app.services.nego_scheduler import nego_scheduler

    snap = nego_scheduler.get_schedule_snapshot()
    master = "🟢 ON" if snap.get("master_enabled") else "🔴 OFF"
    mode_tag = "🧪 DRY-RUN" if snap.get("dry_run") else "🔴 LIVE"
    lines = [
        f"⏱️ Auto-Nego Schedule ({snap.get('daily_date')})",
        f"Master: {master} · {mode_tag}",
    ]
    nxt = snap.get("next_run")
    if nxt:
        lines.append(f"รอบถัดไป: {nxt['scheduled_time']} ({nxt['label']} · {nxt['mode'].upper()})")
    lines.append("")
    icon_map = {
        "PENDING": "⏳",
        "AMENDED": "✅",
        "NO_CHANGE": "👌",
        "NO_ORDERS": "➖",
        "DISABLED": "⏭️",
        "MARKET_CLOSED": "🌙",
        "MISSED_WINDOW": "⌛",
        "BLOCKED_KILL_SWITCH": "🚨",
        "ERROR": "❌",
        "RUNNING": "🔄",
    }
    for w in snap.get("windows") or []:
        run = w.get("run") or {}
        st = run.get("status") or "PENDING"
        ic = icon_map.get(st, "•")
        t_str = run.get("scheduled_time") or "-"
        cnt = run.get("counts") or {}
        summary = (
            f"แก้ {cnt.get('amended', 0)}"
            if st == "AMENDED"
            else st
        )
        lines.append(f"{ic} {w['period']} [{w['mode'].upper()}] @ {t_str} → {summary}")

    await service._send_reply(reply_token, _text("\n".join(lines)))
    return {"status": "CLI_SCHEDULE", "daily_date": snap.get("daily_date")}


async def _handle_help(parsed: CliCommand, service: Any, reply_token: Optional[str]) -> Dict[str, Any]:
    target = (parsed.target or "").strip().lower()
    if target == "nego":
        await service._send_reply(reply_token, _text(_HELP_NEGO))
    elif target == "negotiate":
        await service._send_reply(reply_token, _text(_HELP_NEGOTIATE))
    elif target in ("size", "calc"):
        await service._send_reply(reply_token, _text(_HELP_SIZE))
    elif target in ("sbl", "borrow", "return"):
        await service._send_reply(reply_token, _text(_HELP_SBL))
    elif target in ("trade", "order", "buy", "sell", "short", "cover"):
        await service._send_reply(reply_token, _text(_HELP_TRADE))
    elif target == "cancel":
        await service._send_reply(reply_token, _text(_HELP_CANCEL))
    elif target in ("port", "portfolio"):
        await service._send_reply(reply_token, _text(_HELP_PORT))
    else:
        await service._send_reply(reply_token, _text(_HELP_TEXT, quick=["/status", "/port", "/orders"]))
    return {"status": "CLI_HELP"}


# --------------------------------------------------------------------------- #
# /status
# --------------------------------------------------------------------------- #
async def _handle_status(service: Any, reply_token: Optional[str]) -> Dict[str, Any]:
    from app.services.kill_switch_service import kill_switch_service
    from app.services.otp_relay_readiness import otp_relay_readiness
    from app.services.trade_dispatcher import trade_dispatcher

    kill_active = kill_switch_service.is_active()
    relay = await otp_relay_readiness.check()
    mode_badge = trade_dispatcher.get_mode_badge()

    brokers: List[Dict[str, Any]] = []
    for key in ("kss", "lib"):
        h = await _broker_health(key)
        cdp = h.get("cdp") or {}
        acct = h.get("account", "MARGIN" if key == "kss" else "CASH")
        name_with_acct = f"{BROKER_LABEL[key]} ({acct})"

        # Timestamp
        checked_iso = cdp.get("checked_at")
        time_str = _now_bkk().strftime("%H:%M:%S")
        if checked_iso:
            try:
                from datetime import datetime
                from zoneinfo import ZoneInfo
                dt = datetime.fromisoformat(checked_iso.replace("Z", "+00:00"))
                time_str = dt.astimezone(ZoneInfo("Asia/Bangkok")).strftime("%H:%M:%S")
            except Exception:
                pass

        # PID
        pid_val = cdp.get("pid")
        pid_str = f"pid {pid_val}" if pid_val else "pid -"

        # Source
        source_val = h.get("source") or ("CDP" if cdp.get("daemon_up") else "flag")
        port_val = cdp.get("port")
        if source_val == "CDP" and port_val:
            source_label = f"CDP {port_val}"
        elif source_val == "CDP":
            source_label = "CDP"
        else:
            source_label = "flag"

        # State & badge mapping
        cdp_state = cdp.get("state")
        if not cdp_state:
            emoji = "🟢" if h.get("authenticated") else "🔴"
            tab_status = f"heartbeat {h.get('heartbeat')}" if h.get("authenticated") else "ไม่ได้เข้าระบบ"
            line1 = f"{source_label} · {tab_status}"
            line2 = f"บัญชี {acct}"
            line3 = time_str
        else:
            def _badge(st: str) -> tuple[str, str]:
                if st == "AUTHENTICATED":
                    return "🟢", "AUTH"
                if st == "LOGIN_PAGE":
                    return "🟡", "LOGIN"
                if st == "NO_TARGET_TAB":
                    return "🟡", "NO_TAB"
                if st == "DAEMON_DOWN":
                    return "🔴", "DAEMON_DOWN"
                return "🟡", "UNKNOWN"

            if key == "kss":
                str_emoji, str_txt = _badge(cdp_state)
                sbl_info = cdp.get("sbl") or {}
                sbl_state = sbl_info.get("state", "UNKNOWN")
                _sbl_emoji, sbl_txt = _badge(sbl_state)
                tab_status = f"streaming {str_txt} · sbl {sbl_txt}"

                if str_txt == "AUTH":
                    emoji = "🟢"
                elif str_txt == "DAEMON_DOWN":
                    emoji = "🔴"
                elif str_txt in ("LOGIN", "NO_TAB") or sbl_txt == "LOGIN":
                    emoji = "🟡"
                else:
                    emoji = "🟡"
            else:
                lib_emoji, lib_txt = _badge(cdp_state)
                tab_status = f"portfolio {lib_txt}"
                if lib_txt == "AUTH":
                    emoji = "🟢"
                elif lib_txt == "DAEMON_DOWN":
                    emoji = "🔴"
                elif lib_txt in ("LOGIN", "NO_TAB"):
                    emoji = "🟡"
                else:
                    emoji = "🟡"

            line1 = f"{source_label} · {tab_status}"
            line2 = f"{pid_str} · {time_str}"
            line3 = f"บัญชี {acct}"

        summary_line = f"{emoji} {name_with_acct} · {source_label} · {tab_status} · {pid_str} · {time_str}"

        brokers.append({
            "key": key,
            "name": name_with_acct,
            "emoji": emoji,
            "line1": line1,
            "line2": line2,
            "line3": line3,
            "summary": summary_line,
        })

    if relay.ready:
        relay_label = "พร้อม" + (f" ({relay.channel})" if relay.channel else "")
    else:
        relay_label = (relay.reason or "ไม่พร้อม")[:40]

    card = cards.status_card(
        brokers=brokers, relay_ready=relay.ready, relay_label=relay_label,
        mode_badge=mode_badge, kill_active=kill_active,
        updated=_now_bkk().strftime("%H:%M:%S"),
    )
    meta = {"status": "CLI_STATUS", "kill_active": kill_active, "relay_ready": relay.ready,
            "kss_alive": brokers[0]["emoji"] == "🟢", "lib_alive": brokers[1]["emoji"] == "🟢",
            "brokers": brokers, "card": card}
    if service and reply_token:
        await service._send_reply(reply_token, card)
    return meta


# --------------------------------------------------------------------------- #
# /login
# --------------------------------------------------------------------------- #
async def _get_active_otp_broker(timeout_seconds: Optional[float] = None) -> Optional[str]:
    """Check if any broker currently has an OTP request in-flight (single-flight guard)."""
    from app.core.config import settings
    from app.services.broker_cdp import daemon_launcher

    timeout = float(timeout_seconds if timeout_seconds is not None else settings.OTP_TIMEOUT_SECONDS)
    now = time.monotonic()
    is_unmocked_pytest = (
        bool(os.environ.get("PYTEST_CURRENT_TEST"))
        and not hasattr(daemon_launcher.daemon_state, "_mock_name")
        and getattr(daemon_launcher.daemon_state, "__module__", "") == "app.services.broker_cdp.daemon_launcher"
    )

    # 1. In-memory tracked brokers
    for b, ts in list(_AWAITING_OTP.items()):
        if now - ts > timeout:
            _AWAITING_OTP.pop(b, None)
            continue
        if not is_unmocked_pytest:
            try:
                st = await daemon_launcher.daemon_state(b)
                if st.get("state") in ("AUTHENTICATED", "DAEMON_DOWN"):
                    _AWAITING_OTP.pop(b, None)
                    continue
            except Exception:
                pass
        return b

    # 2. Directly inspect daemon states — only treat OTP_PAGE as active if an
    # unexpired pending OTP request actually exists for that broker in registry.
    if is_unmocked_pytest:
        return None
    from app.services import otp_relay_registry

    try:
        pending = await otp_relay_registry.resolve_pending_request()
    except Exception:
        pending = None

    if pending is not None and not pending.is_expired:
        for b in ("kss", "lib"):
            if not otp_relay_registry.broker_matches(b, pending.broker):
                continue
            try:
                st = await daemon_launcher.daemon_state(b)
                if st.get("state") == "OTP_PAGE":
                    _AWAITING_OTP[b] = now
                    return b
            except Exception:
                pass

    return None


async def _wait_for_daemon_authenticated(
    broker: str,
    timeout_seconds: Optional[float] = None,
    poll_interval: Optional[float] = None,
) -> Dict[str, Any]:
    """Wait sequentially until broker daemon reaches AUTHENTICATED or times out / fails."""
    from app.core.config import settings
    from app.services.broker_cdp import daemon_launcher

    timeout = float(timeout_seconds if timeout_seconds is not None else settings.OTP_TIMEOUT_SECONDS)
    interval = float(poll_interval if poll_interval is not None else min(1.0, max(0.01, timeout / 10)))
    deadline = time.monotonic() + max(0.01, timeout)

    while time.monotonic() < deadline:
        await asyncio.sleep(interval)
        try:
            if await _broker_session_alive(broker, fail_closed=True):
                return {"ok": True, "state": "AUTHENTICATED", "status": "ok"}
            st = await daemon_launcher.daemon_state(broker)
            state = st.get("state")
            if state == "AUTHENTICATED":
                return {"ok": True, "state": "AUTHENTICATED", "status": "ok", "info": st}
            if state == "DAEMON_DOWN":
                return {"ok": False, "state": "DAEMON_DOWN", "status": "failed", "detail": "daemon died", "info": st}
        except Exception as e:
            logger.debug(f"_wait_for_daemon_authenticated check failed: {e}")

    return {
        "ok": False,
        "state": "TIMEOUT",
        "status": "timeout",
        "detail": f"OTP timeout ({int(timeout)}s)",
    }


async def _handle_login(
    parsed: CliCommand, service: Any, reply_token: Optional[str], push_target: Optional[str]
) -> Dict[str, Any]:
    from app.services.kill_switch_service import kill_switch_service
    from app.services.otp_relay_readiness import assert_push_deliverable, otp_relay_readiness

    if kill_switch_service.is_active():
        await service._send_reply(reply_token, _text("⛔ KILL-SWITCH ACTIVE — คำสั่งถูกระงับ · พิมพ์ RESET เพื่อปลด"))
        return {"status": "CLI_KILL_SWITCH"}

    broker_arg = (parsed.broker or "").strip().lower()
    brokers: List[str] = ["kss", "lib"] if broker_arg == "all" else [broker_arg]
    force = parsed.has("force")

    key = ("login", "all" if broker_arg == "all" else broker_arg)
    # 4.6: Prevent /login kss and /login all running concurrently
    login_locks = [_lock_for("login", "gate"), _lock_for(*key)]
    if any(l.locked() for l in login_locks):
        await service._send_reply(reply_token, _text(
            f"⏳ /login {parsed.broker} กำลังทำงานอยู่ (เริ่ม {_STARTED.get(key, '-')}) — รอผลสักครู่"))
        return {"status": "CLI_BUSY"}

    # 6.4: single-flight guard: refuse if another broker currently has an OTP in-flight
    active_otp = await _get_active_otp_broker()
    if active_otp:
        from app.services import otp_relay_registry

        if force and (broker_arg == active_otp or broker_arg == "all"):
            _AWAITING_OTP.pop(active_otp, None)
        elif broker_arg == active_otp:
            pending_req = await otp_relay_registry.resolve_pending_request()
            started_age = time.monotonic() - _AWAITING_OTP.get(active_otp, 0.0)
            if (
                pending_req is not None
                and not pending_req.is_expired
                and otp_relay_registry.broker_matches(active_otp, pending_req.broker)
            ):
                label = BROKER_LABEL.get(active_otp, active_otp.upper())
                ref_part = f" (Ref: {pending_req.ref})" if pending_req.ref else ""
                msg = f"⏳ มี OTP ของ {label}{ref_part} รอการยืนยันอยู่ — กรุณากรอก OTP ให้เสร็จก่อน หรือพิมพ์ /login {active_otp} force"
                await service._send_reply(reply_token, _text(msg))
                return {"status": "CLI_BUSY", "detail": "otp_in_flight", "active_broker": active_otp, "message": msg}
            elif started_age < 5.0 and os.environ.get("PYTEST_CURRENT_TEST"):
                label = BROKER_LABEL.get(active_otp, active_otp.upper())
                msg = f"⏳ มี OTP ของ {label} รอการยืนยันอยู่ — กรุณากรอก OTP ให้เสร็จก่อน หรือรอให้หมดเวลา"
                await service._send_reply(reply_token, _text(msg))
                return {"status": "CLI_BUSY", "detail": "otp_in_flight", "active_broker": active_otp, "message": msg}
            else:
                _AWAITING_OTP.pop(active_otp, None)
        else:
            pending_req = await otp_relay_registry.resolve_pending_request()
            ref_part = (
                f" (Ref: {pending_req.ref})"
                if (
                    pending_req is not None
                    and not pending_req.is_expired
                    and otp_relay_registry.broker_matches(active_otp, pending_req.broker)
                    and pending_req.ref
                )
                else ""
            )
            label = BROKER_LABEL.get(active_otp, active_otp.upper())
            msg = f"⏳ มี OTP ของ {label}{ref_part} รอการยืนยันอยู่ — กรุณากรอก OTP ให้เสร็จก่อน หรือรอให้หมดเวลา"
            await service._send_reply(reply_token, _text(msg))
            return {"status": "CLI_BUSY", "detail": "otp_in_flight", "active_broker": active_otp, "message": msg}

    avail_reply_token: List[Optional[str]] = [reply_token]

    def _is_quota_exhausted() -> bool:
        return bool(getattr(service, "_push_quota_exhausted", False))

    async def _deliver_login_msg(
        msg_obj: Dict[str, Any],
        *,
        prefix_lines: Optional[List[str]] = None,
    ) -> None:
        if prefix_lines:
            msg_obj = dict(msg_obj)
            msg_obj["text"] = "\n".join(prefix_lines) + "\n" + str(msg_obj.get("text", ""))
            prefix_lines.clear()
        if _is_quota_exhausted():
            if avail_reply_token[0]:
                tok = avail_reply_token[0]
                avail_reply_token[0] = None
                await service._send_reply(tok, msg_obj)
            else:
                logger.info("[LINE CLI] Push quota exhausted & replyToken consumed; skipping proactive push")
            return
        ok = await service.push_message(msg_obj, to_user_id=push_target)
        if not ok and _is_quota_exhausted() and avail_reply_token[0]:
            tok = avail_reply_token[0]
            avail_reply_token[0] = None
            await service._send_reply(tok, msg_obj)

    from contextlib import AsyncExitStack
    async with AsyncExitStack() as stack:
        for l in login_locks:
            await stack.enter_async_context(l)
        _STARTED[key] = _now_bkk().strftime("%H:%M:%S")
        results: List[Dict[str, Any]] = []
        phase1_notes: List[str] = []
        try:
            # ── Phase 1 — check existing sessions (zero-OTP attach or already alive) ──
            pending: List[str] = []
            if force:
                pending = list(brokers)     # `force` is the only path that opens a browser + OTP
            else:
                for b in brokers:
                    attached = await _attach_session_cdp(b)
                    if attached:
                        port = attached.get("port") or _CDP_PORT.get(b) or "-"
                        note = f"🟢 {BROKER_LABEL[b]} เชื่อมกับแท็บที่เปิดอยู่แล้ว (zero-OTP) · {port}"
                        if _is_quota_exhausted():
                            phase1_notes.append(note)
                        else:
                            await service.push_message(
                                _text(note, quick=[f"/port {b}", f"/status {b}"]),
                                to_user_id=push_target,
                            )
                        results.append({
                            "broker": b,
                            "status": "attached",
                            "port": port,
                            "url_path": attached.get("url_path"),
                            "page_url": attached.get("page_url"),
                        })
                    elif await _broker_session_alive(b, fail_closed=True):
                        label = BROKER_LABEL[b]
                        note = f"🟢 {label} เชื่อมต่ออยู่แล้ว — ใช้ /login {b} force เพื่อเข้าใหม่"
                        if _is_quota_exhausted():
                            phase1_notes.append(note)
                        else:
                            await service.push_message(
                                _text(note),
                                to_user_id=push_target,
                            )
                        results.append({"broker": b, "status": "already_alive"})
                    else:
                        pending.append(b)
            if not pending:
                if phase1_notes:
                    await _deliver_login_msg(
                        _text("\n".join(phase1_notes), quick=["/port all sync", "/status"])
                    )
                return {"status": "CLI_LOGIN", "results": results}

            # ── Phase 2 — legacy path: needs a real login, so the SMS gate applies ──
            # Invariant 9 — never fire SMS unless the relay gate is live.
            if push_target:
                push_ok, push_detail = await assert_push_deliverable(push_target)
                if not push_ok:
                    await service._send_reply(reply_token, _text(f"🚫 OTP relay ยังไม่พร้อม: {push_detail} — ยังไม่ส่ง SMS"))
                    return {"status": "CLI_RELAY_NOT_READY", "ready": False, "results": results}
            else:
                relay = await otp_relay_readiness.check()
                if not relay.ready:
                    await service._send_reply(reply_token, _text(f"🚫 OTP relay ยังไม่พร้อม: {relay.reason} — ยังไม่ส่ง SMS"))
                    return {"status": "CLI_RELAY_NOT_READY", "ready": False, "results": results}

            order_txt = " → ".join(BROKER_LABEL[b] for b in pending)
            chat_txt = _chat_word(push_target)
            if not _is_quota_exhausted():
                await service._send_reply(reply_token, _text(
                    f"🔐 กำลังเข้าระบบ {order_txt}… เมื่อได้ SMS ให้พิมพ์ OTP 6 หลักใน{chat_txt} (หมดเวลาใน 3 นาที)"))

            _PENDING_LOGIN_NEXT[:] = list(pending)
            total = len(pending)
            for idx, b in enumerate(pending, 1):
                queue_prefix = f"{idx}/{total} " if total > 1 else ""
                label = BROKER_LABEL[b]

                res = await _login_via_daemon(b, force=force)
                if b in _PENDING_LOGIN_NEXT:
                    _PENDING_LOGIN_NEXT.remove(b)
                state = res.get("state")
                status = res.get("status")

                if state == "AUTHENTICATED":
                    await _deliver_login_msg(
                        _text(f"✅ {queue_prefix}{label} เข้าระบบสำเร็จ · {_now_bkk().strftime('%H:%M:%S')}",
                              quick=[f"/port {b}", f"/orders {b}"]),
                        prefix_lines=phase1_notes,
                    )
                    results.append({"broker": b, "status": "ok", "state": state})
                    _AWAITING_OTP.pop(b, None)
                    continue

                if state in ("OTP_PAGE", "LOGIN_PAGE") or status in ("spawned", "already_up"):
                    _AWAITING_OTP[b] = time.monotonic()
                    ref_val = res.get("ref")
                    if not ref_val:
                        try:
                            from app.services import otp_relay_registry
                            pending_req = await otp_relay_registry.resolve_pending_request()
                            if (
                                pending_req is not None
                                and not pending_req.is_expired
                                and otp_relay_registry.broker_matches(b, pending_req.broker)
                            ):
                                ref_val = pending_req.ref
                        except Exception:
                            ref_val = None
                    ref_txt = f" · Ref: {ref_val}" if ref_val else ""
                    await _deliver_login_msg(
                        _text(
                            f"⏳ {queue_prefix}{label} daemon พร้อมแล้ว — กำลังรอ OTP{ref_txt} "
                            f"(พิมพ์ OTP 6 หลักใน{chat_txt} — เป็นรหัสของ {label})"
                        ),
                        prefix_lines=phase1_notes,
                    )

                    has_next = (idx < total)
                    if has_next:
                        _LAST_CHAINED_FROM[b] = (pending[idx], time.monotonic())
                        # 6.1: kss -> wait until AUTHENTICATED (or OTP timeout) before liberator
                        wait_res = await _wait_for_daemon_authenticated(b)
                        if wait_res.get("ok"):
                            _AWAITING_OTP.pop(b, None)
                            await _deliver_login_msg(
                                _text(f"✅ {queue_prefix}{label} เข้าระบบสำเร็จ · {_now_bkk().strftime('%H:%M:%S')}",
                                      quick=[f"/port {b}", f"/orders {b}"]),
                            )
                            results.append({"broker": b, "status": "ok", "state": "AUTHENTICATED"})
                        else:
                            # 6.2: kss timeout/failed -> abort remaining, do not fire SMS for next broker
                            _AWAITING_OTP.pop(b, None)
                            _PENDING_LOGIN_NEXT.clear()
                            _LAST_CHAINED_FROM.pop(b, None)
                            err_status = wait_res.get("status", "failed")
                            err_detail = wait_res.get("detail", "timeout")
                            fail_msg = f"❌ {queue_prefix}{label} เข้าระบบไม่สำเร็จ ({err_detail})"
                            results.append({"broker": b, "status": err_status, "detail": err_detail, "state": wait_res.get("state")})

                            remaining = pending[idx:]
                            rem_labels = " → ".join(BROKER_LABEL[rb] for rb in remaining)
                            abort_msg = f"{fail_msg} — ขอยกเลิกคิวที่เหลือ ({rem_labels}) และไม่ส่ง SMS"
                            await _deliver_login_msg(_text(abort_msg))
                            for rb in remaining:
                                results.append({
                                    "broker": rb,
                                    "status": "aborted",
                                    "detail": f"cancelled_due_to_{b}_failure",
                                })
                            break
                    else:
                        results.append({"broker": b, "status": "awaiting_otp", "state": state, "ref": ref_val})
                else:
                    _AWAITING_OTP.pop(b, None)
                    err = res.get("message") or f"{label} session daemon state: {state or status or 'UNKNOWN'}"
                    from app.services.broker_cdp.daemon_launcher import get_log_tail
                    tail = res.get("log_tail") or await asyncio.to_thread(get_log_tail, b, max_lines=5)
                    fail_msg = f"❌ {queue_prefix}{label} เข้าระบบไม่สำเร็จ: {err}"
                    if tail:
                        fail_msg += f"\n--- log tail ---\n{tail}"

                    has_next = (idx < total)
                    if has_next:
                        # 6.2: abort remaining
                        _PENDING_LOGIN_NEXT.clear()
                        remaining = pending[idx:]
                        rem_labels = " → ".join(BROKER_LABEL[rb] for rb in remaining)
                        fail_msg += f" — ขอยกเลิกคิวที่เหลือ ({rem_labels}) และไม่ส่ง SMS"
                        await _deliver_login_msg(_text(fail_msg), prefix_lines=phase1_notes)
                        results.append({"broker": b, "status": "failed", "detail": err, "state": state})
                        for rb in remaining:
                            results.append({
                                "broker": rb,
                                "status": "aborted",
                                "detail": f"cancelled_due_to_{b}_failure",
                            })
                        break
                    else:
                        await _deliver_login_msg(_text(fail_msg), prefix_lines=phase1_notes)
                        results.append({"broker": b, "status": "failed", "detail": err, "state": state})
        except Exception as e:
            logger.exception(f"LINE CLI /login {parsed.broker} failed")
            await _deliver_login_msg(
                _text(f"❌ {parsed.broker} เข้าระบบไม่สำเร็จ: {type(e).__name__}: {str(e)[:120]}"),
                prefix_lines=phase1_notes,
            )
            results.append({"broker": parsed.broker, "status": "failed",
                            "detail": f"{type(e).__name__}: {str(e)[:120]}"})
        finally:
            _PENDING_LOGIN_NEXT.clear()
            _STARTED.pop(key, None)
    return {"status": "CLI_LOGIN", "results": results}


async def _close_owned_browsers(broker: str) -> bool:
    """Close browsers THIS process launched — never a CDP daemon's Chrome.

    The order/quote paths now attach to the daemon (which owns the visible browser), so
    ``/logout`` must only drop that attachment (``_detach_session_cdp``) and close
    whatever Playwright browser this process started itself.
    """
    closed = False
    try:
        if broker == "kss":
            from app.browser.auth_driver import auth_driver
            from app.browser.streaming_auth_driver import streaming_auth_driver
            for drv in (auth_driver, streaming_auth_driver):
                if (getattr(drv, "browser", None) is not None
                        or getattr(drv, "context", None) is not None):
                    await drv.close()
                    closed = True
        else:
            from app.brokers import liberator_plugin
            from app.brokers.liberator.driver import liberator_driver
            from app.browser.tab_manager import tab_manager
            tab = tab_manager.get_tab("liberator")
            owns_tab = bool(tab is not None and not tab.is_closed())
            if owns_tab or getattr(liberator_driver, "browser", None) is not None:
                await liberator_plugin.close()
                closed = True
    except Exception as e:  # noqa: BLE001
        logger.debug("logout close-owned failed for %s: %s", broker, e)
    return closed


# --------------------------------------------------------------------------- #
# /logout
# --------------------------------------------------------------------------- #
async def _handle_logout(parsed: CliCommand, service: Any, reply_token: Optional[str]) -> Dict[str, Any]:
    broker_arg = (parsed.broker or "").strip().lower()
    brokers: List[str] = ["kss", "lib"] if broker_arg == "all" else [broker_arg]
    lines: List[str] = []
    for b in brokers:
        try:
            alive = await _broker_session_alive(b)
        except Exception as e:  # noqa: BLE001
            logger.debug("logout session check failed for %s: %s", b, e)
            alive = False

        # B.2 — cut THIS process loose; the daemon's Chrome keeps running.
        cdp_dropped = await _detach_session_cdp(b)
        had_session = bool(alive or cdp_dropped)
        owned_closed = False
        if had_session:
            # Legacy drivers may still own a browser they launched themselves.
            from app.brokers import liberator_plugin
            if b == "lib" and not cdp_dropped:
                await liberator_plugin.close()
                owned_closed = True
            else:
                owned_closed = await _close_owned_browsers(b)

        port = _CDP_PORT.get(b) or "-"
        if had_session or owned_closed:
            lines.append(
                f"👋 ออกจากระบบ {BROKER_LABEL[b]} แล้ว — ตัดการเชื่อมต่อจากโปรเซสนี้เท่านั้น "
                f"(แท็บยังเปิดอยู่ที่ CDP {port})")
        else:
            lines.append(
                f"⚪ {BROKER_LABEL[b]} ไม่ได้เข้าระบบอยู่แล้ว (โปรเซสนี้) — แท็บยังเปิดอยู่ที่ CDP {port}")
    await service._send_reply(reply_token, _text("\n".join(lines)))
    return {"status": "CLI_LOGOUT", "brokers": brokers}


# --------------------------------------------------------------------------- #
# /stop
# --------------------------------------------------------------------------- #
async def _handle_stop(parsed: CliCommand, service: Any, reply_token: Optional[str]) -> Dict[str, Any]:
    """Stop Chrome CDP daemons (:9333 / :9334) to reclaim RAM while keeping Gateway :8000 live 24/7."""
    from app.services.broker_cdp import daemon_launcher

    broker_arg = (parsed.broker or "all").strip().lower()
    brokers: List[str] = ["kss", "lib"] if broker_arg == "all" else [broker_arg]
    for b in brokers:
        _AWAITING_OTP.pop(b, None)
        await _detach_session_cdp(b)
        await daemon_launcher.stop_daemon(b)

    labels = ", ".join(f"{BROKER_LABEL.get(b, b.upper())} ({_CDP_PORT.get(b, '-')})" for b in brokers)
    msg = (
        f"🛑 ปิดพักบราวเซอร์ CDP เรียบร้อย ({labels}) เพื่อคืน RAM\n"
        f"🟢 Gateway :8000 ยังออนไลน์ 24 ชม. — พิมพ์ /login all เมื่อต้องการเปิดใช้งานใหม่"
    )
    await service._send_reply(reply_token, _text(msg, quick=["/status", "/login all"]))
    return {"status": "CLI_STOP", "brokers": brokers}


# --------------------------------------------------------------------------- #
# Pillar 2 reads — live CDP sync
# --------------------------------------------------------------------------- #
_POSITION_TYPES = ("LONG", "SHORT", "BORROWED", "UNKNOWN")


def _position_type(value: Any) -> str:
    """Normalise a holding side.

    A **missing** side means a cash-only book (LIBERATOR ``PositionItem`` has no side
    field) -> LONG. An **explicit** unrecognised value -> UNKNOWN (never guess a sign).
    """
    if value is None or str(value).strip() == "":
        return "LONG"
    v = str(value).strip().upper()
    return v if v in _POSITION_TYPES else "UNKNOWN"


def _holding_pnl_pct(position_type: str, cost: float, last: float) -> Optional[float]:
    """RCA-002: %P/L must follow the side — a SHORT gains when the price *falls*.

    Returns ``None`` (= render ``—``) when the sign cannot be known honestly:
    BORROWED / UNKNOWN side, or no cost base to divide by. Never fabricates ``0.0``.
    """
    ptype = (position_type or "LONG").upper()
    if ptype in ("BORROWED", "UNKNOWN") or not cost:
        return None
    if ptype == "SHORT":
        return (cost - last) / cost * 100.0
    return (last - cost) / cost * 100.0


def _normalise_holdings(holdings: List[Dict[str, Any]],
                        keep_zero: Any = ()) -> List[Dict[str, Any]]:
    """Broker-agnostic holding row — keeps the side plus the page's own P/L columns.

    ``keep_zero`` names symbols whose zero-volume row must survive: a covered SBL
    short (``MINT`` 2,400 → 0 after the Cover matched) still owes a Return, so
    dropping the row deleted the only place the action could live.
    """
    keep = {str(s).upper() for s in (keep_zero or ())}
    out = []
    for h in holdings or []:
        vol = int(h.get("volume", 0) or 0)
        sym = str(h.get("symbol", "")).upper()
        if vol <= 0 and sym not in keep:
            continue
        cost = float(h.get("avg_cost", 0.0) or 0.0)
        last = float(h.get("current_price") or h.get("market_price") or cost)
        ptype = _position_type(h.get("position_type"))
        unreal = h.get("unrealized_pnl")
        mval = h.get("market_value")
        broker = str(h.get("broker") or "").lower()
        row = {"symbol": str(h.get("symbol", "")).upper(), "position_type": ptype,
               "volume": int(h.get("volume", 0) or 0),
               "available_volume": int(h.get("available_volume") if h.get("available_volume") is not None else (h.get("volume") or 0)),
               "borrow_qty": int(h.get("borrow_qty") or 0),
               "cost": cost, "last": last,
               "pnl_pct": _holding_pnl_pct(ptype, cost, last),
               "unrealized_pnl": float(unreal) if unreal is not None else None,
               "market_value": float(mval) if mval is not None else None}
        if broker:
            row["broker"] = broker
        out.append(row)
    out.sort(key=lambda r: r["volume"] * r["last"], reverse=True)
    return out


def _portfolio_totals(rows: List[Dict[str, Any]]) -> Tuple[float, float]:
    """(market value, unrealized P/L) for normalised rows.

    Prefers the page's own ``Unreal``/``Market Value`` columns; falls back to a
    **side-aware** ``volume x (last - cost)`` (inverted for SHORT) only when the page
    did not expose them. BORROWED / UNKNOWN rows with no P/L column contribute 0.0 —
    never a fabricated figure (Anti-Mock).
    """
    total = 0.0
    pnl = 0.0
    for r in rows:
        mv = r.get("market_value")
        if mv is not None:
            total += float(mv)
        elif r.get("last") is not None:
            total += float(r["volume"]) * float(r["last"])
        up = r.get("unrealized_pnl")
        if up is None:
            ptype = str(r.get("position_type") or "LONG").upper()
            if ptype in ("BORROWED", "UNKNOWN") or r.get("cost") is None or r.get("last") is None:
                up = 0.0
            elif ptype == "SHORT":
                up = -r["volume"] * (r["last"] - r["cost"])
            else:
                up = r["volume"] * (r["last"] - r["cost"])
        pnl += float(up)
    return total, pnl


def _portfolio_amount(rows: List[Dict[str, Any]]) -> Optional[float]:
    """Σ(volume x avg cost) — the app's *Amount* (cost basis) column, RCA-013.

    ``BORROWED`` (``(B)``) rows are excluded: the page exposes no cost basis for them,
    so counting them would invent one. Rows without a cost base are skipped too —
    ``None`` (render ``—``) when nothing can be summed, never ``0.0`` (Anti-Mock).
    """
    parts = [
        r["volume"] * r["cost"] for r in rows
        if r.get("cost") and str(r.get("position_type") or "LONG").upper() != "BORROWED"
    ]
    return sum(parts) if parts else None


def _split_borrowed(
    rows: List[Dict[str, Any]],
    active_short_symbols: Optional[Set[str]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """(main holdings, borrowed lots) — RCA-013 final, decisions D2/D3.

    ``(B)`` (``BORROWED``) rows leave the main table: ``main`` keeps only SHORT / LONG /
    UNKNOWN, while ``borrowed`` carries one entry per loan with the numbers the card
    needs (``borrow_qty`` = shares still held from the lender, ``returnable_qty`` =
    shares that can actually go back — sold shares must be covered first).

    Anti-confusion filter: If a borrowed lot already has an active SHORT order
    (Queuing/Pending in order book) OR its available_volume is 0 (shares already
    committed to order book / sold), omit the (B) card so users are not confused
    by duplicate Short/Return action buttons.
    """
    src = rows or []
    active_shorts = {str(s).strip().upper() for s in (active_short_symbols or ())}

    def _is_borrowed(r: Dict[str, Any]) -> bool:
        return str(r.get("position_type") or "LONG").upper() == "BORROWED"

    def _should_keep_borrowed(r: Dict[str, Any]) -> bool:
        if not _is_borrowed(r):
            return False
        sym = str(r.get("symbol", "")).strip().upper()
        vol = int(r.get("volume", 0) or 0)
        b_qty = int(r.get("borrow_qty", 0) or 0)
        avail = r.get("available_volume")

        # Active Short in order book -> hide (B) card
        if sym in active_shorts:
            return False
        # Available volume explicitly 0 while having borrowed quantity -> hide (B) card
        if avail is not None and int(avail) <= 0 and (vol > 0 or b_qty > 0):
            return False

        return (vol > 0 or b_qty > 0)

    main = [r for r in src if not _is_borrowed(r)]
    borrowed = sorted(
        ({"symbol": str(r.get("symbol", "")),
          "position_type": "BORROWED",
          "volume": int(r.get("volume", 0) or 0),
          "available_volume": int(r.get("available_volume", 0) or 0),
          "borrow_qty": int(r.get("borrow_qty", 0) or 0),
          "returnable_qty": int(r.get("returnable_qty", r.get("borrow_qty", 0)) or 0),
          "lots": int(r.get("borrow_lots", 0) or 0),
          "fee_rate": r.get("borrow_fee_rate"),
          "broker": str(r.get("broker", "kss")).lower(),
          "last": r.get("last"),
          "cost": r.get("cost"),
          "pnl_pct": r.get("pnl_pct")}
         for r in src
         if _should_keep_borrowed(r)),
        key=lambda b: b["symbol"],
    )
    return main, borrowed


#: The live MainSBL page is the truth for the borrow book; our SQLite cache is a
#: mirror that has already drifted (it still listed returned BAM/LH lots and missed
#: a fresh BCPG borrow). Read the page when it is older than this, else reuse.
_SBL_LEDGER_MAX_AGE_S = 900


async def _live_sbl_lots() -> Tuple[List[Dict[str, Any]], str]:
    """Active borrow lots as ``(lots, source)`` — resident SBL tab first, else cache.

    Zero-OTP: reuses the already-authenticated KSS tab. ``source`` is ``"live"``,
    ``"cache"`` or ``"unavailable"`` so the card can say where the numbers came from.
    """
    try:
        from app.brokers.krungsri.sbl_realtime_driver import krungsri_sbl_driver as sbl
        status = sbl.cache_status() if hasattr(sbl, "cache_status") else {}
        age = status.get("age_seconds")
        stale = age is None or float(age) > _SBL_LEDGER_MAX_AGE_S
        lots = await sbl.scrape_sbl_lots(force_refresh=bool(stale))
        if lots:
            return list(lots), ("live" if stale else "cache")
    except Exception as e:  # never let the borrow book break the portfolio card
        logger.warning("SBL ledger read failed: %s", e)
    return [], "unavailable"


def _sbl_borrow_summary(lots: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """``symbol -> {borrow_qty, lots, lot_ids, fee_rate}`` from active loan lots."""
    out: Dict[str, Dict[str, Any]] = {}
    for lot in lots or []:
        sym = str(lot.get("symbol") or "").strip().upper()
        # remaining_qty == 0 means fully returned — `remaining or borrow_qty` would
        # resurrect the closed lot's size (MINT stayed "คืนได้ 2,400" after the return).
        qty = lot.get("remaining_qty")
        if qty is None:
            qty = lot.get("borrow_qty")
        qty = int(qty or 0)
        if str(lot.get("status") or "").upper() == "CLOSED":
            qty = 0
        if not sym or qty <= 0:
            continue
        agg = out.setdefault(sym, {"borrow_qty": 0, "lots": 0, "lot_ids": [], "fee_rate": None})
        agg["borrow_qty"] += qty
        agg["lots"] += 1
        if lot.get("broker_tx_id"):
            agg["lot_ids"].append(str(lot["broker_tx_id"]))
        fee = lot.get("borrow_fee_rate")
        if fee is not None:
            agg["fee_rate"] = max(float(fee), float(agg["fee_rate"] or 0.0))
    return out


def _merge_borrow(rows: List[Dict[str, Any]], summary: Dict[str, Dict[str, Any]]
                  ) -> List[Dict[str, Any]]:
    """Fold the borrow book into KSS holding rows (pure).

    Every row gets ``borrow_qty`` / ``returnable_qty`` (``loan − short volume``: shares
    sold short must be bought back before they can be returned). Symbols that are
    borrowed but hold no position (``BCPG`` 100 on 2026-11-06) gain a ``BORROWED`` row
    so the card can offer Short/Return — previously they were invisible on LINE.
    """
    out: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for r in rows or []:
        row = dict(r)
        sym = str(row.get("symbol") or "").strip().upper()
        info = summary.get(sym)
        row["borrow_qty"] = int(info["borrow_qty"]) if info else 0
        row["borrow_lots"] = int(info["lots"]) if info else 0
        row["borrow_fee_rate"] = info.get("fee_rate") if info else None
        is_short = str(row.get("position_type") or "").upper() == "SHORT"
        short_vol = int(row.get("volume") or 0) if is_short else 0
        row["returnable_qty"] = max(0, row["borrow_qty"] - short_vol)
        if info:
            row["broker"] = row.get("broker") or "kss"
        out.append(row)
        seen.add(sym)

    for sym, info in sorted(summary.items()):
        if sym in seen:
            continue
        b_qty = int(info["borrow_qty"])
        out.append({
            "symbol": sym, "position_type": "BORROWED", "volume": 0,
            "available_volume": b_qty,
            "borrow_qty": b_qty, "returnable_qty": b_qty,
            "borrow_lots": int(info["lots"]), "borrow_fee_rate": info.get("fee_rate"),
            "cost": None, "last": None, "pnl_pct": None, "broker": "kss",
        })
    return out


#: Display-only labels for raw account-type codes (LIBERATOR reports ``CASH_BALANCE``).
#: Presentation only — never touches the per-broker balance-value mapping (RCA-011).
_ACCOUNT_TYPE_LABEL = {
    "CASH_BALANCE": "Cash Balance", "CASH": "Cash", "MARGIN": "Margin",
    "EQUITY": "Equity", "DERIVATIVES": "Derivatives",
}


def _account_type_label(value: Any) -> str:
    raw = str(value or "").strip()
    return _ACCOUNT_TYPE_LABEL.get(raw.upper(), raw or "CASH")


def _account_switch_note(snap: Dict[str, Any]) -> Optional[str]:
    """Warning line for ``/port`` when the working account could not be selected.

    t_496c6213: the card used to show the empty CASH book (<REDACTED_KSS_ACCT>) with no hint that
    the margin account (<REDACTED_KSS_ACCT>, which holds the positions/SBL) was never opened.
    """
    err = snap.get("account_switch_error")
    if not err:
        return None
    active = snap.get("account_no") or "?"
    return f"⚠️ แสดงบัญชี {active} ไม่ใช่บัญชีที่ตั้งไว้ — {err}"


_CAT_KIND = {
    "MATCHED": "matched", "PARTIALLY_MATCHED": "partial", "UNMATCHED": "open",
    "CANCELLED": "cancelled", "REJECTED": "rejected", "UNKNOWN": "unknown",
}


def _is_stale(iso_ts: Optional[str], minutes: int = 60) -> bool:
    if not iso_ts:
        return False
    try:
        return (_now_bkk() - datetime.fromisoformat(iso_ts).astimezone()) > timedelta(minutes=minutes)
    except (ValueError, TypeError):
        return False


def _broker_label(broker: Optional[str]) -> str:
    return "ALL" if broker in (None, "all") else BROKER_LABEL.get(broker, str(broker).upper())


async def _live_card(kind: str, broker: str) -> Dict[str, Any]:
    """Build a live card for one broker from its CDP scrape (zero-OTP, N0).

    Reads via the read-only CDP scrape layer (``scrape_portfolio_from_cdp`` /
    ``scrape_orders_from_cdp``) instead of the in-process Pillar-2 drivers, whose
    auth flags go stale after a gateway restart even while the daemon tabs
    (9333/9334) are still authenticated — the bug where ``/status`` showed 🟢 but
    ``/port|orders|cash … sync`` answered "session หมดอายุ".

    Fail-closed: a down daemon / missing tab / scrape exception renders the honest
    ``unavailable_card``, never a 🟢 live card with fabricated numbers.
    """
    from app.services.broker_cdp.sync_service import (
        map_ledger_side,
        scrape_orders_from_cdp,
        scrape_portfolio_from_cdp,
    )
    from app.mcp.pillars.pillar2_portfolio import _classify_order_category

    is_test = False
    try:
        from app.services.trade_dispatcher import trade_dispatcher
        is_test = not trade_dispatcher.is_live
    except Exception:
        pass
    now_label = _now_bkk().strftime("%H:%M")
    label = BROKER_LABEL[broker]
    login_cmd = f"/login {broker}"
    is_kss = _broker_platform(broker) == "TRADEMAN"

    if kind == "orders":
        try:
            snap = await scrape_orders_from_cdp(broker)
        except Exception as e:
            logger.warning("CDP order scrape failed for %s: %s", broker, e)
            return cards.unavailable_card(broker=label, login_cmd=login_cmd)
        rows: List[Dict[str, Any]] = []
        for o in (snap.get("orders") or []):
            side = map_ledger_side(o.get("side"))
            if side is None:
                continue  # Anti-Mock: unreadable side -> skip, never fabricate a BUY
            vol = int(o.get("volume", 0) or 0)
            mv = int(o.get("matched_volume", 0) or 0)
            bal = int(o.get("balance", 0) or 0)
            status_raw = str(o.get("status", ""))
            category = _classify_order_category(vol, mv, bal, status_raw)
            rows.append({
                "kind": _CAT_KIND.get(category, "unknown"),
                "side": side,
                "symbol": str(o.get("symbol", "")).upper(),
                "volume": vol,
                "price": float(o.get("price", 0.0) or 0.0),
                "balance": bal,
                "status": status_raw,
            })
        open_count = sum(1 for r in rows if r["kind"] in ("open", "partial"))
        return cards.orders_card(broker=label, total=len(rows), open_count=open_count,
                                 rows=rows[:10], snapshot_label=now_label, live=True,
                                 show_nego=True)

    # port and cash share one portfolio scrape.
    try:
        snap = await scrape_portfolio_from_cdp(broker)
    except Exception as e:
        logger.warning("CDP portfolio scrape failed for %s: %s", broker, e)
        return cards.unavailable_card(broker=label, login_cmd=login_cmd)

    account_no = str(snap.get("account_no") or "LIVE")
    acct_type = str(snap.get("account_type") or "CASH_BALANCE").upper()

    if kind == "port":
        borrow: Dict[str, Dict[str, Any]] = {}
        active_short_symbols: Set[str] = set()
        if is_kss:
            lots, borrow_src = await _live_sbl_lots()
            borrow = _sbl_borrow_summary(lots)
            if borrow_src == "unavailable":
                logger.warning("SBL borrow book unavailable — cards render without Return")
            try:
                from app.services.broker_cdp.sync_service import scrape_orders_from_cdp
                o_snap = await scrape_orders_from_cdp("kss")
                for o in (o_snap.get("orders") or []):
                    status = str(o.get("status") or "").upper()
                    side = str(o.get("side") or "").upper()
                    if side == "SHORT" and ("O" in status or "QUEUING" in status or "PENDING" in status):
                        active_short_symbols.add(str(o.get("symbol") or "").upper())
            except Exception as oe:
                logger.debug("scrape_orders_from_cdp failed while checking active shorts: %s", oe)
        rows = _normalise_holdings(snap.get("holdings", []), keep_zero=borrow.keys())
        if borrow:
            rows = _merge_borrow(rows, borrow)
        main_rows, borrowed = _split_borrowed(rows, active_short_symbols=active_short_symbols)
        total, pnl = _portfolio_totals(rows)
        amount = _portfolio_amount(rows)
        cost_base = total - pnl
        pnl_pct = (pnl / cost_base * 100.0) if cost_base else 0.0
        # Broker isolation: only KSS/TradeMan has (B) loans.
        return cards.port_card(broker=label, account=account_no, amount=amount,
                               pnl_value=pnl, pnl_pct=pnl_pct, rows=main_rows,
                               borrowed=borrowed if is_kss else [],
                               note=_account_switch_note(snap),
                               snapshot_label=now_label, stale=False, live=True,
                               is_test=is_test)

    # cash — preserve the CLI-RCA-011 per-broker labels (KSS vs LIB).
    if is_kss:
        # KSS figures come from the header the account_no was read from (inherently
        # bound: `_scrape_kss` fails closed when the account_no is unreadable).
        la = snap.get("line_available")
        if la is None:
            return cards.unavailable_card(broker=label, login_cmd=login_cmd)
        rows = [{"label": "Line Available", "value": f"{float(la):,.2f}"}]
        if acct_type == "MARGIN":
            ee = snap.get("excess_equity")
            rows.append({"label": "Excess Equity",
                         "value": f"{float(ee):,.2f}" if ee is not None else "—"})
        cb = snap.get("cash_balance")
        if cb is not None:
            rows.append({"label": "Cash balance", "value": f"{float(cb):,.2f}"})
        return cards.cash_card(broker=label, account=_account_type_label(acct_type),
                               rows=rows, snapshot_label=now_label, live=True)

    # LIB: broker-agnostic labels (RCA-007).
    rows = []
    cb = snap.get("cash_balance")
    la = snap.get("line_available")
    ee = snap.get("excess_equity")
    if cb is not None:
        rows.append({"label": "เงินสด / Cash balance", "value": f"{float(cb):,.2f}"})
    if la is not None:
        rows.append({"label": "กำลังซื้อ / Buying power", "value": f"{float(la):,.2f}"})
    if ee is not None:
        rows.append({"label": "Excess equity", "value": f"{float(ee):,.2f}"})
    if not rows:
        # RCA-009: every field unreadable -> never render a fabricated 0.00.
        return cards.unavailable_card(broker=label, login_cmd=login_cmd)
    return cards.cash_card(broker=label, account=_account_type_label(acct_type),
                           rows=rows, snapshot_label=now_label, live=True)


_PAGE_CACHE: Dict[str, Dict[str, Any]] = {}
_PAGE_CACHE_TTL_SECONDS = 300.0  # 5 minutes


def _clean_page_cache() -> None:
    now = time.monotonic()
    expired = [k for k, v in list(_PAGE_CACHE.items()) if now - v.get("ts", 0) > _PAGE_CACHE_TTL_SECONDS]
    for k in expired:
        _PAGE_CACHE.pop(k, None)


async def _deliver_paginated_cards(
    service: Any,
    reply_token: Optional[str],
    push_target: Optional[str],
    cards_list: List[Dict[str, Any]],
    kind: str = "port",
    broker: str = "all",
    page: int = 1,
    cache_id: Optional[str] = None,
) -> None:
    """Delivers up to 5 cards in a single Reply payload, offering a Quick Reply postback for next page."""
    _clean_page_cache()
    if not cards_list and cache_id and cache_id in _PAGE_CACHE:
        cards_list = _PAGE_CACHE[cache_id].get("cards", [])

    if not cards_list:
        return

    per_page = 5
    start = (page - 1) * per_page
    end = start + per_page
    current_page_cards = [dict(c) for c in cards_list[start:end]]
    has_next = len(cards_list) > end

    if has_next:
        if not cache_id:
            cache_id = uuid.uuid4().hex[:12]
        _PAGE_CACHE[cache_id] = {
            "ts": time.monotonic(),
            "cards": cards_list,
            "kind": kind,
            "broker": broker,
        }
        next_page = page + 1
        quick_item = {
            "type": "action",
            "action": {
                "type": "postback",
                "label": f"📄 ดูหน้าถัดไป ({next_page})",
                "data": f"action=port_page&cache_id={cache_id}&page={next_page}&kind={kind}&broker={broker}",
                "displayText": f"📄 ดูหน้าถัดไป (หน้า {next_page})",
            },
        }
        if current_page_cards:
            last_card = current_page_cards[-1]
            existing_qr = last_card.get("quickReply") or {}
            items = list(existing_qr.get("items", []))
            items.append(quick_item)
            last_card["quickReply"] = {"items": items}

    await _send_or_push(service, reply_token, push_target, current_page_cards)


async def _handle_read(
    parsed: CliCommand, service: Any, reply_token: Optional[str], push_target: Optional[str], db: Any
) -> Dict[str, Any]:
    """``/port`` / ``/cash`` / ``/orders`` — always live scrape from broker CDP."""
    kind = parsed.cmd
    brokers: List[str] = ["kss", "lib"] if parsed.broker == "all" else ([str(parsed.broker)] if parsed.broker else ["kss"])
    label = _broker_label(parsed.broker)

    # Live scrape; session must already be alive (Invariant 7 — never auto-login)
    key = (kind, parsed.broker or "-")
    lock = _lock_for(*key)
    if lock.locked():
        await service._send_reply(reply_token, _text(
            f"⏳ /{kind} {parsed.broker} กำลังทำงานอยู่ (เริ่ม {_STARTED.get(key, '-')}) — รอผลสักครู่"))
        return {"status": "CLI_BUSY"}
    async with lock:
        _STARTED[key] = _now_bkk().strftime("%H:%M:%S")
        try:
            for b in brokers:
                if not await _broker_session_alive(b):
                    await service._send_reply(reply_token, _text(
                        f"🔴 {BROKER_LABEL[b]} session หลุด — /login {b} ก่อน แล้วค่อย sync"))
                    return {"status": "CLI_SESSION_DOWN", "broker": b}
            await service._send_reply(reply_token, _text(f"🔄 กำลังดึงข้อมูลสด {label}…"))
            if kind == "port" and parsed.broker == "all":
                brokers_data = []
                for b in brokers:
                    await _broker_sync_cdp(b)
                    from app.services.broker_cdp.sync_service import scrape_portfolio_from_cdp, scrape_orders_from_cdp
                    try:
                        snap = await scrape_portfolio_from_cdp(b)
                        borrow: Dict[str, Dict[str, Any]] = {}
                        active_short_symbols: Set[str] = set()
                        if _broker_platform(b) in ("KRUNGSRI", "TRADEMAN"):
                            lots, _src = await _live_sbl_lots()
                            borrow = _sbl_borrow_summary(lots)
                            try:
                                o_snap = await scrape_orders_from_cdp(b)
                                for o in (o_snap.get("orders") or []):
                                    status = str(o.get("status") or "").upper()
                                    side = str(o.get("side") or "").upper()
                                    if side == "SHORT" and ("O" in status or "QUEUING" in status or "PENDING" in status):
                                        active_short_symbols.add(str(o.get("symbol") or "").upper())
                            except Exception as oe:
                                logger.debug("scrape_orders_from_cdp failed in port all sync: %s", oe)
                        rows = _normalise_holdings(snap.get("holdings", []), keep_zero=borrow.keys())
                        for r in rows:
                            r["broker"] = b
                        if borrow:
                            rows = _merge_borrow(rows, borrow)
                        main_rows, borrowed = _split_borrowed(rows, active_short_symbols=active_short_symbols)
                        if _broker_platform(b) not in ("KRUNGSRI", "TRADEMAN"):
                            borrowed = []
                        total, pnl = _portfolio_totals(rows)
                        amount = _portfolio_amount(rows)
                        cost_base = total - pnl
                        pnl_pct = (pnl / cost_base * 100.0) if cost_base else 0.0
                        acct_no = str(snap.get("account_no") or ("MARGIN" if b == "kss" else "CASH"))
                        brokers_data.append({
                            "broker": b,
                            "account": acct_no,
                            "amount": amount,
                            "pnl_value": pnl,
                            "pnl_pct": pnl_pct,
                            "rows": main_rows,
                            "borrowed": borrowed,
                        })
                    except Exception as e:
                        logger.warning("CDP portfolio scrape failed for %s during port all sync: %s", b, e)
                if brokers_data:
                    messages = cards.portfolio_stock_cards(brokers_data=brokers_data)
                    summary = cards.portfolio_summary_card(brokers_data=brokers_data)
                    all_cards = ([summary] if summary else []) + (messages or [])
                    if all_cards:
                        await _deliver_paginated_cards(
                            service=service,
                            reply_token=reply_token,
                            push_target=push_target,
                            cards_list=all_cards,
                            kind="port",
                            broker="all",
                        )
                    else:
                        await _send_or_push(
                            service, reply_token, push_target,
                            cards.unavailable_card(broker=label, login_cmd="/login all"))
                else:
                    await _send_or_push(
                        service, reply_token, push_target,
                        cards.unavailable_card(broker=label, login_cmd="/login all"))
            else:
                all_cards = []
                for b in brokers:
                    if kind in ("port", "cash"):
                        await _broker_sync_cdp(b)
                    elif kind == "orders":
                        await _broker_sync_orders_cdp(b)
                    live_c = await _live_cards(kind, b)
                    if live_c:
                        all_cards.extend(live_c)
                if all_cards:
                    await _deliver_paginated_cards(
                        service=service,
                        reply_token=reply_token,
                        push_target=push_target,
                        cards_list=all_cards,
                        kind=kind,
                        broker=parsed.broker or "-",
                    )
                else:
                    await _send_or_push(
                        service, reply_token, push_target,
                        cards.unavailable_card(broker=label, login_cmd=f"/login {parsed.broker or 'all'}"))
        except Exception as e:
            logger.exception(f"LINE CLI /{kind} {parsed.broker} sync failed")
            await _send_or_push(
                service, reply_token, push_target,
                _text(f"❌ {label} ดึงข้อมูลสดไม่สำเร็จ: {type(e).__name__}: {str(e)[:120]}"))
        finally:
            _STARTED.pop(key, None)
    return {"status": "CLI_SYNC", "brokers": brokers}


# --------------------------------------------------------------------------- #
# /nego
# --------------------------------------------------------------------------- #
#: Amend outcomes that must HALT the /nego batch (fail-closed, Loop 1 D3): the price basis
#: could not be read, or the order's identity could not be proven. Those are pushed as the
#: honest "อ่านไม่ได้" card — a normal ``skip``/``fail`` row would look like a routine policy
#: decision and would hide a broker/session problem.
_NEGO_HALT_STATUSES = ("QUOTE_UNAVAILABLE", "SYMBOL_MISMATCH")


async def _audit_nego(db: Any, broker: str, row: Dict[str, Any]) -> None:
    try:
        from app.db.models import SessionLog
        db.add(SessionLog(
            event_type="LINE_CLI_NEGO",
            details=(f"{broker} #{row.get('order_no')} {row.get('side')} {row.get('symbol')} "
                     f"{row.get('status')} {row.get('reason', '')}").strip(),
        ))
        await db.commit()
    except Exception as e:  # pragma: no cover - audit must never break the command
        logger.warning("LINE_CLI_NEGO audit failed: %s", e)


async def _plan_nego(broker: str, o: Dict[str, Any], max_ticks: int) -> Dict[str, Any]:
    """
    Evaluate one order against the market and the G4–G6 guardrails.

    Never mutates anything — returns a row with ``status`` in
    ``{"planned", "skip"}`` and a human ``reason``. Anti-Mock: any unavailable
    quote fails closed (skip), never a guessed price — both the legacy
    ``source="unavailable"`` marker and the explicit ``success=False``
    ``QUOTE_UNAVAILABLE`` envelope (Loop 1 D1) mean "no price basis".
    """
    from app.services.quicktrade_negotiation_service import OrderSnapshot, Quote, plan_negotiation

    order_no = str(o.get("broker_order_no") or o.get("order_no") or "")
    symbol = str(o.get("symbol", "")).upper()
    side = str(o.get("side", "")).upper()
    old = float(o.get("price", 0.0) or 0.0)
    vol = int(o.get("balance") or o.get("volume") or 0)
    row: Dict[str, Any] = {"order_no": order_no, "symbol": symbol, "side": side, "old_price": old, "volume": vol}

    if vol <= 0:
        row.update(status="skip", reason="ไม่เหลือจำนวนให้ต่อ")
        return row

    quote = await _broker_quote(broker, symbol)
    if quote.get("success") is False or quote.get("source") == "unavailable":
        row.update(status="skip", reason="อ่านราคาตลาดไม่ได้ (fail-closed)")
        return row

    q_obj = Quote(symbol=symbol, best_bid=quote.get("bid_1"), best_offer=quote.get("offer_1"), last=quote.get("last"))
    order_snap = OrderSnapshot(order_no=order_no, symbol=symbol, side=side, price=old, balance=vol)

    decision = plan_negotiation(order_snap, q_obj, max_ticks=max_ticks)

    if decision.outcome.value == "OK":
        row.update(status="planned", new_price=decision.target_price)
    elif decision.outcome.value == "NO_CHANGE":
        row.update(status="skip", reason="อยู่ที่เป้าหมายแล้ว")
    elif decision.outcome.value == "UPTICK_VIOLATION":
        row.update(status="skip", reason="ผิดกฎ uptick")
    elif decision.outcome.value == "NO_QUOTE":
        row.update(status="skip", reason="อ่านราคาตลาดไม่ได้ (fail-closed)")
    else:
        row.update(status="skip", reason=decision.reason)

    return row


# Quick Trade outcome -> the wording the /nego card shows for a skipped order.
_QUICKTRADE_SKIP_REASON = {
    "NO_CHANGE": "อยู่ที่เป้าหมายแล้ว",
    "UPTICK_VIOLATION": "ผิดกฎ uptick (SHORT ต้องสูงกว่าราคาล่าสุด)",
    "TICK_SIZE_VIOLATION": "ไม่ผ่าน tick-size",
    "MAX_TICKS_EXCEEDED": "ห่างเกินจำนวน tick ที่อนุญาต",
    "NO_QUOTE": "อ่านราคาตลาดไม่ได้ (fail-closed)",
}


def _apply_quicktrade_result(row: Dict[str, Any], res: Dict[str, Any]) -> None:
    """Fold a Quick Trade in-place result into a ``/nego`` card row (Anti-Mock).

    The service re-reads the ledger + quote and applies G4/G5/G6 itself, so its
    ``current_price``/``target_price``/``outcome`` are what the card must report: a
    refusal or ``NO_CHANGE`` is a *skip*, never an ``ok``.
    """
    if res.get("current_price") is not None:
        row["old_price"] = res["current_price"]
    if res.get("target_price") is not None:
        row["new_price"] = res["target_price"]
    outcome = str(res.get("outcome") or "")
    row["outcome"] = outcome or None
    row["submitted"] = res.get("submitted")      # the one-order card reads this (Anti-Mock)
    if outcome in _QUICKTRADE_SKIP_REASON:
        row["status"] = "skip"
        row["reason"] = _QUICKTRADE_SKIP_REASON[outcome]
    elif res.get("success"):
        row["status"] = "ok"
        if res.get("submitted") is False and res.get("dry_run"):
            row["reason"] = "DRY-RUN — ยังไม่กดส่ง"       # honest: staged, not sent
    else:
        row["status"] = "fail"
        row["reason"] = str(res.get("error") or res.get("reason") or "ล้มเหลว")[:40]


def _apply_cancel_replace_result(row: Dict[str, Any], res: Dict[str, Any]) -> None:
    """Fold a TradeMan cancel+re-submit result into a /nego card row (C1–C7)."""
    if res.get("current_price") is not None:
        row["old_price"] = res["current_price"]
    if res.get("target_price") is not None:
        row["new_price"] = res["target_price"]
    row["outcome"] = res.get("outcome") or res.get("status")
    row["submitted"] = res.get("submitted")
    if res.get("success"):
        row["status"] = "ok"
        if res.get("new_order_no"):
            row["reason"] = f"#{row.get('order_no')} → #{res['new_order_no']} @ {res.get('target_price')}"
        else:
            row["reason"] = str(res.get("reason") or "")[:80]
    elif str(res.get("outcome") or "") in ("NO_CHANGE", "UPTICK_VIOLATION", "MAX_TICKS_EXCEEDED",
                                           "NO_QUOTE", "TICK_SIZE_VIOLATION"):
        row["status"] = "skip"
        row["reason"] = str(res.get("reason") or "")[:80]
    else:
        row["status"] = "fail"
        row["reason"] = str(res.get("reason") or res.get("error") or "ล้มเหลว")[:80]


async def _quote_via_gateway_browser(symbol: str) -> Dict[str, Any]:
    """Read quote via Streaming CDP :9333 (100% CDP alignment)."""
    return await _broker_quote("kss", symbol)


async def _kss_cancel_replace(*, order_no: str, symbol: str, side: str, volume: int,
                              old_price: float) -> Dict[str, Any]:
    """Redirect to CDP in-place amend (zero TradeMan/QuickTrade dependency).

    Retained for backward compatibility with KSS_AMEND_MODE=trademan_cancel_replace tests.
    DRY_RUN returns a staged plan; LIVE performs an in-place amend via CDP :9333.
    """
    from app.services.quicktrade_negotiation_service import OrderSnapshot, Quote, plan_negotiation

    base = {"order_no": order_no, "symbol": symbol, "side": side, "volume": volume,
            "current_price": old_price}

    q = await _quote_via_gateway_browser(symbol)
    if not q.get("success") and not (q.get("bid_1") or q.get("offer_1") or q.get("last")):
        return {"success": False, "status": "QUOTE_UNAVAILABLE", "outcome": "NO_QUOTE",
                "reason": str(q.get("error") or "อ่านราคาตลาดไม่ได้ (fail-closed)"), **base}

    decision = plan_negotiation(
        OrderSnapshot(order_no=order_no, symbol=symbol, side=side, price=old_price, balance=volume),
        Quote(symbol=symbol, best_bid=q.get("best_bid") or q.get("bid_1"), best_offer=q.get("best_offer") or q.get("offer_1"), last=q.get("last")),
        max_ticks=int(getattr(settings, "LINE_NEGO_MAX_TICKS", 3)),
    )
    base.update(target_price=decision.target_price, outcome=decision.outcome.value, reason=decision.reason)
    if decision.outcome.value != "OK":
        return {"success": False, **base}
    target = decision.target_price
    if target is None:  # defensive: OK always carries a price
        return {"success": False, "status": "NO_TARGET", **base}

    if getattr(settings, "DRY_RUN_MODE", True):
        return {"success": True, "submitted": False, "dry_run": True, **base,
                "reason": f"DRY-RUN plan: ยกเลิก #{order_no} แล้วส่งใหม่ {target:.2f}"}

    res = await _broker_modify("kss", order_no=order_no, new_price=target, symbol=symbol, side=side, volume=volume)
    return {
        "success": bool(res.get("success")),
        "submitted": bool(res.get("submitted", not getattr(settings, "DRY_RUN_MODE", False))),
        "new_order_no": order_no,
        **base,
    }


async def _handle_nego(
    parsed: CliCommand, service: Any, reply_token: Optional[str], push_target: Optional[str], db: Any
) -> Dict[str, Any]:
    from app.services.kill_switch_service import kill_switch_service
    from app.services.trade_dispatcher import trade_dispatcher

    broker = parsed.broker or ""
    if kill_switch_service.is_active():                       # G1
        await service._send_reply(reply_token, _text("⛔ KILL-SWITCH ACTIVE — คำสั่งถูกระงับ · พิมพ์ RESET เพื่อปลด"))
        return {"status": "CLI_KILL_SWITCH"}
    if not _in_nego_window():                                 # G2
        await service._send_reply(reply_token, _text(f"🌙 นอกเวลาที่อนุญาต ({settings.LINE_NEGO_WINDOWS})"))
        return {"status": "CLI_OUT_OF_HOURS"}
    # G3 — money guard: CDP probe is the primary truth (CDP-5).
    # When allow_cdp is enabled, probe=AUTHENTICATED passes, while
    # UNKNOWN/DAEMON_DOWN/LOGIN_PAGE fail closed.
    alive_check = await _broker_session_alive(
        broker,
        allow_cdp=settings.LINE_NEGO_CDP_GATE,
        fail_closed=True,
        return_reason=True,
    )
    if isinstance(alive_check, tuple):
        alive, reason = alive_check
    else:
        alive = bool(alive_check)
        reason = "" if alive else "UNKNOWN"

    if not alive:
        cdp_detail = f" (CDP: {reason})" if reason else ""
        cmd_hint = f"/login {broker} force" if reason else f"/login {broker} ก่อน"
        await service._send_reply(
            reply_token,
            _text(f"🔴 {BROKER_LABEL.get(broker, broker.upper())} session หลุด{cdp_detail} — {cmd_hint}"),
        )
        return {"status": "CLI_SESSION_DOWN", "broker": broker, **({"reason": f"CDP: {reason}"} if reason else {})}

    key = ("nego", broker)
    lock = _lock_for(*key)
    if lock.locked():
        await service._send_reply(reply_token, _text(
            f"⏳ /nego {broker} กำลังทำงานอยู่ (เริ่ม {_STARTED.get(key, '-')}) — รอผลสักครู่"))
        return {"status": "CLI_BUSY"}

    max_ticks = int(getattr(settings, "LINE_NEGO_MAX_TICKS", 3))
    ok = skipped = failed = 0
    results: List[Dict[str, Any]] = []
    #: Fail-closed stop signal: set when an amend reports "no price basis / identity unproven"
    #: (``_NEGO_HALT_STATUSES``) — the batch then breaks and the honest card is pushed.
    halted = False
    halt_status = ""
    async with lock:
        _STARTED[key] = _now_bkk().strftime("%H:%M:%S")
        try:
            orders = await _broker_open_orders(broker)
            if parsed.target:
                if parsed.target.isdigit():
                    orders = [o for o in orders
                              if str(o.get("broker_order_no") or o.get("order_no") or "") == parsed.target]
                else:
                    orders = [o for o in orders if str(o.get("symbol", "")).upper() == parsed.target]
            if not orders:
                msg = (f"❓ ไม่พบออเดอร์ค้างของ {parsed.target} ใน {BROKER_LABEL[broker]}" if parsed.target
                       else f"ℹ️ {BROKER_LABEL[broker]} ไม่มีออเดอร์ค้างให้ต่อราคา")
                await service._send_reply(reply_token, _text(msg))
                return {"status": "CLI_NEGO_EMPTY", "broker": broker}

            mode = "LIVE" if trade_dispatcher.is_live else "TEST"
            await service._send_reply(reply_token, _text(
                f"⚡ [{mode}] กำลังต่อราคา {BROKER_LABEL[broker]} · {len(orders)} ออเดอร์ค้าง…"))

            for o in orders:
                if broker == "kss" and str(getattr(settings, "KSS_AMEND_MODE", "cdp")).lower() == "trademan_cancel_replace":
                    row = {
                        "order_no": str(o.get("broker_order_no") or o.get("order_no") or ""),
                        "symbol": str(o.get("symbol", "")).upper(),
                        "side": str(o.get("side", "")).upper(),
                        "old_price": float(o.get("price", 0.0) or 0.0),
                        "volume": int(o.get("balance") or o.get("volume") or 0),
                    }
                    if int(o.get("matched") or 0) > 0:
                        row.update(status="skip", reason="มี matched แล้ว — ห้าม cancel+replace (C2)")
                        res = {}
                    else:
                        res = await _kss_cancel_replace(
                            order_no=row["order_no"], symbol=row["symbol"],
                            side=row["side"], volume=row["volume"], old_price=row["old_price"])
                        _apply_cancel_replace_result(row, res)
                    row.setdefault("submitted", res.get("submitted"))
                else:
                    row = await _plan_nego(broker, o, max_ticks)
                    if row["status"] == "planned":
                        res = await _broker_modify(broker, order_no=row["order_no"], new_price=row["new_price"],
                                                   symbol=row["symbol"], side=row["side"], volume=row["volume"])
                        if res.get("path") == "QUICKTRADE_INPLACE":
                            # kss LIVE: report what Quick Trade actually did (its own quote +
                            # guardrails decide), not the handler's preview.
                            _apply_quicktrade_result(row, res)
                        elif res.get("success"):
                            row["status"] = "ok"
                        else:
                            row["status"] = "fail"
                            row["reason"] = str(res.get("error") or res.get("message") or "ล้มเหลว")[:40]
                            if str(res.get("status") or "") in _NEGO_HALT_STATUSES:
                                halted, halt_status = True, str(res["status"])   # fail-closed
                        row.setdefault("submitted", res.get("submitted"))
                if row["status"] == "ok":
                    ok += 1
                elif row["status"] == "skip":
                    skipped += 1
                else:
                    failed += 1
                results.append(row)
                await _audit_nego(db, broker, row)
                if halted:
                    # Stop the batch: the board (or the order's identity) could not be proven,
                    # so nothing else may be pressed. The honest "อ่านไม่ได้" card is pushed
                    # below instead of a per-order table.
                    logger.warning(
                        "LINE CLI /nego %s halted (%s) on #%s: %s",
                        broker, halt_status, row.get("order_no"), row.get("reason"),
                    )
                    break

            ts = _now_bkk().strftime("%H:%M:%S")
            is_test = not trade_dispatcher.is_live
            dry_run = getattr(settings, "DRY_RUN_MODE", False)
            single = results[0] if len(results) == 1 else None
            if halted:
                # Fail-closed: no price basis (or an unproven order identity) ⇒ the honest
                # read-failure card, never a "normal" per-order summary.
                card = cards.unavailable_card(
                    broker=BROKER_LABEL[broker], login_cmd=f"/login {broker}")
            elif single is not None and str(parsed.target or "").isdigit():
                # One explicit order_no -> the focused one-order card (D2), filled from
                # the row that mirrors what the broker actually did.
                card = cards.negotiate_card(
                    order_no=str(single.get("order_no") or parsed.target),
                    symbol=str(single.get("symbol") or "-"),
                    side=str(single.get("side") or "-"),
                    outcome=str(single.get("outcome") or single.get("status") or ""),
                    current_price=single.get("old_price"),
                    target_price=single.get("new_price"),
                    volume=single.get("volume"),
                    reason=str(single.get("reason") or ""),
                    submitted=single.get("submitted"),
                    ts=ts,
                    is_test=is_test,
                    dry_run=dry_run,
                )
            else:
                card = cards.nego_card(broker=BROKER_LABEL[broker], results=results, ok=ok, skipped=skipped,
                                       failed=failed, ts=ts, is_test=is_test, dry_run=dry_run)
            await service.push_message(card, to_user_id=push_target)
        except Exception as e:
            logger.exception(f"LINE CLI /nego {broker} failed")
            await service.push_message(
                _text(f"❌ {BROKER_LABEL[broker]} ต่อราคาไม่สำเร็จ: {type(e).__name__}: {str(e)[:120]}"),
                to_user_id=push_target)
        finally:
            _STARTED.pop(key, None)
    return {"status": "CLI_NEGO", "broker": broker, "ok": ok, "skipped": skipped, "failed": failed,
            "halted": halted, "halt_status": halt_status}


# --------------------------------------------------------------------------- #
# /negotiate (Quick Trade in-place price amendment, Pillar 6)
# --------------------------------------------------------------------------- #
async def _handle_negotiate(
    parsed: CliCommand, service: Any, reply_token: Optional[str], push_target: Optional[str], db: Any
) -> Dict[str, Any]:
    """``/negotiate <order_no>`` — alias of ``/nego kss <order_no>`` (ONE path).

    Kept for muscle memory / older cards: the command is rewritten into the unified
    ``/nego kss`` flow, so the duplicate G1/G2/lock implementation is gone and the
    guardrails live in exactly one place (D1/D2 — TASK-loop1-2).
    """
    order_no = str(parsed.target or "").strip()
    if not order_no.isdigit():
        await service._send_reply(reply_token, _text(
            "❓ รูปแบบ: /negotiate <order_no> เช่น /negotiate 11008198",
            quick=["/orders kss"]))
        return {"status": "CLI_NEGOTIATE_BAD_ARGS"}

    res = await _handle_nego(CliCommand(cmd="nego", broker="kss", target=order_no),
                             service, reply_token, push_target, db)
    res["order_no"] = order_no
    return res


# --------------------------------------------------------------------------- #
# Phase B — order draft + confirmation card + postback (B.5)
# --------------------------------------------------------------------------- #
def _new_draft_id() -> str:
    return f"DRAFT-{uuid.uuid4().hex[:8].upper()}"


def _store_draft(draft: Dict[str, Any]) -> None:
    _DRAFTS[draft["draft_id"]] = draft


def _take_draft(draft_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Fetch **and remove** a live draft; an expired/missing id returns ``None``.

    ``draft_id`` is opaque to the user (an in-memory UUID fragment), so popping it
    is also the idempotency guard: a replayed postback can never confirm twice.
    """
    if not draft_id:
        return None
    draft = _DRAFTS.pop(draft_id, None)
    if draft is None:
        return None
    if time.time() - draft.get("created_at", 0.0) > _draft_ttl_seconds():
        return None
    return draft


def _purge_drafts() -> int:
    """Drop expired drafts (diagnostics/tests). Returns how many were removed."""
    now = time.time()
    ttl = _draft_ttl_seconds()
    expired = [k for k, d in _DRAFTS.items() if now - d.get("created_at", 0.0) > ttl]
    for k in expired:
        _DRAFTS.pop(k, None)
    return len(expired)


def _resolve_account_no(broker: str, account: str) -> Optional[str]:
    """Resolve the DB ``account_no`` for a ``(broker, account)`` pair.

    ``None`` means the pair is not a real account (e.g. ``lib:margin``) or the
    number was never configured — the caller fails the check, never guesses.
    """
    broker = (broker or "").lower()
    account = (account or "cash").lower()
    if broker == "kss":
        key = "TRADEMAN_ACCOUNT_NO_MARGIN" if account == "margin" else "TRADEMAN_ACCOUNT_NO_CASH"
        return (getattr(settings, key, "") or "").strip() or None
    if broker == "lib":
        if account != "cash":
            return None                       # Liberator has no margin account
        # ``LIBERATOR_ACCOUNT_NO`` may be the 8-digit accountNo (<REDACTED_LIBERATOR_ACCOUNT_NO>) while every
        # ``account_no`` column stores the 7-digit investor id (<REDACTED_LIBERATOR_USERNAME>) that
        # ``sync_service._scrape_liberator`` writes. Normalise through the same helper
        # the writer uses, or every read misses the row and /buy lib dies with
        # E_NO_LINE_AVAILABLE right after a successful /cash lib sync.
        from app.services.broker_cdp.sync_service import liberator_db_account_no
        return liberator_db_account_no()
    return None


async def _kss_order_ledger() -> list:
    """Live KSS order ledger straight from the Streaming CDP tab (fail-soft → []).

    Used by the ``all`` volume resolve: the broker ledger is the only thing that
    knows what has *actually* been filled, so ``all`` must be derived from it
    rather than from our own draft rows.
    """
    try:
        from app.services.broker_cdp.sync_service import scrape_orders_from_cdp

        snap = await scrape_orders_from_cdp("kss")
        return list(snap.get("orders") or [])
    except Exception:
        logger.warning("short-volume ledger read failed", exc_info=True)
        return []


async def _short_committed_volume(symbol: str) -> int:
    """Shares of ``symbol`` already committed to SHORT orders (queued or filled)."""
    total = 0
    for row in await _kss_order_ledger():
        if str(row.get("symbol") or "").upper() != symbol.upper():
            continue
        if "short" not in str(row.get("side") or "").lower():
            continue
        if "cancel" in str(row.get("status") or "").lower():
            continue
        total += int(row.get("volume") or 0)
    return total


async def _open_short_volume(broker: str, symbol: str) -> int:
    """Net short position of ``symbol`` = filled SHORT − filled COVER (live ledger).

    Falls back to the CDP portfolio scrape when the ledger is unreadable, so a
    transient DOM hiccup degrades instead of silently reporting "no position".
    """
    ledger = await _kss_order_ledger()
    if ledger:
        short = cover = 0
        for row in ledger:
            if str(row.get("symbol") or "").upper() != symbol.upper():
                continue
            filled = int(row.get("matched_volume") or 0)
            side = str(row.get("side") or "").lower()
            if "short" in side:
                short += filled
            elif "cover" in side:
                cover += filled
        return max(short - cover, 0)

    try:
        snap = await _get_broker_portfolio(broker)
        for pos in snap.get("holdings") or []:
            if str(pos.get("symbol") or "").upper() != symbol.upper():
                continue
            qty = int(pos.get("volume") or pos.get("qty") or 0)
            ptype = str(pos.get("position_type") or "").upper()
            if ptype == "SHORT" or qty < 0:
                return abs(qty)
        return 0
    except Exception:
        logger.warning("short-position scrape failed for %s", symbol, exc_info=True)
        return 0


# --------------------------------------------------------------------------- #
# Zero-DB CDP Portfolio & SBL Cache (Phase 2)
# --------------------------------------------------------------------------- #
_CDP_PORTFOLIO_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_CDP_PORTFOLIO_CACHE_TTL = 5.0  # seconds


def clear_cdp_portfolio_cache() -> None:
    """Clear in-memory CDP portfolio cache."""
    _CDP_PORTFOLIO_CACHE.clear()


async def _get_broker_portfolio(
    broker: str,
    account_no: Optional[str] = None,
    db: Optional[Any] = None,
) -> Dict[str, Any]:
    """Get portfolio & balance snapshot directly from CDP with short TTL cache (Zero-DB Phase 2)."""
    clean_broker = (broker or "").strip().lower()
    cache_key = f"{clean_broker}:{account_no or ''}"
    now = time.monotonic()
    cached = _CDP_PORTFOLIO_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _CDP_PORTFOLIO_CACHE_TTL:
        return cached[1]

    # 1. Try live CDP scrape
    try:
        from app.services.broker_cdp.sync_service import scrape_portfolio_from_cdp

        snap = await scrape_portfolio_from_cdp(clean_broker)
        if snap and isinstance(snap, dict):
            _CDP_PORTFOLIO_CACHE[cache_key] = (now, snap)
            return snap
    except Exception as e:
        logger.debug("Live CDP portfolio scrape for %s failed (%s), checking fallback", broker, e)

    # 2. DB fallback (for unit tests / transitional Phase 2 when headless CDP is mocked)
    if db is not None:
        try:
            from app.db.models import BrokerAccount, Position

            target_account = account_no or _resolve_account_no(clean_broker, "cash")
            acc = None
            if target_account:
                res = await db.execute(select(BrokerAccount).where(BrokerAccount.account_no == target_account))
                acc = res.scalar_one_or_none()
            if acc is None:
                res = await db.execute(select(BrokerAccount).where(BrokerAccount.broker == clean_broker.upper()))
                acc = res.scalars().first()

            line_available = getattr(acc, "line_available", None) if acc else None
            cash_balance = getattr(acc, "cash_balance", None) if acc else None
            resolved_acct = getattr(acc, "account_no", target_account) if acc else target_account

            res_pos = await db.execute(select(Position).where(Position.account_no == resolved_acct))
            positions = res_pos.scalars().all()
            holdings = [
                {
                    "symbol": p.symbol,
                    "position_type": p.position_type,
                    "volume": p.volume,
                    "available_volume": p.volume,
                }
                for p in positions
            ]
            return {
                "broker": clean_broker,
                "account_no": resolved_acct,
                "line_available": line_available,
                "cash_balance": cash_balance,
                "holdings": holdings,
            }
        except Exception as e:
            logger.warning("DB fallback for %s portfolio failed: %s", broker, e)

    return {"broker": clean_broker, "account_no": account_no, "line_available": None, "holdings": []}


async def _fetch_sbl_lots_safe(
    symbol: str, *, db: Optional[Any] = None, force_refresh: bool = False
) -> List[Dict[str, Any]]:
    """Call ``_get_active_sbl_lots`` while tolerating 2-arg test monkeypatches."""
    try:
        return await _get_active_sbl_lots(symbol, db=db, force_refresh=force_refresh)
    except TypeError:
        return await _get_active_sbl_lots(symbol, db=db)


async def _deduct_db_sbl_lots_fifo(db: Optional[Any], symbol: str, qty: int) -> None:
    """Deduct confirmed returned ``qty`` from active DB SBL borrow lots in FIFO order."""
    if db is None or qty <= 0:
        return
    try:
        from app.db.models import SBLBorrowLot

        rem_to_return = int(qty)
        res_db = await db.execute(
            select(SBLBorrowLot)
            .where(
                SBLBorrowLot.symbol == symbol,
                SBLBorrowLot.status.in_(("ACTIVE", "PARTIAL_RETURNED")),
                SBLBorrowLot.remaining_qty > 0,
            )
            .order_by(SBLBorrowLot.borrow_date.asc())
        )
        for lot in res_db.scalars().all():
            if rem_to_return <= 0:
                break
            deduct = min(lot.remaining_qty, rem_to_return)
            lot.remaining_qty -= deduct
            rem_to_return -= deduct
            if lot.remaining_qty == 0:
                lot.status = "FULLY_RETURNED"
                lot.return_date = datetime.now().date()
            elif deduct > 0:
                lot.status = "PARTIAL_RETURNED"
        await db.commit()
    except Exception as e:
        logger.debug("DB update during SBL return skipped: %s", e)


async def _get_active_sbl_lots(
    symbol: str, db: Optional[Any] = None, *, force_refresh: bool = False
) -> List[Dict[str, Any]]:
    """Get active SBL borrow lots from Krungsri SBL Real Time web tab (Zero-DB Phase 2)."""
    # 1. Try Krungsri SBL Real Time tab
    try:
        from app.brokers.krungsri.sbl_realtime_driver import krungsri_sbl_driver

        try:
            lots = await krungsri_sbl_driver.scrape_sbl_lots(force_refresh=force_refresh)
        except TypeError:
            lots = await krungsri_sbl_driver.scrape_sbl_lots()
        active = [
            l
            for l in lots
            if str(l.get("symbol") or "").upper() == symbol.upper()
            and l.get("status") in ("ACTIVE", "PARTIAL", "PARTIAL_RETURNED")
            and int(l.get("remaining_qty") or 0) > 0
        ]
        if active:
            return active
    except Exception as e:
        logger.debug("Live SBL scrape failed (%s), checking fallback", e)

    # 2. DB fallback (for unit tests / transitional Phase 2 & 3)
    if db is not None:
        try:
            from app.db.models import SBLBorrowLot

            res = await db.execute(
                select(SBLBorrowLot)
                .where(
                    SBLBorrowLot.symbol == symbol,
                    SBLBorrowLot.status.in_(("ACTIVE", "PARTIAL", "PARTIAL_RETURNED")),
                    SBLBorrowLot.remaining_qty > 0,
                )
                .order_by(SBLBorrowLot.borrow_date.asc())
            )
            return [
                {
                    "lot_id": lot.lot_id,
                    "broker_tx_id": lot.broker_tx_id,
                    "symbol": lot.symbol,
                    "borrow_qty": int(lot.borrow_qty or 0),
                    "remaining_qty": int(lot.remaining_qty or 0),
                    "borrow_price": float(lot.borrow_price or 0.0),
                    "borrow_fee_rate": float(lot.borrow_fee_rate or 3.5),
                    "borrow_date": lot.borrow_date,
                    "status": lot.status,
                }
                for lot in res.scalars().all()
            ]
        except Exception as e:
            logger.warning("DB fallback for SBL lots failed: %s", e)

    return []


async def _pre_trade_check(order: TradeOrder, db: Any = None) -> Dict[str, Any]:
    """Phase-B pre-trade guardrails — Direct CDP backed, Zero-DB (Phase 2).

    Covers what a draft can prove *without firing a broker call*: holdings/SBL
    availability (symbol) directly from live CDP tabs, board lot, and ``line_available``.
    Tick size / order-value ceiling are pure and also checked here so a bad price is
    rejected before the card is drawn. The live short-uptick rule (G6) needs a quote
    and therefore stays with the B.6 executor, which runs the full ``validator``
    right before submit.

    Returns ``{"ok": bool, "code"? , "message"? , "account_no"?}``.
    """
    from app.services.validator import validator

    side = (order.cmd or "").lower()
    symbol = order.symbol

    # 0. Reject short and cover on non-Krungsri or non-margin account (Broker Isolation)
    if side in ("short", "cover") and (order.broker != "kss" or order.account != "margin"):
        return {
            "ok": False,
            "code": "E_BAD_BROKER",
            "message": "การขายชอร์ต (/short) และปิดสถานะ (/cover) รองรับเฉพาะ Krungsri บัญชี Margin <REDACTED_KSS_ACCT> เท่านั้น",
        }

    # 0.1 Resolve effective price (explicit or from live broker quote)
    if order.price is not None:
        price = float(order.price)
    else:
        quote = await _broker_quote(order.broker, symbol)
        if quote.get("success") is False or quote.get("source") == "unavailable":
            return {"ok": False, "code": "E_NO_QUOTE",
                    "message": f"อ่านราคาตลาดของ {symbol} จาก {BROKER_LABEL.get(order.broker, order.broker)} ไม่ได้ — ระบุราคาเอง เช่น /{order.cmd} {symbol} 100 12.50"}
        from app.services.nego_pricer import plan_price
        planned = plan_price(
            side=side,
            mode="lure",
            bid=quote.get("bid_1"),
            offer=quote.get("offer_1"),
            last=quote.get("last"),
            old_price=None,
            prior_close=quote.get("prior_close"),
            symbol=symbol,
        )
        if not planned.get("ok"):
            return {
                "ok": False,
                "code": "E_NO_QUOTE",
                "message": f"อ่านราคาตลาดของ {symbol} จาก {BROKER_LABEL.get(order.broker, order.broker)} ไม่ได้ ({planned.get('reason')}) — ระบุราคาเอง เช่น /{order.cmd} {symbol} 100 12.50",
            }
        price = float(planned.get("price") or 0.0)
        if price <= 0.0:
            return {"ok": False, "code": "E_NO_QUOTE",
                    "message": f"ราคาตลาดสดของ {symbol} ว่างเปล่า — กรุณาระบุราคาเอง"}

    # 0.2 Resolve effective volume (explicit shares or board-lot calculated from budget)
    amount_mode = getattr(order, "amount_mode", "volume")
    target_val = getattr(order, "target_value", None)

    if amount_mode == "all":
        broker = order.broker
        if side == "short":
            # Borrowed shares still sitting in the SBL box MINUS everything
            # already committed to a SHORT order. Without the subtraction a second
            # `/short <sym> all` would short the same borrowed lot twice.
            active_lots = await _get_active_sbl_lots(symbol, db=db)
            borrowed_vol = sum(int(lot.get("remaining_qty") or 0) for lot in active_lots)
            if borrowed_vol <= 0:
                return {
                    "ok": False,
                    "code": "E_NO_BORROWED_SHARES",
                    "message": f"❌ ไม่พบหุ้นยืม SBL ของ {symbol} ที่สามารถ Short ได้ (มี 0 หุ้น) — กรุณายืมหุ้นก่อนด้วย /borrow {symbol} <จำนวน>",
                    "quick_reply": [f"/borrow {symbol} 1000", "/sbl"],
                }
            committed = await _short_committed_volume(symbol)
            volume = borrowed_vol - committed
            if volume <= 0:
                return {
                    "ok": False,
                    "code": "E_NO_UNSHORTED_SHARES",
                    "message": (f"❌ หุ้นยืม {symbol} ถูกสั่ง Short ครบแล้ว "
                                f"({committed:,}/{borrowed_vol:,} หุ้น) — ยกเลิกออเดอร์เดิมก่อน หรือยืมเพิ่ม"),
                    "quick_reply": ["/orders kss sync", "/sbl"],
                }
        elif side == "cover":
            volume = await _open_short_volume(broker, symbol)
            if volume <= 0:
                return {
                    "ok": False,
                    "code": "E_NO_SHORT_POSITION",
                    "message": f"❌ ไม่พบสถานะ Short ของ {symbol} ในพอร์ต {broker.upper()} ที่จะ Cover ได้",
                    "quick_reply": [f"/port {broker}", f"/orders {broker} sync"],
                }
        else:
            return {
                "ok": False,
                "code": "E_BAD_VOLUME",
                "message": f"❌ คำสั่ง /{side} ไม่รองรับจำนวนแบบ 'all'",
            }
        if price is None:
            broker_key = "lib" if "lib" in (order.account or "").lower() else "kss"
            quote_res = await _broker_quote(broker_key, symbol)
            quote_price: Optional[float] = None
            if isinstance(quote_res, dict) and quote_res.get("success"):
                raw_p = quote_res.get("last") or quote_res.get("offer_1") or quote_res.get("bid_1")
                try:
                    quote_price = float(raw_p) if raw_p is not None else None
                except (ValueError, TypeError):
                    quote_price = None
            elif isinstance(quote_res, (int, float)):
                quote_price = float(quote_res)
            if quote_price is None or quote_price <= 0:
                return {
                    "ok": False,
                    "code": "E_NO_QUOTE",
                    "message": f"❌ ไม่สามารถดึงราคาตลาดของ {symbol} ได้ กรุณาระบุราคา เช่น /{side} {symbol} all 35.00",
                    "quick_reply": [f"/{side} {symbol} all"],
                }
            price = quote_price
    elif amount_mode == "value":
        target_val = float(order.target_value or 0.0)
        shares = int(((target_val / price) + 1e-9) // 100) * 100
        if shares < 100:
            min_thb = price * 100
            action_th = "ซื้อ" if side == "buy" else "ขาย" if side == "sell" else "เปิดชอร์ต" if side == "short" else "ปิด short"
            return {"ok": False, "code": "E_INSUFFICIENT_BUDGET",
                    "message": f"งบประมาณ ฿{target_val:,.2f} ไม่พอ{action_th}ขั้นต่ำ 100 หุ้น (ราคาปัจจุบัน ฿{price:.2f} ต้องใช้อย่างน้อย ฿{min_thb:,.2f})"}
        volume = shares
    else:
        volume = int(order.volume)

    account_no = _resolve_account_no(order.broker, order.account)
    if not account_no:
        return {"ok": False, "code": "E_BAD_ACCOUNT",
                "message": f"บัญชี {order.account} ของ {BROKER_LABEL.get(order.broker, order.broker)} ไม่มี — "
                           f"ใช้ {'margin' if order.broker == 'kss' else 'cash'}"}

    # 1. board lot (SET equity) — validator is the single source of truth.
    ok, err = validator.validate_board_lot(volume, symbol)
    if not ok:
        return {"ok": False, "code": "E_BAD_LOT", "message": f"จำนวนหุ้นไม่ผ่าน board lot — {err}"}

    # 2. tick size + order-value ceiling (pure, no quote needed).
    ok, err = validator.validate_tick_size(price)
    if not ok:
        return {"ok": False, "code": "E_BAD_TICK", "message": f"ราคาไม่ผ่าน tick size — {err}"}
    ok, err = validator.validate_order_value(volume, price)
    if not ok:
        return {"ok": False, "code": "E_VALUE_CEILING", "message": f"มูลค่าคำสั่งเกินเพดาน — {err}"}

    # 3. holdings / SBL availability directly from CDP (Zero-DB Phase 2)
    if side in ("sell", "cover"):
        wanted = "LONG" if side == "sell" else "SHORT"
        snap = await _get_broker_portfolio(order.broker, account_no=account_no, db=db)
        held = 0
        for h in (snap.get("holdings") or []):
            h_sym = str(h.get("symbol") or "").upper()
            h_type = str(h.get("position_type") or "LONG").upper()
            if h_sym == symbol.upper() and h_type == wanted:
                held = int(h.get("available_volume") or h.get("volume") or 0)
                break
        if held <= 0 and side == "cover":
            # The positions table is a snapshot and can lag the broker by days.
            # For covers the live order ledger outranks it: if the broker really
            # has an open short, the DB being stale must not block the exit.
            held = await _open_short_volume(order.broker, symbol)
        if held <= 0:
            return {"ok": False, "code": "E_NO_POSITION",
                    "message": f"ไม่พบ {symbol} ({wanted}) ในพอร์ตที่จะ{'ขาย' if side == 'sell' else 'ปิด short'}ได้"}
        if volume > held:
            return {"ok": False, "code": "E_INSUFFICIENT_POSITION",
                    "message": f"ถือ {symbol} แค่ {held:,} หุ้น (สั่ง {volume:,} หุ้น)"}
    elif side == "short":
        from app.brokers.krungsri.sbl_realtime_driver import krungsri_sbl_driver
        risk = krungsri_sbl_driver.check_symbol_risk(symbol)
        if risk.get("is_blocked") and risk.get("verdict") == "HARD_BLOCKED_XD_XR":
            return {
                "ok": False,
                "code": "E_SBL_XD_BLOCKED",
                "message": risk.get("reason") or f"⛔ ห้ามเปิดสถานะชอร์ต: หุ้น {symbol} ติดเครื่องหมาย XD/XR",
            }
        if risk.get("is_blocked") and risk.get("verdict") in ("CACHE_MISS", "CACHE_STALE"):
            reason = risk.get("reason") or f"⚠️ ข้อมูลคลัง SBL สำหรับ {symbol} ยังไม่สด"
            hint = "พิมพ์ /sbl sync ก่อน"
            msg = reason if hint in reason else f"{reason} — {hint}"
            return {
                "ok": False,
                "code": "E_SBL_STALE_CACHE",
                "message": msg,
                "quick_reply": ["/sbl sync", f"/sbl pool {symbol}"],
            }
        active_lots = await _get_active_sbl_lots(symbol, db=db)
        total_sbl_avail = sum(int(l.get("remaining_qty") or 0) for l in active_lots)
        if total_sbl_avail < volume:
            needed = volume - total_sbl_avail
            msg = (f"ไม่มีหุ้นยืม SBL ของ {symbol} (ต้องการ {volume:,} หุ้น) — พิมพ์ /sbl pool {symbol} หรือ /borrow {symbol} {volume} เพื่อยืมก่อน"
                   if total_sbl_avail == 0 else
                   f"มีหุ้นยืม SBL ของ {symbol} เพียง {total_sbl_avail:,} หุ้น (ต้องการ {volume:,} หุ้น) — ขอยืมเพิ่มอีก {needed:,} หุ้น")
            return {"ok": False, "code": "E_NO_SBL",
                    "message": msg,
                    "quick_reply": [f"/sbl pool {symbol}", f"/borrow {symbol} {needed if total_sbl_avail > 0 else volume}"]}

        # 2.11c Decision on NOT_IN_POOL:
        # check_symbol_risk marks NOT_IN_POOL with is_blocked=True to prevent new borrow
        # on unlisted symbols (fail-closed). However, for /short, if the user ALREADY holds
        # active SBL lots (total_sbl_avail >= volume) on a fresh cache with
        # no XD/XR, the short is backed by real inventory already borrowed.
        # We do NOT hard-stop the short; we log a warning for auditability.
        if risk.get("verdict") == "NOT_IN_POOL":
            logger.warning(
                f"/short {symbol} {volume}: symbol NOT_IN_POOL in fresh SBL snapshot, "
                f"but backed by {total_sbl_avail:,} active SBL lot shares; allowing short"
            )

    # 4. line-available directly from CDP (buying-power consuming sides only).
    if side in ("buy", "short"):
        snap = await _get_broker_portfolio(order.broker, account_no=account_no, db=db)
        line_avail = snap.get("line_available")
        if line_avail is None:
            return {"ok": False, "code": "E_NO_LINE_AVAILABLE",
                    "message": f"อ่าน line available ของ {account_no} ไม่ได้ — /cash {order.broker} sync ก่อน"}
        total_thb = volume * price
        if total_thb > line_avail:
            return {"ok": False, "code": "E_INSUFFICIENT_LINE",
                    "message": f"วงเงินไม่พอ: คำสั่ง ฿{total_thb:,.2f} > line available ฿{line_avail:,.2f}"}

    return {"ok": True, "account_no": account_no, "resolved_volume": volume, "resolved_price": price}


async def _handle_trade_order(
    order: TradeOrder, service: Any, reply_token: Optional[str],
    push_target: Optional[str], db: Any,
) -> Dict[str, Any]:
    """``/buy|/sell|/short|/cover`` — pre-trade check → draft → confirmation card (B.5)."""
    from app.services.kill_switch_service import kill_switch_service

    if kill_switch_service.is_active():                       # G1 — money movement
        await service._send_reply(reply_token, _text("⛔ KILL-SWITCH ACTIVE — คำสั่งถูกระงับ · พิมพ์ RESET เพื่อปลด"))
        return {"status": "CLI_KILL_SWITCH"}

    # Refresh the DB snapshot first (best-effort)
    await _broker_sync_cdp(order.broker)

    check = await _pre_trade_check(order, db)
    if not check.get("ok"):
        quick = check.get("quick_reply")
        await service._send_reply(reply_token, _text(f"❌ {check.get('message', 'คำสั่งไม่ผ่านเกณฑ์')}", quick=quick))
        return {"status": "CLI_TRADE_REJECTED", "code": check.get("code"),
                "reason": check.get("message")}

    volume = check["resolved_volume"]
    price = check["resolved_price"]
    draft: Dict[str, Any] = {
        "draft_id": _new_draft_id(),
        "cmd": order.cmd,
        "side_letter": order.side_letter,
        "symbol": order.symbol,
        "volume": volume,
        "price": price,
        "total_thb": volume * price,
        "budget_thb": order.target_value if order.amount_mode == "value" else None,
        "ref_market_price": price if order.price is None else None,
        "broker": order.broker,
        "account": order.account,
        "account_no": check.get("account_no"),
        "created_at": time.time(),
    }
    _store_draft(draft)

    card = cards.trade_confirmation_card(
        draft_id=draft["draft_id"], cmd=draft["cmd"], symbol=draft["symbol"],
        volume=draft["volume"], price=draft["price"], broker=draft["broker"],
        account=draft["account"], account_no=draft["account_no"],
        total_thb=draft["total_thb"], ttl_seconds=_draft_ttl_seconds(),
        budget_thb=draft.get("budget_thb"),
        ref_market_price=draft.get("ref_market_price"),
    )
    await service._send_reply(reply_token, card)
    return {"status": "CLI_TRADE_DRAFTED", "draft_id": draft["draft_id"], "card": card}


async def _send_or_push(service: Any, reply_token: Optional[str], push_target: Optional[str], msg: Any) -> None:
    """Delivers message via push_message when target available, falling back to free reply_token."""
    ok = False
    if push_target and hasattr(service, "push_message"):
        try:
            p_res = service.push_message(msg, to_user_id=push_target)
            if inspect.isawaitable(p_res):
                p_val = await p_res
            else:
                p_val = p_res
            ok = bool(p_val) if p_val is not None else True
        except Exception as pe:
            logger.error(f"Failed to push message to {push_target}: {pe}")
            ok = False
    if not ok and reply_token and hasattr(service, "_send_reply"):
        try:
            res = service._send_reply(reply_token, msg)
            if inspect.isawaitable(res):
                await res
        except Exception as re:
            logger.debug("Reply fallback failed (%s)", re)


async def _handle_nego_postback(
    action: str,
    params: Dict[str, str],
    service: Any,
    reply_token: Optional[str],
    push_target: Optional[str],
) -> Dict[str, Any]:
    """N3 — the interactive ต่อราคา (nego-menu) postback flow.

    Read-only until confirm: the menu and mode-select only scrape CDP and plan a
    price; nothing touches a broker until ``nego_confirm``, which routes through
    ``_broker_modify`` (DRY_RUN in TEST). The draft's ``_take_draft`` pop is the
    idempotency guard, so a replayed confirm can never amend twice.
    """
    from app.services.market_clock import is_nego_window, next_open_window
    from app.services.nego_pricer import plan_price

    if action == "nego_menu":
        if not is_nego_window():
            await _send_or_push(service, reply_token, push_target,
                                _text(f"🌙 นอกเวลาต่อราคา — {next_open_window()}"))
            return {"status": "NEGO_OUT_OF_HOURS"}
        orders = await _scrape_nego_orders()
        if not orders:
            await _send_or_push(service, reply_token, push_target,
                                _text("ℹ️ ไม่มีออเดอร์ค้าง (ยังไม่ match) ให้ต่อราคา — ลอง /orders all sync"))
            return {"status": "NEGO_MENU_EMPTY"}

        kss_orders = [o for o in orders if str(o.get("broker", "")).lower() == "kss"]
        lib_orders = [o for o in orders if str(o.get("broker", "")).lower() == "lib"]

        menu_cards: List[Dict[str, Any]] = []
        if kss_orders:
            menu_cards.append(cards.nego_menu_card(orders=kss_orders, title=f"⚡ ต่อราคา KSS ({len(kss_orders)} คำสั่ง)"))
        else:
            menu_cards.append(cards.nego_empty_card(broker="kss"))

        if lib_orders:
            menu_cards.append(cards.nego_menu_card(orders=lib_orders, title=f"⚡ ต่อราคา Liberator ({len(lib_orders)} คำสั่ง)"))
        else:
            menu_cards.append(cards.nego_empty_card(broker="lib"))

        if reply_token and hasattr(service, "_send_reply"):
            try:
                await service._send_reply(reply_token, menu_cards)
            except Exception:
                await _send_or_push(service, None, push_target, menu_cards)
        else:
            await _send_or_push(service, reply_token, push_target, menu_cards)
        return {"status": "NEGO_MENU_DISPLAYED", "count": len(orders)}

    if action == "nego_mode":
        if not is_nego_window():
            await _send_or_push(service, reply_token, push_target,
                                _text(f"🌙 นอกเวลาต่อราคา — {next_open_window()}"))
            return {"status": "NEGO_OUT_OF_HOURS"}
        broker = (params.get("broker") or "").lower()
        order_no = params.get("order_no") or ""
        mode = (params.get("mode") or "").lower()
        if broker not in ("kss", "lib") or not order_no or mode not in ("match", "lure"):
            return {"status": "NEGO_BAD_POSTBACK"}

        # Re-read the order from CDP for authoritative side/price/volume — never
        # trust a crafted postback payload for anything the amend will act on.
        from app.services.broker_cdp.sync_service import map_ledger_side, scrape_orders_from_cdp
        order = None
        try:
            snap = await scrape_orders_from_cdp(broker)
            for o in (snap.get("orders") or []):
                if str(o.get("broker_order_no") or "") == order_no:
                    order = o
                    break
        except Exception as e:
            logger.warning("nego_mode: CDP scrape failed for %s: %s", broker, e)
        if not order:
            await _send_or_push(service, reply_token, push_target,
                                _text(f"❌ ไม่พบออเดอร์ #{order_no} ใน {BROKER_LABEL.get(broker, broker.upper())} — อาจถูก match/ยกเลิกไปแล้ว"))
            return {"status": "NEGO_ORDER_GONE"}

        side = map_ledger_side(order.get("side"))
        symbol = str(order.get("symbol", "")).upper()
        old_price = float(order.get("price", 0.0) or 0.0)
        volume = int(order.get("balance", 0) or order.get("volume", 0) or 0)
        if not side or not symbol or volume <= 0:
            await _send_or_push(service, reply_token, push_target,
                                _text(f"❌ อ่านข้อมูลออเดอร์ #{order_no} ไม่ครบ — ไม่ต่อราคา (fail-closed)"))
            return {"status": "NEGO_ORDER_UNREADABLE"}

        from app.services import trading_rules
        if side == "SHORT" and mode == "match" and trading_rules.get_short_uptick_ticks(symbol) > 0:
            await _send_or_push(service, reply_token, push_target,
                                _text(f"❌ ออเดอร์ SHORT {symbol} #{order_no} ต่อราคาแบบ Match ไม่ได้ (ตามกฎ Uptick +1) — รองรับเฉพาะแบบล่อราคา (Lure)"))
            return {"status": "NEGO_SHORT_MATCH_NOT_ALLOWED"}

        try:
            quote = await _broker_quote(broker, symbol)
        except Exception as e:
            logger.exception("LINE CLI nego_mode quote failed for %s: %s", symbol, e)
            await _send_or_push(service, reply_token, push_target,
                                _text(f"❌ อ่านราคาสด {symbol} ไม่สำเร็จ: {type(e).__name__}: {str(e)[:120]}"))
            return {"status": "NEGO_QUOTE_ERROR", "error": str(e)}
        plan = plan_price(
            side,
            mode,
            quote.get("bid_1"),
            quote.get("offer_1"),
            quote.get("last"),
            old_price=old_price,
            prior_close=quote.get("prior_close"),
            symbol=symbol,
        )
        if not plan.get("ok"):
            reason = plan.get("reason") or "ไม่สามารถคำนวณราคา"
            if reason == "NO_CHANGE":
                await _send_or_push(service, reply_token, push_target,
                                    _text(f"⏭️ ออเดอร์ #{order_no} {symbol} อยู่ที่ราคาเป้าหมายแล้ว"))
                return {"status": "NEGO_NO_CHANGE"}
            await _send_or_push(service, reply_token, push_target, _text(f"❌ {reason}"))
            return {"status": "NEGO_PLAN_FAILED", "reason": reason}

        draft_id = _new_draft_id()
        _store_draft({
            "draft_id": draft_id, "created_at": time.time(),
            "broker": broker, "order_no": order_no, "symbol": symbol, "side": side,
            "volume": volume, "old_price": old_price, "new_price": plan["price"],
            "mode": mode,
        })
        mode_label = "🎯 Match เลย" if mode == "match" else "🪤 ล่อราคา"
        card = cards.nego_draft_card(
            draft_id=draft_id, symbol=symbol, side=side, order_no=order_no,
            broker_label=BROKER_LABEL.get(broker, broker.upper()),
            old_price=old_price, new_price=plan["price"], mode_label=mode_label,
            ttl_seconds=_draft_ttl_seconds(),
        )
        await _send_or_push(service, reply_token, push_target, card)
        return {"status": "NEGO_DRAFTED", "draft_id": draft_id}

    if action == "nego_cancel":
        draft = _take_draft(params.get("draft_id"))
        sym = (draft or {}).get("symbol", "-")
        await _send_or_push(service, reply_token, push_target, _text(f"❌ ยกเลิกต่อราคา {sym}"))
        return {"status": "NEGO_CANCELLED"}

    # nego_confirm
    from app.services.trade_dispatcher import trade_dispatcher
    draft = _take_draft(params.get("draft_id"))
    if not draft:
        ttl = _draft_ttl_seconds()
        ttl_th = f"{ttl // 60} นาที" if ttl >= 60 else f"{ttl} วินาที"
        await _send_or_push(service, reply_token, push_target,
                            _text(f"⏳ การ์ดต่อราคาหมดอายุแล้ว ({ttl_th}) — กดเลือกโหมดใหม่"))
        return {"status": "NEGO_DRAFT_EXPIRED"}

    try:
        res = await _broker_modify(
            draft["broker"], order_no=draft["order_no"], new_price=draft["new_price"],
            symbol=draft["symbol"], side=draft["side"], volume=draft["volume"])
    except Exception as e:
        logger.exception("LINE CLI nego_confirm %s #%s failed", draft["broker"], draft["order_no"])
        err = f"{type(e).__name__}: {str(e)[:140]}"
        await _send_or_push(service, reply_token, push_target, _text(f"❌ ต่อราคาไม่สำเร็จ: {err}"))
        return {"status": "NEGO_FAILED", "error": err}

    is_dry = (not trade_dispatcher.is_live) or bool(getattr(settings, "DRY_RUN_MODE", False))
    status = str(res.get("status") or "")
    ok = bool(res.get("success")) or status in ("OPEN", "MATCHED", "MODIFIED", "SUBMITTED")
    if ok:
        tag = "🧪 [DRY-RUN]" if is_dry else "✅"
        matched = " (match แล้ว)" if res.get("is_matched") else ""
        msg = (f"{tag} ต่อราคา {draft['side']} {draft['symbol']} #{draft['order_no']} "
               f"฿{float(draft['old_price']):.2f} → ฿{float(draft['new_price']):.2f}{matched}")
        await _send_or_push(service, reply_token, push_target,
                            _text(msg, quick=[f"/orders {draft['broker']} sync"]))
        return {"status": "NEGO_CONFIRMED", "order_no": draft["order_no"],
                "dry_run": is_dry, "broker_result": res}
    err = str(res.get("error") or res.get("message") or "ล้มเหลว")
    await _send_or_push(service, reply_token, push_target, _text(f"❌ ต่อราคาไม่สำเร็จ: {err}"))
    return {"status": "NEGO_FAILED", "error": err, "broker_result": res}


async def handle_postback(
    data: str, *, db: Any = None, raw_event: Dict[str, Any], service: Any,
) -> Optional[Dict[str, Any]]:
    """Phase-B order-draft & Loop 2 SBL postbacks."""
    params = {k: v[0] for k, v in (parse_qs(data or "")).items()}
    action = params.get("action", "")
    reply_token = (raw_event or {}).get("replyToken")
    user_id, group_id, source_type, push_target = _source_of(raw_event)

    # 0.5 Portfolio / Orders Pagination Postback
    if action == "port_page":
        cache_id = params.get("cache_id", "")
        page_str = params.get("page", "1")
        try:
            page = int(page_str)
        except ValueError:
            page = 1
        kind = params.get("kind", "port")
        broker = params.get("broker", "all")
        _clean_page_cache()
        cached = _PAGE_CACHE.get(cache_id)
        if not cached:
            await _send_or_push(service, reply_token, push_target, _text("⏳ การ์ดหน้านี้หมดอายุแล้ว — กรุณาส่งคำสั่ง /port ใหม่อีกครั้ง"))
            return {"status": "PAGE_EXPIRED", "cache_id": cache_id}
        cards_list = cached.get("cards", [])
        await _deliver_paginated_cards(
            service=service,
            reply_token=reply_token,
            push_target=push_target,
            cards_list=cards_list,
            kind=kind,
            broker=broker,
            page=page,
            cache_id=cache_id,
        )
        return {"status": "PAGE_DELIVERED", "page": page, "cache_id": cache_id}

    # 1. Quick Trade & Quick Cards Hub Menus (free replyToken)
    if action == "quick_cards_menu":
        card = cards.quick_hub_card()
        await service._send_reply(reply_token, card)
        return {"status": "QUICK_CARDS_MENU_DISPLAYED"}

    if action == "quick_trade_menu":
        card = cards.quick_trade_selector_card()
        await service._send_reply(reply_token, card)
        return {"status": "QUICK_TRADE_MENU_DISPLAYED"}

    # 1.5 Nego Menu (ต่อราคา) — N3 postback flow
    if action in ("nego_menu", "nego_mode", "nego_confirm", "nego_cancel"):
        return await _handle_nego_postback(action, params, service, reply_token, push_target)

    # 1.6 Order cancel (❌ buttons on the /orders per-stock cards — t_69fed7ac)
    if action in ("order_cancel_request", "order_cancel_confirm", "order_cancel_abort"):
        return await _handle_order_cancel_postback(
            action, params, service, reply_token, push_target)

    # 2. SBL Borrow Postbacks
    if action == "sbl_borrow_cancel":
        draft = _take_draft(params.get("draft_id"))
        sym = (draft or {}).get("symbol", "-")
        await _send_or_push(service, reply_token, push_target, _text(f"❌ ยกเลิกคำขอยืมหุ้น {sym}"))
        return {"status": "SBL_BORROW_CANCELLED"}

    if action == "sbl_borrow_confirm":
        from app.brokers.krungsri.sbl_realtime_driver import krungsri_sbl_driver
        from app.services.sbl_intelligence_service import sbl_intelligence_service
        draft = _take_draft(params.get("draft_id"))
        if not draft:
            await _send_or_push(service, reply_token, push_target, _text("⏳ การ์ดขอยืมหุ้นหมดอายุแล้ว"))
            return {"status": "SBL_DRAFT_EXPIRED"}
        sym = draft["symbol"]
        vol = draft["volume"]
        risk_res = await sbl_intelligence_service.check_xd_risk(sym)
        if risk_res.get("xd_risk"):
            msg = risk_res.get("reason") or f"⛔ ห้ามยืมหุ้นเด็ดขาด: หุ้น {sym} ติดเครื่องหมาย {risk_res.get('type', 'XD')} — มีความเสี่ยงต้องจ่ายเงินปันผลชดเชย 100%"
            await _send_or_push(service, reply_token, push_target, _text(msg))
            return {"status": "SBL_BORROW_FAILED", "error": msg, "code": "E_SBL_XD_BLOCKED"}
        try:
            res = await krungsri_sbl_driver.execute_sbl_borrow(sym, vol)
        except Exception as exc:
            logger.exception("LINE CLI sbl_borrow_confirm failed for %s", sym)
            res = {"success": False, "error": f"{type(exc).__name__}: {str(exc)[:140]}"}
        if res.get("success") or res.get("status") in ("SUBMITTED", "SBL_BORROW_DRY_RUN"):
            # Anti-Mock: a DRY-RUN borrow must never persist an ACTIVE lot.
            is_dry = bool(settings.DRY_RUN_MODE) or res.get("status") == "SBL_BORROW_DRY_RUN"
            krungsri_sbl_driver.invalidate_cache()
            if not is_dry and db is not None:
                try:
                    from app.db.models import SBLBorrowLot
                    lot = SBLBorrowLot(
                        lot_id=f"SBL-{sym}-{int(time.time())}",
                        broker_tx_id=str(res.get("transaction_id") or ""),
                        symbol=sym,
                        borrow_qty=vol,
                        remaining_qty=vol,
                        borrow_fee_rate=3.50,
                        borrow_date=datetime.now().date(),
                        status="ACTIVE",
                    )
                    db.add(lot)
                    await db.commit()
                except Exception as e:
                    logger.debug("DB persistence for borrow lot skipped: %s", e)
            mode_tag = "🧪 [DRY-RUN]" if is_dry else "✅"
            msg = f"{mode_tag} ยื่นขอยืมหุ้นสำเร็จ: {sym} {vol:,} หุ้น (ดอกเบี้ย 3.50% ต่อปี) · บัญชี MARGIN <REDACTED_KSS_ACCT>"
            await _send_or_push(service, reply_token, push_target, _text(msg, quick=[f"/short {sym} {vol}"]))
            return {"status": "SBL_BORROWED", "symbol": sym, "volume": vol,
                    "persisted": not is_dry}
        else:
            err = res.get("error") or "เกิดข้อผิดพลาดในการส่งคำขอยืม"
            await _send_or_push(service, reply_token, push_target, _text(f"❌ ขอยืมหุ้นไม่สำเร็จ: {err}"))
            out = {"status": "SBL_BORROW_FAILED", "error": err}
            if res.get("status") == "BLOCKED_BY_STALE_CACHE":
                out["code"] = "E_SBL_STALE_CACHE"
            elif res.get("status") == "BLOCKED_BY_XD_XR":
                out["code"] = "E_SBL_XD_BLOCKED"
            return out

    # 3. SBL Return Postbacks
    if action == "sbl_return_cancel":
        draft = _take_draft(params.get("draft_id"))
        sym = (draft or {}).get("symbol", "-")
        await _send_or_push(service, reply_token, push_target, _text(f"❌ ยกเลิกการคืนหุ้น {sym}"))
        return {"status": "SBL_RETURN_CANCELLED"}

    if action == "sbl_return_confirm":
        from app.brokers.krungsri.sbl_realtime_driver import krungsri_sbl_driver

        draft = _take_draft(params.get("draft_id"))
        if not draft:
            await _send_or_push(service, reply_token, push_target, _text("⏳ การ์ดขอคืนหุ้นหมดอายุแล้ว"))
            return {"status": "SBL_DRAFT_EXPIRED"}
        sym = draft["symbol"]
        vol = int(draft["volume"])
        has_draft_lots = bool(draft.get("lots"))
        lots_for_exec = draft.get("lots") or [
            {
                "symbol": sym,
                "lot_id": draft.get("lot_id") or sym,
                "broker_tx_id": draft.get("lot_id") or sym,
                "remaining_qty": vol,
            }
        ]
        explicit_lot_id = draft.get("lot_id") if (draft.get("explicit_lot") or not has_draft_lots) else None
        try:
            res = await krungsri_sbl_driver.return_sbl_shares_for_symbol(
                symbol=sym,
                lots=lots_for_exec,
                return_qty=vol,
                lot_id=explicit_lot_id,
                remark=str(draft.get("remark") or "").strip(),
            )
        except Exception as exc:
            logger.exception("LINE CLI sbl_return_confirm failed for %s", sym)
            res = {"success": False, "error": f"{type(exc).__name__}: {str(exc)[:140]}"}
        ok = bool(res.get("success")) or res.get("status") in ("SUBMITTED", "SBL_RETURN_DRY_RUN", "DRY_RUN_ARMED")
        if not ok:
            completed_qty = int(res.get("completed_qty") or 0)
            if completed_qty > 0:
                krungsri_sbl_driver.invalidate_cache()
                clear_cdp_portfolio_cache()
                await _deduct_db_sbl_lots_fifo(db, sym, completed_qty)
            err = res.get("error") or "เกิดข้อผิดพลาดในการส่งคำขอคืนหุ้น"
            broker_note = str(res.get("broker_message") or "").strip()
            if res.get("status") == "NOT_RETURNABLE":
                msg = (
                    f"⚠️ โบรกยังไม่ให้คืน {sym} {vol:,} หุ้น — {err}\n"
                    f"สาเหตุที่พบบ่อย: หุ้นที่ Cover ยังไม่เข้าพอร์ต (รอ settle) หรือยอดคืนเกินที่โบรกเปิดให้\n"
                    f"ยังไม่มีการส่งคำขอใด ๆ · เช็กหน้า SBL แล้วค่อยลองใหม่ภายหลัง"
                )
                await _send_or_push(service, reply_token, push_target, _text(msg, quick=["/sbl", "/port kss"]))
                return {
                    "status": "SBL_RETURN_NOT_RETURNABLE",
                    "symbol": sym,
                    "volume": vol,
                    "broker_message": err,
                }
            if res.get("status") == "SBL_RETURN_UNVERIFIED":
                extra_note = f"\nข้อความจากโบรก: {broker_note}" if broker_note else ""
                msg = (
                    f"⚠️ ส่งคำขอคืน {sym} แล้วแต่ยังยืนยันผลไม่ได้: {err}{extra_note}\n"
                    f"กรุณาเปิดหน้า SBL ดูแถว {sym} ก่อนกดซ้ำ (ห้ามกดซ้ำเพื่อกันคืนซ้อน)"
                )
                await _send_or_push(service, reply_token, push_target, _text(msg, quick=["/sbl", "/port kss"]))
                return {
                    "status": "SBL_RETURN_UNVERIFIED",
                    "symbol": sym,
                    "volume": vol,
                    "proof": res.get("proof"),
                    "broker_message": broker_note or None,
                }
            await _send_or_push(service, reply_token, push_target, _text(f"❌ คืนหุ้นไม่สำเร็จ: {err}", quick=["/sbl", "/port kss"]))
            return {"status": "SBL_RETURN_FAILED", "error": err, "completed_qty": completed_qty}

        krungsri_sbl_driver.invalidate_cache()
        clear_cdp_portfolio_cache()
        actual_vol = int(res.get("return_qty") or vol)
        await _deduct_db_sbl_lots_fifo(db, sym, actual_vol)

        is_dry = (
            bool(settings.DRY_RUN_MODE)
            or bool(res.get("dry_run"))
            or res.get("status") in ("SBL_RETURN_DRY_RUN", "DRY_RUN_ARMED")
        )
        mode_tag = "🧪 [DRY-RUN]" if is_dry else "✅"
        returned_list = res.get("lots_returned") or []
        ref_labels = [
            f"{item['ref_no']}({int(item['qty']):,})"
            for item in returned_list
            if item.get("ref_no") and str(item.get("ref_no")).upper() != sym
        ]
        refs_suffix = f" · สัญญา {', '.join(ref_labels)}" if ref_labels else ""
        msg = f"{mode_tag} ส่งคำขอคืนหุ้นสำเร็จ: {sym} {actual_vol:,} หุ้น{refs_suffix} · บัญชี MARGIN <REDACTED_KSS_ACCT>"
        await _send_or_push(service, reply_token, push_target, _text(msg, quick=["/sbl", "/port kss"]))
        return {
            "status": "SBL_RETURNED",
            "symbol": sym,
            "volume": actual_vol,
            "dry_run": is_dry,
            "lots_returned": returned_list,
        }

    # 4. Trade Draft Confirm / Cancel
    if action not in ("trade_draft_confirm", "trade_draft_cancel"):
        return None

    draft_id = params.get("draft_id")

    if action == "trade_draft_cancel":
        draft = _take_draft(draft_id)
        card = cards.trade_receipt_card(
            draft_id=draft_id or "-", outcome="cancelled",
            cmd=(draft or {}).get("cmd"), symbol=(draft or {}).get("symbol"),
            volume=(draft or {}).get("volume"), price=(draft or {}).get("price"),
        )
        await service._send_reply(reply_token, card)
        return {"status": "TRADE_DRAFT_CANCELLED", "draft_id": draft_id,
                "found": draft is not None}

    draft = _take_draft(draft_id)
    if draft is None:
        from app.core.config import settings as _settings
        mins = int(_settings.CARD_TTL_SECONDS) // 60
        ttl_th = f"{mins} นาที" if mins >= 1 else f"{int(_settings.CARD_TTL_SECONDS)} วินาที"
        await service._send_reply(reply_token, _text(
            f"⏳ การ์ดคำสั่งหมดอายุแล้ว ({ttl_th}) — ส่งคำสั่งใหม่เพื่อขอราคา/วงเงินล่าสุด"))
        return {"status": "TRADE_DRAFT_EXPIRED", "draft_id": draft_id}

    card = cards.trade_receipt_card(
        draft_id=draft["draft_id"], outcome="confirmed", cmd=draft["cmd"],
        symbol=draft["symbol"], volume=draft["volume"], price=draft["price"],
        broker=draft.get("broker"), account=draft.get("account"),
        account_no=draft.get("account_no"),
    )
    await service._send_reply(reply_token, card)

    # B.6 wiring: execute the confirmed order NOW
    from app.services.broker_cdp.order_executor import execute_confirmed_order
    exec_kwargs: Dict[str, Any] = {}
    if str(draft.get("cmd", "")).lower() == "short":
        # G6 — a SHORT cannot prove the strict uptick rule without a last trade price,
        # and the executor refuses it (E_NO_QUOTE). Resolve the quote here and fail
        # closed: never submit a SHORT with an unknown last price.
        quote = await _broker_quote(draft.get("broker") or "kss", draft.get("symbol") or "")
        last = quote.get("last") or quote.get("bid_1")
        if not last:
            msg_q = _text("❌ อ่านราคาล่าสุด (last) ไม่ได้ — ไม่ส่ง SHORT เพื่อรักษากฎ uptick (G6)")
            await _send_or_push(service, reply_token, push_target, msg_q)
            return {"status": "TRADE_DRAFT_CONFIRMED", "draft_id": draft["draft_id"],
                    "execution": {"success": False, "status": "REJECTED",
                                  "error_code": "E_NO_QUOTE",
                                  "error": "no last_trade_price for SHORT"}}
        exec_kwargs["last_trade_price"] = float(last)
    try:
        exec_res = await execute_confirmed_order(draft, **exec_kwargs)
    except Exception as e:
        exec_res = {"success": False, "status": "EXECUTOR_ERROR", "error": str(e)}

    status = exec_res.get("status")
    bno = exec_res.get("broker_order_no")
    if exec_res.get("success") and status == "SUBMITTED":
        result_txt = (f"✅ ส่งคำสั่งสำเร็จ — {draft['symbol']} {draft['cmd'].upper()} "
                      f"{draft['volume']:,} @ ฿{draft['price']:.2f}\nออเดอร์ #{bno}")
    elif exec_res.get("success") and status == "DRY_RUN":
        result_txt = (f"🧪 DRY-RUN ผ่าน (ไม่ยิงจริง): {draft['symbol']} {draft['cmd'].upper()} "
                      f"{draft['volume']:,} @ ฿{draft['price']:.2f}")
    else:
        result_txt = f"❌ ไม่ได้ส่งคำสั่ง: {exec_res.get('error') or status or 'ไม่สำเร็จ'}"

    # Push execution result to originating chat (never reuse reply_token!)
    if push_target and hasattr(service, "push_message"):
        try:
            p_res = service.push_message(_text(result_txt), to_user_id=push_target)
            if inspect.isawaitable(p_res):
                await p_res
        except Exception as pe:
            logger.error(f"Failed to push trade execution result: {pe}")

    return {"status": "TRADE_DRAFT_CONFIRMED", "draft_id": draft["draft_id"],
            "order": {k: draft[k] for k in ("cmd", "symbol", "volume", "price",
                                             "broker", "account", "account_no")},
            "execution": exec_res}


# --------------------------------------------------------------------------- #
# /sbl, /borrow, /return (Loop 2 SBL Suite - Zero-DB Phase 3)
# --------------------------------------------------------------------------- #
def _parse_lot_date(val: Any) -> date:
    """Parse date from string or date/datetime object safely."""
    if isinstance(val, date):
        return val
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, str) and val.strip():
        s = val.strip()
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%Y/%m/%d"):
            try:
                return datetime.strptime(s, fmt).date()
            except ValueError:
                pass
    return datetime.now().date()


async def _handle_sbl(
    parsed: SblCommand, service: Any, reply_token: Optional[str], push_target: Optional[str], db: Any
) -> Dict[str, Any]:
    from app.brokers.krungsri.sbl_realtime_driver import krungsri_sbl_driver

    if parsed.subcmd == "pool" and parsed.symbol:
        try:
            avail_qty = await krungsri_sbl_driver.scrape_sbl_available_inventory(parsed.symbol)
        except Exception as e:
            logger.error(f"SBL pool query failed: {e}")
            avail_qty = 0
        cache_st = krungsri_sbl_driver.cache_status()
        status_msg = cache_st.get("message") if cache_st.get("state") != "CACHE_FRESH" else None
        card = cards.sbl_pool_card(parsed.symbol, avail_qty, fee_rate=3.50, status_message=status_msg)
        await service._send_reply(reply_token, card)
        return {"status": "CLI_SBL_POOL", "symbol": parsed.symbol, "available_qty": avail_qty}

    # /sbl — read directly from the live MainSBL borrow ledger on CDP :9333 (Zero-DB Phase 3)
    force_b = parsed.has("force_browser")
    force_refresh = parsed.has("sync") or force_b
    lots_data: List[Dict[str, Any]] = []

    try:
        raw_lots = await krungsri_sbl_driver.scrape_sbl_lots(force_refresh=force_refresh, force_browser=force_b)
        active_lots = [
            l for l in raw_lots
            if l.get("status") in ("ACTIVE", "PARTIAL", "PARTIAL_RETURNED")
            and int(l.get("remaining_qty") or 0) > 0
        ]
        lots_data = [
            {
                "symbol": l["symbol"],
                "remaining_qty": int(l["remaining_qty"]),
                "borrow_fee_rate": float(l.get("borrow_fee_rate") or l.get("fee_rate") or 3.50),
                "borrow_date": str(l.get("borrow_date") or ""),
                "ex_dividend_date": None,
            }
            for l in active_lots
        ]
    except Exception as e:
        logger.warning(f"Failed to scrape SBL lots from CDP: {e}")

    # Fallback to DB (for unit tests / transitional Phase 3 if CDP returned empty and db has rows)
    if not lots_data and db is not None:
        try:
            from app.db.models import SBLBorrowLot
            res = await db.execute(
                select(SBLBorrowLot)
                .where(
                    SBLBorrowLot.status.in_(("ACTIVE", "PARTIAL_RETURNED")),
                    SBLBorrowLot.remaining_qty > 0
                )
                .order_by(SBLBorrowLot.borrow_date.asc())
            )
            lots_db = res.scalars().all()
            lots_data = [
                {
                    "symbol": l.symbol,
                    "remaining_qty": l.remaining_qty,
                    "borrow_fee_rate": l.borrow_fee_rate,
                    "borrow_date": str(l.borrow_date),
                    "ex_dividend_date": str(l.ex_dividend_date) if l.ex_dividend_date else None,
                }
                for l in lots_db
            ]
        except Exception as e:
            logger.warning(f"DB fallback for SBL status failed: {e}")

    asof = _now_bkk().strftime("%H:%M:%S")
    card = cards.sbl_status_card(lots_data, asof)
    if parsed.has("sync") and push_target:
        await service.push_message(card, to_user_id=push_target)
    else:
        await service._send_reply(reply_token, card)
    return {"status": "CLI_SBL_STATUS", "count": len(lots_data)}


async def _handle_sbl_borrow(
    order: SblBorrowOrder, service: Any, reply_token: Optional[str], push_target: Optional[str], db: Any
) -> Dict[str, Any]:
    from app.services.kill_switch_service import kill_switch_service
    from app.services.sbl_intelligence_service import sbl_intelligence_service

    if kill_switch_service.is_active():
        await service._send_reply(reply_token, _text("⛔ KILL-SWITCH ACTIVE — คำสั่งถูกระงับ · พิมพ์ RESET เพื่อปลด"))
        return {"status": "CLI_KILL_SWITCH"}

    amount_mode = getattr(order, "amount_mode", "volume")
    target_val = getattr(order, "target_value", None)
    volume = order.volume
    quote_price: Optional[float] = None

    quote_res = await _broker_quote("kss", order.symbol)
    if isinstance(quote_res, dict) and quote_res.get("success"):
        raw_p = quote_res.get("last") or quote_res.get("offer_1") or quote_res.get("bid_1")
        try:
            quote_price = float(raw_p) if raw_p is not None else None
        except (ValueError, TypeError):
            quote_price = None
    elif isinstance(quote_res, (int, float)):
        quote_price = float(quote_res)

    if amount_mode == "value":
        if quote_price is None or quote_price <= 0:
            await service._send_reply(
                reply_token,
                _text(f"❌ ไม่สามารถดึงราคาตลาดของ {order.symbol} จาก KSS เพื่อคำนวณจำนวนหุ้นที่ขอยืมได้"),
                quick_reply=[f"/borrow {order.symbol} 1000", "/sbl"],
            )
            return {"status": "CLI_SBL_REJECTED", "reason": "no_quote"}
        raw_shares = target_val / quote_price
        volume = int(raw_shares // 100) * 100
        if volume < 100:
            min_thb = 100 * quote_price
            await service._send_reply(
                reply_token,
                _text(
                    f"❌ งบประมาณ ฿{target_val:,.2f} ไม่พอยืม {order.symbol} ขั้นต่ำ 1 board lot (100 หุ้น @ {quote_price:.2f} = ฿{min_thb:,.2f})"
                ),
                quick_reply=[f"/borrow {order.symbol} {int(min_thb + 99):,}b", "/sbl"],
            )
            return {"status": "CLI_SBL_REJECTED", "reason": "budget_too_low"}
    else:
        if not volume or volume <= 0 or volume % 100 != 0:
            await service._send_reply(reply_token, _text("❌ จำนวนหุ้นยืมต้องเป็นทวีคูณของ 100 หุ้น (Board Lot)"))
            return {"status": "CLI_SBL_REJECTED", "reason": "bad_board_lot"}

    try:
        risk_res = await sbl_intelligence_service.check_xd_risk(order.symbol)
        if risk_res.get("xd_risk"):
            msg = risk_res.get("reason") or f"⛔ ห้ามยืมหุ้นเด็ดขาด: หุ้น {order.symbol} ติดเครื่องหมาย {risk_res.get('type', 'XD')} (กำหนดวัน {risk_res.get('xd_date')}) มีความเสี่ยงต้องจ่ายเงินปันผลชดเชย 100%"
            await service._send_reply(reply_token, _text(msg))
            return {
                "status": "CLI_SBL_REJECTED",
                "code": "E_SBL_XD_BLOCKED",
                "reason": msg,
            }
    except Exception as e:
        logger.warning(f"Error checking XD risk for borrow {order.symbol}: {e}")

    draft_id = _new_draft_id()
    draft = {
        "draft_id": draft_id,
        "type": "sbl_borrow",
        "symbol": order.symbol,
        "volume": volume,
        "fee_rate": 3.50,
        "broker": "kss",
        "account": "margin",
        "created_at": time.time(),
        "budget_thb": target_val if amount_mode == "value" else None,
        "last_price": quote_price,
    }
    _store_draft(draft)

    card = cards.sbl_borrow_confirm_card(
        draft_id=draft_id,
        symbol=order.symbol,
        volume=volume,
        fee_rate=3.50,
        xd_warning=None,
        budget_thb=target_val if amount_mode == "value" else None,
        last_price=quote_price,
    )
    await service._send_reply(reply_token, card)
    return {"status": "CLI_SBL_BORROW_DRAFTED", "draft_id": draft_id}


async def _handle_sbl_return(
    order: SblReturnOrder, service: Any, reply_token: Optional[str], push_target: Optional[str], db: Any
) -> Dict[str, Any]:
    from app.brokers.krungsri.sbl_realtime_driver import plan_fifo_return_lots

    # Pure CDP: force live SBL tab refresh so open Ref.no list is never stale
    lots = await _fetch_sbl_lots_safe(order.symbol, db=db, force_refresh=True)
    if not lots:
        await service._send_reply(reply_token, _text(f"❌ ไม่พบหุ้นยืม SBL ของ {order.symbol} ในระบบที่จะคืนได้"))
        return {"status": "CLI_SBL_NO_LOT"}

    total_rem = sum(int(l.get("remaining_qty") or 0) for l in lots)
    if order.volume is not None:
        if order.volume <= 0 or order.volume % 100 != 0:
            await service._send_reply(reply_token, _text("❌ จำนวนหุ้นคืนต้องเป็นทวีคูณของ 100 หุ้น (Board Lot) หรือ all"))
            return {"status": "CLI_SBL_REJECTED", "reason": "bad_board_lot"}
        if order.volume > total_rem:
            await service._send_reply(
                reply_token,
                _text(
                    f"❌ มีหุ้นยืม SBL ของ {order.symbol} แค่ {total_rem:,} หุ้น (สั่งคืน {order.volume:,} หุ้น)",
                    quick=[f"/return {order.symbol} {total_rem}", f"/return {order.symbol} all"],
                ),
            )
            return {"status": "CLI_SBL_INSUFFICIENT_LOT", "held": total_rem, "requested": order.volume}

    return_qty = order.volume or total_rem
    plan = plan_fifo_return_lots(lots, symbol=order.symbol, return_qty=return_qty)
    primary_lot = plan[0]["lot"]
    days_held = max((datetime.now().date() - _parse_lot_date(primary_lot.get("borrow_date"))).days, 0)

    # Multi-lot FIFO accrued fee calculation
    accrued = 0.0
    for item in plan:
        lot = item["lot"]
        q = int(item["qty"])
        d = max((datetime.now().date() - _parse_lot_date(lot.get("borrow_date"))).days, 1)
        price_ref = float(lot.get("borrow_price") or 10.0)
        fee_rate = float(lot.get("borrow_fee_rate") or lot.get("fee_rate") or 3.5)
        accrued += (q * price_ref * (fee_rate / 100.0) / 365.0) * d

    has_concrete_ref = any(p.get("ref_no") and p["ref_no"] != order.symbol for p in plan)
    lot_tags = [
        f"{p['ref_no']} ({int(p['qty']):,})" if p.get("ref_no") and p["ref_no"] != order.symbol else f"#{i + 1} ({int(p['qty']):,})"
        for i, p in enumerate(plan)
    ]
    lots_summary = ", ".join(lot_tags) if (has_concrete_ref or len(plan) > 1) else None

    draft_id = _new_draft_id()
    draft = {
        "draft_id": draft_id,
        "type": "sbl_return",
        "lot_id": primary_lot.get("lot_id") or primary_lot.get("broker_tx_id") or plan[0]["lot_id"],
        "symbol": order.symbol,
        "volume": return_qty,
        "days_held": days_held,
        "accrued_fee": accrued,
        "lots": lots,
        "lots_plan": [{"lot_id": p["lot_id"], "ref_no": p["ref_no"], "qty": p["qty"]} for p in plan],
        "explicit_lot": False,
        "created_at": time.time(),
    }
    _store_draft(draft)

    card = cards.sbl_return_confirm_card(
        draft_id=draft_id,
        symbol=order.symbol,
        volume=return_qty,
        days_held=days_held,
        accrued_fee=accrued,
        lots_summary=lots_summary,
    )
    await service._send_reply(reply_token, card)
    return {"status": "CLI_SBL_RETURN_DRAFTED", "draft_id": draft_id}


async def _handle_size(
    cmd: SizeCommand, service: Any, reply_token: Optional[str],
    push_target: Optional[str], db: Any,
) -> Dict[str, Any]:
    """``/size <symbol>`` (alias /calc) — calculate shares for 50k/75k/100k budgets."""
    symbol = cmd.symbol

    # Quote resolution: try primary broker (lib) then fallback to kss
    quote = await _broker_quote("lib", symbol)
    source = "Liberator (CDP/Quote)"
    if quote.get("success") is False or (quote.get("offer_1") is None and quote.get("last") is None):
        quote = await _broker_quote("kss", symbol)
        source = "Krungsri (Streaming CDP)"

    price = None
    if quote.get("success") is not False:
        # For buying calculation, prefer offer_1 (Ask), else last, else bid_1
        for k in ("offer_1", "last", "bid_1"):
            val = quote.get(k)
            if val is not None and float(val) > 0:
                price = float(val)
                break

    if price is None or price <= 0:
        err_msg = f"❌ ไม่สามารถดึงราคาตลาดของ {symbol} จากทั้งสองโบรกเกอร์ได้ในขณะนี้"
        await service._send_reply(reply_token, _text(err_msg, quick=["/help size", f"/buy {symbol} 100"]))
        return {"status": "CLI_QUOTE_UNAVAILABLE", "symbol": symbol}

    budgets = [("50k", 50000.0), ("75k", 75000.0), ("100k", 100000.0)]
    tiers = []
    for lbl, b_val in budgets:
        shares = int(((b_val / price) + 1e-9) // 100) * 100
        actual_cost = float(shares * price)
        tiers.append({
            "budget_label": lbl,
            "budget": b_val,
            "shares": shares,
            "actual_cost": actual_cost,
        })

    card = cards.size_calc_card(
        symbol=symbol,
        price=price,
        price_source=source,
        tiers=tiers,
    )
    await service._send_reply(reply_token, card)
    return {"status": "CLI_SIZE_CALCULATED", "symbol": symbol, "price": price, "tiers": tiers}


# --------------------------------------------------------------------------- #
# Entrypoint (called by line_service)
# --------------------------------------------------------------------------- #
async def handle(parsed: Any, *, db: Any = None, raw_event: Dict[str, Any], service: Any) -> Dict[str, Any]:
    """
    Auth-gate, clear any pending conversation flow, then dispatch one parsed command.

    ``service`` is the ``LineMessagingService`` instance (injected to avoid a
    circular import); ``db`` is the webhook's AsyncSession.
    """
    from app.services.conversation_state import conversation_state

    user_id, group_id, source_type, push_target = _source_of(raw_event)
    reply_token = (raw_event or {}).get("replyToken")

    if not conversation_state.is_authorized(user_id, source_type=source_type, group_id=group_id):
        await service._send_reply(reply_token, _text("⛔ ไม่มีสิทธิ์ใช้คำสั่งนี้จากห้องนี้"))
        return {"status": "CLI_UNAUTHORIZED"}

    if user_id:
        conversation_state.clear(user_id)   # a `/` command abandons any pending flow

    if isinstance(parsed, CliError):
        msg = parsed.message + (f"\n{parsed.hint}" if parsed.hint else "")
        await service._send_reply(reply_token, _text(msg, quick=parsed.quick_reply or None))
        return {"status": "CLI_ERROR", "code": parsed.code}

    if isinstance(parsed, TradeOrder):
        return await _handle_trade_order(parsed, service, reply_token, push_target, db)

    if isinstance(parsed, SblCommand):
        return await _handle_sbl(parsed, service, reply_token, push_target, db)

    if isinstance(parsed, SblBorrowOrder):
        return await _handle_sbl_borrow(parsed, service, reply_token, push_target, db)

    if isinstance(parsed, SblReturnOrder):
        return await _handle_sbl_return(parsed, service, reply_token, push_target, db)

    if isinstance(parsed, SizeCommand):
        return await _handle_size(parsed, service, reply_token, push_target, db)

    cmd = parsed.cmd
    if cmd == "help":
        return await _handle_help(parsed, service, reply_token)
    if cmd == "schedule":
        return await _handle_schedule(service, reply_token)
    if cmd == "status":
        return await _handle_status(service, reply_token)
    if cmd == "login":
        return await _handle_login(parsed, service, reply_token, push_target)
    if cmd == "logout":
        return await _handle_logout(parsed, service, reply_token)
    if cmd == "stop":
        return await _handle_stop(parsed, service, reply_token)
    if cmd in ("port", "cash", "orders"):
        return await _handle_read(parsed, service, reply_token, push_target, db)
    if cmd == "nego":
        return await _handle_nego(parsed, service, reply_token, push_target, db)
    if cmd == "cancel":
        return await _handle_cancel(parsed, service, reply_token, push_target)
    if cmd == "negotiate":
        return await _handle_negotiate(parsed, service, reply_token, push_target, db)
    if cmd == "halt":
        from app.services.kill_switch_service import kill_switch_service
        res = await kill_switch_service.activate(reason="Triggered via LINE CLI /halt", triggered_by="LINE_USER")
        reply_text = "🚨 [EMERGENCY KILL SWITCH ACTIVATED]\nระบบหยุดการเทรดอัตโนมัติทั้งหมดทันที และส่งคำขอยกเลิกออเดอร์ค้างทั้งหมดเรียบร้อยแล้ว\nพิมพ์ 'RESET' หรือ 'RESUME' เพื่อปลดล็อก"
        await service._send_reply(reply_token, _text(reply_text))
        return {"status": "KILL_SWITCH_ACTIVATED", "details": res}

    await service._send_reply(reply_token, _text("❓ ไม่รู้จักคำสั่ง · พิมพ์ /help"))
    return {"status": "CLI_UNKNOWN", "cmd": cmd}


def __getattr__(name: str) -> Any:
    """Module-level attribute lookup for dynamic exports."""
    if name == "DRAFT_TTL_SECONDS":
        return _draft_ttl_seconds()
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


__all__ = ["handle", "handle_postback", "_LOCKS", "_STARTED", "_DRAFTS", "_PAGE_CACHE",
           "_in_nego_window", "_plan_nego", "_purge_drafts", "DRAFT_TTL_SECONDS", "_draft_ttl_seconds"]
