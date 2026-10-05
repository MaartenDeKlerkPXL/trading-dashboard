/* Optimize view: parameter grid on the training part, honest test on the locked part. */

import {
  $, api, postJson, showToast, loadPref, savePref, fmtNumber, fmtPct, fmtTime, formatDay,
  ensureData, setProgress, renderNotice, el, sleep,
} from './common.js';

const PREFS_KEY = 'td.optimize.v1';
const NEUTRAL = [38, 44, 56];        // --border, the "zero" of the diverging scale
const POSITIVE = [57, 135, 229];     // blue
const NEGATIVE = [217, 89, 38];      // orange

export async function initOptimize({ selection, setBusy, backtest, navigate }) {
  const els = {
    form: $('opForm'),
    strategy: $('opStrategy'),
    axes: [...document.querySelectorAll('#opForm .axis')],
    count: $('opCount'),
    fixed: $('opFixed'),
    target: $('opTarget'),
    minTrades: $('opMinTrades'),
    oos: $('opOos'),
    folds: $('opFolds'),
    run: $('opRun'),
    stop: $('opStop'),
    empty: $('opEmpty'),
    output: $('opOutput'),
    title: $('opTitle'),
    subtitle: $('opSubtitle'),
    warnings: $('opWarnings'),
    verdict: $('opVerdict'),
    heatTitle: $('opHeatTitle'),
    heatHint: $('opHeatHint'),
    heatmap: $('opHeatmap'),
    heatLegend: $('opHeatLegend'),
    wfPanel: $('opWfPanel'),
    wfHint: $('opWfHint'),
    wfBody: document.querySelector('#opWfTable tbody'),
  };

  const strategies = backtest.strategies;
  const prefs = loadPref(PREFS_KEY, {});
  let taskId = null;
  let runToken = 0;
  let lastResult = null;

  for (const s of strategies) els.strategy.append(new Option(`${s.label} (${s.version})`, s.key));
  const DEFAULT_STRATEGY = 'sma_cross@v1';
  if (strategies.some((s) => s.key === prefs.strategy)) els.strategy.value = prefs.strategy;
  else if (strategies.some((s) => s.key === DEFAULT_STRATEGY)) els.strategy.value = DEFAULT_STRATEGY;
  els.target.value = prefs.target || 'sharpe';
  els.minTrades.value = prefs.min_trades ?? 20;
  els.oos.value = prefs.oos_pct ?? 30;
  els.folds.value = String(prefs.folds ?? 1);

  const currentStrategy = () => strategies.find((s) => s.key === els.strategy.value);
  const numericParams = (strat) => strat.params.filter((p) => p.type === 'int' || p.type === 'float');

  function defaultRange(p) {
    const base = Number(p.default);
    let from = base * 0.5;
    let to = base * 1.5;
    if (p.min !== null) from = Math.max(from, p.min);
    if (p.max !== null) to = Math.min(to, p.max);
    let step = (to - from) / 5;
    if (p.type === 'int') {
      from = Math.round(from);
      to = Math.round(to);
      step = Math.max(1, Math.round(step));
    } else {
      const unit = p.step || 0.1;
      step = Math.max(unit, Math.round(step / unit) * unit);
      from = Math.round(from / unit) * unit;
      to = Math.round(to / unit) * unit;
    }
    return { start: +from.toFixed(6), stop: +to.toFixed(6), step: +step.toFixed(6) };
  }

  function renderAxes() {
    const strat = currentStrategy();
    const numeric = numericParams(strat);
    const saved = (prefs.axes && prefs.axes[strat.key]) || [];
    els.axes.forEach((axisEl, i) => {
      const select = axisEl.querySelector('.axis__param');
      select.innerHTML = '';
      if (i === 1) select.append(new Option('— geen —', ''));
      for (const p of numeric) select.append(new Option(p.label, p.name));
      const fallback = i === 0 ? numeric[0]?.name : numeric[1]?.name || '';
      const choice = saved[i]?.name ?? fallback;
      select.value = numeric.some((p) => p.name === choice) ? choice : (i === 0 ? numeric[0]?.name : '');
      fillRange(axisEl, saved[i]?.name === select.value ? saved[i] : null);
    });
    renderFixed();
  }

  function fillRange(axisEl, saved) {
    const name = axisEl.querySelector('.axis__param').value;
    const range = axisEl.querySelector('.axis__range');
    range.hidden = !name;
    if (!name) return;
    const p = currentStrategy().params.find((x) => x.name === name);
    const r = saved || defaultRange(p);
    axisEl.querySelector('.axis__from').value = r.start;
    axisEl.querySelector('.axis__to').value = r.stop;
    axisEl.querySelector('.axis__step').value = r.step;
  }

  function axisNames() {
    return els.axes.map((a) => a.querySelector('.axis__param').value).filter(Boolean);
  }

  function renderFixed() {
    const strat = currentStrategy();
    const varied = new Set(axisNames());
    const saved = { ...backtest.savedParams(strat.key), ...((prefs.fixed && prefs.fixed[strat.key]) || {}) };
    els.fixed.innerHTML = '';
    for (const p of strat.params.filter((x) => !varied.has(x.name))) {
      const id = `opFixed-${p.name}`;
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
      els.fixed.append(field);
    }
    if (!els.fixed.children.length) els.fixed.append(el('p', 'hint', 'Alle parameters worden gevarieerd.'));
    updateCount();
  }

  function readRanges() {
    const ranges = [];
    for (const axisEl of els.axes) {
      const name = axisEl.querySelector('.axis__param').value;
      if (!name) continue;
      const num = (sel) => Number(String(axisEl.querySelector(sel).value).replace(',', '.'));
      ranges.push({ name, start: num('.axis__from'), stop: num('.axis__to'), step: num('.axis__step') });
    }
    return ranges;
  }

  function updateCount() {
    const ranges = readRanges();
    const sizes = ranges.map((r) => (r.step > 0 && r.stop >= r.start ? Math.floor((r.stop - r.start) / r.step + 1e-9) + 1 : 0));
    const combos = sizes.reduce((a, b) => a * b, 1);
    const folds = Number(els.folds.value);
    const names = ranges.map((r) => r.name);
    const dupe = new Set(names).size !== names.length;
    if (dupe) {
      els.count.textContent = 'Kies twee verschillende parameters.';
    } else if (!combos) {
      els.count.textContent = 'Controleer van, tot en stap.';
    } else {
      els.count.textContent = `${combos} combinaties × ${folds} venster${folds > 1 ? 's' : ''} ≈ ${combos * folds + folds} backtests`
        + (combos > 400 ? ' (te veel: maximaal 400 combinaties)' : '');
    }
  }

  function readFixed() {
    const strat = currentStrategy();
    const fixed = {};
    for (const input of els.fixed.querySelectorAll('[data-param]')) {
      const p = strat.params.find((x) => x.name === input.dataset.param);
      if (p.type === 'bool') fixed[p.name] = input.checked;
      else if (p.type === 'choice') fixed[p.name] = input.value;
      else {
        const v = Number(String(input.value).replace(',', '.'));
        if (input.value === '' || !Number.isFinite(v)) throw new Error(`Vul een getal in bij "${p.label}".`);
        fixed[p.name] = v;
      }
    }
    return fixed;
  }

  els.strategy.addEventListener('change', renderAxes);
  els.axes.forEach((axisEl) => {
    axisEl.querySelector('.axis__param').addEventListener('change', () => {
      fillRange(axisEl, null);
      renderFixed();
    });
    axisEl.querySelector('.axis__range').addEventListener('input', updateCount);
  });
  els.folds.addEventListener('change', updateCount);
  els.form.addEventListener('submit', (event) => {
    event.preventDefault();
    run();
  });
  els.stop.addEventListener('click', async () => {
    if (!taskId) return;
    els.stop.disabled = true;
    try {
      await api(`/api/tasks/${taskId}`, { method: 'DELETE' });
    } catch (err) {
      showToast(err.message);
    }
  });

  renderAxes();

  /* ---------- Running ---------- */

  async function run() {
    const common = backtest.common();
    if (!common) return;
    let body;
    try {
      body = {
        ...selection(),
        ...common,
        strategy: currentStrategy().key,
        ranges: readRanges(),
        fixed: readFixed(),
        target: els.target.value,
        min_trades: Number(els.minTrades.value),
        oos_pct: Number(els.oos.value),
        folds: Number(els.folds.value),
      };
    } catch (err) {
      showToast(err.message);
      return;
    }
    prefs.strategy = body.strategy;
    prefs.axes = { ...(prefs.axes || {}), [body.strategy]: body.ranges };
    prefs.fixed = { ...(prefs.fixed || {}), [body.strategy]: body.fixed };
    Object.assign(prefs, { target: body.target, min_trades: body.min_trades, oos_pct: body.oos_pct, folds: body.folds });
    savePref(PREFS_KEY, prefs);

    const token = ++runToken;
    setBusy(true);
    els.run.disabled = true;
    els.run.textContent = 'Bezig…';
    els.output.classList.add('is-loading');
    try {
      const ok = await ensureData(selection(), () => token === runToken);
      if (!ok) return;
      let task = await postJson('/api/optimize', body);
      taskId = task.id;
      els.stop.hidden = false;
      els.stop.disabled = false;
      while (task.status === 'running') {
        setProgress(task.done, task.total, '', task.total
          ? `Optimaliseren: ${task.done} van ${task.total} backtests`
          : 'Optimalisatie voorbereiden…');
        await sleep(500);
        task = await api(`/api/tasks/${task.id}`);
      }
      if (token !== runToken) return;
      if (task.status === 'cancelled') showToast('Optimalisatie gestopt.', 'info');
      else if (task.status === 'error') throw new Error(task.message);
      else render(task.result);
    } catch (err) {
      if (token === runToken) showToast(err.message);
    } finally {
      if (token === runToken) {
        taskId = null;
        els.stop.hidden = true;
        setBusy(false);
        els.run.disabled = false;
        els.run.textContent = 'Optimalisatie starten';
        els.output.classList.remove('is-loading');
      }
    }
  }

  /* ---------- Results ---------- */

  const scoreText = (v) => (v === null || v === undefined ? '—' : fmtNumber(v, 2));
  const axisText = (result, params) => result.axes.map((a) => `${a.label} ${fmtNumber(params[a.name], Number.isInteger(params[a.name]) ? 0 : 2)}`).join(' · ');

  function render(result) {
    lastResult = result;
    const s = result.settings;
    els.empty.hidden = true;
    els.output.hidden = false;
    els.title.textContent = `${s.strategy_label} ${s.version} · ${s.symbol} ${s.timeframe}`;
    els.subtitle.textContent = `${formatDay(s.start)} – ${formatDay(s.end)} · geoptimaliseerd op ${result.target_label} · `
      + `${result.variants} varianten · laatste ${fmtNumber(result.oos_pct, 0)}% vergrendeld`
      + (result.folds > 1 ? ` · walk-forward met ${result.folds} vensters` : '');
    renderNotice(els.warnings, result.warnings);
    renderVerdict(result);
    renderHeatmap(result);
    renderWalkForward(result);
  }

  function card(title, lines, tone, extra) {
    const div = el('div', `verdict__card${tone ? ` verdict__card--${tone}` : ''}`);
    div.append(el('h3', 'verdict__title', title));
    for (const [label, value, cls] of lines) {
      const row = el('div', 'verdict__row');
      row.append(el('span', 'verdict__label', label), el('span', `verdict__value ${cls || ''}`, value));
      div.append(row);
    }
    if (extra) div.append(extra);
    return div;
  }

  function metricLines(m) {
    return [
      ['Rendement', fmtPct(m.total_return_pct), m.total_return_pct >= 0 ? 'is-up' : 'is-down'],
      ['Max. drawdown', `−${fmtNumber(m.max_drawdown_pct, 1)}%`],
      ['Sharpe', scoreText(m.sharpe)],
      ['Trades', String(m.trades)],
    ];
  }

  function renderVerdict(result) {
    const b = result.best;
    els.verdict.innerHTML = '';
    const isRet = b.in_sample.total_return_pct;
    const oosRet = b.out_of_sample.total_return_pct;
    const holds = isRet > 0 && oosRet > 0;

    const open = el('button', 'btn btn--small btn--ghost', 'Openen in Backtest');
    open.type = 'button';
    open.addEventListener('click', () => openInBacktest(b.params));

    const settingLines = result.axes.map((a) => [a.label, fmtNumber(b.params[a.name], Number.isInteger(b.params[a.name]) ? 0 : 2)]);
    const targetLine = result.target === 'sharpe' ? [] : [[result.target_label, scoreText(b.score)]];
    els.verdict.append(card('Beste in de training', [
      ...settingLines,
      ...targetLine,
      ...metricLines(b.in_sample),
    ], '', open));

    const verdictText = holds
      ? 'Houdt stand op data die de optimizer niet kende.'
      : oosRet > 0 ? 'Winst op het vergrendelde deel, maar niet in de training: wees voorzichtig.'
        : 'Valt tegen op data die de optimizer niet kende.';
    const note = el('p', 'verdict__note', verdictText);
    els.verdict.append(card(`Vergrendeld deel (vanaf ${fmtTime(b.split_ts, result.settings.timeframe)})`, metricLines(b.out_of_sample),
      holds ? 'good' : 'bad', note));

    const ratio = b.neighbors_avg === null || !b.score ? null : b.neighbors_avg / b.score;
    const robust = ratio === null ? 'Geen buren om te vergelijken.'
      : ratio >= 0.7 ? 'Buren scoren bijna net zo goed: de uitkomst is niet afhankelijk van één precieze instelling.'
        : ratio >= 0.4 ? 'Buren scoren duidelijk minder: de uitkomst is gevoelig voor de precieze instelling.'
          : 'Buren scoren veel slechter: dit lijkt op toeval.';
    els.verdict.append(card('Robuustheid', [
      ['Score beste', scoreText(b.score)],
      [`Gemiddelde van ${b.neighbors_count} buren`, scoreText(b.neighbors_avg)],
    ], ratio === null ? '' : ratio >= 0.7 ? 'good' : ratio >= 0.4 ? '' : 'bad', el('p', 'verdict__note', robust)));
  }

  function color(score, maxAbs) {
    if (score === null || score === undefined || !maxAbs) return null;
    const t = Math.min(1, Math.abs(score) / maxAbs);
    const pole = score >= 0 ? POSITIVE : NEGATIVE;
    const rgb = NEUTRAL.map((n, i) => Math.round(n + (pole[i] - n) * (0.15 + 0.85 * t)));
    const lum = (0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]) / 255;
    return { bg: `rgb(${rgb.join(',')})`, fg: lum > 0.45 ? '#111419' : '#e4e7ee' };
  }

  function renderHeatmap(result) {
    const [ax0, ax1] = result.axes;
    const cols = ax0.values;
    const rows = ax1 ? ax1.values : [null];
    const scores = result.grid.map((g) => g.score).filter((v) => v !== null && v !== undefined);
    const maxAbs = Math.max(...scores.map(Math.abs), 0);
    const bestKey = JSON.stringify(result.axes.map((a) => result.best.params[a.name]));

    els.heatTitle.textContent = `Heatmap: ${result.target_label} per combinatie`;
    els.heatHint.textContent = (result.folds > 1 ? 'Eerste trainingsvenster. ' : 'Alleen het trainingsdeel. ')
      + 'Klik op een vakje om die instellingen in de Backtest te openen.';

    const table = el('table', 'heatmap');
    const caption = el('caption', 'visually-hidden', `${result.target_label} per combinatie van ${ax0.label}${ax1 ? ` en ${ax1.label}` : ''}`);
    table.append(caption);
    const thead = el('thead');
    const hr = el('tr');
    hr.append(el('th', 'heatmap__corner', ax1 ? `${ax1.label} ↓ / ${ax0.label} →` : `${ax0.label} →`));
    for (const v of cols) hr.append(el('th', '', fmtNumber(v, Number.isInteger(v) ? 0 : 2)));
    thead.append(hr);
    table.append(thead);

    const tbody = el('tbody');
    rows.forEach((rv, ri) => {
      const tr = el('tr');
      tr.append(el('th', '', rv === null ? '' : fmtNumber(rv, Number.isInteger(rv) ? 0 : 2)));
      cols.forEach((cv, ci) => {
        const g = result.grid[ci * rows.length + ri];
        const td = el('td');
        const btn = el('button', 'heatmap__cell');
        btn.type = 'button';
        const key = JSON.stringify(result.axes.map((a) => g.params[a.name]));
        if (g.invalid) {
          btn.textContent = '×';
          btn.classList.add('is-invalid');
          btn.disabled = true;
          btn.title = g.invalid;
        } else if (g.score === null) {
          btn.textContent = '–';
          btn.classList.add('is-empty');
          btn.title = `Te weinig trades (${g.metrics ? g.metrics.trades : 0}, minimum ${result.min_trades}).`;
        } else {
          const c = color(g.score, maxAbs);
          btn.style.background = c.bg;
          btn.style.color = c.fg;
          btn.textContent = scoreText(g.score);
          const m = g.metrics;
          btn.title = `${axisText(result, g.params)}\n${result.target_label}: ${scoreText(g.score)}\nRendement ${fmtPct(m.total_return_pct)} · `
            + `max. drawdown −${fmtNumber(m.max_drawdown_pct, 1)}% · ${m.trades} trades`;
        }
        btn.setAttribute('aria-label', `${axisText(result, g.params)}: ${btn.title.split('\n')[0]}`);
        if (key === bestKey) btn.classList.add('is-best');
        if (!btn.disabled) btn.addEventListener('click', () => openInBacktest({ ...result.fixed, ...g.params }));
        td.append(btn);
        tr.append(td);
      });
      tbody.append(tr);
    });
    table.append(tbody);
    els.heatmap.innerHTML = '';
    els.heatmap.append(table);

    els.heatLegend.innerHTML = '';
    if (maxAbs) {
      const low = color(-maxAbs, maxAbs).bg;
      const mid = `rgb(${NEUTRAL.join(',')})`;
      const high = color(maxAbs, maxAbs).bg;
      const bar = el('span', 'heat-legend__bar');
      bar.style.background = `linear-gradient(90deg, ${low}, ${mid}, ${high})`;
      els.heatLegend.append(el('span', '', scoreText(-maxAbs)), bar, el('span', '', scoreText(maxAbs)),
        el('span', 'heat-legend__note', 'omlijnd = beste · × = ongeldige combinatie · – = te weinig trades'));
    }
  }

  function renderWalkForward(result) {
    els.wfPanel.hidden = result.folds <= 1;
    if (result.folds <= 1) return;
    const wf = result.walk_forward;
    els.wfHint.textContent = `Samen: ${fmtPct(wf.total_return_pct)} over ${wf.trades} trades, `
      + `${wf.profitable_windows} van ${result.windows.length} testvensters winstgevend. `
      + (wf.distinct_params > 1 ? `De beste instellingen wisselden ${wf.distinct_params} keer.` : 'Steeds dezelfde instellingen gekozen.');
    els.wfBody.innerHTML = '';
    result.windows.forEach((w, i) => {
      const tr = el('tr');
      const ret = w.out_of_sample.total_return_pct;
      tr.append(
        el('td', '', String(i + 1)),
        el('td', '', fmtTime(w.train_from, 'D1')),
        el('td', '', `${fmtTime(w.test_from, 'D1')} – ${fmtTime(w.test_to, 'D1')}`),
        el('td', '', axisText(result, w.params)),
        el('td', 'num', String(w.out_of_sample.trades)),
        el('td', `num ${ret >= 0 ? 'is-up' : 'is-down'}`, fmtPct(ret)),
      );
      els.wfBody.append(tr);
    });
  }

  function openInBacktest(params) {
    const key = lastResult.settings.strategy;
    backtest.applyStrategy(key, params);
    navigate('backtest');
    backtest.run();
  }

  return {
    run,
    onShow() { updateCount(); },
  };
}
