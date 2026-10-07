"""Application styling. A dark theme is the default because reviewers spend hours
looking at dark footage and a bright chrome washes out shadow detail."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

ACCENT = "#e5484d"  # the LOG action; deliberately the loudest thing on screen
ACCENT_HOVER = "#f2555a"
ACCENT_PRESSED = "#c93c41"

BG_BASE = "#16181b"
BG_PANEL = "#1e2125"
#: Darker than the window: the video letterbox and the snapshot well, which read
#: as holes in the surface rather than panels on it.
BG_SUNKEN = "#0e1013"
BG_ELEVATED = "#262a2f"
BORDER = "#343a41"
TEXT = "#e6e8ea"
TEXT_MUTED = "#9aa3ad"
#: Amber, for something that will work but is a poor idea.
WARNING = "#e3a008"

STATUS_COLOURS = {
    "Needs Review": "#8b949e",
    "Unexplained": "#e3a008",
    "Debunked": "#3fb950",
    "Corroborated": "#58a6ff",
    "Inconclusive": "#bc8cff",
    "Equipment Artifact": "#a1887f",
    "Contamination": "#f778ba",
}

DARK_QSS = f"""
QWidget {{
    background-color: {BG_BASE};
    color: {TEXT};
    font-size: 13px;
}}
QMainWindow::separator {{
    background: {BORDER};
    width: 4px;
    height: 4px;
}}
QToolBar {{
    background: {BG_PANEL};
    border: none;
    padding: 4px;
    spacing: 4px;
}}
QDockWidget {{
    titlebar-close-icon: none;
    font-weight: 600;
}}
QDockWidget::title {{
    background: {BG_PANEL};
    padding: 8px 10px;
    border-bottom: 1px solid {BORDER};
}}
QStatusBar {{
    background: {BG_PANEL};
    border-top: 1px solid {BORDER};
    color: {TEXT_MUTED};
}}
QStatusBar::item {{ border: none; }}

