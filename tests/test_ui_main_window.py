"""The main window: menus, cases, export, and the dialogs it opens.

Most of these exist because the behaviour they describe was once wrong in a way
no unit test would have noticed - a command that could not be reached, a prompt
that defaulted to the destructive answer, a label naming a key nobody was bound
to any more.
"""

from __future__ import annotations

import pytest

from .conftest import OBSERVATION

#: Real displays, including the ones that made a 1100x720 minimum unsatisfiable.
#: The guarantee is for every screen, not for the one this happens to run on.
SCREENS = [
    (1024, 600),  # netbook / small laptop
    (1092, 614),  # 1366x768 at 125% scaling
    (1280, 680),  # 1280x720 less a taskbar
    (1280, 720),  # 1920x1080 at 150% scaling
    (1366, 728),  # 1366x768 at 100%
    (1536, 824),  # 1920x1080 at 125%
    (1920, 1040),  # 1080p at 100%
    (2560, 1400),  # QHD
]


def test_entry_commands_surface_a_hidden_log_panel(qt_app) -> None:
    """Edit and Delete lived only in the Log panel's context menu, so a closed
    panel removed every way to change an entry that had already been saved.
    The menu commands must open the panel rather than do nothing."""
    from evidence_review.ui.main_window import MainWindow

    class Dock:
        def __init__(self) -> None:
            self.shown = False

        def isVisible(self) -> bool:  # noqa: N802 - Qt naming
            return False

        def show(self) -> None:
            self.shown = True

        def selected_entry(self):
            return None

    class Bar:
        def __init__(self) -> None:
            self.messages: list[str] = []

        def showMessage(self, text, *_args) -> None:  # noqa: N802 - Qt naming
            self.messages.append(text)

    class Stub:
        def __init__(self) -> None:
            self.log_dock = Dock()
            self._bar = Bar()

        def statusBar(self) -> Bar:  # noqa: N802 - Qt naming
            return self._bar

    stub = Stub()
    assert MainWindow._selected_log_entry(stub) is None
    assert stub.log_dock.shown, "a hidden Log panel must be opened, not silently ignored"
    assert stub._bar.messages, "and the reviewer must be told what to do next"


def test_sync_indicator_names_the_states_that_lose_uploads(qt_app) -> None:
    """A grey glyph with the reason in a tooltip is not enough warning that
    nothing is leaving the machine. Sync being off, with entries queued behind
    it, has to be legible without hovering."""
    from PySide6.QtWidgets import QLabel

    from evidence_review.ui.main_window import MainWindow

    class Bar:
        def showMessage(self, *_args) -> None:  # noqa: N802 - Qt naming
            pass

    class Stub:
        def __init__(self) -> None:
            self.sync_label = QLabel()

        def statusBar(self) -> Bar:  # noqa: N802 - Qt naming
            return Bar()

    stub = Stub()
    MainWindow._set_sync_indicator(stub, "disabled", 2, "Remote sync is off")
    text = stub.sync_label.text()
    assert "Sync off" in text
    assert "2 waiting" in text

    MainWindow._set_sync_indicator(stub, "idle", 0, "Up to date")
    assert "Sync off" not in stub.sync_label.text()


def test_a_server_url_with_sync_unticked_is_queried_not_saved_silently(qt_app, monkeypatch) -> None:
    """A URL and a token with the enable box unticked look exactly like a working
    server, but nothing is ever uploaded and nothing says so. That combination
    cost a reviewer every entry they logged, so it has to be challenged."""
    from PySide6.QtWidgets import QMessageBox

    from evidence_review.config import Settings
    from evidence_review.ui import settings_dialog as module

    asked: list[str] = []

    def fake_question(_parent, title, _text, *_args, **_kwargs):
        asked.append(title)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(module.QMessageBox, "question", staticmethod(fake_question))
    # The dialog otherwise reads and writes the real OS credential store, and a
    # test has no business deleting the machine's API token.
    monkeypatch.setenv("EVREV_API_TOKEN", "evr_test_token")
    monkeypatch.setattr(module, "set_api_token", lambda _token: True)

    settings = Settings()
    dialog = module.SettingsDialog(settings, None)
    dialog.api_url_edit.setText("https://evidence.example.org")
    dialog.api_enabled_check.setChecked(False)
    dialog._on_save()

    assert asked == ["Sync is off"], "saving an unusable server setup must ask first"
    assert settings.api.enabled is True, "answering yes must actually turn sync on"


def test_the_review_menu_can_edit_and_delete_entries(window) -> None:
    """The user-visible half of the same bug: these commands must exist somewhere
    that a closed Log panel cannot take away."""
    from PySide6.QtWidgets import QMenu

    labels = {
        action.text() for menu in window.menuBar().findChildren(QMenu) for action in menu.actions()
    }
    assert "Edit entry…" in labels
    assert "Delete entry" in labels


@pytest.mark.parametrize(("screen_w", "screen_h"), SCREENS)
def test_a_window_never_demands_more_than_the_screen_can_show(screen_w, screen_h) -> None:
    """The main window asked for a minimum of 1100x720. A 1366x768 laptop at 125%
    scaling has 1092x614 to give, so the window could not be shrunk to fit and
    its edges -- with whatever controls were on them -- stayed off the desktop."""
    from PySide6.QtCore import QSize

    from evidence_review.ui.widgets import fitted_size

    available = QSize(screen_w, screen_h)
    # Deliberately larger than several of these screens, in both directions.
    minimum, size = fitted_size(available, QSize(1100, 720), QSize(2400, 1600))

    assert minimum.width() <= screen_w, "a minimum wider than the screen can never be satisfied"
    assert minimum.height() <= screen_h, "nor a taller one"
    assert size.width() <= screen_w
    assert size.height() <= screen_h


