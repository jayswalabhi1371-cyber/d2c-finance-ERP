"""CSV importers. Each `kind` has one canonical template (see TEMPLATES and
sample_data/). Marketplace-native formats get adapters that map into the
canonical tables; Amazon's settlement V2 flat file is included as the first.

Imports are upserts keyed on natural ids, so re-uploading a file is safe.
"""
import csv
import io
from collections import defaultdict
from datetime import date, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import CHANNEL_DEFAULTS
from app.models import (BankDeposit, Channel, LogisticsShipment, Order, PoLine, PurchaseOrder,
                        QcInvoice, QcPayment, SettlementLine, Sku, TaxStatement)

TEMPLATES = {
    "channels": ["code", "name", "kind", "commission_rate", "fixed_fee", "ship_rate_per_500g",
                 "settlement_days", "claim_window_days"],
    "skus": ["code", "name", "cogs", "gst_rate", "weight_g", "opening_stock_qty"],
    "orders": ["channel", "order_id", "order_date", "delivered_date", "sku", "qty", "gross_amount",
               "payment_mode", "status", "declared_weight_g", "logistics", "return_date",
               "return_received", "return_condition"],
    "settlements": ["channel", "settlement_id", "order_id", "txn_type", "posted_date", "principal",
                    "commission", "fixed_fee", "shipping_fee", "tcs", "tds", "other", "net",
                    "charged_weight_g"],
    "bank_deposits": ["channel", "settlement_id", "utr", "deposit_date", "amount"],
    "logistics": ["channel", "awb", "order_id", "ship_date", "payment_mode", "cod_amount",
                  "remitted_amount", "remittance_date", "shipping_fee", "charged_weight_g"],
    "purchase_orders": ["channel", "po_number", "po_date", "expiry_date", "stream", "facility", "sku",
                        "qty_ordered", "qty_shipped", "qty_received", "qty_accepted", "unit_price",
                        "grn_date"],
    "qc_invoices": ["channel", "po_number", "invoice_number", "invoice_date", "taxable_value",
                    "gst_amount", "total"],
    "qc_payments": ["channel", "invoice_number", "payment_date", "invoice_amount", "ad_deduction",
                    "tds_194q", "other_deduction", "net_paid", "utr"],
    "tax_statements": ["channel", "period", "section", "amount"],
    "amazon_settlement_v2": ["settlement-id", "transaction-type", "order-id", "amount-type",
                             "amount-description", "amount", "posted-date"],
}


def _d(v) -> date | None:
    v = (v or "").strip()
    if not v:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(v[:25], fmt).date()
        except ValueError:
            continue
    return datetime.fromisoformat(v.replace("Z", "+00:00")).date()


def _f(v) -> float:
    v = str(v or "").replace(",", "").replace("₹", "").strip()
    return float(v) if v else 0.0


def _i(v) -> int | None:
    v = str(v or "").strip()
    return int(float(v)) if v else None


def _b(v) -> bool | None:
    v = str(v or "").strip().lower()
    return None if not v else v in ("1", "true", "yes", "y")


class ImportError_(ValueError):
    pass


def _channel(s: Session, brand_id: int, code: str, cache: dict) -> Channel:
    code = code.strip().lower()
    if code not in cache:
        ch = s.scalar(select(Channel).where(Channel.brand_id == brand_id, Channel.code == code))
        if ch is None:
            if code not in CHANNEL_DEFAULTS:
                raise ImportError_(f"Unknown channel '{code}'. Import it via the channels template first.")
            ch = Channel(brand_id=brand_id, code=code, name=code.title(), **CHANNEL_DEFAULTS[code])
            s.add(ch)
            s.flush()
        cache[code] = ch
    return cache[code]


