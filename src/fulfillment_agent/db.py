"""Database access: setup, a guarded read-only query runner, and schema introspection.

Safety is layered rather than trusted to a regex:
1. Model-written SQL runs as `agent_ro`, a role with SELECT grants only and
   default_transaction_read_only=on (see sql/roles.sql).
2. Each query also runs inside an explicit READ ONLY transaction with a
   statement timeout, and only one statement is accepted.
3. Results are capped at a row limit so a careless query can't flood the
   model's context window.
"""

from __future__ import annotations

import datetime as dt
import decimal
import re
from dataclasses import dataclass

import psycopg
from psycopg import sql as pgsql

from .config import PROJECT_ROOT, Settings

SQL_DIR = PROJECT_ROOT / "sql"
TABLE_ORDER = ["products", "inventory", "customers", "orders", "order_items", "substitution_proposals"]


class QueryRejected(ValueError):
    """Raised when SQL fails the pre-execution checks."""


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list]
    truncated: bool

    def to_payload(self) -> dict:
        return {
            "columns": self.columns,
            "rows": self.rows,
            "row_count": len(self.rows),
            "truncated": self.truncated,
        }


def connect(url: str, autocommit: bool = False) -> psycopg.Connection:
    # Store-local time zone so ::date casts and "today" match the store.
    return psycopg.connect(url, autocommit=autocommit, options="-c TimeZone=America/Chicago")


def _jsonable(value):
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    return value


_LEADING_COMMENTS = re.compile(r"^\s*(--[^\n]*\n|/\*.*?\*/\s*)*", re.S)


def check_sql(query: str) -> str:
    """Cheap pre-checks. The database role is the real enforcement."""
    q = query.strip().rstrip(";").strip()
    if not q:
        raise QueryRejected("Empty query.")
    if ";" in q:
        raise QueryRejected("Only a single SQL statement is allowed per call.")
    first_word = _LEADING_COMMENTS.sub("", q).split(None, 1)[0].lower() if q else ""
    if first_word not in ("select", "with"):
        raise QueryRejected("Only read-only SELECT (or WITH ... SELECT) queries are allowed.")
    return q


def run_readonly_query(settings: Settings, query: str, row_limit: int | None = None) -> QueryResult:
    q = check_sql(query)
    limit = row_limit or settings.sql_row_limit
    with connect(settings.ro_url) as conn:
        conn.read_only = True
        with conn.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = '5s'")
            cur.execute(q)
            if cur.description is None:
                return QueryResult([], [], False)
            columns = [c.name for c in cur.description]
            fetched = cur.fetchmany(limit + 1)
        conn.rollback()
    truncated = len(fetched) > limit
    rows = [[_jsonable(v) for v in row] for row in fetched[:limit]]
    return QueryResult(columns, rows, truncated)


def describe_schema(settings: Settings) -> str:
    """Compact, model-friendly schema description built from the live catalog,
    including the COMMENT ON text from schema.sql."""
    query = """
        SELECT c.table_name, c.column_name, c.data_type,
               col_description(format('public.%I', c.table_name)::regclass, c.ordinal_position) AS col_comment,
               obj_description(format('public.%I', c.table_name)::regclass, 'pg_class') AS table_comment
        FROM information_schema.columns c
        WHERE c.table_schema = 'public'
        ORDER BY c.table_name, c.ordinal_position
    """
    with connect(settings.ro_url) as conn, conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()
        # pg_catalog rather than information_schema: the latter hides
        # constraints on tables the current (read-only) role doesn't own.
        cur.execute("""
            SELECT src.relname, a.attname, dst.relname, b.attname
            FROM pg_constraint con
            JOIN pg_class src ON src.oid = con.conrelid
            JOIN pg_class dst ON dst.oid = con.confrelid
            JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = con.conkey[1]
            JOIN pg_attribute b ON b.attrelid = con.confrelid AND b.attnum = con.confkey[1]
            WHERE con.contype = 'f'
        """)
        fks = {(t, c): f"{rt}.{rc}" for t, c, rt, rc in cur.fetchall()}

    tables: dict[str, list[str]] = {}
    table_comments: dict[str, str | None] = {}
    for table, col, dtype, col_comment, table_comment in rows:
        line = f"  - {col} {dtype}"
        if (table, col) in fks:
            line += f" -> {fks[(table, col)]}"
        if col_comment:
            line += f"  -- {col_comment}"
        tables.setdefault(table, []).append(line)
        table_comments[table] = table_comment

    out = []
    for table in TABLE_ORDER:
        if table not in tables:
            continue
        header = f"TABLE {table}"
        if table_comments.get(table):
            header += f"  -- {table_comments[table]}"
        out.append(header)
        out.extend(tables[table])
        out.append("")
    return "\n".join(out).strip()


def setup_database(settings: Settings, seed: int = 7) -> dict[str, int]:
    """Create schema and roles, then load seeded synthetic data. Idempotent."""
    from .data.generate import generate

    data = generate(seed)
    with connect(settings.admin_url, autocommit=True) as conn:
        dbname = conn.info.dbname
        conn.execute(pgsql.SQL("ALTER DATABASE {} SET timezone TO 'America/Chicago'").format(pgsql.Identifier(dbname)))
        conn.execute((SQL_DIR / "schema.sql").read_text())
        for table in TABLE_ORDER:
            rows = data[table]
            if not rows:
                continue
            cols = list(rows[0].keys())
            stmt = pgsql.SQL("COPY {} ({}) FROM STDIN").format(
                pgsql.Identifier(table), pgsql.SQL(", ").join(map(pgsql.Identifier, cols))
            )
            with conn.cursor() as cur, cur.copy(stmt) as copy:
                for row in rows:
                    copy.write_row([row[c] for c in cols])
        # Keep SERIAL sequences ahead of the explicit ids we loaded.
        for table, col in [("customers", "customer_id"), ("orders", "order_id"),
                           ("order_items", "order_item_id"), ("substitution_proposals", "proposal_id")]:
            conn.execute(pgsql.SQL("SELECT setval(pg_get_serial_sequence({}, {}), COALESCE(MAX({}), 1)) FROM {}").format(
                pgsql.Literal(table), pgsql.Literal(col), pgsql.Identifier(col), pgsql.Identifier(table)))
        conn.execute((SQL_DIR / "roles.sql").read_text())
        conn.execute("ANALYZE")
    return {t: len(data[t]) for t in TABLE_ORDER}


def reset_agent_proposals(settings: Settings) -> int:
    """Remove proposals written by the agent (handy between demos)."""
    with connect(settings.admin_url, autocommit=True) as conn:
        cur = conn.execute("DELETE FROM substitution_proposals WHERE created_by = 'agent'")
        return cur.rowcount


__all__ = [
    "QueryRejected", "QueryResult", "check_sql", "connect", "describe_schema",
    "run_readonly_query", "setup_database", "reset_agent_proposals",
]
