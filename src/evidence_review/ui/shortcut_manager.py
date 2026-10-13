"""Applies the key bindings from settings to a window.

Menu entries own their own shortcut so the key shows beside the menu item; every
other action gets a QShortcut. Rebinding tears the whole lot down and rebuilds it,
which keeps the live state and the stored settings from drifting apart.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import QWidget

from .. import shortcuts as registry

log = logging.getLogger(__name__)


class ShortcutManager(QObject):
    """Owns every keyboard binding in a window."""

    def __init__(self, window: QWidget, parent: QObject | None = None) -> None:
        super().__init__(parent or window)
        self._window = window
        self._handlers: dict[str, Callable[[], None]] = {}
        self._menu_actions: dict[str, QAction] = {}
        self._shortcuts: list[QShortcut] = []
        self._bindings: registry.Bindings = registry.default_bindings()

    # -- registration -------------------------------------------------------- #

    def set_handlers(self, handlers: Mapping[str, Callable[[], None]]) -> None:
        unknown = set(handlers) - set(registry.ACTIONS_BY_ID)
        if unknown:
            raise KeyError(f"No such action(s) in the registry: {sorted(unknown)}")
        self._handlers = dict(handlers)

    def register_menu_action(self, action_id: str, action: QAction) -> None:
        """Adopt a menu entry so its shortcut is managed alongside the rest."""
        if action_id not in registry.ACTIONS_BY_ID:
            raise KeyError(f"No such action: {action_id}")
        self._menu_actions[action_id] = action

    # -- state --------------------------------------------------------------- #

    @property
    def bindings(self) -> registry.Bindings:
        return {action_id: list(keys) for action_id, keys in self._bindings.items()}

    def key_for(self, action_id: str) -> str:
        return registry.primary(self._bindings, action_id)

    def hint(self, action_id: str) -> str:
        """ " (Space)" style suffix for a tooltip, or "" when unbound."""
        return registry.describe(self._bindings, action_id)

    # -- application --------------------------------------------------------- #

    def apply(self, overrides: Mapping[str, list[str]] | None) -> registry.Bindings:
        """Rebuild every binding from the stored overrides."""
        self._bindings = registry.resolve(overrides)

        clashes = registry.conflicts(self._bindings)
        if clashes:
            # Not fatal: Qt resolves duplicates ambiguously, so the user gets a
            # muddle rather than a crash. Worth a log line to explain it.
            for sequence, action_ids in clashes.items():
                log.warning("Key %s is bound to several actions: %s", sequence, action_ids)

        for shortcut in self._shortcuts:
            shortcut.setParent(None)
            shortcut.deleteLater()
        self._shortcuts.clear()

        for action in registry.ACTIONS:
            sequences = [QKeySequence(key) for key in self._bindings.get(action.id, []) if key]
            menu_action = self._menu_actions.get(action.id)

            if menu_action is not None:
                # The menu entry carries the binding, so the key is shown in the
                # menu and there is no second, competing QShortcut.
                menu_action.setShortcuts(sequences)
                continue

            handler = self._handlers.get(action.id)
            if handler is None:
                if sequences:
                    log.debug("Action %s has keys but no handler", action.id)
                continue

            for sequence in sequences:
                shortcut = QShortcut(sequence, self._window)
                shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
                shortcut.activated.connect(handler)
                self._shortcuts.append(shortcut)

        log.debug("Applied %d shortcuts", len(self._shortcuts))
        return self.bindings

    # -- help ---------------------------------------------------------------- #

    def help_html(self) -> str:
        """The shortcut reference, generated from what is actually bound."""
        parts: list[str] = []
        for group in registry.GROUP_ORDER:
            rows: list[str] = []
            for action in registry.ACTIONS:
                if action.group != group:
                    continue
                keys = self._bindings.get(action.id) or []
                if not keys:
                    continue
                shown = " or ".join(f"<b>{key}</b>" for key in keys)
                rows.append(f"<tr><td>{shown}</td><td>{action.label}</td></tr>")
            if rows:
                parts.append(f"<p><b>{group}</b></p><table cellpadding=4>")
                parts.extend(rows)
                parts.append("</table>")

        unbound = [
            action.label for action in registry.ACTIONS if not (self._bindings.get(action.id) or [])
        ]
        if unbound:
            parts.append("<p style='color:#9aa3ad'>Unbound: " + ", ".join(unbound) + "</p>")
        parts.append(
            "<p style='color:#9aa3ad'>Change any of these in Settings &rarr; Shortcuts.</p>"
        )
        return "".join(parts)
