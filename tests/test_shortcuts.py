"""Configurable key bindings.

The registry is the single source of truth, so the tests that matter most are the
ones that stop it drifting away from the code that actually wires the keys up.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from evidence_review import shortcuts as registry

# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ctrl+l", "Ctrl+L"),
        ("Ctrl+L", "Ctrl+L"),
        ("CTRL+L", "Ctrl+L"),
        ("space", "Space"),
        ("shift+right", "Shift+Right"),
        ("", ""),
        ("   ", ""),
        ("not a key", ""),
    ],
)
def test_key_sequences_are_canonicalised(raw: str, expected: str) -> None:
    """Storage and comparison both go through this, so "ctrl+l" and "Ctrl+L"
    cannot end up looking like two different bindings."""
    assert registry.normalise(raw) == expected


def test_duplicates_and_blanks_are_dropped() -> None:
    assert registry.normalise_all(["Ctrl+L", "ctrl+l", "", "P"]) == ["Ctrl+L", "P"]


def test_the_defaults_do_not_clash() -> None:
    """Two actions on one key would make which one runs unpredictable."""
    assert registry.conflicts(registry.default_bindings()) == {}


def test_every_action_has_an_id_label_and_group() -> None:
    ids = [action.id for action in registry.ACTIONS]
    assert len(ids) == len(set(ids)), "duplicate action ids"
    for action in registry.ACTIONS:
        assert action.label.strip()
        assert action.group in registry.GROUP_ORDER


def test_overrides_are_merged_onto_defaults() -> None:
    bindings = registry.resolve({"mark_toggle": ["ctrl+m"]})
    assert bindings["mark_toggle"] == ["Ctrl+M"]
    assert bindings["play_pause"] == ["K"], "untouched actions keep their default"


def test_an_empty_override_means_deliberately_unbound() -> None:
    """Distinct from "never touched", which would pick the default back up."""
    bindings = registry.resolve({"play_pause": []})
    assert bindings["play_pause"] == []


def test_unknown_actions_in_the_config_are_ignored() -> None:
    """A config written by a different build must not break startup."""
    bindings = registry.resolve({"from_the_future": ["Ctrl+Z"]})
    assert "from_the_future" not in bindings
    assert bindings["mark_toggle"] == ["Space"]


def test_only_deviations_are_stored() -> None:
    bindings = registry.resolve({"mark_toggle": ["Ctrl+M"]})
    overrides = registry.overrides_from(bindings)
    assert overrides == {"mark_toggle": ["Ctrl+M"]}, "defaults must not be written out"


def test_overrides_round_trip() -> None:
    original = {"mark_toggle": ["Ctrl+M"], "play_pause": []}
    assert registry.overrides_from(registry.resolve(original)) == original


def test_conflicts_are_reported_with_their_owners() -> None:
    clashes = registry.conflicts({"a": ["Ctrl+M"], "b": ["ctrl+m"], "c": ["P"]})
    assert clashes == {"Ctrl+M": ["a", "b"]}


def test_describe_is_tooltip_ready() -> None:
    bindings = registry.resolve(None)
    assert registry.describe(bindings, "mark_toggle") == " (Space)"
    assert registry.describe(registry.resolve({"mark_toggle": []}), "mark_toggle") == ""


# --------------------------------------------------------------------------- #
# The wiring contract
# --------------------------------------------------------------------------- #


def test_every_action_is_wired_to_something(window) -> None:
    """Adding an action to the registry without wiring it would give the user a
    key in Settings that silently does nothing."""
    handlers = set(window._action_handlers())
    menu_actions = set(window.shortcuts._menu_actions)
    wired = handlers | menu_actions

    missing = [action.id for action in registry.ACTIONS if action.id not in wired]
    assert not missing, f"actions in the registry with no handler or menu entry: {missing}"


def test_no_action_is_wired_twice(window) -> None:
    """A menu entry owns its own shortcut; a second QShortcut for the same action
    would fire it twice or trip Qt's ambiguous-shortcut handling."""
    handlers = set(window._action_handlers())
    menu_actions = set(window.shortcuts._menu_actions)
    assert not (handlers & menu_actions), f"wired twice: {sorted(handlers & menu_actions)}"


