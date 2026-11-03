"""Daily outage inventory and weather-dependent production, in Tallinn delivery days."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import re
import xml.etree.ElementTree as ET

import pandas as pd
import requests
import streamlit as st

from energy_sources import ENTSOE_DOMAINS, _entsoe_get_xml
from fetch_entsoe_day_ahead_map import COUNTRIES
from umm_client import UMM_API, normalize_message

TZ = "Europe/Tallinn"
ZONES = ("EE", "FI", "LV", "LT", "SE1", "SE2", "SE3", "SE4")
TECH = {"Tuul": ("B18", "B19"), "Päike": ("B16",)}


def bounds(day):
    start = pd.Timestamp(day, tz=TZ)
    return start.tz_convert("UTC"), (start + pd.DateOffset(days=1)).tz_convert("UTC")


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


def stamp(t):
    return t.tz_convert(TZ).strftime("%d.%m %H:%M %z")


def outage_rows(messages, day):
    start, end = bounds(day)
    rows = []
    for raw in messages:
        if raw.get("isOutdated") or raw.get("eventStatus") == 3 or raw.get("cancellationReason"):
            continue
        for kind in ("productionUnits", "generationUnits", "transmissionUnits"):
            for unit in raw.get(kind) or []:
                transmission = kind == "transmissionUnits"
                areas = [unit.get(k) for k in (("inAreaName", "outAreaName") if transmission else ("areaName",))]
                if not set(areas).intersection(ZONES):
                    continue
                identity = " ".join(str(unit.get(k) or "") for k in ("inAreaName","outAreaName","name","inAreaEic","outAreaEic"))
                if transmission and re.search(r"\bRU(?:S|SSIA)?(?:\d+)?\b|10YRU|KALININGRAD|VENEMAA|RUSSIAN", identity, re.I):
                    continue
                msg = normalize_message(raw)
                for period in unit.get("timePeriods") or []:
                    a, b = (pd.to_datetime(period.get(k), utc=True, errors="coerce") for k in ("eventStart","eventStop"))
                    if pd.isna(a) or pd.isna(b) or min(b,end) <= max(a,start):
                        continue
                    loss, avail = (pd.to_numeric(period.get(k), errors="coerce") for k in ("unavailableCapacity","availableCapacity"))
                    nominal = pd.to_numeric(unit.get("installedCapacity"), errors="coerce")
                    if pd.isna(loss) and nominal >= avail >= 0:
                        loss = nominal - avail
                    if loss <= 0:
                        continue
                    rows.append({"Liik": "Ülekanne" if transmission else "Tootmine",
                                 "Piirkond / suund": " → ".join(x or "?" for x in areas),
                                 "Objekt": " / ".join(filter(None,(unit.get("productionUnitName"),unit.get("name")))),
                                 "Ajavahemik päevas": f"{stamp(max(a,start))} – {stamp(min(b,end))}",
                                 "Mõjutatud MW": loss, "Saadaval MW": avail,
                                 "Perioodi lõpp": stamp(b), "Põhjus": msg["reason"], "UMM": msg["source_url"]})
    return pd.DataFrame(rows).drop_duplicates()


def power_series(xml):
    """Expand ENTSO-E block intervals to quarter hours without bridging missing A01 points."""
    rows = []
    for ts in ET.fromstring(xml).findall(".//{*}TimeSeries"):
        code = ts.findtext(".//{*}psrType")
        if code not in ("B16","B18","B19") or ts.find(".//{*}outBiddingZone_Domain.mRID") is not None:
            continue
        for period in ts.findall("{*}Period"):
            start = pd.Timestamp(period.findtext("{*}timeInterval/{*}start"))
            end = pd.Timestamp(period.findtext("{*}timeInterval/{*}end"))
            step = pd.Timedelta(period.findtext("{*}resolution"))
            points = sorted((int(p.findtext("{*}position")), pd.to_numeric(p.findtext("{*}quantity"),errors="coerce"))
                            for p in period.findall("{*}Point"))
            for i,(pos,val) in enumerate(points):
                a = start + (pos-1)*step
                b = min(a+step,end)
                if ts.findtext("{*}curveType") == "A03":
                    b = min(start+(points[i+1][0]-1)*step,end) if i+1<len(points) else end
                if pd.notna(val) and val>=0 and b>a and step >= pd.Timedelta(minutes=15):
                    rows.extend((t,code,val) for t in pd.date_range(a,b,freq="15min",inclusive="left"))
    if not rows:
        return pd.DataFrame()
    values = pd.DataFrame(rows,columns=["time","type","MW"]).groupby(["time","type"]).MW
    return values.first().where(values.nunique()==1).unstack()


@st.cache_data(ttl=900, show_spinner=False)
def renewable_data(day, zone, _token):
    domain = COUNTRIES["SE"]["zones"][zone] if zone.startswith("SE") else ENTSOE_DOMAINS[zone]
    start,end = bounds(day)
    past,_ = bounds(day-timedelta(days=30))
    def read(doc, a, b):
        xml,_ = _entsoe_get_xml(_token, {"documentType":doc,"processType":"A01" if doc=="A69" else "A16",
              "in_Domain":domain,"periodStart":a.strftime("%Y%m%d%H%M"),"periodEnd":b.strftime("%Y%m%d%H%M")},retries=1)
        return power_series(xml) if xml else pd.DataFrame()
    with ThreadPoolExecutor(max_workers=2) as pool:
        forecast = pool.submit(read,"A69",start,end)
        actual = pool.submit(read,"A75",past,min(end,pd.Timestamp.now(tz="UTC").ceil("15min")))
        return forecast.result(),actual.result()


def daily_comparison(day, forecast, actual, now):
    start,end = bounds(day)
    grid = pd.date_range(start,end,freq="15min",inclusive="left")
    elapsed = grid[grid + pd.Timedelta(minutes=15) <= now]
    result = []
    for tech,codes in TECH.items():
        # Require all types published by each source for that technology.
        def combined(frame):
            cols = [c for c in codes if c in frame]
            return frame[cols].sum(axis=1,min_count=len(cols)) if cols else pd.Series(dtype=float,index=pd.DatetimeIndex([],tz="UTC"))
        f = combined(forecast).reindex(grid)
        actual_mw = combined(actual)
        measured = actual_mw.reindex(elapsed).dropna()
        baseline = []
        for back in range(1,31):
            a,b = bounds(day-timedelta(days=back))
            hist = actual_mw.reindex(pd.date_range(a,b,freq="15min",inclusive="left"))
            if hist.notna().all():
                baseline.append(hist.mean())
        ref = pd.Series(baseline,dtype=float).quantile(.9) if len(baseline)>=20 else None
        hours = len(grid)/4
        ref_energy = ref*hours if ref is not None else None
        f_energy = f.sum()/4 if f.notna().all() else None
        actual_energy = measured.sum()/4 if len(measured) else None
        result.append({"Allikas":tech,"Päevaprognoos MWh":f_energy,"Prognoosi tipp MW":f.max() if f_energy is not None else None,
                       "Heade olude võrdlus MWh":ref_energy,"Prognoos / võrdlus %":100*f_energy/ref_energy if f_energy is not None and ref_energy and ref_energy>0 else None,
                       "Mõõdetud MWh":actual_energy,"Mõõdetud h":len(measured)/4,
                       "Tegelik / võrdlus %":100*actual_energy/ref_energy if len(measured)==len(grid) and ref_energy and ref_energy>0 else None,
                       "Võrdluspäevi":len(baseline)})
    return pd.DataFrame(result).round(1)


@st.fragment
def render_policy(token,today):
    st.subheader("Mõjutatud võimsused")
    day = st.date_input("Ülevaate päev (Eesti aeg)",value=today,min_value=today-timedelta(days=365),max_value=today+timedelta(days=1))
    start,end = bounds(day)
    st.caption(f"Päevaülevaade · {day:%d.%m.%Y} · {(end-start).total_seconds()/3600:g} tundi · v5")
    try:
        raw,complete = notices(day)
        data = outage_rows(raw,day)
        if not complete:
            st.warning("Nord Pooli päring kattis ainult osa teadetest.")
        for kind,title in (("Tootmine","Piiratud elektrijaamad"),("Ülekanne","Piiratud ülekandesuunad")):
            st.markdown(f"#### {title}")
            d = data[data.Liik.eq(kind)].drop(columns="Liik") if not data.empty else data
            if d.empty:
                st.caption("Saadud teadetes selle päevaga kattuvat piirangut ei leitud.")
                continue
            # Keep distinct segments in details; headline peaks need not occur simultaneously.
            keys = ["Piirkond / suund","Objekt"]
            summary = d.groupby(keys,as_index=False)["Mõjutatud MW"].max().sort_values("Mõjutatud MW",ascending=False)
            top = summary.iloc[0]
            st.write(f"{len(summary)} objekti või suunda. " + (f"Suurim teatatud piirang: {top['Objekt']} ({top['Piirkond / suund']}), {top['Mõjutatud MW']:g} MW." if pd.notna(top['Mõjutatud MW']) else "Mõjutatud võimsus pole avaldatud."))
            st.dataframe(summary.rename(columns={"Mõjutatud MW":"Suurim päevane piirang MW"}), hide_index=True)
            with st.expander("Kellaajad, saadaolev võimsus ja algteated – "+kind.lower()):
                st.dataframe(d.sort_values(keys+["Ajavahemik päevas"]),hide_index=True,column_config={"UMM":st.column_config.LinkColumn(display_text="Teade")})
    except (requests.RequestException,KeyError,ValueError,TypeError):
        st.warning("Nord Pooli päevateateid ei õnnestunud lugeda.")
    st.caption("Allikas: Nord Pool UMM. Kattuvaid teateid ja eri kellaaegade tippe ei liideta. Ühenduse teates antud võimsus võib erineda oksjonile antud võimsusest.")
    st.markdown("#### Tuule- ja päikesetoodang samal päeval")
    zone = st.selectbox("Toodangu hinnapiirkond",ZONES)
    if not token:
        st.info("Prognoosi ja mõõteandmete laadimiseks on vaja ENTSO-E ühendust.")
        return
    with st.spinner("Laadin prognoosi ja mõõdetud toodangut…"):
        f,a = renewable_data(day,zone,token)
    now = pd.Timestamp.now(tz="UTC")
    st.dataframe(daily_comparison(day,f,a,now),hide_index=True)
    st.caption("ENTSO-E päev-ette prognoos (A69) ja tegelik tootmine (A75). Heade olude võrdlus on eelmise 30 päeva täielike päevade keskmise MW 90. protsentiil, korrutatud valitud päeva tundidega. Vaja on vähemalt 20 täielikku päeva. See on saavutatud tootmistaseme võrdlus, mitte paigaldatud võimsus ega ilmamudel.")
    st.caption("Mõõdetud MWh hõlmab ainult avaldatud lõppenud intervalle. Tegelikku toodangut võrreldakse kogu päeva võrdlustasemega siis, kui kogu päev on mõõdetud. Tühi lahter tähendab ebapiisavaid andmeid.")
