"""Load config.toml into typed settings."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT_DIR / "config.toml"
DATA_DIR = ROOT_DIR / "data"
FRONTEND_DIR = ROOT_DIR / "frontend"

EXECUTION_MODES = ("backtest", "paper", "live")


@dataclass(frozen=True)
class AppSettings:
    host: str = "127.0.0.1"
    port: int = 8000
    timezone: str = "Europe/Amsterdam"


@dataclass(frozen=True)
class AccountSettings:
    currency: str = "EUR"
    starting_capital: float = 1000.0
    leverage: int = 100
    risk_per_trade_pct: float = 2.0


@dataclass(frozen=True)
class ExecutionSettings:
    mode: str = "paper"


@dataclass(frozen=True)
class DataSettings:
    provider: str = "dukascopy"
    default_symbol: str = "XAUUSD"
    default_timeframe: str = "H1"


@dataclass(frozen=True)
class PaperSettings:
    poll_seconds: int = 30      # how often prices are fetched and strategies run
    keep_awake: bool = True     # keep the Mac from sleeping while paper sessions run


@dataclass(frozen=True)
class Settings:
    app: AppSettings = field(default_factory=AppSettings)
    account: AccountSettings = field(default_factory=AccountSettings)
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)
    data: DataSettings = field(default_factory=DataSettings)
    paper: PaperSettings = field(default_factory=PaperSettings)
    db_path: Path = DATA_DIR / "trading.sqlite"


class ConfigError(ValueError):
    pass


def _section(raw: dict, name: str, cls):
    values = raw.get(name, {})
    known = {f for f in cls.__dataclass_fields__}
    unknown = set(values) - known
    if unknown:
        raise ConfigError(f"Onbekende instelling(en) in [{name}]: {', '.join(sorted(unknown))}")
    return cls(**values)


def load_settings(path: Path = CONFIG_PATH) -> Settings:
    raw: dict = {}
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)

    settings = Settings(
        app=_section(raw, "app", AppSettings),
        account=_section(raw, "account", AccountSettings),
        execution=_section(raw, "execution", ExecutionSettings),
        data=_section(raw, "data", DataSettings),
        paper=_section(raw, "paper", PaperSettings),
    )
    if settings.execution.mode not in EXECUTION_MODES:
        raise ConfigError(
            f"execution.mode moet een van {', '.join(EXECUTION_MODES)} zijn, niet '{settings.execution.mode}'"
        )
    if not 10 <= settings.paper.poll_seconds <= 600:
        raise ConfigError("paper.poll_seconds moet tussen 10 en 600 liggen.")
    if settings.execution.mode == "live":
        # Live trading is only built in phase 6; refuse to start rather than guess.
        raise ConfigError("Live-modus bestaat nog niet (komt in fase 6). Zet execution.mode op 'paper'.")
    return settings
