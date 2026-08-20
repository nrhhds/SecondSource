#!/usr/bin/env python3
"""
Second Source - daily ingestion.

Pulls articles from configured feeds into a local SQLite store.
Stores raw text only. Never republishes. Scoring is a separate step.

Usage:
    python ingest.py                # pull all 'ready' + 'needs_ua' sources
    python ingest.py --all          # include stale/no-feed sources (will mostly no-op)
    python ingest.py --source flphoenix
    python ingest.py --source nsf --backfill 30   # walk paginated archive, manual only
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import feedparser
import requests
import trafilatura

ROOT = Path(__file__).parent
DB_PATH = ROOT / "data" / "articles.db"
SOURCES_PATH = ROOT / "sources.json"

# Identify honestly. We used to send a Chrome/127 string with an identifier
# appended, on the theory that FL outlets block bot agents. That was wrong and it
# cost us a source: floridapolitics.com runs an "Update Browser Required" rule
# that 403s any UA claiming an outdated browser, so the masquerade was the block.
# Tested 2026-08-18 - this honest UA gets 200 from floridapolitics, tampabay and
# newsserviceflorida alike. Do not reintroduce a browser string: it earns nothing,
# it goes stale, and it fails exactly where a named crawler is welcome.
UA = "SecondSourceBot/0.1 (+https://chooseyourbias.com/about)"
# Keep the Accept header: tampabay.com's Arc endpoint content-negotiates and
# returns an unparseable body without it.
HEADERS = {"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/html;q=0.9"}

# Politeness. Do not lower these.
DELAY_BETWEEN_FEEDS = 3.0
DELAY_BETWEEN_ARTICLES = 1.5
REQUEST_TIMEOUT = 20

ACTIVE_STATUSES = {"ready", "needs_ua", "headlines_only"}

# Minimum words for stored body text. Shorter than this is a paywall stub or a
# feed excerpt, not an article.
MIN_BODY_WORDS = 120


def delay_for(src: dict, floor: float) -> float:
    """Honour a source's robots.txt Crawl-delay. Tallahassee Reports asks for 10s
    and our floor is 3s; the stricter of the two wins."""
    return max(float(src.get("crawl_delay") or 0), floor)


def entry_author(entry) -> str:
    return (getattr(entry, "author", "") or "").strip()


def body_from_feed(entry) -> str | None:
    """Extract from content:encoded instead of refetching the article page.

    Used where a feed already carries the whole post. Same extractor as
    extract_fulltext() so stored text is cleaned identically across sources, and
    it saves the origin one request per article - which matters on a backfill.
    """
    if entry.get("content"):
        html = entry["content"][0].get("value", "")
    else:
        html = entry.get("summary", "")
    if not html:
        return None
    text = trafilatura.extract(html, include_comments=False, include_tables=False)
    return text if text and len(text.split()) >= MIN_BODY_WORDS else None


def body_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def accept_body(conn, source_id: str, url: str, text: str | None) -> tuple[str | None, str | None]:
    """Drop a body this source has already stored word for word at another URL.

    Two articles from one outlet never share a body verbatim. When they do, the
    extractor has locked onto page furniture rather than the story. tampabay.com
    is the live example: its article pages return an identical 75-word
    subscriber-comments block for every story, whatever the article. That one is
    caught by MIN_BODY_WORDS today, but the floor is the wrong guard - a wall
    longer than 120 words would be stored as real text and read as healthy,
    because nothing downstream compares one body against another.

    Rejecting the duplicate also demotes the first copy, which was stored before
    there was anything to compare it against. The effect is that the source's
    extraction rate collapses, which is the condition health.py already alarms
    on - so this failure surfaces through the existing alarm instead of needing
    a new one.

    A demoted row keeps its body_sha after losing its text. The hash is the only
    record that this fingerprint is known bad, and dropping it makes the guard
    flip-flop: the next URL would find nothing to match, be accepted, and every
    other article would land in the store as boilerplate.

    Scoped to one source_id on purpose. Two outlets legitimately carry the same
    body - that is wire copy, which is the point of the NSF anchor - and only a
    repeat within a single outlet means the extractor is reading furniture.
    """
    if not text:
        return None, None
    sha = body_sha(text)
    dupes = conn.execute(
        "SELECT id, fulltext_ok FROM articles WHERE source_id = ? AND body_sha = ? AND url <> ?",
        (source_id, sha, url),
    ).fetchall()
    if not dupes:
        return text, sha
    demote = [(d[0],) for d in dupes if d[1]]
    if demote:
        conn.executemany(
            "UPDATE articles SET raw_text = NULL, word_count = 0, fulltext_ok = 0 WHERE id = ?",
            demote,
        )
    print(
        f"  {source_id}: boilerplate body ({len(text.split())} words) seen at "
        f"{len(dupes) + 1} URLs - rejected"
        + (f", {len(demote)} earlier row(s) demoted" if demote else ""),
        file=sys.stderr,
    )
    return None, None


def init_db(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS articles (
            id              TEXT PRIMARY KEY,
            source_id       TEXT NOT NULL,
            url             TEXT NOT NULL UNIQUE,
            title           TEXT,
            published_at    TEXT,
            fetched_at      TEXT NOT NULL,
            raw_text        TEXT,
            word_count      INTEGER,
            fulltext_ok     INTEGER NOT NULL DEFAULT 0,
            body_sha        TEXT,
            scored          INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_source  ON articles(source_id);
        CREATE INDEX IF NOT EXISTS idx_pubdate ON articles(published_at);
        CREATE INDEX IF NOT EXISTS idx_scored  ON articles(scored);

        CREATE TABLE IF NOT EXISTS fetch_log (
            run_at      TEXT NOT NULL,
            source_id   TEXT NOT NULL,
            entries     INTEGER,
            new_rows    INTEGER,
            error       TEXT
        );
    """)

    # body_sha arrived after the first stores. Add it and backfill, so the
    # duplicate-body guard can compare against history on its first run.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(articles)")}
    if "body_sha" not in cols:
        conn.execute("ALTER TABLE articles ADD COLUMN body_sha TEXT")
        conn.executemany(
            "UPDATE articles SET body_sha = ? WHERE id = ?",
            [
                (body_sha(t), i)
                for i, t in conn.execute(
                    "SELECT id, raw_text FROM articles WHERE raw_text IS NOT NULL"
                ).fetchall()
            ],
        )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_body_sha ON articles(source_id, body_sha)")
    conn.commit()


