import { getLatestPortfolioEquity, markPortfolioStale } from "./portfolio.js";
import { miniappApi } from "../api.js";
import {
  calcBoardLotsFromBudget,
  esc,
  fmtInt,
  fmtMoney,
  formatRunbookMarketPhase,
  getShortUptickTicks,
  newIdempotencyKey,
  nextSetTick,
  quoteRefPrice,
  renderQuickQuoteBox,
  renderRunbookReadinessBar,
  roundToSetTick,
  runLockedButton,
  setTickSize,
} from "../utils.js";

const BUDGET_MULTIPLIERS = [
  { mult: 1, label: "1x" },
  { mult: 1.5, label: "1.5x" },
  { mult: 2, label: "2x" },
];

function getSavedBaseBudget(state = _state) {
  const hubVal = Number(state?.hubData?.suggest_entry?.budget_per_slot_thb || 0);
  if (Number.isFinite(hubVal) && hubVal >= 1000) return Math.round(hubVal);
  const inputVal = Number(state?.seBudgetInput || 0);
  if (Number.isFinite(inputVal) && inputVal >= 1000) return Math.round(inputVal);
  return 50000;
}

function getMultiplierPresets(base) {
  const cleanBase = Math.max(1000, Math.round(Number(base || 50000)));
  return BUDGET_MULTIPLIERS.map((m) => ({
    mult: m.mult,
    label: m.label,
    value: Math.round(cleanBase * m.mult),
  }));
}

function getRecommendedSlotInfo(state = _state) {
  const se = state?.hubData?.suggest_entry;
  const ref = se?.sizing_reference || {};
  const slots = Math.max(1, Number(ref.recommended_slots || se?.quota?.cap_unique_tickers || 7));
  const cachedEq = getLatestPortfolioEquity();
  const netEqRaw = ref.net_equity_thb !== null && ref.net_equity_thb !== undefined
    ? Number(ref.net_equity_thb)
    : cachedEq
      ? Number(cachedEq.net_equity_thb)
      : null;
  if (netEqRaw === null || !Number.isFinite(netEqRaw) || netEqRaw <= 0) return null;
  const backendRec =
    ref.net_equity_thb !== null && ref.net_equity_thb !== undefined && Number(ref.recommended_budget_thb) >= 1000
      ? Math.round(Number(ref.recommended_budget_thb))
      : Math.max(1000, Math.round(netEqRaw / slots / 1000) * 1000);
  const partial =
    ref.net_equity_thb !== null && ref.net_equity_thb !== undefined
      ? Boolean(ref.net_equity_partial)
      : Boolean(cachedEq?.partial);
  return {
    netEquity: netEqRaw,
    recommendedBudget: backendRec,
    slots,
    partial,
  };
}

let _state = {
  subtab: "order", // "order" | "quick" | "sbl"
  side: "buy",
  broker: "lib",
  account: "cash",
  symbol: "",
  amountMode: "volume", // "volume" | "value" | "all"
  volume: "100",
  targetValue: "50000",
  targetValueDirty: false,
  price: "",
  quote: null,
  preview: null,
  idemKey: newIdempotencyKey(),
  orderAdvOpen: false,
  // Quick mode
  quickBudget: 50000,
  quickBudgetDirty: false,
  quickAutoShort: false,
  quickBundleSym: "",
  quickPool: null,
  quickKssQuote: null,
  quickLibQuote: null,
  // SBL mode
  sblAction: "return",
  sblVolume: "",
  sblVolumeFromPrefill: false,
  sblPool: null,
  sblLots: [],
  sblReturnableBySym: {},
  sblLotsLoading: false,
  sblLotsLoaded: false,
  sblLotsAttempted: false,
  sblLotsError: "",
  sblSelectedLotId: "",
  sblRemark: "",
  sblAdvOpen: false,
  // Runbook Auto-Exit preview
  runbookOpen: false,
  runbookLoading: false,
  runbookError: "",
  runbookData: null,
  runbookAllowStale: false,
  runbookSelected: {},
  // Priority Hub & Suggest Entry
  manualAdvOpen: false,
  hubLoading: false,
  hubError: "",
  hubData: null,
  seBudgetInput: "50000",
  seBudgetDirty: false,
  seModalOpen: false,
  sePolling: false,
  sePollError: "",
  sePollData: null,
  seAllowStale: false,
  seOverrideHoldCash: false,
  seSelected: {},
  seFilter: "all", // "all" | "eligible" | "short" | "long"
  seLiveEnabled: true,
  seStreamAbort: null,
  sePollFallbackTimer: null,
  seLastTickAt: "",
  seSubmitting: false,
  seSubmitResult: null,
  statusData: null,
  routingKey: "",
};

const RB_GUARD_META = {
  READY: { text: "พร้อมส่ง", cls: "pill-green" },
  ADJUSTED_VOL: { text: "ปรับตามพอร์ต", cls: "pill-amber" },
  SKIPPED_ALREADY_ORDERED: { text: "มีออเดอร์แล้ว", cls: "pill-muted" },
  SKIPPED_NOT_HELD: { text: "ไม่มีหุ้นในพอร์ต", cls: "pill-muted" },
  SKIPPED_ODD_LOT: { text: "เศษหุ้น < 100", cls: "pill-muted" },
  BROKER_UNAVAILABLE: { text: "Session ไม่พร้อม", cls: "pill-red" },
};

function routingSignature(statusData) {
  const r = (statusData && statusData.account_routing) || {};
  return `${r.short_broker || "kss"}:${r.short_account || "margin"}|${r.long_broker || "lib"}:${r.long_account || "cash"}`;
}

function defaultRouteForSide(side, statusData = _state.statusData) {
  const r = (statusData && statusData.account_routing) || {};
  if (side === "short" || side === "cover") {
    return {
      broker: r.short_broker || "kss",
      account: r.short_account || "margin",
    };
  }
  return {
    broker: r.long_broker || "lib",
    account: r.long_account || "cash",
  };
}

function routeLabel(broker, account) {
  const b = broker === "kss" ? "KSS" : "LIB";
  const a = account === "margin" ? "Margin" : "Cash";
  return `${b} ${a}`;
}

function renderRunbookPanel(state) {
  if (!state.runbookOpen) return "";
  const d = state.runbookData;
  const items = d && Array.isArray(d.items) ? d.items : [];
  const qObj = d && d.schedule && d.schedule.queue;
  const pendingQ = qObj && (qObj.status === "PENDING" || qObj.status === "AUTO_PENDING") ? qObj : null;
  const ksActive = Boolean(d && d.kill_switch && d.kill_switch.is_active);
  const selectable = items.filter((it) => it.guard_status === "READY" || it.guard_status === "ADJUSTED_VOL");
  const selectedSyms = selectable.filter((it) => state.runbookSelected[it.symbol] !== false).map((it) => it.symbol);
  const staleBlocked = Boolean(d && !d.is_today && !state.runbookAllowStale);
  const holidayBlocked = Boolean(d && (d.is_holiday || d.market_phase === "HOLIDAY" || d.market_phase === "WEEKEND"));
  const canSubmit = Boolean(d && d.ledger_available && !ksActive && !holidayBlocked && selectedSyms.length > 0 && !staleBlocked);

  return `
    <section class="card" id="runbook-exit-card" style="display:flex;flex-direction:column;gap:8px">
      <div class="card-head" style="margin:0">
        <div>
          <div class="card-title" style="font-size:13px">📘 Runbook Exit วันนี้</div>
          <div class="muted" style="font-size:11px">KSS Margin / LIB Cash</div>
        </div>
        <div class="field-row">
          <button id="tr-runbook-refresh-btn" class="pill-btn pill-sky" type="button" ${state.runbookLoading ? "disabled" : ""}>🔄</button>
          <button id="tr-runbook-close-btn" class="pill-btn pill-muted" type="button">ซ่อน</button>
        </div>
      </div>

      ${state.runbookLoading && !d
      ? `<div class="muted" style="font-size:12px">⏳ กำลังอ่าน Runbook และเทียบพอร์ตจริง...</div>`
      : state.runbookError
        ? `<div class="txt-red" style="font-size:12px">❌ ${esc(state.runbookError)}</div>`
        : !d
          ? ""
          : `
                ${renderRunbookReadinessBar(d)}
                ${ksActive
            ? `<div class="txt-red" style="font-size:12px">🚨 KILL-SWITCH ACTIVE — ระงับการสร้าง Exit</div>`
            : ""
          }
                ${pendingQ
            ? `<div class="field-row" style="justify-content:space-between;background:rgba(56,189,248,0.08);padding:6px 8px;border-radius:10px">
                         <span style="font-size:11.5px">⏳ คิว${pendingQ.status === "AUTO_PENDING" ? " Auto" : ""} รอสุ่มยิง ${esc(pendingQ.scheduled_time || "—")}</span>
                         <button id="tr-runbook-goto-nego" class="btn btn-ghost btn-xs" type="button">📅 ดู Schedule</button>
                       </div>`
            : ""
          }
                ${!d.ledger_available
            ? `<div class="txt-red" style="font-size:12px">⚠️ ${esc(d.ledger_error || "ไม่พบ positions_ledger.json")}</div>`
            : items.length === 0
              ? `<div class="muted" style="font-size:12px">วันนี้ไม่มีรายการ Exit (SELL / COVER)</div>`
              : `
                        ${!d.is_today
                ? `<label class="toggle-row" for="tr-runbook-allow-stale">
                                 <input id="tr-runbook-allow-stale" type="checkbox" ${state.runbookAllowStale ? "checked" : ""}>
                                 <span>⚠️ ข้อมูล as_of (${esc(d.as_of || "—")}) ไม่ใช่วันนี้ — ยืนยันใช้ข้อมูลย้อนหลัง</span>
                               </label>`
                : ""
              }
                        <div style="display:flex;flex-direction:column;gap:6px">
                          ${items
                .map((it) => {
                  const gm = RB_GUARD_META[it.guard_status] || { text: it.guard_status, cls: "pill-muted" };
                  const canCheck = it.guard_status === "READY" || it.guard_status === "ADJUSTED_VOL";
                  const checked = canCheck && state.runbookSelected[it.symbol] !== false;
                  const sideCls = it.action === "SELL" ? "pill-red" : "pill-amber";
                  return `
                                <div style="border:1px solid var(--border);border-radius:10px;padding:8px">
                                  <div class="field-row" style="justify-content:space-between;margin-bottom:4px">
                                    <label class="field-row" style="gap:6px;cursor:${canCheck ? "pointer" : "default"}">
                                      <input type="checkbox" data-rb-sym="${esc(it.symbol)}" ${checked ? "checked" : ""} ${!canCheck ? "disabled" : ""}>
                                      <strong>${esc(it.symbol)}</strong>
                                      <span class="pill ${sideCls}">${esc(it.action)}</span>
                                    </label>
                                    <span class="pill ${gm.cls}">${esc(gm.text)}</span>
                                  </div>
                                  <div class="muted" style="font-size:11.5px;display:flex;flex-wrap:wrap;gap:8px">
                                    <span>ส่ง: <strong class="${it.net_send_vol > 0 ? "txt-sky" : ""}">${fmtInt(it.net_send_vol)} หุ้น</strong></span>
                                    ${it.lure_price != null ? `<span>Lure: <strong>฿${fmtMoney(it.lure_price)}</strong></span>` : it.quote_ok === false ? `<span class="txt-red">Quote ❌</span>` : ""}
                                    <span>${esc(it.exit_reason || "")}</span>
                                  </div>
                                </div>
                              `;
                })
                .join("")}
                        </div>
                        <button id="tr-runbook-submit-btn" class="btn btn-amber" style="width:100%" type="button" ${canSubmit ? "" : "disabled"}>
                          ⚡ ยิงออเดอร์ Exit (${selectedSyms.length} ไม้)
                        </button>
                      `
          }
              `
    }
    </section>
  `;
}

const QUICK_BUDGET_PRESETS = [
  { value: 50000, label: "50k" },
  { value: 100000, label: "100k" },
  { value: 150000, label: "150k" },
];

function fmtCompactK(val) {
  const n = Number(val || 0);
  if (!Number.isFinite(n) || n <= 0) return "—";
  if (n >= 1000) {
    const k = n / 1000;
    return Number.isInteger(k) ? `${k}k` : `${k.toFixed(1)}k`;
  }
  return fmtInt(n);
}

function formatHubSchedulePill(exData) {
  const rbSched = exData && exData.schedule;
  const label = exData && exData.schedule_status_label;
  const tone = exData && exData.schedule_status_tone;
  if (label) {
    const cls =
      tone === "green"
        ? "pill-green"
        : tone === "sky"
          ? "pill-sky"
          : tone === "amber"
            ? "pill-amber"
            : tone === "red"
              ? "pill-red"
              : "pill-muted";
    return `<span class="pill ${cls}">${esc(label)}</span>`;
  }
  if (!rbSched) return `<span class="pill pill-muted">อัตโนมัติ</span>`;
  const q = rbSched.queue;
  const st = String(q?.status || (rbSched.auto_enabled === false ? "DISABLED" : "PENDING")).toUpperCase();
  if (st === "DONE") return `<span class="pill pill-green">✅ ส่ง Exit แล้ว</span>`;
  if (st === "AUTO_PENDING" || st === "PENDING") {
    return `<span class="pill pill-sky">⏳ นัด ${esc(q?.scheduled_time || "10:00–10:30")}</span>`;
  }
  if (st === "MISSED_WINDOW") return `<span class="pill pill-muted">เลยช่วง Auto</span>`;
  if (st === "DISABLED") return `<span class="pill pill-muted">ปิด Auto</span>`;
  if (st === "NO_EXITS") return `<span class="pill pill-green">ไม่มี Exit</span>`;
  return `<span class="pill pill-muted">${esc(st)}</span>`;
}

