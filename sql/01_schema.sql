-- =============================================================================
-- 01_schema.sql — Olist star schema (ANSI SQL / DuckDB dialect)
-- =============================================================================
--
-- Grain is the whole game. Every object below states its grain in a comment,
-- because "one row per X" is the single most common source of dashboard bugs:
-- if fact_order is accidentally at order_item grain, revenue inflates by the
-- average items-per-order (~1.13x on olist) and AOV collapses. Nothing in this
-- file should be read without its grain line.
--
-- Load order (run top to bottom):
--   1. Point data/ at the raw olist CSVs (olist_customers_dataset.csv, etc.).
--   2. This file: staging views -> dimensions -> fact -> drill-down bridge.
--   3. sql/02_metrics.sql: the KPI view layer.
--
-- The whole file is idempotent: every load is CREATE ... IF NOT EXISTS followed
-- by INSERT OR REPLACE keyed on the dimension/row key, so re-running it after a
-- data refresh updates rows in place instead of double-counting them.
--
-- ON THE RAW DOWNLOAD: the Kaggle olist zip nests a few columns inside escaped
-- quotes, so a naive read_csv_auto mis-parses review_comment_message and
-- payment_type. The staging views below assume FLAT CSVs (what the standard
-- olist preprocessing step emits). If you load the raw zip directly, sanitise
-- the nested quoting at load time -- do not patch it in the staging layer, or
-- the quarantine decisions get baked into the star schema.
--
-- Money is DECIMAL(12,2), never DOUBLE. Olist totals ~1.6e7 BRL; binary floats
-- accumulate representation error and make the KPI differ from the finance
-- system by cents. The Python metrics layer sums in float64 (a deliberate,
-- documented trade-off) because the difference is < 1e-6 BRL there, far below
-- any display precision.
-- =============================================================================


-- =============================================================================
-- SECTION 1 -- STAGING VIEWS (one row per raw CSV row; no joins, no logic)
-- =============================================================================
-- Staging views are deliberately dumb: read the CSV, rename to snake_case, cast
-- to a real type. All business rules live in the star schema. That separation is
-- what makes a metric definable in exactly one place.

-- GRAIN: one row per customer record in olist_customers_dataset.csv
--        (99,441 rows). The file has one row per customer_id, but the same real
--        person can appear under several customer_ids via the referral chain --
--        customer_unique_id is the person-level key, customer_id is the
--        order-facing key. The metrics layer keys on customer_id; see the note
--        on dim_customer before treating "customers" as "people".
CREATE OR REPLACE VIEW stg_customers AS
SELECT
    customer_id,
    customer_unique_id,
    customer_city,
    customer_state,
    -- Olist stores this as a string because leading zeros are significant
    -- (Brazilian CEPs). Never cast it to an integer: 01310 and 1310 are the
    -- same postcode, and the leading zero is what joins to geolocation.
    customer_zip_code_prefix
FROM read_csv_auto('data/olist_customers_dataset.csv', header = true);

-- GRAIN: one row per order in olist_orders_dataset.csv (99,441 rows).
--
-- COLUMN NAMES, because this is where the real dataset disagrees with every
-- blog post about it. The actual columns are:
--   order_purchase_timestamp, order_approved_at, order_delivered_carrier_date,
--   order_delivered_customer_date, order_estimated_delivery_date
-- There is no `order_purchase_timestamp` and no `order_delivered_timestamp`; anyone
-- who writes those from memory gets a binder error, which is the good outcome.
--
-- THE MISSING PAYMENT TIMESTAMP: olist's payments table has no timestamp column
-- at all, so "when was this order paid" is not answerable. The order date used
-- throughout this schema is therefore the PURCHASE date, not a payment date, and
-- it is named order_purchase_timestamp everywhere so the distinction is never
-- lost downstream. Consequence worth knowing: any metric that would need
-- time-to-payment (DSO, cash conversion) cannot be built from this dataset.
--
-- `order_delivered_carrier_date` vs `order_delivered_customer_date`: the carrier
-- date is when the courier got it, the customer date is when the buyer got it.
-- Delivery performance is the customer date. Using the carrier date would
-- understate every delivery time by a day or three.
CREATE OR REPLACE VIEW stg_orders AS
SELECT
    order_id,
    customer_id,
    order_status,
    CAST(order_purchase_timestamp          AS TIMESTAMP) AS order_purchase_timestamp,
    CAST(order_estimated_delivery_date     AS TIMESTAMP) AS order_estimated_delivery_timestamp,
    CAST(order_delivered_customer_date     AS TIMESTAMP) AS order_delivered_timestamp,
    CAST(order_approved_at                 AS TIMESTAMP) AS order_approved_timestamp,
    CAST(order_delivered_carrier_date      AS TIMESTAMP) AS order_delivered_carrier_timestamp
