"""Cleaning up a recording, and never losing track of having done it.

This is the feature with the most ways to be quietly wrong, and they are not bugs in the
ordinary sense. A filter that is not applied, a filter that is applied without the
reviewer realising, an entry that does not record what was being heard when it was
written -- in each case everything works and the record is worthless.

So the tests here are mostly about disclosure, and the ones about the filters themselves
measure the audio rather than inspecting the string that produced it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from evidence_review.media.audio_filters import (
    FILTERS,
    PRESETS,
    AudioChain,
    FilterKind,
)
from evidence_review.media.clip import ClipError, ClipMode, export_clip, find_ffmpeg, write_sidecar

ffmpeg = find_ffmpeg()

needs_ffmpeg = pytest.mark.skipif(
    ffmpeg is None, reason="ffmpeg is not present; run installer/fetch_ffmpeg.ps1"
)


# --------------------------------------------------------------------------- #
# The chain
# --------------------------------------------------------------------------- #


def test_nothing_is_applied_by_default() -> None:
    """An opened recording plays as recorded. Anything else would be a tool that
    quietly changes evidence the moment it is started."""
    chain = AudioChain()
    assert chain.to_spec() == ""
    assert chain.describe() == ""
    assert not chain.is_processing


def test_the_first_preset_is_the_recording_as_it_was() -> None:
    assert PRESETS[0].chain.to_spec() == ""
    assert not PRESETS[0].chain.is_processing


def test_bypass_really_applies_nothing() -> None:
    """The control a reviewer uses to check whether what they heard was real.

    If bypass merely dimmed the processing, or left one filter on, the comparison it
    exists for would be worthless.
    """
    chain = AudioChain().with_filter(FilterKind.DENOISE, 30).with_bypass(True)
    assert chain.to_spec() == ""
    assert chain.describe() == ""
    assert not chain.is_processing
    assert chain.settings, "the settings are kept so it can be switched back on"


def test_the_same_filters_always_produce_the_same_specification() -> None:
    """The specification goes into the record, so it has to be deterministic -- two
    entries made under identical settings must say identical things."""
    first = (
        AudioChain()
        .with_filter(FilterKind.DENOISE, 12)
        .with_filter(FilterKind.HIGH_PASS, 80)
    )
    second = (
        AudioChain()
        .with_filter(FilterKind.HIGH_PASS, 80)
        .with_filter(FilterKind.DENOISE, 12)
    )
    assert first.to_spec() == second.to_spec()


def test_a_chain_round_trips_through_its_specification() -> None:
    """How a stored entry is read back and shown in words."""
    chain = (
        AudioChain()
        .with_filter(FilterKind.HIGH_PASS, 120)
        .with_filter(FilterKind.DENOISE, 9)
        .with_filter(FilterKind.LEVEL, 6)
    )
    restored = AudioChain.from_spec(chain.to_spec())
    assert restored.to_spec() == chain.to_spec()
    assert restored.describe() == chain.describe()


def test_an_unrecognised_specification_is_not_fatal() -> None:
    """A chain written by a newer version must still leave the entry readable."""
    restored = AudioChain.from_spec("someneighbourfilter=3,highpass=f=99")
    assert restored.value(FilterKind.HIGH_PASS) == 99


def test_values_are_clamped_to_what_the_control_offers() -> None:
    chain = AudioChain().with_filter(FilterKind.DENOISE, 10_000)
    spec = next(f for f in FILTERS if f.kind is FilterKind.DENOISE)
    assert chain.value(FilterKind.DENOISE) == spec.maximum


def test_describing_a_chain_says_what_was_done_in_words() -> None:
    """The record is read by people, not by ffmpeg."""
    chain = (
        AudioChain()
        .with_filter(FilterKind.HIGH_PASS, 80)
        .with_filter(FilterKind.DENOISE, 12)
    )
    said = chain.describe()
    assert "80 Hz" in said
    assert "noise reduction 12 dB" in said
    assert "highpass" not in said, "that is the specification, not the description"


# --------------------------------------------------------------------------- #
# Do the filters do what their labels say?
# --------------------------------------------------------------------------- #


def energy_in_band(path: Path, low: float, high: float, chain: str = "") -> float:
    """Mean volume in one frequency band, measured by ffmpeg."""
    filters = [chain] if chain else []
    filters.append(
        f"highpass=f={low:g},highpass=f={low:g},lowpass=f={high:g},lowpass=f={high:g}"
    )
    filters.append("volumedetect")
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-nostdin", "-i", str(path),
         "-af", ",".join(filters), "-f", "null", "-"],
        capture_output=True, text=True,
    )
    match = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", result.stderr)
    return float(match.group(1)) if match else 0.0


def tone(tmp_path: Path, name: str, lavfi: str) -> Path:
    target = tmp_path / name
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", lavfi, "-t", "4", "-c:a", "pcm_s16le", str(target)],
        check=True, capture_output=True,
    )
    return target


@needs_ffmpeg
def test_the_hum_notch_removes_mains_hum(tmp_path: Path) -> None:
    source = tone(tmp_path, "hum.wav", "sine=frequency=50:sample_rate=44100:duration=4")
    chain = AudioChain().with_filter(FilterKind.HUM, 50)

    before = energy_in_band(source, 35, 70)
    after = energy_in_band(source, 35, 70, chain.to_spec())

    assert after < before - 15, f"{before:.1f} dB -> {after:.1f} dB"


@needs_ffmpeg
def test_the_high_pass_removes_rumble(tmp_path: Path) -> None:
    source = tone(tmp_path, "rumble.wav", "sine=frequency=35:sample_rate=44100:duration=4")
    chain = AudioChain().with_filter(FilterKind.HIGH_PASS, 160)

    before = energy_in_band(source, 20, 60)
    after = energy_in_band(source, 20, 60, chain.to_spec())

    assert after < before - 20, f"{before:.1f} dB -> {after:.1f} dB"


@needs_ffmpeg
def test_the_voice_preset_does_not_throw_away_the_voice(tmp_path: Path) -> None:
    """A cleanup that removed what somebody is listening for would be worse than none."""
    source = tone(tmp_path, "voice.wav", "sine=frequency=900:sample_rate=44100:duration=4")
    speech = next(p for p in PRESETS if p.name == "Speech")

    before = energy_in_band(source, 700, 1200)
    after = energy_in_band(source, 700, 1200, speech.chain.to_spec())

    assert after > before - 6, f"{before:.1f} dB -> {after:.1f} dB"


@needs_ffmpeg
def test_bringing_up_quiet_parts_makes_a_quiet_recording_audible(tmp_path: Path) -> None:
    source = tone(
        tmp_path, "quiet.wav",
        "sine=frequency=600:sample_rate=44100:duration=4,volume=0.02",
    )
    chain = AudioChain().with_filter(FilterKind.LEVEL, 15)

    before = energy_in_band(source, 400, 900)
    after = energy_in_band(source, 400, 900, chain.to_spec())

    assert after > before + 10, f"{before:.1f} dB -> {after:.1f} dB"


@needs_ffmpeg
@pytest.mark.parametrize("preset", PRESETS, ids=lambda p: p.name)
def test_every_preset_is_something_ffmpeg_will_accept(preset, tmp_path: Path) -> None:
    """A chain that is refused leaves the reviewer hearing the recording while the
    panel says otherwise, which is the state this feature must never produce."""
    spec = preset.chain.to_spec()
    if not spec:
        return
    source = tone(tmp_path, "t.wav", "sine=frequency=440:sample_rate=44100:duration=1")
    result = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-nostdin", "-v", "error",
         "-i", str(source), "-af", spec, "-t", "1", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr.strip()[:200]


# --------------------------------------------------------------------------- #
# The export
# --------------------------------------------------------------------------- #


@pytest.fixture
def hum_recording(tmp_path: Path) -> Path:
    if ffmpeg is None:
        pytest.skip("ffmpeg is not present")
    target = tmp_path / "session.wav"
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=50:sample_rate=44100:duration=20",
         "-f", "lavfi", "-i", "sine=frequency=800:sample_rate=44100:duration=20",
         "-filter_complex", "[1:a]volume=0.4[v];[0:a][v]amix=inputs=2:normalize=0",
         "-c:a", "pcm_s16le", str(target)],
        check=True, capture_output=True,
    )
    return target


@needs_ffmpeg
def test_an_export_without_cleanup_carries_the_recordings_own_audio(
    hum_recording: Path, tmp_path: Path
) -> None:
    target = tmp_path / "plain.mkv"
    result = export_clip(
        source=hum_recording, target=target, start_seconds=2.0, duration_seconds=5.0,
        mode=ClipMode.EXACT,
    )
    assert result.audio_filters == ""
    assert energy_in_band(target, 35, 70) > energy_in_band(hum_recording, 35, 70) - 6


@needs_ffmpeg
def test_cleanup_reaches_the_exported_audio(hum_recording: Path, tmp_path: Path) -> None:
    """Otherwise the reviewer sends out a clip they believe is cleaned and is not."""
    chain = AudioChain().with_filter(FilterKind.HUM, 50)
    target = tmp_path / "cleaned.mkv"

    result = export_clip(
        source=hum_recording, target=target, start_seconds=2.0, duration_seconds=5.0,
        mode=ClipMode.EXACT, audio_filters=chain.to_spec(),
    )

    assert result.audio_filters == chain.to_spec()
    before = energy_in_band(hum_recording, 35, 70)
    after = energy_in_band(target, 35, 70)
    assert after < before - 15, f"{before:.1f} dB -> {after:.1f} dB"


@needs_ffmpeg
def test_cleaning_a_stream_copy_is_refused_not_quietly_dropped(
    hum_recording: Path, tmp_path: Path
) -> None:
    """Copying the recorded audio and filtering it are incompatible. Somebody who
    asked for both and silently got one would not know which."""
    with pytest.raises(ClipError, match="cannot be applied"):
        export_clip(
            source=hum_recording, target=tmp_path / "x.mkv", start_seconds=2.0,
            duration_seconds=4.0, mode=ClipMode.ORIGINAL,
            audio_filters=AudioChain().with_filter(FilterKind.HUM, 50).to_spec(),
        )


# --------------------------------------------------------------------------- #
# Disclosure
# --------------------------------------------------------------------------- #


@needs_ffmpeg
def test_a_processed_clip_declares_it_unmissably(
    hum_recording: Path, tmp_path: Path
) -> None:
    """The point of the whole feature. A clip whose audio has been processed and whose
    record does not say so is the outcome all of this exists to prevent."""
    chain = AudioChain().with_filter(FilterKind.HUM, 50)
    result = export_clip(
        source=hum_recording, target=tmp_path / "cleaned.mkv", start_seconds=2.0,
        duration_seconds=4.0, mode=ClipMode.EXACT, audio_filters=chain.to_spec(),
    )

    text = write_sidecar(result).read_text(encoding="utf-8")

    assert "\nAudio\n" in text, "the section a reader scans for"
    assert "HAS BEEN PROCESSED" in text
    assert "not what the recorder" in text
    assert "50 Hz mains hum notched out" in text, "in words"
    assert chain.to_spec() in text, "and exactly, so it can be reproduced"
    assert "can create detail that was not recorded" in text, "the warning"


@needs_ffmpeg
def test_an_unprocessed_clip_says_so_under_the_same_heading(
    hum_recording: Path, tmp_path: Path
) -> None:
    """Stated either way, so a reader never has to notice an absent line."""
    result = export_clip(
        source=hum_recording, target=tmp_path / "plain.mkv", start_seconds=2.0,
        duration_seconds=4.0, mode=ClipMode.EXACT,
    )
    text = write_sidecar(result).read_text(encoding="utf-8")

    assert "\nAudio\n" in text
    assert "Unprocessed" in text
    assert "as it was recorded" in text


# --------------------------------------------------------------------------- #
# Found by adversarial review, 2026-10-04
# --------------------------------------------------------------------------- #


def test_the_cache_key_survives_a_restart(tmp_path) -> None:
    """It was built with the built-in hash(), which is randomised per process.

    The key differed on every launch, so the cache could never hit after a restart:
    every open re-read the whole recording and wrote another copy of the peaks that
    nothing would ever read. Nothing failed, and every test of the cache passed,
    because they all reopened the file inside one process.
    """
    import subprocess
    import sys

    media = tmp_path / "session.wav"
    media.write_bytes(b"x" * 2048)

    probe = (
        "import sys; sys.path.insert(0, r'{src}');"
        "from pathlib import Path;"
        "from evidence_review.media.waveform import cache_key;"
        "print(cache_key(Path(r'{media}')))"
    ).format(src=Path(__file__).resolve().parents[1] / "src", media=media)

    keys = {
        subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True
        ).stdout.strip()
        for _ in range(2)
    }

    assert len(keys) == 1, f"the key differs between processes: {keys}"
    assert "" not in keys


def test_slowing_down_is_refused_on_export_rather_than_cutting_the_event_short(
    tmp_path,
) -> None:
    """atempo stretches audio, and the output is limited to the event's real length.

    An eight-second event exported at half speed came out as eight seconds holding the
    first four, with the provenance file still stating the full span -- the clip cut off
    halfway through the thing it was made to show, and nothing failing.
    """
    source = tone(tmp_path, "s.wav", "sine=frequency=440:sample_rate=44100:duration=6")
    chain = AudioChain().with_filter(FilterKind.SLOW, 50)

    with pytest.raises(ClipError, match="listening aid"):
        export_clip(
            source=source, target=tmp_path / "out.mkv", start_seconds=1.0,
            duration_seconds=3.0, mode=ClipMode.EXACT,
            audio_filters=chain.to_spec(),
        )


def test_the_export_chain_leaves_the_tempo_change_out_and_keeps_the_rest() -> None:
    chain = (
        AudioChain()
        .with_filter(FilterKind.SLOW, 50)
        .with_filter(FilterKind.HIGH_PASS, 80)
        .with_filter(FilterKind.DENOISE, 10)
    )
    exported = chain.for_export()

    assert "atempo" not in exported.to_spec()
    assert "highpass" in exported.to_spec()
    assert "afftdn" in exported.to_spec()
    assert chain.slows_playback, "the listening chain still slows playback"


@needs_ffmpeg
def test_a_cleaned_export_still_contains_the_whole_event(tmp_path) -> None:
    """The symptom the tempo bug produced, guarded directly: a marker at the end of the
    event has to survive the export."""
    source = tmp_path / "marked.wav"
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "sine=frequency=300:duration=20:sample_rate=44100",
         "-f", "lavfi", "-i", "sine=frequency=2000:duration=20:sample_rate=44100",
         "-filter_complex",
         "[1:a]volume=0.9,atrim=0:2,adelay=8000|8000[m];"
         "[0:a][m]amix=inputs=2:normalize=0,atrim=0:20",
         "-c:a", "pcm_s16le", str(source)],
        check=True, capture_output=True,
    )

    def marker(path: Path) -> float:
        result = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-nostdin", "-i", str(path),
             "-af", "highpass=f=1500,highpass=f=1500,volumedetect", "-f", "null", "-"],
            capture_output=True, text=True,
        )
        found = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", result.stderr)
        return float(found.group(1)) if found else 0.0

    chain = (
        AudioChain()
        .with_filter(FilterKind.SLOW, 50)
        .with_filter(FilterKind.HIGH_PASS, 80)
    )
    target = tmp_path / "cleaned.mkv"
    export_clip(
        source=source, target=target, start_seconds=2.0, duration_seconds=8.0,
        mode=ClipMode.EXACT, audio_filters=chain.for_export().to_spec(),
    )

    assert marker(target) > -45.0, "the end of the event is missing from the clip"
