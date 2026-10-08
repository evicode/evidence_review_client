"""Log templates: the form a case is reviewed with, defined as data.

A template owns the domain half of a log entry -- what the reviewer is asked and
how the answers are validated. The other half, in :mod:`models`, is what the
application cannot work without: which file, where in it, who looked and when.

Nothing here is special-cased for the templates that ship. The built-ins in
:mod:`evidence_review.builtin_templates` are ordinary templates that happen to
have been written by us, and the engine cannot tell them apart from one a reviewer
builds. That is the point: if Paranormal Investigation needed a code path of its
own, a user's template would never work.

Identifiers
-----------
``field_id`` is what a stored value points at, so it must never change. Labels
and keys are renameable; ids are not. Built-in ids are readable and prefixed with
their template's id so a row in ``entry_values`` can be read without a join;
user-created ones are uuid4.
"""

from __future__ import annotations

import datetime as _dt
import enum
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .util import from_iso_date


class FieldType(str, enum.Enum):
    """What a field holds, which decides its editor, validation and storage column."""

    TEXT = "text"
    MULTILINE = "multiline"
    CHOICE = "choice"
    NUMBER = "number"
    DATE = "date"
    TIME = "time"
    BOOLEAN = "boolean"

    @property
    def storage(self) -> str:
        """Which ``entry_values`` column this type is stored in.

        Typed columns are the reason a number sorts as a number and a date
        compares as a date. A single stringly-typed value column would make
        "duration greater than 30" a text comparison, which is wrong for 100
        against 99.
        """
        if self is FieldType.NUMBER or self is FieldType.BOOLEAN:
            return "value_number"
        if self is FieldType.DATE:
            return "value_date"
        return "value_text"

    @property
    def is_prose(self) -> bool:
        return self in (FieldType.TEXT, FieldType.MULTILINE)


class FieldRole(str, enum.Enum):
    """What a field *means*, so behaviour is driven by data and not by field names.

    Without roles the engine would have to ask "is this field called Area", which
    is exactly the hardcoding this feature exists to remove. With them, a template
    nobody has written yet still gets colour-coded rows, a searchable summary and
    a working filter bar.
    """

    #: The wide column in the log table; searched by default.
    SUMMARY = "summary"
    #: Colour-coded from its options, offered as a filter, and the left-hand side
    #: of conditional-requirement rules.
    STATUS = "status"
    #: The "where". Offered as a filter and gets the folder-memory autofill.
    LOCATION = "location"
    #: Secondary prose. Searched, but never the row's headline.
    DETAIL = "detail"


#: Default maximum length per prose type, when a field does not set its own.
DEFAULT_MAX_LENGTH: dict[FieldType, int] = {
    FieldType.TEXT: 500,
    FieldType.MULTILINE: 20000,
    FieldType.CHOICE: 120,
    FieldType.TIME: 8,
}


def new_id() -> str:
    return str(uuid.uuid4())