def article_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def extract_fulltext(url: str) -> str | None:
    """Fetch and extract article body. Returns None on failure or paywall stub."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None
        text = trafilatura.extract(resp.text, include_comments=False, include_tables=False)
        if text and len(text.split()) >= MIN_BODY_WORDS:
            return text
        return None
    except Exception:
        return None


def parse_published(entry) -> str | None:
    for key in ("published_parsed", "updated_parsed"):
        val = getattr(entry, key, None)
        if val:
            return datetime(*val[:6], tzinfo=timezone.utc).isoformat()
    return None


def paged(base: str, page: int) -> str:
    return base if page == 1 else f"{base}{'&' if '?' in base else '?'}paged={page}"


def ingest_paged(conn, src: dict, max_pages: int) -> tuple[int, int, str | None]:
    """Walk a feed's pages until one adds nothing new.

    floridapolitics.com publishes ~56 items/day into a 10-item feed, so its
    window is about four hours. A twice-daily poll of page 1 alone would drop
    most of its output. Stopping at the first page with no new rows means a quiet
    day still costs one request.
    """
    feed_delay = delay_for(src, DELAY_BETWEEN_FEEDS)
    seen_entries = total_new = 0
    for page in range(1, max_pages + 1):
        entries, new_rows, err = ingest_source(conn, src, feed_url=paged(src["feed"], page))
        if err:
            return seen_entries + entries, total_new, err if page == 1 else None
        seen_entries += entries
        total_new += new_rows
        if entries == 0 or new_rows == 0:
            break
        if page < max_pages:
            time.sleep(feed_delay)
    return seen_entries, total_new, None


def ingest_source(conn, src: dict, feed_url: str | None = None) -> tuple[int, int, str | None]:
    feed_url = feed_url or src.get("feed")
    if not feed_url:
        return 0, 0, "no feed configured"

    try:
        resp = requests.get(feed_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
    except Exception as e:
        return 0, 0, f"{type(e).__name__}: {e}"

    entries = parsed.entries or []
    new_rows = 0
    want_fulltext = bool(src.get("fulltext"))
    from_feed = src.get("fulltext_from") == "feed"
    article_delay = delay_for(src, DELAY_BETWEEN_ARTICLES)

    # A republisher's main feed also carries the wire copy it reprints, at the
    # same URL the wire's own source pulls. Excluding by author makes ownership
    # of that row deterministic instead of a race between the two feeds.
    excluded = [a.strip().lower() for a in (src.get("exclude_authors") or [])]

    for entry in entries:
        url = getattr(entry, "link", None)
        if not url:
            continue
        author = entry_author(entry).lower()
        if any(x in author for x in excluded):
            continue
        aid = article_id(url)

        if conn.execute("SELECT 1 FROM articles WHERE id = ?", (aid,)).fetchone():
            continue

        text = None
        if want_fulltext:
            if from_feed:
                text = body_from_feed(entry)
            else:
                text = extract_fulltext(url)
                time.sleep(article_delay)
        text, sha = accept_body(conn, src["id"], url, text)

        conn.execute(
            """INSERT OR IGNORE INTO articles
               (id, source_id, url, title, published_at, fetched_at,
                raw_text, word_count, fulltext_ok, body_sha, scored)
               VALUES (?,?,?,?,?,?,?,?,?,?,0)""",
            (
                aid,
                src["id"],
                url,
                getattr(entry, "title", None),
                parse_published(entry),
                datetime.now(timezone.utc).isoformat(),
                text,
                len(text.split()) if text else 0,
                1 if text else 0,
                sha,
            ),
        )
        new_rows += 1

    conn.commit()
    return len(entries), new_rows, None


def repair_source(conn, src: dict) -> tuple[int, int]:
    """Re-attempt body text for rows stored before a source gained fulltext.

    Flipping a source from headline-only to full text does not retroactively fill
    rows already in the store - dedupe skips them, so they would stay empty for
    good. Extraction goes through the article page here, not the feed, because
    older items have long since fallen out of the feed window.
    """
    rows = conn.execute(
        "SELECT id, url FROM articles WHERE source_id = ? AND fulltext_ok = 0",
        (src["id"],),
    ).fetchall()
    fixed = 0

    if src.get("fulltext_from") == "feed":
        # Paywalled sources serve a subscriber stub on the article page, so the
        # feed is the only route to text - which also caps what is recoverable at
        # whatever still sits inside the feed window.
        want = {aid for aid, _ in rows}
        for page in range(1, int(src.get("catchup_pages") or 1) + 1):
            try:
                resp = requests.get(paged(src["feed"], page), headers=HEADERS, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                entries = feedparser.parse(resp.content).entries or []
            except Exception as e:
                print(f"  page {page}: {type(e).__name__}: {e}", file=sys.stderr)
                break
            if not entries:
                break
            for entry in entries:
                url = getattr(entry, "link", None)
                if not url or article_id(url) not in want:
                    continue
                text, sha = accept_body(conn, src["id"], url, body_from_feed(entry))
                if text:
                    conn.execute(
                        "UPDATE articles SET raw_text = ?, word_count = ?, fulltext_ok = 1,"
                        " body_sha = ? WHERE id = ?",
                        (text, len(text.split()), sha, article_id(url)),
                    )
                    fixed += 1
            time.sleep(delay_for(src, DELAY_BETWEEN_FEEDS))
    else:
        delay = delay_for(src, DELAY_BETWEEN_ARTICLES)
        for aid, url in rows:
            text, sha = accept_body(conn, src["id"], url, extract_fulltext(url))
            if text:
                conn.execute(
                    "UPDATE articles SET raw_text = ?, word_count = ?, fulltext_ok = 1,"
                    " body_sha = ? WHERE id = ?",
                    (text, len(text.split()), sha, aid),
                )
                fixed += 1
            time.sleep(delay)

    conn.commit()
    return len(rows), fixed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="include inactive sources")
    ap.add_argument("--source", help="single source id")
    ap.add_argument("--repair", action="store_true",
                    help="re-attempt full text for stored rows that have none. Requires --source.")
    ap.add_argument("--backfill", type=int, metavar="PAGES",
                    help="walk pages 1..N of a paginated feed. Requires --source. "
                         "Run by hand: a deep backfill outruns the scheduled task's "
                         "one-hour execution limit.")
    args = ap.parse_args()

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    sources = json.loads(SOURCES_PATH.read_text())["sources"]
    if args.source:
        sources = [s for s in sources if s["id"] == args.source]
    elif not args.all:
        sources = [s for s in sources if s.get("status") in ACTIVE_STATUSES]

    if not sources:
        print("no matching sources", file=sys.stderr)
        return 1

    run_at = datetime.now(timezone.utc).isoformat()

    if args.repair:
        if len(sources) != 1:
            print("--repair needs exactly one --source", file=sys.stderr)
            return 1
        attempted, fixed = repair_source(conn, sources[0])
        print(f"{sources[0]['id']}: {fixed} of {attempted} textless rows repaired")
        conn.close()
        return 0

    if args.backfill:
        if len(sources) != 1:
            print("--backfill needs exactly one --source", file=sys.stderr)
            return 1
        src = sources[0]
        base = src.get("feed")
        if not base:
            print(f"{src['id']} has no feed to page through", file=sys.stderr)
            return 1
        feed_delay = delay_for(src, DELAY_BETWEEN_FEEDS)
        for page in range(1, args.backfill + 1):
            url = base if page == 1 else f"{base}{'&' if '?' in base else '?'}paged={page}"
            entries, new_rows, err = ingest_source(conn, src, feed_url=url)
            conn.execute(
                "INSERT INTO fetch_log (run_at, source_id, entries, new_rows, error) VALUES (?,?,?,?,?)",
                (run_at, src["id"], entries, new_rows, err),
            )
            conn.commit()
            print(f"  page {page:>3}  {'ERROR ' + str(err) if err else f'{entries} entries, {new_rows} new'}")
            # An empty page is the end of the archive, not a failure.
            if err or entries == 0:
                break
            time.sleep(feed_delay)
        total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        print()
        print(f"store: {total} articles")
        conn.close()
        return 0

    for src in sources:
        pages = int(src.get("catchup_pages") or 1)
        if pages > 1 and src.get("feed"):
            entries, new_rows, err = ingest_paged(conn, src, pages)
        else:
            entries, new_rows, err = ingest_source(conn, src)
        conn.execute(
            "INSERT INTO fetch_log (run_at, source_id, entries, new_rows, error) VALUES (?,?,?,?,?)",
            (run_at, src["id"], entries, new_rows, err),
        )
        conn.commit()
        status = f"ERROR {err}" if err else f"{entries} entries, {new_rows} new"
        print(f"{src['id']:18} {status}")
        time.sleep(delay_for(src, DELAY_BETWEEN_FEEDS))

    total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    ft = conn.execute("SELECT COUNT(*) FROM articles WHERE fulltext_ok = 1").fetchone()[0]
    print(f"\nstore: {total} articles, {ft} with full text")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
