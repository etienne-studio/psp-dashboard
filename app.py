"""Weekly PSP Dashboard — Streamlit app.

Mirrors the weekly-psp-report skill (v3) with interactive filters.
Filters apply server-side via WHERE clauses; results cached 24h via @st.cache_data.

Run locally:
    gcloud auth application-default login
    pip install -r requirements.txt
    streamlit run app.py

Deploy to Cloud Run: see README.md.
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


# ---------------------------------------------------------------------------
# Password gate (uses st.secrets["app_password"])
# ---------------------------------------------------------------------------
def _require_password() -> None:
    if st.session_state.get("_auth_ok"):
        return
    expected = st.secrets.get("app_password", "")
    if not expected:
        return  # no password configured
    st.title("🔒 Accès protégé")
    st.caption("Entre le mot de passe pour accéder au dashboard.")
    pw = st.text_input("Mot de passe", type="password", key="_auth_input", label_visibility="collapsed", placeholder="Mot de passe")
    if pw:
        if pw == expected:
            st.session_state["_auth_ok"] = True
            st.rerun()
        else:
            st.error("Mot de passe incorrect.")
    st.stop()


_require_password()


# Light visual polish — table sticky header / numeric alignment
st.markdown(
    """
    <style>
      .block-container { padding-top: 1.2rem; padding-bottom: 2rem; max-width: 1400px; }
      [data-testid="stDataFrame"] td { font-variant-numeric: tabular-nums; }
      .meta-box { padding: 10px 14px; background: #f1f5f9; border-left: 4px solid #6366f1;
                  border-radius: 4px; font-size: 13px; color: #475569; margin-bottom: 12px; }
      .meta-box b { color: #0f172a; }
    </style>
    """,
    unsafe_allow_html=True,
)


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
# SQL helpers
# ---------------------------------------------------------------------------
FILTER_DIMS = [
    # (filter key, label, fm column, ft column)
    ("psp", "PSP", "ms_default_psp", "ms_default_psp"),
    ("brand", "Brand", "brand", "t_brand"),
    ("verticale", "Verticale", "sgw_verticale", "sgw_verticale"),
    ("currency", "Currency", "ms_currency", "ms_currency"),
    ("price", "Price", "price_name", "price_name"),
]

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
    scope is 'fm' (memberships) or 'ft' (transactions)."""
    lines = []
    for key, _label, fm_col, ft_col in FILTER_DIMS:
        vals = filters.get(key, [])
        if not vals:
            continue
        col = fm_col if scope == "fm" else ft_col
        rendered = ", ".join(f"'{sql_escape(v)}'" if v != "(empty)" else "''" for v in vals)
        has_empty = "(empty)" in vals
        if has_empty:
            lines.append(f"AND ({scope}.{col} IN ({rendered}) OR {scope}.{col} IS NULL)")
        else:
            lines.append(f"AND {scope}.{col} IN ({rendered})")
    return "\n    ".join(lines)


# ---------------------------------------------------------------------------
# Funnel query (one per brand_type)
# ---------------------------------------------------------------------------
def funnel_sql(brand_type: str, filters: dict) -> str:
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

    return f"""
DECLARE cutoff_ts TIMESTAMP DEFAULT TIMESTAMP(CURRENT_DATE());
DECLARE default_days INT64 DEFAULT {default_days};
{WEEKS_CTE},
bm AS (
  SELECT
    DATE_TRUNC(DATE(c.CreatedAtUtc), WEEK(MONDAY)) AS cohort_week,
    fm.customer_email, fm.ms_status,
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
),
r0_stats AS (
  SELECT cohort_week,
    COUNT(DISTINCT customer_email) AS r0_attempts,
    COUNT(DISTINCT CASE WHEN ms_status NOT IN ('abandonned','processing','paused') THEN customer_email END) AS r0_succeeded,
    COUNT(DISTINCT CASE WHEN ms_status NOT IN ('abandonned','processing','paused')
        AND CancelAtUtc IS NOT NULL AND CancelAtUtc < TrialEndUtc THEN customer_email END) AS unsub_trial
  FROM bm GROUP BY 1
),
elig_users AS (
  SELECT DISTINCT cohort_week, customer_email FROM bm
  WHERE ms_status NOT IN ('abandonned','processing','paused')
    AND TIMESTAMP(TrialEndUtc) <= cutoff_ts
    AND NOT (CancelAtUtc IS NOT NULL AND CancelAtUtc < TrialEndUtc)
),
r1_tbb_cte AS (
  SELECT cohort_week, COUNT(DISTINCT customer_email) AS r1_tbb FROM elig_users GROUP BY 1
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
  SELECT bt.* FROM bt INNER JOIN elig_users e
    ON bt.cohort_week = e.cohort_week AND bt.customer_email = e.customer_email
),
succ_r1 AS (
  SELECT cohort_week, customer_email, membership_id, MIN(t_datetime) AS succ_dt,
    ANY_VALUE(ms_billing_frequency) AS bf, ANY_VALUE(ms_billing_period) AS bp
  FROM btx WHERE invoice_r_index='1' AND transaction_status='succeeded' GROUP BY 1,2,3
),
succ_next_r1 AS (
  SELECT *, CASE WHEN bp='weeks' THEN TIMESTAMP_ADD(succ_dt, INTERVAL (CAST(bf AS INT64)*7) DAY)
                 WHEN bp='months' THEN TIMESTAMP_ADD(succ_dt, INTERVAL (CAST(bf AS INT64)*30) DAY)
                 ELSE TIMESTAMP_ADD(succ_dt, INTERVAL default_days DAY) END AS exp_next FROM succ_r1
),
r2_elig AS (SELECT * FROM succ_next_r1 WHERE exp_next <= cutoff_ts),
r2_tbb_raw AS (
  SELECT e.cohort_week,
    COUNT(DISTINCT e.customer_email) AS elig_users,
    COUNT(DISTINCT CASE WHEN sm.CancelAtUtc IS NOT NULL AND TIMESTAMP(sm.CancelAtUtc) < e.exp_next THEN e.customer_email END) AS cancel_users
  FROM r2_elig e JOIN `eu-andy-marketing-raw.silver_sgw.stg_memberships` sm
    ON CAST(e.membership_id AS STRING) = CAST(sm.Id AS STRING)
  GROUP BY 1
),
succ_r2 AS (
  SELECT cohort_week, customer_email, membership_id, MIN(t_datetime) AS succ_dt,
    ANY_VALUE(ms_billing_frequency) AS bf, ANY_VALUE(ms_billing_period) AS bp
  FROM btx WHERE invoice_r_index='2' AND transaction_status='succeeded' GROUP BY 1,2,3
),
succ_next_r2 AS (
  SELECT *, CASE WHEN bp='weeks' THEN TIMESTAMP_ADD(succ_dt, INTERVAL (CAST(bf AS INT64)*7) DAY)
                 WHEN bp='months' THEN TIMESTAMP_ADD(succ_dt, INTERVAL (CAST(bf AS INT64)*30) DAY)
                 ELSE TIMESTAMP_ADD(succ_dt, INTERVAL default_days DAY) END AS exp_next FROM succ_r2
),
r3_elig AS (SELECT * FROM succ_next_r2 WHERE exp_next <= cutoff_ts),
r3_tbb_raw AS (
  SELECT e.cohort_week,
    COUNT(DISTINCT e.customer_email) AS elig_users,
    COUNT(DISTINCT CASE WHEN sm.CancelAtUtc IS NOT NULL AND TIMESTAMP(sm.CancelAtUtc) < e.exp_next THEN e.customer_email END) AS cancel_users
  FROM r3_elig e JOIN `eu-andy-marketing-raw.silver_sgw.stg_memberships` sm
    ON CAST(e.membership_id AS STRING) = CAST(sm.Id AS STRING)
  GROUP BY 1
),
succ_r3 AS (
  SELECT cohort_week, customer_email, membership_id, MIN(t_datetime) AS succ_dt,
    ANY_VALUE(ms_billing_frequency) AS bf, ANY_VALUE(ms_billing_period) AS bp
  FROM btx WHERE invoice_r_index='3' AND transaction_status='succeeded' GROUP BY 1,2,3
),
succ_next_r3 AS (
  SELECT *, CASE WHEN bp='weeks' THEN TIMESTAMP_ADD(succ_dt, INTERVAL (CAST(bf AS INT64)*7) DAY)
                 WHEN bp='months' THEN TIMESTAMP_ADD(succ_dt, INTERVAL (CAST(bf AS INT64)*30) DAY)
                 ELSE TIMESTAMP_ADD(succ_dt, INTERVAL default_days DAY) END AS exp_next FROM succ_r3
),
r4_elig AS (SELECT * FROM succ_next_r3 WHERE exp_next <= cutoff_ts),
r4_tbb_raw AS (
  SELECT e.cohort_week,
    COUNT(DISTINCT e.customer_email) AS elig_users,
    COUNT(DISTINCT CASE WHEN sm.CancelAtUtc IS NOT NULL AND TIMESTAMP(sm.CancelAtUtc) < e.exp_next THEN e.customer_email END) AS cancel_users
  FROM r4_elig e JOIN `eu-andy-marketing-raw.silver_sgw.stg_memberships` sm
    ON CAST(e.membership_id AS STRING) = CAST(sm.Id AS STRING)
  GROUP BY 1
),
rx_stats AS (
  SELECT cohort_week, invoice_r_index,
    COUNT(DISTINCT CASE WHEN t_attempt_index=1 THEN customer_email END) AS first_attempt_users,
    COUNT(DISTINCT CASE WHEN t_attempt_index=1 THEN transaction_id END) AS first_attempt_tx,
    COUNT(DISTINCT CASE WHEN t_attempt_index=1 AND transaction_status='succeeded' THEN transaction_id END) AS first_attempt_succ_tx,
    COUNT(DISTINCT transaction_id) AS total_tx,
    COUNT(DISTINCT CASE WHEN transaction_status='succeeded' THEN transaction_id END) AS succ_tx,
    COUNT(DISTINCT customer_email) AS attempted_users,
    COUNT(DISTINCT CASE WHEN transaction_status='succeeded' THEN customer_email END) AS succ_users,
    COUNT(DISTINCT CASE WHEN is_refunded=TRUE AND transaction_status='succeeded' THEN transaction_id END) AS refund_tx,
    COUNT(DISTINCT CASE WHEN is_refunded=TRUE AND transaction_status='succeeded' THEN customer_email END) AS refund_users
  FROM btx WHERE invoice_r_index IN ('1','2','3','4') GROUP BY 1,2
)
SELECT
  FORMAT_DATE('%Y-%m-%d', w.week_start) AS week_start,
  w.week_label,
  rx_idx,
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
CROSS JOIN UNNEST(['1','2','3','4']) AS rx_idx
LEFT JOIN r0_stats r0 ON r0.cohort_week = w.week_start
LEFT JOIN r1_tbb_cte r1 ON r1.cohort_week = w.week_start
LEFT JOIN rx_stats s ON s.cohort_week = w.week_start AND s.invoice_r_index = rx_idx
LEFT JOIN (
  SELECT cohort_week, '2' AS rx, elig_users, cancel_users FROM r2_tbb_raw
  UNION ALL SELECT cohort_week, '3', elig_users, cancel_users FROM r3_tbb_raw
  UNION ALL SELECT cohort_week, '4', elig_users, cancel_users FROM r4_tbb_raw
) tbb ON tbb.cohort_week = w.week_start AND tbb.rx = rx_idx
ORDER BY w.week_start, rx_idx
"""


def vamp_cohort_sql(filters: dict) -> str:
    ft_filter = filter_clauses("ft", filters)
    return f"""
{WEEKS_CTE},
tx AS (
  SELECT ft.transaction_id, ft.brand_type, ft.invoice_r_index, ft.transaction_amount,
    DATE_TRUNC(DATE(c.CreatedAtUtc), WEEK(MONDAY)) AS cohort_week
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
),
ctx AS (
  SELECT cohort_week, 'total' AS cat, COUNT(DISTINCT transaction_id) AS n_tx
  FROM tx_cat WHERE cohort_week IN (SELECT week_start FROM weeks) GROUP BY 1
  UNION ALL
  SELECT cohort_week, cat, COUNT(DISTINCT transaction_id) FROM tx_cat
  WHERE cohort_week IN (SELECT week_start FROM weeks) AND cat IS NOT NULL GROUP BY 1,2
),
al AS (SELECT fa.transaction_id FROM `eu-andy-marketing-raw.dashboard.fact_alert` fa),
cal AS (
  SELECT t.cohort_week, 'total' AS cat, COUNT(DISTINCT a.transaction_id) AS n_al
  FROM al a JOIN tx_cat t ON a.transaction_id = t.transaction_id
  WHERE t.cohort_week IN (SELECT week_start FROM weeks) GROUP BY 1
  UNION ALL
  SELECT t.cohort_week, t.cat, COUNT(DISTINCT a.transaction_id) FROM al a
  JOIN tx_cat t ON a.transaction_id = t.transaction_id
  WHERE t.cohort_week IN (SELECT week_start FROM weeks) AND t.cat IS NOT NULL GROUP BY 1,2
)
SELECT FORMAT_DATE('%Y-%m-%d', w.week_start) AS week_start, w.week_label, cat,
  COALESCE(c.n_tx, 0) AS n_tx, COALESCE(a.n_al, 0) AS n_al
FROM weeks w CROSS JOIN UNNEST(['total','rx_booking','r0_micro','rx_micro','rx_magazine']) AS cat
LEFT JOIN ctx c ON c.cohort_week = w.week_start AND c.cat = cat
LEFT JOIN cal a ON a.cohort_week = w.week_start AND a.cat = cat
ORDER BY w.week_start, cat
"""


def vamp_date_sql(filters: dict) -> str:
    ft_filter = filter_clauses("ft", filters)
    # For al_tx join, replace ft. with t. since alerts join uses alias t
    ft_filter_t = ft_filter.replace("AND ft.", "AND t.").replace("OR ft.", "OR t.")
    return f"""
{WEEKS_CTE},
tx AS (
  SELECT ft.transaction_id, ft.brand_type, ft.invoice_r_index, ft.transaction_amount,
    DATE_TRUNC(ft.t_date, WEEK(MONDAY)) AS tx_week
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
),
dtx AS (
  SELECT tx_week AS wk, 'total' AS cat, COUNT(DISTINCT transaction_id) AS n_tx FROM tx_cat GROUP BY 1
  UNION ALL SELECT tx_week, cat, COUNT(DISTINCT transaction_id) FROM tx_cat WHERE cat IS NOT NULL GROUP BY 1,2
),
al AS (
  SELECT fa.transaction_id, DATE_TRUNC(fa.alerted_at, WEEK(MONDAY)) AS alert_week
  FROM `eu-andy-marketing-raw.dashboard.fact_alert` fa
  WHERE DATE_TRUNC(fa.alerted_at, WEEK(MONDAY)) IN (SELECT week_start FROM weeks)
),
al_tx AS (
  SELECT a.alert_week, t.transaction_id,
    CASE
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
  SELECT alert_week AS wk, 'total' AS cat, COUNT(DISTINCT transaction_id) AS n_al FROM al_tx GROUP BY 1
  UNION ALL SELECT alert_week, cat, COUNT(DISTINCT transaction_id) FROM al_tx WHERE cat IS NOT NULL GROUP BY 1,2
)
SELECT FORMAT_DATE('%Y-%m-%d', w.week_start) AS week_start, w.week_label, cat,
  COALESCE(t.n_tx, 0) AS n_tx, COALESCE(a.n_al, 0) AS n_al
FROM weeks w CROSS JOIN UNNEST(['total','rx_booking','r0_micro','rx_micro','rx_magazine']) AS cat
LEFT JOIN dtx t ON t.wk = w.week_start AND t.cat = cat
LEFT JOIN dal a ON a.wk = w.week_start AND a.cat = cat
ORDER BY w.week_start, cat
"""


def filter_options_sql() -> str:
    """Pull distinct values for each filter dimension from fact_memberships (80-day window)."""
    return """
WITH wb AS (
  SELECT DATE_SUB(CURRENT_DATE(), INTERVAL 80 DAY) AS ws_min, CURRENT_DATE() AS ws_max
),
fm_recent AS (
  SELECT
    COALESCE(ms_default_psp,  '') AS psp,
    COALESCE(brand,           '') AS brand,
    COALESCE(sgw_verticale,   '') AS verticale,
    COALESCE(ms_currency,     '') AS currency,
    COALESCE(price_name,      '') AS price
  FROM `eu-andy-marketing-raw.dashboard.fact_memberships` fm
  CROSS JOIN wb
  WHERE DATE(ms_datetime) BETWEEN wb.ws_min AND wb.ws_max
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


def build_funnel_table(df: pd.DataFrame, brand_type: str) -> pd.DataFrame:
    """Pivot the long-format funnel df (one row per week × rx) into a (KPI × week) display table."""
    weeks = list(df["week_label"].drop_duplicates())
    by_wk = {}
    for _, r in df.iterrows():
        wk = r["week_label"]
        if wk not in by_wk:
            by_wk[wk] = {
                "r0_attempts": r["r0_attempts"],
                "r0_succeeded": r["r0_succeeded"],
                "unsub_trial": r["unsub_trial"],
                "r1_tbb": r["r1_tbb"],
                "rx": {},
            }
        by_wk[wk]["rx"][str(r["rx_idx"])] = {
            "fa_u": r["first_attempt_users"],
            "fa_tx": r["first_attempt_tx"],
            "fa_succ_tx": r["first_attempt_succ_tx"],
            "total_tx": r["total_tx"],
            "succ_tx": r["succ_tx"],
            "att_u": r["attempted_users"],
            "succ_u": r["succ_users"],
            "refund_tx": r["refund_tx"],
            "refund_u": r["refund_users"],
            "elig_u": r["tbb_elig_users"],
            "cancel_u": r["tbb_cancel_users"],
            "tbb": max(int(r["tbb_elig_users"]) - int(r["tbb_cancel_users"]), 0),
        }

    rows = []

    def push(label: str, vals: Iterable, *, section: bool = False) -> None:
        rows.append({"KPI": ("__SECTION__" + label) if section else label, **{w: v for w, v in zip(weeks, vals)}})

    # R0 / Trial
    push("R0 / Trial", ["" for _ in weeks], section=True)
    push("# R0 Attempts", [fmt_int(by_wk[w]["r0_attempts"]) for w in weeks])
    push("% Success Rate R0", [fmt_pct(by_wk[w]["r0_succeeded"], by_wk[w]["r0_attempts"]) for w in weeks])
    push("# R0 Succeeded", [fmt_int(by_wk[w]["r0_succeeded"]) for w in weeks])
    push("% Unsub During Trial", [fmt_pct(by_wk[w]["unsub_trial"], by_wk[w]["r0_succeeded"]) for w in weeks])
    push("# R1 To Be Billed", [fmt_int(by_wk[w]["r1_tbb"]) for w in weeks])
    push("% R1 Billed", [fmt_pct(by_wk[w]["rx"].get("1", {}).get("fa_u", 0), by_wk[w]["r1_tbb"]) for w in weeks])

    # R1
    push("R1", ["" for _ in weeks], section=True)
    g1 = lambda w, k: by_wk[w]["rx"].get("1", {}).get(k, 0)
    push("# R1 First Attempt (users)", [fmt_int(g1(w, "fa_u")) for w in weeks])
    push("% Success R1 First Attempt", [fmt_pct(g1(w, "fa_succ_tx"), g1(w, "fa_tx")) for w in weeks])
    push("# R1 Attempts (tx dedup)", [fmt_int(g1(w, "total_tx")) for w in weeks])
    push("% Success R1 Attempts", [fmt_pct(g1(w, "succ_tx"), g1(w, "total_tx")) for w in weeks])
    push("# R1 Succeeded (users)", [fmt_int(g1(w, "succ_u")) for w in weeks])
    push("% R1 Succeeded per User", [fmt_pct(g1(w, "succ_u"), g1(w, "att_u")) for w in weeks])
    push(
        "% Churn Brut R0/R1",
        [fmt_pct(by_wk[w]["r0_succeeded"] - g1(w, "succ_u"), by_wk[w]["r0_succeeded"]) for w in weeks],
    )
    push("# Refund R1", [fmt_int(g1(w, "refund_tx")) for w in weeks])
    push("% Refund R1", [fmt_pct(g1(w, "refund_tx"), g1(w, "succ_tx")) for w in weeks])
    push(
        "% Churn Net R0/R1",
        [
            fmt_pct(
                by_wk[w]["r0_succeeded"] - (g1(w, "succ_u") - g1(w, "refund_u")),
                by_wk[w]["r0_succeeded"],
            )
            for w in weeks
        ],
    )

    # R2 / R3 / R4 (only if total eligible >= 10)
    for rx in ["2", "3", "4"]:
        total_elig = sum(by_wk[w]["rx"].get(rx, {}).get("elig_u", 0) for w in weeks)
        if total_elig < 10:
            continue
        prev = str(int(rx) - 1)
        lbl = f"R{rx}"
        g = lambda w, k, _rx=rx: by_wk[w]["rx"].get(_rx, {}).get(k, 0)
        g_prev = lambda w, k, _prev=prev: by_wk[w]["rx"].get(_prev, {}).get(k, 0)

        def val_or(w, fn, _rx=rx):
            return fn() if g(w, "elig_u") > 0 else "---"

        push(lbl, ["" for _ in weeks], section=True)
        push(f"# {lbl} TBB", [val_or(w, lambda w=w: fmt_int(g(w, "tbb"))) for w in weeks])
        push(f"% {lbl} Billed", [val_or(w, lambda w=w: fmt_pct(g(w, "fa_u"), g(w, "tbb"))) for w in weeks])
        push(f"# {lbl} First Attempt (users)", [val_or(w, lambda w=w: fmt_int(g(w, "fa_u"))) for w in weeks])
        push(f"% Success {lbl} First Attempt", [val_or(w, lambda w=w: fmt_pct(g(w, "fa_succ_tx"), g(w, "fa_tx"))) for w in weeks])
        push(f"# {lbl} Attempts (tx dedup)", [val_or(w, lambda w=w: fmt_int(g(w, "total_tx"))) for w in weeks])
        push(f"% Success {lbl} Attempts", [val_or(w, lambda w=w: fmt_pct(g(w, "succ_tx"), g(w, "total_tx"))) for w in weeks])
        push(f"# {lbl} Succeeded (users)", [val_or(w, lambda w=w: fmt_int(g(w, "succ_u"))) for w in weeks])
        push(f"% {lbl} Succeeded per User", [val_or(w, lambda w=w: fmt_pct(g(w, "succ_u"), g(w, "att_u"))) for w in weeks])
        push(
            f"% Churn Brut R{prev}/{lbl}",
            [val_or(w, lambda w=w: fmt_pct(g_prev(w, "succ_u") - g(w, "succ_u"), g_prev(w, "succ_u"))) for w in weeks],
        )
        push(f"# Refund {lbl}", [val_or(w, lambda w=w: fmt_int(g(w, "refund_tx"))) for w in weeks])
        push(f"% Refund {lbl}", [val_or(w, lambda w=w: fmt_pct(g(w, "refund_tx"), g(w, "succ_tx"))) for w in weeks])
        push(
            f"% Churn Net R{prev}/{lbl}",
            [
                val_or(
                    w,
                    lambda w=w: fmt_pct(
                        g_prev(w, "succ_u") - (g(w, "succ_u") - g(w, "refund_u")),
                        g_prev(w, "succ_u"),
                    ),
                )
                for w in weeks
            ],
        )

    return pd.DataFrame(rows)


def build_vamp_table(df: pd.DataFrame) -> pd.DataFrame:
    weeks = list(df["week_label"].drop_duplicates())
    data = {}
    for _, r in df.iterrows():
        data.setdefault(r["week_label"], {})[r["cat"]] = {"n_tx": r["n_tx"], "n_al": r["n_al"]}

    rows = []

    def push(label: str, vals: Iterable, *, section: bool = False) -> None:
        rows.append({"KPI": ("__SECTION__" + label) if section else label, **{w: v for w, v in zip(weeks, vals)}})

    for name, key in [
        ("Total", "total"),
        ("Rx Booking", "rx_booking"),
        ("R0 Micro", "r0_micro"),
        ("Rx Micro", "rx_micro"),
        ("Rx Magazine", "rx_magazine"),
    ]:
        push(name, ["" for _ in weeks], section=True)
        push(
            f"# Tx Succeeded ({name})",
            [fmt_int(data.get(w, {}).get(key, {}).get("n_tx", 0)) for w in weeks],
        )
        push(
            f"# Alertes ({name})",
            [fmt_int(data.get(w, {}).get(key, {}).get("n_al", 0)) for w in weeks],
        )
        push(
            f"% VAMP ({name})",
            [
                fmt_pct(
                    data.get(w, {}).get(key, {}).get("n_al", 0),
                    data.get(w, {}).get(key, {}).get("n_tx", 0),
                    dec=2,
                )
                for w in weeks
            ],
        )

    return pd.DataFrame(rows)


def style_table(df: pd.DataFrame):
    """Convert section markers to bold rows and return a Styler with light formatting."""
    if df.empty:
        return df
    display = df.copy()
    is_section = display["KPI"].astype(str).str.startswith("__SECTION__")
    display["KPI"] = display["KPI"].astype(str).str.replace("__SECTION__", "", regex=False)
    styler = display.style.set_properties(**{"text-align": "right"}, subset=display.columns.tolist()[1:])
    styler = styler.set_properties(**{"text-align": "left", "font-weight": "500"}, subset=["KPI"])
    styler = styler.apply(
        lambda s: ["background-color: #e2e8f0; font-weight: 600" if v else "" for v in is_section],
        axis=0,
    )
    return styler.hide(axis="index")


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("📊 Weekly PSP Report")
st.caption("Funnel + VAMP — basé sur le skill `weekly-psp-report` (v3).")

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
now_fr = datetime.now(timezone.utc).astimezone().strftime("%-d %B %Y, %H:%M")
header_html = f"""
<div class="meta-box">
  <b>Refresh:</b> {now_fr}
  · <b>Fenêtre:</b> 10 dernières semaines complètes
  · <b>v3 fixes:</b> Booking token exclusion, paused excluded from R0 Succ, dedup tx, R1 TBB = users in r1_elig
  {(' · <b>Filtres actifs:</b> ' + active_filters) if active_filters else ''}
</div>
"""
st.markdown(header_html, unsafe_allow_html=True)

# Tabs
tab_b, tab_m, tab_vc, tab_vd = st.tabs(["Funnel Booking", "Funnel Magazine", "VAMP Cohort", "VAMP Date"])

with tab_b:
    with st.spinner("Funnel Booking…"):
        try:
            df = run_query(funnel_sql("Booking", filters))
            table = build_funnel_table(df, "Booking")
            st.dataframe(style_table(table), use_container_width=True, height=min(40 + 28 * len(table), 1100))
        except Exception as e:
            st.error(f"Erreur Funnel Booking : {e}")

with tab_m:
    with st.spinner("Funnel Magazine…"):
        try:
            df = run_query(funnel_sql("Magazine", filters))
            table = build_funnel_table(df, "Magazine")
            st.dataframe(style_table(table), use_container_width=True, height=min(40 + 28 * len(table), 1100))
        except Exception as e:
            st.error(f"Erreur Funnel Magazine : {e}")

with tab_vc:
    with st.spinner("VAMP Cohort…"):
        try:
            df = run_query(vamp_cohort_sql(filters))
            table = build_vamp_table(df)
            st.dataframe(style_table(table), use_container_width=True, height=40 + 28 * len(table))
        except Exception as e:
            st.error(f"Erreur VAMP Cohort : {e}")

with tab_vd:
    with st.spinner("VAMP Date…"):
        try:
            df = run_query(vamp_date_sql(filters))
            table = build_vamp_table(df)
            st.dataframe(style_table(table), use_container_width=True, height=40 + 28 * len(table))
        except Exception as e:
            st.error(f"Erreur VAMP Date : {e}")
