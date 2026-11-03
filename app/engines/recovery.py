"""Gap 2 — detect leaks AND turn them into claims that get recovered.

reconcile_orders(): order-level match of every order against settlements,
logistics remittances and bank deposits.
detect_leaks(): runs leak rules and upserts Claims (each with a filing deadline,
evidence, and a status workflow open -> filed -> recovered/rejected). Claim
status survives re-runs; open claims that no longer trigger are removed.
"""
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import AMOUNT_TOLERANCE as TOL
from app.config import RECOVERY_SUCCESS_FEE
from app.engines.common import (channels_by_id, expected_fees, expected_net, r2, skus_by_code,
                                slabs)
from app.models import BankDeposit, Claim, LogisticsShipment, Order, SettlementLine

RULES = {
    "commission_overcharge": "Commission above rate card",
    "fixed_fee_overcharge": "Fixed/closing fee above rate card",
    "weight_overcharge": "Shipping charged on a higher weight slab than declared",
    "short_payment": "Marketplace paid less than the order value",
    "unsettled_order": "Delivered order not settled within the payment cycle",
    "return_not_received": "Refund debited but item not returned / returned damaged",
    "fees_on_cancelled": "Fees charged on a cancelled / RTO order",
    "cod_not_remitted": "COD collected by courier but not remitted",
    "payout_not_in_bank": "Settlement payout missing or short in bank",
    "qc_transit_loss": "Quick commerce: units shipped but not received at DC",
    "qc_short_payment": "Quick commerce: payment below accepted invoice value",
    "tax_credit_missing": "TCS/TDS deducted but not in GSTR-2B / Form 26AS",
}


def _index_settlements(s: Session, brand_id: int):
    by_order = defaultdict(list)
    by_settlement = defaultdict(list)
    for sl in s.scalars(select(SettlementLine).where(SettlementLine.brand_id == brand_id)):
        if sl.external_order_id:
            by_order[sl.external_order_id].append(sl)
        by_settlement[(sl.channel_id, sl.settlement_id)].append(sl)
    return by_order, by_settlement


def reconcile_orders(s: Session, brand_id: int, as_of: date) -> list[dict]:
    chans = channels_by_id(s, brand_id)
    skus = skus_by_code(s, brand_id)
    by_order, _ = _index_settlements(s, brand_id)
    ships = {sh.external_order_id: sh for sh in
             s.scalars(select(LogisticsShipment).where(LogisticsShipment.brand_id == brand_id))}
    rows = []
    for o in s.scalars(select(Order).where(Order.brand_id == brand_id).order_by(Order.order_date)):
        ch = chans[o.channel_id]
        sku = skus.get(o.sku_code)
        lines = by_order.get(o.external_id, [])
        paid = sum(l.principal for l in lines if l.txn_type == "order")
        net = sum(l.net for l in lines)
        status, note = "matched", ""
        if o.status in ("cancelled", "rto"):
            charged = sum(l.commission + l.fixed_fee for l in lines)
            status = "fees_on_cancelled" if charged < -TOL else "not_applicable"
        elif o.status == "in_transit" or not o.delivered_date:
            status = "in_transit"
        elif o.payment_mode == "cod" and o.logistics_code:
            sh = ships.get(o.external_id)
            remitted = sh.remitted_amount if sh else 0
            paid, net = remitted, remitted
            due = o.delivered_date + timedelta(days=chans_code_days(chans, o.logistics_code))
            if remitted >= o.gross_amount - TOL:
                status = "matched"
            elif as_of > due:
                status, note = "cod_not_remitted", f"COD Rs {o.gross_amount:.0f}, remitted Rs {remitted:.0f}"
            else:
                status = "awaiting_settlement"
        elif not lines:
            due = o.delivered_date + timedelta(days=ch.settlement_days)
            status = "unsettled_overdue" if as_of > due else "awaiting_settlement"
        elif paid < o.gross_amount - TOL:
            status, note = "short_paid", f"paid Rs {paid:.2f} of Rs {o.gross_amount:.2f}"
        if o.status == "returned" and status == "matched":
            refunded = any(l.txn_type == "refund" for l in lines)
            status = "returned_refunded" if refunded else "return_pending_refund"
        rows.append({
            "order_id": o.external_id, "channel": ch.code, "sku": o.sku_code,
            "order_date": o.order_date.isoformat(), "status_ops": o.status,
            "gross": r2(o.gross_amount), "principal_paid": r2(paid), "net_received": r2(net),
            "expected_net": r2(expected_net(o, ch, sku.gst_rate if sku else 0.18))
            if o.status in ("delivered", "returned") else 0.0,
            "recon_status": status, "note": note,
        })
    return rows


