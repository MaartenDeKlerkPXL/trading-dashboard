"""Executors: backtest (simulation), paper (live prices, fake money) and live (real orders).

Which one runs is decided by one setting: `execution.mode` in config.toml.
Backtests always use the backtest executor.
"""

from __future__ import annotations

from .base import BrokerExecutor


def create_executor(mode: str, **kwargs) -> BrokerExecutor:
    if mode == "backtest":
        from .backtest import BacktestExecutor

        return BacktestExecutor(**kwargs)
    if mode == "paper":
        raise NotImplementedError("De paper-executor komt in fase 4.")
    if mode == "live":
        raise NotImplementedError("De live-executor komt in fase 6 en staat standaard uit.")
    raise ValueError(f"Onbekende modus '{mode}'")
