"""Moving-average crossover with swing stop, risk/reward target and ATR trailing stop.

Rebuilt from the published description of Bjorgum's open-source TradingView strategy "3Commas Bot"
(https://www.tradingview.com/script/MvlwAzSg-3Commas-Bot/):

- Entry: a moving-average cross, up for long and down for short. The type of average and both
  lengths can be chosen.
- Stop-loss: the swing low (long) or swing high (short) of a lookback period, plus an ATR "adder"
  that puts the stop a bit further from support or resistance.
- Target: risk/reward ratio times the distance to the stop (default 1:1).
- Optional trailing stop: starts once the price has covered a chosen part of the way to the target
  (0.5 = half way) and then ratchets along at a multiple of the ATR.

PINE DIFFERENCES (behaviour that is not reproduced 1-to-1):
1. Source code: TradingView could not be reached from the development environment, so this version is
   built from the script's description. The default values are estimates. Paste the Pine code and a
   v2 will follow the original exactly (this v1 then stays unchanged, as with every version).
2. 3Commas: the original sends webhook messages to a 3Commas bot. Here the dashboard places the orders
   itself (paper, or live at BlackBull via cTrader), so no webhooks are needed.
3. Target and trailing: with the trailing stop switched on, no fixed take-profit is placed: the target
   only decides where trailing starts. The target is computed from the close of the signal candle
   (Pine uses the average fill price), so it can differ by the spread and slippage.
4. Stop-loss: here it is mandatory and can only move towards the price. Position size follows the risk
   per trade instead of Pine's order size; entry and exit moments match, amounts do not.
5. Fills at the open of the next candle (Pine default). Bar magnifier and calc_on_every_tick are not
   reproduced; costs (spread, slippage, commission, financing) apply here, Pine's default is 0.
6. EMA and RMA (and the ATR) are seeded with a simple average of the first bars of the loaded data.
   TradingView loads more history, so the very first signals of a period can differ slightly.
"""

from __future__ import annotations

from .base import History, Param, Position, Signal, Strategy
from .indicators import MA_TYPES, AtrRma, MovingAverage


