"""Editable bar/beat chart assembled from live observations; unknown beats stay empty."""

from __future__ import annotations

import math
from collections import defaultdict, deque
from itertools import groupby

from . import harmony
from .chords import QUALITIES, ROOTS

MAX_BEATS = 4096


def parse_chord(text):
    """Accept supported chart symbols or exact Harte labels; preserve a slash bass."""
    if not isinstance(text, str) or len(text) > 40:
        raise ValueError("请输入和弦，例如 C、Am7、D7/F#")
    text = text.strip()
    if not text:
        return None, None
    if ":" in text:
        label, _, bass = text.partition("/")
        root, quality = label.split(":", 1)
        if root not in ROOTS or quality not in QUALITIES or (bass and bass not in ROOTS):
            raise ValueError("不支持的和弦格式")
        return label, bass or None
    # Use the existing chart parser, but reject extensions it would silently simplify.
    from .chart import NOTE
    from .chart import parse_chord as parse

    head = text.partition("/")[0]
    note = NOTE.match(head)
    exact = {
        "",
        "M",
        "maj",
        "m",
        "min",
        "-",
        "7",
        "M7",
        "maj7",
        "m7",
        "min7",
        "-7",
        "m6",
        "m7b5",
        "dim7",
        "dim",
        "aug",
        "+",
        "sus4",
        "sus2",
        "7sus4",
        "add9",
        "madd9",
    }
    if text not in ("N", "N.C.") and (not note or head[note.end() :] not in exact):
        raise ValueError("暂不支持此和弦，请使用三和弦、七和弦、sus 或 add9")
    return parse(text)


class Score:
    def __init__(self):
        self.bpm = None
        self.meter = None
        self.offset = 0.0
        self.key = None
        self.observations = deque(maxlen=18000)
        self.votes = defaultdict(lambda: defaultdict(float))
        self.manual = {}
        self.duration = 0.0
        self.analysis = {}
        self.global_key = None
        self.regions = []
        self.analysis_status = "pending"
        self.revision = 0

    @property
    def step(self):
        return 60 / self.bpm * 4 / self.meter[1] if self.bpm and self.meter else None

    def invalidate(self):
        self.analysis, self.regions, self.global_key = {}, [], None
        self.analysis_status = "pending"
        self.revision += 1

    def reset(self):
        self.observations.clear()
        self.votes.clear()
        self.manual.clear()
        self.duration = 0
        self.invalidate()

    def configure(self, body):
        bpm, meter, offset, key = (body.get(k) for k in ("bpm", "meter", "offset", "key"))
        if bpm is not None and (
            type(bpm) not in (int, float) or not math.isfinite(bpm) or not 20 <= bpm <= 300
        ):
            raise ValueError("速度范围为 20–300 BPM，暂未确定可留空")
        if meter is not None and meter not in (
            "2/4",
            "3/4",
            "4/4",
            "5/4",
            "6/8",
            "7/8",
            "9/8",
            "12/8",
        ):
            raise ValueError("不支持的拍号")
        if type(offset) not in (int, float) or not math.isfinite(offset) or not 0 <= offset <= 3600:
            raise ValueError("第一小节起点须在 0–3600 秒")
        if key is not None and not isinstance(key, str):
            raise ValueError("无效调性")
        normalized = harmony.parse_key(key) if key else None
        parsed_meter = tuple(map(int, meter.split("/"))) if meter else None
        self.bpm, self.meter, self.offset, self.key = bpm, parsed_meter, offset, normalized
        self.votes.clear()
        for event in self.observations:
            self._vote(event)
        self.invalidate()

    def add(self, event):
        self.observations.append(event)
        self.duration = max(self.duration, event["end_time"])
        self._vote(event)
        self.invalidate()

    def _vote(self, event):
        step = self.step
        if step is None or not event["stable"] or event.get("discontinuity"):
            return
        end = event["end_time"] - (0.2 if event.get("engine") == "model" else 0)
        # Only cover the interval actually read by the live estimator; never fill gaps.
        start = max(
            event["start_time"], end - (0.5 if event.get("engine") == "model" else 0.1), self.offset
        )
        if end <= start:
            return
        chord = event["chord"] if event["chord"] != "N" else None
        low = max(0, math.floor((start - self.offset) / step))
        high = min(MAX_BEATS, math.ceil((end - self.offset) / step))
        for beat in range(low, high):
            a = self.offset + beat * step
            overlap = max(0, min(end, a + step) - max(start, a))
            self.votes[beat][chord] += overlap

    def cell(self, beat):
        if beat in self.manual:
            chord, bass = self.manual[beat]
            source = "manual"
        else:
            votes = self.votes.get(beat, {})
            chord = max(votes, key=votes.get) if votes else None
            # Insufficient observed support remains blank instead of extending a prior chord.
            if chord is not None and votes[chord] < self.step * 0.3:
                chord = None
            bass, source = None, "live" if chord else None
        return dict(beat=beat, chord=chord, bass=bass, source=source, **self.analysis.get(beat, {}))

    def edit(self, beat, text, reset=False):
        if type(beat) is not int or not 0 <= beat < MAX_BEATS:
            raise ValueError("拍位置超出范围")
        if self.step is None:
            raise ValueError("请先填写速度和拍号")
        if reset:
            self.manual.pop(beat, None)
        else:
            self.manual[beat] = parse_chord(text)
        self.invalidate()

    def count(self):
        elapsed = math.ceil(max(0, self.duration - self.offset) / self.step) if self.step else 0
        return min(MAX_BEATS, max(elapsed, max(self.manual, default=-1) + 1))

    def analyze(self):
        if self.step is None:
            return
        cells = [self.cell(i) for i in range(self.count())]
        if not any(c["chord"] and c["chord"] != "N" for c in cells):
            return
        runs = [
            (label, len(list(group)))
            for label, group in groupby(cells, key=lambda c: c["chord"] or "N")
        ]
        key, regions, analyses = harmony.analyze(runs, self.key)
        beat = 0
        result = {}
        for (label, length), item in zip(runs, analyses, strict=True):
            for i in range(beat, beat + length):
                if label != "N":
                    region = next(
                        r["label"] for r in regions if r["start_beat"] <= i < r["end_beat"]
                    )
                    result[i] = {**item, "key": region}
            beat += length
        self.analysis, self.global_key, self.regions = result, key, regions
        self.analysis_status = "complete"
        self.revision += 1

    def snapshot(self):
        count = self.count()
        return dict(
            schema="chordotomy.live.score.v1",
            revision=self.revision,
            bpm=self.bpm,
            bpm_unit="quarter",
            meter="/".join(map(str, self.meter)) if self.meter else None,
            offset=self.offset,
            key=self.key,
            global_key=self.global_key,
            key_regions=self.regions,
            analysis_status=self.analysis_status,
            analysis_source="live_chord_sequence",
            duration=self.duration,
            retained_from=self.observations[0]["start_time"] if self.observations else 0,
            limit=MAX_BEATS,
            cells=[self.cell(i) for i in range(count)],
        )