class FieldOption(BaseModel):
    """One allowed value of a choice field.

    A row rather than an entry in a JSON list, because an option carries data of
    its own -- the colour the log table paints a status with, and which value a
    new entry starts on.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    option_id: str = Field(default_factory=new_id)
    value: str
    label: str = ""
    colour: str | None = None
    position: int = 0
    is_default: bool = False

    def model_post_init(self, _context: Any) -> None:
        if not self.label:
            self.label = self.value


class TemplateField(BaseModel):
    """One question on the form."""

    model_config = ConfigDict(str_strip_whitespace=True)

    field_id: str = Field(default_factory=new_id)
    field_key: str
    label: str
    field_type: FieldType = FieldType.TEXT
    role: FieldRole | None = None
    position: int = 0
    is_required: bool = False
    help_text: str = ""
    #: Type-specific settings: ``max_length``, ``rows``, ``minimum``, ``maximum``,
    #: ``suggest_from_history`` for a text field with a growing vocabulary.
    config: dict[str, Any] = Field(default_factory=dict)
    options: list[FieldOption] = Field(default_factory=list)
    #: Whether the field gets a column in the log table. Prose fields are shown
    #: through their role instead, so this stays off for most of them.
    in_table: bool = False

    @field_validator("config", mode="before")
    @classmethod
    def _empty_config_from_php(cls, value: Any) -> Any:
        """Accept ``[]`` (or nothing) as "no settings".

        PHP encodes an empty array as a JSON list, so the PHP server sent
        ``"config": []`` for every field without settings -- and rejecting that
        failed the whole template download, which ran before any entry uploaded.
        The server now sends ``{}``; servers already deployed still send ``[]``.
        """
        if value is None or (isinstance(value, list) and not value):
            return {}
        return value

    # -- shape -------------------------------------------------------------- #

    @property
    def max_length(self) -> int | None:
        explicit = self.config.get("max_length")
        if isinstance(explicit, int):
            return explicit
        return DEFAULT_MAX_LENGTH.get(self.field_type)

    @property
    def suggests_from_history(self) -> bool:
        """Whether past values are offered as completions, as Area always has."""
        return bool(self.config.get("suggest_from_history"))

    @property
    def default_value(self) -> Any:
        for option in self.options:
            if option.is_default:
                return option.value
        # No fallback to the first option. A choice with nothing marked default
        # starts empty on purpose: "Evidential Significance" is a judgement, and
        # pre-filling it with Background would put a judgement in the record that
        # nobody made.
        if self.field_type is FieldType.BOOLEAN:
            return False
        if self.field_type.is_prose or self.field_type is FieldType.TIME:
            return ""
        return None

    def option_for(self, value: Any) -> FieldOption | None:
        text = "" if value is None else str(value)
        for option in self.options:
            if option.value == text:
                return option
        return None

    # -- values ------------------------------------------------------------- #

    def coerce(self, raw: Any) -> Any:
        """Turn whatever the UI or the wire produced into this field's own type.

        Returns None for "no value", so an empty string and an absent answer are
        the same thing. A value that cannot be read as this type is handed back
        unchanged for :meth:`problems` to complain about, rather than being thrown
        away -- silently discarding a reviewer's typing is never the right answer.
        """
        if raw is None:
            return None
        if self.field_type is FieldType.BOOLEAN:
            if isinstance(raw, str):
                return raw.strip().lower() in ("1", "true", "yes", "on")
            return bool(raw)
        if self.field_type is FieldType.NUMBER:
            if isinstance(raw, int | float):
                return float(raw)
            text = str(raw).strip()
            if not text:
                return None
            try:
                return float(text)
            except ValueError:
                return raw
        if self.field_type is FieldType.DATE:
            if isinstance(raw, _dt.date):
                return raw
            text = str(raw).strip()
            if not text:
                return None
            return from_iso_date(text) or raw
        text = str(raw).strip() if not isinstance(raw, str) else raw.strip()
        return text or None

    def problems(self, value: Any) -> list[str]:
        """Why this value is not acceptable for this field."""
        problems: list[str] = []
        empty = value is None or (isinstance(value, str) and not value.strip())

        if self.is_required and empty:
            return [f"'{self.label}' is required."]
        if empty:
            return []

        if self.field_type is FieldType.NUMBER and not isinstance(value, int | float):
            return [f"'{self.label}' must be a number."]
        if self.field_type is FieldType.DATE and not isinstance(value, _dt.date):
            return [f"'{self.label}' must be a date."]
        if self.field_type is FieldType.TIME and not _looks_like_time(str(value)):
            return [f"'{self.label}' must be a time, as 23:14 or 23:14:05."]
        if self.field_type is FieldType.CHOICE and self.options and self.option_for(value) is None:
            allowed = ", ".join(option.value for option in self.options)
            problems.append(f"'{self.label}' must be one of: {allowed}.")

        if isinstance(value, int | float) and not isinstance(value, bool):
            minimum = self.config.get("minimum")
            maximum = self.config.get("maximum")
            if isinstance(minimum, int | float) and value < minimum:
                problems.append(f"'{self.label}' cannot be below {minimum}.")
            if isinstance(maximum, int | float) and value > maximum:
                problems.append(f"'{self.label}' cannot be above {maximum}.")

        limit = self.max_length
        if isinstance(value, str) and limit and len(value) > limit:
            problems.append(f"'{self.label}' is {len(value)} characters; the maximum is {limit}.")
        return problems

    def display(self, value: Any) -> str:
        """The value as the reviewer should read it."""
        if value is None or value == "":
            return ""
        if self.field_type is FieldType.BOOLEAN:
            return "Yes" if value else "No"
        if self.field_type is FieldType.NUMBER:
            number = float(value)
            return str(int(number)) if number.is_integer() else f"{number:g}"
        if self.field_type is FieldType.DATE and isinstance(value, _dt.date):
            return value.isoformat()
        option = self.option_for(value)
        return option.label if option else str(value)


class TemplateRule(BaseModel):
    """ "Y is required when X is V", as data.

    The paranormal rule -- Debunk Reasoning is required when the status is
    Debunked -- used to be an ``if`` in the entry model, which meant no other
    template could ever have one.
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    rule_id: str = Field(default_factory=new_id)
    when_field_id: str
    when_value: str
    requires_field_id: str


