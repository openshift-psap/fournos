FROM python:3.12-slim

WORKDIR /app

# Layer 1 – dependencies only (cached unless pyproject.toml changes)
COPY pyproject.toml README.md ./
RUN mkdir -p fournos && touch fournos/__init__.py \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir . \
    && rm -rf fournos

# Layer 2 – application source (rebuilt on every code change)
COPY fournos/ fournos/
RUN pip install --no-cache-dir --no-deps .

RUN useradd --no-create-home --shell /bin/false appuser
USER appuser

ENTRYPOINT ["kopf", "run", "-m", "fournos.operator", "--liveness=http://0.0.0.0:8080/healthz"]
