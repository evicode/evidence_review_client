"""The templates that ship with the application.

These are ordinary :class:`~evidence_review.templates.LogTemplate` values. The
engine has no idea they are special, which is the test that the engine is right:
if one of these needed a code path of its own, a reviewer's own template would
never work.

Built-in ids are readable and prefixed with their template's id, so a row in
``entry_values`` can be understood without joining. They are part of the stored
data on every install and on both servers, so **never change one**. Changing a
label is free; changing a ``field_id`` orphans every value that points at it.

Duplicating a built-in is the expected way to start a template of your own, which
is why these two are worth getting right.
"""

from __future__ import annotations

from .templates import FieldOption, FieldRole, FieldType, LogTemplate, TemplateField, TemplateRule

PARANORMAL_TEMPLATE_ID = "builtin-paranormal"
CRIMINAL_TEMPLATE_ID = "builtin-criminal"

#: The template an entry belongs to when nothing says otherwise -- including every
#: entry written before templates existed.
DEFAULT_TEMPLATE_ID = PARANORMAL_TEMPLATE_ID


def _options(*pairs: tuple[str, str | None]) -> list[FieldOption]:
    """Choice options from (value, colour) pairs, numbered in the order given."""
    return [
        FieldOption(
            option_id=f"opt-{index}",
            value=value,
            label=value,
            colour=colour,
            position=index,
            is_default=index == 0,
        )
        for index, (value, colour) in enumerate(pairs)
    ]


def _prefix(template_id: str, options: list[FieldOption], field_key: str) -> list[FieldOption]:
    """Make option ids unique across the whole install, not just within a field."""
    for option in options:
        option.option_id = f"{template_id}.{field_key}.{option.option_id.removeprefix('opt-')}"
    return options


# --------------------------------------------------------------------------- #
# Paranormal Investigation
#
# Exactly the form the application had before templates existed, expressed as
# data. That it comes out identical is the proof the engine is right, so the
# labels, the order, the status list and its colours are all carried over
# unchanged.
# --------------------------------------------------------------------------- #

_PARANORMAL_STATUSES = _prefix(
    PARANORMAL_TEMPLATE_ID,
    _options(
        ("Needs Review", "#8b949e"),
        ("Unexplained", "#e3a008"),
        ("Debunked", "#3fb950"),
        ("Corroborated", "#58a6ff"),
        ("Inconclusive", "#bc8cff"),
        ("Equipment Artifact", "#a1887f"),
        ("Contamination", "#f778ba"),
    ),
    "status",
)

PARANORMAL_TEMPLATE = LogTemplate(
    template_id=PARANORMAL_TEMPLATE_ID,
    name="Paranormal Investigation",
    description="Review footage for unexplained activity, and record what debunks it.",
    version=1,
    is_builtin=True,
    fields=[
        TemplateField(
            field_id=f"{PARANORMAL_TEMPLATE_ID}.area",
            field_key="area",
            label="Area",
            field_type=FieldType.TEXT,
            role=FieldRole.LOCATION,
            position=0,
            is_required=True,
            help_text="Where in the location this camera or recorder was.",
            config={"max_length": 200, "suggest_from_history": True},
            in_table=True,
        ),
        TemplateField(
            field_id=f"{PARANORMAL_TEMPLATE_ID}.observation",
            field_key="observation",
            label="What Was Heard or Seen",
            field_type=FieldType.MULTILINE,
            role=FieldRole.SUMMARY,
            position=1,
            is_required=True,
            help_text="Describe the event plainly, without interpreting it.",
            config={"rows": 4},
        ),
        TemplateField(
            field_id=f"{PARANORMAL_TEMPLATE_ID}.debunk_reasoning",
            field_key="debunk_reasoning",
            label="Debunk Reasoning",
            field_type=FieldType.MULTILINE,
            role=FieldRole.DETAIL,
            position=2,
            help_text="What explains it: a car, a pipe, a reflection, pareidolia.",
            config={"rows": 3},
        ),
        TemplateField(
            field_id=f"{PARANORMAL_TEMPLATE_ID}.status",
            field_key="status",
            label="Status",
            field_type=FieldType.CHOICE,
            role=FieldRole.STATUS,
            position=3,
            is_required=True,
            options=_PARANORMAL_STATUSES,
            in_table=True,
        ),
    ],
    rules=[
        TemplateRule(
            rule_id=f"{PARANORMAL_TEMPLATE_ID}.debunked-needs-reason",
            when_field_id=f"{PARANORMAL_TEMPLATE_ID}.status",
            when_value="Debunked",
            requires_field_id=f"{PARANORMAL_TEMPLATE_ID}.debunk_reasoning",
        )
    ],
)


# --------------------------------------------------------------------------- #
# Criminal Investigation
#
# Not the paranormal form relabelled. Persons and Vehicles are the two things
# CCTV review actually produces and they are separate because they are searched
# separately; Exhibit Reference ties the file's hash to the paperwork; Evidential
# Significance is a vocabulary, so it is a choice and can be filtered on.
# --------------------------------------------------------------------------- #

