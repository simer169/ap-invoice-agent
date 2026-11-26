"""Pure planner: turn the generated Dataset into 60 invoice specs (scenario, numbers, dates).

Uses its own RNG stream (`random.Random(f"{seed}-docs")`), so the Phase 3a data is untouched.
Amounts are the PO's pre-tax totals plus 13% HST for CAD vendors (none for USD).
"""

import random
import re
import statistics
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from faker import Faker
from pydantic import BaseModel

from data_gen.catalog import CATEGORIES
from data_gen.generate import DEMO_TODAY, Dataset, PurchaseOrder

BILL_TO = "Demo Rail Leasing Co."
AP_ADDRESS = "ap@demorail.example"
HST_RATE = Decimal("0.13")
EMAIL_FIRST = date(2026, 11, 1)

COUNTS: dict[str, int] = {
    "clean": 14,
    "partial_completes": 16,
    "partial_never": 4,
    "exact_duplicate": 5,
    "reissue": 3,
    "price_above_po": 5,
    "amount_outlier": 3,
    "unknown_vendor": 3,
    "no_po": 3,
    "scanned": 4,
}
OUTCOMES: dict[str, str] = {
    "clean": "READY",
    "partial_completes": "PARK_THEN_READY",
    "partial_never": "STAYS_WAITING",
    "exact_duplicate": "REJECT_DUPLICATE",
    "reissue": "EXCEPTION_REISSUE",
    "price_above_po": "ANOMALY_PRICE",
    "amount_outlier": "ANOMALY_AMOUNT",
    "unknown_vendor": "EXCEPTION_UNKNOWN_VENDOR",
    "no_po": "EXCEPTION_NO_PO",
}
SCANNED_UNDERLYING = ("clean", "partial_completes", "price_above_po", "no_po")
BASE_SCENARIOS = tuple(OUTCOMES)


class VendorInfo(BaseModel):
    name: str
    email_domain: str
    address: list[str]
    phone: str
    currency: Literal["CAD", "USD"]
    payment_terms: str
    layout: int  # 0, 1, 2 -> pdf layouts A, B, C
    known: bool  # present in trex.vendors
    code: str | None
    number_style: int
    abbr: str


class Line(BaseModel):
    part_no: str
    description: str
    qty: int
    unit_price: Decimal
    line_total: Decimal


class InvoiceSpec(BaseModel):
    id: str = ""
    key: int  # planning key, stable before ids are assigned
    scenario: str
    underlying_scenario: str | None = None
    expected_outcome: str
    ready_day: date | None = None
    scanned: bool = False
    vendor: VendorInfo
    invoice_number: str
    invoice_date: date
    po_number: str | None
    source_po: str | None
    lines: list[Line]
    subtotal: Decimal
    tax: Decimal
    total: Decimal
    layout: int
    email_dt: datetime
    duplicate_of: int | None = None  # key of the original
    reissue_of: int | None = None
    reissue_change: str | None = None
    anomaly: dict[str, Any] | None = None
    split: str = "eval"
    subject: str = ""
    message_id: str = ""
    pdf_file: str = ""
    eml_file: str = ""
    attachment_name: str = ""


def money(x: Decimal | float) -> Decimal:
    return Decimal(x).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _totals(lines: list[Line], currency: str) -> tuple[Decimal, Decimal, Decimal]:
    subtotal = sum((ln.line_total for ln in lines), Decimal("0.00"))
    tax = money(subtotal * HST_RATE) if currency == "CAD" else Decimal("0.00")
    return subtotal, tax, subtotal + tax


