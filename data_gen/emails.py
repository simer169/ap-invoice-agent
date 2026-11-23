"""Build the .eml files: invoice mails (PDF attached) and non-invoice mails the watcher ignores."""

import random
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime

from pydantic import BaseModel

from data_gen.generate import Dataset
from data_gen.scenarios import AP_ADDRESS, BILL_TO, InvoiceSpec

TZ = timezone(timedelta(hours=-5))
EMAIL_FIRST = datetime(2026, 11, 1)


def _base(sender_name: str, domain: str, subject: str, when: datetime, message_id: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = f"{sender_name} <ar@{domain}>"
    msg["To"] = f"Accounts Payable <{AP_ADDRESS}>"
    msg["Subject"] = subject
    msg["Date"] = format_datetime(when.replace(tzinfo=TZ))
    msg["Message-ID"] = message_id
    return msg


def invoice_email(spec: InvoiceSpec, pdf: bytes) -> bytes:
    v = spec.vendor
    msg = _base(f"{v.name} Billing", v.email_domain, spec.subject, spec.email_dt, spec.message_id)
    po = f" for PO {spec.po_number}" if spec.po_number else ""
    msg.set_content(
        f"Hello,\n\nPlease find attached invoice {spec.invoice_number}{po}.\n"
        f"Payment terms: {v.payment_terms}.\n\nRegards,\n{v.name}\nAccounts Receivable\n"
    )
    msg.add_attachment(
        pdf, maintype="application", subtype="pdf", filename=spec.attachment_name
    )
    msg.set_boundary(f"==demo-{uuid.uuid5(uuid.NAMESPACE_URL, spec.message_id).hex}==")
    return msg.as_bytes()


class NonInvoice(BaseModel):
    id: str
    kind: str
    eml_file: str
    message_id: str
    subject: str
    sender: str
    expected_outcome: str = "IGNORE"


_KINDS = (
    ("statement", "Monthly account statement - October 2026",
     "Attached is your statement summary. Balance forward and open items are listed in the portal.\n"),
    ("order_confirmation", "Order confirmation for your purchase order",
     "Thank you for your order. We have received it and will confirm ship dates shortly.\n"
     + f"Customer: {BILL_TO}\n"),
    ("shipping_notice", "Shipping notice - your order is on its way",
     "Your parts shipped today by freight. Tracking details are in the portal.\n"),
    ("po_acknowledgment", "PO acknowledgment",
     "We acknowledge your PO and confirm pricing as quoted. This is not an invoice.\n"),
    ("newsletter", "Winter maintenance season: parts availability update",
     "Our latest newsletter covers lead times and holiday shipping hours.\n"),
)


def non_invoice_emails(ds: Dataset, seed: int) -> list[tuple[NonInvoice, bytes]]:
    rng = random.Random(f"{seed}-noninvoice")
    out: list[tuple[NonInvoice, bytes]] = []
    for i, (kind, subject, body) in enumerate(_KINDS, start=1):
        vendor = rng.choice(ds.vendors)
        when = EMAIL_FIRST + timedelta(days=rng.randrange(5), hours=rng.randint(7, 17), minutes=rng.randint(0, 59))
        mid = f"<{uuid.UUID(int=rng.getrandbits(128), version=4)}@{vendor.email_domain}>"
        msg = _base(vendor.name, vendor.email_domain, subject, when, mid)
        msg.set_content(f"Hello,\n\n{body}\nRegards,\n{vendor.name}\n")
        meta = NonInvoice(
            id=f"NI-{i:02d}",
            kind=kind,
            eml_file=f"gen_ni_{i:02d}_{kind}.eml",
            message_id=mid,
            subject=subject,
            sender=f"ar@{vendor.email_domain}",
        )
        out.append((meta, msg.as_bytes()))
    return out