function renderTradeHubGrid(state) {
  const h = state.hubData;
  const ex = h && h.auto_exit;
  const se = h && h.suggest_entry;
  const currentBudgetNum = Number(state.seBudgetInput || 0);

  let stripExitTxt = "Exit: กำลังโหลด...";
  let exitDetailHtml = `<div class="muted" style="font-size:12px">⏳ กำลังอ่านรายการ Exit...</div>`;
  if (state.hubError && !h) {
    stripExitTxt = "Exit: โหลดไม่ได้";
    exitDetailHtml = `<div class="txt-red" style="font-size:12px">❌ ${esc(state.hubError)}</div>`;
  } else if (ex) {
    if (!ex.ledger_available) {
      stripExitTxt = "Exit: ไม่พบ Ledger";
      exitDetailHtml = `<div class="txt-red" style="font-size:12px">⚠️ ${esc(ex.error || "ไม่พบ Ledger")}</div>`;
    } else if (!ex.items || ex.items.length === 0) {
      stripExitTxt = "Exit วันนี้: ไม่มี";
      exitDetailHtml = `<div class="muted" style="font-size:12px">✅ วันนี้ไม่มีหุ้นต้อง Exit (${esc(ex.as_of || "—")})</div>`;
    } else {
      const symsShort = ex.items.map((it) => it.symbol).join(", ");
      const symsDetail = ex.items
        .map((it) => `${it.symbol} (${it.action} ${fmtInt(it.shares)})`)
        .join(" · ");
      stripExitTxt = `Exit: ${ex.count} ตัว (${symsShort})`;
      exitDetailHtml = `
        <div style="font-size:12.5px">
          <span class="pill pill-amber">${ex.count} รายการ</span>
          <strong style="margin-left:6px">${esc(symsDetail)}</strong>
          ${!ex.is_today ? ` <span class="pill pill-red">${esc(ex.as_of || "เก่า")}</span>` : ""}
        </div>
      `;
    }
  }

  let stripEntryTxt = "Entry: กำลังโหลด...";
  let entryHeaderPills = `<span class="pill pill-muted">Top 8</span>`;
  let entryMetaLine = `กำลังโหลดข้อมูลโควตา...`;
  if (se) {
    if (!se.ledger_available) {
      stripEntryTxt = "Entry: ไม่พบข้อมูล";
      entryHeaderPills = `<span class="pill pill-red">ไม่มีไฟล์</span>`;
      entryMetaLine = esc(se.error || "ไม่พบ candidates_<DATE>.json");
    } else {
      const q = se.quota || {};
      const reg = se.regime || {};
      const ref = se.sizing_reference || {};
      const mL = Number(q.model_long || 0);
      const mS = Number(q.model_short || 0);
      const remTot = Number(se.remaining_total || 0);
      const free = Number(q.free_slots || 0);
      const remHeld = Number(q.remaining_unique_tickers || 0);
      const cap = Number(q.cap_unique_tickers || 7);

      stripEntryTxt = se.is_hold_cash
        ? `Entry: Hold Cash (ว่าง ${free}/${cap})`
        : `Entry: ${mL}L:${mS}S · ใช้ได้ ${remTot} (ว่าง ${free}/${cap})`;

      const qBadge = se.is_hold_cash
        ? `<span class="pill pill-muted">0L:0S Hold Cash</span>`
        : `<span class="pill ${mS > 0 ? "pill-red" : "pill-green"}">${mL}L:${mS}S · ใช้ได้ ${remTot}</span>`;
      entryHeaderPills = `
        ${qBadge}
        <span class="pill pill-sky">${remHeld}/${cap} ว่าง ${free}</span>
      `;
      entryMetaLine = `${esc(reg.regime_10 || reg.mode || "—")} · Margin ${esc(ref.margin_multiplier ?? 1)}x | HM ${Number(ref.house_money_multiplier ?? 1).toFixed(2)}x${!se.is_today ? ` · เก่า (${esc(se.as_of || "—")})` : ""
        }`;
    }
  }

  return `
    <div class="trade-status-strip" id="trade-status-strip">
      <span>📤 <strong>${esc(stripExitTxt)}</strong></span>
      <span>🎯 <strong>${esc(stripEntryTxt)}</strong></span>
    </div>

    <section class="card trade-main-card" id="hub-suggest-entry-card">
      <div class="trade-main-head">
        <span class="trade-main-title">🎯 Suggest Entry &amp; Multitrade</span>
        <div class="trade-hub-pills">${entryHeaderPills}</div>
      </div>
      <div class="muted" style="font-size:11.5px">${entryMetaLine}</div>

      <div style="display:flex;flex-direction:column;gap:6px">
        <div class="field-label" style="margin:0">งบต่อ 1 ไม้ (บาท)</div>
        ${(() => {
          const rec = getRecommendedSlotInfo(state);
          const isRecMode = !se || se.budget_mode !== "manual";
          const recActive = Boolean(rec && isRecMode && currentBudgetNum === rec.recommendedBudget);
          const recBtnHtml = rec
            ? `<div class="se-budget-chips has-rec"><button id="se-apply-recommend-btn" type="button" class="se-budget-chip se-budget-chip-rec ${recActive ? "active" : ""}" data-apply-rec="${rec.recommendedBudget}" title="ใช้ค่าแนะนำจาก Net Equity รวม (${fmtInt(rec.netEquity)} ÷ ${rec.slots})">✨ ใช้ค่าแนะนำ (${esc(fmtCompactK(rec.recommendedBudget))})</button></div>`
            : "";
          const subHtml = rec
            ? `<div class="muted" id="se-recommend-row" style="font-size:11px">✨ แนะนำ <strong>฿${fmtInt(rec.recommendedBudget)}/ไม้</strong> (Net Eq ฿${fmtInt(rec.netEquity)} ÷ ${rec.slots}${rec.partial ? " · บางบัญชีออฟไลน์" : ""})</div>`
            : se && se.ledger_available
              ? `<div class="muted" id="se-recommend-row" style="font-size:11px">💡 Sync แท็บ Port เพื่อคำนวณงบแนะนำจาก Net Equity</div>`
              : "";
          return `
            ${recBtnHtml}
            ${subHtml}
          `;
        })()}
        <div class="se-budget-row">
          <input id="se-budget-input" class="se-budget-input" type="number" step="5000" min="1000" inputmode="numeric" value="${esc(state.seBudgetInput)}" title="งบต่อ 1 ไม้ (บาท)">
          <button id="se-budget-save-btn" class="btn btn-ghost btn-sm" type="button">💾 บันทึก</button>
        </div>
      </div>

      <button id="tr-suggest-poll-btn" class="btn btn-primary" style="width:100%" type="button" ${state.sePolling ? "disabled" : ""}>
        ${state.sePolling ? "⏳ กำลังโหลดกระดาน Poll..." : "🔍 เปิดกระดาน Poll & Multitrade"}
      </button>
    </section>

    <section class="card trade-main-card" id="hub-auto-exit-card">
      <div class="trade-main-head">
        <span class="trade-main-title">📤 Auto Exit (Runbook)</span>
        ${formatHubSchedulePill(ex)}
      </div>
      ${exitDetailHtml}
      <button id="tr-runbook-fetch-btn" class="btn btn-ghost btn-sm" style="width:100%" type="button">
        📘 พรีวิว / ส่ง Exit
      </button>
    </section>
  `;
}

function renderSuggestModalRow(c, state, selCounts) {
  const key = `${c.side}:${c.ticker}`;
  const selObj = state.seSelected[key] || { checked: false, price_mode: "lure" };
  const checked = Boolean(selObj.checked) && Boolean(c.selectable);
  const pmode = c.match_allowed && selObj.price_mode === "match" ? "match" : "lure";
  const sideCapHit = c.side === "LONG" ? selCounts.long >= selCounts.remLong : selCounts.short >= selCounts.remShort;
  const capReached = !checked && (selCounts.total >= selCounts.remTotal || sideCapHit);
  const cbDisabled = !c.selectable || capReached || state.seSubmitting;
  const pct = c.live_pct_change;
  const pctStr = pct == null ? "—" : `${pct > 0 ? "+" : ""}${Number(pct).toFixed(2)}%`;
  const pctCls = pct == null ? "pill-muted" : pct >= 0 ? "pill-green" : "pill-red";
  const showPx = pmode === "match" && c.match_price ? c.match_price : c.lure_price;
  const rowCls = checked ? "is-checked" : c.selectable ? "is-eligible" : "is-disabled";
  const multVal = Number(c.effective_multiplier || 1);
  const hmMultVal = Number(c.house_money_multiplier || 1);
  const multTag = `${multVal.toFixed(2)}x${hmMultVal > 1 ? " HM" : ""}`;
  const multCls = multVal > 1 ? "pill-green" : multVal < 1 ? "pill-amber" : "pill-muted";

  return `
    <div class="se-row-compact ${rowCls}" data-se-row="${esc(key)}">
      <div class="field-row" style="justify-content:space-between;gap:6px">
        <label class="field-row" style="gap:7px;cursor:${cbDisabled ? "default" : "pointer"};min-width:0">
          <input class="se-chk" type="checkbox" data-se-chk="${esc(key)}" ${checked ? "checked" : ""} ${cbDisabled ? "disabled" : ""}>
          <span class="muted" style="font-size:11px">#${esc(c.rank)}</span>
          <strong style="font-size:13px">${esc(c.ticker)}</strong>
          <span class="pill ${c.side === "SHORT" ? "pill-red" : "pill-green"}">${esc(c.side)}</span>
          <span class="pill ${multCls}" data-se-mult title="${esc(c.edge_tier_label || "")} · งบไม้ ฿${fmtInt(c.effective_budget_thb || 0)}">${esc(multTag)}</span>
        </label>
        <div class="field-row" style="gap:5px;flex-shrink:0">
          <span style="font-size:12.5px;font-weight:700" data-se-last>${c.last != null ? `฿${fmtMoney(c.last)}` : "—"}</span>
          <span class="pill ${pctCls}" data-se-pct>${esc(pctStr)}</span>
        </div>
      </div>
      <div class="muted" style="font-size:11px;display:flex;flex-wrap:wrap;gap:6px;align-items:center;padding-left:24px">
        <span>Prior ${c.prior_close != null ? `฿${fmtMoney(c.prior_close)}` : "—"}</span>
        <span data-se-target>${showPx != null ? `→ เป้า <strong class="txt-sky">฿${fmtMoney(showPx)}</strong>` : ""}</span>
        <span data-se-shares>${c.planned_shares >= 100 ? `· ${fmtInt(c.planned_shares)} หุ้น (฿${fmtInt(c.planned_value)})` : ""}</span>
        <span data-se-sbl>${c.side === "SHORT" && c.sbl_available_vol != null ? `· SBL ${fmtInt(c.sbl_available_vol)}` : ""}</span>
        ${c.high_atr_warning ? `<span class="pill pill-amber">ATR ${Number(c.atr20_pct || 0).toFixed(1)}%</span>` : ""}
        <span data-se-reason>${!c.selectable && c.disabled_reason ? `<span class="pill pill-muted">${esc(c.disabled_reason)}</span>` : ""}</span>
      </div>
      ${checked && c.match_allowed
      ? `<div class="field-row" style="gap:6px;padding-left:24px;margin-top:2px">
               <span class="muted" style="font-size:10.5px">ราคาสั่งซื้อ:</span>
               <button type="button" class="se-mode-btn ${pmode === "lure" ? "active" : ""}" data-se-pmode="${esc(key)}" data-val="lure">${c.side === "SHORT" ? "Lure (Offer1)" : "Lure (Bid1)"}</button>
               <button type="button" class="se-mode-btn ${pmode === "match" ? "active" : ""}" data-se-pmode="${esc(key)}" data-val="match">${c.side === "SHORT" ? "Match (Bid1)" : "Match (Offer1)"}</button>
             </div>`
      : ""
    }
    </div>
  `;
}

