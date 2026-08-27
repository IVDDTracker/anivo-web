"""Strategy promotion engine. No stage may be skipped; every decision records
the metric values that justified it (thresholds: PromotionConfig / DECISIONS.md D-017).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config.settings import PromotionConfig
from app.models.enums import StrategyStage

_ORDER = [StrategyStage.EXPERIMENTAL, StrategyStage.BACKTESTED, StrategyStage.VALIDATED,
          StrategyStage.PAPER, StrategyStage.TESTNET]


@dataclass
class PromotionEvidence:
    backtest_trades: int = 0
    backtest_profit_factor: float = 0.0
    backtest_max_dd_pct: float = 100.0
    oos_trades: int = 0
    oos_profit_factor: float = 0.0
    oos_expectancy: float = 0.0
    parameter_stable: bool = False
    min_perturbed_pf: float | None = None
    largest_winner_share: float = 1.0
    scorecard: float = 0.0
    paper_days: float = 0.0
    paper_trades: int = 0
    paper_expectancy: float = 0.0


@dataclass
class PromotionResult:
    from_stage: StrategyStage
    to_stage: StrategyStage
    promoted: bool
    reasons: list[str] = field(default_factory=list)


class PromotionEngine:
    def __init__(self, cfg: PromotionConfig) -> None:
        self.cfg = cfg

    def evaluate(self, current: StrategyStage, ev: PromotionEvidence) -> PromotionResult:
        """Evaluate promotion to the NEXT stage only (stages cannot be skipped)."""
        if current not in _ORDER or current == StrategyStage.TESTNET:
            return PromotionResult(current, current, False, ["no further stage"])
        target = _ORDER[_ORDER.index(current) + 1]
        checks: list[tuple[bool, str]] = []
        cfg = self.cfg

        if target == StrategyStage.BACKTESTED:
            checks = [
                (ev.backtest_trades >= cfg.min_backtest_trades,
                 f"backtest trades {ev.backtest_trades} ≥ {cfg.min_backtest_trades}"),
                (ev.backtest_profit_factor > 1.0,
                 f"backtest PF {ev.backtest_profit_factor:.2f} > 1.0"),
                (ev.backtest_max_dd_pct <= cfg.max_drawdown_pct,
                 f"max DD {ev.backtest_max_dd_pct:.1f}% ≤ {cfg.max_drawdown_pct}%"),
            ]
        elif target == StrategyStage.VALIDATED:
            checks = [
                (ev.oos_trades >= cfg.min_oos_trades,
                 f"OOS trades {ev.oos_trades} ≥ {cfg.min_oos_trades}"),
                (ev.oos_profit_factor >= cfg.min_profit_factor_oos,
                 f"OOS PF {ev.oos_profit_factor:.2f} ≥ {cfg.min_profit_factor_oos}"),
                (ev.oos_expectancy > 0, f"OOS expectancy {ev.oos_expectancy:.4f} > 0"),
                (ev.parameter_stable and (ev.min_perturbed_pf or 0)
                 >= cfg.min_perturbed_profit_factor,
                 f"perturbed PF {ev.min_perturbed_pf} ≥ {cfg.min_perturbed_profit_factor}"),
                (ev.largest_winner_share <= cfg.max_single_winner_share,
                 f"largest winner share {ev.largest_winner_share:.2f} ≤ "
                 f"{cfg.max_single_winner_share}"),
            ]
        elif target == StrategyStage.PAPER:
            checks = [(ev.scorecard >= cfg.min_scorecard,
                       f"scorecard {ev.scorecard:.0f} ≥ {cfg.min_scorecard}")]
        elif target == StrategyStage.TESTNET:
            checks = [
                (ev.paper_days >= cfg.min_paper_days,
                 f"paper days {ev.paper_days:.0f} ≥ {cfg.min_paper_days}"),
                (ev.paper_trades >= 10, f"paper trades {ev.paper_trades} ≥ 10"),
                (ev.paper_expectancy > 0, f"paper expectancy {ev.paper_expectancy:.4f} > 0"),
                (ev.scorecard >= cfg.min_scorecard,
                 f"scorecard {ev.scorecard:.0f} ≥ {cfg.min_scorecard}"),
            ]

        passed = all(ok for ok, _ in checks)
        reasons = [f"{'✓' if ok else '✗'} {desc}" for ok, desc in checks]
        return PromotionResult(current, target if passed else current, passed, reasons)
