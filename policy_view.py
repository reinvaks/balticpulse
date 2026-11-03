"""Regional prices and outages by Tallinn delivery interval."""
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
import streamlit as st

from energy_sources import (
    ENTSOE_DOMAINS, _entsoe_get_xml, _parse_entsoe_timeseries,
    fetch_entsoe_day_ahead_ntc, fetch_entsoe_physical_flow,
)
from fetch_entsoe_day_ahead_map import COUNTRIES, fetch_zone
from umm_client import UMM_API, normalize_message

ZONES = ("EE", "FI", "LV", "LT", "SE1", "SE2", "SE3", "SE4")


def bounds(day):
    start = pd.Timestamp(day, tz="Europe/Tallinn")
    return start.tz_convert("UTC"), (start + pd.DateOffset(days=1)).tz_convert("UTC")


def local_time(stamp):
    return stamp.tz_convert("Europe/Tallinn").strftime("%d.%m %H:%M %z")


@st.cache_data(ttl=900, show_spinner=False)
def swedish_prices(day, _token):
    start, end = bounds(day)

    def zone_points(zone):
        try:
            points = fetch_zone(_token, COUNTRIES["SE"]["zones"][zone], start, end)
        except (requests.RequestException, RuntimeError, SyntaxError, ValueError):
            return []
        return [(zone, t, price) for t, price, _ in points if start <= t < end]

    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = [row for part in pool.map(zone_points, ZONES[4:]) for row in part]
    return pd.DataFrame(rows, columns=["region", "time_utc", "price"])


@st.cache_data(ttl=900, show_spinner=False)
def notices(day):
    start, end = bounds(day)
    rows = []
    for page in range(10):
        response = requests.get(UMM_API, params={
            "EventStartDate": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "EventStopDate": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "IncludeOutdated": "false", "Limit": 1000, "Skip": page * 1000,
        }, timeout=(12, 20))
        response.raise_for_status()
        body = response.json()
        rows.extend(body["items"])
        if not body["items"] or len(rows) >= body["total"]:
            return rows, len(rows) >= body["total"]
    return rows, False


def overlapping(rows, start, end):
    output = []
    for raw in rows:
        if raw.get("isOutdated") or raw.get("eventStatus") == 3:
            continue
        msg = normalize_message(raw)
        for kind in ("generationUnits", "productionUnits", "transmissionUnits", "consumptionUnits", "otherUnits"):
            for unit in raw.get(kind) or []:
                areas = [unit[k] for k in ("outAreaName", "inAreaName", "areaName") if unit.get(k)]
                if not set(areas).intersection(ZONES):
                    continue
                for period in unit.get("timePeriods") or []:
                    a, b = (pd.to_datetime(period.get(k), utc=True, errors="coerce") for k in ("eventStart", "eventStop"))
                    if pd.isna(a) or pd.isna(b) or a >= end or b <= start:
                        continue
                    output.append({"Piirkond": " → ".join(areas), "Objekt": unit.get("name"),
                                   "Mõjutatud MW": period.get("unavailableCapacity"),
                                   "Algus": local_time(a), "Lõpp": local_time(b),
                                   "Avaldatud": local_time(pd.Timestamp(msg["publication_time"])) if msg["publication_time"] else None,
                                   "Teade": msg["source_url"]})
    return output


@st.cache_data(ttl=900, show_spinner=False)
def border_data(day, neighbor, _token):
    start, end = bounds(day)

    def direction(pair):
        a, b = pair
        ntc, _ = fetch_entsoe_day_ahead_ntc(_token, start, end, a, b)
        flow, _ = fetch_entsoe_physical_flow(_token, start, end, a, b)
        xml, _ = _entsoe_get_xml(_token, {
            "documentType": "A09", "contract_MarketAgreement.Type": "A01",
            "out_Domain": ENTSOE_DOMAINS[a], "in_Domain": ENTSOE_DOMAINS[b],
            "periodStart": start.strftime("%Y%m%d%H%M"), "periodEnd": end.strftime("%Y%m%d%H%M"),
        }, retries=1)
        schedule = _parse_entsoe_timeseries(xml, value_name="schedule") if xml else pd.DataFrame()
        return f"{a} → {b}", ntc, flow, schedule

    with ThreadPoolExecutor(max_workers=2) as pool:
        return list(pool.map(direction, [(neighbor, "EE"), ("EE", neighbor)]))


