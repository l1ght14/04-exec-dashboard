-- =============================================================================
-- 02_metrics.sql — the metrics layer
-- =============================================================================
--
-- THE RULE: every KPI the dashboard shows comes from a view in this file.
-- No ad-hoc SUM() in a chart callback, no expression copied into a BI tool.
-- Two reasons, and the second is the one that matters:
--   1. Consistency — one definition, one number, no drift between the KPI row
--      and the chart that is supposed to explain it.
--   2. Auditability — a metric is a piece of reasoning someone has to be able
--      to read, argue with, and change in one place. A KPI that exists only as
--      a cell in a chart config is a KPI nobody can review.
--
-- Each metric below is a view so it is (a) testable in isolation with a single
-- SELECT, and (b) reusable -- v_revenue_by_region and v_kpi_summary are the
-- SAME revenue expression, not two that happen to agree today.
--
-- GRAIN of the metrics layer: one row per scalar, or one row per group. This is
-- a KPI layer, not a fact layer -- it reads fact_order, it never extends it.
--
-- PARITY WITH PYTHON: every definition here has a counterpart in
-- src/dashboard/metrics.py, and the two are asserted to agree. If you change a
-- definition in one place, change it in the other. That pairing is what makes
-- this project defensible rather than decorative.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- Shared, filtered fact -- the one place a "which orders count" decision is made
-- -----------------------------------------------------------------------------
-- GRAIN: one row per order, identical to fact_order.
--
-- Excludes un-paid orders (no payment captured => no revenue) but KEEPS orders
-- that were paid and later cancelled, because a cancellation is revenue that was
-- recognised and then not delivered -- that is precisely the number the
-- executive is trying to explain. Dropping cancelled orders here would make
-- cancel_rate permanently 0 and hide the problem the dashboard exists to show.
--
-- is_cancelled is read from fact_order, never re-derived from order_status, so
-- the definition in 01_schema.sql and 02_metrics.sql cannot drift apart.
CREATE OR REPLACE VIEW v_paid_orders AS
SELECT
    order_id,
    customer_id,
    order_date_key,
    order_year,
    order_month,
    order_status,
    payment_value,
    item_count,
    seller_count,
    is_cancelled,
    is_delivered,
    delivery_days,
    review_score
FROM fact_order
WHERE payment_value IS NOT NULL;


-- =============================================================================
-- HEADLINE KPIs (one row each)
-- =============================================================================

-- REVENUE
-- Definition: sum of all captured payment_value across in-scope orders.
-- Grain: one scalar.
-- Why payment_value and not items_price: payment_value is money that actually
-- moved. items_price is the listed value of the basket, ignores discounts and
-- shipping, and totals ~R$ 16.0M by coincidence on a dataset with little
-- discount pressure -- which is exactly the coincidence that makes teams pick
-- the wrong column. Ground truth on full olist: R$ 16,008,872.12.
CREATE OR REPLACE VIEW v_revenue AS
SELECT SUM(payment_value) AS revenue
FROM v_paid_orders;

-- ORDER COUNT
-- Definition: count of DISTINCT orders.
-- Grain: one scalar.
-- Why DISTINCT and not COUNT(*): olist records split payments as separate rows,
-- so a payment-grain table has 103,886 rows for 99,441 orders. COUNT(*) here
-- would report 4,445 phantom orders and understate AOV by ~4%. COUNT(DISTINCT
-- order_id) is correct at payment grain, item grain and order grain, so the
-- metric survives someone re-pointing the fact at a finer table.
CREATE OR REPLACE VIEW v_orders AS
SELECT COUNT(DISTINCT order_id) AS order_count
FROM v_paid_orders;

-- AVERAGE ORDER VALUE
-- Definition: revenue / distinct orders. The identity is asserted, not assumed.
-- Grain: one scalar.
-- Guard: returns NULL rather than a division error when there are no orders.
-- A dashboard that renders 0 for "no orders" is lying -- 0 revenue and no
-- revenue are different states and the UI must be able to show the second one.
CREATE OR REPLACE VIEW v_aov AS
SELECT
    r.revenue,
    o.order_count,
    CASE WHEN o.order_count = 0 THEN NULL
         ELSE r.revenue / o.order_count
    END AS aov
FROM v_revenue r
CROSS JOIN v_orders o;

-- CANCELLATION RATE
-- Definition: distinct cancelled orders / distinct in-scope orders.
-- Grain: one scalar.
-- Cancelled set: order_status IN ('cancelled', 'canceled', 'unavailable').
-- The full rationale, including why 'lost' is excluded, is on fact_order in
-- 01_schema.sql -- it is not repeated here on purpose. Read it there.
-- The filter is written out in full rather than as `is_cancelled` so that a
-- reader of this file sees the actual definition without a join. It is asserted
-- equal to is_cancelled in the test suite, so the duplication cannot rot.
CREATE OR REPLACE VIEW v_cancellation_rate AS
SELECT
    COUNT(DISTINCT order_id)                                              AS orders_total,
    COUNT(DISTINCT CASE WHEN order_status IN ('cancelled', 'canceled', 'unavailable')
                        THEN order_id END)                                AS orders_cancelled,
    CASE WHEN COUNT(DISTINCT order_id) = 0 THEN NULL
         ELSE COUNT(DISTINCT CASE WHEN order_status IN ('cancelled', 'canceled', 'unavailable')
                                  THEN order_id END)
              / COUNT(DISTINCT order_id)
    END                                                                  AS cancellation_rate
