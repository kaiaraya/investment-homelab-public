"""Persistent SET trading rules config (Split Opening/Nego Short rules, auto -10% scan & runtime reject cache)."""

from __future__ import annotations

import csv
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union
from zoneinfo import ZoneInfo

from app.core.config import settings

logger = logging.getLogger(__name__)

_BKK_TZ = ZoneInfo("Asia/Bangkok")
_DEFAULT_PATH = os.path.join("data", "trading_rules.json")
_SYMBOL_SPLIT_RE = re.compile(r"[\s,;]+")

DEFAULT_RULES: Dict[str, Any] = {
    "short_open_uptick_ticks": 1,  # Opening Short: +1 Strict Uptick (Last + 1 tick -> Offer 2 when Last == Offer 1)
    "short_uptick_ticks": 0,       # Negotiation Short: +0 Zero-Plus (>= Last)
    "auto_minus10_strict": True,   # Auto-promote yesterday <= -10% drop symbols to +1 Strict Uptick during Nego
    "strict_uptick_symbols": [],   # Legacy / optional override list
    "updated_at": None,
}

RULE_OPTIONS: List[Dict[str, Any]] = [
    {
        "ticks": 0,
        "code": "zero_plus",
        "label": "+0 Zero-Plus (≥ Last)",
        "description": "เกณฑ์ปกติ SET (16 พ.ย. 2569): ขายชอร์ตได้ที่ราคาเท่ากับหรือสูงกว่า Last",
    },
    {
        "ticks": 1,
        "code": "strict_uptick",
        "label": "+1 Strict Uptick (> Last)",
        "description": "เกณฑ์เข้มงวด: ขายชอร์ตต้องสูงกว่า Last อย่างน้อย 1 ช่องราคา",
    },
]

_CACHE_SCAN_MEM: Dict[str, Any] = {
    "path": None,
    "mtime_ns": None,
    "threshold": None,
    "result": None,
}

_RUNTIME_STRICT_BY_DATE: Dict[str, set[str]] = {}

_UPTICK_REJECTION_PATTERNS = (
    "uptick",
    "zero-plus",
    "zero plus",
    "short sale",
    "short sell",
    "higher than last",
    "greater than last",
    "above the last",
    "above last",
    "เกณฑ์ขายชอร์ต",
    "กฎ short",
    "สูงกว่าราคาล่าสุด",
    "ไม่ต่ำกว่าราคาล่าสุด",
    "ราคาชอร์ต",
)


def _today_bkk() -> str:
    return datetime.now(_BKK_TZ).strftime("%Y-%m-%d")


def _resolve_path(path: Optional[str] = None) -> Path:
    raw = path or os.environ.get("TRADING_RULES_PATH") or _DEFAULT_PATH
    return Path(raw)


def normalize_ticks(val: Any, default: int = 0) -> int:
    if val is None or val == "":
        return default
    try:
        ticks = int(val)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"ค่า uptick_ticks ต้องเป็น 0 หรือ 1 (ได้รับ '{val}')") from exc
    if ticks not in (0, 1):
        raise ValueError(f"ค่า uptick_ticks รองรับเฉพาะ 0 (+0) หรือ 1 (+1) (ได้รับ {ticks})")
    return ticks


def normalize_symbol_list(raw: Union[None, str, Iterable[Any]]) -> List[str]:
    if raw is None:
        return []
    tokens: List[str] = []
    if isinstance(raw, str):
        tokens = _SYMBOL_SPLIT_RE.split(raw.strip())
    elif isinstance(raw, Iterable):
        for item in raw:
            if item is None:
                continue
            tokens.extend(_SYMBOL_SPLIT_RE.split(str(item).strip()))
    else:
        raise ValueError("strict_uptick_symbols ต้องเป็นรายการชื่อหุ้นหรือข้อความ")

    seen: set[str] = set()
    cleaned: List[str] = []
    for tok in tokens:
        sym = tok.strip().upper()
        if not sym or sym in seen:
            continue
        if not re.fullmatch(r"[A-Z0-9._-]{1,20}", sym):
            raise ValueError(f"ชื่อหุ้นไม่ถูกต้อง: '{tok}'")
        seen.add(sym)
        cleaned.append(sym)
    return cleaned


