# wanikani_assistant

Personal tooling for managing WaniKani reviews and lesson queue.

## Overview

- **`local_scripts/`** — Python scripts for pulling data from the WaniKani API and running one-off review actions locally
- **`lambda/`** — AWS Lambda function that automatically advances skippable items and submits correct reviews, running hourly

## Setup

### Credentials

Create `credentials.json` in the project root:

```json
{
    "wanikani_api_token": "your-token-here"
}
```

Get your API token from [wanikani.com/settings/personal_access_tokens](https://www.wanikani.com/settings/personal_access_tokens).

### Python dependencies

No third-party packages required — all scripts use the standard library only.

---

## Local scripts

### `fetch_subjects.py`

Fetches all WaniKani subjects (radicals, kanji, vocabulary) and your SRS stage for each, writing to `csv/csv_subjects.csv`.

```powershell
python local_scripts/fetch_subjects.py
```

Columns: `item_id, srs_stage_group, level, name, reading, english_name, type, srs_stage, document_url`

### `estimate_level_up.py`

Estimates when you will level up on your current level and shows theoretical minimum
times for upcoming levels (optimistic, realistic, and historical).

```powershell
python local_scripts/estimate_level_up.py
python local_scripts/estimate_level_up.py --level 29 --lookahead 15
python local_scripts/estimate_level_up.py --csv
```

With `--csv`, writes `csv/level_up_projection.csv` (current level, upcoming levels,
historical pace, and L60 projections). Use `--csv path/to/file.csv` for a custom path.

### `start_lessons_urls.py`

Starts lessons for subjects you pass as WaniKani URLs. Skips anything already at SRS stage 1+ (lesson already started). Runs locally against the API — nothing is uploaded to S3 or Lambda.

```powershell
# Preview first
python local_scripts/start_lessons_urls.py --dry-run --file urls.txt

# Run for real
python local_scripts/start_lessons_urls.py --file urls.txt
```

`urls.txt` is one URL per line (`#` comments allowed). You can also pass URLs directly on the command line.

Output status per item:
- **done** — lesson started
- **skip** — lesson already started, or not unlocked

### `submit_reviews_urls.py`

Submits one correct review for subjects you pass as WaniKani URLs. If an item is still in the lesson queue (SRS stage 0), starts the lesson first. Skips items that are not unlocked or not yet due for review.

```powershell
# Preview first
python local_scripts/submit_reviews_urls.py --dry-run --file urls.txt

# Run for real
python local_scripts/submit_reviews_urls.py --file urls.txt
```

Output status per item:
- **done** — review submitted
- **skip** — not unlocked, or not yet available for review
- **error** — API rejected the review (e.g. review not due yet)

### `kanji_missing_vocab.py`

Lists kanji at a level where you have not started any related vocabulary
(SRS stage 1+ by default). Includes locked/future-level kanji so you can
generate a full L1–60 report. By default prints a compact kanji-only list; use
`--verbose` to include vocab options under each kanji.

```powershell
python local_scripts/kanji_missing_vocab.py --level 30
python local_scripts/kanji_missing_vocab.py --from 1 --to 60 -o csv/kanji_missing_vocab_L1-60.txt
python local_scripts/kanji_missing_vocab.py --from 1 --to 60 --summary
python local_scripts/kanji_missing_vocab.py --level 29 --verbose
```

Default output is kanji only (levels with no gaps are omitted). Use `--verbose`
for vocab pick lists, `--summary` for per-level counts only.

Use `--min-vocab-stage 2` to require at least one completed vocab review instead
of just a started lesson.

---

## Pending queues

Two local CSV files track subjects you want to act on once they unlock. These are not read by Lambda — they are your own backlog.

Columns (both files):

```
requested_at, document_url, item_id, name, reading, english_name, type, level, status
```

`status` is typically `not_unlocked` while waiting. Update or remove rows as items unlock and you process them.

### `csv/pending_unlock_skippable.csv`

Items you want on the **skippable list** but that are not unlocked yet.

When an item unlocks:

1. Append its row to `csv/csv_subjects_skippable.csv` (same format as `csv_subjects.csv`)
2. Remove it from `pending_unlock_skippable.csv`
3. Sync the skippable list to S3 (see below)

### `csv/pending_unlock_run_once_to_start_reviews.csv`

Items you want to **start once** (lesson → review queue) but that are not unlocked yet. These are *not* added to the skippable list.

When an item unlocks:

1. Run `start_lessons_urls.py` (or `submit_reviews_urls.py`) with its `document_url` (or pass URLs from this file)
2. Remove it from `pending_unlock_run_once_to_start_reviews.csv`

If an item unlocks but is still in the lesson queue (stage 0), start the lesson first — `submit_reviews_urls.py` does this automatically. Reviews can only be submitted after WaniKani marks the assignment as available (`available_at` in the past).

---

## Skippable reviews (Lambda)

Items listed in `csv/csv_subjects_skippable.csv` are automatically handled by an AWS Lambda function each hour.

**Skippable list (CSV):** always auto-started and auto-reviewed at every SRS stage.

**Other radicals (current level only):** auto-started and auto-reviewed only while below Guru (SRS stages 0–4) at your current WaniKani level. Once a radical reaches Guru it unlocks dependent kanji and returns to normal review. Radicals on other levels are not auto-handled unless they are on the skippable list.

**Current-level kanji (lesson queue):** auto-started when unlocked but not yet in lessons (stage 0). Reviews are normal unless the kanji is on the skippable list.

Kanji and vocabulary elsewhere are only auto-handled when explicitly on the skippable list.

For each auto-handled item, the Lambda:

1. **Starts the assignment** if it is still in the lesson queue (stage 0 → stage 1)
2. **Submits a correct review** if it is in the open review queue

### Skippable subjects CSV

`csv/csv_subjects_skippable.csv` uses the same column format as `csv_subjects.csv`. Add any subject row to this file to mark it as skippable.

After updating the file, sync it to S3:

```powershell
aws s3 cp csv\csv_subjects_skippable.csv s3://wanikani-assistant-emily/csv/csv_subjects_skippable.csv
```

No Lambda redeployment is needed after updating the CSV. Deploy the Lambda only when `lambda/handler.py` changes (see below).

### Lambda deployment

The Lambda is deployed via AWS SAM from the `lambda/` folder.

```powershell
cd lambda
sam build
sam deploy
```

AWS resources:
- **Lambda:** [wanikani-skippable-reviews](https://us-east-1.console.aws.amazon.com/lambda/home?region=us-east-1#/functions/wanikani-skippable-reviews)
- **S3 bucket:** [wanikani-assistant-emily](https://s3.console.aws.amazon.com/s3/buckets/wanikani-assistant-emily?region=us-east-1)
- **Schedule:** EventBridge cron — runs at 1 minute past every hour
