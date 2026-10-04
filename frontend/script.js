/* Trading Dashboard – entry point: shared toolbar, navigation and the chart view. */

import {
  $, LWC, DEFAULT_DAYS, LEVEL_UNIT, state, api, showToast, loadPref, savePref,
  isoDay, addDays, daysBetweenInclusive, formatDay, timeframeInfo, instrumentInfo,
  fmtNumber, fmtPrice, fmtPct, fmtTime, fmtClock, buildDateFormats, currentDigits,
  createCandleChart, ensureData, hideProgress,
} from './common.js';
import { initBacktest } from './backtest.js';
import { initOptimize } from './optimize.js';
import { initCompare } from './compare.js';
import { initHistory } from './history.js';
import { initPaper } from './paper.js';

const PREFS_KEY = 'td.chart.v1';

const els = {
  form: $('toolbar'),
  symbol: $('symbol'),
  timeframes: $('timeframes'),
  start: $('start'),
  end: $('end'),
  quickRanges: $('quickRanges'),
  loadBtn: $('loadBtn'),
  chartWrap: document.querySelector('#view-chart .chart-wrap'),
  chartState: $('chartState'),
  legend: $('legend'),
  chartTitle: $('chartTitle'),
  chartSubtitle: $('chartSubtitle'),
  lastPrice: $('lastPrice'),
  rangeChange: $('rangeChange'),
  statHigh: $('statHigh'),
  statLow: $('statLow'),
  statRange: $('statRange'),
  statCount: $('statCount'),
  sourceNote: $('sourceNote'),
  modeBadge: $('modeBadge'),
  serverStatus: $('serverStatus'),
  clock: $('clock'),
};

const views = {
  grafiek: { el: $('view-chart'), button: 'Data laden' },
  backtest: { el: $('view-backtest'), button: 'Backtest starten' },
  optimaliseren: { el: $('view-optimaliseren'), button: 'Optimalisatie starten' },
  vergelijken: { el: $('view-vergelijken'), button: 'Alle strategieën draaien' },
  historie: { el: $('view-historie'), button: '' },
  paper: { el: $('view-paper'), button: '' },
};
const modules = {};

let currentView = 'grafiek';
let chartDirty = true;     // the selection changed since the chart was last loaded
let loadToken = 0;
let chartCandles = [];
let priceChart = null;
let backtest = null;

/* ---------- Toolbar ---------- */

export function selection() {
  return { symbol: state.symbol, timeframe: state.timeframe, start: els.start.value, end: els.end.value };
}

function savePrefs() {
  savePref(PREFS_KEY, selection());
}

/* Keep the selected period within what the timeframe allows. Returns true if it changed. */
function clampRange() {
  const tf = timeframeInfo(state.timeframe);
  const today = isoDay(new Date());
  let changed = false;
  if (!els.end.value || els.end.value > today) { els.end.value = today; changed = true; }
  if (!els.start.value || els.start.value > els.end.value) {
    els.start.value = addDays(els.end.value, -(DEFAULT_DAYS[state.timeframe] - 1));
    changed = true;
  }
  if (daysBetweenInclusive(els.start.value, els.end.value) > tf.max_days) {
    els.start.value = addDays(els.end.value, -(tf.max_days - 1));
    changed = true;
  }
  els.start.max = els.end.value;
  els.end.max = today;
  els.quickRanges.querySelectorAll('button').forEach((btn) => {
    btn.disabled = Number(btn.dataset.days) > tf.max_days;
    btn.title = btn.disabled ? `Te lang voor ${state.timeframe} (max ${tf.max_days} dagen)` : '';
  });
  return changed;
}

function selectionChanged() {
  savePrefs();
  chartDirty = true;
  if (currentView === 'grafiek') loadChart();
  else modules[currentView]?.onSelectionChange?.();
}