def chans_code_days(chans: dict, code: str) -> int:
    for c in chans.values():
        if c.code == code:
            return c.settlement_days
    return 10


def settlement_to_bank(s: Session, brand_id: int, as_of: date) -> list[dict]:
    chans = channels_by_id(s, brand_id)
    _, by_settlement = _index_settlements(s, brand_id)
    deposits = defaultdict(float)
    dep_dates = {}
    for b in s.scalars(select(BankDeposit).where(BankDeposit.brand_id == brand_id)):
        deposits[(b.channel_id, b.settlement_id)] += b.amount
        dep_dates[(b.channel_id, b.settlement_id)] = b.deposit_date
    rows = []
    for (cid, sid), lines in sorted(by_settlement.items(), key=lambda kv: kv[0][1]):
        net = r2(sum(l.net for l in lines))
        dep = r2(deposits.get((cid, sid), 0))
        last = max(l.posted_date for l in lines)
        if abs(net - dep) <= TOL:
            status = "matched"
        elif dep == 0:
            status = "awaiting_payout" if as_of <= last + timedelta(days=5) else "missing_in_bank"
        else:
            status = "amount_mismatch"
        rows.append({"channel": chans[cid].code, "settlement_id": sid, "settlement_date": last.isoformat(),
                     "lines": len(lines), "settlement_net": net, "bank_amount": dep,
                     "difference": r2(net - dep), "deposit_date": dep_dates.get((cid, sid)).isoformat()
                     if (cid, sid) in dep_dates else None, "status": status})
    return rows


def _found(found: dict, ch, rule, ref, desc, expected, actual, leak, event_date):
    if leak <= TOL:
        return
    found[(rule, ref)] = dict(channel_id=ch.id, rule=rule, external_ref=ref, description=desc,
                              expected=r2(expected), actual=r2(actual), leak_amount=r2(leak),
                              event_date=event_date,
                              deadline=event_date + timedelta(days=ch.claim_window_days))


