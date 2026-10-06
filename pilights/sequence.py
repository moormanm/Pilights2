"""Sequence file format.

A sequence is a JSON file. It holds a list of change events. Each event is
``[time_ms, mask]``: at ``time_ms`` from the song start, channel ``i`` is ON
if bit ``i`` of ``mask`` is 1. The state stays the same until the next event.

Example::

    {
      "format": "pilights-seq",
      "version": 1,
      "analyzer_version": 2,
      "channels": 8,
      "duration_ms": 183400,
      "audio_sha256": "…",
      "meta": {"source": "song.mp3", "...": "..."},
      "events": [[0, 0], [125, 3], [250, 1], ...]
    }

``version`` is the version of the file format. ``analyzer_version`` is the
version of the analysis method that made the events (0 = not made by
pilights, or made before this field existed).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

FORMAT = "pilights-seq"
VERSION = 1
MAX_CHANNELS = 8


@dataclass
class Sequence:
    channels: int
    duration_ms: int
    events: list = field(default_factory=list)  # list of (time_ms, mask)
    audio_sha256: str = ""
    meta: dict = field(default_factory=dict)
    analyzer_version: int = 0

    def validate(self) -> None:
        if not 1 <= self.channels <= MAX_CHANNELS:
            raise ValueError(f"channels must be 1..{MAX_CHANNELS}, got {self.channels}")
        limit = 1 << self.channels
        last = -1
        for t, mask in self.events:
            if t < last:
                raise ValueError(f"events are not sorted by time at t={t}")
            if not 0 <= mask < limit:
                raise ValueError(f"mask {mask} at t={t} is out of range")
            last = t

    def to_dict(self) -> dict:
        return {
            "format": FORMAT,
            "version": VERSION,
            "analyzer_version": self.analyzer_version,
            "channels": self.channels,
            "duration_ms": self.duration_ms,
            "audio_sha256": self.audio_sha256,
            "meta": self.meta,
            "events": [[int(t), int(m)] for t, m in self.events],
        }

    def save(self, path: str | Path) -> None:
        self.validate()
        Path(path).write_text(json.dumps(self.to_dict(), separators=(",", ":")) + "\n")

    @classmethod
    def load(cls, path: str | Path) -> "Sequence":
        data = json.loads(Path(path).read_text())
        if data.get("format") != FORMAT:
            raise ValueError(f"{path}: not a {FORMAT} file")
        if data.get("version") != VERSION:
            raise ValueError(f"{path}: version {data.get('version')} is not supported")
        seq = cls(
            channels=data["channels"],
            duration_ms=data["duration_ms"],
            events=[(int(t), int(m)) for t, m in data["events"]],
            audio_sha256=data.get("audio_sha256", ""),
            meta=data.get("meta", {}),
            analyzer_version=int(data.get("analyzer_version", 0)),
        )
        seq.validate()
        return seq


    @property
    def made_by_pilights(self) -> bool:
        return self.analyzer_version > 0 or "params" in self.meta


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def default_sequence_path(audio_path: str | Path) -> Path:
    p = Path(audio_path)
    return p.with_name(p.stem + ".seq.json")