function buildToolbar(prefs) {
  const groups = { metal: 'Metalen', forex: 'Forex', crypto: 'Crypto' };
  for (const [category, label] of Object.entries(groups)) {
    const group = document.createElement('optgroup');
    group.label = label;
    state.config.instruments
      .filter((i) => i.category === category)
      .forEach((i) => group.append(new Option(`${i.symbol} – ${i.name}`, i.symbol)));
    els.symbol.append(group);
  }

  const symbols = state.config.instruments.map((i) => i.symbol);
  const codes = state.config.timeframes.map((t) => t.code);
  state.symbol = symbols.includes(prefs.symbol) ? prefs.symbol : state.config.defaults.symbol;
  state.timeframe = codes.includes(prefs.timeframe) ? prefs.timeframe : state.config.defaults.timeframe;
  els.symbol.value = state.symbol;

  for (const code of codes) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.textContent = code;
    btn.dataset.tf = code;
    btn.setAttribute('role', 'radio');
    els.timeframes.append(btn);
  }
  updateTimeframeButtons();

  if (prefs.start && prefs.end) {
    els.start.value = prefs.start;
    els.end.value = prefs.end;
  }
  clampRange();

  els.symbol.addEventListener('change', () => {
    state.symbol = els.symbol.value;
    selectionChanged();
  });

  els.timeframes.addEventListener('click', (event) => {
    const btn = event.target.closest('button[data-tf]');
    if (!btn || btn.dataset.tf === state.timeframe) return;
    state.timeframe = btn.dataset.tf;
    updateTimeframeButtons();
    if (clampRange()) {
      showToast(`Periode aangepast: ${state.timeframe} laadt maximaal ${timeframeInfo(state.timeframe).max_days} dagen tegelijk.`, 'info');
    }
    selectionChanged();
  });

  // Arrow keys move between timeframe options (radio group behaviour).
  els.timeframes.addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    const idx = codes.indexOf(state.timeframe);
    const next = codes[(idx + (event.key === 'ArrowRight' ? 1 : codes.length - 1)) % codes.length];
    const btn = els.timeframes.querySelector(`[data-tf="${next}"]`);
    btn.click();
    btn.focus();
    event.preventDefault();
  });

  els.quickRanges.addEventListener('click', (event) => {
    const btn = event.target.closest('button[data-days]');
    if (!btn || btn.disabled) return;
    els.end.value = isoDay(new Date());
    els.start.value = addDays(els.end.value, -(Number(btn.dataset.days) - 1));
    clampRange();
    selectionChanged();
  });

  [els.start, els.end].forEach((input) => input.addEventListener('change', () => {
    clampRange();
    savePrefs();
    chartDirty = true;
  }));

  els.form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (clampRange()) {
      showToast(`Periode aangepast naar het maximum voor ${state.timeframe}.`, 'info');
    }
    savePrefs();
    if (currentView === 'grafiek') loadChart();
    else modules[currentView]?.run?.();
  });
}

function updateTimeframeButtons() {
  els.timeframes.querySelectorAll('button').forEach((btn) => {
    const active = btn.dataset.tf === state.timeframe;
    btn.setAttribute('aria-checked', String(active));
    btn.tabIndex = active ? 0 : -1;
  });
}

export function setBusy(busy) {
  els.loadBtn.disabled = busy;
  els.loadBtn.textContent = busy ? 'Bezig…' : views[currentView].button;
  if (!busy) hideProgress();
}

/* ---------- Navigation ---------- */