@pytest.mark.parametrize(("screen_w", "screen_h"), SCREENS)
def test_fitting_never_inflates_a_window_that_already_fits(screen_w, screen_h) -> None:
    """Clamping is a ceiling, not a target: a small dialog must stay small."""
    from PySide6.QtCore import QSize

    from evidence_review.ui.widgets import fitted_size

    minimum, size = fitted_size(QSize(screen_w, screen_h), QSize(300, 200), QSize(620, 450))
    assert (minimum.width(), minimum.height()) == (300, 200)
    assert (size.width(), size.height()) == (620, 450)


def test_the_main_window_floor_fits_a_small_laptop(window) -> None:
    """fitted_size only helps if the floor it is given is reachable. This guards
    the constant itself, so the 1100x720 minimum cannot quietly come back."""
    assert window.minimumWidth() <= 1024, "minimum width must fit a small laptop screen"
    assert window.minimumHeight() <= 600, "minimum height must fit a small laptop screen"


def test_case_menu_lists_cases_and_marks_the_current_one(window) -> None:
    """The picker is built when it opens, so a case created since launch appears
    without a restart."""
    from evidence_review.models import Case

    window._store.upsert_case(Case(case_id="willow-house", name="Willow House"))
    window._store.upsert_case(Case(case_id="harbour-street", name="Harbour Street"))
    window.switch_case("willow-house", "Willow House")

    window._rebuild_case_menu()
    labels = [action.text() for action in window.case_menu.actions() if action.text()]

    assert any(label.startswith("Willow House") for label in labels)
    assert any(label.startswith("Harbour Street") for label in labels)
    assert "New case…" in labels, "there must be a way to start a case from here"

    checked = [
        action.text()
        for action in window.case_menu.actions()
        if action.isCheckable() and action.isChecked()
    ]
    assert len(checked) == 1 and checked[0].startswith("Willow House")


def test_switching_case_repoints_entry_filing_and_the_log(window) -> None:
    """Switching is what makes several concurrent cases workable: it has to move
    the log, the marks and where new entries are filed together."""
    from .conftest import make_entry

    window.switch_case("willow-house", "Willow House")
    window._store.save(make_entry(case_id="willow-house", values={OBSERVATION: "Knocks."}))
    window._store.save(make_entry(case_id="harbour-street", values={OBSERVATION: "Footsteps."}))

    window.refresh_log()
    assert [e.values[OBSERVATION] for e in window.log_dock.model.entries] == ["Knocks."]

    window.switch_case("harbour-street", "Harbour Street")
    assert window._settings.review.case_id == "harbour-street"
    assert [e.values[OBSERVATION] for e in window.log_dock.model.entries] == ["Footsteps."]


def test_switching_case_does_not_discard_the_pull_cursor(window) -> None:
    """The whole point of storing the cursor on the case: coming back resumes."""
    window.switch_case("willow-house", "Willow House")
    window._store.set_pull_cursor("willow-house", "cursor-w-1")

    window.switch_case("harbour-street", "Harbour Street")
    window.switch_case("willow-house", "Willow House")

    assert window._store.get_pull_cursor("willow-house") == "cursor-w-1"


def test_switching_case_records_it_locally(window) -> None:
    """A case switched to must exist in the local table, or it cannot be switched
    back to from the picker."""
    window.switch_case("harbour-street", "Harbour Street")

    stored = {case.case_id: case.name for case in window._store.list_cases()}
    assert stored.get("harbour-street") == "Harbour Street"


