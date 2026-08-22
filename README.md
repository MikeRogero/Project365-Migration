# Project365 and Project365 Pro App Migration Toolkit

Project365 Photo App Migration Toolkit is a local-first migration and enrichment workflow for
long-running Project365 archives. It validates Project365 exports, builds a
canonical SQLite archive, helps recover higher-quality original photos, enriches
entries with modern metadata, and produces Diarium-ready import packages.

The project began from a practical problem: after years of relying on Project365
as a daily life record, the archive had outgrown the application. Project365's
export options are limited to ZIP and PDF files, its images are stored as large
PNG files, and the archive does not naturally benefit from modern diary features
such as location metadata, people identification, tags, social-feed context, and
better media handling.

Rather than abandon the history or manually rebuild it, this toolkit creates a
repeatable, auditable path from legacy Project365 exports to an enriched diary
archive.

## Goals

- Preserve the original Project365 export data locally.
- Validate ZIP exports before importing them into the canonical archive.
- Keep diary text, source media, reports, databases, and generated packages out
  of source control.
- Reconnect entries with better original photos when available.
- Generate smaller, modern JPEG or HEIC working copies from reviewed originals.
- Support local enrichment for location, people, tags, and social-feed context.
- Export a Diarium-compatible Day One ZIP package with verification artifacts.

## Privacy Model

This project is designed for private personal archives.

The Git repository should contain only code, tests, scripts, and lightweight
configuration. The local archive folders are intentionally ignored:

- `Project365Canonical/`
- `Source Data/`
- `Reports/`
- `DiariumContractTests/`
- `LinearExports/`
- generated media, ZIPs, databases, logs, and local browser captures

Do not commit raw Project365 ZIPs, diary entries, original photos, generated
Diarium packages, SQLite databases, or social-export staging files. Some local
outputs intentionally contain private diary text or media references.

## Features

### Export Validation

`project365_export_validator.py` checks Project365 monthly ZIP exports before
they are imported. It validates ZIP integrity, expected `YYYY-MM.zip` naming,
month coverage, duplicate months, real entry dates, root-level
`YYYY-MM-DD.png`/`YYYY-MM-DD.txt` pairs, file sizes, and SHA-256 metadata.

The reports are metadata-focused so the workflow can be reviewed without
printing diary text.

### Canonical Archive Import

`project365_canonical_importer.py` imports validated Project365 ZIPs into a
local canonical archive rooted at `Project365Canonical/`. The canonical archive
uses SQLite as the source of truth for entries, source files, media assets,
metadata, review decisions, and later enrichment.

This separates the migration workflow from the original exports: source files
remain preserved, while repeatable derived data is built under the canonical
workspace.

### Local Control Panel

`project365_control_app.py` provides a local browser-based workflow runner for
the main migration sequence. It runs on `127.0.0.1` by default and is intended
for local use only.

The control panel coordinates:

- Project365 ZIP import
- original-photo matching
- crop confirmation
- working-copy generation
- people and tag review queues
- digiKam people/location import
- Diarium package generation
- status and verification checks

### Original Photo Recovery

Project365 exports can contain reduced or transformed images. The original-photo
workflow searches local photo libraries for better source images that match each
Project365 entry.

The tooling can:

- build a local metadata index of photo libraries
- search by entry date and nearby dates
- use filename dates, EXIF metadata, macOS metadata, and optional indexed
  capture timestamps
- visually rank candidates
- score crop alignment
- record accepted, rejected, or fallback decisions
- preserve rejected candidates so they do not repeatedly reappear

The workflow is intentionally human-reviewed. The tool proposes candidates; it
does not automatically accept them.

### Crop Review

`project365_original_picker.py` includes a crop confirmation interface for
reviewing how a confirmed original photo should be squared for diary use. Crop
geometry is staged first and then committed explicitly, which keeps review
decisions auditable and avoids accidental database writes.

### Working Photo Copies

`project365_media_derivatives.py` generates date-organized working copies from
confirmed originals. The default output policy creates square `2560px` JPEGs at
quality `88`.

These working copies are intended for modern tools such as digiKam, Diarium, or
local verification. The generator supports reviewed crops, overflow padding,
image-size validation, and optional ImageMagick conversion for cases that
macOS `sips` cannot handle reliably.

### People, Tags, and Location Enrichment

