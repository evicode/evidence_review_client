"""Application bootstrap: logging, the Qt application, first run, and the main window."""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import traceback
from pathlib import Path
from types import TracebackType

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from .config import Settings, app_paths, load_settings
from .models import DEFAULT_STATUSES
from .store import LocalStore
from .ui.first_run import run_first_run_if_needed
from .ui.main_window import MainWindow
from .ui.theme import apply_theme
from .version import APP_NAME, APP_SLUG, APP_VERSION, ORG_NAME

log = logging.getLogger(__name__)


def configure_logging(verbose: bool = False) -> None:
    """Rotating file log plus a console handler when a console exists."""
    paths = app_paths()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    formatter = logging.Formatter(
        "%(asctime)s  %(levelname)-7s  %(name)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    file_handler = logging.handlers.RotatingFileHandler(
        paths.log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        console.setLevel(logging.DEBUG if verbose else logging.WARNING)
        root.addHandler(console)

    # PySide6 and httpx are chatty at DEBUG; keep them at INFO.
    for noisy in ("httpx", "httpcore", "PIL", "mutagen"):
        logging.getLogger(noisy).setLevel(logging.INFO)


def _install_excepthook() -> None:
    """Log unhandled exceptions and tell the user, instead of vanishing silently."""

    def handler(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_tb: TracebackType | None,
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        log.critical("Unhandled exception", exc_info=(exc_type, exc_value, exc_tb))
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        if QApplication.instance() is not None:
            box = QMessageBox()
            box.setIcon(QMessageBox.Icon.Critical)
            box.setWindowTitle("Unexpected error")
            box.setText("Something went wrong. Your logged entries are safe in the local database.")
            box.setInformativeText(str(exc_value))
            box.setDetailedText(detail)
            box.exec()

    sys.excepthook = handler


def _set_windows_app_id() -> None:
    """Give Windows a stable identity so the taskbar groups and pins correctly."""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            f"{ORG_NAME}.{APP_SLUG}.{APP_VERSION}"
        )
    except Exception:
        log.debug("Could not set the AppUserModelID", exc_info=True)


def create_application(argv: list[str] | None = None) -> QApplication:
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeMenuBar, False)
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(ORG_NAME)

    # The icon ships inside the package; it was previously looked for in the
    # config directory, where nothing ever writes it, so it never loaded.
    icon_path = Path(__file__).resolve().parent / "resources" / "app.ico"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    else:
        log.debug("Window icon not found at %s", icon_path)
    return app


def carry_over_custom_statuses(settings: Settings, store: LocalStore) -> None:
    """Move a status list curated in Settings onto the template that now owns it.

    Before templates, the status vocabulary lived in the config file. Somebody who
    added "Draught - confirmed" or removed "Contamination" did real work on it, and
    the modal now reads the template instead -- so without this their list would
    silently stop being used.

    Runs once, guarded by a flag, because afterwards the template is the truth. Doing
    it on every launch would undo any later edit made through the template.
    """
    if settings.review.statuses_migrated_to_template:
        return

    configured = [status for status in settings.review.statuses if status.strip()]
    if configured and tuple(configured) != DEFAULT_STATUSES:
        changed = store.apply_status_vocabulary(
            settings.review.case_id, configured, settings.review.default_status
        )
        if changed:
            log.info("Carried %d customised statuses onto the case template", len(configured))

    settings.review.statuses_migrated_to_template = True
    try:
        settings.save()
    except Exception:
        # The carry-over itself succeeded; failing to record it only means it is
        # attempted again next launch, which is harmless.
        log.warning("Could not record the status carry-over", exc_info=True)


def run(argv: list[str] | None = None) -> int:
    """Start the application. Returns the process exit code."""
    argv = list(sys.argv if argv is None else argv)
    verbose = "--verbose" in argv or "-v" in argv
    media_arguments = [arg for arg in argv[1:] if not arg.startswith("-") and os.path.exists(arg)]

    configure_logging(verbose)
    _set_windows_app_id()
    log.info("Starting %s %s", APP_NAME, APP_VERSION)

    app = create_application(argv)
    _install_excepthook()

    settings = load_settings()
    apply_theme(app, settings.ui.theme)

    if not run_first_run_if_needed(settings):
        log.info("First-run setup cancelled; exiting")
        return 0

    paths = app_paths()
    try:
        store = LocalStore(paths.database)
    except Exception as exc:
        log.exception("Could not open the local database")
        QMessageBox.critical(
            None,
            "Database unavailable",
            f"The local database could not be opened:\n\n{paths.database}\n\n{exc}",
        )
        return 1

    carry_over_custom_statuses(settings, store)

    window = MainWindow(settings, store)
    window.show()

    if media_arguments:
        window.load_media(media_arguments[0])

    return app.exec()
