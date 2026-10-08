"""Marks drawn over the picture: the model, the store, and the drawing.

The property everything here protects is that an annotation is a reader's commentary on
the recording and never a change to it. The recording is not touched, the marks live in
their own table, and anything that bakes them into a file has to say so.

The drawing itself is checked by looking at pixels rather than by asserting that a
method was called. A renderer that silently drew nothing would satisfy every structural
assertion anybody is likely to write.
"""

from __future__ import annotations

import datetime as _dt
import subprocess

import pytest
from PySide6.QtGui import QColor, QImage

from evidence_review.annotations import (
    DEFAULT_COLOUR,
    Annotation,
    AnnotationKind,
    render,
    segments,
    visible_at,
)
from evidence_review.media.clip import find_ffmpeg
from evidence_review.models import EntryRow, LogEntry, MediaKind, SyncState
from evidence_review.store import LocalStore
from evidence_review.util import utc_now

from .conftest import make_entry

ffmpeg = find_ffmpeg()


def mark(**overrides) -> Annotation:
    base = {
        "entry_id": "entry-1",
        "kind": AnnotationKind.ELLIPSE,
        "x1": 0.3, "y1": 0.3, "x2": 0.7, "y2": 0.7,
        "start_seconds": 10.0,
        "end_seconds": 14.0,
    }
    base.update(overrides)
    return Annotation.model_validate(base)


def coloured_pixels(image: QImage, colour: str, *, tolerance: int = 60) -> int:
    """How many pixels are close to ``colour``. The test for "was anything drawn"."""
    wanted = QColor(colour)
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    count = 0
    for y in range(0, image.height(), 2):
        for x in range(0, image.width(), 2):
            pixel = image.pixelColor(x, y)
            if pixel.alpha() < 128:
                continue
            if (abs(pixel.red() - wanted.red()) < tolerance
                    and abs(pixel.green() - wanted.green()) < tolerance
                    and abs(pixel.blue() - wanted.blue()) < tolerance):
                count += 1
    return count


# --------------------------------------------------------------------------- #
# Geometry is a fraction of the frame, never a pixel
# --------------------------------------------------------------------------- #


def test_the_same_mark_lands_in_the_same_place_at_any_resolution(qt_app) -> None:
    """The whole point of storing fractions. A circle drawn around a face on a preview
    has to be around that face in a 4K export, not somewhere up and to the left."""
    annotation = mark(colour="#ff00ff", x1=0.25, y1=0.25, x2=0.75, y2=0.75, stroke=0.02)

    small = render([annotation], 320, 180)
    large = render([annotation], 1920, 1080)

    def bounds(image: QImage) -> tuple[float, float, float, float]:
        image = image.convertToFormat(QImage.Format.Format_ARGB32)
        xs, ys = [], []
        for y in range(image.height()):
            for x in range(image.width()):
                if image.pixelColor(x, y).alpha() > 128:
                    xs.append(x / image.width())
                    ys.append(y / image.height())
        return min(xs), min(ys), max(xs), max(ys)

    assert bounds(small) == pytest.approx(bounds(large), abs=0.03)


def test_coordinates_outside_the_frame_are_pulled_back_in() -> None:
    """A drag that ends past the edge of the video is an ordinary thing to do with a
    mouse. Losing the mark over it would lose the reviewer's work."""
    annotation = mark(x1=-0.4, y1=1.9, x2=2.5, y2=-3.0)
    assert (annotation.x1, annotation.y1) == (0.0, 1.0)
    assert (annotation.x2, annotation.y2) == (1.0, 0.0)


def test_a_nonsense_colour_falls_back_rather_than_raising() -> None:
    assert mark(colour="chartreuse").colour == DEFAULT_COLOUR
    assert mark(colour="#ff00ff").colour == "#ff00ff"


# --------------------------------------------------------------------------- #
# When a mark is on screen
# --------------------------------------------------------------------------- #


def test_a_mark_is_on_screen_only_within_its_window() -> None:
    annotation = mark(start_seconds=10.0, end_seconds=14.0)
    assert not annotation.covers(9.5)
    assert annotation.covers(10.0)
    assert annotation.covers(12.0)
    assert annotation.covers(14.0)
    assert not annotation.covers(14.5)


