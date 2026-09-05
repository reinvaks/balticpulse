from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import streamlit as st

from energy_sources import (
    fetch_elering_prices,
    fetch_elering_system,
    fetch_gas_storage,
    fetch_reserve_capacity,
    fetch_balancing_energy,
    fetch_entsoe_generation_by_type,
    fetch_entsoe_estonia_flows,
    fetch_entsoe_estonia_ntc,
    fetch_entsoe_actual_load,
    fetch_eex_ngp_current,
    fetch_eex_ttf_ngp,
    fetch_eia_brent,
    fetch_eex_eua_auction,
)
from umm_client import fetch_umm_messages

TALLINN = ZoneInfo("Europe/Tallinn")
REGIONS = ["EE", "LV", "LT", "FI"]
BALTICS = ["EE", "LV", "LT"]

st.set_page_config(page_title="BalticPulse | Energy Market Dashboard", page_icon="⚡", layout="wide")

def secret(name: str) -> str:
    try:
        return str(st.secrets.get(name, "") or "")
    except Exception:
        return ""


def source_badge(label: str, ok: bool, detail: str | None = None) -> None:
    icon = "🟢" if ok else "🔴"
    st.caption(f"{icon} {label}" + (f" — {detail}" if detail else ""))


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


@st.cache_data(ttl=300)
def load_reserves(region: str):
    return fetch_reserve_capacity(region)


@st.cache_data(ttl=300)
def load_balancing_energy(region: str):
    return fetch_balancing_energy(region)


@st.cache_data(ttl=120)
def load_umm():
    return fetch_umm_messages(limit=500, max_pages=4, retries=3)


@st.cache_data(ttl=1800)
def load_storage(key: str):
    return fetch_gas_storage(key)


@st.cache_data(ttl=300)
def load_entsoe_generation(key: str):
    now = datetime.now(timezone.utc)
    return fetch_entsoe_generation_by_type(key, now - timedelta(hours=36), now + timedelta(hours=1), "EE")


@st.cache_data(ttl=300)
def load_entsoe_flows(key: str):
    now = datetime.now(timezone.utc)
    return fetch_entsoe_estonia_flows(key, now - timedelta(hours=36), now + timedelta(hours=1))


@st.cache_data(ttl=900)
def load_entsoe_ntc(key: str):
    now = datetime.now(timezone.utc)
    return fetch_entsoe_estonia_ntc(key, now - timedelta(hours=6), now + timedelta(days=2))


@st.cache_data(ttl=300)
def load_entsoe_load(key: str):
    now = datetime.now(timezone.utc)
    return fetch_entsoe_actual_load(key, now - timedelta(hours=36), now + timedelta(hours=1), "EE")


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


