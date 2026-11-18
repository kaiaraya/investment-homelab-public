import hashlib
import os
import shutil
from pathlib import Path
from typing import Dict

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.main import app as fastapi_app
from app.db.models import Base
from app.db.session import get_db
from app.core.config import settings

TEST_DB_PATH = "./test_homelab.db"
TEST_DATABASE_URL = f"sqlite+aiosqlite:///{TEST_DB_PATH}"

#: Tracked runbook tree — tests must never write here (F-11 / annealing test pollution).
REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_LEARNED_WORKFLOWS = REPO_ROOT / "learned_workflows"


def snapshot_runbook_hashes(root: Path = REPO_LEARNED_WORKFLOWS) -> Dict[str, str]:
    """Map of runbook path → sha256 digest, used to prove the tracked tree is untouched."""
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }

def _clean_test_db_files() -> None:
    """P4 DB hygiene: remove any leftover test_homelab.db* (including -wal, -shm)."""
    for f in Path(".").glob("test_homelab.db*"):
        try:
            f.unlink()
        except OSError:
            pass


def pytest_sessionstart(session):
    """P4 DB hygiene: clean up test database artifacts before any test executes."""
    _clean_test_db_files()


def pytest_sessionfinish(session, exitstatus):
    """P4 DB hygiene: dispose engine and clean up test database artifacts on exit."""
    try:
        import asyncio
        asyncio.run(test_engine.dispose())
    except Exception:
        pass
    _clean_test_db_files()


test_engine = create_async_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
)

TestingSessionLocal = async_sessionmaker(
    bind=test_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False
)

import app.db.session
import app.browser.trademan_driver
import app.browser.session_sentinel
import app.browser.auth_driver
import app.services.eod_service
import app.services.trade_dispatcher
import app.services.portfolio_overview_service
import app.services.trade_center_service
import app.services.price_negotiation_service
import app.services.market_monitor_service
import app.services.daily_scenario_runner
import app.services.scenario_matrix
import app.services.broker_cdp.sync_service

app.db.session.engine = test_engine
app.db.session.AsyncSessionLocal = TestingSessionLocal
app.db.session.AsyncSessionClick2Win = TestingSessionLocal
app.browser.trademan_driver.AsyncSessionLocal = TestingSessionLocal
app.browser.session_sentinel.AsyncSessionLocal = TestingSessionLocal
app.browser.auth_driver.AsyncSessionLocal = TestingSessionLocal
app.services.eod_service.AsyncSessionLocal = TestingSessionLocal
app.services.trade_dispatcher.AsyncSessionLocal = TestingSessionLocal
app.services.portfolio_overview_service.AsyncSessionLocal = TestingSessionLocal
app.services.trade_center_service.AsyncSessionLocal = TestingSessionLocal
app.services.price_negotiation_service.AsyncSessionLocal = TestingSessionLocal
app.services.market_monitor_service.AsyncSessionLocal = TestingSessionLocal
app.services.daily_scenario_runner.AsyncSessionLocal = TestingSessionLocal
app.services.scenario_matrix.AsyncSessionLocal = TestingSessionLocal
app.services.broker_cdp.sync_service.AsyncSessionLocal = TestingSessionLocal


@pytest_asyncio.fixture(autouse=True)
async def prepare_test_db():
    """Initializes tables before each test and cleans them up after."""
    from app.services.validator import validator
    from app.browser.auth_driver import auth_driver
    from app.services.action_center_service import action_center_service

    validator.clear_idempotency_cache()
    auth_driver.status = "DISCONNECTED"
    auth_driver.current_ref = None
    auth_driver.last_login_attempt = None
    auth_driver.page = None
    action_center_service.clear_all()

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    auth_driver.status = "DISCONNECTED"
    auth_driver.current_ref = None
    auth_driver.last_login_attempt = None
    auth_driver.page = None
    action_center_service.clear_all()


async def override_get_db():
    async with TestingSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


fastapi_app.dependency_overrides[get_db] = override_get_db


@pytest_asyncio.fixture
async def db_session():
    async with TestingSessionLocal() as session:
        yield session



@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest.fixture
def auth_headers():
    return {"X-Homelab-Key": settings.HOMELAB_SECRET_KEY}


@pytest.fixture(autouse=True)
def isolate_external_side_effects(monkeypatch, tmp_path):
    """
    Safety net for ALL tests:
    - LINE runs in simulation mode (no real push to the phone/chat).
    - The KDE Connect singleton never pings the real Mi 10T.
    Individual tests may still patch these objects further.
    """
    from unittest.mock import AsyncMock
    from app.services.kdeconnect_service import kdeconnect_service

    monkeypatch.setattr(settings, "LINE_SIMULATION_MODE", True)
    monkeypatch.setattr(settings, "MINIAPP_INBOX_STATE_PATH", str(tmp_path / "miniapp_inbox.json"))
    monkeypatch.setattr(settings, "LINE_EOD_STATE_PATH", str(tmp_path / "eod_summary_state.json"))
    monkeypatch.setattr(kdeconnect_service, "send_critical_alert", AsyncMock(return_value=True))
    yield


