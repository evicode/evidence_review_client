"""The single source of truth for what the application can do and how it is invoked.

Everything that binds a key reads this registry: the shortcuts themselves, the
menu entries, the button tooltips and the help dialog. Without that, a rebound key
keeps being advertised under its old name somewhere, which is worse than having no
customisation at all.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from PySide6.QtGui import QKeySequence

#: Bindings are stored as ``{action_id: [sequence, ...]}``.
Bindings = dict[str, list[str]]


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """One thing the user can do, and the keys that do it by default."""

    id: str
    label: str
    group: str
    defaults: tuple[str, ...] = ()
    description: str = ""
    #: True when the action also appears in a menu; the menu entry then owns the
    #: binding, so no separate QShortcut is created for it.
    in_menu: bool = False
    #: Actions that would strand the user if unbound are not offered for editing.
    fixed: bool = field(default=False)


GROUP_TAGGING = "Tagging events"
GROUP_PLAYBACK = "Playback"
GROUP_NAVIGATION = "Navigation"
GROUP_REVIEW = "Review"
GROUP_FILE = "File"

ACTIONS: tuple[ActionSpec, ...] = (
    # -- tagging ------------------------------------------------------------ #
    ActionSpec(
        "mark_toggle",
        "Mark event start / end",
        GROUP_TAGGING,
        ("Space",),
        "Open a mark at the playhead, or close the one already open.",
    ),
    ActionSpec(
        "log_event",
        "Log the marked event",
        GROUP_TAGGING,
        ("Return", "Enter"),
        "Pause and write up the event that was just marked.",
    ),
    ActionSpec(
        "cancel_mark",
        "Cancel mark / leave fullscreen",
        GROUP_TAGGING,
        ("Esc",),
        "Abandon an open mark, or leave fullscreen when no mark is open.",
    ),
    ActionSpec(
        "previous_mark",
        "Previous tagged event",
        GROUP_TAGGING,
        ("P",),
        "Step back to the previous tagged event and seek to it.",
    ),
    ActionSpec(
        "next_mark",
        "Next tagged event",
        GROUP_TAGGING,
        ("N",),
        "Step on to the next tagged event and seek to it.",
    ),
    ActionSpec(
        "toggle_repeat",
        "Repeat the current event",
        GROUP_TAGGING,
        ("R",),
        "Loop the current tagged event so it can be watched over and over.",
    ),
    # -- playback ----------------------------------------------------------- #
    ActionSpec("play_pause", "Play / pause", GROUP_PLAYBACK, ("K",)),
    ActionSpec("seek_back", "Seek backward", GROUP_PLAYBACK, ("Left",)),
    ActionSpec("seek_forward", "Seek forward", GROUP_PLAYBACK, ("Right",)),
    ActionSpec("seek_back_fine", "Seek backward (fine)", GROUP_PLAYBACK, ("Shift+Left",)),
    ActionSpec("seek_forward_fine", "Seek forward (fine)", GROUP_PLAYBACK, ("Shift+Right",)),
    ActionSpec("frame_back", "Previous frame", GROUP_PLAYBACK, (",",)),
    ActionSpec("frame_forward", "Next frame", GROUP_PLAYBACK, (".",)),
    ActionSpec("volume_up", "Volume up", GROUP_PLAYBACK, ("Up",)),
    ActionSpec("volume_down", "Volume down", GROUP_PLAYBACK, ("Down",)),
    ActionSpec("toggle_mute", "Mute", GROUP_PLAYBACK, ("M",)),
    ActionSpec("speed_down", "Slower", GROUP_PLAYBACK, ("[",)),
    ActionSpec("speed_up", "Faster", GROUP_PLAYBACK, ("]",)),
    ActionSpec("toggle_fullscreen", "Fullscreen", GROUP_PLAYBACK, ("F", "F11")),
    # -- navigation --------------------------------------------------------- #
    ActionSpec("previous_file", "Previous file", GROUP_NAVIGATION, ("PgUp",)),
    ActionSpec("next_file", "Next file", GROUP_NAVIGATION, ("PgDown",)),
    # -- review ------------------------------------------------------------- #
    # An in_menu action's label is also its menu text, so searching the Shortcuts
    # tab for what the menu says finds the row. Anything more descriptive belongs
    # in `description`, which the editor searches too.
    ActionSpec(
        "log_at_playhead",
        "Log entry",
        GROUP_REVIEW,
        ("Ctrl+L", "L"),
        "Open the LOG modal for the current position, ignoring any mark.",
        in_menu=True,
    ),
    # Edit and delete were menu entries with no id, so they had no key, could not
    # be bound to one, and appeared in neither the Shortcuts tab nor the F1
    # reference - alone among the menu commands. They act on the Log panel's
    # selection, which is also what the panel's own context menu does.
    ActionSpec(
        "edit_entry",
        "Edit entry",
        GROUP_REVIEW,
        ("F2",),
        "Reopen the selected entry in the LOG modal.",
        in_menu=True,
    ),
    ActionSpec(
        "delete_entry",
        "Delete entry",
        GROUP_REVIEW,
        ("Ctrl+Del",),  # Qt's own spelling; see test_every_default_is_already_canonical
        "Soft-delete the selected entry. Nothing is ever removed outright.",
        in_menu=True,
    ),
    ActionSpec(
        "toggle_audio_processing",
        "Compare with the recording",
        GROUP_REVIEW,
        ("B",),
        "Switch audio cleanup off and on while listening. Going back and forth is how "
        "you tell a real voice from something the noise reduction invented.",
        in_menu=True,
    ),
    ActionSpec(
        "annotate_entry",
        "Annotate entry",
        GROUP_REVIEW,
        ("A",),
        "Draw text, arrows and circles over the selected entry's frame.",
        in_menu=True,
    ),
    ActionSpec(
        "toggle_annotations",
        "Show annotations",
        GROUP_REVIEW,
        ("Shift+A",),
        "Show or hide the marks drawn over the video. Affects the screen only — "
        "what an export contains is chosen when exporting.",
        in_menu=True,
    ),
    ActionSpec(
        "save_snapshot",
        "Save snapshot",
        GROUP_REVIEW,
        ("S",),
        "Write the current frame to a PNG of your choosing.",
        in_menu=True,
    ),
    ActionSpec("sync_now", "Sync now", GROUP_REVIEW, ("Ctrl+R",), in_menu=True),
    ActionSpec(
        "export_log",
        "Export log",
        GROUP_REVIEW,
        ("Ctrl+E",),
        "Write the case to a spreadsheet or CSV.",
        in_menu=True,
    ),
    # -- file --------------------------------------------------------------- #
    ActionSpec("open_file", "Open file", GROUP_FILE, ("Ctrl+O",), in_menu=True),
    ActionSpec("open_folder", "Open folder", GROUP_FILE, ("Ctrl+Shift+O",), in_menu=True),
    ActionSpec(
        "edit_templates",
        "Log templates",
        GROUP_FILE,
        ("Ctrl+T",),
        "Define the form this case is reviewed with: its fields, types and rules.",
        in_menu=True,
    ),
    ActionSpec("open_settings", "Settings", GROUP_FILE, ("Ctrl+,",), in_menu=True),
    ActionSpec("show_shortcuts", "Keyboard shortcuts", GROUP_FILE, ("F1",), in_menu=True),
    ActionSpec("quit", "Quit", GROUP_FILE, ("Ctrl+Q",), in_menu=True),
)

ACTIONS_BY_ID: dict[str, ActionSpec] = {action.id: action for action in ACTIONS}

GROUP_ORDER: tuple[str, ...] = (
    GROUP_TAGGING,
    GROUP_PLAYBACK,
    GROUP_NAVIGATION,
    GROUP_REVIEW,
    GROUP_FILE,
)


# --------------------------------------------------------------------------- #
# Key sequences
# --------------------------------------------------------------------------- #


def normalise(sequence: str) -> str:
    """Canonical spelling of a key sequence, or "" if it means nothing.

    Comparisons and storage both go through this, so "ctrl+l", "Ctrl+L" and
    "CTRL+l" cannot end up looking like three different bindings.
    """
    if not sequence or not sequence.strip():
        return ""
    parsed = QKeySequence(sequence.strip())
    if parsed.isEmpty():
        return ""
    return parsed.toString(QKeySequence.SequenceFormat.PortableText)


def normalise_all(sequences: Iterable[str]) -> list[str]:
    """Canonicalise a list, dropping blanks and duplicates but keeping order."""
    seen: list[str] = []
    for sequence in sequences:
        canonical = normalise(sequence)
        if canonical and canonical not in seen:
            seen.append(canonical)
    return seen


def default_bindings() -> Bindings:
    """The defaults, canonicalised.

    Qt has its own spelling for some keys - "Ctrl+Delete" comes back as
    "Ctrl+Del" - so a default written the long way round would never compare equal
    to the same binding after a round trip through the editor, and "Restore all
    defaults" would appear not to have worked.
    """
    return {action.id: normalise_all(action.defaults) for action in ACTIONS}


def resolve(overrides: Mapping[str, list[str]] | None) -> Bindings:
    """Merge user overrides onto the defaults.

    Only deviations are stored, so an action the user never touched picks up any
    change to its default in a later version. An override of ``[]`` means the user
    deliberately unbound it, which is different from never having touched it.
    """
    bindings = default_bindings()
    for action_id, sequences in (overrides or {}).items():
        if action_id not in ACTIONS_BY_ID:
            continue  # an action from a newer or older build; ignore it
        bindings[action_id] = normalise_all(sequences)
    return bindings


def conflicts(bindings: Mapping[str, list[str]]) -> dict[str, list[str]]:
    """Key sequences bound to more than one action, as ``{sequence: [action_id]}``."""
    owners: dict[str, list[str]] = {}
    for action_id, sequences in bindings.items():
        for sequence in sequences:
            canonical = normalise(sequence)
            if canonical:
                owners.setdefault(canonical, []).append(action_id)
    return {seq: ids for seq, ids in owners.items() if len(ids) > 1}


def overrides_from(bindings: Mapping[str, list[str]]) -> Bindings:
    """Reduce a full binding map to just what differs from the defaults."""
    result: Bindings = {}
    for action in ACTIONS:
        current = normalise_all(bindings.get(action.id, []))
        if current != normalise_all(action.defaults):
            result[action.id] = current
    return result


def primary(bindings: Mapping[str, list[str]], action_id: str) -> str:
    """The first key bound to an action, for tooltips and menus. "" if unbound."""
    sequences = bindings.get(action_id) or []
    return sequences[0] if sequences else ""


def describe(bindings: Mapping[str, list[str]], action_id: str) -> str:
    """A tooltip-ready suffix such as " (Space)", or "" when unbound."""
    key = primary(bindings, action_id)
    return f" ({key})" if key else ""


# --------------------------------------------------------------------------- #
# Keys the operating system takes first
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class KeyWarning:
    """Why a key sequence is a poor choice."""

    #: "blocked" -- Windows keeps it and the application never sees it at all.
    #: "risky"   -- it arrives, but it already does something the user wants.
    level: str
    message: str

    @property
    def is_blocked(self) -> bool:
        return self.level == "blocked"


#: Windows handles these itself; a binding on one simply never fires.
_BLOCKED: dict[str, str] = {
    "Ctrl+Alt+Del": "Windows reserves this for the security screen.",
    "Ctrl+Esc": "Windows opens the Start menu with this.",
    "Ctrl+Shift+Esc": "Windows opens Task Manager with this.",
    "Alt+Tab": "Windows switches applications with this.",
    "Alt+Shift+Tab": "Windows switches applications with this.",
    "Ctrl+Alt+Tab": "Windows switches applications with this.",
    "Alt+Esc": "Windows cycles windows with this.",
    "Print": "Windows and screenshot tools usually take this.",
}

#: These do reach the application, but taking them over costs something.
_RISKY: dict[str, str] = {
    "Alt+F4": "This closes the window, and Windows will still close it.",
    "Tab": "This moves keyboard focus between controls.",
    "Shift+Tab": "This moves keyboard focus between controls.",
    "F10": "This opens the menu bar.",
    "Alt": "On its own this opens the menu bar.",
}

#: Alt plus one of these opens a menu, because of the &File style mnemonics.
_MENU_MNEMONICS: dict[str, str] = {"F": "File", "R": "Review", "V": "View", "H": "Help"}


def warnings_for(bindings: Mapping[str, list[str]]) -> dict[str, KeyWarning]:
    """Every action whose keys the system is likely to take, worst level first."""
    found: dict[str, KeyWarning] = {}
    for action_id, sequences in bindings.items():
        for sequence in sequences:
            warning = warn_about(sequence)
            if warning is None:
                continue
            existing = found.get(action_id)
            if existing is None or (warning.is_blocked and not existing.is_blocked):
                found[action_id] = warning
    return found


def warn_about(sequence: str) -> KeyWarning | None:
    """Flag a key sequence the operating system or Qt will take first.

    Assigning one of these is not an error -- the editor still accepts it -- but
    silently binding a key that can never fire is the kind of thing a user spends
    twenty minutes blaming themselves for.
    """
    canonical = normalise(sequence)
    if not canonical:
        return None

    if canonical in _BLOCKED:
        return KeyWarning("blocked", _BLOCKED[canonical])

    # Anything with the Windows key: the shell claims most of these, and which
    # ones survive varies by Windows version and installed software.
    if canonical.startswith("Meta+") or canonical == "Meta":
        return KeyWarning(
            "blocked",
            "Windows claims most Windows-key combinations, so this may never reach "
            "the application.",
        )

    if canonical in _RISKY:
        return KeyWarning("risky", _RISKY[canonical])

    menu = _MENU_MNEMONICS.get(canonical[4:]) if canonical.startswith("Alt+") else None
    if menu:
        return KeyWarning("risky", f"This also opens the {menu} menu from the menu bar.")

    return None