def detect_leaks(s: Session, brand_id: int, as_of: date, extra: dict | None = None) -> dict:
    """Run all marketplace/logistics leak rules, merge `extra` findings from the
    quick-commerce and tax engines, then upsert Claims."""
    chans = channels_by_id(s, brand_id)
    skus = skus_by_code(s, brand_id)
    by_order, _ = _index_settlements(s, brand_id)
    found: dict = {}

    for o in s.scalars(select(Order).where(Order.brand_id == brand_id)):
        ch = chans[o.channel_id]
        lines = by_order.get(o.external_id, [])
        exp = expected_fees(o, ch)
        order_lines = [l for l in lines if l.txn_type == "order"]
        for l in order_lines:
            if ch.commission_rate and -l.commission > exp["commission"] + TOL:
                _found(found, ch, "commission_overcharge", o.external_id,
                       f"{ch.name} charged commission Rs {-l.commission:.2f} vs rate card "
                       f"{ch.commission_rate:.0%} = Rs {exp['commission']:.2f}",
                       exp["commission"], -l.commission, -l.commission - exp["commission"], l.posted_date)
            if ch.fixed_fee and -l.fixed_fee > exp["fixed_fee"] + TOL:
                _found(found, ch, "fixed_fee_overcharge", o.external_id,
                       f"Fixed fee Rs {-l.fixed_fee:.2f} vs Rs {exp['fixed_fee']:.2f}",
                       exp["fixed_fee"], -l.fixed_fee, -l.fixed_fee - exp["fixed_fee"], l.posted_date)
            if ch.ship_rate_per_500g and l.shipping_fee and o.status not in ("rto",):
                should = ch.ship_rate_per_500g * slabs(o.declared_weight_g)
                if -l.shipping_fee > should + TOL:
                    _found(found, ch, "weight_overcharge", o.external_id,
                           f"Charged {l.charged_weight_g or '?'} g slab, declared {o.declared_weight_g} g",
                           should, -l.shipping_fee, -l.shipping_fee - should, l.posted_date)

        paid_by_channel = not (o.payment_mode == "cod" and o.logistics_code)  # else 3PL remits
        if o.status in ("delivered", "returned") and o.delivered_date and paid_by_channel:
            paid = sum(l.principal for l in order_lines)
            due = o.delivered_date + timedelta(days=ch.settlement_days)
            if not order_lines and as_of > due:
                sku = skus.get(o.sku_code)
                amt = expected_net(o, ch, sku.gst_rate if sku else 0.18)
                _found(found, ch, "unsettled_order", o.external_id,
                       f"Delivered {o.delivered_date}, payment due by {due}, nothing settled",
                       amt, 0, amt, due)
            elif order_lines and paid < o.gross_amount - TOL:
                _found(found, ch, "short_payment", o.external_id,
                       f"Principal paid Rs {paid:.2f} vs order value Rs {o.gross_amount:.2f}",
                       o.gross_amount, paid, o.gross_amount - paid, max(l.posted_date for l in order_lines))

        if o.status == "returned":
            refunds = [l for l in lines if l.txn_type == "refund"]
            if refunds and (o.return_received is False or o.return_condition in ("damaged", "wrong_item")):
                amt = -sum(l.principal for l in refunds)
                why = "not received back" if o.return_received is False else f"returned {o.return_condition}"
                # D2C returns come back through the courier, so the claim is against the 3PL.
                claim_ch = next((c for c in chans.values() if c.code == o.logistics_code), ch) \
                    if ch.kind == "d2c" else ch
                _found(found, claim_ch, "return_not_received", o.external_id,
                       f"Customer refunded Rs {amt:.2f}; item {why}", amt, 0, amt,
                       o.return_date or refunds[0].posted_date)

        if o.status in ("cancelled", "rto"):
            charged = -sum(l.commission + l.fixed_fee for l in lines)
            if charged > TOL:
                _found(found, ch, "fees_on_cancelled", o.external_id,
                       f"Rs {charged:.2f} commission/fees charged on {o.status} order",
                       0, charged, charged, max(l.posted_date for l in lines))

    # Logistics: COD not remitted, weight disputes.
    orders = {o.external_id: o for o in s.scalars(select(Order).where(Order.brand_id == brand_id))}
    for sh in s.scalars(select(LogisticsShipment).where(LogisticsShipment.brand_id == brand_id)):
        ch = chans[sh.channel_id]
        o = orders.get(sh.external_order_id)
        if sh.payment_mode == "cod" and o and o.status == "delivered" and o.delivered_date:
            due = o.delivered_date + timedelta(days=ch.settlement_days)
            gap = sh.cod_amount - sh.remitted_amount
            if gap > TOL and as_of > due:
                _found(found, ch, "cod_not_remitted", sh.awb,
                       f"COD Rs {sh.cod_amount:.2f} collected {o.delivered_date}; remitted Rs {sh.remitted_amount:.2f}",
                       sh.cod_amount, sh.remitted_amount, gap, due)
        if ch.ship_rate_per_500g and o and sh.shipping_fee:
            should = ch.ship_rate_per_500g * slabs(o.declared_weight_g)
            if sh.shipping_fee > should + TOL:
                _found(found, ch, "weight_overcharge", sh.awb,
                       f"AWB {sh.awb}: charged {sh.charged_weight_g} g, declared {o.declared_weight_g} g",
                       should, sh.shipping_fee, sh.shipping_fee - should, sh.ship_date)

    # Payouts that never reached the bank (or arrived short).
    for row in settlement_to_bank(s, brand_id, as_of):
        if row["status"] in ("missing_in_bank", "amount_mismatch") and row["difference"] > TOL:
            ch = next(c for c in chans.values() if c.code == row["channel"])
            _found(found, ch, "payout_not_in_bank", row["settlement_id"],
                   f"Settlement {row['settlement_id']} net Rs {row['settlement_net']:.2f}, bank Rs {row['bank_amount']:.2f}",
                   row["settlement_net"], row["bank_amount"], row["difference"],
                   date.fromisoformat(row["settlement_date"]))

    for f in (extra or {}).values():
        found[(f["rule"], f["external_ref"])] = f
    return upsert_claims(s, brand_id, found)


