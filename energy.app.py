from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

import energy_sources as _energy_sources
from energy_sources import (
    fetch_elering_prices,
    fetch_elering_system,
    fetch_gas_storage,
    fetch_reserve_capacity,
    fetch_balancing_energy,
    fetch_entsoe_generation_by_type,
    fetch_entsoe_estonia_flows,
    fetch_entsoe_baltic_flows,
    fetch_entsoe_estonia_ntc,
    fetch_entsoe_actual_load,
    fetch_eex_ngp_current,
    fetch_eex_ttf_ngp,
    fetch_eia_brent,
    fetch_eex_eua_auction,
)
from umm_client import fetch_umm_messages
from energy_news import fetch_energy_news

APP_BUILD_VERSION = "15.7.0"

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


@st.cache_data(ttl=60)
def load_short_prices():
    now = datetime.now(timezone.utc)
    return fetch_elering_prices(now - timedelta(days=1), now + timedelta(days=2))


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
def load_reserves(region: str):
    return fetch_reserve_capacity(region)


@st.cache_data(ttl=300)
def load_balancing_energy(region: str):
    return fetch_balancing_energy(region)




@st.cache_data(ttl=1800)
def load_reserve_ytd_file():
    path = Path(__file__).resolve().parent / "data" / "reserve_capacity_ytd.csv"
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], errors="coerce")
        return df
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=30)
def load_umm():
    # Public Nord Pool REST API is polled every dashboard refresh; 30 s cache keeps it near-live.
    return fetch_umm_messages(limit=500, max_pages=2, retries=3)


@st.cache_data(ttl=300)
def load_energy_news():
    return fetch_energy_news(max_items=30)


@st.cache_data(ttl=1800)
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


@st.cache_data(ttl=900)
def load_entsoe_ntc(key: str):
    now = datetime.now(timezone.utc)
    return fetch_entsoe_estonia_ntc(key, now - timedelta(hours=6), now + timedelta(days=2))


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


@st.cache_data(ttl=21600)
def load_brent():
    return fetch_eia_brent()


@st.cache_data(ttl=3600)
def load_eua():
    return fetch_eex_eua_auction()


