import math
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Channel, Sku


def r2(x: float) -> float:
    return round(float(x or 0) + 0.0, 2)


def period_of(d: date) -> str:
    return d.strftime("%Y-%m")


def period_bounds(period: str) -> tuple[date, date]:
    y, m = map(int, period.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), (m % 12) + 1, 1)
    return start, date.fromordinal(end.toordinal() - 1)


def slabs(weight_g: int | None) -> int:
    return max(1, math.ceil((weight_g or 500) / 500))


def channels_by_id(s: Session, brand_id: int) -> dict[int, Channel]:
    return {c.id: c for c in s.scalars(select(Channel).where(Channel.brand_id == brand_id))}


def channels_by_code(s: Session, brand_id: int) -> dict[str, Channel]:
    return {c.code: c for c in s.scalars(select(Channel).where(Channel.brand_id == brand_id))}


def skus_by_code(s: Session, brand_id: int) -> dict[str, Sku]:
    return {k.code: k for k in s.scalars(select(Sku).where(Sku.brand_id == brand_id))}


def expected_fees(order, ch: Channel) -> dict:
    """What the rate card says the channel should deduct for this order (positive Rs)."""
    commission = ch.commission_rate * order.gross_amount
    fixed = ch.fixed_fee
    ship = ch.ship_rate_per_500g * slabs(order.declared_weight_g) if ch.kind == "marketplace" else 0
    return {"commission": commission, "fixed_fee": fixed, "shipping_fee": ship}


def expected_net(order, ch: Channel, gst_rate: float) -> float:
    from app.config import GST_TCS_RATE, TDS_194O_RATE

    fees = expected_fees(order, ch)
    taxable = order.gross_amount / (1 + gst_rate)
    taxes = (GST_TCS_RATE + TDS_194O_RATE) * taxable if ch.kind == "marketplace" else 0
    return order.gross_amount - sum(fees.values()) - taxes
