"""SMA crossover: long when the fast average crosses above the slow one, short when it crosses below."""

from __future__ import annotations

from .base import History, Param, Position, Signal, Strategy
from .indicators import atr, sma


class SmaCrossV1(Strategy):
    name = "sma_cross"
    version = "v1"
    label = "SMA-crossover"
    description = (
        "Koopt als het snelle voortschrijdend gemiddelde boven het trage kruist en verkoopt (short) "
        "als het eronder kruist. De stop-loss ligt een aantal keer de ATR (gemiddelde beweeglijkheid) "
        "van de instapprijs. Een tegengestelde kruising sluit de positie en draait hem om."
    )
    params = (
        Param("fast", "Snel gemiddelde (bars)", 20, min=2, max=500, step=1),
        Param("slow", "Traag gemiddelde (bars)", 50, min=3, max=1000, step=1),
        Param("atr_period", "ATR-periode (bars)", 14, min=2, max=200, step=1),
        Param("atr_stop", "Stop-loss (× ATR)", 2.0, min=0.2, max=20, step=0.1),
        Param("take_profit_r", "Take-profit (× risico, 0 = geen)", 0.0, min=0, max=20, step=0.1,
              help="Bij 2 ligt de take-profit op twee keer de afstand tot de stop-loss."),
        Param("allow_short", "Ook short gaan", True),
    )

    def validate(self) -> None:
        if self.p["fast"] >= self.p["slow"]:
            raise ValueError("Het snelle gemiddelde moet korter zijn dan het trage.")

    def warmup(self) -> int:
        return max(self.p["slow"] + 1, self.p["atr_period"] + 1)

    def on_bar(self, history: History, position: Position) -> Signal | None:
        fast_now, slow_now = sma(history, self.p["fast"]), sma(history, self.p["slow"])
        fast_prev, slow_prev = sma(history, self.p["fast"], 1), sma(history, self.p["slow"], 1)
        risk = atr(history, self.p["atr_period"])
        if None in (fast_now, slow_now, fast_prev, slow_prev, risk):
            return None

        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now
        close = history[-1].close
        stop_distance = self.p["atr_stop"] * risk
        tp_r = self.p["take_profit_r"]

        if crossed_up and position.side != "long":
            return Signal(
                "long",
                stop_loss=close - stop_distance,
                take_profit=close + tp_r * stop_distance if tp_r else None,
                reason="Snel gemiddelde kruist boven traag",
            )
        if crossed_down and position.side != "short":
            if not self.p["allow_short"]:
                return Signal("flat", reason="Snel gemiddelde kruist onder traag") if position.side else None
            return Signal(
                "short",
                stop_loss=close + stop_distance,
                take_profit=close - tp_r * stop_distance if tp_r else None,
                reason="Snel gemiddelde kruist onder traag",
            )
        return None
