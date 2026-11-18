/** Shared safe escaping and Thai trading formatting helpers. */

export function esc(val) {
  return String(val ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

export function fmtMoney(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return "—";
  return Number(val).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

export function fmtInt(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return "—";
  return Math.trunc(Number(val)).toLocaleString("en-US");
}

export function fmtSignedPct(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) return "—";
  const n = Number(val);
  return `${n >= 0 ? "+" : ""}${n.toFixed(2)}%`;
}

/** Format signed THB currency text with green/red utility class. */
export function renderSignedMoney(val) {
  if (val === null || val === undefined || Number.isNaN(Number(val))) {
    return { cls: "muted", text: "—" };
  }
  const n = Number(val);
  return {
    cls: n >= 0 ? "txt-green" : "txt-red",
    text: `${n >= 0 ? "+" : "-"}฿${fmtMoney(Math.abs(n))}`,
  };
}

/** Pick reference quote price for budget lot sizing based on preferred side/strategy. */
export function quoteRefPrice(quote, prefer = "offer") {
  if (!quote || !quote.available) return null;
  if (prefer === "short_open") {
    const openPx = quote.open_plan && Number(quote.open_plan.price || quote.open_plan.recommended_price);
    if (Number.isFinite(openPx) && openPx > 0) return openPx;
  }
  if (prefer === "short_open" || prefer === "short_lure") {
    const lurePx = quote.lure_plan && Number(quote.lure_plan.price || quote.lure_plan.recommended_price);
    if (Number.isFinite(lurePx) && lurePx > 0) return lurePx;
  }
  if (prefer === "offer") return Number(quote.offer_1 || quote.last || quote.bid_1 || 0) || null;
  return Number(quote.last || quote.offer_1 || quote.bid_1 || 0) || null;
}

/** Render combined KSS SBL pool/XD risk & dual-broker live quote preview block. */
export function renderQuickQuoteBox(pool, kssQ, libQ, budget, autoShort = false) {
  if (!pool && !kssQ && !libQ) return "";
  const xdWarn = pool && pool.xd_risk && pool.xd_risk.xd_risk;
  const kssRef = quoteRefPrice(kssQ, autoShort ? "short_open" : "last");
  const libRef = quoteRefPrice(libQ, "offer");
  const kssEst = kssRef ? calcBoardLotsFromBudget(budget, kssRef) : 0;
  const libEst = libRef ? calcBoardLotsFromBudget(budget, libRef) : 0;

  return `
    <div class="code-block ${xdWarn ? "txt-red" : ""}">
      ${
        pool
          ? `KSS SBL (${esc(pool.symbol)}): คลัง ${fmtInt(pool.available_qty)} หุ้น · ดอกเบี้ย ${pool.fee_rate}%/ปี\nXD Risk: ${xdWarn ? esc(pool.xd_risk.reason || "⛔ ติด XD/XR ห้ามยืม") : "🟢 ปกติ ไม่พบเครื่องหมาย XD/XR"}`
          : "KSS SBL: ยังไม่พบข้อมูลคลัง"
      }
      ${kssQ && kssQ.available ? `\nKSS Bid ฿${fmtMoney(kssQ.bid_1)} · Last ฿${fmtMoney(kssQ.last)} · Offer ฿${fmtMoney(kssQ.offer_1)}${kssEst ? ` (~${fmtInt(kssEst)} หุ้น)` : ""}` : `\nKSS Quote: ไม่พร้อม (${esc((kssQ && kssQ.error) || "-")})`}
      ${libQ && libQ.available ? `\nLIB Bid ฿${fmtMoney(libQ.bid_1)} · Last ฿${fmtMoney(libQ.last)} · Offer ฿${fmtMoney(libQ.offer_1)}${libEst ? ` (~${fmtInt(libEst)} หุ้น)` : ""}` : `\nLIB Quote: ไม่พร้อม (${esc((libQ && libQ.error) || "-")})`}
    </div>
  `;
}

export function newIdempotencyKey() {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `mini-${Date.now()}-${Math.random().toString(16).slice(2, 10)}`;
}

export function setTickSize(price) {
  const p = Number(price);
  if (!Number.isFinite(p) || p <= 0) return 0.01;
  if (p < 2) return 0.01;
  if (p < 5) return 0.02;
  if (p < 10) return 0.02;
  if (p < 25) return 0.05;
  if (p < 50) return 0.10;
  if (p < 100) return 0.25;
  if (p < 200) return 0.50;
  if (p < 400) return 1.00;
  return 2.00;
}

export function getShortUptickTicks(tradingRules, symbol = "", context = "nego") {
  const rules = tradingRules && typeof tradingRules === "object" ? tradingRules : {};
  const negoGlobal = Number(rules.short_uptick_ticks) === 1 ? 1 : 0;
  const openGlobal = rules.short_open_uptick_ticks !== undefined ? (Number(rules.short_open_uptick_ticks) === 1 ? 1 : 0) : 1;
  const cleanSym = String(symbol || "").trim().toUpperCase();
  let negoTicks = negoGlobal;
  if (cleanSym && negoTicks === 0) {
    const autoList = Array.isArray(rules.auto_strict_symbols) ? rules.auto_strict_symbols : [];
    const manualList = Array.isArray(rules.strict_uptick_symbols) ? rules.strict_uptick_symbols : [];
    const hit = (arr) => arr.some((s) => String(s || "").trim().toUpperCase() === cleanSym);
    if (hit(autoList) || hit(manualList)) negoTicks = 1;
  }
  if (String(context || "nego").toLowerCase() === "open") {
    return Math.max(openGlobal, negoTicks);
  }
  return negoTicks;
}

export function nextSetTick(price, steps = 1) {
  let cur = Number(price);
  if (!Number.isFinite(cur) || cur <= 0) return null;
  const dir = steps >= 0 ? 1 : -1;
  for (let i = 0; i < Math.abs(steps); i += 1) {
    if (dir > 0) {
      cur = Math.round((cur + setTickSize(cur)) * 100) / 100;
    } else {
      const tick = setTickSize(Math.max(0.01, cur - 0.0001));
      const next = Math.round((cur - tick) * 100) / 100;
      if (next > 0) cur = next;
      else break;
    }
  }
  return Math.round(cur * 100) / 100;
}

export function roundToSetTick(price) {
  const p = Number(price);
  if (!Number.isFinite(p) || p <= 0) return null;
  const tick = setTickSize(p);
  const rounded = Math.round(Math.round(p / tick) * tick * 100) / 100;
  return rounded > 0 ? rounded : tick;
}

export function calcBoardLotsFromBudget(budgetThb, price) {
  const budget = Number(budgetThb);
  const pxCents = Math.round(Number(price) * 100);
  if (!Number.isFinite(budget) || budget <= 0 || !Number.isFinite(pxCents) || pxCents <= 0) {
    return 0;
  }
  const shares = (budget * 100) / pxCents;
  return Math.max(0, Math.floor((shares + 1e-9) / 100) * 100);
}

export function renderErrorRetryCard({
  title = "เชื่อมต่อไม่สำเร็จ",
  message = "",
  retryId = "view-retry-btn",
  jumpBrokers = true,
} = {}) {
  return `
    <section class="card card-danger" data-error-retry-card="1">
      <div class="card-head">
        <div class="card-title">❌ ${esc(title)}</div>
        <span class="pill pill-red">RETRY</span>
      </div>
      <div class="muted">${esc(message || "ไม่สามารถดึงข้อมูลจาก Gateway/CDP ได้")}</div>
      <div class="btn-row">
        <button id="${esc(retryId)}" class="btn btn-primary" type="button">🔄 ลองโหลดใหม่</button>
        ${jumpBrokers ? `<button class="btn btn-ghost" data-jump-brokers="1" type="button">🏦 เช็ค Brokers</button>` : ""}
      </div>
    </section>
  `;
}

export async function runLockedButton(btn, busyLabel, fn, { toast = null, fallbackErr = "ดำเนินการไม่สำเร็จ" } = {}) {
  if (!btn || btn.disabled) return undefined;
  const origHtml = btn.innerHTML;
  btn.disabled = true;
  if (busyLabel) btn.innerHTML = esc(busyLabel);
  try {
    return await fn();
  } catch (err) {
    if (toast) toast(`❌ ${(err && err.message) || fallbackErr}`, "err");
    return undefined;
  } finally {
    btn.disabled = false;
    btn.innerHTML = origHtml;
  }
}

export function formatRunbookMarketPhase(phase, holidayName = "") {
  switch (phase) {
    case "HOLIDAY":
      return `🌙 วันหยุด SET (${holidayName || "หยุดทำการ"})`;
    case "WEEKEND":
      return "🌙 วันหยุดเสาร์–อาทิตย์";
    case "MARKET_OPEN_AM":
    case "MARKET_OPEN_PM":
      return "🟢 ตลาดเปิด — ยิง Exit (last ± 4 ช่อง)";
    case "LUNCH_BREAK":
      return "☕ พักเที่ยง (รอเปิดบ่าย 14:00)";
    case "PRE_MARKET":
      return "🗓️ ก่อนเปิดตลาด — Auto สุ่ม 10:01–10:28";
    case "AFTER_CLOSE":
    default:
      return "🌙 ตลาดปิดแล้ว";
  }
}

export function renderRunbookReadinessBar(data) {
  if (!data) return "";
  const r = data.readiness || {};
  const mktOpen = Boolean(r.is_market_open_now);
  const tradingDay = r.is_trading_day !== undefined ? Boolean(r.is_trading_day) : (data.market_phase !== "WEEKEND" && data.market_phase !== "HOLIDAY");
  const kss = (r.brokers && r.brokers.kss) || {};
  const lib = (r.brokers && r.brokers.lib) || {};
  const otp = r.otp_relay || {};
  const ksActive = Boolean(
    r.kill_switch !== undefined ? r.kill_switch : (data.kill_switch && data.kill_switch.is_active)
  );
  const reasons = Array.isArray(r.blocking_reasons) ? r.blocking_reasons : [];

  const brokerPill = (label, info) => {
    if (!info || info.ready === undefined) return "";
    const cls = info.ready ? "pill-green" : info.needed ? "pill-red" : "pill-muted";
    const icon = info.ready ? "✅" : info.needed ? "❌" : "⚪";
    const note = info.pin_locked ? " (PIN ล็อก)" : "";
    return `<span class="pill ${cls}">${icon} ${esc(label)}${esc(note)}</span>`;
  };

  return `
    <div class="meta-row" style="margin-bottom:6px;flex-wrap:wrap;gap:6px">
      <span class="pill ${mktOpen ? "pill-green" : tradingDay ? "pill-sky" : "pill-muted"}">${esc(formatRunbookMarketPhase(data.market_phase, data.holiday_name || r.holiday_name))}</span>
      <span class="pill ${data.is_today ? "pill-green" : "pill-amber"}">${data.is_today ? "✅" : "⚠️ Stale"} as_of: ${esc(data.as_of || "—")}</span>
      <span class="pill ${ksActive ? "pill-red" : "pill-green"}">${ksActive ? "🚨 Kill-Switch ON" : "✅ Kill-Switch OFF"}</span>
      ${brokerPill("KSS", kss)}
      ${brokerPill("LIB", lib)}
      ${otp.ready !== undefined ? `<span class="pill ${otp.ready ? "pill-green" : "pill-amber"}">${otp.ready ? "📲" : "⚠️"} OTP Relay</span>` : ""}
      <span class="pill ${data.dry_run ? "pill-amber" : "pill-red"}">${data.dry_run ? "🧪 DRY-RUN" : "🔴 LIVE"}</span>
    </div>
    ${
      reasons.length > 0
        ? `<div class="txt-amber" style="font-size:11.5px;margin-bottom:6px">⚠️ ข้อจำกัดตอนนี้: ${esc(reasons.slice(0, 3).join(" · "))}</div>`
        : ""
    }
  `;
}

const _TH_MONTHS = ["", "ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.", "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."];

/** Format LINE OA monthly quota reset date (1st 00:00 JST = 22:00 ICT prev day). */
export function formatQuotaResetLabel(quota) {
  if (quota && quota.reset_label) return String(quota.reset_label);
  const jstMs = Date.now() + 9 * 3600 * 1000;
  const jstNow = new Date(jstMs);
  const nextMonthJstUtcMs = Date.UTC(jstNow.getUTCFullYear(), jstNow.getUTCMonth() + 1, 1, 0, 0, 0) - 9 * 3600 * 1000;
  const rJst = new Date(nextMonthJstUtcMs + 9 * 3600 * 1000);
  const rBkk = new Date(nextMonthJstUtcMs + 7 * 3600 * 1000);
  const jstStr = `${rJst.getUTCDate()} ${_TH_MONTHS[rJst.getUTCMonth() + 1]} 00:00 JST`;
  const hh = String(rBkk.getUTCHours()).padStart(2, "0");
  const mm = String(rBkk.getUTCMinutes()).padStart(2, "0");
  const bkkStr = `${rBkk.getUTCDate()} ${_TH_MONTHS[rBkk.getUTCMonth() + 1]} ${hh}:${mm} น. ไทย`;
  return `รีเซ็ต ${jstStr} (${bkkStr})`;
}