function renderSuggestModal(state) {
  if (!state.seModalOpen) return "";
  const d = state.sePollData;
  const subRes = state.seSubmitResult;
  const q = (d && d.quota) || {};
  const remTotal = Number(q.remaining_total || 0);
  const remLong = Number(q.remaining_long || 0);
  const remShort = Number(q.remaining_short || 0);

  const selectedEntries = Object.entries(state.seSelected).filter(([, v]) => v && v.checked);
  const selectedCount = selectedEntries.length;
  const selCounts = {
    total: selectedCount,
    long: selectedEntries.filter(([k]) => k.startsWith("LONG:")).length,
    short: selectedEntries.filter(([k]) => k.startsWith("SHORT:")).length,
    remTotal,
    remLong,
    remShort,
  };

  const candsAll = (d && Array.isArray(d.candidates) ? d.candidates : []);
  const candByKey = new Map(candsAll.map((c) => [`${c.side}:${c.ticker}`, c]));
  const totalSelectedThb = selectedEntries.reduce((sum, [k]) => {
    const c = candByKey.get(k);
    return sum + Number((c && c.planned_value) || 0);
  }, 0);

  const eligibleCount = candsAll.filter((c) => c.selectable).length;
  const shortCount = candsAll.filter((c) => c.side === "SHORT").length;
  const longCount = candsAll.filter((c) => c.side === "LONG").length;
  const activeFilter = state.seFilter || "all";
  const filteredCands = candsAll.filter((c) => {
    if (activeFilter === "eligible") return Boolean(c.selectable);
    if (activeFilter === "short") return c.side === "SHORT";
    if (activeFilter === "long") return c.side === "LONG";
    return true;
  });

  const brokersTxt = d && d.brokers
    ? Object.entries(d.brokers)
      .map(([b, info]) => {
        const lineTxt = info && info.line_available != null ? `฿${fmtInt(info.line_available)}` : (info?.error || "N/A");
        return `${b.toUpperCase()} ${lineTxt}`;
      })
      .join(" · ")
    : "—";

  let bodyHtml = "";
  let footHtml = `<button id="se-modal-close-foot" class="btn btn-ghost" style="width:100%" type="button">ปิดหน้าต่าง</button>`;

  if (state.sePolling && !d) {
    bodyHtml = `<div class="sheet-busy-box"><span class="spinner"></span><span>กำลังดึงข้อมูล Top 8 และเชื่อมต่อกระดานสด...</span></div>`;
  } else if (state.sePollError && !d) {
    bodyHtml = `<div class="sheet-status-box sheet-status-err"><strong>❌ Poll ไม่สำเร็จ</strong><div class="muted">${esc(state.sePollError)}</div></div>`;
  } else if (subRes) {
    const rows = Array.isArray(subRes.results) ? subRes.results : [];
    bodyHtml = `
      <div class="sheet-status-box ${subRes.ok ? (subRes.dry_run ? "sheet-status-dry" : "sheet-status-ok") : "sheet-status-err"}">
        <strong>${subRes.dry_run ? "🧪 [DRY-RUN] " : ""}${subRes.ok ? "✅ ดำเนินการ Multitrade สำเร็จ" : "⚠️ มีรายการส่งไม่สำเร็จ"} (${subRes.submitted_count}/${rows.length} ไม้)</strong>
        <div class="muted" style="font-size:11.5px">${esc(subRes.line_status || "")}</div>
      </div>
      ${rows
        .map((r) => {
          const stCls = r.ok ? (r.dry_run ? "pill-amber" : "pill-green") : "pill-red";
          return `
            <div class="se-row-compact">
              <div class="field-row" style="justify-content:space-between">
                <span><strong>${esc(r.symbol)}</strong> <span class="pill ${r.side === "SHORT" ? "pill-red" : "pill-green"}">${esc(r.side)}</span></span>
                <span class="pill ${stCls}">${esc(r.status)}</span>
              </div>
              <div class="muted" style="font-size:11.5px">
                ${fmtInt(r.shares)} หุ้น @ ฿${fmtMoney(r.price)} (${esc(r.price_mode || "lure")})
                ${r.order_no ? ` · #${esc(r.order_no)}` : ""}
                ${r.retried_uptick ? ` · <span class="txt-sky">Auto-retry Uptick</span>` : ""}
              </div>
              ${r.error ? `<div class="txt-red" style="font-size:11.5px">${esc(r.error)}</div>` : ""}
            </div>
          `;
        })
        .join("")}
    `;
    footHtml = `
      <div style="display:flex;gap:8px">
        <button id="se-goto-orders" class="btn btn-primary" style="flex:1" type="button">📋 ไปดู Orders</button>
        <button id="se-res-close" class="btn btn-ghost" type="button">ปิด</button>
      </div>
    `;
  } else if (d) {
    const freeSlots = Number(q.free_slots || 0);
    bodyHtml = `
      ${d.is_hold_cash
        ? `<div class="sheet-status-box sheet-status-warn" style="padding:7px 10px">
               <div class="field-row" style="justify-content:space-between;gap:6px">
                 <span style="font-size:11.5px"><strong>⚪ Hold Cash (0L:0S)</strong> · ดูราคาสดได้</span>
                 ${freeSlots > 0
          ? `<label class="field-row" style="gap:5px;cursor:pointer" for="se-override-hold">
                          <input class="se-chk" id="se-override-hold" type="checkbox" ${state.seOverrideHoldCash ? "checked" : ""}>
                          <span style="font-size:11.5px">🔓 Override (${freeSlots})</span>
                        </label>`
          : `<span class="muted" style="font-size:11px">ว่าง 0</span>`
        }
               </div>
             </div>`
        : ""
      }
      ${!d.is_today
        ? `<label class="toggle-row" for="se-allow-stale">
               <input id="se-allow-stale" type="checkbox" ${state.seAllowStale ? "checked" : ""}>
               <span style="font-size:11.5px">⚠️ ข้อมูล Report วันที่ ${esc(d.as_of || "—")} ไม่ใช่วันนี้ — ยืนยันใช้ข้อมูลย้อนหลัง</span>
             </label>`
        : ""
      }
      ${filteredCands.length === 0
        ? `<div class="muted" style="font-size:12px;padding:16px 0;text-align:center">ไม่มีหุ้นในหมวดที่กรอง</div>`
        : filteredCands.map((c) => renderSuggestModalRow(c, state, selCounts)).join("")
      }
    `;

    footHtml = `
      <div class="field-row" style="justify-content:space-between;font-size:11.5px" id="se-selected-summary">
        <span>เลือก <strong>${selectedCount}/${remTotal}</strong> ไม้</span>
        <span class="muted">รวมประมาณ <strong>฿${fmtInt(totalSelectedThb)}</strong></span>
      </div>
      <div style="display:flex;gap:8px">
        <button id="se-modal-submit-btn" class="btn ${d.dry_run ? "btn-amber" : "btn-primary"}" style="flex:1" type="button" ${selectedCount === 0 || state.sePolling || state.seSubmitting ? "disabled" : ""}>
          ${state.seSubmitting
        ? "⏳ กำลังส่ง Batch..."
        : `${d.dry_run ? "🧪 " : "🚀 "}ยืนยันส่ง Batch (${selectedCount} ไม้)`
      }
        </button>
        <button id="se-modal-close-foot" class="btn btn-ghost" type="button">ยกเลิก</button>
      </div>
    `;
  }

  return `
    <div id="se-poll-backdrop" class="sheet-backdrop" role="dialog" aria-modal="true" aria-label="Suggest Entry Multitrade">
      <div class="suggest-modal-panel">
        <div class="suggest-modal-head">
          <div style="min-width:0">
            <strong style="font-size:14px">🎯 Multitrade Poll (Top 8)</strong>
            <div class="muted" style="font-size:10.5px" id="se-live-clock">${d && d.dry_run ? "DRY-RUN" : "LIVE"} · อัปเดต ${esc(state.seLastTickAt || (d && d.now_bkk) || "—")}</div>
          </div>
          <div class="field-row" style="gap:6px;flex-shrink:0">
            <button id="se-live-toggle" class="se-live-pill ${state.seLiveEnabled ? "" : "is-paused"}" type="button" ${state.seSubmitting ? "disabled" : ""}>
              ${state.seLiveEnabled ? "🟢 Live 2s" : "⏸️ หยุด"}
            </button>
            <button id="se-modal-repoll" class="pill-btn pill-sky" type="button" ${state.sePolling || state.seSubmitting ? "disabled" : ""} title="รีเฟรชพอร์ต+ราคาทั้งหมด">🔄</button>
            <button id="se-modal-close" class="pill-btn pill-muted" type="button" aria-label="ปิด">✕</button>
          </div>
        </div>
        ${d
      ? `<div class="suggest-modal-sub">
                 <div class="field-row" style="justify-content:space-between" id="se-quota-summary">
                   <span>โควตา: <strong>${remTotal} ไม้</strong> (${remLong}L / ${remShort}S)</span>
                   <span class="muted" id="se-brokers-line">งบฐาน ฿${fmtInt(d.budget_per_slot_thb)}${Number(d.sizing_reference?.house_money_multiplier || 1) > 1 ? ` · HM ${Number(d.sizing_reference.house_money_multiplier).toFixed(2)}x` : ""} · ${esc(brokersTxt)}</span>
                 </div>
                 <div class="se-filter-bar">
                   <button type="button" class="se-filter-btn ${activeFilter === "all" ? "active" : ""}" data-se-filter="all">ทั้งหมด (${candsAll.length})</button>
                   <button type="button" class="se-filter-btn ${activeFilter === "eligible" ? "active" : ""}" data-se-filter="eligible">✅ เข้าเกณฑ์ (${eligibleCount})</button>
                   <button type="button" class="se-filter-btn ${activeFilter === "short" ? "active" : ""}" data-se-filter="short">SHORT (${shortCount})</button>
                   <button type="button" class="se-filter-btn ${activeFilter === "long" ? "active" : ""}" data-se-filter="long">LONG (${longCount})</button>
                 </div>
               </div>`
      : ""
    }
        <div class="suggest-modal-body" id="se-modal-body">${bodyHtml}</div>
        <div class="suggest-modal-foot">${footHtml}</div>
      </div>
    </div>
  `;
}

function renderOrderSubtab(state) {
  const q = state.quote;
  const prev = state.preview;
  const routeVal = `${state.broker}:${state.account}`;
  const sideLabel = state.side.toUpperCase();
  const symClean = String(state.symbol || "").trim().toUpperCase();
  const advSummary = `${routeLabel(state.broker, state.account)} · ${state.price ? `฿${esc(state.price)}` : "ราคาตลาด"
    }`;

  return `
    <section class="card">
      <div class="seg-bar">
        ${["buy", "sell", "short", "cover"]
      .map(
        (s) =>
          `<button class="seg-btn side-${s} ${state.side === s ? "active" : ""}" data-side="${s}" type="button">${s.toUpperCase()}</button>`
      )
      .join("")}
      </div>

      <div class="field-col">
        <label class="field-label" for="tr-symbol">ชื่อหุ้น</label>
        <div class="field-row">
          <input id="tr-symbol" class="text-input" value="${esc(state.symbol)}" placeholder="PTT, AOT" autocapitalize="characters">
          <button id="tr-quote-btn" class="btn btn-ghost" type="button">ราคาสด</button>
        </div>
      </div>

      ${q
      ? `<div class="quote-strip">
               <div class="quote-box" data-tap-px="${q.bid_1 ?? ""}"><small>Bid1</small><strong>${q.bid_1 ? `฿${fmtMoney(q.bid_1)}` : "—"}</strong></div>
               <div class="quote-box" data-tap-px="${q.last ?? ""}"><small>Last</small><strong>${q.last ? `฿${fmtMoney(q.last)}` : "—"}</strong></div>
               <div class="quote-box" data-tap-px="${q.offer_1 ?? ""}"><small>Offer1</small><strong>${q.offer_1 ? `฿${fmtMoney(q.offer_1)}` : "—"}</strong></div>
             </div>`
      : ""
    }

      ${state.amountMode === "volume"
      ? `<div class="field-col">
               <label class="field-label" for="tr-vol">จำนวน</label>
               <div class="field-row">
                 <input id="tr-vol" class="text-input" type="number" step="100" value="${esc(state.volume)}" placeholder="100" inputmode="numeric">
                 <span class="muted" style="white-space:nowrap">หุ้น</span>
               </div>
               <div class="chip-row">
                 ${[100, 500, 1000]
        .map((n) => `<button class="chip-btn" data-add-vol="${n}" type="button">+${fmtInt(n)}</button>`)
        .join("")}
                 <button class="chip-btn" data-amode="all" type="button">ทั้งหมด</button>
               </div>
             </div>`
      : state.amountMode === "value"
        ? (() => {
            const baseBudget = getSavedBaseBudget(state);
            const multPresets = getMultiplierPresets(baseBudget);
            return `<div class="field-col">
                 <div class="field-row" style="justify-content:space-between">
                   <label class="field-label" for="tr-budget" style="margin:0">จำนวนเงิน</label>
                   <span class="muted" style="font-size:11px">Base ฿${fmtInt(baseBudget)}</span>
                 </div>
                 <div class="field-row">
                   <input id="tr-budget" class="text-input" type="number" step="1000" value="${esc(state.targetValue)}" placeholder="${esc(String(baseBudget))}" inputmode="numeric">
                   <span class="muted" style="white-space:nowrap">บาท</span>
                 </div>
                 <div class="chip-row">
                   ${multPresets
                     .map((p) => `<button class="chip-btn ${Number(state.targetValue) === p.value ? "active" : ""}" data-set-budget="${p.value}" type="button">${esc(p.label)} (${fmtInt(p.value)} ฿)</button>`)
                     .join("")}
                 </div>
               </div>`;
          })()
        : `<div class="field-row" style="justify-content:space-between;padding:8px 10px;border-radius:10px;border:1px solid var(--border);background:rgba(255,255,255,0.03)">
                 <span style="font-size:12.5px">📦 ใช้จำนวนทั้งหมดที่มีในพอร์ต / SBL</span>
                 <button class="btn btn-ghost btn-xs" data-amode="volume" type="button">ระบุหุ้น</button>
               </div>`
    }

      <button id="tr-preview-btn" class="btn btn-primary" type="button">
        🚀 ส่งคำสั่ง ${esc(sideLabel)}${symClean ? ` ${esc(symClean)}` : ""}
      </button>

      <button id="tr-advance-toggle" class="adv-toggle" type="button" aria-expanded="${state.orderAdvOpen ? "true" : "false"}">
        <span>⚙️ Advance ${state.orderAdvOpen ? "▾" : "▸"}</span>
        <span class="muted" style="font-weight:500">${esc(advSummary)}</span>
      </button>

      ${state.orderAdvOpen
      ? `<div id="tr-advance-panel" class="adv-panel">
               <div class="field-col">
                 <label class="field-label" for="tr-route">โบรกเกอร์และบัญชี</label>
                 <select id="tr-route" class="select-input">
                   <option value="lib:cash" ${routeVal === "lib:cash" ? "selected" : ""}>Liberator · Cash (<REDACTED_LIBERATOR_USERNAME>)</option>
                   <option value="kss:margin" ${routeVal === "kss:margin" ? "selected" : ""}>Krungsri · Margin (<REDACTED_KSS_ACCT>)</option>
                   <option value="kss:cash" ${routeVal === "kss:cash" ? "selected" : ""}>Krungsri · Cash (<REDACTED_KSS_ACCT>)</option>
                 </select>
               </div>

               <div class="field-col">
                 <span class="field-label">รูปแบบจำนวน</span>
                 <div class="seg-bar">
                   <button class="seg-btn ${state.amountMode === "volume" ? "active" : ""}" data-amode="volume" type="button">หุ้น</button>
                   <button class="seg-btn ${state.amountMode === "value" ? "active" : ""}" data-amode="value" type="button">บาท</button>
                   <button class="seg-btn ${state.amountMode === "all" ? "active" : ""}" data-amode="all" type="button">ทั้งหมด</button>
                 </div>
               </div>

               <div class="field-col">
                 <label class="field-label" for="tr-price">ราคา (บาท · เว้นว่าง = ราคาตลาด)</label>
                 <div class="field-row">
                   <button id="tr-px-down" class="btn btn-ghost" type="button">- Tick</button>
                   <input id="tr-price" class="text-input" type="number" step="0.01" value="${esc(state.price)}" placeholder="Auto">
                   <button id="tr-px-up" class="btn btn-ghost" type="button">+ Tick</button>
                 </div>
               </div>
             </div>`
      : ""
    }
    </section>

    ${prev && !prev.ok
      ? `<section class="card card-danger">
             <div class="card-head">
               <div class="card-title">❌ ไม่ผ่านเกณฑ์ (${esc(prev.code || "REJECTED")})</div>
             </div>
             <div class="muted">${esc(prev.message)}</div>
             ${String(prev.code || "").includes("SBL")
        ? `<button id="tr-jump-sbl" class="btn btn-amber" type="button">🔄 ไปหน้ายืมหุ้น SBL</button>`
        : ""
      }
           </section>`
      : ""
    }
  `;
}

