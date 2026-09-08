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