class LogTemplate(BaseModel):
    """A complete form definition."""

    model_config = ConfigDict(str_strip_whitespace=True)

    template_id: str = Field(default_factory=new_id)
    name: str
    description: str = ""
    version: int = 1
    is_builtin: bool = False
    #: Set once the reviewer edits this template, which stops a new release's
    #: shipped version from overwriting their changes.
    is_customised: bool = False
    fields: list[TemplateField] = Field(default_factory=list)
    rules: list[TemplateRule] = Field(default_factory=list)

    # -- lookup ------------------------------------------------------------- #

    @property
    def ordered_fields(self) -> list[TemplateField]:
        return sorted(self.fields, key=lambda f: (f.position, f.label))

    def field(self, field_id: str) -> TemplateField | None:
        for candidate in self.fields:
            if candidate.field_id == field_id:
                return candidate
        return None

    def field_by_key(self, field_key: str) -> TemplateField | None:
        for candidate in self.fields:
            if candidate.field_key == field_key:
                return candidate
        return None

    def field_for_role(self, role: FieldRole) -> TemplateField | None:
        for candidate in self.ordered_fields:
            if candidate.role is role:
                return candidate
        return None

    @property
    def summary_field(self) -> TemplateField | None:
        return self.field_for_role(FieldRole.SUMMARY)

    @property
    def status_field(self) -> TemplateField | None:
        return self.field_for_role(FieldRole.STATUS)

    @property
    def location_field(self) -> TemplateField | None:
        return self.field_for_role(FieldRole.LOCATION)

    @property
    def searchable_fields(self) -> list[TemplateField]:
        """Fields a free-text search looks in: prose, plus anything role-marked."""
        return [
            field
            for field in self.ordered_fields
            if field.field_type.is_prose
            or field.role in (FieldRole.SUMMARY, FieldRole.DETAIL, FieldRole.LOCATION)
        ]

    @property
    def statuses(self) -> list[str]:
        field = self.status_field
        return (
            [option.value for option in sorted(field.options, key=lambda o: o.position)]
            if field
            else []
        )

    def colour_for_status(self, value: str) -> str | None:
        field = self.status_field
        option = field.option_for(value) if field else None
        return option.colour if option else None

    # -- values ------------------------------------------------------------- #

    def default_values(self) -> dict[str, Any]:
        return {
            field.field_id: field.default_value
            for field in self.fields
            if field.default_value not in (None, "")
        }

    def coerce_values(self, raw: dict[str, Any] | None) -> dict[str, Any]:
        """Normalise a value map, dropping empties and keeping unknown keys.

        An unknown ``field_id`` is carried through untouched. It belongs to a
        template version this client has not caught up with, and discarding it
        would destroy a colleague's answer on the next save.
        """
        if not raw:
            return {}
        out: dict[str, Any] = {}
        for field_id, value in raw.items():
            field = self.field(field_id)
            coerced = field.coerce(value) if field else value
            if coerced is None or (isinstance(coerced, str) and not coerced.strip()):
                continue
            out[field_id] = coerced
        return out

    def required_field_ids(self, values: dict[str, Any]) -> set[str]:
        """Which fields must be answered, given what has been answered so far."""
        required = {field.field_id for field in self.fields if field.is_required}
        for rule in self.rules:
            answer = values.get(rule.when_field_id)
            if answer is not None and str(answer) == rule.when_value:
                required.add(rule.requires_field_id)
        return required

    def validation_problems(self, values: dict[str, Any]) -> list[str]:
        """Every reason this value map is not ready to save."""
        problems: list[str] = []
        required = self.required_field_ids(values)

        for field in self.ordered_fields:
            value = values.get(field.field_id)
            empty = value is None or (isinstance(value, str) and not value.strip())
            if field.field_id in required and empty:
                problems.append(self._required_message(field))
                continue
            problems.extend(field.problems(value))
        return problems

    def _required_message(self, field: TemplateField) -> str:
        """Say *why* a field is required when a rule is what made it so."""
        for rule in self.rules:
            if rule.requires_field_id != field.field_id:
                continue
            trigger = self.field(rule.when_field_id)
            if trigger is None:
                continue
            return f"'{field.label}' is required when {trigger.label} is {rule.when_value}."
        return f"'{field.label}' is required."

    def summarise(self, values: dict[str, Any]) -> str:
        """One line describing an entry, for the log table and marker tooltips."""
        field = self.summary_field
        if field is not None:
            text = field.display(values.get(field.field_id))
            if text:
                return text
        for candidate in self.ordered_fields:
            if candidate.field_type.is_prose:
                text = candidate.display(values.get(candidate.field_id))
                if text:
                    return text
        return ""


def _looks_like_time(text: str) -> bool:
    parts = text.strip().split(":")
    if len(parts) not in (2, 3) or not all(part.isdigit() for part in parts):
        return False
    hours, minutes = int(parts[0]), int(parts[1])
    seconds = int(parts[2]) if len(parts) == 3 else 0
    return 0 <= hours <= 23 and 0 <= minutes <= 59 and 0 <= seconds <= 59
