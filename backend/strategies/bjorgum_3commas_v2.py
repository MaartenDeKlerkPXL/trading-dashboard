"""3Commas Bot by Bjorgum, converted line by line from the original Pine Script (v5).

Source: https://www.tradingview.com/script/MvlwAzSg-3Commas-Bot/ (open source). The parts that decide trades:

    ma1 = getMA(maType1, maLength1)                    // default EMA 21
    ma2 = getMA(maType2, maLength2)                    // default EMA 50
    atr = ta.atr(atrLen)                               // 14
    lowestLow   = ta.lowest (low,  swinglookback)      // 5, including the current bar
    highestHigh = ta.highest(high, swinglookback)
    timeFilter  = (useTimeFilter and not isInSession(timeSession)) or not useTimeFilter   // session in GMT-6

    validLongEntry  = ta.crossover (ma1, ma2) and not na(atr)
    validShortEntry = ta.crossunder(ma1, ma2) and not na(atr)

    if LONG and trailStop and lookForExit           // ratchet: only ever up
        trail = (trailSource == "Close" ? close[1] : trailSource == "Open" ? open[1] : lowestLow) - atr * trailStopSize
        trailingStop := max(trailingStop, trail)
    (and mirrored for SHORT with highestHigh, + atr, only ever down)

    longStop  = lowestLow   - atr * RiskM          shortStop  = highestHigh + atr * RiskM
    longlimit = close + RnR * (close - longStop)   shortlimit = close - RnR * (shortStop - close)

    if validShortEntry and (FLAT or (LONG and FLIP)) and withinTime and shortTrades
        tradeStopPrice := shortStop, tradeTargetPrice := useLimit ? shortlimit : na
        tradeExitTriggerPrice := close - RnR * (shortStop - close) * rrExit
        lookForExit := false, trailingStop := tradeStopPrice
    (and mirrored for long)

    lookForExit := true  when  (in position and rrExit != 0 and price reached tradeExitTriggerPrice and trailStop)
                         or    (rrExit == 0 and trailStop)

    strategy.entry("Long", strategy.long, when=validLongEntry)
    strategy.exit("Long Exit", stop = trailStop ? trailingStop : tradeStopPrice, limit = tradeTargetPrice)
    (and mirrored for short)

How the Pine "var" state is rebuilt here: a strategy in this dashboard keeps no memory between candles
(paper and live start from the saved account every round). Everything the Pine script remembers is
recomputed from the price history and the open position: the stop, target and trail trigger from the
signal candle just before the entry, whether trailing has started from the highs/lows since the entry,
and the ratchet from the position's current stop-loss (which only ever moves towards the price).

PINE DIFFERENCES (behaviour that is not reproduced 1-to-1):
1. Position size: Pine trades a fixed 10,000 USD per trade (default_qty_type=strategy.cash). Here size follows
   the risk per trade (2% of equity to the stop-loss). Entry and exit moments match, amounts and percentages
   do not. Pine's 0.05% commission and 1-tick slippage are replaced by this dashboard's cost model.
2. "Set Max Total DrawDown" (strategy.risk.max_drawdown) is not a strategy setting here: the dashboard's hard
   risk limits (max daily loss, max position size, kill switch) take its place.
3. Start/End filter dates: choose the period on the Backtest page instead.
4. Stop-loss and target hit in the same candle: TradingView guesses the order from the candle's shape; this
   dashboard assumes the stop-loss came first (cautious). In paper and live trading, stops are checked per
   minute or by the broker, so this only matters in backtests.
5. Indicators start at the first bar of the loaded data. TradingView loads more history, so EMA-based values
   (EMA, HEMA, DEMA, T3) differ slightly at the very start of a period. Run backtests with some extra history.
6. VWAP restarts every trading day at 17:00 New York time (the forex/metals day). On crypto TradingView
   restarts at 00:00 UTC.
7. Prices: TradingView's FXCM data and this dashboard's data (Dukascopy, or BlackBull in live) differ slightly,
   so an occasional crossing can fall one candle earlier or later.
8. 3Commas webhook messages are not needed: the dashboard places the orders itself.
"""

from __future__ import annotations

from .base import History, Param, Position, Signal, Strategy
from .pine import PINE_MA_TYPES, atr, moving_average, warmup_bars

GMT_MINUS_6 = -6 * 3600