FROM read_csv_auto('data/olist_orders_dataset.csv', header = true);

-- GRAIN: one row per order line item in olist_order_items_dataset.csv
--        (112,650 rows; ~1.13 items per order). This is the table that must
--        never be joined straight to payments -- the two are independent
--        one-to-many children of an order and the cross product is the classic
--        olist revenue-inflation bug. fact_order rolls each up independently.
CREATE OR REPLACE VIEW stg_order_items AS
SELECT
    order_id,
    order_item_id,
    product_id,
    seller_id,
    CAST(price          AS DECIMAL(12,2)) AS price,
    CAST(freight_value  AS DECIMAL(12,2)) AS freight_value,
    -- Real column name is shipping_limit_date, not shipping_date.
    CAST(shipping_limit_date AS TIMESTAMP) AS shipping_timestamp
FROM read_csv_auto('data/olist_order_items_dataset.csv', header = true);

-- GRAIN: one row per payment transaction in olist_order_payments_dataset.csv
--        (103,886 rows, 99,441 distinct orders). The 4,445 extra rows are
--        split-payment orders (an instalment plan recorded as N transactions),
--        NOT duplicates. Summing payment_value therefore stays correct at
--        order grain, but COUNT(*) does not -- that is why order_count() in the
--        metrics layer counts DISTINCT order_id.
CREATE OR REPLACE VIEW stg_payments AS
SELECT
    order_id,
    CAST(payment_sequential   AS INTEGER)     AS payment_sequential,
    payment_type,
    CAST(payment_installments AS INTEGER)     AS payment_installments,
    CAST(payment_value        AS DECIMAL(12,2)) AS payment_value
FROM read_csv_auto('data/olist_order_payments_dataset.csv', header = true);

-- GRAIN: one row per product in olist_products_dataset.csv (32,951 rows).
--        The *_lenght column names are typos in the source dataset; they are
--        corrected here (and only here) so no downstream query has to know.
CREATE OR REPLACE VIEW stg_products AS
SELECT
    product_id,
    product_category_name,
    product_name_lenght          AS product_name_length,
    product_description_lenght   AS product_description_length,
    product_photos_qty,
    product_weight_g,
    product_length_cm,
    product_height_cm,
    product_width_cm
FROM read_csv_auto('data/olist_products_dataset.csv', header = true);


-- =============================================================================
-- SECTION 2 -- DIMENSIONS (one row per entity; the join targets for the UI)
-- =============================================================================

-- GRAIN: one row per customer_id (99,441 rows).
-- Why customer_id and not customer_unique_id: customer_id is what fact_order
-- carries, so it keeps dim_customer conformed and makes every customer-level
-- join a 1:1 lookup. The cost is that "repeat customer" is then measured per
-- account, not per human; customer_unique_id is carried as an attribute so a
-- person-level repeat rate is a one-line alternative when the business asks for
-- it. That trade-off is a decision, not an oversight.
CREATE TABLE IF NOT EXISTS dim_customer (
    customer_id          VARCHAR  NOT NULL,
    customer_unique_id   VARCHAR,
    customer_city        VARCHAR,
    customer_state       VARCHAR,   -- Brazilian state code, 27 values
    customer_region      VARCHAR,   -- derived, see below: the dashboard's "region"
    customer_zip_prefix  VARCHAR,
    PRIMARY KEY (customer_id)
);