QPushButton {{
    background-color: {BG_ELEVATED};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 12px;
    min-height: 20px;
}}
QPushButton:hover {{ background-color: #2f343a; }}
QPushButton:pressed {{ background-color: #23272c; }}
QPushButton:disabled {{ color: #5c646d; background-color: #1b1e22; }}
QPushButton:focus {{ border: 1px solid {ACCENT}; }}

QPushButton#logButton {{
    background-color: {ACCENT};
    border: 1px solid {ACCENT};
    color: #ffffff;
    font-weight: 700;
    letter-spacing: 0.5px;
    padding: 7px 22px;
    border-radius: 6px;
}}
QPushButton#logButton:hover {{ background-color: {ACCENT_HOVER}; }}
QPushButton#logButton:pressed {{ background-color: {ACCENT_PRESSED}; }}
QPushButton#logButton:disabled {{
    background-color: #4a2427;
    border-color: #4a2427;
    color: #9c7a7c;
}}

QToolButton {{
    background: transparent;
    border: 1px solid transparent;
    border-radius: 5px;
    padding: 4px 7px;
    font-size: 14px;
}}
QToolButton:hover {{ background-color: {BG_ELEVATED}; border-color: {BORDER}; }}
QToolButton:pressed {{ background-color: #23272c; }}
QToolButton:checked {{ background-color: {BG_ELEVATED}; border-color: {ACCENT}; }}
QToolButton:disabled {{ color: #5c646d; }}

QLineEdit, QTextEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox,
QDateEdit, QDateTimeEdit, QTimeEdit {{
    background-color: {BG_PANEL};
    border: 1px solid {BORDER};
    border-radius: 5px;
    padding: 5px 8px;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus,
QDateEdit:focus, QDateTimeEdit:focus {{
    border: 1px solid {ACCENT};
}}
QLineEdit:read-only {{ background-color: #1a1d21; color: {TEXT_MUTED}; }}
QLineEdit[invalid="true"], QTextEdit[invalid="true"], QComboBox[invalid="true"] {{
    border: 1px solid {ACCENT};
}}

QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background-color: {BG_ELEVATED};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT};
    outline: none;
}}

QSlider::groove:horizontal {{
    height: 5px;
    background: #2f343a;
    border-radius: 3px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 3px;
}}
QSlider::handle:horizontal {{
    background: #ffffff;
    width: 13px;
    height: 13px;
    margin: -5px 0;
    border-radius: 7px;
}}
QSlider::handle:horizontal:hover {{ background: {ACCENT_HOVER}; }}

QTableView {{
    background-color: {BG_PANEL};
    alternate-background-color: #1a1d21;
    gridline-color: {BORDER};
    border: 1px solid {BORDER};
    border-radius: 6px;
    selection-background-color: #33373d;
    selection-color: {TEXT};
    outline: none;
}}
QHeaderView::section {{
    background-color: {BG_ELEVATED};
    color: {TEXT_MUTED};
    padding: 6px 8px;
    border: none;
    border-right: 1px solid {BORDER};
    border-bottom: 1px solid {BORDER};
    font-weight: 600;
}}
QTreeView {{
    background-color: {BG_PANEL};
    border: 1px solid {BORDER};
    border-radius: 6px;
    outline: none;
}}
QTreeView::item {{ padding: 3px; }}
QTreeView::item:selected {{ background-color: #33373d; }}

QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: #3c434b; border-radius: 5px; min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: #4b535c; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
QScrollBar::handle:horizontal {{
    background: #3c434b; border-radius: 5px; min-width: 28px;
}}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

QGroupBox {{
    border: 1px solid {BORDER};
    border-radius: 6px;
    margin-top: 12px;
    padding-top: 10px;
    font-weight: 600;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 5px;
    color: {TEXT_MUTED};
}}

QLabel#fieldLabel {{ color: {TEXT_MUTED}; font-weight: 600; }}
QLabel#hintLabel {{ color: {TEXT_MUTED}; font-size: 11px; }}
QLabel#warningLabel {{ color: {WARNING}; font-size: 11px; }}

/* Surfaces that used to carry these colours inline. Inline styles survive
   apply_theme clearing the stylesheet, so on the "system" theme they painted
   dark rectangles onto a light window. */
QLabel#emptyState {{ color: #6f7880; font-size: 15px; background: {BG_SUNKEN}; }}
QLabel#snapshotWell {{
    background: {BG_SUNKEN};
    border: 1px solid {BORDER};
    border-radius: 6px;
    color: {TEXT_MUTED};
}}
QFrame#divider {{ background: {BORDER}; border: none; max-height: 1px; }}
QFrame#toolSeparator {{ background: {BORDER}; border: none; }}
QLabel#timeLabel {{
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 13px;
    color: {TEXT};
}}
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 6px; }}
QTabBar::tab {{
    background: {BG_PANEL};
    padding: 7px 14px;
    border: 1px solid {BORDER};
    border-bottom: none;
    border-top-left-radius: 5px;
    border-top-right-radius: 5px;
}}
QTabBar::tab:selected {{ background: {BG_ELEVATED}; color: {TEXT}; }}

QMenu {{
    background-color: {BG_ELEVATED};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px;
}}
QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 4px; }}
QMenu::item:selected {{ background-color: {ACCENT}; color: #ffffff; }}
QMenuBar {{ background: {BG_PANEL}; }}
QMenuBar::item:selected {{ background: {BG_ELEVATED}; }}

QCheckBox::indicator, QRadioButton::indicator {{
    width: 15px; height: 15px;
    border: 1px solid {BORDER};
    border-radius: 3px;
    background: {BG_PANEL};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

QProgressBar {{
    border: 1px solid {BORDER};
    border-radius: 4px;
    background: {BG_PANEL};
    text-align: center;
    height: 6px;
}}
QProgressBar::chunk {{ background: {ACCENT}; border-radius: 3px; }}

QToolTip {{
    background-color: {BG_ELEVATED};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px 7px;
    border-radius: 4px;
}}
"""


def apply_theme(app: QApplication, theme: str = "dark") -> None:
    """Apply the requested theme. Anything other than ``dark`` uses Qt's default."""
    app.setStyle("Fusion")
    if theme != "dark":
        app.setPalette(QApplication.style().standardPalette())
        app.setStyleSheet("")
        return

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG_BASE))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Base, QColor(BG_PANEL))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(BG_ELEVATED))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Button, QColor(BG_ELEVATED))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(BG_ELEVATED))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(TEXT_MUTED))
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(TEXT_MUTED)
    )
    app.setPalette(palette)
    app.setStyleSheet(DARK_QSS)


#: Picked to sit beside STATUS_COLOURS without colliding with them, and to stay
#: legible on both themes.
_CUSTOM_STATUS_COLOURS = ("#7ec8a9", "#d99bd4", "#9db4e8", "#d4b483", "#8fcfd1", "#c49ae0")


def status_colour(status: str) -> str:
    """A colour for a status, including ones the reviewer invented.

    Settings presents adding statuses as a first-class thing, but every unknown
    one used to come back the same muted grey, so a case using custom statuses
    lost the colour coding that makes the log readable at a glance. Hashing the
    name keeps a given status the same colour everywhere and between runs.
    """
    known = STATUS_COLOURS.get(status)
    if known is not None:
        return known
    if not status:
        return TEXT_MUTED
    index = sum(status.encode("utf-8")) % len(_CUSTOM_STATUS_COLOURS)
    return _CUSTOM_STATUS_COLOURS[index]
