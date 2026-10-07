"""Application paths, TOML-backed settings, and secure token storage.

Settings live in ``%APPDATA%\\EvidenceReview\\config.toml`` and can be overridden by
``EVREV_*`` environment variables. The API bearer token is *never* written to the
config file; it goes into the Windows Credential Manager via ``keyring``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import tomli_w
from platformdirs import PlatformDirs
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from .models import DEFAULT_STATUSES
from .util import os_user_display_name
from .version import APP_SLUG, ORG_NAME

log = logging.getLogger(__name__)

_KEYRING_SERVICE = "EvidenceReview.API"
_KEYRING_USERNAME = "bearer-token"


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class AppPaths:
    config_dir: Path
    data_dir: Path
    log_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def database(self) -> Path:
        return self.data_dir / "evidence.db"

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def cache_dir(self) -> Path:
        """Derived files that can always be rebuilt from the media.

        Waveform envelopes and the frame the annotation editor draws on. Nothing here
        is evidence: it can be deleted at any time and the application will make it
        again, which is why it sits apart from snapshots and exports.
        """
        return self.data_dir / "cache"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "evidence_review.log"

    def snapshot_dir_for_case(self, case_id: str) -> Path:
        from .util import safe_filename

        path = self.snapshots_dir / safe_filename(case_id, fallback="default")
        path.mkdir(parents=True, exist_ok=True)
        return path

    def ensure(self) -> AppPaths:
        for directory in (
            self.config_dir,
            self.data_dir,
            self.log_dir,
            self.snapshots_dir,
            self.exports_dir,
            self.cache_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self


@lru_cache(maxsize=1)
def app_paths() -> AppPaths:
    """Resolve the platform application directories.

    ``EVREV_HOME`` overrides everything, which keeps tests and portable installs
    self-contained.
    """
    override = os.environ.get("EVREV_HOME")
    if override:
        root = Path(override).expanduser()
        return AppPaths(config_dir=root, data_dir=root / "data", log_dir=root / "logs").ensure()

    dirs = PlatformDirs(appname=APP_SLUG, appauthor=ORG_NAME, roaming=True)
    return AppPaths(
        config_dir=Path(dirs.user_config_dir),
        data_dir=Path(PlatformDirs(appname=APP_SLUG, appauthor=ORG_NAME).user_data_dir),
        log_dir=Path(PlatformDirs(appname=APP_SLUG, appauthor=ORG_NAME).user_log_dir),
    ).ensure()


# --------------------------------------------------------------------------- #
# Settings model
# --------------------------------------------------------------------------- #


#: Where Evidence Review syncs: the muuurder.com server. Offered in Settings and
#: the first-run wizard -- both used to show "https://evidence.example.org",
#: which told nobody the real address. Not a default for ``base_url`` itself: an
#: address there with syncing off makes Settings ask "Turn sync on?" every save.
DEFAULT_SERVER_URL = "https://muuurder.com/evidencereview"


class ApiSettings(BaseModel):
    """Connection to the shared logging server."""

    base_url: str = ""
    enabled: bool = False
    verify_tls: bool = True
    timeout_seconds: float = 20.0
    sync_interval_seconds: int = 30
    batch_size: int = 50
    #: Whether this client downloads other investigators' entries at all. The name
    #: is historical: pulling once at startup was the old behaviour, and it meant a
    #: teammate's entry never arrived until the app was restarted. When set, the
    #: worker pulls every cycle. Kept under the old key so existing config files,
    #: and anyone who deliberately turned it off, carry over unchanged.
    pull_on_start: bool = True

    @field_validator("base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @field_validator("timeout_seconds")
    @classmethod
    def _sane_timeout(cls, value: float) -> float:
        return min(max(float(value), 1.0), 300.0)

    @field_validator("sync_interval_seconds")
    @classmethod
    def _sane_interval(cls, value: int) -> int:
        return min(max(int(value), 5), 3600)

    @field_validator("batch_size")
    @classmethod
    def _sane_batch(cls, value: int) -> int:
        return min(max(int(value), 1), 200)

    @property
    def is_configured(self) -> bool:
        return self.enabled and bool(self.base_url)


class ReviewSettings(BaseModel):
    """Defaults for the review workflow and the LOG modal."""

    investigator_name: str = Field(default_factory=os_user_display_name)
    case_id: str = "default"
    case_name: str = "Default Case"
    #: The status vocabulary as it was before templates owned it.
    #:
    #: Kept so an existing config file is not silently emptied, and carried onto the
    #: case's template once by carry_over_custom_statuses(). After that the template
    #: is the truth and this is inert history; Settings reads and writes the template.
    statuses: list[str] = Field(default_factory=lambda: list(DEFAULT_STATUSES))
    default_status: str = DEFAULT_STATUSES[0]
    #: Whether the carry-over above has already happened.
    statuses_migrated_to_template: bool = False
    areas: list[str] = Field(default_factory=list)
    auto_resume_after_log: bool = False
    capture_snapshot_on_log: bool = True
    compute_media_hash: bool = True
    guess_area_from_path: bool = True

    @field_validator("statuses")
    @classmethod
    def _non_empty_statuses(cls, value: list[str]) -> list[str]:
        cleaned = [s.strip() for s in value if s and s.strip()]
        return cleaned or list(DEFAULT_STATUSES)


class PlayerSettings(BaseModel):
    seek_step_seconds: float = 5.0
    fine_seek_step_seconds: float = 1.0
    jump_step_seconds: float = 10.0
    volume: int = 100
    muted: bool = False
    speed: float = 1.0
    hardware_decoding: str = "auto-safe"
    last_directory: str = ""


class ShortcutSettings(BaseModel):
    """User key bindings.

    Only deviations from the defaults are stored, keyed by action id, so an action
    the user never touched picks up any change to its default in a later release.
    An explicit empty list means "deliberately unbound", which is not the same as
    absent.
    """

    overrides: dict[str, list[str]] = Field(default_factory=dict)


class UiSettings(BaseModel):
    theme: str = "dark"
    window_geometry: str = ""
    window_state: str = ""
    log_dock_visible: bool = True
    playlist_dock_visible: bool = True


class _BomTolerantTomlSource(TomlConfigSettingsSource):
    """A TOML source that tolerates a UTF-8 byte-order mark.

    ``tomllib`` rejects a BOM outright, and several Windows editors still write
    one. Without this, hand-editing config.toml in the wrong editor makes the app
    silently fall back to defaults and appear to have lost the user's settings.
    """

    def _read_file(self, file_path: Path) -> dict[str, Any]:
        # utf-8-sig strips a BOM when present and is a no-op when it is not.
        return tomllib.loads(Path(file_path).read_text(encoding="utf-8-sig"))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EVREV_",
        env_nested_delimiter="__",
        extra="ignore",
        validate_assignment=True,
    )

    first_run_complete: bool = False
    api: ApiSettings = Field(default_factory=ApiSettings)
    review: ReviewSettings = Field(default_factory=ReviewSettings)
    player: PlayerSettings = Field(default_factory=PlayerSettings)
    ui: UiSettings = Field(default_factory=UiSettings)
    shortcuts: ShortcutSettings = Field(default_factory=ShortcutSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Precedence: explicit init > environment > config.toml > defaults.
        return (
            init_settings,
            env_settings,
            _BomTolerantTomlSource(settings_cls, toml_file=app_paths().config_file),
        )

    # -- persistence -------------------------------------------------------- #

    def save(self) -> Path:
        """Write settings to ``config.toml`` atomically."""
        target = app_paths().config_file
        target.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = self.model_dump(mode="json")
        temp = target.with_suffix(".toml.tmp")
        with temp.open("wb") as handle:
            tomli_w.dump(payload, handle)
        os.replace(temp, target)
        log.debug("Settings saved to %s", target)
        return target


def load_settings() -> Settings:
    """Load settings, falling back to defaults if the config file is unreadable.

    Mute is deliberately **not** carried across launches. Volume is a preference; mute
    is a momentary action, like the cleanup bypass it sits beside. A reviewer muted the
    player once -- while hunting for the volume control, the only thing that looked
    like one being an unlabelled music note -- and every session afterwards started
    silent, with nothing but a small icon saying so. Reviewing a recording you cannot
    hear and concluding there was nothing on it is the kind of wrong answer this
    application exists to prevent.
    """
    try:
        settings = Settings()
        settings.player.muted = False
        return settings
    except Exception:
        log.exception("Could not read config.toml; falling back to defaults")
        return Settings.model_construct(
            first_run_complete=False,
            api=ApiSettings(),
            review=ReviewSettings(),
            player=PlayerSettings(),  # muted defaults to False, as above
            ui=UiSettings(),
            shortcuts=ShortcutSettings(),
        )


# --------------------------------------------------------------------------- #
# Secure token storage
# --------------------------------------------------------------------------- #


def get_api_token() -> str:
    """Read the API bearer token from the OS credential store.

    ``EVREV_API_TOKEN`` takes precedence, which is how the server-side tests and
    headless runs supply a token.
    """
    from_env = os.environ.get("EVREV_API_TOKEN")
    if from_env:
        return from_env
    try:
        import keyring

        return keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME) or ""
    except Exception:
        log.warning("Keyring unavailable; API token could not be read", exc_info=True)
        return ""


def set_api_token(token: str) -> bool:
    """Store (or clear) the API bearer token. Returns True on success."""
    try:
        import keyring

        if token:
            keyring.set_password(_KEYRING_SERVICE, _KEYRING_USERNAME, token)
        else:
            # Clearing a token that was never stored is not an error.
            with contextlib.suppress(keyring.errors.PasswordDeleteError):
                keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
        return True
    except Exception:
        log.warning("Keyring unavailable; API token was not stored", exc_info=True)
        return False
