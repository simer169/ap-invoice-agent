"""Local text embeddings (fastembed / ONNX, BAAI/bge-small-en-v1.5, 384 dims).

Nothing leaves the machine and it costs nothing. The model is downloaded once on first use.

    uv run python -m ap_agent.embeddings --backfill   # embed history rows missing an embedding
"""

import argparse
import sys
from collections.abc import Iterable, Sequence
from decimal import Decimal
from functools import lru_cache
from typing import Any

import psycopg

from ap_agent.config import get_database_url

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIMENSIONS = 384
BATCH_SIZE = 64


@lru_cache
def _model() -> Any:
    from fastembed import TextEmbedding  # heavy import, only when embeddings are needed

    return TextEmbedding(model_name=MODEL_NAME)


def embed_texts(texts: list[str]) -> list[list[float]]:
    """One 384-dim vector per text, in order."""
    if not texts:
        return []
    return [[float(x) for x in vec] for vec in _model().embed(texts)]


def to_vector_literal(vec: Sequence[float]) -> str:
    """pgvector text form, passed as a parameter and cast with %s::vector (no extra dependency)."""
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


def _qty(value: Decimal | float | str) -> str:
    d = Decimal(str(value))
    return str(d.quantize(Decimal(1)) if d == d.to_integral_value() else d.normalize())


def line_summary(lines: Iterable[tuple[str | None, str, Decimal | int | float | str]]) -> str:
    """'80 x BRG-3318 Bearing end cap, 51 x BRG-3322 ...'; same shape as the history line_summary."""
    return ", ".join(
        f"{_qty(q)} x {(p or '').strip()} {d.strip()}".replace("  ", " ") for p, d, q in lines
    )


def summary_text(vendor: str, lines_text: str, total: Decimal | str, currency: str) -> str:
    """'vendor | part numbers, descriptions and quantities | total currency'."""
    return f"{vendor.strip()} | {lines_text.strip()} | {Decimal(str(total)):.2f} {currency}"


def backfill(conn: psycopg.Connection[Any], embed: Any = embed_texts) -> int:
    """Embed vendor_invoice_history rows with no embedding. Returns rows updated; idempotent."""
    done = 0
    while True:
        rows = conn.execute(
            "SELECT h.id, v.name, h.line_summary, h.total, h.currency "
            "FROM trex.vendor_invoice_history h JOIN trex.vendors v ON v.id = h.vendor_id "
            "WHERE h.embedding IS NULL ORDER BY h.id LIMIT %s",
            (BATCH_SIZE,),
        ).fetchall()
        if not rows:
            return done
        texts = [summary_text(r[1], r[2] or "", r[3], r[4]) for r in rows]
        for row, vec in zip(rows, embed(texts), strict=True):
            conn.execute(
                "UPDATE trex.vendor_invoice_history SET embedding = %s::vector "
                "WHERE id = %s AND embedding IS NULL",
                (to_vector_literal(vec), row[0]),
            )
        done += len(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--backfill", action="store_true", help="embed history rows missing one")
    args = parser.parse_args()
    if not args.backfill:
        parser.print_help()
        return 2
    url = get_database_url()
    if not url:
        print("FAIL: DATABASE_URL is not set (see .env.example).")
        return 1
    try:
        with psycopg.connect(url, autocommit=True, connect_timeout=10) as conn:
            n = backfill(conn)
    except psycopg.Error as exc:
        print(f"FAIL backfill: {type(exc).__name__}: {str(exc).splitlines()[0]}")
        return 1
    print(f"embedded {n} history row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
