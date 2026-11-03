"""Demo data: a fictional mid-size brand selling on Amazon, Flipkart, Shopify
(+ Shiprocket COD), Blinkit and Zepto for Aug-Oct 2026, with realistic leaks
planted at known rates. Written as CSVs in the canonical templates and loaded
through the normal importers, so the demo exercises the real import path.

    python -m app.seed            # writes sample_data/*.csv and loads a demo brand
"""
import csv
import io
import random
import zlib
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import CHANNEL_DEFAULTS, GST_TCS_RATE, TDS_194O_RATE, TDS_194Q_RATE, TDS_194Q_THRESHOLD
from app.engines.common import slabs
from app.ingest.parsers import TEMPLATES, import_csv
from app.models import Brand

DEMO_AS_OF = date(2026, 11, 3)
START, END = date(2026, 8, 1), date(2026, 10, 31)

SKUS = [  # code, name, price incl GST, cogs excl GST, gst, weight
    ("KC-SER-30", "Vitamin C Face Serum 30ml", 699, 150, 0.18, 180),
    ("KC-MOI-50", "Ceramide Moisturiser 50g", 549, 120, 0.18, 220),
    ("KC-SUN-50", "Mineral Sunscreen SPF50", 599, 140, 0.18, 200),
    ("KC-HAIR-200", "Onion Hair Oil 200ml", 399, 85, 0.18, 320),
    ("KC-SHAM-250", "Sulphate-free Shampoo 250ml", 449, 95, 0.18, 380),
    ("KC-KIT-GIFT", "Festive Gift Kit", 1999, 620, 0.18, 1150),
    ("KC-TEA-100", "Kashmiri Kahwa 100g", 349, 90, 0.05, 140),
    ("KC-GHEE-500", "A2 Cow Ghee 500ml", 999, 520, 0.12, 640),
]


