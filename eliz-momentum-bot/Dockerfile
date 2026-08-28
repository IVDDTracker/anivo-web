FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

WORKDIR /srv/elizbot

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

# data dir holds the SQLite DB, tick archives, phrase edge table — mount it
VOLUME ["/srv/elizbot/data"]

# On start the app restores daily risk state from the DB and reconciles open
# positions/orders against the exchange (spec §20) — a restart can never
# duplicate a position or reset a burned daily loss budget.
CMD ["python", "-m", "src.main"]
