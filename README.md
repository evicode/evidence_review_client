# Evidence Review

A desktop tool for reviewing evidence media and recording timestamped observations.

Play a video, audio file or still image, press **LOG** the moment something is heard
or seen, and a modal captures a structured observation with most of the fields
already filled in. Entries are written to a local database immediately and
synchronised to a shared server so a team works from one dataset.

**Design priorities, in order:** never lose a log entry, play the file no matter what
codec it is, minimise keystrokes per log, produce defensible records.

---

## What gets logged

Every entry captures the nine review fields, and autofills the ones it can:

| Field | Autofilled from |
|---|---|
| **File Name** | The loaded media. Full path and SHA-256 recorded alongside. |
| **Area** | The last Area used in this folder, then areas already in the log, then a guess from the folder name. |
| **Event Timestamp** | **Where in the media the event happens** - the exact playback position when LOG was pressed. This is what lets anyone jump straight back to the moment. |
| **Event Duration** | How long the event lasted. Play past the end and press *Use current* to set it, or type it. Optional. |
| **What Was Heard or Seen** | You. The cursor lands here. |
| **Debunk Reasoning** | You. Becomes required when Status is `Debunked`. |
| **Status** | Defaults to `Needs Review`; the list is editable. |
| **Investigator Name** | Your configured name. |
| **Date Reviewed** | Today. |

Alongside these, each entry silently records the media path and hash, the captured
frame, the app version, the machine name, and where the timestamp came from.

### Finding an event again

**Event Timestamp is a position in the file, not a clock time.** An entry reading
`00:01:23.512` means the event is one minute twenty-three into that recording.
Double-click the entry in the session log and the player opens that file and seeks
straight there; the modal's **Go to** button does the same while you are still
editing. The total length of the media sits beside the field for context, and the
captured frame is saved with the entry.

### Exporting the clip

Right-click an entry and choose **Export clipâ€¦**, or use the button in the entry's
detail modal, to cut that event out of the recording as a file of its own â€” the piece
you can attach to a report without handing over the whole thirty-minute file.

Two ways to cut, because they trade off against each other:

| | What you get | When to use it |
|---|---|---|
| **Original recorded data** *(default)* | The recorded streams copied across without being decoded. The picture is byte for byte the camera's. Starts at the nearest keyframe **at or before** the event, so the clip can begin slightly early. | Almost always. Nothing is re-compressed, so nothing is invented. |
| **Frame-exact** | Starts exactly on the marked frame, re-encoded with FFV1 â€” mathematically lossless, so identical frame for frame. Files are much larger. | When the clip must start precisely on the frame. |

Compressed video can only be cut at a keyframe without re-compressing it, so an
original-data clip usually starts a fraction of a second early â€” on CCTV with widely
spaced keyframes it can be several seconds. **The dialog works out and shows that
lead-in for the actual file before anything is written**, so the choice is made with the
real number rather than discovered afterwards.

Each clip is written with a `.txt` provenance file beside it naming the source file and
its SHA-256, the exact span, the measured length of the clip, how it was cut, and how
far into the clip the event begins. A clip that starts early is fine; a clip that starts
early without saying so is one somebody will mis-describe.

ffmpeg is bundled, so this works on a machine with nothing installed on it.

### Listening: audio and EVP

Open an audio file and you get **the waveform of the recording**, not a black
rectangle. That is the difference between being able to review a three-hour EVP session
and not: the work is finding two seconds of whisper in hours of room tone, and a seek bar
gives you no clue where to look.

- **Drag across the waveform** to mark the span of something you heard. It becomes a
  pending mark exactly as `Space` `Space` does while watching, so `Enter` writes it up,
  `P`/`N` step through, and `R` repeats it â€” the workflow is the same whether you tagged
  by ear or by eye.
- **Click** to jump there. **Scroll** to zoom around the cursor, **shift-drag** to pan,
  and the strip along the bottom always shows where you are in the whole recording.
