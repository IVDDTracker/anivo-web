FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv/quantlab

COPY pyproject.toml README.md ./
COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic
COPY config ./config
COPY scripts ./scripts

RUN pip install --no-cache-dir .

EXPOSE 8000

# Migrations run on container start, then the app (collectors + API + dashboard).
CMD ["sh", "-c", "alembic upgrade head && python -m app.main"]
