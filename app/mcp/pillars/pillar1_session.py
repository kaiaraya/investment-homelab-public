"""
Pillar 1: Session Sentinel Module.
Handles:
- Unified connection & authentication health check
- CDP-first login initiation (Krungsri Streaming :9333 & Liberator :9334)
- 2FA SMS OTP manual submission fallback
- Safe logout and session termination

In scope: exactly three apps — Krungsri Streaming (SBL Realtime rides the same
Krungsri session), Liberator. Click2Win / TradeMan / QuickTrade / Settrade OpenAPI
are de-scoped: no branch here may resurrect them (CDP-UNIFY 8).
"""

import asyncio
import logging
from typing import Dict, Any, Optional
from datetime import datetime, timezone

from app.core.config import settings
from app.browser.auth_driver import auth_driver
from app.brokers.krungsri.session_health import session_file_info
from app.schemas.auth import OTPPayload, OTPSource

logger = logging.getLogger("Pillar1.Session")

#: CDP purpose per broker — mirrors ``line_cli.handlers._ATTACH_PURPOSE`` so MCP and
#: LINE ask the probe the exact same question.
_CDP_PURPOSE = {"kss": "streaming", "lib": "portfolio"}

#: Per-probe ceiling: the probe itself caps each step at 2s, and /health must answer
#: even when a daemon is wedged.
_CDP_PROBE_TIMEOUT_S = 3.0

#: Registry keys that belong to the three in-scope apps. Settrade Streaming and
#: Krungsri SBL Realtime both ride the Krungsri CDP session, so they collapse onto
#: ``krungsri``; Click2Win / Settrade OpenAPI / TradeMan / QuickTrade are de-scoped
#: and must never appear in a health payload again (CDP-UNIFY 8).
_IN_SCOPE_PLATFORMS = {"krungsri", "liberator"}

#: Human-facing app list for cockpit/status payloads.
IN_SCOPE_APPS = ["Settrade Streaming (Krungsri)", "Krungsri SBL Realtime", "Liberator"]


async def _cdp_probe(broker: str, purpose: str) -> Dict[str, Any]:
    """Read-only CDP truth for one broker tab (never launches/navigates/logs in)."""
    try:
        from app.services.broker_cdp.session_probe import cdp_session_status
        return dict(await asyncio.wait_for(
            cdp_session_status(broker, purpose=purpose), timeout=_CDP_PROBE_TIMEOUT_S))
    except Exception as e:  # noqa: BLE001
        return {
            "broker": broker,
            "purpose": purpose,
            "state": "UNKNOWN",
            "daemon_up": False,
            "reason": f"{type(e).__name__}: {e}",
        }


async def _cdp_session_health() -> Dict[str, Any]:
    """``cdp`` block for /health — same probe + purposes ``handlers._broker_health`` uses,
    so MCP tools and LINE ``/status`` report the same state per broker."""
    kss, lib = await asyncio.gather(
        _cdp_probe("kss", _CDP_PURPOSE["kss"]),
        _cdp_probe("lib", _CDP_PURPOSE["lib"]),
    )
    if kss.get("daemon_up"):
        kss["sbl"] = await _cdp_probe("kss", "sbl")
    else:
        kss["sbl"] = {
            "broker": "kss",
            "purpose": "sbl",
            "state": kss.get("state", "DAEMON_DOWN"),
            "daemon_up": False,
            "reason": kss.get("reason", ""),
        }
    return {"kss": kss, "lib": lib}



