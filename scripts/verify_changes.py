#!/usr/bin/env python3
"""
Verify Loop Hook — Investment Homelab
=====================================
คำสั่งเดียวสำหรับ "verify 1 unit" ใน Loop Engineering (ห้ามรัน full suite ต่อ loop):
  * compileall ไฟล์ .py ที่แก้ (syntax gate — เร็วมาก)
  * รันเฉพาะ **test ที่แมปกับไฟล์ที่แก้** จาก MODULE_TEST_MAP (targeted verify)
  * single-writer guard — ไม่ยอมรันถ้ามี pytest ตัวอื่นรันอยู่ (test_homelab.db ห้าม concurrent
    — พิสูจน์แล้ว 2026-10-08 ว่า 2 pytest พร้อมกัน deadlock บน SQLite ตัวเดียว)

Design:
  * Targeted > Big-Bang — ต่อ 1 unit เรียกสคริปต์นี้ครั้งเดียว จบด้วย exit code (ดู `.agents/rules/loop_engineering.md` §3)
  * Anti-Mock — ไฟล์ที่ไม่มี mapping จะ **ไม่เดา** ให้รัน (exit 2) จนกว่าจะเพิ่ม mapping หรือใช้ `--tests`
  * Full suite = ใช้ `--full` **ครั้งเดียวตอนปิดงาน** เท่านั้น (ไม่ใช่ทุก loop)

Usage:
  .venv/bin/python scripts/verify_changes.py app/services/line_service.py app/schemas/auth.py
  .venv/bin/python scripts/verify_changes.py --fast app/services/line_service.py    # inner loop (-k symbols)
  .venv/bin/python scripts/verify_changes.py --impacted app/core/config.py          # task close (MAP + AST imports)
  .venv/bin/python scripts/verify_changes.py --dry-run app/browser/auth_driver.py   # ดูแผนก่อน ไม่รัน
  .venv/bin/python scripts/verify_changes.py --tests tests/test_webhook_line.py     # override mapping
  .venv/bin/python scripts/verify_changes.py --full                                 # ปลายวัน (batched, daily_close.sh)

Exit code: 0 = PASS, 1 = FAIL, 2 = BLOCKED / UNMAPPED / usage
"""

import argparse
import ast
import os
import subprocess
import sys
import time
import warnings
from typing import Dict, List, Optional, Sequence, Set, Tuple

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BLUE = "\033[94m"
BOLD = "\033[1m"
RESET = "\033[0m"

PYTHON = os.path.join(REPO_ROOT, ".venv", "bin", "python")
if not os.path.exists(PYTHON):  # pragma: no cover - fallback for odd environments
    PYTHON = sys.executable

FULL_SUITE_TARGET = "tests/"
DEFAULT_BATCH_SIZE = 12
MEGA_HUB_FANOUT = 25

