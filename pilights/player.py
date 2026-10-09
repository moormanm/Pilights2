"""Play MP3 files and switch the channels from a sequence.

PortAudioPlayer (default) gets the speaker time of each buffer from PortAudio.
Mpg123 is the older player.

mpg123 runs in remote mode (-R). It sends "@F" lines with the current decode
position for each MP3 frame (about every 26 ms). The light clock subtracts the
audio buffer lead from it, so the lights match the sound and do not drift.
"""

from __future__ import annotations

import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from .outputs import Output
from .sequence import Sequence


class Mpg123:
    def __init__(self, exe: str = "mpg123", extra_args=()):
        if shutil.which(exe) is None:
            raise RuntimeError(f"{exe} is not installed (sudo apt install mpg123)")
        self.proc = subprocess.Popen(
            [exe, "-R", *extra_args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._lock = threading.Lock()
        self._pos = None
        self._at = 0.0
        self._first = None
        self._lead = None
        self._paused_position = None
        self.error = None
        self.started = threading.Event()
        self.finished = threading.Event()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _send(self, cmd: str) -> None:
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()

    def _read(self) -> None:
        for line in self.proc.stdout:
            parts = line.split()
            if not parts:
                continue
            tag = parts[0]
            if tag == "@F" and len(parts) >= 4:
                with self._lock:
                    self._pos = float(parts[3])
                    self._at = time.monotonic()
                    if self._first is None:
                        self._first = self._at
                    # @F is the decode position. mpg123 decodes ahead to fill the audio
                    # buffer, so the sound is later. Sound starts at the first frame,
                    # so the lead is the decode position minus the time since then. Late reads
                    # make it look smaller, so follow increases at once and decreases slowly.
                    lead = self._pos - (self._at - self._first)
                    self._lead = lead if self._lead is None or lead > self._lead else self._lead + 0.02 * (lead - self._lead)
                self.started.set()
            elif tag == "@P" and len(parts) >= 2 and parts[1] == "0" and self.started.is_set():
                self.finished.set()
            elif tag == "@E":
                self.error = line[3:].strip()
                self.started.set()
                self.finished.set()
        self.started.set()
        self.finished.set()

    def load(self, path: str | Path) -> None:
        with self._lock:
            self._pos = None
            self._first = None
            self._lead = None
            self._paused_position = None
        self.error = None
        self.started.clear()
        self.finished.clear()
        self._send(f"LOAD {Path(path).resolve()}")

    def position(self) -> float | None:
        """Position of the sound in seconds, interpolated between @F reports."""
        with self._lock:
            if self._pos is None:
                return None
            if self._paused_position is not None:
                return self._paused_position
            # Limit interpolation so a stall does not run the lights ahead.
            return max(0.0, self._pos + min(time.monotonic() - self._at, 0.1) - self._lead)

    def stop(self) -> None:
        self._send("STOP")

    def pause(self) -> None:
        position = self.position()
        with self._lock:
            self._paused_position = position
        self._send("PAUSE")

    def resume(self) -> None:
        self._send("PAUSE")
        with self._lock:
            self._paused_position = None

    def close(self) -> None:
        try:
            self._send("QUIT")
            self.proc.wait(timeout=2)
        except Exception:
            self.proc.kill()


class PortAudioPlayer:
    """Decode with ffmpeg and play with PortAudio (sounddevice).

    For each audio buffer, PortAudio tells the time when its first sample gets
    to the DAC. The position is calculated from that time, so it includes all
    output latency (buffers, driver, USB).
    """

    BLOCK = 2048  # Frames per ffmpeg read.

    def __init__(self, device=None):
        import sounddevice as sd
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg is not installed (sudo apt install ffmpeg)")
        self._sd = sd
        self.device = device
        info = sd.query_devices(device, kind="output")
        self.rate = int(info["default_samplerate"])
        self.channels = min(2, int(info["max_output_channels"]))
        self._lock = threading.Lock()
        self._stream = None
        self._ffmpeg = None
        self._pos = None
        self._paused_position = None
        self.error = None
        self.started = threading.Event()
        self.finished = threading.Event()

    def _feed(self, proc, q) -> None:
        size = self.BLOCK * self.channels * 4
        try:
            while data := proc.stdout.read(size):
                q.put(np.frombuffer(data, dtype=np.float32).reshape(-1, self.channels))
        except (ValueError, OSError):
            return  # The song was stopped.
        q.put(None)
        proc.wait()
        if proc.returncode != 0 and proc is self._ffmpeg:
            self.error = f"ffmpeg failed: {proc.stderr.read().decode(errors='replace').strip()}"
            self.started.set()
            self.finished.set()

    def load(self, path: str | Path) -> None:
        self._close_stream()
        with self._lock:
            self._pos = None
            self._paused_position = None
            self.error = None
        self.started.clear()
        self.finished.clear()
        cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-f", "f32le", "-acodec", "pcm_f32le",
               "-ac", str(self.channels), "-ar", str(self.rate), "-"]
        self._ffmpeg = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        q: queue.Queue = queue.Queue(maxsize=64)
        threading.Thread(target=self._feed, args=(self._ffmpeg, q), daemon=True).start()
        state = {"buf": np.zeros((0, self.channels), np.float32), "frames": 0, "eof": False}

        def callback(outdata, frames, t, status):
            with self._lock:
                paused = self._paused_position is not None
            if paused:
                outdata.fill(0)
                return
            buf = state["buf"]
            while len(buf) < frames and not state["eof"]:
                try:
                    block = q.get_nowait()
                except queue.Empty:
                    break  # Underrun: play silence for the rest of this buffer.
                if block is None:
                    state["eof"] = True
                else:
                    buf = np.concatenate([buf, block])
            n = min(frames, len(buf))
            outdata[:n] = buf[:n]
            outdata[n:] = 0
            state["buf"] = buf[n:]
            dac = t.outputBufferDacTime or (t.currentTime + self._stream.latency)
            with self._lock:
                self._pos = (state["frames"], dac)
            state["frames"] += n
            if n:
                self.started.set()
            if state["eof"] and not len(state["buf"]):
                raise self._sd.CallbackStop

        self._stream = self._sd.OutputStream(
            samplerate=self.rate, channels=self.channels, dtype="float32", device=self.device,
            latency="high", callback=callback, finished_callback=self.finished.set)
        self._stream.start()

    def position(self) -> float | None:
        """Position of the sound at the speaker, in seconds."""
        with self._lock:
            if self._pos is None or self._stream is None:
                return None
            if self._paused_position is not None:
                return self._paused_position
            frames, dac = self._pos
            return max(0.0, frames / self.rate + min(self._stream.time - dac, 0.2))

    def pause(self) -> None:
        if self._stream is not None and self._paused_position is None:
            with self._lock:
                if self._pos is not None:
                    frames, dac = self._pos
                    self._paused_position = max(0.0, frames / self.rate + min(self._stream.time - dac, 0.2))

    def resume(self) -> None:
        if self._stream is not None and self._paused_position is not None:
            with self._lock:
                self._paused_position = None

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.abort()
        self.finished.set()

    def _close_stream(self) -> None:
        if self._stream is not None:
            self._stream.abort()
            self._stream.close()
            self._stream = None
        if self._ffmpeg is not None:
            proc, self._ffmpeg = self._ffmpeg, None
            proc.terminate()
            proc.stdout.close()

    def close(self) -> None:
        self._close_stream()


def play_song(player: Mpg123, audio: str | Path, seq: Sequence, out: Output, offset_ms: float = 0.0,
              stop: threading.Event | None = None, commands: queue.Queue | None = None) -> str:
    """Play one song and run its sequence. Return a remote playlist command if one was received."""
    events = seq.events
    out.set_mask(0)
    player.load(audio)
    if not player.started.wait(15):
        raise RuntimeError(f"{audio}: playback did not start")
    if player.error:
        raise RuntimeError(f"{audio}: {player.error}")
    idx = 0
    paused = False
    current_mask = 0
    name = Path(audio).name
    while not player.finished.is_set():
        if stop is not None and stop.is_set():
            player.stop()
            break
        if commands is not None:
            try:
                command = commands.get_nowait()
            except queue.Empty:
                command = None
            if command == "pause":
                if paused:
                    player.resume()
                    paused = False
                else:
                    player.pause()
                    paused = True
                out.set_mask(current_mask if not paused else 0)
            elif command in ("next", "previous"):
                player.stop()
                out.set_mask(0)
                return command
            elif command == "reboot":
                player.stop()
                out.set_mask(0)
                return command
        if paused:
            time.sleep(0.02)
            continue
        pos = player.position()
        if pos is None:
            time.sleep(0.005)
            continue
        out.set_status(f"{name}   {pos:6.1f} / {seq.duration_ms / 1000:.1f} s")
        t_ms = pos * 1000.0 - offset_ms
        mask = None
        while idx < len(events) and events[idx][0] <= t_ms:
            mask = events[idx][1]
            idx += 1
        if mask is not None:
            current_mask = mask
            out.set_mask(current_mask)
        wait = (events[idx][0] - t_ms) / 1000.0 if idx < len(events) else 0.05
        time.sleep(min(max(wait, 0.001), 0.005))
    if player.error:
        raise RuntimeError(f"{audio}: {player.error}")
    out.set_mask(0)
    return "finished"
