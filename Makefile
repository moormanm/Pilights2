SONGS ?= examples/*.mp3
ARGS ?=
BOOT_CONFIG ?= /boot/firmware/config.txt
RUN := uv run --frozen pilights
RUN_PI := uv run --frozen --extra pi pilights

.PHONY: help install install-pi lock test analyze play gui wiring wiring-gui remote-config remote-setup remote-test remote-sudo clean

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
	$(RUN_PI) play $(SONGS) --loop $(ARGS)

gui: ## Play SONGS in a desktop window
	$(RUN) play $(SONGS) --gui $(ARGS)

wiring: ## Turn on each GPIO channel in turn (Pi)
	$(RUN_PI) test $(ARGS)

wiring-gui: ## Turn on each channel in turn in a desktop window
	$(RUN) test --gui $(ARGS)

remote-config: ## Enable the GPIO IR receiver overlay in the Pi boot config
	@test -f "$(BOOT_CONFIG)" || { echo "Config file not found: $(BOOT_CONFIG). Set BOOT_CONFIG to the correct path." >&2; exit 1; }
	@if sudo grep -Fqx 'dtoverlay=gpio-ir,gpio_pin=25' "$(BOOT_CONFIG)"; then \
		echo "GPIO IR overlay is already enabled in $(BOOT_CONFIG)"; \
	else \
		printf '\n%s\n' 'dtoverlay=gpio-ir,gpio_pin=25' | sudo tee -a "$(BOOT_CONFIG)" >/dev/null; \
		echo "Added GPIO IR overlay to $(BOOT_CONFIG); reboot the Pi to enable it"; \
	fi

remote-setup: install-pi remote-config remote-sudo ## Install and configure the ELEGOO IR receiver
	sudo apt-get install -y ir-keytable
	sudo install -D -o root -g root -m 0644 config/elegoo-21-keymap /etc/rc_keymaps/elegoo-21-keymap
	sudo install -o root -g root -m 0644 config/pilights-ir-keymap.service /etc/systemd/system/pilights-ir-keymap.service
	sudo install -o root -g root -m 0644 config/70-pilights-ir.rules /etc/udev/rules.d/70-pilights-ir.rules
	sudo usermod -aG input pi
	sudo systemctl daemon-reload
	sudo systemctl enable pilights-ir-keymap.service
	sudo udevadm control --reload-rules
	@echo "Remote setup is complete. Reboot the Pi, then log in again so the input group change applies."

remote-test: ## Test the ELEGOO remote buttons on the Pi
	uv run --frozen --extra pi python -m scripts.test_remote $(ARGS)

remote-sudo: ## Allow the pi user to reboot from the ELEGOO remote
	sudo install -o root -g root -m 0440 config/pilights-remote.sudoers /etc/sudoers.d/pilights-remote
	sudo visudo -c

clean: ## Remove the virtual environment, caches and sequence files in examples/
	rm -rf .venv build dist *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
	rm -f examples/*.seq.json
