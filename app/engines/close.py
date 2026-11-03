"""Gap 8 (and the month-end half of gap 1) — close the month and stay audit-ready.

close_checklist(): automated checks for a period with pass/warn/fail.
lock_period(): freezes the period (ledger never re-posts it) and stores a snapshot.
audit_pack(): ZIP of every schedule an auditor or investor asks for.
"""
import csv
import io
import json
import zipfile
from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engines.capital import borrowing_base
from app.engines.common import channels_by_id, period_bounds, r2
from app.engines.ledger import balances, journal_rows, profit_and_loss, tally_xml, trial_balance
from app.engines.profitability import sku_profitability
from app.engines.recovery import claim_dict, reconcile_orders, settlement_to_bank
from app.engines.tax import reconcile_tax_credits
from app.models import (Brand, Claim, ClosePeriod, LogisticsShipment, Order, PurchaseOrder, QcInvoice,
                        QcPayment, SettlementLine)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

EXCEPTION_STATUSES = {"unsettled_overdue", "short_paid", "cod_not_remitted", "fees_on_cancelled"}


def _check(key, label, status, detail, value=None):
    return {"key": key, "label": label, "status": status, "detail": detail, "value": value}


def close_checklist(s: Session, brand_id: int, period: str, as_of: date) -> dict:
    start, end = period_bounds(period)
    chans = channels_by_id(s, brand_id)
    checks = []

    # 1. Data coverage: every channel that sold in the period has its money feed
    #    (settlements / payments / remittances) imported through period end.
    sold = {o.channel_id for o in s.scalars(select(Order).where(
        Order.brand_id == brand_id, Order.order_date.between(start, end)))}
    sold |= {p.channel_id for p in s.scalars(select(PurchaseOrder).where(
        PurchaseOrder.brand_id == brand_id, PurchaseOrder.po_date.between(start, end)))}
    last_seen: dict = {}

    def seen(cid, d):
        if d and d > last_seen.get(cid, date.min):
            last_seen[cid] = d

    for l in s.scalars(select(SettlementLine).where(SettlementLine.brand_id == brand_id)):
        seen(l.channel_id, l.posted_date)
    for p in s.scalars(select(QcPayment).where(QcPayment.brand_id == brand_id)):
        seen(p.channel_id, p.payment_date)
    for i in s.scalars(select(QcInvoice).where(QcInvoice.brand_id == brand_id)):
        seen(i.channel_id, i.invoice_date)
    for sh in s.scalars(select(LogisticsShipment).where(LogisticsShipment.brand_id == brand_id)):
        seen(sh.channel_id, sh.ship_date)
    cutoff = date.fromordinal(end.toordinal() - 7)
    missing = [chans[c].name for c in sold if last_seen.get(c, date.min) < cutoff]
    checks.append(_check("data_coverage", "Money feeds imported through period end for every channel",
                         "fail" if missing else "pass",
                         f"Stale or missing: {', '.join(missing)}" if missing
                         else f"{len(sold)} channels covered"))

    # 2. Settlement -> bank.
    s2b = [r for r in settlement_to_bank(s, brand_id, as_of) if r["settlement_date"][:7] == period]
    bad = [r for r in s2b if r["status"] in ("missing_in_bank", "amount_mismatch")]
    checks.append(_check("bank_match", "Every payout matched to a bank credit",
                         "warn" if bad else "pass",
                         f"{len(bad)} of {len(s2b)} settlements unmatched (claims raised)" if bad
                         else f"{len(s2b)} settlements matched", r2(sum(r["difference"] for r in bad))))

    # 3. Order-level reconciliation.
    recon = [r for r in reconcile_orders(s, brand_id, as_of) if r["order_date"][:7] == period]
    exc = [r for r in recon if r["recon_status"] in EXCEPTION_STATUSES]
    checks.append(_check("order_recon", "Orders reconciled; exceptions in claims register",
                         "warn" if exc else "pass",
                         f"{len(recon) - len(exc)} of {len(recon)} orders clean; {len(exc)} exceptions",
                         len(exc)))

    # 4. Tax credits.
    tax = [r for r in reconcile_tax_credits(s, brand_id)["rows"] if r["period"] == period]
    no_portal = [r for r in tax if r["status"] == "portal_data_missing"]
    at_risk = r2(sum(r["at_risk"] for r in tax))
    checks.append(_check("tax_credits", "TCS/TDS reconciled to GSTR-2B and Form 26AS",
                         "fail" if no_portal else ("warn" if at_risk > 0 else "pass"),
                         "Upload GSTR-2B / 26AS for this period" if no_portal
                         else f"Rs {at_risk:,.2f} credit at risk" if at_risk else "All credits in portal",
                         at_risk))

    # 5. Claims deadlines.
    claims = list(s.scalars(select(Claim).where(Claim.brand_id == brand_id, Claim.status == "open",
                                                Claim.event_date <= end)))
    expired = [c for c in claims if c.deadline < as_of]
    urgent = [c for c in claims if as_of <= c.deadline <= date.fromordinal(as_of.toordinal() + 7)]
    checks.append(_check("claims", "No claims about to lapse unfiled",
                         "warn" if (expired or urgent) else "pass",
                         f"{len(urgent)} due within 7 days, {len(expired)} past deadline",
                         r2(sum(c.leak_amount for c in urgent + expired))))

    # 6. Trial balance.
    tb = trial_balance(s, brand_id, end=end)
    checks.append(_check("trial_balance", "Trial balance balances",
                         "pass" if tb["balanced"] else "fail",
                         f"Dr {tb['total_debit']:,.2f} / Cr {tb['total_credit']:,.2f}"))

    # 7. Inventory sanity.
    inv = balances(s, brand_id, end=end).get("INVENTORY", (0, 0))
    inv_bal = r2(inv[0] - inv[1])
    checks.append(_check("inventory", "Stock-in-hand is not negative",
                         "pass" if inv_bal >= 0 else "fail",
                         f"Book stock Rs {inv_bal:,.2f}", inv_bal))

    # 8. Receivables quality.
    bb = borrowing_base(s, brand_id, min(as_of, end))
    overdue = bb["totals"]["overdue"]
    checks.append(_check("receivables", "Overdue marketplace receivables reviewed",
                         "warn" if overdue > 0 else "pass",
                         f"Rs {overdue:,.2f} overdue at period end", overdue))

    cp = s.scalar(select(ClosePeriod).where(ClosePeriod.brand_id == brand_id, ClosePeriod.period == period))
    fails = sum(c["status"] == "fail" for c in checks)
    return {"period": period, "status": cp.status if cp else "open",
            "locked_at": cp.locked_at.isoformat() if cp and cp.locked_at else None,
            "checks": checks, "can_lock": fails == 0,
            "score": round(sum({"pass": 1, "warn": 0.5, "fail": 0}[c["status"]] for c in checks) / len(checks), 3)}


