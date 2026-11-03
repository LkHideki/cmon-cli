.DEFAULT_GOAL := help
.PHONY: help install uninstall doctor test

help:  ## Show this list
	@grep -E '^[a-z]+:.*##' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | expand -t 12

install:  ## Install the cmon command, cmon Bar and background collection, then check them
	@scripts/setup.sh install

uninstall:  ## Remove all of the above (keeps your history in ~/.cmon)
	@scripts/setup.sh uninstall

doctor:  ## Check the installation and say what to fix
	@scripts/setup.sh doctor

test:  ## Run lint, Python tests and cmon Bar tests
	uv run ruff check .
	uv run pytest -q
	menubar/test.sh
