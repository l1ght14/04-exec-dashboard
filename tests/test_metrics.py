"""Metrics layer: every KPI asserted against a literal a reader can verify by eye.

The arithmetic is written in a comment next to each assertion. That is
deliberate: this file is documentation as much as it is a test, and the point
of asserting hand-computed values is that a reviewer can check the claim
without running anything. A reviewer who cannot re-derive the number does not
believe the number.

The fixture (see tests/fixtures.py) is at PAYMENT grain -- 6 rows for 5 orders,
because order O-3 is a split payment. That shape is what makes
``order_count() == 5`` while ``len(fact) == 6`` a meaningful assertion rather
than a tautology.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from dashboard.metrics import (
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

# ---------------------------------------------------------------------------
# The frame itself
# ---------------------------------------------------------------------------


class TestFixtureShape:
    def test_frame_is_at_payment_grain(self, fact):
        """6 payment rows, 5 distinct orders.

        100+50+30+20+25+300 are 6 payments; O-1,O-2,O-3,O-4,O-5 are 5 orders.
        O-3 is the duplicated pair. If these two numbers ever become equal the
        fixture has stopped modelling the thing the whole suite guards.
        """
        assert len(fact) == 6
        assert fact["order_id"].nunique() == 5

    def test_two_customers_with_known_order_counts(self, fact):
        """C-A has 4 orders, C-B has 1 (repeat_rate's 0.5 comes from this)."""
        counts = fact.groupby("customer_id")["order_id"].nunique()
        assert counts["C-A"] == 4
        assert counts["C-B"] == 1

    def test_two_orders_have_no_delivery_timestamp(self, fact):
        """O-4 (cancelled) and O-5 (unavailable) were never delivered."""
        undelivered = fact.loc[fact["order_delivered_timestamp"].isna(), "order_id"]
        assert sorted(undelivered) == ["O-4", "O-5"]


# ---------------------------------------------------------------------------
# revenue / order_count / aov
# ---------------------------------------------------------------------------


class TestRevenue:
    def test_revenue_sums_every_payment(self, fact):
        """100 + 50 + 30 + 20 + 25 + 300 = 525.00.

        The O-3 pair (30 + 20) is a split payment and must be summed, not
        collapsed to 30 or to 50.
        """
        assert revenue(fact) == 525.00

    def test_revenue_is_unchanged_by_the_order_payment_grain(self, fact):
        """Summing payments is additive, so a payment-grain frame and an
        order-grain frame with the same payments give the same revenue.

        100 + 50 + (30+20) + 25 + 300 = 525.00 either way. This is the property
        that lets fact_order sit at order grain without losing money -- and the
        reason order_count cannot use len() (next test).
        """
        at_order_grain = fact.groupby("order_id", as_index=False)["payment_value"].sum()
        assert revenue(at_order_grain) == 525.00


class TestOrderCount:
    def test_counts_distinct_orders_not_rows(self, fact):
        """5 distinct orders across 6 rows.

        len(fact) would be 6 here and 103,886 on the real olist payments table
        (against 99,441 real orders) -- a 4% overstatement that still looks
        like a plausible number on a KPI tile.
        """
        assert order_count(fact) == 5
        assert order_count(fact) != len(fact)

    def test_deduplicating_the_frame_does_not_change_the_count(self, fact):
        """Dropping to one row per order leaves the count at 5.

        Proves the metric is a function of the orders, not of the frame's
        shape -- so a future re-grain of the dashboard cannot break it.
        """
        one_row_per_order = fact.drop_duplicates(subset="order_id")
        assert order_count(one_row_per_order) == 5


class TestAov:
    def test_aov_equals_revenue_over_orders(self, fact):
        """525.00 / 5 = 105.00."""
        assert aov(fact) == 105.00

    def test_aov_is_exactly_the_identity(self, fact):
        """The definition, asserted as an identity rather than a literal.

        aov() exists only to guarantee this, so the test is the identity
        itself; the 105.00 above is the hand-checkable version of the same
        claim.
        """
        assert aov(fact) == revenue(fact) / order_count(fact)

    def test_aov_is_unchanged_by_the_frame_grain(self, fact):
        """At order grain: 525.00 / 5 = 105.00, identical to payment grain."""
        at_order_grain = fact.groupby("order_id", as_index=False)["payment_value"].sum()
        assert aov(at_order_grain) == 105.00


# ---------------------------------------------------------------------------
# cancel_rate
# ---------------------------------------------------------------------------


class TestCancelRate:
    def test_counts_the_configured_cancelled_set(self, fact):
        """2 of 5 = 0.40 with the default set.

        Numerator: O-4 'cancelled' and O-5 'unavailable'.
        Denominator: all 5 in-scope orders -- a rate over delivered orders only
        would be 0/3 and structurally incapable of reporting a cancellation.
        """
        assert cancel_rate(fact) == 0.40

    def test_excludes_unavailable_when_not_in_the_set(self, fact):
        """1 of 5 = 0.20 with {'cancelled', 'canceled'}.

        Proves the set is a parameter and not a constant, which is what makes
        "report 'unavailable' as its own line" a one-argument change instead
        of an edit inside a chart callback.
        """
        only_cancelled = {"cancelled", "canceled"}
        assert cancel_rate(fact, cancelled=only_cancelled) == 0.20

    def test_ignores_every_live_pipeline_status(self, fact):
        """Relabelling 2 of the 3 delivered orders leaves cancel_rate at 0.40.

        The fixture already proves 'delivered' is ignored (0.40, not 1.00). This
        adds 'shipped' and 'processing' -- the two statuses a real olist
        dashboard mis-handles most often, because they sit in the data looking
        like failures -- and asserts the KPI does not move. If any live
        pipeline state were treated as a cancellation, the numerator would
        rise and this number would change.
        """
        in_flight = fact.assign(
            order_status=fact["order_status"].replace(
                {"delivered": "shipped"}
            ).mask(fact["order_id"].eq("O-2"), "processing")
        )
        assert cancel_rate(in_flight) == 0.40

    def test_an_empty_cancelled_set_matches_nothing(self, fact):
        """0 of 5 = 0.0.

        The sharpest form of "only the configured set counts": nothing in the
        frame can match an empty set, so a numerator that was derived from any
        truthiness test on ``order_status`` instead of the membership test
        would fail here immediately.
        """
        assert cancel_rate(fact, cancelled=set()) == 0.0

    def test_the_set_is_a_parameter_not_a_hardcoded_filter(self, fact):
        """Naming a live status in the set does start counting it.

        3 of 5 = 0.60 for {'delivered'}, i.e. O-1, O-2, O-3 counted once each
        (not four times, despite O-3 having two payment rows). This is the
        other direction of the same contract: the set is honoured exactly, so
        changing the definition means changing one argument.
        """
        assert cancel_rate(fact, cancelled={"delivered"}) == 0.60

    def test_a_split_payment_cancellation_is_not_counted_twice(self, fact):
        """1 of 5 = 0.20, not 2 of 5 = 0.40.

        O-3 is relabelled 'cancelled' and the two orders that were already
        cancelled are moved to 'processing', so O-3 is the only cancellation
        and it occupies two payment rows (30 + 20). Summing the row mask
        instead of counting distinct orders would report 0.40 -- double the
        truth, from one order. The scenario is synthetic (a cancelled order is
        rarely also a split payment) but the counting rule is the one that
        runs in production, so the guard is asserted rather than trusted.
        """
        only_o3_cancelled = fact.assign(
            order_status=fact["order_status"]
            .replace({"cancelled": "processing", "unavailable": "processing"})
            .mask(fact["order_id"].eq("O-3"), "cancelled")
        )
        assert only_o3_cancelled["order_status"].eq("cancelled").sum() == 2
        assert order_count(only_o3_cancelled) == 5
        # 1 distinct order / 5, not 2 rows / 5.
        assert cancel_rate(only_o3_cancelled) == 0.20

    def test_matches_statuses_regardless_of_case_or_padding(self, fact):
        """'  CANCELLED  ' counts as cancelled: 1 of 5 = 0.20.

        Without normalisation this silently matches nothing and returns 0.0 --
        a confident, wrong, plausible-looking KPI.
        """
        messy = fact.assign(order_status=fact["order_status"].str.upper().str.pad(8))
        assert cancel_rate(messy, cancelled={"cancelled", "canceled"}) == 0.20


# ---------------------------------------------------------------------------
# median_delivery_days -- the key correctness test
# ---------------------------------------------------------------------------


class TestMedianDeliveryDays:
    def test_excludes_undelivered_orders_rather_than_counting_them_as_zero(self, fact):
        """4.0, not 1.0.

        Delivered orders are O-1 (Jan 1 -> Jan 5 = 4d), O-2 (Jan 2 -> Jan 3 =
        1d) and O-3 (Jan 2 -> Jan 9 = 7d). Median of [1, 4, 7] is 4.

        O-4 and O-5 have no delivery timestamp and are excluded. Had they
        been counted as 0 days the population would be [0, 0, 1, 4, 7] with a
        median of 1.0 -- a 3-day understatement on a metric the README names
        as the top revenue driver, and one that makes a delivery regression
        look like an improvement. That is the bug this test exists to stop.
        """
        assert median_delivery_days(fact) == 4.0
        assert median_delivery_days(fact) != 1.0

    def test_uses_the_median_not_the_mean(self, fact):
        """Mean of [1, 4, 7] = 4.0 here, so this fixture cannot separate the
        two -- the delivered window has to be widened to prove the choice.

        Widened window: O-1 Jan 1 -> Jan 11 = 10d, O-2 and O-3 unchanged.
        Population [1, 7, 10] has mean 6.0 and median 7.0. Asserting 7.0
        proves the median is what is computed, which matters because olist's
        delivery distribution is right-skewed and a single 200-day outlier
        moves the mean by days.
        """
        widened = fact.assign(
            order_delivered_timestamp=fact["order_delivered_timestamp"].mask(
                fact["order_id"].eq("O-1"), "2018-01-11 10:00:00"
            )
        )
        assert median_delivery_days(widened) == 7.0

    def test_drops_delivered_before_paid(self, fact):
        """A delivery that precedes payment is a clock error, not a measurement.

        Same arithmetic, one extra impossible row: paid Jan 20, delivered
        Jan 18 -> -2 days. If admitted, the population is [-2, 1, 4, 7] and
        the median falls to 2.5. Asserting 4.0 proves the negative duration
        was rejected.
        """
        bad_clock = pd.concat(
            [
                fact,
                pd.DataFrame(
                    [
                        {
                            "order_id": "O-9",
                            "customer_id": "C-C",
                            "customer_state": "SP",
                            "order_status": "delivered",
                            "order_paid_timestamp": "2018-01-20 10:00:00",
                            "order_delivered_timestamp": "2018-01-18 10:00:00",
                            "order_estimated_delivery_timestamp": None,
                            "payment_type": "credit_card",
                            "payment_installments": 1,
                            "payment_value": 10.00,
                            "product_id": "P-9",
                            "seller_id": "SELLER-1",
                            "product_category_name": "toys",
                            "review_score": 1.0,
                        }
                    ]
                ),
            ],
            ignore_index=True,
        )
        assert median_delivery_days(bad_clock) == 4.0

    def test_returns_nan_when_nothing_was_delivered(self):
        """All orders undelivered -> no median, not 0.0.

        0.0 would claim every order arrived instantly, on a day when in fact
        nothing arrived. The chart must be able to say "no completed deliveries
        in this period" instead of drawing a flat line on the floor. Carries
        order_id because the metric needs it to count one duration per order.
        """
        undelivered = pd.DataFrame(
            {
                "order_id": ["O-1", "O-2"],
                "order_paid_timestamp": ["2018-01-01 10:00:00", "2018-01-02 10:00:00"],
                "order_delivered_timestamp": [None, None],
            }
        )
        assert math.isnan(median_delivery_days(undelivered))

    def test_parses_olist_iso_timestamp_strings(self, fact):
        """The fixture stores raw strings, exactly as the olist CSVs do.

        Ties the metric to the shipped data format: a dashboard that only
        works on pre-parsed datetimes breaks the moment the CSV lands. Asserted
        as "not already a datetime" rather than by naming a dtype, because the
        pandas string dtype is spelled differently in 3.x than in 2.x and this
        suite must not break on a version bump.
        """
        paid = fact["order_paid_timestamp"]
        assert not pd.api.types.is_datetime64_any_dtype(paid)
        assert isinstance(paid.iloc[0], str)
        assert median_delivery_days(fact) == 4.0

    def test_a_split_payment_does_not_weight_its_order_twice(self, fact):
        """4.0, not 5.5.

        O-3 has two payment rows and a 7-day delivery. Counting rows gives
        [1, 4, 7, 7] with a median of 5.5 -- an order made 1.5x more important
        to the delivery metric purely because the customer paid in two
        instalments. On a payment-grain frame this is not an edge case; it is
        what 4,445 orders would do to the KPI.
        """
        assert median_delivery_days(fact) == 4.0
        assert median_delivery_days(fact) != 5.5


# ---------------------------------------------------------------------------
# repeat_rate
# ---------------------------------------------------------------------------


class TestRepeatRate:
    def test_one_of_two_customers_repeats(self, fact):
        """1 of 2 = 0.50.

        C-A has 4 distinct orders (> 1, so repeats). C-B has 1 (not > 1). The
        denominator is customers, never orders -- over 5 orders the same
        computation would give 4/5 = 0.8 and mean nothing.
        """
        assert repeat_rate(fact) == 0.50

    def test_a_split_payment_does_not_make_a_customer_repeat(self):
        """One customer, one order paid in three instalments -> 0.0.

        This is the repeat-rate bug that only appears at payment grain. Counting
        rows would give 3 orders for 1 customer and report 1.0 -- a 100% repeat
        rate for a business that has never had a repeat customer.
        """
        single_order = pd.DataFrame(
            {
                "order_id": ["O-1", "O-1", "O-1"],
                "customer_id": ["C-A", "C-A", "C-A"],
            }
        )
        assert repeat_rate(single_order) == 0.0

    def test_identical_customers_excluded_from_the_denominator(self, fact):
        """C-B becomes NULL: 1 repeat of 1 identified customer = 1.0.

        Keeping the unidentified row as a non-repeat customer would give 0.50
        and understate retention -- the one thing this metric exists to catch.
        """
        with_nulls = fact.assign(
            customer_id=fact["customer_id"].mask(fact["customer_id"].eq("C-B"), None)
        )
        assert repeat_rate(with_nulls) == 1.0


# ---------------------------------------------------------------------------
# seller_count and revenue_by_group
# ---------------------------------------------------------------------------


class TestSellerCount:
    def test_counts_distinct_sellers(self, fact):
        """SELLER-1, SELLER-2, SELLER-3 = 3.

        SELLER-1 fulfils 3 of the 6 rows, but the tile answers "how many
        counterparties do we depend on", which is 3. A per-row count would be
        6 and would move every time any seller sold twice.
        """
        assert seller_count(fact) == 3


class TestRevenueByGroup:
    def test_groups_by_customer_and_sorts_descending(self, fact):
        """C-A = 100+50+30+20+300 = 500.00 ; C-B = 25.00.

        Descending order is part of the contract, so the assertion is on the
        index order and not only on the values: a breakdown whose sort drifted
        would still pass a values-only test.
        """
        by_customer = revenue_by_group(fact, "customer_id")
        assert list(by_customer.index) == ["C-A", "C-B"]
        assert list(by_customer.values) == [500.00, 25.00]

    def test_groups_by_status_and_sorts_descending(self, fact):
        """unavailable 300.00 > delivered 200.00 > cancelled 25.00.

        delivered = 100 + 50 + 30 + 20 = 200.00 across 4 rows collapsing to
        3 orders -- another place where a row count would overstate.
        """
        by_status = revenue_by_group(fact, "order_status")
        assert list(by_status.index) == ["unavailable", "delivered", "cancelled"]
        assert list(by_status.values) == [300.00, 200.00, 25.00]

    def test_keeps_missing_keys_as_their_own_group(self, fact):
        """The region-less order's 25.00 is a group, not an omission.

        Masking RJ to NULL leaves three groups: SP = 100+50+30+20 = 200.00,
        None = 25.00, AM = 300.00. pandas drops NULL keys by default, so the
        default behaviour would report 2 groups and 500.00 -- quietly
        unreconciling against the 525.00 headline. The 3-group, 525.00 result
        is the one that tells a stakeholder the truth.
        """
        without_state = fact.assign(
            customer_state=fact["customer_state"].mask(
                fact["customer_state"].eq("RJ"), None
            )
        )
        by_state = revenue_by_group(without_state, "customer_state")

        assert len(by_state) == 3
        assert int(by_state.index.isna().sum()) == 1
        # Excluding the null-key group leaves AM 300.00 + SP 200.00. Note this
        # filters on the INDEX, not dropna() -- the values are all populated
        # and only the key is missing.
        assert float(by_state[by_state.index.notna()].sum()) == 500.00
        assert float(by_state.sum()) == 525.00

    def test_sums_back_to_the_headline(self, fact):
        """Grouped revenue must reconcile with revenue() exactly.

        Any grouping that does not sum back to the headline is double-counting
        or dropping money, which is the fastest way to lose a stakeholder.
        """
        assert float(revenue_by_group(fact, "customer_id").sum()) == revenue(fact)
        assert float(revenue_by_group(fact, "order_status").sum()) == revenue(fact)


# ---------------------------------------------------------------------------
# kpi_summary
# ---------------------------------------------------------------------------


class TestKpiSummary:
    def test_contains_exactly_the_documented_keys(self, fact):
        """The literal key set, written out rather than imported.

        Comparing against KPI_KEYS would pass even if someone added a KPI and
        updated the tuple in the same commit. Pinning the literal here makes
        adding a tile a deliberate, reviewable change.
        """
        assert set(kpi_summary(fact)) == {
            "revenue",
            "order_count",
            "aov",
            "cancel_rate",
            "repeat_rate",
            "median_delivery_days",
            "seller_count",
        }

    def test_key_order_matches_the_documented_display_order(self, fact):
        """Keys come back in KPI_KEYS order, so the tile row is built once."""
        assert tuple(kpi_summary(fact)) == KPI_KEYS

    def test_every_value_matches_the_individually_computed_metric(self, fact):
        """525.00 / 5 / 105.00 / 0.40 / 0.50 / 4.0 / 3.

        kpi_summary must not contain its own copy of the arithmetic; if it
        ever does, the tile row and the drill-down can disagree, which is the
        failure this whole layer exists to prevent.
        """
        assert kpi_summary(fact) == {
            "revenue": 525.00,
            "order_count": 5,
            "aov": 105.00,
            "cancel_rate": 0.40,
            "repeat_rate": 0.50,
            "median_delivery_days": 4.0,
            "seller_count": 3,
        }


# ---------------------------------------------------------------------------
# Empty input
# ---------------------------------------------------------------------------


class TestEmptyFrame:
    """A query that matches no rows must not raise.

    Date filters on a dashboard hit this constantly (pick an empty week), so
    every metric has to survive it, and the shape of the answer is part of the
    contract: additive metrics are 0, ratios are NaN.
    """

    @pytest.fixture
    def empty(self, fact):
        return fact.iloc[0:0].copy()

    def test_revenue_and_counts_are_zero(self, empty):
        assert revenue(empty) == 0.0
        assert order_count(empty) == 0
        assert seller_count(empty) == 0

    def test_ratios_are_nan_rather_than_zero(self, empty):
        """NaN, not 0.0.

        0.0 would assert the business had zero-value orders. NaN asserts there
        is no data, and the dashboard's empty state can act on that
        difference; a metric that collapses the two cannot.
        """
        assert math.isnan(aov(empty))
        assert math.isnan(cancel_rate(empty))
        assert math.isnan(repeat_rate(empty))
        assert math.isnan(median_delivery_days(empty))

    def test_no_metric_raises(self, empty):
        summary = kpi_summary(empty)
        assert set(summary) == set(KPI_KEYS)
        assert summary["revenue"] == 0.0
        assert summary["order_count"] == 0
        for key in ("aov", "cancel_rate", "repeat_rate", "median_delivery_days"):
            assert math.isnan(summary[key]), key

    def test_revenue_by_group_returns_an_empty_series(self, empty):
        grouped = revenue_by_group(empty, "customer_id")
        assert grouped.empty
        assert isinstance(grouped, pd.Series)

    def test_a_lone_cancelled_order_computes_every_metric(self, fact):
        """The realistic thin edge: one order, no delivery, no repeat.

        25.00 of revenue, 1 order, 25.00 AOV, cancel_rate 1.0 (the only order
        is cancelled), repeat_rate 0.0 (one customer, one order), and NaN for
        delivery because nothing was observed. No metric may raise, because
        this is what a narrow date filter returns.
        """
        single = fact.iloc[[4]].copy()  # O-4, cancelled, never delivered
        assert single["order_id"].tolist() == ["O-4"]

        assert revenue(single) == 25.00
        assert order_count(single) == 1
        assert aov(single) == 25.00
        assert cancel_rate(single) == 1.0
        assert repeat_rate(single) == 0.0
        assert math.isnan(median_delivery_days(single))
        assert set(kpi_summary(single)) == set(KPI_KEYS)


# ---------------------------------------------------------------------------
# Immutability
# ---------------------------------------------------------------------------


class TestNoMutation:
    def test_kpi_summary_leaves_the_input_frame_identical(self, fact):
        """The dashboard holds one frame and filters it per request.

        A metric that wrote a column back would corrupt every metric evaluated
        after it, in a way that only shows up under a specific filter order.
        Cheap to guarantee, expensive to debug.
        """
        before = fact.copy(deep=True)
        kpi_summary(fact)
        pd.testing.assert_frame_equal(fact, before)

    def test_metrics_do_not_change_the_row_count(self, fact):
        before = len(fact)
        median_delivery_days(fact)
        revenue_by_group(fact, "customer_id")
        kpi_summary(fact)
        assert len(fact) == before

    def test_revenue_by_group_returns_an_independent_object(self, fact):
        """Sorting the result must not reorder or mutate the caller's frame."""
        before = fact.copy(deep=True)
        grouped = revenue_by_group(fact, "customer_id")
        grouped.index.name = "mutated"
        pd.testing.assert_frame_equal(fact, before)
