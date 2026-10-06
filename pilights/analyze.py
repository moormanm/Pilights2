"""Analyze an MP3 file and make a light sequence.

Modes (``AnalyzeParams.mode``):

melody (default)
    Channel 0 pulses on bass hits. Channel N-1 pulses on high percussion
    (hi-hat, castanets). Channels 1..N-2 follow the melody: each melody note
    lights one channel (low notes = low channels) for the length of the note.
    Sudden loud hits (after a quieter part) flash all channels.

onset
    Each note attack (onset) flashes a pulse. Strong onsets light more
    channels. The pulse goes to the channels whose frequency band changed most.

energy
    Each channel follows the loudness of one frequency band against its
    rolling average.

The melody and onset modes use several passes over the song:
1. Decode the audio and compute two spectrograms: a short one (good timing)
   for onsets and a long one (good frequency resolution) for pitch.
2. Compute the onset strength (spectral flux) for the full song and for the
   bass and treble ranges.
3. Estimate the tempo from the onset strength.
4. Find the onsets. Thresholds come from statistics of the full song.
5. Rank the onsets by strength (in the full song and near each onset).
6. Compare six melody tracks, select one for each section, remove short
   glitches, and move note starts to the nearest onset.
7. Map the melody notes of this song to the channels, so that each channel
   gets a similar share of the notes.
8. Make the channel states and apply the minimum ON and OFF times.
"""

from __future__ import annotations

import math
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from .sequence import MAX_CHANNELS, Sequence, file_sha256

MODES = ("melody", "onset", "energy")

# Increase this number when a change to the analysis makes better sequences.
# `play` analyzes the song again when its sequence has a lower number.
# 1: energy only. 2: melody and onset. 3: loudness-jump accents. 4: sustained-tone suppression.
# 5: selection between stable melody tracks. 6: repeated-phrase evidence. 7: phrase pass removed.
ANALYZER_VERSION = 7
ACCENT_JUMP_DB = 9.0


@dataclass
class AnalyzeParams:
    mode: str = "melody"
    channels: int = 8
    sample_rate: int = 22050
    fmin: float = 40.0
    fmax: float = 10000.0
    min_on_ms: float = 80.0
    min_off_ms: float = 60.0
    silence_db: float = -45.0
    # melody and onset modes
    sensitivity: float = 1.0
    pulse: float = 0.6
    accent: float = 0.03
    melody_lo: int = 55
    melody_hi: int = 90
    bass_hz: float = 200.0
    treble_hz: float = 3000.0
    # energy mode
    frame_ms: float = 25.0
    n_fft: int = 2048
    window_s: float = 2.0
    on_z: float = 0.8
    off_z: float = 0.2
    rel_floor_db: float = 30.0


ONSET_HOP = 256
ONSET_FFT = 1024
PITCH_FFT = 4096
SUB_BANDS = 48