The enrichment tools add modern diary metadata while keeping review status
explicit:

- `project365_tag_enrichment.py` builds tag review queues.
- `project365_location_enrichment.py` builds location review queues.
- `project365_digikam_people_importer.py` imports people suggestions from
  digiKam XMP sidecars or CSV exports.
- `project365_digikam_location_importer.py` imports GPS coordinates from
  digiKam XMP sidecars.

The digiKam workflow is local. The generated working-copy folder is scanned by
digiKam, and reviewed XMP sidecars are imported back into the canonical archive.

### Social Feed Staging

`project365_social_adapter_config.py` validates a social-export adapter and
stages normalized records locally. `project365_social_ingester.py` can then
import selected social records into the canonical archive.

The included adapter configuration is for X/Twitter archive-style exports. The
staging JSONL may contain private social text, so it is written under ignored
local output paths and should not be committed.

### Diarium Export and Reconciliation

`project365_diarium_exporter.py` builds a Day One ZIP package that Diarium can
import through:

`Settings > Diary > Migrate from other app > Day One`

Do not use Diarium's generic "Import diary" option for these packages; that
expects a Diarium database backup.

`project365_diarium_reconciler.py` compares a Diarium JSON or ZIP export back
against the canonical archive and the generated package manifest. This provides
a post-import verification step before treating a migration batch as complete.

## Requirements

The core tools are plain Python scripts and currently use the Python standard
library.

Recommended environment:

- macOS
- Python 3.11 or newer
- Git
- Project365 ZIP exports
- optional: ExifTool for richer photo metadata indexing
- optional: ImageMagick for robust image conversion and overflow crop handling
- optional: digiKam for local face tagging and GPS metadata review
- optional: Diarium for the final diary import target

Install optional macOS tools with Homebrew if needed:

```sh
brew install exiftool imagemagick
```

## Installation

Clone the repository:

```sh
git clone https://github.com/MikeRogero/Project365-Migration.git
cd Project365-Migration
```

Create the expected local workspace folders:

```sh
mkdir -p "Source Data/Project365 Pro Export Zips"
mkdir -p "Source Data/Original Photos matching Project365 Entries"
mkdir -p Project365Canonical
mkdir -p Reports
```

Copy Project365 monthly ZIP exports into:

```text
Source Data/Project365 Pro Export Zips/
```

If you have a separate original-photo library, keep it outside the repository or
under the ignored local source folder:

```text
Source Data/Original Photos matching Project365 Entries/
```

## Quick Start: Guided Workflow

Start the local control panel:

```sh
./start_project365.sh --open
```

Check server status:

```sh
./project365_status.sh
```

Stop the local control panel:

```sh
./stop_project365.sh
```

The default control-panel URL is:

```text
http://127.0.0.1:8766
```

Use the control panel when possible. It provides the safest sequence for import,
matching, review, derivative generation, enrichment, and Diarium packaging.

## Command-Line Workflow

The scripts can also be run directly. Paths below use the default local folder
layout.

### 1. Validate Project365 ZIP Exports

```sh
python3 project365_export_validator.py \
  --import-dir "Source Data/Project365 Pro Export Zips" \
  --report-dir Reports
```

Review the generated metadata reports in `Reports/`. Fix export naming,
duplicates, or missing months before importing.

### 2. Import Into the Canonical Archive

```sh
python3 project365_canonical_importer.py \
  --import-dir "Source Data/Project365 Pro Export Zips" \
  --canonical-root Project365Canonical \
  --report-dir Reports
```

This creates or updates `Project365Canonical/canonical.db` and copies the
Project365 media into the ignored local canonical workspace.

### 3. Build a Photo Library Index

```sh
python3 project365_photo_library_index.py \
  --canonical-root Project365Canonical \
  --index-root "/path/to/photo/library"
```

Use `--reset` only when you intentionally want to rebuild the index from
scratch.

### 4. Search for Original Photos

```sh
python3 project365_original_reference_pipeline.py \
  --canonical-root Project365Canonical \
  --search-root "Source Data/Original Photos matching Project365 Entries" \
  --photo-index Project365Canonical/photo_library_index.sqlite
```

For focused repair work, limit by entry date or entry ID:

```sh
python3 project365_original_reference_pipeline.py \
  --canonical-root Project365Canonical \
  --entry-date 1998-04-12 \
  --photo-index Project365Canonical/photo_library_index.sqlite \
  --merge-existing-queue
```

