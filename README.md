# BalticPulse V15.3

- EE tootmine ja tarbimine: ainult Eleringi actual väärtused. Viimane kehtiv Eleringi väärtus kuvatakse ka siis, kui avaldamine on üle 15 minuti viibega; vanus kuvatakse kasutajale.
- `plan`/forecast, ENTSO-E ega sünteetilisi asendusandmeid EE põhikPI-des ei kasutata.
- UMM kokkuvõte ja UMM tähelepanureegel on avalehe algusest eemaldatud; UMM detailvahekaart jääb alles.
- Build: 15.3.0.

Streamlit main file: `energy.app.py`.


## 15.3.1 — unified source health
- BalticPulse V15.3 data/UI logic retained.
- Existing source results are reused; the health panel does not duplicate normal API loads.
- Green = source OK and sufficiently fresh.
- Yellow = source responds but data is stale, partial, empty or fallback-like.
- Red = request/authentication/source failed.
- Source-specific freshness thresholds are applied to operational data.
