import { miniappApi } from "../api.js";
import {
  esc,
  fmtInt,
  fmtMoney,
  renderErrorRetryCard,
  renderRunbookReadinessBar,
  runLockedButton,
} from "../utils.js";

let _state = {
  schedule: null,
  loading: false,
  error: "",
  expanded: {},
  rbPreviewOpen: false,
  rbPreviewLoading: false,
  rbPreviewError: "",
  rbPreviewData: null,
};

const NEGO_DONE_STATES = new Set(["AMENDED", "DONE", "NO_CHANGE", "NO_ORDERS", "RUNNING", "MISSED_WINDOW"]);
const RB_DONE_STATES = new Set(["DONE", "NO_EXITS", "RUNNING", "MISSED_WINDOW", "CANCELLED"]);

const STATUS_META = {
  PENDING: { icon: "⏳", text: "รอถึงเวลา", cls: "pill-sky" },
  AUTO_PENDING: { icon: "⏳", text: "รอถึงเวลา", cls: "pill-sky" },
  RUNNING: { icon: "🔄", text: "กำลังรัน", cls: "pill-amber" },
  AMENDED: { icon: "✅", text: "ต่อราคาแล้ว", cls: "pill-green" },
  NO_CHANGE: { icon: "👌", text: "ตรงเป้าแล้ว", cls: "pill-green" },
  NO_ORDERS: { icon: "👌", text: "ไม่มีออเดอร์", cls: "pill-green" },
  DONE: { icon: "✅", text: "ยิง Exit แล้ว", cls: "pill-green" },
  NO_EXITS: { icon: "👌", text: "ไม่มี Exit", cls: "pill-green" },
  DISABLED: { icon: "⏭️", text: "ปิดอยู่", cls: "pill-muted" },
  SKIPPED: { icon: "⏭️", text: "ข้าม", cls: "pill-muted" },
  SKIPPED_STALE_RUNBOOK: { icon: "⚠️", text: "Runbook เก่า", cls: "pill-amber" },
  BLOCKED_NOT_READY: { icon: "⚠️", text: "ระบบไม่พร้อม", cls: "pill-red" },
  BLOCKED_KILL_SWITCH: { icon: "🚨", text: "Kill-Switch", cls: "pill-red" },
  MISSED_WINDOW: { icon: "⌛", text: "เลยช่วงเวลา", cls: "pill-muted" },
  MARKET_CLOSED: { icon: "🌙", text: "ตลาดปิด", cls: "pill-muted" },
  CANCELLED: { icon: "✖️", text: "ยกเลิกแล้ว", cls: "pill-muted" },
  PARTIAL_ERROR: { icon: "⚠️", text: "สำเร็จบางส่วน", cls: "pill-red" },
  ERROR: { icon: "❌", text: "ติด Error", cls: "pill-red" },
  RECORDED: { icon: "✅", text: "บันทึกแล้ว", cls: "pill-green" },
  OK: { icon: "✅", text: "OK", cls: "pill-green" },
  FAILED: { icon: "❌", text: "FAILED", cls: "pill-red" },
};

const RB_PREVIEW_GUARD = {
  READY: { text: "พร้อมส่ง", cls: "pill-green" },
  ADJUSTED_VOL: { text: "ปรับตามพอร์ต", cls: "pill-amber" },
  SKIPPED_ALREADY_ORDERED: { text: "มีออเดอร์แล้ว", cls: "pill-muted" },
  SKIPPED_NOT_HELD: { text: "ไม่มีหุ้นในพอร์ต", cls: "pill-muted" },
  SKIPPED_ODD_LOT: { text: "เศษหุ้น <100", cls: "pill-muted" },
  BROKER_UNAVAILABLE: { text: "Session ไม่พร้อม", cls: "pill-red" },
};

function statusBadge(status, counts = {}, isRunbook = false, postCloseSummary = null, row = null) {
  const st = String(status || "PENDING").toUpperCase();
  const meta = STATUS_META[st] || { icon: "•", text: st, cls: "pill-muted" };
  let detail = meta.text;
  if (row && row.kind === "eod" && st === "RECORDED") {
    detail = row.netEquity != null ? `Net Eq ฿${fmtInt(row.netEquity)}` : "บันทึกแล้ว";
  } else if (st === "AMENDED") {
    detail = `แก้ ${counts.amended || 0}${(counts.no_change || 0) > 0 ? ` · ตรง ${counts.no_change}` : ""}`;
  } else if (postCloseSummary && st === "DONE") {
    detail = `ยกเลิก ${postCloseSummary.cancelled || 0} · คืน SBL ${postCloseSummary.returned || 0}`;
  } else if (isRunbook && st === "DONE") {
    const sent = Number(counts.executed || 0) + Number(counts.adjusted || 0);
    detail = `ส่ง ${sent}`;
  }
  return `<span class="pill ${meta.cls}">${meta.icon} ${esc(detail)}</span>`;
}

