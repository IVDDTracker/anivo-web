"""M1: core infrastructure tests — state machine, hashing, logging masking, clock."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

import pytest

from app.core.clock import SimClock
from app.core.hashing import (
    deterministic_client_order_id,
    event_hash,
    headline_cluster_key,
)
from app.core.logging import JsonFormatter, mask_value, register_secret
from app.core.state import StateMachine
from app.models.enums import SystemState


class TestStateMachine:
    def test_starting_blocks_positions(self, sim_clock):
        sm = StateMachine(clock=sim_clock)
        ok, reason = sm.can_open_new_positions()
        assert not ok and "starting" in reason

    def test_healthy_allows(self, sim_clock):
        sm = StateMachine(clock=sim_clock)
        sm.mark_started()
        ok, _ = sm.can_open_new_positions("BTCUSDT")
        assert ok

    def test_pause_and_resume(self, sim_clock):
        sm = StateMachine(clock=sim_clock)
        sm.mark_started()
        sm.pause()
        assert sm.state == SystemState.PAUSED
        assert not sm.can_open_new_positions()[0]
        sm.resume()
        assert sm.can_open_new_positions()[0]

    def test_risk_lock_dominates_everything(self, sim_clock):
        sm = StateMachine(clock=sim_clock)
        sm.mark_started()
        sm.risk_lock("daily loss limit")
        sm.pause()
        sm.resume()
        assert sm.state == SystemState.RISK_LOCK
        ok, reason = sm.can_open_new_positions()
        assert not ok and "daily loss" in reason
        sm.risk_unlock()
        assert sm.can_open_new_positions()[0]

    def test_stale_symbol_blocks_that_symbol(self, sim_clock):
        sm = StateMachine(clock=sim_clock)
        sm.mark_started()
        sm.set_symbol_stale("ETHUSDT", True)
        assert sm.state == SystemState.DATA_STALE
        assert not sm.can_open_new_positions("ETHUSDT")[0]
        # explicitly fail-safe: unrelated entry attempts without symbol also block
        assert not sm.can_open_new_positions()[0]
        sm.set_symbol_stale("ETHUSDT", False)
        assert sm.can_open_new_positions("ETHUSDT")[0]

    def test_degraded_component_blocks_new_entries(self, sim_clock):
        sm = StateMachine(clock=sim_clock)
        sm.mark_started()
        sm.set_component_degraded("binance_ws", True)
        assert sm.state == SystemState.DEGRADED
        assert not sm.can_open_new_positions("BTCUSDT")[0]


class TestHashing:
    def test_event_hash_stable_and_order_independent_keys(self):
        h1 = event_hash("binance", {"a": 1, "b": 2})
        h2 = event_hash("binance", {"b": 2, "a": 1})
        assert h1 == h2
        assert event_hash("binance", {"a": 1}) != event_hash("other", {"a": 1})

    def test_cluster_key_groups_same_story(self):
        k1 = headline_cluster_key(["BTCUSDT"], "HACK", "Exchange X hacked, funds moved!")
        k2 = headline_cluster_key(["BTCUSDT"], "HACK", "EXCHANGE X HACKED — funds moved")
        assert k1 == k2
        k3 = headline_cluster_key(["ETHUSDT"], "HACK", "Exchange X hacked, funds moved")
        assert k1 != k3

    def test_client_order_id_deterministic_and_bounded(self):
        a = deterministic_client_order_id("intent-123")
        b = deterministic_client_order_id("intent-123")
        c = deterministic_client_order_id("intent-124")
        assert a == b != c
        assert a.startswith("ql-") and len(a) <= 36


class TestLoggingMasking:
    def test_sensitive_keys_masked(self):
        out = mask_value("api_key", "supersecretvalue")
        assert out == "***MASKED***"
        nested = mask_value("ctx", {"token": "abc123xyz", "other": "fine"})
        assert nested["token"] == "***MASKED***"
        assert nested["other"] == "fine"

    def test_registered_secret_masked_in_message(self):
        register_secret("hunter2secret")
        rec = logging.LogRecord("t", logging.INFO, __file__, 1,
                                "sending key hunter2secret to nobody", None, None)
        line = JsonFormatter().format(rec)
        data = json.loads(line)
        assert "hunter2secret" not in line
        assert "***MASKED***" in data["msg"]

    def test_exception_text_masked(self):
        register_secret("topsecrettoken")
        try:
            raise ValueError("boom with topsecrettoken inside")
        except ValueError:
            import sys
            rec = logging.LogRecord("t", logging.ERROR, __file__, 1, "err", None, sys.exc_info())
            line = JsonFormatter().format(rec)
        assert "topsecrettoken" not in line


class TestSimClock:
    def test_cannot_go_backwards(self):
        clk = SimClock(datetime(2026, 1, 2, tzinfo=UTC))
        with pytest.raises(ValueError):
            clk.advance_to(datetime(2026, 1, 1, tzinfo=UTC))

    async def test_sleep_advances_time(self):
        clk = SimClock(datetime(2026, 1, 1, tzinfo=UTC))
        await clk.sleep(3600)
        assert clk.now() == datetime(2026, 1, 1, 1, tzinfo=UTC)
