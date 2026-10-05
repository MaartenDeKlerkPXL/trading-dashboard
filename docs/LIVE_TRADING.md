# Live trading via cTrader (BlackBull Markets)

Live trading staat standaard **uit**. Voordat er een echte order de deur uitgaat, zijn er vier drempels:

1. In `config.toml` staat `mode = "live"` (onder `[execution]`).
2. Je hebt het dashboard met je cTrader ID gekoppeld en een account gekozen.
3. Bij het starten van elke live-strategie typ je drie bedragen over: het kapitaal, het risico per trade en het maximale verlies per dag.
4. Echt geld blijft geweigerd tot je in `config.toml` ook `allow_real_money = true` zet (onder `[live]`). Een demo-account mag altijd.

**Begin altijd op een demo-account.**

## 1. Een app bij cTrader (eenmalig)

Heb je dit al gedaan via `docs/CTRADER_VOORBEREIDEN.md`? Ga dan door naar stap 2.

1. Ga naar <https://openapi.ctrader.com/apps> en log in met je cTrader ID.
2. Maak een app aan met redirect-URL **exact** `http://localhost:8000/ctrader/callback`.
3. Wacht tot de status op **Active** staat.

## 2. De sleutels in `.env` zetten

1. Klik bij je app op **Credentials**. Je ziet een **Client ID** en een **Secret**.
2. Open `.env` in TextEdit:

   ```
   cd ~/Documents/GitHub/trading-dashboard
   cp -n .env.example .env
   open -e .env
   ```

3. Vul in, sla op met `Cmd + S`:

   ```
   CTRADER_CLIENT_ID=...
   CTRADER_CLIENT_SECRET=...
   ```

   De andere twee cTrader-regels (`CTRADER_ACCESS_TOKEN`, `CTRADER_ACCOUNT_ID`) mag je leeg laten. Het dashboard regelt die zelf.

> Stuur de Client ID en de Secret nooit in de chat, en zet ze nergens anders dan in `.env`. Dat bestand gaat nooit naar GitHub en de waarden komen nooit in de logbestanden.

## 3. Live-modus aanzetten

Open `config.toml` en verander onder `[execution]`:

```
mode = "live"
```

Laat `allow_real_money = false` staan. Herstart het dashboard: `Ctrl + C`, daarna `./start.sh`. Rechtsboven staat nu **Live**.

## 4. Koppelen en een account kiezen

1. Ga in het dashboard naar **Live**.
2. Klik op **Koppelen met cTrader**. Je komt bij cTrader.
3. Log in en geef toestemming. cTrader vraagt om **trading**-rechten. Dat is alles wat het dashboard nodig heeft.
   - Via de Open API kan geen geld worden opgenomen of overgemaakt. Die rechten bestaan in deze koppeling niet.
   - Je kunt de toegang altijd intrekken in cTrader, of met **Koppeling verwijderen** in het dashboard.
4. Je komt terug in het dashboard. Klik op **Accounts ophalen**, kies je **demo**-account en klik op **Kiezen**.
5. Controleer de tabel **Instrumenten bij de broker**. Bij XAUUSD hoort een vinkje: 1 lot is dan 100 ounce, net als in de backtests.

De koppeling (toegangssleutel) wordt opgeslagen in `data/ctrader_token.json` (alleen leesbaar voor jou, niet op GitHub). Ze wordt automatisch verlengd voordat ze verloopt.

## 5. De testorder (alleen demo)

Klik op **Testorder plaatsen** (twee keer). Het dashboard:

1. koopt de kleinste positie goud (0,01 lot) met een stop-loss van 10 dollar;
2. controleert of die stop-loss **bij de broker** staat;
3. sluit de positie meteen weer.

Je ziet de stappen onder de knop en de trade in cTrader. Lukt dit, dan werkt de hele keten.

## 6. Een live-strategie starten

1. Kies een paper-strategie als voorbeeld. Strategie, versie, instelling, instrument en timeframe worden overgenomen.
2. Vul het kapitaal in (maximaal `live.max_capital`, standaard € 1.000) en het risico per trade (maximaal 2%).
3. Klik op **Controleren**. Je ziet precies wat er gaat gebeuren.
4. Typ de drie bedragen over. Pas als ze kloppen, wordt **Live starten** klikbaar.

## Hoe het werkt

- **Dezelfde strategiecode** als in backtest en paper trading. Alleen de uitvoering verschilt.
- **Koersen van BlackBull zelf**: de minuutcandles komen via cTrader, dus ze komen overeen met wat je in cTrader ziet.
- **De stop-loss staat bij de broker.** Hij gaat mee met de order, zodat de positie vanaf het eerste moment beschermd is. Daarna wordt hij op het exacte niveau gezet. Ook als het dashboard of de Mac uitvalt, blijft hij staan. Ontbreekt er toch een stop-loss, dan zet het dashboard hem alsnog en krijg je een e-mail.
- **Nooit dubbele orders.** Elke order krijgt een uniek nummer dat eerst wordt opgeslagen en pas daarna verstuurd. Is na een storing niet duidelijk of een order is aangekomen, dan zoekt het dashboard hem op bij de broker. Hij wordt nooit opnieuw verstuurd.
- **Trades komen uit de administratie van de broker.** Sluit de broker een positie (stop-loss of take-profit), dan haalt het dashboard prijs, commissie en swap uit de deal-geschiedenis van de broker.
- **Reconciliatie** draait elke 5 minuten en vergelijkt per strategie:
  - je eigen administratie met de open posities bij de broker;
  - of elke positie een stop-loss heeft;
  - of er posities met het dashboard-label zijn zonder actieve strategie.

  Je krijgt een e-mail bij een afwijking.
- **Risicolimieten en weekendregel** gelden net als bij paper trading. Je kunt ze niet uitzetten voor live.
- **Kill switch en Stoppen** sluiten live-posities direct bij de broker en boeken de trade meteen. Lukt dat niet (bijvoorbeeld zonder verbinding), dan krijg je een e-mail. De stop-loss blijft dan bij de broker staan.
- **Verbinding weg?** Na 15 minuten zonder verbinding met cTrader krijg je een e-mail.

## Echt geld (later)

Pas als een strategie op demo lang genoeg doet wat de backtest en paper trading lieten zien:

1. Zet in `config.toml` `allow_real_money = true` (onder `[live]`) en herstart.
2. Kies je echte account onder **Live** (je moet eerst alle live-strategieën stoppen).
3. Begin met een klein kapitaal.

Overleg dit eerst met mij. Dan lopen we samen de cijfers na.
