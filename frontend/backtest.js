/* Backtest view: strategy settings, running a backtest and showing its results. */

import {
  $, LWC, state, api, postJson, showToast, loadPref, savePref, instrumentInfo, timeframeInfo,
  fmtNumber, fmtPrice, fmtPct, fmtMoney, fmtMoneySigned, fmtTime, fmtTimeShort, formatDay, cssVar,
  baseChartOptions, createCandleChart, ensureData, setProgress, renderNotice, fmtParams, el,
} from './common.js';

const PREFS_KEY = 'td.backtest.v1';
const MAX_TABLE_ROWS = 1000;
const MAX_LOG_ROWS = 2000;

const EVENT_LABELS = {
  signal: 'Signaal', order: 'Order', fill: 'Uitvoering', exit: 'Gesloten',
  skip: 'Overgeslagen', warning: 'Let op', info: 'Info',
};

export async function initBacktest({ selection, setBusy }) {
  const els = {
    form: $('btForm'),
    strategy: $('btStrategy'),
    description: $('btDescription'),
    paramFields: $('btParamFields'),
    capital: $('btCapital'),
    risk: $('btRisk'),
    sizing: $('btSizing'),
    spread: $('btSpread'),
    slippage: $('btSlippage'),
    commission: $('btCommission'),
    financing: $('btFinancing'),
    applyRisk: $('btApplyRisk'),
    resetCosts: $('btResetCosts'),
    run: $('btRun'),
    empty: $('btEmpty'),
    output: $('btOutput'),
    title: $('btTitle'),
    subtitle: $('btSubtitle'),
    warnings: $('btWarnings'),
    kpis: $('btKpis'),
    equityHint: $('btEquityHint'),
    equityLegend: $('btEquityLegend'),
    tradesTitle: $('btTradesTitle'),
    tradesBody: document.querySelector('#btTrades tbody'),
    logTitle: $('btLogTitle'),
    logBody: document.querySelector('#btLog tbody'),
    oosPct: $('btOos'),
    runInfo: $('btRunInfo'),
    oos: $('btOosPanel'),
    oosHint: $('btOosHint'),
    oosBody: document.querySelector('#btOosTable tbody'),
    oosNotes: $('btOosNotes'),
  };

  const strategies = await api('/api/strategies');
  const prefs = loadPref(PREFS_KEY, {});
  let costsSymbol = null;
  let runToken = 0;
  let charts = null;
  let lastResult = null;
  let currency = 'EUR';
  const money = (v) => fmtMoney(v, currency);
  const moneySigned = (v) => fmtMoneySigned(v, currency);

  /* ---------- Settings form ---------- */

  for (const s of strategies) els.strategy.append(new Option(`${s.label} (${s.version})`, s.key));
  const DEFAULT_STRATEGY = 'sma_cross@v1';
  if (strategies.some((s) => s.key === prefs.strategy)) els.strategy.value = prefs.strategy;
  else if (strategies.some((s) => s.key === DEFAULT_STRATEGY)) els.strategy.value = DEFAULT_STRATEGY;

  const currentStrategy = () => strategies.find((s) => s.key === els.strategy.value);

  function renderParams() {
    const strat = currentStrategy();
    els.description.textContent = strat.description;
    const saved = (prefs.params && prefs.params[strat.key]) || {};
    els.paramFields.innerHTML = '';
    for (const p of strat.params) {
      const id = `btParam-${p.name}`;
      const field = document.createElement('div');
      field.className = p.type === 'bool' ? 'field field--check field--wide' : 'field';
      const input = document.createElement('input');
      input.id = id;
      input.dataset.param = p.name;
      input.dataset.type = p.type;
      const label = document.createElement('label');
      label.htmlFor = id;
      label.textContent = p.label;
      const value = saved[p.name] ?? p.default;
      if (p.type === 'bool') {
        input.type = 'checkbox';
        input.checked = Boolean(value);
        field.append(input, label);
      } else {
        input.type = 'number';
        input.inputMode = 'decimal';
        if (p.min !== null) input.min = p.min;
        if (p.max !== null) input.max = p.max;
        input.step = p.step ?? 'any';
        input.value = value;
        field.append(label, input);
      }
      if (p.help) {
        const help = document.createElement('p');
        help.className = 'hint';
        help.textContent = p.help;
        field.append(help);
      }
      els.paramFields.append(field);
    }
  }

  function setCostDefaults(symbol) {
    const inst = instrumentInfo(symbol);
    els.spread.value = inst.costs.spread;
    els.slippage.value = inst.costs.slippage;
    els.commission.value = inst.costs.commission_per_lot;
    els.financing.value = inst.costs.financing_pct;
    document.querySelectorAll('[data-unit-for="price"]').forEach((el) => { el.textContent = `(${inst.quote_currency})`; });
    costsSymbol = symbol;
  }

  /* Costs last used for this instrument, or its defaults. */
  function applyCosts(symbol) {
    setCostDefaults(symbol);
    const saved = prefs.costs && prefs.costs[symbol];
    if (saved) {
      els.spread.value = saved.spread;
      els.slippage.value = saved.slippage;
      els.commission.value = saved.commission_per_lot;
      els.financing.value = saved.financing_pct;
    }
  }

  function restoreForm() {
    renderParams();
    els.capital.value = prefs.capital ?? state.config.account.starting_capital;
    els.risk.value = prefs.risk_pct ?? state.config.account.risk_per_trade_pct;
    els.sizing.value = prefs.sizing_mode ?? 'fractional';
    els.applyRisk.checked = Boolean(prefs.apply_risk);
    els.oosPct.value = prefs.oos_pct ?? 30;
    applyCosts(state.symbol);
  }

  function readNumber(input, label, { min = -Infinity, max = Infinity, positive = false } = {}) {
    const value = Number(input.value.replace(',', '.'));
    if (input.value.trim() === '' || !Number.isFinite(value)) throw fieldError(input, `Vul een getal in bij "${label}".`);
    if (positive && !(value > 0)) throw fieldError(input, `"${label}" moet groter dan 0 zijn: zonder kosten is een backtest te rooskleurig.`);
    if (value < min || value > max) throw fieldError(input, `"${label}" moet tussen ${min} en ${max} liggen.`);
    return value;
  }

  function fieldError(input, message) {
    input.setAttribute('aria-invalid', 'true');
    input.focus();
    return new Error(message);
  }

  function clearInvalid() {
    els.form.querySelectorAll('[aria-invalid]').forEach((node) => node.removeAttribute('aria-invalid'));
  }

  function readForm() {
    clearInvalid();
    const strat = currentStrategy();
    const params = {};
    for (const input of els.paramFields.querySelectorAll('[data-param]')) {
      const p = strat.params.find((x) => x.name === input.dataset.param);
      params[p.name] = p.type === 'bool'
        ? input.checked
        : readNumber(input, p.label, { min: p.min ?? -Infinity, max: p.max ?? Infinity });
    }
    return {
      strategy: strat.key,
      params,
      oos_pct: readNumber(els.oosPct, 'Out-of-sample', { min: 0, max: 50 }),
      ...readCommon(),
    };
  }

  /* Capital, risk and costs: shared with the optimizer and the comparison. */
  function readCommon() {
    return {
      capital: readNumber(els.capital, 'Startkapitaal', { min: 10, max: 100000000 }),
      risk_pct: readNumber(els.risk, 'Risico per trade', { min: 0.1, max: 10 }),
      sizing_mode: els.sizing.value,
      spread: readNumber(els.spread, 'Spread', { positive: true }),
      slippage: readNumber(els.slippage, 'Slippage', { positive: true }),
      commission_per_lot: readNumber(els.commission, 'Commissie', { min: 0 }),
      financing_pct: readNumber(els.financing, 'Financiering', { min: 0, max: 100 }),
      apply_risk: els.applyRisk.checked,
    };
  }

  function rememberForm(form) {
    if (form.strategy) {
      prefs.strategy = form.strategy;
      prefs.params = { ...(prefs.params || {}), [form.strategy]: form.params };
      prefs.oos_pct = form.oos_pct;
    }
    prefs.capital = form.capital;
    prefs.risk_pct = form.risk_pct;
    prefs.sizing_mode = form.sizing_mode;
    prefs.apply_risk = form.apply_risk;
    prefs.costs = {
      ...(prefs.costs || {}),
      [state.symbol]: {
        spread: form.spread, slippage: form.slippage,
        commission_per_lot: form.commission_per_lot, financing_pct: form.financing_pct,
      },
    };
    savePref(PREFS_KEY, prefs);
  }

  els.strategy.addEventListener('change', renderParams);
  els.resetCosts.addEventListener('click', () => {
    setCostDefaults(state.symbol);
    showToast(`Kosten teruggezet naar de standaardwaarden voor ${state.symbol}.`, 'info');
  });
  els.form.addEventListener('submit', (event) => {
    event.preventDefault();
    run();
  });

  restoreForm();

  /* ---------- Running ---------- */

  async function run() {
    let form;
    try {
      form = readForm();
    } catch (err) {
      showToast(err.message);
      return;
    }
    const token = ++runToken;
    const sel = selection();
    rememberForm(form);
    setBusy(true);
    els.run.disabled = true;
    els.run.textContent = 'Bezig…';
    els.output.classList.add('is-loading');
    try {
      const ok = await ensureData(sel, () => token === runToken);
      if (!ok) return;
      setProgress(1, 1, '', 'Backtest berekenen…');
      const result = await postJson('/api/backtest', { ...sel, ...form });
      const candles = await api(`/api/candles?${new URLSearchParams(sel)}`);
      if (token !== runToken) return;
      render(result, candles.candles);
    } catch (err) {
      if (token === runToken) showToast(err.message);
    } finally {
      if (token === runToken) {
        setBusy(false);
        els.run.disabled = false;
        els.run.textContent = 'Backtest starten';
        els.output.classList.remove('is-loading');
      }
    }
  }

  /* ---------- Results ---------- */

  function ensureCharts() {
    if (charts) return charts;
    const equityChart = LWC.createChart($('btEquityChart'), baseChartOptions());
    const accent = cssVar('--accent');
    const equity = equityChart.addSeries(LWC.AreaSeries, {
      lineColor: accent,
      lineWidth: 2,
      topColor: 'rgba(212, 173, 92, 0.22)',
      bottomColor: 'rgba(212, 173, 92, 0.02)',
      priceFormat: { type: 'custom', formatter: (v) => money(v), minMove: 0.01 },
      priceLineVisible: false,
    });
    const drawdown = equityChart.addSeries(LWC.AreaSeries, {
      lineColor: cssVar('--down'),
      lineWidth: 1,
      topColor: 'rgba(220, 100, 80, 0.05)',
      bottomColor: 'rgba(220, 100, 80, 0.30)',
      invertFilledArea: true,
      priceFormat: { type: 'custom', formatter: (v) => `${fmtNumber(v, 1)}%`, minMove: 0.01 },
      priceLineVisible: false,
      lastValueVisible: false,
    }, 1);
    equityChart.panes()[1].setHeight(90);

    equityChart.subscribeCrosshairMove((param) => {
      if (param.time === undefined) { showEquityLegend(null); return; }
      const e = param.seriesData.get(equity);
      const d = param.seriesData.get(drawdown);
      showEquityLegend(e && { time: param.time, equity: e.value, drawdown: d ? d.value : 0 });
    });

    const price = createCandleChart($('btPriceChart'), { volume: false });
    const markers = LWC.createSeriesMarkers(price.candles, []);
    charts = { equityChart, equity, drawdown, price, markers, startLine: null };
    return charts;
  }

  function showEquityLegend(point) {
    if (!point && lastResult) {
      const last = lastResult.equity[lastResult.equity.length - 1];
      const dd = lastResult.drawdown[lastResult.drawdown.length - 1];
      point = last && { time: last.time, equity: last.value, drawdown: dd ? dd.value : 0 };
    }
    els.equityLegend.innerHTML = '';
    if (!point) return;
    const parts = [
      ['', fmtTime(point.time, lastResult.settings.timeframe)],
      ['Vermogen', money(point.equity)],
      ['Drawdown', `${fmtNumber(point.drawdown, 2)}%`],
    ];
    for (const [label, value] of parts) {
      const span = document.createElement('span');
      if (label) span.append(`${label} `);
      const b = document.createElement('b');
      b.textContent = value;
      span.append(b);
      els.equityLegend.append(span);
    }
  }

  function render(result, candles) {
    lastResult = result;
    const s = result.settings;
    const m = result.metrics;
    const inst = instrumentInfo(s.symbol);
    const imported = s.strategy === 'tradingview';
    currency = s.currency || 'EUR';

    els.empty.hidden = true;
    els.output.hidden = false;

    if (imported) {
      els.title.textContent = `${s.strategy_label} · ${s.symbol} ${s.timeframe}`;
      els.subtitle.textContent = `${formatDay(s.start)} – ${formatDay(s.end)} · start ${money(s.capital)}`
        + `${s.capital_inferred ? ' (afgeleid uit het bestand)' : ''} · bedragen in ${currency}`;
    } else {
      els.title.textContent = `${s.strategy_label} ${s.version} · ${s.symbol} ${s.timeframe}`;
      els.subtitle.textContent =
        `${formatDay(s.start)} – ${formatDay(s.end)} · start ${money(s.capital)} · ${fmtNumber(s.risk_pct, 1)}% risico per trade · `
        + `${s.sizing_mode === 'realistic' ? 'hele lots' : 'exacte lotgrootte'} · hefboom 1:${s.leverage} · ${fmtParams(s.params)}`
        + `${s.apply_risk ? ' · met harde risicolimieten' : ''}`;
    }
    renderRunInfo(result);

    renderNotice(els.warnings, result.warnings);
    renderKpis(m, s);
    renderOos(result.oos);
    renderEquity(result, s);
    renderPrice(result, candles, inst);
    renderTrades(result.trades, inst, s.timeframe);
    renderLog(result.events);
  }

  function renderRunInfo(result) {
    els.runInfo.innerHTML = '';
    if (!result.run_id) return;
    const when = result.created_at ? ` op ${fmtTimeShort(result.created_at)}` : '';
    els.runInfo.append(`Opgeslagen als run #${result.run_id}${when}. `);
    const link = el('a', 'link', 'Bekijk alle runs in Historie');
    link.href = '#historie';
    els.runInfo.append(link);
    if (result.strategy_changed) {
      els.runInfo.append(el('span', 'badge badge--warn', 'strategiebestand sindsdien gewijzigd'));
    }
  }

  function renderOos(oos) {
    els.oos.hidden = !oos;
    if (!oos) return;
    els.oosBody.innerHTML = '';
    if (oos.error) {
      els.oosHint.textContent = oos.error;
      renderNotice(els.oosNotes, []);
      return;
    }
    els.oosHint.textContent = `Laatste ${fmtNumber(oos.pct, 0)}% van de periode apart gehouden, vanaf ${fmtTime(oos.split_ts)}. `
      + 'Beide delen starten met hetzelfde kapitaal.';
    const rows = [
      ['Rendement', (m) => fmtPct(m.total_return_pct)],
      ['Max. drawdown', (m) => `−${fmtNumber(m.max_drawdown_pct, 2)}%`],
      ['Sharpe', (m) => (m.sharpe === null ? '—' : fmtNumber(m.sharpe, 2))],
      ['Winrate', (m) => (m.winrate_pct === null ? '—' : `${fmtNumber(m.winrate_pct, 1)}%`)],
      ['Profit factor', (m) => (m.profit_factor === null ? (m.no_losing_trades ? '∞' : '—') : fmtNumber(m.profit_factor, 2))],
      ['Expectancy', (m) => (m.expectancy_r === null ? '—' : `${fmtNumber(m.expectancy_r, 2)} R`)],
      ['Trades', (m) => String(m.trades)],
    ];
    for (const [label, fmt] of rows) {
      const tr = el('tr');
      tr.append(el('th', '', label), el('td', 'num', fmt(oos.in_sample)), el('td', 'num', fmt(oos.out_of_sample)));
      els.oosBody.append(tr);
    }
    renderNotice(els.oosNotes, oos.notes);
  }

  function renderKpis(m, s) {
    const dash = '—';
    const num = (v, d = 2) => (v === null || v === undefined ? dash : fmtNumber(v, d));
    const tone = (v) => (v === null || v === undefined || v === 0 ? '' : v > 0 ? 'is-up' : 'is-down');
    const arrow = (v) => (v > 0 ? '▲ ' : v < 0 ? '▼ ' : '');
    const pf = m.profit_factor !== null ? fmtNumber(m.profit_factor, 2) : (m.no_losing_trades ? '∞' : dash);

    const items = [
      {
        label: 'Totaal rendement', value: `${arrow(m.total_return_pct)}${fmtPct(m.total_return_pct)}`, tone: tone(m.total_return_pct),
        sub: `${money(s.capital)} → ${money(m.final_equity)}`, hero: true,
        tip: 'Hoeveel het startkapitaal is gegroeid of gekrompen, na alle kosten.',
      },
      {
        label: 'Max. drawdown', value: m.max_drawdown_pct ? `−${fmtNumber(m.max_drawdown_pct, 2)}%` : '0%',
        sub: 'grootste daling vanaf top', tip: 'De grootste tussentijdse daling van het vermogen, vanaf het hoogste punt tot het laagste punt daarna.',
      },
      { label: 'Sharpe', value: num(m.sharpe), sub: 'rendement ÷ schommeling', tip: 'Gemiddeld dagrendement gedeeld door de schommeling ervan, op jaarbasis. Boven 1 is goed, boven 2 is uitzonderlijk.' },
      { label: 'Sortino', value: num(m.sortino), sub: 'alleen dalingen als risico', tip: 'Als Sharpe, maar alleen negatieve schommelingen tellen als risico.' },
      {
        label: 'Winrate', value: m.winrate_pct === null ? dash : `${fmtNumber(m.winrate_pct, 1)}%`,
        sub: m.trades ? `${Math.round((m.winrate_pct / 100) * m.trades)} van ${m.trades} trades winstgevend` : 'geen trades',
        tip: 'Percentage trades met winst na kosten.',
      },
      { label: 'Profit factor', value: pf, sub: 'bruto winst ÷ bruto verlies', tip: 'Totale winst van winnende trades gedeeld door het totale verlies van verliezende trades. Boven 1 = winstgevend.' },
      {
        label: 'Aantal trades', value: String(m.trades),
        sub: m.skipped_signals ? `${m.skipped_signals} overgeslagen` : `${num(m.avg_bars_held, 1)} candles gemiddeld open`,
        tip: 'Aantal afgesloten trades. Onder de 30 zijn de cijfers niet betrouwbaar.', warn: m.trades < 30,
      },
      {
        label: 'Gem. trade', value: m.avg_trade === null ? dash : moneySigned(m.avg_trade), tone: tone(m.avg_trade),
        sub: m.avg_win !== null || m.avg_loss !== null ? `winst ${m.avg_win === null ? dash : money(m.avg_win)} · verlies ${m.avg_loss === null ? dash : money(Math.abs(m.avg_loss))}` : '',
        tip: 'Gemiddeld resultaat per trade, na kosten.',
      },
      {
        label: 'Expectancy', value: m.expectancy_r === null ? dash : `${m.expectancy_r >= 0 ? '+' : '−'}${fmtNumber(Math.abs(m.expectancy_r), 2)} R`,
        tone: tone(m.expectancy_r), sub: 'per trade, in risico-eenheden',
        tip: 'Gemiddeld resultaat per trade uitgedrukt in R: 1 R is het bedrag dat je riskeerde tot de stop-loss. +0,3 R betekent gemiddeld 30% van je risico verdiend per trade.',
      },
      {
        label: 'Kosten totaal', value: money(m.costs_total),
        sub: m.costs_total === null ? 'niet bekend' : `${fmtNumber((m.costs_total / s.capital) * 100, 1)}% van startkapitaal`,
        tip: 'Spread, slippage, commissie en financiering samen.',
      },
      {
        label: 'Koers in periode', value: m.buy_hold_pct === null ? dash : fmtPct(m.buy_hold_pct), sub: 'ter vergelijking',
        tip: 'Hoeveel de koers zelf steeg of daalde in deze periode (kopen en vasthouden, zonder hefboom).',
      },
      { label: 'Tijd in de markt', value: m.exposure_pct === null ? dash : `${fmtNumber(m.exposure_pct, 0)}%`, sub: 'met open positie', tip: 'Percentage van de tijd dat er een positie openstond.' },
    ];

    els.kpis.innerHTML = '';
    for (const item of items) {
      const div = document.createElement('div');
      div.className = `kpi${item.hero ? ' kpi--hero' : ''}${item.warn ? ' kpi--warn' : ''}`;
      div.title = item.tip;
      const label = document.createElement('span');
      label.className = 'kpi__label';
      label.textContent = item.label;
      const value = document.createElement('span');
      value.className = `kpi__value ${item.tone || ''}`;
      value.textContent = item.value;
      const sub = document.createElement('span');
      sub.className = 'kpi__sub';
      sub.textContent = item.sub || '';
      div.append(label, value, sub);
      els.kpis.append(div);
    }
  }

  function renderEquity(result, s) {
    const c = ensureCharts();
    c.equity.setData(result.equity);
    c.drawdown.setData(result.drawdown);
    if (c.startLine) c.equity.removePriceLine(c.startLine);
    c.startLine = c.equity.createPriceLine({
      price: s.capital, color: '#6b7285', lineWidth: 1, lineStyle: LWC.LineStyle.Dashed,
      axisLabelVisible: true, title: 'start',
    });
    c.equityChart.timeScale().fitContent();
    els.equityHint.textContent = `In ${currency === 'EUR' ? 'euro' : currency}, na alle kosten. Gestippelde lijn = startkapitaal (${money(s.capital)}).`;
    showEquityLegend(null);
  }

  function renderPrice(result, candles, inst) {
    const c = ensureCharts();
    c.price.setDigits(inst.digits);
    c.price.setData(candles);
    const up = cssVar('--up');
    const down = cssVar('--down');
    const markers = [];
    for (const t of result.trades) {
      const long = t.side === 'long';
      markers.push({
        time: t.entry_ts, position: long ? 'belowBar' : 'aboveBar', shape: long ? 'arrowUp' : 'arrowDown',
        color: long ? up : down, id: `in-${t.id}`,
      });
      markers.push({
        time: t.exit_ts, position: long ? 'aboveBar' : 'belowBar', shape: 'circle',
        color: '#9aa2b4', size: 0.6, id: `out-${t.id}`,
      });
    }
    markers.sort((a, b) => a.time - b.time);
    c.markers.setMarkers(markers);
    c.price.chart.timeScale().fitContent();
  }

  function renderTrades(trades, inst, timeframe) {
    els.tradesTitle.textContent = `Trades (${trades.length})`;
    els.tradesBody.innerHTML = '';
    if (!trades.length) {
      const tr = document.createElement('tr');
      tr.innerHTML = '<td colspan="11" class="table__empty">Geen trades in deze periode. Kijk in het logboek waarom signalen zijn overgeslagen.</td>';
      els.tradesBody.append(tr);
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const t of trades.slice(0, MAX_TABLE_ROWS)) {
      const tr = document.createElement('tr');
      tr.tabIndex = 0;
      tr.dataset.from = t.entry_ts;
      tr.dataset.to = t.exit_ts;
      const cells = [
        [String(t.id), 'num-muted'],
        [t.side === 'long' ? '▲ Long' : '▼ Short', t.side === 'long' ? 'is-up' : 'is-down'],
        [fmtTimeShort(t.entry_ts), ''],
        [fmtPrice(t.entry_price, inst.digits), 'num'],
        [fmtTimeShort(t.exit_ts), ''],
        [fmtPrice(t.exit_price, inst.digits), 'num'],
        [t.lots === null ? '—' : fmtNumber(t.lots, Math.abs(t.lots * 100 - Math.round(t.lots * 100)) > 1e-6 ? 4 : 2), 'num'],
        [t.exit_reason, ''],
        [money(t.costs_total), 'num'],
        [t.r_multiple === null ? '—' : `${t.r_multiple >= 0 ? '+' : '−'}${fmtNumber(Math.abs(t.r_multiple), 2)}`, 'num'],
        [moneySigned(t.pnl), `num ${t.pnl >= 0 ? 'is-up' : 'is-down'}`],
      ];
      for (const [text, cls] of cells) {
        const td = document.createElement('td');
        td.textContent = text;
        if (cls) td.className = cls;
        tr.append(td);
      }
      tr.title = `${t.entry_reason || ''}${t.stop_loss ? ` · stop-loss ${fmtPrice(t.stop_loss, inst.digits)}` : ''}`
        + `${t.take_profit ? ` · take-profit ${fmtPrice(t.take_profit, inst.digits)}` : ''}`;
      fragment.append(tr);
    }
    els.tradesBody.append(fragment);
    if (trades.length > MAX_TABLE_ROWS) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td colspan="11" class="table__empty">Alleen de eerste ${MAX_TABLE_ROWS} trades worden getoond.</td>`;
      els.tradesBody.append(tr);
    }

    const tfSeconds = timeframeInfo(timeframe).seconds;
    const focusTrade = (row) => {
      if (!row || !row.dataset.from) return;
      const pad = tfSeconds * 30;
      charts.price.chart.timeScale().setVisibleRange({ from: Number(row.dataset.from) - pad, to: Number(row.dataset.to) + pad });
      $('btPriceChart').closest('section').scrollIntoView({ behavior: 'smooth', block: 'center' });
    };
    els.tradesBody.onclick = (event) => focusTrade(event.target.closest('tr'));
    els.tradesBody.onkeydown = (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        focusTrade(event.target.closest('tr'));
      }
    };
  }

  function renderLog(events) {
    els.logTitle.textContent = `Logboek (${events.length} regels)`;
    els.logBody.innerHTML = '';
    const fragment = document.createDocumentFragment();
    for (const e of events.slice(0, MAX_LOG_ROWS)) {
      const tr = document.createElement('tr');
      tr.className = `log--${e.kind}`;
      const cells = [fmtTimeShort(e.ts), EVENT_LABELS[e.kind] || e.kind, e.message];
      cells.forEach((text, i) => {
        const td = document.createElement('td');
        td.textContent = text;
        if (i === 1) td.className = 'log__kind';
        tr.append(td);
      });
      fragment.append(tr);
    }
    els.logBody.append(fragment);
    if (events.length > MAX_LOG_ROWS) {
      const tr = document.createElement('tr');
      tr.innerHTML = `<td colspan="3" class="table__empty">Alleen de eerste ${MAX_LOG_ROWS} regels worden getoond.</td>`;
      els.logBody.append(tr);
    }
  }

  /* ---------- Hooks for the toolbar ---------- */

  /* Open a saved run (from Historie). */
  async function showRun(runId) {
    const token = ++runToken;
    setBusy(true);
    els.output.classList.add('is-loading');
    try {
      const result = await api(`/api/runs/${runId}`);
      const s = result.settings;
      const sel = { symbol: s.symbol, timeframe: s.timeframe, start: s.start, end: s.end };
      await ensureData(sel, () => token === runToken);
      const candles = await api(`/api/candles?${new URLSearchParams(sel)}`);
      if (token !== runToken) return;
      render(result, candles.candles);
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } catch (err) {
      if (token === runToken) showToast(err.message);
    } finally {
      if (token === runToken) {
        setBusy(false);
        els.output.classList.remove('is-loading');
      }
    }
  }

  /* Put a strategy with given parameters in the form (e.g. the optimizer's best result). */
  function applyStrategy(key, params) {
    els.strategy.value = key;
    prefs.params = { ...(prefs.params || {}), [key]: params };
    renderParams();
  }

  function common() {
    try {
      const values = readCommon();
      rememberForm(values);
      return values;
    } catch (err) {
      showToast(`${err.message} (instelling op de Backtest-pagina)`);
      return null;
    }
  }

  return {
    run,
    showRun,
    applyStrategy,
    common,
    strategies,
    savedParams: (key) => (prefs.params && prefs.params[key]) || {},
    onShow(params) {
      if (costsSymbol !== state.symbol) applyCosts(state.symbol);
      const runId = params && params.get('run');
      if (runId && String(lastResult?.run_id) !== runId) showRun(runId);
    },
    onSelectionChange() {
      if (costsSymbol !== state.symbol) {
        applyCosts(state.symbol);
        showToast(`Kosten aangepast aan ${state.symbol}.`, 'info');
      }
    },
  };
}