- **Amplify the waveform** with the â–‡ button when a recording is quiet. It scales the
  picture against the loudest moment in that file â€” never against the format â€” so a
  session recorded at a whisper still fills the height. *This changes the picture only;
  the audio is untouched.*
- Logged events are drawn on the waveform in their status colour, so you can see what
  has already been dealt with.

The envelope is read once, in the background, and cached â€” a three-hour session takes
about twenty seconds the first time and opens instantly after that. It is read at 22 kHz
rather than something cheaper on purpose: a lower rate low-passes, and a quiet
high-frequency whisper decoded at 8 kHz barely rises above the noise floor.

Because a recording has no frame to capture, an audio entry's snapshot is **the waveform
around the event** rather than nothing at all.

### Drawing on the picture

With a video open, the **pencil beside LOG** draws text, arrows, circles and boxes over
the moment on screen. `A` does the same thing from the keyboard.

Marks belong to a **logged event** rather than floating on the video â€” that is what makes
them part of the record instead of a doodle. So the pencil takes the event the playhead is
sitting in; if there is none there yet, it starts one, which is the step you would have
had to take anyway.

- **`Shift+A`** shows and hides them while you watch.
- They are stored as fractions of the frame, so they stay where you put them whatever the
  picture is scaled to.
- **Exporting a clip asks whether to burn them in.** You can export the same event with
  the marks and without, and the provenance file beside each clip records which.

### Cleaning up the audio

The **Audio cleanup** panel appears for recordings. It can cut rumble and hiss, notch
out mains hum, reduce broadband noise, bring up quiet parts and slow playback down
without changing pitch â€” with presets to start from.

**It changes what you hear, never the file.** Processing is applied live by the player;
nothing is written to disk.

Three things about it are deliberate, and they matter more than the filters:

- **Nothing is applied by default.** A recording opens playing as recorded.
- **A banner always says, in words, what you are hearing** â€” *"HEARING PROCESSED AUDIO â€”
  rumble below 150 Hz cut; noise reduction 12 dB"* â€” so you can never lose track of the
  state the audio is in.
- **`B` switches it off and on** without losing the settings. Use it constantly.
  Going back and forth with the unprocessed recording is how you tell a real voice from
  something the noise reduction invented.

**The waveform follows the cleanup.** This is the point of it. On a recording with mains
hum in it, the hum fills the picture â€” the background sits at about 40% of the height and
the event you are hunting barely clears it. With the hum notched out and the rumble cut,
the background drops to around 2% and the event stands alone: measured on a three-minute
session, contrast went from 2.5x to 46x. What you are looking at and what you are hearing
stay the same thing.

Because that changes what the picture *means*, the view says so on its own face: an amber
band across the top reading **SHOWING PROCESSED AUDIO** and what was done. It is painted
into the picture, not into the furniture around it, so it survives into the snapshot an
audio entry carries. Switch the cleanup off and the band goes with it.

Re-reading takes a moment â€” a few seconds on a short file, a couple of minutes on a
three-hour session â€” and the old shape stays up while it runs, with a note on the right
saying so rather than an empty well. Each set of settings is cached, so going back to one
you have used is instant.

That last point is not caution for its own sake. Noise reduction pushed hard **creates
detail that was never recorded, and what it creates can sound like speech** â€” it is the
standing criticism of EVP as a method. The answer is not to avoid the tool but to never
lose track of having used it, so:

- **Whatever is switched on when you log an event is written onto that entry**, as the
  exact filter chain. An observation made through 20 dB of noise reduction is a
  different claim from the same words written against the raw recording, and the record
  says which. Editing an entry later keeps what was heard when it was *written*.
- **An exported clip can carry the cleanup**, off by default. Its provenance file then
  states in capitals that the audio was processed, what was applied in plain words, the
  exact chain so anyone can reproduce or undo it, and a warning about what noise
  reduction can invent. A clip with unprocessed audio says that too, under the same
  heading.

### Marking up the picture

Right-click an entry and choose **Annotateâ€¦** (or press `A`) to draw over the frame the
event happens on: **text**, **arrows**, **circles**, **boxes** and **lines**, each in a
colour and thickness you choose, each with a label.

