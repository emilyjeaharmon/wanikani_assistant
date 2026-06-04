# wanikani_assistant

Personal tooling for managing WaniKani reviews and lesson queue.

## Overview

- **`local_scripts/`** — Python scripts for pulling data from the WaniKani API into CSV files for local analysis
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

### `fetch_open_reviews.py`

Fetches all assignments currently available for review, writing to `csv/csv_open_reviews.csv`. Uses the local subjects CSV as a cache to avoid re-fetching subject data.

```powershell
python local_scripts/fetch_open_reviews.py
```

Columns: `item_id, srs_stage_group, level, name, reading, english_name, type, srs_stage, document_url`

---

## Skippable reviews (Lambda)

Items listed in `csv/csv_subjects_skippable.csv` are automatically handled by an AWS Lambda function each hour. For each skippable item, the Lambda:

1. **Starts the assignment** if it is still in the lesson queue (stage 0 → stage 1)
2. **Submits a correct review** if it is in the open review queue

### Skippable subjects CSV

`csv/csv_subjects_skippable.csv` uses the same column format as `csv_subjects.csv`. Add any subject row to this file to mark it as skippable.

After updating the file, sync it to S3:

```powershell
aws s3 cp csv\csv_subjects_skippable.csv s3://wanikani-assistant-emily/csv/csv_subjects_skippable.csv
```

No Lambda redeployment is needed.

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
