/* Shared helpers: API calls, formatting, dates, toasts, charts and the data download flow. */

export const LWC = window.LightweightCharts;

export const DEFAULT_DAYS = { M1: 3, M5: 14, M15: 30, M30: 60, H1: 91, H4: 365, D1: 1095 };
export const LEVEL_UNIT = { M1: 'dagen', M5: 'dagen', M15: 'dagen', M30: 'dagen', H1: 'maanden', H4: 'maanden', D1: 'jaren' };

/* App-wide state shared by the views. */
export const state = {
  config: null,
  timezone: 'Europe/Amsterdam',
  symbol: null,
  timeframe: null,
};

export const $ = (id) => document.getElementById(id);

export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

/* ---------- Storage (convenience only: everything works without it) ---------- */

export function loadPref(key, fallback = {}) {
  try {
    const value = JSON.parse(localStorage.getItem(key) || 'null');
    return value ?? fallback;
  } catch {
    return fallback;
  }
}

export function savePref(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage unavailable */
  }
}

/* ---------- API ---------- */

export async function api(path, options) {
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

export const postJson = (path, data) => api(path, {
  method: 'POST',
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(data),
});

export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/* ---------- Toast (non-blocking message, bottom right) ---------- */

let toastTimer = null;
export function showToast(message, kind = 'error') {
  const el = $('toast');
  el.textContent = message;
  el.classList.toggle('is-info', kind === 'info');
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, kind === 'info' ? 4000 : 8000);
}

/* ---------- Inline warning list (never a pop-up) ---------- */

const WARNING_ICON = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3l9 16H3L12 3zm0 6v4m0 3v.5" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';

export function renderNotice(container, warnings) {
  container.innerHTML = '';
  container.hidden = !warnings || !warnings.length;
  for (const text of warnings || []) {
    const p = document.createElement('p');
    p.className = 'notice__item';
    p.innerHTML = WARNING_ICON;
    const span = document.createElement('span');
    span.textContent = text;
    p.append(span);
    container.append(p);
  }
}

/* Categorical colours for comparing runs: fixed order, validated for colour blindness on the dark surface. */
export const SERIES_COLORS = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'];

export function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

export const fmtParams = (params) => Object.entries(params || {})
  .map(([k, v]) => `${k} ${typeof v === 'boolean' ? (v ? 'ja' : 'nee') : fmtNumber(v, Number.isInteger(v) ? 0 : 2)}`)
  .join(', ');

/* ---------- Dates (inputs hold calendar days, interpreted as UTC by the server) ---------- */

export function isoDay(date) {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, '0');
  const d = String(date.getDate()).padStart(2, '0');
  return `${y}-${m}-${d}`;
}

export function addDays(iso, days) {
  const [y, m, d] = iso.split('-').map(Number);
  return isoDay(new Date(y, m - 1, d + days));
}

export function daysBetweenInclusive(startIso, endIso) {
  const toUtc = (iso) => {
    const [y, m, d] = iso.split('-').map(Number);
    return Date.UTC(y, m - 1, d);
  };
  return Math.round((toUtc(endIso) - toUtc(startIso)) / 86400000) + 1;
}

export function formatDay(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  return new Intl.DateTimeFormat('nl-NL', { day: 'numeric', month: 'short', year: 'numeric' }).format(new Date(y, m - 1, d));
}

export const timeframeInfo = (code) => state.config.timeframes.find((t) => t.code === code);
export const instrumentInfo = (symbol) => state.config.instruments.find((i) => i.symbol === symbol);

/* ---------- Number formatting (Dutch) ---------- */

const numberFormats = new Map();
export function fmtNumber(value, digits) {
  if (!numberFormats.has(digits)) {
    numberFormats.set(digits, new Intl.NumberFormat('nl-NL', { minimumFractionDigits: digits, maximumFractionDigits: digits }));
  }
  return numberFormats.get(digits).format(value);
}

export function currentDigits() {
  const inst = state.symbol && instrumentInfo(state.symbol);
  return inst ? inst.digits : 2;
}

export const fmtPrice = (value, digits = currentDigits()) => fmtNumber(value, digits);
export const fmtPct = (value, digits = 2) => `${value >= 0 ? '+' : '−'}${fmtNumber(Math.abs(value), digits)}%`;

const moneyFormats = new Map();
export function fmtMoney(value, currency = 'EUR') {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  if (!moneyFormats.has(currency)) {
    let format;
    try {
      format = new Intl.NumberFormat('nl-NL', { style: 'currency', currency });
    } catch {
      format = { format: (v) => `${fmtNumber(v, 2)} ${currency}` };
    }
    moneyFormats.set(currency, format);
  }
  return moneyFormats.get(currency).format(value);
}
export const fmtMoneySigned = (value, currency = 'EUR') => (
  value === null || value === undefined ? '—' : `${value >= 0 ? '+' : '−'}${fmtMoney(Math.abs(value), currency)}`
);
export const fmtEur = (value) => fmtMoney(value, 'EUR');
export const fmtEurSigned = (value) => fmtMoneySigned(value, 'EUR');

/* ---------- Time formatting (always Dutch time) ---------- */

