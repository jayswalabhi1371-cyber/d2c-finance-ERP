"""Business rules and statutory rates.

Every number here is a planning default. Verify against current law and each
marketplace's current policy before relying on it for a client's books.

Statutory notes (India):
- GST TCS u/s 52 CGST Act: 0.5% of net taxable value (0.25% CGST + 0.25% SGST,
  or 0.5% IGST) since 10 Jul 2024; reflected via GSTR-8 into the seller's GSTR-2B
  / electronic cash ledger.
- Income-tax TDS on e-commerce payments (old section 194-O): 0.1% since 1 Oct 2024.
- Income-tax TDS on purchase of goods (old section 194Q): 0.1% on amounts above
  Rs 50 lakh per buyer per financial year. Quick-commerce platforms that buy
  outright (direct-buy / B2B) typically deduct this.
- The Income-tax Act, 2025 (in force from 1 Apr 2026) renumbers these sections.
  We keep the familiar "194-O" / "194Q" labels in the UI; map them to the new
  section references in your filings.
"""

GST_TCS_RATE = 0.005
TDS_194O_RATE = 0.001
TDS_194Q_RATE = 0.001
TDS_194Q_THRESHOLD = 5_000_000  # Rs 50 lakh per buyer per FY
GST_ON_FEES = 0.18  # GST charged by marketplaces/logistics on their fees

AMOUNT_TOLERANCE = 1.0  # Rs; differences below this are treated as rounding

# Default channel configuration. Brands override these per channel in the DB.
#   commission_rate:     rate-card commission as a share of principal (incl. GST)
#   fixed_fee:           per-order closing/fixed fee incl. GST
#   ship_rate_per_500g:  shipping fee per 500 g slab incl. GST
#   settlement_days:     days after delivery by which payment is due
#   claim_window_days:   days after the event within which a claim must be filed
CHANNEL_DEFAULTS = {
    "amazon": dict(kind="marketplace", commission_rate=0.12, fixed_fee=25.0,
                   ship_rate_per_500g=45.0, settlement_days=14, claim_window_days=45),
    "flipkart": dict(kind="marketplace", commission_rate=0.14, fixed_fee=20.0,
                     ship_rate_per_500g=50.0, settlement_days=10, claim_window_days=45),
    "myntra": dict(kind="marketplace", commission_rate=0.20, fixed_fee=30.0,
                   ship_rate_per_500g=55.0, settlement_days=15, claim_window_days=30),
    "shopify": dict(kind="d2c", commission_rate=0.02, fixed_fee=0.0,
                    ship_rate_per_500g=0.0, settlement_days=3, claim_window_days=30),
    "shiprocket": dict(kind="logistics", commission_rate=0.0, fixed_fee=0.0,
                       ship_rate_per_500g=40.0, settlement_days=8, claim_window_days=30),
    "blinkit": dict(kind="quick_commerce", commission_rate=0.0, fixed_fee=0.0,
                    ship_rate_per_500g=0.0, settlement_days=30, claim_window_days=60),
    "zepto": dict(kind="quick_commerce", commission_rate=0.0, fixed_fee=0.0,
                  ship_rate_per_500g=0.0, settlement_days=30, claim_window_days=60),
    "instamart": dict(kind="quick_commerce", commission_rate=0.0, fixed_fee=0.0,
                      ship_rate_per_500g=0.0, settlement_days=45, claim_window_days=60),
}

# Recovery-as-a-service success fee (share of money actually recovered).
RECOVERY_SUCCESS_FEE = 0.20

# Borrowing base advance rates on verified receivables (for NBFC partners).
ADVANCE_RATES = {
    "settled_awaiting_payout": 0.90,  # marketplace has confirmed the amount
    "delivered_within_sla": 0.75,     # delivered, not yet settled, still within SLA
    "overdue": 0.0,                   # past SLA: excluded until resolved
    "under_claim": 0.0,               # disputed: excluded
}
MAX_SINGLE_CHANNEL_SHARE = 0.60  # concentration limit on the borrowing base

# Chart of accounts. Codes are stable; names are what Tally/Zoho will see.
ACCOUNTS = {
    "BANK": "Bank Account",
    "INVENTORY": "Stock-in-Hand",
    "GST_OUTPUT": "Output GST",
    "GST_INPUT": "Input GST",
    "TCS_RECEIVABLE": "GST TCS Receivable",
    "TDS_RECEIVABLE": "TDS Receivable",
    "CLAIMS_RECEIVABLE": "Marketplace Claims Receivable",
    "SALES": "Sales",
    "SALES_RETURNS": "Sales Returns",
    "COGS": "Cost of Goods Sold",
    "COMMISSION": "Marketplace Commission",
    "FIXED_FEES": "Marketplace Fixed Fees",
    "SHIPPING": "Shipping & Logistics",
    "ADS": "Advertising (Platform)",
    "OTHER_FEES": "Other Platform Charges",
    "CLAIMS_INCOME": "Claims Recovery Income",
    "CAPITAL": "Capital Account",
}
# AR accounts are per channel: "AR:<channel code>" -> "<Channel> Receivable"
INCOME_ACCOUNTS = {"SALES", "CLAIMS_INCOME"}
CONTRA_INCOME_ACCOUNTS = {"SALES_RETURNS"}
EXPENSE_ACCOUNTS = {"COGS", "COMMISSION", "FIXED_FEES", "SHIPPING", "ADS", "OTHER_FEES"}


def account_name(code: str) -> str:
    if code.startswith("AR:"):
        return f"{code[3:].title()} Receivable"
    return ACCOUNTS[code]
