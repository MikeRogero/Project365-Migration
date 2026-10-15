# Swarm to Day One import

`swarm_dayone_importer.py` converts a Foursquare Swarm data export into a Day
One JSON ZIP without modifying or uploading the source archive.

## Conversion contract

- Every confirmed check-in becomes one entry.
- The venue name is both visible at the top of the entry and stored as Day
  One's structured `location.placeName` when Swarm exported it.
- Swarm's numeric latitude and longitude are stored directly in the Day One
  location object.
- A check-in's shout appears above all of its photos.
- Photos explicitly linked to a check-in or tip stay together with that entry.
- A venue-linked photo is merged into a same-venue check-in only when it is
  within two minutes of an explicitly check-in-linked photo and ImageMagick's
  perceptual comparison confirms that the images are duplicates. The richer
  check-in entry and one deterministic best-quality photo survive. Other
  venue photos form one photo entry per venue and local date.
- Swarm/Foursquare links remain in an italic source footer below the photos.
- Tips become entries. Their GPS is used only when an exported check-in has the
  exact same venue ID; unresolved tip locations remain visibly counted.
- Non-empty comments authored by the archive owner become standalone entries,
  visibly marked as missing their original parent check-in. Swarm exported no
  parent key, so comments are never attached by timestamp proximity.
- Check-ins that report comments retain a visible missing-thread count because
  Swarm did not export those threads' authors or bodies.
- Exported `createdAt` strings are UTC instants. The importer writes those
  instants once and supplies the check-in's recorded or same-venue-derived UTC
  offset to Day One, so the app displays the event's local civil date and time.
- Automatic visits, unconfirmed visits, undated ratings, lists, plans, shares,
  and account/device data are not made into diary entries. Their counts and
  relevant warnings remain in the manifest.

Visual duplicate reconciliation uses ImageMagick's `magick` command and fails
closed: an unreadable comparison preserves both photos and is reported in the
manifest.

The package uses deterministic entry/media identifiers, exact-byte lowercase
MD5 media filenames, and matching `dayone-moment://` photo markers. The builder
validates those relationships before publishing the ZIP.

## Pilot import

Create a small pilot containing a check-in with a confirmed venue-photo visual
duplicate, one photo tip, and one standalone owner-authored comment:

```sh
python3 swarm_dayone_importer.py \
  --source "Source Data/Swarm Data Export" \
  --output-dir "Project365Canonical/exports/swarm_dayone" \
  --pilot
```

Import the generated `swarm_dayone_pilot_v4_*.zip` using Day One's JSON import.
Verify the pilot before creating the full package:

Before writing a package, the command resolves every explicit `with Person`
identity. Known identities are reused automatically. For an unknown Swarm name,
enter the matching digiKam People name when prompted, either as `Full Name` or
`People/Full Name`. The mapping is stored atomically in
`swarm_people_mappings.json` beside the output packages and reused on later
runs. Use `--people-map PATH` to choose another persistent location. A
non-interactive run with an unknown identity stops before publishing a ZIP
rather than guessing or silently dropping the person.

1. The local date and time are correct.
2. Day One's location view shows both the venue name and map position.
3. The same entry appears as a pin in Day One's Map view and opens from it.
4. The written note appears above the photos.
5. The visually duplicate venue-photo records appear as one check-in entry with one
   photo and the written note retained.
6. The standalone comment is readable and clearly marked as parent-unresolved.
7. The Swarm and Foursquare links are clickable in the final source footer.

## 100-entry review sample

For broader review before the full export, create a deterministic 100-entry
sample. It spans rare missing-venue and unresolved-location records, visually
deduplicated check-ins, check-ins with zero/one/multiple photos, reported
comment counts, tips, venue-photo entries, and standalone comments. Selection
is spread across each class's date range, and selected entries always retain
all of their photos:

