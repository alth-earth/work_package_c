MAMBA_PREFIX := $(CURDIR)/.mamba-env
MAMBA_ROOT_PREFIX := $(CURDIR)/.mamba-root
UV := $(MAMBA_PREFIX)/bin/uv
export UV_CACHE_DIR ?= $(CURDIR)/.uv-cache
export UV_PYTHON_INSTALL_DIR ?= $(CURDIR)/.uv-python
export UV_PYTHON_DOWNLOADS ?= never

.PHONY: env-create env-update lock sync test lint check demo clean

env-create:
	mamba env create --root-prefix $(MAMBA_ROOT_PREFIX) --prefix $(MAMBA_PREFIX) -f environment.yml --yes

env-update:
	mamba env update --root-prefix $(MAMBA_ROOT_PREFIX) --prefix $(MAMBA_PREFIX) -f environment.yml --prune --yes

lock:
	@test -x "$(UV)" || (echo "请先执行: make env-create" && exit 1)
	$(UV) lock --python "$(MAMBA_PREFIX)/bin/python"

sync:
	@test -x "$(UV)" || (echo "请先执行: make env-create" && exit 1)
	$(UV) sync --python "$(MAMBA_PREFIX)/bin/python" --locked

test:
	$(UV) run --locked pytest

lint:
	$(UV) run --locked ruff check src tests

check: lint test
	$(UV) lock --check --python "$(MAMBA_PREFIX)/bin/python"
	$(UV) sync --check --python "$(MAMBA_PREFIX)/bin/python"
	$(UV) run --locked arctic-route-plan --help
	$(UV) run --locked arctic-route-motion --help

demo:
	$(UV) run --locked arctic-route-plan synthetic-demo --output-dir output/demo

clean:
	rm -rf .venv .pytest_cache .ruff_cache htmlcov .coverage output/demo
