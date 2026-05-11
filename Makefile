IMG ?= quay.io/rh_perfscale/fournos:latest
KIND_CLUSTER_NAME              ?= fournos-dev
KIND_EXPERIMENTAL_PROVIDER     ?= podman
KIND_CONTEXT                   := kind-$(KIND_CLUSTER_NAME)
VENV_BIN                       := $(if $(wildcard .venv/bin/),.venv/bin/,)
FOURNOS_CONTROLLER_NAMESPACE   ?= fournos-controller-local
FOURNOS_WORKLOAD_NAMESPACE     ?= fournos-local-dev
FOURNOS_SECRETS_NAMESPACE      ?= psap-secrets

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
	kubectl create ns $(FOURNOS_CONTROLLER_NAMESPACE) --dry-run=client -oyaml | kubectl apply -f-
	kubectl create ns $(FOURNOS_WORKLOAD_NAMESPACE) --dry-run=client -oyaml | kubectl apply -f-
	kubectl label ns $(FOURNOS_WORKLOAD_NAMESPACE) fournos.dev/queue-access=true --overwrite
	kubectl create ns $(FOURNOS_SECRETS_NAMESPACE) --dry-run=client -oyaml | kubectl apply -f-
	kubectl apply -f manifests/rbac/sa_fournos.yaml -n $(FOURNOS_CONTROLLER_NAMESPACE)
	kubectl apply -f manifests/rbac/sa_fournos.yaml -n $(FOURNOS_WORKLOAD_NAMESPACE)
	for rbac_file in manifests/rbac/role_fournos.yaml manifests/rbac/rolebinding_fournos.yaml; do \
		cat $$rbac_file | CONTROLLER_NAMESPACE=$(FOURNOS_CONTROLLER_NAMESPACE) envsubst '$$CONTROLLER_NAMESPACE' | kubectl apply -f- -n $(FOURNOS_WORKLOAD_NAMESPACE); \
	done
	cat manifests/rbac/clusterrole_fournos.yaml | kubectl apply -f-
	cat manifests/rbac/clusterrolebinding_fournos.yaml | CONTROLLER_NAMESPACE=$(FOURNOS_CONTROLLER_NAMESPACE) envsubst '$$CONTROLLER_NAMESPACE' | kubectl apply -f-
	cat manifests/secrets-ns-rbac.yaml \
		| CONTROLLER_NAMESPACE=$(FOURNOS_CONTROLLER_NAMESPACE) SECRETS_NAMESPACE=$(FOURNOS_SECRETS_NAMESPACE) envsubst \
		| kubectl apply -f-
	kubectl apply -f config/kueue-cluster-config.yaml
	kubectl apply -f config/kueue-config.yaml -n $(FOURNOS_WORKLOAD_NAMESPACE)
	for wf in config/forge/workflows/*.yaml; do \
		cat $$wf | NAMESPACE=$(FOURNOS_WORKLOAD_NAMESPACE) envsubst '$$NAMESPACE' | kubectl apply -f- -n $(FOURNOS_WORKLOAD_NAMESPACE); \
	done
	cat manifests/deployment.yaml | NAMESPACE=$(FOURNOS_WORKLOAD_NAMESPACE) envsubst '$$NAMESPACE' | kubectl apply -f- -n $(FOURNOS_CONTROLLER_NAMESPACE)

##@ Testing

test:
	FOURNOS_WORKLOAD_NAMESPACE=$(or $(FOURNOS_WORKLOAD_NAMESPACE),fournos-local-dev) \
	FOURNOS_SECRETS_NAMESPACE=$(or $(FOURNOS_SECRETS_NAMESPACE),psap-secrets) \
	$(VENV_BIN)pytest -v tests/

##@ Secrets

sync-vault-secrets:
	$(VENV_BIN)python hacks/sync_vault_secrets.py -n $(FOURNOS_SECRETS_NAMESPACE)

sync-vault-secrets-dry-run:
	$(VENV_BIN)python hacks/sync_vault_secrets.py -n $(FOURNOS_SECRETS_NAMESPACE) --dry-run

##@ Local Development

dev-setup:
	@KIND_CLUSTER_NAME=$(KIND_CLUSTER_NAME) \
	 KIND_EXPERIMENTAL_PROVIDER=$(KIND_EXPERIMENTAL_PROVIDER) \
	 FOURNOS_CONTROLLER_NAMESPACE=$(FOURNOS_CONTROLLER_NAMESPACE) \
	 FOURNOS_WORKLOAD_NAMESPACE=$(or $(FOURNOS_WORKLOAD_NAMESPACE),fournos-local-dev) \
	 FOURNOS_SECRETS_NAMESPACE=$(or $(FOURNOS_SECRETS_NAMESPACE),psap-secrets) \
	 bash dev/setup.sh

dev-run:
	FOURNOS_GC_INTERVAL_SEC=5 \
	FOURNOS_CONTROLLER_NAMESPACE=$(FOURNOS_CONTROLLER_NAMESPACE) \
	FOURNOS_WORKLOAD_NAMESPACE=$(or $(FOURNOS_WORKLOAD_NAMESPACE),fournos-local-dev) \
	FOURNOS_SECRETS_NAMESPACE=$(or $(FOURNOS_SECRETS_NAMESPACE),psap-secrets) \
	FOURNOS_RESOLVE_JOB_TEMPLATE=dev/mock-resolve/resolve_job.yaml \
	$(VENV_BIN)python -m fournos

dev-teardown:
	KIND_EXPERIMENTAL_PROVIDER=$(KIND_EXPERIMENTAL_PROVIDER) kind delete cluster --name $(KIND_CLUSTER_NAME)

##@ CI

ci-setup:
	@KIND_CLUSTER_NAME=$(KIND_CLUSTER_NAME) \
	 KIND_EXPERIMENTAL_PROVIDER=docker \
	 FOURNOS_CONTROLLER_NAMESPACE=$(or $(FOURNOS_CONTROLLER_NAMESPACE),fournos-controller-ci-test) \
	 FOURNOS_WORKLOAD_NAMESPACE=$(or $(FOURNOS_WORKLOAD_NAMESPACE),psap-automation-ci-test) \
	 FOURNOS_SECRETS_NAMESPACE=$(or $(FOURNOS_SECRETS_NAMESPACE),psap-secrets) \
	 bash dev/setup.sh

ci-run:
	FOURNOS_GC_INTERVAL_SEC=5 \
	FOURNOS_CONTROLLER_NAMESPACE=$(or $(FOURNOS_CONTROLLER_NAMESPACE),fournos-controller-ci-test) \
	FOURNOS_WORKLOAD_NAMESPACE=$(or $(FOURNOS_WORKLOAD_NAMESPACE),psap-automation-ci-test) \
	FOURNOS_SECRETS_NAMESPACE=$(or $(FOURNOS_SECRETS_NAMESPACE),psap-secrets) \
	FOURNOS_RESOLVE_JOB_TEMPLATE=dev/mock-resolve/resolve_job.yaml \
	  $(VENV_BIN)python -m fournos \
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
