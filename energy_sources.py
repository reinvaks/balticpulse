from __future__ import annotations

import logging
import re
import time
from io import BytesIO, StringIO
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests

LOG = logging.getLogger(__name__)
TALLINN = ZoneInfo("Europe/Tallinn")

ELERING_BASE = "https://dashboard.elering.ee/api"
VOLTON_BASE = "https://public-data.volton.energy/v1"
AGSI_BASE = "https://agsi.gie.eu/api"
ENTSOE_BASE = "https://web-api.tp.entsoe.eu/api"
EEX_TTF_HISTORY_URL = "https://gasandregistry.eex.com/Gas/NGP/TTF_NGP_60_Days.csv"
EEX_NGP_CURRENT_URLS = {
    "TTF": "https://gasandregistry.eex.com/Gas/NGP/TTF_NGP_15_Mins.csv",
    "FIN": "https://gasandregistry.eex.com/Gas/NGP/FIN_NGP_15_Mins.csv",
    "LTU": "https://gasandregistry.eex.com/Gas/NGP/LTU_NGP_15_Mins.csv",
    "LVA-EST": "https://gasandregistry.eex.com/Gas/NGP/LVA-EST_NGP_15_Mins.csv",
}
EEX_EUA_AUCTION_URL = "https://public.eex-group.com/eex/eua-auction-report/emission-spot-primary-market-auction-report-2026-data.xlsx"
EIA_BRENT_XLS_URL = "https://www.eia.gov/dnav/pet/hist_xls/RBRTEd.xls"
EIA_BRENT_HTML_URL = "https://www.eia.gov/dnav/pet/hist/rbrteD.htm"

ENTSOE_DOMAINS = {
    "EE": "10Y1001A1001A39I",
    "FI": "10YFI-1--------U",
    "LV": "10YLV-1001A00074",
    "LT": "10YLT-1001A0008Q",
}

PSR_TYPES = {
    "B01": "Biomass", "B02": "Fossil Brown coal/Lignite", "B03": "Fossil Coal-derived gas",
    "B04": "Fossil Gas", "B05": "Fossil Hard coal", "B06": "Fossil Oil",
    "B07": "Fossil Oil shale", "B08": "Fossil Peat", "B09": "Geothermal",
    "B10": "Hydro Pumped Storage", "B11": "Hydro Run-of-river and poundage",
    "B12": "Hydro Water Reservoir", "B13": "Marine", "B14": "Nuclear",
    "B15": "Other renewable", "B16": "Solar", "B17": "Waste",
    "B18": "Wind Offshore", "B19": "Wind Onshore", "B20": "Other",
}


@dataclass
class SourceStatus:
    source: str
    ok: bool
    fetched_at: str
    url: str
    status_code: int | None = None
    error: str | None = None
    note: str | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_json(url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
              timeout: tuple[int, int] = (5, 25), retries: int = 3) -> tuple[Any | None, SourceStatus]:
    last_error = None
    last_status = None
    session = requests.Session()
    session.headers.update({"Accept": "application/json", "User-Agent": "Regional-Energy-Dashboard/2.0"})
    if headers:
        session.headers.update(headers)
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=timeout)
            last_status = r.status_code
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
            r.raise_for_status()
            try:
                return r.json(), SourceStatus(source=url, ok=True, fetched_at=_now_iso(), url=r.url, status_code=r.status_code)
            except ValueError as exc:
                return None, SourceStatus(source=url, ok=False, fetched_at=_now_iso(), url=r.url,
                                          status_code=r.status_code, error=f"Malformed JSON: {exc}")
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None, SourceStatus(source=url, ok=False, fetched_at=_now_iso(), url=url,
                              status_code=last_status, error=last_error)



def _get_bytes(url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None,
               timeout: tuple[int, int] = (5, 30), retries: int = 3) -> tuple[bytes | None, SourceStatus]:
    last_error = None
    last_status = None
    req_headers = {"User-Agent": "BalticPulse/1.0", "Accept": "*/*"}
    if headers:
        req_headers.update(headers)
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=req_headers, timeout=timeout)
            last_status = r.status_code
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
            r.raise_for_status()
            return r.content, SourceStatus(source=url, ok=True, fetched_at=_now_iso(), url=r.url, status_code=r.status_code)
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None, SourceStatus(source=url, ok=False, fetched_at=_now_iso(), url=url, status_code=last_status, error=last_error)


def _best_datetime_column(df: pd.DataFrame) -> tuple[str | None, pd.Series | None]:
    best_col = None
    best = None
    best_count = 0
    for col in df.columns:
        name = str(col).lower()
        if not any(k in name for k in ("date", "day", "time", "datum")):
            continue
        parsed = pd.to_datetime(df[col], errors="coerce", dayfirst=False)
        count = int(parsed.notna().sum())
        if count > best_count:
            best_col, best, best_count = str(col), parsed, count
    return best_col, best


def _best_numeric_column(df: pd.DataFrame, keywords: tuple[str, ...]) -> tuple[str | None, pd.Series | None]:
    candidates = []
    for col in df.columns:
        name = str(col).lower()
        score = sum(1 for k in keywords if k in name)
        nums = pd.to_numeric(df[col].astype(str).str.replace(",", ".", regex=False), errors="coerce")
        count = int(nums.notna().sum())
        if count:
            candidates.append((score, count, str(col), nums))
    if not candidates:
        return None, None
    candidates.sort(reverse=True, key=lambda x: (x[0], x[1]))
    _, _, col, nums = candidates[0]
    return col, nums


