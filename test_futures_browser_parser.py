import pandas as pd
from fetch_futures_snapshot import (
    find_euronext_table,
    find_ice_table,
    delivery_start,
)

e = pd.DataFrame({
    "Delivery": ["Q4 2026", "Q1 2027"],
    "Settl.": [67.65, 73.80],
    "O.I": [5095, 1266],
})
df, dcol, scol = find_euronext_table([e])
assert dcol == "Delivery"
assert scol == "Settl."
assert delivery_start("Q4 2026") == "2026-10-01"

i = pd.DataFrame({
    "Contract": ["Oct26", "Nov26"],
    "Last": [82.010, 81.735],
    "Time(GMT)": ["9/10/2026 6:43 PM", "9/10/2026 6:43 PM"],
    "Volume": [119231, 45220],
})
df, ccol, lcol = find_ice_table([i])
assert ccol == "Contract"
assert lcol == "Last"
assert delivery_start("Oct26") == "2026-10-01"

print("futures parser fixtures: OK")
