"""Bollinger mean reversion: buy a close back inside the lower band, take profit at the middle band."""

from __future__ import annotations

from .base import History, Param, Position, Signal, Strategy
from .indicators import atr, sma, stdev


class BollingerReversionV1(Strategy):
    name = "bollinger_reversion"
    version = "v1"
    label = "Bollinger-terugkeer"
    description = (
        "Gaat long als de koers onder de onderste Bollinger-band zakte en weer binnen de band sluit, en short "
        "andersom bij de bovenste band. De positie sluit zodra de koers het midden (het gemiddelde) bereikt, "
        "of op de ATR-stop-loss. Een strategie die rekent op terugkeer naar het gemiddelde: veel kleine winsten."
    )
    params = (
        Param("period", "Periode (bars)", 20, min=2, max=500, step=1),
        Param("mult", "Bandbreedte (× standaardafwijking)", 2.0, min=0.5, max=5, step=0.1),
        Param("atr_period", "ATR-periode (bars)", 14, min=2, max=200, step=1),
        Param("atr_stop", "Stop-loss (× ATR)", 2.0, min=0.2, max=20, step=0.1),
        Param("allow_short", "Ook short gaan", True),
    )

    def warmup(self) -> int:
        return max(self.p["period"] + 1, self.p["atr_period"] + 1)

    def _bands(self, history: History, offset: int):
        mid, dev = sma(history, self.p["period"], offset), stdev(history, self.p["period"], offset)
        if mid is None or dev is None:
            return None
        return mid - self.p["mult"] * dev, mid, mid + self.p["mult"] * dev

    def on_bar(self, history: History, position: Position) -> Signal | None:
        now, before = self._bands(history, 0), self._bands(history, 1)
        risk = atr(history, self.p["atr_period"])
        if now is None or before is None or risk is None:
            return None
        lower, mid, upper = now
        prev_lower, _, prev_upper = before
        close, prev_close = history[-1].close, history[-2].close
        stop = self.p["atr_stop"] * risk

        if position.side == "long" and close >= mid:
            return Signal("flat", reason="Koers terug bij het gemiddelde")
        if position.side == "short" and close <= mid:
            return Signal("flat", reason="Koers terug bij het gemiddelde")
        if position.is_flat:
            if prev_close < prev_lower and close > lower:
                return Signal("long", stop_loss=close - stop, reason="Terug binnen de onderste band")
            if self.p["allow_short"] and prev_close > prev_upper and close < upper:
                return Signal("short", stop_loss=close + stop, reason="Terug binnen de bovenste band")
        return None
