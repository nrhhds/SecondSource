#!/usr/bin/env python3
"""
Second Source - calibration anchor validation.

Calibration test #1 is "News Service of Florida scores near zero". NSF is a
subscription wire with no public full text, so 'nsf' does not come from NSF: it
comes from Tallahassee Reports' author archive, which republishes the wire. If
that republisher retitles or edits what it carries, the anchor is scoring the
republisher's edit and the near-zero result proves nothing about the wire.

'nsf_wire' exists to answer that. Those 15 rows are NSF's own headlines, pulled
direct from the wire's RSS before 'nsf' was repointed at the republisher, and
kept at status: archive for exactly this diff.

---------------------------------------------------------------------------
What this can and cannot establish
---------------------------------------------------------------------------
nsf_wire is HEADLINE-ONLY. The wire's public feed never carried bodies, so
there is no body to diff against. This check can therefore detect:

    retitling      - the republisher rewrote the headline
    non-carriage   - the republisher did not run the item at all

and it cannot detect:

    body trimming  - the republisher cut or condensed the text it did run

Matching headlines are consistent with a faithful republisher; they are not
proof of one. An outlet that reprints headlines verbatim while trimming bodies
passes this check completely. Body fidelity needs a second republisher to diff
against - tampabay.com carries the wire, but harvesting it needs the per-
republisher nsf@<outlet> source_id scheme, which is not built. Until then,
treat a pass here as "no retitling found", not "the anchor is validated".

---------------------------------------------------------------------------
Why matching is fuzzy and date-windowed
---------------------------------------------------------------------------
The republisher's published_at is its own post time, which runs up to a day
behind the wire (observed: wire 2026-08-17 items appear under 2026-08-18). So
candidates are drawn from a window around the wire date rather than the same
day.

Titles are compared after normalisation, because a republisher converting
curly quotes to straight ones is not retitling. Anything short of an exact
normalised match is reported with its similarity score and left for a human to
judge rather than being auto-classified by a threshold - with 15 wire items
that is entirely feasible, and a threshold picked to make this sample come out
clean would not survive the next sample.

Usage:
    python validate_anchor.py
    python validate_anchor.py --window 4      # +/- days to search for a match
    python validate_anchor.py --verbose       # show near-miss candidates
"""

import argparse
import difflib
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone

import db

# A wire item younger than this cannot be called "not carried": the republisher
# runs about a day behind the wire (Monday's copy appears Tuesday), and our own
# pull of it lags by up to half a day again. Counting the last two days as
# non-carriage understates the carriage rate on every run, and understates it
# more the fresher the wire sample is - which, now that nsf_wire only grows
# forward, is a bias that would grow rather than wash out.
MATURITY_DAYS = 3

# Wire items that are standing columns or daily agendas rather than reported
# stories. A republisher skipping these is making an editorial choice about
# formats, not about stories, and it does not bear on retitling - but it does
# bear on what the anchor is made of, so they are counted separately rather
# than dropped silently.
ROUTINE_PATTERNS = [
    r"^on tap in the capitol",
    r"^backroom briefing",
    r"^weekly roundup",
    r"^advances:",
]

# Below this, two headlines are unrelated and printing the pair is noise. This
# only controls what gets SHOWN as a near miss; nothing is classified as a
# retitle automatically.
NEAR_MISS_FLOOR = 0.55


def normalise(title: str) -> str:
    """Fold the differences that are typography rather than editing.

    Curly quotes, en/em dashes and non-breaking spaces all vary between a wire
    feed and a WordPress republisher without a single word having changed.
    Case and trailing punctuation likewise.
    """
    t = unicodedata.normalize("NFKC", title)
    for ch in "‘’‛′":
        t = t.replace(ch, "'")
    for ch in "“”″":
        t = t.replace(ch, '"')
    for ch in "‐‑‒–—―":
        t = t.replace(ch, "-")
    t = t.replace(" ", " ")
    t = t.lower()
    t = re.sub(r"\s+", " ", t).strip()
    return t.strip(" .,;:-")