# ─── Module → targeted test mapping (SSOT: เพิ่มที่นี่ที่เดียวเมื่อมีการแมปใหม่) ───────────
MODULE_TEST_MAP: Dict[str, List[str]] = {
    # OTP relay / LINE (Gate C/D — LINE-RCA-010)
    "app/services/otp_relay_registry.py": ["tests/test_otp_multibroker_attribution.py"],
    "app/services/redaction.py": ["tests/test_redaction.py"],
    "app/services/otp_relay_readiness.py": [
        "tests/test_otp_relay_readiness.py",
        "tests/test_otp_channel_authoritative.py",
        "tests/test_daily_readiness.py",
    ],
    "app/services/line_service.py": [
        "tests/test_otp_multibroker_attribution.py",
        "tests/test_line_otp_group.py",
        "tests/test_webhook_line.py",
        "tests/test_line_flex_card.py",
        "tests/test_line_conversation_flow.py",
        "tests/test_line_async_queue.py",
        "tests/test_line_cli_ux_parity.py",
    ],
    # LINE per-user conversation state machine (2026-10-12)
    "app/services/conversation_state.py": [
        "tests/test_conversation_state.py",
        "tests/test_line_conversation_flow.py",
    ],
    # LINE slash-command CLI (TASK-line-cli, Loop 2)
    "app/services/line_cli/parser.py": [
        "tests/test_line_cli_parser.py",
        "tests/test_line_cli_trade_parser.py",
        "tests/test_line_cli_value_orders.py",
        "tests/test_line_cli_sbl.py",
    ],
    "app/services/line_cli/cards.py": [
        "tests/test_line_cli_flow.py",
        "tests/test_miniapp_phases2_5.py",
        "tests/test_line_cli_ux_parity.py",
    ],
    "app/services/line_cli/handlers.py": [
        "tests/test_line_cli_flow.py",
        "tests/test_line_cli_handlers_sbl.py",
        # Pre-trade / draft / value-order paths all live in handlers.py; without these the
        # gate passes while /short and /buy tests break (2.11c did exactly that).
        "tests/test_line_cli_order_draft.py",
        "tests/test_line_cli_value_orders.py",
        "tests/test_miniapp_phases2_5.py",
        "tests/test_line_cli_ux_parity.py",
    ],
    "app/services/line_cli/queue.py": ["tests/test_line_async_queue.py"],
    "app/services/line_cli/rich_menu.py": [
        "tests/test_line_rich_menu.py",
        "tests/test_line_cli_handlers_sbl.py",
        "tests/test_miniapp_phases2_5.py",
    ],
    "scripts/setup_rich_menu.py": [
        "tests/test_line_rich_menu.py",
        "tests/test_line_cli_handlers_sbl.py",
        "tests/test_miniapp_phases2_5.py",
    ],
    "app/services/miniapp_inbox.py": [
        "tests/test_miniapp_inbox.py",
        "tests/test_line_ecosystem_policy.py",
    ],
    "app/services/eod_summary_push.py": [
        "tests/test_eod_summary_push.py",
        "tests/test_line_ecosystem_policy.py",
    ],
    "tests/test_miniapp_inbox.py": ["tests/test_miniapp_inbox.py"],
    "tests/test_eod_summary_push.py": ["tests/test_eod_summary_push.py"],
    "tests/test_line_ecosystem_policy.py": ["tests/test_line_ecosystem_policy.py"],
    "tests/test_line_rich_menu.py": ["tests/test_line_rich_menu.py"],
    "app/services/line_cli/__init__.py": ["tests/test_line_cli_parser.py"],
    "app/brokers/krungsri/sbl_realtime_driver.py": [
        "tests/test_sbl_account_driver.py",
        "tests/test_line_cli_handlers_sbl.py",
        "tests/test_sbl_realtime_snippets.py",
    ],
    # nego-menu (ต่อราคา) — N1 market clock / N2 price planner / Auto-Nego Scheduler
    "app/services/market_clock.py": ["tests/test_market_clock.py"],
    "app/services/nego_pricer.py": ["tests/test_nego_pricer.py"],
    "app/services/nego_scheduler.py": [
        "tests/test_nego_scheduler.py",
        "tests/test_miniapp_phases2_5.py",
    ],
    "app/services/account_routing.py": [
        "tests/test_miniapp_phases2_5.py",
        "tests/test_miniapp_ux_playwright.py",
    ],
    "app/services/trading_rules.py": [
        "tests/test_validator.py",
        "tests/test_nego_pricer.py",
        "tests/test_miniapp_phases2_5.py",
    ],
    "app/services/runbook_exit_service.py": [
        "tests/test_runbook_exit_service.py",
        "tests/test_miniapp_phases2_5.py",
    ],
    "app/services/entry_ledger_service.py": [
        "tests/test_entry_ledger_service.py",
        "tests/test_suggest_entry_service.py",
    ],
    "app/services/suggest_entry_service.py": [
        "tests/test_suggest_entry_service.py",
        "tests/test_miniapp_phases2_5.py",
    ],
    "tests/test_runbook_exit_service.py": ["tests/test_runbook_exit_service.py"],
    "tests/test_entry_ledger_service.py": ["tests/test_entry_ledger_service.py"],
    "tests/test_suggest_entry_service.py": ["tests/test_suggest_entry_service.py"],
    "tests/test_nego_scheduler.py": ["tests/test_nego_scheduler.py"],
    "app/services/sbl_intelligence_service.py": [
        "tests/test_sbl_intelligence.py",
        "tests/test_line_cli_handlers_sbl.py",
    ],
    "app/api/v1/endpoints/session.py": [
        "tests/test_console_cockpit_api.py",
        "tests/test_session_connect_all_cdp.py",
    ],
    "app/mcp/pillars/pillar1_session.py": [
        "tests/test_pillar1_session.py",
        "tests/test_session_connect_all_cdp.py",
    ],
    # Pillar 2 portfolio (CLI-RCA-003 Anti-Mock balances; RCA-002/RCA-006 holdings)
    "app/mcp/pillars/pillar2_portfolio.py": ["tests/test_remote_mcp.py", "tests/test_pillar2_order_ledger.py"],
    "app/services/kdeconnect_service.py": ["tests/test_kdeconnect_service.py"],
    # Model 19 quantitative engine (Loop 3 — Card 3.1: compass + 6-setup scoring)
    "app/services/model19/__init__.py": ["tests/test_model19_compass.py"],
    "app/services/model19/compass_engine.py": ["tests/test_model19_compass.py"],
    "app/services/model19/setups.py": ["tests/test_model19_compass.py"],
    # Model 19 quantitative engine (Loop 3 — Card 3.2: 10-regime + wave thrust)
    "app/services/model19/regime_engine.py": ["tests/test_model19_regime.py"],
    "app/services/model19/wave_thrust.py": ["tests/test_model19_regime.py"],
    "app/services/action_exit_dispatcher.py": ["tests/test_krungsri_pdca_guardrails.py"],
    "app/api/v1/endpoints/webhook_line.py": [
        "tests/test_webhook_line.py",
        "tests/test_line_otp_group.py",
        "tests/test_otp_multibroker_attribution.py",
    ],
    # Broker drivers (Broker Knowledge Isolation — แยกชุดต่อโบรกเกอร์)
    "app/browser/auth_driver.py": [
        "tests/test_otp_channel_authoritative.py",
        "tests/test_otp_multibroker_attribution.py",
        "tests/test_browser_auto_reconnect.py",
        "tests/test_krungsri_session_health.py",
        "tests/test_auth_session_nondestructive.py",
    ],
    # Broker pre-market snapshot (SNAP.1)
    "app/services/broker_snapshot.py": ["tests/test_broker_snapshot.py"],
    "scripts/pre_market_snapshot.py": ["tests/test_broker_snapshot.py"],
    # Settrade Streaming (Krungsri SSO) — TASK-loop1-8 Phase 1
    "app/browser/streaming_driver.py": ["tests/test_streaming_driver.py"],
    "app/browser/streaming_auth_driver.py": ["tests/test_streaming_driver.py"],
    "app/browser/session_sentinel.py": [
        "tests/test_session_drop_detection.py",
        "tests/test_browser_driver.py",
        "tests/test_browser_auto_reconnect.py",
    ],
    "app/brokers/streaming/plugin.py": ["tests/test_streaming_driver.py"],
    "app/brokers/liberator/driver.py": [
        "tests/test_liberator_session.py",
        "tests/test_liberator_otp_channel_gate.py",
        "tests/test_otp_multibroker_attribution.py",
        "tests/test_otp_channel_authoritative.py",
        "tests/test_liberator_change_order.py",
    ],
    "app/brokers/liberator/plugin.py": [
        "tests/test_liberator_change_order.py",
        "tests/test_pillar2_portfolio_liberator.py",
    ],
    "app/brokers/liberator/cli.py": ["tests/test_liberator_change_order.py"],
    # Contracts / infra
    "app/schemas/auth.py": ["tests/test_type_safe_auth.py"],
    "app/db/session.py": ["tests/test_db.py"],
    "app/db/models.py": ["tests/test_db.py", "tests/test_db_multi_account.py"],
    "scripts/migrate_multi_account.py": ["tests/test_db_multi_account.py"],
    # Config + admin console control (/console redesign 2026-10-10)
    "app/core/config.py": [
        "tests/test_admin_console.py",
        "tests/test_console_cockpit_api.py",
        "tests/test_miniapp_phase1.py",
        "tests/test_miniapp_phases2_5.py",
    ],
    "app/core/feature_matrix.py": ["tests/test_admin_console.py"],
    "app/api/v1/api.py": [
        "tests/test_admin_console.py",
        "tests/test_console_cockpit_api.py",
        "tests/test_miniapp_phase1.py",
    ],
    "app/api/v1/endpoints/admin.py": ["tests/test_admin_console.py"],
    "app/main.py": [
        "tests/test_console_cockpit_api.py",
        "tests/test_miniapp_phase1.py",
    ],
    "app/console.py": ["tests/test_console_cockpit_api.py"],
    # LINE MiniApp (LIFF) Mobile-First WebApp (Phase 1+)
    "app/api/v1/endpoints/miniapp.py": [
        "tests/test_miniapp_phase1.py",
        "tests/test_miniapp_phases2_5.py",
        "tests/test_seamless_login_otp.py",
    ],
    "data/bookmarklets/sbl_return.js": [
        "tests/test_sbl_close_date.py",
        "tests/test_sbl_realtime_snippets.py",
    ],
    "app/miniapp/static/index.html": [
        "tests/test_miniapp_phases2_5.py",
        "tests/test_miniapp_ux_playwright.py",
    ],
    "app/miniapp/static/css/app.css": [
        "tests/test_miniapp_phases2_5.py",
        "tests/test_miniapp_ux_playwright.py",
    ],
    "app/miniapp/static/js/app.js": [
        "tests/test_miniapp_phases2_5.py",
        "tests/test_miniapp_ux_playwright.py",
    ],
    "app/miniapp/static/js/otp_widget.js": [
        "tests/test_miniapp_phases2_5.py",
        "tests/test_miniapp_ux_playwright.py",
    ],
    "app/miniapp/static/js/utils.js": [
        "tests/test_miniapp_phases2_5.py",
        "tests/test_miniapp_ux_playwright.py",
    ],
    "app/miniapp/static/js/api.js": [
        "tests/test_miniapp_phases2_5.py",
        "tests/test_miniapp_ux_playwright.py",
    ],
    "app/miniapp/static/js/auth.js": [
        "tests/test_miniapp_phases2_5.py",
        "tests/test_miniapp_ux_playwright.py",
    ],
    "app/miniapp/static/js/views/portfolio.js": [
        "tests/test_miniapp_phases2_5.py",
        "tests/test_miniapp_ux_playwright.py",
    ],
    "app/miniapp/static/js/views/orders.js": [
        "tests/test_miniapp_phases2_5.py",
        "tests/test_miniapp_ux_playwright.py",
    ],
    "app/miniapp/static/js/views/nego.js": [
        "tests/test_miniapp_phases2_5.py",
        "tests/test_miniapp_ux_playwright.py",
    ],
    "app/miniapp/static/js/views/trade.js": [
        "tests/test_miniapp_phases2_5.py",
        "tests/test_miniapp_ux_playwright.py",
    ],
    "app/miniapp/static/js/views/brokers.js": [
        "tests/test_miniapp_phases2_5.py",
        "tests/test_miniapp_ux_playwright.py",
    ],
    "app/miniapp/__init__.py": ["tests/test_miniapp_phase1.py"],
    "app/miniapp/router.py": [
        "tests/test_miniapp_phase1.py",
        "tests/test_miniapp_phases2_5.py",
        "tests/test_miniapp_ux_playwright.py",
    ],
    "app/services/broker_cdp/trading_facade.py": [
        "tests/test_miniapp_phases2_5.py",
        "tests/test_suggest_entry_service.py",
    ],
    "scripts/broker_cdp/js_runner.py": [
        "tests/test_suggest_entry_service.py",
        "tests/test_miniapp_phases2_5.py",
    ],
    "tests/test_miniapp_phase1.py": ["tests/test_miniapp_phase1.py"],
    "tests/test_miniapp_phases2_5.py": ["tests/test_miniapp_phases2_5.py"],
    "tests/test_miniapp_ux_playwright.py": ["tests/test_miniapp_ux_playwright.py"],
    "tests/test_line_cli_ux_parity.py": ["tests/test_line_cli_ux_parity.py"],
    # Validator SSOT + Krungsri price negotiation (strict SET Uptick, KSS-RCA 2026-10-09)
    "app/services/validator.py": [
        "tests/test_validator.py",
        "tests/test_trade_negotiation.py",
    ],
    "app/services/price_negotiation_service.py": [
        "tests/test_trade_negotiation.py",
        "tests/test_tpsl_overview_negotiate_api.py",
    ],
    "app/browser/trademan_driver.py": [
        "tests/test_trademan_view_order_scraper.py",
        "tests/test_pillar2_order_ledger.py",
        "tests/test_trademan_amend_bypass_dryrun.py",
        "tests/test_sbl_account_driver.py",
        "tests/test_trademan_portfolio_scraper.py",
    ],
    "app/brokers/krungsri/plugin.py": ["tests/test_broker_registry.py"],
    # Pillar 6 — Krungsri Quick Trade passive price negotiation (2026-10-14)
    "app/brokers/krungsri/quicktrade_driver.py": ["tests/test_quicktrade_driver.py"],
    "app/services/quicktrade_negotiation_service.py": ["tests/test_quicktrade_negotiation_policy.py"],
    "app/api/v1/quicktrade.py": ["tests/test_quicktrade_negotiation_policy.py"],
    "app/core/events.py": [
        "tests/test_otp_multibroker_attribution.py",
        "tests/test_line_otp_group.py",
    ],
    # Ops scripts
    "scripts/verify_changes.py": ["tests/test_verify_changes.py"],
    "scripts/mcp/hermes_kanban_mcp.py": ["tests/test_hermes_kanban_mcp.py"],
    "scripts/daily_readiness.py": ["tests/test_daily_readiness.py"],
    "scripts/services_status.py": ["tests/test_services_status.py"],
    "scripts/preflight_check.py": ["tests/test_preflight_check.py"],
    "scripts/preflight_card.py": ["tests/test_preflight_card.py"],
    "scripts/start_homelab.py": [
        "tests/test_start_homelab_stages.py",
        "tests/test_start_homelab_transport.py",
        "tests/test_start_homelab_resolve_plan.py",
    ],
    "scripts/dev_resume_session.py": ["tests/test_dev_resume_session.py"],
    "scripts/hooks/token_guard.py": ["tests/test_token_guard.py"],
    "scripts/legacy/jules_bridge.py": ["tests/test_jules_bridge.py", "tests/test_jules_api.py"],
    "scripts/krungsri/probe_account_info.py": ["tests/test_probe_account_info.py"],
    "scripts/krungsri/probe_krungsri_products.py": ["tests/test_probe_krungsri_products.py"],
    "scripts/broker_probe.py": ["tests/test_kss_probe.py", "tests/test_liberator_probe.py"],
    "scripts/broker_cdp/client.py": ["tests/test_kss_probe.py", "tests/test_liberator_probe.py"],
    "scripts/broker_cdp/brokers.py": [
        "tests/test_kss_probe.py",
        "tests/test_liberator_probe.py",
        "tests/test_cdp_session_probe.py",
        "tests/test_seamless_login_otp.py",
    ],
    "scripts/broker_cdp/otp_relay.py": ["tests/test_cdp_otp_relay.py"],
    "scripts/broker_cdp/session_daemon.py": [
        "tests/test_kss_probe.py",
        "tests/test_liberator_probe.py",
        "tests/test_seamless_login_otp.py",
    ],
    # B.2/B.3/B.7 — CDP gateway: tab manager + portfolio auto-sync + manual-order sync
    "app/services/broker_cdp/manager.py": ["tests/test_broker_cdp_routing.py"],
    "app/services/broker_cdp/session_probe.py": ["tests/test_cdp_session_probe.py"],
    "app/services/broker_cdp/login_progress.py": ["tests/test_seamless_login_otp.py"],
    "app/services/broker_cdp/daemon_launcher.py": [
        "tests/test_daemon_launcher.py",
        "tests/test_seamless_login_otp.py",
    ],
    "tests/test_seamless_login_otp.py": ["tests/test_seamless_login_otp.py"],
    "app/services/broker_cdp/__init__.py": ["tests/test_broker_cdp_routing.py", "tests/test_daemon_launcher.py"],
    "app/services/broker_cdp/sync_service.py": [
        "tests/test_broker_cdp_sync.py",
        "tests/test_broker_cdp_order_sync.py",
    ],
    # B.6 — LIVE order executor (DRY_RUN guard / Anti-Mock order no / SBL + uptick gates)
    "app/services/broker_cdp/order_executor.py": ["tests/test_broker_cdp_order_executor.py"],
    "scripts/broker_cdp/sync_probe.py": ["tests/test_broker_cdp_sync.py"],
    "scripts/broker_cdp/order_sync_probe.py": ["tests/test_broker_cdp_order_sync.py"],
    "tests/test_daemon_launcher.py": ["tests/test_daemon_launcher.py"],
    "tests/test_kss_probe.py": ["tests/test_kss_probe.py"],
    "tests/test_liberator_probe.py": ["tests/test_liberator_probe.py"],
    # Shared test harness — conftest.py holds fixtures (no tests of its own). Changed fixtures
    # affect the annealing/runbook suites (F-11 write-isolation), so map it to them explicitly.
    "tests/conftest.py": [
        "tests/test_annealing_runbook_isolation.py",
        "tests/test_escalation_and_annealing.py",
        "tests/test_click2win_driver.py",
        "tests/test_mcp_and_workflow_engine.py",
    ],
}