def interval_value(frame, column, instant):
    if frame.empty:
        return None
    durations = pd.to_timedelta(frame["resolution"])
    values = frame.loc[(frame.time_utc <= instant) & (instant < frame.time_utc + durations), column].unique()
    return float(values[0]) if len(values) == 1 else None


@st.fragment
def render_policy(prices, token, today):
    st.subheader("Eesti hinnavahe ja katkestused")
    if prices.empty:
        st.info("Piirkondlikud päev-ette hinnad pole saadaval.")
        return
    days = sorted(prices.loc[prices.region.eq("EE"), "time_local"].dt.date.unique())
    if not days:
        st.info("Eesti avaldatud hinnaintervallid puuduvad.")
        return
    day = st.selectbox("Tarnepäev (Eesti aeg)", days, index=days.index(today) if today in days else len(days)-1)
    if not st.toggle("Ava hinnavahe analüüs", key="policy_enabled"):
        return
    data = prices.loc[prices.time_local.dt.date.eq(day), ["region", "time_utc", "price"]]
    if token:
        data = pd.concat([data, swedish_prices(day, token)], ignore_index=True)
    table = data.pivot_table(index="time_utc", columns="region", values="price", aggfunc="first").reindex(columns=ZONES)
    table.index = table.index.tz_convert("Europe/Tallinn")
    st.line_chart(table, y_label="€/MWh")
    missing = table.columns[table.isna().all()].tolist()
    if missing:
        st.caption("Piirkonnaandmed puuduvad: " + ", ".join(missing))
    intervals = table.EE.dropna().sort_values(ascending=False)
    if intervals.empty:
        st.info("Valitud päeval puuduvad Eesti hinnad.")
        return
    instant = st.selectbox("Hinnaintervall (kalleim esimesena)", intervals.index,
                           format_func=lambda t: f"{local_time(t)} · {intervals[t]:.2f} €/MWh")
    prices_at = table.loc[instant]
    st.dataframe(pd.DataFrame({"Hind €/MWh": prices_at, "EE miinus piirkond €/MWh": prices_at.EE - prices_at}).round(2))
    st.markdown("#### Intervalliga kattuvad Nord Pooli katkestusteated")
    try:
        rows, complete = notices(day)
        step = intervals.index.sort_values().to_series().diff().dropna().median()
        finish = instant + (pd.Timedelta(hours=1) if step == pd.Timedelta(hours=1) else pd.Timedelta(minutes=15))
        matches = overlapping(rows, instant, finish)
        if matches:
            st.dataframe(pd.DataFrame(matches).drop_duplicates(), hide_index=True,
                         column_config={"Teade": st.column_config.LinkColumn(display_text="Nord Pool")})
        else:
            st.info("Päringuga saadud teadetes kattuvaid andmeperioode ei leitud.")
        if not complete:
            st.warning("Päringu katvus on osaline.")
    except (requests.RequestException, ValueError, KeyError, TypeError):
        st.warning("Nord Pooli teadete andmed ei ole praegu kättesaadavad.")
    st.caption("Kuvatakse viimase teateversiooni andmed. Teade võib olla avaldatud pärast hinna kujunemist. Kattumine üksi ei tõenda hinnamõju.")
    st.markdown("#### Piiriandmed valitud intervalli alguses")
    neighbor = st.radio("Eesti piir", ["FI", "LV"], horizontal=True)
    if token:
        values = [{"Suund": label, "Päev-ette NTC (MW)": interval_value(ntc, "ntc_mw", instant),
                   "Päev-ette kaubanduslik vahetus (MW)": interval_value(schedule, "schedule", instant),
                   "Tegelik füüsiline voog (MW)": interval_value(flow, "flow_mw", instant)}
                  for label, ntc, flow, schedule in border_data(day, neighbor, token)]
        st.dataframe(values, hide_index=True)
    else:
        st.info("Piiriandmed ja Rootsi hinnad vajavad ENTSO-E ühendust.")
    st.caption("ENTSO-E suunapõhised andmed. Tühi väli tähendab puuduvat või vastuolulist väärtust. Füüsiline voog selgub pärast tarnet.")
