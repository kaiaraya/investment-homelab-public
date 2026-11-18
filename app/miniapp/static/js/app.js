import { getIdToken, initAuth, loginWithLiff, refreshAuthOnUnauthorized } from "./auth.js";
import { miniappApi } from "./api.js";
import { esc, formatQuotaResetLabel } from "./utils.js";
import { renderOtpWidget } from "./otp_widget.js";
import { brokersRenderSignature, renderBrokersView, syncBrokersOtpSlot } from "./views/brokers.js";
import { renderPortfolioView } from "./views/portfolio.js";
import { renderOrdersView } from "./views/orders.js";
import { renderNegoView } from "./views/nego.js";
import { renderTradeView } from "./views/trade.js";

const VALID_TABS = new Set(["portfolio", "orders", "nego", "trade", "brokers"]);

const state = {
  tab: "brokers",
  params: {},
  me: null,
  statusData: null,
  pollTimer: null,
  busyCount: 0,
  renderSeq: 0,
  lastHiddenAt: 0,
  lastResult: null,
  inboxItems: [],
  unreadCount: 0,
  inboxOpen: false,
  inboxFilter: "all",
  quota: null,
  eodStatus: null,
  stickyOtpKey: null,
  otpError: "",
  otpCountdownTimer: null,
  queueAll: { active: false, step: null, error: null, libTriggered: false },
  sseAbort: null,
  sseRetryTimer: null,
  brokersFormActive: false,
  brokersPendingRerender: false,
  brokersFormIdleTimer: null,
  lastBrokersSig: null,
};

function parseDeepLinkParams() {
  const url = new URL(window.location.href);
  const out = {};
  for (const [k, v] of url.searchParams.entries()) {
    out[k] = v;
  }
  if (out["liff.state"]) {
    try {
      const nested = new URLSearchParams(out["liff.state"].replace(/^\?/, ""));
      for (const [k, v] of nested.entries()) {
        if (!out[k]) out[k] = v;
      }
    } catch (_err) {
      // ignore malformed liff.state
    }
  }
  return out;
}

function getAssetVersion() {
  const meta = document.querySelector('meta[name="miniapp-version"]');
  return (meta && meta.content && meta.content.trim()) || "dev";
}

function updateFooterVersion() {
  const el = document.getElementById("app-version-label");
  if (!el) return;
  const modeLabel = (state.statusData && state.statusData.mode && state.statusData.mode.label)
    || (state.me && state.me.mode && state.me.mode.label)
    || "READY";
  el.textContent = `MiniApp v${getAssetVersion()} · ${modeLabel}`;
}

function clearToast() {
  const root = document.getElementById("toast-root");
  if (root) root.innerHTML = "";
}

function toast(msg, kind = "ok") {
  const root = document.getElementById("toast-root");
  if (!root) return;
  const el = document.createElement("div");
  el.className = `toast ${kind === "err" ? "toast-err" : "toast-ok"}`;
  el.textContent = msg;
  root.appendChild(el);
  setTimeout(() => el.remove(), 6000);
}

function setAppBusy(delta) {
  state.busyCount = Math.max(0, state.busyCount + delta);
  const busy = state.busyCount > 0;
  const shell = document.getElementById("app") || document.querySelector(".app-shell");
  if (shell) shell.classList.toggle("app-busy", busy);
  document.querySelectorAll(".tab-btn, [data-jump-tab], #top-kill-btn").forEach((el) => {
    el.disabled = busy;
  });
}

function isAppBusy() {
  return state.busyCount > 0;
}

