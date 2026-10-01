# 04 — Interactive Executive Dashboard

**The skill that 95% of candidates don't have.** Every other project here proves you can
model. This proves you can *communicate*, which is what a Data Scientist actually gets paid
for. It's also the fastest to build, so keep it tight and move on.

## Business question

> Monthly revenue looks fine in aggregate but underperforming in two regions. Which region,
> which product line, and is it price or volume? The VP has 90 seconds.

## Dataset

Pick one with an obvious story and real messiness:

- `olist` — Olist Brazilian e-commerce, ~100k orders. Best choice: messy, multi-table,
  has delivery delays and payment types worth segmenting.
- `superstore` — too easy. It looks clean on a dashboard and hides the work.
- `datauniverse` — the actual data universe CSV, small and sales-shaped.

Olist. It has the joins, nulls, and the "wait is it the order date or the delivery date"
problem that makes a real dataset feel real.

## Stack

SQL (DuckDB is enough — no server) · Python for transforms · Plotly Dash, Streamlit, or
Power BI

Pick **one**. Streamlit if you want it in your repo as code. Power BI if you want the BI
tool named on your resume. Dash if you want to show you can write the framework yourself.
Two dashboards is padding, not breadth.

## Method

1. **Model it in SQL first.** Star schema: `dim_customer`, `dim_product`, `dim_date`,
   `fact_order`. Write the schema out in the README. The modeling *is* the project.
2. **Metrics layer** — define revenue, orders, AOV, repeat rate, cancellation rate, and
   median delivery days as named, documented metrics. One place. Every KPI on the dashboard
   comes from here, not from ad-hoc expressions.
3. **Build for the 90-second read** — the layout answers "what changed, where, and why."
   KPI row → trend → breakdown by dimension → the specific outlier driving it.
4. **Interactivity that earns its place** — date range and segment filters. Drill-down to
   the order-level rows behind an aggregate.
5. **Make it survive contact with a stakeholder** — loading state, empty state, an error
   state, and a note on data freshness ("as of 2026-09-29"). Real dashboards break; the
   ones that handle it get trusted.

## Verified results

Run against the real dataset. **Revenue reconciles to R$16,008,872.12**, the published
olist total, which is the single strongest available check that the grain decisions
and the two independent rollups are right.

| KPI | Value |
|---|---|
| Revenue | R$16,008,872.12 |
| Orders | 99,441 |
| AOV | R$160.99 |
| Cancellation rate | 1.24% |
| Repeat rate (by person) | 3.12% |
| Repeat rate (by account) | 0.00% |
| Median delivery | 10.22 days |
| Sellers | 3,095 |

Regional breakdown, summing exactly to the headline:

| Region | Orders | Revenue | Cancelled revenue |
|---|---|---|---|
| Southeast | 68,266 | R$10,340,831.46 | R$179,335.07 |
| South | 14,148 | R$2,325,141.35 | R$44,503.84 |
| Northeast | 9,394 | R$1,898,479.44 | R$25,502.96 |
| Centre-West | 5,782 | R$1,029,797.52 | R$15,531.56 |
| North | 1,851 | R$414,622.35 | R$4,861.68 |

## Four bugs the real data found that the synthetic fixture did not

Every one of these was invisible until the actual CSVs were loaded. They are the
argument for this file existing, and the reason the first draft of the SQL — 434
lines, carefully commented, entirely unrun — was wrong.

1. **Every raw column name in the orders file was invented.** There is no
   `order_paid_timestamp` and no `order_delivered_timestamp`; the real columns are
   `order_purchase_timestamp` and `order_delivered_customer_date`. Likewise
   `shipping_date` is really `shipping_limit_date`, and the reviews file is
   `olist_order_reviews_dataset.csv`. The whole schema failed to bind.
2. **`repeat_rate` was structurally 0.00%.** This copy of olist has 99,441
   `customer_id`s — one generated per order — so at the account level nobody ever
   repeats and the KPI can never move. There are 96,096 `customer_unique_id`s, so
   person identity survives. The metric is now keyed on people (3.12%) with the
   account-level figure kept alongside it (0.00%) as the explanation.
3. **The region map covered 24 of Brazil's 27 states.** DF, GO, MT and MS fell
   through to `Unknown`, hiding 5,782 orders and R$1.03M from every regional
   breakdown — while the headline total still reconciled, so nothing looked broken.
4. **There is no payment timestamp anywhere in olist.** The payments table has no
   time column at all, so the schema uses the purchase date and names it
   `order_purchase_timestamp` throughout. Any metric needing time-to-payment (DSO,
   cash conversion) is unbuildable from this dataset, and that limitation is now
   documented instead of papered over.

Earlier, executing the SQL against a synthetic fixture also caught two more: DuckDB's
`strftime` has no `%q` quarter specifier, and DuckDB's `/` is true division rather
than integer division, so `(month-1)/3+1` returned `4.667` for December.

## Done means

- [x] Star schema in DuckDB, documented, with grain on every object
- [x] 7 KPI views + `v_kpi_summary` single entry point
- [x] `tests/test_sql.py` — 16 tests that **execute** both SQL files
- [x] 58 tests total
- [x] Revenue reconciles to the published olist total
- [x] Regional breakdown sums to the headline exactly, zero `Unknown`
- [ ] *(yours)* Pick Streamlit or Power BI — one, not both
- [ ] *(yours)* Build the dashboard and write the three insights
- [ ] *(yours)* git init + commit

## The three insights

Not a chart dump. Three sentences, each with a number: *"Median delivery time in the North
region rose 2.3 days since June and correlates with the 12% cancellation increase — it's
the top driver of lost revenue, roughly R$410k."* That sentence gets you the interview.

## Interview questions this buys

*"Walk me through this dashboard. What decision would you make differently on Monday?"* ·
*"What metric is missing here?"* — the fact that you know what's missing is the answer.

## Resume bullet (fill in real numbers after)

> Modeled 100k e-commerce orders into a DuckDB star schema and built an interactive Plotly
> dashboard surfacing three quantified revenue insights, including a R$410k cancellation
> driver traced to a 2.3-day regional delivery regression.
