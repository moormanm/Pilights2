"""Read remote button presses from a Linux evdev input device."""

from __future__ import annotations

import threading


KEY_COMMANDS = {
    116: "reboot",       # KEY_POWER
    164: "pause",        # KEY_PLAYPAUSE
    163: "next",         # KEY_NEXTSONG
    165: "previous",     # KEY_PREVIOUSSONG
}

KEY_NAMES = {
    116: "KEY_POWER",
    164: "KEY_PLAYPAUSE",
    163: "KEY_NEXTSONG",
    165: "KEY_PREVIOUSSONG",
}


def command_for_key(key_code: int) -> str | None:
    """Map a Linux input key code to a playback command."""
    return KEY_COMMANDS.get(key_code)


def find_remote_device():
    """Find the GPIO infrared receiver input device by its kernel device name."""
    try:
        import evdev
    except ImportError as e:
        raise RuntimeError("remote input needs python-evdev; install with `make install-pi`") from e

    devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
    matches = [device for device in devices if "gpio_ir_recv" in device.name.lower()]
    if len(matches) == 1:
        selected = matches[0]
        for device in devices:
            if device is not selected:
                device.close()
        return selected
    if len(matches) > 1:
        names = ", ".join(f"{device.path} ({device.name})" for device in matches)
        for device in devices:
            device.close()
        raise RuntimeError(f"more than one GPIO IR input device found: {names}; choose one with --remote-device")
    found = ", ".join(f"{device.path} ({device.name})" for device in devices) or "none"
    for device in devices:
        device.close()
    raise RuntimeError(f"no GPIO IR input device found; available input devices: {found}")


class IRRemote:
    """Read remote key-down events and send mapped commands to a callback."""

    def __init__(self, on_command, device_path: str | None = None, on_key=None):
        try:
            import evdev
        except ImportError as e:
            raise RuntimeError("remote input needs python-evdev; install with `make install-pi`") from e

        self._evdev = evdev
        self.device = find_remote_device() if device_path is None else evdev.InputDevice(device_path)
        self._on_command = on_command
        self._on_key = on_key
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._read, name="ir-remote", daemon=True)
        self._thread.start()

    def _read(self) -> None:
        try:
            for event in self.device.read_loop():
                if self._stop.is_set():
                    return
                if event.type != self._evdev.ecodes.EV_KEY or event.value != 1:
                    continue
                command = command_for_key(event.code)
                if self._on_key is not None:
                    self._on_key(event.code, command)
                if command is not None:
                    self._on_command(command)
        except OSError:
            if not self._stop.is_set():
                raise

    def close(self) -> None:
        self._stop.set()
        self.device.close()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=1)
