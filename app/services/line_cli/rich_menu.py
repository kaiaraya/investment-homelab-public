"""
LINE Rich Menu Definitions and Management for InvestmentHomelab (Phase 5: 2x3 Hybrid MiniApp).

Provides:
1. 2x3 Grid Rich Menu Specification (2500x1686)
   - Row 1 Left:   📊 Portfolio   -> MiniApp ?tab=portfolio
   - Row 1 Center: 📋 Orders      -> MiniApp ?tab=orders
   - Row 1 Right:  ⚡ Nego        -> MiniApp ?tab=nego
   - Row 2 Left:   💹 Trade       -> MiniApp ?tab=trade
   - Row 2 Center: 🏦 Brokers     -> MiniApp ?tab=brokers
   - Row 2 Right:  🃏 Quick Cards -> Postback action=quick_cards_menu (free replyToken)
2. API management: create, upload image, set default, list, delete.
3. Dark Glassmorphic HTML generator for Playwright rendering.
"""

from typing import Dict, Any, List, Optional
import httpx
import logging
from app.core.config import settings

logger = logging.getLogger(__name__)

RICH_MENU_WIDTH = 2500
RICH_MENU_HEIGHT = 843


def _miniapp_root_uri(liff_id: Optional[str] = None) -> str:
    lid = (liff_id if liff_id is not None else getattr(settings, "LIFF_ID", None) or "").strip()
    if lid:
        base = (getattr(settings, "MINIAPP_PUBLIC_BASE_URL", None) or "https://miniapp.line.me").rstrip("/")
        return f"{base}/{lid}"
    return "https://line.a14s.dev/miniapp/"


