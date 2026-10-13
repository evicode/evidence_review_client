"""Reading the shape of a recording, and working on it.

EVP review is hours of near-silence with a two-second whisper in it somewhere. The
property every test here protects is that the quiet thing is findable: that it shows in
the envelope, that it is offered as a place to listen, and that it survives the journey
from the file to the pixels.

The envelope is checked against a recording built with events at known times and known
levels, because "it returned an array" says nothing about whether the array describes
the recording.

These need the bundled ffmpeg and skip without it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from evidence_review.media.clip import find_ffmpeg, find_ffprobe
from evidence_review.media.waveform import (
    PEAKS_PER_SECOND,
    Waveform,
    WaveformError,
    audio_duration,
    cache_path,
    extract_peaks,
    load_peaks,
    prune_cache,
    save_peaks,
)

ffmpeg = find_ffmpeg()

needs_ffmpeg = pytest.mark.skipif(
    ffmpeg is None or find_ffprobe() is None,
    reason="ffmpeg is not present; run installer/fetch_ffmpeg.ps1",
)

#: Where the events are in the fixture recording, and roughly how loud.
LOUD_AT = 10.0
QUIET_AT = 40.0
SILENT_AT = 25.0


@pytest.fixture(scope="module")
def recording(tmp_path_factory) -> Path:
    """A minute of silence with a loud event at 0:10 and a quiet one at 0:40.

    The quiet one is the point. It is the shape of the problem: something barely above
    the noise that still has to be visible, because that is what an EVP reviewer is
    looking for and what a careless implementation loses.
    """
    if ffmpeg is None:
        pytest.skip("ffmpeg is not present")
    target = tmp_path_factory.mktemp("audio") / "session.wav"
    subprocess.run(
        [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=60",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=60:sample_rate=44100",
            "-f", "lavfi", "-i", "sine=frequency=3000:duration=60:sample_rate=44100",
            "-filter_complex",
            "[1:a]volume=0.8,atrim=0:2,adelay=10000|10000[loud];"
            "[2:a]volume=0.05,atrim=0:1,adelay=40000|40000[quiet];"
            "[0:a][loud][quiet]amix=inputs=3:normalize=0,atrim=0:60",
            "-c:a", "pcm_s16le", str(target),
        ],
        check=True, capture_output=True,
    )
    return target


@pytest.fixture(scope="module")
def waveform(recording: Path) -> Waveform:
    return extract_peaks(recording)


def level_at(wave: Waveform, second: float, span: float = 1.0) -> int:
    low, high = wave.window(second, second + span, 1)[0]
    return max(abs(low), abs(high))


# --------------------------------------------------------------------------- #
# Does the envelope describe the recording?
# --------------------------------------------------------------------------- #


@needs_ffmpeg
def test_the_length_is_read(recording: Path) -> None:
    assert audio_duration(recording) == pytest.approx(60.0, abs=0.2)


@needs_ffmpeg
def test_there_is_one_bucket_per_ten_milliseconds(waveform: Waveform) -> None:
    assert waveform.peaks_per_second == PEAKS_PER_SECOND
    assert waveform.bucket_count == pytest.approx(60 * PEAKS_PER_SECOND, abs=200)


@needs_ffmpeg
def test_the_loud_event_is_where_it_was_recorded(waveform: Waveform) -> None:
    assert level_at(waveform, LOUD_AT + 0.2) > level_at(waveform, SILENT_AT) * 20


@needs_ffmpeg
def test_the_quiet_event_shows_too(waveform: Waveform) -> None:
    """The whole reason this view exists. A quiet event that reads as silence is a
    waveform that has lost the thing somebody is looking for."""
    quiet = level_at(waveform, QUIET_AT + 0.2)
    assert quiet > level_at(waveform, SILENT_AT) * 5
    assert quiet < level_at(waveform, LOUD_AT + 0.2)


@needs_ffmpeg
def test_silence_reads_as_silence(waveform: Waveform) -> None:
    assert level_at(waveform, SILENT_AT) < 50


@needs_ffmpeg
def test_the_scale_comes_from_the_recording_not_the_format(waveform: Waveform) -> None:
    """Nothing in EVP is recorded near full scale, so a view drawn against 32767 is a
    flat line. The peak the view normalises against has to be the file's own."""
    assert 0 < waveform.peak_amplitude < 32767


# --------------------------------------------------------------------------- #
# Drawing a window of it
# --------------------------------------------------------------------------- #


@needs_ffmpeg
def test_a_window_gives_one_column_per_pixel(waveform: Waveform) -> None:
    columns = waveform.window(0, 60, 400)
    assert len(columns) == 400
    assert all(low <= high for low, high in columns)


