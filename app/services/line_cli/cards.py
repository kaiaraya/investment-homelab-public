"""
LINE Flex card builders for the slash-command CLI.

Pure functions: ``dict`` in -> LINE message ``dict`` out. No I/O, no broker
calls — so the exact layout can be unit-tested. Layouts follow the approved
mockups in ``docs/handoff/TASK-line-cli-design.md`` §3 and reuse the dark
theme already used by ``line_service.build_trade_approval_card``.
"""

from typing import Any, Dict, List, Optional

HEADER_BG = "#0f172a"
BODY_BG = "#1e293b"
FG = "#f8fafc"
MUTED = "#94a3b8"
LIVE_COLOR = "#DC2626"
TEST_COLOR = "#2563EB"
KILL_COLOR = "#dc2626"
UP = "#4ade80"
DOWN = "#f87171"
WARN = "#facc15"


# --------------------------------------------------------------------------- #
# Small building blocks
# --------------------------------------------------------------------------- #
def quick_reply(labels: List[str]) -> Dict[str, Any]:
    """Message-action quick reply buttons (label == text == command)."""
    return {
        "items": [
            {"type": "action", "action": {"type": "message", "label": lbl, "text": lbl}}
            for lbl in labels
        ]
    }


def _badge(text: str, bg: str, color: str = "#ffffff") -> Dict[str, Any]:
    return {
        "type": "box", "layout": "vertical", "backgroundColor": bg,
        "cornerRadius": "4px", "paddingStart": "8px", "paddingEnd": "8px",
        "paddingTop": "2px", "paddingBottom": "2px",
        "contents": [{"type": "text", "text": text, "color": color, "weight": "bold", "size": "xxs", "align": "center"}],
    }


def _kv(label: str, value: str, value_color: str = FG, flex_label: int = 3, flex_value: int = 5) -> Dict[str, Any]:
    return {
        "type": "box", "layout": "horizontal", "margin": "xs",
        "contents": [
            {"type": "text", "text": label, "color": MUTED, "size": "sm", "flex": flex_label},
            {"type": "text", "text": value, "color": value_color, "size": "sm", "weight": "bold",
             "flex": flex_value, "align": "end", "wrap": True},
        ],
    }


def _row(cells: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"type": "box", "layout": "horizontal", "margin": "xs", "contents": cells}


def _cell(text: str, color: str = FG, flex: int = 1, align: str = "start",
          weight: str = "regular", wrap: bool = False, size: str = "xs") -> Dict[str, Any]:
    cell: Dict[str, Any] = {"type": "text", "text": text, "color": color, "size": size,
                            "flex": flex, "align": align, "weight": weight}
    if wrap:
        cell["wrap"] = True          # RCA-012: let a long symbol wrap instead of clip
    return cell


def _money(value: Optional[float]) -> str:
    """Format a THB amount — ``None`` renders ``—`` (Anti-Mock, RCA-013).

    A totals cell the page did not expose is never printed as a fabricated ``0.00``.
    """
    return "—" if value is None else f"{value:,.2f}"


def _footer(buttons: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {"type": "box", "layout": "horizontal", "spacing": "sm", "contents": buttons}


def _msg_button(label: str, text: str, color: str = "#334155") -> Dict[str, Any]:
    return {
        "type": "button", "style": "primary", "color": color, "height": "sm", "flex": 1,
        "action": {"type": "message", "label": label, "text": text},
    }


def _miniapp_uri(tab: str, params: Optional[Dict[str, Any]] = None) -> Optional[str]:
    from app.core.config import settings
    from urllib.parse import urlencode

    liff_id = (getattr(settings, "LIFF_ID", None) or "").strip()
    if not liff_id:
        return None
    base = (getattr(settings, "MINIAPP_PUBLIC_BASE_URL", None) or "https://miniapp.line.me").rstrip("/")
    query: Dict[str, Any] = {"tab": tab}
    if params:
        for k, v in params.items():
            if v is not None and str(v).strip() != "":
                query[k] = v
    return f"{base}/{liff_id}?{urlencode(query)}"


def _uri_button(label: str, uri: str, color: str = "#0284c7", style: str = "primary") -> Dict[str, Any]:
    return {
        "type": "button",
        "style": style,
        "color": color,
        "height": "sm",
        "flex": 1,
        "action": {"type": "uri", "label": label, "uri": uri},
    }


def _bubble(
    *,
    title: str,
    badge: Optional[str] = None,
    badge_bg: str = "#334155",
    body: List[Dict[str, Any]],
    footer: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    header_contents: List[Dict[str, Any]] = [
        {"type": "box", "layout": "horizontal", "justifyContent": "space-between", "alignItems": "center",
         "contents": [
             {"type": "text", "text": title, "color": FG, "weight": "bold", "size": "md", "wrap": True, "flex": 4},
             *( [_badge(badge, badge_bg)] if badge else [] ),
         ]},
    ]
    bubble: Dict[str, Any] = {
        "type": "bubble", "size": "mega",
        "header": {"type": "box", "layout": "vertical", "backgroundColor": HEADER_BG, "paddingAll": "14px", "contents": header_contents},
        "body": {"type": "box", "layout": "vertical", "backgroundColor": BODY_BG, "paddingAll": "14px", "contents": body},
    }
    if footer:
        if isinstance(footer, dict):
            bubble["footer"] = footer
        else:
            bubble["footer"] = {"type": "box", "layout": "horizontal", "spacing": "sm", "backgroundColor": BODY_BG,
                                "paddingAll": "10px", "contents": footer}
    return bubble


def _flex(alt: str, contents: Dict[str, Any], quick: Optional[List[str]] = None, size: Optional[str] = None) -> Dict[str, Any]:
    msg: Dict[str, Any] = {"type": "flex", "altText": alt, "contents": contents}
    if size:
        msg["size"] = size
    if quick:
        msg["quickReply"] = quick_reply(quick)
    return msg


# --------------------------------------------------------------------------- #
# /status
# --------------------------------------------------------------------------- #
def status_card(
    *,
    brokers: List[Dict[str, str]],
    relay_ready: bool,
    relay_label: str,
    mode_badge: str,
    kill_active: bool,
    updated: str,
) -> Dict[str, Any]:
    """
    ``brokers`` = [{"name": "KSS", "emoji": "🟢", "line1": "alive · heartbeat 10:41",
    "line2": "บัญชี MARGIN"}]
    """
    is_live = "LIVE" in mode_badge
    badge_bg = KILL_COLOR if kill_active else (LIVE_COLOR if is_live else TEST_COLOR)

    body: List[Dict[str, Any]] = []
    if kill_active:
        body.append({"type": "text", "text": "⛔ KILL-SWITCH ACTIVE — พิมพ์ RESET เพื่อปลด",
                     "color": "#ffffff", "weight": "bold", "size": "sm", "wrap": True,
                     "backgroundColor": KILL_COLOR, "paddingAll": "8px", "cornerRadius": "4px"})
    body.append(_kv("Kill-switch", "🟢 ปกติ" if not kill_active else "🔴 ACTIVE",
                    DOWN if kill_active else UP))
    body.append(_kv("OTP relay", f"{'🟢' if relay_ready else '🔴'} {relay_label}",
                    UP if relay_ready else DOWN))
    for b in brokers:
        body.append({"type": "separator", "margin": "md", "color": "#334155"})
        body.append(_row([
            _cell(f"{b.get('emoji', '⚪')} {b.get('name', '')}", FG, flex=3, weight="bold"),
            _cell(b.get("line1", ""), MUTED, flex=3, align="end"),
        ]))
        if b.get("line2"):
            body.append(_cell(b["line2"], MUTED))
        if b.get("line3"):
            body.append(_cell(b["line3"], MUTED))
    body.append({"type": "separator", "margin": "md", "color": "#334155"})
    body.append(_cell(f"อัปเดต {updated}", MUTED, align="end"))

    # footer: offer /login for any broker that is down
    buttons: List[Dict[str, Any]] = []
    for b in brokers:
        if b.get("emoji") in ("🔴", "⚪", "🟡"):
            buttons.append(_msg_button(f"🔑 /login {b.get('key', '')}", f"/login {b.get('key', '')}", color="#b45309"))
    buttons.append(_msg_button("📊 /port", "/port", color="#0ea5e9"))
    uri = _miniapp_uri("brokers")
    if uri and len(buttons) < 3:
        buttons.append(_uri_button("🏦 MiniApp", uri, color="#3b82f6"))

    alt_states = " ".join(f"{b.get('name', '')}{b.get('emoji', '')}" for b in brokers)
    alt = f"Homelab: {alt_states} · relay{'🟢' if relay_ready else '🔴'} · {'LIVE' if is_live else 'TEST'}"
    return _flex(alt, _bubble(title="🛰️ Homelab Status", badge=mode_badge.split(" ")[0], badge_bg=badge_bg,
                              body=body, footer=buttons[:3]), quick=["/port", "/orders", "/status"])


# --------------------------------------------------------------------------- #
# /port
# --------------------------------------------------------------------------- #
PORT_ROWS_PER_BUBBLE = 12
_BROKER_LABEL = {"kss": "KSS", "lib": "LIB"}


def _port_header_row() -> Dict[str, Any]:
    """RCA-012: the symbol column keeps the largest share so ``DOHOME (S)`` is not clipped."""
    return _row([
        _cell("หุ้น", MUTED, flex=4, weight="bold"), _cell("จำนวน", MUTED, flex=2, align="end"),
        _cell("ทุน", MUTED, flex=2, align="end"), _cell("ล่าสุด", MUTED, flex=2, align="end"),
        _cell("%P/L", MUTED, flex=2, align="end"),
    ])


def _port_row(r: Dict[str, Any], broker: Optional[str] = None, actionable: bool = True) -> Dict[str, Any]:
    """One holding line with optional broker badge and optional actionable negotiation buttons."""
    ptype = str(r.get("position_type") or "LONG").upper()
    tag = " (S)" if ptype == "SHORT" else (" (B)" if ptype == "BORROWED" else "")
    pnl_pct = r.get("pnl_pct")
    if pnl_pct is None:
        pnl_cell = _cell("—", MUTED, flex=2, align="end")
    else:
        pc = float(pnl_pct)
        pnl_cell = _cell(f"{pc:+.2f}%", UP if pc >= 0 else DOWN, flex=2, align="end")

    sym = str(r.get("symbol", "")).upper()
    vol = int(r.get("volume", 0) or 0)
    borrow_qty = int(r.get("borrow_qty", 0) or 0)
    returnable = int(r.get("returnable_qty", 0) or 0)
    state = _position_state(ptype, vol, borrow_qty, returnable)
    if state == "COVERED_AWAIT_RETURN":
        # A covered loan has 0 shares but is still open — say so instead of printing
        # "0" with a P/L the human would read as a flat position.
        tag = " (S)✅"
    has_explicit_broker = bool(r.get("broker"))
    b_code = str(r.get("broker") or broker or ("kss" if ptype in ("SHORT", "BORROWED") else "lib")).lower()
    broker_badge = _BROKER_LABEL.get(b_code, b_code.upper())
    sym_label = f"[{broker_badge}] {sym}{tag}" if has_explicit_broker else f"{sym}{tag}"

    if state in ("COVERED_AWAIT_RETURN", "BORROW_ONLY"):
        qty_cell = _cell("รอคืน" if state == "COVERED_AWAIT_RETURN" else "ยืม", MUTED, flex=2, align="end")
        pnl_cell = _cell(f"คืนได้ {returnable:,}", FG, flex=3, align="end")
        row_box = _row([
            _cell(sym_label, FG, flex=4, weight="bold", wrap=True),
            qty_cell,
            pnl_cell,
        ])
        if not actionable:
            return row_box
        buttons = _position_buttons(ptype, b_code, sym, vol,
                                    borrow_qty=borrow_qty, returnable_qty=returnable)
        if not buttons:
            return row_box
        return {
            "type": "box", "layout": "vertical", "margin": "md", "spacing": "xs",
            "contents": [row_box, {"type": "box", "layout": "horizontal",
                                   "spacing": "sm", "margin": "xs", "contents": buttons}],
        }

    row_box = _row([
        _cell(sym_label, FG, flex=4, weight="bold", wrap=True),
        _cell(f"{vol:,}", FG, flex=2, align="end"),
        _cell(f"{float(r.get('cost', 0.0)):.2f}", FG, flex=2, align="end"),
        _cell(f"{float(r.get('last', 0.0)):.2f}", FG, flex=2, align="end"),
        pnl_cell,
    ])
    if not actionable:
        return row_box

    buttons: List[Dict[str, Any]] = _position_buttons(
        ptype, b_code, sym, vol,
        borrow_qty=int(r.get("borrow_qty", 0) or 0),
        returnable_qty=int(r.get("returnable_qty", 0) or 0),
    )
    if not buttons:
        return row_box

    return {
        "type": "box",
        "layout": "vertical",
        "margin": "md",
        "spacing": "xs",
        "contents": [
            row_box,
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "margin": "xs",
                "contents": buttons,
            },
        ],
    }


def _borrowed_row(b: Dict[str, Any], broker: Optional[str] = None, actionable: bool = True) -> Dict[str, Any]:
    """One borrowed SBL lot with broker badge and Short + Return buttons."""
    sym = str(b.get("symbol", "")).upper()
    vol = int(b.get("volume", 0) or 0)
    has_explicit_broker = bool(b.get("broker"))
    b_code = str(b.get("broker") or broker or "kss").lower()
    broker_badge = _BROKER_LABEL.get(b_code, b_code.upper())
    sym_label = f"[{broker_badge}] {sym}" if has_explicit_broker else sym

    if not actionable:
        return _kv(sym_label, f"{vol:,} หุ้น")

    qty = int(b.get("returnable_qty", vol) or 0) or vol
    loan = int(b.get("borrow_qty", vol) or 0) or vol
    buttons = [
        _msg_button("🟣 Short (ต่อราคา)", f"/short {b_code} {sym} {qty}", color="#581c87"),
        _return_button(sym, qty, loan),
    ]
    return {
        "type": "box",
        "layout": "vertical",
        "margin": "sm",
        "spacing": "xs",
        "contents": [
            _kv(sym_label, f"{loan:,} หุ้น · คืนได้ {qty:,}"),
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "margin": "xs",
                "contents": buttons,
            },
        ],
    }


