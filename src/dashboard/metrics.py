"""KPI definitions for the executive dashboard.

This module is the metrics layer the README asks for: every number the
dashboard shows is defined exactly once, here, and is reachable through a single
entry point (:func:`kpi_summary`). Chart code calls that; chart code does not
re-derive anything.

Two things are load-bearing and worth stating up front.

**Grain.** Each function states the grain it expects. Most of them are grain
*agnostic* on purpose -- they count distinct ``order_id`` rather than rows, so
they return the same answer whether they are handed a payment-grain frame
(103,886 rows for 99,441 olist orders, because split payments are separate
rows) or an order-grain frame. Only :func:`revenue` cares about the grain, and
it is correct at every grain because summing payments preserves the total while
counting rows does not.

**None of these functions mutate their input.** The dashboard holds one
immutable fact frame and filters it per-request, so a metric that wrote a
column back would corrupt every other metric evaluated after it.

**Ratios return NaN, not 0.0, on an empty population.** 0/0 is undefined, and
rendering it as 0.0 would assert "revenue per order is zero" -- a business
failure claim -- when the truth is "there is no data". Screens that cannot show
NaN will show a dash; screens that show 0.0 will mislead. The dashboard's empty
state is a designed screen precisely so this case is never guessed at.
"""

from __future__ import annotations

import math
from typing import Final, Iterable

import pandas as pd

__all__ = [
    "CANCELLED_STATUSES",
    "KPI_KEYS",
    "revenue",
    "order_count",
    "aov",
    "cancel_rate",
    "repeat_rate",
    "median_delivery_days",
    "seller_count",
    "revenue_by_group",
    "kpi_summary",
]

# ---------------------------------------------------------------------------
# Column names and thresholds, named once so a schema change is a one-line edit.
# ---------------------------------------------------------------------------

#: Order identifier. Every distinct-order metric keys on this, never on a row
#: position, because olist's payment table is finer-grained than its order table.
ORDER_ID_COL: Final[str] = "order_id"

#: Statuses that count as cancelled, for the executive cancellation KPI.
#:
#: Olist's ``order_status`` vocabulary is messy and the overlap is real, so the
#: set is explicit rather than derived:
#:
#: * ``'cancelled'`` and ``'canceled'`` -- both spellings occur in the raw data
#:   (the canonical olist value is the one-L ``'canceled'``). Matching only one
#:   under-counts and nobody notices, because the number still looks plausible.
#: * ``'unavailable'`` -- the order was created and payment was captured, then
#:   the item became unfillable. Revenue was taken and never delivered, so for
#:   an executive "how much did we lose to cancellations" figure it belongs
#:   with the cancellations. This is a judgement call, not a fact; the
#:   defensible alternative is to report it as its own line. It is a single
#:   filter here so that changing the call means changing one argument.
#:
#: Deliberately excluded:
#: * ``'lost'`` -- arguably the same story (never delivered) but not
#:   unambiguously a cancellation, and the group is small and stable, so the
#:   exclusion is immaterial to the KPI. Written down so it can be challenged.
#: * ``'delivered'``, ``'shipped'``, ``'processing'``, ``'invoiced'``,
#:   ``'created'``, ``'approved'``, ``'returning'``, ``'proposed'``,
#:   ``'manifesto_shipped'`` -- all live-pipeline states. Counting any of these
#:   as a cancellation is a bug, and the test suite asserts they are ignored.
CANCELLED_STATUSES: Final[frozenset[str]] = frozenset(
    {"cancelled", "canceled", "unavailable"}
)

SECONDS_PER_DAY: Final[float] = 86_400.0


# ---------------------------------------------------------------------------
# Headline KPIs
# ---------------------------------------------------------------------------


