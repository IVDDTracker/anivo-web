"""Event study tests: synthetic pump/dump ticks with KNOWN ground truth."""

from __future__ import annotations

import pytest

from src.backtest.event_study import (
    DumpParams,
    EventResult,
    EventTicks,
    aggregate,
    analyze_event,
    build_phrase_edge_table,
)

T0_MS = 1_800_000_000_000  # arbitrary epoch ms


def synth_ticks(*, pump_s=60.0, peak_gain=2.0, dump_s=120.0, dump_depth=1.5,
                rate_hz=2.0, ref=100.0) -> EventTicks:
    """Pump to +peak_gain% over pump_s, then dump dump_depth% over dump_s.
    During the dump trades flip to aggressive sells."""
    rows = []
    n_pump = int(pump_s * rate_hz)
    for i in range(n_pump):
        t = i / rate_hz
        price = ref * (1 + peak_gain / 100 * (i / max(n_pump - 1, 1)))
        rows.append([T0_MS + int(t * 1000), price, 1.0, False])
    peak_price = ref * (1 + peak_gain / 100)
    n_dump = int(dump_s * rate_hz)
    for i in range(1, n_dump + 1):
        t = pump_s + i / rate_hz
        price = peak_price * (1 - dump_depth / 100 * (i / n_dump))
        rows.append([T0_MS + int(t * 1000), price, 1.5, True])
    return EventTicks.from_rows(T0_MS, rows)


class TestAnalyzeEvent:
    def test_peak_timing_and_returns(self):
        r = analyze_event(synth_ticks())
        assert r.ok
        assert r.reference_price == pytest.approx(100.0)
        assert r.tweet_to_peak_s == pytest.approx(60.0, abs=1.0)
        assert r.peak_return_pct == pytest.approx(2.0, abs=0.05)
        assert r.post_peak_drawdown_pct == pytest.approx(1.5, abs=0.1)
        assert r.returns_pct["+30s"] == pytest.approx(1.0, abs=0.1)  # mid-pump
        assert r.mfe_pct == pytest.approx(2.0, abs=0.05)

    def test_return_grid_has_no_lookahead(self):
        """The +Δs return must equal the LAST trade at/before Δ, never a later one."""
        rows = [[T0_MS, 100.0, 1.0, False],
                [T0_MS + 4_000, 101.0, 1.0, False],
                [T0_MS + 20_000, 150.0, 1.0, False]]  # far future spike
        r = analyze_event(EventTicks.from_rows(T0_MS, rows + [
            [T0_MS + 30_000 + i * 1000, 150.0, 1.0, False] for i in range(10)]))
        assert r.returns_pct["+5s"] == pytest.approx(1.0)   # not the +20s spike
        assert r.returns_pct["+10s"] == pytest.approx(1.0)

    def test_dump_definitions_fire_and_are_ordered(self):
        r = analyze_event(synth_ticks())
        d1 = r.dump_start_s["D1_retrace"]
        d2 = r.dump_start_s["D2_stale_momentum"]
        d4 = r.dump_start_s["D4_flow_flip"]
        assert all(isinstance(v, float) for v in (d1, d2, d4))
        for v in (d1, d2, d4):
            assert v > 60.0  # all after the actual peak
        assert r.peak_to_dump_s["D1_retrace"] == pytest.approx(d1 - 60.0, abs=1.5)
        # definitions disagree — that's the point of comparing them
        assert len({round(d1), round(d2), round(d4)}) >= 2

    def test_d5_reported_unavailable_not_faked(self):
        r = analyze_event(synth_ticks())
        assert r.dump_start_s["D5_orderbook_imbalance"] == "UNAVAILABLE_HISTORICALLY"

    def test_no_pump_no_dump_detection(self):
        flat = EventTicks.from_rows(T0_MS, [
            [T0_MS + i * 500, 100.0 + 0.01 * ((i % 3) - 1), 1.0, i % 2 == 0]
            for i in range(200)])
        r = analyze_event(flat)
        assert r.ok and r.dump_start_s["D1_retrace"] is None
        assert "no meaningful pump" in r.reason

    def test_insufficient_data_excluded(self):
        tiny = EventTicks.from_rows(T0_MS, [[T0_MS + 1000, 100.0, 1.0, False]])
        r = analyze_event(tiny)
        assert not r.ok and "insufficient" in r.reason

    def test_no_dump_when_price_only_rises(self):
        rows = [[T0_MS + i * 500, 100.0 * (1 + 0.0001 * i), 1.0, False]
                for i in range(300)]
        r = analyze_event(EventTicks.from_rows(T0_MS, rows))
        assert r.dump_start_s["D1_retrace"] is None
        assert r.dump_start_s["D4_flow_flip"] is None

    def test_retrace_threshold_is_parameterized(self):
        shallow = analyze_event(synth_ticks(dump_depth=0.5),
                                params=DumpParams(retrace_fraction=0.9))
        assert shallow.dump_start_s["D1_retrace"] is None  # 90% retrace never reached
        strict = analyze_event(synth_ticks(dump_depth=0.5),
                               params=DumpParams(retrace_fraction=0.1))
        assert isinstance(strict.dump_start_s["D1_retrace"], float)


