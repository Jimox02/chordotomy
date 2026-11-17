import pytest

from chordotomy.score import MAX_BEATS, Score, parse_chord


def configure(score, **kwargs):
    score.configure(dict(bpm=120, meter="4/4", offset=0, key="C:maj", **kwargs))


def event(end, chord="C:maj", stable=True, engine="dsp", gap=False):
    return dict(
        start_time=max(0, end - (4 if engine == "model" else 1)),
        end_time=end,
        chord=chord,
        stable=stable,
        engine=engine,
        discontinuity=gap,
    )


def test_unknown_metadata_and_beats_remain_empty():
    s = Score()
    s.add(event(2))
    assert s.snapshot()["cells"] == []
    assert s.snapshot()["bpm"] is None
    s.configure(dict(bpm=120, meter="4/4", offset=0, key=None))
    cells = s.snapshot()["cells"]
    assert all(c["chord"] is None for c in cells)  # A lone 100ms sample is insufficient.
    assert all("numeral" not in c for c in cells)


def test_live_votes_do_not_backfill_or_bridge_gaps():
    s = Score()
    configure(s)
    for end in (1.1, 1.2, 1.3, 1.4, 1.5):
        s.add(event(end))
    s.add(event(3, gap=True))
    s.add(event(3.1, stable=False))
    cells = s.snapshot()["cells"]
    assert cells[0]["chord"] is None and cells[1]["chord"] is None
    assert cells[2]["chord"] == "C:maj"
    assert all(c["chord"] is None for c in cells[3:])
    assert s.analysis_status == "pending"


def test_manual_progression_functions_and_borrowing():
    s = Score()
    configure(s)
    for beat, chord in enumerate(["C", "D7", "G", "Fm", "C"]):
        s.edit(beat, chord)
    assert all("numeral" not in c for c in s.snapshot()["cells"])
    s.analyze()
    cells = s.snapshot()["cells"]
    assert cells[0]["numeral"] == "I"
    assert cells[1]["numeral"] == "V7/V"
    assert cells[1]["role"] == "secondary_dominant"
    assert cells[3]["numeral"] == "iv"
    assert cells[3]["role"] == "borrowed"
    assert s.global_key["source"] == "given"
    s.edit(1, "Dm")
    assert s.analysis_status == "pending"
    s.analyze()
    assert s.cell(1)["numeral"] == "ii"


def test_auto_key_and_manual_blank_override():
    s = Score()
    s.configure(dict(bpm=120, meter="4/4", offset=0, key=None))
    for beat, chord in enumerate(["C", "F", "G7", "C"]):
        s.edit(beat, chord)
    s.analyze()
    assert s.global_key["label"] == "C:maj"
    assert s.global_key["source"] == "estimated"
    s.edit(2, "")
    s.analyze()
    assert s.cell(2)["chord"] is None
    assert "numeral" not in s.cell(2)
    s.edit(2, "", reset=True)
    assert s.cell(2)["source"] is None


def test_compound_meter_offset_and_regrid():
    s = Score()
    s.configure(dict(bpm=120, meter="6/8", offset=1, key=None))
    assert s.step == 0.25
    for end in (1.1, 1.2):
        s.add(event(end))
    assert s.cell(0)["chord"] == "C:maj"
    s.configure(dict(bpm=60, meter="3/4", offset=2, key=None))
    assert s.step == 1
    assert s.cell(0)["chord"] is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), True, 0, 999, "120"])
def test_invalid_tempo_is_atomic(value):
    s = Score()
    configure(s)
    with pytest.raises(ValueError):
        s.configure(dict(bpm=value, meter="4/4", offset=0, key=None))
    assert s.bpm == 120


def test_manual_edits_survive_live_updates_and_session_reset_keeps_metadata():
    s = Score()
    configure(s)
    s.edit(2, "Am7")
    for end in (1.1, 1.2, 1.3):
        s.add(event(end))
    assert s.cell(2)["chord"] == "A:min7"
    s.reset()
    assert s.count() == 0 and s.bpm == 120
    with pytest.raises(ValueError):
        s.edit(MAX_BEATS, "C")
    with pytest.raises(ValueError):
        s.edit(-1, "C")


def test_supported_chord_parser_preserves_bass_rejects_lossy_extensions():
    assert parse_chord("D7/F#") == ("D:7", "F#")
    assert parse_chord("Bbmaj7") == ("A#:maj7", None)
    assert parse_chord("C:maj(9)") == ("C:maj(9)", None)
    assert parse_chord("") == (None, None)
    with pytest.raises(ValueError):
        parse_chord("C13")
    with pytest.raises(ValueError):
        parse_chord("hello")


def test_live_n_is_preserved_in_chart_and_export():
    s = Score()
    configure(s)
    for end in (1.1, 1.2, 1.3, 1.4, 1.5):
        s.add(event(end, chord="N"))
    assert s.cell(2)["chord"] == "N"
    assert s.cell(2)["source"] == "live"
    assert s.snapshot()["cells"][0]["chord"] is None
    s.edit(3, "N")
    assert s.snapshot()["cells"][3]["chord"] == "N"
    s.analyze()
    assert "numeral" not in s.cell(2)
