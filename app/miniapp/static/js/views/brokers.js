import { miniappApi } from "../api.js";
import { renderOtpWidget } from "../otp_widget.js";
import { formatQuotaResetLabel } from "../utils.js";
import { markPortfolioStale } from "./portfolio.js";

const _uiState = {
  brokerAdv: { kss: false, lib: false },
  sysAdvOpen: false,
  kssSelectedAcct: null,
  routingShort: null,
  routingLong: null,
  ruleOpenTicks: null,
  ruleTicks: null,
  ruleAutoMinus10: null,
};

const LOGIN_STEPS = [
  { num: 1, label: "เปิด Chrome" },
  { num: 2, label: "ส่งรหัส" },
  { num: 3, label: "รอ OTP" },
  { num: 4, label: "เข้าพอร์ต" },
];

function renderLoginStepper(lp, isAuth) {
  if (!lp) return "";
  const phase = String(lp.phase || "");
  if (phase === "idle" || (isAuth && phase === "authenticated")) return "";

  const isErr = phase === "error" || phase === "otp_rejected";
  const curStep = Number(lp.step || 0);
  const stepsHtml = LOGIN_STEPS.map((st) => {
    const done = !isErr && curStep > st.num;
    const active = curStep === st.num && !isErr;
    const errHere = isErr && (curStep === st.num || (curStep === 0 && st.num === 2));
    const cls = errHere ? "is-error" : done ? "is-done" : active ? "is-current" : "";
    return `
      <div class="stepper-node ${cls}">
        <span class="stepper-dot">${errHere ? "!" : done ? "✓" : st.num}</span>
        <span class="stepper-lbl">${esc(st.label)}</span>
      </div>
    `;
  }).join("");

  const msgTxt = lp.error || lp.message || "";
  const retryBadge = Number(lp.attempt || 1) > 1 ? ` · ครั้งที่ ${lp.attempt}` : "";
  return `
    <div class="login-stepper ${isErr ? "has-error" : ""}" data-login-stepper="${esc(lp.broker || "")}">
      <div class="stepper-track">${stepsHtml}</div>
      ${
        msgTxt
          ? `<div class="${isErr ? "broker-login-error" : "stepper-msg"}">${isErr ? "❌ " : "⏳ "}${esc(msgTxt)}${esc(retryBadge)}</div>`
          : ""
      }
    </div>
  `;
}

