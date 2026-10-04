/* Compare view: several runs side by side, plus TradingView trade matching. */

import {
  $, LWC, api, postJson, showToast, fmtNumber, fmtPct, fmtMoney, fmtTimeShort, formatDay,
  baseChartOptions, ensureData, setProgress, renderNotice, el, fmtParams, SERIES_COLORS,
} from './common.js';

const BETTER = { higher: 1, lower: -1 };

export function initCompare({ selection, setBusy, backtest, navigate }) {
  const els = {
    runAll: $('cmpRunAll'),
    empty: $('cmpEmpty'),
    output: $('cmpOutput'),
    warnings: $('cmpWarnings'),
    legend: $('cmpLegend'),
    chart: $('cmpChart'),
    tableHead: document.querySelector('#cmpTable thead'),
    tableBody: document.querySelector('#cmpTable tbody'),
    matchings: $('cmpMatchings'),
  };
  let chart = null;
  let series = [];
  let shownIds = '';
  let token = 0;

  els.runAll.addEventListener('click', runAll);

  async function runAll() {
    const common = backtest.common();
    if (!common) return;
    const my = ++token;
    const sel = selection();
    setBusy(true);
    els.runAll.disabled = true;
    try {
      const ok = await ensureData(sel, () => my === token);
      if (!ok) return;
      setProgress(1, 1, '', 'Alle strategieën doorrekenen…');
      const entries = backtest.strategies.map((s) => ({ strategy: s.key, params: backtest.savedParams(s.key) }));
      const res = await postJson('/api/compare/run', { ...sel, ...common, entries });
      if (my !== token) return;
      navigate('vergelijken', { runs: res.run_ids.join(',') });
    } catch (err) {
      if (my === token) showToast(err.message);
    } finally {
      if (my === token) {
        setBusy(false);
        els.runAll.disabled = false;
      }
    }
  }

  async function load(ids) {
    const my = ++token;
    els.output.classList.add('is-loading');
    try {
      const data = await api(`/api/compare?ids=${encodeURIComponent(ids)}`);
      if (my !== token) return;
      shownIds = ids;
      render(data);
    } catch (err) {
      if (my === token) showToast(err.message);
    } finally {
      if (my === token) els.output.classList.remove('is-loading');
    }
  }

  function render(data) {
    els.empty.hidden = true;
    els.output.hidden = false;
    renderNotice(els.warnings, data.warnings);
    renderChart(data.runs);
    renderTable(data.runs);
    renderMatchings(data);
  }

  function renderChart(runs) {
    if (!chart) {
      chart = LWC.createChart(els.chart, baseChartOptions());
    }
    for (const s of series) chart.removeSeries(s);
    series = [];
    els.legend.innerHTML = '';
    runs.forEach((run, i) => {
      const color = SERIES_COLORS[i % SERIES_COLORS.length];
      const s = chart.addSeries(LWC.LineSeries, {
        color,
        lineWidth: 2,
        priceLineVisible: false,
        lastValueVisible: true,
        title: `#${run.id}`,
        priceFormat: { type: 'custom', formatter: (v) => `${fmtNumber(v, 1)}%`, minMove: 0.01 },
      });
      s.setData(dedupe(run.equity_pct));
      series.push(s);
      const li = el('li', 'series-legend__item');
      const swatch = el('span', 'series-legend__swatch');
      swatch.style.background = color;
      li.append(swatch, el('span', '', run.label));
      els.legend.append(li);
    });
    if (series.length) {
      series[0].createPriceLine({ price: 0, color: '#6b7285', lineWidth: 1, lineStyle: LWC.LineStyle.Dashed, axisLabelVisible: false });
    }
    chart.timeScale().fitContent();
  }

  /* Line series need strictly increasing times; imported runs can close two trades at once. */
  function dedupe(points) {
    const out = [];
    for (const p of points) {
      if (out.length && out[out.length - 1].time >= p.time) out[out.length - 1] = { time: out[out.length - 1].time, value: p.value };
      else out.push(p);
    }
    return out;
  }

  function renderTable(runs) {
    const dash = '—';
    const rows = [
      ['Rendement', (r) => r.metrics.total_return_pct, (v) => fmtPct(v), 'higher'],
      ['Max. drawdown', (r) => r.metrics.max_drawdown_pct, (v) => `−${fmtNumber(v, 2)}%`, 'lower'],
      ['Sharpe', (r) => r.metrics.sharpe, (v) => fmtNumber(v, 2), 'higher'],
      ['Sortino', (r) => r.metrics.sortino, (v) => fmtNumber(v, 2), 'higher'],
      ['Winrate', (r) => r.metrics.winrate_pct, (v) => `${fmtNumber(v, 1)}%`, 'higher'],
      ['Profit factor', (r) => r.metrics.profit_factor, (v) => fmtNumber(v, 2), 'higher'],
      ['Expectancy', (r) => r.metrics.expectancy_r, (v) => `${fmtNumber(v, 2)} R`, 'higher'],
      ['Trades', (r) => r.metrics.trades, (v) => String(v), null],
      ['Gem. trade', (r) => r.metrics.avg_trade, null, null],
      ['Kosten', (r) => r.metrics.costs_total, null, null],
    ];

    els.tableHead.innerHTML = '';
    const hr = el('tr');
    hr.append(el('th', '', ''));
    runs.forEach((run, i) => {
      const th = el('th', 'num');
      th.scope = 'col';
      const swatch = el('span', 'series-legend__swatch');
      swatch.style.background = SERIES_COLORS[i % SERIES_COLORS.length];
      th.append(swatch, ` #${run.id}`);
      th.title = run.label;
      hr.append(th);
    });
    els.tableHead.append(hr);

    els.tableBody.innerHTML = '';
    const addRow = (label, cells, cls = '') => {
      const tr = el('tr', cls);
      tr.append(el('th', '', label));
      cells.forEach((c) => tr.append(c));
      els.tableBody.append(tr);
    };
    addRow('Strategie', runs.map((r) => el('td', 'num cell-wrap', r.label.replace(/^#\d+ /, ''))), 'row-setup');
    addRow('Markt', runs.map((r) => el('td', 'num', `${r.settings.symbol} ${r.settings.timeframe}`)), 'row-setup');
    addRow('Periode', runs.map((r) => el('td', 'num', `${formatDay(r.settings.start)} – ${formatDay(r.settings.end)}`)), 'row-setup');
    addRow('Instellingen', runs.map((r) => el('td', 'num cell-wrap cell-small', r.source === 'tradingview' ? 'TradingView-import' : fmtParams(r.settings.params) || dash)), 'row-setup');

    for (const [label, get, fmt, better] of rows) {
      const values = runs.map(get);
      const valid = values.filter((v) => v !== null && v !== undefined);
      let best = null;
      if (better && valid.length > 1) best = BETTER[better] > 0 ? Math.max(...valid) : Math.min(...valid);
      const cells = runs.map((run, i) => {
        const v = values[i];
        let text = dash;
        if (v !== null && v !== undefined) text = fmt ? fmt(v) : fmtMoney(v, run.settings.currency || 'EUR');
        else if (label === 'Profit factor' && run.metrics.no_losing_trades) text = '∞';
        const td = el('td', `num${best !== null && v === best ? ' is-best' : ''}`, text);
        return td;
      });
      addRow(label, cells);
    }
  }

  function renderMatchings(data) {
    els.matchings.innerHTML = '';
    for (const m of data.matchings) {
      const panel = el('section', 'panel');
      const head = el('header', 'panel__head');
      head.append(el('h2', 'panel__title', `Trade voor trade: ${m.reference_label} tegenover ${m.our_label}`));
      head.append(el('p', 'panel__hint', 'Twee trades horen bij elkaar als ze dezelfde richting hebben en hooguit één candle na elkaar openen.'));
      panel.append(head);

      const stats = el('div', 'match-stats');
      const stat = (label, value, sub) => {
        const d = el('div', 'match-stat');
        d.append(el('span', 'kpi__label', label), el('span', 'kpi__value', value), el('span', 'kpi__sub', sub || ''));
        return d;
      };
      stats.append(
        stat('Overeenkomst', `${fmtNumber(m.match_pct, 0)}%`, `${m.matched} gekoppelde trades`),
        stat('Alleen in TradingView', String(m.only_reference.length), listIds(m.only_reference)),
        stat('Alleen hier', String(m.only_ours.length), listIds(m.only_ours)),
        stat('Instapprijs verschil', m.avg_entry_price_diff === null ? '—' : fmtNumber(m.avg_entry_price_diff, 3), 'gemiddeld, absoluut'),
        stat('Zelfde uitstap', m.same_exit_pct === null ? '—' : `${fmtNumber(m.same_exit_pct, 0)}%`, 'binnen één candle'),
      );
      panel.append(stats);

      if (m.matches.length) {
        const wrap = el('div', 'table-wrap');
        const table = el('table', 'table');
        table.innerHTML = '<thead><tr><th scope="col">Instap</th><th scope="col">Richting</th>'
          + '<th scope="col" class="num">Prijs TV</th><th scope="col" class="num">Prijs hier</th>'
          + '<th scope="col">Uitstap TV</th><th scope="col">Uitstap hier</th><th scope="col" class="num">Verschil (candles)</th></tr></thead>';
        const tbody = el('tbody');
        for (const x of m.matches.slice(0, 300)) {
          const tr = el('tr');
          tr.append(
            el('td', '', fmtTimeShort(x.entry_ts)),
            el('td', x.side === 'long' ? 'is-up' : 'is-down', x.side === 'long' ? '▲ Long' : '▼ Short'),
            el('td', 'num', fmtNumber(x.entry_price, 2)),
            el('td', 'num', fmtNumber(x.our_entry_price, 2)),
            el('td', '', fmtTimeShort(x.exit_ts)),
            el('td', '', fmtTimeShort(x.our_exit_ts)),
            el('td', `num${x.exit_bars_apart > 1 ? ' is-warn' : ''}`, fmtNumber(x.exit_bars_apart, 0)),
          );
          tbody.append(tr);
        }
        table.append(tbody);
        wrap.append(table);
        panel.append(wrap);
      }
      els.matchings.append(panel);
    }
  }

  const listIds = (ids) => (ids.length ? `trade ${ids.slice(0, 8).join(', ')}${ids.length > 8 ? ', …' : ''}` : 'geen');

  return {
    run: runAll,
    onShow(params) {
      const ids = params.get('runs');
      if (ids && ids !== shownIds) load(ids);
      else if (!ids && !shownIds) {
        els.empty.hidden = false;
        els.output.hidden = true;
      }
    },
  };
}
