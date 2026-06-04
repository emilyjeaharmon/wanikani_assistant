#!/usr/bin/env python3
"""Fetch all WaniKani subject item IDs and URLs and write them to a CSV file."""

import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_BASE = "https://api.wanikani.com/v2/subjects"
ASSIGNMENTS_URL = "https://api.wanikani.com/v2/assignments"
REVISION = "20170710"
DEFAULT_CREDENTIALS = Path(__file__).resolve().parent / "credentials.json"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "csv" / "csv_subjects.csv"


def english_name_from_document_url(document_url: str) -> str:
    if not document_url:
        return ""
    return urllib.parse.unquote(document_url.rstrip("/").split("/")[-1])


def primary_meaning(meanings: list) -> str:
    for m in meanings:
        if m.get("primary"):
            return m.get("meaning", "")
    return meanings[0].get("meaning", "") if meanings else ""


def primary_reading(readings: list) -> str:
    for r in readings:
        if r.get("primary"):
            return r.get("reading", "")
    return readings[0].get("reading", "") if readings else ""


def srs_stage_group(stage) -> str:
    if stage == "" or stage is None:
        return ""
    stage = int(stage)
    if stage == 0:
        return "Initiation"
    if stage <= 4:
        return "Apprentice"
    if stage <= 6:
        return "Guru"
    if stage == 7:
        return "Master"
    if stage == 8:
        return "Enlightened"
    if stage == 9:
        return "Burned"
    return ""


def load_api_token(credentials_path: Path) -> str:
    with credentials_path.open(encoding="utf-8") as f:
        data = json.load(f)
    token = data.get("wanikani_api_token")
    if not token:
        raise ValueError(f"No 'wanikani_api_token' found in {credentials_path}")
    return token


def api_request(url: str, token: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Wanikani-Revision": REVISION,
        },
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def fetch_srs_stages(token: str) -> dict[int, int]:
    """Return a mapping of subject_id -> srs_stage for all user assignments."""
    stages: dict[int, int] = {}
    url: str | None = ASSIGNMENTS_URL

    while url:
        for attempt in range(5):
            try:
                payload = api_request(url, token)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    reset = exc.headers.get("RateLimit-Reset")
                    wait = max(int(reset) - int(time.time()), 1) if reset else 60
                    print(f"Rate limited; waiting {wait}s...", file=sys.stderr)
                    time.sleep(wait)
                    continue
                raise
        else:
            raise RuntimeError(f"Failed after retries: {url}")

        for assignment in payload["data"]:
            stages[assignment["data"]["subject_id"]] = assignment["data"]["srs_stage"]

        url = payload["pages"]["next_url"]
        print(f"Fetched {len(stages)} / {payload['total_count']} assignments...", file=sys.stderr)

    return stages


def fetch_all_subjects(token: str) -> list[dict]:
    rows: list[dict] = []
    url: str | None = API_BASE

    while url:
        for attempt in range(5):
            try:
                payload = api_request(url, token)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    reset = exc.headers.get("RateLimit-Reset")
                    wait = max(int(reset) - int(time.time()), 1) if reset else 60
                    print(f"Rate limited; waiting {wait}s...", file=sys.stderr)
                    time.sleep(wait)
                    continue
                raise
        else:
            raise RuntimeError(f"Failed after retries: {url}")

        for subject in payload["data"]:
            document_url = subject["data"].get("document_url", "")
            rows.append(
                {
                    "item_id": subject["id"],
                    "name": subject["data"].get("slug", ""),
                    "reading": primary_reading(subject["data"].get("readings", [])),
                    "english_name": primary_meaning(subject["data"].get("meanings", [])),
                    "type": subject["object"],
                    "level": subject["data"].get("level", ""),
                    "document_url": document_url,
                }
            )

        url = payload["pages"]["next_url"]
        print(f"Fetched {len(rows)} / {payload['total_count']} subjects...", file=sys.stderr)

    return rows


def write_csv(rows: list[dict], output_path: Path) -> None:
    fieldnames = ["item_id", "srs_stage_group", "level", "name", "reading", "english_name", "type", "srs_stage", "document_url"]
    with output_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    credentials_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CREDENTIALS
    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUTPUT

    token = load_api_token(credentials_path)
    srs_stages = fetch_srs_stages(token)
    rows = fetch_all_subjects(token)
    for row in rows:
        stage = srs_stages.get(row["item_id"], "")
        row["srs_stage"] = stage
        row["srs_stage_group"] = srs_stage_group(stage)
    write_csv(rows, output_path)
    print(f"Wrote {len(rows)} rows to {output_path}")


if __name__ == "__main__":
    main()