function esc(val) {
  return String(val ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function stateBadge(state) {
  const s = String(state || "UNKNOWN").toUpperCase();
  if (s === "AUTHENTICATED") return { cls: "pill-green", label: "🟢 AUTH" };
  if (s === "OTP_PAGE") return { cls: "pill-amber", label: "🔐 OTP" };
  if (s === "LOGIN_PAGE") return { cls: "pill-amber", label: "🟡 LOGIN" };
  if (s === "NO_TARGET_TAB") return { cls: "pill-amber", label: "🟡 NO_TAB" };
  if (s === "DAEMON_DOWN") return { cls: "pill-red", label: "🔴 DOWN" };
  return { cls: "pill-muted", label: `⚪ ${esc(s)}` };
}

function renderBrokerCard(b, defaultSlug = "", lp = null, queueActive = false, data = null) {
  const slug = b.slug || defaultSlug;
  const cdp = b.cdp || {};
  const mainState = cdp.state || (b.authenticated ? "AUTHENTICATED" : "DAEMON_DOWN");
  const badge = stateBadge(mainState);
  const daemonUp = Boolean(cdp.daemon_up) || mainState !== "DAEMON_DOWN";
  const isAuth = mainState === "AUTHENTICATED";
  const advOpen = Boolean(_uiState.brokerAdv[slug]);
  const loginActive = Boolean(lp && lp.is_active) && !isAuth;
  const hasError = Boolean(lp && lp.phase === "error") && !isAuth;

  const subPills = [];
  if (slug === "kss") {
    const strBadge = stateBadge(cdp.state || mainState);
    const sblState = (cdp.sbl && cdp.sbl.state) || "DAEMON_DOWN";
    const sblBadge = stateBadge(sblState);
    subPills.push(`<span class="pill ${strBadge.cls}">Streaming: ${esc(cdp.state || mainState)}</span>`);
    subPills.push(`<span class="pill ${sblBadge.cls}">SBL: ${esc(sblState)}</span>`);
  } else {
    subPills.push(`<span class="pill ${badge.cls}">Portfolio: ${esc(mainState)}</span>`);
  }

  const pidTxt = cdp.pid ? `PID ${esc(cdp.pid)}` : "PID -";
  const hbTxt = b.heartbeat ? `Heartbeat ${esc(b.heartbeat)}` : "No heartbeat";
  const acctTxt = b.account ? `บัญชี ${esc(b.account)}` : "";

  const kssAccounts =
    slug === "kss" && Array.isArray(data && data.kss_accounts) && data.kss_accounts.length
      ? data.kss_accounts
      : [
          { account_no: "<REDACTED_KSS_ACCT>", label: "Margin" },
          { account_no: "<REDACTED_KSS_ACCT>", label: "Cash" },
        ];
  const activeKss =
    _uiState.kssSelectedAcct ||
    (data && data.kss_active_account) ||
    (String(b.account || "").match(/\b\d{7}\b/) || [])[0] ||
    "<REDACTED_KSS_ACCT>";

  const kssSwitchHtml =
    slug === "kss" && isAuth
      ? `
        <div class="field-row" style="margin-top:8px">
          <select id="brokers-kss-acct" class="select-input" aria-label="สลับบัญชี KSS" style="min-height:38px;padding:6px 10px;font-size:12.5px">
            ${kssAccounts
              .map(
                (a) =>
                  `<option value="${esc(a.account_no)}" ${a.account_no === activeKss ? "selected" : ""}>KSS: ${esc(a.account_no)} (${esc(a.label)})</option>`
              )
              .join("")}
          </select>
          <button id="brokers-kss-switch-btn" class="btn btn-ghost" type="button" style="min-height:38px;padding:6px 12px;font-size:12.5px;white-space:nowrap">สลับบัญชี</button>
        </div>
      `
      : "";

  const btnLabel = loginActive
    ? "⏳ กำลังดำเนินการ..."
    : hasError
      ? "🔁 ลอง Login ใหม่"
      : daemonUp
        ? "🔄 Login / Resume"
        : "▶️ Start &amp; Login";
  const btnCls = hasError ? "btn-amber" : daemonUp ? "btn-primary" : "btn-success";

  const primaryActionHtml = isAuth
    ? ""
    : `
      <div class="btn-row">
        <button class="btn ${btnCls}" data-action="login" data-broker="${esc(slug)}" data-force="false" type="button" ${loginActive || queueActive ? "disabled" : ""}>
          ${btnLabel}
        </button>
      </div>
    `;

  const advPanelHtml = advOpen
    ? `
      <div class="adv-panel" data-broker-adv-panel="${esc(slug)}">
        <div class="meta-row" style="margin:0">
          <span>Port :${esc(b.port)}</span>
          <span>${pidTxt}</span>
          <span>${hbTxt}</span>
        </div>
        <div class="btn-row">
          ${
            isAuth
              ? `<button class="btn btn-primary" data-action="login" data-broker="${esc(slug)}" data-force="false" type="button">🔄 Resume</button>`
              : ""
          }
          <button class="btn btn-amber" data-action="force-login" data-broker="${esc(slug)}" type="button">⚡ Force</button>
          ${
            daemonUp
              ? `<button class="btn btn-ghost" data-action="stop" data-broker="${esc(slug)}" type="button">🛑 Stop</button>`
              : ""
          }
        </div>
      </div>
    `
    : "";

  return `
    <section class="card" data-broker-card="${esc(slug)}">
      <div class="card-head">
        <div>
          <div class="card-title">🏦 ${esc(b.label || slug.toUpperCase())}</div>
          ${acctTxt ? `<div class="muted" style="font-size:11.5px">${acctTxt}</div>` : ""}
        </div>
        <span class="pill ${badge.cls}">${badge.label}</span>
      </div>
      <div class="sub-pills">${subPills.join("")}</div>
      ${kssSwitchHtml}
      ${renderLoginStepper(lp, isAuth)}
      ${primaryActionHtml}
      <button class="adv-toggle" data-broker-adv="${esc(slug)}" type="button" aria-expanded="${advOpen ? "true" : "false"}">
        <span>⚙️ จัดการ ${advOpen ? "▾" : "▸"}</span>
        <span class="muted" style="font-weight:500">${daemonUp ? pidTxt : "ปิดอยู่"}</span>
      </button>
      ${advPanelHtml}
    </section>
  `;
}

function resolveEffectiveOtp(data, ctx) {
  const pendingOtp = data && data.pending_otp;
  const focusOtp = Boolean(ctx && ctx.params && ctx.params.focus === "otp");
  const showOtpBox = Boolean(pendingOtp) || focusOtp;
  const effectivePending =
    pendingOtp ||
    (focusOtp
      ? {
          broker: "KRUNGSRI",
          broker_label: "กรอก OTP ล่าสุด",
          ref: null,
          expires_in_seconds: 180,
          submitted: false,
        }
      : null);
  return { showOtpBox, effectivePending };
}

/** Update only #brokers-otp-card in-place so OTP never gets stuck and dropdowns never close. */
export function syncBrokersOtpSlot(container, data, ctx) {
  if (!container) return false;
  const card = container.querySelector("#brokers-otp-card");
  const host = container.querySelector("#brokers-otp-host");
  if (!card || !host) return false;
  const { showOtpBox, effectivePending } = resolveEffectiveOtp(data, ctx);
  if (!showOtpBox || !effectivePending) {
    card.classList.add("hidden");
    renderOtpWidget(host, { pending: null });
    return true;
  }
  card.classList.remove("hidden");
  renderOtpWidget(host, {
    pending: effectivePending,
    compact: false,
    error: (ctx && ctx.otpError) || "",
    prefix: "card-otp",
    onSubmit: (otp, broker) =>
      ctx && ctx.submitOtp
        ? ctx.submitOtp(otp, broker)
        : miniappApi.submitOtp(otp, broker).then(() => ctx && ctx.refreshStatus && ctx.refreshStatus()),
  });
  return true;
}

/** Deterministic snapshot key so idle 3s polls skip redundant DOM wipes. */
export function brokersRenderSignature(data, ctx) {
  const brokers = (data && data.brokers) || {};
  const kss = brokers.kss || {};
  const lib = brokers.lib || {};
  const lp = (data && data.login_progress) || {};
  const q = (data && data.line_quota) || {};
  const eod = (data && data.eod_status) || {};
  const pending = data && data.pending_otp;
  const qa = (ctx && ctx.queueAll) || {};
  return JSON.stringify({
    kss: {
      auth: Boolean(kss.authenticated),
      acct: kss.account || "",
      state: kss.cdp?.state || "",
      sbl: kss.cdp?.sbl?.state || "",
      pid: _uiState.brokerAdv.kss ? kss.cdp?.pid || "" : "",
      hb: _uiState.brokerAdv.kss ? kss.heartbeat || "" : "",
    },
    lib: {
      auth: Boolean(lib.authenticated),
      acct: lib.account || "",
      state: lib.cdp?.state || "",
      pid: _uiState.brokerAdv.lib ? lib.cdp?.pid || "" : "",
      hb: _uiState.brokerAdv.lib ? lib.heartbeat || "" : "",
    },
    lp: { kss: lp.kss || null, lib: lp.lib || null },
    relay: data?.otp_relay || null,
    ks: Boolean(data?.kill_switch?.is_active),
    pending: pending ? { broker: pending.broker || "", ref: pending.ref || "", sub: Boolean(pending.submitted) } : null,
    focusOtp: Boolean(ctx?.params?.focus === "otp"),
    otpError: (ctx && ctx.otpError) || "",
    qa: { active: Boolean(qa.active), step: qa.step || "", error: qa.error || "" },
    kssActive: data?.kss_active_account || "",
    kssAccounts: data?.kss_accounts || null,
    routing: data?.account_routing || null,
    rules: data?.trading_rules || null,
    quota: { used: q.used ?? null, limit: q.limit ?? null, exhausted: Boolean(q.exhausted), mode: q.push_mode || "" },
    eod: eod.last_result || eod.push_time || null,
    adv: { ..._uiState.brokerAdv, sys: _uiState.sysAdvOpen },
  });
}

export function renderBrokersView(container, data, ctx) {
  const brokers = (data && data.brokers) || {};
  const kss = brokers.kss || { slug: "kss", label: "Krungsri (KSS)", port: 9333, cdp: {} };
  const lib = brokers.lib || { slug: "lib", label: "Liberator", port: 9334, cdp: {} };
  const lp = (data && data.login_progress) || {};
  const relay = (data && data.otp_relay) || {};
  const ks = (data && data.kill_switch) || {};
  const queueAll = ctx.queueAll || { active: false, step: null, error: null };

  const bothAuth = Boolean(kss.authenticated && lib.authenticated);
  const { showOtpBox, effectivePending } = resolveEffectiveOtp(data, ctx);

  const otpHtml = `<section class="card card-otp ${showOtpBox ? "" : "hidden"}" id="brokers-otp-card"><div id="brokers-otp-host"></div></section>`;

  const connectAllHtml =
    bothAuth && !queueAll.active && !queueAll.error
      ? ""
      : `
      <section class="card card-connect-all">
        ${
          queueAll.active
            ? `
            <div class="card-head" style="margin-bottom:6px">
              <div class="card-title">⏳ กำลัง Login ทั้ง 2 โบรกเกอร์</div>
              <span class="pill pill-amber">${queueAll.step === "lib" ? "2/2 LIB" : "1/2 KSS"}</span>
            </div>
            <div class="muted" style="font-size:12px;margin-bottom:8px">
              ${queueAll.step === "lib" ? "Krungsri พร้อมแล้ว → กำลังเชื่อมต่อ Liberator..." : "กำลังเชื่อมต่อ Krungsri (KSS) ก่อน แล้วจะต่อ Liberator อัตโนมัติ"}
            </div>
            <button id="cancel-connect-all-btn" class="btn btn-ghost btn-sm" type="button">ยกเลิกคิวอัตโนมัติ</button>
          `
            : `
            <div class="btn-row">
              <button id="connect-all-btn" class="btn btn-primary" type="button">
                ⚡ Login ทั้ง 2 โบรกเกอร์ (KSS → LIB)
              </button>
            </div>
            ${
              queueAll.error
                ? `<div class="broker-login-error" style="margin-top:8px">❌ คิว Login หยุดที่ ${esc((queueAll.step || "").toUpperCase())}: ${esc(queueAll.error)}</div>`
                : ""
            }
          `
        }
      </section>
    `;

  const relayPill = relay.ready
    ? `<span class="pill pill-green">🟢 พร้อม (${esc(relay.channel || "miniapp")})</span>`
    : `<span class="pill pill-red">🔴 ไม่พร้อม (${esc(relay.reason || "disabled")})</span>`;

  const quota = (data && data.line_quota) || {};
  const qLimit = quota.limit ?? 300;
  const qUsed = quota.used;
  const qPct = Number(quota.pct || 0);
  const qFillCls = quota.exhausted || qPct >= 95 ? "danger" : qPct >= 75 ? "warn" : "";
  const qLabel = qUsed !== null && qUsed !== undefined ? `${qUsed}/${qLimit}` : `?/${qLimit}`;
  const eod = (data && data.eod_status) || {};
  const eodTime = eod.push_time || "17:05";
  const eodLast = eod.last_result;
  const eodNetEq = eodLast && eodLast.net_equity != null ? ` · Net Eq ฿${fmtInt(eodLast.net_equity)}` : "";
  const eodLine = eodLast
    ? `${esc(eodLast.date || "")} · ${eodLast.sent ? "ส่ง LINE แล้ว" : "บันทึก Inbox + Net Eq แล้ว"}${esc(eodNetEq)}`
    : `กำหนดบันทึก ${esc(eodTime)} น. (จ.-ศ. · MiniApp Inbox)`;

  const ksActive = Boolean(ks.is_active);
  const ksPill = ksActive
    ? `<span class="pill pill-red">🚨 ACTIVE</span>`
    : `<span class="pill pill-green">🟢 ปกติ</span>`;

  const ksBtn = ksActive
    ? `<button class="btn btn-success" data-action="ks-reset" type="button">✅ RESET Kill-Switch</button>`
    : `<button class="btn btn-danger" data-action="ks-activate" type="button">⛔ HALT NOW (Kill-Switch)</button>`;

  const routing = (data && data.account_routing) || {};
  const shortVal = _uiState.routingShort || `${routing.short_broker || "kss"}:${routing.short_account || "margin"}`;
  const longVal = _uiState.routingLong || `${routing.long_broker || "lib"}:${routing.long_account || "cash"}`;
  const shortOptions =
    (routing.options && Array.isArray(routing.options.short) && routing.options.short) || [
      { value: "kss:margin", label: "Krungsri · Margin (<REDACTED_KSS_ACCT>)" },
    ];
  const longOptions =
    (routing.options && Array.isArray(routing.options.long) && routing.options.long) || [
      { value: "lib:cash", label: "Liberator · Cash Balance" },
      { value: "kss:cash", label: "Krungsri · Cash (<REDACTED_KSS_ACCT>)" },
      { value: "kss:margin", label: "Krungsri · Margin (<REDACTED_KSS_ACCT>)" },
    ];

  const routingCardHtml = `
    <section class="card" id="brokers-routing-card">
      <div class="card-head" style="margin-bottom:6px">
        <div class="card-title">🎯 บัญชีหลักตามกลยุทธ์</div>
        <span class="pill pill-sky">Default (Trade)</span>
      </div>
      <div class="muted" style="font-size:11.5px;margin-bottom:8px">
        กำหนดค่าเริ่มต้นสำหรับหน้า Trade (สั่งปกติ &amp; Quick) — Auto-Exit ยังส่งตามโบรกเกอร์ที่ถือจริง
      </div>
      <div class="field-col" style="margin-bottom:8px">
        <label class="field-label" for="routing-short-select">📉 บัญชีหลักสำหรับ Short (Cover)</label>
        <select id="routing-short-select" class="select-input">
          ${shortOptions
            .map((o) => `<option value="${esc(o.value)}" ${o.value === shortVal ? "selected" : ""}>${esc(o.label)}</option>`)
            .join("")}
        </select>
      </div>
      <div class="field-col" style="margin-bottom:10px">
        <label class="field-label" for="routing-long-select">📈 บัญชีหลักสำหรับ Long (Buy/Sell)</label>
        <select id="routing-long-select" class="select-input">
          ${longOptions
            .map((o) => `<option value="${esc(o.value)}" ${o.value === longVal ? "selected" : ""}>${esc(o.label)}</option>`)
            .join("")}
        </select>
      </div>
      <button id="routing-save-btn" class="btn btn-primary" type="button">💾 บันทึกบัญชีหลัก</button>
    </section>
  `;

  const trRules = (data && data.trading_rules) || {
    short_open_uptick_ticks: 1,
    short_uptick_ticks: 0,
    auto_minus10_strict: true,
    auto_strict_details: [],
    auto_strict_symbols: [],
  };
  const activeOpenTicks =
    _uiState.ruleOpenTicks !== null
      ? Number(_uiState.ruleOpenTicks)
      : trRules.short_open_uptick_ticks !== undefined
        ? (Number(trRules.short_open_uptick_ticks) === 1 ? 1 : 0)
        : 1;
  const activeRuleTicks =
    _uiState.ruleTicks !== null
      ? Number(_uiState.ruleTicks)
      : Number(trRules.short_uptick_ticks) === 1
        ? 1
        : 0;
  const autoMinus10 =
    _uiState.ruleAutoMinus10 !== null
      ? Boolean(_uiState.ruleAutoMinus10)
      : trRules.auto_minus10_strict !== false;
  const autoDetails = Array.isArray(trRules.auto_strict_details) ? trRules.auto_strict_details : [];
  const autoAsOf = trRules.auto_strict_as_of || "";
  const autoBadgesHtml = !autoMinus10
    ? `<span class="muted" style="font-size:11px">ปิดตรวจ -10% อัตโนมัติ</span>`
    : autoDetails.length
      ? autoDetails
          .map((d) => `<span class="pill pill-amber">${esc(d.symbol)} (${Number(d.pct_change).toFixed(1)}%)</span>`)
          .join(" ")
      : `<span class="muted" style="font-size:11px">ไม่พบหุ้นปิดร่วง ≤ -10% ล่าสุด${autoAsOf ? ` (${esc(autoAsOf)})` : ""}</span>`;

  container.innerHTML = `
    ${otpHtml}
    ${connectAllHtml}
    ${renderBrokerCard(kss, "kss", lp.kss || null, Boolean(queueAll.active), data)}
    ${renderBrokerCard(lib, "lib", lp.lib || null, Boolean(queueAll.active), data)}
    ${routingCardHtml}
    <section class="card ${ksActive ? "card-danger" : ""}">
      <div class="card-head">
        <div class="field-row" style="gap:8px">
          <div class="card-title">🛡️ ความปลอดภัย</div>
          ${ksPill}
        </div>
        <button class="pill-btn pill-sky" data-action="refresh" type="button">🔄 รีเฟรช</button>
      </div>
      <div class="btn-row">${ksBtn}</div>
      <button id="brokers-sys-adv-toggle" class="adv-toggle" type="button" aria-expanded="${_uiState.sysAdvOpen ? "true" : "false"}">
        <span>⚙️ เครื่องมือระบบ (Advance) ${_uiState.sysAdvOpen ? "▾" : "▸"}</span>
        <span class="muted" style="font-weight:500">Open +${activeOpenTicks} / Nego +${activeRuleTicks} · EOD</span>
      </button>
      ${
        _uiState.sysAdvOpen
          ? `<div id="brokers-sys-adv-panel" class="adv-panel">
               <div class="field-col" style="padding:10px;border-radius:10px;border:1px solid var(--border);background:rgba(255,255,255,0.03)">
                 <div class="field-row" style="justify-content:space-between;margin-bottom:4px">
                   <strong style="font-size:12.5px">📏 กฎ Short Sell (SET Uptick Policy)</strong>
                   <span class="pill ${activeRuleTicks === 0 ? "pill-green" : "pill-amber"}">เปิด +${activeOpenTicks} · ต่อ +${activeRuleTicks}</span>
                 </div>
                 <label class="field-label" for="rule-open-uptick-select">ตอนเปิด Short ใหม่ (Suggest / Quick)</label>
                 <select id="rule-open-uptick-select" class="select-input">
                   <option value="1" ${activeOpenTicks === 1 ? "selected" : ""}>+1 Strict Uptick (Last + 1 Tick / Offer 2)</option>
                   <option value="0" ${activeOpenTicks === 0 ? "selected" : ""}>+0 Zero-Plus (≥ Last)</option>
                 </select>
                 <label class="field-label" for="rule-uptick-select" style="margin-top:6px">ตอนต่อราคา Short (Orders / Auto-Nego)</label>
                 <select id="rule-uptick-select" class="select-input">
                   <option value="0" ${activeRuleTicks === 0 ? "selected" : ""}>+0 Zero-Plus (ใช้ Last · Match เมื่อ Bid1=Last)</option>
                   <option value="1" ${activeRuleTicks === 1 ? "selected" : ""}>+1 Strict Uptick (บังคับ &gt; Last ทุกตัว)</option>
                 </select>
                 <label class="toggle-row" for="rule-auto-minus10" style="margin-top:6px">
                   <input id="rule-auto-minus10" type="checkbox" ${autoMinus10 ? "checked" : ""}>
                   <span>ตรวจหุ้นปิดร่วง ≥ 10% เมื่อวานอัตโนมัติ (ปรับ Nego เป็น +1)</span>
                 </label>
                 <div id="rule-auto-strict-list" class="field-row" style="gap:5px;flex-wrap:wrap;margin-top:4px">${autoBadgesHtml}</div>
                 <button id="rule-save-btn" class="btn btn-primary btn-sm" type="button" style="margin-top:8px">💾 บันทึกกฎ Short Sell</button>
               </div>
               <div class="meta-row" style="margin:0">
                 <span>OTP Relay: ${relayPill}</span>
                 <span>Build: <span class="pill pill-muted">v${esc(ctx.assetVersion || "dev")}</span></span>
               </div>
               <div class="inbox-quota-box">
                 <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
                   <span><strong>📊 LINE Quota:</strong> ${esc(qLabel)} (${eod.line_push_enabled ? esc(quota.push_mode || "eod_only") : "ปิด EOD LINE"})</span>
                   <span class="pill ${quota.exhausted ? "pill-red" : "pill-sky"}">${quota.exhausted ? "429 เต็ม" : eod.line_push_enabled ? "พร้อมส่ง EOD" : "0 Quota (Inbox Only)"}</span>
                 </div>
                 <div class="quota-bar-track"><div class="quota-bar-fill ${qFillCls}" style="width:${Math.min(100, Math.max(0, qPct))}%"></div></div>
                 <div class="muted" style="font-size:11px">EOD ${esc(eodTime)}: ${eodLine}</div>
                 <div class="muted" style="font-size:11px">🗓️ ${esc(formatQuotaResetLabel(quota))}</div>
               </div>
               <div class="btn-row">
                 <button class="btn btn-ghost" data-action="eod-preview" type="button">📊 Preview EOD วันนี้</button>
                 <button class="btn btn-amber" data-action="eod-send-now" type="button">📊 รัน EOD + บันทึก Net Eq</button>
               </div>
               ${
                 showOtpBox
                   ? ""
                   : `<button class="btn btn-ghost" data-action="show-otp" type="button">🔐 เปิดช่องกรอก OTP 6 หลัก</button>`
               }
               <button class="btn btn-ghost" data-action="hard-reload" type="button">🔄 โหลดหน้าเว็บใหม่ (Clear WebView)</button>
             </div>`
          : ""
      }
    </section>
  `;

  container.querySelectorAll("[data-broker-adv]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const slug = btn.dataset.brokerAdv;
      _uiState.brokerAdv[slug] = !_uiState.brokerAdv[slug];
      renderBrokersView(container, data, ctx);
    });
  });

  const sysAdvBtn = container.querySelector("#brokers-sys-adv-toggle");
  if (sysAdvBtn) {
    sysAdvBtn.addEventListener("click", () => {
      _uiState.sysAdvOpen = !_uiState.sysAdvOpen;
      renderBrokersView(container, data, ctx);
    });
  }

  const otpHost = container.querySelector("#brokers-otp-host");
  if (otpHost && effectivePending) {
    renderOtpWidget(otpHost, {
      pending: effectivePending,
      compact: false,
      error: ctx.otpError || "",
      prefix: "card-otp",
      onSubmit: (otp, broker) =>
        ctx.submitOtp
          ? ctx.submitOtp(otp, broker)
          : miniappApi.submitOtp(otp, broker).then(() => ctx.refreshStatus()),
    });
  }

  const connectAllBtn = container.querySelector("#connect-all-btn");
  if (connectAllBtn && ctx.startConnectAllQueue) {
    connectAllBtn.addEventListener("click", () => ctx.startConnectAllQueue());
  }
  const cancelAllBtn = container.querySelector("#cancel-connect-all-btn");
  if (cancelAllBtn && ctx.cancelConnectAllQueue) {
    cancelAllBtn.addEventListener("click", () => ctx.cancelConnectAllQueue());
  }

  const kssSel = container.querySelector("#brokers-kss-acct");
  if (kssSel) {
    kssSel.addEventListener("change", () => {
      _uiState.kssSelectedAcct = kssSel.value;
    });
  }
  const kssSwBtn = container.querySelector("#brokers-kss-switch-btn");
  if (kssSwBtn) {
    kssSwBtn.addEventListener("click", async () => {
      const acct = container.querySelector("#brokers-kss-acct")?.value;
      if (!acct) return;
      const orig = kssSwBtn.innerHTML;
      kssSwBtn.disabled = true;
      kssSwBtn.textContent = "⏳ สลับ...";
      try {
        const res = await miniappApi.switchKssAccount(acct);
        const kssInfo = res?.brokers?.kss || {};
        if (kssInfo.error || (kssInfo.account_no && String(kssInfo.account_no) !== String(acct))) {
          throw new Error(kssInfo.error || `สลับเป็นบัญชี KSS ${acct} ไม่สำเร็จ (อ่านได้ ${kssInfo.account_no || "—"})`);
        }
        _uiState.kssSelectedAcct = acct;
        markPortfolioStale();
        try {
          await ctx.refreshStatus();
        } catch (_ignored) {}
        const msg = `สลับ KSS เป็นบัญชี ${acct} แล้ว`;
        ctx.toast(`✅ ${msg}`, "ok");
        if (ctx.setLastResult) ctx.setLastResult({ kind: "ok", title: "✅ สลับบัญชี KSS สำเร็จ", detail: msg });
      } catch (err) {
        ctx.toast(`❌ ${err.message}`, "err");
        if (ctx.setLastResult) ctx.setLastResult({ kind: "err", title: "❌ สลับบัญชี KSS ไม่สำเร็จ", detail: err.message });
      } finally {
        kssSwBtn.disabled = false;
        kssSwBtn.innerHTML = orig;
      }
    });
  }

  const rShortSel = container.querySelector("#routing-short-select");
  if (rShortSel) {
    rShortSel.addEventListener("change", () => {
      _uiState.routingShort = rShortSel.value;
    });
  }
  const rLongSel = container.querySelector("#routing-long-select");
  if (rLongSel) {
    rLongSel.addEventListener("change", () => {
      _uiState.routingLong = rLongSel.value;
    });
  }
  const rSaveBtn = container.querySelector("#routing-save-btn");
  if (rSaveBtn) {
    rSaveBtn.addEventListener("click", async () => {
      const [sb, sa] = (container.querySelector("#routing-short-select")?.value || "kss:margin").split(":");
      const [lb, la] = (container.querySelector("#routing-long-select")?.value || "lib:cash").split(":");
      const orig = rSaveBtn.innerHTML;
      rSaveBtn.disabled = true;
      rSaveBtn.textContent = "⏳ กำลังบันทึก...";
      try {
        await miniappApi.saveAccountRouting({
          short_broker: sb,
          short_account: sa,
          long_broker: lb,
          long_account: la,
        });
        _uiState.routingShort = null;
        _uiState.routingLong = null;
        await ctx.refreshStatus();
        const detail = `Short: ${sb.toUpperCase()} ${sa} · Long: ${lb.toUpperCase()} ${la}`;
        ctx.toast(`✅ บันทึกบัญชีหลักแล้ว (${detail})`, "ok");
        if (ctx.setLastResult) ctx.setLastResult({ kind: "ok", title: "✅ บันทึกบัญชีหลักแล้ว", detail });
      } catch (err) {
        ctx.toast(`❌ ${err.message}`, "err");
        if (ctx.setLastResult) ctx.setLastResult({ kind: "err", title: "❌ บันทึกบัญชีหลักไม่สำเร็จ", detail: err.message });
      } finally {
        rSaveBtn.disabled = false;
        rSaveBtn.innerHTML = orig;
      }
    });
  }

  const ruleOpenSel = container.querySelector("#rule-open-uptick-select");
  if (ruleOpenSel) {
    ruleOpenSel.addEventListener("change", () => {
      _uiState.ruleOpenTicks = Number(ruleOpenSel.value) === 1 ? 1 : 0;
    });
  }
  const ruleSel = container.querySelector("#rule-uptick-select");
  if (ruleSel) {
    ruleSel.addEventListener("change", () => {
      _uiState.ruleTicks = Number(ruleSel.value) === 1 ? 1 : 0;
    });
  }
  const ruleMinus10El = container.querySelector("#rule-auto-minus10");
  if (ruleMinus10El) {
    ruleMinus10El.addEventListener("change", () => {
      _uiState.ruleAutoMinus10 = Boolean(ruleMinus10El.checked);
    });
  }
  const ruleSaveBtn = container.querySelector("#rule-save-btn");
  if (ruleSaveBtn) {
    ruleSaveBtn.addEventListener("click", async () => {
      const openTicks = Number(container.querySelector("#rule-open-uptick-select")?.value) === 1 ? 1 : 0;
      const ticks = Number(container.querySelector("#rule-uptick-select")?.value) === 1 ? 1 : 0;
      const autoMinus = Boolean(container.querySelector("#rule-auto-minus10")?.checked ?? true);
      const orig = ruleSaveBtn.innerHTML;
      ruleSaveBtn.disabled = true;
      ruleSaveBtn.textContent = "⏳ กำลังบันทึก...";
      try {
        const res = await miniappApi.saveTradingRules({
          short_open_uptick_ticks: openTicks,
          short_uptick_ticks: ticks,
          auto_minus10_strict: autoMinus,
        });
        _uiState.ruleOpenTicks = null;
        _uiState.ruleTicks = null;
        _uiState.ruleAutoMinus10 = null;
        await ctx.refreshStatus();
        const saved = (res && res.trading_rules) || {};
        const autoCnt = Array.isArray(saved.auto_strict_symbols) ? saved.auto_strict_symbols.length : 0;
        const detail = `เปิด +${openTicks} · ต่อ +${ticks}${autoMinus ? ` · Auto -10% (${autoCnt} ตัว)` : ""}`;
        ctx.toast(`✅ บันทึกกฎ Short Sell แล้ว (${detail})`, "ok");
        if (ctx.setLastResult) ctx.setLastResult({ kind: "ok", title: "✅ บันทึกกฎ Short Sell แล้ว", detail });
      } catch (err) {
        ctx.toast(`❌ ${err.message}`, "err");
        if (ctx.setLastResult) ctx.setLastResult({ kind: "err", title: "❌ บันทึกกฎ Short Sell ไม่สำเร็จ", detail: err.message });
      } finally {
        ruleSaveBtn.disabled = false;
        ruleSaveBtn.innerHTML = orig;
      }
    });
  }

  container.querySelectorAll("[data-action]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const act = btn.dataset.action;
      const broker = btn.dataset.broker;
      if (act === "refresh") {
        await ctx.refreshStatus(true);
        return;
      }
      if (act === "hard-reload") {
        const url = new URL(window.location.href);
        url.searchParams.set("tab", "brokers");
        url.searchParams.set("_r", String(Date.now()));
        window.location.replace(url.toString());
        return;
      }
      if (act === "show-otp") {
        ctx.params.focus = "otp";
        renderBrokersView(container, data, ctx);
        return;
      }
      if (act === "login") {
        await runBrokerAction(btn, () => miniappApi.loginBroker(broker, false), ctx, `เริ่ม Login ${broker.toUpperCase()}...`);
        return;
      }
      if (act === "force-login") {
        ctx.confirmSheet({
          title: `ยืนยัน Force Login (${broker.toUpperCase()})`,
          body: "จะปิด Chrome CDP เดิมแล้วเปิดใหม่พร้อมเข้าระบบ (อาจส่ง SMS OTP ตาม Invariant 9) ยืนยันหรือไม่?",
          confirmLabel: "⚡ Force Login",
          danger: true,
          busyTitle: `⏳ กำลัง Force Login ${broker.toUpperCase()}...`,
          onConfirm: () =>
            runBrokerAction(btn, () => miniappApi.loginBroker(broker, true), ctx, `Force Login ${broker.toUpperCase()}`, { modalMode: true }),
        });
        return;
      }
      if (act === "stop") {
        ctx.confirmSheet({
          title: `ปิดพัก Chrome CDP (${broker.toUpperCase()})`,
          body: "ปิดบราวเซอร์เพื่อคืน RAM โดย Gateway :8000 ยังคงออนไลน์ 24 ชม.",
          confirmLabel: "🛑 ปิดพัก Chrome",
          busyTitle: `⏳ กำลังปิดพัก ${broker.toUpperCase()}...`,
          onConfirm: () =>
            runBrokerAction(btn, () => miniappApi.stopBroker(broker), ctx, `ปิดพัก ${broker.toUpperCase()} แล้ว`, { modalMode: true }),
        });
        return;
      }
      if (act === "ks-activate") {
        ctx.confirmSheet({
          title: "🚨 ยืนยันเปิด Kill-Switch (HALT)",
          body: "ระงับคำสั่งซื้อขายทั้งหมดทันที และพยายามยกเลิกออเดอร์ที่เปิดค้างอยู่",
          confirmLabel: "⛔ HALT NOW",
          danger: true,
          busyTitle: "⏳ กำลังเปิด Kill-Switch...",
          onConfirm: () =>
            runBrokerAction(btn, () => miniappApi.toggleKillSwitch("activate"), ctx, "เปิด Kill-Switch แล้ว", { modalMode: true }),
        });
        return;
      }
      if (act === "eod-preview") {
        const orig = btn.innerHTML;
        btn.disabled = true;
        btn.textContent = "⏳ กำลังอ่าน CDP...";
        try {
          const pv = await miniappApi.getEodPreview();
          const c = pv.counts || {};
          const kindTxt =
            pv.status_kind === "session_unavailable"
              ? "เชื่อมต่อ CDP ไม่สำเร็จ"
              : pv.status_kind === "matched"
                ? `Matched ${c.matched || 0} รายการ`
                : pv.status_kind === "open_only"
                  ? `ไม่มี Matched · ค้าง ${c.open || 0}`
                  : "ไม่มีคำสั่งวันนี้ (0/0)";
          ctx.toast(`📊 EOD Preview (${pv.date}): ${kindTxt}`, pv.status_kind === "session_unavailable" ? "err" : "ok");
          if (ctx.setLastResult) {
            ctx.setLastResult({
              kind: pv.status_kind === "session_unavailable" ? "warn" : "ok",
              title: `📊 EOD Preview ${pv.date}: ${kindTxt}`,
              detail: `Matched ${c.matched || 0} · Open ${c.open || 0} (Preview ฟรีไม่เสียโควต้า LINE)`,
              ctaLabel: "📋 ดูออเดอร์ทั้งหมด",
              onCta: () => ctx.navigate("orders", { status_filter: c.matched ? "matched" : "all" }),
            });
          }
        } catch (err) {
          ctx.toast(`❌ ${err.message}`, "err");
        } finally {
          btn.disabled = false;
          btn.innerHTML = orig;
        }
        return;
      }
      if (act === "eod-send-now") {
        ctx.confirmSheet({
          title: "📊 รันสรุปปิดตลาด EOD & บันทึก Net Equity",
          body: "จะดึงยอด Matched / ค้าง + Net Equity ณ ปัจจุบัน บันทึกลง MiniApp Inbox และไฟล์ net_equity_history.csv (ไม่ส่งเข้า LINE OA)",
          confirmLabel: "📊 ยืนยันบันทึก EOD",
          busyTitle: "⏳ กำลังรวบรวมข้อมูล CDP และบันทึก...",
          onConfirm: async () => {
            const res = await miniappApi.sendEodNow(true);
            try {
              await ctx.refreshStatus();
            } catch (_ignored) {}
            const sent = Boolean(res && res.sent);
            const c = res?.counts || {};
            const netEq = res?.net_equity != null ? ` · Net Eq ฿${fmtInt(res.net_equity)}` : "";
            return {
              ok: res?.status_kind !== "error",
              kind: "ok",
              title: sent ? "✅ บันทึก EOD + ส่ง LINE OA แล้ว" : "✅ บันทึก EOD + Net Equity แล้ว",
              detail: `วันที่ ${res?.date || ""} · Matched ${c.matched || 0} · Open ${c.open || 0}${netEq}`,
              closeLabel: "ปิด",
            };
          },
        });
        return;
      }
      if (act === "ks-reset") {
        ctx.confirmSheet({
          title: "✅ ปลดล็อก Kill-Switch",
          body: "คืนสถานะระบบให้สามารถส่งคำสั่งซื้อขายได้ตามปกติ",
          confirmLabel: "✅ RESET",
          busyTitle: "⏳ กำลังปลดล็อก Kill-Switch...",
          onConfirm: () =>
            runBrokerAction(btn, () => miniappApi.toggleKillSwitch("reset"), ctx, "ปลดล็อก Kill-Switch แล้ว", { modalMode: true }),
        });
      }
    });
  });
}

