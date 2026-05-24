"""Weekly PSP Dashboard — Streamlit app (v4: dynamic dimensions).

Mirrors the weekly-psp-report skill (v3) with interactive filters AND user-selected
dimensions for column splitting (up to 2 stacked, e.g. Semaine × Verticale).

Filters apply server-side via WHERE clauses; results cached 24h via @st.cache_data.

Run locally:
    gcloud auth application-default login
    pip install -r requirements.txt
    streamlit run app.py

Deploy to Cloud Run / Streamlit Cloud: see README.md.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Iterable

import pandas as pd
import streamlit as st
from google.cloud import bigquery

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PROJECT_ID = os.environ.get("BQ_PROJECT", "eu-andy-marketing-raw")
CACHE_TTL = 24 * 60 * 60  # 24h

st.set_page_config(
    page_title="Weekly PSP Report",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Light visual polish — table sticky header / numeric alignment
st.markdown(
    """
    <style>
      .block-container { padding-top: 1.2rem; padding-bottom: 2rem; max-width: 1600px; }
      [data-testid="stDataFrame"] td { font-variant-numeric: tabular-nums; }
      .meta-box { padding: 10px 14px; background: #f1f5f9; border-left: 4px solid #6366f1;
                  border-radius: 4px; font-size: 13px; color: #475569; margin-bottom: 12px; }
      .meta-box b { color: #0f172a; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Password gate (optional — only active if "app_password" set in secrets)
# ---------------------------------------------------------------------------
def _require_password() -> None:
    expected = st.secrets.get("app_password", "")
    if not expected:
        return
    if st.session_state.get("authed"):
        return
    st.title("🔒 Accès protégé")
    pwd = st.text_input(
        "Entre le mot de passe pour accéder au dashboard",
        type="password",
        key="pwd_input",
    )
    if pwd == expected:
        st.session_state["authed"] = True
        st.rerun()
    elif pwd:
        st.error("Mot de passe incorrect")
    st.stop()


_require_password()


# ---------------------------------------------------------------------------
# BigQuery client (resource cache, shared across reruns)
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner=False)
def get_bq_client() -> bigquery.Client:
    # Streamlit Cloud → read SA from secrets; local dev → fall back to ADC.
    if "gcp_service_account" in st.secrets:
        from google.oauth2 import service_account
        info = dict(st.secrets["gcp_service_account"])
        creds = service_account.Credentials.from_service_account_info(info)
        return bigquery.Client(project=info.get("project_id", PROJECT_ID), credentials=creds)
    return bigquery.Client(project=PROJECT_ID)


@st.cache_data(ttl=CACHE_TTL, show_spinner=False)
def run_query(sql: str) -> pd.DataFrame:
    """Run a BigQuery SQL string and return a pandas DataFrame.
    Cached for 24h per unique SQL string (filter values are inlined in SQL)."""
    job = get_bq_client().query(sql)
    return job.result().to_dataframe(create_bqstorage_client=False)


# ---------------------------------------------------------------------------
# Filter dimensions (sidebar multi-select filters applied as WHERE clauses)
# ---------------------------------------------------------------------------
FILTER_DIMS = [
    # (filter key, label, fm column-or-expression, ft column-or-expression)
    # If the spec contains "{alias}" it is treated as a raw SQL expression
    # (alias substituted with fm/ft); otherwise it's a plain column name.
    # NOTE: PRICE_EXPR is defined below — for "price" we use that same bucket
    # expression so the filter values match the dimension values.
    ("psp", "PSP", "ms_default_psp", "ms_default_psp"),
    ("brand", "Brand", "brand", "t_brand"),
    ("verticale", "Verticale", "sgw_verticale", "sgw_verticale"),
    ("currency", "Currency", "ms_currency", "ms_currency"),
    # "price" is reassigned just below DIMENSION_DIMS (after PRICE_EXPR is defined)
    ("price", "Prix abonnement", "price_name", "price_name"),
]


# ---------------------------------------------------------------------------
# Display dimensions (column-axis splits — user-selectable, max 2 stacked)
# The "week" dim is special: cohort_week is always available; for non-week dims
# we project COALESCE(...,'') from fm / ft and group by them.
# ---------------------------------------------------------------------------
# Price bucketing expression — depends on brand_type
# Booking: round to nearest 10€ → 19€ / 29€ / 49€ / 59€ / 69€ buckets
# Magazine: exact match on ms_price_amount_eur → 25ct / 10ct / 1ct / 1€ / 1.90€
# {alias} is substituted to fm/ft/t by dim_select_clause depending on scope.
PRICE_EXPR = (
    "CASE "
    "WHEN {alias}.brand_type = 'Booking' THEN "
    "  CASE CAST(ROUND(COALESCE({alias}.rounded_subscription_price, 0)) AS INT64) "
    "    WHEN 20 THEN '19€ bi-mensuel' "
    "    WHEN 30 THEN '29€' "
    "    WHEN 50 THEN '49€ mensuel' "
    "    WHEN 60 THEN '59€' "
    "    WHEN 70 THEN '69€ mensuel' "
    "    ELSE CONCAT(CAST(CAST(ROUND(COALESCE({alias}.rounded_subscription_price, 0)) AS INT64) AS STRING), '€') "
    "  END "
    "WHEN {alias}.brand_type = 'Magazine' THEN "
    "  CASE "
    "    WHEN ROUND({alias}.ms_price_amount_eur, 2) = 0.25 THEN '25ct weekly' "
    "    WHEN ROUND({alias}.ms_price_amount_eur, 2) = 0.10 THEN '10ct weekly' "
    "    WHEN ROUND({alias}.ms_price_amount_eur, 2) = 0.01 THEN '1ct' "
    "    WHEN ROUND({alias}.ms_price_amount_eur, 2) = 1.00 THEN '1€' "
    "    WHEN ROUND({alias}.ms_price_amount_eur, 2) = 1.90 THEN '1.90€' "
    "    ELSE CONCAT(CAST(ROUND({alias}.ms_price_amount_eur, 2) AS STRING), '€') "
    "  END "
    "ELSE '' END"
)

DIMENSION_DIMS = [
    # (key, label, fm column-or-expression, ft column-or-expression)
    # If the spec contains "{alias}" it is treated as a raw SQL expression
    # (alias substituted with fm/ft/t); otherwise it's a plain column name.
    ("week", "Semaine", None, None),
    ("verticale", "Verticale", "sgw_verticale", "sgw_verticale"),
    ("psp", "PSP", "ms_default_psp", "ms_default_psp"),
    ("currency", "Currency", "ms_currency", "ms_currency"),
    ("booking_market", "Booking Market", "sgw_booking_market", "sgw_booking_market"),
    ("bid_market", "Bid Market", "gad_market", "gad_market"),
    ("price", "Prix abonnement", PRICE_EXPR, PRICE_EXPR),
]

DIM_BY_LABEL = {d[1]: d for d in DIMENSION_DIMS}

# Now that PRICE_EXPR is defined, rewire the "price" filter to use the same
# bucket expression as the dimension (so filter values match dimension values).
FILTER_DIMS = [
    (k, l, PRICE_EXPR if k == "price" else fm, PRICE_EXPR if k == "price" else ft)
    for (k, l, fm, ft) in FILTER_DIMS
]


def selected_dims(labels: list) -> list:
    """Resolve user-chosen labels to dimension specs (in chosen order)."""
    return [DIM_BY_LABEL[l] for l in labels if l in DIM_BY_LABEL]


def non_week_dims(dims: list) -> list:
    return [d for d in dims if d[0] != "week"]


def dim_select_clause(scope: str, dims: list, indent: int = 4) -> str:
    """SELECT projections for non-week dim cols. Always ends with a trailing comma+newline.

    If the dim spec contains '{alias}' it is treated as a SQL expression (alias
    substituted with the scope). Otherwise it's a plain column name and is
    wrapped with COALESCE(scope.col, '').
    """
    nw = non_week_dims(dims)
    if not nw:
        return ""
    sp = " " * indent
    lines = []
    for key, _label, fm_col, ft_col in nw:
        col_spec = fm_col if scope == "fm" else ft_col
        if "{alias}" in col_spec:
            expr = col_spec.format(alias=scope)
            lines.append(f"{sp}COALESCE({expr}, '') AS dim_{key}")
        else:
            lines.append(f"{sp}COALESCE({scope}.{col_spec}, '') AS dim_{key}")
    return ",\n".join(lines) + ",\n"


def dim_pass_clause(alias: str, dims: list, indent: int = 4) -> str:
    """Pass-through projections (e.g. e.dim_verticale, e.dim_psp). With trailing comma if non-empty."""
    nw = non_week_dims(dims)
    if not nw:
        return ""
    sp = " " * indent
    cols = [f"{sp}{alias}.dim_{d[0]}" for d in nw]
    return ",\n".join(cols) + ",\n"


def dim_cols_bare(dims: list, alias: str = "") -> str:
    """Bare 'dim_X, dim_Y' (no trailing comma). Optionally prefixed with alias."""
    nw = non_week_dims(dims)
    if not nw:
        return ""
    pfx = f"{alias}." if alias else ""
    return ", ".join(f"{pfx}dim_{d[0]}" for d in nw)


def dim_cols_trailing(dims: list, alias: str = "") -> str:
    """Returns 'dim_X, dim_Y, ' with trailing comma, or '' if no non-week dims."""
    bare = dim_cols_bare(dims, alias)
    return bare + ", " if bare else ""


def dim_join_on(left_alias: str, right_alias: str, dims: list) -> str:
    """ON-clause additions for joining on dim cols. Returns ' AND a.dim_X = b.dim_X ...' (with leading AND)."""
    nw = non_week_dims(dims)
    if not nw:
        return ""
    return "".join(
        f" AND {left_alias}.dim_{d[0]} = {right_alias}.dim_{d[0]}" for d in nw
    )


# ---------------------------------------------------------------------------
# SQL helpers
# ---------------------------------------------------------------------------
WEEKS_CTE = """
WITH weeks AS (
  SELECT
    DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL (w * 7) DAY), WEEK(MONDAY)) AS week_start,
    FORMAT_DATE('S%V', DATE_TRUNC(DATE_SUB(CURRENT_DATE(), INTERVAL (w * 7) DAY), WEEK(MONDAY))) AS week_label
  FROM UNNEST(GENERATE_ARRAY(1, 10)) AS w
), window_bounds AS (
  SELECT MIN(week_start) AS ws_min, DATE_ADD(MAX(week_start), INTERVAL 6 DAY) AS ws_max FROM weeks
)
"""


def sql_escape(value: str) -> str:
    return value.replace("'", "''")


def filter_clauses(scope: str, filters: dict) -> str:
    """Build AND-prefixed SQL clauses for the active filters.
    scope is 'fm' (memberships) or 'ft' (transactions).

    Filter specs containing '{alias}' are treated as raw SQL expressions
    (alias substituted with scope). Otherwise they're plain column names.
    """
    lines = []
    for key, _label, fm_col, ft_col in FILTER_DIMS:
        vals = filters.get(key, [])
        if not vals:
            continue
        col_spec = fm_col if scope == "fm" else ft_col
        if "{alias}" in col_spec:
            target = col_spec.format(alias=scope)
        else:
            target = f"{scope}.{col_spec}"
        rendered = ", ".join(f"'{sql_escape(v)}'" if v != "(empty)" else "''" for v in vals)
        has_empty = "(empty)" in vals
        if has_empty:
            lines.append(f"AND ({target} IN ({rendered}) OR {target} IS NULL)")
        else:
            lines.append(f"AND {target} IN ({rendered})")
    return "\n    ".join(lines)


# ---------------------------------------------------------------------------
# Funnel query (one per brand_type) — supports dynamic dimensions
# ---------------------------------------------------------------------------
def funnel_sql(brand_type: str, filters: dict, dims: list) -> str:
    is_booking = brand_type == "Booking"
    token_filter = (
        "AND NOT (sm.Segment = 'unknown' AND sm.Country = '' AND sm.Language = '')"
        if is_booking
        else ""
    )
    helpprio_fm = (
        "AND fm.brand != 'helpprio.com'"
        if is_booking
        else "AND COALESCE(fm.brand, '') NOT LIKE '%helpprio%'"
    )
    helpprio_ft = (
        "AND COALESCE(ft.t_brand, '') != 'helpprio.com'"
        if is_booking
        else "AND COALESCE(ft.t_brand, '') NOT LIKE '%helpprio%'"
    )
    default_days = 14 if is_booking else 7
    fm_filter = filter_clauses("fm", filters)
    ft_filter = filter_clauses("ft", filters)

    # Dim helpers
    bm_dim_sel = dim_select_clause("fm", dims)             # fm.<col> AS dim_X, ...
    elig_dim_pass = dim_pass_clause("bm", dims)            # bm.dim_X, ...
    btx_dim_pass = dim_pass_clause("e", dims)              # e.dim_X, ...
    dims_only = dim_cols_bare(dims)                        # dim_X, dim_Y (no trailing)
    dims_only_trailing = dim_cols_trailing(dims)           # dim_X, dim_Y, (trailing)
    dim_on_r0 = dim_join_on("r0", "da", dims)
    dim_on_r1 = dim_join_on("r1", "da", dims)
    dim_on_s = dim_join_on("s", "da", dims)
    dim_on_tbb = dim_join_on("tbb", "da", dims)
    has_nw = bool(non_week_dims(dims))
    dim_axis_join = (
        f"CROSS JOIN dim_axis da\n" if has_nw else ""
    )
    final_dim_select = (
        ", ".join(f"da.dim_{d[0]} AS dim_{d[0]}" for d in non_week_dims(dims)) + ",\n  "
        if has_nw
        else ""
    )

    # Build dim_axis CTE only if needed
    dim_axis_cte = (
        f",\ndim_axis AS (\n  SELECT DISTINCT {dims_only} FROM bm\n)"
        if has_nw
        else ""
    )

    return f"""
DECLARE cutoff_ts TIMESTAMP DEFAULT TIMESTAMP(CURRENT_DATE());
DECLARE default_days INT64 DEFAULT {default_days};
{WEEKS_CTE},
bm AS (
  SELECT
    DATE_TRUNC(DATE(c.CreatedAtUtc), WEEK(MONDAY)) AS cohort_week,
{bm_dim_sel}    fm.customer_email, fm.ms_status,
    sm.CancelAtUtc, sm.TrialEndUtc
  FROM `eu-andy-marketing-raw.dashboard.fact_memberships` fm
  JOIN `eu-andy-marketing-raw.silver_sgw.stg_customers` c ON fm.customer_id = c.Id
  JOIN `eu-andy-marketing-raw.silver_sgw.stg_memberships` sm
    ON CAST(fm.membership_id AS STRING) = CAST(sm.Id AS STRING)
  CROSS JOIN window_bounds wb
  WHERE DATE(c.CreatedAtUtc) BETWEEN wb.ws_min AND wb.ws_max
    AND DATE_TRUNC(DATE(c.CreatedAtUtc), WEEK(MONDAY)) IN (SELECT week_start FROM weeks)
    AND fm.brand_type = '{brand_type}'
    {helpprio_fm}
    AND LOWER(fm.customer_email) NOT LIKE '%@yopmail%'
    AND LOWER(fm.customer_email) NOT LIKE '%@sharebot%'
    AND LOWER(fm.customer_firstname) NOT LIKE '%test%'
    {token_filter}
    {fm_filter}
){dim_axis_cte},
r0_stats AS (
  SELECT cohort_week, {dims_only_trailing}
    COUNT(DISTINCT customer_email) AS r0_attempts,
    COUNT(DISTINCT CASE WHEN ms_status NOT IN ('abandonned','processing','paused') THEN customer_email END) AS r0_succeeded,
    COUNT(DISTINCT CASE WHEN ms_status NOT IN ('abandonned','processing','paused')
        AND CancelAtUtc IS NOT NULL AND CancelAtUtc < TrialEndUtc THEN customer_email END) AS unsub_trial
  FROM bm GROUP BY ALL
),
elig_users AS (
  SELECT DISTINCT cohort_week, {dims_only_trailing}customer_email FROM bm
  WHERE ms_status NOT IN ('abandonned','processing','paused')
    AND TIMESTAMP(TrialEndUtc) <= cutoff_ts
    AND NOT (CancelAtUtc IS NOT NULL AND CancelAtUtc < TrialEndUtc)
),
r1_tbb_cte AS (
  SELECT cohort_week, {dims_only_trailing}COUNT(DISTINCT customer_email) AS r1_tbb FROM elig_users GROUP BY ALL
),
bt AS (
  SELECT DISTINCT
    DATE_TRUNC(DATE(c.CreatedAtUtc), WEEK(MONDAY)) AS cohort_week,
    ft.transaction_id, ft.customer_email, ft.membership_id, ft.invoice_r_index,
    ft.transaction_status, ft.is_refunded, ft.t_attempt_index, ft.t_datetime,
    ft.ms_billing_frequency, ft.ms_billing_period
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ft
  JOIN `eu-andy-marketing-raw.silver_sgw.stg_customers` c ON ft.customer_id = c.Id
  CROSS JOIN window_bounds wb
  WHERE DATE(c.CreatedAtUtc) BETWEEN wb.ws_min AND wb.ws_max
    AND ft.t_date BETWEEN wb.ws_min AND CURRENT_DATE()
    AND DATE_TRUNC(DATE(c.CreatedAtUtc), WEEK(MONDAY)) IN (SELECT week_start FROM weeks)
    AND ft.brand_type = '{brand_type}'
    {helpprio_ft}
    AND LOWER(ft.customer_email) NOT LIKE '%@yopmail%'
    AND LOWER(ft.customer_email) NOT LIKE '%@sharebot%'
    AND LOWER(ft.customer_firstname) NOT LIKE '%test%'
    {ft_filter}
),
btx AS (
  SELECT bt.*,
{btx_dim_pass}    1 AS _btx_marker
  FROM bt INNER JOIN elig_users e
    ON bt.cohort_week = e.cohort_week AND bt.customer_email = e.customer_email
),
succ_r1 AS (
  SELECT cohort_week, {dims_only_trailing}customer_email, membership_id, MIN(t_datetime) AS succ_dt,
    ANY_VALUE(ms_billing_frequency) AS bf, ANY_VALUE(ms_billing_period) AS bp
  FROM btx WHERE invoice_r_index='1' AND transaction_status='succeeded' GROUP BY ALL
),
succ_next_r1 AS (
  SELECT *, CASE WHEN bp='weeks' THEN TIMESTAMP_ADD(succ_dt, INTERVAL (CAST(bf AS INT64)*7) DAY)
                 WHEN bp='months' THEN TIMESTAMP_ADD(succ_dt, INTERVAL (CAST(bf AS INT64)*30) DAY)
                 ELSE TIMESTAMP_ADD(succ_dt, INTERVAL default_days DAY) END AS exp_next FROM succ_r1
),
r2_elig AS (SELECT * FROM succ_next_r1 WHERE exp_next <= cutoff_ts),
r2_tbb_raw AS (
  SELECT e.cohort_week, {dim_cols_trailing(dims, "e")}
    COUNT(DISTINCT e.customer_email) AS elig_users,
    COUNT(DISTINCT CASE WHEN sm.CancelAtUtc IS NOT NULL AND TIMESTAMP(sm.CancelAtUtc) < e.exp_next THEN e.customer_email END) AS cancel_users
  FROM r2_elig e JOIN `eu-andy-marketing-raw.silver_sgw.stg_memberships` sm
    ON CAST(e.membership_id AS STRING) = CAST(sm.Id AS STRING)
  GROUP BY ALL
),
succ_r2 AS (
  SELECT cohort_week, {dims_only_trailing}customer_email, membership_id, MIN(t_datetime) AS succ_dt,
    ANY_VALUE(ms_billing_frequency) AS bf, ANY_VALUE(ms_billing_period) AS bp
  FROM btx WHERE invoice_r_index='2' AND transaction_status='succeeded' GROUP BY ALL
),
succ_next_r2 AS (
  SELECT *, CASE WHEN bp='weeks' THEN TIMESTAMP_ADD(succ_dt, INTERVAL (CAST(bf AS INT64)*7) DAY)
                 WHEN bp='months' THEN TIMESTAMP_ADD(succ_dt, INTERVAL (CAST(bf AS INT64)*30) DAY)
                 ELSE TIMESTAMP_ADD(succ_dt, INTERVAL default_days DAY) END AS exp_next FROM succ_r2
),
r3_elig AS (SELECT * FROM succ_next_r2 WHERE exp_next <= cutoff_ts),
r3_tbb_raw AS (
  SELECT e.cohort_week, {dim_cols_trailing(dims, "e")}
    COUNT(DISTINCT e.customer_email) AS elig_users,
    COUNT(DISTINCT CASE WHEN sm.CancelAtUtc IS NOT NULL AND TIMESTAMP(sm.CancelAtUtc) < e.exp_next THEN e.customer_email END) AS cancel_users
  FROM r3_elig e JOIN `eu-andy-marketing-raw.silver_sgw.stg_memberships` sm
    ON CAST(e.membership_id AS STRING) = CAST(sm.Id AS STRING)
  GROUP BY ALL
),
succ_r3 AS (
  SELECT cohort_week, {dims_only_trailing}customer_email, membership_id, MIN(t_datetime) AS succ_dt,
    ANY_VALUE(ms_billing_frequency) AS bf, ANY_VALUE(ms_billing_period) AS bp
  FROM btx WHERE invoice_r_index='3' AND transaction_status='succeeded' GROUP BY ALL
),
succ_next_r3 AS (
  SELECT *, CASE WHEN bp='weeks' THEN TIMESTAMP_ADD(succ_dt, INTERVAL (CAST(bf AS INT64)*7) DAY)
                 WHEN bp='months' THEN TIMESTAMP_ADD(succ_dt, INTERVAL (CAST(bf AS INT64)*30) DAY)
                 ELSE TIMESTAMP_ADD(succ_dt, INTERVAL default_days DAY) END AS exp_next FROM succ_r3
),
r4_elig AS (SELECT * FROM succ_next_r3 WHERE exp_next <= cutoff_ts),
r4_tbb_raw AS (
  SELECT e.cohort_week, {dim_cols_trailing(dims, "e")}
    COUNT(DISTINCT e.customer_email) AS elig_users,
    COUNT(DISTINCT CASE WHEN sm.CancelAtUtc IS NOT NULL AND TIMESTAMP(sm.CancelAtUtc) < e.exp_next THEN e.customer_email END) AS cancel_users
  FROM r4_elig e JOIN `eu-andy-marketing-raw.silver_sgw.stg_memberships` sm
    ON CAST(e.membership_id AS STRING) = CAST(sm.Id AS STRING)
  GROUP BY ALL
),
rx_stats AS (
  SELECT cohort_week, {dims_only_trailing}invoice_r_index,
    COUNT(DISTINCT CASE WHEN t_attempt_index=1 THEN customer_email END) AS first_attempt_users,
    COUNT(DISTINCT CASE WHEN t_attempt_index=1 THEN transaction_id END) AS first_attempt_tx,
    COUNT(DISTINCT CASE WHEN t_attempt_index=1 AND transaction_status='succeeded' THEN transaction_id END) AS first_attempt_succ_tx,
    COUNT(DISTINCT transaction_id) AS total_tx,
    COUNT(DISTINCT CASE WHEN transaction_status='succeeded' THEN transaction_id END) AS succ_tx,
    COUNT(DISTINCT customer_email) AS attempted_users,
    COUNT(DISTINCT CASE WHEN transaction_status='succeeded' THEN customer_email END) AS succ_users,
    COUNT(DISTINCT CASE WHEN is_refunded=TRUE AND transaction_status='succeeded' THEN transaction_id END) AS refund_tx,
    COUNT(DISTINCT CASE WHEN is_refunded=TRUE AND transaction_status='succeeded' THEN customer_email END) AS refund_users
  FROM btx WHERE invoice_r_index IN ('1','2','3','4') GROUP BY ALL
)
SELECT
  FORMAT_DATE('%Y-%m-%d', w.week_start) AS week_start,
  w.week_label,
  {final_dim_select}rx_idx,
  COALESCE(r0.r0_attempts, 0) AS r0_attempts,
  COALESCE(r0.r0_succeeded, 0) AS r0_succeeded,
  COALESCE(r0.unsub_trial, 0) AS unsub_trial,
  COALESCE(r1.r1_tbb, 0) AS r1_tbb,
  COALESCE(s.first_attempt_users, 0) AS first_attempt_users,
  COALESCE(s.first_attempt_tx, 0) AS first_attempt_tx,
  COALESCE(s.first_attempt_succ_tx, 0) AS first_attempt_succ_tx,
  COALESCE(s.total_tx, 0) AS total_tx,
  COALESCE(s.succ_tx, 0) AS succ_tx,
  COALESCE(s.attempted_users, 0) AS attempted_users,
  COALESCE(s.succ_users, 0) AS succ_users,
  COALESCE(s.refund_tx, 0) AS refund_tx,
  COALESCE(s.refund_users, 0) AS refund_users,
  COALESCE(tbb.elig_users, 0) AS tbb_elig_users,
  COALESCE(tbb.cancel_users, 0) AS tbb_cancel_users
FROM weeks w
{dim_axis_join}CROSS JOIN UNNEST(['1','2','3','4']) AS rx_idx
LEFT JOIN r0_stats r0 ON r0.cohort_week = w.week_start{dim_on_r0}
LEFT JOIN r1_tbb_cte r1 ON r1.cohort_week = w.week_start{dim_on_r1}
LEFT JOIN rx_stats s ON s.cohort_week = w.week_start AND s.invoice_r_index = rx_idx{dim_on_s}
LEFT JOIN (
  SELECT cohort_week, {dims_only_trailing}'2' AS rx, elig_users, cancel_users FROM r2_tbb_raw
  UNION ALL SELECT cohort_week, {dims_only_trailing}'3', elig_users, cancel_users FROM r3_tbb_raw
  UNION ALL SELECT cohort_week, {dims_only_trailing}'4', elig_users, cancel_users FROM r4_tbb_raw
) tbb ON tbb.cohort_week = w.week_start AND tbb.rx = rx_idx{dim_on_tbb}
ORDER BY w.week_start, {dim_cols_trailing(dims, "da") if has_nw else ""}rx_idx
"""


def vamp_cohort_sql(filters: dict, dims: list) -> str:
    ft_filter = filter_clauses("ft", filters)
    bm_dim_sel = dim_select_clause("ft", dims)
    dims_only_trailing = dim_cols_trailing(dims)
    has_nw = bool(non_week_dims(dims))
    final_dim_select = (
        ", ".join(f"da.dim_{d[0]} AS dim_{d[0]}" for d in non_week_dims(dims)) + ",\n  "
        if has_nw
        else ""
    )
    dim_axis_join = "CROSS JOIN dim_axis da\n" if has_nw else ""
    dim_on_c = dim_join_on("c", "da", dims)
    dim_on_a = dim_join_on("a", "da", dims)
    dim_axis_cte = (
        f",\ndim_axis AS (\n  SELECT DISTINCT {dim_cols_bare(dims)} FROM tx_cat\n)"
        if has_nw
        else ""
    )

    return f"""
{WEEKS_CTE},
tx AS (
  SELECT ft.transaction_id, ft.brand_type, ft.invoice_r_index, ft.transaction_amount,
{bm_dim_sel}    DATE_TRUNC(DATE(c.CreatedAtUtc), WEEK(MONDAY)) AS cohort_week
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ft
  JOIN `eu-andy-marketing-raw.silver_sgw.stg_customers` c ON ft.customer_id = c.Id
  CROSS JOIN window_bounds wb
  WHERE DATE(c.CreatedAtUtc) BETWEEN wb.ws_min AND wb.ws_max
    AND COALESCE(ft.t_brand, '') NOT LIKE '%helpprio%'
    AND LOWER(ft.customer_email) NOT LIKE '%@yopmail%'
    AND LOWER(ft.customer_email) NOT LIKE '%@sharebot%'
    AND LOWER(ft.customer_firstname) NOT LIKE '%test%'
    AND ft.transaction_status = 'succeeded'
    {ft_filter}
),
tx_cat AS (
  SELECT *, CASE
    WHEN brand_type='Booking' AND invoice_r_index='0' AND transaction_amount <= 1 THEN 'r0_micro'
    WHEN invoice_r_index='RX_micro' AND transaction_amount = 0.01 THEN 'rx_micro'
    WHEN brand_type='Booking' AND transaction_amount > 10 THEN 'rx_booking'
    WHEN brand_type='Magazine' AND transaction_amount > 1 THEN 'rx_magazine'
    ELSE NULL END AS cat FROM tx
){dim_axis_cte},
ctx AS (
  SELECT cohort_week, {dims_only_trailing}'total' AS cat, COUNT(DISTINCT transaction_id) AS n_tx
  FROM tx_cat WHERE cohort_week IN (SELECT week_start FROM weeks) GROUP BY ALL
  UNION ALL
  SELECT cohort_week, {dims_only_trailing}cat, COUNT(DISTINCT transaction_id) FROM tx_cat
  WHERE cohort_week IN (SELECT week_start FROM weeks) AND cat IS NOT NULL GROUP BY ALL
),
al AS (SELECT fa.transaction_id FROM `eu-andy-marketing-raw.dashboard.fact_alert` fa),
cal AS (
  SELECT t.cohort_week, {dim_cols_trailing(dims, "t")}'total' AS cat, COUNT(DISTINCT a.transaction_id) AS n_al
  FROM al a JOIN tx_cat t ON a.transaction_id = t.transaction_id
  WHERE t.cohort_week IN (SELECT week_start FROM weeks) GROUP BY ALL
  UNION ALL
  SELECT t.cohort_week, {dim_cols_trailing(dims, "t")}t.cat, COUNT(DISTINCT a.transaction_id) FROM al a
  JOIN tx_cat t ON a.transaction_id = t.transaction_id
  WHERE t.cohort_week IN (SELECT week_start FROM weeks) AND t.cat IS NOT NULL GROUP BY ALL
)
SELECT FORMAT_DATE('%Y-%m-%d', w.week_start) AS week_start, w.week_label, {final_dim_select}cat,
  COALESCE(c.n_tx, 0) AS n_tx, COALESCE(a.n_al, 0) AS n_al
FROM weeks w {dim_axis_join}CROSS JOIN UNNEST(['total','rx_booking','r0_micro','rx_micro','rx_magazine']) AS cat
LEFT JOIN ctx c ON c.cohort_week = w.week_start AND c.cat = cat{dim_on_c}
LEFT JOIN cal a ON a.cohort_week = w.week_start AND a.cat = cat{dim_on_a}
ORDER BY w.week_start, {dim_cols_trailing(dims, "da") if has_nw else ""}cat
"""


def vamp_date_sql(filters: dict, dims: list) -> str:
    ft_filter = filter_clauses("ft", filters)
    # For al_tx join, replace ft. with t. since alerts join uses alias t
    ft_filter_t = ft_filter.replace("AND ft.", "AND t.").replace("OR ft.", "OR t.")
    bm_dim_sel_ft = dim_select_clause("ft", dims)
    bm_dim_sel_t = dim_select_clause("t", dims)
    dims_only_trailing = dim_cols_trailing(dims)
    has_nw = bool(non_week_dims(dims))
    final_dim_select = (
        ", ".join(f"da.dim_{d[0]} AS dim_{d[0]}" for d in non_week_dims(dims)) + ",\n  "
        if has_nw
        else ""
    )
    dim_axis_join = "CROSS JOIN dim_axis da\n" if has_nw else ""
    dim_on_t = dim_join_on("t", "da", dims)
    dim_on_a = dim_join_on("a", "da", dims)
    dim_axis_cte = (
        f",\ndim_axis AS (\n  SELECT DISTINCT {dim_cols_bare(dims)} FROM tx_cat\n)"
        if has_nw
        else ""
    )

    return f"""
{WEEKS_CTE},
tx AS (
  SELECT ft.transaction_id, ft.brand_type, ft.invoice_r_index, ft.transaction_amount,
{bm_dim_sel_ft}    DATE_TRUNC(ft.t_date, WEEK(MONDAY)) AS tx_week
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ft
  CROSS JOIN window_bounds wb
  WHERE ft.t_date BETWEEN wb.ws_min AND wb.ws_max
    AND COALESCE(ft.t_brand, '') NOT LIKE '%helpprio%'
    AND LOWER(ft.customer_email) NOT LIKE '%@yopmail%'
    AND LOWER(ft.customer_email) NOT LIKE '%@sharebot%'
    AND LOWER(ft.customer_firstname) NOT LIKE '%test%'
    AND ft.transaction_status = 'succeeded'
    {ft_filter}
),
tx_cat AS (
  SELECT *, CASE
    WHEN brand_type='Booking' AND invoice_r_index='0' AND transaction_amount <= 1 THEN 'r0_micro'
    WHEN invoice_r_index='RX_micro' AND transaction_amount = 0.01 THEN 'rx_micro'
    WHEN brand_type='Booking' AND transaction_amount > 10 THEN 'rx_booking'
    WHEN brand_type='Magazine' AND transaction_amount > 1 THEN 'rx_magazine'
    ELSE NULL END AS cat FROM tx
){dim_axis_cte},
dtx AS (
  SELECT tx_week AS wk, {dims_only_trailing}'total' AS cat, COUNT(DISTINCT transaction_id) AS n_tx FROM tx_cat GROUP BY ALL
  UNION ALL SELECT tx_week, {dims_only_trailing}cat, COUNT(DISTINCT transaction_id) FROM tx_cat WHERE cat IS NOT NULL GROUP BY ALL
),
al AS (
  SELECT fa.transaction_id, DATE_TRUNC(fa.alerted_at, WEEK(MONDAY)) AS alert_week
  FROM `eu-andy-marketing-raw.dashboard.fact_alert` fa
  WHERE DATE_TRUNC(fa.alerted_at, WEEK(MONDAY)) IN (SELECT week_start FROM weeks)
),
al_tx AS (
  SELECT a.alert_week, t.transaction_id,
{bm_dim_sel_t}    CASE
      WHEN t.brand_type='Booking' AND t.invoice_r_index='0' AND t.transaction_amount <= 1 THEN 'r0_micro'
      WHEN t.invoice_r_index='RX_micro' AND t.transaction_amount = 0.01 THEN 'rx_micro'
      WHEN t.brand_type='Booking' AND t.transaction_amount > 10 THEN 'rx_booking'
      WHEN t.brand_type='Magazine' AND t.transaction_amount > 1 THEN 'rx_magazine'
      ELSE NULL END AS cat
  FROM al a JOIN `eu-andy-marketing-raw.dashboard.fact_transactions` t ON a.transaction_id = t.transaction_id
  WHERE COALESCE(t.t_brand, '') NOT LIKE '%helpprio%'
    AND LOWER(t.customer_email) NOT LIKE '%@yopmail%'
    AND LOWER(t.customer_email) NOT LIKE '%@sharebot%'
    AND LOWER(t.customer_firstname) NOT LIKE '%test%'
    AND t.transaction_status = 'succeeded'
    {ft_filter_t}
),
dal AS (
  SELECT alert_week AS wk, {dims_only_trailing}'total' AS cat, COUNT(DISTINCT transaction_id) AS n_al FROM al_tx GROUP BY ALL
  UNION ALL SELECT alert_week, {dims_only_trailing}cat, COUNT(DISTINCT transaction_id) FROM al_tx WHERE cat IS NOT NULL GROUP BY ALL
)
SELECT FORMAT_DATE('%Y-%m-%d', w.week_start) AS week_start, w.week_label, {final_dim_select}cat,
  COALESCE(t.n_tx, 0) AS n_tx, COALESCE(a.n_al, 0) AS n_al
FROM weeks w {dim_axis_join}CROSS JOIN UNNEST(['total','rx_booking','r0_micro','rx_micro','rx_magazine']) AS cat
LEFT JOIN dtx t ON t.wk = w.week_start AND t.cat = cat{dim_on_t}
LEFT JOIN dal a ON a.wk = w.week_start AND a.cat = cat{dim_on_a}
ORDER BY w.week_start, {dim_cols_trailing(dims, "da") if has_nw else ""}cat
"""


def filter_options_sql() -> str:
    """Pull distinct values for each filter dimension from fact_memberships (80-day window)."""
    price_bucket = PRICE_EXPR.format(alias="fm")
    return f"""
WITH wb AS (
  SELECT DATE_SUB(CURRENT_DATE(), INTERVAL 80 DAY) AS ws_min, CURRENT_DATE() AS ws_max
),
fm_recent AS (
  SELECT
    COALESCE(fm.ms_default_psp,  '') AS psp,
    COALESCE(fm.brand,           '') AS brand,
    COALESCE(fm.sgw_verticale,   '') AS verticale,
    COALESCE(fm.ms_currency,     '') AS currency,
    COALESCE({price_bucket},     '') AS price
  FROM `eu-andy-marketing-raw.dashboard.fact_memberships` fm
  CROSS JOIN wb
  WHERE DATE(fm.ms_datetime) BETWEEN wb.ws_min AND wb.ws_max
    AND COALESCE(fm.brand, '') NOT LIKE '%helpprio%'
    AND LOWER(fm.customer_email) NOT LIKE '%@yopmail%'
    AND LOWER(fm.customer_email) NOT LIKE '%@sharebot%'
    AND LOWER(fm.customer_firstname) NOT LIKE '%test%'
)
SELECT dim, val, COUNT(*) AS n FROM fm_recent
UNPIVOT (val FOR dim IN (psp, brand, verticale, currency, price))
GROUP BY 1,2
ORDER BY 1, 3 DESC
"""


# ---------------------------------------------------------------------------
# Formatting + rendering
# ---------------------------------------------------------------------------
def fmt_int(n) -> str:
    if pd.isna(n):
        return "0"
    return f"{int(n):,}".replace(",", " ")


def fmt_pct(num, den, dec: int = 1) -> str:
    if den is None or den == 0 or pd.isna(den):
        return "---"
    return f"{(num / den * 100):.{dec}f} %"


def _group_keys(df: pd.DataFrame, dims: list) -> tuple:
    """Compute the display group key for each row, return (sorted_keys, key_for_row_fn).

    The key is a tuple of dim values in user order. Week dim → week_label.
    Non-week dim → dim_<key> column value (empty string → '(empty)').
    If no dims selected → single key ("Total",)."""
    if not dims:
        return (("Total",),), (lambda r: ("Total",))

    def key_for_row(r):
        parts = []
        for d in dims:
            if d[0] == "week":
                parts.append(str(r["week_label"]))
            else:
                v = r.get(f"dim_{d[0]}", "")
                parts.append(str(v) if v not in (None, "", float("nan")) else "(empty)")
                # pandas might give NaN for empty — handle below
        return tuple(parts)

    keys = set()
    for _, r in df.iterrows():
        keys.add(key_for_row(r))
    sorted_keys = sorted(keys)
    return sorted_keys, key_for_row


def _safe_str(v) -> str:
    if v is None:
        return "(empty)"
    try:
        if pd.isna(v):
            return "(empty)"
    except (TypeError, ValueError):
        pass
    s = str(v)
    return s if s != "" else "(empty)"


def build_funnel_table(df: pd.DataFrame, brand_type: str, dims: list) -> pd.DataFrame:
    """Pivot the long-format funnel df into a (KPI × dim_combo) display table."""
    # Determine grouping
    has_week = any(d[0] == "week" for d in dims) if dims else False
    non_week = non_week_dims(dims) if dims else []

    def gk(r):
        if not dims:
            return ("Total",)
        parts = []
        for d in dims:
            if d[0] == "week":
                parts.append(str(r["week_label"]))
            else:
                parts.append(_safe_str(r.get(f"dim_{d[0]}", "")))
        return tuple(parts)

    by_group = {}
    # Track which (cohort_week × non_week dim combo) tuples we've already counted
    # so cohort-level metrics aren't multiplied by rx_idx repetition.
    cohort_seen = {}

    for _, r in df.iterrows():
        g = gk(r)
        if g not in by_group:
            by_group[g] = {
                "r0_attempts": 0, "r0_succeeded": 0, "unsub_trial": 0, "r1_tbb": 0,
                "rx": {},
            }
            cohort_seen[g] = set()

        # Cohort-level metrics — count once per (week, non_week_dim_combo) inside the group
        cohort_id = (r["week_label"],) + tuple(_safe_str(r.get(f"dim_{d[0]}", "")) for d in non_week)
        if cohort_id not in cohort_seen[g]:
            cohort_seen[g].add(cohort_id)
            by_group[g]["r0_attempts"] += int(r["r0_attempts"])
            by_group[g]["r0_succeeded"] += int(r["r0_succeeded"])
            by_group[g]["unsub_trial"] += int(r["unsub_trial"])
            by_group[g]["r1_tbb"] += int(r["r1_tbb"])

        # Per-rx metrics — aggregate by summing
        rx = str(r["rx_idx"])
        rx_acc = by_group[g]["rx"].setdefault(rx, {
            "fa_u": 0, "fa_tx": 0, "fa_succ_tx": 0, "total_tx": 0, "succ_tx": 0,
            "att_u": 0, "succ_u": 0, "refund_tx": 0, "refund_u": 0,
            "elig_u": 0, "cancel_u": 0,
        })
        rx_acc["fa_u"] += int(r["first_attempt_users"])
        rx_acc["fa_tx"] += int(r["first_attempt_tx"])
        rx_acc["fa_succ_tx"] += int(r["first_attempt_succ_tx"])
        rx_acc["total_tx"] += int(r["total_tx"])
        rx_acc["succ_tx"] += int(r["succ_tx"])
        rx_acc["att_u"] += int(r["attempted_users"])
        rx_acc["succ_u"] += int(r["succ_users"])
        rx_acc["refund_tx"] += int(r["refund_tx"])
        rx_acc["refund_u"] += int(r["refund_users"])
        rx_acc["elig_u"] += int(r["tbb_elig_users"])
        rx_acc["cancel_u"] += int(r["tbb_cancel_users"])
        rx_acc["tbb"] = max(rx_acc["elig_u"] - rx_acc["cancel_u"], 0)

    groups = sorted(by_group.keys())
    rows = []

    def push(label: str, vals: Iterable, *, section: bool = False) -> None:
        rows.append({"__key__": ("__SECTION__" + label) if section else label,
                     **{g: v for g, v in zip(groups, vals)}})

    def g0(g, k):
        return by_group[g][k]

    def gr(g, rx, k):
        return by_group[g]["rx"].get(rx, {}).get(k, 0)

    # R0 / Trial
    push("R0 / Trial", ["" for _ in groups], section=True)
    push("# R0 Attempts", [fmt_int(g0(g, "r0_attempts")) for g in groups])
    push("% Success Rate R0", [fmt_pct(g0(g, "r0_succeeded"), g0(g, "r0_attempts")) for g in groups])
    push("# R0 Succeeded", [fmt_int(g0(g, "r0_succeeded")) for g in groups])
    push("% Unsub During Trial", [fmt_pct(g0(g, "unsub_trial"), g0(g, "r0_succeeded")) for g in groups])
    push("# R1 To Be Billed", [fmt_int(g0(g, "r1_tbb")) for g in groups])
    push("% R1 Billed", [fmt_pct(gr(g, "1", "fa_u"), g0(g, "r1_tbb")) for g in groups])

    # R1
    push("R1", ["" for _ in groups], section=True)
    push("# R1 First Attempt (users)", [fmt_int(gr(g, "1", "fa_u")) for g in groups])
    push("% Success R1 First Attempt", [fmt_pct(gr(g, "1", "fa_succ_tx"), gr(g, "1", "fa_tx")) for g in groups])
    push("# R1 Attempts (tx dedup)", [fmt_int(gr(g, "1", "total_tx")) for g in groups])
    push("% Success R1 Attempts", [fmt_pct(gr(g, "1", "succ_tx"), gr(g, "1", "total_tx")) for g in groups])
    push("# R1 Succeeded (users)", [fmt_int(gr(g, "1", "succ_u")) for g in groups])
    push("% R1 Succeeded per User", [fmt_pct(gr(g, "1", "succ_u"), gr(g, "1", "att_u")) for g in groups])
    push("% Churn Brut R0/R1",
         [fmt_pct(g0(g, "r0_succeeded") - gr(g, "1", "succ_u"), g0(g, "r0_succeeded")) for g in groups])
    push("# Refund R1", [fmt_int(gr(g, "1", "refund_tx")) for g in groups])
    push("% Refund R1", [fmt_pct(gr(g, "1", "refund_tx"), gr(g, "1", "succ_tx")) for g in groups])
    push("% Churn Net R0/R1",
         [fmt_pct(g0(g, "r0_succeeded") - (gr(g, "1", "succ_u") - gr(g, "1", "refund_u")),
                  g0(g, "r0_succeeded")) for g in groups])

    # R2 / R3 / R4 — show only if total eligible (across all groups) >= 10
    for rx in ["2", "3", "4"]:
        total_elig = sum(gr(g, rx, "elig_u") for g in groups)
        if total_elig < 10:
            continue
        prev = str(int(rx) - 1)
        lbl = f"R{rx}"

        def val_or(g, fn, _rx=rx):
            return fn() if gr(g, _rx, "elig_u") > 0 else "---"

        push(lbl, ["" for _ in groups], section=True)
        push(f"# {lbl} TBB", [val_or(g, lambda g=g: fmt_int(gr(g, rx, "tbb"))) for g in groups])
        push(f"% {lbl} Billed", [val_or(g, lambda g=g: fmt_pct(gr(g, rx, "fa_u"), gr(g, rx, "tbb"))) for g in groups])
        push(f"# {lbl} First Attempt (users)", [val_or(g, lambda g=g: fmt_int(gr(g, rx, "fa_u"))) for g in groups])
        push(f"% Success {lbl} First Attempt", [val_or(g, lambda g=g: fmt_pct(gr(g, rx, "fa_succ_tx"), gr(g, rx, "fa_tx"))) for g in groups])
        push(f"# {lbl} Attempts (tx dedup)", [val_or(g, lambda g=g: fmt_int(gr(g, rx, "total_tx"))) for g in groups])
        push(f"% Success {lbl} Attempts", [val_or(g, lambda g=g: fmt_pct(gr(g, rx, "succ_tx"), gr(g, rx, "total_tx"))) for g in groups])
        push(f"# {lbl} Succeeded (users)", [val_or(g, lambda g=g: fmt_int(gr(g, rx, "succ_u"))) for g in groups])
        push(f"% {lbl} Succeeded per User", [val_or(g, lambda g=g: fmt_pct(gr(g, rx, "succ_u"), gr(g, rx, "att_u"))) for g in groups])
        push(f"% Churn Brut R{prev}/{lbl}",
             [val_or(g, lambda g=g: fmt_pct(gr(g, prev, "succ_u") - gr(g, rx, "succ_u"), gr(g, prev, "succ_u"))) for g in groups])
        push(f"# Refund {lbl}", [val_or(g, lambda g=g: fmt_int(gr(g, rx, "refund_tx"))) for g in groups])
        push(f"% Refund {lbl}", [val_or(g, lambda g=g: fmt_pct(gr(g, rx, "refund_tx"), gr(g, rx, "succ_tx"))) for g in groups])
        push(f"% Churn Net R{prev}/{lbl}",
             [val_or(g, lambda g=g: fmt_pct(
                 gr(g, prev, "succ_u") - (gr(g, rx, "succ_u") - gr(g, rx, "refund_u")),
                 gr(g, prev, "succ_u"))) for g in groups])

    out = pd.DataFrame(rows)
    out.attrs["dims"] = dims
    out.attrs["groups"] = groups
    return out


def build_vamp_table(df: pd.DataFrame, dims: list) -> pd.DataFrame:
    non_week = non_week_dims(dims) if dims else []

    def gk(r):
        if not dims:
            return ("Total",)
        parts = []
        for d in dims:
            if d[0] == "week":
                parts.append(str(r["week_label"]))
            else:
                parts.append(_safe_str(r.get(f"dim_{d[0]}", "")))
        return tuple(parts)

    by_group = {}
    for _, r in df.iterrows():
        g = gk(r)
        bg = by_group.setdefault(g, {})
        cat = r["cat"]
        existing = bg.get(cat, {"n_tx": 0, "n_al": 0})
        existing["n_tx"] += int(r["n_tx"])
        existing["n_al"] += int(r["n_al"])
        bg[cat] = existing

    groups = sorted(by_group.keys())
    rows = []

    def push(label: str, vals: Iterable, *, section: bool = False) -> None:
        rows.append({"__key__": ("__SECTION__" + label) if section else label,
                     **{g: v for g, v in zip(groups, vals)}})

    for name, key in [
        ("Total", "total"),
        ("Rx Booking", "rx_booking"),
        ("R0 Micro", "r0_micro"),
        ("Rx Micro", "rx_micro"),
        ("Rx Magazine", "rx_magazine"),
    ]:
        push(name, ["" for _ in groups], section=True)
        push(f"# Tx Succeeded ({name})",
             [fmt_int(by_group.get(g, {}).get(key, {}).get("n_tx", 0)) for g in groups])
        push(f"# Alertes ({name})",
             [fmt_int(by_group.get(g, {}).get(key, {}).get("n_al", 0)) for g in groups])
        push(f"% VAMP ({name})",
             [fmt_pct(
                 by_group.get(g, {}).get(key, {}).get("n_al", 0),
                 by_group.get(g, {}).get(key, {}).get("n_tx", 0),
                 dec=2,
             ) for g in groups])

    out = pd.DataFrame(rows)
    out.attrs["dims"] = dims
    out.attrs["groups"] = groups
    return out


def style_table(df: pd.DataFrame):
    """Convert section markers to bold rows. Apply multi-level column headers if multiple dims."""
    if df.empty:
        return df

    dims = df.attrs.get("dims", [])
    groups = df.attrs.get("groups", [])

    display = df.copy()
    # First column is __key__ (KPI/section). Rename to "KPI" for display.
    is_section = display["__key__"].astype(str).str.startswith("__SECTION__")
    display["__key__"] = display["__key__"].astype(str).str.replace("__SECTION__", "", regex=False)

    # Rename columns: tuple groups → flat strings (or MultiIndex for >1 dim)
    if not dims or len(dims) == 0:
        # Single "Total" column
        rename = {("Total",): "Total"}
        display.rename(columns=rename, inplace=True)
        display.rename(columns={"__key__": "KPI"}, inplace=True)
        column_subset = [c for c in display.columns if c != "KPI"]
        styler = display.style.set_properties(**{"text-align": "right"}, subset=column_subset)
        styler = styler.set_properties(**{"text-align": "left", "font-weight": "500"}, subset=["KPI"])
        styler = styler.apply(
            lambda s: ["background-color: #e2e8f0; font-weight: 600" if v else "" for v in is_section],
            axis=0,
        )
        return styler.hide(axis="index")

    if len(dims) == 1:
        # Single-dim columns — flatten tuples
        flat = {g: g[0] for g in groups}
        display.rename(columns=flat, inplace=True)
        display.rename(columns={"__key__": "KPI"}, inplace=True)
        col_order = ["KPI"] + [flat[g] for g in groups]
        display = display[col_order]
        column_subset = [c for c in display.columns if c != "KPI"]
        styler = display.style.set_properties(**{"text-align": "right"}, subset=column_subset)
        styler = styler.set_properties(**{"text-align": "left", "font-weight": "500"}, subset=["KPI"])
        styler = styler.apply(
            lambda s: ["background-color: #e2e8f0; font-weight: 600" if v else "" for v in is_section],
            axis=0,
        )
        return styler.hide(axis="index")

    # Multi-dim: 2 levels (we cap at 2). Build MultiIndex columns.
    # KPI column → ("KPI", "") at all levels.
    levels = [d[1] for d in dims]
    new_cols = [("KPI",) + ("",) * (len(dims) - 1)]
    new_cols.extend(tuple(g) for g in groups)
    display.rename(columns={g: g for g in groups}, inplace=True)
    display.rename(columns={"__key__": ("KPI",) + ("",) * (len(dims) - 1)}, inplace=True)
    # Reorder: KPI first, then groups
    display = display[[("KPI",) + ("",) * (len(dims) - 1)] + list(groups)]
    display.columns = pd.MultiIndex.from_tuples(new_cols, names=levels)

    kpi_col = ("KPI",) + ("",) * (len(dims) - 1)
    column_subset = [c for c in display.columns if c != kpi_col]
    styler = display.style.set_properties(**{"text-align": "right"}, subset=column_subset)
    styler = styler.set_properties(**{"text-align": "left", "font-weight": "500"}, subset=[kpi_col])
    styler = styler.apply(
        lambda s: ["background-color: #e2e8f0; font-weight: 600" if v else "" for v in is_section],
        axis=0,
    )
    return styler.hide(axis="index")


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("📊 Weekly PSP Report")
st.caption("Funnel + VAMP — basé sur le skill `weekly-psp-report` (v3) — dimensions dynamiques.")

# Sidebar: dimensions (column-axis splits)
st.sidebar.header("Dimensions")
all_dim_labels = [d[1] for d in DIMENSION_DIMS]
selected_dim_labels = st.sidebar.multiselect(
    "Splitter les colonnes par (ordre = imbrication, max 2)",
    options=all_dim_labels,
    default=["Semaine"],
    max_selections=2,
    help="1ʳᵉ dimension = niveau extérieur, 2ᵉ = niveau intérieur. Tu peux retirer 'Semaine' pour agréger les 10 semaines.",
    key="dim_selector",
)
dims = selected_dims(selected_dim_labels)

st.sidebar.divider()

# Sidebar: filters
st.sidebar.header("Filtres")
with st.spinner("Chargement des options de filtre…"):
    try:
        opts_df = run_query(filter_options_sql())
    except Exception as e:
        st.sidebar.error(f"Échec chargement filtres : {e}")
        opts_df = pd.DataFrame(columns=["dim", "val", "n"])

filters: dict = {}
for key, label, _fm, _ft in FILTER_DIMS:
    dim_opts = opts_df[opts_df["dim"] == key].sort_values("n", ascending=False)
    choices = [v if v != "" else "(empty)" for v in dim_opts["val"].tolist()]
    counts = {v if v != "" else "(empty)": int(n) for v, n in zip(dim_opts["val"], dim_opts["n"])}
    selected = st.sidebar.multiselect(
        label,
        options=choices,
        default=[],
        format_func=lambda v, _c=counts: f"{v} ({_c.get(v, 0):,})".replace(",", " "),
        key=f"filter_{key}",
    )
    if selected:
        filters[key] = selected

# Cache controls
st.sidebar.divider()
col_a, col_b = st.sidebar.columns(2)
if col_a.button("🔄 Rafraîchir", use_container_width=True, help="Vide le cache et recharge"):
    st.cache_data.clear()
    st.rerun()
col_b.caption(f"TTL cache: 24h")

# Header / meta line
active_filters = ", ".join(f"{label} ({len(filters[key])})" for key, label, _, _ in FILTER_DIMS if key in filters)
active_dims = " × ".join(d[1] for d in dims) if dims else "Aucune (Total)"
now_fr = datetime.now(timezone.utc).astimezone().strftime("%-d %B %Y, %H:%M")
header_html = f"""
<div class="meta-box">
  <b>Refresh:</b> {now_fr}
  · <b>Fenêtre:</b> 10 dernières semaines complètes
  · <b>Dimensions:</b> {active_dims}
  {(' · <b>Filtres actifs:</b> ' + active_filters) if active_filters else ''}
</div>
"""
st.markdown(header_html, unsafe_allow_html=True)

# Tabs
tab_b, tab_m, tab_vc, tab_vd = st.tabs(["Funnel Booking", "Funnel Magazine", "VAMP Cohort", "VAMP Date"])

with tab_b:
    with st.spinner("Funnel Booking…"):
        try:
            df = run_query(funnel_sql("Booking", filters, dims))
            table = build_funnel_table(df, "Booking", dims)
            st.dataframe(style_table(table), use_container_width=True, height=min(40 + 28 * len(table), 1100))
        except Exception as e:
            st.error(f"Erreur Funnel Booking : {e}")

with tab_m:
    with st.spinner("Funnel Magazine…"):
        try:
            df = run_query(funnel_sql("Magazine", filters, dims))
            table = build_funnel_table(df, "Magazine", dims)
            st.dataframe(style_table(table), use_container_width=True, height=min(40 + 28 * len(table), 1100))
        except Exception as e:
            st.error(f"Erreur Funnel Magazine : {e}")

with tab_vc:
    with st.spinner("VAMP Cohort…"):
        try:
            df = run_query(vamp_cohort_sql(filters, dims))
            table = build_vamp_table(df, dims)
            st.dataframe(style_table(table), use_container_width=True, height=40 + 28 * len(table))
        except Exception as e:
            st.error(f"Erreur VAMP Cohort : {e}")

with tab_vd:
    with st.spinner("VAMP Date…"):
        try:
            df = run_query(vamp_date_sql(filters, dims))
            table = build_vamp_table(df, dims)
            st.dataframe(style_table(table), use_container_width=True, height=40 + 28 * len(table))
        except Exception as e:
            st.error(f"Erreur VAMP Date : {e}")
