"""Cleaning up a recording to listen to it, without pretending that is the recording.

This is the most dangerous feature in the application, and the design is shaped by that
rather than by what is easy.

Noise reduction applied hard enough to a hiss will produce artefacts that sound like
whispered words. That is not a hypothetical risk; it is the central methodological
criticism of EVP, and a tool that makes it easy to do and hard to notice would be
actively harmful to the people using it. What makes processing defensible is not
avoiding it -- a reviewer genuinely cannot hear a quiet voice under mains hum -- but
never losing track of what was done.

So:

* **Nothing is applied by default.** An opened recording plays as recorded.
* **Processing is a view, never an edit.** It is applied live by the player; the file on
  disk is never written to.
* **It can be taken off instantly**, so the reviewer can go back and forth between what
  was recorded and what they are hearing. A filtered artefact rarely survives that.
* **What was applied is recorded on the entry.** An observation made while listening
  through 20 dB of noise reduction is a different claim from one made on the raw
  recording, and the record has to say which.
* **An export that was processed says so**, in its provenance file, in capitals.

The chain is stored as its canonical ffmpeg specification -- exact and reproducible by
anybody with ffmpeg -- and shown through :meth:`AudioChain.describe`, which puts it in
words a reader of the record will understand.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class FilterKind(str, Enum):
    """The cleanups worth offering, in the order they should be applied.

    Order matters: cutting rumble before denoising means the denoiser is not spending
    its estimate on energy that is about to be thrown away.
    """

    HIGH_PASS = "high_pass"
    LOW_PASS = "low_pass"
    HUM = "hum"
    DENOISE = "denoise"
    LEVEL = "level"
    SLOW = "slow"


@dataclass(frozen=True, slots=True)
class FilterSpec:
    """One cleanup, its parameter, and what it does in plain words."""

    kind: FilterKind
    label: str
    #: What the number means, for the control's suffix.
    unit: str
    minimum: float
    maximum: float
    default: float
    step: float
    #: One line under the control. The panel is a working surface, not a manual: the
    #: full explanation moved to a tooltip once six controls with four lines of prose
    #: each pushed half of them off the bottom of the panel.
    summary: str
    #: The whole story, on hover.
    explanation: str

    def clamp(self, value: float) -> float:
        return max(self.minimum, min(self.maximum, float(value)))


#: Declared once, in application order.
FILTERS: tuple[FilterSpec, ...] = (
    FilterSpec(
        FilterKind.HIGH_PASS,
        "Cut rumble below",
        "Hz",
        20,
        500,
        80,
        10,
        "Handling noise, footsteps, wind.",
        "Rolls off handling noise, footsteps and air movement at 12 dB per octave. "
        "Little of a human voice lives below 80 Hz, so this is usually safe.",
    ),
    FilterSpec(
        FilterKind.LOW_PASS,
        "Cut hiss above",
        "Hz",
        1000,
        16000,
        8000,
        250,
        "Tape and preamp hiss.",
        "Rolls off tape and preamp hiss at 12 dB per octave — a gentle slope "
        "rather than a wall, because a steep filter rings and ringing is detail "
        "that was never recorded. Speech is mostly below 8 kHz, but consonants "
        "reach higher, so cutting too low makes everything sound like a whisper.",
    ),
    FilterSpec(
        FilterKind.HUM,
        "Remove mains hum at",
        "Hz",
        50,
        60,
        50,
        10,
        "50 Hz in Europe, 60 Hz in North America.",
        "Notches out mains hum and its first harmonics. Use 50 Hz in the UK and "
        "Europe, 60 Hz in North America.",
    ),
    FilterSpec(
        FilterKind.DENOISE,
        "Noise reduction",
        "dB",
        1,
        40,
        12,
        1,
        "Careful: pushed hard this invents detail.",
        "Subtracts a steady noise floor. THE ONE TO BE CAREFUL WITH: pushed hard it "
        "invents detail, and what it invents can sound like speech. Keep it as low as "
        "will do, and check against the unprocessed recording before concluding "
        "anything.",
    ),
    FilterSpec(
        FilterKind.LEVEL,
        "Bring up quiet parts",
        "x",
        1,
        30,
        10,
        1,
        "Makes a faint voice audible.",
        "Evens out the level so a faint voice is audible without the loud parts "
        "becoming painful. Raises the noise floor along with everything else.",
    ),
    FilterSpec(
        FilterKind.SLOW,
        "Play slower",
        "%",
        25,
        100,
        75,
        5,
        "Listening only — never exported.",
        "Slows playback without changing pitch, which can make a rushed or slurred "
        "phrase easier to make out.",
    ),
)

FILTERS_BY_KIND = {spec.kind: spec for spec in FILTERS}


@dataclass(frozen=True, slots=True)
class AudioChain:
    """Which cleanups are on, and at what settings.

    Immutable: every change produces a new chain, so the one recorded against an entry
    cannot be altered afterwards by the reviewer carrying on turning knobs.
    """

    settings: tuple[tuple[FilterKind, float], ...] = ()
    #: Switched off wholesale without losing the settings, for instant comparison with
    #: the recording. This is the single most important control here.
    bypassed: bool = False

    # ------------------------------------------------------------------ #

    @property
    def is_processing(self) -> bool:
        """Is the reviewer hearing something other than the recording?"""
        return bool(self.settings) and not self.bypassed

    def value(self, kind: FilterKind) -> float | None:
        for active, amount in self.settings:
            if active is kind:
                return amount
        return None

    def enabled(self, kind: FilterKind) -> bool:
        return self.value(kind) is not None

    def with_filter(self, kind: FilterKind, amount: float | None) -> AudioChain:
        """Turn one cleanup on at ``amount``, or off with ``None``."""
        kept = tuple((k, v) for k, v in self.settings if k is not kind)
        if amount is None:
            return replace(self, settings=kept)
        spec = FILTERS_BY_KIND[kind]
        updated = (*kept, (kind, spec.clamp(amount)))
        # Kept in declared order so the chain is deterministic: the same set of
        # cleanups always produces the same specification, and so the same record.
        order = {spec.kind: index for index, spec in enumerate(FILTERS)}
        return replace(self, settings=tuple(sorted(updated, key=lambda p: order[p[0]])))

    def with_bypass(self, bypassed: bool) -> AudioChain:
        return replace(self, bypassed=bypassed)

    def cleared(self) -> AudioChain:
        return AudioChain()

    # ------------------------------------------------------------------ #

    def without_time_changes(self) -> AudioChain:
        """The chain minus anything that stretches or compresses time.

        Two things need this. An export cut to the length of its event would hold only
        part of it if the audio were slowed. And the waveform is drawn against the
        recording's own clock, so an envelope read through a tempo change would put
        every peak at the wrong moment -- the picture and the playhead would disagree,
        and a span dragged on it would mark the wrong part of the recording.
        """
        return replace(
            self, settings=tuple((k, v) for k, v in self.settings if k is not FilterKind.SLOW)
        )

    def for_export(self) -> AudioChain:
        """The part of this chain that belongs in an exported file.

        The tempo change is left out, and deliberately.

        It stretches audio in time, so a clip cut to the length of its event would hold
        only the first part of it, slowed -- an eight-second event exported at half
        speed came out as eight seconds containing the first four, with the provenance
        file still stating the full span. It also breaks every timing figure in that
        file: the lead-in and the length are measured against the recording, and at half
        speed they no longer describe the clip.

        Slowing down is for listening. An export is for somebody else to examine, and it
        should hold the event at the speed it happened.
        """
        return self.without_time_changes()

    @property
    def slows_playback(self) -> bool:
        return self.enabled(FilterKind.SLOW) and not self.bypassed

    def to_spec(self) -> str:
        """The ffmpeg filter chain, or "" for the recording as it is.

        Exact and reproducible: anybody with ffmpeg can apply this string to the source
        and get what the reviewer heard. That is why it, rather than a description, is
        what gets stored.
        """
        if self.bypassed:
            return ""
        parts: list[str] = []
        for kind, amount in self.settings:
            if kind is FilterKind.HIGH_PASS:
                parts.append(f"highpass=f={amount:g}")
            elif kind is FilterKind.LOW_PASS:
                parts.append(f"lowpass=f={amount:g}")
            elif kind is FilterKind.HUM:
                # The fundamental and two harmonics: hum is never only the fundamental.
                for harmonic in (1, 2, 3):
                    parts.append(f"bandreject=f={amount * harmonic:g}:width_type=h:w=4")
            elif kind is FilterKind.DENOISE:
                parts.append(f"afftdn=nr={amount:g}:nf=-40")
            elif kind is FilterKind.LEVEL:
                parts.append(f"dynaudnorm=f=250:g=15:p=0.9:m={amount:g}")
            elif kind is FilterKind.SLOW:
                parts.append(f"atempo={max(0.5, amount / 100):g}")
        return ",".join(parts)

    def describe(self) -> str:
        """What was applied, in words, for somebody reading the record.

        Returns "" when nothing was, which is what the record should say for an
        observation made on the recording as it was given.
        """
        if self.bypassed or not self.settings:
            return ""
        said: list[str] = []
        for kind, amount in self.settings:
            if kind is FilterKind.HIGH_PASS:
                said.append(f"rumble below {amount:g} Hz cut")
            elif kind is FilterKind.LOW_PASS:
                said.append(f"hiss above {amount:g} Hz cut")
            elif kind is FilterKind.HUM:
                said.append(f"{amount:g} Hz mains hum notched out")
            elif kind is FilterKind.DENOISE:
                said.append(f"noise reduction {amount:g} dB")
            elif kind is FilterKind.LEVEL:
                said.append(f"quiet parts brought up {amount:g}x")
            elif kind is FilterKind.SLOW:
                said.append(f"played at {amount:g}% speed")
        return "; ".join(said)

    # ------------------------------------------------------------------ #

    @classmethod
    def from_spec(cls, spec: str) -> AudioChain:
        """Rebuild a chain from a stored specification.

        Anything unrecognised is ignored rather than raising: a chain written by a
        newer version has to leave this one able to open the entry.
        """
        if not spec or not spec.strip():
            return cls()
        chain = cls()
        seen_hum = False
        for part in spec.split(","):
            name, _, argument = part.partition("=")
            name = name.strip()
            try:
                if name == "highpass":
                    chain = chain.with_filter(FilterKind.HIGH_PASS, _number(argument))
                elif name == "lowpass":
                    chain = chain.with_filter(FilterKind.LOW_PASS, _number(argument))
                elif name == "bandreject" and not seen_hum:
                    # Only the fundamental; the harmonics are derived from it.
                    seen_hum = True
                    chain = chain.with_filter(FilterKind.HUM, _number(argument))
                elif name == "afftdn":
                    chain = chain.with_filter(FilterKind.DENOISE, _number(argument, "nr"))
                elif name == "dynaudnorm":
                    chain = chain.with_filter(FilterKind.LEVEL, _number(argument, "m"))
                elif name == "atempo":
                    chain = chain.with_filter(FilterKind.SLOW, _number(argument) * 100)
            except (TypeError, ValueError):
                continue
        return chain


def _number(argument: str, key: str | None = None) -> float:
    """Pull a number out of an ffmpeg argument list like ``nr=12:nf=-40``."""
    for piece in argument.split(":"):
        name, _, value = piece.partition("=")
        if key is None:
            return float(value or name)
        if name == key:
            return float(value)
    raise ValueError(f"no {key} in {argument!r}")


# --------------------------------------------------------------------------- #
# Presets
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Preset:
    name: str
    description: str
    chain: AudioChain


def _chain(*pairs: tuple[FilterKind, float]) -> AudioChain:
    chain = AudioChain()
    for kind, amount in pairs:
        chain = chain.with_filter(kind, amount)
    return chain


#: Starting points, because a reviewer wants "clean up this hiss", not "afftdn=nr=12".
#:
#: The first is deliberately first and deliberately empty: the recording as it was
#: given is the default, and going back to it is one click.
PRESETS: tuple[Preset, ...] = (
    Preset(
        "As recorded",
        "No processing at all. What the recorder captured.",
        AudioChain(),
    ),
    Preset(
        "Speech",
        "Narrows to the range a human voice occupies and evens out the level. "
        "A reasonable first try on almost anything.",
        _chain(
            (FilterKind.HIGH_PASS, 150),
            (FilterKind.LOW_PASS, 7000),
            (FilterKind.LEVEL, 10),
        ),
    ),
    Preset(
        "Hiss",
        "For a noisy recorder or a high gain setting. Starts gently — turn the "
        "reduction up only as far as it needs.",
        _chain(
            (FilterKind.HIGH_PASS, 100),
            (FilterKind.DENOISE, 10),
            (FilterKind.LEVEL, 8),
        ),
    ),
    Preset(
        "Mains hum",
        "For a recording made near wiring or a transformer.",
        _chain((FilterKind.HUM, 50), (FilterKind.HIGH_PASS, 80)),
    ),
    Preset(
        "Rumble",
        "For handling noise, wind or footsteps.",
        _chain((FilterKind.HIGH_PASS, 160)),
    ),
)


__all__ = [
    "FILTERS",
    "FILTERS_BY_KIND",
    "PRESETS",
    "AudioChain",
    "FilterKind",
    "FilterSpec",
    "Preset",
]
