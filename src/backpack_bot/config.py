from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from dotenv import load_dotenv


DEFAULT_API_BASE_URL = "https://api.backpack.exchange"


class ConfigError(ValueError):
    """Raised when required local configuration is missing."""


@dataclass(frozen=True)
class Settings:
    api_base_url: str = DEFAULT_API_BASE_URL
    api_key: str | None = None
    api_secret: str | None = None


@dataclass(frozen=True)
class BotSettings:
    mode: str
    market_type: str
    symbol: str
    database_path: Path
    history_interval: str
    history_days: float
    paper_initial_balance: Decimal
    paper_max_leverage: Decimal
    paper_daily_loss_limit_pct: Decimal
    paper_risk_per_trade_pct: Decimal
    paper_max_daily_entries: int
    strategy: str
    strategy_interval: str
    ema_fast_period: int
    ema_slow_period: int
    vwma_period: int
    smc_swing_length: int
    smc_require_ema_filter: bool
    bollinger_period: int
    bollinger_stddev_multiple: Decimal
    cci_period: int
    atr_period: int
    atr_stop_multiple: Decimal
    backtest_slippage_bps: Decimal
    margin_allocation_pct: Decimal
    position_notional_multiple: Decimal
    stop_loss_equity_pct: Decimal
    take_profit_margin_pct: Decimal