def test_handlers_name_real_actions(window) -> None:
    unknown = set(window._action_handlers()) - set(registry.ACTIONS_BY_ID)
    assert not unknown, f"handlers for actions that do not exist: {sorted(unknown)}"


# --------------------------------------------------------------------------- #
# Applying bindings
# --------------------------------------------------------------------------- #


def test_rebinding_replaces_the_old_key(window) -> None:
    from PySide6.QtGui import QKeySequence, QShortcut

    def bound_keys() -> set[str]:
        return {
            s.key().toString(QKeySequence.SequenceFormat.PortableText)
            for s in window.findChildren(QShortcut)
        }

    assert "Space" in bound_keys()

    window._settings.shortcuts.overrides = {"mark_toggle": ["Ctrl+M"]}
    window.apply_shortcuts()

    keys = bound_keys()
    assert "Ctrl+M" in keys, "the new key was not bound"
    assert "Space" not in keys, "the old key is still bound"


def test_unbinding_removes_the_key(window) -> None:
    from PySide6.QtGui import QKeySequence, QShortcut

    window._settings.shortcuts.overrides = {"mark_toggle": []}
    window.apply_shortcuts()

    keys = {
        s.key().toString(QKeySequence.SequenceFormat.PortableText)
        for s in window.findChildren(QShortcut)
    }
    assert "Space" not in keys


def test_menu_entries_show_their_current_key(window) -> None:
    """The menu is where most people look for a shortcut, so it has to track."""
    from PySide6.QtGui import QKeySequence

    action = window.shortcuts._menu_actions["open_file"]
    assert action.shortcut().toString(QKeySequence.SequenceFormat.PortableText) == "Ctrl+O"

    window._settings.shortcuts.overrides = {"open_file": ["Ctrl+Shift+F"]}
    window.apply_shortcuts()
    assert action.shortcut().toString(QKeySequence.SequenceFormat.PortableText) == "Ctrl+Shift+F"


def test_tooltips_follow_the_binding(window) -> None:
    """A tooltip that still names the old key is a lie the user will trust."""
    assert "(K)" in window.transport.play_button.toolTip()

    window._settings.shortcuts.overrides = {"play_pause": ["Ctrl+Space"]}
    window.apply_shortcuts()
    assert "(Ctrl+Space)" in window.transport.play_button.toolTip()
    assert "(K)" not in window.transport.play_button.toolTip()


def test_the_help_dialog_is_generated_from_live_bindings(window) -> None:
    window._settings.shortcuts.overrides = {"mark_toggle": ["Ctrl+M"]}
    window.apply_shortcuts()
    html = window.shortcuts.help_html()
    assert "Ctrl+M" in html
    assert "Mark event start / end" in html


def test_unbound_actions_are_listed_as_such(window) -> None:
    window._settings.shortcuts.overrides = {"toggle_repeat": []}
    window.apply_shortcuts()
    html = window.shortcuts.help_html()
    assert "Unbound:" in html
    assert "Repeat the current event" in html


def test_a_handler_for_an_unknown_action_is_rejected(window) -> None:
    with pytest.raises(KeyError):
        window.shortcuts.set_handlers({"no_such_action": lambda: None})


# --------------------------------------------------------------------------- #
# Real key presses
# --------------------------------------------------------------------------- #


