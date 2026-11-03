"""Gap 6 — capital priced on verified receivables, not on revenue.

Builds a receivables book where every rupee traces to a marketplace settlement,
a delivered order inside its payment cycle, a quick-commerce invoice, or a COD
shipment, then computes a borrowing base an NBFC/bank partner can lend against.

Compliance: lending must be done by an RBI-regulated entity (NBFC/bank). This
platform acts as the data/lending service provider; follow the RBI Digital
Lending Directions, show the lender's Key Fact Statement, and never pool or
route loan money through the platform's own accounts.
"""
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import ADVANCE_RATES, MAX_SINGLE_CHANNEL_SHARE
from app.engines.common import channels_by_id, expected_net, r2, skus_by_code
from app.engines.ledger import balances
from app.engines.recovery import settlement_to_bank
from app.models import Claim, LogisticsShipment, Order, QcInvoice, QcPayment, SettlementLine

BUCKETS = [(15, "0-15"), (30, "16-30"), (60, "31-60"), (10 ** 6, "60+")]


def _bucket(age: int) -> str:
    return next(label for limit, label in BUCKETS if age <= limit)


def receivables_book(s: Session, brand_id: int, as_of: date) -> list[dict]:
    chans = channels_by_id(s, brand_id)
    code_to_ch = {c.code: c for c in chans.values()}
    skus = skus_by_code(s, brand_id)
    items = []

    def add(ch, ref, kind, amount, since: date, category):
        if amount > 0.5:
            items.append({"channel": ch, "ref": ref, "kind": kind, "amount": r2(amount),
                          "since": since.isoformat(), "age_days": (as_of - since).days,
                          "age_bucket": _bucket((as_of - since).days), "category": category})

    # Settlements confirmed by the marketplace but not yet in the bank.
    for row in settlement_to_bank(s, brand_id, as_of):
        if row["status"] == "awaiting_payout":
            add(row["channel"], row["settlement_id"], "settlement", row["settlement_net"],
                date.fromisoformat(row["settlement_date"]), "settled_awaiting_payout")

    # Delivered orders not yet settled.
    settled = {sl.external_order_id for sl in s.scalars(
        select(SettlementLine).where(SettlementLine.brand_id == brand_id, SettlementLine.txn_type == "order"))}
    ships = {sh.external_order_id: sh for sh in s.scalars(
        select(LogisticsShipment).where(LogisticsShipment.brand_id == brand_id))}
    for o in s.scalars(select(Order).where(Order.brand_id == brand_id, Order.status == "delivered")):
        if not o.delivered_date or o.delivered_date > as_of:
            continue
        ch = chans[o.channel_id]
        if o.payment_mode == "cod" and o.logistics_code:
            sh = ships.get(o.external_id)
            owed = o.gross_amount - (sh.remitted_amount if sh else 0)
            lch = code_to_ch.get(o.logistics_code, ch)
            due = o.delivered_date + timedelta(days=lch.settlement_days)
            add(lch.code, o.external_id, "cod", owed, o.delivered_date,
                "delivered_within_sla" if as_of <= due else "overdue")
        elif o.external_id not in settled:
            sku = skus.get(o.sku_code)
            due = o.delivered_date + timedelta(days=ch.settlement_days)
            add(ch.code, o.external_id, "order", expected_net(o, ch, sku.gst_rate if sku else 0.18),
                o.delivered_date, "delivered_within_sla" if as_of <= due else "overdue")

    # Quick-commerce invoices not yet paid.
    paid = {(p.channel_id, p.invoice_number) for p in s.scalars(
        select(QcPayment).where(QcPayment.brand_id == brand_id))}
    for inv in s.scalars(select(QcInvoice).where(QcInvoice.brand_id == brand_id)):
        if (inv.channel_id, inv.invoice_number) in paid or inv.invoice_date > as_of:
            continue
        ch = chans[inv.channel_id]
        due = inv.invoice_date + timedelta(days=ch.settlement_days)
        add(ch.code, inv.invoice_number, "qc_invoice", inv.total, inv.invoice_date,
            "delivered_within_sla" if as_of <= due else "overdue")

    # Open disputes (amounts the brand says it is owed, not yet agreed).
    for c in s.scalars(select(Claim).where(Claim.brand_id == brand_id,
                                           Claim.status.in_(("open", "filed")))):
        if c.rule in ("unsettled_order", "cod_not_remitted"):
            continue  # already counted above as overdue
        add(chans[c.channel_id].code, f"claim-{c.id}", c.rule, c.leak_amount, c.event_date, "under_claim")
    return items


def borrowing_base(s: Session, brand_id: int, as_of: date) -> dict:
    items = receivables_book(s, brand_id, as_of)
    by_ch = defaultdict(lambda: defaultdict(float))
    aging = defaultdict(lambda: defaultdict(float))
    for it in items:
        by_ch[it["channel"]][it["category"]] += it["amount"]
        if it["category"] != "under_claim":
            aging[it["channel"]][it["age_bucket"]] += it["amount"]

    eligible = {ch: sum(v * ADVANCE_RATES[cat] for cat, v in cats.items()) for ch, cats in by_ch.items()}
    total = sum(eligible.values())
    capped = {}
    for ch, e in eligible.items():
        others = total - e
        # cap so this channel is at most MAX share of the post-cap total
        cap = MAX_SINGLE_CHANNEL_SHARE / (1 - MAX_SINGLE_CHANNEL_SHARE) * others if others else e
        capped[ch] = min(e, cap) if len(eligible) > 1 else e

    ledger_ar = {acct[3:]: r2(dr - cr) for acct, (dr, cr) in balances(s, brand_id, end=as_of).items()
                 if acct.startswith("AR:")}
    rows = []
    for ch, cats in sorted(by_ch.items()):
        rows.append({"channel": ch, **{k: r2(cats.get(k, 0)) for k in ADVANCE_RATES},
                     "eligible_before_cap": r2(eligible[ch]), "borrowing_base": r2(capped[ch]),
                     "concentration_capped": capped[ch] < eligible[ch] - 0.5,
                     "ledger_ar_balance": ledger_ar.get(ch, 0.0),
                     "aging": {label: r2(aging[ch].get(label, 0)) for _, label in BUCKETS}})
    verified = sum(r["settled_awaiting_payout"] + r["delivered_within_sla"] for r in rows)
    return {"as_of": as_of.isoformat(), "advance_rates": ADVANCE_RATES,
            "max_single_channel_share": MAX_SINGLE_CHANNEL_SHARE, "rows": rows,
            "totals": {"verified_receivables": r2(verified),
                       "overdue": r2(sum(r["overdue"] for r in rows)),
                       "under_claim": r2(sum(r["under_claim"] for r in rows)),
                       "borrowing_base": r2(sum(r["borrowing_base"] for r in rows)),
                       "ledger_ar_total": r2(sum(ledger_ar.values()))},
            "items": items}
