"""Persistent default broker/account routing for Short (Cover) vs Long (Buy/Sell)."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_DEFAULT_PATH = os.path.join("data", "account_routing.json")

_BROKER_ALIASES: Dict[str, str] = {
    "kss": "kss",
    "krungsri": "kss",
    "trademan": "kss",
    "lib": "lib",
    "liberator": "lib",
}

_ALLOWED_SHORT_ROUTES: Tuple[Tuple[str, str], ...] = (("kss", "margin"),)
_ALLOWED_LONG_ROUTES: Tuple[Tuple[str, str], ...] = (
    ("lib", "cash"),
    ("kss", "cash"),
    ("kss", "margin"),
)

ROUTE_OPTIONS: Dict[str, List[Dict[str, str]]] = {
    "short": [
        {"broker": "kss", "account": "margin", "value": "kss:margin", "label": "Krungsri · Margin (<REDACTED_KSS_ACCT>)"},
    ],
    "long": [
        {"broker": "lib", "account": "cash", "value": "lib:cash", "label": "Liberator · Cash Balance"},
        {"broker": "kss", "account": "cash", "value": "kss:cash", "label": "Krungsri · Cash (<REDACTED_KSS_ACCT>)"},
        {"broker": "kss", "account": "margin", "value": "kss:margin", "label": "Krungsri · Margin (<REDACTED_KSS_ACCT>)"},
    ],
}

DEFAULT_ROUTING: Dict[str, Any] = {
    "short_broker": "kss",
    "short_account": "margin",
    "long_broker": "lib",
    "long_account": "cash",
    "updated_at": None,
}


def _resolve_path(path: Optional[str] = None) -> Path:
    raw = path or os.environ.get("ACCOUNT_ROUTING_PATH") or _DEFAULT_PATH
    return Path(raw)


def _norm_broker(val: Any, fallback: str) -> str:
    raw = str(val or "").strip().lower()
    if raw in _BROKER_ALIASES:
        return _BROKER_ALIASES[raw]
    if not raw:
        return fallback
    raise ValueError(f"ไม่รองรับโบรกเกอร์ '{val}'")


def _norm_account(val: Any, fallback: str) -> str:
    raw = str(val or "").strip().lower()
    if raw in ("margin", "cash"):
        return raw
    if not raw:
        return fallback
    raise ValueError(f"ไม่รองรับประเภทบัญชี '{val}'")


def _validate_pair(kind: str, broker: str, account: str) -> Tuple[str, str]:
    allowed = _ALLOWED_SHORT_ROUTES if kind == "short" else _ALLOWED_LONG_ROUTES
    pair = (broker, account)
    if pair not in allowed:
        if kind == "short":
            raise ValueError("บัญชีหลักสำหรับ Short (Cover) รองรับเฉพาะ Krungsri Margin (<REDACTED_KSS_ACCT>)")
        raise ValueError(f"บัญชีหลักสำหรับ Long ไม่รองรับ {broker.upper()} {account}")
    return pair


def get_account_routing(path: Optional[str] = None) -> Dict[str, Any]:
    """Load persisted Short/Long primary account routing (fail-safe to defaults)."""
    cfg = dict(DEFAULT_ROUTING)
    fp = _resolve_path(path)
    if fp.exists():
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                sb = _norm_broker(raw.get("short_broker"), cfg["short_broker"])
                sa = _norm_account(raw.get("short_account"), cfg["short_account"])
                lb = _norm_broker(raw.get("long_broker"), cfg["long_broker"])
                la = _norm_account(raw.get("long_account"), cfg["long_account"])
                cfg["short_broker"], cfg["short_account"] = _validate_pair("short", sb, sa)
                cfg["long_broker"], cfg["long_account"] = _validate_pair("long", lb, la)
                cfg["updated_at"] = raw.get("updated_at")
        except Exception as exc:
            logger.warning("Failed to load account_routing from %s (%s); using defaults", fp, exc)
            cfg = dict(DEFAULT_ROUTING)
    return {
        **cfg,
        "options": ROUTE_OPTIONS,
    }


def save_account_routing(
    *,
    short_broker: str = "kss",
    short_account: str = "margin",
    long_broker: str = "lib",
    long_account: str = "cash",
    path: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate and atomically persist primary Short/Long account routing."""
    sb = _norm_broker(short_broker, "kss")
    sa = _norm_account(short_account, "margin")
    lb = _norm_broker(long_broker, "lib")
    la = _norm_account(long_account, "cash")
    sb, sa = _validate_pair("short", sb, sa)
    lb, la = _validate_pair("long", lb, la)

    payload: Dict[str, Any] = {
        "short_broker": sb,
        "short_account": sa,
        "long_broker": lb,
        "long_account": la,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    fp = _resolve_path(path)
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(fp)
    return {
        **payload,
        "options": ROUTE_OPTIONS,
    }


def default_route_for_side(side: str, path: Optional[str] = None) -> Tuple[str, str]:
    """Return ``(broker, account)`` default for the given order side."""
    cfg = get_account_routing(path=path)
    clean_side = (side or "").strip().lower()
    if clean_side in ("short", "cover"):
        return str(cfg["short_broker"]), str(cfg["short_account"])
    return str(cfg["long_broker"]), str(cfg["long_account"])
