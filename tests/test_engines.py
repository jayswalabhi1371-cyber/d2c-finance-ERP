import io
import os
import zipfile
from datetime import date

import pytest

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_d2c_erp.db")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import Base, SessionLocal, engine, init_db  # noqa: E402
from app.engines import capital, close, ledger, profitability, quickcommerce, recovery, tax  # noqa: E402
from app.ingest.parsers import import_csv  # noqa: E402
from app.models import Brand, Claim, JournalEntry, SettlementLine  # noqa: E402
from app.pipeline import run_all  # noqa: E402
from app.seed import DEMO_AS_OF, seed_demo  # noqa: E402

A = DEMO_AS_OF


@pytest.fixture(scope="module")
def demo():
    Base.metadata.drop_all(engine)
    init_db()
    s = SessionLocal()
    bid = seed_demo(s)
    run_all(s, bid, A)
    yield s, bid
    s.close()


def test_every_planted_leak_type_is_detected(demo):
    s, bid = demo
    rules = {r["rule"] for r in recovery.claims_summary(s, bid, A)["by_rule"]}
    assert rules >= {"commission_overcharge", "fixed_fee_overcharge", "weight_overcharge", "short_payment",
                     "unsettled_order", "return_not_received", "fees_on_cancelled", "cod_not_remitted",
                     "payout_not_in_bank", "qc_transit_loss", "qc_short_payment", "tax_credit_missing"}


def test_planted_bank_gaps(demo):
    s, bid = demo
    bad = [r for r in recovery.settlement_to_bank(s, bid, A) if r["status"] in ("missing_in_bank", "amount_mismatch")]
    assert len(bad) == 3
    assert any(r["status"] == "amount_mismatch" and abs(r["difference"] - 1250) < 0.01 for r in bad)


def test_books_balance_and_every_entry_balances(demo):
    s, bid = demo
    assert ledger.trial_balance(s, bid)["balanced"]
    for e in s.scalars(select(JournalEntry).where(JournalEntry.brand_id == bid)):
        assert abs(sum(l.debit for l in e.lines) - sum(l.credit for l in e.lines)) < 0.01, e.source_ref


def test_sku_profitability_ties_to_books(demo):
    s, bid = demo
    p = profitability.sku_profitability(s, bid)
    assert p["ties_to_books"]
    pnl = ledger.profit_and_loss(s, bid)
    assert abs(p["totals"]["net_revenue"] - pnl["net_revenue"]) < 1
    assert abs(p["totals"]["cm3"] - pnl["net_profit"]) < 1  # no other income yet


def test_tax_credit_mismatches_found(demo):
    s, bid = demo
    rows = {(r["channel"], r["period"], r["section"]): r for r in tax.reconcile_tax_credits(s, bid)["rows"]}
    assert rows[("flipkart", "2026-09", "TCS")]["status"] == "not_in_portal"
    assert rows[("amazon", "2026-08", "194O")]["status"] == "not_in_portal"
    assert rows[("amazon", "2026-09", "TCS")]["status"] == "matched"
    assert rows[("amazon", "2026-10", "TCS")]["status"] == "portal_data_missing"


def test_quick_commerce_chain(demo):
    s, bid = demo
    qc = quickcommerce.reconcile_quick_commerce(s, bid, A)
    flags = {f for r in qc["pos"] for f in r["flags"]}
    assert {"transit_loss", "short_payment", "po_expired_unfulfilled"} <= flags
    assert all(c["ad_spend_netted"] > 0 for c in qc["channels"])


def test_194q_threshold():
    from types import SimpleNamespace as NS
    invs = [NS(channel_id=1, invoice_date=date(2026, 5, 1), invoice_number="A", taxable_value=4_000_000),
            NS(channel_id=1, invoice_date=date(2026, 6, 1), invoice_number="B", taxable_value=2_000_000),
            NS(channel_id=1, invoice_date=date(2026, 7, 1), invoice_number="C", taxable_value=1_000_000)]
    exp = quickcommerce.expected_194q_by_invoice(invs)
    assert exp["A"] == 0 and round(exp["B"], 2) == 1000.0 and round(exp["C"], 2) == 1000.0


def test_borrowing_base_excludes_overdue_and_disputes(demo):
    s, bid = demo
    bb = capital.borrowing_base(s, bid, A)
    t = bb["totals"]
    assert 0 < t["borrowing_base"] < t["verified_receivables"]
    for r in bb["rows"]:
        assert r["borrowing_base"] <= 0.9 * r["settled_awaiting_payout"] + 0.75 * r["delivered_within_sla"] + 0.01


def test_claim_lifecycle_posts_recovery_income(demo):
    s, bid = demo
    c = s.scalar(select(Claim).where(Claim.brand_id == bid, Claim.rule == "commission_overcharge"))
    c.status, c.recovered_amount, c.recovered_date = "recovered", c.leak_amount, date(2026, 10, 20)
    s.commit()
    ledger.rebuild_ledger(s, bid)
    s.commit()
    assert ledger.profit_and_loss(s, bid)["other_income"] == pytest.approx(c.leak_amount)
    run_all(s, bid, A)  # re-detection must not reset a recovered claim
    assert s.get(Claim, c.id).status == "recovered"


