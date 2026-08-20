#!/usr/bin/env python3
"""
Second Source - static site generation.

Reads the article store and emits flat HTML. No build step, no framework, no
server-side anything. Output is a directory of files that Vercel serves.

    python render.py                      # -> public/site
    python render.py --out public          # at launch, replaces the holding page
    python render.py --limit 40

Invariants enforced here, not left to the templates:

  * raw_text is never written to disk. Score and link out. The store is for
    analysis; it is not a copy of anyone's archive. See assert_no_republish().
  * every rendered score carries its rubric version.
  * withdrawn scores render as a withdrawal notice with the reason code, not as
    a missing page.
"""

import argparse
import html
import json
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from psycopg.rows import dict_row

import db

ROOT = Path(__file__).parent
SOURCES_PATH = ROOT / "sources.json"
ASSET_SRC = ROOT / "public"                    # shared style.css + fonts
PROSE_SRC = ROOT / "public" / "proto"          # hand-written pages, not yet data-driven
DEFAULT_OUT = ROOT / "public" / "site"

# Pages that are still hand-written prose rather than generated from data.
STATIC_PAGES = ["method.html", "logs.html", "byline.html"]

# Signals surfaced in the card readout, in display order.
CARD_SIGNALS = [
    ("named_sources", "Named"),
    ("anon_sources", "Anon"),
    ("stakeholder_categories", "Stakeholders"),
    ("primary_doc_links", "Primary docs"),
]


# --------------------------------------------------------------------------
# storage contract
# --------------------------------------------------------------------------
#
# The scores, receipts, withdrawals, clusters and cluster_members DDL used to
# live here as a SCHEMA string, and the articles/fetch_log DDL lived in
# ingest.py. Both now live only in schema.sql, applied through
# db.apply_schema(). Three copies of a schema is three chances for them to
# disagree, which already happened once: body_sha was added to ingest.py and
# had to be mirrored by hand.
#
# The reasoning that was attached to those tables is preserved in schema.sql -
# the long format so adding a rubric signal needs no migration, the receipts
# stored rather than regenerated, and the enumerated withdrawal codes as the
# only way to pull a score.


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def e(value) -> str:
    """Escape for HTML. Everything user-facing goes through this."""
    return html.escape("" if value is None else str(value), quote=True)


def assert_no_republish(markup: str, raw_texts: list[str]) -> None:
    """Hard stop if article body text reached the output.

    Cheap check, but it fails loudly on the one mistake that would actually be
    expensive: a template change that starts emitting stored article text.

    Probes from the middle of the body, not the opening. Extracted text usually
    begins with the headline, which legitimately appears in output - probing the
    first line would fail on every article for the wrong reason.
    """
    haystack = " ".join(markup.split())
    for raw in raw_texts:
        if not raw:
            continue
        words = raw.split()
        if len(words) < 60:
            continue
        for start in (len(words) // 3, len(words) // 2):
            probe = " ".join(words[start:start + 12])
            if len(probe) > 40 and probe in haystack:
                raise SystemExit(
                    "refusing to write output: stored article text appears in "
                    "the rendered page. Score and link out - never republish."
                )


def load_sources() -> dict:
    data = json.loads(SOURCES_PATH.read_text(encoding="utf-8"))
    return {s["id"]: s for s in data["sources"]}


def fmt_date(value) -> str:
    """Format a timestamp for display, from a datetime or an ISO string.

    published_at and fetched_at are TIMESTAMPTZ, so psycopg hands back datetime
    objects where SQLite returned the ISO text it was given. Strings still
    reach here from elsewhere, so both are accepted - same reason health.py's
    parse() takes either.

    The two strftime attempts are a platform split, not a type one: %-d strips
    the leading zero on glibc and raises on Windows, where the fallback trims
    it by hand.
    """
    if not value:
        return ""
    dt = value if isinstance(value, datetime) else None
    if dt is None:
        try:
            dt = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return str(value)[:10]
    try:
        return dt.strftime("%b %-d, %Y")
    except ValueError:
        return dt.strftime("%b %d, %Y").replace(" 0", " ")


def pct(part: float, whole: float) -> float:
    return 0.0 if not whole else round(100.0 * part / whole, 1)


# --------------------------------------------------------------------------
# templates
# --------------------------------------------------------------------------

def page(title: str, body: str, *, depth: int = 0, nav: str = "") -> str:
    """Shell shared by every generated page. depth = directories below root."""
    up = "../" * depth
    links = [("index.html", "Latest"), ("story.html", "Stories"),
             ("outlet.html", "Outlets"), ("method.html", "Method"),
             ("logs.html", "Logs")]
    nav_html = "".join(
        f'<a href="{up}{href}"{" aria-current=\"page\"" if label == nav else ""}>{label}</a>'
        for href, label in links
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)}</title>
<link rel="stylesheet" href="{up}style.css">
</head>
<body>
<div class="shell">
  <header class="masthead">
    <a class="wordmark" href="{up}index.html">Second<span>.</span>Source</a>
    <span class="domain">chooseyourbias.com</span>
    <nav>{nav_html}</nav>
  </header>
{body}
  <footer class="foot">
    Rubric and source code are public at
    <a href="https://github.com/nrhhds/SecondSource">github.com/nrhhds/SecondSource</a>.
    Scores publish without human review and are withdrawn only by logged reason code.
  </footer>
