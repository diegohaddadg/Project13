// Project13 Dashboard — Real-time monitoring client

const WS_RECONNECT_DELAY = 2000;
let ws = null;

function getWsUrl() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const token = new URLSearchParams(location.search).get('token') || '';
  let url = `${proto}//${location.host}/ws/live`;
  if (token) url += `?token=${token}`;
  return url;
}

function connectWs() {
  if (ws && ws.readyState <= 1) return;
  ws = new WebSocket(getWsUrl());
  ws.onopen = () => {
    document.getElementById('conn-status').textContent = 'live';
    document.getElementById('conn-status').className = 'conn-status connected';
  };
  ws.onclose = () => {
    document.getElementById('conn-status').textContent = 'disconnected';
    document.getElementById('conn-status').className = 'conn-status disconnected';
    setTimeout(connectWs, WS_RECONNECT_DELAY);
  };
  ws.onerror = () => {};
  ws.onmessage = (e) => {
    try { updateDashboard(JSON.parse(e.data)); } catch(err) { console.error(err); }
  };
}

// --- Helpers ---

function fmtTime(s) {
  if (s <= 0) return 'now';
  if (s < 60) return `${s.toFixed(0)}s`;
  if (s < 3600) { const m=Math.floor(s/60); return `${m}m ${Math.floor(s%60)}s`; }
  const h = Math.floor(s/3600), m = Math.floor((s%3600)/60);
  return `${h}h ${m.toString().padStart(2,'0')}m`;
}

function fmtPnl(v) {
  return `$${v >= 0 ? '+' : ''}${v.toFixed(2)}`;
}

function pnlCls(v) {
  return v > 0 ? 'green' : v < 0 ? 'red' : '';
}

const PHASE_LABELS = {
  TRADING: {text: 'LIVE', cls: 'badge-signalable'},
  ACTIVE_WINDOW: {text: 'IN WINDOW', cls: 'badge-active'},
  RESOLVED: {text: 'RESOLVED', cls: 'badge-resolved'},
};

// --- Update functions ---

function updateDashboard(d) {
  updateHeader(d.status);
  updatePrices(d.prices);
  updateMarkets(d.markets);
  updateSignals(d.signals);
  updatePositions(d.positions);
  updatePerformance(d.performance);
  updateRiskAndHealth(d.risk, d.health, d.status);
}

function updateHeader(s) {
  if (!s) return;
  const mb = document.getElementById('mode-badge');
  mb.textContent = s.execution_mode.toUpperCase();
  mb.className = 'badge ' + (s.execution_mode === 'paper' ? 'badge-paper' : 'badge-live');

  const kb = document.getElementById('ks-badge');
  const ksBlocks = s.kill_switch_blocks_trading !== undefined
    ? s.kill_switch_blocks_trading
    : s.kill_switch_active;
  if (ksBlocks) {
    kb.textContent = 'TRADING HALTED';
    kb.className = 'badge badge-kill';
    kb.title = (s.kill_switch_reason || 'Kill switch is blocking new trades').toString();
  } else {
    kb.textContent = 'Trading allowed';
    kb.className = 'badge badge-ok';
    kb.title = 'Kill switch is not blocking (system may still reject trades for risk/health)';
  }

  const hb = document.getElementById('health-badge');
  if (s.warming_up) {
    hb.textContent = 'WARMING UP';
    hb.className = 'badge badge-warn';
  } else {
    hb.textContent = s.system_healthy ? 'HEALTHY' : 'DEGRADED';
    hb.className = 'badge ' + (s.system_healthy ? 'badge-ok' : 'badge-warn');
  }

  // Testing mode badge
  let testBadge = document.getElementById('test-badge');
  if (!testBadge) {
    testBadge = document.createElement('span');
    testBadge.id = 'test-badge';
    testBadge.className = 'badge';
    document.querySelector('.header-left').appendChild(testBadge);
  }
  if (s.testing_mode) {
    testBadge.textContent = 'TESTING';
    testBadge.className = 'badge badge-warn';
    testBadge.style.display = '';
  } else {
    testBadge.style.display = 'none';
  }

  const u = s.uptime_seconds;
  document.getElementById('uptime').textContent =
    `Up ${Math.floor(u/3600)}h ${Math.floor((u%3600)/60)}m`;
}