```sh
python3 swarm_dayone_importer.py \
  --source "Source Data/Swarm Data Export" \
  --output-dir "Project365Canonical/exports/swarm_dayone" \
  --sample-size 100
```

The manifest records every entry's `review_class` and the complete
`sample_class_counts` distribution.

## Representative-photo backfill review

Photo-less check-ins and tips can be reviewed in a private local page. It
offers up to four photos already exported for the exact same Foursquare venue,
ranked by relationship, time proximity, and resolution. Keys `1` through `4`
select a candidate, `S` skips, and the arrow keys navigate. A JPEG, PNG, or
HEIC can also be dragged onto the entry.

An exported photo has a red border when its capture time is within an inclusive
seven-day window before or after the visit. Images previously selected for the
same exact Foursquare venue appear in a separate reusable-choice section.

The review workspace keeps the decision tool in the left 25% and venue
references in the right 75%, with Foursquare first, Google Maps second, and
Google Images third.
Foursquare sends `X-Frame-Options: SAMEORIGIN`, so its photos page opens in one
reused right-side browser window; Google Maps is embedded in the page. After
the Foursquare window is opened once, it follows the current check-in. A local
image file or an image from any publicly routable HTTPS host can be dropped
anywhere in the left pane. Local/private-network targets, insecure URLs, and
unsupported drag payloads produce a visible error.

Provider access is deliberately lazy. **Auto-submit** defaults off, so loading,
navigating, or selecting a provider sends no request to Foursquare, Google, or
OpenStreetMap. Each provider has an explicit Submit button. Google Images
reverse-geocodes the entry only after submission, displays the cached address
below the venue name, and prepares an exact venue-plus-address query. Nominatim
requests are serialized to at most one per second and cached atomically in
`location_cache.json`; results are attributed to OpenStreetMap contributors.
The endpoint can be replaced with `--geocoder-url`.

The Auto-submit choice is stored locally in the browser. When enabled, only the
selected provider follows later target changes. Foursquare and Google Images
still need one user-initiated submission to establish their reusable browser
windows because browsers block unsolicited popups; Google Maps can update its
embedded frame directly. Google Images opens a blank reusable window first,
then navigates it once to the completed venue-plus-address search.

```sh
python3 swarm_photo_backfill_review.py \
  --source "Source Data/Swarm Data Export" \
  --state-dir "Project365Canonical/swarm_dayone_backfill"
```

The optional “Find Wikimedia photos” action is deliberately on demand: only
then does it send the venue name and coordinates rounded to two decimal places
to Wikimedia Commons. Each uncached click performs one title search and, when
needed, one serial nearby search. Concurrent duplicate clicks are coalesced,
and the client identifies itself with a project contact URL as required by
Wikimedia's robot policy. It retains only reusable-license candidates and
stores the chosen file, author, license, license link, and source page. Google
and Foursquare photos are not scraped or bulk cached.

Selections are saved atomically in `decisions.json`. They do not modify the
source archive and can be cleared from the page. After review, apply them with:

```sh
python3 swarm_dayone_importer.py \
  --source "Source Data/Swarm Data Export" \
  --output-dir "Project365Canonical/exports/swarm_dayone" \
  --backfill-decisions "Project365Canonical/swarm_dayone_backfill/decisions.json"
```

This emits a version 4 package with a new deterministic identifier namespace.
Every selected image is tagged `swarm:representative-photo` and visibly labeled
as a venue reference that was not captured during that visit. Import version 4
into a fresh test journal rather than on top of the version 3 test import.

## Photo order, crop queue, and digiKam People

Start the persistent Swarm media review after the backfill review:

```sh
python3 swarm_media_workflow.py \
  --source "Source Data/Swarm Data Export" \
  --database "Project365Canonical/swarm_dayone_media/review.sqlite3"
```

