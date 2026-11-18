#!/usr/bin/env python3
"""
Daily Readiness Skill Runner — Investment Homelab
=================================================
คำสั่งเดียวที่ใช้ทุกวัน: **เตรียมความพร้อม OTP Relay (LINE group "a14s homelab") +
Broker Sessions** แล้วคืน GO / NO-GO (exit code) เพื่อให้ไปทดสอบ pillar อื่นได้ทันที

Design (Loop Engineering / Single Source of Truth):
  * OTP relay      -> app.services.otp_relay_readiness.check()  (Invariant 9 — LINE variant)
                      พร้อมเมื่อ: LINE token จริง + LINE_SIMULATION_MODE=False +
                      มี push target (LINE_GROUP_ID ของกลุ่ม "a14s homelab" **หรือ**
                      LINE_ADMIN_USER_ID ของแชท 1:1 — LINE-RCA-006) + backend :8000 listen
                      (KDE Connect ถูกทำเป็น dormant — ไม่ได้ลบ)
  * Krungsri (KSS) -> Pure CDP (:9333): `cdp_session_status("kss", "streaming" + "sbl")`,
                      `ensure_daemon` / `restart_daemon`, `ensure_kss_tabs_complete`,
                      `trigger_cdp_login("kss", wait_until_complete=True)` (TradeMan retired)
  * Liberator      -> Pure CDP (:9334): `cdp_session_status("lib", "portfolio")`,
                      `ensure_daemon` / `restart_daemon`,
                      `trigger_cdp_login("liberator", wait_until_complete=True)`
  * Click2Win      -> file presence only (sandbox, ไม่มี JWT, auto web login ไม่มี OTP)
  * Backend :8000  -> TCP probe + เป็น *gate* ของ OTP relay (LINE webhook เข้ามาที่นี่)

Safety gates (บังคับ):
  * Invariant 9  — ห้ามยิง SMS OTP ถ้า relay ไม่พร้อม: auto re-login ต้องผ่าน
                    otp_relay_readiness.check().ready == True ก่อนทุกครั้ง (ไม่งั้น status=BRIDGE_DOWN)
  * Anti-Mock    — ไม่มีการคืนค่า/ยอดเสมือน; รายงานตามที่ตรวจได้จริงเท่านั้น
  * Broker Knowledge Isolation — ใช้ semantics ของ driver แต่ละโบรกเกอร์แยกกัน
  * User decision (2026-10-08): auto re-login ทันทีเมื่อ relay พร้อม (มือว่างจบในตัวเดียว)

Usage:
  .venv/bin/python scripts/daily_readiness.py                # เตรียมให้พร้อม + GO/NO-GO
  .venv/bin/python scripts/daily_readiness.py --json         # สรุปแบบ machine-readable
  .venv/bin/python scripts/daily_readiness.py --no-login     # ตรวจอย่างเดียว (report-only)
  .venv/bin/python scripts/daily_readiness.py --skip-liberator
  .venv/bin/python scripts/daily_readiness.py --click2win    # auto-login Click2Win ด้วย

Exit code: 0 = GO, 1 = NO-GO
"""

import argparse
import asyncio
import json
import os
import socket
import sys
import time
from datetime import datetime
from typing import Any, Dict

# Ensure repo root importable when run as `python scripts/daily_readiness.py`
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# Headed login (Krungsri/Liberator) needs the physical display (mirrors start_homelab.py)
os.environ.setdefault("DISPLAY", ":0")

# ANSI colors (repo convention — same palette as scripts/preflight_check.py)
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BLUE = "\033[94m"
BOLD = "\033[1m"
RESET = "\033[0m"