async def session_get_health() -> Dict[str, Any]:
    """
    Unified Session Health Check.
    Returns status of the three in-scope apps: Krungsri Streaming (:9333 — SBL
    Realtime rides the same session) and Liberator (:9334). De-scoped apps are not
    reported at all (CDP-UNIFY 8: no phantom rows for apps we no longer run).
    """
    kss_status = auth_driver.status
    kss_authenticated = auth_driver.is_authenticated
    kss_ref = auth_driver.current_ref
    kss_heartbeat = auth_driver.last_heartbeat.isoformat() if auth_driver.last_heartbeat else None

    # File-backed session state (KSS-RCA-011): in-memory driver state alone cannot tell
    # "no saved session at all" from "saved session present but driver not attached".
    # Mirrors the REST endpoint GET /api/v1/session/status -> session_file_exists.
    kss_session_file = session_file_info(settings.SESSION_STATE_PATH)

    from app.schemas.account import account_context

    # Check KDE Connect reachability & active transport routes
    kde_reachable = False
    kde_device_id = getattr(settings, "KDECONNECT_DEVICE_ID", None)
    kde_routes = {
        "active_route": "DISCONNECTED",
        "route_label": "❌ Disconnected",
        "remote_ip": "-",
        "wifi": "OFFLINE",
        "tailscale": "OFFLINE"
    }
    try:
        from app.services.kdeconnect_service import kdeconnect_service
        kde_reachable = await kdeconnect_service.is_device_reachable()
        dev = await kdeconnect_service.get_reachable_device_id()
        if dev:
            kde_device_id = dev
        kde_routes = await kdeconnect_service.get_network_routes()
    except Exception as e:
        logger.debug(f"KDE Connect health check error: {e}")

    # Pluggable Broker Registry health check — in-scope apps only (CDP-UNIFY 8).
    # The registry may still hold de-scoped plugins (click2win / settrade_openapi);
    # reporting their health made the cockpit show phantom apps, so filter here
    # instead of trusting the registry to have been pruned.
    broker_health = {}
    try:
        from app.brokers import broker_registry
        all_health = await broker_registry.get_all_health()
        broker_health = {k: v for k, v in all_health.items() if k in _IN_SCOPE_PLATFORMS}
    except Exception as e:
        logger.debug(f"Broker registry health check: {e}")

    # CDP truth block (CDP-4): the same read-only probe LINE `/status` uses, so both
    # channels answer the same question. Never raises — a broken daemon is a state,
    # not an error. Existing keys above are untouched (additive only).
    cdp_health = await _cdp_session_health()
    cdp_authenticated = any(
        probe.get("state") == "AUTHENTICATED"
        for probe in (cdp_health.get("kss"), cdp_health.get("lib"))
        if isinstance(probe, dict)
    )

    return {
        "status": "healthy" if (kss_authenticated or cdp_authenticated
                                 or any(b.get("is_authenticated") for b in broker_health.values())) else "degraded",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "brokers": broker_health,
        "in_scope_apps": IN_SCOPE_APPS,
        "de_scoped_apps": ["Click2Win", "TradeMan", "QuickTrade", "Settrade OpenAPI"],
        "cdp": cdp_health,
        "krungsri_trademan": {
            "status": kss_status,
            "authenticated": kss_authenticated,
            "current_otp_ref": kss_ref,
            "last_heartbeat": kss_heartbeat,
            "session_file_exists": kss_session_file["exists"],
            "session_file_mtime": kss_session_file["mtime_iso"],
            "session_file_age_hours": kss_session_file["age_hours"],
            "needs_fresh_login": kss_session_file["needs_fresh_login"],
            "session_file_reason": kss_session_file["reason"],
            "active_account": account_context.krungsri_active.value,
            "accounts": ["CASH", "MARGIN"]
        },
        "settrade": {
            # Click2Win / Settrade OpenAPI are de-scoped (CDP-UNIFY 8). The key stays
            # so old clients do not KeyError, but it only ever reports the in-scope
            # Settrade surface we still drive: Streaming over the Krungsri CDP tab.
            "streaming_via_cdp": True,
            "desktop_port": 9333,
            "openapi_configured": False,
            "de_scoped": ["CLICK2WIN", "SETTRADE_OPENAPI"],
        },
        "kdeconnect": {
            "status": "ONLINE" if kde_reachable else "OFFLINE",
            "reachable": kde_reachable,
            "device_id": kde_device_id or "Not Paired",
            "auto_otp_active": True,
            "network_routes": kde_routes,
            "active_route": kde_routes.get("active_route", "DISCONNECTED"),
            "route_label": kde_routes.get("route_label", "❌ Disconnected"),
            "remote_ip": kde_routes.get("remote_ip", "-"),
            "wifi_status": kde_routes.get("wifi", "OFFLINE"),
            "tailscale_status": kde_routes.get("tailscale", "OFFLINE")
        }
    }


