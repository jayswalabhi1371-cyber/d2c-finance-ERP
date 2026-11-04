"""HTTP API + dashboard.

    uvicorn app.main:app --reload      # then open http://localhost:8000
"""
import base64
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_session, init_db
from app.engines import capital, close, ledger, profitability, quickcommerce, recovery, tax
from app.engines.common import channels_by_id, period_bounds
from app.ingest.parsers import TEMPLATES, ImportError_, import_csv, template_csv
from app.models import Brand, Channel, Claim
from app.pipeline import run_all
from app.seed import DEMO_AS_OF, seed_demo

STATIC = Path(__file__).resolve().parent.parent / "static"


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(lifespan=lifespan, title="D2C Finance OS", version="0.1.0",
              description="Settlement-to-ledger reconciliation, recovery, quick commerce, tax credits, "
                          "SKU profitability, receivable-backed capital and month-end close for D2C brands.")

# ---- Deployment settings (environment variables) ---------------------------------
# APP_PASSWORD  set it in production: every page and API call then needs this password
#               (HTTP Basic auth; user name APP_USERNAME, default "admin"). Interim gate
#               until per-user Supabase Auth is wired in.
# ENABLE_DEMO   "true" to allow the "Load demo brand" button (default: on only when no
#               password is set, i.e. local development).
APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
ENABLE_DEMO = os.environ.get("ENABLE_DEMO", "false" if APP_PASSWORD else "true").lower() == "true"


@app.middleware("http")
async def password_gate(request: Request, call_next):
    if APP_PASSWORD and request.url.path != "/healthz":
        ok = False
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                user, _, pw = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, APP_USERNAME) & secrets.compare_digest(pw, APP_PASSWORD)
            except (ValueError, UnicodeDecodeError):
                ok = False
        if not ok:
            return Response("Authentication required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="D2C Finance OS"'})
    return await call_next(request)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}


@app.get("/api/config")
def config():
    return {"demo_enabled": ENABLE_DEMO}


def _brand(s: Session, brand_id: int) -> Brand:
    b = s.get(Brand, brand_id)
    if not b:
        raise HTTPException(404, "Brand not found")
    return b


def _range(period: str | None):
    return period_bounds(period) if period else (None, None)


AsOf = Query(default=None, description="Valuation date (YYYY-MM-DD); defaults to today")


def _as_of(as_of: date | None) -> date:
    return as_of or date.today()


# ---- Brands & data ------------------------------------------------------------

class BrandIn(BaseModel):
    name: str
    gstin: str | None = None


@app.get("/api/brands")
def list_brands(s: Session = Depends(get_session)):
    return [{"id": b.id, "name": b.name, "gstin": b.gstin} for b in s.scalars(select(Brand))]


@app.post("/api/brands")
def create_brand(body: BrandIn, s: Session = Depends(get_session)):
    b = Brand(name=body.name, gstin=body.gstin)
    s.add(b)
    s.commit()
    return {"id": b.id, "name": b.name}


@app.post("/api/demo")
def create_demo(s: Session = Depends(get_session)):
    if not ENABLE_DEMO:
        raise HTTPException(403, "Demo data is disabled on this server (set ENABLE_DEMO=true to allow).")
    bid = seed_demo(s)
    result = run_all(s, bid, DEMO_AS_OF)
    return {"brand_id": bid, "as_of": DEMO_AS_OF.isoformat(), **result}


@app.get("/api/templates")
def templates():
    return TEMPLATES


@app.get("/api/templates/{kind}.csv", response_class=PlainTextResponse)
def template(kind: str):
    if kind not in TEMPLATES:
        raise HTTPException(404, "Unknown template")
    return template_csv(kind)


@app.post("/api/brands/{brand_id}/import/{kind}")
async def upload(brand_id: int, kind: str, file: UploadFile = File(...), as_of: date | None = AsOf,
                 s: Session = Depends(get_session)):
    _brand(s, brand_id)
    try:
        res = import_csv(s, brand_id, kind, await file.read())
    except (ImportError_, KeyError, ValueError) as e:
        s.rollback()
        raise HTTPException(400, str(e))
    return {**res, "pipeline": run_all(s, brand_id, _as_of(as_of))}


@app.post("/api/brands/{brand_id}/run")
def run(brand_id: int, as_of: date | None = AsOf, s: Session = Depends(get_session)):
    _brand(s, brand_id)
    return run_all(s, brand_id, _as_of(as_of))


@app.get("/api/brands/{brand_id}/channels")
def get_channels(brand_id: int, s: Session = Depends(get_session)):
    return [{c: getattr(ch, c) for c in ("id", "code", "name", "kind", "commission_rate", "fixed_fee",
                                         "ship_rate_per_500g", "settlement_days", "claim_window_days")}
            for ch in s.scalars(select(Channel).where(Channel.brand_id == brand_id))]