def upsert_claims(s: Session, brand_id: int, found: dict) -> dict:
    existing = {(c.rule, c.external_ref): c for c in
                s.scalars(select(Claim).where(Claim.brand_id == brand_id))}
    created = updated = removed = 0
    for key, f in found.items():
        c = existing.get(key)
        if c is None:
            s.add(Claim(brand_id=brand_id, **f))
            created += 1
        elif c.status == "open":
            for k in ("description", "expected", "actual", "leak_amount", "event_date", "deadline"):
                setattr(c, k, f[k])
            updated += 1
    for key, c in existing.items():
        if key not in found and c.status == "open":
            s.delete(c)
            removed += 1
    s.flush()
    return {"claims_created": created, "claims_updated": updated, "claims_auto_resolved": removed}


def claims_summary(s: Session, brand_id: int, as_of: date) -> dict:
    chans = channels_by_id(s, brand_id)
    claims = list(s.scalars(select(Claim).where(Claim.brand_id == brand_id)))
    by_rule = defaultdict(lambda: {"count": 0, "leak": 0.0, "recovered": 0.0})
    for c in claims:
        r = by_rule[c.rule]
        r["count"] += 1
        r["leak"] += c.leak_amount
        r["recovered"] += c.recovered_amount
    open_c = [c for c in claims if c.status in ("open", "filed")]
    recovered = sum(c.recovered_amount for c in claims)
    return {
        "total_found": r2(sum(c.leak_amount for c in claims)),
        "open_amount": r2(sum(c.leak_amount for c in open_c)),
        "recovered": r2(recovered),
        "success_fee": r2(recovered * RECOVERY_SUCCESS_FEE),
        "expiring_7d": r2(sum(c.leak_amount for c in open_c if c.status == "open"
                              and as_of <= c.deadline <= as_of + timedelta(days=7))),
        "expired_unfiled": r2(sum(c.leak_amount for c in open_c if c.status == "open"
                                  and c.deadline < as_of)),
        "by_rule": [{"rule": k, "label": RULES.get(k, k), "count": v["count"], "leak": r2(v["leak"]),
                     "recovered": r2(v["recovered"])} for k, v in
                    sorted(by_rule.items(), key=lambda kv: -kv[1]["leak"])],
        "by_channel": [{"channel": ch.code, "leak": r2(sum(c.leak_amount for c in claims if c.channel_id == cid))}
                       for cid, ch in chans.items() if any(c.channel_id == cid for c in claims)],
    }


def claim_dict(c: Claim, chans: dict, as_of: date) -> dict:
    days_left = (c.deadline - as_of).days
    return {"id": c.id, "channel": chans[c.channel_id].code, "rule": c.rule,
            "rule_label": RULES.get(c.rule, c.rule), "ref": c.external_ref,
            "description": c.description, "expected": c.expected, "actual": c.actual,
            "leak_amount": c.leak_amount, "event_date": c.event_date.isoformat(),
            "deadline": c.deadline.isoformat(), "days_left": days_left,
            "urgency": ("expired" if days_left < 0 else "urgent" if days_left <= 7 else "ok")
            if c.status == "open" else "-",
            "status": c.status, "recovered_amount": c.recovered_amount,
            "ticket_ref": c.ticket_ref, "notes": c.notes}


def claim_ticket_text(c: Claim, chans: dict) -> str:
    """Ready-to-paste text for the marketplace seller-support / claims portal."""
    ch = chans[c.channel_id]
    return (f"Subject: Reimbursement request — {RULES.get(c.rule, c.rule)} — {c.external_ref}\n\n"
            f"Hello {ch.name} Seller Support,\n\n"
            f"We request a review and reimbursement for reference {c.external_ref}.\n"
            f"Issue: {c.description}.\n"
            f"Expected: Rs {c.expected:,.2f}; Actual: Rs {c.actual:,.2f}; "
            f"Amount claimed: Rs {c.leak_amount:,.2f}.\n"
            f"Event date: {c.event_date:%d %b %Y}.\n\n"
            f"Supporting documents (settlement report extract, order details, "
            f"weight/packaging proof where relevant) are attached.\n\nRegards")