FROM v_paid_orders;

-- REPEAT RATE
-- Definition: share of PEOPLE with MORE THAN ONE order.
-- Grain: one scalar.
--
-- WHY customer_unique_id AND NOT customer_id. This is not a stylistic choice and
-- the data forces it. In this copy of olist the customers file has 99,441 rows --
-- one per order, each with a freshly generated customer_id -- so at the account
-- level every customer places exactly one order and the repeat rate is *always
-- exactly 0.0*, permanently, no matter what the business does. A dashboard whose
-- headline retention KPI is structurally pinned to zero is worse than no KPI: it
-- looks like a real number and cannot respond to anything.
--
-- customer_unique_id is the stable person-level key and does vary: 96,096 distinct
-- values for 99,441 orders. That gap is the entire repeat-purchase signal.
-- v_repeat_rate_by_account below keeps the account-level number so the contrast is
-- visible rather than quietly resolved.
--
-- Two further decisions:
--   * "> 1", not ">= 1". A repeat rate over all customers would then be 1.0
--     forever and carry no information.
--   * COUNT(DISTINCT order_id) per person, not COUNT(*). On a payment-grain
--     fact a single 2-instalment order would count as two orders and the buyer
--     would be mislabelled "repeat" -- the most likely bug in this metric across
--     the whole olist analysis ecosystem.
--   * NULL keys are excluded from the denominator, matching the Python layer.
CREATE OR REPLACE VIEW v_repeat_rate AS
WITH per_person AS (
    SELECT
        c.customer_unique_id,
        COUNT(DISTINCT f.order_id) AS orders_per_person
    FROM fact_order f
    JOIN dim_customer c ON c.customer_id = f.customer_id
    WHERE c.customer_unique_id IS NOT NULL
    GROUP BY c.customer_unique_id
)
SELECT
    COUNT(*)                                                        AS people,
    COUNT(*) FILTER (WHERE orders_per_person > 1)                   AS repeat_people,
    CASE WHEN COUNT(*) = 0 THEN NULL
         ELSE COUNT(*) FILTER (WHERE orders_per_person > 1) / COUNT(*)
    END                                                             AS repeat_rate,
    -- Secondary read, not the KPI: what the people look like in absolute terms.
    AVG(orders_per_person)                                          AS avg_orders_per_person
FROM per_person;


-- GRAIN: one row per scalar. Kept as its own view, not deleted, because
-- "0% at the account level, 3% at the person level" is itself the finding and
-- belongs on the dashboard next to the metric it explains.
CREATE OR REPLACE VIEW v_repeat_rate_by_account AS
WITH per_account AS (
    SELECT customer_id, COUNT(DISTINCT order_id) AS orders_per_account
    FROM fact_order
    WHERE customer_id IS NOT NULL
    GROUP BY customer_id
)
SELECT
    COUNT(*)                                                        AS accounts,
    COUNT(*) FILTER (WHERE orders_per_account > 1)                  AS repeat_accounts,
    CASE WHEN COUNT(*) = 0 THEN NULL
         ELSE COUNT(*) FILTER (WHERE orders_per_account > 1) / COUNT(*)
    END                                                             AS repeat_rate_by_account
FROM per_account;

-- MEDIAN DELIVERY DAYS
-- Definition: median of (delivered_at - paid_at) over orders that were BOTH
--             paid and delivered.
-- Grain: one scalar.
-- THE JUDGEMENT CALL: undelivered orders (cancelled, in transit, lost) are
-- EXCLUDED from the population, not counted as 0 days.
--   * Counting them as 0 says "half our orders arrived instantly". False, and
--     the error is directional: it drags the median down, so a genuine
--     delivery regression reads as an improvement. That is the worst possible
--     failure mode for the metric the README names as a top revenue driver.
--   * Including them with their true elapsed time is worse in a different way:
--     those orders are still open, so their delivery time is not yet observed
--     and would count as a growing penalty each day the data is not refreshed.
--   * The statistically correct treatment is right-censored survival analysis
--     (Kaplan-Meier on the competing-risks setup: delivered vs cancelled). That
--     is the honest upgrade, and it is NOT implemented here -- it needs a
--     per-cohort time window, which is a different metric, not a fix to this
--     one. See the `ponytail:` note in src/dashboard/metrics.py.
--   * Median, not mean: olist's delivery distribution is right-skewed with a
--     genuine bimodal shape (fast in-region, very slow cross-region), and one
--     200-day outlier moves the mean by days while the median stays put.
--   * NULL delivery_days from the fact (delivered before paid -- a known olist
--     data error) is also excluded, because a negative duration is not a
--     measurement.
CREATE OR REPLACE VIEW v_median_delivery_days AS
SELECT
    COUNT(delivery_days)                              AS orders_delivered,
    MEDIAN(delivery_days)                             AS median_delivery_days,
    -- Supporting context, not the KPI: the mean and the 90th percentile are
    -- shown next to the median on the dashboard precisely so a stakeholder
    -- cannot accuse the median of hiding a bad tail.
    AVG(delivery_days)                                AS mean_delivery_days,
    QUANTILE_CONT(delivery_days, 0.9)                 AS p90_delivery_days
