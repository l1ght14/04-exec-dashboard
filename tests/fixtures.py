"""Hand-built fact frame with values chosen so every expected metric is
computable with a pencil.

The frame is at **payment grain on purpose**, not order grain, because that is
the shape the metrics layer has to survive in production: olist records a split
payment as one row per instalment, so the real payment table has 103,886 rows
for 99,441 orders. A fixture at order grain would make ``len(fact)`` and
``nunique(order_id)`` agree, and the bug this suite exists to prevent -- an
order counted twice, an AOV quietly understated by 4% -- would never be caught.

Five orders, six payment rows, two customers. Every messy case an executive
dashboard trips over is present:

===========================  =========  ==========  ==========  ========
order                       customer   status      delivered?  payment
===========================  =========  ==========  ==========  ========
O-1                         C-A        delivered   yes         100.00
O-2                         C-A        delivered   yes          50.00
O-3 (two instalments)       C-A        delivered   yes          30.00 + 20.00
O-4                         C-B        cancelled   no           25.00
O-5                         C-A        unavailable no          300.00
===========================  =========  ==========  ==========  ========

So the frame is 6 rows but 5 distinct orders; C-A has 4 orders and C-B has 1;
two orders have no delivery timestamp; and the cancelled set in
``metrics.CANCELLED_STATUSES`` matches two of the five orders rather than one,
because ``'unavailable'`` is deliberately in it.

Timestamps are ISO strings exactly as olist ships them, so the metrics layer's
``to_datetime`` parsing is exercised rather than bypassed by pre-parsed values.
"""

from __future__ import annotations

import pandas as pd

#: Column order mirrors fact_order in sql/01_schema.sql.
COLUMNS: tuple[str, ...] = (
    "order_id",
    "customer_id",
    "customer_state",
    "order_status",
    "order_paid_timestamp",
    "order_delivered_timestamp",
    "order_estimated_delivery_timestamp",
    "payment_type",
    "payment_installments",
    "payment_value",
    "product_id",
    "seller_id",
    "product_category_name",
    "review_score",
)

# Expected values for this fixture, by hand, so a reader can check the
# assertions in test_metrics.py without running anything:
#
#   revenue             100 + 50 + 30 + 20 + 25 + 300          = 525.00
#   order_count         distinct O-1..O-5  (6 rows, 5 orders)   = 5
#   aov                 525.00 / 5                              = 105.00
#   seller_count        SELLER-1, SELLER-2, SELLER-3           = 3
#   cancel_rate         O-4 'cancelled' + O-5 'unavailable'
#                       = 2 / 5                                = 0.40
#                       ... with 'unavailable' excluded, 1 / 5    = 0.20
#   repeat_rate         C-A has 4 (>1); C-B has 1 (not >1)
#                       = 1 / 2                                = 0.50
#   median_delivery     delivered: O-1 = 4d, O-2 = 1d, O-3 = 7d
#     _days             median of [1, 4, 7]                     = 4.0
#                       (O-4 and O-5 are undelivered -> excluded.
#                        Had they been counted as 0 the median would be
#                        1.0, which is the bug the test guards against.)
#   revenue by customer C-A = 100+50+30+20+300 = 500.00 ; C-B = 25.00
#   revenue by status   unavailable 300.00 > delivered 200.00 > cancelled 25.00
_ROWS: tuple[dict[str, object], ...] = (
    {
        "order_id": "O-1",
        "customer_id": "C-A",
        "customer_state": "SP",
        "order_status": "delivered",
        "order_paid_timestamp": "2018-01-01 10:00:00",
        "order_delivered_timestamp": "2018-01-05 10:00:00",
        "order_estimated_delivery_timestamp": "2018-01-08 10:00:00",
        "payment_type": "credit_card",
        "payment_installments": 1,
        "payment_value": 100.00,
        "product_id": "P-1",
        "seller_id": "SELLER-1",
        "product_category_name": "health_beauty",
        "review_score": 5.0,
    },
    {
        "order_id": "O-2",
        "customer_id": "C-A",
        "customer_state": "SP",
        "order_status": "delivered",
        "order_paid_timestamp": "2018-01-02 10:00:00",
        "order_delivered_timestamp": "2018-01-03 10:00:00",
        "order_estimated_delivery_timestamp": "2018-01-06 10:00:00",
        "payment_type": "boleto",
        "payment_installments": 1,
        "payment_value": 50.00,
        "product_id": "P-2",
        "seller_id": "SELLER-1",
        "product_category_name": "health_beauty",
        "review_score": 4.0,
    },
    {
        # Split payment: the same order twice, 30 + 20. This is the row pair
        # that makes len(fact) = 6 while order_count = 5.
        "order_id": "O-3",
        "customer_id": "C-A",
        "customer_state": "SP",
        "order_status": "delivered",
        "order_paid_timestamp": "2018-01-02 10:00:00",
        "order_delivered_timestamp": "2018-01-09 10:00:00",
        "order_estimated_delivery_timestamp": "2018-01-12 10:00:00",
        "payment_type": "credit_card",
        "payment_installments": 2,
        "payment_value": 30.00,
        "product_id": "P-3",
        "seller_id": "SELLER-2",
        "product_category_name": "toys",
        "review_score": 3.0,
    },
    {
        "order_id": "O-3",
        "customer_id": "C-A",
        "customer_state": "SP",
        "order_status": "delivered",
        "order_paid_timestamp": "2018-01-02 10:00:00",
        "order_delivered_timestamp": "2018-01-09 10:00:00",
        "order_estimated_delivery_timestamp": "2018-01-12 10:00:00",
        "payment_type": "credit_card",
        "payment_installments": 2,
        "payment_value": 20.00,
        "product_id": "P-3",
        "seller_id": "SELLER-2",
        "product_category_name": "toys",
        "review_score": 3.0,
    },
    {
        # Cancelled and never delivered. Belongs in the cancel_rate numerator
        # and must be absent from the delivery population.
        "order_id": "O-4",
        "customer_id": "C-B",
        "customer_state": "RJ",
        "order_status": "cancelled",
        "order_paid_timestamp": "2018-01-03 10:00:00",
        "order_delivered_timestamp": None,
        "order_estimated_delivery_timestamp": "2018-01-09 10:00:00",
        "payment_type": "credit_card",
        "payment_installments": 1,
        "payment_value": 25.00,
        "product_id": "P-4",
        "seller_id": "SELLER-1",
        "product_category_name": "housewares",
        "review_score": None,
    },
    {
        # 'unavailable' is in CANCELLED_STATUSES by decision, not by accident:
        # payment was captured and the item was never delivered.
        "order_id": "O-5",
        "customer_id": "C-A",
        "customer_state": "AM",
        "order_status": "unavailable",
        "order_paid_timestamp": "2018-01-04 10:00:00",
        "order_delivered_timestamp": None,
        "order_estimated_delivery_timestamp": "2018-01-12 10:00:00",
        "payment_type": "boleto",
        "payment_installments": 1,
        "payment_value": 300.00,
        "product_id": "P-5",
        "seller_id": "SELLER-3",
        "product_category_name": "electronics",
        "review_score": None,
    },
)


def build_fact() -> pd.DataFrame:
    """A fresh copy of the fixture frame.

    Returns a new object on every call so no test can mutate the frame another
    test depends on -- the metrics layer promises not to mutate its input, and
    a shared fixture that a single stray assignment could corrupt would make
    that promise untestable.
    """
    return pd.DataFrame(_ROWS, columns=COLUMNS)