function renderRunbookPreviewSection() {
  if (!_state.rbPreviewOpen) return "";
  const p = _state.rbPreviewData;
  if (_state.rbPreviewLoading && !p) {
    return `<div class="muted" style="font-size:12px;margin-top:8px">⏳ กำลังอ่าน positions_ledger.json + เช็กพอร์ตจริง...</div>`;
  }
  if (_state.rbPreviewError) {
    return `<div class="txt-red" style="font-size:12px;margin-top:8px">❌ ${esc(_state.rbPreviewError)}</div>`;
  }
  if (!p) return "";

  const items = Array.isArray(p.items) ? p.items : [];
  return `
    <div style="border-top:1px solid var(--border);margin-top:8px;padding-top:8px">
      <div class="field-row" style="justify-content:space-between;margin-bottom:6px">
        <strong style="font-size:12px">📋 พรีวิว positions_ledger.json (${esc(p.as_of || "—")})</strong>
        <button id="rb-preview-close-btn" class="btn btn-ghost btn-xs" type="button">ซ่อน</button>
      </div>
      ${renderRunbookReadinessBar(p)}
      ${
        !p.ledger_available
          ? `<div class="txt-red" style="font-size:12px">⚠️ ${esc(p.ledger_error || "ไม่พบไฟล์ positions_ledger.json")}</div>`
          : items.length === 0
            ? `<div class="muted" style="font-size:12px">วันนี้ไม่มีรายการ Exit (SELL / COVER) ใน managed KSS / LIB</div>`
            : `<div class="checklist-items" style="margin-top:4px">
                 ${items
                   .map((it) => {
                     const gm = RB_PREVIEW_GUARD[it.guard_status] || { text: it.guard_status, cls: "pill-muted" };
                     const lureStr = it.lure_price != null ? ` @ ฿${fmtMoney(it.lure_price)}` : "";
                     return `
                       <div class="checklist-item-line">
                         <span><strong>${esc(it.symbol)}</strong> ${esc(it.action)} (${esc(String(it.broker || "").toUpperCase())}) · ส่ง <strong>${fmtInt(it.net_send_vol)}</strong>${lureStr}</span>
                         <span class="pill ${gm.cls}">${esc(gm.text)}${it.quote_ok === false ? " (Quote ❌)" : ""}</span>
                       </div>
                     `;
                   })
                   .join("")}
               </div>`
      }
    </div>
  `;
}

