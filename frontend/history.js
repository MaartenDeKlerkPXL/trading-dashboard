/* History view: all saved runs, notes, deleting, selecting for comparison, TradingView import. */

import {
  $, state, api, postJson, showToast, fmtNumber, fmtPct, fmtTimeShort, formatDay, el, fmtParams,
} from './common.js';

const MAX_COMPARE = 8;

export function initHistory({ backtest, navigate }) {
  const els = {
    body: document.querySelector('#hiTable tbody'),
    title: $('hiTitle'),
    selected: $('hiSelected'),
    compare: $('hiCompare'),
    importBox: $('tvImport'),
    form: $('tvForm'),
    file: $('tvFile'),
    symbol: $('tvSymbol'),
    timeframe: $('tvTimeframe'),
    timezone: $('tvTimezone'),
    capital: $('tvCapital'),
    submit: $('tvRun'),
  };
  const selected = new Set();
  let runs = [];

  for (const i of state.config.instruments) els.symbol.append(new Option(`${i.symbol} – ${i.name}`, i.symbol));
  for (const t of state.config.timeframes) els.timeframe.append(new Option(t.code, t.code));

  /* ---------- List ---------- */

  async function load() {
    try {
      runs = await api('/api/runs');
    } catch (err) {
      showToast(err.message);
      return;
    }
    for (const id of [...selected]) if (!runs.some((r) => r.id === id)) selected.delete(id);
    render();
  }

  function render() {
    els.title.textContent = `Opgeslagen runs (${runs.length})`;
    els.body.innerHTML = '';
    if (!runs.length) {
      const tr = el('tr');
      const td = el('td', 'table__empty', 'Nog geen runs. Elke backtest die je draait, wordt hier automatisch bewaard.');
      td.colSpan = 12;
      tr.append(td);
      els.body.append(tr);
      updateSelection();
      return;
    }
    const fragment = document.createDocumentFragment();
    for (const run of runs) fragment.append(row(run));
    els.body.append(fragment);
    updateSelection();
  }

  function row(run) {
    const s = run.settings;
    const m = run.metrics;
    const tr = el('tr');
    tr.dataset.id = run.id;

    const check = el('input');
    check.type = 'checkbox';
    check.checked = selected.has(run.id);
    check.setAttribute('aria-label', `Run ${run.id} selecteren`);
    check.addEventListener('change', () => {
      if (check.checked) {
        if (selected.size >= MAX_COMPARE) {
          check.checked = false;
          showToast(`Je kunt maximaal ${MAX_COMPARE} runs tegelijk vergelijken.`, 'info');
          return;
        }
        selected.add(run.id);
      } else selected.delete(run.id);
      updateSelection();
    });
    const tdCheck = el('td');
    tdCheck.append(check);

    const strategy = el('td', 'cell-wrap');
    const name = run.name || `${s.strategy_label || run.strategy} ${run.version || ''}`.trim();
    strategy.append(el('span', 'run-name', name));
    if (run.source === 'tradingview') strategy.append(el('span', 'badge', 'TradingView'));
    if (run.strategy_changed) strategy.append(el('span', 'badge badge--warn', 'bestand gewijzigd'));
    if (run.strategy_missing) strategy.append(el('span', 'badge badge--warn', 'strategie bestaat niet meer'));
    if (run.source !== 'tradingview' && Object.keys(s.params || {}).length) {
      strategy.append(el('span', 'run-params', fmtParams(s.params)));
    }

    const ret = m.total_return_pct;
    const note = el('input', 'note-input');
    note.type = 'text';
    note.value = run.note || '';
    note.placeholder = 'Notitie…';
    note.maxLength = 5000;
    note.setAttribute('aria-label', `Notitie bij run ${run.id}`);
    note.addEventListener('change', async () => {
      try {
        await api(`/api/runs/${run.id}`, {
          method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ note: note.value }),
        });
        run.note = note.value;
        note.classList.add('is-saved');
        setTimeout(() => note.classList.remove('is-saved'), 1200);
      } catch (err) {
        showToast(err.message);
      }
    });
    const tdNote = el('td');
    tdNote.append(note);

    const actions = el('td');
    const actionBox = el('div', 'row-actions');
    const open = el('button', 'btn btn--small btn--ghost', 'Openen');
    open.type = 'button';
    open.addEventListener('click', () => navigate('backtest', { run: run.id }));
    const report = el('a', 'btn btn--small btn--ghost', 'Rapport');
    report.href = `/api/runs/${run.id}/report`;
    report.setAttribute('download', '');
    report.title = 'Analyse-rapport downloaden (Markdown)';
    const del = el('button', 'btn btn--small btn--danger-ghost', 'Verwijderen');
    del.type = 'button';
    let armed = null;
    del.addEventListener('click', async () => {
      // Two-step delete instead of a confirmation pop-up.
      if (!armed) {
        del.textContent = 'Zeker?';
        del.classList.add('is-armed');
        armed = setTimeout(() => { armed = null; del.textContent = 'Verwijderen'; del.classList.remove('is-armed'); }, 3000);
        return;
      }
      clearTimeout(armed);
      try {
        await api(`/api/runs/${run.id}`, { method: 'DELETE' });
        selected.delete(run.id);
        runs = runs.filter((r) => r.id !== run.id);
        render();
      } catch (err) {
        showToast(err.message);
      }
    });
    actionBox.append(open, report, del);
    actions.append(actionBox);

    tr.append(
      tdCheck,
      el('td', 'num-muted', String(run.id)),
      el('td', 'cell-tight', fmtTimeShort(run.created_at)),
      strategy,
      el('td', '', `${run.symbol} ${run.timeframe}`),
      el('td', 'cell-tight', `${formatDay(run.start)} – ${formatDay(run.end)}`),
      el('td', `num ${ret >= 0 ? 'is-up' : 'is-down'}`, fmtPct(ret)),
      el('td', 'num', `−${fmtNumber(m.max_drawdown_pct, 1)}%`),
      el('td', 'num', m.sharpe === null ? '—' : fmtNumber(m.sharpe, 2)),
      el('td', `num${m.trades < 30 ? ' is-warn' : ''}`, String(m.trades)),
      tdNote,
      actions,
    );
    return tr;
  }

  function updateSelection() {
    const n = selected.size;
    els.compare.disabled = n < 2;
    els.selected.textContent = n ? `${n} geselecteerd` : 'Selecteer twee of meer runs om te vergelijken.';
  }

  els.compare.addEventListener('click', () => {
    const ids = runs.filter((r) => selected.has(r.id)).map((r) => r.id).reverse();
    navigate('vergelijken', { runs: ids.join(',') });
  });

  /* ---------- TradingView import ---------- */

  els.form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const file = els.file.files[0];
    if (!file) {
      showToast('Kies eerst een CSV-bestand.');
      els.file.focus();
      return;
    }
    if (file.size > 10_000_000) {
      showToast('Het bestand is te groot (maximaal 10 MB).');
      return;
    }
    const capital = els.capital.value.trim() ? Number(els.capital.value.replace(',', '.')) : null;
    if (capital !== null && !(capital > 0)) {
      showToast('Het startkapitaal moet groter dan 0 zijn, of leeg voor automatisch.');
      return;
    }
    els.submit.disabled = true;
    els.submit.textContent = 'Bezig…';
    try {
      const content = await file.text();
      const res = await postJson('/api/import/tradingview', {
        filename: file.name, content, symbol: els.symbol.value, timeframe: els.timeframe.value,
        timezone: els.timezone.value, capital,
      });
      showToast(`${res.trades} trades geïmporteerd als run #${res.run_id}`
        + (res.capital_inferred ? ` (startkapitaal ${fmtNumber(res.capital, 0)} afgeleid uit het bestand).` : '.'), 'info');
      els.form.reset();
      els.symbol.value = state.symbol;
      els.timeframe.value = state.timeframe;
      selected.add(res.run_id);
      await load();
    } catch (err) {
      showToast(err.message);
    } finally {
      els.submit.disabled = false;
      els.submit.textContent = 'Importeren';
    }
  });

  return {
    onShow() {
      els.symbol.value = state.symbol;
      els.timeframe.value = state.timeframe;
      load();
    },
  };
}
