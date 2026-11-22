"""Pure, deterministic generation of the fake trex dataset (no database access).

Same seed -> identical Dataset. Every generated code starts with GEN_PREFIX so the DB layer can
find and replace exactly the rows it created.
"""

import random
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from faker import Faker
from pydantic import BaseModel, ConfigDict

from data_gen.catalog import CATEGORIES, VENDORS_PER_CATEGORY, Category

GEN_PREFIX = "GEN-"
DEMO_TODAY = date(2026, 11, 5)
SCHEDULE_FIRST = date(2026, 11, 6)
SCHEDULE_LAST = date(2026, 11, 30)

N_POS = 80
N_FULL, N_PARTIAL, N_OPEN = 32, 32, 16  # 40% / 40% / 20%
N_NEVER_COMPLETE = 4
N_USD_VENDORS = 3

Currency = Literal["CAD", "USD"]
Status = Literal["OPEN", "PARTIAL", "RECEIVED"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class Vendor(_Frozen):
    vendor_code: str
    name: str
    email_domain: str
    currency: Currency
    payment_terms_days: int
    discount_pct: Decimal
    discount_days: int
    bank_ref: str
    category: str  # generator-only; not a DB column


class PoLine(_Frozen):
    line_no: int
    part_no: str
    description: str
    qty_ordered: int
    unit_price: Decimal


class ReceiptLine(_Frozen):
    line_no: int
    qty: int


class Receipt(_Frozen):
    rr_number: str
    received_at: datetime
    received_by: str
    lines: tuple[ReceiptLine, ...]


class PurchaseOrder(_Frozen):
    po_number: str
    vendor_code: str
    order_date: date
    status: Status
    currency: Currency
    total: Decimal
    lines: tuple[PoLine, ...]
    receipts: tuple[Receipt, ...]  # inserted by build (on or before DEMO_TODAY)
    scheduled: tuple[Receipt, ...]  # future deliveries, only written to schedule.json


class HistoryInvoice(_Frozen):
    vendor_code: str
    invoice_number: str
    invoice_date: date
    currency: Currency
    total: Decimal
    line_summary: str


class Dataset(_Frozen):
    seed: int
    vendors: tuple[Vendor, ...]
    purchase_orders: tuple[PurchaseOrder, ...]
    history: tuple[HistoryInvoice, ...]

    def schedule_entries(self) -> list[dict[str, object]]:
        """Future deliveries in JSON-ready form, ordered by date then PO."""
        out: list[dict[str, object]] = []
        for po in self.purchase_orders:
            for r in po.scheduled:
                out.append(
                    {
                        "po_number": po.po_number,
                        "rr_number": r.rr_number,
                        "delivery_date": r.received_at.date().isoformat(),
                        "received_at": r.received_at.isoformat(),
                        "received_by": r.received_by,
                        "lines": [{"line_no": ln.line_no, "qty": ln.qty} for ln in r.lines],
                    }
                )
        out.sort(key=lambda e: (str(e["delivery_date"]), str(e["po_number"]), str(e["rr_number"])))
        return out


def _money(x: float) -> Decimal:
    return Decimal(str(round(x, 2))).quantize(Decimal("0.01"))


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _ts(rng: random.Random, d: date) -> datetime:
    return datetime(d.year, d.month, d.day, rng.randint(8, 15), rng.choice((0, 15, 30, 45)), tzinfo=UTC)


def _rand_date(rng: random.Random, first: date, last: date) -> date:
    return first + timedelta(days=rng.randint(0, (last - first).days))


# --------------------------------------------------------------------------------- vendors
def _make_vendors(
    rng: random.Random, fake: Faker
) -> tuple[list[Vendor], dict[str, dict[str, Decimal]], dict[str, Category]]:
    cats: list[Category] = []
    for cat, n in zip(CATEGORIES, VENDORS_PER_CATEGORY, strict=True):
        cats.extend([cat] * n)
    rng.shuffle(cats)
    n_vendors = len(cats)

    usd = set(rng.sample(range(n_vendors), N_USD_VENDORS))
    terms = ["net30"] * 10 + ["net45"] * 8 + ["2/10"] * (n_vendors - 18)
    rng.shuffle(terms)

    vendors: list[Vendor] = []
    prices: dict[str, dict[str, Decimal]] = {}
    cat_by_vendor: dict[str, Category] = {}
    names: set[str] = set()
    domains: set[str] = set()
    for i, cat in enumerate(cats):
        while True:
            last = fake.last_name()
            name = f"{last} {rng.choice(cat.suffixes)}"
            domain = f"{_slug(last)}{cat.domain_word}.example"  # reserved TLD, never a real firm
            if name not in names and domain not in domains:
                break
        names.add(name)
        domains.add(domain)
        code = f"{GEN_PREFIX}V{i + 1:03d}"
        currency: Currency = "USD" if i in usd else "CAD"
        fx = 0.75 if currency == "USD" else 1.0
        prices[code] = {
            p.part_no: _money(p.base_price * rng.uniform(0.9, 1.1) * fx) for p in cat.parts
        }
        t = terms[i]
        vendors.append(
            Vendor(
                vendor_code=code,
                name=name,
                email_domain=domain,
                currency=currency,
                payment_terms_days=45 if t == "net45" else 30,
                discount_pct=Decimal("2.00") if t == "2/10" else Decimal("0.00"),
                discount_days=10 if t == "2/10" else 0,
                bank_ref=f"BNK-****{rng.randint(1000, 9999)}",
                category=cat.key,
            )
        )
        cat_by_vendor[code] = cat
    return vendors, prices, cat_by_vendor


# ------------------------------------------------------------------------------ receiving
class _Draft:
    def __init__(self, status: Status, order_date: date, never_completes: bool) -> None:
        self.status = status
        self.order_date = order_date
        self.never_completes = never_completes


def _drafts(rng: random.Random) -> list[_Draft]:
    statuses: list[Status] = ["RECEIVED"] * N_FULL + ["PARTIAL"] * N_PARTIAL + ["OPEN"] * N_OPEN
    rng.shuffle(statuses)
    partial_idx = [i for i, s in enumerate(statuses) if s == "PARTIAL"]
    never = set(rng.sample(partial_idx, N_NEVER_COMPLETE))
    drafts: list[_Draft] = []
    for i, st in enumerate(statuses):
        if st == "RECEIVED":
            d = _rand_date(rng, date(2026, 8, 1), date(2026, 10, 22))
        elif st == "PARTIAL":
            d = _rand_date(rng, date(2026, 8, 10), date(2026, 10, 28))
        else:  # not received yet: recent orders
            d = _rand_date(rng, date(2026, 9, 25), DEMO_TODAY)
        drafts.append(_Draft(st, d, i in never))
    drafts.sort(key=lambda dr: dr.order_date)  # PO numbers follow order date
    return drafts


def _receipt(
    rng: random.Random,
    fake: Faker,
    seq: int,
    k: int,
    when: date,
    qtys: dict[int, int],
) -> Receipt:
    return Receipt(
        rr_number=f"{GEN_PREFIX}RR-{seq:04d}-{k}",
        received_at=_ts(rng, when),
        received_by=fake.name(),
        lines=tuple(ReceiptLine(line_no=n, qty=q) for n, q in sorted(qtys.items()) if q > 0),
    )


def _full_receipts(
    rng: random.Random, fake: Faker, seq: int, lines: tuple[PoLine, ...], order_date: date
) -> list[Receipt]:
    first = min(order_date + timedelta(days=rng.randint(3, 14)), DEMO_TODAY)
    if rng.random() < 0.25 and first + timedelta(days=2) <= DEMO_TODAY:  # delivered in two parts
        part1 = {ln.line_no: ln.qty_ordered // 2 for ln in lines}
        part2 = {ln.line_no: ln.qty_ordered - ln.qty_ordered // 2 for ln in lines}
        second = min(first + timedelta(days=rng.randint(2, 10)), DEMO_TODAY)
        return [
            _receipt(rng, fake, seq, 1, first, part1),
            _receipt(rng, fake, seq, 2, second, part2),
        ]
    return [_receipt(rng, fake, seq, 1, first, {ln.line_no: ln.qty_ordered for ln in lines})]


def _partial_receipts(
    rng: random.Random,
    fake: Faker,
    seq: int,
    lines: tuple[PoLine, ...],
    order_date: date,
    never_completes: bool,
) -> tuple[list[Receipt], list[Receipt]]:
    got: dict[int, int] = {}
    for ln in lines:
        r = rng.random()
        if r < 0.45:
            got[ln.line_no] = ln.qty_ordered
        elif r < 0.80:  # short
            got[ln.line_no] = min(ln.qty_ordered - 1, max(1, int(ln.qty_ordered * rng.uniform(0.3, 0.8))))
        else:
            got[ln.line_no] = 0
    first_line = lines[0].line_no
    if all(got[ln.line_no] >= ln.qty_ordered for ln in lines):  # need at least one short line
        got[first_line] = max(1, lines[0].qty_ordered // 2)
    if not any(q > 0 for q in got.values()):  # need at least one received line
        got[first_line] = max(1, lines[0].qty_ordered // 2)
    when = min(order_date + timedelta(days=rng.randint(3, 10)), DEMO_TODAY)
    receipts = [_receipt(rng, fake, seq, 1, when, got)]

    remaining = {ln.line_no: ln.qty_ordered - got[ln.line_no] for ln in lines}
    remaining = {n: q for n, q in remaining.items() if q > 0}
    if never_completes or not remaining:
        return receipts, []

    window = (SCHEDULE_LAST - SCHEDULE_FIRST).days + 1
    first_half = {n: q // 2 for n, q in remaining.items()}
    if rng.random() < 0.35 and any(first_half.values()):
        d1, d2 = sorted(rng.sample(range(window), 2))
        second_half = {n: remaining[n] - first_half[n] for n in remaining}
        batches = [(d1, first_half), (d2, second_half)]
    else:
        batches = [(rng.randrange(window), remaining)]
    scheduled = [
        _receipt(rng, fake, seq, 2 + j, SCHEDULE_FIRST + timedelta(days=d), q)
        for j, (d, q) in enumerate(batches)
    ]
    return receipts, scheduled


# ------------------------------------------------------------------------------------ POs
def _make_pos(
    rng: random.Random,
    fake: Faker,
    vendors: list[Vendor],
    prices: dict[str, dict[str, Decimal]],
    cat_by_vendor: dict[str, Category],
) -> list[PurchaseOrder]:
    pos: list[PurchaseOrder] = []
    for seq, dr in enumerate(_drafts(rng)[:N_POS], start=1):
        vendor = rng.choice(vendors)
        cat = cat_by_vendor[vendor.vendor_code]
        chosen = sorted(rng.sample(cat.parts, rng.randint(2, 8)), key=lambda p: p.part_no)
        lines = tuple(
            PoLine(
                line_no=n,
                part_no=p.part_no,
                description=p.description,
                qty_ordered=rng.randint(*cat.qty_range),
                unit_price=prices[vendor.vendor_code][p.part_no],
            )
            for n, p in enumerate(chosen, start=1)
        )
        total = sum((ln.unit_price * ln.qty_ordered for ln in lines), Decimal("0.00"))
        receipts: list[Receipt] = []
        scheduled: list[Receipt] = []
        if dr.status == "RECEIVED":
            receipts = _full_receipts(rng, fake, seq, lines, dr.order_date)
        elif dr.status == "PARTIAL":
            receipts, scheduled = _partial_receipts(
                rng, fake, seq, lines, dr.order_date, dr.never_completes
            )
        pos.append(
            PurchaseOrder(
                po_number=f"{GEN_PREFIX}PO-{seq:04d}",
                vendor_code=vendor.vendor_code,
                order_date=dr.order_date,
                status=dr.status,
                currency=vendor.currency,
                total=total.quantize(Decimal("0.01")),
                lines=lines,
                receipts=tuple(receipts),
                scheduled=tuple(scheduled),
            )
        )
    return pos


# --------------------------------------------------------------------------------- history
def _make_history(
    rng: random.Random, vendors: list[Vendor], cat_by_vendor: dict[str, Category]
) -> list[HistoryInvoice]:
    out: list[HistoryInvoice] = []
    start = DEMO_TODAY - timedelta(days=364)  # 12 months back
    span = (DEMO_TODAY - start).days
    for v in vendors:
        cat = cat_by_vendor[v.vendor_code]
        avg_price = sum(p.base_price for p in cat.parts) / len(cat.parts)
        avg_qty = sum(cat.qty_range) / 2
        mean = avg_price * avg_qty * 3.5 * rng.uniform(0.6, 1.4) * (0.75 if v.currency == "USD" else 1)
        n = rng.randint(8, 20)
        days = sorted(rng.sample(range(span + 1), n))
        for i, offset in enumerate(days, start=1):
            total = max(mean * 0.25, rng.gauss(mean, mean * 0.2))
            picks = rng.sample(cat.parts, rng.randint(1, 4))
            summary = ", ".join(f"{rng.randint(2, 40)} x {p.part_no} {p.description}" for p in picks)
            out.append(
                HistoryInvoice(
                    vendor_code=v.vendor_code,
                    invoice_number=f"{GEN_PREFIX}INV-{v.vendor_code[-3:]}-{i:04d}",
                    invoice_date=start + timedelta(days=offset),
                    currency=v.currency,
                    total=_money(total),
                    line_summary=summary,
                )
            )
    return out


def generate(seed: int = 42) -> Dataset:
    """Build the whole fake dataset deterministically from `seed`."""
    rng = random.Random(seed)
    fake = Faker("en_CA")
    fake.seed_instance(seed)
    vendors, prices, cat_by_vendor = _make_vendors(rng, fake)
    pos = _make_pos(rng, fake, vendors, prices, cat_by_vendor)
    history = _make_history(rng, vendors, cat_by_vendor)
    return Dataset(
        seed=seed,
        vendors=tuple(vendors),
        purchase_orders=tuple(pos),
        history=tuple(history),
    )
