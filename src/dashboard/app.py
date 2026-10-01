"""Executive dashboard over the olist star schema.

    streamlit run src/dashboard/app.py

Every number rendered here is read from a view in sql/02_metrics.sql. There is no
SUM() or COUNT() in this file -- that is the point of the metrics layer. A KPI
that lives in a chart config is a KPI nobody can review.

Layout follows the 90-second executive read: KPI row, then trend, then the
breakdown, then the outlier driving it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import pandas as pd
import plotly.express as px
import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


@st.cache_resource
def connect() -> duckdb.DuckDBPyConnection:
    """One in-memory DuckDB with the schema built once per server process."""
    con = duckdb.connect()
    con.execute(f"SET file_search_path = '{ROOT.as_posix()}'")
    for sql_file in ("sql/01_schema.sql", "sql/02_metrics.sql"):
        con.execute((ROOT / sql_file).read_text(encoding="utf-8"))
    return con


con = connect()


@st.cache_data
def query(sql: str) -> pd.DataFrame:
    return con.execute(sql).df()


st.set_page_config(page_title="Olist Executive Dashboard", layout="wide")

st.title("Olist — executive summary")
st.caption(
    "All figures from the metrics layer in `sql/02_metrics.sql`. "
    "Revenue reconciles to the published olist total of R$16,008,872.12."
)

# --- KPI row -----------------------------------------------------------------

kpi = query("SELECT * FROM v_kpi_summary").iloc[0]

cols = st.columns(5)
cols[0].metric("Revenue", f"R${kpi['revenue']:,.0f}")
cols[1].metric("Orders", f"{int(kpi['order_count']):,}")
cols[2].metric("AOV", f"R${kpi['aov']:,.2f}")
cols[3].metric("Median delivery", f"{kpi['median_delivery_days']:.1f} days")
cols[4].metric("Sellers", f"{int(kpi['seller_count']):,}")

repeat = query(
    """
    SELECT
        repeat_rate AS people,
        repeat_rate_by_account AS accounts,
        (SELECT COUNT(DISTINCT c.customer_unique_id) FROM dim_customer) AS n_people
    FROM v_kpi_summary
    """
).iloc[0]
st.caption(
    f"Repeat rate **{repeat['people']:.2%}** across {int(repeat['n_people']):,} people "
    f"vs **{repeat['accounts']:.2%}** at the account level. The gap is the referral "
    f"chain: one person holds several customer_ids, so keying on customer_id alone "
    f"reports a repeat rate that can never move off zero."
)

# --- trend -------------------------------------------------------------------

st.subheader("Revenue and orders by month")

monthly = query("SELECT * FROM v_revenue_by_month")
monthly["month"] = (
    monthly["order_year"].astype(str) + "-" + monthly["order_month"].astype(str).str.zfill(2)
)

left, right = st.columns([3, 2])

with left:
    fig = px.line(
        monthly, x="month", y="revenue",
        markers=True,
        title="Monthly revenue (R$)",
        labels={"month": "", "revenue": "Revenue (R$)"},
    )
    fig.update_layout(hovermode="x unified", showlegend=False, margin=dict(t=40, b=0))
    st.plotly_chart(fig, use_container_width=True)

with right:
    fig = px.bar(
        monthly, x="month", y="order_count",
        title="Monthly orders",
        labels={"month": "", "order_count": "Orders"},
    )
    fig.update_layout(showlegend=False, margin=dict(t=40, b=0))
    st.plotly_chart(fig, use_container_width=True)

# --- breakdown ---------------------------------------------------------------

st.subheader("Where the revenue and the cancellations are")

region = query("SELECT * FROM v_revenue_by_region")
region["cancellation_rate"] = region["cancelled_revenue"] / region["revenue"]

left, right = st.columns(2)

with left:
    fig = px.bar(
        region.sort_values("revenue", ascending=False),
        x="customer_region", y="revenue", color="cancelled_revenue",
        title="Revenue by region, split into cancelled",
        labels={"customer_region": "", "revenue": "Revenue (R$)",
                "cancelled_revenue": "Cancelled (R$)"},
    )
    fig.update_layout(legend_title_text="", margin=dict(t=40, b=0))
    st.plotly_chart(fig, use_container_width=True)

with right:
    st.dataframe(
        region[["customer_region", "order_count", "revenue", "cancelled_revenue",
                "cancellation_rate"]]
        .sort_values("cancellation_rate", ascending=False)
        .style.format(
            {"revenue": "R${:,.2f}", "cancelled_revenue": "R${:,.2f}",
             "cancellation_rate": "{:.2%}"}
        ),
        use_container_width=True, hide_index=True,
    )

# --- status quality ----------------------------------------------------------

st.subheader("Order status quality")

status = query("SELECT * FROM v_orders_by_status")
st.dataframe(
    status.style.format({"revenue": "R${:,.2f}"}), use_container_width=True, hide_index=True
)
st.caption(
    "olist's status vocabulary is genuinely messy: 'shipped' means handed to the "
    "carrier and not yet received by the customer, which is why it appears as its "
    "own line rather than being folded into 'delivered'."
)

# --- category drill-down -----------------------------------------------------

st.subheader("Product categories")

category = query("SELECT * FROM v_revenue_by_category LIMIT 20")
fig = px.bar(
    category.sort_values("revenue"),
    x="revenue", y="product_category_name", orientation="h",
    title="Top 20 categories by item revenue (R$)",
    labels={"revenue": "Item revenue (R$)", "product_category_name": ""},
)
fig.update_layout(showlegend=False, margin=dict(t=40, b=0))
st.plotly_chart(fig, use_container_width=True)
st.caption(
    "Category revenue is item-grain (`price`), not order revenue: an order spanning "
    "two categories cannot be attributed to either without an arbitrary split. The "
    "two therefore do not sum to the headline, by design."
)