def in_session(ts: int, start_hhmm: int, end_hhmm: int) -> bool:
    """Pine's time(timeframe.period, "HHMM-HHMM:1234567", "GMT-6"): is the bar's open time inside the session?"""
    minute = ((ts + GMT_MINUS_6) // 60) % 1440
    start = start_hhmm // 100 * 60 + start_hhmm % 100
    end = end_hhmm // 100 * 60 + end_hhmm % 100
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


class Bjorgum3CommasV2(Strategy):
    name = "bjorgum_3commas"
    version = "v2"
    label = "3Commas Bot (Bjorgum, exact uit Pine)"
    description = (
        "Regel voor regel omgezet uit de Pine-code van Bjorgums '3Commas Bot'. Instap: gemiddelde 1 kruist "
        "gemiddelde 2 (standaard EMA 21 en EMA 50), omhoog voor long, omlaag voor short. Stop-loss: onder de "
        "laagste koers (long) of boven de hoogste koers (short) van de laatste 5 candles, plus 1× de ATR. "
        "Koersdoel: standaard 1:1. Uitstappen gebeurt alleen via stop-loss, koersdoel of de optionele trailing "
        "stop. Een tegengestelde kruising wordt genegeerd zolang er een positie openstaat, tenzij "
        "'Omkeertrades toestaan' aan staat."
    )
    params = (
        Param("long_trades", "Long trades (Detect Long Trades)", True),
        Param("short_trades", "Short trades (Detect Short Trades)", True),
        Param("use_limit", "Koersdoel gebruiken (Use Limit exit)", True,
              help="Uit: geen vaste take-profit. Zet dan de trailing stop aan, anders sluit alleen de stop-loss."),
        Param("trail_stop", "ATR-trailing stop (Use ATR Trailing Stop)", False),
        Param("flip", "Omkeertrades toestaan (Allow Reversal Trades)", False,
              help="Aan: een tegengestelde kruising draait een open positie om. Uit: die kruising wordt genegeerd."),
        Param("rr", "Koersdoel × risico (Reward to Risk Ratio)", 1.0, min=0, max=20, step=0.1),
        Param("risk_adj", "Extra afstand stop × ATR (Risk Adjustment)", 1.0, min=0, max=20, step=0.1),
        Param("swing_lookback", "Bodem/top over (bars) (Swing Lookback)", 5, min=1, max=500, step=1),
        Param("atr_len", "ATR-periode (ATR length)", 14, min=1, max=500, step=1),
        Param("trail_mult", "Trailing stop × ATR (ATR Trailing Stop Multiplier)", 1.0, min=0, max=20, step=0.1),
        Param("trail_source", "Trailing stop gemeten vanaf (ATR Trailing Stop Source)", "High/Low",
              choices=("High/Low", "Close", "Open"),
              help="High/Low = laagste koers (long) of hoogste koers (short) van de bodem/top-periode; "
                   "Close/Open = slot- of openingskoers van de vorige candle."),
        Param("rr_exit", "Trailing start bij deel van het doel (R:R To Trigger Exit)", 0.0, min=0, max=10,
              step=0.05, help="0 = de trailing stop loopt vanaf de instap mee. 0,5 = pas als de koers halverwege "
                              "het koersdoel is."),
        Param("ma_type1", "Soort gemiddelde 1 (MA Type #1)", "EMA", choices=PINE_MA_TYPES),
        Param("ma_type2", "Soort gemiddelde 2 (MA Type #2)", "EMA", choices=PINE_MA_TYPES),
        Param("ma_len1", "Lengte gemiddelde 1 (MA Length #1)", 21, min=1, max=1000, step=1),
        Param("ma_len2", "Lengte gemiddelde 2 (MA Length #2)", 50, min=1, max=1000, step=1),
        Param("use_time_filter", "Tijdfilter gebruiken (Use Time Session Filter)", False),
        Param("ignore_from", "Geen nieuwe trades vanaf (UUMM, GMT-6)", 0, min=0, max=2359, step=1,
              help="Zoals in de Pine-code in GMT-6. 0 = 00:00 GMT-6 = 07:00 Nederlandse wintertijd (08:00 zomertijd)."),
        Param("ignore_until", "… tot (UUMM, GMT-6)", 300, min=0, max=2359, step=1,
              help="300 = 03:00 GMT-6 = 10:00 Nederlandse wintertijd (11:00 zomertijd)."),
    )

    def __init__(self, **values):
        super().__init__(**values)
        self.ma1 = moving_average(self.p["ma_type1"], self.p["ma_len1"])
        self.ma2 = moving_average(self.p["ma_type2"], self.p["ma_len2"])
        self.atr = atr(self.p["atr_len"])

    def validate(self) -> None:
        for key in ("ignore_from", "ignore_until"):
            if self.p[key] % 100 >= 60:
                raise ValueError("Tijden voor het tijdfilter schrijf je als UUMM, bijvoorbeeld 300 voor 03:00.")

    def warmup(self) -> int:
        return max(warmup_bars(self.p["ma_type1"], self.p["ma_len1"]),
                   warmup_bars(self.p["ma_type2"], self.p["ma_len2"]),
                   self.p["swing_lookback"], self.p["atr_len"]) + 1

    # ---------- the calculations of one bar ----------

    def _cross(self, history: History, i: int) -> str | None:
        """'up' for ta.crossover(ma1, ma2) on bar i, 'down' for ta.crossunder, else None."""
        if i < 1:
            return None
        a, b = self.ma1.update(history), self.ma2.update(history)
        a1, a0, b1, b0 = a[i], a[i - 1], b[i], b[i - 1]
        if None in (a1, a0, b1, b0) or self.atr.update(history)[i] is None:
            return None
        if a1 > b1 and a0 <= b0:
            return "up"
        if a1 < b1 and a0 >= b0:
            return "down"
        return None

    def _levels(self, history: History, i: int, side: str) -> tuple[float, float, float]:
        """Stop, limit and trail trigger as set on bar i (tradeStopPrice, longlimit, tradeExitTriggerPrice)."""
        lb = self.p["swing_lookback"]
        window = history[max(0, i + 1 - lb):i + 1]
        atr = self.atr.update(history)[i]
        close = history[i].close
        if side == "long":
            stop = min(b.low for b in window) - atr * self.p["risk_adj"]
            risk = close - stop
            return stop, close + self.p["rr"] * risk, close + self.p["rr"] * risk * self.p["rr_exit"]
        stop = max(b.high for b in window) + atr * self.p["risk_adj"]
        risk = stop - close
        return stop, close - self.p["rr"] * risk, close - self.p["rr"] * risk * self.p["rr_exit"]

    def _within_time(self, ts: int) -> bool:
        return not self.p["use_time_filter"] or not in_session(ts, self.p["ignore_from"], self.p["ignore_until"])

    # ---------- decisions ----------

    def on_bar(self, history: History, position: Position) -> Signal | None:
        last = len(history) - 1
        cross = self._cross(history, last)
        flat = position.side is None
        within = self._within_time(history[last].ts)

        if cross == "down" and (flat or (position.side == "long" and self.p["flip"])) and within \
                and self.p["short_trades"]:
            stop, limit, _ = self._levels(history, last, "short")
            return Signal("short", stop_loss=stop, take_profit=limit if self.p["use_limit"] else None,
                          reason=f"{self.p['ma_type1']} {self.p['ma_len1']} kruist onder "
                                 f"{self.p['ma_type2']} {self.p['ma_len2']}")
        if cross == "up" and (flat or (position.side == "short" and self.p["flip"])) and within \
                and self.p["long_trades"]:
            stop, limit, _ = self._levels(history, last, "long")
            return Signal("long", stop_loss=stop, take_profit=limit if self.p["use_limit"] else None,
                          reason=f"{self.p['ma_type1']} {self.p['ma_len1']} kruist boven "
                                 f"{self.p['ma_type2']} {self.p['ma_len2']}")

        if self.p["trail_stop"] and not flat and position.stop_loss is not None:
            return self._trail(history, position)
        return None

    def _entry_index(self, history: History, entry_ts: int) -> int | None:
        for i in range(len(history) - 1, -1, -1):
            if history[i].ts <= entry_ts:
                return i
        return None

    def _trail(self, history: History, position: Position) -> Signal | None:
        last = len(history) - 1
        entry_i = self._entry_index(history, position.entry_ts)
        if entry_i is None:
            return None
        if self.p["rr_exit"] != 0:
            # The signal candle: the last crossing in the direction of the position before the entry.
            want = "up" if position.side == "long" else "down"
            signal_i = next((k for k in range(entry_i - 1, max(0, entry_i - 6), -1)
                             if self._cross(history, k) == want), entry_i - 1)
            if signal_i < 0:
                return None
            _, _, trigger = self._levels(history, signal_i, position.side)
            # lookForExit is set at the close of a bar in the position; trailing starts on the bar after.
            since = history[entry_i:last]
            if position.side == "long" and not any(b.high >= trigger for b in since):
                return None
            if position.side == "short" and not any(b.low <= trigger for b in since):
                return None

        atr = self.atr.update(history)[last]
        lb = self.p["swing_lookback"]
        window = history[max(0, last + 1 - lb):last + 1]
        source = self.p["trail_source"]
        prev = history[last - 1] if last >= 1 else history[last]
        if position.side == "long":
            base = prev.close if source == "Close" else prev.open if source == "Open" else min(b.low for b in window)
            trail = base - atr * self.p["trail_mult"]
            if trail > position.stop_loss:
                return Signal("adjust", stop_loss=trail, reason="trailing stop")
        else:
            base = prev.close if source == "Close" else prev.open if source == "Open" else max(b.high for b in window)
            trail = base + atr * self.p["trail_mult"]
            if trail < position.stop_loss:
                return Signal("adjust", stop_loss=trail, reason="trailing stop")
        return None