def _position_state(ptype: str, volume: int, borrow_qty: int, returnable_qty: int) -> str:
    """Which SBL action a row is waiting for.

    ``OPEN_SHORT`` sold short and still holds the loan · ``COVERED_AWAIT_RETURN``
    bought back, loan still open (the MINT 2,400 case) · ``BORROW_ONLY`` shares in
    hand, nothing sold · ``LONG`` plain holding · ``CLOSED`` nothing to do.
    """
    if ptype == "BORROWED":
        # A broker-reported loan row can arrive before the ledger learns about it, so
        # the share count is evidence too — never hide the Short/Return buttons then.
        return "BORROW_ONLY" if (volume or borrow_qty or returnable_qty) else "CLOSED"
    if ptype == "SHORT":
        if volume > 0:
            return "OPEN_SHORT"
        return "COVERED_AWAIT_RETURN" if borrow_qty > 0 else "CLOSED"
    return "LONG"


def _return_button(symbol: str, quantity: int, borrow_qty: int) -> Dict[str, Any]:
    """Return button — full amount goes out as ``all``, a partial one names the qty."""
    arg = "all" if quantity >= borrow_qty else str(max(quantity, 0))
    return _msg_button("🔄 คืนหุ้น", f"/return {symbol} {arg}", color="#334155")


def _position_buttons(ptype: str, b_code: str, symbol: str, volume: int,
                      borrow_qty: int = 0, returnable_qty: int = 0,
                      lots: int = 0) -> List[Dict[str, Any]]:
    """Action buttons for one per-stock card, by position state.

    ``OPEN_SHORT`` → Cover (+ Return once shares are back in hand) ·
    ``COVERED_AWAIT_RETURN`` → Return only · ``BORROW_ONLY`` → Short + Return ·
    ``LONG`` → Sell on LIB only. Buttons are ``ต่อราคา`` (a limit price is resolved
    later, never a market order). Actions that cannot work are omitted, not greyed.
    """
    state = _position_state(ptype, volume, borrow_qty, returnable_qty)
    if state == "OPEN_SHORT":
        buttons = [_msg_button("🔵 Cover (ต่อราคา)", f"/cover {b_code} {symbol} {volume}", color="#1e40af")]
        if returnable_qty > 0:
            buttons.append(_return_button(symbol, returnable_qty, borrow_qty))
        return buttons
    if state == "COVERED_AWAIT_RETURN":
        return [_return_button(symbol, returnable_qty, borrow_qty)]
    if state == "BORROW_ONLY":
        qty = returnable_qty or borrow_qty or volume
        return [
            _msg_button("🟣 Short (ต่อราคา)", f"/short {b_code} {symbol} {qty}", color="#581c87"),
            _return_button(symbol, qty, borrow_qty),
        ]
    if b_code == "lib":
        return [_msg_button("🔴 ขาย (ต่อราคา)", f"/sell {b_code} {symbol} {volume}", color="#991b1b")]
    return []


def _stock_bubble(row: Dict[str, Any], is_test: bool = False) -> Dict[str, Any]:
    """One holding card — a single bubble per position with its own buttons.

    Title carries ``[KSS]``/``[LIB]`` + side tag ``(S)``/``(B)``. BORROWED lots show
    only จำนวน — the page exposes no cost/last/P-L basis, so never fabricate a
    ``0.00`` (Anti-Mock, same as ``_borrowed_row``).
    """
    ptype = str(row.get("position_type") or "LONG").upper()
    tag = " (S)" if ptype == "SHORT" else (" (B)" if ptype == "BORROWED" else "")
    sym = str(row.get("symbol", "")).upper()
    vol = int(row.get("volume", 0) or 0)
    borrow_qty = int(row.get("borrow_qty", 0) or 0)
    returnable = int(row.get("returnable_qty", 0) or 0)
    lots = int(row.get("borrow_lots", 0) or 0)
    state = _position_state(ptype, vol, borrow_qty, returnable)
    b_code = str(row.get("broker") or ("kss" if ptype in ("SHORT", "BORROWED") else "lib")).lower()
    badge = _BROKER_LABEL.get(b_code, b_code.upper())

    if state == "COVERED_AWAIT_RETURN":
        body: List[Dict[str, Any]] = [_kv("สถานะ", "✅ Cover ครบแล้ว — รอคืนหุ้น", UP)]
        if row.get("last") is not None:
            body.append(_kv("ล่าสุด", _money(row.get("last"))))
    elif state == "BORROW_ONLY":
        body = [_kv("สถานะ", "🟣 ยืมแล้ว ยังไม่ได้ Short", MUTED)]
        if row.get("last") is not None:
            body.append(_kv("ล่าสุด", _money(row.get("last"))))
    else:
        body = [_kv("จำนวน", f"{vol:,} หุ้น")]
    if ptype == "BORROWED":
        body.append(_kv("ประเภท", "หุ้นยืม SBL", MUTED))
    elif state != "COVERED_AWAIT_RETURN":
        body.append(_kv("ทุน", _money(row.get("cost"))))
        body.append(_kv("ล่าสุด", _money(row.get("last"))))
        pnl_pct = row.get("pnl_pct")
        if pnl_pct is None:
            body.append(_kv("%P/L", "—", MUTED))
        else:
            pc = float(pnl_pct)
            body.append(_kv("%P/L", f"{pc:+.2f}%", UP if pc >= 0 else DOWN))
    if borrow_qty > 0:
        loan = f"{borrow_qty:,} หุ้น" + (f" ({lots} lot)" if lots > 1 else "")
        body.append(_kv("ยืมค้าง", loan))
        if state == "COVERED_AWAIT_RETURN":
            body.append(_kv("คืนได้", f"{returnable:,} หุ้น", FG))
        elif state == "OPEN_SHORT":
            body.append(_kv("คืนได้", f"{returnable:,} หุ้น" if returnable else "ต้อง Cover ก่อน",
                            FG if returnable else MUTED))

    footer = _position_buttons(ptype, b_code, sym, vol,
                               borrow_qty=borrow_qty, returnable_qty=returnable, lots=lots)
    return _bubble(title=f"📊 [{badge}] {sym}{tag}",
                   badge="TEST" if is_test else None, badge_bg=TEST_COLOR,
                   body=body, footer=footer)


def portfolio_summary_card(*, brokers_data: Optional[List[Dict[str, Any]]] = None,
                           is_test: bool = False) -> Optional[Dict[str, Any]]:
    """Lead bubble for ``/port all sync`` — per-broker Amount + P/L totals, one
    broker per line (KSS block then LIB), so the totals survive the per-stock split."""
    body: List[Dict[str, Any]] = []
    for bdata in brokers_data or []:
        b_name = str(bdata.get("broker", "kss")).lower()
        if not (bool(bdata.get("actionable", True)) and b_name in ("kss", "lib")):
            continue
        badge = _BROKER_LABEL.get(b_name, b_name.upper())
        pnl = bdata.get("pnl_value")
        pnl_pct = bdata.get("pnl_pct")
        if pnl is None:
            pnl_txt = "—"
        else:
            pnl_txt = f"{float(pnl):+,.2f}"
        if pnl_pct is not None:
            pnl_txt += f" ({float(pnl_pct):+.2f}%)"
        body.append(_kv(f"[{badge}] Amount", _money(bdata.get("amount"))))
        body.append(_kv(f"[{badge}] P/L", pnl_txt, UP if (pnl or 0.0) >= 0 else DOWN))
    if not body:
        return None
    bubble = _bubble(title="📊 พอร์ตรวม", badge="TEST" if is_test else None,
                     badge_bg=TEST_COLOR, body=body)
    return _flex("📊 พอร์ตรวม", bubble)


#: LINE carousel cap — 12 bubbles per Flex carousel message.
STOCK_CARDS_PER_CAROUSEL = 12


def portfolio_stock_cards(
    *,
    brokers_data: Optional[List[Dict[str, Any]]] = None,
    is_test: bool = False,
    per_carousel: int = STOCK_CARDS_PER_CAROUSEL,
) -> List[Dict[str, Any]]:
    """Per-stock cards for ``/port all sync`` — every position its own bubble.

    Iterates ``brokers_data`` in order (KSS block first, then LIB). Each main
    holding (LONG/SHORT/UNKNOWN) and each borrowed SBL lot becomes one bubble.
    Returns a LIST of Flex messages (>``per_carousel`` bubbles split across
    carousels); empty list = nothing to show (caller pushes the unavailable card).
    """
    stocks: List[Dict[str, Any]] = []
    for bdata in brokers_data or []:
        b_name = str(bdata.get("broker", "kss")).lower()
        if not (bool(bdata.get("actionable", True)) and b_name in ("kss", "lib")):
            continue
        for r in (bdata.get("rows") or []):
            item = dict(r)
            item.setdefault("broker", item.get("broker") or b_name)
            stocks.append(item)
        for b in (bdata.get("borrowed") or []):
            stocks.append({
                "symbol": str(b.get("symbol", "")).upper(),
                "volume": int(b.get("volume", 0) or 0),
                "borrow_qty": int(b.get("borrow_qty", 0) or 0),
                "returnable_qty": int(b.get("returnable_qty", b.get("volume", 0)) or 0),
                "borrow_lots": int(b.get("lots", 0) or 0),
                "position_type": "BORROWED",
                "broker": str(b.get("broker") or b_name).lower(),
                "last": b.get("last"),
                "cost": b.get("cost"),
                "pnl_pct": b.get("pnl_pct"),
            })

    bubbles = [_stock_bubble(r, is_test=is_test) for r in stocks]
    messages: List[Dict[str, Any]] = []
    for i in range(0, len(bubbles), per_carousel):
        page = bubbles[i:i + per_carousel]
        alt = f"📊 พอร์ต {len(stocks)} การ์ด (หน้า {i // per_carousel + 1})"
        messages.append(_flex(alt, page[0]) if len(page) == 1 else carousel(page, alt))
    return messages