def test_locked_period_is_frozen(demo):
    s, bid = demo
    before = ledger.profit_and_loss(s, bid, date(2026, 8, 1), date(2026, 8, 31))
    assert close.lock_period(s, bid, "2026-08", A, force=True)["locked"]
    s.commit()
    # mutate an August settlement; a locked month must not change on re-run
    sl = s.scalar(select(SettlementLine).where(SettlementLine.brand_id == bid,
                                               SettlementLine.posted_date < date(2026, 9, 1),
                                               SettlementLine.commission < 0))
    sl.commission -= 1000
    s.commit()
    run_all(s, bid, A)
    after = ledger.profit_and_loss(s, bid, date(2026, 8, 1), date(2026, 8, 31))
    assert after["net_profit"] == before["net_profit"]
    close.unlock_period(s, bid, "2026-08")
    sl.commission += 1000
    s.commit()
    run_all(s, bid, A)


def test_audit_pack_contents(demo):
    s, bid = demo
    z = zipfile.ZipFile(io.BytesIO(close.audit_pack(s, bid, "2026-09", A)))
    names = set(z.namelist())
    assert {"00_README.txt", "02_trial_balance.csv", "07_claims_register.csv", "11_tally_import.xml"} <= names
    assert z.read("11_tally_import.xml").startswith(b"<ENVELOPE>")


def test_amazon_v2_adapter():
    s = SessionLocal()
    b = Brand(name="AmzTest")
    s.add(b)
    s.commit()
    flat = ("settlement-id\ttransaction-type\torder-id\tamount-type\tamount-description\tamount\tposted-date\n"
            "S1\tOrder\t408-1\tItemPrice\tPrincipal\t500.00\t2026-09-01\n"
            "S1\tOrder\t408-1\tItemPrice\tTax\t90.00\t2026-09-01\n"
            "S1\tOrder\t408-1\tItemFees\tCommission\t-70.80\t2026-09-01\n"
            "S1\tOrder\t408-1\tItemFees\tFixed closing fee\t-25.00\t2026-09-01\n"
            "S1\tOrder\t408-1\tItemWithheldTax\tTCS-IGST\t-2.50\t2026-09-01\n")
    import_csv(s, b.id, "amazon_settlement_v2", flat)
    sl = s.scalar(select(SettlementLine).where(SettlementLine.brand_id == b.id))
    assert (sl.principal, sl.commission, sl.fixed_fee, sl.tcs) == (590.0, -70.8, -25.0, -2.5)
    assert sl.net == pytest.approx(491.7)
    s.close()


def test_api_smoke(demo):
    from app.main import app
    _, bid = demo
    c = TestClient(app)
    for path in ["/api/brands", f"/api/brands/{bid}/overview", f"/api/brands/{bid}/claims",
                 f"/api/brands/{bid}/reconciliation/orders", f"/api/brands/{bid}/reconciliation/bank",
                 f"/api/brands/{bid}/quick-commerce", f"/api/brands/{bid}/tax-credits",
                 f"/api/brands/{bid}/books/trial-balance?period=2026-09", f"/api/brands/{bid}/books/pnl",
                 f"/api/brands/{bid}/profitability?by_channel=true", f"/api/brands/{bid}/capital",
                 f"/api/brands/{bid}/close/2026-09", f"/api/brands/{bid}/export/tally.xml?period=2026-09",
                 f"/api/brands/{bid}/audit-pack/2026-09.zip", "/"]:
        r = c.get(path + ("&" if "?" in path else "?") + f"as_of={A}")
        assert r.status_code == 200, (path, r.text[:300])
    claim_id = c.get(f"/api/brands/{bid}/claims?as_of={A}&status=open").json()["rows"][0]["id"]
    r = c.patch(f"/api/brands/{bid}/claims/{claim_id}", json={"status": "filed", "ticket_ref": "CASE-1"})
    assert r.json()["status"] == "filed"
    bad = c.post(f"/api/brands/{bid}/import/orders", files={"file": ("x.csv", b"foo,bar\n1,2\n")})
    assert bad.status_code == 400


def test_password_gate(demo, monkeypatch):
    import base64

    import app.main as m
    monkeypatch.setattr(m, "APP_PASSWORD", "s3cret")
    c = TestClient(m.app)
    assert c.get("/healthz").status_code == 200
    assert c.get("/api/brands").status_code == 401
    bad = base64.b64encode(b"admin:wrong").decode()
    assert c.get("/api/brands", headers={"Authorization": f"Basic {bad}"}).status_code == 401
    good = base64.b64encode(b"admin:s3cret").decode()
    assert c.get("/api/brands", headers={"Authorization": f"Basic {good}"}).status_code == 200


def test_demo_can_be_disabled(demo, monkeypatch):
    import app.main as m
    monkeypatch.setattr(m, "ENABLE_DEMO", False)
    assert TestClient(m.app).post("/api/demo").status_code == 403
