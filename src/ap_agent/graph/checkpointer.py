"""Postgres checkpointer on DATABASE_URL, with its tables kept in the `langgraph` schema."""

from typing import Any

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA = "langgraph"


def _use_schema(conn: psycopg.Connection[Any]) -> None:
    """Point the session at schema langgraph.

    A startup `options=-csearch_path=...` is NOT used: the Supabase pooler drops startup options,
    which silently put the tables in public.
    """
    conn.execute(f"SET search_path TO {SCHEMA}")


def make_pool(url: str, max_size: int = 4) -> ConnectionPool[Any]:
    """Pool suited to the Supabase session pooler: few, short-lived, health-checked connections.

    PostgresSaver requires autocommit, dict rows and no server-side prepared statements.
    """
    return ConnectionPool(
        url,
        min_size=1,
        max_size=max_size,
        max_lifetime=600,
        check=ConnectionPool.check_connection,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "connect_timeout": 10,
        },
        configure=_use_schema,
        open=True,
    )


def make_checkpointer(pool: ConnectionPool[Any]) -> PostgresSaver:
    return PostgresSaver(pool)  # type: ignore[arg-type]


def setup_checkpointer(url: str) -> None:
    """Create the checkpoint tables in schema langgraph (idempotent)."""
    with psycopg.connect(
        url,
        autocommit=True,
        prepare_threshold=0,
        row_factory=dict_row,  # type: ignore[arg-type]
        connect_timeout=10,
    ) as conn:
        conn.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        _use_schema(conn)
        PostgresSaver(conn).setup()  # type: ignore[arg-type]