def is_routine(title: str) -> bool:
    low = normalise(title)
    return any(re.search(p, low) for p in ROUTINE_PATTERNS)


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def fetch(conn):
    """Wire headlines, and every republished item that could match one.

    The republished side is pulled across the wire's date span widened by the
    window, so a single query serves every comparison.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT published_at, title, url FROM {db.TABLES}.articles "
            "WHERE source_id = 'nsf_wire' AND title IS NOT NULL "
            "ORDER BY published_at"
        )
        wire = cur.fetchall()

        cur.execute(
            f"SELECT published_at, title, word_count FROM {db.TABLES}.articles "
            "WHERE source_id = 'nsf' AND title IS NOT NULL "
            "ORDER BY published_at"
        )
        repub = cur.fetchall()
    return wire, repub


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=4,
                    help="days either side of the wire date to search")
    ap.add_argument("--verbose", action="store_true",
                    help="show near-miss candidates for unmatched items")
    ap.add_argument("--maturity", type=int, default=MATURITY_DAYS,
                    help="wire items younger than this are too recent to judge")
    args = ap.parse_args()

    with db.connect() as conn:
        wire, repub = fetch(conn)

    if not wire:
        print("no nsf_wire rows - nothing to validate against")
        return 1

    wire_days = sorted({w[0].date() for w in wire})
    print("CALIBRATION ANCHOR - headline fidelity check")
    print(f"  wire headlines : {len(wire)} over {len(wire_days)} days "
          f"({wire_days[0]} to {wire_days[-1]})")
    print(f"  republished    : {len(repub)} items in store")
    print(f"  match window   : +/- {args.window} days")
    print()

    exact, retitled, absent_routine, absent_story, too_recent = [], [], [], [], []
    mature_before = datetime.now(timezone.utc) - timedelta(days=args.maturity)

    for pub, title, _url in wire:
        lo = pub - timedelta(days=args.window)
        hi = pub + timedelta(days=args.window)
        candidates = [(p, t, w) for p, t, w in repub if lo <= p <= hi]

        norm = normalise(title)
        scored = sorted(
            ((similarity(norm, normalise(t)), p, t, w) for p, t, w in candidates),
            reverse=True, key=lambda r: r[0],
        )
        best = scored[0] if scored else None

        if best and best[0] == 1.0:
            exact.append((pub, title, best))
        elif best and best[0] >= NEAR_MISS_FLOOR:
            retitled.append((pub, title, best))
        elif is_routine(title):
            absent_routine.append((pub, title, best))
        elif pub > mature_before:
            too_recent.append((pub, title, best))
        else:
            absent_story.append((pub, title, best))

    def show(label, rows, with_best):
        if not rows:
            return
        print(f"{label} ({len(rows)})")
        for pub, title, best in rows:
            print(f"  {pub.date()}  {title}")
            if with_best and best:
                print(f"            -> {best[0]:.2f}  {best[2]}  ({best[3]}w)")
        print()

    show("VERBATIM - republished headline identical", exact, False)
    show("REVIEW - close but not identical, judge by eye", retitled, True)
    show("NOT CARRIED - standing column or daily agenda", absent_routine, False)
    show("NOT CARRIED - reported story", absent_story, args.verbose)
    show(f"TOO RECENT to judge - under {args.maturity}d, republisher runs behind",
         too_recent, False)

    carried = len(exact) + len(retitled)
    stories = len(wire) - len(absent_routine) - len(too_recent)

    print("-" * 70)
    print(f"  judged                 : {stories} reported wire stories "
          f"({len(absent_routine)} standing columns and {len(too_recent)} too "
          f"recent excluded from {len(wire)})")
    print(f"  carried by republisher : {carried}/{stories}"
          + (f"  = {100 * carried // stories}%" if stories else ""))
    if carried:
        print(f"  headline fidelity      : {len(exact)}/{carried} verbatim, "
              f"{len(retitled)} to review")
    print()

    if retitled:
        print("VERDICT: possible retitling - review the pairs above.")
        print("  If any is the same story under a rewritten headline, the anchor")
        print("  is scoring the republisher's edit and calibration test #1 is")
        print("  invalid as written.")
        return 1

    if not carried:
        print("VERDICT: no overlap. The wire sample and the republished sample")
        print("  do not cover the same stories, so this proves nothing either")
        print("  way. Pull a fresh nsf_wire sample over a current window.")
        return 1

    print("VERDICT: no retitling found in the overlap.")
    print(f"  Basis: {carried} stories over {len(wire_days)} days. That is a thin")
    print("  sample, and headline-only - it does not rule out body trimming.")
    print("  See the module docstring before quoting this as validation.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
