"""Platform exception hierarchy."""


class QuantLabError(Exception):
    """Base class for all platform errors."""


class ProductionExecutionDisabled(QuantLabError):
    """Raised whenever production Binance order execution is attempted.

    This is a permanent, by-design safety property of the platform.
    There is no configuration that disables this exception.
    """

    def __init__(self, detail: str = "") -> None:
        msg = (
            "Production order execution is HARD-DISABLED by design. "
            "Use the paper engine or the Binance Spot Testnet executor."
        )
        if detail:
            msg = f"{msg} ({detail})"
        super().__init__(msg)


class ConfigurationError(QuantLabError):
    pass


class DataQualityError(QuantLabError):
    pass


class StaleDataError(DataQualityError):
    pass


class CollectorError(QuantLabError):
    pass


class ExchangeError(QuantLabError):
    """An error reported by an exchange API."""

    def __init__(self, message: str, *, code: int | None = None, http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class OrderOutcomeUnknown(QuantLabError):
    """Submit/cancel outcome unknown (timeout, 5xx, -1007). Must reconcile before retry."""


class FilterViolation(QuantLabError):
    """An order does not satisfy exchange symbol filters."""


class RiskRejected(QuantLabError):
    pass


class ReconciliationMismatch(QuantLabError):
    pass
