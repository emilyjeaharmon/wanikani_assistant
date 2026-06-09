#!/usr/bin/env python3
"""Start lessons for WaniKani subjects from URLs.

Only acts on items still in the lesson queue (SRS stage 0). Items at stage 1+
are skipped (lesson already started or further along).

Usage:
    python local_scripts/start_lessons_urls.py URL [URL ...]
    python local_scripts/start_lessons_urls.py --file urls.txt
    python local_scripts/start_lessons_urls.py --dry-run URL [URL ...]

URLs may point to radicals, kanji, or vocabulary pages on wanikani.com.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ASSIGNMENTS_BASE_URL = "https://api.wanikani.com/v2/assignments"
SUBJECTS_URL = "https://api.wanikani.com/v2/subjects"
REVISION = "20170710"
DEFAULT_CREDENTIALS = Path(__file__).resolve().parent.parent / "credentials.json"

TYPE_MAP = {
    "radicals": "radical",
    "kanji": "kanji",
    "vocabulary": "vocabulary",
}


def load_api_token(credentials_path: Path) -> str:
    with credentials_path.open(encoding="utf-8") as f:
        data = json.load(f)
    token = data.get("wanikani_api_token")
    if not token:
        raise ValueError(f"No 'wanikani_api_token' found in {credentials_path}")
    return token


def api_call(
    url: str,
    token: str,
    *,
    data: bytes | None = None,
    method: str = "GET",
) -> dict:
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
                print(f"Rate limited; waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"Failed after retries: {url}")


def parse_wanikani_url(url: str) -> tuple[str, str]:
    parts = urllib.parse.urlparse(url.strip()).path.strip("/").split("/")
    if len(parts) != 2:
        raise ValueError(f"Unexpected URL format: {url}")
    subject_type, slug = parts
    slug = urllib.parse.unquote(slug)
    api_type = TYPE_MAP.get(subject_type)
    if not api_type:
        raise ValueError(f"Unsupported URL type '{subject_type}' in {url}")
    return api_type, slug


def lookup_subject(token: str, api_type: str, slug: str) -> dict:
    lookup_url = (
        f"{SUBJECTS_URL}?"
        + urllib.parse.urlencode({"slugs": slug, "types": api_type})
    )
    payload = api_call(lookup_url, token)
    if not payload["data"]:
        raise ValueError(f"Subject not found for slug={slug!r} type={api_type}")
    return payload["data"][0]


def fetch_assignment(token: str, subject_id: int) -> dict | None:
    url = f"{ASSIGNMENTS_BASE_URL}?subject_ids={subject_id}"
    payload = api_call(url, token)
    if not payload["data"]:
        return None
    return payload["data"][0]


def start_assignment(token: str, assignment_id: int, *, dry_run: bool) -> None:
    if dry_run:
        return
    api_call(
        f"{ASSIGNMENTS_BASE_URL}/{assignment_id}/start",
        token,
        data=b"{}",
        method="PUT",
    )


def subject_label(subject: dict) -> str:
    data = subject["data"]
    meanings = data.get("meanings", [])
    english = next(
        (m["meaning"] for m in meanings if m.get("primary")),
        meanings[0]["meaning"] if meanings else "",
    )
    return f"{data.get('slug', '?')} ({english}) [{subject['object']}]"


def process_url(token: str, url: str, *, dry_run: bool) -> str:
    api_type, slug = parse_wanikani_url(url)
    subject = lookup_subject(token, api_type, slug)
    subject_id = subject["id"]
    label = subject_label(subject)

    assignment = fetch_assignment(token, subject_id)
    if assignment is None:
        return f"skip | {label} | no assignment (not unlocked?)"

    stage = assignment["data"]["srs_stage"]
    if stage >= 1:
        return f"skip | {label} | lesson already started (SRS stage {stage})"

    if dry_run:
        return f"dry-run | {label} | would start lesson (stage 0)"

    start_assignment(token, assignment["id"], dry_run=dry_run)
    return f"done | {label} | started lesson"


def load_urls_from_file(path: Path) -> list[str]:
    urls: list[str] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            urls.append(line)
    return urls


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Start lessons for WaniKani subject URLs.",
    )
    parser.add_argument("urls", nargs="*", help="WaniKani subject URLs")
    parser.add_argument(
        "--file",
        "-f",
        type=Path,
        help="Text file with one URL per line (# comments allowed)",
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=DEFAULT_CREDENTIALS,
        help=f"Path to credentials.json (default: {DEFAULT_CREDENTIALS})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would happen without calling the start endpoint",
    )
    args = parser.parse_args()

    urls = list(args.urls)
    if args.file:
        urls.extend(load_urls_from_file(args.file))
    urls = [u for u in urls if u.strip()]

    if not urls:
        parser.error("Provide at least one URL or use --file")

    token = load_api_token(args.credentials)
    if args.dry_run:
        print("Dry run — no changes will be made.\n")

    counts = {"done": 0, "skip": 0, "dry-run": 0, "error": 0}
    for url in urls:
        try:
            result = process_url(token, url, dry_run=args.dry_run)
        except (ValueError, urllib.error.HTTPError, urllib.error.URLError) as exc:
            result = f"error | {url} | {exc}"
        print(result)
        status = result.split(" | ", 1)[0]
        counts[status] = counts.get(status, 0) + 1

    print(
        f"\nSummary: done={counts.get('done', 0)}, "
        f"skip={counts.get('skip', 0)}, "
        f"dry-run={counts.get('dry-run', 0)}, "
        f"error={counts.get('error', 0)}"
    )


if __name__ == "__main__":
    main()
