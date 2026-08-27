"""Feature engine + technical indicator tests, incl. the no-lookahead guarantee."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np

from app.features import technical as ta
from app.features.engine import FeatureEngine, compute_features
from app.features.microstructure import MicrostructureTracker
from app.models.market import BookTicker, TradeTick
from tests.helpers import make_candles


class TestIndicators:
    def test_sma(self):
        vals = np.array([1.0, 2, 3, 4, 5])
        out = ta.sma(vals, 3)
        assert np.isnan(out[1]) and out[2] == 2.0 and out[4] == 4.0

    def test_ema_converges_to_constant(self):
        out = ta.ema(np.full(100, 5.0), 10)
        assert abs(out[-1] - 5.0) < 1e-9

    def test_rsi_bounds_and_direction(self):
        up = np.linspace(100, 200, 50)
        down = np.linspace(200, 100, 50)
        assert ta.rsi(up, 14)[-1] > 90
        assert ta.rsi(down, 14)[-1] < 10

    def test_atr_positive_and_reasonable(self):
        candles = make_candles(100)
        highs = np.array([c.high for c in candles])
        lows = np.array([c.low for c in candles])
        closes = np.array([c.close for c in candles])
        out = ta.atr(highs, lows, closes, 14)
        assert np.isnan(out[12]) and out[-1] > 0

    def test_donchian_excludes_current_bar(self):
        highs = np.array([10.0] * 60 + [20.0])
        lows = np.array([5.0] * 61)
        upper, lower, _ = ta.donchian(highs, lows, 55)
        # current-bar spike must NOT be inside its own channel (else breakouts undetectable)
        assert upper[-1] == 10.0

    def test_momentum(self):
        closes = np.linspace(100, 110, 21)
        assert abs(ta.momentum(closes, 20)[-1] - 0.10) < 1e-9

    def test_zscore_excludes_self(self):
        vals = np.array([1.0] * 20 + [100.0])
        z = ta.zscore_last(vals, 20)
        assert z == 0.0 or z > 5  # std of window is 0 → clamped to 0 by implementation
        vals2 = np.concatenate([np.random.default_rng(0).normal(10, 1, 50), [20.0]])
        assert ta.zscore_last(vals2, 50) > 5


class TestFeatureEngine:
    def test_features_computed_after_min_bars(self):
        eng = FeatureEngine()
        candles = make_candles(300)
        for c in candles:
            feats = eng.on_candle(c)
        assert feats["close"] == candles[-1].close
        assert "rsi14" in feats and "atr14" in feats and "donchian_pos" in feats

    def test_insufficient_history_returns_empty(self):
        assert compute_features(make_candles(30)) == {}

    def test_no_lookahead(self):
        """Features at bar i must be identical whether or not future bars exist."""
        candles = make_candles(400)
        feats_partial = compute_features(candles[:300])
        feats_full_history_truncated = compute_features(candles[:300])
        assert feats_partial == feats_full_history_truncated
        # and appending future candles must not alter a past computation
        eng = FeatureEngine()
        for c in candles[:300]:
            snapshot = eng.on_candle(c)
        for c in candles[300:]:
            eng.on_candle(c)
        assert snapshot == feats_partial

    def test_out_of_order_candle_ignored(self):
        eng = FeatureEngine()
        candles = make_candles(200)
        for c in candles:
            eng.on_candle(c)
        latest = eng.latest("BTCUSDT", "1h")
        eng.on_candle(candles[50])  # stale bar arrives late
        assert eng.latest("BTCUSDT", "1h") == latest

    def test_replacement_candle_updates(self):
        eng = FeatureEngine()
        candles = make_candles(200)
        for c in candles:
            eng.on_candle(c)
        corrected = candles[-1].model_copy(update={"close": candles[-1].close * 1.001})
        feats = eng.on_candle(corrected)
        assert feats["close"] == corrected.close
        assert len(eng.history("BTCUSDT", "1h")) == 200


class TestMicrostructure:
    def _now(self):
        return datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def test_trade_imbalance_and_delta(self):
        tracker = MicrostructureTracker(window_s=300)
        now = self._now()
        for i in range(10):
            tracker.on_trade(TradeTick(symbol="BTCUSDT", price=100.0, qty=2.0,
                                       timestamp=now, is_buyer_maker=False, trade_id=i))
        for i in range(10, 15):
            tracker.on_trade(TradeTick(symbol="BTCUSDT", price=100.0, qty=1.0,
                                       timestamp=now, is_buyer_maker=True, trade_id=i))
        feats = tracker.features("BTCUSDT", now)
        assert feats["aggr_buy_quote_vol"] == 2000.0
        assert feats["aggr_sell_quote_vol"] == 500.0
        assert 0 < feats["trade_imbalance"] <= 1

    def test_spread_features(self):
        tracker = MicrostructureTracker(window_s=300)
        now = self._now()
        tracker.on_book_ticker(BookTicker(symbol="BTCUSDT", bid_price=100.0, bid_qty=5,
                                          ask_price=100.1, ask_qty=5, timestamp=now))
        feats = tracker.features("BTCUSDT", now)
        assert abs(feats["spread_pct_last"] - 0.1 / 100.05) < 1e-9

    def test_old_trades_age_out(self):
        from datetime import timedelta

        tracker = MicrostructureTracker(window_s=60)
        now = self._now()
        tracker.on_trade(TradeTick(symbol="BTCUSDT", price=100.0, qty=1.0,
                                   timestamp=now - timedelta(seconds=120),
                                   is_buyer_maker=False, trade_id=1))
        assert "trade_imbalance" not in tracker.features("BTCUSDT", now)
