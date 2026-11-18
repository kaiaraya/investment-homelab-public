"""CDP-UNIFY 8 — de-scoped purge guard.

Locks in the 2026-11-04 witness finding: the operator must never again see
Click2Win / TradeMan / QuickTrade / Settrade OpenAPI presented as if they were
live apps. These tests fail the moment a de-scoped name creeps back into a
user-visible surface or a session entry point starts waking a dead driver.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.mcp.pillars.pillar1_session import session_get_health, session_login_broker
from app.services.line_cli import handlers
from app.services.line_cli.parser import parse
from app.services.otp_relay_registry import BROKER_LABELS, broker_label

ROOT = Path(__file__).resolve().parent.parent
DE_SCOPED_NAMES = ("Click2Win", "TradeMan", "QuickTrade", "SETTRADE_OPENAPI")


# --------------------------------------------------------------------------- #
# 1. Labels shown on the OTP card / console
# --------------------------------------------------------------------------- #
def test_broker_labels_speak_only_in_scope_names():
    assert BROKER_LABELS["KRUNGSRI"] == "Krungsri (Streaming)"
    assert BROKER_LABELS["TRADEMAN"] == "Krungsri (Streaming)"   # legacy tag alias
    assert BROKER_LABELS["LIBERATOR"] == "Liberator"
    assert "CLICK2WIN" not in BROKER_LABELS

    for tag in BROKER_LABELS:
        label = broker_label(tag)
        for dead in DE_SCOPED_NAMES:
            assert dead not in label, f"{tag} label still advertises {dead}"


def test_unknown_tag_is_not_dressed_up_as_a_phantom_app():
    # No mapping → raw tag, never a friendly label for an app we do not run.
    assert broker_label("CLICK2WIN") == "CLICK2WIN"


# --------------------------------------------------------------------------- #
# 2. Session entry point fails closed
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
@pytest.mark.parametrize("platform", ["CLICK2WIN", "SETTRADE_OPENAPI", "SETTRADE", "QUICKTRADE"])
async def test_de_scoped_platform_fails_closed(platform):
    res = await session_login_broker(platform)
    assert res["success"] is False
    assert res["status"] == "DE_SCOPED"
    assert res["otp_ref"] is None


@pytest.mark.asyncio
async def test_unknown_platform_is_rejected_not_mapped():
    res = await session_login_broker("CLICK2WIN_SANDBOX")
    assert res["success"] is False
    assert res["status"] == "UNKNOWN_PLATFORM"


@pytest.mark.asyncio
async def test_health_reports_only_in_scope_surfaces():
    health = await session_get_health()
    # legacy keys kept for back-compat ...
    assert "krungsri_trademan" in health
    assert "settrade" in health
    # ... but they no longer carry Click2Win / OpenAPI session state
    settrade = health["settrade"]
    assert not any(k.startswith("click2win") for k in settrade)
    assert settrade["openapi_configured"] is False
    assert settrade["streaming_via_cdp"] is True
    assert "CLICK2WIN" in settrade["de_scoped"]
    # no phantom app rows ride along in the registry payload
    assert set(health["brokers"]) <= {"krungsri", "liberator"}, health["brokers"]
    assert len(health["in_scope_apps"]) == 3


# --------------------------------------------------------------------------- #
# 3. LINE command surface
# --------------------------------------------------------------------------- #
def test_quicktrade_command_is_gone_but_halt_survives():
    assert getattr(parse("/quicktrade"), "cmd", None) in (None, "")
    assert getattr(parse("/trade"), "cmd", None) in (None, "")
    assert parse("/halt").cmd == "halt"


def test_help_text_never_advertises_de_scoped_names():
    # The operator-facing help payload (module constant), not code comments.
    for dead in ("QuickTrade", "Click2Win", "TradeMan"):
        assert dead not in handlers._HELP_TEXT, f"help text still mentions {dead}"
    for cmd in ("/quicktrade", "/trade"):
        assert cmd not in handlers._HELP_TEXT

    # No de-scoped command can still be routed either.
    source = Path(handlers.__file__).read_text(encoding="utf-8")
    assert 'cmd == "quicktrade"' not in source


# --------------------------------------------------------------------------- #
# 4. Operator-facing files
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("rel_path", ["app/console.py", "scripts/start_homelab.py"])
def test_operator_surfaces_are_clean(rel_path):
    text = (ROOT / rel_path).read_text(encoding="utf-8")
    for dead in ("Click2Win", "TradeMan", "QuickTrade"):
        assert dead not in text, f"{rel_path} still shows {dead} to the operator"


def test_pillar1_module_no_longer_imports_dead_drivers():
    text = (ROOT / "app/mcp/pillars/pillar1_session.py").read_text(encoding="utf-8")
    assert "click2win_auth_driver" not in text
    assert "settrade_openapi_plugin" not in text
