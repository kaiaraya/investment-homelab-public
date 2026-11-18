import pytest
from app.core.config import settings
from app.services.line_service import line_service


@pytest.fixture(autouse=True)
def _enable_legacy_line_push(monkeypatch):
    monkeypatch.setattr(settings, "LINE_PUSH_MODE", "all")

def test_build_batch_flex_message():
    sample_batch = {
        "batch_id": "B-TEST1234",
        "card": {
            "batch_id": "B-TEST1234",
            "title": "Batch Review",
            "immediate_orders": [
                {"symbol": "TRUE", "side": "SELL", "volume": 100, "price": 13.10},
                {"symbol": "TIDLOR", "side": "SELL", "volume": 2000, "price": 18.20},
                {"symbol": "AOT", "side": "SELL", "volume": 600, "price": 62.25},
                {"symbol": "TFM", "side": "SELL", "volume": 4500, "price": 6.15}
            ],
            "triggers_count": 4,
            "warnings": ["⚠️ Warning 1"]
        }
    }
    
    flex = line_service._build_batch_flex_message(sample_batch)
    assert flex["type"] == "flex"
    assert "altText" in flex
    bubble = flex["contents"]
    assert bubble["type"] == "bubble"
    assert bubble["header"]["type"] == "box"
    assert bubble["body"]["type"] == "box"
    assert bubble["footer"]["type"] == "box"
    
    # Check action buttons
    buttons = bubble["footer"]["contents"]
    assert len(buttons) == 2
    assert "action=batch_approve&batch_id=B-TEST1234" in buttons[0]["action"]["data"]
    assert "action=batch_cancel&batch_id=B-TEST1234" in buttons[1]["action"]["data"]
    
    # Check quick reply
    assert "quickReply" in flex


@pytest.mark.asyncio
async def test_push_message_wraps_raw_bubble(monkeypatch):
    sent_payload = []

    async def fake_post(self, url, headers=None, json=None, **kwargs):
        sent_payload.append(json)
        class FakeResp:
            status_code = 200
            text = "ok"
        return FakeResp()

    monkeypatch.setattr(line_service, "_send_reply", None)
    monkeypatch.setattr("httpx.AsyncClient.post", fake_post)
    from app.core.config import settings
    monkeypatch.setattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", "real_token_xyz")
    monkeypatch.setattr(settings, "LINE_SIMULATION_MODE", False)

    raw_bubble = {"type": "bubble", "body": {"type": "box", "layout": "vertical", "contents": []}}
    await line_service.push_message(raw_bubble, to_user_id="U1234")

    assert len(sent_payload) == 1
    msg = sent_payload[0]["messages"][0]
    assert msg["type"] == "flex"
    assert msg["contents"]["type"] == "bubble"

