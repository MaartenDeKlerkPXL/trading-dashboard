# cTrader voorbereiden

> Daarna verder met `docs/LIVE_TRADING.md`.

Voor echte orders, en later ook voor koersen rechtstreeks van BlackBull, praat het dashboard met BlackBull via de **cTrader Open API**. Daarvoor heb je twee dingen nodig. Je kunt ze nu al regelen, want de goedkeuring van de app kan een paar dagen duren.

## 1. Een cTrader-demo-account bij BlackBull

1. Log in op het klantenportaal van BlackBull Markets.
2. Open een nieuw **demo-account** en kies als platform **cTrader**. Een bestaand MT4/MT5-demo-account werkt hier niet.
3. Kies als accountvaluta **EUR**, zet de hefboom op **1:100**, en neem als nepsaldo bijvoorbeeld € 1.000.
4. Je krijgt een **cTrader ID** (e-mail en wachtwoord). Daarmee log je in bij cTrader.

## 2. Een app registreren bij cTrader

1. Ga naar <https://openapi.ctrader.com/apps> en log in met je cTrader ID.
2. Klik op **Add new App** en vul in:
   - **Application name:** `Trading Dashboard`
   - **Description:** `Persoonlijk dashboard voor backtesten en paper trading`
3. Klik op **Add redirect URL** en vul exact in: `http://localhost:8000/ctrader/callback`
4. Sla op. De status staat dan op **Submitted**. Wacht tot die op **Active** staat. Dat kan een paar dagen duren.

Bronnen: [cTrader Help – App and account authentication](https://help.ctrader.com/open-api/account-authentication/).

## Belangrijk

- Stuur de **Client ID** en **Secret** nooit in de chat en zet ze nergens anders neer dan in het bestand `.env`. Hoe dat gaat, staat in `docs/LIVE_TRADING.md`.
- Bij de koppeling vraagt cTrader welke rechten de app krijgt. Geef alleen **trading**-rechten. Opnemen of overboeken van geld kan via deze API sowieso niet.
