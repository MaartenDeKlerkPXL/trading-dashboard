/* Risk & alerts view: kill switch, hard limits, today's risk per strategy, alerts and reconciliation.
   Also keeps the kill-switch bar and the alert badge in the menu up to date on every page. */

import {
  $, api, postJson, showToast, el, fmtNumber, fmtPct, fmtEur, fmtTimeShort, fmtClock, renderNotice,
} from './common.js';

const POLL_MS = 15000;
const BRIEF_MS = 20000;
const ARM_MS = 3000;

const LEVEL_LABEL = { urgent: 'Urgent', warning: 'Let op', info: 'Info' };

/* A button that needs a second click within a few seconds: no pop-ups. */
function twoStep(button, armedText, action) {
  const idle = button.textContent;
  let armed = null;
  const reset = () => { armed = null; button.textContent = idle; button.classList.remove('is-armed'); };
  button.addEventListener('click', async () => {
    if (!armed) {
      button.textContent = armedText;
      button.classList.add('is-armed');
      armed = setTimeout(reset, ARM_MS);
      return;
    }
    clearTimeout(armed);
    reset();
    await action();
  });
}

export function initRisk() {
  const els = {
    view: $('view-risico'),
    kill: $('rkKill'),
    killTitle: $('rkKillTitle'),
    killText: $('rkKillText'),
    close: $('rkClose'),
    closeLabel: $('rkClose').closest('label'),
    killBtn: $('rkKillBtn'),
    error: $('rkError'),
    limits: $('rkLimits'),
    sessions: document.querySelector('#rkSessions tbody'),
    email: $('rkEmail'),
    test: $('rkTest'),
    monitor: $('rkMonitor'),
    alerts: $('rkAlerts'),
    reconcile: $('rkReconcile'),
    reconInfo: $('rkReconInfo'),
    recon: document.querySelector('#rkRecon tbody'),
    banner: $('killBanner'),
    bannerText: $('killBannerText'),
    navBadge: $('navAlerts'),
  };
  let data = null;
  let timer = null;
  let busy = false;

  /* ---------- Shared: kill-switch bar and menu badge ---------- */

  function renderBrief(killSwitch, openAlerts) {
    const active = Boolean(killSwitch && killSwitch.active);
    els.banner.hidden = !active;
    if (active) {
      els.bannerText.textContent = `Kill switch actief sinds ${fmtTimeShort(killSwitch.since)}: alle strategieën staan stil `
        + 'en er kan niets starten tot je hem opheft.';
    }
    const urgent = (openAlerts && openAlerts.urgent) || 0;
    const warning = (openAlerts && openAlerts.warning) || 0;
    const n = urgent + warning;
    els.navBadge.hidden = n === 0;
    els.navBadge.textContent = String(n);
    els.navBadge.classList.toggle('is-warn', urgent === 0);
    els.navBadge.title = n ? `${n} open melding${n === 1 ? '' : 'en'}` : '';
  }

  async function refreshBrief() {
    try {
      const brief = await api('/api/risk/brief');
      renderBrief(brief.kill_switch, brief.open_alerts);
    } catch {
      /* the server status in the top bar already shows connection problems */
    }
  }

  /* ---------- Kill switch ---------- */

  async function kill() {
    try {
      const r = await postJson('/api/risk/kill', { close_positions: els.close.checked });
      showToast(`Kill switch actief: ${r.paused} strategie(ën) gepauzeerd, ${r.cancelled} order(s) geannuleerd, `
        + `${r.closed} positie(s) gesloten.`, 'info');
    } catch (err) {
      showToast(err.message);
    }
    await refresh();
  }

  async function release() {
    try {
      await postJson('/api/risk/release', {});
      showToast('Kill switch opgeheven. Hervat de strategieën die weer mogen draaien op de pagina Paper trading.', 'info');
    } catch (err) {
      showToast(err.message);
    }
    await refresh();
  }

  twoStep(els.killBtn, 'Zeker? Klik nogmaals', async () => {
    if (data && data.kill_switch.active) await release();
    else await kill();
  });

  function renderKill(ks) {
    const active = Boolean(ks.active);
    els.kill.classList.toggle('is-active', active);
    els.closeLabel.hidden = active;
    if (active) {
      els.killTitle.textContent = 'Kill switch actief';
      els.killText.textContent = `Sinds ${fmtTimeShort(ks.since)}. Alle strategieën zijn gepauzeerd en wachtende orders `
        + `geannuleerd${ks.close_positions ? ', open posities zijn gesloten' : '; open posities staan nog open met hun stop-loss'}. `
        + 'Nieuwe strategieën starten of hervatten kan pas weer na het opheffen.';
      els.killBtn.textContent = 'Kill switch opheffen';
      els.killBtn.className = 'btn btn--ghost';
    } else {
      els.killTitle.textContent = 'Kill switch';
      els.killText.textContent = 'Zet in één keer alle strategieën stil en annuleert alle wachtende orders. Daarna kan '
        + 'er niets meer starten tot je de kill switch opheft. Gebruik hem als er iets misgaat of als je twijfelt.';
      els.killBtn.textContent = 'Alles stoppen';
      els.killBtn.className = 'btn btn--kill';
    }
  }

  /* ---------- Limits ---------- */

  function limitTile(label, value, sub) {
    const div = el('div', 'limit');
    div.append(el('span', 'limit__label', label), el('span', 'limit__value', value));
    if (sub) div.append(el('span', 'limit__sub', sub));
    return div;
  }

  function renderLimits(d) {
    const l = d.limits;
    const lots = (v) => `${fmtNumber(v, 2).replace(/,?0+$/, '')} lot`;
    const custom = Object.entries(l.max_lots).filter(([, v]) => v !== l.max_lots_default)
      .map(([s, v]) => `${s} ${lots(v)}`).join(' · ');
    els.limits.replaceChildren(
      limitTile('Max dagverlies', `${fmtNumber(l.max_daily_loss_pct, 1)}%`, 'per strategie, vanaf 00:00'),
      limitTile('Open posities', `${d.open_positions} / ${l.max_open_positions}`,
        d.live_open_positions ? `paper samen; live apart: ${d.live_open_positions}` : 'alle strategieën samen'),
      limitTile('Max risico per trade', `${fmtNumber(l.max_risk_per_trade_pct, 1)}%`, 'van de equity, tot de stop-loss'),
      limitTile('Max positiegrootte', lots(l.max_lots_default), custom || 'voor elk instrument'),
      limitTile('Weekendregel', l.weekend_close ? 'Aan' : 'Uit', l.weekend_close
        ? `forex en metalen in de winst sluiten ${l.weekend_close_minutes_before} min vóór de sluiting op vrijdag`
        : 'posities blijven over het weekend open'),
      limitTile('Uitvoering', d.mode === 'live' ? 'Paper + live' : 'Paper',
        d.mode === 'live' ? 'orders gaan naar cTrader' : 'live trading staat uit (config.toml)'),
    );
  }

  /* ---------- Today per strategy ---------- */

  function renderSessions(d) {
    els.sessions.innerHTML = '';
    if (!d.sessions.length) {
      const tr = el('tr');
      const td = el('td', 'table__empty', 'Geen actieve paper-strategieën.');
      td.colSpan = 5;
      tr.append(td);
      els.sessions.append(tr);
      return;
    }
    const limit = d.limits.max_daily_loss_pct;
    for (const s of d.sessions) {
      const tr = el('tr');
      const name = el('td');
      const link = el('a', 'link', `${s.mode === 'live' ? 'LIVE · ' : ''}${s.label}`);
      link.href = `#paper?id=${s.id}`;
      name.append(link, el('span', 'cell-sub', `${s.symbol} ${s.timeframe}`));

      const status = el('td');
      status.append(el('span', `status status--${s.status}`, s.status_label));
      if (s.status_reason) status.append(el('span', 'cell-note', s.status_reason));

      const day = el('td');
      const wrap = el('div', 'dayloss');
      const track = el('div', 'dayloss__track');
      const used = Math.max(0, Math.min(1, -s.day_pnl_pct / limit));
      const bar = el('div', `dayloss__bar${s.blocked_today ? ' is-hit' : used >= 0.5 ? ' is-warn' : ''}`);
      bar.style.width = `${(s.blocked_today ? 1 : used) * 100}%`;
      track.append(bar);
      track.title = `Verlies vandaag tegenover de limiet van ${fmtNumber(limit, 1)}%`;
      wrap.append(track, el('span', `dayloss__text ${s.day_pnl_pct >= 0 ? 'is-up' : 'is-down'}`, fmtPct(s.day_pnl_pct)));
      day.append(wrap);
      if (s.blocked_today) day.append(el('span', 'cell-note', 'Daglimiet bereikt: geen nieuwe trades tot morgen.'));

      const pos = s.position;
      const posText = pos
        ? `${pos.side === 'long' ? '▲ Long' : '▼ Short'} ${fmtNumber(pos.lots, pos.lots < 0.1 ? 4 : 2)} lot`
        : (s.pending_orders ? 'Order wacht' : '—');
      tr.append(name, status, el('td', 'num', fmtEur(s.equity)), day, el('td', '', posText));
      els.sessions.append(tr);
    }
  }

  /* ---------- Alerts ---------- */

  function renderAlerts(d) {
    const e = d.email;
    els.email.textContent = e.configured
      ? `E-mail naar ${e.to} bij urgente problemen: de loop stopt of crasht, of de koersen of broker zijn langer dan `
        + `${e.feed_down_minutes} minuten onbereikbaar. Hooguit één e-mail per probleem per ${e.repeat_minutes} minuten.`
      : 'E-mail is nog niet ingesteld: meldingen verschijnen alleen hier. Zie docs/EMAIL_MELDINGEN.md.';
    els.test.disabled = !e.configured;

    const m = d.monitor;
    const parts = [];
    parts.push(m.heartbeat ? `Laatste ronde van de loop: ${fmtClock(new Date(m.heartbeat * 1000))}` : 'De loop heeft nog niet gedraaid');
    parts.push(m.heartbeat_url ? 'externe bewaking (heartbeat) aan' : 'externe bewaking (heartbeat) uit');
    if (m.heartbeat_error) parts.push(m.heartbeat_error);
    for (const [symbol, since] of Object.entries(m.feed_down || {})) {
      parts.push(`${symbol}: geen koersen sinds ${fmtClock(new Date(since * 1000))}`);
    }
    els.monitor.textContent = `${parts.join(' · ')}.`;

    els.alerts.innerHTML = '';
    if (!d.alerts.length) {
      els.alerts.append(el('li', 'alerts__empty', 'Nog geen meldingen. Alles in orde.'));
      return;
    }
    for (const a of d.alerts) {
      const li = el('li', `alert alert--${a.level}${a.resolved_at ? ' is-resolved' : ''}`);
      li.append(el('p', 'alert__title', `${LEVEL_LABEL[a.level] || a.level}: ${a.title}`));
      li.append(el('p', 'alert__message', a.message));
      const meta = el('p', 'alert__meta');
      let text = `${fmtTimeShort(a.first_at)}`;
      if (a.count > 1) text += ` · ${a.count}× · laatst ${fmtTimeShort(a.last_at)}`;
      if (a.emailed_at) text += ` · e-mail verstuurd om ${fmtClock(new Date(a.emailed_at * 1000))}`;
      if (a.resolved_at) text += ` · afgehandeld ${fmtTimeShort(a.resolved_at)}`;
      meta.append(document.createTextNode(text));
      if (a.email_error && !a.resolved_at) meta.append(el('span', 'is-error', ` · ${a.email_error}`));
      li.append(meta);
      if (!a.resolved_at) {
        const btn = el('button', 'btn btn--small btn--ghost alert__action', 'Afhandelen');
        btn.type = 'button';
        btn.addEventListener('click', async () => {
          try {
            await postJson(`/api/alerts/${a.id}/resolve`, {});
          } catch (err) {
            showToast(err.message);
          }
          await refresh();
        });
        li.append(btn);
      }
      els.alerts.append(li);
    }
  }

  els.test.addEventListener('click', async () => {
    els.test.disabled = true;
    els.test.textContent = 'Versturen…';
    try {
      const r = await postJson('/api/alerts/test', {});
      showToast(`Testmail verstuurd naar ${r.to}. Kijk ook in je spammap.`, 'info');
    } catch (err) {
      showToast(err.message);
    } finally {
      els.test.textContent = 'Testmail versturen';
      els.test.disabled = !(data && data.email.configured);
    }
  });

  /* ---------- Reconciliation ---------- */

  function renderRecon(d) {
    const r = d.reconciliation;
    els.recon.innerHTML = '';
    if (!r) {
      els.reconInfo.textContent = `Nog niet gecontroleerd. Dit gebeurt automatisch elke ${d.reconcile_minutes} minuten zolang de loop draait.`;
      return;
    }
    els.reconInfo.textContent = `Laatst gecontroleerd om ${fmtClock(new Date(r.checked_at * 1000))} · automatisch elke `
      + `${d.reconcile_minutes} minuten · ${r.differences ? `${r.differences} afwijking${r.differences === 1 ? '' : 'en'}` : 'geen afwijkingen'}`
      + ` · open posities paper ${r.open_positions} (maximaal ${d.limits.max_open_positions})${r.open_positions_ok ? '' : ': te veel!'}`
      + `${r.live_open_positions !== undefined ? `, live ${r.live_open_positions}` : ''}`
      + `${r.broker_error ? ` · broker niet bereikbaar: ${r.broker_error}` : ''}`
      + `${r.unknown_positions && r.unknown_positions.length ? ` · ${r.unknown_positions.length} positie(s) bij de broker zonder actieve strategie` : ''}.`;
    const rows = r.sessions.filter((s) => s.status !== 'stopped' || s.differences);
    if (!rows.length) {
      const tr = el('tr');
      const td = el('td', 'table__empty', 'Geen sessies om te controleren.');
      td.colSpan = 5;
      tr.append(td);
      els.recon.append(tr);
      return;
    }
    for (const s of rows) {
      s.checks.forEach((c, i) => {
        const tr = el('tr');
        const name = el('td', '', i === 0 ? `${s.label} · ${s.symbol} ${s.timeframe}` : '');
        const check = el('td', '', c.name);
        if (c.note) check.title = c.note;
        const outcome = el('td', c.ok ? 'check-ok' : 'check-bad', c.ok ? '✓ klopt' : '✗ wijkt af');
        tr.append(name, check, el('td', 'num', c.ours), el('td', 'num', c.theirs), outcome);
        els.recon.append(tr);
      });
    }
  }

  els.reconcile.addEventListener('click', async () => {
    els.reconcile.disabled = true;
    try {
      const r = await postJson('/api/reconcile', {});
      showToast(r.differences ? `${r.differences} afwijking(en) gevonden.` : 'Alles klopt.', r.differences ? 'error' : 'info');
    } catch (err) {
      showToast(err.message);
    } finally {
      els.reconcile.disabled = false;
    }
    await refresh();
  });

  /* ---------- Loading ---------- */

  async function refresh() {
    if (busy) return;
    busy = true;
    try {
      data = await api('/api/risk');
      renderNotice(els.error, []);
      renderKill(data.kill_switch);
      renderLimits(data);
      renderSessions(data);
      renderAlerts(data);
      renderRecon(data);
      renderBrief(data.kill_switch, data.open_alerts);
    } catch (err) {
      renderNotice(els.error, [`Kon de risicogegevens niet laden: ${err.message}`]);
    } finally {
      busy = false;
    }
  }

  refreshBrief();
  setInterval(refreshBrief, BRIEF_MS);

  return {
    onShow() {
      refresh();
      clearInterval(timer);
      timer = setInterval(() => { if (!els.view.hidden) refresh(); }, POLL_MS);
    },
    refreshBrief,
  };
}
