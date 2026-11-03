"""Gap 5 — SKU profitability that ties to the books.

Built from the same journal lines as the P&L, never from a separate analytics
copy. Costs without a SKU tag (e.g. quick-commerce ad spend netted against a
payment) are allocated by revenue share within their channel; anything still
unallocatable is shown as its own row, so SKU totals always equal the P&L.
"""
from collections import defaultdict
from datetime import date

from sqlalchemy.orm import Session

from app.engines.common import r2, skus_by_code
from app.engines.ledger import balances

LINES = [("gross_sales", "SALES", True), ("returns", "SALES_RETURNS", False),
         ("cogs", "COGS", False), ("commission", "COMMISSION", False),
         ("fixed_fees", "FIXED_FEES", False), ("shipping", "SHIPPING", False),
         ("ads", "ADS", False), ("other_fees", "OTHER_FEES", False)]
ACCT_TO_LINE = {a: (k, credit_normal) for k, a, credit_normal in LINES}


def sku_profitability(s: Session, brand_id: int, start: date | None = None, end: date | None = None,
                      by_channel: bool = False) -> dict:
    raw = balances(s, brand_id, start, end, by=("account", "channel_code", "sku_code"))
    cells = defaultdict(lambda: defaultdict(float))  # (sku, channel) -> line -> amount
    untagged = defaultdict(lambda: defaultdict(float))  # channel -> line -> amount
    for (acct, ch, sku), (dr, cr) in raw.items():
        if acct not in ACCT_TO_LINE:
            continue
        line, credit_normal = ACCT_TO_LINE[acct]
        amt = (cr - dr) if credit_normal else (dr - cr)
        if sku:
            cells[(sku, ch)][line] += amt
        else:
            untagged[ch][line] += amt

    # Allocate untagged costs within each channel by net revenue share.
    unallocated = defaultdict(float)
    for ch, lines in untagged.items():
        rev = {k: v["gross_sales"] - v["returns"] for k, v in cells.items() if k[1] == ch}
        total = sum(rev.values())
        for line, amt in lines.items():
            if total > 0:
                for k, r in rev.items():
                    cells[k][line] += amt * r / total
            else:
                unallocated[line] += amt

    skus = skus_by_code(s, brand_id)
    grouped = defaultdict(lambda: defaultdict(float))
    for (sku, ch), lines in cells.items():
        key = (sku, ch) if by_channel else (sku, None)
        for line, amt in lines.items():
            grouped[key][line] += amt

    def finish(d: dict, sku: str, ch: str | None, name: str) -> dict:
        net = d["gross_sales"] - d["returns"]
        cm1 = net - d["cogs"]
        cm2 = cm1 - d["commission"] - d["fixed_fees"] - d["shipping"]
        cm3 = cm2 - d["ads"] - d["other_fees"]
        row = {"sku": sku, "name": name, "channel": ch}
        row.update({k: r2(d[k]) for k, _, _ in LINES})
        row.update({"net_revenue": r2(net), "cm1": r2(cm1), "cm2": r2(cm2), "cm3": r2(cm3),
                    "cm1_pct": round(cm1 / net, 4) if net else 0,
                    "cm2_pct": round(cm2 / net, 4) if net else 0,
                    "cm3_pct": round(cm3 / net, 4) if net else 0})
        return row

    rows = [finish(d, sku, ch, skus[sku].name if sku in skus else sku)
            for (sku, ch), d in grouped.items()]
    if any(abs(v) > 0.005 for v in unallocated.values()):
        rows.append(finish(unallocated, "UNALLOCATED", None, "Not attributable to a SKU"))
    rows.sort(key=lambda r: -r["cm3"])

    # Tie-out: SKU totals vs P&L accounts.
    acct_bal = balances(s, brand_id, start, end)
    tie = []
    for line, acct, credit_normal in LINES:
        sku_total = r2(sum(r[line] for r in rows))
        dr, cr = acct_bal.get(acct, (0.0, 0.0))
        book = r2(cr - dr if credit_normal else dr - cr)
        tie.append({"line": line, "account": acct, "sku_total": sku_total, "books": book,
                    "difference": r2(sku_total - book)})
    return {"rows": rows, "tie_out": tie, "ties_to_books": all(abs(t["difference"]) < 1 for t in tie),
            "totals": {k: r2(sum(r[k] for r in rows)) for k in ("net_revenue", "cm1", "cm2", "cm3")}}