@needs_ffmpeg
def test_a_column_takes_the_extreme_not_the_average(waveform: Waveform) -> None:
    """Averaging is how a waveform loses the single click somebody is hunting for.

    The whole recording squeezed into one column has to still show the loudest sample
    in it, not the mean of a minute of mostly-silence.
    """
    whole = waveform.window(0, 60, 1)[0]
    assert max(abs(whole[0]), whole[1]) == waveform.peak_amplitude


@needs_ffmpeg
def test_zooming_in_shows_the_event_filling_the_view(waveform: Waveform) -> None:
    floor = level_at(waveform, SILENT_AT)
    columns = waveform.window(LOUD_AT, LOUD_AT + 2.0, 200)
    loud_columns = sum(1 for low, high in columns if max(abs(low), high) > floor * 10)
    assert loud_columns > 150


@needs_ffmpeg
def test_asking_past_the_end_does_not_raise(waveform: Waveform) -> None:
    assert len(waveform.window(55.0, 600.0, 50)) == 50


def test_an_empty_waveform_draws_nothing() -> None:
    import array

    empty = Waveform(array.array("h"), PEAKS_PER_SECOND, 10.0, 1)
    assert empty.window(0, 10, 100) == []
    assert empty.loudest_moments() == []
    assert empty.noise_floor() == 0


# --------------------------------------------------------------------------- #
# Where to listen
# --------------------------------------------------------------------------- #


@needs_ffmpeg
def test_both_events_are_offered_as_places_to_listen(waveform: Waveform) -> None:
    moments = waveform.loudest_moments(count=8, apart=3.0)
    assert any(abs(m - LOUD_AT) < 2.0 for m in moments)
    assert any(abs(m - QUIET_AT) < 2.0 for m in moments)


@needs_ffmpeg
def test_silence_is_never_offered_as_a_place_to_listen(waveform: Waveform) -> None:
    """A list padded out with silence looks like an answer and is not one. It sends a
    reviewer to empty air, which is worse than offering nothing."""
    moments = waveform.loudest_moments(count=20, apart=3.0)
    assert moments, "the recording has two real events in it"
    for moment in moments:
        assert any(abs(moment - real) < 2.5 for real in (LOUD_AT, QUIET_AT)), (
            f"{moment:.1f}s is silence"
        )


# --------------------------------------------------------------------------- #
# Reading one span at a finer resolution
# --------------------------------------------------------------------------- #


@needs_ffmpeg
def test_a_span_can_be_read_on_its_own(recording: Path) -> None:
    """What zooming past 10 ms buckets does. Reading the whole file again at that
    resolution would be minutes of work for two seconds of view."""
    detail = extract_peaks(
        recording, peaks_per_second=1000, start=LOUD_AT, duration=2.0
    )
    assert detail.duration == pytest.approx(2.0, abs=0.01)
    assert detail.bucket_count > 1500
    assert max(abs(detail.peaks[0]), abs(detail.peaks[1])) > 0


# --------------------------------------------------------------------------- #
# Caching
# --------------------------------------------------------------------------- #


@needs_ffmpeg
def test_an_envelope_survives_a_round_trip(waveform: Waveform, tmp_path: Path) -> None:
    target = tmp_path / "x.peaks"
    assert save_peaks(waveform, target)

    reloaded = load_peaks(target)
    assert reloaded is not None
    assert reloaded.bucket_count == waveform.bucket_count
    assert reloaded.peaks_per_second == waveform.peaks_per_second
    assert reloaded.peak_amplitude == waveform.peak_amplitude
    assert reloaded.duration == pytest.approx(waveform.duration)
    assert level_at(reloaded, LOUD_AT + 0.2) == level_at(waveform, LOUD_AT + 0.2)


def test_a_corrupt_cache_is_a_miss_not_a_crash(tmp_path: Path) -> None:
    """A derived file that cannot be read must never stop a recording being opened."""
    bad = tmp_path / "bad.peaks"
    bad.write_bytes(b"this is not a waveform")
    assert load_peaks(bad) is None
    assert load_peaks(tmp_path / "absent.peaks") is None


def test_the_cache_key_changes_when_the_file_does(tmp_path: Path) -> None:
    """Keyed on size and modification time, like the media hash cache. A re-encoded
    recording has to be read again rather than drawn from a stale envelope."""
    media = tmp_path / "a.wav"
    media.write_bytes(b"x" * 100)
    first = cache_path(tmp_path, media)

    media.write_bytes(b"x" * 200)
    second = cache_path(tmp_path, media)

    assert first is not None and second is not None
    assert first != second


def test_pruning_keeps_the_most_recently_used(tmp_path: Path) -> None:
    import os
    import time

    for index in range(5):
        entry = tmp_path / f"{index}.peaks"
        entry.write_bytes(b"0" * 1000)
        # Spread the access times so "least recently used" means something.
        stamp = time.time() - (5 - index) * 100
        os.utime(entry, (stamp, stamp))

    removed = prune_cache(tmp_path, keep_bytes=2500)

    assert removed > 0
    survivors = sorted(p.name for p in tmp_path.glob("*.peaks"))
    assert "4.peaks" in survivors, "the most recently used was deleted"
    assert "0.peaks" not in survivors, "the least recently used survived"