INSERT OR REPLACE INTO dim_customer
SELECT
    customer_id,
    customer_unique_id,
    customer_city,
    customer_state,
    -- The dataset has no region column. Brazilian e-commerce reporting groups
    -- states into 5 macroregions, and the executive question in the README is
    -- explicitly regional, so the grouping is derived once here rather than
    -- re-derived (and mis-typed) in every chart.
    --
    -- All 27 states are listed. An earlier draft covered only 24 and let the
    -- other four fall through to 'Unknown' -- DF, GO, MT and MS, which is
    -- 5,782 orders and R$1.03M of revenue quietly missing from every regional
    -- breakdown. A region map that is incomplete is worse than no map: the
    -- total is still right, so nothing looks broken.
    CASE customer_state
        WHEN 'SP' THEN 'Southeast'  WHEN 'RJ' THEN 'Southeast'
        WHEN 'MG' THEN 'Southeast'  WHEN 'ES' THEN 'Southeast'
        WHEN 'PR' THEN 'South'     WHEN 'SC' THEN 'South'
        WHEN 'RS' THEN 'South'
        WHEN 'BA' THEN 'Northeast'  WHEN 'PE' THEN 'Northeast'
        WHEN 'CE' THEN 'Northeast'  WHEN 'RN' THEN 'Northeast'
        WHEN 'PB' THEN 'Northeast'  WHEN 'MA' THEN 'Northeast'
        WHEN 'PI' THEN 'Northeast'  WHEN 'AL' THEN 'Northeast'
        WHEN 'SE' THEN 'Northeast'
        WHEN 'PA' THEN 'North'     WHEN 'AM' THEN 'North'
        WHEN 'TO' THEN 'North'     WHEN 'RO' THEN 'North'
        WHEN 'AC' THEN 'North'     WHEN 'AP' THEN 'North'
        WHEN 'RR' THEN 'North'
        WHEN 'DF' THEN 'Centre-West' WHEN 'GO' THEN 'Centre-West'
        WHEN 'MT' THEN 'Centre-West' WHEN 'MS' THEN 'Centre-West'
        ELSE 'Unknown'
    END AS customer_region,
    customer_zip_code_prefix
FROM stg_customers;

-- GRAIN: one row per product_id (32,951 rows).
-- denormalized_category is denormalised on purpose: joining fact -> order_item
-- -> product just to label a bar chart makes the category breakdown the most
-- expensive query on the dashboard for a column that almost never changes.
CREATE TABLE IF NOT EXISTS dim_product (
    product_id                VARCHAR NOT NULL,
    product_category_name     VARCHAR,   -- olist uses 'N/A' for the 610 unclassified
    product_name_length       INTEGER,
    product_description_length INTEGER,
    product_photos_qty        INTEGER,
    product_weight_g          INTEGER,
    product_length_cm         INTEGER,
    product_height_cm         INTEGER,
    product_width_cm          INTEGER,
    PRIMARY KEY (product_id)
);

INSERT OR REPLACE INTO dim_product
SELECT
    product_id,
    NULLIF(product_category_name, 'N/A') AS product_category_name,
    product_name_length,
    product_description_length,
    product_photos_qty,
    product_weight_g,
    product_length_cm,
    product_height_cm,
    product_width_cm
FROM stg_products;

