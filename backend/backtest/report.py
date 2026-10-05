"""Analysis report of a saved backtest run, as one Markdown document.

Meant to be read by a person and analysed by Claude to find what can be improved in a next strategy
version: besides the settings and key figures it breaks the trades down by side, exit reason, time of
day, weekday, month, market conditions, and how far trades moved for and against before they closed.
"""

from __future__ import annotations

import bisect
import statistics
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..data.instruments import INSTRUMENTS
from ..strategies.base import Bar

WEEKDAYS = ("maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag")
R_BUCKETS = (-1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0, 3.0)


# ---------- formatting ----------

def _n(v, digits=2) -> str:
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "ja" if v else "nee"
    if isinstance(v, int):
        return f"{v:,}".replace(",", ".")
    if isinstance(v, float):
        if v != v:
            return "—"
        return f"{v:,.{digits}f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return str(v)


def _pct(v, digits=1) -> str:
    return "—" if v is None else f"{_n(v, digits)}%"


def _table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


# ---------- statistics ----------

def group_stats(trades: list[dict]) -> dict:
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in losses)
    rs = [t["r_multiple"] for t in trades if t.get("r_multiple") is not None]   # imports have no R
    return {
        "trades": n,
        "winrate": len(wins) / n * 100 if n else None,
        "pnl": sum(t["pnl"] for t in trades),
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
        "avg_r": sum(rs) / len(rs) if rs else None,
        "avg_win": gross_win / len(wins) if wins else None,
        "avg_loss": -gross_loss / len(losses) if losses else None,
    }


def _stats_rows(groups: dict[str, list[dict]]) -> list[list]:
    rows = []
    for name, trades in groups.items():
        if not trades:
            continue
        s = group_stats(trades)
        pf = "∞" if s["profit_factor"] is None and s["pnl"] > 0 else _n(s["profit_factor"])
        rows.append([name, s["trades"], _pct(s["winrate"]), _n(s["pnl"]), pf, _n(s["avg_r"])])
    return rows


STATS_HEADERS = ["Groep", "Trades", "Winrate", "Resultaat (€)", "Profit factor", "Gem. R"]


def excursions(trade: dict, bars: list[Bar], times: list[int] | None = None) -> tuple[float, float] | None:
    """Most favourable and most adverse move during the trade, in R (initial risk), from the candles."""
    entry, stop = trade["entry_price"], trade.get("initial_stop") or trade.get("stop_loss")
    if not stop or entry == stop:
        return None
    risk = abs(entry - stop)
    times = times if times is not None else [b.ts for b in bars]
    window = bars[bisect.bisect_left(times, trade["entry_ts"]):bisect.bisect_right(times, trade["exit_ts"])]
    if not window:
        return None
    if trade["side"] == "long":
        mfe = (max(b.high for b in window) - entry) / risk
        mae = (entry - min(b.low for b in window)) / risk
    else:
        mfe = (entry - min(b.low for b in window)) / risk
        mae = (max(b.high for b in window) - entry) / risk
    return max(0.0, mfe), max(0.0, mae)


def _atr_series(bars: list[Bar], n: int = 14) -> list[float | None]:
    out, trs = [], []
    for i, b in enumerate(bars):
        tr = b.high - b.low if i == 0 else max(b.high - b.low, abs(b.high - bars[i - 1].close),
                                               abs(b.low - bars[i - 1].close))
        trs.append(tr)
        out.append(sum(trs[-n:]) / n if len(trs) >= n else None)
    return out


def _sma_series(bars: list[Bar], n: int) -> list[float | None]:
    out, total = [], 0.0
    for i, b in enumerate(bars):
        total += b.close
        if i >= n:
            total -= bars[i - n].close
        out.append(total / n if i >= n - 1 else None)
    return out


def drawdowns(equity: list[dict], top: int = 3) -> list[dict]:
    """The deepest drawdown periods: peak, bottom, recovery (or None) and depth in %."""
    episodes, peak_v, peak_t, bottom_v, bottom_t = [], None, None, None, None
    for p in equity:
        t, v = p["time"], p["value"]
        if peak_v is None or v >= peak_v:
            if peak_v is not None and bottom_v is not None and bottom_v < peak_v:
                episodes.append({"peak": peak_t, "bottom": bottom_t, "recovered": t,
                                 "depth": (peak_v - bottom_v) / peak_v * 100})
            peak_v, peak_t, bottom_v, bottom_t = v, t, None, None
        elif bottom_v is None or v < bottom_v:
            bottom_v, bottom_t = v, t
    if peak_v is not None and bottom_v is not None and bottom_v < peak_v:
        episodes.append({"peak": peak_t, "bottom": bottom_t, "recovered": None,
                         "depth": (peak_v - bottom_v) / peak_v * 100})
    return sorted(episodes, key=lambda e: -e["depth"])[:top]