def test_a_mark_on_a_single_frame_still_shows() -> None:
    """Start and end the same is what placing a mark on one frame looks like. Treating
    the window as exclusive would make it invisible."""
    assert mark(start_seconds=10.0, end_seconds=10.0).covers(10.0)


def test_a_window_entered_backwards_still_works() -> None:
    """Typing the end before the start is a slip, not an instruction to show nothing."""
    assert mark(start_seconds=14.0, end_seconds=10.0).covers(12.0)


def test_deleted_marks_are_never_drawn() -> None:
    assert visible_at([mark(is_deleted=True)], 12.0) == []


# --------------------------------------------------------------------------- #
# Splitting a span into pictures to burn in
# --------------------------------------------------------------------------- #


def test_marks_that_never_change_need_only_one_picture() -> None:
    """The common case. Any more would be wasted renders and wasted ffmpeg inputs."""
    marks = [mark(start_seconds=0.0, end_seconds=10.0) for _ in range(3)]
    spans = segments(marks, 0.0, 10.0)
    assert len(spans) == 1
    assert len(spans[0][2]) == 3


def test_a_mark_that_comes_and_goes_splits_the_span() -> None:
    marks = [
        mark(start_seconds=0.0, end_seconds=10.0),
        mark(start_seconds=4.0, end_seconds=7.0),
    ]
    spans = segments(marks, 0.0, 10.0)
    assert [(round(s, 2), round(e, 2), len(group)) for s, e, group in spans] == [
        (0.0, 4.0, 1), (4.0, 7.0, 2), (7.0, 10.0, 1)
    ]


def test_a_span_with_nothing_visible_produces_nothing() -> None:
    assert segments([mark(start_seconds=50.0, end_seconds=60.0)], 0.0, 10.0) == []


# --------------------------------------------------------------------------- #
# Drawing
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "kind",
    [AnnotationKind.ELLIPSE, AnnotationKind.RECTANGLE, AnnotationKind.ARROW,
     AnnotationKind.LINE],
)
def test_every_shape_actually_draws_something(qt_app, kind) -> None:
    """A renderer that quietly drew nothing would pass any assertion about its return
    value. This looks at the pixels."""
    image = render([mark(kind=kind, colour="#ff00ff", stroke=0.02)], 400, 300)
    assert coloured_pixels(image, "#ff00ff") > 20, f"{kind.value} drew nothing"


def test_nothing_is_drawn_where_there_are_no_marks(qt_app) -> None:
    image = render([], 400, 300)
    assert image.width() == 400
    for y in range(0, 300, 7):
        for x in range(0, 400, 7):
            assert image.pixelColor(x, y).alpha() == 0


def test_shapes_carry_a_dark_halo_so_they_show_on_pale_footage(qt_app) -> None:
    """A white arrow on an overcast sky is not a mark, it is a rumour."""
    image = render([mark(colour="#ffffff", stroke=0.02)], 400, 300)
    assert coloured_pixels(image, "#000000", tolerance=70) > 20


def test_the_selection_handles_never_reach_a_file(qt_app) -> None:
    """They are an editing aid. Burning them into an exhibit would put marks in the
    picture that mean nothing to anybody looking at it later."""
    annotation = mark(colour="#ff00ff")
    plain = render([annotation], 400, 300)
    selected = render([annotation], 400, 300, selected_id=annotation.annotation_id)
    assert plain != selected, "handles should show when the editor asks for them"
    assert plain == render([annotation], 400, 300), "and not otherwise"


def test_text_is_drawn_on_a_plate_so_it_can_be_read(qt_app) -> None:
    image = render(
        [mark(kind=AnnotationKind.TEXT, text="Figure in doorway", colour="#ffffff")],
        600, 400,
    )
    assert coloured_pixels(image, "#000000", tolerance=70) > 50, "no backing plate"


def test_a_label_near_the_edge_is_kept_on_the_picture(qt_app) -> None:
    """Drawn half outside the frame, it would simply be missing from the export."""
    image = render(
        [mark(kind=AnnotationKind.TEXT, text="Right at the edge", x1=0.97, y1=0.98)],
        600, 400,
    )
    drawn = [
        (x, y)
        for y in range(0, 400, 3) for x in range(0, 600, 3)
        if image.pixelColor(x, y).alpha() > 128
    ]
    assert drawn, "the label was not drawn at all"
    assert max(x for x, _ in drawn) < 600 and max(y for _, y in drawn) < 400


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #


def test_marks_survive_a_round_trip(store: LocalStore) -> None:
    entry = store.save(make_entry(media_path=r"C:\ev\a.mp4"))
    original = mark(entry_id=entry.entry_id, text="Figure", colour="#4aa3ff")
    store.save_annotations(entry.entry_id, [original])

    (loaded,) = store.annotations_for(entry.entry_id)
    assert loaded.annotation_id == original.annotation_id
    assert loaded.text == "Figure"
    assert loaded.colour == "#4aa3ff"
    assert loaded.kind is AnnotationKind.ELLIPSE
    assert (loaded.x1, loaded.y2) == (original.x1, original.y2)
    assert (loaded.start_seconds, loaded.end_seconds) == (10.0, 14.0)


def test_a_removed_mark_is_tombstoned_rather_than_erased(store: LocalStore) -> None:
    """Consistent with entries. An accidental removal has to be recoverable, and a
    record of what was once asserted about the footage is worth keeping."""
    entry = store.save(make_entry())
    first, second = mark(entry_id=entry.entry_id), mark(entry_id=entry.entry_id)
    store.save_annotations(entry.entry_id, [first, second])

    store.save_annotations(entry.entry_id, [first])

    assert len(store.annotations_for(entry.entry_id)) == 1
    kept = store.annotations_for(entry.entry_id, include_deleted=True)
    assert len(kept) == 2
    assert [a.is_deleted for a in kept].count(True) == 1


def test_saving_marks_does_not_rewrite_the_observation(store: LocalStore) -> None:
    """The point of a separate table. Annotating is not editing what was observed, and
    the entry's own answers and modification time have to show that."""
    entry = store.save(make_entry())
    before = store.get(entry.entry_id)

    store.save_annotations(entry.entry_id, [mark(entry_id=entry.entry_id)])

    after = store.get(entry.entry_id)
    assert after.values == before.values
    assert after.updated_at_utc == before.updated_at_utc


def test_saving_marks_queues_the_entry_for_sync(store: LocalStore) -> None:
    """Otherwise the marks never leave the machine.

    The sync worker looks for pending entries, and uses local_revision to prove the
    version it uploaded is still the current one. Both have to move when the marks move
    -- a revision that stood still would let the worker mark an entry synced while
    holding a copy that no longer had the latest marks in it.
    """
    entry = store.save(make_entry())
    # The revision has to come from the stored row: save() hands back the object it
    # was given, whose counter has not been advanced by the write.
    stored = store.get(entry.entry_id)
    assert store.mark_synced(
        entry.entry_id, server_id=1, expected_revision=stored.local_revision
    )
    synced = store.get(entry.entry_id)
    assert synced.sync_state is SyncState.SYNCED

    store.save_annotations(entry.entry_id, [mark(entry_id=entry.entry_id)])

    after = store.get(entry.entry_id)
    assert after.sync_state is SyncState.PENDING
    assert after.local_revision > synced.local_revision


def test_marks_can_be_gathered_for_a_whole_recording(store: LocalStore) -> None:
    """Exporting a recording has to pick up every mark on it, not only those from the
    entry the dialog happened to be opened from."""
    first = store.save(make_entry(media_path=r"C:\ev\a.mp4", event_offset_seconds=10.0))
    second = store.save(make_entry(media_path=r"C:\ev\a.mp4", event_offset_seconds=40.0))
    other = store.save(make_entry(media_path=r"C:\ev\b.mp4", event_offset_seconds=5.0))

    store.save_annotations(first.entry_id, [mark(entry_id=first.entry_id)])
    store.save_annotations(second.entry_id, [mark(entry_id=second.entry_id)])
    store.save_annotations(other.entry_id, [mark(entry_id=other.entry_id)])

    found = store.annotations_for_media(r"C:\ev\a.mp4")
    assert len(found) == 2
    assert {a.entry_id for a in found} == {first.entry_id, second.entry_id}