def test_settings_cannot_rename_a_case_as_a_side_effect(window, monkeypatch) -> None:
    """Settings used to hold its own Case ID box and re-slug it on save, so opening
    Settings and pressing Save could change which case you were filing into. Cases
    are the picker's business now; Settings only reports which one is active."""
    from PySide6.QtWidgets import QMessageBox

    from evidence_review.ui import settings_dialog as settings_module
    from evidence_review.ui.settings_dialog import SettingsDialog

    # _on_save can raise modal prompts about shortcut keys, which would block a
    # headless run forever. Answer them rather than avoid exercising the save.
    monkeypatch.setattr(
        settings_module.QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    monkeypatch.setattr(settings_module.QMessageBox, "warning", staticmethod(lambda *a, **k: None))

    window.switch_case("willow-house", "Willow House")

    dialog = SettingsDialog(window._settings, window)
    try:
        assert not hasattr(dialog, "case_id_edit"), "Settings must not edit the case id"
        assert not hasattr(dialog, "case_name_edit"), "Settings must not edit the case name"
        # It still has to say which case is active, or the tab loses the context.
        assert "Willow House" in dialog.case_label.text()
        assert "willow-house" in dialog.case_label.text()

        dialog._on_save()
    finally:
        dialog.deleteLater()

    assert window._settings.review.case_id == "willow-house", "saving must not move the case"
    assert window._settings.review.case_name == "Willow House"


def test_a_case_can_be_renamed_without_changing_its_id(window, monkeypatch) -> None:
    """The id is on every entry already, is the snapshot folder name, and is what
    the server keys on. Renaming must be the safe half of what Settings allowed."""
    from evidence_review.ui import main_window as main_window_module

    from .conftest import make_entry

    window.switch_case("willow-house", "Willow House")
    window._store.save(make_entry(case_id="willow-house", values={OBSERVATION: "Knocks."}))

    monkeypatch.setattr(
        main_window_module.QInputDialog,
        "getText",
        staticmethod(lambda *a, **k: ("Willow House Annexe", True)),
    )
    window._rename_case()

    assert window._settings.review.case_id == "willow-house", "the id must not move"
    assert window._settings.review.case_name == "Willow House Annexe"
    assert "Willow House Annexe" in window.case_button.text()
    assert {c.case_id: c.name for c in window._store.list_cases()}["willow-house"] == (
        "Willow House Annexe"
    )

    # The entry is still in the case, because the id never moved.
    window.refresh_log()
    assert [e.values[OBSERVATION] for e in window.log_dock.model.entries] == ["Knocks."]


def test_first_run_server_fields_are_usable_before_ticking_the_box(qt_app) -> None:
    """They used to be disabled until the checkbox was ticked, which Settings never
    did. Pasting a server URL into a dead field and watching nothing happen is a
    bad first five minutes, and the two places disagreeing is worse."""
    from evidence_review.config import Settings
    from evidence_review.ui.first_run import FirstRunDialog

    settings = Settings()
    settings.api.enabled = False
    wizard = FirstRunDialog(settings)
    try:
        assert wizard.api_url_edit.isEnabled(), "the URL field must accept typing"
        assert wizard.api_token_edit.isEnabled(), "the token field must accept typing"
    finally:
        wizard.deleteLater()


def test_first_run_asks_before_saving_a_url_with_sync_off(qt_app, monkeypatch, tmp_path) -> None:
    """The same question Settings asks, for the same reason: a URL with the box
    unticked looks like a working server but uploads nothing."""
    from PySide6.QtWidgets import QMessageBox

    from evidence_review.config import Settings
    from evidence_review.ui import first_run as first_run_module
    from evidence_review.ui.first_run import FirstRunDialog

    asked: list[str] = []

    def fake_question(_parent, title, _text, *a, **k):
        asked.append(title)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(first_run_module.QMessageBox, "question", staticmethod(fake_question))
    # Never touch the real credential store.
    monkeypatch.setattr(first_run_module, "set_api_token", lambda _token: True)

    settings = Settings()
    settings.api.enabled = False
    monkeypatch.setattr(type(settings), "save", lambda _self: tmp_path / "config.toml")

    wizard = FirstRunDialog(settings)
    try:
        wizard.investigator_edit.setText("Tester")
        wizard.api_url_edit.setText("https://evidence.example.org")
        wizard.api_enabled_check.setChecked(False)
        wizard._on_accept()
    finally:
        wizard.deleteLater()

    assert asked == ["Sync is off"], "it must ask before saving a setup that cannot work"
    assert settings.api.enabled is True, "answering Yes must actually turn sync on"
    assert settings.api.base_url == "https://evidence.example.org"


def test_sync_now_calls_a_method_the_worker_has(window, monkeypatch) -> None:
    """Sync Now called SyncWorker.request_pull(), which was deleted when pulling
    became continuous. Nothing caught it: no test drove the menu item, so Ctrl+R
    raised AttributeError straight into the unhandled-exception box.

    This exercises the real slot against a real SyncWorker, so the wiring is
    checked against the worker's actual API rather than a mock that would accept
    any call at all."""
    from evidence_review.config import ApiSettings
    from evidence_review.sync import SyncWorker

    worker = SyncWorker(
        window._store,
        ApiSettings(base_url="https://evidence.example.org", enabled=True),
        "test-token",
        case_id=window._settings.review.case_id,
    )
    window._sync_worker = worker
    try:
        window._sync_now()  # must not raise
    finally:
        window._sync_worker = None
        worker.deleteLater()


def test_every_menu_slot_is_callable_without_arguments(window) -> None:
    """A menu action calls its slot with no arguments. Anything it then calls on a
    collaborator has to exist - this is the class of bug Sync Now shipped with."""
    import inspect

    slots = [
        window._sync_now,
        window._show_shortcuts,
        window._show_about,
        window._rebuild_case_menu,
    ]
    for slot in slots:
        signature = inspect.signature(slot)
        required = [
            p
            for p in signature.parameters.values()
            if p.default is inspect.Parameter.empty
            and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        ]
        assert not required, f"{slot.__name__} cannot be called from a menu action"


def test_export_offers_the_whole_case_when_the_log_is_filtered(window, monkeypatch) -> None:
    """Export used to take whatever the filter box was showing and report
    "Exported N entries" either way, so a reviewer who had left a word in the
    filter handed over an export silently missing most of the case."""
    from PySide6.QtWidgets import QMessageBox

    from evidence_review.ui import main_window as mw

    from .conftest import make_entry

    window.switch_case("willow-house", "Willow House")
    window._store.save(
        make_entry(case_id="willow-house", values={OBSERVATION: "Knocks in the basement."})
    )
    window._store.save(
        make_entry(case_id="willow-house", values={OBSERVATION: "Footsteps upstairs."})
    )
    window._store.save(make_entry(case_id="willow-house", values={OBSERVATION: "A door closing."}))

    window.log_dock.search_edit.setText("basement")
    window.refresh_log()
    assert len(window.log_dock.model.entries) == 1, "the filter must actually be filtering"

    asked: list[str] = []
    monkeypatch.setattr(
        mw.QMessageBox,
        "question",
        staticmethod(
            lambda _p, title, *a, **k: asked.append(title) or QMessageBox.StandardButton.Yes
        ),
    )
    monkeypatch.setattr(
        mw.QFileDialog,
        "getSaveFileName",
        staticmethod(lambda *a, **k: ("", "")),  # cancel the save; the prompt is the point
    )

    window._export_log()

    assert asked == ["The log is filtered"], "it must say the log is filtered before exporting"


def test_log_dialog_warns_before_discarding_a_typed_observation(window, monkeypatch) -> None:
    """Esc is advertised on the dialog's own Cancel button, and the modal opens the
    instant LOG is pressed. Binning a written-up observation without asking also
    deletes the captured frame on the way out."""
    from PySide6.QtWidgets import QMessageBox

    from evidence_review.autofill import MediaContext
    from evidence_review.ui import log_dialog as log_dialog_module
    from evidence_review.ui.log_dialog import LogEntryDialog

    media = window._paths.data_dir / "clip.mp4"
    media.write_bytes(b"not really a video")

    dialog = LogEntryDialog(
        context=MediaContext(path=media, kind="video", duration_seconds=10.0),
        settings=window._settings,
        store=window._store,
        event_offset_seconds=1.0,
    )
    try:
        # Nothing typed: closing is free.
        assert dialog._has_unsaved_work() is False

        summary = dialog.template.summary_field
        dialog.editor_for(summary.field_id).set_value("Two distinct knocks from the north wall.")
        assert dialog._has_unsaved_work() is True

        answered: list[str] = []
        monkeypatch.setattr(
            log_dialog_module.QMessageBox,
            "question",
            staticmethod(
                lambda _p, title, *a, **k: (
                    answered.append(title) or QMessageBox.StandardButton.Cancel
                )
            ),
        )
        dialog.reject()
        assert answered == ["Discard this observation?"]
        assert dialog.isVisible() or not dialog.result(), "Cancel must keep the dialog open"
    finally:
        dialog.deleteLater()


def test_stop_does_not_disable_logging(window) -> None:
    """Stop used to unload the media and disable LOG, Snapshot and Fullscreen, with
    nothing to re-enable them but reopening the file - a dead end one click away
    from Play, on the button the whole app exists for."""
    window.transport.set_media_loaded(True)

    window._stop()

    assert window.transport.log_button.isEnabled(), "LOG must survive Stop"
    assert window.transport.snapshot_button.isEnabled()


def test_the_sync_indicator_never_shows_a_bare_number(window) -> None:
    """In the healthy-with-backlog case the caption was just "3". Three of what?"""
    window._set_sync_indicator("idle", 3, "3 waiting to upload")
    assert "waiting" in window.sync_label.text()

    window._set_sync_indicator("disabled", 2, "Remote sync is off")
    text = window.sync_label.text()
    assert "Sync off" in text and "2 waiting" in text


def test_a_missing_frame_says_which_kind_of_missing(window, tmp_path) -> None:
    """ "No frame captured" read the same whether capture failed, was switched off,
    or succeeded and then lost the file."""
    from evidence_review.autofill import MediaContext
    from evidence_review.ui.log_dialog import LogEntryDialog

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")
    context = MediaContext(path=media, kind="video", duration_seconds=10.0)

    def build(**kwargs):
        return LogEntryDialog(
            context=context,
            settings=window._settings,
            store=window._store,
            event_offset_seconds=1.0,
            **kwargs,
        )

    window._settings.review.capture_snapshot_on_log = False
    off = build()
    try:
        assert "capture is off" in off.snapshot_label.text()
    finally:
        off.deleteLater()

    window._settings.review.capture_snapshot_on_log = True
    failed = build()
    try:
        assert "could not be captured" in failed.snapshot_label.text()
    finally:
        failed.deleteLater()

    lost = build(snapshot_path=str(tmp_path / "gone.png"))
    try:
        assert "missing from disk" in lost.snapshot_label.text()
    finally:
        lost.deleteLater()


def test_player_step_labels_do_not_name_keys_or_absent_controls(window) -> None:
    """ "Skip button step" named a button the player does not have, and "Arrow key
    step" stopped being true the moment someone used the Shortcuts tab in the
    very same dialog."""
    from evidence_review.ui.settings_dialog import SettingsDialog

    window._settings.shortcuts.overrides = {"seek_back": ["F5"], "seek_forward": ["F6"]}

    dialog = SettingsDialog(window._settings, window)
    try:
        hint = dialog.seek_keys_hint.text()
        assert "F5" in hint and "F6" in hint, "the hint must follow the bindings"
        assert "Arrow" not in hint
    finally:
        dialog.deleteLater()


def test_a_status_is_validated_where_it_is_typed(window, monkeypatch) -> None:
    """An over-long status was accepted in Settings and only rejected much later,
    at log-save time, in a different dialog."""
    from PySide6.QtWidgets import QInputDialog

    from evidence_review.ui import settings_dialog as settings_module
    from evidence_review.ui.settings_dialog import STATUS_LABEL_LIMIT, SettingsDialog

    dialog = SettingsDialog(window._settings, window, store=window._store)
    try:
        before = dialog.status_list.count()
        warned: list[str] = []
        monkeypatch.setattr(
            settings_module.QMessageBox,
            "warning",
            staticmethod(lambda _p, title, *a, **k: warned.append(title)),
        )
        monkeypatch.setattr(
            QInputDialog,
            "getText",
            staticmethod(lambda *a, **k: ("x" * (STATUS_LABEL_LIMIT + 1), True)),
        )
        dialog._add_status()

        assert warned == ["Status is too long"]
        assert dialog.status_list.count() == before, "it must not have been added"
    finally:
        dialog.deleteLater()


def test_date_reviewed_cannot_be_in_the_future(window, tmp_path) -> None:
    """It goes into the export as a fact about when the evidence was looked at."""
    from PySide6.QtCore import QDate

    from evidence_review.autofill import MediaContext
    from evidence_review.ui.log_dialog import LogEntryDialog

    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")

    dialog = LogEntryDialog(
        context=MediaContext(path=media, kind="video", duration_seconds=10.0),
        settings=window._settings,
        store=window._store,
        event_offset_seconds=1.0,
    )
    try:
        assert dialog.date_reviewed_edit.maximumDate() == QDate.currentDate()
        dialog.date_reviewed_edit.setDate(QDate.currentDate().addDays(5))
        assert dialog.date_reviewed_edit.date() <= QDate.currentDate()
    finally:
        dialog.deleteLater()


def test_the_volume_tooltip_says_the_number_and_flags_amplification(window) -> None:
    """The slider runs to 130 and the tooltip was the bare word "Volume", so
    nothing said where 100% was or that above it is amplification - which matters
    when the thing being reviewed is a faint sound."""
    transport = window.transport

    transport.set_volume(100)
    assert "100%" in transport.volume_slider.toolTip()
    assert "amplified" not in transport.volume_slider.toolTip()

    transport.set_volume(125)
    assert "125%" in transport.volume_slider.toolTip()
    assert "amplified" in transport.volume_slider.toolTip()


def test_areas_can_be_seeded_before_anything_is_logged(window, monkeypatch, tmp_path) -> None:
    """review.areas was read by the LOG modal and writable by nothing, so the Area
    dropdown's seed list was permanently empty. Knowing a building's rooms before
    the first entry exists is the normal case."""
    from PySide6.QtWidgets import QInputDialog

    from evidence_review.autofill import MediaContext
    from evidence_review.ui.log_dialog import LogEntryDialog
    from evidence_review.ui.settings_dialog import SettingsDialog

    window._settings.review.areas = []
    dialog = SettingsDialog(window._settings, window, store=window._store)
    try:
        monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Cellar", True)))
        dialog._add_area()
        monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Loft", True)))
        dialog._add_area()
        assert dialog.area_list.count() == 2

        window._settings.review.areas = [
            dialog.area_list.item(i).text() for i in range(dialog.area_list.count())
        ]
    finally:
        dialog.deleteLater()

    # The modal must now offer them.
    window._settings.review.guess_area_from_path = False
    media = tmp_path / "clip.mp4"
    media.write_bytes(b"x")
    modal = LogEntryDialog(
        context=MediaContext(path=media, kind="video", duration_seconds=10.0),
        settings=window._settings,
        store=window._store,
        event_offset_seconds=1.0,
    )
    try:
        # The seeded list reaches the template's location field, found by role.
        combo = modal.editor_for(modal.template.location_field.field_id).inner
        offered = [combo.itemText(index) for index in range(combo.count())]
        assert "Cellar" in offered and "Loft" in offered
    finally:
        modal.deleteLater()


def test_still_images_get_zoom_and_rotate_controls(window, tmp_path) -> None:
    """The image backend has had zoom and rotate all along: zoom_changed was
    emitted to nobody, rotate_by was never called, and zoom was mouse-wheel only
    with no way back to fit. A reviewer who scrolled into a photograph was stuck
    there, and a sideways one could not be straightened."""
    transport = window.transport

    # Playback mode: the image controls stay out of the way.
    transport.set_playback_mode(True)
    assert not transport.rotate_button.isVisibleTo(transport)
    assert transport.play_button.isVisibleTo(transport)

    # Still image: the transport goes, these arrive.
    transport.set_playback_mode(False)
    assert transport.rotate_button.isVisibleTo(transport)
    assert transport.fit_button.isVisibleTo(transport)
    assert transport.zoom_in_button.isVisibleTo(transport)
    assert transport.zoom_out_button.isVisibleTo(transport)
    assert not transport.play_button.isVisibleTo(transport)


def test_the_image_controls_reach_the_backend(window) -> None:
    """Signals wired to nothing are what this whole feature was before."""
    calls: list[tuple[str, object]] = []
    window.image_surface.zoom_by = lambda f: calls.append(("zoom", f))
    window.image_surface.fit_to_window = lambda: calls.append(("fit", None))
    window.image_surface.rotate_by = lambda d: calls.append(("rotate", d))

    window.transport.zoom_in_button.click()
    window.transport.zoom_out_button.click()
    window.transport.fit_button.click()
    window.transport.rotate_button.click()

    kinds = [kind for kind, _ in calls]
    assert kinds == ["zoom", "zoom", "fit", "rotate"]
    assert calls[0][1] > 1, "zoom in must magnify"
    assert calls[1][1] < 1, "zoom out must shrink"
    assert calls[3][1] == 90


def test_image_mode_restores_controls_the_video_row_had_shed(qt_app) -> None:
    """_fit_controls drops optional controls to make a narrow video row fit, and
    returns early when not in playback mode - so anything it had shed stayed
    hidden for still images, which have six fewer controls and room to spare.
    Previous file simply vanished on a narrow window."""
    from PySide6.QtWidgets import QAbstractButton

    from evidence_review.ui.transport_bar import TransportBar

    bar = TransportBar()
    try:
        bar.resize(900, 90)  # narrow enough that the video row must shed controls
        bar.show()
        bar.set_playback_mode(True)
        shed = [w for w in bar._optional_widgets if not w.isVisibleTo(bar)]
        assert shed, "the row must actually be dropping something at this width"

        bar.set_playback_mode(False)
        visible = {b.toolTip() for b in bar.findChildren(QAbstractButton) if b.isVisibleTo(bar)}
        assert "Previous file (PgUp)" in visible
        assert "Next file (PgDn)" in visible
    finally:
        bar.deleteLater()


# --------------------------------------------------------------------------- #
# The volume control, reported from a real session as missing
# --------------------------------------------------------------------------- #


def test_the_volume_control_is_labelled_like_every_other_control(window) -> None:
    """Reported during a real test as "I see no volume control."

    It was there: a 22px button reading U+266B BEAMED EIGHTH NOTES, and a bare 90px
    slider. A music note says "audio", not "volume", and "Speed" sitting right beside it
    had a visible label while this did not -- so the eye read the pair as decoration
    belonging to the speed control.
    """
    bar = window.transport

    assert bar.volume_label.text() == "Volume"
    assert bar.volume_label.isVisibleTo(bar)
    # The convention it now matches.
    assert bar.speed_label.text() == "Speed"


def test_the_mute_button_shows_a_speaker_not_a_music_note(window) -> None:
    """A drawn speaker, for the reason icons.py exists: every speaker character on
    Windows lives in Segoe UI Emoji, so it arrives as a colour bitmap -- teal, and the
    muted one bright red -- among a row of monochrome theme-coloured controls."""
    bar = window.transport

    assert bar.mute_button.text() == "", "a text glyph is back on the mute button"
    assert not bar.mute_button.icon().isNull(), "the mute button has no icon"


def test_muting_changes_the_icon(window) -> None:
    """The button is the only thing that says whether sound is coming out, so it has to
    actually change -- and it used to, by swapping one character for another."""
    from PySide6.QtCore import QSize

    bar = window.transport
    size = QSize(16, 16)

    before = bar.mute_button.icon().pixmap(size).toImage()
    bar.mute_button.setChecked(True)
    after = bar.mute_button.icon().pixmap(size).toImage()

    assert before != after, "muting left the button looking identical"


def test_the_speaker_and_the_muted_speaker_are_different_drawings(qt_app) -> None:
    """Drawn, cached by argument -- so a cache keyed carelessly would serve one as the
    other and the button would stop reporting the state."""
    from PySide6.QtCore import QSize

    from evidence_review.ui.icons import speaker_icon

    size = QSize(16, 16)
    plain = speaker_icon("#e6e6e6", 16).pixmap(size).toImage()
    muted = speaker_icon("#e6e6e6", 16, muted=True).pixmap(size).toImage()

    assert plain != muted, "the cache is serving the muted speaker as the plain one"

    # And colour is part of the key too, since the icon is re-tinted when muted.
    other = speaker_icon("#9aa3ad", 16).pixmap(size).toImage()
    assert plain != other, "the cache ignores the colour"


# --------------------------------------------------------------------------- #
# The Session Log follows the file that is open
# --------------------------------------------------------------------------- #


def _entry_for(store, path, *, case="default", offset=12.0, text="A knock."):
    from pathlib import Path

    from evidence_review.models import EntryRow, MediaKind

    return store.save(
        EntryRow(
            case_id=case,
            file_name=Path(path).name,
            media_path=str(path),
            media_kind=MediaKind.VIDEO,
            event_offset_seconds=offset,
            values={"builtin-paranormal.observation": text},
        )
    )


def test_the_log_shows_the_open_file_not_the_whole_case(window, tmp_path) -> None:
    """Reported as "logs are not attached to the files".

    With a recording open the log was filling with entries from a video logged days
    earlier, while the timeline beside it carried no marks at all -- because the marks
    were filtered to the open file and the log was not. Two panels describing different
    things, with nothing saying so.
    """
    from evidence_review.ui.main_window import MediaContext

    this_file = tmp_path / "Session 1 - cellar.wav"
    other_file = tmp_path / "Adobe House.MP4"
    mine = _entry_for(window._store, this_file, case=window._settings.review.case_id)
    _entry_for(window._store, other_file, case=window._settings.review.case_id,
               offset=30.0, text="Something else entirely")

    window._media_context = MediaContext(
        path=this_file, kind="audio", duration_seconds=60.0, sha256=None
    )
    window.refresh_log()

    shown = [e.entry_id for e in window.log_dock.model.entries]
    assert shown == [mine.entry_id], "the log is showing another file's work"


def test_all_files_shows_the_whole_case_again(window, tmp_path) -> None:
    """Reviewing a case rather than a file is a reasonable thing to want, so it is one
    click away rather than gone."""
    from evidence_review.ui.main_window import MediaContext

    this_file = tmp_path / "cellar.wav"
    other_file = tmp_path / "landing.wav"
    _entry_for(window._store, this_file, case=window._settings.review.case_id)
    _entry_for(window._store, other_file, case=window._settings.review.case_id, offset=30.0)

    window._media_context = MediaContext(
        path=this_file, kind="audio", duration_seconds=60.0, sha256=None
    )
    window.refresh_log()
    assert len(window.log_dock.model.entries) == 1

    window.log_dock.all_files_check.setChecked(True)
    assert len(window.log_dock.model.entries) == 2, "All files did not widen the view"


def test_with_nothing_open_the_log_shows_the_case(window, tmp_path) -> None:
    """Scoping to the open file must not mean an empty log when none is open."""
    _entry_for(window._store, tmp_path / "a.mp4", case=window._settings.review.case_id)
    _entry_for(window._store, tmp_path / "b.mp4", case=window._settings.review.case_id,
               offset=40.0)

    window._media_context = None
    window.refresh_log()

    assert len(window.log_dock.model.entries) == 2


def test_an_empty_scoped_log_does_not_claim_the_case_is_empty(window, tmp_path) -> None:
    """"No entries yet" is a far more alarming claim than "nothing against this file",
    and the reviewer has no way to tell which one is meant."""
    from evidence_review.ui.main_window import MediaContext

    _entry_for(window._store, tmp_path / "other.mp4", case=window._settings.review.case_id)
    window._media_context = MediaContext(
        path=tmp_path / "fresh.wav", kind="audio", duration_seconds=10.0, sha256=None
    )
    window.refresh_log()

    said = window.log_dock.count_label.text()
    assert not window.log_dock.model.entries
    assert "this file" in said.lower(), said
    assert "All files" in said, "it does not say how to see the rest of the case"


# --------------------------------------------------------------------------- #
# The entry commands say when they cannot run
# --------------------------------------------------------------------------- #


def test_entry_commands_are_greyed_out_with_nothing_selected(window, tmp_path) -> None:
    """Asked whether a confirmation box appeared when Delete was pressed: "No box
    appeared at all." That is what Review -> Delete Entry did with no row selected --
    a five-second status-bar line at the bottom of a crowded window. A destructive
    command that silently does nothing is indistinguishable from a broken one.
    """
    window._media_context = None
    window.refresh_log()

    assert window._entry_actions, "no entry commands were registered"
    for action in window._entry_actions:
        assert not action.isEnabled(), f"{action.text()!r} is offered with nothing selected"


def test_entry_commands_come_back_when_a_row_is_selected(window, tmp_path) -> None:
    from PySide6.QtCore import QItemSelectionModel

    _entry_for(window._store, tmp_path / "a.mp4", case=window._settings.review.case_id)
    window._media_context = None
    window.refresh_log()

    index = window.log_dock.model.index(0, 0)
    window.log_dock.table.selectionModel().select(
        index,
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )

    for action in window._entry_actions:
        assert action.isEnabled(), f"{action.text()!r} stayed greyed out with a row selected"


# --------------------------------------------------------------------------- #
# Annotation, reachable from the video rather than only from a logged row
# --------------------------------------------------------------------------- #


def test_the_transport_offers_annotation_on_a_video(window) -> None:
    """Reported as "where is that feature? I don't see it."

    It was reachable only by logging an event, then finding that row in the Session
    Log's right-click menu -- so nothing on screen while watching said it existed, and
    you had to know that logging was the way in before you could discover it.
    """
    bar = window.transport
    # Wide enough that nothing is shed, so this measures the mode rather than the fit:
    # an unshown window leaves the bar at 640px, where everything optional goes and the
    # assertion would pass or fail for the wrong reason.
    bar.resize(1900, bar.height())
    bar.set_media_loaded(True)
    bar.set_playback_mode(True)
    bar.set_audio_mode(False)
    bar._fit_controls()

    assert bar.annotate_button.isVisibleTo(bar)
    assert bar.annotate_button.isEnabled()
    assert not bar.annotate_button.icon().isNull(), "the button has no icon"


def test_the_annotate_button_stays_out_of_the_way_for_a_recording(window) -> None:
    """Annotations are drawn on a picture, and a recording has none. Offering it there
    is the dead end the context menu already avoids."""
    bar = window.transport
    bar.resize(1900, bar.height())  # so the fit is not what hides it
    bar.set_media_loaded(True)
    bar.set_playback_mode(True)
    bar.set_audio_mode(True)
    bar._fit_controls()

    assert not bar.annotate_button.isVisibleTo(bar)

    bar.set_playback_mode(False)  # a still image
    bar._fit_controls()
    assert not bar.annotate_button.isVisibleTo(bar)


def test_annotating_picks_the_event_the_playhead_is_in(window, tmp_path, monkeypatch) -> None:
    """The reviewer is watching, not managing a list. If they are inside a logged
    event, that is the one they mean."""
    from evidence_review.ui.main_window import MediaContext

    video = tmp_path / "hallway.mp4"
    here = _entry_for(window._store, video, case=window._settings.review.case_id, offset=30.0)
    _entry_for(window._store, video, case=window._settings.review.case_id, offset=90.0)

    window._media_context = MediaContext(
        path=video, kind="video", duration_seconds=120.0, sha256=None
    )
    monkeypatch.setattr(type(window), "current_playback_position", lambda self: 30.4)

    assert window._entry_to_annotate().entry_id == here.entry_id


def test_a_selected_row_wins_over_the_playhead(window, tmp_path, monkeypatch) -> None:
    """Picking a row is a deliberate act; where the player happens to sit is not."""
    from PySide6.QtCore import QItemSelectionModel

    from evidence_review.ui.main_window import MediaContext

    video = tmp_path / "hallway.mp4"
    _entry_for(window._store, video, case=window._settings.review.case_id, offset=30.0)
    _entry_for(window._store, video, case=window._settings.review.case_id, offset=90.0)

    window._media_context = MediaContext(
        path=video, kind="video", duration_seconds=120.0, sha256=None
    )
    window.refresh_log()
    monkeypatch.setattr(type(window), "current_playback_position", lambda self: 30.4)

    index = window.log_dock.model.index(0, 0)
    window.log_dock.table.selectionModel().select(
        index,
        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
    )
    chosen = window.log_dock.model.entry_at(0)

    assert window._entry_to_annotate().entry_id == chosen.entry_id


def test_nowhere_near_an_event_there_is_nothing_to_annotate(window, tmp_path, monkeypatch) -> None:
    """Far from any logged event the command has to start one rather than silently
    draw on an unrelated moment."""
    from evidence_review.ui.main_window import MediaContext

    video = tmp_path / "hallway.mp4"
    _entry_for(window._store, video, case=window._settings.review.case_id, offset=30.0)
    window._media_context = MediaContext(
        path=video, kind="video", duration_seconds=600.0, sha256=None
    )
    monkeypatch.setattr(type(window), "current_playback_position", lambda self: 400.0)

    assert window._entry_to_annotate() is None


def test_another_file_s_events_are_never_offered(window, tmp_path, monkeypatch) -> None:
    """Drawing on this video a mark belonging to a different recording would attach
    evidence to the wrong file."""
    from evidence_review.ui.main_window import MediaContext

    mine = tmp_path / "hallway.mp4"
    theirs = tmp_path / "cellar.mp4"
    _entry_for(window._store, theirs, case=window._settings.review.case_id, offset=30.0)

    window._media_context = MediaContext(
        path=mine, kind="video", duration_seconds=120.0, sha256=None
    )
    monkeypatch.setattr(type(window), "current_playback_position", lambda self: 30.0)

    assert window._entry_to_annotate() is None


def test_the_annotate_command_needs_a_video_not_a_selection(window, tmp_path) -> None:
    """Greying it out until a row was selected is most of why it could not be found."""
    from evidence_review.ui.main_window import MediaContext

    window._media_context = None
    window.refresh_log()
    assert not window._annotate_action.isEnabled()

    window._media_context = MediaContext(
        path=tmp_path / "hallway.mp4", kind="video", duration_seconds=10.0, sha256=None
    )
    window.refresh_log()
    assert window._annotate_action.isEnabled(), (
        "the command is still gated on a selection, which is what hid it"
    )


def test_annotating_a_recording_says_why_not(window, tmp_path, monkeypatch) -> None:
    from evidence_review.ui.main_window import MediaContext

    said = {}
    monkeypatch.setattr(
        "evidence_review.ui.main_window.QMessageBox.information",
        lambda parent, title, text, *a, **k: said.update(title=title, text=text),
    )
    window._media_context = MediaContext(
        path=tmp_path / "cellar.wav", kind="audio", duration_seconds=10.0, sha256=None
    )

    window.annotate_here()

    assert said, "it refused silently"
    assert "waveform" in said["text"], said


def test_switching_sync_on_offers_the_real_server(qt_app, tmp_path) -> None:
    """The server box used to show "https://evidence.example.org", so nobody was
    told where Evidence Review syncs. Ticking the box with no address now fills in
    muuurder.com; an address already typed is left alone."""
    from evidence_review.config import DEFAULT_SERVER_URL, Settings
    from evidence_review.store import LocalStore
    from evidence_review.ui.settings_dialog import SettingsDialog

    assert DEFAULT_SERVER_URL == "https://muuurder.com/evidencereview"
    store = LocalStore(tmp_path / "e.db")
    try:
        dialog = SettingsDialog(settings=Settings(), store=store)
        assert dialog.api_url_edit.placeholderText() == DEFAULT_SERVER_URL
        dialog.api_enabled_check.setChecked(False)
        dialog.api_url_edit.setText("")
        dialog.api_enabled_check.setChecked(True)
        assert dialog.api_url_edit.text() == DEFAULT_SERVER_URL

        dialog.api_enabled_check.setChecked(False)
        dialog.api_url_edit.setText("https://my.own.server/ev")
        dialog.api_enabled_check.setChecked(True)
        assert dialog.api_url_edit.text() == "https://my.own.server/ev"
    finally:
        store.close()


def test_first_run_offers_the_real_server_too(qt_app, tmp_path, monkeypatch) -> None:
    from evidence_review.config import DEFAULT_SERVER_URL, Settings
    from evidence_review.ui.first_run import FirstRunDialog

    monkeypatch.setenv("EVREV_HOME", str(tmp_path))
    wizard = FirstRunDialog(Settings())
    assert wizard.api_url_edit.placeholderText() == DEFAULT_SERVER_URL
    wizard.api_enabled_check.setChecked(False)
    wizard.api_url_edit.setText("")
    wizard.api_enabled_check.setChecked(True)
    assert wizard.api_url_edit.text() == DEFAULT_SERVER_URL