function formatNowTime() {
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

function clearLastResult() {
  state.lastResult = null;
  renderLastResultBanner();
}

function setLastResult(res) {
  if (!res) {
    clearLastResult();
    return;
  }
  state.lastResult = {
    kind: res.kind || "ok",
    title: String(res.title || "ดำเนินการแล้ว"),
    detail: String(res.detail || ""),
    ts: res.ts || formatNowTime(),
    ctaLabel: res.ctaLabel || "",
    onCta: typeof res.onCta === "function" ? res.onCta : null,
  };
  renderLastResultBanner();
}

function renderLastResultBanner() {
  const banner = document.getElementById("last-result-banner");
  if (!banner) return;
  const r = state.lastResult;
  if (!r) {
    banner.className = "result-banner hidden";
    banner.innerHTML = "";
    return;
  }
  const kindCls =
    r.kind === "err"
      ? "result-banner-err"
      : r.kind === "warn"
        ? "result-banner-warn"
        : r.kind === "dry"
          ? "result-banner-dry"
          : "result-banner-ok";
  banner.className = `result-banner ${kindCls}`;
  banner.innerHTML = `
    <div class="result-banner-head">
      <div class="result-banner-title">${esc(r.title)}${r.ts ? ` <span class="muted" style="font-weight:500;font-size:11px">(${esc(r.ts)})</span>` : ""}</div>
      <button id="result-banner-close" class="result-banner-close" type="button" aria-label="ปิดผลลัพธ์">×</button>
    </div>
    ${r.detail ? `<div class="result-banner-detail">${esc(r.detail)}</div>` : ""}
    ${
      r.ctaLabel && r.onCta
        ? `<div class="result-banner-actions"><button id="result-banner-cta" class="result-banner-cta" type="button">${esc(r.ctaLabel)}</button></div>`
        : ""
    }
  `;
  const closeBtn = banner.querySelector("#result-banner-close");
  if (closeBtn) closeBtn.onclick = clearLastResult;
  const ctaBtn = banner.querySelector("#result-banner-cta");
  if (ctaBtn && r.onCta) {
    ctaBtn.onclick = () => {
      if (isAppBusy()) return;
      r.onCta();
    };
  }
}

function confirmSheet({
  title,
  body = "",
  renderBody = null,
  confirmLabel = "ยืนยัน",
  danger = false,
  busyTitle = "⏳ กำลังส่งคำสั่งกับโบรกเกอร์...",
  busyNote = "กรุณารอสักครู่ ระบบกำลังดำเนินการ (ห้ามปิดหรือกดซ้ำ)",
  onConfirm,
}) {
  if (isAppBusy()) {
    toast("⏳ กำลังดำเนินการคำสั่งก่อนหน้า กรุณารอสักครู่...", "err");
    return;
  }
  const backdrop = document.getElementById("sheet-backdrop");
  const titleEl = document.getElementById("sheet-title");
  const bodyEl = document.getElementById("sheet-body");
  const actionsEl = document.getElementById("sheet-actions");
  const ctasEl = document.getElementById("sheet-result-ctas");
  const cancelBtn = document.getElementById("sheet-cancel");
  const confirmBtn = document.getElementById("sheet-confirm");

  let _modalBusy = false;
  let _inResultMode = false;

  titleEl.textContent = title;
  cancelBtn.disabled = false;
  cancelBtn.textContent = "ยกเลิก";
  confirmBtn.disabled = false;
  confirmBtn.textContent = confirmLabel;
  confirmBtn.className = `btn ${danger ? "btn-danger" : "btn-primary"}`;
  if (actionsEl) actionsEl.classList.remove("hidden");
  if (ctasEl) {
    ctasEl.classList.add("hidden");
    ctasEl.innerHTML = "";
    ctasEl.style.gridTemplateColumns = "";
  }

  if (typeof renderBody === "function") {
    bodyEl.innerHTML = "";
    renderBody(bodyEl, {
      setConfirmEnabled: (enabled) => {
        if (!_modalBusy && !_inResultMode) confirmBtn.disabled = !enabled;
      },
      setConfirmLabel: (label) => {
        if (!_modalBusy && !_inResultMode) confirmBtn.textContent = label;
      },
    });
  } else {
    bodyEl.textContent = body;
  }
  backdrop.classList.remove("hidden");

  const close = () => {
    if (_modalBusy) return;
    backdrop.classList.add("hidden");
    document.removeEventListener("keydown", onKeyDown);
  };
  const onKeyDown = (e) => {
    if (e.key === "Escape" && !_modalBusy) close();
  };
  document.addEventListener("keydown", onKeyDown);

  cancelBtn.onclick = close;
  backdrop.onclick = (e) => {
    if (e.target === backdrop && !_modalBusy) close();
  };

  confirmBtn.onclick = async () => {
    if (confirmBtn.disabled || _modalBusy || _inResultMode) return;
    _modalBusy = true;
    setAppBusy(1);

    titleEl.textContent = busyTitle;
    cancelBtn.disabled = true;
    confirmBtn.disabled = true;
    confirmBtn.textContent = "⏳ กำลังส่ง...";
    bodyEl.innerHTML = `
      <div class="sheet-busy-box">
        <span class="spinner-inline" aria-hidden="true"></span>
        <div>
          <div style="font-weight:700">${esc(busyTitle.replace(/^⏳\s*/, ""))}</div>
          <div class="muted" style="font-size:12px;margin-top:2px">${esc(busyNote)}</div>
        </div>
      </div>
    `;

    let resultObj = null;
    let execErr = null;
    try {
      resultObj = onConfirm ? await onConfirm() : { ok: true };
    } catch (err) {
      execErr = err;
    } finally {
      _modalBusy = false;
      _inResultMode = true;
      setAppBusy(-1);
    }

    let kind = "ok";
    let resTitle = "✅ ดำเนินการสำเร็จ";
    let resDetail = "";
    let kvDetails = [];
    let ctas = [];
    let closeLabel = "ปิด";
    let primaryBannerCta = null;

    if (execErr) {
      kind = "err";
      resTitle = "❌ คำสั่งไม่สำเร็จ";
      resDetail = (execErr && execErr.message) || String(execErr) || "เกิดข้อผิดพลาดในการดำเนินการ";
    } else if (resultObj && typeof resultObj === "object") {
      if (resultObj.ok === false) {
        kind = resultObj.kind || "err";
        resTitle = resultObj.title || "❌ ไม่สำเร็จ";
      } else {
        kind = resultObj.kind || (resultObj.dryRun ? "dry" : "ok");
        resTitle = resultObj.title || (kind === "dry" ? "🧪 สำเร็จ [DRY-RUN]" : "✅ ดำเนินการสำเร็จ");
      }
      resDetail = String(resultObj.detail || resultObj.message || "");
      kvDetails = Array.isArray(resultObj.details) ? resultObj.details : [];
      ctas = Array.isArray(resultObj.ctas) ? resultObj.ctas : [];
      closeLabel = resultObj.closeLabel || "ปิด";
      if (ctas.length > 0 && typeof ctas[0].onClick === "function") {
        primaryBannerCta = { label: ctas[0].label, onClick: ctas[0].onClick };
      }
    }

    titleEl.textContent = resTitle;
    const statusCls =
      kind === "err"
        ? "sheet-status-err"
        : kind === "warn"
          ? "sheet-status-warn"
          : kind === "dry"
            ? "sheet-status-dry"
            : "sheet-status-ok";
    const kvHtml = kvDetails
      .filter((row) => row && row.label)
      .map(
        (row) =>
          `<div class="sheet-kv-row"><span>${esc(row.label)}</span><strong>${esc(row.value ?? "—")}</strong></div>`
      )
      .join("");

    bodyEl.innerHTML = `
      <div class="sheet-status-box ${statusCls}">
        ${resDetail ? `<div style="font-size:13.5px;font-weight:600;color:#f8fafc">${esc(resDetail)}</div>` : ""}
        ${kvHtml ? `<div class="sheet-kv-list">${kvHtml}</div>` : ""}
      </div>
    `;

    if (actionsEl) actionsEl.classList.add("hidden");
    if (ctasEl) {
      const buttonsSpec = [
        ...ctas.map((c, idx) => ({
          id: `sheet-res-cta-${idx}`,
          label: c.label || "ดำเนินการต่อ",
          cls: c.primary ? "btn btn-primary" : "btn btn-ghost",
          onClick: () => {
            close();
            if (typeof c.onClick === "function") c.onClick();
          },
        })),
        {
          id: "sheet-res-close",
          label: closeLabel,
          cls: ctas.length ? "btn btn-ghost" : "btn btn-primary",
          onClick: close,
        },
      ];
      ctasEl.style.gridTemplateColumns = buttonsSpec.length === 1 ? "1fr" : "1fr 1fr";
      ctasEl.innerHTML = buttonsSpec
        .map((b) => `<button id="${esc(b.id)}" class="${b.cls}" type="button">${esc(b.label)}</button>`)
        .join("");
      buttonsSpec.forEach((b) => {
        const el = ctasEl.querySelector(`#${b.id}`);
        if (el) el.onclick = b.onClick;
      });
      ctasEl.classList.remove("hidden");
    } else {
      cancelBtn.disabled = false;
      cancelBtn.textContent = closeLabel;
      cancelBtn.onclick = close;
    }

    setLastResult({
      kind,
      title: resTitle,
      detail: resDetail,
      ctaLabel: primaryBannerCta ? primaryBannerCta.label : "",
      onCta: primaryBannerCta ? primaryBannerCta.onClick : null,
    });
    toast(resDetail ? `${resTitle}: ${resDetail}` : resTitle, kind === "err" ? "err" : "ok");
  };
}

function updateTopBar(statusData, meData) {
  const mode = (statusData && statusData.mode) || (meData && meData.mode);
  const ks = (statusData && statusData.kill_switch) || (meData && meData.kill_switch);
  const modeEl = document.getElementById("mode-badge");
  if (modeEl && mode) {
    modeEl.textContent = mode.label;
    modeEl.className = `pill ${mode.is_live ? "pill-red" : "pill-amber"}`;
  }

  const killBtn = document.getElementById("top-kill-btn");
  if (killBtn && ks) {
    killBtn.className = `icon-btn ${ks.is_active ? "danger-active" : ""}`;
    killBtn.textContent = ks.is_active ? "🚨" : "🛡️";
  }

  const brokers = (statusData && statusData.brokers) || {};
  const kssPill = document.getElementById("top-kss-pill");
  const libPill = document.getElementById("top-lib-pill");
  if (kssPill && brokers.kss) {
    const ok = brokers.kss.authenticated;
    kssPill.textContent = ok ? "🟢 KSS" : "🔴 KSS";
    kssPill.className = `pill-btn ${ok ? "pill-green" : "pill-red"}`;
  }
  if (libPill && brokers.lib) {
    const ok = brokers.lib.authenticated;
    libPill.textContent = ok ? "🟢 LIB" : "🔴 LIB";
    libPill.className = `pill-btn ${ok ? "pill-green" : "pill-red"}`;
  }

  if (statusData && statusData.line_quota) {
    state.quota = statusData.line_quota;
  }
  if (statusData && statusData.eod_status) {
    state.eodStatus = statusData.eod_status;
  }
  if (statusData && statusData.inbox_unread !== undefined) {
    state.unreadCount = Number(statusData.inbox_unread || 0);
  }
  renderQuotaPill();
  renderInboxBadge();
  renderStickyOtpBanner();
  updateFooterVersion();
}

function renderQuotaPill() {
  const el = document.getElementById("top-quota-pill");
  if (!el) return;
  const q = state.quota || {};
  const limit = q.limit ?? 300;
  const used = q.used;
  const label = used !== null && used !== undefined ? `${used}/${limit}` : `?/${limit}`;
  const exhausted = Boolean(q.exhausted);
  const pct = Number(q.pct || 0);
  el.textContent = exhausted ? `FULL ${label}` : `EOD ${label}`;
  el.className = `pill-btn quota-pill ${exhausted ? "pill-red" : pct >= 80 ? "pill-amber" : "pill-sky"}`;
}

function renderInboxBadge() {
  const badge = document.getElementById("top-inbox-badge");
  if (!badge) return;
  const n = Math.max(0, Number(state.unreadCount || 0));
  if (n <= 0) {
    badge.className = "inbox-badge hidden";
    badge.textContent = "0";
  } else {
    badge.className = "inbox-badge";
    badge.textContent = n > 99 ? "99+" : String(n);
  }
}

function pendingBrokerSlug(pending) {
  const brk = String((pending && pending.broker) || "").toUpperCase();
  if (brk.includes("LIB")) return "lib";
  if (brk.includes("KRUNG") || brk.includes("KSS")) return "kss";
  return null;
}

function pruneAuthenticatedPendingOtp(statusData) {
  if (!statusData || !statusData.pending_otp) return statusData;
  const pending = statusData.pending_otp;
  const slug = pendingBrokerSlug(pending);
  if (!slug) return statusData;
  const lpPhase = statusData.login_progress?.[slug]?.phase;
  const brkAuth = statusData.brokers?.[slug]?.authenticated;
  if (lpPhase === "authenticated" && (pending.submitted || brkAuth)) {
    return { ...statusData, pending_otp: null };
  }
  return statusData;
}

function normalizePendingOtp(raw) {
  if (!raw) return null;
  const cur = state.statusData && state.statusData.pending_otp;
  const samePending =
    cur && cur.broker === raw.broker && (cur.ref || null) === (raw.ref || null);
  const ttlSec = raw.expires_in_seconds;
  let deadlineMs = samePending && typeof cur._deadlineMs === "number" ? cur._deadlineMs : null;
  if (typeof ttlSec === "number" && Number.isFinite(ttlSec)) {
    const serverDeadline = Date.now() + Math.max(0, ttlSec) * 1000;
    if (!deadlineMs || Math.abs(serverDeadline - deadlineMs) > 4000) {
      deadlineMs = serverDeadline;
    }
  }
  return { ...raw, _deadlineMs: deadlineMs };
}

function isTypingOtp() {
  const el = document.activeElement;
  return Boolean(el && el.classList && el.classList.contains("otp-digit"));
}

function isBrokersFormControl(el) {
  if (!el || el.classList?.contains("otp-digit")) return false;
  const tag = String(el.tagName || "").toUpperCase();
  return tag === "SELECT" || tag === "INPUT" || tag === "TEXTAREA";
}

function isBrokersFormBusy() {
  if (state.tab !== "brokers") return false;
  if (state.brokersFormActive) return true;
  const main = document.getElementById("main-content");
  const el = document.activeElement;
  return Boolean(main && el && main.contains(el) && isBrokersFormControl(el));
}

function buildRenderCtx() {
  const mode = (state.statusData && state.statusData.mode) || (state.me && state.me.mode);
  const mySeq = state.renderSeq;
  const targetTab = state.tab;
  return {
    params: state.params,
    assetVersion: getAssetVersion(),
    dryRun: mode ? !mode.is_live : true,
    toast,
    confirmSheet,
    setLastResult,
    clearLastResult,
    isAppBusy,
    isCurrent: () => mySeq === state.renderSeq && state.tab === targetTab,
    syncNav: () => syncBottomNavActive(state.tab),
    refreshStatus,
    navigate,
    statusData: state.statusData,
    submitOtp: submitOtpFlow,
    otpError: state.otpError,
    queueAll: state.queueAll,
    startConnectAllQueue,
    cancelConnectAllQueue,
  };
}

function syncBrokersTabSafe({ force = false, allowWhileTypingOtp = false } = {}) {
  if (state.tab !== "brokers") return;
  const main = document.getElementById("main-content");
  if (!main) return;
  const ctx = buildRenderCtx();
  const otpCleared = !(state.statusData && state.statusData.pending_otp);

  // Always keep #brokers-otp-card synced in-place so OTP never gets stuck even when a select menu is open.
  if (!isTypingOtp() || allowWhileTypingOtp || otpCleared) {
    syncBrokersOtpSlot(main, state.statusData, ctx);
  }

  if (isTypingOtp() && !allowWhileTypingOtp && !otpCleared) return;
  if (isBrokersFormBusy()) {
    state.brokersPendingRerender = true;
    return;
  }

  const nextSig = brokersRenderSignature(state.statusData, ctx);
  if (!force && state.lastBrokersSig === nextSig && main.querySelector("[data-broker-card]")) {
    return;
  }
  state.brokersPendingRerender = false;
  renderActiveTab();
}

function clearBrokersFormGuardSoon(delayMs = 80) {
  if (state.brokersFormIdleTimer) clearTimeout(state.brokersFormIdleTimer);
  state.brokersFormIdleTimer = setTimeout(() => {
    state.brokersFormIdleTimer = null;
    const main = document.getElementById("main-content");
    const active = document.activeElement;
    if (main && active && main.contains(active) && isBrokersFormControl(active)) {
      return;
    }
    state.brokersFormActive = false;
    if (state.brokersPendingRerender && state.tab === "brokers") {
      syncBrokersTabSafe({ force: false });
    }
  }, delayMs);
}

async function submitOtpFlow(otp, explicitBroker = null) {
  state.otpError = "";
  const curPending = state.statusData && state.statusData.pending_otp;
  const targetBroker =
    explicitBroker || (curPending && curPending.broker) || null;
  try {
    const res = await miniappApi.submitOtp(otp, targetBroker);
    const nextPending = res.pending_otp
      ? normalizePendingOtp({ ...res.pending_otp, submitted: true })
      : curPending
        ? { ...curPending, submitted: true }
        : null;
    state.statusData = {
      ...(state.statusData || {}),
      pending_otp: nextPending,
    };
    const msg = res.message || "ส่ง OTP แล้ว กำลังรอโบรกเกอร์ยืนยัน...";
    toast(`⏳ ${msg}`, "ok");
    setLastResult({ kind: "ok", title: "⏳ กำลังตรวจสอบ OTP", detail: msg });
    renderStickyOtpBanner();
    syncBrokersTabSafe({ force: false, allowWhileTypingOtp: true });
    return res;
  } catch (err) {
    state.otpError = err.message || "ส่ง OTP ไม่สำเร็จ";
    toast(`❌ ${state.otpError}`, "err");
    setLastResult({ kind: "err", title: "❌ ส่ง OTP ไม่สำเร็จ", detail: state.otpError });
    renderStickyOtpBanner();
    syncBrokersTabSafe({ force: false, allowWhileTypingOtp: true });
    throw err;
  }
}

function syncOtpCountdown() {
  const pending = state.statusData && state.statusData.pending_otp;
  if (pending && !state.otpCountdownTimer) {
    state.otpCountdownTimer = setInterval(() => {
      const p = state.statusData && state.statusData.pending_otp;
      if (!p) {
        clearInterval(state.otpCountdownTimer);
        state.otpCountdownTimer = null;
        return;
      }
      if (state.tab !== "brokers") {
        renderStickyOtpBanner();
      } else {
        const cardHost = document.getElementById("brokers-otp-host");
        if (cardHost) {
          renderOtpWidget(cardHost, {
            pending: p,
            compact: false,
            error: state.otpError,
            onSubmit: submitOtpFlow,
            prefix: "card-otp",
          });
        }
      }
    }, 1000);
  } else if (!pending && state.otpCountdownTimer) {
    clearInterval(state.otpCountdownTimer);
    state.otpCountdownTimer = null;
  }
}

function renderStickyOtpBanner() {
  const host = document.getElementById("sticky-otp-banner");
  if (!host) return;
  const pending = state.statusData && state.statusData.pending_otp;
  syncOtpCountdown();
  if (!pending || state.tab === "brokers") {
    host.className = "sticky-otp-banner hidden";
    host.innerHTML = "";
    delete host.dataset.otpKey;
    delete host.dataset.otpMode;
    state.stickyOtpKey = null;
    return;
  }

  state.stickyOtpKey = `${pending.broker || ""}:${pending.ref || ""}`;
  host.className = "sticky-otp-banner";
  renderOtpWidget(host, {
    pending,
    compact: true,
    error: state.otpError,
    onSubmit: submitOtpFlow,
    prefix: "sticky-otp",
  });
}

function cancelConnectAllQueue() {
  state.queueAll = { active: false, step: null, error: null, libTriggered: false };
  if (state.tab === "brokers") renderActiveTab();
}

async function advanceConnectAllQueue() {
  const q = state.queueAll;
  if (!q || !q.active || !state.statusData) return;
  const brokers = state.statusData.brokers || {};
  const lp = state.statusData.login_progress || {};
  const kssAuth = Boolean(brokers.kss && brokers.kss.authenticated);
  const libAuth = Boolean(brokers.lib && brokers.lib.authenticated);
  const kssErr = lp.kss && lp.kss.phase === "error" ? lp.kss.error || lp.kss.message : null;
  const libErr = lp.lib && lp.lib.phase === "error" ? lp.lib.error || lp.lib.message : null;

  if (kssAuth && libAuth) {
    state.queueAll = { active: false, step: null, error: null, libTriggered: false };
    toast("✅ เชื่อมต่อครบทั้ง KSS และ Liberator แล้ว", "ok");
    setLastResult({
      kind: "ok",
      title: "✅ พร้อมเทรดทั้ง 2 โบรกเกอร์",
      detail: "Krungsri (KSS) และ Liberator ออนไลน์เรียบร้อยแล้ว",
    });
    if (state.tab === "brokers") renderActiveTab();
    return;
  }

  if (q.step === "kss") {
    if (kssAuth) {
      q.step = "lib";
      if (!q.libTriggered && !libAuth) {
        q.libTriggered = true;
        toast("🟢 KSS พร้อมแล้ว → กำลังเริ่ม Login Liberator...", "ok");
        if (state.tab === "brokers") renderActiveTab();
        try {
          await miniappApi.loginBroker("lib", false);
          await refreshStatus(false);
        } catch (err) {
          state.queueAll = { active: false, step: "lib", error: err.message, libTriggered: true };
          toast(`❌ LIB: ${err.message}`, "err");
          if (state.tab === "brokers") renderActiveTab();
        }
      }
      return;
    }
    if (kssErr) {
      state.queueAll = { active: false, step: "kss", error: kssErr, libTriggered: false };
      if (state.tab === "brokers") renderActiveTab();
    }
    return;
  }

  if (q.step === "lib" && libErr) {
    state.queueAll = { active: false, step: "lib", error: libErr, libTriggered: true };
    if (state.tab === "brokers") renderActiveTab();
  }
}

async function startConnectAllQueue() {
  const brokers = (state.statusData && state.statusData.brokers) || {};
  const kssAuth = Boolean(brokers.kss && brokers.kss.authenticated);
  const libAuth = Boolean(brokers.lib && brokers.lib.authenticated);
  if (kssAuth && libAuth) {
    toast("🟢 ทั้ง KSS และ Liberator ออนไลน์อยู่แล้ว", "ok");
    return;
  }
  state.queueAll = {
    active: true,
    step: kssAuth ? "lib" : "kss",
    error: null,
    libTriggered: kssAuth,
  };
  if (state.tab === "brokers") renderActiveTab();
  try {
    const first = kssAuth ? "lib" : "kss";
    await miniappApi.loginBroker(first, false);
    await refreshStatus(false);
    await advanceConnectAllQueue();
  } catch (err) {
    state.queueAll = {
      active: false,
      step: kssAuth ? "lib" : "kss",
      error: err.message || "เริ่มเชื่อมต่อไม่สำเร็จ",
      libTriggered: kssAuth,
    };
    toast(`❌ ${state.queueAll.error}`, "err");
    if (state.tab === "brokers") renderActiveTab();
  }
}

function renderInboxDrawer() {
  const drawer = document.getElementById("inbox-drawer");
  const quotaBox = document.getElementById("inbox-quota-summary");
  const listEl = document.getElementById("inbox-list");
  if (!drawer || !quotaBox || !listEl) return;

  if (!state.inboxOpen) {
    drawer.classList.add("hidden");
    return;
  }
  drawer.classList.remove("hidden");

  const q = state.quota || {};
  const qLimit = q.limit ?? 300;
  const qUsed = q.used;
  const qPct = Number(q.pct || 0);
  const qCls = q.exhausted || qPct >= 95 ? "danger" : qPct >= 75 ? "warn" : "";
  const qTxt = qUsed !== null && qUsed !== undefined ? `${qUsed} / ${qLimit}` : `? / ${qLimit}`;
  quotaBox.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
      <span><strong>📊 LINE Quota เดือนนี้:</strong> ${esc(qTxt)}</span>
      <span class="pill ${q.exhausted ? "pill-red" : "pill-sky"}">${esc(q.push_mode || "eod_only")}</span>
    </div>
    <div class="quota-bar-track"><div class="quota-bar-fill ${qCls}" style="width:${Math.min(100, Math.max(0, qPct))}%"></div></div>
    <div class="muted" style="font-size:11px">สรุปปิดตลาด EOD &amp; บันทึก Net Equity อัตโนมัติ จ.-ศ. เวลา ${esc(q.eod_push_time || "17:05")} น. (${q.line_eod_push_enabled ? "ส่ง LINE OA" : "MiniApp Inbox · ไม่ใช้โควต้า LINE"})</div>
    <div class="muted" style="font-size:11px">🗓️ ${esc(formatQuotaResetLabel(q))}</div>
  `;

  document.querySelectorAll("#inbox-filters [data-ifilter]").forEach((btn) => {
    btn.classList.toggle("pill-sky", btn.dataset.ifilter === state.inboxFilter);
  });

  const isSystemHeartbeat = (it) => {
    const c = String(it?.category || "").toLowerCase();
    const m = it?.meta || {};
    const title = String(it?.title || "");
    return c === "system" && (Boolean(m.is_heartbeat) || m.dedup_key === "gateway_online" || title.includes("Gateway ออนไลน์"));
  };

  const filtered = (state.inboxItems || []).filter((it) => {
    const c = String(it.category || "").toLowerCase();
    if (state.inboxFilter === "otp") return c === "otp";
    if (state.inboxFilter === "trade") return c === "trade" || c === "nego";
    if (state.inboxFilter === "eod") return c === "eod" || c === "system" || c === "session" || c === "readiness";
    return !isSystemHeartbeat(it);
  });

  if (!filtered.length) {
    listEl.innerHTML = `<div class="muted" style="text-align:center;padding:24px 8px">ยังไม่มีข้อความแจ้งเตือนในหมวดนี้</div>`;
    return;
  }

  listEl.innerHTML = filtered
    .map((it) => {
      const tsStr = it.ts ? String(it.ts).slice(11, 16) : "";
      const datePart = it.ts ? String(it.ts).slice(5, 10) : "";
      const repeatCount = Number(it.meta?.repeat_count || 1);
      const sevCls =
        it.severity === "error"
          ? "pill-red"
          : it.severity === "warning"
            ? "pill-amber"
            : it.severity === "success"
              ? "pill-green"
              : "pill-muted";
      return `
        <button class="inbox-item ${it.read ? "" : "unread"}" data-inbox-id="${esc(it.id)}" data-inbox-tab="${esc(it.action_tab || "")}" type="button">
          <div class="inbox-item-top">
            <span>${esc(it.title)}${repeatCount > 1 ? ` <span class="pill pill-muted">×${repeatCount}</span>` : ""}</span>
            <span class="pill ${sevCls}">${esc((it.category || "sys").toUpperCase())}</span>
          </div>
          ${it.body ? `<div class="inbox-item-body">${esc(it.body)}</div>` : ""}
          <div class="inbox-item-meta">
            <span>${esc(datePart)} ${esc(tsStr)}${it.line_pushed ? " · ส่ง LINE แล้ว" : ""}</span>
            <span>${it.action_tab ? `เปิด ${esc(it.action_tab)} →` : ""}</span>
          </div>
        </button>
      `;
    })
    .join("");

  listEl.querySelectorAll("[data-inbox-id]").forEach((el) => {
    el.addEventListener("click", async () => {
      const id = el.dataset.inboxId;
      const tab = el.dataset.inboxTab;
      const item = (state.inboxItems || []).find((x) => x.id === id);
      if (item && !item.read) {
        item.read = true;
        state.unreadCount = Math.max(0, Number(state.unreadCount || 0) - 1);
        renderInboxBadge();
        miniappApi.markInboxRead(id).catch(() => {});
      }
      closeInboxDrawer();
      if (tab && VALID_TABS.has(tab)) {
        navigate(tab);
      }
    });
  });
}

async function loadInbox(quiet = true) {
  try {
    const res = await miniappApi.getInbox(50);
    state.inboxItems = res.items || [];
    state.unreadCount = Number(res.unread_count || 0);
    if (res.quota) state.quota = res.quota;
    renderInboxBadge();
    renderQuotaPill();
    if (state.inboxOpen) renderInboxDrawer();
  } catch (err) {
    if (!quiet) toast(`❌ โหลด Inbox ไม่สำเร็จ: ${err.message}`, "err");
  }
}

async function openInboxDrawer() {
  state.inboxOpen = true;
  renderInboxDrawer();
  await loadInbox(false);
}

function closeInboxDrawer() {
  state.inboxOpen = false;
  renderInboxDrawer();
}

function handleSseEvent(eventName, payload) {
  if (eventName === "connected") {
    if (payload.unread_count !== undefined) {
      state.unreadCount = Number(payload.unread_count || 0);
      renderInboxBadge();
    }
    if (payload.quota) {
      state.quota = payload.quota;
      renderQuotaPill();
    }
    const patch = { ...(state.statusData || {}) };
    if (payload.pending_otp !== undefined) {
      patch.pending_otp = normalizePendingOtp(payload.pending_otp);
    }
    if (payload.login_progress !== undefined) {
      patch.login_progress = payload.login_progress;
    }
    state.statusData = pruneAuthenticatedPendingOtp(patch);
    renderStickyOtpBanner();
    syncBrokersTabSafe({ force: false });
    return;
  }
  if (eventName === "miniapp_inbox") {
    if (payload.item) {
      state.inboxItems = [payload.item, ...(state.inboxItems || []).filter((x) => x.id !== payload.item.id)].slice(0, 150);
      if (["otp", "error", "eod"].includes(String(payload.item.category || "")) || payload.item.severity === "error") {
        toast(payload.item.title, payload.item.severity === "error" ? "err" : "ok");
      }
    }
    if (payload.unread_count !== undefined) state.unreadCount = Number(payload.unread_count || 0);
    renderInboxBadge();
    if (state.inboxOpen) renderInboxDrawer();
    return;
  }
  if (eventName === "miniapp_inbox_read") {
    if (payload.item_id) {
      (state.inboxItems || []).forEach((it) => {
        if (it.id === payload.item_id) it.read = true;
      });
    } else {
      (state.inboxItems || []).forEach((it) => {
        it.read = true;
      });
    }
    if (payload.unread_count !== undefined) state.unreadCount = Number(payload.unread_count || 0);
    renderInboxBadge();
    if (state.inboxOpen) renderInboxDrawer();
    return;
  }
  if (eventName === "miniapp_inbox_cleared") {
    state.inboxItems = [];
    state.unreadCount = 0;
    renderInboxBadge();
    if (state.inboxOpen) renderInboxDrawer();
    return;
  }
  if (eventName === "otp_required") {
    state.otpError = "";
    state.statusData = {
      ...(state.statusData || {}),
      pending_otp: normalizePendingOtp({
        broker: payload.broker,
        broker_label: payload.broker_label || payload.label || payload.broker,
        ref: payload.ref || null,
        expires_in_seconds: payload.expires_in_seconds ?? payload.timeout_seconds ?? null,
        submitted: Boolean(payload.submitted),
      }),
    };
    renderStickyOtpBanner();
    syncBrokersTabSafe({ force: false });
    return;
  }
  if (eventName === "otp_submitted") {
    const cur = (state.statusData && state.statusData.pending_otp) || {};
    state.statusData = {
      ...(state.statusData || {}),
      pending_otp: normalizePendingOtp({
        ...cur,
        ...payload,
        submitted: true,
      }),
    };
    renderStickyOtpBanner();
    syncBrokersTabSafe({ force: false, allowWhileTypingOtp: true });
    return;
  }
  if (eventName === "otp_resolved") {
    state.otpError = "";
    if (state.statusData) {
      state.statusData = { ...state.statusData, pending_otp: null };
      renderStickyOtpBanner();
      syncBrokersTabSafe({ force: false, allowWhileTypingOtp: true });
    }
    refreshStatus(false);
    return;
  }
  if (eventName === "login_progress" && payload && payload.broker) {
    const prevLp = (state.statusData && state.statusData.login_progress) || {};
    const slug = payload.broker;
    let nextPending = state.statusData ? state.statusData.pending_otp : null;
    if (payload.phase === "otp_rejected") {
      state.otpError = payload.error || "รหัส OTP ไม่ถูกต้องหรือหมดอายุ กรุณากรอกใหม่";
      if (nextPending) {
        nextPending = { ...nextPending, submitted: false };
      }
      toast(`❌ ${state.otpError}`, "err");
    } else if (payload.phase === "authenticated") {
      const pendingBrk = (nextPending && nextPending.broker) || "";
      const isMatch =
        (slug === "lib" && pendingBrk.includes("LIB")) ||
        (slug === "kss" && pendingBrk.includes("KRUNG"));
      if (isMatch) {
        nextPending = null;
        state.otpError = "";
      }
    } else if (payload.phase === "error" && payload.error) {
      toast(`❌ ${slug.toUpperCase()}: ${payload.error}`, "err");
    }
    state.statusData = {
      ...(state.statusData || {}),
      pending_otp: nextPending,
      login_progress: { ...prevLp, [slug]: payload },
    };
    renderStickyOtpBanner();
    syncBrokersTabSafe({
      force: false,
      allowWhileTypingOtp: payload.phase === "otp_rejected" || payload.phase === "authenticated",
    });
    if (payload.phase === "authenticated" || payload.phase === "error") {
      refreshStatus(false);
    }
    return;
  }
  if (eventName === "line_quota_updated") {
    state.quota = payload;
    renderQuotaPill();
    if (state.inboxOpen) renderInboxDrawer();
    return;
  }
  if (eventName === "kill_switch_updated") {
    state.statusData = { ...(state.statusData || {}), kill_switch: payload };
    updateTopBar(state.statusData, state.me);
    syncBrokersTabSafe({ force: false });
  }
}

async function connectMiniAppSse() {
  if (state.sseAbort) {
    try {
      state.sseAbort.abort();
    } catch (_ignored) {}
  }
  if (state.sseRetryTimer) {
    clearTimeout(state.sseRetryTimer);
    state.sseRetryTimer = null;
  }

  const controller = new AbortController();
  state.sseAbort = controller;
  const headers = new Headers();
  const token = getIdToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);

  let shouldRetry = false;
  try {
    const resp = await fetch("/api/v1/miniapp/events", {
      headers,
      cache: "no-store",
      signal: controller.signal,
    });
    if (!resp.ok || !resp.body) {
      if (resp.status === 401) {
        const refreshed = await refreshAuthOnUnauthorized();
        if (refreshed) shouldRetry = true;
      } else if (resp.status >= 500) {
        shouldRetry = true;
      }
      return;
    }
    shouldRetry = true;
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop() || "";
      for (const block of blocks) {
        if (!block.trim() || block.startsWith(":")) continue;
        let evName = "message";
        const dataLines = [];
        for (const rawLine of block.split("\n")) {
          const line = rawLine.replace(/\r$/, "");
          if (line.startsWith("event:")) evName = line.slice(6).trim();
          else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
        }
        if (dataLines.length) {
          try {
            handleSseEvent(evName, JSON.parse(dataLines.join("\n")));
          } catch (_ignored) {}
        }
      }
    }
  } catch (err) {
    if (err && err.name !== "AbortError") {
      shouldRetry = true;
    }
  } finally {
    if (shouldRetry && !controller.signal.aborted && document.visibilityState === "visible") {
      state.sseRetryTimer = setTimeout(() => connectMiniAppSse(), 5000);
    }
  }
}

function syncBottomNavActive(tab = state.tab) {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    const isMatch = btn.dataset.tab === tab;
    btn.classList.toggle("active", isMatch);
    if (isMatch) btn.setAttribute("aria-current", "page");
    else btn.removeAttribute("aria-current");
  });
}

function isOverlayOpen() {
  const sheet = document.getElementById("sheet-backdrop");
  const seModal = document.getElementById("se-poll-backdrop");
  return Boolean(
    (sheet && !sheet.classList.contains("hidden")) ||
      seModal ||
      state.inboxOpen
  );
}

function navigate(nextTab, extraParams = {}) {
  if (isAppBusy()) return;
  let targetTab = VALID_TABS.has(nextTab) ? nextTab : "brokers";
  const nextParams = { ...extraParams };
  if (targetTab === "nego" && nextParams.order_no) {
    targetTab = "orders";
    nextParams.nego_order_no = nextParams.order_no;
  }
  if (targetTab !== state.tab) {
    clearToast();
    state.brokersFormActive = false;
    state.brokersPendingRerender = false;
    state.lastBrokersSig = null;
  }
  const hasPrefill = Boolean(
    nextParams.side ||
      nextParams.symbol ||
      nextParams.subtab ||
      nextParams.sbl_action ||
      nextParams.order_no ||
      nextParams.nego_order_no
  );
  state.params = { ...nextParams, ...(hasPrefill ? { _consumePrefill: true } : {}) };
  state.tab = targetTab;
  syncBottomNavActive(targetTab);
  renderActiveTab();
}

function switchTab(nextTab) {
  if (isAppBusy()) return;
  clearToast();
  navigate(nextTab, {});
}

function renderActiveTab() {
  renderStickyOtpBanner();
  syncBottomNavActive(state.tab);

  const main = document.getElementById("main-content");
  if (!main) return;

  const mySeq = ++state.renderSeq;
  const ctx = buildRenderCtx();

  if (state.tab === "brokers") {
    state.lastBrokersSig = brokersRenderSignature(state.statusData, ctx);
    renderBrokersView(main, state.statusData, ctx);
    return;
  }
  if (state.tab === "portfolio") {
    renderPortfolioView(main, ctx);
    return;
  }
  if (state.tab === "orders") {
    renderOrdersView(main, ctx);
    return;
  }
  if (state.tab === "nego") {
    renderNegoView(main, ctx);
    return;
  }
  if (state.tab === "trade") {
    renderTradeView(main, ctx);
  }
}

async function refreshStatus(showToast = false) {
  try {
    const data = await miniappApi.getBrokersStatus();
    if (data && data.pending_otp) {
      data.pending_otp = normalizePendingOtp(data.pending_otp);
    }
    state.statusData = pruneAuthenticatedPendingOtp(data);
    updateTopBar(data, state.me);
    await advanceConnectAllQueue();
    syncBrokersTabSafe({ force: false });
    if (showToast) toast("อัปเดตสถานะล่าสุดแล้ว", "ok");
  } catch (err) {
    if (showToast) toast(`❌ ${err.message}`, "err");
  }
}

function renderLockedScreen(title, message, showLoginBtn = false) {
  const main = document.getElementById("main-content");
  if (!main) return;
  main.innerHTML = `
    <section class="card card-danger">
      <div class="card-head">
        <div class="card-title">🔒 ${title}</div>
        <span class="pill pill-red">FAIL-CLOSED</span>
      </div>
      <div class="muted">${message}</div>
      ${
        showLoginBtn
          ? `<button id="liff-login-btn" class="btn btn-primary" type="button">เข้าสู่ระบบด้วย LINE</button>`
          : ""
      }
    </section>
  `;
  const btn = document.getElementById("liff-login-btn");
  if (btn) btn.addEventListener("click", loginWithLiff);
}

async function bootstrap() {
  updateFooterVersion();
  state.params = parseDeepLinkParams();
  if (
    state.params.side ||
    state.params.symbol ||
    state.params.subtab ||
    state.params.sbl_action ||
    state.params.order_no ||
    state.params.nego_order_no
  ) {
    state.params._consumePrefill = true;
  }
  if (state.params.tab && VALID_TABS.has(state.params.tab)) {
    state.tab = state.params.tab;
    if (state.tab === "nego" && state.params.order_no) {
      state.tab = "orders";
      state.params.nego_order_no = state.params.order_no;
    }
  }

  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });
  document.querySelectorAll("[data-jump-tab]").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.jumpTab));
  });

  const mainContentEl = document.getElementById("main-content");
  if (mainContentEl) {
    const activateFormGuard = (ev) => {
      if (state.tab !== "brokers") return;
      const target = ev.target && ev.target.closest ? ev.target.closest("select, input, textarea") : null;
      if (!target || !isBrokersFormControl(target)) return;
      state.brokersFormActive = true;
      if (state.brokersFormIdleTimer) {
        clearTimeout(state.brokersFormIdleTimer);
        state.brokersFormIdleTimer = null;
      }
      if (String(target.tagName || "").toUpperCase() === "SELECT") {
        // Fallback safety in case native picker closes without blur/change on mobile WebView
        state.brokersFormIdleTimer = setTimeout(() => {
          const cur = document.activeElement;
          if (!cur || !mainContentEl.contains(cur) || !isBrokersFormControl(cur)) {
            state.brokersFormActive = false;
            if (state.brokersPendingRerender && state.tab === "brokers") {
              syncBrokersTabSafe({ force: false });
            }
          }
        }, 12000);
      }
    };
    mainContentEl.addEventListener("pointerdown", activateFormGuard, true);
    mainContentEl.addEventListener("focusin", activateFormGuard, true);
    mainContentEl.addEventListener(
      "change",
      (ev) => {
        if (state.tab !== "brokers") return;
        if (ev.target && isBrokersFormControl(ev.target)) {
          clearBrokersFormGuardSoon(150);
        }
      },
      true
    );
    mainContentEl.addEventListener(
      "focusout",
      (ev) => {
        if (state.tab !== "brokers") return;
        if (ev.target && isBrokersFormControl(ev.target)) {
          clearBrokersFormGuardSoon(150);
        }
      },
      true
    );
  }

  const topKillBtn = document.getElementById("top-kill-btn");
  if (topKillBtn) {
    topKillBtn.addEventListener("click", () => switchTab("brokers"));
  }
  const topInboxBtn = document.getElementById("top-inbox-btn");
  if (topInboxBtn) {
    topInboxBtn.addEventListener("click", () => openInboxDrawer());
  }
  const topQuotaPill = document.getElementById("top-quota-pill");
  if (topQuotaPill) {
    topQuotaPill.addEventListener("click", () => openInboxDrawer());
  }
  const inboxCloseBtn = document.getElementById("inbox-close-btn");
  if (inboxCloseBtn) {
    inboxCloseBtn.addEventListener("click", closeInboxDrawer);
  }
  const inboxBackdrop = document.getElementById("inbox-drawer");
  if (inboxBackdrop) {
    inboxBackdrop.addEventListener("click", (e) => {
      if (e.target === inboxBackdrop) closeInboxDrawer();
    });
  }
  const markAllBtn = document.getElementById("inbox-mark-all-btn");
  if (markAllBtn) {
    markAllBtn.addEventListener("click", async () => {
      try {
        await miniappApi.markInboxRead(null);
        state.inboxItems = (state.inboxItems || []).map((it) => ({ ...it, read: true }));
        state.unreadCount = 0;
        renderInboxBadge();
        renderInboxDrawer();
      } catch (err) {
        toast(`❌ ${err.message}`, "err");
      }
    });
  }
  const exportBtn = document.getElementById("inbox-export-btn");
  if (exportBtn) {
    exportBtn.addEventListener("click", () => {
      const items = state.inboxItems || [];
      if (!items.length) {
        toast("ยังไม่มีประวัติให้สำรอง", "err");
        return;
      }
      try {
        const blob = new Blob([JSON.stringify(items, null, 2)], { type: "application/json" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `homelab_notifications_${new Date().toISOString().slice(0, 10)}.json`;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
        toast(`💾 ดาวน์โหลดสำรอง ${items.length} รายการแล้ว`, "ok");
      } catch (err) {
        toast(`❌ สำรองไฟล์ไม่สำเร็จ: ${err.message}`, "err");
      }
    });
  }
  const clearBtn = document.getElementById("inbox-clear-btn");
  if (clearBtn) {
    clearBtn.addEventListener("click", async () => {
      if (clearBtn.disabled) return;
      clearBtn.disabled = true;
      try {
        const res = await miniappApi.clearInbox(true);
        state.inboxItems = [];
        state.unreadCount = 0;
        renderInboxBadge();
        renderInboxDrawer();
        const cnt = Number(res?.cleared || 0);
        toast(
          res?.archived_file
            ? `🗑️ สำรองและล้างประวัติ ${cnt} รายการแล้ว`
            : `🗑️ ล้างประวัติแจ้งเตือนแล้ว (${cnt})`,
          "ok"
        );
      } catch (err) {
        toast(`❌ ล้างประวัติไม่สำเร็จ: ${err.message}`, "err");
      } finally {
        clearBtn.disabled = false;
      }
    });
  }
  document.querySelectorAll("#inbox-filters [data-ifilter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.inboxFilter = btn.dataset.ifilter || "all";
      renderInboxDrawer();
    });
  });

  try {
    const authRes = await initAuth();
    if (!authRes.ok) {
      if (authRes.reason === "NOT_LOGGED_IN") {
        renderLockedScreen(
          "ต้องเข้าสู่ระบบ LINE LIFF",
          "กรุณาเข้าสู่ระบบด้วยบัญชี LINE ที่ได้รับอนุญาตเพื่อใช้งาน InvestmentHomelab",
          true
        );
      } else {
        renderLockedScreen(
          "ไม่สามารถยืนยันตัวตน LIFF",
          `สาเหตุ: ${authRes.reason} — กรุณาเปิดผ่าน LINE OA Rich Menu`
        );
      }
      return;
    }

    state.me = await miniappApi.getMe();
    await refreshStatus(false);
    renderActiveTab();
    loadInbox(true);
    connectMiniAppSse();

    const handleAppResume = async (forceViewRefresh = false) => {
      if (isAppBusy() || isTypingOtp()) return;
      const elapsedMs = state.lastHiddenAt ? Date.now() - state.lastHiddenAt : 0;
      const shouldSync = forceViewRefresh || elapsedMs >= 15000;
      if (!shouldSync) return;
      state.lastHiddenAt = 0;
      await refreshStatus(false);
      loadInbox(true);
      if (!state.sseAbort || state.sseAbort.signal.aborted) {
        connectMiniAppSse();
      }
      if (!isOverlayOpen() && ["nego", "orders", "portfolio"].includes(state.tab)) {
        renderActiveTab();
      }
    };

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") {
        state.lastHiddenAt = Date.now();
      } else if (document.visibilityState === "visible") {
        handleAppResume(false);
      }
    });
    window.addEventListener("pageshow", (ev) => {
      if (ev && ev.persisted) {
        handleAppResume(true);
      }
    });

    state.pollTimer = setInterval(() => {
      if (isAppBusy() || document.visibilityState !== "visible") return;
      const lp = (state.statusData && state.statusData.login_progress) || {};
      const loginActive = Boolean(
        (lp.kss && lp.kss.is_active) ||
          (lp.lib && lp.lib.is_active) ||
          (state.queueAll && state.queueAll.active) ||
          (state.statusData && state.statusData.pending_otp)
      );
      if (loginActive || state.tab === "brokers") {
        refreshStatus(false);
      }
    }, 3000);
  } catch (err) {
    const hint = err.status === 401 || err.status === 403
      ? "ยังไม่ได้ตั้งค่า LIFF_ID / LINE_ALLOWED_USER_IDS ใน .env หรือบัญชี LINE นี้ไม่มีสิทธิ์เข้าถึง"
      : err.message;
    renderLockedScreen("การเชื่อมต่อถูกปฏิเสธ", hint, Boolean(window.liff));
  }
}

document.addEventListener("DOMContentLoaded", bootstrap);
