import logging
import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import settings
from app.db.init_db import init_db
from app.api.v1.api import api_router

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("homelab")


# Hide high-frequency /console polling requests from the uvicorn access log
# (errors and all other requests are still logged).
_QUIET_POLL_PATHS = (
    "/api/v1/orders/hitl/pending",
    "/api/v1/session/health",
    "/api/v1/agentic/kill-switch",
)


class _PollingAccessFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 5:
            path, status = str(args[2]), args[4]
            if path.startswith(_QUIET_POLL_PATHS) and isinstance(status, int) and status < 400:
                return False
        return True


logging.getLogger("uvicorn.access").addFilter(_PollingAccessFilter())


from app.browser.session_sentinel import session_sentinel
from app.browser.auth_driver import auth_driver
from app.services.phase3_scheduler import phase3_scheduler
from app.services.nego_scheduler import nego_scheduler
from app.services.runbook_exit_service import runbook_exit_service
from app.services.eod_summary_push import eod_summary_service
from app.services.action_exit_dispatcher import action_exit_dispatcher
from app.ai.orchestrator import master_orchestrator


from app.services.line_cli.queue import async_command_queue


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifespan event handler: session monitor, Phase 3 scheduler, and background resources (Zero-DB)."""
    logger.info("Starting Investment Homelab Gateway (Zero-DB Pure CDP Architecture)...")
    # Zero-DB Phase 4 (t_bbb1b2d6): Gateway runs pure CDP without SQLite database initialization
    # await init_db()
    logger.info("Zero-DB Gateway ready to receive webhooks.")
    if getattr(settings, "ENABLE_LEGACY_SESSION_SENTINEL", False):
        await session_sentinel.start()
    else:
        logger.info("Legacy TradeMan SessionSentinel is DISABLED (Pure CDP mode).")
    async_command_queue.start()
    
    # Start Tactical Action Exit Autonomous Dispatcher & 09:45 Pre-Flight Daemon
    # Slim-gateway: defaults OFF (ENABLE_ACTION_EXIT_DISPATCHER=False) so an OTP/auth-only
    # boot does not spawn the Pillar-2 order-exit daemon. Enable when testing exits.
    if settings.ENABLE_ACTION_EXIT_DISPATCHER:
        action_exit_dispatcher.start()
        logger.info("Tactical Action Exit Autonomous Dispatcher started.")
    else:
        logger.info("Action-Exit Dispatcher is DISABLED (ENABLE_ACTION_EXIT_DISPATCHER=False).")

    # Start Autonomous PT-MRO Master Orchestrator Daemon
    if settings.ENABLE_AGENTIC_ORCHESTRATOR:
        master_orchestrator.start_background_loop()
        logger.info("Autonomous MasterOrchestrator PT-MRO background loop started.")
    else:
        logger.info("MasterOrchestrator is DISABLED (ENABLE_AGENTIC_ORCHESTRATOR=False).")

    if settings.ENABLE_INTRADAY_SCHEDULER:
        logger.info("Starting Intraday Scheduler (8 Market Regimes)...")
        await phase3_scheduler.start()
    else:
        logger.info("Intraday Scheduler is DISABLED (ENABLE_INTRADAY_SCHEDULER=False). Running in Isolated/Debug mode.")

    if getattr(settings, "ENABLE_NEGO_SCHEDULER", True):
        await nego_scheduler.start()
    else:
        logger.info("Auto-Nego Scheduler is DISABLED (ENABLE_NEGO_SCHEDULER=False).")

    if getattr(settings, "ENABLE_RUNBOOK_EXIT_SCHEDULER", True):
        await runbook_exit_service.start()
    else:
        logger.info("Runbook Auto-Exit Scheduler is DISABLED (ENABLE_RUNBOOK_EXIT_SCHEDULER=False).")

    if getattr(settings, "LINE_EOD_PUSH_ENABLED", True):
        await eod_summary_service.start()

    # Legacy proactive startup LINE push runs ONLY when LINE_PUSH_MODE == "all".
    # In default "eod_only" mode, log startup once to MiniApp Inbox with zero LINE quota spent.
    async def _startup_status_notice():
        try:
            await asyncio.sleep(1)
            mode = (getattr(settings, "LINE_PUSH_MODE", "eod_only") or "eod_only").strip().lower()
            if mode != "all":
                from app.services.miniapp_inbox import miniapp_inbox

                quota = await miniapp_inbox.get_line_quota_status()
                await miniapp_inbox.publish(
                    category="system",
                    severity="info",
                    title="🚀 InvestmentHomelab Gateway ออนไลน์",
                    body=(
                        f"โหมดแจ้งเตือน: {mode} · EOD {getattr(settings, 'LINE_EOD_PUSH_TIME', '17:05')} (MiniApp) · "
                        f"LINE Quota {quota.get('used') if quota.get('used') is not None else '?'}/{quota.get('limit') or 300}"
                    ),
                    action_tab="brokers",
                    line_pushed=False,
                    meta={"dedup_key": "gateway_online", "is_heartbeat": True},
                )
                return

            from app.services.line_service import line_service
            if getattr(line_service, "_push_quota_exhausted", False):
                logger.info("Startup broker status push skipped (LINE monthly push quota exhausted; replyToken mode active).")
                return
            from app.services.line_cli.handlers import _handle_status
            res = await _handle_status(None, None)
            card = res.get("card")
            if card:
                pushed = await line_service.push_message(card)
                if pushed:
                    logger.info("Proactive startup broker status pushed to LINE successfully.")
                else:
                    logger.info("Proactive startup broker status push skipped (LINE push returned non-200 / quota limit).")
        except Exception as e:
            logger.warning(f"Startup status notice failed: {e}")

    asyncio.create_task(_startup_status_notice())

    # Note: /console launch is strictly managed by scripts/start_homelab.py after its readiness stages pass
    # (Stage 1 OTP relay (LINE group) + Stage 2 Krungsri login gates; Stage 3 Liberator is optional).

    yield
    logger.info("Shutting down Investment Homelab Gateway...")
    await async_command_queue.stop()
    if settings.ENABLE_AGENTIC_ORCHESTRATOR:
        await master_orchestrator.stop_background_loop()
    if settings.ENABLE_ACTION_EXIT_DISPATCHER:
        await action_exit_dispatcher.stop()
    if settings.ENABLE_INTRADAY_SCHEDULER:
        await phase3_scheduler.stop()
    if getattr(settings, "ENABLE_NEGO_SCHEDULER", True):
        await nego_scheduler.stop()
    if getattr(settings, "ENABLE_RUNBOOK_EXIT_SCHEDULER", True):
        await runbook_exit_service.stop()
    if getattr(settings, "LINE_EOD_PUSH_ENABLED", True):
        await eod_summary_service.stop()
    if getattr(settings, "ENABLE_LEGACY_SESSION_SENTINEL", False):
        await session_sentinel.stop()
    await auth_driver.close()


app = FastAPI(
    title=settings.PROJECT_NAME,
    version="1.0.0",
    description="Semi-Automated Trading & SBL Lifecycle Homelab Gateway",
    lifespan=lifespan
)

# Enable CORS for local testing and future Web Dashboard (Phase 5)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from fastapi.responses import HTMLResponse
from app.console import CONSOLE_HTML

# Include API Router
app.include_router(api_router, prefix=settings.API_V1_STR)


@app.get("/", response_class=HTMLResponse)
@app.get("/console", response_class=HTMLResponse)
async def test_console():
    """Interactive Phase 1 Test Console with 1-click test actions."""
    return HTMLResponse(content=CONSOLE_HTML)


@app.get("/api")
async def api_info():
    return {
        "app": settings.PROJECT_NAME,
        "status": "online",
        "console": "/console",
        "docs": "/docs",
        "api_v1": settings.API_V1_STR
    }


@app.get("/health")
async def root_health():
    return {"status": "healthy"}


# ------------------------------------------------------------------------------
# Static Screenshots & Remote MCP Server (SSE Transport)
# ------------------------------------------------------------------------------
import os
from fastapi.staticfiles import StaticFiles
from app.mcp.server import get_modular_sse_app

# 1. Mount Static Screenshots Directory for Tailscale HTTP Access
SCREENSHOTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "screenshots")
os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
app.mount("/screenshots", StaticFiles(directory=SCREENSHOTS_DIR), name="screenshots")

# 2. Mount LINE MiniApp (LIFF) Web Shell & Static Assets (must precede root "/" MCP mount)
from app.miniapp.router import NoCacheStaticFiles, router as miniapp_web_router, STATIC_DIR as MINIAPP_STATIC_DIR
app.mount("/miniapp/static", NoCacheStaticFiles(directory=str(MINIAPP_STATIC_DIR)), name="miniapp-static")
app.include_router(miniapp_web_router)

# 3. Mount Next-Gen 6-Pillars Modular MCP Server at root (/sse and /messages/)
app.mount("/", get_modular_sse_app())
