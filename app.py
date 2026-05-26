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

import html as html_lib
import os
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, List

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

# Light visual polish — table sticky header / colored cells / numeric alignment
st.markdown(
    """
    <style>
      .block-container { padding-top: 1.2rem; padding-bottom: 2rem; max-width: 1700px; }
      .meta-box { padding: 10px 14px; background: #f1f5f9; border-left: 4px solid #6366f1;
                  border-radius: 4px; font-size: 13px; color: #475569; margin-bottom: 12px; }
      .meta-box b { color: #0f172a; }

      /* PSP custom HTML table */
      .psp-scroll {
        overflow: auto;
        max-height: 800px;
        border: 1px solid #cbd5e1;
        border-radius: 6px;
        background: white;
      }
      .psp-table {
        border-collapse: separate;
        border-spacing: 0;
        font-size: 13px;
        font-variant-numeric: tabular-nums;
        width: 100%;
      }
      .psp-table th, .psp-table td {
        padding: 6px 12px;
        border-bottom: 1px solid #f1f5f9;
        border-right: 1px solid #f1f5f9;
        white-space: nowrap;
        text-align: right;
      }
      .psp-table thead th {
        position: sticky;
        top: 0;
        background: #f1f5f9;
        font-weight: 600;
        color: #334155;
        border-bottom: 2px solid #cbd5e1;
        z-index: 2;
        text-align: center;
      }
      .psp-table thead tr:first-child th { top: 0; }
      .psp-table thead tr:nth-child(2) th { top: 33px; }
      .psp-table thead th.kpi {
        z-index: 3;
        background: #e2e8f0;
        text-align: left;
        border-right: 2px solid #94a3b8;
      }
      .psp-table tbody td.kpi {
        position: sticky;
        left: 0;
        background: white;
        text-align: left;
        font-weight: 500;
        color: #334155;
        border-right: 2px solid #94a3b8;
        z-index: 1;
        min-width: 230px;
        max-width: 320px;
      }
      .psp-table tbody tr:nth-child(even) td.kpi { background: #fafbfc; }
      .psp-table tbody tr:nth-child(even) td:not(.kpi) { background: #fafbfc; }
      .psp-table tbody td.kpi.important { font-weight: 700; color: #0f172a; }
      .psp-table tbody td.count { color: #0f172a; font-weight: 500; }
      .psp-table tbody td.count.important { font-weight: 700; }
      .psp-table tbody td.pct { color: #2563eb; background-color: #f0f7ff; }
      .psp-table tbody tr:nth-child(even) td.pct { background-color: #e6f1fd; }
      .psp-table tbody td.churn { color: #dc2626; font-weight: 600; }
      .psp-table tbody tr:nth-child(even) td.churn { background-color: #fff5f5; }
      .psp-table tbody tr.section td {
        background: #1e293b !important;
        color: #f8fafc;
        font-weight: 700;
        font-size: 11px;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        text-align: left;
        padding: 8px 12px;
      }
      .psp-table tbody tr.section td.kpi {
        position: sticky;
        left: 0;
        z-index: 1;
        border-right: 2px solid #1e293b;
      }
      /* Thick separator between outer-dim groups (e.g. between two weeks
         when stacking Semaine × Verticale). */
      .psp-table th.col-boundary,
      .psp-table td.col-boundary {
        border-right: 2px solid #475569;
      }
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
    # Expressions are rewired just below DIMENSION_DIMS (after they are defined).
    ("conciergerie",    "Conciergerie",    "PLACEHOLDER",        "PLACEHOLDER"),
    ("verticale",       "Verticale",       "sgw_verticale",      "sgw_verticale"),
    ("psp",             "PSP",             "ms_default_psp",     "ms_default_psp"),
    ("currency",        "Devise",          "ms_currency",        "ms_currency"),
    ("booking_market",  "Marché Booking",  "sgw_booking_market", "sgw_booking_market"),
    ("price_booking",   "Prix Booking",    "PLACEHOLDER",        "PLACEHOLDER"),
    ("price_magazine",  "Prix Magazine",   "PLACEHOLDER",        "PLACEHOLDER"),
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
_BOOKING_BUCKETS = (
    "CASE CAST(ROUND(COALESCE({alias}.rounded_subscription_price, 0)) AS INT64) "
    "  WHEN 20 THEN '19€ bi-mensuel' "
    "  WHEN 30 THEN '29€' "
    "  WHEN 50 THEN '49€ mensuel' "
    "  WHEN 60 THEN '59€' "
    "  WHEN 70 THEN '69€ mensuel' "
    "  ELSE CONCAT(CAST(CAST(ROUND(COALESCE({alias}.rounded_subscription_price, 0)) AS INT64) AS STRING), '€') "
    "END"
)
_MAGAZINE_BUCKETS = (
    "CASE "
    "  WHEN ROUND({alias}.ms_price_amount_eur, 2) = 0.25 THEN '25ct weekly' "
    "  WHEN ROUND({alias}.ms_price_amount_eur, 2) = 0.10 THEN '10ct weekly' "
    "  WHEN ROUND({alias}.ms_price_amount_eur, 2) = 0.01 THEN '1ct' "
    "  WHEN ROUND({alias}.ms_price_amount_eur, 2) = 1.00 THEN '1€' "
    "  WHEN ROUND({alias}.ms_price_amount_eur, 2) = 1.90 THEN '1.90€' "
    "  ELSE CONCAT(CAST(ROUND({alias}.ms_price_amount_eur, 2) AS STRING), '€') "
    "END"
)

# Unified price expression — used by the "Prix abonnement" DIMENSION (works
# across brand_types because the dim is typically used inside a brand-scoped tab).
PRICE_EXPR = (
    "CASE "
    f"WHEN {{alias}}.brand_type = 'Booking' THEN {_BOOKING_BUCKETS} "
    f"WHEN {{alias}}.brand_type = 'Magazine' THEN {_MAGAZINE_BUCKETS} "
    "ELSE '' END"
)

# Split expressions — used by the two separate FILTERS.
# Each returns the bucket only if brand_type matches, '' otherwise. When the
# user picks a value for "Prix Booking", the filter clause implicitly restricts
# to Booking rows (Magazine rows return '' and don't match the IN list).
BOOKING_PRICE_EXPR = (
    "CASE "
    f"WHEN {{alias}}.brand_type = 'Booking' THEN {_BOOKING_BUCKETS} "
    "ELSE '' END"
)
MAGAZINE_PRICE_EXPR = (
    "CASE "
    f"WHEN {{alias}}.brand_type = 'Magazine' THEN {_MAGAZINE_BUCKETS} "
    "ELSE '' END"
)

# Conciergerie expression — maps brand to the canonical business name.
# Same brand for both Booking and Magazine (e.g. "Reserv-Go" and
# "Reserv-go - magazine" → "Reserv-Go").
# Two variants: one using fm.brand, one using ft.t_brand.
def _conciergerie_expr(brand_col: str) -> str:
    return (
        f"CASE LOWER(REGEXP_REPLACE(COALESCE({{alias}}.{brand_col}, ''), r' - magazine$', '')) "
        "  WHEN 'reserv-go'    THEN 'Reserv-Go' "
        "  WHEN 'book-ici'     THEN 'Book-Ici' "
        "  WHEN 'rezaflash'    THEN 'Rezaflash' "
        "  WHEN 'resadexa'     THEN 'Resadexa' "
        "  WHEN 'jumpaide.com' THEN 'Jumpaide' "
        "  WHEN 'concimax'     THEN 'Concimax' "
        "  WHEN 'rapidoxy'     THEN 'Rapidoxy' "
        "  WHEN 'concicast'    THEN 'Concicast' "
        f"  ELSE COALESCE({{alias}}.{brand_col}, '') "
        "END"
    )

CONCIERGERIE_EXPR_FM = _conciergerie_expr("brand")
CONCIERGERIE_EXPR_FT = _conciergerie_expr("t_brand")

DIMENSION_DIMS = [
    # (key, label, fm column-or-expression, ft column-or-expression)
    # Spec shapes (see dim_select_clause):
    #   - 'RAW:<expr>'    → raw expression (no scope prefix). Used for
    #                       cp_price.price_booking / cp_price.price_magazine.
    #   - '...{alias}...' → SQL expression with {alias} substituted to scope.
    #   - 'col_name'      → plain column (wrapped with COALESCE(scope.col, '')).
    # The three "date_*" keys are mutually exclusive (only one date granularity
    # makes sense at a time) — the UI keeps just the first date dim selected.
    ("date_week",      "Date (semaine)",          None,                          None),
    ("date_day",       "Date (jour)",             None,                          None),
    ("date_month",     "Date (mois)",             None,                          None),
    ("conciergerie",   "Conciergerie",            CONCIERGERIE_EXPR_FM,          CONCIERGERIE_EXPR_FT),
    ("verticale",      "Verticale",               "sgw_verticale",               "sgw_verticale"),
    # Customer-level price buckets (cp_price CTE).
    # Same semantics as the "Prix Booking" / "Prix Magazine" FILTERS: each
    # customer is assigned ONE Booking bucket (most recent in window) and ONE
    # Magazine bucket, regardless of which row we're looking at. A customer
    # with only Magazine subs lands in '(empty)' for the Booking dim.
    ("price_booking",  "Prix Booking",            "RAW:cp_price.price_booking",  "RAW:cp_price.price_booking"),
    ("price_magazine", "Prix Magazine",           "RAW:cp_price.price_magazine", "RAW:cp_price.price_magazine"),
    ("psp",            "PSP",                     "ms_default_psp",              "ms_default_psp"),
    ("currency",       "Devise",                  "ms_currency",                 "ms_currency"),
    ("booking_market", "Marché Booking",          "sgw_booking_market",          "sgw_booking_market"),
]

# Mapping of date dim key → BigQuery DATE_TRUNC granularity argument.
_DATE_DIM_TRUNC = {
    "date_day":   "DAY",
    "date_week":  "WEEK(MONDAY)",
    "date_month": "MONTH",
}
# Format string for FORMAT_DATE() to render a column header for each period.
# Day → DD/MM/YYYY ; Week → "S20 (11/05)" so the user sees the actual Monday;
# Month → "Mai 2026". Sorting is done in Python by week_start (real date), not
# by the label, so these can stay human-friendly.
_DATE_DIM_LABEL_FMT = {
    "date_day":   "%d/%m/%Y",
    "date_week":  "S%V (%d/%m)",
    "date_month": "%b %Y",
}
# Set of keys recognized as date dims.
_DATE_DIM_KEYS = set(_DATE_DIM_TRUNC.keys())

DIM_BY_LABEL = {d[1]: d for d in DIMENSION_DIMS}

# Now that all expressions are defined, wire the filters that use them.
def _wire_filter(k, fm, ft):
    if k == "price_booking":
        return (BOOKING_PRICE_EXPR, BOOKING_PRICE_EXPR)
    if k == "price_magazine":
        return (MAGAZINE_PRICE_EXPR, MAGAZINE_PRICE_EXPR)
    if k == "conciergerie":
        return (CONCIERGERIE_EXPR_FM, CONCIERGERIE_EXPR_FT)
    return (fm, ft)

FILTER_DIMS = [
    (k, l, *_wire_filter(k, fm, ft))
    for (k, l, fm, ft) in FILTER_DIMS
]


def selected_dims(labels: list) -> list:
    """Resolve user-chosen labels to dimension specs (in chosen order)."""
    return [DIM_BY_LABEL[l] for l in labels if l in DIM_BY_LABEL]


def date_dim_key(dims: list) -> str:
    """Return the key of the date dim selected (date_day/date_week/date_month),
    or None if none selected. Only the first date dim found is used."""
    for d in dims:
        if d[0] in _DATE_DIM_KEYS:
            return d[0]
    return None


def non_week_dims(dims: list) -> list:
    """Return dims excluding any date dim (back-compat name)."""
    return [d for d in dims if d[0] not in _DATE_DIM_KEYS]


def dim_select_clause(scope: str, dims: list, indent: int = 4) -> str:
    """SELECT projections for non-week dim cols. Always ends with a trailing comma+newline.

    Three shapes supported in the dim spec (fm_col / ft_col):
      - 'RAW:<expr>'         → use <expr> as-is (no scope prefix). Used for
                               cross-table expressions like `cp_price.price_booking`.
      - '...{alias}...'      → SQL expression; {alias} substituted with scope.
      - 'col_name'           → plain column; rendered as `COALESCE(scope.col_name, '')`.
    """
    nw = non_week_dims(dims)
    if not nw:
        return ""
    sp = " " * indent
    lines = []
    for key, _label, fm_col, ft_col in nw:
        col_spec = fm_col if scope == "fm" else ft_col
        if col_spec.startswith("RAW:"):
            raw = col_spec[4:]
            lines.append(f"{sp}COALESCE({raw}, '') AS dim_{key}")
        elif "{alias}" in col_spec:
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
def weeks_in_range(start: date, end: date) -> List[date]:
    """Return all Monday-starting week dates whose week intersects [start, end]."""
    if start > end:
        start, end = end, start
    first = start - timedelta(days=start.weekday())
    last = end - timedelta(days=end.weekday())
    out: List[date] = []
    d = first
    while d <= last:
        out.append(d)
        d += timedelta(days=7)
    return out


def periods_in_range(start: date, end: date, granularity: str = "date_week") -> List[date]:
    """Return period-start dates within [start, end] for the chosen granularity.
    granularity is one of: date_day / date_week / date_month."""
    if start > end:
        start, end = end, start
    if granularity == "date_day":
        out: List[date] = []
        d = start
        while d <= end:
            out.append(d)
            d += timedelta(days=1)
        return out
    if granularity == "date_month":
        out = []
        d = date(start.year, start.month, 1)
        while d <= end:
            out.append(d)
            if d.month == 12:
                d = date(d.year + 1, 1, 1)
            else:
                d = date(d.year, d.month + 1, 1)
        return out
    return weeks_in_range(start, end)


def weeks_cte_sql(weeks_list: List[date], granularity: str = "date_week") -> str:
    """Build the WITH weeks / window_bounds CTE from a list of period-start dates.
    Despite the legacy name, this works for any granularity (day/week/month) —
    we keep the CTE & column names as `weeks` / `week_start` / `week_label` for
    back-compat with the downstream SQL.

    `window_bounds` covers from the first period to the end of the last period
    (last day for daily, last day-of-week for weekly, last day-of-month for monthly).
    """
    if not weeks_list:
        weeks_arr = "DATE '1970-01-01'"
    else:
        weeks_arr = ", ".join(f"DATE '{w.isoformat()}'" for w in weeks_list)
    label_fmt = _DATE_DIM_LABEL_FMT.get(granularity, "S%V")
    # window_bounds: end depends on granularity
    if granularity == "date_day":
        ws_max_expr = "MAX(week_start)"
    elif granularity == "date_month":
        ws_max_expr = "DATE_SUB(DATE_ADD(MAX(week_start), INTERVAL 1 MONTH), INTERVAL 1 DAY)"
    else:
        ws_max_expr = "DATE_ADD(MAX(week_start), INTERVAL 6 DAY)"
    return f"""
WITH weeks AS (
  SELECT
    w AS week_start,
    FORMAT_DATE('{label_fmt}', w) AS week_label
  FROM UNNEST([{weeks_arr}]) AS w
), window_bounds AS (
  SELECT MIN(week_start) AS ws_min, {ws_max_expr} AS ws_max FROM weeks
)
"""


# Back-compat constant — fall back to "last 10 complete weeks" if a query is
# called without an explicit weeks_list (legacy callers).
def _default_weeks() -> List[date]:
    today = date.today()
    # Last 10 complete weeks ending Sunday before today's week.
    last_complete_sunday = today - timedelta(days=today.weekday() + 1)
    end = last_complete_sunday
    start = end - timedelta(days=10 * 7 - 1)
    return weeks_in_range(start, end)


WEEKS_CTE = weeks_cte_sql(_default_weeks())


def sql_escape(value: str) -> str:
    return value.replace("'", "''")


# ---------------------------------------------------------------------------
# Filter semantics — differentiated by filter kind.
#
# CUSTOMER-LEVEL filters (Prix Booking / Prix Magazine):
#   Keep all customers with AT LEAST ONE matching membership, then show ALL
#   their rows (Booking + Magazine). Lets the user answer "in the Magazine tab,
#   show me the behaviour of customers who pay 49€ on Booking" — even though
#   their Magazine subs have nothing to do with the 49€ Booking bucket.
#   Implemented via the `customer_pool` CTE (HAVING COUNTIF > 0) joined to the
#   main FROM blocks.
#
# ROW-LEVEL filters (PSP / Verticale / Conciergerie / Devise / Marché Booking):
#   Each row must independently match the filter. Used to be customer-level
#   but that was confusing: a customer who had memberships on multiple PSPs
#   (e.g. NMI then re-routed to Pixxles) would show ALL their memberships
#   even when filtering on just one PSP. Row-level applies the filter directly
#   in the WHERE of bm/bt/tx/al_tx.
# ---------------------------------------------------------------------------
_CUSTOMER_LEVEL_FILTER_KEYS = ("price_booking", "price_magazine")


def _has_active_filters(filters: dict) -> bool:
    return any(filters.get(k[0]) for k in FILTER_DIMS)


def _has_customer_level_filters(filters: dict) -> bool:
    return any(filters.get(k) for k in _CUSTOMER_LEVEL_FILTER_KEYS)


def _has_row_level_filters(filters: dict) -> bool:
    return any(
        filters.get(d[0])
        for d in FILTER_DIMS
        if d[0] not in _CUSTOMER_LEVEL_FILTER_KEYS
    )


def customer_pool_cte(filters: dict) -> str:
    """Build the `customer_pool` CTE — restricted to CUSTOMER-LEVEL filters
    only (Prix Booking / Prix Magazine). For row-level filters, see
    `row_level_filter_clause`.

    Returns ',\\ncustomer_pool AS (...)' so it can be inserted right after
    WEEKS_CTE; '' if no customer-level filter is active.
    """
    if not _has_customer_level_filters(filters):
        return ""

    having_clauses = []
    for key, _label, fm_col, _ft_col in FILTER_DIMS:
        if key not in _CUSTOMER_LEVEL_FILTER_KEYS:
            continue
        vals = filters.get(key, [])
        if not vals:
            continue
        if "{alias}" in fm_col:
            target = fm_col.format(alias="fm")
        else:
            target = f"fm.{fm_col}"
        rendered = ", ".join(
            f"'{sql_escape(v)}'" if v != "(empty)" else "''" for v in vals
        )
        has_empty = "(empty)" in vals
        if has_empty:
            cond = f"({target} IN ({rendered}) OR {target} IS NULL)"
        else:
            cond = f"{target} IN ({rendered})"
        having_clauses.append(f"COUNTIF({cond}) > 0")

    if not having_clauses:
        return ""

    having_sql = "\n    AND ".join(having_clauses)
    return f""",
customer_pool AS (
  SELECT fm.customer_email
  FROM `eu-andy-marketing-raw.dashboard.fact_memberships` fm
  CROSS JOIN window_bounds wb_cp
  WHERE DATE(fm.ms_datetime) BETWEEN wb_cp.ws_min AND wb_cp.ws_max
    AND COALESCE(fm.brand, '') NOT LIKE '%helpprio%'
    AND LOWER(fm.customer_email) NOT LIKE '%@yopmail%'
    AND LOWER(fm.customer_email) NOT LIKE '%@sharebot%'
    AND LOWER(fm.customer_firstname) NOT LIKE '%test%'
  GROUP BY fm.customer_email
  HAVING {having_sql}
)"""


def customer_pool_where(alias: str, filters: dict) -> str:
    """Deprecated — kept for back-compat. Returns '' since we now restrict to
    the pool via an explicit INNER JOIN (see customer_pool_join)."""
    return ""


def customer_pool_join(alias: str, filters: dict) -> str:
    """Returns an INNER JOIN against customer_pool to restrict rows to the
    customer-level pool, or '' if no customer-level filter is active.

    BigQuery cannot decorrelate IN/EXISTS subqueries that reference an
    aggregated CTE, so we use a plain JOIN — which it can plan efficiently.
    The pool alias is suffixed with the table alias to avoid collisions when
    the same pool is referenced from multiple FROM blocks in one query.
    """
    if not _has_customer_level_filters(filters):
        return ""
    return (
        f"INNER JOIN customer_pool cp_pool_{alias} "
        f"ON cp_pool_{alias}.customer_email = {alias}.customer_email"
    )


def row_level_filter_clause(scope: str, filters: dict) -> str:
    """Build ROW-LEVEL filter conditions for non-price filters
    (PSP / Verticale / Conciergerie / Devise / Marché Booking).

    Returns a chain of '\\n    AND <cond>' clauses ready to be injected at the
    end of the WHERE block of bm/bt/tx/al_tx; '' if no row-level filter
    active.

    `scope` is the table alias ('fm', 'ft', or 't') — picked column expression
    is taken from FILTER_DIMS' fm_col / ft_col, with {alias} substituted.
    """
    if not _has_row_level_filters(filters):
        return ""
    clauses = []
    for key, _label, fm_col, ft_col in FILTER_DIMS:
        if key in _CUSTOMER_LEVEL_FILTER_KEYS:
            continue
        vals = filters.get(key, [])
        if not vals:
            continue
        col_spec = fm_col if scope == "fm" else ft_col
        if "{alias}" in col_spec:
            target = col_spec.format(alias=scope)
        else:
            target = f"{scope}.{col_spec}"
        rendered = ", ".join(
            f"'{sql_escape(v)}'" if v != "(empty)" else "''" for v in vals
        )
        has_empty = "(empty)" in vals
        if has_empty:
            cond = f"({target} IN ({rendered}) OR {target} IS NULL)"
        else:
            cond = f"{target} IN ({rendered})"
        clauses.append(f"AND {cond}")
    if not clauses:
        return ""
    return "\n    " + "\n    ".join(clauses)


# Legacy alias for any old call-site that still expects a per-row filter clause.
def filter_clauses(scope: str, filters: dict) -> str:
    return customer_pool_where(scope, filters)


# ---------------------------------------------------------------------------
# customer_price CTE — customer-level price-bucket lookup.
#
# Mirrors the FILTER semantics for "Prix Booking" / "Prix Magazine" but exposed
# as a DIMENSION (column-axis split). For each customer, we compute:
#   - price_booking  = bucket of their MOST RECENT Booking membership
#                      (NULL if they never had a Booking sub in the window)
#   - price_magazine = idem for Magazine
# Then the funnel/vamp queries LEFT JOIN this CTE on customer_email and project
# COALESCE(cp_price.price_booking, '') AS dim_price_booking.
#
# Why MOST RECENT (not MAX): if a customer upgrades 19€ → 49€, we want to
# represent them as "49€ customer" (current state). ARRAY_AGG ORDER BY
# ms_datetime DESC LIMIT 1 gives the most-recent non-NULL bucket.
# ---------------------------------------------------------------------------
_CP_PRICE_DIM_KEYS = ("price_booking", "price_magazine")


def _has_cp_price_dim(dims: list) -> bool:
    """True iff the user selected a customer-level price dim."""
    return any(d[0] in _CP_PRICE_DIM_KEYS for d in dims)


def customer_price_cte(dims: list) -> str:
    """Build the `customer_price` CTE — one row per customer with their most
    recent Booking & Magazine bucket. Returns ',\\ncustomer_price AS (...)' so
    it can be appended right after WEEKS_CTE / customer_pool_cte; '' if no
    customer-level price dim is selected.

    Depends on `window_bounds` (from weeks_cte_sql) — must be emitted AFTER it.
    """
    if not _has_cp_price_dim(dims):
        return ""
    bb = _BOOKING_BUCKETS.format(alias="fm")
    mb = _MAGAZINE_BUCKETS.format(alias="fm")
    return f""",
customer_price AS (
  SELECT
    fm.customer_email,
    ARRAY_AGG(
      CASE WHEN fm.brand_type = 'Booking' THEN {bb} ELSE NULL END
      IGNORE NULLS ORDER BY fm.ms_datetime DESC LIMIT 1
    )[SAFE_OFFSET(0)] AS price_booking,
    ARRAY_AGG(
      CASE WHEN fm.brand_type = 'Magazine' THEN {mb} ELSE NULL END
      IGNORE NULLS ORDER BY fm.ms_datetime DESC LIMIT 1
    )[SAFE_OFFSET(0)] AS price_magazine
  FROM `eu-andy-marketing-raw.dashboard.fact_memberships` fm
  CROSS JOIN window_bounds wb_cp_price
  WHERE DATE(fm.ms_datetime) BETWEEN wb_cp_price.ws_min AND wb_cp_price.ws_max
    AND COALESCE(fm.brand, '') NOT LIKE '%helpprio%'
    AND LOWER(fm.customer_email) NOT LIKE '%@yopmail%'
    AND LOWER(fm.customer_email) NOT LIKE '%@sharebot%'
    AND LOWER(fm.customer_firstname) NOT LIKE '%test%'
  GROUP BY fm.customer_email
)"""


def customer_price_join(alias: str, dims: list) -> str:
    """LEFT JOIN against customer_price keyed on customer_email, or '' when no
    customer-level price dim is selected.

    LEFT (not INNER): customers absent from fact_memberships in the window
    (rare — can happen if a tx exists inside the window but their memberships
    are outside) should still appear, with NULL → '(empty)' bucket.

    The cp_price alias is a constant (not suffixed). Each SQL function only
    references it from one CTE per table alias, so no collisions.
    """
    if not _has_cp_price_dim(dims):
        return ""
    return (
        f"LEFT JOIN customer_price cp_price "
        f"ON cp_price.customer_email = {alias}.customer_email"
    )


# ---------------------------------------------------------------------------
# Funnel query (one per brand_type) — supports dynamic dimensions
# ---------------------------------------------------------------------------
def funnel_sql(brand_type: str, filters: dict, dims: list, weeks_list: List[date] = None) -> str:
    granularity = date_dim_key(dims) or "date_week"
    trunc_arg = _DATE_DIM_TRUNC[granularity]
    weeks_cte = weeks_cte_sql(weeks_list, granularity) if weeks_list is not None else WEEKS_CTE
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
    cp_cte = customer_pool_cte(filters)
    cp_join_fm = customer_pool_join("fm", filters)
    cp_join_ft = customer_pool_join("ft", filters)
    cp_price_cte = customer_price_cte(dims)
    cp_price_join_fm = customer_price_join("fm", dims)
    # Row-level filters (PSP / Verticale / Conciergerie / Devise / Marché Booking)
    # — applied directly in WHERE of bm and bt so non-matching rows are excluded.
    fm_filter = row_level_filter_clause("fm", filters)
    ft_filter = row_level_filter_clause("ft", filters)

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
{weeks_cte}{cp_cte}{cp_price_cte},
bm AS (
  SELECT
    DATE_TRUNC(DATE(c.CreatedAtUtc), {trunc_arg}) AS cohort_week,
{bm_dim_sel}    fm.customer_email, fm.ms_status,
    sm.CancelAtUtc, sm.TrialEndUtc
  FROM `eu-andy-marketing-raw.dashboard.fact_memberships` fm
  JOIN `eu-andy-marketing-raw.silver_sgw.stg_customers` c ON fm.customer_id = c.Id
  JOIN `eu-andy-marketing-raw.silver_sgw.stg_memberships` sm
    ON CAST(fm.membership_id AS STRING) = CAST(sm.Id AS STRING)
  {cp_join_fm}
  {cp_price_join_fm}
  CROSS JOIN window_bounds wb
  WHERE DATE(c.CreatedAtUtc) BETWEEN wb.ws_min AND wb.ws_max
    AND DATE_TRUNC(DATE(c.CreatedAtUtc), {trunc_arg}) IN (SELECT week_start FROM weeks)
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
    DATE_TRUNC(DATE(c.CreatedAtUtc), {trunc_arg}) AS cohort_week,
    ft.transaction_id, ft.customer_email, ft.membership_id, ft.invoice_r_index,
    ft.transaction_status, ft.is_refunded, ft.t_attempt_index, ft.t_datetime,
    ft.ms_billing_frequency, ft.ms_billing_period
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ft
  JOIN `eu-andy-marketing-raw.silver_sgw.stg_customers` c ON ft.customer_id = c.Id
  {cp_join_ft}
  CROSS JOIN window_bounds wb
  WHERE DATE(c.CreatedAtUtc) BETWEEN wb.ws_min AND wb.ws_max
    AND ft.t_date BETWEEN wb.ws_min AND CURRENT_DATE()
    AND DATE_TRUNC(DATE(c.CreatedAtUtc), {trunc_arg}) IN (SELECT week_start FROM weeks)
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


def vamp_cohort_sql(filters: dict, dims: list, weeks_list: List[date] = None) -> str:
    granularity = date_dim_key(dims) or "date_week"
    trunc_arg = _DATE_DIM_TRUNC[granularity]
    weeks_cte = weeks_cte_sql(weeks_list, granularity) if weeks_list is not None else WEEKS_CTE
    cp_cte = customer_pool_cte(filters)
    cp_join_ft = customer_pool_join("ft", filters)
    cp_price_cte = customer_price_cte(dims)
    cp_price_join_ft = customer_price_join("ft", dims)
    ft_filter = row_level_filter_clause("ft", filters)
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
{weeks_cte}{cp_cte}{cp_price_cte},
tx AS (
  SELECT ft.transaction_id, ft.brand_type, ft.invoice_r_index, ft.transaction_amount,
{bm_dim_sel}    DATE_TRUNC(DATE(c.CreatedAtUtc), {trunc_arg}) AS cohort_week
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ft
  JOIN `eu-andy-marketing-raw.silver_sgw.stg_customers` c ON ft.customer_id = c.Id
  {cp_join_ft}
  {cp_price_join_ft}
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
    -- R0 trial: any brand, small first transaction (exclusive bucket so no
    -- double-count downstream).
    WHEN invoice_r_index='0' AND transaction_amount <= 1 THEN 'r0_micro'
    WHEN invoice_r_index='RX_micro' AND transaction_amount = 0.01 THEN 'rx_micro'
    -- Recurring transactions: categorize by brand, exclude R0 / micro.
    WHEN brand_type='Booking'  AND invoice_r_index NOT IN ('0','RX_micro') THEN 'rx_booking'
    WHEN brand_type='Magazine' AND invoice_r_index NOT IN ('0','RX_micro') THEN 'rx_magazine'
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


def vamp_date_sql(filters: dict, dims: list, weeks_list: List[date] = None) -> str:
    granularity = date_dim_key(dims) or "date_week"
    trunc_arg = _DATE_DIM_TRUNC[granularity]
    weeks_cte = weeks_cte_sql(weeks_list, granularity) if weeks_list is not None else WEEKS_CTE
    cp_cte = customer_pool_cte(filters)
    cp_join_ft = customer_pool_join("ft", filters)
    cp_join_t = customer_pool_join("t", filters)
    cp_price_cte = customer_price_cte(dims)
    cp_price_join_ft = customer_price_join("ft", dims)
    cp_price_join_t = customer_price_join("t", dims)
    ft_filter = row_level_filter_clause("ft", filters)
    ft_filter_t = row_level_filter_clause("t", filters)
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
{weeks_cte}{cp_cte}{cp_price_cte},
tx AS (
  SELECT ft.transaction_id, ft.brand_type, ft.invoice_r_index, ft.transaction_amount,
{bm_dim_sel_ft}    DATE_TRUNC(ft.t_date, {trunc_arg}) AS tx_week
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ft
  {cp_join_ft}
  {cp_price_join_ft}
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
    WHEN invoice_r_index='0' AND transaction_amount <= 1 THEN 'r0_micro'
    WHEN invoice_r_index='RX_micro' AND transaction_amount = 0.01 THEN 'rx_micro'
    WHEN brand_type='Booking'  AND invoice_r_index NOT IN ('0','RX_micro') THEN 'rx_booking'
    WHEN brand_type='Magazine' AND invoice_r_index NOT IN ('0','RX_micro') THEN 'rx_magazine'
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
      WHEN t.invoice_r_index='0' AND t.transaction_amount <= 1 THEN 'r0_micro'
      WHEN t.invoice_r_index='RX_micro' AND t.transaction_amount = 0.01 THEN 'rx_micro'
      WHEN t.brand_type='Booking'  AND t.invoice_r_index NOT IN ('0','RX_micro') THEN 'rx_booking'
      WHEN t.brand_type='Magazine' AND t.invoice_r_index NOT IN ('0','RX_micro') THEN 'rx_magazine'
      ELSE NULL END AS cat
  FROM al a JOIN `eu-andy-marketing-raw.dashboard.fact_transactions` t ON a.transaction_id = t.transaction_id
  {cp_join_t}
  {cp_price_join_t}
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


def filter_options_sql(start: date = None, end: date = None) -> str:
    """Pull distinct values for each filter dimension from fact_memberships
    over the user-selected date range (or fall back to 80 days if no range
    provided).

    The window matches what `funnel_sql` / `vamp_*_sql` actually query, so
    the sidebar can't propose a value that has no data — and (critically)
    can't HIDE a value that does have data in the selected range. Without
    this, picking a date range outside the default 80-day window would make
    historical PSPs like `quaife` invisible in the filter dropdown.
    """
    booking_bucket = BOOKING_PRICE_EXPR.format(alias="fm")
    magazine_bucket = MAGAZINE_PRICE_EXPR.format(alias="fm")
    conciergerie = CONCIERGERIE_EXPR_FM.format(alias="fm")
    # Build window bounds — fall back to last 80 days if no range provided.
    if start and end:
        ws_min = f"DATE '{start.isoformat()}'"
        ws_max = f"DATE '{end.isoformat()}'"
    else:
        ws_min = "DATE_SUB(CURRENT_DATE(), INTERVAL 80 DAY)"
        ws_max = "CURRENT_DATE()"
    return f"""
WITH wb AS (
  SELECT {ws_min} AS ws_min, {ws_max} AS ws_max
),
fm_recent AS (
  SELECT
    COALESCE(fm.ms_default_psp,     '') AS psp,
    COALESCE({conciergerie},        '') AS conciergerie,
    COALESCE(fm.sgw_verticale,      '') AS verticale,
    COALESCE(fm.ms_currency,        '') AS currency,
    COALESCE(fm.sgw_booking_market, '') AS booking_market,
    COALESCE({booking_bucket},      '') AS price_booking,
    COALESCE({magazine_bucket},     '') AS price_magazine
  FROM `eu-andy-marketing-raw.dashboard.fact_memberships` fm
  CROSS JOIN wb
  WHERE DATE(fm.ms_datetime) BETWEEN wb.ws_min AND wb.ws_max
    AND COALESCE(fm.brand, '') NOT LIKE '%helpprio%'
    AND LOWER(fm.customer_email) NOT LIKE '%@yopmail%'
    AND LOWER(fm.customer_email) NOT LIKE '%@sharebot%'
    AND LOWER(fm.customer_firstname) NOT LIKE '%test%'
)
SELECT dim, val, COUNT(*) AS n FROM fm_recent
UNPIVOT (val FOR dim IN (psp, conciergerie, verticale, currency, booking_market, price_booking, price_magazine))
WHERE NOT (dim IN ('price_booking', 'price_magazine') AND val = '')
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
            if d[0] in _DATE_DIM_KEYS:
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
    has_week = any(d[0] in _DATE_DIM_KEYS for d in dims) if dims else False
    non_week = non_week_dims(dims) if dims else []

    def gk(r):
        if not dims:
            return ("Total",)
        parts = []
        for d in dims:
            if d[0] in _DATE_DIM_KEYS:
                parts.append(str(r["week_label"]))
            else:
                parts.append(_safe_str(r.get(f"dim_{d[0]}", "")))
        return tuple(parts)

    by_group = {}
    # Track which (cohort_week × non_week dim combo) tuples we've already counted
    # so cohort-level metrics aren't multiplied by rx_idx repetition.
    cohort_seen = {}
    # Map display group → sort key (replaces date label with ISO week_start
    # so chronological sorting works even when labels are "May 2026" / "Apr 2026").
    group_sort_key = {}

    for _, r in df.iterrows():
        g = gk(r)
        if g not in by_group:
            by_group[g] = {
                "r0_attempts": 0, "r0_succeeded": 0, "unsub_trial": 0, "r1_tbb": 0,
                "rx": {},
            }
            cohort_seen[g] = set()
            # Build the sort key: same shape as g, but date dims use week_start (ISO).
            sort_parts = []
            for d in dims or []:
                if d[0] in _DATE_DIM_KEYS:
                    sort_parts.append(str(r.get("week_start", "")))
                else:
                    sort_parts.append(_safe_str(r.get(f"dim_{d[0]}", "")))
            group_sort_key[g] = tuple(sort_parts) if sort_parts else g

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

    groups = sorted(by_group.keys(), key=lambda g: group_sort_key.get(g, g))
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
        push(f"# {lbl} To Be Billed", [val_or(g, lambda g=g: fmt_int(gr(g, rx, "tbb"))) for g in groups])
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
            if d[0] in _DATE_DIM_KEYS:
                parts.append(str(r["week_label"]))
            else:
                parts.append(_safe_str(r.get(f"dim_{d[0]}", "")))
        return tuple(parts)

    by_group = {}
    group_sort_key = {}
    for _, r in df.iterrows():
        g = gk(r)
        bg = by_group.setdefault(g, {})
        if g not in group_sort_key:
            sort_parts = []
            for d in dims or []:
                if d[0] in _DATE_DIM_KEYS:
                    sort_parts.append(str(r.get("week_start", "")))
                else:
                    sort_parts.append(_safe_str(r.get(f"dim_{d[0]}", "")))
            group_sort_key[g] = tuple(sort_parts) if sort_parts else g
        cat = r["cat"]
        existing = bg.get(cat, {"n_tx": 0, "n_al": 0})
        existing["n_tx"] += int(r["n_tx"])
        existing["n_al"] += int(r["n_al"])
        bg[cat] = existing

    groups = sorted(by_group.keys(), key=lambda g: group_sort_key.get(g, g))
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


# Labels of metrics considered "important" (rendered in bold)
_IMPORTANT_TOKENS = (
    "# R0 Attempts",
    "# R0 Succeeded",
    "# R1 To Be Billed",
    "# R1 Succeeded (users)",
    "# R2 To Be Billed",
    "# R2 Succeeded (users)",
    "# R3 To Be Billed",
    "# R3 Succeeded (users)",
    "# R4 To Be Billed",
    "# R4 Succeeded (users)",
    "% VAMP (Total)",
    "% VAMP (Rx Booking)",
    "% VAMP (Rx Magazine)",
    "# Tx Succeeded (Total)",
)


def _esc(v) -> str:
    return html_lib.escape("" if v is None else str(v))


def render_table_html(df: pd.DataFrame) -> str:
    """Render a built table (from build_funnel_table / build_vamp_table) as
    a self-contained HTML table with sticky header + sticky first column and
    color coding for # / % / Churn / important rows."""
    if df.empty:
        return "<div style='color:#64748b;padding:12px;'>Aucune donnée</div>"

    dims = df.attrs.get("dims", [])
    groups = df.attrs.get("groups", [])
    levels = [d[1] for d in dims] if dims else []
    boundary_positions: set = set()  # populated below (only when 2 dims)

    # ---- Header ----------------------------------------------------------
    if not dims:
        # 0 dims — single "Total" column
        header_html = "<tr><th class='kpi'>KPI</th><th>Total</th></tr>"
    elif len(dims) == 1:
        cells = "".join(f"<th>{_esc(g[0])}</th>" for g in groups)
        header_html = (
            f"<tr><th class='kpi'>{_esc(levels[0])}</th>{cells}</tr>"
        )
    else:
        # 2 dims — 2-row header. Compute spans for outer dim.
        # Build ordered dict-like: outer -> [inner1, inner2, ...]
        outer_to_inners: dict = {}
        for g in groups:
            outer_to_inners.setdefault(g[0], []).append(g[1])

        # Compute boundary positions in `groups`: the last column of each
        # outer-dim group gets a thick right border in the header & body.
        idx = 0
        outer_count = len(outer_to_inners)
        for o_i, (_outer, inners) in enumerate(outer_to_inners.items()):
            idx += len(inners)
            if o_i < outer_count - 1:
                boundary_positions.add(idx - 1)

        row1_cells = [f"<th class='kpi' rowspan='2'>{_esc(' / '.join(levels))}</th>"]
        for o_i, (outer, inners) in enumerate(outer_to_inners.items()):
            cls = "col-boundary" if o_i < outer_count - 1 else ""
            cls_attr = f" class='{cls}'" if cls else ""
            row1_cells.append(
                f"<th colspan='{len(inners)}'{cls_attr}>{_esc(outer)}</th>"
            )
        row1 = "<tr>" + "".join(row1_cells) + "</tr>"

        row2_cells = []
        i = 0
        for _outer, inners in outer_to_inners.items():
            for inner in inners:
                cls = "col-boundary" if i in boundary_positions else ""
                cls_attr = f" class='{cls}'" if cls else ""
                row2_cells.append(f"<th{cls_attr}>{_esc(inner)}</th>")
                i += 1
        row2 = "<tr>" + "".join(row2_cells) + "</tr>"
        header_html = row1 + row2

    # ---- Body ------------------------------------------------------------
    total_cols = len(groups) + 1
    body_parts = []
    for _, row in df.iterrows():
        key = str(row["__key__"])
        if key.startswith("__SECTION__"):
            label = key.replace("__SECTION__", "")
            body_parts.append(
                f"<tr class='section'><td class='kpi' colspan='{total_cols}'>"
                f"{_esc(label)}</td></tr>"
            )
            continue

        # Cell type
        if "Churn" in key:
            cell_class = "churn"
        elif key.lstrip().startswith("%"):
            cell_class = "pct"
        else:
            cell_class = "count"

        # Important?
        is_important = any(tok in key for tok in _IMPORTANT_TOKENS)
        kpi_class = "kpi important" if is_important else "kpi"
        if is_important and cell_class == "count":
            cell_class += " important"

        cells_html = "".join(
            f"<td class='{cell_class}{(' col-boundary' if i in boundary_positions else '')}'>{_esc(row.get(g, ''))}</td>"
            for i, g in enumerate(groups)
        )
        body_parts.append(
            f"<tr><td class='{kpi_class}'>{_esc(key)}</td>{cells_html}</tr>"
        )

    body_html = "\n".join(body_parts)

    return (
        f"<div class='psp-scroll'>"
        f"<table class='psp-table'>"
        f"<thead>{header_html}</thead>"
        f"<tbody>{body_html}</tbody>"
        f"</table>"
        f"</div>"
    )


# Back-compat alias (some old call-sites might still expect it during refactor)
def style_table(df):
    return render_table_html(df)


# ---------------------------------------------------------------------------
# Executive Summary tab — fixed window: last completed week (S-1) vs S-2.
#
# Independent from sidebar date range and dimensions. Filters are NOT applied
# either (v1) — the table always reflects the full picture across the 7 fixed
# (conciergerie × PSP) pairs.
#
# Layout:
#   - Top: 5 cards (one per "company" — CT/Ray/RB/LM/NOV) with the S-1 alert
#     count (any alert except Order Insight, which fact_alert already filters
#     out) and the WoW evolution vs S-2.
#   - Bot: KPI table — rows = R0, CA Net, % Churn R0→R1, % Refund, % VAMP ;
#     cols = the 7 (conciergerie, PSP) pairs.
# ---------------------------------------------------------------------------

# Fixed (conciergerie, ms_default_psp, display label) pairs displayed in the
# KPI table. PSPs are LOWERCASE to match ms_default_psp values in BigQuery.
EXEC_PSP_PAIRS = [
    ("Reserv-Go", "trustpayment", "Trustpayment"),
    ("Book-Ici",  "trustpayment", "Trustpayment"),
    ("Resadexa",  "trustpayment", "Trustpayment"),
    ("Concimax",  "pixxles",      "Pixxles"),
    ("Jumpaide",  "pixxles",      "Pixxles"),
    ("Rapidoxy",  "pixxles",      "Pixxles"),
    ("Rezaflash", "nmi",          "NMI"),
]

# Company rollup for the top alert cards.
EXEC_COMPANIES = [
    ("CT",  ["Reserv-Go", "Book-Ici", "Resadexa"]),
    ("Ray", ["Jumpaide"]),
    ("RB",  ["Concimax"]),
    ("LM",  ["Rapidoxy"]),
    ("NOV", ["Rezaflash"]),
]


def exec_summary_sql() -> str:
    """Build SQL for the Executive Summary tab.

    Returns rows of (conciergerie, psp, bucket, metric, value):
      - conciergerie ∈ {Reserv-Go, Book-Ici, Resadexa, Rezaflash, Jumpaide,
                        Concimax, Rapidoxy}
      - psp = LOWER(ms_default_psp) (e.g. 'trustpayment', 'pixxles', 'nmi')
      - bucket ∈ {'s1' (current Month-to-Date), 's2' (previous month same window)}
      - metric ∈ {r0, tbb_count, r1_net_count, brut, refund_rev,
                  tx_succ_visa, alerts_visa}
      - value FLOAT64

    Window is fixed (CURRENT_DATE() server-side). Ignores sidebar
    date/dimensions/filters by design.

      - s1 = MTD courant : 1st of current month → yesterday
      - s2 = MTD M-1     : 1st of previous month → same day-of-month as s1_end

    KPIs derived in Python from these atoms (see render_exec_summary):
      - R0                = r0                            (customer-level count, not membership)
      - CA Net            = brut - refund_rev             (brut by t_date; refund by refunded_at_utc)
      - % Churn R0→R1 net = 1 - (r1_net_count / tbb_count)  (Booking ONLY)
      - % Refund          = refund_rev / brut             (CA-based, dates differentiated)
      - % VAMP Ratio      = alerts_visa / tx_succ_visa    (VISA only — VAMP = Visa Acquirer Monitoring Program)

    Date semantics:
      - r0           : by ms_date (signup date)
      - tbb_cohort   : by ms_trial_end (cohort that should have been billed in window)
      - brut         : by t_date (transaction date) for status='succeeded' tx (all cards)
      - refund_rev   : by refunded_at_utc (refund date), independent of t_date (all cards)
      - tx_succ_visa : by t_date (transaction date), VISA + DELTA only
      - alerts_visa  : by alerted_at (alert date), cardnetwork='Visa' only

    Card scope:
      - r0 / brut / refund_rev : all card brands (R0, CA, Refund are global metrics)
      - tx_succ_visa / alerts_visa : Visa only (DELTA = Visa Debit UK, same network)
    """
    # Conciergerie canonical mapping — same logic as CONCIERGERIE_EXPR_FM/FT
    # but inlined with the conciergerie names this tab cares about.
    def _conc(alias: str, col: str) -> str:
        return (
            f"CASE LOWER(REGEXP_REPLACE(COALESCE({alias}.{col}, ''), r' - magazine$', '')) "
            "  WHEN 'reserv-go'    THEN 'Reserv-Go' "
            "  WHEN 'book-ici'     THEN 'Book-Ici' "
            "  WHEN 'resadexa'     THEN 'Resadexa' "
            "  WHEN 'rezaflash'    THEN 'Rezaflash' "
            "  WHEN 'jumpaide.com' THEN 'Jumpaide' "
            "  WHEN 'concimax'     THEN 'Concimax' "
            "  WHEN 'rapidoxy'     THEN 'Rapidoxy' "
            "  ELSE NULL END"
        )

    fm_conc = _conc("fm", "brand")
    ft_conc = _conc("ft", "t_brand")
    t_conc  = _conc("t",  "t_brand")

    return f"""
WITH weeks_def AS (
  -- s1 = current Month-to-Date (1st of current month → yesterday)
  -- s2 = same window one month earlier (1st of prev month → same day-of-month
  --      as yesterday). BQ DATE_SUB(... INTERVAL 1 MONTH) clips to last valid
  --      day when the target month is shorter (e.g. Mar 31 - 1m → Feb 28/29).
  SELECT
    DATE_TRUNC(CURRENT_DATE(), MONTH)                                            AS s1_start,
    DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)                                     AS s1_end,
    DATE_SUB(DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 1 MONTH)                AS s2_start,
    DATE_SUB(DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY), INTERVAL 1 MONTH)         AS s2_end
),
fm_in_window AS (
  SELECT
    fm.membership_id,
    fm.customer_id,
    fm.brand_type,
    fm.ms_date,
    fm.ms_trial_end,
    fm.ms_default_psp AS psp,
    COALESCE(fm.ms_cancelled_during_trial, FALSE) AS cancelled_during_trial,
    {fm_conc} AS conciergerie
  FROM `eu-andy-marketing-raw.dashboard.fact_memberships` fm
  WHERE fm.ms_status NOT IN ('abandonned', 'processing')
    AND (
      (fm.ms_date       BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def))
      OR (fm.ms_trial_end BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def))
    )
),
r0 AS (
  -- R0 = nombre de CUSTOMERS uniques signés up dans la semaine (et pas
  -- de memberships : un customer qui a 2 subs au sein de la même semaine
  -- (Booking + Magazine ou re-signup) compte une seule fois).
  SELECT conciergerie, psp,
    CASE
      WHEN ms_date BETWEEN (SELECT s1_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def) THEN 's1'
      WHEN ms_date BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s2_end FROM weeks_def) THEN 's2'
    END AS bucket,
    'r0' AS metric,
    CAST(COUNT(DISTINCT customer_id) AS FLOAT64) AS value
  FROM fm_in_window
  WHERE conciergerie IS NOT NULL
    AND ms_date BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def)
  GROUP BY 1, 2, 3
),
tbb_cohort AS (
  -- Cohort for Churn R0→R1: memberships whose TrialEnd falls in s1/s2
  -- and that weren't cancelled during the trial (TBB = To Be Billed).
  -- BOOKING ONLY — the churn measured here is the conversion of the
  -- Booking sub from trial to billed. Magazine cross-sells have their
  -- own dynamic and shouldn't pollute this number.
  SELECT fm.conciergerie, fm.psp,
    CASE
      WHEN fm.ms_trial_end BETWEEN (SELECT s1_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def) THEN 's1'
      WHEN fm.ms_trial_end BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s2_end FROM weeks_def) THEN 's2'
    END AS bucket,
    fm.membership_id,
    MAX(IF(r1.membership_id IS NOT NULL, 1, 0)) AS has_r1_net
  FROM fm_in_window fm
  LEFT JOIN (
    SELECT DISTINCT membership_id
    FROM `eu-andy-marketing-raw.dashboard.fact_transactions`
    WHERE invoice_r_index = '1'
      AND transaction_status = 'succeeded'
      AND is_refunded = FALSE
      AND is_alerted = FALSE
      AND brand_type = 'Booking'
  ) r1 ON r1.membership_id = fm.membership_id
  WHERE fm.conciergerie IS NOT NULL
    AND fm.brand_type = 'Booking'
    AND fm.ms_trial_end BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def)
    AND NOT fm.cancelled_during_trial
  GROUP BY 1, 2, 3, 4
),
churn_agg AS (
  SELECT conciergerie, psp, bucket, 'tbb_count' AS metric,
    CAST(COUNT(*) AS FLOAT64) AS value
  FROM tbb_cohort GROUP BY 1, 2, 3
  UNION ALL
  SELECT conciergerie, psp, bucket, 'r1_net_count' AS metric,
    CAST(SUM(has_r1_net) AS FLOAT64) AS value
  FROM tbb_cohort GROUP BY 1, 2, 3
),
ft_in_window AS (
  -- Transactions by t_date (transaction date) — used for brut revenue and
  -- VAMP denominator. Includes ALL R indexes (R0 + R1..R4 + Cx + micros).
  SELECT
    ft.transaction_id,
    ft.t_date,
    ft.transaction_amount,
    ft.transaction_status,
    ft.invoice_r_index,
    ft.ms_default_psp AS psp,
    {ft_conc} AS conciergerie,
    CASE
      WHEN ft.t_date BETWEEN (SELECT s1_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def) THEN 's1'
      WHEN ft.t_date BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s2_end FROM weeks_def) THEN 's2'
    END AS bucket
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ft
  WHERE ft.t_date BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def)
),
refund_in_window AS (
  -- Refunds by refunded_at_utc (refund processing date) — independent of
  -- the original transaction date. A tx originated in week N-3 but refunded
  -- in week N-1 lands in N-1 here. transaction_amount = original tx amount
  -- (per Notion '€ Refund' convention).
  SELECT
    ft.transaction_id,
    ft.transaction_amount,
    ft.ms_default_psp AS psp,
    {ft_conc} AS conciergerie,
    CASE
      WHEN ft.refunded_at_utc BETWEEN (SELECT s1_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def) THEN 's1'
      WHEN ft.refunded_at_utc BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s2_end FROM weeks_def) THEN 's2'
    END AS bucket
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ft
  WHERE ft.is_refunded = TRUE
    AND ft.refunded_at_utc BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def)
),
ft_visa_in_window AS (
  -- Visa-only succeeded tx — used as VAMP denominator. Includes DELTA
  -- (Visa Debit UK, same network). All R indexes counted.
  SELECT ft.transaction_id, ft.t_date, ft.ms_default_psp AS psp,
    {ft_conc} AS conciergerie,
    CASE
      WHEN ft.t_date BETWEEN (SELECT s1_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def) THEN 's1'
      WHEN ft.t_date BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s2_end FROM weeks_def) THEN 's2'
    END AS bucket
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ft
  WHERE ft.t_date BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def)
    AND ft.transaction_status = 'succeeded'
    AND UPPER(ft.t_card_brand) IN ('VISA', 'DELTA')
),
tx_metrics AS (
  -- 'brut'         : € of succeeded tx by t_date (all card brands)
  -- 'refund_rev'   : € of refunds by refunded_at_utc (all card brands)
  -- 'tx_succ_visa' : # of succeeded Visa+Delta tx by t_date — VAMP denominator
  SELECT conciergerie, psp, bucket, 'brut' AS metric,
    SUM(CASE WHEN transaction_status='succeeded' THEN transaction_amount ELSE 0 END) AS value
  FROM ft_in_window WHERE conciergerie IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2,3
  UNION ALL
  SELECT conciergerie, psp, bucket, 'refund_rev' AS metric,
    SUM(transaction_amount) AS value
  FROM refund_in_window WHERE conciergerie IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2,3
  UNION ALL
  SELECT conciergerie, psp, bucket, 'tx_succ_visa' AS metric,
    CAST(COUNT(DISTINCT transaction_id) AS FLOAT64) AS value
  FROM ft_visa_in_window WHERE conciergerie IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2,3
),
alerts_join AS (
  -- Visa-only alerts. fact_alert is a VIEW that already excludes Order
  -- Insight + Order Insight 3.0. We further restrict to cardnetwork='Visa'
  -- because the cards + VAMP only care about Visa (VAMP = Visa Acquirer
  -- Monitoring Program). Joined to fact_transactions on transaction_id to
  -- recover t_brand / ms_default_psp / invoice_r_index.
  SELECT
    fa.transaction_id, fa.alerted_at,
    {t_conc} AS conciergerie,
    t.ms_default_psp AS psp,
    t.invoice_r_index,
    CASE
      WHEN fa.alerted_at BETWEEN (SELECT s1_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def) THEN 's1'
      WHEN fa.alerted_at BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s2_end FROM weeks_def) THEN 's2'
    END AS bucket
  FROM `eu-andy-marketing-raw.dashboard.fact_alert` fa
  JOIN `eu-andy-marketing-raw.dashboard.fact_transactions` t USING (transaction_id)
  WHERE fa.alerted_at BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def)
    AND UPPER(fa.cardnetwork) = 'VISA'
),
alerts_metrics AS (
  -- 'alerts_visa' = Visa alerts (R0 + Rx) by alerted_at. Used by:
  --   * the top "Alertes par société" cards (Visa only per user spec)
  --   * the VAMP Ratio numerator
  SELECT conciergerie, psp, bucket, 'alerts_visa' AS metric,
    CAST(COUNT(DISTINCT transaction_id) AS FLOAT64) AS value
  FROM alerts_join
  WHERE conciergerie IS NOT NULL AND bucket IS NOT NULL
  GROUP BY 1,2,3
)
SELECT * FROM r0
UNION ALL SELECT conciergerie, psp, bucket, metric, value FROM churn_agg
UNION ALL SELECT * FROM tx_metrics
UNION ALL SELECT * FROM alerts_metrics
"""


def _exec_period_bounds() -> tuple:
    """Compute (s1_start, s1_end, s2_start, s2_end) MTD windows for labelling.

      - s1 = MTD courant : 1st of current month → yesterday
      - s2 = MTD M-1     : 1st of previous month → same day-of-month as s1_end
                          (clipped to last day of prev month if needed)

    BQ uses the same logic via CURRENT_DATE() / DATE_SUB(... 1 MONTH) — both
    should match unless crossing a midnight boundary between server-call and
    label rendering."""
    today = date.today()
    s1_start = today.replace(day=1)
    s1_end = today - timedelta(days=1)
    # 1st of previous month
    if s1_start.month == 1:
        s2_start = date(s1_start.year - 1, 12, 1)
    else:
        s2_start = date(s1_start.year, s1_start.month - 1, 1)
    # Same day-of-month as s1_end in prev month (clipped if shorter)
    try:
        s2_end = s2_start.replace(day=s1_end.day)
    except ValueError:
        # Prev month has fewer days — fall back to last day of prev month.
        # (Last day = 1st of current month - 1 day.)
        s2_end = s1_start - timedelta(days=1)
    return s1_start, s1_end, s2_start, s2_end


def render_exec_summary(df: pd.DataFrame) -> str:
    """Render the Executive Summary as inline HTML (cards + KPI table)."""
    if df.empty:
        return "<div style='color:#64748b;padding:12px;'>Aucune donnée</div>"

    # Pivot to dict for O(1) lookup: data[(conc, psp, bucket)][metric] = value
    data: dict = {}
    for _, r in df.iterrows():
        key = (r["conciergerie"], r["psp"], r["bucket"])
        try:
            v = float(r["value"]) if pd.notna(r["value"]) else 0.0
        except (TypeError, ValueError):
            v = 0.0
        data.setdefault(key, {})[r["metric"]] = v

    def m(conc, psp, bucket, metric):
        return data.get((conc, psp, bucket), {}).get(metric, 0.0)

    # --- French number formatters ---
    def fr_int(v):
        return f"{int(round(v)):,}".replace(",", " ")

    def fr_pct(v):
        return f"{v*100:.1f}%".replace(".", ",")

    def fr_eur(v):
        if abs(v) >= 1000:
            return f"{v/1000:,.1f} K€".replace(",", " ").replace(".", ",")
        return f"{v:,.0f} €".replace(",", " ")

    def wow_pct(curr, prev):
        if prev is None or prev == 0 or curr is None:
            return None
        return (curr - prev) / prev

    def wow_html(pct, lower_is_better=False):
        if pct is None:
            return "<span class='exec-wow-neutral'>—</span>"
        sign = "+" if pct >= 0 else ""
        is_good = (pct < 0) if lower_is_better else (pct > 0)
        if pct == 0:
            cls = "exec-wow-neutral"
        else:
            cls = "exec-wow-good" if is_good else "exec-wow-bad"
        return f"<span class='{cls}'>{sign}{pct*100:.1f}%</span>".replace(".", ",")

    # --- Top: company cards (Visa alerts MTD, WoW vs MTD M-1) ---
    cards = []
    for company, conciergeries in EXEC_COMPANIES:
        s1 = sum(m(c, p, "s1", "alerts_visa") for (c, p, _l) in EXEC_PSP_PAIRS if c in conciergeries)
        s2 = sum(m(c, p, "s2", "alerts_visa") for (c, p, _l) in EXEC_PSP_PAIRS if c in conciergeries)
        delta = wow_html(wow_pct(s1, s2), lower_is_better=True)
        # Sub-label: list of conciergeries inside
        sub = " · ".join(conciergeries)
        cards.append(
            "<div class='exec-card'>"
            f"<div class='exec-card-head'>{company}</div>"
            f"<div class='exec-card-sub'>{sub}</div>"
            f"<div class='exec-card-value'>{fr_int(s1)}</div>"
            f"<div class='exec-card-foot'>alertes Visa MTD &middot; {delta} vs M-1</div>"
            "</div>"
        )
    cards_html = "<div class='exec-cards'>" + "".join(cards) + "</div>"

    # --- Bottom: KPI table ---
    header_cells = "".join(
        "<th>"
        f"<div class='exec-th-conc'>{c}</div>"
        f"<div class='exec-th-psp'>({lbl})</div>"
        "</th>"
        for (c, p, lbl) in EXEC_PSP_PAIRS
    )

    def kpi_row(label, fmt, compute_fn, lower_is_better=False):
        cells = []
        for (c, p, _lbl) in EXEC_PSP_PAIRS:
            v1 = compute_fn(c, p, "s1")
            v2 = compute_fn(c, p, "s2")
            value_str = fmt(v1) if v1 is not None else "—"
            delta_str = wow_html(wow_pct(v1, v2) if (v1 is not None and v2 is not None) else None,
                                 lower_is_better=lower_is_better)
            cells.append(
                f"<td><div class='exec-cell-val'>{value_str}</div>"
                f"<div class='exec-cell-wow'>{delta_str}</div></td>"
            )
        return f"<tr><td class='exec-kpi-label'>{label}</td>{''.join(cells)}</tr>"

    def churn_fn(c, p, b):
        tbb = m(c, p, b, "tbb_count")
        r1  = m(c, p, b, "r1_net_count")
        return None if tbb == 0 else 1 - (r1 / tbb)

    def refund_fn(c, p, b):
        # CA-based refund rate: € refunded (by refund_at_utc) / € brut (by t_date)
        brut = m(c, p, b, "brut")
        ref  = m(c, p, b, "refund_rev")
        return None if brut == 0 else ref / brut

    def vamp_fn(c, p, b):
        # VAMP Visa only: alerts (by alerted_at, cardnetwork='Visa')
        #               / succeeded tx (by t_date, t_card_brand IN VISA, DELTA)
        # R0 included on both sides. VAMP = Visa Acquirer Monitoring Program.
        denom = m(c, p, b, "tx_succ_visa")
        num   = m(c, p, b, "alerts_visa")
        return None if denom == 0 else num / denom

    rows_html = "".join([
        kpi_row("# R0 (customers)",        fr_int, lambda c, p, b: m(c, p, b, "r0"),                                lower_is_better=False),
        kpi_row("€ CA Net",                fr_eur, lambda c, p, b: m(c, p, b, "brut") - m(c, p, b, "refund_rev"),    lower_is_better=False),
        kpi_row("% Churn R0→R1 (Booking)", fr_pct, churn_fn,                                                          lower_is_better=True),
        kpi_row("% Refund (CA)",           fr_pct, refund_fn,                                                         lower_is_better=True),
        kpi_row("% VAMP Ratio (Visa)",     fr_pct, vamp_fn,                                                           lower_is_better=True),
    ])

    table_html = (
        "<table class='exec-table'>"
        f"<thead><tr><th class='exec-kpi-label'>KPI</th>{header_cells}</tr></thead>"
        f"<tbody>{rows_html}</tbody>"
        "</table>"
    )

    # Period label (client-side, same logic as BQ — matches unless tomorrow rolled over)
    s1_start, s1_end, s2_start, s2_end = _exec_period_bounds()
    period_label = (
        f"<b>MTD courant :</b> {s1_start.strftime('%d/%m')} → {s1_end.strftime('%d/%m')} "
        f"&middot; <b>MTD M-1 :</b> {s2_start.strftime('%d/%m')} → {s2_end.strftime('%d/%m')}"
    )

    css = """
    <style>
      .exec-summary { font-size: 14px; }
      .exec-period {
        background: #fef3c7;
        border-left: 4px solid #f59e0b;
        padding: 8px 12px;
        border-radius: 4px;
        margin-bottom: 16px;
        font-size: 13px;
      }
      .exec-section {
        font-size: 16px;
        font-weight: 700;
        color: #0f172a;
        margin: 20px 0 10px 0;
        padding-bottom: 6px;
        border-bottom: 2px solid #e2e8f0;
      }
      .exec-cards {
        display: grid;
        grid-template-columns: repeat(5, 1fr);
        gap: 12px;
        margin-bottom: 8px;
      }
      .exec-card {
        background: white;
        border: 1px solid #cbd5e1;
        border-radius: 8px;
        padding: 14px 16px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.04);
      }
      .exec-card-head {
        font-size: 12px;
        font-weight: 700;
        text-transform: uppercase;
        color: #475569;
        letter-spacing: 0.05em;
      }
      .exec-card-sub {
        font-size: 11px;
        color: #94a3b8;
        margin-top: 2px;
        margin-bottom: 8px;
      }
      .exec-card-value {
        font-size: 30px;
        font-weight: 700;
        color: #0f172a;
        line-height: 1.1;
      }
      .exec-card-foot {
        font-size: 12px;
        color: #64748b;
        margin-top: 6px;
      }
      .exec-table {
        border-collapse: separate;
        border-spacing: 0;
        width: 100%;
        background: white;
        border: 1px solid #cbd5e1;
        border-radius: 8px;
        overflow: hidden;
        font-size: 13px;
      }
      .exec-table thead th {
        background: #0f172a;
        color: white;
        padding: 10px 12px;
        text-align: center;
        font-weight: 600;
        border-bottom: 2px solid #1e293b;
      }
      .exec-table thead th.exec-kpi-label {
        text-align: left;
        background: #1e293b;
      }
      .exec-th-conc { font-size: 13px; font-weight: 700; }
      .exec-th-psp  { font-size: 11px; opacity: 0.75; font-weight: 400; margin-top: 2px; }
      .exec-table tbody td {
        padding: 10px 12px;
        text-align: center;
        border-bottom: 1px solid #e2e8f0;
        vertical-align: middle;
      }
      .exec-table tbody tr:last-child td { border-bottom: none; }
      .exec-table tbody tr:nth-child(even) td { background: #f8fafc; }
      .exec-kpi-label {
        text-align: left !important;
        font-weight: 700;
        color: #0f172a;
        background: #f1f5f9 !important;
        position: sticky;
        left: 0;
      }
      .exec-cell-val { font-size: 14px; font-weight: 600; color: #0f172a; }
      .exec-cell-wow { font-size: 11px; margin-top: 3px; }
      .exec-wow-good    { color: #059669; font-weight: 600; }
      .exec-wow-bad     { color: #dc2626; font-weight: 600; }
      .exec-wow-neutral { color: #94a3b8; }
    </style>
    """

    return (
        css
        + "<div class='exec-summary'>"
        + f"<div class='exec-period'>📅 {period_label} (Month-to-Date · indépendant des filtres de la sidebar)</div>"
        + "<h3 class='exec-section'>Alertes Visa par société — MTD (hors Order Insight)</h3>"
        + cards_html
        + "<h3 class='exec-section'>KPI MTD par Conciergerie × PSP</h3>"
        + table_html
        + "</div>"
    )


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("📊 Weekly PSP Report")
st.caption("Funnel + VAMP — basé sur le skill `weekly-psp-report` (v3) — dimensions dynamiques.")

# Sidebar: date range
st.sidebar.header("Période")
_today = date.today()
_default_end = _today - timedelta(days=_today.weekday() + 1)  # last Sunday
_default_start = _default_end - timedelta(days=10 * 7 - 1)    # 10 weeks before
_date_range = st.sidebar.date_input(
    "Plage de cohortes",
    value=(_default_start, _default_end),
    min_value=_today - timedelta(days=365 * 2),
    max_value=_today,
    help="Les semaines complètes (lundi → dimanche) qui intersectent la plage.",
    key="date_range",
)
if isinstance(_date_range, tuple) and len(_date_range) == 2:
    _start_d, _end_d = _date_range
else:
    _start_d, _end_d = _default_start, _default_end
_picked_start, _picked_end = _start_d, _end_d  # remembered for use after dim picker

st.sidebar.divider()

# Sidebar: dimensions (column-axis splits) — two independent dropdowns.
st.sidebar.header("Dimensions")
_NONE_LABEL = "— aucune —"
_all_dim_labels = [d[1] for d in DIMENSION_DIMS]
_dim_options = [_NONE_LABEL] + _all_dim_labels

dim1_label = st.sidebar.selectbox(
    "Dimension 1 (extérieure)",
    options=_dim_options,
    index=_dim_options.index("Date (semaine)"),
    help="Niveau extérieur du split de colonnes.",
    key="dim1_selector",
)
dim2_label = st.sidebar.selectbox(
    "Dimension 2 (intérieure)",
    options=_dim_options,
    index=0,
    help="Niveau intérieur du split. Choisis « aucune » pour ne pas empiler.",
    key="dim2_selector",
)

selected_dim_labels = []
if dim1_label != _NONE_LABEL:
    selected_dim_labels.append(dim1_label)
if dim2_label != _NONE_LABEL and dim2_label != dim1_label:
    selected_dim_labels.append(dim2_label)

dims = selected_dims(selected_dim_labels)
# Mutually-exclusive date granularities — keep only the first date dim.
_seen_date = False
_dims_filtered = []
for d in dims:
    if d[0] in _DATE_DIM_KEYS:
        if _seen_date:
            continue
        _seen_date = True
    _dims_filtered.append(d)
dims = _dims_filtered

# Now compute the periods list using the granularity that the user picked.
_granularity = date_dim_key(dims) or "date_week"
weeks_list = periods_in_range(_picked_start, _picked_end, _granularity)
_period_word = {"date_day": "jour", "date_week": "semaine", "date_month": "mois"}[_granularity]
st.sidebar.caption(f"{len(weeks_list)} {_period_word}{'s' if len(weeks_list) > 1 else ''} sélectionné{'s' if len(weeks_list) > 1 else ''}")

st.sidebar.divider()

# Sidebar: filters
# Filter options are scoped to the user-selected date range so the dropdown
# proposes exactly the values that exist in the data the user is looking at.
st.sidebar.header("Filtres")
with st.spinner("Chargement des options de filtre…"):
    try:
        opts_df = run_query(filter_options_sql(_picked_start, _picked_end))
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
tab_exec, tab_b, tab_m, tab_vc, tab_vd = st.tabs(
    ["Executive Summary", "Funnel Booking", "Funnel Magazine", "VAMP Cohort", "VAMP Date"]
)

with tab_exec:
    # Independent from sidebar — always shows last completed week (S-1)
    # vs the week before (S-2). Filters/dimensions/date range NOT applied.
    with st.spinner("Executive Summary…"):
        try:
            df_exec = run_query(exec_summary_sql())
            st.markdown(render_exec_summary(df_exec), unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Erreur Executive Summary : {e}")

with tab_b:
    with st.spinner("Funnel Booking…"):
        try:
            df = run_query(funnel_sql("Booking", filters, dims, weeks_list))
            table = build_funnel_table(df, "Booking", dims)
            st.markdown(render_table_html(table), unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Erreur Funnel Booking : {e}")

with tab_m:
    with st.spinner("Funnel Magazine…"):
        try:
            df = run_query(funnel_sql("Magazine", filters, dims, weeks_list))
            table = build_funnel_table(df, "Magazine", dims)
            st.markdown(render_table_html(table), unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Erreur Funnel Magazine : {e}")

with tab_vc:
    with st.spinner("VAMP Cohort…"):
        try:
            df = run_query(vamp_cohort_sql(filters, dims, weeks_list))
            table = build_vamp_table(df, dims)
            st.markdown(render_table_html(table), unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Erreur VAMP Cohort : {e}")

with tab_vd:
    with st.spinner("VAMP Date…"):
        try:
            df = run_query(vamp_date_sql(filters, dims, weeks_list))
            table = build_vamp_table(df, dims)
            st.markdown(render_table_html(table), unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Erreur VAMP Date : {e}")