def test_a_rebound_key_actually_fires_the_action(window) -> None:
    """Binding-level checks can pass while nothing happens on the keyboard, so
    this drives real key events through Qt."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    def press(key, modifier=Qt.KeyboardModifier.NoModifier):
        # Shortcuts only activate for a window Qt considers active, which needs
        # the event loop pumped after showing and between presses.
        QTest.keyClick(window, key, modifier)
        QApplication.processEvents()

    window.show()
    QApplication.processEvents()

    fired: list[str] = []
    window.toggle_mark = lambda: fired.append("mark")
    window.apply_shortcuts()

    press(Qt.Key_Space)
    assert fired == ["mark"], "the default key did nothing"

    press(Qt.Key_M, Qt.KeyboardModifier.ControlModifier)
    assert fired == ["mark"], "an unbound key fired the action"

    window._settings.shortcuts.overrides = {"mark_toggle": ["Ctrl+M"]}
    window.apply_shortcuts()
    fired.clear()

    press(Qt.Key_Space)
    assert fired == [], "the old key still fires after rebinding"

    press(Qt.Key_M, Qt.KeyboardModifier.ControlModifier)
    assert fired == ["mark"], "the new key does not fire"


def test_an_unbound_action_cannot_be_triggered(window) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    window.show()
    QApplication.processEvents()

    fired: list[str] = []
    window.toggle_mark = lambda: fired.append("mark")
    window._settings.shortcuts.overrides = {"mark_toggle": []}
    window.apply_shortcuts()

    QTest.keyClick(window, Qt.Key_Space)
    QApplication.processEvents()
    assert fired == []


# --------------------------------------------------------------------------- #
# The editor
# --------------------------------------------------------------------------- #


@pytest.fixture
def editor(qt_app):
    from evidence_review.ui.shortcut_editor import ShortcutEditor

    return ShortcutEditor(registry.resolve(None))


def test_the_editor_starts_from_the_current_bindings(editor) -> None:
    assert editor.bindings()["mark_toggle"] == ["Space"]
    assert editor.bindings()["log_event"] == ["Return", "Enter"]


def test_assigning_a_taken_key_removes_it_from_the_old_action(editor) -> None:
    """Leaving both bound would make Qt pick between them arbitrarily."""
    from PySide6.QtGui import QKeySequence

    primary, _alternate = editor._rows["toggle_repeat"]
    primary.setKeySequence(QKeySequence("Space"))  # Space belongs to mark_toggle

    bindings = editor.bindings()
    assert bindings["toggle_repeat"] == ["Space"]
    assert bindings["mark_toggle"] == [], "the old owner kept the key"
    assert editor.conflicts() == {}
    assert "taken from" in editor.note_label.text()


def test_resetting_one_action_restores_its_defaults(editor) -> None:
    from PySide6.QtGui import QKeySequence

    editor._rows["mark_toggle"][0].setKeySequence(QKeySequence("Ctrl+M"))
    assert editor.bindings()["mark_toggle"] == ["Ctrl+M"]

    editor._reset_action(registry.ACTIONS_BY_ID["mark_toggle"])
    assert editor.bindings()["mark_toggle"] == ["Space"]


def test_resetting_everything_restores_every_default(editor) -> None:
    from PySide6.QtGui import QKeySequence

    editor._rows["mark_toggle"][0].setKeySequence(QKeySequence("Ctrl+M"))
    editor._rows["play_pause"][0].clear()

    editor.reset_all()
    assert editor.bindings() == registry.default_bindings()
    assert editor.conflicts() == {}


def test_clearing_a_key_unbinds_the_action(editor) -> None:
    """Qt's built-in clear button is invisible against the dark theme, so each row
    has an explicit Clear of its own."""
    editor._clear_action(registry.ACTIONS_BY_ID["log_event"])
    assert editor.bindings()["log_event"] == [], "both slots should be cleared"
    assert "unbound" in editor.note_label.text()


def test_clearing_is_not_the_same_as_resetting(editor) -> None:
    editor._clear_action(registry.ACTIONS_BY_ID["toggle_repeat"])
    assert editor.bindings()["toggle_repeat"] == []
    editor._reset_action(registry.ACTIONS_BY_ID["toggle_repeat"])
    assert editor.bindings()["toggle_repeat"] == ["R"]


def test_the_editor_never_produces_a_conflict(editor) -> None:
    from PySide6.QtGui import QKeySequence

    for action_id in ("toggle_repeat", "next_mark", "previous_mark"):
        editor._rows[action_id][0].setKeySequence(QKeySequence("Ctrl+J"))
    assert editor.conflicts() == {}, "only the last action should hold Ctrl+J"
    assert editor.bindings()["previous_mark"] == ["Ctrl+J"]
    assert editor.bindings()["toggle_repeat"] == []
    assert editor.bindings()["next_mark"] == []


def test_filtering_hides_unrelated_rows(editor) -> None:
    editor._apply_filter("repeat")
    visible = [
        editor.table.item(row, 0).text().strip()
        for row in range(editor.table.rowCount())
        if not editor.table.isRowHidden(row) and editor.table.item(row, 0)
    ]
    assert visible == ["Repeat the current event"]

    editor._apply_filter("")
    shown = sum(1 for row in range(editor.table.rowCount()) if not editor.table.isRowHidden(row))
    assert shown == editor.table.rowCount()


# --------------------------------------------------------------------------- #
# Keys the system takes first
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sequence",
    [
        "Ctrl+Alt+Del",
        "Alt+Tab",
        "Alt+Shift+Tab",
        "Ctrl+Esc",
        "Ctrl+Shift+Esc",
        "Alt+Esc",
        "Print",
        "Meta+L",
        "Meta+Shift+S",
    ],
)
def test_keys_windows_keeps_are_flagged_as_blocked(sequence: str) -> None:
    """Binding one of these looks like it worked and then silently never fires."""
    warning = registry.warn_about(sequence)
    assert warning is not None, f"{sequence} was not flagged"
    assert warning.is_blocked
    assert warning.message.strip()


@pytest.mark.parametrize(
    ("sequence", "expected_in_message"),
    [
        ("Alt+F4", "closes the window"),
        ("Tab", "focus"),
        ("Shift+Tab", "focus"),
        ("F10", "menu bar"),
        ("Alt+F", "File menu"),
        ("Alt+R", "Review menu"),
        ("Alt+V", "View menu"),
        ("Alt+H", "Help menu"),
    ],
)
def test_keys_that_already_do_something_are_flagged_as_risky(
    sequence: str, expected_in_message: str
) -> None:
    warning = registry.warn_about(sequence)
    assert warning is not None, f"{sequence} was not flagged"
    assert not warning.is_blocked, f"{sequence} should be usable, just unwise"
    assert expected_in_message in warning.message


@pytest.mark.parametrize(
    "sequence", ["Ctrl+M", "Space", "K", "Ctrl+Shift+P", "F5", "Alt+M", "Ctrl+Alt+K", ""]
)
def test_ordinary_keys_are_not_flagged(sequence: str) -> None:
    assert registry.warn_about(sequence) is None


def test_the_defaults_are_all_usable() -> None:
    """Shipping a default the system swallows would be embarrassing."""
    flagged = registry.warnings_for(registry.default_bindings())
    assert flagged == {}, f"default bindings the system would take: {flagged}"


def test_the_worst_warning_wins_for_an_action() -> None:
    """An action with one risky and one blocked key is reported as blocked."""
    flagged = registry.warnings_for({"mark_toggle": ["Tab", "Alt+Tab"]})
    assert flagged["mark_toggle"].is_blocked


def test_the_editor_flags_a_blocked_key(editor) -> None:
    from PySide6.QtGui import QKeySequence

    editor._rows["toggle_repeat"][0].setKeySequence(QKeySequence("Alt+Tab"))

    assert "toggle_repeat" in editor.blocked_keys()
    assert "will not work" in editor.note_label.text()
    assert "Windows switches applications" in editor.note_label.text()


def test_the_editor_marks_the_row_of_a_flagged_key(editor) -> None:
    """The note line only describes the last edit, so the row has to carry it."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeySequence

    editor._rows["toggle_repeat"][0].setKeySequence(QKeySequence("Alt+Tab"))

    marked = [
        editor.table.item(row, 0).text()
        for row in range(editor.table.rowCount())
        if editor.table.item(row, 0)
        and editor.table.item(row, 0).data(Qt.ItemDataRole.UserRole) == "toggle_repeat"
    ]
    assert marked and "⚠" in marked[0], "the row was not marked"


