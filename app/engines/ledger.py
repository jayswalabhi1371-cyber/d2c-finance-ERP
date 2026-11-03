"""Gap 1 — one source of truth from settlement to ledger.

Every operational record is posted as a balanced double-entry journal, tagged by
channel and SKU, so reconciliation, receivables, tax credits and SKU profit all
come from the same numbers the books show. Locked periods are never re-posted.
"""
from collections import defaultdict
from datetime import date
from xml.sax.saxutils import escape

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import (CONTRA_INCOME_ACCOUNTS, EXPENSE_ACCOUNTS, GST_ON_FEES, INCOME_ACCOUNTS,
                        account_name)
from app.engines.common import channels_by_id, period_of, r2, skus_by_code
from app.models import (BankDeposit, Claim, ClosePeriod, JournalEntry, JournalLine,
                        LogisticsShipment, Order, PurchaseOrder, QcInvoice, QcPayment,
                        SettlementLine)


class _Poster:
    def __init__(self, s: Session, brand_id: int, locked: set[str]):
        self.s, self.brand_id, self.locked = s, brand_id, locked
        self.count = 0

    def post(self, d: date, source: str, ref: str, narration: str, lines: list[tuple]):
        """lines: (account, debit, credit, channel_code, sku_code)."""
        if d is None or period_of(d) in self.locked:
            return
        lines = [(a, r2(dr), r2(cr), ch, sku) for a, dr, cr, ch, sku in lines if r2(dr) or r2(cr)]
        if not lines:
            return
        diff = r2(sum(l[1] for l in lines) - sum(l[2] for l in lines))
        if diff:  # absorb paise-level rounding into the largest line
            i = max(range(len(lines)), key=lambda k: lines[k][1] + lines[k][2])
            a, dr, cr, ch, sku = lines[i]
            lines[i] = (a, dr, r2(cr + diff), ch, sku) if cr else (a, r2(dr - diff), cr, ch, sku)
        e = JournalEntry(brand_id=self.brand_id, entry_date=d, period=period_of(d),
                         source=source, source_ref=ref, narration=narration[:300])
        e.lines = [JournalLine(brand_id=self.brand_id, account=a, debit=dr, credit=cr,
                               channel_code=ch, sku_code=sku) for a, dr, cr, ch, sku in lines]
        self.s.add(e)
        self.count += 1


def _fee_lines(amount_signed: float, expense: str, ar: str, ch: str, sku: str | None):
    """A charge (negative) debits expense + input GST and credits AR; a reversal flips it."""
    amt = abs(amount_signed)
    base, gst = amt / (1 + GST_ON_FEES), amt - amt / (1 + GST_ON_FEES)
    if amount_signed < 0:
        return [(expense, base, 0, ch, sku), ("GST_INPUT", gst, 0, ch, sku), (ar, 0, amt, ch, None)]
    return [(expense, 0, base, ch, sku), ("GST_INPUT", 0, gst, ch, sku), (ar, amt, 0, ch, None)]


