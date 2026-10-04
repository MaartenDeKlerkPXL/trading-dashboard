# Projectkeuzes

Dit bestand legt vast wat we hebben afgesproken, zodat keuzes niet ongemerkt veranderen.
Laatst bijgewerkt: fase 3.

## Afspraken

| Onderwerp | Keuze |
|---|---|
| Broker | BlackBull Markets, Standard-account, EUR, hefboom 1:100 |
| Koppeling met de broker | cTrader Open API (werkt op macOS; MT4/MT5 hebben alleen een Windows-koppeling) |
| Markten | Focus op goud (XAUUSD); ook zilver, forex en crypto beschikbaar |
| Stijl | Alles proberen: scalping t/m swing (M1 t/m D1) |
| Paper-kapitaal | € 1.000 per strategie, elke strategie een eigen virtuele rekening |
| Positiegrootte | 2% risico van het kapitaal per trade (afgestemd op de afstand tot de stop-loss) |
| Strategieën | Startset: SMA-crossover, RSI mean-reversion, Donchian-breakout, Bollinger-bands. Pine Script-omzetting kan altijd. |
| Optimaliseren | Parameter-sweep met heatmap. De out-of-sample-periode is vergrendeld en wordt nooit geoptimaliseerd. Waarschuwing bij veel geteste varianten. |
| Risicolimieten | Max 2% dagverlies, max 3 open posities, max 2% risico per trade, max positiegrootte per instrument |
| Weekend | Posities die in de winst staan worden vóór het weekend gesloten (forex/metalen) |
| E-mail (alleen urgent) | Loop gestopt/gecrasht, en broker-verbinding langer dan 15 minuten weg. Maximaal 1 mail per probleem per uur. Naar Wordpressuser.0123@gmail.com |
| Mac | Mac mini, blijft aan. De app houdt de Mac wakker zolang hij draait (komt in fase 4). |
| Interface | Nederlands. Tijden in Nederlandse tijd, opslag in UTC. |
| Kleuren grafiek | Stijgend = blauw en hol, dalend = koraalrood en gevuld (goed leesbaar bij kleurenblindheid) |

## Data

- **Fase 1:** gratis historische data van Dukascopy (bid-prijzen, zonder account). Een tussenoplossing.
- **Later:** data van BlackBull zelf via cTrader. De koersen en spreads komen dan precies overeen met die van je broker.
- **Bekend verschil:** Dukascopy-dagcandles lopen van 00:00 tot 00:00 UTC. BlackBull gebruikt servertijd (GMT+2/+3). Daardoor wijken D1- en H4-candles licht af van wat je in TradingView ziet. Met de cTrader-data verdwijnt dit verschil.

## Backtest-regels (fase 2)

- **Geen lookahead.** Een strategie ziet alleen afgesloten candles. Een signaal op candle N wordt uitgevoerd op de opening van candle N+1.
- **Prijzen.** De data zijn bid-prijzen. Kopen gebeurt op de ask (bid + spread), verkopen op de bid. Elke marktorder betaalt bovendien slippage.
- **Stop-loss is verplicht.** De positiegrootte wordt berekend op de afstand tot de stop-loss, en in live trading staat de stop-loss bij de broker. Een signaal zonder stop-loss wordt niet uitgevoerd en komt in het logboek.
- **Stop-loss en take-profit in dezelfde candle.** Het programma kan niet zien welke als eerste werd geraakt en neemt daarom aan dat het de stop-loss was (voorzichtig).
- **Koersgat.** Opent de koers voorbij de stop-loss, dan wordt de positie gesloten tegen de openingskoers. Dat is slechter dan de stop-loss, net als in het echt.
- **Kosten.** Spread, slippage, commissie per lot en financiering (standaard 6% per jaar per nacht dat een positie openstaat). Spread en slippage mogen nooit 0 zijn.
- **Omrekenen naar EUR.** Gebeurt met de EURUSD- of EURJPY-slotkoers van de vorige dag.
- **Lotgrootte.** Er zijn twee standen:
  - *Ideaal*: exacte grootte, voor zuivere percentages. Dit is de standaard in het dashboard.
  - *Realistisch*: hele lots vanaf 0,01. Is zelfs 0,01 lot te riskant, dan wordt de trade overgeslagen en gelogd.

  Met € 1.000 en 2% risico (€ 20) valt goud op H1 vaak onder die minimumgrens. Voor paper trading kiezen we daarom straks bewust een stand.
- **Sharpe en Sortino.** Berekend uit dagrendementen, op jaarbasis met 252 handelsdagen (crypto: 365).
- **Betrouwbaarheid.** Bij minder dan 30 trades staat er een waarschuwing.

## Strategieën, optimaliseren en vergelijken (fase 3)

- **Strategieën.** Elk bestand in `backend/strategies/` is één strategie met een vaste versie. Er zijn er nu vier:
  - SMA-crossover;
  - RSI-omkeer (omgezet uit Pine);
  - Donchian-uitbraak;
  - Bollinger-terugkeer.

  Een nieuwe strategie maak je vanuit `_template.py`.
- **Wijzigingen worden gemarkeerd.** Elke run bewaart een vingerafdruk van het strategiebestand. Is het bestand later aangepast, dan staat er in Historie "bestand gewijzigd". In fase 4 wordt dit een harde regel: een draaiende versie mag niet meer veranderen.
- **Elke run wordt bewaard** met parameters, kosten, versie en alle trades. Je kunt runs openen, er een notitie bij zetten, ze verwijderen en ze vergelijken (maximaal 8 tegelijk).
- **Out-of-sample.** Een backtest kan het laatste deel van de periode los testen (standaard 30%). Beide delen starten met hetzelfde kapitaal.
- **Optimaliseren.**
  - De optimizer varieert één of twee parameters, met maximaal 400 combinaties.
  - Hij test alleen op het trainingsdeel. De beste combinatie wordt daarna één keer getest op het vergrendelde deel.
  - Je krijgt een waarschuwing als:
    - er veel varianten zijn getest;
    - de beste waarde aan de rand van het bereik ligt;
    - de buren in de heatmap veel slechter scoren (dan is de uitkomst waarschijnlijk toeval).
  - Met walk-forward herhaalt hij dit over 2 tot 6 opeenvolgende vensters.
- **TradingView-import.** Je kunt de CSV van "List of trades" importeren. Trades worden gekoppeld als ze dezelfde richting hebben en hooguit één candle na elkaar openen. Uitleg staat in `docs/PINE_OMZETTEN.md`.
- **Pine-omzetting.** Plak de Pine-code in de chat. Wat niet 1-op-1 kan, staat bovenin het bestand onder "PINE DIFFERENCES".

## Nog open (vragen we op het juiste moment)

- **Reconciliatie-meldingen (fase 5):** wil je een e-mail als je eigen administratie en die van de broker niet overeenkomen? Uitleg volgt in fase 5.

## Fases

1. ✅ Projectopzet, data ophalen en tonen op een grafiek
2. ✅ Backtest-engine met één voorbeeldstrategie en metrics
3. ✅ Strategieën vergelijken, runs opslaan, CSV-import uit TradingView, Pine-omzetting, optimaliseren
4. Paper trading-loop, logging, vergelijking live vs. backtest, evaluatiekaarten
5. Risk engine, kill switch, e-mailmeldingen, reconciliatie
6. Live-executor: standaard uit, eerst alleen testen op een demo-account
