"""File list for the folder under review."""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..util import ALL_MEDIA_EXTENSIONS, classify_media, humanise_bytes

log = logging.getLogger(__name__)

# Monochrome glyphs the default Windows UI font contains; the film/speaker/
# picture emoji are not in it and fell back to colour emoji.
KIND_GLYPHS = {"video": "▶", "audio": "♫", "image": "▣"}
FALLBACK_GLYPH = "•"
#: A file that would not play. Marked in the list rather than only in the log,
#: because an unplayable file is one that was never reviewed.
FAILED_GLYPH = "✕"
FAILED_COLOUR = "#e5484d"

#: Ceilings so pointing at a huge drive cannot hang the UI. The scan runs on the
#: UI thread, so the number of directory entries walked has to be bounded too.
MAX_FILES = 20_000
MAX_SCANNED_ENTRIES = 200_000


class PlaylistDock(QDockWidget):
    """Lists the reviewable media in a folder and drives next/previous navigation."""

    file_activated = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Files", parent)
        self.setObjectName("playlistDock")
        self.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )

        self._root: Path | None = None
        self._paths: list[Path] = []
        #: Set when a scan was cut short or failed, so the list is not the whole folder.
        self._scan_warning: str | None = None
        #: Files that would not play this session, by path, with the reason. A
        #: status-bar message that scrolls away after eight seconds is no use when
        #: the question - which of these 200 files could I not review? - is asked
        #: at the end rather than at the time.
        self._failures: dict[str, str] = {}

        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        self.folder_label = QLabel("No folder open")
        self.folder_label.setObjectName("hintLabel")
        self.folder_label.setWordWrap(True)
        layout.addWidget(self.folder_label)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter files…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)
        controls.addWidget(self.filter_edit, 1)
        layout.addLayout(controls)

        self.recursive_check = QCheckBox("Include subfolders")
        self.recursive_check.setChecked(True)
        self.recursive_check.toggled.connect(self._reload)
        layout.addWidget(self.recursive_check)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_widget.setAlternatingRowColors(True)
        self.list_widget.itemActivated.connect(self._on_item_activated)
        self.list_widget.itemClicked.connect(self._on_item_activated)
        layout.addWidget(self.list_widget, 1)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("hintLabel")
        layout.addWidget(self.summary_label)

        # Hidden unless the scan was cut short or failed. Amber rather than red:
        # what is listed is accurate, it is just not everything.
        self.scan_warning_label = QLabel("")
        self.scan_warning_label.setWordWrap(True)
        self.scan_warning_label.setObjectName("warningLabel")
        self.scan_warning_label.hide()
        layout.addWidget(self.scan_warning_label)

        self.setWidget(container)

    # -- loading -------------------------------------------------------------- #

    def open_folder(self, folder: str | Path) -> int:
        """Scan ``folder`` for media. Returns the number of files found."""
        self._root = Path(folder)
        return self._reload()

    def _reload(self) -> int:
        if self._root is None or not self._root.is_dir():
            return 0

        self.folder_label.setText(str(self._root))
        pattern = "**/*" if self.recursive_check.isChecked() else "*"

        found: list[Path] = []
        examined = 0
        # A partial list that looks complete is the worst outcome here: a file that
        # was never listed is a file that was never reviewed, and nothing on screen
        # used to admit either the ceiling or a failed walk.
        self._scan_warning = None
        try:
            for candidate in self._root.glob(pattern):
                # Count every entry walked, not just the matches. Counting matches
                # let a drive full of non-media files walk forever on the UI thread
                # while the ceiling was never reached.
                examined += 1
                if examined >= MAX_SCANNED_ENTRIES or len(found) >= MAX_FILES:
                    log.warning(
                        "Stopped scanning %s after %d entries (%d media files found)",
                        self._root,
                        examined,
                        len(found),
                    )
                    self._scan_warning = (
                        f"Stopped after {examined:,} items — this folder is too large to "
                        f"list in full. Open a subfolder to see the rest."
                    )
                    break
                if candidate.is_file() and candidate.suffix.lower() in ALL_MEDIA_EXTENSIONS:
                    found.append(candidate)
        except OSError as exc:
            log.exception("Could not scan %s", self._root)
            self._scan_warning = f"This folder could not be read in full — {exc.strerror or exc}."

        self._paths = sorted(found, key=lambda p: (str(p.parent).lower(), p.name.lower()))
        self._rebuild_list()
        return len(self._paths)

    def mark_failed(self, path, reason: str) -> None:
        """Record that this file would not play, and show it in the list."""
        self._failures[str(Path(path))] = reason
        self._rebuild_list()

    def _rebuild_list(self) -> None:
        self.list_widget.clear()
        total_bytes = 0
        for path in self._paths:
            kind = classify_media(path) or "video"
            failure = self._failures.get(str(path))
            glyph = FAILED_GLYPH if failure else KIND_GLYPHS.get(kind, FALLBACK_GLYPH)
            item = QListWidgetItem(f"{glyph}  {path.name}")
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            try:
                size = path.stat().st_size
                total_bytes += size
                relative = path.relative_to(self._root) if self._root else path
                tooltip = f"{relative}\n{humanise_bytes(size)}"
            except (OSError, ValueError):
                tooltip = str(path)
            if failure:
                item.setForeground(QColor(FAILED_COLOUR))
                tooltip = f"{tooltip}\n\nWould not play: {failure}"
            item.setToolTip(tooltip)
            self.list_widget.addItem(item)

        self._total_bytes = total_bytes
        self._refresh_scan_warning()
        self._apply_filter(self.filter_edit.text())

    def _summary_text(self, visible: int | None = None) -> str:
        """One place decides what the summary says.

        _rebuild_list used to write a rich summary and then _apply_filter
        immediately overwrote it with a plainer one, so the size - and later the
        count of files that would not play - never survived to the screen.
        """
        count = len(self._paths)
        if visible is not None:
            return f"{visible} of {count} shown"
        if not count:
            if self._root is None:
                return ""
            # "0 files - 0 B" told the reviewer nothing about why, with the answer
            # - the subfolders box - sitting directly above it.
            return "No media here." + (
                "" if self.recursive_check.isChecked() else " Try including subfolders."
            )

        parts = [
            f"{count} file{'' if count == 1 else 's'}",
            humanise_bytes(getattr(self, "_total_bytes", 0)),
        ]
        broken = sum(1 for path in self._paths if str(path) in self._failures)
        if broken:
            parts.append(f"{broken} would not play")
        return "  ·  ".join(parts)

    def _refresh_scan_warning(self) -> None:
        warning = getattr(self, "_scan_warning", None)
        self.scan_warning_label.setText(warning or "")
        self.scan_warning_label.setVisible(bool(warning))

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        visible = 0
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            matches = not needle or needle in item.text().lower()
            item.setHidden(not matches)
            visible += int(matches)
        self.summary_label.setText(self._summary_text(visible if needle else None))

    # -- navigation ----------------------------------------------------------- #

    def select_path(self, path: str | Path) -> None:
        """Highlight ``path`` without re-emitting activation."""
        target = str(Path(path))
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == target:
                self.list_widget.blockSignals(True)
                self.list_widget.setCurrentRow(row)
                self.list_widget.blockSignals(False)
                self.list_widget.scrollToItem(item)
                return

    def _visible_rows(self) -> list[int]:
        return [
            row
            for row in range(self.list_widget.count())
            if not self.list_widget.item(row).isHidden()
        ]

    def step(self, offset: int) -> str | None:
        """Move ``offset`` places through the visible list and return the new path."""
        rows = self._visible_rows()
        if not rows:
            return None
        current = self.list_widget.currentRow()
        try:
            index = rows.index(current)
        except ValueError:
            index = -1 if offset > 0 else len(rows)
        target = index + offset
        if not (0 <= target < len(rows)):
            return None
        row = rows[target]
        self.list_widget.setCurrentRow(row)
        path = self.list_widget.item(row).data(Qt.ItemDataRole.UserRole)
        self.file_activated.emit(path)
        return path

    def current_path(self) -> str | None:
        item = self.list_widget.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        self.file_activated.emit(item.data(Qt.ItemDataRole.UserRole))
