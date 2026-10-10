#!/usr/bin/env python3
"""Print ELEGOO remote button presses without controlling playback or rebooting."""

import argparse
import time

from pilights.remote import IRRemote, KEY_NAMES


LABELS = {
    "reboot": "Power",
    "pause": "Play/Pause",
    "next": "Forward",
    "previous": "Back",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", help="Linux input device, for example /dev/input/event2")
    args = parser.parse_args()

    def show_key(code, command):
        name = KEY_NAMES.get(code, f"KEY_{code}")
        label = LABELS.get(command, "unknown key")
        print(f"Detected {name}: {label}", flush=True)

    try:
        remote = IRRemote(lambda command: None, device_path=args.device, on_key=show_key)
    except RuntimeError as error:
        parser.exit(1, f"error: {error}\n")

    print(f"Listening on {remote.device.path} ({remote.device.name}). Press buttons; Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        remote.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