def lock_period(s: Session, brand_id: int, period: str, as_of: date, force: bool = False) -> dict:
    cl = close_checklist(s, brand_id, period, as_of)
    if not cl["can_lock"] and not force:
        return {"locked": False, "reason": "Checklist has failing items", "checklist": cl}
    start, end = period_bounds(period)
    cp = s.scalar(select(ClosePeriod).where(ClosePeriod.brand_id == brand_id, ClosePeriod.period == period))
    if cp is None:
        cp = ClosePeriod(brand_id=brand_id, period=period)
        s.add(cp)
    cp.status, cp.locked_at = "locked", _now()
    cp.snapshot = {"trial_balance": trial_balance(s, brand_id, end=end),
                   "pnl": profit_and_loss(s, brand_id, start, end), "checklist": cl}
    s.flush()
    return {"locked": True, "period": period}


def unlock_period(s: Session, brand_id: int, period: str) -> dict:
    cp = s.scalar(select(ClosePeriod).where(ClosePeriod.brand_id == brand_id, ClosePeriod.period == period))
    if cp:
        cp.status = "open"
    s.flush()
    return {"locked": False, "period": period}


def _csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    flat = [{k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in r.items()} for r in rows]
    w = csv.DictWriter(buf, fieldnames=list(flat[0].keys()))
    w.writeheader()
    w.writerows(flat)
    return buf.getvalue()


def audit_pack(s: Session, brand_id: int, period: str, as_of: date) -> bytes:
    start, end = period_bounds(period)
    brand = s.get(Brand, brand_id)
    chans = channels_by_id(s, brand_id)
    tb = trial_balance(s, brand_id, end=end)
    pnl = profit_and_loss(s, brand_id, start, end)
    cl = close_checklist(s, brand_id, period, as_of)
    claims = [claim_dict(c, chans, as_of) for c in s.scalars(select(Claim).where(Claim.brand_id == brand_id))]
    recon = [r for r in reconcile_orders(s, brand_id, as_of) if r["order_date"][:7] == period]
    files = {
        "01_close_checklist.csv": _csv(cl["checks"]),
        "02_trial_balance.csv": _csv(tb["rows"]),
        "03_profit_and_loss.csv": _csv(pnl["lines"] + [
            {"account": "NET", "name": "Net profit", "amount": pnl["net_profit"], "type": "total"}]),
        "04_receivables_verified.csv": _csv(borrowing_base(s, brand_id, min(as_of, end))["items"]),
        "05_unreconciled_orders.csv": _csv([r for r in recon if r["recon_status"] in EXCEPTION_STATUSES]),
        "06_settlement_to_bank.csv": _csv([r for r in settlement_to_bank(s, brand_id, as_of)
                                           if r["settlement_date"][:7] == period]),
        "07_claims_register.csv": _csv(claims),
        "08_tax_credit_recon.csv": _csv([r for r in reconcile_tax_credits(s, brand_id)["rows"]
                                         if r["period"] == period]),
        "09_sku_profitability.csv": _csv(sku_profitability(s, brand_id, start, end)["rows"]),
        "10_journal.csv": _csv(journal_rows(s, brand_id, start, end)),
        "11_tally_import.xml": tally_xml(s, brand_id, brand.name, start, end),
    }
    summary = (f"Audit pack — {brand.name} — {period}\nGenerated {_now():%Y-%m-%d %H:%M} UTC\n"
               f"Close status: {cl['status']} (readiness {cl['score']:.0%})\n"
               f"Net revenue: Rs {pnl['net_revenue']:,.2f}\nNet profit: Rs {pnl['net_profit']:,.2f}\n"
               f"Trial balance balanced: {tb['balanced']}\n\nFiles:\n" + "\n".join(files))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("00_README.txt", summary)
        for name, content in files.items():
            z.writestr(name, content)
    return buf.getvalue()