def rebuild_ledger(s: Session, brand_id: int) -> dict:
    locked = {p.period for p in s.scalars(select(ClosePeriod).where(
        ClosePeriod.brand_id == brand_id, ClosePeriod.status == "locked"))}
    old_ids = select(JournalEntry.id).where(JournalEntry.brand_id == brand_id,
                                            JournalEntry.period.not_in(locked or {""}))
    s.execute(delete(JournalLine).where(JournalLine.entry_id.in_(old_ids)))
    s.execute(delete(JournalEntry).where(JournalEntry.brand_id == brand_id,
                                         JournalEntry.period.not_in(locked or {""})))
    s.flush()

    P = _Poster(s, brand_id, locked)
    chans = channels_by_id(s, brand_id)
    skus = skus_by_code(s, brand_id)
    orders = list(s.scalars(select(Order).where(Order.brand_id == brand_id)))
    order_by_ext = {o.external_id: o for o in orders}

    # 0. Opening stock, on the first day of the earliest month with activity.
    first = min((o.order_date for o in orders), default=None)
    if first:
        opening = [(k.code, k.opening_stock_qty * k.cogs) for k in skus.values() if k.opening_stock_qty]
        if opening:
            P.post(first.replace(day=1), "opening", "opening-stock", "Opening stock",
                   [("INVENTORY", v, 0, None, code) for code, v in opening]
                   + [("CAPITAL", 0, sum(v for _, v in opening), None, None)])

    # 1. Sales recognised on delivery (+ COGS); returns put sellable stock back.
    for o in orders:
        ch = chans[o.channel_id]
        sku = skus.get(o.sku_code)
        gst_rate = sku.gst_rate if sku else 0.18
        if o.delivered_date and o.status in ("delivered", "returned"):
            ar_ch = o.logistics_code if (o.payment_mode == "cod" and o.logistics_code) else ch.code
            taxable = o.gross_amount / (1 + gst_rate)
            cogs = (sku.cogs if sku else 0) * o.qty
            P.post(o.delivered_date, "sale", o.external_id, f"{ch.name} sale {o.external_id}", [
                (f"AR:{ar_ch}", o.gross_amount, 0, ar_ch, None),
                ("SALES", 0, taxable, ch.code, o.sku_code),
                ("GST_OUTPUT", 0, o.gross_amount - taxable, ch.code, o.sku_code),
                ("COGS", cogs, 0, ch.code, o.sku_code),
                ("INVENTORY", 0, cogs, ch.code, o.sku_code),
            ])
        if o.status == "returned" and o.return_received and o.return_condition == "sellable" and sku:
            cogs = sku.cogs * o.qty
            P.post(o.return_date or o.delivered_date, "return_stock", o.external_id,
                   f"Return restocked {o.external_id}", [
                       ("INVENTORY", cogs, 0, ch.code, o.sku_code),
                       ("COGS", 0, cogs, ch.code, o.sku_code)])

    # 2. Settlement lines: fees, taxes withheld, refunds, reimbursements.
    for sl in s.scalars(select(SettlementLine).where(SettlementLine.brand_id == brand_id)):
        ch = chans[sl.channel_id]
        o = order_by_ext.get(sl.external_order_id or "")
        sku_code = o.sku_code if o else None
        gst_rate = skus[sku_code].gst_rate if sku_code in skus else 0.18
        ar = f"AR:{ch.code}"
        lines: list[tuple] = []
        if sl.txn_type == "refund" and sl.principal < 0:
            amt = -sl.principal
            taxable = amt / (1 + gst_rate)
            lines += [("SALES_RETURNS", taxable, 0, ch.code, sku_code),
                      ("GST_OUTPUT", amt - taxable, 0, ch.code, sku_code),
                      (ar, 0, amt, ch.code, None)]
        elif sl.txn_type == "reimbursement":
            amt = sl.principal + sl.other
            lines += [(ar, amt, 0, ch.code, None), ("CLAIMS_RECEIVABLE", 0, amt, ch.code, sku_code)]
        for field, acct in (("commission", "COMMISSION"), ("fixed_fee", "FIXED_FEES"),
                            ("shipping_fee", "SHIPPING")):
            v = getattr(sl, field)
            if v:
                lines += _fee_lines(v, acct, ar, ch.code, sku_code)
        if sl.other and sl.txn_type != "reimbursement":
            lines += _fee_lines(sl.other, "OTHER_FEES", ar, ch.code, sku_code)
        for v, acct in ((sl.tcs, "TCS_RECEIVABLE"), (sl.tds, "TDS_RECEIVABLE")):
            if v:
                lines += [(acct, -v, 0, ch.code, None), (ar, 0, -v, ch.code, None)] if v < 0 else \
                         [(acct, 0, v, ch.code, None), (ar, v, 0, ch.code, None)]
        P.post(sl.posted_date, "settlement", f"{sl.settlement_id}/{sl.external_order_id or '-'}",
               f"{ch.name} settlement {sl.settlement_id} {sl.txn_type} {sl.external_order_id or ''}",
               lines)

    # 3. Bank deposits clear channel receivables.
    for b in s.scalars(select(BankDeposit).where(BankDeposit.brand_id == brand_id)):
        ch = chans[b.channel_id]
        P.post(b.deposit_date, "bank", b.utr or b.settlement_id or str(b.id),
               f"{ch.name} payout {b.settlement_id or ''} UTR {b.utr or ''}",
               [("BANK", b.amount, 0, ch.code, None), (f"AR:{ch.code}", 0, b.amount, ch.code, None)])

    # 4. Logistics: shipping charges (paid from wallet) and COD remittances.
    for sh in s.scalars(select(LogisticsShipment).where(LogisticsShipment.brand_id == brand_id)):
        ch = chans[sh.channel_id]
        o = order_by_ext.get(sh.external_order_id)
        sku_code = o.sku_code if o else None
        sales_ch = chans[o.channel_id].code if o else ch.code
        if sh.shipping_fee:
            base = sh.shipping_fee / (1 + GST_ON_FEES)
            P.post(sh.ship_date, "logistics_fee", sh.awb, f"{ch.name} freight AWB {sh.awb}", [
                ("SHIPPING", base, 0, sales_ch, sku_code),
                ("GST_INPUT", sh.shipping_fee - base, 0, sales_ch, sku_code),
                ("BANK", 0, sh.shipping_fee, ch.code, None)])
        if sh.remitted_amount and sh.remittance_date:
            P.post(sh.remittance_date, "cod_remittance", sh.awb, f"{ch.name} COD remittance {sh.awb}", [
                ("BANK", sh.remitted_amount, 0, ch.code, None),
                (f"AR:{ch.code}", 0, sh.remitted_amount, ch.code, None)])

    # 5. Quick commerce: invoice on GRN (revenue + COGS on accepted qty), then payment.
    pos = {(p.channel_id, p.po_number): p for p in
           s.scalars(select(PurchaseOrder).where(PurchaseOrder.brand_id == brand_id))}
    for inv in s.scalars(select(QcInvoice).where(QcInvoice.brand_id == brand_id)):
        ch = chans[inv.channel_id]
        po = pos.get((inv.channel_id, inv.po_number))
        accepted = [(l.sku_code, l.qty_accepted * l.unit_price, l.qty_accepted)
                    for l in (po.lines if po else []) if l.qty_accepted]
        grn_value = sum(v for _, v, _ in accepted) or 1
        lines = [(f"AR:{ch.code}", inv.total, 0, ch.code, None),
                 ("GST_OUTPUT", 0, inv.gst_amount, ch.code, None)]
        if accepted:
            for sku_code, v, qty in accepted:
                lines.append(("SALES", 0, inv.taxable_value * v / grn_value, ch.code, sku_code))
                cogs = (skus[sku_code].cogs if sku_code in skus else 0) * qty
                lines += [("COGS", cogs, 0, ch.code, sku_code), ("INVENTORY", 0, cogs, ch.code, sku_code)]
        else:
            lines.append(("SALES", 0, inv.taxable_value, ch.code, None))
        P.post(inv.invoice_date, "qc_invoice", inv.invoice_number,
               f"{ch.name} invoice {inv.invoice_number} PO {inv.po_number}", lines)

    for pay in s.scalars(select(QcPayment).where(QcPayment.brand_id == brand_id)):
        ch = chans[pay.channel_id]
        ar = f"AR:{ch.code}"
        lines = [("BANK", pay.net_paid, 0, ch.code, None),
                 ("TDS_RECEIVABLE", pay.tds_194q, 0, ch.code, None)]
        for amt, acct in ((pay.ad_deduction, "ADS"), (pay.other_deduction, "OTHER_FEES")):
            if amt:
                base = amt / (1 + GST_ON_FEES)
                lines += [(acct, base, 0, ch.code, None), ("GST_INPUT", amt - base, 0, ch.code, None)]
        lines.append((ar, 0, pay.net_paid + pay.tds_194q + pay.ad_deduction + pay.other_deduction,
                      ch.code, None))
        P.post(pay.payment_date, "qc_payment", pay.utr or pay.invoice_number,
               f"{ch.name} payment for {pay.invoice_number}", lines)

    # 6. Recovered claims are income; the receivable clears when the money arrives.
    for c in s.scalars(select(Claim).where(Claim.brand_id == brand_id, Claim.status == "recovered")):
        if c.recovered_amount:
            ch = chans[c.channel_id]
            P.post(c.recovered_date or c.event_date, "claim", f"claim-{c.id}",
                   f"Recovered {c.rule} {c.external_ref}", [
                       ("CLAIMS_RECEIVABLE", c.recovered_amount, 0, ch.code, None),
                       ("CLAIMS_INCOME", 0, c.recovered_amount, ch.code, None)])

    s.flush()
    return {"entries_posted": P.count, "locked_periods": sorted(locked)}


