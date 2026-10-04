"""Import the "List of trades" CSV export from TradingView's Strategy Tester.

TradingView has used several layouts over the years (column names, currency
suffixes, "Profit" vs "Net P&L"). Columns are therefore found by name, not position.
Each trade appears as two rows: an entry row and an exit row with the same trade number.
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from datetime import timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..backtest.metrics import compute_metrics, drawdown_curve

DATE_FORMATS = (
    "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
    "%b %d, %Y %H:%M", "%b %d, %Y, %H:%M", "%d %b %Y %H:%M", "%b %d, %Y",
)
MAX_BYTES = 10_000_000


class ImportError_(ValueError):
    """A Dutch, user-facing explanation of why the file could not be read."""


def _find(headers: list[str], *tests) -> int | None:
    for test in tests:
        for i, h in enumerate(headers):
            if test(h):
                return i
    return None


def _columns(raw_headers: list[str]) -> dict:
    h = [x.strip().lower() for x in raw_headers]
    pct = lambda x: "%" in x  # noqa: E731
    cols = {
        "num": _find(h, lambda x: x.startswith("trade #"), lambda x: x in ("trade", "trade number", "trade no")),
        "type": _find(h, lambda x: x == "type"),
        "signal": _find(h, lambda x: x == "signal"),
        "time": _find(h, lambda x: x.startswith("date"), lambda x: "time" in x),
        "price": _find(h, lambda x: x.startswith("price")),
        "qty": _find(h, lambda x: x.startswith("contracts"), lambda x: "qty" in x, lambda x: x.startswith("quantity")),
        "profit": _find(h, lambda x: (x.startswith("profit") or x.startswith("net p&l")) and not pct(x)),
        "profit_pct": _find(h, lambda x: (x.startswith("profit") or x.startswith("net p&l")) and pct(x)),
        "cum": _find(h, lambda x: x.startswith("cum") and not pct(x)),
        "cum_pct": _find(h, lambda x: x.startswith("cum") and pct(x)),
    }
    missing = [name for name in ("num", "type", "time", "price") if cols[name] is None]
    if missing:
        labels = {"num": "Trade #", "type": "Type", "time": "Date/Time", "price": "Price"}
        raise ImportError_(
            "Dit lijkt geen 'List of trades'-export van TradingView: de kolom(men) "
            + ", ".join(labels[m] for m in missing) + " ontbreken."
        )
    price_header = raw_headers[cols["price"]].strip()
    match = re.search(r"\b([A-Z]{3})\b", price_header)
    cols["currency"] = match.group(1) if match else ""
    return cols


def _number(text: str, decimal_comma: bool) -> float | None:
    t = (text or "").strip().replace("−", "-").replace(" ", "").replace(" ", "")
    t = re.sub(r"[^0-9,.\-eE+]", "", t)
    if not t or t in "-+":
        return None
    if "," in t and "." in t:
        t = t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".") if decimal_comma else t.replace(",", "")
    try:
        return float(t)
    except ValueError:
        return None


def _timestamp(text: str, tz: ZoneInfo) -> int:
    value = text.strip()
    for fmt in DATE_FORMATS:
        try:
            dt = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return int(dt.replace(tzinfo=tz).timestamp())
    raise ImportError_(f"Onbekend datumformaat in de CSV: '{value}'.")


def parse_trades(content: bytes | str, timezone: str = "Europe/Amsterdam") -> dict:
    """Returns {"trades": [...], "currency": str, "capital": float|None, "open_trades": int}."""
    if isinstance(content, bytes):
        if len(content) > MAX_BYTES:
            raise ImportError_("Het bestand is te groot (maximaal 10 MB).")
        for encoding in ("utf-8-sig", "utf-16", "latin-1"):
            try:
                content = content.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
    text = content.lstrip("﻿")
    if not text.strip():
        raise ImportError_("Het bestand is leeg.")
    try:
        tz = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise ImportError_(f"Onbekende tijdzone '{timezone}'.") from None

    first_line = text.splitlines()[0]
    delimiter = max((",", ";", "\t"), key=first_line.count)
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=delimiter) if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise ImportError_("Het bestand bevat geen trades.")
    cols = _columns(rows[0])
    decimal_comma = delimiter == ";"

    def cell(row, name):
        i = cols.get(name)
        return row[i] if i is not None and i < len(row) else ""

    grouped: dict[str, dict] = {}
    order: list[str] = []
    for row in rows[1:]:
        num = cell(row, "num").strip()
        kind = cell(row, "type").strip().lower()
        if not num or not kind:
            continue
        if num not in grouped:
            grouped[num] = {}
            order.append(num)
        role = "entry" if kind.startswith("entry") else "exit" if kind.startswith("exit") else None
        if role is None:
            continue
        grouped[num][role] = row
        grouped[num]["side"] = "long" if "long" in kind else "short" if "short" in kind else None

    trades, open_trades, last_cum, last_cum_pct = [], 0, None, None
    for num in order:
        g = grouped[num]
        entry, exit_ = g.get("entry"), g.get("exit")
        if entry is None or exit_ is None or g.get("side") is None:
            open_trades += 1
            continue
        if cell(exit_, "signal").strip().lower() == "open":
            open_trades += 1
            continue
        pnl = _number(cell(exit_, "profit"), decimal_comma)
        if pnl is None:
            pnl = _number(cell(entry, "profit"), decimal_comma)
        pnl_pct = _number(cell(exit_, "profit_pct"), decimal_comma)
        if pnl_pct is None:
            pnl_pct = _number(cell(entry, "profit_pct"), decimal_comma)
        entry_price = _number(cell(entry, "price"), decimal_comma)
        exit_price = _number(cell(exit_, "price"), decimal_comma)
        if entry_price is None or exit_price is None:
            raise ImportError_(f"Trade {num}: de prijs kon niet gelezen worden.")
        qty = _number(cell(entry, "qty"), decimal_comma)
        trades.append({
            "id": len(trades) + 1,
            "tv_number": num,
            "side": g["side"],
            "lots": qty,
            "entry_ts": _timestamp(cell(entry, "time"), tz),
            "entry_price": entry_price,
            "exit_ts": _timestamp(cell(exit_, "time"), tz),
            "exit_price": exit_price,
            "entry_reason": cell(entry, "signal").strip(),
            "exit_reason": cell(exit_, "signal").strip() or "Uitstap",
            "pnl": pnl if pnl is not None else 0.0,
            "tv_profit_pct": pnl_pct,  # TradingView's own %, relative to the position, not the account
            "stop_loss": None,
            "take_profit": None,
        })
        cum = _number(cell(exit_, "cum"), decimal_comma)
        cum_pct = _number(cell(exit_, "cum_pct"), decimal_comma)
        if cum is not None and cum_pct is not None:
            last_cum, last_cum_pct = cum, cum_pct

    if not trades:
        raise ImportError_("Er staan geen afgesloten trades in dit bestand.")
    trades.sort(key=lambda t: (t["entry_ts"], t["id"]))
    for i, t in enumerate(trades, start=1):
        t["id"] = i

    capital = None
    if last_cum is not None and last_cum_pct:
        capital = last_cum / (last_cum_pct / 100)
        if capital <= 0:
            capital = None
    return {"trades": trades, "currency": cols["currency"], "capital": capital, "open_trades": open_trades}


def build_run(parsed: dict, symbol: str, timeframe: str, name: str, capital: float | None) -> dict:
    """A run result in the same shape as an engine backtest, so it can be listed and compared."""
    trades = parsed["trades"]
    capital = parsed["capital"] or capital
    if not capital or capital <= 0:
        raise ImportError_("Vul het startkapitaal in dat je in TradingView gebruikte.")

    equity = [(trades[0]["entry_ts"], capital)]
    balance = capital
    for t in sorted(trades, key=lambda t: t["exit_ts"]):
        equity_before = balance
        balance += t["pnl"]
        t["return_pct"] = t["pnl"] / equity_before * 100 if equity_before else 0.0
        equity.append((t["exit_ts"], balance))
    equity.sort(key=lambda p: p[0])

    shaped = [{**t, "r_multiple": 0.0, "costs_total": 0.0, "bars_held": 0} for t in trades]
    metrics = compute_metrics(equity, shaped, capital, 252, 0, 0)
    metrics.update(expectancy_r=None, costs_total=None, exposure_pct=None, avg_bars_held=None,
                   buy_hold_pct=None, skipped_signals=0)
    for t in trades:
        t.update(r_multiple=None, costs={}, costs_total=None, bars_held=None)

    def day(ts: int) -> str:
        return datetime.fromtimestamp(ts, dt_timezone.utc).strftime("%Y-%m-%d")

    warnings = ["Geïmporteerd uit TradingView. Kosten, Sharpe en Sortino zijn uit de trades afgeleid en kunnen "
                "afwijken van wat TradingView zelf toont."]
    if parsed["open_trades"]:
        warnings.append(f"{parsed['open_trades']} open trade(s) zonder uitstap zijn niet meegeteld.")
    dd = drawdown_curve(equity)
    return {
        "settings": {
            "symbol": symbol, "timeframe": timeframe,
            "start": day(trades[0]["entry_ts"]), "end": day(max(t["exit_ts"] for t in trades)),
            "strategy": "tradingview", "strategy_label": f"TradingView: {name}", "version": "",
            "params": {}, "capital": capital, "currency": parsed["currency"] or "?",
            "capital_inferred": parsed["capital"] is not None,
        },
        "metrics": metrics,
        "trades": trades,
        "equity": [{"time": ts, "value": round(v, 2)} for ts, v in equity],
        "drawdown": [{"time": ts, "value": round(v, 3)} for ts, v in dd],
        "events": [],
        "warnings": warnings,
    }
