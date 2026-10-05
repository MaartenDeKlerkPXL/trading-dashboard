/* Paper trading view: start sessions, follow them live, compare with the backtest, evaluate. */

import {
  $, LWC, state, api, postJson, showToast, fmtNumber, fmtPrice, fmtPct, fmtEur, fmtEurSigned, fmtTime, fmtTimeShort,
  fmtClock, baseChartOptions, renderNotice, el, fmtParams, SERIES_COLORS, cssVar,
} from './common.js';

const POLL_MS = 15000;
const EVENT_LABELS = {
  signal: 'Signaal', order: 'Order', fill: 'Uitvoering', exit: 'Gesloten', skip: 'Overgeslagen',
  warning: 'Let op', info: 'Info', error: 'Fout', risk: 'Risicoregel',
};
const STATUS_ICON = { pass: '✓', fail: '✗', pending: '…' };
const STATUS_TEXT = { pass: 'voldaan', fail: 'niet voldaan', pending: 'nog te vroeg' };

export function initPaper({ selection, backtest, navigate }) {
  const els = {
    pulse: $('ppPulse'),
    loopTitle: $('ppLoopTitle'),
    loopText: $('ppLoopText'),
    tick: $('ppTick'),
    awake: $('ppAwake'),
    error: $('ppError'),
    newBox: $('ppNew'),
    form: $('ppForm'),
    newHint: $('ppNewHint'),
    strategy: $('ppStrategy'),
    params: $('ppParams'),
    start: $('ppStart'),
    cards: $('ppCards'),
    detail: $('ppDetail'),
    title: $('ppTitle'),
    subtitle: $('ppSubtitle'),
    blocked: $('ppBlocked'),
    kpis: $('ppKpis'),
    legend: $('ppLegend'),
    chartLegend: $('ppChartLegend'),
    compareBody: document.querySelector('#ppCompare tbody'),
    match: $('ppMatch'),
    criteria: $('ppCriteria'),
    criteriaEdit: $('ppCriteriaEdit'),
    criteriaRows: $('ppCriteriaRows'),
    addCriterion: $('ppAddCriterion'),
    saveCriteria: $('ppSaveCriteria'),
    lockCriteria: $('ppLockCriteria'),
    evalHint: $('ppEvalHint'),
    decision: $('ppDecision'),
    notes: $('ppNotes'),
    notesSaved: $('ppNotesSaved'),
    tradesTitle: $('ppTradesTitle'),
    tradesBody: document.querySelector('#ppTrades tbody'),
    logTitle: $('ppLogTitle'),
    logFilter: $('ppLogFilter'),
    logBody: document.querySelector('#ppLog tbody'),
  };

  let selectedId = null;
  let sessions = [];
  let chart = null;
  let lines = null;
  let lastEvents = [];
  let evaluation = null;
  let timer = null;
  let polling = false;

  /* ---------- Start form ---------- */

  for (const s of backtest.strategies) els.strategy.append(new Option(`${s.label} (${s.version})`, s.key));
  els.strategy.value = backtest.strategies.some((s) => s.key === 'sma_cross@v1') ? 'sma_cross@v1' : backtest.strategies[0].key;

  function renderParams() {
    const strat = backtest.strategies.find((s) => s.key === els.strategy.value);
    const saved = backtest.savedParams(strat.key);
    els.params.innerHTML = '';
    for (const p of strat.params) {
      const id = `ppParam-${p.name}`;
      const field = el('div', p.type === 'bool' ? 'field field--check field--wide' : 'field');
      const input = el(p.type === 'choice' ? 'select' : 'input');
      input.id = id;
      input.dataset.param = p.name;
      const label = el('label', '', p.label);
      label.htmlFor = id;
      const value = saved[p.name] ?? p.default;
      if (p.type === 'choice') {
        for (const option of p.choices) {
          const opt = document.createElement('option');
          opt.value = option;
          opt.textContent = option;
          input.append(opt);
        }
        input.value = p.choices.includes(value) ? value : p.default;
        field.append(label, input);
      } else if (p.type === 'bool') {
        input.type = 'checkbox';
        input.checked = Boolean(value);
        field.append(input, label);
      } else {
        input.type = 'number';
        input.step = p.step ?? 'any';
        if (p.min !== null) input.min = p.min;
        if (p.max !== null) input.max = p.max;
        input.value = value;
        field.append(label, input);
      }
      els.params.append(field);
    }
    updateNewHint();
  }

  function updateNewHint() {
    const sel = selection();
    els.newHint.textContent = `Draait op ${sel.symbol} ${sel.timeframe} (kies hierboven). Kapitaal, risico, lotgrootte en kosten `
      + 'komen van de Backtest-pagina. Vanaf het starten ligt deze versie van de strategie vast.';
  }

  function readParams() {
    const strat = backtest.strategies.find((s) => s.key === els.strategy.value);
    const params = {};
    for (const input of els.params.querySelectorAll('[data-param]')) {
      const p = strat.params.find((x) => x.name === input.dataset.param);
      if (p.type === 'bool') params[p.name] = input.checked;
      else if (p.type === 'choice') params[p.name] = input.value;
      else {
        const v = Number(String(input.value).replace(',', '.'));
        if (input.value === '' || !Number.isFinite(v)) throw new Error(`Vul een getal in bij "${p.label}".`);
        params[p.name] = v;
      }
    }
    return params;
  }

  els.strategy.addEventListener('change', renderParams);
  els.form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const common = backtest.common();
    if (!common) return;
    let params;
    try {
      params = readParams();
    } catch (err) {
      showToast(err.message);
      return;
    }
    const sel = selection();
    els.start.disabled = true;
    try {
      const s = await postJson('/api/paper/sessions', {
        symbol: sel.symbol, timeframe: sel.timeframe, strategy: els.strategy.value, params,
        capital: common.capital, risk_pct: common.risk_pct, sizing_mode: common.sizing_mode,
        spread: common.spread, slippage: common.slippage, commission_per_lot: common.commission_per_lot,
        financing_pct: common.financing_pct,
      });
      showToast(`Paper trading gestart: ${s.strategy_label} op ${s.symbol} ${s.timeframe}. De eerste beslissing volgt als de huidige candle sluit.`, 'info');
      els.newBox.open = false;
      navigate('paper', { id: s.id });
      refresh(true);
    } catch (err) {
      showToast(err.message);
    } finally {
      els.start.disabled = false;
    }
  });

  renderParams();

  /* ---------- Loop status and session cards ---------- */

  async function refresh(force = false) {
    if (polling && !force) return;
    if (document.body.dataset.view !== 'paper') return;
    polling = true;
    try {
      const [status, list] = await Promise.all([api('/api/paper/status'), api('/api/paper/sessions')]);
      sessions = list;
      renderStatus(status);
      renderCards();
      if (selectedId) await loadDetail();   // also live sessions, which have their own page
    } catch (err) {
      renderNotice(els.error, [err.message]);
    } finally {
      polling = false;
    }
  }

  function renderStatus(st) {
    const running = st.sessions_running;
    let title;
    let text;
    els.pulse.className = 'pulse';
    if (st.mode !== 'paper') {
      title = 'Paper trading staat uit';
      text = `In config.toml staat execution.mode = "${st.mode}". Zet het op "paper" en herstart het dashboard.`;
      els.pulse.classList.add('is-off');
    } else if (!running) {
      title = 'Geen paper-strategieën actief';
      text = `De loop staat klaar en haalt elke ${st.interval} seconden koersen op zodra er een strategie draait.`;
      els.pulse.classList.add('is-idle');
    } else {
      title = `${running} paper-strateg${running === 1 ? 'ie' : 'ieën'} actief`;
      text = `Koersen elke ${st.interval} s via Dukascopy (enkele minuten vertraging). `
        + (st.last_tick_at ? `Laatst bijgewerkt om ${fmtClock(new Date(st.last_tick_at * 1000))}.` : 'Eerste update volgt zo.');
      els.pulse.classList.add(st.errors_in_a_row ? 'is-warn' : 'is-on');
    }
    els.loopTitle.textContent = title;
    els.loopText.textContent = text;
    els.awake.textContent = st.keep_awake.active
      ? 'Deze Mac blijft wakker zolang er een paper-strategie draait.'
      : st.keep_awake.enabled ? '' : (st.keep_awake.requested ? '(Wakker houden werkt alleen op macOS.)' : '');
    renderNotice(els.error, st.last_error ? [`Probleem bij de laatste update: ${st.last_error}`] : []);
  }

  function renderCards() {
    els.cards.innerHTML = '';
    if (!sessions.length) {
      const empty = el('div', 'panel bt-empty');
      empty.append(el('p', 'bt-empty__title', 'Nog geen paper-strategieën'),
        el('p', 'bt-empty__text', 'Open "Nieuwe paper-strategie starten" hierboven om met nepgeld en live koersen te beginnen.'));
      els.cards.append(empty);
      return;
    }
    for (const s of sessions) els.cards.append(card(s));
  }

  function card(s) {
    const div = el('article', `pp-card${s.id === selectedId ? ' is-selected' : ''} pp-card--${s.status}`);
    const head = el('header', 'pp-card__head');
    head.append(el('h3', 'pp-card__title', `${s.strategy_label} ${s.version}`), el('span', `status status--${s.status}`, s.status_label));
    div.append(head);
    div.append(el('p', 'pp-card__market', `${s.symbol} ${s.timeframe} · sinds ${fmtTimeShort(s.started_at)}`));

    const ret = el('p', `pp-card__return ${s.return_pct >= 0 ? 'is-up' : 'is-down'}`, `${s.return_pct >= 0 ? '▲' : '▼'} ${fmtPct(s.return_pct)}`);
    const equity = el('p', 'pp-card__equity', `${fmtEur(s.equity)} · ${s.trades} trade${s.trades === 1 ? '' : 's'}`);
    div.append(ret, equity);

    const pos = s.position;
    const inst = state.config.instruments.find((i) => i.symbol === s.symbol);
    div.append(el('p', 'pp-card__position', pos
      ? `${pos.side === 'long' ? '▲ Long' : '▼ Short'} ${fmtNumber(pos.lots, pos.lots < 0.1 ? 4 : 2)} lot op ${fmtPrice(pos.entry_price, inst.digits)} · stop ${fmtPrice(pos.stop_loss, inst.digits)}`
      : (s.pending_orders ? 'Order wacht op de volgende koers' : 'Geen open positie')));
    if (s.last_price !== null && s.last_price !== undefined) {
      div.append(el('p', 'pp-card__price', `Laatste koers ${fmtPrice(s.last_price, inst.digits)} om ${fmtTimeShort(s.last_price_ts)}`));
    }
    if (s.status_reason) div.append(el('p', 'pp-card__reason', s.status_reason));

    const actions = el('div', 'pp-card__actions');
    const details = el('button', 'btn btn--small btn--ghost', s.id === selectedId ? 'Geopend' : 'Details');
    details.type = 'button';
    details.addEventListener('click', () => navigate('paper', { id: s.id }));
    actions.append(details);
    if (s.status === 'running' || s.status === 'paused') {
      const toggle = el('button', 'btn btn--small btn--ghost', s.status === 'running' ? 'Pauzeren' : 'Hervatten');
      toggle.type = 'button';
      toggle.addEventListener('click', () => act(s.id, s.status === 'running' ? 'pause' : 'resume'));
      actions.append(toggle);
    }
    if (s.status !== 'stopped') {
      const stop = el('button', 'btn btn--small btn--danger-ghost', 'Stoppen');
      stop.type = 'button';
      let armed = null;
      stop.addEventListener('click', () => {
        if (!armed) {
          stop.textContent = s.position ? 'Zeker? Sluit positie' : 'Zeker? Definitief';
          stop.classList.add('is-armed');
          armed = setTimeout(() => { armed = null; stop.textContent = 'Stoppen'; stop.classList.remove('is-armed'); }, 3000);
          return;
        }
        clearTimeout(armed);
        act(s.id, 'stop');
      });
      actions.append(stop);
    }
    div.append(actions);
    return div;
  }

  async function act(id, action) {
    try {
      await postJson(`/api/paper/sessions/${id}/${action}`, {});
      await refresh(true);
    } catch (err) {
      showToast(err.message);
    }
  }

  els.tick.addEventListener('click', async () => {
    els.tick.disabled = true;
    els.tick.textContent = 'Bezig…';
    try {
      await postJson('/api/paper/tick', {});
      await refresh(true);
    } catch (err) {
      showToast(err.message);
    } finally {
      els.tick.disabled = false;
      els.tick.textContent = 'Nu bijwerken';
    }
  });

  /* ---------- Detail ---------- */

  async function loadDetail() {
    const id = selectedId;
    const [d, cmp] = await Promise.all([api(`/api/paper/sessions/${id}`), api(`/api/paper/sessions/${id}/compare`)]);
    if (id !== selectedId) return;
    renderDetail(d, cmp);
    if (!evaluation || evaluation.sessionId !== id) await loadEvaluation();
  }

  function renderDetail(d, cmp) {
    els.detail.hidden = false;
    const where = d.mode === 'live' ? `LIVE (${d.broker && d.broker.is_live ? 'echt geld' : 'demo'}) · ` : '';
    els.title.textContent = `${where}${d.strategy_label} ${d.version} · ${d.symbol} ${d.timeframe}`;
    els.subtitle.textContent = `Gestart ${fmtTimeShort(d.started_at)} · start ${fmtEur(d.settings.capital)} · `
      + `${fmtNumber(d.settings.risk_pct, 1)}% risico per trade · ${d.settings.sizing_mode === 'realistic' ? 'hele lots' : 'exacte lotgrootte'} · ${fmtParams(d.params)}`;
    renderNotice(els.blocked, d.status === 'blocked' ? [`Geblokkeerd: ${d.status_reason}. Start een nieuwe sessie met een nieuwe versie.`] : []);

    const m = d.metrics;
    const kpis = [
      ['Vermogen', fmtEur(d.equity), `${fmtPct(d.return_pct)} sinds start`, d.return_pct],
      ['Max. drawdown', `−${fmtNumber(m.max_drawdown_pct, 2)}%`, 'grootste daling vanaf top'],
      ['Trades', String(m.trades), m.winrate_pct === null ? 'nog geen afgesloten trades' : `winrate ${fmtNumber(m.winrate_pct, 1)}%`],
      ['Gem. trade', m.avg_trade === null ? '—' : fmtEurSigned(m.avg_trade), 'na kosten', m.avg_trade],
    ];
    els.kpis.innerHTML = '';
    for (const [label, value, sub, tone] of kpis) {
      const k = el('div', 'kpi');
      k.append(el('span', 'kpi__label', label),
        el('span', `kpi__value ${tone > 0 ? 'is-up' : tone < 0 ? 'is-down' : ''}`, value), el('span', 'kpi__sub', sub));
      els.kpis.append(k);
    }

    renderChart(cmp);
    renderCompareTable(cmp);
    const mt = cmp.matching;
    els.match.textContent = mt.reference_trades || mt.our_trades
      ? `Trade voor trade: ${mt.matched} van ${Math.max(mt.reference_trades, mt.our_trades)} trades komen overeen met de backtest`
        + ` (${fmtNumber(mt.match_pct, 0)}%). Alleen in de backtest: ${mt.only_reference.length}, alleen in paper: ${mt.only_ours.length}.`
      : 'Nog geen afgesloten trades om te vergelijken.';
    renderTrades(d.trades, d.symbol);
    lastEvents = d.events;
    renderLog();
  }

  function ensureChart() {
    if (chart) return;
    chart = LWC.createChart($('ppChart'), baseChartOptions());
    const pct = { type: 'custom', formatter: (v) => `${fmtNumber(v, 2)}%`, minMove: 0.01 };
    lines = {
      paper: chart.addSeries(LWC.LineSeries, { color: SERIES_COLORS[0], lineWidth: 2, priceFormat: pct, priceLineVisible: false, title: 'paper' }),
      backtest: chart.addSeries(LWC.LineSeries, { color: SERIES_COLORS[1], lineWidth: 2, lineStyle: LWC.LineStyle.Dashed, priceFormat: pct, priceLineVisible: false, title: 'backtest' }),
      deviation: chart.addSeries(LWC.BaselineSeries, {
        baseValue: { type: 'price', price: 0 },
        topLineColor: cssVar('--up'), topFillColor1: 'rgba(58,158,200,0.25)', topFillColor2: 'rgba(58,158,200,0.05)',
        bottomLineColor: cssVar('--down'), bottomFillColor1: 'rgba(220,100,80,0.05)', bottomFillColor2: 'rgba(220,100,80,0.25)',
        lineWidth: 1, priceFormat: { type: 'custom', formatter: (v) => `${fmtNumber(v, 2)} pp`, minMove: 0.01 },
        priceLineVisible: false, lastValueVisible: true,
      }, 1),
    };
    chart.panes()[1].setHeight(90);
    els.legend.innerHTML = '';
    for (const [label, color, dashed] of [['Paper (live koersen)', SERIES_COLORS[0], false], ['Backtest, zelfde periode', SERIES_COLORS[1], true], ['Afwijking (paper − backtest)', cssVar('--text-muted'), false]]) {
      const li = el('li', 'series-legend__item');
      const sw = el('span', `series-legend__swatch${dashed ? ' is-dashed' : ''}`);
      sw.style.background = color;
      li.append(sw, el('span', '', label));
      els.legend.append(li);
    }
    chart.subscribeCrosshairMove((param) => {
      els.chartLegend.innerHTML = '';
      if (param.time === undefined) return;
      const p = param.seriesData.get(lines.paper);
      const b = param.seriesData.get(lines.backtest);
      const dv = param.seriesData.get(lines.deviation);
      const parts = [['', fmtTime(param.time, 'M1')], ['Paper', p ? `${fmtNumber(p.value, 2)}%` : '—'],
        ['Backtest', b ? `${fmtNumber(b.value, 2)}%` : '—'], ['Afwijking', dv ? `${fmtNumber(dv.value, 2)} pp` : '—']];
      for (const [label, value] of parts) {
        const span = el('span');
        if (label) span.append(`${label} `);
        span.append(el('b', '', value));
        els.chartLegend.append(span);
      }
    });
  }

  function renderChart(cmp) {
    ensureChart();
    lines.paper.setData(cmp.paper);
    lines.backtest.setData(cmp.backtest);
    lines.deviation.setData(cmp.deviation);
    chart.timeScale().fitContent();
  }

  function renderCompareTable(cmp) {
    const labels = {
      total_return_pct: ['Rendement', (v) => fmtPct(v), (v) => `${v >= 0 ? '+' : '−'}${fmtNumber(Math.abs(v), 2)} pp`],
      max_drawdown_pct: ['Max. drawdown', (v) => `−${fmtNumber(v, 2)}%`, (v) => `${v >= 0 ? '+' : '−'}${fmtNumber(Math.abs(v), 2)} pp`],
      sharpe: ['Sharpe', (v) => fmtNumber(v, 2), (v) => fmtNumber(v, 2)],
      winrate_pct: ['Winrate', (v) => `${fmtNumber(v, 1)}%`, (v) => `${fmtNumber(v, 1)} pp`],
      profit_factor: ['Profit factor', (v) => fmtNumber(v, 2), (v) => fmtNumber(v, 2)],
      trades: ['Trades', (v) => String(v), (v) => `${v >= 0 ? '+' : ''}${v}`],
      avg_trade: ['Gem. trade', (v) => fmtEurSigned(v), (v) => fmtEurSigned(v)],
      expectancy_r: ['Expectancy', (v) => `${fmtNumber(v, 2)} R`, (v) => `${fmtNumber(v, 2)} R`],
      costs_total: ['Kosten', (v) => fmtEur(v), (v) => fmtEurSigned(v)],
    };
    els.compareBody.innerHTML = '';
    for (const row of cmp.metrics) {
      const [label, fmt, fmtDiff] = labels[row.key];
      const show = (v) => (v === null || v === undefined ? '—' : fmt(v));
      const tr = el('tr');
      tr.append(el('th', '', label), el('td', 'num', show(row.paper)), el('td', 'num', show(row.backtest)),
        el('td', 'num', row.difference === null ? '—' : fmtDiff(row.difference)));
      els.compareBody.append(tr);
    }
  }

  function renderTrades(trades, symbol) {
    const inst = state.config.instruments.find((i) => i.symbol === symbol);
    els.tradesTitle.textContent = `Trades (${trades.length})`;
    els.tradesBody.innerHTML = '';
    if (!trades.length) {
      const tr = el('tr');
      const td = el('td', 'table__empty', 'Nog geen afgesloten trades.');
      td.colSpan = 11;
      tr.append(td);
      els.tradesBody.append(tr);
      return;
    }
    for (const t of [...trades].reverse()) {
      const tr = el('tr');
      tr.append(
        el('td', 'num-muted', String(t.id)),
        el('td', t.side === 'long' ? 'is-up' : 'is-down', t.side === 'long' ? '▲ Long' : '▼ Short'),
        el('td', '', fmtTimeShort(t.entry_ts)),
        el('td', 'num', fmtPrice(t.entry_price, inst.digits)),
        el('td', '', fmtTimeShort(t.exit_ts)),
        el('td', 'num', fmtPrice(t.exit_price, inst.digits)),
        el('td', 'num', fmtNumber(t.lots, t.lots < 0.1 ? 4 : 2)),
        el('td', '', t.exit_reason),
        el('td', 'num', fmtEur(t.costs_total)),
        el('td', 'num', `${t.r_multiple >= 0 ? '+' : '−'}${fmtNumber(Math.abs(t.r_multiple), 2)}`),
        el('td', `num ${t.pnl >= 0 ? 'is-up' : 'is-down'}`, fmtEurSigned(t.pnl)),
      );
      els.tradesBody.append(tr);
    }
  }

  function renderLog() {
    const filter = els.logFilter.value ? els.logFilter.value.split(',') : null;
    const rows = filter ? lastEvents.filter((e) => filter.includes(e.kind)) : lastEvents;
    els.logTitle.textContent = `Volledig logboek (${lastEvents.length} regels)`;
    els.logBody.innerHTML = '';
    const fragment = document.createDocumentFragment();
    for (const e of rows.slice(0, 2000)) {
      const tr = el('tr', `log--${e.kind}`);
      tr.append(el('td', '', fmtTimeShort(e.ts)), el('td', 'num-muted', fmtTimeShort(e.logged_at)),
        el('td', 'log__kind', EVENT_LABELS[e.kind] || e.kind), el('td', '', e.message));
      fragment.append(tr);
    }
    els.logBody.append(fragment);
  }
  els.logFilter.addEventListener('change', renderLog);

  /* ---------- Evaluation card ---------- */

  async function loadEvaluation() {
    const id = selectedId;
    const ev = await api(`/api/paper/sessions/${id}/evaluation`);
    if (id !== selectedId) return;
    evaluation = { ...ev, sessionId: id };
    renderEvaluation();
  }

  function renderEvaluation() {
    const ev = evaluation;
    els.criteria.innerHTML = '';
    for (const r of ev.results) {
      const li = el('li', `criterion criterion--${r.status}`);
      li.append(el('span', 'criterion__icon', STATUS_ICON[r.status]), el('span', 'criterion__text', r.text),
        el('span', 'criterion__state', `${STATUS_TEXT[r.status]}${r.actual ? ` · nu ${r.actual}` : ''}`));
      els.criteria.append(li);
    }
    if (!ev.results.length) els.criteria.append(el('li', 'hint', 'Nog geen criteria. Voeg ze toe vóór je de resultaten bekijkt.'));

    const locked = Boolean(ev.locked_at);
    els.criteriaEdit.hidden = locked;
    els.evalHint.textContent = locked
      ? `Criteria vastgelegd op ${fmtTimeShort(ev.locked_at)}. Ze kunnen niet meer veranderen. De strategie draait nu ${fmtNumber(ev.days_running, 1)} dagen.`
      : 'Leg vooraf vast wanneer deze strategie geslaagd is. Na het vastleggen kunnen de criteria niet meer veranderen.';
    if (!locked) {
      els.criteriaRows.innerHTML = '';
      for (const c of ev.criteria) addCriterionRow(c);
      if (!ev.criteria.length) {
        addCriterionRow({ type: 'return_vs_backtest', value: 5, after_days: 28 });
        addCriterionRow({ type: 'max_drawdown', value: 15, after_days: 0 });
        addCriterionRow({ type: 'min_trades', value: 20, after_days: 28 });
      }
    }

    els.decision.innerHTML = '';
    for (const [value, label] of Object.entries(ev.decisions)) els.decision.append(new Option(label, value));
    els.decision.value = ev.decision;
    if (document.activeElement !== els.notes) els.notes.value = ev.notes;
  }

  function addCriterionRow(c) {
    const row = el('div', 'criteria-row');
    const type = el('select');
    type.setAttribute('aria-label', 'Soort criterium');
    for (const item of evaluation.catalog) type.append(new Option(item.label, item.type));
    type.value = c.type;
    const value = el('input');
    value.type = 'number';
    value.step = 'any';
    value.value = c.value;
    value.setAttribute('aria-label', 'Waarde');
    const days = el('input');
    days.type = 'number';
    days.min = 0;
    days.max = 365;
    days.value = c.after_days;
    days.setAttribute('aria-label', 'Beoordelen na aantal dagen');
    const remove = el('button', 'btn btn--small btn--danger-ghost', 'Weg');
    remove.type = 'button';
    remove.addEventListener('click', () => row.remove());
    const daysWrap = el('label', 'criteria-row__days');
    daysWrap.append('na', days, 'dagen');
    row.append(type, value, daysWrap, remove);
    els.criteriaRows.append(row);
  }

  function readCriteria() {
    return [...els.criteriaRows.querySelectorAll('.criteria-row')].map((row) => {
      const [type, value] = row.querySelectorAll('select, input');
      const days = row.querySelector('.criteria-row__days input');
      return { type: type.value, value: Number(String(value.value).replace(',', '.')), after_days: Number(days.value || 0) };
    });
  }

  async function saveEvaluation(body, message) {
    try {
      const ev = await api(`/api/paper/sessions/${selectedId}/evaluation`, {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      evaluation = { ...ev, sessionId: selectedId };
      renderEvaluation();
      if (message) showToast(message, 'info');
    } catch (err) {
      showToast(err.message);
    }
  }

  els.addCriterion.addEventListener('click', () => addCriterionRow({ type: 'min_trades', value: 10, after_days: 28 }));
  els.saveCriteria.addEventListener('click', () => saveEvaluation({ criteria: readCriteria() }, 'Criteria opgeslagen.'));
  let lockArmed = null;
  els.lockCriteria.addEventListener('click', () => {
    if (!lockArmed) {
      els.lockCriteria.textContent = 'Zeker? Kan niet terug';
      lockArmed = setTimeout(() => { lockArmed = null; els.lockCriteria.textContent = 'Vastleggen'; }, 3000);
      return;
    }
    clearTimeout(lockArmed);
    lockArmed = null;
    els.lockCriteria.textContent = 'Vastleggen';
    saveEvaluation({ criteria: readCriteria(), lock: true }, 'Criteria vastgelegd.');
  });
  els.decision.addEventListener('change', () => saveEvaluation({ decision: els.decision.value }, 'Besluit opgeslagen.'));
  els.notes.addEventListener('change', async () => {
    await saveEvaluation({ notes: els.notes.value });
    els.notesSaved.textContent = `Opgeslagen om ${fmtClock(new Date())}.`;
  });

  /* ---------- View hooks ---------- */

  function startPolling() {
    clearInterval(timer);
    timer = setInterval(() => refresh(), POLL_MS);
  }

  return {
    onShow(params) {
      const id = Number(params.get('id')) || null;
      if (id !== selectedId) {
        selectedId = id;
        evaluation = null;
        els.detail.hidden = !id;
      }
      updateNewHint();
      refresh(true);
      startPolling();
    },
    onSelectionChange() { updateNewHint(); },
  };
}
