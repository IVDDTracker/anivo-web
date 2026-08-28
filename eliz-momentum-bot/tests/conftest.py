from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.core.clock import SimClock
from src.storage.database import Database


@pytest.fixture
def sim_clock() -> SimClock:
    return SimClock(datetime(2026, 3, 1, 12, 0, tzinfo=UTC))


@pytest.fixture
async def db(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    await database.create_all()
    yield database
    await database.dispose()
