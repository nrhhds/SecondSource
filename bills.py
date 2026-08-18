#!/usr/bin/env python3
"""
Second Source - LegiScan bulk dataset puller.

Free tier, weekly JSON snapshots. Weekly cadence matches weekly publishing;
the paid real-time API is not needed.

Bill data is public record. Unlike article text it is safe to store and to
republish, but the three fields the project actually depends on are:

    history[].importance  - the omission filter (major steps vs procedural noise)
    change_hash           - week-over-week diffing
    supplements[]         - fiscal notes/analyses, to check whether coverage
                            linked the primary document

Set the API key in .env (gitignored), never on the command line:

    LEGISCAN_API_KEY=...

Usage:
    python bills.py list                 # sessions + dataset dates for FL
    python bills.py pull --year 2026     # download + extract one session
    python bills.py fields --year 2026   # verify the fields above exist
    python bills.py watch --quiet        # flag sessions LegiScan just added
"""

import argparse
import base64
import io
import json
import os
import sys
import time
import zipfile
from collections import Counter
from pathlib import Path

import requests

ROOT = Path(__file__).parent
DATA = ROOT / "data" / "legiscan"
ENV_PATH = ROOT / ".env"
KNOWN_SESSIONS_PATH = DATA / "known_sessions.json"

API = "https://api.legiscan.com/"
STATE = "FL"
REQUEST_TIMEOUT = 30
DELAY_BETWEEN_CALLS = 1.0


def load_key() -> str:
    key = os.environ.get("LEGISCAN_API_KEY")
    if key:
        return key.strip()
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, val = line.partition("=")
            if name.strip() == "LEGISCAN_API_KEY":
                return val.strip().strip("'\"")
    sys.exit(
        "No LEGISCAN_API_KEY found.\n"
        f"Create {ENV_PATH} containing:  LEGISCAN_API_KEY=your_key_here\n"
        "(.env is gitignored.)"
    )