**The recording is never changed.** Marks are saved against the entry in their own
table. The file the camera wrote is not touched, and nothing is drawn into any file
unless you ask for it when exporting.

Each mark has its own **time window** â€” it appears and disappears at the moments you
give it, so a circle around somebody who walks out of shot after two seconds comes off
when they do. It defaults to the event's own span.

`Shift+A`, or **View â†’ Show annotations**, turns the marks on and off over the video.
That affects the screen only.

**Marks sync with the case**, so a colleague sees what you drew. They are merged one
mark at a time rather than replaced, which means two investigators can annotate the same
event without either losing their work â€” the thing that would otherwise happen is that
whoever synced last silently owned the entry. Removing a mark travels too, and annotating
an entry never rewrites its observation.

**Exporting, with or without them.** The export dialog has a separate
**Annotations** choice, and it is **off by default** â€” a clip of the recording is the
usual thing to want, and a clip with a later reader's circles in it is a different
object. Tick it and the marks become part of the picture:

- it requires a mode that re-encodes, so **Original recorded data is not available**
  while it is ticked â€” you cannot copy the recorded bytes and draw on them at the same
  time. The dialog says so and moves you to Frame-exact rather than failing later.
- the provenance file states how many marks were drawn in, what each one said, and that
  **they are not part of the recording**. A clip with nothing drawn on it says that too,
  so a reader never has to notice the absence of a line.

A third mode, **Compressed, for sending**, re-encodes to H.264 for a file small enough
to email. It is the only mode that discards picture detail, it is never the default, and
its provenance file says plainly that the picture was re-compressed.

---

## Installing

### Windows

