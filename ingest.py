#!/usr/bin/env python3
"""
Second Source - daily ingestion.

Pulls articles from configured feeds into a local SQLite store.
Stores raw text only. Never republishes. Scoring is a separate step.

Usage:
    python ingest.py                # pull all 'ready' + 'needs_ua' sources
    python ingest.py --all          # include stale/no-feed sources (will mostly no-op)
    python ingest.py --source flphoenix
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

# A real user-agent. Several FL outlets 402/429 default bot agents.
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/127.0 Safari/537.36 "
    "SecondSource/0.1 (+https://chooseyourbias.com/about)"
)
HEADERS = {"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/html;q=0.9"}

# Politeness. Do not lower these.
DELAY_BETWEEN_FEEDS = 3.0
DELAY_BETWEEN_ARTICLES = 1.5
REQUEST_TIMEOUT = 20

ACTIVE_STATUSES = {"ready", "needs_ua", "headlines_only"}


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
        # Anything this short is a paywall stub, not an article.
        if text and len(text.split()) >= 120:
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


def ingest_source(conn, src: dict) -> tuple[int, int, str | None]:
    feed_url = src.get("feed")
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

    for entry in entries:
        url = getattr(entry, "link", None)
        if not url:
            continue
        aid = article_id(url)

        if conn.execute("SELECT 1 FROM articles WHERE id = ?", (aid,)).fetchone():
            continue

        text = None
        if want_fulltext:
            text = extract_fulltext(url)
            time.sleep(DELAY_BETWEEN_ARTICLES)

        conn.execute(
            """INSERT OR IGNORE INTO articles
               (id, source_id, url, title, published_at, fetched_at,
                raw_text, word_count, fulltext_ok, scored)
               VALUES (?,?,?,?,?,?,?,?,?,0)""",
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
            ),
        )
        new_rows += 1

    conn.commit()
    return len(entries), new_rows, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="include inactive sources")
    ap.add_argument("--source", help="single source id")
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
    for src in sources:
        entries, new_rows, err = ingest_source(conn, src)
        conn.execute(
            "INSERT INTO fetch_log (run_at, source_id, entries, new_rows, error) VALUES (?,?,?,?,?)",
            (run_at, src["id"], entries, new_rows, err),
        )
        conn.commit()
        status = f"ERROR {err}" if err else f"{entries} entries, {new_rows} new"
        print(f"{src['id']:18} {status}")
        time.sleep(DELAY_BETWEEN_FEEDS)

    total = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    ft = conn.execute("SELECT COUNT(*) FROM articles WHERE fulltext_ok = 1").fetchone()[0]
    print(f"\nstore: {total} articles, {ft} with full text")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
