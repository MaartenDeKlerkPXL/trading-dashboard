"""Load config.toml into typed settings."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from .risk import RiskLimits

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
class LiveSettings:
    allow_real_money: bool = False      # demo accounts only, unless this is true
    max_capital: float = 1000.0         # most EUR one live strategy may use
    redirect_url: str = "http://localhost:8000/ctrader/callback"   # as registered at openapi.ctrader.com


@dataclass(frozen=True)
class AlertSettings:
    feed_down_minutes: int = 15          # e-mail when prices/broker are unreachable this long
    repeat_minutes: int = 60             # at most one e-mail per problem per this many minutes
    loop_errors: int = 3                 # e-mail after this many failed loop rounds in a row
    reconcile_minutes: int = 5           # how often bookkeeping is reconciled
    email_on_reconciliation: bool = False


@dataclass(frozen=True)
class Settings:
    app: AppSettings = field(default_factory=AppSettings)
    account: AccountSettings = field(default_factory=AccountSettings)
    execution: ExecutionSettings = field(default_factory=ExecutionSettings)
    data: DataSettings = field(default_factory=DataSettings)
    paper: PaperSettings = field(default_factory=PaperSettings)
    risk: RiskLimits = field(default_factory=RiskLimits)
    alerts: AlertSettings = field(default_factory=AlertSettings)
    live: LiveSettings = field(default_factory=LiveSettings)
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
        risk=_section(raw, "risk", RiskLimits),
        alerts=_section(raw, "alerts", AlertSettings),
        live=_section(raw, "live", LiveSettings),
    )
    from .data.instruments import INSTRUMENTS

    unknown = set(settings.risk.max_lots) - set(INSTRUMENTS)
    if unknown:
        raise ConfigError(f"Onbekend instrument in [risk.max_lots]: {', '.join(sorted(unknown))}")
    if "timezone" not in raw.get("risk", {}):
        settings = replace(settings, risk=replace(settings.risk, timezone=settings.app.timezone))
    try:
        settings.risk.validate()
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    a = settings.alerts
    if not 1 <= a.feed_down_minutes <= 1440 or not 5 <= a.repeat_minutes <= 1440:
        raise ConfigError("alerts.feed_down_minutes (1–1440) of alerts.repeat_minutes (5–1440) klopt niet.")
    if not 1 <= a.loop_errors <= 100 or not 1 <= a.reconcile_minutes <= 1440:
        raise ConfigError("alerts.loop_errors (1–100) of alerts.reconcile_minutes (1–1440) klopt niet.")
    if settings.account.risk_per_trade_pct > settings.risk.max_risk_per_trade_pct:
        raise ConfigError(
            f"account.risk_per_trade_pct ({settings.account.risk_per_trade_pct}) is hoger dan de harde limiet "
            f"risk.max_risk_per_trade_pct ({settings.risk.max_risk_per_trade_pct})."
        )
    if settings.execution.mode not in EXECUTION_MODES:
        raise ConfigError(
            f"execution.mode moet een van {', '.join(EXECUTION_MODES)} zijn, niet '{settings.execution.mode}'"
        )
    if not 10 <= settings.paper.poll_seconds <= 600:
        raise ConfigError("paper.poll_seconds moet tussen 10 en 600 liggen.")
    if not 10 <= settings.live.max_capital <= 1_000_000:
        raise ConfigError("live.max_capital moet tussen 10 en 1.000.000 liggen.")
    return settings
