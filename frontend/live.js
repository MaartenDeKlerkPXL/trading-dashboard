/* Live view: connect to cTrader, choose an account, place a demo test order and start live strategies.
   A live strategy only starts after you type the amounts over. No pop-ups: everything happens inline. */

import {
  $, api, postJson, showToast, el, fmtNumber, fmtPct, fmtEur, fmtTimeShort, renderNotice, fmtParams,
} from './common.js';

const POLL_MS = 15000;
const ARM_MS = 3000;
const STATUS_TEXT = {
  sending: 'verzenden', filled: 'uitgevoerd', rejected: 'geweigerd', unknown: 'onbekend',
  not_found: 'niet gevonden', skipped: 'overgeslagen', expired: 'verlopen',
};

function twoStep(button, armedText, action) {
  let armed = null;
  let idle = button.textContent;
  button.addEventListener('click', async () => {
    if (!armed) {
      idle = button.textContent;
      button.textContent = armedText;
      button.classList.add('is-armed');
      armed = setTimeout(() => { armed = null; button.textContent = idle; button.classList.remove('is-armed'); }, ARM_MS);
      return;
    }
    clearTimeout(armed);
    armed = null;
    button.textContent = idle;
    button.classList.remove('is-armed');
    await action();
  });
}

function parseAmount(text) {
  let s = String(text || '').replace(/[€\s ]/g, '');
  if (!s) return null;
  if (s.includes(',') && s.includes('.')) s = s.replace(/\./g, '').replace(',', '.');
  else if (s.includes(',')) s = s.replace(',', '.');
  else if (/^\d{1,3}(\.\d{3})+$/.test(s)) s = s.replace(/\./g, '');
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

export function initLive({ navigate }) {
  const els = {
    view: $('view-live'),
    off: $('lvOff'),
    money: $('lvMoney'),
    error: $('lvError'),
    step1: $('lvStep1'), step1Text: $('lvStep1Text'),
    step2: $('lvStep2'), step2Text: $('lvStep2Text'),
    step3: $('lvStep3'), step3Text: $('lvStep3Text'),
    connect: $('lvConnect'), forget: $('lvForget'),
    accounts: $('lvAccounts'), choose: $('lvChoose'), fetch: $('lvFetch'),
    account: $('lvAccount'),
    symbolsPanel: $('lvSymbolsPanel'), symbols: document.querySelector('#lvSymbols tbody'),
    testPanel: $('lvTestPanel'), test: $('lvTest'), testSteps: $('lvTestSteps'),
    startPanel: $('lvStartPanel'), form: $('lvForm'), template: $('lvTemplate'), capital: $('lvCapital'),
    risk: $('lvRisk'), check: $('lvCheck'),
    confirm: $('lvConfirm'), confirmTitle: $('lvConfirmTitle'), confirmList: $('lvConfirmList'),
    typeCapital: $('lvTypeCapital'), typeRisk: $('lvTypeRisk'), typeLoss: $('lvTypeLoss'),
    start: $('lvStart'), cancel: $('lvCancel'),
    cards: $('lvCards'),
    orders: document.querySelector('#lvOrders tbody'),
    navDot: $('navLive'),
  };
  let st = null;
  let plan = null;
  let timer = null;
  let accountLoaded = false;

  /* ---------- Connection steps ---------- */

  function setStep(li, done, text) {
    li.classList.toggle('is-done', done);
    li.classList.toggle('is-todo', !done);
    li.querySelector('.step__text').textContent = text;
  }

  function renderConnection() {
    const link = st.link;
    els.off.hidden = st.enabled;
    els.money.textContent = st.allow_real_money
      ? 'Let op: handelen met echt geld staat AAN in config.toml.'
      : 'Echt geld staat uit: alleen demo-accounts kunnen handelen.';
    setStep(els.step1, link.configured, link.configured
      ? 'CTRADER_CLIENT_ID en CTRADER_CLIENT_SECRET zijn gevonden.'
      : 'Vul CTRADER_CLIENT_ID en CTRADER_CLIENT_SECRET in .env in en herstart het dashboard.');
    const expires = link.token_expires_at ? ` De koppeling wordt automatisch verlengd (nu geldig tot ${fmtTimeShort(link.token_expires_at)}).` : '';
    setStep(els.step2, link.linked, link.linked
      ? `Gekoppeld.${expires}`
      : `Je logt in bij cTrader en geeft het dashboard alleen handelsrechten. Redirect-URL bij je app: ${link.redirect_url}`);
    els.connect.hidden = !link.configured;
    els.connect.textContent = link.linked ? 'Opnieuw koppelen' : 'Koppelen met cTrader';
    els.connect.classList.toggle('btn--primary', !link.linked);
    els.connect.classList.toggle('btn--ghost', link.linked);
    els.forget.hidden = !link.linked;

    const acc = link.account;
    setStep(els.step3, Boolean(acc), acc
      ? `Account ${acc.login} (${acc.is_live ? 'ECHT GELD' : 'demo'}) is gekozen.`
      : (link.linked ? 'Haal je accounts op en kies je demo-account.' : 'Eerst koppelen.'));
    els.accounts.innerHTML = '';
    for (const a of link.accounts) {
      const opt = el('option', '', `${a.login} · ${a.is_live ? 'ECHT GELD' : 'demo'}`);
      opt.value = a.account_id;
      if (acc && acc.account_id === a.account_id) opt.selected = true;
      els.accounts.append(opt);
    }
    els.accounts.hidden = !link.accounts.length;
    els.choose.hidden = !link.accounts.length;
    els.fetch.hidden = !link.linked;
    els.fetch.disabled = !link.linked;

    renderNotice(els.error, link.last_error ? [link.last_error] : []);
    els.symbolsPanel.hidden = !acc;
    els.testPanel.hidden = !acc || acc.is_live;
    els.startPanel.hidden = !acc;
    els.navDot.hidden = !st.sessions.some((s) => s.status === 'running');
  }

  function renderAccount(data) {
    const info = data.info;
    const acc = data.account;
    els.account.hidden = false;
    els.account.replaceChildren(...[
      ['Account', `${acc.login}`, acc.is_live ? 'ECHT GELD' : 'DEMO'],
      ['Broker', info.broker || '—'],
      ['Saldo', `${info.currency === 'EUR' ? fmtEur(info.balance) : `${fmtNumber(info.balance, 2)} ${info.currency}`}`],
      ['Hefboom', info.leverage ? `1:${fmtNumber(info.leverage, 0)}` : '—'],
    ].map(([label, value, badge]) => {
      const div = el('div', 'account__item');
      const v = el('span', 'account__value', value);
      if (badge) {
        v.append(' ');
        v.append(el('span', `badge ${badge === 'DEMO' ? 'badge--demo' : 'badge--real'}`, badge));
      }
      div.append(el('span', 'account__label', label), v);
      return div;
    }));
    els.symbols.innerHTML = '';
    for (const s of data.symbols) {
      const tr = el('tr');
      if (!s.broker_name) {
        tr.append(el('td', '', s.symbol), el('td', 'cell-sub', 'niet beschikbaar op dit account'), el('td'), el('td'), el('td'));
      } else {
        tr.append(el('td', '', s.symbol), el('td', '', s.broker_name),
          el('td', 'num', `${fmtNumber(s.lot_units, s.lot_units < 10 ? 2 : 0)} eenheden`),
          el('td', 'num', `${fmtNumber(s.min_lots, 2)} lot`),
          el('td', s.matches ? 'check-ok' : 'check-bad', s.matches ? '✓' : `✗ wij rekenen met ${s.contract_size}`));
      }
      els.symbols.append(tr);
    }
    if (!els.capital.value) els.capital.value = Math.min(info.balance, st.max_capital, 1000);
    if (!els.risk.value) els.risk.value = Math.min(2, st.risk.max_risk_per_trade_pct);
    els.risk.max = st.risk.max_risk_per_trade_pct;
  }

  async function loadAccount() {
    try {
      renderAccount(await api('/api/live/account'));
      accountLoaded = true;
    } catch (err) {
      renderNotice(els.error, [err.message]);
    }
  }

  els.fetch.addEventListener('click', async () => {
    els.fetch.disabled = true;
    try {
      const accounts = await postJson('/api/live/accounts', {});
      showToast(`${accounts.length} account(s) gevonden.`, 'info');
    } catch (err) {
      showToast(err.message);
    }
    await refresh();
  });

  els.choose.addEventListener('click', async () => {
    try {
      await postJson('/api/live/account', { account_id: Number(els.accounts.value) });
      accountLoaded = false;
    } catch (err) {
      showToast(err.message);
    }
    await refresh();
  });

  twoStep(els.forget, 'Zeker? Klik nogmaals', async () => {
    try {
      await postJson('/api/live/disconnect', {});
      showToast('Koppeling verwijderd.', 'info');
    } catch (err) {
      showToast(err.message);
    }
    els.account.hidden = true;
    accountLoaded = false;
    await refresh();
  });

  /* ---------- Demo test order ---------- */

  twoStep(els.test, 'Zeker? Klik nogmaals', async () => {
    els.test.disabled = true;
    els.testSteps.replaceChildren(el('li', '', 'Bezig…'));
    try {
      const r = await postJson('/api/live/test-order', {});
      els.testSteps.replaceChildren(...r.steps.map((s) => el('li', '', s)));
    } catch (err) {
      els.testSteps.replaceChildren(el('li', 'check-bad', err.message));
    } finally {
      els.test.disabled = false;
    }
  });

  /* ---------- Starting a live strategy ---------- */

  async function loadTemplates() {
    const sessions = await api('/api/paper/sessions');
    const current = els.template.value;
    els.template.innerHTML = '';
    if (!sessions.length) {
      els.template.append(el('option', '', 'Start eerst een paper-strategie'));
      els.template.disabled = true;
      els.check.disabled = true;
      return;
    }
    els.template.disabled = false;
    els.check.disabled = false;
    for (const s of sessions) {
      const opt = el('option', '', `#${s.id} ${s.strategy_label} ${s.version} · ${s.symbol} ${s.timeframe} · `
        + `${fmtParams(s.params)} · paper ${fmtPct(s.return_pct)}`);
      opt.value = s.id;
      els.template.append(opt);
    }
    if (current) els.template.value = current;
  }

  function body(typed) {
    return {
      template_id: Number(els.template.value),
      capital: Number(els.capital.value),
      risk_pct: Number(els.risk.value),
      ...(typed ? { typed } : {}),
    };
  }

  function resetConfirm() {
    plan = null;
    els.confirm.hidden = true;
    for (const input of [els.typeCapital, els.typeRisk, els.typeLoss]) { input.value = ''; input.classList.remove('is-ok'); }
    els.start.disabled = true;
  }

  els.form.addEventListener('submit', async (e) => {
    e.preventDefault();
    resetConfirm();
    try {
      plan = await postJson('/api/live/sessions/prepare', body());
    } catch (err) {
      showToast(err.message);
      return;
    }
    const a = plan.account;
    els.confirmTitle.textContent = `${plan.strategy_label} ${plan.version} live op ${plan.symbol} ${plan.timeframe}, `
      + `${a.is_live ? 'met ECHT GELD' : 'op een demo-account'}`;
    const rows = [
      ['Account', `${a.login} (${a.is_live ? 'echt geld' : 'demo'}), saldo ${fmtEur(a.balance)}`],
      ['Instelling', fmtParams(plan.params) || '—'],
      ['Kapitaal', fmtEur(plan.capital)],
      ['Risico per trade', `${fmtEur(plan.risk_eur)} (${fmtNumber(plan.risk_pct, 1)}%)`],
      ['Max verlies per dag', fmtEur(plan.daily_loss_eur)],
      ['Max positiegrootte', `${fmtNumber(plan.max_lots, 2)} lot`],
    ];
    els.confirmList.replaceChildren(...rows.flatMap(([k, v]) => [el('dt', '', k), el('dd', '', v)]));
    els.confirm.hidden = false;
    els.typeCapital.focus();
  });

  function checkTyped() {
    if (!plan) return;
    const pairs = [[els.typeCapital, plan.capital], [els.typeRisk, plan.risk_eur], [els.typeLoss, plan.daily_loss_eur]];
    let ok = true;
    for (const [input, expected] of pairs) {
      const v = parseAmount(input.value);
      const match = v !== null && Math.abs(v - expected) < 0.005;
      input.classList.toggle('is-ok', match);
      ok = ok && match;
    }
    els.start.disabled = !ok;
  }
  for (const input of [els.typeCapital, els.typeRisk, els.typeLoss]) input.addEventListener('input', checkTyped);
  els.cancel.addEventListener('click', resetConfirm);

  els.start.addEventListener('click', async () => {
    els.start.disabled = true;
    try {
      const s = await postJson('/api/live/sessions', body({
        capital: els.typeCapital.value, risk_eur: els.typeRisk.value, daily_loss_eur: els.typeLoss.value,
      }));
      showToast(`Live-strategie #${s.id} gestart.`, 'info');
      resetConfirm();
    } catch (err) {
      showToast(err.message);
      checkTyped();
    }
    await refresh();
  });

  /* ---------- Live strategies and orders ---------- */

  function renderCards() {
    els.cards.innerHTML = '';
    for (const s of st.sessions) {
      const div = el('article', `pp-card pp-card--${s.status}`);
      const head = el('header', 'pp-card__head');
      head.append(el('h3', 'pp-card__title', `LIVE · ${s.strategy_label} ${s.version}`),
        el('span', `status status--${s.status}`, s.status_label));
      div.append(head);
      const real = s.broker && s.broker.is_live;
      const market = el('p', 'pp-card__market', `${s.symbol} ${s.timeframe} · account ${s.broker ? s.broker.login : '?'} · sinds ${fmtTimeShort(s.started_at)} `);
      market.append(el('span', `badge ${real ? 'badge--real' : 'badge--demo'}`, real ? 'ECHT GELD' : 'DEMO'));
      div.append(market);
      div.append(el('p', `pp-card__return ${s.return_pct >= 0 ? 'is-up' : 'is-down'}`, `${s.return_pct >= 0 ? '▲' : '▼'} ${fmtPct(s.return_pct)}`));
      div.append(el('p', 'pp-card__equity', `${fmtEur(s.equity)} · ${s.trades} trade${s.trades === 1 ? '' : 's'}`));
      const pos = s.position;
      div.append(el('p', 'pp-card__position', pos
        ? `${pos.side === 'long' ? '▲ Long' : '▼ Short'} ${fmtNumber(pos.lots, 2)} lot op ${fmtNumber(pos.entry_price, 2)} · stop bij de broker ${fmtNumber(pos.stop_loss, 2)}`
        : 'Geen open positie'));
      if (s.status_reason) div.append(el('p', 'pp-card__reason', s.status_reason));
      const actions = el('div', 'pp-card__actions');
      const details = el('button', 'btn btn--small btn--ghost', 'Details');
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
        twoStep(stop, pos ? 'Zeker? Sluit positie' : 'Zeker? Definitief', () => act(s.id, 'stop'));
        actions.append(stop);
      }
      div.append(actions);
      els.cards.append(div);
    }
  }

  async function act(id, action) {
    try {
      await postJson(`/api/paper/sessions/${id}/${action}`, {});
    } catch (err) {
      showToast(err.message);
    }
    await refresh();
  }

  async function renderOrders() {
    const orders = await api('/api/live/orders');
    els.orders.innerHTML = '';
    if (!orders.length) {
      const tr = el('tr');
      const td = el('td', 'table__empty', 'Nog geen orders.');
      td.colSpan = 7;
      tr.append(td);
      els.orders.append(tr);
      return;
    }
    for (const o of orders) {
      const tr = el('tr');
      const what = o.kind === 'open' ? `openen ${o.side}`
        : o.kind === 'modify' ? `stop-loss verplaatsen${o.reason ? ` (${o.reason})` : ''}`
          : `sluiten${o.reason ? ` (${o.reason})` : ''}`;
      tr.append(
        el('td', '', fmtTimeShort(o.created_at)), el('td', '', `#${o.session_id}`), el('td', '', what),
        el('td', 'num', o.lots ? fmtNumber(o.lots, 2) : (o.volume ? '—' : '—')),
        el('td', 'num', o.price ? fmtNumber(o.price, 2) : '—'),
        el('td', `order-status--${o.status}`, STATUS_TEXT[o.status] || o.status),
        el('td', 'cell-sub', o.error || (o.position_id ? `positie #${o.position_id}` : '')),
      );
      els.orders.append(tr);
    }
  }

  async function refresh() {
    try {
      st = await api('/api/live/status');
      renderConnection();
      renderCards();
      if (st.link.account && !accountLoaded) await loadAccount();
      if (st.link.account) await loadTemplates();
      await renderOrders();
    } catch (err) {
      renderNotice(els.error, [`Kon de live-gegevens niet laden: ${err.message}`]);
    }
  }

  return {
    onShow(params) {
      if (params.get('gekoppeld')) showToast('Gekoppeld met cTrader. Kies nu je demo-account.', 'info');
      if (params.get('fout')) showToast(`Koppelen is niet gelukt (${params.get('fout')}). Zie de melding hieronder.`);
      refresh();
      clearInterval(timer);
      timer = setInterval(() => { if (!els.view.hidden) refresh(); }, POLL_MS);
    },
  };
}
