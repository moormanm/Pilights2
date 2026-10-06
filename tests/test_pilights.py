import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import numpy as np

from pilights.analyze import ANALYZER_VERSION, AnalyzeParams, analyze_samples, enforce_min_durations
from pilights.sequence import Sequence

SR = 22050


def tone(freq, seconds, sr):
    t = np.arange(int(seconds * sr)) / sr
    return 0.5 * np.sin(2 * np.pi * freq * t).astype(np.float32)


def note(midi, seconds, sr=SR):
    """A plucked note with harmonics."""
    f = 440.0 * 2 ** ((midi - 69) / 12)
    t = np.arange(int(seconds * sr)) / sr
    x = sum(0.6 ** h * np.sin(2 * np.pi * f * (h + 1) * t) for h in range(5))
    return (0.3 * x * np.exp(-t * 1.5)).astype(np.float32)


class AnalyzeTest(unittest.TestCase):
    def test_stable_track_ignores_weak_changes_but_keeps_strong_changes(self):
        from pilights.analyze import stable_pitch_track

        scores = np.ones((400, 2))
        scores[:, 1] = 0.98
        scores[10::20, 1] = 1.02
        path = stable_pitch_track(scores, 0.8, np.array([], dtype=int), 100)
        self.assertTrue(np.all(path == 0))
        scores[200:, 0] = 0.1
        scores[200:, 1] = 1.0
        path = stable_pitch_track(scores, 0.8, np.array([200]), 100)
        self.assertTrue(np.all(path[:190] == 0))
        self.assertTrue(np.all(path[210:] == 1))

    def test_candidate_selection_keeps_a_repeated_pattern(self):
        from pilights.analyze import select_melody_track

        fps = 100
        expected = np.tile(np.repeat([0, 1, 2, 1], 50), 12)
        scores = np.full((len(expected), 3), 0.1)
        scores[np.arange(len(expected)), expected] = 1.0
        for start in range(20, len(expected), 50):
            scores[start:start + 12, (expected[start] + 1) % 3] = 1.03
        cands = np.array([60, 61, 62])
        info = {}
        result = select_melody_track(scores, cands, np.ones(len(scores), bool),
                                     np.arange(0, len(scores), 50), fps, info)
        self.assertGreater(np.mean(result == cands[expected]), 0.95)
        self.assertTrue(any(c["change_penalty"] > 0 and c["selected_sections"] > 0
                            for c in info["melody_candidates"]))
        self.assertEqual(sum(c["selected_sections"] for c in info["melody_candidates"]), 3)

    def test_energy_bass_bursts_drive_low_channels_only(self):
        p = AnalyzeParams(mode="energy")
        sr = p.sample_rate
        silence = np.zeros(int(0.5 * sr), dtype=np.float32)
        parts = []
        for _ in range(4):
            parts += [tone(60, 0.5, sr), silence]
        _, duration_ms, states = analyze_samples(np.concatenate(parts), p)
        self.assertAlmostEqual(duration_ms, 4000, delta=5)
        self.assertGreater(states[:, 0].mean(), 0.3)
        self.assertTrue(np.all(states[:, 4:] == 0))

    def test_events_start_at_zero_and_end_off(self):
        for mode in ("melody", "onset", "energy"):
            p = AnalyzeParams(mode=mode)
            events, duration_ms, _ = analyze_samples(tone(3000, 2.0, p.sample_rate), p)
            self.assertEqual(events[0][0], 0, mode)
            self.assertEqual(events[-1][1], 0, mode)
            self.assertTrue(all(a[0] < b[0] for a, b in zip(events, events[1:])), mode)

    def test_melody_rising_notes_light_rising_channels(self):
        p = AnalyzeParams(accent=0)
        midis = [60, 62, 64, 65, 67, 69, 71, 72]
        x = np.concatenate([np.zeros(SR // 2, np.float32)] + [note(m, 0.5) for m in midis])
        info = {}
        _, _, states = analyze_samples(x, p, info)
        fps = SR / 256
        held = []
        for i in range(len(midis)):
            mid = int((0.5 + 0.5 * i + 0.25) * fps)
            on = np.flatnonzero(states[mid, 1:7])
            self.assertEqual(len(on), 1, f"note {i}: {states[mid].astype(int)}")
            held.append(int(on[0]))
        self.assertEqual(held, sorted(held))
        self.assertEqual(held[0], 0)
        self.assertEqual(held[-1], 5)
        # Each note turns its channel ON at the note start, also when the channel is the same as before.
        for i, ch in enumerate(held):
            rises = np.flatnonzero(np.diff(states[:, 1 + ch].astype(int)) == 1) + 1
            self.assertLess(np.min(np.abs(rises / fps - (0.5 + 0.5 * i))), 0.03, f"note {i}")

    def test_onsets_are_on_time(self):
        p = AnalyzeParams(mode="onset", accent=0)
        x = np.concatenate([np.zeros(SR // 2, np.float32)] + [note(60 + i % 3, 0.25) for i in range(8)])
        events, _, states = analyze_samples(x, p)
        fps = SR / 256
        starts = np.flatnonzero(np.diff(states.any(axis=1).astype(int)) == 1) + 1
        expected = (0.5 + 0.25 * np.arange(8)) * fps
        self.assertEqual(len(starts), 8)
        self.assertLess(np.max(np.abs(starts - expected)) / fps, 0.025)

    def test_melody_changes_over_a_louder_sustained_tone(self):
        from pilights.analyze import melody_notes

        midis = [64, 67, 69, 67, 64, 62, 64, 67]
        backing_hz = 440 * 2 ** ((56 - 69) / 12)
        x = tone(backing_hz, len(midis), SR) * 1.2
        x += np.concatenate([note(m, 1.0) for m in midis])
        fps = SR / 256
        gate = np.ones(1 + len(x) // 256, dtype=bool)
        onsets = np.round(np.arange(len(midis)) * fps).astype(int)
        notes, _ = melody_notes(x, AnalyzeParams(), 256, fps, gate, onsets, {})
        for i, midi in enumerate(midis):
            detected = notes[int((i + 0.15) * fps):int((i + 0.4) * fps)]
            self.assertGreater(np.mean(detected == midi), 0.5, f"note {i}")

    def test_sustained_melody_note_is_kept(self):
        from pilights.analyze import melody_notes

        x = tone(440, 5.0, SR)
        fps = SR / 256
        gate = np.ones(1 + len(x) // 256, dtype=bool)
        notes, _ = melody_notes(x, AnalyzeParams(), 256, fps, gate, np.array([0]), {})
        self.assertGreater(np.mean(notes[int(fps):int(4 * fps)] == 69), 0.95)

    def test_accents_only_on_jump_in_loudness(self):
        # Loud steady hits, a quiet part, then one big hit: only the big hit is an accent.
        rng = np.random.default_rng(0)
        t = np.arange(int(0.1 * SR)) / SR
        hit = (rng.normal(0, 0.3, len(t)) * np.exp(-t * 30)).astype(np.float32)
        bed = tone(220, 8.0, SR) * 0.4
        for k in range(32):
            bed[int(k * 0.25 * SR):int(k * 0.25 * SR) + len(t)] += hit
        quiet = tone(220, 2.0, SR) * 0.01
        end = tone(220, 2.0, SR) * 0.4
        end[:len(t)] += 3 * hit
        info = {}
        _, _, states = analyze_samples(np.concatenate([bed, quiet, end]), AnalyzeParams(), info)
        fps = SR / 256
        starts = (np.flatnonzero(np.diff(states.all(axis=1).astype(int)) == 1) + 1) / fps
        self.assertEqual(len(starts), 1, starts)
        self.assertAlmostEqual(starts[0], 10.0, delta=0.05)

    def test_min_durations(self):
        x = np.array([0, 1, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0], dtype=bool)
        y = enforce_min_durations(x, min_on=3, min_off=2)
        self.assertEqual(y.astype(int).tolist(), [0, 1, 1, 1, 0, 0, 1, 1, 1, 0, 0, 0])
        y = enforce_min_durations(x, min_on=3, min_off=3)
        self.assertEqual(y.astype(int).tolist(), [0, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0])


class AudioInputsTest(unittest.TestCase):
    def test_sequence_files_are_skipped(self):
        from contextlib import redirect_stderr
        from io import StringIO
        from pilights.cli import _audio_inputs

        message = StringIO()
        with redirect_stderr(message):
            audio = _audio_inputs(["Hallowed Be Thy Name.mp3", "Hallowed Be Thy Name.seq.json", "next.wav"])
        self.assertEqual(audio, ["Hallowed Be Thy Name.mp3", "next.wav"])
        self.assertIn("Skipping sequence file: Hallowed Be Thy Name.seq.json", message.getvalue())

    def test_sequence_only_input_is_an_error(self):
        from contextlib import redirect_stderr
        from io import StringIO
        from pilights.cli import _audio_inputs

        with redirect_stderr(StringIO()), self.assertRaisesRegex(RuntimeError, "no audio files"):
            _audio_inputs(["song.seq.json"])


class SequenceTest(unittest.TestCase):
    def test_channel_limit_and_default_pin_mapping(self):
        from pilights.outputs import DEFAULT_PINS, Output

        self.assertEqual(DEFAULT_PINS, (14, 15, 18, 17, 27, 22, 23, 24))
        self.assertEqual(Output.channels, 8)
        Sequence(channels=8, duration_ms=10, events=[(0, 255)]).validate()
        for channels in (9, 16):
            with self.assertRaisesRegex(ValueError, "channels must be 1..8"):
                Sequence(channels=channels, duration_ms=10).validate()

    def test_round_trip(self):
        seq = Sequence(channels=8, duration_ms=1000, events=[(0, 0), (100, 255), (900, 0)], audio_sha256="ab")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.seq.json"
            seq.save(path)
            loaded = Sequence.load(path)
        self.assertEqual(loaded.events, seq.events)
        self.assertEqual(loaded.audio_sha256, "ab")

    def test_rejects_bad_mask(self):
        with self.assertRaises(ValueError):
            Sequence(channels=2, duration_ms=10, events=[(0, 4)]).validate()




@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is not installed")
class RegenerateTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.audio = self.dir / "song.wav"
        self.seq = self.dir / "song.seq.json"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=440:d=1", str(self.audio)], check=True)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def check(self):
        from pilights.cli import _check_sequence

        return _check_sequence(self.audio, self.seq, 8)

    def test_missing_sequence_is_made(self):
        self.assertEqual(self.check().analyzer_version, ANALYZER_VERSION)
        self.assertTrue(self.seq.exists())

    def test_old_version_is_made_again_with_same_options(self):
        from pilights.analyze import params_from_options
        from pilights.cli import _analyze_one

        _analyze_one(self.audio, self.seq, params_from_options({"sensitivity": 1.5}, 6))
        data = json.loads(self.seq.read_text())
        data["analyzer_version"] = ANALYZER_VERSION - 1
        self.seq.write_text(json.dumps(data))
        seq = self.check()
        self.assertEqual(seq.analyzer_version, ANALYZER_VERSION)
        self.assertEqual(seq.channels, 6)
        self.assertEqual(seq.meta["options"]["sensitivity"], 1.5)

    def test_too_many_channel_sequence_is_made_again_with_current_channel_count(self):
        from pilights.cli import _check_sequence

        data = {
            "format": "pilights-seq",
            "version": 1,
            "analyzer_version": ANALYZER_VERSION,
            "channels": 16,
            "duration_ms": 1000,
            "audio_sha256": "",
            "meta": {"options": {"sensitivity": 1.5}},
            "events": [[0, 0], [500, 65535], [1000, 0]],
        }
        self.seq.write_text(json.dumps(data))
        seq = _check_sequence(self.audio, self.seq, 8)
        self.assertEqual(seq.channels, 8)
        self.assertEqual(seq.analyzer_version, ANALYZER_VERSION)
        self.assertEqual(seq.meta["options"]["sensitivity"], 1.5)

    def test_current_version_is_kept(self):
        self.check()
        mtime = self.seq.stat().st_mtime_ns
        self.check()
        self.assertEqual(self.seq.stat().st_mtime_ns, mtime)

    def test_hand_made_sequence_is_kept(self):
        Sequence(channels=8, duration_ms=1000, events=[(0, 1), (500, 0)]).save(self.seq)
        self.assertEqual(self.check().events, [(0, 1), (500, 0)])


if __name__ == "__main__":
    unittest.main()
