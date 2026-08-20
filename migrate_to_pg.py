#!/usr/bin/env python3
"""
Second Source - one-time copy of the SQLite store into Postgres.

Reads data/articles.db and writes it into the schema db.py points at. Safe to
re-run: articles key on their id, so a second pass inserts nothing.

    python migrate_to_pg.py --dry-run    # report what would move
    python migrate_to_pg.py
    python migrate_to_pg.py --reset-log  # also replace fetch_log (see below)

body_sha is computed here rather than copied. The column was added to ingest.py
after this store was built and its backfill never ran, so data/articles.db has
no such column - the guard that rejects a body already seen at another URL
would start with no history and accept the first duplicate of everything.
Hashing on the way across gives it that history.

fetch_log has no unique key, so re-running would duplicate its rows. It is
skipped when the target already has rows; --reset-log replaces them instead.
The scores, receipts, withdrawals and cluster tables are copied when the source
database has them - render.py created them lazily, so an older store may not.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

import db
from ingest import body_sha

SQLITE_PATH = Path(__file__).parent / "data" / "articles.db"

# Copied verbatim, keyed so a re-run is a no-op. Ordered so clusters exist
# before cluster_members references them, in case foreign keys are ever added.
SIMPLE_TABLES = [
    "scores",
    "receipts",
    "withdrawals",
    "clusters",
    "cluster_members",
]


def sqlite_columns(sconn, table: str) -> list[str]:
    return [r[1] for r in sconn.execute(f"PRAGMA table_info({table})")]


def sqlite_has_table(sconn, table: str) -> bool:
    return bool(
        sconn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def copy_articles(sconn, pconn, dry_run: bool) -> tuple[int, int]:
    cols = sqlite_columns(sconn, "articles")
    has_sha = "body_sha" in cols

    src = sconn.execute(
        "SELECT id, source_id, url, title, published_at, fetched_at,"
        " raw_text, word_count, fulltext_ok, scored"
        + (", body_sha" if has_sha else "")
        + " FROM articles"
    ).fetchall()

    rows = []
    hashed = 0
    for r in src:
        (aid, source_id, url, title, published_at, fetched_at,
         raw_text, word_count, fulltext_ok, scored) = r[:10]
        sha = r[10] if has_sha else None
        if sha is None and raw_text:
            # Mirrors the backfill init_db used to do on first run.
            sha = body_sha(raw_text)
            hashed += 1
        rows.append((
            aid, source_id, url, title,
            published_at or None, fetched_at or None,
            raw_text, word_count, fulltext_ok, sha, scored,
        ))

    if dry_run or not rows:
        return len(rows), hashed

    with pconn.cursor() as cur:
        cur.executemany(
            f"""INSERT INTO {db.TABLES}.articles
                (id, source_id, url, title, published_at, fetched_at,
                 raw_text, word_count, fulltext_ok, body_sha, scored)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING""",
            rows,
        )
    pconn.commit()
    return len(rows), hashed


def copy_fetch_log(sconn, pconn, dry_run: bool, reset: bool) -> tuple[int, str]:
    rows = sconn.execute(
        "SELECT run_at, source_id, entries, new_rows, error FROM fetch_log"
    ).fetchall()
    rows = [(a or None, b, c, d, e) for a, b, c, d, e in rows]

    existing = pconn.execute(f"SELECT COUNT(*) FROM {db.TABLES}.fetch_log").fetchone()[0]
    if existing and not reset:
        return 0, f"skipped, target already has {existing} rows (use --reset-log to replace)"
    if dry_run:
        return len(rows), "would replace" if existing else "would insert"

    with pconn.cursor() as cur:
        if existing:
            cur.execute(f"TRUNCATE {db.TABLES}.fetch_log")
        cur.executemany(
            f"INSERT INTO {db.TABLES}.fetch_log (run_at, source_id, entries, new_rows, error)"
            " VALUES (%s,%s,%s,%s,%s)",
            rows,
        )
    pconn.commit()
    return len(rows), "replaced" if existing else "inserted"


def copy_simple(sconn, pconn, table: str, dry_run: bool) -> tuple[int, str]:
    if not sqlite_has_table(sconn, table):
        return 0, "absent in source"
    cols = sqlite_columns(sconn, table)
    rows = sconn.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
    if not rows:
        return 0, "empty"
    if dry_run:
        return len(rows), "would insert"
    placeholders = ",".join(["%s"] * len(cols))
    with pconn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO {db.TABLES}.{table} ({', '.join(cols)})"
            f" VALUES ({placeholders}) ON CONFLICT DO NOTHING",
            [tuple(r) for r in rows],
        )
    pconn.commit()
    return len(rows), "inserted"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report, change nothing")
    ap.add_argument("--reset-log", action="store_true",
                    help="replace the target fetch_log instead of skipping it")
    args = ap.parse_args()

    if not SQLITE_PATH.exists():
        print(f"no SQLite store at {SQLITE_PATH} - nothing to migrate", file=sys.stderr)
        return 1

    sconn = sqlite3.connect(SQLITE_PATH)
    pconn = db.connect()
    db.apply_schema(pconn)

    n, hashed = copy_articles(sconn, pconn, args.dry_run)
    print(f"articles          {n:>6}  ({hashed} body_sha computed on the way)")

    n, note = copy_fetch_log(sconn, pconn, args.dry_run, args.reset_log)
    print(f"fetch_log         {n:>6}  {note}")

    for table in SIMPLE_TABLES:
        n, note = copy_simple(sconn, pconn, table, args.dry_run)
        print(f"{table:<18}{n:>6}  {note}")

    print()
    if args.dry_run:
        print("dry run - nothing written")
    else:
        total = pconn.execute(f"SELECT COUNT(*) FROM {db.TABLES}.articles").fetchone()[0]
        ft = pconn.execute(
            f"SELECT COUNT(*) FROM {db.TABLES}.articles WHERE fulltext_ok = 1"
        ).fetchone()[0]
        sha = pconn.execute(
            f"SELECT COUNT(*) FROM {db.TABLES}.articles WHERE body_sha IS NOT NULL"
        ).fetchone()[0]
        print(f"target: {total} articles, {ft} with full text, {sha} with body_sha")

    sconn.close()
    pconn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