function updatePrices(p) {
  if (!p) return;
  // Show model spot (Coinbase USD preferred) as the primary price
  const mainPrice = p.model_spot || p.price;
  document.getElementById('big-price').textContent =
    mainPrice ? `$${mainPrice.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}` : '--';
  const gap = p.price_source_gap;
  const gapStr = gap != null ? `Gap: $${gap.toFixed(0)}` : '';
  const gapWarn = gap != null && gap > 25;
  document.getElementById('source-info').textContent =
    p.source ? `Model: ${p.model_source} | ${p.latency_ms ? p.latency_ms.toFixed(0)+'ms' : '--'}${gapStr ? ' | '+gapStr : ''}` : '--';
  document.getElementById('source-info').style.color = gapWarn ? 'var(--yellow)' : '';

  const lEl = document.getElementById('latency');
  if (p.latency_ms != null) {
    lEl.textContent = p.latency_ms.toFixed(0) + 'ms';
    lEl.className = 'metric-value ' + (p.latency_ms < 100 ? 'green' : p.latency_ms < 300 ? 'yellow' : 'red');
  }

  document.getElementById('volatility').textContent = p.volatility ? `$${p.volatility.toFixed(2)}` : 'collecting...';

  const bEl = document.getElementById('binance-status');
  bEl.textContent = p.binance.ok
    ? `$${p.binance.price ? p.binance.price.toLocaleString('en-US',{maximumFractionDigits:0}) : '--'} ${p.binance.tick_rate.toFixed(0)}/s`
    : 'DOWN';
  bEl.className = 'metric-value ' + (p.binance.ok ? 'green' : 'red');

  const cEl = document.getElementById('coinbase-status');
  cEl.textContent = p.coinbase.ok
    ? `$${p.coinbase.price ? p.coinbase.price.toLocaleString('en-US',{maximumFractionDigits:0}) : '--'} ${p.coinbase.tick_rate.toFixed(0)}/s`
    : 'DOWN';
  cEl.className = 'metric-value ' + (p.coinbase.ok ? 'green' : 'red');

  drawSparkline(p.sparkline);
}

function drawSparkline(data) {
  const canvas = document.getElementById('sparkline');
  if (!canvas || !data || data.length < 2) return;
  const ctx = canvas.getContext('2d');
  const W = canvas.parentElement.clientWidth, H = 40;
  canvas.width = W; canvas.height = H;
  const prices = data.map(d => d.p);
  const mn = Math.min(...prices), mx = Math.max(...prices), range = mx-mn||1;
  ctx.clearRect(0,0,W,H);
  ctx.strokeStyle = '#c0a080';
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  for (let i=0; i<prices.length; i++) {
    const x = (i/(prices.length-1))*W;
    const y = H - ((prices[i]-mn)/range)*(H-4) - 2;
    i===0 ? ctx.moveTo(x,y) : ctx.lineTo(x,y);
  }
  ctx.stroke();
}

