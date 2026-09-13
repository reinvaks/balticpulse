# BalticPulse V15.3

- EE tootmine ja tarbimine: ainult Eleringi actual väärtused. Viimane kehtiv Eleringi väärtus kuvatakse ka siis, kui avaldamine on üle 15 minuti viibega; vanus kuvatakse kasutajale.
- `plan`/forecast, ENTSO-E ega sünteetilisi asendusandmeid EE põhikPI-des ei kasutata.
- UMM kokkuvõte ja UMM tähelepanureegel on avalehe algusest eemaldatud; UMM detailvahekaart jääb alles.
- Build: 15.3.0.

Streamlit main file: `energy.app.py`.

## V15.4 reservituru YTD vaade

Reservvõimsuse põhigraafik kuvab jooksva aasta aFRR/mFRR capacity hindade kuukeskmisi EE/LV/LT lõikes. Voltoni avalik `latest.json` katab ainult jooksva 7 päeva akna, seetõttu koostab `.github/workflows/update-reserve-ytd.yml` päevaarhiividest `data/reserve_capacity_ytd.csv` faili. Workflow käivitub kord päevas ja seda saab esimesel deploy'l käsitsi käivitada (`Run workflow`), et jooksva aasta ajalugu backfill'ida. Vanad päevafailid on Voltoni dokumentatsiooni järgi immutable; viimased kolm päeva loetakse uuesti võimalike hiliste paranduste tõttu.


## V15.5 Baltikumi reaalaja süsteemivaade

- Avalehe `Olukord praegu` kuvab nüüd samad süsteemi KPI-d Eesti, Läti ja Leedu kohta: tootmine MW, tarbimine MW, taastuvtootmine MW ja taastuvate osakaal %.
- Eesti kogutootmine ja tarbimine jäävad rangelt Eleringi actual-andmeteks; ENTSO-E ei täida neid KPI-sid.
- Läti ja Leedu kogutootmine tuleb ENTSO-E A75 actual generation andmetest ning tarbimine A65 actual total load andmetest.
- Taastuvtootmine arvutatakse kõigis kolmes riigis ENTSO-E A75 tootmisliikide põhjal konservatiivselt: biomass, tavahüdro, reservuaarhüdro, päike, tuul, geotermaal, marine ja Other renewable. Pumped storage ja kogu Waste kategooria ei kuulu automaatselt taastuvate hulka.
- `Baltikumi süsteem` detailvaates on EE/LV/LT alamvaated, 24 h tootmine vs tarbimine, tootmisjaotus ning kõrvalpiiride ENTSO-E A11 tegelikud füüsilised vood.
- Läti/Leedu väärtuste juures kuvatakse vaatluse vanus. Kui tegelik vaatlus on üle 2 tunni vana, kuvatakse hoiatus; väärtust ei asendata prognoosi ega sünteetilise numbriga.
- Build: 15.5.1.

Allikapoliitika: AST operatiivvaade on AST enda sõnul valideerimata ning Litgridi veebigraafik on küll väga sage, kuid HTML/veebikihi scraping oleks habras. Seetõttu kasutab V15.5 LV/LT põhisüsteemi KPI-deks ENTSO-E Transparency Platformi actual-andmeid.


## V15.5.1 — source health
- Built directly on BalticPulse V15.5.
- `energy.app.py` and `energy_sources.py` are both version 15.5.1.
- Adds green/yellow/red source health without changing the V15.5 data architecture.
- Yellow means stale, partial, empty or mirror/fallback-like state; red means query/source failure.

## V15.6.0
- GitHub Actions stores last successful ENTSO-E EE/LV/LT system snapshot every 15 min.
- Streamlit uses direct ENTSO-E first and snapshot second.
- Failed refresh does not replace valid previous values with blanks.
- No synthetic values are generated.
- KPIs show relative change vs same time 24h earlier.
- GitHub repository secret required: `ENTSOE_API_KEY`.

## V15.6.1 hotfix
- Fixed runtime `NameError: current_price is not defined` in the source-health panel.
- The panel now uses the already-computed `current_prices["EE"]` value.
- `energy.app.py` and `energy_sources.py` are both version 15.6.1.
- Added a regression check that no undefined `current_price(...)` call remains.

## V15.6.2 — ENTSO-E 404 safe
- A75/A16 + in_Domain remains the official ENTSO-E request contract.
- Actual-data windows are now rounded to completed UTC hours.
- HTTP failures do not overwrite the last successful per-country GitHub snapshot.
- Baltic KPIs are rendered prominently with 24h percentage comparison.
- No synthetic values.


## V15.7.0 — UMM + global energy news
- UMM normalization updated to the current public Nord Pool `/messages` JSON contract.
- Nested `generationUnits`, `productionUnits`, `consumptionUnits`, `transmissionUnits`, `otherUnits` and `timePeriods` are parsed.
- UMM list defaults to newest publication/update first. V15.6 sorted by affected MW, which could make old large outages appear "stale".
- UMM polling: dashboard 60 s, UMM cache 30 s.
- Explicit UMM freshness KPI and API/UI divergence warning.
- SignalR `/messageHub` is NOT enabled yet because the hub event/payload contract remains [KONTROLLIMATA] in this build.
- New global energy-news tab: Reuters, S&P Global Energy, Energy Intelligence, Utility Dive and IEA discovery feeds; no fabricated articles.

## V15.7.1 hotfix
- Fixed BalticPulse file-version mismatch.
- `energy.app.py` and `energy_sources.py` are both `15.7.1`.
- No functional UMM/news logic changed from V15.7.0.

## V15.7.3
- Removed the second consecutive Baltic KPI overview; only the original “Olukord praegu” EE/LV/LT block remains.
- Energy news core feed now uses direct publisher RSS/Atom feeds rather than Google News.
- Added visible per-source diagnostics for partial/failed news feeds.
- News remains on-demand and cannot block startup.

## V15.8.0 — market price history
- Electricity EE/LV/LT/FI: 1 week / 1 month / 1 year / 5 years on demand from Elering NPS.
- Electricity last 12 months: time-weighted monthly mean plus exact MTU minimum/maximum and timestamps.
- Gas TTF/LVA-EST/FIN/LTU: same period selector from official EEX NGP history.
- EEX free public NGP history is officially limited to 60 days; 1y/5y gas history is not fabricated.
- Added daily GitHub Action to accumulate validated EEX NGP history in `data/gas_ngp_history.csv`.

## V15.8.1 — reliable energy news
- News is no longer fetched by Streamlit Cloud.
- GitHub Actions refreshes `data/energy_news.json` every 30 minutes.
- Curated discovery: Reuters, S&P Global Energy, IEA, Utility Dive, Energy Intelligence.
- Previous valid snapshot is preserved if a refresh returns zero stories.
- BalticPulse only reads the local JSON snapshot, so news-source DNS/TLS/Cloudflare failures cannot break the app.