async function runBrokerAction(btn, fn, ctx, successTitle, { modalMode = false } = {}) {
  const orig = btn.innerHTML;
  btn.disabled = true;
  if (!modalMode) btn.textContent = "⏳ กำลังส่ง...";
  try {
    const res = await fn();
    const state = res?.result?.state || (res?.kill_switch?.is_active !== undefined ? (res.kill_switch.is_active ? "HALTED" : "NORMAL") : "");
    const statusVal = String(res?.result?.status || "").toLowerCase();
    const lpErr = res?.result?.error || res?.result?.login_progress?.error;
    const failed =
      res?.ok === false ||
      statusVal === "crashed" ||
      statusVal === "timeout" ||
      String(state).toUpperCase() === "DAEMON_DOWN" ||
      res?.result?.login_progress?.phase === "error";
    if (failed) {
      throw new Error(lpErr || res?.detail || `ไม่สามารถเปิดระบบได้ (สถานะ: ${state || statusVal || "FAILED"})`);
    }
    const detail = state ? `สถานะ: ${state}` : (res?.message || "");
    try {
      await ctx.refreshStatus();
    } catch (_ignored) {}
    if (modalMode) {
      return {
        ok: true,
        title: `✅ ${successTitle}`,
        detail: detail || successTitle,
        details: state ? [{ label: "สถานะล่าสุด", value: String(state) }] : [],
        closeLabel: "ปิด",
      };
    }
    const msg = `✅ ${successTitle}${state ? ` (${state})` : ""}`;
    ctx.toast(msg, "ok");
    if (ctx.setLastResult) ctx.setLastResult({ kind: "ok", title: `✅ ${successTitle}`, detail });
    return undefined;
  } catch (err) {
    if (modalMode) throw err;
    ctx.toast(`❌ ${err.message}`, "err");
    if (ctx.setLastResult) ctx.setLastResult({ kind: "err", title: "❌ ดำเนินการไม่สำเร็จ", detail: err.message });
    return undefined;
  } finally {
    btn.disabled = false;
    btn.innerHTML = orig;
  }
}
