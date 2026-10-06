SONGS ?= examples/*.mp3
ARGS ?=
RUN := uv run --frozen pilights

.PHONY: help install install-pi lock test analyze play gui wiring wiring-gui clean

help: ## Show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-12s %s\n", $$1, $$2}'

install: ## Install for desktop use (no GPIO)
	uv sync --frozen

install-pi: ## Install on the Raspberry Pi (with GPIO)
	uv sync --frozen --extra pi

lock: ## Update uv.lock after you change pyproject.toml
	uv lock

test: ## Run the unit tests
	uv run --frozen python -m unittest discover tests

analyze: ## Make sequence files for SONGS
	$(RUN) analyze $(SONGS) $(ARGS)

play: ## Play SONGS in a loop on the GPIO pins (Pi)
	$(RUN) play $(SONGS) --loop $(ARGS)

gui: ## Play SONGS in a desktop window
	$(RUN) play $(SONGS) --gui $(ARGS)

wiring: ## Turn on each GPIO channel in turn (Pi)
	$(RUN) test $(ARGS)

wiring-gui: ## Turn on each channel in turn in a desktop window
	$(RUN) test --gui $(ARGS)

clean: ## Remove the virtual environment, caches and sequence files in examples/
	rm -rf .venv build dist *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -f examples/*.seq.json
