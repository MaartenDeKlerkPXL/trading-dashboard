"""Template for a new strategy. Copy this file to `<name>_v1.py` and fill it in.

Files starting with "_" are not loaded, so this template never shows up in the dashboard.

Rules:
- One strategy per file. The file name and `version` go together: my_idea_v1.py → version "v1".
- Never change a version that has been paper traded: copy it to _v2 and change the copy.
- Only use `history` (closed bars up to now) and `position`. There is no way to see future bars.
- Every opening signal needs a stop-loss: position size is based on it.
"""

from __future__ import annotations

from .base import History, Param, Position, Signal, Strategy
from .indicators import atr, sma


class MyIdeaV1(Strategy):
    name = "my_idea"              # stable identifier, lowercase with underscores
    version = "v1"
    label = "Mijn idee"           # shown in the dashboard
    description = "Wat de strategie doet, in gewone taal."
    params = (
        Param("period", "Periode (bars)", 20, min=2, max=500, step=1),
        Param("atr_stop", "Stop-loss (× ATR)", 2.0, min=0.2, max=20, step=0.1),
    )

    def warmup(self) -> int:
        # Bars needed before the first decision.
        return max(self.p["period"], 14) + 1

    def on_bar(self, history: History, position: Position) -> Signal | None:
        average, risk = sma(history, self.p["period"]), atr(history, 14)
        if average is None or risk is None:
            return None
        close = history[-1].close
        if close > average and position.side != "long":
            return Signal("long", stop_loss=close - self.p["atr_stop"] * risk, reason="Boven het gemiddelde")
        if close < average and position.side == "long":
            return Signal("flat", reason="Onder het gemiddelde")
        return None
