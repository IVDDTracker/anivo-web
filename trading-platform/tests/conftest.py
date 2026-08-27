from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.clock import SimClock
from app.storage.db import Database


@pytest.fixture
def sim_clock() -> SimClock:
    return SimClock(datetime(2026, 1, 1, tzinfo=UTC))


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await database.create_all()
    yield database
    await database.dispose()