# LINE group OTP relay (2026-10-08) — primary OTP channel
LINE_GROUP_NAME = "a14s homelab"
RELAY_FIX_BACKEND = ".venv/bin/python scripts/start_homelab.py  (หรือ uvicorn app.main:app --host 0.0.0.0 --port 8000)"
# Dormant legacy KDE Connect checklist (kept for zero-contradiction; no longer the gate)
MOBILE_CHECKLIST = "learned_workflows/shared/kdeconnect_mobile_otp_checklist.md"
DEVICE_ID = "9507a3fd53f848da9135ebe6253db18a"  # Mi 10T Pro (settings.KDECONNECT_DEVICE_ID)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _hr() -> None:
    print(f"{BOLD}{BLUE}{'=' * 64}{RESET}")


def check_backend() -> Dict[str, Any]:
    """TCP report only. start_homelab.py คือผู้ที่ launch uvicorn (ไม่ทำที่นี่)."""
    up = _port_open("127.0.0.1", 8000)
    print(f"\n{BOLD}0. 🔌 Backend Gateway (uvicorn :8000):{RESET}")
    if up:
        print(f"   {GREEN}✅ FastAPI Gateway is UP on port 8000.{RESET}")
    else:
        print(f"   {YELLOW}⚠️  Port 8000 is DOWN — เปิดด้วย: "
              f"'.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000'{RESET}")
    return {"up": up}


