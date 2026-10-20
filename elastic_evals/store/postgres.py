"""Postgres configuration store (DESIGN.md §5.4): a thin layer over psycopg with migrations."""

import os
from contextlib import contextmanager
from importlib import resources

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool


class Postgres:
    """Thread-safe: every call borrows a connection from a small pool (API requests run in threads)."""

    def __init__(self, dsn: str | None = None, max_size: int = 10):
        self.dsn = dsn or os.environ.get("EE_POSTGRES_DSN", "postgresql://postgres@localhost:5432/elastic_evals")
        self.pool = ConnectionPool(self.dsn, min_size=1, max_size=max_size, open=True,
                                   kwargs={"autocommit": True, "row_factory": dict_row})

    @contextmanager
    def tx(self):
        with self.pool.connection() as conn, conn.transaction():
            yield conn

    def all(self, sql: str, *params):
        with self.pool.connection() as conn:
            return conn.execute(sql, params).fetchall()

    def one(self, sql: str, *params):
        with self.pool.connection() as conn:
            return conn.execute(sql, params).fetchone()

    def exec(self, sql: str, *params) -> int:
        with self.pool.connection() as conn:
            return conn.execute(sql, params).rowcount

    def migrate(self):
        """Apply schema/NNN_*.sql files in order, once each."""
        self.exec("CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, applied_at timestamptz DEFAULT now())")
        done = {r["name"] for r in self.all("SELECT name FROM schema_migrations")}
        files = sorted(f for f in resources.files("elastic_evals.store").joinpath("schema").iterdir()
                       if f.name.endswith(".sql"))
        for f in files:
            if f.name not in done:
                with self.tx() as c:
                    c.execute(f.read_text())
                    c.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))

    def audit(self, actor: str, action: str, target: str | None = None, tenant_id: str | None = None, **detail):
        self.exec("INSERT INTO audit_log (tenant_id, actor, action, target, detail) VALUES (%s, %s, %s, %s, %s)",
                  tenant_id, actor, action, target, Jsonb(detail))

    def close(self):
        self.pool.close()


def create_database(dsn: str):
    """Create the database named in `dsn` if it does not exist (dev and tests)."""
    base, name = dsn.rsplit("/", 1)
    with psycopg.connect(base + "/postgres", autocommit=True) as c:
        if not c.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            c.execute(f'CREATE DATABASE "{name}"')


__all__ = ["Postgres", "Jsonb", "create_database"]
