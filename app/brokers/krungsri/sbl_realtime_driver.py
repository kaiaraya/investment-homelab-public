"""
Krungsri SBL Real Time Driver
=============================
Autonomous driver for Krungsri Securities SBL Real Time Portal (/sblweb/MainSBL.aspx).
Replaces the legacy TradeMan SBL scraping mechanism with the real Member SSO portal.

Capabilities:
1. Full On-Demand Scraping:
   - SET50, SET100, NEW SEC borrowable inventories (Volume, Fee %, Initial Margin %)
   - XD/XR Corporate Actions Calendar (Symbol, Book Date, Type e.g. XD/XR/XM)
   - Real-time Purchasing Power (PP)
   - Active Borrow Transactions Table
2. Risk Supervision & Hard Blocker:
   - Hard blocks any short selling or borrowing on symbols registered in XD/XR list
     to prevent 100% dividend compensation liabilities.
3. Thread-safe Caching in `data/krungsri_sbl_realtime.json`.
"""

import os
import re
import json
import logging
import asyncio
import time
from datetime import datetime, timezone, date, timedelta
from typing import Dict, Any, List, Optional, Tuple

from playwright.async_api import async_playwright, Page, BrowserContext

from app.core.config import settings

logger = logging.getLogger("Krungsri.SBLRealtime")

CACHE_FILE = os.path.join(settings.PROJECT_ROOT if hasattr(settings, "PROJECT_ROOT") else os.getcwd(), "data", "krungsri_sbl_realtime.json")
SBL_START_URL = "https://www.krungsrisecurities.com/th/SBL/start.aspx"

# ─── SBL cache staleness (fail-closed policy) ─────────────────────────────────
# Default freshness window = 24 hours ("1 trading day" in the conservative sense:
# anything older than yesterday's snapshot must not be trusted to permit a borrow).
# A long weekend therefore makes a Friday snapshot stale on Monday — that is
# intentional; the operator runs `/sbl sync` before borrowing.
# Override, in priority order: settings.SBL_CACHE_STALENESS_HOURS (attribute, for
# tests/integration), then env SBL_CACHE_STALENESS_HOURS, then 24h.
DEFAULT_STALENESS_HOURS = 24.0

# Cache keys that may carry the snapshot timestamp (legacy files first).
_CACHE_TIMESTAMP_KEYS = ("timestamp", "scraped_at", "cached_at")


def _staleness_seconds() -> float:
    """Freshness window in seconds (see DEFAULT_STALENESS_HOURS for the policy)."""
    raw = getattr(settings, "SBL_CACHE_STALENESS_HOURS", None)
    if raw is None:
        raw = os.getenv("SBL_CACHE_STALENESS_HOURS")
    try:
        hours = float(raw) if raw is not None else DEFAULT_STALENESS_HOURS
    except (TypeError, ValueError):
        hours = DEFAULT_STALENESS_HOURS
    if hours <= 0:
        hours = DEFAULT_STALENESS_HOURS
    return hours * 3600.0


def _cache_timestamp_of(data: Dict[str, Any]) -> Optional[Any]:
    for key in _CACHE_TIMESTAMP_KEYS:
        if data.get(key):
            return data.get(key)
    return None


def _parse_cache_timestamp(raw: Any) -> Optional[datetime]:
    """Best-effort parse of a cache timestamp; None when absent/unparseable."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
        if value <= 0:
            return None
        if value > 1e11:  # milliseconds since epoch
            value /= 1000.0
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(raw).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%d/%m/%Y"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        # Legacy writers stamped naive UTC (datetime.utcnow()); assume UTC.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_book_date(date_str: Any) -> Optional[date]:
    """Parse a SBL XD/XR calendar ``book_date`` cell into a ``date``.

    The calendar grid is DD/MM/YYYY (e.g. ``07/10/2026``); tolerate ``-`` as a
    separator. Returns ``None`` for anything unparseable — callers must treat
    ``None`` as fail-closed.
    """
    s = str(date_str or "").strip()
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _bkk_today() -> date:
    """Today's date in Bangkok (+07) — the SET calendar's home timezone."""
    return (datetime.now(timezone.utc) + timedelta(hours=7)).date()


def normalize_sbl_ref(raw: Any) -> str:
    """Normalize a broker Ref.No or DB lot_id (e.g. ``SBL-MINT-B494819`` -> ``B494819``)."""
    text = str(raw or "").strip().upper()
    if not text:
        return ""
    return text.split("-")[-1].strip() if "-" in text else text


def _tx_sort_key(lot: Dict[str, Any]) -> Tuple[Tuple[int, int, int, int, int, int], str]:
    """Chronological FIFO sort key `(Y, M, D, H, m, s), ref` from SBL lot/tx dict."""
    raw_d = lot.get("borrow_date") or lot.get("tran_date")
    d_tuple = (9999, 12, 31)
    if isinstance(raw_d, date) and not isinstance(raw_d, datetime):
        d_tuple = (raw_d.year, raw_d.month, raw_d.day)
    elif isinstance(raw_d, datetime):
        d_tuple = (raw_d.year, raw_d.month, raw_d.day)
    elif raw_d:
        s = str(raw_d).strip()
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y"):
            try:
                parsed = datetime.strptime(s[:10], fmt)
                d_tuple = (parsed.year, parsed.month, parsed.day)
                break
            except ValueError:
                continue

    raw_t = str(lot.get("time") or "").strip()
    t_tuple = (0, 0, 0)
    if raw_t:
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                pt = datetime.strptime(raw_t, fmt)
                t_tuple = (pt.hour, pt.minute, pt.second)
                break
            except ValueError:
                continue

    ref = normalize_sbl_ref(lot.get("broker_tx_id") or lot.get("ref_no") or lot.get("lot_id"))
    return (d_tuple[0], d_tuple[1], d_tuple[2], t_tuple[0], t_tuple[1], t_tuple[2]), ref


def _extract_lot_remaining(lot: Dict[str, Any]) -> int:
    if (lot.get("close_date") or "").strip():
        return 0
    st = str(lot.get("status") or "").strip().upper()
    if st in ("CLOSED", "FULLY_RETURNED", "EXPIRED", "CANCELLED"):
        return 0
    raw = lot.get("remaining_qty")
    if raw is None:
        raw = lot.get("quantity") or lot.get("borrow_qty") or 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def plan_fifo_return_lots(
    lots: List[Dict[str, Any]],
    *,
    symbol: str,
    return_qty: Optional[int] = None,
    lot_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Build FIFO slice allocation across open SBL contracts for ``symbol`` without requiring ``Ref.no``.

    - If ``lot_id`` is given (and isn't just the symbol name), restrict to that concrete contract.
    - If ``return_qty`` is ``None``, returns all active open contracts in FIFO order.
    - Otherwise allocates sequentially oldest-first until ``return_qty`` is fulfilled.
    """
    sym = str(symbol or "").strip().upper()
    if not sym:
        raise ValueError("EMPTY_SYMBOL")

    target_raw = str(lot_id or "").strip().upper()
    target_norm = normalize_sbl_ref(target_raw)
    explicit_lot = bool(target_raw and target_raw != sym and target_norm != sym)

    candidates: List[Dict[str, Any]] = []
    for lot in lots or []:
        if str(lot.get("symbol") or "").strip().upper() != sym:
            continue
        rem = _extract_lot_remaining(lot)
        if rem <= 0:
            continue
        b_tx = str(lot.get("broker_tx_id") or lot.get("ref_no") or "").strip()
        l_id = str(lot.get("lot_id") or "").strip()
        norm_b = normalize_sbl_ref(b_tx or l_id)
        if explicit_lot:
            if not (
                target_raw in {b_tx.upper(), l_id.upper(), norm_b}
                or (target_norm and target_norm == norm_b)
            ):
                continue
        candidates.append(lot)

    candidates.sort(key=_tx_sort_key)
    if not candidates:
        raise ValueError("NO_ACTIVE_SBL_LOT")

    total_rem = sum(_extract_lot_remaining(l) for l in candidates)
    if return_qty is None:
        need = total_rem
    else:
        need = int(return_qty)
        if need <= 0:
            raise ValueError("INVALID_RETURN_QTY")
        if need > total_rem:
            raise ValueError(f"INSUFFICIENT_SBL_LOT:{total_rem}:{need}")

    plan: List[Dict[str, Any]] = []
    rem_need = need
    for lot in candidates:
        if rem_need <= 0:
            break
        avail = _extract_lot_remaining(lot)
        take = avail if return_qty is None else min(avail, rem_need)
        if take <= 0:
            continue
        effective_id = str(lot.get("lot_id") or lot.get("broker_tx_id") or lot.get("ref_no") or sym).strip()
        ref_no = normalize_sbl_ref(lot.get("broker_tx_id") or lot.get("ref_no") or effective_id)
        plan.append({
            "lot_id": effective_id,
            "ref_no": ref_no,
            "symbol": sym,
            "qty": take,
            "available_qty": avail,
            "lot": lot,
        })
        rem_need -= take
    return plan


