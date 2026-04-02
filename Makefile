IMG ?= quay.io/rh_perfscale/fournos:latest
KIND_CLUSTER_NAME              ?= fournos-dev
KIND_EXPERIMENTAL_PROVIDER     ?= podman
KIND_CONTEXT                   := kind-$(KIND_CLUSTER_NAME)
VENV_BIN                       := $(if $(wildcard .venv/bin/),.venv/bin/,)

.PHONY: lint format test docker-build docker-push \
        install deploy dev-setup dev-run dev-teardown \
        ci-setup ci-run ci-stop

##@ Code Quality

lint:
	$(VENV_BIN)ruff check fournos/ tests/

format:
	$(VENV_BIN)ruff format fournos/ tests/

##@ Container

docker-build:
	docker build -t $(IMG) .

docker-push:
	docker push $(IMG)

##@ Cluster

install:
	kubectl apply -f manifests/crd.yaml

deploy: install
	kubectl apply -f manifests/rbac.yaml
	kubectl apply -f manifests/kueue-config.yaml
	kubectl apply -f manifests/tekton/
	kubectl apply -f manifests/deployment.yaml

##@ Testing

test:
	$(VENV_BIN)pytest -v tests/

##@ Local Development

dev-setup:
	@KIND_CLUSTER_NAME=$(KIND_CLUSTER_NAME) \
	 KIND_EXPERIMENTAL_PROVIDER=$(KIND_EXPERIMENTAL_PROVIDER) \
	 bash dev/setup.sh

dev-run:
	FOURNOS_GC_INTERVAL_SEC=5 $(VENV_BIN)kopf run -m fournos.operator --namespace $(FOURNOS_NAMESPACE)

dev-teardown:
	KIND_EXPERIMENTAL_PROVIDER=$(KIND_EXPERIMENTAL_PROVIDER) kind delete cluster --name $(KIND_CLUSTER_NAME)

##@ CI

ci-setup:
	@KIND_CLUSTER_NAME=$(KIND_CLUSTER_NAME) \
	 KIND_EXPERIMENTAL_PROVIDER=docker \
	 bash dev/setup.sh

ci-run:
	FOURNOS_GC_INTERVAL_SEC=5 \
	  $(VENV_BIN)kopf run -m fournos.operator \
	  --liveness=http://0.0.0.0:8080/healthz > fournos.log 2>&1 & \
	echo $$! > fournos.pid; \
	echo "Waiting for operator to be ready..."; \
	for i in $$(seq 1 30); do \
	  curl -sf --connect-timeout 1 --max-time 1 http://localhost:8080/healthz > /dev/null 2>&1 \
	    && echo "Operator is ready" && break; \
	  if [ $$i -eq 30 ]; then echo "Operator failed to start"; cat fournos.log; exit 1; fi; \
	  sleep 1; \
	done

ci-stop:
	@if [ -f fournos.pid ]; then \
	  kill "$$(cat fournos.pid)" 2>/dev/null || true; \
	  rm -f fournos.pid; \
	fi
	@cat fournos.log 2>/dev/null || true
