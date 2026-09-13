import pandas as pd

# Representative schema observed from Elering with-plan series.
df = pd.DataFrame({
    "timestamp": [1720000000, 1720000900],
    "real": [812.5, 820.1],
    "plan": [840.0, 840.0],
})
assert "real" in df.columns
assert list(pd.to_numeric(df["real"])) == [812.5, 820.1]
assert list(pd.to_numeric(df["plan"])) != list(pd.to_numeric(df["real"]))
print("Elering timestamp/real/plan fixture: OK")