function buildUnifiedTimeline(d) {
  if (!d) return [];
  const rows = [];

  const rb = d.runbook_exit;
  if (rb) {
    const q = rb.queue;
    const autoEnabled = rb.auto_enabled !== false;
    const isHoliday = Boolean(rb.is_holiday || rb.market_phase === "HOLIDAY" || rb.market_phase === "WEEKEND");
    const st = isHoliday && !q
      ? "MARKET_CLOSED"
      : q
        ? String(q.status || "PENDING").toUpperCase()
        : autoEnabled
          ? "PENDING"
          : "DISABLED";
    const isDone = RB_DONE_STATES.has(st) || isHoliday;
    const canRunNow = Boolean(
      !isHoliday &&
        (!q || ["PENDING", "AUTO_PENDING", "BLOCKED_NOT_READY", "BLOCKED_KILL_SWITCH", "SKIPPED_STALE_RUNBOOK", "MISSED_WINDOW"].includes(st))
    );
    rows.push({
      id: "runbook_exit",
      kind: "runbook",
      sortKey: "10:00-0",
      period: q ? `${q.window_start || "10:00"}–${q.window_end || "10:30"}` : "10:00–10:30",
      title: "Runbook Auto-Exit",
      tag: { text: "EXIT", cls: "pill-sky" },
      timeText: q?.finished_at ? `เสร็จ ${q.finished_at.slice(0, 5)}` : q?.scheduled_time ? `นัด ${q.scheduled_time}` : "อัตโนมัติ",
      status: st,
      counts: (q && q.counts) || {},
      enabled: autoEnabled,
      canToggle: !isDone,
      isDone,
      isActiveNow: false,
      canRunNow,
      items: (q && Array.isArray(q.items) && q.items) || [],
      error: q?.error || "",
      brokerErrors: (q && q.broker_errors) || {},
      lineStatus: q?.line_status || "",
    });
  }

  const windows = (d.windows || []).filter((w) => w.tradable !== false && w.id !== "w3_lunch");
  windows.forEach((w, idx) => {
    const run = w.run || {};
    const st = String(run.status || (w.enabled ? "PENDING" : "DISABLED")).toUpperCase();
    const isDone = NEGO_DONE_STATES.has(st);
    const startPart = String(w.period || "").slice(0, 5) || `99:${idx}`;
    rows.push({
      id: w.id,
      kind: "nego",
      winMode: w.mode,
      sortKey: `${startPart}-1-${idx}`,
      period: w.period,
      title: w.mode === "post_close" ? w.label : `Auto-Nego · ${w.label}`,
      tag: w.mode === "post_close"
        ? { text: "POST-CLOSE", cls: "pill-green" }
        : w.mode === "match"
          ? { text: "MATCH", cls: "pill-sky" }
          : { text: "LURE", cls: "pill-amber" },
      timeText: run.finished_at ? `เสร็จ ${run.finished_at.slice(0, 5)}` : run.scheduled_time ? `นัด ${run.scheduled_time}` : "—",
      status: st,
      counts: run.counts || {},
      postCloseSummary: run.post_close_summary || null,
      enabled: Boolean(w.enabled),
      canToggle: !isDone,
      isDone,
      isActiveNow: Boolean(w.in_window_now),
      canRunNow: !isDone,
      items: Array.isArray(run.items) ? run.items : [],
      error: run.error || "",
      brokerErrors: run.broker_errors || {},
      lineStatus: run.line_status || "",
    });
  });

  const isMarketClosed = Boolean(
    d.is_weekday === false ||
      (rb && (rb.is_holiday || rb.market_phase === "HOLIDAY" || rb.market_phase === "WEEKEND"))
  );

  const eod = d.eod;
  if (eod) {
    const eodTime = eod.push_time || "17:05";
    const lastRes = eod.last_result && eod.last_result.date === d.daily_date ? eod.last_result : null;
    const todayEq = eod.today_net_equity || null;
    const isDoneToday = Boolean(lastRes || todayEq || eod.last_sent_date === d.daily_date);
    const eodAutoEnabled = eod.enabled !== false;
    const netEq = todayEq?.total_net_equity ?? lastRes?.net_equity ?? null;
    const eodSt = isMarketClosed && !isDoneToday
      ? "MARKET_CLOSED"
      : isDoneToday
        ? "RECORDED"
        : eodAutoEnabled
          ? "PENDING"
          : "DISABLED";
    const execAt = todayEq?.recorded_at || lastRes?.executed_at || "";
    rows.push({
      id: "eod_summary",
      kind: "eod",
      sortKey: `${eodTime}-2`,
      period: eodTime,
      title: "EOD & บันทึก Net Equity",
      tag: { text: "EOD", cls: "pill-sky" },
      timeText: execAt ? `บันทึก ${String(execAt).slice(11, 16) || execAt.slice(0, 5)}` : "อัตโนมัติ (Inbox + CSV)",
      status: eodSt,
      counts: lastRes?.counts || {},
      netEquity: netEq,
      eodData: {
        lastResult: lastRes,
        todayNetEquity: todayEq,
        csvPath: eod.csv_path || "data/net_equity_history.csv",
        linePushEnabled: Boolean(eod.line_push_enabled),
      },
      enabled: eodAutoEnabled,
      canToggle: !isDoneToday && !isMarketClosed,
      isDone: isDoneToday || isMarketClosed,
      isActiveNow: false,
      canRunNow: !isMarketClosed,
      items: [],
      error: "",
      brokerErrors: {},
      lineStatus: "",
    });
  }

  const pipe = d.daily_pipeline;
  if (pipe) {
    const pTime = pipe.scheduled_time || "17:30";
    const rawSt = String(pipe.status || "PENDING").toUpperCase();
    const pSt = isMarketClosed && rawSt === "PENDING" ? "MARKET_CLOSED" : rawSt;
    const pDone = rawSt === "OK" || rawSt === "FAILED" || (isMarketClosed && rawSt === "PENDING");
    const startedStr = pipe.started ? String(pipe.started).replace("T", " ").slice(0, 16) : "";
    rows.push({
      id: "daily_pipeline",
      kind: "pipeline",
      sortKey: `${pTime}-3`,
      period: pTime,
      title: "Daily Pipeline (trading-daily-shadow)",
      tag: { text: "PIPELINE", cls: "pill-muted" },
      timeText: pipe.ran_post_close_today && startedStr
        ? `เสร็จ ${startedStr.slice(11, 16)}`
        : "DailyScanner + Portfolio",
      status: pSt,
      counts: {},
      pipelineData: pipe,
      enabled: true,
      canToggle: false,
      isDone: pDone,
      isActiveNow: false,
      canRunNow: false,
      items: [],
      error: pipe.failed ? `Stage ${pipe.failed_stage || "?"} failed` : "",
      brokerErrors: {},
      lineStatus: "",
    });
  }

  rows.sort((a, b) => a.sortKey.localeCompare(b.sortKey));
  return rows;
}

