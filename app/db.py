"""PostgreSQL connection pool (psycopg 3)."""
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import DATABASE_URL

pool = ConnectionPool(
    DATABASE_URL,
    min_size=2,
    max_size=20,
    kwargs={"row_factory": dict_row, "options": "-c search_path=medmatch,public"},
    open=False,
)


def open_pool() -> None:
    pool.open(wait=True, timeout=15)


@contextmanager
def get_conn():
    with pool.connection() as conn:
        yield conn


def fetch_all(sql: str, params=None):
    with get_conn() as conn:
        return conn.execute(sql, params or ()).fetchall()


def fetch_one(sql: str, params=None):
    with get_conn() as conn:
        return conn.execute(sql, params or ()).fetchone()
