"""Marking up the frame an event happens on.

Opens on the frame at the event's own moment, with whatever marks the entry already
carries. Nothing is written to the recording: the marks are saved against the entry, and
only an export that is explicitly asked to burn them in ever draws them into a file.

The time window at the bottom is the part that is easy to overlook and worth getting
right. A mark belongs to a span of the recording, not to the whole of it -- a circle
around somebody who walks out of shot after two seconds should come off when they do.
It defaults to the event's own span, which is the answer nine times out of ten.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..annotations import PALETTE, Annotation, AnnotationKind
from ..models import EntryRow
from ..util import format_timecode
from .annotation_canvas import AnnotationCanvas
from .widgets import TimecodeEdit, field_label, fit_to_screen, hint_label

log = logging.getLogger(__name__)

#: The order the tools appear in. Select first, because the thing a reviewer does most
#: often on reopening is adjust what is already there.
TOOLS: tuple[tuple[str, AnnotationKind | None], ...] = (
    ("Select", None),
    ("Circle", AnnotationKind.ELLIPSE),
    ("Box", AnnotationKind.RECTANGLE),
    ("Arrow", AnnotationKind.ARROW),
    ("Line", AnnotationKind.LINE),
    ("Text", AnnotationKind.TEXT),
)


class AnnotationEditor(QDialog):
    """Draw text, arrows, circles and boxes over one event's frame."""

    def __init__(
        self,
        entry: EntryRow,
        *,
        frame: QImage | None = None,
        annotations: list[Annotation] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._entry = entry
        self._saved: list[Annotation] | None = None

        start = entry.event_offset_seconds or 0.0
        self._default_window = (
            start,
            start + (entry.event_duration_seconds or 0.0),
        )

        self.setWindowTitle("Annotate")
        self.setModal(True)
        self.resize(1040, 700)

        self._build_ui()
        self.canvas.set_frame(frame or QImage())
        self.canvas.set_annotations(annotations or [])
        self._refresh_list()
        self._show_selection(None)
        fit_to_screen(self)

    # ------------------------------------------------------------------ #

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        heading = QLabel(self._entry.file_name)
        heading.setStyleSheet("font-size:14px; font-weight:600;")
        outer.addWidget(heading)
        outer.addWidget(
            hint_label(
                "Marks are saved with the entry. The recording is never changed — they "
                "are only drawn into a file if you ask for that when exporting."
            )
        )

        body = QHBoxLayout()
        body.setSpacing(12)

        # -- the drawing surface ------------------------------------------- #
        left = QVBoxLayout()
        left.setSpacing(8)
        left.addLayout(self._build_toolbar())

        self.canvas = AnnotationCanvas()
        self.canvas.annotations_changed.connect(self._on_canvas_changed)
        self.canvas.selection_changed.connect(self._show_selection)
        left.addWidget(self.canvas, 1)
        body.addLayout(left, 1)

        # -- the side panel ------------------------------------------------ #
        body.addWidget(self._build_side_panel())
        outer.addLayout(body, 1)

        # -- buttons -------------------------------------------------------- #
        buttons = QHBoxLayout()
        self.count_label = hint_label("")
        buttons.addWidget(self.count_label)
        buttons.addStretch(1)

        clear = QPushButton("Remove all")
        clear.setToolTip("Take every mark off this entry")
        clear.clicked.connect(self._remove_all)
        buttons.addWidget(clear)

        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)

        save = QPushButton("Save marks")
        save.setDefault(True)
        save.clicked.connect(self._save)
        buttons.addWidget(save)
        outer.addLayout(buttons)

    def _build_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(4)
        self._tool_group = QButtonGroup(self)
        self._tool_group.setExclusive(True)

        for index, (label, kind) in enumerate(TOOLS):
            button = QToolButton()
            button.setText(label)
            button.setCheckable(True)
            button.setChecked(index == 0)
            button.setToolTip(
                "Click a mark to select it, then drag to move or use the corners to resize"
                if kind is None
                else f"Drag on the picture to draw a {label.lower()}"
            )
            button.clicked.connect(lambda _c=False, k=kind: self.canvas.set_tool(k))
            self._tool_group.addButton(button, index)
            bar.addWidget(button)

        bar.addSpacing(12)

        bar.addWidget(field_label("Colour"))
        self.colour_combo = QComboBox()
        for name, value in PALETTE:
            self.colour_combo.addItem(self._swatch(value), name, value)
        self.colour_combo.currentIndexChanged.connect(
            lambda _i: self.canvas.set_style(colour=self.colour_combo.currentData())
        )
        bar.addWidget(self.colour_combo)

        bar.addWidget(field_label("Thickness"))
        self.stroke_spin = QDoubleSpinBox()
        self.stroke_spin.setRange(0.1, 5.0)
        self.stroke_spin.setSingleStep(0.1)
        self.stroke_spin.setValue(0.5)
        self.stroke_spin.setSuffix(" %")
        self.stroke_spin.setToolTip(
            "Line thickness as a percentage of the picture height, so it looks the same "
            "whatever the recording's resolution"
        )
        self.stroke_spin.valueChanged.connect(
            lambda value: self.canvas.set_style(stroke=value / 100.0)
        )
        bar.addWidget(self.stroke_spin)

        bar.addStretch(1)
        return bar

    def _build_side_panel(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(290)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        layout.addWidget(field_label("Marks on this entry"))
        self.list_widget = QListWidget()
        self.list_widget.setMaximumHeight(150)
        self.list_widget.currentItemChanged.connect(self._on_list_selection)
        layout.addWidget(self.list_widget)

        layout.addWidget(field_label("Label"))
        self.text_edit = QPlainTextEdit()
        self.text_edit.setMaximumHeight(80)
        self.text_edit.setPlaceholderText("Words to show beside this mark")
        self.text_edit.textChanged.connect(self._on_text_changed)
        layout.addWidget(self.text_edit)

        layout.addWidget(field_label("On screen from"))
        self.from_edit = TimecodeEdit()
        self.from_edit.value_changed.connect(
            lambda value: self._on_window_changed(start=value)
        )
        layout.addWidget(self.from_edit)

        layout.addWidget(field_label("until"))
        self.until_edit = TimecodeEdit()
        self.until_edit.value_changed.connect(
            lambda value: self._on_window_changed(end=value)
        )
        layout.addWidget(self.until_edit)

        self.window_hint = hint_label("")
        self.window_hint.setWordWrap(True)
        layout.addWidget(self.window_hint)

        layout.addStretch(1)

        self.delete_button = QPushButton("Remove this mark")
        self.delete_button.clicked.connect(self.canvas.remove_selected)
        layout.addWidget(self.delete_button)
        return panel

    @staticmethod
    def _swatch(colour: str) -> QPixmap:
        pixmap = QPixmap(14, 14)
        pixmap.fill(QColor(colour))
        return pixmap

    # ------------------------------------------------------------------ #
    # Keeping the panel and the canvas in step
    # ------------------------------------------------------------------ #

    def _on_canvas_changed(self) -> None:
        self._refresh_list()
        self.count_label.setText(self._count_text())

    def _count_text(self) -> str:
        count = len(self.canvas.annotations())
        if not count:
            return "No marks yet — pick a tool and drag on the picture."
        return f"{count} mark{'s' if count != 1 else ''} on this entry"

    def _refresh_list(self) -> None:
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        selected = self.canvas.selected()
        for annotation in self.canvas.annotations():
            item = QListWidgetItem(annotation.summary)
            item.setData(Qt.ItemDataRole.UserRole, annotation.annotation_id)
            self.list_widget.addItem(item)
            if selected and annotation.annotation_id == selected.annotation_id:
                self.list_widget.setCurrentItem(item)
        self.list_widget.blockSignals(False)
        self.count_label.setText(self._count_text())

    def _on_list_selection(self, current: QListWidgetItem | None, _previous=None) -> None:
        if current is None:
            return
        self.canvas.select(current.data(Qt.ItemDataRole.UserRole))

    def _show_selection(self, annotation: Annotation | None) -> None:
        """Point the side panel at whatever is selected, without echoing back."""
        has = annotation is not None
        for widget in (self.text_edit, self.from_edit, self.until_edit, self.delete_button):
            widget.setEnabled(has)

        self.text_edit.blockSignals(True)
        self.from_edit.blockSignals(True)
        self.until_edit.blockSignals(True)
        try:
            self.text_edit.setPlainText(annotation.text if has else "")
            self.from_edit.set_seconds(annotation.start_seconds if has else None)
            self.until_edit.set_seconds(annotation.end_seconds if has else None)
        finally:
            self.text_edit.blockSignals(False)
            self.from_edit.blockSignals(False)
            self.until_edit.blockSignals(False)

        if has:
            self.stroke_spin.blockSignals(True)
            self.stroke_spin.setValue(annotation.stroke * 100.0)
            self.stroke_spin.blockSignals(False)
            index = self.colour_combo.findData(annotation.colour)
            if index >= 0:
                self.colour_combo.blockSignals(True)
                self.colour_combo.setCurrentIndex(index)
                self.colour_combo.blockSignals(False)

        self._refresh_window_hint(annotation)
        self._refresh_list()

    def _refresh_window_hint(self, annotation: Annotation | None) -> None:
        if annotation is None:
            self.window_hint.setText("")
            return
        first, last = sorted((annotation.start_seconds, annotation.end_seconds))
        if last - first < 0.001:
            self.window_hint.setText(
                "This mark is on screen for a single moment, so it will barely be seen. "
                "Give it an end time."
            )
            return
        self.window_hint.setText(
            f"Shown for {format_timecode(last - first)} of the recording "
            f"({format_timecode(first)} to {format_timecode(last)})."
        )

    def _on_text_changed(self) -> None:
        if self.canvas.selected() is not None:
            self.canvas.update_selected(text=self.text_edit.toPlainText())

    def _on_window_changed(self, *, start: float | None = None, end: float | None = None) -> None:
        annotation = self.canvas.selected()
        if annotation is None:
            return
        changes = {}
        if start is not None:
            changes["start_seconds"] = max(0.0, start)
        if end is not None:
            changes["end_seconds"] = max(0.0, end)
        if changes:
            self.canvas.update_selected(**changes)
            self._refresh_window_hint(self.canvas.selected())

    # ------------------------------------------------------------------ #

    def _remove_all(self) -> None:
        if not self.canvas.annotations():
            return
        answer = QMessageBox.question(
            self, "Remove every mark?",
            "Take all marks off this entry?\n\nThe recording is not affected.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer is QMessageBox.StandardButton.Yes:
            self.canvas.set_annotations([])
            self._on_canvas_changed()
            self._show_selection(None)

    def _save(self) -> None:
        """Give every mark a sensible window before saving it.

        A mark drawn without the reviewer touching the time fields would otherwise sit
        at 0..0 and never appear. Defaulting to the event's own span is what they meant.
        """
        start, end = self._default_window
        if end <= start:
            end = start + 5.0

        saved: list[Annotation] = []
        for annotation in self.canvas.annotations():
            if abs(annotation.end_seconds - annotation.start_seconds) < 0.001:
                annotation = annotation.moved_to(start_seconds=start, end_seconds=end)
            saved.append(annotation)
        self._saved = saved
        self.accept()

    @property
    def saved_annotations(self) -> list[Annotation] | None:
        """What to store, or None if the dialog was cancelled."""
        return self._saved
