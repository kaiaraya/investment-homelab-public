"""Pure-CDP 16:40 Daily EOD Matched-Order LINE Summary Push Service.

Fires at most once per Bangkok weekday (default ``16:40``) to the 1:1 LINE OA
target (``LINE_ADMIN_USER_ID`` -> 1x quota), reporting live CDP Matched orders,
Open/Unmatched orders, Honest No-Orders, or CDP-Unavailable states, and
records every run in MiniApp Notification Inbox.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time
import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.services.broker_cdp import session_probe, trading_facade
from app.services.line_cli import cards

logger = logging.getLogger(__name__)
_BKK = ZoneInfo("Asia/Bangkok")
DEFAULT_CHECK_INTERVAL_SEC = 30


def _now_bkk(override: Optional[datetime] = None) -> datetime:
    if override is not None:
        return override.astimezone(_BKK) if override.tzinfo else override.replace(tzinfo=_BKK)
    return datetime.now(_BKK)


def _parse_hhmm(raw: str, default: Tuple[int, int] = (16, 40)) -> time:
    try:
        parts = str(raw or "").strip().split(":")
        hh = max(0, min(23, int(parts[0])))
        mm = max(0, min(59, int(parts[1])))
        return time(hh, mm)
    except Exception:
        return time(default[0], default[1])


def _is_matched_order(order: Dict[str, Any]) -> bool:
    kind = str(order.get("kind") or "").strip().lower()
    if kind == "matched":
        return True
    status = str(order.get("status") or "").strip().upper()
    if status in {"M", "MATCHED", "FULLY MATCHED", "ALL MATCHED", "FILLED"}:
        return True
    return "MATCH" in status and "UNMATCH" not in status


class EodSummaryPushService:
    """Daily weekday 16:40 EOD summary builder & deduplicated LINE OA pusher."""

    def __init__(self, *, clock_fn: Optional[Callable[[], datetime]] = None) -> None:
        self._clock_fn = clock_fn
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._lock = asyncio.Lock()

    def _now(self) -> datetime:
        return _now_bkk(self._clock_fn() if self._clock_fn else None)

    @staticmethod
    def _state_path() -> Path:
        raw = getattr(settings, "LINE_EOD_STATE_PATH", "data/eod_summary_state.json")
        return Path(raw or "data/eod_summary_state.json")

    def _load_state(self) -> Dict[str, Any]:
        path = self._state_path()
        default_enabled = bool(getattr(settings, "EOD_SUMMARY_ENABLED", True))
        if not path.exists():
            return {"enabled": default_enabled, "last_sent_date": None, "last_result": None, "history": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("enabled", default_enabled)
                data.setdefault("last_sent_date", None)
                data.setdefault("last_result", None)
                data.setdefault("history", [])
                return data
        except Exception as exc:
            logger.warning("[EODSummary] Failed loading state %s: %s", path, exc)
        return {"enabled": default_enabled, "last_sent_date": None, "last_result": None, "history": []}

    def _save_state(self, state: Dict[str, Any]) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f"{path.suffix}.tmp.{os.getpid()}")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="eod_summary_push_loop")
        logger.info(
            "EOD Summary Service started (auto=%s, line_push=%s, time=%s BKK).",
            getattr(settings, "EOD_SUMMARY_ENABLED", True),
            getattr(settings, "LINE_EOD_PUSH_ENABLED", False),
            getattr(settings, "LINE_EOD_PUSH_TIME", "17:05"),
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("EOD Summary Push Service stopped.")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.tick_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("[EODSummary] tick failed: %s", exc)
            await asyncio.sleep(DEFAULT_CHECK_INTERVAL_SEC)

    def set_enabled(self, enabled: bool) -> Dict[str, Any]:
        state = self._load_state()
        state["enabled"] = bool(enabled)
        self._save_state(state)
        return self.get_status()

    def get_status(self) -> Dict[str, Any]:
        state = self._load_state()
        master_auto = bool(getattr(settings, "EOD_SUMMARY_ENABLED", True))
        toggle_enabled = bool(state.get("enabled", master_auto))
        return {
            "enabled": master_auto and toggle_enabled,
            "line_push_enabled": bool(getattr(settings, "LINE_EOD_PUSH_ENABLED", False)),
            "push_time": getattr(settings, "LINE_EOD_PUSH_TIME", "17:05"),
            "push_mode": getattr(settings, "LINE_PUSH_MODE", "eod_only"),
            "last_sent_date": state.get("last_sent_date"),
            "last_result": state.get("last_result"),
            "history": (state.get("history") or [])[:10],
        }

    async def _probe_brokers_readonly(self) -> Dict[str, Dict[str, Any]]:
        targets = (("kss", "streaming"), ("lib", "portfolio"))
        results = await asyncio.gather(
            *(session_probe.cdp_session_status(b, purpose=p) for b, p in targets),
            return_exceptions=True,
        )
        out: Dict[str, Dict[str, Any]] = {}
        for (broker, _), res in zip(targets, results):
            if isinstance(res, Exception):
                out[broker] = {"available": False, "state": "PROBE_ERROR", "error": str(res)}
            elif isinstance(res, dict):
                st = str(res.get("state") or "UNKNOWN")
                ok = st == "AUTHENTICATED"
                out[broker] = {
                    "available": ok,
                    "state": st,
                    "error": None if ok else (res.get("error") or f"CDP session {st}"),
                }
            else:
                out[broker] = {"available": False, "state": "UNKNOWN", "error": "Invalid probe result"}
        return out

    def _collect_nego_summary(self, now_bkk: datetime) -> Optional[Dict[str, Any]]:
        try:
            from app.services.nego_scheduler import nego_scheduler

            snap = nego_scheduler.get_schedule_snapshot(now_bkk)
            runs_done = 0
            amended = 0
            for win in snap.get("windows") or []:
                run = win.get("run") or {}
                st = str(run.get("status") or "")
                if st in {"AMENDED", "NO_ORDERS", "ERROR"}:
                    runs_done += 1
                amended += int((run.get("counts") or {}).get("amended") or 0)
            return {"runs_done": runs_done, "amended": amended}
        except Exception:
            return None

    async def build_summary(self) -> Dict[str, Any]:
        """Collect read-only CDP session + order state and build the EOD summary & Flex card."""
        now_bkk = self._now()
        date_str = now_bkk.strftime("%Y-%m-%d")
        broker_states = await self._probe_brokers_readonly()
        any_auth = any(v.get("available") for v in broker_states.values())

        matched_orders: List[Dict[str, Any]] = []
        open_orders: List[Dict[str, Any]] = []
        overview_error: Optional[str] = None
        portfolio_overview: Optional[Dict[str, Any]] = None

        if any_auth:
            try:
                overview = await trading_facade.fetch_orders_overview(broker="all")
                for b_key, info in (overview.get("brokers") or {}).items():
                    if b_key in broker_states and isinstance(info, dict):
                        avail = bool(info.get("available"))
                        broker_states[b_key]["available"] = avail and broker_states[b_key].get("available", True)
                        if info.get("error"):
                            broker_states[b_key]["error"] = info.get("error")

                for od in overview.get("orders") or []:
                    if not isinstance(od, dict):
                        continue
                    if _is_matched_order(od):
                        matched_orders.append(od)
                    else:
                        open_orders.append(od)
            except Exception as exc:
                overview_error = str(exc)
                for b_key, info in broker_states.items():
                    if info.get("available"):
                        info["available"] = False
                        info["error"] = f"Order fetch error: {exc}"

            try:
                portfolio_overview = await trading_facade.fetch_portfolio_overview(broker="all")
            except Exception as exc:
                logger.warning("[EODSummary] Portfolio Net Equity fetch failed: %s", exc)
                portfolio_overview = None

        all_unavailable = not any(v.get("available") for v in broker_states.values()) or (
            overview_error is not None and not matched_orders and not open_orders
        )
        if all_unavailable:
            status_kind = "session_unavailable"
        elif matched_orders:
            status_kind = "matched"
        elif open_orders:
            status_kind = "open_only"
        else:
            status_kind = "no_orders"

        port_summary = (portfolio_overview or {}).get("summary") or {}
        net_equity_val = port_summary.get("total_net_equity")
        nego_summary = self._collect_nego_summary(now_bkk)
        card = cards.eod_summary_card(
            date_str=date_str,
            status_kind=status_kind,
            matched_orders=matched_orders,
            open_orders=open_orders,
            broker_states=broker_states,
            nego_summary=nego_summary,
            net_equity=net_equity_val,
        )
        return {
            "date": date_str,
            "generated_at": now_bkk.isoformat(timespec="seconds"),
            "status_kind": status_kind,
            "counts": {
                "matched": len(matched_orders),
                "open": len(open_orders),
            },
            "matched_orders": matched_orders,
            "open_orders": open_orders,
            "broker_states": broker_states,
            "nego_summary": nego_summary,
            "net_equity": net_equity_val,
            "portfolio_overview": portfolio_overview,
            "card": card,
        }

    async def send_now(self, *, force: bool = True) -> Dict[str, Any]:
        """Build EOD summary, upsert daily Net Equity CSV/JSON, log to MiniApp Inbox, and optionally push LINE."""
        async with self._lock:
            now_bkk = self._now()
            today_str = now_bkk.strftime("%Y-%m-%d")
            state = self._load_state()
            if not force and state.get("last_sent_date") == today_str:
                return {"sent": False, "skipped_reason": "already_sent_today", "date": today_str}

            summary = await self.build_summary()
            from app.services.net_equity_history import net_equity_history

            eq_record = net_equity_history.record_daily_net_equity(
                date_str=today_str,
                overview=summary.get("portfolio_overview"),
                counts=summary["counts"],
                source="eod_manual" if force else "eod_auto",
                recorded_at=now_bkk.isoformat(timespec="seconds"),
            )
            net_eq = eq_record.get("total_net_equity")

            push_mode = (getattr(settings, "LINE_PUSH_MODE", "eod_only") or "eod_only").strip().lower()
            line_eod_enabled = bool(getattr(settings, "LINE_EOD_PUSH_ENABLED", False))
            target = (settings.LINE_ADMIN_USER_ID or settings.LINE_GROUP_ID or "").strip()

            pushed = False
            skip_reason: Optional[str] = None
            from app.services.line_service import line_service

            if not line_eod_enabled:
                skip_reason = "line_push_disabled"
            elif push_mode == "off":
                skip_reason = "line_push_mode_off"
            elif not target:
                skip_reason = "missing_line_target"
            else:
                pushed = bool(
                    await line_service.push_message(
                        summary["card"],
                        to_user_id=target,
                        category="eod_summary",
                        record_inbox=False,
                    )
                )
                if not pushed:
                    skip_reason = "quota_exhausted" if getattr(line_service, "_push_quota_exhausted", False) else "line_push_failed"

            matched_cnt = summary["counts"]["matched"]
            open_cnt = summary["counts"]["open"]
            status_kind = summary["status_kind"]
            eod_time = str(getattr(settings, "LINE_EOD_PUSH_TIME", "17:05") or "17:05")
            net_eq_tag = f" · Net Eq ฿{float(net_eq):,.2f}" if net_eq is not None else ""
            if status_kind == "session_unavailable":
                sev = "warning"
                title = f"📊 สรุปปิดตลาด {today_str} (เชื่อมต่อ CDP ไม่สำเร็จ)"
                body = f"ไม่พบ Session โบรกเกอร์ที่พร้อมใช้งาน ณ {eod_time} จึงไม่สามารถยืนยัน Matched / Net Equity ได้{net_eq_tag}"
            elif status_kind == "matched":
                sev = "success"
                top_syms = ", ".join(str(o.get("symbol") or "?") for o in summary["matched_orders"][:5])
                title = f"📊 สรุปปิดตลาด {today_str}: Matched {matched_cnt} รายการ"
                body = f"Matched {matched_cnt} ({top_syms}) · ค้าง {open_cnt}{net_eq_tag}"
            elif status_kind == "open_only":
                sev = "info"
                title = f"📊 สรุปปิดตลาด {today_str}: ไม่มี Matched (ค้าง {open_cnt})"
                body = f"มีคำสั่งค้าง/ไม่ Matched {open_cnt} รายการ{net_eq_tag}"
            else:
                sev = "info"
                title = f"📊 สรุปปิดตลาด {today_str}: ไม่มีคำสั่งวันนี้"
                body = f"0 Matched · 0 Open{net_eq_tag}"

            try:
                from app.services.miniapp_inbox import miniapp_inbox

                await miniapp_inbox.publish(
                    category="eod",
                    severity=sev,
                    title=title,
                    body=body,
                    action_tab="orders" if status_kind != "session_unavailable" else "brokers",
                    line_pushed=pushed,
                    meta={
                        "date": today_str,
                        "status_kind": status_kind,
                        "counts": summary["counts"],
                        "net_equity": net_eq,
                        "net_equity_partial": bool(eq_record.get("partial")),
                        "skip_reason": skip_reason,
                    },
                )
            except Exception as exc:
                logger.debug("[EODSummary] Inbox publish failed: %s", exc)

            result_record = {
                "date": today_str,
                "sent": pushed,
                "recorded": True,
                "status_kind": status_kind,
                "counts": summary["counts"],
                "net_equity": net_eq,
                "net_equity_partial": bool(eq_record.get("partial")),
                "net_equity_paths": eq_record.get("paths"),
                "target": target or None,
                "skip_reason": skip_reason,
                "executed_at": now_bkk.isoformat(timespec="seconds"),
            }
            if pushed or skip_reason in {"quota_exhausted", "eod_push_disabled", "line_push_disabled", "line_push_mode_off"}:
                state["last_sent_date"] = today_str
            state["last_result"] = result_record
            state["history"] = [result_record, *(state.get("history") or [])][:30]
            self._save_state(state)
            summary_out = {k: v for k, v in summary.items() if k != "portfolio_overview"}
            return {**result_record, "summary": summary_out}

    async def tick_once(self) -> Optional[Dict[str, Any]]:
        if not bool(getattr(settings, "EOD_SUMMARY_ENABLED", True)):
            return None
        now_bkk = self._now()
        if now_bkk.weekday() >= 5:
            return None

        target_t = _parse_hhmm(getattr(settings, "LINE_EOD_PUSH_TIME", "17:05"))
        now_t = now_bkk.time().replace(tzinfo=None)
        if now_t < target_t or now_t > time(18, 30):
            return None

        state = self._load_state()
        if not bool(state.get("enabled", True)):
            return None
        today_str = now_bkk.strftime("%Y-%m-%d")
        if state.get("last_sent_date") == today_str:
            return None

        return await self.send_now(force=False)


eod_summary_service = EodSummaryPushService()