class TestAggregate:
    def _results(self):
        out = []
        for i, (peak_s, stage, phrases) in enumerate([
                (30, "CONFIRMED", ["bought"]), (60, "CONFIRMED", ["long"]),
                (90, "EARLY", ["looks interesting"]), (45, "EARLY", ["watching"]),
                (120, "CONFIRMED", ["bought"])]):
            r = analyze_event(synth_ticks(pump_s=peak_s))
            r.tweet_id, r.symbol, r.stage, r.phrases = str(i), "TAOUSDT", stage, phrases
            out.append(r)
        failed = EventResult(tweet_id="x", symbol="TAOUSDT", stage="CONFIRMED",
                             reason="no tick data")
        return out + [failed]

    def test_percentiles_and_sample_sizes(self):
        report = aggregate(self._results())
        assert report["events_analyzed"] == 5
        assert report["events_excluded"][0]["reason"] == "no tick data"
        peak = report["all_events"]["tweet_to_peak_seconds"]
        assert peak["n"] == 5
        assert peak["p25"] <= peak["median"] <= peak["p75"] <= peak["p90"]
        assert report["by_stage"]["EARLY"]["n"] == 2
        assert report["by_phrase"]["bought"]["n"] == 2
        assert "headline_answer" in report
        assert "median" in report["headline_answer"]["answer"]

    def test_resolution_disclosed(self):
        report = aggregate(self._results())
        assert "millisecond" in report["data_resolution"]

    def test_phrase_edge_table(self):
        table = build_phrase_edge_table(self._results(), min_gain_pct=0.3)
        assert table["bought"]["n"] == 2 and table["bought"]["edge"] == 1.0
        assert "looks interesting" in table


class TestSimulator:
    async def test_replay_produces_trades(self, tmp_path, monkeypatch, db):
        import argparse
        import json as _json

        from src.backtest import simulator

        data_dir = tmp_path / "data"
        (data_dir / "ticks").mkdir(parents=True)
        ticks = synth_ticks(pump_s=40.0, peak_gain=2.0, dump_s=180.0, dump_depth=2.5,
                            rate_hz=3.0)
        rows = [[int(t), float(p), float(q), bool(s)] for t, p, q, s in
                zip(ticks.ts_ms, ticks.price, ticks.qty, ticks.is_sell, strict=True)]
        (data_dir / "ticks" / "42_TAOUSDT.json").write_text(_json.dumps(rows))
        (data_dir / "events.json").write_text(_json.dumps([{
            "tweet_id": "42", "symbol": "TAOUSDT", "tweet_ts_ms": T0_MS,
            "stage": "CONFIRMED", "phrases": ["bought"], "text": "bought $TAO",
            "rules": {"tick_size": "0.01", "step_size": "0.1", "min_qty": "0.1",
                      "min_notional": "5"}}]))

        from src.core import config as config_module

        cfg = config_module.Settings(_env_file=None)
        cfg = cfg.model_copy(update={"data_dir": data_dir})
        monkeypatch.setattr(simulator, "get_settings", lambda: cfg)
        result = await simulator.run(argparse.Namespace(latency_s=3.0, spread_bps=4.0,
                                                        limit=0))
        assert result["sessions"][0]["final_state"] in ("DONE", "LONG_OPEN")
        perf = result["performance"]
        assert perf["total_trades"] >= 1
        assert "long_leg" in perf
        assert "synthesized" in result["assumptions"]["note"]
