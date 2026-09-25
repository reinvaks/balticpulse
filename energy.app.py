from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

import energy_sources as _energy_sources
from data_quality import SourceHealth, assess as assess_source, mask as mask_source, human_age as dq_human_age
from energy_sources import (
    fetch_elering_prices,
    fetch_elering_system,
    fetch_gas_storage,
    fetch_entsoe_generation_by_type,
    fetch_entsoe_estonia_flows,
    fetch_entsoe_baltic_flows,
    fetch_entsoe_actual_load,
    fetch_eex_ngp_current,
    fetch_eex_ngp_history,
    fetch_eex_ttf_ngp,
    fetch_eia_brent,
    fetch_eex_eua_auction,
)

APP_BUILD_VERSION = "16.1.9"

TALLINN = ZoneInfo("Europe/Tallinn")
REGIONS = ["EE", "LV", "LT", "FI"]
BALTICS = ["EE", "LV", "LT"]

st.set_page_config(page_title="🇪🇪🇱🇻🇱🇹🇫🇮 BalticPulse | Energy Market Dashboard", page_icon="🇪🇪", layout="wide")

if getattr(_energy_sources, "BUILD_VERSION", None) != APP_BUILD_VERSION:
    st.error(
        f"BalticPulse failiversioonide konflikt: energy.app.py={APP_BUILD_VERSION}, "
        f"energy_sources.py={getattr(_energy_sources, 'BUILD_VERSION', 'vana/puudub')}. "
        "Laadi GitHubi kõik sama versiooni BalticPulse failid üle ja tee Streamlit Cloudis Reboot app."
    )
    st.stop()

def secret(name: str) -> str:
    try:
        return str(st.secrets.get(name, "") or "")
    except Exception:
        return ""


def source_badge(label: str, ok: bool | None = None, detail: str | None = None, *, level: str | None = None) -> None:
    """Compact source-health row: green=OK, yellow=partial/stale, red=failed."""
    if level is None:
        level = "ok" if ok else "error"
    icon = {"ok": "🟢", "warning": "🟡", "error": "🔴"}.get(level, "⚪")
    st.caption(f"{icon} {label}" + (f" — {detail}" if detail else ""))


def status_level(status, *, has_data: bool = True, age_min: float | None = None, warn_after_min: float | None = None) -> str:
    if not getattr(status, "ok", False):
        return "error"
    if not has_data:
        return "warning"
    if warn_after_min is not None and age_min is not None and age_min > warn_after_min:
        return "warning"
    note = str(getattr(status, "note", "") or "").lower()
    if any(k in note for k in ("fallback", "partial", "stale", "older", "mirror")):
        return "warning"
    return "ok"


def status_detail(status, *, age_text: str | None = None, extra: str | None = None) -> str:
    parts = []
    if age_text and age_text != "—":
        parts.append(f"andmed {age_text} vanad")
    if getattr(status, "status_code", None) is not None:
        parts.append(f"HTTP {status.status_code}")
    msg = getattr(status, "error", None) or getattr(status, "note", None)
    if msg:
        parts.append(str(msg))
    if extra:
        parts.append(extra)
    return " · ".join(parts) or "OK"


def newest_age_minutes(df: pd.DataFrame, *columns: str) -> float | None:
    if df is None or df.empty:
        return None
    for col in columns:
        if col in df.columns:
            vals = pd.to_datetime(df[col], utc=True, errors="coerce").dropna()
            if not vals.empty:
                latest_past = vals[vals <= pd.Timestamp.now(tz="UTC")]
                ts = latest_past.max() if not latest_past.empty else vals.min()
                return age_minutes(ts)
    return None

def fmt_age(ts) -> str:
    if ts is None or pd.isna(ts):
        return "—"
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    age = pd.Timestamp.now(tz="UTC") - t.tz_convert("UTC")
    mins = max(0, int(age.total_seconds() // 60))
    if mins < 60:
        return f"{mins} min"
    return f"{mins // 60} h {mins % 60} min"


def age_minutes(ts) -> float | None:
    if ts is None or pd.isna(ts):
        return None
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    return max(0.0, (pd.Timestamp.now(tz="UTC") - t.tz_convert("UTC")).total_seconds() / 60.0)

def is_fresh(ts, max_minutes: float) -> bool:
    a = age_minutes(ts)
    return a is not None and a <= max_minutes

def _snapshot_updated_at(payload: dict):
    if not isinstance(payload, dict):
        return None
    return pd.to_datetime(payload.get("updated_at"), utc=True, errors="coerce")


def _snapshot_is_fresh(payload: dict, max_minutes: float) -> bool:
    ts = _snapshot_updated_at(payload)
    return pd.notna(ts) and is_fresh(ts, max_minutes)


def render_health_badge(health: SourceHealth) -> None:
    age_txt = dq_human_age(health.age_minutes)
    connection = "otseühendus" if health.direct else ("snapshot" if health.state == "SNAPSHOT" else "peegel/mirror")
    detail = f"{health.state} · {connection} · {age_txt}"
    if health.detail:
        detail += f" · {health.detail}"
    source_badge(
        health.name,
        detail=detail,
        level=("ok" if health.state == "LIVE" else "warning" if health.state in ("SNAPSHOT","MIRROR","STALE") else "error"),
    )



@st.cache_data(ttl=60)
def load_short_prices():
    now = datetime.now(timezone.utc)
    return fetch_elering_prices(now - timedelta(days=1), now + timedelta(days=2))


def published_day_ahead_prices(prices: pd.DataFrame, now_local: datetime) -> pd.DataFrame:
    """Only show tomorrow after publication and a complete day for every region."""
    if prices.empty:
        return prices
    tomorrow = now_local.date() + timedelta(days=1)
    if now_local.hour < 14:
        return prices[prices["time_local"].dt.date != tomorrow].copy()

    start = pd.Timestamp(datetime.combine(tomorrow, datetime.min.time()), tz=TALLINN)
    end = start + pd.DateOffset(days=1)
    for region in REGIONS:
        times = prices.loc[
            (prices["region"] == region) & (prices["time_local"].dt.date == tomorrow),
            "time_utc",
        ].dropna().drop_duplicates().sort_values()
        if len(times) < 2:
            break
        step = times.diff().dropna().median()
        # Nord Pool may publish 15-minute or hourly MTUs. Require a full,
        # gap-free day, including the 23/25-hour daylight-saving days.
        if step not in (pd.Timedelta(minutes=15), pd.Timedelta(hours=1)):
            break
        if times.iloc[0] != start.tz_convert("UTC") or times.iloc[-1] + step != end.tz_convert("UTC"):
            break
        if not times.diff().dropna().eq(step).all():
            break
    else:
        return prices
    return prices[prices["time_local"].dt.date != tomorrow].copy()


def brent_spot_is_recent(observed, today) -> bool:
    """Allow the latest two business days for a daily EIA spot observation."""
    date = pd.to_datetime(observed, errors="coerce")
    if pd.isna(date):
        return False
    days = pd.bdate_range(date.normalize(), pd.Timestamp(today))
    return date.date() <= today and len(days) <= 3



@st.cache_data(persist="disk", max_entries=12)
def load_electricity_price_history(days: int, as_of_day: str):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(days))
    chunks = []
    cur = start
    step = timedelta(days=31)
    while cur < end:
        nxt = min(cur + step, end)
        chunks.append((cur, nxt))
        cur = nxt

    frames, errors = [], []
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(chunks)))) as pool:
        futures = [pool.submit(fetch_elering_prices, x, y) for x, y in chunks]
        for fut in futures:
            try:
                df, stx = fut.result()
                if not df.empty:
                    frames.append(df)
                if not stx.ok and stx.error:
                    errors.append(stx.error)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

    if not frames:
        return pd.DataFrame(), errors

    df = pd.concat(frames, ignore_index=True).drop_duplicates(["region", "timestamp"])
    return df.sort_values(["region", "time_utc"]), errors