# ---- Reports -----------------------------------------------------------------

def balances(s: Session, brand_id: int, start: date | None = None, end: date | None = None,
             by: tuple[str, ...] = ("account",)) -> dict:
    """Debit/credit totals grouped in SQL by the given JournalLine columns."""
    cols = [getattr(JournalLine, k) for k in by]
    q = (select(*cols, func.sum(JournalLine.debit), func.sum(JournalLine.credit))
         .join(JournalEntry, JournalLine.entry_id == JournalEntry.id)
         .where(JournalLine.brand_id == brand_id).group_by(*cols))
    if start:
        q = q.where(JournalEntry.entry_date >= start)
    if end:
        q = q.where(JournalEntry.entry_date <= end)
    out: dict = {}
    for row in s.execute(q):
        key = tuple(row[:-2]) if len(by) > 1 else row[0]
        out[key] = [float(row[-2] or 0), float(row[-1] or 0)]
    return out


def trial_balance(s: Session, brand_id: int, end: date | None = None) -> dict:
    rows = []
    for acct, (dr, cr) in sorted(balances(s, brand_id, end=end).items()):
        net = r2(dr - cr)
        rows.append({"account": acct, "name": account_name(acct),
                     "debit": net if net > 0 else 0.0, "credit": -net if net < 0 else 0.0})
    td, tc = r2(sum(r["debit"] for r in rows)), r2(sum(r["credit"] for r in rows))
    return {"rows": rows, "total_debit": td, "total_credit": tc, "balanced": abs(td - tc) < 0.05}