function renderMarket(el, m) {
  if (!m) {
    el.innerHTML = '<div style="color:var(--text2);padding:8px 0">Searching for active market...</div>';
    return;
  }
  const up_pct = (m.yes_price*100).toFixed(1);
  const dn_pct = (m.no_price*100).toFixed(1);
  const phaseInfo = PHASE_LABELS[m.phase] || PHASE_LABELS.TRADING;

  // Price freshness
  const freshAge = m.updated_ago_s || 0;
  const freshLabel = freshAge < 10 ? 'live' : freshAge < 30 ? `${freshAge.toFixed(0)}s ago` : 'stale';
  const freshCls = freshAge < 10 ? 'green' : freshAge < 30 ? 'yellow' : 'red';

  // Strike display
  const strikeFmt = m.strike_price > 0 ? `$${m.strike_price.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2})}` : 'N/A';
  const strikeHtml = `<span class="metric-value accent">${strikeFmt}</span>`;

  // Pre-window row only for plausible short waits (5m/15m); ignore bogus multi-hour values.
  const ttw = m.time_to_window || 0;
  const tr = m.time_remaining || 0;
  const showPreWindow = !m.window_started && ttw > 0 && ttw <= 3600;
  let timingHtml;
  if (showPreWindow) {
    timingHtml = `
    <div class="metric-row"><span class="metric-label">Observation starts in</span><span class="countdown">${fmtTime(ttw)}</span></div>
    <div class="metric-row"><span class="metric-label">Window ends in</span><span class="countdown${tr<=20?' urgent':''}">${fmtTime(tr)}</span></div>`;
  } else {
    timingHtml = `<div class="metric-row"><span class="metric-label">Window ends in</span><span class="countdown${tr<=20?' urgent':''}">${fmtTime(tr)}</span></div>`;
  }

  // Window progress bar
  let windowBarHtml = '';
  if (m.window_progress != null && m.window_progress > 0) {
    const pct = (m.window_progress * 100).toFixed(0);
    const wp = m.window_phase || 'EARLY';
    const wpCls = wp === 'SNIPER' ? 'red' : wp === 'LATE' ? 'yellow' : 'green';
    const wpBadge = wp === 'SNIPER' ? 'badge-kill' : wp === 'LATE' ? 'badge-warn' : 'badge-ok';
    windowBarHtml = `
      <div class="meter-container" style="margin:6px 0">
        <div class="meter-label"><span>Window</span><span class="badge ${wpBadge}" style="font-size:9px;padding:1px 6px">${wp}</span></div>
        <div class="meter-track"><div class="meter-fill ${wpCls}" style="width:${pct}%"></div></div>
      </div>`;
  }

  // Diagnostics — always show bot's current thinking
  let diagHtml = '';
  const d = m.diagnostics;
  if (d && d.spot != null) {
    const dist = d.distance != null ? `${d.distance >= 0 ? '+' : ''}$${d.distance.toFixed(2)}` : '--';
    const distCls = d.distance > 0 ? 'green' : d.distance < 0 ? 'red' : '';
    const bestDir = d.best_direction || '--';
    const bestEdge = d.best_edge != null ? (d.best_edge * 100).toFixed(2) + '%' : '--';
    const edgeCls = (d.best_edge||0) > 0.08 ? 'positive' : (d.best_edge||0) > 0.02 ? 'neutral' : 'negative';
    const netEv = d.net_ev != null ? d.net_ev.toFixed(4) : '--';
    const netEvCls = (d.net_ev||0) > 0.03 ? 'green' : (d.net_ev||0) > 0 ? 'yellow' : 'red';
    const kellyStr = d.kelly_size != null ? (d.kelly_size * 100).toFixed(1) + '%' : '--';
    const costsStr = d.estimated_costs != null ? (d.estimated_costs * 100).toFixed(2) + '%' : '--';
    const disagree = d.disagreement != null ? (d.disagreement * 100).toFixed(1) + '%' : null;
    const fragile = d.fragile_certainty;
    const dataOnly = d.data_only_15m;
    const reasonsHtml = (d.reasons || []).map(r =>
      `<div style="font-size:10px;color:var(--text2);padding:1px 0">· ${r}</div>`).join('');

    diagHtml = `
      <div style="border-top:1px solid var(--border);margin-top:6px;padding-top:6px">
        <div style="font-size:10px;color:var(--accent);margin-bottom:3px;font-weight:600">Bot Analysis</div>
        <div class="metric-row"><span class="metric-label">Spot → Strike</span><span class="metric-value ${distCls}">${dist}</span></div>
        ${d.model_up != null ? `<div class="metric-row"><span class="metric-label">Model</span><span class="metric-value">Up ${(d.model_up*100).toFixed(1)}% / Dn ${(d.model_down*100).toFixed(1)}%</span></div>` : ''}
        <div class="metric-row"><span class="metric-label">Best Edge</span><span class="edge-badge ${edgeCls}">${bestDir} ${bestEdge}</span></div>
        <div class="metric-row"><span class="metric-label">Net EV</span><span class="metric-value ${netEvCls}">${netEv}</span></div>
        <div class="metric-row"><span class="metric-label">Est. Costs</span><span class="metric-value" style="color:var(--text2)">${costsStr}</span></div>
        <div class="metric-row"><span class="metric-label">Kelly Size</span><span class="metric-value">${kellyStr}</span></div>
        ${disagree ? `<div class="metric-row"><span class="metric-label">Disagreement</span><span class="metric-value ${(d.disagreement||0) > 0.25 ? 'yellow' : ''}">${disagree}</span></div>` : ''}
        ${fragile ? '<div class="metric-row"><span class="metric-label">Fragile Certainty</span><span class="metric-value red">YES</span></div>' : ''}
        ${d.move_5s != null ? `<div class="metric-row"><span class="metric-label">Move 5/10/30s</span><span class="metric-value">\$${d.move_5s.toFixed(1)} / \$${(d.move_10s||0).toFixed(1)} / \$${(d.move_30s||0).toFixed(1)}</span></div>` : ''}
        ${d.urgency_pass != null ? `<div class="metric-row"><span class="metric-label">Urgency</span><span class="metric-value ${d.urgency_pass ? 'green' : 'red'}">${d.urgency_pass ? 'PASS' : 'WEAK'}</span></div>` : ''}
        ${d.lag_proxy_pass != null ? `<div class="metric-row"><span class="metric-label">Lag gate</span><span class="metric-value ${d.proto_latency_gate ? 'green' : 'red'}">${d.proto_latency_gate ? 'PASS' : 'FAIL'} (age ${(d.market_age_ms||0).toFixed(0)}ms)</span></div>` : ''}
        ${d.freshness_pass != null ? `<div class="metric-row"><span class="metric-label">Freshness</span><span class="metric-value ${d.freshness_pass ? 'green' : 'red'}">${d.freshness_pass ? 'FRESH' : 'STALE'} (${d.freshest_window || '?'})</span></div>` : ''}
        ${d.market_phase ? `<div class="metric-row"><span class="metric-label">Phase</span><span class="metric-value">${d.market_phase.toUpperCase()} ${d.phase_would_pass != null ? (d.phase_would_pass ? '' : '<span style="color:var(--yellow)">(would block)</span>') : ''}</span></div>` : ''}
        ${dataOnly ? '<div style="font-size:10px;color:var(--yellow);padding:2px 0;font-weight:600">15min latency_arb: DATA ONLY (paused)</div>' : ''}
        ${reasonsHtml}
      </div>`;
  }

  el.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
      <span class="badge ${phaseInfo.cls}">${phaseInfo.text}</span>
      <span style="font-size:10px" class="${freshCls}">${m.price_source} · ${freshLabel}</span>
    </div>
    <div class="market-bar-container">
      <div class="market-bar">
        <div class="up-bar" style="width:${up_pct}%">Up ${up_pct}%</div>
        <div class="down-bar" style="width:${dn_pct}%">Dn ${dn_pct}%</div>
      </div>
    </div>
    ${windowBarHtml}
    <div class="metric-row"><span class="metric-label">Spread</span><span class="metric-value">${m.spread.toFixed(3)}</span></div>
    <div class="metric-row"><span class="metric-label">Strike</span><span class="metric-value accent">${strikeFmt}</span></div>
    ${timingHtml}
    ${m.question ? '<div style="color:var(--text2);font-size:10px;margin-top:2px;opacity:0.7">' + m.question + '</div>' : ''}
    ${diagHtml}`;
}

function updateMarkets(m) {
  if (!m) return;
  renderMarket(document.getElementById('market-5m-content'), m.btc_5min);
  renderMarket(document.getElementById('market-15m-content'), m.btc_15min);
}

function updateSignals(s) {
  if (!s) return;
  const recent = s.recent_signals || [];
  const tbody = document.getElementById('signal-table');
  const current = document.getElementById('signal-current');

  if (recent.length > 0) {
    const top = recent[0];
    const dirCls = top.direction === 'UP' ? 'dir-up' : 'dir-down';
    current.innerHTML = `<span class="${dirCls}">${top.direction}</span> ${top.market_type} [${top.strategy}] edge=<span class="edge-badge ${top.edge>0?'positive':'negative'}">${(top.edge*100).toFixed(1)}%</span>`;
  } else {
    // Show idle reason
    const reason = s.idle_reason || 'No active signals';
    current.innerHTML = `<span style="color:var(--text2)">${reason}</span>`;
  }

  if (recent.length === 0) {
    tbody.innerHTML = `<tr><td colspan="6" style="color:var(--text2);text-align:center;padding:12px 0">
      ${s.any_signalable ? 'Evaluating live markets — edge below threshold' : 'No active markets discovered'}
    </td></tr>`;
  } else {
    tbody.innerHTML = recent.slice(0, 20).map(sig => {
      const age = ((Date.now()/1000) - sig.timestamp);
      const ageStr = fmtTime(age);
      const dirCls = sig.direction === 'UP' ? 'dir-up' : 'dir-down';
      const edgeCls = sig.edge > 0.08 ? 'positive' : sig.edge > 0 ? 'neutral' : 'negative';
      return `<tr>
        <td>${ageStr}</td>
        <td>${sig.market_type.replace('btc-','')}</td>
        <td>${sig.strategy}</td>
        <td class="${dirCls}">${sig.direction}</td>
        <td><span class="edge-badge ${edgeCls}">${(sig.edge*100).toFixed(1)}%</span></td>
        <td>${sig.confidence}</td>
      </tr>`;
    }).join('');
  }
}

function updatePositions(p) {
  if (!p) return;
  const sc = document.getElementById('starting-capital');
  if (sc && p.starting_capital_usdc != null) sc.textContent = `$${p.starting_capital_usdc.toFixed(2)}`;
  const dep = document.getElementById('deployed-capital');
  if (dep && p.deployed_capital_usdc != null) dep.textContent = `$${p.deployed_capital_usdc.toFixed(2)}`;
  const rp = document.getElementById('realized-pnl');
  if (rp && p.realized_pnl_usdc != null) {
    rp.textContent = fmtPnl(p.realized_pnl_usdc);
    rp.className = 'metric-value ' + pnlCls(p.realized_pnl_usdc);
  }
  document.getElementById('capital').textContent = `$${p.available_capital.toFixed(2)}`;
  // Show total equity if different from available capital
  const eq = p.total_equity || p.available_capital;
  const eqEl = document.getElementById('total-equity');
  if (eqEl) eqEl.textContent = `$${eq.toFixed(2)}`;
  document.getElementById('open-count').textContent = p.open_positions_count || p.open_positions.length;
  document.getElementById('rejected-count').textContent = p.rejected_count;

  document.getElementById('positions-table').innerHTML = p.open_positions.length > 0
    ? p.open_positions.map(pos => {
        const dirCls = pos.direction === 'UP' ? 'dir-up' : 'dir-down';
        return `<tr><td>${pos.market_type.replace('btc-','')}</td><td class="${dirCls}">${pos.direction}</td><td>${pos.entry_price.toFixed(3)}</td><td>${pos.num_shares.toFixed(0)}</td><td>${fmtTime(pos.hold_seconds)}</td></tr>`;
      }).join('')
    : '<tr><td colspan="5" style="color:var(--text2);text-align:center;padding:8px">No open positions</td></tr>';

  document.getElementById('fills-table').innerHTML = p.recent_fills.length > 0
    ? p.recent_fills.slice(-10).reverse().map(f => {
        const age = f.fill_age_seconds != null ? f.fill_age_seconds : ((Date.now()/1000) - f.timestamp);
        const dirCls = f.direction === 'UP' ? 'dir-up' : 'dir-down';
        const pnlStr = f.pnl != null ? fmtPnl(f.pnl) : 'open';
        const link = f.linked_open ? '●' : '·';
        return `<tr><td>${fmtTime(age)}</td><td>${f.market_type.replace('btc-','')}</td><td class="${dirCls}">${f.direction}</td><td>$${f.size_usdc.toFixed(1)}</td><td class="${pnlCls(f.pnl||0)}" title="${f.linked_open ? 'Linked open position' : 'Resolved or flat'}">${link} ${pnlStr}</td></tr>`;
      }).join('')
    : '<tr><td colspan="5" style="color:var(--text2);text-align:center;padding:8px">No fills yet</td></tr>';

  // Closed / resolved positions
  const closedEl = document.getElementById('closed-table');
  if (closedEl) {
    closedEl.innerHTML = (p.recent_closed||[]).length > 0
      ? p.recent_closed.slice(0,5).map(c => {
          const dirCls = c.direction === 'UP' ? 'dir-up' : 'dir-down';
          const pnlStr = c.pnl != null ? fmtPnl(c.pnl) : '--';
          return `<tr><td>${c.market_type.replace('btc-','')}</td><td class="${dirCls}">${c.direction}</td><td>${c.entry_price.toFixed(3)}</td><td class="${pnlCls(c.pnl||0)}">${pnlStr}</td></tr>`;
        }).join('')
      : '<tr><td colspan="4" style="color:var(--text2);text-align:center;padding:6px">No resolved positions yet</td></tr>';
  }

  // Rejection breakdown
  const rejEl = document.getElementById('rejection-detail');
  if (rejEl) {
    const bd = p.rejection_breakdown || {};
    const rejs = p.recent_rejections || [];
    const entries = Object.entries(bd).filter(([k,v]) => v > 0);
    if (entries.length > 0) {
      rejEl.innerHTML = entries.map(([k,v]) =>
        `<span style="color:var(--text2);font-size:10px;margin-right:8px">${k}:${v}</span>`
      ).join('');
    } else {
      rejEl.innerHTML = '<span style="color:var(--text2);font-size:10px">None</span>';
    }
  }
}

function updatePerformance(p) {
  if (!p) return;

  if (p.total_trades === 0) {
    document.getElementById('total-pnl').textContent = '--';
    document.getElementById('total-pnl').className = 'metric-value';
    document.getElementById('total-trades').textContent = '0';
    document.getElementById('win-rate').textContent = '--';
    document.getElementById('win-rate').className = 'metric-value';
    document.getElementById('profit-factor').textContent = '--';
    document.getElementById('best-trade').textContent = '--';
    document.getElementById('best-trade').className = 'metric-value';
    document.getElementById('worst-trade').textContent = '--';
    document.getElementById('worst-trade').className = 'metric-value';
    document.getElementById('sharpe').textContent = '--';
    document.getElementById('max-dd').textContent = '0.0%';
    document.getElementById('strategy-breakdown').innerHTML =
      '<div style="color:var(--text2);font-size:11px;padding:4px 0">Performance metrics will appear after the first resolved trade</div>';
    return;
  }

  document.getElementById('total-pnl').textContent = fmtPnl(p.total_pnl);
  document.getElementById('total-pnl').className = 'metric-value ' + pnlCls(p.total_pnl);
  document.getElementById('total-trades').textContent = p.total_trades;
  document.getElementById('win-rate').textContent = `${(p.win_rate*100).toFixed(0)}%`;
  document.getElementById('win-rate').className = 'metric-value ' + (p.win_rate >= 0.5 ? 'green' : p.win_rate >= 0.3 ? 'yellow' : 'red');
  document.getElementById('profit-factor').textContent = p.profit_factor.toFixed(2);
  document.getElementById('best-trade').textContent = fmtPnl(p.best_trade);
  document.getElementById('best-trade').className = 'metric-value green';
  document.getElementById('worst-trade').textContent = fmtPnl(p.worst_trade);
  document.getElementById('worst-trade').className = 'metric-value red';
  document.getElementById('sharpe').textContent = p.sharpe_ratio.toFixed(2);
  document.getElementById('max-dd').textContent = `${(p.max_drawdown*100).toFixed(1)}%`;

  const bd = p.strategy_breakdown || {};
  const bdEl = document.getElementById('strategy-breakdown');
  if (Object.keys(bd).length > 0) {
    bdEl.innerHTML = '<div style="color:var(--text2);font-size:11px;margin-bottom:4px">By Strategy</div>' +
      Object.entries(bd).map(([k,v]) =>
        `<div class="metric-row"><span class="metric-label">${k}</span><span class="metric-value ${pnlCls(v.total_pnl)}">${v.trades}t ${(v.win_rate*100).toFixed(0)}% ${fmtPnl(v.total_pnl)}</span></div>`
      ).join('');
  } else {
    bdEl.innerHTML = '';
  }
}

function updateRiskAndHealth(r, h, st) {
  if (!r || !h) return;

  const blockers = [...(r.trading_blockers || [])];
  if (st && st.warming_up) blockers.unshift('Feeds warming up');
  if (!h.any_feed_ok) blockers.push('No healthy spot price feed');

  const globalOk = r.trading_allowed !== false && blockers.length === 0;
  const paperWarn = r.paper_warn_only && blockers.length > 0;
  const banner = document.getElementById('trading-status-banner');
  if (banner) {
    if (paperWarn) {
      banner.textContent = 'Paper mode: risk limits breached, continuing for data collection.';
      banner.className = 'status-banner status-block';
    } else if (globalOk) {
      banner.textContent = 'Yes — global gates pass (individual signals may still be rejected for EV, latency, etc.).';
      banner.className = 'status-banner status-ok';
    } else {
      banner.textContent = 'No — blocked until the issues below are cleared.';
      banner.className = 'status-banner status-block';
    }
  }

  const blEl = document.getElementById('trading-blockers');
  if (blEl) {
    blEl.innerHTML = '';
    if (blockers.length === 0) {
      const li = document.createElement('li');
      li.className = 'muted';
      li.textContent = 'None';
      blEl.appendChild(li);
    } else {
      blockers.forEach((b) => {
        const li = document.createElement('li');
        li.textContent = b;
        blEl.appendChild(li);
      });
    }
  }

  const psn = document.getElementById('per-signal-note');
  if (psn) {
    let t = r.per_signal_note || '';
    if (!h.latency_ok) t += ' Feed latency is high — expect signal-level latency rejects.';
    psn.textContent = t.trim();
  }

  const dp = r.daily_pnl;
  const cap = r.daily_limit_usd != null ? r.daily_limit_usd : r.daily_limit;
  const dlp = r.daily_loss_limit_pct != null ? r.daily_loss_limit_pct : 0.15;
  const eq = r.total_equity != null ? r.total_equity : 0;
  const lh = r.limits_headroom || {};
  const dailyRoom = lh.daily_headroom_usdc != null ? lh.daily_headroom_usdc : cap + dp;

  const elDaily = document.getElementById('risk-daily-pnl');
  if (elDaily) {
    const sessEq = r.session_start_equity != null ? r.session_start_equity : eq;
    elDaily.textContent = `${dp >= 0 ? '+' : ''}${dp.toFixed(2)} USD vs halt −${cap.toFixed(2)} USD (${(dlp * 100).toFixed(0)}% of $${sessEq.toFixed(2)} session start)`;
    elDaily.className = 'metric-value ' + pnlCls(dp);
  }
  const elRoom = document.getElementById('risk-daily-room');
  if (elRoom) {
    const roomStr = `${dailyRoom >= 0 ? dailyRoom.toFixed(2) : '0.00'} USD until daily loss halt (${(dlp * 100).toFixed(0)}% / $${cap.toFixed(2)} cap)`;
    elRoom.textContent = roomStr;
    elRoom.className = 'metric-value ' + (dailyRoom <= cap * 0.15 ? 'red' : dailyRoom <= cap * 0.35 ? 'yellow' : 'green');
  }

  const elDd = document.getElementById('risk-dd-text');
  if (elDd) {
    const hdr = lh.drawdown_headroom_pct != null ? lh.drawdown_headroom_pct : 0;
    const ddu = r.drawdown_usd != null ? r.drawdown_usd : 0;
    const ddl = r.drawdown_max_loss_usd != null ? r.drawdown_max_loss_usd : 0;
    elDd.textContent = `${(r.drawdown_pct * 100).toFixed(1)}% ($${ddu.toFixed(2)} below HWM) vs max ${(r.drawdown_limit * 100).toFixed(0)}% (≈$${ddl.toFixed(2)} from peak) — ${(hdr * 100).toFixed(1)}% headroom`;
    elDd.className = 'metric-value ' + (hdr < 0.02 ? 'red' : hdr < 0.05 ? 'yellow' : 'green');
  }

  const elExp = document.getElementById('risk-exp-text');
  if (elExp) {
    const ehr = lh.exposure_headroom_pct != null ? lh.exposure_headroom_pct : 0;
    elExp.textContent = `${(r.exposure_pct * 100).toFixed(0)}% deployed vs ${(r.exposure_limit * 100).toFixed(0)}% max — ${(ehr * 100).toFixed(0)}% headroom`;
    elExp.className = 'metric-value ' + (ehr < 0.05 ? 'red' : ehr < 0.15 ? 'yellow' : 'green');
  }

  const cl = document.getElementById('consec-losses');
  if (cl) {
    cl.textContent = `${r.consecutive_losses} / ${r.max_consecutive} (then cooldown)`;
    cl.className = 'metric-value ' + (r.consecutive_losses >= r.max_consecutive ? 'red' : r.consecutive_losses >= 2 ? 'yellow' : 'green');
  }

  const cd = document.getElementById('risk-cooldown');
  if (cd) {
    const rem = r.cooldown_remaining_s || 0;
    cd.textContent = rem > 0 ? fmtTime(rem) : '—';
    cd.className = 'metric-value ' + (rem > 0 ? 'yellow' : '');
  }

  const rj = document.getElementById('risk-rej');
  if (rj) rj.textContent = String(r.risk_rejections != null ? r.risk_rejections : 0);

  const components = [
    ['Binance', h.binance_ok],
    ['Coinbase', h.coinbase_ok],
    ['Polymarket', h.polymarket_ok],
    ['Latency', h.latency_ok],
    ['Volatility data', h.volatility_available],
  ];
  document.getElementById('health-list').innerHTML = components.map(([name, ok]) =>
    `<div class="health-item"><div class="health-dot ${ok ? 'ok' : 'err'}"></div>${name}</div>`
  ).join('');

  const warnings = h.warnings || [];
  document.getElementById('warnings-list').innerHTML = warnings.length > 0
    ? warnings.map(w => `<div class="health-warn">! ${w}</div>`).join('')
    : '<div class="health-ok-note">No feed warnings</div>';
}

// --- Kill Switch ---

async function activateKillSwitch() {
  if (!confirm('ACTIVATE KILL SWITCH?\n\nThis will immediately halt all new trading.\nRecovery requires manual intervention.\n\nAre you sure?')) return;
  if (!confirm('FINAL CONFIRMATION: Activate kill switch NOW?')) return;

  try {
    const token = new URLSearchParams(location.search).get('token') || '';
    const headers = {'X-Confirm': 'KILL', 'Content-Type': 'application/json'};
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const res = await fetch('/api/kill-switch/activate', {method: 'POST', headers});
    const data = await res.json();
    alert(res.ok ? 'Kill switch ACTIVATED: ' + (data.reason||'Activated') : 'Failed: ' + (data.detail||'Error'));
  } catch(e) {
    alert('Error: ' + e.message);
  }
}

// --- Init ---
connectWs();
