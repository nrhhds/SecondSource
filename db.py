#!/usr/bin/env python3
"""
Second Source - Postgres connection.

Single place that knows how to reach the store, so ingest.py, health.py,
render.py and the migration share one connection policy.

Connection string comes from DATABASE_URL and is never stored in the repo. In
GitHub Actions it is a repository secret; locally, export it or keep it in
.env (gitignored).

---------------------------------------------------------------------------
Use the Supavisor POOLER connection string, not the direct one
---------------------------------------------------------------------------
Supabase direct connections (db.<ref>.supabase.co:5432) resolve to IPv6 only
unless the project has the IPv4 add-on. GitHub Actions runners have no IPv6
connectivity, so a direct URI fails there with a network unreachable error
that looks nothing like a configuration problem. The pooler host is
dual-stack. Session mode (port 5432 on the pooler host) is the closest match
to a plain connection; transaction mode (6543) also works here because
prepare_threshold is disabled below.

---------------------------------------------------------------------------
Why every query schema-qualifies its tables
---------------------------------------------------------------------------
Tables live in the 'secondsource' schema, deliberately outside the schemas
Supabase exposes over PostgREST (see schema.sql). Rather than relying on
search_path, which does not survive transaction-mode pooling, callers write
f"... FROM {TABLES}.articles ...". Explicit, pooler-agnostic, and a
misconfiguration fails loudly with "relation does not exist" instead of
silently reading the wrong schema.
"""

import os
import sys
from pathlib import Path

import psycopg

# Schema holding every Second Source table. Interpolated into SQL by callers as
# a literal - it is a module constant, never user input.
TABLES = "secondsource"

ENV_PATH = Path(__file__).parent / ".env"


def from_env_file(name: str) -> str | None:
    """Read one name out of .env, same convention as bills.py's load_key().

    The environment wins where it is set, which is how Actions injects the
    secret. Locally the value lives in .env beside LEGISCAN_API_KEY, so a
    plain `python ingest.py` works in a fresh shell without exporting anything
    first - the alternative is a confusing "DATABASE_URL is not set" in a
    checkout that plainly has it.
    """
    if not ENV_PATH.exists():
        return None
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        if key.strip() == name:
            return val.strip().strip('"').strip("'") or None
    return None


def dsn() -> str:
    url = os.environ.get("DATABASE_URL") or from_env_file("DATABASE_URL")
    if not url:
        sys.exit(
            "DATABASE_URL is not set.\n"
            "  Actions: add it as a repository secret.\n"
            f"  Local:   add a DATABASE_URL= line to {ENV_PATH.name} (gitignored),\n"
            "           or export it in the shell.\n"
            "Use the Supabase pooler host, not db.<ref>.supabase.co (IPv6-only)."
        )
    return url


def connect(autocommit: bool = False) -> psycopg.Connection:
    """Open a connection. Caller commits unless autocommit is requested.

    prepare_threshold=None disables psycopg's automatic prepared statements.
    They are a small win on repeated inserts but they break under
    transaction-mode pooling, where consecutive statements can land on
    different backend sessions. Correctness over the microseconds.
    """
    return psycopg.connect(dsn(), autocommit=autocommit, prepare_threshold=None)


SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def apply_schema(conn) -> None:
    """Apply schema.sql. Idempotent, and the ONLY definition of the schema.

    ingest.py used to carry its own CREATE TABLE statements and render.py a
    second set, so three places could disagree about the shape of the store -
    and did. body_sha was added to ingest.py's DDL and had to be mirrored into
    schema.sql by hand; the next column would have been another chance to
    forget, failing at runtime with "column does not exist".

    Reading the file means a column can only be added once. Cheap enough to run
    on every ingest, so a fresh database needs no separate bootstrap step.
    """
    with conn.cursor() as cur:
        cur.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
