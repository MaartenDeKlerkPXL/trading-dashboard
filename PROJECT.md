# Projectkeuzes

Dit bestand legt vast wat we hebben afgesproken, zodat keuzes niet ongemerkt veranderen.
Laatst bijgewerkt: fase 1.

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

## Nog open (vragen we op het juiste moment)

- **Reconciliatie-meldingen (fase 5):** wil je een e-mail als je eigen administratie en die van de broker niet overeenkomen? Uitleg volgt in fase 5.
- **Minimale lotgrootte (fase 2):** met € 1.000 en 2% risico (€ 20) is de kleinste goudpositie (0,01 lot) soms al te groot als de stop-loss ver weg staat. Daarvoor kiezen we een regel: de trade overslaan, of loggen dat het risico hoger is.

## Fases

1. ✅ Projectopzet, data ophalen en tonen op een grafiek
2. Backtest-engine met één voorbeeldstrategie en metrics
3. Strategieën vergelijken, runs opslaan, CSV-import uit TradingView, Pine-omzetting
4. Paper trading-loop, logging, vergelijking live vs. backtest, evaluatiekaarten
5. Risk engine, kill switch, e-mailmeldingen, reconciliatie
6. Live-executor: standaard uit, eerst alleen testen op een demo-account
