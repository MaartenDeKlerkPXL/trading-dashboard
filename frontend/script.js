'use strict';

/* Trading Dashboard – phase 1: load candles and show them on a chart. */

const LWC = window.LightweightCharts;

const DEFAULT_DAYS = { M1: 3, M5: 14, M15: 30, M30: 60, H1: 91, H4: 365, D1: 1095 };
const LEVEL_UNIT = { M1: 'dagen', M5: 'dagen', M15: 'dagen', M30: 'dagen', H1: 'maanden', H4: 'maanden', D1: 'jaren' };
const PREFS_KEY = 'td.chart.v1';

const state = {
  config: null,
  timezone: 'Europe/Amsterdam',
  symbol: null,
  timeframe: null,
  loadToken: 0,
  candles: [],
};

const $ = (id) => document.getElementById(id);
const els = {
  form: $('toolbar'),
  symbol: $('symbol'),
  timeframes: $('timeframes'),
  start: $('start'),
  end: $('end'),
  quickRanges: $('quickRanges'),
  loadBtn: $('loadBtn'),
  progress: $('progress'),
  progressBar: $('progressBar'),
  progressText: $('progressText'),
  chart: $('chart'),
  chartWrap: document.querySelector('.chart-wrap'),
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
  toast: $('toast'),
};

/* ---------- Small helpers ---------- */

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function loadPrefs() {
  try {
    return JSON.parse(localStorage.getItem(PREFS_KEY) || '{}') || {};
  } catch {
    return {};
  }
}

function savePrefs() {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify({
      symbol: state.symbol,
      timeframe: state.timeframe,
      start: els.start.value,
      end: els.end.value,
    }));
  } catch {
    /* storage unavailable: preferences are a convenience only */
  }
}

async function api(path, options) {
  let res;
  try {
    res = await fetch(path, options);
  } catch {
    throw new Error('De server is niet bereikbaar. Staat het Terminal-venster met start.sh nog open?');
  }
  let body = null;
  try {
    body = await res.json();
  } catch {
    /* non-JSON response */
  }
  if (!res.ok) {
    const detail = body && body.detail;
    throw new Error(typeof detail === 'string' ? detail : `Er ging iets mis op de server (fout ${res.status}).`);
  }
  return body;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

let toastTimer = null;
function showToast(message, kind = 'error') {
  els.toast.textContent = message;
  els.toast.classList.toggle('is-info', kind === 'info');
  els.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { els.toast.hidden = true; }, kind === 'info' ? 4000 : 8000);
}

/* ---------- Dates (inputs hold calendar days, interpreted as UTC by the server) ---------- */

function isoDay(date) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

function addDays(iso, days) {
  const [y, m, d] = iso.split('-').map(Number);
  return isoDay(new Date(y, m - 1, d + days));
}

function daysBetweenInclusive(startIso, endIso) {
  const a = Date.UTC(...startIso.split('-').map((v, i) => (i === 1 ? Number(v) - 1 : Number(v))));
  const b = Date.UTC(...endIso.split('-').map((v, i) => (i === 1 ? Number(v) - 1 : Number(v))));
  return Math.round((b - a) / 86400000) + 1;
}

function timeframeInfo(code) {
  return state.config.timeframes.find((t) => t.code === code);
}

