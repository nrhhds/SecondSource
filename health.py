#!/usr/bin/env python3
"""
Second Source - ingestion health check.

Answers the question the ingest log cannot: is a source quiet because the news
is quiet, or because it broke? A dead feed and a slow news day look identical
in the run output, so each source is judged against its own history rather than
one global threshold.

Four failure modes, all of which look like success in ingest.py's output:

    1. pipeline stalled  - scheduler died; every source goes quiet at once
    2. feed erroring     - last run returned an error
    3. feed stagnant     - feed responds, items never advance (see: CNS, stale
                           since Apr 2025 while still serving 200 OK)
    4. extraction broken - articles arrive but full text stops extracting,
                           e.g. after a site redesign

Exit code is 1 if anything is ALARM, so a scheduled run surfaces it via
LastTaskResult instead of needing to be read.

Usage:
    python health.py
    python health.py --quiet     # print only problems
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "articles.db"
SOURCES_PATH = ROOT / "sources.json"

# Runs are scheduled twice daily. Past this, the scheduler itself is suspect.
PIPELINE_WARN_HOURS = 15
PIPELINE_ALARM_HOURS = 26

# Used until a source has enough history to have earned its own baseline.
# 48h is deliberate: the shallowest feeds only expose ~2 days of items, so a
# longer gap than this means coverage is being lost permanently.
THIN_WARN_HOURS = 48
THIN_ALARM_HOURS = 96
MIN_ARTICLES_FOR_BASELINE = 12

# Extraction is expected only where sources.json says fulltext: true.
EXTRACT_WARN_RATE = 0.80
EXTRACT_ALARM_RATE = 0.50
MIN_ARTICLES_FOR_EXTRACT_CHECK = 5

OK, WARN, ALARM = "OK", "WARN", "ALARM"
RANK = {OK: 0, WARN: 1, ALARM: 2}


def parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def hours_since(dt: datetime | None, now: datetime) -> float | None:
    return None if dt is None else (now - dt).total_seconds() / 3600


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile. Small samples here; not worth a numpy dependency."""
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(p / 100 * len(ordered)) - 1))
    return ordered[idx]


def quiet_thresholds(pub_times: list[datetime]) -> tuple[float, float, str]:
    """Derive per-source quiet limits from its own publishing rhythm."""
    if len(pub_times) < MIN_ARTICLES_FOR_BASELINE:
        return THIN_WARN_HOURS, THIN_ALARM_HOURS, "thin history"
    ordered = sorted(pub_times)
    gaps = [
        (b - a).total_seconds() / 3600
        for a, b in zip(ordered, ordered[1:])
        if (b - a).total_seconds() > 0
    ]
    if not gaps:
        return THIN_WARN_HOURS, THIN_ALARM_HOURS, "no gaps"
    p95 = percentile(gaps, 95)
    # Floors keep a high-volume source from alarming over one quiet evening.
    return max(2 * p95, 12.0), max(3 * p95, 30.0), f"p95 gap {p95:.1f}h"