def fetch_eex_ngp_current(area: str = "TTF") -> tuple[pd.DataFrame, SourceStatus]:
    """EEX Neutral Gas Price current D/D+1/D+2 file, refreshed every 15 minutes.

    EEX explicitly documents these files as current NGP values. The parser is deliberately
    tolerant because EEX occasionally changes presentation column names; it only returns
    rows where both a delivery date and a plausible EUR/MWh price can be identified.
    """
    area = area.upper()
    url = EEX_NGP_CURRENT_URLS.get(area)
    if not url:
        return pd.DataFrame(), SourceStatus(source=f"EEX NGP {area}", ok=False, fetched_at=_now_iso(), error="Unsupported NGP area")
    raw, status = _get_bytes(url)
    status.source = f"EEX NGP {area} current (15-min refresh)"
    if raw is None:
        return pd.DataFrame(), status
    try:
        text = raw.decode("utf-8-sig", errors="replace")
        try:
            df = pd.read_csv(StringIO(text), sep=None, engine="python")
        except Exception:
            df = pd.read_csv(StringIO(text), sep=";", engine="python")
        df.columns = [str(c).strip() for c in df.columns]

        # Delivery date: prefer columns that semantically look like gas/delivery day.
        date_candidates = []
        for col in df.columns:
            lc = str(col).lower()
            parsed = pd.to_datetime(df[col], errors="coerce", dayfirst=False)
            score = 0
            if "gas" in lc and ("day" in lc or "date" in lc): score += 4
            if "deliver" in lc: score += 4
            if "date" in lc or "day" in lc: score += 2
            if parsed.notna().any(): date_candidates.append((score, int(parsed.notna().sum()), col, parsed))
        if not date_candidates:
            raise ValueError(f"Could not identify delivery date in columns {list(df.columns)}")
        date_candidates.sort(reverse=True, key=lambda x: (x[0], x[1]))
        _, _, dcol, dates = date_candidates[0]

        # Price: strongly prefer NGP/price columns and reject obvious volume/date fields.
        price_candidates = []
        for col in df.columns:
            lc = str(col).lower()
            if col == dcol or any(k in lc for k in ("volume", "time", "date", "day")):
                continue
            nums = pd.to_numeric(df[col].astype(str).str.replace(",", ".", regex=False), errors="coerce")
            valid = nums[(nums > -500) & (nums < 1000)]
            if valid.empty:
                continue
            score = 0
            if "ngp" in lc: score += 5
            if "price" in lc: score += 4
            if "eur" in lc: score += 2
            if area.lower().replace("-", "") in lc.replace("-", "").replace("_", ""): score += 2
            price_candidates.append((score, int(valid.notna().sum()), col, nums))
        if not price_candidates:
            raise ValueError(f"Could not identify NGP price in columns {list(df.columns)}")
        price_candidates.sort(reverse=True, key=lambda x: (x[0], x[1]))
        _, _, pcol, prices = price_candidates[0]

        out = pd.DataFrame({"delivery_date": dates.dt.date, "price_eur_mwh": prices}).dropna()
        out = out[(out["price_eur_mwh"] > -500) & (out["price_eur_mwh"] < 1000)]
        out = out.drop_duplicates("delivery_date", keep="last").sort_values("delivery_date")
        if out.empty:
            raise ValueError("No current NGP rows parsed")
        status.note = "Official EEX current NGP file; EEX states it is refreshed every 15 minutes for D/D+1/D+2."
        return out, status
    except Exception as exc:
        status.ok = False
        status.error = f"Current NGP CSV parse error: {exc}"
        return pd.DataFrame(), status


def fetch_eex_ttf_ngp() -> tuple[pd.DataFrame, SourceStatus]:
    """EEX Neutral Gas Price TTF — final daily history, public 60-day CSV.

    This is a spot-market TTF reference published by EEX, not a front-month futures price.
    """
    raw, status = _get_bytes(EEX_TTF_HISTORY_URL)
    status.source = "EEX Neutral Gas Price TTF (NGP TTF)"
    if raw is None:
        return pd.DataFrame(), status
    try:
        text = raw.decode("utf-8-sig", errors="replace")
        try:
            df = pd.read_csv(StringIO(text), sep=None, engine="python")
        except Exception:
            df = pd.read_csv(StringIO(text), sep=";", engine="python")
        df.columns = [str(c).strip() for c in df.columns]
        dcol, dates = _best_datetime_column(df)
        pcol, prices = _best_numeric_column(df, ("ttf", "ngp", "price", "eur", "value"))
        if dcol is None or dates is None or pcol is None or prices is None:
            raise ValueError(f"Could not identify date/price columns: {list(df.columns)}")
        out = pd.DataFrame({"date": dates, "price_eur_mwh": prices}).dropna().drop_duplicates("date").sort_values("date")
        if out.empty:
            raise ValueError("No TTF NGP rows parsed")
        status.note = "Final daily EEX NGP TTF history (public 60-day file); not TTF front-month futures."
        return out, status
    except Exception as exc:
        status.ok = False
        status.error = f"TTF CSV parse error: {exc}"
        return pd.DataFrame(), status


def _read_excel_flex(raw: bytes) -> list[pd.DataFrame]:
    book = pd.ExcelFile(BytesIO(raw))
    frames = []
    for sheet in book.sheet_names:
        try:
            frames.append(pd.read_excel(book, sheet_name=sheet, header=None))
        except Exception:
            continue
    return frames