def _days(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


def _next_thursday(d: date) -> date:
    return d + timedelta(days=(3 - d.weekday()) % 7 or 7)


def generate(as_of: date = DEMO_AS_OF, seed: int = 42) -> dict[str, list[dict]]:
    rnd = random.Random(seed)
    out: dict[str, list[dict]] = defaultdict(list)
    out["skus"] = [dict(code=c, name=n, cogs=cogs, gst_rate=g, weight_g=w, opening_stock_qty=6000)
                   for c, n, _, cogs, g, w in SKUS]
    out["channels"] = [dict(code=c, name={"shiprocket": "Shiprocket"}.get(c, c.title()), **{
        k: v for k, v in CHANNEL_DEFAULTS[c].items()}) for c in
        ("amazon", "flipkart", "shopify", "shiprocket", "blinkit", "zepto")]
    cfg = {c: CHANNEL_DEFAULTS[c] for c in CHANNEL_DEFAULTS}
    price = {c: p for c, _, p, _, _, _ in SKUS}
    gst = {c: g for c, _, _, _, g, _ in SKUS}
    weight = {c: w for c, _, _, _, _, w in SKUS}
    settlements = defaultdict(list)  # (channel, settlement_id) -> rows

    def settle(ch, posted, row):
        if posted > as_of:
            return
        prefix = {"amazon": "AMZ", "flipkart": "FK", "shopify": "SHP"}[ch]
        sid = f"{prefix}-{(posted if ch == 'shopify' else _next_thursday(posted - timedelta(days=1))):%Y%m%d}"
        p = posted if ch == "shopify" else _next_thursday(posted - timedelta(days=1))
        if p > as_of:
            return
        row.update(channel=ch, settlement_id=sid, posted_date=p.isoformat())
        row["net"] = round(sum(row.get(k, 0) for k in ("principal", "commission", "fixed_fee",
                                                       "shipping_fee", "tcs", "tds", "other")), 2)
        settlements[(ch, sid)].append(row)

    n = 0
    for d in _days(START, END):
        for ch, per_day in (("amazon", 9), ("flipkart", 5), ("shopify", 4)):
            for _ in range(rnd.randint(per_day - 2, per_day + 2)):
                n += 1
                sku = rnd.choice(SKUS)[0]
                qty = 1 if rnd.random() < 0.85 else 2
                gross = round(price[sku] * qty * rnd.choice((1, 1, 0.9, 0.85)), 2)
                oid = {"amazon": "408-", "flipkart": "OD", "shopify": "#"}[ch] + f"{n:07d}"
                r = rnd.random()
                status = ("delivered" if r < 0.81 else "returned" if r < 0.89 else "rto" if r < 0.95 else "cancelled")
                delivered = d + timedelta(days=rnd.randint(2, 6))
                if delivered > as_of and status in ("delivered", "returned"):
                    status = "in_transit"
                if status == "returned" and delivered + timedelta(days=4) > as_of:
                    status = "delivered"
                decl_w = weight[sku] * qty + 80
                cod = ch == "shopify" and rnd.random() < 0.4
                o = dict(channel=ch, order_id=oid, order_date=d.isoformat(),
                         delivered_date=delivered.isoformat() if status in ("delivered", "returned") else "",
                         sku=sku, qty=qty, gross_amount=gross, payment_mode="cod" if cod else "prepaid",
                         status=status, declared_weight_g=decl_w,
                         logistics="shiprocket" if ch == "shopify" else "")
                ret_date = None
                if status == "returned":
                    ret_date = delivered + timedelta(days=rnd.randint(2, 4))
                    x = rnd.random()
                    o.update(return_date=ret_date.isoformat(),
                             return_received="false" if x < 0.15 else "true",
                             return_condition="" if x < 0.15 else ("damaged" if x < 0.30 else
                                                                   "wrong_item" if x < 0.34 else "sellable"))
                out["orders"].append(o)

                taxable = gross / (1 + gst[sku])
                c = cfg[ch]
                if ch in ("amazon", "flipkart"):
                    if status in ("delivered", "returned"):
                        if rnd.random() < 0.015:
                            continue  # planted: never settled
                        comm = c["commission_rate"] * gross * (1.25 if rnd.random() < 0.04 else 1)
                        fixed = c["fixed_fee"] + (15 if rnd.random() < 0.03 else 0)
                        charged_w = decl_w + (600 if rnd.random() < 0.07 else 0)
                        principal = gross - (rnd.choice((75, 120, 199)) if rnd.random() < 0.012 else 0)
                        settle(ch, delivered + timedelta(days=rnd.randint(3, 7)), dict(
                            order_id=oid, txn_type="order", principal=round(principal, 2),
                            commission=-round(comm, 2), fixed_fee=-round(fixed, 2),
                            shipping_fee=-round(c["ship_rate_per_500g"] * slabs(charged_w), 2),
                            tcs=-round(GST_TCS_RATE * taxable, 2), tds=-round(TDS_194O_RATE * taxable, 2),
                            charged_weight_g=charged_w))
                        if status == "returned":
                            settle(ch, ret_date + timedelta(days=1), dict(
                                order_id=oid, txn_type="refund", principal=-gross,
                                commission=round(c["commission_rate"] * gross * 0.8, 2),
                                tcs=round(GST_TCS_RATE * taxable, 2), tds=round(TDS_194O_RATE * taxable, 2)))
                    elif status == "rto":
                        settle(ch, d + timedelta(days=12), dict(
                            order_id=oid, txn_type="order",
                            shipping_fee=-round(c["ship_rate_per_500g"] * slabs(decl_w) * 2, 2),
                            fixed_fee=-round(c["fixed_fee"], 2) if rnd.random() < 0.2 else 0))
                    elif status == "cancelled" and rnd.random() < 0.2:
                        settle(ch, d + timedelta(days=5), dict(
                            order_id=oid, txn_type="order", commission=-round(c["commission_rate"] * gross, 2),
                            fixed_fee=-round(c["fixed_fee"], 2)))
                else:  # shopify: gateway settles prepaid; Shiprocket ships everything
                    if not cod and status in ("delivered", "returned", "in_transit"):
                        settle(ch, d + timedelta(days=2), dict(order_id=oid, txn_type="order", principal=gross,
                                                               commission=-round(0.02 * gross, 2)))
                        if status == "returned":
                            settle(ch, ret_date + timedelta(days=1), dict(order_id=oid, txn_type="refund",
                                                                         principal=-gross))
                    charged_w = decl_w + (500 if rnd.random() < 0.08 else 0)
                    ship_fee = cfg["shiprocket"]["ship_rate_per_500g"] * slabs(charged_w) * (2 if status == "rto" else 1)
                    remitted, rdate = 0.0, ""
                    if cod and status in ("delivered", "returned"):
                        rd = delivered + timedelta(days=rnd.randint(4, 7))
                        x = rnd.random()
                        if rd <= as_of and x > 0.03:
                            remitted = gross if x > 0.05 else round(gross * 0.5, 2)
                            rdate = rd.isoformat()
                    if status != "cancelled":
                        out["logistics"].append(dict(
                            channel="shiprocket", awb=f"SR{n:09d}", order_id=oid, ship_date=(d + timedelta(days=1)).isoformat(),
                            payment_mode="cod" if cod else "prepaid",
                            cod_amount=gross if cod and status in ("delivered", "returned") else 0,
                            remitted_amount=remitted, remittance_date=rdate, shipping_fee=round(ship_fee, 2),
                            charged_weight_g=charged_w))

    # Settlement rows + bank deposits (2 payouts missing, 1 short).
    sids = sorted(settlements, key=lambda k: k[1])
    missing = {sids[len(sids) // 3], sids[2 * len(sids) // 3]}
    short = sids[len(sids) // 2]
    for key in sids:
        rows = settlements[key]
        out["settlements"].extend(rows)
        posted = date.fromisoformat(rows[0]["posted_date"])
        dep = posted + timedelta(days=2)
        if key in missing or dep > as_of:
            continue
        amt = round(sum(r["net"] for r in rows), 2) - (1250 if key == short else 0)
        out["bank_deposits"].append(dict(channel=key[0], settlement_id=key[1], utr=f"UTR{zlib.crc32(key[1].encode()):010d}",
                                         deposit_date=dep.isoformat(), amount=round(amt, 2)))

    # Quick commerce: POs -> GRN -> invoice -> payment (ads + 194Q netted).
    po_n = 0
    fy_cum = defaultdict(float)
    for d in _days(START, END):
        for ch in ("blinkit", "zepto"):
            if d.weekday() not in ((0, 3) if ch == "blinkit" else (1, 4)):
                continue
            po_n += 1
            po = f"{ch[:2].upper()}PO{po_n:05d}"
            stream = "consignment" if ch == "zepto" and po_n % 5 == 0 else "direct_buy"
            grn = d + timedelta(days=4)
            expired = po_n == 7
            lines = []
            for sku in rnd.sample([s[0] for s in SKUS if s[0] != "KC-KIT-GIFT"], 4):
                q = rnd.randrange(24, 132, 12)
                shipped = 0 if expired else int(q * rnd.uniform(0.82, 1.0))
                received = shipped - (rnd.randint(3, 10) if rnd.random() < 0.08 and shipped else 0)
                accepted = received - (rnd.randint(1, 6) if rnd.random() < 0.15 and received else 0)
                unit = round(price[sku] / (1 + gst[sku]) * 0.62, 2)
                lines.append((sku, q, shipped, received, accepted, unit))
                out["purchase_orders"].append(dict(
                    channel=ch, po_number=po, po_date=d.isoformat(), expiry_date=(d + timedelta(days=7)).isoformat(),
                    stream=stream, facility=f"{ch.title()} DC {['GGN', 'BLR', 'MUM'][po_n % 3]}", sku=sku,
                    qty_ordered=q, qty_shipped=shipped, qty_received=received, qty_accepted=accepted,
                    unit_price=unit, grn_date="" if expired or grn > as_of else grn.isoformat()))
            if expired or grn > as_of:
                continue
            taxable = round(sum(a * u for _, _, _, _, a, u in lines), 2)
            g = round(sum(a * u * gst[s] for s, _, _, _, a, u in lines), 2)
            inv = f"INV-{ch[:2].upper()}-{po_n:05d}"
            out["qc_invoices"].append(dict(channel=ch, po_number=po, invoice_number=inv,
                                           invoice_date=grn.isoformat(), taxable_value=taxable,
                                           gst_amount=g, total=round(taxable + g, 2)))
            before = fy_cum[ch]
            fy_cum[ch] += taxable
            tds = round(max(0.0, fy_cum[ch] - max(before, TDS_194Q_THRESHOLD)) * TDS_194Q_RATE, 2)
            if po_n % 23 == 0:
                tds = round(taxable * TDS_194Q_RATE, 2)  # planted: deducted below threshold
            pay = grn + timedelta(days=CHANNEL_DEFAULTS[ch]["settlement_days"])
            if pay > as_of:
                continue
            ads = round(taxable * rnd.uniform(0.06, 0.12) * 1.18, 2)
            net = round(taxable + g - ads - tds - (3500 if po_n % 17 == 0 else 0), 2)
            out["qc_payments"].append(dict(channel=ch, invoice_number=inv, payment_date=pay.isoformat(),
                                           invoice_amount=round(taxable + g, 2), ad_deduction=ads,
                                           tds_194q=tds, other_deduction=0, net_paid=net,
                                           utr=f"QCUTR{po_n:08d}"))

    # Government portal data for Aug & Sep (Oct GSTR-2B / 26AS not out yet).
    tax = defaultdict(float)
    for r in out["settlements"]:
        if r["txn_type"] in ("order", "refund"):
            per = r["posted_date"][:7]
            tax[(r["channel"], per, "TCS")] += -r.get("tcs", 0)
            tax[(r["channel"], per, "194O")] += -r.get("tds", 0)
    for p in out["qc_payments"]:
        tax[(p["channel"], p["payment_date"][:7], "194Q")] += p["tds_194q"]
    for (ch, per, sec), amt in sorted(tax.items()):
        if per not in ("2026-08", "2026-09") or ch == "shopify":
            continue
        if (ch, per, sec) == ("flipkart", "2026-09", "TCS"):
            amt *= 0.6  # planted: Flipkart under-reported TCS in GSTR-8
        if (ch, per, sec) == ("amazon", "2026-08", "194O"):
            amt *= 0.5  # planted: 26AS short
        out["tax_statements"].append(dict(channel=ch, period=per, section=sec, amount=round(amt, 2)))
    return out


def to_csv(kind: str, rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=TEMPLATES[kind], extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in TEMPLATES[kind]})
    return buf.getvalue()


ORDER = ["channels", "skus", "orders", "settlements", "bank_deposits", "logistics",
         "purchase_orders", "qc_invoices", "qc_payments", "tax_statements"]


def seed_demo(s: Session, name: str = "Kesar & Co. (demo)", as_of: date = DEMO_AS_OF,
              write_dir: Path | None = None) -> int:
    data = generate(as_of)
    brand = Brand(name=name, gstin="27ABCDE1234F1Z5")
    s.add(brand)
    s.commit()
    for kind in ORDER:
        text = to_csv(kind, data[kind])
        if write_dir:
            write_dir.mkdir(parents=True, exist_ok=True)
            (write_dir / f"{kind}.csv").write_text(text)
        import_csv(s, brand.id, kind, text)
    return brand.id


if __name__ == "__main__":
    from app.db import SessionLocal, init_db
    from app.pipeline import run_all

    init_db()
    with SessionLocal() as s:
        bid = seed_demo(s, write_dir=Path(__file__).resolve().parent.parent / "sample_data")
        print(run_all(s, bid, DEMO_AS_OF))
