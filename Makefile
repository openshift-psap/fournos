.PHONY: dev-setup dev-run dev-test dev-teardown lint

# Local dev cluster (kind + Tekton + Kueue + mock resources)
dev-setup:
	@bash dev/setup.sh

dev-run:
	.venv/bin/uvicorn fournos.app:app --reload --host 127.0.0.1 --port 8000

dev-test:
	.venv/bin/pytest tests/ -v -s

dev-teardown:
	KIND_EXPERIMENTAL_PROVIDER=podman kind delete cluster --name fournos-dev

# Code quality
lint:
	.venv/bin/ruff check fournos/

format:
	.venv/bin/ruff format fournos/
