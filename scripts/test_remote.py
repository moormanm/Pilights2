#!/usr/bin/env python3
"""Print ELEGOO remote button presses without controlling playback or rebooting."""

import argparse
import time

from pilights.remote import IRRemote


LABELS = {
    "reboot": "Power",
    "pause": "Play/Pause",
    "next": "Forward",
    "previous": "Back",
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pin", type=int, default=25, help="BCM pin for the IR receiver (default 25)")
    parser.add_argument("--edges", action="store_true", help="print every GPIO edge for signal troubleshooting")
    args = parser.parse_args()

    def show_code(code, command):
        label = LABELS.get(command, "unknown button")
        print(f"NEC code 0x{code:08X}: {label}", flush=True)

    def show_edge(level, tick):
        print(f"GPIO edge: level={level} tick={tick}", flush=True)

    try:
        remote = IRRemote(
            args.pin,
            lambda command: print(f"Detected: {LABELS[command]}", flush=True),
            on_code=show_code,
            on_edge=show_edge if args.edges else None,
        )
    except RuntimeError as error:
        parser.exit(1, f"error: {error}\n")

    print("Press Power, Play/Pause, Forward, or Back. Press Ctrl+C to stop.")
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
