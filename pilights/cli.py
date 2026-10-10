"""Command-line interface.

    python -m pilights analyze song.mp3 [-o song.seq.json]
    python -m pilights play song.mp3 [song2.mp3 ...] [--loop] [--console]
    python -m pilights test            # chase each channel to check wiring

Add --gui (desktop window) or --console (terminal) to play or test without GPIO.
"""

from __future__ import annotations

import argparse
import json
import queue
import signal
import subprocess
import sys
import time
from pathlib import Path

from .outputs import DEFAULT_PINS, ConsoleOutput, GpioOutput


def _pins(text: str) -> tuple:
    pins = tuple(int(p) for p in text.split(",") if p.strip())
    if not 1 <= len(pins) <= 8:
        raise argparse.ArgumentTypeError("give 1 to 8 comma-separated BCM pin numbers")
    return pins


def _make_output(args):
    if args.gui:
        from .gui import GuiOutput

        return GuiOutput(args.pins, active_low=args.active_low)
    if args.console:
        return ConsoleOutput(len(args.pins))
    return GpioOutput(args.pins, active_low=args.active_low)


def _on_sigterm(signum, frame):
    raise KeyboardInterrupt


def _run(out, work) -> None:
    """Run work(stop) directly, or on a worker thread while the GUI window is open."""
    import threading

    stop = getattr(out, "stop", None) or threading.Event()
    try:
        if hasattr(out, "run"):
            out.run(lambda: work(stop))
        else:
            work(stop)
    except KeyboardInterrupt:
        pass
    finally:
        out.close()


def _analyze_one(audio, out: Path, p):
    from .analyze import analyze_file

    t0 = time.monotonic()
    seq, states = analyze_file(audio, p)
    seq.save(out)
    duty = ", ".join(f"{100 * states[:, c].mean():.0f}%" for c in range(p.channels))
    info = seq.meta.get("analysis", {})
    extra = f"{info['tempo_bpm']} BPM, " if "tempo_bpm" in info else ""
    print(f"{audio} -> {out}: {p.mode} mode, {extra}{len(seq.events)} events, {seq.duration_ms / 1000:.1f}s, "
          f"{time.monotonic() - t0:.1f}s to analyze; ON time per channel: {duty}", file=sys.stderr)
    return seq


def _check_sequence(audio, seq_path: Path, channels: int):
    """Load the sequence. Analyze again if it is missing, from an older analyzer, or from other audio."""
    from .analyze import ANALYZER_VERSION, params_from_options
    from .sequence import MAX_CHANNELS, Sequence, file_sha256

    if not seq_path.exists():
        print(f"{seq_path} not found; analyzing {audio} with default settings", file=sys.stderr)
        return _analyze_one(audio, seq_path, params_from_options({}, channels))
    data = json.loads(seq_path.read_text())
    old_channels = data.get("channels", 0)
    if isinstance(old_channels, int) and old_channels > MAX_CHANNELS:
        meta = data.get("meta", {})
        made_by_pilights = data.get("analyzer_version", 0) > 0 or "params" in meta
        if not made_by_pilights:
            raise ValueError(f"{seq_path}: has {old_channels} channels; this version supports at most "
                             f"{MAX_CHANNELS}. Regenerate it or use a sequence with 8 channels or fewer.")
        options = meta.get("options", meta.get("params", {}))
        print(f"{seq_path}: has {old_channels} channels; analyzing {audio} again with {channels} channels",
              file=sys.stderr)
        return _analyze_one(audio, seq_path, params_from_options(options, channels))
    seq = Sequence.load(seq_path)
    changed = bool(seq.audio_sha256) and seq.audio_sha256 != file_sha256(audio)
    if not seq.made_by_pilights:
        if changed:
            print(f"warning: {seq_path} was made from a different file than {audio}", file=sys.stderr)
        return seq
    if seq.analyzer_version > ANALYZER_VERSION:
        print(f"warning: {seq_path} is from a newer pilights (analyzer {seq.analyzer_version}); "
              f"using it as it is", file=sys.stderr)
        return seq
    if seq.analyzer_version < ANALYZER_VERSION or changed:
        why = "the audio file changed" if changed else \
            f"analyzer version {seq.analyzer_version} is older than {ANALYZER_VERSION}"
        options = seq.meta.get("options", {})
        print(f"{seq_path}: {why}; analyzing {audio} again"
              + (f" with options {options}" if options else ""), file=sys.stderr)
        return _analyze_one(audio, seq_path, params_from_options(options, seq.channels))
    return seq


