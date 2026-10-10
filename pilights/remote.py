"""Read the common 21-key ELEGOO NEC infrared remote."""

from __future__ import annotations

import time


REMOTE_COMMANDS = {
    0x00FFA25D: "reboot",
    0x00FF02FD: "pause",
    0x00FFC23D: "next",
    0x00FF22DD: "previous",
}


def logical_code(code: int) -> int:
    """Convert the decoder's least-significant-bit-first value to its displayed NEC code."""
    return int.from_bytes(code.to_bytes(4, "little"), "big")


def command_for_code(code: int) -> str | None:
    """Map a decoded NEC frame to a playback command."""
    return REMOTE_COMMANDS.get(logical_code(code))


class NECDecoder:
    """Decode 32-bit NEC frames from receiver edge levels and microsecond ticks."""

    def __init__(self):
        self._last_level = None
        self._last_tick = None
        self._active = False
        self._bits = 0
        self._count = 0

    def feed(self, level: int, tick: int) -> int | None:
        if self._last_tick is None:
            self._last_level = level
            self._last_tick = tick
            return None

        duration = (tick - self._last_tick) & 0xFFFFFFFF
        falling = self._last_level == 1 and level == 0
        self._last_level = level
        self._last_tick = tick
        if not falling:
            return None

        if 4000 <= duration <= 5000:
            self._active = True
            self._bits = 0
            self._count = 0
            return None
        if not self._active:
            return None
        if 350 <= duration <= 800:
            bit = 0
        elif 1300 <= duration <= 2000:
            bit = 1
        else:
            self._active = False
            return None

        self._bits |= bit << self._count
        self._count += 1
        if self._count != 32:
            return None
        self._active = False
        return self._bits


class IRRemote:
    """Send decoded ELEGOO remote commands to a callback."""

    def __init__(self, pin: int, on_command, on_code=None, on_edge=None):
        try:
            import lgpio
        except ImportError as e:
            raise RuntimeError("remote input needs lgpio; install with `make install-pi`") from e

        self._lgpio = lgpio
        self._on_command = on_command
        self._on_code = on_code
        self._on_edge = on_edge
        self._decoder = NECDecoder()
        self._last_command = None
        self._last_command_at = 0.0
        self._chip = lgpio.gpiochip_open(0)
        if self._chip < 0:
            raise RuntimeError(f"cannot open GPIO chip 0: {lgpio.error_text(self._chip)}")
        status = lgpio.gpio_claim_input(self._chip, pin, lgpio.SET_PULL_UP)
        if status < 0:
            lgpio.gpiochip_close(self._chip)
            raise RuntimeError(f"cannot claim remote input on BCM pin {pin}: {lgpio.error_text(status)}")
        try:
            self._callback = lgpio.callback(self._chip, pin, lgpio.BOTH_EDGES, self._edge)
        except Exception:
            lgpio.gpiochip_close(self._chip)
            raise

    def _edge(self, chip, gpio, level, tick) -> None:
        if self._on_edge is not None:
            self._on_edge(level, tick)
        code = self._decoder.feed(level, tick)
        if code is None:
            return
        command = command_for_code(code)
        if self._on_code is not None:
            self._on_code(logical_code(code), command)
        if command is None:
            return
        now = time.monotonic()
        if command == self._last_command and now - self._last_command_at < 0.3:
            return
        self._last_command = command
        self._last_command_at = now
        self._on_command(command)

    def close(self) -> None:
        self._callback.cancel()
        self._lgpio.gpiochip_close(self._chip)
