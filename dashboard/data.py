"""Data access for the dashboard: cached wrappers around ap_agent.ui_queries (no SQL here)."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

import psycopg
import streamlit as st

from ap_agent import ui_queries as q
from ap_agent.config import get_database_url, get_settings, get_ui_database_url
from ap_agent.extract.normalize import DateOrder
from ap_agent.mail.storage import default_storage

TTL_SECONDS = 30


@contextmanager
def _conn() -> Iterator[psycopg.Connection]:
    url = get_ui_database_url()
    if not url:
        raise RuntimeError("DATABASE_URL (or UI_DATABASE_URL) is not set; see .env.example.")
    with psycopg.connect(url, connect_timeout=10) as conn:
        conn.read_only = True  # the dashboard never writes through this connection
        yield conn


def _date_order() -> DateOrder:
    return get_settings().DATE_ORDER


@st.cache_data(ttl=TTL_SECONDS, show_spinner=False)
def inbox_counts() -> dict[str, int]:
    with _conn() as c:
        return q.inbox_counts(c)


@st.cache_data(ttl=TTL_SECONDS, show_spinner=False)
def list_inbox(outcome: str | None = None) -> list[q.InboxRow]:
    with _conn() as c:
        return q.list_inbox(c, outcome)


@st.cache_data(ttl=TTL_SECONDS, show_spinner=False)
def list_extracted_invoices() -> list[q.InvoiceRow]:
    with _conn() as c:
        return q.list_extracted_invoices(c, _date_order())


@st.cache_data(ttl=TTL_SECONDS, show_spinner=False)
def list_po_statuses() -> list[str]:
    with _conn() as c:
        return q.list_po_statuses(c)


@st.cache_data(ttl=TTL_SECONDS, show_spinner=False)
def list_vendor_options() -> list[q.VendorOption]:
    with _conn() as c:
        return q.list_vendor_options(c)


@st.cache_data(ttl=TTL_SECONDS, show_spinner=False)
def list_purchase_orders(
    status: str | None = None, vendor_id: int | None = None
) -> list[q.POSummary]:
    with _conn() as c:
        return q.list_purchase_orders(c, status, vendor_id)


@st.cache_data(ttl=TTL_SECONDS, show_spinner=False)
def get_po_detail(po_number: str) -> q.PODetail | None:
    with _conn() as c:
        return q.get_po_detail(c, po_number, _date_order())


@st.cache_data(ttl=TTL_SECONDS, show_spinner=False)
def list_vendors() -> list[q.VendorRow]:
    with _conn() as c:
        return q.list_vendors(c)


def pdf_url(s3_key: str) -> str:
    """Fresh 10-minute presigned URL. Deliberately not cached, never displayed as text."""
    return default_storage().presigned_url(s3_key, expires=600)


def demo_mode() -> bool:
    return get_settings().DEMO_MODE


def demo_today() -> date | None:
    try:
        from data_gen.generate import DEMO_TODAY
    except ImportError:
        return None
    return DEMO_TODAY


def simulate_day(day: date) -> list[str]:
    """Dev only (DEMO_MODE): apply scheduled deliveries up to `day`. The one writing path."""
    if not demo_mode():
        raise PermissionError("DEMO_MODE is not enabled")
    from data_gen.simulate import apply_day, load_schedule

    url = get_database_url()
    if not url:
        raise RuntimeError("DATABASE_URL is not set")
    with psycopg.connect(url, connect_timeout=10) as conn:
        return apply_day(conn, day, load_schedule())
