#!/usr/bin/env python3
"""
Second Source - IP reputation probe.

Answers one question: does the honest UA still get 200 from this machine's
network? Cloudflare weighs UA *and* client IP, so SecondSourceBot/0.1 being
welcome from a residential connection does not carry over to a datacenter
range for free. floridapolitics.com is the source at risk: it 403'd until
2026-08-18 and it is the 'center' prior for the lean-separation test.

Read-only. Touches no database, writes no files.

Run it from the laptop and from the candidate runner within a few minutes of
each other. Feed contents drift -- flpolitics went 30 -> 60 entries in the
nine hours between two runs on 2026-08-18 -- so an old ingest log is not a
valid control.

Probes the feed AND articles per source. A feed returning 200 while the
article HTML 403s is the false pass: every active source is fulltext, so
losing article fetch loses the source.

Samples SAMPLE_ARTICLES articles rather than one. The first item in a feed is
often a brief, a gallery or a live blog that is legitimately under the word
floor -- tampabay.com's Arc feed does this -- and a single short sample is
indistinguishable from a Cloudflare interstitial, which also extracts to
almost nothing. Several samples separate them: a real block is short on every
one, a short brief is short on one.

Usage:  python probe.py           # table to stdout, JSON to stderr
        python probe.py --json    # JSON only
"""

import argparse
import json
import platform
import statistics
import sys
import time
from datetime import datetime, timezone

import feedparser
import requests
import trafilatura

from ingest import (
    ACTIVE_STATUSES,
    DELAY_BETWEEN_ARTICLES,
    DELAY_BETWEEN_FEEDS,
    HEADERS,
    MIN_BODY_WORDS,
    REQUEST_TIMEOUT,
    SOURCES_PATH,
    UA,
    delay_for,
)

SAMPLE_ARTICLES = 3


def probe_source(src: dict) -> dict:
    """Fetch a source's feed, then several articles from it. Never raises."""
    row = {
        "id": src["id"],
        "feed_status": None,
        "entries": 0,
        "article_statuses": [],
        "article_words": [],
        "verdict": "ERROR",
        "error": None,
    }

    try:
        r = requests.get(src["feed"], headers=HEADERS, timeout=REQUEST_TIMEOUT)
        row["feed_status"] = r.status_code
        if r.status_code != 200:
            row["verdict"] = "FEED_BLOCKED"
            return row
        # bozo alone is not failure - feedparser flags minor issues on feeds that
        # still yield entries. Entry count is the signal that matters.
        parsed = feedparser.parse(r.content)
        row["entries"] = len(parsed.entries)
        if not parsed.entries:
            row["verdict"] = "FEED_UNPARSEABLE"
            return row
    except Exception as e:
        row["error"] = f"{type(e).__name__}: {e}"
        return row

    links = [e["link"] for e in parsed.entries if e.get("link")][:SAMPLE_ARTICLES]
    if not links:
        row["verdict"] = "NO_LINKS"
        return row

    delay = delay_for(src, DELAY_BETWEEN_ARTICLES)
    for link in links:
        time.sleep(delay)
        try:
            r = requests.get(link, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            row["article_statuses"].append(r.status_code)
            if r.status_code != 200:
                row["article_words"].append(0)
                continue
            text = trafilatura.extract(r.text, include_comments=False, include_tables=False)
            row["article_words"].append(len(text.split()) if text else 0)
        except Exception as e:
            row["article_statuses"].append(None)
            row["article_words"].append(0)
            row["error"] = f"{type(e).__name__}: {e}"

    ok_status = [s for s in row["article_statuses"] if s == 200]
    if not ok_status:
        row["verdict"] = "ARTICLE_BLOCKED"
    elif any(w >= MIN_BODY_WORDS for w in row["article_words"]):
        # Same floor ingest.py uses. One sample clearing it proves this network
        # is served real article HTML; ingest.py drops the short ones anyway.
        row["verdict"] = "OK"
    else:
        row["verdict"] = "THIN"

    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="JSON only, no table")
    args = ap.parse_args()

    sources = [
        s for s in json.loads(SOURCES_PATH.read_text())["sources"]
        if s.get("status") in ACTIVE_STATUSES and s.get("feed")
    ]

    rows = []
    for i, src in enumerate(sources):
        if i:
            time.sleep(delay_for(src, DELAY_BETWEEN_FEEDS))
        rows.append(probe_source(src))

    report = {
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "host": platform.node(),
        "platform": platform.platform(),
        "ua": UA,
        "sample_articles": SAMPLE_ARTICLES,
        "results": rows,
    }

    if args.json:
        print(json.dumps(report, indent=2))
        return 1 if any(r["verdict"] != "OK" for r in rows) else 0

    print(f"probe {report['probed_at']}  host={report['host']}")
    print(f"{'source':20} {'feed':>5} {'entries':>7}  {'articles':<14} {'words':<18} verdict")
    for r in rows:
        st = ",".join(str(s or "-") for s in r["article_statuses"]) or "-"
        wd = ",".join(str(w) for w in r["article_words"]) or "-"
        best = max(r["article_words"], default=0)
        print(
            f"{r['id']:20} {str(r['feed_status'] or '-'):>5} {r['entries']:>7}  "
            f"{st:<14} {wd:<18} {r['verdict']} (best {best})"
            + (f"  {r['error']}" if r["error"] else "")
        )

    bad = [r["id"] for r in rows if r["verdict"] != "OK"]
    print()
    print(f"{len(rows) - len(bad)}/{len(rows)} OK" + (f"  --  FAILED: {', '.join(bad)}" if bad else ""))
    print(json.dumps(report), file=sys.stderr)

    # Non-zero so a blocked source turns the workflow red instead of passing quietly.
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
