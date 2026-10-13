"""Shared fixtures.

Both the client and the server read configuration at import time, so the
environment is redirected to a throwaway directory *before* anything else is
imported. Nothing here touches the real database or the real config file.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# --- must happen before importing evidence_review or the server app ---------
_TEST_HOME = Path(tempfile.mkdtemp(prefix="evrev_tests_"))
os.environ["EVREV_HOME"] = str(_TEST_HOME)
os.environ["EVREV_SERVER_DATABASE_URL"] = f"sqlite:///{(_TEST_HOME / 'server.db').as_posix()}"
os.environ["EVREV_SERVER_SNAPSHOT_DIR"] = str(_TEST_HOME / "server_snapshots")
os.environ.setdefault("EVREV_API_TOKEN", "test-token")
# ---------------------------------------------------------------------------

import datetime as _dt  # noqa: E402

import pytest  # noqa: E402

from evidence_review.builtin_templates import PARANORMAL_TEMPLATE  # noqa: E402
from evidence_review.models import EntryRow, MediaKind, SyncState  # noqa: E402
from evidence_review.store import LocalStore  # noqa: E402

#: The paranormal template's field ids, so a test can say ``values={AREA: "Hall"}``
#: instead of repeating the full id. Looked up through the template rather than
#: spelled out, so a renamed field is caught here rather than in forty assertions.
AREA = PARANORMAL_TEMPLATE.field_by_key("area").field_id
OBSERVATION = PARANORMAL_TEMPLATE.field_by_key("observation").field_id
DEBUNK = PARANORMAL_TEMPLATE.field_by_key("debunk_reasoning").field_id
STATUS = PARANORMAL_TEMPLATE.field_by_key("status").field_id


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    """A fresh local store backed by a temporary database."""
    instance = LocalStore(tmp_path / "evidence.db")
    yield instance
    instance.close()


@pytest.fixture
def sample_entry() -> EntryRow:
    now = _dt.datetime(2026, 9, 14, 21, 35, 12, tzinfo=_dt.UTC)
    return EntryRow(
        entry_id="11111111-2222-3333-4444-555555555555",
        case_id="willow-house",
        file_name="Basement_Cam2.mp4",
        media_path=r"C:\evidence\Basement\Basement_Cam2.mp4",
        media_sha256="a" * 64,
        media_kind=MediaKind.VIDEO,
        event_offset_seconds=83.4,
        event_duration_seconds=3.7,
        media_duration_seconds=3600.0,
        values={
            AREA: "Basement",
            OBSERVATION: "Two distinct knocks from the north wall.",
            STATUS: "Unexplained",
        },
        investigator_name="Jane Doe",
        date_reviewed=_dt.date(2026, 9, 15),
        created_at_utc=now,
        updated_at_utc=now,
        sync_state=SyncState.PENDING,
    )


def make_entry(**overrides) -> EntryRow:
    """Build an entry with sensible defaults, overriding whatever a test cares about.

    ``values`` is merged into the defaults rather than replacing them, so a test that
    only cares about the status does not have to restate the whole form.
    """
    now = _dt.datetime.now(_dt.UTC)
    values = {
        AREA: "Hallway",
        OBSERVATION: "Something happened.",
        STATUS: "Needs Review",
    }
    values.update(overrides.pop("values", {}))
    base = {
        "file_name": "clip.mp4",
        "media_path": r"C:\evidence\clip.mp4",
        "media_kind": MediaKind.VIDEO,
        "values": values,
        "investigator_name": "Tester",
        "created_at_utc": now,
        "updated_at_utc": now,
    }
    base.update(overrides)
    return EntryRow.model_validate(base)


# --------------------------------------------------------------------------- #
# Qt
# --------------------------------------------------------------------------- #
#
# These live here rather than beside the first test that needed them: the UI
# tests are split across several files now, and a real MainWindow is the only way
# to check anything about wiring, menus or dialogs.

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qt_app():
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


@pytest.fixture
def window(qt_app, tmp_path):
    """A real MainWindow, configured as if first run were already done."""
    from evidence_review.config import Settings
    from evidence_review.ui.main_window import MainWindow

    settings = Settings()
    settings.first_run_complete = True
    settings.review.investigator_name = "Tester"
    settings.api.enabled = False
    store = LocalStore(tmp_path / "evidence.db")
    win = MainWindow(settings, store)
    yield win
    win.stop_sync()
    store.close()
