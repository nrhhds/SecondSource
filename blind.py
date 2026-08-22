#!/usr/bin/env python3
"""
Second Source - blinding.

Design rule 1: the scorer never sees who wrote the article. This is the
project's main methodological differentiator, so it is the one preprocessing
step that has to be measured rather than assumed - a blinder that quietly
misses is worse than no blinder, because the claim is what the work rests on.

Measured on the corpus before this existed, an outlet names itself in its own
body text at these rates:

    flpolitics  100%      nsf         91%      flphoenix   52%
    tbtimes      40%      tallahassee 11%      capitolist   0%
    flvoice       0%      nsf@wuft     0%

Florida Politics appends a standing footer to every article and the wire
prepends its own byline, so for two of the three biggest sources the outlet
was identifiable in essentially every row. `--audit` reports what is left.

---------------------------------------------------------------------------
Redact in place; delete only what is not body
---------------------------------------------------------------------------
In-body mentions are REPLACED with a token, never deleted. Rubric signal 5
counts the paragraph position of the opposing view, and signal 2 measures
quote length; deleting a phrase mid-sentence shifts both and would show up as
a bias score rather than as a preprocessing artefact. Leading bylines and
trailing boilerplate ARE deleted, because they are not article body - they are
furniture wrapped around it.

---------------------------------------------------------------------------
Identifiers come from sources.json
---------------------------------------------------------------------------
Outlet names and domains are derived from the source registry, plus any
`blind_aliases` a source declares, so adding a source extends the blinder
automatically. A hardcoded list here would rot silently the first time a
source is added - and silently is the one way this must not fail.

---------------------------------------------------------------------------
Known gap: reporter names in body text
---------------------------------------------------------------------------
Reporter names are stripped from the BYLINE only, not redacted throughout.
Florida Politics' "Sunburn" roundup cites other outlets' reporters by name
mid-body - "via Jesse Scheckner of [OUTLET]" - and Scheckner is one of its
own. So authorship can still be inferred from an aggregation newsletter even
with the outlet redacted.

Left alone deliberately. Redacting every harvested name everywhere would
collide with news subjects who share a name with a reporter, and quietly
rewriting a politician's name inside a quote is a worse failure than this
one. Revisit if aggregation formats enter the scoreable set; they are a small
share of the corpus and a poor fit for article-level scoring anyway.

Usage:
    python blind.py --audit                 # leak rate over the whole corpus
    python blind.py --audit --verbose       # list the rows that still leak
    python blind.py --sample nsf            # before/after on a few rows
"""

import argparse
import json
import re
import sys
from pathlib import Path

import db

ROOT = Path(__file__).parent
SOURCES_PATH = ROOT / "sources.json"

OUTLET_TOKEN = "[OUTLET]"
URL_TOKEN = "[URL]"

# How far into the body a leading byline may run before we stop believing it is
# one. The longest real byline seen is "By Jim Turner and Ana Goni-Lessan, The
# News Service of Florida" at 62 characters.
BYLINE_WINDOW = 140

# Standing boilerplate, anchored to the end of the text. Each is a block an
# outlet appends to every article rather than anything a reporter wrote.
#
# The Florida Politics footer is on 218 of 218 rows; the States Newsroom block
# is on Florida Phoenix syndications. Both name their outlet, so leaving them
# in would defeat the redaction downstream of it.
FOOTER_PATTERNS = [
    # "The post <headline> appeared first on Florida Politics - ..."
    (r"\s*The post\b.{0,300}?appeared first on\b.*$", "flpolitics post-footer"),
    # "This story was originally produced by X, which is part of States
    # Newsroom, ... includes Florida Phoenix, and is supported by grants ..."
    (r"\s*This story was originally produced by\b.*$", "states newsroom credit"),
    (r"\s*[A-Z][\w' ]{0,40} is part of States Newsroom\b.*$", "states newsroom credit"),
    (r"\s*This content provided in partnership with\b.*$", "partner credit"),
]

URL_RE = re.compile(r"https?://\S+|\bwww\.[a-z0-9.-]+\.[a-z]{2,}\S*", re.I)


