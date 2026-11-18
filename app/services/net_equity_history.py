"""Daily EOD Net Equity History Service.

Persists one row per trading date (`YYYY-MM-DD`) via atomic upsert into:
- ``data/net_equity_history.json``
- ``data/net_equity_history.csv``
- ``/srv/antigravity/shadow/InvestorPerformance/data/net_equity_history.csv`` (mirror when parent dir exists)
"""

from __future__ import annotations

import csv
import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from app.core.config import settings

logger = logging.getLogger(__name__)
_BKK_TZ = ZoneInfo("Asia/Bangkok")

CSV_COLUMNS = [
    "date",
    "total_net_equity",
    "kss_margin_net_equity",
    "kss_cash_net_equity",
    "lib_cash_net_equity",
    "total_cash_balance",
    "total_long_mv",
    "total_short_liability",
    "selected_accounts",
    "partial",
    "matched_orders",
    "open_orders",
    "source",
    "recorded_at",
]


def _round_opt(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return round(float(val), 2)
    except (TypeError, ValueError):
        return None


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def _format_csv_text(rows: List[Dict[str, Any]]) -> str:
    import io

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        out: Dict[str, Any] = {}
        for col in CSV_COLUMNS:
            val = row.get(col)
            if val is None:
                out[col] = ""
            elif col == "selected_accounts" and isinstance(val, list):
                out[col] = ",".join(str(x) for x in val)
            elif col == "partial":
                out[col] = "true" if val else "false"
            else:
                out[col] = val
        writer.writerow(out)
    return buf.getvalue()


class NetEquityHistoryService:
    """Thread-safe / single-writer daily Net Equity upsert store."""

    def _json_path(self) -> Path:
        return Path(getattr(settings, "NET_EQUITY_HISTORY_JSON_PATH", "data/net_equity_history.json"))

    def _csv_path(self) -> Path:
        return Path(getattr(settings, "NET_EQUITY_HISTORY_CSV_PATH", "data/net_equity_history.csv"))

    def _mirror_csv_path(self) -> Optional[Path]:
        raw = str(getattr(settings, "NET_EQUITY_MIRROR_CSV_PATH", "") or "").strip()
        if not raw:
            return None
        return Path(raw)

    def _load_entries(self) -> List[Dict[str, Any]]:
        path = self._json_path()
        if not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and isinstance(payload.get("entries"), list):
                return [e for e in payload["entries"] if isinstance(e, dict) and e.get("date")]
            if isinstance(payload, list):
                return [e for e in payload if isinstance(e, dict) and e.get("date")]
        except Exception as exc:
            logger.warning("[NetEquityHistory] Failed loading %s: %s", path, exc)
        return []

    def _persist_entries(self, entries: List[Dict[str, Any]]) -> Dict[str, Optional[str]]:
        # Sort chronologically ascending in CSV/JSON storage so graphing tools read oldest->newest directly
        ordered = sorted(entries, key=lambda r: str(r.get("date") or ""))
        now_iso = datetime.now(_BKK_TZ).isoformat(timespec="seconds")

        json_path = self._json_path()
        csv_path = self._csv_path()
        _atomic_write_text(
            json_path,
            json.dumps({"updated_at": now_iso, "entries": ordered}, ensure_ascii=False, indent=2),
        )
        csv_text = _format_csv_text(ordered)
        _atomic_write_text(csv_path, csv_text)

        mirrored: Optional[str] = None
        mirror_path = self._mirror_csv_path()
        if mirror_path and mirror_path.parent.exists() and mirror_path.parent.is_dir():
            try:
                _atomic_write_text(mirror_path, csv_text)
                mirrored = str(mirror_path)
            except Exception as exc:
                logger.warning("[NetEquityHistory] Mirror write skipped %s: %s", mirror_path, exc)

        return {
            "json_path": str(json_path),
            "csv_path": str(csv_path),
            "mirror_csv_path": mirrored,
        }

    def record_daily_net_equity(
        self,
        *,
        date_str: str,
        overview: Optional[Dict[str, Any]],
        counts: Optional[Dict[str, Any]] = None,
        source: str = "eod",
        recorded_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upsert one daily Net Equity record for ``date_str`` (`YYYY-MM-DD`)."""
        summary = (overview or {}).get("summary") or {}
        accounts = (overview or {}).get("accounts") or []
        by_key = {
            str(a.get("key") or ""): a
            for a in accounts
            if isinstance(a, dict) and a.get("key")
        }

        def _acct_eq(key: str) -> Optional[float]:
            info = by_key.get(key)
            if not isinstance(info, dict) or not info.get("available"):
                return None
            return _round_opt(info.get("net_equity"))

        selected = summary.get("selected_accounts")
        if not isinstance(selected, list):
            selected = [
                str(a.get("key"))
                for a in accounts
                if isinstance(a, dict) and a.get("selected_in_routing") and a.get("key")
            ]

        selected_keys: List[str] = []
        for s in selected:
            if isinstance(s, dict):
                k = s.get("key") or s.get("account") or s.get("broker")
                if k:
                    selected_keys.append(str(k))
            elif s:
                selected_keys.append(str(s))

        entry: Dict[str, Any] = {
            "date": str(date_str),
            "total_net_equity": _round_opt(summary.get("total_net_equity")),
            "kss_margin_net_equity": _acct_eq("kss_margin"),
            "kss_cash_net_equity": _acct_eq("kss_cash"),
            "lib_cash_net_equity": _acct_eq("lib_cash"),
            "total_cash_balance": _round_opt(summary.get("total_cash_balance")),
            "total_long_mv": _round_opt(summary.get("total_long_market_value")),
            "total_short_liability": _round_opt(summary.get("total_short_liability")),
            "selected_accounts": selected_keys,
            "partial": bool(summary.get("partial", overview is None)),
            "matched_orders": int((counts or {}).get("matched") or 0),
            "open_orders": int((counts or {}).get("open") or 0),
            "source": str(source or "eod"),
            "recorded_at": recorded_at or datetime.now(_BKK_TZ).isoformat(timespec="seconds"),
        }

        existing_list = self._load_entries()
        existing_same = next((e for e in existing_list if str(e.get("date") or "") == str(date_str)), None)
        if entry["total_net_equity"] is None and isinstance(existing_same, dict) and existing_same.get("total_net_equity") is not None:
            # Never erase an earlier verified intraday/EOD Net Equity snapshot if CDP is offline on a later re-run
            paths = {
                "json_path": str(self._json_path()),
                "csv_path": str(self._csv_path()),
                "mirror_csv_path": str(self._mirror_csv_path()) if self._mirror_csv_path() else None,
            }
            return {**existing_same, "preserved_existing": True, "paths": paths}

        entries = [e for e in existing_list if str(e.get("date") or "") != str(date_str)]
        entries.append(entry)
        paths = self._persist_entries(entries)
        return {**entry, "paths": paths}

    def get_entry_for_date(self, date_str: str) -> Optional[Dict[str, Any]]:
        for e in self._load_entries():
            if str(e.get("date") or "") == str(date_str):
                return e
        return None

    def get_history(self, *, limit: int = 90) -> Dict[str, Any]:
        entries = sorted(self._load_entries(), key=lambda r: str(r.get("date") or ""))
        if limit > 0 and len(entries) > limit:
            entries = entries[-limit:]
        return {
            "count": len(entries),
            "csv_path": str(self._csv_path()),
            "json_path": str(self._json_path()),
            "mirror_csv_path": str(self._mirror_csv_path()) if self._mirror_csv_path() else None,
            "entries": entries,
        }


net_equity_history = NetEquityHistoryService()