# ---- Overview -------------------------------------------------------------------

@app.get("/api/brands/{brand_id}/overview")
def overview(brand_id: int, as_of: date | None = AsOf, s: Session = Depends(get_session)):
    b = _brand(s, brand_id)
    a = _as_of(as_of)
    cs = recovery.claims_summary(s, brand_id, a)
    pnl = ledger.profit_and_loss(s, brand_id)
    bb = capital.borrowing_base(s, brand_id, a)
    tx = tax.reconcile_tax_credits(s, brand_id, a)["totals"]
    recon = recovery.reconcile_orders(s, brand_id, a)
    exceptions = sum(r["recon_status"] in close.EXCEPTION_STATUSES for r in recon)
    periods = sorted({r["order_date"][:7] for r in recon})
    return {
        "brand": {"id": b.id, "name": b.name}, "as_of": a.isoformat(), "periods": periods,
        "leakage": cs, "pnl": pnl,
        "leak_pct_of_revenue": round(cs["total_found"] / pnl["net_revenue"], 4) if pnl["net_revenue"] else 0,
        "capital": bb["totals"], "tax": tx,
        "orders": {"total": len(recon), "exceptions": exceptions,
                   "auto_matched_pct": round(1 - exceptions / len(recon), 4) if recon else 0},
        "close": [close.close_checklist(s, brand_id, p, a) | {"checks": None} for p in periods],
    }


# ---- Gap 1/2: reconciliation & recovery -------------------------------------------

@app.get("/api/brands/{brand_id}/reconciliation/orders")
def recon_orders(brand_id: int, as_of: date | None = AsOf, status: str | None = None,
                 channel: str | None = None, limit: int = 500, s: Session = Depends(get_session)):
    rows = recovery.reconcile_orders(s, brand_id, _as_of(as_of))
    counts: dict = {}
    for r in rows:
        counts[r["recon_status"]] = counts.get(r["recon_status"], 0) + 1
    rows = [r for r in rows if (not status or r["recon_status"] == status) and (not channel or r["channel"] == channel)]
    return {"counts": counts, "total": len(rows), "rows": rows[:limit]}


@app.get("/api/brands/{brand_id}/reconciliation/bank")
def recon_bank(brand_id: int, as_of: date | None = AsOf, s: Session = Depends(get_session)):
    return recovery.settlement_to_bank(s, brand_id, _as_of(as_of))


@app.get("/api/brands/{brand_id}/claims")
def list_claims(brand_id: int, as_of: date | None = AsOf, status: str | None = None, rule: str | None = None,
                s: Session = Depends(get_session)):
    a = _as_of(as_of)
    chans = channels_by_id(s, brand_id)
    q = select(Claim).where(Claim.brand_id == brand_id)
    if status:
        q = q.where(Claim.status == status)
    if rule:
        q = q.where(Claim.rule == rule)
    rows = [recovery.claim_dict(c, chans, a) for c in s.scalars(q)]
    rows.sort(key=lambda r: ({"urgent": 0, "ok": 1, "expired": 2, "-": 3}[r["urgency"]], -r["leak_amount"]))
    return {"summary": recovery.claims_summary(s, brand_id, a), "rows": rows}


class ClaimUpdate(BaseModel):
    status: str
    ticket_ref: str | None = None
    recovered_amount: float | None = None
    recovered_date: date | None = None
    notes: str | None = None


@app.patch("/api/brands/{brand_id}/claims/{claim_id}")
def update_claim(brand_id: int, claim_id: int, body: ClaimUpdate, s: Session = Depends(get_session)):
    c = s.get(Claim, claim_id)
    if not c or c.brand_id != brand_id:
        raise HTTPException(404, "Claim not found")
    if body.status not in ("open", "filed", "recovered", "rejected"):
        raise HTTPException(400, "status must be open|filed|recovered|rejected")
    c.status = body.status
    for k in ("ticket_ref", "notes"):
        if getattr(body, k) is not None:
            setattr(c, k, getattr(body, k))
    if body.status == "recovered":
        c.recovered_amount = body.recovered_amount if body.recovered_amount is not None else c.leak_amount
        c.recovered_date = body.recovered_date or date.today()
    else:
        c.recovered_amount, c.recovered_date = 0, None
    s.commit()
    ledger.rebuild_ledger(s, brand_id)
    s.commit()
    return recovery.claim_dict(c, channels_by_id(s, brand_id), date.today())


@app.get("/api/brands/{brand_id}/claims/{claim_id}/ticket", response_class=PlainTextResponse)
def claim_ticket(brand_id: int, claim_id: int, s: Session = Depends(get_session)):
    c = s.get(Claim, claim_id)
    if not c or c.brand_id != brand_id:
        raise HTTPException(404, "Claim not found")
    return recovery.claim_ticket_text(c, channels_by_id(s, brand_id))