def load_identifiers() -> tuple[list[str], list[str]]:
    """Outlet strings and bare domains to redact, newest-longest first.

    Longest first matters: "News Service of Florida" must be consumed before a
    shorter alias inside it, or the leftover fragment survives the pass that
    was supposed to remove it.
    """
    sources = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))["sources"]
    names, domains = set(), set()
    for s in sources:
        # Drop a parenthetical qualifier: "News Service of Florida (via WUFT)"
        # is a registry label, not a string that appears in anyone's prose.
        name = re.sub(r"\s*\(.*?\)\s*", " ", s.get("name") or "").strip()
        if name:
            names.add(name)
        for alias in s.get("blind_aliases") or []:
            names.add(alias)
        for url in [s.get("feed")] + list(s.get("feeds") or []):
            if not url:
                continue
            m = re.search(r"https?://(?:www\.)?([a-z0-9.-]+)", url, re.I)
            if m:
                domains.add(m.group(1).lower())
    return (sorted(names, key=len, reverse=True),
            sorted(domains, key=len, reverse=True))


ANCHORED_BYLINE_RE = re.compile(
    r"^By ([A-Z][\w.'’-]+(?: [A-Z][\w.'’-]+){0,2}"
    r"(?: and [A-Z][\w.'’-]+(?: [A-Z][\w.'’-]+){0,2})?), "
)


def harvest_reporters(rows) -> list[str]:
    """Learn reporter names from bylines that an outlet name already anchors.

    Some bylines carry no outlet - "By Gray Rohrer The title and summary of a
    proposal ..." - and there is no reliable boundary between a surname and the
    first words of the article. Guessing at one risks eating body text, and "By
    Monday the Legislature ..." is a sentence a lede could plausibly open with.

    But the same reporters file anchored bylines elsewhere in the corpus: Gray
    Rohrer appears in 15 of them. Harvesting the names from those makes the
    unanchored cut exact instead of speculative, and the list maintains itself
    as the corpus grows rather than being a constant someone has to remember to
    update.
    """
    names = set()
    for text in rows:
        m = ANCHORED_BYLINE_RE.match(text.lstrip())
        if m:
            for part in m.group(1).split(" and "):
                if part.strip():
                    names.add(part.strip())
    return sorted(names, key=len, reverse=True)


def strip_byline(text: str, names: list[str], reporters: list[str] = ()) -> tuple[str, str | None]:
    """Remove a leading byline, but only when we can prove where it ends.

    The reliable anchor is the outlet name the byline ends with: NSF files as
    "By <reporter>, The News Service of Florida <body>", so cutting through the
    end of a known outlet name inside the opening window is exact.

    Without that anchor - "By Gray Rohrer The title and summary of a proposal
    ..." - there is no non-guessy boundary between the reporter's surname and
    the first words of the article, and a wrong cut silently eats body text.
    Those are left intact and reported by --audit as unstripped, because a
    visible miss can be fixed and an invisible one cannot.
    """
    if not text.lstrip().startswith("By "):
        return text, None
    head = text[:BYLINE_WINDOW]
    best_end, best_name = None, None
    for name in names:
        i = head.lower().find(name.lower())
        if i != -1 and (best_end is None or i + len(name) > best_end):
            best_end, best_name = i + len(name), name
    if best_end is None:
        # No outlet anchored the cut. Fall back to a harvested reporter name,
        # which is exact where a name-shaped guess would not be.
        for rep in reporters:
            if head.lower().startswith(f"by {rep.lower()}"):
                return text[len("By ") + len(rep):].lstrip(" ,.-—"), rep
        return text, None
    return text[best_end:].lstrip(" ,.-—"), best_name


def strip_footers(text: str) -> tuple[str, list[str]]:
    removed = []
    for pattern, label in FOOTER_PATTERNS:
        new = re.sub(pattern, "", text, flags=re.S)
        if new != text:
            removed.append(label)
            text = new
    return text.rstrip(), removed


def redact(text: str, names: list[str], domains: list[str]) -> tuple[str, int]:
    """Replace surviving identifiers in place. Structure is preserved."""
    n = 0
    text, k = URL_RE.subn(URL_TOKEN, text)
    n += k
    for name in names:
        text, k = re.subn(re.escape(name), OUTLET_TOKEN, text, flags=re.I)
        n += k
    for dom in domains:
        text, k = re.subn(re.escape(dom), OUTLET_TOKEN, text, flags=re.I)
        n += k
    return text, n