@st.cache_data(persist="disk", max_entries=4)
def load_gas_ngp_history(as_of_day: str):
    areas = ["TTF", "LVA-EST", "FIN", "LTU"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(zip(areas, pool.map(fetch_eex_ngp_history, areas)))


def _price_interval_hours(group: pd.DataFrame) -> pd.Series:
    g = group.sort_values("time_utc")
    hours = (g["time_utc"].shift(-1) - g["time_utc"]).dt.total_seconds() / 3600.0
    hours = hours.where((hours >= 0.20) & (hours <= 1.10))
    fallback = hours.dropna().tail(16).median()
    if pd.isna(fallback):
        fallback = 1.0
    return hours.fillna(float(fallback))


def electricity_monthly_summary(df: pd.DataFrame, months: int = 12) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    x = df.copy()
    x["time_local"] = pd.to_datetime(x["time_local"], errors="coerce")
    x = x.dropna(subset=["time_local", "price", "region"])
    cutoff = pd.Timestamp.now(tz=TALLINN) - pd.DateOffset(months=months)
    x = x[x["time_local"] >= cutoff]

    rows = []
    for region, g in x.groupby("region"):
        g = g.sort_values("time_utc").copy()
        g["duration_h"] = _price_interval_hours(g)
        g["month"] = g["time_local"].dt.to_period("M").astype(str)
        for month, m in g.groupby("month"):
            denom = m["duration_h"].sum()
            mean = (m["price"] * m["duration_h"]).sum() / denom if denom > 0 else m["price"].mean()
            min_row = m.loc[m["price"].idxmin()]
            max_row = m.loc[m["price"].idxmax()]
            rows.append({
                "Kuu": month,
                "Piirkond": region,
                "Keskmine €/MWh": float(mean),
                "Min €/MWh": float(min_row["price"]),
                "Min aeg": min_row["time_local"],
                "Max €/MWh": float(max_row["price"]),
                "Max aeg": max_row["time_local"],
            })

    return pd.DataFrame(rows).sort_values(["Kuu", "Piirkond"], ascending=[False, True]) if rows else pd.DataFrame()


def gas_monthly_summary(history_by_area: dict, months: int = 12) -> pd.DataFrame:
    rows = []
    cutoff = pd.Timestamp.now().normalize() - pd.DateOffset(months=months)

    for area, payload in history_by_area.items():
        df, status = payload
        if df.empty:
            continue
        x = df.copy()
        x["date"] = pd.to_datetime(x["date"], errors="coerce")
        x = x.dropna(subset=["date", "price_eur_mwh"])
        x = x[x["date"] >= cutoff]
        x["month"] = x["date"].dt.to_period("M").astype(str)

        for month, m in x.groupby("month"):
            min_row = m.loc[m["price_eur_mwh"].idxmin()]
            max_row = m.loc[m["price_eur_mwh"].idxmax()]
            rows.append({
                "Kuu": month,
                "Piirkond": area,
                "Keskmine €/MWh": float(m["price_eur_mwh"].mean()),
                "Min €/MWh": float(min_row["price_eur_mwh"]),
                "Min kuupäev": min_row["date"].date(),
                "Max €/MWh": float(max_row["price_eur_mwh"]),
                "Max kuupäev": max_row["date"].date(),
            })

    return pd.DataFrame(rows).sort_values(["Kuu", "Piirkond"], ascending=[False, True]) if rows else pd.DataFrame()


def _history_days(label: str) -> int:
    return {"1 nädal": 7, "1 kuu": 31, "1 aasta": 366, "5 aastat": 1826}[label]



def _system_history_days(label: str) -> int:
    return {
        "48 tundi": 2,
        "1 nädal": 7,
        "1 kuu": 31,
        "1 aasta": 366,
        "5 aastat": 1826,
    }[label]


def _chunk_ranges(start: datetime, end: datetime, chunk_days: int):
    cur = start
    step = timedelta(days=chunk_days)
    while cur < end:
        nxt = min(cur + step, end)
        yield cur, nxt
        cur = nxt


@st.cache_data(ttl=21600)
def load_elering_system_history(days: int):
    """Historical EE production/load from Elering, fetched in bounded chunks.

    No interpolation or synthetic values. Failed chunks are reported.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=int(days))
    # Longer Elering requests are split to avoid oversized CSV responses.
    chunk_days = 31 if days > 31 else max(2, days)
    frames, errors = [], []

    ranges = list(_chunk_ranges(start, end, chunk_days))
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(ranges)))) as pool:
        futs = [pool.submit(fetch_elering_system, x, y) for x, y in ranges]
        for fut in futs:
            try:
                df, stx = fut.result()
                if not df.empty:
                    frames.append(df)
                if not stx.ok and stx.error:
                    errors.append(stx.error)
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

    if not frames:
        return pd.DataFrame(), errors

    df = pd.concat(frames, ignore_index=True)
    if "time_utc" in df.columns:
        df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True, errors="coerce")
        df = df.dropna(subset=["time_utc"]).drop_duplicates("time_utc").sort_values("time_utc")
        df["time_local"] = df["time_utc"].dt.tz_convert(TALLINN)
    return df, errors


@st.cache_data(ttl=21600)
def load_entsoe_system_history(token: str, region: str, days: int):
    """Historical actual generation/load from ENTSO-E for one Baltic bidding zone.

    Requests are chunked. For long periods, raw data are later aggregated in the UI;
    no values are fabricated.
    """
    if not token:
        return pd.DataFrame(), pd.DataFrame(), ["ENTSOE_API_KEY puudub"]

    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(days=int(days))

    # ENTSO-E can reject oversized actual-data windows. 31-day chunks are conservative
    # and still allow 5-year on-demand retrieval without changing source semantics.
    chunk_days = 31
    ranges = list(_chunk_ranges(start, end, chunk_days))
    gen_frames, load_frames, errors = [], [], []

    def _one(x, y):
        gdf, gst = fetch_entsoe_generation_by_type(token, x, y, region)
        ldf, lst = fetch_entsoe_actual_load(token, x, y, region)
        return gdf, gst, ldf, lst

    with ThreadPoolExecutor(max_workers=min(6, max(1, len(ranges)))) as pool:
        futs = [pool.submit(_one, x, y) for x, y in ranges]
        for fut in futs:
            try:
                gdf, gst, ldf, lst = fut.result()
                if not gdf.empty:
                    gen_frames.append(gdf)
                if not ldf.empty:
                    load_frames.append(ldf)
                if not gst.ok and gst.error:
                    errors.append(f"generation: {gst.error}")
                if not lst.ok and lst.error:
                    errors.append(f"load: {lst.error}")
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

    g = pd.concat(gen_frames, ignore_index=True) if gen_frames else pd.DataFrame()
    l = pd.concat(load_frames, ignore_index=True) if load_frames else pd.DataFrame()

    if not g.empty:
        g["time_utc"] = pd.to_datetime(g["time_utc"], utc=True, errors="coerce")
        g = g.dropna(subset=["time_utc", "generation_mw"])
        g = g.drop_duplicates(["time_utc", "technology"]).sort_values("time_utc")
        g["time_local"] = g["time_utc"].dt.tz_convert(TALLINN)

    if not l.empty:
        l["time_utc"] = pd.to_datetime(l["time_utc"], utc=True, errors="coerce")
        l = l.dropna(subset=["time_utc", "load_mw"]).drop_duplicates("time_utc").sort_values("time_utc")
        l["time_local"] = l["time_utc"].dt.tz_convert(TALLINN)

    return g, l, errors


def _build_system_history_chart(region: str, elering_df: pd.DataFrame, gen_df: pd.DataFrame, load_df: pd.DataFrame):
    """Normalize actual system history into Tootmine/Tarbimine/Taastuvtootmine lines."""
    parts = []

    # Production: EE prefers Elering actual; if absent, use ENTSO-E actual generation total.
    _ee_prod_added = False
    if region == "EE" and elering_df is not None and not elering_df.empty and "production_mw" in elering_df.columns:
        p = elering_df[["time_utc", "time_local", "production_mw"]].copy()
        p["MW"] = pd.to_numeric(p["production_mw"], errors="coerce")
        p = p.dropna(subset=["MW"])
        if not p.empty:
            p["series"] = "Tootmine"
            parts.append(p[["time_utc", "time_local", "MW", "series"]])
            _ee_prod_added = True
    if (region != "EE" or not _ee_prod_added) and gen_df is not None and not gen_df.empty:
        p = (
            gen_df.groupby(["time_utc", "time_local"], as_index=False)["generation_mw"]
            .sum()
            .rename(columns={"generation_mw": "MW"})
        )
        p["series"] = "Tootmine"
        parts.append(p[["time_utc", "time_local", "MW", "series"]])

    # Consumption: EE prefers Elering actual; otherwise ENTSO-E A65.
    _ee_cons_added = False
    if region == "EE" and elering_df is not None and not elering_df.empty and "consumption_mw" in elering_df.columns:
        c = elering_df[["time_utc", "time_local", "consumption_mw"]].copy()
        c["MW"] = pd.to_numeric(c["consumption_mw"], errors="coerce")
        c = c.dropna(subset=["MW"])
        if not c.empty:
            c["series"] = "Tarbimine"
            parts.append(c[["time_utc", "time_local", "MW", "series"]])
            _ee_cons_added = True
    if (region != "EE" or not _ee_cons_added) and load_df is not None and not load_df.empty:
        c = load_df[["time_utc", "time_local", "load_mw"]].copy()
        c["MW"] = pd.to_numeric(c["load_mw"], errors="coerce")
        c["series"] = "Tarbimine"
        parts.append(c[["time_utc", "time_local", "MW", "series"]].dropna(subset=["MW"]))

    # Renewable actual generation from ENTSO-E A75 only.
    if gen_df is not None and not gen_df.empty:
        renewable_names = {
            "Biomass", "Geothermal", "Hydro Run-of-river and poundage",
            "Hydro Water Reservoir", "Marine", "Other renewable",
            "Solar", "Wind Offshore", "Wind Onshore",
        }
        rg = gen_df[gen_df["technology"].isin(renewable_names)]
        if not rg.empty:
            rr = (
                rg.groupby(["time_utc", "time_local"], as_index=False)["generation_mw"]
                .sum()
                .rename(columns={"generation_mw": "MW"})
            )
            rr["series"] = "Taastuvtootmine"
            parts.append(rr[["time_utc", "time_local", "MW", "series"]])

    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True).dropna(subset=["time_utc", "MW"]).sort_values("time_utc")


def _aggregate_system_history(chart: pd.DataFrame, period_label: str) -> pd.DataFrame:
    if chart.empty:
        return chart
    x = chart.copy()
    # Keep operational detail for short periods. Aggregate long periods to daily
    # actual averages to make the graph readable and computationally light.
    if period_label in ("1 aasta", "5 aastat"):
        x["date"] = pd.to_datetime(x["time_local"]).dt.date
        x = (
            x.groupby(["date", "series"], as_index=False)["MW"]
            .mean()
            .rename(columns={"date": "time_local"})
        )
        x["time_local"] = pd.to_datetime(x["time_local"])
    return x


@st.cache_data(ttl=60)
def load_system():
    now = datetime.now(timezone.utc)
    return fetch_elering_system(now - timedelta(hours=48), now + timedelta(hours=2))

@st.cache_data(ttl=120)
def load_baltic_system_snapshot():
    path = Path(__file__).resolve().parent / "data" / "baltic_system_snapshot.json"
    if not path.exists():
        return {}, {"ok": False, "error": "snapshot puudub"}
    try:
        import json
        return json.loads(path.read_text(encoding="utf-8")), {"ok": True, "error": None}
    except Exception as exc:
        return {}, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

def pct_delta(current, previous):
    try:
        c, p = float(current), float(previous)
        if pd.isna(c) or pd.isna(p) or p == 0: return None
        return (c - p) / abs(p) * 100.0
    except Exception:
        return None

def delta_text(current, previous):
    d = pct_delta(current, previous)
    return f"{d:+.1f}% vs 24h tagasi" if d is not None else None

def snapshot_region(data, region):
    return (data.get("regions", {}).get(region, {}) if isinstance(data, dict) else {}) or {}

def snapshot_history_df(data, region):
    rows = snapshot_region(data, region).get("history", [])
    if not rows: return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["time_utc"] = pd.to_datetime(df["time_utc"], utc=True, errors="coerce")
    df["time_local"] = df["time_utc"].dt.tz_convert(TALLINN)
    return df.dropna(subset=["time_utc"]).sort_values("time_utc")


@st.cache_data(ttl=300)
def load_storage(key: str):
    return fetch_gas_storage(key)


@st.cache_data(ttl=300)
def load_entsoe_generation(key: str, region: str = "EE"):
    now = datetime.now(timezone.utc)
    return fetch_entsoe_generation_by_type(key, now - timedelta(hours=36), now + timedelta(hours=1), region)


@st.cache_data(ttl=300)
def load_entsoe_flows(key: str):
    now = datetime.now(timezone.utc)
    return fetch_entsoe_estonia_flows(key, now - timedelta(hours=36), now + timedelta(hours=1))

@st.cache_data(ttl=300)
def load_entsoe_baltic_flows(key: str):
    now = datetime.now(timezone.utc)
    return fetch_entsoe_baltic_flows(key, now - timedelta(hours=36), now + timedelta(hours=1))


@st.cache_data(ttl=300)
def load_entsoe_load(key: str, region: str = "EE"):
    now = datetime.now(timezone.utc)
    return fetch_entsoe_actual_load(key, now - timedelta(hours=36), now + timedelta(hours=1), region)


@st.cache_data(ttl=300)
def load_ngp_current(area: str):
    return fetch_eex_ngp_current(area)


@st.cache_data(ttl=900)
def load_ttf():
    return fetch_eex_ttf_ngp()


@st.cache_data(ttl=3600)
def load_brent():
    return fetch_eia_brent()


@st.cache_data(ttl=3600)
def load_eua():
    return fetch_eex_eua_auction()


@st.cache_data(ttl=300)
def load_eu_day_ahead_snapshot():
    path = Path(__file__).resolve().parent / "data" / "eu_day_ahead_prices.json"
    if not path.exists():
        return {}, {"ok": False, "error": "data/eu_day_ahead_prices.json puudub"}
    try:
        import json
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload, {"ok": True, "error": None}
    except Exception as exc:
        return {}, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}



EU_PRICE_LABEL_POINTS = {
    "AUT": (47.5, 14.3, "AT"),
    "BEL": (50.8, 4.7, "BE"),
    "BGR": (42.7, 25.5, "BG"),
    "HRV": (45.2, 16.2, "HR"),
    "CZE": (49.8, 15.5, "CZ"),
    "DEU": (51.1, 10.4, "DE"),
    "LUX": (49.6, 6.1, "LU"),
    "DNK": (56.1, 9.4, "DK"),
    "EST": (58.6, 25.2, "EE"),
    "ESP": (40.3, -3.7, "ES"),
    "FIN": (63.8, 26.0, "FI"),
    "FRA": (46.5, 2.4, "FR"),
    "GRC": (39.1, 22.0, "GR"),
    "HUN": (47.1, 19.3, "HU"),
    "LVA": (56.9, 24.6, "LV"),
    "LTU": (55.2, 23.9, "LT"),
    "NLD": (52.2, 5.5, "NL"),
    "POL": (52.0, 19.2, "PL"),
    "PRT": (39.7, -8.0, "PT"),
    "ROU": (45.8, 24.9, "RO"),
    "SVK": (48.7, 19.5, "SK"),
    "SVN": (46.1, 14.9, "SI"),
    "SWE": (62.0, 16.0, "SE"),
}

def _eu_map_df(payload: dict, date_str: str) -> pd.DataFrame:
    if not _snapshot_is_fresh(payload, 180):
        return pd.DataFrame()
    rows = []
    day = (payload.get("days", {}) or {}).get(date_str, {}) if isinstance(payload, dict) else {}
    for row in day.get("countries", []) or []:
        if row.get("price_eur_mwh") is None:
            continue
        rows.append(row)
    return pd.DataFrame(rows)


def _render_eu_price_map(df: pd.DataFrame, title: str):
    if df.empty:
        st.info(f"{title}: andmed pole snapshot'is saadaval.")
        return

    x = df.copy()
    x["price_eur_mwh"] = pd.to_numeric(x["price_eur_mwh"], errors="coerce")
    x = x.dropna(subset=["iso3", "price_eur_mwh"])
    if x.empty:
        st.info(f"{title}: valideeritud hinnad puuduvad.")
        return

    # Base choropleth keeps the colour comparison.
    fig = px.choropleth(
        x,
        locations="iso3",
        color="price_eur_mwh",
        hover_name="country",
        hover_data={
            "iso3": False,
            "price_eur_mwh": ":.2f",
            "zones": True,
        },
        labels={
            "price_eur_mwh": "€/MWh",
            "zones": "Hinnapiirkonnad",
        },
        scope="europe",
        title=title,
        color_continuous_scale="YlOrRd",
    )

    # Numeric labels are a separate Scattergeo layer because choropleth itself
    # only shows numeric values in hover, not persistently on the map.
    label_rows = []
    for _, row in x.iterrows():
        point = EU_PRICE_LABEL_POINTS.get(str(row["iso3"]))
        if point is None:
            continue
        lat, lon, code = point
        label_rows.append({
            "lat": lat,
            "lon": lon,
            "code": code,
            "price": float(row["price_eur_mwh"]),
            "country": row.get("country", code),
        })

    if label_rows:
        labels_df = pd.DataFrame(label_rows)
        fig.add_trace(
            go.Scattergeo(
                lon=labels_df["lon"],
                lat=labels_df["lat"],
                mode="markers+text",
                marker={
                    "size": 31,
                    "color": "rgba(255,255,255,0.90)",
                    "line": {"width": 1, "color": "rgba(30,30,30,0.70)"},
                },
                text=[
                    f"<b>{code}</b><br>{price:.1f}"
                    for code, price in zip(labels_df["code"], labels_df["price"])
                ],
                textposition="middle center",
                textfont={"size": 9, "color": "#111111"},
                customdata=labels_df[["country", "price"]],
                hovertemplate=(
                    "<b>%{customdata[0]}</b><br>"
                    "%{customdata[1]:.2f} €/MWh"
                    "<extra></extra>"
                ),
                showlegend=False,
            )
        )

    fig.update_geos(
        showcountries=True,
        countrycolor="rgba(255,255,255,0.75)",
        showcoastlines=True,
        coastlinecolor="rgba(90,90,90,0.45)",
        showocean=True,
        oceancolor="rgba(230,238,247,0.65)",
        fitbounds="locations",
        visible=False,
    )
    fig.update_layout(
        margin={"r": 0, "t": 48, "l": 0, "b": 0},
        coloraxis_colorbar_title="€/MWh",
        height=620,
    )
    st.plotly_chart(fig, use_container_width=True)
    source_links(("entsoe_api", "A44 day-ahead"), ("dayahead_snapshot", "GitHub Actions snapshot"))
    st.caption(
        "Kaardil olev number = päeva-ette keskmine hind €/MWh. "
        "Mitme hinnapiirkonnaga riigi puhul on kuvatud snapshot'is arvutatud "
        "piirkondade aritmeetiline keskmine."
    )


def _shade_month_groups(df: pd.DataFrame):
    """Same subtle background for all EE/LV/LT/FI rows belonging to one month."""
    months = list(dict.fromkeys(df["Kuu"].astype(str).tolist()))
    parity = {m: i % 2 for i, m in enumerate(months)}
    def row_style(row):
        bg = "background-color: rgba(120, 120, 120, 0.08)" if parity.get(str(row["Kuu"]), 0) else "background-color: rgba(120, 120, 120, 0.02)"
        return [bg] * len(row)
    return df.style.apply(row_style, axis=1).format({
        "Keskmine €/MWh": "{:.2f}", "Min €/MWh": "{:.2f}", "Max €/MWh": "{:.2f}",
        "Min aeg": lambda x: x.strftime("%d.%m.%Y %H:%M") if pd.notna(x) else "",
        "Max aeg": lambda x: x.strftime("%d.%m.%Y %H:%M") if pd.notna(x) else "",
    })


@st.cache_data(ttl=300)
def load_futures_snapshot():
    path = Path(__file__).resolve().parent / "data" / "futures.json"
    if not path.exists():
        return {}, {"ok": False, "error": "data/futures.json puudub"}
    try:
        import json
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload, {"ok": True, "error": None}
    except Exception as exc:
        return {}, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _future_key_rows(payload: dict, section: str):
    rows, _state = _future_rows_market_aware(payload, section, "key")
    return rows


def _future_curve_rows(payload: dict, section: str):
    rows, _state = _future_rows_market_aware(payload, section, "curve")
    return rows





def _futures_diag(payload: dict, section: str) -> tuple[str, list[str]]:
    if not isinstance(payload, dict) or not payload:
        return "snapshot puudub", []
    updated = pd.to_datetime(payload.get("updated_at"), utc=True, errors="coerce")
    updated_txt = "uuendamata"
    if pd.notna(updated):
        try:
            updated_txt = updated.tz_convert(TALLINN).strftime("%d.%m.%Y %H:%M")
        except Exception:
            updated_txt = str(updated)
    errors = payload.get(section, {}).get("errors", []) if isinstance(payload.get(section, {}), dict) else []
    return updated_txt, list(errors or [])



SOURCE_LINKS = {
    "elering_prices": ("Elering NPS / Nord Pool day-ahead API", "https://dashboard.elering.ee/api/nps/price"),
    "elering_system": ("Elering electricity system API", "https://dashboard.elering.ee/api/system/with-plan"),
    "entsoe_tp": ("ENTSO-E Transparency Platform", "https://transparency.entsoe.eu/"),
    "entsoe_api": ("ENTSO-E Web API", "https://web-api.tp.entsoe.eu/api"),
    "eex_ngp": ("EEX Neutral Gas Price", "https://gasandregistry.eex.com/Gas/NGP/"),
    "eex_eua": ("EEX EU ETS primary market auction", "https://www.eex.com/en/market-data/environmentals/spot"),
    "eia_brent": ("U.S. EIA Europe Brent Spot Price FOB", "https://www.eia.gov/dnav/pet/hist/RBRTED.htm"),
    "gie_agsi": ("GIE AGSI+ gas storage", "https://agsi.gie.eu/"),
    "ice_ttf": ("ICE Endex Dutch TTF Natural Gas Futures", "https://www.ice.com/products/27996665/Dutch-TTF-Natural-Gas-Futures/data"),
    "ice_brent": ("ICE Futures Europe Brent Crude Futures", "https://www.ice.com/products/219/Brent-Crude-Futures/data"),
    "euronext_power": ("Euronext Nord Pool Power Futures", "https://live.euronext.com/en/products/commodities/power-derivatives"),
    "baltic_snapshot": ("BalticPulse Baltic system snapshot", "data/baltic_system_snapshot.json"),
    "futures_snapshot": ("BalticPulse futures snapshot", "data/futures.json"),
    "dayahead_snapshot": ("BalticPulse EU day-ahead snapshot", "data/eu_day_ahead_prices.json"),
}

def source_link(key: str, note: str | None = None):
    label, url = SOURCE_LINKS[key]
    suffix = f" — {note}" if note else ""
    if url.startswith("data/"):
        st.caption(f"Allikas: **{label}** (`{url}`){suffix}")
    else:
        st.markdown(f"Allikas: [{label}]({url}){suffix}")


def source_links(*items):
    parts = []
    for item in items:
        key = item[0]
        note = item[1] if len(item) > 1 else None
        label, url = SOURCE_LINKS[key]
        if url.startswith("data/"):
            part = f"**{label}** (`{url}`)"
        else:
            part = f"[{label}]({url})"
        if note:
            part += f" — {note}"
        parts.append(part)
    st.markdown("Allikad: " + " · ".join(parts))



def _is_weekend_market_closed(tz_name: str, now_utc=None) -> bool:
    now_utc = now_utc or datetime.now(timezone.utc)
    try:
        local_now = now_utc.astimezone(ZoneInfo(tz_name))
    except Exception:
        local_now = now_utc
    return local_now.weekday() >= 5


def _futures_market_state(section: str, payload: dict, now_utc=None) -> dict:
    meta = (payload or {}).get(section, {}) if isinstance(payload, dict) else {}
    tz_name = meta.get("timezone") or {
        "power": "Europe/Paris",
        "gas": "Europe/Amsterdam",
        "brent": "Europe/London",
    }.get(section, "UTC")

    observed = pd.to_datetime(meta.get("observed_at"), utc=True, errors="coerce")
    if pd.isna(observed):
        observed = pd.to_datetime((payload or {}).get("updated_at"), utc=True, errors="coerce")

    return {
        "closed": _is_weekend_market_closed(tz_name, now_utc=now_utc),
        "timezone": tz_name,
        "observed_at": observed,
    }


def _future_rows_market_aware(payload: dict, section: str, kind: str):
    state = _futures_market_state(section, payload)
    rows = ((payload or {}).get(section, {}) or {}).get(kind, []) if isinstance(payload, dict) else []
    if not rows:
        return pd.DataFrame(), state

    # Closed market: keep last official observed values visible even beyond normal freshness limit.
    if state["closed"]:
        return pd.DataFrame(rows), state

    # Open weekday: normal freshness rule remains strict.
    if not _snapshot_is_fresh(payload, 120):
        return pd.DataFrame(), state

    return pd.DataFrame(rows), state


def _render_futures_market_state(state: dict):
    observed = state.get("observed_at")
    observed_txt = "aeg teadmata"
    if pd.notna(observed):
        observed_txt = observed.tz_convert(TALLINN).strftime("%d.%m.%Y %H:%M")

    if state.get("closed"):
        st.info(
            f"🔒 TURG SULETUD — kuvatakse viimane ametlik turuseis seisuga **{observed_txt}**."
        )
    else:
        st.caption(f"Viimane futuuride turuseis: {observed_txt}.")


@st.fragment(run_every=60)
def render_dashboard():
    futures_snapshot, futures_snapshot_status = load_futures_snapshot()
    eu_da_snapshot, eu_da_status = load_eu_day_ahead_snapshot()

    # Header
    c1, c2 = st.columns([4, 1])
    with c1:
        st.title("⚡ BalticPulse")
        st.caption(f"BalticPulse • Build {APP_BUILD_VERSION}")
        st.caption("Balti ja Põhjamaade energiaturu olukorrapilt — elekter, võrk, gaas ja põhifundamentaalid.")
    with c2:
        st.write("")
        if st.button("🔄 Värskenda", use_container_width=True):
            st.rerun()

    now_local = datetime.now(TALLINN)
    today = now_local.date()
    tomorrow = today + timedelta(days=1)
    st.caption(f"Vaate aeg: **{now_local:%d.%m.%Y %H:%M:%S}** Europe/Tallinn · automaatne värskendus iga minut; hoidlaandmete korduspäring kuni iga 5 minuti järel")

    with st.spinner("Laadin operatiivandmeid..."):
        entsoe_key = secret("ENTSOE_API_KEY")
        agsi_key = secret("GIE_AGSI_API_KEY")
        # Independent sources are fetched concurrently so a slow daily/fundamental source does not
        # hold the operational view hostage. Individual loaders still retain their own cache TTLs.
        with ThreadPoolExecutor(max_workers=12) as pool:
            fut_prices = pool.submit(load_short_prices)
            fut_system = pool.submit(load_system)
            fut_baltic_snapshot = pool.submit(load_baltic_system_snapshot)
            fut_storage = pool.submit(load_storage, agsi_key)
            gen_futs = {r: pool.submit(load_entsoe_generation, entsoe_key, r) for r in BALTICS}
            load_futs = {r: pool.submit(load_entsoe_load, entsoe_key, r) for r in BALTICS}
            fut_flows = pool.submit(load_entsoe_flows, entsoe_key)
            fut_baltic_flows = pool.submit(load_entsoe_baltic_flows, entsoe_key)
            fut_ttf_hist = pool.submit(load_ttf)
            fut_brent = pool.submit(load_brent)
            fut_eua = pool.submit(load_eua)
            ngp_futs = {a: pool.submit(load_ngp_current, a) for a in ["TTF", "LVA-EST", "FIN", "LTU"]}

            prices, price_status = fut_prices.result()
            prices = published_day_ahead_prices(prices, now_local)
            system_df, system_status = fut_system.result()
            baltic_system_snapshot, baltic_snapshot_status = fut_baltic_snapshot.result()
            storage_df, storage_status = fut_storage.result()
            entsoe_generation_results = {r: f.result() for r, f in gen_futs.items()}
            entsoe_load_results = {r: f.result() for r, f in load_futs.items()}
            entsoe_flows, entsoe_flow_statuses = fut_flows.result()
            entsoe_baltic_flows, entsoe_baltic_flow_statuses = fut_baltic_flows.result()
            ttf_df, ttf_status = fut_ttf_hist.result()
            brent_df, brent_status = fut_brent.result()
            eua_df, eua_status = fut_eua.result()
            ngp_current_results = {a: f.result() for a, f in ngp_futs.items()}

    # ---------- NORMALISEERITUD FUNDAMENTAALID ----------
    # Build all headline values immediately after source fetches, before any UI uses them.
    ngp_current: dict[str, float | None] = {area: None for area in ["TTF", "LVA-EST", "FIN", "LTU"]}
    for _area, (_ngp_df, _ngp_status) in ngp_current_results.items():
        if _ngp_df is None or _ngp_df.empty:
            continue
        _ngp_x = _ngp_df.copy()
        if "delivery_date" in _ngp_x.columns:
            _ngp_x["delivery_date"] = pd.to_datetime(_ngp_x["delivery_date"], errors="coerce").dt.date
        if "price_eur_mwh" in _ngp_x.columns:
            _ngp_x["price_eur_mwh"] = pd.to_numeric(_ngp_x["price_eur_mwh"], errors="coerce")
            _ngp_x = _ngp_x.dropna(subset=["price_eur_mwh"])
        if _ngp_x.empty or "price_eur_mwh" not in _ngp_x.columns:
            continue

        # Strict current-day policy: D KPI may only use today's delivery row.
        # D+1/D+2 must never silently replace today's gas price.
        if "delivery_date" in _ngp_x.columns:
            _today = now_local.date()
            _today_rows = _ngp_x[_ngp_x["delivery_date"] == _today]
            if _today_rows.empty:
                continue
            _row = _today_rows.iloc[-1]
            ngp_current[_area] = float(_row["price_eur_mwh"])

    brent_latest = None
    if brent_df is not None and not brent_df.empty and "price_usd_bbl" in brent_df.columns:
        _brent_vals = pd.to_numeric(brent_df["price_usd_bbl"], errors="coerce").dropna()
        if not _brent_vals.empty:
            brent_latest = float(_brent_vals.iloc[-1])

    eua_latest = None
    if eua_df is not None and not eua_df.empty and "price_eur_tco2" in eua_df.columns:
        _eua_vals = pd.to_numeric(eua_df["price_eur_tco2"], errors="coerce").dropna()
        if not _eua_vals.empty:
            eua_latest = float(_eua_vals.iloc[-1])


    # ---------- BALTICPULSE SOURCE-INTEGRITY STANDARD ----------
    # Current/headline values are displayable only when their source state is acceptable.
    source_health: dict[str, SourceHealth] = {}

    _price_obs = None
    if not prices.empty and "time_utc" in prices.columns:
        _pvals = pd.to_datetime(prices["time_utc"], utc=True, errors="coerce").dropna()
        _past = _pvals[_pvals <= pd.Timestamp.now(tz="UTC")]
        _price_obs = _past.max() if not _past.empty else None
    source_health["Elering prices"] = assess_source(
        "Elering hinnad",
        has_data=not prices.empty,
        direct_ok=bool(getattr(price_status, "ok", False)),
        observed_at=_price_obs,
        max_age_minutes=120,
        mode="direct",
        checked_at=getattr(price_status, "fetched_at", None),
        detail=getattr(price_status, "error", None) or "",
    )

    _sys_obs = None
    if not system_df.empty and "time_utc" in system_df.columns:
        _svals = pd.to_datetime(system_df["time_utc"], utc=True, errors="coerce").dropna()
        if not _svals.empty:
            _sys_obs = _svals.max()
    source_health["Elering system"] = assess_source(
        "Elering süsteem",
        has_data=not system_df.empty,
        direct_ok=bool(getattr(system_status, "ok", False)),
        observed_at=_sys_obs,
        max_age_minutes=20,
        mode="direct",
        checked_at=getattr(system_status, "fetched_at", None),
        detail=getattr(system_status, "error", None) or "",
    )

    # Strictly mask stale/failed primary current values.
    if not source_health["Elering system"].displayable:
        prod = None
        cons = None
        prod_time = None
        cons_time = None

    # Current NGP is direct-only and must contain today's delivery date.
    for _area in ["TTF", "LVA-EST", "FIN", "LTU"]:
        _df_ngp, _st_ngp = ngp_current_results.get(_area, (pd.DataFrame(), None))
        _has_today = ngp_current.get(_area) is not None
        source_health[f"EEX NGP {_area}"] = assess_source(
            f"EEX NGP {_area}",
            has_data=_has_today,
            direct_ok=bool(getattr(_st_ngp, "ok", False)),
            observed_at=pd.Timestamp.now(tz="UTC") if _has_today else None,
            max_age_minutes=60,
            mode="direct",
            checked_at=getattr(_st_ngp, "fetched_at", None),
            detail=(getattr(_st_ngp, "error", None) or ("tänase gas day rida puudub" if not _has_today else "")),
        )
        ngp_current[_area] = mask_source(ngp_current.get(_area), source_health[f"EEX NGP {_area}"])

    _brent_obs = brent_df["date"].max() if not brent_df.empty and "date" in brent_df.columns else None
    _brent_recent = brent_spot_is_recent(_brent_obs, today)
    source_health["Brent"] = assess_source(
        "U.S. EIA Brent spot",
        has_data=brent_latest is not None and _brent_recent,
        direct_ok=bool(getattr(brent_status, "ok", False)),
        observed_at=_brent_obs,
        max_age_minutes=4 * 24 * 60,
        mode="direct",
        checked_at=getattr(brent_status, "fetched_at", None),
        detail=getattr(brent_status, "error", None) or ("viimane EIA spot-hind on aegunud" if not _brent_recent else ""),
    )
    brent_latest = mask_source(brent_latest, source_health["Brent"])

    _eua_obs = eua_df["date"].max() if not eua_df.empty and "date" in eua_df.columns else None
    source_health["EUA"] = assess_source(
        "EEX EUA oksjon",
        has_data=eua_latest is not None,
        direct_ok=bool(getattr(eua_status, "ok", False)),
        observed_at=_eua_obs,
        max_age_minutes=10 * 24 * 60,
        mode="direct",
        checked_at=getattr(eua_status, "fetched_at", None),
        detail=getattr(eua_status, "error", None) or "",
    )
    eua_latest = mask_source(eua_latest, source_health["EUA"])

    # Snapshot-based sources are explicitly NOT direct connections.
    _fut_updated = _snapshot_updated_at(futures_snapshot)
    source_health["Futures snapshot"] = assess_source(
        "Futuurid (Euronext/ICE)",
        has_data=bool(futures_snapshot and any((futures_snapshot.get(k, {}) or {}).get("curve") for k in ("power","gas","brent"))),
        direct_ok=False,
        observed_at=_fut_updated,
        max_age_minutes=120,
        mode="snapshot",
        detail="GitHub Actions snapshot; Streamlitil puudub otseühendus börsiga",
    )

    _map_updated = _snapshot_updated_at(eu_da_snapshot)
    source_health["Day-ahead map snapshot"] = assess_source(
        "ENTSO-E EL hinnakaart",
        has_data=bool((eu_da_snapshot or {}).get("days")),
        direct_ok=False,
        observed_at=_map_updated,
        max_age_minutes=180,
        mode="snapshot",
        detail="GitHub Actions A44 snapshot; Streamlitil puudub otseühendus ENTSO-E kaardipäringuga",
    )

    # ---------- NORMALISEERITUD HETKESEIS ----------
    price_daily = pd.DataFrame()
    current_prices: dict[str, float | None] = {r: None for r in REGIONS}
    if not prices.empty:
        p = prices.copy()
        p["local_date"] = p["time_local"].dt.date
        price_daily = p.groupby(["local_date", "region"], as_index=False)["price"].agg(mean="mean", min="min", max="max")
        now_ts = pd.Timestamp(now_local)
        for region in REGIONS:
            rp = p[p["region"] == region].sort_values("time_local").copy()
            if rp.empty:
                continue
            diffs = rp["time_local"].diff().dropna().dt.total_seconds() / 60
            step_min = float(diffs[diffs > 0].median()) if not diffs[diffs > 0].empty else 60.0
            active = rp[(rp["time_local"] <= now_ts) & (now_ts < rp["time_local"] + pd.to_timedelta(step_min, unit="m"))]
            if not active.empty:
                current_prices[region] = float(active.iloc[-1]["price"])

    # Apply source-integrity gate to live spot KPIs.
    if not source_health["Elering prices"].displayable:
        current_prices = {r: None for r in REGIONS}

    last_sys = pd.DataFrame()
    if not system_df.empty:
        val_cols = [c for c in ["production_mw", "consumption_mw"] if c in system_df.columns]
        if val_cols:
            last_sys = system_df.dropna(subset=val_cols, how="all").tail(1)
    elering_sys_time = last_sys["time_utc"].iloc[0] if not last_sys.empty and "time_utc" in last_sys else None
    # Source-integrity standard: stale Elering current values are not shown as current KPIs.
    prod = last_sys["production_mw"].iloc[0] if not last_sys.empty and "production_mw" in last_sys and pd.notna(last_sys["production_mw"].iloc[0]) else None
    cons = last_sys["consumption_mw"].iloc[0] if not last_sys.empty and "consumption_mw" in last_sys and pd.notna(last_sys["consumption_mw"].iloc[0]) else None
    prod_source = "Elering" if prod is not None else None
    cons_source = "Elering" if cons is not None else None
    prod_time = elering_sys_time if prod is not None else None
    cons_time = elering_sys_time if cons is not None else None
    elering_system_stale = (age_minutes(elering_sys_time) or 0) > 15 if elering_sys_time is not None else False

    # Elering-only policy for the primary production and consumption KPIs.
    # ENTSO-E remains a separate comparison/source-detail view and never fills these KPIs.

    # Primary system timestamp comes only from Elering observations.
    _sys_times = [pd.Timestamp(t) for t in [prod_time, cons_time] if t is not None and not pd.isna(t)]
    sys_time = min(_sys_times) if _sys_times else None

    # Latest ENTSO-E cross-border net flows from Estonia's perspective.
    # Positive = net export from Estonia, negative = net import into Estonia.
    latest_border_flows: dict[str, float | None] = {"EE–FI": None, "EE–LV": None}
    latest_border_flow_time: dict[str, pd.Timestamp | None] = {"EE–FI": None, "EE–LV": None}
    if not entsoe_flows.empty:
        for border in latest_border_flows:
            b = entsoe_flows[entsoe_flows["border"] == border].copy()
            if not b.empty:
                # Sum directional observations only within the same latest timestamp.
                latest_t = b["time_utc"].max()
                bx = b[b["time_utc"] == latest_t]
                if not bx.empty and is_fresh(latest_t, 120):
                    latest_border_flows[border] = float(pd.to_numeric(bx["signed_mw"], errors="coerce").sum())
                    latest_border_flow_time[border] = pd.Timestamp(latest_t)

    # Baltic system values: direct ENTSO-E first, GitHub snapshot second.
    renewable_names = {"Biomass","Geothermal","Hydro Run-of-river and poundage","Hydro Water Reservoir","Marine","Other renewable","Solar","Wind Offshore","Wind Onshore"}

    def nearest(df, time_col, value_col, target, tolerance="90min"):
        if df is None or df.empty or time_col not in df or value_col not in df: return None
        x = df[[time_col,value_col]].dropna().copy()
        if x.empty: return None
        x[time_col] = pd.to_datetime(x[time_col], utc=True, errors="coerce")
        x = x.dropna().sort_values(time_col)
        if x.empty: return None
        delta = (x[time_col] - target).abs()
        i = delta.idxmin()
        if delta.loc[i] > pd.Timedelta(tolerance): return None
        return float(x.loc[i,value_col])

    def make_snapshot(region):
        gdf, gst = entsoe_generation_results.get(region, (pd.DataFrame(), None))
        ldf, lst = entsoe_load_results.get(region, (pd.DataFrame(), None))
        _snapshot_fresh = _snapshot_is_fresh(baltic_system_snapshot, 60)
        out = {"production_mw":None,"consumption_mw":None,"renewable_mw":None,"renewable_share":None,
               "generation_time":None,"load_time":None,"generation_status":gst,"load_status":lst,
               "previous_24h":{},"source":"ENTSO-E direct"}

        if not gdf.empty:
            gx = gdf.dropna(subset=["generation_mw"]).copy()
            if not gx.empty:
                gt = gx["time_utc"].max()
                latest = gx[gx["time_utc"] == gt]
                total = pd.to_numeric(latest["generation_mw"], errors="coerce").sum(min_count=1)
                ren = pd.to_numeric(latest[latest["technology"].isin(renewable_names)]["generation_mw"], errors="coerce").sum(min_count=1)
                out["generation_time"] = pd.Timestamp(gt)
                out["production_mw"] = float(total) if pd.notna(total) else None
                out["renewable_mw"] = float(ren) if pd.notna(ren) else None
                out["renewable_share"] = float(100*ren/total) if pd.notna(total) and total>0 and pd.notna(ren) else None
                total_ts = gx.groupby("time_utc",as_index=False)["generation_mw"].sum().rename(columns={"generation_mw":"production_mw"})
                ren_ts = gx[gx["technology"].isin(renewable_names)].groupby("time_utc",as_index=False)["generation_mw"].sum().rename(columns={"generation_mw":"renewable_mw"})
                merged = total_ts.merge(ren_ts,on="time_utc",how="left")
                merged["renewable_mw"] = merged["renewable_mw"].fillna(0)
                merged["renewable_share"] = (merged["renewable_mw"]/merged["production_mw"]*100).where(merged["production_mw"]>0)
                target = pd.Timestamp(gt)-pd.Timedelta(hours=24)
                for fld in ["production_mw","renewable_mw","renewable_share"]:
                    out["previous_24h"][fld] = nearest(merged,"time_utc",fld,target)

        if not ldf.empty:
            lx = ldf.dropna(subset=["load_mw"]).sort_values("time_utc")
            if not lx.empty:
                row = lx.iloc[-1]
                out["consumption_mw"] = float(row["load_mw"])
                out["load_time"] = pd.Timestamp(row["time_utc"])
                out["previous_24h"]["consumption_mw"] = nearest(lx,"time_utc","load_mw",pd.Timestamp(row["time_utc"])-pd.Timedelta(hours=24))

        snap = snapshot_region(baltic_system_snapshot, region)
        cur = snap.get("current", {})
        prev = snap.get("previous_24h", {})
        used = False
        for fld in ["production_mw","consumption_mw","renewable_mw","renewable_share"]:
            if out[fld] is None and cur.get(fld) is not None:
                out[fld] = cur.get(fld); used = True
            if out["previous_24h"].get(fld) is None and prev.get(fld) is not None:
                out["previous_24h"][fld] = prev.get(fld)
        if out["generation_time"] is None and cur.get("generation_time"):
            out["generation_time"] = pd.to_datetime(cur["generation_time"], utc=True, errors="coerce")
        if out["load_time"] is None and cur.get("load_time"):
            out["load_time"] = pd.to_datetime(cur["load_time"], utc=True, errors="coerce")
        if used:
            out["source"] = "ENTSO-E GitHub snapshot"

        _gen_direct_ok = bool(getattr(gst, "ok", False))
        _load_direct_ok = bool(getattr(lst, "ok", False))
        _gen_age_ok = out["generation_time"] is not None and is_fresh(out["generation_time"], 180)
        _load_age_ok = out["load_time"] is not None and is_fresh(out["load_time"], 180)

        if out.get("source") == "ENTSO-E GitHub snapshot" and not _snapshot_fresh:
            out["production_mw"] = None
            out["consumption_mw"] = None
            out["renewable_mw"] = None
            out["renewable_share"] = None
            out["source"] = "STALE GitHub snapshot — not displayed"
        else:
            if not _gen_age_ok:
                out["production_mw"] = None
                out["renewable_mw"] = None
                out["renewable_share"] = None
            if not _load_age_ok:
                out["consumption_mw"] = None

        # Explicit connection mode used by UI.
        if _gen_direct_ok or _load_direct_ok:
            out["connection_mode"] = "LIVE"
        elif out.get("source") == "ENTSO-E GitHub snapshot":
            out["connection_mode"] = "SNAPSHOT"
        elif "STALE" in str(out.get("source")):
            out["connection_mode"] = "STALE"
        else:
            out["connection_mode"] = "UNAVAILABLE"
        return out

    baltic_snapshots = {r: make_snapshot(r) for r in BALTICS}

    # Estonia: Elering actual is primary. If Elering is temporarily unavailable,
    # retain validated ENTSO-E / GitHub snapshot values instead of overwriting them with None.
    _ee_used_elering_prod = prod is not None and source_health["Elering system"].displayable
    _ee_used_elering_cons = cons is not None and source_health["Elering system"].displayable

    if prod is not None:
        baltic_snapshots["EE"]["production_mw"] = float(prod)
        if prod_time is not None:
            baltic_snapshots["EE"]["generation_time"] = pd.Timestamp(prod_time)

    if cons is not None:
        baltic_snapshots["EE"]["consumption_mw"] = float(cons)
        if cons_time is not None:
            baltic_snapshots["EE"]["load_time"] = pd.Timestamp(cons_time)

    if _ee_used_elering_prod or _ee_used_elering_cons:
        if _ee_used_elering_prod and _ee_used_elering_cons:
            baltic_snapshots["EE"]["source"] = "Elering actual + ENTSO-E renewables"
        else:
            baltic_snapshots["EE"]["source"] = "Elering partial actual + ENTSO-E fallback"
    else:
        # make_snapshot() has already resolved direct ENTSO-E first and GitHub snapshot second.
        baltic_snapshots["EE"]["source"] = (
            baltic_snapshots["EE"].get("source")
            or "ENTSO-E / GitHub snapshot fallback"
        )
    if not system_df.empty:
        sx = system_df.copy()
        sx["time_utc"] = pd.to_datetime(sx["time_utc"], utc=True, errors="coerce")
        if prod_time is not None:
            baltic_snapshots["EE"]["previous_24h"]["production_mw"] = nearest(sx,"time_utc","production_mw",pd.Timestamp(prod_time).tz_convert("UTC")-pd.Timedelta(hours=24))
        if cons_time is not None:
            baltic_snapshots["EE"]["previous_24h"]["consumption_mw"] = nearest(sx,"time_utc","consumption_mw",pd.Timestamp(cons_time).tz_convert("UTC")-pd.Timedelta(hours=24))


    country_meta = {"EE": ("🇪🇪", "Eesti"), "LV": ("🇱🇻", "Läti"), "LT": ("🇱🇹", "Leedu")}
    for region in BALTICS:
        flag, name = country_meta[region]
        snap = baltic_snapshots.get(region, {"production_mw": None, "consumption_mw": None, "renewable_mw": None, "renewable_share": None, "generation_time": None, "load_time": None, "previous_24h": {}, "source": "andmed puuduvad"})
        prev = snap.get("previous_24h", {}) or {}
        st.markdown(f"**{flag} {name}**")
        sys_cols = st.columns(4)
        sys_cols[0].metric(f"{flag} Tootmine", f"{snap['production_mw']:.0f} MW" if snap['production_mw'] is not None else "—",
                           delta=delta_text(snap.get("production_mw"),prev.get("production_mw")),
                           help=f"Allikas: {snap.get('source')}. Võrdlus sama ajaga 24 tundi tagasi.")
        sys_cols[1].metric(f"{flag} Tarbimine", f"{snap['consumption_mw']:.0f} MW" if snap['consumption_mw'] is not None else "—",
                           delta=delta_text(snap.get("consumption_mw"),prev.get("consumption_mw")),
                           help=f"Allikas: {snap.get('source')}. Võrdlus sama ajaga 24 tundi tagasi.")
        sys_cols[2].metric(f"{flag} Taastuvtootmine", f"{snap['renewable_mw']:.0f} MW" if snap['renewable_mw'] is not None else "—",
                           delta=delta_text(snap.get("renewable_mw"),prev.get("renewable_mw")),
                           help="ENTSO-E A75; vajadusel viimane edukas GitHub Actionsi snapshot.")
        sys_cols[3].metric(f"{flag} Taastuvate osakaal", f"{snap['renewable_share']:.1f}%" if snap['renewable_share'] is not None else "—",
                           delta=delta_text(snap.get("renewable_share"),prev.get("renewable_share")),
                           help="Suhteline muutus võrreldes sama ajaga 24 tundi tagasi.")
        _mode = snap.get("connection_mode", "LIVE" if "Elering" in str(snap.get("source")) else "UNAVAILABLE")
        if _mode == "SNAPSHOT":
            st.warning(f"{flag} {name}: otseühendus puudub — kasutatakse värsket ametliku ENTSO-E GitHub snapshot'i. Allikas: {snap.get('source')}.")
        elif _mode == "STALE":
            st.error(f"{flag} {name}: snapshot on vananenud; väärtusi ei kuvata.")
        elif _mode == "UNAVAILABLE":
            st.error(f"{flag} {name}: värske valideeritud andmeallikas puudub.")
        ages = [age_minutes(t) for t in [snap.get("generation_time"), snap.get("load_time")] if t is not None]
        if ages and max(ages) > 180:
            st.warning(f"{flag} {name}: allika viimane tegelik vaatlus on üle 3 tunni vana. Vananenud väärtust KPI-na ei kuvata.")


    market_cols = st.columns(3)
    market_cols[0].metric("🇪🇪 EE spot — käimasolev MTU", f"{current_prices['EE']:.1f} €/MWh" if current_prices["EE"] is not None else "—")
    market_cols[1].metric("🇫🇮 FI spot — käimasolev MTU", f"{current_prices['FI']:.1f} €/MWh" if current_prices["FI"] is not None else "—")
    spread = None
    if current_prices["EE"] is not None and current_prices["FI"] is not None:
        spread = current_prices["EE"] - current_prices["FI"]
    market_cols[2].metric("EE–FI hinnavahe", f"{spread:+.1f} €/MWh" if spread is not None else "—")

    flow1, flow2 = st.columns(2)

    _ee_fi_flow = latest_border_flows.get("EE–FI")
    _ee_lv_flow = latest_border_flows.get("EE–LV")

    _ee_fi_label = (
        "—" if _ee_fi_flow is None
        else f"{abs(float(_ee_fi_flow)):.0f} MW "
             + ("eksport" if float(_ee_fi_flow) > 0 else "import" if float(_ee_fi_flow) < 0 else "tasakaalus")
    )
    _ee_lv_label = (
        "—" if _ee_lv_flow is None
        else f"{abs(float(_ee_lv_flow)):.0f} MW "
             + ("eksport" if float(_ee_lv_flow) > 0 else "import" if float(_ee_lv_flow) < 0 else "tasakaalus")
    )

    flow1.metric(
        "EE–FI füüsiline netovoog",
        _ee_fi_label,
        delta=(f"{fmt_age(latest_border_flow_time['EE–FI'])} vana" if latest_border_flow_time.get("EE–FI") is not None else None),
        delta_color="off",
        help="ENTSO-E A11. Positiivne märk tähendab Eesti netoeksporti; negatiivne Eesti netoimporti.",
    )
    flow2.metric(
        "EE–LV füüsiline netovoog",
        _ee_lv_label,
        delta=(f"{fmt_age(latest_border_flow_time['EE–LV'])} vana" if latest_border_flow_time.get("EE–LV") is not None else None),
        delta_color="off",
        help="ENTSO-E A11. Positiivne märk tähendab Eesti netoeksporti; negatiivne Eesti netoimporti.",
    )

    st.markdown("#### Piiriülesed füüsilised vood")
    flow_rows = []
    for border, flow in latest_border_flows.items():
        flow_rows.append({
            "Piir": border,
            "Füüsiline netovoog MW": abs(flow) if flow is not None else None,
            "Voo suund": "EE eksport" if flow is not None and flow > 0 else "EE import" if flow is not None and flow < 0 else "—",
        })
    st.dataframe(pd.DataFrame(flow_rows), hide_index=True, use_container_width=True,
                 column_config={"Füüsiline netovoog MW": st.column_config.NumberColumn(format="%.0f")})
    source_link("entsoe_tp", note="A11 actual physical flow")

    st.markdown("#### Turu põhifundamentaalid")
    f1, f2, f3, f4 = st.columns(4)
    f1.metric("🇳🇱 TTF NGP — D", f"{ngp_current['TTF']:.1f} €/MWh" if ngp_current.get("TTF") is not None else "—", help="EEX current NGP; EEX uuendab faili iga 15 minuti järel D/D+1/D+2 jaoks.")
    f2.metric("🇪🇪🇱🇻 Eesti–Läti gaas (LVA–EST) — D", f"{ngp_current['LVA-EST']:.1f} €/MWh" if ngp_current.get("LVA-EST") is not None else "—", help="EEX LVA-EST Neutral Gas Price, current gas day.")
    f3.metric("🇫🇮 Soome gaas (FIN NGP) — D", f"{ngp_current['FIN']:.1f} €/MWh" if ngp_current.get("FIN") is not None else "—", help="EEX FIN Neutral Gas Price, current gas day.")
    f4.metric("🇱🇹 Leedu gaas (LTU NGP) — D", f"{ngp_current['LTU']:.1f} €/MWh" if ngp_current.get("LTU") is not None else "—", help="EEX LTU Neutral Gas Price, current gas day.")
    f5, f6 = st.columns(2)
    f5.metric("🌍 Brent — EIA spot (päevane)", f"{brent_latest:.1f} $/bbl" if brent_latest is not None else "—", help="Viimane kuni kahe tööpäeva vanune EIA päevahind. Intraday hinda näitab eraldi ICE futuur.")
    st.caption(f"Brent spot vaatlus: {pd.Timestamp(_brent_obs).strftime('%d.%m.%Y') if pd.notna(_brent_obs) else 'puudub'} · "
               + ("päevane EIA hind" if brent_latest is not None else "aegunud või avaldamata; hinda ei kuvata"))
    f6.metric("EUA — EEX oksjon", f"{eua_latest:.2f} €/tCO₂" if eua_latest is not None else "—", help="EEX EUA primaaroksjoni viimane clearing price. See ei ole secondary-market intraday hind.")
    st.caption("Operatiivne gaas: EEX NGP current files. Brent on EIA päevane spot-seeria ja EUA EEX primaaroksjoni hind.")



    # ---------- 2. TÄNA JA HOMME — GRAAFILINE HINNAPILT ----------
    st.subheader("Täna ja homme — elektrihinnad")
    st.caption("Elering / Nord Pool day-ahead · 15-min või allika tegelik MTU · ainult avaldatud hinnad, prognoos- ega täiteandmeid ei kasutata.")

    if not prices.empty:
        ph = prices.copy()
        ph = ph[(ph["time_local"].dt.date >= today) & (ph["time_local"].dt.date <= tomorrow)]
        if not ph.empty:
            fig_price = px.line(
                ph,
                x="time_local",
                y="price",
                color="region",
                labels={"time_local": "Aeg", "price": "€/MWh", "region": "Piirkond"},
                color_discrete_map={"EE": "#1f77b4", "FI": "#2ca02c", "LV": "#d62728", "LT": "#ff7f0e"},
            )
            # Make Estonia visually dominant without changing the underlying data.
            for trace in fig_price.data:
                if trace.name == "EE":
                    trace.update(line={"width": 4})
                    trace.update(fill="tozeroy", fillcolor="rgba(31,119,180,0.08)")
                else:
                    trace.update(line={"width": 2})
            fig_price.add_vline(
                x=pd.Timestamp(now_local),
                line_dash="dash",
                line_width=1.5,
                annotation_text="Praegu",
                annotation_position="top",
            )
            midnight_tomorrow = pd.Timestamp(datetime.combine(tomorrow, datetime.min.time(), tzinfo=TALLINN))
            fig_price.add_vline(
                x=midnight_tomorrow,
                line_dash="dot",
                line_width=1,
                annotation_text="Homme",
                annotation_position="top right",
            )
            fig_price.add_hline(y=0, line_width=1, line_dash="dot")
            fig_price.update_layout(
                height=450,
                margin=dict(l=10, r=10, t=25, b=10),
                hovermode="x unified",
                legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
                xaxis_tickformat="%d.%m %H:%M",
                yaxis_title="Hind (€/MWh)",
            )
            st.plotly_chart(fig_price, use_container_width=True)
            source_link("elering_prices")

            ee = ph[ph["region"] == "EE"].copy()
            today_ee = ee[ee["time_local"].dt.date == today]
            tomorrow_ee = ee[ee["time_local"].dt.date == tomorrow]
            k1,k2,k3,k4 = st.columns(4)
            k1.metric("EE hetkehind", f"{current_prices['EE']:.1f} €/MWh" if current_prices["EE"] is not None else "—")
            k2.metric("EE tänane keskmine", f"{today_ee['price'].mean():.1f} €/MWh" if not today_ee.empty else "—")
            if not today_ee.empty:
                k3.metric("EE tänane min / max", f"{today_ee['price'].min():.1f} / {today_ee['price'].max():.1f} €/MWh")
            else:
                k3.metric("EE tänane min / max", "—")
            k4.metric("EE homne keskmine", f"{tomorrow_ee['price'].mean():.1f} €/MWh" if not tomorrow_ee.empty else "Pole veel avaldatud")
        else:
            st.info("Eleringi hinnaliides ei tagastanud tänase ega homse päeva avaldatud hindu.")
    else:
        st.warning(f"Hinnagraafik puudub: {price_status.error or price_status.note or 'Eleringi hinnaliidesest ei tulnud andmeid.'}")

    # ---------- OPERATIONAL ATTENTION RULES ----------
    # These are transparent dashboard heuristics, not regulatory limits or forecasts.
    attention: list[dict[str, str]] = []

    def add_alert(level: str, topic: str, message: str) -> None:
        attention.append({"Tase": level, "Teema": topic, "Tähelepanek": message})

    if spread is not None and abs(spread) >= 50:
        level = "🔴 Kõrge" if abs(spread) >= 100 else "🟠 Tähelepanu"
        add_alert(level, "EE–FI hinnavahe", f"Hetke hinnavahe {spread:+.1f} €/MWh (reegel: |spread| ≥ 50 €/MWh).")

    # GIE AGSI+: alert on low fill or a fast 7-day percentage-point decline, without seasonal forecasting.
    if not storage_df.empty and "scope" in storage_df.columns and "full" in storage_df.columns:
        for scope in ["EU", "LV"]:
            g = storage_df[storage_df["scope"] == scope].copy()
            if g.empty:
                continue
            if "gas_day" in g.columns:
                g = g.dropna(subset=["gas_day", "full"]).sort_values("gas_day")
            else:
                g = g.dropna(subset=["full"])
            if g.empty:
                continue
            latest_fill = float(g.iloc[-1]["full"])
            if latest_fill < 30:
                add_alert("🟠 Tähelepanu", f"{scope} gaasihoidlad", f"Täituvus on {latest_fill:.1f}% (heuristiline reegel: < 30%).")
            if len(g) >= 8:
                seven_days_ago = float(g.iloc[-8]["full"])
                drop_pp = seven_days_ago - latest_fill
                if drop_pp >= 5:
                    add_alert("🟠 Tähelepanu", f"{scope} gaasihoidlad", f"Täituvus langes ~7 päevaga {drop_pp:.1f} protsendipunkti (reegel: ≥ 5 pp).")

    st.markdown("#### ⚠️ Tähelepanu vajavad näitajad")
    if attention:
        attention_df = pd.DataFrame(attention)
        order = {"🔴 Kõrge": 0, "🟠 Tähelepanu": 1}
        attention_df["_order"] = attention_df["Tase"].map(order).fillna(9)
        st.dataframe(attention_df.sort_values("_order").drop(columns="_order"), hide_index=True, use_container_width=True)
        source_links(("elering_prices", "price heuristics"), ("gie_agsi", "storage"))
    else:
        st.success("Ükski seadistatud operatiivne tähelepanureegel ei ole praegu käivitunud.")
    st.caption("Tähelepanureeglid on läbipaistvad heuristikad olukorrapildi kiirendamiseks, mitte ametlikud häirepiirid ega prognoosid. Lävendid: |EE–FI spread| 50/100 €/MWh; gaasihoidlad <30% või ~7 päeva langus ≥5 pp.")

    # Daily market table near top: decision-useful and compact.
    st.markdown("#### Tänane ja homne päev-ette hinnapilt")
    if price_daily.empty:
        st.warning("Regionaalsed hinnad pole hetkel saadaval.")
    else:
        rows = []
        for region in REGIONS:
            row = {"Piirkond": region}
            for d, label in [(today, "Täna €/MWh"), (tomorrow, "Homme €/MWh")]:
                x = price_daily[(price_daily["region"] == region) & (price_daily["local_date"] == d)]
                row[label] = float(x.iloc[0]["mean"]) if not x.empty else None
            rows.append(row)
        overview_prices = pd.DataFrame(rows)
        st.dataframe(
            overview_prices,
            hide_index=True,
            use_container_width=True,
            column_config={
                "Täna €/MWh": st.column_config.NumberColumn(format="%.1f"),
                "Homme €/MWh": st.column_config.NumberColumn(format="%.1f"),
            },
        )
        source_link("elering_prices", note="today/tomorrow day-ahead averages")

    # Freshness & source health is a first-class part of the dashboard.
    with st.expander("🔌 Andmeallikate staatus — BalticPulse source-integrity standard", expanded=False):
        st.markdown(
            "**🟢 LIVE** = otseühendus ametliku allikaga ja andmed värsked · "
            "**🟡 SNAPSHOT/MIRROR** = otseühendust Streamlitist ei ole, kuid värske kontrollitud vahendus on olemas · "
            "**🟠 STALE** = andmed on üle allikapõhise värskuspiiri ja neid KPI-na ei kuvata · "
            "**🔴 UNAVAILABLE** = värske valideeritud väärtus puudub."
        )
        st.caption("Reegel: BalticPulse ei genereeri puuduvaid väärtusi, ei interpoleeri hetkenäitajaid ega esita vananenud väärtust kehtiva hetkeinfona.")
        price_age_min = newest_age_minutes(prices, "time_utc")
        sys_age_min = age_minutes(sys_time)
        storage_age_min = newest_age_minutes(storage_df, "gasDayStart", "date")

        left_status, right_status = st.columns(2)

        with left_status:
            render_health_badge(source_health["Elering prices"])
            render_health_badge(source_health["Elering system"])
            render_health_badge(source_health["Day-ahead map snapshot"])
            render_health_badge(source_health["Futures snapshot"])
            st.markdown("---")
            price_has_current = current_prices.get("EE") is not None
            source_badge(
                "Elering hinnad",
                detail=status_detail(
                    price_status,
                    age_text=fmt_age(prices["time_utc"].max()) if not prices.empty and "time_utc" in prices else None,
                    extra=("käimasolev MTU olemas" if price_has_current else "käimasolev MTU puudub"),
                ),
                level=status_level(
                    price_status,
                    has_data=(not prices.empty and price_has_current),
                    age_min=price_age_min,
                    warn_after_min=120,
                ),
            )

            source_badge(
                "Elering süsteem",
                detail=status_detail(system_status, age_text=fmt_age(sys_time)),
                level=status_level(
                    system_status,
                    has_data=not system_df.empty,
                    age_min=sys_age_min,
                    warn_after_min=15,
                ),
            )
            source_badge(
                "GIE AGSI+",
                detail=status_detail(
                    storage_status,
                    age_text=(fmt_age(storage_df["gasDayStart"].max()) if not storage_df.empty and "gasDayStart" in storage_df else None),
                ),
                level=status_level(
                    storage_status,
                    has_data=not storage_df.empty,
                    age_min=storage_age_min,
                    warn_after_min=72 * 60,
                ),
            )

            for region in ["EE", "LV", "LT"]:
                gdf, gst = entsoe_generation_results[region]
                ldf, lst = entsoe_load_results[region]
                g_age = newest_age_minutes(gdf, "time_local")
                l_age = newest_age_minutes(ldf, "time_local")

                source_badge(
                    f"{region} ENTSO-E tootmine",
                    detail=status_detail(
                        gst,
                        age_text=(fmt_age(gdf["time_local"].max()) if not gdf.empty and "time_local" in gdf else None),
                    ),
                    level=status_level(gst, has_data=not gdf.empty, age_min=g_age, warn_after_min=120),
                )
                source_badge(
                    f"{region} ENTSO-E tarbimine",
                    detail=status_detail(
                        lst,
                        age_text=(fmt_age(ldf["time_local"].max()) if not ldf.empty and "time_local" in ldf else None),
                    ),
                    level=status_level(lst, has_data=not ldf.empty, age_min=l_age, warn_after_min=120),
                )

        with right_status:
            for _key in ["EEX NGP TTF", "EEX NGP LVA-EST", "EEX NGP FIN", "EEX NGP LTU", "Brent", "EUA"]:
                if _key in source_health:
                    render_health_badge(source_health[_key])
            st.markdown("---")
            for area, (df_ngp, st_ngp) in ngp_current_results.items():
                source_badge(
                    st_ngp.source or f"EEX {area} NGP",
                    detail=status_detail(
                        st_ngp,
                        extra=(f"{len(df_ngp)} väärtust" if not df_ngp.empty else "väärtus puudub"),
                    ),
                    level=status_level(st_ngp, has_data=not df_ngp.empty),
                )

            source_badge(
                ttf_status.source or "EEX TTF",
                detail=status_detail(ttf_status, extra=(f"{len(ttf_df)} rida" if not ttf_df.empty else "andmed puuduvad")),
                level=status_level(ttf_status, has_data=not ttf_df.empty),
            )

            source_badge(
                brent_status.source or "EIA Brent",
                detail=status_detail(
                    brent_status,
                    age_text=(fmt_age(_brent_obs) if pd.notna(_brent_obs) else None),
                ),
                level=status_level(
                    brent_status,
                    has_data=brent_latest is not None,
                    age_min=age_minutes(_brent_obs),
                    warn_after_min=4 * 24 * 60,
                ),
            )

            source_badge(
                eua_status.source or "EEX EUA",
                detail=status_detail(
                    eua_status,
                    age_text=(fmt_age(eua_df["Date"].max()) if not eua_df.empty and "Date" in eua_df else None),
                ),
                level=status_level(
                    eua_status,
                    has_data=not eua_df.empty,
                    age_min=newest_age_minutes(eua_df, "Date"),
                    warn_after_min=14 * 24 * 60,
                ),
            )

            flow_ok = sum(1 for stx in entsoe_flow_statuses if stx.ok)
            flow_level = "error" if flow_ok == 0 else ("warning" if flow_ok < len(entsoe_flow_statuses) or entsoe_flows.empty else "ok")
            source_badge(
                "ENTSO-E EE füüsilised vood",
                detail=f"{flow_ok}/{len(entsoe_flow_statuses)} päringut OK",
                level=flow_level,
            )

            baltic_flow_ok = sum(1 for stx in entsoe_baltic_flow_statuses if stx.ok)
            baltic_flow_level = "error" if baltic_flow_ok == 0 else ("warning" if baltic_flow_ok < len(entsoe_baltic_flow_statuses) or entsoe_baltic_flows.empty else "ok")
            source_badge(
                "ENTSO-E Baltikumi füüsilised vood",
                detail=f"{baltic_flow_ok}/{len(entsoe_baltic_flow_statuses)} päringut OK",
                level=baltic_flow_level,
            )

    # ---------- 2. DETAIL TABS ----------
    tab_overview, tab_prices, tab_fundamentals, tab_system, tab_entsoe, tab_gas, tab_quality = st.tabs([
        "📌 Põhivaade", "⚡ Elektrihinnad", "📈 Fundamentaalid", "🏭 Baltikumi süsteem", "🌐 ENTSO-E", "🔥 Gaasihoidlad", "✅ Andmekvaliteet"
    ])

    with tab_overview:
        st.markdown("## 📌 Üldine armatuurlaud")
        st.caption("Kiirvaade olulisematele elektri-, süsteemi- ja gaasinäitajatele.")
        left, right = st.columns(2)
        with left:
            st.markdown("### 🇪🇪 EE / 🇱🇻 LV / 🇱🇹 LT / 🇫🇮 FI spot-hinnad")
            if prices.empty:
                st.warning("Eleringi hinnad pole hetkel saadaval.")
            else:
                p = prices[prices["time_local"].dt.date >= today]
                fig = px.line(p, x="time_local", y="price", color="region", labels={"price":"€/MWh","time_local":"Aeg","region":"Piirkond"})
                fig.add_vline(x=now_local, line_dash="dash")
                st.plotly_chart(fig, use_container_width=True)
                source_link("elering_prices")
            source_badge("Elering Dashboard / Nord Pool", price_status.ok)
        with right:
            st.markdown("### 🇪🇪🇱🇻🇱🇹 Baltikumi süsteemi hetkeseis")
            rows = []
            for region in BALTICS:
                snap = baltic_snapshots.get(region, {"production_mw": None, "consumption_mw": None, "renewable_mw": None, "renewable_share": None, "generation_time": None, "load_time": None, "previous_24h": {}, "source": "andmed puuduvad"})
                rows.append({"Riik": country_meta[region][0] + " " + region, "Tootmine MW": snap["production_mw"], "Tarbimine MW": snap["consumption_mw"], "Taastuv MW": snap["renewable_mw"], "Taastuv %": snap["renewable_share"]})
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True, column_config={"Tootmine MW": st.column_config.NumberColumn(format="%.0f"), "Tarbimine MW": st.column_config.NumberColumn(format="%.0f"), "Taastuv MW": st.column_config.NumberColumn(format="%.0f"), "Taastuv %": st.column_config.NumberColumn(format="%.1f%%")})
            source_links(("elering_system", "EE primary"), ("entsoe_tp", "LV/LT and EE renewables/fallback"), ("baltic_snapshot", "fallback only"))
            st.caption("EE kogunäidud: Elering actual; Eleringi puudumisel valideeritud ENTSO-E/snapshot fallback. LV/LT: ENTSO-E actual.")

    with tab_prices:
        st.markdown("## ⚡ Elektriturg")
        st.caption("Päeva-ette hinnad, ajalugu, kuustatistika ja elektri forward-vaade.")
        st.markdown("### Regionaalsed päeva-ette hinnad")
        st.caption("EE/LV/LT/FI Nord Pool päev-ette hinnad Eleringi avalikust NPS API-st.")

        if prices.empty:
            st.error(price_status.error or "Andmed pole saadaval")
        else:
            selected = st.multiselect("Piirkonnad", REGIONS, default=REGIONS, key="price_live_regions")
            p = prices[prices["region"].isin(selected)]
            fig = px.line(p, x="time_local", y="price", color="region")
            fig.update_layout(yaxis_title="€/MWh", xaxis_title="Aeg (Europe/Tallinn)")
            st.plotly_chart(fig, use_container_width=True)
            source_link("elering_prices")

        st.divider()
        st.markdown("### 🗺️ EL päev-ette hinnakaardid")
        st.caption(
            "ENTSO-E Transparency Platform A44 päev-ette hinnad. Mitme hinnapiirkonnaga riigi puhul "
            "värvitakse riik hinnapiirkondade lihtsa aritmeetilise keskmise järgi; see on BalticPulse'i agregatsioon, mitte ametlik riigihind."
        )
        _today_str = today.isoformat()
        _tomorrow_str = tomorrow.isoformat()
        _map_today = _eu_map_df(eu_da_snapshot, _today_str)
        _map_tomorrow = _eu_map_df(eu_da_snapshot, _tomorrow_str)
        _m1, _m2 = st.columns(2)
        with _m1:
            _render_eu_price_map(_map_today, f"Täna · {today:%d.%m.%Y}")
        with _m2:
            if now_local.hour >= 14:
                _render_eu_price_map(_map_tomorrow, f"Homme · {tomorrow:%d.%m.%Y}")
            else:
                st.info("Homse kaart kuvatakse pärast 14:00 Eesti aja järgi, kui ENTSO-E päev-ette hinnad on avaldatud.")
        if not eu_da_status.get("ok"):
            st.warning(f"EL hinnakaardi snapshot: {eu_da_status.get('error')}")
        elif eu_da_snapshot.get("updated_at"):
            _map_updated = pd.to_datetime(eu_da_snapshot.get("updated_at"), utc=True, errors="coerce")
            if pd.notna(_map_updated):
                st.caption(f"Kaardi snapshot uuendatud {_map_updated.tz_convert(TALLINN):%d.%m.%Y %H:%M}.")

        st.divider()
        st.markdown("#### Ajaloolised päev-ette hinnad")

        price_period = st.segmented_control(
            "Periood",
            ["1 nädal", "1 kuu", "1 aasta", "5 aastat"],
            default="1 kuu",
            key="electricity_history_period",
        )
        hist_regions = st.multiselect(
            "Ajaloo piirkonnad", REGIONS, default=REGIONS, key="price_history_regions"
        )

        with st.spinner(f"Laadin perioodi „{price_period}“ (juba laaditud andmed avanevad vahemälust)..."):
            hist_df, hist_errors = load_electricity_price_history(_history_days(price_period), today.isoformat())
        loaded_period = price_period

        if not hist_df.empty:
            h = hist_df[hist_df["region"].isin(hist_regions)].copy()

            if loaded_period in ("1 aasta", "5 aastat"):
                rows = []
                for region, g in h.groupby("region"):
                    g = g.sort_values("time_utc").copy()
                    g["duration_h"] = _price_interval_hours(g)
                    g["day"] = g["time_local"].dt.date
                    for day, d in g.groupby("day"):
                        denom = d["duration_h"].sum()
                        avg = (d["price"] * d["duration_h"]).sum() / denom if denom > 0 else d["price"].mean()
                        rows.append({"date": pd.Timestamp(day), "region": region, "price": avg})
                chart_df = pd.DataFrame(rows)
                fig = px.line(
                    chart_df, x="date", y="price", color="region",
                    labels={"date":"Kuupäev","price":"Päeva keskmine €/MWh","region":"Piirkond"},
                )
            else:
                fig = px.line(
                    h, x="time_local", y="price", color="region",
                    labels={"time_local":"Aeg","price":"€/MWh","region":"Piirkond"},
                )

            st.plotly_chart(fig, use_container_width=True)
            source_link("elering_prices", note="historical Nord Pool day-ahead prices")

            errs = hist_errors
            if errs:
                with st.expander("Ajaloo päringu hoiatused"):
                    for err in errs[:20]:
                        st.write(f"• {err}")

        st.markdown("#### Viimase 12 kuu kuuhinnad")
        st.caption(
            "Keskmine on ajakaalutud MTU keskmine. Minimaalne ja maksimaalne hind on "
            "konkreetse turuperioodi hind koos kuupäeva ja kellaajaga."
        )

        if st.button("Laadi viimase 12 kuu kuustatistika", key="load_electricity_monthly"):
            with st.spinner("Laadin viimase 12 kuu andmeid..."):
                year_df, year_errors = load_electricity_price_history(370, today.isoformat())
                st.session_state["electricity_monthly_table"] = electricity_monthly_summary(year_df, 12)
                st.session_state["electricity_monthly_errors"] = year_errors

        monthly_el = st.session_state.get("electricity_monthly_table", pd.DataFrame())
        if not monthly_el.empty:
            _monthly_view = monthly_el.copy()
            _flags = {"EE":"🇪🇪 EE", "LV":"🇱🇻 LV", "LT":"🇱🇹 LT", "FI":"🇫🇮 FI"}
            _monthly_view["Piirkond"] = _monthly_view["Piirkond"].map(_flags).fillna(_monthly_view["Piirkond"])
            st.dataframe(
                _shade_month_groups(_monthly_view),
                hide_index=True,
                use_container_width=True,
            )
            source_link("elering_prices", note="12-month monthly statistics")
            st.caption("Iga kuu neli rida (EE/LV/LT/FI) on sama taustavarjundiga; järgmine kuu kasutab vahelduvat varjundit.")

        st.caption("Allikas: Eleringi avalik NPS API / Nord Pool päev-ette turg.")


        st.divider()
        st.markdown("#### ⚡ Elektri forward-vaade")
        st.caption(
            "Euronext Nord Pool Power Futures. Nordic SYS on süsteemihinna futuur; "
            "FI implied = SYS + Helsinki EPAD; LT implied = SYS + Vilnius EPAD. "
            "Kuvatakse viimase eduka GitHub snapshot'i settlement-hinnad."
        )

        pkey = _future_key_rows(futures_snapshot, "power")
        pcurve = _future_curve_rows(futures_snapshot, "power")
        _render_futures_market_state(_futures_market_state("power", futures_snapshot))
        pmeta = futures_snapshot.get("power", {}) if futures_snapshot else {}
        p_updated = pd.to_datetime(futures_snapshot.get("updated_at"), utc=True, errors="coerce") if futures_snapshot else pd.NaT

        if pkey.empty:
            _f_updated, _f_errors = _futures_diag(futures_snapshot, "power")
            st.warning(
                f"Elektrifutuuride snapshotis pole hinnaseeriat. Snapshot: {_f_updated}. "
                "Kontrolli GitHub Actions → Update BalticPulse futures."
            )
            if _f_errors:
                with st.expander("Elektrifutuuride diagnostika", expanded=True):
                    for err in _f_errors:
                        st.write(f"• {err}")
        else:
            if pd.notna(p_updated):
                st.caption(f"Futuuride snapshot uuendatud: {p_updated.tz_convert(TALLINN).strftime('%d.%m.%Y %H:%M')}")
            st.dataframe(
                pkey,
                hide_index=True,
                use_container_width=True,
                column_config={
                    "settlement": st.column_config.NumberColumn("Settlement €/MWh", format="%.2f"),
                    "open_interest": st.column_config.NumberColumn("Open interest", format="%.0f"),
                },
            )
            source_links(("euronext_power", "settlement prices"), ("futures_snapshot", "GitHub Actions snapshot"))

            if not pcurve.empty:
                curve_sel = st.multiselect(
                    "Forward curve",
                    sorted(pcurve["series"].dropna().unique().tolist()),
                    default=[x for x in ["Nordic SYS", "FI implied", "LT implied"] if x in set(pcurve["series"])],
                    key="power_future_curve_series",
                )
                pc = pcurve[pcurve["series"].isin(curve_sel)].copy()
                if not pc.empty:
                    pc["delivery_start"] = pd.to_datetime(pc["delivery_start"], errors="coerce")
                    fig = px.line(
                        pc.sort_values("delivery_start"), x="delivery_start", y="settlement",
                        color="series", markers=True,
                        hover_data=[c for c in ["delivery", "open_interest", "source_product"] if c in pc.columns],
                        labels={"delivery_start":"Tarneperioodi algus", "settlement":"€/MWh", "series":"Forward"},
                        title="Elektri forward curve",
                    )
                    st.plotly_chart(fig, use_container_width=True)
                    source_links(("euronext_power", "forward curve"), ("futures_snapshot", "GitHub Actions snapshot"))
            st.caption("Allikas: Euronext Amsterdam — Nordic System Price Futures, Helsinki EPAD ja Vilnius EPAD.")


    with tab_system:
        st.markdown("## 🏭 Baltikumi elektrisüsteem")
        st.caption(
            "Tegelik tootmine, tarbimine ja taastuvtootmine. "
            "Lühivaade kasutab jooksvaid ametlikke allikaid; pikem ajalugu laaditakse nõudmisel."
        )

        system_period = st.segmented_control(
            "Graafiku periood",
            ["48 tundi", "1 nädal", "1 kuu", "1 aasta", "5 aastat"],
            default="48 tundi",
            key="baltic_system_period",
        )
        system_days = _system_history_days(system_period)

        if system_period != "48 tundi":
            st.caption(
                "Pikema perioodi andmeid ei laadita rakenduse käivitamisel. "
                "Vajuta allpool valitud riigi vaates nuppu „Laadi periood“."
            )

        st.markdown("### 🇪🇪🇱🇻🇱🇹 Tegelik süsteemipilt")
        st.caption(
            "EE: Elering actual on primaarne, ENTSO-E actual on fallback/taastuvtootmise allikas. "
            "LV/LT: ENTSO-E A75 actual generation ja A65 actual total load. "
            "Puuduvaid perioode ei interpoleerita."
        )

        ee_tab, lv_tab, lt_tab = st.tabs(["🇪🇪 Eesti", "🇱🇻 Läti", "🇱🇹 Leedu"])

        for region, panel in [("EE", ee_tab), ("LV", lv_tab), ("LT", lt_tab)]:
            with panel:
                flag, name = country_meta[region]
                snap = baltic_snapshots.get(
                    region,
                    {
                        "production_mw": None,
                        "consumption_mw": None,
                        "renewable_mw": None,
                        "renewable_share": None,
                        "generation_time": None,
                        "load_time": None,
                        "previous_24h": {},
                        "source": "andmed puuduvad",
                    },
                )
                prev = snap.get("previous_24h", {}) or {}

                k = st.columns(4)
                k[0].metric(
                    "Tootmine",
                    f"{snap['production_mw']:.0f} MW" if snap.get("production_mw") is not None else "—",
                    delta=delta_text(snap.get("production_mw"), prev.get("production_mw")),
                )
                k[1].metric(
                    "Tarbimine",
                    f"{snap['consumption_mw']:.0f} MW" if snap.get("consumption_mw") is not None else "—",
                    delta=delta_text(snap.get("consumption_mw"), prev.get("consumption_mw")),
                )
                k[2].metric(
                    "Taastuvtootmine",
                    f"{snap['renewable_mw']:.0f} MW" if snap.get("renewable_mw") is not None else "—",
                    delta=delta_text(snap.get("renewable_mw"), prev.get("renewable_mw")),
                )
                k[3].metric(
                    "Taastuvate osakaal",
                    f"{snap['renewable_share']:.1f}%" if snap.get("renewable_share") is not None else "—",
                    delta=delta_text(snap.get("renewable_share"), prev.get("renewable_share")),
                )

                # ---------- current 48h view ----------
                if system_period == "48 tundi":
                    if region == "EE":
                        ee_sys = system_df.copy().sort_values("time_utc") if not system_df.empty else pd.DataFrame()
                        ee_gdf, ee_gst = entsoe_generation_results.get("EE", (pd.DataFrame(), None))
                        ee_ldf, ee_lst = entsoe_load_results.get("EE", (pd.DataFrame(), None))

                        chart = _build_system_history_chart("EE", ee_sys, ee_gdf, ee_ldf)

                        # If the live Elering/ENTSO-E time series are unavailable, use only a
                        # validated persisted official snapshot history.
                        if chart.empty:
                            hist = snapshot_history_df(baltic_system_snapshot, "EE")
                            if not hist.empty:
                                parts = []
                                for col, label in [
                                    ("production_mw", "Tootmine"),
                                    ("consumption_mw", "Tarbimine"),
                                    ("renewable_mw", "Taastuvtootmine"),
                                ]:
                                    if col in hist.columns:
                                        q = hist[["time_utc", "time_local", col]].copy().dropna(subset=[col])
                                        if not q.empty:
                                            q["MW"] = pd.to_numeric(q[col], errors="coerce")
                                            q["series"] = label
                                            parts.append(q[["time_utc", "time_local", "MW", "series"]])
                                if parts:
                                    chart = pd.concat(parts, ignore_index=True)
                                    st.info("Graafik kasutab viimast edukat ametlikku GitHub snapshot-ajalugu.")

                        if chart.empty:
                            st.warning(
                                "Eesti graafiku jaoks puudub praegu nii värske Eleringi/ENTSO-E aegrida "
                                "kui ka ametlik snapshot-ajalugu. KPI-d võivad olla saadaval eraldi viimasest vaatlusest."
                            )
                        else:
                            cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=48)
                            chart = chart[chart["time_utc"] >= cutoff]
                            fig = px.line(
                                chart,
                                x="time_local",
                                y="MW",
                                color="series",
                                labels={"time_local": "Aeg", "series": "Näitaja"},
                                title="Eesti tegelik tootmine, tarbimine ja taastuvtootmine",
                            )
                            st.plotly_chart(fig, use_container_width=True)
                            source_links(("elering_system", "EE production/load primary"), ("entsoe_tp", "renewables and fallback"), ("baltic_snapshot", "fallback only"))
                            _shown_series = sorted(chart["series"].dropna().unique().tolist())
                            st.caption("Graafikul: " + ", ".join(_shown_series))

                        source_badge(
                            "Elering / ENTSO-E EE",
                            level="ok" if not chart.empty else "error",
                            detail=snap.get("source", "allikas puudub"),
                        )

                    else:
                        gdf, gst = entsoe_generation_results.get(region, (pd.DataFrame(), None))
                        ldf, lst = entsoe_load_results.get(region, (pd.DataFrame(), None))
                        chart = _build_system_history_chart(region, pd.DataFrame(), gdf, ldf)

                        if chart.empty:
                            hist = snapshot_history_df(baltic_system_snapshot, region)
                            if not hist.empty:
                                parts = []
                                for col, label in [
                                    ("production_mw", "Tootmine"),
                                    ("consumption_mw", "Tarbimine"),
                                    ("renewable_mw", "Taastuvtootmine"),
                                ]:
                                    if col in hist.columns:
                                        q = hist[["time_utc", "time_local", col]].copy().dropna(subset=[col])
                                        if not q.empty:
                                            q["MW"] = pd.to_numeric(q[col], errors="coerce")
                                            q["series"] = label
                                            parts.append(q[["time_utc", "time_local", "MW", "series"]])
                                if parts:
                                    chart = pd.concat(parts, ignore_index=True)
                                    st.info("Kasutatakse viimast edukat ENTSO-E GitHub snapshot-ajalugu.")

                        if chart.empty:
                            st.warning(f"{name}: tegelik ajalooline aegrida pole hetkel saadaval.")
                        else:
                            cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(hours=48)
                            chart = chart[chart["time_utc"] >= cutoff]
                            fig = px.line(
                                chart,
                                x="time_local",
                                y="MW",
                                color="series",
                                labels={"time_local": "Aeg", "series": "Näitaja"},
                                title=f"{name} tegelik tootmine, tarbimine ja taastuvtootmine",
                            )
                            st.plotly_chart(fig, use_container_width=True)
                            source_links(("entsoe_tp", "A75 generation + A65 load"), ("baltic_snapshot", "fallback only"))

                        source_badge(
                            f"ENTSO-E {region}",
                            level="ok" if not chart.empty else "error",
                            detail=(gst.error if gst is not None and not gst.ok else "A75 generation + A65 load"),
                        )

                # ---------- user-selected historical view ----------
                else:
                    load_key = f"system_hist_load_{region}_{system_period}"
                    state_key = f"system_hist_{region}_{system_period}"

                    if st.button(f"Laadi {system_period} — {name}", key=load_key):
                        with st.spinner(f"Laadin {name} süsteemiandmeid: {system_period}..."):
                            token = secret("ENTSOE_API_KEY")

                            if region == "EE":
                                ee_hist, ee_errors = load_elering_system_history(system_days)
                                g_hist, l_hist, entsoe_errors = load_entsoe_system_history(token, "EE", system_days)
                                chart = _build_system_history_chart("EE", ee_hist, g_hist, l_hist)
                                errors = list(ee_errors) + list(entsoe_errors)
                                source_text = (
                                    "Elering actual + ENTSO-E A75/A65"
                                    if not ee_hist.empty
                                    else "ENTSO-E A75/A65 (Elering history unavailable)"
                                )
                            else:
                                g_hist, l_hist, errors = load_entsoe_system_history(token, region, system_days)
                                chart = _build_system_history_chart(region, pd.DataFrame(), g_hist, l_hist)
                                source_text = "ENTSO-E A75/A65"

                            st.session_state[state_key] = {
                                "chart": chart,
                                "errors": errors,
                                "source": source_text,
                            }

                    loaded = st.session_state.get(state_key)
                    if loaded:
                        chart = loaded.get("chart", pd.DataFrame())
                        errors = loaded.get("errors", [])
                        source_text = loaded.get("source", "ametlik allikas")

                        if chart is None or chart.empty:
                            st.warning(
                                f"{name}: valitud perioodi jaoks ei saadud valideeritud tegelikke andmeid. "
                                "Puuduvat osa ei täideta."
                            )
                        else:
                            chart_show = _aggregate_system_history(chart, system_period)
                            fig = px.line(
                                chart_show,
                                x="time_local",
                                y="MW",
                                color="series",
                                labels={"time_local": "Aeg", "series": "Näitaja"},
                                title=f"{name} — {system_period}",
                            )
                            st.plotly_chart(fig, use_container_width=True)
                            source_links(("elering_system", "EE when available"), ("entsoe_tp", "EE/LV/LT actual history"))

                            min_t = pd.to_datetime(chart["time_utc"], utc=True, errors="coerce").min()
                            max_t = pd.to_datetime(chart["time_utc"], utc=True, errors="coerce").max()
                            coverage = (
                                f"{min_t.tz_convert(TALLINN).strftime('%d.%m.%Y %H:%M')} – "
                                f"{max_t.tz_convert(TALLINN).strftime('%d.%m.%Y %H:%M')}"
                                if pd.notna(min_t) and pd.notna(max_t)
                                else "katvus teadmata"
                            )
                            st.caption(f"Allikas: {source_text}. Tegelik andmekatvus: {coverage}.")

                        if errors:
                            with st.expander("Allikapäringu diagnostika"):
                                for err in errors[:30]:
                                    st.write(f"• {err}")
                    else:
                        st.info(
                            f"Valitud on {system_period}. Ajaloolised andmed laaditakse ainult nupuvajutusel, "
                            "et pikk päring ei aeglustaks BalticPulse'i käivitumist."
                        )

                # Network context remains current-only and independent from selected history period.
                if not entsoe_baltic_flows.empty:
                    rel = entsoe_baltic_flows[
                        (entsoe_baltic_flows["from_region"] == region)
                        | (entsoe_baltic_flows["to_region"] == region)
                    ].copy()
                    if not rel.empty:
                        latest_rows = []
                        for (border, direction), gx in rel.groupby(["border", "direction"]):
                            row = gx.sort_values("time_utc").tail(1).iloc[0]
                            latest_rows.append(
                                {
                                    "Piir": border,
                                    "Suund": direction,
                                    "Voog MW": row["flow_mw"],
                                    "Vaatlus": row["time_local"],
                                }
                            )
                        st.markdown("#### Piiriülesed füüsilised vood")
                        st.dataframe(
                            pd.DataFrame(latest_rows),
                            hide_index=True,
                            use_container_width=True,
                            column_config={
                                "Voog MW": st.column_config.NumberColumn(format="%.0f")
                            },
                        )
                        source_link("entsoe_tp", note="A11 actual physical flow")
                        st.caption(
                            "ENTSO-E A11 actual physical flow. Võrguvood on hetkevaade ega muutu "
                            "ülaltoodud ajaloo perioodivalikuga."
                        )


    with tab_entsoe:
        st.markdown("### ENTSO-E Transparency Platform — Baltikumi tegelik tootmine ja Eesti piiriülesed füüsilised vood")
        st.caption("Baltikumi tootmisjaotus: A75 / A16 realised; koormus: A65. Eesti füüsilised vood: A11. Need on ENTSO-E Transparency Platformi allikaandmed, mitte dashboardis tuletatud väärtused.")

        gen_tabs = st.tabs(["🇪🇪 Eesti", "🇱🇻 Läti", "🇱🇹 Leedu"])
        for region, panel in zip(BALTICS, gen_tabs):
            with panel:
                name = {"EE": "Eesti", "LV": "Läti", "LT": "Leedu"}[region]
                gdf, gst = entsoe_generation_results[region]
                st.markdown(f"#### {name} tegelik elektritootmine tootmisliigi kaupa")
                if gdf.empty:
                    st.warning(gst.error or gst.note or f"{name} ENTSO-E tootmisandmed pole saadaval.")
                else:
                    g = gdf.dropna(subset=["generation_mw"]).copy()
                    fig = px.area(g, x="time_local", y="generation_mw", color="technology",
                                  labels={"time_local": "Aeg", "generation_mw": "MW", "technology": "Tootmisliik"})
                    fig.update_layout(legend_title_text="Tootmisliik")
                    st.plotly_chart(fig, use_container_width=True)
                    source_link("entsoe_tp", note=f"{region} A75 actual generation by type")
                    latest_t = g["time_utc"].max()
                    latest_g = (g[g["time_utc"] == latest_t]
                                .groupby("technology", as_index=False)["generation_mw"]
                                .sum().sort_values("generation_mw", ascending=False))
                    st.dataframe(latest_g, hide_index=True, use_container_width=True,
                                 column_config={"generation_mw": st.column_config.NumberColumn("MW", format="%.1f")})
                    st.caption(f"Uusim ENTSO-E tootmisvaatlus: {fmt_age(latest_t)} tagasi.")

        st.markdown("#### Eesti–Soome ja Eesti–Läti füüsilised vood")
        if entsoe_flows.empty:
            st.warning("ENTSO-E piiriülesed füüsilised vood pole hetkel saadaval.")
        else:
            f = entsoe_flows.copy()
            # Directional series are shown explicitly; signed_mw is only used for Estonia-centric net view.
            fig = px.line(f, x="time_local", y="flow_mw", color="direction", facet_row="border",
                          labels={"time_local": "Aeg", "flow_mw": "MW", "direction": "Suund"})
            st.plotly_chart(fig, use_container_width=True)
            source_link("entsoe_tp", note="A11 directional physical flows")
            net = f.groupby(["time_utc", "time_local", "border"], as_index=False)["signed_mw"].sum()
            fig2 = px.line(net, x="time_local", y="signed_mw", color="border",
                           labels={"time_local": "Aeg", "signed_mw": "Eesti netoeksport (+) / netoimport (−), MW", "border": "Piir"})
            fig2.add_hline(y=0, line_dash="dash")
            st.plotly_chart(fig2, use_container_width=True)
            source_link("entsoe_tp", note="A11 flows aggregated to Estonia net view")
            st.caption("Netovoo märk on defineeritud Eesti vaates: + = eksport Eestist, − = import Eestisse. Alusread jäävad eraldi suunaga nähtavaks.")

    with tab_gas:
        st.markdown("## 🔥 Gaasihoidlad")
        st.caption("EL ja Inčukalnsi täituvus ning gaasivarude operatiivne seis.")
        st.markdown("### EL ja Läti gaasihoidlad — GIE AGSI+")
        st.caption("AGSI+ avaldab eelmise gaasipäeva lõpu seisu iga päev. BalticPulse kontrollib uut kirjet iga 5 minuti järel; intraday hoidlanäit puudub.")
        if storage_df.empty:
            st.warning(storage_status.error or "AGSI+ andmeid ei kuvata, sest GIE API võti puudub või päring ebaõnnestus.")
            st.code('GIE_AGSI_API_KEY = "sinu-võti"', language="toml")
        else:
            sort_col = "gas_day" if "gas_day" in storage_df.columns else storage_df.columns[0]
            latest = storage_df.sort_values(sort_col).groupby("scope").tail(1)
            cols = st.columns(max(1, len(latest)))
            for col, (_, row) in zip(cols, latest.iterrows()):
                with col:
                    fill = row.get("full")
                    stored = row.get("gasInStorage")
                    st.metric(f"{row.get('scope')} täituvus", f"{fill:.1f}%" if pd.notna(fill) else "—")
                    details = []
                    if pd.notna(stored):
                        details.append(f"Laos {stored:.1f} TWh")
                    if pd.notna(row.get("workingGasVolume")):
                        details.append(f"töömaht {row.get('workingGasVolume'):.1f} TWh")
                    if pd.notna(row.get("gas_day")):
                        details.append(f"gaasipäev {pd.Timestamp(row.get('gas_day')).date()}")
                    st.caption(" · ".join(details))

            if "gas_day" in storage_df.columns and "full" in storage_df.columns:
                chart = storage_df.dropna(subset=["gas_day", "full"]).copy().sort_values("gas_day")
                # Keep only a useful recent window even if AGSI returns more rows.
                latest_day = chart["gas_day"].max()
                chart = chart[chart["gas_day"] >= latest_day - pd.Timedelta(days=30)]
                if not chart.empty:
                    fig = px.line(chart, x="gas_day", y="full", color="scope", markers=True,
                                  labels={"gas_day": "Gaasipäev", "full": "Täituvus %", "scope": "Piirkond"})
                    st.plotly_chart(fig, use_container_width=True)
                    source_link("gie_agsi", note="storage history")

            source_badge("GIE AGSI+", storage_status.ok,
                         f"Kontrollitud {pd.to_datetime(storage_status.fetched_at, utc=True).tz_convert(TALLINN):%d.%m.%Y %H:%M} · "
                         f"{storage_status.error or storage_status.note or 'andmed saadaval'}")

    with tab_fundamentals:
        st.markdown("## 📈 Turu fundamentaalid")
        st.caption("Gaas, futuurid, Brent ja EUA — spot- ja forward-vaade ühes plokis.")
        st.markdown("### Turu põhifundamentaalid")

        st.markdown("---")
        st.markdown("### 🔥 Gaasiturg")
        st.caption("Spot-indeksid, ajalooline hinnapilt ja TTF forward curve.")
        st.markdown("#### Gaasihinnad — EEX Neutral Gas Price")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("🇳🇱 TTF NGP", f"{ngp_current['TTF']:.1f} €/MWh" if ngp_current.get("TTF") is not None else "—")
        c2.metric("🇪🇪🇱🇻 LVA-EST NGP", f"{ngp_current['LVA-EST']:.1f} €/MWh" if ngp_current.get("LVA-EST") is not None else "—")
        c3.metric("🇫🇮 FIN NGP", f"{ngp_current['FIN']:.1f} €/MWh" if ngp_current.get("FIN") is not None else "—")
        c4.metric("🇱🇹 LTU NGP", f"{ngp_current['LTU']:.1f} €/MWh" if ngp_current.get("LTU") is not None else "—")


        st.markdown("#### 🔥 TTF forward-vaade")
        st.caption(
            "ICE Endex Dutch TTF Natural Gas Futures (vähemalt 15 min viitega). "
            "M+1 / Q+1 / Y+1 on futuurid, mitte prognoos tulevasest spot-hinnast."
        )
        gkey = _future_key_rows(futures_snapshot, "gas")
        gcurve = _future_curve_rows(futures_snapshot, "gas")
        _render_futures_market_state(_futures_market_state("gas", futures_snapshot))
        gmeta = futures_snapshot.get("gas", {}) if futures_snapshot else {}
        if gkey.empty:
            _g_updated, _g_errors = _futures_diag(futures_snapshot, "gas")
            st.warning(
                f"TTF futuuride snapshotis pole hinnaseeriat. Snapshot: {_g_updated}. "
                "Allikas: ICE Endex delayed market data."
            )
            if _g_errors:
                with st.expander("TTF futuuride diagnostika", expanded=True):
                    for err in _g_errors:
                        st.write(f"• {err}")
        else:
            st.dataframe(
                gkey,
                hide_index=True,
                use_container_width=True,
                column_config={
                    "last": st.column_config.NumberColumn("Viimane €/MWh", format="%.3f"),
                    "volume": st.column_config.NumberColumn("Volume", format="%.0f"),
                },
            )
            source_links(("ice_ttf", "delayed futures data"), ("futures_snapshot", "GitHub Actions snapshot"))
            if not gcurve.empty:
                gc = gcurve.copy()
                gc["delivery_start"] = pd.to_datetime(gc["delivery_start"], errors="coerce")
                gc = gc.dropna(subset=["delivery_start", "last"]).sort_values("delivery_start")
                if not gc.empty:
                    fig = px.line(
                        gc, x="delivery_start", y="last", markers=True,
                        hover_data=[c for c in ["contract", "volume", "time"] if c in gc.columns],
                        labels={"delivery_start":"Tarneperioodi algus", "last":"€/MWh"},
                        title="Dutch TTF forward curve — ICE Endex",
                    )
                    st.plotly_chart(fig, use_container_width=True)
                    source_links(("ice_ttf", "forward curve"), ("futures_snapshot", "GitHub Actions snapshot"))
            st.caption("Allikas: ICE Endex Dutch TTF Natural Gas Futures; turuandmed on ICE järgi vähemalt 15 min viitega.")

        gas_period = st.segmented_control(
            "Gaasiajaloo periood",
            ["1 nädal", "1 kuu", "1 aasta", "5 aastat"],
            default="1 kuu",
            key="gas_history_period",
        )
        gas_areas = st.multiselect(
            "Gaasipiirkonnad",
            ["TTF", "LVA-EST", "FIN", "LTU"],
            default=["TTF", "LVA-EST", "FIN", "LTU"],
            key="gas_history_areas",
        )

        with st.spinner("Laadin EEX NGP ajaloo (juba laaditud andmed avanevad vahemälust)..."):
            gas_hist = load_gas_ngp_history(today.isoformat())

        if gas_hist:
            cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=_history_days(gas_period))
            frames, coverage = [], []

            for area in gas_areas:
                df, status = gas_hist.get(area, (pd.DataFrame(), None))
                if df.empty:
                    continue
                x = df.copy()
                x["date"] = pd.to_datetime(x["date"], errors="coerce")
                coverage.append({
                    "Piirkond": area,
                    "Andmed alates": x["date"].min(),
                    "Andmed kuni": x["date"].max(),
                })
                x = x[x["date"] >= cutoff]
                x["Piirkond"] = area
                frames.append(x)

            if frames:
                gh = pd.concat(frames, ignore_index=True)
                gh["date"] = pd.to_datetime(gh["date"], errors="coerce")
                gh["price_eur_mwh"] = pd.to_numeric(gh["price_eur_mwh"], errors="coerce")
                gh = gh.dropna(subset=["date", "price_eur_mwh", "Piirkond"])

                fig = go.Figure()
                for area in gas_areas:
                    g = gh[gh["Piirkond"] == area].copy().sort_values("date")
                    if g.empty:
                        continue

                    g = (
                        g.groupby("date", as_index=False)["price_eur_mwh"]
                        .last()
                        .sort_values("date")
                    )

                    # Insert missing calendar days as NaN so Plotly never draws
                    # a false diagonal segment across gaps.
                    full_days = pd.date_range(
                        g["date"].min().normalize(),
                        g["date"].max().normalize(),
                        freq="D",
                    )
                    g = (
                        g.set_index("date")
                        .reindex(full_days)
                        .rename_axis("date")
                        .reset_index()
                    )

                    fig.add_trace(
                        go.Scatter(
                            x=g["date"],
                            y=g["price_eur_mwh"],
                            mode="lines+markers",
                            name=area,
                            connectgaps=False,
                            hovertemplate=(
                                f"{area}<br>%{{x|%d.%m.%Y}}"
                                "<br>%{y:.2f} €/MWh<extra></extra>"
                            ),
                        )
                    )

                fig.update_layout(
                    xaxis_title="Gaasipäev",
                    yaxis_title="€/MWh",
                    legend_title="Piirkond",
                    hovermode="x unified",
                )
                st.plotly_chart(fig, use_container_width=True)
                source_link("eex_ngp", note="official NGP history")

            if gas_period in ("1 aasta", "5 aastat"):
                st.warning(
                    "EEX ametlik tasuta avalik NGP ajaloo fail katab 60 päeva. "
                    "BalticPulse ei täida 1 aasta ega 5 aasta puuduvat osa kontrollimata hinnaseeriaga."
                )

            if coverage:
                st.dataframe(pd.DataFrame(coverage), hide_index=True, use_container_width=True)
                source_link("eex_ngp", note="actual public-history coverage")

        st.markdown("#### Gaasi kuuhinnad")
        st.caption(
            "NGP on päevane lõplik spot-indeks. Seetõttu näidatakse kuumiinimumi ja -maksimumi juures "
            "kuupäeva, mitte kellaaega. Avalik EEX ajalugu katab 60 päeva."
        )

        if st.button("Koosta gaasi kuustatistika", key="load_gas_monthly"):
            with st.spinner("Koostan EEX NGP kuustatistika..."):
                if not gas_hist:
                    gas_hist = load_gas_ngp_history(today.isoformat())
                    st.session_state["gas_ngp_history"] = gas_hist
                st.session_state["gas_monthly_table"] = gas_monthly_summary(gas_hist, 12)

        monthly_gas = st.session_state.get("gas_monthly_table", pd.DataFrame())
        if not monthly_gas.empty:
            st.dataframe(
                monthly_gas,
                hide_index=True,
                use_container_width=True,
                column_config={
                    "Keskmine €/MWh": st.column_config.NumberColumn(format="%.2f"),
                    "Min €/MWh": st.column_config.NumberColumn(format="%.2f"),
                    "Max €/MWh": st.column_config.NumberColumn(format="%.2f"),
                },
            )
            source_link("eex_ngp", note="monthly NGP statistics")

        st.divider()
        st.markdown("---")
        st.markdown("### 🛢️ Toorained ja CO₂")
        st.markdown("#### Muud fundamentaalhinnad")
        l, r = st.columns(2)

        with l:
            st.metric("🌍 Brent spot (EIA päevahind)", f"{brent_latest:.1f} $/bbl" if brent_latest is not None else "—")
            st.caption(f"Viimane vaatlus: {pd.Timestamp(_brent_obs).strftime('%d.%m.%Y') if pd.notna(_brent_obs) else 'puudub'}. "
                       + ("" if brent_latest is not None else "Värsket päevahinda ei ole; allolevat ajaloolist graafikut ei kuvata."))
            if brent_latest is not None and not brent_df.empty:
                fig = px.line(
                    brent_df.tail(180), x="date", y="price_usd_bbl",
                    labels={"date":"Kuupäev","price_usd_bbl":"$/bbl"},
                    title="Europe Brent Spot Price FOB — EIA",
                )
                st.plotly_chart(fig, use_container_width=True)
                source_link("eia_brent")
            else:
                st.warning(brent_status.error or "Värsket EIA Brent spot-hinda pole saadaval. ICE futuur on allpool eraldi.")

            st.markdown("#### 🛢️ ICE Brent futuurid")
            _bkey = _future_key_rows(futures_snapshot, "brent")
            _bcurve = _future_curve_rows(futures_snapshot, "brent")
            _render_futures_market_state(_futures_market_state("brent", futures_snapshot))
            _bmeta = futures_snapshot.get("brent", {}) if futures_snapshot else {}
            if _bkey.empty:
                _b_updated, _b_errors = _futures_diag(futures_snapshot, "brent")
                st.warning(
                    f"Brent futuuride snapshotis pole hinnaseeriat. Snapshot: {_b_updated}. "
                    "Allikas: ICE Futures Europe delayed market data."
                )
                if _b_errors:
                    with st.expander("Brent futuuride diagnostika", expanded=True):
                        for err in _b_errors:
                            st.write(f"• {err}")
            else:
                st.dataframe(
                    _bkey, hide_index=True, use_container_width=True,
                    column_config={
                        "last": st.column_config.NumberColumn("Viimane $/bbl", format="%.2f"),
                        "volume": st.column_config.NumberColumn("Volume", format="%.0f"),
                    },
                )
                source_links(("ice_brent", "delayed futures data"), ("futures_snapshot", "GitHub Actions snapshot"))
                if not _bcurve.empty:
                    _bc = _bcurve.copy()
                    _bc["delivery_start"] = pd.to_datetime(_bc["delivery_start"], errors="coerce")
                    _bc = _bc.dropna(subset=["delivery_start", "last"]).sort_values("delivery_start")
                    if not _bc.empty:
                        _figb = px.line(
                            _bc, x="delivery_start", y="last", markers=True,
                            hover_data=[c for c in ["contract", "volume", "time"] if c in _bc.columns],
                            labels={"delivery_start":"Tarnekuu", "last":"$/bbl"},
                            title="ICE Brent Crude Futures forward curve",
                        )
                        st.plotly_chart(_figb, use_container_width=True)
                        source_links(("ice_brent", "forward curve"), ("futures_snapshot", "GitHub Actions snapshot"))
                st.caption("Allikas: ICE Futures Europe Brent Crude Futures; turuandmed on viitega.")

        with r:
            st.metric("EUA oksjon", f"{eua_latest:.2f} €/tCO₂" if eua_latest is not None else "—")
            if not eua_df.empty:
                fig = px.line(
                    eua_df, x="date", y="price_eur_tco2", markers=True,
                    labels={"date":"Oksjonipäev","price_eur_tco2":"€/tCO₂"},
                    title="EUA primaaroksjoni clearing price — EEX",
                )
                st.plotly_chart(fig, use_container_width=True)
                source_link("eex_eua", note="primary auction clearing price")
            else:
                st.warning(eua_status.error or "EEX EUA oksjoniandmed pole saadaval.")

        st.info(
            "Metoodika: EEX NGP on spot-turu päevane indeks, mitte TTF front-month futuur. "
            "Pikemat gaasiajalugu ei konstrueerita avaliku allika 60 päeva piirist väljapoole."
        )


    with tab_quality:
        st.markdown("### Andmekvaliteedi ja ulatuse reeglid")
        st.markdown(
            """
    - **Ei kasutata sünteetilisi varuväärtusi.** API tõrke korral näidatakse puuduvat väärtust või veateadet.
    - **Päev-ette hind ≠ lõpptarbija hind.** Maksud, võrgutasud ja müüja marginaal ei kuulu börsihinna sisse.
    - **Värskus on osa andmekvaliteedist.** Spot-hind seotakse täpselt käimasoleva MTU-ga; Eleringi tootmise ja tarbimise põhiväärtust lubatakse kuni 15 min vanusena; vanem väärtus ei ole “praegu” ja seda ei asendata ENTSO-E-ga. ENTSO-E tootmisliikide ning piiriüleste voogude väärtused on eraldi allikad ja nende värskus kuvatakse eraldi.
    - **ENTSO-E ristkontroll:** Balti tootmisjaotus (A75) ja EE–FI/EE–LV füüsilised vood (A11) pärinevad Transparency Platformist. Eleringi kogutootmist ja ENTSO-E tootmisliike ei sunnita kunstlikult võrdseks, sest avaldamisajad ja metoodika võivad erineda.
    - **Gaasihoidlad on päevased.** AGSI+ viimane kirje kajastab gaasipäeva, mitte hetke intraday taset.
    - **Fundamentaalid:** TTF/LVA-EST/FIN/LTU NGP = EEX current files (15-min refresh); TTF 60 päeva final history on eraldi ajaloo jaoks. Brent = U.S. EIA päevane Europe Brent Spot Price FOB; EUA = EEX primaaroksjoni clearing price. Päevaseid/event-põhiseid instrumente ei nimetata intraday reaalajaks.
    - **Fallback:** kui Eleringi tootmine/tarbimine puudub, kasutatakse ainult ENTSO-E A75/A65 tegelikke vaatlusi; sünteetilist varuväärtust ei looda.
            """
        )

    st.divider()
    st.caption("BalticPulse · Allikad: Elering, ENTSO-E Transparency Platform, GIE AGSI+, EEX, U.S. EIA. Põhimõte: parem puuduv number kui kontrollimata number.")


render_dashboard()