class Bjorgum3CommasV1(Strategy):
    name = "bjorgum_3commas"
    version = "v1"
    label = "3Commas Bot (Bjorgum, uit TradingView)"
    description = (
        "Gaat long als het snelle gemiddelde boven het trage kruist en short als het eronder kruist. De stop-loss "
        "ligt onder de laatste bodem (long) of boven de laatste top (short), met een extra afstand van een deel "
        "van de ATR. Het koersdoel is een veelvoud van het risico (standaard 1:1). Optioneel loopt er een "
        "trailing stop mee zodra de koers een deel van de weg naar het doel heeft afgelegd. Een tegengestelde "
        "kruising draait de positie om. Nagebouwd uit de beschrijving van het TradingView-script; de "
        "standaardwaarden zijn een schatting."
    )
    params = (
        Param("ma_type", "Soort gemiddelde", "EMA", choices=MA_TYPES,
              help="SMA = gewoon, EMA/RMA = recente koersen tellen zwaarder, WMA = gewogen, HMA = Hull "
                   "(snel en vloeiend), VWMA = gewogen naar volume."),
        Param("fast", "Snel gemiddelde (bars)", 9, min=2, max=500, step=1),
        Param("slow", "Traag gemiddelde (bars)", 21, min=3, max=1000, step=1),
        Param("swing_lookback", "Bodem/top zoeken over (bars)", 10, min=2, max=200, step=1,
              help="De stop-loss ligt onder de laagste koers (long) of boven de hoogste koers (short) van "
                   "zoveel candles."),
        Param("atr_period", "ATR-periode (bars)", 14, min=2, max=200, step=1),
        Param("atr_adder", "Extra afstand stop (× ATR)", 1.0, min=0, max=10, step=0.1,
              help="Zet de stop-loss iets verder dan de bodem of top, zodat ruis hem niet meteen raakt."),
        Param("risk_reward", "Koersdoel (× risico)", 1.0, min=0.2, max=20, step=0.1,
              help="1 = het doel ligt even ver van de instap als de stop-loss (1:1)."),
        Param("use_trailing", "Trailing stop gebruiken", False,
              help="Aan: geen vaste take-profit, maar een stop-loss die meeloopt met de koers."),
        Param("trail_trigger", "Trailing start bij (deel van de weg naar het doel)", 0.5, min=0, max=1, step=0.05,
              help="0,5 = de trailing stop begint als de koers halverwege het koersdoel is."),
        Param("trail_atr", "Afstand trailing stop (× ATR)", 1.0, min=0.1, max=20, step=0.1),
        Param("allow_short", "Ook short gaan", True),
    )

    def __init__(self, **values):
        super().__init__(**values)
        self.fast_ma = MovingAverage(self.p["ma_type"], self.p["fast"])
        self.slow_ma = MovingAverage(self.p["ma_type"], self.p["slow"])
        self.atr = AtrRma(self.p["atr_period"])

    def validate(self) -> None:
        if self.p["fast"] >= self.p["slow"]:
            raise ValueError("Het snelle gemiddelde moet korter zijn dan het trage.")

    def warmup(self) -> int:
        return max(self.slow_ma.warmup(), self.p["swing_lookback"], self.p["atr_period"]) + 2

    # ---------- levels ----------

    def _levels(self, history: History, i: int, side: str) -> tuple[float, float] | None:
        """Stop-loss and target as decided on bar i (an index into the visible history)."""
        lb = self.p["swing_lookback"]
        if i + 1 < lb:
            return None
        window = history[i + 1 - lb:i + 1]
        atr = self.atr.value(history, len(history) - 1 - i)
        if atr is None:
            return None
        close = history[i].close
        if side == "long":
            stop = min(b.low for b in window) - self.p["atr_adder"] * atr
            if stop >= close:
                return None
            return stop, close + (close - stop) * self.p["risk_reward"]
        stop = max(b.high for b in window) + self.p["atr_adder"] * atr
        if stop <= close:
            return None
        return stop, close - (stop - close) * self.p["risk_reward"]

    def _entry_index(self, history: History, entry_ts: int) -> int | None:
        for i in range(len(history) - 1, -1, -1):
            if history[i].ts <= entry_ts:
                return i
        return None

    # ---------- decisions ----------

    def on_bar(self, history: History, position: Position) -> Signal | None:
        fast_now, fast_prev = self.fast_ma.value(history), self.fast_ma.value(history, 1)
        slow_now, slow_prev = self.slow_ma.value(history), self.slow_ma.value(history, 1)
        atr = self.atr.value(history)
        if None in (fast_now, fast_prev, slow_now, slow_prev, atr):
            return None
        last = len(history) - 1
        crossed_up = fast_prev <= slow_prev and fast_now > slow_now
        crossed_down = fast_prev >= slow_prev and fast_now < slow_now
        trailing = self.p["use_trailing"]

        if crossed_up and position.side != "long":
            levels = self._levels(history, last, "long")
            if levels:
                stop, target = levels
                return Signal("long", stop_loss=stop, take_profit=None if trailing else target,
                              reason=f"{self.p['ma_type']} {self.p['fast']} kruist boven {self.p['slow']}")
            return Signal("flat", reason="Kruising omhoog, maar geen geldige stop-loss") if position.side else None
        if crossed_down and position.side != "short":
            if not self.p["allow_short"]:
                return Signal("flat", reason="Gemiddelden kruisen omlaag") if position.side else None
            levels = self._levels(history, last, "short")
            if levels:
                stop, target = levels
                return Signal("short", stop_loss=stop, take_profit=None if trailing else target,
                              reason=f"{self.p['ma_type']} {self.p['fast']} kruist onder {self.p['slow']}")
            return Signal("flat", reason="Kruising omlaag, maar geen geldige stop-loss") if position.side else None

        if trailing and position.side and position.stop_loss is not None:
            return self._trail(history, position, atr)
        return None

    def _trail(self, history: History, position: Position, atr: float) -> Signal | None:
        entry_i = self._entry_index(history, position.entry_ts)
        if entry_i is None or entry_i < 1:
            return None
        # The levels as they were decided on the signal candle, just before the entry.
        levels = self._levels(history, entry_i - 1, position.side)
        if levels is None:
            return None
        _, target = levels
        entry = position.entry_price
        since = history[entry_i:]
        close = history[-1].close
        if position.side == "long":
            trigger = entry + self.p["trail_trigger"] * (target - entry)
            if max(b.high for b in since) < trigger:
                return None
            new_stop = close - self.p["trail_atr"] * atr
            if new_stop > position.stop_loss and new_stop < close:
                return Signal("adjust", stop_loss=new_stop, reason="trailing stop")
        else:
            trigger = entry - self.p["trail_trigger"] * (entry - target)
            if min(b.low for b in since) > trigger:
                return None
            new_stop = close + self.p["trail_atr"] * atr
            if new_stop < position.stop_loss and new_stop > close:
                return Signal("adjust", stop_loss=new_stop, reason="trailing stop")
        return None
