"""An append-only log of everything that happens: signals, orders, fills, skips."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class EventLog:
    events: list[dict] = field(default_factory=list)

    def add(self, ts: int, kind: str, message: str, **data) -> None:
        """kind: signal | order | fill | exit | skip | warning | info"""
        self.events.append({"ts": ts, "kind": kind, "message": message, **data})

    def count(self, kind: str) -> int:
        return sum(1 for e in self.events if e["kind"] == kind)


def nl(value: float, digits: int = 2) -> str:
    """Number in Dutch notation for log messages: 1.234,56"""
    text = f"{value:,.{digits}f}"
    return text.replace(",", "\u2009").replace(".", ",").replace("\u2009", ".")


def nl_lots(lots: float) -> str:
    return nl(lots, 4).rstrip("0").rstrip(",") if lots < 0.1 or round(lots, 2) != lots else nl(lots, 2)