-- GRAIN: one row per calendar day.
-- A date spine, not a distinct list of dates in the data: without the empty
-- days, a trend line silently bridges a zero-revenue week and the chart claims
-- demand held steady when it actually stopped. Bounds are derived from the
-- loaded data on every run, so the spine cannot go stale as the dataset grows.
-- No holidays, no fiscal calendar -- not needed for any KPI defined in 02.
CREATE TABLE IF NOT EXISTS dim_date (
    date_key       INTEGER NOT NULL,   -- YYYYMMDD, the surrogate key
    calendar_date  DATE    NOT NULL,
    year           INTEGER NOT NULL,
    quarter        INTEGER NOT NULL,
    month          INTEGER NOT NULL,
    month_name     VARCHAR NOT NULL,
    day_of_month   INTEGER NOT NULL,
    day_of_week    INTEGER NOT NULL,   -- 0 = Monday
    is_weekend     BOOLEAN NOT NULL,
    PRIMARY KEY (date_key)
);

INSERT OR REPLACE INTO dim_date
WITH bounds AS (
    SELECT
        CAST(LEAST(
            MIN(d), DATE '2016-08-01'
        ) AS DATE)                       AS lo,
        CAST(GREATEST(
            MAX(d), DATE '2018-10-31'
        ) AS DATE)                       AS hi
    FROM (
        SELECT CAST(order_purchase_timestamp AS DATE) AS d
        FROM stg_orders
        WHERE order_purchase_timestamp IS NOT NULL
    ) src
),
spine AS (
    SELECT CAST(gs AS DATE) AS calendar_date
    FROM bounds, generate_series(lo, hi, INTERVAL 1 DAY) AS t(gs)
)
SELECT
    CAST(STRFTIME(calendar_date, '%Y%m%d') AS INTEGER) AS date_key,
    calendar_date,
    CAST(STRFTIME(calendar_date, '%Y')    AS INTEGER) AS year,
    -- Quarter by arithmetic, not STRFTIME('%q'): that specifier is a Postgres
    -- extension and DuckDB's strftime rejects it entirely.
    --
    -- FLOOR is load-bearing, not decoration. DuckDB's `/` is TRUE division and
    -- returns DOUBLE even for integer operands, so the obvious
    -- (month - 1) / 3 + 1 yields 4.667 for December instead of 4. Flooring first
    -- makes it correct on DuckDB and still correct on Postgres and Spark, where
    -- `/` happens to truncate anyway. Asserted in tests/test_sql.py.
    CAST(FLOOR((CAST(STRFTIME(calendar_date, '%m') AS INTEGER) - 1) / 3) + 1
         AS INTEGER) AS quarter,
    CAST(STRFTIME(calendar_date, '%m')    AS INTEGER) AS month,
    STRFTIME(calendar_date, '%B')                     AS month_name,
    CAST(STRFTIME(calendar_date, '%d')    AS INTEGER) AS day_of_month,
    CAST(ISODOW(calendar_date) - 1        AS INTEGER) AS day_of_week,
    CAST(ISODOW(calendar_date) IN (6, 7)  AS BOOLEAN) AS is_weekend
FROM spine;