</div>
</body>
</html>
"""


def mbar(segments: list[tuple[str, float]]) -> str:
    """The measurement bar. segments = [(css_class, width_percent), ...]"""
    inner = "".join(f'<i class="{cls}" style="width:{w:.4g}%"></i>' for cls, w in segments)
    return f'<div class="mbar">{inner}</div>'


def readout(scores: dict) -> str:
    cells = []
    for key, label in CARD_SIGNALS:
        if key not in scores:
            continue
        val = scores[key]["value"]
        shown = f"{val:g}" if val is not None else e(scores[key]["value_text"])
        cells.append(f"<span>{label}<b>{shown}</b></span>")
    return f'<div class="readout">{"".join(cells)}</div>' if cells else ""


def article_card(row: dict, src: dict, scores: dict, *, depth: int) -> str:
    up = "../" * depth
    outlet = e(src.get("name", row["source_id"]))
    href = f"{up}article/{row['id']}.html"

    if not row["fulltext_ok"]:
        why = ("Hard paywall - headline and dek only."
               if src.get("paywall") == "hard" else "Full text unavailable.")
        return f"""
        <article class="card">
          <div class="meta"><span class="outlet">{outlet}</span>
            <span class="tag tag--out">Not scored</span>
            <span class="sep">/</span><span>{e(fmt_date(row["published_at"]))}</span></div>
          <h3><a href="{e(row["url"])}" rel="nofollow noopener">{e(row["title"])}</a></h3>
          <p class="note" style="margin:.5rem 0 0">{why} Counted as coverage present,
            excluded from scoring.</p>
        </article>"""

    bar = ""
    if "quote_words_for" in scores and "quote_words_against" in scores:
        f = scores["quote_words_for"]["value"] or 0
        a = scores["quote_words_against"]["value"] or 0
        total = f + a
        if total:
            bar = (mbar([("a", pct(f, total)), ("b", pct(a, total))]) +
                   f'<div class="mstat"><span><b>{pct(max(f, a), total):.0f}%</b> '
                   f'of quoted words to one side</span></div>')

    return f"""
        <article class="card">
          <div class="meta"><span class="outlet">{outlet}</span>
            {'<span class="tag tag--anchor">Anchor</span>' if src.get("calibration_anchor") else ''}
            <span class="sep">/</span><span>{e(fmt_date(row["published_at"]))}</span></div>
          <h3><a href="{href}">{e(row["title"])}</a></h3>
          {bar}
          {readout(scores)}
        </article>"""


def signal_block(signal: str, s: dict, receipts: list[dict]) -> str:
    val = f"{s['value']:g}" if s["value"] is not None else e(s["value_text"])
    judged = ' <span class="judged">J</span>' if s["judged"] else ""
    desc = ""
    if s["judged"]:
        bits = []
        if s["confidence"] is not None:
            bits.append(f"Confidence {s['confidence']:.2f}")
        if s["spread"] is not None:
            bits.append(f"spread &plusmn;{s['spread']:.1f} over 5 runs")
        desc = f'<p class="signal-desc">Model-judged. {", ".join(bits)}.</p>'

    rec = ""
    if receipts:
        items = "".join(
            f'<li><span class="para">{"&para;" + str(r["paragraph"]) if r["paragraph"] is not None else ""}</span>'
            f'{("<mark>" + e(r["quote"]) + "</mark>") if r["quote"] else ""}'
            f'{(" <span class=\"why\">" + e(r["note"]) + "</span>") if r["note"] else ""}</li>'
            for r in receipts
        )
        rec = f'<details class="receipts"><summary>Receipts</summary><ul class="evidence">{items}</ul></details>'

    return f"""
      <div class="signal">
        <div class="signal-head"><span class="signal-name">{e(signal)}{judged}</span>
          <span class="signal-val">{val}</span></div>
        {desc}{rec}
      </div>"""


def article_page(row: dict, src: dict, scores: dict, receipts: dict,
                 withdrawal: dict | None) -> str:
    outlet = e(src.get("name", row["source_id"]))
    version = next(iter(scores.values()))["rubric_version"] if scores else "-"

    if withdrawal:
        body = f"""
  <span class="kicker" style="margin-top:1.8rem">Score withdrawn</span>
  <h1 class="page sm" style="margin-top:.3rem">{e(row["title"])}</h1>
  <div class="meta"><span class="outlet">{outlet}</span><span class="sep">/</span>
    <span>{e(fmt_date(row["published_at"]))}</span></div>
  <div class="correction" style="margin-top:2rem">
    <b>Withdrawn &middot; {e(withdrawal["code"])} &middot; {e(fmt_date(withdrawal["withdrawn_at"]))}</b>
    {e(withdrawal["detail"] or "")} This withdrawal is listed in the
    <a href="../logs.html">public log</a>.
  </div>"""
        return page(f"Withdrawn - {row['title']}", body, depth=1)

    blocks = "".join(
        signal_block(sig, s, receipts.get(sig, []))
        for sig, s in sorted(scores.items())
    ) or '<p class="note">Not yet scored.</p>'

    body = f"""
  <span class="kicker" style="margin-top:1.8rem">Article score</span>
  <h1 class="page sm" style="margin-top:.3rem">{e(row["title"])}</h1>
  <div class="meta">
    <a class="outlet" href="../outlet/{e(row["source_id"])}.html">{outlet}</a><span class="sep">/</span>
    <span>{e(fmt_date(row["published_at"]))}</span><span class="sep">/</span>
    <span>{row["word_count"]:,} words</span><span class="sep">/</span>
    <a href="{e(row["url"])}" rel="nofollow noopener" style="text-decoration:underline">Read at the source &rarr;</a>
  </div>

  <div class="caveat" style="margin-top:1.75rem">
    <strong>No composite score.</strong> Components below are what the code
    counted. Quoted fragments are verbatim, shown so you can check the count
    against the article - not as a substitute for reading it.
  </div>

  <main>{blocks}</main>

  <div class="repro">
    <div class="sec" style="margin-top:0"><h2>Reproduce this</h2></div>
    <pre>python score.py --url {e(row["url"])} --rubric {e(version)} --seed 7</pre>
    <p class="stamp">scored blind &middot; rubric v{e(version)} &middot; article {e(row["id"])}</p>
  </div>"""
    return page(f"Score - {row['title']}", body, depth=1)


def index_page(cards: str, stats: dict) -> str:
    body = f"""
  <div class="status">
    <b>Pre-launch</b>
    <span>The engine has not passed the wire-anchor, separation, or valence-swap
      tests. Nothing here is published as a finding.</span>
    <a href="method.html">What has to be true first &rarr;</a>
  </div>

  <div class="cols">
    <main>
      <div class="sec"><h2>Scored recently</h2>
        <span class="count">{stats["shown"]} of {stats["scored"]:,} scored</span></div>
      <div class="cards">{cards}</div>
    </main>
    <aside class="rail">
      <div class="sec" style="margin-top:1.6rem"><h2>Collection</h2></div>
      <div class="statrow" style="grid-template-columns:1fr 1fr;margin-top:0">
        <div><div class="k">Articles</div><div class="v">{stats["total"]:,}</div></div>
        <div><div class="k">Full text</div><div class="v">{stats["fulltext"]:,}</div></div>
        <div><div class="k">Scored</div><div class="v">{stats["scored"]:,}</div></div>
        <div><div class="k">Withdrawals</div><div class="v">{stats["withdrawn"]:,}</div></div>
      </div>
      <p class="note">Generated {e(stats["generated"])}. Every withdrawal carries a
        reason code and appears in the <a href="logs.html">public log</a>.</p>
    </aside>
  </div>"""
    return page("Second Source", body, nav="Latest")


def outlet_index_page(sources: dict, counts: dict, full: dict) -> str:
    rows = sorted(sources.values(), key=lambda s: -counts.get(s["id"], 0))
    trs = []
    for s in rows:
        n = counts.get(s["id"], 0)
        name = e(s["name"])
        cell = (f'<a href="outlet/{e(s["id"])}.html">{name}</a>' if n else
                f'<span class="no">{name}</span>')
        anchor = ' <span class="tag tag--anchor">Anchor</span>' if s.get("calibration_anchor") else ""
        trs.append(
            f'<tr><td>{cell}{anchor}</td><td>{e(s.get("status", ""))}</td>'
            f'<td>{"full" if s.get("fulltext") else "headline + dek"}</td>'
            f'<td class="num">{n:,}</td><td class="num">{full.get(s["id"], 0):,}</td></tr>')

    body = f"""
  <span class="kicker" style="margin-top:1.8rem">Outlets</span>
  <h1 class="page" style="margin-top:.3rem">Tracked sources</h1>
  <div class="meta"><span>{len(sources)} sources</span><span class="sep">/</span>
    <span>{sum(1 for c in counts.values() if c)} collecting</span></div>

  <div class="sec"><h2>Collection status</h2></div>
  <div class="scroll"><table>
    <thead><tr><th>Outlet</th><th>Status</th><th>Text</th>
      <th class="num">Collected</th><th class="num">Full text</th></tr></thead>
    <tbody>{"".join(trs)}</tbody>
  </table></div>
  <p class="note" style="margin-top:.9rem;max-width:44rem">
    Outlets with no rows are hard-paywalled or blocked to automated fetching.
    They are absent from the analysis, which is a limitation of this project and
    not a statement about them.</p>"""
    return page("Outlets - Second Source", body, nav="Outlets")


def story_index_page(clusters: list) -> str:
    if not clusters:
        inner = """
  <div class="caveat" style="margin-top:1.75rem">
    <strong>No clusters yet.</strong> Story clustering needs three or more
    outlets on the same story before omission analysis means anything. Nothing
    is published from a cluster of one.
  </div>"""
    else:
        inner = '<div class="cards">' + "".join(
            f"""<article class="card">
              <div class="meta"><span>{c["n"]} outlets</span></div>
              <h3><a href="story/{e(c["id"])}.html">{e(c["label"])}</a></h3>
            </article>""" for c in clusters) + "</div>"

    body = f"""
  <span class="kicker" style="margin-top:1.8rem">Stories</span>
  <h1 class="page" style="margin-top:.3rem">Story clusters</h1>
  {inner}"""
    return page("Stories - Second Source", body, nav="Stories")


def outlet_page(src: dict, rows: list, cards: str) -> str:
    body = f"""
  <span class="kicker" style="margin-top:1.8rem">Outlet</span>
  <h1 class="page" style="margin-top:.3rem">{e(src["name"])}</h1>
  <div class="meta"><span>{e(src.get("status", ""))}</span><span class="sep">/</span>
    <span>{"full text" if src.get("fulltext") else "headline only"}</span><span class="sep">/</span>
    <span>{len(rows):,} articles collected</span></div>

  <div class="caveat" style="margin-top:1.75rem">
    <strong>This page carries no lean label.</strong> Priors exist in one file and
    are used in one place - as the hypothesis the calibration tries to falsify.
    A prior never reaches the scorer and never appears beside an outlet's name here.
    <a href="../method.html" style="text-decoration:underline">The priors, stated openly &rarr;</a>
  </div>

  <div class="sec"><h2>Recent</h2></div>
  <div class="cards">{cards}</div>"""
    return page(f"{src['name']} - Second Source", body, depth=1, nav="Outlets")


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

def scalar(conn, sql: str):
    """First column of the first row, for a connection using dict_row.

    sqlite3.Row supports both positional and by-name access, so `fetchone()[0]`
    worked on aggregates. psycopg's dict_row returns a plain dict, where [0] is
    a KeyError - hence the `n` alias on every aggregate below.
    """
    return next(iter(conn.execute(sql).fetchone().values()))


def fetch_scores(conn) -> tuple[dict, dict]:
    by_article = defaultdict(dict)
    for r in conn.execute(f"SELECT * FROM {db.TABLES}.scores"):
        by_article[r["article_id"]][r["signal"]] = dict(r)
    by_signal = defaultdict(lambda: defaultdict(list))
    for r in conn.execute(f"SELECT * FROM {db.TABLES}.receipts ORDER BY paragraph"):
        by_signal[r["article_id"]][r["signal"]].append(dict(r))
    return by_article, by_signal


def build(out: Path, limit: int) -> int:
    # dict_row is the psycopg equivalent of sqlite3.Row: rows index by column
    # name, which every template below relies on.
    conn = db.connect()
    conn.row_factory = dict_row
    db.apply_schema(conn)

    sources = load_sources()
    scores, receipts = fetch_scores(conn)
    withdrawn = {
        r["article_id"]: dict(r)
        for r in conn.execute(f"SELECT * FROM {db.TABLES}.withdrawals")
    }

    rows = [dict(r) for r in conn.execute(
        f"SELECT * FROM {db.TABLES}.articles"
        " ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT %s",
        (limit,))]

    out.mkdir(parents=True, exist_ok=True)
    (out / "article").mkdir(exist_ok=True)
    (out / "outlet").mkdir(exist_ok=True)

    raw_texts = [r["raw_text"] for r in rows]
    written = 0

    for row in rows:
        src = sources.get(row["source_id"], {"name": row["source_id"]})
        markup = article_page(row, src, scores.get(row["id"], {}),
                              receipts.get(row["id"], {}), withdrawn.get(row["id"]))
        assert_no_republish(markup, [row["raw_text"]])
        (out / "article" / f"{row['id']}.html").write_text(markup, encoding="utf-8")
        written += 1

    by_outlet = defaultdict(list)
    for row in rows:
        by_outlet[row["source_id"]].append(row)

    for sid, orows in by_outlet.items():
        src = sources.get(sid, {"name": sid})
        cards = "".join(article_card(r, src, scores.get(r["id"], {}), depth=1)
                        for r in orows[:12])
        markup = outlet_page(src, orows, cards)
        assert_no_republish(markup, [r["raw_text"] for r in orows])
        (out / "outlet" / f"{sid}.html").write_text(markup, encoding="utf-8")
        written += 1

    cards = "".join(
        article_card(r, sources.get(r["source_id"], {"name": r["source_id"]}),
                     scores.get(r["id"], {}), depth=0)
        for r in rows[:20])
    stats = {
        # scalar() rather than [0]: dict_row makes a row a dict keyed by column
        # name, so positional indexing raises KeyError instead of returning the
        # count.
        "total": scalar(conn, f"SELECT COUNT(*) n FROM {db.TABLES}.articles"),
        "fulltext": scalar(
            conn, f"SELECT COUNT(*) n FROM {db.TABLES}.articles WHERE fulltext_ok=1"),
        "scored": scalar(
            conn, f"SELECT COUNT(DISTINCT article_id) n FROM {db.TABLES}.scores"),
        "withdrawn": len(withdrawn),
        "shown": min(len(rows), 20),
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%MZ"),
    }
    markup = index_page(cards, stats)
    assert_no_republish(markup, raw_texts)
    (out / "index.html").write_text(markup, encoding="utf-8")
    written += 1

    # Index pages the nav links to. Counts come from the whole store, not the
    # render window, so they don't shrink when --limit does.
    counts = {r["source_id"]: r["n"] for r in conn.execute(
        f"SELECT source_id, COUNT(*) n FROM {db.TABLES}.articles GROUP BY source_id")}
    full = {r["source_id"]: r["n"] for r in conn.execute(
        f"SELECT source_id, COUNT(*) n FROM {db.TABLES}.articles"
        " WHERE fulltext_ok=1 GROUP BY source_id")}
    (out / "outlet.html").write_text(outlet_index_page(sources, counts, full), encoding="utf-8")

    clusters = [dict(r) for r in conn.execute(
        f"""SELECT c.id, c.label, COUNT(m.article_id) n FROM {db.TABLES}.clusters c
            LEFT JOIN {db.TABLES}.cluster_members m ON m.cluster_id = c.id
            GROUP BY c.id, c.label ORDER BY n DESC""")]
    (out / "story.html").write_text(story_index_page(clusters), encoding="utf-8")
    written += 2

    # Design assets and the pages that are still hand-written prose.
    shutil.copy2(ASSET_SRC / "style.css", out / "style.css")
    if (ASSET_SRC / "fonts").exists():
        shutil.copytree(ASSET_SRC / "fonts", out / "fonts", dirs_exist_ok=True)
    for name in STATIC_PAGES:
        src_page = PROSE_SRC / name
        if src_page.exists():
            # Those pages sit one level below the shared stylesheet in the repo;
            # in the build they sit beside it.
            (out / name).write_text(
                src_page.read_text(encoding="utf-8").replace('"../style.css"', '"style.css"'),
                encoding="utf-8")
            written += 1

    conn.close()
    print(f"wrote {written} files to {out}")
    print(f"  {stats['total']:,} articles, {stats['fulltext']:,} with full text, "
          f"{stats['scored']:,} scored, {stats['withdrawn']} withdrawn")
    if not stats["scored"]:
        print("  note: no rows in `scores` yet - article pages render as 'not yet scored'")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help="output directory (default: public/site)")
    ap.add_argument("--limit", type=int, default=200,
                    help="most recent N articles to render (default: 200)")
    # --db is gone with SQLite: there is one store now, addressed by
    # DATABASE_URL, and pointing this at a file would silently render nothing.
    args = ap.parse_args()
    return build(args.out, args.limit)


if __name__ == "__main__":
    sys.exit(main())