function renderRowDetails(row) {
  if (!_state.expanded[row.id]) return "";
  if (row.kind === "eod") {
    const info = row.eodData || {};
    const eq = info.todayNetEquity || {};
    const lr = info.lastResult || {};
    const cnt = lr.counts || {};
    const netEq = eq.total_net_equity ?? lr.net_equity ?? null;
    const kssM = eq.kss_margin_net_equity;
    const libC = eq.lib_cash_net_equity;
    const matched = eq.matched_orders ?? cnt.matched ?? 0;
    const open = eq.open_orders ?? cnt.open ?? 0;
    return `
      <div class="checklist-items" style="margin-top:8px">
        <div class="checklist-item-line">
          <span><strong>Net Equity รวม</strong></span>
          <span><strong>${netEq != null ? `฿${fmtMoney(netEq)}` : "ยังไม่มีข้อมูล (รอ CDP / กดรัน)"}</strong></span>
        </div>
        ${
          kssM != null || libC != null
            ? `<div class="checklist-item-line">
                 <span>แยกบัญชี</span>
                 <span class="muted">${kssM != null ? `KSS Margin ฿${fmtInt(kssM)}` : ""}${kssM != null && libC != null ? " · " : ""}${libC != null ? `LIB Cash ฿${fmtInt(libC)}` : ""}</span>
               </div>`
            : ""
        }
        <div class="checklist-item-line">
          <span>สรุปคำสั่ง</span>
          <span class="muted">Matched ${fmtInt(matched)} · ค้าง ${fmtInt(open)}</span>
        </div>
        <div class="checklist-item-line">
          <span>ไฟล์กราฟ (Upsert รายวัน)</span>
          <span class="muted">${esc(info.csvPath || "data/net_equity_history.csv")}</span>
        </div>
        <div class="checklist-item-line">
          <span>แจ้งเตือน LINE</span>
          <span class="muted">${info.linePushEnabled ? "เปิดส่ง LINE OA" : "บันทึก MiniApp Inbox เท่านั้น (ไม่ใช้โควต้า LINE)"}</span>
        </div>
        ${
          row.canRunNow
            ? `<div class="field-row" style="gap:6px;flex-wrap:wrap;margin-top:6px"><button id="eod-sched-run-btn" class="pill-btn pill-amber" type="button">▶ รันตอนนี้</button></div>`
            : ""
        }
      </div>
    `;
  }
  if (row.kind === "pipeline") {
    const p = row.pipelineData || {};
    const stages = Array.isArray(p.stages) ? p.stages : [];
    const lastStart = p.started ? String(p.started).replace("T", " ").slice(0, 19) : "—";
    return `
      <div class="checklist-items" style="margin-top:8px">
        ${row.error ? `<div class="txt-red">❌ ${esc(row.error)}</div>` : ""}
        <div class="checklist-item-line">
          <span>Systemd Timer</span>
          <span class="muted">${esc(p.timer || "trading-daily-shadow.timer")} (จ.-ศ. 08:30 &amp; 17:30)</span>
        </div>
        <div class="checklist-item-line">
          <span>รันล่าสุดใน log</span>
          <span class="muted">${esc(lastStart)}${p.seconds != null ? ` (${Number(p.seconds).toFixed(1)}s)` : ""}</span>
        </div>
        ${
          stages.length
            ? `<div class="field-row" style="gap:6px;flex-wrap:wrap;margin-top:4px">${stages
                .map((s) => `<span class="pill ${s.status === "ok" ? "pill-green" : "pill-red"}">${esc(s.id)}: ${esc(s.status)}</span>`)
                .join("")}</div>`
            : `<div class="muted" style="font-size:11.5px">ยังไม่พบประวัติ stages วันนี้</div>`
        }
      </div>
    `;
  }
  const isRunbook = row.kind === "runbook";
  const brokerErrEntries = Object.entries(row.brokerErrors || {});
  const itemLines = row.items.map((it) => {
    if (isRunbook) {
      const vol = it.executed_volume || it.net_send_vol || it.runbook_volume || 0;
      const px = it.resolved_price != null ? ` @ ฿${fmtMoney(it.resolved_price)}` : "";
      const ord = it.broker_order_no ? ` #${esc(it.broker_order_no)}` : "";
      return `
        <div class="checklist-item-line">
          <span><strong>${esc(it.symbol)}</strong> ${esc(it.action || it.side)} (${esc(String(it.broker || "").toUpperCase())}) · ส่ง ${fmtInt(vol)}${px}${ord}</span>
          <span class="muted">${esc(it.outcome || "PENDING")}${it.reason ? ` — ${esc(it.reason)}` : ""}</span>
        </div>
      `;
    }
    if (it.mode === "post_close" || row.winMode === "post_close") {
      const volStr = it.volume ? ` ${fmtInt(it.volume)} หุ้น` : "";
      const refStr = it.order_no ? ` #${esc(it.order_no)}` : "";
      return `
        <div class="checklist-item-line">
          <span><strong>${esc(it.symbol)}</strong> ${esc(it.side)}${volStr}${refStr} (${esc(String(it.broker || "").toUpperCase())})</span>
          <span class="muted">${esc(it.outcome)}${it.reason ? ` — ${esc(it.reason)}` : ""}</span>
        </div>
      `;
    }
    const px =
      it.outcome === "AMENDED" || it.outcome === "DRY_RUN"
        ? `฿${fmtMoney(it.old_price)} → ฿${fmtMoney(it.new_price)}`
        : `฿${fmtMoney(it.old_price)}`;
    return `
      <div class="checklist-item-line">
        <span><strong>${esc(it.symbol)}</strong> ${esc(it.side)} #${esc(it.order_no)} (${esc(String(it.broker || "").toUpperCase())})</span>
        <span>${px} · <span class="muted">${esc(it.outcome)}${it.reason ? ` — ${esc(it.reason)}` : ""}</span></span>
      </div>
    `;
  });

  const actionBtns = [];
  if (isRunbook) {
    actionBtns.push(`<button id="rb-preview-btn" class="pill-btn pill-sky" type="button">🔍 พรีวิว Runbook</button>`);
    if (row.canRunNow) {
      actionBtns.push(`<button id="rb-run-now-btn" class="pill-btn pill-amber" type="button">▶ รันตอนนี้</button>`);
    }
  } else if (row.canRunNow) {
    actionBtns.push(`<button class="pill-btn pill-amber" data-win-run="${esc(row.id)}" type="button">▶ รันตอนนี้</button>`);
  }

  return `
    <div class="checklist-items" style="margin-top:8px">
      ${row.error ? `<div class="txt-red">❌ ${esc(row.error)}</div>` : ""}
      ${brokerErrEntries.map(([b, m]) => `<div class="txt-amber">⚠️ ${esc(String(b).toUpperCase())}: ${esc(m)}</div>`).join("")}
      ${itemLines.join("")}
      ${row.lineStatus ? `<div class="muted" style="font-size:11px">LINE: ${esc(row.lineStatus)}</div>` : ""}
      ${actionBtns.length > 0 ? `<div class="field-row" style="gap:6px;flex-wrap:wrap;margin-top:6px">${actionBtns.join("")}</div>` : ""}
      ${isRunbook ? renderRunbookPreviewSection() : ""}
    </div>
  `;
}

