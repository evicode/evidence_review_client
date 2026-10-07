"""First-run setup: the few things the app cannot sensibly guess."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config import DEFAULT_SERVER_URL, ApiSettings, Settings, app_paths, set_api_token
from ..media.mpv_loader import MpvNotAvailable, ensure_mpv_loadable
from ..sync import ApiClient, ApiError
from ..util import os_user_display_name, slugify_case_id
from ..version import APP_NAME, APP_VERSION
from .widgets import fit_to_screen, hint_label

log = logging.getLogger(__name__)


class FirstRunDialog(QDialog):
    """Shown once, before the main window."""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle(f"Welcome to {APP_NAME}")
        self.setModal(True)
        self.setMinimumWidth(560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 16)
        layout.setSpacing(14)

        heading = QLabel(f"{APP_NAME} {APP_VERSION}")
        heading.setStyleSheet("font-size:19px; font-weight:700;")
        layout.addWidget(heading)
        layout.addWidget(
            hint_label(
                "Your name and the case you are reviewing. The shared server is optional, "
                "and everything here can be changed later in Settings."
            )
        )

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(9)
        layout.addLayout(form)

        self.investigator_edit = QLineEdit(
            settings.review.investigator_name or os_user_display_name()
        )
        self.investigator_edit.setPlaceholderText("Your name, as it should appear in the log")
        form.addRow("Investigator name", self.investigator_edit)

        self.case_name_edit = QLineEdit(settings.review.case_name)
        self.case_name_edit.setPlaceholderText("e.g. Willow House - October review")
        form.addRow("Case name", self.case_name_edit)

        # The URL and token fields stay usable whether or not this is ticked, the
        # same as Settings. Pasting a server URL into a dead field and watching
        # nothing happen is a worse first five minutes than one question at the end.
        self.api_enabled_check = QCheckBox("Log to a shared server as well as this computer")
        self.api_enabled_check.setChecked(settings.api.enabled)
        form.addRow(self.api_enabled_check)

        self.api_url_edit = QLineEdit(settings.api.base_url)
        self.api_url_edit.setPlaceholderText(DEFAULT_SERVER_URL)
        # Ticking "shared server" with no address fills in the real one: the
        # placeholder used to be "https://evidence.example.org", which told
        # nobody where to sync, and people entered whatever they could guess.
        self.api_enabled_check.toggled.connect(self._offer_default_server)
        form.addRow("Server URL", self.api_url_edit)

        self.api_token_edit = QLineEdit()
        self.api_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_token_edit.setPlaceholderText("From your muuurder.com profile, Evidence Review section")
        form.addRow("API token", self.api_token_edit)

        test_row = QHBoxLayout()
        test_row.setContentsMargins(0, 0, 0, 0)
        self.test_button = QPushButton("Test connection")
        self.test_button.clicked.connect(self._test_connection)
        test_row.addWidget(self.test_button)
        self.test_label = QLabel("")
        self.test_label.setWordWrap(True)
        test_row.addWidget(self.test_label, 1)
        test_container = QWidget()
        test_container.setLayout(test_row)
        form.addRow(test_container)

        layout.addWidget(
            hint_label(
                "Entries are always written to this computer first, so reviewing works "
                "with no network at all. Anything unsent is uploaded when the server "
                "becomes reachable."
            )
        )

        self.mpv_label = QLabel()
        self.mpv_label.setWordWrap(True)
        layout.addWidget(self.mpv_label)
        self._check_mpv()

        buttons = QDialogButtonBox()
        start = buttons.addButton("Start reviewing", QDialogButtonBox.ButtonRole.AcceptRole)
        start.setDefault(True)
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        fit_to_screen(self)

    # ------------------------------------------------------------------ #

    def _check_mpv(self) -> None:
        try:
            ensure_mpv_loadable()
        except MpvNotAvailable:
            self.mpv_label.setText(
                "⚠ libmpv was not found, so video and audio playback is unavailable. "
                "Still images will still work. See Settings → Diagnostics."
            )
            self.mpv_label.setStyleSheet("color:#e3a008;")
        else:
            self.mpv_label.setText("✓ Media engine ready.")
            self.mpv_label.setStyleSheet("color:#3fb950;")

    def _offer_default_server(self, checked: bool) -> None:
        """Fill in the muuurder.com server when syncing is switched on with none set."""
        if checked and not self.api_url_edit.text().strip():
            self.api_url_edit.setText(DEFAULT_SERVER_URL)

    def _test_connection(self) -> None:
        url = self.api_url_edit.text().strip().rstrip("/")
        if not url:
            self.test_label.setText("Enter a server URL first.")
            return
        probe = ApiSettings(base_url=url, enabled=True, timeout_seconds=5.0)
        self.test_button.setEnabled(False)
        self.test_label.setText("Contacting the server…")
        try:
            with ApiClient(probe, self.api_token_edit.text().strip()) as client:
                payload = client.health()
        except ApiError as exc:
            log.warning("First-run connection test failed: %s", exc)
            self.test_label.setText(str(exc))
            self.test_label.setStyleSheet("color:#e5484d;")
        except Exception as exc:  # the dialog must survive anything the client raises
            log.exception("First-run connection test raised")
            self.test_label.setText(f"Could not reach the server: {exc}")
            self.test_label.setStyleSheet("color:#e5484d;")
        else:
            self.test_label.setText(f"Connected. Server version {payload.get('version', '?')}.")
            self.test_label.setStyleSheet("color:#3fb950;")
        finally:
            self.test_button.setEnabled(True)

    def _on_accept(self) -> None:
        name = self.investigator_edit.text().strip()
        if not name:
            QMessageBox.warning(
                self,
                "Investigator name",
                "Every log entry records who reviewed it, so a name is required.",
            )
            self.investigator_edit.setFocus()
            return

        review = self._settings.review
        review.investigator_name = name
        review.case_name = self.case_name_edit.text().strip() or "Default Case"
        review.case_id = slugify_case_id(review.case_name, max_length=60)

        api_url = self.api_url_edit.text().strip().rstrip("/")
        api_enabled = self.api_enabled_check.isChecked()
        # Same question Settings asks, for the same reason: a URL and a token with
        # the box unticked look exactly like a working server, but nothing is ever
        # uploaded and nothing says so.
        if api_url and not api_enabled:
            answer = QMessageBox.question(
                self,
                "Sync is off",
                "A server URL is set, but “Log to a shared server as well as this "
                "computer” is not ticked, so entries will stay on this machine.\n\n"
                "Turn sync on?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer is QMessageBox.StandardButton.Yes:
                api_enabled = True
                self.api_enabled_check.setChecked(True)

        api = self._settings.api
        api.enabled = api_enabled
        api.base_url = api_url
        # Settings warns loudly in this exact situation; the wizard used to discard
        # the return value, so a reviewer whose credential store was unavailable
        # finished setup with a configuration that looked right and would 401
        # forever with nothing saying why.
        if api_enabled and not set_api_token(self.api_token_edit.text().strip()):
            QMessageBox.warning(
                self,
                "Token not stored",
                "The Windows Credential Manager is unavailable, so the API token "
                "could not be saved. Sync will use it for this session only; "
                "re-enter it in Settings → Server afterwards.",
            )

        self._settings.first_run_complete = True
        try:
            self._settings.save()
        except OSError as exc:
            QMessageBox.critical(
                self,
                "Could not save settings",
                f"Settings could not be written to:\n{app_paths().config_file}\n\n{exc}",
            )
            return
        self.accept()


def run_first_run_if_needed(settings: Settings, parent: QWidget | None = None) -> bool:
    """Show the wizard when required. Returns False if the user cancelled."""
    if settings.first_run_complete and settings.review.investigator_name:
        return True
    dialog = FirstRunDialog(settings, parent)
    dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
    return dialog.exec() == QDialog.DialogCode.Accepted
