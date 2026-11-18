# Titanic MCP Server - developer and demo commands. Run 'make help' for a list.
SHELL := /bin/bash
.DEFAULT_GOAL := help

VERSION        ?= 1.0.0
IMAGE          ?= titanic-mcp:$(VERSION)
CLUSTER        ?= titanic-mcp-demo
NAMESPACE      ?= titanic-mcp
KPS_VERSION    ?= 93.4.0
DB_COMMIT      ?= 867f5bae5c0b53b0035bacb671ae1d3b07bc18ce
DB_SHA256      ?= aa9cf7683387fefae3759a6d51fd03c917836fdc9c61299ffea15de5bf5f327d
GIT_COMMIT     := $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
KUBECTL        ?= $(shell command -v kubectl 2>/dev/null || echo $(HOME)/.docker/bin/kubectl)
DOCKER         ?= $(shell command -v docker 2>/dev/null || echo $(HOME)/.docker/bin/docker)
export PATH    := $(HOME)/.docker/bin:$(PATH)

.PHONY: help install data lint typecheck test check run image helm-lint compose-up compose-down \
        demo demo-cluster demo-infra demo-monitoring demo-app demo-wait demo-run demo-traffic \
        demo-rollout demo-status demo-down

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# ---- development -------------------------------------------------------------------------------------
install: ## Install Python dependencies (uv)
	uv sync --frozen

data: ## Download titanic.db from the pinned commit and verify the checksum (DS-01)
	@mkdir -p data
	curl -fsSL -o data/titanic.db https://raw.githubusercontent.com/davidjamesknight/SQLite_databases_for_learning_data_science/$(DB_COMMIT)/titanic.db
	@echo "$(DB_SHA256)  data/titanic.db" | shasum -a 256 -c -

lint: ## Lint and check the format (ruff)
	uv run ruff check src tests scripts
	uv run ruff format --check src tests scripts

typecheck: ## Static type check (mypy --strict)
	uv run mypy src

test: data ## Run all tests with coverage (PostgreSQL tests need Docker)
	uv run pytest --cov --cov-report=term-missing:skip-covered

check: lint typecheck test helm-lint ## All quality gates

run: data ## Run the server locally (API key mode, no working dataset)
	TMCP_ENV=development TMCP_AUTH_MODE=api_key TMCP_API_KEYS_FILE=deploy/compose/api-keys.yaml \
	TMCP_DB_PATH=data/titanic.db TMCP_POLICY_PATH=policy/policy.yaml TMCP_LOG_JSON=false \
	TMCP_RESOURCE_URL=http://localhost:8000/mcp uv run titanic-mcp

image: ## Build the container image
	$(DOCKER) build -t $(IMAGE) --build-arg VERSION=$(VERSION) --build-arg GIT_COMMIT=$(GIT_COMMIT) \
	  --build-arg BUILD_DATE=$$(date -u +%Y-%m-%dT%H:%M:%SZ) --build-arg DB_COMMIT=$(DB_COMMIT) \
	  --build-arg DB_SHA256=$(DB_SHA256) .

helm-lint: ## Lint the Helm chart with the default and the demo values (DEP-02)
	helm lint charts/titanic-mcp
	helm lint charts/titanic-mcp -f deploy/kind/values-demo.yaml

compose-up: image ## Local environment: server + PostgreSQL in Docker Compose (API keys)
	$(DOCKER) compose -f deploy/compose/docker-compose.yaml up -d --wait

compose-down: ## Stop the local environment
	$(DOCKER) compose -f deploy/compose/docker-compose.yaml down -v

# ---- demo (kind) - DEP-05: 'make demo' starts the full system ------------------------------------------
demo: demo-cluster image demo-infra demo-monitoring demo-app demo-wait ## Start the full demo in a kind cluster
	@echo ""
	@echo "Demo is ready."
	@echo "  MCP server : http://localhost:18080/mcp"
	@echo "  Keycloak   : http://localhost:18081   (admin / demo-admin-password-change-me)"
	@echo "  Grafana    : http://localhost:13000   (anonymous view; admin / demo-grafana-password-change-me)"
	@echo "  Prometheus : http://localhost:19090"
	@echo "  Jaeger     : http://localhost:16686"
	@echo "Run the scenario: make demo-run"

demo-cluster:
	@kind get clusters 2>/dev/null | grep -qx $(CLUSTER) || kind create cluster --config deploy/kind/kind-config.yaml --wait 120s
	$(KUBECTL) config use-context kind-$(CLUSTER)

demo-infra:
	$(KUBECTL) apply -f deploy/kind/namespace.yaml
	$(KUBECTL) apply -f deploy/kind/postgres.yaml -f deploy/kind/keycloak.yaml -f deploy/kind/jaeger.yaml

demo-monitoring:
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
	helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server/ >/dev/null 2>&1 || true
	helm repo update >/dev/null
	helm upgrade --install metrics-server metrics-server/metrics-server --namespace kube-system \
	  --set 'args={--kubelet-insecure-tls}' --wait --timeout 5m   # HPA needs CPU metrics (kind only: self-signed kubelet certs)
	helm upgrade --install monitoring prometheus-community/kube-prometheus-stack --version $(KPS_VERSION) \
	  --namespace monitoring --create-namespace -f deploy/kind/monitoring-values.yaml --wait --timeout 10m

demo-app:
	kind load docker-image $(IMAGE) --name $(CLUSTER)
	helm upgrade --install titanic-mcp charts/titanic-mcp -n $(NAMESPACE) -f deploy/kind/values-demo.yaml \
	  --set image.tag=$(VERSION) --set-string podAnnotations.build=$(GIT_COMMIT)

demo-wait:
	$(KUBECTL) -n $(NAMESPACE) rollout status statefulset/postgres --timeout=5m
	$(KUBECTL) -n $(NAMESPACE) rollout status deploy/keycloak --timeout=10m
	$(KUBECTL) -n $(NAMESPACE) rollout status deploy/titanic-mcp --timeout=10m

demo-run: ## Run the presentation scenario against the demo
	uv run python scripts/demo.py

demo-traffic: ## Send 3 minutes of traffic for the dashboard
	uv run python scripts/demo.py --traffic 180

demo-rollout: ## Rolling update under load: shows that no request fails (spec section 14, step 5)
	uv run python scripts/rollout_check.py

demo-status: ## Show the pods of the demo
	$(KUBECTL) -n $(NAMESPACE) get pods,svc,hpa,pdb

demo-down: ## Delete the kind cluster
	kind delete cluster --name $(CLUSTER)