function instrumentInfo(symbol) {
  return state.config.instruments.find((i) => i.symbol === symbol);
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

/* ---------- Formatting ---------- */

const numberFormats = new Map();
function fmtNumber(value, digits) {
  if (!numberFormats.has(digits)) {
    numberFormats.set(digits, new Intl.NumberFormat('nl-NL', { minimumFractionDigits: digits, maximumFractionDigits: digits }));
  }
  return numberFormats.get(digits).format(value);
}

function currentDigits() {
  const inst = state.symbol && instrumentInfo(state.symbol);
  return inst ? inst.digits : 2;
}

const fmtPrice = (value) => fmtNumber(value, currentDigits());
const fmtPct = (value) => `${value >= 0 ? '+' : '−'}${fmtNumber(Math.abs(value), 2)}%`;

let dateFormats = null;
function buildDateFormats(timeZone) {
  const make = (opts) => new Intl.DateTimeFormat('nl-NL', { timeZone, ...opts });
  dateFormats = {
    full: make({ weekday: 'short', day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }),
    day: make({ weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' }),
    year: make({ year: 'numeric' }),
    month: make({ month: 'short' }),
    dayMonth: make({ day: 'numeric', month: 'short' }),
    time: make({ hour: '2-digit', minute: '2-digit' }),
    clock: make({ hour: '2-digit', minute: '2-digit', second: '2-digit' }),
  };
}

function fmtTime(ts) {
  const date = new Date(ts * 1000);
  return state.timeframe === 'D1' ? dateFormats.day.format(date) : dateFormats.full.format(date);
}

function tickMarkFormatter(time, type) {
  const date = new Date(time * 1000);
  switch (type) {
    case 0: return dateFormats.year.format(date);
    case 1: return dateFormats.month.format(date);
    case 2: return dateFormats.dayMonth.format(date);
    default: return dateFormats.time.format(date);
  }
}

/* ---------- Chart ---------- */

let chart = null;
let candleSeries = null;
let volumeSeries = null;

function createChart() {
  const up = cssVar('--up');
  const down = cssVar('--down');
  const grid = 'rgba(38, 44, 56, 0.55)';

  chart = LWC.createChart(els.chart, {
    autoSize: true,
    layout: {
      background: { type: LWC.ColorType.Solid, color: cssVar('--surface') },
      textColor: cssVar('--text-muted'),
      fontFamily: getComputedStyle(document.body).fontFamily,
      fontSize: 12,
      panes: { separatorColor: cssVar('--border'), separatorHoverColor: 'rgba(212, 173, 92, 0.25)' },
    },
    grid: { vertLines: { color: grid }, horzLines: { color: grid } },
    rightPriceScale: { borderColor: cssVar('--border') },
    timeScale: { borderColor: cssVar('--border'), timeVisible: true, secondsVisible: false, tickMarkFormatter },
    crosshair: {
      mode: LWC.CrosshairMode.Normal,
      vertLine: { color: '#6b7285', labelBackgroundColor: '#343b4a' },
      horzLine: { color: '#6b7285', labelBackgroundColor: '#343b4a' },
    },
    localization: { locale: 'nl-NL', timeFormatter: fmtTime },
  });

  // Rising candles are hollow, falling candles filled: direction never depends on colour alone.
  candleSeries = chart.addSeries(LWC.CandlestickSeries, {
    upColor: 'rgba(0, 0, 0, 0)',
    downColor: down,
    borderVisible: true,
    borderUpColor: up,
    borderDownColor: down,
    wickUpColor: up,
    wickDownColor: down,
    priceLineColor: cssVar('--accent'),
    priceLineStyle: LWC.LineStyle.Dashed,
  });

  volumeSeries = chart.addSeries(LWC.HistogramSeries, {
    priceFormat: { type: 'custom', formatter: (v) => fmtNumber(v, 0), minMove: 1 },
    priceLineVisible: false,
    lastValueVisible: false,
  }, 1);
  chart.panes()[1].setHeight(80);

  chart.subscribeCrosshairMove((param) => {
    const bar = param.time !== undefined ? param.seriesData.get(candleSeries) : null;
    renderLegend(bar || state.candles[state.candles.length - 1]);
  });
}

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

function applyInstrumentFormat() {
  const digits = currentDigits();
  candleSeries.applyOptions({ priceFormat: { type: 'custom', formatter: fmtPrice, minMove: 1 / 10 ** digits } });
}

function renderCandles(payload) {
  const candles = payload.candles;
  state.candles = candles;
  const inst = instrumentInfo(payload.symbol);

  els.chartTitle.textContent = `${inst.symbol} · ${inst.name}`;
  els.chartSubtitle.textContent = `${payload.timeframe} · ${formatRangeLabel()}`;

  if (!candles.length) {
    candleSeries.setData([]);
    volumeSeries.setData([]);
    setChartState('Geen koersdata in deze periode', 'Waarschijnlijk was de markt gesloten (weekend of feestdag). Kies een andere periode.');
    resetStats();
    return;
  }

  applyInstrumentFormat();
  const upVol = 'rgba(58, 158, 200, 0.35)';
  const downVol = 'rgba(220, 100, 80, 0.35)';
  candleSeries.setData(candles.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
  volumeSeries.setData(candles.map((c) => ({ time: c.time, value: c.volume, color: c.close >= c.open ? upVol : downVol })));
  chart.timeScale().fitContent();
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

function resetStats() {
  els.lastPrice.textContent = '—';
  els.rangeChange.hidden = true;
  [els.statHigh, els.statLow, els.statRange, els.statCount].forEach((el) => { el.textContent = '—'; });
  els.sourceNote.textContent = '';
  els.legend.textContent = '';
}

function setChartState(title, text, isError = false) {
  if (!title) { els.chartState.hidden = true; return; }
  els.chartState.hidden = false;
  els.chartState.classList.toggle('is-error', isError);
  els.chartState.querySelector('.chart-state__title').textContent = title;
  els.chartState.querySelector('.chart-state__text').textContent = text;
}

function formatRangeLabel() {
  const fmt = (iso) => {
    const [y, m, d] = iso.split('-').map(Number);
    return new Intl.DateTimeFormat('nl-NL', { day: 'numeric', month: 'short', year: 'numeric' }).format(new Date(y, m - 1, d));
  };
  return `${fmt(els.start.value)} – ${fmt(els.end.value)}`;
}

/* ---------- Loading flow: download missing data, then read it ---------- */

function setProgress(done, total, unit) {
  els.progress.hidden = false;
  const ratio = total ? done / total : 0;
  els.progressBar.style.transform = `scaleX(${ratio})`;
  els.progressText.textContent = total
    ? `Koersdata ophalen: ${done} van ${total} ${unit}`
    : 'Koersdata ophalen…';
}

function setLoading(loading) {
  els.loadBtn.disabled = loading;
  els.loadBtn.textContent = loading ? 'Bezig…' : 'Data laden';
  els.chartWrap.classList.toggle('is-loading', loading);
  if (!loading) {
    setTimeout(() => { els.progress.hidden = true; els.progressBar.style.transform = 'scaleX(0)'; }, 500);
  }
}

async function loadData() {
  const token = ++state.loadToken;
  const params = {
    symbol: state.symbol,
    timeframe: state.timeframe,
    start: els.start.value,
    end: els.end.value,
  };
  savePrefs();
  setLoading(true);
  const unit = LEVEL_UNIT[state.timeframe];
  setProgress(0, 0, unit);

  try {
    let job = await api('/api/data/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    });
    while (job.status === 'running') {
      if (token !== state.loadToken) return;  // a newer request took over
      setProgress(job.done, job.total, unit);
      await sleep(400);
      job = await api(`/api/data/jobs/${job.id}`);
    }
    if (token !== state.loadToken) return;
    setProgress(job.total, job.total, unit);
    if (job.status === 'error') throw new Error(job.message || 'Downloaden mislukt.');
    if (job.message) showToast(job.message, 'info');

    const payload = await api(`/api/candles?${new URLSearchParams(params)}`);
    if (token !== state.loadToken) return;
    renderCandles(payload);
  } catch (err) {
    if (token !== state.loadToken) return;
    if (!state.candles.length) {
      setChartState('Data laden mislukt', err.message, true);
    }
    showToast(err.message);
  } finally {
    if (token === state.loadToken) setLoading(false);
  }
}

/* ---------- Controls ---------- */

function buildControls(prefs) {
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
    loadData();
  });

  els.timeframes.addEventListener('click', (event) => {
    const btn = event.target.closest('button[data-tf]');
    if (!btn || btn.dataset.tf === state.timeframe) return;
    state.timeframe = btn.dataset.tf;
    updateTimeframeButtons();
    if (clampRange()) {
      showToast(`Periode aangepast: ${state.timeframe} laadt maximaal ${timeframeInfo(state.timeframe).max_days} dagen tegelijk.`, 'info');
    }
    loadData();
  });

  // Arrow keys move between timeframe options (radio group behaviour).
  els.timeframes.addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    const idx = codes.indexOf(state.timeframe);
    const next = codes[(idx + (event.key === 'ArrowRight' ? 1 : codes.length - 1)) % codes.length];
    els.timeframes.querySelector(`[data-tf="${next}"]`).click();
    els.timeframes.querySelector(`[data-tf="${next}"]`).focus();
    event.preventDefault();
  });

  els.quickRanges.addEventListener('click', (event) => {
    const btn = event.target.closest('button[data-days]');
    if (!btn || btn.disabled) return;
    els.end.value = isoDay(new Date());
    els.start.value = addDays(els.end.value, -(Number(btn.dataset.days) - 1));
    clampRange();
    loadData();
  });

  els.form.addEventListener('submit', (event) => {
    event.preventDefault();
    if (clampRange()) {
      showToast(`Periode aangepast naar het maximum voor ${state.timeframe}.`, 'info');
    }
    loadData();
  });
}

function updateTimeframeButtons() {
  els.timeframes.querySelectorAll('button').forEach((btn) => {
    const active = btn.dataset.tf === state.timeframe;
    btn.setAttribute('aria-checked', String(active));
    btn.tabIndex = active ? 0 : -1;
  });
}

/* ---------- Status bar ---------- */

async function checkHealth() {
  try {
    const health = await api('/api/health');
    els.serverStatus.className = 'server-status is-ok';
    els.serverStatus.querySelector('.server-status__text').textContent = 'Server actief';
    return health;
  } catch {
    els.serverStatus.className = 'server-status is-down';
    els.serverStatus.querySelector('.server-status__text').textContent = 'Server niet bereikbaar';
    return null;
  }
}

function tickClock() {
  els.clock.textContent = dateFormats.clock.format(new Date());
}

/* ---------- Start ---------- */

async function init() {
  buildDateFormats(state.timezone);
  tickClock();
  setInterval(tickClock, 1000);

  if (!LWC) {
    setChartState('Grafiekbibliotheek niet geladen', 'Het bestand vendor/lightweight-charts ontbreekt. Haal de laatste versie op via GitHub Desktop.', true);
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

  await checkHealth();
  setInterval(checkHealth, 10000);

  buildControls(loadPrefs());
  createChart();
  loadData();
}

init();