async def ensure_mobile_bridge(args: argparse.Namespace) -> Dict[str, Any]:
    """
    Gate หลักของทั้งระบบ: ช่องทาง OTP relay ต้อง live ก่อนจะยิง login ใด ๆ (Invariant 9).

    ช่องทางปัจจุบัน (2026-10-08): LINE relay — กลุ่ม "a14s homelab" (LINE_GROUP_ID) หรือ
    แชท 1:1 กับ OA (LINE_ADMIN_USER_ID, LINE-RCA-006)
    ตรวจผ่าน `app.services.otp_relay_readiness` (single source of truth — ตัวเดียวกับที่
    auth_driver / liberator_driver ใช้จริง) health check กับ gate จึงไม่ขัดกันเอง

    คืนค่า dict ที่มี key `reachable` / `refreshed` / `pinged` เหมือนเดิม เพื่อความเข้ากันได้
    กับผู้เรียกและ unit test เดิม (Zero-Contradiction)
    """
    from app.core.config import settings
    from app.services.otp_relay_readiness import otp_relay_readiness

    print(f"\n{BOLD}1. 📱 OTP Relay (LINE: กลุ่ม '{LINE_GROUP_NAME}' หรือแชท 1:1):{RESET}")
    print(f"   {BLUE}webhook:{RESET} {getattr(settings, 'LINE_PUBLIC_WEBHOOK_URL', '')}")

    readiness = await otp_relay_readiness.check()
    reachable = bool(readiness.ready)
    refreshed = False  # kept for backward compatibility (ไม่มี refresh step ในเส้นทาง LINE)

    if reachable:
        if readiness.channel == "miniapp":
            target_desc = "HomeApp MiniApp (LIFF)"
        elif readiness.channel == "line_group":
            target_desc = f"group '{LINE_GROUP_NAME}'"
        else:
            target_desc = "1:1 chat with the OA"
        print(f"   {GREEN}✅ relay พร้อม (channel={readiness.channel}) — {target_desc} + gateway up{RESET}")
    else:
        print(f"   {RED}❌ relay ไม่พร้อม: {readiness.reason}{RESET}")
        for k, v in readiness.details.items():
            print(f"      • {k} = {v}")

    pinged = False
    if reachable and not args.no_ping:
        # Human-confirm (stepwise-approval §5): push a readiness note into the LINE group,
        # unless push quota (HTTP 429) is exhausted (incoming OTP webhook stays 100% live).
        from app.services.line_service import line_service

        quota_exhausted = bool(
            (readiness.details or {}).get("push_quota_exhausted")
            or getattr(line_service, "_push_quota_exhausted", False)
        )
        if quota_exhausted:
            print(
                f"   {YELLOW}ℹ️  LINE Push quota เต็ม (HTTP 429) — ข้ามการยิง ping ล่วงหน้า "
                f"(Webhook รับรหัส OTP ขาเข้ายังทำงานได้ปกติ){RESET}"
            )
        else:
            from app.services.line_service import push_context

            try:
                with push_context(category="readiness", action_tab="brokers", inbox_title="OTP Relay พร้อมใช้งาน"):
                    pinged = await line_service.push_message(
                        f"✅ [{LINE_GROUP_NAME}] OTP relay พร้อม — เริ่มเตรียม session ได้\n"
                        "(เมื่อมีคำขอ OTP ระบบจะแจ้งในกลุ่มนี้ ให้พิมพ์รหัส 6 หลักตอบกลับได้เลย)"
                    )
            except Exception as e:
                print(f"   {YELLOW}   LINE push notice: {e}{RESET}")
            if pinged:
                print(f"   {GREEN}✅ ส่งข้อความยืนยันเข้าแชท LINE แล้ว (human confirm: เช็คในกลุ่ม หรือในแชท 1:1 กับ OA){RESET}")
            elif (getattr(settings, "LINE_PUSH_MODE", "eod_only") or "eod_only").lower() != "all":
                print(f"   {GREEN}📥 บันทึกสถานะพร้อมใช้งานเข้า MiniApp Inbox แล้ว (LINE_PUSH_MODE=eod_only){RESET}")
            else:
                print(f"   {YELLOW}⚠️  ส่งข้อความยืนยันเข้าแชท LINE ไม่สำเร็จ{RESET}")

    if not reachable:
        # HITL escalation — ไม่วนไม่รู้จบ (root_cause_and_escalation Invariant 4)
        print(f"   {RED}⛔ relay DOWN — login ใหม่จะถูกยกเลิก (ห้ามยิง SMS OTP){RESET}")
        print(f"   {YELLOW}👉 HITL: แก้ตามลำดับนี้แล้วรันซ้ำ{RESET}")
        print(f"      1) เปิด backend (ต้อง listen :8000): {RELAY_FIX_BACKEND}")
        print(f"      2) ตรวจ tunnel: {getattr(settings, 'LINE_PUBLIC_WEBHOOK_URL', '')}")
        print(f"      3) พิมพ์ข้อความ 1 ครั้งในกลุ่ม '{LINE_GROUP_NAME}' หรือในแชท 1:1 กับ OA")
        print(f"         (webhook จะ auto-capture LINE_GROUP_ID / LINE_ADMIN_USER_ID ลง .env ให้เอง)")
        print(f"   {YELLOW}   (dormant KDE Connect checklist: {MOBILE_CHECKLIST}){RESET}")
        try:
            from app.services.line_service import line_service
            await line_service.send_system_notification(
                "OTP relay ไม่พร้อม",
                readiness.reason,
                action_required=f"เปิด backend :8000 + พิมพ์ข้อความ 1 ครั้งในกลุ่ม LINE '{LINE_GROUP_NAME}' หรือในแชท 1:1 กับ OA",
                severity="CRITICAL",
            )
        except Exception:
            pass

    return {
        "reachable": reachable,
        "refreshed": refreshed,
        "pinged": pinged,
        "channel": readiness.channel,
        "reason": readiness.reason,
    }


