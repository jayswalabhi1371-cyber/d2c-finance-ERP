# D2C Finance OS

Finance operating system for Indian D2C brands: **find, recover and book every rupee
your marketplaces owe you — and turn it into working capital.**

Built from the gaps in *D2C Finance Ops — Market Survey & Founder Tech Roadmap*. Today's
tools each solve one slice (OMS reconciliation add-ons, accounting-first recon, analytics,
RBF). This product connects them in one ledger, from marketplace settlement through closed
books, tax credits, profit and capital.

## The 8 gaps → what the software does

| # | Gap (from the survey) | Module | Code |
|---|---|---|---|
| 1 | No single source of truth from settlement to ledger | Every order, settlement, refund, payout, COD remittance and QC invoice is posted as balanced double-entry journals tagged by channel + SKU. Order→settlement and settlement→bank matching. Tally XML and journal-CSV export. | `app/engines/ledger.py`, `recovery.py` (`reconcile_orders`, `settlement_to_bank`) |
| 2 | Detection without recovery | 12 leak rules → **claims** with filing deadlines, urgency, ready-to-paste ticket text, workflow `open → filed → recovered/rejected`, success-fee tracking. Recovered money posts to the books automatically. | `app/engines/recovery.py` |
| 3 | Quick commerce is a different, messier problem | PO → ASN → GRN → invoice → payment chain per PO; fill rate, transit loss, GRN rejections, short payment, expired POs; three streams (direct-buy, consignment, ad spend netted); 194Q with the ₹50 lakh FY threshold. | `app/engines/quickcommerce.py` |
| 4 | Tax credits leak quietly | Per channel × month × section: **computed** (law) vs **deducted** (platform) vs **portal** (GSTR-2B TCS / Form 26AS TDS). Flags not-in-portal, over-deducted, missing portal data; at-risk credits become claims. | `app/engines/tax.py` |
| 5 | Profit numbers don't tie to the books | SKU CM1/CM2/CM3 computed **from the same journal lines** as the P&L; untagged costs allocated by revenue share; tie-out table proves SKU totals = P&L. | `app/engines/profitability.py` |
| 6 | Capital priced on revenue, not verified receivables | Receivables book where every rupee traces to a settlement, delivered order, QC invoice or COD shipment; aging; advance rates; concentration cap; overdue/disputed excluded; lender-pack CSV. | `app/engines/capital.py` |
| 7 | Mid-size brands fall in the middle | Multi-tenant by `brand_id`; Supabase row-level security; per-channel rate cards and windows configurable; runs on SQLite (pilot) or Postgres (production). | `app/models.py`, `supabase/migrations/` |
| 8 | Audit & fundraising readiness | Month-end close checklist (8 automated checks), period **lock** (journals frozen, snapshot stored, DB trigger blocks edits), one-click **audit pack** ZIP: TB, P&L, verified receivables, unreconciled items, bank recon, claims register, tax recon, SKU P&L, journal, Tally XML. | `app/engines/close.py` |

### Leak rules (gap 2)
`commission_overcharge` · `fixed_fee_overcharge` · `weight_overcharge` (marketplace + 3PL) ·
`short_payment` · `unsettled_order` · `return_not_received` (SAFE-T / SPF style; D2C returns
routed to the courier) · `fees_on_cancelled` · `cod_not_remitted` · `payout_not_in_bank` ·
`qc_transit_loss` · `qc_short_payment` · `tax_credit_missing`

## Quickstart

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload
# open http://localhost:8000 → "Load demo brand"
```

The demo brand is fictional ("Kesar & Co."). It has 3 months of data (Aug–Oct 2026) across
Amazon, Flipkart, Shopify + Shiprocket (COD), Blinkit and Zepto, with leaks planted at known
rates: about 1,650 orders, 1,370 settlement lines and 52 quick-commerce POs. It is generated
as CSVs and loaded through the normal importers. Run `python -m app.seed` to write them to
`sample_data/`.

Tests (13; they also run against Postgres with `DATABASE_URL=postgresql+psycopg://...`):

```bash
.venv/bin/python -m pytest -q
```

API docs: http://localhost:8000/docs

## Loading a real brand

1. *Import data → New brand.*
2. Import, in this order (templates downloadable in the UI and at `/api/templates/<kind>.csv`):
   `channels` (your actual rate cards) → `skus` (landed cost, GST rate, weight, opening stock) →
   `orders` → `settlements` **or** `amazon_settlement_v2` (Amazon's native flat file) →
   `bank_deposits` → `logistics` → `purchase_orders` / `qc_invoices` / `qc_payments` →
   `tax_statements` (from GSTR-2B and Form 26AS).
3. Engines re-run after every upload. Re-uploading a file is safe (upserts).

For your first pilots, map each client's exports into the templates (a pandas notebook is fine).
Add native adapters (Flipkart, Myntra, Blinkit, Zepto, Shiprocket) one at a time as clients
need them, following `_import_amazon_settlement_v2` in `app/ingest/parsers.py`.

## Production on Supabase

```bash
supabase db push        # applies supabase/migrations/0001_init.sql
export DATABASE_URL="postgresql+psycopg://postgres:<password>@db.<project>.supabase.co:5432/postgres"
```

The migration creates all tables, a `brand_members` table (owner/accountant/viewer), and RLS
policies on every table. A trigger rejects edits to journals in a locked month. It was checked
on Postgres 16 with a stub `auth` schema.

## Architecture

```
CSV / API feeds ──► ingest/parsers.py ──► canonical tables (models.py)
                                              │
               pipeline.run_all():  quickcommerce ─┐
                                    tax ───────────┼─► recovery.detect_leaks ─► claims
                                    ledger.rebuild_ledger ◄──────────────────────┘
                                              │
           reports: reconciliation · claims · QC · tax · TB/P&L/journal/Tally
                    · SKU profitability · borrowing base · close checklist · audit pack
                                              │
                         FastAPI (main.py) ──► dashboard (static/index.html)
```

Design rules: one ledger feeds every report, so nothing can disagree with the books.
Engines are idempotent, so you can re-run after every import. Claim status and locked
periods survive re-runs. Statutory rates and rate cards live in `app/config.py`.

## Known limitations — next things to build

- **Money is stored as floats, rounded to paise.** Move to `NUMERIC(14,2)`/`Decimal` before
  the first paying client.
- **No auth in the FastAPI layer yet.** RLS exists in the database; wire Supabase Auth JWTs
  into the API before going live.
- **Live connectors** (Amazon SP-API, Shopify API, Flipkart) are not built; data comes in as CSV.
- **Claims are not filed automatically.** The software prepares the ticket text, and a person
  (or a future browser/API automation) files it.
- **Consignment POs** are recognised on GRN like direct-buy. True consignment revenue is
  recognised on sell-through, which needs a sell-through report import.
- **Tax rates and section numbers** (TCS 0.5%, 194-O 0.1%, 194Q 0.1% above ₹50 lakh) are
  current planning defaults. The Income-tax Act, 2025 renumbers these sections. Have a CA
  verify `app/config.py`.
- **Tally import** creates journal vouchers summarised by day and source. Ledger masters
  (names in `ACCOUNTS`) must already exist in Tally.
- **Lending** must go through an RBI-regulated NBFC/bank partner under the digital-lending
  rules. This software supplies verified data; it never holds or routes loan money.
