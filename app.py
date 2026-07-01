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
    ("mid",             "MID",             "PLACEHOLDER",        "PLACEHOLDER"),
    ("brand_psp",       "Conciergerie × PSP", "RAW:bpsp.brand_psp", "RAW:bpsp.brand_psp"),
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


# ============================================================================
# LTV SIMULATOR — constants + helpers
# ----------------------------------------------------------------------------
# Projection LTV pour chaque cohorte affichée dans le Funnel Booking.
#   - R observés (R1-R4) : data réelle de la cohorte
#   - R au-delà : projection avec decay factor hardcodé depuis cohortes Déc 2025
#   - 2 chains indépendantes (BRUT et NET), même logique
#
# Decay factor = ratio churn(R_n) / churn(R_n-1) entre cycles successifs,
# calculé sur cohortes méthodo canonique (c.CreatedAtUtc + ms_date in Déc 2025
# + ms_status NOT IN abandonned/processing/paused + brand_type=Booking).
#
# Au-delà du dernier R observé en référence : decay = 0.9 constant.
# ============================================================================

# Decay €19 bi-mensuel (cohort Déc 2025 TP EUR, R1-R11 observés)
LTV_DECAY_BIM_NET = {2: 0.7013, 3: 0.8071, 4: 0.7722, 5: 0.6991, 6: 0.8238,
                     7: 1.1581, 8: 0.8333, 9: 1.3923, 10: 1.0020, 11: 0.7521}
LTV_DECAY_BIM_BRUT = {2: 0.9565, 3: 0.7651, 4: 0.8067, 5: 0.6829, 6: 0.7954,
                      7: 1.2364, 8: 0.7581, 9: 1.4641, 10: 0.9032, 11: 0.8273}

# Decay €49 mensuel (cohort Déc 2025 TP EUR, R1-R5 observés)
LTV_DECAY_MENS_NET = {2: 0.6015, 3: 0.9660, 4: 0.7541, 5: 1.0290}
LTV_DECAY_MENS_BRUT = {2: 0.8447, 3: 1.0200, 4: 0.6655, 5: 1.1116}

# Mapping bucket prix → (cycle_days, horizon, amount_eur, last_obs_ref, decay_brut, decay_net)
LTV_PRICE_CONFIG = {
    "19€ bi-mensuel": (14, 26, 19.99, 11, LTV_DECAY_BIM_BRUT, LTV_DECAY_BIM_NET),
    "49€ mensuel":    (30, 12, 49.99, 5,  LTV_DECAY_MENS_BRUT, LTV_DECAY_MENS_NET),
    "69€ mensuel":    (30, 12, 69.99, 5,  LTV_DECAY_MENS_BRUT, LTV_DECAY_MENS_NET),  # mirror €49
}
LTV_TRIAL_DAYS = 3
LTV_DEFAULT_DECAY = 0.9  # appliqué au-delà du dernier R observé en référence

# Seuil de masquage des cohortes trop petites (colonnes du tableau + points de
# courbe). Les cohortes avec moins de N R0 succeeded sont masquées (manque de
# significativité statistique).
MIN_R0_DISPLAY = 200


def _dynamic_max_rx_observed(weeks_list, today=None,
                              min_cycle_days: int = 14,
                              default: int = 4, hard_cap: int = 26) -> int:
    """Calcule max_rx_observed à demander au funnel_sql selon la fenêtre.

    Pour le tab LTV (et le simulator LTV dans Funnel Booking), on a besoin
    d'observer tous les R où la cohorte est mature (pour calculer ARPU réel +
    démarrer le decay au bon endroit).

    Cycle de référence = 14j (bimensuel €19, le plus court = + de R observables
    sur la même fenêtre). Si la cohorte est en €49 (cycle 30j) on sur-estime
    (= demande plus de R que strictement nécessaire) sans impact correctness.

    weeks_list[0] = début de la plus ancienne cohorte de la fenêtre.
    """
    if not weeks_list:
        return default
    today = today or date.today()
    earliest_start = min(weeks_list)
    days_since = (today - earliest_start).days
    r_max = 1 + (days_since - LTV_TRIAL_DAYS) // min_cycle_days
    return max(default, min(hard_cap, r_max))


def _ltv_fmt_eur(amount) -> str:
    """Format FR : 1 234,56 €."""
    if amount is None:
        return "—"
    s = f"{amount:,.2f}"
    s = s.replace(",", " ").replace(".", ",")  # NBSP entre milliers
    return f"{s} €"


def _ltv_get_price_from_group(group_key: tuple, dims: list):
    """Extract price bucket from group_key if 'price_booking' is a dim.
    Returns the bucket string ('19€ bi-mensuel' etc.) or None."""
    if not dims:
        return None
    for i, d in enumerate(dims):
        if d[0] == "price_booking" and i < len(group_key):
            value = group_key[i]
            if value in LTV_PRICE_CONFIG:
                return value
    return None


def _ltv_cohort_end_date(group_sort_key_tuple: tuple, dims: list,
                          picked_end_date: date) -> date:
    """Determine the END date of the cohort window (= LATEST user signup).

    Pour le calcul de maturité dans _ltv_compute : le LATEST user de la cohorte
    est le dernier inscrit. Si la sidebar plafonne la fenêtre (picked_end), on
    cap aussi : le LATEST user de la cohorte n'est pas au-delà de picked_end.

    Ex: cohorte juin (1-30 juin) avec sidebar picked_end=25 juin → LATEST user
    est du 25 juin (pas du 30) → mature R1 le 28 juin → today=30 juin OK.

    - Si dim date : end = period_end (week/month) capped par picked_end
    - Sinon : picked_end direct (cohort spans entire window)
    """
    if dims:
        for i, d in enumerate(dims):
            if d[0] in _DATE_DIM_KEYS and i < len(group_sort_key_tuple):
                ws_str = group_sort_key_tuple[i]
                try:
                    ws = date.fromisoformat(str(ws_str))
                except (ValueError, TypeError):
                    return picked_end_date
                if d[0] == "date_day":
                    period_end = ws
                elif d[0] == "date_week":
                    period_end = ws + timedelta(days=6)
                elif d[0] == "date_month":
                    if ws.month == 12:
                        next_month = date(ws.year + 1, 1, 1)
                    else:
                        next_month = date(ws.year, ws.month + 1, 1)
                    period_end = next_month - timedelta(days=1)
                else:
                    period_end = picked_end_date
                # Cap par sidebar picked_end (la cohorte ne va pas au-delà
                # de la fin de fenêtre sélectionnée par l'user)
                return min(period_end, picked_end_date)
    return picked_end_date


def _ltv_compute(group_data: dict, price_bucket: str,
                  cohort_end_date: date, today: date,
                  r_max_obs_override: int = None) -> dict:
    """Compute ARPU + LTV brut/net for one cohort group.

    Returns dict with keys arpu_brut_eur, arpu_net_eur, ltv_brut_eur,
    ltv_net_eur, r_max_obs, or None if not computable.

    Maturité = basée sur le LATEST user de la cohorte (= cohort_end_date).
    cohort_end_date est déjà cappé par picked_end côté caller (_ltv_cohort_end_date)
    pour ne pas dépasser la fenêtre sidebar. Ex : cohorte juin avec
    picked_end=25 juin → cohort_end_date=25 juin → R1 mature le 28 juin.

    r_max_obs_override : si fourni, plafonne r_max_obs à cette valeur. Sert au
    calcul "R1 only" (passer 1) qui simule "qu'aurait été la LTV en ne se
    basant que sur R1 observé + projection R2+ via decay" — utile pour comparer
    avec la version mature (qui observe R1..r_max_obs_natural).
    """
    if price_bucket not in LTV_PRICE_CONFIG:
        return None
    cycle_days, horizon, amount, last_obs_ref, decay_brut, decay_net = \
        LTV_PRICE_CONFIG[price_bucket]

    r0 = group_data.get("r0_succeeded", 0)
    if r0 == 0:
        return None

    # Max R observable basé sur LATEST user (signup = cohort_end_date).
    # Rn du latest user = cohort_end + trial_days + (n-1) * cycle_days
    # Rn obs iff cohort_end + trial_days + (n-1)*cycle_days <= today
    # => n <= 1 + (today - cohort_end - trial_days) / cycle_days
    days_since_end = (today - cohort_end_date).days
    if days_since_end < LTV_TRIAL_DAYS:
        return None  # trial still in flight for latest user
    r_max_obs = 1 + (days_since_end - LTV_TRIAL_DAYS) // cycle_days
    # funnel_sql now returns R1-R{max_rx_observed} (dynamique côté tab LTV).
    # On cap r_max_obs par le nombre réel de R disponibles dans `rx` (sinon
    # obs_brut[n]=0 pour les R non scannés et la projection part dans le mur).
    rx_keys = group_data.get("rx", {}).keys()
    rx_available_max = max((int(k) for k in rx_keys if k.isdigit()), default=0)
    r_max_obs = min(r_max_obs, horizon, rx_available_max if rx_available_max > 0 else 1)
    if r_max_obs_override is not None:
        r_max_obs = min(r_max_obs, max(1, r_max_obs_override))
    if r_max_obs < 1:
        return None

    # Observed retention (BRUT + NET) per R0
    rx = group_data.get("rx", {})
    obs_brut = {}
    obs_net = {}
    for n in range(1, r_max_obs + 1):
        d = rx.get(str(n), {})
        succ_u = d.get("succ_u", 0)
        refund_u = d.get("refund_u", 0)
        obs_brut[n] = succ_u / r0
        obs_net[n] = max(succ_u - refund_u, 0) / r0

    # ARPU = realized revenue per R0 (over observed R only)
    arpu_brut_eur = sum(obs_brut.values()) * amount
    arpu_net_eur = sum(obs_net.values()) * amount

    # Project R_max_obs+1 .. horizon using decay factors
    # Starting churns = last observed cycle churn
    proj_brut = dict(obs_brut)
    proj_net = dict(obs_net)
    last_n = r_max_obs
    if last_n == 1:
        prev_churn_brut = 1 - obs_brut[1]
        prev_churn_net = 1 - obs_net[1]
    else:
        # Use ratios for last cycle churn
        prev_b = obs_brut[last_n - 1]
        prev_n = obs_net[last_n - 1]
        prev_churn_brut = 1 - (obs_brut[last_n] / prev_b) if prev_b > 0 else 0
        prev_churn_net = 1 - (obs_net[last_n] / prev_n) if prev_n > 0 else 0
    cur_brut = obs_brut[last_n]
    cur_net = obs_net[last_n]
    for n in range(last_n + 1, horizon + 1):
        d_b = decay_brut.get(n, LTV_DEFAULT_DECAY) if n <= last_obs_ref \
              else LTV_DEFAULT_DECAY
        d_n = decay_net.get(n, LTV_DEFAULT_DECAY) if n <= last_obs_ref \
              else LTV_DEFAULT_DECAY
        this_churn_brut = max(min(prev_churn_brut * d_b, 1.0), 0.0)
        this_churn_net = max(min(prev_churn_net * d_n, 1.0), 0.0)
        cur_brut = cur_brut * (1 - this_churn_brut)
        cur_net = cur_net * (1 - this_churn_net)
        proj_brut[n] = cur_brut
        proj_net[n] = cur_net
        prev_churn_brut = this_churn_brut
        prev_churn_net = this_churn_net

    ltv_brut_eur = sum(proj_brut.values()) * amount
    ltv_net_eur = sum(proj_net.values()) * amount

    return {
        "arpu_brut_eur": arpu_brut_eur,
        "arpu_net_eur": arpu_net_eur,
        "ltv_brut_eur": ltv_brut_eur,
        "ltv_net_eur": ltv_net_eur,
        "r_max_obs": r_max_obs,
    }


# Brand naming convention :
#   <Conciergerie>                              → Booking sub, default MID
#   <conciergerie> - magazine                   → Magazine cross-sell, default MID
#   <Conciergerie> - <MID>                      → Booking sub, specific MID
#   <conciergerie> - magazine - <MID>           → Magazine cross-sell, specific MID
#
# Examples observed in fact_memberships :
#   "Reserv-Go"                       → Reserv-Go × default
#   "Reserv-go - magazine"            → Reserv-Go × default × magazine variant
#   "Rezaflash"                       → Rezaflash × default MID (= EMS)
#   "Rezaflash - Kadima"              → Rezaflash × Kadima MID
#   "Rezaflash - magazine - Kadima"   → Rezaflash × Kadima × magazine variant
#   "Rapidoxy - LaBanquePostale"      → Rapidoxy × LaBanquePostale MID
#
# Two derived dims :
#   - CONCIERGERIE = first segment before ' - ' (collapses magazine + MID).
#   - MID          = brand with ' - magazine' stripped anywhere (keeps the
#                    MID suffix). Special-cased for Rezaflash default → EMS
#                    per business convention (default Rezaflash = EMS MID).
def _conciergerie_expr(brand_col: str) -> str:
    """Canonical conciergerie name = first segment of `brand` before ` - `.
    Anything unknown → NULL (will be excluded from filters/dims via
    `COALESCE(... , '')` upstream)."""
    return (
        f"CASE LOWER(SPLIT(COALESCE({{alias}}.{brand_col}, ''), ' - ')[OFFSET(0)]) "
        "  WHEN 'reserv-go'    THEN 'Reserv-Go' "
        "  WHEN 'book-ici'     THEN 'Book-Ici' "
        "  WHEN 'rezaflash'    THEN 'Rezaflash' "
        "  WHEN 'resadexa'     THEN 'Resadexa' "
        "  WHEN 'jumpaide.com' THEN 'Jumpaide' "
        "  WHEN 'concimax'     THEN 'Concimax' "
        "  WHEN 'rapidoxy'     THEN 'Rapidoxy' "
        "  ELSE NULL "  # exclut Concicast (legacy, plus de volume) + helpprio
        "END"
    )


def _mid_expr(brand_col: str) -> str:
    """MID = conciergerie canonical + suffix MID (if any).

    Strip ' - magazine' anywhere in the string (handles all 4 brand shapes),
    then map known prefixes to canonical conciergerie name + keep the MID
    suffix. Rezaflash default brand (no suffix) → 'Rezaflash EMS' per
    business convention (default MID at Novalane = EMS)."""
    cleaned = f"REGEXP_REPLACE(COALESCE({{alias}}.{brand_col}, ''), r' - magazine', '')"
    return (
        f"CASE LOWER({cleaned}) "
        "  WHEN 'reserv-go'                  THEN 'Reserv-Go' "
        "  WHEN 'book-ici'                   THEN 'Book-Ici' "
        "  WHEN 'resadexa'                   THEN 'Resadexa' "
        "  WHEN 'rezaflash'                  THEN 'Rezaflash EMS' "
        "  WHEN 'rezaflash - kadima'         THEN 'Rezaflash Kadima' "
        "  WHEN 'jumpaide.com'               THEN 'Jumpaide' "
        "  WHEN 'concimax'                   THEN 'Concimax' "
        "  WHEN 'rapidoxy'                   THEN 'Rapidoxy' "
        "  WHEN 'rapidoxy - labanquepostale' THEN 'Rapidoxy LaBanquePostale' "
        "  ELSE NULL "
        "END"
    )


CONCIERGERIE_EXPR_FM = _conciergerie_expr("brand")
CONCIERGERIE_EXPR_FT = _conciergerie_expr("t_brand")
MID_EXPR_FM = _mid_expr("brand")
MID_EXPR_FT = _mid_expr("t_brand")

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
    ("mid",            "MID",                     MID_EXPR_FM,                   MID_EXPR_FT),
    ("brand_psp",      "Conciergerie × PSP",      "RAW:bpsp.brand_psp",          "RAW:bpsp.brand_psp"),
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
    if k == "mid":
        return (MID_EXPR_FM, MID_EXPR_FT)
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


def weeks_cte_sql(weeks_list: List[date], granularity: str = "date_week",
                   picked_end: date = None) -> str:
    """Build the WITH weeks / window_bounds CTE from a list of period-start dates.
    Despite the legacy name, this works for any granularity (day/week/month) —
    we keep the CTE & column names as `weeks` / `week_start` / `week_label` for
    back-compat with the downstream SQL.

    `window_bounds` covers from the first period to the end of the last period
    (last day for daily, last day-of-week for weekly, last day-of-month for monthly).

    Si `picked_end` est fourni, ws_max est CAPPÉ par cette date. Sert quand la
    sidebar plafonne la fenêtre avant la fin de la dernière période (ex :
    granularité mois, fenêtre jusqu'au 25 juin → la cohorte juin ne doit
    contenir que les users 1-25 juin, pas 1-30 juin, sinon les R1 pas encore
    matures gonflent artificiellement le churn R0/R1).
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
    # Cap par picked_end si fourni (cf. docstring)
    if picked_end is not None:
        ws_max_expr = f"LEAST({ws_max_expr}, DATE '{picked_end.isoformat()}')"
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
  -- Univers = customers ayant une TRANSACTION dans la fenêtre (pas signup dans
  -- la fenêtre). Évite de perdre les transacs récurrentes des cohortes plus
  -- anciennes sur les onglets en date de transaction (VAMP). On évalue ensuite
  -- le filtre sur TOUTES les memberships du customer.
  SELECT fm.customer_email
  FROM `eu-andy-marketing-raw.dashboard.fact_memberships` fm
  WHERE fm.customer_email IN (
    SELECT ftw.customer_email
    FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ftw
    CROSS JOIN window_bounds wb_cp
    WHERE ftw.t_date BETWEEN wb_cp.ws_min AND wb_cp.ws_max
  )
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
        if col_spec.startswith("RAW:"):
            target = col_spec[4:]
        elif "{alias}" in col_spec:
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
  -- Borné sur la fenêtre via ms_date (partition prune) : pour les cohortes de
  -- la fenêtre, le bucket prix vient nécessairement d'une membership dans la
  -- fenêtre. Sans cette borne on scanne tout l'historique de fact_memberships
  -- pour chaque customer = timeout sur les fenêtres larges.
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
  WHERE fm.ms_date BETWEEN wb_cp_price.ws_min AND wb_cp_price.ws_max
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
# brand_psp — dimension/filtre "Conciergerie × PSP réel".
# Doc Notion "Identifier le couple PSP x Brand (Warning NMI)". Niveau membership.
# PSP réel = ms_default_psp hors NMI ; pour NMI = MidId de la transaction R0
# (silver_sgw.stg_transactions) mappé via le référentiel. Pattern = customer_price :
# CTE brand_psp_map (membership_id -> brand_psp), LEFT JOIN, exposé en 'RAW:'.
# ---------------------------------------------------------------------------
# Subquery : MidId de la transaction R0 NMI par membership (référentiel doc).
# BORNÉE par t_date (la fenêtre) -> indispensable pour la perf (sinon scan de
# toute la table de transactions). date_filter et extra_from sont fournis par
# l'appelant pour borner sur window_bounds (funnel/vamp) ou des dates explicites.
def _r0_mid_subquery(date_filter: str, extra_from: str = "") -> str:
    return (
        "SELECT f.membership_id, ANY_VALUE(s.MidId) AS MidId\n"
        "    FROM `eu-andy-marketing-raw.dashboard.fact_transactions` f\n"
        "    JOIN `eu-andy-marketing-raw.silver_sgw.stg_transactions` s ON f.transaction_id = s.Id\n"
        f"    {extra_from}\n"
        "    WHERE f.t_psp_name = 'nmi' AND f.invoice_r_index = '0'\n"
        f"    {date_filter}\n"
        "    GROUP BY f.membership_id"
    )


def _brand_psp_concat(brand_col: str, psp_col: str, mid_alias: str) -> str:
    """Expression 'Conciergerie - PSP réel' (doc Notion 'PSP x Brand / Warning NMI').
    PSP réel = ms_default_psp hors NMI ; sinon mappe le MidId du R0. Le référentiel
    MidId est centralisé ICI (à maintenir, cf. doc)."""
    conc = (
        f"CASE LOWER(TRIM(SPLIT(COALESCE({brand_col}, ''), ' - ')[OFFSET(0)])) "
        "WHEN 'rezaflash' THEN 'Rezaflash' WHEN 'reserv-go' THEN 'Reserv-Go' "
        "WHEN 'book-ici' THEN 'Book-Ici' WHEN 'resadexa' THEN 'Resadexa' "
        "WHEN 'concimax' THEN 'Concimax' WHEN 'jumpaide.com' THEN 'jumpaide.com' "
        "WHEN 'rapidoxy' THEN 'Rapidoxy' WHEN 'helpprio.com' THEN 'helpprio.com' "
        "WHEN 'concicast' THEN 'Concicast' "
        f"ELSE TRIM(SPLIT(COALESCE({brand_col}, ''), ' - ')[OFFSET(0)]) END"
    )
    psp = (
        f"CASE WHEN {psp_col} <> 'nmi' THEN {psp_col} "
        f"WHEN {mid_alias}.MidId = '688b5f4e-4f33-4b16-b2c7-6c601ba15306' THEN 'EMS' "
        f"WHEN {mid_alias}.MidId IN ('5f915cec-f0b3-40e9-9908-b3590b791448', "
        f"'f6130732-c577-4d1d-9ab9-802900b478a0') THEN 'Kadima' "  # tsyskadimaems = Kadima
        f"WHEN {mid_alias}.MidId = '4a7af99e-20b3-48b3-8e93-6fc39f8012b0' THEN 'Cliq' "
        f"WHEN {mid_alias}.MidId = '9cf8e38c-c719-4d33-a1e3-aaa72cf88cdd' THEN 'CASH' "
        f"ELSE 'EMS' END"  # MID NMI non résolu (R0 sans tx, etc.) = EMS (cf. ELA)
    )
    return f"CONCAT({conc}, ' - ', {psp})"


def _needs_brand_psp(dims: list, filters: dict) -> bool:
    return any(d[0] == "brand_psp" for d in dims) or bool(filters.get("brand_psp"))


def brand_psp_cte(dims: list, filters: dict) -> str:
    """CTE brand_psp_map (membership_id -> brand_psp), restreint à la fenêtre.
    Renvoie ',\\nbrand_psp_map AS (...)' ; '' si brand_psp ni dim ni filtre.
    Dépend de window_bounds -> émettre APRÈS weeks_cte / customer_price."""
    if not _needs_brand_psp(dims, filters):
        return ""
    expr = _brand_psp_concat("m.brand", "m.ms_default_psp", "r")
    # Univers = memberships ayant une TRANSACTION dans la fenêtre (toute date de
    # signup) — évite de perdre les transacs récurrentes des cohortes anciennes
    # sur les onglets en date de transaction (VAMP Date). Le MidId du R0 est
    # résolu pour CES memberships, quelle que soit la date du R0 (sinon un vieux
    # NMI récurrent tomberait dans le fallback EMS).
    return f""",
brand_psp_map AS (
  SELECT m.membership_id, {expr} AS brand_psp
  FROM `eu-andy-marketing-raw.dashboard.fact_memberships` m
  LEFT JOIN (
    SELECT f.membership_id, ANY_VALUE(s.MidId) AS MidId
    FROM `eu-andy-marketing-raw.dashboard.fact_transactions` f
    JOIN `eu-andy-marketing-raw.silver_sgw.stg_transactions` s ON f.transaction_id = s.Id
    WHERE f.t_psp_name = 'nmi' AND f.invoice_r_index = '0'
      AND f.membership_id IN (
        SELECT f2.membership_id
        FROM `eu-andy-marketing-raw.dashboard.fact_transactions` f2
        CROSS JOIN window_bounds wb_r0
        WHERE f2.t_date BETWEEN wb_r0.ws_min AND wb_r0.ws_max
      )
    GROUP BY f.membership_id
  ) r ON r.membership_id = m.membership_id
  WHERE m.membership_id IN (
    SELECT f3.membership_id
    FROM `eu-andy-marketing-raw.dashboard.fact_transactions` f3
    CROSS JOIN window_bounds wb_bpsp
    WHERE f3.t_date BETWEEN wb_bpsp.ws_min AND wb_bpsp.ws_max
  )
)"""


def brand_psp_join(alias: str, dims: list, filters: dict) -> str:
    """LEFT JOIN brand_psp_map sur membership_id, ou '' si brand_psp non utilisé."""
    if not _needs_brand_psp(dims, filters):
        return ""
    return f"LEFT JOIN brand_psp_map bpsp ON bpsp.membership_id = {alias}.membership_id"


# ---------------------------------------------------------------------------
# Funnel query (one per brand_type) — supports dynamic dimensions
# ---------------------------------------------------------------------------
def funnel_sql(brand_type: str, filters: dict, dims: list, weeks_list: List[date] = None,
               granularity_override: str = None, cohort_pool_sql: str = None,
               max_rx: int = 4, cohort_map_sql: str = None,
               max_rx_observed: int = 4, picked_end: date = None) -> str:
    # max_rx (2..4) : plafonne le calcul du funnel à R{max_rx}. Par défaut 4
    # (R0→R4, comportement inchangé pour les onglets Funnel). L'analyse A/B passe
    # à 2 (cohortes fraîches : R3/R4 toujours vides) -> supprime 2 grosses
    # jointures stg_memberships (r3/r4_tbb_raw) = beaucoup plus rapide.
    # max_rx_observed (1..26) : plafonne le nombre de niveaux R retournés dans
    # rx_stats (UNNEST côté final SELECT). Par défaut 4 (= comportement historique
    # rapide). L'onglet LTV peut monter ça à 26 si besoin d'observer R5+ pour la
    # projection LTV — mais pour les cohortes récentes (mai/juin) ça suffit
    # largement à 4 et c'est ~10x plus rapide.
    max_rx = max(2, min(int(max_rx), 4))
    max_rx_observed = max(1, min(int(max_rx_observed), 26))
    # granularity_override : force la granularité de cohorte (ex. 'date_day' pour
    #   l'onglet Analyse A/B) au lieu de la déduire des dims.
    # cohort_pool_sql : subquery renvoyant une colonne `customer_id`. Si fournie,
    #   on restreint bm/bt à ces customers (INNER JOIN) — sert à définir une
    #   cohorte A/B custom (psp/productId/metadata) sans la coder en filtre.
    granularity = granularity_override or date_dim_key(dims) or "date_week"
    trunc_arg = _DATE_DIM_TRUNC[granularity]
    weeks_cte = weeks_cte_sql(weeks_list, granularity, picked_end=picked_end) if weeks_list is not None else WEEKS_CTE
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
    bpsp_cte = brand_psp_cte(dims, filters)
    bpsp_join_fm = brand_psp_join("fm", dims, filters)
    bpsp_join_ft = brand_psp_join("ft", dims, filters)
    # Cohorte A/B : deux modes.
    #  - cohort_pool_sql : whitelist `customer_id` -> INNER JOIN (1 requête / cohorte).
    #  - cohort_map_sql  : (customer_id, cohort) -> 1 SEULE requête pour toutes les
    #    cohortes d'un tableau, la cohorte devient une dimension (RAW:cmap.cohort).
    #    Sémantique IDENTIQUE au pool (jointure customer-level) mais 2 requêtes au
    #    lieu de 2×N. Le dim est calculé dans bm puis propagé via elig_users -> btx.
    cohort_map_cte = (
        f",\ncohort_map AS (\n{cohort_map_sql}\n)" if cohort_map_sql else ""
    )
    if cohort_map_sql:
        cohort_join_fm = "  INNER JOIN cohort_map cmap ON cmap.customer_id = fm.customer_id"
        cohort_join_ft = (
            "  INNER JOIN (SELECT DISTINCT customer_id FROM cohort_map) cohort_pool "
            "ON cohort_pool.customer_id = ft.customer_id"
        )
    else:
        cohort_join_fm = (
            f"  INNER JOIN (\n{cohort_pool_sql}\n  ) cohort_pool ON cohort_pool.customer_id = fm.customer_id"
            if cohort_pool_sql else ""
        )
        cohort_join_ft = (
            f"  INNER JOIN (\n{cohort_pool_sql}\n  ) cohort_pool ON cohort_pool.customer_id = ft.customer_id"
            if cohort_pool_sql else ""
        )
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

    # --- Chaîne R-suivants (R2..max_rx) assemblée dynamiquement -------------
    # Chaque niveau R{n} a besoin de succ_r{n-1}, succ_next_r{n-1}, r{n}_elig,
    # r{n}_tbb_raw (jointure stg_memberships = coûteuse). max_rx=4 reproduit le
    # comportement complet ; max_rx=2 (A/B) saute r3/r4_tbb_raw.
    def _succ(k):
        return (f"succ_r{k} AS (\n"
                f"  SELECT cohort_week, {dims_only_trailing}customer_email, membership_id, MIN(t_datetime) AS succ_dt,\n"
                f"    ANY_VALUE(ms_billing_frequency) AS bf, ANY_VALUE(ms_billing_period) AS bp\n"
                f"  FROM btx WHERE invoice_r_index='{k}' AND transaction_status='succeeded' GROUP BY ALL\n)")
    def _succ_next(k):
        return (f"succ_next_r{k} AS (\n"
                f"  SELECT *, CASE WHEN bp='weeks' THEN TIMESTAMP_ADD(succ_dt, INTERVAL (CAST(bf AS INT64)*7) DAY)\n"
                f"                 WHEN bp='months' THEN TIMESTAMP_ADD(succ_dt, INTERVAL (CAST(bf AS INT64)*30) DAY)\n"
                f"                 ELSE TIMESTAMP_ADD(succ_dt, INTERVAL default_days DAY) END AS exp_next FROM succ_r{k}\n)")
    def _rxelig(n):
        return f"r{n}_elig AS (SELECT * FROM succ_next_r{n-1} WHERE exp_next <= cutoff_ts)"
    def _rxtbb(n):
        return (f"r{n}_tbb_raw AS (\n"
                f"  SELECT e.cohort_week, {dim_cols_trailing(dims, 'e')}\n"
                f"    COUNT(DISTINCT e.customer_email) AS elig_users,\n"
                f"    COUNT(DISTINCT CASE WHEN sm.CancelAtUtc IS NOT NULL AND TIMESTAMP(sm.CancelAtUtc) < e.exp_next THEN e.customer_email END) AS cancel_users\n"
                f"  FROM r{n}_elig e JOIN `eu-andy-marketing-raw.silver_sgw.stg_memberships` sm\n"
                f"    ON CAST(e.membership_id AS STRING) = CAST(sm.Id AS STRING)\n"
                f"  GROUP BY ALL\n)")
    _rx_parts = []
    for n in range(2, max_rx + 1):
        _rx_parts += [_succ(n - 1), _succ_next(n - 1), _rxelig(n), _rxtbb(n)]
    rx_chain = (",\n".join(_rx_parts) + ",\n") if _rx_parts else ""
    _tbb_union = "\n  UNION ALL ".join(
        f"SELECT cohort_week, {dims_only_trailing}'{n}' AS rx, elig_users, cancel_users FROM r{n}_tbb_raw"
        for n in range(2, max_rx + 1))
    tbb_join = (
        f"LEFT JOIN (\n  {_tbb_union}\n) tbb ON tbb.cohort_week = w.week_start "
        f"AND tbb.rx = rx_idx{dim_on_tbb}"
    )

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

    # UNNEST rx_idx list (R1..R{max_rx_observed}) — pre-built to avoid nested f-strings.
    _rx_idx_array = ", ".join(f"'{i}'" for i in range(1, max_rx_observed + 1))

    return f"""