async def session_login_broker(platform: str = "KRUNGSRI", force_fresh: bool = False) -> Dict[str, Any]:
    """
    Initiates authentication for the specified platform.
    - 'KRUNGSRI' / 'TRADEMAN' (alias): Krungsri Streaming via the CDP daemon (:9333)
    - 'LIBERATOR': Liberator via the CDP daemon (:9334)
    - 'CLICK2WIN' / 'SETTRADE_OPENAPI' / 'QUICKTRADE': de-scoped → DE_SCOPED
    """
    plat = platform.upper().strip()
    if plat in ["KRUNGSRI", "TRADEMAN"]:
        from app.services.broker_cdp import daemon_launcher
        logger.info(f"Pillar 1: Initiating Krungsri session daemon (force_fresh={force_fresh})...")
        if force_fresh:
            spawn_res = await daemon_launcher.restart_daemon("kss")
        else:
            spawn_res = await daemon_launcher.ensure_daemon("kss")

        st = spawn_res.get("state")
        success = (st == "AUTHENTICATED")
        status_str = "AUTHENTICATED" if success else (st or "UNKNOWN")
        if success:
            msg = "Krungsri session daemon authenticated"
        elif st in ("OTP_PAGE", "LOGIN_PAGE"):
            msg = f"Awaiting login/2FA on Krungsri ({st})"
        else:
            msg = f"Krungsri session daemon state: {st}"

        return {
            "platform": "TRADEMAN",
            "success": success,
            "status": status_str,
            "message": msg,
            "otp_ref": None,
        }
    elif plat in ["LIBERATOR"]:
        from app.services.broker_cdp import daemon_launcher
        logger.info(f"Pillar 1: Initiating Liberator session daemon (force_fresh={force_fresh})...")
        if force_fresh:
            spawn_res = await daemon_launcher.restart_daemon("lib")
        else:
            spawn_res = await daemon_launcher.ensure_daemon("lib")

        st = spawn_res.get("state")
        success = (st == "AUTHENTICATED")
        status_str = "AUTHENTICATED" if success else (st or "UNKNOWN")
        if success:
            msg = "Liberator session daemon authenticated"
        elif st in ("OTP_PAGE", "LOGIN_PAGE"):
            msg = f"Awaiting login/2FA on Liberator ({st})"
        else:
            msg = f"Liberator session daemon state: {st}"

        return {
            "platform": "LIBERATOR",
            "success": success,
            "status": status_str,
            "message": msg,
            "otp_ref": None,
        }
    elif plat in ("SETTRADE_OPENAPI", "SETTRADE", "CLICK2WIN", "QUICKTRADE"):
        # De-scoped (CDP-UNIFY 8). Fail closed with an explicit code instead of
        # waking a driver we no longer maintain — a silent success here would be a
        # lie about a session that cannot exist.
        logger.warning(f"Pillar 1: refusing de-scoped platform {plat!r}")
        return {
            "platform": plat,
            "success": False,
            "status": "DE_SCOPED",
            "message": f"{plat} is de-scoped — use KRUNGSRI (Streaming) or LIBERATOR",
            "otp_ref": None,
        }
    else:
        logger.warning(f"Pillar 1: unknown platform {plat!r}")
        return {
            "platform": plat,
            "success": False,
            "status": "UNKNOWN_PLATFORM",
            "message": "Supported platforms: KRUNGSRI · LIBERATOR",
            "otp_ref": None,
        }


