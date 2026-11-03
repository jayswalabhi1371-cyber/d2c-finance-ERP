"""Data model. Every table is scoped by brand_id (multi-tenant; RLS in Supabase).

Sign convention for settlement lines follows marketplace reports: money paid to
the brand is positive, charges/deductions are negative, net = sum of columns.
"""
from datetime import date, datetime, timezone

from sqlalchemy import (JSON, Boolean, Date, DateTime, Float, ForeignKey, Integer, String,
                        UniqueConstraint)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Brand(Base):
    __tablename__ = "brands"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    gstin: Mapped[str | None] = mapped_column(String(15))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Channel(Base):
    __tablename__ = "channels"
    __table_args__ = (UniqueConstraint("brand_id", "code"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    code: Mapped[str] = mapped_column(String(40))  # amazon, flipkart, shopify, blinkit...
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(20))  # marketplace|d2c|quick_commerce|logistics
    commission_rate: Mapped[float] = mapped_column(Float, default=0)
    fixed_fee: Mapped[float] = mapped_column(Float, default=0)
    ship_rate_per_500g: Mapped[float] = mapped_column(Float, default=0)
    settlement_days: Mapped[int] = mapped_column(Integer, default=15)
    claim_window_days: Mapped[int] = mapped_column(Integer, default=45)


class Sku(Base):
    __tablename__ = "skus"
    __table_args__ = (UniqueConstraint("brand_id", "code"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    code: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(200))
    cogs: Mapped[float] = mapped_column(Float)  # landed cost per unit, excl. GST
    gst_rate: Mapped[float] = mapped_column(Float, default=0.18)
    weight_g: Mapped[int] = mapped_column(Integer, default=500)
    opening_stock_qty: Mapped[int] = mapped_column(Integer, default=0)


class Order(Base):
    """One row per order item (marketplace order-item granularity)."""
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("brand_id", "channel_id", "external_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(80), index=True)
    order_date: Mapped[date] = mapped_column(Date)
    delivered_date: Mapped[date | None] = mapped_column(Date)
    sku_code: Mapped[str] = mapped_column(String(60))
    qty: Mapped[int] = mapped_column(Integer, default=1)
    gross_amount: Mapped[float] = mapped_column(Float)  # customer price incl. GST
    payment_mode: Mapped[str] = mapped_column(String(10), default="prepaid")  # prepaid|cod
    status: Mapped[str] = mapped_column(String(20))  # delivered|returned|rto|cancelled|in_transit
    declared_weight_g: Mapped[int | None] = mapped_column(Integer)
    logistics_code: Mapped[str | None] = mapped_column(String(40))  # D2C orders shipped via 3PL
    return_date: Mapped[date | None] = mapped_column(Date)
    return_received: Mapped[bool | None] = mapped_column(Boolean)
    return_condition: Mapped[str | None] = mapped_column(String(20))  # sellable|damaged|wrong_item


class SettlementLine(Base):
    __tablename__ = "settlement_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    settlement_id: Mapped[str] = mapped_column(String(80), index=True)
    external_order_id: Mapped[str | None] = mapped_column(String(80), index=True)
    txn_type: Mapped[str] = mapped_column(String(20))  # order|refund|reimbursement|adjustment
    posted_date: Mapped[date] = mapped_column(Date)
    principal: Mapped[float] = mapped_column(Float, default=0)
    commission: Mapped[float] = mapped_column(Float, default=0)
    fixed_fee: Mapped[float] = mapped_column(Float, default=0)
    shipping_fee: Mapped[float] = mapped_column(Float, default=0)
    tcs: Mapped[float] = mapped_column(Float, default=0)
    tds: Mapped[float] = mapped_column(Float, default=0)
    other: Mapped[float] = mapped_column(Float, default=0)
    net: Mapped[float] = mapped_column(Float, default=0)
    charged_weight_g: Mapped[int | None] = mapped_column(Integer)


class BankDeposit(Base):
    __tablename__ = "bank_deposits"
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    settlement_id: Mapped[str | None] = mapped_column(String(80), index=True)
    utr: Mapped[str | None] = mapped_column(String(40))
    deposit_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[float] = mapped_column(Float)


class LogisticsShipment(Base):
    """3PL data (e.g. Shiprocket): shipping charges and COD remittance per AWB."""
    __tablename__ = "logistics_shipments"
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    awb: Mapped[str] = mapped_column(String(40))
    external_order_id: Mapped[str] = mapped_column(String(80), index=True)
    ship_date: Mapped[date] = mapped_column(Date)
    payment_mode: Mapped[str] = mapped_column(String(10))
    cod_amount: Mapped[float] = mapped_column(Float, default=0)
    remitted_amount: Mapped[float] = mapped_column(Float, default=0)
    remittance_date: Mapped[date | None] = mapped_column(Date)
    shipping_fee: Mapped[float] = mapped_column(Float, default=0)  # incl. GST, positive
    charged_weight_g: Mapped[int | None] = mapped_column(Integer)


# ---- Quick commerce (B2B) -------------------------------------------------

class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"
    __table_args__ = (UniqueConstraint("brand_id", "channel_id", "po_number"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    po_number: Mapped[str] = mapped_column(String(60))
    po_date: Mapped[date] = mapped_column(Date)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    stream: Mapped[str] = mapped_column(String(20), default="direct_buy")  # direct_buy|consignment
    facility: Mapped[str | None] = mapped_column(String(80))
    lines: Mapped[list["PoLine"]] = relationship(back_populates="po", cascade="all, delete-orphan")


class PoLine(Base):
    __tablename__ = "po_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    po_id: Mapped[int] = mapped_column(ForeignKey("purchase_orders.id"), index=True)
    sku_code: Mapped[str] = mapped_column(String(60))
    qty_ordered: Mapped[int] = mapped_column(Integer)
    qty_shipped: Mapped[int] = mapped_column(Integer, default=0)  # from ASN
    qty_received: Mapped[int] = mapped_column(Integer, default=0)  # from GRN
    qty_accepted: Mapped[int] = mapped_column(Integer, default=0)  # from GRN
    unit_price: Mapped[float] = mapped_column(Float)  # excl. GST
    grn_date: Mapped[date | None] = mapped_column(Date)
    po: Mapped[PurchaseOrder] = relationship(back_populates="lines")


class QcInvoice(Base):
    __tablename__ = "qc_invoices"
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    po_number: Mapped[str] = mapped_column(String(60), index=True)
    invoice_number: Mapped[str] = mapped_column(String(60), index=True)
    invoice_date: Mapped[date] = mapped_column(Date)
    taxable_value: Mapped[float] = mapped_column(Float)
    gst_amount: Mapped[float] = mapped_column(Float)
    total: Mapped[float] = mapped_column(Float)


class QcPayment(Base):
    """Payment advice from a quick-commerce platform. Ad spend is netted here."""
    __tablename__ = "qc_payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    invoice_number: Mapped[str] = mapped_column(String(60), index=True)
    payment_date: Mapped[date] = mapped_column(Date)
    invoice_amount: Mapped[float] = mapped_column(Float)  # amount the platform accepted
    ad_deduction: Mapped[float] = mapped_column(Float, default=0)  # incl. GST
    tds_194q: Mapped[float] = mapped_column(Float, default=0)
    other_deduction: Mapped[float] = mapped_column(Float, default=0)  # incl. GST
    net_paid: Mapped[float] = mapped_column(Float)
    utr: Mapped[str | None] = mapped_column(String(40))


class TaxStatement(Base):
    """Credits as the government portal shows them (GSTR-2B TCS, Form 26AS TDS)."""
    __tablename__ = "tax_statements"
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    period: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    section: Mapped[str] = mapped_column(String(10))  # TCS|194O|194Q
    amount: Mapped[float] = mapped_column(Float)


# ---- Books ------------------------------------------------------------------

class JournalEntry(Base):
    __tablename__ = "journal_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    entry_date: Mapped[date] = mapped_column(Date, index=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    source: Mapped[str] = mapped_column(String(30))
    source_ref: Mapped[str] = mapped_column(String(120))
    narration: Mapped[str] = mapped_column(String(300))
    lines: Mapped[list["JournalLine"]] = relationship(back_populates="entry",
                                                      cascade="all, delete-orphan")


class JournalLine(Base):
    __tablename__ = "journal_lines"
    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(ForeignKey("journal_entries.id"), index=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    account: Mapped[str] = mapped_column(String(40), index=True)
    debit: Mapped[float] = mapped_column(Float, default=0)
    credit: Mapped[float] = mapped_column(Float, default=0)
    channel_code: Mapped[str | None] = mapped_column(String(40))
    sku_code: Mapped[str | None] = mapped_column(String(60))
    entry: Mapped[JournalEntry] = relationship(back_populates="lines")


# ---- Recovery & close ---------------------------------------------------------

class Claim(Base):
    __tablename__ = "claims"
    __table_args__ = (UniqueConstraint("brand_id", "rule", "external_ref"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    channel_id: Mapped[int] = mapped_column(ForeignKey("channels.id"), index=True)
    rule: Mapped[str] = mapped_column(String(40))
    external_ref: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(String(400))
    expected: Mapped[float] = mapped_column(Float, default=0)
    actual: Mapped[float] = mapped_column(Float, default=0)
    leak_amount: Mapped[float] = mapped_column(Float)
    event_date: Mapped[date] = mapped_column(Date)
    deadline: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="open")  # open|filed|recovered|rejected
    recovered_amount: Mapped[float] = mapped_column(Float, default=0)
    recovered_date: Mapped[date | None] = mapped_column(Date)
    ticket_ref: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str | None] = mapped_column(String(500))
    detected_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class ClosePeriod(Base):
    __tablename__ = "close_periods"
    __table_args__ = (UniqueConstraint("brand_id", "period"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    brand_id: Mapped[int] = mapped_column(ForeignKey("brands.id"), index=True)
    period: Mapped[str] = mapped_column(String(7))
    status: Mapped[str] = mapped_column(String(10), default="open")  # open|locked
    locked_at: Mapped[datetime | None] = mapped_column(DateTime)
    snapshot: Mapped[dict | None] = mapped_column(JSON)
