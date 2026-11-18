"""MiniApp Notification Inbox & LINE Quota Poller (Zero-DB JSON + SSE).

Absorbs intraday operational notifications (OTP requests, trade receipts,
Auto-Nego runs, readiness reports, session drops) without consuming LINE
monthly push quota, and surfaces live LINE quota consumption for the MiniApp
top bar and Brokers view.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import time
from typing import Any, Dict, List, Optional
import uuid
from zoneinfo import ZoneInfo

import httpx

from app.core.config import settings
from app.core.sse_broadcaster import sse_broadcaster

logger = logging.getLogger(__name__)
_BKK = ZoneInfo("Asia/Bangkok")
_JST = ZoneInfo("Asia/Tokyo")
_THAI_MONTHS = (
    "",
    "ม.ค.",
    "ก.พ.",
    "มี.ค.",
    "เม.ย.",
    "พ.ค.",
    "มิ.ย.",
    "ก.ค.",
    "ส.ค.",
    "ก.ย.",
    "ต.ค.",
    "พ.ย.",
    "ธ.ค.",
)

MAX_INBOX_ITEMS = 150
QUOTA_CACHE_TTL_SECONDS = 60.0
_LINE_QUOTA_URL = "https://api.line.me/v2/bot/message/quota"
_LINE_QUOTA_CONSUMPTION_URL = "https://api.line.me/v2/bot/message/quota/consumption"


def _now_bkk() -> datetime:
    return datetime.now(_BKK)


def compute_line_quota_reset_info(now_utc: Optional[datetime] = None) -> Dict[str, str]:
    """Return next LINE Messaging API monthly quota reset timestamp (1st 00:00 JST = 22:00 ICT prev day)."""
    cur_jst = (now_utc or datetime.now(_JST)).astimezone(_JST)
    if cur_jst.month == 12:
        reset_jst = datetime(cur_jst.year + 1, 1, 1, 0, 0, 0, tzinfo=_JST)
    else:
        reset_jst = datetime(cur_jst.year, cur_jst.month + 1, 1, 0, 0, 0, tzinfo=_JST)
    reset_bkk = reset_jst.astimezone(_BKK)
    jst_lbl = f"{reset_jst.day} {_THAI_MONTHS[reset_jst.month]} 00:00 JST"
    bkk_lbl = f"{reset_bkk.day} {_THAI_MONTHS[reset_bkk.month]} {reset_bkk:%H:%M} น. ไทย"
    return {
        "reset_at_jst": reset_jst.isoformat(timespec="seconds"),
        "reset_at_bkk": reset_bkk.isoformat(timespec="seconds"),
        "reset_date_bkk": reset_bkk.strftime("%Y-%m-%d"),
        "reset_label": f"รีเซ็ต {jst_lbl} ({bkk_lbl})",
    }


def _with_quota_reset_info(payload: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(payload)
    out.update(compute_line_quota_reset_info())
    return out


class MiniAppInboxService:
    """Thread/async-safe JSON ring-buffer inbox + 60s cached LINE quota poller."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._quota_mem: Optional[Dict[str, Any]] = None
        self._quota_token_key: Optional[str] = None

    @staticmethod
    def _state_path() -> Path:
        raw = getattr(settings, "MINIAPP_INBOX_STATE_PATH", "data/miniapp_inbox.json")
        return Path(raw or "data/miniapp_inbox.json")

    @staticmethod
    def _archive_dir() -> Path:
        raw = getattr(settings, "MINIAPP_INBOX_ARCHIVE_DIR", "logs")
        return Path(raw or "logs")

    @staticmethod
    def _is_gateway_online_item(it: Dict[str, Any]) -> bool:
        meta = it.get("meta") if isinstance(it.get("meta"), dict) else {}
        if meta.get("dedup_key") == "gateway_online":
            return True
        return str(it.get("title") or "").strip().startswith("🚀 InvestmentHomelab Gateway ออนไลน์")

    @classmethod
    def _insert_item_with_dedup(
        cls,
        existing: List[Dict[str, Any]],
        new_item: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        meta = new_item.get("meta") if isinstance(new_item.get("meta"), dict) else {}
        dedup_key = str(meta.get("dedup_key") or "").strip()
        if not dedup_key:
            return [new_item, *existing][:MAX_INBOX_ITEMS]

        kept: List[Dict[str, Any]] = []
        prior_repeats = 0
        for it in existing:
            it_meta = it.get("meta") if isinstance(it.get("meta"), dict) else {}
            same_key = str(it_meta.get("dedup_key") or "").strip() == dedup_key
            same_gw = dedup_key == "gateway_online" and cls._is_gateway_online_item(it)
            if same_key or same_gw:
                prior_repeats += max(1, int(it_meta.get("repeat_count") or 1))
            else:
                kept.append(it)

        if prior_repeats > 0:
            new_item.setdefault("meta", {})
            new_item["meta"]["repeat_count"] = prior_repeats + 1
        return [new_item, *kept][:MAX_INBOX_ITEMS]

    def _empty_state(self) -> Dict[str, Any]:
        return {
            "items": [],
            "last_read_ts": None,
            "quota_cache": None,
        }

    def _load_state_sync(self) -> Dict[str, Any]:
        path = self._state_path()
        if not path.exists():
            return self._empty_state()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return self._empty_state()
            items = data.get("items")
            if not isinstance(items, list):
                items = []
            return {
                "items": items[:MAX_INBOX_ITEMS],
                "last_read_ts": data.get("last_read_ts"),
                "quota_cache": data.get("quota_cache") if isinstance(data.get("quota_cache"), dict) else None,
            }
        except Exception as exc:
            logger.warning("[MiniAppInbox] Failed loading %s: %s", path, exc)
            return self._empty_state()

    def _save_state_sync(self, state: Dict[str, Any]) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f"{path.suffix}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _unread_count(items: List[Dict[str, Any]]) -> int:
        return sum(1 for it in items if not it.get("read", False))

    @staticmethod
    def _build_item(
        *,
        category: str,
        severity: str,
        title: str,
        body: str,
        broker: Optional[str] = None,
        action_tab: Optional[str] = None,
        line_pushed: bool = False,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = _now_bkk()
        return {
            "id": f"n_{uuid.uuid4().hex[:10]}",
            "ts": now.isoformat(timespec="seconds"),
            "epoch": time.time(),
            "category": (category or "system").strip().lower(),
            "severity": (severity or "info").strip().lower(),
            "title": (title or "System Notice").strip()[:160],
            "body": (body or "").strip()[:800],
            "broker": broker.lower() if isinstance(broker, str) and broker.strip() else None,
            "action_tab": action_tab.strip() if isinstance(action_tab, str) and action_tab.strip() else None,
            "line_pushed": bool(line_pushed),
            "read": False,
            "meta": dict(meta) if isinstance(meta, dict) else {},
        }

    def add_sync(
        self,
        *,
        category: str = "system",
        severity: str = "info",
        title: str,
        body: str,
        broker: Optional[str] = None,
        action_tab: Optional[str] = None,
        line_pushed: bool = False,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Append an inbox event synchronously without awaiting SSE."""
        item = self._build_item(
            category=category,
            severity=severity,
            title=title,
            body=body,
            broker=broker,
            action_tab=action_tab,
            line_pushed=line_pushed,
            meta=meta,
        )
        state = self._load_state_sync()
        items = self._insert_item_with_dedup(state["items"], item)
        state["items"] = items
        self._save_state_sync(state)
        return item

    async def publish(
        self,
        *,
        category: str = "system",
        severity: str = "info",
        title: str,
        body: str,
        broker: Optional[str] = None,
        action_tab: Optional[str] = None,
        line_pushed: bool = False,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Append an inbox item, persist atomically, and push SSE update."""
        item = self._build_item(
            category=category,
            severity=severity,
            title=title,
            body=body,
            broker=broker,
            action_tab=action_tab,
            line_pushed=line_pushed,
            meta=meta,
        )
        async with self._lock:
            state = self._load_state_sync()
            items = self._insert_item_with_dedup(state["items"], item)
            state["items"] = items
            self._save_state_sync(state)
            unread_count = self._unread_count(items)

        try:
            await sse_broadcaster.broadcast_event(
                "miniapp_inbox",
                {"item": item, "unread_count": unread_count},
            )
        except Exception as exc:
            logger.debug("[MiniAppInbox] SSE broadcast skipped: %s", exc)
        return item

    async def list_items(self, limit: int = 50) -> Dict[str, Any]:
        limit = max(1, min(int(limit), MAX_INBOX_ITEMS))
        async with self._lock:
            state = self._load_state_sync()
            items = state["items"]
            return {
                "items": items[:limit],
                "unread_count": self._unread_count(items),
                "total": len(items),
                "last_read_ts": state.get("last_read_ts"),
            }

    async def mark_read(self, item_id: Optional[str] = None) -> Dict[str, Any]:
        now_iso = _now_bkk().isoformat(timespec="seconds")
        changed = 0
        async with self._lock:
            state = self._load_state_sync()
            items = state["items"]
            for it in items:
                if item_id is None or it.get("id") == item_id:
                    if not it.get("read", False):
                        it["read"] = True
                        changed += 1
            if item_id is None:
                state["last_read_ts"] = now_iso
            self._save_state_sync(state)
            unread_count = self._unread_count(items)

        try:
            await sse_broadcaster.broadcast_event(
                "miniapp_inbox_read",
                {"item_id": item_id, "unread_count": unread_count, "marked": changed},
            )
        except Exception:
            pass
        return {"unread_count": unread_count, "marked": changed, "last_read_ts": now_iso}

    async def clear_items(self, archive: bool = True) -> Dict[str, Any]:
        """Archive current inbox items to monthly JSONL and clear active ring buffer."""
        now_bkk = _now_bkk()
        now_iso = now_bkk.isoformat(timespec="seconds")
        archived_file: Optional[str] = None
        cleared = 0

        async with self._lock:
            state = self._load_state_sync()
            items = list(state.get("items") or [])
            cleared = len(items)
            if archive and items:
                arch_dir = self._archive_dir()
                arch_dir.mkdir(parents=True, exist_ok=True)
                arch_path = arch_dir / f"notifications_archive_{now_bkk.strftime('%Y_%m')}.jsonl"
                lines = [json.dumps(it, ensure_ascii=False) for it in reversed(items)]
                with arch_path.open("a", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
                archived_file = str(arch_path)
            state["items"] = []
            state["last_read_ts"] = now_iso
            self._save_state_sync(state)

        try:
            await sse_broadcaster.broadcast_event(
                "miniapp_inbox_cleared",
                {"unread_count": 0, "cleared": cleared, "archived_file": archived_file},
            )
        except Exception:
            pass
        return {
            "ok": True,
            "cleared": cleared,
            "unread_count": 0,
            "archived_file": archived_file,
            "cleared_at": now_iso,
        }

    async def mark_quota_exhausted(self, used: Optional[int] = None, limit: int = 300) -> Dict[str, Any]:
        """Update cached quota state immediately on HTTP 429 push failure."""
        payload = _with_quota_reset_info(
            {
                "type": "limited",
                "used": int(limit if used is None else used),
                "limit": int(limit),
                "remaining": 0,
                "pct": 100.0,
                "exhausted": True,
                "fetched_at": time.time(),
                "source": "http_429",
                "push_mode": getattr(settings, "LINE_PUSH_MODE", "eod_only"),
                "chat_commands_enabled": bool(getattr(settings, "LINE_CHAT_COMMANDS_ENABLED", False)),
                "eod_push_time": getattr(settings, "LINE_EOD_PUSH_TIME", "17:05"),
                "line_eod_push_enabled": bool(getattr(settings, "LINE_EOD_PUSH_ENABLED", False)),
            }
        )
        token_key = f"{self._state_path()}::{settings.LINE_CHANNEL_ACCESS_TOKEN}"
        self._quota_mem = payload
        self._quota_token_key = token_key
        async with self._lock:
            state = self._load_state_sync()
            state["quota_cache"] = payload
            self._save_state_sync(state)
        try:
            await sse_broadcaster.broadcast_event("line_quota_updated", payload)
        except Exception:
            pass
        return payload

    async def get_line_quota_status(self, force_refresh: bool = False) -> Dict[str, Any]:
        """Fetch live LINE Messaging API quota + consumption with a 60s cache."""
        token = (settings.LINE_CHANNEL_ACCESS_TOKEN or "").strip()
        sim = bool(settings.LINE_SIMULATION_MODE) or not token or token == "mock_line_channel_token"
        push_mode = getattr(settings, "LINE_PUSH_MODE", "eod_only")
        chat_enabled = bool(getattr(settings, "LINE_CHAT_COMMANDS_ENABLED", False))
        eod_time = getattr(settings, "LINE_EOD_PUSH_TIME", "17:05")

        if sim:
            return _with_quota_reset_info(
                {
                    "type": "limited",
                    "used": 0,
                    "limit": 300,
                    "remaining": 300,
                    "pct": 0.0,
                    "exhausted": False,
                    "fetched_at": time.time(),
                    "source": "simulation",
                    "push_mode": push_mode,
                    "chat_commands_enabled": chat_enabled,
                    "eod_push_time": eod_time,
                }
            )

        token_key = f"{self._state_path()}::{token}"
        now = time.time()
        if (
            not force_refresh
            and self._quota_mem is not None
            and self._quota_token_key == token_key
            and (now - float(self._quota_mem.get("fetched_at") or 0)) < QUOTA_CACHE_TTL_SECONDS
        ):
            out = dict(self._quota_mem)
            out["push_mode"] = push_mode
            out["chat_commands_enabled"] = chat_enabled
            out["eod_push_time"] = eod_time
            return _with_quota_reset_info(out)

        headers = {"Authorization": f"Bearer {token}"}
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                q_resp, c_resp = await asyncio.gather(
                    client.get(_LINE_QUOTA_URL, headers=headers),
                    client.get(_LINE_QUOTA_CONSUMPTION_URL, headers=headers),
                )
            if q_resp.status_code == 200 and c_resp.status_code == 200:
                q_json = q_resp.json()
                c_json = c_resp.json()
                q_type = q_json.get("type", "limited")
                limit = int(q_json.get("value") or 300) if q_type == "limited" else None
                used = int(c_json.get("totalUsage") or 0)
                if limit is not None:
                    remaining = max(0, limit - used)
                    pct = round(min(100.0, (used / max(limit, 1)) * 100.0), 1)
                    exhausted = used >= limit
                else:
                    remaining = None
                    pct = 0.0
                    exhausted = False

                payload = _with_quota_reset_info(
                    {
                        "type": q_type,
                        "used": used,
                        "limit": limit,
                        "remaining": remaining,
                        "pct": pct,
                        "exhausted": exhausted,
                        "fetched_at": now,
                        "source": "line_api",
                        "push_mode": push_mode,
                        "chat_commands_enabled": chat_enabled,
                        "eod_push_time": eod_time,
                    }
                )
                self._quota_mem = payload
                self._quota_token_key = token_key
                async with self._lock:
                    state = self._load_state_sync()
                    state["quota_cache"] = payload
                    self._save_state_sync(state)

                try:
                    from app.services.line_service import line_service

                    line_service._push_quota_exhausted = bool(exhausted)
                except Exception:
                    pass
                return dict(payload)

            logger.warning(
                "[MiniAppInbox] LINE quota probe HTTP %s/%s",
                q_resp.status_code,
                c_resp.status_code,
            )
        except Exception as exc:
            logger.warning("[MiniAppInbox] LINE quota probe error: %s", exc)

        # Fallback to disk cache if probe failed transiently
        state = self._load_state_sync()
        cached = state.get("quota_cache")
        if isinstance(cached, dict):
            out = dict(cached)
            out["source"] = "cache_fallback"
            out["push_mode"] = push_mode
            out["chat_commands_enabled"] = chat_enabled
            out["eod_push_time"] = eod_time
            return _with_quota_reset_info(out)

        return _with_quota_reset_info(
            {
                "type": "limited",
                "used": None,
                "limit": 300,
                "remaining": None,
                "pct": 0.0,
                "exhausted": False,
                "fetched_at": now,
                "source": "unavailable",
                "push_mode": push_mode,
                "chat_commands_enabled": chat_enabled,
                "eod_push_time": eod_time,
            }
        )


miniapp_inbox = MiniAppInboxService()