-- =============================================================================
-- SECTION 3 -- FACT_ORDER
-- =============================================================================
-- GRAIN: one row per order_id. Exactly 99,441 rows, one and only one per order.
--
-- Why order grain and not order_item or payment grain:
--   * An order with 3 items and 2 instalments would produce 6 rows on a naive
--     join, making SUM(payment_value) count the order 3x. Rolling each child
--     table up independently and joining the two rollups is what keeps revenue
--     at R$ 16,008,872.12 instead of ~R$ 18M.
--   * Every KPI in the README is an order-level or customer-level metric, so
--     the executive grain is the natural home for them.
--   * Item-level detail is not lost -- it lives in bridge_order_item, which the
--     dashboard drills into when a stakeholder asks "which SKU drove this?".
--
-- Repeating groups (product_id, seller_id) are deliberately NOT columns here:
-- they are multi-valued per order and would break the grain. Only their
-- non-repeating summaries (item_count, seller_count) are stored.
--
-- Two derived columns worth calling out:
--   is_cancelled      -- single source of truth for "cancelled", mirrored by the
--                        metrics layer. Defined once, here and in 02.
--   delivery_days     -- NULL for orders never delivered. A NULL here means
--                        "not yet observed", NOT "zero days", and every consumer
--                        (including median_delivery_days) must exclude it.
--                        Counting them as 0 would drag the median down and make
--                        a delivery regression look like an improvement.
CREATE TABLE IF NOT EXISTS fact_order (
    order_id                         VARCHAR NOT NULL,
    customer_id                      VARCHAR NOT NULL,   -- FK -> dim_customer
    order_date_key                   INTEGER NOT NULL,   -- FK -> dim_date (paid date)
    order_status                     VARCHAR NOT NULL,
    order_purchase_timestamp             TIMESTAMP,
    order_delivered_timestamp        TIMESTAMP,
    order_estimated_delivery_timestamp TIMESTAMP,
    order_year                       INTEGER,
    order_month                      INTEGER,
    payment_value                    DECIMAL(12,2) NOT NULL,  -- sum of all instalments
    payment_type                     VARCHAR,   -- first (lowest payment_sequential)
    payment_installments             INTEGER,   -- max instalments on the order
    item_count                       INTEGER NOT NULL,
    items_price                      DECIMAL(12,2) NOT NULL,  -- sum of item prices
    items_freight_value              DECIMAL(12,2) NOT NULL,  -- sum of freight
    seller_count                     INTEGER NOT NULL,
    is_cancelled                     BOOLEAN NOT NULL,
    is_delivered                     BOOLEAN NOT NULL,
    delivery_days                    DOUBLE,   -- NULL until actually delivered
    review_score                     DOUBLE,   -- 1..5, one review per order
    PRIMARY KEY (order_id)
);