def revenue(fact: pd.DataFrame, value_col: str = "payment_value") -> float:
    """Total money captured across in-scope orders.

    Why ``payment_value`` and not the basket's item price: ``payment_value`` is
    money that actually moved. An order's item prices ignore shipping and
    discounts, and on olist the two totals land close enough together
    (~R$ 16M each) that picking the wrong column is invisible on a dashboard
    and wrong in the reconciliation against finance. Payments are the ledger.

    Grain: any. Summing payments is additive, so this is correct at payment
    grain, item grain and order grain alike -- which is exactly why the count
    next door has to be careful.

    Returns 0.0 for an empty frame (a sum over nothing is zero, and zero
    revenue is a real state a dashboard should be able to render).
    """
    return float(fact[value_col].sum())


def order_count(fact: pd.DataFrame, order_col: str = ORDER_ID_COL) -> int:
    """Number of distinct orders.

    Why ``nunique`` and not ``len(fact)``: olist's payment table has 103,886
    rows for 99,441 orders, because a split-payment order is recorded once per
    instalment. ``len`` on a payment-grain frame reports 4,445 phantom orders
    and drags AOV down by ~4% -- a silent, plausible-looking error, which is
    the worst kind.

    Counting distinct IDs is also future-proof: the same function returns the
    right answer if someone re-points the dashboard at a finer table, which
    ``len`` would not.
    """
    return int(fact[order_col].nunique())


def aov(
    fact: pd.DataFrame,
    value_col: str = "payment_value",
    order_col: str = ORDER_ID_COL,
) -> float:
    """Average order value: revenue divided by distinct orders.

    The identity ``aov == revenue / order_count`` is the whole reason this
    function exists as a thin wrapper -- it cannot drift from the two metrics
    above because it *is* the two metrics above. Recomputing revenue by an
    independent route inside a chart callback is how a KPI row and a tooltip
    end up disagreeing.

    The guard is the interesting part: with zero orders the quotient is 0/0.
    Returning NaN keeps "no data" distinguishable from "orders worth nothing",
    which the dashboard's empty state needs in order to render the right
    screen. Returning 0.0 here would make a data outage look like a business
    collapse.
    """
    orders = order_count(fact, order_col)
    if orders == 0:
        return math.nan
    return revenue(fact, value_col) / orders


def cancel_rate(
    fact: pd.DataFrame,
    status_col: str = "order_status",
    cancelled: Iterable[str] = CANCELLED_STATUSES,
    order_col: str = ORDER_ID_COL,
) -> float:
    """Share of distinct orders that were cancelled.

    The denominator is *all* in-scope orders, not just delivered ones. A
    cancellation rate defined over delivered orders is a tautology. Cancelled
    orders are excluded from :func:`revenue` upstream (the money was returned),
    but they must stay in this denominator -- a dashboard that hides them
    reports a cancellation rate of zero, forever, which is the exact failure
    this metric exists to prevent.

    Statuses are normalised (stripped, lower-cased) before matching. That is one
    line to stop ``'Cancelled'`` or ``'CANCELLED'`` from silently matching
    nothing and producing a confident 0.0. Silently-wrong KPIs are the failure
    mode worth spending a line of code on.

    The cancelled set is a parameter rather than a constant so the definition
    can be varied deliberately (a stakeholder who wants 'unavailable' reported
    separately passes a different set) instead of by editing a filter buried in
    a chart.

    Returns NaN when there are no orders, for the same reason as :func:`aov`.
    """
    total = order_count(fact, order_col)
    if total == 0:
        return math.nan

    statuses = fact[status_col].astype("string").str.strip().str.lower()
    is_cancelled = statuses.isin(set(cancelled))
    # Distinct again: a cancelled order occupies one row per instalment, so
    # summing the mask would count a 3-instalment cancellation three times.
    cancelled_orders = fact.loc[is_cancelled, order_col].nunique()
    return cancelled_orders / total


