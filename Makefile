.PHONY: dev-setup dev-run dev-test dev-teardown lint format ci-setup ci-run ci-test

KIND_CLUSTER_NAME              ?= fournos-dev
KIND_EXPERIMENTAL_PROVIDER     ?= podman
KIND_CONTEXT                   := kind-$(KIND_CLUSTER_NAME)
FOURNOS_RECONCILE_INTERVAL_SEC ?= 10
VENV_BIN                       := $(if $(wildcard .venv/bin/),.venv/bin/,)

# Local dev cluster (kind + Tekton + Kueue + mock resources)
dev-setup:
	@KIND_CLUSTER_NAME=$(KIND_CLUSTER_NAME) \
	 KIND_EXPERIMENTAL_PROVIDER=$(KIND_EXPERIMENTAL_PROVIDER) \
	 bash dev/setup.sh

dev-run:
	kubectl config use-context $(KIND_CONTEXT)
	FOURNOS_ADMISSION_POLL_INTERVAL_SEC=1 FOURNOS_RECONCILE_INTERVAL_SEC=$(FOURNOS_RECONCILE_INTERVAL_SEC) \
	  $(VENV_BIN)uvicorn fournos.app:app --reload --host 127.0.0.1 --port 8000 --log-config fournos/log-config.yaml

dev-test:
	kubectl config use-context $(KIND_CONTEXT)
	FOURNOS_RECONCILE_INTERVAL_SEC=$(FOURNOS_RECONCILE_INTERVAL_SEC) \
	  $(VENV_BIN)pytest tests/ -v -s

dev-teardown:
	KIND_EXPERIMENTAL_PROVIDER=$(KIND_EXPERIMENTAL_PROVIDER) kind delete cluster --name $(KIND_CLUSTER_NAME)

# CI targets
ci-setup:
	@KIND_CLUSTER_NAME=$(KIND_CLUSTER_NAME) \
	 KIND_EXPERIMENTAL_PROVIDER=$(KIND_EXPERIMENTAL_PROVIDER) \
	 bash dev/setup.sh

ci-run:
	kubectl config use-context $(KIND_CONTEXT)
	FOURNOS_RECONCILE_INTERVAL_SEC=$(FOURNOS_RECONCILE_INTERVAL_SEC) \
	  $(VENV_BIN)uvicorn fournos.app:app --host 127.0.0.1 --port 8000 --log-config fournos/log-config.yaml & \
	echo $$! > fournos.pid; \
	echo "Waiting for Fournos to be ready..."; \
	for i in $$(seq 1 30); do \
	  curl -sf --connect-timeout 1 --max-time 1 http://localhost:8000/healthz > /dev/null 2>&1 && echo "Fournos is up" && break; \
	  if [ $$i -eq 30 ]; then echo "Fournos failed to start"; kill $$(cat fournos.pid); exit 1; fi; \
	  sleep 1; \
	done

ci-test: dev-test

# Code quality
lint:
	$(VENV_BIN)ruff check fournos/

format:
	$(VENV_BIN)ruff format fournos/
