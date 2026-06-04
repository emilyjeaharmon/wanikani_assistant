#!/usr/bin/env python3
"""Fetch all currently available WaniKani reviews and write them to a CSV file."""

import csv
import sys
from pathlib import Path

from fetch_subjects import (
    DEFAULT_CREDENTIALS,
    api_request,
    english_name_from_document_url,
    load_api_token,
    primary_meaning,
    primary_reading,
    srs_stage_group,
)

ASSIGNMENTS_URL = "https://api.wanikani.com/v2/assignments?immediately_available_for_review"
SUBJECTS_URL = "https://api.wanikani.com/v2/subjects"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "csv" / "csv_open_reviews.csv"
DEFAULT_SUBJECTS = Path(__file__).resolve().parent / "csv" / "csv_subjects.csv"
SUBJECT_BATCH_SIZE = 100


def fetch_open_review_assignments(token: str) -> list[dict]:
    assignments: list[dict] = []
    url: str | None = ASSIGNMENTS_URL

    while url:
        payload = api_request(url, token)
        assignments.extend(payload["data"])
        url = payload["pages"]["next_url"]
        print(
            f"Fetched {len(assignments)} / {payload['total_count']} open reviews...",
            file=sys.stderr,
        )

    return assignments


def load_subjects_lookup(subjects_path: Path) -> dict[int, dict]:
    lookup: dict[int, dict] = {}
    with subjects_path.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            lookup[int(row["item_id"])] = row
    return lookup


def fetch_subjects_by_ids(token: str, subject_ids: list[int]) -> dict[int, dict]:
    subjects: dict[int, dict] = {}

    for start in range(0, len(subject_ids), SUBJECT_BATCH_SIZE):
        batch = subject_ids[start : start + SUBJECT_BATCH_SIZE]
        ids_param = ",".join(str(subject_id) for subject_id in batch)
        url = f"{SUBJECTS_URL}?ids={ids_param}"
        payload = api_request(url, token)
        for subject in payload["data"]:
            document_url = subject["data"].get("document_url", "")
            subjects[subject["id"]] = {
                "item_id": str(subject["id"]),
                "name": subject["data"].get("slug", ""),
                "reading": primary_reading(subject["data"].get("readings", [])),
                "english_name": primary_meaning(subject["data"].get("meanings", [])),
                "type": subject["object"],
                "level": subject["data"].get("level", ""),
                "document_url": document_url,
            }

    return subjects


def resolve_subject(
    subject_id: int,
    subjects_lookup: dict[int, dict],
    api_subjects: dict[int, dict],
) -> dict | None:
    if subject_id in subjects_lookup:
        return subjects_lookup[subject_id]
    if subject_id in api_subjects:
        return api_subjects[subject_id]
    return None


def build_rows(
    assignments: list[dict],
    subjects_lookup: dict[int, dict],
    api_subjects: dict[int, dict],
) -> list[dict]:
    rows: list[dict] = []

    for assignment in assignments:
        data = assignment["data"]
        subject_id = data["subject_id"]
        subject = resolve_subject(subject_id, subjects_lookup, api_subjects)
        if subject is None:
            print(f"Warning: no subject found for id {subject_id}", file=sys.stderr)
            continue

        stage = data["srs_stage"]
        rows.append(
            {
                "item_id": subject_id,
                "level": subject.get("level", ""),
                "name": subject["name"],
                "reading": subject.get("reading", ""),
                "english_name": subject.get("english_name", ""),
                "type": subject["type"],
                "srs_stage": stage,
                "srs_stage_group": srs_stage_group(stage),
                "document_url": subject["document_url"],
            }
        )

    return rows


def write_csv(rows: list[dict], output_path: Path) -> None:
    fieldnames = [
        "item_id",
        "srs_stage_group",
        "level",
        "name",
        "reading",
        "english_name",
        "type",
        "srs_stage",
        "document_url",
    ]
    with output_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    credentials_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CREDENTIALS
    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_OUTPUT
    subjects_path = Path(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_SUBJECTS

    token = load_api_token(credentials_path)
    assignments = fetch_open_review_assignments(token)

    if not assignments:
        write_csv([], output_path)
        print(f"No open reviews. Wrote empty CSV to {output_path}")
        return

    subjects_lookup: dict[int, dict] = {}
    if subjects_path.exists():
        subjects_lookup = load_subjects_lookup(subjects_path)
        print(f"Loaded {len(subjects_lookup)} subjects from {subjects_path}", file=sys.stderr)
    else:
        print(f"Warning: {subjects_path} not found; fetching subjects from API", file=sys.stderr)

    subject_ids = [assignment["data"]["subject_id"] for assignment in assignments]
    missing_ids = [subject_id for subject_id in subject_ids if subject_id not in subjects_lookup]
    api_subjects = fetch_subjects_by_ids(token, missing_ids) if missing_ids else {}

    rows = build_rows(assignments, subjects_lookup, api_subjects)
    write_csv(rows, output_path)
    print(f"Wrote {len(rows)} rows to {output_path}")


if __name__ == "__main__":
    main()