def repeat_rate(
    fact: pd.DataFrame,
    customer_col: str = "customer_id",
    order_col: str = ORDER_ID_COL,
) -> float:
    """Share of customers with more than one order.

    Three decisions, all of which change the answer on real data:

    * **"> 1", not ">= 1".** A repeat rate computed over every customer is 1.0
      by construction and carries no information.
    * **Distinct orders per customer, not rows.** On a payment-grain fact a
      customer's single two-instalment order would count as two orders and the
      customer would be mislabelled "repeat". This is the single most likely
      bug in this metric anywhere olist is analysed, and it is why the frame's
      grain has to be stated rather than assumed.
    * **NULL customers are dropped from the denominator.** An unidentified
      customer cannot be a repeat customer, and including it as a non-repeat
      biases the rate downward -- hiding retention problems in the one number
      that is supposed to surface them.

    Note this measures repeat *accounts*, not repeat *people*: olist's referral
    chain gives one person several ``customer_id`` values, so a person-level
    rate needs ``customer_unique_id`` from ``dim_customer``. That is a separate
    metric, not a bug fix, so it is not silently substituted here.
    """
    orders_per_customer = fact.groupby(customer_col, dropna=True)[order_col].nunique()
    customers = int(orders_per_customer.shape[0])
    if customers == 0:
        return math.nan
    repeat_customers = int((orders_per_customer > 1).sum())
    return repeat_customers / customers


def median_delivery_days(
    fact: pd.DataFrame,
    paid_col: str = "order_paid_timestamp",
    delivered_col: str = "order_delivered_timestamp",
    order_col: str = ORDER_ID_COL,
) -> float:
    """Median days from payment to delivery, over orders that were delivered.

    **The judgement call: undelivered orders are EXCLUDED, not counted as 0.**

    Counting them as 0 days asserts that half the cancelled and in-transit
    orders arrived instantly. It is false, and -- this is the part that matters
    -- the error is *directional*: it drags the median down, so a genuine
    delivery regression reads as an improvement. For the metric the README
    names as a top revenue driver, that is the worst available failure mode.
    ``tests/test_metrics.py`` asserts the excluded case explicitly so the
    behaviour cannot be "simplified" away later.

    The alternative of including them at their true elapsed time is wrong in a
    different way: those orders are still open, so their delivery time is not
    yet observed and grows by a day for every day the data is not refreshed.
    Either way the population is wrong, and excluding is the defensible one.

    Four more decisions, each earning its place:

    * **One duration per order.** The frame may be at payment grain, where a
      3-instalment order occupies 3 rows. Taking the median over rows weights
      that order threefold, so an order is worth more to the delivery metric
      according to how its customer chose to pay. On a fixture with 4-day, 1-day
      and 7-day orders this moved the median from 4.0 to 5.5 -- a 37% error
      from a single duplicated order.
    * **Delivered before paid is excluded** (it yields a negative duration). A
      handful of olist rows have this clock skew; a negative delivery time is
      not a measurement, and admitting one is a cheap way to make the median
      look great.
    * **NULL timestamps are dropped implicitly.** In pandas any comparison
      against ``NaT`` is ``False``, so ``delivered >= paid`` alone filters
      cancelled and in-transit orders out. That is a documented property of the
      semantics, not a trick -- but it is why there is no ``notna()`` call
      below, and changing it carelessly would reintroduce the bug above.
    * **Median, not mean.** Olist's delivery distribution is right-skewed and
      genuinely bimodal (fast in-region, very slow cross-region); one 200-day
      outlier moves the mean by days while the median stays where the bulk of
      the business is.

    Returns NaN when no order qualifies -- an empty period has no median, and
    the chart should say so rather than draw a zero.

    ponytail: this is a plain median over observed completions, so it ignores
    censoring entirely. The statistically correct version is right-censored
    survival analysis (Kaplan-Meier, treating cancellation as a competing
    risk), which needs a per-cohort observation window and is therefore a
    different metric rather than a fix to this one. Upgrade when delivery
    performance becomes a contractual or compensation number.
    """
    timestamps = fact.loc[:, [order_col, paid_col, delivered_col]]
    paid = pd.to_datetime(timestamps[paid_col], errors="coerce")
    delivered = pd.to_datetime(timestamps[delivered_col], errors="coerce")

    # NaT comparisons are False, so this drops undelivered orders, orders with
    # a missing payment timestamp, and the delivered-before-paid data errors.
    observed = delivered >= paid
    window = (delivered - paid).mask(~observed)

    per_order = (
        pd.DataFrame({"order": timestamps[order_col], "days": window})
        .dropna(subset=["days"])
        .drop_duplicates(subset="order")
    )
    if per_order.empty:
        return math.nan
    return float(per_order["days"].dt.total_seconds().median() / SECONDS_PER_DAY)