### 5. Review Original Matches and Crops

Start the control panel and open the Original Picker from the workflow page:

```sh
./start_project365.sh --open
```

Review each candidate manually. Accept an external original only when it is the
right source image. Reject incorrect candidates so they are not repeatedly shown.
Use the crop confirmation page to stage and commit square crop decisions.

### 6. Generate Working Photo Copies

```sh
python3 project365_media_derivatives.py \
  --canonical-root Project365Canonical \
  --format jpeg \
  --long-edge 2560 \
  --quality 88
```

The default output folder is:

```text
Project365Canonical/media/diarium_derivatives/Project365_square_2560_q88/
```

### 7. Enrich People and Locations with digiKam

Scan the generated working-copy folder in digiKam. Export or write XMP sidecars,
then import people and GPS metadata:

```sh
python3 project365_digikam_people_importer.py \
  --canonical-root Project365Canonical \
  --xmp-root Project365Canonical/media/diarium_derivatives/Project365_square_2560_q88
```

```sh
python3 project365_digikam_location_importer.py \
  --canonical-root Project365Canonical \
  --xmp-root Project365Canonical/media/diarium_derivatives/Project365_square_2560_q88
```

### 8. Stage Social Feed Context

Validate and stage an extracted X/Twitter archive locally:

```sh
python3 project365_social_adapter_config.py \
  --config config/social_adapters/x_archive_posts_v1.json \
  --source-root "/path/to/extracted/archive"
```

Then import selected staged records:

```sh
python3 project365_social_ingester.py \
  --canonical-root Project365Canonical \
  --staging-jsonl Project365Canonical/staging/social/events.jsonl \
  --selected-only
```

### 9. Generate a Diarium Import Package

```sh
python3 project365_diarium_exporter.py \
  --canonical-root Project365Canonical \
  --output-dir Project365Canonical/exports/diarium_import_batches \
  --package-name project365_dayone.zip \
  --start-date 1998-04-01 \
  --end-date 2026-12-31 \
  --limit 100000
```

Import the ZIP in Diarium with:

```text
Settings > Diary > Migrate from other app > Day One
```

### 10. Reconcile the Diarium Import

After importing into Diarium, export from Diarium as JSON or ZIP and reconcile
it against the package manifest:

```sh
python3 project365_diarium_reconciler.py \
  --canonical-root Project365Canonical \
  --diarium-export "/path/to/diarium-export.zip" \
  --pilot-manifest Project365Canonical/exports/diarium_import_batches/project365_dayone_manifest.csv
```

Treat the migration batch as complete only after reconciliation passes.

## Suggested Migration Sequence

1. Export Project365 monthly ZIP files.
2. Validate the ZIP export set.
3. Import valid ZIPs into the canonical archive.
4. Build or refresh the local photo-library index.
5. Search for external original-photo candidates.
6. Review original-photo matches in the picker.
7. Confirm crops for accepted originals.
8. Generate working photo copies.
9. Run local people/location tooling such as digiKam.
10. Import reviewed people, tags, and GPS metadata.
11. Stage selected social-feed context.
12. Generate a Diarium Day One package.
13. Import the package into Diarium.
14. Reconcile the Diarium export back to the canonical archive.

## Development

Run the unit tests with:

```sh
python3 -m unittest discover tests
```

For focused checks:

```sh
python3 -m unittest tests.test_project365_original_picker
python3 -m unittest tests.test_project365_control_app
```

Before opening a pull request or publishing a release, verify that no private
archive outputs are tracked:

```sh
git status --short
git ls-files | grep -Ei '\.(jpg|jpeg|png|heic|mov|mp4|zip|sqlite|db|csv|jsonl|log|txt|bmp|tif|tiff|webp)$|(^|/)(Project365Canonical|Source Data|Reports|DiariumContractTests|LinearExports|media|exports|runtime|cache|staging)(/|$)'
```

The second command should print nothing for a code-only release.

## Project Status

This is an early open-source release of a real migration toolkit. It was built
to solve a practical personal-archive problem first, so some workflows still
reflect local assumptions and macOS-specific tools. The long-term direction is
to keep the migration process local, verifiable, and privacy-preserving while
making the workflow easier for other Project365 users to adapt.

## License

This project is released under the MIT License. See `LICENSE` for details.
