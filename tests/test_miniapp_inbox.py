"""Unit tests for MiniApp Notification Inbox & LINE Quota Poller (Zero-DB JSON + SSE)."""

from __future__ import annotations

import json
import pytest

from app.core.config import settings
from app.services.miniapp_inbox import (
    MAX_INBOX_ITEMS,
    MiniAppInboxService,
    QUOTA_CACHE_TTL_SECONDS,
)


@pytest.mark.asyncio
async def test_inbox_publish_list_mark_read_and_cap(tmp_path, monkeypatch):
    state_file = tmp_path / "inbox.json"
    monkeypatch.setattr(settings, "MINIAPP_INBOX_STATE_PATH", str(state_file))
    svc = MiniAppInboxService()

    for idx in range(MAX_INBOX_ITEMS + 12):
        await svc.publish(
            category="trade" if idx % 2 == 0 else "otp",
            severity="info",
            title=f"Event #{idx}",
            body=f"Body detail {idx}",
            broker="kss",
            action_tab="orders",
        )

    listing = await svc.list_items(limit=MAX_INBOX_ITEMS)
    assert listing["total"] == MAX_INBOX_ITEMS
    assert listing["unread_count"] == MAX_INBOX_ITEMS
    assert listing["items"][0]["title"] == f"Event #{MAX_INBOX_ITEMS + 11}"

    # Persistence check from a fresh service instance
    svc2 = MiniAppInboxService()
    listing2 = await svc2.list_items(limit=10)
    assert listing2["total"] == MAX_INBOX_ITEMS
    assert len(listing2["items"]) == 10

    first_id = listing2["items"][0]["id"]
    one = await svc2.mark_read(first_id)
    assert one["unread_count"] == MAX_INBOX_ITEMS - 1

    all_res = await svc2.mark_read(None)
    assert all_res["unread_count"] == 0
    assert (await svc2.list_items())["unread_count"] == 0


@pytest.mark.asyncio
async def test_line_quota_poller_caches_and_syncs_exhaustion(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MINIAPP_INBOX_STATE_PATH", str(tmp_path / "inbox.json"))
    monkeypatch.setattr(settings, "LINE_SIMULATION_MODE", False)
    monkeypatch.setattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "live_tok_xyz")

    from app.services.line_service import line_service

    line_service._push_quota_exhausted = False
    calls = {"count": 0, "used": 300}

    class _Resp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            calls["count"] += 1
            if url.endswith("/consumption"):
                return _Resp(200, {"totalUsage": calls["used"]})
            return _Resp(200, {"type": "limited", "value": 300})

    monkeypatch.setattr("app.services.miniapp_inbox.httpx.AsyncClient", _FakeClient)
    svc = MiniAppInboxService()

    q1 = await svc.get_line_quota_status()
    assert q1["used"] == 300
    assert q1["limit"] == 300
    assert q1["remaining"] == 0
    assert q1["exhausted"] is True
    assert q1["source"] == "line_api"
    assert line_service._push_quota_exhausted is True
    assert calls["count"] == 2

    # Within 60s TTL -> uses cache cleanly
    calls["used"] = 42
    q2 = await svc.get_line_quota_status()
    assert calls["count"] == 2
    assert q2["used"] == 300

    # force_refresh -> fetches updated usage and clears _push_quota_exhausted
    q3 = await svc.get_line_quota_status(force_refresh=True)
    assert calls["count"] == 4
    assert q3["used"] == 42
    assert q3["remaining"] == 258
    assert q3["exhausted"] is False
    assert line_service._push_quota_exhausted is False

    # HTTP 429 helper marks quota exhausted immediately on disk cache
    ex = await svc.mark_quota_exhausted(used=300, limit=300)
    assert ex["exhausted"] is True
    saved = json.loads((tmp_path / "inbox.json").read_text(encoding="utf-8"))
    assert saved["quota_cache"]["source"] == "http_429"
    assert QUOTA_CACHE_TTL_SECONDS == 60.0


@pytest.mark.asyncio
async def test_inbox_dedup_gateway_online_and_clear_archives_jsonl(tmp_path, monkeypatch):
    state_file = tmp_path / "inbox.json"
    archive_dir = tmp_path / "logs"
    monkeypatch.setattr(settings, "MINIAPP_INBOX_STATE_PATH", str(state_file))
    monkeypatch.setattr(settings, "MINIAPP_INBOX_ARCHIVE_DIR", str(archive_dir), raising=False)

    svc = MiniAppInboxService()
    # Legacy startup entry without dedup_key + new deduped entries must collapse into 1
    await svc.publish(category="system", title="🚀 InvestmentHomelab Gateway ออนไลน์", body="run 1")
    await svc.publish(category="trade", title="Order Matched", body="ORI Cover matched")
    await svc.publish(
        category="system",
        title="🚀 InvestmentHomelab Gateway ออนไลน์",
        body="run 2",
        meta={"dedup_key": "gateway_online", "is_heartbeat": True},
    )
    await svc.publish(
        category="system",
        title="🚀 InvestmentHomelab Gateway ออนไลน์",
        body="run 3",
        meta={"dedup_key": "gateway_online", "is_heartbeat": True},
    )

    listing = await svc.list_items(limit=20)
    assert listing["total"] == 2
    assert listing["items"][0]["title"] == "🚀 InvestmentHomelab Gateway ออนไลน์"
    assert listing["items"][0]["body"] == "run 3"
    assert listing["items"][0]["meta"]["repeat_count"] == 3
    assert listing["items"][1]["title"] == "Order Matched"

    cleared = await svc.clear_items(archive=True)
    assert cleared["ok"] is True
    assert cleared["cleared"] == 2
    assert cleared["unread_count"] == 0
    assert cleared["archived_file"] is not None

    arch_path = tmp_path / "logs" / cleared["archived_file"].split("/")[-1]
    assert arch_path.exists()
    lines = [json.loads(line) for line in arch_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0]["title"] == "Order Matched"
    assert lines[1]["body"] == "run 3"
    assert (await svc.list_items())["total"] == 0


@pytest.mark.asyncio
async def test_line_quota_reset_info_jst_to_bkk(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from app.services.miniapp_inbox import compute_line_quota_reset_info

    nov = compute_line_quota_reset_info(datetime(2026, 11, 18, 6, 0, tzinfo=timezone.utc))
    assert nov["reset_at_jst"] == "2026-12-01T00:00:00+09:00"
    assert nov["reset_at_bkk"] == "2026-11-30T22:00:00+07:00"
    assert nov["reset_date_bkk"] == "2026-11-30"
    assert "1 ธ.ค. 00:00 JST" in nov["reset_label"]
    assert "30 พ.ย. 22:00 น. ไทย" in nov["reset_label"]

    dec = compute_line_quota_reset_info(datetime(2026, 12, 20, 12, 0, tzinfo=timezone.utc))
    assert dec["reset_at_jst"] == "2027-01-01T00:00:00+09:00"
    assert dec["reset_at_bkk"] == "2026-12-31T22:00:00+07:00"

    monkeypatch.setattr(settings, "MINIAPP_INBOX_STATE_PATH", str(tmp_path / "inbox.json"))
    monkeypatch.setattr(settings, "LINE_SIMULATION_MODE", True)
    svc = MiniAppInboxService()
    q = await svc.get_line_quota_status()
    assert q["reset_label"]
    assert q["reset_at_jst"]
    assert q["reset_at_bkk"]