# ---- Gap 3: quick commerce ----------------------------------------------------------

@app.get("/api/brands/{brand_id}/quick-commerce")
def qc(brand_id: int, as_of: date | None = AsOf, s: Session = Depends(get_session)):
    res = quickcommerce.reconcile_quick_commerce(s, brand_id, _as_of(as_of))
    res.pop("findings")
    return res


# ---- Gap 4: tax credits ---------------------------------------------------------------

@app.get("/api/brands/{brand_id}/tax-credits")
def tax_credits(brand_id: int, as_of: date | None = AsOf, s: Session = Depends(get_session)):
    res = tax.reconcile_tax_credits(s, brand_id, _as_of(as_of))
    res.pop("findings")
    return res


# ---- Books -------------------------------------------------------------------------------

@app.get("/api/brands/{brand_id}/books/trial-balance")
def tb(brand_id: int, period: str | None = None, s: Session = Depends(get_session)):
    return ledger.trial_balance(s, brand_id, end=_range(period)[1])


@app.get("/api/brands/{brand_id}/books/pnl")
def pnl(brand_id: int, period: str | None = None, s: Session = Depends(get_session)):
    return ledger.profit_and_loss(s, brand_id, *_range(period))


@app.get("/api/brands/{brand_id}/books/journal")
def journal(brand_id: int, period: str | None = None, limit: int = 300, s: Session = Depends(get_session)):
    rows = ledger.journal_rows(s, brand_id, *_range(period))
    return {"total": len(rows), "rows": rows[:limit]}


@app.get("/api/brands/{brand_id}/export/tally.xml")
def tally(brand_id: int, period: str | None = None, s: Session = Depends(get_session)):
    b = _brand(s, brand_id)
    xml = ledger.tally_xml(s, brand_id, b.name, *_range(period))
    return Response(xml, media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="tally_{period or "all"}.xml"'})


@app.get("/api/brands/{brand_id}/export/journal.csv")
def journal_csv(brand_id: int, period: str | None = None, s: Session = Depends(get_session)):
    """Manual-journal CSV (works for Zoho Books import and any other ledger)."""
    return Response(close._csv(ledger.journal_rows(s, brand_id, *_range(period))), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="journal_{period or "all"}.csv"'})


# ---- Gap 5: profitability ---------------------------------------------------------------

@app.get("/api/brands/{brand_id}/profitability")
def profit(brand_id: int, period: str | None = None, by_channel: bool = False, s: Session = Depends(get_session)):
    return profitability.sku_profitability(s, brand_id, *_range(period), by_channel=by_channel)


# ---- Gap 6: capital ----------------------------------------------------------------------

@app.get("/api/brands/{brand_id}/capital")
def cap(brand_id: int, as_of: date | None = AsOf, include_items: bool = False, s: Session = Depends(get_session)):
    res = capital.borrowing_base(s, brand_id, _as_of(as_of))
    if not include_items:
        res["items"] = len(res["items"])
    return res


@app.get("/api/brands/{brand_id}/capital/lender-pack.csv")
def lender_pack(brand_id: int, as_of: date | None = AsOf, s: Session = Depends(get_session)):
    items = capital.borrowing_base(s, brand_id, _as_of(as_of))["items"]
    return Response(close._csv(items), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="verified_receivables.csv"'})


# ---- Gap 8: close & audit ------------------------------------------------------------------

@app.get("/api/brands/{brand_id}/close/{period}")
def get_close(brand_id: int, period: str, as_of: date | None = AsOf, s: Session = Depends(get_session)):
    return close.close_checklist(s, brand_id, period, _as_of(as_of))


@app.post("/api/brands/{brand_id}/close/{period}/lock")
def lock(brand_id: int, period: str, as_of: date | None = AsOf, force: bool = False,
         s: Session = Depends(get_session)):
    res = close.lock_period(s, brand_id, period, _as_of(as_of), force)
    s.commit()
    return res


@app.post("/api/brands/{brand_id}/close/{period}/unlock")
def unlock(brand_id: int, period: str, s: Session = Depends(get_session)):
    res = close.unlock_period(s, brand_id, period)
    s.commit()
    return res


@app.get("/api/brands/{brand_id}/audit-pack/{period}.zip")
def audit(brand_id: int, period: str, as_of: date | None = AsOf, s: Session = Depends(get_session)):
    _brand(s, brand_id)
    data = close.audit_pack(s, brand_id, period, _as_of(as_of))
    return Response(data, media_type="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="audit_pack_{period}.zip"'})


# ---- UI ------------------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