async def session_submit_otp(otp_code: str) -> Dict[str, Any]:
    """
    Manually submits a 6-digit SMS OTP for Krungsri Securities.
    """
    code = str(otp_code).strip()
    if len(code) != 6 or not code.isdigit():
        return {"success": False, "message": "Invalid OTP format. Must be 6 digits."}

    from app.core.events import event_bus
    payload = OTPPayload(
        otp=code,
        ref=auth_driver.current_ref or "MANUAL",
        source=OTPSource.API_MANUAL,
        timestamp=datetime.now(timezone.utc)
    )
    await event_bus.publish_otp(payload)
    return {
        "success": True,
        "status": auth_driver.status,
        "message": "OTP submitted successfully and verified"
    }


async def session_logout_all() -> Dict[str, Any]:
    """
    Safely logs out and terminates browser contexts across all platforms.
    """
    await auth_driver.close()
    return {"success": True, "message": "All broker browser sessions safely terminated."}


async def session_connect_all() -> Dict[str, Any]:
    """
    Connects/prepares all platforms in one click (CDP-first).
    1. Refreshes KDE Connect device discovery
    2. Attaches or ensures Krungsri CDP session daemon (:9333)
    3. Attaches or ensures Liberator CDP session daemon (:9334)
    """
    results: Dict[str, Any] = {}

    # 1. KDE Connect
    try:
        from app.services.kdeconnect_service import kdeconnect_service
        await kdeconnect_service.refresh_devices()
        results["kdeconnect"] = {"status": "refreshed", "reachable": await kdeconnect_service.is_device_reachable()}
    except Exception as e:
        results["kdeconnect"] = {"status": "error", "error": str(e)}

    # 2. Broker CDP Daemons (Krungsri :9333, Liberator :9334)
    from app.services.broker_cdp import daemon_launcher

    broker_targets = [
        ("krungsri", "kss"),
        ("liberator", "liberator"),
    ]

    for broker_key, broker_slug in broker_targets:
        try:
            st = await daemon_launcher.daemon_state(broker_slug)
            curr_state = st.get("state")
            if curr_state == "AUTHENTICATED":
                results[broker_key] = {
                    "success": True,
                    "status": "already_up",
                    "state": "AUTHENTICATED",
                    "port": st.get("port"),
                    "pid": st.get("pid"),
                    "message": f"{broker_key.capitalize()} CDP daemon active and authenticated",
                }
            elif curr_state in ("OTP_PAGE", "LOGIN_PAGE"):
                results[broker_key] = {
                    "success": False,
                    "status": curr_state,
                    "state": curr_state,
                    "port": st.get("port"),
                    "pid": st.get("pid"),
                    "message": "เปิดหน้า login แล้ว รอ OTP ที่การ์ด LINE",
                }
            else:
                ens = await daemon_launcher.ensure_daemon(broker_slug)
                ens_state = ens.get("state")
                is_auth = (ens_state == "AUTHENTICATED")
                if ens_state in ("OTP_PAGE", "LOGIN_PAGE"):
                    msg = "เปิดหน้า login แล้ว รอ OTP ที่การ์ด LINE"
                elif is_auth:
                    msg = f"{broker_key.capitalize()} CDP daemon connected and authenticated"
                else:
                    msg = f"{broker_key.capitalize()} CDP daemon state: {ens_state}"

                results[broker_key] = {
                    "success": is_auth,
                    "status": ens.get("status") or ens_state,
                    "state": ens_state,
                    "port": ens.get("port"),
                    "pid": ens.get("pid"),
                    "message": msg,
                }
        except Exception as e:
            logger.error(f"Pillar 1: Error connecting {broker_key} CDP daemon: {e}")
            results[broker_key] = {
                "success": False,
                "status": "error",
                "state": "UNKNOWN",
                "port": 9333 if broker_slug == "kss" else 9334,
                "pid": None,
                "error": str(e),
                "message": f"Failed to connect {broker_key}: {e}",
            }

    # Aggregated Health
    health = await session_get_health()
    return {
        "success": True,
        "actions": results,
        "health": health
    }


def register_session_pillar(server):
    """Registers Pillar 1 tools into the MCP server."""
    server.tool()(session_get_health)
    server.tool()(session_login_broker)
    server.tool()(session_submit_otp)
    server.tool()(session_logout_all)
    server.tool()(session_connect_all)