def import_csv(s: Session, brand_id: int, kind: str, content: str | bytes, channel: str | None = None) -> dict:
    if kind not in TEMPLATES:
        raise ImportError_(f"Unknown import kind '{kind}'. Valid: {', '.join(TEMPLATES)}")
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")
    delim = "\t" if content.split("\n", 1)[0].count("\t") > content.split("\n", 1)[0].count(",") else ","
    rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items()}
            for r in csv.DictReader(io.StringIO(content), delimiter=delim)]
    needed = set(TEMPLATES[kind]) - {"net", "charged_weight_g", "logistics", "return_date",
                                     "return_received", "return_condition", "declared_weight_g",
                                     "delivered_date", "expiry_date", "facility", "grn_date", "utr",
                                     "settlement_id", "remittance_date", "opening_stock_qty"}
    if kind == "amazon_settlement_v2":
        needed = {"settlement-id", "order-id", "amount-description", "amount", "posted-date"}
    missing = needed - set(rows[0].keys()) if rows else set()
    if missing:
        raise ImportError_(f"Missing columns for {kind}: {', '.join(sorted(missing))}")
    cache: dict = {}
    n = globals()[f"_import_{kind}"](s, brand_id, rows, cache, channel)
    s.commit()
    return {"kind": kind, "rows": len(rows), "imported": n}


def _import_channels(s, brand_id, rows, cache, _):
    for r in rows:
        code = r["code"].lower()
        ch = s.scalar(select(Channel).where(Channel.brand_id == brand_id, Channel.code == code))
        if ch is None:
            ch = Channel(brand_id=brand_id, code=code)
            s.add(ch)
        ch.name = r.get("name") or code.title()
        ch.kind = r["kind"]
        for k in ("commission_rate", "fixed_fee", "ship_rate_per_500g"):
            setattr(ch, k, _f(r.get(k)))
        ch.settlement_days = _i(r.get("settlement_days")) or 15
        ch.claim_window_days = _i(r.get("claim_window_days")) or 45
    return len(rows)


def _import_skus(s, brand_id, rows, cache, _):
    existing = {k.code: k for k in s.scalars(select(Sku).where(Sku.brand_id == brand_id))}
    for r in rows:
        k = existing.get(r["code"]) or Sku(brand_id=brand_id, code=r["code"])
        k.name, k.cogs = r.get("name") or r["code"], _f(r["cogs"])
        k.gst_rate = _f(r.get("gst_rate")) if r.get("gst_rate") else 0.18
        if k.gst_rate > 1:
            k.gst_rate /= 100
        k.weight_g = _i(r.get("weight_g")) or 500
        k.opening_stock_qty = _i(r.get("opening_stock_qty")) or 0
        s.add(k)
    return len(rows)


def _import_orders(s, brand_id, rows, cache, _):
    existing = {(o.channel_id, o.external_id): o for o in s.scalars(select(Order).where(Order.brand_id == brand_id))}
    for r in rows:
        ch = _channel(s, brand_id, r["channel"], cache)
        o = existing.get((ch.id, r["order_id"])) or Order(brand_id=brand_id, channel_id=ch.id,
                                                         external_id=r["order_id"])
        o.order_date, o.delivered_date = _d(r["order_date"]), _d(r.get("delivered_date"))
        o.sku_code, o.qty = r["sku"], _i(r.get("qty")) or 1
        o.gross_amount = _f(r["gross_amount"])
        o.payment_mode = (r.get("payment_mode") or "prepaid").lower()
        o.status = r["status"].lower()
        o.declared_weight_g = _i(r.get("declared_weight_g"))
        o.logistics_code = (r.get("logistics") or "").lower() or None
        if o.logistics_code:
            _channel(s, brand_id, o.logistics_code, cache)
        o.return_date, o.return_received = _d(r.get("return_date")), _b(r.get("return_received"))
        o.return_condition = (r.get("return_condition") or "").lower() or None
        s.add(o)
    return len(rows)


def _replace_settlements(s, brand_id, keys: set):
    for cid, sid in keys:
        s.execute(delete(SettlementLine).where(SettlementLine.brand_id == brand_id,
                                               SettlementLine.channel_id == cid,
                                               SettlementLine.settlement_id == sid))


