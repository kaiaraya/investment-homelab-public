/**
 * LIFF SDK v2 authentication & 24h Homelab session token helper.
 * Fails closed on public tunnel when LIFF_ID is missing or id_token is absent.
 */

let _cachedToken = null;
let _sessionToken = null;
let _sessionExp = 0;
let _config = null;
let _refreshPromise = null;

function _sessionStorageKey() {
  const lid = (_config && _config.liffId) || "default";
  return `homelab_miniapp_session:${lid}`;
}

function _loadStoredSession() {
  try {
    if (typeof window === "undefined" || !window.localStorage) return;
    const raw = window.localStorage.getItem(_sessionStorageKey());
    if (!raw) return;
    const parsed = JSON.parse(raw);
    const nowSec = Math.floor(Date.now() / 1000);
    if (parsed && typeof parsed.token === "string" && parsed.token.startsWith("hl_") && Number(parsed.exp) > nowSec + 60) {
      _sessionToken = parsed.token;
      _sessionExp = Number(parsed.exp);
    } else {
      window.localStorage.removeItem(_sessionStorageKey());
    }
  } catch (_err) {
    // ignore storage access issues
  }
}

function _saveStoredSession(token, exp) {
  _sessionToken = token || null;
  _sessionExp = Number(exp || 0);
  try {
    if (typeof window === "undefined" || !window.localStorage) return;
    if (_sessionToken && _sessionExp > 0) {
      window.localStorage.setItem(_sessionStorageKey(), JSON.stringify({ token: _sessionToken, exp: _sessionExp }));
    } else {
      window.localStorage.removeItem(_sessionStorageKey());
    }
  } catch (_err) {
    // ignore storage access issues
  }
}

export function clearStoredSession() {
  _sessionToken = null;
  _sessionExp = 0;
  try {
    if (typeof window !== "undefined" && window.localStorage) {
      window.localStorage.removeItem(_sessionStorageKey());
    }
  } catch (_err) {
    // ignore
  }
}

export async function loadConfig() {
  if (_config) return _config;
  const resp = await fetch("/miniapp/config.json", { cache: "no-store" });
  if (!resp.ok) throw new Error("โหลด config ของ MiniApp ไม่สำเร็จ");
  _config = await resp.json();
  _loadStoredSession();
  return _config;
}

function _hasValidSessionToken() {
  const nowSec = Math.floor(Date.now() / 1000);
  return Boolean(_sessionToken && _sessionExp > nowSec + 60);
}

export function getIdToken() {
  if (_hasValidSessionToken()) {
    return _sessionToken;
  }
  if (_sessionToken && !_hasValidSessionToken()) {
    clearStoredSession();
  }
  if (typeof window !== "undefined" && window.liff && _config?.liffConfigured) {
    try {
      if (window.liff.isLoggedIn()) {
        const fresh = window.liff.getIDToken();
        if (fresh) _cachedToken = fresh;
      }
    } catch (_err) {
      // ignore if liff not ready yet
    }
  }
  return _cachedToken;
}

export function loginWithLiff() {
  clearStoredSession();
  if (typeof window !== "undefined" && window.liff && _config?.liffConfigured) {
    window.liff.login({ redirectUri: window.location.href });
  }
}

export async function exchangeSessionToken(rawIdToken = null) {
  const idTok = rawIdToken || _cachedToken;
  if (!idTok) return false;
  try {
    const resp = await fetch("/api/v1/miniapp/auth/session", {
      method: "POST",
      headers: { Authorization: `Bearer ${idTok}` },
      cache: "no-store",
    });
    if (resp.status === 404 || resp.status === 405) {
      // Legacy mock server without /auth/session -> keep raw LIFF id_token
      return true;
    }
    if (!resp.ok) {
      return false;
    }
    const data = await resp.json().catch(() => null);
    if (data && data.session_token) {
      _saveStoredSession(data.session_token, data.expires_at);
      return true;
    }
  } catch (_err) {
    // Fall back to raw LIFF id_token on transient network glitch
  }
  return false;
}

export async function refreshAuthOnUnauthorized() {
  if (_refreshPromise) return _refreshPromise;
  _refreshPromise = (async () => {
    const cfg = await loadConfig().catch(() => _config);
    clearStoredSession();

    if (typeof window !== "undefined" && window.liff && cfg?.liffConfigured) {
      try {
        if (!window.liff.isLoggedIn()) {
          loginWithLiff();
          return false;
        }
        const fresh = window.liff.getIDToken();
        if (fresh) {
          _cachedToken = fresh;
          if (await exchangeSessionToken(fresh)) {
            return true;
          }
        }
        loginWithLiff();
      } catch (_err) {
        // ignore and fail closed
      }
    }
    return false;
  })();

  try {
    return await _refreshPromise;
  } finally {
    _refreshPromise = null;
  }
}

export async function initAuth() {
  const cfg = await loadConfig();

  if (cfg.liffConfigured && cfg.liffId) {
    if (!window.liff) {
      return { ok: false, reason: "LIFF_SDK_UNAVAILABLE", cfg };
    }
    await window.liff.init({ liffId: cfg.liffId });
    if (!window.liff.isLoggedIn()) {
      if (_hasValidSessionToken()) {
        return { ok: true, mode: "session", cfg };
      }
      return { ok: false, reason: "NOT_LOGGED_IN", cfg };
    }
    _cachedToken = window.liff.getIDToken();
    if (_cachedToken) {
      const exchanged = await exchangeSessionToken(_cachedToken);
      if (!exchanged && !_hasValidSessionToken()) {
        // If LINE cached an expired id_token and no valid hl_ session exists, re-login cleanly.
        const probeResp = await fetch("/api/v1/miniapp/me", {
          headers: { Authorization: `Bearer ${_cachedToken}` },
          cache: "no-store",
        }).catch(() => null);
        if (probeResp && probeResp.status === 401) {
          return { ok: false, reason: "NOT_LOGGED_IN", cfg };
        }
      }
      return { ok: true, mode: _hasValidSessionToken() ? "session" : "liff", cfg };
    }
    if (_hasValidSessionToken()) {
      return { ok: true, mode: "session", cfg };
    }
    return { ok: false, reason: "MISSING_ID_TOKEN", cfg };
  }

  // LIFF_ID not configured yet: attempt local-dev /me probe (backend decides fail-closed).
  return { ok: true, mode: "probe", cfg };
}
