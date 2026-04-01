FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY fournos/ fournos/

RUN pip install --no-cache-dir .

ENTRYPOINT ["kopf", "run", "-m", "fournos.operator", "--liveness=http://0.0.0.0:8080/healthz"]
