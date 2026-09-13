import importlib.util
from pathlib import Path
import pandas as pd

spec = importlib.util.spec_from_file_location("fut", Path("fetch_futures_snapshot.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

# Delivery parsing
assert m.delivery_start("Oct 2026") == "2026-10-01"
assert m.delivery_start("Q4 2026") == "2026-10-01"
assert m.delivery_start("2027") == "2027-01-01"
assert m.delivery_start("Oct26") == "2026-10-01"
assert m.delivery_start("Q4 26") == "2026-10-01"
assert m.delivery_start("Winter26") == "2026-10-01"

# Euronext table recognition
edf = pd.DataFrame({
    "Delivery": ["Oct 2026", "Nov 2026"],
    "Settl.": [54.58, 72.00],
    "O.I": [123, 50],
})
table, dcol, scol = m.find_euronext_table([edf])
assert table is not None and dcol == "Delivery" and scol == "Settl."

# ICE table recognition
idf = pd.DataFrame({
    "Contract": ["Oct26", "Nov26", "Q4 26"],
    "Last": [82.010, 81.735, 81.665],
    "Time(GMT)": ["x","x","x"],
    "Volume": [119231,45220,5610],
})
table, ccol, lcol = m.find_ice_table([idf])
assert table is not None and ccol == "Contract" and lcol == "Last"

print("futures parser fixtures: OK")