async def ensure_krungsri(args: argparse.Namespace, bridge_ok: bool) -> Dict[str, Any]:
    """
    Krungsri Securities (KSS Pure CDP :9333):
      - ตรวจทั้งแท็บ `streaming` (Settrade Streaming) และแท็บ `sbl` (MainSBL.aspx)
      - หาก `streaming` ผ่านแต่ `sbl` หาย สั่ง `ensure_kss_tabs_complete()` ซ่อมแบบ Zero-OTP
      - หากต้องล็อกอินใหม่ Gate ด้วย `bridge_ok` (Invariant 9) เสมอ
    """
    from app.services.broker_cdp import daemon_launcher
    from app.services.broker_cdp import session_probe

    print(f"\n{BOLD}2. 🔑 Krungsri Securities CDP Session (:9333 — Streaming + SBL):{RESET}")

    if args.force_fresh and not args.no_login:
        str_state = "FORCE_FRESH"
        sbl_state = "FORCE_FRESH"
    else:
        str_probe = await session_probe.cdp_session_status("kss", purpose="streaming")
        sbl_probe = await session_probe.cdp_session_status("kss", purpose="sbl")
        str_state = str_probe.get("state", "DAEMON_DOWN")
        sbl_state = sbl_probe.get("state", "DAEMON_DOWN")

    verdict_str = f"streaming={str_state},sbl={sbl_state}"
    print(f"   port: 9333 | {verdict_str}")

    if args.no_login:
        both_auth = str_state == "AUTHENTICATED" and sbl_state == "AUTHENTICATED"
        print(f"   {YELLOW}⏭️  --no-login: ข้าม auto re-login (report-only){RESET}")
        return {
            "status": "SKIPPED",
            "authenticated": None,
            "streaming_state": str_state,
            "sbl_state": sbl_state,
            "needs_fresh_login": not both_auth,
            "verdict": verdict_str,
            "port": 9333,
            "message": "",
        }

    if not args.force_fresh and str_state == "AUTHENTICATED":
        if sbl_state != "AUTHENTICATED":
            print(f"   … streaming พร้อมแล้ว แต่ sbl={sbl_state} -> ซ่อมแท็บผ่าน SSO (Zero-OTP)…")
            heal_res = await daemon_launcher.ensure_kss_tabs_complete()
            sbl_probe = await session_probe.cdp_session_status("kss", purpose="sbl")
            sbl_state = sbl_probe.get("state", heal_res.get("sbl_state", sbl_state))
            verdict_str = f"streaming={str_state},sbl={sbl_state}"
        if str_state == "AUTHENTICATED" and sbl_state == "AUTHENTICATED":
            print(f"   {GREEN}✅ Krungsri CDP พร้อมใช้งานครบทั้ง Streaming + SBL Real Time{RESET}")
            return {
                "status": "AUTHENTICATED",
                "authenticated": True,
                "streaming_state": str_state,
                "sbl_state": sbl_state,
                "needs_fresh_login": False,
                "verdict": verdict_str,
                "port": 9333,
                "message": "CDP session active",
            }

    if not bridge_ok:
        print(f"   {RED}⛔ BRIDGE_DOWN — ยกเลิก auto login (ไม่ยิง SMS OTP; Invariant 9){RESET}")
        return {
            "status": "BRIDGE_DOWN",
            "authenticated": None,
            "streaming_state": str_state,
            "sbl_state": sbl_state,
            "needs_fresh_login": True,
            "verdict": verdict_str,
            "port": 9333,
            "message": "OTP relay down",
        }

    print(f"   … เตรียม KSS CDP (:9333) ผ่าน daemon_launcher (Pure CDP)…")
    try:
        if args.force_fresh:
            daemon_res = await daemon_launcher.restart_daemon("kss")
        else:
            daemon_res = await daemon_launcher.ensure_daemon("kss")

        if daemon_res.get("state") != "AUTHENTICATED":
            daemon_res = await daemon_launcher.trigger_cdp_login("kss", wait_until_complete=True)

        str_probe = await session_probe.cdp_session_status("kss", purpose="streaming")
        sbl_probe = await session_probe.cdp_session_status("kss", purpose="sbl")
        str_state = str_probe.get("state", daemon_res.get("state", "UNKNOWN"))
        sbl_state = sbl_probe.get("state", daemon_res.get("sbl_state", "UNKNOWN"))

        if str_state == "AUTHENTICATED" and sbl_state != "AUTHENTICATED":
            heal_res = await daemon_launcher.ensure_kss_tabs_complete()
            sbl_probe = await session_probe.cdp_session_status("kss", purpose="sbl")
            sbl_state = sbl_probe.get("state", heal_res.get("sbl_state", sbl_state))

        verdict_str = f"streaming={str_state},sbl={sbl_state}"
        both_ok = str_state == "AUTHENTICATED" and sbl_state == "AUTHENTICATED"
        out = {
            "status": "AUTHENTICATED" if both_ok else "FAILED",
            "authenticated": both_ok,
            "streaming_state": str_state,
            "sbl_state": sbl_state,
            "needs_fresh_login": not both_ok,
            "verdict": verdict_str,
            "port": 9333,
            "message": "CDP session active" if both_ok else f"Incomplete CDP state ({verdict_str})",
        }
    except Exception as e:
        out = {
            "status": "ERROR",
            "authenticated": False,
            "streaming_state": str_state,
            "sbl_state": sbl_state,
            "needs_fresh_login": True,
            "verdict": verdict_str,
            "port": 9333,
            "message": str(e),
        }

    if out["authenticated"]:
        print(f"   {GREEN}✅ Krungsri CDP พร้อมใช้งานครบทั้ง Streaming + SBL Real Time{RESET}")
    else:
        print(f"   {RED}❌ Krungsri CDP login ไม่สำเร็จ: {out['message']}{RESET}")
    return out


