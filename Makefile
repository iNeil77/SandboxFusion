HOST ?= 0.0.0.0
PORT ?= 8080
TEST_NP ?= 16
run:
	uvicorn sandbox.server.server:app --reload --host $(HOST) --port $(PORT)

run-online:
	uvicorn sandbox.server.server:app --host $(HOST) --port $(PORT)

install-runtimes:
	cd runtime/python && bash install-python-runtime.sh
	cd runtime/node && npm ci
	cd runtime/go && go build
	cd runtime/lean && lake build

build-base-image:
	docker build . -f scripts/Dockerfile.base -t ineil77/sandbox-fusion-base:25042026

build-server-image:
	docker build . -f scripts/Dockerfile.server -t ineil77/sandbox-fusion-server:25042026

test: test-docker-full

test-docker-full:
	pytest -m "not datalake" -n $(TEST_NP) --sandbox-docker full

test-docker-lite:
	pytest -m "not datalake" -n $(TEST_NP) --sandbox-docker lite

test-case:
	pytest -s -vv -k $(CASE) --sandbox-docker $(MODE)

format:
	pycln --config pyproject.toml
	isort sandbox/*
	yapf -ir sandbox/*

format-client:
	mv scripts/client/pyproject.toml scripts/faas/pyproject.toml && yapf -ir scripts/client/* && mv scripts/faas/pyproject.toml scripts/client/pyproject.toml

# mypy --explicit-package-bases sandbox
check:
	pycln --config pyproject.toml --check
	yapf --diff --recursive sandbox/*
	make test-docker-full