def profit_and_loss(s: Session, brand_id: int, start: date | None = None, end: date | None = None):
    b = balances(s, brand_id, start, end)

    def net(a, credit_normal):
        dr, cr = b.get(a, (0, 0))
        return r2(cr - dr) if credit_normal else r2(dr - cr)

    income = {a: net(a, True) for a in sorted(INCOME_ACCOUNTS)}
    contra = {a: net(a, False) for a in sorted(CONTRA_INCOME_ACCOUNTS)}
    expenses = {a: net(a, False) for a in sorted(EXPENSE_ACCOUNTS)}
    revenue = r2(income["SALES"] - contra["SALES_RETURNS"])
    total_exp = r2(sum(expenses.values()))
    return {
        "lines": ([{"account": a, "name": account_name(a), "amount": v, "type": "income"}
                   for a, v in income.items()]
                  + [{"account": a, "name": account_name(a), "amount": -v, "type": "income"}
                     for a, v in contra.items()]
                  + [{"account": a, "name": account_name(a), "amount": -v, "type": "expense"}
                     for a, v in expenses.items()]),
        "net_revenue": revenue,
        "other_income": income["CLAIMS_INCOME"],
        "total_expenses": total_exp,
        "net_profit": r2(revenue + income["CLAIMS_INCOME"] - total_exp),
    }


