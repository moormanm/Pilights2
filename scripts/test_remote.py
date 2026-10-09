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
    args = parser.parse_args()

    try:
        remote = IRRemote(args.pin, lambda command: print(f"Detected: {LABELS[command]}", flush=True))
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