def scan_minus10_drop_symbols(
    *,
    cache_path: Optional[str] = None,
    drop_threshold_pct: float = -10.0,
) -> Dict[str, Any]:
    """Read last 2 trading closes from DailyScanner daily price CSV and flag drops <= -10%."""
    raw_path = str(
        cache_path
        if cache_path is not None
        else getattr(settings, "DAILY_PRICES_CACHE_CSV_PATH", "") or ""
    ).strip()
    empty = {"available": False, "as_of": None, "prev_date": None, "symbols": [], "details": []}
    if not raw_path:
        return empty
    fp = Path(raw_path)
    if not fp.exists() or not fp.is_file():
        return empty

    try:
        st = fp.stat()
        mtime_ns = st.st_mtime_ns
    except OSError:
        return empty

    if (
        _CACHE_SCAN_MEM["path"] == str(fp)
        and _CACHE_SCAN_MEM["mtime_ns"] == mtime_ns
        and _CACHE_SCAN_MEM["threshold"] == drop_threshold_pct
        and _CACHE_SCAN_MEM["result"] is not None
    ):
        return _CACHE_SCAN_MEM["result"]

    try:
        with fp.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            if not header or len(header) < 2:
                return empty
            prev_row: Optional[List[str]] = None
            last_row: Optional[List[str]] = None
            for row in reader:
                if row and row[0].strip():
                    prev_row = last_row
                    last_row = row
    except Exception as exc:
        logger.warning("Failed reading daily prices cache %s: %s", fp, exc)
        return empty

    if not prev_row or not last_row:
        return empty

    prev_date = prev_row[0].strip()
    last_date = last_row[0].strip()
    details: List[Dict[str, Any]] = []
    symbols: List[str] = []

    for idx, raw_sym in enumerate(header[1:], start=1):
        sym = str(raw_sym or "").strip().upper()
        if not sym or idx >= len(prev_row) or idx >= len(last_row):
            continue
        try:
            c_prev = float(prev_row[idx])
            c_last = float(last_row[idx])
        except (TypeError, ValueError):
            continue
        if c_prev <= 0 or c_last <= 0:
            continue
        pct = round((c_last - c_prev) / c_prev * 100.0, 2)
        if pct <= drop_threshold_pct:
            symbols.append(sym)
            details.append(
                {
                    "symbol": sym,
                    "pct_change": pct,
                    "prev_close": round(c_prev, 2),
                    "last_close": round(c_last, 2),
                    "prev_date": prev_date,
                    "as_of": last_date,
                    "source": "yesterday_minus10",
                }
            )

    details.sort(key=lambda d: (d["pct_change"], d["symbol"]))
    symbols = [d["symbol"] for d in details]
    result = {
        "available": True,
        "as_of": last_date,
        "prev_date": prev_date,
        "symbols": symbols,
        "details": details,
    }
    _CACHE_SCAN_MEM.update(
        {
            "path": str(fp),
            "mtime_ns": mtime_ns,
            "threshold": drop_threshold_pct,
            "result": result,
        }
    )
    return result


def mark_runtime_strict_symbol(symbol: str, *, date_str: Optional[str] = None) -> List[str]:
    """Record a symbol rejected by broker short-sale rules for strict +1 handling today."""
    clean = str(symbol or "").strip().upper()
    day = date_str or _today_bkk()
    for stale_day in [k for k in _RUNTIME_STRICT_BY_DATE if k != day]:
        _RUNTIME_STRICT_BY_DATE.pop(stale_day, None)
    bucket = _RUNTIME_STRICT_BY_DATE.setdefault(day, set())
    if clean:
        bucket.add(clean)
    return sorted(bucket)


def get_runtime_strict_symbols(*, date_str: Optional[str] = None) -> List[str]:
    day = date_str or _today_bkk()
    return sorted(_RUNTIME_STRICT_BY_DATE.get(day) or ())


def clear_runtime_strict_symbols() -> None:
    _RUNTIME_STRICT_BY_DATE.clear()


def is_short_uptick_rejection(error_code: Optional[str], error_text: Optional[str]) -> bool:
    """True when an order/amend failure indicates exchange/broker Short Sale uptick rejection."""
    code = str(error_code or "").strip().upper()
    text = str(error_text or "").strip().lower()
    if code in {"E_PIN", "E_BAD_PIN", "E_NO_PIN", "E_PIN_GUARD", "E_NO_SBL", "E_BAD_LOT", "E_KILL_SWITCH"}:
        return False
    if "pin" in text and ("wrong" in text or "invalid" in text or "incorrect" in text or "ไม่ถูกต้อง" in text):
        return False
    if code == "E_UPTICK_VIOLATION":
        return True
    if code == "E_VALIDATOR" and "uptick" in text:
        return True
    return any(pat in text for pat in _UPTICK_REJECTION_PATTERNS)


def _load_persisted_cfg(path: Optional[str] = None) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "short_open_uptick_ticks": DEFAULT_RULES["short_open_uptick_ticks"],
        "short_uptick_ticks": DEFAULT_RULES["short_uptick_ticks"],
        "auto_minus10_strict": DEFAULT_RULES["auto_minus10_strict"],
        "strict_uptick_symbols": list(DEFAULT_RULES["strict_uptick_symbols"]),
        "updated_at": DEFAULT_RULES["updated_at"],
    }
    fp = _resolve_path(path)
    if fp.exists():
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg["short_open_uptick_ticks"] = normalize_ticks(
                    raw.get("short_open_uptick_ticks"),
                    DEFAULT_RULES["short_open_uptick_ticks"],
                )
                cfg["short_uptick_ticks"] = normalize_ticks(
                    raw.get("short_uptick_ticks"),
                    DEFAULT_RULES["short_uptick_ticks"],
                )
                if "auto_minus10_strict" in raw:
                    cfg["auto_minus10_strict"] = bool(raw.get("auto_minus10_strict"))
                cfg["strict_uptick_symbols"] = normalize_symbol_list(raw.get("strict_uptick_symbols"))
                cfg["updated_at"] = raw.get("updated_at")
        except Exception as exc:
            logger.warning("Failed to load trading_rules from %s (%s); using defaults", fp, exc)
            cfg = {
                "short_open_uptick_ticks": DEFAULT_RULES["short_open_uptick_ticks"],
                "short_uptick_ticks": DEFAULT_RULES["short_uptick_ticks"],
                "auto_minus10_strict": DEFAULT_RULES["auto_minus10_strict"],
                "strict_uptick_symbols": list(DEFAULT_RULES["strict_uptick_symbols"]),
                "updated_at": None,
            }
    return cfg