def _audio_inputs(paths):
    audio = []
    for path in paths:
        if str(path).lower().endswith(".seq.json"):
            print(f"Skipping sequence file: {path}", file=sys.stderr)
        else:
            audio.append(path)
    if not audio:
        raise RuntimeError("no audio files supplied; sequence files are not audio")
    return audio


def _reboot_system() -> None:
    try:
        subprocess.run(["sudo", "-n", "systemctl", "--force", "reboot"], check=True)
    except (OSError, subprocess.CalledProcessError) as e:
        print(f"error: remote reboot failed: {e}; configure passwordless sudo for systemctl reboot",
              file=sys.stderr)


def cmd_analyze(args) -> int:
    from .analyze import AnalyzeParams
    from .sequence import default_sequence_path

    audio_inputs = _audio_inputs(args.audio)
    if args.output and len(audio_inputs) > 1:
        print("error: -o can only be used with one input file", file=sys.stderr)
        return 2
    p = AnalyzeParams(
        mode=args.mode, sensitivity=args.sensitivity, pulse=args.pulse, accent=args.accent,
        channels=args.channels, frame_ms=args.frame_ms, fmin=args.fmin, fmax=args.fmax,
        window_s=args.window, on_z=args.on_z, off_z=args.off_z,
        min_on_ms=args.min_on_ms, min_off_ms=args.min_off_ms, silence_db=args.silence_db,
        rel_floor_db=args.rel_floor_db,
    )
    for audio in audio_inputs:
        _analyze_one(audio, Path(args.output) if args.output else default_sequence_path(audio), p)
    return 0


def cmd_play(args) -> int:
    from .player import Mpg123, PortAudioPlayer, play_song
    from .sequence import default_sequence_path

    audio_inputs = _audio_inputs(args.audio)
    if args.sequence and len(audio_inputs) > 1:
        print("error: -s can only be used with one input file", file=sys.stderr)
        return 2
    songs = []
    for audio in audio_inputs:
        seq_path = Path(args.sequence) if args.sequence else default_sequence_path(audio)
        if not Path(audio).is_file():
            print(f"error: {audio} not found", file=sys.stderr)
            return 2
        songs.append((audio, _check_sequence(audio, seq_path, len(args.pins))))

    out = _make_output(args)
    if args.player == "mpg123":
        player = Mpg123(extra_args=args.mpg123_args.split() if args.mpg123_args else ())
    else:
        player = PortAudioPlayer(int(args.device) if args.device and args.device.isdigit() else args.device)
    commands = queue.Queue()
    remote = None
    if args.remote:
        from .remote import IRRemote

        try:
            remote = IRRemote(commands.put, device_path=args.remote_device)
        except Exception:
            out.close()
            player.close()
            raise

    def work(stop):
        index = 0
        while not stop.is_set():
            audio, seq = songs[index]
            print(f"Playing {audio}", file=sys.stderr)
            command = play_song(player, audio, seq, out, offset_ms=args.offset_ms, stop=stop, commands=commands)
            if command == "reboot":
                _reboot_system()
                return
            if command == "next":
                index = (index + 1) % len(songs)
                continue
            if command == "previous":
                index = (index - 1) % len(songs)
                continue
            if stop.is_set():
                return
            index += 1
            if index == len(songs):
                if not args.loop:
                    return
                index = 0
            deadline = time.monotonic() + args.gap
            paused_gap_remaining = 0.0
            gap_paused = False
            while args.gap > 0 and not stop.is_set() and (gap_paused or time.monotonic() < deadline):
                remaining = deadline - time.monotonic()
                if not gap_paused and remaining <= 0:
                    break
                try:
                    gap_command = commands.get(timeout=0.1 if gap_paused else min(remaining, 0.1))
                except queue.Empty:
                    continue
                if gap_command == "reboot":
                    _reboot_system()
                    return
                elif gap_command == "pause":
                    if gap_paused:
                        gap_paused = False
                        deadline = time.monotonic() + paused_gap_remaining
                    else:
                        gap_paused = True
                        paused_gap_remaining = max(0.0, deadline - time.monotonic())
                elif gap_command == "next":
                    index = (index + 1) % len(songs)
                    break
                elif gap_command == "previous":
                    index = (index - 1) % len(songs)
                    break

    try:
        _run(out, work)
    finally:
        if remote is not None:
            remote.close()
        player.close()
    return 0


