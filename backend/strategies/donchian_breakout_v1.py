"""Donchian breakout (turtle style): trade the break of the recent high/low, exit on a shorter channel."""

from __future__ import annotations

from .base import History, Param, Position, Signal, Strategy
from .indicators import atr, highest, lowest


class DonchianBreakoutV1(Strategy):
    name = "donchian_breakout"
    version = "v1"
    label = "Donchian-uitbraak"
    description = (
        "Gaat long als de koers boven het hoogste punt van de afgelopen N candles sluit, en short bij een slot "
        "onder het laagste punt. Uitstappen gebeurt bij een kortere tegengestelde uitbraak, of op de "
        "ATR-stop-loss. Een trendvolgende strategie: weinig winnaars, maar soms grote."
    )
    params = (
        Param("entry_period", "Instap-kanaal (bars)", 20, min=2, max=500, step=1),
        Param("exit_period", "Uitstap-kanaal (bars)", 10, min=2, max=500, step=1),
        Param("atr_period", "ATR-periode (bars)", 14, min=2, max=200, step=1),
        Param("atr_stop", "Stop-loss (× ATR)", 2.0, min=0.2, max=20, step=0.1),
        Param("allow_short", "Ook short gaan", True),
    )

    def validate(self) -> None:
        if self.p["exit_period"] > self.p["entry_period"]:
            raise ValueError("Het uitstap-kanaal mag niet langer zijn dan het instap-kanaal.")

    def warmup(self) -> int:
        return max(self.p["entry_period"] + 1, self.p["atr_period"] + 1)

    def on_bar(self, history: History, position: Position) -> Signal | None:
        upper, lower = highest(history, self.p["entry_period"]), lowest(history, self.p["entry_period"])
        exit_high, exit_low = highest(history, self.p["exit_period"]), lowest(history, self.p["exit_period"])
        risk = atr(history, self.p["atr_period"])
        if None in (upper, lower, exit_high, exit_low, risk):
            return None
        close = history[-1].close
        stop = self.p["atr_stop"] * risk

        if close > upper and position.side != "long":
            return Signal("long", stop_loss=close - stop, reason=f"Slot boven {self.p['entry_period']}-candle-hoog")
        if close < lower and position.side != "short":
            if self.p["allow_short"]:
                return Signal("short", stop_loss=close + stop, reason=f"Slot onder {self.p['entry_period']}-candle-laag")
            if position.side == "long":
                return Signal("flat", reason=f"Slot onder {self.p['entry_period']}-candle-laag")
            return None
        if position.side == "long" and close < exit_low:
            return Signal("flat", reason=f"Slot onder {self.p['exit_period']}-candle-laag")
        if position.side == "short" and close > exit_high:
            return Signal("flat", reason=f"Slot boven {self.p['exit_period']}-candle-hoog")
        return None