let dateFormats = null;
export function buildDateFormats(timeZone) {
  const make = (opts) => new Intl.DateTimeFormat('nl-NL', { timeZone, ...opts });
  dateFormats = {
    full: make({ weekday: 'short', day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }),
    short: make({ day: 'numeric', month: 'short', year: '2-digit', hour: '2-digit', minute: '2-digit' }),
    day: make({ weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' }),
    year: make({ year: 'numeric' }),
    month: make({ month: 'short' }),
    dayMonth: make({ day: 'numeric', month: 'short' }),
    time: make({ hour: '2-digit', minute: '2-digit' }),
    clock: make({ hour: '2-digit', minute: '2-digit', second: '2-digit' }),
  };
}

export function fmtTime(ts, timeframe = state.timeframe) {
  const date = new Date(ts * 1000);
  return timeframe === 'D1' ? dateFormats.day.format(date) : dateFormats.full.format(date);
}

export const fmtTimeShort = (ts) => dateFormats.short.format(new Date(ts * 1000));
export const fmtClock = (date) => dateFormats.clock.format(date);

function tickMarkFormatter(time, type) {
  const date = new Date(time * 1000);
  switch (type) {
    case 0: return dateFormats.year.format(date);
    case 1: return dateFormats.month.format(date);
    case 2: return dateFormats.dayMonth.format(date);
    default: return dateFormats.time.format(date);
  }
}

/* ---------- Charts ---------- */

const GRID = 'rgba(38, 44, 56, 0.55)';

export function baseChartOptions(timeFormatter = (t) => fmtTime(t)) {
  return {
    autoSize: true,
    layout: {
      background: { type: LWC.ColorType.Solid, color: cssVar('--surface') },
      textColor: cssVar('--text-muted'),
      fontFamily: getComputedStyle(document.body).fontFamily,
      fontSize: 12,
      panes: { separatorColor: cssVar('--border'), separatorHoverColor: 'rgba(212, 173, 92, 0.25)' },
    },
    grid: { vertLines: { color: GRID }, horzLines: { color: GRID } },
    rightPriceScale: { borderColor: cssVar('--border') },
    timeScale: { borderColor: cssVar('--border'), timeVisible: true, secondsVisible: false, tickMarkFormatter },
    crosshair: {
      mode: LWC.CrosshairMode.Normal,
      vertLine: { color: '#6b7285', labelBackgroundColor: '#343b4a' },
      horzLine: { color: '#6b7285', labelBackgroundColor: '#343b4a' },
    },
    localization: { locale: 'nl-NL', timeFormatter },
  };
}

/* Candles (+ optional volume pane). Rising candles hollow, falling filled: direction never relies on colour. */
export function createCandleChart(container, { volume = true } = {}) {
  const up = cssVar('--up');
  const down = cssVar('--down');
  const chart = LWC.createChart(container, baseChartOptions());
  const candles = chart.addSeries(LWC.CandlestickSeries, {
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
  let volumeSeries = null;
  if (volume) {
    volumeSeries = chart.addSeries(LWC.HistogramSeries, {
      priceFormat: { type: 'custom', formatter: (v) => fmtNumber(v, 0), minMove: 1 },
      priceLineVisible: false,
      lastValueVisible: false,
    }, 1);
    chart.panes()[1].setHeight(80);
  }

  return {
    chart,
    candles,
    volume: volumeSeries,
    setDigits(digits) {
      candles.applyOptions({ priceFormat: { type: 'custom', formatter: (p) => fmtNumber(p, digits), minMove: 1 / 10 ** digits } });
    },
    setData(rows) {
      candles.setData(rows.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
      if (volumeSeries) {
        const upVol = 'rgba(58, 158, 200, 0.35)';
        const downVol = 'rgba(220, 100, 80, 0.35)';
        volumeSeries.setData(rows.map((c) => ({ time: c.time, value: c.volume, color: c.close >= c.open ? upVol : downVol })));
      }
    },
  };
}

/* ---------- Data download flow (used by every view that needs candles) ---------- */

/**
 * Make sure candles for `params` are cached, showing progress in the toolbar.
 * Resolves when done; throws with a Dutch message on failure. `isCurrent()` lets
 * a newer request cancel this one silently (returns false).
 */
export async function ensureData(params, isCurrent = () => true) {
  const unit = LEVEL_UNIT[params.timeframe];
  setProgress(0, 0, unit);
  let job = await postJson('/api/data/sync', params);
  while (job.status === 'running') {
    if (!isCurrent()) return false;
    setProgress(job.done, job.total, unit);
    await sleep(400);
    job = await api(`/api/data/jobs/${job.id}`);
  }
  if (!isCurrent()) return false;
  setProgress(job.total, job.total, unit);
  if (job.status === 'error') throw new Error(job.message || 'Downloaden mislukt.');
  if (job.message) showToast(job.message, 'info');
  return true;
}

export function setProgress(done, total, unit, text) {
  $('progress').hidden = false;
  const ratio = total ? done / total : 0;
  $('progressBar').style.transform = `scaleX(${ratio})`;
  $('progressText').textContent = text || (total ? `Koersdata ophalen: ${done} van ${total} ${unit}` : 'Koersdata ophalen…');
}

export function hideProgress() {
  setTimeout(() => {
    $('progress').hidden = true;
    $('progressBar').style.transform = 'scaleX(0)';
  }, 500);
}