def get_rules(path: Optional[str] = None) -> Dict[str, Any]:
    """Load persisted trading rules merged with auto -10% scan and runtime strict symbols."""
    cfg = _load_persisted_cfg(path=path)
    scan = scan_minus10_drop_symbols() if cfg["auto_minus10_strict"] else {
        "available": False,
        "as_of": None,
        "prev_date": None,
        "symbols": [],
        "details": [],
    }
    runtime_syms = get_runtime_strict_symbols()
    combined_auto = sorted(set(scan.get("symbols") or ()) | set(runtime_syms))
    return {
        **cfg,
        "auto_strict_symbols": combined_auto,
        "auto_strict_details": scan.get("details") or [],
        "auto_strict_as_of": scan.get("as_of"),
        "runtime_strict_symbols": runtime_syms,
        "options": RULE_OPTIONS,
    }


def save_rules(
    *,
    short_uptick_ticks: Any = 0,
    short_open_uptick_ticks: Any = None,
    auto_minus10_strict: Optional[bool] = None,
    strict_uptick_symbols: Union[None, str, Iterable[Any]] = None,
    path: Optional[str] = None,
) -> Dict[str, Any]:
    """Validate and atomically persist SET Short Sell trading rules."""
    existing = _load_persisted_cfg(path=path)
    open_ticks = (
        normalize_ticks(short_open_uptick_ticks, existing["short_open_uptick_ticks"])
        if short_open_uptick_ticks is not None
        else existing["short_open_uptick_ticks"]
    )
    nego_ticks = normalize_ticks(short_uptick_ticks, 0)
    auto_flag = bool(auto_minus10_strict) if auto_minus10_strict is not None else existing["auto_minus10_strict"]
    symbols = normalize_symbol_list(strict_uptick_symbols) if strict_uptick_symbols is not None else existing["strict_uptick_symbols"]

    payload: Dict[str, Any] = {
        "short_open_uptick_ticks": open_ticks,
        "short_uptick_ticks": nego_ticks,
        "auto_minus10_strict": auto_flag,
        "strict_uptick_symbols": symbols,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    fp = _resolve_path(path)
    fp.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp.with_name(f".{fp.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(fp)
    return get_rules(path=path)


def get_short_uptick_detail(
    symbol: Optional[str] = None,
    *,
    context: str = "nego",
    path: Optional[str] = None,
) -> Tuple[int, Optional[str]]:
    """Return ``(required_ticks, reason)`` for ``open`` or ``nego`` context."""
    rules = get_rules(path=path)
    clean = str(symbol or "").strip().upper()
    nego_global = int(rules.get("short_uptick_ticks", 0))
    open_global = int(rules.get("short_open_uptick_ticks", 1))

    nego_ticks = nego_global
    reason: Optional[str] = None
    if nego_global > 0:
        reason = "ตั้งค่ากฎต่อราคา +1"
    elif clean:
        for d in rules.get("auto_strict_details") or []:
            if str(d.get("symbol") or "").upper() == clean:
                nego_ticks = 1
                reason = f"ปิดร่วง {d.get('pct_change')}% ({d.get('as_of') or 'เมื่อวาน'})"
                break
        if nego_ticks == 0 and clean in (rules.get("runtime_strict_symbols") or ()):
            nego_ticks = 1
            reason = "โบรกปฏิเสธ Uptick วันนี้"
        if nego_ticks == 0 and clean in (rules.get("strict_uptick_symbols") or ()):
            nego_ticks = 1
            reason = "รายการ +1 พิเศษ"

    if str(context or "nego").strip().lower() == "open":
        open_ticks = max(open_global, nego_ticks)
        if open_global >= 1 and reason is None:
            reason = "กฎเปิด Short +1 (Offer 2 / > Last)"
        return open_ticks, reason
    return nego_ticks, reason


def get_short_uptick_ticks(
    symbol: Optional[str] = None,
    path: Optional[str] = None,
    *,
    context: str = "nego",
) -> int:
    """Return required ticks above Last (0 for Zero-Plus >= Last, 1 for Strict > Last)."""
    ticks, _ = get_short_uptick_detail(symbol, context=context, path=path)
    return ticks