def port_card(
    *,
    broker: str,
    account: str,
    amount: Optional[float],
    pnl_value: float,
    pnl_pct: float,
    rows: List[Dict[str, Any]],
    snapshot_label: str,
    stale: bool,
    live: bool,
    borrowed: Optional[List[Dict[str, Any]]] = None,
    is_test: bool = False,
    note: Optional[str] = None,
    rows_per_bubble: int = PORT_ROWS_PER_BUBBLE,
    actionable: bool = True,
) -> Dict[str, Any]:
    """``rows`` = normalised holdings **minus the (B) loans** (``handlers._split_borrowed``).

    RCA-013 final: the header mirrors the app's own labels — ``Amount`` (cost basis) then
    ``Unrealized P/L``. ``Market Value`` is gone (D1): the app counts (B) in it while the
    page's totals row does not, and the loan is returned at its own volume anyway.
    ``borrowed`` (``[{"symbol", "volume"}]``) renders as a ``หุ้นยืมคงเหลือ (B)`` section
    under the P/L line — omitted entirely when empty (D2/D3). A figure the page/data did
    not provide renders ``—``, never a fabricated ``0.00``.

    A long book is paginated ``PORT_ROWS_PER_BUBBLE`` rows per bubble into a LINE
    carousel (RCA-002 follow-up) — every row is shown, no "และอีก N ตัว" teaser.
    """
    pnl_color = UP if pnl_value >= 0 else DOWN
    rows = rows or []
    loans = borrowed or []
    summary: List[Dict[str, Any]] = [
        _kv("Amount", _money(amount)),
        _kv("Unrealized P/L", f"{pnl_value:+,.2f} ({pnl_pct:+.2f}%)", pnl_color),
    ]
    if loans:
        summary.append(_cell("หุ้นยืมคงเหลือ (B)", MUTED, weight="bold"))
        for b in loans:
            # One renderer for a loan everywhere: it shows หุ้นที่ยืม vs คืนได้ and the
            # Short/Return buttons, so this card and the per-stock cards never disagree.
            summary.append(_borrowed_row(b, broker=broker, actionable=actionable))
    summary.append({"type": "separator", "margin": "md", "color": "#334155"})
    if note:
        # t_496c6213: an account-selection problem must be visible on the card, not a
        # silent empty portfolio.
        summary.append(_cell(note, WARN))
    header = _port_header_row()
    badge = "TEST" if is_test else None
    footer = [_msg_button(f"🔄 /port {broker} sync", f"/port {broker} sync", color="#0ea5e9"),
              _msg_button(f"📋 /orders {broker}", f"/orders {broker}")]
    mini_port = _miniapp_uri("portfolio")
    if mini_port:
        footer.append(_uri_button("📱 MiniApp", mini_port, color="#3b82f6"))
    ts_color = WARN if stale else (UP if live else MUTED)
    prefix = "🟢 live " if live else "🕒 snapshot "
    status = _cell(f"{prefix}{snapshot_label}" + ("  ⚠️ ข้อมูลเก่า" if stale else ""), ts_color)

    pages = [rows[i:i + rows_per_bubble] for i in range(0, len(rows), rows_per_bubble)] or [[]]
    bubbles: List[Dict[str, Any]] = []
    for idx, page in enumerate(pages):
        if idx == 0:
            body: List[Dict[str, Any]] = list(summary) + [header]
            if not rows:
                body.append(_cell("ℹ️ ยังไม่มีหุ้นในพอร์ต", MUTED))
            for r in page:
                pbox = _port_row(r, broker=r.get("broker") or broker, actionable=actionable)
                body.append(pbox)
            body.append({"type": "separator", "margin": "md", "color": "#334155"})
            body.append(status)
            bubbles.append(_bubble(title=f"📊 {broker.upper()} · {account}", badge=badge,
                                   badge_bg=TEST_COLOR, body=body, footer=footer))
        else:
            body = [header]
            for r in page:
                pbox = _port_row(r, broker=r.get("broker") or broker, actionable=actionable)
                body.append(pbox)
            bubbles.append(_bubble(title=f"📊 {broker.upper()} · ต่อ {idx + 1}/{len(pages)}", body=body))

    alt = f"📊 {broker} พอร์ต Amount {_money(amount)} ({len(rows)} ตัว)"
    if len(bubbles) == 1:
        return _flex(alt, bubbles[0])
    return carousel(bubbles, alt)


def portfolio_multi_broker_card(
    *,
    brokers_data: Optional[List[Dict[str, Any]]] = None,
    rows: Optional[List[Dict[str, Any]]] = None,
    borrowed: Optional[List[Dict[str, Any]]] = None,
    amount: Optional[float] = None,
    pnl_value: float = 0.0,
    pnl_pct: float = 0.0,
    snapshot_label: str = "",
    stale: bool = False,
    live: bool = False,
    is_test: bool = False,
    rows_per_bubble: int = PORT_ROWS_PER_BUBBLE,
) -> Dict[str, Any]:
    """Interactive multi-broker portfolio card (LIB Cash vs KSS Margin).

    Sends context `broker` down to `_port_row` and `_borrowed_row`.
    """
    badge = "TEST" if is_test else None
    ts_color = WARN if stale else (UP if live else MUTED)
    prefix = "🟢 live " if live else "🕒 snapshot "
    status = _cell(f"{prefix}{snapshot_label}" + ("  ⚠️ ข้อมูลเก่า" if stale else ""), ts_color)

    if brokers_data:
        bubbles: List[Dict[str, Any]] = []
        for bdata in brokers_data:
            b_name = str(bdata.get("broker", "kss")).lower()
            b_label = _BROKER_LABEL.get(b_name, b_name.upper())
            b_acct = bdata.get("account", "MARGIN" if b_name == "kss" else "CASH")
            b_amount = bdata.get("amount")
            b_pnl_val = float(bdata.get("pnl_value", 0.0) or 0.0)
            b_pnl_pct = float(bdata.get("pnl_pct", 0.0) or 0.0)
            b_rows = bdata.get("rows", [])
            b_borrowed = bdata.get("borrowed", [])
            b_actionable = bool(bdata.get("actionable", True) and b_name in ("kss", "lib"))
            if b_actionable:
                b_footer = [_msg_button(f"🔄 /port {b_name} sync", f"/port {b_name} sync", color="#0ea5e9"),
                            _msg_button(f"📋 /orders {b_name}", f"/orders {b_name}")]
            else:
                b_footer = None

            pnl_col = UP if b_pnl_val >= 0 else DOWN
            b_summary: List[Dict[str, Any]] = [
                _kv("Amount", _money(b_amount)),
                _kv("Unrealized P/L", f"{b_pnl_val:+,.2f} ({b_pnl_pct:+.2f}%)", pnl_col),
            ]
            if b_borrowed:
                b_summary.append(_cell("หุ้นยืมคงเหลือ (B)", MUTED, weight="bold"))
                b_summary.extend(_borrowed_row(b, broker=b.get("broker") or b_name, actionable=b_actionable) for b in b_borrowed)
            b_summary.append({"type": "separator", "margin": "md", "color": "#334155"})
            header = _port_header_row()

            pages = [b_rows[i:i + rows_per_bubble] for i in range(0, len(b_rows), rows_per_bubble)] or [[]]
            for idx, page in enumerate(pages):
                if idx == 0:
                    body: List[Dict[str, Any]] = list(b_summary) + [header]
                    if not b_rows:
                        body.append(_cell("ℹ️ ยังไม่มีหุ้นในพอร์ต", MUTED))
                    body.extend(_port_row(r, broker=r.get("broker") or b_name, actionable=b_actionable) for r in page)
                    body.append({"type": "separator", "margin": "md", "color": "#334155"})
                    body.append(status)
                    bubbles.append(_bubble(title=f"📊 {b_label} · {b_acct}", badge=badge,
                                           badge_bg=TEST_COLOR, body=body, footer=b_footer))
                else:
                    body = [header] + [_port_row(r, broker=r.get("broker") or b_name, actionable=b_actionable) for r in page]
                    bubbles.append(_bubble(title=f"📊 {b_label} · ต่อ {idx + 1}/{len(pages)}", body=body))

        alt = f"📊 Multi-Broker Portfolio ({len(brokers_data)} โบรกเกอร์)"
        if len(bubbles) == 1:
            return _flex(alt, bubbles[0])
        return carousel(bubbles, alt)

    # Combined rows representation
    rows = rows or []
    loans = borrowed or []
    pnl_color = UP if pnl_value >= 0 else DOWN
    summary: List[Dict[str, Any]] = [
        _kv("Amount", _money(amount)),
        _kv("Unrealized P/L", f"{pnl_value:+,.2f} ({pnl_pct:+.2f}%)", pnl_color),
    ]
    if loans:
        summary.append(_cell("หุ้นยืมคงเหลือ (B)", MUTED, weight="bold"))
        summary.extend(_borrowed_row(b, broker=b.get("broker") or "kss") for b in loans)
    summary.append({"type": "separator", "margin": "md", "color": "#334155"})
    header = _port_header_row()
    footer = [_msg_button("🔄 /port all sync", "/port all sync", color="#0ea5e9"),
              _msg_button("📋 /orders all", "/orders all")]

    pages = [rows[i:i + rows_per_bubble] for i in range(0, len(rows), rows_per_bubble)] or [[]]
    bubbles = []
    for idx, page in enumerate(pages):
        if idx == 0:
            body = list(summary) + [header]
            if not rows:
                body.append(_cell("ℹ️ ยังไม่มีหุ้นในพอร์ต", MUTED))
            body.extend(_port_row(r, broker=r.get("broker")) for r in page)
            body.append({"type": "separator", "margin": "md", "color": "#334155"})
            body.append(status)
            bubbles.append(_bubble(title="📊 MULTI-BROKER PORTFOLIO", badge=badge,
                                   badge_bg=TEST_COLOR, body=body, footer=footer))
        else:
            body = [header] + [_port_row(r, broker=r.get("broker")) for r in page]
            bubbles.append(_bubble(title=f"📊 MULTI-BROKER · ต่อ {idx + 1}/{len(pages)}", body=body))

    alt = f"📊 พอร์ต Multi-Broker Amount {_money(amount)} ({len(rows)} ตัว)"
    if len(bubbles) == 1:
        return _flex(alt, bubbles[0])
    return carousel(bubbles, alt)


# --------------------------------------------------------------------------- #
# /cash
# --------------------------------------------------------------------------- #
def cash_card(*, broker: str, account: str, rows: List[Dict[str, str]], snapshot_label: str, live: bool) -> Dict[str, Any]:
    """``rows`` = [{"label": "เงินสด / Cash balance", "value": "250,000.00", "color": FG?}]"""
    body: List[Dict[str, Any]] = [_kv(r["label"], r["value"], r.get("color", FG)) for r in rows]
    body.append({"type": "separator", "margin": "md", "color": "#334155"})
    body.append(_cell(("🟢 live " if live else "🕒 snapshot ") + snapshot_label, UP if live else MUTED))
    footer = [_msg_button(f"🔄 /cash {broker} sync", f"/cash {broker} sync", color="#0ea5e9")]
    return _flex(f"💰 {broker} เงินคงเหลือ",
                 _bubble(title=f"💰 {broker.upper()} · {account}", body=body, footer=footer))


