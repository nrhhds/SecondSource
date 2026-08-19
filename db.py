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

import psycopg

# Schema holding every Second Source table. Interpolated into SQL by callers as
# a literal - it is a module constant, never user input.
TABLES = "secondsource"


def dsn() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit(
            "DATABASE_URL is not set.\n"
            "  Actions: add it as a repository secret.\n"
            "  Local:   export DATABASE_URL='postgresql://...pooler.supabase.com:5432/postgres'\n"
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