def seller_count(fact: pd.DataFrame, seller_col: str = "seller_id") -> int:
    """Number of distinct sellers fulfilling the in-scope orders.

    Distinct sellers, not seller-order pairs. The question behind the tile is
    "is our supply base concentrating?", and that number only moves when a
    seller appears or disappears -- a per-order count would move every time a
    seller sells twice, which says nothing about concentration.
    """
    return int(fact[seller_col].nunique())


def revenue_by_group(
    fact: pd.DataFrame, group_col: str, value_col: str = "payment_value"
) -> pd.Series:
    """Revenue per value of ``group_col``, sorted highest first.

    Sorting descending is part of the contract, not a convenience: the whole
    job of a breakdown chart is answering "which one", and every consumer
    (chart, table, CSV export, test) would otherwise have to remember to sort.
    Doing it here means the ranking cannot disagree with the number.

    ``dropna=False`` keeps missing keys as their own group. A revenue slice
    whose region is unknown is a real slice of money, and pandas' default is to
    drop NULL keys silently -- which is precisely how a breakdown stops adding
    up to the headline total without anyone noticing. Pass
    ``group_col`` values through unchanged; callers that want a label can
    rename the index themselves.
    """
    grouped = fact.groupby(group_col, dropna=False)[value_col].sum()
    return grouped.sort_values(ascending=False)


# ---------------------------------------------------------------------------
# The single entry point
# ---------------------------------------------------------------------------

#: The documented key set of :func:`kpi_summary`, in display order.
#: Pinned here so the contract is greppable, and asserted as a literal in the
#: test suite so that adding a KPI is a deliberate act rather than a side
#: effect of someone adding a dict key.
KPI_KEYS: Final[tuple[str, ...]] = (
    "revenue",
    "order_count",
    "aov",
    "cancel_rate",
    "repeat_rate",
    "median_delivery_days",
    "seller_count",
)


def kpi_summary(fact: pd.DataFrame) -> dict[str, float | int]:
    """Every headline KPI in one call. This is the dashboard's entry point.

    The README requires that every KPI on the dashboard comes from one place.
    That is not achievable if each tile calls a different function with its own
    filter: the tiles will eventually disagree, and a user staring at two
    numbers that cannot both be true has lost all trust in the screen. One
    function over one frame, evaluated once, cannot disagree with itself.

    The dict is built by calling the metric functions rather than by inlining
    their expressions, so a definition lives in exactly one place and this view
    cannot drift from it. The cost is recomputing ``order_count`` a handful of
    times, which is nothing next to the risk of a second, subtly different
    copy of the same arithmetic.

    Keys are exactly :data:`KPI_KEYS`. Missing keys must be *absent*, not
    ``None``: a dashboard that renders a tile from ``summary.get("aov")`` will
    happily draw a silent blank instead of failing loudly when a key vanishes.

    Ratios are NaN and counts are 0 on an empty frame -- see the module
    docstring for why that distinction is load-bearing.
    """
    return {
        "revenue": revenue(fact),
        "order_count": order_count(fact),
        "aov": aov(fact),
        "cancel_rate": cancel_rate(fact),
        "repeat_rate": repeat_rate(fact),
        "median_delivery_days": median_delivery_days(fact),
        "seller_count": seller_count(fact),
    }
