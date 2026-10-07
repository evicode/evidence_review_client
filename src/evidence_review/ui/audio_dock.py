"""Cleaning up a recording to listen to it.

The controls are the easy part. What this panel is really for is making sure nobody
forgets they are using them.

Noise reduction pushed hard invents detail, and what it invents can sound like speech.
That is the standing criticism of EVP as a method, and the honest answer to it is not to
refuse the tool -- a reviewer genuinely cannot hear a quiet voice under mains hum -- but
to make the state of the audio impossible to lose track of. So the panel is built around
three things that matter more than the filters:

* a banner that says, in words, what is being heard right now;
* a bypass that is the biggest control here and takes one key, because going back and
  forth with the recording is how a reviewer tells a real voice from an artefact;
* and the knowledge that whatever is set here gets written onto any entry logged while
  it is on.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..media.audio_filters import FILTERS, PRESETS, AudioChain, FilterKind
from . import theme
from .widgets import field_label, hint_label, scrollable

log = logging.getLogger(__name__)

#: Shown when the settings match none of the presets.
CUSTOM_PRESET = "Settings of your own"


class AudioDock(QDockWidget):
    """Live cleanup for a recording, and a running statement of what it is doing."""

    chain_changed = Signal(object)  # AudioChain

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Audio cleanup", parent)
        self.setObjectName("audioDock")
        self.setAllowedAreas(
            Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.LeftDockWidgetArea
        )

        self._chain = AudioChain()
        self._updating = False
        self._rows: dict[FilterKind, tuple[QCheckBox, QDoubleSpinBox]] = {}

        self.setWidget(self._build())
        # Narrower than this and the filter rows stop being readable -- the label and
        # its value are a single statement ("Remove mains hum at 50 Hz") and half of it
        # is no use. A saved layout from an earlier version can be narrower than this,
        # so it is a floor rather than a starting size.
        self.setMinimumWidth(300)
        self._refresh_banner()

    # ------------------------------------------------------------------ #

    def _build(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(10)

        # -- what is being heard, right now -------------------------------- #
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.setFrameShape(QFrame.Shape.StyledPanel)
        self.banner.setMargin(8)
        layout.addWidget(self.banner)

        self.bypass_button = QPushButton("Compare with the recording")
        self.bypass_button.setCheckable(True)
        self.bypass_button.setMinimumHeight(34)
        self.bypass_button.setToolTip(
            "Switch all processing off and on without losing the settings.\n"
            "Going back and forth is how you tell a real voice from something the "
            "noise reduction invented."
        )
        self.bypass_button.toggled.connect(self._on_bypass)
        layout.addWidget(self.bypass_button)

        layout.addWidget(field_label("Start from"))
        self.preset_combo = QComboBox()
        for preset in PRESETS:
            self.preset_combo.addItem(preset.name, preset)
        # Once a control is touched the settings no longer match the preset that was
        # picked, and a box still naming it is a label contradicting the state -- in
        # the one panel whose whole job is not misrepresenting the state. It read
        # "As recorded - No processing at all" with four filters running.
        self.preset_combo.addItem(CUSTOM_PRESET, None)
        self.preset_combo.currentIndexChanged.connect(self._on_preset)
        layout.addWidget(self.preset_combo)
        self.preset_hint = hint_label(PRESETS[0].description)
        self.preset_hint.setWordWrap(True)
        layout.addWidget(self.preset_hint)

        # -- the filters themselves ---------------------------------------- #
        inner = QWidget()
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(0, 0, 0, 0)
        inner_layout.setSpacing(12)

        for spec in FILTERS:
            row = QWidget()
            row_layout = QVBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(3)

            top = QHBoxLayout()
            top.setSpacing(6)
            box = QCheckBox(spec.label)
            box.toggled.connect(
                lambda on, kind=spec.kind: self._on_toggle(kind, on)
            )
            top.addWidget(box, 1)

            amount = QDoubleSpinBox()
            amount.setRange(spec.minimum, spec.maximum)
            amount.setSingleStep(spec.step)
            amount.setValue(spec.default)
            amount.setDecimals(0)
            amount.setSuffix(f" {spec.unit}")
            amount.setEnabled(False)
            amount.valueChanged.connect(
                lambda value, kind=spec.kind: self._on_amount(kind, value)
            )
            # The checkbox beside it takes the stretch, so without a floor the value
            # is what gets crushed when the dock is narrow: "8000 Hz" rendered as
            # "8000" and "50 Hz" as "5(". A cutoff frequency that cannot be read is
            # the one part of this row that has to be exact.
            amount.setMinimumWidth(amount.sizeHint().width())
            top.addWidget(amount)
            row_layout.addLayout(top)

            explanation = hint_label(spec.summary)
            explanation.setWordWrap(True)
            explanation.setToolTip(spec.explanation)
            box.setToolTip(spec.explanation)
            amount.setToolTip(spec.explanation)
            row_layout.addWidget(explanation)

            inner_layout.addWidget(row)
            self._rows[spec.kind] = (box, amount)

        inner_layout.addStretch(1)
        layout.addWidget(scrollable(inner), 1)

        reset = QPushButton("Back to the recording as it is")
        reset.setToolTip("Turn everything off")
        reset.clicked.connect(self._on_reset)
        layout.addWidget(reset)

        layout.addWidget(
            hint_label(
                "Processing affects what you hear, never the file. Whatever is on when "
                "you log an event is written onto that entry, so the record says what "
                "you were listening to."
            )
        )
        return page

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #

    def chain(self) -> AudioChain:
        return self._chain

    def _match_preset(self, chain: AudioChain) -> None:
        """Name the preset this chain is, or say it is a custom one."""
        wanted = chain.with_bypass(False).to_spec()
        for index in range(self.preset_combo.count()):
            preset = self.preset_combo.itemData(index)
            if preset is not None and preset.chain.to_spec() == wanted:
                self.preset_combo.setCurrentIndex(index)
                self.preset_hint.setText(preset.description)
                return
        self.preset_combo.setCurrentIndex(self.preset_combo.count() - 1)
        self.preset_hint.setText(
            "Settings of your own. Pick one above to start again from a known point."
        )

    def set_chain(self, chain: AudioChain) -> None:
        """Point every control at ``chain`` without echoing changes back out."""
        self._chain = chain
        self._updating = True
        try:
            for kind, (box, amount) in self._rows.items():
                value = chain.value(kind)
                box.setChecked(value is not None)
                amount.setEnabled(value is not None)
                if value is not None:
                    amount.setValue(value)
            self.bypass_button.setChecked(chain.bypassed)
            self._match_preset(chain)
        finally:
            self._updating = False
        self._refresh_banner()

    def _emit(self, chain: AudioChain) -> None:
        self._chain = chain
        self._updating = True
        try:
            self._match_preset(chain)
        finally:
            self._updating = False
        self._refresh_banner()
        self.chain_changed.emit(chain)

    # ------------------------------------------------------------------ #
    # The banner
    # ------------------------------------------------------------------ #

    def _refresh_banner(self) -> None:
        """Say what is being heard, in words, at all times.

        Never a bare "filters: on". Somebody coming back to a session after lunch has
        to be able to read what state the audio is in without opening anything.
        """
        chain = self._chain
        if chain.bypassed and chain.settings:
            self.banner.setText(
                "<b>Hearing the recording as it was.</b><br>"
                "Processing is set up but switched off."
            )
            self.banner.setStyleSheet(
                f"background:{theme.BG_ELEVATED}; color:{theme.TEXT}; "
                f"border:1px solid {theme.BORDER}; border-radius:4px;"
            )
            self.bypass_button.setText("Switch processing back on")
            return

        if not chain.is_processing:
            self.banner.setText("<b>Hearing the recording as it was.</b><br>Nothing applied.")
            self.banner.setStyleSheet(
                f"background:{theme.BG_ELEVATED}; color:{theme.TEXT_MUTED}; "
                f"border:1px solid {theme.BORDER}; border-radius:4px;"
            )
            self.bypass_button.setText("Compare with the recording")
            self.bypass_button.setEnabled(False)
            return

        self.bypass_button.setEnabled(True)
        self.bypass_button.setText("Compare with the recording")
        self.banner.setText(
            "<b>HEARING PROCESSED AUDIO</b><br>"
            f"{chain.describe()}.<br>"
            "This is not what the recorder captured."
        )
        # The warning amber, not the accent red: this is a state to be aware of, not a
        # fault. Red here would cry wolf on something reviewers do all day.
        self.banner.setStyleSheet(
            f"background:{theme.WARNING}; color:#1a1d21; "
            "border-radius:4px; font-size:12px;"
        )

    # ------------------------------------------------------------------ #
    # Controls
    # ------------------------------------------------------------------ #

    def _on_toggle(self, kind: FilterKind, on: bool) -> None:
        if self._updating:
            return
        _, amount = self._rows[kind]
        amount.setEnabled(on)
        self._emit(self._chain.with_filter(kind, amount.value() if on else None))

    def _on_amount(self, kind: FilterKind, value: float) -> None:
        if self._updating or not self._chain.enabled(kind):
            return
        self._emit(self._chain.with_filter(kind, value))

    def _on_bypass(self, bypassed: bool) -> None:
        if self._updating:
            return
        self._emit(self._chain.with_bypass(bypassed))

    def _on_preset(self, index: int) -> None:
        if self._updating:
            return
        preset = self.preset_combo.itemData(index)
        if preset is None:
            return
        self.preset_hint.setText(preset.description)
        chain = preset.chain.with_bypass(self._chain.bypassed)
        self.set_chain(chain)
        self.chain_changed.emit(chain)

    def _on_reset(self) -> None:
        self._updating = True
        try:
            self.preset_combo.setCurrentIndex(0)
        finally:
            self._updating = False
        self.set_chain(AudioChain())
        self.chain_changed.emit(self._chain)

    def toggle_bypass(self) -> None:
        """The keyboard route. Does nothing when there is nothing to compare against."""
        if not self._chain.settings:
            return
        self.bypass_button.setChecked(not self.bypass_button.isChecked())