async def run_sbl_return_ui(
    page: Page,
    *,
    symbol: str = "",
    lot_id: str = "",
    return_qty: int = 0,
    dry_run: bool = False,
    remark: str = "",
    sbl_url: str = "https://www.krungsrisecurities.com/sblweb/MainSBL.aspx",
) -> Dict[str, Any]:
    """Execute SBL share return via real Playwright DOM + RadWindow interaction on ``MainSBL.aspx``."""
    sym = str(symbol or "").strip().upper()
    norm_ref = normalize_sbl_ref(lot_id)
    effective_ref = norm_ref if (norm_ref and norm_ref != sym) else ""
    remark_clean = str(remark or "").strip()

    trace: Dict[str, Any] = {
        "symbol": sym,
        "requested_ref": effective_ref or None,
        "requested_qty": int(return_qty or 0),
        "dry_run": bool(dry_run),
        "search_applied": False,
        "dialogs": [],
        "radalerts": [],
        "post_status": None,
        "post_snippet": "",
    }
    captured_dialogs: List[str] = []
    captured_posts: List[Tuple[int, str]] = []

    async def _on_dialog(dlg: Any) -> None:
        try:
            msg = str(dlg.message or "").strip()
            if msg:
                captured_dialogs.append(msg)
            await dlg.accept()
        except Exception:
            pass

    async def _on_response(resp: Any) -> None:
        try:
            url_l = str(resp.url or "").lower()
            if "return.aspx" in url_l and str(resp.request.method or "").upper() == "POST":
                body_txt = await resp.text()
                captured_posts.append((int(resp.status), body_txt))
        except Exception:
            pass

    page.on("dialog", _on_dialog)
    page.on("response", _on_response)

    async def _reset_search_filter() -> None:
        if not trace.get("search_applied"):
            return
        try:
            has_search = await page.query_selector("#ctl00_MainCenter_txtSearchSymbol")
            if not has_search:
                return
            await page.evaluate("""() => {
                const inp = document.querySelector('#ctl00_MainCenter_txtSearchSymbol');
                const btn = document.querySelector('#MainCenter_lbNext');
                const rad = typeof $find === 'function' ? $find('ctl00_MainCenter_txtSearchSymbol') : null;
                if (rad && typeof rad.set_value === 'function') {
                    rad.set_value('');
                } else if (inp) {
                    inp.value = '';
                }
                if (btn) btn.click();
            }""")
            try:
                await page.wait_for_load_state("networkidle", timeout=2500)
            except Exception:
                await asyncio.sleep(0.4)
        except Exception:
            pass

    try:
        # 1. Ensure MainSBL.aspx is loaded (never trigger login/OTP; fail closed on SSO timeout)
        cur_url = str(page.url or "").lower()
        has_table = await page.query_selector("#ctl00_MainCenter_rgTrans_ctl00")
        if "mainsbl.aspx" not in cur_url and not has_table:
            if any(k in cur_url for k in ("login", "otp", "twofa")):
                return {
                    "success": False,
                    "status": "SBL_SESSION_EXPIRED",
                    "error": "SBL session expired — please run /daily_readiness or unlock CDP first",
                    "trace": trace,
                }
            await page.goto(sbl_url, wait_until="domcontentloaded", timeout=12000)
            if any(k in str(page.url or "").lower() for k in ("login", "otp", "twofa")):
                return {
                    "success": False,
                    "status": "SBL_SESSION_EXPIRED",
                    "error": "Redirected to Krungsri login while opening MainSBL.aspx — OTP blocked by safety guard",
                    "trace": trace,
                }
            await page.wait_for_selector("#ctl00_MainCenter_rgTrans_ctl00", timeout=8000)

        async def _read_grid_rows() -> List[Dict[str, Any]]:
            return await page.evaluate("""() => {
                const clean = s => (s || '').replace(/\\u00a0/g, ' ').trim();
                const tbl = document.querySelector('#ctl00_MainCenter_rgTrans_ctl00');
                if (!tbl) return [];
                const out = [];
                for (const tr of tbl.querySelectorAll('tbody tr, tr.rgRow, tr.rgAltRow')) {
                    const cells = Array.from(tr.querySelectorAll('td')).map(c => clean(c.innerText));
                    if (cells.length < 9) continue;
                    const refNo = cells[0].toUpperCase();
                    if (!refNo || /no records|ลำดับ/i.test(refNo)) continue;
                    const btn = tr.querySelector("a[id*='RadReturn']") ||
                        Array.from(tr.querySelectorAll('a,button')).find(b => /คืน|return/i.test(b.textContent || '')) || null;
                    out.push({
                        ref_no: refNo,
                        tran_date: cells[1] || '',
                        time: cells[2] || '',
                        symbol: (cells[3] || '').toUpperCase(),
                        quantity: parseInt((cells[5] || '').replace(/,/g, ''), 10) || 0,
                        close_date: cells[9] || '',
                        has_return_button: Boolean(btn),
                        return_button_id: btn ? (btn.id || '') : '',
                    });
                }
                return out;
            }""")

        def _pick_contract(rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
            matched: List[Dict[str, Any]] = []
            for r in rows or []:
                if effective_ref and r.get("ref_no") == effective_ref:
                    matched.append(r)
                elif not effective_ref and sym and r.get("symbol") == sym:
                    matched.append(r)
            if not matched:
                return None
            open_rows = [r for r in matched if not (r.get("close_date") or "").strip()]
            open_rows.sort(key=_tx_sort_key)
            return open_rows[0] if open_rows else matched[0]

        rows = await _read_grid_rows()
        matched_row = _pick_contract(rows)

        # 2. Not visible in initial grid -> trigger Search for Symbol (#ctl00_MainCenter_txtSearchSymbol + #MainCenter_lbNext)
        if (not matched_row) and sym:
            search_present = await page.query_selector("#ctl00_MainCenter_txtSearchSymbol")
            if search_present:
                await page.evaluate(
                    """(symbolVal) => {
                        const inp = document.querySelector('#ctl00_MainCenter_txtSearchSymbol');
                        const cs = document.querySelector('#ctl00_MainCenter_txtSearchSymbol_ClientState');
                        const rad = typeof $find === 'function' ? $find('ctl00_MainCenter_txtSearchSymbol') : null;
                        if (rad && typeof rad.set_value === 'function') {
                            rad.set_value(symbolVal);
                        } else if (inp) {
                            inp.focus();
                            inp.value = symbolVal;
                            inp.dispatchEvent(new Event('input', { bubbles: true }));
                            inp.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                        if (cs) {
                            try {
                                const st = JSON.parse(cs.value || '{}');
                                st.validationText = symbolVal;
                                st.valueAsString = symbolVal;
                                st.lastSetTextBoxValue = symbolVal;
                                cs.value = JSON.stringify(st);
                            } catch (e) {}
                        }
                        const btn = document.querySelector('#MainCenter_lbNext');
                        if (btn) btn.click();
                    }""",
                    sym,
                )
                trace["search_applied"] = True
                try:
                    await page.wait_for_load_state("networkidle", timeout=3500)
                except Exception:
                    await asyncio.sleep(0.8)
                rows = await _read_grid_rows()
                matched_row = _pick_contract(rows)

        if not matched_row:
            return {
                "success": False,
                "status": "CONTRACT_NOT_FOUND",
                "error": f"ไม่พบสัญญา SBL ที่เปิดอยู่ของ {sym or effective_ref} บนหน้า MainSBL.aspx",
                "trace": trace,
            }

        if (matched_row.get("close_date") or "").strip():
            return {
                "success": True,
                "status": "ALREADY_RETURNED",
                "closed": True,
                "lot_id": matched_row["ref_no"],
                "symbol": matched_row["symbol"],
                "return_qty": 0,
                "contract": matched_row,
                "source": "sbl_return_cdp_ui",
                "trace": trace,
            }

        actual_qty = int(return_qty) if int(return_qty or 0) > 0 else int(matched_row.get("quantity") or 0)
        if actual_qty <= 0 or actual_qty > int(matched_row.get("quantity") or 0):
            return {
                "success": False,
                "status": "RETURN_QTY_EXCEEDS_CONTRACT",
                "lot_id": matched_row["ref_no"],
                "symbol": matched_row["symbol"],
                "error": f"จำนวนคืน ({actual_qty:,}) เกินยอดสัญญา {matched_row['ref_no']} ({matched_row.get('quantity', 0):,})",
                "trace": trace,
            }

        # 3. Click row's RadReturn button to open Return.aspx inside RadWindow UserListDialog
        # Mark any reused singleton RadWindow iframe so we never read its stale pre-click DOM.
        try:
            await page.evaluate("""() => {
                for (const ifr of document.querySelectorAll('iframe')) {
                    if ((ifr.name || '') === 'UserListDialog' || (ifr.src || '').toLowerCase().includes('return.aspx')) {
                        try {
                            if (ifr.contentWindow) ifr.contentWindow.__sblStaleFrame = true;
                        } catch (e) {}
                    }
                }
            }""")
        except Exception:
            pass

        btn_id = matched_row.get("return_button_id")
        if btn_id:
            await page.locator(f"#{btn_id}").click(timeout=4000)
        else:
            clicked = await page.evaluate(
                """(refTarget) => {
                    for (const tr of document.querySelectorAll('#ctl00_MainCenter_rgTrans_ctl00 tbody tr')) {
                        const td0 = tr.querySelector('td');
                        if (td0 && td0.innerText.replace(/\\u00a0/g, ' ').trim().toUpperCase() === refTarget) {
                            const btn = tr.querySelector("a[id*='RadReturn']") || tr.querySelector('a,button');
                            if (btn) { btn.click(); return true; }
                        }
                    }
                    return false;
                }""",
                matched_row["ref_no"],
            )
            if not clicked:
                return {
                    "success": False,
                    "status": "RETURN_BUTTON_MISSING",
                    "lot_id": matched_row["ref_no"],
                    "symbol": matched_row["symbol"],
                    "error": f"ไม่พบปุ่มคืนหุ้น (RadReturn) ในแถว {matched_row['ref_no']}",
                    "trace": trace,
                }

        async def _find_fresh_return_frame() -> Optional[Any]:
            candidate = None
            for fr in page.frames:
                if "return.aspx" not in str(fr.url or "").lower() or fr.is_detached():
                    continue
                candidate = fr
                try:
                    is_stale = await fr.evaluate("() => Boolean(window.__sblStaleFrame)")
                except Exception:
                    continue
                if not is_stale:
                    return fr
            return candidate

        # Locate Return.aspx RadWindow iframe (ignoring pre-click stale DOM during navigation)
        ret_frame = None
        for idx_wait in range(45):
            for fr in page.frames:
                if "return.aspx" not in str(fr.url or "").lower() or fr.is_detached():
                    continue
                try:
                    is_stale = await fr.evaluate("() => Boolean(window.__sblStaleFrame)")
                except Exception:
                    is_stale = True
                if not is_stale or idx_wait >= 35:
                    ret_frame = fr
                    break
            if ret_frame:
                break
            await asyncio.sleep(0.15)

        if ret_frame is None:
            return {
                "success": False,
                "status": "RETURN_MODAL_NOT_OPENED",
                "lot_id": matched_row["ref_no"],
                "symbol": matched_row["symbol"],
                "error": f"เปิดหน้าต่าง Return.aspx สำหรับ {matched_row['ref_no']} ไม่สำเร็จ",
                "trace": trace,
            }

        modal_meta: Dict[str, Any] = {}
        for attempt in range(5):
            try:
                try:
                    await ret_frame.wait_for_load_state("load", timeout=6000)
                except Exception:
                    pass
                await ret_frame.wait_for_selector("#txtReturnQty", timeout=6000)
                modal_meta = await ret_frame.evaluate("""() => ({
                    ref: (document.getElementById('txtRef')?.value || '').trim().toUpperCase(),
                    symbol: (document.getElementById('txtSymbol')?.value || '').trim().toUpperCase(),
                    balance_raw: (document.getElementById('txtBalance')?.value || '').trim(),
                    has_save: Boolean(document.getElementById('RadSave')),
                    has_cancel: Boolean(document.getElementById('RadCancel')),
                })""")
                if modal_meta.get("ref") and modal_meta["ref"] != matched_row["ref_no"] and attempt < 4:
                    await asyncio.sleep(0.25)
                    ret_frame = await _find_fresh_return_frame() or ret_frame
                    continue
                break
            except Exception as exc:
                err_l = str(exc).lower()
                if attempt < 4 and ("destroyed" in err_l or "detached" in err_l or "navigation" in err_l):
                    await asyncio.sleep(0.25)
                    refreshed = await _find_fresh_return_frame()
                    if refreshed is not None:
                        ret_frame = refreshed
                    continue
                raise
        trace["modal_meta"] = modal_meta
        bal_num = int(re.sub(r"[^\d]", "", modal_meta.get("balance_raw") or "") or 0)
        if bal_num > 0 and actual_qty > bal_num:
            if modal_meta.get("has_cancel"):
                try:
                    await ret_frame.locator("#RadCancel").click(timeout=2000)
                except Exception:
                    pass
            return {
                "success": False,
                "status": "NOT_RETURNABLE",
                "lot_id": matched_row["ref_no"],
                "symbol": matched_row["symbol"],
                "broker_message": f"จำนวนที่ต้องการคืน ({actual_qty:,}) มากกว่าจำนวนที่สามารถคืนได้ ({bal_num:,})",
                "error": f"จำนวนที่ต้องการคืน ({actual_qty:,}) มากกว่าจำนวนที่สามารถคืนได้ ({bal_num:,})",
                "trace": trace,
            }

        # 4. Fill txtReturnQty (via Telerik client API + DOM fallback) & optional txtRemark
        qty_state = await ret_frame.evaluate(
            """([qty, rem]) => {
                const radQty = typeof $find === 'function' ? $find('txtReturnQty') : null;
                if (radQty && typeof radQty.set_value === 'function') {
                    radQty.set_value(Number(qty));
                }
                const qInp = document.getElementById('txtReturnQty');
                if (qInp) {
                    if (!radQty || !String(qInp.value || '').trim()) {
                        qInp.focus();
                        qInp.value = Number(qty).toLocaleString('en-US');
                    }
                    qInp.dispatchEvent(new Event('input', { bubbles: true }));
                    qInp.dispatchEvent(new Event('change', { bubbles: true }));
                    qInp.blur();
                }
                const csQty = document.getElementById('txtReturnQty_ClientState');
                if (csQty && (!csQty.value || !csQty.value.includes(String(qty)))) {
                    try {
                        const st = JSON.parse(csQty.value || '{}');
                        st.enabled = true;
                        st.validationText = String(qty);
                        st.valueAsString = String(qty);
                        st.lastSetTextBoxValue = Number(qty).toLocaleString('en-US');
                        csQty.value = JSON.stringify(st);
                    } catch (e) {}
                }
                if (rem) {
                    const radRem = typeof $find === 'function' ? $find('txtRemark') : null;
                    if (radRem && typeof radRem.set_value === 'function') {
                        radRem.set_value(rem);
                    }
                    const rInp = document.getElementById('txtRemark');
                    if (rInp) {
                        if (!radRem || !rInp.value) rInp.value = rem;
                        rInp.dispatchEvent(new Event('input', { bubbles: true }));
                        rInp.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }
                return {
                    txtReturnQty: qInp ? qInp.value : '',
                    txtReturnQty_ClientState: csQty ? csQty.value : '',
                };
            }""",
            [actual_qty, remark_clean],
        )
        trace["filled_state"] = qty_state

        # 5. DRY-RUN stops cleanly by clicking Cancel without saving
        if dry_run:
            if modal_meta.get("has_cancel"):
                try:
                    await ret_frame.locator("#RadCancel").click(timeout=2000)
                except Exception:
                    pass
            return {
                "success": True,
                "status": "DRY_RUN_ARMED",
                "dry_run": True,
                "lot_id": matched_row["ref_no"],
                "symbol": matched_row["symbol"],
                "return_qty": actual_qty,
                "contract": matched_row,
                "source": "sbl_return_cdp_ui",
                "trace": trace,
                "message": f"[DRY-RUN] Contract {matched_row['ref_no']} ({matched_row['symbol']}) form filled via UI ({actual_qty:,} shares). Cancelled cleanly.",
            }

        # 6. LIVE Submit — click real #RadSave inside Return.aspx iframe and trace server outcome
        save_loc = ret_frame.locator("#RadSave")
        if await save_loc.count() == 0:
            return {
                "success": False,
                "status": "SAVE_BUTTON_MISSING",
                "lot_id": matched_row["ref_no"],
                "symbol": matched_row["symbol"],
                "error": "ไม่พบปุ่ม SAVE (#RadSave) ในหน้าต่าง Return.aspx",
                "trace": trace,
            }

        await save_loc.click(timeout=4000)

        async def _is_return_modal_closed() -> bool:
            if ret_frame.is_detached():
                return True
            try:
                return bool(await page.evaluate("""() => {
                    const ifr = Array.from(document.querySelectorAll('iframe')).find(
                        el => el.name === 'UserListDialog' || (el.src || '').toLowerCase().includes('return.aspx')
                    );
                    if (!ifr) return true;
                    const wrap = ifr.closest('[id*="RadWindowWrapper"], .RadWindow') || ifr;
                    const cs = window.getComputedStyle(wrap);
                    return cs.display === 'none' || cs.visibility === 'hidden' || ifr.offsetParent === null;
                }"""))
            except Exception:
                return False

        async def _dismiss_rad_dialogs(ctx_target: Any) -> List[str]:
            """Detect visible Telerik radalert messages and click their visible OK/Close buttons."""
            try:
                res = await ctx_target.evaluate("""() => {
                    const isVis = (el) => {
                        if (!el) return false;
                        const st = window.getComputedStyle(el);
                        if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
                        const r = el.getBoundingClientRect();
                        return r.width > 0 && r.height > 0;
                    };
                    const texts = [];
                    for (const el of document.querySelectorAll('.rwDialogText, .rwDialogMessage')) {
                        if (!isVis(el)) continue;
                        const t = (el.innerText || el.textContent || '').trim();
                        if (t) texts.push(t);
                    }
                    let clicked = 0;
                    const btns = document.querySelectorAll(
                        '.rwOkBtn, .rwPopupButton, .RadWindow .rwControlButtons a, [role="dialog"] button'
                    );
                    for (const btn of btns) {
                        if (!isVis(btn)) continue;
                        const label = (btn.innerText || btn.value || btn.title || '').trim().toLowerCase();
                        const isOkLike = btn.classList.contains('rwOkBtn')
                            || btn.classList.contains('rwPopupButton')
                            || /^(ok|ตกลง|ใช่|yes|close|ปิด)$/i.test(label);
                        if (isOkLike) {
                            btn.click();
                            clicked += 1;
                        }
                    }
                    return { texts, clicked };
                }""")
                if isinstance(res, dict):
                    if res.get("clicked"):
                        trace["ok_clicks"] = int(trace.get("ok_clicks") or 0) + int(res["clicked"])
                    return [str(t).strip() for t in (res.get("texts") or []) if str(t).strip()]
            except Exception:
                pass
            return []

        banner_hit = ""
        broker_err = ""
        frame_closed = False

        def _extract_post_signals(body_text: str) -> None:
            nonlocal banner_hit, broker_err
            m_alert = re.search(
                r"(?:radalert|alert)\(\s*['\"]([^'\"]{1,240})['\"]",
                body_text,
                re.I,
            )
            if m_alert:
                msg = m_alert.group(1).strip()
                if msg and msg not in trace["radalerts"]:
                    trace["radalerts"].append(msg)
                    if re.search(r"Save\s*Successfull|บันทึกข้อมูลสำเร็จ", msg, re.I):
                        banner_hit = banner_hit or msg
                    else:
                        broker_err = broker_err or msg
            if re.search(r"Save\s*Successfull|บันทึกข้อมูลสำเร็จ|CloseAndRebind\s*\(", body_text, re.I):
                banner_hit = banner_hit or "Save Successfull"
            elif not broker_err:
                m_err = re.search(
                    r"(ไม่สามารถ[^<\n'\"]{0,140}|เกิดข้อผิดพลาด[^<\n'\"]{0,140}|มากกว่าจำนวนที่สามารถคืนได้[^<\n'\"]{0,100})",
                    body_text,
                    re.I,
                )
                if m_err:
                    broker_err = m_err.group(1).strip()

        settle_ticks = 0
        for _ in range(36):
            await asyncio.sleep(0.2)
            # Check Telerik radalert DOM in iframe (if attached) AND parent page (never skip parent!)
            for ctx_frame in (ret_frame, page):
                if ctx_frame is ret_frame:
                    try:
                        if ret_frame.is_detached():
                            frame_closed = True
                            continue
                    except Exception:
                        frame_closed = True
                        continue
                rad_msgs = await _dismiss_rad_dialogs(ctx_frame)
                for rad_txt in rad_msgs:
                    if rad_txt not in trace["radalerts"]:
                        trace["radalerts"].append(rad_txt)
                    if re.search(r"Save\s*Successfull|บันทึกข้อมูลสำเร็จ", rad_txt, re.I):
                        banner_hit = banner_hit or rad_txt
                    else:
                        broker_err = broker_err or rad_txt

            # Inspect captured Return.aspx POST HTTP response text (including <script> tail past 4KB head)
            if captured_posts:
                st, full_body = captured_posts[-1]
                trace["post_status"] = st
                trace["post_snippet"] = (full_body[-1500:] if len(full_body) > 1500 else full_body)[:600]
                _extract_post_signals(full_body)
                if await _is_return_modal_closed():
                    frame_closed = True

            if broker_err:
                break
            if frame_closed or (banner_hit and captured_posts):
                if not frame_closed:
                    await _dismiss_rad_dialogs(page)
                    frame_closed = await _is_return_modal_closed()
                # Parent MainSBL.aspx often displays its RadAlert OK popup right AFTER Return.aspx closes.
                # Wait briefly (up to ~1.0s) until a parent .rwOkBtn or native dialog is clicked/seen.
                if settle_ticks < 5 and not trace.get("ok_clicks") and not captured_dialogs:
                    settle_ticks += 1
                    continue
                break

        if captured_dialogs:
            trace["dialogs"] = captured_dialogs
            last_dlg = captured_dialogs[-1]
            if re.search(r"Save\s*Successfull|บันทึกข้อมูลสำเร็จ", last_dlg, re.I):
                banner_hit = banner_hit or last_dlg
            elif not broker_err:
                broker_err = last_dlg

        # Dismiss modal if still lingering after rejection so grid stays clean
        if not frame_closed and not ret_frame.is_detached():
            try:
                if await ret_frame.locator("#RadCancel").count() > 0:
                    await ret_frame.locator("#RadCancel").click(timeout=1500)
            except Exception:
                pass

        trace["frame_closed"] = bool(frame_closed)
        if broker_err:
            not_ret = bool(re.search(r"มากกว่าจำนวนที่สามารถคืนได้|ไม่สามารถคืน", broker_err))
            return {
                "success": False,
                "status": "NOT_RETURNABLE" if not_ret else "RETURN_REJECTED",
                "lot_id": matched_row["ref_no"],
                "symbol": matched_row["symbol"],
                "return_qty": actual_qty,
                "broker_message": broker_err,
                "error": broker_err,
                "source": "sbl_return_cdp_ui",
                "trace": trace,
            }

        if not (frame_closed or banner_hit):
            return {
                "success": False,
                "status": "RETURN_REJECTED",
                "lot_id": matched_row["ref_no"],
                "symbol": matched_row["symbol"],
                "return_qty": actual_qty,
                "error": "ไม่ได้รับการยืนยัน Save Successfull หรือปิดหน้าต่าง Return.aspx หลังคลิก SAVE",
                "source": "sbl_return_cdp_ui",
                "trace": trace,
            }

        if not dry_run and not broker_err and (frame_closed or banner_hit):
            # Final sweep for any late parent RadAlert OK button before clean URL refresh
            await _dismiss_rad_dialogs(page)

        return {
            "success": True,
            "status": "RETURN_ACCEPTED",
            "lot_id": matched_row["ref_no"],
            "symbol": matched_row["symbol"],
            "return_qty": actual_qty,
            "banner": banner_hit,
            "source": "sbl_return_cdp_ui",
            "trace": trace,
        }
    finally:
        try:
            page.remove_listener("dialog", _on_dialog)
            page.remove_listener("response", _on_response)
        except Exception:
            pass
        await _reset_search_filter()
        if not dry_run and sbl_url and captured_posts:
            try:
                clean_sbl_url = sbl_url.split("?")[0] if "mainsbl.aspx" in sbl_url.lower() else sbl_url
                cur_lower = str(page.url or "").lower()
                if "mainsbl.aspx" in cur_lower and not any(k in cur_lower for k in ("login", "otp", "twofa")):
                    await page.goto(clean_sbl_url, wait_until="domcontentloaded", timeout=10000)
                    trace["url_refreshed"] = True
                    try:
                        await page.wait_for_selector("#ctl00_MainCenter_rgTrans_ctl00", timeout=5000)
                    except Exception:
                        pass
            except Exception as nav_exc:
                trace["url_refresh_error"] = str(nav_exc)[:120]


class KrungsriSBLRealtimeDriver:
    """Production driver for Krungsri Member SSO SBL Real Time application."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._cached_data: Optional[Dict[str, Any]] = None
        # True when the snapshot came from disk without any usable timestamp —
        # such a cache is treated as stale (fail-closed) rather than trusted.
        self._cache_stamp_missing = False
        self._needs_live_refresh = False
        self._load_cache_from_disk()

    def _load_cache_from_disk(self):
        if os.path.exists(CACHE_FILE):
            try:
                with open(CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._cached_data = data
                    self._cache_stamp_missing = _parse_cache_timestamp(_cache_timestamp_of(data)) is None
                    if self._cache_stamp_missing:
                        logger.warning(
                            f"SBL cache {CACHE_FILE} has no usable timestamp — treating it as stale (fail-closed)"
                        )
                else:
                    self._cached_data = None
                    self._cache_stamp_missing = False
            except Exception as e:
                logger.warning(f"Failed to read SBL cache from {CACHE_FILE}: {e}")

    def _save_cache_to_disk(self, data: Dict[str, Any]):
        try:
            # Every persisted snapshot must carry a timestamp, otherwise the next
            # process cannot tell fresh data from a stale file (and must block).
            if isinstance(data, dict) and _parse_cache_timestamp(_cache_timestamp_of(data)) is None:
                data["timestamp"] = datetime.now(timezone.utc).isoformat()
            self._cache_stamp_missing = False
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self._cached_data = data
        except Exception as e:
            logger.error(f"Failed to persist SBL cache: {e}")

    def get_cached_data(self) -> Optional[Dict[str, Any]]:
        if not self._cached_data:
            self._load_cache_from_disk()
        return self._cached_data

    def cache_status(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        """
        Classifies the SBL snapshot: ``CACHE_MISS`` | ``CACHE_STALE`` | ``CACHE_FRESH``.

        Also returns ``age_seconds``, the raw ``timestamp``, the active
        ``staleness_seconds`` window and a Thai operator ``message`` — the read
        path (`/sbl`, `/sbl pool <sym>`) renders that message instead of raising
        when there is nothing trustworthy to show.

        A snapshot written by this process (`_save_cache_to_disk`, which always
        stamps it) is trusted; a snapshot loaded from disk without a timestamp is
        stale; a disk snapshot older than the window is stale.
        """
        window = _staleness_seconds()
        status: Dict[str, Any] = {
            "state": "CACHE_MISS",
            "timestamp": None,
            "age_seconds": None,
            "staleness_seconds": window,
            "message": "ยังไม่มีข้อมูลคลังหุ้นยืม SBL — พิมพ์ /sbl sync ก่อน",
        }
        data = self.get_cached_data()
        if not isinstance(data, dict) or not data:
            return status

        raw_ts = _cache_timestamp_of(data)
        ts = _parse_cache_timestamp(raw_ts)
        if raw_ts is not None:
            status["timestamp"] = raw_ts

        if ts is None:
            if self._cache_stamp_missing:
                status.update(
                    state="CACHE_STALE",
                    message="ข้อมูลคลัง SBL ไม่มี timestamp จึงตรวจความสดไม่ได้ — พิมพ์ /sbl sync ก่อน",
                )
                return status
            status.update(state="CACHE_FRESH", message="ข้อมูลคลัง SBL สด (เขียนในโปรเซสนี้)")
            return status

        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        age = max(0.0, (current - ts).total_seconds())
        status["age_seconds"] = round(age, 1)
        if age > window:
            status.update(
                state="CACHE_STALE",
                message=(
                    f"ข้อมูลคลัง SBL เก่าเกิน {window / 3600:.0f} ชม. "
                    f"(อายุ {age / 3600:.1f} ชม.) — พิมพ์ /sbl sync ก่อน"
                ),
            )
            return status
        status.update(state="CACHE_FRESH", message="ข้อมูลคลัง SBL สด")
        return status

    def invalidate_cache(self):
        """Invalidates in-memory cache and marks the next lot read for live sync."""
        self._cached_data = None
        self._needs_live_refresh = True

    async def scrape_full_pool(self, headless: bool = True, force_browser: bool = False) -> Dict[str, Any]:
        """
        Executes an on-demand full scan of Krungsri SBL Real Time.
        Scrapes SET50, SET100, NEW SEC, XD/XR, Purchasing Power, and Active Transactions.

        Prefers JS snippet execution via active CDP session (zero-OTP).
        Playwright Chromium clone is an explicit fallback only (force_browser=True).
        """
        async with self._lock:
            # 1. Try JS-first via active CDP session (zero-OTP)
            if not force_browser:
                try:
                    from scripts.broker_cdp.js_runner import run_snippet
                    cdp_res = await run_snippet(
                        broker="kss",
                        snippet_name="sbl_read",
                        target_kind="sbl",
                        timeout_s=30.0,
                    )
                    if cdp_res.get("success") and cdp_res.get("data"):
                        data = cdp_res["data"]
                        if not data.get("error"):
                            xd_list = data.get("xd_xr_inventory") or data.get("xd_xr_risks") or []
                            set50 = data.get("set50_inventory") or []
                            set100 = data.get("set100_inventory") or []
                            newsec = data.get("newsec_inventory") or []
                            active_txs = data.get("active_borrow_transactions") or []
                            counts = dict(data.get("counts") or {})
                            counts.setdefault("set50", len(set50))
                            counts.setdefault("set100", len(set100))
                            counts.setdefault("newsec", len(newsec))
                            counts.setdefault("xd_xr_risks", len(xd_list))
                            counts.setdefault("active_borrows", len(active_txs))

                            result_data = {
                                "success": True,
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "source": "sbl_read_cdp",
                                "url": data.get("url") or cdp_res.get("tab_url") or SBL_START_URL,
                                "account_no": data.get("account") or getattr(settings, "TRADEMAN_ACCOUNT_NO_MARGIN", "302495"),
                                "account_name": data.get("customer_name") or "คุณอารยะ สวัสดิชัย",
                                "purchasing_power": float(data.get("purchasing_power", 0.0) or 0.0),
                                "counts": counts,
                                "inventory": data.get("inventory") or [],
                                "set50_inventory": set50,
                                "set100_inventory": set100,
                                "newsec_inventory": newsec,
                                "xd_xr_inventory": xd_list,
                                "active_borrow_transactions": active_txs,
                            }
                            self._save_cache_to_disk(result_data)
                            self._needs_live_refresh = False
                            logger.info(
                                f"✅ SBL Realtime sync complete (CDP zero-OTP): "
                                f"PP={result_data['purchasing_power']:,.2f} | "
                                f"SET50={len(set50)} | XD/XR Risks={len(xd_list)}"
                            )
                            return result_data
                        else:
                            logger.warning(f"SBL read snippet returned error: {data.get('error')}")
                    else:
                        logger.warning(f"SBL read snippet failed: {cdp_res.get('error')}")
                except Exception as e:
                    logger.warning(f"SBL sync via CDP failed, falling back: {e}")

                return {
                    "success": False,
                    "error": (
                        "SBL Real Time CDP sync failed (daemon down or SBL tab missing). "
                        "Pass force_browser=True to launch a dedicated Chromium fallback (risks SSO decay)."
                    ),
                    "cached_data": self._cached_data,
                }

            # 2. Cloned Playwright Chromium fallback (explicit force_browser=True)
            session_file = settings.SESSION_STATE_PATH
            if not os.path.exists(session_file):
                return {
                    "success": False,
                    "error": f"Session file not found at {session_file}. Run ./start_homelab.sh first."
                }

            playwright = await async_playwright().start()
            launch_args = {
                "headless": headless,
                "args": ["--no-sandbox", "--disable-dev-shm-usage", "--window-size=1280,900"]
            }
            if os.path.exists(settings.PLAYWRIGHT_CHROME_EXECUTABLE):
                launch_args["executable_path"] = settings.PLAYWRIGHT_CHROME_EXECUTABLE

            browser = await playwright.chromium.launch(**launch_args)
            context = await browser.new_context(
                storage_state=session_file,
                viewport={"width": 1280, "height": 900}
            )
            page = await context.new_page()

            try:
                logger.info(f"Navigating to Krungsri SBL: {SBL_START_URL}")
                await page.goto(SBL_START_URL, wait_until="networkidle", timeout=30000)

                # 1. Purchasing Power & Account Info
                body_text = await page.inner_text("body")
                pp_match = re.search(r"PP\s*=\s*([0-9,.]+)", body_text)
                purchasing_power = float(pp_match.group(1).replace(",", "")) if pp_match else 0.0

                cus_code = ""
                cus_name = ""
                try:
                    cus_code_el = await page.query_selector("#ctl00_MainCenter_txSearchCode, #ctl00_MainCenter_txtCusCode")
                    if cus_code_el:
                        cus_code = (await cus_code_el.input_value()).strip()
                    cus_name_el = await page.query_selector("#ctl00_MainCenter_lbCusName")
                    if cus_name_el:
                        cus_name = (await cus_name_el.input_value()).strip()
                except Exception:
                    pass

                # 2. Extract SET 50 (Default active tab)
                logger.info("Extracting SET50 inventory...")
                set50_inventory = await self._extract_inventory_table(page)

                # 3. Switch to SET 100
                logger.info("Extracting SET100 inventory...")
                await page.click("#MainCenter_lbSet100")
                await page.wait_for_timeout(1800)
                set100_inventory = await self._extract_inventory_table(page)

                # 4. Switch to NEW SEC
                logger.info("Extracting NEW SEC inventory...")
                await page.click("#MainCenter_lbNewSec")
                await page.wait_for_timeout(1800)
                newsec_inventory = await self._extract_inventory_table(page)

                # 5. Switch to XD, XR (CRITICAL RISK TAB)
                logger.info("Extracting XD/XR Corporate Actions Calendar...")
                await page.click("#MainCenter_lbXD")
                await page.wait_for_timeout(1800)
                xd_xr_calendar = await self._extract_xd_xr_table(page)

                # 6. Extract Active Borrow Transactions
                active_transactions = await self._extract_transactions_table(page)

                result_data = {
                    "success": True,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "source": "KRUNGSRI_SSO_SBL_REALTIME",
                    "url": page.url,
                    "account_no": cus_code or getattr(settings, "TRADEMAN_ACCOUNT_NO_MARGIN", "302495"),
                    "account_name": cus_name or "คุณอารยะ สวัสดิชัย",
                    "purchasing_power": purchasing_power,
                    "counts": {
                        "set50": len(set50_inventory),
                        "set100": len(set100_inventory),
                        "newsec": len(newsec_inventory),
                        "xd_xr_risks": len(xd_xr_calendar),
                        "active_borrows": len(active_transactions)
                    },
                    "set50_inventory": set50_inventory,
                    "set100_inventory": set100_inventory,
                    "newsec_inventory": newsec_inventory,
                    "xd_xr_inventory": xd_xr_calendar,
                    "active_borrow_transactions": active_transactions
                }

                self._save_cache_to_disk(result_data)
                logger.info(f"✅ SBL Realtime sync complete: PP={purchasing_power:,.2f} | SET50={len(set50_inventory)} | XD/XR Risks={len(xd_xr_calendar)}")
                return result_data

            except Exception as e:
                logger.error(f"Error during SBL Realtime scraping: {e}", exc_info=True)
                return {
                    "success": False,
                    "error": str(e),
                    "cached_data": self._cached_data
                }
            finally:
                await browser.close()
                await playwright.stop()

    async def _extract_inventory_table(self, page: Page) -> List[Dict[str, Any]]:
        """Parses the 4-column inventory sub-tables into structured items."""
        js_extract = """
        () => {
            const items = [];
            const seen = new Set();
            // In MainSBL.aspx, tables contain rows with Symbol, Fee, Volume, IM
            const tables = document.querySelectorAll('table');
            tables.forEach(table => {
                const rows = table.querySelectorAll('tr');
                rows.forEach(tr => {
                    const cells = Array.from(tr.querySelectorAll('td, th')).map(c => c.innerText.trim());
                    // Check if 4 cells: [Symbol, Fee, Volume, IM]
                    if (cells.length >= 4) {
                        const sym = cells[0].toUpperCase();
                        const feeStr = cells[1];
                        const volStr = cells[2].replace(/,/g, '');
                        const imStr = cells[3];
                        
                        // Validate symbol format (alphanumeric 2-10 chars, not header)
                        if (/^[A-Z0-9]{2,10}$/.test(sym) && !['SYMBOL', 'FEE', 'VOLUME', 'IM', 'SET50', 'SET100'].includes(sym)) {
                            if (!seen.has(sym)) {
                                seen.add(sym);
                                const fee = parseFloat(feeStr) || 3.5;
                                const vol = parseInt(volStr, 10) || 0;
                                const im = parseFloat(imStr) || 50.0;
                                items.push({
                                    symbol: sym,
                                    borrow_fee_pct: fee,
                                    available_qty: vol,
                                    initial_margin_pct: im
                                });
                            }
                        }
                    }
                });
            });
            return items;
        }
        """
        try:
            return await page.evaluate(js_extract)
        except Exception as e:
            logger.warning(f"Failed to evaluate inventory table: {e}")
            return []

    async def _extract_xd_xr_table(self, page: Page) -> List[Dict[str, Any]]:
        """Parses the XD/XR corporate actions calendar table."""
        js_extract = """
        () => {
            const items = [];
            const seen = new Set();
            const validCorporateActions = ['XD', 'XR', 'XM', 'XW', 'XB', 'XI', 'XA', 'XE'];
            const tables = document.querySelectorAll('table');
            tables.forEach(table => {
                // Ignore transactions table at bottom
                if (table.closest('#ctl00_MainCenter_rgTrans_ctl00') || table.id.includes('rgTrans')) return;
                const rows = table.querySelectorAll('tr');
                rows.forEach(tr => {
                    const cells = Array.from(tr.querySelectorAll('td, th')).map(c => c.innerText.trim());
                    // XD/XR format: [Symbol, Book Date, Type]
                    if (cells.length >= 3) {
                        const sym = cells[0].toUpperCase();
                        const dateStr = cells[1];
                        const typeStr = cells[2].toUpperCase();
                        
                        if (/^[A-Z0-9]{2,10}$/.test(sym) && !['SYMBOL', 'BOOK DATE', 'TYPE'].includes(sym) && validCorporateActions.includes(typeStr)) {
                            if (!seen.has(sym)) {
                                seen.add(sym);
                                items.push({
                                    symbol: sym,
                                    book_date: dateStr,
                                    type: typeStr,
                                    sign: typeStr
                                });
                            }
                        }
                    }
                });
            });
            return items;
        }
        """
        try:
            return await page.evaluate(js_extract)
        except Exception as e:
            logger.warning(f"Failed to evaluate XD/XR table: {e}")
            return []

    async def _extract_transactions_table(self, page: Page) -> List[Dict[str, Any]]:
        """Parses active borrowing contracts from the transactions table."""
        js_extract = """
        () => {
            const items = [];
            const headerTable = document.querySelector('#ctl00_MainCenter_rgTrans_ctl00_Header');
            const dataTable = document.querySelector('#ctl00_MainCenter_rgTrans_ctl00');
            if (!dataTable) return items;
            
            const rows = dataTable.querySelectorAll('tbody tr');
            rows.forEach(tr => {
                const cells = Array.from(tr.querySelectorAll('td')).map(c => c.innerText.trim());
                // Ref.No, Tran Date, Time, Symbol, Type, Quantity, Borrowing Price, Borrowing Value, Fee Rate, Close Date
                if (cells.length >= 9) {
                    const refNo = cells[0];
                    if (refNo && refNo !== 'No records to display.') {
                        items.push({
                            ref_no: refNo,
                            tran_date: cells[1],
                            time: cells[2],
                            symbol: cells[3].toUpperCase(),
                            type: cells[4],
                            quantity: parseInt(cells[5].replace(/,/g, ''), 10) || 0,
                            borrow_price: parseFloat(cells[6].replace(/,/g, '')) || 0.0,
                            borrow_value: parseFloat(cells[7].replace(/,/g, '')) || 0.0,
                            fee_rate: parseFloat(cells[8]) || 3.5,
                            close_date: cells[9] || ''
                        });
                    }
                }
            });
            return items;
        }
        """
        try:
            return await page.evaluate(js_extract)
        except Exception as e:
            logger.warning(f"Failed to extract transactions table: {e}")
            return []

    def check_symbol_risk(self, symbol: str) -> Dict[str, Any]:
        """
        Validates whether a symbol is safe to short/borrow. **Fail-closed.**

        Decision order
        --------------
        1. ``HARD_BLOCKED_XD_XR`` — symbol is on the XD/XR corporate-action
           calendar. Checked first (and on any age of snapshot) because a
           registered ex-dividend/ex-rights marker does not stop being true when
           the pool list ages; it is the 100% dividend-compensation trap.
        2. ``CACHE_MISS`` — there is no SBL snapshot at all (no cache file /
           empty cache). ``is_blocked: True``.
        3. ``CACHE_STALE`` — the snapshot is older than the freshness window
           (``DEFAULT_STALENESS_HOURS`` = **24h / one trading day**; see
           ``_staleness_seconds`` for overrides) or was loaded from disk without
           any usable timestamp. ``is_blocked: True``. Consequence: a Friday
           snapshot is stale on Monday — run ``/sbl sync`` before borrowing.
        4. Fresh snapshot, symbol in a pool with ``available_qty > 0`` (and no
           XD/XR marker, checked in step 1) → ``BORROW_ALLOWED``. This is the
           **only** path to ``is_blocked: False`` for a borrow.
        5. Fresh snapshot, symbol in a pool with ``available_qty == 0`` →
           ``OUT_OF_STOCK`` (blocked).
        6. Fresh snapshot, symbol absent from every pool → ``NOT_IN_POOL``
           (blocked). *Deliberate change* from the old ``UNVERIFIED_INVENTORY``
           ``is_blocked: False``: a fresh, complete snapshot that does not list
           the symbol is evidence there is no borrowable inventory to lend, so
           waving the borrow through was a fail-open hole. The operator can
           still borrow after a manual check on the broker portal; this function
           simply refuses to *vouch* for it. The old verdict name is preserved
           in ``legacy_verdict`` for log/UI continuity.

        The read path is unaffected: ``cache_status()`` carries a Thai operator
        message ("ยังไม่มีข้อมูล … /sbl sync") so `/sbl` and `/sbl pool <sym>`
        render a hint instead of raising.
        """
        sym = symbol.strip().upper()
        cache = self.get_cached_data() or {}
        status = self.cache_status()
        xd_list = cache.get("xd_xr_inventory", []) or []
        set50 = cache.get("set50_inventory", []) or []
        set100 = cache.get("set100_inventory", []) or []
        newsec = cache.get("newsec_inventory", []) or []

        def _result(blocked: bool, verdict: str, badge: str, reason: str, **extra) -> Dict[str, Any]:
            out = {
                "symbol": sym,
                "is_blocked": blocked,
                "verdict": verdict,
                "badge": badge,
                "reason": reason,
                "available_qty": 0,
                "borrow_fee_pct": 3.5,
                "cache_state": status.get("state"),
                "cache_age_seconds": status.get("age_seconds"),
            }
            out.update(extra)
            return out

        # 1. XD/XR hard block — but only while the corporate action is still
        #    pending (book_date is today or in the future). Once the ex-date has
        #    passed, the 100% dividend-compensation trap is gone and the XD block
        #    must be lifted, not linger forever on an ageing snapshot.
        xd_match = next((x for x in xd_list if isinstance(x, dict) and (x.get("symbol") or "").strip().upper() == sym), None)
        if xd_match:
            b_date = xd_match.get("book_date", "")
            x_type = xd_match.get("type", "XD")
            bd = _parse_book_date(b_date)
            if bd is None or bd >= _bkk_today():
                return _result(
                    True,
                    "HARD_BLOCKED_XD_XR",
                    f"⛔ บล็อกคำสั่ง (ติด {x_type} วันที่ {b_date})",
                    (
                        f"⛔ ห้ามเปิดสถานะขายชอร์ต (Short Sell) เด็ดขาด เนื่องจากหุ้น {sym} มีกำหนดขึ้นเครื่องหมาย {x_type} "
                        f"ในวันที่ {b_date} ซึ่งตามกฎเกณฑ์ SBL ผู้ยืมจะต้องจ่ายเงินชดเชยปันผลหรือสิทธิ์ 100% ให้แก่ผู้ให้ยืม"
                    ),
                    corporate_action=xd_match,
                )
            logger.info(f"SBL risk: {sym} {x_type} book_date {b_date} is in the past — XD block lifted")

        # 2./3. No trustworthy snapshot → fail closed.
        if status.get("state") != "CACHE_FRESH":
            verdict = "CACHE_MISS" if status.get("state") == "CACHE_MISS" else "CACHE_STALE"
            return _result(
                True,
                verdict,
                "⚪ ไม่มีข้อมูลคลัง SBL ที่เชื่อถือได้",
                (
                    f"⚠️ ยังไม่มีข้อมูลคลังหุ้นยืม SBL ที่สดพอจะยืนยันได้ว่าหุ้น {sym} ให้ยืมได้ "
                    f"({status.get('message')}) — ระบบบล็อกไว้ก่อนเพื่อความปลอดภัย (fail-closed)"
                ),
                cache_message=status.get("message"),
                operator_hint="พิมพ์ /sbl sync เพื่อรีเฟรชคลัง SBL แล้วลองใหม่",
            )

        # 4./5./6. Fresh snapshot: look the symbol up across every pool.
        all_inv = set50 + set100 + newsec
        inv_item = next(
            (i for i in all_inv if isinstance(i, dict) and (i.get("symbol") or "").strip().upper() == sym),
            None,
        )
        if inv_item:
            avail = inv_item.get("available_qty", 0) or 0
            fee = inv_item.get("borrow_fee_pct", 3.5)
            im = inv_item.get("initial_margin_pct", 50.0)
            if avail > 0:
                return _result(
                    False,
                    "BORROW_ALLOWED",
                    f"🟢 พร้อมยืม Short ({avail:,} หุ้น)",
                    f"มีคลังหุ้นพร้อมให้ยืม {avail:,} หุ้น ดอกเบี้ย {fee}% ปลอดภัยไม่มีเครื่องหมาย XD/XR",
                    available_qty=avail,
                    borrow_fee_pct=fee,
                    initial_margin_pct=im,
                )
            return _result(
                True,
                "OUT_OF_STOCK",
                "🔴 คลังหมด (Available = 0)",
                f"ไม่มีสต็อกหุ้น {sym} ให้ยืมในคลัง Krungsri SBL ณ ขณะนี้",
                borrow_fee_pct=fee,
                initial_margin_pct=im,
            )

        return _result(
            True,
            "NOT_IN_POOL",
            "⚪ ไม่อยู่ในคลังยืม (บล็อกไว้ก่อน)",
            (
                f"หุ้น {sym} ไม่ได้อยู่ในหมวด SET50/SET100/NEWSEC ของคลัง SBL ที่ดึงมาล่าสุด "
                f"จึงยังยืนยันไม่ได้ว่ามีหุ้นให้ยืม — ระบบบล็อกไว้ก่อน (fail-closed) "
                f"หากต้องการยืมจริงให้ตรวจหน้าต่างยืมหุ้นเดี่ยวของ Krungsri ก่อน"
            ),
            legacy_verdict="UNVERIFIED_INVENTORY",
            operator_hint="พิมพ์ /sbl sync เพื่อรีเฟรชคลัง SBL หรือตรวจสอบที่หน้าต่างยืมหุ้นเดี่ยว",
        )

    async def scrape_sbl_available_inventory(self, symbol: str) -> int:
        """
        Returns borrowable volume for a symbol from SBL Real Time cache or scrape.

        Read-safe: never raises, returns 0 when the snapshot is missing/stale and
        a refresh cannot be performed (the caller renders "ยังไม่มีข้อมูล").
        """
        sym = symbol.strip().upper()
        risk = self.check_symbol_risk(sym)

        # No snapshot at all → one best-effort refresh (only when a saved session
        # exists), then re-evaluate. Deliberately NOT triggered for a merely stale
        # snapshot: repeatedly poking the SBL portal from a read-only command
        # (`/sbl pool`) decays the SSO session — the operator refreshes explicitly
        # with `/sbl sync` instead. Never raises.
        if risk.get("verdict") == "CACHE_MISS":
            try:
                if os.path.exists(settings.SESSION_STATE_PATH):
                    res = await self.scrape_full_pool(headless=True)
                    if res.get("success"):
                        risk = self.check_symbol_risk(sym)
            except Exception as e:
                logger.warning(f"Live SBL inventory refresh failed: {e}")

        if risk.get("is_blocked"):
            if risk.get("verdict") == "CACHE_STALE":
                logger.warning(
                    f"SBL pool query for {sym} answered 0 — snapshot stale, operator must run /sbl sync"
                )
            return 0
        return risk.get("available_qty", 0) or 0

    async def scrape_sbl_lots(self, force_refresh: bool = False, force_browser: bool = False) -> List[Dict[str, Any]]:
        """Returns active borrow transactions from SBL Real Time."""
        cache = self.get_cached_data()
        needs_sync = force_refresh or getattr(self, "_needs_live_refresh", False)
        if needs_sync or not cache or not cache.get("active_borrow_transactions"):
            res = await self.scrape_full_pool(headless=True, force_browser=force_browser)
            if res.get("success"):
                cache = res
                self._needs_live_refresh = False
        txs = (cache or {}).get("active_borrow_transactions", [])
        lots = []
        for tx in txs:
            close_date = (tx.get("close_date") or "").strip()
            qty = int(tx.get("quantity", 0) or 0)
            # A row that carries a close date is already returned/expired on the broker
            # side; reporting its full quantity as "remaining" made the card offer a
            # return for a closed contract (MINT B494819 after the 06/11 return).
            lots.append({
                "broker_tx_id": tx.get("ref_no"),
                "symbol": tx.get("symbol"),
                "borrow_qty": qty,
                "remaining_qty": 0 if close_date else qty,
                "status": "CLOSED" if close_date else "ACTIVE",
                "close_date": close_date,
                "has_return_button": bool(tx.get("has_return_button")),
                "borrow_price": tx.get("borrow_price", 0.0),
                "borrow_fee_rate": tx.get("fee_rate", 3.5),
                "borrow_date": tx.get("tran_date", ""),
                "time": tx.get("time", ""),
            })
        return lots

    async def execute_sbl_borrow(self, symbol: str, volume: int) -> Dict[str, Any]:
        """
        Executes a borrow order via SBL Real Time (/sblweb/MainSBL.aspx).
        Respects DRY_RUN_MODE and enforces XD/XR hard block.
        """
        sym = symbol.strip().upper()
        risk = self.check_symbol_risk(sym)
        if risk.get("is_blocked") and risk.get("verdict") == "HARD_BLOCKED_XD_XR":
            return {
                "success": False,
                "status": "BLOCKED_BY_XD_XR",
                "error": risk.get("reason"),
            }
        # Fail-closed (2.11b): never start a borrow flow on a snapshot we cannot
        # trust — an empty/stale pool cache used to wave the borrow through.
        if risk.get("verdict") in ("CACHE_MISS", "CACHE_STALE"):
            return {
                "success": False,
                "status": "BLOCKED_BY_STALE_CACHE",
                "error": risk.get("reason") or "ข้อมูลคลัง SBL ยังไม่สด — พิมพ์ /sbl sync ก่อนยืมหุ้น",
            }

        # 1. Try JS-first via active CDP session
        try:
            from scripts.broker_cdp.js_runner import run_snippet
            cdp_res = await run_snippet(
                broker="kss",
                snippet_name="sbl_borrow",
                target_kind="sbl",
                arg={
                    "symbol": sym,
                    "volume": volume,
                    "dry_run": bool(settings.DRY_RUN_MODE),
                },
                timeout_s=30.0,
            )
            if cdp_res.get("success") and cdp_res.get("data"):
                data = cdp_res["data"]
                if data.get("success"):
                    return {
                        "success": True,
                        "status": data.get("status", "SBL_BORROW_SUBMITTED"),
                        "symbol": sym,
                        "volume": volume,
                        "dry_run": data.get("dry_run", bool(settings.DRY_RUN_MODE)),
                        "purchasing_power": data.get("purchasing_power"),
                        "source": "sbl_borrow_js",
                    }
                elif data.get("status") == "BLOCKED_BY_XD_XR":
                    return {
                        "success": False,
                        "status": "BLOCKED_BY_XD_XR",
                        "error": data.get("error"),
                        "source": "sbl_borrow_js",
                    }
        except Exception as e:
            logger.warning(f"SBL borrow JS execution failed, falling back: {e}")

        if settings.DRY_RUN_MODE:
            logger.info(f"[DRY-RUN] SBL Realtime borrow: {sym} {volume:,} shares")
            return {
                "success": True,
                "status": "SBL_BORROW_DRY_RUN",
                "symbol": sym,
                "volume": volume,
                "transaction_id": f"SBL-RT-{sym}-{int(time.time())}",
                "account_no": getattr(settings, "TRADEMAN_ACCOUNT_NO_MARGIN", "<REDACTED_KSS_ACCT>"),
                "fee_rate": risk.get("borrow_fee_pct", 3.50),
            }

        # Fail-closed: live SBL borrow via Web DOM is not implemented yet.
        # Fabricating a SUBMITTED success on a live path would violate the
        # "no fabricated live success" guardrail and risk capital.
        return {
            "success": False,
            "status": "LIVE_SBL_NOT_IMPLEMENTED",
            "error": "Live SBL via Web DOM is disabled/not implemented — fail-closed to protect capital",
        }

    async def verify_return_submitted(self, *, lot_id: str, symbol: Optional[str],
                                      return_qty: int) -> Tuple[bool, Dict[str, Any]]:
        """``(verified, proof)`` — re-read the live borrow ledger after a submit attempt.

        A return only counts once the broker's own grid agrees: the ref either left the
        active table (full return) or its remaining dropped by ``return_qty`` (partial).
        HTTP 200 is not evidence — the page returns it for rejected requests too.
        """
        ref = normalize_sbl_ref(lot_id)
        sym_clean = str(symbol or "").strip().upper()
        # Never treat a missing ref or bare ticker symbol as a vanished lot — fail closed.
        if not ref or (sym_clean and ref == sym_clean):
            return False, {"error": "missing concrete broker ref_no", "ref": ref, "symbol": sym_clean}

        proof: Dict[str, Any] = {"ref": ref}
        for attempt, pause in enumerate((3.0, 2.5), start=1):
            if pause:
                await asyncio.sleep(pause)
            try:
                lots = await self.scrape_sbl_lots(force_refresh=True)
            except Exception as e:  # noqa: BLE001 — treat as unverified, never as success
                proof = {"error": f"ledger re-read failed: {e}", "ref": ref, "attempt": attempt}
                continue
            for lot in lots or []:
                lot_ref = normalize_sbl_ref(lot.get("broker_tx_id") or lot.get("ref_no") or lot.get("lot_id"))
                if lot_ref == ref:
                    remain = int(lot.get("remaining_qty") or 0)
                    borrowed = int(lot.get("borrow_qty") or 0)
                    proof = {
                        "ref": ref, "remaining": remain, "borrow_qty": borrowed,
                        "reduced": borrowed - remain, "status": lot.get("status"),
                        "close_date": lot.get("close_date"), "attempt": attempt,
                    }
                    if borrowed - remain >= return_qty:
                        return True, proof
                    break
            else:
                return True, {"ref": ref, "gone": True, "lots_after": len(lots or []),
                              "attempt": attempt}
        return False, proof

    async def _return_via_cdp_ui(
        self,
        *,
        lot_id: str,
        return_qty: int,
        symbol: Optional[str] = None,
        dry_run: bool = False,
        remark: str = "",
    ) -> Dict[str, Any]:
        """Connect to active KSS CDP browser (:9333) and execute return via real UI."""
        from scripts.broker_cdp.client import cdp_url_for, connect_cdp, resolve_target_tab

        pw, browser, ctx = await connect_cdp(cdp_url_for("kss"), "kss")
        try:
            sbl_page = next((pg for pg in ctx.pages if "mainsbl.aspx" in str(pg.url or "").lower()), None)
            if not sbl_page:
                sbl_page = await resolve_target_tab(ctx, "sbl", "kss")
            if not sbl_page:
                sbl_page = ctx.pages[0] if ctx.pages else None
            if not sbl_page:
                return {"success": False, "status": "NO_SBL_TAB", "error": "No KSS browser tab available on CDP :9333"}
            await sbl_page.bring_to_front()
            return await run_sbl_return_ui(
                sbl_page,
                symbol=symbol or "",
                lot_id=lot_id,
                return_qty=return_qty,
                dry_run=dry_run,
                remark=remark,
            )
        finally:
            try:
                await browser.close()
                await pw.stop()
            except Exception:
                pass

    async def return_sbl_shares(
        self,
        lot_id: str,
        return_qty: int,
        symbol: Optional[str] = None,
        remark: str = "",
    ) -> Dict[str, Any]:
        """
        Submits SBL share return request via Krungsri SBL Real Time UI over CDP.
        Respects DRY_RUN_MODE and records diagnostics trace automatically.
        """
        is_dry = bool(settings.DRY_RUN_MODE)
        try:
            data = await self._return_via_cdp_ui(
                lot_id=lot_id,
                return_qty=return_qty,
                symbol=symbol,
                dry_run=is_dry,
                remark=remark,
            )
            trace = data.get("trace") or {}
            logger.info(
                "SBL UI return outcome: sym=%s lot=%s qty=%s dry=%s status=%s trace=%s",
                symbol, data.get("lot_id") or lot_id, return_qty, is_dry, data.get("status"), trace,
            )
            if data.get("success"):
                actual_ref = str(data.get("lot_id") or lot_id).strip()
                actual_sym = symbol or data.get("symbol")
                actual_qty = int(data.get("return_qty") or return_qty)
                if data.get("dry_run") or data.get("status") in ("DRY_RUN_ARMED", "SBL_RETURN_DRY_RUN", "ALREADY_RETURNED"):
                    return {
                        "success": True,
                        "status": data.get("status", "DRY_RUN_ARMED"),
                        "lot_id": actual_ref,
                        "symbol": actual_sym,
                        "return_qty": actual_qty if data.get("status") != "ALREADY_RETURNED" else 0,
                        "dry_run": bool(data.get("dry_run", is_dry)),
                        "source": data.get("source", "sbl_return_cdp_ui"),
                        "verified": True,
                        "proof": data,
                        "trace": trace,
                    }

                ok, proof = await self.verify_return_submitted(
                    lot_id=actual_ref, symbol=actual_sym, return_qty=actual_qty
                )
                if not ok:
                    if isinstance(proof, dict) and trace:
                        proof["trace"] = trace
                    hint = data.get("broker_message") or data.get("error") or (
                        "ส่งคำขอแล้วแต่ยังไม่เห็นยอดคืนบนหน้า SBL — ยังไม่ถือว่าสำเร็จ"
                    )
                    return {
                        "success": False,
                        "status": "SBL_RETURN_UNVERIFIED",
                        "error": hint,
                        "lot_id": actual_ref,
                        "symbol": actual_sym,
                        "return_qty": actual_qty,
                        "source": data.get("source", "sbl_return_cdp_ui"),
                        "proof": proof,
                        "trace": trace,
                    }
                self.invalidate_cache()
                return {
                    "success": True,
                    "status": data.get("status", "SBL_RETURN_SUBMITTED"),
                    "lot_id": actual_ref,
                    "symbol": actual_sym,
                    "return_qty": actual_qty,
                    "dry_run": False,
                    "source": data.get("source", "sbl_return_cdp_ui"),
                    "verified": True,
                    "proof": proof,
                    "trace": trace,
                }
            if data.get("status"):
                return {
                    "success": False,
                    "status": data["status"],
                    "error": (data.get("error") or data.get("broker_message")
                              or data.get("message") or "SBL page refused the return"),
                    "lot_id": data.get("lot_id") or lot_id,
                    "symbol": symbol or data.get("symbol"),
                    "return_qty": return_qty,
                    "broker_message": data.get("broker_message"),
                    "source": data.get("source", "sbl_return_cdp_ui"),
                    "proof": data,
                    "trace": trace,
                }
        except ConnectionError as e:
            logger.warning(f"SBL CDP unavailable: {e}")
            if is_dry:
                logger.info(f"[DRY-RUN] SBL Realtime return (CDP offline fallback): {symbol or lot_id} {return_qty:,} shares")
                return {
                    "success": True,
                    "status": "SBL_RETURN_DRY_RUN",
                    "symbol": symbol,
                    "return_qty": return_qty,
                    "lot_id": lot_id,
                    "dry_run": True,
                    "source": "dry_run_offline",
                }
            return {
                "success": False,
                "status": "SBL_CDP_UNAVAILABLE",
                "error": f"เชื่อมต่อ KSS CDP (:9333) ไม่สำเร็จ: {e}",
                "lot_id": lot_id,
                "symbol": symbol,
            }
        except Exception as e:
            logger.warning(f"SBL return UI execution failed: {e}", exc_info=True)
            if is_dry:
                logger.info(f"[DRY-RUN] SBL Realtime return fallback after error: {symbol or lot_id} {return_qty:,}")
                return {
                    "success": True,
                    "status": "SBL_RETURN_DRY_RUN",
                    "symbol": symbol,
                    "return_qty": return_qty,
                    "lot_id": lot_id,
                    "dry_run": True,
                    "source": "dry_run_fallback",
                }
            return {
                "success": False,
                "status": "SBL_RETURN_FAILED",
                "error": f"ส่งคำขอคืนหุ้นผ่าน UI ไม่สำเร็จ: {e}",
                "lot_id": lot_id,
                "symbol": symbol,
            }

        return {
            "success": False,
            "status": "SBL_RETURN_FAILED",
            "error": "SBL return UI returned empty outcome",
            "lot_id": lot_id,
            "symbol": symbol,
        }

    async def return_sbl_shares_for_symbol(
        self,
        *,
        symbol: str,
        lots: List[Dict[str, Any]],
        return_qty: Optional[int] = None,
        lot_id: Optional[str] = None,
        remark: str = "",
    ) -> Dict[str, Any]:
        """Execute SBL share return in FIFO order across 1..N open contracts for ``symbol``."""
        clean_sym = str(symbol or "").strip().upper()
        try:
            plan = plan_fifo_return_lots(lots, symbol=clean_sym, return_qty=return_qty, lot_id=lot_id)
        except ValueError as exc:
            code = str(exc)
            return {
                "success": False,
                "status": code.split(":", 1)[0],
                "error": f"ไม่พบสัญญา SBL ที่เพียงพอสำหรับคืน {clean_sym}: {code}",
                "symbol": clean_sym,
            }

        completed: List[Dict[str, Any]] = []
        total_done = 0
        any_dry = False
        traces: List[Dict[str, Any]] = []
        for idx, item in enumerate(plan):
            call_kw: Dict[str, Any] = {
                "lot_id": item["lot_id"],
                "return_qty": item["qty"],
                "symbol": clean_sym,
            }
            if remark:
                call_kw["remark"] = remark
            res = await self.return_sbl_shares(**call_kw)
            if isinstance(res, dict) and res.get("trace"):
                traces.append(res["trace"])
            ok = bool(res.get("success")) or res.get("status") in ("SUBMITTED", "SBL_RETURN_DRY_RUN", "DRY_RUN_ARMED")
            if not ok:
                err_msg = res.get("error") or "ส่งคำขอคืนหุ้นไม่สำเร็จ"
                if completed:
                    done_refs = ", ".join(f"{c['ref_no']}({c['qty']:,})" for c in completed)
                    err_msg = f"คืนแล้วบางส่วน [{done_refs}] แต่สัญญา {item['ref_no']} ล้มเหลว: {err_msg}"
                return {
                    "success": False,
                    "status": res.get("status") or "SBL_RETURN_FAILED",
                    "error": err_msg,
                    "symbol": clean_sym,
                    "lot_id": item["lot_id"],
                    "return_qty": total_done,
                    "completed_slices": completed,
                    "completed_qty": total_done,
                    "failed_slice": item,
                    "broker_message": res.get("broker_message"),
                    "proof": res.get("proof"),
                    "trace": traces or res.get("trace"),
                }
            any_dry = any_dry or bool(res.get("dry_run")) or res.get("status") in ("SBL_RETURN_DRY_RUN", "DRY_RUN_ARMED")
            slice_qty = int(res.get("return_qty") if res.get("return_qty") is not None else item["qty"])
            total_done += slice_qty
            completed.append({
                "lot_id": res.get("lot_id") or item["lot_id"],
                "ref_no": normalize_sbl_ref(res.get("lot_id") or item["ref_no"]),
                "qty": slice_qty,
                "status": res.get("status"),
            })
            if idx < len(plan) - 1:
                await asyncio.sleep(0.3)

        self.invalidate_cache()
        first_ref = completed[0]["ref_no"] if completed else (lot_id or clean_sym)
        last_status = completed[-1]["status"] if completed else "SBL_RETURN_SUBMITTED"
        return {
            "success": True,
            "status": last_status,
            "symbol": clean_sym,
            "lot_id": first_ref,
            "return_qty": total_done,
            "lots_returned": completed,
            "dry_run": any_dry,
            "verified": True,
            "trace": traces,
        }


krungsri_sbl_driver = KrungsriSBLRealtimeDriver()