DOC_SUFFIXES = (".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".env")


def _rel(path: str) -> str:
    """Normalize an absolute or relative path to a repo-relative POSIX path."""
    abs_path = os.path.abspath(path if os.path.isabs(path) else os.path.join(os.getcwd(), path))
    try:
        return os.path.relpath(abs_path, REPO_ROOT).replace(os.sep, "/")
    except ValueError:  # pragma: no cover - different drive on Windows
        return path.replace(os.sep, "/")


def _pytest_already_running() -> List[str]:
    """Single-writer guard: list other live pytest processes (SQLite DB is not concurrent-safe)."""
    found: List[str] = []
    me = os.getpid()
    try:
        pids = [n for n in os.listdir("/proc") if n.isdigit()]
    except OSError:  # pragma: no cover - non-Linux
        return found
    for name in pids:
        pid = int(name)
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().decode(errors="replace").replace("\x00", " ").strip()
        except OSError:
            continue
        if "pytest" in cmd and "verify_changes.py" not in cmd:
            found.append(f"pid {pid}: {cmd[:120]}")
    return found


def _py_files(changed: Sequence[str]) -> List[str]:
    """Repo-relative .py files among the changed paths that actually exist."""
    return [
        f for f in (_rel(c) for c in changed)
        if f.endswith(".py") and os.path.exists(os.path.join(REPO_ROOT, f))
    ]


def _guard_blocked() -> bool:
    """True (and prints the offenders) when another pytest is live — SQLite test DB is single-writer."""
    running = _pytest_already_running()
    if not running:
        return False
    print(f"{YELLOW}{BOLD}BLOCKED{RESET} — pytest อื่นกำลังรันอยู่ (test_homelab.db ห้าม concurrent):")
    for line in running:
        print(f"  {line}")
    return True



def resolve_tests(changed: Sequence[str], explicit: Sequence[str]) -> Tuple[List[str], List[str], List[str]]:
    """Map changed files → targeted tests. Returns (tests, unmapped, report_only)."""
    if explicit:
        return list(explicit), [], []

    tests: List[str] = []
    unmapped: List[str] = []
    report_only: List[str] = []

    def add(target: str) -> None:
        if target not in tests:
            tests.append(target)

    for raw in changed:
        rel = _rel(raw)
        if rel.startswith(FULL_SUITE_TARGET) and rel.endswith(".py") and not rel.endswith("conftest.py"):
            add(rel)  # editing a test → run that test (conftest.py holds fixtures → MODULE_TEST_MAP)
        elif rel in MODULE_TEST_MAP:
            for target in MODULE_TEST_MAP[rel]:
                add(target)
        elif rel.endswith(DOC_SUFFIXES):
            report_only.append(rel)  # docs: Zero-Contradiction review only (no test)
        elif rel.endswith(".py"):
            unmapped.append(rel)  # NEVER guess — Anti-Mock
        else:
            report_only.append(rel)

    return tests, unmapped, report_only


def _path_to_module(rel_path: str) -> Optional[str]:
    """Convert repo-relative Python path to dotted module name (None if non-.py)."""
    if not rel_path.endswith(".py"):
        return None
    parts = list(rel_path[:-3].split("/"))
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(p for p in parts if p)


def _extract_imports(abs_path: str, rel_path: str) -> Set[str]:
    """Parse AST imports from one file (best-effort, suppressing SyntaxWarning)."""
    try:
        with open(abs_path, "r", encoding="utf-8", errors="ignore") as fh:
            source = fh.read()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(source, filename=rel_path)
    except (OSError, SyntaxError):
        return set()

    mods: Set[str] = set()
    pkg_parts = rel_path[:-3].split("/")[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level > 0:
                up_count = max(0, len(pkg_parts) - (node.level - 1))
                base_parts = [*pkg_parts[:up_count], *(base.split(".") if base else [])]
                base = ".".join(p for p in base_parts if p)
            if base:
                mods.add(base)
                for alias in node.names:
                    mods.add(f"{base}.{alias.name}")
    return mods


def _scan_reverse_imports() -> Dict[str, Set[str]]:
    """Scan app/, scripts/ (excluding legacy), and tests/test_*.py for reverse imports."""
    rev: Dict[str, Set[str]] = {}
    for top in ("app", "scripts", "tests"):
        base_dir = os.path.join(REPO_ROOT, top)
        if not os.path.isdir(base_dir):
            continue
        for cur_root, dirnames, filenames in os.walk(base_dir):
            dirnames[:] = [
                d for d in sorted(dirnames)
                if d != "__pycache__" and not (top == "scripts" and d == "legacy")
            ]
            for fname in sorted(filenames):
                if not fname.endswith(".py"):
                    continue
                if top == "tests" and not fname.startswith("test_"):
                    continue
                abs_path = os.path.join(cur_root, fname)
                rel = os.path.relpath(abs_path, REPO_ROOT).replace(os.sep, "/")
                for imp in _extract_imports(abs_path, rel):
                    rev.setdefault(imp, set()).add(rel)
    return rev


def resolve_impacted_tests(
    changed: Sequence[str],
    explicit: Sequence[str],
    *,
    max_hub_fanout: int = MEGA_HUB_FANOUT,
) -> Tuple[List[str], List[str], List[str]]:
    """Resolve MODULE_TEST_MAP + 2-hop AST reverse-import test files (strict Anti-Mock if 0)."""
    if explicit:
        return list(explicit), [], []

    tests: List[str] = []
    report_only: List[str] = []
    unmapped: List[str] = []
    seen_tests: Set[str] = set()

    def add_test(target: str) -> None:
        if target not in seen_tests:
            seen_tests.add(target)
            tests.append(target)

    rev = _scan_reverse_imports()

    for raw in changed:
        rel = _rel(raw)
        if rel.startswith(FULL_SUITE_TARGET) and rel.endswith(".py") and not rel.endswith("conftest.py"):
            add_test(rel)
            continue
        if rel.endswith(DOC_SUFFIXES):
            report_only.append(rel)
            continue
        if not rel.endswith(".py"):
            report_only.append(rel)
            continue

        matched_before = len(tests)
        for target in MODULE_TEST_MAP.get(rel, []):
            add_test(target)

        mod = _path_to_module(rel)
        if mod:
            importers = rev.get(mod, set())
            app_callers = sorted(p for p in importers if not p.startswith(FULL_SUITE_TARGET))
            for test_file in sorted(p for p in importers if p.startswith(FULL_SUITE_TARGET)):
                add_test(test_file)
            # Only expand 2-hop through non-hub modules (mega-hubs like config.py hit ~100 tests)
            if len(app_callers) <= max_hub_fanout:
                for caller_rel in app_callers:
                    caller_mod = _path_to_module(caller_rel)
                    if not caller_mod:
                        continue
                    for test_file in sorted(
                        p for p in rev.get(caller_mod, set()) if p.startswith(FULL_SUITE_TARGET)
                    ):
                        add_test(test_file)

        if len(tests) == matched_before and rel not in MODULE_TEST_MAP:
            unmapped.append(rel)

    return tests, unmapped, report_only


def compile_changed(changed: Sequence[str]) -> Tuple[bool, str]:
    """Syntax gate on the .py files touched in this unit (fast, deterministic)."""
    files = _py_files(changed)
    if not files:
        return True, "compileall: no .py files to check"
    proc = subprocess.run(
        [PYTHON, "-m", "compileall", "-q", *files],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    ok = proc.returncode == 0
    detail = "compileall: OK" if ok else "compileall: FAILED\n" + proc.stdout + proc.stderr
    return ok, detail


def clean_test_db() -> None:
    """P4 DB hygiene: remove any leftover test_homelab.db* (including -wal, -shm)."""
    import glob
    for p in glob.glob(os.path.join(REPO_ROOT, "test_homelab.db*")):
        try:
            os.remove(p)
        except OSError:
            pass


def detect_changed_symbols(paths: Sequence[str]) -> List[str]:
    """P3 fast mode: extract modified function/class names from git diff hunk headers."""
    import re
    files = _py_files(paths)
    if not files:
        return []
    symbols: set = set()
    for diff_cmd in (
        ["git", "diff", "-U0", "HEAD", "--", *files],
        ["git", "diff", "-U0", "--", *files],
    ):
        try:
            proc = subprocess.run(diff_cmd, cwd=REPO_ROOT, capture_output=True, text=True)
            for line in proc.stdout.splitlines():
                if line.startswith("@@"):
                    m = re.search(r"@@.*@@\s*(?:async\s+)?(?:def|class)\s+([a-zA-Z0-9_]+)", line)
                    if m:
                        sym = m.group(1).lstrip("_")
                        if sym and len(sym) >= 3:
                            symbols.add(sym)
            if symbols:
                break
        except Exception:
            pass
    return sorted(symbols)


def _collect_full_test_files() -> List[str]:
    """Return sorted repo-relative test_*.py files under tests/."""
    found: List[str] = []
    tests_dir = os.path.join(REPO_ROOT, "tests")
    if not os.path.isdir(tests_dir):
        return found
    for cur_root, dirnames, filenames in os.walk(tests_dir):
        dirnames[:] = [d for d in sorted(dirnames) if d != "__pycache__"]
        for fname in sorted(filenames):
            if fname.startswith("test_") and fname.endswith(".py"):
                abs_path = os.path.join(cur_root, fname)
                found.append(os.path.relpath(abs_path, REPO_ROOT).replace(os.sep, "/"))
    return sorted(found)


def run_pytest(targets: Sequence[str], label: str, extra_args: Optional[Sequence[str]] = None) -> int:
    clean_test_db()
    cmd = [PYTHON, "-m", "pytest", *targets, "-q"]
    if extra_args:
        cmd.extend(extra_args)
    display_args = f" {' '.join(extra_args)}" if extra_args else ""
    print(f"{BLUE}▶{RESET} {label}: {' '.join(targets)}{display_args}")
    started = time.time()
    proc = subprocess.run(cmd, cwd=REPO_ROOT)
    elapsed = time.time() - started
    clean_test_db()
    if proc.returncode == 0:
        print(f"{GREEN}{BOLD}PASS{RESET} ({label}) in {elapsed:.1f}s")
    else:
        print(f"{RED}{BOLD}FAIL{RESET} ({label}, exit {proc.returncode}) in {elapsed:.1f}s")
    return proc.returncode


def run_batched_pytest(
    targets: Sequence[str],
    label: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    extra_args: Optional[Sequence[str]] = None,
) -> int:
    """Run pytest across isolated batches to avoid cross-file SQLite/asyncio deadlocks."""
    batch_sz = max(1, batch_size)
    has_k_filter = bool(extra_args and "-k" in extra_args)

    if len(targets) <= batch_sz:
        rc = run_pytest(targets, label, extra_args) if extra_args else run_pytest(targets, label)
        return rc

    batches = [list(targets[i : i + batch_sz]) for i in range(0, len(targets), batch_sz)]
    total = len(batches)
    failed_batches: List[Tuple[int, List[str], int]] = []
    collected_any = False
    started = time.time()
    for idx, batch in enumerate(batches, 1):
        batch_label = f"{label}[{idx}/{total}]"
        rc = run_pytest(batch, batch_label, extra_args) if extra_args else run_pytest(batch, batch_label)
        if has_k_filter and rc == 5:
            continue
        collected_any = True
        if rc != 0:
            failed_batches.append((idx, batch, rc))
    elapsed = time.time() - started
    if has_k_filter and not collected_any:
        return 5
    if not failed_batches:
        print(f"{GREEN}{BOLD}PASS{RESET} ({label}, {total} batches / {len(targets)} files) in {elapsed:.1f}s")
        return 0
    print(f"{RED}{BOLD}FAIL{RESET} ({label}, {len(failed_batches)}/{total} batches failed) in {elapsed:.1f}s")
    for idx, batch, rc in failed_batches:
        print(f"  {RED}✗{RESET} batch {idx} (exit {rc}): {' '.join(batch)}")
    return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Targeted verify hook for one Loop-Engineering unit (compileall + mapped/impacted tests)."
    )
    parser.add_argument("paths", nargs="*", help="ไฟล์ที่แก้ใน unit นี้ (repo-relative หรือ absolute)")
    parser.add_argument("--tests", nargs="+", default=[], help="ระบุ test target เอง (ข้าม mapping)")
    parser.add_argument("--impacted", action="store_true", help="task close: MODULE_TEST_MAP + 2-hop AST reverse imports")
    parser.add_argument("--full", action="store_true", help="batched full suite — ใช้ปลายวันใน daily_close.sh")
    parser.add_argument("--fast", action="store_true", help="fast mode — รันเฉพาะ test ที่ตรงกับ symbol ที่แก้ (<=15s)")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help="จำนวนไฟล์สูงสุดต่อ pytest process")
    parser.add_argument("--exclude-browser", action="store_true", help="ข้าม @pytest.mark.browser (-m 'not browser')")
    parser.add_argument("--dry-run", action="store_true", help="แสดงแผนแล้วจบ (ไม่รัน test)")
    args = parser.parse_args(argv)

    if not args.paths and not args.tests and not args.full:
        parser.error("ต้องระบุไฟล์ที่แก้อย่างน้อย 1 ไฟล์ หรือใช้ --tests/--full")

    base_extra_args: List[str] = []
    if args.exclude_browser:
        base_extra_args.extend(["-m", "not browser"])

    if args.full:
        full_files = _collect_full_test_files()
        if args.dry_run:
            suffix = f" {' '.join(base_extra_args)}" if base_extra_args else ""
            print(f"[dry-run] full suite → {len(full_files)} files (batch-size={max(1, args.batch_size)}){suffix}")
            return 0
        if _guard_blocked():
            return 2
        print(f"{BOLD}Full suite (batched){RESET} — {len(full_files)} file(s), batch-size={max(1, args.batch_size)}")
        return run_batched_pytest(
            full_files,
            "full",
            batch_size=args.batch_size,
            extra_args=base_extra_args or None,
        )

    resolver = resolve_impacted_tests if args.impacted else resolve_tests
    tests, unmapped, report_only = resolver(args.paths, args.tests)
    base_label = "impacted" if args.impacted else "targeted"
    mode_label = base_label
    run_extra_args = list(base_extra_args)

    if args.fast and tests:
        symbols = detect_changed_symbols(args.paths)
        if symbols:
            run_extra_args.extend(["-k", " or ".join(symbols)])
            mode_label = "fast"

    print(f"{BOLD}Unit verify ({mode_label}){RESET} — {len(args.paths)} changed file(s)")
    for rel in report_only:
        print(f"  {BLUE}·{RESET} {rel}  →  report-only (Zero-Contradiction review, ไม่มี test)")
    for rel in unmapped:
        print(f"  {RED}✗{RESET} {rel}  →  {RED}UNMAPPED{RESET} (เพิ่มใน MODULE_TEST_MAP หรือใช้ --tests)")

    if args.dry_run:
        for target in tests or ["(none)"]:
            print(f"  {BLUE}→{RESET} would run: {target}")
        if run_extra_args:
            print(f"  {BLUE}·{RESET} pytest args: {' '.join(run_extra_args)}")
        print(f"  {BLUE}·{RESET} compileall: {' '.join(sorted(_py_files(args.paths))) or '(none)'}")
        return 2 if unmapped else 0

    ok, detail = compile_changed(args.paths)
    print(f"  {BLUE}·{RESET} {detail}")
    if not ok:
        return 1
    if unmapped:
        print(f"{RED}{BOLD}UNMAPPED{RESET} — ไม่รัน test (Anti-Mock: ห้ามเดา)")
        return 2
    if not tests:
        print(f"{YELLOW}{BOLD}NO TESTS{RESET} — docs/อย่างเดียว → compileall ผ่าน = จบ unit")
        return 0

    if _guard_blocked():
        return 2

    rc = run_batched_pytest(
        tests,
        mode_label,
        batch_size=args.batch_size,
        extra_args=run_extra_args or None,
    )
    if mode_label == "fast" and rc == 5:
        print(f"{YELLOW}↺{RESET} fast filter (-k) ไม่ตรงชื่อ test ใด → fallback รัน {base_label} เต็มชุด")
        return run_batched_pytest(
            tests,
            base_label,
            batch_size=args.batch_size,
            extra_args=base_extra_args or None,
        )
    return rc


if __name__ == "__main__":
    sys.exit(main())