function renderQuickSubtab() {
  const shortR = defaultRouteForSide("short");
  const longR = defaultRouteForSide("buy");
  const baseBudget = getSavedBaseBudget(_state);
  const multPresets = getMultiplierPresets(baseBudget);
  const cleanSym = String(_state.symbol || "").trim().toUpperCase();
  const candSides = (_state.hubData && _state.hubData.suggest_entry && _state.hubData.suggest_entry.candidate_sides) || {};
  const candSide = cleanSym ? candSides[cleanSym] || null : null;
  const kssBtnCls = _state.quickAutoShort ? "btn-purple" : "btn-amber";
  const kssBtnLabel = _state.quickAutoShort
    ? `⚡ ยืม SBL + Short ทันที (${esc(shortR.broker.toUpperCase())})`
    : `📥 ยืม SBL เข้า (B) (${esc(shortR.broker.toUpperCase())})`;

  return `
    <section class="card">
      <div class="card-head" style="margin-bottom:2px">
        <div class="card-title">⚡ Quick Order</div>
        <span class="muted" style="font-size:11.5px">Base ฿${fmtInt(baseBudget)} · ${esc(routeLabel(shortR.broker, shortR.account))} · ${esc(routeLabel(longR.broker, longR.account))}</span>
      </div>
      <div class="field-row">
        <input id="quick-sym-input" class="text-input" value="${esc(_state.symbol)}" placeholder="PTT, ADVANC" autocapitalize="characters">
        <button id="quick-check-btn" class="btn btn-primary" type="button">เช็คคลัง &amp; ราคา</button>
      </div>
      <div id="quick-suggest-hint" class="muted" style="font-size:11.5px;${candSide ? "" : "display:none"}">
        ${candSide === "SHORT" ? `🎯 Suggest Entry วันนี้: <strong class="txt-amber">SHORT (${esc(cleanSym)} · KSS SBL)</strong>` : candSide === "LONG" ? `🎯 Suggest Entry วันนี้: <strong class="txt-green">LONG (${esc(cleanSym)} · LIB Cash)</strong>` : ""}
      </div>
      <div class="budget-pills">
        ${QUICK_BUDGET_PRESETS.map(
    (p) =>
      `<button class="chip-btn budget-pill ${Number(_state.quickBudget) === p.value ? "active" : ""}" data-qbudget="${p.value}" type="button">${esc(p.label)} (${fmtInt(p.value)} ฿)</button>`
  ).join("")}
      </div>
      <div class="field-row">
        <input id="quick-budget-input" class="text-input" type="number" min="100" step="1000" value="${esc(_state.quickBudget)}" placeholder="${esc(String(baseBudget))}" inputmode="numeric">
        <span class="muted" style="white-space:nowrap">บาท</span>
      </div>
      <label class="toggle-row" for="quick-auto-short">
        <input id="quick-auto-short" type="checkbox" ${_state.quickAutoShort ? "checked" : ""}>
        <span>⚡ ยืม SBL สำเร็จแล้ว Short ทันที (KSS)</span>
      </label>
      ${renderQuickQuoteBox(_state.quickPool, _state.quickKssQuote, _state.quickLibQuote, _state.quickBudget, _state.quickAutoShort)}
      <div class="btn-row">
        <button id="quick-borrow-btn" class="btn ${kssBtnCls}" type="button">${kssBtnLabel}${candSide === "SHORT" ? " ★" : ""}</button>
        <button id="quick-buy-lib-btn" class="btn btn-success" type="button">🟢 ซื้อ (${esc(routeLabel(longR.broker, longR.account))})${candSide === "LONG" ? " ★" : ""}</button>
      </div>
    </section>
  `;
}

function renderSblSubtab(state) {
  const isReturn = state.sblAction === "return";
  const cleanSym = String(state.symbol || "").trim().toUpperCase();
  const bySymbol = new Map();
  for (const lot of state.sblLots || []) {
    const sym = String(lot.symbol || "").trim().toUpperCase();
    const rem = Number(lot.remaining_qty ?? lot.borrow_qty ?? 0);
    if (!sym || rem <= 0) continue;
    if (!bySymbol.has(sym)) bySymbol.set(sym, { symbol: sym, totalQty: 0, lots: [] });
    const entry = bySymbol.get(sym);
    entry.totalQty += rem;
    entry.lots.push(lot);
  }
  const symEntries = Array.from(bySymbol.values()).sort((a, b) => a.symbol.localeCompare(b.symbol));
  const selectedEntry = cleanSym ? bySymbol.get(cleanSym) : null;
  const matchingLots = selectedEntry ? selectedEntry.lots : [];
  const totalMatchQty = selectedEntry ? selectedEntry.totalQty : 0;

  return `
    <section class="card">
      <div class="seg-bar">
        <button class="seg-btn ${!isReturn ? "active" : ""}" data-sblact="borrow" type="button">📥 ยืมหุ้น</button>
        <button class="seg-btn ${isReturn ? "active" : ""}" data-sblact="return" type="button">📤 คืนหุ้น</button>
      </div>
      ${isReturn
      ? `
        <div class="field-col" style="margin-bottom:4px">
          <div class="card-head" style="margin-bottom:4px">
            <span class="field-label" style="margin:0">หุ้นที่ยืมอยู่ (แตะเพื่อคืน)</span>
            <button id="sbl-refresh-lots-btn" class="pill-btn pill-sky" type="button" ${state.sblLotsLoading ? "disabled" : ""}>${state.sblLotsLoading ? "⏳..." : "🔄 โหลด"}</button>
          </div>
          ${state.sblLotsError
        ? `<div class="muted txt-amber" style="font-size:11.5px">⚠️ ${esc(state.sblLotsError)}</div>`
        : symEntries.length
          ? `<div class="chip-row">${symEntries
            .map((e) => {
              const activeCls = cleanSym === e.symbol ? "active" : "";
              const cntNote = e.lots.length > 1 ? ` · ${e.lots.length} ใบ` : "";
              return `<button class="chip-btn ${activeCls}" data-sbl-symchip="${esc(e.symbol)}" data-sbl-qty="${e.totalQty}" type="button">${esc(e.symbol)} (${fmtInt(e.totalQty)}${cntNote})</button>`;
            })
            .join("")}</div>`
          : `<div class="muted" style="font-size:11.5px">${state.sblLotsLoading ? "กำลังดึงรายการ SBL..." : state.sblLotsLoaded ? "ไม่พบรายการที่เปิดอยู่" : "แตะ 🔄 โหลด เพื่อดึงรายการ"}</div>`
      }
        </div>
      `
      : ""
    }
      <div class="field-col">
        <label class="field-label" for="sbl-sym">ชื่อหุ้น · KSS Margin</label>
        <div class="field-row">
          <input id="sbl-sym" class="text-input" value="${esc(state.symbol)}" placeholder="AAV, PTT" autocapitalize="characters">
          <button id="sbl-check-btn" class="btn btn-ghost" type="button">${isReturn ? "เช็ค" : "เช็ค Pool/XD"}</button>
        </div>
      </div>
      ${state.sblPool
      ? `<div class="code-block ${state.sblPool.xd_risk?.xd_risk ? "txt-red" : ""}">คลัง SBL ${esc(state.sblPool.symbol)}: ${fmtInt(state.sblPool.available_qty)} หุ้น · 3.50%/ปี
XD Risk: ${state.sblPool.xd_risk?.xd_risk ? esc(state.sblPool.xd_risk.reason) : "🟢 ปลอดภัย"}</div>`
      : ""
    }
      <div class="field-col">
        <label class="field-label" for="sbl-vol">จำนวน</label>
        <div class="field-row">
          <input id="sbl-vol" class="text-input" type="number" step="100" value="${esc(state.sblVolume)}" placeholder="${isReturn ? "เลือกหุ้นด้านบน หรือระบุจำนวน" : "1000"}" inputmode="numeric">
          <span class="muted" style="white-space:nowrap">หุ้น</span>
        </div>
      </div>

      <div class="btn-row">
        <button id="sbl-exec-btn" class="btn btn-amber" type="button">${!isReturn ? "📥 ยืนยันขอยืมหุ้น" : "📤 ยืนยันคืนหุ้น"}</button>
        ${isReturn ? `<button id="sbl-ret-all-btn" class="btn btn-success" type="button">คืนทั้งหมด</button>` : ""}
      </div>

      ${isReturn
      ? `
        <button id="sbl-advance-toggle" class="adv-toggle" type="button" aria-expanded="${state.sblAdvOpen ? "true" : "false"}">
          <span>⚙️ Advance ${state.sblAdvOpen ? "▾" : "▸"}</span>
          <span class="muted" style="font-weight:500">${state.sblSelectedLotId ? `Ref: ${esc(state.sblSelectedLotId)}` : "FIFO อัตโนมัติ"}</span>
        </button>
        ${state.sblAdvOpen
        ? `<div id="sbl-advance-panel" class="adv-panel">
                 ${matchingLots.length > 0
          ? `<div class="field-col">
                          <label class="field-label" for="sbl-lot-id">สัญญาที่คืน (Ref.no)</label>
                          <select id="sbl-lot-id" class="select-input">
                            <option value="" ${!state.sblSelectedLotId ? "selected" : ""}>🔄 FIFO อัตโนมัติ (${matchingLots.length} สัญญา · ${fmtInt(totalMatchQty)} หุ้น)</option>
                            ${matchingLots
            .map((lot) => {
              const refNo = String(lot.broker_tx_id || lot.lot_id || "").trim();
              const rem = Number(lot.remaining_qty ?? lot.borrow_qty ?? 0);
              const sel = state.sblSelectedLotId === refNo ? "selected" : "";
              const dt = [lot.borrow_date, lot.time].filter(Boolean).join(" ");
              return `<option value="${esc(refNo)}" ${sel}>${esc(refNo)}${dt ? ` · ${esc(dt)}` : ""} · ${fmtInt(rem)} หุ้น</option>`;
            })
            .join("")}
                          </select>
                        </div>`
          : `<div class="muted" style="font-size:11.5px">ระบบเลือกสัญญาแบบ FIFO ให้อัตโนมัติ (หรือกดเช็คเพื่อโหลด Ref.no)</div>`
        }
                 <div class="field-col">
                   <label class="field-label" for="sbl-remark">หมายเหตุ</label>
                   <input id="sbl-remark" class="text-input" value="${esc(state.sblRemark)}" placeholder="เว้นว่างได้">
                 </div>
               </div>`
        : ""
      }
      `
      : ""
    }
    </section>
  `;
}

