"""Shared enumerations. String-valued for painless JSON/DB serialization."""

from enum import StrEnum


class SourceType(StrEnum):
    MARKET = "MARKET"
    TELEGRAM = "TELEGRAM"
    GITHUB = "GITHUB"
    NEWS = "NEWS"
    SENTIMENT = "SENTIMENT"
    SYSTEM = "SYSTEM"


class Direction(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class TimeInForce(StrEnum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class OrderStatus(StrEnum):
    PENDING_SUBMIT = "PENDING_SUBMIT"  # intent persisted, not yet sent
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"  # submit outcome unknown (timeout/5xx) — must reconcile


class Venue(StrEnum):
    PAPER = "PAPER"
    TESTNET = "TESTNET"
    PRODUCTION = "PRODUCTION"  # intents only; execution is hard-disabled


class SystemState(StrEnum):
    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    PAUSED = "PAUSED"
    DATA_STALE = "DATA_STALE"
    RISK_LOCK = "RISK_LOCK"


class ExecutionMode(StrEnum):
    PAPER_ONLY = "PAPER_ONLY"
    TESTNET_ACTIVE = "TESTNET_ACTIVE"


class Regime(StrEnum):
    STRONG_UPTREND = "STRONG_UPTREND"
    WEAK_UPTREND = "WEAK_UPTREND"
    RANGE = "RANGE"
    HIGH_VOL_RANGE = "HIGH_VOL_RANGE"
    DOWNTREND = "DOWNTREND"
    PANIC = "PANIC"
    LOW_LIQUIDITY = "LOW_LIQUIDITY"
    VOL_EXPANSION = "VOL_EXPANSION"
    VOL_COMPRESSION = "VOL_COMPRESSION"
    UNKNOWN = "UNKNOWN"


class StrategyStage(StrEnum):
    EXPERIMENTAL = "EXPERIMENTAL"
    BACKTESTED = "BACKTESTED"
    VALIDATED = "VALIDATED"
    PAPER = "PAPER"
    TESTNET = "TESTNET"
    DEGRADED = "DEGRADED"
    DISABLED = "DISABLED"


class SignalDecision(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    DO_NOTHING = "DO_NOTHING"


class EventCategory(StrEnum):
    LISTING = "LISTING"
    DELISTING = "DELISTING"
    HACK = "HACK"
    EXPLOIT = "EXPLOIT"
    PARTNERSHIP = "PARTNERSHIP"
    TOKEN_UNLOCK = "TOKEN_UNLOCK"
    OUTAGE = "OUTAGE"
    REGULATORY = "REGULATORY"
    RUMOR = "RUMOR"
    WHALE = "WHALE"
    SENTIMENT = "SENTIMENT"
    RELEASE = "RELEASE"
    DEVELOPMENT = "DEVELOPMENT"
    SECURITY_ADVISORY = "SECURITY_ADVISORY"
    MACRO = "MACRO"
    OTHER = "OTHER"


class PipelineStage(StrEnum):
    DATA_QUALITY = "DATA_QUALITY"
    SIGNAL_GENERATION = "SIGNAL_GENERATION"
    SIGNAL_CONFIRMATION = "SIGNAL_CONFIRMATION"
    STRATEGY_FILTER = "STRATEGY_FILTER"
    REGIME_FILTER = "REGIME_FILTER"
    RISK_ENGINE = "RISK_ENGINE"
    PORTFOLIO_FILTER = "PORTFOLIO_FILTER"
    EXECUTION_SIMULATION = "EXECUTION_SIMULATION"
    FINAL_DECISION = "FINAL_DECISION"
