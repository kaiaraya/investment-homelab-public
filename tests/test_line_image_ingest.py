"""LINE inbound image → data/screenshots/inbox + attach to the referenced card.

Card: t_0bd598ba. Phase 1 is archive-only: no AI vision here (see t_f7e4c35a for the
vision/OCR card). What must hold:

1. an image event is downloaded with the channel token and written to the inbox,
2. the webhook never crashes on a bad/slow/oversized image,
3. a `#t_xxxxxxxx` from the operator's last text message makes the image attach to
   that card through the hermes CLI,
4. no card ref → no attach, and the reply says so instead of silently dropping it.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.core.config import settings
from app.services import line_service as svc_mod
from app.services.line_service import LineMessagingService

INBOX = Path(svc_mod.__file__).resolve().parents[2] / "data" / "screenshots" / "inbox"


class _FakeResp:
    def __init__(self, status_code=200, content=b"\xff\xd8\xff\xe0fakejpeg"):
        self.status_code = status_code
        self.content = content
        self.text = ""


class _FakeClient:
    """Stand-in for httpx.AsyncClient used as `async with`."""

    calls: list[tuple[str, dict]] = []
    resp = _FakeResp()

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        type(self).calls.append((url, headers or {}))
        return type(self).resp


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(settings, "LINE_CHAT_COMMANDS_ENABLED", True)
    monkeypatch.setattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "tok_test_123", raising=False)
    monkeypatch.setattr(settings, "LINE_SIMULATION_MODE", False, raising=False)
    monkeypatch.setattr(svc_mod.httpx, "AsyncClient", _FakeClient, raising=False)
    svc_mod._last_card_ref.clear()
    _FakeClient.calls = []
    _FakeClient.resp = _FakeResp()

    svc = LineMessagingService()
    sent: list[str] = []

    async def _fake_reply(token, text):
        sent.append(text)

    svc._send_reply = _fake_reply  # type: ignore[assignment]
    svc.sent = sent  # type: ignore[attr-defined]
    yield svc
    # remove whatever the test dropped in the real inbox
    for f in INBOX.glob("*-msg_*.jpg"):
        try:
            if time.time() - f.stat().st_mtime < 120:
                f.unlink()
        except OSError:
            pass


def _image_event(message_id="msg_1", user="U_admin"):
    return {
        "type": "message",
        "replyToken": "rt_1",
        "source": {"type": "user", "userId": user},
        "message": {"type": "image", "id": message_id, "contentProvider": {"type": "line"}},
    }


def _text_event(text, user="U_admin"):
    return {
        "type": "message",
        "replyToken": "rt_2",
        "source": {"type": "user", "userId": user},
        "message": {"type": "text", "id": "m_txt", "text": text},
    }


async def test_image_is_saved_to_inbox_and_replied(service):
    res = await service.process_webhook_events(db=None, events=[_image_event()])

    assert res[0]["status"] == "IMAGE_RECEIVED"
    saved = INBOX / res[0]["filename"]
    assert saved.exists() and saved.stat().st_size > 0
    # the download went to the content endpoint with the channel token
    assert _FakeClient.calls[0][0].endswith("/v2/bot/message/msg_1/content")
    assert _FakeClient.calls[0][1]["Authorization"] == "Bearer tok_test_123"
    assert "/screenshots/inbox/" in service.sent[0]


async def test_image_attaches_to_referenced_card(service):
    attached: list[tuple[str, str]] = []
    service._attach_to_card = lambda tid, p: (attached.append((tid, p.name)) or (True, "ok"))  # type: ignore

    await service.process_webhook_events(db=None, events=[_text_event("ดูการ์ด #t_abc123ef หน่อย")])
    res = await service.process_webhook_events(db=None, events=[_image_event("msg_2")])

    assert attached and attached[0][0] == "t_abc123ef"
    assert res[0]["attached_to"] == "t_abc123ef"
    assert "แนบเข้า #t_abc123ef" in service.sent[-1]


async def test_image_without_card_ref_says_so(service):
    attached = []
    service._attach_to_card = lambda tid, p: (attached.append(tid) or (True, "ok"))  # type: ignore

    res = await service.process_webhook_events(db=None, events=[_image_event("msg_3")])

    assert attached == []
    assert res[0]["attached_to"] is None
    assert "ไม่มีเลขการ์ด" in service.sent[-1]


async def test_stale_card_ref_is_forgotten(service):
    service._attach_to_card = None  # any attach attempt would raise
    svc_mod._last_card_ref["U_admin"] = ("t_oldref99", time.time() - svc_mod._CARD_REF_TTL_S - 1)

    res = await service.process_webhook_events(db=None, events=[_image_event("msg_4")])
    assert res[0]["attached_to"] is None


async def test_attach_failure_is_reported_not_raised(service):
    service._attach_to_card = lambda tid, p: (False, "boom")  # type: ignore

    await service.process_webhook_events(db=None, events=[_text_event("#t_deadbeef")])
    res = await service.process_webhook_events(db=None, events=[_image_event("msg_5")])

    assert res[0]["attached_to"] is None
    assert "แนบเข้า #t_deadbeef ไม่สำเร็จ" in service.sent[-1]


async def test_http_error_never_crashes_the_webhook(service):
    _FakeClient.resp = _FakeResp(status_code=500)

    res = await service.process_webhook_events(db=None, events=[_image_event("msg_6")])

    assert res[0]["status"] == "IMAGE_FAILED"
    assert "ดาวน์โหลดไม่สำเร็จ" in service.sent[0]


async def test_oversized_payload_is_refused(service, monkeypatch):
    monkeypatch.setattr(svc_mod, "_MAX_IMAGE_BYTES", 4, raising=False)
    _FakeClient.resp = _FakeResp(content=b"x" * 64)

    res = await service.process_webhook_events(db=None, events=[_image_event("msg_7")])

    assert res[0]["status"] == "IMAGE_FAILED"
    assert res[0]["filename"] is None
    assert not list(INBOX.glob("*msg_7*.jpg"))


async def test_simulation_mode_does_not_hit_the_api(monkeypatch):
    monkeypatch.setattr(settings, "LINE_CHAT_COMMANDS_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "LINE_SIMULATION_MODE", True, raising=False)
    monkeypatch.setattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "mock_line_channel_token", raising=False)
    _FakeClient.calls = []
    svc = LineMessagingService()
    svc._send_reply = lambda *a, **k: __import__("asyncio").sleep(0)  # type: ignore

    res = await svc.process_webhook_events(db=None, events=[_image_event("msg_8")])

    assert _FakeClient.calls == []
    assert res[0]["status"] == "IMAGE_FAILED"
