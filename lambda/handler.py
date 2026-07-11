"""AWS Lambda handler — starts skippable lesson assignments and submits correct reviews.

Also auto-passes non-skippable radicals until they reach Guru (SRS stage 5), which
unlocks their dependent kanji. After Guru, those radicals return to normal review.
Items on the skippable list are always auto-passed at every SRS stage.

Current-level kanji still in the lesson queue (stage 0) are auto-started but not
auto-reviewed unless they are on the skippable list.

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
USER_URL = "https://api.wanikani.com/v2/user"
REVISION = "20170710"
USER_AGENT = "wanikani-assistant/1.0"
PASSING_STAGE = 5


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
                "User-Agent": USER_AGENT,
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


def fetch_paginated(token: str, url: str) -> list[dict]:
    rows: list[dict] = []
    while url:
        payload = api_call(url, token)
        rows.extend(payload["data"])
        url = payload["pages"]["next_url"]
    return rows


def fetch_assignments_for_subjects(token: str, subject_ids: set[int]) -> list[dict]:
    if not subject_ids:
        return []
    ids_param = ",".join(str(i) for i in subject_ids)
    return fetch_paginated(token, f"{ASSIGNMENTS_BASE_URL}?subject_ids={ids_param}")


def fetch_open_review_assignments(token: str, subject_ids: set[int]) -> list[dict]:
    if not subject_ids:
        return []
    ids_param = ",".join(str(i) for i in subject_ids)
    return fetch_paginated(
        token,
        f"{ASSIGNMENTS_BASE_URL}?immediately_available_for_review&subject_ids={ids_param}",
    )


def fetch_user_level(token: str) -> int:
    return api_call(USER_URL, token)["data"]["level"]


def fetch_pre_guru_radical_subject_ids(token: str, level: int) -> set[int]:
    """Radicals at the current level still in lessons or apprentice (before Guru)."""
    assignments = fetch_paginated(
        token,
        f"{ASSIGNMENTS_BASE_URL}?subject_types=radical&srs_stages=0,1,2,3,4&levels={level}",
    )
    return {a["data"]["subject_id"] for a in assignments}


def fetch_unstarted_kanji_subject_ids(token: str, level: int) -> set[int]:
    """Kanji at the current level still in the lesson queue (stage 0)."""
    assignments = fetch_paginated(
        token,
        f"{ASSIGNMENTS_BASE_URL}?subject_types=kanji&srs_stages=0&levels={level}",
    )
    return {a["data"]["subject_id"] for a in assignments}


def build_auto_sets(token: str, skippable_ids: set[int]) -> tuple[int, set[int], set[int]]:
    """Return (level, review_auto_ids, lesson_start_ids)."""
    level = fetch_user_level(token)
    pre_guru_radicals = fetch_pre_guru_radical_subject_ids(token, level) - skippable_ids
    unstarted_kanji = fetch_unstarted_kanji_subject_ids(token, level)
    review_auto_ids = skippable_ids | pre_guru_radicals
    lesson_start_ids = review_auto_ids | unstarted_kanji
    print(
        f"Level {level}: {len(skippable_ids)} skippable, "
        f"{len(pre_guru_radicals)} pre-Guru radicals (non-skippable), "
        f"{len(unstarted_kanji)} unstarted kanji, "
        f"{len(review_auto_ids)} auto-review, {len(lesson_start_ids)} auto-start."
    )
    return level, review_auto_ids, lesson_start_ids


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
        "started_skippable": [],
        "started_auto_radical": [],
        "started_auto_kanji": [],
        "reviewed_skippable": [],
        "reviewed_auto_radical": [],
    }

    _, review_auto_ids, lesson_start_ids = build_auto_sets(token, skippable_ids)

    all_assignments = fetch_assignments_for_subjects(token, lesson_start_ids)
    stage0 = [a for a in all_assignments if a["data"]["srs_stage"] == 0]
    print(f"Found {len(stage0)} assignment(s) at stage 0.")
    for a in stage0:
        start_assignment(token, a["id"])
        sid = a["data"]["subject_id"]
        subject_type = a["data"]["subject_type"]
        print(f"  Started assignment {a['id']} (subject_id {sid}, {subject_type}).")
        result["started"].append(sid)
        if sid in skippable_ids:
            result["started_skippable"].append(sid)
        elif subject_type == "kanji":
            result["started_auto_kanji"].append(sid)
        else:
            result["started_auto_radical"].append(sid)

    open_assignments = fetch_open_review_assignments(token, review_auto_ids)
    print(f"Found {len(open_assignments)} open review(s).")
    for a in open_assignments:
        subject_id = a["data"]["subject_id"]
        submit_correct_review(token, subject_id)
        print(f"  Submitted correct review for subject_id {subject_id}.")
        result["reviewed"].append(subject_id)
        if subject_id in skippable_ids:
            result["reviewed_skippable"].append(subject_id)
        else:
            result["reviewed_auto_radical"].append(subject_id)

    print(
        f"Done. Started {len(result['started'])} "
        f"({len(result['started_skippable'])} skippable, "
        f"{len(result['started_auto_radical'])} auto-radical, "
        f"{len(result['started_auto_kanji'])} auto-kanji), "
        f"reviewed {len(result['reviewed'])} "
        f"({len(result['reviewed_skippable'])} skippable, "
        f"{len(result['reviewed_auto_radical'])} auto-radical)."
    )
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
