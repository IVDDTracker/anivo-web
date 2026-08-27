"""Application settings (pydantic-settings). All secrets come from the environment.

Defaults are conservative and documented in DECISIONS.md.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models.enums import ExecutionMode

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"


class BinanceConfig(BaseModel):
    # Public market data endpoints (no key required / consumed) — see DECISIONS.md D-003
    market_rest_base: str = "https://data-api.binance.vision"
    market_ws_base: str = "wss://data-stream.binance.vision"
    # Production account (read-only usage only; order endpoints are refused in transport)
    prod_rest_base: str = "https://api.binance.com"
    # Spot testnet
    testnet_rest_base: str = "https://testnet.binance.vision/api"
    testnet_ws_base: str = "wss://stream.testnet.binance.vision"
    recv_window_ms: int = 5000
    request_timeout_s: float = 10.0
    ws_reconnect_before_h: float = 23.0  # proactive reconnect before the 24h server limit
    ws_stale_after_s: float = 90.0  # no message for this long → feed stale
    kline_backfill_limit: int = 1000
    depth_levels: int = 20
    # weight guardrail used if exchangeInfo rateLimits are unavailable
    fallback_weight_per_minute: int = 6000


class RiskConfig(BaseModel):
    starting_equity_usdt: float = 10_000.0
    max_risk_per_position_pct: float = 1.0          # % equity at risk per position (stop distance)
    max_position_notional_pct: float = 20.0         # % equity notional cap per position
    max_open_positions: int = 4
    max_exposure_per_asset_pct: float = 25.0
    max_correlated_exposure_pct: float = 50.0       # sum of correlated (|rho|>threshold) longs
    correlation_threshold: float = 0.7
    max_daily_loss_pct: float = 3.0
    max_weekly_loss_pct: float = 6.0
    max_drawdown_pct: float = 15.0
    max_consecutive_losses: int = 5
    min_liquidity_quote_vol_24h: float = 50_000_000.0
    max_spread_pct: float = 0.10                    # 10 bps
    min_signal_confidence: float = 60.0
    cooldown_after_loss_minutes: int = 30
    cooldown_after_vol_shock_minutes: int = 60
    vol_shock_return_pct: float = 5.0               # 1m |return| beyond this → vol shock
    stale_data_max_age_s: float = 120.0
    abnormal_price_jump_pct: float = 20.0           # tick-to-tick jump → quarantine feed
    min_data_quality: float = 0.8


class CostConfig(BaseModel):
    taker_fee_bps: float = 10.0
    maker_fee_bps: float = 10.0
    base_slippage_bps: float = 2.0
    impact_coeff: float = 0.1     # extra bps per (order notional / top-of-book notional)
    latency_ms: int = 250


class TelegramConfig(BaseModel):
    api_base: str = "https://api.telegram.org"
    poll_timeout_s: int = 50
    min_notify_interval_s: float = 1.1  # per-chat throttle
    ingest_reliability_cap: float = 0.35


class GitHubConfig(BaseModel):
    api_base: str = "https://api.github.com"
    api_version: str = "2022-11-28"
    poll_interval_s: int = 300


class RetentionConfig(BaseModel):
    raw_hf_events_days: int = 14
    candles_1m_days: int = 400
    orderbook_snapshots_days: int = 7
    features_days: int = 120


class RegimeConfig(BaseModel):
    ema_fast: int = 50
    ema_slow: int = 200
    vol_lookback: int = 30
    vol_history: int = 365
    panic_drawdown_pct: float = 12.0     # decline within panic_window bars
    panic_window_bars: int = 48
    high_vol_percentile: float = 0.85
    low_vol_percentile: float = 0.20


class PromotionConfig(BaseModel):
    min_backtest_trades: int = 100
    min_oos_trades: int = 30
    min_profit_factor_oos: float = 1.2
    max_drawdown_pct: float = 20.0
    parameter_perturbation_pct: float = 20.0
    min_perturbed_profit_factor: float = 1.0
    min_paper_days: int = 14
    min_scorecard: float = 60.0
    max_single_winner_share: float = 0.4


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = Field(default="dev", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    api_host: str = Field(default="127.0.0.1", alias="API_HOST")
    api_port: int = Field(default=8000, alias="API_PORT")

    database_url: str = Field(
        default="postgresql+asyncpg://quantlab:quantlab@localhost:5432/quantlab", alias="DATABASE_URL"
    )
    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")

    execution_mode: ExecutionMode = Field(default=ExecutionMode.PAPER_ONLY, alias="EXECUTION_MODE")

    binance_readonly_api_key: str = Field(default="", alias="BINANCE_READONLY_API_KEY")
    binance_readonly_api_secret: str = Field(default="", alias="BINANCE_READONLY_API_SECRET")
    binance_testnet_api_key: str = Field(default="", alias="BINANCE_TESTNET_API_KEY")
    binance_testnet_api_secret: str = Field(default="", alias="BINANCE_TESTNET_API_SECRET")
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")
    github_token: str = Field(default="", alias="GITHUB_TOKEN")
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")

    symbols: list[str] = Field(default=["BTCUSDT", "ETHUSDT", "SOLUSDT"])
    collect_only_symbols: list[str] = Field(default=["BNBUSDT"])
    timeframes: list[str] = Field(default=["1m", "5m", "15m", "1h", "4h", "1d"])
    signal_timeframe: str = "1h"

    binance: BinanceConfig = BinanceConfig()
    risk: RiskConfig = RiskConfig()
    costs: CostConfig = CostConfig()
    telegram: TelegramConfig = TelegramConfig()
    github: GitHubConfig = GitHubConfig()
    retention: RetentionConfig = RetentionConfig()
    regime: RegimeConfig = RegimeConfig()
    promotion: PromotionConfig = PromotionConfig()

    config_dir: Path = CONFIG_DIR

    @property
    def all_symbols(self) -> list[str]:
        return sorted(set(self.symbols) | set(self.collect_only_symbols))

    def secrets(self) -> list[str]:
        return [
            v
            for v in (
                self.binance_readonly_api_key,
                self.binance_readonly_api_secret,
                self.binance_testnet_api_key,
                self.binance_testnet_api_secret,
                self.telegram_bot_token,
                self.github_token,
                self.anthropic_api_key,
            )
            if v
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()