class _Planner:
    def __init__(self, ds: Dataset, seed: int) -> None:
        self.ds = ds
        self.seed = seed
        self.rng = random.Random(f"{seed}-docs")
        self.fake_ca = Faker("en_CA")
        self.fake_us = Faker("en_US")
        self.fake_ca.seed_instance(self.rng.getrandbits(32))
        self.fake_us.seed_instance(self.rng.getrandbits(32))
        self.vendors: dict[str, VendorInfo] = {}
        self.next_number: dict[str, int] = {}
        self.specs: list[InvoiceSpec] = []
        self._key = 0
        self.po_by_number = {p.po_number: p for p in ds.purchase_orders}
        for v in ds.vendors:
            terms = (
                f"{v.discount_pct.normalize():f}/{v.discount_days} Net {v.payment_terms_days}"
                if v.discount_days
                else f"Net {v.payment_terms_days}"
            )
            self.vendors[v.vendor_code] = self._vendor_info(
                v.name, v.email_domain, v.currency, terms, True, v.vendor_code
            )

    # --------------------------------------------------------------------------- vendors
    def _vendor_info(
        self,
        name: str,
        domain: str,
        currency: str,
        terms: str,
        known: bool,
        code: str | None,
    ) -> VendorInfo:
        fake = self.fake_us if currency == "USD" else self.fake_ca
        street = fake.street_address()
        city = fake.city()
        if currency == "USD":
            region = f"{city}, {fake.state_abbr()} {fake.postcode()}"
        else:
            region = f"{city}, {fake.province_abbr()} {fake.postcode()}"
        words = [w for w in re.findall(r"[A-Za-z]+", name) if w[0].isupper()]
        return VendorInfo(
            name=name,
            email_domain=domain,
            address=[street, region, "Canada" if currency == "CAD" else "USA"],
            phone=fake.phone_number(),
            currency=currency,  # type: ignore[arg-type]
            payment_terms=terms,
            layout=self.rng.randrange(3),
            known=known,
            code=code,
            number_style=self.rng.randrange(3),
            abbr=(words[0][:2] if words else "IN").upper(),
        )

    def _invented_vendors(self, n: int) -> list[VendorInfo]:
        taken = {v.name for v in self.ds.vendors}
        taken_dom = {v.email_domain for v in self.ds.vendors}
        out: list[VendorInfo] = []
        for i in range(n):
            while True:
                last = self.fake_ca.last_name()
                cat = self.rng.choice(CATEGORIES)
                name = f"{last} {self.rng.choice(cat.suffixes)}"
                domain = f"{_slug(last).replace('-', '')}-{cat.domain_word}.example"
                if name not in taken and domain not in taken_dom:
                    break
            taken.add(name)
            taken_dom.add(domain)
            currency = "USD" if i == n - 1 else "CAD"
            terms = self.rng.choice(["Net 30", "Net 45", "2/10 Net 30"])
            out.append(self._vendor_info(name, domain, currency, terms, False, None))
        return out

    def invoice_number(self, v: VendorInfo) -> str:
        k = v.code or v.name
        n = self.next_number.get(k)
        n = self.rng.randint(10000, 40000) if n is None else n + self.rng.randint(1, 37)
        self.next_number[k] = n
        if v.number_style == 0:
            return str(n)
        if v.number_style == 1:
            return f"INV-{n}"
        return f"{v.abbr}-26-{n % 10000:04d}"

    # ---------------------------------------------------------------------------- specs
    def new(
        self,
        scenario: str,
        vendor: VendorInfo,
        lines: list[Line],
        po_number: str | None,
        source_po: str | None,
        order_date: date | None,
        *,
        underlying: str | None = None,
    ) -> InvoiceSpec:
        email_day = EMAIL_FIRST + timedelta(days=self.rng.randrange(5))
        inv_date = email_day - timedelta(days=self.rng.randint(0, 2))
        if order_date is not None:
            inv_date = max(inv_date, order_date)
        email_dt = datetime(
            email_day.year, email_day.month, email_day.day,
            self.rng.randint(7, 17), self.rng.randint(0, 59),
        )  # fmt: skip
        subtotal, tax, total = _totals(lines, vendor.currency)
        outcome_of = underlying or scenario
        self._key += 1
        spec = InvoiceSpec(
            key=self._key,
            scenario=scenario,
            underlying_scenario=underlying,
            expected_outcome=OUTCOMES[outcome_of],
            scanned=scenario == "scanned",
            vendor=vendor,
            invoice_number=self.invoice_number(vendor),
            invoice_date=inv_date,
            po_number=po_number,
            source_po=source_po,
            lines=lines,
            subtotal=subtotal,
            tax=tax,
            total=total,
            layout=vendor.layout,
            email_dt=email_dt,
        )
        self.specs.append(spec)
        return spec

    @staticmethod
    def po_lines(po: PurchaseOrder) -> list[Line]:
        return [
            Line(
                part_no=ln.part_no,
                description=ln.description,
                qty=ln.qty_ordered,
                unit_price=ln.unit_price,
                line_total=money(ln.unit_price * ln.qty_ordered),
            )
            for ln in po.lines
        ]

    def from_po(
        self, scenario: str, po: PurchaseOrder, *, underlying: str | None = None
    ) -> InvoiceSpec:
        base = underlying or scenario
        spec = self.new(
            scenario,
            self.vendors[po.vendor_code],
            self.po_lines(po),
            None if base == "no_po" else po.po_number,
            po.po_number if base == "no_po" else None,
            po.order_date,
            underlying=underlying,
        )
        if base == "partial_completes":
            spec.ready_day = max(r.received_at.date() for r in po.scheduled)
        if base == "price_above_po":
            self._inflate_price(spec)
        return spec

    def _inflate_price(self, spec: InvoiceSpec) -> None:
        candidates = [i for i, ln in enumerate(spec.lines) if ln.unit_price >= 20]
        idx = (
            self.rng.choice(candidates)
            if candidates
            else max(range(len(spec.lines)), key=lambda i: spec.lines[i].unit_price)
        )
        old = spec.lines[idx]
        while True:
            pct = Decimal(str(round(self.rng.uniform(3.2, 9.8), 2)))
            new_price = money(old.unit_price * (1 + pct / 100))
            actual = (new_price / old.unit_price - 1) * 100
            if Decimal(3) <= actual <= Decimal(10):
                break
        spec.lines[idx] = old.model_copy(
            update={"unit_price": new_price, "line_total": money(new_price * old.qty)}
        )
        spec.subtotal, spec.tax, spec.total = _totals(spec.lines, spec.vendor.currency)
        spec.anomaly = {
            "type": "price_above_po",
            "line_no": idx + 1,
            "part_no": old.part_no,
            "po_unit_price": str(old.unit_price),
            "invoice_unit_price": str(new_price),
            "pct_over": f"{actual:.2f}",
        }

    def from_po_amount(self, po: PurchaseOrder, stats: tuple[float, float, float]) -> None:
        mean, std, hist_max = stats
        spec = self.from_po("amount_outlier", po)
        floor = Decimal(str(round(3 * hist_max, 2)))
        fee_added = False
        if spec.total < floor:
            rate = Decimal(1) + (HST_RATE if spec.vendor.currency == "CAD" else Decimal(0))
            fee = money((floor - spec.total) / rate) + Decimal("1.00")
            spec.lines.append(
                Line(
                    part_no="SVC-EXP",
                    description="Expedite and special handling charge",
                    qty=1,
                    unit_price=fee,
                    line_total=fee,
                )
            )
            spec.subtotal, spec.tax, spec.total = _totals(spec.lines, spec.vendor.currency)
            fee_added = True
        spec.anomaly = {
            "type": "amount_outlier",
            "vendor_history_mean": f"{mean:.2f}",
            "vendor_history_std": f"{std:.2f}",
            "vendor_history_max": f"{hist_max:.2f}",
            "fee_line_added": fee_added,
        }

    # --------------------------------------------------------------------------- plan
    def run(self) -> list[InvoiceSpec]:
        ds, rng = self.ds, self.rng
        received = [p for p in ds.purchase_orders if p.status == "RECEIVED"]
        partial = [p for p in ds.purchase_orders if p.status == "PARTIAL"]
        sched = [p for p in partial if p.scheduled]
        never = [p for p in partial if not p.scheduled]
        rng.shuffle(received)
        rng.shuffle(sched)

        # amount outliers first: highest invoice total vs the vendor's own history
        hist: dict[str, list[float]] = defaultdict(list)
        for h in ds.history:
            hist[h.vendor_code].append(float(h.total))

        def z(po: PurchaseOrder) -> float:
            xs = hist[po.vendor_code]
            tax = 1.13 if po.currency == "CAD" else 1.0
            return (float(po.total) * tax - statistics.mean(xs)) / statistics.pstdev(xs)

        outliers = sorted(received, key=lambda p: (-z(p), p.po_number))[:3]
        received = [p for p in received if p not in outliers]
        for po in outliers:
            xs = hist[po.vendor_code]
            self.from_po_amount(po, (statistics.mean(xs), statistics.pstdev(xs), max(xs)))

        def take(pool: list[PurchaseOrder], n: int) -> list[PurchaseOrder]:
            got = pool[:n]
            del pool[:n]
            return got

        clean = [self.from_po("clean", po) for po in take(received, COUNTS["clean"])]
        for po in take(received, COUNTS["price_above_po"]):
            self.from_po("price_above_po", po)
        for po in take(received, COUNTS["no_po"]):
            self.from_po("no_po", po)
        for po in take(sched, COUNTS["partial_completes"]):
            self.from_po("partial_completes", po)
        for po in never:
            self.from_po("partial_never", po)

        # scanned: extra invoices whose outcome is that of the underlying case
        for under in SCANNED_UNDERLYING:
            pool = sched if under == "partial_completes" else received
            self.from_po("scanned", take(pool, 1)[0], underlying=under)

        # unknown vendors: invented vendor + invented PO number, parts from any category
        for vinfo in self._invented_vendors(COUNTS["unknown_vendor"]):
            cat = rng.choice(CATEGORIES)
            fx = Decimal("0.75") if vinfo.currency == "USD" else Decimal(1)
            lines: list[Line] = []
            for part in rng.sample(cat.parts, rng.randint(2, 5)):
                qty = rng.randint(*cat.qty_range)
                price = money(
                    Decimal(str(part.base_price)) * Decimal(str(rng.uniform(0.9, 1.1))) * fx
                )
                lines.append(
                    Line(
                        part_no=part.part_no,
                        description=part.description,
                        qty=qty,
                        unit_price=price,
                        line_total=money(price * qty),
                    )
                )
            self.new(
                "unknown_vendor", vinfo, lines, f"GEN-PO-{rng.randint(9000, 9999)}", None, None
            )

        self._duplicates_and_reissues(clean)
        return self._finalize()

    def _duplicates_and_reissues(self, clean: list[InvoiceSpec]) -> None:
        rng = self.rng
        originals = rng.sample(clean, COUNTS["exact_duplicate"] + COUNTS["reissue"])
        for o in originals:  # originals arrive early so the second email can follow
            o.email_dt = o.email_dt.replace(day=rng.randint(1, 3))
            o.invoice_date = min(o.invoice_date, o.email_dt.date())
        dup_src, re_src = (
            originals[: COUNTS["exact_duplicate"]],
            originals[COUNTS["exact_duplicate"] :],
        )
        for o in dup_src:
            self._key += 1
            later = o.email_dt + timedelta(days=rng.randint(1, 2), minutes=rng.randint(5, 600))
            self.specs.append(
                o.model_copy(
                    deep=True,
                    update={
                        "key": self._key,
                        "scenario": "exact_duplicate",
                        "expected_outcome": OUTCOMES["exact_duplicate"],
                        "email_dt": later,
                        "duplicate_of": o.key,
                        "split": "eval",
                    },
                )
            )
        changes = ["date", "layout", rng.choice(["date", "layout"])]
        for o, change in zip(re_src, changes, strict=True):
            self._key += 1
            inv_date, layout = o.invoice_date, o.layout
            if change == "date":
                inv_date = o.invoice_date + timedelta(days=rng.randint(1, 2))
            else:
                layout = (o.layout + rng.randint(1, 2)) % 3
            email_day = max(o.email_dt.date() + timedelta(days=1), inv_date)
            later = datetime(
                email_day.year,
                email_day.month,
                email_day.day,
                rng.randint(7, 17),
                rng.randint(0, 59),
            )
            self.specs.append(
                o.model_copy(
                    deep=True,
                    update={
                        "key": self._key,
                        "scenario": "reissue",
                        "expected_outcome": OUTCOMES["reissue"],
                        "invoice_date": inv_date,
                        "layout": layout,
                        "email_dt": later,
                        "reissue_of": o.key,
                        "reissue_change": change,
                    },
                )
            )

    def _finalize(self) -> list[InvoiceSpec]:
        rng = self.rng
        specs = sorted(self.specs, key=lambda s: (s.email_dt, s.key))
        id_of = {s.key: f"INV-{i:03d}" for i, s in enumerate(specs, start=1)}
        mid_rng = random.Random(f"{self.seed}-message-ids")
        for i, s in enumerate(specs, start=1):
            s.id = id_of[s.key]
            s.message_id = (
                f"<{uuid.UUID(int=mid_rng.getrandbits(128), version=4)}@{s.vendor.email_domain}>"
            )
            s.attachment_name = f"Invoice_{re.sub(r'[^A-Za-z0-9]+', '_', s.invoice_number)}.pdf"
            s.pdf_file = f"gen_{i:03d}_{_slug(s.invoice_number)}.pdf"
            s.eml_file = f"gen_{i:03d}_{_slug(s.invoice_number)}.eml"
            forms = [f"Invoice {s.invoice_number}", f"{s.invoice_number} attached"]
            if s.invoice_number[0].isdigit():
                forms[1] = f"INV-{s.invoice_number} attached"
            if s.po_number:
                forms.append(f"Billing for PO {s.po_number}")
            s.subject = rng.choice(forms)
        # dev split: one of each base scenario (non-scanned) + one scanned
        by_scn: dict[str, list[InvoiceSpec]] = defaultdict(list)
        for s in specs:
            by_scn[s.scenario].append(s)
        dev = [rng.choice(by_scn[scn]) for scn in BASE_SCENARIOS]
        dev.append(rng.choice(by_scn["scanned"]))
        for s in dev:
            s.split = "dev"
        for s in specs:
            if s.split != "dev":
                s.split = "eval"
        return specs


def plan(ds: Dataset, seed: int) -> list[InvoiceSpec]:
    """Plan all 60 invoices, ordered by email time, with ids/files/message-ids assigned."""
    specs = _Planner(ds, seed).run()
    assert date(2026, 11, 1) <= min(s.email_dt.date() for s in specs)
    assert max(s.email_dt.date() for s in specs) <= DEMO_TODAY
    return specs