async def ensure_liberator(args: argparse.Namespace, bridge_ok: bool) -> Dict[str, Any]:
    """
    Liberator WebTrade (Pure CDP :9334):
      - ตรวจแท็บ `portfolio` ผ่าน `cdp_session_status("lib", purpose="portfolio")`
      - Gate ด้วย `bridge_ok` (Invariant 9) ก่อนเรียก `ensure_daemon` / `trigger_cdp_login` เสมอ
    """
    from app.services.broker_cdp import daemon_launcher
    from app.services.broker_cdp import session_probe

    print(f"\n{BOLD}3. 🛡️ Liberator WebTrade CDP Session (:9334 — Portfolio + Trade):{RESET}")

    if args.force_fresh and not args.no_login:
        port_state = "FORCE_FRESH"
    else:
        port_probe = await session_probe.cdp_session_status("lib", purpose="portfolio")
        port_state = port_probe.get("state", "DAEMON_DOWN")

    print(f"   port: 9334 | portfolio={port_state}")

    if args.no_login:
        is_auth = port_state == "AUTHENTICATED"
        print(f"   {YELLOW}⏭️  --no-login: ข้าม auto re-login (report-only){RESET}")
        return {
            "status": "SKIPPED",
            "authenticated": None,
            "state": port_state,
            "exists": is_auth,
            "expired": not is_auth,
            "port": 9334,
            "message": "",
        }

    if not args.force_fresh and port_state == "AUTHENTICATED":
        print(f"   {GREEN}✅ Liberator CDP พร้อมใช้งาน{RESET}")
        return {
            "status": "AUTHENTICATED",
            "authenticated": True,
            "state": port_state,
            "exists": True,
            "expired": False,
            "port": 9334,
            "message": "CDP session active",
        }

    if not bridge_ok:
        print(f"   {RED}⛔ BRIDGE_DOWN — ยกเลิก auto login (ไม่ยิง SMS OTP; Invariant 9){RESET}")
        return {
            "status": "BRIDGE_DOWN",
            "authenticated": None,
            "state": port_state,
            "exists": False,
            "expired": True,
            "port": 9334,
            "message": "OTP relay down",
        }

    print(f"   … เตรียม Liberator CDP (:9334) ผ่าน daemon_launcher (Pure CDP)…")
    try:
        if args.force_fresh:
            daemon_res = await daemon_launcher.restart_daemon("liberator")
        else:
            daemon_res = await daemon_launcher.ensure_daemon("liberator")

        if daemon_res.get("state") != "AUTHENTICATED":
            daemon_res = await daemon_launcher.trigger_cdp_login("liberator", wait_until_complete=True)

        port_probe = await session_probe.cdp_session_status("lib", purpose="portfolio")
        port_state = port_probe.get("state", daemon_res.get("state", "UNKNOWN"))
        if daemon_res.get("state") == "AUTHENTICATED" and port_state != "AUTHENTICATED":
            port_state = "AUTHENTICATED"

        ok = port_state == "AUTHENTICATED"
        out = {
            "status": "AUTHENTICATED" if ok else "FAILED",
            "authenticated": ok,
            "state": port_state,
            "exists": ok,
            "expired": not ok,
            "port": 9334,
            "message": "CDP session active" if ok else f"Liberator state={port_state}",
        }
    except Exception as e:
        out = {
            "status": "ERROR",
            "authenticated": False,
            "state": port_state,
            "exists": False,
            "expired": True,
            "port": 9334,
            "message": str(e),
        }

    if out["authenticated"]:
        print(f"   {GREEN}✅ Liberator CDP พร้อมใช้งาน{RESET}")
    else:
        print(f"   {RED}❌ Liberator CDP login ไม่สำเร็จ: {out['message']}{RESET}")
    return out


