FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY requirements.lock pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install -r requirements.lock \
    && /opt/venv/bin/pip install --no-deps .

# Weights are verified against the checksums in src/facepass/engine/manifest.py.
RUN /opt/venv/bin/facepass models download --directory /app/models


FROM python:3.12-slim

ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    FACEPASS_MODELS_DIR=/app/models
WORKDIR /app

RUN useradd --create-home --uid 1000 facepass

COPY --from=build /opt/venv /opt/venv
COPY --from=build /app/models /app/models
COPY alembic.ini ./
COPY migrations ./migrations

USER facepass
EXPOSE 8000

CMD ["uvicorn", "facepass.asgi:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
