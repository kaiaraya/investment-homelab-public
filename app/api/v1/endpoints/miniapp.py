"""LINE MiniApp (LIFF) REST API endpoints for Phase 1+ (Auth, Brokers, OTP, Kill-Switch)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, NoReturn, Optional, Union
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.events import OTPPayload, OTPSource, event_bus
from app.core.sse_broadcaster import sse_broadcaster
from app.services import account_routing, otp_relay_registry, trading_rules
from app.services.broker_cdp import daemon_launcher, trading_facade
from app.services.broker_cdp.sync_service import (
    get_active_kss_account,
    get_active_kss_account_type,
)
from app.services.broker_cdp.login_progress import (
    all_login_progress,
    get_login_progress,
    is_login_active,
    state_file_for,
)
from app.services.eod_summary_push import eod_summary_service
from app.services.kill_switch_service import kill_switch_service
from app.services.miniapp_inbox import miniapp_inbox
from app.services.nego_scheduler import nego_scheduler
from app.services.net_equity_history import net_equity_history
from app.services.runbook_exit_service import RunbookExitError, runbook_exit_service
from app.services.suggest_entry_service import SuggestEntryError, suggest_entry_service
from app.services.line_cli.handlers import (
    _broker_health,
    _detach_session_cdp,
    _login_via_daemon,
)
from app.services.otp_relay_readiness import otp_relay_readiness
from app.services.trade_dispatcher import trade_dispatcher

logger = logging.getLogger(__name__)
router = APIRouter()

LINE_OAUTH_VERIFY_URL = "https://api.line.me/oauth2/v2.1/verify"
_LOCAL_CLIENT_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}
_PROXY_HEADERS = (
    "cf-connecting-ip",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-real-ip",
    "forwarded",
)
_TOKEN_CACHE: Dict[str, Dict[str, Any]] = {}
_TOKEN_CACHE_MAX = 64
_SESSION_TOKEN_PREFIX = "hl_"
_SESSION_TTL_SECONDS = 24 * 3600
_BROKER_ALIASES = {
    "kss": "kss",
    "krungsri": "kss",
    "trademan": "kss",
    "lib": "lib",
    "liberator": "lib",
}


class BrokerLoginPayload(BaseModel):
    force: bool = False


class SubmitOtpPayload(BaseModel):
    otp: str = Field(..., pattern=r"^\d{6}$", description="Exact 6-digit OTP code")
    broker: Optional[str] = Field(default=None, description="Optional broker slug override (kss|lib)")


class KillSwitchTogglePayload(BaseModel):
    action: Literal["activate", "reset"]
    reason: str = Field(default="Triggered via LINE MiniApp", max_length=200)


def _normalize_broker(broker: str) -> str:
    norm = _BROKER_ALIASES.get((broker or "").strip().lower())
    if not norm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported broker '{broker}'. Expected 'kss' or 'lib'.",
        )
    return norm


def _is_direct_local_request(request: Request) -> bool:
    """True only when request comes straight from loopback without reverse-proxy headers."""
    for h in _PROXY_HEADERS:
        if request.headers.get(h):
            return False
    host = request.client.host if request.client else ""
    return host in _LOCAL_CLIENT_HOSTS


def _allowed_user_ids() -> set[str]:
    allowed = {
        uid.strip()
        for uid in (settings.LINE_ALLOWED_USER_IDS or "").split(",")
        if uid and uid.strip()
    }
    admin_id = (settings.LINE_ADMIN_USER_ID or "").strip()
    if admin_id:
        allowed.add(admin_id)
    return allowed


def _resolve_line_login_client_id() -> str:
    explicit = (getattr(settings, "LINE_LOGIN_CHANNEL_ID", None) or "").strip()
    if explicit:
        return explicit
    liff_id = (getattr(settings, "LIFF_ID", None) or "").strip()
    if "-" in liff_id:
        return liff_id.split("-", 1)[0].strip()
    return ""


async def _verify_line_id_token_remote(id_token: str, client_id: str) -> Dict[str, Any]:
    """Verify a LIFF id_token against LINE OAuth2 verify endpoint."""
    async with httpx.AsyncClient(timeout=8.0) as client:
        resp = await client.post(
            LINE_OAUTH_VERIFY_URL,
            data={"id_token": id_token, "client_id": client_id},
        )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired LINE LIFF id_token",
        )
    claims = resp.json()
    if not isinstance(claims, dict) or not claims.get("sub"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="LINE token verification returned no subject",
        )
    return claims


async def verify_liff_id_token(id_token: str) -> Dict[str, Any]:
    """Verify LIFF id_token with short bounded in-memory TTL caching."""
    client_id = _resolve_line_login_client_id()
    if not client_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LIFF_ID or LINE_LOGIN_CHANNEL_ID is not configured on gateway",
        )

    now = time.time()
    cache_key = hashlib.sha256(f"{client_id}:{id_token}".encode("utf-8")).hexdigest()
    cached = _TOKEN_CACHE.get(cache_key)
    if cached and float(cached.get("expires_at", 0.0)) > now:
        return cached["claims"]

    claims = await _verify_line_id_token_remote(id_token, client_id)

    exp_raw = claims.get("exp")
    try:
        exp_ts = float(exp_raw) if exp_raw is not None else now + 300.0
    except (TypeError, ValueError):
        exp_ts = now + 300.0

    if exp_ts <= now:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="LINE LIFF id_token has expired",
        )

    if len(_TOKEN_CACHE) >= _TOKEN_CACHE_MAX:
        expired = [k for k, v in _TOKEN_CACHE.items() if float(v.get("expires_at", 0.0)) <= now]
        for k in expired:
            _TOKEN_CACHE.pop(k, None)
        if len(_TOKEN_CACHE) >= _TOKEN_CACHE_MAX:
            _TOKEN_CACHE.clear()

    _TOKEN_CACHE[cache_key] = {
        "claims": claims,
        "expires_at": min(exp_ts, now + 300.0),
    }
    return claims


def _session_signing_key() -> bytes:
    secret = (getattr(settings, "HOMELAB_SECRET_KEY", None) or "").strip()
    if not secret or secret == "change-me-set-in-dotenv":
        secret = f"{secret}:{getattr(settings, 'LINE_CHANNEL_SECRET', '')}"
    return secret.encode("utf-8")


def issue_miniapp_session_token(
    claims: Dict[str, Any],
    ttl_seconds: int = _SESSION_TTL_SECONDS,
) -> tuple[str, int]:
    """Sign a 24h Homelab MiniApp session token after LIFF id_token verification."""
    now = int(time.time())
    exp = now + max(60, int(ttl_seconds))
    sub = str(claims.get("sub") or "").strip()
    payload = {
        "sub": sub,
        "name": claims.get("name") or sub,
        "picture": claims.get("picture"),
        "iat": now,
        "exp": exp,
    }
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).rstrip(b"=").decode("ascii")
    sig = base64.urlsafe_b64encode(
        hmac.new(_session_signing_key(), body.encode("ascii"), hashlib.sha256).digest()
    ).rstrip(b"=").decode("ascii")
    return f"{_SESSION_TOKEN_PREFIX}{body}.{sig}", exp


def verify_miniapp_session_token(token: str) -> Dict[str, Any]:
    """Verify an HMAC-signed Homelab MiniApp session token."""
    raw = (token or "").strip()
    if not raw.startswith(_SESSION_TOKEN_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Homelab session token",
        )
    encoded = raw[len(_SESSION_TOKEN_PREFIX):]
    parts = encoded.split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Malformed Homelab session token",
        )
    body_b64, sig_b64 = parts
    expected_sig = base64.urlsafe_b64encode(
        hmac.new(_session_signing_key(), body_b64.encode("ascii"), hashlib.sha256).digest()
    ).rstrip(b"=").decode("ascii")
    if not hmac.compare_digest(sig_b64, expected_sig):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Homelab session token signature",
        )
    try:
        padded = body_b64 + "=" * (-len(body_b64) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unreadable Homelab session token",
        )
    if not isinstance(claims, dict) or not str(claims.get("sub") or "").strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Homelab session token missing subject",
        )
    try:
        exp = float(claims.get("exp") or 0)
    except (TypeError, ValueError):
        exp = 0.0
    if exp <= time.time():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Homelab session token has expired",
        )
    return claims


async def require_liff_user(
    request: Request,
    authorization: Optional[str] = Header(default=None),
) -> Dict[str, Any]:
    """Authenticate caller via Homelab session token or LIFF id_token and enforce allow-list."""
    token: Optional[str] = None
    if authorization:
        parts = authorization.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1].strip():
            token = parts[1].strip()

    if not token:
        if getattr(settings, "MINIAPP_DEV_BYPASS_LOCAL", False) and _is_direct_local_request(request):
            return {
                "sub": (settings.LINE_ADMIN_USER_ID or "LOCAL_DEV").strip(),
                "name": "Local Dev Operator",
                "picture": None,
                "auth_mode": "local_dev",
            }
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization Bearer LIFF id_token",
        )

    if token.startswith(_SESSION_TOKEN_PREFIX):
        claims = verify_miniapp_session_token(token)
        auth_mode = "session"
    else:
        claims = await verify_liff_id_token(token)
        auth_mode = "liff"

    sub = str(claims.get("sub") or "").strip()
    allowed = _allowed_user_ids()
    if not allowed or sub not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="LINE user is not authorized for InvestmentHomelab MiniApp",
        )

    return {
        "sub": sub,
        "name": claims.get("name") or sub,
        "picture": claims.get("picture"),
        "auth_mode": auth_mode,
    }


def _mode_payload() -> Dict[str, Any]:
    is_live = bool(trade_dispatcher.is_live)
    return {
        "is_live": is_live,
        "label": "LIVE" if is_live else "DRY-RUN",
        "badge": trade_dispatcher.get_mode_badge(),
    }


async def _pending_otp_payload() -> Optional[Dict[str, Any]]:
    pending = await otp_relay_registry.resolve_pending_request()
    if pending is None or pending.is_expired:
        return None
    created_at = getattr(pending, "created_at", None)
    # Defense-in-depth: if that broker's daemon already progressed to authenticated after
    # this OTP request was created, auto-clear the stale pending marker so MiniApp never
    # remains stuck on "กำลังตรวจสอบ OTP...".
    lp = get_login_progress(pending.broker)
    if (
        isinstance(lp, dict)
        and lp.get("phase") == "authenticated"
        and lp.get("updated_at") is not None
        and (created_at is None or float(lp["updated_at"]) >= created_at.timestamp())
    ):
        otp_relay_registry.clear_pending_request(pending.broker)
        return None

    expires_in = None
    if created_at is not None:
        age = max(0.0, time.time() - created_at.timestamp())
        expires_in = max(0, int(settings.OTP_TIMEOUT_SECONDS - age))
    submitted_at = getattr(pending, "submitted_at", None)
    is_submitted = bool(getattr(pending, "is_submitted", submitted_at is not None))
    return {
        "broker": pending.broker,
        "broker_label": otp_relay_registry.broker_label(pending.broker) or pending.broker,
        "ref": pending.ref,
        "expires_in_seconds": expires_in,
        "submitted": is_submitted,
        "submitted_at": submitted_at.isoformat() if submitted_at is not None else None,
    }


@router.get("/health", summary="Public MiniApp readiness check (no secrets)")
async def miniapp_health() -> Dict[str, Any]:
    return {
        "status": "ok",
        "liff_configured": bool((getattr(settings, "LIFF_ID", None) or "").strip()),
        "dev_bypass_local": bool(getattr(settings, "MINIAPP_DEV_BYPASS_LOCAL", False)),
    }


@router.get("/me", summary="Authenticated LINE operator profile and gateway safety flags")
async def miniapp_me(user: Dict[str, Any] = Depends(require_liff_user)) -> Dict[str, Any]:
    return {
        "user": user,
        "mode": _mode_payload(),
        "kill_switch": kill_switch_service.get_status(),
        "trading_rules": trading_rules.get_rules(),
        "liff_configured": bool((getattr(settings, "LIFF_ID", None) or "").strip()),
    }


@router.post("/auth/session", summary="Exchange verified LIFF id_token for a 24h Homelab session token")
async def miniapp_auth_session(user: Dict[str, Any] = Depends(require_liff_user)) -> Dict[str, Any]:
    session_token, expires_at = issue_miniapp_session_token(user, ttl_seconds=_SESSION_TTL_SECONDS)
    return {
        "session_token": session_token,
        "expires_at": expires_at,
        "ttl_seconds": _SESSION_TTL_SECONDS,
        "user": user,
    }


@router.get("/brokers/status", summary="Read-only CDP daemon, OTP relay, and safety status")
async def miniapp_brokers_status(
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    kss_h, lib_h, relay, pending_otp, quota, inbox_summary = await asyncio.gather(
        _broker_health("kss"),
        _broker_health("lib"),
        otp_relay_readiness.check(),
        _pending_otp_payload(),
        miniapp_inbox.get_line_quota_status(),
        miniapp_inbox.list_items(limit=1),
    )
    return {
        "brokers": {
            "kss": {
                "slug": "kss",
                "label": "Krungsri (KSS)",
                "port": 9333,
                "authenticated": bool(kss_h.get("authenticated")),
                "account": kss_h.get("account"),
                "heartbeat": kss_h.get("heartbeat"),
                "source": kss_h.get("source"),
                "cdp": kss_h.get("cdp") or {},
            },
            "lib": {
                "slug": "lib",
                "label": "Liberator",
                "port": 9334,
                "authenticated": bool(lib_h.get("authenticated")),
                "account": lib_h.get("account"),
                "heartbeat": lib_h.get("heartbeat"),
                "source": lib_h.get("source"),
                "cdp": lib_h.get("cdp") or {},
            },
        },
        "otp_relay": {
            "ready": bool(relay.ready),
            "channel": relay.channel,
            "reason": relay.reason,
        },
        "pending_otp": pending_otp,
        "login_progress": all_login_progress(),
        "kill_switch": kill_switch_service.get_status(),
        "mode": _mode_payload(),
        "line_quota": quota,
        "inbox_unread": int(inbox_summary.get("unread_count") or 0),
        "eod_status": eod_summary_service.get_status(),
        "kss_active_account": get_active_kss_account(),
        "kss_active_type": get_active_kss_account_type(),
        "kss_accounts": [
            {"account_no": str(getattr(settings, "STREAMING_ACCOUNT_NO_MARGIN", "") or "<REDACTED_KSS_ACCT>"), "label": "Margin"},
            {"account_no": str(getattr(settings, "STREAMING_ACCOUNT_NO_CASH", "") or "<REDACTED_KSS_ACCT>"), "label": "Cash"},
        ],
        "account_routing": account_routing.get_account_routing(),
        "trading_rules": trading_rules.get_rules(),
    }


class AccountRoutingPayload(BaseModel):
    short_broker: str = "kss"
    short_account: str = "margin"
    long_broker: str = "lib"
    long_account: str = "cash"


@router.get("/brokers/account-routing", summary="Get primary Short/Long default broker & account routing")
async def miniapp_get_account_routing(
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    return {"ok": True, "account_routing": account_routing.get_account_routing()}


@router.post("/brokers/account-routing", summary="Save primary Short/Long default broker & account routing")
async def miniapp_save_account_routing(
    payload: AccountRoutingPayload,
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    try:
        saved = account_routing.save_account_routing(
            short_broker=payload.short_broker,
            short_account=payload.short_account,
            long_broker=payload.long_broker,
            long_account=payload.long_account,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"ok": True, "account_routing": saved}


class TradingRulesPayload(BaseModel):
    short_uptick_ticks: Literal[0, 1] = 0
    short_open_uptick_ticks: Optional[Literal[0, 1]] = None
    auto_minus10_strict: Optional[bool] = None
    strict_uptick_symbols: Optional[Union[List[str], str]] = None


@router.get("/system/trading-rules", summary="Get persisted SET trading rules (Open/Nego Short rules & -10% auto-scan)")
async def miniapp_get_trading_rules(
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    return {"ok": True, "trading_rules": trading_rules.get_rules()}


@router.post("/system/trading-rules", summary="Save persisted SET trading rules (Open/Nego Short rules & -10% toggle)")
async def miniapp_save_trading_rules(
    payload: TradingRulesPayload,
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    try:
        saved = trading_rules.save_rules(
            short_uptick_ticks=payload.short_uptick_ticks,
            short_open_uptick_ticks=payload.short_open_uptick_ticks,
            auto_minus10_strict=payload.auto_minus10_strict,
            strict_uptick_symbols=payload.strict_uptick_symbols,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return {"ok": True, "trading_rules": saved}


def _summarize_daemon_failure(broker: str, res: Dict[str, Any]) -> str:
    """Build a concise user-facing failure reason from a crashed/down daemon result."""
    status_val = str(res.get("status") or "").lower()
    state_val = str(res.get("state") or "DAEMON_DOWN").upper()
    tail = str(res.get("log_tail") or "").strip()
    last_line = ""
    if tail:
        lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
        if lines:
            last_line = lines[-1][:220]
    base = f"เปิด {broker.upper()} Chrome CDP ไม่สำเร็จ ({status_val or state_val})"
    return f"{base}: {last_line}" if last_line else base


@router.post("/brokers/{broker}/login", summary="Start or resume CDP daemon login (Invariant 9 gated)")
async def miniapp_broker_login(
    broker: str,
    payload: Optional[BrokerLoginPayload] = None,
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    norm = _normalize_broker(broker)
    if kill_switch_service.is_active():
        raise HTTPException(
            status_code=status.HTTP_423_LOCKED,
            detail="KILL_SWITCH_ACTIVE — reset Kill-Switch before starting broker login",
        )

    relay = await otp_relay_readiness.check()
    if not relay.ready:
        raise HTTPException(
            status_code=status.HTTP_412_PRECONDITION_FAILED,
            detail=f"OTP relay not ready ({relay.reason or 'unavailable'}) — SMS OTP blocked (Invariant 9)",
        )

    force = bool(payload.force) if payload else False
    res = await _login_via_daemon(norm, force=force)
    if isinstance(res, dict) and (
        res.get("status") in ("crashed", "timeout") or res.get("state") == "DAEMON_DOWN"
    ):
        reason = _summarize_daemon_failure(norm, res)
        await miniapp_inbox.publish(
            category="broker",
            severity="error",
            title=f"❌ เปิด {norm.upper()} ไม่สำเร็จ",
            body=reason,
            broker=norm,
            action_tab="brokers",
            meta={"status": res.get("status"), "state": res.get("state")},
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=reason,
        )

    if (
        isinstance(res, dict)
        and res.get("state") == "AUTHENTICATED"
        and norm == "kss"
        and not is_login_active("kss")
    ):
        if res.get("sbl_state") != "AUTHENTICATED":
            try:
                await daemon_launcher.ensure_kss_tabs_complete()
            except Exception as exc:
                logger.debug("ensure_kss_tabs_complete skipped: %s", exc)
        # Always land KSS on Margin (<REDACTED_KSS_ACCT>) after login / resume unless already active.
        margin_acct = str(getattr(settings, "STREAMING_ACCOUNT_NO_MARGIN", "") or "<REDACTED_KSS_ACCT>").strip()
        if get_active_kss_account() != margin_acct:
            try:
                await trading_facade.switch_kss_account(margin_acct, target="streaming")
            except Exception as exc:
                logger.debug("Post-login switch KSS to Margin (%s) skipped: %s", margin_acct, exc)

    pending_otp = await _pending_otp_payload()
    return {
        "ok": True,
        "broker": norm,
        "result": res,
        "pending_otp": pending_otp,
        "login_progress": all_login_progress(),
    }


@router.post("/brokers/{broker}/stop", summary="Stop broker Chrome CDP daemon to reclaim RAM")
async def miniapp_broker_stop(
    broker: str,
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    norm = _normalize_broker(broker)
    await _detach_session_cdp(norm)
    res = await daemon_launcher.stop_daemon(norm)
    return {
        "ok": True,
        "broker": norm,
        "result": res,
    }


@router.post("/otp", summary="Submit 6-digit OTP into otp_relay_registry from MiniApp")
async def miniapp_submit_otp(
    payload: SubmitOtpPayload,
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    pending = await otp_relay_registry.resolve_pending_request()
    broker_tag: Optional[str] = None
    if payload.broker:
        norm = _normalize_broker(payload.broker)
        broker_tag = "KRUNGSRI" if norm == "kss" else "LIBERATOR"
    elif pending is not None and not pending.is_expired:
        broker_tag = pending.broker

    ref_code = pending.ref if (pending is not None and not pending.is_expired) else None

    await event_bus.publish_otp(
        OTPPayload(
            source=OTPSource.LINE_CHAT,
            otp=payload.otp,
            ref=ref_code,
            broker=broker_tag,
        )
    )
    await otp_relay_registry.record_otp_received(
        payload.otp,
        ref=ref_code,
        broker=broker_tag,
    )
    mark_fn = getattr(otp_relay_registry, "mark_pending_submitted", None)
    if callable(mark_fn) and broker_tag:
        try:
            mark_fn(broker_tag, submitted=True)
        except Exception:
            pass
    broker_lbl = otp_relay_registry.broker_label(broker_tag) or broker_tag or "Broker"
    await miniapp_inbox.publish(
        category="otp",
        severity="info",
        title=f"⏳ ส่ง OTP แล้ว ({broker_lbl})",
        body=f"ส่งรหัส 6 หลักเข้าระบบรีเลย์แล้ว กำลังรอโบรกเกอร์ยืนยัน (Ref: {ref_code or '-'})",
        broker="kss" if broker_tag == "KRUNGSRI" else ("lib" if broker_tag == "LIBERATOR" else None),
        action_tab="brokers",
        line_pushed=False,
        meta={"ref": ref_code, "submitted": True},
    )
    pending_after = await _pending_otp_payload()
    try:
        await sse_broadcaster.broadcast_event(
            "otp_submitted",
            pending_after or {"broker": broker_tag, "broker_label": broker_lbl, "ref": ref_code, "submitted": True},
        )
    except Exception:
        pass
    return {
        "ok": True,
        "broker": broker_tag,
        "broker_label": broker_lbl,
        "ref": ref_code,
        "submitted": True,
        "pending_otp": pending_after,
        "message": "ส่ง OTP เข้าสู่ระบบแล้ว กำลังรอโบรกเกอร์ยืนยัน...",
    }


@router.post("/kill-switch", summary="Activate or reset Emergency Kill-Switch")
async def miniapp_kill_switch(
    payload: KillSwitchTogglePayload,
    user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    actor = f"MINIAPP:{user['sub']}"
    if payload.action == "activate":
        res = await kill_switch_service.activate(reason=payload.reason, triggered_by=actor)
    else:
        res = await kill_switch_service.reset(authorized_by=actor)
    ks_state = kill_switch_service.get_status()
    is_act = bool(ks_state.get("is_active"))
    await miniapp_inbox.publish(
        category="system",
        severity="error" if is_act else "success",
        title="🚨 Emergency Kill-Switch เปิดใช้งาน" if is_act else "✅ ปลดล็อก Kill-Switch แล้ว",
        body=f"โดย {actor} · {payload.reason or ('หยุดออเดอร์ทั้งหมด' if is_act else 'กลับมาพร้อมเทรด')}",
        action_tab="brokers",
        line_pushed=False,
    )
    try:
        await sse_broadcaster.broadcast_event("kill_switch_updated", ks_state)
    except Exception:
        pass
    return {
        "ok": True,
        "kill_switch": ks_state,
        "result": res,
    }


def _raise_facade(exc: trading_facade.FacadeHttpError) -> NoReturn:
    detail: Dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.extra:
        detail.update(exc.extra)
    raise HTTPException(status_code=exc.status_code, detail=detail) from exc


class KssAccountPayload(BaseModel):
    account_no: str = Field(..., min_length=4, max_length=20)


class CancelOrderPayload(BaseModel):
    broker: str
    order_no: str = Field(..., min_length=1)
    symbol: str = ""


class NegoExecutePayload(BaseModel):
    broker: str
    order_no: str = Field(..., min_length=1)
    symbol: str = Field(..., min_length=1)
    side: str = Field(..., min_length=1)
    volume: int = 0
    old_price: Optional[float] = None
    mode: Literal["match", "lure", "custom"]
    custom_price: Optional[float] = None


class TradeOrderPayload(BaseModel):
    side: Literal["buy", "sell", "short", "cover"]
    symbol: str = Field(..., min_length=1, max_length=24)
    broker: Optional[str] = None
    account: Optional[str] = None
    amount_mode: Literal["volume", "value", "all"] = "volume"
    volume: int = Field(default=0, ge=0)
    target_value: Optional[float] = Field(default=None, gt=0)
    price: Optional[float] = Field(default=None, gt=0)


class TradeExecutePayload(TradeOrderPayload):
    idempotency_key: str = Field(..., min_length=4, max_length=80)


class SblActionPayload(BaseModel):
    action: Literal["borrow", "return"]
    symbol: str = Field(..., min_length=1, max_length=24)
    volume: Optional[int] = Field(default=None, ge=0)
    amount_mode: Literal["volume", "value", "all"] = "volume"
    target_value: Optional[float] = Field(default=None, gt=0)
    ref_price: Optional[float] = Field(default=None, gt=0)
    auto_short: bool = False
    short_price: Optional[float] = Field(default=None, gt=0)
    lot_id: Optional[str] = Field(default=None, max_length=32)
    remark: Optional[str] = Field(default=None, max_length=200)
    idempotency_key: Optional[str] = Field(default=None, max_length=80)


@router.get("/portfolio", summary="Phase 2: Multi-broker portfolio, cash & SBL lots (Zero-OTP)")
async def miniapp_portfolio(
    broker: str = Query(default="all"),
    kss_account: Optional[str] = Query(default=None),
    force_sbl: bool = Query(default=False),
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    try:
        return await trading_facade.fetch_portfolio_overview(
            broker=broker, kss_account=kss_account, force_sbl=force_sbl
        )
    except trading_facade.FacadeHttpError as exc:
        _raise_facade(exc)


@router.post("/portfolio/kss-account", summary="Phase 2: Switch active Krungsri Margin/Cash account")
async def miniapp_switch_kss_account(
    payload: KssAccountPayload,
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    try:
        return await trading_facade.fetch_portfolio_overview(
            broker="kss", kss_account=payload.account_no
        )
    except trading_facade.FacadeHttpError as exc:
        _raise_facade(exc)


@router.get("/sbl/pool/{symbol}", summary="Phase 2: Check SBL borrowable pool & XD risk")
async def miniapp_sbl_pool(
    symbol: str,
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    try:
        return await trading_facade.fetch_sbl_pool_quote(symbol)
    except trading_facade.FacadeHttpError as exc:
        _raise_facade(exc)


@router.get("/orders", summary="Phase 3: Live orders ledger across KSS & Liberator")
async def miniapp_orders(
    broker: str = Query(default="all"),
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    try:
        return await trading_facade.fetch_orders_overview(broker=broker)
    except trading_facade.FacadeHttpError as exc:
        _raise_facade(exc)


@router.post("/orders/cancel", summary="Phase 3: Cancel an open resting order (fail-closed)")
async def miniapp_orders_cancel(
    payload: CancelOrderPayload,
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    try:
        return await trading_facade.execute_order_cancel(
            broker=payload.broker, order_no=payload.order_no, symbol=payload.symbol
        )
    except trading_facade.FacadeHttpError as exc:
        _raise_facade(exc)


@router.get("/nego/quote", summary="Phase 3: Live Bid1/Offer1/Last and Match/Lure price plan")
async def miniapp_nego_quote(
    broker: str = Query(default="kss"),
    symbol: str = Query(...),
    side: Optional[str] = Query(default=None),
    old_price: Optional[float] = Query(default=None),
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    try:
        return await trading_facade.compute_nego_quote(
            broker=broker, symbol=symbol, side=side, old_price=old_price
        )
    except trading_facade.FacadeHttpError as exc:
        _raise_facade(exc)


@router.post("/nego/execute", summary="Phase 3: Amend resting order with SET tick & uptick guards")
async def miniapp_nego_execute(
    payload: NegoExecutePayload,
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    try:
        return await trading_facade.execute_nego_amend(
            broker=payload.broker,
            order_no=payload.order_no,
            symbol=payload.symbol,
            side=payload.side,
            volume=payload.volume,
            old_price=payload.old_price,
            mode=payload.mode,
            custom_price=payload.custom_price,
        )
    except trading_facade.FacadeHttpError as exc:
        _raise_facade(exc)


class NegoScheduleTogglePayload(BaseModel):
    window_id: Optional[str] = Field(default=None)
    enabled: bool


class NegoScheduleRerollPayload(BaseModel):
    window_id: Optional[str] = Field(default=None)


class NegoScheduleRunNowPayload(BaseModel):
    window_id: str = Field(..., min_length=1)


def _read_daily_pipeline_status(today_str: str) -> Dict[str, Any]:
    path = Path(
        str(
            getattr(
                settings,
                "DAILY_PIPELINE_STATUS_PATH",
                "/srv/antigravity/shadow/logs/run_daily_latest.json",
            )
        )
    )
    base: Dict[str, Any] = {
        "scheduled_time": "17:30",
        "timer": "trading-daily-shadow.timer",
        "available": False,
        "status": "PENDING",
        "target_date": None,
        "started": None,
        "seconds": None,
        "failed": False,
        "failed_stage": None,
        "mode": None,
        "stages": [],
    }
    if not path.exists():
        return base
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return base
    except Exception as exc:
        logger.debug("Read daily_pipeline status failed %s: %s", path, exc)
        return base

    started = str(raw.get("started") or "")
    target_date = str(raw.get("target_date") or raw.get("date") or "")
    ran_post_close_today = False
    if started.startswith(today_str):
        try:
            hh = int(started[11:13])
            mm = int(started[14:16])
            ran_post_close_today = (hh * 60 + mm) >= (17 * 60)
        except (ValueError, IndexError):
            ran_post_close_today = False

    failed = bool(raw.get("failed"))
    st = ("FAILED" if failed else "OK") if ran_post_close_today else "PENDING"
    stages_in = raw.get("stages") if isinstance(raw.get("stages"), list) else []
    stages_out = [
        {
            "id": str(s.get("id") or ""),
            "status": str(s.get("status") or ""),
            "code": s.get("code"),
            "seconds": s.get("seconds"),
        }
        for s in stages_in
        if isinstance(s, dict)
    ]
    base.update(
        {
            "available": True,
            "status": st,
            "ran_post_close_today": ran_post_close_today,
            "target_date": target_date or None,
            "started": started or None,
            "seconds": raw.get("seconds"),
            "failed": failed,
            "failed_stage": raw.get("failed_stage"),
            "mode": raw.get("mode"),
            "stages": stages_out,
        }
    )
    return base


def _schedule_with_runbook(nego_snap: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(nego_snap)
    out["runbook_exit"] = runbook_exit_service.get_schedule_snapshot()
    today_str = str(nego_snap.get("daily_date") or datetime.now(ZoneInfo("Asia/Bangkok")).strftime("%Y-%m-%d"))
    eod_snap = dict(eod_summary_service.get_status())
    eod_snap["today_net_equity"] = net_equity_history.get_entry_for_date(today_str)
    eod_snap["csv_path"] = str(getattr(settings, "NET_EQUITY_HISTORY_CSV_PATH", "data/net_equity_history.csv"))
    out["eod"] = eod_snap
    out["daily_pipeline"] = _read_daily_pipeline_status(today_str)
    return out


@router.get("/nego/schedule", summary="Auto-Nego Scheduler windows, random times & Daily Checklist")
async def miniapp_nego_schedule(
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    return _schedule_with_runbook(nego_scheduler.get_schedule_snapshot())


@router.post("/nego/schedule/toggle", summary="Toggle Master or per-window Auto-Nego schedule")
async def miniapp_nego_schedule_toggle(
    payload: NegoScheduleTogglePayload,
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    try:
        return _schedule_with_runbook(
            nego_scheduler.set_toggle(window_id=payload.window_id, enabled=payload.enabled)
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "E_BAD_WINDOW", "message": str(exc)},
        ) from exc


@router.post("/nego/schedule/reroll", summary="Re-roll pending random trigger times for today")
async def miniapp_nego_schedule_reroll(
    payload: NegoScheduleRerollPayload,
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    try:
        return _schedule_with_runbook(nego_scheduler.reroll_times(window_id=payload.window_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "E_BAD_WINDOW", "message": str(exc)},
        ) from exc


@router.post("/nego/schedule/run-now", summary="Manually trigger one Auto-Nego schedule window now")
async def miniapp_nego_schedule_run_now(
    payload: NegoScheduleRunNowPayload,
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    try:
        return _schedule_with_runbook(
            await nego_scheduler.run_window_now(payload.window_id, source="manual")
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"code": "E_BAD_WINDOW", "message": str(exc)},
        ) from exc


@router.get("/quote", summary="Phase 4: Live quote & tick steps for trade entry")
async def miniapp_quote(
    symbol: str = Query(...),
    broker: str = Query(default="kss"),
    side: Optional[str] = Query(default=None),
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    try:
        return await trading_facade.compute_nego_quote(broker=broker, symbol=symbol, side=side)
    except trading_facade.FacadeHttpError as exc:
        _raise_facade(exc)


@router.post("/trade/preview", summary="Phase 4: Pre-trade guardrails & share/THB sizing preview")
async def miniapp_trade_preview(
    payload: TradeOrderPayload,
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    try:
        return await trading_facade.preview_trade_order(**payload.model_dump())
    except trading_facade.FacadeHttpError as exc:
        _raise_facade(exc)


@router.post("/trade/execute", summary="Phase 4: Single-flight idempotent trade execution")
async def miniapp_trade_execute(
    payload: TradeExecutePayload,
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    try:
        return await trading_facade.execute_trade_with_idempotency(**payload.model_dump())
    except trading_facade.FacadeHttpError as exc:
        _raise_facade(exc)


@router.post("/sbl/action", summary="Phase 4: Borrow or return SBL shares on Krungsri Margin")
async def miniapp_sbl_action(
    payload: SblActionPayload,
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    try:
        return await trading_facade.execute_sbl_action(**payload.model_dump())
    except trading_facade.FacadeHttpError as exc:
        _raise_facade(exc)


class RunbookExitSubmitPayload(BaseModel):
    allow_stale: bool = False
    symbols: Optional[List[str]] = None


@router.get("/runbook/exits", summary="Preview today's Runbook exits reconciled with live brokers")
async def miniapp_runbook_exits_preview(
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    return await runbook_exit_service.preview_runbook_exits()


@router.post("/runbook/exits/submit", summary="Schedule (10:00-10:30) or execute Runbook exits")
async def miniapp_runbook_exits_submit(
    payload: Optional[RunbookExitSubmitPayload] = Body(default=None),
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    try:
        res = await runbook_exit_service.enqueue_or_execute_exits(
            allow_stale=bool(payload.allow_stale) if payload else False,
            symbols=payload.symbols if payload else None,
        )
        return res
    except RunbookExitError as exc:
        _raise_facade(exc)


@router.get("/runbook/exits/schedule", summary="Get today's Runbook Auto-Exit schedule & checklist")
async def miniapp_runbook_exits_schedule(
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    return runbook_exit_service.get_schedule_snapshot()


@router.post("/runbook/exits/schedule/reroll", summary="Re-roll random time for pending Runbook Exit queue")
async def miniapp_runbook_exits_reroll(
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    try:
        rb = runbook_exit_service.reroll_scheduled_time()
        return {**_schedule_with_runbook(nego_scheduler.get_schedule_snapshot()), "runbook_exit": rb}
    except RunbookExitError as exc:
        _raise_facade(exc)


@router.post("/runbook/exits/schedule/run-now", summary="Execute pending Runbook Exit queue immediately")
async def miniapp_runbook_exits_run_now(
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    try:
        rb = await runbook_exit_service.run_queue_now()
        return {**_schedule_with_runbook(nego_scheduler.get_schedule_snapshot()), "runbook_exit": rb}
    except RunbookExitError as exc:
        _raise_facade(exc)


class RunbookExitTogglePayload(BaseModel):
    enabled: bool


@router.post("/runbook/exits/schedule/toggle", summary="Enable/disable daily 10:00-10:30 Runbook Auto-Exit")
async def miniapp_runbook_exits_toggle(
    payload: RunbookExitTogglePayload,
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    rb = runbook_exit_service.set_auto_enabled(bool(payload.enabled))
    return {**_schedule_with_runbook(nego_scheduler.get_schedule_snapshot()), "runbook_exit": rb}


@router.post("/runbook/exits/schedule/cancel", summary="Cancel pending Runbook Exit queue")
async def miniapp_runbook_exits_cancel(
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    try:
        rb = runbook_exit_service.cancel_scheduled_queue()
        return {**_schedule_with_runbook(nego_scheduler.get_schedule_snapshot()), "runbook_exit": rb}
    except RunbookExitError as exc:
        _raise_facade(exc)


class SuggestEntryConfigPayload(BaseModel):
    budget_per_slot_thb: Optional[float] = Field(default=None, ge=1000.0, le=50_000_000.0)
    use_recommended: bool = False


class SuggestEntryItemPayload(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=24)
    side: Literal["LONG", "SHORT"]
    price_mode: Literal["lure", "match"] = "lure"


class SuggestEntrySubmitPayload(BaseModel):
    items: List[SuggestEntryItemPayload] = Field(..., min_length=1, max_length=10)
    allow_stale: bool = False
    override_hold_cash: bool = False


@router.get("/trade/hub-summary", summary="Fast local summary for Tab Trade split view (Auto Exit + Suggest Entry)")
async def miniapp_trade_hub_summary(
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    return suggest_entry_service.get_hub_summary()


@router.get("/suggest-entry/poll", summary="On-demand live Poll across Top-8 Merge Pool candidates")
async def miniapp_suggest_entry_poll(
    allow_stale: bool = Query(default=False),
    override_hold_cash: bool = Query(default=False),
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    return await suggest_entry_service.poll_candidates(
        allow_stale=allow_stale,
        override_hold_cash=override_hold_cash,
    )


@router.get("/suggest-entry/quotes", summary="Lightweight live quote tick for Suggest Entry modal")
async def miniapp_suggest_entry_quotes(
    allow_stale: bool = Query(default=False),
    override_hold_cash: bool = Query(default=False),
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    return await suggest_entry_service.poll_live_quotes(
        allow_stale=allow_stale,
        override_hold_cash=override_hold_cash,
    )


@router.get("/suggest-entry/stream", summary="SSE live 2s quote stream for Suggest Entry Multitrade modal")
async def miniapp_suggest_entry_stream(
    request: Request,
    allow_stale: bool = Query(default=False),
    override_hold_cash: bool = Query(default=False),
    interval_s: float = Query(default=2.0, ge=1.0, le=10.0),
    max_ticks: int = Query(default=150, ge=1, le=300),
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> StreamingResponse:
    async def event_stream():
        for tick in range(max_ticks):
            if await request.is_disconnected():
                break
            try:
                payload = await suggest_entry_service.poll_live_quotes(
                    allow_stale=allow_stale,
                    override_hold_cash=override_hold_cash,
                )
                yield f"event: quotes\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            except Exception as exc:
                err_payload = json.dumps({"error": str(exc)}, ensure_ascii=False)
                yield f"event: stream_error\ndata: {err_payload}\n\n"
            if tick < max_ticks - 1:
                await asyncio.sleep(interval_s)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/suggest-entry/config", summary="Save user-configured THB budget per Suggest Entry lot")
async def miniapp_suggest_entry_config(
    payload: SuggestEntryConfigPayload,
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    try:
        return suggest_entry_service.save_config(
            payload.budget_per_slot_thb,
            use_recommended=payload.use_recommended,
        )
    except SuggestEntryError as exc:
        _raise_facade(exc)


@router.post("/suggest-entry/submit", summary="Execute user-confirmed Multiple-Choice Suggest Entry orders")
async def miniapp_suggest_entry_submit(
    payload: SuggestEntrySubmitPayload,
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    try:
        return await suggest_entry_service.submit_selected(
            [it.model_dump() for it in payload.items],
            allow_stale=payload.allow_stale,
            override_hold_cash=payload.override_hold_cash,
        )
    except SuggestEntryError as exc:
        _raise_facade(exc)


class InboxReadPayload(BaseModel):
    item_id: Optional[str] = Field(default=None, max_length=64)


class InboxClearPayload(BaseModel):
    archive: bool = True


class EodSendPayload(BaseModel):
    force: bool = True


class EodTogglePayload(BaseModel):
    enabled: bool


@router.get("/inbox", summary="Notification Inbox items & live LINE quota status")
async def miniapp_get_inbox(
    limit: int = Query(default=50, ge=1, le=150),
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    data, quota = await asyncio.gather(
        miniapp_inbox.list_items(limit=limit),
        miniapp_inbox.get_line_quota_status(),
    )
    return {
        **data,
        "quota": quota,
    }


@router.post("/inbox/read", summary="Mark one or all MiniApp Notification Inbox items read")
async def miniapp_mark_inbox_read(
    payload: Optional[InboxReadPayload] = Body(default=None),
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    item_id = payload.item_id if payload else None
    return await miniapp_inbox.mark_read(item_id=item_id)


@router.post("/inbox/clear", summary="Archive inbox logs to JSONL and clear MiniApp Notification Inbox")
async def miniapp_clear_inbox(
    payload: Optional[InboxClearPayload] = Body(default=None),
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    archive = payload.archive if payload is not None else True
    return await miniapp_inbox.clear_items(archive=archive)


@router.get("/quota", summary="LINE Messaging API monthly quota & EOD summary status")
async def miniapp_get_quota(
    refresh: bool = Query(default=False),
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    quota = await miniapp_inbox.get_line_quota_status(force_refresh=refresh)
    return {
        "quota": quota,
        "eod_status": eod_summary_service.get_status(),
    }


@router.get("/eod/preview", summary="Read-only preview of today's 17:05 EOD summary & Net Equity")
async def miniapp_eod_preview(
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    return await eod_summary_service.build_summary()


@router.post("/eod/send-now", summary="Run 17:05 EOD summary, record Net Equity CSV/JSON & save to MiniApp Inbox")
async def miniapp_eod_send_now(
    payload: Optional[EodSendPayload] = Body(default=None),
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    force = payload.force if payload else True
    return await eod_summary_service.send_now(force=force)


@router.post("/eod/toggle", summary="Enable or disable daily 17:05 EOD & Net Equity auto-record")
async def miniapp_eod_toggle(
    payload: EodTogglePayload,
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    eod_summary_service.set_enabled(payload.enabled)
    return _schedule_with_runbook(nego_scheduler.get_schedule_snapshot())


@router.get("/eod/net-equity-history", summary="Get daily EOD Net Equity history for charting")
async def miniapp_eod_net_equity_history(
    limit: int = Query(90, ge=1, le=3650),
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> Dict[str, Any]:
    return net_equity_history.get_history(limit=limit)


_SSE_FILE_POLL_INTERVAL = 0.5


def _path_stat_sig(path: Any) -> Any:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _pending_sig(pending: Optional[Dict[str, Any]]) -> Any:
    if not pending:
        return None
    return (pending.get("broker"), pending.get("ref"), bool(pending.get("submitted")))


def _progress_sig(prog: Optional[Dict[str, Any]]) -> Any:
    if not prog:
        return None
    return (
        prog.get("phase"),
        prog.get("step"),
        prog.get("message"),
        prog.get("error"),
        prog.get("ref"),
        prog.get("attempt"),
    )


async def _collect_cross_process_sse_events(state: Dict[str, Any]) -> List[tuple[str, Dict[str, Any]]]:
    """Detect out-of-process writes from standalone session daemons and emit SSE events."""
    events: List[tuple[str, Dict[str, Any]]] = []

    otp_path_fn = getattr(otp_relay_registry, "_get_otp_state_file", None)
    if callable(otp_path_fn):
        otp_sig = _path_stat_sig(otp_path_fn())
        if otp_sig != state["otp_file_sig"]:
            state["otp_file_sig"] = otp_sig
            pending = await _pending_otp_payload()
            new_pending_sig = _pending_sig(pending)
            old_pending_sig = state["pending_sig"]
            if new_pending_sig != old_pending_sig:
                state["pending_sig"] = new_pending_sig
                if pending is not None:
                    if pending.get("submitted"):
                        events.append(("otp_submitted", pending))
                    else:
                        events.append(("otp_required", pending))
                elif old_pending_sig is not None:
                    events.append(
                        (
                            "otp_resolved",
                            {"broker": old_pending_sig[0], "ref": old_pending_sig[1]},
                        )
                    )

    for canon_key, slug in (("kss", "kss"), ("liberator", "lib")):
        p_sig = _path_stat_sig(state_file_for(canon_key))
        if p_sig != state["prog_file_sigs"].get(slug):
            state["prog_file_sigs"][slug] = p_sig
            prog = get_login_progress(canon_key)
            new_psig = _progress_sig(prog)
            if new_psig != state["prog_sigs"].get(slug):
                state["prog_sigs"][slug] = new_psig
                events.append(
                    (
                        "login_progress",
                        prog or {"broker": slug, "phase": "idle", "step": 0, "message": "", "error": None},
                    )
                )

    inbox_sig = _path_stat_sig(miniapp_inbox._state_path())
    if inbox_sig != state["inbox_file_sig"]:
        state["inbox_file_sig"] = inbox_sig
        try:
            snap = await miniapp_inbox.list_items(limit=5)
            items = snap.get("items") or []
            top_item = items[0] if items else None
            top_id = top_item.get("id") if isinstance(top_item, dict) else None
            if top_id and top_id != state["last_inbox_id"]:
                state["last_inbox_id"] = top_id
                events.append(
                    (
                        "miniapp_inbox",
                        {
                            "item": top_item,
                            "unread_count": int(snap.get("unread_count") or 0),
                        },
                    )
                )
            elif not top_id and state["last_inbox_id"] is not None:
                state["last_inbox_id"] = None
                events.append(("miniapp_inbox_cleared", {"unread_count": 0}))
        except Exception:
            pass

    return events


@router.get("/events", summary="Authenticated SSE live stream for MiniApp Inbox, OTP, and Kill-Switch")
async def miniapp_events(
    request: Request,
    _user: Dict[str, Any] = Depends(require_liff_user),
) -> StreamingResponse:
    async def event_generator():
        queue = sse_broadcaster.subscribe()
        try:
            inbox_snapshot, pending_otp, quota = await asyncio.gather(
                miniapp_inbox.list_items(limit=20),
                _pending_otp_payload(),
                miniapp_inbox.get_line_quota_status(),
            )
            login_prog = all_login_progress()
            init_payload = {
                "status": "MINIAPP_STREAM_READY",
                "unread_count": int(inbox_snapshot.get("unread_count") or 0),
                "pending_otp": pending_otp,
                "login_progress": login_prog,
                "quota": quota,
                "kill_switch": kill_switch_service.get_status(),
            }
            otp_path_fn = getattr(otp_relay_registry, "_get_otp_state_file", None)
            init_items = inbox_snapshot.get("items") or []
            watcher_state: Dict[str, Any] = {
                "otp_file_sig": _path_stat_sig(otp_path_fn()) if callable(otp_path_fn) else None,
                "pending_sig": _pending_sig(pending_otp),
                "prog_file_sigs": {
                    "kss": _path_stat_sig(state_file_for("kss")),
                    "lib": _path_stat_sig(state_file_for("liberator")),
                },
                "prog_sigs": {
                    "kss": _progress_sig(login_prog.get("kss")),
                    "lib": _progress_sig(login_prog.get("lib")),
                },
                "inbox_file_sig": _path_stat_sig(miniapp_inbox._state_path()),
                "last_inbox_id": init_items[0].get("id") if init_items else None,
            }
            yield f"event: connected\ndata: {json.dumps(init_payload, ensure_ascii=False)}\n\n"
            last_yield_ts = time.monotonic()
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=_SSE_FILE_POLL_INTERVAL)
                    ev_type = str(msg.get("type") or "message")
                    ev_data = json.dumps(msg.get("data", {}), ensure_ascii=False)
                    yield f"event: {ev_type}\ndata: {ev_data}\n\n"
                    last_yield_ts = time.monotonic()
                except asyncio.TimeoutError:
                    pass

                for ev_type, ev_payload in await _collect_cross_process_sse_events(watcher_state):
                    yield f"event: {ev_type}\ndata: {json.dumps(ev_payload, ensure_ascii=False)}\n\n"
                    last_yield_ts = time.monotonic()

                if time.monotonic() - last_yield_ts >= 20.0:
                    yield ": keepalive\n\n"
                    last_yield_ts = time.monotonic()
        except asyncio.CancelledError:
            pass
        finally:
            sse_broadcaster.unsubscribe(queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
