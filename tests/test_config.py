"""Settings loading and persistence.

The BOM case is a regression test: `tomllib` rejects a byte-order mark outright,
and several Windows editors still write one. Before this was handled, hand-editing
config.toml in the wrong editor made the app silently fall back to defaults and
look as though it had lost the user's settings.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

BODY = textwrap.dedent(
    """
    first_run_complete = true

    [review]
    investigator_name = "BOM Tester"
    case_id = "bom-case"
    default_status = "Unexplained"

    [api]
    enabled = true
    base_url = "https://evidence.example.org"
    sync_interval_seconds = 45
    """
).strip()

PROBE = textwrap.dedent(
    """
    import json
    from evidence_review.config import load_settings

    s = load_settings()
    print(json.dumps({
        "investigator": s.review.investigator_name,
        "case_id": s.review.case_id,
        "default_status": s.review.default_status,
        "first_run_complete": s.first_run_complete,
        "base_url": s.api.base_url,
        "interval": s.api.sync_interval_seconds,
    }))
    """
)


def load_in_subprocess(home: Path) -> dict:
    """Settings resolve paths at import time, so each case needs a clean process."""
    import json

    env = {**os.environ, "EVREV_HOME": str(home)}
    env.pop("EVREV_API_TOKEN", None)
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize(
    ("label", "encoding"),
    [("plain utf-8", "utf-8"), ("utf-8 with BOM", "utf-8-sig")],
)
def test_config_loads_with_and_without_a_bom(tmp_path: Path, label: str, encoding: str) -> None:
    home = tmp_path / label.replace(" ", "_")
    home.mkdir()
    (home / "config.toml").write_text(BODY, encoding=encoding)

    settings = load_in_subprocess(home)
    assert settings["investigator"] == "BOM Tester", f"settings lost for {label}"
    assert settings["case_id"] == "bom-case"
    assert settings["first_run_complete"] is True
    assert settings["base_url"] == "https://evidence.example.org"
    assert settings["interval"] == 45


def test_a_corrupt_config_falls_back_to_defaults_instead_of_crashing(tmp_path: Path) -> None:
    home = tmp_path / "corrupt"
    home.mkdir()
    (home / "config.toml").write_text("this is not [valid toml at all", encoding="utf-8")

    settings = load_in_subprocess(home)
    assert settings["first_run_complete"] is False
    assert settings["interval"] == 30, "defaults should be intact"


def test_missing_config_uses_defaults(tmp_path: Path) -> None:
    home = tmp_path / "empty"
    home.mkdir()
    settings = load_in_subprocess(home)
    assert settings["first_run_complete"] is False
    assert settings["base_url"] == ""


def test_settings_round_trip_through_save(tmp_path: Path) -> None:
    """What save() writes must be exactly what a fresh process reads back."""
    home = tmp_path / "roundtrip"
    home.mkdir()

    writer = textwrap.dedent(
        """
        from evidence_review.config import load_settings
        s = load_settings()
        s.review.investigator_name = "Jane Doe"
        s.review.case_id = "willow-house"
        s.review.statuses = ["Needs Review", "Unexplained", "Debunked"]
        s.api.base_url = "https://example.org"
        s.api.sync_interval_seconds = 90
        s.first_run_complete = True
        print(s.save())
        """
    )
    env = {**os.environ, "EVREV_HOME": str(home)}
    result = subprocess.run(
        [sys.executable, "-c", writer], capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode == 0, result.stderr

    written = home / "config.toml"
    assert written.is_file()
    assert not written.read_bytes().startswith(b"\xef\xbb\xbf"), "we must not write a BOM"

    settings = load_in_subprocess(home)
    assert settings["investigator"] == "Jane Doe"
    assert settings["case_id"] == "willow-house"
    assert settings["base_url"] == "https://example.org"
    assert settings["interval"] == 90
    assert settings["first_run_complete"] is True


def test_environment_overrides_the_file(tmp_path: Path) -> None:
    import json

    home = tmp_path / "envoverride"
    home.mkdir()
    (home / "config.toml").write_text(BODY, encoding="utf-8")

    env = {
        **os.environ,
        "EVREV_HOME": str(home),
        "EVREV_API__BASE_URL": "https://override.example.org",
    }
    result = subprocess.run(
        [sys.executable, "-c", PROBE], capture_output=True, text=True, env=env, check=False
    )
    assert result.returncode == 0, result.stderr
    settings = json.loads(result.stdout.strip().splitlines()[-1])
    assert settings["base_url"] == "https://override.example.org"
    assert settings["investigator"] == "BOM Tester", "the file still supplies everything else"


def test_out_of_range_values_are_clamped(tmp_path: Path) -> None:
    home = tmp_path / "clamped"
    home.mkdir()
    (home / "config.toml").write_text("[api]\nsync_interval_seconds = 999999\n", encoding="utf-8")
    settings = load_in_subprocess(home)
    assert settings["interval"] == 3600


# --------------------------------------------------------------------------- #
# The version is written in more than one file; keep them agreeing
# --------------------------------------------------------------------------- #


def _repo_root():
    from pathlib import Path

    return Path(__file__).resolve().parent.parent


def test_the_packaged_version_matches_the_one_the_app_reports() -> None:
    """build.ps1 reads the version from pyproject.toml and says it is "defined in
    exactly one place". It is not: APP_VERSION is a second literal, and it is the
    one the window title, the User-Agent and every entry's app_version carry. If
    they drift, the installer and the thing it installs disagree about what they
    are."""
    import re

    from evidence_review.version import APP_VERSION

    pyproject = (_repo_root() / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', pyproject)
    assert match, "pyproject.toml no longer declares a version"
    assert match.group(1) == APP_VERSION, (
        f"pyproject.toml says {match.group(1)}, version.py says {APP_VERSION}"
    )


def test_the_docs_name_the_installer_that_will_be_built() -> None:
    """The README points a reviewer at a download by filename, and the filename
    carries the version. Bump one without the other and the link is to a file
    that does not exist."""
    import re

    from evidence_review.version import APP_VERSION

    expected = f"EvidenceReviewSetup-{APP_VERSION}.exe"
    for name in ("README.md", "SPEC.md"):
        text = (_repo_root() / name).read_text(encoding="utf-8")
        if "EvidenceReviewSetup-" not in text:
            continue
        stale = re.findall(r"EvidenceReviewSetup-[0-9.]+\.exe", text)
        assert set(stale) == {expected}, f"{name} names {sorted(set(stale))}, expected {expected}"


def test_a_muted_player_does_not_survive_a_restart(tmp_path, monkeypatch) -> None:
    """Volume persists; mute does not.

    A reviewer muted the player once, hunting for a volume control that was an
    unlabelled music note, and every session afterwards opened silent -- the state
    restored faithfully from config.toml, announced by nothing but a small icon. On a
    tool for finding two seconds of whisper in three hours, starting silent and saying
    so quietly is the same class of failure as processed audio with no banner.
    """
    from evidence_review.config import app_paths, load_settings

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        "\n".join(
            [
                "first_run_complete = true",
                "",
                "[player]",
                "volume = 70",
                "muted = true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EVREV_HOME", str(home))
    # app_paths is lru_cached, so whichever test resolved it first owns it for the whole
    # run. Without clearing it the config written above is never read and this asserts
    # nothing: it passed on its own and failed only in the full suite, which is the tell.
    app_paths.cache_clear()
    try:
        settings = load_settings()
    finally:
        app_paths.cache_clear()

    assert settings.player.muted is False, "the session started muted"
    assert settings.player.volume == 70, "volume is a preference and should have stuck"