The local page on port 8772 opens with the unreviewed multi-photo queue. Drag
retained cards into Day One order; the first retained card is the
calendar/cover photo. **Next** durably stages that order and advances through a
stable pass without shrinking its total. The top-right **Submit** button shows
the staged count and atomically commits the batch; only then do completed
entries leave the default queue. Staging survives a page or service restart,
which resumes at the first unstaged entry. Before submitting, **Previous** and
**Next** move back and forth through staged items so their order can be checked
or changed without losing the queued decisions. **All items** includes completed
entries when an order needs to be revisited. Reject omits one photo from the Day
One export without deleting the Swarm source. Reject and Crop queue are
independent persistent toggles. Order, completion, rejection, and per-photo
crop-queue choices are stored in SQLite by stable Swarm entry and photo IDs.
Rerunning the command reconciles the current export without discarding saved
choices. New photos are appended to the saved order and return that entry to the
review queue. If the bytes behind an existing photo ID change, its crop and
People results are invalidated rather than silently reused.

**Open shared crop workbench** now routes queued Swarm photos into the same Crop
Workbench used for Project365. Choose **Swarm imports** in the Photo source
selector to review missing, completed, or all queued Swarm crops with the normal
crop, rotation, fill, estimate, keyboard, date-scope, and navigation controls.
Saving a Swarm crop immediately creates a managed 2560px JPEG derivative; the
Swarm source image is never modified. Reset clears the saved crop but keeps the
photo in the queue. A queued but unfinished crop blocks the Day One build, so it
cannot silently fall back to the original.

The workbench is provider-based rather than import-specific. Project365 and
Swarm retain their own storage and save rules behind the shared UI. A future
Facebook, X, or other importer should add a provider descriptor plus list,
detail, image, save, reset, and optional capability methods; it should not add a
new crop page unless its media has a genuinely incompatible editing model.

Prepare independent digiKam working copies with:

```sh
python3 swarm_media_workflow.py \
  --source "Source Data/Swarm Data Export" \
  --database "Project365Canonical/swarm_dayone_media/review.sqlite3" \
  --prepare-digikam
```

Add the reported `digikam_working_copies` folder to digiKam and configure
metadata writes to adjacent `.xmp` sidecars. Do not write metadata into the
working-copy JPEGs. After face/name review in digiKam, import exact photo-level
People mappings with:

```sh
python3 swarm_media_workflow.py \
  --source "Source Data/Swarm Data Export" \
  --database "Project365Canonical/swarm_dayone_media/review.sqlite3" \
  --import-digikam-people
```

Malformed sidecars retain the last known good mapping and are reported as
errors. A valid sidecar with no People removes prior digiKam mappings for that
photo. People are exported only for the photos actually attached to the entry.

Build the reviewed version 5 package with:

```sh
python3 swarm_dayone_importer.py \
  --source "Source Data/Swarm Data Export" \
  --output-dir "Project365Canonical/exports/swarm_dayone" \
  --backfill-decisions "Project365Canonical/swarm_dayone_backfill/decisions.json" \
  --media-workflow-db "Project365Canonical/swarm_dayone_media/review.sqlite3"
```

Version 6 omits rejected photos, applies saved order to both `orderInEntry` and
the photo markers, substitutes only completed crop derivatives, and adds
`person:Name` Day One tags from exact exported-photo mappings. Check-ins also
retain explicit `with Person` entities from the Swarm export after resolving
them through the private, persistent People mapping. The export does not provide a
separate historical check-in-with field, so omitted companions are not
inferred. Use a fresh Day One test journal.

## Full package

After the pilot is accepted, omit `--pilot`:

```sh
python3 swarm_dayone_importer.py \
  --source "Source Data/Swarm Data Export" \
  --output-dir "Project365Canonical/exports/swarm_dayone"
```

A full archive can contain several gigabytes of photos, so the full run needs
comparable free disk space for the new ZIP. Package validation is not proof of
Day One's final display; retain the source export and the generated manifest
until the imported journal has been checked in the app.