# --------------------------------------------------------------------------- #
# "อ่านไม่ได้" / session expired (RCA-009, RCA-006b)
# --------------------------------------------------------------------------- #
UNAVAILABLE_HINT = "อ่านไม่ได้ / session หมดอายุ"


def unavailable_card(*, broker: str, login_cmd: str) -> Dict[str, Any]:
    """Honest read-failure card — never claims 🟢 live and never shows fabricated numbers."""
    line = f"⚠️ {UNAVAILABLE_HINT} — {login_cmd}"
    body: List[Dict[str, Any]] = [
        _cell(line, WARN, weight="bold"),
        {"type": "separator", "margin": "md", "color": "#334155"},
        _cell("ตัวเลขจะกลับมาแสดงหลังเข้าสู่ระบบใหม่", MUTED),
    ]
    footer = [_msg_button(f"🔐 {login_cmd}", login_cmd, color="#0ea5e9")]
    mini_otp = _miniapp_uri("brokers", {"focus": "otp"})
    if mini_otp:
        footer.append(_uri_button("🏦 เปิด MiniApp", mini_otp, color="#b45309"))
    return _flex(f"⚠️ {broker} อ่านข้อมูลไม่ได้",
                 _bubble(title=f"⚠️ {broker.upper()} · อ่านไม่ได้", body=body, footer=footer))


# --------------------------------------------------------------------------- #
# /orders
# --------------------------------------------------------------------------- #
_STATUS_EMOJI = {
    "open": "⏳", "matched": "✅", "partial": "⏳", "cancelled": "✖", "rejected": "✖", "unknown": "❔",
}


def orders_card(
    *,
    broker: str,
    total: int,
    open_count: int,
    rows: List[Dict[str, Any]],
    snapshot_label: str,
    live: bool,
    show_nego: bool,
) -> Dict[str, Any]:
    """``rows`` = [{"kind": "open", "side": "BUY", "symbol": "PTT", "volume": 1000,
    "price": 34.0, "balance": 1000, "status": "…"}]"""
    body: List[Dict[str, Any]] = []
    if not rows:
        body.append(_cell("ℹ️ ยังไม่มีออเดอร์ในระบบ", MUTED))
    for r in rows:
        kind = str(r.get("kind", "unknown")).lower()
        emoji = _STATUS_EMOJI.get(kind, "❔")
        bal = int(r.get("balance", 0) or 0)
        tail = f"ค้าง {bal:,}" if kind in ("open", "partial") else str(r.get("status", emoji))
        # CLI-RCA-004: two lines per order — the symbol gets its own line in bold `md` so a long
        # ticker is never clipped on a phone screen; volume/price + status sit on line two.
        body.append({
            "type": "box", "layout": "vertical", "margin": "sm", "spacing": "xs",
            "contents": [
                _cell(f"{emoji} {r.get('side', '')} {r.get('symbol', '')}", FG,
                      weight="bold", size="md", wrap=True),
                _row([
                    _cell(f"{int(r.get('volume', 0)):,} @{float(r.get('price', 0.0)):.2f}", FG, flex=3),
                    _cell(tail, MUTED, flex=2, align="end"),
                ]),
            ],
        })
    body.append({"type": "separator", "margin": "md", "color": "#334155"})
    body.append(_cell(("🟢 live " if live else "🕒 snapshot ") + snapshot_label, UP if live else MUTED))

    footer: List[Dict[str, Any]] = []
    if show_nego and open_count > 0:
        footer.append(_msg_button(f"⚡ /nego {broker}", f"/nego {broker}", color="#b45309"))
    footer.append(_msg_button(f"🔄 /orders {broker} sync", f"/orders {broker} sync", color="#0ea5e9"))
    mini_orders = _miniapp_uri("orders")
    if mini_orders and len(footer) < 3:
        footer.append(_uri_button("📱 MiniApp", mini_orders, color="#3b82f6"))
    title = f"📋 {broker.upper()} ออเดอร์วันนี้ ({open_count} ค้าง / {total} ทั้งหมด)"
    return _flex(f"📋 {broker} ออเดอร์ {total} รายการ",
                 _bubble(title=title, body=body, footer=footer))


# --------------------------------------------------------------------------- #
# /orders — per-stock cards with an inline ❌ cancel button (TASK t_69fed7ac)
# --------------------------------------------------------------------------- #
def _cancellable(row: Dict[str, Any]) -> bool:
    """Only a live resting order may be cancelled (open / queuing / partial)."""
    if row.get("cancellable") is not None:
        return bool(row.get("cancellable"))
    kind = str(row.get("kind") or "").lower()
    return kind in ("open", "partial") and int(row.get("balance", 0) or 0) > 0


def orders_cancel_cards(
    *,
    broker: str,
    orders: List[Dict[str, Any]],
    snapshot_label: str,
    max_cards: int = 10,
) -> List[Dict[str, Any]]:
    """Strategy B — one bubble per **stock**, no summary card.

    ``orders`` = [{"order_no", "symbol", "side", "volume", "price", "balance",
    "status", "kind", "cancellable"}]. Each bubble lists that stock's orders and
    carries a ``❌ ยกเลิก #<no>`` postback button **only** for orders that are
    still resting (open / partial) — a matched or cancelled row never gets one.
    Returns one or more carousel messages (LINE caps a carousel at 12 bubbles).
    """
    broker_label = _BROKER_LABEL.get(str(broker).lower(), str(broker).upper())
    by_symbol: Dict[str, List[Dict[str, Any]]] = {}
    for r in orders:
        by_symbol.setdefault(str(r.get("symbol") or "?").upper(), []).append(r)

    bubbles: List[Dict[str, Any]] = []
    for symbol in sorted(by_symbol):
        rows = by_symbol[symbol]
        body: List[Dict[str, Any]] = []
        footer: List[Dict[str, Any]] = []
        for r in rows:
            kind = str(r.get("kind") or "unknown").lower()
            side = str(r.get("side") or "").upper()
            no = str(r.get("order_no") or "")
            emoji = _STATUS_EMOJI.get(kind, "❔")
            bal = int(r.get("balance", 0) or 0)
            body.append(_row([
                _cell(f"{emoji} {side} {int(r.get('volume', 0) or 0):,} "
                      f"@{float(r.get('price', 0.0) or 0.0):.2f}",
                      _SIDE_COLOR.get(side.lower(), FG), flex=5, weight="bold", wrap=True),
                _cell(f"#{no}" if no else "—", MUTED, flex=3, align="end"),
            ]))
            tail = f"ค้าง {bal:,}" if kind in ("open", "partial") else str(r.get("status") or kind)
            body.append(_cell(tail, MUTED, size="xxs", align="end", wrap=True))
            if _cancellable(r) and no and len(footer) < 2:
                footer.append(_postback_button(
                    f"❌ ยกเลิก #{no}",
                    f"action=order_cancel_request&broker={str(broker).lower()}&order_no={no}",
                    f"❌ ยกเลิกออเดอร์ #{no} {side} {symbol}",
                    DOWN,
                ))
        resting = [r for r in rows if _cancellable(r)]
        if len(resting) > 2:
            body.append(_cell(
                f"อีก {len(resting) - 2} ออเดอร์ที่ยกเลิกได้ — พิมพ์ /cancel <order_no>",
                MUTED, size="xxs", wrap=True))
        body.append({"type": "separator", "margin": "md", "color": "#334155"})
        body.append(_cell(f"🟢 live {snapshot_label}", UP))
        bubbles.append(_bubble(
            title=f"{symbol} · {broker_label}",
            badge=_STATUS_EMOJI.get(str(rows[0].get("kind") or "").lower(), "❔"),
            badge_bg="#1e293b",
            body=body, footer=footer or None,
        ))

    if not bubbles:
        return [orders_card(broker=broker_label, total=0, open_count=0, rows=[],
                            snapshot_label=snapshot_label, live=True, show_nego=False)]

    messages: List[Dict[str, Any]] = []
    for i in range(0, len(bubbles), max_cards):
        chunk = bubbles[i:i + max_cards]
        messages.append(carousel(chunk, f"📋 {broker_label} — {len(by_symbol)} หุ้น · {len(orders)} ออเดอร์"))
    return messages


def order_cancel_confirm_card(
    *,
    draft_id: str,
    broker: str,
    symbol: str,
    side: str,
    order_no: str,
    volume: int,
    price: float,
    status_label: str,
    ttl_seconds: int,
) -> Dict[str, Any]:
    """Two-step confirmation — nothing is cancelled until ✅ is pressed."""
    broker_label = _BROKER_LABEL.get(str(broker).lower(), str(broker).upper())
    body: List[Dict[str, Any]] = [
        _kv("หุ้น", symbol),
        _kv("คำสั่ง", side, _SIDE_COLOR.get(side.lower(), FG)),
        _kv("ออเดอร์", f"#{order_no}"),
        _kv("คงเหลือ", f"{int(volume):,} หุ้น"),
        _kv("ราคา", f"฿{float(price):.2f}"),
        _kv("สถานะ", status_label, WARN),
        {"type": "separator", "margin": "md", "color": "#334155"},
        _cell("⚠️ ยกเลิกแล้วกู้คืนไม่ได้ — ต้องส่งคำสั่งใหม่เอง", MUTED, wrap=True),
        _cell(f"⏱️ การ์ดหมดอายุใน {max(1, int(ttl_seconds) // 60)} นาที", MUTED, wrap=True),
    ]
    footer = [
        _postback_button("✅ ยืนยันยกเลิก", f"action=order_cancel_confirm&draft_id={draft_id}",
                         f"✅ ยืนยันยกเลิก #{order_no} {symbol}", DOWN),
        _postback_button("↩️ ไม่ยกเลิก", f"action=order_cancel_abort&draft_id={draft_id}",
                         f"↩️ ไม่ยกเลิก #{order_no}", "#475569"),
    ]
    return _flex(
        f"ยืนยันยกเลิกออเดอร์ #{order_no} {side} {symbol}",
        _bubble(title=f"🗑️ ยกเลิกออเดอร์ {symbol}", badge=broker_label,
                badge_bg=DOWN, body=body, footer=footer),
    )


def order_cancel_result_card(
    *,
    broker: str,
    symbol: str,
    order_no: str,
    side: str,
    volume: int,
    success: bool,
    verified: bool,
    detail: Optional[str],
    ts: str,
) -> Dict[str, Any]:
    """Outcome card — an unverified cancel is reported as ❌, never as done."""
    broker_label = _BROKER_LABEL.get(str(broker).lower(), str(broker).upper())
    ok = bool(success and verified)
    body: List[Dict[str, Any]] = [
        _kv("หุ้น", symbol),
        _kv("คำสั่ง", side, _SIDE_COLOR.get(side.lower(), FG)),
        _kv("ออเดอร์", f"#{order_no}"),
        _kv("คงเหลือก่อนยกเลิก", f"{int(volume):,} หุ้น"),
        _kv("ผล", "✅ ยกเลิกสำเร็จ (ยืนยันจากโบรกแล้ว)" if ok else "❌ ยกเลิกไม่สำเร็จ", UP if ok else DOWN),
    ]
    if detail:
        body.append(_cell(str(detail)[:200], MUTED, wrap=True, size="xs"))
    body.append({"type": "separator", "margin": "md", "color": "#334155"})
    body.append(_cell(f"🕒 {ts}", MUTED, align="end"))
    footer = [_msg_button(f"📋 /orders {broker} sync", f"/orders {broker} sync", "#0ea5e9")]
    return _flex(
        f"{'✅ ยกเลิก' if ok else '❌ ยกเลิกไม่สำเร็จ'} #{order_no} {symbol}",
        _bubble(title=f"🗑️ ผลการยกเลิก {symbol}", badge=broker_label,
                badge_bg=UP if ok else DOWN, body=body, footer=footer),
    )



