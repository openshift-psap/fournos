.PHONY: dev-setup dev-run dev-test dev-teardown lint

KIND_CLUSTER_NAME              ?= fournos-dev
KIND_CONTEXT                   := kind-$(KIND_CLUSTER_NAME)
FOURNOS_RECONCILE_INTERVAL_SEC ?= 10

# Local dev cluster (kind + Tekton + Kueue + mock resources)
dev-setup:
	@KIND_CLUSTER_NAME=$(KIND_CLUSTER_NAME) bash dev/setup.sh

dev-run:
	kubectl config use-context $(KIND_CONTEXT)
	FOURNOS_RECONCILE_INTERVAL_SEC=$(FOURNOS_RECONCILE_INTERVAL_SEC) .venv/bin/uvicorn fournos.app:app --reload --host 127.0.0.1 --port 8000 --log-config fournos/log-config.yaml

dev-test:
	kubectl config use-context $(KIND_CONTEXT)
	FOURNOS_RECONCILE_INTERVAL_SEC=$(FOURNOS_RECONCILE_INTERVAL_SEC) .venv/bin/pytest tests/ -v -s

dev-teardown:
	KIND_EXPERIMENTAL_PROVIDER=podman kind delete cluster --name $(KIND_CLUSTER_NAME)

# Code quality
lint:
	.venv/bin/ruff check fournos/

format:
	.venv/bin/ruff format fournos/