DECLARE cutoff_ts TIMESTAMP DEFAULT TIMESTAMP(CURRENT_DATE());
DECLARE default_days INT64 DEFAULT {default_days};
{weeks_cte}{cp_cte}{cp_price_cte}{bpsp_cte}{cohort_map_cte},
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
  {bpsp_join_fm}
{cohort_join_fm}
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
    ft.transaction_status, ft.is_refunded, ft.is_alerted, ft.t_attempt_index, ft.t_datetime,
    ft.ms_billing_frequency, ft.ms_billing_period
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ft
  JOIN `eu-andy-marketing-raw.silver_sgw.stg_customers` c ON ft.customer_id = c.Id
  {cp_join_ft}
  {bpsp_join_ft}
{cohort_join_ft}
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
{rx_chain}rx_stats AS (
  SELECT cohort_week, {dims_only_trailing}invoice_r_index,
    COUNT(DISTINCT CASE WHEN t_attempt_index=1 THEN customer_email END) AS first_attempt_users,
    COUNT(DISTINCT CASE WHEN t_attempt_index=1 THEN transaction_id END) AS first_attempt_tx,
    COUNT(DISTINCT CASE WHEN t_attempt_index=1 AND transaction_status='succeeded' THEN transaction_id END) AS first_attempt_succ_tx,
    COUNT(DISTINCT transaction_id) AS total_tx,
    COUNT(DISTINCT CASE WHEN transaction_status='succeeded' THEN transaction_id END) AS succ_tx,
    COUNT(DISTINCT customer_email) AS attempted_users,
    COUNT(DISTINCT CASE WHEN transaction_status='succeeded' THEN customer_email END) AS succ_users,
    COUNT(DISTINCT CASE WHEN is_refunded=TRUE AND transaction_status='succeeded' THEN transaction_id END) AS refund_tx,
    COUNT(DISTINCT CASE WHEN is_refunded=TRUE AND transaction_status='succeeded' THEN customer_email END) AS refund_users,
    -- Net strict (= def Notion / Exec) : succeeded ET NI refundé NI alerté.
    COUNT(DISTINCT CASE WHEN transaction_status='succeeded'
        AND COALESCE(is_refunded, FALSE)=FALSE
        AND COALESCE(is_alerted, FALSE)=FALSE THEN customer_email END) AS net_users
  FROM btx
  WHERE SAFE_CAST(invoice_r_index AS INT64) BETWEEN 1 AND {max_rx_observed}
  GROUP BY ALL
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
  COALESCE(s.net_users, 0) AS net_users,
  COALESCE(tbb.elig_users, 0) AS tbb_elig_users,
  COALESCE(tbb.cancel_users, 0) AS tbb_cancel_users
FROM weeks w
{dim_axis_join}CROSS JOIN UNNEST([{_rx_idx_array}]) AS rx_idx
LEFT JOIN r0_stats r0 ON r0.cohort_week = w.week_start{dim_on_r0}
LEFT JOIN r1_tbb_cte r1 ON r1.cohort_week = w.week_start{dim_on_r1}
LEFT JOIN rx_stats s ON s.cohort_week = w.week_start AND s.invoice_r_index = rx_idx{dim_on_s}
{tbb_join}
ORDER BY w.week_start, {dim_cols_trailing(dims, "da") if has_nw else ""}rx_idx
"""


def ltv_vamp_sql(filters: dict, dims: list, weeks_list: List[date] = None,
                  picked_end: date = None) -> str:
    """Query VAMP réels pour le tab LTV.

    Retourne par cohorte × dim :
      - booking_r1plus_succ    : # tx Booking succeeded R1+ (hors R0)
      - booking_r1plus_alerted : # tx Booking alerted R1+ (hors R0)
      - total_succ             : # tx du user (toutes brand_type, toutes Rx)
      - total_alerted          : # tx alerted du user (toutes)

    Cohorte = R0 Booking succeeded dans la fenêtre (même définition que
    funnel_sql/bm). Post-signup uniquement (ft.t_date >= c.CreatedAtUtc).
    Tous types de cartes (pas de filtre cardnetwork='Visa').
    """
    granularity = date_dim_key(dims) or "date_week"
    trunc_arg = _DATE_DIM_TRUNC[granularity]
    weeks_cte = weeks_cte_sql(weeks_list, granularity, picked_end=picked_end) if weeks_list is not None else WEEKS_CTE

    cp_cte = customer_pool_cte(filters)
    cp_join_fm = customer_pool_join("fm", filters)
    cp_price_cte = customer_price_cte(dims)
    cp_price_join_fm = customer_price_join("fm", dims)
    bpsp_cte = brand_psp_cte(dims, filters)
    bpsp_join_fm = brand_psp_join("fm", dims, filters)
    fm_filter = row_level_filter_clause("fm", filters)

    bm_dim_sel = dim_select_clause("fm", dims)
    dims_only_trailing = dim_cols_trailing(dims)
    dims_only = dim_cols_bare(dims)
    has_nw = bool(non_week_dims(dims))
    final_dim_select = (
        ", ".join(f"da.dim_{d[0]} AS dim_{d[0]}" for d in non_week_dims(dims)) + ",\n  "
        if has_nw
        else ""
    )
    dim_axis_join = "CROSS JOIN dim_axis da\n" if has_nw else ""
    dim_axis_cte = (
        f",\ndim_axis AS (\n  SELECT DISTINCT {dims_only} FROM cohort_users\n)"
        if has_nw
        else ""
    )
    dim_on_stats = dim_join_on("v", "da", dims)

    return f"""
DECLARE cutoff_ts TIMESTAMP DEFAULT TIMESTAMP(CURRENT_DATE());
{weeks_cte}{cp_cte}{cp_price_cte}{bpsp_cte},
cohort_users AS (
  -- Cohorte Booking R0 succeeded (même définition que bm dans funnel_sql).
  SELECT DISTINCT
    DATE_TRUNC(DATE(c.CreatedAtUtc), {trunc_arg}) AS cohort_week,
{bm_dim_sel}    fm.customer_id,
    c.CreatedAtUtc AS signup_ts
  FROM `eu-andy-marketing-raw.dashboard.fact_memberships` fm
  JOIN `eu-andy-marketing-raw.silver_sgw.stg_customers` c ON fm.customer_id = c.Id
  JOIN `eu-andy-marketing-raw.silver_sgw.stg_memberships` sm
    ON CAST(fm.membership_id AS STRING) = CAST(sm.Id AS STRING)
  {cp_join_fm}
  {cp_price_join_fm}
  {bpsp_join_fm}
  CROSS JOIN window_bounds wb
  WHERE DATE(c.CreatedAtUtc) BETWEEN wb.ws_min AND wb.ws_max
    AND DATE_TRUNC(DATE(c.CreatedAtUtc), {trunc_arg}) IN (SELECT week_start FROM weeks)
    AND fm.brand_type = 'Booking'
    AND fm.brand != 'helpprio.com'
    AND fm.ms_status NOT IN ('abandonned','processing','paused')
    AND LOWER(fm.customer_email) NOT LIKE '%@yopmail%'
    AND LOWER(fm.customer_email) NOT LIKE '%@sharebot%'
    AND LOWER(fm.customer_firstname) NOT LIKE '%test%'
    AND NOT (sm.Segment = 'unknown' AND sm.Country = '' AND sm.Language = '')
    {fm_filter}
){dim_axis_cte},
alerted_tx AS (
  -- fact_alert VIEW : exclut déjà Order Insight. Tous types de cartes (pas
  -- de filtre Visa car ELA demande all-card VAMP dans le tab LTV).
  SELECT DISTINCT fa.transaction_id
  FROM `eu-andy-marketing-raw.dashboard.fact_alert` fa
),
tx_enriched AS (
  -- Post-signup uniquement (ft.t_date >= DATE(cu.signup_ts)). Toutes tx
  -- succeeded du customer (toutes brand_type, tous R index).
  SELECT
    cu.cohort_week, {dims_only_trailing}
    ft.brand_type,
    SAFE_CAST(ft.invoice_r_index AS INT64) AS r_idx,
    ft.transaction_id,
    CASE WHEN al.transaction_id IS NOT NULL THEN 1 ELSE 0 END AS is_alert
  FROM cohort_users cu
  JOIN `eu-andy-marketing-raw.dashboard.fact_transactions` ft
    ON ft.customer_id = cu.customer_id
  LEFT JOIN alerted_tx al ON al.transaction_id = ft.transaction_id
  WHERE ft.transaction_status = 'succeeded'
    AND ft.t_date >= DATE(cu.signup_ts)
    AND ft.t_date <= CURRENT_DATE()
),
vamp_stats AS (
  SELECT
    cohort_week, {dims_only_trailing}
    COUNTIF(brand_type='Booking' AND r_idx >= 1) AS booking_r1plus_succ,
    SUM(CASE WHEN brand_type='Booking' AND r_idx >= 1 THEN is_alert ELSE 0 END) AS booking_r1plus_alerted,
    COUNT(*) AS total_succ,
    SUM(is_alert) AS total_alerted
  FROM tx_enriched
  GROUP BY ALL
)
SELECT
  FORMAT_DATE('%Y-%m-%d', w.week_start) AS week_start,
  w.week_label,
  {final_dim_select}COALESCE(v.booking_r1plus_succ, 0)    AS booking_r1plus_succ,
  COALESCE(v.booking_r1plus_alerted, 0) AS booking_r1plus_alerted,
  COALESCE(v.total_succ, 0)             AS total_succ,
  COALESCE(v.total_alerted, 0)          AS total_alerted
FROM weeks w
{dim_axis_join}LEFT JOIN vamp_stats v ON v.cohort_week = w.week_start{dim_on_stats}
ORDER BY w.week_start{", " + dim_cols_trailing(dims, "da").rstrip(", ") if has_nw else ""}
"""


def vamp_cohort_sql(filters: dict, dims: list, weeks_list: List[date] = None,
                     picked_end: date = None) -> str:
    granularity = date_dim_key(dims) or "date_week"
    trunc_arg = _DATE_DIM_TRUNC[granularity]
    weeks_cte = weeks_cte_sql(weeks_list, granularity, picked_end=picked_end) if weeks_list is not None else WEEKS_CTE
    cp_cte = customer_pool_cte(filters)
    cp_join_ft = customer_pool_join("ft", filters)
    cp_price_cte = customer_price_cte(dims)
    cp_price_join_ft = customer_price_join("ft", dims)
    bpsp_cte = brand_psp_cte(dims, filters)
    bpsp_join_ft = brand_psp_join("ft", dims, filters)
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
{weeks_cte}{cp_cte}{cp_price_cte}{bpsp_cte},
tx AS (
  SELECT ft.transaction_id, ft.brand_type, ft.invoice_r_index, ft.transaction_amount,
{bm_dim_sel}    DATE_TRUNC(DATE(c.CreatedAtUtc), {trunc_arg}) AS cohort_week
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ft
  JOIN `eu-andy-marketing-raw.silver_sgw.stg_customers` c ON ft.customer_id = c.Id
  {cp_join_ft}
  {cp_price_join_ft}
  {bpsp_join_ft}
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


def vamp_date_sql(filters: dict, dims: list, weeks_list: List[date] = None,
                   picked_end: date = None) -> str:
    granularity = date_dim_key(dims) or "date_week"
    trunc_arg = _DATE_DIM_TRUNC[granularity]
    weeks_cte = weeks_cte_sql(weeks_list, granularity, picked_end=picked_end) if weeks_list is not None else WEEKS_CTE
    cp_cte = customer_pool_cte(filters)
    cp_join_ft = customer_pool_join("ft", filters)
    cp_join_t = customer_pool_join("t", filters)
    cp_price_cte = customer_price_cte(dims)
    cp_price_join_ft = customer_price_join("ft", dims)
    cp_price_join_t = customer_price_join("t", dims)
    bpsp_cte = brand_psp_cte(dims, filters)
    bpsp_join_ft = brand_psp_join("ft", dims, filters)
    bpsp_join_t = brand_psp_join("t", dims, filters)
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
{weeks_cte}{cp_cte}{cp_price_cte}{bpsp_cte},
tx AS (
  SELECT ft.transaction_id, ft.brand_type, ft.invoice_r_index, ft.transaction_amount,
{bm_dim_sel_ft}    DATE_TRUNC(ft.t_date, {trunc_arg}) AS tx_week
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ft
  {cp_join_ft}
  {cp_price_join_ft}
  {bpsp_join_ft}
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
  SELECT fa.transaction_id, DATE_TRUNC(fa.alerted_at, {trunc_arg}) AS alert_week
  FROM `eu-andy-marketing-raw.dashboard.fact_alert` fa
  WHERE DATE_TRUNC(fa.alerted_at, {trunc_arg}) IN (SELECT week_start FROM weeks)
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
  {bpsp_join_t}
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
    brand_psp = _brand_psp_concat("fm.brand", "fm.ms_default_psp", "rmid")
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
    COALESCE({brand_psp},           '') AS brand_psp,
    COALESCE(fm.sgw_verticale,      '') AS verticale,
    COALESCE(fm.ms_currency,        '') AS currency,
    COALESCE(fm.sgw_booking_market, '') AS booking_market,
    COALESCE({booking_bucket},      '') AS price_booking,
    COALESCE({magazine_bucket},     '') AS price_magazine
  FROM `eu-andy-marketing-raw.dashboard.fact_memberships` fm
  LEFT JOIN (
    {_r0_mid_subquery(f"AND f.t_date BETWEEN DATE_SUB({ws_min}, INTERVAL 15 DAY) AND DATE_ADD({ws_max}, INTERVAL 15 DAY)")}
  ) rmid ON rmid.membership_id = fm.membership_id
  CROSS JOIN wb
  WHERE DATE(fm.ms_datetime) BETWEEN wb.ws_min AND wb.ws_max
    AND COALESCE(fm.brand, '') NOT LIKE '%helpprio%'
    AND LOWER(fm.customer_email) NOT LIKE '%@yopmail%'
    AND LOWER(fm.customer_email) NOT LIKE '%@sharebot%'
    AND LOWER(fm.customer_firstname) NOT LIKE '%test%'
)
SELECT dim, val, COUNT(*) AS n FROM fm_recent
UNPIVOT (val FOR dim IN (psp, conciergerie, brand_psp, verticale, currency, booking_market, price_booking, price_magazine))
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


def build_funnel_table(df: pd.DataFrame, brand_type: str, dims: list,
                        picked_end: date = None, today: date = None) -> pd.DataFrame:
    """Pivot the long-format funnel df into a (KPI × dim_combo) display table.

    Args:
        picked_end : end date of selected sidebar range (used by LTV simulator).
        today      : observation date (defaults to date.today() — used by LTV).
    """
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
            "att_u": 0, "succ_u": 0, "refund_tx": 0, "refund_u": 0, "net_u": 0,
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
        rx_acc["net_u"] += int(r["net_users"])
        rx_acc["elig_u"] += int(r["tbb_elig_users"])
        rx_acc["cancel_u"] += int(r["tbb_cancel_users"])
        rx_acc["tbb"] = max(rx_acc["elig_u"] - rx_acc["cancel_u"], 0)

    # Seuil 200 R0 succeeded — masque les cohortes trop petites
    groups_all = sorted(by_group.keys(), key=lambda g: group_sort_key.get(g, g))
    groups = [g for g in groups_all if by_group[g]["r0_succeeded"] >= MIN_R0_DISPLAY]
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
    # % Churn Net R0/R1 — Net STRICT : R1 net = succeeded ET ni refundé ni
    # alerté (net_u). Aligné sur la def Notion / Exec Summary.
    push("% Churn Net R0/R1",
         [fmt_pct(g0(g, "r0_succeeded") - gr(g, "1", "net_u"),
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
        # % Churn Net Rn/Rm = (Rn_net - Rm_net) / Rn_net
        # où Rk_net = net_u (succeeded ET ni refundé ni alerté — Net STRICT,
        # aligné Notion/Exec). AVANT : Rk_net = succ_u - refund_u (n'excluait
        # pas is_alerted, et un bug antérieur divisait par Rn_brut).
        push(f"% Churn Net R{prev}/{lbl}",
             [val_or(g, lambda g=g: fmt_pct(
                 gr(g, prev, "net_u") - gr(g, rx, "net_u"),
                 gr(g, prev, "net_u"))) for g in groups])

    # =====================================================================
    # LTV Simulator (Booking only) — adds 4 lines at the bottom
    # =====================================================================
    if brand_type == "Booking":
        _today = today or date.today()
        _picked_end = picked_end or _today

        arpu_brut_vals, arpu_net_vals = [], []
        ltv_brut_vals, ltv_net_vals = [], []
        for g in groups:
            price = _ltv_get_price_from_group(g, dims)
            if price is None:
                for lst in (arpu_brut_vals, arpu_net_vals, ltv_brut_vals, ltv_net_vals):
                    lst.append("—")
                continue
            sort_key = group_sort_key.get(g, g)
            cohort_end = _ltv_cohort_end_date(sort_key, dims, _picked_end)
            sim = _ltv_compute(by_group[g], price, cohort_end, _today)
            if sim is None:
                for lst in (arpu_brut_vals, arpu_net_vals, ltv_brut_vals, ltv_net_vals):
                    lst.append("—")
            else:
                arpu_brut_vals.append(_ltv_fmt_eur(sim["arpu_brut_eur"]))
                arpu_net_vals.append(_ltv_fmt_eur(sim["arpu_net_eur"]))
                ltv_brut_vals.append(_ltv_fmt_eur(sim["ltv_brut_eur"]))
                ltv_net_vals.append(_ltv_fmt_eur(sim["ltv_net_eur"]))

        push("LTV Simulator", ["" for _ in groups], section=True)
        push("# R0 Succeeded", [fmt_int(g0(g, "r0_succeeded")) for g in groups])
        push("ARPU brute (€)", arpu_brut_vals)
        push("ARPU net (€)", arpu_net_vals)
        push("LTV brute (€)", ltv_brut_vals)
        push("LTV net (€)", ltv_net_vals)

    out = pd.DataFrame(rows)
    out.attrs["dims"] = dims
    out.attrs["groups"] = groups
    return out


# ---------------------------------------------------------------------------
# LTV tab table — focus R0→R1 leviers + ARPU R1 + LTV simulée
# ---------------------------------------------------------------------------

def build_ltv_table(df: pd.DataFrame, dims: list,
                     picked_end: date = None, today: date = None,
                     df_vamp: pd.DataFrame = None) -> pd.DataFrame:
    """Pivot le df funnel_sql en table KPI × dim_combo orientée LTV.

    Sections :
      - R0 / Trial : # R0 Succeeded, % Unsub During Trial
      - R0 → R1 (leviers) : % R1 Succeeded per User, % Refund R1, % Churn Brut/Net R0/R1
      - LTV (par R0) : ARPU R1 (€), LTV brute (€), LTV net (€)

    ARPU R1 et LTV en € ne sont calculées que si la dim `price_booking` est
    sélectionnée (sinon affichage "—").
    """
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

    by_group: dict = {}
    cohort_seen: dict = {}
    group_sort_key: dict = {}

    for _, r in df.iterrows():
        g = gk(r)
        if g not in by_group:
            by_group[g] = {
                "r0_attempts": 0, "r0_succeeded": 0, "unsub_trial": 0, "r1_tbb": 0,
                "rx": {},
            }
            cohort_seen[g] = set()
            sort_parts = []
            for d in dims or []:
                if d[0] in _DATE_DIM_KEYS:
                    sort_parts.append(str(r.get("week_start", "")))
                else:
                    sort_parts.append(_safe_str(r.get(f"dim_{d[0]}", "")))
            group_sort_key[g] = tuple(sort_parts) if sort_parts else g

        cohort_id = (r["week_label"],) + tuple(_safe_str(r.get(f"dim_{d[0]}", "")) for d in non_week)
        if cohort_id not in cohort_seen[g]:
            cohort_seen[g].add(cohort_id)
            by_group[g]["r0_attempts"] += int(r["r0_attempts"])
            by_group[g]["r0_succeeded"] += int(r["r0_succeeded"])
            by_group[g]["unsub_trial"] += int(r["unsub_trial"])
            by_group[g]["r1_tbb"] += int(r["r1_tbb"])

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

    # Agrégation VAMP réels par groupe (df_vamp facultatif : si absent, on
    # laisse "—" sur les 2 lignes VAMP réels).
    vamp_by_group: dict = {}
    if df_vamp is not None and not df_vamp.empty:
        for _, r in df_vamp.iterrows():
            g = gk(r)
            v = vamp_by_group.setdefault(g, {
                "booking_r1plus_succ": 0, "booking_r1plus_alerted": 0,
                "total_succ": 0, "total_alerted": 0,
            })
            v["booking_r1plus_succ"]    += int(r.get("booking_r1plus_succ", 0) or 0)
            v["booking_r1plus_alerted"] += int(r.get("booking_r1plus_alerted", 0) or 0)
            v["total_succ"]             += int(r.get("total_succ", 0) or 0)
            v["total_alerted"]          += int(r.get("total_alerted", 0) or 0)

    # Filtrage seuil 200 R0 succeeded — on masque les colonnes trop petites
    # (cohortes sous-significatives → KPI trop bruités).
    groups_all = sorted(by_group.keys(), key=lambda g: group_sort_key.get(g, g))
    groups = [g for g in groups_all if by_group[g]["r0_succeeded"] >= MIN_R0_DISPLAY]
    rows = []

    def push(label, vals, *, section=False):
        rows.append({"__key__": ("__SECTION__" + label) if section else label,
                     **{g: v for g, v in zip(groups, vals)}})

    def g0(g, k):
        return by_group[g][k]

    def gr(g, rx, k):
        return by_group[g]["rx"].get(rx, {}).get(k, 0)

    # =====================================================================
    # Section "Stats Funnel" (R0 / Trial + leviers R0→R1)
    # =====================================================================
    push("Stats Funnel", ["" for _ in groups], section=True)
    push("# R0 Succeeded", [fmt_int(g0(g, "r0_succeeded")) for g in groups])
    push("% Unsub During Trial",
         [fmt_pct(g0(g, "unsub_trial"), g0(g, "r0_succeeded")) for g in groups])
    push("% R1 Succeeded per User",
         [fmt_pct(gr(g, "1", "succ_u"), gr(g, "1", "att_u")) for g in groups])
    push("% Refund R1",
         [fmt_pct(gr(g, "1", "refund_tx"), gr(g, "1", "succ_tx")) for g in groups])
    push("% Churn Brut R0/R1",
         [fmt_pct(g0(g, "r0_succeeded") - gr(g, "1", "succ_u"), g0(g, "r0_succeeded")) for g in groups])
    push("% Churn Net R0/R1",
         [fmt_pct(g0(g, "r0_succeeded") - (gr(g, "1", "succ_u") - gr(g, "1", "refund_u")),
                  g0(g, "r0_succeeded")) for g in groups])

    # =====================================================================
    # Calcul commun ARPU + LTV (utilisé par les sections Réel + Projection)
    # =====================================================================
    _today = today or date.today()
    _picked_end = picked_end or _today

    # ARPU R1 (€) = (R1 NET × prix) / R0
    # ARPU Total (€) = total revenue NET observé (R1..r_max_obs) / R0
    # LTV Base R1 brut/net = projection depuis R1 seulement
    # LTV Base Totale brut/net = observé R1..r_max_obs + projection depuis r_max_obs+1
    arpu_r1_vals, arpu_total_vals = [], []
    ltv_brut_r1_vals, ltv_net_r1_vals = [], []
    ltv_brut_tot_vals, ltv_net_tot_vals = [], []

    for g in groups:
        price = _ltv_get_price_from_group(g, dims)
        r0 = g0(g, "r0_succeeded")
        if price is None or r0 == 0:
            for vals in (arpu_r1_vals, arpu_total_vals,
                         ltv_brut_r1_vals, ltv_net_r1_vals,
                         ltv_brut_tot_vals, ltv_net_tot_vals):
                vals.append("—")
            continue

        sort_key = group_sort_key.get(g, g)
        cohort_end = _ltv_cohort_end_date(sort_key, dims, _picked_end)

        # Sim R1-only : force r_max_obs=1 → projection naïve depuis R1
        sim_r1 = _ltv_compute(by_group[g], price, cohort_end, _today,
                               r_max_obs_override=1)
        # Sim Total : maturité naturelle → obs R1..r_max_obs + proj r_max_obs+1..horizon
        sim_tot = _ltv_compute(by_group[g], price, cohort_end, _today)

        # ARPU R1 (€) — calcul direct
        amount = LTV_PRICE_CONFIG[price][2]
        r1_net = gr(g, "1", "succ_u") - gr(g, "1", "refund_u")
        arpu_r1_vals.append(_ltv_fmt_eur((r1_net / r0) * amount))

        # ARPU Total (€) — revenue NET observé cumulé / R0
        if sim_tot is not None:
            arpu_total_vals.append(_ltv_fmt_eur(sim_tot["arpu_net_eur"]))
        else:
            arpu_total_vals.append("—")

        # LTV Base R1
        if sim_r1 is not None:
            ltv_brut_r1_vals.append(_ltv_fmt_eur(sim_r1["ltv_brut_eur"]))
            ltv_net_r1_vals.append(_ltv_fmt_eur(sim_r1["ltv_net_eur"]))
        else:
            ltv_brut_r1_vals.append("—")
            ltv_net_r1_vals.append("—")

        # LTV Base Totale
        if sim_tot is not None:
            ltv_brut_tot_vals.append(_ltv_fmt_eur(sim_tot["ltv_brut_eur"]))
            ltv_net_tot_vals.append(_ltv_fmt_eur(sim_tot["ltv_net_eur"]))
        else:
            ltv_brut_tot_vals.append("—")
            ltv_net_tot_vals.append("—")

    # =====================================================================
    # Section "Réel" (ARPU + VAMP observés)
    # =====================================================================
    push("Réel", ["" for _ in groups], section=True)
    push("ARPU R1 (€)", arpu_r1_vals)
    push("ARPU Total (€)", arpu_total_vals)

    def _vamp_ratio(g, num_key, den_key):
        v = vamp_by_group.get(g)
        if v is None:
            return "—"
        return fmt_pct(v[num_key], v[den_key])

    push("Vamp Ratio - Abo Booking Only",
         [_vamp_ratio(g, "booking_r1plus_alerted", "booking_r1plus_succ") for g in groups])
    push("VAMP Ratio - Total",
         [_vamp_ratio(g, "total_alerted", "total_succ") for g in groups])

    # =====================================================================
    # Section "Projection" (LTV + VAMP projetés via decay)
    # =====================================================================
    push("Projection", ["" for _ in groups], section=True)
    push("LTV brute - Base R1 (€)", ltv_brut_r1_vals)
    push("LTV nette - Base R1 (€)", ltv_net_r1_vals)
    push("LTV brute - Base Totale (€)", ltv_brut_tot_vals)
    push("LTV nette - Base Totale (€)", ltv_net_tot_vals)
    # VAMP projetés : à implémenter dans un prochain commit (ratios sheet MTV)
    push("Vamp Ratio - Abo Booking Only (projeté)", ["—" for _ in groups])
    push("VAMP Ratio - Total (projeté)", ["—" for _ in groups])

    out = pd.DataFrame(rows)
    out.attrs["dims"] = dims
    out.attrs["groups"] = groups
    return out


# ---------------------------------------------------------------------------
# Funnel graph — same data as build_funnel_table but pivoted for plotting.
#
# Each "point" on the graph = one (time bucket, curve dim value) cell, which
# is exactly one cohort. We aggregate the funnel_sql raw output the same way
# build_funnel_table does (one row per rx_idx per cohort), then compute every
# KPI per cell. The result is a tidy long DataFrame ready for a Plotly chart:
#
#   time | time_label | curve | kpi | value
#
# - time          : pd.Timestamp / date (X axis)
# - time_label    : human-readable ('S20 (11/05)', '15/05/2026', 'Mai 2026')
# - curve         : dim value of the curve split (or 'Total' if no curve dim)
# - kpi           : KPI label, same naming as in build_funnel_table rows
# - value         : raw float / int (None if undefined — e.g. div by 0)
#
# Ratio KPIs are computed PER CELL (= same as the table cell). No further
# aggregation — what the user sees on a graph point matches exactly what
# they'd see in the equivalent cell of the table.
# ---------------------------------------------------------------------------

# Ordered list of all KPIs the funnel exposes. Used to populate the radio
# list in the order users expect (matches build_funnel_table sections).
ALL_FUNNEL_KPIS = [
    # R0
    "# R0 Attempts",
    "% Success Rate R0",
    "# R0 Succeeded",
    "% Unsub During Trial",
    "# R1 To Be Billed",
    "% R1 Billed",
    # R1
    "# R1 First Attempt (users)",
    "% Success R1 First Attempt",
    "# R1 Attempts (tx dedup)",
    "% Success R1 Attempts",
    "# R1 Succeeded (users)",
    "% R1 Succeeded per User",
    "% Churn Brut R0/R1",
    "# Refund R1",
    "% Refund R1",
    "% Churn Net R0/R1",
    # R2..R4 — generated below
]
for _rx in ["2", "3", "4"]:
    _prev = str(int(_rx) - 1)
    _lbl = f"R{_rx}"
    ALL_FUNNEL_KPIS += [
        f"# {_lbl} To Be Billed",
        f"% {_lbl} Billed",
        f"# {_lbl} First Attempt (users)",
        f"% Success {_lbl} First Attempt",
        f"# {_lbl} Attempts (tx dedup)",
        f"% Success {_lbl} Attempts",
        f"# {_lbl} Succeeded (users)",
        f"% {_lbl} Succeeded per User",
        f"% Churn Brut R{_prev}/{_lbl}",
        f"# Refund {_lbl}",
        f"% Refund {_lbl}",
        f"% Churn Net R{_prev}/{_lbl}",
    ]

# LTV Simulator KPIs (Booking only, calculated from observed R + decay projection)
ALL_FUNNEL_KPIS += [
    "ARPU brute (€)",
    "ARPU net (€)",
    "LTV brute (€)",
    "LTV net (€)",
]


# Section structure mirroring build_funnel_table — used by the graph's left
# panel so it has the same hierarchical look as the table above.
FUNNEL_KPI_SECTIONS = [
    ("R0 / Trial", [
        "# R0 Attempts",
        "% Success Rate R0",
        "# R0 Succeeded",
        "% Unsub During Trial",
        "# R1 To Be Billed",
        "% R1 Billed",
    ]),
    ("R1", [
        "# R1 First Attempt (users)",
        "% Success R1 First Attempt",
        "# R1 Attempts (tx dedup)",
        "% Success R1 Attempts",
        "# R1 Succeeded (users)",
        "% R1 Succeeded per User",
        "% Churn Brut R0/R1",
        "# Refund R1",
        "% Refund R1",
        "% Churn Net R0/R1",
    ]),
]
for _rx in ["2", "3", "4"]:
    _prev = str(int(_rx) - 1)
    _lbl = f"R{_rx}"
    FUNNEL_KPI_SECTIONS.append((_lbl, [
        f"# {_lbl} To Be Billed",
        f"% {_lbl} Billed",
        f"# {_lbl} First Attempt (users)",
        f"% Success {_lbl} First Attempt",
        f"# {_lbl} Attempts (tx dedup)",
        f"% Success {_lbl} Attempts",
        f"# {_lbl} Succeeded (users)",
        f"% {_lbl} Succeeded per User",
        f"% Churn Brut R{_prev}/{_lbl}",
        f"# Refund {_lbl}",
        f"% Refund {_lbl}",
        f"% Churn Net R{_prev}/{_lbl}",
    ]))

FUNNEL_KPI_SECTIONS.append(("LTV Simulator", [
    "ARPU R1 (€)",
    "ARPU brute (€)",
    "ARPU net (€)",
    "LTV brute (€)",
    "LTV net (€)",
]))

# Ajouter ARPU R1 (€) dans la liste plate
if "ARPU R1 (€)" not in ALL_FUNNEL_KPIS:
    ALL_FUNNEL_KPIS.append("ARPU R1 (€)")


# ---------------------------------------------------------------------------
# LTV tab — KPI sections (sous-ensemble du Funnel Booking + ARPU R1)
# ---------------------------------------------------------------------------
LTV_KPI_SECTIONS = [
    ("Stats Funnel", [
        "# R0 Succeeded",
        "% Unsub During Trial",
        "% R1 Succeeded per User",
        "% Refund R1",
        "% Churn Brut R0/R1",
        "% Churn Net R0/R1",
    ]),
    ("Réel", [
        "ARPU R1 (€)",
        "ARPU Total (€)",
        "Vamp Ratio - Abo Booking Only",
        "VAMP Ratio - Total",
    ]),
    ("Projection", [
        "LTV brute - Base R1 (€)",
        "LTV nette - Base R1 (€)",
        "LTV brute - Base Totale (€)",
        "LTV nette - Base Totale (€)",
        "Vamp Ratio - Abo Booking Only (projeté)",
        "VAMP Ratio - Total (projeté)",
    ]),
]
ALL_LTV_KPIS = [k for _, kpis in LTV_KPI_SECTIONS for k in kpis]


# ---------------------------------------------------------------------------
# VAMP — listes de KPIs + sections (pour le graphe Plotly sous tabs VAMP)
# ---------------------------------------------------------------------------

# Mapping nom de section ↔ valeur de `cat` retournée par vamp_*_sql
VAMP_CAT_MAP = [
    ("Total",       "total"),
    ("Rx Booking",  "rx_booking"),
    ("R0 Micro",    "r0_micro"),
    ("Rx Micro",    "rx_micro"),
    ("Rx Magazine", "rx_magazine"),
]

ALL_VAMP_KPIS = []
VAMP_KPI_SECTIONS = []
for _section_name, _cat in VAMP_CAT_MAP:
    _kpis = [
        f"# Tx Succeeded ({_section_name})",
        f"# Alertes ({_section_name})",
        f"% VAMP ({_section_name})",
    ]
    ALL_VAMP_KPIS.extend(_kpis)
    VAMP_KPI_SECTIONS.append((_section_name, _kpis))


def vamp_graph_data(df: pd.DataFrame, time_dim_key: str,
                    curve_dim_key: str | None,
                    min_denom_for_ratios: int = 20) -> pd.DataFrame:
    """Aggrège le df brut vamp_*_sql en tidy long DataFrame pour plotting.

    Le df source contient 1 ligne par (week_start, dim_<time>, dim_<curve>, cat)
    avec colonnes n_tx (tx succ) et n_al (alertes). On agrège par (time, curve)
    et on dérive 15 KPIs : 5 catégories × {# Tx Succeeded, # Alertes, % VAMP}.

    Returns tidy DataFrame (time, time_label, curve, kpi, value).
    """
    if df.empty:
        return pd.DataFrame(columns=["time", "time_label", "curve", "kpi", "value"])

    def _parse_time(s):
        if pd.isna(s):
            return None
        try:
            return pd.to_datetime(s).date()
        except Exception:
            return s

    cells: dict = {}      # (t_key, c_val) → {cat → {n_tx, n_al}}
    cell_labels: dict = {}

    for _, r in df.iterrows():
        t_key = _parse_time(r["week_start"])
        t_label = r["week_label"]
        c_val = _safe_str(r.get(f"dim_{curve_dim_key}", "")) if curve_dim_key else "Total"
        if c_val == "":
            c_val = "(empty)"
        key = (t_key, c_val)
        if key not in cells:
            cells[key] = {}
            cell_labels[key] = t_label
        cat = r["cat"]
        acc = cells[key].setdefault(cat, {"n_tx": 0, "n_al": 0})
        acc["n_tx"] += int(r["n_tx"])
        acc["n_al"] += int(r["n_al"])

    def _ratio(num, denom):
        if denom is None or denom == 0 or denom < min_denom_for_ratios:
            return None
        return num / denom

    rows = []
    for (t_key, c_val), cats in cells.items():
        t_label = cell_labels[(t_key, c_val)]
        for section_name, cat_key in VAMP_CAT_MAP:
            cat_data = cats.get(cat_key, {"n_tx": 0, "n_al": 0})
            n_tx = cat_data["n_tx"]
            n_al = cat_data["n_al"]
            kpis = {
                f"# Tx Succeeded ({section_name})": float(n_tx),
                f"# Alertes ({section_name})":      float(n_al),
                f"% VAMP ({section_name})":         _ratio(n_al, n_tx),
            }
            for kpi_name, val in kpis.items():
                rows.append({
                    "time": t_key,
                    "time_label": t_label,
                    "curve": c_val,
                    "kpi": kpi_name,
                    "value": val,
                })

    return pd.DataFrame(rows)


def render_vamp_graph(vamp_kind: str, filters: dict, picked_start, picked_end,
                     key_prefix: str) -> None:
    """Render le graphe Plotly sous un tab VAMP (Cohort ou Date).

    vamp_kind : 'cohort' ou 'date' → utilise vamp_cohort_sql ou vamp_date_sql.
    Structure identique à render_funnel_graph (KPI buttons gauche + chart droite)
    mais avec ALL_VAMP_KPIS / VAMP_KPI_SECTIONS et vamp_graph_data en backend.
    """
    import plotly.express as px

    st.markdown("### 📈 Évolution dans le temps")

    # --- Top controls ---
    ctrl1, ctrl2, _spacer = st.columns([1.5, 1.5, 4])
    time_options = ["Date (jour)", "Date (semaine)", "Date (mois)"]
    graph_time_label = ctrl1.selectbox(
        "Granularité X", options=time_options, index=1,
        key=f"{key_prefix}_graph_time",
        help="Granularité de l'axe X (indépendant de la sidebar)",
    )
    _NONE_GRAPH = "— aucune (agrégé) —"
    curve_options = [_NONE_GRAPH] + [d[1] for d in DIMENSION_DIMS if d[0] not in _DATE_DIM_KEYS]
    graph_curve_label = ctrl2.selectbox(
        "Dim courbe", options=curve_options, index=0,
        key=f"{key_prefix}_graph_curve",
        help="Une courbe par valeur de cette dim. « Aucune » = une seule courbe agrégée.",
    )

    graph_time_dim = next(d for d in DIMENSION_DIMS if d[1] == graph_time_label)
    if graph_curve_label != _NONE_GRAPH:
        graph_curve_dim = next(d for d in DIMENSION_DIMS if d[1] == graph_curve_label)
        graph_dims_list = [graph_time_dim, graph_curve_dim]
        curve_key = graph_curve_dim[0]
    else:
        graph_curve_dim = None
        graph_dims_list = [graph_time_dim]
        curve_key = None

    graph_weeks_list = periods_in_range(picked_start, picked_end, graph_time_dim[0])
    if not graph_weeks_list:
        st.info("Plage de dates vide pour cette granularité.")
        return

    sql_fn = vamp_cohort_sql if vamp_kind == "cohort" else vamp_date_sql
    pretty_kind = "Cohort" if vamp_kind == "cohort" else "Date"

    with st.spinner("Graphe…"):
        try:
            raw_df = run_query(sql_fn(filters, graph_dims_list, graph_weeks_list))
            graph_df = vamp_graph_data(raw_df, graph_time_dim[0], curve_key)
        except Exception as e:
            st.error(f"Erreur graphe : {e}")
            return

    if graph_df.empty:
        st.info("Aucune donnée pour ce périmètre.")
        return

    has_data = (
        graph_df.dropna(subset=["value"])
        .groupby("kpi")
        .size()
        .reset_index(name="n")
    )
    available = set(has_data["kpi"].tolist())
    if not available:
        st.info("Aucun KPI avec données sur cette plage.")
        return

    # --- Selected KPI state ---
    state_key = f"{key_prefix}_graph_kpi"
    if state_key not in st.session_state or st.session_state[state_key] not in available:
        # Défaut : "# Tx Succeeded (Total)"
        if "# Tx Succeeded (Total)" in available:
            st.session_state[state_key] = "# Tx Succeeded (Total)"
        else:
            ordered_available = [k for k in ALL_VAMP_KPIS if k in available]
            st.session_state[state_key] = ordered_available[0] if ordered_available else None

    # CSS partagé avec le funnel graph (même classe `.psp-kpi-buttons`).
    st.markdown(
        """
        <style>
          .psp-kpi-buttons div[data-testid="stVerticalBlock"] { gap: 2px; }
          .psp-kpi-buttons div.stButton > button {
            text-align: left; justify-content: flex-start;
            font-size: 12px; padding: 4px 10px; border-radius: 4px;
            font-weight: 400; min-height: 0; line-height: 1.3;
            width: 100%; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
          }
          .psp-kpi-buttons div.stButton > button p { font-size: 12px; margin: 0; }
          .psp-kpi-section-header {
            font-size: 11px; font-weight: 700; text-transform: uppercase;
            color: white; background: #0f172a; padding: 6px 10px;
            margin: 8px 0 4px 0; border-radius: 4px; letter-spacing: 0.04em;
          }
          .psp-kpi-section-header:first-child { margin-top: 0; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    col_kpi, col_chart = st.columns([1.6, 4.4])
    with col_kpi:
        st.markdown('<div class="psp-kpi-buttons">', unsafe_allow_html=True)
        with st.container(height=520, border=False):
            for section_name, section_kpis in VAMP_KPI_SECTIONS:
                visible_kpis = [k for k in section_kpis if k in available]
                if not visible_kpis:
                    continue
                st.markdown(
                    f'<div class="psp-kpi-section-header">{section_name}</div>',
                    unsafe_allow_html=True,
                )
                for kpi in visible_kpis:
                    is_active = (kpi == st.session_state[state_key])
                    if st.button(
                        kpi,
                        key=f"{key_prefix}_kpi_btn_{kpi}",
                        type="primary" if is_active else "secondary",
                        use_container_width=True,
                    ):
                        st.session_state[state_key] = kpi
                        st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    selected_kpi = st.session_state[state_key]

    with col_chart:
        plot_data = graph_df[graph_df["kpi"] == selected_kpi].copy()
        if plot_data.dropna(subset=["value"]).empty:
            st.info(f"Aucune donnée pour {selected_kpi} sur cette plage "
                    f"(possible : dénominateur < 20 sur tous les points).")
            return

        is_pct = selected_kpi.startswith("%")
        if is_pct:
            plot_data["value"] = plot_data["value"] * 100

        plot_data = plot_data.sort_values("time")

        palette = (
            px.colors.qualitative.D3
            + px.colors.qualitative.Set2
            + px.colors.qualitative.Set3
        )

        fig = px.line(
            plot_data, x="time", y="value", color="curve",
            markers=True, color_discrete_sequence=palette,
            hover_data={"time_label": True, "time": False, "value": ":.2f"},
            labels={
                "time": graph_time_label, "value": selected_kpi,
                "curve": graph_curve_label if graph_curve_dim else "Série",
            },
        )
        if is_pct:
            fig.update_yaxes(ticksuffix=" %", tickformat=".2f")
        else:
            fig.update_yaxes(tickformat=",d", separatethousands=True)

        fig.update_traces(
            mode="lines+markers",
            line=dict(width=2.5),
            marker=dict(size=7, line=dict(width=1, color="white")),
            connectgaps=False,
            hovertemplate=(
                "<b>%{fullData.name}</b><br>"
                + "%{customdata[0]}<br>"
                + f"{selected_kpi} : %{{y:,.2f}}"
                + (" %" if is_pct else "")
                + "<extra></extra>"
            ),
        )

        fig.update_layout(
            title=dict(
                text=f"<b>{selected_kpi}</b>  ·  VAMP {pretty_kind}",
                x=0.0, xanchor="left",
                font=dict(size=15, color="#0f172a"),
            ),
            height=520,
            margin=dict(l=10, r=10, t=60, b=40),
            plot_bgcolor="white", paper_bgcolor="white",
            hovermode="x unified",
            xaxis=dict(
                showgrid=True, gridcolor="#f1f5f9", gridwidth=1,
                showline=True, linecolor="#cbd5e1", linewidth=1,
                ticks="outside", tickcolor="#94a3b8",
                title=dict(font=dict(size=12, color="#475569")),
            ),
            yaxis=dict(
                showgrid=True, gridcolor="#f1f5f9", gridwidth=1,
                showline=True, linecolor="#cbd5e1", linewidth=1,
                zeroline=True, zerolinecolor="#cbd5e1",
                ticks="outside", tickcolor="#94a3b8",
                title=dict(font=dict(size=12, color="#475569")),
                rangemode="tozero" if not is_pct else "normal",
            ),
            legend=dict(
                orientation="v", x=1.02, y=1, xanchor="left",
                bgcolor="rgba(255,255,255,0.9)",
                bordercolor="#e2e8f0", borderwidth=1,
                font=dict(size=11),
            ),
            font=dict(family="-apple-system, BlinkMacSystemFont, sans-serif"),
        )

        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "💡 Points masqués : sur les **ratios % VAMP**, les cellules dont "
            "le dénominateur (# Tx Succeeded) < 20 sont laissées vides."
        )


def funnel_graph_data(
    df: pd.DataFrame,
    time_dim_key: str,
    curve_dim_key: str | None,
    min_denom_for_ratios: int = 20,
    picked_end: date = None,
    today: date = None,
    brand_type: str = "Booking",
) -> pd.DataFrame:
    """Aggregate raw funnel df → tidy long DataFrame for plotting.

    Mirrors build_funnel_table's aggregation but with the (cell = cohort)
    semantics since dims = [time, curve]: each cell aggregates the 5 rx_idx
    rows of ONE cohort, cohort-level fields counted once, per-rx fields
    summed.

    Args:
      df                   : raw BQ output from funnel_sql(brand_type, ..., dims=[time, curve])
      time_dim_key         : 'date_day' | 'date_week' | 'date_month'
      curve_dim_key        : DIMENSION_DIMS key for the curve, or None (single 'Total')
      min_denom_for_ratios : ratios computed on a denominator strictly smaller
                             than this return None (point will be skipped on
                             the graph). Default 20 to avoid noisy points on
                             tiny cohorts. Volume KPIs are NOT affected.

    Returns: tidy DataFrame (time, time_label, curve, kpi, value).
    """
    if df.empty:
        return pd.DataFrame(columns=["time", "time_label", "curve", "kpi", "value"])

    # Pre-parse week_start to a sortable date (BQ returns ISO string).
    def _parse_time(s):
        if pd.isna(s):
            return None
        try:
            return pd.to_datetime(s).date()
        except Exception:
            return s

    cells: dict = {}  # key = (time, curve), value = agg dict
    cell_labels: dict = {}  # key = (time, curve), value = time_label

    for _, r in df.iterrows():
        t_key = _parse_time(r["week_start"])
        t_label = r["week_label"]
        c_val = _safe_str(r.get(f"dim_{curve_dim_key}", "")) if curve_dim_key else "Total"
        if c_val == "":
            c_val = "(empty)"
        key = (t_key, c_val)

        if key not in cells:
            cells[key] = {
                "r0_attempts": 0, "r0_succeeded": 0, "unsub_trial": 0, "r1_tbb": 0,
                "rx": {},
                "_cohort_counted": False,
            }
            cell_labels[key] = t_label

        # Cohort-level — count once per cell (each cell IS one cohort here).
        if not cells[key]["_cohort_counted"]:
            cells[key]["_cohort_counted"] = True
            cells[key]["r0_attempts"] += int(r["r0_attempts"])
            cells[key]["r0_succeeded"] += int(r["r0_succeeded"])
            cells[key]["unsub_trial"] += int(r["unsub_trial"])
            cells[key]["r1_tbb"] += int(r["r1_tbb"])

        # Per-rx — accumulate.
        rx = str(r["rx_idx"])
        acc = cells[key]["rx"].setdefault(rx, {
            "fa_u": 0, "fa_tx": 0, "fa_succ_tx": 0,
            "total_tx": 0, "succ_tx": 0,
            "att_u": 0, "succ_u": 0,
            "refund_tx": 0, "refund_u": 0, "net_u": 0,
            "elig_u": 0, "cancel_u": 0,
        })
        acc["fa_u"] += int(r["first_attempt_users"])
        acc["fa_tx"] += int(r["first_attempt_tx"])
        acc["fa_succ_tx"] += int(r["first_attempt_succ_tx"])
        acc["total_tx"] += int(r["total_tx"])
        acc["succ_tx"] += int(r["succ_tx"])
        acc["att_u"] += int(r["attempted_users"])
        acc["succ_u"] += int(r["succ_users"])
        acc["refund_tx"] += int(r["refund_tx"])
        acc["refund_u"] += int(r["refund_users"])
        acc["net_u"] += int(r["net_users"])
        acc["elig_u"] += int(r["tbb_elig_users"])
        acc["cancel_u"] += int(r["tbb_cancel_users"])
        acc["tbb"] = max(acc["elig_u"] - acc["cancel_u"], 0)

    def _ratio(num, denom):
        # Returns None when the denominator is too small to be meaningful — the
        # graph will then show a gap for that point. Volume KPIs (which call
        # this with denom=None implicitly via direct assignment) are unaffected.
        if denom is None or denom == 0:
            return None
        if denom < min_denom_for_ratios:
            return None
        return num / denom

    rows = []
    for (t_key, c_val), agg in cells.items():
        def g0(k, _agg=agg):
            return _agg.get(k, 0)

        def gr(rx, k, _agg=agg):
            return _agg["rx"].get(rx, {}).get(k, 0)

        kpis = {
            "# R0 Attempts":            g0("r0_attempts"),
            "% Success Rate R0":        _ratio(g0("r0_succeeded"), g0("r0_attempts")),
            "# R0 Succeeded":           g0("r0_succeeded"),
            "% Unsub During Trial":     _ratio(g0("unsub_trial"), g0("r0_succeeded")),
            "# R1 To Be Billed":        g0("r1_tbb"),
            "% R1 Billed":              _ratio(gr("1", "fa_u"), g0("r1_tbb")),
            # R1
            "# R1 First Attempt (users)":   gr("1", "fa_u"),
            "% Success R1 First Attempt":   _ratio(gr("1", "fa_succ_tx"), gr("1", "fa_tx")),
            "# R1 Attempts (tx dedup)":     gr("1", "total_tx"),
            "% Success R1 Attempts":        _ratio(gr("1", "succ_tx"), gr("1", "total_tx")),
            "# R1 Succeeded (users)":       gr("1", "succ_u"),
            "% R1 Succeeded per User":      _ratio(gr("1", "succ_u"), gr("1", "att_u")),
            "% Churn Brut R0/R1":           _ratio(g0("r0_succeeded") - gr("1", "succ_u"), g0("r0_succeeded")),
            "# Refund R1":                  gr("1", "refund_tx"),
            "% Refund R1":                  _ratio(gr("1", "refund_tx"), gr("1", "succ_tx")),
            "% Churn Net R0/R1":            _ratio(
                g0("r0_succeeded") - gr("1", "net_u"),
                g0("r0_succeeded")),
        }
        # R2 / R3 / R4 — same formulas as in build_funnel_table.
        for rx in ["2", "3", "4"]:
            prev = str(int(rx) - 1)
            lbl = f"R{rx}"
            if gr(rx, "elig_u") == 0:
                # Skip this rx for this cell — KPIs not defined yet (no eligible)
                continue
            kpis[f"# {lbl} To Be Billed"]          = gr(rx, "tbb")
            kpis[f"% {lbl} Billed"]                = _ratio(gr(rx, "fa_u"), gr(rx, "tbb"))
            kpis[f"# {lbl} First Attempt (users)"] = gr(rx, "fa_u")
            kpis[f"% Success {lbl} First Attempt"] = _ratio(gr(rx, "fa_succ_tx"), gr(rx, "fa_tx"))
            kpis[f"# {lbl} Attempts (tx dedup)"]   = gr(rx, "total_tx")
            kpis[f"% Success {lbl} Attempts"]      = _ratio(gr(rx, "succ_tx"), gr(rx, "total_tx"))
            kpis[f"# {lbl} Succeeded (users)"]     = gr(rx, "succ_u")
            kpis[f"% {lbl} Succeeded per User"]    = _ratio(gr(rx, "succ_u"), gr(rx, "att_u"))
            kpis[f"% Churn Brut R{prev}/{lbl}"]    = _ratio(gr(prev, "succ_u") - gr(rx, "succ_u"), gr(prev, "succ_u"))
            kpis[f"# Refund {lbl}"]                = gr(rx, "refund_tx")
            kpis[f"% Refund {lbl}"]                = _ratio(gr(rx, "refund_tx"), gr(rx, "succ_tx"))
            # % Churn Net Rn/Rm = (Rn_net - Rm_net) / Rn_net
            # où Rk_net = net_u (succeeded ET ni refundé ni alerté — Net STRICT,
            # aligné Notion/Exec).
            _prev_net = gr(prev, "net_u")
            _rx_net   = gr(rx,   "net_u")
            kpis[f"% Churn Net R{prev}/{lbl}"]     = _ratio(_prev_net - _rx_net, _prev_net)

        # LTV Simulator — Booking only, requires curve_dim = price_booking
        # so the cell's c_val IS the price bucket.
        if brand_type == "Booking" and curve_dim_key == "price_booking" \
                and c_val in LTV_PRICE_CONFIG:
            _today_g = today or date.today()
            _picked_end_g = picked_end or _today_g
            # Cohort end = t_key + period span, CAPPED by picked_end_g.
            # (Si la sidebar plafonne la fenêtre, le LATEST user de la cohorte
            # est <= picked_end → permet à la cohorte du mois en cours d'être
            # mature dès que picked_end est >= trial_days avant today.)
            if time_dim_key == "date_day":
                _ce = t_key
            elif time_dim_key == "date_week":
                _ce = t_key + timedelta(days=6) if t_key else _picked_end_g
            elif time_dim_key == "date_month":
                if t_key:
                    if t_key.month == 12:
                        _ce = date(t_key.year + 1, 1, 1) - timedelta(days=1)
                    else:
                        _ce = date(t_key.year, t_key.month + 1, 1) - timedelta(days=1)
                else:
                    _ce = _picked_end_g
            else:
                _ce = _picked_end_g
            # Cap par picked_end (cf. _ltv_cohort_end_date)
            if _ce is not None and _picked_end_g is not None:
                _ce = min(_ce, _picked_end_g)
            _gdat = {"r0_succeeded": g0("r0_succeeded"), "rx": agg.get("rx", {})}
            # Sim Total (maturité naturelle) — réel obs R1..r_max_obs + proj
            _sim = _ltv_compute(_gdat, c_val, _ce, _today_g)
            if _sim is not None:
                # Anciens noms (compat Funnel Booking)
                kpis["ARPU brute (€)"] = _sim["arpu_brut_eur"]
                kpis["ARPU net (€)"]   = _sim["arpu_net_eur"]
                kpis["LTV brute (€)"]  = _sim["ltv_brut_eur"]
                kpis["LTV net (€)"]    = _sim["ltv_net_eur"]
                # Nouveaux noms (tab LTV — section Cohorte totale)
                kpis["ARPU réel cumulé brut (€)"] = _sim["arpu_brut_eur"]
                kpis["ARPU réel cumulé net (€)"]  = _sim["arpu_net_eur"]
                kpis["LTV brute totale (€)"]      = _sim["ltv_brut_eur"]
                kpis["LTV nette totale (€)"]      = _sim["ltv_net_eur"]
            # Sim R1-only (force r_max_obs=1) — projection naïve depuis R1
            _sim_r1 = _ltv_compute(_gdat, c_val, _ce, _today_g,
                                    r_max_obs_override=1)
            if _sim_r1 is not None:
                kpis["LTV brute R1 (€)"] = _sim_r1["ltv_brut_eur"]
                kpis["LTV nette R1 (€)"] = _sim_r1["ltv_net_eur"]
            # ARPU R1 = R1 NET users × prix / R0 (= revenue NET R1 par R0)
            _arpt = LTV_PRICE_CONFIG[c_val][2]
            _r0 = g0("r0_succeeded")
            if _r0 > 0:
                _r1_net = gr("1", "succ_u") - gr("1", "refund_u")
                kpis["ARPU R1 (€)"] = (_r1_net / _r0) * _arpt

        # Seuil 200 R0 — masque toute la cellule (KPI volume ET ratio) si
        # r0_succeeded < MIN_R0_DISPLAY. value=None → Plotly affiche un trou.
        if g0("r0_succeeded") < MIN_R0_DISPLAY:
            kpis = {k: None for k in kpis}

        t_label = cell_labels[(t_key, c_val)]
        for kpi_name, val in kpis.items():
            rows.append({
                "time": t_key,
                "time_label": t_label,
                "curve": c_val,
                "kpi": kpi_name,
                "value": val,
            })

    return pd.DataFrame(rows)


def render_funnel_graph(brand_type: str, filters: dict, picked_start, picked_end, key_prefix: str,
                         kpi_sections: list = None, all_kpis: list = None,
                         max_rx: int = 4,
                         max_rx_observed_dynamic: bool = False) -> None:
    """Render the "Évolution dans le temps" graph section under a funnel tab.

    Layout mirrors the funnel TABLE structure:
      Left  : KPI selector — sections (R0/Trial, R1, R2..R4) with one button
              per KPI, the active KPI highlighted (primary), inactive in
              secondary. Scrollable container if list is long.
      Right : Plotly line chart of the active KPI, one line per curve-dim value.

    Independent from sidebar dims / granularity (own selectors at the top of
    the section). Reuses sidebar filters + picked date range. Streamlit widgets
    get a unique `key_prefix` so we can have one graph per tab without
    state collision.

    Points with a tiny denominator (< 20) for RATIO KPIs are hidden — see the
    `min_denom_for_ratios` argument in funnel_graph_data.
    """
    import plotly.express as px  # lazy import — only when graph is rendered

    # Allow custom KPI sections/list (used by LTV tab to filter only LTV KPIs)
    _kpi_sections = kpi_sections if kpi_sections is not None else FUNNEL_KPI_SECTIONS
    _all_kpis = all_kpis if all_kpis is not None else ALL_FUNNEL_KPIS

    st.markdown("### 📈 Évolution dans le temps")

    # --- Top controls : granularité X + dim courbe ---------------------------
    ctrl1, ctrl2, _spacer = st.columns([1.5, 1.5, 4])
    time_options = ["Date (jour)", "Date (semaine)", "Date (mois)"]
    graph_time_label = ctrl1.selectbox(
        "Granularité X",
        options=time_options,
        index=1,  # Semaine par défaut
        key=f"{key_prefix}_graph_time",
        help="Granularité de l'axe X (indépendant de la sidebar)",
    )
    _NONE_GRAPH = "— aucune (agrégé) —"
    curve_options = [_NONE_GRAPH] + [d[1] for d in DIMENSION_DIMS if d[0] not in _DATE_DIM_KEYS]
    graph_curve_label = ctrl2.selectbox(
        "Dim courbe",
        options=curve_options,
        index=0,
        key=f"{key_prefix}_graph_curve",
        help="Une courbe par valeur de cette dim. « Aucune » = une seule courbe agrégée.",
    )

    graph_time_dim = next(d for d in DIMENSION_DIMS if d[1] == graph_time_label)
    if graph_curve_label != _NONE_GRAPH:
        graph_curve_dim = next(d for d in DIMENSION_DIMS if d[1] == graph_curve_label)
        graph_dims_list = [graph_time_dim, graph_curve_dim]
        curve_key = graph_curve_dim[0]
    else:
        graph_curve_dim = None
        graph_dims_list = [graph_time_dim]
        curve_key = None

    graph_weeks_list = periods_in_range(picked_start, picked_end, graph_time_dim[0])
    if not graph_weeks_list:
        st.info("Plage de dates vide pour cette granularité.")
        return

    # --- Data fetch ----------------------------------------------------------
    with st.spinner("Graphe…"):
        try:
            _gmax_rx_obs = (
                _dynamic_max_rx_observed(graph_weeks_list, today=date.today())
                if max_rx_observed_dynamic else 4
            )
            raw_df = run_query(funnel_sql(
                brand_type, filters, graph_dims_list, graph_weeks_list,
                max_rx=max_rx, max_rx_observed=_gmax_rx_obs,
                picked_end=picked_end,
            ))
            graph_df = funnel_graph_data(
                raw_df, graph_time_dim[0], curve_key,
                picked_end=picked_end, brand_type=brand_type,
            )
        except Exception as e:
            st.error(f"Erreur graphe : {e}")
            return

    if graph_df.empty:
        st.info("Aucune donnée pour ce périmètre.")
        return

    # Available KPIs = those with at least one non-null point.
    has_data = (
        graph_df.dropna(subset=["value"])
        .groupby("kpi")
        .size()
        .reset_index(name="n")
    )
    available = set(has_data["kpi"].tolist())
    if not available:
        st.info("Aucun KPI avec données sur cette plage (cohortes trop petites pour des ratios fiables, "
                "ou volumes tous nuls).")
        return

    # --- Selected KPI state --------------------------------------------------
    state_key = f"{key_prefix}_graph_kpi"
    # Initialise / repair the selection if missing or no longer available.
    if state_key not in st.session_state or st.session_state[state_key] not in available:
        # Default to "# R0 Succeeded" if possible, else first available.
        if "# R0 Succeeded" in available:
            st.session_state[state_key] = "# R0 Succeeded"
        elif "# R0 Attempts" in available:
            st.session_state[state_key] = "# R0 Attempts"
        else:
            ordered_available = [k for k in _all_kpis if k in available]
            st.session_state[state_key] = ordered_available[0] if ordered_available else None

    # --- Scoped CSS so KPI buttons look like compact radio entries -----------
    # Buttons get marked via a wrapper div with class `psp-kpi-buttons` so we
    # don't leak styles to other Streamlit buttons in the app.
    st.markdown(
        """
        <style>
          .psp-kpi-buttons div[data-testid="stVerticalBlock"] {
            gap: 2px;
          }
          .psp-kpi-buttons div.stButton > button {
            text-align: left;
            justify-content: flex-start;
            font-size: 12px;
            padding: 4px 10px;
            border-radius: 4px;
            font-weight: 400;
            min-height: 0;
            line-height: 1.3;
            width: 100%;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
          }
          .psp-kpi-buttons div.stButton > button p {
            font-size: 12px;
            margin: 0;
          }
          .psp-kpi-section-header {
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
            color: white;
            background: #0f172a;
            padding: 6px 10px;
            margin: 8px 0 4px 0;
            border-radius: 4px;
            letter-spacing: 0.04em;
          }
          .psp-kpi-section-header:first-child {
            margin-top: 0;
          }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # --- Layout : KPI buttons left, chart right ------------------------------
    col_kpi, col_chart = st.columns([1.6, 4.4])
    with col_kpi:
        st.markdown('<div class="psp-kpi-buttons">', unsafe_allow_html=True)
        with st.container(height=520, border=False):
            for section_name, section_kpis in _kpi_sections:
                visible_kpis = [k for k in section_kpis if k in available]
                if not visible_kpis:
                    continue
                st.markdown(
                    f'<div class="psp-kpi-section-header">{section_name}</div>',
                    unsafe_allow_html=True,
                )
                for kpi in visible_kpis:
                    is_active = (kpi == st.session_state[state_key])
                    if st.button(
                        kpi,
                        key=f"{key_prefix}_kpi_btn_{kpi}",
                        type="primary" if is_active else "secondary",
                        use_container_width=True,
                    ):
                        st.session_state[state_key] = kpi
                        st.rerun()  # refresh button styling immediately
        st.markdown('</div>', unsafe_allow_html=True)

    selected_kpi = st.session_state[state_key]

    # --- Chart ---------------------------------------------------------------
    with col_chart:
        plot_data = graph_df[graph_df["kpi"] == selected_kpi].copy()
        if plot_data.dropna(subset=["value"]).empty:
            st.info(f"Aucune donnée pour {selected_kpi} sur cette plage "
                    f"(possible : dénominateur < 20 sur tous les points).")
            return

        is_pct = selected_kpi.startswith("%")
        if is_pct:
            plot_data["value"] = plot_data["value"] * 100

        plot_data = plot_data.sort_values("time")

        # Colour palette : Plotly's "D3" mixed with a couple of vivid extras —
        # works well for up to ~15 curves. Beyond that the chart is busy but
        # the user explicitly said no plafond.
        palette = (
            px.colors.qualitative.D3
            + px.colors.qualitative.Set2
            + px.colors.qualitative.Set3
        )

        fig = px.line(
            plot_data,
            x="time",
            y="value",
            color="curve",
            markers=True,
            color_discrete_sequence=palette,
            hover_data={"time_label": True, "time": False, "value": ":.2f"},
            labels={
                "time": graph_time_label,
                "value": selected_kpi,
                "curve": graph_curve_label if graph_curve_dim else "Série",
            },
        )

        # Y axis : % style for ratios, thousands separator for volumes.
        if is_pct:
            fig.update_yaxes(ticksuffix=" %", tickformat=".1f")
        else:
            fig.update_yaxes(tickformat=",d", separatethousands=True)

        # Line + marker styling
        fig.update_traces(
            mode="lines+markers",
            line=dict(width=2.5),
            marker=dict(size=7, line=dict(width=1, color="white")),
            connectgaps=False,  # gap at None points (small-cohort threshold)
            hovertemplate=(
                "<b>%{fullData.name}</b><br>"
                + ("%{customdata[0]}<br>" if "time_label" in plot_data.columns else "")
                + f"{selected_kpi} : %{{y:,.2f}}"
                + (" %" if is_pct else "")
                + "<extra></extra>"
            ),
        )

        # Title block — clean, left-aligned
        fig.update_layout(
            title=dict(
                text=f"<b>{selected_kpi}</b>  ·  {brand_type}",
                x=0.0,
                xanchor="left",
                font=dict(size=15, color="#0f172a"),
            ),
            height=520,
            margin=dict(l=10, r=10, t=60, b=40),
            plot_bgcolor="white",
            paper_bgcolor="white",
            hovermode="x unified",
            xaxis=dict(
                showgrid=True, gridcolor="#f1f5f9", gridwidth=1,
                showline=True, linecolor="#cbd5e1", linewidth=1,
                ticks="outside", tickcolor="#94a3b8",
                title=dict(font=dict(size=12, color="#475569")),
            ),
            yaxis=dict(
                showgrid=True, gridcolor="#f1f5f9", gridwidth=1,
                showline=True, linecolor="#cbd5e1", linewidth=1,
                zeroline=True, zerolinecolor="#cbd5e1",
                ticks="outside", tickcolor="#94a3b8",
                title=dict(font=dict(size=12, color="#475569")),
                rangemode="tozero" if not is_pct else "normal",
            ),
            legend=dict(
                orientation="v",
                x=1.02, y=1, xanchor="left",
                bgcolor="rgba(255,255,255,0.9)",
                bordercolor="#e2e8f0", borderwidth=1,
                font=dict(size=11),
            ),
            font=dict(family="-apple-system, BlinkMacSystemFont, sans-serif"),
        )

        st.plotly_chart(fig, use_container_width=True)
        st.caption(
            "💡 Points masqués : sur les **ratios**, les cellules dont le dénominateur "
            "< 20 sont laissées vides (cohorte trop petite pour être lisible)."
        )


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
    # LTV Simulator
    "ARPU brute (€)",
    "ARPU net (€)",
    "LTV brute (€)",
    "LTV net (€)",
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

# --- Exec Summary : dimension Conciergerie × PSP réel ----------------------
# Les colonnes ne sont plus des paires hardcodées mais (conciergerie, psp_réel)
# présentes dans la data, filtrées par un seuil de CA encaissé (brut s1).
_EXEC_CONC_ORDER = ["Reserv-Go", "Book-Ici", "Resadexa", "Concimax",
                    "Jumpaide", "Rapidoxy", "Rezaflash"]
_EXEC_PSP_LABELS = {
    "trustpayment": "Trustpayment", "pixxles": "Pixxles",
    "labanquepostale": "La Banque Postale", "emerchantpay": "eMerchantPay",
    "EMS": "EMS", "Kadima": "Kadima", "Cliq": "Cliq", "CASH": "CASH",
}
_EXEC_BRUT_MIN = 10000.0  # une colonne s'affiche si brut s1 >= ce seuil (€ encaissés)

# CSS de base partagé par les DEUX onglets Exec (Summary + Billing). Émis par
# chaque rendu (avec la nav par page, chaque page doit porter son propre CSS).
# Conteneur scrollable horizontal + colonne KPI sticky à gauche + largeur min
# des colonnes -> on voit toujours toutes les colonnes même quand il y en a
# beaucoup (dimension Conciergerie × PSP réel dynamique).
_EXEC_BASE_CSS = """
    <style>
      .exec-summary { font-size: 14px; }
      .exec-period {
        background: #fef3c7; border-left: 4px solid #f59e0b; padding: 8px 12px;
        border-radius: 4px; margin-bottom: 16px; font-size: 13px;
      }
      .exec-section {
        font-size: 16px; font-weight: 700; color: #0f172a; margin: 20px 0 10px 0;
        padding-bottom: 6px; border-bottom: 2px solid #e2e8f0;
      }
      .exec-cards { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 8px; }
      .exec-card {
        background: white; border: 1px solid #cbd5e1; border-radius: 8px;
        padding: 14px 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.04);
      }
      .exec-card-head { font-size: 12px; font-weight: 700; text-transform: uppercase; color: #475569; letter-spacing: 0.05em; }
      .exec-card-sub { font-size: 11px; color: #94a3b8; margin-top: 2px; margin-bottom: 8px; }
      .exec-card-value { font-size: 30px; font-weight: 700; color: #0f172a; line-height: 1.1; }
      .exec-card-foot { font-size: 12px; color: #64748b; margin-top: 6px; }
      /* Conteneur scrollable horizontal (porte la bordure/radius) */
      .exec-scroll {
        overflow-x: auto; max-width: 100%;
        border: 1px solid #cbd5e1; border-radius: 8px; background: white;
      }
      .exec-table {
        border-collapse: separate; border-spacing: 0; width: 100%;
        background: white; font-size: 13px;
      }
      .exec-table thead th {
        background: #0f172a; color: white; padding: 10px 12px; text-align: center;
        font-weight: 600; border-bottom: 2px solid #1e293b;
        min-width: 96px; white-space: nowrap;
      }
      .exec-table thead th.exec-kpi-label {
        text-align: left; background: #1e293b; left: 0; position: sticky; z-index: 3;
        min-width: 210px;
      }
      .exec-th-conc { font-size: 13px; font-weight: 700; }
      .exec-th-total { border-left: 3px solid #475569 !important; background: #1e293b !important; }
      .exec-total-cell { border-left: 3px solid #475569 !important; background: #f1f5f9 !important; font-weight: 700; }
      .exec-total-cell .exec-cell-val { font-size: 15px !important; font-weight: 700 !important; }
      .exec-th-psp  { font-size: 11px; opacity: 0.75; font-weight: 400; margin-top: 2px; }
      .exec-table tbody td {
        padding: 10px 12px; text-align: center; border-bottom: 1px solid #e2e8f0;
        vertical-align: middle; min-width: 96px; white-space: nowrap;
      }
      .exec-table tbody tr:last-child td { border-bottom: none; }
      .exec-table tbody tr:nth-child(even) td { background: #f8fafc; }
      .exec-kpi-label {
        text-align: left !important; font-weight: 700; color: #0f172a;
        background: #f1f5f9 !important; position: sticky; left: 0; z-index: 2;
        min-width: 210px;
      }
      .exec-cell-val { font-size: 14px; font-weight: 600; color: #0f172a; }
      .exec-cell-wow { font-size: 11px; margin-top: 3px; }
      .exec-wow-good    { color: #059669; font-weight: 600; }
      .exec-wow-bad     { color: #dc2626; font-weight: 600; }
      .exec-wow-neutral { color: #94a3b8; }
    </style>
    """


def _exec_psp_reel(psp_col: str, mid_alias: str) -> str:
    """PSP réel pour l'Exec = même découpage que la dimension brand_psp : hors NMI
    = ms_default_psp ; NMI = EMS / Kadima / Cliq / CASH selon le MidId du R0. Le
    fallback NMI (MidId non résolu) = EMS (cf. ELA). Plus de bucket 'autres'."""
    return (
        f"CASE WHEN {psp_col} <> 'nmi' THEN {psp_col} "
        f"WHEN {mid_alias}.MidId = '688b5f4e-4f33-4b16-b2c7-6c601ba15306' THEN 'EMS' "
        f"WHEN {mid_alias}.MidId IN ('5f915cec-f0b3-40e9-9908-b3590b791448', "
        f"'f6130732-c577-4d1d-9ab9-802900b478a0') THEN 'Kadima' "
        f"WHEN {mid_alias}.MidId = '4a7af99e-20b3-48b3-8e93-6fc39f8012b0' THEN 'Cliq' "
        f"WHEN {mid_alias}.MidId = '9cf8e38c-c719-4d33-a1e3-aaa72cf88cdd' THEN 'CASH' "
        f"ELSE 'EMS' END"
    )


def _exec_psp_label(psp: str) -> str:
    return _EXEC_PSP_LABELS.get(psp, psp)


def _exec_all_pairs(data: dict) -> set:
    """Toutes les paires (conciergerie, psp) présentes dans data (pour les cards)."""
    return {(c, p) for (c, p, _b) in data}


def _exec_visible_pairs(data: dict, brut_metric: str = "brut",
                        min_brut: float = _EXEC_BRUT_MIN) -> list:
    """Paires (conciergerie, psp) à AFFICHER en colonnes = CA s1 >= seuil, triées
    par ordre conciergerie puis psp. brut_metric = 'brut' (Exec) ou 'ca_brut'
    (Billing)."""
    def s1brut(c, p):
        return data.get((c, p, "s1"), {}).get(brut_metric, 0.0)
    vis = [(c, p) for (c, p) in _exec_all_pairs(data) if s1brut(c, p) >= min_brut]
    return sorted(vis, key=lambda cp: (
        _EXEC_CONC_ORDER.index(cp[0]) if cp[0] in _EXEC_CONC_ORDER else 99, cp[1]))


def exec_summary_sql(period_start: date, period_end: date) -> str:
    """Build SQL for the Executive Summary tab.

    Args:
      period_start : 1st day of the period to analyse (s1_start).
      period_end   : last day of the period (s1_end). For MTD = yesterday.
                     For a full past month = last day of that month.

    Returns rows of (conciergerie, psp, bucket, metric, value):
      - conciergerie ∈ {Reserv-Go, Book-Ici, Resadexa, Rezaflash, Jumpaide,
                        Concimax, Rapidoxy}
      - psp = LOWER(ms_default_psp) (e.g. 'trustpayment', 'pixxles', 'nmi')
      - bucket ∈ {'s1' (selected period), 's2' (previous month same window)}
      - metric ∈ {r0, churn_cohort_size, r1_net_count, brut, refund_rev,
                  tx_succ_visa, alerts_visa}
      - value FLOAT64

    Window is parameterised by the caller (typically driven by a month
    selector in the Exec Summary tab). Ignores sidebar dims/filters by design.

      - s1 = selected period (e.g. MTD courant OR mois complet passé)
      - s2 = same window one month earlier (DATE_SUB INTERVAL 1 MONTH on
             period_start and period_end — BQ clips to last valid day of
             the prev month if needed)

    KPIs derived in Python from these atoms (see render_exec_summary):
      - R0                = r0                            (customer-level count, not membership)
      - CA Net            = brut - refund_rev             (brut by t_date; refund by refunded_at_utc)
      - % Churn R0→R1 net = 1 - (r1_net_count / churn_cohort_size)  (Booking ONLY)
      - % Refund          = refund_rev / brut             (CA-based, dates differentiated)
      - % VAMP Ratio      = alerts_visa / tx_succ_visa    (VISA only — VAMP = Visa Acquirer Monitoring Program)

    IMPORTANT — Churn cohort definition (≠ TBB), ALIGNÉE SUR LE FUNNEL :
      Le dénominateur du % Churn R0→R1 est la cohorte d'ACQUISITION (signup,
      ms_date), niveau CUSTOMER (customer_id), Booking only — même logique que
      r0_succeeded du funnel. Il INCLUT les cancelled-during-trial (= vrais
      churners). GARDE-FOU de maturité : on ne garde que les customers dont
      l'issue R0→R1 est tranchée — trial terminé (ms_trial_end <= aujourd'hui)
      OU déjà cancellé. Les customers encore en trial actif sont exclus pour
      ne pas surestimer le churn du mois en cours (inscrits de fin de période
      qui n'ont pas encore pu atteindre R1).

      This is DIFFERENT from the funnel table's "# R1 To Be Billed" (TBB),
      which is an audit metric (\"who SHOULD have been billed by now?\") and
      legitimately excludes cancelled-during-trial. Don't confuse the two.

    Date semantics:
      - r0                : by ms_date (signup date)
      - churn_cohort_size : by ms_date (signup), garde-fou trial terminé/cancellé
      - brut              : by t_date (transaction date) for status='succeeded' tx (all cards)
      - refund_rev        : by refunded_at_utc (refund date), independent of t_date (all cards)
      - tx_succ_visa      : by t_date (transaction date), VISA + DELTA only
      - alerts_visa       : by alerted_at (alert date), cardnetwork='Visa' only

    Card scope:
      - r0 / brut / refund_rev : all card brands (R0, CA, Refund are global metrics)
      - tx_succ_visa / alerts_visa : Visa only (DELTA = Visa Debit UK, same network)
    """
    # Conciergerie canonical mapping — same logic as the global
    # CONCIERGERIE_EXPR_FM/FT: take SPLIT(brand, ' - ')[0] to collapse
    # both magazine variants and MID suffixes into one canonical name.
    def _conc(alias: str, col: str) -> str:
        return (
            f"CASE LOWER(SPLIT(COALESCE({alias}.{col}, ''), ' - ')[OFFSET(0)]) "
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
    # PSP réel (NMI -> EMS/Kadima/Cliq/CASH via MidId du R0). Alias 'rm'.
    psp_fm = _exec_psp_reel("fm.ms_default_psp", "rm")
    psp_ft = _exec_psp_reel("ft.ms_default_psp", "rm")
    psp_t  = _exec_psp_reel("t.ms_default_psp",  "rm")

    # Les bornes s2 sont calculées côté Python par _exec_period_bounds()
    # pour gérer correctement le cas "mois complet" vs "MTD partiel".
    s1_start, s1_end, s2_start, s2_end = _exec_period_bounds(period_start, period_end)
    s1s, s1e, s2s, s2e = (d.isoformat() for d in (s1_start, s1_end, s2_start, s2_end))

    return f"""
WITH weeks_def AS (
  -- s1 = période sélectionnée (MTD courant ou mois passé complet)
  -- s2 = comparaison M-1 (calculée Python : mois complet précédent si s1
  --      est un mois complet, sinon décalage day-of-month range).
  SELECT
    DATE '{s1s}' AS s1_start,
    DATE '{s1e}' AS s1_end,
    DATE '{s2s}' AS s2_start,
    DATE '{s2e}' AS s2_end
),
r0_mid_map AS (
  -- MidId du R0 NMI par membership (borné sur la fenêtre exec). Sert à
  -- résoudre le PSP réel (EMS/Kadima/Cliq/CASH) pour le NMI.
  SELECT f.membership_id, ANY_VALUE(s.MidId) AS MidId
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` f
  JOIN `eu-andy-marketing-raw.silver_sgw.stg_transactions` s ON f.transaction_id = s.Id
  WHERE f.t_psp_name = 'nmi' AND f.invoice_r_index = '0'
    AND f.t_date BETWEEN DATE_SUB((SELECT s2_start FROM weeks_def), INTERVAL 15 DAY)
                     AND DATE_ADD((SELECT s1_end FROM weeks_def), INTERVAL 15 DAY)
  GROUP BY f.membership_id
),
fm_in_window AS (
  SELECT
    fm.membership_id,
    fm.customer_id,
    fm.brand_type,
    fm.ms_status,
    fm.ms_date,
    fm.ms_trial_end,
    {psp_fm} AS psp,
    COALESCE(fm.ms_cancelled_during_trial, FALSE) AS cancelled_during_trial,
    {fm_conc} AS conciergerie
  FROM `eu-andy-marketing-raw.dashboard.fact_memberships` fm
  LEFT JOIN r0_mid_map rm ON rm.membership_id = fm.membership_id
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
churn_cohort AS (
  -- % Churn R0→R1 (Booking) — ALIGNÉ SUR LE FUNNEL.
  -- Dénominateur = CUSTOMERS (customer_id) dont le SIGNUP (ms_date) tombe
  -- dans s1/s2 — même cohorte d'acquisition que le funnel, niveau customer.
  --   * BOOKING ONLY (conversion du sub Booking, trial → billed R1).
  --   * Exclut paused/abandonned/processing (comme r0_succeeded du funnel).
  --   * INCLUT les cancelled-during-trial (= vrais churners).
  --   * GARDE-FOU maturité : on ne garde que les customers dont l'issue
  --     R0→R1 est tranchée — trial terminé (ms_trial_end <= aujourd'hui) OU
  --     déjà cancellé. Les customers encore en trial actif (issue non
  --     tranchée) sont exclus, sinon le churn du mois en cours serait
  --     surestimé par les inscrits de fin de période.
  --
  -- Distinct du "# R1 To Be Billed" (TBB) du funnel, mesure d'audit billing
  -- qui exclut, elle, les cancelled-during-trial. Ne pas confondre.
  SELECT conciergerie, psp, bucket, customer_id,
    MAX(IF(r1.membership_id IS NOT NULL, 1, 0)) AS has_r1_net
  FROM (
    SELECT fm.conciergerie, fm.psp, fm.customer_id, fm.membership_id,
      CASE
        WHEN fm.ms_date BETWEEN (SELECT s1_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def) THEN 's1'
        WHEN fm.ms_date BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s2_end FROM weeks_def) THEN 's2'
      END AS bucket
    FROM fm_in_window fm
    WHERE fm.conciergerie IS NOT NULL
      AND fm.brand_type = 'Booking'
      AND fm.ms_status NOT IN ('abandonned', 'processing', 'paused')
      AND fm.ms_date BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def)
      AND (fm.ms_trial_end <= CURRENT_DATE() OR fm.cancelled_during_trial = TRUE)
  ) fmb
  LEFT JOIN (
    SELECT DISTINCT membership_id
    FROM `eu-andy-marketing-raw.dashboard.fact_transactions`
    WHERE invoice_r_index = '1'
      AND transaction_status = 'succeeded'
      AND is_refunded = FALSE
      AND is_alerted = FALSE
      AND brand_type = 'Booking'
  ) r1 ON r1.membership_id = fmb.membership_id
  GROUP BY 1, 2, 3, 4
),
churn_agg AS (
  -- COUNT(*) sur les lignes groupées par customer = nb de customers de la cohorte
  SELECT conciergerie, psp, bucket, 'churn_cohort_size' AS metric,
    CAST(COUNT(*) AS FLOAT64) AS value
  FROM churn_cohort GROUP BY 1, 2, 3
  UNION ALL
  SELECT conciergerie, psp, bucket, 'r1_net_count' AS metric,
    CAST(SUM(has_r1_net) AS FLOAT64) AS value
  FROM churn_cohort GROUP BY 1, 2, 3
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
    {psp_ft} AS psp,
    {ft_conc} AS conciergerie,
    CASE
      WHEN ft.t_date BETWEEN (SELECT s1_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def) THEN 's1'
      WHEN ft.t_date BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s2_end FROM weeks_def) THEN 's2'
    END AS bucket
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ft
  LEFT JOIN r0_mid_map rm ON rm.membership_id = ft.membership_id
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
    {psp_ft} AS psp,
    {ft_conc} AS conciergerie,
    CASE
      WHEN ft.refunded_at_utc BETWEEN (SELECT s1_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def) THEN 's1'
      WHEN ft.refunded_at_utc BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s2_end FROM weeks_def) THEN 's2'
    END AS bucket
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ft
  LEFT JOIN r0_mid_map rm ON rm.membership_id = ft.membership_id
  WHERE ft.is_refunded = TRUE
    AND ft.refunded_at_utc BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def)
),
ft_visa_in_window AS (
  -- Visa-only succeeded tx — used as VAMP denominator. Includes DELTA
  -- (Visa Debit UK, same network). All R indexes counted.
  SELECT ft.transaction_id, ft.t_date, {psp_ft} AS psp,
    {ft_conc} AS conciergerie,
    CASE
      WHEN ft.t_date BETWEEN (SELECT s1_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def) THEN 's1'
      WHEN ft.t_date BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s2_end FROM weeks_def) THEN 's2'
    END AS bucket
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ft
  LEFT JOIN r0_mid_map rm ON rm.membership_id = ft.membership_id
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
    {psp_t} AS psp,
    t.invoice_r_index,
    CASE
      WHEN fa.alerted_at BETWEEN (SELECT s1_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def) THEN 's1'
      WHEN fa.alerted_at BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s2_end FROM weeks_def) THEN 's2'
    END AS bucket
  FROM `eu-andy-marketing-raw.dashboard.fact_alert` fa
  JOIN `eu-andy-marketing-raw.dashboard.fact_transactions` t USING (transaction_id)
  LEFT JOIN r0_mid_map rm ON rm.membership_id = t.membership_id
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


_FR_MONTHS = {
    1: "Janvier", 2: "Février", 3: "Mars", 4: "Avril",
    5: "Mai", 6: "Juin", 7: "Juillet", 8: "Août",
    9: "Septembre", 10: "Octobre", 11: "Novembre", 12: "Décembre",
}


def _fr_month_year(d: date) -> str:
    """FR label for a month: 'Mai 2026'."""
    return f"{_FR_MONTHS[d.month]} {d.year}"


def _months_for_exec_selector(n: int = 12) -> list:
    """Return list of (label, period_start, period_end) for the Exec Summary
    month selector.

      - Index 0 = mois courant en MTD (1er → hier).
      - Index 1+ = mois précédents complets (1er → dernier jour).

    Returns up to n entries (default 12). Latest first."""
    today = date.today()
    cur_first = today.replace(day=1)
    out = []
    for i in range(n):
        if i == 0:
            p_start = cur_first
            p_end = today - timedelta(days=1)
            if p_end < p_start:
                # On est le 1er du mois → MTD = juste aujourd'hui ; on skip
                # cette entrée et on commencera par le mois passé complet.
                continue
            label = (
                f"{_fr_month_year(p_start)} — MTD "
                f"({p_start.strftime('%d/%m')} → {p_end.strftime('%d/%m')})"
            )
        else:
            # i mois en arrière depuis le 1er du mois courant
            year = cur_first.year
            month = cur_first.month - i
            while month <= 0:
                month += 12
                year -= 1
            p_start = date(year, month, 1)
            if month == 12:
                next_first = date(year + 1, 1, 1)
            else:
                next_first = date(year, month + 1, 1)
            p_end = next_first - timedelta(days=1)
            label = f"{_fr_month_year(p_start)} (mois complet)"
        out.append((label, p_start, p_end))
    return out


def _sub_month_clip(d: date) -> date:
    """Soustrait 1 mois à `d`, en clippant au dernier jour valide du mois cible.
    Ex: 31 mars → 28/29 février ; 30 mars → 28/29 février ; 15 mars → 15 février.
    """
    year = d.year
    month = d.month - 1
    if month == 0:
        month = 12
        year -= 1
    if month == 12:
        next_first = date(year + 1, 1, 1)
    else:
        next_first = date(year, month + 1, 1)
    last_day_of_target = (next_first - timedelta(days=1)).day
    return date(year, month, min(d.day, last_day_of_target))


def _last_day_of_month(d: date) -> date:
    """Dernier jour du mois de `d`."""
    if d.month == 12:
        next_first = date(d.year + 1, 1, 1)
    else:
        next_first = date(d.year, d.month + 1, 1)
    return next_first - timedelta(days=1)


def _exec_period_bounds(period_start: date, period_end: date) -> tuple:
    """Retourne (s1_start, s1_end, s2_start, s2_end) pour la fenêtre choisie.

      - s1 = période sélectionnée (passée en arg)
      - s2 = comparaison M-1 :
            * Si s1 est un mois complet (1er → dernier jour) :
                s2 = mois précédent complet (1er → dernier jour de M-1)
                Ex: Avril 1-30 → Mars 1-31 (pas Mars 1-30)
            * Sinon (MTD ou autre fenêtre partielle) :
                s2 = même day-of-month range décalé d'1 mois (DATE_SUB INTERVAL 1 MONTH)
                Ex: 1-25 mai → 1-25 avril
    """
    s1_last_day_of_month = _last_day_of_month(period_start)
    is_full_month = (period_start.day == 1 and period_end == s1_last_day_of_month)

    if is_full_month:
        # M-1 = mois complet précédent
        if period_start.month == 1:
            s2_start = date(period_start.year - 1, 12, 1)
        else:
            s2_start = date(period_start.year, period_start.month - 1, 1)
        s2_end = _last_day_of_month(s2_start)
    else:
        # MTD ou fenêtre partielle : même day-of-month range décalé d'1 mois
        s2_start = _sub_month_clip(period_start)
        s2_end = _sub_month_clip(period_end)
    return period_start, period_end, s2_start, s2_end


def render_exec_summary(df: pd.DataFrame, period_start: date, period_end: date) -> str:
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

    # Colonnes dynamiques (conciergerie × psp réel). pairs = affichées (brut s1
    # >= seuil) ; all_pairs = toutes (pour les cards société). Format (c, p, label).
    pairs = [(c, p, _exec_psp_label(p)) for (c, p) in _exec_visible_pairs(data)]
    all_pairs = [(c, p, _exec_psp_label(p)) for (c, p) in _exec_all_pairs(data)]

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
        s1 = sum(m(c, p, "s1", "alerts_visa") for (c, p, _l) in all_pairs if c in conciergeries)
        s2 = sum(m(c, p, "s2", "alerts_visa") for (c, p, _l) in all_pairs if c in conciergeries)
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

    # --- Bottom: KPI table (colonne Total à droite) ---
    header_cells = "".join(
        "<th>"
        f"<div class='exec-th-conc'>{c}</div>"
        f"<div class='exec-th-psp'>({lbl})</div>"
        "</th>"
        for (c, p, lbl) in pairs
    )
    header_cells += f"<th class='exec-th-total'><div class='exec-th-conc'>Total</div><div class='exec-th-psp'>({len(pairs)} colonnes)</div></th>"

    def kpi_row(label, fmt, compute_fn, total_fn=None, lower_is_better=False):
        cells = []
        for (c, p, _lbl) in pairs:
            v1 = compute_fn(c, p, "s1")
            v2 = compute_fn(c, p, "s2")
            value_str = fmt(v1) if v1 is not None else "—"
            delta_str = wow_html(wow_pct(v1, v2) if (v1 is not None and v2 is not None) else None,
                                 lower_is_better=lower_is_better)
            cells.append(
                f"<td><div class='exec-cell-val'>{value_str}</div>"
                f"<div class='exec-cell-wow'>{delta_str}</div></td>"
            )
        # Cellule Total (recalcule depuis les atomes pour les ratios — somme
        # des numérateurs / somme des dénominateurs, jamais moyenne de ratios).
        if total_fn is not None:
            t1 = total_fn("s1")
            t2 = total_fn("s2")
            t_value_str = fmt(t1) if t1 is not None else "—"
            t_delta_str = wow_html(wow_pct(t1, t2) if (t1 is not None and t2 is not None) else None,
                                   lower_is_better=lower_is_better)
            cells.append(
                f"<td class='exec-total-cell'>"
                f"<div class='exec-cell-val'>{t_value_str}</div>"
                f"<div class='exec-cell-wow'>{t_delta_str}</div></td>"
            )
        return f"<tr><td class='exec-kpi-label'>{label}</td>{''.join(cells)}</tr>"

    # ---- Compute fns par cellule (existants) ------------------------------
    def churn_fn(c, p, b):
        # Churn cohort = customers Booking signés-up (ms_date) ∈ fenêtre,
        # garde-fou maturité (trial terminé OU cancellé). Aligné funnel.
        # INCLUT les cancelled-during-trial (= vrais churners). PAS un TBB.
        cohort = m(c, p, b, "churn_cohort_size")
        r1     = m(c, p, b, "r1_net_count")
        return None if cohort == 0 else 1 - (r1 / cohort)

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

    # ---- Total fns (agrégation correcte par KPI) --------------------------
    def _sum_atom(b, atom):
        return sum(m(c, p, b, atom) for (c, p, _l) in pairs)

    def total_r0(b):
        return _sum_atom(b, "r0")

    def total_ca_net(b):
        return _sum_atom(b, "brut") - _sum_atom(b, "refund_rev")

    def total_churn(b):
        # SOMME des r1_net / SOMME des cohorts (pas moyenne de ratios !)
        cohort_sum = _sum_atom(b, "churn_cohort_size")
        r1_sum     = _sum_atom(b, "r1_net_count")
        return None if cohort_sum == 0 else 1 - (r1_sum / cohort_sum)

    def total_refund(b):
        brut_sum   = _sum_atom(b, "brut")
        refund_sum = _sum_atom(b, "refund_rev")
        return None if brut_sum == 0 else refund_sum / brut_sum

    def total_vamp(b):
        denom_sum = _sum_atom(b, "tx_succ_visa")
        num_sum   = _sum_atom(b, "alerts_visa")
        return None if denom_sum == 0 else num_sum / denom_sum

    rows_html = "".join([
        kpi_row("# R0 (customers)",        fr_int, lambda c, p, b: m(c, p, b, "r0"),                                total_fn=total_r0,     lower_is_better=False),
        kpi_row("€ CA Net",                fr_eur, lambda c, p, b: m(c, p, b, "brut") - m(c, p, b, "refund_rev"),    total_fn=total_ca_net, lower_is_better=False),
        kpi_row("% Churn R0→R1 (Booking)", fr_pct, churn_fn,                                                          total_fn=total_churn,  lower_is_better=True),
        kpi_row("% Refund (CA)",           fr_pct, refund_fn,                                                         total_fn=total_refund, lower_is_better=True),
        kpi_row("% VAMP Ratio (Visa)",     fr_pct, vamp_fn,                                                           total_fn=total_vamp,   lower_is_better=True),
    ])

    table_html = (
        "<table class='exec-table'>"
        f"<thead><tr><th class='exec-kpi-label'>KPI</th>{header_cells}</tr></thead>"
        f"<tbody>{rows_html}</tbody>"
        "</table>"
    )

    # Labels période — utilise les bornes calculées côté Python (match BQ).
    s1_start, s1_end, s2_start, s2_end = _exec_period_bounds(period_start, period_end)
    today = date.today()
    is_mtd = (s1_end >= today - timedelta(days=1) and s1_start.day == 1
              and s1_start.month == today.month and s1_start.year == today.year)
    if is_mtd:
        s1_label = f"<b>MTD {_fr_month_year(s1_start)} :</b> {s1_start.strftime('%d/%m')} → {s1_end.strftime('%d/%m')}"
        s2_label = f"<b>MTD M-1 :</b> {s2_start.strftime('%d/%m')} → {s2_end.strftime('%d/%m')}"
    else:
        s1_label = f"<b>{_fr_month_year(s1_start)} (complet) :</b> {s1_start.strftime('%d/%m')} → {s1_end.strftime('%d/%m')}"
        s2_label = f"<b>{_fr_month_year(s2_start)} (M-1) :</b> {s2_start.strftime('%d/%m')} → {s2_end.strftime('%d/%m')}"
    period_label = f"{s1_label} &middot; {s2_label}"

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
      .exec-th-total {
        border-left: 3px solid #475569 !important;
        background: #1e293b !important;
      }
      .exec-total-cell {
        border-left: 3px solid #475569 !important;
        background: #f1f5f9 !important;
        font-weight: 700;
      }
      .exec-total-cell .exec-cell-val {
        font-size: 15px !important;
        font-weight: 700 !important;
      }
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
    css = _EXEC_BASE_CSS  # CSS partagé (scroll horizontal + colonne KPI sticky)

    return (
        css
        + "<div class='exec-summary'>"
        + f"<div class='exec-period'>📅 {period_label} (Month-to-Date · indépendant des filtres de la sidebar)</div>"
        + "<h3 class='exec-section'>Alertes Visa par société — MTD (hors Order Insight)</h3>"
        + cards_html
        + "<h3 class='exec-section'>KPI MTD par Conciergerie × PSP réel</h3>"
        + "<div class='exec-scroll'>" + table_html + "</div>"
        + "</div>"
    )


# ---------------------------------------------------------------------------
# Executive Summary BILLING — onglet par semaine (vs S-1)
#
# Layout: 3 sections colorées (Processing / Success Rate / VAMP), 7 colonnes
# Conciergerie × PSP + Total à droite. Sélecteur de semaine dédié.
# ---------------------------------------------------------------------------

def _weeks_for_billing_selector(n: int = 12) -> list:
    """Retourne (label, week_start, week_end) pour les n dernières semaines.

      - Idx 0 = semaine courante en WTD (lundi → hier) si on est pas lundi
      - Idx 1+ = semaines complètes précédentes (lundi → dimanche)

    Week labels FR: 'S21 (18/05 → 24/05)'."""
    today = date.today()
    # Lundi de la semaine courante
    cur_monday = today - timedelta(days=today.weekday())
    out = []
    for i in range(n):
        if i == 0:
            # Semaine courante en WTD
            w_start = cur_monday
            w_end = today - timedelta(days=1)
            if w_end < w_start:
                # On est lundi → WTD = 0 jour. Skip.
                continue
            iso_week = w_start.isocalendar()[1]
            label = (
                f"S{iso_week:02d} — WTD ({w_start.strftime('%d/%m')} "
                f"→ {w_end.strftime('%d/%m')})"
            )
        else:
            # Semaine complète : i semaines en arrière
            w_start = cur_monday - timedelta(days=7 * i)
            w_end = w_start + timedelta(days=6)
            iso_week = w_start.isocalendar()[1]
            label = (
                f"S{iso_week:02d} ({w_start.strftime('%d/%m')} "
                f"→ {w_end.strftime('%d/%m')}) — complète"
            )
        out.append((label, w_start, w_end))
    return out


def _exec_billing_period_bounds(week_start: date, week_end: date) -> tuple:
    """Bornes (s1_start, s1_end, s2_start, s2_end) pour la semaine sélectionnée
    et la semaine précédente (même nombre de jours pour les WTD).
    """
    days_in_window = (week_end - week_start).days + 1
    s2_start = week_start - timedelta(days=7)
    s2_end = s2_start + timedelta(days=days_in_window - 1)
    return week_start, week_end, s2_start, s2_end


def exec_quarter_sql(period_start: date, period_end: date) -> str:
    """Exec agrégé par PSP (conciergeries regroupées), sur 3 mois glissants
    finissant au mois sélectionné : m3 = sélection (possiblement MTD), m2/m1 = 2
    mois complets précédents. NMI regroupé en un seul 'NMI' (pas d'éclatement
    processeur -> pas besoin du MidId map). Mêmes atomes/dates que
    exec_summary_sql ; ratios recomposés au rendu (somme num./dénom.)."""
    m3s = period_start.replace(day=1)
    m2s = (m3s - timedelta(days=1)).replace(day=1)
    m1s = (m2s - timedelta(days=1)).replace(day=1)
    m1, m2, m3, p_end = (d.isoformat() for d in (m1s, m2s, m3s, period_end))

    def _psp(col):
        # NMI (toutes MID) regroupé ; hors NMI = ms_default_psp brut.
        return f"CASE WHEN {col} = 'nmi' THEN 'NMI' ELSE {col} END"
    psp_fm, psp_ft, psp_t = (_psp("fm.ms_default_psp"),
                             _psp("ft.ms_default_psp"), _psp("t.ms_default_psp"))

    def _conc(alias, col):  # sert juste à exclure helpprio / marques inconnues
        return (f"CASE LOWER(SPLIT(COALESCE({alias}.{col}, ''), ' - ')[OFFSET(0)]) "
                "WHEN 'reserv-go' THEN 'x' WHEN 'book-ici' THEN 'x' "
                "WHEN 'resadexa' THEN 'x' WHEN 'rezaflash' THEN 'x' "
                "WHEN 'jumpaide.com' THEN 'x' WHEN 'concimax' THEN 'x' "
                "WHEN 'rapidoxy' THEN 'x' ELSE NULL END")
    fm_conc, ft_conc, t_conc = (_conc("fm", "brand"),
                                _conc("ft", "t_brand"), _conc("t", "t_brand"))

    def _bucket(datecol):
        return (f"CASE DATE_TRUNC({datecol}, MONTH) "
                f"WHEN DATE '{m1}' THEN 'm1' WHEN DATE '{m2}' THEN 'm2' "
                f"WHEN DATE '{m3}' THEN 'm3' END")

    return f"""
WITH bounds AS (SELECT DATE '{m1}' AS scan_start, DATE '{p_end}' AS scan_end),
fm_in_window AS (
  SELECT fm.membership_id, fm.customer_id, fm.brand_type, fm.ms_status, fm.ms_date,
    fm.ms_trial_end, {psp_fm} AS psp,
    COALESCE(fm.ms_cancelled_during_trial, FALSE) AS cancelled_during_trial,
    {fm_conc} AS conc
  FROM `eu-andy-marketing-raw.dashboard.fact_memberships` fm CROSS JOIN bounds
  WHERE fm.ms_status NOT IN ('abandonned','processing')
    AND ((fm.ms_date BETWEEN scan_start AND scan_end)
      OR (fm.ms_trial_end BETWEEN scan_start AND scan_end))
),
r0 AS (
  SELECT psp, {_bucket('ms_date')} AS bucket, 'r0' AS metric,
    CAST(COUNT(DISTINCT customer_id) AS FLOAT64) AS value
  FROM fm_in_window CROSS JOIN bounds
  WHERE conc IS NOT NULL AND ms_date BETWEEN scan_start AND scan_end
  GROUP BY 1, 2
),
churn_cohort AS (
  SELECT psp, bucket, customer_id,
    MAX(IF(r1.membership_id IS NOT NULL, 1, 0)) AS has_r1_net
  FROM (
    SELECT fm.psp, fm.customer_id, fm.membership_id, {_bucket('fm.ms_date')} AS bucket
    FROM fm_in_window fm CROSS JOIN bounds
    WHERE fm.conc IS NOT NULL AND fm.brand_type='Booking'
      AND fm.ms_status NOT IN ('abandonned','processing','paused')
      AND fm.ms_date BETWEEN scan_start AND scan_end
      AND (fm.ms_trial_end <= CURRENT_DATE() OR fm.cancelled_during_trial = TRUE)
  ) fmb
  LEFT JOIN (
    SELECT DISTINCT membership_id
    FROM `eu-andy-marketing-raw.dashboard.fact_transactions`
    WHERE invoice_r_index='1' AND transaction_status='succeeded'
      AND is_refunded=FALSE AND is_alerted=FALSE AND brand_type='Booking'
  ) r1 ON r1.membership_id = fmb.membership_id
  GROUP BY 1, 2, 3
),
churn_agg AS (
  SELECT psp, bucket, 'churn_cohort_size' AS metric, CAST(COUNT(*) AS FLOAT64) AS value
  FROM churn_cohort GROUP BY 1, 2
  UNION ALL
  SELECT psp, bucket, 'r1_net_count', CAST(SUM(has_r1_net) AS FLOAT64)
  FROM churn_cohort GROUP BY 1, 2
),
ft_in_window AS (
  SELECT ft.transaction_id, ft.transaction_amount, ft.transaction_status,
    {psp_ft} AS psp, {ft_conc} AS conc, {_bucket('ft.t_date')} AS bucket
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ft CROSS JOIN bounds
  WHERE ft.t_date BETWEEN scan_start AND scan_end
),
refund_in_window AS (
  SELECT ft.transaction_amount, {psp_ft} AS psp, {ft_conc} AS conc,
    {_bucket('ft.refunded_at_utc')} AS bucket
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ft CROSS JOIN bounds
  WHERE ft.is_refunded=TRUE AND ft.refunded_at_utc BETWEEN scan_start AND scan_end
),
ft_visa AS (
  SELECT ft.transaction_id, {psp_ft} AS psp, {ft_conc} AS conc, {_bucket('ft.t_date')} AS bucket
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ft CROSS JOIN bounds
  WHERE ft.t_date BETWEEN scan_start AND scan_end AND ft.transaction_status='succeeded'
    AND UPPER(ft.t_card_brand) IN ('VISA','DELTA')
),
tx_metrics AS (
  SELECT psp, bucket, 'brut' AS metric,
    SUM(CASE WHEN transaction_status='succeeded' THEN transaction_amount ELSE 0 END) AS value
  FROM ft_in_window WHERE conc IS NOT NULL AND bucket IS NOT NULL GROUP BY 1, 2
  UNION ALL
  SELECT psp, bucket, 'refund_rev', SUM(transaction_amount)
  FROM refund_in_window WHERE conc IS NOT NULL AND bucket IS NOT NULL GROUP BY 1, 2
  UNION ALL
  SELECT psp, bucket, 'tx_succ_visa', CAST(COUNT(DISTINCT transaction_id) AS FLOAT64)
  FROM ft_visa WHERE conc IS NOT NULL AND bucket IS NOT NULL GROUP BY 1, 2
),
alerts_join AS (
  SELECT fa.transaction_id, {t_conc} AS conc, {psp_t} AS psp,
    {_bucket('fa.alerted_at')} AS bucket
  FROM `eu-andy-marketing-raw.dashboard.fact_alert` fa
  JOIN `eu-andy-marketing-raw.dashboard.fact_transactions` t USING (transaction_id)
  CROSS JOIN bounds
  WHERE fa.alerted_at BETWEEN scan_start AND scan_end AND UPPER(fa.cardnetwork)='VISA'
),
alerts_metrics AS (
  SELECT psp, bucket, 'alerts_visa' AS metric, CAST(COUNT(DISTINCT transaction_id) AS FLOAT64) AS value
  FROM alerts_join WHERE conc IS NOT NULL AND bucket IS NOT NULL GROUP BY 1, 2
)
SELECT psp, bucket, metric, value FROM r0
UNION ALL SELECT psp, bucket, metric, value FROM churn_agg
UNION ALL SELECT psp, bucket, metric, value FROM tx_metrics
UNION ALL SELECT psp, bucket, metric, value FROM alerts_metrics
"""


_EXEC_QUARTER_PSP_ORDER = ["trustpayment", "pixxles", "labanquepostale", "NMI"]


def render_exec_quarter(df: pd.DataFrame, period_start: date, period_end: date) -> str:
    """Rendu de l'Exec agrégé par PSP, 3 mois (trimestre glissant). Colonnes =
    PSP × {m1, m2, m3}, juste la valeur (l'évolution se lit sur les 3 mois)."""
    if df.empty:
        return "<div style='color:#64748b;padding:12px;'>Aucune donnée</div>"
    data: dict = {}
    for _, r in df.iterrows():
        try:
            v = float(r["value"]) if pd.notna(r["value"]) else 0.0
        except (TypeError, ValueError):
            v = 0.0
        data.setdefault((r["psp"], r["bucket"]), {})[r["metric"]] = v

    def m(psp, b, metric):
        return data.get((psp, b), {}).get(metric, 0.0)

    m3s = period_start.replace(day=1)
    m2s = (m3s - timedelta(days=1)).replace(day=1)
    m1s = (m2s - timedelta(days=1)).replace(day=1)
    buckets = [("m1", _FR_MONTHS[m1s.month]), ("m2", _FR_MONTHS[m2s.month]),
               ("m3", _FR_MONTHS[m3s.month])]

    psps_present = {p for (p, _b) in data.keys()}

    def quarter_brut(psp):
        return sum(m(psp, b, "brut") for b, _l in buckets)
    shown = [p for p in psps_present if quarter_brut(p) >= 1000.0]
    _order = {p: i for i, p in enumerate(_EXEC_QUARTER_PSP_ORDER)}
    shown.sort(key=lambda p: (_order.get(p, 99), p))

    def psp_label(p):
        return _EXEC_PSP_LABELS.get(p, p)

    def fr_int(v):
        return f"{int(round(v)):,}".replace(",", " ")

    def fr_pct(v):
        return "—" if v is None else f"{v*100:.1f}%".replace(".", ",")

    def fr_eur(v):
        if abs(v) >= 1000:
            return f"{v/1000:,.1f} K€".replace(",", " ").replace(".", ",")
        return f"{v:,.0f} €".replace(",", " ")

    def k_r0(psp, b):
        return fr_int(m(psp, b, "r0"))

    def k_ca(psp, b):
        return fr_eur(m(psp, b, "brut") - m(psp, b, "refund_rev"))

    def k_churn(psp, b):
        coh, r1 = m(psp, b, "churn_cohort_size"), m(psp, b, "r1_net_count")
        return fr_pct(None if coh == 0 else 1 - r1 / coh)

    def k_refund(psp, b):
        brut, ref = m(psp, b, "brut"), m(psp, b, "refund_rev")
        return fr_pct(None if brut == 0 else ref / brut)

    def k_vamp(psp, b):
        d, n = m(psp, b, "tx_succ_visa"), m(psp, b, "alerts_visa")
        return fr_pct(None if d == 0 else n / d)

    def tsum(b, atom):
        return sum(m(p, b, atom) for p in shown)

    def t_r0(b):
        return fr_int(tsum(b, "r0"))

    def t_ca(b):
        return fr_eur(tsum(b, "brut") - tsum(b, "refund_rev"))

    def t_churn(b):
        coh, r1 = tsum(b, "churn_cohort_size"), tsum(b, "r1_net_count")
        return fr_pct(None if coh == 0 else 1 - r1 / coh)

    def t_refund(b):
        brut, ref = tsum(b, "brut"), tsum(b, "refund_rev")
        return fr_pct(None if brut == 0 else ref / brut)

    def t_vamp(b):
        d, n = tsum(b, "tx_succ_visa"), tsum(b, "alerts_visa")
        return fr_pct(None if d == 0 else n / d)

    kpis = [
        ("# R0 (customers)", k_r0, t_r0),
        ("€ CA Net", k_ca, t_ca),
        ("% Churn R0→R1 (Booking)", k_churn, t_churn),
        ("% Refund (CA)", k_refund, t_refund),
        ("% VAMP Ratio (Visa)", k_vamp, t_vamp),
    ]

    # Header : ligne 1 = PSP (colspan 3) ; ligne 2 = les 3 mois.
    h1 = "<th class='eq-kpi' rowspan='2'>KPI</th>"
    for p in shown:
        h1 += f"<th colspan='3' class='eq-grp'>{psp_label(p)}</th>"
    h1 += "<th colspan='3' class='eq-grp eq-total'>Total</th>"
    h2 = ""
    for _ in shown:
        for j, (_b, lbl) in enumerate(buckets):
            h2 += f"<th class='eq-mo{' eq-gsep' if j == 0 else ''}'>{lbl}</th>"
    for j, (_b, lbl) in enumerate(buckets):
        h2 += f"<th class='eq-mo eq-total{' eq-gsep' if j == 0 else ''}'>{lbl}</th>"

    body = ""
    for label, fn, tfn in kpis:
        cells = ""
        for i, p in enumerate(shown):
            band = " eq-band" if i % 2 else ""
            for j, (b, _l) in enumerate(buckets):
                gs = " eq-gsep" if j == 0 else ""
                cells += f"<td class='eq-val{gs}{band}'>{fn(p, b)}</td>"
        for j, (b, _l) in enumerate(buckets):
            gs = " eq-gsep" if j == 0 else ""
            cells += f"<td class='eq-val eq-total{gs}'>{tfn(b)}</td>"
        body += f"<tr><td class='eq-kpi'>{label}</td>{cells}</tr>"

    is_mtd = period_end < _last_day_of_month(period_start)
    sel = f"{_fr_month_year(m3s)}" + (" (MTD)" if is_mtd else "")
    sub = (f"Trimestre glissant : {_FR_MONTHS[m1s.month]} · {_FR_MONTHS[m2s.month]} · "
           f"{_FR_MONTHS[m3s.month]} {m3s.year}. Conciergeries agrégées par PSP "
           f"(NMI regroupé). Mois sélectionné : {sel}.")

    css = """
    <style>
      .eq-wrap { font-size: 13px; overflow-x: auto; }
      .eq-note { background:#fef3c7; border-left:4px solid #f59e0b; padding:8px 12px;
                 border-radius:4px; margin-bottom:12px; font-size:13px; }
      table.eq { border-collapse: separate; border-spacing: 0; background:white;
                 border:1px solid #cbd5e1; border-radius:8px; overflow:hidden; }
      table.eq th, table.eq td { padding:8px 12px; text-align:center; white-space:nowrap;
                                 border-right:1px solid #eef1f5; }
      table.eq thead th { background:#0f172a; color:white; font-weight:600;
                          position:sticky; top:0; }
      /* Séparation ENTRE PSP : gros trait vertical + fond de bloc alterné. */
      th.eq-grp { border-left:4px solid #0f172a; font-size:13px; }
      th.eq-gsep, td.eq-gsep { border-left:4px solid #0f172a !important; }
      td.eq-band { background:#e8edf3 !important; }
      th.eq-mo { background:#1e293b; color:#cbd5e1; font-weight:500; font-size:12px; }
      th.eq-kpi, td.eq-kpi { text-align:left; position:sticky; left:0; z-index:1;
                             background:#1e293b; color:white; font-weight:600; min-width:200px; }
      td.eq-kpi { background:#f8fafc; color:#0f172a; border-right:3px solid #0f172a; }
      td.eq-val { color:#0f172a; min-width:78px; }
      .eq-total { background:#f1f5f9; font-weight:700; }
      th.eq-total { background:#1e293b; }
      tbody tr:nth-child(even) td.eq-val { background:#fbfcfe; }
    </style>
    """
    table = (f"<table class='eq'><thead><tr>{h1}</tr><tr>{h2}</tr></thead>"
             f"<tbody>{body}</tbody></table>")
    return f"{css}<div class='eq-wrap'><div class='eq-note'>{sub}</div>{table}</div>"


def exec_billing_sql(s1_start: date, s1_end: date,
                     s2_start: date, s2_end: date) -> str:
    """SQL Executive Summary Billing — atomes par (conciergerie, psp, bucket).

    Buckets : s1 (semaine sélectionnée) et s2 (semaine précédente).

    Atomes retournés (en long format) :
      Processing :
        - ca_brut        : SUM(amount succeeded) by t_date
        - ca_refund      : SUM(amount refunded)  by refunded_at_utc
      Success Rate R1 :
        - r1_fa_tx, r1_fa_succ_tx        : 1ère tentative R1 (au niveau tx)
        - r1_att_users, r1_succ_users    : users avec ≥1 tx R1 attempt / success
      Success Rate Total (toutes R indexes) :
        - all_fa_tx, all_fa_succ_tx      : 1ère tentative toutes R (au niveau tx)
        - all_att_users, all_succ_users  : users avec ≥1 tx attempt / success
      VAMP :
        - volume_succ_total              : # tx succeeded toutes cartes (R0 inclus)
        - tx_succ_visa                   : # tx succeeded Visa+Delta
        - tx_succ_mc                     : # tx succeeded MasterCard
        - alerts_visa, alerts_mc         : # alertes par cardnetwork
    """
    def _conc(alias: str, col: str) -> str:
        return (
            f"CASE LOWER(SPLIT(COALESCE({alias}.{col}, ''), ' - ')[OFFSET(0)]) "
            "  WHEN 'reserv-go'    THEN 'Reserv-Go' "
            "  WHEN 'book-ici'     THEN 'Book-Ici' "
            "  WHEN 'resadexa'     THEN 'Resadexa' "
            "  WHEN 'rezaflash'    THEN 'Rezaflash' "
            "  WHEN 'jumpaide.com' THEN 'Jumpaide' "
            "  WHEN 'concimax'     THEN 'Concimax' "
            "  WHEN 'rapidoxy'     THEN 'Rapidoxy' "
            "  ELSE NULL END"
        )

    ft_conc = _conc("ft", "t_brand")
    t_conc  = _conc("t",  "t_brand")
    psp_ft = _exec_psp_reel("ft.ms_default_psp", "rm")
    psp_t  = _exec_psp_reel("t.ms_default_psp",  "rm")

    s1s, s1e, s2s, s2e = (d.isoformat() for d in (s1_start, s1_end, s2_start, s2_end))

    return f"""
WITH weeks_def AS (
  SELECT
    DATE '{s1s}' AS s1_start,
    DATE '{s1e}' AS s1_end,
    DATE '{s2s}' AS s2_start,
    DATE '{s2e}' AS s2_end
),
r0_mid_map AS (
  SELECT f.membership_id, ANY_VALUE(s.MidId) AS MidId
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` f
  JOIN `eu-andy-marketing-raw.silver_sgw.stg_transactions` s ON f.transaction_id = s.Id
  WHERE f.t_psp_name = 'nmi' AND f.invoice_r_index = '0'
    AND f.t_date BETWEEN DATE_SUB((SELECT s2_start FROM weeks_def), INTERVAL 15 DAY)
                     AND DATE_ADD((SELECT s1_end FROM weeks_def), INTERVAL 15 DAY)
  GROUP BY f.membership_id
),
ft_window AS (
  SELECT
    ft.transaction_id,
    ft.t_date,
    ft.transaction_amount,
    ft.transaction_status,
    ft.invoice_r_index,
    ft.t_attempt_index,
    ft.t_card_brand,
    ft.customer_email,
    {psp_ft} AS psp,
    {ft_conc} AS conciergerie,
    CASE
      WHEN ft.t_date BETWEEN (SELECT s1_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def) THEN 's1'
      WHEN ft.t_date BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s2_end FROM weeks_def) THEN 's2'
    END AS bucket
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ft
  LEFT JOIN r0_mid_map rm ON rm.membership_id = ft.membership_id
  WHERE ft.t_date BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def)
),
refund_window AS (
  SELECT
    ft.transaction_amount,
    {psp_ft} AS psp,
    {ft_conc} AS conciergerie,
    CASE
      WHEN ft.refunded_at_utc BETWEEN (SELECT s1_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def) THEN 's1'
      WHEN ft.refunded_at_utc BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s2_end FROM weeks_def) THEN 's2'
    END AS bucket
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ft
  LEFT JOIN r0_mid_map rm ON rm.membership_id = ft.membership_id
  WHERE ft.is_refunded = TRUE
    AND ft.refunded_at_utc BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def)
),
alerts_window AS (
  SELECT
    fa.transaction_id,
    fa.cardnetwork,
    {psp_t} AS psp,
    {t_conc} AS conciergerie,
    CASE
      WHEN fa.alerted_at BETWEEN (SELECT s1_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def) THEN 's1'
      WHEN fa.alerted_at BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s2_end FROM weeks_def) THEN 's2'
    END AS bucket
  FROM `eu-andy-marketing-raw.dashboard.fact_alert` fa
  JOIN `eu-andy-marketing-raw.dashboard.fact_transactions` t USING (transaction_id)
  LEFT JOIN r0_mid_map rm ON rm.membership_id = t.membership_id
  WHERE fa.alerted_at BETWEEN (SELECT s2_start FROM weeks_def) AND (SELECT s1_end FROM weeks_def)
),
-- Processing
proc_metrics AS (
  SELECT conciergerie, psp, bucket, 'ca_brut' AS metric,
    SUM(CASE WHEN transaction_status='succeeded' THEN transaction_amount ELSE 0 END) AS value
  FROM ft_window WHERE conciergerie IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2,3
  UNION ALL
  SELECT conciergerie, psp, bucket, 'ca_refund' AS metric, SUM(transaction_amount) AS value
  FROM refund_window WHERE conciergerie IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2,3
),
-- Success Rate R1 (au niveau R1 spécifiquement)
sr_r1_metrics AS (
  SELECT conciergerie, psp, bucket, 'r1_fa_tx' AS metric,
    CAST(COUNT(DISTINCT CASE WHEN invoice_r_index='1' AND t_attempt_index=1 THEN transaction_id END) AS FLOAT64) AS value
  FROM ft_window WHERE conciergerie IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2,3
  UNION ALL
  SELECT conciergerie, psp, bucket, 'r1_fa_succ_tx',
    CAST(COUNT(DISTINCT CASE WHEN invoice_r_index='1' AND t_attempt_index=1 AND transaction_status='succeeded' THEN transaction_id END) AS FLOAT64)
  FROM ft_window WHERE conciergerie IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2,3
  UNION ALL
  SELECT conciergerie, psp, bucket, 'r1_att_users',
    CAST(COUNT(DISTINCT CASE WHEN invoice_r_index='1' THEN customer_email END) AS FLOAT64)
  FROM ft_window WHERE conciergerie IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2,3
  UNION ALL
  SELECT conciergerie, psp, bucket, 'r1_succ_users',
    CAST(COUNT(DISTINCT CASE WHEN invoice_r_index='1' AND transaction_status='succeeded' THEN customer_email END) AS FLOAT64)
  FROM ft_window WHERE conciergerie IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2,3
  UNION ALL
  SELECT conciergerie, psp, bucket, 'r0_att_users',
    CAST(COUNT(DISTINCT CASE WHEN invoice_r_index='0' THEN customer_email END) AS FLOAT64)
  FROM ft_window WHERE conciergerie IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2,3
  UNION ALL
  SELECT conciergerie, psp, bucket, 'r0_succ_users',
    CAST(COUNT(DISTINCT CASE WHEN invoice_r_index='0' AND transaction_status='succeeded' THEN customer_email END) AS FLOAT64)
  FROM ft_window WHERE conciergerie IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2,3
),
-- Success Rate Total (toutes R indexes confondues)
sr_all_metrics AS (
  SELECT conciergerie, psp, bucket, 'all_fa_tx' AS metric,
    CAST(COUNT(DISTINCT CASE WHEN t_attempt_index=1 THEN transaction_id END) AS FLOAT64) AS value
  FROM ft_window WHERE conciergerie IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2,3
  UNION ALL
  SELECT conciergerie, psp, bucket, 'all_fa_succ_tx',
    CAST(COUNT(DISTINCT CASE WHEN t_attempt_index=1 AND transaction_status='succeeded' THEN transaction_id END) AS FLOAT64)
  FROM ft_window WHERE conciergerie IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2,3
  UNION ALL
  SELECT conciergerie, psp, bucket, 'all_att_users',
    CAST(COUNT(DISTINCT customer_email) AS FLOAT64)
  FROM ft_window WHERE conciergerie IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2,3
  UNION ALL
  SELECT conciergerie, psp, bucket, 'all_succ_users',
    CAST(COUNT(DISTINCT CASE WHEN transaction_status='succeeded' THEN customer_email END) AS FLOAT64)
  FROM ft_window WHERE conciergerie IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2,3
),
-- VAMP : volume (toutes cartes) + volumes Visa & MC + alertes Visa & MC
vamp_metrics AS (
  SELECT conciergerie, psp, bucket, 'volume_succ_total' AS metric,
    CAST(COUNT(DISTINCT CASE WHEN transaction_status='succeeded' THEN transaction_id END) AS FLOAT64) AS value
  FROM ft_window WHERE conciergerie IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2,3
  UNION ALL
  SELECT conciergerie, psp, bucket, 'tx_succ_visa',
    CAST(COUNT(DISTINCT CASE WHEN transaction_status='succeeded' AND UPPER(t_card_brand) IN ('VISA','DELTA') THEN transaction_id END) AS FLOAT64)
  FROM ft_window WHERE conciergerie IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2,3
  UNION ALL
  SELECT conciergerie, psp, bucket, 'tx_succ_mc',
    CAST(COUNT(DISTINCT CASE WHEN transaction_status='succeeded' AND UPPER(t_card_brand) IN ('MASTERCARD','MC') THEN transaction_id END) AS FLOAT64)
  FROM ft_window WHERE conciergerie IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2,3
  UNION ALL
  SELECT conciergerie, psp, bucket, 'alerts_visa',
    CAST(COUNT(DISTINCT transaction_id) AS FLOAT64)
  FROM alerts_window WHERE conciergerie IS NOT NULL AND bucket IS NOT NULL
    AND UPPER(cardnetwork) = 'VISA'
  GROUP BY 1,2,3
  UNION ALL
  SELECT conciergerie, psp, bucket, 'alerts_mc',
    CAST(COUNT(DISTINCT transaction_id) AS FLOAT64)
  FROM alerts_window WHERE conciergerie IS NOT NULL AND bucket IS NOT NULL
    AND UPPER(cardnetwork) = 'MASTERCARD'
  GROUP BY 1,2,3
),
col_ref AS (
  -- Référence pour la SÉLECTION des colonnes (pas affichée) : CA encaissé sur
  -- 30 jours glissants par Conciergerie × PSP réel, même échelle que l'Exec
  -- Summary mensuel -> Billing affiche les MÊMES colonnes (seuil 10 k€).
  SELECT {ft_conc} AS conciergerie, {psp_ft} AS psp, 's1' AS bucket,
    'col_ca_ref' AS metric,
    SUM(CASE WHEN ft.transaction_status='succeeded' THEN ft.transaction_amount ELSE 0 END) AS value
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ft
  LEFT JOIN r0_mid_map rm ON rm.membership_id = ft.membership_id
  WHERE ft.t_date BETWEEN DATE_SUB((SELECT s1_end FROM weeks_def), INTERVAL 29 DAY)
                     AND (SELECT s1_end FROM weeks_def)
  GROUP BY 1, 2
  HAVING conciergerie IS NOT NULL
)
SELECT * FROM proc_metrics
UNION ALL SELECT * FROM sr_r1_metrics
UNION ALL SELECT * FROM sr_all_metrics
UNION ALL SELECT * FROM vamp_metrics
UNION ALL SELECT * FROM col_ref
"""


def exec_billing_quarter_sql(period_start: date, period_end: date) -> str:
    """Exec Billing agrégé par PSP (conciergeries regroupées), 3 mois glissants
    finissant au mois sélectionné. NMI regroupé en un seul 'NMI'. Mêmes atomes
    que exec_billing_sql (processing / success rate / VAMP Visa+MC), bucketés par
    mois (m1/m2/m3) ; ratios recomposés au rendu."""
    m3s = period_start.replace(day=1)
    m2s = (m3s - timedelta(days=1)).replace(day=1)
    m1s = (m2s - timedelta(days=1)).replace(day=1)
    m1, m2, m3, p_end = (d.isoformat() for d in (m1s, m2s, m3s, period_end))

    def _psp(col):
        return f"CASE WHEN {col} = 'nmi' THEN 'NMI' ELSE {col} END"
    psp_ft, psp_t = _psp("ft.ms_default_psp"), _psp("t.ms_default_psp")

    def _conc(alias, col):
        return (f"CASE LOWER(SPLIT(COALESCE({alias}.{col}, ''), ' - ')[OFFSET(0)]) "
                "WHEN 'reserv-go' THEN 'x' WHEN 'book-ici' THEN 'x' "
                "WHEN 'resadexa' THEN 'x' WHEN 'rezaflash' THEN 'x' "
                "WHEN 'jumpaide.com' THEN 'x' WHEN 'concimax' THEN 'x' "
                "WHEN 'rapidoxy' THEN 'x' ELSE NULL END")
    ft_conc, t_conc = _conc("ft", "t_brand"), _conc("t", "t_brand")

    def _bucket(datecol):
        return (f"CASE DATE_TRUNC({datecol}, MONTH) "
                f"WHEN DATE '{m1}' THEN 'm1' WHEN DATE '{m2}' THEN 'm2' "
                f"WHEN DATE '{m3}' THEN 'm3' END")

    return f"""
WITH bounds AS (SELECT DATE '{m1}' AS scan_start, DATE '{p_end}' AS scan_end),
ft_window AS (
  SELECT ft.transaction_id, ft.transaction_amount, ft.transaction_status,
    ft.invoice_r_index, ft.t_attempt_index, ft.t_card_brand, ft.customer_email,
    {psp_ft} AS psp, {ft_conc} AS conc, {_bucket('ft.t_date')} AS bucket
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ft CROSS JOIN bounds
  WHERE ft.t_date BETWEEN scan_start AND scan_end
),
refund_window AS (
  SELECT ft.transaction_amount, {psp_ft} AS psp, {ft_conc} AS conc,
    {_bucket('ft.refunded_at_utc')} AS bucket
  FROM `eu-andy-marketing-raw.dashboard.fact_transactions` ft CROSS JOIN bounds
  WHERE ft.is_refunded = TRUE AND ft.refunded_at_utc BETWEEN scan_start AND scan_end
),
alerts_window AS (
  SELECT fa.transaction_id, fa.cardnetwork, {psp_t} AS psp, {t_conc} AS conc,
    {_bucket('fa.alerted_at')} AS bucket
  FROM `eu-andy-marketing-raw.dashboard.fact_alert` fa
  JOIN `eu-andy-marketing-raw.dashboard.fact_transactions` t USING (transaction_id)
  CROSS JOIN bounds
  WHERE fa.alerted_at BETWEEN scan_start AND scan_end
),
proc_metrics AS (
  SELECT psp, bucket, 'ca_brut' AS metric,
    SUM(CASE WHEN transaction_status='succeeded' THEN transaction_amount ELSE 0 END) AS value
  FROM ft_window WHERE conc IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2
  UNION ALL
  SELECT psp, bucket, 'ca_refund', SUM(transaction_amount)
  FROM refund_window WHERE conc IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2
),
sr_metrics AS (
  SELECT psp, bucket, 'r1_fa_tx' AS metric, CAST(COUNT(DISTINCT CASE WHEN invoice_r_index='1' AND t_attempt_index=1 THEN transaction_id END) AS FLOAT64) AS value FROM ft_window WHERE conc IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2
  UNION ALL SELECT psp, bucket, 'r1_fa_succ_tx', CAST(COUNT(DISTINCT CASE WHEN invoice_r_index='1' AND t_attempt_index=1 AND transaction_status='succeeded' THEN transaction_id END) AS FLOAT64) FROM ft_window WHERE conc IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2
  UNION ALL SELECT psp, bucket, 'r1_att_users', CAST(COUNT(DISTINCT CASE WHEN invoice_r_index='1' THEN customer_email END) AS FLOAT64) FROM ft_window WHERE conc IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2
  UNION ALL SELECT psp, bucket, 'r1_succ_users', CAST(COUNT(DISTINCT CASE WHEN invoice_r_index='1' AND transaction_status='succeeded' THEN customer_email END) AS FLOAT64) FROM ft_window WHERE conc IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2
  UNION ALL SELECT psp, bucket, 'all_fa_tx', CAST(COUNT(DISTINCT CASE WHEN t_attempt_index=1 THEN transaction_id END) AS FLOAT64) FROM ft_window WHERE conc IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2
  UNION ALL SELECT psp, bucket, 'all_fa_succ_tx', CAST(COUNT(DISTINCT CASE WHEN t_attempt_index=1 AND transaction_status='succeeded' THEN transaction_id END) AS FLOAT64) FROM ft_window WHERE conc IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2
  UNION ALL SELECT psp, bucket, 'all_att_users', CAST(COUNT(DISTINCT customer_email) AS FLOAT64) FROM ft_window WHERE conc IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2
  UNION ALL SELECT psp, bucket, 'all_succ_users', CAST(COUNT(DISTINCT CASE WHEN transaction_status='succeeded' THEN customer_email END) AS FLOAT64) FROM ft_window WHERE conc IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2
  UNION ALL SELECT psp, bucket, 'r0_att_users', CAST(COUNT(DISTINCT CASE WHEN invoice_r_index='0' THEN customer_email END) AS FLOAT64) FROM ft_window WHERE conc IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2
  UNION ALL SELECT psp, bucket, 'r0_succ_users', CAST(COUNT(DISTINCT CASE WHEN invoice_r_index='0' AND transaction_status='succeeded' THEN customer_email END) AS FLOAT64) FROM ft_window WHERE conc IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2
),
vamp_metrics AS (
  SELECT psp, bucket, 'volume_succ_total' AS metric, CAST(COUNT(DISTINCT CASE WHEN transaction_status='succeeded' THEN transaction_id END) AS FLOAT64) AS value FROM ft_window WHERE conc IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2
  UNION ALL SELECT psp, bucket, 'tx_succ_visa', CAST(COUNT(DISTINCT CASE WHEN transaction_status='succeeded' AND UPPER(t_card_brand) IN ('VISA','DELTA') THEN transaction_id END) AS FLOAT64) FROM ft_window WHERE conc IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2
  UNION ALL SELECT psp, bucket, 'tx_succ_mc', CAST(COUNT(DISTINCT CASE WHEN transaction_status='succeeded' AND UPPER(t_card_brand) IN ('MASTERCARD','MC') THEN transaction_id END) AS FLOAT64) FROM ft_window WHERE conc IS NOT NULL AND bucket IS NOT NULL GROUP BY 1,2
  UNION ALL SELECT psp, bucket, 'alerts_visa', CAST(COUNT(DISTINCT transaction_id) AS FLOAT64) FROM alerts_window WHERE conc IS NOT NULL AND bucket IS NOT NULL AND UPPER(cardnetwork)='VISA' GROUP BY 1,2
  UNION ALL SELECT psp, bucket, 'alerts_mc', CAST(COUNT(DISTINCT transaction_id) AS FLOAT64) FROM alerts_window WHERE conc IS NOT NULL AND bucket IS NOT NULL AND UPPER(cardnetwork)='MASTERCARD' GROUP BY 1,2
)
SELECT psp, bucket, metric, value FROM proc_metrics
UNION ALL SELECT psp, bucket, metric, value FROM sr_metrics
UNION ALL SELECT psp, bucket, metric, value FROM vamp_metrics
"""


def render_exec_billing_quarter(df: pd.DataFrame, period_start: date,
                                period_end: date) -> str:
    """Rendu Exec Billing agrégé par PSP, 3 mois (trimestre glissant). Colonnes =
    PSP × {m1, m2, m3}, chaque mois : valeur + % vs mois précédent."""
    if df.empty:
        return "<div style='color:#64748b;padding:12px;'>Aucune donnée</div>"
    data: dict = {}
    for _, r in df.iterrows():
        try:
            v = float(r["value"]) if pd.notna(r["value"]) else 0.0
        except (TypeError, ValueError):
            v = 0.0
        data.setdefault((r["psp"], r["bucket"]), {})[r["metric"]] = v

    def m(psp, b, atom):
        return data.get((psp, b), {}).get(atom, 0.0)

    def asum(psps, b, atom):
        return sum(m(p, b, atom) for p in psps)

    m3s = period_start.replace(day=1)
    m2s = (m3s - timedelta(days=1)).replace(day=1)
    m1s = (m2s - timedelta(days=1)).replace(day=1)
    buckets = [("m1", _FR_MONTHS[m1s.month]), ("m2", _FR_MONTHS[m2s.month]),
               ("m3", _FR_MONTHS[m3s.month])]
    prev_of = {"m1": None, "m2": "m1", "m3": "m2"}

    psps_present = {p for (p, _b) in data.keys()}
    shown = [p for p in psps_present
             if sum(m(p, b, "ca_brut") for b, _l in buckets) >= 1000.0]
    _order = {p: i for i, p in enumerate(_EXEC_QUARTER_PSP_ORDER)}
    shown.sort(key=lambda p: (_order.get(p, 99), p))

    def fr_int(v):
        return f"{int(round(v)):,}".replace(",", " ")

    def fr_pct(v):
        return "—" if v is None else f"{v*100:.1f}%".replace(".", ",")

    def fr_eur(v):
        if abs(v) >= 1000:
            return f"{v/1000:,.1f} K€".replace(",", " ").replace(".", ",")
        return f"{v:,.0f} €".replace(",", " ")


    # KPI value functions : f(list_of_psps, bucket) -> numeric | None
    def v_brut(ps, b): return asum(ps, b, "ca_brut")
    def v_ref(ps, b): return asum(ps, b, "ca_refund")
    def v_net(ps, b): return asum(ps, b, "ca_brut") - asum(ps, b, "ca_refund")
    def _ratio(ps, b, num, den):
        d = asum(ps, b, den)
        return None if d == 0 else asum(ps, b, num) / d
    def v_r0pu(ps, b): return _ratio(ps, b, "r0_succ_users", "r0_att_users")
    def v_r1fa(ps, b): return _ratio(ps, b, "r1_fa_succ_tx", "r1_fa_tx")
    def v_r1pu(ps, b): return _ratio(ps, b, "r1_succ_users", "r1_att_users")
    def v_allfa(ps, b): return _ratio(ps, b, "all_fa_succ_tx", "all_fa_tx")
    def v_allpu(ps, b): return _ratio(ps, b, "all_succ_users", "all_att_users")
    def v_vol(ps, b): return asum(ps, b, "volume_succ_total")
    def v_alv(ps, b): return asum(ps, b, "alerts_visa")
    def v_alm(ps, b): return asum(ps, b, "alerts_mc")
    def v_vv(ps, b): return _ratio(ps, b, "alerts_visa", "tx_succ_visa")
    def v_vm(ps, b): return _ratio(ps, b, "alerts_mc", "tx_succ_mc")

    # (kind, label, fn, fmt, lower_is_better)  | kind: 'sec' / 'sub' / 'kpi'
    rows = [
        ("sec", "💰 PROCESSING", None, None, False),
        ("kpi", "CA Brut", v_brut, fr_eur, False),
        ("kpi", "Refund (€)", v_ref, fr_eur, True),
        ("kpi", "CA Net", v_net, fr_eur, False),
        ("sec", "✅ SUCCESS RATE", None, None, False),
        ("sub", "R0", None, None, False),
        ("kpi", "Success Rate R0 — Per User", v_r0pu, fr_pct, False),
        ("sub", "R1", None, None, False),
        ("kpi", "Success Rate R1 — First Attempt", v_r1fa, fr_pct, False),
        ("kpi", "Success Rate R1 — Per User", v_r1pu, fr_pct, False),
        ("sub", "Total (toutes R indexes)", None, None, False),
        ("kpi", "Success Rate — First Attempt", v_allfa, fr_pct, False),
        ("kpi", "Success Rate — Per User", v_allpu, fr_pct, False),
        ("sec", "⚠️ VAMP", None, None, False),
        ("sub", "Volumes", None, None, False),
        ("kpi", "Volume tx succeeded (total)", v_vol, fr_int, False),
        ("kpi", "Nb d'alertes VISA", v_alv, fr_int, True),
        ("kpi", "Nb d'alertes MC", v_alm, fr_int, True),
        ("sub", "Taux", None, None, False),
        ("kpi", "VAMP Ratio VISA", v_vv, fr_pct, True),
        ("kpi", "VAMP Ratio MC", v_vm, fr_pct, True),
    ]

    ncols = 1 + 3 * len(shown) + 3

    def cell(fn, fmt, lower, psps, b, extra=""):
        val = fn(psps, b)
        vs = fmt(val) if val is not None else "—"
        return f"<td class='eqb-val{extra}'><div class='eqb-v'>{vs}</div></td>"

    h1 = "<th class='eqb-kpi' rowspan='2'>KPI</th>"
    for p in shown:
        h1 += f"<th colspan='3' class='eqb-grp'>{_EXEC_PSP_LABELS.get(p, p)}</th>"
    h1 += "<th colspan='3' class='eqb-grp eqb-total'>Total</th>"
    h2 = ""
    for _ in shown:
        for j, (_b, lbl) in enumerate(buckets):
            h2 += f"<th class='eqb-mo{' eqb-gsep' if j == 0 else ''}'>{lbl}</th>"
    for j, (_b, lbl) in enumerate(buckets):
        h2 += f"<th class='eqb-mo eqb-total{' eqb-gsep' if j == 0 else ''}'>{lbl}</th>"

    body = ""
    for kind, label, fn, fmt, lower in rows:
        if kind == "sec":
            body += (f"<tr class='eqb-sec'><td colspan='{ncols}'>{label}</td></tr>")
            continue
        if kind == "sub":
            body += (f"<tr class='eqb-sub'><td colspan='{ncols}'>{label}</td></tr>")
            continue
        cells = ""
        for ki, p in enumerate(shown):
            band = " eqb-band" if ki % 2 else ""
            for j, (b, _l) in enumerate(buckets):
                cells += cell(fn, fmt, lower, [p], b,
                              (" eqb-gsep" if j == 0 else "") + band)
        for j, (b, _l) in enumerate(buckets):
            cells += cell(fn, fmt, lower, shown, b,
                          " eqb-total" + (" eqb-gsep" if j == 0 else ""))
        body += f"<tr><td class='eqb-kpi'>{label}</td>{cells}</tr>"

    is_mtd = period_end < _last_day_of_month(period_start)
    sub = (f"Billing agrégé par PSP — trimestre glissant : {_FR_MONTHS[m1s.month]} · "
           f"{_FR_MONTHS[m2s.month]} · {_FR_MONTHS[m3s.month]} {m3s.year}. "
           f"Conciergeries agrégées, NMI regroupé."
           + (" Dernier mois en MTD." if is_mtd else ""))

    css = """
    <style>
      .eqb-wrap { font-size: 13px; overflow-x: auto; }
      .eqb-note { background:#fef3c7; border-left:4px solid #f59e0b; padding:8px 12px;
                  border-radius:4px; margin-bottom:12px; font-size:13px; }
      table.eqb { border-collapse: separate; border-spacing: 0; background:white;
                  border:1px solid #cbd5e1; border-radius:8px; overflow:hidden; }
      table.eqb th, table.eqb td { padding:8px 12px; text-align:center; white-space:nowrap;
                                   border-right:1px solid #eef1f5; }
      table.eqb thead th { background:#0f172a; color:white; font-weight:600; }
      /* Séparation ENTRE PSP : gros trait vertical + fond de bloc alterné
         (les bandeaux de section coupent le trait, mais le fond garde les
         blocs PSP lisibles de haut en bas). */
      th.eqb-grp { border-left:4px solid #0f172a; font-size:13px; }
      th.eqb-gsep, td.eqb-gsep { border-left:4px solid #0f172a !important; }
      td.eqb-band { background:#e8edf3 !important; }
      th.eqb-mo { background:#1e293b; color:#cbd5e1; font-weight:500; font-size:12px; }
      th.eqb-kpi, td.eqb-kpi { text-align:left; position:sticky; left:0; z-index:1;
                               min-width:210px; }
      th.eqb-kpi { background:#1e293b; color:white; }
      td.eqb-kpi { background:#f8fafc; color:#0f172a; font-weight:600;
                   border-right:3px solid #0f172a; }
      td.eqb-val { color:#0f172a; min-width:74px; }
      .eqb-v { font-weight:600; }
      td.eqb-total, th.eqb-total { background:#f1f5f9; }
      th.eqb-total { background:#0b1220; }
      tr.eqb-sec td { background:#0f172a; color:white; text-transform:uppercase;
                      font-weight:700; text-align:left; letter-spacing:.04em; }
      tr.eqb-sub td { background:#e2e8f0; color:#0f172a; text-align:left;
                      font-weight:600; font-size:12px; }
    </style>
    """
    return (
        css
        + "<div class='eqb-wrap'>"
        + f"<div class='eqb-note'>{sub}</div>"
        + "<table class='eqb'><thead>"
        + f"<tr>{h1}</tr><tr>{h2}</tr>"
        + "</thead><tbody>"
        + body
        + "</tbody></table></div>"
    )


def render_exec_billing(df: pd.DataFrame,
                         s1_start: date, s1_end: date,
                         s2_start: date, s2_end: date) -> str:
    """Render le tab Executive Summary Billing — 3 sections colorées."""
    if df.empty:
        return "<div style='color:#64748b;padding:12px;'>Aucune donnée</div>"

    # Pivot {(conc, psp, bucket): {metric: value}}
    data: dict = {}
    for _, r in df.iterrows():
        key = (r["conciergerie"], r["psp"], r["bucket"])
        try:
            v = float(r["value"]) if pd.notna(r["value"]) else 0.0
        except (TypeError, ValueError):
            v = 0.0
        data.setdefault(key, {})[r["metric"]] = v

    def m(c, p, b, metric):
        return data.get((c, p, b), {}).get(metric, 0.0)

    # Colonnes dynamiques (conciergerie × psp réel) — MÊMES colonnes que l'Exec
    # Summary : sélection sur le CA encaissé 30 j glissants (col_ca_ref) >= seuil,
    # pour ne pas masquer des colonnes à cause de la fenêtre hebdo plus courte.
    pairs = [(c, p, _exec_psp_label(p)) for (c, p) in _exec_visible_pairs(data, "col_ca_ref")]
    # Regroupe les colonnes PAR PSP (puis conciergerie) pour qu'on distingue
    # clairement les blocs PSP ; un trait épais sépare chaque groupe de PSP.
    _psp_ord = {p: i for i, p in enumerate(
        ["trustpayment", "pixxles", "labanquepostale", "EMS", "Kadima", "Cliq", "CASH"])}
    _conc_ord = {c: i for i, c in enumerate(_EXEC_CONC_ORDER)}
    pairs.sort(key=lambda t: (_psp_ord.get(t[1], 99), _conc_ord.get(t[0], 99), t[0]))
    # col_cls[i] = 'exec-grp-start' si la colonne démarre un nouveau bloc PSP.
    col_cls = [
        "exec-grp-start" if (i > 0 and p != pairs[i - 1][1]) else ""
        for i, (c, p, _l) in enumerate(pairs)
    ]

    # --- Formatters FR ---
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

    # --- Header (colonnes dynamiques + Total) ---
    header_cells = "".join(
        f"<th class='{col_cls[i]}'>"
        f"<div class='exec-th-conc'>{c}</div>"
        f"<div class='exec-th-psp'>({lbl})</div>"
        "</th>"
        for i, (c, p, lbl) in enumerate(pairs)
    )
    header_cells += (
        "<th class='exec-th-total exec-grp-start'>"
        "<div class='exec-th-conc'>Total</div>"
        f"<div class='exec-th-psp'>({len(pairs)} colonnes)</div>"
        "</th>"
    )

    def _sum_atom(b, atom):
        return sum(m(c, p, b, atom) for (c, p, _l) in pairs)

    # --- KPI row builder ---
    def kpi_row(label, fmt, compute_fn, total_fn=None, lower_is_better=False,
                section_class=""):
        cells = []
        for i, (c, p, _lbl) in enumerate(pairs):
            v1 = compute_fn(c, p, "s1")
            v2 = compute_fn(c, p, "s2")
            value_str = fmt(v1) if v1 is not None else "—"
            delta_str = wow_html(
                wow_pct(v1, v2) if (v1 is not None and v2 is not None) else None,
                lower_is_better=lower_is_better
            )
            cells.append(
                f"<td class='{col_cls[i]}'><div class='exec-cell-val'>{value_str}</div>"
                f"<div class='exec-cell-wow'>{delta_str}</div></td>"
            )
        if total_fn is not None:
            t1 = total_fn("s1")
            t2 = total_fn("s2")
            t_value_str = fmt(t1) if t1 is not None else "—"
            t_delta_str = wow_html(
                wow_pct(t1, t2) if (t1 is not None and t2 is not None) else None,
                lower_is_better=lower_is_better
            )
            cells.append(
                f"<td class='exec-total-cell exec-grp-start'>"
                f"<div class='exec-cell-val'>{t_value_str}</div>"
                f"<div class='exec-cell-wow'>{t_delta_str}</div></td>"
            )
        return f"<tr class='{section_class}'><td class='exec-kpi-label'>{label}</td>{''.join(cells)}</tr>"

    def section_header(label, color_class):
        n_cols = len(pairs) + 2  # KPI col + colonnes + Total
        return (
            f"<tr class='billing-section-header {color_class}'>"
            f"<td colspan='{n_cols}'>{label}</td></tr>"
        )

    def subsection_header(label, color_class):
        n_cols = len(pairs) + 2
        return (
            f"<tr class='billing-subsection-header {color_class}'>"
            f"<td colspan='{n_cols}'>{label}</td></tr>"
        )

    # ========================================================================
    # Section 1 — PROCESSING (CA Brut, Refund, CA Net)
    # ========================================================================
    proc_rows = []
    proc_rows.append(section_header("💰 PROCESSING", "billing-proc"))
    proc_rows.append(kpi_row(
        "CA Brut", fr_eur,
        lambda c, p, b: m(c, p, b, "ca_brut"),
        total_fn=lambda b: _sum_atom(b, "ca_brut"),
        lower_is_better=False, section_class="billing-proc-row"
    ))
    proc_rows.append(kpi_row(
        "Refund (€)", fr_eur,
        lambda c, p, b: m(c, p, b, "ca_refund"),
        total_fn=lambda b: _sum_atom(b, "ca_refund"),
        lower_is_better=True, section_class="billing-proc-row"
    ))
    proc_rows.append(kpi_row(
        "CA Net", fr_eur,
        lambda c, p, b: m(c, p, b, "ca_brut") - m(c, p, b, "ca_refund"),
        total_fn=lambda b: _sum_atom(b, "ca_brut") - _sum_atom(b, "ca_refund"),
        lower_is_better=False, section_class="billing-proc-row billing-proc-row-emph"
    ))

    # ========================================================================
    # Section 2 — SUCCESS RATE
    # ========================================================================
    sr_rows = []
    sr_rows.append(section_header("✅ SUCCESS RATE", "billing-sr"))

    # Sub-section R0 (uniquement par user : users avec R0 succeeded / users avec ≥1 R0)
    sr_rows.append(subsection_header("R0", "billing-sr-sub"))

    def _sr_r0_pu(c, p, b):
        denom = m(c, p, b, "r0_att_users")
        return None if denom == 0 else m(c, p, b, "r0_succ_users") / denom

    def _sr_r0_pu_total(b):
        denom = _sum_atom(b, "r0_att_users")
        return None if denom == 0 else _sum_atom(b, "r0_succ_users") / denom

    sr_rows.append(kpi_row(
        "Success Rate R0 — Per User", fr_pct, _sr_r0_pu,
        total_fn=_sr_r0_pu_total, lower_is_better=False,
        section_class="billing-sr-row"
    ))

    # Sub-section R1
    sr_rows.append(subsection_header("R1", "billing-sr-sub"))

    def _sr_r1_fa(c, p, b):
        denom = m(c, p, b, "r1_fa_tx")
        return None if denom == 0 else m(c, p, b, "r1_fa_succ_tx") / denom

    def _sr_r1_pu(c, p, b):
        denom = m(c, p, b, "r1_att_users")
        return None if denom == 0 else m(c, p, b, "r1_succ_users") / denom

    def _sr_r1_fa_total(b):
        denom = _sum_atom(b, "r1_fa_tx")
        return None if denom == 0 else _sum_atom(b, "r1_fa_succ_tx") / denom

    def _sr_r1_pu_total(b):
        denom = _sum_atom(b, "r1_att_users")
        return None if denom == 0 else _sum_atom(b, "r1_succ_users") / denom

    sr_rows.append(kpi_row(
        "Success Rate R1 — First Attempt", fr_pct, _sr_r1_fa,
        total_fn=_sr_r1_fa_total, lower_is_better=False,
        section_class="billing-sr-row"
    ))
    sr_rows.append(kpi_row(
        "Success Rate R1 — Per User", fr_pct, _sr_r1_pu,
        total_fn=_sr_r1_pu_total, lower_is_better=False,
        section_class="billing-sr-row"
    ))

    # Sub-section Total (toutes R indexes)
    sr_rows.append(subsection_header("Total (toutes R indexes)", "billing-sr-sub"))

    def _sr_all_fa(c, p, b):
        denom = m(c, p, b, "all_fa_tx")
        return None if denom == 0 else m(c, p, b, "all_fa_succ_tx") / denom

    def _sr_all_pu(c, p, b):
        denom = m(c, p, b, "all_att_users")
        return None if denom == 0 else m(c, p, b, "all_succ_users") / denom

    def _sr_all_fa_total(b):
        denom = _sum_atom(b, "all_fa_tx")
        return None if denom == 0 else _sum_atom(b, "all_fa_succ_tx") / denom

    def _sr_all_pu_total(b):
        denom = _sum_atom(b, "all_att_users")
        return None if denom == 0 else _sum_atom(b, "all_succ_users") / denom

    sr_rows.append(kpi_row(
        "Success Rate — First Attempt", fr_pct, _sr_all_fa,
        total_fn=_sr_all_fa_total, lower_is_better=False,
        section_class="billing-sr-row"
    ))
    sr_rows.append(kpi_row(
        "Success Rate — Per User", fr_pct, _sr_all_pu,
        total_fn=_sr_all_pu_total, lower_is_better=False,
        section_class="billing-sr-row"
    ))

    # ========================================================================
    # Section 3 — VAMP
    # ========================================================================
    vamp_rows = []
    vamp_rows.append(section_header("⚠️ VAMP", "billing-vamp"))

    # Volumes
    vamp_rows.append(subsection_header("Volumes", "billing-vamp-sub"))
    vamp_rows.append(kpi_row(
        "Volume tx succeeded (total)", fr_int,
        lambda c, p, b: m(c, p, b, "volume_succ_total"),
        total_fn=lambda b: _sum_atom(b, "volume_succ_total"),
        lower_is_better=False, section_class="billing-vamp-row"
    ))
    vamp_rows.append(kpi_row(
        "Nb d'alertes VISA", fr_int,
        lambda c, p, b: m(c, p, b, "alerts_visa"),
        total_fn=lambda b: _sum_atom(b, "alerts_visa"),
        lower_is_better=True, section_class="billing-vamp-row"
    ))
    vamp_rows.append(kpi_row(
        "Nb d'alertes MC", fr_int,
        lambda c, p, b: m(c, p, b, "alerts_mc"),
        total_fn=lambda b: _sum_atom(b, "alerts_mc"),
        lower_is_better=True, section_class="billing-vamp-row"
    ))

    # Taux
    vamp_rows.append(subsection_header("Taux", "billing-vamp-sub"))

    def _vamp_visa(c, p, b):
        denom = m(c, p, b, "tx_succ_visa")
        return None if denom == 0 else m(c, p, b, "alerts_visa") / denom

    def _vamp_mc(c, p, b):
        denom = m(c, p, b, "tx_succ_mc")
        return None if denom == 0 else m(c, p, b, "alerts_mc") / denom

    def _vamp_visa_total(b):
        denom = _sum_atom(b, "tx_succ_visa")
        return None if denom == 0 else _sum_atom(b, "alerts_visa") / denom

    def _vamp_mc_total(b):
        denom = _sum_atom(b, "tx_succ_mc")
        return None if denom == 0 else _sum_atom(b, "alerts_mc") / denom

    vamp_rows.append(kpi_row(
        "VAMP Ratio VISA", fr_pct, _vamp_visa,
        total_fn=_vamp_visa_total, lower_is_better=True,
        section_class="billing-vamp-row"
    ))
    vamp_rows.append(kpi_row(
        "VAMP Ratio MC", fr_pct, _vamp_mc,
        total_fn=_vamp_mc_total, lower_is_better=True,
        section_class="billing-vamp-row"
    ))

    rows_html = "".join(proc_rows + sr_rows + vamp_rows)

    table_html = (
        "<table class='exec-table billing-table'>"
        f"<thead><tr><th class='exec-kpi-label'>KPI</th>{header_cells}</tr></thead>"
        f"<tbody>{rows_html}</tbody>"
        "</table>"
    )

    # --- Bandeau période ---
    days_in_window = (s1_end - s1_start).days + 1
    iso_w1 = s1_start.isocalendar()[1]
    iso_w2 = s2_start.isocalendar()[1]
    if days_in_window < 7:
        s1_label = f"<b>S{iso_w1:02d} WTD :</b> {s1_start.strftime('%d/%m')} → {s1_end.strftime('%d/%m')} ({days_in_window}j)"
        s2_label = f"<b>S{iso_w2:02d} WTD-1 :</b> {s2_start.strftime('%d/%m')} → {s2_end.strftime('%d/%m')}"
    else:
        s1_label = f"<b>S{iso_w1:02d} :</b> {s1_start.strftime('%d/%m')} → {s1_end.strftime('%d/%m')}"
        s2_label = f"<b>S{iso_w2:02d} (S-1) :</b> {s2_start.strftime('%d/%m')} → {s2_end.strftime('%d/%m')}"
    period_label = f"{s1_label} &middot; {s2_label}"

    css_extra = """
    <style>
      /* Section headers : bandeau sombre uppercase (segmente PROCESSING / SR / VAMP) */
      .billing-section-header td {
        font-size: 13px !important;
        font-weight: 700 !important;
        text-transform: uppercase !important;
        padding: 10px 14px !important;
        letter-spacing: 0.06em !important;
        text-align: left !important;
        color: white !important;
        background: #0f172a !important;
        border-top: 2px solid white !important;
      }

      /* Sub-section headers (R1, Total, Volumes, Taux) */
      .billing-subsection-header td {
        font-size: 11px !important;
        font-weight: 700 !important;
        text-transform: uppercase !important;
        padding: 6px 14px !important;
        letter-spacing: 0.05em !important;
        text-align: left !important;
        color: #475569 !important;
        background: #f1f5f9 !important;
        border-top: 1px solid #cbd5e1 !important;
        border-bottom: 1px solid #cbd5e1 !important;
      }

      /* Séparation des colonnes : trait fin entre chaque colonne, trait ÉPAIS
         à chaque changement de PSP (regroupement des colonnes par PSP). */
      .billing-table th, .billing-table td { border-right: 1px solid #e5e8ee; }
      .billing-table td.exec-grp-start,
      .billing-table th.exec-grp-start { border-left: 3px solid #64748b !important; }
      /* La bordure épaisse ne doit pas s'appliquer aux bandeaux de section
         (colspan) — ils restent pleine largeur. */
      .billing-section-header td, .billing-subsection-header td {
        border-right: 0 !important; border-left: 0 !important;
      }
    </style>
    """

    return (
        _EXEC_BASE_CSS + css_extra
        + "<div class='exec-summary'>"
        + f"<div class='exec-period'>📅 {period_label}</div>"
        + "<h3 class='exec-section'>Executive Summary Billing — par Conciergerie × PSP réel</h3>"
        + "<div class='exec-scroll'>" + table_html + "</div>"
        + "</div>"
    )


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
st.title("📊 Weekly PSP Report")
st.caption("Funnel + VAMP — basé sur le skill `weekly-psp-report` (v3) — dimensions dynamiques.")

# Sidebar: navigation (Option A) — une seule page exécute ses requêtes à la
# fois. Avant (st.tabs), le contenu des 7 onglets s'exécutait à CHAQUE
# interaction. Ici on ne charge que la page regardée, et les contrôles funnel
# (dates/dims/filtres) seulement si la page en a besoin.
st.sidebar.header("Page")
_PAGES = ["Executive Summary", "Exec par PSP — Trimestre", "Executive Summary Billing",
          "Exec Billing par PSP — Trimestre",
          "Funnel Booking", "Funnel Magazine", "LTV", "VAMP Cohort", "VAMP Date", "Analyse A/B"]
page = st.sidebar.radio("Aller à", _PAGES, index=0, key="page_nav",
                        label_visibility="collapsed")
st.sidebar.divider()

# Pages qui ont besoin des contrôles funnel (dates / dimensions / filtres).
_FUNNEL_PAGES = {"Funnel Booking", "Funnel Magazine", "LTV", "VAMP Cohort", "VAMP Date"}
_needs_controls = page in _FUNNEL_PAGES

# Defaults (utilisés quand la page n'utilise pas la sidebar funnel)
_today = date.today()
_default_end = _today - timedelta(days=_today.weekday() + 1)   # dernier dimanche
_default_start = _default_end - timedelta(days=10 * 7 - 1)     # 10 semaines avant
_picked_start, _picked_end = _default_start, _default_end
dims: list = []
filters: dict = {}
weeks_list = None

if _needs_controls:
    # Sidebar: date range
    st.sidebar.header("Période")
    _date_range = st.sidebar.date_input(
        "Plage de cohortes",
        value=(_default_start, _default_end),
        min_value=_today - timedelta(days=365 * 2),
        max_value=_today,
        help="Les semaines complètes (lundi → dimanche) qui intersectent la plage.",
        key="date_range",
    )
    if isinstance(_date_range, tuple) and len(_date_range) == 2:
        _picked_start, _picked_end = _date_range

    st.sidebar.divider()

    # Sidebar: dimensions (column-axis splits) — two independent dropdowns.
    st.sidebar.header("Dimensions")
    _NONE_LABEL = "— aucune —"
    _dim_options = [_NONE_LABEL] + [d[1] for d in DIMENSION_DIMS]
    dim1_label = st.sidebar.selectbox(
        "Dimension 1 (extérieure)", options=_dim_options,
        index=_dim_options.index("Date (semaine)"),
        help="Niveau extérieur du split de colonnes.", key="dim1_selector",
    )
    dim2_label = st.sidebar.selectbox(
        "Dimension 2 (intérieure)", options=_dim_options, index=0,
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

    _granularity = date_dim_key(dims) or "date_week"
    weeks_list = periods_in_range(_picked_start, _picked_end, _granularity)
    _period_word = {"date_day": "jour", "date_week": "semaine", "date_month": "mois"}[_granularity]
    st.sidebar.caption(f"{len(weeks_list)} {_period_word}{'s' if len(weeks_list) > 1 else ''} sélectionné{'s' if len(weeks_list) > 1 else ''}")

    st.sidebar.divider()

    # Sidebar: filters (scoped to the selected date range)
    st.sidebar.header("Filtres")
    with st.spinner("Chargement des options de filtre…"):
        try:
            opts_df = run_query(filter_options_sql(_picked_start, _picked_end))
        except Exception as e:
            st.sidebar.error(f"Échec chargement filtres : {e}")
            opts_df = pd.DataFrame(columns=["dim", "val", "n"])
    for key, label, _fm, _ft in FILTER_DIMS:
        dim_opts = opts_df[opts_df["dim"] == key].sort_values("n", ascending=False)
        choices = [v if v != "" else "(empty)" for v in dim_opts["val"].tolist()]
        counts = {v if v != "" else "(empty)": int(n) for v, n in zip(dim_opts["val"], dim_opts["n"])}
        selected = st.sidebar.multiselect(
            label, options=choices, default=[],
            format_func=lambda v, _c=counts: f"{v} ({_c.get(v, 0):,})".replace(",", " "),
            key=f"filter_{key}",
        )
        if selected:
            filters[key] = selected

# Cache controls (toujours visibles)
st.sidebar.divider()
col_a, col_b = st.sidebar.columns(2)
if col_a.button("🔄 Rafraîchir", use_container_width=True, help="Vide le cache et recharge"):
    st.cache_data.clear()
    st.rerun()
col_b.caption("TTL cache: 24h")

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
if _needs_controls:
    st.markdown(header_html, unsafe_allow_html=True)

# ===========================================================================
# Onglet ANALYSE A/B — cohortes figées (indépendant de la sidebar)
# ===========================================================================
# Onglet "mouvant" : on fige des cohortes A/B précises (fenêtre de signup +
# conditions psp/productId/metadata) et on affiche, par PSP, le MÊME funnel
# (KPI R0→R4 identiques aux onglets Funnel) pour chaque cohorte, en colonnes
# Booking + Magazine. Réutilise funnel_sql + build_funnel_table (mêmes calculs).
#
# AB_WINDOW : valeurs par défaut des helpers (chaque onglet utilise sa propre
# fenêtre : NMI_WINDOW, LBP_WINDOW, TP_DD_WIN, TP_PREAUTH_WIN).
AB_WINDOW_START = date(2026, 6, 16)
AB_WINDOW_END   = date(2026, 6, 21)

# --- NMI : 1 colonne par processeur réel (MidId du R0 derrière la gateway NMI).
# Référentiel MidId (cf. doc Notion + _brand_psp_concat). New/Old Kadima séparés
# par MidId, confirmé par le flag abNmiProcessor ('new_kadima' -> f6130732 ;
# 'old_kadima' -> 5f915cec). Pas de Référence -> mirror=False. Fenêtre 16 → 21
# (plage où Old & New Kadima coexistent ; arrêt au 21 pour maturité propre).
NMI_WINDOW = (date(2026, 6, 16), date(2026, 6, 21))
_NMI_MIDS = [
    ("NMI EMS",        "688b5f4e-4f33-4b16-b2c7-6c601ba15306"),
    ("NMI Cliq",       "4a7af99e-20b3-48b3-8e93-6fc39f8012b0"),
    ("NMI CASH",       "9cf8e38c-c719-4d33-a1e3-aaa72cf88cdd"),
    ("NMI New Kadima", "f6130732-c577-4d1d-9ab9-802900b478a0"),
    ("NMI Old Kadima", "5f915cec-f0b3-40e9-9908-b3590b791448"),
]
NMI_COHORTS = [
    (name, f"fm.ms_default_psp='nmi' AND rmid.MidId='{mid}'")
    for name, mid in _NMI_MIDS
]

# --- La Banque Postale (psp = labanquepostale) : 1 colonne par conciergerie.
# Pas de test A/B ici, juste une répartition par brand -> pas de Référence ni de
# mirror (mirror=False). Fenêtre signup 09 → 21/06.
LBP_WINDOW = (date(2026, 6, 9), date(2026, 6, 21))
def _lbp_pred(conc: str) -> str:
    return (f"fm.ms_default_psp='labanquepostale' "
            f"AND LOWER(TRIM(SPLIT(COALESCE(fm.brand,''),' - ')[OFFSET(0)]))='{conc}'")
LBP_COHORTS = [
    ("RapidOxy LBP", _lbp_pred("rapidoxy")),
    ("Jumpaide LBP", _lbp_pred("jumpaide.com")),
    ("Concimax LBP", _lbp_pred("concimax")),
]

# --- TP : 2 tests aux dates distinctes, 1 tableau chacun. La Référence partage
# la fenêtre de sa cohorte (build_ab_table applique la même fenêtre à toutes les
# colonnes). « Preauth + DD » supprimé.
TP_DD_WIN = (date(2026, 6, 8), date(2026, 6, 14))
TP_PREAUTH_WIN = (date(2026, 6, 16), date.today())
TP_DD_COHORTS = [
    ("Référence", "fm.ms_default_psp='trustpayment' "
     "AND COALESCE(JSON_VALUE(sm.Metadata,'$.dynamic_descriptor_tp'),'')!='B'"),
    ("DD TP", "fm.ms_default_psp='trustpayment' "
     "AND JSON_VALUE(sm.Metadata,'$.dynamic_descriptor_tp')='B'"),
]
TP_PREAUTH_COHORTS = [
    ("Référence", "fm.ms_default_psp='trustpayment' "
     "AND COALESCE(JSON_VALUE(sm.Metadata,'$.abPreAuth'),'')!='B'"),
    ("Pre Auth TP", "fm.ms_default_psp='trustpayment' "
     "AND JSON_VALUE(sm.Metadata,'$.abPreAuth')='B'"),
]

def _ab_r0_mid_join(win_start: date, win_end: date) -> str:
    """LEFT JOIN donnant le MidId du R0 (processeur réel derrière NMI) par
    membership. Ajouté seulement si le prédicat référence `rmid.` (cohortes NMI
    par processeur : EMS / Cliq / CASH / Kadima). Borné ±5 j autour de la fenêtre."""
    sub = _r0_mid_subquery(
        f"AND f.t_date BETWEEN DATE_SUB('{win_start.isoformat()}', INTERVAL 5 DAY) "
        f"AND DATE_ADD('{win_end.isoformat()}', INTERVAL 5 DAY)"
    )
    return (
        "    LEFT JOIN (\n    " + sub + "\n    ) rmid ON rmid.membership_id = fm.membership_id\n"
    )


def _ab_pool_sql(predicate: str, win_start: date = AB_WINDOW_START,
                 win_end: date = AB_WINDOW_END) -> str:
    """Subquery renvoyant les customer_id de la cohorte (membership Booking
    matchant le prédicat, signup dans la fenêtre). Injecté dans funnel_sql."""
    r0_mid = _ab_r0_mid_join(win_start, win_end) if "rmid." in predicate else ""
    return (
        "    SELECT DISTINCT fm.customer_id\n"
        "    FROM `eu-andy-marketing-raw.dashboard.fact_memberships` fm\n"
        "    JOIN `eu-andy-marketing-raw.silver_sgw.stg_memberships` sm\n"
        "      ON CAST(fm.membership_id AS STRING) = CAST(sm.Id AS STRING)\n"
        "    LEFT JOIN `eu-andy-marketing-raw.silver_sgw.stg_prices` pr\n"
        "      ON CAST(fm.price_id AS STRING) = CAST(pr.Id AS STRING)\n"
        f"{r0_mid}"
        f"    WHERE DATE(sm.CreatedAtUtc) BETWEEN '{win_start.isoformat()}' AND '{win_end.isoformat()}'\n"
        "      AND fm.brand_type = 'Booking'\n"
        f"      AND ({predicate})"
    )


def _ab_test_dates_subquery(test_preds: list, win_start: date, win_end: date) -> str:
    """Sous-requête : dates de signup (Booking) où AU MOINS une cohorte de test
    existe, dans la fenêtre. Sert à caler la Référence sur la plage réelle du test."""
    ors = " OR ".join(f"({p})" for p in test_preds)
    return (
        "SELECT DISTINCT DATE(sm.CreatedAtUtc)\n"
        "      FROM `eu-andy-marketing-raw.dashboard.fact_memberships` fm\n"
        "      JOIN `eu-andy-marketing-raw.silver_sgw.stg_memberships` sm ON CAST(fm.membership_id AS STRING)=CAST(sm.Id AS STRING)\n"
        "      LEFT JOIN `eu-andy-marketing-raw.silver_sgw.stg_prices` pr ON CAST(fm.price_id AS STRING)=CAST(pr.Id AS STRING)\n"
        f"      WHERE DATE(sm.CreatedAtUtc) BETWEEN '{win_start.isoformat()}' AND '{win_end.isoformat()}'\n"
        f"        AND fm.brand_type='Booking' AND ({ors})"
    )


def _ab_mirror_cohorts(cohorts: list, win_start: date, win_end: date) -> list:
    """Restreint la 1re cohorte (= Référence) aux DATES de signup des cohortes de
    test (les suivantes), pour que la Référence couvre la même plage que le test
    qu'elle accompagne (cf. demande : 'la référence doit avoir la même plage de
    date que la cohorte d'en face'). Les cohortes de test gardent leur prédicat."""
    if len(cohorts) < 2:
        return list(cohorts)
    ref_name, ref_pred = cohorts[0]
    tests = [p for _, p in cohorts[1:]]
    sub = _ab_test_dates_subquery(tests, win_start, win_end)
    mirrored = f"({ref_pred}) AND DATE(sm.CreatedAtUtc) IN (\n      {sub}\n    )"
    return [(ref_name, mirrored)] + list(cohorts[1:])


def _ab_weeks(win_start: date, win_end: date):
    """Jours de la fenêtre signup ± 1 jour de marge (écart minuit customer vs
    membership). Le pool customer définit la vraie cohorte ; dims=[] => 1 colonne."""
    return periods_in_range(
        date.fromordinal(win_start.toordinal() - 1),
        date.fromordinal(win_end.toordinal() + 1),
        "date_day",
    )


def ab_maturity_sql(cohorts=(), win_start: date = AB_WINDOW_START,
                    win_end: date = AB_WINDOW_END, mirror: bool = True) -> str:
    """Par cohorte : jours écoulés entre la veille minuit et le dernier R0 créé
    (= dernier signup Booking de la cohorte), + nb de users. cohorts = liste de
    (group, name, predicate). mirror=True : la Référence de chaque groupe est
    restreinte aux dates de signup de ses cohortes de test (même plage)."""
    if mirror:
        by_g, order = {}, []
        for psp, name, pred in cohorts:
            if psp not in by_g:
                by_g[psp] = []
                order.append(psp)
            by_g[psp].append((name, pred))
        cohorts = [
            (psp, n, p)
            for psp in order
            for (n, p) in _ab_mirror_cohorts(by_g[psp], win_start, win_end)
        ]
    parts = []
    for psp, name, pred in cohorts:
        r0_mid = _ab_r0_mid_join(win_start, win_end) if "rmid." in pred else ""
        parts.append(
            f"SELECT '{psp}' AS psp, '{name}' AS cohort,\n"
            "  DATE_DIFF(DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY), DATE(MAX(sm.CreatedAtUtc)), DAY) AS days_elapsed,\n"
            "  DATE(MAX(sm.CreatedAtUtc)) AS last_r0,\n"
            "  COUNT(DISTINCT fm.customer_id) AS users\n"
            "FROM `eu-andy-marketing-raw.dashboard.fact_memberships` fm\n"
            "JOIN `eu-andy-marketing-raw.silver_sgw.stg_memberships` sm\n"
            "  ON CAST(fm.membership_id AS STRING) = CAST(sm.Id AS STRING)\n"
            "LEFT JOIN `eu-andy-marketing-raw.silver_sgw.stg_prices` pr\n"
            "  ON CAST(fm.price_id AS STRING) = CAST(pr.Id AS STRING)\n"
            f"{r0_mid}"
            f"WHERE DATE(sm.CreatedAtUtc) BETWEEN '{win_start.isoformat()}' AND '{win_end.isoformat()}'\n"
            f"  AND fm.brand_type='Booking' AND ({pred})"
        )
    return "\nUNION ALL\n".join(parts)


def build_ab_table(cohorts, win_start: date, win_end: date,
                   mirror: bool = True) -> pd.DataFrame:
    """Table combinée : lignes = KPI funnel R0→R4, colonnes = (cohorte ×
    {Booking, Magazine}). cohorts = liste de (name, predicate). Réutilise
    build_funnel_table -> calculs identiques au funnel. mirror=True : la 1re
    cohorte (Référence) est restreinte aux dates de signup des cohortes de test."""
    if mirror:
        cohorts = _ab_mirror_cohorts(cohorts, win_start, win_end)
    weeks = _ab_weeks(win_start, win_end)
    groups = [(name, brand) for (name, _pred) in cohorts for brand in ("Booking", "Magazine")]
    col_dicts: dict = {}
    # cohort_map = même pools (customer-level) + label cohorte, en UNION ALL.
    # => 1 requête funnel par brand (au lieu de 2×N), sémantique identique.
    cmap_parts = []
    for name, pred in cohorts:
        pool = _ab_pool_sql(pred, win_start, win_end)
        safe = name.replace("'", "''")
        cmap_parts.append(f"    SELECT customer_id, '{safe}' AS cohort FROM (\n{pool}\n    ) _cp_{len(cmap_parts)}")
    cohort_map_sql = "\n    UNION ALL\n".join(cmap_parts)
    cohort_dim = ("cohort", "Cohorte", "RAW:cmap.cohort", "RAW:cmap.cohort")
    for brand in ("Booking", "Magazine"):
        df = run_query(funnel_sql(brand, {}, [cohort_dim], weeks,
                                  granularity_override="date_day",
                                  cohort_map_sql=cohort_map_sql, max_rx=2))
        tbl = build_funnel_table(df, brand, [cohort_dim], picked_end=win_end)
        for name, _pred in cohorts:
            col_dicts[(name, brand)] = {
                str(r["__key__"]): r.get((name,), "") for _, r in tbl.iterrows()
            }

    rows = []
    for section_name, kpis in FUNNEL_KPI_SECTIONS:
        if section_name == "LTV Simulator":
            continue  # simulateur revenu, hors périmètre de l'analyse A/B
        present = [k for k in kpis if any(k in col_dicts[g] for g in groups)]
        if not present:
            continue
        rows.append({"__key__": "__SECTION__" + section_name, **{g: "" for g in groups}})
        for k in present:
            rows.append({"__key__": k, **{g: col_dicts[g].get(k, "—") for g in groups}})

    out = pd.DataFrame(rows)
    out.attrs["dims"] = [("cohort", "Cohorte", "", ""), ("brand", "Abo", "", "")]
    out.attrs["groups"] = groups
    return out


def render_ab_maturity(df_mat: pd.DataFrame, psp_group: str) -> str:
    """Petit bandeau de maturité au-dessus de chaque table PSP."""
    sub = df_mat[df_mat["psp"] == psp_group]
    if sub.empty:
        return ""
    cells = []
    for _, r in sub.iterrows():
        d = int(r["days_elapsed"]) if r["days_elapsed"] is not None else 0
        cells.append(
            f"<tr><td style='padding:2px 10px;'>{_esc(r['cohort'])}</td>"
            f"<td style='padding:2px 10px;text-align:right;font-weight:600;'>{d} j</td>"
            f"<td style='padding:2px 10px;text-align:right;color:#64748b;'>{int(r['users'])} users</td>"
            f"<td style='padding:2px 10px;color:#64748b;'>dernier R0 : {_esc(r['last_r0'])}</td></tr>"
        )
    return (
        "<div style='margin:6px 0 10px;font-size:13px;'>"
        "<div style='color:#64748b;margin-bottom:4px;'>⏱️ Maturité (jours écoulés depuis le dernier R0 créé — "
        "indique quels R sont analysables) :</div>"
        "<table style='border-collapse:collapse;'>" + "".join(cells) + "</table></div>"
    )


# Tabs
def _ab_conc_case(alias: str) -> str:
    return (
        f"CASE LOWER(SPLIT(COALESCE({alias}.brand,''),' - ')[OFFSET(0)]) "
        "WHEN 'reserv-go' THEN 'Reserv-Go' "
        "WHEN 'book-ici' THEN 'Book-Ici' "
        "WHEN 'resadexa' THEN 'Resadexa' ELSE 'autre' END"
    )


def ab_dd_tp_matrix_sql() -> str:
    """Focus DD TP : matrice conciergerie Booking × magazine cross-brand sur la
    cohorte DD TP (1 ligne par conciergerie Booking, comptes de users)."""
    return f"""
WITH cohort_bk AS (
  SELECT DISTINCT fm.customer_id, {_ab_conc_case('fm')} AS booking_conc
  FROM `eu-andy-marketing-raw.dashboard.fact_memberships` fm
  JOIN `eu-andy-marketing-raw.silver_sgw.stg_memberships` sm
    ON CAST(fm.membership_id AS STRING) = CAST(sm.Id AS STRING)
  WHERE DATE(sm.CreatedAtUtc) BETWEEN '{TP_DD_WIN[0].isoformat()}' AND '{TP_DD_WIN[1].isoformat()}'
    AND fm.brand_type='Booking' AND fm.ms_default_psp='trustpayment'
    AND JSON_VALUE(sm.Metadata,'$.dynamic_descriptor_tp')='B'
    AND fm.ms_status NOT IN ('abandonned','processing','paused')
),
cohort_mag AS (
  SELECT DISTINCT fm.customer_id, {_ab_conc_case('fm')} AS mag_conc
  FROM `eu-andy-marketing-raw.dashboard.fact_memberships` fm
  JOIN `eu-andy-marketing-raw.silver_sgw.stg_memberships` sm
    ON CAST(fm.membership_id AS STRING) = CAST(sm.Id AS STRING)
  WHERE DATE(sm.CreatedAtUtc) BETWEEN '{TP_DD_WIN[0].isoformat()}' AND '{TP_DD_WIN[1].isoformat()}'
    AND fm.brand_type='Magazine' AND fm.ms_status NOT IN ('abandonned','processing','paused')
    AND fm.customer_id IN (SELECT customer_id FROM cohort_bk)
)
SELECT b.booking_conc,
  COUNT(DISTINCT b.customer_id) AS booking_users,
  COUNT(DISTINCT IF(m.mag_conc='Reserv-Go', m.customer_id, NULL)) AS mag_reservgo,
  COUNT(DISTINCT IF(m.mag_conc='Book-Ici',  m.customer_id, NULL)) AS mag_bookici,
  COUNT(DISTINCT IF(m.mag_conc='Resadexa',  m.customer_id, NULL)) AS mag_resadexa
FROM cohort_bk b
LEFT JOIN cohort_mag m USING(customer_id)
GROUP BY b.booking_conc
"""


def render_dd_tp_matrix(df: pd.DataFrame) -> str:
    """Matrice conciergerie Booking (lignes) × magazine cross-brand (colonnes),
    avec total mag par ligne et par colonne. Séparation Booking | mags."""
    order = ["Reserv-Go", "Book-Ici", "Resadexa"]
    mag_cols = [("Reserv-Go", "mag_reservgo"), ("Book-Ici", "mag_bookici"), ("Resadexa", "mag_resadexa")]
    by = {str(r["booking_conc"]): r for _, r in df.iterrows()}
    col_tot = {k: 0 for _, k in mag_cols}
    grand = 0
    cell = "padding:6px 14px;text-align:right;border-bottom:1px solid #e2e8f0;"
    sep = "border-left:3px solid #334155;"
    rows_html = ""
    for conc in order:
        r = by.get(conc)
        bk = int(r["booking_users"]) if r is not None else 0
        tds, rowtot = [], 0
        for i, (_mlabel, k) in enumerate(mag_cols):
            v = int(r[k]) if r is not None else 0
            col_tot[k] += v
            rowtot += v
            disp = str(v) if v else "<span style='color:#cbd5e1;'>–</span>"
            tds.append(f"<td style='{cell}{sep if i == 0 else ''}'>{disp}</td>")
        grand += rowtot
        rows_html += (
            f"<tr><td style='{cell}text-align:left;font-weight:600;'>{_esc(conc)}</td>"
            f"<td style='{cell}'>{bk}</td>" + "".join(tds)
            + f"<td style='{cell}font-weight:700;'>{rowtot}</td></tr>"
        )
    tot_tds = "".join(
        f"<td style='{cell}{sep if i == 0 else ''}font-weight:700;'>{col_tot[k]}</td>"
        for i, (_, k) in enumerate(mag_cols)
    )
    rows_html += (
        f"<tr style='background:#f1f5f9;'><td style='{cell}text-align:left;font-weight:700;'>Total mag</td>"
        f"<td style='{cell}'></td>{tot_tds}"
        f"<td style='{cell}font-weight:800;'>{grand}</td></tr>"
    )
    head = (
        f"<th style='{cell}text-align:left;'>Conciergerie (Booking)</th>"
        f"<th style='{cell}'>Booking</th>"
        f"<th style='{cell}{sep}'>Mag Reserv-Go</th>"
        f"<th style='{cell}'>Mag Book-Ici</th>"
        f"<th style='{cell}'>Mag Resadexa</th>"
        f"<th style='{cell}'>Total mag</th>"
    )
    return (
        "<table style='border-collapse:collapse;font-size:13px;margin-top:6px;'>"
        f"<thead><tr>{head}</tr></thead><tbody>{rows_html}</tbody></table>"
    )


if page == "Executive Summary":
    # Indépendant de la sidebar : a son propre sélecteur de mois (MTD courant
    # + 11 mois précédents complets). Compare auto à M-1 équivalent.
    _exec_months = _months_for_exec_selector(12)
    _exec_labels = [m[0] for m in _exec_months]
    _selected_label = st.selectbox(
        "📅 Mois à analyser",
        options=_exec_labels,
        index=0,  # défaut = mois courant en MTD
        key="exec_month_selector",
        help="Indépendant de la sidebar. Compare auto à M-1 équivalent.",
    )
    _selected_idx = _exec_labels.index(_selected_label)
    _exec_p_start = _exec_months[_selected_idx][1]
    _exec_p_end   = _exec_months[_selected_idx][2]

    with st.spinner("Executive Summary…"):
        try:
            df_exec = run_query(exec_summary_sql(_exec_p_start, _exec_p_end))
            st.markdown(
                render_exec_summary(df_exec, _exec_p_start, _exec_p_end),
                unsafe_allow_html=True,
            )
        except Exception as e:
            st.error(f"Erreur Executive Summary : {e}")

elif page == "Exec par PSP — Trimestre":
    # Agrégé par PSP (conciergeries regroupées), 3 mois glissants finissant au
    # mois sélectionné (Avr/Mai/Juin si juin). NMI regroupé.
    _eq_months = _months_for_exec_selector(12)
    _eq_labels = [mth[0] for mth in _eq_months]
    _eq_sel = st.selectbox("📅 Mois de fin (→ trimestre des 3 mois)", options=_eq_labels,
                           index=0, key="exec_quarter_selector",
                           help="Sélectionne le dernier mois ; les 2 mois précédents sont ajoutés.")
    _eq_idx = _eq_labels.index(_eq_sel)
    _eq_p_start = _eq_months[_eq_idx][1]
    _eq_p_end = _eq_months[_eq_idx][2]
    with st.spinner("Exec par PSP — Trimestre…"):
        try:
            df_eq = run_query(exec_quarter_sql(_eq_p_start, _eq_p_end))
            st.markdown(render_exec_quarter(df_eq, _eq_p_start, _eq_p_end),
                        unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Erreur Exec par PSP : {e}")

elif page == "Executive Summary Billing":
    # Indépendant de la sidebar : son propre sélecteur de semaine (WTD courant
    # + 11 semaines précédentes complètes). Comparaison vs S-1.
    _billing_weeks = _weeks_for_billing_selector(12)
    _billing_labels = [w[0] for w in _billing_weeks]
    _billing_sel_label = st.selectbox(
        "📅 Semaine à analyser",
        options=_billing_labels,
        index=0,
        key="billing_week_selector",
        help="Indépendant de la sidebar. Compare auto à la semaine précédente (S-1).",
    )
    _billing_sel_idx = _billing_labels.index(_billing_sel_label)
    _b_w_start = _billing_weeks[_billing_sel_idx][1]
    _b_w_end   = _billing_weeks[_billing_sel_idx][2]
    _b_s1s, _b_s1e, _b_s2s, _b_s2e = _exec_billing_period_bounds(_b_w_start, _b_w_end)

    with st.spinner("Executive Summary Billing…"):
        try:
            df_billing = run_query(exec_billing_sql(_b_s1s, _b_s1e, _b_s2s, _b_s2e))
            st.markdown(
                render_exec_billing(df_billing, _b_s1s, _b_s1e, _b_s2s, _b_s2e),
                unsafe_allow_html=True,
            )
        except Exception as e:
            st.error(f"Erreur Executive Summary Billing : {e}")

elif page == "Exec Billing par PSP — Trimestre":
    # Billing agrégé par PSP (conciergeries regroupées), 3 mois glissants
    # finissant au mois sélectionné. NMI regroupé. % vs mois précédent.
    _ebq_months = _months_for_exec_selector(12)
    _ebq_labels = [mth[0] for mth in _ebq_months]
    _ebq_sel = st.selectbox("📅 Mois de fin (→ trimestre des 3 mois)",
                            options=_ebq_labels, index=0,
                            key="exec_billing_quarter_selector",
                            help="Sélectionne le dernier mois ; les 2 mois précédents sont ajoutés.")
    _ebq_idx = _ebq_labels.index(_ebq_sel)
    _ebq_p_start = _ebq_months[_ebq_idx][1]
    _ebq_p_end = _ebq_months[_ebq_idx][2]
    with st.spinner("Exec Billing par PSP — Trimestre…"):
        try:
            df_ebq = run_query(exec_billing_quarter_sql(_ebq_p_start, _ebq_p_end))
            st.markdown(render_exec_billing_quarter(df_ebq, _ebq_p_start, _ebq_p_end),
                        unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Erreur Exec Billing par PSP : {e}")

elif page == "Funnel Booking":
    with st.spinner("Funnel Booking…"):
        try:
            df = run_query(funnel_sql("Booking", filters, dims, weeks_list,
                                       picked_end=_picked_end))
            table = build_funnel_table(df, "Booking", dims, picked_end=_picked_end)
            st.markdown(render_table_html(table), unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Erreur Funnel Booking : {e}")
    # Graph section under the table — own dim/granularity selectors, reuses
    # sidebar filters + date range.
    st.divider()
    render_funnel_graph("Booking", filters, _picked_start, _picked_end, key_prefix="booking")

elif page == "Funnel Magazine":
    with st.spinner("Funnel Magazine…"):
        try:
            df = run_query(funnel_sql("Magazine", filters, dims, weeks_list,
                                       picked_end=_picked_end))
            table = build_funnel_table(df, "Magazine", dims, picked_end=_picked_end)
            st.markdown(render_table_html(table), unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Erreur Funnel Magazine : {e}")
    # Graph section under the table — own dim/granularity selectors, reuses
    # sidebar filters + date range.
    st.divider()
    render_funnel_graph("Magazine", filters, _picked_start, _picked_end, key_prefix="magazine")

elif page == "LTV":
    st.caption(
        "Décompo R0→R1 + ARPU R1 + LTV simulée (Booking only). "
        "ARPU R1 et LTV en € nécessitent la dim **Prix Booking** (sinon affichage « — »). "
        "LTV simulée = R observés + projection via decay factors hardcodés (cohortes déc 2025)."
    )
    with st.spinner("LTV…"):
        try:
            # Tab LTV n'utilise pas les KPI TBB (% Billed / # To Be Billed) → on
            # passe max_rx=2 pour skip les CTEs r3_tbb_raw/r4_tbb_raw (jointures
            # stg_memberships coûteuses).
            # max_rx_observed dynamique : on demande au SQL tous les R où la
            # cohorte la plus ancienne de la fenêtre est encore mature (cycle
            # ref = 14j bimensuel). Permet à _ltv_compute d'utiliser le R réel
            # max observé (ex: R13 pour cohorte déc 2025 €19) au lieu d'être
            # plafonné à R4 par défaut.
            _ltv_max_rx_obs = _dynamic_max_rx_observed(weeks_list, today=date.today())
            df = run_query(funnel_sql(
                "Booking", filters, dims, weeks_list,
                max_rx=2, max_rx_observed=_ltv_max_rx_obs,
                picked_end=_picked_end,
            ))
            # VAMP réels — query séparée (cohorte Booking R0 + toutes tx du user)
            df_vamp = run_query(ltv_vamp_sql(
                filters, dims, weeks_list, picked_end=_picked_end,
            ))
            table = build_ltv_table(df, dims, picked_end=_picked_end,
                                     df_vamp=df_vamp)
            st.markdown(render_table_html(table), unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Erreur LTV : {e}")
    # Graph section under the table — réutilise render_funnel_graph avec
    # les sections KPI propres à la LTV.
    st.divider()
    render_funnel_graph(
        "Booking", filters, _picked_start, _picked_end, key_prefix="ltv",
        kpi_sections=LTV_KPI_SECTIONS, all_kpis=ALL_LTV_KPIS,
        max_rx=2, max_rx_observed_dynamic=True,
    )

elif page == "VAMP Cohort":
    with st.spinner("VAMP Cohort…"):
        try:
            df = run_query(vamp_cohort_sql(filters, dims, weeks_list))
            table = build_vamp_table(df, dims)
            st.markdown(render_table_html(table), unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Erreur VAMP Cohort : {e}")
    # Graph section under the table
    st.divider()
    render_vamp_graph("cohort", filters, _picked_start, _picked_end, key_prefix="vamp_cohort")

elif page == "VAMP Date":
    with st.spinner("VAMP Date…"):
        try:
            df = run_query(vamp_date_sql(filters, dims, weeks_list))
            table = build_vamp_table(df, dims)
            st.markdown(render_table_html(table), unsafe_allow_html=True)
        except Exception as e:
            st.error(f"Erreur VAMP Date : {e}")
    # Graph section under the table
    st.divider()
    render_vamp_graph("date", filters, _picked_start, _picked_end, key_prefix="vamp_date")

elif page == "Analyse A/B":
    st.caption(
        "Analyse A/B — cohortes figées, fenêtres propres à chaque test. "
        "Indépendant de la sidebar. **Seule la section choisie se charge** (pour aller vite)."
    )
    # Sous-navigation : on ne lance QUE les requêtes de la section sélectionnée
    # (avant : tout se chargeait d'un coup → ~25 requêtes funnel = très lent).
    _AB_SECTIONS = ["TP — DD", "TP — Pre Auth", "NMI", "La Banque Postale",
                    "Focus DD TP"]
    _ab_sel = st.radio("Section", _AB_SECTIONS, horizontal=True, key="ab_section",
                       label_visibility="collapsed")

    if _ab_sel in ("TP — DD", "TP — Pre Auth"):
        if _ab_sel == "TP — DD":
            _cohorts, _win, _grp = TP_DD_COHORTS, TP_DD_WIN, "DD TP"
        else:
            _cohorts, _win, _grp = TP_PREAUTH_COHORTS, TP_PREAUTH_WIN, "Pre Auth TP"
        st.subheader(f"TrustPayment — {_grp}")
        st.caption(f"Fenêtre signup {_win[0].strftime('%d/%m')} → {_win[1].strftime('%d/%m')}. "
                   "Référence restreinte aux dates de signup réelles du test (même plage).")
        with st.spinner(f"{_ab_sel}…"):
            try:
                _mat = run_query(ab_maturity_sql(
                    [(_grp, n, p) for n, p in _cohorts], _win[0], _win[1]))
                st.markdown(render_ab_maturity(_mat, _grp), unsafe_allow_html=True)
            except Exception as e:
                st.error(f"Erreur maturité {_grp} : {e}")
            try:
                _tp_tbl = build_ab_table(_cohorts, _win[0], _win[1])
                st.markdown(render_table_html(_tp_tbl), unsafe_allow_html=True)
            except Exception as e:
                st.error(f"Erreur table {_grp} : {e}")

    elif _ab_sel == "NMI":
        st.subheader("NMI — par processeur réel (MidId du R0)")
        st.caption(f"Fenêtre signup {NMI_WINDOW[0].strftime('%d/%m')} → "
                   f"{NMI_WINDOW[1].strftime('%d/%m')}. 1 colonne par processeur derrière "
                   "la gateway NMI (EMS / Cliq / CASH / New Kadima / Old Kadima).")
        with st.spinner("NMI…"):
            try:
                _nmi_mat = run_query(ab_maturity_sql(
                    [("NMI", n, p) for n, p in NMI_COHORTS],
                    NMI_WINDOW[0], NMI_WINDOW[1], mirror=False))
                st.markdown(render_ab_maturity(_nmi_mat, "NMI"), unsafe_allow_html=True)
            except Exception as e:
                st.error(f"Erreur maturité NMI : {e}")
            try:
                _nmi_tbl = build_ab_table(NMI_COHORTS, NMI_WINDOW[0], NMI_WINDOW[1],
                                          mirror=False)
                st.markdown(render_table_html(_nmi_tbl), unsafe_allow_html=True)
            except Exception as e:
                st.error(f"Erreur table NMI : {e}")

    elif _ab_sel == "La Banque Postale":
        st.subheader("La Banque Postale")
        st.caption(f"Fenêtre signup {LBP_WINDOW[0].strftime('%d/%m')} → "
                   f"{LBP_WINDOW[1].strftime('%d/%m')}. 1 colonne par conciergerie "
                   "(psp = labanquepostale).")
        with st.spinner("La Banque Postale…"):
            try:
                _lbp_mat = run_query(ab_maturity_sql(
                    [("LBP", n, p) for n, p in LBP_COHORTS],
                    LBP_WINDOW[0], LBP_WINDOW[1], mirror=False))
                st.markdown(render_ab_maturity(_lbp_mat, "LBP"), unsafe_allow_html=True)
            except Exception as e:
                st.error(f"Erreur maturité LBP : {e}")
            try:
                _lbp_tbl = build_ab_table(LBP_COHORTS, LBP_WINDOW[0], LBP_WINDOW[1],
                                          mirror=False)
                st.markdown(render_table_html(_lbp_tbl), unsafe_allow_html=True)
            except Exception as e:
                st.error(f"Erreur table LBP : {e}")

    elif _ab_sel == "Focus DD TP":
        st.subheader("Focus — DD TP · magazine cross-brand")
        st.caption(
            "Cohorte DD TP (descripteur dynamique). Par conciergerie en Booking, "
            "répartition des users sur le magazine d'une AUTRE conciergerie TP. "
            "Attendu : Booking ≈ Total mag de la ligne."
        )
        with st.spinner("Focus DD TP…"):
            try:
                _dd = run_query(ab_dd_tp_matrix_sql())
                st.markdown(render_dd_tp_matrix(_dd), unsafe_allow_html=True)
            except Exception as e:
                st.error(f"Erreur Focus DD TP : {e}")
