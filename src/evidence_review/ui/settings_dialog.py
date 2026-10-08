"""Settings, including the connection test for the shared server."""

from __future__ import annotations

import html
import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import shortcuts as registry
from ..config import DEFAULT_SERVER_URL, Settings, app_paths, get_api_token, set_api_token
from ..media.mpv_loader import describe_availability
from ..models import DEFAULT_STATUSES
from ..store import LocalStore
from ..sync import ApiClient, ApiError, SubscriptionRequired
from ..templates import DEFAULT_MAX_LENGTH, FieldType
from ..version import APP_VERSION
from .widgets import fit_to_screen, hint_label, scrollable

log = logging.getLogger(__name__)


#: A status is one option of a choice field, and an area one value of a text field,
#: so their limits come from those field types rather than from a column name. The
#: old MAX_LENGTHS entries went away with the fixed columns they described.
STATUS_LABEL_LIMIT = DEFAULT_MAX_LENGTH[FieldType.CHOICE]
AREA_LABEL_LIMIT = 200


class SettingsDialog(QDialog):
    """Edits :class:`Settings` in place and persists on accept."""

    def __init__(
        self,
        settings: Settings,
        parent: QWidget | None = None,
        store: LocalStore | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        # Statuses belong to the case's template, not to settings, so editing them
        # needs the store. Optional only so a test can open the dialog without one.
        self._store = store
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setMinimumWidth(620)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 12)
        layout.setSpacing(12)

        # Every tab scrolls. Which screen this opens on is not ours to choose, and
        # a setting below the fold is a setting the user does not have.
        tabs = QTabWidget()
        tabs.addTab(scrollable(self._build_review_tab()), "Review")
        tabs.addTab(scrollable(self._build_server_tab()), "Server")
        tabs.addTab(scrollable(self._build_player_tab()), "Player")
        tabs.addTab(scrollable(self._build_shortcuts_tab()), "Shortcuts")
        tabs.addTab(scrollable(self._build_about_tab()), "Diagnostics")
        layout.addWidget(tabs)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._load()
        self.setSizeGripEnabled(True)
        fit_to_screen(self)

    # ------------------------------------------------------------------ #
    # Tabs
    # ------------------------------------------------------------------ #

    def _build_review_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(9)

        self.investigator_edit = QLineEdit()
        form.addRow("Investigator name", self.investigator_edit)

        # Read-only. The case is switched, created and renamed from the status bar,
        # and having a second editable copy here meant two ways to change it - one
        # of which re-slugged the id on every save, so opening Settings and pressing
        # Save could rename a case as a side effect.
        self.case_label = QLabel()
        self.case_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow("Case", self.case_label)

        # addRow(widget) spans both columns; addRow("", widget) would indent it into
        # the field column, which is what made these hints easy to miss before.
        form.addRow(
            hint_label("Switch, create or rename a case from the case button in the status bar."),
        )

        self.status_list = QListWidget()
        self.status_list.setMaximumHeight(140)
        status_buttons = QHBoxLayout()
        add_button = QPushButton("Add…")
        add_button.clicked.connect(self._add_status)
        remove_button = QPushButton("Remove")
        remove_button.clicked.connect(self._remove_status)
        reset_button = QPushButton("Reset")
        reset_button.clicked.connect(self._reset_statuses)
        status_buttons.addWidget(add_button)
        status_buttons.addWidget(remove_button)
        status_buttons.addWidget(reset_button)
        status_buttons.addStretch(1)

        status_container = QWidget()
        status_layout = QVBoxLayout(status_container)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.addWidget(self.status_list)
        status_layout.addLayout(status_buttons)
        form.addRow("Status options", status_container)

        self.default_status_combo = QComboBox()
        form.addRow("Default status", self.default_status_combo)
        # The same statuses, in the same place: this list and the template editor both
        # write the case template's status field. This is the quick way to change the
        # wording; the editor is where colours, order and the rest of the form live.
        form.addRow(
            "",
            hint_label(
                "These belong to this case's template. For their colours, and for the "
                "rest of the form, use File → Log templates (Ctrl+T)."
            ),
        )

        # review.areas was read by the LOG modal and writable by nothing, so the
        # Area dropdown's seed list was permanently empty and only ever filled
        # from entries already logged. Knowing a building's rooms before the first
        # entry exists is the normal case, not the exception.
        self.area_list = QListWidget()
        self.area_list.setMaximumHeight(110)
        area_buttons = QHBoxLayout()
        add_area = QPushButton("Add…")
        add_area.clicked.connect(self._add_area)
        remove_area = QPushButton("Remove")
        remove_area.clicked.connect(self._remove_area)
        area_buttons.addWidget(add_area)
        area_buttons.addWidget(remove_area)
        area_buttons.addStretch(1)

        area_container = QWidget()
        area_layout = QVBoxLayout(area_container)
        area_layout.setContentsMargins(0, 0, 0, 0)
        area_layout.addWidget(self.area_list)
        area_layout.addLayout(area_buttons)
        form.addRow("Areas", area_container)
        form.addRow(
            hint_label(
                "Offered in the Area box when logging, alongside areas already used in "
                "this case. Leave it empty to work only from what has been logged."
            ),
        )

        self.auto_resume_check = QCheckBox("Resume playback after saving a log entry")
        form.addRow(self.auto_resume_check)

        self.snapshot_check = QCheckBox("Capture the current frame when logging")
        form.addRow(self.snapshot_check)

        self.hash_check = QCheckBox("Compute a SHA-256 hash of each media file")
        form.addRow(self.hash_check)
        form.addRow(
            hint_label(
                "Hashing runs in the background and is cached per file. Turn it off if you "
                "are reviewing very large files from slow storage."
            ),
        )

        self.guess_area_check = QCheckBox("Guess the Area from the folder or filename")
        form.addRow(self.guess_area_check)

        return page

    def _seek_keys_hint(self) -> str:
        """Name the keys these two steps are bound to, as they are bound now."""
        bindings = registry.resolve(self._settings.shortcuts.overrides)

        def keys_for(action_id: str) -> str:
            return " or ".join(bindings.get(action_id) or []) or "nothing"

        return (
            f"Seek is bound to {keys_for('seek_back')} / {keys_for('seek_forward')}, "
            f"fine seek to {keys_for('seek_back_fine')} / {keys_for('seek_forward_fine')}."
        )

    def _build_server_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setVerticalSpacing(9)

        self.api_enabled_check = QCheckBox("Send log entries to a shared server")
        form.addRow(self.api_enabled_check)

        self.api_url_edit = QLineEdit()
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
        form.addRow(
            hint_label(
                "The token is stored in the Windows Credential Manager, never in the "
                "configuration file."
            ),
        )
        # Where a token comes from is not guessable from a password box labelled
        # "Bearer token". The address is discovered from the server rather than
        # built here, so it follows whatever deployment this install points at.
        self.tokens_link_label = QLabel()
        self.tokens_link_label.setOpenExternalLinks(True)
        self.tokens_link_label.setWordWrap(True)
        self.tokens_link_label.setVisible(False)
        form.addRow("", self.tokens_link_label)

        self.verify_tls_check = QCheckBox("Verify the TLS certificate")
        form.addRow(self.verify_tls_check)

        self.sync_interval_spin = QSpinBox()
        self.sync_interval_spin.setRange(5, 3600)
        self.sync_interval_spin.setSuffix(" s")
        form.addRow("Sync every", self.sync_interval_spin)

        self.batch_size_spin = QSpinBox()
        self.batch_size_spin.setRange(1, 200)
        form.addRow("Entries per batch", self.batch_size_spin)

        # Not "on start" any more: pulling happens every sync cycle, which is what
        # makes a shared log actually shared. Unticking it means this machine
        # uploads but never receives.
        self.pull_check = QCheckBox("Receive other investigators' entries")
        self.pull_check.setToolTip(
            "Checked, this machine also downloads entries other investigators have "
            "logged for the current case. Unchecked, it only uploads its own."
        )
        form.addRow(self.pull_check)

        test_row = QHBoxLayout()
        self.test_button = QPushButton("Test connection")
        self.test_button.clicked.connect(self._test_connection)
        test_row.addWidget(self.test_button)
        self.test_result_label = QLabel("")
        self.test_result_label.setWordWrap(True)
        test_row.addWidget(self.test_result_label, 1)
        container = QWidget()
        container.setLayout(test_row)
        test_row.setContentsMargins(0, 0, 0, 0)
        form.addRow(container)

        return page

    def _build_player_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setVerticalSpacing(9)

        # Named by what they do, not by the control or key that happens to drive
        # them: there is no "skip" button in the player - they are tooltipped Back
        # and Forward - and every key here can be rebound, so "Arrow key step" and
        # "Shift+arrow step" stopped being true the moment someone used the
        # Shortcuts tab in this same dialog.
        self.jump_step_spin = QDoubleSpinBox()
        self.jump_step_spin.setRange(1.0, 300.0)
        self.jump_step_spin.setSuffix(" s")
        form.addRow("Back / forward buttons", self.jump_step_spin)

        self.seek_step_spin = QDoubleSpinBox()
        self.seek_step_spin.setRange(0.1, 120.0)
        self.seek_step_spin.setSuffix(" s")
        form.addRow("Seek", self.seek_step_spin)

        self.fine_seek_spin = QDoubleSpinBox()
        self.fine_seek_spin.setRange(0.05, 60.0)
        self.fine_seek_spin.setSuffix(" s")
        form.addRow("Fine seek", self.fine_seek_spin)
        self.seek_keys_hint = hint_label("")
        form.addRow(self.seek_keys_hint)

        self.hwdec_combo = QComboBox()
        self.hwdec_combo.addItems(["auto-safe", "auto", "no", "d3d11va", "dxva2", "nvdec"])
        form.addRow("Hardware decoding", self.hwdec_combo)
        form.addRow(
            hint_label(
                "'auto-safe' is the right answer almost always. Switch to 'no' if a "
                "particular file shows corrupted frames."
            ),
        )

        self.theme_combo = QComboBox()
        self.theme_combo.addItems(["dark", "system"])
        form.addRow("Theme", self.theme_combo)
        form.addRow(hint_label("A theme change takes effect the next time the app starts."))

        return page

    def _build_shortcuts_tab(self) -> QWidget:
        from .. import shortcuts as registry
        from .shortcut_editor import ShortcutEditor

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 8, 0, 0)
        self.shortcut_editor = ShortcutEditor(registry.resolve(self._settings.shortcuts.overrides))
        layout.addWidget(self.shortcut_editor)
        return page

    def _build_about_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        paths = app_paths()
        report = QPlainTextEdit()
        report.setReadOnly(True)
        report.setPlainText(
            "\n".join(
                [
                    f"Evidence Review {APP_VERSION}",
                    "",
                    describe_availability(),
                    "",
                    f"Config file:  {paths.config_file}",
                    f"Database:     {paths.database}",
                    f"Snapshots:    {paths.snapshots_dir}",
                    f"Exports:      {paths.exports_dir}",
                    f"Log file:     {paths.log_file}",
                ]
            )
        )
        layout.addWidget(report)

        open_button = QPushButton("Open the data folder")
        open_button.clicked.connect(self._open_data_folder)
        layout.addWidget(open_button, 0, Qt.AlignmentFlag.AlignLeft)
        return page

    # ------------------------------------------------------------------ #
    # Load and save
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        review = self._settings.review
        api = self._settings.api
        player = self._settings.player

        self.investigator_edit.setText(review.investigator_name)
        self.case_label.setText(f"{review.case_name}  ({review.case_id})")
        self.seek_keys_hint.setText(self._seek_keys_hint())
        self.area_list.clear()
        self.area_list.addItems(review.areas)
        statuses, default_status = self._status_vocabulary()
        self.status_list.clear()
        self.status_list.addItems(statuses)
        self._refresh_default_statuses(default_status)
        self.auto_resume_check.setChecked(review.auto_resume_after_log)
        self.snapshot_check.setChecked(review.capture_snapshot_on_log)
        self.hash_check.setChecked(review.compute_media_hash)
        self.guess_area_check.setChecked(review.guess_area_from_path)

        self.api_enabled_check.setChecked(api.enabled)
        self.api_url_edit.setText(api.base_url)
        # Remember what was loaded. get_api_token() returns "" when the credential
        # store is momentarily unavailable, and writing that back would delete a
        # perfectly good token just because Settings was opened during the blip.
        self._loaded_token = get_api_token()
        self.api_token_edit.setText(self._loaded_token)
        self.verify_tls_check.setChecked(api.verify_tls)
        self.sync_interval_spin.setValue(api.sync_interval_seconds)
        self.batch_size_spin.setValue(api.batch_size)
        self.pull_check.setChecked(api.pull_on_start)

        self.jump_step_spin.setValue(player.jump_step_seconds)
        self.seek_step_spin.setValue(player.seek_step_seconds)
        self.fine_seek_spin.setValue(player.fine_seek_step_seconds)
        index = self.hwdec_combo.findText(player.hardware_decoding)
        self.hwdec_combo.setCurrentIndex(max(index, 0))
        theme_index = self.theme_combo.findText(self._settings.ui.theme)
        self.theme_combo.setCurrentIndex(max(theme_index, 0))

    def _on_save(self) -> None:
        statuses = [self.status_list.item(row).text() for row in range(self.status_list.count())]
        if not statuses:
            QMessageBox.warning(self, "Statuses", "Keep at least one status option.")
            return
        if not self.investigator_edit.text().strip():
            QMessageBox.warning(
                self, "Investigator", "An investigator name is required for the log."
            )
            return

        review = self._settings.review
        review.investigator_name = self.investigator_edit.text().strip()
        review.areas = [self.area_list.item(i).text() for i in range(self.area_list.count())]
        default_status = self.default_status_combo.currentText() or statuses[0]
        if self._store is not None:
            self._store.apply_status_vocabulary(review.case_id, statuses, default_status)
        review.auto_resume_after_log = self.auto_resume_check.isChecked()
        review.capture_snapshot_on_log = self.snapshot_check.isChecked()
        review.compute_media_hash = self.hash_check.isChecked()
        review.guess_area_from_path = self.guess_area_check.isChecked()

        api_url = self.api_url_edit.text().strip().rstrip("/")
        api_enabled = self.api_enabled_check.isChecked()
        # A server URL and a token with the box unticked look exactly like a
        # configured server, but nothing is ever uploaded and nothing says so.
        # Ask rather than save a setup that cannot work.
        if api_url and not api_enabled:
            answer = QMessageBox.question(
                self,
                "Sync is off",
                "A server URL is set, but “Send log entries to a shared server” is "
                "not ticked, so entries will stay on this machine.\n\n"
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
        api.verify_tls = self.verify_tls_check.isChecked()
        api.sync_interval_seconds = self.sync_interval_spin.value()
        api.batch_size = self.batch_size_spin.value()
        api.pull_on_start = self.pull_check.isChecked()

        player = self._settings.player
        player.jump_step_seconds = self.jump_step_spin.value()
        player.seek_step_seconds = self.seek_step_spin.value()
        player.fine_seek_step_seconds = self.fine_seek_spin.value()
        player.hardware_decoding = self.hwdec_combo.currentText()

        self._settings.ui.theme = self.theme_combo.currentText()

        from .. import shortcuts as registry

        bindings = self.shortcut_editor.bindings()
        clashes = registry.conflicts(bindings)
        if clashes:
            # The editor takes a key away from its previous owner as soon as it is
            # reassigned, so reaching here means something unusual happened.
            detail = "\n".join(
                f"{sequence}: " + ", ".join(registry.ACTIONS_BY_ID[a].label for a in action_ids)
                for sequence, action_ids in clashes.items()
            )
            QMessageBox.warning(
                self,
                "Duplicate shortcuts",
                "These keys are bound to more than one action, so which one runs "
                f"would be unpredictable:\n\n{detail}",
            )
            return
        blocked = self.shortcut_editor.blocked_keys()
        if blocked:
            # Not an error: the user may know something we do not about their
            # machine. But a key that never fires is a silent failure worth one
            # explicit confirmation.
            detail = "\n".join(
                f"{registry.ACTIONS_BY_ID[action_id].label}: "
                f"{registry.primary(bindings, action_id)} - {warning.message}"
                for action_id, warning in blocked.items()
            )
            answer = QMessageBox.question(
                self,
                "Keys the system will take",
                "These keys are handled by Windows before this application sees "
                f"them, so they will not work:\n\n{detail}\n\nSave anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer is not QMessageBox.StandardButton.Yes:
                return

        self._settings.shortcuts.overrides = registry.overrides_from(bindings)

        token = self.api_token_edit.text().strip()
        if token != self._loaded_token and not set_api_token(token):
            QMessageBox.warning(
                self,
                "Token not stored",
                "The Windows Credential Manager is unavailable, so the API token change "
                "could not be saved. Sync will use it for this session only.",
            )

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

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _status_vocabulary(self) -> tuple[list[str], str]:
        """The case template's status options, or the shipped defaults without a store."""
        if self._store is not None:
            statuses, default_status = self._store.status_vocabulary(self._settings.review.case_id)
            if statuses:
                return statuses, default_status
        return list(DEFAULT_STATUSES), DEFAULT_STATUSES[0]

    def _refresh_default_statuses(self, selected: str) -> None:
        self.default_status_combo.clear()
        statuses = [self.status_list.item(row).text() for row in range(self.status_list.count())]
        self.default_status_combo.addItems(statuses)
        index = self.default_status_combo.findText(selected)
        self.default_status_combo.setCurrentIndex(max(index, 0))

    def _add_status(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        text, accepted = QInputDialog.getText(self, "Add status", "New status label:")
        label = text.strip()
        if not accepted or not label:
            return

        # Checked here rather than at log-save time, which is a different dialog
        # and a long way from the mistake.
        limit = STATUS_LABEL_LIMIT
        if len(label) > limit:
            QMessageBox.warning(
                self,
                "Status is too long",
                f"A status can be at most {limit} characters; that one is {len(label)}.",
            )
            return
        existing = {self.status_list.item(i).text() for i in range(self.status_list.count())}
        if label in existing:
            QMessageBox.information(
                self, "Already in the list", f"“{label}” is already a status option."
            )
            return

        self.status_list.addItem(label)
        self._refresh_default_statuses(self.default_status_combo.currentText())

    def _remove_status(self) -> None:
        row = self.status_list.currentRow()
        if row < 0:
            # A button that silently does nothing reads as a broken button.
            QMessageBox.information(self, "Nothing selected", "Select a status in the list first.")
            return
        self.status_list.takeItem(row)
        self._refresh_default_statuses(self.default_status_combo.currentText())

    def _add_area(self) -> None:
        from PySide6.QtWidgets import QInputDialog

        text, accepted = QInputDialog.getText(self, "Add area", "Area name:")
        label = text.strip()
        if not accepted or not label:
            return
        limit = AREA_LABEL_LIMIT
        if len(label) > limit:
            QMessageBox.warning(
                self,
                "Area is too long",
                f"An area can be at most {limit} characters; that one is {len(label)}.",
            )
            return
        existing = {self.area_list.item(i).text() for i in range(self.area_list.count())}
        if label in existing:
            QMessageBox.information(self, "Already in the list", f"“{label}” is already an area.")
            return
        self.area_list.addItem(label)

    def _remove_area(self) -> None:
        row = self.area_list.currentRow()
        if row < 0:
            QMessageBox.information(self, "Nothing selected", "Select an area in the list first.")
            return
        self.area_list.takeItem(row)

    def _reset_statuses(self) -> None:
        current = [self.status_list.item(i).text() for i in range(self.status_list.count())]
        if current and current != list(DEFAULT_STATUSES):
            answer = QMessageBox.question(
                self,
                "Reset the status list?",
                "This replaces the list with the defaults, discarding any statuses "
                "you have added.\n\nReset it?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if answer is not QMessageBox.StandardButton.Yes:
                return
        self.status_list.clear()
        self.status_list.addItems(DEFAULT_STATUSES)
        self._refresh_default_statuses(DEFAULT_STATUSES[0])

    def _offer_default_server(self, checked: bool) -> None:
        """Fill in the muuurder.com server when syncing is switched on with none set."""
        if checked and not self.api_url_edit.text().strip():
            self.api_url_edit.setText(DEFAULT_SERVER_URL)

    def _test_connection(self) -> None:
        from ..config import ApiSettings

        url = self.api_url_edit.text().strip().rstrip("/")
        if not url:
            self.test_result_label.setText("Enter a server URL first.")
            return

        probe = ApiSettings(
            base_url=url,
            enabled=True,
            verify_tls=self.verify_tls_check.isChecked(),
            timeout_seconds=5.0,
        )
        self.test_button.setEnabled(False)
        self.test_result_label.setText("Contacting the server…")
        self.test_result_label.setStyleSheet("")
        token = self.api_token_edit.text().strip()
        try:
            with ApiClient(probe, token) as client:
                payload = client.health()
                # /health is deliberately unauthenticated -- it is what this
                # button calls before a token has been typed. So reaching it
                # proves the address and nothing else, and reporting "Connected"
                # on that alone told people a token was good when the very next
                # sync would refuse it. If there is a token, use it for real.
                if token:
                    client.pull_page(case_id=None, cursor=None, limit=1)
        except SubscriptionRequired as exc:
            # Only a token problem is fixed by a new token. For a payment or a
            # subscription problem, "Get a token" sent people to make a token
            # that would be refused exactly like this one; offer the page the
            # server named for this reason instead.
            if exc.reason in ("token_not_linked", "no_account"):
                self._show_tokens_link(payload.get("tokens_url", ""))
            else:
                self._show_tokens_link(exc.upgrade_url, caption="Sort this out at")
            self.test_result_label.setText(str(exc))
            self.test_result_label.setStyleSheet("color:#e3a008;")
        except ApiError as exc:
            self.test_result_label.setText(str(exc))
            self.test_result_label.setStyleSheet("color:#e5484d;")
        except Exception as exc:
            log.exception("Connection test failed")
            self.test_result_label.setText(str(exc))
            self.test_result_label.setStyleSheet("color:#e5484d;")
        else:
            version = payload.get("version", "?")
            self._show_tokens_link(payload.get("tokens_url", ""))
            if token:
                self.test_result_label.setText(
                    f"Connected and the token works. Server version {version}."
                )
            else:
                self.test_result_label.setText(
                    f"Server reached, version {version}. No token entered yet."
                )
            self.test_result_label.setStyleSheet("color:#3fb950;")
        finally:
            self.test_button.setEnabled(True)

    def _show_tokens_link(
        self, url: str, caption: str = "Get a token for this server at"
    ) -> None:
        """Offer a page the server named -- by default the one that hands out tokens."""
        url = (url or "").strip()
        if not url.startswith(("http://", "https://")):
            self.tokens_link_label.setVisible(False)
            return
        safe = html.escape(url, quote=True)
        self.tokens_link_label.setText(
            f'{html.escape(caption)} <a href="{safe}">{safe}</a>'
        )
        self.tokens_link_label.setVisible(True)

    @staticmethod
    def _open_data_folder() -> None:
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(app_paths().data_dir.absolute().as_uri())
