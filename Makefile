PYTHON ?= python3
VENV_DIR ?= .venv
VENV_PYTHON := $(VENV_DIR)/bin/python
VENV_PIP := $(VENV_PYTHON) -m pip
VENV_AUTH := $(VENV_DIR)/bin/telegram-proxy-auth

.PHONY: venv install test test-e2e auth run pre-commit-install clean-venv

venv:
	test -d $(VENV_DIR) || $(PYTHON) -m venv $(VENV_DIR)
	$(VENV_PIP) install --upgrade pip

install: venv
	$(VENV_PIP) install -e '.[dev]'

test: install
	$(VENV_PYTHON) -m pytest -m "not e2e"

test-e2e: install
	$(VENV_PYTHON) -m pytest -m e2e

auth: install
	$(VENV_AUTH)

run: install
	$(VENV_PYTHON) -m uvicorn telegram_proxy_api.app:app --host 0.0.0.0 --port 8080

pre-commit-install: install
	$(VENV_DIR)/bin/pre-commit install

clean-venv:
	rm -rf $(VENV_DIR)
