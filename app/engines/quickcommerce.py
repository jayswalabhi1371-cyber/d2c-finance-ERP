"""Gap 3 — quick commerce is B2B: PO -> ASN -> GRN -> invoice -> payment, with ad
spend netted against payments and 194Q TDS deducted per platform.

Reconciles each PO through that chain, splits money into the three streams
(direct-buy, consignment, ad spend), and raises claims for transit losses and
short payments.
"""
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import AMOUNT_TOLERANCE as TOL
from app.config import TDS_194Q_RATE, TDS_194Q_THRESHOLD
from app.engines.common import channels_by_id, period_of, r2, skus_by_code
from app.models import PurchaseOrder, QcInvoice, QcPayment


def fy_of(d: date) -> str:
    y = d.year if d.month >= 4 else d.year - 1
    return f"FY{y}-{(y + 1) % 100:02d}"


def expected_194q_by_invoice(invoices: list[QcInvoice]) -> dict[str, float]:
    """194Q applies only on purchases above Rs 50 lakh per buyer per FY."""
    cum: dict = defaultdict(float)
    out = {}
    for inv in sorted(invoices, key=lambda i: (i.invoice_date, i.invoice_number)):
        key = (inv.channel_id, fy_of(inv.invoice_date))
        before = cum[key]
        cum[key] += inv.taxable_value
        above = max(0.0, cum[key] - max(before, TDS_194Q_THRESHOLD))
        out[inv.invoice_number] = above * TDS_194Q_RATE
    return out


def reconcile_quick_commerce(s: Session, brand_id: int, as_of: date) -> dict:
    chans = channels_by_id(s, brand_id)
    skus = skus_by_code(s, brand_id)
    invoices = list(s.scalars(select(QcInvoice).where(QcInvoice.brand_id == brand_id)))
    inv_by_po = defaultdict(list)
    for inv in invoices:
        inv_by_po[(inv.channel_id, inv.po_number)].append(inv)
    pays_by_inv = defaultdict(list)
    for p in s.scalars(select(QcPayment).where(QcPayment.brand_id == brand_id)):
        pays_by_inv[(p.channel_id, p.invoice_number)].append(p)
    exp_tds = expected_194q_by_invoice(invoices)

    po_rows, findings = [], {}
    streams = defaultdict(lambda: defaultdict(float))
    for po in s.scalars(select(PurchaseOrder).where(PurchaseOrder.brand_id == brand_id)
                        .order_by(PurchaseOrder.po_date)):
        ch = chans[po.channel_id]
        ordered = sum(l.qty_ordered for l in po.lines)
        shipped = sum(l.qty_shipped for l in po.lines)
        received = sum(l.qty_received for l in po.lines)
        accepted = sum(l.qty_accepted for l in po.lines)
        grn_value = sum(l.qty_accepted * l.unit_price for l in po.lines)
        transit_loss = sum(max(0, l.qty_shipped - l.qty_received) * l.unit_price *
                           (1 + (skus[l.sku_code].gst_rate if l.sku_code in skus else 0.18))
                           for l in po.lines)
        rejected_value = sum((l.qty_received - l.qty_accepted) * l.unit_price for l in po.lines)
        invs = inv_by_po.get((po.channel_id, po.po_number), [])
        inv_taxable = sum(i.taxable_value for i in invs)
        inv_total = sum(i.total for i in invs)
        pays = [p for i in invs for p in pays_by_inv.get((po.channel_id, i.invoice_number), [])]
        settled = sum(p.net_paid + p.tds_194q + p.ad_deduction + p.other_deduction for p in pays)
        ads = sum(p.ad_deduction for p in pays)
        tds = sum(p.tds_194q for p in pays)
        tds_exp = sum(exp_tds.get(i.invoice_number, 0) for i in invs)
        net_paid = sum(p.net_paid for p in pays)

        flags = []
        last_grn = max((l.grn_date for l in po.lines if l.grn_date), default=None)
        if shipped == 0 and po.expiry_date and po.expiry_date < as_of:
            flags.append("po_expired_unfulfilled")
        if transit_loss > TOL:
            flags.append("transit_loss")
            findings[("qc_transit_loss", po.po_number)] = dict(
                channel_id=ch.id, rule="qc_transit_loss", external_ref=po.po_number,
                description=f"{ch.name} PO {po.po_number}: shipped {shipped}, received {received} units",
                expected=r2(transit_loss), actual=0, leak_amount=r2(transit_loss),
                event_date=last_grn or po.po_date,
                deadline=(last_grn or po.po_date) + timedelta(days=ch.claim_window_days))
        if inv_taxable - grn_value > TOL:
            flags.append("invoice_exceeds_grn")
        if pays and inv_total - settled > TOL:
            flags.append("short_payment")
            findings[("qc_short_payment", po.po_number)] = dict(
                channel_id=ch.id, rule="qc_short_payment", external_ref=po.po_number,
                description=f"{ch.name} PO {po.po_number}: invoiced Rs {inv_total:.2f}, accounted "
                            f"Rs {settled:.2f} (paid + ads + TDS + other)",
                expected=r2(inv_total), actual=r2(settled), leak_amount=r2(inv_total - settled),
                event_date=max(p.payment_date for p in pays),
                deadline=max(p.payment_date for p in pays) + timedelta(days=ch.claim_window_days))
        if invs and not pays:
            due = min(i.invoice_date for i in invs) + timedelta(days=ch.settlement_days)
            flags.append("payment_overdue" if as_of > due else "awaiting_payment")
        if abs(tds - tds_exp) > TOL and pays:
            flags.append("tds_194q_mismatch")
        if rejected_value > TOL:
            flags.append("rejected_at_grn")

        stream_key = po.stream
        if invs:
            streams[ch.code][stream_key] += inv_taxable
        streams[ch.code]["ad_spend"] += ads
        po_rows.append({
            "channel": ch.code, "po_number": po.po_number, "po_date": po.po_date.isoformat(),
            "stream": po.stream, "facility": po.facility, "ordered": ordered, "shipped": shipped,
            "received": received, "accepted": accepted,
            "fill_rate": round(shipped / ordered, 3) if ordered else 0,
            "acceptance_rate": round(accepted / received, 3) if received else 0,
            "grn_value": r2(grn_value), "invoice_taxable": r2(inv_taxable), "invoice_total": r2(inv_total),
            "net_paid": r2(net_paid), "ad_deduction": r2(ads), "tds_194q": r2(tds),
            "tds_194q_expected": r2(tds_exp), "transit_loss": r2(transit_loss),
            "rejected_value": r2(rejected_value), "flags": flags,
            "status": "clean" if not [f for f in flags if f != "awaiting_payment"] else "exception",
        })

    ch_summary = []
    for code, st in streams.items():
        rows = [r for r in po_rows if r["channel"] == code]
        sales = st.get("direct_buy", 0) + st.get("consignment", 0)
        ch_summary.append({
            "channel": code, "pos": len(rows),
            "fill_rate": round(sum(r["shipped"] for r in rows) / max(1, sum(r["ordered"] for r in rows)), 3),
            "direct_buy_sales": r2(st.get("direct_buy", 0)), "consignment_sales": r2(st.get("consignment", 0)),
            "ad_spend_netted": r2(st.get("ad_spend", 0)),
            "ad_spend_pct_of_sales": round(st.get("ad_spend", 0) / sales, 4) if sales else 0,
            "exceptions": sum(1 for r in rows if r["status"] == "exception"),
        })
    return {"pos": po_rows, "channels": ch_summary, "findings": findings}
