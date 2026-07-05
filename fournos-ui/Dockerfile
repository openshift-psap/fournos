FROM python:3.12-slim AS base

RUN groupadd --gid 1001 app && \
    useradd --uid 1001 --gid app --create-home app

WORKDIR /opt/fournos-dashboard

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

RUN chown -R app:app /opt/fournos-dashboard

USER app

EXPOSE 8000

ENTRYPOINT ["uvicorn", "app.main:app", \
            "--host", "0.0.0.0", \
            "--port", "8000", \
            "--workers", "1", \
            "--log-level", "info"]
