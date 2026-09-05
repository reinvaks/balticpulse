# Regiooni energeetika põhiülevaade — lõppversioon

Streamlit armatuurlaud Eesti, Baltikumi ja Soome operatiivse energiaturu olukorrapildi jaoks.

## Sisuline ülesehitus
1. **Olukord praegu** — EE/FI spot, EE–FI hinnavahe, Eesti tootmine/tarbimine, aktiivsed UMM-id, suurim üksik mõjutatud MW ning EE–FI / EE–LV füüsilised netovood.
2. **Täna/homme** — EE/LV/LT/FI päeva-ette keskmised.
3. **Olulised sündmused** — aktiivsed Nord Pool UMM-id prioriseerituna mõjutatud võimsuse järgi.
4. **Elektrihinnad** — regionaalne päev-ette profiil Eleringist.
5. **Eesti süsteem** — Eleringi tegelik kogutootmine ja -tarbimine.
6. **ENTSO-E** — Eesti tegelik tootmine tootmisliigi kaupa (A75/A16) ning EE↔FI ja EE↔LV füüsilised vood (A11).
7. **Reservid** — Balti aFRR/mFRR balancing-capacity (€/MW/h) ja aktiveeritud balancing-energy clearing prices (€/MWh) EE/LV/LT.
8. **Gaasihoidlad** — EL ja Läti GIE AGSI+ päevased täituvuse, laovaru, süstimise ja väljavõtu andmed.
9. **Tähelepanuplokk** — läbipaistvad heuristikad suurte hinnavahede, UMM mõju, bilansivajaduse, ühenduste kõrge kasutuse, balancing-energy hinnahüpete ja gaasihoidlate kiire muutuse jaoks.
10. **Andmekvaliteet** — allikate staatus, värskus ja semantilised piirangud.

## Streamlit Cloud
Main file path:

```text
energy.app.py
```

### Secrets
Lisa Streamlit Cloud → Settings → Secrets:

```toml
GIE_AGSI_API_KEY = "sinu-GIE-AGSI-võti"
ENTSOE_API_KEY = "sinu-ENTSOE-Transparency-võti"
```

Võtmeid ei salvestata koodi ega GitHubi reposse.

## Valideeritud andmeallikad ja kasutus
- **Elering Dashboard API** — EE/LV/LT/FI spot-hinnad ning Eesti kogutootmine/-tarbimine.
- **ENTSO-E Transparency Platform Web API** — A75 actual generation per type (process A16 realised); A11 physical flows EE↔FI ja EE↔LV.
- **Nord Pool UMM** — kiireloomulised turuteated ja teatepõhine mõjutatud võimsus.
- **Baltic Transparency Dashboard / Elering via Volton Public Data** — aFRR/mFRR balancing capacity ning balancing-energy clearing prices.
- **GIE AGSI+** — EL ja Läti gaasihoidlad; autentimine `x-key` päisega.

## Olulised kvaliteedireeglid
- Puuduvaid turuandmeid ei sünteesita.
- UMM MW väärtusi ei summeerita automaatselt süsteemi netokatkestuseks.
- ENTSO-E **physical flow (A11)** ei ole sama asi mis available transfer capacity.
- Reservi capacity hind (€/MW/h) ei ole balancing-energy hind (€/MWh).
- Päev-ette börsihind ei ole lõpptarbija hind.
- AGSI+ on päevane andmestik; seda ei esitata intraday reaalaja mõõdikuna.
- Eleringi ja ENTSO-E andmeid kasutatakse ristkontrolliks, kuid neid ei sunnita kunstlikult võrdseks.

## GIE API käitumine
Rakendus küsib `https://agsi.gie.eu/api` endpointi `x-key` päisega ning kasutab operatiivvaates `reverse=true`, et saada uusimad gaasipäevad. API võtme puudumisel kuvatakse selge konfiguratsiooniteade.

## ENTSO-E API käitumine
Rakendus kasutab `https://web-api.tp.entsoe.eu/api` endpointi. XML veateated ja HTTP vead tuuakse kasutajale nähtavale. Päringud on cache'itud 5 minutiks, et vältida asjatut koormust ja rate-limit riski.


## V4: ülekandevõimsus ja kaks täiendavat operatiivnäitajat

- ENTSO-E A61 + `contract_MarketAgreement.Type=A01`: EE–FI ja EE–LV päev-ette suunaline NTC mõlemas suunas.
- A11 füüsiline voog jääb eraldi; `voog / NTC` on ainult kontekstinäitaja, mitte vaba jääkvõimsus.
- `EE bilansivajadus` = Eleringi tegelik tarbimine − tegelik kodumaine tootmine.
- `Taastuvate osakaal tootmises` arvutatakse ENTSO-E A75 viimase ühise tootmisvaatluse PSR-liikidest ja on operatiivne indikatsioon, mitte ametlik statistiline taastuvenergia osakaal.


## Tähelepanureeglid
Need on dashboardi heuristikad, mitte ametlikud häirepiirid:
- |EE–FI spot spread| ≥ 50 €/MWh (kõrge ≥ 100)
- suurim üksik aktiivne UMM ≥ 300 MW (kõrge ≥ 600)
- EE tarbimine − tootmine ≥ 500 MW (kõrge ≥ 800)
- füüsiline voog / sama suuna päev-ette NTC ≥ 90% (kõrge ≥ 100%)
- aFRR/mFRR balancing-energy |hind| ≥ 500 €/MWh (kõrge ≥ 1000)
- EU või LV gaasihoidlate täituvus < 30% või ~7 päeva langus ≥ 5 protsendipunkti

Lävendid on UI-s kasutajale nähtavad ja neid saab vajadusel hiljem konfiguratsioonifaili tõsta.
