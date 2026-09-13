from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

import pandas as pd
import requests

OUT = Path("data/futures.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; BalticPulseFutures/16.0.6)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

EURONEXT = {
    "SYS_M": ("NSBM", "Nordic SYS", "month"),
    "SYS_Q": ("NSBQ", "Nordic SYS", "quarter"),
    "SYS_Y": ("NSBY", "Nordic SYS", "year"),
    "FI_M": ("HLBM", "FI EPAD", "month"),
    "FI_Q": ("HLBQ", "FI EPAD", "quarter"),
    "FI_Y": ("HLBY", "FI EPAD", "year"),
    "LT_M": ("VIBM", "LT EPAD", "month"),
    "LT_Q": ("VIBQ", "LT EPAD", "quarter"),
    "LT_Y": ("VIBY", "LT EPAD", "year"),
}

ICE_TTF_URL = "https://www.ice.com/products/27996665/Dutch-TTF-Natural-Gas-Futures/data?marketId=5927115"
ICE_BRENT_URL = "https://www.ice.com/products/219/Brent-Crude-Futures/data?marketId=5049381"

MONTHS = {m: i for i, m in enumerate(
    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"], 1
)}

def num(v):
    if v is None:
        return None
    s = str(v).replace("\\xa0", " ").strip()
    if s in ("", "-", "—", "nan", "None"):
        return None
    s = s.replace(",", "")
    try:
        return float(s)
    except Exception:
        return None

def delivery_start(label):
    s = str(label).strip()

    m = re.fullmatch(r"([A-Z][a-z]{2})\s+(20\d{2})", s)
    if m and m.group(1) in MONTHS:
        return f"{m.group(2)}-{MONTHS[m.group(1)]:02d}-01"

    m = re.fullmatch(r"([A-Z][a-z]{2})(\d{2})", s)
    if m and m.group(1) in MONTHS:
        return f"20{m.group(2)}-{MONTHS[m.group(1)]:02d}-01"

    m = re.fullmatch(r"Q([1-4])\s*(20\d{2})", s, re.I)
    if m:
        return f"{m.group(2)}-{(int(m.group(1))-1)*3+1:02d}-01"

    m = re.fullmatch(r"Q([1-4])\s*(\d{2})", s, re.I)
    if m:
        return f"20{m.group(2)}-{(int(m.group(1))-1)*3+1:02d}-01"

    m = re.fullmatch(r"(20\d{2})", s)
    if m:
        return f"{m.group(1)}-01-01"

    m = re.fullmatch(r"Cal\s*(\d{2})", s, re.I)
    if m:
        return f"20{m.group(1)}-01-01"

    m = re.fullmatch(r"(Winter|Summer)\s*(\d{2})", s, re.I)
    if m:
        yr = 2000 + int(m.group(2))
        mo = 10 if m.group(1).lower() == "winter" else 4
        return f"{yr}-{mo:02d}-01"

    return None

def norm_cols(df):
    d = df.copy()
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = [
            " ".join(str(x) for x in col if str(x) != "nan").strip()
            for col in d.columns
        ]
    else:
        d.columns = [str(c).strip() for c in d.columns]
    return d

def pick_col(df, needles, exact=None):
    exact = exact or []
    for c in df.columns:
        lc = str(c).strip().lower()
        if lc in exact:
            return c
    for c in df.columns:
        lc = str(c).strip().lower()
        if any(n in lc for n in needles):
            return c
    return None

def html_tables(url):
    r = requests.get(url, timeout=(6, 30), headers=HEADERS)
    r.raise_for_status()
    if len(r.text) < 500:
        raise ValueError(f"response too short ({len(r.text)} chars)")
    return [norm_cols(x) for x in pd.read_html(StringIO(r.text))]

def find_euronext_table(tables):
    for d in tables:
        dcol = pick_col(d, ["delivery"], exact=["delivery"])
        scol = pick_col(d, ["settl"], exact=["settl.", "settlement"])
        if dcol is not None and scol is not None:
            return d, dcol, scol
    return None, None, None

def fetch_euronext(code, series, tenor):
    # Explicit settlement-prices endpoint exposes the delivery table in server HTML.
    urls = [
        f"https://live.euronext.com/en/product/commodities-futures/{code}-DAMS/settlement-prices",
        f"https://live.euronext.com/en/product/commodities-futures/{code}-DAMS",
    ]
    last_error = None

    for url in urls:
        try:
            tables = html_tables(url)
            df, dcol, scol = find_euronext_table(tables)
            if df is None:
                raise ValueError("delivery/settlement table not found")

            oicol = pick_col(df, ["o.i", "open interest"], exact=["o.i"])
            rows = []
            for _, row in df.iterrows():
                delivery = str(row[dcol]).strip()
                settlement = num(row[scol])
                ds = delivery_start(delivery)
                if settlement is None or ds is None:
                    continue
                rows.append({
                    "series": series,
                    "tenor": tenor,
                    "delivery": delivery,
                    "delivery_start": ds,
                    "settlement": settlement,
                    "open_interest": num(row[oicol]) if oicol is not None else None,
                    "source_product": code,
                    "source_url": url,
                })
            if rows:
                return rows
            raise ValueError("no valid delivery rows")
        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"{code}: {type(last_error).__name__}: {last_error}")