def inspect_click2win(args: argparse.Namespace) -> Dict[str, Any]:
    """
    Click2Win (Settrade sandbox) ไม่มี JWT → ตรวจได้แค่ file presence + cookie count
    (ตรงกับ semantics ของ preflight_check.click2win_session_status — ไม่รายงาน ✅ เท็จ)
    """
    from app.core.config import settings

    print(f"\n{BOLD}4. 🎮 Settrade Click2Win (Simulation):{RESET}")
    path = getattr(settings, "CLICK2WIN_SESSION_PATH", "data/click2win_session.json")
    exists = os.path.exists(path)
    cookies = 0
    age_h = None
    if exists:
        age_h = (time.time() - os.path.getmtime(path)) / 3600.0
        try:
            with open(path, "r", encoding="utf-8") as f:
                cookies = len(json.load(f).get("cookies", []))
        except Exception:
            pass

    if exists:
        print(f"   {BLUE}ℹ️  Saved file present ({cookies} cookies, {age_h:.1f}h old).{RESET}")
    else:
        print(f"   {BLUE}ℹ️  No saved session — ใช้ auto web login (ไม่มี SMS OTP){RESET}")
    if args.click2win:
        print(f"   {YELLOW}ℹ️  --click2win: (optional) auto web-login ยังไม่ผูกใน runner นี้ "
              f"— ใช้ 'Connect All Brokers' ที่ /console หรือ MCP broker_login แทน{RESET}")
    return {"path": path, "exists": exists, "cookies": cookies, "age_hours": age_h}


def _krungsri_ready(res: Dict[str, Any]) -> bool:
    """
    ตัดสินความพร้อม Krungsri แบบซื่อตรง (Anti-Mock):
      AUTHENTICATED        -> True
      FAILED/ERROR/BRIDGE_DOWN -> False
      SKIPPED (report-only)-> ใช้ offline verdict: RECENT = พร้อม (ยังไม่ต้อง re-login)
    """
    st = res.get("status")
    if st == "AUTHENTICATED":
        return True
    if st in ("FAILED", "ERROR", "BRIDGE_DOWN"):
        return False
    if st == "SKIPPED":
        return not bool(res.get("needs_fresh_login", True))
    return False


def _liberator_ready(res: Dict[str, Any]) -> bool:
    """แบบเดียวกับ Krungsri แต่ใช้ JWT guard ของ Liberator (expired / exists)."""
    st = res.get("status")
    if st == "AUTHENTICATED":
        return True
    if st in ("FAILED", "ERROR", "BRIDGE_DOWN"):
        return False
    if st == "SKIPPED":
        return bool(res.get("exists")) and not bool(res.get("expired", True))
    return False


