"""Event study (spec §14): what actually happens after an Eliz tweet?

Core research question, answered with distributions (never a single mean):
  "How many seconds after the tweet does the pump end and the dump begin?"

Data: Binance USDⓈ-M aggTrades (millisecond timestamps) fetched per event by
`src.backtest.data_fetcher`. RESOLUTION NOTE: all timings here are as precise
as aggTrade timestamps (ms). If an event's tick file is missing and only 1m
klines exist, the event is EXCLUDED from second-level timing stats rather than
pretending to second precision.

Dump-start is deliberately measured under FOUR alternative definitions (D5,
order-book imbalance, is impossible from historical aggTrades and is reported
as unavailable — not silently approximated):

  D1 retrace      first time price gives back ≥ R of the tweet→peak gain
  D2 stale+mom    no new high for S seconds AND short momentum < 0
  D3 dyn_drawdown drawdown from peak ≥ K × pre-peak 1s volatility
  D4 flow_flip    rolling buy-share over W seconds drops below B after the peak

All thresholds are parameters (CLI/config), not hard-coded constants.

No look-ahead: every "+Δs return" uses only trades with ts ≤ tweet_ts+Δ; peak
and dump detection are descriptive statistics ABOUT the past, used to design
the live reversal logic — they are never fed into simulated trading decisions
(the simulator runs the causal live code path instead).

    python -m src.backtest.event_study [--horizon-min 30] [--retrace 0.3] ...
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field

import numpy as np

RETURN_GRID_SECONDS = [1, 5, 10, 30, 60, 120, 300, 600, 1800]


@dataclass
class DumpParams:
    retrace_fraction: float = 0.3        # D1
    stale_high_seconds: float = 20.0     # D2
    momentum_window_s: float = 8.0       # D2
    vol_multiple: float = 3.0            # D3
    flow_window_s: float = 10.0          # D4
    flow_buy_share: float = 0.40         # D4
    min_peak_gain_pct: float = 0.15      # ignore events that never moved


@dataclass
class EventTicks:
    """ticks: arrays sorted by ts. ts_ms int64, price float, qty float, is_sell bool."""

    tweet_ts_ms: int
    ts_ms: np.ndarray
    price: np.ndarray
    qty: np.ndarray
    is_sell: np.ndarray

    @classmethod
    def from_rows(cls, tweet_ts_ms: int, rows: list) -> EventTicks:
        arr = sorted(rows, key=lambda r: r[0])
        return cls(tweet_ts_ms=tweet_ts_ms,
                   ts_ms=np.array([r[0] for r in arr], dtype=np.int64),
                   price=np.array([r[1] for r in arr], dtype=float),
                   qty=np.array([r[2] for r in arr], dtype=float),
                   is_sell=np.array([bool(r[3]) for r in arr]))


@dataclass
class EventResult:
    tweet_id: str
    symbol: str
    stage: str
    phrases: list = field(default_factory=list)
    ok: bool = False
    reason: str = ""
    reference_price: float = 0.0
    first_reaction_s: float | None = None
    returns_pct: dict = field(default_factory=dict)          # +Δs → % return
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    tweet_to_peak_s: float = 0.0
    peak_return_pct: float = 0.0
    post_peak_drawdown_pct: float = 0.0
    dump_start_s: dict = field(default_factory=dict)         # definition → seconds|None
    peak_to_dump_s: dict = field(default_factory=dict)


def analyze_event(ticks: EventTicks, *, horizon_s: float = 1800.0,
                  params: DumpParams | None = None) -> EventResult | None:
    """Pure per-event analysis (unit-tested; the aggregate report builds on this)."""
    p = params or DumpParams()
    result = EventResult(tweet_id="", symbol="", stage="")
    t0 = ticks.tweet_ts_ms
    horizon_ms = int(horizon_s * 1000)
    mask = (ticks.ts_ms >= t0) & (ticks.ts_ms <= t0 + horizon_ms)
    if mask.sum() < 10:
        result.reason = "insufficient trades after tweet"
        return result
    ts = ticks.ts_ms[mask]
    price = ticks.price[mask]
    qty = ticks.qty[mask]
    is_sell = ticks.is_sell[mask]
    rel_s = (ts - t0) / 1000.0

    ref = float(price[0])
    result.ok = True
    result.reference_price = ref
    result.first_reaction_s = float(rel_s[0])

    # +Δs returns — only data up to each horizon (no look-ahead)
    for delta in RETURN_GRID_SECONDS:
        idx = np.searchsorted(rel_s, delta, side="right") - 1
        if idx >= 0:
            result.returns_pct[f"+{delta}s"] = round(
                (float(price[idx]) / ref - 1.0) * 100.0, 4)

    result.mfe_pct = round((float(price.max()) / ref - 1.0) * 100.0, 4)
    result.mae_pct = round((float(price.min()) / ref - 1.0) * 100.0, 4)

    peak_idx = int(np.argmax(price))
    peak_price = float(price[peak_idx])
    peak_s = float(rel_s[peak_idx])
    result.tweet_to_peak_s = round(peak_s, 3)
    result.peak_return_pct = round((peak_price / ref - 1.0) * 100.0, 4)
    after_peak = price[peak_idx:]
    if len(after_peak) > 1:
        result.post_peak_drawdown_pct = round(
            (peak_price - float(after_peak.min())) / peak_price * 100.0, 4)

    if result.peak_return_pct < p.min_peak_gain_pct:
        result.dump_start_s = {d: None for d in ("D1_retrace", "D2_stale_momentum",
                                                 "D3_dyn_drawdown", "D4_flow_flip")}
        result.dump_start_s["D5_orderbook_imbalance"] = "UNAVAILABLE_HISTORICALLY"
        result.reason = "no meaningful pump (peak gain below threshold)"
        return result

    dumps: dict[str, float | None] = {}

    # D1: give back ≥ retrace_fraction of the gain
    d1_level = peak_price - p.retrace_fraction * (peak_price - ref)
    d1 = _first_after(rel_s, price, peak_idx, lambda i: price[i] <= d1_level)
    dumps["D1_retrace"] = d1

    # D2: no new high for S seconds AND momentum negative
    d2 = None
    running_max_idx = np.maximum.accumulate(
        np.arange(len(price)) * (price == np.maximum.accumulate(price)))
    for i in range(peak_idx + 1, len(price)):
        last_high_s = rel_s[running_max_idx[i]]
        if rel_s[i] - last_high_s >= p.stale_high_seconds:
            m_idx = np.searchsorted(rel_s, rel_s[i] - p.momentum_window_s, side="right") - 1
            if m_idx >= 0 and price[i] < price[m_idx]:
                d2 = float(rel_s[i])
                break
    dumps["D2_stale_momentum"] = d2

    # D3: drawdown from peak ≥ K × pre-peak 1-second volatility
    pre = price[: peak_idx + 1]
    pre_rel = rel_s[: peak_idx + 1]
    d3 = None
    if len(pre) > 5:
        seconds = np.floor(pre_rel).astype(int)
        last_per_sec = {int(s): float(v) for s, v in zip(seconds, pre, strict=True)}
        series = np.array([last_per_sec[k] for k in sorted(last_per_sec)])
        if len(series) > 3:
            rets = np.diff(np.log(series))
            vol_pct = float(np.std(rets)) * 100.0
            threshold_pct = max(p.vol_multiple * vol_pct, 0.05)
            d3 = _first_after(rel_s, price, peak_idx,
                              lambda i: (peak_price - price[i]) / peak_price * 100.0
                              >= threshold_pct)
    dumps["D3_dyn_drawdown"] = d3

    # D4: rolling buy-share flips to sellers after the peak
    d4 = None
    quote = price * qty
    for i in range(peak_idx + 1, len(price)):
        w_start = np.searchsorted(rel_s, rel_s[i] - p.flow_window_s, side="left")
        window_quote = quote[w_start: i + 1]
        window_sell = is_sell[w_start: i + 1]
        total = float(window_quote.sum())
        if total > 0:
            buy_share = float(window_quote[~window_sell].sum()) / total
            if buy_share < p.flow_buy_share:
                d4 = float(rel_s[i])
                break
    dumps["D4_flow_flip"] = d4

    result.dump_start_s = {k: (round(v, 3) if isinstance(v, float) else v)
                           for k, v in dumps.items()}
    result.dump_start_s["D5_orderbook_imbalance"] = "UNAVAILABLE_HISTORICALLY"
    result.peak_to_dump_s = {
        k: (round(v - peak_s, 3) if isinstance(v, float) else None)
        for k, v in dumps.items()}
    return result


def _first_after(rel_s: np.ndarray, price: np.ndarray, start_idx: int,
                 predicate) -> float | None:
    for i in range(start_idx + 1, len(price)):
        if predicate(i):
            return float(rel_s[i])
    return None


def _pcts(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    arr = np.array(values)
    return {"n": len(arr), "mean": round(float(arr.mean()), 2),
            "median": round(float(np.median(arr)), 2),
            "p25": round(float(np.percentile(arr, 25)), 2),
            "p50": round(float(np.percentile(arr, 50)), 2),
            "p75": round(float(np.percentile(arr, 75)), 2),
            "p90": round(float(np.percentile(arr, 90)), 2)}


def aggregate(results: list[EventResult]) -> dict:
    """Distributions per signal stage and per phrase (spec §14). No single-number
    answers: every stat carries its sample size."""
    ok = [r for r in results if r.ok]
    report: dict = {
        "data_resolution": "Binance futures aggTrades (millisecond timestamps); "
                           "events without tick data are excluded, not approximated",
        "events_total": len(results),
        "events_analyzed": len(ok),
        "events_excluded": [{"tweet_id": r.tweet_id, "reason": r.reason}
                            for r in results if not r.ok],
    }

    def block(subset: list[EventResult]) -> dict:
        out: dict = {
            "n": len(subset),
            "tweet_to_peak_seconds": _pcts([r.tweet_to_peak_s for r in subset]),
            "peak_return_pct": _pcts([r.peak_return_pct for r in subset]),
            "post_peak_drawdown_pct": _pcts([r.post_peak_drawdown_pct for r in subset]),
            "tweet_to_dump_seconds": {}, "peak_to_dump_seconds": {},
        }
        for definition in ("D1_retrace", "D2_stale_momentum", "D3_dyn_drawdown",
                           "D4_flow_flip"):
            vals = [r.dump_start_s.get(definition) for r in subset]
            nums = [v for v in vals if isinstance(v, (int, float))]
            out["tweet_to_dump_seconds"][definition] = {
                **_pcts(nums), "no_dump_detected": len(vals) - len(nums)}
            p2d = [r.peak_to_dump_s.get(definition) for r in subset]
            out["peak_to_dump_seconds"][definition] = _pcts(
                [v for v in p2d if isinstance(v, (int, float))])
        out["returns_pct_grid"] = {
            f"+{d}s": _pcts([r.returns_pct[f"+{d}s"] for r in subset
                             if f"+{d}s" in r.returns_pct])
            for d in RETURN_GRID_SECONDS}
        return out

    report["all_events"] = block(ok)
    report["by_stage"] = {stage: block([r for r in ok if r.stage == stage])
                          for stage in sorted({r.stage for r in ok})}
    phrase_set = sorted({ph for r in ok for ph in r.phrases})
    report["by_phrase"] = {ph: block([r for r in ok if ph in r.phrases])
                           for ph in phrase_set}

    d1 = report["all_events"]["tweet_to_dump_seconds"].get("D1_retrace", {})
    report["headline_answer"] = {
        "question": "How many seconds after an Eliz tweet does the dump typically start?",
        "answer": ("Distributions differ by definition and signal type — see "
                   "tweet_to_dump_seconds per definition/stage/phrase. Under D1 "
                   f"(≥30% retrace of the gain): n={d1.get('n', 0)}, "
                   f"median={d1.get('median')}s, p25={d1.get('p25')}s, "
                   f"p75={d1.get('p75')}s, p90={d1.get('p90')}s."),
        "caveats": ["small samples shrink meaning — check every n",
                    "D5 (order-book based) cannot be measured from historical aggTrades",
                    "definitions D1-D4 are parameterized; re-run with other thresholds"],
    }
    return report


def build_phrase_edge_table(results: list[EventResult], *, min_gain_pct: float = 0.3) -> dict:
    """Per-phrase historical edge for the live classifier (data/phrase_edge.json)."""
    table: dict[str, dict] = {}
    for r in results:
        if not r.ok:
            continue
        for phrase in r.phrases:
            entry = table.setdefault(phrase, {"wins": 0, "n": 0})
            entry["n"] += 1
            if r.peak_return_pct >= min_gain_pct:
                entry["wins"] += 1
    return {ph: {"edge": round(v["wins"] / v["n"], 3), "n": v["n"]}
            for ph, v in table.items() if v["n"] > 0}


# ── CLI: run over fetched data ───────────────────────────────────────────────


async def run_cli(args: argparse.Namespace) -> dict:
    from src.core.config import get_settings

    cfg = get_settings()
    events_path = cfg.data_dir / "events.json"
    ticks_dir = cfg.data_dir / "ticks"
    if not events_path.exists():
        return {"error": f"{events_path} not found — run `python -m src.backtest."
                         f"data_fetcher` first (needs X + Binance access)"}
    events = json.loads(events_path.read_text())
    params = DumpParams(retrace_fraction=args.retrace,
                        stale_high_seconds=args.stale_seconds,
                        vol_multiple=args.vol_multiple,
                        flow_buy_share=args.flow_buy_share)
    results: list[EventResult] = []
    for ev in events:
        result = EventResult(tweet_id=ev["tweet_id"], symbol=ev["symbol"],
                             stage=ev.get("stage", "?"), phrases=ev.get("phrases", []))
        tick_file = ticks_dir / f"{ev['tweet_id']}_{ev['symbol']}.json"
        if not tick_file.exists():
            result.reason = "no tick data (excluded — no second-level precision available)"
            results.append(result)
            continue
        rows = json.loads(tick_file.read_text())
        analyzed = analyze_event(
            EventTicks.from_rows(int(ev["tweet_ts_ms"]), rows),
            horizon_s=args.horizon_min * 60.0, params=params)
        analyzed.tweet_id, analyzed.symbol = ev["tweet_id"], ev["symbol"]
        analyzed.stage, analyzed.phrases = ev.get("stage", "?"), ev.get("phrases", [])
        results.append(analyzed)

    report = aggregate(results)
    (cfg.data_dir / "event_study_report.json").write_text(
        json.dumps(report, indent=2, default=str))
    edge = build_phrase_edge_table(results)
    (cfg.data_dir / "phrase_edge.json").write_text(json.dumps(edge, indent=2))
    report["phrase_edge_table_written"] = str(cfg.data_dir / "phrase_edge.json")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--horizon-min", type=float, default=30.0)
    parser.add_argument("--retrace", type=float, default=0.3, help="D1 retrace fraction")
    parser.add_argument("--stale-seconds", type=float, default=20.0, help="D2 stale-high s")
    parser.add_argument("--vol-multiple", type=float, default=3.0, help="D3 vol multiple")
    parser.add_argument("--flow-buy-share", type=float, default=0.40, help="D4 threshold")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run_cli(args)), indent=2, default=str))


if __name__ == "__main__":
    main()
