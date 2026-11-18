import { getIdToken, refreshAuthOnUnauthorized } from "./auth.js";

export class ApiError extends Error {
  constructor(status, message, detail = null, code = null) {
    super(message);
    this.status = status;
    this.detail = detail;
    this.code = code;
  }
}

const DEFAULT_TIMEOUT_MS = 20000;

export async function apiRequest(path, options = {}) {
  const headers = new Headers(options.headers || {});
  const token = getIdToken();
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  if (options.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  const timeoutMs = Number(options.timeoutMs) > 0 ? Number(options.timeoutMs) : DEFAULT_TIMEOUT_MS;
  const controller = new AbortController();
  const externalSignal = options.signal;
  const onExternalAbort = () => controller.abort();
  if (externalSignal) {
    if (externalSignal.aborted) {
      controller.abort();
    } else {
      externalSignal.addEventListener("abort", onExternalAbort, { once: true });
    }
  }
  const timerId = setTimeout(() => controller.abort(), timeoutMs);

  let resp;
  try {
    resp = await fetch(path, {
      method: options.method || "GET",
      headers,
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
      cache: "no-store",
      signal: controller.signal,
    });
  } catch (err) {
    if (err && err.name === "AbortError") {
      const sec = Math.max(1, Math.round(timeoutMs / 1000));
      throw new ApiError(
        408,
        `เชื่อมต่อเซิร์ฟเวอร์หมดเวลา (เกิน ${sec} วินาที) — กด 'ลองโหลดใหม่' หรือตรวจสอบแท็บ Brokers`,
        null,
        "E_TIMEOUT"
      );
    }
    throw err;
  } finally {
    clearTimeout(timerId);
    if (externalSignal) {
      externalSignal.removeEventListener("abort", onExternalAbort);
    }
  }

  let data = null;
  const ct = resp.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    data = await resp.json().catch(() => null);
  } else {
    const txt = await resp.text().catch(() => "");
    data = txt ? { detail: txt } : null;
  }

  if (!resp.ok) {
    if (resp.status === 401 && !options._retried401) {
      const refreshed = await refreshAuthOnUnauthorized();
      if (refreshed) {
        return apiRequest(path, { ...options, _retried401: true });
      }
    }
    const rawDetail = data && data.detail;
    const isObjDetail = rawDetail && typeof rawDetail === "object";
    const code = (isObjDetail ? rawDetail.code : data && data.code) || null;
    const msg =
      (isObjDetail ? rawDetail.message || rawDetail.code : rawDetail) ||
      (data && data.message) ||
      `HTTP ${resp.status}`;
    throw new ApiError(resp.status, String(msg), data, code);
  }
  return data;
}

