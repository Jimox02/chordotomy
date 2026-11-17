"""Bounded, causal live chord preview. No beat/key or offline-schema claims."""

from __future__ import annotations

import sys
import threading
import time
from collections import deque
from contextlib import ExitStack, contextmanager
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
    engine: str = "dsp"
    processing_seconds: float = 0.0

    def to_dict(self):
        return {"schema": "chordotomy.live.v1", "type": "observation", **asdict(self)}


class Detector:
    """One-second trailing FFT window; repeat evidence before accepting a change.

    This is a lightweight preview, not the offline CQT/model engine. Timestamps
    describe the evidence window, not an estimated musical chord boundary.
    """

    def __init__(self, rate=RATE, window=1.0, hold=2, silence_db=-55.0, interval=0.0):
        if rate < 8000 or not 0.25 <= window <= 4 or hold < 1:
            raise ValueError("rate >= 8000, window 0.25..4 seconds and hold >= 1 required")
        self.interval, self.last_analysis = int(rate * interval), -int(rate * window)
        self.engine = "dsp"
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
            self.last_analysis = start_sample - self.size
            self.buffer = np.empty(0, dtype=np.float32)
            self.current, self.pending, self.count = "N", None, 0
        # Select the strongest channel: averaging phase-inverted stereo can cancel music.
        mono = data[:, np.argmax(np.mean(data**2, axis=0))]
        self.buffer = np.concatenate((self.buffer, mono))[-self.size :]
        self.end = start_sample + len(data)
        if len(self.buffer) < self.size or self.end - self.last_analysis < self.interval:
            return None
        level = float(20 * np.log10(max(float(np.sqrt(np.mean(mono**2))), 1e-12)))
        self.last_analysis = self.end
        began = time.perf_counter()
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
            self.engine,
            time.perf_counter() - began,
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


def _ole32():
    import ctypes

    dll = ctypes.WinDLL("ole32")
    dll.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    dll.CoInitializeEx.restype = ctypes.c_long
    dll.CoUninitialize.argtypes = []
    dll.CoUninitialize.restype = None
    return dll


@contextmanager
def com_apartment():
    """COM belongs to the calling thread, not to the imported SoundCard module."""
    if sys.platform != "win32":
        yield
        return
    ole = _ole32()
    # MTA, matching SoundCard's non-main-thread initialization.
    result = ole.CoInitializeEx(None, 0)
    if result not in (0, 1):
        raise RuntimeError(f"COM initialization failed: 0x{result & 0xFFFFFFFF:08x}")
    try:
        yield
    finally:
        # S_FALSE is successful too and increments the per-thread COM reference count.
        ole.CoUninitialize()


def speakers():
    # SoundCard initializes COM during its first import and rejects S_FALSE.
    # Import before entering our own scope; reused request threads still need the scope.
    sc = soundcard_backend()
    with com_apartment():
        return [{"id": s.id, "name": s.name} for s in sc.all_speakers()]


class Capture:
    """Producer owns WASAPI/COM. Old audio is dropped, never accumulated indefinitely."""

    def __init__(self, device=None, queue_blocks=10):
        self.device = device
        self.queue = Queue(maxsize=queue_blocks)
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.dropped = 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        apartment = ExitStack()
        try:
            # Let SoundCard perform its first-import initialization before our balanced call.
            sc = soundcard_backend()
            apartment.enter_context(com_apartment())
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
            apartment.close()
            self.ready.set()

    def __enter__(self):
        self.thread.start()
        if not self.ready.wait(10):
            self.stop.set()
            raise RuntimeError("audio device did not open within 10 seconds")
        if self.error:
            raise RuntimeError(f"cannot open loopback: {self.error}") from self.error
        return self

    def blocks(self, coalesce=False):
        while True:
            if self.error:
                raise RuntimeError(f"capture stopped: {self.error}") from self.error
            try:
                item = self.queue.get(timeout=0.25)
                if coalesce:
                    item = coalesce_blocks(self.queue, item)
                yield item
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


def coalesce_blocks(queue, first):
    """Drain queued capture into one contiguous batch, retaining only after the last gap."""
    start, audio = first
    parts = [audio]
    end = start + len(audio)
    while True:
        try:
            next_start, audio = queue.get_nowait()
        except Empty:
            break
        if next_start != end:
            start, parts = next_start, []
        parts.append(audio)
        end = next_start + len(audio)
    return start, np.concatenate(parts)


def _configure_live_cpu():
    import torch

    # Small repeated windows suffer from CPU thread-pool oversubscription.
    torch.set_num_threads(1)


class ModelDetector(Detector):
    """Experimental four-second trailing lv-chordia preview, at most once per audio second."""

    def __init__(self, rate=RATE):
        from . import model

        if not model.available():
            raise RuntimeError("模型未安装，请运行 uv sync --extra live --extra model")
        _configure_live_cpu()
        model._networks()  # Cached CPU ensemble; load once, before capture starts.
        super().__init__(rate=rate, window=4, interval=1)
        self.engine = "model"

    def _rank(self):
        import librosa

        from . import model
        from .features import HOP, SR

        audio = librosa.resample(self.buffer, orig_sr=self.rate, target_sr=SR)
        states, scores, _ = model.recognize(audio)
        # Use recent frames while excluding 200 ms of padded right-edge context.
        end = max(1, len(states) - round(0.2 * SR / HOP))
        start = max(0, end - round(0.5 * SR / HOP))
        chosen = int(np.bincount(states[start:end], minlength=len(LABELS)).argmax())
        mean = scores[:, start:end].mean(axis=1)
        ranked = [
            int(i) for i in np.argsort(-mean, kind="stable") if i != chosen and np.isfinite(mean[i])
        ]
        return [LABELS[i] for i in [chosen, *ranked][:3]]


def make_detector(engine="dsp", rate=RATE):
    if engine == "dsp":
        return Detector(rate=rate)
    if engine == "model":
        return ModelDetector(rate=rate)
    raise ValueError("engine must be dsp or model")