@st.fragment(run_every=60)
def render_dashboard():
    # Header
    c1, c2 = st.columns([4, 1])
    with c1:
        st.title("⚡ BalticPulse")
        st.caption(f"Build {APP_BUILD_VERSION} • UMM live REST • energiauudised")
        st.caption("Balti ja Põhjamaade energiaturu reaalaja olukorrapilt — elekter, võrk, reservid, UMM-id, gaas ja põhifundamentaalid.")
    with c2:
        st.write("")
        if st.button("🔄 Värskenda", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    now_local = datetime.now(TALLINN)
    today = now_local.date()
    tomorrow = today + timedelta(days=1)
    st.caption(f"Vaate aeg: **{now_local:%d.%m.%Y %H:%M:%S}** Europe/Tallinn · automaatne värskendus iga 1 min (Streamliti native fragment)")

    with st.spinner("Laadin operatiivandmeid..."):
        entsoe_key = secret("ENTSOE_API_KEY")
        agsi_key = secret("GIE_AGSI_API_KEY")
        # Independent sources are fetched concurrently so a slow daily/fundamental source does not
        # hold the operational view hostage. Individual loaders still retain their own cache TTLs.
        with ThreadPoolExecutor(max_workers=12) as pool:
            fut_prices = pool.submit(load_short_prices)
            fut_system = pool.submit(load_system)
            fut_baltic_snapshot = pool.submit(load_baltic_system_snapshot)
            fut_umm = pool.submit(load_umm)
            fut_news = pool.submit(load_energy_news)
            fut_storage = pool.submit(load_storage, agsi_key)
            gen_futs = {r: pool.submit(load_entsoe_generation, entsoe_key, r) for r in BALTICS}
            load_futs = {r: pool.submit(load_entsoe_load, entsoe_key, r) for r in BALTICS}
            fut_flows = pool.submit(load_entsoe_flows, entsoe_key)
            fut_baltic_flows = pool.submit(load_entsoe_baltic_flows, entsoe_key)
            fut_ntc = pool.submit(load_entsoe_ntc, entsoe_key)
            fut_ttf_hist = pool.submit(load_ttf)
            fut_brent = pool.submit(load_brent)
            fut_eua = pool.submit(load_eua)
            reserve_futs = {r: pool.submit(load_reserves, r) for r in BALTICS}
            energy_futs = {r: pool.submit(load_balancing_energy, r) for r in BALTICS}
            ngp_futs = {a: pool.submit(load_ngp_current, a) for a in ["TTF", "LVA-EST", "FIN", "LTU"]}

            prices, price_status = fut_prices.result()
            system_df, system_status = fut_system.result()
            baltic_system_snapshot, baltic_snapshot_status = fut_baltic_snapshot.result()
            umm_rows, umm_meta = fut_umm.result()
            news_rows, news_meta = fut_news.result()
            storage_df, storage_status = fut_storage.result()
            entsoe_generation_results = {r: f.result() for r, f in gen_futs.items()}
            entsoe_load_results = {r: f.result() for r, f in load_futs.items()}
            entsoe_generation, entsoe_generation_status = entsoe_generation_results["EE"]
            entsoe_load, entsoe_load_status = entsoe_load_results["EE"]
            entsoe_flows, entsoe_flow_statuses = fut_flows.result()
            entsoe_baltic_flows, entsoe_baltic_flow_statuses = fut_baltic_flows.result()
            entsoe_ntc, entsoe_ntc_statuses = fut_ntc.result()
            ttf_df, ttf_status = fut_ttf_hist.result()
            brent_df, brent_status = fut_brent.result()
            eua_df, eua_status = fut_eua.result()
            reserve_results = {r: f.result() for r, f in reserve_futs.items()}
            reserve_ytd = load_reserve_ytd_file()
            balancing_energy_results = {r: f.result() for r, f in energy_futs.items()}
            ngp_current_results = {a: f.result() for a, f in ngp_futs.items()}

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

    last_sys = pd.DataFrame()
    if not system_df.empty:
        val_cols = [c for c in ["production_mw", "consumption_mw"] if c in system_df.columns]
        if val_cols:
            last_sys = system_df.dropna(subset=val_cols, how="all").tail(1)
    elering_sys_time = last_sys["time_utc"].iloc[0] if not last_sys.empty and "time_utc" in last_sys else None
    # Always show the latest ACTUAL Elering observation returned by the API.
    # Freshness is metadata, not a reason to hide a valid Elering value. No fallback is used.
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

    umm_df = pd.DataFrame(umm_rows)
    active_umm = pd.DataFrame()
    if not umm_df.empty:
        for c in ["event_start", "event_end", "publication_time"]:
            if c in umm_df.columns:
                umm_df[c] = pd.to_datetime(umm_df[c], utc=True, errors="coerce")
        # Keep only the latest published revision per message ID for operational status.
        if "message_id" in umm_df.columns and "publication_time" in umm_df.columns:
            with_id = umm_df[umm_df["message_id"].astype(str).str.len() > 0].sort_values("publication_time").groupby("message_id", as_index=False).tail(1)
            without_id = umm_df[umm_df["message_id"].astype(str).str.len() == 0]
            umm_df = pd.concat([with_id, without_id], ignore_index=True)
        now_utc_ts = pd.Timestamp.now(tz="UTC")
        starts = umm_df.get("event_start", pd.Series(pd.NaT, index=umm_df.index, dtype="datetime64[ns, UTC]"))
        ends = umm_df.get("event_end", pd.Series(pd.NaT, index=umm_df.index, dtype="datetime64[ns, UTC]"))
        active_mask = (starts.isna() | (starts <= now_utc_ts)) & (ends.isna() | (ends >= now_utc_ts))
        if "is_outdated" in umm_df.columns:
            active_mask &= ~umm_df["is_outdated"].fillna(False).astype(bool)
        active_umm = umm_df[active_mask].copy()
        if "affected_capacity" in active_umm.columns:
            active_umm["affected_capacity"] = pd.to_numeric(active_umm["affected_capacity"], errors="coerce")

    newest_umm_time = None
    umm_age_minutes = None
    if not umm_df.empty and "publication_time" in umm_df.columns:
        _pub = umm_df["publication_time"].dropna()
        if not _pub.empty:
            newest_umm_time = _pub.max()
            umm_age_minutes = max(0.0, (pd.Timestamp.now(tz="UTC") - newest_umm_time).total_seconds() / 60.0)

    largest_umm = None
    if not active_umm.empty and "affected_capacity" in active_umm.columns:
        cap = active_umm.dropna(subset=["affected_capacity"])
        cap = cap[cap["affected_capacity"] > 0]
        if not cap.empty:
            largest_umm = float(cap["affected_capacity"].max())

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

    # Directional day-ahead transfer capacity matching the current local market interval.
    latest_ntc: dict[str, dict[str, float | None]] = {
        "EE–FI": {"EE→FI": None, "FI→EE": None},
        "EE–LV": {"EE→LV": None, "LV→EE": None},
    }
    if not entsoe_ntc.empty:
        ntc_now = pd.Timestamp(now_local)
        for border, directions in latest_ntc.items():
            for direction in directions:
                x = entsoe_ntc[(entsoe_ntc["border"] == border) & (entsoe_ntc["direction"] == direction)].copy()
                if not x.empty:
                    before = x[x["time_local"] <= ntc_now].sort_values("time_local")
                    row = before.tail(1) if not before.empty else x.sort_values("time_local").head(1)
                    if not row.empty:
                        latest_ntc[border][direction] = float(pd.to_numeric(row.iloc[0]["ntc_mw"], errors="coerce"))

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
        if used: out["source"] = "ENTSO-E GitHub snapshot"
        return out

    baltic_snapshots = {r: make_snapshot(r) for r in BALTICS}

    # Estonia total production/consumption remain strictly Elering actual.
    baltic_snapshots["EE"]["production_mw"] = float(prod) if prod is not None else None
    baltic_snapshots["EE"]["consumption_mw"] = float(cons) if cons is not None else None
    baltic_snapshots["EE"]["generation_time"] = pd.Timestamp(prod_time) if prod_time is not None else baltic_snapshots["EE"]["generation_time"]
    baltic_snapshots["EE"]["load_time"] = pd.Timestamp(cons_time) if cons_time is not None else baltic_snapshots["EE"]["load_time"]
    baltic_snapshots["EE"]["source"] = "Elering actual + ENTSO-E renewables"
    if not system_df.empty:
        sx = system_df.copy()
        sx["time_utc"] = pd.to_datetime(sx["time_utc"], utc=True, errors="coerce")
        if prod_time is not None:
            baltic_snapshots["EE"]["previous_24h"]["production_mw"] = nearest(sx,"time_utc","production_mw",pd.Timestamp(prod_time).tz_convert("UTC")-pd.Timedelta(hours=24))
        if cons_time is not None:
            baltic_snapshots["EE"]["previous_24h"]["consumption_mw"] = nearest(sx,"time_utc","consumption_mw",pd.Timestamp(cons_time).tz_convert("UTC")-pd.Timedelta(hours=24))

    # Renewable generation is shown separately from ENTSO-E A75 actual generation by type.
    # It never replaces the Elering total-production KPI.
    renewable_generation_mw = None
    renewable_share = None
    if not entsoe_generation.empty:
        gt = entsoe_generation.dropna(subset=["generation_mw"]).copy()
        if not gt.empty:
            latest_gt = gt["time_utc"].max()
            gx = gt[gt["time_utc"] == latest_gt].copy() if is_fresh(latest_gt, 120) else pd.DataFrame()
            total_gen = pd.to_numeric(gx["generation_mw"], errors="coerce").sum(min_count=1)
            ren_gen = pd.to_numeric(gx[gx["technology"].isin(renewable_names)]["generation_mw"], errors="coerce").sum(min_count=1)
            if pd.notna(ren_gen):
                renewable_generation_mw = float(ren_gen)
            if pd.notna(total_gen) and total_gen > 0 and pd.notna(ren_gen):
                renewable_share = float(100 * ren_gen / total_gen)

    # Latest official fundamental reference values. Current EEX NGP files are used for
    # near-real-time gas KPIs; the 60-day final TTF file remains for chart history only.
    ngp_current: dict[str, float | None] = {a: None for a in ["TTF", "LVA-EST", "FIN", "LTU"]}
    ngp_delivery: dict[str, object | None] = {a: None for a in ngp_current}
    for area, (df_ngp, _st) in ngp_current_results.items():
        if not df_ngp.empty:
            today_rows = df_ngp[df_ngp["delivery_date"] == today].dropna(subset=["price_eur_mwh"])
            row = today_rows.tail(1) if not today_rows.empty else df_ngp.dropna(subset=["price_eur_mwh"]).sort_values("delivery_date").head(1)
            if not row.empty:
                ngp_current[area] = float(row.iloc[0]["price_eur_mwh"])
                ngp_delivery[area] = row.iloc[0]["delivery_date"]
    ttf_latest = ngp_current["TTF"]
    ttf_date = ngp_delivery["TTF"]

    brent_latest = None
    brent_date = None
    if not brent_df.empty:
        x = brent_df.dropna(subset=["price_usd_bbl"]).sort_values("date")
        if not x.empty:
            brent_latest = float(x.iloc[-1]["price_usd_bbl"])
            brent_date = x.iloc[-1]["date"]

    eua_latest = None
    eua_date = None
    if not eua_df.empty:
        x = eua_df.dropna(subset=["price_eur_tco2"]).sort_values("date")
        if not x.empty:
            eua_latest = float(x.iloc[-1]["price_eur_tco2"])
            eua_date = x.iloc[-1]["date"]

    # ---------- 1. EXECUTIVE SNAPSHOT ----------
    st.subheader("Olukord praegu")
    st.caption("Sama operatiivne vaade kogu Baltikumile. Eesti kogutootmine/tarbimine: Elering actual. Läti ja Leedu: ENTSO-E A75/A65 actual. Taastuvtootmine: ENTSO-E A75.")
    country_meta = {"EE": ("🇪🇪", "Eesti"), "LV": ("🇱🇻", "Läti"), "LT": ("🇱🇹", "Leedu")}
    for region in BALTICS:
        flag, name = country_meta[region]
        snap = baltic_snapshots[region]
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
        if snap.get("source") == "ENTSO-E GitHub snapshot":
            st.info(f"{flag} {name}: kasutatakse viimast edukat ENTSO-E GitHub snapshot'i.")
        ages = [age_minutes(t) for t in [snap.get("generation_time"), snap.get("load_time")] if t is not None]
        if ages and max(ages) > 120:
            st.warning(f"{flag} {name}: viimane tegelik vaatlus on üle 2 tunni vana. Kuvatakse viimane edukas tegelik väärtus; sünteetilist täidet ei kasutata.")


    st.markdown("## 🇪🇪🇱🇻🇱🇹 Baltikumi võtmenäitajad")
    st.caption(
        "Tegelik tootmine, tarbimine, taastuvtootmine ja taastuvate osakaal. "
        "Delta = sama aeg 24 tundi tagasi. LV/LT: otse-ENTSO-E või viimane edukas GitHub snapshot."
    )
    _country = {"EE":("🇪🇪","Eesti"),"LV":("🇱🇻","Läti"),"LT":("🇱🇹","Leedu")}
    for _region in ["EE","LV","LT"]:
        _flag, _name = _country[_region]
        _snap = baltic_snapshots.get(_region, {}) or {}
        _prev = _snap.get("previous_24h", {}) or {}
        st.markdown(f"### {_flag} {_name}")
        _a,_b,_c,_d = st.columns(4)
        _pv = _snap.get("production_mw")
        _cv = _snap.get("consumption_mw")
        _rv = _snap.get("renewable_mw")
        _sv = _snap.get("renewable_share")
        _a.metric("Tootmine", f"{_pv:.0f} MW" if _pv is not None and pd.notna(_pv) else "Andmed puuduvad",
                  delta=delta_text(_pv,_prev.get("production_mw")))
        _b.metric("Tarbimine", f"{_cv:.0f} MW" if _cv is not None and pd.notna(_cv) else "Andmed puuduvad",
                  delta=delta_text(_cv,_prev.get("consumption_mw")))
        _c.metric("Taastuvtootmine", f"{_rv:.0f} MW" if _rv is not None and pd.notna(_rv) else "Andmed puuduvad",
                  delta=delta_text(_rv,_prev.get("renewable_mw")))
        _d.metric("Taastuvate osakaal", f"{_sv:.1f}%" if _sv is not None and pd.notna(_sv) else "Andmed puuduvad",
                  delta=delta_text(_sv,_prev.get("renewable_share")))
        if _snap.get("source") == "ENTSO-E GitHub snapshot":
            st.info(f"{_flag} {_name}: kasutatakse viimast edukat ENTSO-E snapshot'i.")
    st.divider()

    market_cols = st.columns(3)
    market_cols[0].metric("🇪🇪 EE spot — käimasolev MTU", f"{current_prices['EE']:.1f} €/MWh" if current_prices["EE"] is not None else "—")
    market_cols[1].metric("🇫🇮 FI spot — käimasolev MTU", f"{current_prices['FI']:.1f} €/MWh" if current_prices["FI"] is not None else "—")
    spread = None
    if current_prices["EE"] is not None and current_prices["FI"] is not None:
        spread = current_prices["EE"] - current_prices["FI"]
    market_cols[2].metric("EE–FI hinnavahe", f"{spread:+.1f} €/MWh" if spread is not None else "—")

    flow1, flow2 = st.columns(2)
    def flow_label(v):
        if v is None:
            return "—"
        return f"{abs(v):.0f} MW " + ("eksport" if v > 0 else "import" if v < 0 else "tasakaalus")
    flow1.metric("EE–FI füüsiline netovoog", flow_label(latest_border_flows["EE–FI"]), delta=(f"{fmt_age(latest_border_flow_time['EE–FI'])} vana" if latest_border_flow_time["EE–FI"] is not None else None), delta_color="off", help="ENTSO-E A11. Positiivne märk tähendab Eesti netoeksporti; negatiivne Eesti netoimporti.")
    flow2.metric("EE–LV füüsiline netovoog", flow_label(latest_border_flows["EE–LV"]), delta=(f"{fmt_age(latest_border_flow_time['EE–LV'])} vana" if latest_border_flow_time["EE–LV"] is not None else None), delta_color="off", help="ENTSO-E A11. Positiivne märk tähendab Eesti netoeksporti; negatiivne Eesti netoimporti.")

    st.markdown("#### Ühenduste võimsus ja kasutus")
    cap_rows = []
    for border, dirs in latest_ntc.items():
        flow = latest_border_flows.get(border)
        export_dir = "EE→FI" if border == "EE–FI" else "EE→LV"
        import_dir = "FI→EE" if border == "EE–FI" else "LV→EE"
        relevant_dir = export_dir if (flow is not None and flow >= 0) else import_dir
        cap = dirs.get(relevant_dir)
        utilisation = (abs(flow) / cap * 100) if flow is not None and cap and cap > 0 else None
        cap_rows.append({
            "Piir": border,
            "Füüsiline netovoog MW": abs(flow) if flow is not None else None,
            "Voo suund": "EE eksport" if flow is not None and flow > 0 else "EE import" if flow is not None and flow < 0 else "—",
            "Vastava suuna päev-ette NTC MW": cap,
            "Voog / NTC %": utilisation,
            "EE ekspordi NTC MW": dirs.get(export_dir),
            "EE impordi NTC MW": dirs.get(import_dir),
        })
    st.dataframe(pd.DataFrame(cap_rows), hide_index=True, use_container_width=True, column_config={
        "Füüsiline netovoog MW": st.column_config.NumberColumn(format="%.0f"),
        "Vastava suuna päev-ette NTC MW": st.column_config.NumberColumn(format="%.0f"),
        "Voog / NTC %": st.column_config.NumberColumn(format="%.1f%%"),
        "EE ekspordi NTC MW": st.column_config.NumberColumn(format="%.0f"),
        "EE impordi NTC MW": st.column_config.NumberColumn(format="%.0f"),
    })
    st.caption("ENTSO-E A61 päev-ette NTC on prognoositud suunaline ülekandevõimsus. 'Voog / NTC' on kontekstinäitaja, mitte vaba ülekandevõimsuse arvutus; A11 füüsiline voog ja A61 NTC on eri publikatsioonid ning võivad olla eri ajatempliga.")

    st.markdown("#### Turu põhifundamentaalid")
    f1, f2, f3, f4 = st.columns(4)
    f1.metric("🇳🇱 TTF NGP — D", f"{ngp_current['TTF']:.1f} €/MWh" if ngp_current["TTF"] is not None else "—", help="EEX current NGP; EEX uuendab faili iga 15 minuti järel D/D+1/D+2 jaoks.")
    f2.metric("🇪🇪🇱🇻 Eesti–Läti gaas (LVA–EST) — D", f"{ngp_current['LVA-EST']:.1f} €/MWh" if ngp_current["LVA-EST"] is not None else "—", help="EEX LVA-EST Neutral Gas Price, current gas day.")
    f3.metric("🇫🇮 Soome gaas (FIN NGP) — D", f"{ngp_current['FIN']:.1f} €/MWh" if ngp_current["FIN"] is not None else "—", help="EEX FIN Neutral Gas Price, current gas day.")
    f4.metric("🇱🇹 Leedu gaas (LTU NGP) — D", f"{ngp_current['LTU']:.1f} €/MWh" if ngp_current["LTU"] is not None else "—", help="EEX LTU Neutral Gas Price, current gas day.")
    f5, f6 = st.columns(2)
    f5.metric("🌍 Brent — EIA spot (päevane)", f"{brent_latest:.1f} $/bbl" if brent_latest is not None else "—", help="Ametlik EIA päevane Europe Brent Spot Price FOB. See ei ole intraday reaalaja hind.")
    f6.metric("EUA — EEX oksjon", f"{eua_latest:.2f} €/tCO₂" if eua_latest is not None else "—", help="EEX EUA primaaroksjoni viimane clearing price. See ei ole secondary-market intraday hind.")
    st.caption("Operatiivne gaas: EEX NGP TTF/LVA-EST/FIN/LTU current files (15-min refresh). Brent on EIA päevane ametlik spot-seeria ja EUA EEX primaaroksjoni hind — neid ei esitata intraday reaalajana.")



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

    for border, dirs in latest_ntc.items():
        flow = latest_border_flows.get(border)
        if flow is None:
            continue
        export_dir = "EE→FI" if border == "EE–FI" else "EE→LV"
        import_dir = "FI→EE" if border == "EE–FI" else "LV→EE"
        relevant_dir = export_dir if flow >= 0 else import_dir
        cap = dirs.get(relevant_dir)
        if cap and cap > 0:
            util = abs(flow) / cap * 100
            if util >= 90:
                level = "🔴 Kõrge" if util >= 100 else "🟠 Tähelepanu"
                add_alert(level, f"{border} ühendus", f"A11 füüsiline voog on {util:.0f}% vastava suuna A61 päev-ette NTC-st (reegel: ≥ 90%).")

    # Balancing-energy stress indicator: latest non-null aFRR/mFRR clearing price in each dataset.
    latest_balancing_prices: list[tuple[str, str, str, float, pd.Timestamp]] = []
    for region, (edf, _) in balancing_energy_results.items():
        if edf.empty:
            continue
        for (product, direction), grp in edf.dropna(subset=["price_eur_mwh"]).groupby(["product", "direction"]):
            g = grp.sort_values("time_utc")
            if not g.empty:
                row = g.iloc[-1]
                latest_balancing_prices.append((region, product, direction, float(row["price_eur_mwh"]), pd.Timestamp(row["time_utc"])))
    for region, product, direction, price, ts in latest_balancing_prices:
        if not is_fresh(ts, 120):
            continue
        if abs(price) >= 500:
            level = "🔴 Kõrge" if abs(price) >= 1000 else "🟠 Tähelepanu"
            add_alert(level, "Balancing energy", f"{region} {product} {direction}: {price:.0f} €/MWh ({fmt_age(ts)} vana; reegel: |hind| ≥ 500 €/MWh).")

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
    else:
        st.success("Ükski seadistatud operatiivne tähelepanureegel ei ole praegu käivitunud.")
    st.caption("Tähelepanureeglid on läbipaistvad heuristikad olukorrapildi kiirendamiseks, mitte ametlikud häirepiirid ega prognoosid. Lävendid: |EE–FI spread| 50/100 €/MWh; voog/DA NTC 90/100%; balancing energy |500/1000| €/MWh; gaasihoidlad <30% või ~7 päeva langus ≥5 pp.")

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

    # Freshness & source health is a first-class part of the dashboard.
    with st.expander("🔌 Andmeallikate staatus — roheline / kollane / punane", expanded=False):
        price_age_min = newest_age_minutes(prices, "time_utc")
        sys_age_min = age_minutes(sys_time)
        storage_age_min = newest_age_minutes(storage_df, "gasDayStart", "date")

        left_status, right_status = st.columns(2)

        with left_status:
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

            umm_level = "error" if umm_meta.error else ("warning" if not umm_rows else "ok")
            source_badge(
                "Nord Pool UMM",
                detail=umm_meta.error or f"HTTP {umm_meta.status_code or '—'} · {len(umm_rows)} teadet",
                level=umm_level,
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
                    age_text=(fmt_age(brent_df["Date"].max()) if not brent_df.empty and "Date" in brent_df else None),
                ),
                level=status_level(
                    brent_status,
                    has_data=not brent_df.empty,
                    age_min=newest_age_minutes(brent_df, "Date"),
                    warn_after_min=7 * 24 * 60,
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

            for region, (rdf, statuses) in reserve_results.items():
                ok_count = sum(1 for stx in statuses if stx.ok)
                level = "error" if ok_count == 0 else ("warning" if ok_count < len(statuses) or rdf.empty else "ok")
                errors = [stx.error or stx.note for stx in statuses if (stx.error or stx.note)]
                source_badge(
                    f"BTD reservvõimsus {region}",
                    detail=f"{ok_count}/{len(statuses)} voogu OK" + (f" · {' | '.join(errors[:2])}" if errors else ""),
                    level=level,
                )

            for region, (edf, statuses) in balancing_energy_results.items():
                ok_count = sum(1 for stx in statuses if stx.ok)
                level = "error" if ok_count == 0 else ("warning" if ok_count < len(statuses) or edf.empty else "ok")
                errors = [stx.error or stx.note for stx in statuses if (stx.error or stx.note)]
                source_badge(
                    f"Balancing energy {region}",
                    detail=f"{ok_count}/{len(statuses)} voogu OK" + (f" · {' | '.join(errors[:2])}" if errors else ""),
                    level=level,
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

            ntc_ok = sum(1 for stx in entsoe_ntc_statuses if stx.ok)
            ntc_level = "error" if ntc_ok == 0 else ("warning" if ntc_ok < len(entsoe_ntc_statuses) or entsoe_ntc.empty else "ok")
            source_badge(
                "ENTSO-E päev-ette NTC",
                detail=f"{ntc_ok}/{len(entsoe_ntc_statuses)} päringut OK",
                level=ntc_level,
            )

    # ---------- 2. DETAIL TABS ----------
    tab_overview, tab_prices, tab_system, tab_entsoe, tab_umm, tab_news, tab_reserves, tab_gas, tab_fundamentals, tab_quality = st.tabs([
        "📌 Põhivaade", "⚡ Elektrihinnad", "🏭 Baltikumi süsteem", "🌐 ENTSO-E", "📣 UMM", "🌍 Energiauudised", "🔄 Reservid", "🔥 Gaasihoidlad", "📈 Fundamentaalid", "✅ Andmekvaliteet"
    ])

    with tab_overview:
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
            source_badge("Elering Dashboard / Nord Pool", price_status.ok)
        with right:
            st.markdown("### 🇪🇪🇱🇻🇱🇹 Baltikumi süsteemi hetkeseis")
            rows = []
            for region in BALTICS:
                snap = baltic_snapshots[region]
                rows.append({"Riik": country_meta[region][0] + " " + region, "Tootmine MW": snap["production_mw"], "Tarbimine MW": snap["consumption_mw"], "Taastuv MW": snap["renewable_mw"], "Taastuv %": snap["renewable_share"]})
            st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True, column_config={"Tootmine MW": st.column_config.NumberColumn(format="%.0f"), "Tarbimine MW": st.column_config.NumberColumn(format="%.0f"), "Taastuv MW": st.column_config.NumberColumn(format="%.0f"), "Taastuv %": st.column_config.NumberColumn(format="%.1f%%")})
            st.caption("EE kogunäidud: Elering actual. LV/LT: ENTSO-E actual. Taastuvtootmine: ENTSO-E A75.")

        st.markdown("### Balti reservituru hinnapilt — jooksva aasta areng")
        if not reserve_ytd.empty:
            ytd = reserve_ytd.copy()
            ytd = ytd[ytd["date"].dt.year == datetime.now(TALLINN).year]
            if not ytd.empty:
                monthly = (ytd.groupby([pd.Grouper(key="date", freq="MS"), "region", "product", "direction"], as_index=False)["price_eur_mw_h"].mean())
                fig_r = px.line(monthly, x="date", y="price_eur_mw_h", color="region", line_dash="product", facet_row="direction", markers=True,
                                labels={"date":"Kuu", "price_eur_mw_h":"Keskmine €/MW/h", "region":"Piirkond", "product":"Toode"})
                fig_r.update_layout(title=f"BBCM reservvõimsuse kuukeskmised {datetime.now(TALLINN).year}")
                st.plotly_chart(fig_r, use_container_width=True)
                latest_month = monthly["date"].max()
                snap = monthly[monthly["date"] == latest_month].copy()
                st.dataframe(snap, hide_index=True, use_container_width=True, column_config={"price_eur_mw_h": st.column_config.NumberColumn("Keskmine €/MW/h", format="%.1f")})
                st.caption("Allikas: Baltic Transparency Dashboard via Volton Public Data. Jooksva aasta päevaarhiiv agregeeritakse päevakeskmisteks ja kuvatakse kuukeskmisena; andmed uuenevad GitHub Actionsi kaudu iga päev.")
            else:
                st.info("Jooksva aasta reserviajalugu pole veel YTD failis saadaval.")
        else:
            st.info("Jooksva aasta reserviajaloo fail pole veel loodud. Kuni GitHub Actionsi esimese backfill'ini vaata detailvaates viimase 7 päeva live-andmeid.")

    with tab_prices:
        st.markdown("### Regionaalsed päeva-ette hinnad")
        if prices.empty:
            st.error(price_status.error or "Andmed pole saadaval")
        else:
            selected = st.multiselect("Piirkonnad", REGIONS, default=REGIONS)
            p = prices[prices["region"].isin(selected)]
            fig = px.line(p, x="time_local", y="price", color="region", markers=False)
            fig.update_layout(yaxis_title="€/MWh", xaxis_title="Aeg (Europe/Tallinn)")
            st.plotly_chart(fig, use_container_width=True)
            summary = p.assign(day=p["time_local"].dt.date).groupby(["day", "region"])["price"].agg(["mean","min","max"]).reset_index()
            st.dataframe(summary, hide_index=True, use_container_width=True)
        st.caption("Eleringi avalik NPS API; hinnad on börsi päev-ette hinnad, mitte lõpptarbija hind.")

    with tab_system:
        st.markdown("### 🇪🇪🇱🇻🇱🇹 Baltikumi elektrisüsteem — tegelik tootmine, tarbimine ja taastuvad")
        st.caption("Eesti kogutootmine/tarbimine: Elering actual-only. Läti ja Leedu: ENTSO-E A75 actual generation ja A65 actual total load. Taastuvjaotus kõigis riikides ENTSO-E A75 järgi.")
        ee_tab, lv_tab, lt_tab = st.tabs(["🇪🇪 Eesti", "🇱🇻 Läti", "🇱🇹 Leedu"])
        for region, panel in [("EE", ee_tab), ("LV", lv_tab), ("LT", lt_tab)]:
            with panel:
                flag, name = country_meta[region]
                snap = baltic_snapshots[region]
                k = st.columns(4)
                prev = snap.get("previous_24h", {}) or {}
                k[0].metric("Tootmine", f"{snap['production_mw']:.0f} MW" if snap['production_mw'] is not None else "—", delta=delta_text(snap.get("production_mw"),prev.get("production_mw")))
                k[1].metric("Tarbimine", f"{snap['consumption_mw']:.0f} MW" if snap['consumption_mw'] is not None else "—", delta=delta_text(snap.get("consumption_mw"),prev.get("consumption_mw")))
                k[2].metric("Taastuvtootmine", f"{snap['renewable_mw']:.0f} MW" if snap['renewable_mw'] is not None else "—", delta=delta_text(snap.get("renewable_mw"),prev.get("renewable_mw")))
                k[3].metric("Taastuvate osakaal", f"{snap['renewable_share']:.1f}%" if snap['renewable_share'] is not None else "—", delta=delta_text(snap.get("renewable_share"),prev.get("renewable_share")))
                if region == "EE":
                    sys = system_df.copy().sort_values("time_utc") if not system_df.empty else pd.DataFrame()
                    if sys.empty:
                        st.warning("Eleringi tegelikud tootmise/tarbimise andmed pole hetkel saadaval.")
                    else:
                        value_cols = [c for c in ["production_mw", "consumption_mw"] if c in sys.columns]
                        if value_cols:
                            chart = sys.tail(24 * 12).melt(id_vars=["time_local"], value_vars=value_cols, var_name="series", value_name="mw").dropna(subset=["mw"])
                            chart["series"] = chart["series"].map({"production_mw":"Tootmine", "consumption_mw":"Tarbimine"})
                            fig = px.line(chart, x="time_local", y="mw", color="series", labels={"time_local":"Aeg", "mw":"MW", "series":"Näitaja"}, title="Eesti tegelik tootmine ja tarbimine")
                            st.plotly_chart(fig, use_container_width=True)
                        source_badge("Elering actual", system_status.ok, f"uusim vaatlus {fmt_age(sys_time)}")
                else:
                    gdf, gst = entsoe_generation_results[region]
                    ldf, lst = entsoe_load_results[region]
                    parts = []
                    if not gdf.empty:
                        total_ts = gdf.groupby(["time_utc", "time_local"], as_index=False)["generation_mw"].sum().rename(columns={"generation_mw":"MW"})
                        total_ts["series"] = "Tootmine"
                        parts.append(total_ts[["time_local", "MW", "series"]])
                    if not ldf.empty:
                        load_ts = ldf[["time_local", "load_mw"]].rename(columns={"load_mw":"MW"}).copy()
                        load_ts["series"] = "Tarbimine"
                        parts.append(load_ts[["time_local", "MW", "series"]])
                    if parts:
                        chart = pd.concat(parts, ignore_index=True)
                        fig = px.line(chart, x="time_local", y="MW", color="series", labels={"time_local":"Aeg", "series":"Näitaja"}, title=f"{name} tegelik tootmine ja tarbimine")
                        st.plotly_chart(fig, use_container_width=True)
                    else:
                        hist = snapshot_history_df(baltic_system_snapshot, region)
                        if not hist.empty:
                            parts2 = []
                            if "production_mw" in hist.columns:
                                a = hist[["time_local","production_mw"]].rename(columns={"production_mw":"MW"}).dropna(); a["series"]="Tootmine"; parts2.append(a)
                            if "consumption_mw" in hist.columns:
                                b = hist[["time_local","consumption_mw"]].rename(columns={"consumption_mw":"MW"}).dropna(); b["series"]="Tarbimine"; parts2.append(b)
                            if parts2:
                                chart = pd.concat(parts2, ignore_index=True)
                                fig = px.line(chart, x="time_local", y="MW", color="series", labels={"time_local":"Aeg","series":"Näitaja"}, title=f"{name} tegelik tootmine ja tarbimine — snapshot")
                                st.plotly_chart(fig, use_container_width=True)
                                st.info("Kasutatakse viimast edukat ENTSO-E GitHub Actionsi snapshot'i.")
                        else:
                            st.warning(f"{name}: nii otse-ENTSO-E kui snapshot puuduvad.")
                    if not gdf.empty:
                        g = gdf.dropna(subset=["generation_mw"]).copy()
                        fig2 = px.area(g, x="time_local", y="generation_mw", color="technology", labels={"time_local":"Aeg", "generation_mw":"MW", "technology":"Tootmisliik"}, title=f"{name} tootmisjaotus")
                        st.plotly_chart(fig2, use_container_width=True)
                    source_badge(f"ENTSO-E {region} A75 generation", gst.ok, gst.error or gst.note)
                    source_badge(f"ENTSO-E {region} A65 load", lst.ok, lst.error or lst.note)

                # Same network context for all Baltic countries: latest directional A11 flows on adjacent borders.
                if not entsoe_baltic_flows.empty:
                    rel = entsoe_baltic_flows[(entsoe_baltic_flows["from_region"] == region) | (entsoe_baltic_flows["to_region"] == region)].copy()
                    if not rel.empty:
                        latest_rows = []
                        for (border, direction), gx in rel.groupby(["border", "direction"]):
                            row = gx.sort_values("time_utc").tail(1).iloc[0]
                            latest_rows.append({"Piir": border, "Suund": direction, "Voog MW": row["flow_mw"], "Vaatlus": row["time_local"]})
                        st.markdown("#### Piiriülesed füüsilised vood")
                        st.dataframe(pd.DataFrame(latest_rows), hide_index=True, use_container_width=True, column_config={"Voog MW": st.column_config.NumberColumn(format="%.0f")})
                        st.caption("ENTSO-E A11 actual physical flow. Kuvatakse suunalised väärtused; neid ei tõlgendata automaatselt vaba ülekandevõimsusena.")

    with tab_entsoe:
        st.markdown("### ENTSO-E Transparency Platform — Baltikumi tootmisjaotus ja Eesti piiriülesed füüsilised vood")
        st.caption("Baltikumi tootmisjaotus: A75 / A16 realised; koormus: A65. Eesti füüsilised vood: A11. Need on ENTSO-E Transparency Platformi allikaandmed, mitte dashboardis tuletatud väärtused.")

        st.markdown("#### Eesti tegelik elektritootmine tootmisliigi kaupa")
        if entsoe_generation.empty:
            st.warning(entsoe_generation_status.error or entsoe_generation_status.note or "ENTSO-E tootmisandmed pole saadaval.")
            if not secret("ENTSOE_API_KEY"):
                st.code('ENTSOE_API_KEY = "sinu-võti"', language="toml")
        else:
            g = entsoe_generation.dropna(subset=["generation_mw"]).copy()
            fig = px.area(g, x="time_local", y="generation_mw", color="technology",
                          labels={"time_local": "Aeg", "generation_mw": "MW", "technology": "Tootmisliik"})
            fig.update_layout(legend_title_text="Tootmisliik")
            st.plotly_chart(fig, use_container_width=True)
            latest_t = g["time_utc"].max()
            latest_g = g[g["time_utc"] == latest_t].groupby("technology", as_index=False)["generation_mw"].sum().sort_values("generation_mw", ascending=False)
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
            net = f.groupby(["time_utc", "time_local", "border"], as_index=False)["signed_mw"].sum()
            fig2 = px.line(net, x="time_local", y="signed_mw", color="border",
                           labels={"time_local": "Aeg", "signed_mw": "Eesti netoeksport (+) / netoimport (−), MW", "border": "Piir"})
            fig2.add_hline(y=0, line_dash="dash")
            st.plotly_chart(fig2, use_container_width=True)
            st.caption("Netovoo märk on defineeritud Eesti vaates: + = eksport Eestist, − = import Eestisse. Alusread jäävad eraldi suunaga nähtavaks.")

        st.markdown("#### Päev-ette suunaline ülekandevõimsus (NTC)")
        if entsoe_ntc.empty:
            st.warning("ENTSO-E päev-ette NTC andmed pole hetkel saadaval.")
        else:
            n = entsoe_ntc.copy()
            fig3 = px.line(n, x="time_local", y="ntc_mw", color="direction", facet_row="border",
                           labels={"time_local": "Aeg", "ntc_mw": "Päev-ette NTC, MW", "direction": "Suund"})
            st.plotly_chart(fig3, use_container_width=True)
            st.caption("A61 + contract_MarketAgreement.Type=A01. See on päev-ette prognoositud suunaline transfer capacity, mitte intraday jääkvõimsus ega füüsilise voo põhjal arvutatud vaba maht.")

    with tab_umm:
        st.markdown("### Nord Pool UMM — kiireloomulised turuteated")
        st.caption(
            "Allikas on Nord Pooli avalik UMM REST API. BalticPulse küsib API-t uuesti iga minuti järel "
            "(cache 30 s). Vaikimisi kuvatakse kõige uuemad avaldatud/uuendatud teated esimesena."
        )
        if umm_meta.error:
            st.error(f"Nord Pool UMM API viga: {umm_meta.error}")
        elif umm_df.empty:
            st.info("UMM teateid ei leitud.")
        else:
            c_fresh, c_count, c_active = st.columns(3)
            c_fresh.metric(
                "Uusim UMM",
                newest_umm_time.tz_convert(TALLINN).strftime("%d.%m %H:%M") if newest_umm_time is not None else "—",
                delta=f"{umm_age_minutes:.0f} min tagasi" if umm_age_minutes is not None else None,
                delta_color="off",
            )
            c_count.metric("API-st laaditud", f"{len(umm_df)} teadet")
            c_active.metric("Hetkel aktiivsed", f"{len(active_umm)} teadet")

            if umm_age_minutes is not None and umm_age_minutes > 180:
                st.warning(
                    "Nord Pool API vastas, kuid uusim API-s nähtav UMM on üle 3 tunni vana. "
                    "See võib olla täiesti normaalne vaiksel perioodil; kui Nord Pooli veebis on uuem teade, "
                    "on tegu API/UI lahknevusega ja seda ei varjata."
                )

            only_active = st.toggle("Ainult hetkel aktiivsed sündmused", value=False)
            u = active_umm if only_active else umm_df.copy()

            # Latest publication/version first. The previous build sorted by MW, which made old large
            # outages look like the newest messages.
            if "publication_time" in u.columns:
                u = u.sort_values("publication_time", ascending=False, na_position="last")

            areas = sorted({
                part.strip()
                for value in u.get("area", pd.Series(dtype=str)).dropna().astype(str)
                for part in value.split(",")
                if part.strip()
            })
            area_sel = st.multiselect("Piirkond", areas, default=[])
            if area_sel and "area" in u.columns:
                u = u[u["area"].astype(str).apply(lambda x: any(sel in [p.strip() for p in x.split(",")] for sel in area_sel))]

            columns = [c for c in [
                "publication_time","area","asset_name","market_participant","status","message_type",
                "affected_capacity","installed_capacity","available_capacity","event_start","event_end",
                "reason","source_url"
            ] if c in u.columns]
            st.dataframe(
                u[columns], hide_index=True, use_container_width=True,
                column_config={
                    "publication_time": st.column_config.DatetimeColumn("Avaldatud/uuendatud", format="DD.MM.YYYY HH:mm"),
                    "affected_capacity": st.column_config.NumberColumn("Mõjutatud MW", format="%.0f"),
                    "installed_capacity": st.column_config.NumberColumn("Installeeritud MW", format="%.0f"),
                    "available_capacity": st.column_config.NumberColumn("Saadaval MW", format="%.0f"),
                    "source_url": st.column_config.LinkColumn("Nord Pool"),
                }
            )
            st.caption(
                "Märkus: SignalR push-kanal on Nord Pooli poolt kinnitatud, kuid BalticPulse ei kasuta seda enne, "
                "kui messageHub sündmuse nimi ja payload-contract on ametlikust arendusdokumentatsioonist kontrollitud. "
                "Praegune lahendus kasutab valideeritud avalikku /messages REST API-t."
            )

    with tab_news:
        st.markdown("### 🌍 Olulised energiauudised")
        st.caption(
            "Kuratoeritud värske voog energiale keskendunud või tugeva energiatoimetusega allikatest. "
            "BalticPulse ei genereeri uudiseid: kuvatakse väljaande pealkiri, avaldamisaeg, teema ja allikalink."
        )
        if not news_rows:
            st.warning("Uudistevoogu ei õnnestunud hetkel laadida.")
            if news_meta.errors:
                st.caption(" · ".join(news_meta.errors[:4]))
        else:
            st.caption(f"Allikad kättesaadavad: {news_meta.sources_ok}/{news_meta.sources_total}")
            ndf = pd.DataFrame(news_rows)
            ndf["published_at"] = pd.to_datetime(ndf["published_at"], utc=True, errors="coerce")
            ndf = ndf.sort_values("published_at", ascending=False, na_position="last")

            topics = sorted(x for x in ndf["topic"].dropna().unique() if x)
            selected_topics = st.multiselect("Teemad", topics, default=[])
            if selected_topics:
                ndf = ndf[ndf["topic"].isin(selected_topics)]

            for _, row in ndf.head(18).iterrows():
                ts = row["published_at"]
                when = ts.tz_convert(TALLINN).strftime("%d.%m %H:%M") if pd.notna(ts) else "aeg teadmata"
                st.markdown(f"**{row['title']}**")
                st.caption(f"{row['source']} · {when} · {row['topic']}")
                if row.get("summary"):
                    st.write(str(row["summary"])[:320])
                st.link_button("Ava artikkel ↗", row["url"])
                st.divider()


    with tab_reserves:
        st.markdown("### Balti balancing capacity market — jooksva aasta areng")
        st.caption("Põhivaade on jooksva aasta YTD trend: aFRR ja mFRR võimsuse valmisolekutasud (€/MW/h), agregeeritud kuukeskmisteks. Autoriteetne algallikas on Baltic Transparency Dashboard; arhiiv tuleb Voltoni CC-BY-4.0 päevafailidest.")
        if not reserve_ytd.empty:
            ytd = reserve_ytd[reserve_ytd["date"].dt.year == datetime.now(TALLINN).year].copy()
            if not ytd.empty:
                monthly = ytd.groupby([pd.Grouper(key="date", freq="MS"), "region", "product", "direction"], as_index=False)["price_eur_mw_h"].mean()
                fig_y = px.line(monthly, x="date", y="price_eur_mw_h", color="region", line_dash="product", facet_row="direction", markers=True,
                                labels={"date":"Kuu", "price_eur_mw_h":"Keskmine €/MW/h", "region":"Piirkond", "product":"Toode"})
                st.plotly_chart(fig_y, use_container_width=True)
        else:
            st.info("YTD arhiiv tekib pärast GitHub Actionsi workflow esimest käivitust. Allpool on viimase 7 päeva live-vaade.")

        st.markdown("#### Viimase 7 päeva detail")
        selected_reserve_regions = st.multiselect("Piirkonnad", BALTICS, default=BALTICS, key="reserve_regions")
        frames = []
        for region in selected_reserve_regions:
            rdf, _ = reserve_results[region]
            if not rdf.empty:
                frames.append(rdf)
        if not frames:
            st.warning("Reservituru andmed pole hetkel saadaval.")
        else:
            r = pd.concat(frames, ignore_index=True).dropna(subset=["price_eur_mw_h"])
            fig = px.line(r, x="time_local", y="price_eur_mw_h", color="region", line_dash="product", facet_row="direction",
                          labels={"price_eur_mw_h":"€/MW/h", "time_local":"Aeg"})
            st.plotly_chart(fig, use_container_width=True)
            latest_day = r["time_local"].dt.date.max()
            s = r[r["time_local"].dt.date == latest_day].groupby(["region","product","direction"])["price_eur_mw_h"].agg(["mean","min","max"]).reset_index()
            st.dataframe(s, hide_index=True, use_container_width=True)
            st.info("FCR-i ei kuvata enne, kui selle kasutatav andmeliides on eraldi valideeritud. Capacity hind (€/MW/h) ja balancing-energy hind (€/MWh) jäävad eraldi plokkidesse.")

        st.markdown("### Balti aktiveeritud tasakaalustusenergia hinnad — EE/LV/LT")
        st.caption("aFRR ja mFRR marginal clearing prices, €/MWh, 15-min MTU. Up = süsteem vajab ülesreguleerimist; down = allareguleerimist. Null väärtust ei täideta, sest see võib tähendada, et selles suunas aktivatsiooni ei kliiritud.")
        energy_frames = []
        for region in selected_reserve_regions:
            edf, _ = balancing_energy_results[region]
            if not edf.empty:
                energy_frames.append(edf)
        if not energy_frames:
            st.warning("Balancing-energy andmed pole hetkel saadaval.")
        else:
            e = pd.concat(energy_frames, ignore_index=True)
            eplot = e.dropna(subset=["price_eur_mwh"]).copy()
            if eplot.empty:
                st.info("Andmeallikas vastas, kuid valitud perioodis ei ole mitte-null clearing-price väärtusi.")
            else:
                fig_e = px.line(
                    eplot, x="time_local", y="price_eur_mwh", color="region",
                    line_dash="product", facet_row="direction",
                    labels={"price_eur_mwh": "€/MWh", "time_local": "Aeg", "region": "Piirkond", "product": "Toode"},
                )
                st.plotly_chart(fig_e, use_container_width=True)
                latest_e = (eplot.sort_values("time_utc")
                            .groupby(["region", "product", "direction"], as_index=False)
                            .tail(1)[["region", "product", "direction", "time_local", "price_eur_mwh"]]
                            .sort_values(["region", "product", "direction"]))
                st.dataframe(latest_e, hide_index=True, use_container_width=True, column_config={
                    "price_eur_mwh": st.column_config.NumberColumn("Viimane €/MWh", format="%.1f"),
                    "time_local": st.column_config.DatetimeColumn("MTU", format="DD.MM.YYYY HH:mm"),
                })
                st.caption("Allikas: Elering / Baltic Transparency Dashboard via Volton Public Data. Andmestik on eraldi balancing-capacity turust.")

    with tab_gas:
        st.markdown("### EL ja Läti gaasihoidlad — GIE AGSI+")
        st.caption("AGSI+ on päevane andmestik: see ei ole intraday reaalaja mõõdik. GIE järgi kajastab päevakirje eelmise gaasipäeva lõpu seisu.")
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

            show_cols = [c for c in ["scope","gasDayStart","full","gasInStorage","workingGasVolume","injection","withdrawal","injectionCapacity","withdrawalCapacity","status"] if c in storage_df.columns]
            st.dataframe(storage_df[show_cols], hide_index=True, use_container_width=True)
            source_badge("GIE AGSI+", storage_status.ok, storage_status.error or storage_status.note)

    with tab_fundamentals:
        st.markdown("### Turu põhifundamentaalid")
        st.caption("Ametlikud/esmased allikad, mitte Yahoo Finance. TTF NGP on spot-referents, Brent on EIA spot-seeria ja EUA on EEX primaaroksjoni clearing price.")
        c1, c2, c3 = st.columns(3)
        c1.metric("🇳🇱 TTF NGP", f"{ttf_latest:.1f} €/MWh" if ttf_latest is not None else "—", help=f"Viimane kuupäev: {pd.Timestamp(ttf_date).date() if ttf_date is not None else '—'}")
        c2.metric("🌍 Brent spot", f"{brent_latest:.1f} $/bbl" if brent_latest is not None else "—", help=f"Viimane kuupäev: {pd.Timestamp(brent_date).date() if brent_date is not None else '—'}")
        c3.metric("EUA oksjon", f"{eua_latest:.2f} €/tCO₂" if eua_latest is not None else "—", help=f"Viimane oksjon: {pd.Timestamp(eua_date).date() if eua_date is not None else '—'}")

        if not ttf_df.empty:
            fig = px.line(ttf_df.tail(60), x="date", y="price_eur_mwh", markers=True, labels={"date":"Kuupäev","price_eur_mwh":"€/MWh"}, title="EEX Neutral Gas Price TTF — viimased lõplikud päevahinnad")
            st.plotly_chart(fig, use_container_width=True)
        else:
            st.warning(ttf_status.error or "EEX TTF NGP andmed pole saadaval.")

        l, r = st.columns(2)
        with l:
            if not brent_df.empty:
                b = brent_df.tail(180)
                fig = px.line(b, x="date", y="price_usd_bbl", labels={"date":"Kuupäev","price_usd_bbl":"$/bbl"}, title="Europe Brent Spot Price FOB — EIA")
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.warning(brent_status.error or "EIA Brent andmed pole saadaval.")
        with r:
            if not eua_df.empty:
                fig = px.line(eua_df, x="date", y="price_eur_tco2", markers=True, labels={"date":"Oksjonipäev","price_eur_tco2":"€/tCO₂"}, title="EUA primaaroksjoni clearing price — EEX")
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.warning(eua_status.error or "EEX EUA oksjoniandmed pole saadaval.")

        st.info("Metoodika: EEX TTF NGP ei ole TTF front-month futuur; EIA Brent on füüsilise spot-turu referents; EEX EUA oksjonihind on primaarmarketi hind. Nii väldime eri instrumentide eksitavat nimetamist üheks 'turuhinnaks'.")


    with tab_quality:
        st.markdown("### Andmekvaliteedi ja ulatuse reeglid")
        st.markdown(
            """
    - **Ei kasutata sünteetilisi varuväärtusi.** API tõrke korral näidatakse puuduvat väärtust või veateadet.
    - **UMM võimsusi ei liideta automaatselt.** Teated võivad kattuda, olla sama sündmuse versioonid või kirjeldada eri turuobjekte.
    - **Capacity ≠ energy.** Reservi valmisolekutasu (€/MW/h) ja aktiveeritud tasakaalustusenergia hind (€/MWh) on eri näitajad.
    - **Päev-ette hind ≠ lõpptarbija hind.** Maksud, võrgutasud ja müüja marginaal ei kuulu börsihinna sisse.
    - **Värskus on osa andmekvaliteedist.** Spot-hind seotakse täpselt käimasoleva MTU-ga; Eleringi tootmise ja tarbimise põhiväärtust lubatakse kuni 15 min vanusena; vanem väärtus ei ole “praegu” ja seda ei asendata ENTSO-E-ga. ENTSO-E tootmisliikide ning piiriüleste voogude väärtused on eraldi allikad ja nende värskus kuvatakse eraldi.
    - **ENTSO-E ristkontroll:** Eesti tootmisjaotus (A75) ja EE–FI/EE–LV füüsilised vood (A11) pärinevad Transparency Platformist. Eleringi kogutootmist ja ENTSO-E tootmisliike ei sunnita kunstlikult võrdseks, sest avaldamisajad ja metoodika võivad erineda.
    - **Gaasihoidlad on päevased.** AGSI+ viimane kirje kajastab gaasipäeva, mitte hetke intraday taset.
    - **Ülekandevõimsus:** A61 päev-ette NTC kuvatakse eraldi A11 füüsilisest voost. `Voog / NTC` on koormuse kontekstinäitaja, mitte intraday vaba jääkvõimsus.
    - **Fundamentaalid:** TTF/LVA-EST/FIN/LTU NGP = EEX current files (15-min refresh); TTF 60 päeva final history on eraldi ajaloo jaoks. Brent = U.S. EIA päevane Europe Brent Spot Price FOB; EUA = EEX primaaroksjoni clearing price. Päevaseid/event-põhiseid instrumente ei nimetata intraday reaalajaks.
    - **Fallback:** kui Eleringi tootmine/tarbimine puudub, kasutatakse ainult ENTSO-E A75/A65 tegelikke vaatlusi; sünteetilist varuväärtust ei looda.
    - **Ulatuse piir:** intraday offered capacity / jääkvõimsust ei nimetata päev-ette NTC-ks.
            """
        )

    st.divider()
    st.caption("BalticPulse · Allikad: Elering, ENTSO-E Transparency Platform, Nord Pool UMM, Baltic Transparency Dashboard/Volton, GIE AGSI+, EEX, U.S. EIA ning kuratoeritud energia-uudisallikad. Põhimõte: parem puuduv number kui kontrollimata number.")


render_dashboard()
