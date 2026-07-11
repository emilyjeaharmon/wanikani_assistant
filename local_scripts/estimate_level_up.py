#!/usr/bin/env python3
"""Estimate WaniKani level-up timing for the current level and upcoming levels.

Level-up requires ceil(90% * kanji_count) kanji at Guru (SRS stage 5+).
Vocabulary and radical completion do not count toward the 90% threshold, but
radicals unlock kanji that depend on them.

Usage:
    python local_scripts/estimate_level_up.py
    python local_scripts/estimate_level_up.py --level 29 --lookahead 15
    python local_scripts/estimate_level_up.py --csv csv/level_up_projection.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

API_BASE = "https://api.wanikani.com/v2"
REVISION = "20170710"
USER_AGENT = "wanikani-assistant/1.0"
DEFAULT_CREDENTIALS = Path(__file__).resolve().parent.parent / "credentials.json"
DEFAULT_CSV = Path(__file__).resolve().parent.parent / "csv" / "level_up_projection.csv"
GOAL_LEVEL = 60

# Levels where most kanji unlock on day 1 (single ~82h cycle is enough).
FAST_LEVELS = {1, 2, 43, 44, 46, 47, *range(49, 61)}

KANJI_SRS_ID = 1
RADICAL_SRS_ID = 2


@dataclass(frozen=True)
class SrsSystem:
    system_id: int
    name: str
    passing_stage: int
    intervals: dict[int, int]  # stage position -> seconds


def load_api_token(credentials_path: Path) -> str:
    with credentials_path.open(encoding="utf-8") as f:
        data = json.load(f)
    token = data.get("wanikani_api_token")
    if not token:
        raise ValueError(f"No 'wanikani_api_token' found in {credentials_path}")
    return token


def api_request(url: str, token: str) -> dict:
    for _ in range(15):
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Wanikani-Revision": REVISION,
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
                print(f"Rate limited; waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
    raise RuntimeError(f"Failed after retries: {url}")


def fetch_paginated(token: str, url: str) -> list[dict]:
    rows: list[dict] = []
    while url:
        payload = api_request(url, token)
        rows.extend(payload["data"])
        url = payload["pages"]["next_url"]
    return rows


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def ceil_percent(count: int, percent: float) -> int:
    return math.ceil(count * percent)


def format_duration(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h"


def format_dt(dt: datetime) -> str:
    return dt.astimezone().strftime("%Y-%m-%d %H:%M %Z")


def load_srs_systems(token: str) -> dict[int, SrsSystem]:
    systems: dict[int, SrsSystem] = {}
    for item in fetch_paginated(token, f"{API_BASE}/spaced_repetition_systems"):
        data = item["data"]
        intervals = {
            stage["position"]: stage["interval"]
            for stage in data["stages"]
            if stage["interval"] is not None
        }
        systems[item["id"]] = SrsSystem(
            system_id=item["id"],
            name=data["name"],
            passing_stage=data["passing_stage_position"],
            intervals=intervals,
        )
    return systems


def seconds_to_passing_stage(
    from_stage: int,
    srs: SrsSystem,
    *,
    include_passing_interval: bool = False,
) -> int:
    """Sum SRS intervals from from_stage up to (but not past) passing stage."""
    total = 0
    end = srs.passing_stage + (1 if include_passing_interval else 0)
    for position in range(from_stage + 1, end):
        total += srs.intervals.get(position, 0)
    return total


def lesson_to_passing_seconds(srs: SrsSystem) -> int:
    return seconds_to_passing_stage(0, srs)


def fetch_user_level(token: str) -> int:
    return api_request(f"{API_BASE}/user", token)["data"]["level"]


def fetch_level_progressions(token: str) -> list[dict]:
    return fetch_paginated(token, f"{API_BASE}/level_progressions")


def fetch_kanji_subjects(token: str) -> dict[int, dict]:
    """Return kanji subjects keyed by id."""
    kanji: dict[int, dict] = {}
    for item in fetch_paginated(token, f"{API_BASE}/subjects?types=kanji"):
        kanji[item["id"]] = item
    return kanji


def fetch_radical_subjects(token: str) -> dict[int, dict]:
    radicals: dict[int, dict] = {}
    for item in fetch_paginated(token, f"{API_BASE}/subjects?types=radical"):
        radicals[item["id"]] = item
    return radicals


def fetch_level_assignments(token: str, level: int) -> list[dict]:
    return fetch_paginated(
        token,
        f"{API_BASE}/assignments?levels={level}&subject_types=kanji,radical",
    )


def kanji_needed(level_kanji_count: int) -> int:
    return ceil_percent(level_kanji_count, 0.9)


def optimistic_guru_time(
    assignment: dict,
    *,
    now: datetime,
    kanji_srs: SrsSystem,
) -> datetime:
    data = assignment["data"]
    stage = data["srs_stage"]

    if stage >= kanji_srs.passing_stage:
        passed_at = parse_dt(data.get("passed_at"))
        return passed_at or now

    if stage == 0:
        base = now
    else:
        available_at = parse_dt(data.get("available_at"))
        base = max(available_at, now) if available_at else now

    remaining = seconds_to_passing_stage(stage, kanji_srs)
    return base + timedelta(seconds=remaining)


def realistic_guru_time(
    optimistic: datetime,
    assignment: dict,
    *,
    now: datetime,
    kanji_srs: SrsSystem,
    review_slop: timedelta,
) -> datetime:
    data = assignment["data"]
    stage = data["srs_stage"]
    if stage >= kanji_srs.passing_stage:
        return optimistic

    reviews_remaining = max(kanji_srs.passing_stage - max(stage, 1), 0)
    if stage == 0:
        reviews_remaining = kanji_srs.passing_stage
    slop = review_slop * reviews_remaining
    return optimistic + slop


def analyze_current_level(
    token: str,
    level: int,
    kanji_srs: SrsSystem,
    *,
    review_slop_hours: float,
) -> dict:
    now = datetime.now(timezone.utc)
    review_slop = timedelta(hours=review_slop_hours)
    assignments = fetch_level_assignments(token, level)
    kanji_assignments = [a for a in assignments if a["data"]["subject_type"] == "kanji"]
    radical_assignments = [a for a in assignments if a["data"]["subject_type"] == "radical"]

    total = len(kanji_assignments)
    needed = kanji_needed(total)
    guru_count = sum(1 for a in kanji_assignments if a["data"]["srs_stage"] >= kanji_srs.passing_stage)
    radical_guru = sum(
        1 for a in radical_assignments if a["data"]["srs_stage"] >= kanji_srs.passing_stage
    )

    projections: list[tuple[dict, datetime, datetime]] = []
    for assignment in kanji_assignments:
        opt = optimistic_guru_time(assignment, now=now, kanji_srs=kanji_srs)
        real = realistic_guru_time(
            opt,
            assignment,
            now=now,
            kanji_srs=kanji_srs,
            review_slop=review_slop,
        )
        projections.append((assignment, opt, real))

    not_guru = [p for p in projections if p[0]["data"]["srs_stage"] < kanji_srs.passing_stage]
    not_guru_opt = sorted(not_guru, key=lambda row: row[1])
    not_guru_real = sorted(not_guru, key=lambda row: row[2])

    remaining = max(needed - guru_count, 0)
    optimistic_eta = None
    realistic_eta = None
    if remaining == 0:
        optimistic_eta = now
        realistic_eta = now
    elif len(not_guru_opt) >= remaining:
        optimistic_eta = not_guru_opt[remaining - 1][1]
        realistic_eta = not_guru_real[remaining - 1][2]

    return {
        "level": level,
        "now": now,
        "kanji_total": total,
        "kanji_needed": needed,
        "kanji_guru": guru_count,
        "radical_total": len(radical_assignments),
        "radical_guru": radical_guru,
        "remaining": remaining,
        "optimistic_eta": optimistic_eta,
        "realistic_eta": realistic_eta,
        "projections": projections,
    }


def historical_level_durations(progressions: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for item in progressions:
        data = item["data"]
        unlocked = parse_dt(data.get("unlocked_at"))
        passed = parse_dt(data.get("passed_at"))
        if not unlocked or not passed:
            continue
        duration = (passed - unlocked).total_seconds()
        rows.append(
            {
                "level": data["level"],
                "days": duration / 86400,
                "unlocked_at": unlocked,
                "passed_at": passed,
                "fast": data["level"] in FAST_LEVELS,
            }
        )
    rows.sort(key=lambda row: row["level"])
    return rows


def historical_summary(durations: list[dict]) -> dict:
    if not durations:
        return {
            "median_all": None,
            "median_normal": None,
            "median_fast": None,
            "recent_normal": None,
        }

    def median(values: list[float]) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        return (ordered[mid - 1] + ordered[mid]) / 2

    all_days = [row["days"] for row in durations]
    normal_days = [row["days"] for row in durations if not row["fast"]]
    fast_days = [row["days"] for row in durations if row["fast"]]
    recent_normal_days = [
        row["days"]
        for row in durations[-8:]
        if not row["fast"] and row["days"] <= 30
    ]

    return {
        "median_all": median(all_days),
        "median_normal": median(normal_days),
        "median_fast": median(fast_days),
        "recent_normal": median(recent_normal_days) if recent_normal_days else median(normal_days),
    }


def immediate_kanji_at_level_start(
    level: int,
    kanji_by_level: dict[int, list[dict]],
    radicals: dict[int, dict],
) -> int:
    """Kanji that do not depend on radicals introduced at this level."""
    level_radical_ids = {
        radical_id
        for radical_id, radical in radicals.items()
        if radical["data"]["level"] == level
    }
    immediate = 0
    for kanji in kanji_by_level.get(level, []):
        components = set(kanji["data"].get("component_subject_ids", []))
        if not components.intersection(level_radical_ids):
            immediate += 1
    return immediate


def theoretical_minimum_days(
    level: int,
    kanji_count: int,
    immediate_kanji: int,
    kanji_srs: SrsSystem,
    radical_srs: SrsSystem,
) -> tuple[float, str]:
    kanji_floor = lesson_to_passing_seconds(kanji_srs) / 86400
    radical_floor = lesson_to_passing_seconds(radical_srs) / 86400
    needed = kanji_needed(kanji_count)

    if level in FAST_LEVELS or immediate_kanji >= needed:
        return kanji_floor, "fast"

    # Normal level: first batch gurus on kanji cycle; second batch after radical cycle.
    return radical_floor + kanji_floor, "normal"


def build_kanji_by_level(kanji_subjects: dict[int, dict]) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = {}
    for subject in kanji_subjects.values():
        level = subject["data"]["level"]
        grouped.setdefault(level, []).append(subject)
    return grouped


def print_current_level_report(result: dict, kanji_srs: SrsSystem) -> None:
    now = result["now"]
    print(f"=== Level {result['level']} (current) ===")
    print(
        f"Kanji Guru: {result['kanji_guru']}/{result['kanji_total']} "
        f"(need {result['kanji_needed']} for level-up)"
    )
    print(f"Radicals Guru: {result['radical_guru']}/{result['radical_total']}")

    stage_counts: dict[int, int] = {}
    for assignment, _, _ in result["projections"]:
        stage = assignment["data"]["srs_stage"]
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
    if stage_counts:
        parts = ", ".join(f"stage {stage}: {count}" for stage, count in sorted(stage_counts.items()))
        print(f"Kanji SRS distribution: {parts}")

    if result["remaining"] == 0:
        print("Status: ready to level up (90% kanji at Guru+)")
        return

    print(f"Remaining: {result['remaining']} more kanji must reach Guru")
    if result["optimistic_eta"]:
        opt_seconds = (result["optimistic_eta"] - now).total_seconds()
        print(
            f"Optimistic ETA: {format_dt(result['optimistic_eta'])} "
            f"({format_duration(opt_seconds)} from now)"
        )
    if result["realistic_eta"]:
        real_seconds = (result["realistic_eta"] - now).total_seconds()
        print(
            f"Realistic ETA:  {format_dt(result['realistic_eta'])} "
            f"({format_duration(real_seconds)} from now)"
        )
        print("  (realistic adds review slop for sleep/scheduling; see --review-slop-hours)")


def print_historical_report(durations: list[dict], summary: dict) -> None:
    print("\n=== Historical level-up times (unlocked → passed) ===")
    if not durations:
        print("No completed level progressions found.")
        return

    for row in durations[-12:]:
        tag = "fast" if row["fast"] else "normal"
        print(f"  L{row['level']:>2}: {row['days']:5.1f} days  ({tag})")

    if summary["median_normal"] is not None:
        print(f"\nMedian normal level: {summary['median_normal']:.1f} days")
    if summary["median_fast"] is not None:
        print(f"Median fast level:   {summary['median_fast']:.1f} days")
    if summary["median_all"] is not None:
        print(f"Median all levels:   {summary['median_all']:.1f} days")
    if summary["recent_normal"] is not None:
        print(f"Recent normal pace:  {summary['recent_normal']:.1f} days (last 8, ≤30d each)")


def build_upcoming_level_rows(
    start_level: int,
    end_level: int,
    kanji_by_level: dict[int, list[dict]],
    radicals: dict[int, dict],
    kanji_srs: SrsSystem,
    radical_srs: SrsSystem,
) -> list[dict]:
    rows: list[dict] = []
    for level in range(start_level, end_level):
        kanji_list = kanji_by_level.get(level, [])
        count = len(kanji_list)
        if not count:
            continue
        needed = kanji_needed(count)
        immediate = immediate_kanji_at_level_start(level, kanji_by_level, radicals)
        min_days, kind = theoretical_minimum_days(
            level,
            count,
            immediate,
            kanji_srs,
            radical_srs,
        )
        rows.append(
            {
                "level": level,
                "kanji_count": count,
                "kanji_needed": needed,
                "immediate_kanji": immediate,
                "min_days": min_days,
                "level_type": kind,
                "is_fast": kind == "fast",
            }
        )
    return rows


def days_for_level(
    level_row: dict,
    summary: dict,
    *,
    scenario: str,
) -> float:
    if scenario == "optimistic":
        return level_row["min_days"]
    if scenario == "realistic":
        return level_row["min_days"] + 0.5
    if scenario == "historical":
        if level_row["level_type"] == "fast" and summary["median_fast"] is not None:
            return summary["median_fast"]
        if summary["median_normal"] is not None:
            return summary["median_normal"]
        return level_row["min_days"] + 2
    if scenario == "recent":
        if level_row["level_type"] == "fast" and summary["median_fast"] is not None:
            return summary["median_fast"]
        if summary["recent_normal"] is not None:
            return summary["recent_normal"]
        return level_row["min_days"] + 2
    raise ValueError(f"Unknown scenario: {scenario}")


def project_to_goal(
    current: dict,
    upcoming_rows: list[dict],
    summary: dict,
    *,
    goal_level: int = GOAL_LEVEL,
) -> list[dict]:
    """Project arrival at goal_level under several scenarios."""
    now = current["now"]
    scenarios = [
        ("optimistic", "SRS floor + instant reviews", "optimistic_eta"),
        ("realistic", "SRS floor + review slop", "realistic_eta"),
        ("historical", "Your median pace by level type", "realistic_eta"),
        ("recent", "Your recent normal-level median (last 8, ≤30d)", "realistic_eta"),
    ]
    projections: list[dict] = []

    for scenario_key, description, current_eta_key in scenarios:
        current_finish = current.get(current_eta_key) or now
        days_remaining = max((current_finish - now).total_seconds() / 86400, 0)

        future_rows = [row for row in upcoming_rows if row["level"] > current["level"]]
        per_level: list[dict] = []
        for row in future_rows:
            if row["level"] >= goal_level:
                continue
            level_days = days_for_level(row, summary, scenario=scenario_key)
            days_remaining += level_days
            per_level.append({"level": row["level"], "days": level_days})

        projected_at = now + timedelta(days=days_remaining)
        projections.append(
            {
                "scenario": scenario_key,
                "description": description,
                "projected_at": projected_at,
                "days_remaining": days_remaining,
                "levels_remaining": len(future_rows),
                "per_level": per_level,
            }
        )

    return projections


def write_projection_csv(
    path: Path,
    *,
    current: dict,
    upcoming_rows: list[dict],
    durations: list[dict],
    l60_projections: list[dict],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "section",
        "level",
        "kanji_total",
        "kanji_guru",
        "kanji_needed",
        "remaining",
        "optimistic_eta",
        "realistic_eta",
        "kanji_count",
        "immediate_kanji",
        "min_days",
        "level_type",
        "days",
        "unlocked_at",
        "passed_at",
        "scenario",
        "description",
        "projected_at",
        "days_remaining",
        "levels_remaining",
    ]

    def iso(dt: datetime | None) -> str:
        return dt.isoformat() if dt else ""

    rows: list[dict] = []

    rows.append(
        {
            "section": "current",
            "level": current["level"],
            "kanji_total": current["kanji_total"],
            "kanji_guru": current["kanji_guru"],
            "kanji_needed": current["kanji_needed"],
            "remaining": current["remaining"],
            "optimistic_eta": iso(current["optimistic_eta"]),
            "realistic_eta": iso(current["realistic_eta"]),
        }
    )

    for item in upcoming_rows:
        rows.append(
            {
                "section": "upcoming",
                "level": item["level"],
                "kanji_count": item["kanji_count"],
                "kanji_needed": item["kanji_needed"],
                "immediate_kanji": item["immediate_kanji"],
                "min_days": f"{item['min_days']:.2f}",
                "level_type": item["level_type"],
            }
        )

    for item in durations:
        rows.append(
            {
                "section": "historical",
                "level": item["level"],
                "days": f"{item['days']:.2f}",
                "level_type": "fast" if item["fast"] else "normal",
                "unlocked_at": iso(item["unlocked_at"]),
                "passed_at": iso(item["passed_at"]),
            }
        )

    for item in l60_projections:
        rows.append(
            {
                "section": "goal_projection",
                "scenario": item["scenario"],
                "description": item["description"],
                "projected_at": iso(item["projected_at"]),
                "days_remaining": f"{item['days_remaining']:.1f}",
                "levels_remaining": item["levels_remaining"],
            }
        )
        for level_row in item["per_level"]:
            rows.append(
                {
                    "section": "goal_per_level",
                    "scenario": item["scenario"],
                    "level": level_row["level"],
                    "days": f"{level_row['days']:.2f}",
                }
            )

    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def print_upcoming_report(
    upcoming_rows: list[dict],
    *,
    start_level: int,
    lookahead: int,
    kanji_srs: SrsSystem,
    radical_srs: SrsSystem,
) -> None:
    end_level = start_level + lookahead
    visible = [row for row in upcoming_rows if start_level <= row["level"] < end_level]
    print(f"\n=== Upcoming levels (theoretical minimum, levels {start_level}–{end_level - 1}) ===")
    kanji_floor_days = lesson_to_passing_seconds(kanji_srs) / 86400
    radical_floor_days = lesson_to_passing_seconds(radical_srs) / 86400
    print(
        f"SRS floors: kanji lesson→Guru {kanji_floor_days:.2f}d "
        f"({int(kanji_floor_days * 24)}h), "
        f"radical lesson→Guru {radical_floor_days:.2f}d "
        f"({int(radical_floor_days * 24)}h)"
    )
    print(f"{'Level':>5}  {'Kanji':>5}  {'Need':>4}  {'Day-1':>5}  {'Min days':>8}  Type")
    print("-" * 52)

    for row in visible:
        fast_marker = " ⚡" if row["is_fast"] else ""
        print(
            f"{row['level']:>5}  {row['kanji_count']:>5}  {row['kanji_needed']:>4}  "
            f"{row['immediate_kanji']:>5}  {row['min_days']:>7.2f}d  "
            f"{row['level_type']}{fast_marker}"
        )


def print_l60_report(projections: list[dict], *, goal_level: int = GOAL_LEVEL) -> None:
    print(f"\n=== Projected arrival at level {goal_level} ===")
    for item in projections:
        print(
            f"  {item['scenario']:11}  {format_dt(item['projected_at'])}  "
            f"({item['days_remaining']:.0f} days) — {item['description']}"
        )

def main() -> None:
    parser = argparse.ArgumentParser(description="Estimate WaniKani level-up timing.")
    parser.add_argument(
        "--level",
        type=int,
        default=None,
        help="Level to analyze (default: your current level)",
    )
    parser.add_argument(
        "--lookahead",
        type=int,
        default=12,
        help="How many upcoming levels to show theoretical minimums for",
    )
    parser.add_argument(
        "--review-slop-hours",
        type=float,
        default=2.0,
        help="Extra hours added per remaining review for the realistic ETA (default: 2)",
    )
    parser.add_argument(
        "--goal-level",
        type=int,
        default=GOAL_LEVEL,
        help=f"Target level for long-range projection (default: {GOAL_LEVEL})",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        nargs="?",
        const=DEFAULT_CSV,
        default=None,
        help=f"Write projection CSV (default path: {DEFAULT_CSV})",
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=DEFAULT_CREDENTIALS,
        help=f"Path to credentials.json (default: {DEFAULT_CREDENTIALS})",
    )
    args = parser.parse_args()

    token = load_api_token(args.credentials)
    srs_systems = load_srs_systems(token)
    kanji_srs = srs_systems[KANJI_SRS_ID]
    radical_srs = srs_systems[RADICAL_SRS_ID]

    current_level = fetch_user_level(token)
    target_level = args.level or current_level

    print("Fetching level progression history...")
    durations = historical_level_durations(fetch_level_progressions(token))
    summary = historical_summary(durations)

    print("Fetching subjects...")
    kanji_subjects = fetch_kanji_subjects(token)
    radicals = fetch_radical_subjects(token)
    kanji_by_level = build_kanji_by_level(kanji_subjects)

    upcoming_rows = build_upcoming_level_rows(
        target_level,
        args.goal_level,
        kanji_by_level,
        radicals,
        kanji_srs,
        radical_srs,
    )

    print("Analyzing assignments...")
    current = analyze_current_level(
        token,
        target_level,
        kanji_srs,
        review_slop_hours=args.review_slop_hours,
    )

    l60_projections = project_to_goal(
        current,
        upcoming_rows,
        summary,
        goal_level=args.goal_level,
    )

    print()
    print_current_level_report(current, kanji_srs)
    print_historical_report(durations, summary)
    print_upcoming_report(
        upcoming_rows,
        start_level=target_level,
        lookahead=args.lookahead,
        kanji_srs=kanji_srs,
        radical_srs=radical_srs,
    )
    print_l60_report(l60_projections, goal_level=args.goal_level)

    next_fast = [
        row["level"]
        for row in upcoming_rows
        if row["level"] < target_level + args.lookahead and row["is_fast"]
    ]
    if next_fast:
        print(f"\n⚡ Fast levels in range: {', '.join(f'L{level}' for level in next_fast)}")
        print("   Most kanji unlock on day 1 — theoretical minimum ~3.4 days per level.")

    if args.csv is not None:
        write_projection_csv(
            args.csv,
            current=current,
            upcoming_rows=upcoming_rows,
            durations=durations,
            l60_projections=l60_projections,
        )
        print(f"\nWrote CSV to {args.csv}")


if __name__ == "__main__":
    main()
