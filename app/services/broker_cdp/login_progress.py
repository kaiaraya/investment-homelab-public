# -*- coding: utf-8 -*-
"""Cross-process login progress store (`data/login_state_<broker>.json`).

Shared atomically between standalone `scripts/broker_cdp/session_daemon.py`
processes and the FastAPI gateway (`app.main:app`) so MiniApp receives real-time
login steps, errors (e.g. Liberator /va/login timeouts), and active-login locks
without colliding on the same CDP Chrome tab.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("broker_cdp_login_progress")

ACTIVE_LOGIN_PHASES = frozenset(
    {
        "launching_chrome",
        "checking_session",
        "submitting_credentials",
        "retrying_submit",
        "waiting_otp",
        "verifying_otp",
        "otp_rejected",
        "opening_tabs",
    }
)

STEP_BY_PHASE = {
    "idle": 0,
    "launching_chrome": 1,
    "checking_session": 1,
    "submitting_credentials": 2,
    "retrying_submit": 2,
    "waiting_otp": 3,
    "verifying_otp": 3,
    "otp_rejected": 3,
    "opening_tabs": 4,
    "authenticated": 4,
    "error": 0,
}

DEFAULT_STALE_SECONDS = 45.0


def canonical_broker_key(broker: str) -> str:
    b = (broker or "").strip().lower()
    if b in ("lib", "liberator"):
        return "liberator"
    if b in ("kss", "krungsri", "trademan"):
        return "kss"
    return b or "unknown"


def miniapp_broker_slug(broker: str) -> str:
    return "lib" if canonical_broker_key(broker) == "liberator" else "kss"


def _data_dir() -> Path:
    try:
        from app.core.config import settings

        raw = getattr(settings, "DATA_DIR", "data") or "data"
    except Exception:
        raw = "data"
    p = Path(raw)
    if not p.is_absolute():
        repo_root = Path(__file__).resolve().parents[3]
        p = repo_root / p
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_file_for(broker: str) -> Path:
    return _data_dir() / f"login_state_{canonical_broker_key(broker)}.json"


def set_login_progress(
    broker: str,
    phase: str,
    message: str = "",
    *,
    step: Optional[int] = None,
    error: Optional[str] = None,
    ref: Optional[str] = None,
    attempt: int = 1,
    pid: Optional[int] = None,
) -> Dict[str, Any]:
    """Atomically persist the latest login milestone for `broker`."""
    canon = canonical_broker_key(broker)
    now = time.time()
    payload: Dict[str, Any] = {
        "broker": miniapp_broker_slug(canon),
        "canonical_broker": canon,
        "phase": phase,
        "step": int(step if step is not None else STEP_BY_PHASE.get(phase, 0)),
        "message": message,
        "error": error,
        "ref": ref,
        "attempt": int(attempt),
        "pid": pid if pid is not None else os.getpid(),
        "updated_at": now,
    }
    path = state_file_for(canon)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.debug("Could not write login progress for %s: %s", canon, exc)
    return payload


def get_login_progress(broker: str, *, max_age_seconds: Optional[float] = 900.0) -> Optional[Dict[str, Any]]:
    """Read persisted login progress for `broker`, returning None if absent or stale."""
    path = state_file_for(broker)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        updated_at = float(data.get("updated_at") or 0.0)
        age = max(0.0, time.time() - updated_at)
        if max_age_seconds is not None and age > max_age_seconds:
            return None
        data["age_seconds"] = round(age, 1)
        data["is_active"] = bool(data.get("phase") in ACTIVE_LOGIN_PHASES and age <= 330.0)
        return data
    except (OSError, ValueError, TypeError) as exc:
        logger.debug("Could not read login progress for %s: %s", broker, exc)
        return None


def clear_login_progress(broker: str) -> None:
    """Remove login progress file for `broker`."""
    path = state_file_for(broker)
    if path.is_file():
        try:
            path.unlink()
        except OSError:
            pass


def _pid_alive(pid: Optional[int]) -> bool:
    if not pid or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_login_active(broker: str, stale_seconds: float = DEFAULT_STALE_SECONDS) -> bool:
    """True when another process is actively running the login flow for `broker`.

    Used to prevent `trigger_cdp_login()` from colliding with an in-flight
    `session_daemon.py` login attempt on the same Chrome tab.
    """
    data = get_login_progress(broker, max_age_seconds=None)
    if not data:
        return False
    phase = str(data.get("phase") or "")
    if phase not in ACTIVE_LOGIN_PHASES:
        return False
    age = float(data.get("age_seconds") or 0.0)
    # Waiting for OTP can stay quiet for up to 300s (heartbeats update every ~15s,
    # so allow stale_seconds unless pid is dead).
    pid = data.get("pid")
    if isinstance(pid, int) and not _pid_alive(pid):
        return False
    limit = 310.0 if phase in ("waiting_otp", "verifying_otp", "otp_rejected") else stale_seconds
    return age <= limit


def all_login_progress() -> Dict[str, Optional[Dict[str, Any]]]:
    """Return MiniApp-keyed login progress dict for `kss` and `lib`."""
    return {
        "kss": get_login_progress("kss"),
        "lib": get_login_progress("liberator"),
    }