def test_marks_on_a_deleted_entry_drop_out_of_the_recording_view(store: LocalStore) -> None:
    entry = store.save(make_entry(media_path=r"C:\ev\a.mp4"))
    store.save_annotations(entry.entry_id, [mark(entry_id=entry.entry_id)])
    assert store.annotations_for_media(r"C:\ev\a.mp4")

    store.soft_delete(entry.entry_id)
    assert store.annotations_for_media(r"C:\ev\a.mp4") == []


def test_counts_come_back_without_loading_every_mark(store: LocalStore) -> None:
    first = store.save(make_entry())
    second = store.save(make_entry())
    store.save_annotations(
        first.entry_id, [mark(entry_id=first.entry_id) for _ in range(3)]
    )
    counts = store.annotation_counts([first.entry_id, second.entry_id])
    assert counts[first.entry_id] == 3
    assert second.entry_id not in counts


def test_an_unknown_shape_from_a_newer_client_is_still_drawn(store: LocalStore) -> None:
    """A mark somebody made about this footage is worth keeping even if this version
    does not know what shape it is. Refusing to open the entry would be worse."""
    entry = store.save(make_entry())
    store.save_annotations(entry.entry_id, [mark(entry_id=entry.entry_id)])
    store.conn.execute("UPDATE annotations SET kind = 'hexagon'")
    store.conn.commit()

    (loaded,) = store.annotations_for(entry.entry_id)
    assert loaded.kind is AnnotationKind.RECTANGLE


def test_the_annotations_table_survives_an_upgrade(tmp_path) -> None:
    """Opening an older database must add the table without disturbing what is in it."""
    first = LocalStore(tmp_path / "evidence.db")
    entry = first.save(
        EntryRow(file_name="a.mp4", media_path=r"C:\ev\a.mp4", media_kind=MediaKind.VIDEO)
    )
    first.save_annotations(entry.entry_id, [mark(entry_id=entry.entry_id)])

    reopened = LocalStore(tmp_path / "evidence.db")
    assert len(reopened.annotations_for(entry.entry_id)) == 1
    assert reopened.get(entry.entry_id) is not None


# --------------------------------------------------------------------------- #
# Taking a colleague's marks
# --------------------------------------------------------------------------- #


def remote_entry(local: EntryRow, marks: list[Annotation], **overrides) -> LogEntry:
    """The same entry as it comes back from the server, carrying marks."""
    data = local.to_wire().model_dump()
    data["annotations"] = [m.model_dump() for m in marks]
    data.update(overrides)
    return LogEntry.model_validate(data)


def test_marks_arrive_with_a_pulled_entry(store: LocalStore) -> None:
    local = store.save(make_entry(media_path=r"C:\ev\a.mp4"))
    theirs = mark(entry_id=local.entry_id, text="Theirs")

    store.merge_remote([remote_entry(local, [theirs])])

    stored = store.annotations_for(local.entry_id)
    assert [a.text for a in stored] == ["Theirs"]


def test_a_colleagues_mark_joins_our_own_rather_than_replacing_it(store: LocalStore) -> None:
    """The reason marks merge per id. Two people annotating one event is ordinary."""
    local = store.save(make_entry())
    ours = mark(entry_id=local.entry_id, text="Ours")
    store.save_annotations(local.entry_id, [ours])

    theirs = mark(entry_id=local.entry_id, text="Theirs")
    store.merge_remote([remote_entry(store.get(local.entry_id), [theirs])])

    assert {a.text for a in store.annotations_for(local.entry_id)} == {"Ours", "Theirs"}


def test_a_mark_the_server_has_not_heard_of_is_not_deleted(store: LocalStore) -> None:
    """Almost always one this client made and has not pushed yet."""
    local = store.save(make_entry())
    ours = mark(entry_id=local.entry_id, text="Not pushed yet")
    store.save_annotations(local.entry_id, [ours])

    store.merge_remote([remote_entry(store.get(local.entry_id), [])])

    assert [a.text for a in store.annotations_for(local.entry_id)] == ["Not pushed yet"]