def _import_settlements(s, brand_id, rows, cache, _):
    parsed = []
    for r in rows:
        ch = _channel(s, brand_id, r["channel"], cache)
        vals = {k: _f(r.get(k)) for k in ("principal", "commission", "fixed_fee", "shipping_fee",
                                          "tcs", "tds", "other")}
        net = _f(r["net"]) if r.get("net") else sum(vals.values())
        parsed.append(SettlementLine(brand_id=brand_id, channel_id=ch.id, settlement_id=r["settlement_id"],
                                     external_order_id=r.get("order_id") or None,
                                     txn_type=(r.get("txn_type") or "order").lower(),
                                     posted_date=_d(r["posted_date"]), net=net,
                                     charged_weight_g=_i(r.get("charged_weight_g")), **vals))
    _replace_settlements(s, brand_id, {(p.channel_id, p.settlement_id) for p in parsed})
    s.add_all(parsed)
    return len(parsed)


# Amazon settlement V2 (flat file, long format): one row per amount component.
AMAZON_MAP = {
    "principal": "principal", "tax": "principal", "shipping": "principal", "shippingtax": "principal",
    "commission": "commission", "refundcommission": "commission",
    "fixed closing fee": "fixed_fee", "variable closing fee": "fixed_fee",
    "fba weight based fee": "shipping_fee", "fbaweightbasedfee": "shipping_fee",
    "shipping chargeback": "shipping_fee", "shippingchargeback": "shipping_fee",
    "easy ship weight handling fees": "shipping_fee", "technology fee": "other",
    "tcs-cgst": "tcs", "tcs-sgst": "tcs", "tcs-igst": "tcs", "tds (section 194-o)": "tds",
    "tds": "tds", "reversal_reimbursement": "principal", "safe-t reimbursement": "principal",
}


def _import_amazon_settlement_v2(s, brand_id, rows, cache, _):
    ch = _channel(s, brand_id, "amazon", cache)
    groups: dict = defaultdict(lambda: defaultdict(float))
    meta: dict = {}
    for r in rows:
        if not r.get("order-id") and not r.get("amount-description"):
            continue  # settlement header row
        ttype = (r.get("transaction-type") or "Order").lower()
        txn = "refund" if "refund" in ttype else "reimbursement" if "reimburse" in ttype or \
            "safe-t" in (r.get("amount-description") or "").lower() else "order" if ttype == "order" else "adjustment"
        key = (r["settlement-id"], r.get("order-id") or "", txn)
        field = AMAZON_MAP.get((r.get("amount-description") or "").strip().lower(), "other")
        groups[key][field] += _f(r["amount"])
        meta[key] = _d(r["posted-date"])
    lines = []
    for (sid, oid, txn), vals in groups.items():
        lines.append(SettlementLine(brand_id=brand_id, channel_id=ch.id, settlement_id=sid,
                                    external_order_id=oid or None, txn_type=txn, posted_date=meta[(sid, oid, txn)],
                                    net=round(sum(vals.values()), 2), **{k: round(v, 2) for k, v in vals.items()}))
    _replace_settlements(s, brand_id, {(ch.id, l.settlement_id) for l in lines})
    s.add_all(lines)
    return len(lines)


def _import_bank_deposits(s, brand_id, rows, cache, _):
    existing = {(b.channel_id, b.utr) for b in s.scalars(select(BankDeposit).where(BankDeposit.brand_id == brand_id))}
    n = 0
    for r in rows:
        ch = _channel(s, brand_id, r["channel"], cache)
        if r.get("utr") and (ch.id, r["utr"]) in existing:
            continue
        s.add(BankDeposit(brand_id=brand_id, channel_id=ch.id, settlement_id=r.get("settlement_id") or None,
                          utr=r.get("utr") or None, deposit_date=_d(r["deposit_date"]), amount=_f(r["amount"])))
        n += 1
    return n


def _import_logistics(s, brand_id, rows, cache, _):
    existing = {(x.channel_id, x.awb): x for x in
                s.scalars(select(LogisticsShipment).where(LogisticsShipment.brand_id == brand_id))}
    for r in rows:
        ch = _channel(s, brand_id, r["channel"], cache)
        x = existing.get((ch.id, r["awb"])) or LogisticsShipment(brand_id=brand_id, channel_id=ch.id, awb=r["awb"])
        x.external_order_id, x.ship_date = r["order_id"], _d(r["ship_date"])
        x.payment_mode = (r.get("payment_mode") or "prepaid").lower()
        x.cod_amount, x.remitted_amount = _f(r.get("cod_amount")), _f(r.get("remitted_amount"))
        x.remittance_date = _d(r.get("remittance_date"))
        x.shipping_fee, x.charged_weight_g = _f(r.get("shipping_fee")), _i(r.get("charged_weight_g"))
        s.add(x)
    return len(rows)


