from pathlib import Path
import pandas as pd
from energy_sources import fetch_eex_ngp_history

OUT = Path("data/gas_ngp_history.csv")
rows = []
for area in ["TTF", "LVA-EST", "FIN", "LTU"]:
    df, status = fetch_eex_ngp_history(area)
    if df.empty:
        print(area, status.error or status.note)
        continue
    x = df.copy()
    x["area"] = area
    rows.append(x[["date", "area", "price_eur_mwh"]])

if not rows:
    raise SystemExit("No EEX NGP history obtained; keeping previous archive")

new = pd.concat(rows, ignore_index=True)
if OUT.exists():
    old = pd.read_csv(OUT)
    new = pd.concat([old, new], ignore_index=True)

new["date"] = pd.to_datetime(new["date"], errors="coerce").dt.date
new = (
    new.dropna(subset=["date", "area", "price_eur_mwh"])
       .drop_duplicates(["date", "area"], keep="last")
       .sort_values(["date", "area"])
)

OUT.parent.mkdir(parents=True, exist_ok=True)
new.to_csv(OUT, index=False)
print(f"Wrote {len(new)} rows to {OUT}")
