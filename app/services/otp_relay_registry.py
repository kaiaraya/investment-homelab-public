"""
OTP Relay Registry — broker attribution + primary-DB pinning (LINE-RCA-010)
===========================================================================

Root cause this module fixes (2026-10-08, live evidence):

1. **Wrong database (fatal).** `webhook_line.py` depends on `get_db()`, which is
   *platform-aware* (`get_session_factory(None)` → `settings.TRADING_PLATFORM`).
   With `TRADING_PLATFORM=CLICK2WIN` the webhook wrote `LINE_OTP_RECEIVED` into
   `investment_homelab_click2win.db` while **every** driver polls the *primary*
   database (`AsyncSessionLocal`) → the row was invisible → `TIMEOUT (180s)` even
   though the operator typed the code. Proven by the orphan row in the sandbox DB
   (`id=26 LINE_OTP_RECEIVED … 922412 (Ref: N/A)`).
   The relay channel is broker-agnostic, so it is **pinned to the primary DB** here.

2. **No broker attribution.** The webhook ACK hard-coded "Krungsri Securities" and
   resolved `ref` only from `auth_driver.current_ref`, so a Liberator OTP was
   labelled Krungsri with `Ref: N/A`. Requests are now recorded as an
   `OTP_REQUESTED` marker carrying `broker=` / `ref=`; the reply echoes the broker
   that actually asked (**latest-request-wins**, operator decision 2026-10-08).

Zero-Contradiction: `auth_driver.OTP_SESSION_LOG_PATTERN` still parses the
`LINE_OTP_RECEIVED` details written here — the shape only gained a trailing
`(Broker: X)` tag. Rows without the tag (legacy / KDE) stay acceptable (lenient).

Broker Knowledge Isolation: this module only *labels* + *correlates*; it never
carries broker-specific DOM/session assumptions.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy import select

from app.db.models import SessionLog
from app.services.redaction import redact_otp_details

logger = logging.getLogger(__name__)

# --- Event types (single source of truth for the relay channel) --------------
EVENT_OTP_REQUESTED = "OTP_REQUESTED"
EVENT_OTP_RECEIVED = "LINE_OTP_RECEIVED"

# A pending request is honoured for slightly longer than the 180s OTP wait, so a
# slow operator still gets honest attribution instead of a silent `Ref: N/A`.
DEFAULT_PENDING_TTL_SECONDS = 300

# Canonical broker labels (moved here from `line_service.push_otp_request` so the
# push and the ACK can never drift apart — Zero-Contradiction).
# De-scoped apps are deliberately absent (CDP-UNIFY 8): Click2Win / TradeMan /
# QuickTrade / Settrade OpenAPI are gone, so an unknown tag falls through to the
# raw tag in `broker_label()` instead of being dressed up as a phantom app.
BROKER_LABELS = {
    "KRUNGSRI": "Krungsri (Streaming)",
    "TRADEMAN": "Krungsri (Streaming)",
    "LIBERATOR": "Liberator",
}

_REQUEST_BROKER_RE = re.compile(r"broker=([A-Za-z0-9_]+)")
_REQUEST_REF_RE = re.compile(r"\bref=(\S+)")
_OTP_BROKER_RE = re.compile(r"\(Broker:\s*([A-Za-z0-9_]+)\)")

# --- In-Memory & File-backed State (Zero-DB Single Source of Truth) ------------
_IN_MEMORY_PENDING_REQUEST: Optional[PendingOtpRequest] = None
_IN_MEMORY_LATEST_OTP: Optional[Dict[str, Any]] = None
_OTP_COUNTER: int = 0


def _get_otp_state_file() -> Path:
    from app.core.config import settings
    p = Path(getattr(settings, "DATA_DIR", "data")) / "otp_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _save_state_to_file(data: dict) -> None:
    path = _get_otp_state_file()
    tmp_path = path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp_path.replace(path)
    except Exception as e:
        logger.warning(f"[OTP RELAY] Could not write state file: {e}")


def _load_state_from_file() -> dict:
    path = _get_otp_state_file()
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"[OTP RELAY] Could not read state file: {e}")
        return {}


def clear_otp_state() -> None:
    """Reset in-memory and file state (used by test fixtures and clean startup)."""
    global _IN_MEMORY_PENDING_REQUEST, _IN_MEMORY_LATEST_OTP, _OTP_COUNTER
    _IN_MEMORY_PENDING_REQUEST = None
    _IN_MEMORY_LATEST_OTP = None
    _OTP_COUNTER = 0
    path = _get_otp_state_file()
    if path.is_file():
        try:
            path.unlink()
        except OSError:
            pass


# --------------------------------------------------------------------- pure ----

def broker_label(broker: Optional[str]) -> Optional[str]:
    """Human-facing label for a broker tag (`LIBERATOR` -> `Liberator WebTrade`)."""
    if not broker:
        return None
    tag = str(broker).strip()
    if not tag:
        return None
    return BROKER_LABELS.get(tag.upper(), tag)


def format_request_details(
    broker: Optional[str],
    ref: Optional[str],
    ttl_seconds: int = DEFAULT_PENDING_TTL_SECONDS,
) -> str:
    """Details string for an `OTP_REQUESTED` marker (latest-request-wins)."""
    return (
        f"OTP requested by broker={(broker or '').upper()} "
        f"ref={ref or 'N/A'} ttl={int(ttl_seconds)}s"
    )


def format_otp_details(
    otp: str,
    ref: Optional[str] = None,
    broker: Optional[str] = None,
) -> str:
    """
    Details string for a `LINE_OTP_RECEIVED` row.

    The shape stays compatible with ``auth_driver.OTP_SESSION_LOG_PATTERN`` (a
    tolerant ``Received...OTP...: NNNNNN`` regex) so the channel-gated DB waits keep
    working; the trailing ``(Broker: X)`` tag is what lets each driver ignore the
    other broker's OTP (LINE-RCA-010).
    """
    tag = f" (Broker: {broker.upper()})" if broker else ""
    return f"Received OTP 6-digits from LINE: {otp} (Ref: {ref or 'N/A'}){tag}"


def parse_request_broker(details: Optional[str]) -> Optional[str]:
    """Extract the broker tag from an `OTP_REQUESTED` marker (upper-cased)."""
    m = _REQUEST_BROKER_RE.search(details or "")
    return m.group(1).upper() if m else None


def parse_request_ref(details: Optional[str]) -> Optional[str]:
    """Extract the ref from an `OTP_REQUESTED` marker (`N/A` -> None)."""
    m = _REQUEST_REF_RE.search(details or "")
    if not m:
        return None
    val = m.group(1).strip()
    return None if val.upper() in ("N/A", "NONE", "-", "") else val


def parse_otp_broker(details: Optional[str]) -> Optional[str]:
    """
    Extract the `(Broker: X)` tag from a `LINE_OTP_RECEIVED` row.

    ``None`` means "untagged" (legacy row / KDE SMS) — callers must treat that as
    *acceptable* so historical rows keep working (lenient, never fail-closed here).
    """
    m = _OTP_BROKER_RE.search(details or "")
    return m.group(1).upper() if m else None


# Broker tags each driver accepts as "its own". Labels only — no DOM/session
# behaviour crosses brokers (Broker Knowledge Isolation).
BROKER_ALIASES: Dict[str, tuple] = {
    "KRUNGSRI": ("KRUNGSRI", "TRADEMAN", "KSS", "SETTRADE"),
    "TRADEMAN": ("KRUNGSRI", "TRADEMAN", "KSS", "SETTRADE"),
    "KSS": ("KRUNGSRI", "TRADEMAN", "KSS", "SETTRADE"),
    "SETTRADE": ("KRUNGSRI", "TRADEMAN", "KSS", "SETTRADE"),
    "LIBERATOR": ("LIBERATOR", "LIB"),
    "LIB": ("LIBERATOR", "LIB"),
}


def broker_matches(expected: Optional[str], actual: Optional[str]) -> bool:
    """
    True when an OTP tagged `actual` may be consumed by the `expected` broker.

    * ``actual is None``  → untagged (legacy / KDE SMS) → **lenient accept**, so
      historical rows and the dormant KDE fallback keep working.
    * otherwise the tag must name the same broker (aliases allowed).
    """
    if not actual:
        return True
    exp = str(expected or "").upper()
    return str(actual).upper() in BROKER_ALIASES.get(exp, (exp,))


# ---------------------------------------------------------------- DB-backed ----

@dataclass
class PendingOtpRequest:
    """The most recent broker OTP request still inside its TTL window."""

    broker: str
    ref: Optional[str]
    label: str
    created_at: Optional[datetime] = None
    submitted_at: Optional[datetime] = None

    @property
    def is_submitted(self) -> bool:
        return self.submitted_at is not None

    @property
    def is_expired(self) -> bool:
        if self.created_at is None:
            return False
        created_utc = self.created_at if self.created_at.tzinfo else self.created_at.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - created_utc).total_seconds() > DEFAULT_PENDING_TTL_SECONDS


def _primary_session():
    """Primary-DB session factory — imported lazily so test overrides apply."""
    from app.db.session import AsyncSessionLocal

    return AsyncSessionLocal()


async def record_request(
    broker: str,
    ref: Optional[str] = None,
    ttl_seconds: int = DEFAULT_PENDING_TTL_SECONDS,
) -> bool:
    """
    Persist an OTP request marker into In-Memory state + data/otp_state.json (Zero-DB).

    Called by `line_service.push_otp_request()` immediately before the push, so the
    webhook can attribute the operator's reply to the broker that actually asked.
    Best-effort: a failure never aborts the login flow.
    """
    global _IN_MEMORY_PENDING_REQUEST
    broker_tag = (broker or "").strip().upper()
    now_utc = datetime.now(timezone.utc)
    details = format_request_details(broker_tag, ref, ttl_seconds)

    req = PendingOtpRequest(
        broker=broker_tag,
        ref=ref,
        label=broker_label(broker_tag) or broker_tag,
        created_at=now_utc,
    )
    _IN_MEMORY_PENDING_REQUEST = req

    state = _load_state_from_file()
    state.pop("pending_cleared_at", None)
    state["pending_request"] = {
        "broker": req.broker,
        "ref": req.ref,
        "label": req.label,
        "details": details,
        "created_at": now_utc.isoformat(),
        "ttl_seconds": int(ttl_seconds),
    }
    _save_state_to_file(state)
    logger.info(f"[OTP RELAY] pending request recorded (Zero-DB) -> {details}")
    return True


def _parse_iso_utc(raw: Any) -> Optional[datetime]:
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


async def resolve_pending_request(
    ttl_seconds: int = DEFAULT_PENDING_TTL_SECONDS,
) -> Optional[PendingOtpRequest]:
    """
    Latest-request-wins: return newest request from data/otp_state.json, In-Memory, or DB fallback.

    When data/otp_state.json exists on disk it is authoritative across processes so an
    external `session_daemon.py` process clearing `pending_request` immediately clears any
    stale `_IN_MEMORY_PENDING_REQUEST` cached in the FastAPI gateway process.
    """
    global _IN_MEMORY_PENDING_REQUEST

    path = _get_otp_state_file()
    cleared_at_utc: Optional[datetime] = None

    if path.is_file():
        state = _load_state_from_file()
        cleared_at_utc = _parse_iso_utc(state.get("pending_cleared_at"))
        p = state.get("pending_request")
        if p and isinstance(p, dict):
            try:
                created_utc = _parse_iso_utc(p.get("created_at"))
                if created_utc is not None:
                    submitted_dt = _parse_iso_utc(p.get("submitted_at"))
                    if (
                        submitted_dt is None
                        and _IN_MEMORY_PENDING_REQUEST is not None
                        and broker_matches(p.get("broker"), _IN_MEMORY_PENDING_REQUEST.broker)
                        and _IN_MEMORY_PENDING_REQUEST.created_at is not None
                        and abs(
                            _IN_MEMORY_PENDING_REQUEST.created_at.timestamp()
                            - created_utc.timestamp()
                        )
                        < 1.0
                    ):
                        submitted_dt = _IN_MEMORY_PENDING_REQUEST.submitted_at
                    age = (datetime.now(timezone.utc) - created_utc).total_seconds()
                    if age <= ttl_seconds:
                        req = PendingOtpRequest(
                            broker=p["broker"],
                            ref=p.get("ref"),
                            label=p.get("label") or broker_label(p["broker"]) or p["broker"],
                            created_at=created_utc,
                            submitted_at=submitted_dt,
                        )
                        _IN_MEMORY_PENDING_REQUEST = req
                        return req
                    _IN_MEMORY_PENDING_REQUEST = None
            except Exception as e:
                logger.debug(f"[OTP RELAY] Could not parse file pending_request: {e}")
        else:
            # File exists and has no active pending_request: authoritative clear across processes
            _IN_MEMORY_PENDING_REQUEST = None
    else:
        req = _IN_MEMORY_PENDING_REQUEST
        if req is not None:
            if req.is_expired:
                _IN_MEMORY_PENDING_REQUEST = None
            else:
                return req

    # Fallback to DB (for legacy / tests that seed SessionLog directly)
    try:
        async with _primary_session() as db:
            stmt = (
                select(SessionLog)
                .where(SessionLog.event_type == EVENT_OTP_REQUESTED)
                .order_by(SessionLog.created_at.desc(), SessionLog.id.desc())
                .limit(1)
            )
            row = (await db.execute(stmt)).scalar_one_or_none()
            if row and row.details:
                broker = parse_request_broker(row.details)
                if broker:
                    created = row.created_at
                    if created is not None:
                        created_utc = created if created.tzinfo else created.replace(tzinfo=timezone.utc)
                        age = (datetime.now(timezone.utc) - created_utc).total_seconds()
                        after_clear = (
                            cleared_at_utc is None or created_utc.timestamp() > cleared_at_utc.timestamp()
                        )
                        if age <= ttl_seconds and after_clear:
                            return PendingOtpRequest(
                                broker=broker,
                                ref=parse_request_ref(row.details),
                                label=broker_label(broker) or broker,
                                created_at=created,
                            )
    except Exception:
        pass

    return None


async def get_pending_request(
    ttl_seconds: int = DEFAULT_PENDING_TTL_SECONDS,
) -> Optional[PendingOtpRequest]:
    """Convenience alias for resolve_pending_request."""
    return await resolve_pending_request(ttl_seconds=ttl_seconds)


def clear_pending_request(broker: Optional[str] = None) -> None:
    """Clear pending OTP request from In-Memory and data/otp_state.json when broker matches."""
    global _IN_MEMORY_PENDING_REQUEST
    if _IN_MEMORY_PENDING_REQUEST is not None:
        if broker is None or broker_matches(broker, _IN_MEMORY_PENDING_REQUEST.broker):
            _IN_MEMORY_PENDING_REQUEST = None

    state = _load_state_from_file()
    pending = state.get("pending_request")
    if isinstance(pending, dict):
        pending_broker = pending.get("broker")
        if broker is None or broker_matches(broker, pending_broker):
            state.pop("pending_request", None)
            state["pending_cleared_at"] = datetime.now(timezone.utc).isoformat()
            _save_state_to_file(state)


def mark_pending_submitted(
    broker: Optional[str] = None,
    *,
    submitted: bool = True,
) -> Optional[PendingOtpRequest]:
    """Mark/unmark the pending OTP request as submitted (for live verifying UI state)."""
    global _IN_MEMORY_PENDING_REQUEST
    now_utc = datetime.now(timezone.utc) if submitted else None
    if _IN_MEMORY_PENDING_REQUEST is not None:
        if broker is None or broker_matches(broker, _IN_MEMORY_PENDING_REQUEST.broker):
            _IN_MEMORY_PENDING_REQUEST.submitted_at = now_utc

    state = _load_state_from_file()
    pending = state.get("pending_request")
    if isinstance(pending, dict):
        pending_broker = pending.get("broker")
        if broker is None or broker_matches(broker, pending_broker):
            if submitted and now_utc is not None:
                pending["submitted_at"] = now_utc.isoformat()
            else:
                pending.pop("submitted_at", None)
            state["pending_request"] = pending
            _save_state_to_file(state)
    return _IN_MEMORY_PENDING_REQUEST


async def record_otp_received(
    otp: str,
    ref: Optional[str] = None,
    broker: Optional[str] = None,
) -> bool:
    """
    Persist a LINE_OTP_RECEIVED marker into In-Memory state + data/otp_state.json (Zero-DB).
    """
    global _IN_MEMORY_LATEST_OTP, _OTP_COUNTER
    state = _load_state_from_file()
    file_seq = int((state.get("latest_otp") or {}).get("seq", 0) or 0)
    _OTP_COUNTER = max(_OTP_COUNTER, file_seq) + 1
    broker_tag = (broker or "").strip().upper()
    now_utc = datetime.now(timezone.utc)
    details = format_otp_details(otp, ref, broker)

    otp_data = {
        "otp": otp,
        "ref": ref,
        "broker": broker_tag,
        "details": details,
        "seq": _OTP_COUNTER,
        "created_at": now_utc.isoformat(),
        "timestamp": now_utc.timestamp(),
    }
    _IN_MEMORY_LATEST_OTP = otp_data

    state["latest_otp"] = otp_data
    _save_state_to_file(state)
    logger.info(f"[OTP RELAY] LINE_OTP_RECEIVED recorded (Zero-DB) -> {redact_otp_details(details)}")
    return True


def get_latest_otp_seq() -> int:
    """Highest OTP sequence counter (In-Memory or from file)."""
    mem_seq = int(_IN_MEMORY_LATEST_OTP.get("seq", 0) or 0) if _IN_MEMORY_LATEST_OTP else 0
    state = _load_state_from_file()
    latest = state.get("latest_otp")
    file_seq = int(latest.get("seq", 0) or 0) if (latest and isinstance(latest, dict)) else 0
    return max(mem_seq, file_seq)


def get_latest_otp_match(
    broker: str,
    since_seq: int = 0,
    since_time: Optional[datetime] = None,
    max_age_seconds: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Return latest OTP match if it satisfies broker attribution and freshness."""
    otp_data = _IN_MEMORY_LATEST_OTP
    state = _load_state_from_file()
    file_otp = state.get("latest_otp")
    if isinstance(file_otp, dict):
        if not otp_data or int(file_otp.get("seq", 0) or 0) >= int(otp_data.get("seq", 0) or 0):
            otp_data = file_otp

    if not otp_data or not isinstance(otp_data, dict):
        return None

    seq = int(otp_data.get("seq", 0))
    if since_seq and seq <= since_seq:
        return None

    if max_age_seconds is not None and otp_data.get("timestamp") is not None:
        try:
            if (datetime.now(timezone.utc).timestamp() - float(otp_data["timestamp"])) > max_age_seconds:
                return None
        except (TypeError, ValueError):
            pass

    if since_time:
        created_str = otp_data.get("created_at")
        if created_str:
            try:
                created_dt = datetime.fromisoformat(created_str)
                since_utc = since_time if since_time.tzinfo else since_time.replace(tzinfo=timezone.utc)
                if created_dt < since_utc:
                    return None
            except Exception:
                pass

    actual_broker = otp_data.get("broker")
    if broker_matches(broker, actual_broker):
        return otp_data
    return None

