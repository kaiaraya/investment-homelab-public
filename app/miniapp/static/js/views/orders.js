import { miniappApi } from "../api.js";
import { esc, fmtInt, fmtMoney, getShortUptickTicks, renderErrorRetryCard, runLockedButton } from "../utils.js";

let _state = {
  brokerFilter: "all",
  statusFilter: "open",
  data: null,
  loading: false,
  error: "",
  pendingNegoOrderNo: null,
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

function renderOrderCard(o) {
  const kind = String(o.kind || "").toLowerCase();
  const side = String(o.side || "").toUpperCase();
  const isOpen = Boolean(o.cancellable);
  const pillCls =
    kind === "matched"
      ? "pill-green"
      : kind === "cancelled" || kind === "rejected"
        ? "pill-muted"
        : "pill-amber";
  const sideCls =
    side === "BUY" || side === "COVER" ? "txt-green" : side === "SHORT" ? "txt-amber" : "txt-red";

  return `
    <section class="card">
      <div class="card-head">
        <div class="field-row">
          <strong class="${sideCls}">${esc(side)}</strong>
          <strong style="font-size:15px">${esc(o.symbol)}</strong>
          <span class="pill pill-muted">${esc(String(o.broker || "").toUpperCase())}</span>
        </div>
        <span class="pill ${pillCls}">${esc(o.status || kind.toUpperCase())}</span>
      </div>
      <div class="meta-row">
        <span>#${esc(o.order_no)}</span>
        <span>ราคา <strong>฿${fmtMoney(o.price)}</strong></span>
        <span>แมตช์ ${fmtInt(o.matched_volume)}/${fmtInt(o.volume)} (ค้าง ${fmtInt(o.balance)})</span>
      </div>
      ${
        isOpen
          ? `<div class="btn-row">
               <button class="btn btn-amber" data-nego-order='${esc(JSON.stringify(o))}' type="button">⚡ ต่อราคา</button>
               <button class="btn btn-danger" data-cancel-order='${esc(JSON.stringify(o))}' type="button">❌ ยกเลิก</button>
             </div>`
          : ""
      }
    </section>
  `;
}

export async function renderOrdersView(container, ctx) {
  const { params = {}, toast, confirmSheet, navigate, initialFetch = true, isCurrent = () => true, syncNav = null } = ctx;

  if (params._consumePrefill && (params.nego_order_no || params.order_no || params.status_filter)) {
    if (params.nego_order_no || params.order_no) {
      _state.pendingNegoOrderNo = String(params.nego_order_no || params.order_no);
    }
    if (["open", "matched", "all"].includes(String(params.status_filter || "").toLowerCase())) {
      _state.statusFilter = String(params.status_filter).toLowerCase();
    }
    delete params._consumePrefill;
  }

  const openFastNegoSheet = (o) => {
    const side = String(o.side || "").toUpperCase();
    const isShort = side === "SHORT";
    let quote = null;
    let quoteErr = "";
    let loadingQuote = true;
    let mode = "custom";
    let customPrice = String(o.price ?? "");
    let controlsRef = null;

    confirmSheet({
      title: `⚡ ต่อราคา ${side} ${o.symbol} #${o.order_no}`,
      confirmLabel: "✅ ยืนยันแก้ราคา",
      danger: false,
      busyTitle: `⏳ กำลังต่อราคา ${o.symbol} #${o.order_no}...`,
      renderBody: (bodyEl, controls) => {
        controlsRef = controls;

        const paintSheet = () => {
          const q = quote;
          const activeRules = (ctx.statusData && ctx.statusData.trading_rules) || {};
          const uptickTicks =
            q && q.short_uptick_ticks !== undefined
              ? (Number(q.short_uptick_ticks) === 1 ? 1 : 0)
              : getShortUptickTicks(activeRules, o.symbol);
          const lureOk = Boolean(q && q.lure_plan && q.lure_plan.ok);
          const matchAllowed = q ? (q.match_allowed !== undefined ? Boolean(q.match_allowed) : !isShort) : !isShort;
          const matchOk = Boolean(q && q.match_plan && q.match_plan.ok && matchAllowed);
          const numPx = Number(customPrice);
          const canSubmit =
            mode === "lure"
              ? lureOk
              : mode === "match"
                ? matchOk
                : Number.isFinite(numPx) && numPx > 0;
          controls.setConfirmEnabled(canSubmit);

          const modeSummary =
            mode === "lure" && lureOk
              ? `โหมด LURE → ฿${fmtMoney(q.lure_plan.price)}`
              : mode === "match" && matchOk
                ? `โหมด MATCH → ฿${fmtMoney(q.match_plan.price)}`
                : `กำหนดเอง → ฿${Number.isFinite(numPx) && numPx > 0 ? fmtMoney(numPx) : "—"}`;
          controls.setConfirmLabel(`✅ ${modeSummary}`);

          const strictReason = q && q.strict_uptick_reason ? ` (${esc(q.strict_uptick_reason)})` : "";
          const shortBannerHtml = isShort
            ? uptickTicks === 0
              ? `<div class="txt-sky" style="font-size:12px">ℹ️ SHORT (+0 Zero-Plus): วาง ≥ Last${matchOk ? " · เข้าเงื่อนไข Match ได้ทันที (Bid1 ≥ Last)" : " · เอา Match ออก (Bid1 &lt; Last) — ใช้ Lure รอคิว"}</div>`
              : `<div class="txt-amber" style="font-size:12px">⚠️ SHORT (+1 Strict Uptick${strictReason}): ห้าม Match — ตั้ง Lure &gt; Last เท่านั้น</div>`
            : "";

          const matchBtnHtml = matchOk
            ? `<button id="nego-match-btn" class="btn btn-success ${mode === "match" ? "pill-sky" : ""}" type="button">⚡ Match ฿${fmtMoney(q.match_plan.price)}</button>`
            : "";

          bodyEl.innerHTML = `
            <div style="display:flex;flex-direction:column;gap:10px">
              <div class="meta-row">
                <span class="pill pill-muted">${esc(String(o.broker || "").toUpperCase())}</span>
                <span>ตั้งเดิม <strong>฿${fmtMoney(o.price)}</strong></span>
                <span>ค้าง <strong>${fmtInt(o.balance)}</strong> หุ้น</span>
              </div>
              ${shortBannerHtml}
              ${loadingQuote ? `<div class="muted" style="font-size:12px">⏳ กำลังดึง Quote สด (Bid1/Last/Offer1)...</div>` : ""}
              ${quoteErr ? `<div class="txt-amber" style="font-size:12px">⚠️ ดึง Quote สดไม่สำเร็จ: ${esc(quoteErr)} (ยังตั้งราคาเองได้)</div>` : ""}
              <div class="quote-strip">
                <button class="quote-box" data-fill-px="${q?.bid_1 ?? ""}" ${!q?.bid_1 ? "disabled" : ""} type="button">
                  <small>Bid1</small><strong>${q?.bid_1 ? `฿${fmtMoney(q.bid_1)}` : "—"}</strong>
                </button>
                <button class="quote-box" data-fill-px="${q?.last ?? ""}" ${!q?.last ? "disabled" : ""} type="button">
                  <small>Last</small><strong>${q?.last ? `฿${fmtMoney(q.last)}` : "—"}</strong>
                </button>
                <button class="quote-box" data-fill-px="${q?.offer_1 ?? ""}" ${!q?.offer_1 ? "disabled" : ""} type="button">
                  <small>Offer1</small><strong>${q?.offer_1 ? `฿${fmtMoney(q.offer_1)}` : "—"}</strong>
                </button>
              </div>
              <div class="btn-row">
                <button id="nego-lure-btn" class="btn btn-amber ${mode === "lure" ? "pill-sky" : ""}" ${!lureOk ? "disabled" : ""} type="button">
                  🪤 Lure ${lureOk ? `฿${fmtMoney(q.lure_plan.price)}` : `(${esc(q?.lure_plan?.reason || "รอ Quote")})`}
                </button>
                ${matchBtnHtml}
              </div>
              <div class="field-col">
                <label class="field-label" for="nego-custom-px">ตั้งราคาใหม่เอง (ปรับตาม Tick SET)</label>
                <div class="price-stepper">
                  <button id="nego-tick-down" class="btn btn-ghost" type="button">- Tick</button>
                  <input id="nego-custom-px" class="text-input" type="number" step="0.01" min="0.01" value="${esc(customPrice)}" inputmode="decimal">
                  <button id="nego-tick-up" class="btn btn-ghost" type="button">+ Tick</button>
                </div>
              </div>
            </div>
          `;

          bodyEl.querySelectorAll("[data-fill-px]").forEach((box) => {
            box.onclick = () => {
              const val = box.dataset.fillPx;
              if (!val) return;
              mode = "custom";
              customPrice = String(val);
              paintSheet();
            };
          });

          const lureBtn = bodyEl.querySelector("#nego-lure-btn");
          if (lureBtn && lureOk) {
            lureBtn.onclick = () => {
              mode = "lure";
              customPrice = String(q.lure_plan.price);
              paintSheet();
            };
          }

          const matchBtn = bodyEl.querySelector("#nego-match-btn");
          if (matchBtn && matchOk) {
            matchBtn.onclick = () => {
              mode = "match";
              customPrice = String(q.match_plan.price);
              paintSheet();
            };
          }

          const pxInput = bodyEl.querySelector("#nego-custom-px");
          if (pxInput) {
            pxInput.oninput = () => {
              mode = "custom";
              customPrice = pxInput.value;
              const n = Number(customPrice);
              controls.setConfirmEnabled(Number.isFinite(n) && n > 0);
              controls.setConfirmLabel(`✅ กำหนดเอง → ฿${Number.isFinite(n) && n > 0 ? fmtMoney(n) : "—"}`);
            };
          }

          const tickDown = bodyEl.querySelector("#nego-tick-down");
          const tickUp = bodyEl.querySelector("#nego-tick-up");
          const stepTick = async (dir) => {
            const cur = Number((pxInput && pxInput.value) || customPrice || o.price || 0);
            if (!Number.isFinite(cur) || cur <= 0) {
              toast("กรุณาระบุราคาก่อนปรับ Tick", "err");
              return;
            }
            if (tickDown) tickDown.disabled = true;
            if (tickUp) tickUp.disabled = true;
            try {
              const q2 = await miniappApi.getNegoQuote(o.broker, o.symbol, o.side, cur);
              const nxt = dir > 0 ? q2.tick_up : q2.tick_down;
              if (nxt) {
                mode = "custom";
                customPrice = String(nxt);
              }
            } catch (err) {
              toast(`❌ ปรับ Tick ไม่สำเร็จ: ${err.message}`, "err");
            } finally {
              paintSheet();
            }
          };
          if (tickDown) tickDown.onclick = () => stepTick(-1);
          if (tickUp) tickUp.onclick = () => stepTick(1);
        };

        paintSheet();
        miniappApi
          .getNegoQuote(o.broker, o.symbol, o.side, o.price)
          .then((qData) => {
            quote = qData;
            quoteErr = "";
          })
          .catch((err) => {
            quoteErr = err?.message || "ไม่สามารถดึง Quote ได้";
            toast(`❌ ดึงราคาสดไม่สำเร็จ: ${quoteErr}`, "err");
          })
          .finally(() => {
            loadingQuote = false;
            if (controlsRef && bodyEl.isConnected) paintSheet();
          });
      },
      onConfirm: async () => {
        const targetPx =
          mode === "lure" && quote?.lure_plan?.ok
            ? quote.lure_plan.price
            : mode === "match" && quote?.match_plan?.ok
              ? quote.match_plan.price
              : Number(customPrice);
        const res = await miniappApi.executeNego({
          broker: o.broker,
          order_no: o.order_no,
          symbol: o.symbol,
          side: o.side,
          volume: o.balance,
          old_price: o.price,
          mode,
          custom_price: mode === "custom" ? Number(customPrice) : null,
        });
        try {
          await loadData();
        } catch (_ignored) {
          // Amend succeeded; preserve result summary even if refresh fails.
        }
        const noChange = Boolean(res && res.no_change);
        const isDry = Boolean(res && res.dry_run);
        return {
          ok: true,
          kind: noChange ? "warn" : isDry ? "dry" : "ok",
          dryRun: isDry,
          title: noChange
            ? "⏭️ ราคาเดิมตรงเป้าหมายแล้ว"
            : isDry
              ? "🧪 [DRY-RUN] ต่อราคาแล้ว"
              : "✅ แก้ราคาสำเร็จ",
          detail:
            res?.message ||
            `${o.side} ${o.symbol} #${o.order_no}: ฿${fmtMoney(o.price)} → ฿${fmtMoney(targetPx)}`,
          details: [
            { label: "ออเดอร์", value: `#${o.order_no} (${o.side} ${o.symbol})` },
            { label: "ราคา", value: `฿${fmtMoney(o.price)} → ฿${fmtMoney(res?.new_price ?? targetPx)}` },
            { label: "โหมด", value: `${mode.toUpperCase()}${isDry ? " · DRY-RUN" : ""}` },
          ],
          ctas: [{ label: "⏱️ ไปหน้า Schedule", onClick: () => navigate("nego") }],
          closeLabel: "ปิด",
        };
      },
    });
  };

  const paint = () => {
    if (!isCurrent() || !container.isConnected) return;
    if (syncNav) syncNav();
    const d = _state.data;
    const allOrders = (d && d.orders) || [];
    const brokers = (d && d.brokers) || {};
    const downMsgs = Object.values(brokers)
      .filter((b) => b && !b.available)
      .map((b) => `${String(b.broker || "").toUpperCase()}: ${b.error || "Session Down"}`);

    const filtered = allOrders.filter((o) => {
      if (_state.brokerFilter !== "all" && o.broker !== _state.brokerFilter) return false;
      if (_state.statusFilter === "open") return o.kind === "open" || o.kind === "partial";
      if (_state.statusFilter === "matched") return o.kind === "matched";
      return true;
    });

    container.innerHTML = `
      <section class="card orders-filter-card">
        <div class="orders-head-row">
          <div>
            <div class="card-title">📋 Orders (${filtered.length})</div>
            ${_state.lastUpdatedAt ? `<div class="muted" style="font-size:11px">อัปเดตล่าสุด ${_state.lastUpdatedAt}</div>` : ""}
          </div>
          <div class="orders-head-controls">
            <select id="orders-broker-select" class="select-input select-compact" aria-label="กรองตามโบรกเกอร์">
              <option value="all" ${_state.brokerFilter === "all" ? "selected" : ""}>ทุกโบรกฯ</option>
              <option value="kss" ${_state.brokerFilter === "kss" ? "selected" : ""}>KSS</option>
              <option value="lib" ${_state.brokerFilter === "lib" ? "selected" : ""}>LIB</option>
            </select>
            <button id="orders-sync-btn" class="pill-btn pill-sky ${_state.loading ? "is-loading" : ""}" type="button" ${_state.loading ? "disabled aria-busy=\"true\"" : ""}>
              <span class="${_state.loading ? "spin-inline" : ""}">🔄</span> ${_state.loading ? "กำลังซิงค์..." : "รีเฟรช"}
            </button>
          </div>
        </div>
        <div class="seg-bar seg-compact">
          <button class="seg-btn ${_state.statusFilter === "open" ? "active" : ""}" data-sfilter="open" type="button">รอจับคู่ (${(d && d.counts?.open) || 0})</button>
          <button class="seg-btn ${_state.statusFilter === "matched" ? "active" : ""}" data-sfilter="matched" type="button">แมตช์ (${(d && d.counts?.matched) || 0})</button>
          <button class="seg-btn ${_state.statusFilter === "all" ? "active" : ""}" data-sfilter="all" type="button">ทั้งหมด (${(d && d.counts?.total) || 0})</button>
        </div>
      </section>

      ${
        downMsgs.length
          ? `<section class="card card-danger">
               <div class="card-title">⚠️ บางโบรกเกอร์อ่านตารางออเดอร์ไม่ได้</div>
               <div class="muted">${esc(downMsgs.join(" · "))}</div>
               <button class="btn btn-primary" data-jump-brokers="1" type="button">🔑 ไป Login</button>
             </section>`
          : ""
      }

      ${
        _state.error
          ? renderErrorRetryCard({
              title: d ? "รีเฟรชรายการออเดอร์ไม่สำเร็จ" : "อ่านรายการออเดอร์ไม่สำเร็จ",
              message: _state.error,
              retryId: "orders-retry-btn",
            })
          : ""
      }

      ${
        !d
          ? _state.error
            ? ""
            : `<section class="card"><div class="muted">${_state.loading ? "กำลังดึงรายการออเดอร์จาก CDP..." : "ยังไม่มีข้อมูลออเดอร์"}</div></section>`
          : filtered.length === 0
            ? `<section class="card"><div class="muted">ไม่มีรายการออเดอร์ในตัวกรองนี้</div></section>`
            : filtered.map(renderOrderCard).join("")
      }
    `;

    container.querySelectorAll("[data-sfilter]").forEach((btn) => {
      btn.onclick = () => {
        _state.statusFilter = btn.dataset.sfilter;
        paint();
      };
    });
    const brokerSel = container.querySelector("#orders-broker-select");
    if (brokerSel) {
      brokerSel.onchange = () => {
        _state.brokerFilter = brokerSel.value;
        paint();
      };
    }

    const syncBtn = container.querySelector("#orders-sync-btn");
    if (syncBtn) syncBtn.onclick = () => loadData();

    const retryBtn = container.querySelector("#orders-retry-btn");
    if (retryBtn) retryBtn.onclick = () => loadData();

    container.querySelectorAll("[data-jump-brokers]").forEach((btn) => {
      btn.onclick = () => navigate("brokers");
    });

    container.querySelectorAll("[data-nego-order]").forEach((btn) => {
      btn.onclick = () => {
        const o = JSON.parse(btn.dataset.negoOrder);
        openFastNegoSheet(o);
      };
    });

    container.querySelectorAll("[data-cancel-order]").forEach((btn) => {
      btn.onclick = () => {
        const o = JSON.parse(btn.dataset.cancelOrder);
        confirmSheet({
          title: `ยกเลิกออเดอร์ #${o.order_no}?`,
          body: `${o.side} ${o.symbol} จำนวนค้าง ${fmtInt(o.balance)} หุ้น @ ฿${fmtMoney(o.price)} (${String(o.broker).toUpperCase()})`,
          confirmLabel: "ยืนยันยกเลิก",
          danger: true,
          busyTitle: `⏳ กำลังยกเลิกออเดอร์ #${o.order_no}...`,
          onConfirm: async () => {
            const res = await miniappApi.cancelOrder(o.broker, o.order_no, o.symbol);
            try {
              await loadData();
            } catch (_ignored) {
              // Cancel succeeded; preserve clear confirmation summary.
            }
            const isDry = Boolean(res && res.dry_run);
            return {
              ok: true,
              dryRun: isDry,
              title: isDry ? "🧪 [DRY-RUN] ยกเลิกออเดอร์แล้ว" : "✅ ยกเลิกออเดอร์สำเร็จ",
              detail: `#${o.order_no} (${o.side} ${o.symbol} ${fmtInt(o.balance)} หุ้น)`,
              details: [
                { label: "เลขออเดอร์", value: `#${o.order_no}` },
                { label: "หุ้น / ฝั่ง", value: `${o.symbol} · ${o.side}` },
                { label: "จำนวน", value: `${fmtInt(o.balance)} หุ้น` },
                { label: "โบรกเกอร์", value: String(o.broker || "").toUpperCase() },
              ],
              ctas: [{ label: "🔄 รีเฟรช Orders", onClick: () => loadData() }],
              closeLabel: "ปิด",
            };
          },
        });
      };
    });
  };

  const loadData = async () => {
    if (_state.loading) return;
    _state.loading = true;
    _state.error = "";
    paint();
    try {
      _state.data = await miniappApi.getOrders("all");
      _state.lastUpdatedAt = _nowTimeBkk();
      _state.error = "";
    } catch (err) {
      _state.error = err?.message || "เชื่อมต่อไม่สำเร็จ";
      toast(`❌ อ่านออเดอร์ไม่สำเร็จ: ${_state.error}`, "err");
    } finally {
      _state.loading = false;
    }
    paint();

    if (isCurrent() && _state.pendingNegoOrderNo && _state.data) {
      const targetNo = _state.pendingNegoOrderNo;
      _state.pendingNegoOrderNo = null;
      const match = (_state.data.orders || []).find(
        (x) => String(x.order_no) === targetNo && x.cancellable
      );
      if (match) {
        openFastNegoSheet(match);
      }
    }
  };

  if (initialFetch) {
    await loadData();
  } else {
    paint();
  }
}
