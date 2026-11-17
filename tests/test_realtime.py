import json
import threading
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from typer.testing import CliRunner

from chordotomy.cli import app
from chordotomy.realtime import Capture, Detector

RATE = 8000


def tone(notes, seconds=2):
    t = np.arange(round(RATE * seconds)) / RATE
    return sum(0.15 * np.sin(2 * np.pi * 440 * 2 ** ((n - 69) / 12) * t) for n in notes)


def stream(detector, audio, start=0):
    events = []
    for offset in range(0, len(audio), 800):
        event = detector.feed(audio[offset : offset + 800], start + offset)
        if event:
            events.append(event)
    return events


@pytest.mark.parametrize(
    "notes,label", [([60, 64, 67], "C:maj"), ([57, 60, 64], "A:min"), ([55, 59, 62, 65], "G:7")]
)
def test_known_chords(notes, label):
    events = stream(Detector(rate=RATE), tone(notes))
    assert events[-1].chord == label
    assert events[-1].stable


def test_change_silence_and_stereo_cancellation():
    c = tone([60, 64, 67])
    audio = np.concatenate([c, tone([57, 60, 64]), np.zeros(RATE * 2)])
    events = stream(Detector(rate=RATE), np.column_stack([audio, -audio]))
    assert any(e.chord == "C:maj" for e in events)
    assert any(e.chord == "A:min" for e in events)
    assert events[-1].chord == "N"
    assert all(e.start_time >= 0 and e.end_time > e.start_time for e in events)


def test_gap_resets_evidence_and_bounded_buffer():
    d = Detector(rate=RATE)
    stream(d, tone([60, 64, 67]))
    assert d.current == "C:maj"
    assert d.feed(np.zeros(800), 80000) is None
    assert d.current == "N"
    stream(d, np.zeros(80000), 80800)
    assert len(d.buffer) <= RATE


def test_file_cli_and_no_overwrite(tmp_path):
    path, output = tmp_path / "test.wav", tmp_path / "events.jsonl"
    sf.write(path, tone([60, 64, 67]), RATE)
    runner = CliRunner()
    args = ["listen", "--input", str(path), "--fast", "--output", str(output)]
    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    events = [json.loads(line) for line in output.read_text().splitlines()]
    assert events[-1]["chord"] == "C:maj"
    assert events[-1]["schema"] == "chordotomy.live.v1"
    original = output.read_bytes()
    assert runner.invoke(app, args).exit_code == 1
    assert output.read_bytes() == original


def test_capture_bounded_queue_and_resource_cleanup(monkeypatch):
    import chordotomy.realtime as live

    counter = 0
    closed = []
    disconnect = threading.Event()

    class Recorder:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            closed.append(True)

        def record(self, numframes):
            nonlocal counter
            counter += 1
            if counter == 31:
                disconnect.wait(2)
                raise RuntimeError("device disconnected")
            return np.zeros((numframes, 2))

    backend = SimpleNamespace(
        default_speaker=lambda: SimpleNamespace(id="output"),
        get_microphone=lambda *a, **kw: SimpleNamespace(recorder=lambda **kw: Recorder()),
    )
    monkeypatch.setattr(live, "soundcard_backend", lambda: backend)
    with Capture() as capture:
        disconnect.set()
        capture.thread.join(2)
        assert capture.queue.qsize() == 10
        assert capture.dropped == 20
        with pytest.raises(RuntimeError, match="disconnected"):
            next(capture.blocks())
    assert closed


def test_gap_marker_survives_window_warmup():
    d = Detector(rate=RATE)
    stream(d, tone([60, 64, 67]))
    events = stream(d, tone([57, 60, 64]), 40000)
    assert events[0].discontinuity
    assert not events[1].discontinuity
    assert events[-1].chord == "A:min"


def test_noise_is_not_forced_to_chord():
    noise = np.random.default_rng(42).normal(0, 0.05, RATE * 2)
    assert stream(Detector(rate=RATE), noise)[-1].chord == "N"


def test_duration_limits_audio_timestamps(tmp_path):
    path = tmp_path / "test.wav"
    sf.write(path, tone([60, 64, 67]), RATE)
    result = CliRunner().invoke(
        app, ["listen", "--input", str(path), "--fast", "--duration", "1.25"]
    )
    assert result.exit_code == 0
    rows = [json.loads(line) for line in result.output.splitlines() if line.startswith("{")]
    assert rows[-1]["end_time"] == 1.25


@pytest.mark.parametrize("result", [0, 1])
def test_com_init_balanced_on_exception(monkeypatch, result):
    import chordotomy.realtime as live

    calls = []
    monkeypatch.setattr(live, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        live,
        "_ole32",
        lambda: SimpleNamespace(
            CoInitializeEx=lambda *args: calls.append(("init", threading.get_ident())) or result,
            CoUninitialize=lambda: calls.append(("close", threading.get_ident())),
        ),
    )
    with pytest.raises(ValueError), live.com_apartment():
        raise ValueError("test")
    assert [c[0] for c in calls] == ["init", "close"]
    assert calls[0][1] == calls[1][1]


def test_failed_com_does_not_uninitialize(monkeypatch):
    import chordotomy.realtime as live

    calls = []
    monkeypatch.setattr(live, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        live,
        "_ole32",
        lambda: SimpleNamespace(
            CoInitializeEx=lambda *args: -2147417850,
            CoUninitialize=lambda: calls.append("close"),
        ),
    )
    with pytest.raises(RuntimeError, match="80010106"), live.com_apartment():
        pytest.fail("failed initialization must not enter")
    assert calls == []


def test_capture_initializes_com_on_worker_before_backend(monkeypatch):
    from contextlib import contextmanager

    import chordotomy.realtime as live

    calls = []

    @contextmanager
    def apartment():
        calls.append(("init", threading.get_ident()))
        try:
            yield
        finally:
            calls.append(("close", threading.get_ident()))

    def backend():
        calls.append(("backend", threading.get_ident()))
        raise RuntimeError("device unavailable")

    monkeypatch.setattr(live, "com_apartment", apartment)
    monkeypatch.setattr(live, "soundcard_backend", backend)
    capture = Capture()
    with pytest.raises(RuntimeError, match="device unavailable"):
        capture.__enter__()
    capture.thread.join(2)
    assert [c[0] for c in calls] == ["init", "backend", "close"]
    assert len({c[1] for c in calls}) == 1
    assert calls[0][1] != threading.get_ident()


def test_speaker_enumeration_has_own_com_scope(monkeypatch):
    from contextlib import contextmanager

    import chordotomy.realtime as live

    calls = []

    @contextmanager
    def apartment():
        calls.append("init")
        yield
        calls.append("close")

    def backend():
        assert calls == ["init"]
        return SimpleNamespace(all_speakers=lambda: [SimpleNamespace(id="x", name="output")])

    monkeypatch.setattr(live, "com_apartment", apartment)
    monkeypatch.setattr(live, "soundcard_backend", backend)
    assert live.speakers() == [{"id": "x", "name": "output"}]
    assert calls == ["init", "close"]