function showView(hash) {
  const [rawName, query = ''] = hash.split('?');
  let name = rawName;
  if (!views[name]) name = 'grafiek';
  currentView = name;
  document.body.dataset.view = name;
  for (const [key, view] of Object.entries(views)) view.el.hidden = key !== name;
  document.querySelectorAll('.sidenav__link[data-view]').forEach((link) => {
    const active = link.dataset.view === name;
    link.classList.toggle('is-active', active);
    if (active) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  if (!els.loadBtn.disabled) els.loadBtn.textContent = views[name].button;
  if (name === 'grafiek' && chartDirty) loadChart();
  modules[name]?.onShow?.(new URLSearchParams(query));
}

/* Navigate to a view; extra parameters end up in the address (e.g. #vergelijken?runs=1,2). */
export function navigate(name, params) {
  const hash = params ? `${name}?${new URLSearchParams(params)}` : name;
  if (location.hash.slice(1) === hash) showView(hash);
  else location.hash = hash;
}

/* ---------- Chart view ---------- */

function renderLegend(bar) {
  if (!bar) { els.legend.textContent = ''; return; }
  const change = bar.open ? ((bar.close - bar.open) / bar.open) * 100 : 0;
  els.legend.innerHTML = '';
  const parts = [
    ['', fmtTime(bar.time)],
    ['O', fmtPrice(bar.open)],
    ['H', fmtPrice(bar.high)],
    ['L', fmtPrice(bar.low)],
    ['C', fmtPrice(bar.close)],
    ['', fmtPct(change)],
  ];
  for (const [label, value] of parts) {
    const span = document.createElement('span');
    if (label) span.append(`${label} `);
    const b = document.createElement('b');
    b.textContent = value;
    span.append(b);
    els.legend.append(span);
  }
}

function setChartState(title, text, isError = false) {
  if (!title) { els.chartState.hidden = true; return; }
  els.chartState.hidden = false;
  els.chartState.classList.toggle('is-error', isError);
  els.chartState.querySelector('.chart-state__title').textContent = title;
  els.chartState.querySelector('.chart-state__text').textContent = text;
}

function resetStats() {
  els.lastPrice.textContent = '—';
  els.rangeChange.hidden = true;
  [els.statHigh, els.statLow, els.statRange, els.statCount].forEach((el) => { el.textContent = '—'; });
  els.sourceNote.textContent = '';
  els.legend.textContent = '';
}

function renderCandles(payload) {
  const candles = payload.candles;
  chartCandles = candles;
  const inst = instrumentInfo(payload.symbol);

  els.chartTitle.textContent = `${inst.symbol} · ${inst.name}`;
  els.chartSubtitle.textContent = `${payload.timeframe} · ${formatDay(els.start.value)} – ${formatDay(els.end.value)}`;

  if (!candles.length) {
    priceChart.setData([]);
    setChartState('Geen koersdata in deze periode', 'Waarschijnlijk was de markt gesloten (weekend of feestdag). Kies een andere periode.');
    resetStats();
    return;
  }

  priceChart.setDigits(inst.digits);
  priceChart.setData(candles);
  priceChart.chart.timeScale().fitContent();
  setChartState(null);
  renderLegend(candles[candles.length - 1]);

  let high = -Infinity;
  let low = Infinity;
  for (const c of candles) {
    if (c.high > high) high = c.high;
    if (c.low < low) low = c.low;
  }
  const first = candles[0];
  const last = candles[candles.length - 1];
  const change = ((last.close - first.open) / first.open) * 100;

  els.lastPrice.textContent = fmtPrice(last.close);
  els.rangeChange.hidden = false;
  els.rangeChange.textContent = `${change >= 0 ? '▲' : '▼'} ${fmtPct(change)}`;
  els.rangeChange.className = `change ${change >= 0 ? 'is-up' : 'is-down'}`;
  els.rangeChange.title = 'Verandering over de geladen periode';
  els.statHigh.textContent = fmtPrice(high);
  els.statLow.textContent = fmtPrice(low);
  els.statRange.textContent = fmtPct(((high - low) / low) * 100).replace('+', '');
  els.statCount.textContent = fmtNumber(candles.length, 0);

  const missing = payload.missing_chunks
    ? ` Let op: ${payload.missing_chunks} ${LEVEL_UNIT[payload.timeframe]} konden niet worden opgehaald.`
    : '';
  els.sourceNote.textContent =
    `Bron: Dukascopy (bid-prijzen). Tijden in Nederlandse tijd. Laatste candle: ${fmtTime(last.time)}.${missing}`;
}

async function loadChart() {
  const token = ++loadToken;
  const params = selection();
  chartDirty = false;
  setBusy(true);
  els.chartWrap.classList.add('is-loading');
  try {
    const ok = await ensureData(params, () => token === loadToken);
    if (!ok) return;
    const payload = await api(`/api/candles?${new URLSearchParams(params)}`);
    if (token !== loadToken) return;
    renderCandles(payload);
  } catch (err) {
    if (token !== loadToken) return;
    chartDirty = true;
    if (!chartCandles.length) setChartState('Data laden mislukt', err.message, true);
    showToast(err.message);
  } finally {
    if (token === loadToken) {
      setBusy(false);
      els.chartWrap.classList.remove('is-loading');
    }
  }
}

/* ---------- Status bar ---------- */

async function checkHealth() {
  const text = els.serverStatus.querySelector('.server-status__text');
  try {
    await api('/api/health');
    els.serverStatus.className = 'server-status is-ok';
    text.textContent = 'Server actief';
  } catch {
    els.serverStatus.className = 'server-status is-down';
    text.textContent = 'Server niet bereikbaar';
  }
}

function tickClock() {
  els.clock.textContent = fmtClock(new Date());
}

/* ---------- Start ---------- */

async function init() {
  buildDateFormats(state.timezone);
  tickClock();
  setInterval(tickClock, 1000);

  if (!LWC) {
    setChartState('Grafiekbibliotheek niet geladen', 'Het bestand vendor/lightweight-charts ontbreekt. Haal de laatste versie op met git pull.', true);
    return;
  }

  try {
    state.config = await api('/api/config');
  } catch (err) {
    setChartState('Kan de server niet bereiken', err.message, true);
    await checkHealth();
    return;
  }

  state.timezone = state.config.timezone || state.timezone;
  buildDateFormats(state.timezone);
  els.modeBadge.textContent = { paper: 'Paper', backtest: 'Backtest', live: 'Live' }[state.config.mode] || state.config.mode;

  checkHealth();
  setInterval(checkHealth, 10000);

  buildToolbar(loadPref(PREFS_KEY));
  priceChart = createCandleChart($('chart'));
  priceChart.chart.subscribeCrosshairMove((param) => {
    const bar = param.time !== undefined ? param.seriesData.get(priceChart.candles) : null;
    renderLegend(bar || chartCandles[chartCandles.length - 1]);
  });
  priceChart.setDigits(currentDigits());

  backtest = await initBacktest({ selection, setBusy });
  modules.backtest = backtest;
  modules.optimaliseren = await initOptimize({ selection, setBusy, backtest, navigate });
  modules.vergelijken = initCompare({ selection, setBusy, backtest, navigate });
  modules.historie = initHistory({ backtest, navigate });
  modules.paper = initPaper({ selection, backtest, navigate });

  window.addEventListener('hashchange', () => showView(location.hash.slice(1)));
  showView(location.hash.slice(1) || 'grafiek');
}

init();