# --------------------------------------------------------------------------- #
# /nego
# --------------------------------------------------------------------------- #
def nego_card(
    *,
    broker: str,
    results: List[Dict[str, Any]],
    ok: int,
    skipped: int,
    failed: int,
    ts: str,
    is_test: bool,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """``results`` = [{"status": "ok|skip|fail", "order_no", "side", "symbol",
    "old_price", "new_price", "reason"}]"""
    body: List[Dict[str, Any]] = []
    for r in results:
        st = r.get("status")
        sym = r.get("symbol", "")
        side = r.get("side", "")
        no = r.get("order_no", "")
        if st == "ok":
            body.append(_row([
                _cell(f"✅ #{no} {side} {sym}", UP, flex=3, weight="bold"),
                _cell(f"{float(r.get('old_price', 0.0)):.2f} → {float(r.get('new_price', 0.0)):.2f}", UP, flex=4, align="end"),
            ]))
        else:
            emoji = "⏭️" if st == "skip" else "❌"
            body.append(_row([
                _cell(f"{emoji} #{no} {side} {sym}", WARN if st == "skip" else DOWN, flex=3, weight="bold"),
                _cell(str(r.get("reason", "")), MUTED, flex=4, align="end"),
            ]))
    body.append({"type": "separator", "margin": "md", "color": "#334155"})
    body.append(_kv("สำเร็จ / ข้าม / ล้มเหลว", f"{ok} · {skipped} · {failed}",
                    UP if failed == 0 else DOWN))
    body.append(_cell(f"🕒 {ts}", MUTED, align="end"))
    footer = [_msg_button(f"📋 /orders {broker} sync", f"/orders {broker} sync")]

    badge = "DRY-RUN" if dry_run else ("TEST" if is_test else None)
    badge_bg = WARN if dry_run else TEST_COLOR

    return _flex(f"⚡ ผลต่อราคา {broker}: สำเร็จ {ok} ข้าม {skipped} ล้มเหลว {failed}",
                 _bubble(title=f"⚡ ผลต่อราคา {broker.upper()}", badge=badge,
                         badge_bg=badge_bg, body=body, footer=footer))


# --------------------------------------------------------------------------- #
# /negotiate (Quick Trade in-place price amendment)
# --------------------------------------------------------------------------- #
#: outcome -> (emoji, label, colour)
_NEGOTIATE_OUTCOME: Dict[str, tuple] = {
    "OK": ("✅", "ต่อราคาสำเร็จ", UP),
    "NO_CHANGE": ("⏭️", "อยู่ที่เป้าหมายแล้ว", WARN),
    "UPTICK_VIOLATION": ("⛔", "ผิดกฎ Uptick (SHORT)", DOWN),
    "NO_QUOTE": ("🚫", "อ่านราคาตลาดไม่ได้", DOWN),
    "UNSUPPORTED_SIDE": ("❓", "ไม่รู้จักประเภทคำสั่ง", DOWN),
    "FAILED": ("❌", "ต่อราคาไม่สำเร็จ", DOWN),
    "ERROR": ("❌", "ต่อราคาไม่สำเร็จ", DOWN),
}


def _px(value: Optional[float]) -> str:
    """Price cell — ``None`` renders ``—`` (Anti-Mock: never a fabricated price)."""
    return "—" if value is None else f"{float(value):.2f}"


def negotiate_card(
    *,
    order_no: str,
    symbol: str,
    side: str,
    outcome: str,
    current_price: Optional[float],
    target_price: Optional[float],
    volume: Optional[int],
    reason: str,
    submitted: Optional[bool],
    ts: str,
    is_test: bool,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """One-order result card for ``/negotiate`` (Quick Trade in-place amendment).

    ``submitted`` — ``True`` the broker accepted the amendment, ``False`` it was
    staged only (TEST / DRY-RUN), ``None`` nothing was sent (Anti-Mock: never imply
    a submission that did not happen).
    """
    emoji, label, colour = _NEGOTIATE_OUTCOME.get(outcome, ("ℹ️", outcome or "ไม่ทราบผล", MUTED))
    if submitted is True:
        send_text, send_colour = "ส่งเข้าตลาดแล้ว", UP
    elif submitted is False:
        send_text, send_colour = "จัดเตรียมไว้ (TEST/DRY-RUN)", WARN
    else:
        send_text, send_colour = "—", MUTED

    body: List[Dict[str, Any]] = [
        _kv("ออเดอร์", f"#{order_no} {side} {symbol}"),
        _kv("ราคาเดิม → เป้าหมาย", f"{_px(current_price)} → {_px(target_price)}"),
        _kv("จำนวน", f"{int(volume):,}" if volume is not None else "—"),
        _kv("สถานะการส่ง", send_text, send_colour),
        {"type": "separator", "margin": "md", "color": "#334155"},
        _cell(f"{emoji} {label}", colour, weight="bold", wrap=True),
    ]
    if reason:
        body.append(_cell(reason[:160], MUTED, wrap=True))
    body.append(_cell(f"🕒 {ts}", MUTED, align="end"))
    footer = [
        _msg_button(f"⚡ /negotiate {order_no}", f"/negotiate {order_no}", color="#b45309"),
        _msg_button("📋 /orders kss sync", "/orders kss sync"),
    ]

    badge = "DRY-RUN" if dry_run else ("TEST" if is_test else None)
    badge_bg = WARN if dry_run else TEST_COLOR

    return _flex(f"⚡ ต่อราคา #{order_no}: {label}",
                 _bubble(title=f"⚡ Quick Trade #{order_no}",
                         badge=badge, badge_bg=badge_bg,
                         body=body, footer=footer))


# --------------------------------------------------------------------------- #
# Phase B — order confirmation + receipt cards (B.5)
# --------------------------------------------------------------------------- #
_BROKER_LABEL = {"kss": "KSS", "lib": "LIB"}
_SIDE_LABEL = {"buy": "BUY", "sell": "SELL", "short": "SHORT", "cover": "COVER"}
#: side → colour (buy/cover = green, sell/short = red).
_SIDE_COLOR = {"buy": UP, "sell": DOWN, "short": DOWN, "cover": UP}


def _postback_button(label: str, data: str, display_text: str, color: str = "#334155") -> Dict[str, Any]:
    return {
        "type": "button", "style": "primary", "color": color, "height": "sm", "flex": 1,
        "action": {"type": "postback", "label": label, "data": data, "displayText": display_text},
    }


def trade_confirmation_card(
    *,
    draft_id: str,
    cmd: str,
    symbol: str,
    volume: int,
    price: float,
    broker: str,
    account: str,
    account_no: Optional[str],
    total_thb: float,
    ttl_seconds: int,
    budget_thb: Optional[float] = None,
    ref_market_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Human-in-the-loop confirm card for a drafted Phase-B order (nothing submitted).

    The two footer buttons carry the opaque ``draft_id`` in their postback payload;
    the colour of the title badge follows the side (green BUY/COVER, red SELL/SHORT).
    """
    side_label = _SIDE_LABEL.get((cmd or "").lower(), (cmd or "").upper())
    side_color = _SIDE_COLOR.get((cmd or "").lower(), FG)
    broker_label = _BROKER_LABEL.get((broker or "").lower(), (broker or "").upper())
    account_label = "MARGIN" if (account or "").lower() == "margin" else "CASH"

    body: List[Dict[str, Any]] = [
        _kv("หุ้น", symbol),
        _kv("คำสั่ง", side_label, side_color),
    ]
    if budget_thb is not None:
        body.append(_kv("งบประมาณที่ตั้ง", f"฿{float(budget_thb):,.2f}"))
        body.append(_kv("ราคาอ้างอิงตลาด", f"฿{float(ref_market_price or price):.2f}"))
        body.append(_kv("จำนวนคำนวณ", f"{int(volume):,} หุ้น (Board Lot)"))
        body.append(_kv("มูลค่ารวม", f"฿{float(total_thb):,.2f}"))
    else:
        body.append(_kv("จำนวน", f"{int(volume):,} หุ้น"))
        body.append(_kv("ราคา", f"฿{float(price):.2f}"))
        body.append(_kv("มูลค่า", f"฿{float(total_thb):,.2f}"))

    body.extend([
        _kv("บัญชี", f"{broker_label} · {account_label}" + (f" {account_no}" if account_no else "")),
        {"type": "separator", "margin": "md", "color": "#334155"},
        _cell(f"⏱️ การ์ดหมดอายุใน {int(ttl_seconds) // 60} นาที — ราคา/วงเงินอาจเปลี่ยน", MUTED, wrap=True),
    ])

    confirm_data = f"action=trade_draft_confirm&draft_id={draft_id}"
    cancel_data = f"action=trade_draft_cancel&draft_id={draft_id}"
    footer = [
        _postback_button("✅ ยืนยัน", confirm_data,
                         f"✅ ยืนยันคำสั่ง {side_label} {symbol} {int(volume):,} @ ฿{float(price):.2f}", UP),
        _postback_button("❌ ยกเลิก", cancel_data, f"❌ ยกเลิกคำสั่ง {side_label} {symbol}", DOWN),
    ]

    return _flex(
        f"ยืนยันคำสั่ง {side_label} {symbol} {int(volume):,} @ ฿{float(price):.2f}",
        _bubble(title=f"🧾 ยืนยันคำสั่ง {side_label} {symbol}", badge=side_label,
                badge_bg=side_color, body=body, footer=footer),
    )


def trade_receipt_card(
    *,
    draft_id: str,
    outcome: str,
    cmd: Optional[str] = None,
    symbol: Optional[str] = None,
    volume: Optional[int] = None,
    price: Optional[float] = None,
    broker: Optional[str] = None,
    account: Optional[str] = None,
    account_no: Optional[str] = None,
    broker_order_no: Optional[str] = None,
    dry_run: bool = False,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    """Receipt after trade execution or draft confirmation/cancellation."""
    confirmed = outcome == "confirmed"
    failed = outcome == "failed"
    side_label = _SIDE_LABEL.get((cmd or "").lower(), (cmd or "").upper()) if cmd else None
    broker_label = _BROKER_LABEL.get((broker or "").lower(), (broker or "").upper()) if broker else None

    if confirmed:
        head = (
            "🧪 [DRY-RUN] ยืนยันคำสั่งแล้ว"
            if dry_run
            else ("✅ ยืนยันคำสั่งแล้ว · ส่งเข้าโบรกเกอร์" if broker_order_no else "✅ ยืนยันคำสั่งแล้ว")
        )
        head_color = WARN if dry_run else UP
        detail = (
            f"จำลองคำสั่งเรียบร้อย (#{broker_order_no})"
            if (dry_run and broker_order_no)
            else f"ส่งคำสั่งไปยังโบรกเกอร์แล้ว (#{broker_order_no})"
            if broker_order_no
            else "ส่งต่อให้ executor ดำเนินการ — กำลังส่งคำสั่งไปยังโบรกเกอร์"
        )
        badge = "DRY-RUN" if dry_run else "CONFIRMED"
    elif failed:
        head, head_color = "❌ ส่งคำสั่งไม่สำเร็จ", DOWN
        detail = str(error or "โบรกเกอร์ปฏิเสธคำสั่ง")[:220]
        badge = "FAILED"
    else:
        head, head_color = "❌ ยกเลิกคำสั่งแล้ว", DOWN
        detail = "draft ถูกลบออกแล้ว — ไม่มีการส่งคำสั่ง"
        badge = "CANCELLED"

    body: List[Dict[str, Any]] = [
        _cell(head, head_color, weight="bold"),
        _kv("draft_id", draft_id),
    ]
    if broker_order_no:
        body.append(_kv("Order #", str(broker_order_no)))
    if symbol:
        body.append(_kv("หุ้น", symbol))
    if side_label:
        body.append(_kv("คำสั่ง", side_label, _SIDE_COLOR.get((cmd or "").lower(), FG)))
    if volume is not None:
        body.append(_kv("จำนวน", f"{int(volume):,} หุ้น"))
    if price is not None:
        body.append(_kv("ราคา", f"฿{float(price):.2f}"))
    if broker_label:
        account_label = "MARGIN" if (account or "").lower() == "margin" else "CASH"
        body.append(_kv("บัญชี", f"{broker_label} · {account_label}" + (f" {account_no}" if account_no else "")))
    body.append({"type": "separator", "margin": "md", "color": "#334155"})
    body.append(_cell(detail, DOWN if failed else MUTED, wrap=True))

    mini_orders = _miniapp_uri("orders")
    footer = [_uri_button("📋 ดูสถานะออเดอร์", mini_orders, color="#0284c7")] if mini_orders else None

    return _flex(
        f"{head} (draft {draft_id})",
        _bubble(title=f"🧾 ใบรับคำสั่ง {draft_id}", badge=badge, badge_bg=head_color, body=body, footer=footer),
    )


def sbl_result_card(
    *,
    action: str,
    symbol: str,
    volume: int = 0,
    lots_returned: Optional[List[Dict[str, Any]]] = None,
    dry_run: bool = False,
    success: bool = True,
    error: Optional[str] = None,
    auto_short: Optional[Dict[str, Any]] = None,
    ts: Optional[str] = None,
) -> Dict[str, Any]:
    """Result card pushed to LINE Group after MiniApp SBL borrow / borrow+short / return execution."""
    act = str(action or "sbl").strip().lower()
    sym = str(symbol or "-").strip().upper()
    act_th = "ขอยืมหุ้น SBL" if act == "borrow" else "คืนหุ้น SBL" if act == "return" else act.upper()
    partial_short_fail = bool(auto_short and auto_short.get("attempted") and not auto_short.get("ok"))

    if not success:
        head, head_color, badge = f"❌ {act_th} ไม่สำเร็จ", DOWN, "FAILED"
    elif partial_short_fail:
        head, head_color, badge = "⚠️ ยืม SBL สำเร็จ (Short ไม่สำเร็จ)", WARN, "PARTIAL"
    elif dry_run:
        head, head_color, badge = f"🧪 [DRY-RUN] {act_th} สำเร็จ", WARN, "DRY-RUN"
    else:
        head, head_color, badge = f"✅ {act_th} สำเร็จ", UP, "LIVE"

    refs = [
        str(item.get("ref_no")).strip()
        for item in (lots_returned or [])
        if isinstance(item, dict) and item.get("ref_no") and str(item.get("ref_no")).strip() != sym
    ]
    body: List[Dict[str, Any]] = [
        _cell(head, head_color, weight="bold", wrap=True),
        _kv("หุ้น", sym),
        _kv("รายการ", act_th),
        _kv("จำนวน", f"{int(volume or 0):,} หุ้น"),
        _kv("บัญชี", "KSS · MARGIN <REDACTED_KSS_ACCT>"),
    ]
    if refs:
        body.append(_kv("Ref.no", ", ".join(refs)))
    if auto_short and auto_short.get("attempted"):
        if auto_short.get("ok"):
            ord_no = (auto_short.get("execution") or {}).get("broker_order_no") or auto_short.get("draft_id") or "ส่งแล้ว"
            body.append(_kv("Auto-Short", f"✅ #{ord_no}", UP))
        else:
            body.append(_kv("Auto-Short", f"❌ {str(auto_short.get('error') or 'ไม่สำเร็จ')[:120]}", DOWN))
    if error:
        body.extend([
            {"type": "separator", "margin": "md", "color": "#334155"},
            _cell(str(error)[:240], DOWN, wrap=True, size="xs"),
        ])
    if ts:
        body.append(_cell(f"🕒 {ts}", MUTED, align="end"))

    mini_port = _miniapp_uri("portfolio")
    footer = [_uri_button("📊 ดูพอร์ตล่าสุด", mini_port, color="#0284c7")] if mini_port else None

    return _flex(
        f"{head} {sym} {int(volume or 0):,} หุ้น",
        _bubble(title=f"📦 ผลทำรายการ SBL ({sym})", badge=badge, badge_bg=head_color, body=body, footer=footer),
    )


# --------------------------------------------------------------------------- #
# SBL Cards (Loop 2)
# --------------------------------------------------------------------------- #
def sbl_status_card(lots: List[Dict[str, Any]], asof_time: str) -> Dict[str, Any]:
    """Displays active borrowed lots on Krungsri MARGIN <REDACTED_KSS_ACCT>."""
    if not lots:
        body = [
            _cell("ไม่มีรายการหุ้นยืม SBL คงค้าง", MUTED),
            _cell("บัญชี Krungsri MARGIN (<REDACTED_KSS_ACCT>) พร้อมใช้งาน", UP),
            {"type": "separator", "margin": "md", "color": "#334155"},
            _cell(f"อัปเดตเมื่อ {asof_time}", MUTED),
        ]
        return _flex(
            "สถานะหุ้นยืม SBL: ไม่มีหุ้นยืมค้าง",
            _bubble(title="📦 หุ้นยืม SBL คงค้าง (KSS)", badge="SBL", badge_bg="#6366f1", body=body),
            quick=["/sbl pool PTT", "/sbl sync", "/port kss"],
        )

    rows: List[Dict[str, Any]] = [
        _row([
            _cell("หุ้น", MUTED, flex=2, weight="bold"),
            _cell("คงเหลือ", MUTED, flex=2, align="end", weight="bold"),
            _cell("ดอกเบี้ย", MUTED, flex=2, align="end", weight="bold"),
        ]),
        {"type": "separator", "margin": "xs", "color": "#334155"},
    ]
    for lot in lots:
        sym = str(lot.get("symbol", "-"))
        qty = int(lot.get("remaining_qty", 0) or 0)
        fee = float(lot.get("borrow_fee_rate", 3.5) or 3.5)
        xd = lot.get("ex_dividend_date")
        row_cells = [
            _cell(sym, FG, flex=2, weight="bold"),
            _cell(f"{qty:,}", FG, flex=2, align="end"),
            _cell(f"{fee:.2f}%", WARN, flex=2, align="end"),
        ]
        rows.append(_row(row_cells))
        if xd:
            rows.append(_cell(f"⚠️ เตือน XD: {xd}", DOWN, size="xxs"))

    rows.append({"type": "separator", "margin": "md", "color": "#334155"})
    rows.append(_cell(f"อัปเดตเมื่อ {asof_time} · บัญชี MARGIN <REDACTED_KSS_ACCT>", MUTED))

    return _flex(
        f"สถานะหุ้นยืม SBL ({len(lots)} รายการ)",
        _bubble(title=f"📦 หุ้นยืม SBL คงค้าง ({len(lots)} รายการ)", badge="SBL", badge_bg="#6366f1", body=rows),
        quick=["/sbl sync", "/port kss", "/help return"],
    )


def sbl_pool_card(
    symbol: str,
    available_qty: int,
    fee_rate: float = 3.50,
    status_message: Optional[str] = None,
) -> Dict[str, Any]:
    """Displays available inventory for a symbol in Krungsri TradeMan SBL pool."""
    if available_qty > 0:
        avail_text = f"{available_qty:,} หุ้น"
        avail_color = UP
    elif status_message:
        avail_text = "ยังไม่มีข้อมูล — /sbl sync ก่อน"
        avail_color = MUTED
    else:
        avail_text = "ไม่มีในคลัง"
        avail_color = DOWN
    body = [
        _kv("หุ้น", symbol),
        _kv("จำนวนในคลัง", avail_text, avail_color),
        _kv("อัตราดอกเบี้ยยืม", f"{fee_rate:.2f}% ต่อปี", WARN),
        _kv("โบรกเกอร์", "Krungsri TradeMan (MARGIN)"),
        {"type": "separator", "margin": "md", "color": "#334155"},
        _cell("💡 สั่งยืมด้วยคำสั่ง /borrow หรือกดปุ่มด้านล่าง", MUTED, wrap=True),
    ]
    quick = [f"/borrow {symbol} 100", f"/borrow {symbol} 1000"] if available_qty > 0 else (
        ["/sbl sync", f"/sbl pool {symbol}"] if status_message else ["/sbl"]
    )
    return _flex(
        f"คลังหุ้นยืม SBL: {symbol} มี {avail_text}",
        _bubble(title=f"🔍 คลังหุ้นยืม SBL ({symbol})", badge="POOL", badge_bg="#6366f1", body=body),
        quick=quick,
    )


def sbl_borrow_confirm_card(
    *,
    draft_id: str,
    symbol: str,
    volume: int,
    fee_rate: float = 3.50,
    xd_warning: Optional[str] = None,
    budget_thb: Optional[float] = None,
    last_price: Optional[float] = None,
) -> Dict[str, Any]:
    """Confirmation card for SBL stock borrow."""
    body = [
        _kv("หุ้น", symbol),
        _kv("จำนวนขอยืม", f"{volume:,} หุ้น"),
    ]
    if budget_thb is not None:
        body.append(_kv("งบประมาณ", f"฿{float(budget_thb):,.2f}"))
    if last_price is not None:
        try:
            last_price = float(last_price)
        except (ValueError, TypeError):
            last_price = None
    if last_price is not None and last_price > 0:
        est_val = volume * last_price
        est_fee_day = (est_val * (fee_rate / 100.0)) / 365.0
        body.append(_kv("มูลค่าหุ้นที่ยืม", f"฿{est_val:,.2f} (@{last_price:.2f})"))
        body.append(_kv("อัตราดอกเบี้ย", f"{fee_rate:.2f}% ต่อปี (~฿{est_fee_day:.2f}/วัน)", WARN))
    else:
        body.append(_kv("อัตราดอกเบี้ย", f"{fee_rate:.2f}% ต่อปี", WARN))
    body.append(_kv("บัญชี", "Krungsri MARGIN <REDACTED_KSS_ACCT>"))
    if xd_warning:
        body.append(_cell(f"⚠️ {xd_warning}", DOWN, wrap=True))
    body.append({"type": "separator", "margin": "md", "color": "#334155"},)
    body.append(_cell("⏱️ คำขอยืมผ่านระบบ SBL Krungsri — กรุณากดยืนยัน", MUTED, wrap=True))

    confirm_data = f"action=sbl_borrow_confirm&draft_id={draft_id}"
    cancel_data = f"action=sbl_borrow_cancel&draft_id={draft_id}"
    footer = [
        _postback_button("✅ ยืนยันยืม", confirm_data, f"✅ ยืนยันขอยืมหุ้น {symbol} {volume:,} หุ้น", UP),
        _postback_button("❌ ยกเลิก", cancel_data, f"❌ ยกเลิกขอยืมหุ้น {symbol}", DOWN),
    ]
    return _flex(
        f"ยืนยันขอยืมหุ้น SBL {symbol} {volume:,} หุ้น",
        _bubble(title=f"📦 ยืนยันขอยืมหุ้น SBL ({symbol})", badge="BORROW", badge_bg="#6366f1", body=body, footer=footer),
    )


def sbl_return_confirm_card(
    *,
    draft_id: str,
    symbol: str,
    volume: int,
    days_held: int = 0,
    accrued_fee: float = 0.0,
    lots_summary: Optional[str] = None,
) -> Dict[str, Any]:
    """Confirmation card for SBL stock return."""
    body = [
        _kv("หุ้น", symbol),
        _kv("จำนวนที่คืน", f"{volume:,} หุ้น"),
        _kv("ระยะเวลาที่ยืม", f"{days_held} วัน"),
        _kv("ดอกเบี้ยสะสมโดยประมาณ", f"฿{accrued_fee:,.2f}"),
        _kv("บัญชี", "Krungsri MARGIN <REDACTED_KSS_ACCT>"),
    ]
    if lots_summary:
        body.append(_kv("สัญญา (FIFO)", lots_summary))
    body.extend([
        {"type": "separator", "margin": "md", "color": "#334155"},
        _cell("💡 คืนหุ้นเพื่อหยุดการคิดดอกเบี้ยยืมรายวัน", MUTED, wrap=True),
    ])
    confirm_data = f"action=sbl_return_confirm&draft_id={draft_id}"
    cancel_data = f"action=sbl_return_cancel&draft_id={draft_id}"
    footer = [
        _postback_button("✅ ยืนยันคืน", confirm_data, f"✅ ยืนยันคืนหุ้น {symbol} {volume:,} หุ้น", UP),
        _postback_button("❌ ยกเลิก", cancel_data, f"❌ ยกเลิกคืนหุ้น {symbol}", DOWN),
    ]
    return _flex(
        f"ยืนยันคืนหุ้น SBL {symbol} {volume:,} หุ้น",
        _bubble(title=f"📦 ยืนยันคืนหุ้น SBL ({symbol})", badge="RETURN", badge_bg="#6366f1", body=body, footer=footer),
    )


def quick_trade_selector_card() -> Dict[str, Any]:
    """Interactive trade side selector card for Rich Menu Quick Trade button."""
    body = [
        _cell("เลือกประเภทคำสั่งซื้อขายที่ต้องการส่ง:", FG, weight="bold"),
        _cell("• ฝั่งซื้อ/ขายปกติ ส่งผ่าน Liberator (CASH)", MUTED, size="xxs"),
        _cell("• ฝั่งชอร์ต/คัฟเวอร์ ส่งผ่าน Krungsri (MARGIN)", MUTED, size="xxs"),
        {"type": "separator", "margin": "md", "color": "#334155"},
        {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "contents": [
                _msg_button("🟢 ซื้อหุ้น (BUY · LIB CASH)", "/buy PTT 50k", color="#166534"),
                _msg_button("🔴 ขายหุ้น (SELL · LIB CASH)", "/sell PTT 1000 35.0", color="#991b1b"),
                # SHORT/COVER require a price argument; a bare ``/short PTT 1000``
                # returns E_MISSING_ARG and the tap does nothing. Value mode (``50k``)
                # is the one form that parses without a hand-typed price.
                _msg_button("🟣 ชอร์ตหุ้น (SHORT · KSS MARGIN)", "/short PTT 50k", color="#581c87"),
                _msg_button("🔵 ปิดชอร์ต (COVER · KSS MARGIN)", "/cover PTT 50k", color="#1e40af"),
            ],
        },
    ]
    return _flex(
        "⚡ เลือกประเภทคำสั่ง Quick Trade",
        _bubble(title="⚡ Quick Trade Studio", badge="TRADE", badge_bg="#3b82f6", body=body),
        quick=["/buy PTT 50k", "/sell PTT 1000", "/sbl", "/port all sync"],
    )


def quick_hub_card() -> Dict[str, Any]:
    """Quick Hub Flex Card triggered by Rich Menu cell 6 (free replyToken, 0 push quota)."""
    chat_btns = [
        {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "contents": [
                _msg_button("📊 พอร์ตรวม", "/port all sync", color="#0284c7"),
                _msg_button("📋 ออเดอร์", "/orders all sync", color="#d97706"),
            ],
        },
        {
            "type": "box",
            "layout": "horizontal",
            "spacing": "sm",
            "contents": [
                _msg_button("🔐 สถานะระบบ", "/status", color="#334155"),
                _postback_button("⚡ ต่อราคา", "action=nego_menu", "⚡ ต่อราคาออเดอร์ค้าง", color="#ca8a04"),
            ],
        },
    ]

    mini_rows: List[Dict[str, Any]] = []
    u_port = _miniapp_uri("portfolio")
    u_trade = _miniapp_uri("trade")
    u_brokers = _miniapp_uri("brokers")
    if u_port and u_trade and u_brokers:
        mini_rows = [
            {"type": "separator", "margin": "md", "color": "#334155"},
            _cell("เปิดหน้าจอเต็มบน LINE MiniApp:", MUTED, size="xxs"),
            {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "contents": [
                    _uri_button("📊 Port", u_port, color="#0284c7"),
                    _uri_button("💹 Trade", u_trade, color="#16a34a"),
                    _uri_button("🏦 Brokers", u_brokers, color="#7e22ce"),
                ],
            },
        ]

    body: List[Dict[str, Any]] = [
        _cell("เลือกส่งคำสั่งการ์ดสรุปในแชท หรือเปิด MiniApp:", FG, weight="bold", wrap=True),
        {
            "type": "box",
            "layout": "vertical",
            "spacing": "sm",
            "margin": "md",
            "contents": chat_btns + mini_rows,
        },
    ]
    return _flex(
        "🃏 Homelab Quick Cards & MiniApp Hub",
        _bubble(title="🃏 Quick Cards Hub", badge="FREE REPLY", badge_bg="#0284c7", body=body),
        quick=["/port all sync", "/orders all sync", "/sbl", "/status"],
    )


# --------------------------------------------------------------------------- #
# carousel
# --------------------------------------------------------------------------- #
def carousel(bubbles: List[Dict[str, Any]], alt: str) -> Dict[str, Any]:
    return {"type": "flex", "altText": alt, "contents": {"type": "carousel", "contents": bubbles}}


# --------------------------------------------------------------------------- #
# /nego menu (ต่อราคา — N3 postback flow)
# --------------------------------------------------------------------------- #
def nego_menu_card(*, orders: List[Dict[str, Any]], title: Optional[str] = None) -> Dict[str, Any]:
    """Carousel of unmatched open orders across both brokers; each bubble carries
    the ⚡ match / 🪤 lure mode buttons + ❌ cancel button (Unified Order & Nego Card)."""
    bubbles: List[Dict[str, Any]] = []
    for o in orders:
        broker = str(o.get("broker", "")).lower()
        broker_label = _BROKER_LABEL.get(broker, broker.upper())
        side = str(o.get("side", "")).upper()
        sym = str(o.get("symbol", "")).upper()
        side_color = _SIDE_COLOR.get(side.lower(), FG)
        order_no = str(o.get("order_no", ""))
        mode_match = f"action=nego_mode&broker={broker}&order_no={order_no}&mode=match"
        mode_lure = f"action=nego_mode&broker={broker}&order_no={order_no}&mode=lure"
        cancel_data = f"action=order_cancel_request&broker={broker}&order_no={order_no}"

        body: List[Dict[str, Any]] = [
            _kv("ออเดอร์", f"#{order_no}"),
            _kv("ราคาเดิม", f"฿{float(o.get('price', 0.0) or 0.0):.2f}"),
            _kv("ค้าง", f"{int(o.get('balance', 0) or 0):,} / {int(o.get('volume', 0) or 0):,} หุ้น"),
        ]

        btn_cancel = _postback_button(f"❌ ยกเลิก #{order_no}", cancel_data, f"❌ ยกเลิกออเดอร์ #{order_no} {side} {sym}", DOWN)
        btn_lure = _postback_button("🪤 ล่อ", mode_lure, f"🪤 ต่อราคา {side} {sym} #{order_no} แบบ lure", WARN)

        if side == "SHORT":
            body.append(_cell("🪤 lure — ต่อท้ายคิวที่ราคาฝั่งตัวเอง (Short ต่อราคาแบบล่อเท่านั้น)", MUTED, size="xxs", wrap=True))
            footer: Any = [btn_lure, btn_cancel]
        else:
            btn_match = _postback_button("⚡ Match", mode_match, f"⚡ ต่อราคา {side} {sym} #{order_no} แบบ match", UP)
            footer = {
                "type": "box", "layout": "vertical", "spacing": "xs", "backgroundColor": BODY_BG,
                "paddingAll": "10px",
                "contents": [
                    {"type": "box", "layout": "horizontal", "spacing": "sm", "contents": [btn_match, btn_lure]},
                    {"type": "box", "layout": "horizontal", "spacing": "sm", "contents": [btn_cancel]},
                ],
            }
        bubbles.append(_bubble(
            title=f"{side} {sym}",
            badge=f"{broker_label}",
            badge_bg=side_color,
            body=body, footer=footer,
        ))

    alt = title or f"⚡ จัดการออเดอร์/ต่อราคา — {len(orders)} ออเดอร์ค้าง"
    return carousel(bubbles, alt)


def orders_empty_card(*, broker: str) -> Dict[str, Any]:
    """Single bubble indicating no open/resting orders for a specific broker."""
    broker_label = _BROKER_LABEL.get(broker.lower(), broker.upper())
    bubble = _bubble(
        title=f"📋 ออเดอร์ {broker_label}",
        badge=broker_label,
        badge_bg="#64748b",
        body=[
            _cell(f"ไม่มีออเดอร์ค้างใน {broker_label} ขณะนี้", MUTED, align="center", size="sm"),
            _cell("คำสั่งจับคู่ครบแล้ว หรือไม่มีคำสั่งเปิดรออยู่", MUTED, align="center", size="xxs"),
        ],
    )
    return _flex(f"📋 ออเดอร์ {broker_label} — ไม่มีออเดอร์ค้าง", bubble)


def orders_manage_cards(
    *,
    broker: str,
    orders: List[Dict[str, Any]],
    snapshot_label: str = "",
    max_cards: int = 12,
) -> List[Dict[str, Any]]:
    """Unified Order Management & Nego Card — 1 Bubble per Resting Order.

    Combines Status, Negotiation (Match / Lure), and Cancel buttons in one place.
    If no resting orders, falls back to an empty orders card.
    """
    broker_label = _BROKER_LABEL.get(str(broker).lower(), str(broker).upper())
    resting = [r for r in orders if _cancellable(r)]
    if not resting:
        return [orders_empty_card(broker=broker_label)]

    messages: List[Dict[str, Any]] = []
    for i in range(0, len(resting), max_cards):
        chunk = resting[i:i + max_cards]
        alt = f"📋 {broker_label} — {len(chunk)} ออเดอร์ค้าง"
        messages.append(nego_menu_card(orders=chunk, title=alt))
    return messages


def nego_empty_card(*, broker: str) -> Dict[str, Any]:
    """Single bubble indicating no open orders for a specific broker."""
    broker_label = _BROKER_LABEL.get(broker.lower(), broker.upper())
    bubble = _bubble(
        title=f"⚡ ต่อราคา {broker_label}",
        badge=broker_label,
        badge_bg="#64748b",
        body=[
            _cell(f"ไม่มีออเดอร์ค้างใน {broker_label} ขณะนี้", MUTED, align="center", size="sm"),
            _cell("คำสั่งจับคู่ครบแล้ว หรือไม่มีคำสั่งเปิดรออยู่", MUTED, align="center", size="xxs"),
        ],
    )
    return _flex(f"⚡ ต่อราคา {broker_label} — ไม่มีออเดอร์ค้าง", bubble)


def nego_draft_card(
    *,
    draft_id: str,
    symbol: str,
    side: str,
    order_no: str,
    broker_label: str,
    old_price: float,
    new_price: float,
    mode_label: str,
    ttl_seconds: int,
) -> Dict[str, Any]:
    """Draft confirm card for one price amendment (nothing submitted yet)."""
    side_color = _SIDE_COLOR.get(side.lower(), FG)
    body: List[Dict[str, Any]] = [
        _kv("หุ้น", symbol),
        _kv("คำสั่ง", side, side_color),
        _kv("ออเดอร์", f"#{order_no}"),
        _kv("โหมด", mode_label),
        _kv("ราคาเดิม", f"฿{float(old_price):.2f}"),
        _kv("ราคาใหม่", f"฿{float(new_price):.2f}", UP),
        {"type": "separator", "margin": "md", "color": "#334155"},
        _cell(f"⏱️ การ์ดหมดอายุใน {int(ttl_seconds) // 60} นาที — ราคาอาจเปลี่ยน", MUTED, wrap=True),
    ]
    confirm_data = f"action=nego_confirm&draft_id={draft_id}"
    cancel_data = f"action=nego_cancel&draft_id={draft_id}"
    footer = [
        _postback_button("✅ ยืนยัน", confirm_data,
                         f"✅ ยืนยันต่อราคา {side} {symbol} → ฿{float(new_price):.2f}", UP),
        _postback_button("❌ ยกเลิก", cancel_data, f"❌ ยกเลิกต่อราคา {side} {symbol}", DOWN),
    ]
    return _flex(
        f"ยืนยันต่อราคา {side} {symbol} ฿{float(old_price):.2f} → ฿{float(new_price):.2f}",
        _bubble(title=f"🧾 ต่อราคา {symbol}", badge=f"{broker_label} · {mode_label}",
                badge_bg=side_color, body=body, footer=footer),
    )


def size_calc_card(
    *,
    symbol: str,
    price: float,
    price_source: str = "quote",
    tiers: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Flex Card for /size <sym> calculation results.

    tiers = [
        {"budget_label": "50k", "budget": 50000.0, "shares": 1400, "actual_cost": 48300.0},
        ...
    ]
    """
    body: List[Dict[str, Any]] = [
        _kv("หุ้น", symbol),
        _kv("ราคาอ้างอิง", f"฿{float(price):.2f}", UP),
        _kv("แหล่งราคา", price_source),
        {"type": "separator", "margin": "md", "color": "#334155"},
    ]
    quick_replies: List[str] = []
    for t in tiers:
        b_lbl = t.get("budget_label", "")
        shares = int(t.get("shares", 0))
        cost = float(t.get("actual_cost", 0.0))
        body.append(
            _kv(f"งบ {b_lbl}", f"{shares:,} หุ้น (฿{cost:,.2f})")
        )
        quick_replies.append(f"/buy {symbol} {b_lbl}")

    body.extend([
        {"type": "separator", "margin": "md", "color": "#334155"},
        _cell("💡 ปัดเศษลงตาม Board Lot (100 หุ้น) · กดปุ่มด้านล่างเพื่อส่งคำสั่งซื้อ", MUTED, wrap=True),
    ])

    footer_buttons = [
        _msg_button(f"ซื้อ {t.get('budget_label')}", f"/buy {symbol} {t.get('budget_label')}")
        for t in tiers
    ]
    footer = _footer(footer_buttons) if footer_buttons else None

    return _flex(
        f"คำนวณขนาดซื้อ {symbol} @ ฿{float(price):.2f}",
        _bubble(
            title=f"🧮 คำนวณขนาด {symbol}",
            badge=f"฿{float(price):.2f}",
            badge_bg="#2563EB",
            body=body,
            footer=footer.get("contents") if footer else None,
        ),
        quick=quick_replies,
    )


def _resolve_miniapp_url(tab: str = "portfolio", params: Optional[Dict[str, Any]] = None) -> str:
    uri = _miniapp_uri(tab, params=params)
    if uri:
        return uri
    from app.core.config import settings
    from urllib.parse import urlencode, urlparse

    webhook_url = (getattr(settings, "LINE_PUBLIC_WEBHOOK_URL", None) or "").strip()
    parsed = urlparse(webhook_url) if webhook_url else None
    if parsed and parsed.scheme and parsed.netloc:
        base = f"{parsed.scheme}://{parsed.netloc}/miniapp/"
    else:
        base = "https://line.a14s.dev/miniapp/"
    query: Dict[str, Any] = {"tab": tab}
    if params:
        for k, v in params.items():
            if v is not None and str(v).strip() != "":
                query[k] = v
    return f"{base}?{urlencode(query)}"


def miniapp_launcher_card(reason: Optional[str] = None, tab: str = "portfolio") -> Dict[str, Any]:
    """Free reply card directing users from LINE OA chat to HomeApp MiniApp."""
    body: List[Dict[str, Any]] = [
        {
            "type": "text",
            "text": "คำสั่งแชท, OTP 6 หลัก, Kill-Switch และแจ้งเตือนระหว่างวันย้ายไปอยู่ใน HomeApp MiniApp แล้ว",
            "color": FG,
            "size": "sm",
            "wrap": True,
        },
        {
            "type": "text",
            "text": "LINE OA @678clhio สงวนไว้สำหรับเปิด MiniApp เท่านั้น (สรุปปิดตลาด EOD บันทึกใน MiniApp Inbox เวลา 17:05 น.)",
            "color": MUTED,
            "size": "xs",
            "wrap": True,
            "margin": "sm",
        },
    ]
    if reason:
        body.append({"type": "text", "text": reason, "color": WARN, "size": "xs", "wrap": True, "margin": "sm"})

    url = _resolve_miniapp_url(tab)
    return _flex(
        "เปิด HomeApp MiniApp",
        _bubble(
            title="📱 HomeApp MiniApp",
            badge="MINIAPP",
            badge_bg="#0284c7",
            body=body,
            footer=[_uri_button("🚀 เปิด HomeApp MiniApp", url, color="#0284c7")],
        ),
    )


def eod_summary_card(
    *,
    date_str: str,
    status_kind: str,
    matched_orders: List[Dict[str, Any]],
    open_orders: List[Dict[str, Any]],
    broker_states: Dict[str, Dict[str, Any]],
    nego_summary: Optional[Dict[str, Any]] = None,
    net_equity: Optional[float] = None,
) -> Dict[str, Any]:
    """Single Flex Bubble summarizing daily EOD orders & Net Equity at 17:05 BKK."""
    badge_map = {
        "matched": (f"MATCHED {len(matched_orders)}", "#16a34a"),
        "open_only": (f"OPEN {len(open_orders)}", "#d97706"),
        "no_orders": ("NO ORDERS", "#475569"),
        "session_unavailable": ("CDP DOWN", "#dc2626"),
    }
    badge_text, badge_bg = badge_map.get(status_kind, ("EOD", "#334155"))

    body: List[Dict[str, Any]] = []
    if status_kind == "session_unavailable":
        body.append(
            {
                "type": "text",
                "text": "⚠️ ไม่สามารถเชื่อมต่อ Session โบรกเกอร์ (CDP) ณ 17:05 จึงไม่สามารถยืนยันรายการ Matched ได้",
                "color": DOWN,
                "size": "sm",
                "weight": "bold",
                "wrap": True,
            }
        )
        for b_key, info in (broker_states or {}).items():
            err = info.get("error") or info.get("state") or "unavailable"
            body.append(_cell(f"• {str(b_key).upper()}: {err}", MUTED, wrap=True, size="xs"))
        target_tab = "brokers"
    else:
        body.append(_kv("Matched วันนี้", f"{len(matched_orders)} รายการ", UP if matched_orders else MUTED))
        body.append(_kv("ค้าง / ไม่ Matched", f"{len(open_orders)} รายการ", WARN if open_orders else MUTED))
        target_tab = "orders"

        if matched_orders:
            body.append({"type": "separator", "margin": "sm", "color": "#334155"})
            for od in matched_orders[:8]:
                sym = str(od.get("symbol") or "?")
                side = str(od.get("side") or "").upper()
                vol = od.get("matched_volume") or od.get("volume") or 0
                px = od.get("price")
                px_str = f"฿{float(px):.2f}" if px not in (None, "") else "MP"
                brk = str(od.get("broker") or "").upper()
                body.append(
                    _row(
                        [
                            _cell(f"{sym} ({brk})", FG, flex=3, weight="bold", wrap=True),
                            _cell(f"{side} {int(vol):,} @ {px_str}", UP, flex=4, align="end"),
                        ]
                    )
                )
            if len(matched_orders) > 8:
                body.append(
                    _cell(
                        f"...และอีก {len(matched_orders) - 8} รายการ ดูทั้งหมดใน MiniApp",
                        MUTED,
                        wrap=True,
                        size="xs",
                    )
                )
        elif open_orders:
            body.append({"type": "separator", "margin": "sm", "color": "#334155"})
            body.append(_cell("ไม่มี Matched วันนี้ — รายการที่ยังไม่ Matched:", WARN, wrap=True))
            for od in open_orders[:4]:
                sym = str(od.get("symbol") or "?")
                side = str(od.get("side") or "").upper()
                vol = od.get("balance") or od.get("volume") or 0
                px = od.get("price")
                px_str = f"฿{float(px):.2f}" if px not in (None, "") else "MP"
                st = str(od.get("status") or "OPEN")
                body.append(_cell(f"• {sym} {side} {int(vol):,} @ {px_str} ({st})", MUTED, wrap=True))
        else:
            body.append(
                {
                    "type": "text",
                    "text": "ไม่มีคำสั่งซื้อ-ขายวันนี้ (0 Matched / 0 Open)",
                    "color": MUTED,
                    "size": "xs",
                    "wrap": True,
                    "margin": "sm",
                }
            )

        degraded = [b for b, info in (broker_states or {}).items() if not info.get("available")]
        if degraded:
            body.append(
                _cell(
                    f"⚠️ หมายเหตุ: ไม่สามารถดึงข้อมูลจาก {', '.join(b.upper() for b in degraded)}",
                    WARN,
                    wrap=True,
                )
            )

    if net_equity is not None:
        body.append(_kv("Net Equity รวม", f"฿{float(net_equity):,.2f}", FG))

    if nego_summary:
        amended = int(nego_summary.get("amended") or 0)
        runs_done = int(nego_summary.get("runs_done") or 0)
        body.append(
            _cell(
                f"🤖 Auto-Nego วันนี้: ทำงาน {runs_done} รอบ · ต่อราคา {amended} รายการ",
                MUTED,
                wrap=True,
            )
        )

    url = _resolve_miniapp_url(target_tab)
    return _flex(
        f"สรุปปิดตลาด {date_str}: Matched {len(matched_orders)} / Open {len(open_orders)}",
        _bubble(
            title=f"📊 สรุปปิดตลาด {date_str}",
            badge=badge_text,
            badge_bg=badge_bg,
            body=body,
            footer=[_uri_button("📋 เปิด MiniApp ดูรายการเต็ม", url, color="#0284c7")],
        ),
    )


__all__ = [
    "quick_reply",
    "status_card",
    "port_card",
    "cash_card",
    "orders_card",
    "orders_cancel_cards",
    "orders_manage_cards",
    "orders_empty_card",
    "order_cancel_confirm_card",
    "order_cancel_result_card",
    "nego_card",
    "negotiate_card",
    "trade_confirmation_card",
    "trade_receipt_card",
    "sbl_status_card",
    "sbl_pool_card",
    "sbl_borrow_confirm_card",
    "sbl_return_confirm_card",
    "sbl_result_card",
    "quick_trade_selector_card",
    "nego_menu_card",
    "nego_draft_card",
    "size_calc_card",
    "miniapp_launcher_card",
    "eod_summary_card",
    "carousel",
]


