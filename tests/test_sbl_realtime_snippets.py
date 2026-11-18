import pytest
import os
import json
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import app.brokers.krungsri.sbl_realtime_driver as sbl_mod
from app.brokers.krungsri.sbl_realtime_driver import (
    KrungsriSBLRealtimeDriver,
    krungsri_sbl_driver,
    _bkk_today,
)
from app.core.config import settings


@pytest.mark.asyncio
async def test_sbl_borrow_snippet_files_exist():
    """Verify that the SBL borrow and return JS snippet files exist and are non-empty."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    borrow_js = os.path.join(root, "data", "bookmarklets", "sbl_borrow.js")
    return_js = os.path.join(root, "data", "bookmarklets", "sbl_return.js")
    assert os.path.exists(borrow_js)
    assert os.path.exists(return_js)
    with open(borrow_js, "r", encoding="utf-8") as f:
        content = f.read()
        assert "#MainCenter_lbBorrow" in content
        assert "BLOCKED_BY_XD_XR" in content
    with open(return_js, "r", encoding="utf-8") as f:
        content = f.read()
        assert "RadSave" in content
        assert "Return.aspx" in content


@pytest.mark.asyncio
async def test_execute_sbl_borrow_xd_hard_block():
    """Verify that execute_sbl_borrow refuses symbols flagged with XD/XR in cache."""
    mock_cache = {
        "xd_xr_inventory": [
            {"symbol": "PTT", "book_date": "28/10/2026", "type": "XD"}
        ]
    }
    with patch.object(krungsri_sbl_driver, "get_cached_data", return_value=mock_cache):
        res = await krungsri_sbl_driver.execute_sbl_borrow("PTT", 100)
    assert res["success"] is False
    assert res["status"] == "BLOCKED_BY_XD_XR"
    assert "XD" in res["error"]


@pytest.mark.asyncio
async def test_execute_sbl_borrow_dry_run_js_first():
    """Verify that execute_sbl_borrow delegates to js_runner with dry_run=True when DRY_RUN_MODE is active."""
    mock_cdp_return = {
        "success": True,
        "data": {
            "success": True,
            "status": "DRY_RUN_ARMED",
            "dry_run": True,
            "symbol": "TU",
            "volume": 100,
            "purchasing_power": 669627.64,
            "message": "[DRY-RUN] SBL borrow form filled for TU 100 shares. PostBack held."
        }
    }
    with patch.object(settings, "DRY_RUN_MODE", True):
        with patch.object(krungsri_sbl_driver, "get_cached_data", return_value={"xd_xr_inventory": []}):
            with patch("scripts.broker_cdp.js_runner.run_snippet", new_callable=AsyncMock, return_value=mock_cdp_return) as m_runner:
                res = await krungsri_sbl_driver.execute_sbl_borrow("TU", 100)
    assert res["success"] is True
    assert res["status"] == "DRY_RUN_ARMED"
    assert res["source"] == "sbl_borrow_js"
    assert res["dry_run"] is True
    m_runner.assert_awaited_once()


@pytest.mark.asyncio
async def test_return_sbl_shares_dry_run_cdp_ui():
    """Verify return_sbl_shares delegates to Playwright UI runner and skips verify_return_submitted in DRY_RUN."""
    mock_ui_return = {
        "success": True,
        "status": "DRY_RUN_ARMED",
        "dry_run": True,
        "lot_id": "B494354",
        "symbol": "TU",
        "return_qty": 100,
        "source": "sbl_return_cdp_ui",
        "trace": {"search_applied": True, "filled_state": {"txtReturnQty": "100"}},
        "message": "[DRY-RUN] Contract B494354 (TU) form filled via UI. Cancelled cleanly.",
    }
    with patch.object(settings, "DRY_RUN_MODE", True):
        with patch.object(krungsri_sbl_driver, "_return_via_cdp_ui", new_callable=AsyncMock, return_value=mock_ui_return) as m_ui:
            res = await krungsri_sbl_driver.return_sbl_shares("B494354", 100, symbol="TU")
    assert res["success"] is True
    assert res["status"] == "DRY_RUN_ARMED"
    assert res["source"] == "sbl_return_cdp_ui"
    assert res["dry_run"] is True
    assert res["lot_id"] == "B494354"
    m_ui.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_sbl_return_ui_mock_dom_search_dryrun_live_and_reject():
    """End-to-end Playwright test against mock MainSBL.aspx + Return.aspx Telerik DOM."""
    import threading
    from functools import partial
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse
    from playwright.async_api import async_playwright
    from app.brokers.krungsri.sbl_realtime_driver import run_sbl_return_ui

    state = {"search": "", "closed": False, "reject_mode": False, "last_post": None}

    class MockSblHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path.endswith("/MainSBL.aspx"):
                # Initial grid shows only TPIPP; searching "AAV" reveals B495090
                q_sym = state["search"]
                row_html = ""
                if q_sym == "AAV":
                    close_cell = "11/11/2026" if state["closed"] else ""
                    btn_html = (
                        ""
                        if state["closed"]
                        else '<a id="ctl00_MainCenter_rgTrans_ctl00_ctl16_RadReturn" href="javascript:void(0);" onclick="return ShowReturnForm(\'B495090   \',\'0\');">คืน</a>'
                    )
                    row_html = f"""
                    <tr class="rgRow">
                      <td>B495090</td><td>09/11/2026</td><td>10:32:28</td><td>AAV</td><td>B</td>
                      <td>94,900</td><td>0.77</td><td>73,073.00</td><td>3.5</td><td>{close_cell}</td><td>{btn_html}</td>
                    </tr>"""
                else:
                    row_html = """
                    <tr class="rgRow">
                      <td>B495111</td><td>10/11/2026</td><td>10:30:28</td><td>TPIPP</td><td>B</td>
                      <td>48,700</td><td>1.53</td><td>74,511.00</td><td>3.5</td><td></td>
                      <td><a id="ctl00_MainCenter_rgTrans_ctl00_ctl04_RadReturn" href="javascript:void(0);" onclick="return ShowReturnForm('B495111   ','0');">คืน</a></td>
                    </tr>"""
                html = f"""<!doctype html><html><body>
                <form id="form1" method="get" action="/sblweb/MainSBL.aspx">
                  <input id="ctl00_MainCenter_txtSearchSymbol" name="sym" value="{q_sym}" />
                  <input id="ctl00_MainCenter_txtSearchSymbol_ClientState" type="hidden" value="{{}}" />
                  <a id="MainCenter_lbNext" href="javascript:void(0)" onclick="doSearch()">Search</a>
                  <table id="ctl00_MainCenter_rgTrans_ctl00"><tbody>{row_html}</tbody></table>
                  <div id="RadWindowWrapper_ctl00_MainCenter_UserListDialog" style="display:none">
                    <iframe id="UserListDialogFrame" name="UserListDialog" src="about:blank"></iframe>
                  </div>
                </form>
                <script>
                  window.$find = function(id) {{
                    if (id === 'ctl00_MainCenter_txtSearchSymbol') {{
                      return {{ set_value: function(v) {{ document.getElementById(id).value = v; }} }};
                    }}
                    return null;
                  }};
                  function doSearch() {{
                    var s = encodeURIComponent(document.getElementById('ctl00_MainCenter_txtSearchSymbol').value || '');
                    window.location.href = '/sblweb/MainSBL.aspx?search=' + s;
                  }}
                  function refreshGrid() {{
                    window.location.href = '/sblweb/MainSBL.aspx?search=' + encodeURIComponent('{q_sym}');
                  }}
                  function ShowReturnForm(refId) {{
                    var w = document.getElementById('RadWindowWrapper_ctl00_MainCenter_UserListDialog');
                    var f = document.getElementById('UserListDialogFrame');
                    w.style.display = 'block';
                    f.radWindow = {{ BrowserWindow: window, close: function() {{ w.style.display = 'none'; }} }};
                    f.src = '/sblweb/Return.aspx?Ref=' + encodeURIComponent((refId || '').trim());
                    return false;
                  }}
                </script></body></html>"""
                qs = parse_qs(parsed.query)
                if "search" in qs:
                    state["search"] = (qs["search"][0] or "").strip().upper()
                    # Re-render with updated search immediately on redirectless GET
                    row_html2 = ""
                    if state["search"] == "AAV":
                        close_cell = "11/11/2026" if state["closed"] else ""
                        btn_html = "" if state["closed"] else '<a id="ctl00_MainCenter_rgTrans_ctl00_ctl16_RadReturn" href="javascript:void(0);" onclick="return ShowReturnForm(\'B495090   \',\'0\');">คืน</a>'
                        row_html2 = f'<tr class="rgRow"><td>B495090</td><td>09/11/2026</td><td>10:32:28</td><td>AAV</td><td>B</td><td>94,900</td><td>0.77</td><td>73,073.00</td><td>3.5</td><td>{close_cell}</td><td>{btn_html}</td></tr>'
                    html = html.replace(row_html, row_html2)
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
                return

            if parsed.path.endswith("/Return.aspx"):
                html = """<!doctype html><html><body>
                <form id="form1" method="post" action="/sblweb/Return.aspx?Ref=B495090">
                  <input type="hidden" id="__EVENTTARGET" name="__EVENTTARGET" value="" />
                  <input type="hidden" id="__EVENTARGUMENT" name="__EVENTARGUMENT" value="" />
                  <input id="txtSymbol" name="txtSymbol" value="AAV" readonly />
                  <input id="txtRef" name="txtRef" value="B495090" readonly />
                  <input id="txtBalance" name="txtBalance" value="94,900" readonly />
                  <input id="txtReturnQty" name="txtReturnQty" value="" />
                  <input id="txtReturnQty_ClientState" name="txtReturnQty_ClientState" type="hidden" value="{}" />
                  <textarea id="txtRemark" name="txtRemark"></textarea>
                  <a id="RadSave" href="javascript:__doPostBack('RadSave','')">SAVE</a>
                  <a id="RadCancel" href="javascript:CancelEdit()">CANCEL</a>
                </form>
                <script>
                  window.$find = function(id) {
                    if (id === 'txtReturnQty' || id === 'txtRemark') {
                      return {
                        set_value: function(v) {
                          document.getElementById(id).value = String(v);
                          var cs = document.getElementById(id + '_ClientState');
                          if (cs) cs.value = JSON.stringify({ enabled: true, validationText: String(v), valueAsString: String(v) });
                        }
                      };
                    }
                    return null;
                  };
                  function CancelEdit() {
                    if (window.frameElement && window.frameElement.radWindow) window.frameElement.radWindow.close();
                  }
                  function __doPostBack(target, arg) {
                    document.getElementById('__EVENTTARGET').value = target;
                    document.getElementById('__EVENTARGUMENT').value = arg;
                    document.getElementById('form1').submit();
                  }
                </script></body></html>"""
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
                return

            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length).decode("utf-8")
            fields = parse_qs(raw_body)
            state["last_post"] = {k: v[0] for k, v in fields.items()}
            if state["reject_mode"]:
                resp = """<!doctype html><html><body>
                <div class="rwDialogText">จำนวนที่ต้องการคืนมากกว่าจำนวนที่สามารถคืนได้</div>
                <button class="rwOkBtn" type="button">OK</button>
                <script>radalert('จำนวนที่ต้องการคืนมากกว่าจำนวนที่สามารถคืนได้');</script>
                </body></html>"""
            else:
                state["closed"] = True
                resp = """<!doctype html><html><body>
                <div class="rwDialogText">Save Successfull</div>
                <button class="rwOkBtn" type="button" onclick="window.frameElement.radWindow.BrowserWindow.refreshGrid(); window.frameElement.radWindow.close();">OK</button>
                <script>/* Save Successfull */</script>
                </body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(resp.encode("utf-8"))

    server = ThreadingHTTPServer(("127.0.0.1", 0), MockSblHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        async with async_playwright() as pw:
            launch_kw = {"headless": True}
            if os.path.exists(settings.PLAYWRIGHT_CHROME_EXECUTABLE):
                launch_kw["executable_path"] = settings.PLAYWRIGHT_CHROME_EXECUTABLE
            browser = await pw.chromium.launch(**launch_kw)
            page = await browser.new_page()
            base_sbl = f"http://127.0.0.1:{port}/sblweb/MainSBL.aspx"

            # 1) DRY-RUN finds AAV via txtSearchSymbol (no Ref.no needed), fills Telerik ClientState, clicks Cancel
            await page.goto(base_sbl)
            res_dry = await run_sbl_return_ui(page, symbol="AAV", return_qty=94900, dry_run=True, sbl_url=base_sbl)
            assert res_dry["success"] is True
            assert res_dry["status"] == "DRY_RUN_ARMED"
            assert res_dry["lot_id"] == "B495090"
            assert res_dry["trace"]["search_applied"] is True
            assert '"valueAsString":"94900"' in res_dry["trace"]["filled_state"]["txtReturnQty_ClientState"]
            assert state["last_post"] is None

            # 2) LIVE refusal via radalert surfaces NOT_RETURNABLE + exact broker message
            state["reject_mode"] = True
            await page.goto(base_sbl)
            res_rej = await run_sbl_return_ui(page, symbol="AAV", return_qty=94900, dry_run=False, sbl_url=base_sbl)
            assert res_rej["success"] is False
            assert res_rej["status"] == "NOT_RETURNABLE"
            assert "มากกว่าจำนวนที่สามารถคืนได้" in res_rej["broker_message"]

            # 3) LIVE save succeeds, sends Telerik ClientState + __EVENTTARGET=RadSave, closes modal
            state["reject_mode"] = False
            await page.goto(base_sbl)
            res_ok = await run_sbl_return_ui(page, symbol="AAV", return_qty=94900, dry_run=False, remark="test", sbl_url=base_sbl)
            assert res_ok["success"] is True
            assert res_ok["status"] == "RETURN_ACCEPTED"
            assert res_ok["lot_id"] == "B495090"
            assert state["last_post"]["__EVENTTARGET"] == "RadSave"
            assert state["last_post"]["txtReturnQty"] == "94900"
            assert '"valueAsString":"94900"' in state["last_post"]["txtReturnQty_ClientState"]
            assert state["closed"] is True
            assert res_ok["trace"].get("url_refreshed") is True
            assert "?search=" not in page.url
            await browser.close()
    finally:
        server.shutdown()
        server.server_close()


async def test_run_sbl_return_ui_late_parent_radalert_and_url_refresh():
    """Verify >4KB Return.aspx POST radalert tail + late parent .rwOkBtn dismissal + clean MainSBL reload."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import parse_qs, urlparse
    from playwright.async_api import async_playwright
    from app.brokers.krungsri.sbl_realtime_driver import run_sbl_return_ui

    state = {"search": "", "closed": False}

    class LateParentHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path.endswith("/MainSBL.aspx"):
                qs = parse_qs(parsed.query)
                if "search" in qs:
                    state["search"] = (qs["search"][0] or "").strip().upper()
                q_sym = state["search"]
                close_cell = "11/11/2026" if state["closed"] else ""
                btn_html = (
                    ""
                    if state["closed"]
                    else '<a id="ctl00_MainCenter_rgTrans_ctl00_ctl04_RadReturn" href="javascript:void(0);" onclick="return ShowReturnForm(\'B495099\');">คืน</a>'
                )
                row_html = f"""
                <tr class="rgRow">
                  <td>B495099</td><td>11/11/2026</td><td>09:00:00</td><td>AAV</td><td>B</td>
                  <td>10,000</td><td>1.00</td><td>10,000.00</td><td>3.5</td><td>{close_cell}</td><td>{btn_html}</td>
                </tr>"""
                # Hidden dummy .rwOkBtn precedes live parent dialog in DOM order
                html = f"""<!doctype html><html><body>
                <div style="display:none" class="rwTemplate">
                  <div class="rwDialogText">Hidden template</div>
                  <button class="rwOkBtn" id="dummyOk" type="button" onclick="window.__dummyClicked=true">OK</button>
                </div>
                <div id="parentAlert" class="rwDialogPopup" style="display:none">
                  <div class="rwDialogText">Save Successfull</div>
                  <button class="rwOkBtn" id="realParentOk" type="button" onclick="window.__parentOkClicked=true; document.getElementById('parentAlert').style.display='none';">OK</button>
                </div>
                <input id="ctl00_MainCenter_txtSearchSymbol" value="{q_sym}" />
                <a id="MainCenter_lbNext" href="javascript:void(0)" onclick="doSearch()">Search</a>
                <table id="ctl00_MainCenter_rgTrans_ctl00"><tbody>{row_html}</tbody></table>
                <div id="RadWindowWrapper_ctl00_MainCenter_UserListDialog" style="display:none">
                  <iframe id="UserListDialogFrame" name="UserListDialog" src="about:blank"></iframe>
                </div>
                <script>
                  window.__dummyClicked = false;
                  window.__parentOkClicked = false;
                  function doSearch() {{
                    window.location.href = '/sblweb/MainSBL.aspx?search=' + encodeURIComponent(document.getElementById('ctl00_MainCenter_txtSearchSymbol').value || '');
                  }}
                  function ShowReturnForm(refId) {{
                    var w = document.getElementById('RadWindowWrapper_ctl00_MainCenter_UserListDialog');
                    var f = document.getElementById('UserListDialogFrame');
                    w.style.display = 'block';
                    f.radWindow = {{
                      BrowserWindow: window,
                      close: function() {{
                        w.style.display = 'none';
                        setTimeout(function() {{
                          document.getElementById('parentAlert').style.display = 'block';
                        }}, 250);
                      }}
                    }};
                    f.src = '/sblweb/Return.aspx?Ref=' + encodeURIComponent(refId);
                    return false;
                  }}
                </script></body></html>"""
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
                return

            if parsed.path.endswith("/Return.aspx"):
                html = """<!doctype html><html><body>
                <form id="form1" method="post" action="/sblweb/Return.aspx?Ref=B495099">
                  <input type="hidden" id="__EVENTTARGET" name="__EVENTTARGET" value="" />
                  <input id="txtReturnQty" name="txtReturnQty" value="" />
                  <input id="txtReturnQty_ClientState" name="txtReturnQty_ClientState" type="hidden" value="{}" />
                  <a id="RadSave" href="javascript:__doPostBack('RadSave','')">SAVE</a>
                </form>
                <script>
                  function __doPostBack(t) {
                    document.getElementById('__EVENTTARGET').value = t;
                    document.getElementById('form1').submit();
                  }
                </script></body></html>"""
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html.encode("utf-8"))
                return

            self.send_response(404)
            self.end_headers()

        def do_POST(self):
            state["closed"] = True
            head_pad = "/* " + ("x" * 4500) + " */"
            resp = f"""<!doctype html><html><head><style>{head_pad}</style></head><body>
            <script>
              if (window.frameElement && window.frameElement.radWindow) window.frameElement.radWindow.close();
              radalert('Save Successfull');
            </script></body></html>"""
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(resp.encode("utf-8"))

    server = ThreadingHTTPServer(("127.0.0.1", 0), LateParentHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        async with async_playwright() as pw:
            launch_kw = {"headless": True}
            if os.path.exists(settings.PLAYWRIGHT_CHROME_EXECUTABLE):
                launch_kw["executable_path"] = settings.PLAYWRIGHT_CHROME_EXECUTABLE
            browser = await pw.chromium.launch(**launch_kw)
            page = await browser.new_page()
            base_sbl = f"http://127.0.0.1:{port}/sblweb/MainSBL.aspx"
            await page.goto(base_sbl)
            res = await run_sbl_return_ui(
                page, symbol="AAV", lot_id="B495099", return_qty=10000, dry_run=False, sbl_url=base_sbl
            )
            assert res["success"] is True
            assert res["status"] == "RETURN_ACCEPTED"
            assert "Save Successfull" in (res["trace"].get("radalerts") or [])
            assert int(res["trace"].get("ok_clicks") or 0) >= 1
            assert res["trace"].get("url_refreshed") is True
            assert page.url == base_sbl
            assert "11/11/2026" in await page.inner_text("#ctl00_MainCenter_rgTrans_ctl00")
            await browser.close()
    finally:
        server.shutdown()
        server.server_close()


# ───────────────────────────────────────────────────────────────────────────── #
# 2.11b — check_symbol_risk must FAIL CLOSED on an empty or stale SBL cache
# ───────────────────────────────────────────────────────────────────────────── #
def _driver_with(cache):
    """Fresh driver instance whose cache is exactly ``cache`` (no disk load)."""
    drv = KrungsriSBLRealtimeDriver.__new__(KrungsriSBLRealtimeDriver)
    drv._lock = asyncio.Lock()
    drv._cached_data = cache
    drv._cache_stamp_missing = False
    return drv


def _fresh(**pools):
    data = {
        "success": True,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "set50_inventory": pools.get("set50", []),
        "set100_inventory": pools.get("set100", []),
        "newsec_inventory": pools.get("newsec", []),
        "xd_xr_inventory": pools.get("xd", []),
    }
    return data


def test_cache_missing_blocks_symbol():
    """No cache at all → is_blocked True (CACHE_MISS), not a silent pass."""
    drv = _driver_with({})
    with patch.object(drv, "get_cached_data", return_value={}):
        risk = drv.check_symbol_risk("PTT")
    assert risk["is_blocked"] is True
    assert risk["verdict"] == "CACHE_MISS"
    assert risk["available_qty"] == 0
    assert "/sbl sync" in risk["reason"]
    assert risk["cache_state"] == "CACHE_MISS"


def test_cache_stale_blocks_symbol():
    """Cache older than the staleness window → is_blocked True (CACHE_STALE)."""
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    cache = _fresh(set50=[{"symbol": "PTT", "available_qty": 5000, "borrow_fee_pct": 3.5}])
    cache["timestamp"] = old
    drv = _driver_with(cache)
    with patch.object(drv, "get_cached_data", return_value=cache):
        risk = drv.check_symbol_risk("PTT")
        status = drv.cache_status()
    assert risk["is_blocked"] is True
    assert risk["verdict"] == "CACHE_STALE"
    assert risk["available_qty"] == 0
    assert status["state"] == "CACHE_STALE"
    assert status["age_seconds"] > status["staleness_seconds"]


def test_fresh_cache_with_inventory_allows_borrow():
    """Only a fresh snapshot proving available_qty > 0 yields is_blocked False."""
    cache = _fresh(set50=[{"symbol": "PTT", "available_qty": 12345,
                           "borrow_fee_pct": 3.5, "initial_margin_pct": 50.0}])
    drv = _driver_with(cache)
    with patch.object(drv, "get_cached_data", return_value=cache):
        risk = drv.check_symbol_risk("ptt")
    assert risk["is_blocked"] is False
    assert risk["verdict"] == "BORROW_ALLOWED"
    assert risk["available_qty"] == 12345
    assert risk["symbol"] == "PTT"


def test_fresh_cache_xd_marker_blocks_borrow():
    """XD marker on a fresh cache still hard-blocks even with inventory > 0."""
    cache = _fresh(
        set50=[{"symbol": "PTT", "available_qty": 99999, "borrow_fee_pct": 3.5}],
        xd=[{"symbol": "PTT", "book_date": "28/10/2026", "type": "XD"}],
    )
    drv = _driver_with(cache)
    with patch.object(drv, "get_cached_data", return_value=cache):
        risk = drv.check_symbol_risk("PTT")
    assert risk["is_blocked"] is True
    assert risk["verdict"] == "HARD_BLOCKED_XD_XR"
    assert "ชดเชยปันผล" in risk["reason"]


def test_xd_marker_with_past_book_date_lifts_block():
    """B2: an XD marker whose book_date has already passed no longer hard-blocks.

    The ex-dividend event already happened, so the dividend-compensation trap is
    gone — the symbol must fall through to the pool checks instead of being
    blocked forever on an ageing snapshot.
    """
    past = (_bkk_today() - timedelta(days=3)).strftime("%d/%m/%Y")
    cache = _fresh(
        set50=[{"symbol": "PTT", "available_qty": 99999, "borrow_fee_pct": 3.5}],
        xd=[{"symbol": "PTT", "book_date": past, "type": "XD"}],
    )
    drv = _driver_with(cache)
    with patch.object(drv, "get_cached_data", return_value=cache):
        risk = drv.check_symbol_risk("PTT")
    assert risk["verdict"] != "HARD_BLOCKED_XD_XR"
    assert risk["verdict"] == "BORROW_ALLOWED"   # inventory > 0, no XD trap left
    assert risk["is_blocked"] is False


def test_xd_marker_with_unparseable_book_date_still_blocks():
    """Fail-closed: a book_date we cannot parse must keep the XD hard-block."""
    cache = _fresh(
        set50=[{"symbol": "PTT", "available_qty": 99999, "borrow_fee_pct": 3.5}],
        xd=[{"symbol": "PTT", "book_date": "7 ต.ค. 2569", "type": "XD"}],
    )
    drv = _driver_with(cache)
    with patch.object(drv, "get_cached_data", return_value=cache):
        risk = drv.check_symbol_risk("PTT")
    assert risk["verdict"] == "HARD_BLOCKED_XD_XR"
    assert risk["is_blocked"] is True


def test_fresh_cache_symbol_absent_blocks_borrow():
    """Fresh cache without the symbol no longer returns is_blocked False."""
    cache = _fresh(set50=[{"symbol": "CPALL", "available_qty": 100, "borrow_fee_pct": 3.5}])
    drv = _driver_with(cache)
    with patch.object(drv, "get_cached_data", return_value=cache):
        risk = drv.check_symbol_risk("ZZZZ")
    assert risk["is_blocked"] is True
    assert risk["verdict"] == "NOT_IN_POOL"
    assert risk["legacy_verdict"] == "UNVERIFIED_INVENTORY"
    assert risk["available_qty"] == 0


def test_fresh_cache_zero_inventory_is_out_of_stock():
    cache = _fresh(set100=[{"symbol": "DELTA", "available_qty": 0, "borrow_fee_pct": 3.5}])
    drv = _driver_with(cache)
    with patch.object(drv, "get_cached_data", return_value=cache):
        risk = drv.check_symbol_risk("DELTA")
    assert risk["is_blocked"] is True
    assert risk["verdict"] == "OUT_OF_STOCK"


def test_disk_cache_without_timestamp_is_treated_as_stale(tmp_path, monkeypatch):
    """A persisted cache carrying no timestamp cannot be trusted → stale."""
    cache_file = tmp_path / "krungsri_sbl_realtime.json"
    cache_file.write_text(json.dumps({
        "success": True,
        "set50_inventory": [{"symbol": "PTT", "available_qty": 5000, "borrow_fee_pct": 3.5}],
        "xd_xr_inventory": [],
    }), encoding="utf-8")
    monkeypatch.setattr(sbl_mod, "CACHE_FILE", str(cache_file))

    drv = KrungsriSBLRealtimeDriver()
    status = drv.cache_status()
    risk = drv.check_symbol_risk("PTT")
    assert status["state"] == "CACHE_STALE"
    assert risk["is_blocked"] is True
    assert risk["verdict"] == "CACHE_STALE"


def test_save_cache_to_disk_stamps_timestamp(tmp_path, monkeypatch):
    """Every snapshot written to disk carries a timestamp, and reloads as fresh."""
    cache_file = tmp_path / "krungsri_sbl_realtime.json"
    monkeypatch.setattr(sbl_mod, "CACHE_FILE", str(cache_file))

    drv = KrungsriSBLRealtimeDriver()
    drv._save_cache_to_disk({
        "success": True,
        "set50_inventory": [{"symbol": "PTT", "available_qty": 10, "borrow_fee_pct": 3.5}],
        "xd_xr_inventory": [],
    })
    stored = json.loads(cache_file.read_text(encoding="utf-8"))
    assert stored.get("timestamp")

    reloaded = KrungsriSBLRealtimeDriver()
    assert reloaded.cache_status()["state"] == "CACHE_FRESH"
    assert reloaded.check_symbol_risk("PTT")["verdict"] == "BORROW_ALLOWED"


@pytest.mark.asyncio
async def test_pool_read_path_returns_zero_without_raising_on_empty_cache():
    """/sbl pool path must render 0 instead of raising when there is no cache."""
    drv = _driver_with({})
    with patch.object(drv, "get_cached_data", return_value={}), \
         patch.object(settings, "SESSION_STATE_PATH", "/nonexistent/session_state.json"):
        avail = await drv.scrape_sbl_available_inventory("PTT")
    assert avail == 0


@pytest.mark.asyncio
async def test_pool_read_path_returns_zero_on_stale_cache_without_scraping():
    """Stale cache → 0 and no browser scrape (keeps the SSO session intact)."""
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    cache = _fresh(set50=[{"symbol": "PTT", "available_qty": 5000, "borrow_fee_pct": 3.5}])
    cache["timestamp"] = old
    drv = _driver_with(cache)
    with patch.object(drv, "get_cached_data", return_value=cache), \
         patch.object(drv, "scrape_full_pool", new_callable=AsyncMock) as m_scrape:
        avail = await drv.scrape_sbl_available_inventory("PTT")
    assert avail == 0
    m_scrape.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_sbl_borrow_blocked_by_stale_cache():
    """The borrow flow itself refuses to start on a stale snapshot."""
    old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    cache = _fresh(set50=[{"symbol": "PTT", "available_qty": 5000, "borrow_fee_pct": 3.5}])
    cache["timestamp"] = old
    drv = _driver_with(cache)
    with patch.object(drv, "get_cached_data", return_value=cache):
        res = await drv.execute_sbl_borrow("PTT", 100)
    assert res["success"] is False
    assert res["status"] == "BLOCKED_BY_STALE_CACHE"


# ───────────────────────────────────────────────────────────────────────────── #
# CDP zero-OTP sync (sbl_read.js) vs cloned browser fallback
# ───────────────────────────────────────────────────────────────────────────── #
@pytest.mark.asyncio
async def test_scrape_full_pool_cdp_success_persists_cache_without_browser(tmp_path, monkeypatch):
    """run_snippet success persists fresh stamped cache and never constructs a browser."""
    cache_file = tmp_path / "krungsri_sbl_realtime.json"
    monkeypatch.setattr(sbl_mod, "CACHE_FILE", str(cache_file))

    mock_cdp_data = {
        "broker": "kss",
        "app": "sbl_realtime",
        "url": "https://www.krungsrisecurities.com/sblweb/MainSBL.aspx",
        "account": "<REDACTED_KSS_ACCT>",
        "customer_name": "คุณอารยะ สวัสดิชัย",
        "purchasing_power": 750000.0,
        "counts": {
            "set50": 1,
            "set100": 1,
            "newsec": 0,
            "xd_xr_risks": 1,
            "active_borrows": 1,
            "total_unique_inventory": 2,
        },
        "inventory": [
            {"symbol": "PTT", "available_qty": 5000, "borrow_fee_pct": 3.5},
            {"symbol": "CPALL", "available_qty": 2000, "borrow_fee_pct": 3.5},
        ],
        "set50_inventory": [{"symbol": "PTT", "available_qty": 5000, "borrow_fee_pct": 3.5}],
        "set100_inventory": [{"symbol": "CPALL", "available_qty": 2000, "borrow_fee_pct": 3.5}],
        "newsec_inventory": [],
        "xd_xr_risks": [{"symbol": "DELTA", "book_date": "28/10/2026", "type": "XD"}],
        "active_borrow_transactions": [{"ref_no": "B12345", "symbol": "PTT", "quantity": 1000}],
    }

    drv = KrungsriSBLRealtimeDriver()
    with patch("scripts.broker_cdp.js_runner.run_snippet", new_callable=AsyncMock) as m_runner, \
         patch("app.brokers.krungsri.sbl_realtime_driver.async_playwright") as m_pw:
        m_runner.return_value = {"success": True, "data": mock_cdp_data, "tab_url": "https://sbl/MainSBL.aspx"}
        res = await drv.scrape_full_pool(headless=True, force_browser=False)

    assert res.get("success") is True
    assert res["source"] == "sbl_read_cdp"
    assert res["purchasing_power"] == 750000.0
    assert len(res["set50_inventory"]) == 1
    assert len(res["xd_xr_inventory"]) == 1
    assert res["xd_xr_inventory"][0]["symbol"] == "DELTA"

    # Crucial: Playwright was NEVER constructed
    m_pw.assert_not_called()
    m_runner.assert_awaited_once_with(
        broker="kss",
        snippet_name="sbl_read",
        target_kind="sbl",
        timeout_s=30.0,
    )

    # Cache on disk is stamped and fresh
    stored = json.loads(cache_file.read_text(encoding="utf-8"))
    assert stored.get("timestamp") is not None
    assert stored["source"] == "sbl_read_cdp"
    assert drv.cache_status()["state"] == "CACHE_FRESH"


@pytest.mark.asyncio
async def test_scrape_full_pool_cdp_failure_fails_closed_without_force_browser():
    """When CDP fails and force_browser=False, fail-closed without launching Playwright."""
    drv = KrungsriSBLRealtimeDriver()
    with patch("scripts.broker_cdp.js_runner.run_snippet", new_callable=AsyncMock) as m_runner, \
         patch("app.brokers.krungsri.sbl_realtime_driver.async_playwright") as m_pw:
        m_runner.return_value = {"success": False, "error": "Daemon connection refused"}
        res = await drv.scrape_full_pool(force_browser=False)

    assert res["success"] is False
    assert "CDP sync failed" in res["error"]
    m_pw.assert_not_called()


@pytest.mark.asyncio
async def test_scrape_full_pool_force_browser_bypasses_cdp():
    """force_browser=True bypasses CDP snippet and directly enters browser clone path."""
    drv = KrungsriSBLRealtimeDriver()
    with patch("scripts.broker_cdp.js_runner.run_snippet", new_callable=AsyncMock) as m_runner, \
         patch.object(settings, "SESSION_STATE_PATH", "/nonexistent/session_state.json"):
        res = await drv.scrape_full_pool(force_browser=True)

    m_runner.assert_not_called()
    assert res["success"] is False
    assert "Session file not found" in res["error"]
