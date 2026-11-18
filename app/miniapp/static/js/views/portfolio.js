import { miniappApi } from "../api.js";
import {
  esc,
  fmtInt,
  fmtMoney,
  fmtSignedPct,
  renderErrorRetryCard,
  renderSignedMoney,
  runLockedButton,
} from "../utils.js";

let _state = {
  brokerFilter: "all",
  typeFilter: "all",
  summaryExpanded: false,
  expandedRowKey: null,
  data: null,
  eodHistory: null,
  chartRange: "1M",
  chartSelectedIdx: null,
  loading: false,
  error: "",
  stale: false,
  lastUpdatedAt: "",
};

function _nowTimeBkk() {
  try {
    return new Date().toLocaleTimeString("th-TH", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
  } catch (_err) {
    return "";
  }
}

export function markPortfolioStale() {
  _state.stale = true;
}

export function getLatestPortfolioEquity() {
  const sum = _state.data && _state.data.summary;
  if (!sum || sum.total_net_equity === null || sum.total_net_equity === undefined) return null;
  const val = Number(sum.total_net_equity);
  if (!Number.isFinite(val)) return null;
  return {
    net_equity_thb: val,
    partial: Boolean(sum.partial),
    as_of: _state.lastUpdatedAt || null,
  };
}

function resolvePortAccount(broker, ptype, activeKssAcct, rowAccountNo = null, rowAccountType = null, kssAccounts = []) {
  const b = String(broker || "kss").toLowerCase();
  if (b === "lib") return "cash";
  if (ptype === "SHORT" || ptype === "BORROWED") return "margin";
  const rType = String(rowAccountType || "").toUpperCase();
  if (rType.includes("CASH")) return "cash";
  if (rType.includes("CREDIT") || rType.includes("MARGIN")) return "margin";
  const acctNo = String(rowAccountNo || activeKssAcct || "").trim();
  const matched = (Array.isArray(kssAccounts) ? kssAccounts : []).find((a) => String(a.account_no || "").trim() === acctNo);
  if (matched) {
    const lbl = String(matched.label || matched.account_type || "").toLowerCase();
    if (lbl.includes("cash")) return "cash";
    if (lbl.includes("margin") || lbl.includes("credit")) return "margin";
  }
  return "margin";
}

function collectAllRows(d) {
  const brokers = (d && d.brokers) || {};
  const accounts = Array.isArray(d && d.accounts) ? d.accounts : [];
  const rows = [];
  if (accounts.length > 0) {
    const seen = new Set();
    for (const acc of accounts) {
      if (!acc || !acc.available) continue;
      for (const r of acc.rows || []) {
        const key = `${r.broker || acc.broker}:${acc.account_no || r.account_no || ""}:${r.position_type || "LONG"}:${r.symbol || ""}`;
        if (seen.has(key)) continue;
        seen.add(key);
        rows.push({
          ...r,
          broker: r.broker || acc.broker,
          account_no: r.account_no || acc.account_no || null,
          account_type: r.account_type || acc.account_type || null,
        });
      }
    }
    const kssBook = brokers.kss;
    if (kssBook && kssBook.available) {
      for (const r of kssBook.borrowed || []) {
        rows.push({ ...r, broker: r.broker || "kss", position_type: "BORROWED" });
      }
    }
    return rows;
  }
  for (const key of ["kss", "lib"]) {
    const b = brokers[key];
    if (!b || !b.available) continue;
    for (const r of b.rows || []) {
      rows.push({ ...r, broker: r.broker || key, account_no: b.account_no || null });
    }
    for (const r of b.borrowed || []) {
      rows.push({ ...r, broker: r.broker || key, position_type: "BORROWED" });
    }
  }
  return rows;
}

function computeBookNetEquity(book) {
  if (!book || !book.available) return null;
  if (book.net_equity !== null && book.net_equity !== undefined && !Number.isNaN(Number(book.net_equity))) {
    return Number(book.net_equity);
  }
  if (book.cash_balance === null || book.cash_balance === undefined || Number.isNaN(Number(book.cash_balance))) {
    return null;
  }
  let longMv = 0;
  let shortLiab = 0;
  for (const r of book.rows || []) {
    const pt = String(r.position_type || "LONG").toUpperCase();
    if (pt === "BORROWED") continue;
    let mv = r.market_value !== null && r.market_value !== undefined ? Number(r.market_value) : null;
    if (mv === null || Number.isNaN(mv)) {
      const vol = Number(r.volume || 0);
      const last = Number(r.last || 0);
      if (vol > 0 && last > 0) mv = vol * last;
      else mv = null;
    }
    if (mv === null || Number.isNaN(mv)) continue;
    if (pt === "SHORT") shortLiab += Math.abs(mv);
    else longMv += mv;
  }
  return Number(book.cash_balance) + longMv - shortLiab;
}

function resolveTotalNetEquity(d) {
  const sum = (d && d.summary) || {};
  if (sum.total_net_equity !== null && sum.total_net_equity !== undefined && !Number.isNaN(Number(sum.total_net_equity))) {
    return Number(sum.total_net_equity);
  }
  const source = Array.isArray(d && d.accounts) && d.accounts.length
    ? d.accounts
    : Object.values((d && d.brokers) || {});
  let total = 0;
  let hasAny = false;
  for (const b of source) {
    const eq = computeBookNetEquity(b);
    if (eq !== null) {
      total += eq;
      hasAny = true;
    }
  }
  return hasAny ? total : null;
}

function calcRowPnlThb(row, ptype) {
  if (row.unrealized_pnl !== null && row.unrealized_pnl !== undefined && !Number.isNaN(Number(row.unrealized_pnl))) {
    return Number(row.unrealized_pnl);
  }
  const vol = Number(row.volume || 0);
  const cost = Number(row.cost || 0);
  const last = Number(row.last || 0);
  if (ptype === "BORROWED" || vol <= 0 || cost <= 0 || last <= 0) return null;
  return (ptype === "SHORT" ? cost - last : last - cost) * vol;
}

function calcAggregatePnlPct(allRows, totalPnl) {
  if (totalPnl === null || totalPnl === undefined || Number.isNaN(Number(totalPnl))) return null;
  let totalCost = 0;
  for (const r of allRows) {
    const ptype = String(r.position_type || "LONG").toUpperCase();
    const vol = Number(r.volume || 0);
    const cost = Number(r.cost || 0);
    if (ptype !== "BORROWED" && vol > 0 && cost > 0) {
      totalCost += Math.abs(vol * cost);
    }
  }
  return totalCost > 0 ? (Number(totalPnl) / totalCost) * 100 : null;
}

function formatAccountTypeLabel(accountType) {
  const raw = String(accountType || "").trim().toUpperCase();
  if (raw === "CREDIT_BALANCE" || raw === "MARGIN") return "MARGIN";
  if (raw === "CASH_BALANCE" || raw === "CASH") return "CASH";
  return raw || "OK";
}

function renderBrokerBreakdownTile(key, book) {
  const shortName = key === "lib" || key === "lib_cash" ? "LIB" : "KSS";
  const titleHint = (book && book.label) || shortName;
  const roles = Array.isArray(book && book.routing_roles) ? book.routing_roles : [];
  const roleBadge =
    book && book.selected_in_routing === false
      ? `<span class="pill pill-muted">ไม่ได้เลือก</span>`
      : roles.length
        ? `<span class="pill pill-sky">${esc(roles.join("/"))}</span>`
        : "";
  if (!book || !book.available) {
    return `
      <div class="port-brk-box">
        <div class="card-head">
          <strong>${esc(titleHint)}${book && book.account_no ? ` · ${esc(book.account_no)}` : ""}</strong>
          <span class="pill pill-red">OFFLINE</span>
        </div>
        <div class="muted" style="font-size:11px">${esc((book && book.error) || "Session ไม่พร้อม")}</div>
        <button class="chip-btn" data-jump-brokers="1" type="button" style="min-height:36px;margin-top:4px">🔑 Login</button>
      </div>
    `;
  }
  const acctLabel = formatAccountTypeLabel(book.account_type);
  const netEq = computeBookNetEquity(book);
  return `
    <div class="port-brk-box">
      <div class="card-head">
        <strong>${shortName} · ${esc(book.account_no || "-")}</strong>
        <div class="field-row" style="gap:4px">
          ${roleBadge}
          <span class="pill pill-green">${esc(acctLabel)}</span>
        </div>
      </div>
      <div class="field-row" style="justify-content:space-between;gap:8px;font-size:11.5px">
        <span>Net Eq: <strong style="color:var(--text)">฿${fmtMoney(netEq)}</strong></span>
        <span class="muted">Cash: ฿${fmtMoney(book.cash_balance)}</span>
      </div>
      <div class="muted" style="font-size:10.5px">Line/PP: ฿${fmtMoney(book.line_available)}</div>
    </div>
  `;
}

function renderIntegratedSummary(d, allRows) {
  const sum = d.summary || {};
  const sbl = d.sbl || {};
  const brokers = d.brokers || {};
  const netEquityVal = resolveTotalNetEquity(d);
  const selectedLabels = Array.isArray(sum.selected_account_labels) ? sum.selected_account_labels : [];
  const pnl = renderSignedMoney(sum.total_unrealized_pnl);
  const aggPct = calcAggregatePnlPct(allRows, sum.total_unrealized_pnl);
  const sblSource = String(sbl.source || "unavailable").toUpperCase();
  const sblCount = Array.isArray(sbl.lots) ? sbl.lots.length : Number(sbl.lots || 0);
  const sblLotsTag = Number.isFinite(sblCount) && sblCount > 0 ? ` · ${fmtInt(sblCount)} lot` : "";
  const sblPillCls =
    sblSource === "LIVE" ? "pill-green" : sblSource === "CACHE" ? "pill-amber" : "pill-muted";

  const activeKss = (brokers.kss && brokers.kss.account_no) || d.kss_active_account || "";
  const kssAccts = Array.isArray(d.kss_accounts) ? d.kss_accounts : [];
  const sblActiveCount = allRows.filter((r) => {
    const t = String(r.position_type || "").toUpperCase();
    return Number(r.borrow_qty || 0) > 0 || t === "SHORT" || t === "BORROWED";
  }).length;
  const breakdownBooks =
    Array.isArray(d.accounts) && d.accounts.length
      ? d.accounts.map((a) => [a.key || a.broker, a])
      : [
          ["kss", brokers.kss],
          ["lib", brokers.lib],
        ];

  return `
    <section class="card card-summary port-summary-card">
      <div class="card-head">
        <div class="field-row" style="gap:6px;min-width:0;flex-wrap:wrap">
          <div class="card-title">💼 Integrated Port</div>
          ${_state.lastUpdatedAt ? `<span class="muted" style="font-size:11px">· ${esc(_state.lastUpdatedAt)}</span>` : ""}
          ${sum.partial ? `<span class="pill pill-amber">⚠️ บางบัญชีออฟไลน์</span>` : ""}
        </div>
        <div class="field-row" style="gap:6px;flex-shrink:0">
          <button
            id="port-toggle-details-btn"
            class="port-adv-toggle-btn ${_state.summaryExpanded ? "active" : ""}"
            type="button"
            aria-expanded="${_state.summaryExpanded ? "true" : "false"}"
            aria-label="จัดการบัญชีและรายละเอียดพอร์ต"
            title="รายละเอียด KSS / LIB และสลับบัญชี"
          >${_state.summaryExpanded ? "▲ ซ่อน" : "⚙️ จัดการบัญชี"}</button>
          <button
            id="port-sync-btn"
            class="port-icon-btn ${_state.loading ? "is-spinning" : ""}"
            type="button"
            aria-label="Sync สด"
            title="Sync พอร์ตล่าสุด"
            ${_state.loading ? "disabled aria-busy=\"true\"" : ""}
          ><span class="port-sync-glyph">↻</span></button>
        </div>
      </div>

      <div class="port-kpi-3">
        <div class="port-kpi-cell" title="มูลค่าสุทธิเมื่อปิดทุกสถานะวันนี้และถอนเงินได้จริง เฉพาะพอร์ตที่เลือกในกลยุทธ์">
          <span class="port-kpi-label">Net Equity รวม</span>
          <strong class="port-kpi-val" id="port-net-equity-val">฿${fmtMoney(netEquityVal)}</strong>
          <span class="port-kpi-sub muted">Cash ฿${fmtMoney(sum.total_cash_balance)} · Line ฿${fmtMoney(sum.total_line_available)}</span>
        </div>
        <div class="port-kpi-cell">
          <span class="port-kpi-label">Total P/L</span>
          <strong class="port-kpi-val ${pnl.cls}">${pnl.text}</strong>
          <span class="port-kpi-sub ${pnl.cls}">${aggPct !== null ? fmtSignedPct(aggPct) : "—"}</span>
        </div>
        <div class="port-kpi-cell">
          <span class="port-kpi-label">Open</span>
          <strong class="port-kpi-val">${fmtInt(allRows.length)}</strong>
          <span class="port-kpi-sub muted">${sblActiveCount > 0 ? `${fmtInt(sblActiveCount)} SBL` : d.dry_run ? "🧪 DRY" : "🔴 LIVE"}</span>
        </div>
      </div>
      ${
        selectedLabels.length
          ? `<div class="muted" id="port-selected-scope" style="font-size:11px;margin-top:4px">🎯 รวมเฉพาะพอร์ตที่เลือก: ${esc(selectedLabels.join(" + "))}</div>`
          : ""
      }

      ${
        _state.summaryExpanded
          ? `
        <div class="port-summary-drawer">
          <div class="muted" style="font-size:11px">💡 Net Equity = เงินสด + หุ้น Long − ภาระซื้อคืน Short (ไม่รวมวงเงิน Margin / หุ้นยืม B)</div>
          <div class="port-brk-strip">
            ${breakdownBooks.map(([k, bk]) => renderBrokerBreakdownTile(k, bk)).join("")}
          </div>
          <div class="card-head">
            <span class="pill ${sblPillCls}">SBL: ${esc(sblSource)}${sblLotsTag}</span>
            <span class="pill ${d.dry_run ? "pill-amber" : "pill-red"}">${d.dry_run ? "🧪 DRY-RUN" : "🔴 LIVE"}</span>
          </div>
          <div class="field-row">
            <button class="btn btn-ghost btn-sm" data-jump-brokers type="button">⚙️ สลับบัญชี KSS / ตั้งค่าหลักที่แท็บ Brokers</button>
          </div>
        </div>
      `
          : ""
      }
    </section>
  `;
}

function renderPositionRow(row, idx, activeKssAcct, symLots = [], kssAccounts = []) {
  const sym = esc(row.symbol);
  const rawSym = String(row.symbol || "").toUpperCase();
  const ptype = String(row.position_type || "LONG").toUpperCase();
  const broker = String(row.broker || "kss").toLowerCase();
  const acct = resolvePortAccount(broker, ptype, activeKssAcct, row.account_no, row.account_type, kssAccounts);
  const vol = Number(row.volume || 0);
  const bQty = Number(row.borrow_qty || 0);
  const rQty = Number(row.returnable_qty || 0);
  const refs = symLots.map((l) => l.ref_no || l.lot_id).filter(Boolean);
  const sblPayload = { symbol: rawSym, volume: ptype === "BORROWED" ? Math.max(0, rQty) : rQty, refs };
  const rowKey = `${broker}:${ptype}:${rawSym}:${idx}`;
  const isExpanded = _state.expandedRowKey === rowKey;

  const pnlPct = Number(row.pnl_pct);
  const hasPct = row.pnl_pct !== null && row.pnl_pct !== undefined && !Number.isNaN(pnlPct);
  const pnlPctCls = hasPct ? (pnlPct >= 0 ? "txt-green" : "txt-red") : "muted";
  const pnlThb = calcRowPnlThb(row, ptype);
  const pnlThbObj = renderSignedMoney(pnlThb);

  const rowTheme =
    ptype === "BORROWED"
      ? { rowCls: "row-borrowed", tagCls: "tag-b", tagText: "B" }
      : ptype === "SHORT"
        ? { rowCls: "row-short", tagCls: "tag-s", tagText: "S" }
        : { rowCls: "row-long", tagCls: "tag-l", tagText: "L" };

  const isCoveredSbl = ptype === "BORROWED" && Boolean(row.covered_from_short);
  let quickIconHtml = "";
  let drawerActionsHtml = "";
  if (ptype === "SHORT") {
    const tradeGo = esc(JSON.stringify({ side: "cover", symbol: rawSym, broker: "kss", account: "margin", volume: Math.max(100, vol) }));
    quickIconHtml = `<button class="port-icon-btn act-cover" data-trade-go='${tradeGo}' type="button" title="Cover ${sym}" aria-label="Cover ${sym}">🛡</button>`;
    drawerActionsHtml = `
      <button class="btn btn-success" data-trade-go='${tradeGo}' type="button">🛡 Cover ${fmtInt(vol)}</button>
      ${
        rQty > 0
          ? `<button class="btn btn-amber" data-sbl-return='${esc(JSON.stringify(sblPayload))}' type="button">🔄 คืน ${fmtInt(rQty)}</button>`
          : ""
      }
    `;
  } else if (ptype === "BORROWED") {
    const tradeGo = esc(JSON.stringify({ side: "short", symbol: rawSym, broker: "kss", account: "margin", volume: Math.max(100, bQty) }));
    if (isCoveredSbl) {
      quickIconHtml = `<button class="port-icon-btn act-cover" data-sbl-return='${esc(JSON.stringify(sblPayload))}' ${rQty < 100 ? "disabled" : ""} type="button" title="คืนหุ้น SBL ${sym}" aria-label="คืนหุ้น SBL ${sym}">🔄</button>`;
      drawerActionsHtml = `
        <button class="btn btn-amber" data-sbl-return='${esc(JSON.stringify(sblPayload))}' ${rQty < 100 ? "disabled" : ""} type="button">🔄 คืน ${fmtInt(rQty)}</button>
        <button class="btn btn-ghost" data-trade-go='${tradeGo}' type="button">⚡ Short เพิ่ม ${fmtInt(bQty)}</button>
      `;
    } else {
      quickIconHtml = `<button class="port-icon-btn act-short" data-trade-go='${tradeGo}' type="button" title="Short ${sym}" aria-label="Short ${sym}">⚡</button>`;
      drawerActionsHtml = `
        <button class="btn btn-purple" data-trade-go='${tradeGo}' type="button">⚡ Short ${fmtInt(bQty)}</button>
        <button class="btn btn-amber" data-sbl-return='${esc(JSON.stringify(sblPayload))}' ${rQty < 100 ? "disabled" : ""} type="button">🔄 คืน ${fmtInt(rQty)}</button>
      `;
    }
  } else {
    const tradeGo = esc(JSON.stringify({ side: "sell", symbol: rawSym, broker, account: acct, volume: Math.max(100, vol) }));
    quickIconHtml = `<button class="port-icon-btn act-sell" data-trade-go='${tradeGo}' type="button" title="Sell ${sym}" aria-label="Sell ${sym}">💸</button>`;
    drawerActionsHtml = `<button class="btn btn-danger" data-trade-go='${tradeGo}' type="button">💸 Sell ${fmtInt(vol)}</button>`;
  }

  const pnlColHtml =
    ptype === "BORROWED"
      ? isCoveredSbl
        ? `
          <span class="port-pnl-pct txt-green">✅ Covered</span>
          <span class="port-pnl-thb txt-amber">รอคืน ${fmtInt(rQty)}</span>
        `
        : `
          <span class="port-pnl-pct txt-amber">SBL (B)</span>
          <span class="port-pnl-thb">คืนได้ ${fmtInt(rQty)}</span>
        `
      : `
        <span class="port-pnl-pct ${pnlPctCls}">${hasPct ? fmtSignedPct(pnlPct) : "—"}</span>
        <span class="port-pnl-thb ${pnlThb !== null ? pnlThbObj.cls : "muted"}">${pnlThb !== null ? pnlThbObj.text : "—"}</span>
      `;

  return `
    <div class="port-row-wrap ${rowTheme.rowCls}" data-ptype="${esc(ptype)}" data-broker="${esc(broker)}" data-sym="${sym}">
      <div class="port-row" data-row-toggle="${esc(rowKey)}">
        <div class="port-ticker-col">
          <div class="port-ticker-main">
            <strong class="port-sym">${sym}</strong>
            <span class="tag-mini ${rowTheme.tagCls}">${rowTheme.tagText}</span>
            <span class="tag-mini tag-brk">${esc(broker.toUpperCase())}</span>
            ${isCoveredSbl ? `<span class="pill pill-green" style="font-size:10px;padding:1px 6px">รอคืน SBL</span>` : ""}
          </div>
          <div class="port-sub">
            ${fmtInt(ptype === "BORROWED" ? bQty || vol : vol)} หุ้น${row.cost ? ` · @${fmtMoney(row.cost)}` : row.last ? ` · @${fmtMoney(row.last)}` : ""}
          </div>
        </div>
        <div class="port-pnl-col">
          ${pnlColHtml}
        </div>
        <div class="port-action-col" data-sym-actions="${sym}">
          ${quickIconHtml}
          <button
            class="port-icon-btn ${isExpanded ? "active" : ""}"
            data-expand-row="${esc(rowKey)}"
            type="button"
            aria-expanded="${isExpanded ? "true" : "false"}"
            aria-label="รายละเอียดและคำสั่งเพิ่มเติม ${sym}"
            title="เพิ่มเติม"
          >⋯</button>
        </div>
      </div>
      ${
        isExpanded
          ? `
        <div class="port-row-drawer">
          <div class="port-drawer-stats">
            <div class="port-drawer-stat"><small>จำนวน</small><strong>${fmtInt(ptype === "BORROWED" ? bQty || vol : vol)}</strong></div>
            <div class="port-drawer-stat"><small>ทุน (Avg)</small><strong>${row.cost ? `฿${fmtMoney(row.cost)}` : "—"}</strong></div>
            <div class="port-drawer-stat"><small>ล่าสุด</small><strong>${row.last ? `฿${fmtMoney(row.last)}` : "—"}</strong></div>
          </div>
          ${
            bQty > 0
              ? `<div class="muted" style="font-size:11px">${isCoveredSbl ? "✅ Cover ครบแล้ว — รอคืนหุ้น SBL · " : ""}บัญชี ${esc(broker.toUpperCase())}·${esc(acct.toUpperCase())} · ยืม SBL ${fmtInt(bQty)} / คืนได้ ${fmtInt(rQty)}${row.borrow_fee_rate ? ` · ค่าธรรมเนียม ${row.borrow_fee_rate}%/ปี` : ""}${refs.length ? ` · สัญญา ${esc(refs.slice(0, 3).join(", "))}${refs.length > 3 ? ` (+${refs.length - 3})` : ""}` : ""}</div>`
              : `<div class="muted" style="font-size:11px">บัญชี ${esc(broker.toUpperCase())} · ${esc(acct.toUpperCase())}</div>`
          }
          <div class="port-drawer-actions">${drawerActionsHtml}</div>
        </div>
      `
          : ""
      }
    </div>
  `;
}

function filterEodChartEntries(entries, rangeKey) {
  const valid = (Array.isArray(entries) ? entries : []).filter(
    (e) => e && e.date && e.total_net_equity !== null && e.total_net_equity !== undefined && Number.isFinite(Number(e.total_net_equity))
  );
  if (!valid.length) return [];
  const daysBack = rangeKey === "7D" ? 7 : rangeKey === "3M" ? 90 : 30;
  const latestMs = Date.parse(`${valid[valid.length - 1].date}T00:00:00Z`);
  if (!Number.isFinite(latestMs)) return valid;
  const cutoffMs = latestMs - daysBack * 86400000;
  return valid.filter((e) => {
    const ms = Date.parse(`${e.date}T00:00:00Z`);
    return Number.isFinite(ms) ? ms >= cutoffMs : true;
  });
}

function fmtShortThb(val) {
  const n = Number(val);
  if (!Number.isFinite(n)) return "—";
  if (Math.abs(n) >= 1000000) return `฿${(n / 1000000).toFixed(2)}M`;
  if (Math.abs(n) >= 1000) return `฿${Math.round(n / 1000)}k`;
  return `฿${fmtInt(n)}`;
}

function renderPortfolioEodChart() {
  const rawEntries = (_state.eodHistory && _state.eodHistory.entries) || [];
  const pts = filterEodChartEntries(rawEntries, _state.chartRange);
  const ranges = ["7D", "1M", "3M"];
  const rangeBtnsHtml = ranges
    .map(
      (rk) =>
        `<button type="button" class="port-chart-range-btn ${_state.chartRange === rk ? "active" : ""}" data-chart-range="${rk}">${rk}</button>`
    )
    .join("");

  if (!pts.length) {
    return `
      <section id="port-eod-chart-card" class="card port-chart-card">
        <div class="card-head">
          <div class="card-title">📈 กราฟ Portfolio (EOD Net Equity)</div>
          <div class="port-chart-ranges" role="group" aria-label="ช่วงเวลา">${rangeBtnsHtml}</div>
        </div>
        <div class="muted" style="font-size:12px;padding:10px 0">ยังไม่มีข้อมูล EOD ในช่วงเวลานี้ (ระบบบันทึกอัตโนมัติทุกวันทำการ 17:05 น. หรือกดรันที่แท็บ Schedule)</div>
      </section>
    `;
  }

  const firstVal = Number(pts[0].total_net_equity);
  const lastVal = Number(pts[pts.length - 1].total_net_equity);
  const delta = lastVal - firstVal;
  const deltaPct = firstVal > 0 ? (delta / firstVal) * 100 : 0;
  const deltaCls = pts.length < 2 ? "pill-muted" : delta >= 0 ? "pill-green" : "pill-red";
  const deltaText =
    pts.length < 2
      ? `วันแรกที่บันทึก`
      : `${delta >= 0 ? "+" : "-"}฿${fmtMoney(Math.abs(delta))} (${deltaPct >= 0 ? "+" : ""}${deltaPct.toFixed(2)}%)`;

  const activeIdx =
    _state.chartSelectedIdx !== null && _state.chartSelectedIdx >= 0 && _state.chartSelectedIdx < pts.length
      ? _state.chartSelectedIdx
      : pts.length - 1;
  const activePt = pts[activeIdx];

  const vals = pts.map((p) => Number(p.total_net_equity));
  const rawMin = Math.min(...vals);
  const rawMax = Math.max(...vals);
  const span = rawMax - rawMin;
  const pad = span > 0 ? span * 0.16 : Math.max(1000, rawMax * 0.02);
  const minY = Math.max(0, rawMin - pad);
  const maxY = rawMax + pad;
  const yRange = maxY - minY || 1;

  const W = 360;
  const H = 128;
  const L = 46;
  const R = 12;
  const T = 12;
  const B = 22;
  const plotW = W - L - R;
  const plotH = H - T - B;

  const coords = pts.map((p, idx) => {
    const x = pts.length === 1 ? L + plotW / 2 : L + (idx / (pts.length - 1)) * plotW;
    const y = T + (1 - (Number(p.total_net_equity) - minY) / yRange) * plotH;
    return { x: Number(x.toFixed(1)), y: Number(y.toFixed(1)), pt: p, idx };
  });

  const lineColor = pts.length >= 2 && delta < 0 ? "#fb7185" : "#38bdf8";
  const linePtsAttr = coords.map((c) => `${c.x},${c.y}`).join(" ");
  const firstC = coords[0];
  const lastC = coords[coords.length - 1];
  const areaPath =
    coords.length >= 2
      ? `M ${firstC.x} ${T + plotH} L ${coords.map((c) => `${c.x} ${c.y}`).join(" L ")} L ${lastC.x} ${T + plotH} Z`
      : "";
  const activeC = coords[activeIdx];
  const midYVal = (minY + maxY) / 2;

  const kssM = activePt.kss_margin_net_equity;
  const libC = activePt.lib_cash_net_equity;
  const splitParts = [];
  if (kssM !== null && kssM !== undefined) splitParts.push(`KSS Margin ฿${fmtInt(kssM)}`);
  if (libC !== null && libC !== undefined) splitParts.push(`LIB Cash ฿${fmtInt(libC)}`);

  return `
    <section id="port-eod-chart-card" class="card port-chart-card">
      <div class="card-head">
        <div>
          <div class="card-title">📈 กราฟ Portfolio (EOD)</div>
          <div class="field-row" style="gap:6px;margin-top:2px;flex-wrap:wrap">
            <strong style="font-size:14px">฿${fmtMoney(lastVal)}</strong>
            <span class="pill ${deltaCls}" style="font-size:10.5px">${esc(deltaText)}</span>
          </div>
        </div>
        <div class="port-chart-ranges" role="group" aria-label="ช่วงเวลากราฟ">${rangeBtnsHtml}</div>
      </div>

      <div class="port-chart-svg-wrap">
        <svg class="port-chart-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="กราฟ Net Equity รายวันจาก EOD">
          <defs>
            <linearGradient id="portEqGrad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stop-color="${lineColor}" stop-opacity="0.34" />
              <stop offset="100%" stop-color="${lineColor}" stop-opacity="0.02" />
            </linearGradient>
          </defs>
          <line x1="${L}" y1="${T}" x2="${W - R}" y2="${T}" class="port-chart-grid" />
          <line x1="${L}" y1="${(T + plotH / 2).toFixed(1)}" x2="${W - R}" y2="${(T + plotH / 2).toFixed(1)}" class="port-chart-grid" />
          <line x1="${L}" y1="${T + plotH}" x2="${W - R}" y2="${T + plotH}" class="port-chart-grid" />
          <text x="${L - 5}" y="${T + 4}" text-anchor="end" class="port-chart-ylabel">${esc(fmtShortThb(maxY))}</text>
          <text x="${L - 5}" y="${(T + plotH / 2 + 3).toFixed(1)}" text-anchor="end" class="port-chart-ylabel">${esc(fmtShortThb(midYVal))}</text>
          <text x="${L - 5}" y="${T + plotH + 3}" text-anchor="end" class="port-chart-ylabel">${esc(fmtShortThb(minY))}</text>
          ${
            pts.length === 1
              ? `<line x1="${L}" y1="${activeC.y}" x2="${W - R}" y2="${activeC.y}" stroke="${lineColor}" stroke-width="1.5" stroke-dasharray="4 3" opacity="0.65" />`
              : `<path d="${areaPath}" fill="url(#portEqGrad)" /><polyline fill="none" stroke="${lineColor}" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" points="${linePtsAttr}" />`
          }
          <line x1="${activeC.x}" y1="${T}" x2="${activeC.x}" y2="${T + plotH}" stroke="${lineColor}" stroke-width="1" stroke-dasharray="2 2" opacity="0.7" />
          ${coords
            .map(
              (c) => `
              <circle
                cx="${c.x}"
                cy="${c.y}"
                r="${c.idx === activeIdx ? 4.8 : pts.length <= 15 ? 3.2 : 2.2}"
                fill="${c.idx === activeIdx ? lineColor : "#0f172a"}"
                stroke="${lineColor}"
                stroke-width="2"
                data-eq-idx="${c.idx}"
                style="cursor:pointer"
              ><title>${esc(c.pt.date)}: ฿${esc(fmtMoney(c.pt.total_net_equity))}</title></circle>`
            )
            .join("")}
          <text x="${L}" y="${H - 4}" text-anchor="start" class="port-chart-xlabel">${esc(pts[0].date.slice(5))}</text>
          ${pts.length >= 2 ? `<text x="${W - R}" y="${H - 4}" text-anchor="end" class="port-chart-xlabel">${esc(pts[pts.length - 1].date.slice(5))}</text>` : ""}
        </svg>
      </div>

      <div id="port-eod-point-detail" class="port-chart-detail">
        <span><strong>${esc(activePt.date)}</strong> · Net Eq <strong>฿${fmtMoney(activePt.total_net_equity)}</strong></span>
        ${splitParts.length ? `<span class="muted">${esc(splitParts.join(" · "))}</span>` : ""}
      </div>
      ${
        pts.length === 1
          ? `<div class="muted" style="font-size:11px;margin-top:2px">เก็บข้อมูล EOD แล้ว 1 วัน — เส้นแนวโน้มจะแสดงต่อเนื่องเมื่อบันทึกครบ ≥ 2 วันทำการ (17:05 น.)</div>`
          : ""
      }
    </section>
  `;
}

export async function renderPortfolioView(container, ctx) {
  const { toast, confirmSheet, navigate, initialFetch = true, isCurrent = () => true, syncNav = null } = ctx;

  const loadData = async (forceSbl = false) => {
    if (_state.loading) return;
    _state.loading = true;
    _state.error = "";
    paint();
    try {
      const [portRes, histRes] = await Promise.allSettled([
        miniappApi.getPortfolio("all", null, forceSbl),
        miniappApi.getNetEquityHistory(90),
      ]);
      if (histRes.status === "fulfilled" && histRes.value) {
        _state.eodHistory = histRes.value;
        _state.chartSelectedIdx = null;
      }
      if (portRes.status === "rejected") {
        throw portRes.reason;
      }
      _state.data = portRes.value;
      _state.lastUpdatedAt = _nowTimeBkk();
      _state.error = "";
      _state.stale = false;
    } catch (err) {
      _state.error = err?.message || "เชื่อมต่อไม่สำเร็จ";
      toast(`❌ ดึงพอร์ตไม่สำเร็จ: ${_state.error}`, "err");
    } finally {
      _state.loading = false;
    }
    paint();
  };

  const paint = () => {
    if (!isCurrent() || !container.isConnected) return;
    if (syncNav) syncNav();
    const d = _state.data;
    const brokers = (d && d.brokers) || {};
    const kssAccts = Array.isArray(d && d.kss_accounts) ? d.kss_accounts : [];
    const activeKss = (brokers.kss && brokers.kss.account_no) || (d && d.kss_active_account) || "";
    const sblLotsBySym = {};
    const rawSblLots = d && d.sbl && Array.isArray(d.sbl.lots) ? d.sbl.lots : [];
    for (const lot of rawSblLots) {
      const symKey = String(lot.symbol || "").trim().toUpperCase();
      const rem = Number(lot.remaining_qty ?? lot.borrow_qty ?? 0);
      if (!symKey || rem <= 0) continue;
      if (!sblLotsBySym[symKey]) sblLotsBySym[symKey] = [];
      sblLotsBySym[symKey].push({
        lot_id: String(lot.lot_id || lot.broker_tx_id || symKey).trim(),
        ref_no: String(lot.broker_tx_id || lot.lot_id || "").trim(),
        qty: rem,
      });
    }
    const allRows = d ? collectAllRows(d) : [];

    const filteredRows = allRows.filter((r) => {
      const brk = String(r.broker || "kss").toLowerCase();
      if (_state.brokerFilter !== "all" && brk !== _state.brokerFilter) return false;
      if (_state.typeFilter === "sbl") {
        const pt = String(r.position_type || "").toUpperCase();
        return Number(r.borrow_qty || 0) > 0 || pt === "SHORT" || pt === "BORROWED";
      }
      return true;
    });

    const activeChip =
      _state.typeFilter === "sbl"
        ? "sbl"
        : _state.brokerFilter === "kss"
          ? "kss"
          : _state.brokerFilter === "lib"
            ? "lib"
            : "all";

    container.innerHTML = `
      ${
        _state.error
          ? renderErrorRetryCard({
              title: d ? "รีเฟรชพอร์ตไม่สำเร็จ (แสดงข้อมูลล่าสุดเดิม)" : "ดึงข้อมูลพอร์ตไม่สำเร็จ",
              message: _state.error,
              retryId: "port-retry-btn",
            })
          : ""
      }
      ${
        d
          ? renderIntegratedSummary(d, allRows)
          : _state.error
            ? ""
            : `<section class="card"><div class="muted">${_state.loading ? "กำลังดึงข้อมูลพอร์ตจาก CDP..." : "ยังไม่มีข้อมูลพอร์ต"}</div></section>`
      }
      ${renderPortfolioEodChart()}

      ${
        d
          ? `
        <div class="port-filter-strip" role="tablist" aria-label="ตัวกรองพอร์ต">
          <button class="port-chip ${activeChip === "all" ? "active" : ""}" data-qchip="all" type="button">ทั้งหมด (${fmtInt(allRows.length)})</button>
          <button class="port-chip ${activeChip === "kss" ? "active" : ""}" data-qchip="kss" type="button">KSS</button>
          <button class="port-chip ${activeChip === "lib" ? "active" : ""}" data-qchip="lib" type="button">LIB</button>
          <button class="port-chip ${activeChip === "sbl" ? "active" : ""}" data-qchip="sbl" type="button">SBL (B/S)</button>
        </div>

        <section class="card port-table-card">
          ${
            filteredRows.length === 0
              ? `<div class="muted" style="padding:14px 12px">ไม่พบรายการหุ้นในตัวกรองนี้</div>`
              : `
            <div class="port-table">
              <div class="port-thead">
                <span>Ticker</span>
                <span style="text-align:right">PnL (%)</span>
                <span style="text-align:right">Action</span>
              </div>
              ${filteredRows
                .map((row, idx) =>
                  renderPositionRow(row, idx, activeKss, sblLotsBySym[String(row.symbol || "").toUpperCase()] || [], kssAccts)
                )
                .join("")}
            </div>
          `
          }
        </section>
      `
          : ""
      }
    `;

    const detailsBtn = container.querySelector("#port-toggle-details-btn");
    if (detailsBtn) {
      detailsBtn.onclick = () => {
        _state.summaryExpanded = !_state.summaryExpanded;
        paint();
      };
    }

    const syncBtn = container.querySelector("#port-sync-btn");
    if (syncBtn) {
      syncBtn.onclick = () => loadData(true);
    }

    const retryBtn = container.querySelector("#port-retry-btn");
    if (retryBtn) {
      retryBtn.onclick = () => loadData(true);
    }

    container.querySelectorAll("[data-chart-range]").forEach((btn) => {
      btn.onclick = () => {
        _state.chartRange = btn.dataset.chartRange || "1M";
        _state.chartSelectedIdx = null;
        paint();
      };
    });

    container.querySelectorAll("[data-eq-idx]").forEach((dot) => {
      dot.onclick = () => {
        const idx = Number(dot.dataset.eqIdx);
        if (Number.isFinite(idx)) {
          _state.chartSelectedIdx = idx;
          paint();
        }
      };
    });

    container.querySelectorAll("[data-qchip]").forEach((btn) => {
      btn.onclick = () => {
        const mode = btn.dataset.qchip;
        if (mode === "kss" || mode === "lib") {
          _state.brokerFilter = mode;
          _state.typeFilter = "all";
        } else if (mode === "sbl") {
          _state.brokerFilter = "all";
          _state.typeFilter = "sbl";
        } else {
          _state.brokerFilter = "all";
          _state.typeFilter = "all";
        }
        _state.expandedRowKey = null;
        paint();
      };
    });

    container.querySelectorAll("[data-expand-row]").forEach((btn) => {
      btn.onclick = (e) => {
        e.stopPropagation();
        const key = btn.dataset.expandRow;
        _state.expandedRowKey = _state.expandedRowKey === key ? null : key;
        paint();
      };
    });

    container.querySelectorAll("[data-row-toggle]").forEach((rowEl) => {
      rowEl.onclick = (e) => {
        if (e.target.closest("button, a, select, input, [data-sym-actions]")) return;
        const key = rowEl.dataset.rowToggle;
        _state.expandedRowKey = _state.expandedRowKey === key ? null : key;
        paint();
      };
    });

    container.querySelectorAll("[data-jump-brokers]").forEach((btn) => {
      btn.onclick = () => navigate("brokers");
    });

    container.querySelectorAll("[data-trade-go]").forEach((btn) => {
      btn.onclick = () => {
        _state.expandedRowKey = null;
        navigate("trade", JSON.parse(btn.dataset.tradeGo));
      };
    });

    container.querySelectorAll("[data-sbl-return]").forEach((btn) => {
      btn.onclick = () => {
        const payload = JSON.parse(btn.dataset.sblReturn);
        const refs = Array.isArray(payload.refs) ? payload.refs.filter(Boolean) : [];
        const fifoHint =
          refs.length > 1
            ? `FIFO อัตโนมัติ ${refs.length} สัญญา (${refs.slice(0, 3).join(", ")}${refs.length > 3 ? "…" : ""})`
            : refs.length === 1
              ? `สัญญา ${refs[0]}`
              : "FIFO อัตโนมัติ (ค้นหาจากชื่อหุ้น)";
        confirmSheet({
          title: `ยืนยันคืนหุ้น SBL ${payload.symbol}`,
          body: `ขอคืนหุ้นยืม ${payload.symbol} จำนวน ${fmtInt(payload.volume)} หุ้น (${fifoHint} · Krungsri Margin)`,
          confirmLabel: "ยืนยันคืนหุ้น",
          busyTitle: `⏳ กำลังคืนหุ้น SBL ${payload.symbol}...`,
          onConfirm: async () => {
            const res = await miniappApi.sblAction({
              action: "return",
              symbol: payload.symbol,
              volume: payload.volume,
              amount_mode: "volume",
            });
            const retRefs = Array.isArray(res.lots_returned)
              ? res.lots_returned.map((l) => l.ref_no).filter((r) => r && r !== payload.symbol)
              : [];
            const refNote = retRefs.length ? retRefs.join(", ") : "ค้นหาจากชื่อหุ้น";
            try {
              await loadData(true);
            } catch (_ignored) {
              // SBL return succeeded; keep result summary visible even if refresh lags.
            }
            return {
              ok: true,
              dryRun: Boolean(res.dry_run),
              title: res.dry_run ? "🧪 [DRY-RUN] คืนหุ้น SBL สำเร็จ" : "✅ คืนหุ้น SBL สำเร็จ",
              detail: `${payload.symbol} จำนวน ${fmtInt(res.volume)} หุ้น`,
              details: [
                { label: "หุ้น", value: payload.symbol },
                { label: "จำนวนที่คืน", value: `${fmtInt(res.volume)} หุ้น` },
                { label: "Ref.no", value: refNote },
                { label: "โหมด", value: res.dry_run ? "DRY-RUN" : "LIVE (MainSBL)" },
              ],
              ctas: [{ label: "🔄 โหลดพอร์ตซ้ำ", onClick: () => loadData(true) }],
              closeLabel: "ปิด",
            };
          },
        });
      };
    });
  };

  if ((!_state.data || _state.stale) && initialFetch) {
    _state.stale = false;
    await loadData(false);
  } else {
    paint();
  }
}
