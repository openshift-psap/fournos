FROM registry.access.redhat.com/ubi10/python-312-minimal:10.1

WORKDIR /opt/app-root/src

# Layer 1 – dependencies only (cached unless pyproject.toml changes)
COPY pyproject.toml ./
RUN mkdir -p fournos && touch fournos/__init__.py \
    && pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir . \
    && rm -rf fournos

# Layer 2 – application source (rebuilt on every code change)
COPY fournos/ fournos/
RUN pip install --no-cache-dir --no-deps .

USER 1001

ENTRYPOINT ["kopf", "run", "-m", "fournos.operator", "--liveness=http://0.0.0.0:8080/healthz"]