_CRIMINAL_STATUSES = _prefix(
    CRIMINAL_TEMPLATE_ID,
    _options(
        ("Needs Review", "#8b949e"),
        ("Relevant", "#58a6ff"),
        ("Not Relevant", "#6e7681"),
        ("Requires Follow-up", "#e3a008"),
        ("Subject Identified", "#3fb950"),
        ("Corroborates Account", "#56d4dd"),
        ("Contradicts Account", "#f85149"),
        ("Evidential", "#bc8cff"),
    ),
    "status",
)

_SIGNIFICANCE = _prefix(
    CRIMINAL_TEMPLATE_ID,
    _options(
        ("Background", "#8b949e"),
        ("Supporting", "#58a6ff"),
        ("Key", "#e3a008"),
        ("Disclosable", "#f778ba"),
    ),
    "significance",
)
# Significance is a judgement, not a default. Nothing is "Background" until
# somebody says so, so the field starts empty.
for _option in _SIGNIFICANCE:
    _option.is_default = False

CRIMINAL_TEMPLATE = LogTemplate(
    template_id=CRIMINAL_TEMPLATE_ID,
    name="Criminal Investigation",
    description="Review recorded media for evidence, identifications and follow-up actions.",
    version=1,
    is_builtin=True,
    fields=[
        TemplateField(
            field_id=f"{CRIMINAL_TEMPLATE_ID}.location",
            field_key="location",
            label="Location / Camera",
            field_type=FieldType.TEXT,
            role=FieldRole.LOCATION,
            position=0,
            is_required=True,
            help_text="Which camera or place this recording covers, e.g. Cam 4 - rear car park.",
            config={"max_length": 200, "suggest_from_history": True},
            in_table=True,
        ),
        TemplateField(
            field_id=f"{CRIMINAL_TEMPLATE_ID}.observation",
            field_key="observation",
            label="Observation",
            field_type=FieldType.MULTILINE,
            role=FieldRole.SUMMARY,
            position=1,
            is_required=True,
            help_text="What is seen or heard. Describe it; do not infer from it.",
            config={"rows": 4},
        ),
        TemplateField(
            field_id=f"{CRIMINAL_TEMPLATE_ID}.status",
            field_key="status",
            label="Status",
            field_type=FieldType.CHOICE,
            role=FieldRole.STATUS,
            position=2,
            is_required=True,
            options=_CRIMINAL_STATUSES,
            in_table=True,
        ),
        TemplateField(
            field_id=f"{CRIMINAL_TEMPLATE_ID}.persons",
            field_key="persons",
            label="Persons",
            field_type=FieldType.MULTILINE,
            position=3,
            help_text="Descriptions, clothing, and any identification made. One per line.",
            config={"rows": 3},
        ),
        TemplateField(
            field_id=f"{CRIMINAL_TEMPLATE_ID}.vehicles",
            field_key="vehicles",
            label="Vehicles",
            field_type=FieldType.MULTILINE,
            position=4,
            help_text="Registration, make, model, colour. One per line.",
            config={"rows": 3},
        ),
        TemplateField(
            field_id=f"{CRIMINAL_TEMPLATE_ID}.actual_time",
            field_key="actual_time",
            label="Actual Time",
            field_type=FieldType.TIME,
            position=5,
            help_text=(
                "Wall-clock time of the event, read off the recording's own timestamp. "
                "Event Timestamp is the position in the file; this is when it happened."
            ),
            in_table=True,
        ),
        TemplateField(
            field_id=f"{CRIMINAL_TEMPLATE_ID}.exhibit_reference",
            field_key="exhibit_reference",
            label="Exhibit Reference",
            field_type=FieldType.TEXT,
            position=6,
            help_text="Continuity reference for this media, as it appears in the case papers.",
            config={"max_length": 120},
            in_table=True,
        ),
        TemplateField(
            field_id=f"{CRIMINAL_TEMPLATE_ID}.significance",
            field_key="significance",
            label="Evidential Significance",
            field_type=FieldType.CHOICE,
            position=7,
            options=_SIGNIFICANCE,
            help_text="How this entry is expected to be used.",
        ),
        TemplateField(
            field_id=f"{CRIMINAL_TEMPLATE_ID}.follow_up",
            field_key="follow_up",
            label="Follow-up Action",
            field_type=FieldType.MULTILINE,
            role=FieldRole.DETAIL,
            position=8,
            help_text="What needs doing next, and by whom.",
            config={"rows": 3},
        ),
    ],
    rules=[
        TemplateRule(
            rule_id=f"{CRIMINAL_TEMPLATE_ID}.followup-needs-action",
            when_field_id=f"{CRIMINAL_TEMPLATE_ID}.status",
            when_value="Requires Follow-up",
            requires_field_id=f"{CRIMINAL_TEMPLATE_ID}.follow_up",
        )
    ],
)


BUILTIN_TEMPLATES: tuple[LogTemplate, ...] = (PARANORMAL_TEMPLATE, CRIMINAL_TEMPLATE)

BUILTIN_TEMPLATES_BY_ID: dict[str, LogTemplate] = {
    template.template_id: template for template in BUILTIN_TEMPLATES
}