def get_homelab_rich_menu_spec(liff_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Returns the LINE Rich Menu JSON schema for the 1x3 Compact MiniApp launcher (2500x843).

    Cell 1 (leftmost third, width=833) opens HomeApp MiniApp via LIFF URI.
    Cells 2 & 3 are visually reserved (unmapped in `areas` -> zero webhook/quota impact).
    """
    w1 = 833
    return {
        "size": {
            "width": RICH_MENU_WIDTH,
            "height": RICH_MENU_HEIGHT,
        },
        "selected": True,
        "name": "InvestmentHomelab MiniApp 1x3",
        "chatBarText": "เปิด HomeApp",
        "areas": [
            {
                "bounds": {"x": 0, "y": 0, "width": w1, "height": RICH_MENU_HEIGHT},
                "action": {
                    "type": "uri",
                    "label": "🚀 เปิด MiniApp",
                    "uri": _miniapp_root_uri(liff_id=liff_id),
                },
            }
        ],
    }


def get_rich_menu_html() -> str:
    """
    Returns high-contrast dark glassmorphic HTML (2500x843, 1x3 Compact Grid) for Playwright rendering.
    """
    return f"""<!DOCTYPE html>
<html lang="th">
<head>
  <meta charset="UTF-8">
  <style>
    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Noto Sans Thai", sans-serif;
    }}
    body {{
      width: {RICH_MENU_WIDTH}px;
      height: {RICH_MENU_HEIGHT}px;
      background: radial-gradient(circle at 20% 50%, #111f3b 0%, #06090e 100%);
      color: #ffffff;
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      grid-template-rows: 1fr;
      gap: 24px;
      padding: 28px;
      overflow: hidden;
    }}

    .grid-cell {{
      position: relative;
      border-radius: 38px;
      background: linear-gradient(145deg, rgba(255, 255, 255, 0.065) 0%, rgba(255, 255, 255, 0.015) 100%);
      border: 2px solid rgba(255, 255, 255, 0.1);
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      text-align: center;
      padding: 36px;
      box-shadow: inset 0 1px 1px rgba(255, 255, 255, 0.1), 0 20px 40px rgba(0, 0, 0, 0.45);
    }}

    .c-miniapp {{
      background: linear-gradient(145deg, rgba(14, 165, 233, 0.18) 0%, rgba(15, 23, 42, 0.85) 100%);
      border: 3px solid rgba(56, 189, 248, 0.65);
    }}
    .c-reserved {{
      background: linear-gradient(145deg, rgba(255, 255, 255, 0.025) 0%, rgba(255, 255, 255, 0.008) 100%);
      border: 2px dashed rgba(148, 163, 184, 0.28);
      opacity: 0.72;
    }}

    .icon-box {{
      width: 170px;
      height: 170px;
      border-radius: 44px;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 84px;
      margin-bottom: 30px;
      box-shadow: 0 14px 32px rgba(0, 0, 0, 0.4);
    }}

    .i-miniapp  {{ background: linear-gradient(135deg, #0284c7, #38bdf8); }}
    .i-reserved {{ background: linear-gradient(135deg, #1e293b, #334155); opacity: 0.7; font-size: 64px; }}

    .title {{
      font-size: 58px;
      font-weight: 800;
      letter-spacing: -0.5px;
      margin-bottom: 14px;
    }}
    .c-reserved .title {{
      font-size: 46px;
      color: #94a3b8;
    }}
    .subtitle {{
      font-size: 32px;
      font-weight: 500;
      color: #cbd5e1;
    }}
    .c-reserved .subtitle {{
      font-size: 28px;
      color: #64748b;
    }}
    .badge {{
      display: inline-block;
      margin-top: 26px;
      padding: 10px 28px;
      border-radius: 9999px;
      font-size: 26px;
      font-weight: 700;
      background: rgba(56, 189, 248, 0.2);
      color: #e0f2fe;
      border: 1px solid rgba(56, 189, 248, 0.5);
    }}
    .c-reserved .badge {{
      background: rgba(148, 163, 184, 0.08);
      color: #64748b;
      border-color: rgba(148, 163, 184, 0.2);
    }}
  </style>
</head>
<body>
  <div class="grid-cell c-miniapp">
    <div class="icon-box i-miniapp">🚀</div>
    <div class="title">HomeApp MiniApp</div>
    <div class="subtitle">พอร์ต · เทรด · ต่อราคา · OTP · Stop</div>
    <div class="badge">เปิดใช้งาน</div>
  </div>
  <div class="grid-cell c-reserved">
    <div class="icon-box i-reserved">⏳</div>
    <div class="title">Reserved</div>
    <div class="subtitle">ช่องสำรองสำหรับขยายผล</div>
    <div class="badge">Coming Soon</div>
  </div>
  <div class="grid-cell c-reserved">
    <div class="icon-box i-reserved">⏳</div>
    <div class="title">Reserved</div>
    <div class="subtitle">ช่องสำรองสำหรับขยายผล</div>
    <div class="badge">Coming Soon</div>
  </div>
</body>
</html>"""


class LineRichMenuService:
    """
    Manages creation, image uploading, and default assignment of LINE Rich Menu.
    """

    def __init__(self, token: Optional[str] = None):
        self.token = token or settings.LINE_CHANNEL_ACCESS_TOKEN
        self.is_simulation = settings.LINE_SIMULATION_MODE or not self.token or self.token == "mock_line_channel_token"

    async def list_rich_menus(self) -> List[Dict[str, Any]]:
        """Lists all active rich menus from LINE API."""
        if self.is_simulation:
            logger.info("[LINE RichMenu] Simulating list_rich_menus")
            return []

        url = "https://api.line.me/v2/bot/richmenu/list"
        headers = {"Authorization": f"Bearer {self.token}"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.json().get("richmenus", [])
            logger.error(f"[LINE RichMenu] List failed ({resp.status_code}): {resp.text}")
            return []

    async def create_rich_menu(self, spec: Optional[Dict[str, Any]] = None) -> Optional[str]:
        """Creates a rich menu resource in LINE and returns its richMenuId."""
        payload = spec or get_homelab_rich_menu_spec()

        if self.is_simulation:
            logger.info(f"[LINE RichMenu] Simulating create_rich_menu: {payload.get('name')}")
            return "richmenu-simulated-id-homelab"

        url = "https://api.line.me/v2/bot/richmenu"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            if resp.status_code in (200, 201):
                menu_id = resp.json().get("richMenuId")
                logger.info(f"[LINE RichMenu] Created richMenuId: {menu_id}")
                return menu_id
            logger.error(f"[LINE RichMenu] Creation failed ({resp.status_code}): {resp.text}")
            return None

    async def upload_rich_menu_image(self, rich_menu_id: str, image_bytes: bytes, content_type: str = "image/png") -> bool:
        """Uploads PNG/JPEG image data to a created richMenuId."""
        if self.is_simulation:
            logger.info(f"[LINE RichMenu] Simulating upload_rich_menu_image for {rich_menu_id} ({len(image_bytes)} bytes)")
            return True

        url = f"https://api-data.line.me/v2/bot/richmenu/{rich_menu_id}/content"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": content_type
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(url, headers=headers, content=image_bytes)
            if resp.status_code == 200:
                logger.info(f"[LINE RichMenu] Successfully uploaded image for {rich_menu_id}")
                return True
            logger.error(f"[LINE RichMenu] Image upload failed ({resp.status_code}): {resp.text}")
            return False

    async def set_default_rich_menu(self, rich_menu_id: str) -> bool:
        """Links the rich menu as default for all users."""
        if self.is_simulation:
            logger.info(f"[LINE RichMenu] Simulating set_default_rich_menu: {rich_menu_id}")
            return True

        url = f"https://api.line.me/v2/bot/user/all/richmenu/{rich_menu_id}"
        headers = {"Authorization": f"Bearer {self.token}"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, headers=headers)
            if resp.status_code == 200:
                logger.info(f"[LINE RichMenu] Set default rich menu to {rich_menu_id}")
                return True
            logger.error(f"[LINE RichMenu] Set default failed ({resp.status_code}): {resp.text}")
            return False

    async def delete_rich_menu(self, rich_menu_id: str) -> bool:
        """Deletes a rich menu from LINE API."""
        if self.is_simulation:
            logger.info(f"[LINE RichMenu] Simulating delete_rich_menu: {rich_menu_id}")
            return True

        url = f"https://api.line.me/v2/bot/richmenu/{rich_menu_id}"
        headers = {"Authorization": f"Bearer {self.token}"}
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(url, headers=headers)
            return resp.status_code == 200