def _import_purchase_orders(s, brand_id, rows, cache, _):
    pos: dict = {}
    for r in rows:
        ch = _channel(s, brand_id, r["channel"], cache)
        key = (ch.id, r["po_number"])
        if key not in pos:
            po = s.scalar(select(PurchaseOrder).where(PurchaseOrder.brand_id == brand_id,
                                                      PurchaseOrder.channel_id == ch.id,
                                                      PurchaseOrder.po_number == r["po_number"]))
            if po is None:
                po = PurchaseOrder(brand_id=brand_id, channel_id=ch.id, po_number=r["po_number"])
                s.add(po)
            po.po_date, po.expiry_date = _d(r["po_date"]), _d(r.get("expiry_date"))
            po.stream = (r.get("stream") or "direct_buy").lower()
            po.facility = r.get("facility") or None
            po.lines = []
            pos[key] = po
        pos[key].lines.append(PoLine(sku_code=r["sku"], qty_ordered=_i(r["qty_ordered"]) or 0,
                                     qty_shipped=_i(r.get("qty_shipped")) or 0,
                                     qty_received=_i(r.get("qty_received")) or 0,
                                     qty_accepted=_i(r.get("qty_accepted")) or 0,
                                     unit_price=_f(r["unit_price"]), grn_date=_d(r.get("grn_date"))))
    return len(rows)


def _import_qc_invoices(s, brand_id, rows, cache, _):
    existing = {(i.channel_id, i.invoice_number): i for i in
                s.scalars(select(QcInvoice).where(QcInvoice.brand_id == brand_id))}
    for r in rows:
        ch = _channel(s, brand_id, r["channel"], cache)
        i = existing.get((ch.id, r["invoice_number"])) or QcInvoice(brand_id=brand_id, channel_id=ch.id,
                                                                    invoice_number=r["invoice_number"])
        i.po_number, i.invoice_date = r["po_number"], _d(r["invoice_date"])
        i.taxable_value, i.gst_amount = _f(r["taxable_value"]), _f(r["gst_amount"])
        i.total = _f(r["total"]) or i.taxable_value + i.gst_amount
        s.add(i)
    return len(rows)


def _import_qc_payments(s, brand_id, rows, cache, _):
    existing = {(p.channel_id, p.invoice_number, p.utr) for p in
                s.scalars(select(QcPayment).where(QcPayment.brand_id == brand_id))}
    n = 0
    for r in rows:
        ch = _channel(s, brand_id, r["channel"], cache)
        if (ch.id, r["invoice_number"], r.get("utr") or None) in existing:
            continue
        s.add(QcPayment(brand_id=brand_id, channel_id=ch.id, invoice_number=r["invoice_number"],
                        payment_date=_d(r["payment_date"]), invoice_amount=_f(r["invoice_amount"]),
                        ad_deduction=_f(r.get("ad_deduction")), tds_194q=_f(r.get("tds_194q")),
                        other_deduction=_f(r.get("other_deduction")), net_paid=_f(r["net_paid"]),
                        utr=r.get("utr") or None))
        n += 1
    return n


def _import_tax_statements(s, brand_id, rows, cache, _):
    for r in rows:
        ch = _channel(s, brand_id, r["channel"], cache)
        sec = r["section"].upper().replace("-", "")
        s.execute(delete(TaxStatement).where(TaxStatement.brand_id == brand_id, TaxStatement.channel_id == ch.id,
                                             TaxStatement.period == r["period"], TaxStatement.section == sec))
    for r in rows:
        ch = _channel(s, brand_id, r["channel"], cache)
        s.add(TaxStatement(brand_id=brand_id, channel_id=ch.id, period=r["period"],
                           section=r["section"].upper().replace("-", ""), amount=_f(r["amount"])))
    return len(rows)


def template_csv(kind: str) -> str:
    return ",".join(TEMPLATES[kind]) + "\n"