export async function renderTradeView(container, ctx) {
  const { params = {}, dryRun = true, toast, confirmSheet, navigate, statusData = null, isCurrent = () => true, syncNav = null } = ctx;
  if (statusData) {
    _state.statusData = statusData;
  }
  const nextSig = routingSignature(_state.statusData);
  const routingChanged = nextSig !== _state.routingKey;
  if (routingChanged) {
    _state.routingKey = nextSig;
    if (!params._consumePrefill) {
      const def = defaultRouteForSide(_state.side, _state.statusData);
      _state.broker = def.broker;
      _state.account = def.account;
      _state.preview = null;
    }
  }

  if (params._consumePrefill) {
    delete params._consumePrefill;
    if (params.subtab === "sbl" || params.subtab === "quick" || params.subtab === "order") {
      _state.subtab = params.subtab;
      _state.manualAdvOpen = true;
    } else if (params.side) {
      _state.subtab = "order";
      _state.manualAdvOpen = true;
    }

    if (params.side && ["buy", "sell", "short", "cover"].includes(String(params.side).toLowerCase())) {
      _state.side = String(params.side).toLowerCase();
      const def = defaultRouteForSide(_state.side);
      _state.broker = params.broker || def.broker;
      _state.account = params.account || def.account;
    }
    if (params.symbol) _state.symbol = String(params.symbol).toUpperCase();
    if (params.volume) {
      _state.volume = String(params.volume);
      _state.sblVolume = String(params.volume);
      _state.sblVolumeFromPrefill = true;
      _state.amountMode = "volume";
    } else {
      _state.sblVolumeFromPrefill = false;
    }
    if (params.sbl_action) _state.sblAction = params.sbl_action;
    _state.preview = null;
    _state.idemKey = newIdempotencyKey();
  }

  const syncBudgetFromBase = (baseVal, { syncQuick = false } = {}) => {
    const cleanBase = Math.round(Number(baseVal || 0));
    if (!Number.isFinite(cleanBase) || cleanBase < 1000) return;
    if (!_state.seBudgetDirty) {
      _state.seBudgetInput = String(cleanBase);
    }
    if (syncQuick && !_state.quickBudgetDirty) {
      _state.quickBudget = cleanBase;
    }
    if (!_state.targetValueDirty) {
      _state.targetValue = String(cleanBase);
    }
  };

  const resetPreview = () => {
    _state.preview = null;
    _state.idemKey = newIdempotencyKey();
  };

  const loadHubSummary = async () => {
    _state.hubLoading = true;
    _state.hubError = "";
    try {
      const res = await miniappApi.getTradeHubSummary();
      _state.hubData = res;
      const se = res && res.suggest_entry;
      const rec = getRecommendedSlotInfo(_state);
      const effectiveBase =
        se && se.budget_mode !== "manual" && rec
          ? rec.recommendedBudget
          : se && se.budget_per_slot_thb != null
            ? se.budget_per_slot_thb
            : null;
      if (effectiveBase != null) {
        syncBudgetFromBase(effectiveBase);
      }
    } catch (err) {
      _state.hubError = (err && err.message) || "โหลดสรุป Trade ไม่สำเร็จ";
    } finally {
      _state.hubLoading = false;
      paint();
    }
  };

  const stopSuggestStream = () => {
    if (_state.seStreamAbort) {
      try {
        _state.seStreamAbort.abort();
      } catch { }
      _state.seStreamAbort = null;
    }
    if (_state.sePollFallbackTimer) {
      clearTimeout(_state.sePollFallbackTimer);
      _state.sePollFallbackTimer = null;
    }
  };

  const applyLiveQuotesSnapshot = (d) => {
    if (!d || !_state.seModalOpen || _state.seSubmitResult || !container.isConnected) {
      stopSuggestStream();
      return;
    }
    _state.sePollData = d;
    _state.seLastTickAt = d.now_bkk || _state.seLastTickAt;

    const clockEl = container.querySelector("#se-live-clock");
    if (clockEl) {
      clockEl.textContent = `${d.dry_run ? "DRY-RUN" : "LIVE"} · อัปเดต ${_state.seLastTickAt || "—"}`;
    }

    const q = d.quota || {};
    const remTotal = Number(q.remaining_total || 0);
    const remLong = Number(q.remaining_long || 0);
    const remShort = Number(q.remaining_short || 0);

    const quotaWrap = container.querySelector("#se-quota-summary");
    if (quotaWrap && quotaWrap.firstElementChild) {
      quotaWrap.firstElementChild.innerHTML = `โควตา: <strong>${remTotal} ไม้</strong> (${remLong}L / ${remShort}S)`;
    }
    const brokersEl = container.querySelector("#se-brokers-line");
    if (brokersEl) {
      const brokersTxt = d.brokers
        ? Object.entries(d.brokers)
          .map(([b, info]) => {
            const lineTxt = info && info.line_available != null ? `฿${fmtInt(info.line_available)}` : (info?.error || "N/A");
            return `${b.toUpperCase()} ${lineTxt}`;
          })
          .join(" · ")
        : "—";
      const hmTxt = Number(d.sizing_reference?.house_money_multiplier || 1) > 1
        ? ` · HM ${Number(d.sizing_reference.house_money_multiplier).toFixed(2)}x`
        : "";
      brokersEl.textContent = `งบฐาน ฿${fmtInt(d.budget_per_slot_thb)}${hmTxt} · ${brokersTxt}`;
    }

    const candsAll = Array.isArray(d.candidates) ? d.candidates : [];
    const candByKey = new Map(candsAll.map((c) => [`${c.side}:${c.ticker}`, c]));

    const eligibleCount = candsAll.filter((c) => c.selectable).length;
    const shortCount = candsAll.filter((c) => c.side === "SHORT").length;
    const longCount = candsAll.filter((c) => c.side === "LONG").length;
    const fLabels = {
      all: `ทั้งหมด (${candsAll.length})`,
      eligible: `✅ เข้าเกณฑ์ (${eligibleCount})`,
      short: `SHORT (${shortCount})`,
      long: `LONG (${longCount})`,
    };
    container.querySelectorAll("[data-se-filter]").forEach((btn) => {
      const fk = btn.dataset.seFilter;
      if (fLabels[fk]) btn.textContent = fLabels[fk];
    });

    const selectedEntries = Object.entries(_state.seSelected).filter(([, v]) => v && v.checked);
    const selCounts = {
      total: selectedEntries.length,
      long: selectedEntries.filter(([k]) => k.startsWith("LONG:")).length,
      short: selectedEntries.filter(([k]) => k.startsWith("SHORT:")).length,
      remTotal,
      remLong,
      remShort,
    };
    const totalSelectedThb = selectedEntries.reduce((sum, [k]) => {
      const c = candByKey.get(k);
      return sum + Number((c && c.planned_value) || 0);
    }, 0);
    const selSummaryEl = container.querySelector("#se-selected-summary");
    if (selSummaryEl) {
      selSummaryEl.innerHTML = `
        <span>เลือก <strong>${selectedEntries.length}/${remTotal}</strong> ไม้</span>
        <span class="muted">รวมประมาณ <strong>฿${fmtInt(totalSelectedThb)}</strong></span>
      `;
    }

    container.querySelectorAll("[data-se-row]").forEach((rowEl) => {
      const key = rowEl.dataset.seRow;
      const c = candByKey.get(key);
      if (!c) return;
      const selObj = _state.seSelected[key] || { checked: false, price_mode: "lure" };
      const isChecked = Boolean(selObj.checked);
      const pmode = c.match_allowed && selObj.price_mode === "match" ? "match" : "lure";
      const showPx = pmode === "match" && c.match_price ? c.match_price : c.lure_price;
      const pct = c.live_pct_change;
      const pctStr = pct == null ? "—" : `${pct > 0 ? "+" : ""}${Number(pct).toFixed(2)}%`;
      const pctCls = pct == null ? "pill-muted" : pct >= 0 ? "pill-green" : "pill-red";

      const lastEl = rowEl.querySelector("[data-se-last]");
      if (lastEl) lastEl.textContent = c.last != null ? `฿${fmtMoney(c.last)}` : "—";
      const pctEl = rowEl.querySelector("[data-se-pct]");
      if (pctEl) {
        pctEl.textContent = pctStr;
        pctEl.className = `pill ${pctCls}`;
      }
      const targetEl = rowEl.querySelector("[data-se-target]");
      if (targetEl) {
        targetEl.innerHTML = showPx != null ? `→ เป้า <strong class="txt-sky">฿${fmtMoney(showPx)}</strong>` : "";
      }
      const sharesEl = rowEl.querySelector("[data-se-shares]");
      if (sharesEl) {
        sharesEl.textContent = c.planned_shares >= 100 ? `· ${fmtInt(c.planned_shares)} หุ้น (฿${fmtInt(c.planned_value)})` : "";
      }
      const multEl = rowEl.querySelector("[data-se-mult]");
      if (multEl) {
        const mv = Number(c.effective_multiplier || 1);
        const hmv = Number(c.house_money_multiplier || 1);
        multEl.textContent = `${mv.toFixed(2)}x${hmv > 1 ? " HM" : ""}`;
        multEl.className = `pill ${mv > 1 ? "pill-green" : mv < 1 ? "pill-amber" : "pill-muted"}`;
        multEl.title = `${c.edge_tier_label || ""} · งบไม้ ฿${fmtInt(c.effective_budget_thb || 0)}`;
      }
      const sblEl = rowEl.querySelector("[data-se-sbl]");
      if (sblEl) {
        sblEl.textContent = c.side === "SHORT" && c.sbl_available_vol != null ? `· SBL ${fmtInt(c.sbl_available_vol)}` : "";
      }
      const reasonEl = rowEl.querySelector("[data-se-reason]");
      if (reasonEl) {
        reasonEl.innerHTML = !c.selectable && c.disabled_reason ? `<span class="pill pill-muted">${esc(c.disabled_reason)}</span>` : "";
      }

      if (!isChecked) {
        const sideCapHit = c.side === "LONG" ? selCounts.long >= selCounts.remLong : selCounts.short >= selCounts.remShort;
        const capReached = selCounts.total >= selCounts.remTotal || sideCapHit;
        const cb = rowEl.querySelector("[data-se-chk]");
        if (cb) cb.disabled = !c.selectable || capReached || _state.seSubmitting;
        rowEl.className = `se-row-compact ${c.selectable ? "is-eligible" : "is-disabled"}`;
      }
    });
  };

  const scheduleFallbackPoll = () => {
    if (!container.isConnected || !_state.seModalOpen || !_state.seLiveEnabled || _state.seSubmitResult) return;
    _state.sePollFallbackTimer = setTimeout(async () => {
      _state.sePollFallbackTimer = null;
      if (!container.isConnected || !_state.seModalOpen || !_state.seLiveEnabled || _state.seSubmitResult) return;
      try {
        const res = await miniappApi.pollSuggestQuotes(
          Boolean(_state.seAllowStale),
          Boolean(_state.seOverrideHoldCash)
        );
        applyLiveQuotesSnapshot(res);
      } catch {
        // Silent retry on next 2s interval while live streaming is active
      }
      if (container.isConnected && _state.seModalOpen && _state.seLiveEnabled && !_state.seSubmitResult) {
        scheduleFallbackPoll();
      }
    }, 2000);
  };

  const startSuggestStream = () => {
    stopSuggestStream();
    if (!container.isConnected || !_state.seModalOpen || !_state.seLiveEnabled || _state.seSubmitResult || !_state.sePollData) {
      return;
    }
    const ctrl = new AbortController();
    _state.seStreamAbort = ctrl;
    miniappApi
      .streamSuggestQuotes({
        allowStale: Boolean(_state.seAllowStale),
        overrideHoldCash: Boolean(_state.seOverrideHoldCash),
        intervalS: 2.0,
        signal: ctrl.signal,
        onQuotes: (payload) => {
          if (ctrl.signal.aborted) return;
          applyLiveQuotesSnapshot(payload);
        },
        onError: () => { },
      })
      .then(() => {
        if (!ctrl.signal.aborted && _state.seStreamAbort === ctrl) {
          _state.seStreamAbort = null;
          scheduleFallbackPoll();
        }
      })
      .catch(() => {
        if (!ctrl.signal.aborted && _state.seStreamAbort === ctrl) {
          _state.seStreamAbort = null;
          scheduleFallbackPoll();
        }
      });
  };

  const runSuggestPoll = async ({ openModal = true } = {}) => {
    stopSuggestStream();
    if (openModal) _state.seModalOpen = true;
    _state.sePolling = true;
    _state.sePollError = "";
    _state.seSubmitResult = null;
    paint();
    try {
      if (_state.seBudgetDirty) {
        const bVal = Number(_state.seBudgetInput);
        if (Number.isFinite(bVal) && bVal >= 1000) {
          await miniappApi.saveSuggestEntryConfig(bVal);
          _state.seBudgetDirty = false;
        }
      }
      const res = await miniappApi.pollSuggestEntry(
        Boolean(_state.seAllowStale),
        Boolean(_state.seOverrideHoldCash)
      );
      _state.sePollData = res;
      _state.seLastTickAt = res.now_bkk || "";
      _state.seSelected = {}; // Start unchecked (user chooses Multiple Choices explicitly)
    } catch (err) {
      _state.sePollError = (err && err.message) || "Poll ไม่สำเร็จ";
      toast(`❌ ${_state.sePollError}`, "err");
    } finally {
      _state.sePolling = false;
      paint();
      if (_state.seModalOpen && !_state.seSubmitResult && _state.sePollData && _state.seLiveEnabled) {
        startSuggestStream();
      }
    }
  };

  const syncSblVolumeForSymbol = (sym) => {
    const clean = String(sym || "").trim().toUpperCase();
    if (!clean || _state.sblAction !== "return") return false;
    const matched = (_state.sblLots || []).filter(
      (l) => String(l.symbol || "").trim().toUpperCase() === clean
    );
    const totalLotQty = matched.reduce(
      (acc, l) => acc + Number(l.remaining_qty ?? l.borrow_qty ?? 0),
      0
    );
    const retQty = Number((_state.sblReturnableBySym || {})[clean] || 0);
    const fillQty = retQty > 0 ? retQty : totalLotQty;
    if (fillQty > 0) {
      _state.sblVolume = String(fillQty);
      return true;
    }
    return false;
  };

  const fetchSblLots = async (force = false) => {
    _state.sblLotsLoading = true;
    _state.sblLotsAttempted = true;
    _state.sblLotsError = "";
    paint();
    try {
      const portData = await miniappApi.getPortfolio("kss", null, force);
      const rawLots = portData && portData.sbl && Array.isArray(portData.sbl.lots) ? portData.sbl.lots : [];
      _state.sblLots = rawLots.filter((l) => Number(l.remaining_qty ?? l.borrow_qty ?? 0) > 0);
      const retMap = {};
      for (const br of (portData?.brokers?.kss?.borrowed || [])) {
        const s = String(br.symbol || "").trim().toUpperCase();
        if (s) retMap[s] = Number(br.returnable_qty ?? br.borrow_qty ?? 0);
      }
      _state.sblReturnableBySym = retMap;
      _state.sblLotsLoaded = true;
      if (_state.sblAction === "return" && !_state.sblVolumeFromPrefill) {
        syncSblVolumeForSymbol(_state.symbol);
      }
    } catch (err) {
      _state.sblLotsError = (err && err.message) || "โหลดรายการหุ้นยืม SBL ไม่สำเร็จ";
    } finally {
      _state.sblLotsLoading = false;
      paint();
    }
  };

  const fetchQuickBundle = async (sym) => {
    const cleanSym = String(sym || "").trim().toUpperCase();
    if (!cleanSym) return false;
    _state.symbol = cleanSym;
    _state.quickBundleSym = cleanSym;
    const shortRoute = defaultRouteForSide("short");
    const longRoute = defaultRouteForSide("buy");
    const [poolRes, kssRes, libRes] = await Promise.allSettled([
      miniappApi.getSblPool(cleanSym),
      miniappApi.getQuote(cleanSym, shortRoute.broker, "short"),
      miniappApi.getQuote(cleanSym, longRoute.broker, "buy"),
    ]);
    _state.quickPool = poolRes.status === "fulfilled" ? poolRes.value : null;
    _state.quickKssQuote = kssRes.status === "fulfilled" ? kssRes.value : { available: false, error: kssRes.reason?.message };
    _state.quickLibQuote = libRes.status === "fulfilled" ? libRes.value : { available: false, error: libRes.reason?.message };
    if (poolRes.status === "rejected" && kssRes.status === "rejected" && libRes.status === "rejected") {
      throw poolRes.reason || new Error("อ่านข้อมูลคลัง/ราคาไม่สำเร็จ");
    }
    return true;
  };

  const loadRunbookPreview = async () => {
    _state.runbookOpen = true;
    _state.runbookLoading = true;
    _state.runbookError = "";
    paint();
    try {
      const res = await miniappApi.getRunbookExits();
      _state.runbookData = res;
      const sel = {};
      for (const it of res.items || []) {
        if (it.guard_status === "READY" || it.guard_status === "ADJUSTED_VOL") {
          sel[it.symbol] = true;
        }
      }
      _state.runbookSelected = sel;
    } catch (err) {
      _state.runbookError = (err && err.message) || "โหลดข้อมูล Runbook ไม่สำเร็จ";
      toast(`❌ ${_state.runbookError}`, "err");
    } finally {
      _state.runbookLoading = false;
      paint();
    }
  };

  const paint = () => {
    if (!isCurrent() || !container.isConnected) return;
    if (syncNav) syncNav();
    const prevModalBody = container.querySelector("#se-modal-body");
    const prevModalScroll = prevModalBody ? prevModalBody.scrollTop : 0;
    container.innerHTML = `
      ${renderTradeHubGrid(_state)}
      ${renderRunbookPanel(_state)}

      <button id="tr-manual-advance-toggle" class="adv-toggle" type="button" aria-expanded="${_state.manualAdvOpen ? "true" : "false"}">
        <span>⚙️ Advance (สั่งปกติ · Quick · SBL) ${_state.manualAdvOpen ? "▾" : "▸"}</span>
        <span class="muted" style="font-weight:500">${_state.manualAdvOpen ? "ซ่อน" : "เปิดโหมดสั่งเอง"}</span>
      </button>

      ${_state.manualAdvOpen
        ? `<div id="tr-manual-advance-panel" style="display:flex;flex-direction:column;gap:10px">
               <section class="card" style="padding:8px">
                 <div class="seg-bar seg-bar-3" style="margin:0">
                   <button class="seg-btn ${_state.subtab === "order" ? "active" : ""}" data-subtab="order" type="button">📈 สั่งปกติ</button>
                   <button class="seg-btn ${_state.subtab === "quick" ? "active" : ""}" data-subtab="quick" type="button">⚡ Quick</button>
                   <button class="seg-btn ${_state.subtab === "sbl" ? "active" : ""}" data-subtab="sbl" type="button">🔄 SBL</button>
                 </div>
               </section>

               ${_state.subtab === "order"
          ? renderOrderSubtab(_state)
          : _state.subtab === "quick"
            ? renderQuickSubtab()
            : renderSblSubtab(_state)
        }
             </div>`
        : ""
      }

      ${renderSuggestModal(_state)}
    `;
    const nextModalBody = container.querySelector("#se-modal-body");
    if (nextModalBody && prevModalScroll > 0) {
      nextModalBody.scrollTop = prevModalScroll;
    }

    const manualAdvBtn = container.querySelector("#tr-manual-advance-toggle");
    if (manualAdvBtn) {
      manualAdvBtn.onclick = () => {
        _state.manualAdvOpen = !_state.manualAdvOpen;
        paint();
        if (
          _state.manualAdvOpen &&
          _state.subtab === "sbl" &&
          _state.sblAction === "return" &&
          !_state.sblLotsLoaded &&
          !_state.sblLotsLoading &&
          !_state.sblLotsAttempted
        ) {
          fetchSblLots(false);
        }
      };
    }

    container.querySelectorAll("[data-subtab]").forEach((b) => {
      b.onclick = () => {
        _state.subtab = b.dataset.subtab;
        paint();
        if (
          _state.manualAdvOpen &&
          _state.subtab === "sbl" &&
          _state.sblAction === "return" &&
          !_state.sblLotsLoaded &&
          !_state.sblLotsLoading &&
          !_state.sblLotsAttempted
        ) {
          fetchSblLots(false);
        }
      };
    });

    if (_state.manualAdvOpen && _state.subtab === "order") {
      container.querySelectorAll("[data-side]").forEach((b) => {
        b.onclick = () => {
          _state.side = b.dataset.side;
          const def = defaultRouteForSide(_state.side);
          _state.broker = def.broker;
          _state.account = def.account;
          resetPreview();
          paint();
        };
      });

      const advBtn = container.querySelector("#tr-advance-toggle");
      if (advBtn) {
        advBtn.onclick = () => {
          _state.orderAdvOpen = !_state.orderAdvOpen;
          paint();
        };
      }

      const routeEl = container.querySelector("#tr-route");
      if (routeEl) {
        routeEl.onchange = () => {
          const [b, a] = routeEl.value.split(":");
          _state.broker = b;
          _state.account = a;
          resetPreview();
          paint();
        };
      }

      const symEl = container.querySelector("#tr-symbol");
      if (symEl) {
        symEl.oninput = () => {
          _state.symbol = symEl.value.toUpperCase();
          resetPreview();
        };
      }

      const quoteBtn = container.querySelector("#tr-quote-btn");
      if (quoteBtn) {
        quoteBtn.onclick = () =>
          runLockedButton(
            quoteBtn,
            "⏳...",
            async () => {
              const sym = (_state.symbol || symEl?.value || "").trim().toUpperCase();
              if (!sym) return toast("กรุณาใส่ชื่อหุ้น", "err");
              _state.symbol = sym;
              _state.quote = await miniappApi.getQuote(sym, _state.broker, _state.side);
              if (!_state.quote.available) {
                toast(`⚠️ ${_state.quote.error || "ดึงราคาไม่ได้"}`, "err");
              }
              paint();
            },
            { toast }
          );
      }

      container.querySelectorAll("[data-tap-px]").forEach((el) => {
        el.onclick = () => {
          if (!el.dataset.tapPx) return;
          _state.price = el.dataset.tapPx;
          _state.orderAdvOpen = true;
          resetPreview();
          paint();
        };
      });

      container.querySelectorAll("[data-amode]").forEach((b) => {
        b.onclick = () => {
          _state.amountMode = b.dataset.amode;
          if (_state.amountMode === "value" && !_state.targetValueDirty) {
            _state.targetValue = String(getSavedBaseBudget(_state));
          }
          resetPreview();
          paint();
        };
      });

      const volEl = container.querySelector("#tr-vol");
      if (volEl) {
        volEl.oninput = () => {
          _state.volume = volEl.value;
          resetPreview();
        };
      }
      container.querySelectorAll("[data-add-vol]").forEach((b) => {
        b.onclick = () => {
          const cur = Number(_state.volume || 0);
          _state.volume = String(cur + Number(b.dataset.addVol));
          _state.amountMode = "volume";
          resetPreview();
          paint();
        };
      });

      const budgetEl = container.querySelector("#tr-budget");
      if (budgetEl) {
        budgetEl.oninput = () => {
          _state.targetValue = budgetEl.value;
          _state.targetValueDirty = true;
          resetPreview();
        };
      }
      container.querySelectorAll("[data-set-budget]").forEach((b) => {
        b.onclick = () => {
          _state.targetValue = String(b.dataset.setBudget);
          _state.targetValueDirty = true;
          resetPreview();
          paint();
        };
      });

      const pxEl = container.querySelector("#tr-price");
      if (pxEl) {
        pxEl.oninput = () => {
          _state.price = pxEl.value;
          resetPreview();
        };
      }

      const downBtn = container.querySelector("#tr-px-down");
      const upBtn = container.querySelector("#tr-px-up");
      const stepPrice = async (dir) => {
        const sym = (_state.symbol || symEl?.value || "").trim().toUpperCase();
        const cur = Number(_state.price || _state.quote?.last || 0);
        if (!sym || !Number.isFinite(cur) || cur <= 0) {
          toast("กรุณาใส่ชื่อหุ้นและราคาอ้างอิงก่อนปรับ Tick", "err");
          return;
        }
        if (downBtn) downBtn.disabled = true;
        if (upBtn) upBtn.disabled = true;
        try {
          const q2 = await miniappApi.getNegoQuote(_state.broker, sym, _state.side, cur);
          const nxt = dir > 0 ? q2.tick_up : q2.tick_down;
          if (nxt) {
            _state.price = String(nxt);
            resetPreview();
            paint();
          }
        } catch (err) {
          toast(`❌ ปรับ Tick ไม่สำเร็จ: ${err.message}`, "err");
        } finally {
          if (downBtn) downBtn.disabled = false;
          if (upBtn) upBtn.disabled = false;
        }
      };
      if (downBtn) downBtn.onclick = () => stepPrice(-1);
      if (upBtn) upBtn.onclick = () => stepPrice(1);

      const buildPayload = () => ({
        side: _state.side,
        symbol: (_state.symbol || symEl?.value || "").trim().toUpperCase(),
        broker: _state.broker,
        account: _state.account,
        amount_mode: _state.amountMode,
        volume: _state.amountMode === "volume" ? Number(_state.volume || 0) : 0,
        target_value: _state.amountMode === "value" ? Number(_state.targetValue || 0) : null,
        price: _state.price ? Number(_state.price) : null,
      });

      const openOrderConfirm = (prev, payload) => {
        confirmSheet({
          title: `ยืนยันส่งคำสั่ง [${prev.mode_label}]`,
          body: `${prev.cmd.toUpperCase()} ${prev.symbol} · ${fmtInt(prev.resolved_volume)} หุ้น @ ฿${fmtMoney(prev.resolved_price)} (รวม ฿${fmtMoney(prev.total_thb)} · ${String(prev.broker).toUpperCase()} ${prev.account.toUpperCase()})`,
          confirmLabel: prev.dry_run ? "ส่ง DRY-RUN" : "ส่งคำสั่ง LIVE",
          danger: !prev.dry_run,
          busyTitle: `⏳ กำลังส่งคำสั่ง ${prev.cmd.toUpperCase()} ${prev.symbol}...`,
          onConfirm: async () => {
            const res = await miniappApi.executeTrade({
              ...payload,
              idempotency_key: _state.idemKey,
            });
            const ordNo = res.execution?.broker_order_no || res.draft_id || "—";
            const totalVal = Number(res.order.volume || 0) * Number(res.order.price || 0);
            markPortfolioStale();
            resetPreview();
            paint();
            return {
              ok: true,
              dryRun: Boolean(res.dry_run),
              title: res.dry_run ? "🧪 [DRY-RUN] ส่งคำสั่งสำเร็จ" : "✅ ส่งคำสั่งสำเร็จ",
              detail: `${res.order.cmd.toUpperCase()} ${res.order.symbol} ${fmtInt(res.order.volume)} หุ้น @ ฿${fmtMoney(res.order.price)} (#${ordNo})`,
              details: [
                { label: "Order #", value: `#${ordNo}` },
                { label: "คำสั่ง / หุ้น", value: `${res.order.cmd.toUpperCase()} ${res.order.symbol}` },
                { label: "จำนวน @ ราคา", value: `${fmtInt(res.order.volume)} หุ้น @ ฿${fmtMoney(res.order.price)}` },
                { label: "มูลค่ารวม", value: `฿${fmtMoney(totalVal)}` },
                { label: "โบรกเกอร์ / บัญชี", value: `${String(res.order.broker || "").toUpperCase()} · ${res.order.account}` },
              ],
              ctas: [{ label: "📋 ไปดู Orders", primary: true, onClick: () => navigate("orders") }],
              closeLabel: "📝 ทำรายการต่อ",
            };
          },
        });
      };

      const previewBtn = container.querySelector("#tr-preview-btn");
      if (previewBtn) {
        previewBtn.onclick = () =>
          runLockedButton(
            previewBtn,
            "⏳ กำลังตรวจ...",
            async () => {
              const payload = buildPayload();
              if (!payload.symbol) return toast("กรุณาระบุชื่อหุ้น", "err");
              const prev = await miniappApi.previewTrade(payload);
              _state.preview = prev;
              _state.idemKey = newIdempotencyKey();
              paint();
              if (prev && prev.ok) {
                openOrderConfirm(prev, payload);
              }
            },
            { toast }
          );
      }

      const jumpSbl = container.querySelector("#tr-jump-sbl");
      if (jumpSbl) {
        jumpSbl.onclick = () => {
          _state.subtab = "quick";
          paint();
        };
      }
    } else if (_state.manualAdvOpen && _state.subtab === "quick") {
      const symInput = container.querySelector("#quick-sym-input");
      if (symInput) {
        symInput.oninput = () => {
          const val = symInput.value.trim().toUpperCase();
          _state.symbol = val;
          const hintEl = container.querySelector("#quick-suggest-hint");
          const candSides = (_state.hubData && _state.hubData.suggest_entry && _state.hubData.suggest_entry.candidate_sides) || {};
          const candSide = val ? candSides[val] || null : null;
          if (hintEl) {
            if (candSide === "SHORT") {
              hintEl.style.display = "";
              hintEl.innerHTML = `🎯 Suggest Entry วันนี้: <strong class="txt-amber">SHORT (${esc(val)} · KSS SBL)</strong>`;
            } else if (candSide === "LONG") {
              hintEl.style.display = "";
              hintEl.innerHTML = `🎯 Suggest Entry วันนี้: <strong class="txt-green">LONG (${esc(val)} · LIB Cash)</strong>`;
            } else {
              hintEl.style.display = "none";
              hintEl.innerHTML = "";
            }
          }
        };
      }

      const checkBtn = container.querySelector("#quick-check-btn");
      if (checkBtn) {
        checkBtn.onclick = () =>
          runLockedButton(
            checkBtn,
            "⏳ กำลังเช็ค...",
            async () => {
              const sym = (container.querySelector("#quick-sym-input")?.value || _state.symbol || "").trim().toUpperCase();
              if (!sym) return toast("กรุณาใส่ชื่อหุ้น", "err");
              await fetchQuickBundle(sym);
              paint();
            },
            { toast }
          );
      }

      container.querySelectorAll("[data-qbudget]").forEach((btn) => {
        btn.onclick = () => {
          _state.quickBudget = Number(btn.dataset.qbudget);
          _state.quickBudgetDirty = true;
          paint();
        };
      });

      const budgetInput = container.querySelector("#quick-budget-input");
      if (budgetInput) {
        budgetInput.oninput = () => {
          const val = Number(budgetInput.value);
          if (Number.isFinite(val) && val > 0) {
            _state.quickBudget = val;
            _state.quickBudgetDirty = true;
          }
        };
      }

      const autoShortChk = container.querySelector("#quick-auto-short");
      if (autoShortChk) {
        autoShortChk.onchange = () => {
          _state.quickAutoShort = Boolean(autoShortChk.checked);
          paint();
        };
      }

      const openBudgetNegotiationSheet = async (mode) => {
        const inputSym = (container.querySelector("#quick-sym-input")?.value || _state.symbol || "").trim().toUpperCase();
        if (!inputSym) return toast("กรุณาระบุชื่อหุ้นก่อนทำรายการ", "err");
        const budget = Number(_state.quickBudget || 0);
        if (!Number.isFinite(budget) || budget <= 0) return toast("กรุณาระบุจำนวนเงิน (บาท) ที่มากกว่า 0", "err");

        if (_state.quickBundleSym !== inputSym || (!_state.quickKssQuote && !_state.quickLibQuote)) {
          try {
            await fetchQuickBundle(inputSym);
            paint();
          } catch (err) {
            return toast(`❌ ${err.message}`, "err");
          }
        }

        const isBuyLib = mode === "lib_buy";
        const longRoute = defaultRouteForSide("buy");
        const shortRoute = defaultRouteForSide("short");
        const activeRoute = isBuyLib ? longRoute : shortRoute;
        const activeRouteTxt = routeLabel(activeRoute.broker, activeRoute.account);
        const autoShort = !isBuyLib && Boolean(_state.quickAutoShort);
        const quote = isBuyLib ? _state.quickLibQuote : _state.quickKssQuote;
        const pool = _state.quickPool;

        if (!isBuyLib && pool && pool.xd_risk && pool.xd_risk.xd_risk) {
          return toast(`⛔ ห้ามยืม ${inputSym}: ${pool.xd_risk.reason || "ติด XD/XR"}`, "err");
        }

        const bid = Number(quote && quote.bid_1) || null;
        const offer = Number(quote && quote.offer_1) || null;
        const last = Number(quote && quote.last) || null;
        const activeRules = (ctx.statusData && ctx.statusData.trading_rules) || (_state.hubData && _state.hubData.trading_rules) || {};
        const quickTicks = getShortUptickTicks(activeRules, inputSym, "open");
        const strictUptickMin = autoShort && last ? (quickTicks === 0 ? last : nextSetTick(last, quickTicks)) : null;

        let selectedPx =
          quoteRefPrice(quote, isBuyLib ? "offer" : autoShort ? "short_open" : "last") || 10.0;
        const idemKey = newIdempotencyKey();
        const isDry = Boolean(dryRun);

        const sheetTitle = isBuyLib
          ? `ยืนยันซื้อ ${inputSym} (${activeRouteTxt})`
          : autoShort
            ? `ยืนยันยืม SBL + Short ทันที ${inputSym} (${activeRouteTxt})`
            : `ยืนยันยืม SBL ${inputSym} (KSS → เข้า (B))`;

        confirmSheet({
          title: sheetTitle,
          confirmLabel: isBuyLib
            ? isDry ? `ซื้อ ${activeRouteTxt} (DRY-RUN)` : `ส่งคำสั่งซื้อ ${activeRouteTxt}`
            : autoShort
              ? isDry ? "ยืม + Short (DRY-RUN)" : "ยืนยันยืม + Short LIVE"
              : isDry ? "ยืม SBL (DRY-RUN)" : "ยืนยันยืม SBL LIVE",
          danger: !isDry,
          renderBody: (bodyEl, controls) => {
            const renderSheetState = () => {
              const vol = calcBoardLotsFromBudget(budget, selectedPx);
              const totalThb = vol * selectedPx;
              const tick = setTickSize(selectedPx);
              const uptickWarn = Boolean(autoShort && strictUptickMin && selectedPx < strictUptickMin);
              const valid = vol >= 100 && selectedPx > 0 && !uptickWarn;
              controls.setConfirmEnabled(valid);

              bodyEl.innerHTML = `
                <div style="display:flex;flex-direction:column;gap:10px">
                  <div class="meta-row">
                    <span class="pill ${isDry ? "pill-amber" : "pill-red"}">${isDry ? "🧪 DRY-RUN" : "🔴 LIVE"}</span>
                    <span>โบรค: <strong>${esc(activeRouteTxt)}</strong></span>
                    <span>จำนวนเงิน: <strong>฿${fmtMoney(budget)}</strong></span>
                  </div>
                  <div class="quote-strip">
                    <button class="quote-box" data-qpick="${bid || ""}" ${!bid ? "disabled" : ""} type="button">
                      <small>Bid</small><strong>${bid ? `฿${fmtMoney(bid)}` : "—"}</strong>
                    </button>
                    <button class="quote-box" data-qpick="${last || ""}" ${!last ? "disabled" : ""} type="button">
                      <small>Last</small><strong>${last ? `฿${fmtMoney(last)}` : "—"}</strong>
                    </button>
                    <button class="quote-box" data-qpick="${offer || ""}" ${!offer ? "disabled" : ""} type="button">
                      <small>Offer</small><strong>${offer ? `฿${fmtMoney(offer)}` : "—"}</strong>
                    </button>
                  </div>
                  <label class="muted" style="margin-bottom:-4px">${autoShort ? (quickTicks === 0 ? "ราคา Short (Zero-Plus +0):" : "ราคา Short (Strict Uptick +1):") : isBuyLib ? "ราคาซื้อ Limit:" : "ราคาอ้างอิงคำนวณหุ้นยืม:"}</label>
                  <div class="price-stepper">
                    <button class="btn btn-ghost" data-tickstep="-1" type="button">-${tick.toFixed(2)}</button>
                    <input id="sheet-nego-price" class="text-input" type="number" step="${tick}" min="0.01" value="${selectedPx.toFixed(2)}" inputmode="decimal">
                    <button class="btn btn-ghost" data-tickstep="1" type="button">+${tick.toFixed(2)}</button>
                  </div>
                  <div class="code-block ${uptickWarn || vol < 100 ? "txt-red" : ""}">จำนวนคำนวณ: ${fmtInt(vol)} หุ้น (${vol / 100} lots) @ ฿${fmtMoney(selectedPx)}
มูลค่าจริง: ฿${fmtMoney(totalThb)}
${uptickWarn ? `⛔ SHORT ต้อง ≥ ฿${fmtMoney(strictUptickMin)} (${quickTicks === 0 ? "Zero-Plus ≥ Last" : "Last + 1 Tick"})` : ""}${vol < 100 ? "⛔ วงเงินไม่พอ 100 หุ้น" : ""}</div>
                </div>
              `;

              bodyEl.querySelectorAll("[data-qpick]").forEach((btn) => {
                btn.onclick = () => {
                  const p = Number(btn.dataset.qpick);
                  if (Number.isFinite(p) && p > 0) {
                    selectedPx = roundToSetTick(p) || p;
                    renderSheetState();
                  }
                };
              });
              bodyEl.querySelectorAll("[data-tickstep]").forEach((btn) => {
                btn.onclick = () => {
                  const next = nextSetTick(selectedPx, Number(btn.dataset.tickstep));
                  if (next) {
                    selectedPx = next;
                    renderSheetState();
                  }
                };
              });
              const pxInput = bodyEl.querySelector("#sheet-nego-price");
              if (pxInput) {
                pxInput.onchange = () => {
                  const v = roundToSetTick(Number(pxInput.value));
                  if (v) selectedPx = v;
                  renderSheetState();
                };
              }
            };
            renderSheetState();
          },
          onConfirm: async () => {
            const vol = calcBoardLotsFromBudget(budget, selectedPx);
            if (isBuyLib) {
              const res = await miniappApi.executeTrade({
                side: "buy",
                symbol: inputSym,
                broker: longRoute.broker,
                account: longRoute.account,
                amount_mode: "volume",
                volume: vol,
                price: selectedPx,
                idempotency_key: idemKey,
              });
              markPortfolioStale();
              const ordNo = res.execution?.broker_order_no || res.draft_id || "—";
              return {
                ok: true,
                dryRun: Boolean(res.dry_run),
                title: res.dry_run ? `🧪 [DRY-RUN] ซื้อ ${activeRouteTxt} สำเร็จ` : `✅ ซื้อหุ้น ${activeRouteTxt} สำเร็จ`,
                detail: `BUY ${inputSym} ${fmtInt(vol)} หุ้น @ ฿${fmtMoney(selectedPx)} (#${ordNo})`,
                details: [
                  { label: "Order #", value: `#${ordNo}` },
                  { label: "หุ้น", value: `${inputSym} (${activeRouteTxt})` },
                  { label: "จำนวน @ ราคา", value: `${fmtInt(vol)} หุ้น @ ฿${fmtMoney(selectedPx)}` },
                ],
                ctas: [{ label: "📋 ไปดู Orders", primary: true, onClick: () => navigate("orders") }],
                closeLabel: "📝 ทำรายการต่อ",
              };
            }

            const res = await miniappApi.sblAction({
              action: "borrow",
              symbol: inputSym,
              amount_mode: "value",
              target_value: budget,
              ref_price: selectedPx,
              auto_short: autoShort,
              short_price: autoShort ? selectedPx : null,
              idempotency_key: idemKey,
            });
            markPortfolioStale();
            if (autoShort && res.partial_short_failed) {
              const shortErr = (res.auto_short && res.auto_short.error) || "ส่งคำสั่ง Short ไม่สำเร็จ";
              return {
                ok: false,
                kind: "warn",
                title: "⚠️ ยืม SBL แล้ว แต่ Short ไม่สำเร็จ",
                detail: shortErr,
                details: [
                  { label: "ยืม SBL", value: `${inputSym} ${fmtInt(res.volume)} หุ้น (เข้าพอร์ต (B) แล้ว)` },
                  { label: "ราคา Short ที่ส่ง", value: `฿${fmtMoney(selectedPx)}` },
                  { label: "สถานะ Short", value: shortErr },
                ],
                ctas: [{ label: "📊 ดูพอร์ต", primary: true, onClick: () => navigate("portfolio") }],
                closeLabel: "ปิด",
              };
            }
            const shortOrdNo = res.auto_short?.execution?.broker_order_no || res.auto_short?.draft_id || "";
            return {
              ok: true,
              dryRun: Boolean(res.dry_run),
              title: res.dry_run
                ? `🧪 [DRY-RUN] ${autoShort ? "ยืม + Short สำเร็จ" : "ยืม SBL สำเร็จ"}`
                : `✅ ${autoShort ? "ยืม SBL + Short สำเร็จ" : "ยืม SBL สำเร็จ"}`,
              detail: `${inputSym} ${fmtInt(res.volume)} หุ้น${autoShort ? ` @ ฿${fmtMoney(selectedPx)}` : " (เข้า (B))"}`,
              details: [
                { label: "หุ้น", value: `${inputSym} (KSS Margin)` },
                { label: "จำนวนที่ยืม", value: `${fmtInt(res.volume)} หุ้น` },
                ...(autoShort ? [{ label: "Order Short #", value: shortOrdNo ? `#${shortOrdNo}` : "ส่งแล้ว" }] : []),
              ],
              ctas: [
                {
                  label: autoShort ? "📋 ไปดู Orders" : "📊 ดูพอร์ต",
                  primary: true,
                  onClick: () => navigate(autoShort ? "orders" : "portfolio"),
                },
              ],
              closeLabel: "📝 ทำรายการต่อ",
            };
          },
        });
      };

      const borrowBtn = container.querySelector("#quick-borrow-btn");
      if (borrowBtn) {
        borrowBtn.onclick = () =>
          runLockedButton(borrowBtn, "⏳...", () => openBudgetNegotiationSheet("kss_borrow"), { toast });
      }

      const buyLibBtn = container.querySelector("#quick-buy-lib-btn");
      if (buyLibBtn) {
        buyLibBtn.onclick = () =>
          runLockedButton(buyLibBtn, "⏳...", () => openBudgetNegotiationSheet("lib_buy"), { toast });
      }
    } else if (_state.manualAdvOpen && _state.subtab === "sbl") {
      container.querySelectorAll("[data-sblact]").forEach((b) => {
        b.onclick = () => {
          _state.sblAction = b.dataset.sblact;
          _state.sblVolumeFromPrefill = false;
          if (_state.sblAction === "return") {
            if (!_state.sblLotsLoaded && !_state.sblLotsLoading) {
              _state.sblVolume = "";
              fetchSblLots(false);
            } else {
              if (!syncSblVolumeForSymbol(_state.symbol)) {
                _state.sblVolume = "";
              }
              paint();
            }
          } else {
            if (!_state.sblVolume) _state.sblVolume = "1000";
            paint();
          }
        };
      });

      const sblAdvBtn = container.querySelector("#sbl-advance-toggle");
      if (sblAdvBtn) {
        sblAdvBtn.onclick = () => {
          _state.sblAdvOpen = !_state.sblAdvOpen;
          paint();
        };
      }

      const refreshLotsBtn = container.querySelector("#sbl-refresh-lots-btn");
      if (refreshLotsBtn) refreshLotsBtn.onclick = () => fetchSblLots(true);

      container.querySelectorAll("[data-sbl-symchip]").forEach((chip) => {
        chip.onclick = () => {
          const sym = String(chip.dataset.sblSymchip || "").trim().toUpperCase();
          const qty = String(chip.dataset.sblQty || "").trim();
          _state.symbol = sym;
          _state.sblSelectedLotId = "";
          _state.sblVolumeFromPrefill = false;
          if (!syncSblVolumeForSymbol(sym) && qty && Number(qty) > 0) {
            _state.sblVolume = qty;
          }
          _state.sblPool = null;
          paint();
        };
      });

      const symEl = container.querySelector("#sbl-sym");
      if (symEl) {
        symEl.oninput = () => {
          _state.symbol = symEl.value.toUpperCase();
          _state.sblSelectedLotId = "";
        };
        symEl.onchange = () => {
          if (_state.sblAction === "return") {
            syncSblVolumeForSymbol(_state.symbol);
            paint();
          }
        };
      }
      const lotSel = container.querySelector("#sbl-lot-id");
      if (lotSel) {
        lotSel.onchange = () => {
          const chosen = String(lotSel.value || "").trim();
          _state.sblSelectedLotId = chosen;
          const cleanSym = (_state.symbol || "").trim().toUpperCase();
          const matching = (_state.sblLots || []).filter(
            (l) => String(l.symbol || "").trim().toUpperCase() === cleanSym
          );
          if (chosen) {
            const found = matching.find((l) => String(l.broker_tx_id || l.lot_id || "").trim() === chosen);
            const rem = Number(found?.remaining_qty ?? found?.borrow_qty ?? 0);
            if (rem > 0) _state.sblVolume = String(rem);
          } else {
            const totalRem = matching.reduce((acc, l) => acc + Number(l.remaining_qty ?? l.borrow_qty ?? 0), 0);
            if (totalRem > 0) _state.sblVolume = String(totalRem);
          }
          paint();
        };
      }
      const volEl = container.querySelector("#sbl-vol");
      if (volEl) volEl.oninput = () => (_state.sblVolume = volEl.value);
      const remarkEl = container.querySelector("#sbl-remark");
      if (remarkEl) remarkEl.oninput = () => (_state.sblRemark = remarkEl.value);

      const checkBtn = container.querySelector("#sbl-check-btn");
      if (checkBtn) {
        checkBtn.onclick = () =>
          runLockedButton(
            checkBtn,
            "⏳...",
            async () => {
              const sym = (_state.symbol || "").trim().toUpperCase();
              if (!sym) return toast("กรุณาระบุชื่อหุ้น", "err");
              if (_state.sblAction === "return") {
                await fetchSblLots(true);
                const matched = (_state.sblLots || []).filter(
                  (l) => String(l.symbol || "").trim().toUpperCase() === sym
                );
                const totalRem = matched.reduce((acc, l) => acc + Number(l.remaining_qty ?? l.borrow_qty ?? 0), 0);
                if (totalRem > 0) {
                  _state.sblVolume = String(totalRem);
                  _state.sblSelectedLotId = "";
                  toast(`พบ ${sym} เปิดยืม ${matched.length} สัญญา · รวม ${fmtInt(totalRem)} หุ้น`, "ok");
                } else {
                  toast(`ไม่พบสัญญา SBL ${sym} ที่เปิดอยู่บน MainSBL`, "err");
                }
              }
              _state.sblPool = await miniappApi.getSblPool(sym);
              paint();
            },
            { toast }
          );
      }

      const fireSbl = (modeAll = false) => {
        const sym = (_state.symbol || "").trim().toUpperCase();
        if (!sym) return toast("กรุณาระบุชื่อหุ้น", "err");
        const act = _state.sblAction;
        const vol = modeAll ? null : Number(_state.sblVolume || 0);
        if (!modeAll && (!Number.isFinite(vol) || vol < 100)) {
          return toast("กรุณาเลือกหุ้นหรือระบุจำนวน (≥ 100 หุ้น)", "err");
        }
        const lotId = act === "return" ? String(_state.sblSelectedLotId || "").trim() : "";
        const remark = act === "return" ? String(_state.sblRemark || "").trim() : "";
        const lotDesc = lotId ? `สัญญา ${lotId}` : "FIFO อัตโนมัติ";
        confirmSheet({
          title: `ยืนยัน SBL ${act.toUpperCase()} ${sym}`,
          body: modeAll
            ? `คืนหุ้นยืม ${sym} ทั้งหมด (${lotDesc})`
            : `${act === "borrow" ? "ขอยืม" : "ขอคืน"} ${sym} จำนวน ${fmtInt(vol)} หุ้น (${act === "return" ? `${lotDesc} · ` : ""}KSS Margin)`,
          confirmLabel: "ยืนยัน",
          danger: false,
          busyTitle: `⏳ กำลังส่งคำสั่ง SBL ${act.toUpperCase()} ${sym}...`,
          onConfirm: async () => {
            const payload = {
              action: act,
              symbol: sym,
              volume: vol,
              amount_mode: modeAll ? "all" : "volume",
            };
            if (lotId) payload.lot_id = lotId;
            if (remark) payload.remark = remark;
            const res = await miniappApi.sblAction(payload);
            const retRefs = Array.isArray(res.lots_returned)
              ? res.lots_returned.map((l) => l.ref_no).filter((r) => r && r !== sym)
              : [];
            const refNote = retRefs.length ? retRefs.join(", ") : lotId || "ค้นหาจากชื่อหุ้น";
            markPortfolioStale();
            if (act === "return") {
              _state.sblSelectedLotId = "";
              try {
                await fetchSblLots(true);
              } catch (_ignored) { }
            }
            const actTh = act === "borrow" ? "ขอยืม SBL" : "คืนหุ้น SBL";
            return {
              ok: true,
              dryRun: Boolean(res.dry_run),
              title: res.dry_run ? `🧪 [DRY-RUN] ${actTh} สำเร็จ` : `✅ ${actTh} สำเร็จ`,
              detail: `${sym} จำนวน ${fmtInt(res.volume)} หุ้น`,
              details: [
                { label: "หุ้น", value: sym },
                { label: "รายการ", value: actTh },
                { label: "จำนวน", value: `${fmtInt(res.volume)} หุ้น` },
                ...(act === "return" ? [{ label: "Ref.no", value: refNote }] : []),
                { label: "โหมด", value: res.dry_run ? "DRY-RUN" : "LIVE (Krungsri SBL)" },
              ],
              ctas: [{ label: "📊 ดูพอร์ต", primary: true, onClick: () => navigate("portfolio") }],
              closeLabel: "ปิด",
            };
          },
        });
      };

      const execSblBtn = container.querySelector("#sbl-exec-btn");
      if (execSblBtn) execSblBtn.onclick = () => fireSbl(false);
      const retAllBtn = container.querySelector("#sbl-ret-all-btn");
      if (retAllBtn) retAllBtn.onclick = () => fireSbl(true);
    }

    const rbFetchBtn = container.querySelector("#tr-runbook-fetch-btn");
    if (rbFetchBtn) {
      rbFetchBtn.onclick = () =>
        runLockedButton(rbFetchBtn, "⏳ กำลังโหลด...", loadRunbookPreview, { toast });
    }
    const rbRefreshBtn = container.querySelector("#tr-runbook-refresh-btn");
    if (rbRefreshBtn) {
      rbRefreshBtn.onclick = () =>
        runLockedButton(rbRefreshBtn, "⏳...", loadRunbookPreview, { toast });
    }
    const rbCloseBtn = container.querySelector("#tr-runbook-close-btn");
    if (rbCloseBtn) {
      rbCloseBtn.onclick = () => {
        _state.runbookOpen = false;
        paint();
      };
    }
    const rbAllowStaleEl = container.querySelector("#tr-runbook-allow-stale");
    if (rbAllowStaleEl) {
      rbAllowStaleEl.onchange = () => {
        _state.runbookAllowStale = Boolean(rbAllowStaleEl.checked);
        paint();
      };
    }
    container.querySelectorAll("[data-rb-sym]").forEach((cb) => {
      cb.onchange = () => {
        const sym = cb.dataset.rbSym;
        _state.runbookSelected[sym] = Boolean(cb.checked);
        paint();
      };
    });
    const rbGotoNego = container.querySelector("#tr-runbook-goto-nego");
    if (rbGotoNego) rbGotoNego.onclick = () => navigate("nego");

    const rbSubmitBtn = container.querySelector("#tr-runbook-submit-btn");
    if (rbSubmitBtn) {
      rbSubmitBtn.onclick = () => {
        const d = _state.runbookData;
        if (!d) return;
        const chosen = (d.items || []).filter(
          (it) =>
            (it.guard_status === "READY" || it.guard_status === "ADJUSTED_VOL") &&
            _state.runbookSelected[it.symbol] !== false
        );
        if (chosen.length === 0) return toast("กรุณาเลือกอย่างน้อย 1 รายการ", "err");
        const isDry = Boolean(d.dry_run);
        const syms = chosen.map((it) => it.symbol);
        const summary = chosen
          .map((it) => `${it.symbol} ${it.action} ${fmtInt(it.net_send_vol)} (${String(it.broker || "").toUpperCase()})`)
          .join(", ");
        confirmSheet({
          title: `ยืนยัน Runbook Exit (${chosen.length} ไม้)?`,
          body: `${summary} · ${formatRunbookMarketPhase(d.market_phase, d.holiday_name)} · ${isDry ? "🧪 โหมด DRY-RUN" : "🔴 โหมด LIVE AUTOTRADE"}`,
          confirmLabel: isDry ? "ยืนยัน (DRY-RUN)" : "ยืนยันส่ง Exit",
          danger: !isDry,
          busyTitle: "⏳ กำลังส่งข้อมูลเข้าคิว Runbook Exit...",
          onConfirm: async () => {
            const res = await miniappApi.submitRunbookExits({
              allow_stale: Boolean(_state.runbookAllowStale),
              symbols: syms,
            });
            markPortfolioStale();
            if (_state.runbookData && res.schedule) {
              _state.runbookData = { ..._state.runbookData, schedule: res.schedule };
            }
            paint();
            return {
              ok: true,
              dryRun: isDry,
              title:
                res.mode === "SCHEDULED"
                  ? "🗓️ ตั้งคิว Runbook Auto-Exit แล้ว"
                  : "✅ ดำเนินการ Runbook Exit แล้ว",
              detail: res.message || `โหมด ${res.mode}`,
              details: [
                { label: "โหมด", value: res.mode || "—" },
                { label: "เวลาสุ่มยิง", value: res.scheduled_time || "ทันที" },
                { label: "รายการ", value: summary },
              ],
              ctas: [
                { label: "📅 ไปดู Schedule", primary: true, onClick: () => navigate("nego") },
                { label: "📋 ไปดู Orders", onClick: () => navigate("orders") },
              ],
              closeLabel: "ปิด",
            };
          },
        });
      };
    }
    // Suggest Entry Primary Card & Modal Handlers
    const seBudgetEl = container.querySelector("#se-budget-input");
    if (seBudgetEl) {
      seBudgetEl.oninput = () => {
        _state.seBudgetInput = seBudgetEl.value;
        _state.seBudgetDirty = true;
      };
    }
    const applyAndSaveSlotBudget = async (rawVal, { useRecommended = false } = {}) => {
      const val = Number(rawVal);
      if (!useRecommended && (!Number.isFinite(val) || val < 1000)) return;
      if (Number.isFinite(val) && val >= 1000) {
        _state.seBudgetInput = String(val);
      }
      _state.seBudgetDirty = false;
      _state.targetValueDirty = false;
      if (Number.isFinite(val) && val >= 1000) syncBudgetFromBase(val);
      paint();
      try {
        const res = await miniappApi.saveSuggestEntryConfig(
          Number.isFinite(val) && val >= 1000 ? val : null,
          useRecommended
        );
        const savedVal = Math.round(Number(res.budget_per_slot_thb || val));
        _state.seBudgetInput = String(savedVal);
        syncBudgetFromBase(savedVal);
        toast(
          useRecommended
            ? `✨ ตั้งงบแนะนำ ฿${fmtInt(_state.seBudgetInput)}/ไม้ แล้ว`
            : `💾 ตั้งงบต่อไม้ ฿${fmtInt(_state.seBudgetInput)} แล้ว`,
          "ok"
        );
        await loadHubSummary();
      } catch (err) {
        toast(`❌ ${(err && err.message) || "บันทึกงบไม่สำเร็จ"}`, "err");
      }
    };
    const seApplyRecBtn = container.querySelector("#se-apply-recommend-btn");
    if (seApplyRecBtn) {
      seApplyRecBtn.onclick = () => applyAndSaveSlotBudget(seApplyRecBtn.dataset.applyRec, { useRecommended: true });
    }
    const seBudgetSaveBtn = container.querySelector("#se-budget-save-btn");
    if (seBudgetSaveBtn) {
      seBudgetSaveBtn.onclick = () =>
        runLockedButton(
          seBudgetSaveBtn,
          "⏳",
          async () => {
            const val = Number(container.querySelector("#se-budget-input")?.value || _state.seBudgetInput);
            if (!Number.isFinite(val) || val < 1000) {
              return toast("งบต่อ 1 ไม้ต้อง ≥ 1,000 บาท", "err");
            }
            const res = await miniappApi.saveSuggestEntryConfig(val);
            const savedVal = Math.round(Number(res.budget_per_slot_thb || val));
            _state.seBudgetInput = String(savedVal);
            _state.seBudgetDirty = false;
            _state.quickBudgetDirty = false;
            _state.targetValueDirty = false;
            syncBudgetFromBase(savedVal);
            toast(`💾 บันทึกงบต่อไม้ ฿${fmtInt(_state.seBudgetInput)} แล้ว`, "ok");
            await loadHubSummary();
          },
          { toast }
        );
    }
    const sePollBtn = container.querySelector("#tr-suggest-poll-btn");
    if (sePollBtn) {
      sePollBtn.onclick = () => runSuggestPoll({ openModal: true });
    }
    const closeSuggestModal = () => {
      stopSuggestStream();
      _state.seModalOpen = false;
      _state.seSubmitResult = null;
      paint();
    };
    const seCloseBtn = container.querySelector("#se-modal-close");
    if (seCloseBtn) seCloseBtn.onclick = closeSuggestModal;
    const seCloseFootBtn = container.querySelector("#se-modal-close-foot");
    if (seCloseFootBtn) seCloseFootBtn.onclick = closeSuggestModal;
    const seResCloseBtn = container.querySelector("#se-res-close");
    if (seResCloseBtn) seResCloseBtn.onclick = closeSuggestModal;
    const seBackdrop = container.querySelector("#se-poll-backdrop");
    if (seBackdrop) {
      seBackdrop.onclick = (e) => {
        if (e.target === seBackdrop && !_state.seSubmitting) closeSuggestModal();
      };
    }
    const seLiveToggle = container.querySelector("#se-live-toggle");
    if (seLiveToggle) {
      seLiveToggle.onclick = () => {
        _state.seLiveEnabled = !_state.seLiveEnabled;
        if (_state.seLiveEnabled) {
          paint();
          startSuggestStream();
        } else {
          stopSuggestStream();
          paint();
        }
      };
    }
    const seRepollBtn = container.querySelector("#se-modal-repoll");
    if (seRepollBtn) {
      seRepollBtn.onclick = () => runSuggestPoll({ openModal: true });
    }
    container.querySelectorAll("[data-se-filter]").forEach((btn) => {
      btn.onclick = () => {
        _state.seFilter = btn.dataset.seFilter || "all";
        paint();
      };
    });
    const seOverrideEl = container.querySelector("#se-override-hold");
    if (seOverrideEl) {
      seOverrideEl.onchange = () => {
        _state.seOverrideHoldCash = Boolean(seOverrideEl.checked);
        runSuggestPoll({ openModal: true });
      };
    }
    const seAllowStaleEl = container.querySelector("#se-allow-stale");
    if (seAllowStaleEl) {
      seAllowStaleEl.onchange = () => {
        _state.seAllowStale = Boolean(seAllowStaleEl.checked);
        runSuggestPoll({ openModal: true });
      };
    }
    container.querySelectorAll("[data-se-chk]").forEach((cb) => {
      cb.onchange = () => {
        const key = cb.dataset.seChk;
        const prev = _state.seSelected[key] || { checked: false, price_mode: "lure" };
        _state.seSelected[key] = { ...prev, checked: Boolean(cb.checked) };
        paint();
      };
    });
    container.querySelectorAll("[data-se-pmode]").forEach((btn) => {
      btn.onclick = () => {
        const key = btn.dataset.sePmode;
        const val = btn.dataset.val === "match" ? "match" : "lure";
        const prev = _state.seSelected[key] || { checked: false, price_mode: "lure" };
        _state.seSelected[key] = { ...prev, price_mode: val };
        paint();
      };
    });
    const seGotoOrdersBtn = container.querySelector("#se-goto-orders");
    if (seGotoOrdersBtn) {
      seGotoOrdersBtn.onclick = () => {
        closeSuggestModal();
        navigate("orders");
      };
    }
    const seSubmitBtn = container.querySelector("#se-modal-submit-btn");
    if (seSubmitBtn) {
      seSubmitBtn.onclick = () =>
        runLockedButton(
          seSubmitBtn,
          "⏳ กำลังส่ง...",
          async () => {
            const items = Object.entries(_state.seSelected)
              .filter(([, v]) => v && v.checked)
              .map(([key, v]) => {
                const idx = key.indexOf(":");
                return {
                  side: key.slice(0, idx),
                  symbol: key.slice(idx + 1),
                  price_mode: v.price_mode || "lure",
                };
              });
            if (items.length === 0) return toast("กรุณาเลือกหุ้นอย่างน้อย 1 ตัว", "err");
            stopSuggestStream();
            _state.seSubmitting = true;
            paint();
            try {
              const res = await miniappApi.submitSuggestEntry({
                items,
                allow_stale: Boolean(_state.seAllowStale),
                override_hold_cash: Boolean(_state.seOverrideHoldCash),
              });
              _state.seSubmitResult = res;
              _state.seSelected = {};
              markPortfolioStale();
              await loadHubSummary();
            } finally {
              _state.seSubmitting = false;
              paint();
            }
          },
          { toast }
        );
    }
  };

  paint();
  loadHubSummary();
  if (_state.manualAdvOpen && _state.subtab === "sbl" && _state.sblAction === "return" && !_state.sblLotsLoaded && !_state.sblLotsLoading && !_state.sblLotsAttempted) {
    fetchSblLots(false);
  }
}
