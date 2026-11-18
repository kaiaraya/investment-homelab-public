// SBL Return snippet: inspects active contract, fills return quantity, and submits return
(async (arg) => {
    const clean = s => (s || '').replace(/\u00a0/g, ' ').trim();
    const NO_RETURN = rec => /Save Successfull|บันทึกข้อมูลสำเร็จ/i.test(rec);

    if (!location.href.toLowerCase().includes('sbl') && !document.querySelector('#ctl00_MainCenter_rgTrans_ctl00')) {
        return { success: false, status: 'NOT_ON_SBL_PAGE', error: 'Not on SBL Real Time page', current_url: location.href };
    }

    const rawArg = typeof arg === 'string' ? JSON.parse(arg || '{}') : (arg || {});
    // Accept both the broker ref (B494819) and our DB lot id (SBL-MINT-B494819).
    const targetRefRaw = (rawArg.ref_no || rawArg.ref || rawArg.lot_id || '').trim().toUpperCase();
    const targetRef = targetRefRaw.includes('-') ? targetRefRaw.split('-').pop() : targetRefRaw;
    const targetSymbol = (rawArg.symbol || '').trim().toUpperCase();
    const effectiveRef = targetRef && targetRef !== targetSymbol ? targetRef : '';
    const volume = parseInt(rawArg.volume || rawArg.quantity || rawArg.return_qty || 0, 10);
    const remark = (rawArg.remark || '').trim();
    const dryRun = Boolean(rawArg.dry_run || rawArg.dryRun);

    // 1. Locate matching open transaction in active table (FIFO oldest-first when matching by symbol)
    const dataTable = document.querySelector('#ctl00_MainCenter_rgTrans_ctl00');
    if (!dataTable) {
        return { success: false, status: 'NO_ACTIVE_TRANSACTIONS_TABLE', error: 'Active transactions table not found' };
    }

    const parseDateNum = (d, t) => {
        const dp = (d || '').split(/[/-]/);
        const tp = (t || '00:00:00').split(':');
        if (dp.length === 3 && dp[2].length === 4) {
            return `${dp[2]}${dp[1].padStart(2, '0')}${dp[0].padStart(2, '0')} ${tp.join(':')}`;
        }
        return `${d || '9999-99-99'} ${t || '23:59:59'}`;
    };

    const candidates = [];
    const rows = Array.from(dataTable.querySelectorAll('tbody tr, tr.rgRow, tr.rgAltRow'));
    for (const tr of rows) {
        const cells = Array.from(tr.querySelectorAll('td')).map(c => clean(c.innerText));
        if (cells.length >= 9) {
            const refNo = cells[0].toUpperCase();
            const sym = cells[3].toUpperCase();
            if (!refNo || /no records|ลำดับ/i.test(refNo)) continue;

            const returnBtn = tr.querySelector("a[id*='RadReturn']") ||
                [...tr.querySelectorAll('a,button')].find(b => /คืน|return/i.test(b.textContent || '')) || null;

            const match = (effectiveRef && refNo === effectiveRef) || (!effectiveRef && targetSymbol && sym === targetSymbol);
            if (match) {
                candidates.push({
                    ref_no: refNo,
                    tran_date: cells[1],
                    time: cells[2],
                    symbol: sym,
                    quantity: parseInt(cells[5].replace(/,/g, ''), 10) || 0,
                    borrow_price: parseFloat(cells[6].replace(/,/g, '')) || 0.0,
                    borrow_value: parseFloat(cells[7].replace(/,/g, '')) || 0.0,
                    fee_rate: parseFloat(cells[8]) || 3.5,
                    close_date: cells[9] || '',
                    has_return_button: !!returnBtn,
                    return_button_id: returnBtn ? returnBtn.id : null
                });
            }
        }
    }

    const openCandidates = candidates
        .filter(c => !c.close_date)
        .sort((a, b) => parseDateNum(a.tran_date, a.time).localeCompare(parseDateNum(b.tran_date, b.time)));
    const matchedContract = openCandidates[0] || candidates[0] || null;

    if (!matchedContract) {
        return {
            success: false,
            status: 'CONTRACT_NOT_FOUND',
            error: `No active SBL borrow contract matched ref='${targetRef}' symbol='${targetSymbol}'`
        };
    }

    // A row that already carries a close date is returned/expired: never POST it again.
    if (matchedContract.close_date) {
        return {
            success: true,
            status: 'ALREADY_RETURNED',
            closed: true,
            contract: matchedContract,
            return_qty: 0,
            message: `Contract ${matchedContract.ref_no} (${matchedContract.symbol}) already closed on ${matchedContract.close_date}`
        };
    }

    const returnQty = volume > 0 ? volume : matchedContract.quantity;
    if (returnQty > matchedContract.quantity) {
        return {
            success: false,
            status: 'RETURN_QTY_EXCEEDS_CONTRACT',
            error: `Requested ${returnQty} > contract ${matchedContract.quantity} (${matchedContract.ref_no})`
        };
    }

    // 2. DRY_RUN validation check
    if (dryRun) {
        return {
            success: true,
            status: 'DRY_RUN_ARMED',
            dry_run: true,
            contract: matchedContract,
            return_qty: returnQty,
            message: `[DRY-RUN] Contract ${matchedContract.ref_no} (${matchedContract.symbol}) found with ${matchedContract.quantity} shares. Return held.`
        };
    }

    // 3. LIVE Execution — replay the real Return.aspx WebForm.
    //    The page is a Telerik/ASP.NET WebForms postback: every control also posts a
    //    hidden `<id>_ClientState` JSON blob, and the server reads the numeric value
    //    from THERE. Posting only `txtReturnQty` gets a silent 200 that returns nothing
    //    (verified against the live ledger on 2026-11-06), so serialize the whole form,
    //    patch the two return fields, and let the page answer for itself.
    try {
        const getUrl = `Return.aspx?Ref=${encodeURIComponent(matchedContract.ref_no)}`;
        const pageResp = await fetch(getUrl);
        if (!pageResp.ok) {
            return { success: false, status: 'MODAL_FETCH_FAILED', error: `Failed to fetch ${getUrl}: HTTP ${pageResp.status}` };
        }
        const html = await pageResp.text();
        const doc = new DOMParser().parseFromString(html, 'text/html');
        const form = doc.forms[0];
        if (!form) {
            return { success: false, status: 'MODAL_FORM_MISSING', error: 'Return.aspx had no form to post' };
        }

        const fmt = n => Number(n).toLocaleString('en-US');
        // Reuse the server's own client-state JSON and only move the value fields.
        const patchClientState = (raw, text, numeric) => {
            let st;
            try { st = JSON.parse(raw || '{}'); } catch (e) { st = {}; }
            st.enabled = true;
            if (numeric) {
                st.validationText = String(numeric);
                st.valueAsString = String(numeric);
                st.lastSetTextBoxValue = text;
            } else {
                st.value = text;
            }
            return JSON.stringify(st);
        };

        const qtyText = fmt(returnQty);
        const setField = (id, text, numeric) => {
            const el = doc.getElementById(id);
            if (!el) return false;
            el.value = text;
            const cs = doc.getElementById(id + '_ClientState');
            if (cs) cs.value = patchClientState(cs.value, text, numeric);
            return true;
        };

        if (!setField('txtReturnQty', qtyText, returnQty)) {
            return { success: false, status: 'RETURN_FIELD_MISSING', error: 'txtReturnQty not found on Return.aspx' };
        }
        if (remark) setField('txtRemark', remark, null);
        // Keep the read-only mirrors consistent with the contract row we matched.
        setField('txtSymbol', matchedContract.symbol, null);
        setField('txtRef', matchedContract.ref_no, null);

        const body = new URLSearchParams();
        for (const el of Array.from(form.elements)) {
            if (!el.name) continue;
            const tag = (el.tagName || '').toLowerCase();
            if (tag === 'submit' || tag === 'image' || tag === 'button') continue;
            if (el.name === '__EVENTTARGET' || el.name === '__EVENTARGUMENT') continue;
            body.append(el.name, el.value == null ? '' : el.value);
        }
        body.set('__EVENTTARGET', 'RadSave');
        body.set('__EVENTARGUMENT', '');

        const postResp = await fetch(getUrl, {
            method: 'POST',
            headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
            body: body.toString()
        });
        const respText = await postResp.text();
        const okBanner = (respText.match(/Save\s*Successfull|บันทึกข้อมูลสำเร็จ/i) || [])[0] || '';
        const errHit = (respText.match(/(?:ไม่สามารถ[^<\n]{0,140}|เกิดข้อผิดพลาด[^<\n]{0,140}|Exception[^<\n]{0,140})/i) || [])[0] || '';
        // The broker answers a refused return with HTTP 200 + a Telerik radalert, so the
        // only truthful signal is the dialog text ("...มากกว่าจำนวนที่สามารถคืนได้").
        const alertHit = (respText.match(/radalert\(\s*'([^']{1,180})'/i) || [])[1] || '';
        const reason = (alertHit || errHit || (okBanner ? '' : 'no success banner in response'))
            .replace(/\s+/g, ' ').trim();
        // Shares bought back today are not in custody yet, so the broker's own
        // "returnable" can still be 0 — never retry in a loop, just report it.
        const notReturnable = /มากกว่าจำนวนที่สามารถคืนได้|ไม่สามารถคืน/.test(alertHit);

        // The grid is server-rendered, so a fetch-based POST leaves the table stale.
        // Reload the SBL page (the service session survives) so Python can re-read the
        // ledger instead of trusting this response.
        if (postResp.ok && okBanner) {
            setTimeout(() => { try { location.reload(); } catch (e) {} }, 400);
        }

        return {
            success: postResp.ok && Boolean(okBanner) && !errHit,
            status: postResp.ok
                ? (okBanner ? 'RETURN_ACCEPTED' : (notReturnable ? 'NOT_RETURNABLE' : 'RETURN_REJECTED'))
                : 'RETURN_HTTP_ERROR',
            http_status: postResp.status,
            ref_no: matchedContract.ref_no,
            symbol: matchedContract.symbol,
            return_qty: returnQty,
            banner: okBanner,
            broker_message: alertHit.slice(0, 200),
            response_hint: reason.slice(0, 220),
            verified: false,
            submitted_at: new Date().toISOString()
        };
    } catch (err) {
        return {
            success: false,
            status: 'RETURN_SUBMIT_EXCEPTION',
            ref_no: matchedContract.ref_no,
            error: err.message || String(err)
        };
    }
})()
