"""Unit tests for the 1x3 Compact Rich Menu (2500x843) & CLI --replace-all support."""

from __future__ import annotations

from app.services.line_cli import rich_menu


def test_rich_menu_1x3_compact_spec_and_reserved_cells():
    assert rich_menu.RICH_MENU_WIDTH == 2500
    assert rich_menu.RICH_MENU_HEIGHT == 843

    spec = rich_menu.get_homelab_rich_menu_spec("2012540531-S5Ozs4US")
    assert spec["size"] == {"width": 2500, "height": 843}
    assert spec["name"] == "InvestmentHomelab MiniApp 1x3"
    assert len(spec["chatBarText"]) <= 14
    assert spec["selected"] is True

    # Strictly 1 clickable region (Cell 1); Cells 2 & 3 left unmapped to avoid webhook/quota chatter
    areas = spec["areas"]
    assert len(areas) == 1
    assert areas[0]["bounds"] == {"x": 0, "y": 0, "width": 833, "height": 843}
    assert areas[0]["action"]["type"] == "uri"
    assert areas[0]["action"]["uri"] == "https://miniapp.line.me/2012540531-S5Ozs4US"


def test_rich_menu_fallback_uri_and_html_dimensions():
    spec_fallback = rich_menu.get_homelab_rich_menu_spec("")
    assert spec_fallback["areas"][0]["action"]["uri"] == "https://line.a14s.dev/miniapp/"

    html = rich_menu.get_rich_menu_html()
    assert "width: 2500px;" in html
    assert "height: 843px;" in html
    assert "grid-template-columns: repeat(3, 1fr);" in html
    assert "HomeApp MiniApp" in html
    assert "Coming Soon" in html