@pytest.fixture(autouse=True)
def isolate_learned_workflow_writes(monkeypatch, tmp_path):
    """
    Safety net for ALL tests (F-11 / annealing test pollution):
    ``WorkflowEngine.save_workflow_spec()`` and ``update_workflow_step()`` resolve their target
    file through the module-level ``workflow_engine.WORKFLOWS_DIR``. Redirect that constant to a
    throwaway copy of the runbook tree, so annealing tests still parse the real runbooks but every
    write lands in ``tmp_path`` instead of the git-tracked ``learned_workflows/`` directory.
    """
    from app.services import workflow_engine

    sandbox = (tmp_path / "learned_workflows").resolve()
    if sandbox.is_relative_to(REPO_LEARNED_WORKFLOWS.resolve()):  # pragma: no cover - guard rail
        raise RuntimeError(f"refusing to sandbox runbook writes inside the repo: {sandbox}")

    shutil.copytree(REPO_LEARNED_WORKFLOWS, sandbox)
    monkeypatch.setattr(workflow_engine, "WORKFLOWS_DIR", str(sandbox))
    yield


@pytest.fixture(scope="session", autouse=True)
def guard_tracked_runbooks():
    """
    Session-level leak guard (F-11): fail the session if ANY test wrote into the git-tracked
    ``learned_workflows/`` tree — even a test that bypassed the per-test sandbox above.
    """
    before = snapshot_runbook_hashes()
    yield
    after = snapshot_runbook_hashes()
    if before != after:
        leaked = sorted(set(before) ^ set(after)) + sorted(
            p for p in set(before) & set(after) if before[p] != after[p]
        )
        raise AssertionError(
            "tests wrote into the tracked runbook tree (learned_workflows/) — "
            f"offending files: {leaked}. Route writes through the "
            "isolate_learned_workflow_writes sandbox instead."
        )


@pytest.fixture(autouse=True)
def _default_kss_amend_mode(monkeypatch):
    """Keep the suite independent of the operator's .env amend mode.

    Tests that exercise the TradeMan cancel+replace path set the mode explicitly.
    """
    from app.core.config import settings
    monkeypatch.setattr(settings, "KSS_AMEND_MODE", "quicktrade")


@pytest.fixture(autouse=True)
def _reset_kss_active_account_tracker():
    """The Streaming active-account tracker is process-global (t_496c6213).

    One test recording "<REDACTED_KSS_ACCT> CASH" must not leak into the next test's /status or
    /port-anonymous assertions.
    """
    from app.services.broker_cdp import sync_service
    sync_service.reset_active_kss_account()
    yield
    sync_service.reset_active_kss_account()


@pytest.fixture(autouse=True)
def _isolate_pin_guard(monkeypatch, tmp_path):
    """Keep the PIN-attempt guard out of the repo's ``data/`` and out of other tests.

    The guard is fail-closed for real money, so a leftover failure recorded by one test
    would silently block the next one; each test gets a private state file instead.
    """
    monkeypatch.setattr(settings, "PIN_GUARD_ENABLED", True)
    monkeypatch.setattr(settings, "PIN_GUARD_MAX_FAILURES", 1)
    monkeypatch.setattr(settings, "PIN_GUARD_LOCK_MINUTES", 240)
    monkeypatch.setattr(settings, "PIN_GUARD_STATE_PATH", str(tmp_path / "pin_guard.json"))


@pytest.fixture(autouse=True)
def _never_touch_a_real_browser(monkeypatch):
    """Forbid the unit suite from reaching the live CDP daemon (port 9333/9334).

    ``order_executor`` tries the direct-API JS snippet *before* the DOM fallback, and that
    snippet POSTs a real order carrying the trading PIN. With the gateway's Chrome open,
    a plain ``pytest`` run therefore hit ``apigw.settrade.com`` for real (2026-10-29: the
    broker answered "pin is suspended"). Tests that mean to exercise the JS path patch
    ``run_snippet`` themselves and still work.
    """
    try:
        from scripts.broker_cdp import js_runner
    except Exception:                                  # scripts/ not importable — fine
        return

    async def _blocked(*_a, **_kw):
        raise RuntimeError("run_snippet is disabled in tests: never POST to a live broker")

    monkeypatch.setattr(js_runner, "run_snippet", _blocked)

    async def _blocked_scrape(*_a, **_kw):
        raise RuntimeError("scrape_portfolio_from_cdp is disabled in tests: never touch a live browser")

    try:
        from app.services.broker_cdp import sync_service
        monkeypatch.setattr(sync_service, "scrape_portfolio_from_cdp", _blocked_scrape)
    except Exception:
        pass

    try:
        from app.brokers.krungsri.sbl_realtime_driver import krungsri_sbl_driver
        monkeypatch.setattr(krungsri_sbl_driver, "scrape_sbl_lots", _blocked_scrape)
        monkeypatch.setattr(krungsri_sbl_driver, "_return_via_cdp_ui", _blocked_scrape)
    except Exception:
        pass

    async def _mock_cdp_status(broker, purpose="trade"):
        return {
            "broker": broker,
            "purpose": purpose,
            "state": "AUTHENTICATED",
            "checked_at": "2026-11-09T00:00:00Z",
            "account": "TEST_ACCT",
            "pid": 1234,
            "sbl": {"state": "AUTHENTICATED"},
        }

    try:
        from app.services.broker_cdp import session_probe
        monkeypatch.setattr(session_probe, "cdp_session_status", _mock_cdp_status)
    except Exception:
        pass

    async def _mock_order_exec(*_a, **_kw):
        return {"success": True, "status": "DRY_RUN", "broker_order_no": None}

    try:
        from app.services.broker_cdp import order_executor
        monkeypatch.setattr(order_executor, "execute_confirmed_order", _mock_order_exec)
        monkeypatch.setattr(order_executor, "execute_order_via_cdp", _mock_order_exec)
    except Exception:
        pass

    try:
        from app.services.line_cli.handlers import clear_cdp_portfolio_cache
        clear_cdp_portfolio_cache()
    except Exception:
        pass
