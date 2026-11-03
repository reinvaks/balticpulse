"""Regional prices and outages by Tallinn delivery interval."""
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
import streamlit as st

from energy_sources import (
    fetch_elering_prices, ENTSOE_DOMAINS, _entsoe_get_xml, _parse_entsoe_timeseries,
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
        if raw.get("isOutdated") or raw.get("eventStatus") == 3 or raw.get("cancellationReason"):
            continue
        msg = normalize_message(raw)
        for kind in ("generationUnits", "productionUnits", "transmissionUnits", "consumptionUnits", "otherUnits"):
            for unit in raw.get(kind) or []:
                areas = [unit[k] for k in ("inAreaName", "outAreaName", "areaName") if unit.get(k)]
                if not set(areas).intersection(ZONES):
                    continue
                for period in unit.get("timePeriods") or []:
                    a, b = (pd.to_datetime(period.get(k), utc=True, errors="coerce") for k in ("eventStart", "eventStop"))
                    if pd.isna(a) or pd.isna(b) or a >= end or b <= start:
                        continue
                    output.append({"Piirkond": " → ".join(areas), "Objekt": " / ".join(filter(None, (unit.get("productionUnitName"), unit.get("name")))),
                                   "_direction": tuple(areas[:2]) if kind == "transmissionUnits" and unit.get("outAreaName") and unit.get("inAreaName") else (),
                                   "_generation_area": unit.get("areaName") if kind in ("generationUnits", "productionUnits") else None,
                                   "_published": pd.to_datetime(msg["publication_time"], utc=True, errors="coerce"),
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



@st.cache_data(ttl=900, show_spinner=False)
def price_history(day):
    start, _ = bounds(day)
    history, _ = fetch_elering_prices(start - pd.Timedelta(days=8), start)
    return history


@st.cache_data(ttl=900, show_spinner=False)
def forecasts(day, _token):
    """Request TSO day-ahead series once for the delivery day and its reference week."""
    start, end = bounds(day)
    def load(job):
        region, doc = job
        domain = ENTSOE_DOMAINS[region]
        xml, _ = _entsoe_get_xml(_token, {
            "documentType": doc, "processType": "A01",
            ("in_Domain" if doc == "A69" else "outBiddingZone_Domain"): domain,
            "periodStart": (start - pd.Timedelta(days=8)).strftime("%Y%m%d%H%M"),
            "periodEnd": end.strftime("%Y%m%d%H%M"),
        }, retries=1)
        frame = _parse_entsoe_timeseries(xml, value_name="mw") if xml else pd.DataFrame()
        return job, frame
    jobs = [(region, doc) for region in ("EE", "FI", "LV", "LT") for doc in ("A69", "A65")]
    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(pool.map(load, jobs))


def reference(series, instant):
    """Daily observations at the same Tallinn clock time. Skip ambiguous DST times."""
    times = series.index.tz_convert("Europe/Tallinn")
    delta = (instant.tz_localize(None).normalize() - times.tz_localize(None).normalize()).days
    peers = series[(delta >= 1) & (delta <= 7) & (times.hour == instant.hour) & (times.minute == instant.minute)].dropna()
    dates = pd.Index(peers.index.date)
    return peers[~dates.duplicated(keep=False)]


def forecast_samples(frame, instant, types=None):
    if frame.empty:
        return pd.Series(dtype=float)
    parts = [frame] if types is None else [frame[frame.psr_type.eq(k)] for k in types if frame.psr_type.eq(k).any()]
    if not parts:
        return pd.Series(dtype=float)
    samples = {}
    for offset in range(8):
        wall = instant.tz_localize(None) - pd.Timedelta(days=offset)
        at = instant if offset == 0 else wall.tz_localize("Europe/Tallinn", ambiguous="NaT", nonexistent="NaT")
        if pd.isna(at):
            continue
        values = [interval_value(part, "mw", at) for part in parts]
        if all(v is not None and v >= 0 for v in values):
            samples[at] = sum(values)
    return pd.Series(samples, dtype=float)


def level_summary(price, instant, history, forecasts_by_area, matches, complete):
    lines = []
    peers = pd.Series(dtype=float)
    if not history.empty:
        ee = history[history.region.eq("EE")].drop_duplicates("time_local")
        if not ee.empty:
            peers = reference(ee.set_index("time_local").price, instant)
    level = "unknown"
    if len(peers) >= 4:
        median = peers.median()
        week = ee.loc[(ee.time_local < instant.normalize()) & (ee.time_local >= instant.normalize() - pd.DateOffset(days=7)), "price"].dropna()
        level = "low" if price < peers.quantile(.25) else "high" if price > peers.quantile(.75) else "middle"
        wording = {"low": "nädala samal kellaajal pigem madal", "high": "nädala samal kellaajal pigem kõrge", "middle": "nädala samal kellaajal tavapärane"}
        lines.append(f"**Eesti hind {price:.2f} €/MWh** oli {wording[level]}. Eelmise nädala {len(peers)} võrreldava päeva mediaan oli {median:.2f} €/MWh.")
        if len(week) >= 96:
            rank = 100 * week.lt(price).mean()
            lines.append(f"Kogu eelmise nädala võrdluses oli {rank:.0f}% avaldatud hinnaintervallidest sellest odavamad.")
            if level == "middle":
                level = "high" if rank >= 80 else "low" if rank <= 20 and price < week.median() else level
    else:
        lines.append(f"**Eesti hind {price:.2f} €/MWh.** Nädala sama kellaaja hinnavõrdluseks on liiga vähe andmeid.")
    signals = []
    for region in ("EE", "FI", "LV", "LT"):
        gen = forecasts_by_area.get((region, "A69"), pd.DataFrame())
        demand = forecasts_by_area.get((region, "A65"), pd.DataFrame())
        for name, frame, types in (("tuuletootmine",gen,("B18","B19")),("päikesetootmine",gen,("B16",)),("tarbimine",demand,None)):
            samples = forecast_samples(frame, instant, types)
            if instant not in samples:
                continue
            past = samples.drop(instant)
            if len(past) < 4:
                continue
            val = samples[instant]
            base = past.median()
            change = val - base
            if abs(change) < max(10, base * .15):
                continue
            upward_pressure = (change > 0) == (name == "tarbimine")
            if (level == "high" and upward_pressure) or (level == "low" and not upward_pressure):
                signals.append((region, abs(change), f"{region}: prognoositud {name} {val:.0f} MW "
                                f"(sama kellaaja mediaan {base:.0f} MW; vahe {change:+.0f} MW)"))
    if signals:
        signals.sort(key=lambda x: (x[0] != "EE", -x[1]))
        lines.append("**Prognoosid toetavad seda hinnasuunda.** " + ". ".join(v[2] for v in signals[:3]) +
                     ". " + ("Suurem taastuvtootmine või väiksem nõudlus vähendab vajadust kallimate pakkumiste järele." if level=="low" else
                              "Väiksem taastuvtootmine või suurem nõudlus suurendab vajadust muude tootmisallikate või impordi järele."))
    elif level in ("high", "low"):
        lines.append("Tuule, päikese ja nõudluse prognoosidest ei leitud piisavat võrdlusandmetega kinnitatud selgitust.")
    cutoff = (instant.tz_convert("Europe/Brussels").normalize() - pd.DateOffset(days=1)) + pd.DateOffset(hours=12)
    eligible = [m for m in matches if pd.notna(m["_published"]) and m["_published"] < cutoff
                and pd.to_numeric(m["Mõjutatud MW"], errors="coerce") > 0]
    for title, key in (("Turult väljas tootmisvõimsus","_generation_area"), ("Vähendatud ülekandevõimsus","_direction")):
        def priority(m):
            areas = set(m["_direction"] or (m["_generation_area"],))
            return (0 if "EE" in areas else 1 if areas.intersection({"FI", "LV", "LT"}) else 2,
                    -float(m["Mõjutatud MW"]))
        group = sorted((m for m in eligible if m[key]), key=priority)
        entries = {}
        for m in group:
            identity = (m["Piirkond"], m["Objekt"])
            entries.setdefault(identity, m)
        if entries:
            shown = list(entries.values())[:3]
            details = [f"[{m['Objekt'] or m['Piirkond']}]({m['Teade']}) ({m['Piirkond']}), {float(m['Mõjutatud MW']):g} MW" for m in shown]
            lines.append(f"**{title}.** " + " · ".join(details) + ".")
    if any(m["_generation_area"] or m["_direction"] for m in eligible):
        lines.append("Need enne oksjonit avaldatud piirangud vähendasid piirkonna tootmis- või ülekandevõimalusi. " +
                     ("Madal hind kujunes neist piirangutest hoolimata." if level=="low" else
                      "Üksiku katkestuse mõju Eesti hinnale jääb määramata. Piiride tegelikku täitumist kontrollib allolev hinnavahe analüüs."))
    elif complete:
        lines.append("Kontrollitud teadete viimaste versioonide seas ei leitud enne oksjonit avaldatud kattuvat tootmis- ega ülekandepiirangut.")
    return lines

def explain(table, instant, borders, matches):
    prices = table.loc[instant]
    neighbors = {"FI": "Soome", "LV": "Läti"}
    known = [n for n in neighbors if pd.notna(prices[n])]
    known.sort(key=lambda n: abs(prices.EE - prices[n]), reverse=True)
    lines = [f"**Eestis maksis elekter {prices.EE:.2f} €/MWh** ({local_time(instant)}, intervalli algus)."]
    if not known:
        return lines + ["Soome ja Läti piirkonnahinnad puuduvad. Hinnaerinevust ega selle põhjust ei saa hinnata."]
    if all(round(prices.EE - prices[n], 2) == 0 for n in known):
        names = " ja ".join(neighbors[n] for n in known)
        lines.append(f"Eesti hind oli sama mis {names} hind. Nende piirkondade vahel selles intervallis hinnavahet ei tekkinud.")
    # The auction date follows the Central European market delivery day.
    cutoff = (instant.tz_convert("Europe/Brussels").normalize() - pd.DateOffset(days=1)) + pd.DateOffset(hours=12)
    for n in known:
        gap = round(prices.EE - prices[n], 2)
        if gap == 0:
            if len(known) > 1 and any(round(prices.EE - prices[k], 2) for k in known):
                lines.append(f"**{neighbors[n]}.** Hind oli Eestiga sama. Sellel piiril hinnad ei lahknenud.")
            continue
        cheap, costly = (n, "EE") if gap > 0 else ("EE", n)
        label = f"{cheap} → {costly}"
        direct = borders.get(label, {})
        reverse = borders.get(f"{costly} → {cheap}", {})
        capacity = direct.get("Päev-ette võimsus (MW)")
        scheduled = direct.get("Kaubanduslik vahetus (MW)")
        opposite = reverse.get("Kaubanduslik vahetus (MW)")
        prefix = f"**{neighbors[n]}.** Eesti hind oli {abs(gap):.2f} €/MWh {'kõrgem' if gap > 0 else 'madalam'} ({neighbors[n]} hind {prices[n]:.2f} €/MWh). "
        if capacity == 0:
            lines.append(prefix + f"Odavamast piirkonnast kallimasse ({label}) oli avaldatud päev-ette ülekandevõimsus 0 MW. See on otsene takistus odavama elektri turule pääsemisel selle piiri kaudu."
                         if scheduled in (None, 0) else prefix + "Avaldatud nullvõimsus ja positiivne kaubanduslik vahetus on vastuolus. Piiri roll jääb lahtiseks.")
        elif capacity is None or scheduled is None or opposite is None:
            lines.append(prefix + f"Suunal {label} puudub võimsus või täielik vahetusgraafik. Seetõttu ei saa kinnitada, kas odavama elektri sissevedu jõudis piirini.")
        else:
            net = scheduled - opposite
            evidence = f"Suunal {label} oli päev-ette netovahetus {net:.1f} MW ja avaldatud võimsus {capacity:.1f} MW. "
            if capacity > 0 and net > 0 and abs(capacity - net) <= 0.5:
                lines.append(prefix + evidence + "Vahetus jõudis avaldatud piirini (võrdlustäpsus 0,5 MW). See toetab selgitust, et selle piiri kaudu ei saanud odavamat elektrit rohkem sisse tuua.")
            elif capacity > net >= 0:
                lines.append(prefix + evidence + f"Avaldatud piirini jäi {capacity-net:.1f} MW. Nende andmete põhjal ei saa hinnavahet selle piiri täitumisega põhjendada.")
            else:
                lines.append(prefix + evidence + "Need näitajad ei kinnita, et hinnavahe tekkis selle piiri täitumisest.")
        eligible = [m for m in matches if m["_direction"] == (cheap, costly) and pd.notna(m["_published"])
                    and m["_published"] < cutoff and pd.to_numeric(m["Mõjutatud MW"], errors="coerce") > 0]
        if eligible:
            m = max(eligible, key=lambda x: float(x["Mõjutatud MW"]))
            lines.append(f"Enne oksjonit avaldatud [katkestusteade]({m['Teade']}) kattis sama suunda ja intervalli: "
                         f"{m['Objekt'] or label}, mõjutatud võimsus {float(m['Mõjutatud MW']):g} MW. "
                         "Teade kinnitab piirangut. Selle eraldi hinnamõju eurodes ei ole mõõdetud.")
    if len(known) == 1:
        absent = next(n for n in neighbors if n not in known)
        lines.append(f"{neighbors[absent]} hind selles intervallis puudub.")
    if known and round(prices.EE - prices[known[0]], 2):
        n = known[0]
        comparable = table[["EE", n]].dropna()
        spreads = (comparable.EE - comparable[n]).round(2)
        sign = spreads.loc[instant] > 0
        count = int(((spreads > 0) if sign else (spreads < 0)).sum())
        lines.append(f"**Päeva võrdlus.** Eesti oli {neighbors[n]} piirkonnast {'kallim' if sign else 'odavam'} "
                     f"{count} intervallis {len(comparable)} võrreldavast. See näitab, kui sageli sama erinevus päeva jooksul esines.")
    return lines


@st.fragment
def render_policy(prices, token, today):
    st.subheader("Eesti hinnatase, hinnavahe ja katkestused")
    st.caption("Hinnaanalüüsi versioon 3.11.2026 v3")
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
    step = intervals.index.sort_values().to_series().diff().dropna().median()
    finish = instant + (pd.Timedelta(hours=1) if step == pd.Timedelta(hours=1) else pd.Timedelta(minutes=15))
    try:
        rows, complete = notices(day)
        matches = overlapping(rows, instant, finish)
        notice_warning = "" if complete else "Nord Pooli päring kattis ainult osa teadetest."
    except (requests.RequestException, ValueError, KeyError, TypeError):
        matches, notice_warning = [], "Nord Pooli katkestusteated ei ole praegu kättesaadavad."

    borders = {}
    if token:
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = {n: pool.submit(border_data, day, n, token) for n in ("FI", "LV")}
            for n, job in jobs.items():
                for label, ntc, flow, schedule in job.result():
                    borders[label] = {
                        "Päev-ette võimsus (MW)": interval_value(ntc, "ntc_mw", instant),
                        "Kaubanduslik vahetus (MW)": interval_value(schedule, "schedule", instant),
                        "Füüsiline voog (MW)": interval_value(flow, "flow_mw", instant),
                    }

    st.markdown("#### Mis selgitab Eesti hinnataset?")
    history = price_history(day)
    outlook = forecasts(day, token) if token else {}
    for paragraph in level_summary(prices_at.EE, instant, history, outlook, matches, not notice_warning):
        st.markdown(paragraph)
    st.caption("Hinnataseme võrdlus: eelmise 7 päeva sama Eesti kellaaeg, vähemalt 4 vaatlust; madal alla alumise ja kõrge üle ülemise kvartiili. "
               "Kui sama kellaaja hind on keskmises vahemikus, kontrollitakse lisaks kogu eelmise nädala alumist ja ülemist viiendikku. Prognoosivõrdluses tuuakse välja vähemalt 15% ja 10 MW erinevused. Allikas: ENTSO-E päev-ette tuule- ja päikeseprognoos A69 ning tarbimisprognoos A65. "
               "Prognoose võidakse avaldada või uuendada pärast oksjonit; need aitavad hinnata turuolusid tagantjärele.")
    st.markdown("#### Miks hind naabritega võrreldes erines?")
    for paragraph in explain(table, instant, borders, matches):
        st.markdown(paragraph)
    if notice_warning:
        st.caption(notice_warning)
    st.caption("Allikad: Eleringi päev-ette hinnad, ENTSO-E päev-ette võimsus (A61) ja kaubanduslik vahetus (A09), Nord Pooli UMM. "
               "[Kuidas piirkonnahind kujuneb](https://www.nordpoolgroup.com/en/trading/Day-ahead-trading/Price-calculation/)")
    st.line_chart(table, y_label="€/MWh")
    with st.expander("Vaata arvutuse aluseks olevaid hindu, piiriandmeid ja teateid"):
        st.dataframe(pd.DataFrame({"Hind €/MWh": prices_at, "EE miinus piirkond €/MWh": prices_at.EE - prices_at}).round(2))
        if borders:
            st.dataframe(pd.DataFrame.from_dict(borders, orient="index"))
        if matches:
            frame = pd.DataFrame(matches).drop(columns=["_published", "_direction", "_generation_area"]).drop_duplicates()
            st.dataframe(frame, hide_index=True, column_config={"Teade": st.column_config.LinkColumn(display_text="Nord Pool")})
        elif not notice_warning:
            st.caption("Päringu vastuses ei olnud selle intervalliga kattuvaid teateid.")