async def run(args: argparse.Namespace) -> Dict[str, Any]:
    _hr()
    print(f"{BOLD}{BLUE}  🚀 DAILY READINESS — Mobile Bridge + Broker Sessions{RESET}")
    print(f"   {datetime.now().strftime('%A %Y-%m-%d %H:%M:%S')}")
    _hr()

    backend = check_backend()

    bridge = await ensure_mobile_bridge(args)
    bridge_ok = bool(bridge["reachable"])

    krungsri = await ensure_krungsri(args, bridge_ok)
    liberator: Dict[str, Any] = {"status": "SKIPPED", "authenticated": None, "message": "skipped"}
    if not args.skip_liberator:
        liberator = await ensure_liberator(args, bridge_ok)
    else:
        print(f"\n{BOLD}3. 🛡️ Liberator WebTrade Session (Live):{RESET}")
        print(f"   {YELLOW}⏭️  --skip-liberator: ข้าม{RESET}")

    click2win = inspect_click2win(args)

    # ---- GO / NO-GO decision -------------------------------------------------
    # NOTE: ในโหมด --no-login (report-only) เรา *ตั้งใจ* ข้าม login จึงตัดสินด้วย
    # offline verdict (session file) แทน `authenticated` — ห้ามรายงาน NO-GO ปลอม
    # จากเหตุ "ข้ามโดยเจตนา" (Anti-Mock: verdict ต้องสะท้อนความจริง)
    reasons = []
    if not bridge_ok:
        reasons.append(
            f"OTP relay (LINE) DOWN — OTP bridge ไม่พร้อม: {bridge.get('reason')}"
        )
    if not _krungsri_ready(krungsri):
        reasons.append(f"Krungsri CDP ไม่พร้อม ({krungsri.get('status')} / verdict={krungsri.get('verdict')})")
    if not args.skip_liberator and not _liberator_ready(liberator):
        reasons.append(f"Liberator ไม่พร้อม ({liberator.get('status')})")

    verdict = "GO" if not reasons else "NO-GO"

    print(f"\n{BOLD}{BLUE}{'=' * 64}{RESET}")
    if verdict == "GO":
        print(f"{BOLD}{GREEN}  ✅ GO — พร้อมทดสอบ pillar ถัดไป{RESET}")
        print(f"   • OTP relay     : LIVE (channel={bridge.get('channel')})")
        print(f"   • Krungsri      : {krungsri.get('status')}")
        if not args.skip_liberator:
            print(f"   • Liberator     : {liberator.get('status')}")
    else:
        print(f"{BOLD}{RED}  ⛔ NO-GO — ยังไม่พร้อม (ต้อง HITL){RESET}")
        for r in reasons:
            print(f"   • {r}")
        if not bridge_ok:
            print(f"   👉 เปิด backend :8000 + พิมพ์ข้อความ 1 ครั้งในกลุ่ม LINE '{LINE_GROUP_NAME}' หรือในแชท 1:1 กับ OA แล้วรันซ้ำ")
            print(f"      (ห้ามยิง SMS OTP ถ้า relay ไม่พร้อม — Invariant 9)")
    print(f"{BOLD}{BLUE}{'=' * 64}{RESET}")

    summary = {
        "timestamp": _now_iso(),
        "backend": backend,
        "mobile_bridge": bridge,
        "krungsri": krungsri,
        "liberator": liberator,
        "click2win": click2win,
        "verdict": verdict,
        "reasons": reasons,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Daily readiness: prepare the LINE group OTP relay + broker sessions (GO/NO-GO)."
    )
    parser.add_argument("--json", action="store_true", help="พิมพ์สรุปแบบ JSON ท้ายสุด")
    parser.add_argument("--no-login", action="store_true",
                        help="ตรวจอย่างเดียว ไม่ auto re-login (report-only)")
    parser.add_argument("--force-fresh", action="store_true",
                        help="บังคับ fresh login (purge cookies + SMS OTP)")
    parser.add_argument("--no-ping", action="store_true",
                        help="ไม่ส่งข้อความยืนยันเข้าแชท LINE (กลุ่ม/1:1)")
    parser.add_argument("--skip-liberator", action="store_true", help="ข้าม Liberator")
    parser.add_argument("--click2win", action="store_true", help="(optional) พิจารณา Click2Win")
    return parser


def parse_args(argv=None) -> argparse.Namespace:
    """แยกออกมาเพื่อให้ unit test เรียกได้โดยไม่ต้องรัน asyncio.run()."""
    return build_parser().parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    summary = asyncio.run(run(args))
    return 0 if summary["verdict"] == "GO" else 1


if __name__ == "__main__":
    sys.exit(main())

