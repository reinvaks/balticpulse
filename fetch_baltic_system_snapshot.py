from __future__ import annotations
import json, os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pandas as pd
from energy_sources import fetch_entsoe_generation_by_type, fetch_entsoe_actual_load

OUT = Path("data/baltic_system_snapshot.json")
BALTICS = ["EE", "LV", "LT"]
RENEWABLE_NAMES = {
    "Biomass","Geothermal","Hydro Run-of-river and poundage","Hydro Water Reservoir",
    "Marine","Other renewable","Solar","Wind Offshore","Wind Onshore",
}

def nearest(df, time_col, value_col, target, tolerance="90min"):
    if df.empty or time_col not in df or value_col not in df: return (None, None)
    x = df[[time_col, value_col]].dropna().copy()
    if x.empty: return (None, None)
    x[time_col] = pd.to_datetime(x[time_col], utc=True, errors="coerce")
    x = x.dropna().sort_values(time_col)
    if x.empty: return (None, None)
    delta = (x[time_col] - target).abs()
    i = delta.idxmin()
    if delta.loc[i] > pd.Timedelta(tolerance): return (None, None)
    return (float(x.loc[i, value_col]), x.loc[i, time_col])

def one_region(region, token, start, end):
    gdf, gst = fetch_entsoe_generation_by_type(token, start, end, region)
    ldf, lst = fetch_entsoe_actual_load(token, start, end, region)
    out = {"region": region, "current": {}, "previous_24h": {}, "history": [],
           "generation_status": {"ok": gst.ok, "error": gst.error, "note": gst.note, "status_code": gst.status_code},
           "load_status": {"ok": lst.ok, "error": lst.error, "note": lst.note, "status_code": lst.status_code}}

    gen = pd.DataFrame()
    if not gdf.empty:
        gx = gdf.copy()
        gx["time_utc"] = pd.to_datetime(gx["time_utc"], utc=True, errors="coerce")
        gx["generation_mw"] = pd.to_numeric(gx["generation_mw"], errors="coerce")
        gx = gx.dropna(subset=["time_utc","generation_mw"])
        total = gx.groupby("time_utc", as_index=False)["generation_mw"].sum().rename(columns={"generation_mw":"production_mw"})
        ren = gx[gx["technology"].isin(RENEWABLE_NAMES)].groupby("time_utc", as_index=False)["generation_mw"].sum().rename(columns={"generation_mw":"renewable_mw"})
        gen = total.merge(ren, on="time_utc", how="left")
        gen["renewable_mw"] = gen["renewable_mw"].fillna(0.0)
        gen["renewable_share"] = (gen["renewable_mw"] / gen["production_mw"] * 100).where(gen["production_mw"] > 0)

    load = pd.DataFrame()
    if not ldf.empty:
        load = ldf[["time_utc","load_mw"]].copy()
        load["time_utc"] = pd.to_datetime(load["time_utc"], utc=True, errors="coerce")
        load["load_mw"] = pd.to_numeric(load["load_mw"], errors="coerce")
        load = load.dropna().sort_values("time_utc")

    now = pd.Timestamp.now(tz="UTC")
    prev = now - pd.Timedelta(hours=24)

    for target, key in [(now,"current"),(prev,"previous_24h")]:
        block = {}
        for field in ["production_mw","renewable_mw","renewable_share"]:
            v, ts = nearest(gen, "time_utc", field, target)
            block[field] = v
            if ts is not None: block["generation_time"] = ts.isoformat()
        v, ts = nearest(load, "time_utc", "load_mw", target)
        block["consumption_mw"] = v
        if ts is not None: block["load_time"] = ts.isoformat()
        out[key] = block

    hist = gen.copy() if not gen.empty else pd.DataFrame()
    if not load.empty:
        load2 = load.rename(columns={"load_mw":"consumption_mw"})
        hist = load2 if hist.empty else pd.merge_asof(hist.sort_values("time_utc"), load2.sort_values("time_utc"), on="time_utc", direction="nearest", tolerance=pd.Timedelta("30min"))
    if not hist.empty:
        out["history"] = [{
            "time_utc": pd.Timestamp(r["time_utc"]).isoformat(),
            "production_mw": None if pd.isna(r.get("production_mw")) else float(r.get("production_mw")),
            "consumption_mw": None if pd.isna(r.get("consumption_mw")) else float(r.get("consumption_mw")),
            "renewable_mw": None if pd.isna(r.get("renewable_mw")) else float(r.get("renewable_mw")),
            "renewable_share": None if pd.isna(r.get("renewable_share")) else float(r.get("renewable_share")),
        } for _, r in hist.tail(400).iterrows()]
    return out

def main():
    token = os.environ.get("ENTSOE_API_KEY","").strip()
    if not token: raise SystemExit("ENTSOE_API_KEY missing")
    end = datetime.now(timezone.utc) + timedelta(hours=1)
    start = end - timedelta(hours=50)
    regions = {r: one_region(r, token, start, end) for r in BALTICS}
    if all(not regions[r].get("current") for r in BALTICS) and OUT.exists():
        raise SystemExit("All regions failed; keeping previous snapshot")
    payload = {"schema_version":1,"updated_at":datetime.now(timezone.utc).isoformat(),
               "source":"ENTSO-E Transparency Platform via GitHub Actions","regions":regions}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("snapshot written", OUT)

if __name__ == "__main__":
    main()