def decode_audio(path: str | Path, sample_rate: int) -> np.ndarray:
    """Decode an audio file to mono float32 samples with ffmpeg."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is not installed (sudo apt install ffmpeg)")
    cmd = [
        "ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
        "-f", "f32le", "-acodec", "pcm_f32le", "-ac", "1", "-ar", str(sample_rate), "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode(errors='replace').strip()}")
    return np.frombuffer(proc.stdout, dtype=np.float32)


def band_edges(channels: int, fmin: float, fmax: float) -> np.ndarray:
    return np.geomspace(fmin, fmax, channels + 1)


def _stft_map(samples: np.ndarray, n_fft: int, hop: int, fn, n_out: int) -> np.ndarray:
    """Apply fn to the STFT magnitude in chunks. Frame i is centered at sample i * hop."""
    pad = n_fft // 2
    x = np.pad(samples.astype(np.float32), (pad, pad))
    n_frames = 1 + max(0, (len(x) - n_fft) // hop)
    window = np.hanning(n_fft).astype(np.float32)
    frames = np.lib.stride_tricks.as_strided(
        x, shape=(n_frames, n_fft), strides=(x.strides[0] * hop, x.strides[0]), writeable=False
    )
    out = np.empty((n_frames, n_out), dtype=np.float32)
    chunk = 256  # Frames per chunk; keeps memory low on a Pi 3.
    for start in range(0, n_frames, chunk):
        out[start:start + chunk] = fn(np.abs(np.fft.rfft(frames[start:start + chunk] * window, axis=1)))
    return out


def _band_masks(freqs: np.ndarray, edges: np.ndarray) -> np.ndarray:
    n = len(edges) - 1
    bin_band = np.searchsorted(edges, freqs, side="right") - 1
    bin_band[(freqs < edges[0]) | (freqs >= edges[-1])] = -1
    # Make sure each band has at least one bin (narrow bass bands).
    for b in range(n):
        if not np.any(bin_band == b):
            center = math.sqrt(edges[b] * edges[b + 1])
            bin_band[int(np.argmin(np.abs(freqs - center)))] = b
    return np.stack([bin_band == b for b in range(n)], axis=1).astype(np.float32)


def band_energies_db(samples: np.ndarray, p: AnalyzeParams, hop: int, n_fft: int | None = None,
                     bands: int | None = None) -> np.ndarray:
    """Return an array (n_frames, bands) of log-spaced band energies in dB."""
    n_fft = n_fft or p.n_fft
    bands = bands or p.channels
    freqs = np.fft.rfftfreq(n_fft, 1.0 / p.sample_rate)
    masks = _band_masks(freqs, band_edges(bands, p.fmin, min(p.fmax, p.sample_rate / 2)))
    out = _stft_map(samples, n_fft, hop, lambda mag: (mag * mag) @ masks, bands)
    return 10.0 * np.log10(out + 1e-10)


def pitch_salience(samples: np.ndarray, sr: int, hop: int, midi_lo: int, midi_hi: int,
                   n_fft: int = PITCH_FFT, harmonics: int = 6, alpha: float = 0.8):
    """Harmonic-sum pitch salience for each semitone in [midi_lo, midi_hi]."""
    freqs = np.fft.rfftfreq(n_fft, 1.0 / sr)
    cands = np.arange(midi_lo, midi_hi + 1)
    W = np.zeros((len(freqs), len(cands)), dtype=np.float32)
    for j, m in enumerate(cands):
        f0 = 440.0 * 2 ** ((m - 69) / 12)
        for h in range(1, harmonics + 1):
            lo, hi = h * f0 * 2 ** (-0.5 / 12), h * f0 * 2 ** (0.5 / 12)
            if hi > sr / 2:
                break
            sel = (freqs >= lo) & (freqs < hi)
            if not sel.any():
                sel[int(np.argmin(np.abs(freqs - h * f0)))] = True
            W[sel, j] += alpha ** (h - 1) / sel.sum()
    k = 31  # Spectral whitening: remove the smooth envelope so that partials stand out.

    def fn(mag):
        mag = np.sqrt(mag)
        c = np.cumsum(np.pad(mag, ((0, 0), (k // 2 + 1, k // 2))), axis=1)
        smooth = (c[:, k:] - c[:, :-k]) / k
        return np.maximum(mag - smooth, 0) @ W

    return _stft_map(samples, n_fft, hop, fn, len(cands)), cands


def frame_rms_db(samples: np.ndarray, hop: int, n_frames: int) -> np.ndarray:
    """RMS level (dBFS) for each frame, centered at frame_index * hop."""
    padded = np.pad(samples, (hop // 2, n_frames * hop))
    blocks = padded[: n_frames * hop].reshape(n_frames, hop)
    rms = np.sqrt(np.mean(blocks.astype(np.float64) ** 2, axis=1))
    return 20.0 * np.log10(rms + 1e-10)


def rolling_mean(x: np.ndarray, width: int) -> np.ndarray:
    """Centered moving average along axis 0 (edges use a shorter window)."""
    width = max(1, int(width))
    n = x.shape[0]
    c = np.cumsum(np.concatenate([np.zeros((1,) + x.shape[1:]), x]), axis=0)
    idx = np.arange(n)
    lo = np.clip(idx - width // 2, 0, n)
    hi = np.clip(idx + width - width // 2, 0, n)
    d = (hi - lo).reshape((-1,) + (1,) * (x.ndim - 1))
    return (c[hi] - c[lo]) / d


def hysteresis(z: np.ndarray, on_z: float, off_z: float) -> np.ndarray:
    state = np.zeros(z.shape, dtype=bool)
    on = False
    for i, v in enumerate(z):
        if on:
            on = v >= off_z
        else:
            on = v > on_z
        state[i] = on
    return state


def enforce_min_durations(x: np.ndarray, min_on: int, min_off: int) -> np.ndarray:
    """Extend ON runs shorter than min_on, then fill OFF gaps shorter than min_off."""
    x = x.copy()
    n = len(x)
    i = 0
    while i < n:
        if x[i]:
            j = i
            while j < n and x[j]:
                j += 1
            if j - i < min_on:
                j = min(n, i + min_on)
                x[i:j] = True
            i = j
        else:
            i += 1
    i = 0
    while i < n:
        if not x[i]:
            j = i
            while j < n and not x[j]:
                j += 1
            if 0 < i and j < n and j - i < min_off:
                x[i:j] = True
            i = j
        else:
            i += 1
    return x


# ---------------------------------------------------------------------------
# Onset helpers


def spectral_flux(E: np.ndarray, lag: int = 2) -> np.ndarray:
    """Positive dB change per band. The reference is the max of the neighbor bands
    (SuperFlux), so vibrato does not look like a new note."""
    E = np.maximum(E, E.max() - 80.0)
    ref = E.copy()
    ref[:, 1:] = np.maximum(ref[:, 1:], E[:, :-1])
    ref[:, :-1] = np.maximum(ref[:, :-1], E[:, 1:])
    F = np.zeros_like(E)
    F[lag:] = np.maximum(E[lag:] - ref[:-lag], 0.0)
    return F


def normalize(env: np.ndarray, gate: np.ndarray) -> np.ndarray:
    ref = np.percentile(env[gate], 99) if gate.any() else 0.0
    return env / ref if ref > 0 else env * 0.0


def estimate_beat(env: np.ndarray, fps: float) -> float:
    """Beat period in seconds from the autocorrelation of the onset strength."""
    e = env - env.mean()
    n = len(e)
    max_lag = min(n - 1, int(fps * 60 / 50))
    if max_lag < 2:
        return 0.5
    f = np.fft.rfft(e, 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[: max_lag + 1]
    lags = np.arange(1, max_lag + 1)
    bpm = 60.0 * fps / lags
    weight = np.exp(-0.5 * (np.log2(bpm / 120.0) / 0.9) ** 2)  # Prefer tempos near 120 BPM.
    score = np.where((bpm >= 50) & (bpm <= 220), ac[1:] * weight, -np.inf)
    i = int(np.argmax(score))
    lag = float(lags[i])
    if 0 < i < len(score) - 1 and np.isfinite(score[i - 1]) and np.isfinite(score[i + 1]):
        a, b, c = score[i - 1], score[i], score[i + 1]
        den = a - 2 * b + c
        if den < 0:
            lag += 0.5 * (a - c) / den
    return lag / fps


def pick_peaks(env: np.ndarray, fps: float, min_gap_s: float, delta: float, floor: float) -> np.ndarray:
    """Frames where env is a local maximum and above its local mean by delta."""
    w = max(1, int(round(0.03 * fps)))
    padded = np.pad(env, w, constant_values=-np.inf)
    local_max = np.lib.stride_tricks.sliding_window_view(padded, 2 * w + 1).max(axis=1)
    local_mean = rolling_mean(env, int(0.5 * fps))
    cand = np.flatnonzero((env >= local_max) & (env >= local_mean + delta) & (env >= floor))
    gap = max(1, int(round(min_gap_s * fps)))
    peaks: list[int] = []
    for n in cand:
        if peaks and n - peaks[-1] < gap:
            if env[n] > env[peaks[-1]]:
                peaks[-1] = n
        else:
            peaks.append(int(n))
    return np.array(peaks, dtype=int)


def add_pulses(state: np.ndarray, onsets, ends, min_on: int, min_off: int) -> None:
    """Turn state ON from each onset to its end. Keep an OFF gap before the next pulse."""
    n = len(state)
    for i, (o, e) in enumerate(zip(onsets, ends)):
        nxt = onsets[i + 1] if i + 1 < len(onsets) else n
        e = max(e, o + min_on)
        if e > nxt - min_off:
            e = nxt if nxt - min_off - o < min_on else nxt - min_off
        state[o:min(e, n)] = True


def pulse_ends(onsets: np.ndarray, n_frames: int, beat: int, frac: float) -> np.ndarray:
    nxt = np.append(onsets[1:], n_frames)
    return onsets + np.round(np.minimum(nxt - onsets, beat) * frac).astype(int)


def strength_rank(times: np.ndarray, s: np.ndarray, window_s: float = 8.0, local: float = 0.5) -> np.ndarray:
    """0..1: rank in the full song mixed with rank among onsets within window_s (weight local)."""
    if len(s) < 2:
        return np.ones(len(s))
    glob = np.argsort(np.argsort(s)) / (len(s) - 1)
    loc = np.array([(s[np.abs(times - t) <= window_s] < v).mean() for t, v in zip(times, s)])
    return (1.0 - local) * glob + local * loc


# ---------------------------------------------------------------------------
# Melody helpers


def median_filter(x: np.ndarray, width: int) -> np.ndarray:
    h = width // 2
    padded = np.pad(x, h, mode="edge")
    return np.median(np.lib.stride_tricks.sliding_window_view(padded, 2 * h + 1), axis=1).astype(x.dtype)


def runs(x: np.ndarray):
    """Return (starts, ends, values) of runs of equal values."""
    change = np.flatnonzero(np.diff(x)) + 1
    starts = np.r_[0, change]
    ends = np.r_[change, len(x)]
    return starts, ends, x[starts]


def absorb_short_runs(x: np.ndarray, min_len: int) -> np.ndarray:
    """Replace runs shorter than min_len with the value of the run before them."""
    x = x.copy()
    for _ in range(3):
        starts, ends, vals = runs(x)
        short = np.flatnonzero(ends - starts < min_len)
        if not len(short):
            break
        for i in short:
            if i > 0:
                x[starts[i]:ends[i]] = x[starts[i] - 1]
            elif len(starts) > 1:
                x[starts[i]:ends[i]] = vals[1]
    return x


def snap_boundaries(x: np.ndarray, onsets: np.ndarray, max_dist: int) -> np.ndarray:
    """Move each change of value in x to the nearest onset within max_dist frames."""
    if not len(onsets):
        return x
    y = x.copy()
    starts, _, _ = runs(x)
    for b in starts[1:]:
        j = int(np.searchsorted(onsets, b))
        cand = [onsets[k] for k in (j - 1, j) if 0 <= k < len(onsets)]
        o = min(cand, key=lambda c: abs(c - b))
        if 0 < abs(o - b) <= max_dist:
            if o < b:
                y[o:b] = x[b]
            else:
                y[b:o] = x[b - 1]
    return y


def pitch_to_channel(pitches: np.ndarray, n: int) -> dict:
    """Map each used pitch to one of n channels, in pitch order, with similar use per channel."""
    vals, counts = np.unique(pitches, return_counts=True)
    if not len(vals):
        return {}
    if len(vals) <= n:
        pos = np.arange(len(vals)) * (n - 1) / max(1, len(vals) - 1)
        return {int(v): int(round(c)) for v, c in zip(vals, pos)}
    mid = (np.cumsum(counts) - counts / 2) / counts.sum()
    return {int(v): int(min(n - 1, m * n)) for v, m in zip(vals, mid)}


def stable_pitch_track(scores: np.ndarray, penalty: float, onsets: np.ndarray, fps: float) -> np.ndarray:
    """Find a pitch path. A change costs less near a detected note start."""
    if penalty == 0:
        return np.argmax(scores, axis=1)
    step = max(1, int(round(0.035 * fps)))
    blocks = np.arange(0, len(scores), step)
    evidence = np.add.reduceat(scores, blocks) / np.minimum(step, len(scores) - blocks)[:, None]
    evidence /= np.maximum(evidence.max(axis=1, keepdims=True), 1e-9)
    changes = np.full(len(blocks), penalty)
    for onset in onsets:
        nearest = int(np.clip(round(onset / step), 0, len(blocks) - 1))
        changes[max(0, nearest - 1):nearest + 2] = penalty * 0.25
    k = scores.shape[1]
    distance = np.abs(np.arange(k)[:, None] - np.arange(k)[None, :])
    cost = (distance > 0) * (1.0 + np.minimum(distance, 12) / 24)
    back = np.zeros((len(blocks), k), dtype=np.int16)
    value = evidence[0].copy()
    for i in range(1, len(blocks)):
        previous = value[:, None] - changes[i] * cost
        back[i] = np.argmax(previous, axis=0)
        value = evidence[i] + previous[back[i], np.arange(k)]
        value -= value.max()
    path = np.empty(len(blocks), dtype=int)
    path[-1] = np.argmax(value)
    for i in range(len(blocks) - 1, 0, -1):
        path[i - 1] = back[i, path[i]]
    return np.repeat(path, step)[:len(scores)]


def select_melody_track(S: np.ndarray, cands: np.ndarray, voiced: np.ndarray,
                        onsets: np.ndarray, fps: float, info: dict) -> np.ndarray:
    """Compare melody tracks in 8-second sections, with audio agreement as the main score."""
    background = rolling_mean(S, max(1, int(2 * fps)))
    reference = np.maximum(S - 0.7 * background, 0)
    reference /= np.maximum(reference.max(axis=1, keepdims=True), 1e-9)
    raw = S / np.maximum(S.max(axis=1, keepdims=True), 1e-9)
    support = 0.75 * reference + 0.25 * raw
    near_onset = np.zeros(len(S), dtype=bool)
    evidence_weight = np.full(len(S), 0.2)
    radius = max(1, int(0.08 * fps))
    for onset in onsets:
        near_onset[max(0, onset - radius):onset + radius + 1] = True
        evidence_weight[onset:min(len(S), onset + max(1, int(0.3 * fps)))] = 1.0
    candidates = []
    settings = [(suppression, penalty) for suppression in (0.0, 0.7) for penalty in (0.0, 0.3, 0.8)]
    for suppression, penalty in settings:
        scores = np.maximum(S - suppression * background, 0)
        path = stable_pitch_track(scores, penalty, onsets, fps)
        note = np.where(voiced, cands[path], 0).astype(int)
        note = absorb_short_runs(median_filter(note, 9), max(2, int(0.07 * fps)))
        candidates.append(note)
    chosen = np.zeros(len(S), dtype=int)
    selections = np.zeros(len(settings), dtype=int)
    section = max(1, int(8 * fps))
    for start in range(0, len(S), section):
        end = min(start + section, len(S))
        active = voiced[start:end]
        if not active.any():
            continue
        quality = []
        for note in candidates:
            part = note[start:end]
            idx = np.clip(part - cands[0], 0, len(cands) - 1)
            agreement = support[np.arange(start, end), idx]
            agreement = float(np.average(np.where(part[active] > 0, agreement[active], 0),
                                         weights=evidence_weight[start:end][active]))
            a, b, v = runs(part)
            short = float(np.sum((b - a)[(v > 0) & (b - a < 0.15 * fps)])) / max(1, active.sum())
            transitions = a[1:][(v[1:] > 0) & (v[:-1] > 0)]
            unaligned = np.count_nonzero(~near_onset[start + transitions]) / max(1, (end - start) / fps)
            # Channel balance is a small preference, not a reason to replace a supported pitch.
            counts = np.unique(part[part > 0], return_counts=True)[1]
            dominance = counts.max() / counts.sum() if len(counts) else 0.0
            quality.append(agreement - 0.25 * short - 0.04 * unaligned - 0.03 * dominance)
        best = int(np.argmax(quality))
        chosen[start:end] = candidates[best][start:end]
        selections[best] += 1
    info["melody_candidates"] = [
        {"suppression": s, "change_penalty": p, "selected_sections": int(n)}
        for (s, p), n in zip(settings, selections)
    ]
    return chosen


def melody_notes(samples: np.ndarray, p: AnalyzeParams, hop: int, fps: float, gate: np.ndarray,
                 onsets: np.ndarray, info: dict) -> tuple[np.ndarray, np.ndarray]:
    """Return (midi note per frame or 0, re-attack flag per frame)."""
    S, cands = pitch_salience(samples, p.sample_rate, hop, p.melody_lo, p.melody_hi)
    S = S[: len(gate)]
    peak = S.max(axis=1)
    conf = peak / (np.median(S, axis=1) + 1e-9)
    thr = max(2.5, np.percentile(conf[gate], 30)) if gate.any() else np.inf
    voiced = gate & (conf > thr) & (peak > 0.05 * np.percentile(peak[gate], 99))
    note = select_melody_track(S, cands, voiced, onsets, fps, info)
    note = snap_boundaries(note, onsets, int(0.12 * fps))
    note = absorb_short_runs(note, max(2, int(0.05 * fps)))

    # Re-attack: an onset inside a note where the salience of that note rises.
    reattack = np.zeros(len(note), dtype=bool)
    idx = {int(m): i for i, m in enumerate(cands)}
    for o in onsets:
        m = int(note[o])
        if m and o >= 2 and o + 3 < len(note) and note[o - 1] == m:
            j = idx[m]
            if S[o + 3, j] > 1.3 * S[o - 2, j]:
                reattack[o] = True
    info["voiced_pct"] = round(100 * float(np.mean(note[gate] > 0)), 1) if gate.any() else 0.0
    return note, reattack


# ---------------------------------------------------------------------------
# Analysis modes


def _states_to_events(states: np.ndarray, hop: int, sr: int, n_samples: int) -> tuple[list, int]:
    weights = (1 << np.arange(states.shape[1])).astype(np.int64)
    masks = states.astype(np.int64) @ weights
    change = np.flatnonzero(np.diff(masks)) + 1
    idx = np.r_[0, change]
    events = [(int(round(i * hop * 1000.0 / sr)), int(masks[i])) for i in idx]
    duration_ms = int(round(n_samples * 1000.0 / sr))
    if events[-1][1] != 0:
        events.append((duration_ms, 0))
    return events, duration_ms


def _energy_states(samples: np.ndarray, p: AnalyzeParams) -> tuple[np.ndarray, int]:
    hop = max(1, int(round(p.sample_rate * p.frame_ms / 1000.0)))
    fps = p.sample_rate / hop
    energy = band_energies_db(samples, p, hop)
    n_frames = energy.shape[0]

    win = max(1, int(round(p.window_s * fps)))
    mean = rolling_mean(energy.astype(np.float64), win)
    var = rolling_mean(energy.astype(np.float64) ** 2, win) - mean ** 2
    std = np.maximum(np.sqrt(np.maximum(var, 0.0)), 1.5)  # dB floor: steady tones do not flicker
    z = (energy - mean) / std

    loud = frame_rms_db(samples, hop, n_frames) > p.silence_db
    strong = energy > energy.max(axis=1, keepdims=True) - p.rel_floor_db
    min_on = max(1, int(math.ceil(p.min_on_ms / 1000.0 * fps)))
    min_off = max(1, int(math.ceil(p.min_off_ms / 1000.0 * fps)))

    states = np.zeros((n_frames, p.channels), dtype=bool)
    for ch in range(p.channels):
        s = hysteresis(z[:, ch], p.on_z, p.off_z) & loud & strong[:, ch]
        states[:, ch] = enforce_min_durations(s, min_on, min_off)
    return states, hop


def _onset_states(samples: np.ndarray, p: AnalyzeParams, info: dict) -> tuple[np.ndarray, int]:
    hop = ONSET_HOP
    fps = p.sample_rate / hop
    N = p.channels
    E = band_energies_db(samples, p, hop, n_fft=ONSET_FFT, bands=SUB_BANDS)
    n_frames = len(E)
    level = frame_rms_db(samples, hop, n_frames)
    gate = level > p.silence_db
    F = spectral_flux(E)
    F[~gate] = 0.0
    F[n_frames - ONSET_FFT // (2 * hop):] = 0.0  # The end of the file is a click, not an onset.
    env = normalize(F.mean(axis=1), gate)

    beat_s = estimate_beat(env, fps)
    beat = max(1, int(round(beat_s * fps)))
    sens = max(p.sensitivity, 1e-3)
    onsets = pick_peaks(env, fps, beat_s / 4 * 0.75, 0.06 / sens, 0.04 / sens)
    min_on = max(1, int(math.ceil(p.min_on_ms / 1000.0 * fps)))
    min_off = max(1, int(math.ceil(p.min_off_ms / 1000.0 * fps)))
    info.update(tempo_bpm=round(60.0 / beat_s, 1), onsets=int(len(onsets)))

    states = np.zeros((n_frames, N), dtype=bool)
    if not len(onsets):
        return states, hop
    strength = np.array([env[max(0, o - 1):o + 2].max() for o in onsets])
    rank = strength_rank(onsets / fps, strength)
    ends = pulse_ends(onsets, n_frames, beat, p.pulse)
    accent = np.zeros(len(onsets), dtype=bool)
    if p.accent > 0:
        # Accents must be strong onsets in the louder half of the song, and a sudden
        # jump in loudness (a hit after a quieter part), not only a loud note in a loud part.
        lvl = np.array([level[o:o + int(0.15 * fps)].max() for o in onsets])
        before = np.array([np.median(level[max(0, o - int(fps)):max(1, o - 2)]) for o in onsets])
        loud = (lvl >= np.percentile(level[gate], 50)) & (lvl - before >= ACCENT_JUMP_DB) \
            & (strength >= np.quantile(strength, 0.7))
        cand = np.flatnonzero(loud)
        picked: list[int] = []
        for i in cand[np.argsort(-strength[cand])]:
            if len(picked) >= math.ceil(p.accent * len(onsets)):
                break
            if all(abs(onsets[i] - onsets[j]) >= 4 * beat for j in picked):
                picked.append(i)
        accent[picked] = True
    info["accents"] = int(accent.sum())

    if p.mode == "onset" or N < 4:
        # Channel groups of sub-bands; pulse the channels whose band changed most.
        groups = np.array_split(np.arange(SUB_BANDS), N)
        chf = np.stack([F[:, g].mean(axis=1) for g in groups], axis=1)
        cf = np.array([chf[max(0, o - 1):o + 3].max(axis=0) for o in onsets])
        cf = cf / (np.median(cf, axis=0) + 1e-9)
        k = np.clip(np.ceil(N * rank ** 1.6 * sens), 1, N).astype(int)
        k[accent] = N
        for ch in range(N):
            sel = np.array([ch in np.argsort(-cf[i])[: k[i]] for i in range(len(onsets))])
            add_pulses(states[:, ch], onsets[sel], ends[sel], min_on, min_off)
        return states, hop

    # melody mode: CH0 bass, CH1..N-2 melody, CH N-1 treble, accents on all.
    centers = np.sqrt(band_edges(SUB_BANDS, p.fmin, min(p.fmax, p.sample_rate / 2))[:-1]
                      * band_edges(SUB_BANDS, p.fmin, min(p.fmax, p.sample_rate / 2))[1:])
    for ch, sel in ((0, centers < p.bass_hz), (N - 1, centers > p.treble_hz)):
        if not sel.any():
            continue
        band_env = normalize(F[:, sel].mean(axis=1), gate)
        peaks = pick_peaks(band_env, fps, max(beat_s / 4 * 0.75, (min_on + min_off) / fps), 0.12 / sens, 0.15 / sens)
        if len(peaks) > 1:
            keep = strength_rank(peaks / fps, band_env[peaks], local=0.85) >= 1.0 - min(1.0, 0.6 * sens)
            peaks = peaks[keep]
        add_pulses(states[:, ch], peaks, pulse_ends(peaks, n_frames, beat, p.pulse), min_on, min_off)
        info["bass_hits" if ch == 0 else "treble_hits"] = int(len(peaks))

    note, reattack = melody_notes(samples, p, hop, fps, gate, onsets, info)
    mel = N - 2
    cmap = pitch_to_channel(note[note > 0], mel)
    mch = np.array([cmap.get(int(m), -1) if m else -1 for m in note])
    starts, ends_, vals = runs(note)
    for s, e, m in zip(starts, ends_, vals):
        if not m:
            continue
        c = cmap[int(m)]
        cuts = [s] + [int(i) for i in np.flatnonzero(reattack[s:e]) + s if i - s >= min_on] + [e]
        for a, b in zip(cuts, cuts[1:]):
            # Leave an OFF gap when the same channel starts again at b.
            b2 = b - min_off if b < len(mch) and mch[b] == c else b
            states[a:max(b2, a + min_on), 1 + c] = True
    info["melody_notes"] = {str(k): v + 1 for k, v in sorted(cmap.items())}

    # Where no melody is found, pulse one melody channel on each onset: brighter sound = higher channel.
    soon = int(0.12 * fps)
    free = np.array([not note[o:o + soon].any() and not states[o:o + soon, 1:N - 1].any() for o in onsets],
                    dtype=bool)
    if free.any():
        fo = onsets[free]
        cent = np.array([(F[o:o + 3].sum(axis=0) * np.log(centers)).sum() / (F[o:o + 3].sum() + 1e-9) for o in fo])
        edges = np.quantile(cent, np.linspace(0, 1, mel + 1)[1:-1])
        chans = np.searchsorted(edges, cent)
        fe = pulse_ends(fo, n_frames, beat, p.pulse)
        for c in range(mel):
            sel = chans == c
            add_pulses(states[:, 1 + c], fo[sel], fe[sel], min_on, min_off)
        info["melody_fallback_onsets"] = int(free.sum())

    acc_on = onsets[accent]
    acc_state = np.zeros(n_frames, dtype=bool)
    add_pulses(acc_state, acc_on, pulse_ends(acc_on, n_frames, beat, p.pulse), min_on, min_off)
    states[acc_state] = True
    return states, hop


def analyze_samples(samples: np.ndarray, p: AnalyzeParams, info: dict | None = None) -> tuple[list, int, np.ndarray]:
    """Return (events, duration_ms, states[n_frames, channels]). Fills info with statistics."""
    if not 1 <= p.channels <= MAX_CHANNELS:
        raise ValueError(f"channels must be 1..{MAX_CHANNELS}, got {p.channels}")
    if p.mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    info = {} if info is None else info
    if p.mode == "energy":
        states, hop = _energy_states(samples, p)
    else:
        states, hop = _onset_states(samples, p, info)
    events, duration_ms = _states_to_events(states, hop, p.sample_rate, len(samples))
    return events, duration_ms, states


def options_of(p: AnalyzeParams) -> dict:
    """Parameters that are different from the defaults."""
    default = asdict(AnalyzeParams())
    return {k: v for k, v in asdict(p).items() if default[k] != v}


def params_from_options(options: dict, channels: int | None = None) -> AnalyzeParams:
    """Defaults plus the known options. Unknown options (from other versions) are ignored."""
    p = AnalyzeParams()
    for k, v in options.items():
        if hasattr(p, k):
            setattr(p, k, v)
    if channels is not None:
        p.channels = channels
    return p


def analyze_file(path: str | Path, p: AnalyzeParams) -> tuple[Sequence, np.ndarray]:
    samples = decode_audio(path, p.sample_rate)
    if samples.size == 0:
        raise RuntimeError(f"{path}: no audio decoded")
    info: dict = {}
    events, duration_ms, states = analyze_samples(samples, p, info)
    meta = {
        "source": Path(path).name,
        "params": asdict(p),
        "options": options_of(p),
        "analysis": {k: (v.item() if isinstance(v, np.generic) else v) for k, v in info.items()},
    }
    if p.mode == "energy":
        meta["bands_hz"] = [round(float(f), 1) for f in band_edges(p.channels, p.fmin, min(p.fmax, p.sample_rate / 2))]
    seq = Sequence(
        channels=p.channels,
        duration_ms=duration_ms,
        events=events,
        audio_sha256=file_sha256(path),
        meta=meta,
        analyzer_version=ANALYZER_VERSION,
    )
    return seq, states