export async function renderNegoView(container, ctx) {
  const { toast, initialFetch = true, isCurrent = () => true, syncNav = null } = ctx;

  const paint = () => {
    if (!isCurrent() || !container.isConnected) return;
    if (syncNav) syncNav();
    const d = _state.schedule;
    const ksActive = Boolean(d && d.kill_switch && d.kill_switch.is_active);
    const nextRun = d && d.next_run;
    const rows = buildUnifiedTimeline(d);
    const doneCount = rows.filter((r) => r.isDone).length;
    const pendingCount = rows.filter((r) => !r.isDone && r.enabled).length;

    container.innerHTML = `
      <section class="card card-summary">
        <div class="card-head">
          <div>
            <div class="card-title">⏱️ แผน Automation วันนี้ (${esc((d && d.daily_date) || "—")})</div>
            <div class="muted" style="font-size:11.5px">ทำงานอัตโนมัติไม่ต้อง Confirm · กด Toggle เพื่อหยุดงานที่ยังไม่รัน</div>
          </div>
          <div class="field-row" style="gap:8px">
            <button id="nego-reload-btn" class="pill-btn pill-sky" type="button" title="รีเฟรช">🔄</button>
            <label class="switch-label" title="เปิด/ปิดระบบ Auto-Nego ทั้งหมด">
              <input id="nego-master-toggle" class="switch-input" type="checkbox" ${d && d.master_enabled ? "checked" : ""} ${!d ? "disabled" : ""}>
            </label>
          </div>
        </div>
        <div class="meta-row" style="flex-wrap:wrap;gap:6px">
          <span class="pill ${d && d.dry_run ? "pill-amber" : "pill-red"}">${d && d.dry_run ? "🧪 DRY-RUN" : "🔴 LIVE"}</span>
          ${
            !d
              ? `<span class="muted">กำลังโหลด...</span>`
              : !d.master_enabled
                ? `<span>⏸️ ปิดระบบชั่วคราว (Master OFF)</span>`
                : `<span>ทำแล้ว <strong>${doneCount}/${rows.length}</strong> · รอทำ <strong>${pendingCount}</strong>${
                    nextRun
                      ? ` · ถัดไป <strong>${esc(nextRun.scheduled_time)}</strong> (อีก ${nextRun.eta_minutes}น.)`
                      : d.is_weekday
                        ? " · ครบแผนวันนี้แล้ว"
                        : " · วันหยุดเสาร์–อาทิตย์"
                  }</span>`
          }
        </div>
      </section>

      ${
        ksActive
          ? `<section class="card card-danger">
               <div class="card-title">🚨 KILL-SWITCH ACTIVE</div>
               <div class="muted">ระงับ Automation ทั้งหมดจนกว่าจะปลดล็อกที่แท็บ Brokers</div>
             </section>`
          : ""
      }

      ${
        _state.error
          ? renderErrorRetryCard({
              title: "โหลดตาราง Automation ไม่สำเร็จ",
              message: _state.error,
              retryId: "nego-retry-btn",
            })
          : ""
      }

      <section id="schedule-timeline-card" class="card">
        <div class="card-head" style="margin-bottom:6px">
          <div class="card-title">📋 ตารางงานทั้งวัน (10:00 – 17:30)</div>
          <span class="muted" style="font-size:11px">แตะแถวเพื่อดูรายละเอียด</span>
        </div>
        ${
          _state.loading && !d
            ? `<div class="muted">กำลังโหลดตารางเวลา...</div>`
            : rows.length === 0
              ? `<div class="muted">ยังไม่มีรายการตารางเวลา</div>`
              : `<div class="sched-list">
                   ${rows
                     .map((r) => {
                       const rowCls = r.isActiveNow ? "sched-active" : r.isDone ? "sched-closed" : "";
                       const toggleAttr =
                         r.kind === "runbook"
                           ? `id="rb-auto-toggle"`
                           : r.kind === "eod"
                             ? `id="eod-auto-toggle"`
                             : `data-win-toggle="${esc(r.id)}"`;
                       const switchHtml =
                         r.kind === "pipeline"
                           ? `<span class="pill pill-muted" style="font-size:10.5px">Read-only</span>`
                           : `<label class="switch-label" title="${r.canToggle ? "เปิด/ปิดงานนี้" : "งานนี้ทำงานไปแล้ว"}">
                                 <input
                                   class="switch-input"
                                   type="checkbox"
                                   ${toggleAttr}
                                   ${r.enabled ? "checked" : ""}
                                   ${!r.canToggle ? "disabled" : ""}
                                   aria-label="เปิดปิด ${esc(r.title)}"
                                 >
                               </label>`;
                       return `
                         <div class="sched-row ${rowCls}" data-timeline-row="${esc(r.id)}" ${r.kind === "nego" ? `data-window-row="${esc(r.id)}"` : ""}>
                           <div class="sched-row-top" data-toggle-items="${esc(r.id)}" style="cursor:pointer">
                             <div style="min-width:0;flex:1">
                               <div class="field-row" style="gap:6px;flex-wrap:wrap">
                                 <strong>${esc(r.period)}</strong>
                                 <span class="pill ${r.tag.cls}">${esc(r.tag.text)}</span>
                                 <span class="muted" style="font-size:11.5px">${esc(r.timeText)}</span>
                               </div>
                               <div style="font-size:12.5px;margin-top:3px">
                                 ${esc(r.title)} <span class="muted">${_state.expanded[r.id] ? "▾" : "▸"}</span>
                               </div>
                             </div>
                             <div class="field-row" style="gap:8px;flex-shrink:0" onclick="event.stopPropagation()">
                               ${statusBadge(r.status, r.counts, r.kind === "runbook", r.postCloseSummary, r)}
                               ${switchHtml}
                             </div>
                           </div>
                           ${renderRowDetails(r)}
                         </div>
                       `;
                     })
                     .join("")}
                 </div>`
        }
      </section>
    `;

    const reloadBtn = container.querySelector("#nego-reload-btn");
    if (reloadBtn) reloadBtn.onclick = () => runLockedButton(reloadBtn, "⏳", loadSchedule, { toast });

    const retryBtn = container.querySelector("#nego-retry-btn");
    if (retryBtn) retryBtn.onclick = () => runLockedButton(retryBtn, "⏳ โหลด...", loadSchedule, { toast });

    const masterToggle = container.querySelector("#nego-master-toggle");
    if (masterToggle) {
      masterToggle.onchange = async () => {
        const nextVal = Boolean(masterToggle.checked);
        masterToggle.disabled = true;
        try {
          _state.schedule = await miniappApi.toggleNegoSchedule({ window_id: null, enabled: nextVal });
          toast(nextVal ? "✅ เปิด Auto-Nego Scheduler แล้ว" : "⏸️ ปิด Auto-Nego Scheduler ชั่วคราวแล้ว", "ok");
          paint();
        } catch (err) {
          toast(`❌ ตั้งค่าไม่สำเร็จ: ${err.message}`, "err");
          masterToggle.checked = !nextVal;
          masterToggle.disabled = false;
        }
      };
    }

    container.querySelectorAll("[data-win-toggle]").forEach((el) => {
      el.onchange = async () => {
        const wid = el.dataset.winToggle;
        const nextVal = Boolean(el.checked);
        el.disabled = true;
        try {
          _state.schedule = await miniappApi.toggleNegoSchedule({ window_id: wid, enabled: nextVal });
          toast(`${nextVal ? "✅ เปิด" : "⏭️ ปิด"}ช่วงเวลา ${wid} แล้ว`, "ok");
          paint();
        } catch (err) {
          toast(`❌ สลับช่วงเวลาไม่สำเร็จ: ${err.message}`, "err");
          el.checked = !nextVal;
          el.disabled = false;
        }
      };
    });

    container.querySelectorAll("[data-win-run]").forEach((btn) => {
      btn.onclick = () => {
        const wid = btn.dataset.winRun;
        runLockedButton(
          btn,
          "⏳ กำลังรัน...",
          async () => {
            const updated = await miniappApi.runNegoScheduleNow(wid);
            _state.schedule = updated;
            _state.expanded[wid] = true;
            const updatedWin = (updated.windows || []).find((x) => x.id === wid);
            const run = (updatedWin && updatedWin.run) || {};
            const cnt = run.counts || {};
            if (run.status === "ERROR" || run.status === "BLOCKED_KILL_SWITCH") {
              toast(`❌ ${run.error || "รัน Auto-Nego ไม่สำเร็จ"}`, "err");
            } else if (run.post_close_summary) {
              const pc = run.post_close_summary;
              toast(`✅ Post-Close · ยกเลิก ${pc.cancelled || 0} · คืน SBL ${pc.returned || 0} รายการ`, "ok");
            } else {
              toast(`✅ ต่อราคาแล้ว · ตรวจ ${cnt.checked || 0} · แก้ ${cnt.amended || 0}`, "ok");
            }
            paint();
          },
          { toast }
        );
      };
    });

    container.querySelectorAll("[data-toggle-items]").forEach((rowEl) => {
      rowEl.onclick = () => {
        const rid = rowEl.dataset.toggleItems;
        _state.expanded[rid] = !_state.expanded[rid];
        paint();
      };
    });

    const rbAutoToggle = container.querySelector("#rb-auto-toggle");
    if (rbAutoToggle) {
      rbAutoToggle.onchange = async () => {
        const nextVal = Boolean(rbAutoToggle.checked);
        rbAutoToggle.disabled = true;
        try {
          _state.schedule = await miniappApi.toggleRunbookExitAuto(nextVal);
          toast(
            nextVal
              ? "✅ เปิด Runbook Auto-Exit 10:00–10:30 แล้ว"
              : "⏸️ ปิด Runbook Auto-Exit รายวันแล้ว",
            "ok"
          );
          paint();
        } catch (err) {
          toast(`❌ ตั้งค่า Auto-Exit ไม่สำเร็จ: ${err.message}`, "err");
          rbAutoToggle.checked = !nextVal;
          rbAutoToggle.disabled = false;
        }
      };
    }

    const rbPreviewBtn = container.querySelector("#rb-preview-btn");
    if (rbPreviewBtn) {
      rbPreviewBtn.onclick = () =>
        runLockedButton(
          rbPreviewBtn,
          "⏳ กำลังตรวจ...",
          async () => {
            _state.rbPreviewOpen = true;
            _state.rbPreviewLoading = true;
            _state.rbPreviewError = "";
            paint();
            try {
              const res = await miniappApi.getRunbookExits();
              _state.rbPreviewData = res;
              if (_state.schedule && res.schedule) {
                _state.schedule = { ..._state.schedule, runbook_exit: res.schedule };
              }
            } catch (err) {
              _state.rbPreviewError = (err && err.message) || "ดึงข้อมูล Runbook ไม่สำเร็จ";
              toast(`❌ ${_state.rbPreviewError}`, "err");
            } finally {
              _state.rbPreviewLoading = false;
              paint();
            }
          },
          { toast }
        );
    }

    const rbPreviewCloseBtn = container.querySelector("#rb-preview-close-btn");
    if (rbPreviewCloseBtn) {
      rbPreviewCloseBtn.onclick = () => {
        _state.rbPreviewOpen = false;
        paint();
      };
    }

    const rbRunNowBtn = container.querySelector("#rb-run-now-btn");
    if (rbRunNowBtn) {
      rbRunNowBtn.onclick = () =>
        runLockedButton(
          rbRunNowBtn,
          "⏳ กำลังรัน...",
          async () => {
            const updated = await miniappApi.runRunbookExitNow();
            _state.schedule = updated;
            _state.expanded["runbook_exit"] = true;
            const qAfter = (updated.runbook_exit && updated.runbook_exit.queue) || {};
            const cnt = qAfter.counts || {};
            if (qAfter.status === "BLOCKED_NOT_READY" || qAfter.status === "BLOCKED_KILL_SWITCH" || qAfter.status === "ERROR") {
              toast(`⚠️ ${qAfter.error || "ระบบยังไม่พร้อม — ไม่มีการส่งออเดอร์"}`, "err");
            } else if (qAfter.status === "NO_EXITS") {
              toast("👌 วันนี้ไม่มีรายการ Exit ใน Runbook", "ok");
            } else {
              toast(
                `✅ Runbook Exit (${qAfter.status}): ส่ง ${Number(cnt.executed || 0) + Number(cnt.adjusted || 0)} · ข้าม ${cnt.skipped || 0}`,
                "ok"
              );
            }
            paint();
          },
          { toast }
        );
    }

    const eodAutoToggle = container.querySelector("#eod-auto-toggle");
    if (eodAutoToggle) {
      eodAutoToggle.onchange = async () => {
        const nextVal = Boolean(eodAutoToggle.checked);
        eodAutoToggle.disabled = true;
        try {
          _state.schedule = await miniappApi.toggleEodAuto(nextVal);
          toast(
            nextVal
              ? "✅ เปิด EOD 17:05 & บันทึก Net Equity อัตโนมัติแล้ว"
              : "⏸️ ปิด EOD 17:05 อัตโนมัติแล้ว",
            "ok"
          );
          paint();
        } catch (err) {
          toast(`❌ ตั้งค่า EOD ไม่สำเร็จ: ${err.message}`, "err");
          eodAutoToggle.checked = !nextVal;
          eodAutoToggle.disabled = false;
        }
      };
    }

    const eodSchedRunBtn = container.querySelector("#eod-sched-run-btn");
    if (eodSchedRunBtn) {
      eodSchedRunBtn.onclick = () =>
        runLockedButton(
          eodSchedRunBtn,
          "⏳ กำลังบันทึก...",
          async () => {
            const res = await miniappApi.sendEodNow(true);
            _state.expanded["eod_summary"] = true;
            _state.schedule = await miniappApi.getNegoSchedule();
            const netEq = res?.net_equity;
            const c = res?.counts || {};
            toast(
              netEq != null
                ? `✅ บันทึก EOD + Net Eq ฿${fmtInt(netEq)} แล้ว (Matched ${c.matched || 0})`
                : `✅ บันทึก EOD Inbox แล้ว (Matched ${c.matched || 0} · Net Eq ยังไม่อ่านได้)`,
              "ok"
            );
            paint();
          },
          { toast }
        );
    }
  };

  const loadSchedule = async () => {
    _state.loading = true;
    _state.error = "";
    paint();
    try {
      _state.schedule = await miniappApi.getNegoSchedule();
      _state.error = "";
    } catch (err) {
      _state.error = err?.message || "เชื่อมต่อไม่สำเร็จ";
      toast(`❌ โหลดตาราง Automation ไม่สำเร็จ: ${_state.error}`, "err");
    } finally {
      _state.loading = false;
    }
    paint();
  };

  if (initialFetch) {
    await loadSchedule();
  } else {
    paint();
  }
}
