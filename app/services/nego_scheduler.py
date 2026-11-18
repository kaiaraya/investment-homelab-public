"""Pure-CDP Background Auto-Nego Scheduler & Daily Checklist Service.

Runs inside the FastAPI gateway lifespan (Zero-DB, atomic JSON persistence in
``data/nego_scheduler_state.json``). Picks one random execution minute per
enabled intraday window per Bangkok weekday, executes autonomous price
negotiation via ``trading_facade.execute_nego_amend(..., notify_line=False)``,
records a structured Daily Checklist for the MiniApp Schedule UI, and sends
LINE notifications to ``a14s homelab`` ONLY on critical errors (deduped daily)
to preserve 100% of monthly LINE push quota.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, time
import json
import logging
import os
from pathlib import Path
import random
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.services import pin_guard
from app.services.broker_cdp.session_probe import cdp_session_status
from app.services.broker_cdp import trading_facade
from app.services.kill_switch_service import kill_switch_service
from app.services.market_clock import is_nego_window
from app.services.trade_dispatcher import trade_dispatcher

logger = logging.getLogger(__name__)
_BKK = ZoneInfo("Asia/Bangkok")


@dataclass(frozen=True)
class NegoWindowSpec:
    id: str
    start: str
    end: str
    default_enabled: bool
    mode: str  # "lure" | "match" | "post_close" | "off"
    allowed_sides: Tuple[str, ...]
    tradable: bool
    label: str
    description: str
    exec_start: Optional[str] = None
    exec_end: Optional[str] = None


WINDOW_SPECS: Tuple[NegoWindowSpec, ...] = (
    NegoWindowSpec(
        id="w2_morning",
        start="10:30",
        end="12:30",
        default_enabled=True,
        mode="lure",
        allowed_sides=("BUY", "SELL", "SHORT", "COVER"),
        tradable=True,
        label="ช่วงเช้า (Lure)",
        description="ต่อราคาแบบล่อ (Buy / Sell / Short / Cover)",
    ),
    NegoWindowSpec(
        id="w3_lunch",
        start="12:30",
        end="14:00",
        default_enabled=False,
        mode="off",
        allowed_sides=(),
        tradable=False,
        label="พักเที่ยง SET",
        description="ตลาดปิดพักเที่ยง (ไม่สามารถต่อราคา)",
    ),
    NegoWindowSpec(
        id="w4_afternoon",
        start="14:00",
        end="16:00",
        default_enabled=True,
        mode="lure",
        allowed_sides=("BUY", "SELL", "SHORT", "COVER"),
        tradable=True,
        label="ช่วงบ่าย (Lure)",
        description="ต่อราคาแบบล่อ (Buy / Sell / Short / Cover)",
    ),
    NegoWindowSpec(
        id="w5_close",
        start="16:00",
        end="16:30",
        default_enabled=True,
        mode="match",
        allowed_sides=("BUY", "SELL", "SHORT", "COVER"),
        tradable=True,
        label="ก่อนปิดตลาด (Match)",
        description="Match เฉพาะขาออก (Sell / Cover) · Buy / Short ตั้งล่อแบบ Lure เท่านั้น",
    ),
    NegoWindowSpec(
        id="w6_post_close",
        start="16:30",
        end="17:30",
        exec_start="16:40",
        exec_end="16:55",
        default_enabled=True,
        mode="post_close",
        allowed_sides=("BUY", "SELL", "SHORT", "COVER"),
        tradable=True,
        label="หลังปิดตลาด (Cancel & คืน SBL)",
        description="16:40–16:55 ยกเลิกออเดอร์ค้างหลัง ATC & คืนหุ้น SBL ที่ว่างอัตโนมัติ (ก่อนตัดรอบ 17:00)",
    ),
)

_SPEC_BY_ID: Dict[str, NegoWindowSpec] = {w.id: w for w in WINDOW_SPECS}
_BROKERS: Tuple[str, ...] = ("kss", "lib")


def _to_minutes(hhmm: str) -> int:
    h, m = map(int, hhmm.split(":"))
    return h * 60 + m


def _fmt_minutes(total_mins: int) -> str:
    return f"{total_mins // 60:02d}:{total_mins % 60:02d}"


def _now_bkk(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return datetime.now(_BKK)
    if now.tzinfo is None:
        return now.replace(tzinfo=_BKK)
    return now.astimezone(_BKK)


def _is_already_matched_error(code: str, message: str) -> bool:
    hay = f"{code} {message}".upper()
    return any(
        tok in hay
        for tok in ("MATCHED", "ALREADY_MATCHED", "NOT_FOUND", "ORDER_NOT_OPEN", "NOT_OPEN")
    )


def _pick_random_minute(
    spec: NegoWindowSpec,
    now_mins: Optional[int] = None,
    rng: Optional[random.Random] = None,
) -> Optional[str]:
    """Pick a random HH:MM inside [exec_start or start+1m, exec_end or end-2m]."""
    if not spec.tradable:
        return None
    start_m = _to_minutes(spec.exec_start) if spec.exec_start else (_to_minutes(spec.start) + 1)
    end_m = _to_minutes(spec.exec_end) if spec.exec_end else (_to_minutes(spec.end) - 2)
    if now_mins is not None:
        raw_end = _to_minutes(spec.end)
        if now_mins >= raw_end:
            return None
        if now_mins + 1 <= end_m:
            start_m = max(start_m, now_mins + 1)
        elif now_mins + 1 < raw_end:
            # Past preferred sub-window (e.g. >16:55 inside 16:30-17:30): run on next minute.
            return _fmt_minutes(now_mins + 1)
        else:
            # Less than 2 minutes left before window end: run immediately on next loop tick.
            return _fmt_minutes(now_mins)
    if start_m > end_m:
        return _fmt_minutes(max(_to_minutes(spec.start), min(end_m, _to_minutes(spec.end) - 1)))
    picker = rng or random
    return _fmt_minutes(picker.randint(start_m, end_m))


class NegoScheduler:
    """Autonomous Pure-CDP price negotiation scheduler with Daily Checklist."""

    def __init__(
        self,
        state_path: Optional[str] = None,
        check_interval: Optional[int] = None,
        clock_fn: Optional[Callable[[], datetime]] = None,
        rng: Optional[random.Random] = None,
    ):
        self._explicit_state_path = state_path
        self._explicit_interval = check_interval
        self._clock_fn = clock_fn
        self._rng = rng
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._exec_lock = asyncio.Lock()
        self._state: Optional[Dict[str, Any]] = None

    @property
    def state_path(self) -> Path:
        raw = self._explicit_state_path or getattr(
            settings, "NEGO_SCHEDULER_STATE_PATH", "data/nego_scheduler_state.json"
        )
        return Path(raw)

    @property
    def check_interval(self) -> int:
        if self._explicit_interval is not None:
            return max(1, int(self._explicit_interval))
        return max(5, int(getattr(settings, "NEGO_SCHEDULER_CHECK_INTERVAL_SEC", 20)))

    def _now(self) -> datetime:
        return _now_bkk(self._clock_fn() if self._clock_fn else None)

    def _default_state(self, now_bkk: datetime) -> Dict[str, Any]:
        return {
            "master_enabled": True,
            "windows": {w.id: {"enabled": w.default_enabled} for w in WINDOW_SPECS},
            "daily_date": now_bkk.strftime("%Y-%m-%d"),
            "daily_runs": self._build_daily_runs(
                now_bkk,
                {w.id: {"enabled": w.default_enabled} for w in WINDOW_SPECS},
            ),
            "sent_error_keys": [],
        }

    def _build_daily_runs(
        self,
        now_bkk: datetime,
        windows_cfg: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Dict[str, Any]]:
        now_m = now_bkk.hour * 60 + now_bkk.minute
        runs: Dict[str, Dict[str, Any]] = {}
        for spec in WINDOW_SPECS:
            enabled = bool(windows_cfg.get(spec.id, {}).get("enabled", spec.default_enabled))
            end_m = _to_minutes(spec.end)
            if not spec.tradable:
                status = "MARKET_CLOSED"
                sched = None
                line_status = "งดส่ง (ตลาดพักเที่ยง)"
            elif not enabled:
                status = "DISABLED"
                sched = None
                line_status = "งดส่ง (ปิดอยู่)"
            elif now_m >= end_m:
                status = "MISSED_WINDOW"
                sched = None
                line_status = "งดส่ง LINE"
            else:
                sched = _pick_random_minute(spec, now_mins=now_m, rng=self._rng)
                status = "PENDING"
                line_status = "รอทำงาน"
            runs[spec.id] = self._empty_run_entry(
                spec,
                status=status,
                scheduled_time=sched,
                line_status=line_status,
            )
        return runs

    @staticmethod
    def _empty_run_entry(
        spec: NegoWindowSpec,
        *,
        status: str,
        scheduled_time: Optional[str],
        line_status: str,
    ) -> Dict[str, Any]:
        return {
            "window_id": spec.id,
            "status": status,
            "scheduled_time": scheduled_time,
            "trigger_source": None,
            "started_at": None,
            "finished_at": None,
            "dry_run": None,
            "counts": {
                "checked": 0,
                "amended": 0,
                "no_change": 0,
                "skipped": 0,
                "failed": 0,
            },
            "items": [],
            "broker_errors": {},
            "error": None,
            "line_status": line_status,
        }

    def _load_or_init_state(self, now_bkk: Optional[datetime] = None) -> Dict[str, Any]:
        cur = now_bkk or self._now()
        today_str = cur.strftime("%Y-%m-%d")

        if self._state is None:
            path = self.state_path
            if path.exists():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        self._state = raw
                except Exception as exc:
                    logger.warning("Failed to read %s, reinitializing: %s", path, exc)
            if self._state is None:
                self._state = self._default_state(cur)
                self._save_state()

        state = self._state
        state.setdefault("master_enabled", True)
        win_cfg = state.setdefault("windows", {})
        valid_ids = {spec.id for spec in WINDOW_SPECS}
        cfg_pruned = False
        for retired_id in [k for k in list(win_cfg.keys()) if k not in valid_ids]:
            win_cfg.pop(retired_id, None)
            cfg_pruned = True
        for spec in WINDOW_SPECS:
            entry = win_cfg.setdefault(spec.id, {"enabled": spec.default_enabled})
            if not spec.tradable:
                entry["enabled"] = False
        state.setdefault("sent_error_keys", [])

        if state.get("daily_date") != today_str or not isinstance(state.get("daily_runs"), dict):
            state["daily_date"] = today_str
            state["daily_runs"] = self._build_daily_runs(cur, win_cfg)
            state["sent_error_keys"] = []
            self._save_state()
        else:
            # Ensure every window spec matches schema upgrade & remove retired windows (e.g. w1_open).
            changed = cfg_pruned
            runs = state["daily_runs"]
            for retired_id in [k for k in list(runs.keys()) if k not in valid_ids]:
                runs.pop(retired_id, None)
                changed = True
            now_m = cur.hour * 60 + cur.minute
            for spec in WINDOW_SPECS:
                if spec.id not in runs:
                    enabled = bool(win_cfg.get(spec.id, {}).get("enabled", spec.default_enabled))
                    if not spec.tradable:
                        runs[spec.id] = self._empty_run_entry(
                            spec,
                            status="MARKET_CLOSED",
                            scheduled_time=None,
                            line_status="งดส่ง (ตลาดพักเที่ยง)",
                        )
                    elif not enabled:
                        runs[spec.id] = self._empty_run_entry(
                            spec,
                            status="DISABLED",
                            scheduled_time=None,
                            line_status="งดส่ง (ปิดอยู่)",
                        )
                    elif now_m >= _to_minutes(spec.end):
                        runs[spec.id] = self._empty_run_entry(
                            spec,
                            status="MISSED_WINDOW",
                            scheduled_time=None,
                            line_status="งดส่ง LINE",
                        )
                    else:
                        sched = _pick_random_minute(spec, now_mins=now_m, rng=self._rng)
                        runs[spec.id] = self._empty_run_entry(
                            spec,
                            status="PENDING" if sched else "MISSED_WINDOW",
                            scheduled_time=sched,
                            line_status="รอทำงาน" if sched else "งดส่ง LINE",
                        )
                    changed = True
            if changed:
                self._save_state()

        self._expire_passed_pending_windows(cur)
        return state

    def _expire_passed_pending_windows(self, now_bkk: datetime) -> None:
        if not self._state:
            return
        now_m = now_bkk.hour * 60 + now_bkk.minute
        runs = self._state.get("daily_runs") or {}
        changed = False
        for spec in WINDOW_SPECS:
            run = runs.get(spec.id)
            if not run:
                continue
            if run.get("status") == "PENDING" and now_m >= _to_minutes(spec.end):
                run["status"] = "MISSED_WINDOW"
                run["line_status"] = "งดส่ง LINE"
                changed = True
        if changed:
            self._save_state()

    def _save_state(self) -> None:
        if self._state is None:
            return
        path = self.state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f"{path.suffix}.tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    # ------------------------------------------------------------------
    # Public Control & Query API (consumed by MiniApp & LINE /schedule)
    # ------------------------------------------------------------------

    def get_schedule_snapshot(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        cur = _now_bkk(now) if now is not None else self._now()
        state = self._load_or_init_state(cur)
        now_m = cur.hour * 60 + cur.minute
        is_weekday = cur.weekday() < 5
        windows_out: List[Dict[str, Any]] = []
        next_run: Optional[Dict[str, Any]] = None

        for spec in WINDOW_SPECS:
            enabled = bool(state["windows"].get(spec.id, {}).get("enabled", spec.default_enabled))
            run = deepcopy(state["daily_runs"][spec.id])
            start_m, end_m = _to_minutes(spec.start), _to_minutes(spec.end)
            in_window = start_m <= now_m < end_m
            sched_str = run.get("scheduled_time")

            if (
                is_weekday
                and state.get("master_enabled")
                and spec.tradable
                and enabled
                and run.get("status") == "PENDING"
                and sched_str
            ):
                eta = max(0, _to_minutes(sched_str) - now_m)
                if next_run is None or _to_minutes(sched_str) < _to_minutes(next_run["scheduled_time"]):
                    next_run = {
                        "window_id": spec.id,
                        "label": spec.label,
                        "mode": spec.mode,
                        "scheduled_time": sched_str,
                        "eta_minutes": eta,
                    }

            windows_out.append(
                {
                    "id": spec.id,
                    "start": spec.start,
                    "end": spec.end,
                    "period": f"{spec.start} - {spec.end}",
                    "mode": spec.mode,
                    "allowed_sides": list(spec.allowed_sides),
                    "tradable": spec.tradable,
                    "enabled": enabled if spec.tradable else False,
                    "label": spec.label,
                    "description": spec.description,
                    "in_window_now": is_weekday and in_window,
                    "run": run,
                }
            )

        is_dry = (not bool(trade_dispatcher.is_live)) or bool(getattr(settings, "DRY_RUN_MODE", False))
        return {
            "daily_date": state["daily_date"],
            "now_bkk": cur.strftime("%H:%M:%S"),
            "is_weekday": is_weekday,
            "in_nego_market_window": is_nego_window(cur),
            "master_enabled": bool(state.get("master_enabled", True)),
            "running_now": self._exec_lock.locked(),
            "dry_run": is_dry,
            "kill_switch": kill_switch_service.get_status(),
            "line_quota_exhausted": self._is_line_quota_exhausted(),
            "next_run": next_run,
            "windows": windows_out,
        }

    def set_toggle(
        self,
        *,
        enabled: bool,
        window_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        cur = _now_bkk(now) if now is not None else self._now()
        state = self._load_or_init_state(cur)
        now_m = cur.hour * 60 + cur.minute

        if window_id is None:
            state["master_enabled"] = bool(enabled)
            self._save_state()
            return self.get_schedule_snapshot(cur)

        spec = _SPEC_BY_ID.get(window_id)
        if spec is None:
            raise ValueError(f"ไม่พบช่วงเวลา '{window_id}'")
        if not spec.tradable:
            raise ValueError("ช่วง 12:30 - 14:00 เป็นเวลาพักเที่ยง SET ตลาดปิดไม่สามารถเปิดได้")

        state["windows"][window_id]["enabled"] = bool(enabled)
        run = state["daily_runs"][window_id]

        # Only reschedule if this window hasn't already executed today.
        terminal_done = {"AMENDED", "DONE", "NO_CHANGE", "NO_ORDERS", "ERROR", "BLOCKED_KILL_SWITCH"}
        if run.get("status") not in terminal_done and run.get("status") != "RUNNING":
            if enabled:
                if now_m >= _to_minutes(spec.end):
                    run["status"] = "MISSED_WINDOW"
                    run["scheduled_time"] = None
                    run["line_status"] = "งดส่ง LINE"
                else:
                    existing = run.get("scheduled_time")
                    if existing and _to_minutes(existing) >= now_m:
                        sched = existing
                    else:
                        sched = _pick_random_minute(spec, now_mins=now_m, rng=self._rng)
                    run["scheduled_time"] = sched
                    run["status"] = "PENDING" if sched else "MISSED_WINDOW"
                    run["line_status"] = "รอทำงาน" if sched else "งดส่ง LINE"
            else:
                run["status"] = "DISABLED"
                run["scheduled_time"] = None
                run["line_status"] = "งดส่ง (ปิดอยู่)"

        self._save_state()
        return self.get_schedule_snapshot(cur)

    def reroll_times(
        self,
        *,
        window_id: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        cur = _now_bkk(now) if now is not None else self._now()
        state = self._load_or_init_state(cur)
        now_m = cur.hour * 60 + cur.minute
        targets = [_SPEC_BY_ID[window_id]] if window_id else list(WINDOW_SPECS)

        if window_id and window_id not in _SPEC_BY_ID:
            raise ValueError(f"ไม่พบช่วงเวลา '{window_id}'")

        for spec in targets:
            if not spec.tradable:
                continue
            if not state["windows"].get(spec.id, {}).get("enabled", False):
                continue
            run = state["daily_runs"][spec.id]
            if run.get("status") != "PENDING":
                continue
            sched = _pick_random_minute(spec, now_mins=now_m, rng=self._rng)
            run["scheduled_time"] = sched
            if sched is None:
                run["status"] = "MISSED_WINDOW"
                run["line_status"] = "งดส่ง LINE"

        self._save_state()
        return self.get_schedule_snapshot(cur)

    # ------------------------------------------------------------------
    # Background Loop & Execution
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        self._load_or_init_state()
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("NegoScheduler started (check every %ss).", self.check_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("NegoScheduler stopped.")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.tick_once()
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("NegoScheduler loop error: %s", exc, exc_info=True)
                await asyncio.sleep(self.check_interval)

    async def tick_once(self, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Evaluate today's windows once and execute any window whose trigger time arrived."""
        cur = _now_bkk(now) if now is not None else self._now()
        state = self._load_or_init_state(cur)

        if cur.weekday() >= 5 or not state.get("master_enabled", True):
            return []

        now_m = cur.hour * 60 + cur.minute
        results: List[Dict[str, Any]] = []
        for spec in WINDOW_SPECS:
            if not spec.tradable:
                continue
            if not state["windows"].get(spec.id, {}).get("enabled", False):
                continue
            run = state["daily_runs"][spec.id]
            if run.get("status") != "PENDING":
                continue
            sched = run.get("scheduled_time")
            if not sched:
                continue
            sched_m = _to_minutes(sched)
            end_m = _to_minutes(spec.end)
            if sched_m <= now_m < end_m:
                res = await self.run_window_now(spec.id, source="scheduler", now=cur)
                results.append(res)
        return results

    async def run_window_now(
        self,
        window_id: str,
        *,
        source: str = "manual",
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """Execute a specific window under single-flight lock (never overlaps manual runs)."""
        spec = _SPEC_BY_ID.get(window_id)
        if spec is None:
            raise ValueError(f"ไม่พบช่วงเวลา '{window_id}'")
        if not spec.tradable:
            raise ValueError("ช่วง 12:30 - 14:00 เป็นเวลาพักเที่ยง SET ไม่สามารถรันต่อราคาได้")

        async with self._exec_lock:
            cur = _now_bkk(now) if now is not None else self._now()
            state = self._load_or_init_state(cur)
            is_dry = (not bool(trade_dispatcher.is_live)) or bool(
                getattr(settings, "DRY_RUN_MODE", False)
            )
            run = self._empty_run_entry(
                spec,
                status="RUNNING",
                scheduled_time=state["daily_runs"][spec.id].get("scheduled_time")
                or cur.strftime("%H:%M"),
                line_status="กำลังทำงาน",
            )
            run["trigger_source"] = source
            run["started_at"] = cur.strftime("%H:%M:%S")
            run["dry_run"] = is_dry
            state["daily_runs"][spec.id] = run
            self._save_state()

            if kill_switch_service.is_active():
                run["status"] = "BLOCKED_KILL_SWITCH"
                run["finished_at"] = self._now().strftime("%H:%M:%S")
                run["error"] = "KILL-SWITCH ACTIVE — ระงับการต่อราคาอัตโนมัติ"
                run["line_status"] = "งดส่ง LINE (Kill-Switch)"
                self._save_state()
                return self.get_schedule_snapshot(cur)

            if spec.mode == "post_close":
                now_m = cur.hour * 60 + cur.minute
                if source == "scheduler" and (
                    cur.weekday() >= 5 or not (_to_minutes(spec.start) <= now_m < _to_minutes(spec.end))
                ):
                    run["status"] = "MISSED_WINDOW"
                    run["finished_at"] = self._now().strftime("%H:%M:%S")
                    run["error"] = "นอกเวลาทำการหลังปิดตลาด (16:30–17:30)"
                    run["line_status"] = "งดส่ง LINE"
                    self._save_state()
                    return self.get_schedule_snapshot(cur)
                return await self._execute_post_close_window(
                    spec, run=run, state=state, cur=cur, is_dry=is_dry
                )

            if source == "scheduler" and not is_nego_window(cur):
                run["status"] = "MISSED_WINDOW"
                run["finished_at"] = self._now().strftime("%H:%M:%S")
                run["error"] = "นอกเวลาทำการตลาดต่อราคา (market_clock)"
                run["line_status"] = "งดส่ง LINE"
                self._save_state()
                return self.get_schedule_snapshot(cur)

            ready_brokers, broker_errors, critical_reasons = await self._probe_ready_brokers()
            run["broker_errors"] = dict(broker_errors)

            if not ready_brokers:
                run["status"] = "ERROR"
                run["finished_at"] = self._now().strftime("%H:%M:%S")
                err_summary = " · ".join(
                    f"{b.upper()}: {msg}" for b, msg in broker_errors.items()
                ) or "ไม่มีโบรกเกอร์พร้อมใช้งาน"
                run["error"] = err_summary
                pushed = await self._notify_critical_error_once(
                    state,
                    error_key="no_ready_brokers",
                    title=f"🚨 Auto-Nego [{spec.start}-{spec.end}] รันไม่ได้",
                    detail=err_summary,
                )
                run["line_status"] = self._format_error_line_status(pushed)
                self._save_state()
                return self.get_schedule_snapshot(cur)

            crit_line_status: Optional[str] = None
            # Notify immediately if any broker hit PIN lock even if another broker is healthy.
            for crit_key, crit_msg in critical_reasons:
                crit_pushed = await self._notify_critical_error_once(
                    state,
                    error_key=crit_key,
                    title=f"🚨 Auto-Nego [{spec.start}-{spec.end}] คำเตือนความปลอดภัย",
                    detail=crit_msg,
                )
                if crit_pushed is True or crit_line_status is None:
                    crit_line_status = self._format_error_line_status(crit_pushed)

            items: List[Dict[str, Any]] = []
            counts = {"checked": 0, "amended": 0, "no_change": 0, "skipped": 0, "failed": 0}
            failed_msgs: List[str] = []

            for broker in ready_brokers:
                try:
                    snap = await trading_facade.fetch_orders_overview(broker=broker)
                except Exception as exc:
                    broker_errors[broker] = str(exc)
                    run["broker_errors"] = dict(broker_errors)
                    failed_msgs.append(f"{broker.upper()} อ่านออเดอร์ไม่สำเร็จ: {exc}")
                    continue

                b_info = (snap.get("brokers") or {}).get(broker) or {}
                if not b_info.get("available"):
                    broker_errors[broker] = str(b_info.get("error") or "Session ไม่พร้อม")
                    run["broker_errors"] = dict(broker_errors)
                    continue

                open_orders = [o for o in (snap.get("orders") or []) if o.get("cancellable")]
                for order in open_orders:
                    item = await self._process_single_order(spec, order, is_dry=is_dry)
                    items.append(item)
                    counts["checked"] += 1
                    st = item["outcome"]
                    if st in ("AMENDED", "DRY_RUN"):
                        counts["amended"] += 1
                    elif st == "NO_CHANGE":
                        counts["no_change"] += 1
                    elif st in ("SKIPPED_SIDE", "SKIPPED_GUARD", "ALREADY_MATCHED"):
                        counts["skipped"] += 1
                    else:
                        counts["failed"] += 1
                        failed_msgs.append(
                            f"{broker.upper()} {order.get('symbol')} #{order.get('order_no')}: {item.get('reason')}"
                        )

            run["items"] = items
            run["counts"] = counts
            run["finished_at"] = self._now().strftime("%H:%M:%S")

            if failed_msgs:
                run["status"] = "ERROR" if counts["amended"] == 0 else "AMENDED"
                run["error"] = " | ".join(failed_msgs[:3])
                pushed = await self._notify_critical_error_once(
                    state,
                    error_key=f"amend_fail_{spec.id}",
                    title=f"🚨 Auto-Nego [{spec.start}-{spec.end}] มีออเดอร์แก้ราคาไม่สำเร็จ",
                    detail="\n".join(failed_msgs[:5]),
                )
                run["line_status"] = self._format_error_line_status(pushed)
            elif counts["amended"] > 0:
                run["status"] = "AMENDED"
                run["line_status"] = crit_line_status or "งดส่ง LINE (ประหยัดโควต้า)"
            elif counts["no_change"] > 0 or counts["skipped"] > 0:
                run["status"] = "NO_CHANGE"
                run["line_status"] = crit_line_status or "งดส่ง LINE (ประหยัดโควต้า)"
            elif ready_brokers and all(b in broker_errors for b in ready_brokers):
                run["status"] = "ERROR"
                run["error"] = " · ".join(f"{b.upper()}: {m}" for b, m in broker_errors.items())
                pushed = await self._notify_critical_error_once(
                    state,
                    error_key=f"scrape_fail_{spec.id}",
                    title=f"🚨 Auto-Nego [{spec.start}-{spec.end}] อ่านออเดอร์ไม่ได้",
                    detail=run["error"],
                )
                run["line_status"] = self._format_error_line_status(pushed)
            else:
                run["status"] = "NO_ORDERS"
                run["line_status"] = crit_line_status or "งดส่ง LINE (ประหยัดโควต้า)"

            self._save_state()
            return self.get_schedule_snapshot(cur)

    async def _probe_ready_brokers(
        self,
    ) -> Tuple[List[str], Dict[str, str], List[Tuple[str, str]]]:
        """Read-only check for authenticated CDP sessions & PIN guard (Invariant 9 safe)."""
        return await probe_ready_brokers(_BROKERS)

    async def _execute_post_close_window(
        self,
        spec: NegoWindowSpec,
        *,
        run: Dict[str, Any],
        state: Dict[str, Any],
        cur: datetime,
        is_dry: bool,
    ) -> Dict[str, Any]:
        """Cancel unmatched resting orders and auto-return unshorted/covered KSS SBL shares."""
        ready_brokers, broker_errors, critical_reasons = await self._probe_ready_brokers()
        run["broker_errors"] = dict(broker_errors)

        if not ready_brokers:
            run["status"] = "ERROR"
            run["finished_at"] = self._now().strftime("%H:%M:%S")
            err_summary = " · ".join(
                f"{b.upper()}: {msg}" for b, msg in broker_errors.items()
            ) or "ไม่มีโบรกเกอร์พร้อมใช้งาน"
            run["error"] = err_summary
            pushed = await self._notify_critical_error_once(
                state,
                error_key=f"no_ready_brokers_{spec.id}",
                title=f"🚨 Post-Close [{spec.start}-{spec.end}] รันไม่ได้",
                detail=err_summary,
            )
            run["line_status"] = self._format_error_line_status(pushed)
            self._save_state()
            return self.get_schedule_snapshot(cur)

        crit_line_status: Optional[str] = None
        for crit_key, crit_msg in critical_reasons:
            crit_pushed = await self._notify_critical_error_once(
                state,
                error_key=crit_key,
                title=f"🚨 Post-Close [{spec.start}-{spec.end}] คำเตือนความปลอดภัย",
                detail=crit_msg,
            )
            if crit_pushed is True or crit_line_status is None:
                crit_line_status = self._format_error_line_status(crit_pushed)

        items: List[Dict[str, Any]] = []
        counts = {"checked": 0, "amended": 0, "no_change": 0, "skipped": 0, "failed": 0}
        failed_msgs: List[str] = []
        cancelled_cnt = 0
        returned_cnt = 0
        returned_shares = 0

        # 1. Cancel any remaining open orders after close/ATC
        for broker in ready_brokers:
            try:
                snap = await trading_facade.fetch_orders_overview(broker=broker)
            except Exception as exc:
                broker_errors[broker] = str(exc)
                run["broker_errors"] = dict(broker_errors)
                failed_msgs.append(f"{broker.upper()} อ่านออเดอร์ไม่สำเร็จ: {exc}")
                continue

            b_info = (snap.get("brokers") or {}).get(broker) or {}
            if not b_info.get("available"):
                broker_errors[broker] = str(b_info.get("error") or "Session ไม่พร้อม")
                run["broker_errors"] = dict(broker_errors)
                continue

            open_orders = [o for o in (snap.get("orders") or []) if o.get("cancellable")]
            for order in open_orders:
                order_no = str(order.get("order_no") or "")
                sym = str(order.get("symbol") or "").upper()
                side = str(order.get("side") or "").upper()
                bal = int(order.get("balance") or 0)
                px = float(order.get("price") or 0.0)
                counts["checked"] += 1
                base_item = {
                    "kind": "cancel",
                    "broker": broker,
                    "order_no": order_no,
                    "symbol": sym,
                    "side": f"CANCEL {side}",
                    "volume": bal,
                    "old_price": px,
                    "new_price": px,
                    "mode": spec.mode,
                }
                try:
                    await trading_facade.execute_order_cancel(
                        broker=broker,
                        order_no=order_no,
                        symbol=sym,
                        notify_line=False,
                    )
                    cancelled_cnt += 1
                    counts["amended"] += 1
                    items.append({
                        **base_item,
                        "outcome": "DRY_RUN" if is_dry else "CANCELLED",
                        "reason": "จำลองยกเลิกออเดอร์ค้าง (DRY-RUN)" if is_dry else "ยกเลิกออเดอร์ค้างแล้ว",
                    })
                except trading_facade.FacadeHttpError as exc:
                    if exc.status_code == 409 and exc.code in ("ORDER_NOT_OPEN", "ORDER_ALREADY_CLOSED"):
                        counts["no_change"] += 1
                        items.append({
                            **base_item,
                            "outcome": "ALREADY_CLOSED",
                            "reason": exc.message,
                        })
                    else:
                        counts["failed"] += 1
                        failed_msgs.append(f"{broker.upper()} ยกเลิก {sym} #{order_no}: {exc.message}")
                        items.append({**base_item, "outcome": "FAILED", "reason": f"[{exc.code}] {exc.message}"})
                except Exception as exc:
                    counts["failed"] += 1
                    failed_msgs.append(f"{broker.upper()} ยกเลิก {sym} #{order_no}: {exc}")
                    items.append({**base_item, "outcome": "FAILED", "reason": str(exc)})

        # 2. Auto-return any unshorted / covered KSS SBL borrowed shares (avoid overnight fee)
        if "kss" in ready_brokers:
            try:
                port_snap = await trading_facade.fetch_portfolio_overview(broker="kss", force_sbl=True)
            except Exception as exc:
                port_snap = None
                broker_errors["kss"] = str(exc)
                run["broker_errors"] = dict(broker_errors)
                failed_msgs.append(f"KSS อ่านพอร์ต/SBL ไม่สำเร็จ: {exc}")

            if isinstance(port_snap, dict):
                kss_book = (port_snap.get("brokers") or {}).get("kss") or {}
                if not kss_book.get("available"):
                    err_txt = str(kss_book.get("error") or "KSS Portfolio ไม่พร้อม")
                    broker_errors["kss"] = err_txt
                    run["broker_errors"] = dict(broker_errors)
                    failed_msgs.append(f"KSS: {err_txt}")
                else:
                    active_short_by_sym: Dict[str, int] = {}
                    for r in kss_book.get("rows") or []:
                        if str(r.get("position_type") or "").upper() == "SHORT":
                            vol = int(r.get("volume") or 0)
                            sym = str(r.get("symbol") or "").strip().upper()
                            if sym and vol > 0:
                                active_short_by_sym[sym] = active_short_by_sym.get(sym, 0) + vol

                    borrowed_by_sym: Dict[str, int] = {}
                    for lot in ((port_snap.get("sbl") or {}).get("lots") or []):
                        sym = str(lot.get("symbol") or "").strip().upper()
                        rem = int(lot.get("remaining_qty") or lot.get("borrow_qty") or 0)
                        if sym and rem > 0:
                            borrowed_by_sym[sym] = borrowed_by_sym.get(sym, 0) + rem
                    if not borrowed_by_sym:
                        for br in kss_book.get("borrowed") or []:
                            sym = str(br.get("symbol") or "").strip().upper()
                            b_qty = int(br.get("borrow_qty") or br.get("volume") or 0)
                            if sym and b_qty > 0:
                                borrowed_by_sym[sym] = max(borrowed_by_sym.get(sym, 0), b_qty)

                    for sym, total_borrowed in sorted(borrowed_by_sym.items()):
                        active_short = active_short_by_sym.get(sym, 0)
                        raw_ret = max(0, total_borrowed - active_short)
                        ret_qty = (raw_ret // 100) * 100
                        if ret_qty < 100:
                            if raw_ret > 0:
                                counts["checked"] += 1
                                counts["skipped"] += 1
                                items.append({
                                    "kind": "sbl_return",
                                    "broker": "kss",
                                    "order_no": "SBL",
                                    "symbol": sym,
                                    "side": "RETURN SBL",
                                    "volume": raw_ret,
                                    "old_price": 0.0,
                                    "new_price": 0.0,
                                    "mode": spec.mode,
                                    "outcome": "SKIPPED_GUARD",
                                    "reason": f"เศษหุ้น {raw_ret} หุ้น (<100)",
                                })
                            continue

                        counts["checked"] += 1
                        base_ret = {
                            "kind": "sbl_return",
                            "broker": "kss",
                            "order_no": "SBL",
                            "symbol": sym,
                            "side": "RETURN SBL",
                            "volume": ret_qty,
                            "old_price": 0.0,
                            "new_price": 0.0,
                            "mode": spec.mode,
                        }
                        try:
                            res = await trading_facade.execute_sbl_action(
                                action="return",
                                symbol=sym,
                                amount_mode="volume",
                                volume=ret_qty,
                                remark="Auto Post-Close 16:30-17:30",
                                notify_line=False,
                            )
                            returned_cnt += 1
                            returned_shares += ret_qty
                            counts["amended"] += 1
                            items.append({
                                **base_ret,
                                "order_no": str(res.get("lot_id") or "SBL"),
                                "outcome": "DRY_RUN" if is_dry else "RETURNED",
                                "reason": "จำลองคืนหุ้น SBL (DRY-RUN)" if is_dry else f"คืนหุ้น SBL {ret_qty:,} หุ้นสำเร็จ",
                            })
                        except trading_facade.FacadeHttpError as exc:
                            if exc.code in ("CLI_SBL_NO_LOT", "NOT_RETURNABLE"):
                                counts["skipped"] += 1
                                items.append({
                                    **base_ret,
                                    "outcome": "SKIPPED_GUARD",
                                    "reason": exc.message,
                                })
                            else:
                                counts["failed"] += 1
                                failed_msgs.append(f"KSS คืน SBL {sym}: {exc.message}")
                                items.append({**base_ret, "outcome": "FAILED", "reason": f"[{exc.code}] {exc.message}"})
                        except Exception as exc:
                            counts["failed"] += 1
                            failed_msgs.append(f"KSS คืน SBL {sym}: {exc}")
                            items.append({**base_ret, "outcome": "FAILED", "reason": str(exc)})

        run["items"] = items
        run["counts"] = counts
        run["post_close_summary"] = {
            "cancelled": cancelled_cnt,
            "returned": returned_cnt,
            "returned_shares": returned_shares,
        }
        run["finished_at"] = self._now().strftime("%H:%M:%S")

        if failed_msgs:
            run["status"] = "ERROR" if (cancelled_cnt + returned_cnt) == 0 else "DONE"
            run["error"] = " | ".join(failed_msgs[:3])
            pushed = await self._notify_critical_error_once(
                state,
                error_key=f"post_close_fail_{spec.id}",
                title=f"🚨 Post-Close [{spec.start}-{spec.end}] มีรายการไม่สำเร็จ",
                detail="\n".join(failed_msgs[:5]),
            )
            run["line_status"] = self._format_error_line_status(pushed)
        elif (cancelled_cnt + returned_cnt) > 0:
            run["status"] = "DONE"
            run["line_status"] = crit_line_status or "งดส่ง LINE (ประหยัดโควต้า)"
        elif counts["no_change"] > 0 or counts["skipped"] > 0:
            run["status"] = "NO_CHANGE"
            run["line_status"] = crit_line_status or "งดส่ง LINE (ประหยัดโควต้า)"
        else:
            run["status"] = "NO_ORDERS"
            run["line_status"] = crit_line_status or "งดส่ง LINE (ประหยัดโควต้า)"

        self._save_state()
        return self.get_schedule_snapshot(cur)

    async def _process_single_order(
        self,
        spec: NegoWindowSpec,
        order: Dict[str, Any],
        *,
        is_dry: bool,
    ) -> Dict[str, Any]:
        broker = str(order.get("broker") or "").lower()
        order_no = str(order.get("order_no") or "")
        symbol = str(order.get("symbol") or "").upper()
        side = str(order.get("side") or "").upper()
        old_price = float(order.get("price") or 0.0)
        balance = int(order.get("balance") or 0)

        base = {
            "broker": broker,
            "order_no": order_no,
            "symbol": symbol,
            "side": side,
            "volume": balance,
            "old_price": old_price,
            "new_price": old_price,
            "mode": spec.mode,
        }

        if side not in spec.allowed_sides:
            return {
                **base,
                "outcome": "SKIPPED_SIDE",
                "reason": f"ข้าม {side} ตามกติกาช่วง {spec.start}-{spec.end}",
            }

        if spec.mode == "match":
            eff_mode = "match" if side in ("SELL", "COVER") else "lure"
        else:
            eff_mode = spec.mode

        try:
            res = await trading_facade.execute_nego_amend(
                broker=broker,
                order_no=order_no,
                symbol=symbol,
                side=side,
                volume=balance,
                old_price=old_price,
                mode=eff_mode,
                notify_line=False,
            )
        except trading_facade.FacadeHttpError as exc:
            if _is_already_matched_error(exc.code, exc.message):
                return {**base, "outcome": "ALREADY_MATCHED", "reason": exc.message}
            if exc.status_code == 422 and exc.code in ("NEGO_PLAN_FAILED", "NEGO_SHORT_MATCH_NOT_ALLOWED"):
                return {**base, "outcome": "SKIPPED_GUARD", "reason": exc.message}
            return {**base, "outcome": "FAILED", "reason": f"[{exc.code}] {exc.message}"}
        except Exception as exc:
            return {**base, "outcome": "FAILED", "reason": str(exc)}

        if res.get("no_change"):
            return {
                **base,
                "outcome": "NO_CHANGE",
                "new_price": float(res.get("new_price") or old_price),
                "reason": "ราคาเดิมตรงเป้าหมายแล้ว",
            }

        return {
            **base,
            "outcome": "DRY_RUN" if is_dry else "AMENDED",
            "new_price": float(res.get("new_price") or old_price),
            "reason": "จำลองแก้ราคา (DRY-RUN)" if is_dry else "แก้ราคาสำเร็จ",
        }

    @staticmethod
    def _is_line_quota_exhausted() -> bool:
        try:
            from app.services.line_service import line_service

            return bool(getattr(line_service, "_push_quota_exhausted", False))
        except Exception:
            return False

    def _format_error_line_status(self, pushed: Optional[bool]) -> str:
        return format_error_line_status(pushed)

    async def _notify_critical_error_once(
        self,
        state: Dict[str, Any],
        *,
        error_key: str,
        title: str,
        detail: str,
    ) -> Optional[bool]:
        """Record error in MiniApp Inbox and (if LINE_PUSH_MODE=all) push LINE once/day."""
        return await notify_critical_error_once(
            state,
            error_key=error_key,
            title=title,
            detail=detail,
            category="nego",
            action_tab="schedule",
            schedule_label="Schedule",
        )


async def probe_ready_brokers(
    brokers: Tuple[str, ...] = _BROKERS,
) -> Tuple[List[str], Dict[str, str], List[Tuple[str, str]]]:
    """Read-only check for authenticated CDP sessions & PIN guard (Invariant 9 safe)."""
    ready: List[str] = []
    errors: Dict[str, str] = {}
    criticals: List[Tuple[str, str]] = []
    is_live = bool(trade_dispatcher.is_live)

    for broker in brokers:
        pg = pin_guard.check(broker)
        if pg.get("locked"):
            msg = f"PIN ถูกล็อกชั่วคราว ({pg.get('reason') or 'PIN_LOCKED'})"
            errors[broker] = msg
            criticals.append((f"pin_lock_{broker}", f"{broker.upper()}: {msg}"))
            continue

        if is_live:
            purpose = "streaming" if broker == "kss" else "portfolio"
            try:
                probe = await cdp_session_status(broker, purpose=purpose)
            except Exception as exc:
                errors[broker] = f"ตรวจ Session ไม่ได้: {exc}"
                continue
            if probe.get("state") != "AUTHENTICATED":
                st = str(probe.get("state") or "UNKNOWN")
                errors[broker] = f"Session ไม่พร้อม ({st})"
                continue

        ready.append(broker)

    return ready, errors, criticals


async def notify_critical_error_once(
    state: Dict[str, Any],
    *,
    error_key: str,
    title: str,
    detail: str,
    category: str = "nego",
    action_tab: str = "schedule",
    schedule_label: str = "Schedule",
) -> Optional[bool]:
    """Record error in MiniApp Inbox and (if LINE_PUSH_MODE=all) push LINE once/day."""
    dedup_key = f"{state.get('daily_date')}:{error_key}"
    sent_keys = state.setdefault("sent_error_keys", [])
    if dedup_key in sent_keys:
        return None

    try:
        from app.services.miniapp_inbox import miniapp_inbox

        await miniapp_inbox.publish(
            category=category,
            severity="error",
            title=title,
            body=detail,
            action_tab=action_tab,
            line_pushed=False,
            meta={"error_key": error_key, "dedup_key": dedup_key},
        )
    except Exception as exc:
        logger.debug("MiniApp Inbox publish skipped in scheduler: %s", exc)

    try:
        from app.services.line_service import line_service, push_context

        mode = (getattr(settings, "LINE_PUSH_MODE", "eod_only") or "eod_only").strip().lower()
        if mode == "all" and getattr(line_service, "_push_quota_exhausted", False):
            return False
        msg = f"{title}\n{detail}\n\n📋 ดู Checklist เพิ่มใน MiniApp แท็บ {schedule_label}"
        with push_context(category=category, inbox_severity="error", action_tab=action_tab, record_inbox=False):
            ok = bool(await line_service.push_message(msg))
        if ok or mode != "all":
            sent_keys.append(dedup_key)
        return ok
    except Exception as exc:
        logger.warning("Critical error LINE push failed: %s", exc)
        if (getattr(settings, "LINE_PUSH_MODE", "eod_only") or "eod_only").strip().lower() != "all":
            sent_keys.append(dedup_key)
        return False


def format_error_line_status(pushed: Optional[bool]) -> str:
    if pushed is True:
        return "📨 แจ้ง Error LINE แล้ว"
    if pushed is False and (getattr(settings, "LINE_PUSH_MODE", "eod_only") or "eod_only").lower() != "all":
        return "📥 แจ้ง Error เข้า MiniApp Inbox แล้ว"
    if pushed is False and NegoScheduler._is_line_quota_exhausted():
        return "⚠️ LINE โควต้าเต็ม (แจ้งไม่ได้)"
    if pushed is None:
        return "📨 แจ้ง Error ไปแล้วก่อนหน้า (Dedup)"
    return "⚠️ ส่ง Error LINE ไม่สำเร็จ"


nego_scheduler = NegoScheduler()