def streaks(trades: list[dict]) -> tuple[int, int]:
    best = worst = cur_w = cur_l = 0
    for t in trades:
        if t["pnl"] > 0:
            cur_w, cur_l = cur_w + 1, 0
        else:
            cur_w, cur_l = 0, cur_l + 1
        best, worst = max(best, cur_w), max(worst, cur_l)
    return best, worst


# ---------- the document ----------

def build_report(run: dict, bars: list[Bar] | None, tz_name: str = "Europe/Amsterdam",
                 strategy_changed: bool = False) -> str:
    tz = ZoneInfo(tz_name)
    s, m = run["settings"], run["metrics"]
    trades = run.get("trades") or []
    events = run.get("events") or []
    instrument = INSTRUMENTS.get(s.get("symbol"))
    digits = instrument.digits if instrument else 5
    times = [b.ts for b in bars] if bars else []

    def when(ts):
        return datetime.fromtimestamp(ts, tz).strftime("%d-%m-%Y %H:%M") if ts else "—"

    title = f"{s.get('strategy_label', s.get('strategy', run.get('name', 'Backtest')))} {s.get('version', '')}".strip()
    out = [f"# Backtest-analyse: {title} op {s.get('symbol')} {s.get('timeframe')}", ""]
    out.append(f"Run #{run['id']} · gemaakt {when(run.get('created_at'))} · periode {s.get('start')} t/m {s.get('end')} · "
               f"tijden in {tz_name}")
    out.append("")
    out.append("> Stuur dit bestand naar Claude met de vraag: *\"Analyseer deze backtest en maak een betere versie.\"*")
    out.append("")

    # Settings
    out += ["## Instellingen", ""]
    costs = s.get("costs") or {}
    rows = [
        ["Strategie", f"`{s.get('strategy', '—')}` ({s.get('strategy_label', '—')})"],
        ["Bron", run.get("source", "engine")],
        ["Code-vingerafdruk", f"`{run.get('code_hash') or '—'}`" + (" — **strategiebestand sindsdien gewijzigd**"
                                                                     if strategy_changed else "")],
        ["Instrument / timeframe", f"{s.get('symbol')} / {s.get('timeframe')}"],
        ["Periode", f"{s.get('start')} t/m {s.get('end')}"],
        ["Startkapitaal", f"€ {_n(s.get('capital'))}"],
        ["Risico per trade", _pct(s.get("risk_pct"))],
        ["Lotgrootte", "hele lots (realistisch)" if s.get("sizing_mode") == "realistic" else "exact (fractioneel)"],
        ["Hefboom", f"1:{s.get('leverage')}"],
        ["Spread / slippage", f"{_n(costs.get('spread'), 5)} / {_n(costs.get('slippage'), 5)} (prijs-eenheden)"],
        ["Commissie", f"€ {_n(costs.get('commission_per_lot'))} per lot per kant"],
        ["Financiering", _pct(costs.get("financing_pct")) + " per jaar"],
        ["Harde risicolimieten", _n(bool(s.get("apply_risk")))],
        ["Out-of-sample", _pct(s.get("oos_pct"), 0) if s.get("oos_pct") else "uit"],
    ]
    out += [_table(["Instelling", "Waarde"], rows), ""]
    params = s.get("params") or {}
    if params:
        out += ["### Parameters van de strategie", "", _table(["Parameter", "Waarde"],
                                                                [[f"`{k}`", _n(v)] for k, v in params.items()]), ""]

    # Results
    out += ["## Resultaat", ""]
    keys = [
        ("total_return_pct", "Totaal rendement", "%"), ("buy_hold_pct", "Kopen en vasthouden", "%"),
        ("max_drawdown_pct", "Max. drawdown", "%"), ("sharpe", "Sharpe", ""), ("sortino", "Sortino", ""),
        ("trades", "Trades", ""), ("winrate_pct", "Winrate", "%"), ("profit_factor", "Profit factor", ""),
        ("avg_trade", "Gemiddelde trade (€)", ""), ("expectancy_r", "Verwachting per trade (R)", ""),
        ("avg_win", "Gemiddelde winst (€)", ""), ("avg_loss", "Gemiddeld verlies (€)", ""),
        ("costs_total", "Totale kosten (€)", ""), ("exposure_pct", "Tijd in de markt", "%"),
        ("skipped_signals", "Overgeslagen signalen", ""), ("risk_events", "Ingrepen risicoregels", ""),
    ]
    rows = []
    for key, label, unit in keys:
        if key in m:
            v = m[key]
            rows.append([label, _pct(v, 2) if unit == "%" else _n(v)])
    extra = [k for k in m if k not in {k for k, _, _ in keys}]
    rows += [[f"`{k}`", _n(m[k])] for k in extra if not isinstance(m[k], (dict, list))]
    out += [_table(["Kerncijfer", "Waarde"], rows), ""]
    warnings = run.get("warnings") or []
    if warnings:
        out += ["### Waarschuwingen", ""] + [f"- {w}" for w in warnings] + [""]
    oos = run.get("oos")
    if oos and not oos.get("error"):
        out += ["### In-sample tegenover out-of-sample", ""]
        rows = []
        for key, label in (("total_return_pct", "Rendement %"), ("max_drawdown_pct", "Max. drawdown %"),
                           ("sharpe", "Sharpe"), ("trades", "Trades"), ("winrate_pct", "Winrate %"),
                           ("profit_factor", "Profit factor"), ("expectancy_r", "Verwachting (R)")):
            rows.append([label, _n(oos["in_sample"].get(key)), _n(oos["out_of_sample"].get(key))])
        out += [f"Splitsing op {when(oos.get('split_ts'))}.", "", _table(["", "In-sample", "Out-of-sample"], rows), ""]
        out += [f"- {n}" for n in oos.get("notes", [])] + [""]

    if not trades:
        out += ["## Trades", "", "Deze backtest had geen trades.", ""]
        return "\n".join(out)

    # Breakdown
    out += ["## Long tegenover short", "", _table(STATS_HEADERS, _stats_rows({
        "long": [t for t in trades if t["side"] == "long"], "short": [t for t in trades if t["side"] == "short"]})), ""]

    reasons: dict[str, list] = {}
    for t in trades:
        reasons.setdefault(t.get("exit_reason") or "—", []).append(t)
    out += ["## Uitstapredenen", "", _table(STATS_HEADERS, _stats_rows(reasons)), ""]

    buckets = [0] * (len(R_BUCKETS) + 1)
    with_r = [t for t in trades if t.get("r_multiple") is not None]
    for t in with_r:
        buckets[sum(1 for edge in R_BUCKETS if t["r_multiple"] > edge)] += 1
    labels = [f"≤ {_n(R_BUCKETS[0], 1)}"] + [f"{_n(a, 1)} tot {_n(b, 1)}" for a, b in zip(R_BUCKETS, R_BUCKETS[1:])] \
        + [f"> {_n(R_BUCKETS[-1], 1)}"]
    out += ["## Verdeling van de uitkomst in R", "",
            "R = resultaat gedeeld door het risico bij instap (afstand tot de oorspronkelijke stop-loss).", "",
            _table(["R", "Trades", "Aandeel"], [[lab, c, _pct(c / len(with_r) * 100)] for lab, c in zip(labels, buckets)])
            if with_r else "Geen R bekend (geen stop-loss in de gegevens).", ""]

    # Excursions
    if bars:
        ex = [(t, excursions(t, bars, times)) for t in trades]
        ex = [(t, e) for t, e in ex if e]
        if ex:
            losers = [(t, e) for t, e in ex if t["pnl"] <= 0]
            winners = [(t, e) for t, e in ex if t["pnl"] > 0]
            out += ["## Hoe ver liepen trades mee en tegen? (MFE / MAE)", "",
                    "MFE = grootste beweging in de goede richting, MAE = grootste beweging tegen de trade in, "
                    "beide in R, gemeten op de candles van de trade.", ""]
            rows = []
            for name, group in (("Winnaars", winners), ("Verliezers", losers), ("Alle trades", ex)):
                if not group:
                    continue
                mfes = [e[0] for _, e in group]
                maes = [e[1] for _, e in group]
                rows.append([name, len(group), _n(statistics.median(mfes)), _n(statistics.mean(mfes)),
                             _n(statistics.median(maes)), _n(max(maes))])
            out += [_table(["Groep", "Trades", "MFE mediaan", "MFE gemiddeld", "MAE mediaan", "MAE max"], rows), ""]
            if losers:
                rows = [[f"≥ {_n(level, 2)} R", sum(1 for _, e in losers if e[0] >= level),
                         _pct(sum(1 for _, e in losers if e[0] >= level) / len(losers) * 100)]
                        for level in (0.25, 0.5, 1.0, 1.5, 2.0)]
                out += ["### Verliezers die eerst in de winst stonden", "",
                        "Hoeveel verliezende trades eerst minstens zoveel winst toonden voordat ze verloren:", "",
                        _table(["Eerst in de winst", "Verliezers", "Aandeel"], rows), ""]
            if winners:
                rows = [[f"≥ {_n(level, 2)} R", sum(1 for _, e in winners if e[1] >= level),
                         _pct(sum(1 for _, e in winners if e[1] >= level) / len(winners) * 100)]
                        for level in (0.25, 0.5, 0.75, 0.9)]
                out += ["### Winnaars die eerst bijna de stop raakten", "",
                        _table(["Eerst tegen de trade in", "Winnaars", "Aandeel"], rows), ""]

    held_w = [t["bars_held"] for t in trades if t["pnl"] > 0 and t.get("bars_held") is not None]
    held_l = [t["bars_held"] for t in trades if t["pnl"] <= 0 and t.get("bars_held") is not None]
    out += ["## Duur van trades (in candles)", "", _table(["Groep", "Mediaan", "Gemiddeld", "Langste"], [
        [name, _n(statistics.median(g)), _n(statistics.mean(g)), _n(max(g))]
        for name, g in (("Winnaars", held_w), ("Verliezers", held_l)) if g]), ""]

    by_hour: dict[str, list] = {}
    by_day: dict[str, list] = {}
    by_month: dict[str, list] = {}
    for t in sorted(trades, key=lambda x: x["entry_ts"]):
        dt = datetime.fromtimestamp(t["entry_ts"], tz)
        by_hour.setdefault(f"{dt.hour:02d}:00", []).append(t)
        by_day.setdefault(WEEKDAYS[dt.weekday()], []).append(t)
        by_month.setdefault(dt.strftime("%Y-%m"), []).append(t)
    out += ["## Per uur van instap", "", _table(STATS_HEADERS, _stats_rows(dict(sorted(by_hour.items())))), ""]
    out += ["## Per weekdag van instap", "",
            _table(STATS_HEADERS, _stats_rows({d: by_day[d] for d in WEEKDAYS if d in by_day})), ""]
    out += ["## Per maand", "", _table(STATS_HEADERS, _stats_rows(by_month)), ""]

    # Market conditions at entry
    if bars and len(bars) > 220:
        atr = _atr_series(bars)
        sma200 = _sma_series(bars, 200)
        sma50 = _sma_series(bars, 50)
        vols, rows_trend = [], {"met de trend mee (prijs en SMA50 boven/onder SMA200)": [],
                                "tegen de trend in": [], "geen duidelijke trend": [], "onbekend (te vroeg)": []}
        for t in trades:
            i = bisect.bisect_right(times, t["entry_ts"]) - 1     # the candle of the entry
            if i < 1:
                rows_trend["onbekend (te vroeg)"].append(t)
                continue
            j = i - 1     # the closed candle the decision was based on
            if atr[j]:
                vols.append((atr[j] / bars[j].close, t))
            if sma200[j] is None or sma50[j] is None:
                rows_trend["onbekend (te vroeg)"].append(t)
                continue
            up = bars[j].close > sma200[j] and sma50[j] > sma200[j]
            down = bars[j].close < sma200[j] and sma50[j] < sma200[j]
            if (up and t["side"] == "long") or (down and t["side"] == "short"):
                rows_trend["met de trend mee (prijs en SMA50 boven/onder SMA200)"].append(t)
            elif up or down:
                rows_trend["tegen de trend in"].append(t)
            else:
                rows_trend["geen duidelijke trend"].append(t)
        out += ["## Marktomstandigheden bij instap", "", "### Trend (SMA50 en SMA200 op dezelfde timeframe)", "",
                _table(STATS_HEADERS, _stats_rows(rows_trend)), ""]
        if len(vols) >= 9:
            vols.sort(key=lambda x: x[0])
            third = len(vols) // 3
            groups = {"rustig (laagste derde ATR/koers)": [t for _, t in vols[:third]],
                      "normaal": [t for _, t in vols[third:2 * third]],
                      "druk (hoogste derde ATR/koers)": [t for _, t in vols[2 * third:]]}
            out += ["### Beweeglijkheid (ATR14 als % van de koers)", "", _table(STATS_HEADERS, _stats_rows(groups)), ""]

    best, worst = streaks(sorted(trades, key=lambda x: x["exit_ts"]))
    cost_parts: dict[str, float] = {}
    for t in trades:
        for k, v in (t.get("costs") or {}).items():
            cost_parts[k] = cost_parts.get(k, 0.0) + v
    gross = sum(t["pnl"] + (t.get("costs_total") or 0.0) for t in trades)
    out += ["## Reeksen en kosten", "", _table(["", "Waarde"], [
        ["Langste reeks winnaars", best], ["Langste reeks verliezers", worst],
        ["Resultaat vóór kosten (€)", _n(gross)], ["Resultaat na kosten (€)", _n(sum(t["pnl"] for t in trades))],
        *[[f"Kosten: {k}", f"€ {_n(v)}"] for k, v in cost_parts.items()],
    ]), ""]

    dds = drawdowns(run.get("equity") or [])
    if dds:
        out += ["## Diepste drawdowns", "", _table(["Top", "Dal", "Hersteld", "Diepte"], [
            [when(d["peak"]), when(d["bottom"]), when(d["recovered"]) if d["recovered"] else "nog niet",
             _pct(d["depth"], 2)] for d in dds]), ""]

    skip: dict[str, int] = {}
    for e in events:
        if e.get("kind") in ("skip", "risk", "warning", "error"):
            key = e.get("reason_code") or e["message"].split(":")[0][:80]
            skip[key] = skip.get(key, 0) + 1
    if skip:
        out += ["## Overgeslagen signalen, waarschuwingen en ingrepen", "",
                _table(["Reden", "Aantal"], [[k, v] for k, v in sorted(skip.items(), key=lambda x: -x[1])]), ""]

    # All trades, machine-readable
    out += ["## Alle trades", "", "Komma-gescheiden; tijden in UTC (unix) en in " + tz_name + ".", "", "```csv",
            "nr,richting,instap_unix,instap,instapprijs,uitstap_unix,uitstap,uitstapprijs,lots,stop_start,stop_eind,"
            "take_profit,reden_in,reden_uit,resultaat_eur,rendement_pct,r,kosten_eur,candles"]
    for t in trades:
        out.append(",".join(str(x) for x in [
            t["id"], t["side"], t["entry_ts"], when(t["entry_ts"]), round(t["entry_price"], digits), t["exit_ts"],
            when(t["exit_ts"]), round(t["exit_price"], digits), round(t["lots"], 4),
            round(t.get("initial_stop") or t.get("stop_loss") or 0, digits), round(t.get("stop_loss") or 0, digits),
            "" if t.get("take_profit") is None else round(t["take_profit"], digits),
            '"' + (t.get("entry_reason") or "").replace('"', "'") + '"',
            '"' + (t.get("exit_reason") or "").replace('"', "'") + '"',
            round(t["pnl"], 2), round(t.get("return_pct") or 0.0, 3),
            "" if t.get("r_multiple") is None else round(t["r_multiple"], 3),
            round(t.get("costs_total") or 0.0, 2), "" if t.get("bars_held") is None else t["bars_held"],
        ]))
    out += ["```", "", f"_Gemaakt door het Trading Dashboard op {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC._", ""]
    return "\n".join(out)


def report_filename(run: dict) -> str:
    s = run["settings"]
    strategy = (s.get("strategy") or "import").replace("@", "_")
    return f"backtest-{strategy}-{s.get('symbol')}-{s.get('timeframe')}-{s.get('start')}_{s.get('end')}-run{run['id']}.md"
