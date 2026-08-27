"""Prometheus metrics registry."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, generate_latest

registry = CollectorRegistry()

EVENTS_RECEIVED = Counter("ql_events_received_total", "Events received per source",
                          ["source"], registry=registry)
EVENTS_DEDUPED = Counter("ql_events_deduplicated_total", "Duplicate events dropped",
                         ["source"], registry=registry)
SIGNALS_GENERATED = Counter("ql_signals_generated_total", "Signals generated",
                            ["strategy", "symbol"], registry=registry)
SIGNALS_REJECTED = Counter("ql_signals_rejected_total", "Signals rejected by pipeline stage",
                           ["stage"], registry=registry)
ORDERS_SUBMITTED = Counter("ql_orders_submitted_total", "Orders submitted",
                           ["venue", "side"], registry=registry)
ORDERS_REJECTED = Counter("ql_orders_rejected_total", "Orders rejected",
                          ["venue"], registry=registry)
RISK_REJECTIONS = Counter("ql_risk_rejections_total", "Risk engine rejections",
                          ["check"], registry=registry)
EXCEPTIONS = Counter("ql_exceptions_total", "Unhandled exceptions in supervised tasks",
                     ["component"], registry=registry)

DATA_LAG = Gauge("ql_data_lag_seconds", "Seconds since last market event", ["symbol"],
                 registry=registry)
QUEUE_SIZE = Gauge("ql_queue_size", "Bus subscriber queue sizes", ["queue"],
                   registry=registry)
EQUITY = Gauge("ql_equity", "Current equity", ["venue"], registry=registry)
DRAWDOWN = Gauge("ql_drawdown_pct", "Current drawdown percent", ["venue"], registry=registry)
OPEN_POSITIONS = Gauge("ql_open_positions", "Open positions", ["venue"], registry=registry)
SYSTEM_STATE = Gauge("ql_system_state", "System state (enum index)", registry=registry)
COMPONENT_HEALTHY = Gauge("ql_component_healthy", "Component health (1/0)", ["component"],
                          registry=registry)


def render() -> bytes:
    return generate_latest(registry)
