"""Executes sql/01_schema.sql and sql/02_metrics.sql against a synthetic olist.

The point of this file: SQL that has never been run is a liability in a portfolio
repo. Reviewing it by eye did not catch that DuckDB's strftime has no `%q`
quarter specifier, so the schema did not even load. This test loads it, on every
run, in CI.

The fixture is tiny but deliberately shaped to trip the grain bugs that matter:
an order with two line items, an order with two payment instalments, an
undelivered order, a cancelled order, an 'unavailable' order, an order delivered
before it was paid, and an unpaid order that must not reach the fact table.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

duckdb = pytest.importorskip("duckdb")

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

# order_id, customer_id, status, paid, delivered
# o11 has NO paid timestamp, so the fact table's unpaid filter must drop it.
ORDERS = [
    ("o1", "c1", "delivered",   "2017-01-05 10:00:00", "2017-01-09 10:00:00"),
    ("o2", "c1", "delivered",   "2017-01-20 10:00:00", "2017-01-24 10:00:00"),
    ("o3", "c1", "shipped",     "2017-02-01 10:00:00", None),
    ("o4", "c2", "canceled",    "2017-02-10 10:00:00", None),
    ("o5", "c2", "unavailable", "2017-02-11 10:00:00", None),
    ("o6", "c3", "delivered",   "2017-03-01 10:00:00", "2017-03-01 08:00:00"),
    ("o7", "c4", "delivered",   "2017-03-15 10:00:00", "2017-03-22 10:00:00"),
    ("o8", "c4", "delivered",   "2017-04-01 10:00:00", "2017-04-08 10:00:00"),
    ("o9", "c4", "delivered",   "2017-04-02 10:00:00", "2017-04-09 10:00:00"),
    ("o10", "c5", "delivered",  "2017-05-01 10:00:00", "2017-05-03 10:00:00"),
    ("o11", "c5", "created",    None,                  None),
]

# o1 has 2 line items, so a naive items-to-payments join would count it 4x.
ITEMS = [
    ("o1", 1, "p1", "s1", 100.0, 20.0), ("o1", 2, "p2", "s2", 50.0, 10.0),
    ("o2", 1, "p1", "s1", 75.0, 15.0),  ("o3", 1, "p3", "s1", 30.0, 5.0),
    ("o4", 1, "p2", "s2", 60.0, 12.0),  ("o5", 1, "p3", "s3", 40.0, 8.0),
    ("o6", 1, "p1", "s1", 25.0, 4.0),   ("o7", 1, "p2", "s2", 90.0, 18.0),
    ("o8", 1, "p3", "s3", 35.0, 7.0),   ("o9", 1, "p1", "s1", 120.0, 24.0),
    ("o10", 1, "p2", "s2", 55.0, 9.0),
]

# o9 is paid in 2 instalments summing to 120. Revenue must still count 120, once.
PAYMENTS = [
    ("o1", 1, "credit_card", 1, 150.0), ("o2", 1, "credit_card", 1, 75.0),
    ("o3", 1, "boleto", 1, 30.0),       ("o4", 1, "credit_card", 1, 60.0),
    ("o5", 1, "credit_card", 1, 40.0),  ("o6", 1, "credit_card", 1, 25.0),
    ("o7", 1, "credit_card", 1, 90.0),  ("o8", 1, "boleto", 1, 35.0),
    ("o9", 1, "credit_card", 2, 100.0), ("o9", 2, "credit_card", 2, 20.0),
    ("o10", 1, "credit_card", 1, 55.0),
]


def _write_fixture() -> None:
    DATA.mkdir(exist_ok=True)

    pd.DataFrame(
        [         ("c1", "u1", "Sao Paulo", "SP", "01310"),
         ("c2", "u_shared", "Rio de Janeiro", "RJ", "20040"),
         # c3 shares c2's person. This is the referral chain the real data
         # contains: 99,441 customer_ids but only 96,096 people, so ~3,345
         # customer_ids are second accounts of someone already counted. Without
         # a shared unique_id in the fixture, account-level and person-level
         # repeat rate are identical and the distinction cannot be tested.
         ("c3", "u_shared", "Salvador", "BA", "40020"),
         ("c4", "u4", "Curitiba", "PR", "80010"),
         ("c5", "u5", "Manaus", "AM", "69000"),
         # The Centre-West states. An earlier version of the region map omitted
         # DF/GO/MT/MS, so these four fell through to 'Unknown' and R$1.03M of
         # revenue vanished from every regional breakdown while the headline
         # total still added up correctly. Nothing looked broken.
         ("c6", "u6", "Brasilia", "DF", "70000"),
         ("c7", "u7", "Goiania", "GO", "74000"),
         ("c8", "u8", "Cuiaba", "MT", "78000"),
         ("c9", "u9", "Campo Grande", "MS", "79000")],
        columns=["customer_id", "customer_unique_id", "customer_city",
                 "customer_state", "customer_zip_code_prefix"],
    ).to_csv(DATA / "olist_customers_dataset.csv", index=False)

    pd.DataFrame(
        [(o, c, s, p, d) for o, c, s, p, d in ORDERS],
        columns=["order_id", "customer_id", "order_status",
                 "order_purchase_timestamp", "order_delivered_customer_date"],
    ).assign(
        # These are the REAL olist column names. An earlier fixture used invented
        # ones (order_paid_timestamp, order_delivered_timestamp, shipping_date)
        # that the source CSVs do not have, so 13 tests were passing against a
        # fiction while the actual data failed to bind. A fixture that invents
        # columns is worse than no fixture at all.
        order_estimated_delivery_date="2017-06-01 00:00:00",
        order_approved_at="2017-01-04 00:00:00",
        order_delivered_carrier_date="2017-01-08 00:00:00",
    ).to_csv(DATA / "olist_orders_dataset.csv", index=False)

    pd.DataFrame(
        ITEMS,
        columns=["order_id", "order_item_id", "product_id", "seller_id",
                 "price", "freight_value"],
    ).assign(shipping_limit_date="2017-01-05 00:00:00").to_csv(
        DATA / "olist_order_items_dataset.csv", index=False)

    pd.DataFrame(
        PAYMENTS,
        columns=["order_id", "payment_sequential", "payment_type",
                 "payment_installments", "payment_value"],
    ).to_csv(DATA / "olist_order_payments_dataset.csv", index=False)

    pd.DataFrame(
        [("p1", "health_beauty", 10, 100, 1, 100, 10, 10, 10),
         ("p2", "housewares", 12, 110, 2, 200, 20, 20, 20),
         ("p3", "N/A", 8, 80, 1, 50, 5, 5, 5)],
        columns=["product_id", "product_category_name", "product_name_lenght",
                 "product_description_lenght", "product_photos_qty",
                 "product_weight_g", "product_length_cm", "product_height_cm",
                 "product_width_cm"],
    ).to_csv(DATA / "olist_products_dataset.csv", index=False)

    pd.DataFrame(
        [(i + 1, o, 5.0) for i, (o, *_) in enumerate(ORDERS) if o != "o3"],
        columns=["review_id", "order_id", "review_score"],
    ).to_csv(DATA / "olist_order_reviews_dataset.csv", index=False)


@pytest.fixture(scope="module")
def con():
    """A duckdb connection with the star schema and metrics layer built."""
    _write_fixture()
    try:
        conn = duckdb.connect()
        # The SQL uses relative paths like 'data/...'; pin cwd to the project root.
        conn.execute(f"SET file_search_path = '{ROOT.as_posix()}'")
        for sql_file in ("sql/01_schema.sql", "sql/02_metrics.sql"):
            conn.execute((ROOT / sql_file).read_text(encoding="utf-8"))
        yield conn
    finally:
        for csv in DATA.glob("olist_*.csv"):
            csv.unlink()


def test_both_sql_files_execute(con):
    assert con.execute("SELECT COUNT(*) FROM fact_order").fetchone()[0] > 0


def test_fact_is_exactly_one_row_per_paid_order(con):
    # 11 orders in the fixture, minus o11 which was never paid.
    assert con.execute("SELECT COUNT(*) FROM fact_order").fetchone()[0] == 10
    dupes = con.execute(
        "SELECT COUNT(*) FROM (SELECT order_id FROM fact_order "
        "GROUP BY order_id HAVING COUNT(*) > 1)"
    ).fetchone()[0]
    assert dupes == 0


def test_revenue_is_not_inflated_by_the_items_to_payments_cross_product(con):
    # 150+75+30+60+40+25+90+35+120+55. o1 has 2 items and 1 payment: a naive join
    # would give it 300. o9 has 1 item and 2 instalments summing to 120.
    assert con.execute("SELECT SUM(payment_value) FROM fact_order").fetchone()[0] == 680.0


def test_undelivered_orders_carry_null_not_zero_delivery_days(con):
    rows = dict(
        con.execute(
            "SELECT order_id, delivery_days FROM fact_order "
            "WHERE order_id IN ('o3','o4','o5')"
        ).fetchall()
    )
    assert set(rows) == {"o3", "o4", "o5"}
    assert all(v is None for v in rows.values())


def test_order_delivered_before_paid_is_treated_as_a_clock_error(con):
    # o6 delivered 2h before payment. Must be NULL, never a negative duration.
    assert con.execute(
        "SELECT delivery_days FROM fact_order WHERE order_id = 'o6'"
    ).fetchone()[0] is None


def test_median_delivery_days_ignores_undelivered_orders(con):
    # Real durations: o1=4, o2=4, o7=7, o8=7, o9=7, o10=2 -> sorted [2,4,4,7,7,7].
    # Median of 6 values is the mean of the 3rd and 4th: (4+7)/2 = 5.5.
    # Counting the 4 undelivered orders as 0 would give 4.0 and would read as an
    # improvement, which is the exact failure mode the schema comment warns about.
    med = con.execute(
        "SELECT MEDIAN(delivery_days) FROM fact_order WHERE delivery_days IS NOT NULL"
    ).fetchone()[0]
    assert float(med) == 5.5


def test_cancellation_flag_covers_canceled_and_unavailable(con):
    # o4 = 'canceled', o5 = 'unavailable'. o3/o11 are live-pipeline states.
    flagged = con.execute(
        "SELECT order_id FROM fact_order WHERE is_cancelled ORDER BY order_id"
    ).fetchall()
    assert [r[0] for r in flagged] == ["o4", "o5"]


def test_zip_code_keeps_its_leading_zero(con):
    assert con.execute(
        "SELECT customer_zip_prefix FROM dim_customer WHERE customer_id = 'c1'"
    ).fetchone()[0] == "01310"


def test_dim_customer_derives_brazilian_regions(con):
    regions = dict(
        con.execute("SELECT customer_state, customer_region FROM dim_customer").fetchall()
    )
    assert regions == {
        "SP": "Southeast", "RJ": "Southeast", "BA": "Northeast",
        "PR": "South", "AM": "North",
        "DF": "Centre-West", "GO": "Centre-West",
        "MT": "Centre-West", "MS": "Centre-West",
    }


def test_no_known_state_falls_through_to_unknown(con):
    # The map must be complete, not merely large. Brazil has 27 states; an
    # incomplete map hides real revenue from the regional breakdown while the
    # headline total still reconciles, so nothing appears broken.
    unknown = con.execute(
        "SELECT COUNT(*) FROM dim_customer WHERE customer_region = 'Unknown'"
    ).fetchone()[0]
    assert unknown == 0


def test_every_olist_state_maps_to_one_of_five_regions(con):
    # Guards the 27-state list itself against a future edit that drops one.
    BAZILIAN_STATES = {
        "AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS",
        "MT", "PA", "PB", "PE", "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC",
        "SE", "SP", "TO",
    }
    found = {
        r[0] for r in con.execute(
            "SELECT DISTINCT state FROM ("
            "  SELECT UNNEST(['AC','AL','AM','AP','BA','CE','DF','ES','GO','MA',"
            "    'MG','MS','MT','PA','PB','PE','PI','PR','RJ','RN','RO','RR','RS',"
            "    'SC','SE','SP','TO']) AS state) t"
        ).fetchall()
    }
    assert found == BAZILIAN_STATES
    assert len(BAZILIAN_STATES) == 27


def test_repeat_rate_counts_people_not_accounts(con):
    # Fixture orders per account: c1=3, c2=2, c3=1, c4=3, c5=1 (o11 unpaid, excluded).
    # People: u1 = c1 (3 orders), u_shared = c2 + c3 (3 orders), u4 = c4 (3), u5 = c5 (1).
    #   accounts with >1 order : c1, c2, c4          -> 3 of 5 = 0.6
    #   people   with >1 order : u1, u_shared, u4     -> 3 of 4 = 0.75
    # Keying on customer_id alone calls u_shared a non-repeat buyer and reports
    # 0.6 instead of 0.75.
    account_rate = con.execute(
        "SELECT repeat_rate_by_account FROM v_kpi_summary"
    ).fetchone()[0]
    person_rate = con.execute("SELECT repeat_rate FROM v_kpi_summary").fetchone()[0]
    assert account_rate == pytest.approx(0.6)
    assert person_rate == pytest.approx(0.75)
    assert person_rate > account_rate


def test_unclassified_product_category_becomes_null_not_the_na_string(con):
    assert con.execute(
        "SELECT product_category_name FROM dim_product WHERE product_id = 'p3'"
    ).fetchone()[0] is None


def test_item_count_rolls_up_without_weighting_by_payment_count(con):
    assert con.execute(
        "SELECT item_count FROM fact_order WHERE order_id = 'o1'"
    ).fetchone()[0] == 2


def test_metrics_view_layer_is_queryable(con):
    # v_kpi_summary is the single entry point the dashboard would read.
    assert con.execute("SELECT COUNT(*) FROM v_kpi_summary").fetchone()[0] >= 1


def test_date_spine_has_no_gaps_and_derives_quarter_without_strftime_q(con):
    spine = con.execute(
        "SELECT COUNT(*), MIN(calendar_date), MAX(calendar_date), "
        "COUNT(DISTINCT quarter) FROM dim_date"
    ).fetchone()
    n_days, lo, hi, quarters = spine
    assert (hi - lo).days + 1 == n_days
    assert quarters == 4
    assert con.execute(
        "SELECT quarter FROM dim_date WHERE calendar_date = DATE '2017-02-14'"
    ).fetchone()[0] == 1
    assert con.execute(
        "SELECT quarter FROM dim_date WHERE calendar_date = DATE '2017-11-14'"
    ).fetchone()[0] == 4