Download **`EvidenceReviewSetup-1.7.7.exe`** from the [latest release](https://github.com/evicode/evidence_review_client/releases/latest) and run it. It installs per-user, so there is no administrator prompt, and it
needs no Python, no pip and no DLL hunting â€” libmpv is bundled.

Windows will warn that the publisher is unknown, because the installer is not
code-signed. Choose **More info**, then **Run anyway**.

### macOS

**There is no prebuilt macOS download yet.** Build it from source â€” one command
once the prerequisites are in place:

```bash
brew install mpv            # libmpv, which does the playback
git clone https://github.com/evicode/evidence_review_client.git
cd evidence_review/installer
./build_macos.sh
```

That produces `installer/dist/EvidenceReview-1.1.0.dmg`. Open it and drag
**Evidence Review** to Applications.

Because the app is built locally and not notarised, the first launch is refused
with a message about an unidentified developer. **Right-click the app and choose
Open**, then confirm â€” once only. After that it opens normally.

If you would rather just run it than package it, see **From source** below.

---

On first launch a short wizard asks for your name and, optionally, the shared
server URL and token.

### From source

```powershell
# Windows
git clone https://github.com/evicode/evidence_review_client.git
cd evidence_review

python -m venv .venv
.\.venv\Scripts\pip install -e ".[dev]"

# Fetch libmpv once (~115 MB; needs 7-Zip, or the script fetches 7zr.exe itself)
powershell -ExecutionPolicy Bypass -File installer\fetch_libmpv.ps1

.\.venv\Scripts\python -m evidence_review
```

```bash
# macOS
git clone https://github.com/evicode/evidence_review_client.git
cd evidence_review

brew install mpv            # libmpv, which does the playback

python3 -m venv .venv
./.venv/bin/pip install -e ".[dev]"
./.venv/bin/python -m evidence_review
```

---

## Tagging events while you watch

The fastest way to work is to let the video run and tag events as they happen,
then write them up afterwards.

| Key | What it does |
|---|---|
| **Space** | Mark the start of an event. Press it again to close the event. |
| **Enter** | Pause and open the LOG modal, with the marked span already filled in. |
| **Esc** | Cancel a mark you have started. |
| **P** / **N** | Step to the previous / next tagged event and jump to it. |
| **R** | Repeat the current event on a loop, so you can watch it over and over. |

Every tagged event appears on a **strip beneath the video**, aligned to the
timeline. Events already written up are coloured by status; ones you have marked
but not yet logged are shown in red with a tick, so nothing gets forgotten. Click
any mark to jump to it, or hover to see what it says.

Marking never pauses playback â€” that is the point. You can tag three events in a
row and write them up afterwards using `P` and `N` to walk back through them.
Marks that you never log live only for the session; once logged, they are part of
the case and come back every time you open the file.

---

## Controls

| Key | Action | Key | Action |
|---|---|---|---|
| `Space` | **Mark event start / end** | `,` `.` | Previous / next frame |
| `Enter` | **Log the marked event** | `S` | Save snapshot |
| `Esc` | Cancel mark / leave fullscreen | `F` / `F11` | Fullscreen |
| `P` / `N` | Previous / next tagged event | `M` | Mute |
| `R` | Repeat the current event | `[` `]` | Slower / faster |
| `K` | Play / pause | `â†‘` `â†“` | Volume |
| `â†` `â†’` | Seek 5 s | `PgUp` `PgDn` | Previous / next file |
| `Shift+â†` / `Shift+â†’` | Seek 1 s | `Ctrl+O` | Open file |
| `Ctrl+L` or `L` | Log entry | `Ctrl+Shift+O` | Open folder |
| `F2` | Edit entry | `Ctrl+,` | Settings |
| `Ctrl+Del` | Delete entry | `F1` | Keyboard shortcuts |
| `Ctrl+E` | Export log | `Ctrl+Q` | Quit |
| `Ctrl+R` | Sync now | `Ctrl+T` | Log templates |
| `A` | Annotate entry | `Shift+A` | Show / hide annotations |
| `B` | Compare with the recording | | *(audio cleanup on / off)* |

**`Space` marks, `K` plays.** Space is the busiest key in review work, so it is
given to the action you perform most: tagging. Play/pause moves to `K`, and the
on-screen buttons still do everything.

**Every one of these is configurable.** Settings â†’ Shortcuts lists all 36 actions
grouped by what they do. Click a key box, press the combination you want, and it
is yours. `Clear` unbinds an action entirely; `Reset` restores its default; and
`Restore all defaults` puts everything back.

A key can only mean one thing, so assigning one that is already taken removes it
from the other action and tells you which.

Keys the system takes first are flagged as you assign them. **Red** means Windows
handles it before the application ever sees it, so the binding would never fire at
all â€” `Alt+Tab`, `Ctrl+Esc`, `Ctrl+Shift+Esc`, `Print` and most Windows-key
combinations. **Amber** means it will work but already does something you probably
want, such as `Tab` moving focus, `F10` opening the menu bar, or `Alt+F` opening
the File menu. Neither is forbidden â€” the row stays marked, and saving a red one
asks for confirmation first. Menus, button tooltips and the `F1`
reference all read from the same place, so they always show the keys you actually
bound rather than the ones that shipped. Only your changes are written to
`config.toml`; anything you leave alone follows the default if it ever changes.

Double-clicking a row in the session log loads that media and seeks to the event,
which turns the log into a navigable index of the evidence.

---

## Offline-first, by design

Pressing **Save** in the LOG modal writes to local SQLite inside a transaction and
returns. It never waits on the network and never fails because of it.

A background worker drains unsent entries to the server with exponential backoff.
Every entry carries a client-generated UUID, so the server upserts on it and
retries can never duplicate a record. Pull the network cable mid-review and nothing
is lost; the queue flushes when connectivity returns. The status bar shows what is
still waiting.

The same cycle pulls, so entries logged by other investigators on the current case
arrive while you work rather than only at startup. The pull is incremental â€” each
case remembers where it got to â€” so a quiet cycle costs one request that comes back
empty. Untick **Receive other investigators' entries** to upload without
downloading.

---

## Cases

A case is the unit of work: the log you are looking at, and where new entries are
filed. The case name in the status bar is a button â€” it lists the cases this
machine knows about with their entry counts, and creates, switches and renames
them.

Switching moves the log, the marker strip and where new entries go, together. Each
case keeps its own place in the server's history, so moving between several in one
sitting does not re-download anything you already have. An entry stays in the case
it was recorded in; changing case does not reassign old evidence.

---

## Building the installer

Each platform is built on itself: PyInstaller freezes for the machine it runs on,
and neither Inno Setup nor `hdiutil` exists on the other side.

```powershell
# Windows
cd installer
.\build.ps1
```

One command: creates an isolated build environment, fetches and verifies libmpv,
runs PyInstaller, then runs Inno Setup. The result is
`installer\dist\EvidenceReviewSetup-1.7.7.exe` â€” about 168 MB, from a 564 MB bundle.
Most of that is libmpv for playback and ffmpeg for clip export, both bundled so the tool
works on a machine with nothing installed on it. `-SkipFfmpeg` builds without clip
export and comes back down to about 75 MB.

```bash
# macOS
brew install mpv
cd installer
./build_macos.sh
```

Same shape: build environment, PyInstaller, then `hdiutil` wraps the `.app` in
`installer/dist/EvidenceReview-1.1.0.dmg` with the usual drag-to-Applications
layout. `--app-only` stops at the `.app`; `--console` keeps stdout attached so a
startup crash is visible.

Neither installer is signed. On Windows that is a SmartScreen warning; on macOS
Gatekeeper refuses the first launch until the user right-clicks and chooses Open.
Removing those costs a code-signing certificate on Windows, and a Developer ID
plus notarisation on macOS.

Inno Setup 6 is the only prerequisite the script cannot fetch itself:

```powershell
winget install JRSoftware.InnoSetup
```

The build script finds it whether winget installed it per-user under
`%LOCALAPPDATA%\Programs` or an installer put it in `Program Files`. If it is
missing the script says so and stops cleanly, leaving the portable folder build in
`installer\dist\EvidenceReview\`. Use `-SkipInstaller` to stop there deliberately.

The installer is per-user: it needs no administrator prompt, registers an entry in
Add/Remove Programs, and leaves `%LOCALAPPDATA%\EvidenceReview` â€” the evidence
database, captured frames and exports â€” in place when uninstalled.

If a built bundle will not start, rebuild with `.\build.ps1 -Console`. That attaches
a console so the startup traceback is visible; a windowed build has nowhere to print
it. Never ship a console build.

---

## Where things live

```
%APPDATA%\EvidenceReview\config.toml      settings
%LOCALAPPDATA%\EvidenceReview\
    evidence.db                           the local log (SQLite, WAL)
    snapshots\<case>\                     captured frames
    exports\                              CSV and XLSX exports
    logs\evidence_review.log              application log
Windows Credential Manager                the API token, never in a file
```

Uninstalling leaves all of this in place.

---

## Technology

Built with PySide6 (Qt 6), libmpv via python-mpv, httpx, Pydantic v2, stdlib
`sqlite3` in WAL mode, keyring, openpyxl.

**Why libmpv rather than Qt's own multimedia stack:** QtMultimedia on Windows
delegates to Media Foundation, which fails on a large share of real evidence media â€”
MKV, many AVI variants, HEVC in odd containers, DVR and bodycam exports, ADPCM
audio. libmpv plays all of it, and gives exact frame stepping and OSD-free frame
capture as well. The cost is one bundled DLL (~115 MB uncompressed, compressed in
the installer), which the build script fetches for you.

---

## Tests

```powershell
.\.venv\Scripts\python -m pytest
```

Covers timestamp parsing across recorder naming conventions, store round-trips and
timezone preservation, sync idempotency and backoff, offline queueing, export
formatting.

It also pins the things a user can see: that no tooltip, menu or prompt names a
key it does not own, that the menus and the shortcut registry agree, and that this
README documents every key the application binds.

`tests/test_regressions.py` holds tests for bugs found in adversarial review, each
naming the wrong behaviour it prevents: the upload races that could mark an unsent
edit or deletion as synced, cursor paging over entries that share a timestamp,
spreadsheet formula injection in exports, path-safe case identifiers, and the
migration that carried existing rows onto the current field shape.

---
