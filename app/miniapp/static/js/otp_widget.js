/**
 * Shared 6-box segmented OTP widget with iOS/Android SMS AutoFill,
 * 6-digit paste, immediate auto-submit, and non-blinking countdown.
 */

function escapeHtml(val) {
  return String(val ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export function formatOtpCountdown(expiresInSeconds) {
  const sec = Math.max(0, Math.floor(Number(expiresInSeconds) || 0));
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function computeRemainingSeconds(pending) {
  if (!pending) return 0;
  if (typeof pending._deadlineMs === "number") {
    return Math.max(0, Math.ceil((pending._deadlineMs - Date.now()) / 1000));
  }
  return Math.max(0, Number(pending.expires_in_seconds ?? 0));
}

/**
 * Mount or incrementally update a 6-box OTP widget inside `container`.
 * Calling this repeatedly every 1s preserves focus and typed digits unless
 * `pending` (broker/ref) or `mode` (`input` vs `verifying`) changed.
 */
export function renderOtpWidget(container, opts) {
  if (!container) return;
  const {
    pending,
    compact = false,
    error = "",
    onSubmit,
    prefix = compact ? "sticky-otp" : "card-otp",
  } = opts || {};

  if (!pending) {
    container.innerHTML = "";
    delete container.dataset.otpKey;
    delete container.dataset.otpMode;
    return;
  }

  const key = `${pending.broker || ""}:${pending.ref || ""}`;
  const isVerifying = Boolean(pending.submitted);
  const mode = isVerifying ? "verifying" : "input";
  const remain = computeRemainingSeconds(pending);
  const refBadge = escapeHtml(pending.ref || "-");
  const brokerLabel = escapeHtml(pending.broker_label || pending.broker || "Broker");

  // Incremental update when already mounted for same (broker, ref, mode) and no error reset
  const alreadyMounted =
    container.dataset.otpKey === key &&
    container.dataset.otpMode === mode &&
    container.querySelector(".otp-widget");

  if (alreadyMounted && !error) {
    const timerEl = container.querySelector(".otp-timer");
    if (timerEl) {
      timerEl.textContent = remain > 0 ? `เหลือ ${formatOtpCountdown(remain)}` : "ใกล้หมดเวลา";
    }
    return;
  }

  container.dataset.otpKey = key;
  container.dataset.otpMode = mode;

  if (isVerifying) {
    container.innerHTML = `
      <div class="otp-widget otp-widget-verifying ${compact ? "is-compact" : ""}" data-testid="${prefix}-verifying">
        <div class="otp-widget-head">
          <div class="otp-widget-title">
            <span class="otp-spinner" aria-hidden="true"></span>
            <span>กำลังตรวจสอบ OTP กับ ${brokerLabel}...</span>
          </div>
          <span class="otp-timer muted">${remain > 0 ? `เหลือ ${formatOtpCountdown(remain)}` : ""}</span>
        </div>
        <div class="otp-widget-sub muted">
          Ref: <strong>${refBadge}</strong> · ส่งรหัสให้ระบบแล้ว โปรดรอสักครู่หากโบรกเกอร์อนุมัติหรือปฏิเสธ
        </div>
      </div>
    `;
    return;
  }

  const boxesHtml = [0, 1, 2, 3, 4, 5]
    .map(
      (idx) => `
      <input
        type="text"
        inputmode="numeric"
        pattern="[0-9]*"
        maxlength="${idx === 0 ? 6 : 1}"
        class="otp-digit"
        id="${prefix}-digit-${idx}"
        data-otp-idx="${idx}"
        aria-label="OTP digit ${idx + 1}"
        ${idx === 0 ? 'autocomplete="one-time-code"' : 'autocomplete="off"'}
        required
      />`
    )
    .join("");

  container.innerHTML = `
    <form class="otp-widget ${compact ? "is-compact" : ""}" id="${prefix}-form" novalidate>
      <div class="otp-widget-head">
        <div class="otp-widget-title">
          <strong>🔐 OTP ${brokerLabel}</strong>
          <span class="badge badge-warn">Ref: ${refBadge}</span>
        </div>
        <span class="otp-timer muted">${remain > 0 ? `เหลือ ${formatOtpCountdown(remain)}` : ""}</span>
      </div>
      ${error ? `<div class="otp-error" role="alert">${escapeHtml(error)}</div>` : ""}
      <div class="otp-boxes-row">
        <div class="otp-boxes" role="group" aria-label="กรอกรหัส OTP 6 หลัก">
          ${boxesHtml}
        </div>
        <button type="submit" class="btn btn-primary ${compact ? "btn-sm" : ""} otp-submit-btn">
          ส่ง
        </button>
      </div>
    </form>
  `;

  const form = container.querySelector("form.otp-widget");
  const inputs = Array.from(container.querySelectorAll("input.otp-digit"));
  const submitBtn = container.querySelector(".otp-submit-btn");
  let inFlight = false;

  const collectCode = () => inputs.map((el) => (el.value || "").replace(/\D/g, "").slice(-1)).join("");

  const fillFromString = (raw) => {
    const digits = String(raw || "").replace(/\D/g, "").slice(0, 6);
    inputs.forEach((el, i) => {
      el.value = digits[i] || "";
      el.classList.toggle("is-filled", Boolean(el.value));
    });
    return digits;
  };

  const setBusy = (busy) => {
    inFlight = busy;
    if (submitBtn) {
      submitBtn.disabled = busy;
      submitBtn.textContent = busy ? "..." : "ส่ง";
    }
    inputs.forEach((el) => {
      el.disabled = busy;
    });
  };

  const triggerSubmit = async () => {
    if (inFlight) return;
    const code = collectCode();
    if (!/^\d{6}$/.test(code)) {
      const errNode = container.querySelector(".otp-error");
      if (errNode) errNode.textContent = "กรุณากรอกรหัส OTP ครบ 6 หลัก";
      const firstEmpty = inputs.find((el) => !el.value) || inputs[0];
      firstEmpty?.focus();
      return;
    }
    setBusy(true);
    try {
      if (typeof onSubmit === "function") {
        await onSubmit(code, pending.broker);
      }
    } catch (_err) {
      // Allow immediate re-entry if API fails before reload
      setBusy(false);
    }
  };

  inputs.forEach((input, idx) => {
    input.addEventListener("input", (ev) => {
      if (inFlight) return;
      const digits = (input.value || "").replace(/\D/g, "");
      if (digits.length > 1) {
        // SMS AutoFill (iOS/Android drops full 6-digit string into box 0) or paste
        const filled = fillFromString(digits);
        if (filled.length === 6) {
          triggerSubmit();
        } else {
          inputs[Math.min(filled.length, 5)]?.focus();
        }
        return;
      }
      input.value = digits;
      input.classList.toggle("is-filled", Boolean(digits));
      if (digits && idx < inputs.length - 1) {
        inputs[idx + 1].focus();
        inputs[idx + 1].select?.();
      }
      if (collectCode().length === 6 && ev.inputType !== "deleteContentBackward") {
        triggerSubmit();
      }
    });

    input.addEventListener("keydown", (ev) => {
      if (ev.key === "Backspace" && !input.value && idx > 0) {
        inputs[idx - 1].value = "";
        inputs[idx - 1].classList.remove("is-filled");
        inputs[idx - 1].focus();
        ev.preventDefault();
      } else if (ev.key === "ArrowLeft" && idx > 0) {
        inputs[idx - 1].focus();
      } else if (ev.key === "ArrowRight" && idx < inputs.length - 1) {
        inputs[idx + 1].focus();
      }
    });

    input.addEventListener("paste", (ev) => {
      const text = ev.clipboardData?.getData("text") || "";
      const digits = text.replace(/\D/g, "");
      if (digits.length >= 2) {
        ev.preventDefault();
        const filled = fillFromString(digits);
        if (filled.length === 6) {
          triggerSubmit();
        } else {
          inputs[Math.min(filled.length, 5)]?.focus();
        }
      }
    });
  });

  form?.addEventListener("submit", (ev) => {
    ev.preventDefault();
    triggerSubmit();
  });

  if (error || !compact) {
    setTimeout(() => {
      const cur = document.activeElement;
      const curTag = String(cur?.tagName || "").toUpperCase();
      if (cur?.classList?.contains("otp-digit") || curTag === "SELECT" || curTag === "INPUT" || curTag === "TEXTAREA") {
        return;
      }
      inputs[0]?.focus?.();
    }, 30);
  }
}