@st.fragment(run_every=120)
def render_dashboard():
    # Header
    c1, c2 = st.columns([4, 1])
    with c1:
        st.title("⚡ BalticPulse")
        st.caption("Balti ja Põhjamaade energiaturu reaalaja olukorrapilt — elekter, võrk, reservid, UMM-id, gaas ja põhifundamentaalid.")
    with c2:
        st.write("")
        if st.button("🔄 Värskenda", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    now_local = datetime.now(TALLINN)
    today = now_local.date()
    tomorrow = today + timedelta(days=1)
    st.caption(f"Vaate aeg: **{now_local:%d.%m.%Y %H:%M:%S}** Europe/Tallinn · automaatne värskendus iga 2 min (Streamliti native fragment)")

    with st.spinner("Laadin operatiivandmeid..."):
        entsoe_key = secret("ENTSOE_API_KEY")
        agsi_key = secret("GIE_AGSI_API_KEY")
        # Independent sources are fetched concurrently so a slow daily/fundamental source does not
        # hold the operational view hostage. Individual loaders still retain their own cache TTLs.
        with ThreadPoolExecutor(max_workers=12) as pool:
            fut_prices = pool.submit(load_short_prices)
            fut_system = pool.submit(load_system)
            fut_umm = pool.submit(load_umm)
            fut_storage = pool.submit(load_storage, agsi_key)
            fut_gen = pool.submit(load_entsoe_generation, entsoe_key)
            fut_flows = pool.submit(load_entsoe_flows, entsoe_key)
            fut_ntc = pool.submit(load_entsoe_ntc, entsoe_key)
            fut_load = pool.submit(load_entsoe_load, entsoe_key)
            fut_ttf_hist = pool.submit(load_ttf)
            fut_brent = pool.submit(load_brent)
            fut_eua = pool.submit(load_eua)
            reserve_futs = {r: pool.submit(load_reserves, r) for r in BALTICS}
            energy_futs = {r: pool.submit(load_balancing_energy, r) for r in BALTICS}
            ngp_futs = {a: pool.submit(load_ngp_current, a) for a in ["TTF", "LVA-EST", "FIN", "LTU"]}

            prices, price_status = fut_prices.result()
            system_df, system_status = fut_system.result()
            umm_rows, umm_meta = fut_umm.result()
            storage_df, storage_status = fut_storage.result()
            entsoe_generation, entsoe_generation_status = fut_gen.result()
            entsoe_flows, entsoe_flow_statuses = fut_flows.result()
            entsoe_ntc, entsoe_ntc_statuses = fut_ntc.result()
            entsoe_load, entsoe_load_status = fut_load.result()
            ttf_df, ttf_status = fut_ttf_hist.result()
            brent_df, brent_status = fut_brent.result()
            eua_df, eua_status = fut_eua.result()
            reserve_results = {r: f.result() for r, f in reserve_futs.items()}
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
    # Elering system data is treated as operational only when reasonably fresh.
    _elering_fresh = is_fresh(elering_sys_time, 15)
    prod = last_sys["production_mw"].iloc[0] if _elering_fresh and not last_sys.empty and "production_mw" in last_sys and pd.notna(last_sys["production_mw"].iloc[0]) else None
    cons = last_sys["consumption_mw"].iloc[0] if _elering_fresh and not last_sys.empty and "consumption_mw" in last_sys and pd.notna(last_sys["consumption_mw"].iloc[0]) else None
    prod_source = "Elering" if prod is not None else None
    cons_source = "Elering" if cons is not None else None
    prod_time = elering_sys_time if prod is not None else None
    cons_time = elering_sys_time if cons is not None else None

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
        active_umm = umm_df[(starts.isna() | (starts <= now_utc_ts)) & (ends.isna() | (ends >= now_utc_ts))].copy()
        if "affected_capacity" in active_umm.columns:
            active_umm["affected_capacity"] = pd.to_numeric(active_umm["affected_capacity"], errors="coerce")

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
            renewable_names = {"Biomass", "Geothermal", "Hydro Run-of-river and poundage",
                               "Hydro Water Reservoir", "Marine", "Other renewable", "Solar",
                               "Wind Offshore", "Wind Onshore"}
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
    cols = st.columns(7)
    cols[0].metric("EE spot — käimasolev MTU", f"{current_prices['EE']:.1f} €/MWh" if current_prices["EE"] is not None else "—")
    cols[1].metric("FI spot — käimasolev MTU", f"{current_prices['FI']:.1f} €/MWh" if current_prices["FI"] is not None else "—")
    spread = None
    if current_prices["EE"] is not None and current_prices["FI"] is not None:
        spread = current_prices["EE"] - current_prices["FI"]
    cols[2].metric("EE–FI hinnavahe", f"{spread:+.1f} €/MWh" if spread is not None else "—")
    cols[3].metric("EE tootmine", f"{prod:.0f} MW" if prod is not None else "—", delta=(f"{fmt_age(prod_time)} vana" if prod_time is not None else None), delta_color="off", help=f"Allikas: {prod_source or 'andmed puuduvad'}. Põhi-KPI ei kasuta ENTSO-E fallback’i.")
    cols[4].metric("EE tarbimine", f"{cons:.0f} MW" if cons is not None else "—", delta=(f"{fmt_age(cons_time)} vana" if cons_time is not None else None), delta_color="off", help=f"Allikas: {cons_source or 'andmed puuduvad'}. Põhi-KPI ei kasuta ENTSO-E fallback’i.")
    cols[5].metric("Aktiivsed UMM-id", f"{len(active_umm)}")
    cols[6].metric("Suurim UMM mõju", f"{largest_umm:.0f} MW" if largest_umm is not None else "—", help="Suurim üksik aktiivses UMM-is raporteeritud mõjutatud võimsus. UMM-ide MW väärtusi ei liideta, sest teated võivad kattuda või olla sama sündmuse versioonid.")

    flow1, flow2, flow3, flow4 = st.columns(4)
    def flow_label(v):
        if v is None:
            return "—"
        return f"{abs(v):.0f} MW " + ("eksport" if v > 0 else "import" if v < 0 else "tasakaalus")
    flow1.metric("EE–FI füüsiline netovoog", flow_label(latest_border_flows["EE–FI"]), delta=(f"{fmt_age(latest_border_flow_time['EE–FI'])} vana" if latest_border_flow_time["EE–FI"] is not None else None), delta_color="off", help="ENTSO-E A11. Positiivne märk tähendab Eesti netoeksporti; negatiivne Eesti netoimporti.")
    flow2.metric("EE–LV füüsiline netovoog", flow_label(latest_border_flows["EE–LV"]), delta=(f"{fmt_age(latest_border_flow_time['EE–LV'])} vana" if latest_border_flow_time["EE–LV"] is not None else None), delta_color="off", help="ENTSO-E A11. Positiivne märk tähendab Eesti netoeksporti; negatiivne Eesti netoimporti.")
    flow3.metric("EE taastuvtootmine", f"{renewable_generation_mw:.0f} MW" if renewable_generation_mw is not None else "—", help="ENTSO-E A75 tegelik tootmine tootmisliikide kaupa; taastuvate summa. See ei ole Eleringi kogutootmise asendus.")
    flow4.metric("Taastuvate osakaal ENTSO-E tootmises", f"{renewable_share:.1f}%" if renewable_share is not None else "—", help="Arvutatud ENTSO-E A75 viimase värske tootmisvaatluse tootmisliikidest. Operatiivne indikatsioon, mitte ametlik taastuvenergia statistika.")

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
    f1.metric("TTF NGP — D", f"{ngp_current['TTF']:.1f} €/MWh" if ngp_current["TTF"] is not None else "—", help="EEX current NGP; EEX uuendab faili iga 15 minuti järel D/D+1/D+2 jaoks.")
    f2.metric("LVA–EST NGP — D", f"{ngp_current['LVA-EST']:.1f} €/MWh" if ngp_current["LVA-EST"] is not None else "—", help="EEX LVA-EST Neutral Gas Price, current gas day.")
    f3.metric("FIN NGP — D", f"{ngp_current['FIN']:.1f} €/MWh" if ngp_current["FIN"] is not None else "—", help="EEX FIN Neutral Gas Price, current gas day.")
    f4.metric("LTU NGP — D", f"{ngp_current['LTU']:.1f} €/MWh" if ngp_current["LTU"] is not None else "—", help="EEX LTU Neutral Gas Price, current gas day.")
    f5, f6 = st.columns(2)
    f5.metric("Brent — EIA spot (päevane)", f"{brent_latest:.1f} $/bbl" if brent_latest is not None else "—", help="Ametlik EIA päevane Europe Brent Spot Price FOB. See ei ole intraday reaalaja hind.")
    f6.metric("EUA — EEX oksjon", f"{eua_latest:.2f} €/tCO₂" if eua_latest is not None else "—", help="EEX EUA primaaroksjoni viimane clearing price. See ei ole secondary-market intraday hind.")
    st.caption("Operatiivne gaas: EEX NGP TTF/LVA-EST/FIN/LTU current files (15-min refresh). Brent on EIA päevane ametlik spot-seeria ja EUA EEX primaaroksjoni hind — neid ei esitata intraday reaalajana.")


    # ---------- OPERATIONAL ATTENTION RULES ----------
    # These are transparent dashboard heuristics, not regulatory limits or forecasts.
    attention: list[dict[str, str]] = []

    def add_alert(level: str, topic: str, message: str) -> None:
        attention.append({"Tase": level, "Teema": topic, "Tähelepanek": message})

    if spread is not None and abs(spread) >= 50:
        level = "🔴 Kõrge" if abs(spread) >= 100 else "🟠 Tähelepanu"
        add_alert(level, "EE–FI hinnavahe", f"Hetke hinnavahe {spread:+.1f} €/MWh (reegel: |spread| ≥ 50 €/MWh).")

    if largest_umm is not None and largest_umm >= 300:
        level = "🔴 Kõrge" if largest_umm >= 600 else "🟠 Tähelepanu"
        add_alert(level, "UMM", f"Suurim üksik aktiivne UMM mõjutab {largest_umm:.0f} MW (reegel: ≥ 300 MW).")

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
    st.caption("Tähelepanureeglid on läbipaistvad heuristikad olukorrapildi kiirendamiseks, mitte ametlikud häirepiirid ega prognoosid. Lävendid: |EE–FI spread| 50/100 €/MWh; UMM 300/600 MW; voog/DA NTC 90/100%; balancing energy |500/1000| €/MWh; gaasihoidlad <30% või ~7 päeva langus ≥5 pp.")

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

    # Critical events = active UMMs with capacity, sorted descending. This is a queue, not a false aggregate.
    st.markdown("#### Olulised aktiivsed turusündmused")
    if active_umm.empty:
        st.info("Aktiivseid UMM-e ei leitud või UMM allikas ei vastanud.")
    else:
        critical = active_umm.copy()
        if "affected_capacity" in critical.columns:
            critical = critical.sort_values("affected_capacity", ascending=False, na_position="last")
        show = [c for c in ["area", "asset_name", "message_type", "affected_capacity", "event_end", "reason", "source_url"] if c in critical.columns]
        st.dataframe(
            critical[show].head(10), hide_index=True, use_container_width=True,
            column_config={
                "affected_capacity": st.column_config.NumberColumn("Mõjutatud MW", format="%.0f"),
                "source_url": st.column_config.LinkColumn("Allikas"),
            },
        )

    # Freshness & source health is a first-class part of the dashboard.
    with st.expander("Andmeallikate kvaliteet ja värskus", expanded=False):
        price_age = fmt_age(prices["time_utc"].max()) if not prices.empty and "time_utc" in prices else "—"
        system_age = fmt_age(sys_time)
        source_badge("Elering hinnad", price_status.ok, f"uusim vaatlus {price_age}; {price_status.error or price_status.note or 'OK'}")
        source_badge("Elering süsteem", system_status.ok, f"uusim vaatlus {system_age}; {system_status.error or system_status.note or 'OK'}")
        source_badge("Nord Pool UMM", not bool(umm_meta.error), umm_meta.error or f"HTTP {umm_meta.status_code}")
        for region, (_, statuses) in reserve_results.items():
            for s in statuses:
                source_badge(s.source, s.ok, s.error or s.note)
        for region, (_, statuses) in balancing_energy_results.items():
            for s in statuses:
                source_badge(s.source, s.ok, s.error or s.note)
        source_badge("GIE AGSI+", storage_status.ok, storage_status.error or storage_status.note)
        source_badge(entsoe_generation_status.source, entsoe_generation_status.ok, entsoe_generation_status.error or entsoe_generation_status.note)
        source_badge(entsoe_load_status.source, entsoe_load_status.ok, entsoe_load_status.error or entsoe_load_status.note)
        for area, (_, st_ngp) in ngp_current_results.items():
            source_badge(st_ngp.source, st_ngp.ok, st_ngp.error or st_ngp.note)
        source_badge(ttf_status.source, ttf_status.ok, ttf_status.error or ttf_status.note)
        source_badge(brent_status.source, brent_status.ok, brent_status.error or brent_status.note)
        source_badge(eua_status.source, eua_status.ok, eua_status.error or eua_status.note)
        for s in entsoe_flow_statuses:
            source_badge(s.source, s.ok, s.error or s.note)
        for s in entsoe_ntc_statuses:
            source_badge(s.source, s.ok, s.error or s.note)

    # ---------- 2. DETAIL TABS ----------
    tab_overview, tab_prices, tab_system, tab_entsoe, tab_umm, tab_reserves, tab_gas, tab_fundamentals, tab_quality = st.tabs([
        "📌 Põhivaade", "⚡ Elektrihinnad", "🏭 Eesti süsteem", "🌐 ENTSO-E", "📣 UMM", "🔄 Reservid", "🔥 Gaasihoidlad", "📈 Fundamentaalid", "✅ Andmekvaliteet"
    ])

    with tab_overview:
        left, right = st.columns(2)
        with left:
            st.markdown("### EE/LV/LT/FI spot-hinnad")
            if prices.empty:
                st.warning("Eleringi hinnad pole hetkel saadaval.")
            else:
                p = prices[prices["time_local"].dt.date >= today]
                fig = px.line(p, x="time_local", y="price", color="region", labels={"price":"€/MWh","time_local":"Aeg","region":"Piirkond"})
                fig.add_vline(x=now_local, line_dash="dash")
                st.plotly_chart(fig, use_container_width=True)
            source_badge("Elering Dashboard / Nord Pool", price_status.ok)
        with right:
            st.markdown("### Eesti tootmine ja tarbimine")
            if system_df.empty or not any(c in system_df.columns for c in ["production_mw", "consumption_mw"]):
                st.warning("Eleringi süsteemiandmeid ei õnnestunud parsida või neid pole saadaval.")
            else:
                plot_cols = [c for c in ["production_mw", "consumption_mw"] if c in system_df.columns]
                m = system_df.melt(id_vars="time_local", value_vars=plot_cols, var_name="series", value_name="MW")
                labels = {"production_mw":"Tootmine", "consumption_mw":"Tarbimine"}
                m["series"] = m["series"].map(labels)
                fig = px.line(m, x="time_local", y="MW", color="series")
                st.plotly_chart(fig, use_container_width=True)
            source_badge("Elering electricity system", system_status.ok, f"uusim vaatlus {fmt_age(sys_time)}")

        st.markdown("### Balti reservituru hinnapilt")
        reserve_frames = []
        for region, (rdf, _) in reserve_results.items():
            if not rdf.empty:
                reserve_frames.append(rdf)
        if reserve_frames:
            rr = pd.concat(reserve_frames, ignore_index=True).dropna(subset=["price_eur_mw_h"])
            latest_day = rr["time_local"].dt.date.max()
            rr_day = rr[rr["time_local"].dt.date == latest_day]
            summary = rr_day.groupby(["region", "product", "direction"])["price_eur_mw_h"].mean().reset_index()
            pivot = summary.pivot_table(index=["region"], columns=["product", "direction"], values="price_eur_mw_h")
            pivot.columns = [f"{a} {b} €/MW/h" for a, b in pivot.columns]
            st.dataframe(pivot.reset_index(), hide_index=True, use_container_width=True)
        else:
            st.info("Balti reservituru hinnad pole hetkel saadaval.")

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
        st.markdown("### Eesti elektrisüsteemi tegelikud allikaandmed")
        if system_df.empty:
            st.error(system_status.error or system_status.note or "Andmed pole saadaval")
        else:
            st.dataframe(system_df.tail(200), hide_index=True, use_container_width=True)
            st.caption("Kuvatakse ainult Eleringi vastusest parsitud tegelikud väljad. Importi/eksporti ega tootmisliike ei tuletata kogutootmisest.")

    with tab_entsoe:
        st.markdown("### ENTSO-E Transparency Platform — Eesti tootmisjaotus ja piiriülesed füüsilised vood")
        st.caption("Tootmisjaotus: A75 / A16 realised. Füüsilised vood: A11. Need on ENTSO-E Transparency Platformi allikaandmed, mitte dashboardis tuletatud väärtused.")

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
        st.caption("UMM võib hõlmata tootmist, tarbimist, ülekannet ja muud siseteavet. Mõjutatud MW on teatepõhine ning eri teateid ei summeerita süsteemi netokatkestuseks.")
        if umm_meta.error:
            st.error(f"Nord Pool UMM API viga: {umm_meta.error}")
        elif umm_df.empty:
            st.info("UMM teateid ei leitud.")
        else:
            only_active = st.toggle("Ainult aktiivsed", value=True)
            u = active_umm if only_active else umm_df
            areas = sorted([x for x in u.get("area", pd.Series(dtype=str)).dropna().astype(str).unique() if x])
            area_sel = st.multiselect("Piirkond", areas, default=[])
            if area_sel:
                u = u[u["area"].astype(str).isin(area_sel)]
            if "affected_capacity" in u.columns:
                u = u.copy()
                u["affected_capacity"] = pd.to_numeric(u["affected_capacity"], errors="coerce")
                u = u.sort_values(["affected_capacity", "publication_time"] if "publication_time" in u.columns else ["affected_capacity"], ascending=False, na_position="last")
            columns = [c for c in ["area","asset_name","market_participant","status","message_type","affected_capacity","installed_capacity","available_capacity","publication_time","event_start","event_end","reason","source_url"] if c in u.columns]
            st.dataframe(u[columns], hide_index=True, use_container_width=True,
                         column_config={
                             "affected_capacity": st.column_config.NumberColumn("Mõjutatud MW", format="%.0f"),
                             "installed_capacity": st.column_config.NumberColumn("Installeeritud MW", format="%.0f"),
                             "available_capacity": st.column_config.NumberColumn("Saadaval MW", format="%.0f"),
                             "source_url": st.column_config.LinkColumn("Nord Pool"),
                         })

    with tab_reserves:
        st.markdown("### Balti balancing capacity market — EE/LV/LT")
        st.caption("Kuvatakse aFRR ja mFRR võimsuse valmisolekutasud (€/MW/h). Autoriteetne algallikas on Baltic Transparency Dashboard; JSON-liidesena kasutatakse Voltoni CC-BY-4.0 peeglit.")
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
        c1.metric("TTF NGP", f"{ttf_latest:.1f} €/MWh" if ttf_latest is not None else "—", help=f"Viimane kuupäev: {pd.Timestamp(ttf_date).date() if ttf_date is not None else '—'}")
        c2.metric("Brent spot", f"{brent_latest:.1f} $/bbl" if brent_latest is not None else "—", help=f"Viimane kuupäev: {pd.Timestamp(brent_date).date() if brent_date is not None else '—'}")
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
    st.caption("BalticPulse · Allikad: Elering, ENTSO-E Transparency Platform, Nord Pool UMM, Baltic Transparency Dashboard/Volton, GIE AGSI+, EEX ja U.S. EIA. Põhimõte: parem puuduv number kui kontrollimata number.")


render_dashboard()
