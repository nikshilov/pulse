# Garden Pulse — top-level developer Makefile
#
# Common entry points for build / test / run / demo. Designed to be the
# first thing a new contributor types after `git clone`.
#
# Usage:
#   make help        # list all targets
#   make build       # compile the Go server
#   make test        # run Go + Python test suites
#   make run         # start the server on 127.0.0.1:18789
#   make demo        # end-to-end demo (see examples/)
#   make clean       # remove build artifacts
#   make lint        # go vet + gofmt check

GO            ?= go
PYTHON        ?= python3
PIP           ?= pip3
PYTEST        ?= $(PYTHON) -m pytest
BIN_DIR       := bin
PULSE_BIN     := $(BIN_DIR)/pulse
PULSE_DATA    ?= $(HOME)/.pulse
PULSE_ADDR    ?= 127.0.0.1:18789

.DEFAULT_GOAL := help
.PHONY: help build test test-go test-py run run-server demo clean lint fmt deps

help: ## Show this help
	@awk 'BEGIN {FS = ":.*?## "} /^[a-zA-Z_-]+:.*?## / {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

deps: ## Install Python dev deps for tests/scripts
	$(PIP) install -r requirements.txt
	$(PIP) install pytest

build: ## Compile the Go server -> bin/pulse
	@mkdir -p $(BIN_DIR)
	$(GO) build -o $(PULSE_BIN) ./cmd/pulse
	@echo "built $(PULSE_BIN)"

test: test-go test-py ## Run all tests (Go + Python)

test-go: ## Run Go test suite
	$(GO) test ./...

test-py: ## Run Python pytest suite
	@if [ -d scripts/tests ]; then \
		$(PYTEST) scripts/tests/ -q; \
	else \
		echo "scripts/tests not found, skipping"; \
	fi

run: ## Run the server in-process (go run ./cmd/pulse)
	$(GO) run ./cmd/pulse -addr $(PULSE_ADDR) -data-dir $(PULSE_DATA)

run-server: build ## Build then start bin/pulse
	$(PULSE_BIN) -addr $(PULSE_ADDR) -data-dir $(PULSE_DATA)

demo: ## Run the end-to-end example (examples/03-end-to-end)
	@if [ -d examples/03-end-to-end ]; then \
		cd examples/03-end-to-end && $(PYTHON) run.py; \
	else \
		echo "examples/03-end-to-end not found"; exit 1; \
	fi

lint: ## go vet + gofmt -l (fail if any file needs gofmt)
	$(GO) vet ./...
	@unformatted=$$(gofmt -l $$(find . -type f -name '*.go' -not -path './.worktrees/*' -not -path './pulse-dev/*' -not -path './lab/*')); \
	if [ -n "$$unformatted" ]; then \
		echo "gofmt would change these files:"; \
		echo "$$unformatted"; \
		exit 1; \
	fi
	@echo "lint clean"

fmt: ## Apply gofmt to all .go files
	gofmt -w $$(find . -type f -name '*.go' -not -path './.worktrees/*' -not -path './pulse-dev/*' -not -path './lab/*')

clean: ## Remove build artifacts
	rm -rf $(BIN_DIR)
	rm -f *.test *.out
	@echo "cleaned"
