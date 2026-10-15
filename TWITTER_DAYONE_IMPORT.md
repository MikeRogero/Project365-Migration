# Twitter / X Archive to Day One

This workflow turns an extracted standard Twitter/X archive into explicitly reviewed,
media-first Day One entries. The archive remains read-only. Review decisions are stored
locally in SQLite, and the exporter will not include an entry unless it has a reviewed
**Include** decision.

## Diary model

- A standalone authored post becomes one candidate entry.
- A post followed by replies to the account's own archived posts becomes one ordered
  thread entry instead of several fragments.
- Local photos and videos stay with their source thread and are placed before the text
  in the Day One entry.
- Retweets are excluded from the recommended queue by default.
- A reply whose parent is absent from the archive is excluded from the recommended
  queue. It remains available under **Needs context** and **All items** so it can be
  restored only after enough context is written in the review tool.
- A unique matching `note-tweet.js` record supplies the complete long-form text. The
  workflow does not guess when timestamps are ambiguous.

## Control Panel workflow

1. Start the Project365 Control Panel and open **Import your data**.
2. Enable **Twitter / X** if it is not already enabled.
3. Put one extracted archive under `Source Data/Twitter Data Export`.
4. Use **Refresh staged metadata** for the normalized audit snapshot.
5. Use **Open diary review**. The local reviewer starts on port 8773 and synchronizes
   the archive into `Project365Canonical/twitter_dayone_review/review.sqlite3`.
6. Review the **Recommended** queue. Each card supports:
   - photo/video preview;
   - editable title, diary text, and context note;
   - a one-to-five diary-value rating;
   - reversible Include, Needs context, and Exclude decisions;
   - focused queues plus **All items** recovery.
7. After approving a small, representative set, choose a test-set limit and select
   **Build reviewed test ZIP**.

The export is written under `Project365Canonical/exports/twitter_dayone` with a paired
integrity/provenance manifest. The test-set selector prioritizes the highest-rated
approved entries, then entries with media.

## Day One package contract

The ZIP contains `Journal.json`, `photos/`, and `videos/` as needed. It uses deterministic
entry and attachment identifiers, exact-byte lowercase MD5 media names, one matching
`dayone-moment://` marker per attachment, source tags, UTC creation timestamps, and the
archive account timezone when it can be mapped safely.

The builder verifies ZIP integrity, UUID uniqueness, marker/metadata agreement, member
presence, hashes, and unreferenced media before publishing the ZIP. This verifies the
package structure, not Day One's visual rendering.

## Pilot import

Import only the generated test ZIP into Day One first. Check:

- timestamps and diary dates;
- photo and video order;
- media-first entry layout;
- Markdown titles, separators, and context notes;
- source links and tags;
- whether re-importing would create duplicate journals or entries.

Keep the full archive export blocked until the pilot has been visually inspected in the
actual Day One app. The ZIP and its manifest contain readable private diary data.

## Focused verification

```sh
PYTHONPYCACHEPREFIX=/tmp/project365-pycache \
  python3 -m unittest \
  tests.test_twitter_dayone_workflow \
  tests.test_project365_control_app
```

```sh
PYTHONPYCACHEPREFIX=/tmp/project365-pycache \
  python3 -m py_compile twitter_dayone_workflow.py project365_control_app.py
```