def load_bot_settings() -> BotSettings:
    """Load non-secret bot choices from bot.toml."""
    project_root = Path(__file__).resolve().parents[2]
    config_path = project_root / "bot.toml"
    try:
        with config_path.open("rb") as config_file:
            values = tomllib.load(config_file)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"bot.toml을 읽을 수 없습니다: {exc}") from exc

    mode = str(values.get("mode", "")).upper()
    market_type = str(values.get("market_type", "")).upper()
    symbol = str(values.get("symbol", "")).upper()
    raw_database_path = Path(str(values.get("database_path", "data/market_data.db")))
    history_interval = str(values.get("history_interval", "1m"))
    history_days = float(values.get("history_days", 30))
    try:
        paper_initial_balance = Decimal(str(values.get("paper_initial_balance", 1000)))
        paper_max_leverage = Decimal(str(values.get("paper_max_leverage", 1)))
        paper_daily_loss_limit_pct = Decimal(
            str(values.get("paper_daily_loss_limit_pct", 2))
        )
        paper_risk_per_trade_pct = Decimal(
            str(values.get("paper_risk_per_trade_pct", "0.5"))
        )
        atr_stop_multiple = Decimal(str(values.get("atr_stop_multiple", 2)))
        backtest_slippage_bps = Decimal(
            str(values.get("backtest_slippage_bps", 2))
        )
        margin_allocation_pct = Decimal(str(values.get("margin_allocation_pct", 20)))
        position_notional_multiple = Decimal(
            str(values.get("position_notional_multiple", 3))
        )
        stop_loss_equity_pct = Decimal(
            str(values.get("stop_loss_equity_pct", 1))
        )
        take_profit_margin_pct = Decimal(
            str(values.get("take_profit_margin_pct", 10))
        )
        bollinger_stddev_multiple = Decimal(
            str(values.get("bollinger_stddev_multiple", 2))
        )
    except InvalidOperation as exc:
        raise ConfigError("paper_initial_balance는 숫자여야 합니다.") from exc
    if mode not in {"PAPER", "LIVE"}:
        raise ConfigError("bot.toml의 mode는 paper 또는 live여야 합니다.")
    if market_type != "PERP":
        raise ConfigError("현재 프로젝트의 market_type은 PERP여야 합니다.")
    if not symbol.endswith("_PERP"):
        raise ConfigError("무기한 선물 symbol은 _PERP로 끝나야 합니다.")
    if raw_database_path.is_absolute():
        raise ConfigError("database_path는 프로젝트 내부의 상대 경로여야 합니다.")
    database_path = (project_root / raw_database_path).resolve()
    if not database_path.is_relative_to(project_root):
        raise ConfigError("database_path는 프로젝트 폴더 밖을 가리킬 수 없습니다.")
    if database_path.suffix.lower() != ".db":
        raise ConfigError("database_path는 .db 파일이어야 합니다.")
    valid_history_intervals = {
        "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h",
        "6h", "8h", "12h", "1d", "3d", "1w",
    }
    if history_interval not in valid_history_intervals:
        raise ConfigError("지원하지 않는 history_interval입니다.")
    if history_days <= 0:
        raise ConfigError("history_days는 0보다 커야 합니다.")
    if paper_initial_balance <= 0:
        raise ConfigError("paper_initial_balance는 0보다 커야 합니다.")
    if paper_max_leverage <= 0 or paper_max_leverage > 4:
        raise ConfigError("paper_max_leverage는 0보다 크고 4 이하여야 합니다.")
    if paper_daily_loss_limit_pct <= 0 or paper_daily_loss_limit_pct > 100:
        raise ConfigError("paper_daily_loss_limit_pct는 0보다 크고 100 이하여야 합니다.")
    if paper_risk_per_trade_pct <= 0 or paper_risk_per_trade_pct > 100:
        raise ConfigError("paper_risk_per_trade_pct는 0보다 크고 100 이하여야 합니다.")
    paper_max_daily_entries = int(values.get("paper_max_daily_entries", 10))
    if paper_max_daily_entries <= 0:
        raise ConfigError("paper_max_daily_entries는 1 이상이어야 합니다.")
    strategy = str(values.get("strategy", "ema_trend"))
    strategy_interval = str(values.get("strategy_interval", "15m"))
    ema_fast_period = int(values.get("ema_fast_period", 20))
    ema_slow_period = int(values.get("ema_slow_period", 50))
    atr_period = int(values.get("atr_period", 14))
    vwma_period = int(values.get("vwma_period", 100))
    smc_swing_length = int(values.get("smc_swing_length", 50))
    bollinger_period = int(values.get("bollinger_period", 20))
    cci_period = int(values.get("cci_period", 20))
    smc_require_ema_filter = values.get("smc_require_ema_filter", True)
    if not isinstance(smc_require_ema_filter, bool):
        raise ConfigError("smc_require_ema_filter는 true 또는 false여야 합니다.")
    if strategy not in {
        "ema_trend",
        "ema_vwma_long",
        "smc_swing_bos_long",
        "bollinger_cci_long",
    }:
        raise ConfigError("지원하지 않는 strategy입니다.")
    if strategy_interval not in valid_history_intervals:
        raise ConfigError("지원하지 않는 strategy_interval입니다.")
    if ema_fast_period <= 0 or ema_slow_period <= ema_fast_period:
        raise ConfigError("EMA 주기는 0 < fast < slow 순서여야 합니다.")
    if atr_period <= 0 or atr_stop_multiple <= 0:
        raise ConfigError("ATR 주기와 손절 배수는 0보다 커야 합니다.")
    if backtest_slippage_bps < 0:
        raise ConfigError("backtest_slippage_bps는 0 이상이어야 합니다.")
    if vwma_period <= 0:
        raise ConfigError("vwma_period는 1 이상이어야 합니다.")
    if smc_swing_length < 10:
        raise ConfigError("smc_swing_length는 10 이상이어야 합니다.")
    if bollinger_period <= 0 or bollinger_stddev_multiple <= 0:
        raise ConfigError("볼린저 밴드 주기와 표준편차 배수는 0보다 커야 합니다.")
    if cci_period <= 0:
        raise ConfigError("CCI 주기는 0보다 커야 합니다.")
    if margin_allocation_pct <= 0 or margin_allocation_pct > 100:
        raise ConfigError("margin_allocation_pct는 0보다 크고 100 이하여야 합니다.")
    if position_notional_multiple <= 0 or position_notional_multiple > 4:
        raise ConfigError("position_notional_multiple은 0보다 크고 4 이하여야 합니다.")
    if position_notional_multiple > paper_max_leverage:
        raise ConfigError("포지션 배수는 최대 레버리지 이하여야 합니다.")
    if stop_loss_equity_pct <= 0 or stop_loss_equity_pct > 100:
        raise ConfigError("stop_loss_equity_pct는 0보다 크고 100 이하여야 합니다.")
    if take_profit_margin_pct <= 0:
        raise ConfigError("take_profit_margin_pct는 0보다 커야 합니다.")

    return BotSettings(
        mode=mode,
        market_type=market_type,
        symbol=symbol,
        database_path=database_path,
        history_interval=history_interval,
        history_days=history_days,
        paper_initial_balance=paper_initial_balance,
        paper_max_leverage=paper_max_leverage,
        paper_daily_loss_limit_pct=paper_daily_loss_limit_pct,
        paper_risk_per_trade_pct=paper_risk_per_trade_pct,
        paper_max_daily_entries=paper_max_daily_entries,
        strategy=strategy,
        strategy_interval=strategy_interval,
        ema_fast_period=ema_fast_period,
        ema_slow_period=ema_slow_period,
        vwma_period=vwma_period,
        smc_swing_length=smc_swing_length,
        smc_require_ema_filter=smc_require_ema_filter,
        bollinger_period=bollinger_period,
        bollinger_stddev_multiple=bollinger_stddev_multiple,
        cci_period=cci_period,
        atr_period=atr_period,
        atr_stop_multiple=atr_stop_multiple,
        backtest_slippage_bps=backtest_slippage_bps,
        margin_allocation_pct=margin_allocation_pct,
        position_notional_multiple=position_notional_multiple,
        stop_loss_equity_pct=stop_loss_equity_pct,
        take_profit_margin_pct=take_profit_margin_pct,
    )


def load_settings(*, require_credentials: bool = False) -> Settings:
    """Load settings from the project .env file and environment variables."""
    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(project_root / ".env")

    api_key = os.getenv("BACKPACK_API_KEY", "").strip() or None
    api_secret = os.getenv("BACKPACK_API_SECRET", "").strip() or None
    api_base_url = os.getenv("BACKPACK_API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/")

    if require_credentials and (not api_key or not api_secret):
        raise ConfigError(
            "읽기 전용 API 키가 아직 설정되지 않았습니다. "
            "프로젝트의 .env 파일에 BACKPACK_API_KEY와 "
            "BACKPACK_API_SECRET을 입력하세요."
        )

    return Settings(
        api_base_url=api_base_url,
        api_key=api_key,
        api_secret=api_secret,
    )
