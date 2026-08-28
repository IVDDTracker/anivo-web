"""All tunables live here (env / .env). Nothing trade-relevant is hard-coded.

LIVE trading requires BOTH `MODE=LIVE` and `ENABLE_LIVE_TRADING=true`
(double-flag safety per spec §13). Default mode is PAPER.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Mode(StrEnum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


class ListenerMode(StrEnum):
    AUTO = "auto"
    STREAM = "stream"
    POLL = "poll"


class SignalSource(StrEnum):
    X = "X"
    TELEGRAM = "TELEGRAM"
    BOTH = "BOTH"


class StrategyMode(StrEnum):
    LONG_SHORT = "LONG_SHORT"    # original: ride the pump, then fade it
    SHORT_ONLY = "SHORT_ONLY"    # skip the pump entirely; only fade confirmed reversals


class ReversalWeights(BaseModel):
    """Weights of the 0-100 reversal score components (sum need not be 100;
    normalized at runtime). Tune via backtest, not by editing code."""

    pullback_from_peak: float = 25.0
    stale_high: float = 15.0
    velocity_drop: float = 15.0
    flow_reversal: float = 20.0
    imbalance_shift: float = 10.0
    negative_momentum: float = 15.0
    vwap_loss: float = 10.0


class ReversalParams(BaseModel):
    # thresholds that map raw observations to component scores (backtest-tunable)
    pullback_full_score_pct: float = 1.2      # % retrace off peak → full component score
    stale_high_full_score_s: float = 20.0     # seconds without a new high → full score
    velocity_drop_ratio: float = 0.4          # recent/earlier trade-rate below this → full
    flow_window_s: float = 10.0               # rolling window for buy/sell flow
    momentum_window_s: float = 8.0            # short-term momentum lookback
    min_peak_gain_pct: float = 0.15           # ignore "reversal" before any real move


class ShortParams(BaseModel):
    stop_loss_pct: float = 1.0                # above short entry
    take_profit_pct: float = 1.5              # below short entry
    trailing_stop_pct: float = 0.6            # trail once in profit
    trailing_activation_pct: float = 0.5      # profit needed before trailing arms
    entry_max_bounce_pct: float = 0.5         # skip short if price bounced back this much


class LongParams(BaseModel):
    initial_stop_pct: float = 1.5             # hard stop under long entry
    max_holding_seconds: float = 1800.0       # absolute cap for the long leg
    order_type: str = "MARKET"                # MARKET | AGGRESSIVE_LIMIT
    aggressive_limit_offset_pct: float = 0.05 # cross the spread by this much


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    mode: Mode = Field(default=Mode.PAPER, alias="MODE")
    enable_live_trading: bool = Field(default=False, alias="ENABLE_LIVE_TRADING")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    signal_source: SignalSource = Field(default=SignalSource.TELEGRAM, alias="SIGNAL_SOURCE")
    strategy_mode: StrategyMode = Field(default=StrategyMode.SHORT_ONLY, alias="STRATEGY_MODE")

    # X / Twitter (only used when SIGNAL_SOURCE includes X)
    x_bearer_token: str = Field(default="", alias="X_BEARER_TOKEN")
    x_target_username: str = Field(default="eliz883", alias="X_TARGET_USERNAME")
    x_listener_mode: ListenerMode = Field(default=ListenerMode.AUTO, alias="X_LISTENER_MODE")
    x_poll_interval_seconds: float = Field(default=8.0, alias="X_POLL_INTERVAL_SECONDS")
    x_api_base: str = "https://api.x.com"
    max_tweet_age_seconds: float = Field(default=45.0, alias="MAX_TWEET_AGE_SECONDS")

    # Binance USDS-M futures
    binance_api_key: str = Field(default="", alias="BINANCE_API_KEY")
    binance_api_secret: str = Field(default="", alias="BINANCE_API_SECRET")
    binance_futures_testnet: bool = Field(default=False, alias="BINANCE_FUTURES_TESTNET")
    fapi_base: str = "https://fapi.binance.com"
    fapi_testnet_base: str = "https://testnet.binancefuture.com"
    fstream_base: str = "wss://fstream.binance.com"
    recv_window_ms: int = 5000

    # Telegram notifications (bot)
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

    # Telegram SOURCE ingestion (your own user account via MTProto/Telethon;
    # api credentials from https://my.telegram.org — free)
    telegram_api_id: int = Field(default=0, alias="TELEGRAM_API_ID")
    telegram_api_hash: str = Field(default="", alias="TELEGRAM_API_HASH")
    telegram_session: str = Field(default="", alias="TELEGRAM_SESSION")
    tg_source_channels: str = Field(default="", alias="TG_SOURCE_CHANNELS")
    tg_max_message_age_seconds: float = Field(default=90.0,
                                              alias="TG_MAX_MESSAGE_AGE_SECONDS")

    # SHORT_ONLY mode gates
    min_pump_percent: float = Field(default=1.5, alias="MIN_PUMP_PERCENT")
    pump_watch_window_seconds: float = Field(default=900.0,
                                             alias="PUMP_WATCH_WINDOW_SECONDS")

    # LLM (optional, ambiguous tweets only)
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    llm_model: str = "claude-opus-5"

    # Storage
    database_url: str = Field(default="sqlite+aiosqlite:///data/elizbot.db", alias="DATABASE_URL")

    # Risk
    account_capital: float = Field(default=500.0, alias="ACCOUNT_CAPITAL")
    max_risk_per_trade_usdt: float = Field(default=5.0, alias="MAX_RISK_PER_TRADE_USDT")
    max_daily_loss_usdt: float = Field(default=25.0, alias="MAX_DAILY_LOSS_USDT")
    max_position_notional_usdt: float = Field(default=100.0, alias="MAX_POSITION_NOTIONAL_USDT")
    max_leverage: int = Field(default=2, alias="MAX_LEVERAGE")
    max_trades_per_day: int = Field(default=6, alias="MAX_TRADES_PER_DAY")
    max_consecutive_losses: int = Field(default=3, alias="MAX_CONSECUTIVE_LOSSES")

    # market filters
    max_chase_percent: float = Field(default=1.5, alias="MAX_CHASE_PERCENT")
    max_spread_percent: float = Field(default=0.10, alias="MAX_SPREAD_PERCENT")
    min_24h_volume: float = Field(default=20_000_000.0, alias="MIN_24H_VOLUME")
    min_orderbook_liquidity: float = Field(default=30_000.0, alias="MIN_ORDERBOOK_LIQUIDITY")
    min_confidence: float = Field(default=0.55, alias="MIN_CONFIDENCE")
    trade_early_signals: bool = Field(default=False, alias="TRADE_EARLY_SIGNALS")

    # data quality / kill switch
    max_data_staleness_seconds: float = 5.0
    max_signal_to_order_latency_ms: float = 3000.0

    # reversal / legs
    min_reversal_score: float = Field(default=65.0, alias="MIN_REVERSAL_SCORE")
    short_confirmation_seconds: float = Field(default=5.0, alias="SHORT_CONFIRMATION_SECONDS")
    max_short_holding_seconds: float = Field(default=900.0, alias="MAX_SHORT_HOLDING_SECONDS")
    short_confirmation_window_s: float = Field(default=45.0,
                                               alias="SHORT_CONFIRMATION_WINDOW_SECONDS")
    reversal_weights: ReversalWeights = ReversalWeights()
    reversal_params: ReversalParams = ReversalParams()
    long_params: LongParams = LongParams()
    short_params: ShortParams = ShortParams()

    # costs (paper fills + backtest; futures taker default)
    taker_fee_rate: float = 0.0005
    paper_slippage_bps: float = 3.0

    data_dir: Path = Path("data")

    @property
    def tg_channels(self) -> list[str]:
        return [c.strip() for c in self.tg_source_channels.split(",") if c.strip()]

    @property
    def live_execution_enabled(self) -> bool:
        """True only when BOTH safety flags are set (spec §13)."""
        return self.mode == Mode.LIVE and self.enable_live_trading

    @property
    def fapi_url(self) -> str:
        return self.fapi_testnet_base if self.binance_futures_testnet else self.fapi_base

    def secrets(self) -> list[str]:
        return [s for s in (self.x_bearer_token, self.binance_api_key, self.binance_api_secret,
                            self.telegram_bot_token, self.anthropic_api_key,
                            self.telegram_api_hash, self.telegram_session) if s]


@lru_cache
def get_settings() -> Settings:
    return Settings()
