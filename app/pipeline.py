"""Runs every engine in dependency order. Idempotent: safe to re-run after each import."""
from datetime import date

from sqlalchemy.orm import Session

from app.engines.ledger import rebuild_ledger
from app.engines.quickcommerce import reconcile_quick_commerce
from app.engines.recovery import detect_leaks
from app.engines.tax import reconcile_tax_credits


def run_all(s: Session, brand_id: int, as_of: date | None = None) -> dict:
    as_of = as_of or date.today()
    qc = reconcile_quick_commerce(s, brand_id, as_of)
    tax = reconcile_tax_credits(s, brand_id, as_of)
    claims = detect_leaks(s, brand_id, as_of, extra={**qc["findings"], **tax["findings"]})
    ledger = rebuild_ledger(s, brand_id)
    s.commit()
    return {"as_of": as_of.isoformat(), **claims, **ledger}
