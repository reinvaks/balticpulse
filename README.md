# BalticPulse

Balti ja Põhjamaade energiaturu operatiivne olukorrapilt Streamlitile. Põhimõte: operatiivses põhivaates kuvatakse ainult piisavalt värsked tegelikud väärtused; vananenud või puuduva allika korral näidatakse `—`, mitte sünteetilist asendusnumbrit.

## Main file

`energy.app.py`

## Streamlit Secrets

```toml
ENTSOE_API_KEY = "sinu-ENTSO-E-Transparency-Platform-võti"
GIE_AGSI_API_KEY = "sinu-GIE-AGSI-võti"
```

## Operatiivse vaate värskus

- Rakendus värskendab brauserivaadet automaatselt iga **2 minuti** järel.
- Elering spot: käimasolev 15-min Market Time Unit (day-ahead clearing price).
- Elering tootmine/tarbimine: põhivaates ainult kuni **30 min** vana vaatlus.
- ENTSO-E A75/A65 fallback: põhivaates ainult kuni **120 min** vana actual-väärtus.
- ENTSO-E A11 füüsilised vood: põhivaates ainult kuni **120 min** vana vaatlus.
- Nord Pool UMM: cache **120 s**; teateid ei summeerita automaatselt netokatkestuseks.
- EEX NGP TTF/LVA-EST/FIN/LTU: EEX current failid, ametlikult **15-min refresh** D/D+1/D+2 jaoks.
- Baltic Transparency Dashboard / Volton balancing data: avalik peegel värskendub **tunnis**; MTU ise on 15 min.
- GIE AGSI+: päevane gas-day andmestik, mitte intraday.
- EIA Brent: ametlik päevane spot-referents, mitte intraday.
- EEX EUA: primaaroksjoni clearing price, mitte secondary-market intraday hind.

## Andmeallikad

- **Elering Dashboard API** — EE/LV/LT/FI day-ahead elektrihinnad ning Eesti süsteemi tootmine/tarbimine.
- **ENTSO-E Transparency Platform** — Eesti tegelik tootmine tootmisliikide kaupa (A75), tegelik koormus (A65), EE–FI/EE–LV füüsilised vood (A11) ja päev-ette NTC (A61).
- **Nord Pool UMM** — REMIT/UMM turuteated ja mõjutatud võimsus.
- **Baltic Transparency Dashboard / Volton public mirror** — Balti aFRR/mFRR capacity ja balancing-energy hinnad.
- **GIE AGSI+** — EL ja Läti gaasihoidlate päevased andmed.
- **EEX** — current Neutral Gas Price TTF, LVA-EST, FIN ja LTU; TTF 60 päeva final history.
- **U.S. EIA** — Europe Brent Spot Price FOB.
- **EEX** — EUA Primary Auction clearing price 2026.

## Metoodika

BalticPulse ei sünteesi puuduvaid turuandmeid. Kui Eleringi tootmise/tarbimise väärtus puudub või on liiga vana, võib rakendus kasutada ainult piisavalt värsket ENTSO-E tegelikku tootmist (A75) või tegelikku koormust (A65) fallback'ina.

Päev-ette spot-hind on käimasoleva MTU turuhind, kuid see ei ole intraday uuesti kliiritav hind. EEX NGP on spot-turu referents ja uueneb 15 minuti järel. EIA Brent ning EEX EUA oksjon on aeglasemad fundamentaalnäitajad ning UI märgib need vastavalt.

UMM-idest kasutatakse operatiivses aktiivsete teadete loendis viimast avaldatud revisjoni message ID kohta. Mõjutatud MW väärtusi ei liideta automaatselt, sest teated võivad kirjeldada kattuvaid sündmusi.

## Deploy

1. Laadi failid GitHubi repo juurkausta.
2. Lisa Streamlit Cloudis secrets.
3. Main file path: `energy.app.py`.
4. App URL: `balticpulse.streamlit.app` (kui nimi on saadaval).
