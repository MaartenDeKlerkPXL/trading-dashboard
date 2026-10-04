"""The strategy contract, shared by backtest, paper and live trading.

A strategy only ever sees closed candles up to "now" and the current position,
and answers with a Signal (or None). It never places orders itself: the
runner turns signals into orders for whichever executor is active.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, Literal

Side = Literal["long", "short"]
Action = Literal["long", "short", "flat"]


@dataclass(frozen=True)
class Bar:
    ts: int        # open time, unix seconds UTC
    open: float
    high: float
    low: float
    close: float
    volume: float


class History(Sequence):
    """Read-only window on closed bars. Index -1 is the most recent closed bar.

    Bars after "now" are unreachable, which makes lookahead impossible.
    """

    __slots__ = ("_bars", "_end")

    def __init__(self, bars: list[Bar], end: int):
        self._bars = bars
        self._end = end

    def __len__(self) -> int:
        return self._end

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = index.indices(self._end)
            return self._bars[start:stop:step]
        if index < 0:
            index += self._end
        if not 0 <= index < self._end:
            raise IndexError("bar index outside the visible history")
        return self._bars[index]

    def closes(self, n: int) -> list[float]:
        """The last n closing prices, oldest first."""
        return [b.close for b in self._bars[max(0, self._end - n):self._end]]


@dataclass(frozen=True)
class Position:
    side: Side | None = None       # None = flat
    lots: float = 0.0
    entry_price: float = 0.0
    entry_ts: int = 0
    stop_loss: float | None = None
    take_profit: float | None = None

    @property
    def is_flat(self) -> bool:
        return self.side is None


@dataclass(frozen=True)
class Signal:
    """The position the strategy wants from the next bar on.

    action      "long" / "short" opens (or keeps / reverses to) that side, "flat" closes.
    size        fraction of the standard risk per trade (1.0 = full configured risk).
    stop_loss   absolute price. Required to open a position: sizing is based on it,
                and in live trading it is placed at the broker.
    take_profit absolute price, optional.
    """

    action: Action
    stop_loss: float | None = None
    take_profit: float | None = None
    size: float = 1.0
    reason: str = ""


@dataclass(frozen=True)
class Param:
    name: str
    label: str            # Dutch label for the UI
    default: float | int | bool
    min: float | None = None
    max: float | None = None
    step: float | None = None
    help: str = ""

    @property
    def kind(self) -> str:
        if isinstance(self.default, bool):
            return "bool"
        return "int" if isinstance(self.default, int) else "float"

    def coerce(self, value):
        if self.kind == "bool":
            if isinstance(value, str):
                return value.lower() in ("1", "true", "ja", "on")
            return bool(value)
        try:
            number = int(value) if self.kind == "int" else float(value)
        except (TypeError, ValueError):
            raise ValueError(f"'{self.label}' moet een getal zijn") from None
        if self.kind == "int" and float(value) != number:
            raise ValueError(f"'{self.label}' moet een geheel getal zijn")
        if self.min is not None and number < self.min:
            raise ValueError(f"'{self.label}' moet minimaal {self.min} zijn")
        if self.max is not None and number > self.max:
            raise ValueError(f"'{self.label}' mag maximaal {self.max} zijn")
        return number


class Strategy(ABC):
    """Base class. A new version of a strategy is a new file with a new `version`."""

    name: ClassVar[str]          # stable identifier, e.g. "sma_cross"
    version: ClassVar[str]       # "v1", "v2", ...
    label: ClassVar[str]         # Dutch name for the UI
    description: ClassVar[str]   # Dutch explanation for the UI
    params: ClassVar[tuple[Param, ...]] = ()

    def __init__(self, **values):
        known = {p.name: p for p in self.params}
        unknown = set(values) - set(known)
        if unknown:
            raise ValueError(f"Onbekende parameter(s): {', '.join(sorted(unknown))}")
        self.p = {name: param.coerce(values.get(name, param.default)) for name, param in known.items()}
        self.validate()

    @classmethod
    def key(cls) -> str:
        return f"{cls.name}@{cls.version}"

    def validate(self) -> None:
        """Override to check combinations of parameters (raise ValueError in Dutch)."""

    def warmup(self) -> int:
        """Number of closed bars needed before on_bar is called."""
        return 1

    @abstractmethod
    def on_bar(self, history: History, position: Position) -> Signal | None:
        """Called once per closed bar. Return None to leave everything as it is."""