def test_an_older_copy_from_the_server_does_not_overwrite_a_newer_local_one(
    store: LocalStore,
) -> None:
    """An edit made here and not yet pushed must survive the next pull."""
    local = store.save(make_entry())
    ours = mark(entry_id=local.entry_id, text="Edited here")
    store.save_annotations(local.entry_id, [ours])

    stale = ours.model_copy(
        update={
            "text": "Older server copy",
            "updated_at_utc": ours.updated_at_utc - _dt.timedelta(hours=1),
        }
    )
    store.merge_remote([remote_entry(store.get(local.entry_id), [stale])])

    assert [a.text for a in store.annotations_for(local.entry_id)] == ["Edited here"]


def test_a_newer_copy_from_the_server_is_taken(store: LocalStore) -> None:
    local = store.save(make_entry())
    ours = mark(entry_id=local.entry_id, text="Ours")
    store.save_annotations(local.entry_id, [ours])

    theirs = ours.model_copy(
        update={
            "text": "Corrected by a colleague",
            "updated_at_utc": ours.updated_at_utc + _dt.timedelta(minutes=5),
        }
    )
    store.merge_remote([remote_entry(store.get(local.entry_id), [theirs])])

    assert [a.text for a in store.annotations_for(local.entry_id)] == [
        "Corrected by a colleague"
    ]


def test_a_tombstone_from_the_server_removes_the_mark_here(store: LocalStore) -> None:
    """A removal has to travel. Arriving as an absence it would be indistinguishable
    from a mark the server has not been told about, and would never take effect."""
    local = store.save(make_entry())
    ours = mark(entry_id=local.entry_id, text="To be removed")
    store.save_annotations(local.entry_id, [ours])

    removed = ours.model_copy(
        update={
            "is_deleted": True,
            "updated_at_utc": ours.updated_at_utc + _dt.timedelta(minutes=1),
        }
    )
    store.merge_remote([remote_entry(store.get(local.entry_id), [removed])])

    assert store.annotations_for(local.entry_id) == []
    assert len(store.annotations_for(local.entry_id, include_deleted=True)) == 1


def test_marks_arrive_even_when_the_local_entry_is_newer(store: LocalStore) -> None:
    """Marks are merged on their own timestamps, so a colleague's circles are not
    thrown away just because this machine edited the observation more recently."""
    local = store.save(make_entry())
    newer = store.get(local.entry_id).model_copy(
        update={"updated_at_utc": utc_now() + _dt.timedelta(hours=1)}
    )
    store.save(newer)

    theirs = mark(entry_id=local.entry_id, text="Theirs")
    remote = remote_entry(
        store.get(local.entry_id), [theirs],
        updated_at_utc=utc_now() - _dt.timedelta(hours=1),
    )
    store.merge_remote([remote])

    assert [a.text for a in store.annotations_for(local.entry_id)] == ["Theirs"]


def test_taking_marks_does_not_queue_the_entry_straight_back_for_push(
    store: LocalStore,
) -> None:
    """Otherwise every pull would cause a push, and two clients would trade the same
    entry back and forth for ever."""
    local = store.save(make_entry())
    store.merge_remote([remote_entry(local, [mark(entry_id=local.entry_id)])])

    after = store.get(local.entry_id)
    assert after.sync_state is SyncState.SYNCED


def test_an_entry_read_back_carries_its_marks_for_the_push(store: LocalStore) -> None:
    """The push sends the entry document, so the marks have to be on it."""
    local = store.save(make_entry())
    store.save_annotations(local.entry_id, [mark(entry_id=local.entry_id, text="Mine")])

    payload = store.get(local.entry_id).wire_payload()

    assert len(payload["annotations"]) == 1
    assert payload["annotations"][0]["text"] == "Mine"


def test_a_removed_mark_is_still_on_the_wire_as_a_tombstone(store: LocalStore) -> None:
    local = store.save(make_entry())
    store.save_annotations(local.entry_id, [mark(entry_id=local.entry_id)])
    store.save_annotations(local.entry_id, [])

    payload = store.get(local.entry_id).wire_payload()

    assert len(payload["annotations"]) == 1
    assert payload["annotations"][0]["is_deleted"] is True