def journal_rows(s: Session, brand_id: int, start=None, end=None) -> list[dict]:
    q = select(JournalEntry).where(JournalEntry.brand_id == brand_id).order_by(
        JournalEntry.entry_date, JournalEntry.id)
    if start:
        q = q.where(JournalEntry.entry_date >= start)
    if end:
        q = q.where(JournalEntry.entry_date <= end)
    rows = []
    for e in s.scalars(q):
        for l in e.lines:
            rows.append({"date": e.entry_date.isoformat(), "voucher": e.id, "source": e.source,
                         "ref": e.source_ref, "narration": e.narration, "account": l.account,
                         "account_name": account_name(l.account), "debit": l.debit,
                         "credit": l.credit, "channel": l.channel_code or "", "sku": l.sku_code or ""})
    return rows


def tally_xml(s: Session, brand_id: int, company: str, start=None, end=None,
              summarise_by_day: bool = True) -> str:
    """Tally Prime import XML (Journal vouchers). In Tally, debits are negative
    amounts with ISDEEMEDPOSITIVE=Yes. Summarising by day/source keeps the
    voucher count manageable for high-order-volume brands."""
    q = select(JournalEntry).where(JournalEntry.brand_id == brand_id).order_by(JournalEntry.entry_date)
    if start:
        q = q.where(JournalEntry.entry_date >= start)
    if end:
        q = q.where(JournalEntry.entry_date <= end)
    groups: dict = defaultdict(lambda: defaultdict(float))
    narr: dict = {}
    for e in s.scalars(q):
        key = (e.entry_date, e.source) if summarise_by_day else (e.entry_date, e.source, e.id)
        narr[key] = (f"{e.source} summary {e.entry_date.isoformat()}" if summarise_by_day
                     else e.narration)
        for l in e.lines:
            groups[key][account_name(l.account)] += l.debit - l.credit
    vouchers = []
    for i, (key, accts) in enumerate(sorted(groups.items(), key=lambda kv: kv[0][:2]), 1):
        d = key[0].strftime("%Y%m%d")
        entries = "".join(
            f"<ALLLEDGERENTRIES.LIST><LEDGERNAME>{escape(name)}</LEDGERNAME>"
            f"<ISDEEMEDPOSITIVE>{'Yes' if amt > 0 else 'No'}</ISDEEMEDPOSITIVE>"
            f"<AMOUNT>{-r2(amt):.2f}</AMOUNT></ALLLEDGERENTRIES.LIST>"
            for name, amt in sorted(accts.items()) if r2(amt))
        vouchers.append(
            f'<TALLYMESSAGE xmlns:UDF="TallyUDF"><VOUCHER VCHTYPE="Journal" ACTION="Create">'
            f"<DATE>{d}</DATE><VOUCHERTYPENAME>Journal</VOUCHERTYPENAME>"
            f"<VOUCHERNUMBER>D2C-{d}-{i}</VOUCHERNUMBER>"
            f"<NARRATION>{escape(narr[key])}</NARRATION>{entries}</VOUCHER></TALLYMESSAGE>")
    return ("<ENVELOPE><HEADER><TALLYREQUEST>Import Data</TALLYREQUEST></HEADER><BODY><IMPORTDATA>"
            f"<REQUESTDESC><REPORTNAME>Vouchers</REPORTNAME><STATICVARIABLES>"
            f"<SVCURRENTCOMPANY>{escape(company)}</SVCURRENTCOMPANY></STATICVARIABLES></REQUESTDESC>"
            f"<REQUESTDATA>{''.join(vouchers)}</REQUESTDATA></IMPORTDATA></BODY></ENVELOPE>")
