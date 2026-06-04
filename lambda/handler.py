"""AWS Lambda handler — starts skippable lesson assignments and submits correct reviews.

Configuration via environment variables:
  WANIKANI_API_TOKEN      Your WaniKani v2 API token.
  SKIPPABLE_IDS_BUCKET    S3 bucket containing the skippable subjects CSV.
  SKIPPABLE_IDS_KEY       S3 object key, e.g. csv/csv_subjects_skippable.csv
"""

import csv
import io
import json
import os
import time
import urllib.error
import urllib.request

import boto3

ASSIGNMENTS_BASE_URL = "https://api.wanikani.com/v2/assignments"
REVIEWS_URL = "https://api.wanikani.com/v2/reviews"
REVISION = "20170710"


def api_call(url: str, token: str, *, data: bytes | None = None, method: str = "GET") -> dict:
    for _ in range(10):
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {token}",
                "Wanikani-Revision": REVISION,
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                reset = exc.headers.get("RateLimit-Reset")
                wait = max(int(reset) - int(time.time()), 1) if reset else 60
                print(f"Rate limited; waiting {wait}s...")
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"Failed after retries: {url}")


def fetch_assignments_for_subjects(token: str, subject_ids: set[int]) -> list[dict]:
    ids_param = ",".join(str(i) for i in subject_ids)
    assignments: list[dict] = []
    url: str | None = f"{ASSIGNMENTS_BASE_URL}?subject_ids={ids_param}"
    while url:
        payload = api_call(url, token)
        assignments.extend(payload["data"])
        url = payload["pages"]["next_url"]
    return assignments


def fetch_open_review_assignments(token: str, subject_ids: set[int]) -> list[dict]:
    ids_param = ",".join(str(i) for i in subject_ids)
    assignments: list[dict] = []
    url: str | None = f"{ASSIGNMENTS_BASE_URL}?immediately_available_for_review&subject_ids={ids_param}"
    while url:
        payload = api_call(url, token)
        assignments.extend(payload["data"])
        url = payload["pages"]["next_url"]
    return assignments


def start_assignment(token: str, assignment_id: int) -> dict:
    return api_call(
        f"{ASSIGNMENTS_BASE_URL}/{assignment_id}/start",
        token,
        data=b"{}",
        method="PUT",
    )


def submit_correct_review(token: str, subject_id: int) -> dict:
    body = json.dumps(
        {
            "review": {
                "subject_id": subject_id,
                "incorrect_meaning_answers": 0,
                "incorrect_reading_answers": 0,
            }
        }
    ).encode()
    return api_call(REVIEWS_URL, token, data=body, method="POST")


def run(token: str, skippable_ids: set[int]) -> dict:
    result = {
        "started": [],
        "reviewed": [],
    }

    # Phase 1: start any skippable items still in lessons (stage 0)
    all_assignments = fetch_assignments_for_subjects(token, skippable_ids)
    stage0 = [a for a in all_assignments if a["data"]["srs_stage"] == 0]
    print(f"Found {len(stage0)} skippable assignment(s) at stage 0.")
    for a in stage0:
        start_assignment(token, a["id"])
        sid = a["data"]["subject_id"]
        print(f"  Started assignment {a['id']} (subject_id {sid}).")
        result["started"].append(sid)

    # Phase 2: submit correct reviews for any now-open skippable items
    open_assignments = fetch_open_review_assignments(token, skippable_ids)
    print(f"Found {len(open_assignments)} open review(s) matching skippable subjects.")
    for a in open_assignments:
        subject_id = a["data"]["subject_id"]
        submit_correct_review(token, subject_id)
        print(f"  Submitted correct review for subject_id {subject_id}.")
        result["reviewed"].append(subject_id)

    print(f"Done. Started {len(result['started'])}, reviewed {len(result['reviewed'])}.")
    return result


def load_skippable_ids_from_s3(bucket: str, key: str) -> set[int]:
    s3 = boto3.client("s3")
    response = s3.get_object(Bucket=bucket, Key=key)
    content = response["Body"].read().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(content))
    return {int(row["item_id"]) for row in reader}


def lambda_handler(event: dict, context) -> dict:
    token = os.environ["WANIKANI_API_TOKEN"]
    bucket = os.environ["SKIPPABLE_IDS_BUCKET"]
    key = os.environ["SKIPPABLE_IDS_KEY"]
    skippable_ids = load_skippable_ids_from_s3(bucket, key)
    print(f"Loaded {len(skippable_ids)} skippable subject IDs from s3://{bucket}/{key}.")
    return run(token, skippable_ids)