def implied(sys_rows, epad_rows, name):
    idx = {(r["tenor"], r["delivery"]): r for r in epad_rows}
    out = []
    for sys in sys_rows:
        epad = idx.get((sys["tenor"], sys["delivery"]))
        if not epad:
            continue
        out.append({
            **sys,
            "series": name,
            "settlement": sys["settlement"] + epad["settlement"],
            "open_interest": None,
            "source_product": f"{sys['source_product']}+{epad['source_product']}",
            "source_url": "https://live.euronext.com/en/products/commodities/commodity-futures/list",
        })
    return out

def select_key_power(curve):
    rows = []
    for series in ["Nordic SYS", "FI implied", "LT implied"]:
        sub = [r for r in curve if r["series"] == series]
        for label, tenor in [("M+1","month"), ("Q+1","quarter"), ("Y+1","year")]:
            candidates = sorted(
                [r for r in sub if r["tenor"] == tenor],
                key=lambda x: x["delivery_start"],
            )
            if candidates:
                r = candidates[0]
                rows.append({
                    "series": series,
                    "horizon": label,
                    "delivery": r["delivery"],
                    "settlement": r["settlement"],
                    "open_interest": r.get("open_interest"),
                    "source_product": r["source_product"],
                })
    return rows

def find_ice_table(tables):
    for d in tables:
        ccol = pick_col(d, ["contract"], exact=["contract"])
        lcol = pick_col(d, ["last"], exact=["last"])
        if ccol is not None and lcol is not None:
            return d, ccol, lcol
    return None, None, None

def fetch_ice_curve(url, label):
    tables = html_tables(url)
    df, ccol, lcol = find_ice_table(tables)
    if df is None:
        raise ValueError(f"{label}: contract/last table not found")

    timecol = pick_col(df, ["time"], exact=["time(gmt)", "time"])
    volcol = pick_col(df, ["volume"], exact=["volume"])

    rows = []
    for _, row in df.iterrows():
        contract = str(row[ccol]).strip()
        last = num(row[lcol])
        ds = delivery_start(contract)
        if last is None or ds is None:
            continue
        rows.append({
            "contract": contract,
            "delivery_start": ds,
            "last": last,
            "time": str(row[timecol]) if timecol is not None else "",
            "volume": num(row[volcol]) if volcol is not None else None,
            "source_url": url,
        })

    if not rows:
        raise ValueError(f"{label}: no valid price rows")
    return rows

def select_key_gas(curve):
    out = []
    wanted = [("M+1", r"[A-Z][a-z]{2}\d{2}"),
              ("Q+1", r"Q[1-4]\s*\d{2}"),
              ("Y+1", r"Cal\s*\d{2}")]
    for label, pat in wanted:
        candidates = [
            r for r in curve
            if re.fullmatch(pat, str(r.get("contract","")), re.I)
        ]
        candidates = sorted(candidates, key=lambda x: x["delivery_start"])
        if candidates:
            r = candidates[0]
            out.append({
                "horizon": label,
                "contract": r["contract"],
                "last": r["last"],
                "volume": r.get("volume"),
                "time": r.get("time"),
            })
    return out

