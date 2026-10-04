# TradingView-strategieën gebruiken

TradingView-strategieën zijn geschreven in Pine Script en kunnen niet in dit dashboard draaien. Er zijn twee manieren om ze toch te gebruiken.

## Route 1: laten omzetten naar Python

1. Open de strategie in TradingView: **Pine Editor**, kopieer de hele code.
2. Plak de code in de chat met Claude, met de vraag: *"Zet deze om naar een strategie voor het dashboard."*
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
| **Trailing stops** (`strategy.exit` met `trail_points`) | Nog niet ondersteund. Kan later worden toegevoegd. |
| **Indicatoren** die vanaf het begin van de grafiek rekenen (zoals `ta.rsi` en `ta.ema`) | Hier vanaf een recent venster. Het verschil is verwaarloosbaar klein. |
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
