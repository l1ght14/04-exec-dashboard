"""Executive dashboard metrics layer.

Public surface is :mod:`dashboard.metrics`; the KPI definitions live there and
are re-exported here so dashboard code has one import path.
"""

from dashboard.metrics import (
    CANCELLED_STATUSES,
    KPI_KEYS,
    aov,
    cancel_rate,
    kpi_summary,
    median_delivery_days,
    order_count,
    repeat_rate,
    revenue,
    revenue_by_group,
    seller_count,
)

__all__ = [
    "CANCELLED_STATUSES",
    "KPI_KEYS",
    "aov",
    "cancel_rate",
    "kpi_summary",
    "median_delivery_days",
    "order_count",
    "repeat_rate",
    "revenue",
    "revenue_by_group",
    "seller_count",
]