def select_key_brent(curve):
    monthly = sorted(
        [r for r in curve if re.fullmatch(r"[A-Z][a-z]{2}\d{2}", str(r.get("contract","")))],
        key=lambda x: x["delivery_start"],
    )
    out = []
    for label, idx in [("M+1",0), ("M+3",2), ("M+6",5), ("M+12",11)]:
        if idx < len(monthly):
            r = monthly[idx]
            out.append({
                "horizon": label,
                "contract": r["contract"],
                "last": r["last"],
                "volume": r.get("volume"),
                "time": r.get("time"),
            })
    return out

def main():
    previous = {}
    if OUT.exists():
        try:
            previous = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            previous = {}

    power_errors = []
    buckets = {}

    for key, (code, series, tenor) in EURONEXT.items():
        try:
            buckets[key] = fetch_euronext(code, series, tenor)
            print(f"Euronext {code}: {len(buckets[key])} rows")
        except Exception as exc:
            power_errors.append(f"Euronext {code}: {type(exc).__name__}: {exc}")
            print(power_errors[-1])

    sys_rows = sum((buckets.get(k, []) for k in ["SYS_M","SYS_Q","SYS_Y"]), [])
    fi_rows = sum((buckets.get(k, []) for k in ["FI_M","FI_Q","FI_Y"]), [])
    lt_rows = sum((buckets.get(k, []) for k in ["LT_M","LT_Q","LT_Y"]), [])

    power_curve = (
        sys_rows
        + implied(sys_rows, fi_rows, "FI implied")
        + implied(sys_rows, lt_rows, "LT implied")
    )

    power = {
        "curve": power_curve,
        "key": select_key_power(power_curve),
        "errors": power_errors,
    }

    gas_errors = []
    try:
        gas_curve = fetch_ice_curve(ICE_TTF_URL, "ICE TTF")
        gas = {
            "curve": gas_curve,
            "key": select_key_gas(gas_curve),
            "errors": gas_errors,
            "source_url": ICE_TTF_URL,
        }
        print(f"ICE TTF: {len(gas_curve)} rows")
    except Exception as exc:
        gas_errors.append(f"ICE TTF: {type(exc).__name__}: {exc}")
        gas = {"curve": [], "key": [], "errors": gas_errors, "source_url": ICE_TTF_URL}

    brent_errors = []
    try:
        brent_curve = fetch_ice_curve(ICE_BRENT_URL, "ICE Brent")
        brent = {
            "curve": brent_curve,
            "key": select_key_brent(brent_curve),
            "errors": brent_errors,
            "source_url": ICE_BRENT_URL,
        }
        print(f"ICE Brent: {len(brent_curve)} rows")
    except Exception as exc:
        brent_errors.append(f"ICE Brent: {type(exc).__name__}: {exc}")
        brent = {"curve": [], "key": [], "errors": brent_errors, "source_url": ICE_BRENT_URL}

    # Preserve last successful section independently.
    if not power["curve"] and previous.get("power", {}).get("curve"):
        old = previous["power"]
        old.setdefault("errors", [])
        old["errors"] = power_errors + old["errors"]
        power = old

    if not gas["curve"] and previous.get("gas", {}).get("curve"):
        old = previous["gas"]
        old.setdefault("errors", [])
        old["errors"] = gas_errors + old["errors"]
        gas = old

    if not brent["curve"] and previous.get("brent", {}).get("curve"):
        old = previous["brent"]
        old.setdefault("errors", [])
        old["errors"] = brent_errors + old["errors"]
        brent = old

    payload = {
        "schema_version": 3,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "power": power,
        "gas": gas,
        "brent": brent,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        f"snapshot written: power={len(power.get('curve',[]))}, "
        f"gas={len(gas.get('curve',[]))}, brent={len(brent.get('curve',[]))}"
    )

    # Fail the workflow only if nothing at all was obtained and there was no prior valid snapshot.
    if not any([
        power.get("curve"),
        gas.get("curve"),
        brent.get("curve"),
    ]):
        raise SystemExit("No futures data obtained from any official source")

if __name__ == "__main__":
    main()