def check(conn, src: dict, now: datetime) -> dict:
    sid = src["id"]
    state, notes = OK, []

    last = conn.execute(
        "SELECT run_at, entries, error FROM fetch_log WHERE source_id = ? ORDER BY run_at DESC LIMIT 1",
        (sid,),
    ).fetchone()

    rows = conn.execute(
        "SELECT published_at FROM articles WHERE source_id = ? AND published_at IS NOT NULL",
        (sid,),
    ).fetchall()
    pub_times = [p for p in (parse(r[0]) for r in rows) if p]

    if not last:
        return {"id": sid, "state": WARN, "quiet": None, "notes": ["never attempted"]}

    run_at, entries, error = last

    if error:
        state = ALARM
        notes.append(f"last run errored: {str(error)[:60]}")
    elif entries == 0:
        state = ALARM
        notes.append("feed returned 0 entries")

    if not pub_times:
        notes.append("no dated articles stored")
        return {"id": sid, "state": max(state, WARN, key=RANK.get), "quiet": None, "notes": notes}

    quiet = hours_since(max(pub_times), now)
    warn_at, alarm_at, basis = quiet_thresholds(pub_times)
    if quiet >= alarm_at:
        state = max(state, ALARM, key=RANK.get)
        notes.append(f"newest item {quiet:.0f}h old (alarm >{alarm_at:.0f}h, {basis})")
    elif quiet >= warn_at:
        state = max(state, WARN, key=RANK.get)
        notes.append(f"newest item {quiet:.0f}h old (warn >{warn_at:.0f}h, {basis})")

    if src.get("fulltext"):
        recent = conn.execute(
            """SELECT COUNT(*), COALESCE(SUM(fulltext_ok), 0) FROM articles
               WHERE source_id = ? AND fetched_at >= datetime('now', '-14 days')""",
            (sid,),
        ).fetchone()
        n, ok = recent
        if n >= MIN_ARTICLES_FOR_EXTRACT_CHECK:
            rate = ok / n
            if rate < EXTRACT_ALARM_RATE:
                state = max(state, ALARM, key=RANK.get)
                notes.append(f"full text extracting on {rate:.0%} of last {n}")
            elif rate < EXTRACT_WARN_RATE:
                state = max(state, WARN, key=RANK.get)
                notes.append(f"full text extracting on {rate:.0%} of last {n}")

    return {"id": sid, "state": state, "quiet": quiet, "notes": notes}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true", help="print only WARN and ALARM rows")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"no database at {DB_PATH} - run ingest.py first", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    conn = sqlite3.connect(DB_PATH)
    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))["sources"]
    active = [s for s in sources if s.get("status") in {"ready", "needs_ua", "headlines_only"}]

    # Pipeline first: if the scheduler is dead, every source below is a symptom,
    # not a cause, and reporting six alarms would bury the one that matters.
    row = conn.execute("SELECT MAX(run_at) FROM fetch_log").fetchone()
    since = hours_since(parse(row[0]), now) if row and row[0] else None
    if since is None:
        print(f"{'PIPELINE':10} {ALARM:6} no runs recorded")
        return 1
    if since >= PIPELINE_ALARM_HOURS:
        print(f"{'PIPELINE':10} {ALARM:6} last run {since:.0f}h ago - scheduler is not firing")
        print("           source-level results below are unreliable until this is fixed")
        return 1
    pipeline_state = WARN if since >= PIPELINE_WARN_HOURS else OK
    if pipeline_state == WARN or not args.quiet:
        print(f"{'PIPELINE':10} {pipeline_state:6} last run {since:.1f}h ago")

    results = [check(conn, s, now) for s in active]
    worst = max((RANK[r["state"]] for r in results), default=0)

    if not args.quiet:
        print()
        # Known-inactive sources are reported, never alarmed. A permanently red
        # alarm is one you stop reading, and it would mask a real new failure.
        inactive = {}
        for s in sources:
            if s.get("status") not in {"ready", "needs_ua", "headlines_only"}:
                inactive.setdefault(s["status"], []).append(s["id"])
        for status, ids in sorted(inactive.items()):
            print(f"{'inactive':10} {'-':6} {status:16} {', '.join(ids)}")
        if inactive:
            print()
    for r in sorted(results, key=lambda r: -RANK[r["state"]]):
        if args.quiet and r["state"] == OK:
            continue
        quiet = f"{r['quiet']:.0f}h" if r["quiet"] is not None else "-"
        detail = "; ".join(r["notes"]) or "healthy"
        print(f"{r['id']:10} {r['state']:6} newest {quiet:>6}  {detail}")

    if args.quiet and worst == 0:
        print("all sources healthy")

    conn.close()
    return 1 if worst == RANK[ALARM] else 0


if __name__ == "__main__":
    sys.exit(main())