def api(op: str, **params) -> dict:
    """One API call. Raises on transport or API-level error."""
    params = {"key": load_key(), "op": op, **params}
    resp = requests.get(API, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != "OK":
        raise RuntimeError(f"{op}: {payload.get('alert') or payload}")
    time.sleep(DELAY_BETWEEN_CALLS)
    return payload


def datasets() -> list[dict]:
    return api("getDatasetList", state=STATE).get("datasetlist", [])


def cmd_list(_args) -> int:
    rows = datasets()
    if not rows:
        print("no datasets returned")
        return 1
    print(f"{'session_id':>11}  {'years':<11} {'dataset_date':<13} {'size':>9}  session_name")
    for d in sorted(rows, key=lambda r: r.get("year_start", 0)):
        years = f"{d.get('year_start')}-{d.get('year_end')}"
        size = d.get("dataset_size") or 0
        print(
            f"{d.get('session_id'):>11}  {years:<11} {str(d.get('dataset_date')):<13} "
            f"{size:>9,}  {d.get('session_name')}"
        )
    return 0


def save_known(state: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    KNOWN_SESSIONS_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def cmd_watch(args) -> int:
    """Report FL sessions LegiScan has added since the last run.

    The 2027 session cannot be pulled until Florida opens prefiling for it, and
    checking by hand is the chore that quietly gets dropped for three months.
    Runs alongside the twice-daily ingest so the session's arrival lands in
    ingest.log rather than waiting to be remembered.

    A new session stays loud every run until its dataset is on disk. Announcing
    once would put the whole point of this on a single log line nobody read.
    """
    live = {
        str(d["session_id"]): d.get("session_name") or ""
        for d in datasets()
        if d.get("session_id")
    }
    if not live:
        print("watch: LegiScan returned no sessions", file=sys.stderr)
        return 1

    if not KNOWN_SESSIONS_PATH.exists():
        # Everything LegiScan already lists is by definition not news.
        save_known({"sessions": live, "pending": []})
        if not args.quiet:
            print(f"watch: baseline seeded with {len(live)} FL sessions")
        return 0

    state = json.loads(KNOWN_SESSIONS_PATH.read_text(encoding="utf-8"))
    known = state.get("sessions") or {}
    pending = [s for s in (state.get("pending") or []) if s in live]

    fresh = [s for s in sorted(live, key=int) if s not in known and s not in pending]
    unpulled = [s for s in pending if not (DATA / f"FL_{s}").exists()]

    for sid in fresh:
        print(f"watch: NEW SESSION {sid}  {live[sid]}")
        print(f"       pull it with:  python bills.py pull --session-id {sid}")
    for sid in unpulled:
        print(f"watch: {sid} {live[sid]} still not pulled - "
              f"python bills.py pull --session-id {sid}")

    known.update(live)
    save_known({
        "sessions": known,
        "pending": sorted(set(fresh) | set(unpulled), key=int),
    })

    if not fresh and not unpulled and not args.quiet:
        print(f"watch: no new FL sessions ({len(known)} known)")
    return 0


def pick(rows: list[dict], year: int | None, session_id: int | None) -> dict:
    """Resolve one dataset. Florida runs several special sessions a year, so a
    year alone is ambiguous - prefer the regular session and make the rest explicit."""
    if session_id:
        for d in rows:
            if d.get("session_id") == session_id:
                return d
        sys.exit(f"no FL dataset with session_id {session_id}. Run 'list'.")

    matches = [
        d for d in rows
        if d.get("year_start") and d.get("year_end")
        and d["year_start"] <= year <= d["year_end"]
    ]
    if not matches:
        sys.exit(f"no FL dataset covering {year}. Run 'list' to see what exists.")

    regular = [d for d in matches if "regular" in str(d.get("session_name", "")).lower()]
    if len(regular) == 1:
        if len(matches) > 1:
            others = len(matches) - 1
            print(f"note: {others} other {year} session(s) exist (special); using the regular session")
        return regular[0]

    print(f"{len(matches)} sessions cover {year} - pass --session-id to choose:", file=sys.stderr)
    for d in sorted(matches, key=lambda r: str(r.get("session_name"))):
        print(f"  {d.get('session_id'):>6}  {d.get('dataset_size') or 0:>10,}  {d.get('session_name')}", file=sys.stderr)
    sys.exit(1)


def cmd_pull(args) -> int:
    d = pick(datasets(), args.year, args.session_id)
    print(f"session {d['session_id']}: {d.get('session_name')} (snapshot {d.get('dataset_date')})")

    payload = api("getDataset", id=d["session_id"], access_key=d["access_key"])
    blob = payload.get("dataset", {})
    if "zip" not in blob:
        print(f"unexpected getDataset response, keys: {sorted(blob)}", file=sys.stderr)
        return 1

    raw = base64.b64decode(blob["zip"])
    out = DATA / f"FL_{d['session_id']}"
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        zf.extractall(out)
        names = zf.namelist()

    # access_key is a credential, not metadata - keep it out of the saved snapshot record.
    (DATA / f"FL_{d['session_id']}.meta.json").write_text(
        json.dumps({k: v for k, v in d.items() if k != "access_key"}, indent=2),
        encoding="utf-8",
    )
    print(f"extracted {len(names)} files to {out}")
    print(f"dataset_hash {d.get('dataset_hash')} recorded for week-over-week diffing")
    return 0


def resolve_local(year: int | None, session_id: int | None) -> Path:
    """Find an already-extracted dataset directory, mirroring pick()'s preference."""
    if session_id:
        root = DATA / f"FL_{session_id}"
        if not root.exists():
            sys.exit(f"{root} not found - run: python bills.py pull --session-id {session_id}")
        return root

    metas = []
    for m in DATA.glob("FL_*.meta.json"):
        meta = json.loads(m.read_text(encoding="utf-8"))
        if meta.get("year_start") and meta["year_start"] <= year <= meta["year_end"]:
            metas.append(meta)
    if not metas:
        sys.exit(f"no extracted {year} dataset - run: python bills.py pull --year {year}")

    regular = [m for m in metas if "regular" in str(m.get("session_name", "")).lower()]
    chosen = regular[0] if len(regular) == 1 else metas[0]
    if len(metas) > 1:
        print(f"using {chosen.get('session_name')} ({len(metas)} local {year} datasets)")
    return DATA / f"FL_{chosen['session_id']}"


def bill_files(year: int | None, session_id: int | None) -> list[Path]:
    return sorted(resolve_local(year, session_id).rglob("bill/*.json"))


def cmd_fields(args) -> int:
    files = bill_files(args.year, args.session_id)
    if not files:
        print("no bill/*.json files found; inspect the extracted tree layout", file=sys.stderr)
        return 1

    n = len(files)
    if n < 200:
        print(f"note: only {n} bills - if you expected a full regular session, "
              f"check you pulled the right session_id\n", file=sys.stderr)
    with_hash = with_supp = with_hist = 0
    importance = Counter()
    supp_types = Counter()
    sample = None

    for f in files:
        bill = json.loads(f.read_text(encoding="utf-8")).get("bill", {})
        if sample is None:
            sample = bill
        if bill.get("change_hash"):
            with_hash += 1
        hist = bill.get("history") or []
        if hist:
            with_hist += 1
            for h in hist:
                importance[h.get("importance")] += 1
        supps = bill.get("supplements") or []
        if supps:
            with_supp += 1
            for s in supps:
                # Do NOT classify on type/type_id. In the FL 2026 dataset every
                # supplement is typed "Veto Letter" (type_id 8) including committee
                # staff analyses. title + state_link are what actually identify them.
                supp_types[s.get("title") or "(untitled)"] += 1

    print(f"bills in FL {args.year} dataset: {n}\n")
    print("--- fields the project depends on ---")
    print(f"change_hash present       {with_hash:>6} / {n}")
    print(f"history[] present         {with_hist:>6} / {n}")
    print(f"  history[].importance    {dict(importance)}")
    print(f"supplements[] present     {with_supp:>6} / {n}")
    print(f"  supplement types        {dict(supp_types.most_common(10))}")
    print("\n--- top-level keys on a sample bill ---")
    print(", ".join(sorted(sample)))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list FL sessions and dataset snapshots").set_defaults(fn=cmd_list)
    w = sub.add_parser("watch", help="report FL sessions added since the last run")
    w.add_argument("--quiet", action="store_true", help="print only new or unpulled sessions")
    w.set_defaults(fn=cmd_watch)
    for name, fn, help_text in (
        ("pull", cmd_pull, "download and extract a session dataset"),
        ("fields", cmd_fields, "verify required fields exist in an extracted dataset"),
    ):
        s = sub.add_parser(name, help=help_text)
        g = s.add_mutually_exclusive_group(required=True)
        g.add_argument("--year", type=int, help="prefers the regular session for that year")
        g.add_argument("--session-id", type=int, dest="session_id", help="exact session, e.g. 2220")
        s.set_defaults(fn=fn, year=None, session_id=None)
    args = ap.parse_args()
    try:
        return args.fn(args)
    except requests.RequestException as e:
        print(f"request failed: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"API error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
