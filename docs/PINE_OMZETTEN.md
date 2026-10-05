# Strategieën toevoegen

Er zijn drie manieren om een strategie aan het dashboard toe te voegen.

| Manier | Wanneer | Wat jij doet |
|---|---|---|
| **A. TradingView-strategie laten omzetten** (route 1 hieronder) | Je vindt een strategie op TradingView | Plak de link **en** de Pine-code in de chat |
| **B. Een idee in gewone taal** | Je hebt zelf een idee, bijvoorbeeld "koop goud als de RSI onder 30 komt en de prijs boven het 200-daags gemiddelde staat" | Beschrijf het in de chat: wanneer erin, wanneer eruit, waar de stop-loss ligt |
| **C. Zelf programmeren** | Je wilt zelf Python schrijven | Kopieer `backend/strategies/_template.py` naar `mijn_idee_v1.py` en vul het in |

Na A of B maak ik een nieuw bestand in `backend/strategies/` met tests. Jij haalt het op met `git pull` en herstart het dashboard. De strategie staat dan in de keuzelijst bij Backtest, Optimaliseren, Paper trading en (via een paper-strategie) Live.

Een strategie aanpassen die al in paper of live draait, kan niet: dan wordt er een nieuwe versie gemaakt (`_v2`). De oude versie blijft ongewijzigd, zodat je resultaten nooit ongemerkt veranderen.

Voorbeelden van omgezette TradingView-strategieën:
- `rsi_reversal_v1.py`, uit de Pine-code van TradingView's klassieke "RSI Strategy";
- `bjorgum_3commas_v1.py`, nagebouwd uit de beschrijving van Bjorgums "3Commas Bot". De Pine-code kon niet worden gelezen, dus de standaardwaarden zijn een schatting.
- `bjorgum_3commas_v2.py`, regel voor regel omgezet uit de Pine-code van dezelfde "3Commas Bot". De tests vergelijken hem trade voor trade met een letterlijke nabootsing van het Pine-script, bij negen verschillende instellingen.

# TradingView-strategieën gebruiken

TradingView-strategieën zijn geschreven in Pine Script en kunnen niet in dit dashboard draaien. Er zijn twee manieren om ze toch te gebruiken.

## Route 1: laten omzetten naar Python

1. Open de strategie in TradingView: **Pine Editor**, kopieer de hele code.
2. Plak de code in de chat met Claude, met de vraag: *"Zet deze om naar een strategie voor het dashboard."*
   Alleen een link is niet genoeg: TradingView is niet bereikbaar vanaf de ontwikkelomgeving. Bij een open-source script staat de code onder de grafiek op de scriptpagina (knop **Source code**), of in de Pine Editor via **Add to chart** en dan het **{}**-icoon. Invite-only- en protected-scripts hebben geen leesbare code: die zijn alleen na te bouwen vanuit de beschrijving.
3. Claude maakt een nieuw bestand in `backend/strategies/`, bijvoorbeeld `mijn_strategie_v1.py`. Bovenin staat:
   - de originele Pine-code;
   - een lijst **PINE DIFFERENCES**: alles wat niet 1-op-1 na te bootsen is.
4. Haal de update op met `git pull` en herstart het dashboard. De strategie staat dan in de keuzelijst.

Een voorbeeld is `rsi_reversal_v1.py`, omgezet uit TradingView's klassieke "RSI Strategy".

### Wat nooit precies hetzelfde wordt (en waarom)

| Pine-gedrag | Hoe het hier werkt |
|---|---|
| **Repainting** (`request.security` met `lookahead_on`, of signalen op een candle die nog niet gesloten is) | Kan hier niet: een strategie ziet alleen gesloten candles. Strategieën die in TradingView "te mooi" zijn, presteren hier vaak minder. Dat is eerlijker. |
| **Bar magnifier** en `calc_on_every_tick` | Niet na te bootsen: de engine kent alleen open/hoog/laag/slot per candle. Raken stop-loss en take-profit in dezelfde candle, dan rekenen we met de stop-loss (voorzichtig). |
| **Pyramiding** (meerdere instappen in dezelfde richting) | Nog niet ondersteund: één positie tegelijk per strategie. |
| `process_orders_on_close=true` | Hier wordt altijd uitgevoerd op de opening van de volgende candle. |
| **Positiegrootte** (`default_qty_type`, contracten) | Hier altijd op basis van risico: 2% van het kapitaal tot de stop-loss. Instap- en uitstapmomenten kunnen gelijk zijn, de percentages niet. |
| **Geen stop-loss** in de Pine-code | Hier is een stop-loss verplicht. Bij de omzetting wordt er een toegevoegd (meestal op basis van ATR). Dit staat altijd in de lijst met verschillen. |
| **Kosten** (Pine rekent standaard zonder commissie en slippage) | Hier altijd spread, slippage en financiering. |
| **Trailing stops** (`strategy.exit` met `trail_points`/`trail_offset`) | Ondersteund: een strategie kan de stop-loss elke candle dichter naar de koers zetten (`Signal("adjust", stop_loss=...)`). In paper trading gebeurt dat per minuut, live bij de broker zelf. Verder weg zetten mag niet. |
| **Keuzelijsten** (`input.string(options=[...])`, bijvoorbeeld het soort gemiddelde) | Ondersteund: verschijnen als keuzelijst in het dashboard. |
| **Indicatoren** die vanaf het begin van de grafiek rekenen (zoals `ta.rsi`, `ta.ema`, `ta.rma`, `ta.atr`) | Hier rekenen ze vanaf het begin van de geladen data, op dezelfde manier als Pine. TradingView laadt meer geschiedenis, dus de allereerste signalen van een periode kunnen iets verschillen. |
| **Tijdzone en sessies** | Hier altijd UTC-candles. De dagcandles van je broker (servertijd) kunnen iets anders lopen. |

## Route 2: de TradingView-backtest importeren ter controle

Hiermee vergelijk je de uitkomst van TradingView met die van dit dashboard, trade voor trade.

1. Voeg in TradingView de strategie toe aan de grafiek en open de **Strategy Tester**.
2. Ga naar het tabblad **List of trades** en klik op het exporteer-icoon. Kies **CSV**.
3. Ga in het dashboard naar **Historie** en open **TradingView-backtest importeren**.
4. Kies het CSV-bestand, het instrument en de timeframe. Kies bij tijdzone wat er rechtsonder in je TradingView-grafiek staat.
5. Zet de import en een eigen backtest van dezelfde strategie en periode samen in **Vergelijken**. Je ziet dan:
   - welke trades in beide voorkomen;
   - welke trades maar in één van de twee voorkomen;
   - hoe ver de instapprijzen uit elkaar liggen.

Kleine verschillen zijn normaal. De koersdata komen van een andere bron (Dukascopy in plaats van de koersen van BlackBull in TradingView) en de kosten verschillen. Grote verschillen in het *aantal* trades of in de *richting* wijzen op een fout in de omzetting.