def blind(text: str, names: list[str], domains: list[str],
          reporters: list[str] = ()) -> dict:
    body, byline = strip_byline(text, names, reporters)
    body, footers = strip_footers(body)
    body, redactions = redact(body, names, domains)
    return {
        "text": body,
        "byline_removed": byline,
        "byline_suspected": byline is None and text.lstrip().startswith("By "),
        "footers_removed": footers,
        "redactions": redactions,
    }


def leaks(text: str, names: list[str], domains: list[str]) -> list[str]:
    low = text.lower()
    return ([n for n in names if n.lower() in low]
            + [d for d in domains if d in low])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--audit", action="store_true", help="leak rate over the corpus")
    ap.add_argument("--sample", metavar="SOURCE_ID", help="show before/after rows")
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--verbose", action="store_true", help="list leaking rows")
    args = ap.parse_args()

    names, domains = load_identifiers()

    # Harvested once from the whole corpus, not per row: an unanchored byline
    # is only cuttable because the same reporter filed an anchored one
    # somewhere else, so the list has to be built before any row is blinded.
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(f"SELECT raw_text FROM {db.TABLES}.articles WHERE fulltext_ok = 1")
        reporters = harvest_reporters(" ".join(r[0].split()) for r in cur.fetchall())

    if args.sample:
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT title, raw_text FROM {db.TABLES}.articles"
                " WHERE source_id = %s AND fulltext_ok = 1 LIMIT %s",
                (args.sample, args.limit),
            )
            for title, raw in cur.fetchall():
                raw = " ".join(raw.split())
                r = blind(raw, names, domains, reporters)
                print(f"--- {title[:66]}")
                print(f"    BEFORE {raw[:100]}")
                print(f"    AFTER  {r['text'][:100]}")
                print(f"    byline={r['byline_removed']!r} footers={r['footers_removed']} "
                      f"redactions={r['redactions']} residual={leaks(r['text'], names, domains)}")
        return 0

    if not args.audit:
        ap.print_help()
        return 1

    with db.connect() as conn, conn.cursor() as cur:
        cur.execute(
            f"SELECT source_id, title, raw_text FROM {db.TABLES}.articles"
            " WHERE fulltext_ok = 1 ORDER BY source_id"
        )
        rows = cur.fetchall()

    stats, worst = {}, []
    for sid, title, raw in rows:
        raw = " ".join(raw.split())
        st = stats.setdefault(sid, {"n": 0, "before": 0, "after": 0, "suspect": 0})
        st["n"] += 1
        if leaks(raw, names, domains):
            st["before"] += 1
        r = blind(raw, names, domains, reporters)
        residual = leaks(r["text"], names, domains)
        if residual:
            st["after"] += 1
            worst.append((sid, title, residual))
        if r["byline_suspected"]:
            st["suspect"] += 1

    print("BLINDING AUDIT - outlet identifiable in body text")
    print(f"{'source':20} {'rows':>5} {'before':>8} {'after':>8} {'byline?':>8}")
    tot = {"n": 0, "before": 0, "after": 0, "suspect": 0}
    for sid in sorted(stats):
        s = stats[sid]
        for k in tot:
            tot[k] += s[k]
        pb = f"{100 * s['before'] // s['n']}%"
        pa = f"{100 * s['after'] // s['n']}%"
        print(f"{sid:20} {s['n']:>5} {pb:>8} {pa:>8} {s['suspect']:>8}")
    pb = f"{100 * tot['before'] // tot['n']}%" if tot["n"] else "-"
    pa = f"{100 * tot['after'] // tot['n']}%" if tot["n"] else "-"
    print(f"{'ALL':20} {tot['n']:>5} {pb:>8} {pa:>8} {tot['suspect']:>8}")
    print()
    print("  byline? = opens with 'By ' but no known outlet anchored the cut,")
    print("            so nothing was stripped. Visible on purpose.")

    if worst and args.verbose:
        print()
        print(f"RESIDUAL LEAKS ({len(worst)})")
        for sid, title, residual in worst[:40]:
            print(f"  {sid:18} {str(residual)[:44]:46} {(title or '')[:40]}")

    # A residual leak is a rule-1 failure, so it fails loudly rather than
    # printing a table nobody reads.
    return 1 if tot["after"] else 0


if __name__ == "__main__":
    sys.exit(main())