def test_editing_an_entry_does_not_drop_its_marks(store: LocalStore) -> None:
    """save() does not write the annotations table, and nothing about an ordinary
    edit should disturb what has been drawn on the footage."""
    local = store.save(make_entry())
    store.save_annotations(local.entry_id, [mark(entry_id=local.entry_id, text="Kept")])

    edited = store.get(local.entry_id)
    store.save(edited.model_copy(update={"investigator_name": "Someone Else"}))

    assert [a.text for a in store.annotations_for(local.entry_id)] == ["Kept"]


# --------------------------------------------------------------------------- #
# Found by the second adversarial pass, 2026-10-04
# --------------------------------------------------------------------------- #


def test_the_picture_size_follows_rotation(tmp_path) -> None:
    """Phone footage carries a rotation matrix and is everyday evidence video.

    ffmpeg applies it when decoding, so the filter chain sees the axes swapped while the
    stream header still reports the coded size. An overlay rendered to the coded size was
    stretched across the rotated frame: a square mark drawn as 0.30 x 0.30 of the picture
    came out 0.55 x 0.18, distorted and moved off whatever it was drawn around.

    It was invisible in review because the player takes its geometry from mpv, which has
    already applied the rotation -- so the preview was right and only the file was wrong.
    """
    from evidence_review.media.clip import video_size

    if ffmpeg is None:
        pytest.skip("ffmpeg is not present")

    upright = tmp_path / "upright.mp4"
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=black:size=640x360:duration=2:rate=10",
         "-c:v", "libopenh264", str(upright)],
        check=True, capture_output=True,
    )
    assert video_size(upright) == (640, 360)

    rotated = tmp_path / "rotated.mp4"
    subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y",
         "-display_rotation", "90", "-i", str(upright), "-c", "copy", str(rotated)],
        check=True, capture_output=True,
    )

    assert video_size(rotated) == (360, 640), (
        "the overlay would be rendered the wrong way round for this clip"
    )


def test_an_edit_is_always_strictly_newer_than_the_one_before() -> None:
    """The wall clock is not fine enough to rely on.

    Windows resolves it to about 15 ms -- the reason entries carry a revision counter --
    and two thousand readings on this machine produced one distinct value. Two edits in
    a tick carried identical timestamps, and both the local merge and the server's take
    the newer of two copies with a strict comparison, so the second was discarded in
    silence.
    """
    mark = Annotation(entry_id="e", kind=AnnotationKind.ELLIPSE, text="first")
    second = mark.moved_to(text="second")
    third = second.moved_to(text="third")

    assert mark.updated_at_utc < second.updated_at_utc < third.updated_at_utc


def test_a_correction_made_immediately_after_still_merges(store) -> None:
    entry = store.save(
        EntryRow(file_name="a.mp4", media_path=r"C:\e\a.mp4", media_kind=MediaKind.VIDEO)
    )
    original = Annotation(entry_id=entry.entry_id, kind=AnnotationKind.ELLIPSE, text="first")
    store.save_annotations(entry.entry_id, [original])

    store.merge_annotations(entry.entry_id, [original.moved_to(text="corrected")])

    assert store.annotations_for(entry.entry_id)[0].text == "corrected"


def test_a_dead_tie_resolves_the_same_way_everywhere(tmp_path) -> None:
    """Two clients, two different edits, identical timestamps. One has to lose -- that
    is inherent to last-write-wins -- but every machine must land on the same answer, or
    they disagree for ever and each keeps re-pushing its own."""
    from evidence_review.store import LocalStore

    base = Annotation(entry_id="shared-entry", kind=AnnotationKind.ELLIPSE, text="base")
    mine = base.model_copy(update={"text": "mine"})
    theirs = base.model_copy(
        update={"text": "theirs", "updated_at_utc": mine.updated_at_utc}
    )

    settled = []
    for name, own, other in (("a", mine, theirs), ("b", theirs, mine)):
        store = LocalStore(tmp_path / f"{name}.db")
        entry = store.save(
            EntryRow(entry_id="shared-entry", file_name="x.mp4",
                     media_path=r"C:\e\x.mp4", media_kind=MediaKind.VIDEO)
        )
        store.save_annotations(entry.entry_id, [own])
        store.merge_annotations(entry.entry_id, [other])
        settled.append(store.annotations_for(entry.entry_id)[0].text)
        store.close()

    assert settled[0] == settled[1], f"the two machines diverged: {settled}"