def cmd_test(args) -> int:
    out = _make_output(args)

    def work(stop):
        for n in range(args.cycles):
            for ch in range(out.channels):
                out.set_status(f"Cycle {n + 1}/{args.cycles}: channel {ch}")
                out.set_mask(1 << ch)
                if stop.wait(args.step):
                    return
        out.set_status("All channels ON")
        out.set_mask((1 << out.channels) - 1)
        stop.wait(args.step * 2)

    _run(out, work)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="pilights", description="Sync Christmas lights to MP3 files.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    from .analyze import MODES, AnalyzeParams

    D = AnalyzeParams()
    a = sub.add_parser("analyze", help="make a .seq.json sequence from MP3 files")
    a.add_argument("audio", nargs="+")
    a.add_argument("-o", "--output", help="output path (default: <song>.seq.json)")
    a.add_argument("--mode", choices=MODES, default=D.mode,
                   help="melody: bass, melody and treble channels (default); onset: pulses on note "
                        "attacks; energy: band loudness")
    a.add_argument("--channels", type=int, choices=range(1, 9), default=D.channels)
    a.add_argument("--sensitivity", type=float, default=D.sensitivity,
                   help="melody/onset: higher = more pulses (default %(default)s)")
    a.add_argument("--pulse", type=float, default=D.pulse,
                   help="melody/onset: pulse length as a fraction of the time to the next onset (default %(default)s)")
    a.add_argument("--accent", type=float, default=D.accent,
                   help="melody/onset: fraction of the strongest onsets that light all channels (default %(default)s)")
    a.add_argument("--frame-ms", type=float, default=D.frame_ms, help="energy: time resolution (default %(default)s)")
    a.add_argument("--fmin", type=float, default=40.0, help="lowest band edge in Hz")
    a.add_argument("--fmax", type=float, default=10000.0, help="highest band edge in Hz")
    a.add_argument("--window", type=float, default=D.window_s, help="energy: adaptive threshold window in seconds")
    a.add_argument("--on-z", type=float, default=D.on_z, help="energy: turn ON above this z-score")
    a.add_argument("--off-z", type=float, default=D.off_z, help="energy: turn OFF below this z-score")
    a.add_argument("--min-on-ms", type=float, default=D.min_on_ms, help="minimum ON time (default %(default)s)")
    a.add_argument("--min-off-ms", type=float, default=D.min_off_ms, help="minimum OFF time (default %(default)s)")
    a.add_argument("--silence-db", type=float, default=-45.0, help="all OFF below this level (dBFS)")
    a.add_argument("--rel-floor-db", type=float, default=D.rel_floor_db,
                   help="energy: band must be within this many dB of the loudest band to turn ON")
    a.set_defaults(func=cmd_analyze)

    def add_output_args(p):
        p.add_argument("--pins", type=_pins, default=DEFAULT_PINS,
                       help="comma-separated BCM pins, channel 0 first (default %(default)s)")
        p.add_argument("--active-low", action="store_true", help="for relay boards that switch ON at LOW")
        mode = p.add_mutually_exclusive_group()
        mode.add_argument("--console", action="store_true", help="show channels in the terminal, not GPIO")
        mode.add_argument("--gui", action="store_true", help="show channels in a desktop window, not GPIO")

    p = sub.add_parser("play", help="play MP3 files and run their sequences")
    p.add_argument("audio", nargs="+")
    p.add_argument("-s", "--sequence", help="sequence path (default: <song>.seq.json)")
    p.add_argument("--loop", action="store_true", help="repeat the playlist until stopped")
    p.add_argument("--gap", type=float, default=0.0, help="seconds of pause between songs")
    p.add_argument("--offset-ms", type=float, default=0.0,
                   help="delay lights by this many ms (use to match audio output latency)")
    p.add_argument("--player", choices=("portaudio", "mpg123"), default="portaudio",
                   help="audio player (default %(default)s; best sync)")
    p.add_argument("--device", help="portaudio output device, number or part of the name (see: python -m sounddevice)")
    p.add_argument("--mpg123-args", default="", help='extra mpg123 options, e.g. "-a hw:0,0"')
    p.add_argument("--remote", action="store_true", help="enable an ELEGOO NEC infrared remote")
    p.add_argument("--remote-device", help="Linux input device for the IR receiver (default: detect GPIO IR device)")
    add_output_args(p)
    p.set_defaults(func=cmd_play)

    t = sub.add_parser("test", help="turn on each channel in turn to check wiring")
    t.add_argument("--step", type=float, default=0.5)
    t.add_argument("--cycles", type=int, default=2)
    add_output_args(t)
    t.set_defaults(func=cmd_test)

    args = ap.parse_args(argv)
    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        return args.func(args)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