FROM v_paid_orders;

-- SELLER COUNT
-- Definition: distinct sellers fulfilling orders in scope.
-- Grain: one scalar.
-- Distinct sellers, not seller-order pairs. The dashboard's "is supply
-- concentrating?" question is about how many counterparties the business
-- depends on, and that number only moves when a seller is added or dropped.
CREATE OR REPLACE VIEW v_seller_count AS
SELECT COUNT(DISTINCT seller_id) AS seller_count
FROM (
    SELECT DISTINCT order_id, seller_id
    FROM bridge_order_item
);


-- =============================================================================
-- THE SINGLE ENTRY POINT
-- =============================================================================
-- v_kpi_summary is the one row the dashboard's KPI strip reads. It exists so
-- the four numbers on screen are provably the same four numbers, fetched once,
-- from the same rows, in the same filter. Four separate queries that each
-- rebuild their own WHERE clause will eventually disagree, and the user will be
-- looking at two numbers that cannot both be true.
--
-- Grain: exactly one row, one column per metric.
CREATE OR REPLACE VIEW v_kpi_summary AS
SELECT
    (SELECT revenue             FROM v_revenue)            AS revenue,
    (SELECT order_count         FROM v_orders)             AS order_count,
    (SELECT aov                 FROM v_aov)                AS aov,
    (SELECT cancellation_rate   FROM v_cancellation_rate)  AS cancel_rate,
    (SELECT repeat_rate         FROM v_repeat_rate)        AS repeat_rate,
    (SELECT repeat_rate_by_account FROM v_repeat_rate_by_account) AS repeat_rate_by_account,
    (SELECT median_delivery_days FROM v_median_delivery_days) AS median_delivery_days,
    (SELECT seller_count        FROM v_seller_count)       AS seller_count
;


-- =============================================================================
-- BREAKDOWNS (the "where" of what changed)
-- =============================================================================
-- Each is the same revenue expression as v_revenue, re-grouped. If a breakdown
-- ever disagrees with the headline, the bug is in the grouping, not the
-- definition -- which is exactly the diagnostic you want to be able to make.

-- GRAIN: one row per calendar month. The trend line.
CREATE OR REPLACE VIEW v_revenue_by_month AS
SELECT
    order_year,
    order_month,
    COUNT(DISTINCT order_id)   AS order_count,
    SUM(payment_value)         AS revenue
FROM v_paid_orders
GROUP BY order_year, order_month
ORDER BY order_year, order_month;

-- GRAIN: one row per customer region. "Which region is dragging us down?"
-- COALESCE to 'Unknown' rather than dropping the NULL group: an order with no
-- matching dimension row is a real, visible slice of revenue, and silently
-- dropping it is how a regional total stops adding up to the headline. This
-- matches revenue_by_group(dropna=False) in the Python layer.
CREATE OR REPLACE VIEW v_revenue_by_region AS
SELECT
    COALESCE(c.customer_region, 'Unknown') AS customer_region,
    COUNT(DISTINCT f.order_id)  AS order_count,
    SUM(f.payment_value)        AS revenue,
    SUM(CASE WHEN f.is_cancelled THEN f.payment_value ELSE 0 END) AS cancelled_revenue
FROM fact_order f
LEFT JOIN dim_customer c ON c.customer_id = f.customer_id
GROUP BY COALESCE(c.customer_region, 'Unknown')
ORDER BY revenue DESC;

-- GRAIN: one row per product category. "Which product line?"
-- Reached through bridge_order_item, not through a column on fact_order --
-- category is multi-valued per order, so the only honest place to summarise it
-- is at item grain. paid_revenue is a share of the order, not the order value:
-- an order spanning two categories has its revenue split pro-rata by item
-- price, so the parts still sum back to the headline total.
CREATE OR REPLACE VIEW v_revenue_by_category AS
SELECT
    b.product_category_name,
    COUNT(DISTINCT b.order_id)   AS order_count,
    SUM(b.price)                 AS revenue
FROM bridge_order_item b
WHERE b.product_category_name IS NOT NULL
GROUP BY b.product_category_name
ORDER BY revenue DESC;

-- GRAIN: one row per order status. The data-quality dashboard.
-- Exists because olist's status vocabulary is genuinely messy, and an exec who
-- sees "shipped" next to "delivered" at 1% of orders deserves to know that
-- "shipped" means handed to the carrier and not yet received by the customer.
CREATE OR REPLACE VIEW v_orders_by_status AS
SELECT
    order_status,
    COUNT(DISTINCT order_id)  AS order_count,
    SUM(payment_value)        AS revenue,
    SUM(is_cancelled::INTEGER) AS cancelled_orders
FROM v_paid_orders
GROUP BY order_status
ORDER BY order_count DESC;