def test_the_mark_clears_when_the_key_is_fixed(editor) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeySequence

    def row_text() -> str:
        for row in range(editor.table.rowCount()):
            item = editor.table.item(row, 0)
            if item and item.data(Qt.ItemDataRole.UserRole) == "toggle_repeat":
                return item.text()
        return ""

    editor._rows["toggle_repeat"][0].setKeySequence(QKeySequence("Alt+Tab"))
    assert "⚠" in row_text()

    editor._reset_action(registry.ACTIONS_BY_ID["toggle_repeat"])
    assert "⚠" not in row_text()
    assert editor.blocked_keys() == {}


def test_a_risky_key_is_flagged_but_not_blocked(editor) -> None:
    from PySide6.QtGui import QKeySequence

    editor._rows["toggle_repeat"][0].setKeySequence(QKeySequence("F10"))
    assert editor.blocked_keys() == {}, "risky is not blocked"
    assert "may cause trouble" in editor.note_label.text()


# --------------------------------------------------------------------------- #
# Typing must not trigger shortcuts
# --------------------------------------------------------------------------- #


def test_typing_in_a_filter_box_does_not_fire_single_key_shortcuts(window) -> None:
    """Space, K, P and N are all single keys, and the log dock has a filter box.
    Qt's ShortcutOverride keeps the line edit's claim on printable keys; using
    ApplicationShortcut context instead would break this."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    window.show()
    QApplication.processEvents()

    fired: list[str] = []
    window.toggle_mark = lambda: fired.append("mark")
    window._toggle_pause = lambda: fired.append("play")
    window.apply_shortcuts()

    box = window.log_dock.search_edit
    box.setFocus()
    QApplication.processEvents()
    assert box.hasFocus()

    for key in (Qt.Key_K, Qt.Key_Space, Qt.Key_N, Qt.Key_P):
        QTest.keyClick(box, key)
        QApplication.processEvents()

    assert fired == [], f"typing triggered actions: {fired}"
    assert box.text() == "k np"


# --------------------------------------------------------------------------- #
# Menu wiring
# --------------------------------------------------------------------------- #


def test_menu_actions_do_not_receive_qts_checked_flag(qt_app) -> None:
    """QAction.triggered carries a `checked` bool, and PySide hands it to any slot
    willing to take a positional argument. open_log_dialog(mark=None) is willing,
    so the menu entry used to call it with mark=False -- not None, so it took the
    "a mark was supplied" branch and died on False.start."""
    from PySide6.QtWidgets import QMainWindow

    from evidence_review.ui.main_window import MainWindow

    host = QMainWindow()
    menu = host.menuBar().addMenu("Review")
    received: list[object] = []

    def open_log_dialog(mark=None):
        received.append(mark)

    MainWindow._add_action(host, menu, "Log Observation…", None, open_log_dialog)
    menu.actions()[0].trigger()

    assert received == [None], "the checked flag must not arrive as a positional argument"


# --------------------------------------------------------------------------- #
# Fitting the screen in front of us
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Switching between cases
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# The first-run wizard and Settings must not disagree
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Menu commands must only call methods the objects they talk to actually have
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Ways the app could quietly lose or misrepresent evidence
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Nothing may name a key it does not own
# --------------------------------------------------------------------------- #


def test_the_empty_state_names_the_keys_actually_bound(window) -> None:
    """It hardcoded "Ctrl+O" and "Ctrl+Shift+O". That is the first thing a new
    reviewer reads, and the app lets every key be rebound."""
    window._settings.shortcuts.overrides = {"open_file": ["Ctrl+9"]}
    window.apply_shortcuts()

    text = window.placeholder.text()
    assert "Ctrl+9" in text, "the empty state must follow the binding"
    assert "Ctrl+O" not in text, "it must not still advertise the default"


def test_the_play_button_tooltip_survives_playing(window) -> None:
    """set_paused() rewrote the tooltip with a hardcoded "(K)", so the first play
    or pause overwrote whatever apply_shortcut_hints had put there."""
    window._settings.shortcuts.overrides = {"play_pause": ["J"]}
    window.apply_shortcuts()

    window.transport.set_paused(False)
    assert "J" in window.transport.play_button.toolTip()
    assert "(K)" not in window.transport.play_button.toolTip()

    window.transport.set_paused(True)
    assert "J" in window.transport.play_button.toolTip()


def test_mark_coach_marks_name_the_keys_actually_bound(window) -> None:
    """The status-bar prompts during marking hardcoded Space/Esc/Enter/R."""
    from evidence_review import shortcuts as registry

    window._settings.shortcuts.overrides = {
        "mark_toggle": ["F7"],
        "cancel_mark": ["F8"],
    }
    window.apply_shortcuts()

    # Every id these prompts reference has to exist, or they render empty.
    ids = {action.id for action in registry.ACTIONS}
    for action_id in ("mark_toggle", "cancel_mark", "log_event", "toggle_repeat"):
        assert action_id in ids, f"{action_id} is not a real action id"

    assert window.shortcuts.key_for("mark_toggle") == "F7"
    assert window.shortcuts.key_for("cancel_mark") == "F8"


def test_menu_text_matches_the_shortcut_registry(window) -> None:
    """The menu said "Log Observation…" while the Shortcuts tab said "Log at the
    playhead", and the editor's search only ever saw the registry side - so
    searching for what the menu calls a command found nothing.

    The menu now takes its text from the registry, and this keeps it that way.
    """
    from PySide6.QtWidgets import QMenu

    from evidence_review import shortcuts as registry

    menu_texts = {
        action.text().rstrip("…")
        for menu in window.menuBar().findChildren(QMenu)
        for action in menu.actions()
        if action.text()
    }

    missing = [
        action.label
        for action in registry.ACTIONS
        if action.in_menu and action.label not in menu_texts
    ]
    assert not missing, f"registry labels with no matching menu entry: {missing}"


def test_commands_name_the_record_consistently(window) -> None:
    """ "Log Observation…" sat three lines above "Edit Entry…" for the same object,
    and the dialog titled itself "Edit Observation" over a "Save Entry" button.
    The record is an entry everywhere else - the table, the exports, both servers
    - so that is what the commands call it."""
    from PySide6.QtWidgets import QMenu

    texts = [
        action.text()
        for menu in window.menuBar().findChildren(QMenu)
        for action in menu.actions()
        if action.text()
    ]
    assert "Observation" not in " ".join(texts), (
        "the menus must not mix 'observation' into command names"
    )
    for expected in ("Log entry…", "Edit entry…", "Delete entry"):
        assert expected in texts, f"missing {expected!r}"


def test_every_default_is_already_canonical() -> None:
    """Qt spells some keys its own way - "Ctrl+Delete" comes back "Ctrl+Del" - so a
    default written the long way round never compares equal to itself after a trip
    through the editor, and "Restore all defaults" looks broken."""
    for action in registry.ACTIONS:
        for sequence in action.defaults:
            assert registry.normalise(sequence) == sequence, (
                f"{action.id} default {sequence!r} is not Qt's spelling "
                f"({registry.normalise(sequence)!r})"
            )


def test_edit_and_delete_entry_are_bindable(window) -> None:
    """They were menu entries with action_id=None, so they had no key, could not be
    given one, and appeared in neither the Shortcuts tab nor the F1 reference -
    alone among the menu commands."""
    for action_id in ("edit_entry", "delete_entry"):
        assert action_id in registry.ACTIONS_BY_ID
        assert action_id in window.shortcuts._menu_actions, f"{action_id} is not a menu action"

    from PySide6.QtGui import QKeySequence

    action = window.shortcuts._menu_actions["edit_entry"]
    assert action.shortcut().toString(QKeySequence.SequenceFormat.PortableText) == "F2"

    window._settings.shortcuts.overrides = {"edit_entry": ["Ctrl+Shift+E"]}
    window.apply_shortcuts()
    assert action.shortcut().toString(QKeySequence.SequenceFormat.PortableText) == "Ctrl+Shift+E"


# --------------------------------------------------------------------------- #
# A partial file list must not look like a complete one
# --------------------------------------------------------------------------- #


def test_restore_all_defaults_asks_first(qt_app, monkeypatch) -> None:
    """Undoing it means cancelling the whole Settings dialog, which throws away
    unrelated edits too. The confirmation sits in confirm_reset_all rather than
    reset_all, because a modal inside the action would hang any headless caller."""
    from PySide6.QtGui import QKeySequence
    from PySide6.QtWidgets import QMessageBox

    from evidence_review.ui import shortcut_editor as editor_module
    from evidence_review.ui.shortcut_editor import ShortcutEditor

    editor = ShortcutEditor(registry.resolve(None))
    try:
        editor._rows["mark_toggle"][0].setKeySequence(QKeySequence("Ctrl+M"))

        asked: list[str] = []
        monkeypatch.setattr(
            editor_module.QMessageBox,
            "question",
            staticmethod(
                lambda _p, title, *a, **k: asked.append(title) or QMessageBox.StandardButton.Cancel
            ),
        )
        editor.confirm_reset_all()
        assert asked == ["Restore all shortcuts?"]
        assert editor.bindings()["mark_toggle"] == ["Ctrl+M"], "Cancel must change nothing"

        monkeypatch.setattr(
            editor_module.QMessageBox,
            "question",
            staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
        )
        editor.confirm_reset_all()
        assert editor.bindings() == registry.default_bindings()
    finally:
        editor.deleteLater()


def test_restore_all_defaults_does_not_ask_when_nothing_changed(qt_app, monkeypatch) -> None:
    """Nothing to lose, nothing to ask about."""
    from evidence_review.ui import shortcut_editor as editor_module
    from evidence_review.ui.shortcut_editor import ShortcutEditor

    editor = ShortcutEditor(registry.resolve(None))
    try:

        def explode(*_a, **_k):
            raise AssertionError("must not ask when the bindings are already default")

        monkeypatch.setattr(editor_module.QMessageBox, "question", staticmethod(explode))
        editor.confirm_reset_all()
    finally:
        editor.deleteLater()


# --------------------------------------------------------------------------- #
# Telling the reviewer what is actually going on
# --------------------------------------------------------------------------- #


def test_the_shortcut_reference_can_be_scrolled_and_resized(window) -> None:
    """It was a QMessageBox holding thirty actions in five tables: not scrollable,
    not resizable, and past the bottom of a laptop screen. It is the app's own
    answer to "which key does that"."""
    from PySide6.QtWidgets import QDialog, QTextBrowser

    dialog = window._build_shortcuts_dialog()
    try:
        assert isinstance(dialog, QDialog)
        browser = dialog.findChild(QTextBrowser)
        assert browser is not None, "the reference must live in something scrollable"
        assert "Tagging events" in browser.toHtml()
        assert dialog.isSizeGripEnabled(), "it must be resizable"
    finally:
        dialog.deleteLater()


# --------------------------------------------------------------------------- #
# A file that would not play is a file that was not reviewed
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# The README is a promise about the keys; keep it one
# --------------------------------------------------------------------------- #


def _readme_text() -> str:
    from pathlib import Path

    return (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")


def test_the_readme_documents_every_default_key() -> None:
    """Adding an action to the registry and forgetting the README leaves the one
    document a reviewer is handed describing a key set that no longer exists.
    This session added two actions and renamed four, and the README had drifted
    on all six.

    Checked by key rather than by label: the README pairs actions up as prose
    ("Previous / next frame"), which is better writing than the registry's one
    row per direction, and no test should push back on that.
    """
    #: The README writes a few keys the way a reader expects to see them rather
    #: than the way Qt spells them.
    as_written = {
        "Left": "←",
        "Right": "→",
        "Shift+Left": "Shift+←",
        "Shift+Right": "Shift+→",
        "Up": "↑",
        "Down": "↓",
        "PgDown": "PgDn",
        # Return and Enter are the main and the numpad key. The README names the
        # one a reader presses and does not belabour the distinction.
        "Return": "Enter",
    }

    readme = _readme_text()
    controls = readme.split("## Controls", 1)[1].split("\n\n**", 1)[0]

    missing: list[str] = []
    for action in registry.ACTIONS:
        for key in action.defaults:
            if as_written.get(key, key) not in controls:
                missing.append(f"{action.label}: {key}")
    assert not missing, f"default keys the README does not mention: {missing}"


def test_the_readme_action_count_is_right() -> None:
    """It said "all 30 actions" while there were 32."""
    import re

    readme = _readme_text()
    match = re.search(r"lists all (\d+) actions", readme)
    assert match, "the README no longer states how many actions there are"
    assert int(match.group(1)) == len(registry.ACTIONS), (
        f"README says {match.group(1)} actions; the registry has {len(registry.ACTIONS)}"
    )