export const miniappApi = {
  getMe: () => apiRequest("/api/v1/miniapp/me"),
  getBrokersStatus: () => apiRequest("/api/v1/miniapp/brokers/status"),
  loginBroker: (broker, force = false) =>
    apiRequest(`/api/v1/miniapp/brokers/${encodeURIComponent(broker)}/login`, {
      method: "POST",
      body: { force },
      timeoutMs: 45000,
    }),
  stopBroker: (broker) =>
    apiRequest(`/api/v1/miniapp/brokers/${encodeURIComponent(broker)}/stop`, {
      method: "POST",
      timeoutMs: 30000,
    }),
  submitOtp: (otp, broker = null) =>
    apiRequest("/api/v1/miniapp/otp", {
      method: "POST",
      body: broker ? { otp, broker } : { otp },
    }),
  toggleKillSwitch: (action, reason = "Triggered via LINE MiniApp") =>
    apiRequest("/api/v1/miniapp/kill-switch", {
      method: "POST",
      body: { action, reason },
    }),

  // Phase 2: Portfolio & SBL
  getPortfolio: (broker = "all", kssAccount = null, forceSbl = false) => {
    const q = new URLSearchParams({ broker });
    if (kssAccount) q.set("kss_account", kssAccount);
    if (forceSbl) q.set("force_sbl", "true");
    return apiRequest(`/api/v1/miniapp/portfolio?${q.toString()}`);
  },
  switchKssAccount: (accountNo) =>
    apiRequest("/api/v1/miniapp/portfolio/kss-account", {
      method: "POST",
      body: { account_no: accountNo },
      timeoutMs: 35000,
    }),
  saveAccountRouting: (payload) =>
    apiRequest("/api/v1/miniapp/brokers/account-routing", {
      method: "POST",
      body: payload,
    }),
  getSblPool: (symbol) =>
    apiRequest(`/api/v1/miniapp/sbl/pool/${encodeURIComponent(symbol)}`),

  // Phase 3: Orders & Fast Nego
  getOrders: (broker = "all") =>
    apiRequest(`/api/v1/miniapp/orders?broker=${encodeURIComponent(broker)}`, {
      timeoutMs: 40000,
    }),
  cancelOrder: (broker, orderNo, symbol = "") =>
    apiRequest("/api/v1/miniapp/orders/cancel", {
      method: "POST",
      body: { broker, order_no: orderNo, symbol },
      timeoutMs: 35000,
    }),
  getNegoQuote: (broker, symbol, side = null, oldPrice = null) => {
    const q = new URLSearchParams({ broker, symbol });
    if (side) q.set("side", side);
    if (oldPrice !== null && oldPrice !== undefined) q.set("old_price", String(oldPrice));
    return apiRequest(`/api/v1/miniapp/nego/quote?${q.toString()}`);
  },
  executeNego: (payload) =>
    apiRequest("/api/v1/miniapp/nego/execute", {
      method: "POST",
      body: payload,
      timeoutMs: 35000,
    }),
  getNegoSchedule: () => apiRequest("/api/v1/miniapp/nego/schedule"),
  toggleNegoSchedule: (payload) =>
    apiRequest("/api/v1/miniapp/nego/schedule/toggle", {
      method: "POST",
      body: payload,
    }),
  rerollNegoSchedule: (windowId = null) =>
    apiRequest("/api/v1/miniapp/nego/schedule/reroll", {
      method: "POST",
      body: windowId ? { window_id: windowId } : {},
    }),
  runNegoScheduleNow: (windowId) =>
    apiRequest("/api/v1/miniapp/nego/schedule/run-now", {
      method: "POST",
      body: { window_id: windowId },
      timeoutMs: 45000,
    }),

  // Phase 4: Quote, Trade Preview/Execute, SBL Borrow/Return
  getQuote: (symbol, broker = "kss", side = null) => {
    const q = new URLSearchParams({ symbol, broker });
    if (side) q.set("side", side);
    return apiRequest(`/api/v1/miniapp/quote?${q.toString()}`);
  },
  previewTrade: (payload) =>
    apiRequest("/api/v1/miniapp/trade/preview", {
      method: "POST",
      body: payload,
    }),
  executeTrade: (payload) =>
    apiRequest("/api/v1/miniapp/trade/execute", {
      method: "POST",
      body: payload,
      timeoutMs: 40000,
    }),
  sblAction: (payload) =>
    apiRequest("/api/v1/miniapp/sbl/action", {
      method: "POST",
      body: payload,
      timeoutMs: payload && payload.action === "return" ? 45000 : 30000,
    }),

  // Phase 6: MiniApp Notification Inbox, LINE Quota & EOD Summary
  getInbox: (limit = 50) =>
    apiRequest(`/api/v1/miniapp/inbox?limit=${encodeURIComponent(limit)}`),
  markInboxRead: (itemId = null) =>
    apiRequest("/api/v1/miniapp/inbox/read", {
      method: "POST",
      body: itemId ? { item_id: itemId } : {},
    }),
  clearInbox: (archive = true) =>
    apiRequest("/api/v1/miniapp/inbox/clear", {
      method: "POST",
      body: { archive: Boolean(archive) },
    }),
  getQuota: (refresh = false) =>
    apiRequest(`/api/v1/miniapp/quota?refresh=${refresh ? "true" : "false"}`),
  getEodPreview: () => apiRequest("/api/v1/miniapp/eod/preview"),
  sendEodNow: (force = true) =>
    apiRequest("/api/v1/miniapp/eod/send-now", {
      method: "POST",
      body: { force },
      timeoutMs: 35000,
    }),
  toggleEodAuto: (enabled) =>
    apiRequest("/api/v1/miniapp/eod/toggle", {
      method: "POST",
      body: { enabled: Boolean(enabled) },
    }),
  getNetEquityHistory: (limit = 90) =>
    apiRequest(`/api/v1/miniapp/eod/net-equity-history?limit=${Number(limit) || 90}`),

  // Runbook Auto-Exit (Phase 1)
  getRunbookExits: () => apiRequest("/api/v1/miniapp/runbook/exits"),
  submitRunbookExits: (payload = {}) =>
    apiRequest("/api/v1/miniapp/runbook/exits/submit", {
      method: "POST",
      body: payload,
      timeoutMs: 45000,
    }),
  rerollRunbookExitSchedule: () =>
    apiRequest("/api/v1/miniapp/runbook/exits/schedule/reroll", {
      method: "POST",
    }),
  runRunbookExitNow: () =>
    apiRequest("/api/v1/miniapp/runbook/exits/schedule/run-now", {
      method: "POST",
      timeoutMs: 45000,
    }),
  cancelRunbookExitQueue: () =>
    apiRequest("/api/v1/miniapp/runbook/exits/schedule/cancel", {
      method: "POST",
    }),
  toggleRunbookExitAuto: (enabled) =>
    apiRequest("/api/v1/miniapp/runbook/exits/schedule/toggle", {
      method: "POST",
      body: { enabled: Boolean(enabled) },
    }),

  // Trade Hub & On-Demand Suggest Entry
  getTradeHubSummary: () => apiRequest("/api/v1/miniapp/trade/hub-summary"),
  pollSuggestEntry: (allowStale = false, overrideHoldCash = false) =>
    apiRequest(
      `/api/v1/miniapp/suggest-entry/poll?allow_stale=${allowStale ? "true" : "false"}&override_hold_cash=${overrideHoldCash ? "true" : "false"}`,
      { timeoutMs: 30000 }
    ),
  pollSuggestQuotes: (allowStale = false, overrideHoldCash = false) =>
    apiRequest(
      `/api/v1/miniapp/suggest-entry/quotes?allow_stale=${allowStale ? "true" : "false"}&override_hold_cash=${overrideHoldCash ? "true" : "false"}`,
      { timeoutMs: 10000 }
    ),
  streamSuggestQuotes: async ({
    allowStale = false,
    overrideHoldCash = false,
    intervalS = 2.0,
    signal,
    onQuotes,
    onError,
  }) => {
    const url = `/api/v1/miniapp/suggest-entry/stream?allow_stale=${allowStale ? "true" : "false"}&override_hold_cash=${overrideHoldCash ? "true" : "false"}&interval_s=${encodeURIComponent(intervalS)}`;
    const openStream = async () => {
      const headers = new Headers();
      const token = getIdToken();
      if (token) headers.set("Authorization", `Bearer ${token}`);
      return fetch(url, { method: "GET", headers, cache: "no-store", signal });
    };
    let resp = await openStream();
    if (resp.status === 401) {
      const refreshed = await refreshAuthOnUnauthorized();
      if (refreshed) resp = await openStream();
    }
    if (!resp.ok) {
      throw new ApiError(resp.status, `Stream HTTP ${resp.status}`);
    }
    if (!resp.body || !resp.body.getReader) {
      throw new Error("ReadableStream unsupported");
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      let sep;
      while ((sep = buf.indexOf("\n\n")) !== -1) {
        const rawBlock = buf.slice(0, sep);
        buf = buf.slice(sep + 2);
        let evName = "message";
        const dataLines = [];
        for (const line of rawBlock.split("\n")) {
          if (line.startsWith("event:")) evName = line.slice(6).trim();
          else if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
        }
        if (!dataLines.length) continue;
        try {
          const parsed = JSON.parse(dataLines.join("\n"));
          if (evName === "quotes" && onQuotes) onQuotes(parsed);
          else if (evName === "stream_error" && onError) onError(new Error(parsed.error || "stream_error"));
        } catch (_parseErr) {}
      }
    }
  },
  saveSuggestEntryConfig: (budgetPerSlotThb, useRecommended = false) =>
    apiRequest("/api/v1/miniapp/suggest-entry/config", {
      method: "POST",
      body: useRecommended
        ? {
            use_recommended: true,
            ...(budgetPerSlotThb != null ? { budget_per_slot_thb: Number(budgetPerSlotThb) } : {}),
          }
        : { budget_per_slot_thb: Number(budgetPerSlotThb) },
    }),
  submitSuggestEntry: (payload) =>
    apiRequest("/api/v1/miniapp/suggest-entry/submit", {
      method: "POST",
      body: payload,
      timeoutMs: 60000,
    }),

  // System Trading Rules (SET Short Sell +0 / +1)
  getTradingRules: () => apiRequest("/api/v1/miniapp/system/trading-rules"),
  saveTradingRules: (payload) =>
    apiRequest("/api/v1/miniapp/system/trading-rules", {
      method: "POST",
      body: payload,
    }),
};