# --------------------------------------------------------------------------- #
# When it cannot work
# --------------------------------------------------------------------------- #


def test_a_missing_recording_is_refused_with_the_path(tmp_path: Path) -> None:
    if ffmpeg is None:
        pytest.skip("ffmpeg is not present")
    with pytest.raises(WaveformError, match="not where"):
        extract_peaks(tmp_path / "gone.wav")


@needs_ffmpeg
def test_a_file_that_is_not_audio_is_refused_readably(tmp_path: Path) -> None:
    notes = tmp_path / "notes.txt"
    notes.write_text("not audio", encoding="utf-8")
    with pytest.raises(WaveformError) as raised:
        extract_peaks(notes)
    assert len(str(raised.value)) > 40, "a bare failure is not an explanation"


@needs_ffmpeg
def test_a_read_can_be_abandoned(recording: Path) -> None:
    """Opening another recording must not mean waiting for this one to finish."""
    from evidence_review.media.waveform import WaveformCancelled

    with pytest.raises(WaveformCancelled):
        extract_peaks(recording, should_stop=lambda: True)


@pytest.fixture(scope="module")
def high_frequency_recording(tmp_path_factory) -> Path:
    """Near-silence with a quiet 6 kHz event in it.

    Above the 4 kHz ceiling an 8 kHz decode leaves, which is the whole reason the
    envelope is read at 22 kHz. Measured before choosing: at 8 kHz this event stands
    out about five times above the noise floor, at 22 kHz about seventeen.
    """
    if ffmpeg is None:
        pytest.skip("ffmpeg is not present")
    target = tmp_path_factory.mktemp("hf") / "whisper.wav"
    subprocess.run(
        [
            str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "anoisesrc=d=30:c=pink:a=0.004:r=44100",
            "-f", "lavfi", "-i", "sine=frequency=6000:duration=30:sample_rate=44100",
            "-filter_complex",
            "[1:a]volume=0.25,atrim=0:2,adelay=15000|15000[hf];"
            "[0:a][hf]amix=inputs=2:normalize=0,atrim=0:30",
            "-c:a", "pcm_s16le", str(target),
        ],
        check=True, capture_output=True,
    )
    return target


@needs_ffmpeg
def test_a_quiet_high_frequency_event_is_not_decoded_away(
    high_frequency_recording: Path,
) -> None:
    """The reason the decode rate is 22 kHz and not something cheaper.

    Downsampling low-passes. A 6 kHz whisper read at 8 kHz is attenuated to barely
    above the noise, and the waveform shows a flat line exactly where the one
    interesting moment in the recording is. Nothing fails; the event is simply not
    there to be seen.
    """
    wave = extract_peaks(high_frequency_recording)
    event = level_at(wave, 15.5)
    background = level_at(wave, 5.0)

    assert event > background * 8, (
        f"the 6 kHz event is only {event / max(1, background):.1f}x the noise floor; "
        "it has been low-passed away by too cheap a decode"
    )


def test_the_envelope_is_actually_read_through_the_filters(tmp_path) -> None:
    """Not that the plumbing was called -- that the picture genuinely changes.

    Everything else about this feature can be satisfied by bookkeeping: the chain is
    recorded, the badge goes up, the cache gets its own key. If the filters never
    reached ffmpeg the drawn shape would be identical to the raw one and labelled as
    processed -- a picture of one thing captioned as another, which is the exact
    failure the badge exists to prevent.

    So this measures it. A recording of nothing but 50 Hz mains hum, read through the
    hum removal, has to come out quieter than the same recording read straight.
    """
    import subprocess

    from evidence_review.media.clip import find_ffmpeg
    from evidence_review.media.waveform import extract_peaks

    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        pytest.skip("ffmpeg is not present")

    hum = tmp_path / "hum.wav"
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=50:duration=4:sample_rate=44100",
         "-c:a", "pcm_s16le", str(hum)],
        check=True, capture_output=True,
    )

    raw = extract_peaks(hum)
    cleaned = extract_peaks(hum, audio_filters="bandreject=f=50:width_type=h:w=6")

    # Measured over the last second, not the whole file: a notch filter rings for a
    # moment when it starts, and that transient is louder than anything after it. Taking
    # the file peak would be measuring the filter settling rather than its effect.
    def steady(wave) -> int:
        tail = wave.peaks[-2 * wave.peaks_per_second :]
        return max(abs(value) for value in tail)

    assert steady(raw) > 0, "the fixture is silent"
    assert steady(cleaned) < steady(raw) * 0.1, (
        f"the hum survived the filter: {steady(raw)} -> {steady(cleaned)}; "
        "the chain is not reaching ffmpeg, so the picture would be raw but badged"
    )