def fetch_eia_brent() -> tuple[pd.DataFrame, SourceStatus]:
    """Official EIA Europe Brent Spot Price FOB daily series (USD/bbl).

    Primary route is the public EIA HTML history table, which is more stable for
    server-side apps than depending on the legacy XLS workbook layout. The XLS
    file remains a fallback. No synthetic or interpolated values are created.
    """
    raw_html, status = _get_bytes(EIA_BRENT_HTML_URL, headers={"Accept": "text/html,*/*"})
    status.source = "U.S. EIA Europe Brent Spot Price FOB"
    if raw_html:
        try:
            tables = pd.read_html(StringIO(raw_html.decode("utf-8", errors="replace")))
            best = pd.DataFrame()
            weekday_map = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4}
            for tab in tables:
                if tab.empty:
                    continue
                cols = [str(c) for c in tab.columns]
                if not any("Week Of" in c for c in cols):
                    continue
                week_col = next(c for c in tab.columns if "Week Of" in str(c))
                rows = []
                for _, r in tab.iterrows():
                    label = str(r.get(week_col, "")).strip()
                    m = re.search(r"(\d{4})\s+([A-Za-z]{3})-\s*(\d{1,2})", label)
                    if not m:
                        continue
                    try:
                        base = pd.Timestamp(datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%Y %b %d"))
                    except Exception:
                        continue
                    for col in tab.columns:
                        cname = str(col).strip()
                        day = next((k for k in weekday_map if cname.startswith(k)), None)
                        if day is None:
                            continue
                        val = pd.to_numeric(pd.Series([r.get(col)]), errors="coerce").iloc[0]
                        if pd.isna(val):
                            continue
                        d = base + pd.Timedelta(days=weekday_map[day])
                        rows.append({"date": d.normalize(), "price_usd_bbl": float(val)})
                out = pd.DataFrame(rows)
                if not out.empty:
                    out = out.drop_duplicates("date", keep="last").sort_values("date")
                    if len(out) > len(best):
                        best = out
            if not best.empty:
                status.note = "Official EIA Europe Brent Spot Price FOB daily history table; no interpolation."
                return best.tail(500), status
        except Exception as exc:
            status.note = f"HTML parse failed; trying official XLS fallback: {exc}"

    # Fallback to official EIA XLS download.
    raw, xls_status = _get_bytes(EIA_BRENT_XLS_URL)
    xls_status.source = "U.S. EIA Europe Brent Spot Price FOB"
    if raw is None:
        if status.error and not xls_status.error:
            xls_status.error = status.error
        return pd.DataFrame(), xls_status
    try:
        frames = _read_excel_flex(raw)
        best = pd.DataFrame()
        for raw_df in frames:
            for header_row in range(min(12, len(raw_df))):
                header = raw_df.iloc[header_row].astype(str).str.strip()
                df = raw_df.iloc[header_row + 1:].copy()
                df.columns = header
                _, dates = _best_datetime_column(df)
                _, prices = _best_numeric_column(df, ("brent", "dollar", "price", "value"))
                if dates is None or prices is None:
                    continue
                out = pd.DataFrame({"date": dates, "price_usd_bbl": prices}).dropna()
                out = out[(out["price_usd_bbl"] > 1) & (out["price_usd_bbl"] < 500)]
                out = out.drop_duplicates("date", keep="last").sort_values("date")
                if len(out) > len(best):
                    best = out
        if best.empty:
            raise ValueError("No Brent observations parsed from EIA HTML or XLS")
        xls_status.note = "Official EIA XLS fallback; no interpolation."
        return best.tail(500), xls_status
    except Exception as exc:
        xls_status.ok = False
        xls_status.error = f"EIA Brent parse error: {exc}"
        return pd.DataFrame(), xls_status

def fetch_eex_eua_auction() -> tuple[pd.DataFrame, SourceStatus]:
    """Official EEX EUA primary-auction clearing prices for 2026 (EUR/tCO2).

    EEX's interface specification defines the columns explicitly, including
    ``Auction date`` and ``Auction clearing price [€/tCO2]``. The parser therefore
    selects those semantic columns rather than guessing the most plausible numeric column.
    This is the primary-auction price, not a secondary-market EUA spot/futures quote.
    """
    raw, status = _get_bytes(EEX_EUA_AUCTION_URL)
    status.source = "EEX EUA Primary Auction clearing price"
    if raw is None:
        return pd.DataFrame(), status
    try:
        frames = _read_excel_flex(raw)
        candidates = []
        for raw_df in frames:
            if raw_df.empty:
                continue
            for header_row in range(min(30, len(raw_df))):
                headers = [str(x).strip() for x in raw_df.iloc[header_row].tolist()]
                low = [h.lower() for h in headers]
                date_idx = next((i for i,h in enumerate(low) if "auction date" in h), None)
                price_idx = next((i for i,h in enumerate(low) if "auction clearing price" in h), None)
                if date_idx is None or price_idx is None:
                    continue
                body = raw_df.iloc[header_row+1:].copy()
                dates = pd.to_datetime(body.iloc[:, date_idx], errors="coerce", dayfirst=False)
                prices = pd.to_numeric(
                    body.iloc[:, price_idx].astype(str)
                    .str.replace("€", "", regex=False)
                    .str.replace(",", ".", regex=False)
                    .str.strip(),
                    errors="coerce",
                )
                out = pd.DataFrame({"date": dates, "price_eur_tco2": prices}).dropna()
                out = out[(out["price_eur_tco2"] > 1) & (out["price_eur_tco2"] < 500)]
                out = out.drop_duplicates("date", keep="last").sort_values("date")
                if not out.empty:
                    candidates.append(out)
        if not candidates:
            raise ValueError("Columns 'Auction date' and 'Auction clearing price [€/tCO2]' not found or contained no values")
        best = max(candidates, key=len)
        status.note = "Official EEX primary-auction clearing price; not secondary-market EUA spot/futures."
        return best, status
    except Exception as exc:
        status.ok = False
        status.error = f"EUA auction XLSX parse error: {exc}"
        return pd.DataFrame(), status

def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fetch_elering_prices(start: datetime, end: datetime) -> tuple[pd.DataFrame, SourceStatus]:
    url = f"{ELERING_BASE}/nps/price"
    payload, status = _get_json(url, params={"start": _iso_utc(start), "end": _iso_utc(end)})
    status.source = "Elering Dashboard / Nord Pool day-ahead"
    if not status.ok or not isinstance(payload, dict):
        return pd.DataFrame(), status
    data = payload.get("data", {})
    if not isinstance(data, dict):
        status.ok = False
        status.error = "Unexpected Elering price response schema"
        return pd.DataFrame(), status
    rows: list[dict[str, Any]] = []
    for region in ("ee", "lv", "lt", "fi"):
        items = data.get(region, [])
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            ts = item.get("timestamp")
            price = item.get("price")
            try:
                ts = int(ts)
                price = float(price)
            except (TypeError, ValueError):
                continue
            rows.append({"timestamp": ts, "price": price, "region": region.upper()})
    if not rows:
        status.note = "HTTP request succeeded but no price rows were returned."
        return pd.DataFrame(), status
    df = pd.DataFrame(rows).drop_duplicates(["region", "timestamp"])
    df["time_utc"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    df["time_local"] = df["time_utc"].dt.tz_convert("Europe/Tallinn")
    return df.sort_values(["time_local", "region"]), status


def fetch_elering_system(start: datetime, end: datetime) -> tuple[pd.DataFrame, SourceStatus]:
    """Fetch Elering actual production and consumption.

    Elering's public ``system/with-plan`` endpoint can return CSV as well as JSON.
    CSV is preferred here because the column contract is flatter and more stable for the
    production/consumption pair. JSON remains a fallback so a presentation-format change
    does not blank the dashboard. Only actual source values are returned; planned values
    are explicitly excluded.
    """
    url = f"{ELERING_BASE}/system/with-plan"
    base_params = {
        "start": _iso_utc(start),
        "end": _iso_utc(end),
        "fields": "_datetime,production,consumption",
    }

    # Preferred route: the endpoint's explicit CSV export.  The historical/current
    # contract contains timestamp, actual production/consumption and planned columns.
    csv_params = {**base_params, "language": "et", "format": "csv"}
    raw, csv_status = _get_bytes(url, params=csv_params, headers={"Accept": "text/csv,*/*"})
    csv_status.source = "Elering electricity system (CSV)"
    if raw:
        try:
            text = raw.decode("utf-8-sig", errors="replace")
            # Elering CSV export is normally semicolon-delimited. Fall back to sniffing
            # to tolerate a delimiter change.
            try:
                df = pd.read_csv(StringIO(text), sep=";")
                if len(df.columns) <= 1:
                    df = pd.read_csv(StringIO(text), sep=None, engine="python")
            except Exception:
                df = pd.read_csv(StringIO(text), sep=None, engine="python")
            df.columns = [str(c).strip() for c in df.columns]

            # Timestamp: prefer the explicit UTC epoch column.
            time_col = next((c for c in df.columns if "ajatempel" in c.lower() or "timestamp" in c.lower()), None)
            if time_col is None:
                time_col = next((c for c in df.columns if "kuup" in c.lower() or "date" in c.lower() or "time" in c.lower()), None)
            if time_col is not None:
                vals = df[time_col]
                numeric = pd.to_numeric(vals, errors="coerce")
                if numeric.notna().sum() >= max(1, len(df) // 2):
                    unit = "ms" if numeric.dropna().median() > 10_000_000_000 else "s"
                    df["time_utc"] = pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce")
                else:
                    df["time_utc"] = pd.to_datetime(vals, utc=True, errors="coerce")

                def actual_col(kind: str) -> str | None:
                    keys = ("tootmine", "production") if kind == "production" else ("tarbimine", "consumption")
                    candidates = []
                    for c in df.columns:
                        lc = c.lower()
                        if any(k in lc for k in keys) and not any(k in lc for k in ("planeer", "planned", "forecast", "prognoos")):
                            candidates.append(c)
                    return candidates[0] if candidates else None

                pcol = actual_col("production")
                ccol = actual_col("consumption")
                if pcol or ccol:
                    out = pd.DataFrame({"time_utc": df["time_utc"]})
                    if pcol:
                        out["production_mw"] = pd.to_numeric(df[pcol].astype(str).str.replace(",", ".", regex=False), errors="coerce")
                    if ccol:
                        out["consumption_mw"] = pd.to_numeric(df[ccol].astype(str).str.replace(",", ".", regex=False), errors="coerce")
                    out = out.dropna(subset=[c for c in ["production_mw", "consumption_mw"] if c in out.columns], how="all")
                    out = out[out["time_utc"].notna()].copy()
                    if not out.empty:
                        out["time_local"] = out["time_utc"].dt.tz_convert("Europe/Tallinn")
                        csv_status.note = f"CSV parsed; production column={pcol!r}, consumption column={ccol!r}"
                        return out.sort_values("time_utc").drop_duplicates("time_utc"), csv_status
            csv_status.note = "CSV endpoint responded but actual production/consumption columns were not recognized; trying JSON fallback."
        except Exception as exc:
            csv_status.note = f"CSV parse failed ({type(exc).__name__}: {exc}); trying JSON fallback."

    # Fallback route: JSON. Be deliberately permissive about nested dictionaries and
    # named series because the dashboard API has used more than one presentation shape.
    payload, status = _get_json(url, params=base_params)
    status.source = "Elering electricity system (JSON fallback)"
    if not status.ok or not isinstance(payload, (dict, list)):
        if csv_status.error or csv_status.note:
            status.note = "; ".join(x for x in [csv_status.error, csv_status.note, status.note] if x)
        return pd.DataFrame(), status

    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    rows: list[dict[str, Any]] = []

    # Flat list of observations.
    if isinstance(data, list):
        rows.extend(item for item in data if isinstance(item, dict))

    # Dict of series or nested containers.
    elif isinstance(data, dict):
        series_maps: dict[int, dict[str, Any]] = {}

        def consume_series(series_name: str, values: Any) -> None:
            if isinstance(values, dict):
                # Some API shapes wrap rows in data/values/items.
                for k in ("data", "values", "items", "rows"):
                    if isinstance(values.get(k), list):
                        consume_series(series_name, values[k])
                        return
            if not isinstance(values, list):
                return
            for item in values:
                if not isinstance(item, dict):
                    continue
                ts = item.get("timestamp") or item.get("time") or item.get("datetime") or item.get("_datetime")
                if ts is None:
                    continue
                try:
                    if isinstance(ts, str) and not ts.isdigit():
                        key = int(pd.Timestamp(ts).timestamp())
                    else:
                        key = int(ts)
                        if key > 10_000_000_000:
                            key //= 1000
                except Exception:
                    continue
                value = item.get("value")
                if value is None:
                    for k, v in item.items():
                        if k not in {"timestamp", "time", "datetime", "_datetime"} and isinstance(v, (int, float)):
                            value = v
                            break
                series_maps.setdefault(key, {"timestamp": key})[str(series_name)] = value

        for series_name, values in data.items():
            # Preserve directly flat observation lists if present under a generic container.
            if str(series_name).lower() in {"rows", "items", "values"} and isinstance(values, list) and values and isinstance(values[0], dict):
                if any(k in values[0] for k in ("production", "consumption", "tootmine", "tarbimine")):
                    rows.extend(values)
                    continue
            consume_series(str(series_name), values)
        if not rows:
            rows = list(series_maps.values())

    if not rows:
        status.ok = False
        status.error = "Endpoint responded but production/consumption series could not be parsed"
        return pd.DataFrame(), status

    df = pd.DataFrame(rows)
    time_col = next((c for c in ["timestamp", "time", "datetime", "_datetime"] if c in df.columns), None)
    if time_col is None:
        status.ok = False
        status.error = "Elering system response has no recognizable time field"
        return pd.DataFrame(), status

    if time_col == "timestamp":
        numeric = pd.to_numeric(df[time_col], errors="coerce")
        if numeric.notna().any():
            unit = "ms" if numeric.dropna().median() > 10_000_000_000 else "s"
            df["time_utc"] = pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce")
        else:
            df["time_utc"] = pd.to_datetime(df[time_col], utc=True, errors="coerce")
    else:
        df["time_utc"] = pd.to_datetime(df[time_col], utc=True, errors="coerce")
    df = df[df["time_utc"].notna()].copy()
    df["time_local"] = df["time_utc"].dt.tz_convert("Europe/Tallinn")

    rename: dict[str, str] = {}
    for col in df.columns:
        lc = str(col).lower()
        if ("consumption" in lc or "tarb" in lc) and not any(k in lc for k in ("plan", "forecast", "prognoos")):
            rename[col] = "consumption_mw"
        elif ("production" in lc or "toot" in lc) and not any(k in lc for k in ("plan", "forecast", "prognoos")):
            rename[col] = "production_mw"
    df = df.rename(columns=rename)
    for c in ["consumption_mw", "production_mw"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    keep = [c for c in ["time_local", "time_utc", "consumption_mw", "production_mw"] if c in df.columns]
    if len(keep) <= 2:
        status.ok = False
        status.error = f"JSON parsed but actual production/consumption fields were absent. Columns: {list(df.columns)}"
        return pd.DataFrame(), status
    status.note = "JSON fallback parsed successfully."
    return df[keep].sort_values("time_local").drop_duplicates("time_utc"), status


def fetch_reserve_capacity(region: str = "EE") -> tuple[pd.DataFrame, list[SourceStatus]]:
    """Fetch BBCM reserve capacity prices via Volton's public CC-BY-4.0 mirror of BTD.

    BTD remains the authoritative source. Volton is used because it publishes a stable,
    documented JSON contract without authentication. FCR is intentionally omitted because
    the checked public mirror currently documents aFRR and mFRR capacity datasets here.
    """
    suffix = {"EE": "", "LV": "-lv", "LT": "-lt"}.get(region.upper())
    if suffix is None:
        raise ValueError("region must be EE, LV or LT")
    datasets = {
        "aFRR": f"afrr-capacity-price{suffix}",
        "mFRR": f"mfrr-capacity-price{suffix}",
    }
    frames: list[pd.DataFrame] = []
    statuses: list[SourceStatus] = []
    for product, slug in datasets.items():
        url = f"{VOLTON_BASE}/{slug}/latest.json"
        payload, status = _get_json(url)
        status.source = f"Baltic Transparency Dashboard via Volton – {product} {region.upper()}"
        statuses.append(status)
        if not status.ok or not isinstance(payload, dict):
            continue
        rows = payload.get("rows", [])
        if not isinstance(rows, list):
            continue
        df = pd.DataFrame(rows)
        if df.empty or "mtu_start" not in df.columns or "price_eur_mw_h" not in df.columns:
            continue
        df["time_utc"] = pd.to_datetime(df["mtu_start"], utc=True, errors="coerce")
        df["time_local"] = df["time_utc"].dt.tz_convert("Europe/Tallinn")
        df["price_eur_mw_h"] = pd.to_numeric(df["price_eur_mw_h"], errors="coerce")
        df["product"] = product
        df["region"] = region.upper()
        frames.append(df[["time_local", "time_utc", "region", "product", "direction", "price_eur_mw_h"]])
    return (pd.concat(frames, ignore_index=True).sort_values("time_local") if frames else pd.DataFrame()), statuses


def fetch_balancing_energy(region: str = "EE") -> tuple[pd.DataFrame, list[SourceStatus]]:
    """Fetch aFRR/mFRR activated balancing-energy clearing prices via Volton public data.

    Values are marginal clearing prices in EUR/MWh for each 15-minute MTU and direction.
    Up/down are kept explicit. Missing prices (no activation) remain NaN and are never filled.
    """
    suffix = {"EE": "", "LV": "-lv", "LT": "-lt"}.get(region.upper())
    if suffix is None:
        raise ValueError("region must be EE, LV or LT")
    datasets = {
        "aFRR": f"afrr-clearing-price{suffix}",
        "mFRR": f"mfrr-clearing-price{suffix}",
    }
    frames: list[pd.DataFrame] = []
    statuses: list[SourceStatus] = []
    for product, slug in datasets.items():
        url = f"{VOLTON_BASE}/{slug}/latest.json"
        payload, status = _get_json(url)
        status.source = f"Balancing energy via Volton – {product} {region.upper()}"
        statuses.append(status)
        if not status.ok or not isinstance(payload, dict):
            continue
        rows = payload.get("rows", [])
        if not isinstance(rows, list):
            status.ok = False
            status.error = "Unexpected balancing-energy response schema: rows is not a list"
            continue
        df = pd.DataFrame(rows)
        required = {"mtu_start", "direction", "price_eur_mwh"}
        if df.empty or not required.issubset(df.columns):
            status.note = "Request succeeded but required balancing-energy fields were absent."
            continue
        df["time_utc"] = pd.to_datetime(df["mtu_start"], utc=True, errors="coerce")
        df["time_local"] = df["time_utc"].dt.tz_convert("Europe/Tallinn")
        df["price_eur_mwh"] = pd.to_numeric(df["price_eur_mwh"], errors="coerce")
        df["direction"] = df["direction"].astype(str).str.lower()
        df["product"] = product
        df["region"] = region.upper()
        frames.append(df[["time_local", "time_utc", "region", "product", "direction", "price_eur_mwh"]])
    return (pd.concat(frames, ignore_index=True).sort_values("time_local") if frames else pd.DataFrame()), statuses


def fetch_gas_storage(agsi_key: str | None) -> tuple[pd.DataFrame, SourceStatus]:
    """Fetch latest EU and Latvia AGSI+ storage data using the user's personal x-key.

    GIE publishes daily gas-day data. reverse=true is used so the newest gas days are returned
    first; no historical paging is needed for the operational dashboard.
    """
    url = AGSI_BASE
    if not agsi_key:
        return pd.DataFrame(), SourceStatus(
            source="GIE AGSI+", ok=False, fetched_at=_now_iso(), url=url,
            error="GIE_AGSI_API_KEY is not configured", note="AGSI+ API requires a personal x-key."
        )

    frames: list[pd.DataFrame] = []
    statuses: list[SourceStatus] = []
    queries = [
        ("EU", {"type": "eu", "size": 30, "reverse": "true"}),
        ("LV", {"country": "LV", "size": 30, "reverse": "true"}),
    ]
    for label, params in queries:
        payload, status = _get_json(url, params=params, headers={"x-key": agsi_key})
        status.source = f"GIE AGSI+ {label}"
        statuses.append(status)
        if not status.ok or not isinstance(payload, dict):
            continue
        rows = payload.get("data", [])
        if not isinstance(rows, list):
            status.ok = False
            status.error = "Unexpected AGSI response schema: data is not a list"
            continue
        df = pd.DataFrame(rows)
        if df.empty:
            status.note = "AGSI request succeeded but returned no rows."
            continue
        df["scope"] = label
        frames.append(df)

    ok_statuses = [x for x in statuses if x.ok]
    if not frames:
        failed = next((x for x in statuses if not x.ok), None)
        return pd.DataFrame(), failed or SourceStatus(
            source="GIE AGSI+", ok=False, fetched_at=_now_iso(), url=url, error="No data"
        )

    df = pd.concat(frames, ignore_index=True)
    for c in ["full", "gasInStorage", "workingGasVolume", "injection", "withdrawal",
              "injectionCapacity", "withdrawalCapacity", "consumption", "consumptionFull"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    if "gasDayStart" in df.columns:
        df["gas_day"] = pd.to_datetime(df["gasDayStart"], errors="coerce")
    if "gas_day" in df.columns:
        df = df.sort_values(["scope", "gas_day"], ascending=[True, False])

    combined = SourceStatus(
        source="GIE AGSI+ EU + Latvia",
        ok=bool(ok_statuses),
        fetched_at=_now_iso(),
        url=url,
        status_code=ok_statuses[-1].status_code if ok_statuses else None,
        error=None if len(ok_statuses) == len(statuses) else "; ".join(
            f"{x.source}: {x.error or 'failed'}" for x in statuses if not x.ok
        ) or None,
        note="Daily data; newest gas days requested with reverse=true."
    )
    return df, combined


def _entsoe_status(ok: bool, url: str, *, status_code: int | None = None,
                   error: str | None = None, note: str | None = None) -> SourceStatus:
    return SourceStatus(source="ENTSO-E Transparency Platform", ok=ok, fetched_at=_now_iso(),
                        url=url, status_code=status_code, error=error, note=note)


def _entsoe_get_xml(token: str | None, params: dict[str, Any], retries: int = 3) -> tuple[str | None, SourceStatus]:
    if not token:
        return None, _entsoe_status(False, ENTSOE_BASE, error="ENTSOE_API_KEY is not configured")
    q = dict(params)
    q["securityToken"] = token
    session = requests.Session()
    session.headers.update({"Accept": "application/xml,text/xml,*/*", "User-Agent": "Regional-Energy-Dashboard/3.0"})
    last_error = None
    last_status = None
    for attempt in range(retries):
        try:
            r = session.get(ENTSOE_BASE, params=q, timeout=(5, 30))
            last_status = r.status_code
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
                    continue
            r.raise_for_status()
            text = r.text
            # ENTSO-E can return an XML acknowledgement/reason with HTTP 200.
            try:
                root = ET.fromstring(text)
                reason = next((el.text for el in root.iter() if el.tag.split('}')[-1] == 'text' and el.text), None)
                root_name = root.tag.split('}')[-1].lower()
                if 'acknowledgement' in root_name and reason:
                    return None, _entsoe_status(False, r.url, status_code=r.status_code, error=reason)
            except ET.ParseError:
                return None, _entsoe_status(False, r.url, status_code=r.status_code, error="Malformed XML response")
            return text, _entsoe_status(True, r.url, status_code=r.status_code)
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    return None, _entsoe_status(False, ENTSOE_BASE, status_code=last_status, error=last_error)


def _localname(tag: str) -> str:
    return tag.split('}')[-1]


def _child_text(node: ET.Element, name: str) -> str | None:
    for el in node.iter():
        if _localname(el.tag) == name and el.text:
            return el.text.strip()
    return None


def _resolution_delta(value: str | None) -> pd.Timedelta:
    mapping = {"PT15M": pd.Timedelta(minutes=15), "PT30M": pd.Timedelta(minutes=30),
               "PT60M": pd.Timedelta(hours=1), "PT1H": pd.Timedelta(hours=1),
               "P1D": pd.Timedelta(days=1)}
    return mapping.get(value or "", pd.Timedelta(hours=1))


def _parse_entsoe_timeseries(xml_text: str, *, value_name: str) -> pd.DataFrame:
    root = ET.fromstring(xml_text)
    rows: list[dict[str, Any]] = []
    for ts in [x for x in root.iter() if _localname(x.tag) == "TimeSeries"]:
        psr = None
        for el in ts.iter():
            if _localname(el.tag) == "psrType" and el.text:
                psr = el.text.strip()
                break
        periods = [x for x in ts.iter() if _localname(x.tag) == "Period"]
        for period in periods:
            start_txt = None
            for ti in period:
                if _localname(ti.tag) == "timeInterval":
                    start_txt = _child_text(ti, "start")
                    break
            if not start_txt:
                start_txt = _child_text(period, "start")
            start = pd.to_datetime(start_txt, utc=True, errors="coerce")
            if pd.isna(start):
                continue
            resolution = _child_text(period, "resolution")
            step = _resolution_delta(resolution)
            for point in [x for x in period if _localname(x.tag) == "Point"]:
                pos_txt = _child_text(point, "position")
                val_txt = _child_text(point, "quantity") or _child_text(point, "price.amount")
                try:
                    pos = int(pos_txt or "")
                    val = float(val_txt or "")
                except (TypeError, ValueError):
                    continue
                t = start + (pos - 1) * step
                rows.append({"time_utc": t, value_name: val, "psr_type": psr,
                             "resolution": resolution})
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["time_local"] = pd.to_datetime(df["time_utc"], utc=True).dt.tz_convert("Europe/Tallinn")
    return df.sort_values("time_utc")


def fetch_entsoe_generation_by_type(token: str | None, start: datetime, end: datetime,
                                    region: str = "EE") -> tuple[pd.DataFrame, SourceStatus]:
    """ENTSO-E actual aggregated generation per production type (A75, A16 realised)."""
    domain = ENTSOE_DOMAINS.get(region.upper())
    if not domain:
        return pd.DataFrame(), _entsoe_status(False, ENTSOE_BASE, error=f"Unknown domain {region}")
    params = {
        "documentType": "A75", "processType": "A16", "in_Domain": domain,
        "periodStart": start.astimezone(timezone.utc).strftime("%Y%m%d%H%M"),
        "periodEnd": end.astimezone(timezone.utc).strftime("%Y%m%d%H%M"),
    }
    xml, status = _entsoe_get_xml(token, params)
    status.source = f"ENTSO-E actual generation by type {region.upper()} (A75)"
    if not xml:
        return pd.DataFrame(), status
    df = _parse_entsoe_timeseries(xml, value_name="generation_mw")
    if df.empty:
        status.note = "Request succeeded but no generation time series were parsed."
        return df, status
    df["technology"] = df["psr_type"].map(PSR_TYPES).fillna(df["psr_type"])
    # A75 may contain separate consumption series for storage units. Keep only explicit positive generation rows.
    df["generation_mw"] = pd.to_numeric(df["generation_mw"], errors="coerce")
    return df.dropna(subset=["generation_mw"]), status



def fetch_entsoe_actual_load(token: str | None, start: datetime, end: datetime,
                             region: str = "EE") -> tuple[pd.DataFrame, SourceStatus]:
    """ENTSO-E actual total load (A65, A16 realised) for a bidding zone."""
    domain = ENTSOE_DOMAINS.get(region.upper())
    if not domain:
        return pd.DataFrame(), _entsoe_status(False, ENTSOE_BASE, error=f"Unknown domain {region}")
    params = {
        "documentType": "A65", "processType": "A16", "outBiddingZone_Domain": domain,
        "periodStart": start.astimezone(timezone.utc).strftime("%Y%m%d%H%M"),
        "periodEnd": end.astimezone(timezone.utc).strftime("%Y%m%d%H%M"),
    }
    xml, status = _entsoe_get_xml(token, params)
    status.source = f"ENTSO-E actual total load {region.upper()} (A65)"
    if not xml:
        return pd.DataFrame(), status
    df = _parse_entsoe_timeseries(xml, value_name="load_mw")
    if df.empty:
        status.note = "Request succeeded but no actual-load time series were parsed."
        return df, status
    df["load_mw"] = pd.to_numeric(df["load_mw"], errors="coerce")
    return df.dropna(subset=["load_mw"])[["time_utc", "time_local", "load_mw", "resolution"]], status


def fetch_entsoe_physical_flow(token: str | None, start: datetime, end: datetime,
                               from_region: str, to_region: str) -> tuple[pd.DataFrame, SourceStatus]:
    """ENTSO-E physical flow (A11) in the requested direction, from out_Domain to in_Domain."""
    out_domain = ENTSOE_DOMAINS.get(from_region.upper())
    in_domain = ENTSOE_DOMAINS.get(to_region.upper())
    if not out_domain or not in_domain:
        return pd.DataFrame(), _entsoe_status(False, ENTSOE_BASE, error="Unknown ENTSO-E domain")
    params = {
        "documentType": "A11", "out_Domain": out_domain, "in_Domain": in_domain,
        "periodStart": start.astimezone(timezone.utc).strftime("%Y%m%d%H%M"),
        "periodEnd": end.astimezone(timezone.utc).strftime("%Y%m%d%H%M"),
    }
    xml, status = _entsoe_get_xml(token, params)
    status.source = f"ENTSO-E physical flow {from_region.upper()}→{to_region.upper()} (A11)"
    if not xml:
        return pd.DataFrame(), status
    df = _parse_entsoe_timeseries(xml, value_name="flow_mw")
    if df.empty:
        status.note = "Request succeeded but no physical-flow time series were parsed."
        return df, status
    df["from_region"] = from_region.upper()
    df["to_region"] = to_region.upper()
    return df[["time_utc", "time_local", "from_region", "to_region", "flow_mw", "resolution"]], status


def fetch_entsoe_estonia_flows(token: str | None, start: datetime, end: datetime) -> tuple[pd.DataFrame, list[SourceStatus]]:
    """Fetch both directions for EE-FI and EE-LV and calculate a signed net flow from Estonia's perspective.

    Positive signed_mw = export from Estonia; negative signed_mw = import to Estonia.
    Individual directional series remain available so no netting assumption is hidden.
    """
    frames: list[pd.DataFrame] = []
    statuses: list[SourceStatus] = []
    for a, b in [("EE", "FI"), ("FI", "EE"), ("EE", "LV"), ("LV", "EE")]:
        df, st = fetch_entsoe_physical_flow(token, start, end, a, b)
        statuses.append(st)
        if not df.empty:
            x = df.copy()
            x["border"] = "EE–FI" if {a, b} == {"EE", "FI"} else "EE–LV"
            x["direction"] = f"{a}→{b}"
            x["signed_mw"] = x["flow_mw"] if a == "EE" else -x["flow_mw"]
            frames.append(x)
    if not frames:
        return pd.DataFrame(), statuses
    return pd.concat(frames, ignore_index=True).sort_values("time_utc"), statuses



def fetch_entsoe_day_ahead_ntc(token: str | None, start: datetime, end: datetime,
                                from_region: str, to_region: str) -> tuple[pd.DataFrame, SourceStatus]:
    """ENTSO-E forecasted day-ahead net transfer capacity (A61, contract A01).

    This is a directional cross-zonal capacity publication, not residual free capacity after
    physical flow. It is therefore shown next to, but never subtracted from, A11 physical flow.
    """
    out_domain = ENTSOE_DOMAINS.get(from_region.upper())
    in_domain = ENTSOE_DOMAINS.get(to_region.upper())
    if not out_domain or not in_domain:
        return pd.DataFrame(), _entsoe_status(False, ENTSOE_BASE, error="Unknown ENTSO-E domain")
    params = {
        "documentType": "A61",
        "contract_MarketAgreement.Type": "A01",
        "out_Domain": out_domain,
        "in_Domain": in_domain,
        "periodStart": start.astimezone(timezone.utc).strftime("%Y%m%d%H%M"),
        "periodEnd": end.astimezone(timezone.utc).strftime("%Y%m%d%H%M"),
    }
    xml, status = _entsoe_get_xml(token, params)
    status.source = f"ENTSO-E day-ahead NTC {from_region.upper()}→{to_region.upper()} (A61)"
    if not xml:
        return pd.DataFrame(), status
    df = _parse_entsoe_timeseries(xml, value_name="ntc_mw")
    if df.empty:
        status.note = "Request succeeded but no day-ahead NTC time series were parsed."
        return df, status
    df["from_region"] = from_region.upper()
    df["to_region"] = to_region.upper()
    df["border"] = "EE–FI" if {from_region.upper(), to_region.upper()} == {"EE", "FI"} else "EE–LV"
    df["direction"] = f"{from_region.upper()}→{to_region.upper()}"
    return df[["time_utc", "time_local", "from_region", "to_region", "border", "direction", "ntc_mw", "resolution"]], status


def fetch_entsoe_estonia_ntc(token: str | None, start: datetime, end: datetime) -> tuple[pd.DataFrame, list[SourceStatus]]:
    """Fetch directional day-ahead NTC for EE-FI and EE-LV in both directions."""
    frames: list[pd.DataFrame] = []
    statuses: list[SourceStatus] = []
    for a, b in [("EE", "FI"), ("FI", "EE"), ("EE", "LV"), ("LV", "EE")]:
        df, st = fetch_entsoe_day_ahead_ntc(token, start, end, a, b)
        statuses.append(st)
        if not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame(), statuses
    return pd.concat(frames, ignore_index=True).sort_values("time_utc"), statuses

def current_local_date() -> date:
    return datetime.now(TALLINN).date()
