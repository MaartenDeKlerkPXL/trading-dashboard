"""RSI reversal, converted from a Pine Script strategy (TradingView's classic "RSI Strategy").

Original Pine Script (v6):

    strategy("RSI Strategy", overlay=true)
    length     = input(14)
    overSold   = input(30)
    overBought = input(70)
    vrsi = ta.rsi(close, length)
    co = ta.crossover(vrsi, overSold)
    cu = ta.crossunder(vrsi, overBought)
    if (not na(vrsi))
        if (co)
            strategy.entry("RsiLE", strategy.long, comment="RsiLE")
        if (cu)
            strategy.entry("RsiSE", strategy.short, comment="RsiSE")

PINE DIFFERENCES (behaviour that is not reproduced 1-to-1):
1. Stop-loss: the Pine original has none. This app requires one, so an ATR stop is added
   (atr_stop × ATR). Trades that hit it close earlier than in TradingView.
2. Position size: Pine trades 1 contract by default; here size follows the risk per trade.
   Entry and exit moments match, percentages do not.
3. RSI warm-up: like ta.rsi, the RSI is smoothed from the first bar of the loaded data. TradingView
   loads more history before the visible period, so the very first trades can differ slightly.
4. Fills: both fill at the open of the next bar (Pine default process_orders_on_close=false).
   calc_on_every_tick and the bar magnifier are not reproduced: this engine only sees closed bars.
5. Costs: Pine's default commission and slippage are 0; here spread, slippage and financing apply.
6. Pyramiding: Pine's default (0) allows one position per direction, the same as here.
"""

from __future__ import annotations

from .base import History, Param, Position, Signal, Strategy
from .indicators import RsiTracker, atr


class RsiReversalV1(Strategy):
    name = "rsi_reversal"
    version = "v1"
    label = "RSI-omkeer (uit Pine)"
    description = (
        "Omgezet uit TradingView's klassieke 'RSI Strategy'. Gaat long als de RSI van onder het oversold-niveau "
        "weer omhoog kruist, en short als de RSI van boven het overbought-niveau weer omlaag kruist. "
        "Een tegengesteld signaal draait de positie om. Toegevoegd: een ATR-stop-loss (Pine had er geen)."
    )
    params = (
        Param("length", "RSI-periode (bars)", 14, min=2, max=200, step=1),
        Param("oversold", "Oversold-niveau", 30.0, min=1, max=50, step=1),
        Param("overbought", "Overbought-niveau", 70.0, min=50, max=99, step=1),
        Param("atr_period", "ATR-periode (bars)", 14, min=2, max=200, step=1),
        Param("atr_stop", "Stop-loss (× ATR)", 3.0, min=0.2, max=20, step=0.1,
              help="Niet in de Pine-versie: toegevoegd omdat elke trade hier een stop-loss nodig heeft."),
        Param("allow_short", "Ook short gaan", True),
    )

    def validate(self) -> None:
        if self.p["oversold"] >= self.p["overbought"]:
            raise ValueError("Het oversold-niveau moet lager zijn dan het overbought-niveau.")

    def warmup(self) -> int:
        return max(self.p["length"] + 2, self.p["atr_period"] + 1)

    def on_bar(self, history: History, position: Position) -> Signal | None:
        if not hasattr(self, "_rsi"):
            self._rsi = RsiTracker(self.p["length"])
        values = self._rsi.update(history)
        risk = atr(history, self.p["atr_period"])
        if len(values) < 2 or risk is None:
            return None
        prev, now = values[-2], values[-1]
        close = history[-1].close
        stop = self.p["atr_stop"] * risk

        crossed_up = prev <= self.p["oversold"] < now          # ta.crossover(vrsi, overSold)
        crossed_down = prev >= self.p["overbought"] > now      # ta.crossunder(vrsi, overBought)
        if crossed_up and position.side != "long":
            return Signal("long", stop_loss=close - stop, reason=f"RSI kruist omhoog door {self.p['oversold']:g}")
        if crossed_down and position.side != "short":
            if not self.p["allow_short"]:
                return Signal("flat", reason="RSI kruist omlaag door overbought") if position.side else None
            return Signal("short", stop_loss=close + stop, reason=f"RSI kruist omlaag door {self.p['overbought']:g}")
        return None
