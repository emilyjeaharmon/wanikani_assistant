#!/usr/bin/env python3
"""Find kanji with no started vocabulary coverage.

For each kanji at the requested levels (unlocked or not), checks whether at
least one related vocabulary item (from the kanji's amalgamation list) has
reached a minimum SRS stage. Kanji with no such vocab are reported. Locked
kanji (future levels) are included so the report can cover L1–60.

Usage:
    python local_scripts/kanji_missing_vocab.py --level 30
    python local_scripts/kanji_missing_vocab.py --from 1 --to 30
    python local_scripts/kanji_missing_vocab.py --from 1 --to 30 --summary
    python local_scripts/kanji_missing_vocab.py --level 5 --min-vocab-stage 2

By default, a vocab counts as "started" at SRS stage 1+ (lesson begun).
Use --min-vocab-stage 2 to require at least one review completed.

Multi-level runs fetch all assignments once up front (fast). Use --summary
to print only per-level counts instead of full detail.
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.error
import urllib.request
from contextlib import nullcontext
from pathlib import Path

ASSIGNMENTS_URL = "https://api.wanikani.com/v2/assignments"
SUBJECTS_URL = "https://api.wanikani.com/v2/subjects"
REVISION = "20170710"
DEFAULT_CREDENTIALS = Path(__file__).resolve().parent.parent / "credentials.json"
BATCH_SIZE = 100

SRS_LABELS = {
    0: "lesson queue",
    1: "Apprentice 1",
    2: "Apprentice 2",
    3: "Apprentice 3",
    4: "Apprentice 4",
    5: "Guru 1",
    6: "Guru 2",
    7: "Master",
    8: "Enlightened",
    9: "Burned",
}


def load_api_token(credentials_path: Path) -> str:
    import json

    with credentials_path.open(encoding="utf-8") as f:
        data = json.load(f)
    token = data.get("wanikani_api_token")
    if not token:
        raise ValueError(f"No 'wanikani_api_token' found in {credentials_path}")
    return token


def api_call(url: str, token: str) -> dict:
    import json

    for _ in range(10):
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Wanikani-Revision": REVISION,
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


def fetch_paginated(token: str, url: str, *, label: str = "") -> list[dict]:
    rows: list[dict] = []
    page = 0
    while url:
        page += 1
        if label:
            print(f"  {label} (page {page})...", file=sys.stderr, flush=True)
        payload = api_call(url, token)
        rows.extend(payload["data"])
        url = payload["pages"]["next_url"]
    return rows


def fetch_subjects_by_ids(token: str, ids: list[int]) -> dict[int, dict]:
    if not ids:
        return {}
    subjects: dict[int, dict] = {}
    batches = range(0, len(ids), BATCH_SIZE)
    for i, start in enumerate(batches, 1):
        batch = ids[start : start + BATCH_SIZE]
        ids_param = ",".join(str(x) for x in batch)
        print(
            f"  vocab subjects batch {i}/{len(batches)}...",
            file=sys.stderr,
            flush=True,
        )
        for subject in fetch_paginated(
            token, f"{SUBJECTS_URL}?ids={ids_param}", label=""
        ):
            subjects[subject["id"]] = subject
    return subjects


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


def srs_label(stage: int | None) -> str:
    if stage is None:
        return "not unlocked"
    return SRS_LABELS.get(stage, f"stage {stage}")


def vocab_summary(subject: dict, stage: int | None) -> dict:
    data = subject["data"]
    return {
        "id": subject["id"],
        "slug": data.get("slug", ""),
        "reading": primary_reading(data.get("readings", [])),
        "english": primary_meaning(data.get("meanings", [])),
        "level": data.get("level"),
        "type": subject["object"],
        "url": data.get("document_url", ""),
        "stage": stage,
        "unlocked": stage is not None,
    }


class LevelAnalyzer:
    """Caches assignments and vocab subjects for fast multi-level scans."""

    def __init__(self, token: str, levels: list[int]) -> None:
        self.token = token
        self.levels = levels

        print("Loading all assignments...", file=sys.stderr, flush=True)
        assignments = fetch_paginated(token, ASSIGNMENTS_URL, label="assignments")
        self.stages = {
            a["data"]["subject_id"]: a["data"]["srs_stage"] for a in assignments
        }
        print(f"  {len(self.stages)} assignments loaded.", file=sys.stderr, flush=True)

        print("Loading kanji subjects...", file=sys.stderr, flush=True)
        self.kanji_by_level: dict[int, list[dict]] = {}
        for level in levels:
            kanji = fetch_paginated(
                token,
                f"{SUBJECTS_URL}?types=kanji&levels={level}",
                label=f"kanji L{level}",
            )
            self.kanji_by_level[level] = kanji

        vocab_ids: set[int] = set()
        for level in levels:
            for kanji in self.kanji_by_level[level]:
                # Include amalgamations for locked kanji too (future levels).
                vocab_ids.update(kanji["data"].get("amalgamation_subject_ids", []))

        print(
            f"Loading {len(vocab_ids)} related vocab subjects...",
            file=sys.stderr,
            flush=True,
        )
        self.vocab_by_id = fetch_subjects_by_ids(token, sorted(vocab_ids))
        print("Ready.\n", file=sys.stderr, flush=True)

    def gaps_for_level(self, level: int, *, min_vocab_stage: int) -> list[dict]:
        # Include locked kanji (not unlocked yet) so future levels are complete.
        all_kanji = self.kanji_by_level.get(level, [])
        if not all_kanji:
            return []

        gaps: list[dict] = []
        for kanji in sorted(all_kanji, key=lambda s: s["data"]["slug"]):
            kanji_id = kanji["id"]
            kanji_stage = self.stages.get(kanji_id)
            related_ids = kanji["data"].get("amalgamation_subject_ids", [])
            related = [
                vocab_summary(self.vocab_by_id[vid], self.stages.get(vid))
                for vid in related_ids
                if vid in self.vocab_by_id
            ]
            started = [
                v for v in related if v["unlocked"] and v["stage"] >= min_vocab_stage
            ]
            if started:
                continue

            unlocked_options = [v for v in related if v["unlocked"]]
            locked_options = [v for v in related if not v["unlocked"]]

            gaps.append(
                {
                    "kanji_id": kanji_id,
                    "kanji_slug": kanji["data"].get("slug", ""),
                    "kanji_reading": primary_reading(kanji["data"].get("readings", [])),
                    "kanji_english": primary_meaning(kanji["data"].get("meanings", [])),
                    "kanji_stage": kanji_stage,
                    "kanji_url": kanji["data"].get("document_url", ""),
                    "unlocked_options": sorted(
                        unlocked_options, key=lambda v: (v["level"], v["slug"])
                    ),
                    "locked_options": sorted(
                        locked_options, key=lambda v: (v["level"], v["slug"])
                    ),
                }
            )

        return gaps


def print_level_report(
    level: int,
    gaps: list[dict],
    *,
    min_vocab_stage: int,
    out: object = sys.stdout,
    kanji_only: bool = False,
) -> None:
    if not gaps:
        return

    threshold = (
        "lesson started (SRS 1+)"
        if min_vocab_stage == 1
        else f"SRS stage {min_vocab_stage}+"
    )
    print(f"\n=== Level {level} ===", file=out)
    print(f"{len(gaps)} kanji with no vocab at {threshold}:\n", file=out)
    for gap in gaps:
        print(
            f"{gap['kanji_slug']} ({gap['kanji_english']}) "
            f"[{gap['kanji_reading']}] — {srs_label(gap['kanji_stage'])}",
            file=out,
        )
        print(f"  {gap['kanji_url']}", file=out)
        if kanji_only:
            continue

        if gap["unlocked_options"]:
            print("  Unlocked vocab you can start:", file=out)
            for v in gap["unlocked_options"]:
                print(
                    f"    • {v['slug']} ({v['english']}) [{v['reading']}] "
                    f"L{v['level']} — {srs_label(v['stage'])}",
                    file=out,
                )
                print(f"      {v['url']}", file=out)
        else:
            print("  No related vocab unlocked yet.", file=out)

        if gap["locked_options"]:
            print(
                f"  Locked vocab ({len(gap['locked_options'])} more once kanji reach Guru):",
                file=out,
            )
            for v in gap["locked_options"][:5]:
                print(
                    f"    • {v['slug']} ({v['english']}) [{v['reading']}] L{v['level']}",
                    file=out,
                )
            if len(gap["locked_options"]) > 5:
                print(f"    … and {len(gap['locked_options']) - 5} more", file=out)
        print(file=out)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="List kanji missing vocabulary coverage (includes locked/future levels).",
    )
    parser.add_argument("--level", type=int, help="WaniKani level to analyze")
    parser.add_argument("--from", dest="from_level", type=int, help="First level (inclusive)")
    parser.add_argument("--to", dest="to_level", type=int, help="Last level (inclusive)")
    parser.add_argument(
        "--min-vocab-stage",
        type=int,
        default=1,
        help="Minimum vocab SRS stage to count as started (default: 1)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Include vocab options under each kanji (default: kanji list only)",
    )
    parser.add_argument(
        "--summary",
        action="store_true",
        help="Print only per-level gap counts (no kanji list)",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Write report to this file (default: print to terminal)",
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=DEFAULT_CREDENTIALS,
        help=f"Path to credentials.json (default: {DEFAULT_CREDENTIALS})",
    )
    args = parser.parse_args()

    if args.level is not None:
        levels = [args.level]
    elif args.from_level is not None and args.to_level is not None:
        levels = list(range(args.from_level, args.to_level + 1))
    else:
        parser.error("Provide --level N or --from A --to B")

    token = load_api_token(args.credentials)
    analyzer = LevelAnalyzer(token, levels)
    total_gaps = 0

    with args.output.open("w", encoding="utf-8") if args.output else nullcontext(sys.stdout) as out:
        if args.output:
            print(f"Writing report to {args.output}...", file=sys.stderr, flush=True)

        for level in levels:
            gaps = analyzer.gaps_for_level(level, min_vocab_stage=args.min_vocab_stage)
            total_gaps += len(gaps)
            if args.summary and len(levels) > 1:
                startable = sum(1 for g in gaps if g["unlocked_options"])
                line = (
                    f"L{level:>2}: {len(gaps):>3} kanji missing vocab "
                    f"({startable} have unlocked options)"
                )
                print(line, file=out)
            else:
                print_level_report(
                    level,
                    gaps,
                    min_vocab_stage=args.min_vocab_stage,
                    out=out,
                    kanji_only=not args.verbose,
                )

        if len(levels) > 1:
            print(
                f"\nTotal across levels {levels[0]}–{levels[-1]}: "
                f"{total_gaps} kanji missing vocab coverage.",
                file=out,
            )

    if args.output:
        print(f"Wrote {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
