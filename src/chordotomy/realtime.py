"""Bounded, causal live chord preview. No beat/key or offline-schema claims."""

from __future__ import annotations

import sys
import threading
from collections import deque
from dataclasses import asdict, dataclass
from queue import Empty, Full, Queue

import numpy as np

from .chords import LABELS, match

RATE = 48000
BLOCK = 4800


@dataclass
class Event:
    start_time: float
    end_time: float
    chord: str
    candidates: list[str]
    level_dbfs: float
    stable: bool
    discontinuity: bool = False

    def to_dict(self):
        return {"schema": "chordotomy.live.v1", "type": "observation", **asdict(self)}


class Detector:
    """One-second trailing FFT window; repeat evidence before accepting a change.

    This is a lightweight preview, not the offline CQT/model engine. Timestamps
    describe the evidence window, not an estimated musical chord boundary.
    """

    def __init__(self, rate=RATE, window=1.0, hold=2, silence_db=-55.0):
        if rate < 8000 or not 0.25 <= window <= 4 or hold < 1:
            raise ValueError("rate >= 8000, window 0.25..4 seconds and hold >= 1 required")
        self.rate, self.size, self.hold = rate, int(rate * window), hold
        self.silence_db = silence_db
        self.buffer = np.empty(0, dtype=np.float32)
        self.end = 0
        self.discontinuity = False
        self.current = "N"
        self.pending = None
        self.count = 0
        self.taper = np.hanning(self.size)

    def feed(self, data, start_sample):
        data = np.asarray(data, dtype=np.float32)
        if data.ndim == 1:
            data = data[:, None]
        if data.ndim != 2 or not np.isfinite(data).all():
            raise ValueError("audio must be finite frames x channels")
        if len(data) == 0:
            return None
        gap = start_sample != self.end
        if gap:
            self.discontinuity = True
            self.buffer = np.empty(0, dtype=np.float32)
            self.current, self.pending, self.count = "N", None, 0
        # Select the strongest channel: averaging phase-inverted stereo can cancel music.
        mono = data[:, np.argmax(np.mean(data**2, axis=0))]
        self.buffer = np.concatenate((self.buffer, mono))[-self.size :]
        self.end = start_sample + len(data)
        if len(self.buffer) < self.size:
            return None
        level = float(20 * np.log10(max(float(np.sqrt(np.mean(mono**2))), 1e-12)))
        candidates = self._rank() if level > self.silence_db else ["N"]
        proposed = candidates[0]
        self.count = self.count + 1 if proposed == self.pending else 1
        self.pending = proposed
        if self.count >= self.hold:
            self.current = proposed
        event = Event(
            (self.end - self.size) / self.rate,
            self.end / self.rate,
            self.current,
            candidates,
            level,
            self.current == proposed and self.count >= self.hold,
            self.discontinuity,
        )

        self.discontinuity = False
        return event

    def _rank(self):
        spectrum = np.abs(np.fft.rfft(self.buffer * self.taper))
        frequencies = np.fft.rfftfreq(self.size, 1 / self.rate)
        band = spectrum[(frequencies >= 65) & (frequencies <= 2100)]
        flatness = np.exp(np.mean(np.log(np.maximum(band, 1e-12)))) / max(float(band.mean()), 1e-12)
        if flatness > 0.5:
            return ["N"]
        peaks = (
            np.flatnonzero(
                (spectrum[1:-1] > spectrum[:-2])
                & (spectrum[1:-1] >= spectrum[2:])
                & (spectrum[1:-1] >= max(spectrum.max() * 0.04, 1e-9))
            )
            + 1
        )
        peaks = peaks[(frequencies[peaks] >= 65) & (frequencies[peaks] <= 2100)]
        chroma = np.zeros(12)
        if len(peaks) < 3:
            return ["N"]
        midi = 69 + 12 * np.log2(frequencies[peaks] / 440)
        nearest = np.rint(midi).astype(int)
        tonal = np.abs(midi - nearest) < 0.35
        np.add.at(chroma, nearest[tonal] % 12, spectrum[peaks[tonal]])
        if np.count_nonzero(chroma > chroma.max() * 0.1) < 3:
            return ["N"]
        scores = match(chroma[:, None], np.zeros((12, 1)))[:, 0]
        return [LABELS[i] for i in np.argsort(-scores, kind="stable")[:3]]


def soundcard_backend():
    if sys.platform != "win32":
        raise RuntimeError("system loopback currently supports Windows only; use --input for files")
    try:
        import soundcard
    except ImportError as exc:
        raise RuntimeError("install live capture with: uv sync --extra live") from exc
    return soundcard


def speakers():
    return [{"id": s.id, "name": s.name} for s in soundcard_backend().all_speakers()]


class Capture:
    """Producer owns WASAPI/COM. Old audio is dropped, never accumulated indefinitely."""

    def __init__(self, device=None):
        self.device = device
        self.queue = Queue(maxsize=10)
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.dropped = 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        try:
            sc = soundcard_backend()
            speaker = sc.get_speaker(self.device) if self.device else sc.default_speaker()
            if speaker is None:
                raise RuntimeError("no playback device found")
            mic = sc.get_microphone(speaker.id, include_loopback=True)
            # All native channels: SoundCard documents broken WASAPI single-channel recording.
            with mic.recorder(samplerate=RATE, blocksize=BLOCK * 4) as recorder:
                self.ready.set()
                start = 0
                while not self.stop.is_set():
                    data = recorder.record(numframes=BLOCK)
                    item = (start, data)
                    start += len(data)
                    try:
                        self.queue.put_nowait(item)
                    except Full:
                        try:
                            self.queue.get_nowait()
                            self.dropped += 1
                        except Empty:
                            pass
                        self.queue.put_nowait(item)
        except Exception as exc:
            self.error = exc
        finally:
            self.ready.set()

    def __enter__(self):
        self.thread.start()
        if not self.ready.wait(10):
            self.stop.set()
            raise RuntimeError("audio device did not open within 10 seconds")
        if self.error:
            raise RuntimeError(f"cannot open loopback: {self.error}") from self.error
        return self

    def blocks(self):
        while True:
            if self.error:
                raise RuntimeError(f"capture stopped: {self.error}") from self.error
            try:
                yield self.queue.get(timeout=0.25)
            except Empty:
                if not self.thread.is_alive():
                    return

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join(timeout=2)


class Timeline:
    """Bounded terminal history; full observations can be streamed to disk as JSONL."""

    def __init__(self, limit=60):
        self.items = deque(maxlen=limit)

    def append(self, event):
        if not self.items or self.items[-1].chord != event.chord:
            self.items.append(event)
