from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
import logging

import pandas as pd
import requests

BASE = "https://public-data.volton.energy/v1"
OUT = Path(__file__).resolve().parent / "data" / "reserve_capacity_ytd.csv"
REGIONS = {"EE": "", "LV": "-lv", "LT": "-lt"}
PRODUCTS = {"aFRR": "afrr-capacity-price", "mFRR": "mfrr-capacity-price"}
START_DATES = {"aFRR": date(2025, 4, 15), "mFRR": date(2025, 2, 4)}
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("reserve-ytd")


def fetch_day(region: str, product: str, day: date):
    slug = PRODUCTS[product] + REGIONS[region]
    url = f"{BASE}/{slug}/{day.isoformat()}.json"
    try:
        r = requests.get(url, timeout=(4, 15), headers={"User-Agent": "BalticPulse/15.4"})
        if r.status_code == 404:
            return []
        r.raise_for_status()
        payload = r.json()
        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        if not rows:
            return []
        df = pd.DataFrame(rows)
        if "price_eur_mw_h" not in df or "direction" not in df:
            return []
        df["price_eur_mw_h"] = pd.to_numeric(df["price_eur_mw_h"], errors="coerce")
        out = []
        for direction, g in df.groupby("direction"):
            vals = g["price_eur_mw_h"].dropna()
            if vals.empty:
                continue
            out.append({
                "date": day.isoformat(),
                "region": region,
                "product": product,
                "direction": str(direction).lower(),
                "price_eur_mw_h": float(vals.mean()),
                "samples": int(vals.size),
            })
        return out
    except Exception as exc:
        LOG.warning("%s %s %s failed: %s", region, product, day, exc)
        return []


def main():
    today = date.today()
    year_start = date(today.year, 1, 1)
    existing = pd.DataFrame()
    if OUT.exists():
        try:
            existing = pd.read_csv(OUT)
        except Exception:
            existing = pd.DataFrame()

    existing_keys = set()
    if not existing.empty:
        for row in existing.itertuples(index=False):
            existing_keys.add((str(row.date), str(row.region), str(row.product), str(row.direction)))

    # Re-fetch the latest 3 days to pick up late corrections; all older daily files are immutable.
    refresh_from = today - timedelta(days=3)
    tasks = []
    for product in PRODUCTS:
        start = max(year_start, START_DATES[product])
        day = start
        while day <= today:
            for region in REGIONS:
                # A day is considered complete only when both directions are already stored.
                have_up = (day.isoformat(), region, product, "up") in existing_keys
                have_down = (day.isoformat(), region, product, "down") in existing_keys
                if day >= refresh_from or not (have_up and have_down):
                    tasks.append((region, product, day))
            day += timedelta(days=1)

    LOG.info("Fetching %d day/dataset archives", len(tasks))
    new_rows = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        futs = {pool.submit(fetch_day, *t): t for t in tasks}
        for fut in as_completed(futs):
            new_rows.extend(fut.result())

    if not existing.empty:
        # Remove refreshed keys before appending replacements.
        refresh_keys = {(r["date"], r["region"], r["product"], r["direction"]) for r in new_rows}
        mask = existing.apply(lambda r: (str(r["date"]), str(r["region"]), str(r["product"]), str(r["direction"])) not in refresh_keys, axis=1)
        existing = existing[mask]

    combined = pd.concat([existing, pd.DataFrame(new_rows)], ignore_index=True) if new_rows or not existing.empty else pd.DataFrame()
    if combined.empty:
        raise SystemExit("No reserve-capacity archive data retrieved")
    combined["date"] = pd.to_datetime(combined["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    combined = combined.dropna(subset=["date", "price_eur_mw_h"]).drop_duplicates(["date","region","product","direction"], keep="last")
    combined = combined.sort_values(["date","region","product","direction"])
    OUT.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(OUT, index=False)
    LOG.info("Wrote %d daily summary rows to %s", len(combined), OUT)

if __name__ == "__main__":
    main()
