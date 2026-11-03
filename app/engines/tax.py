"""Gap 4 — tax credits leak quietly.

For every channel x month x section, compare three numbers:
  computed  — what the law says should be withheld (rate x taxable value)
  deducted  — what the platform actually withheld (settlements / payment advice)
  portal    — what the government shows as your credit (GSTR-2B TCS, Form 26AS TDS)
Only `portal` is usable credit; deducted-but-missing is money at risk.
"""
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import AMOUNT_TOLERANCE as TOL
from app.config import GST_TCS_RATE, TDS_194O_RATE
from app.engines.common import channels_by_id, period_bounds, period_of, r2, skus_by_code
from app.engines.quickcommerce import expected_194q_by_invoice
from app.models import Order, QcInvoice, QcPayment, SettlementLine, TaxStatement

SECTION_LABEL = {"TCS": "GST TCS (sec 52) → GSTR-2B", "194O": "TDS 194-O → Form 26AS",
                 "194Q": "TDS 194Q → Form 26AS"}


def reconcile_tax_credits(s: Session, brand_id: int, as_of: date | None = None) -> dict:
    chans = channels_by_id(s, brand_id)
    skus = skus_by_code(s, brand_id)
    sku_of = {o.external_id: o.sku_code for o in s.scalars(select(Order).where(Order.brand_id == brand_id))}
    agg = defaultdict(lambda: {"computed": 0.0, "deducted": 0.0, "portal": 0.0, "lines": 0})

    for sl in s.scalars(select(SettlementLine).where(SettlementLine.brand_id == brand_id)):
        if chans[sl.channel_id].kind != "marketplace" or sl.txn_type not in ("order", "refund"):
            continue
        gst = skus[sku_of[sl.external_order_id]].gst_rate if sku_of.get(sl.external_order_id) in skus else 0.18
        taxable = sl.principal / (1 + gst)
        per = period_of(sl.posted_date)
        for sec, rate, val in (("TCS", GST_TCS_RATE, sl.tcs), ("194O", TDS_194O_RATE, sl.tds)):
            a = agg[(sl.channel_id, per, sec)]
            a["computed"] += rate * taxable
            a["deducted"] += -val
            a["lines"] += 1

    invoices = list(s.scalars(select(QcInvoice).where(QcInvoice.brand_id == brand_id)))
    exp = expected_194q_by_invoice(invoices)
    for p in s.scalars(select(QcPayment).where(QcPayment.brand_id == brand_id)):
        a = agg[(p.channel_id, period_of(p.payment_date), "194Q")]
        a["computed"] += exp.get(p.invoice_number, 0)
        a["deducted"] += p.tds_194q
        a["lines"] += 1

    uploaded = set()
    for t in s.scalars(select(TaxStatement).where(TaxStatement.brand_id == brand_id)):
        agg[(t.channel_id, t.period, t.section)]["portal"] += t.amount
        uploaded.add((t.period, t.section))

    rows, findings = [], {}
    for (cid, per, sec), a in sorted(agg.items(), key=lambda kv: (kv[0][1], chans[kv[0][0]].code, kv[0][2])):
        ch = chans[cid]
        comp, ded, portal = r2(a["computed"]), r2(a["deducted"]), r2(a["portal"])
        tol = max(TOL, 0.01 * comp)
        at_risk = r2(max(0.0, ded - portal)) if (per, sec) in uploaded else 0.0
        if (per, sec) not in uploaded:
            status = "portal_data_missing"
        elif ded - portal > TOL:
            status = "not_in_portal"
        elif portal - ded > TOL:
            status = "portal_exceeds_books"
        elif ded - comp > tol:
            status = "over_deducted"
        elif comp - ded > tol:
            status = "under_deducted"
        else:
            status = "matched"
        rows.append({"channel": ch.code, "period": per, "section": sec, "label": SECTION_LABEL[sec],
                     "computed": comp, "deducted": ded, "portal": portal,
                     "over_deducted": r2(max(0.0, ded - comp)) if ded - comp > tol else 0.0,
                     "at_risk": at_risk, "status": status})
        if at_risk > TOL:
            _, end = period_bounds(per)
            findings[("tax_credit_missing", f"{ch.code}:{per}:{sec}")] = dict(
                channel_id=cid, rule="tax_credit_missing", external_ref=f"{ch.code}:{per}:{sec}",
                description=f"{SECTION_LABEL[sec]}: {ch.name} withheld Rs {ded:.2f} in {per}, "
                            f"portal shows Rs {portal:.2f}. Ask {ch.name} to file/correct its return.",
                expected=ded, actual=portal, leak_amount=at_risk, event_date=end,
                deadline=end + timedelta(days=ch.claim_window_days))
    return {
        "rows": rows,
        "totals": {
            "deducted": r2(sum(r["deducted"] for r in rows)),
            "claimable_in_portal": r2(sum(r["portal"] for r in rows)),
            "at_risk": r2(sum(r["at_risk"] for r in rows)),
            "over_deducted": r2(sum(r["over_deducted"] for r in rows)),
            "periods_missing_portal_data": sorted({f"{r['period']} {r['section']}" for r in rows
                                                  if r["status"] == "portal_data_missing"}),
        },
        "findings": findings,
    }