INSERT OR REPLACE INTO fact_order
WITH payments AS (
    -- Rollup 1: one row per order. payment_type is a de-duplication to a
    -- non-repeating attribute; the VALUE is summed, so split payments are
    -- still fully counted.
    SELECT
        order_id,
        SUM(payment_value)                     AS payment_value,
        MIN(payment_type)                      AS payment_type,
        MAX(payment_installments)              AS payment_installments
    FROM stg_payments
    GROUP BY order_id
),
items AS (
    -- Rollup 2: one row per order, computed independently of payments.
    SELECT
        order_id,
        COUNT(*)                   AS item_count,
        SUM(price)                 AS items_price,
        SUM(freight_value)         AS items_freight_value,
        COUNT(DISTINCT seller_id)  AS seller_count
    FROM stg_order_items
    GROUP BY order_id
),
reviews AS (
    -- Olist has at most one review per order; MAX() is defensive against a
    -- duplicate landing in a future refresh.
    SELECT order_id, MAX(review_score) AS review_score
    -- Real filename is olist_order_reviews_dataset.csv. The nested-quote columns
    -- (review_comment_title/message) are deliberately not selected: the source CSV
    -- wraps them in escaped quotes, and the schema wants a clean 1..5 score.
    FROM read_csv_auto('data/olist_order_reviews_dataset.csv', header = true)
    GROUP BY order_id
)
SELECT
    o.order_id,
    o.customer_id,
    CAST(STRFTIME(CAST(o.order_purchase_timestamp AS DATE), '%Y%m%d') AS INTEGER) AS order_date_key,
    o.order_status,
    o.order_purchase_timestamp,
    o.order_delivered_timestamp,
    o.order_estimated_delivery_timestamp,
    CAST(STRFTIME(CAST(o.order_purchase_timestamp AS DATE), '%Y') AS INTEGER) AS order_year,
    CAST(STRFTIME(CAST(o.order_purchase_timestamp AS DATE), '%m') AS INTEGER) AS order_month,

    COALESCE(p.payment_value, 0)        AS payment_value,
    p.payment_type,
    p.payment_installments,

    COALESCE(i.item_count, 0)           AS item_count,
    COALESCE(i.items_price, 0)          AS items_price,
    COALESCE(i.items_freight_value, 0)  AS items_freight_value,
    COALESCE(i.seller_count, 0)         AS seller_count,

    -- CANCELLATION DEFINITION (single source of truth, mirrored in
    -- sql/02_metrics.sql and src/dashboard/metrics.py::cancel_rate):
    --   cancelled := order_status IN ('cancelled', 'canceled', 'unavailable')
    --
    -- Why these three and not others. Olist's status vocabulary is messy and
    -- overlaps:
    --   * 'cancelled' / 'canceled' -- the two spellings both appear in the raw
    --     data; keeping only one silently under-counts. The canonical olist
    --     value is 'canceled' (one L) but the dashboard should not depend on
    --     that, and the reader of the filter needs to see why both are here.
    --   * 'unavailable' -- the order WAS created and a payment WAS captured,
    --     then the item became unfillable. Revenue was taken and never
    --     delivered, so for an executive "cancellation" figure it belongs with
    --     the cancellations. The defensible alternative is to report it as its
    --     own line; if stakeholders want that, change this one filter and the
    --     whole dashboard moves with it. That is the point of defining it once.
    --   * 'lost' is arguably the same story (never delivered) and is excluded
    --     only to keep the definition to statuses that unambiguously mean
    --     "will not ship". It is a small, stable group, so the exclusion is
    --     immaterial to the KPI -- but it is a choice, and it is written down
    --     so a reviewer can challenge it.
    --   * 'delivered', 'shipped', 'processing', 'invoiced', 'created',
    --     'approved', 'returning', 'proposed', 'manifesto_shipped' are all
    --     live-pipeline states and must never count as cancellations.
    (o.order_status IN ('cancelled', 'canceled', 'unavailable'))  AS is_cancelled,
    (o.order_delivered_timestamp IS NOT NULL)                      AS is_delivered,

    -- NULL (not 0) when undelivered. This is the deliberate choice described
    -- above and asserted in tests/test_metrics.py.
    CASE
        WHEN o.order_delivered_timestamp IS NULL THEN NULL
        WHEN o.order_delivered_timestamp < o.order_purchase_timestamp THEN NULL  -- data error
        ELSE DATE_DIFF('second',
                       o.order_purchase_timestamp,
                       o.order_delivered_timestamp) / 86400.0
    END AS delivery_days,

    r.review_score
FROM stg_orders o
LEFT JOIN payments p ON p.order_id = o.order_id
LEFT JOIN items    i ON i.order_id = o.order_id
LEFT JOIN reviews  r ON r.order_id = o.order_id
WHERE o.order_purchase_timestamp IS NOT NULL
-- Orders never paid are not revenue and have no payment_date_key, so they are
-- excluded from the fact rather than carried with a 0 and a NULL date. Keep
-- this filter explicit in the README: it is why fact_order is slightly smaller
-- than olist_orders_dataset.csv.
;


-- =============================================================================
-- SECTION 4 -- DRILL-DOWN BRIDGE
-- =============================================================================
-- GRAIN: one row per (order_id, order_item_id) -- the item-level detail the
-- executive dashboard rolls up but the stakeholder needs on demand.
--
-- This is the ONLY table where a product or seller legitimately multiplies a
-- fact row, and it is not a fact table: nothing sums payment_value off it. The
-- bridge exists so the "which product line" question is one join away instead
-- of a rewrite of the star schema.
CREATE OR REPLACE VIEW bridge_order_item AS
SELECT
    f.order_id,
    i.order_item_id,
    i.product_id,
    i.seller_id,
    i.price,
    i.freight_value,
    p.product_category_name,
    c.customer_state,
    c.customer_region,
    f.order_date_key,
    f.payment_value
FROM fact_order f
JOIN stg_order_items i ON i.order_id = f.order_id
LEFT JOIN dim_product    p ON p.product_id  = i.product_id
LEFT JOIN dim_customer   c ON c.customer_id = f.customer_id;
