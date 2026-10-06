"""Light outputs: GPIO pins (Raspberry Pi) or console (for tests)."""

from __future__ import annotations

import sys

# BCM pin numbers, in channel order (physical pins 8, 10, 12, 11, 13, 15, 16, 18).
DEFAULT_PINS = (14, 15, 18, 17, 27, 22, 23, 24)


class Output:
    channels = 8

    def set_mask(self, mask: int) -> None:
        raise NotImplementedError

    def set_status(self, text: str) -> None:
        pass

    def close(self) -> None:
        pass


class GpioOutput(Output):
    """Drive relays or SSRs with gpiozero. Use active_low for most relay boards."""

    def __init__(self, pins=DEFAULT_PINS, active_low: bool = False):
        from gpiozero import DigitalOutputDevice  # Imported here so non-Pi hosts can run tests.

        self.channels = len(pins)
        self._devs = [
            DigitalOutputDevice(pin, active_high=not active_low, initial_value=False) for pin in pins
        ]
        self._mask = 0

    def set_mask(self, mask: int) -> None:
        changed = mask ^ self._mask
        for i, dev in enumerate(self._devs):
            if changed >> i & 1:
                dev.value = bool(mask >> i & 1)
        self._mask = mask

    def close(self) -> None:
        self.set_mask(0)
        for dev in self._devs:
            dev.close()


class ConsoleOutput(Output):
    """Show the channel state as one line in the terminal."""

    def __init__(self, channels: int = 8, stream=sys.stdout):
        self.channels = channels
        self._stream = stream
        self._mask = None

    def set_mask(self, mask: int) -> None:
        if mask == self._mask:
            return
        self._mask = mask
        bar = "".join("#" if mask >> i & 1 else "." for i in range(self.channels))
        self._stream.write(f"\r[{bar}]")
        self._stream.flush()

    def close(self) -> None:
        self.set_mask(0)
        self._stream.write("\n")
        self._stream.flush()
