# Trading Dashboard

Een lokaal trading dashboard voor backtesten, paper trading en (later) live orders.
Alles draait op je eigen Mac. Het dashboard open je in de browser op <http://localhost:8000>.

> **Status: fase 3.** Je kunt koersdata bekijken en strategieën backtesten, optimaliseren en vergelijken, en TradingView-backtests importeren.
> Er worden nog geen orders geplaatst, ook geen nep-orders.

---

## Eenmalige installatie

### 1. Python 3.12 installeren

Op je Mac staat standaard Python 3.9. Dat is te oud. Installeer een nieuwere versie via Homebrew:

1. Open **Terminal**: druk op `Cmd + Spatie`, typ `Terminal` en druk op Enter.
2. Plak dit commando en druk op Enter:

   ```
   brew install python@3.12
   ```

3. Wacht tot het klaar is. Dat kan een paar minuten duren.
4. Controleer of het gelukt is:

   ```
   python3.12 --version
   ```

   Het is gelukt als je `Python 3.12.x` ziet.

   Zie je `command not found`? Sluit Terminal dan volledig af (`Cmd + Q`), open hem opnieuw en probeer het nog een keer.

### 2. Het project op je Mac zetten

1. Installeer **GitHub Desktop** via <https://desktop.github.com> en log in met je GitHub-account.
2. Kies in GitHub Desktop **File → Clone Repository**. Selecteer `MaartenDeKlerkPXL/trading-dashboard` en klik op **Clone**.
3. De map komt standaard in `Documenten/GitHub/trading-dashboard`.

Een nieuwe versie ophalen doe je later in GitHub Desktop met **Fetch origin** en daarna **Pull origin**.

---

## Starten

**Manier 1: dubbelklikken.** Open de projectmap in Finder en dubbelklik op `start.command`.

**Manier 2: via Terminal:**

```
cd ~/Documents/GitHub/trading-dashboard
./start.sh
```

De eerste keer installeert het script de benodigde pakketten. Dat duurt ongeveer een minuut.

Het is gelukt als je dit ziet:

```
  Dashboard start op http://localhost:8000/
  Stoppen: druk op Ctrl+C in dit venster.
```

Je browser opent dan vanzelf het dashboard.

**Stoppen:** klik in het Terminal-venster en druk op `Ctrl + C`.

> Laat het Terminal-venster open zolang je het dashboard gebruikt. Het dashboard werkt alleen zolang je Mac aan staat en dit venster open is.

---

## Tests draaien

```
cd ~/Documents/GitHub/trading-dashboard
./test.sh
```

Het is gelukt als de laatste regel iets zegt als `64 passed`.

De tests gebruiken geen internet en plaatsen nooit orders.

---

## Instellingen

Alle instellingen staan in `config.toml`. Je kunt dat bestand openen met TextEdit of VS Code. Na een wijziging herstart je de app: eerst `Ctrl + C`, daarna opnieuw `./start.sh`.

API-sleutels komen later in een bestand `.env`. Kopieer daarvoor `.env.example` naar `.env`. Het bestand `.env` wordt nooit naar GitHub gestuurd.

## Code bijwerken

```
cd ~/Documents/GitHub/trading-dashboard
git pull
```

Draaide het dashboard al? Stop het dan eerst met `Ctrl + C`, haal de update op met `git pull` en start het opnieuw met `./start.sh`.

## Projectstructuur

| Map / bestand | Inhoud |
|---|---|
| `backend/` | Python-server (FastAPI): data, opslag in SQLite, backtest-engine, API |
| `backend/strategies/` | De strategieën: elk bestand is één strategie met een versie (bijv. `sma_cross_v1.py`) |
| `backend/execution/` | De "executors": backtest nu, paper en live later |
| `frontend/` | Het dashboard: `index.html`, `style.css` en één script per pagina (`script.js`, `backtest.js`, `optimize.js`, `compare.js`, `history.js`, gedeeld: `common.js`) |
| `docs/` | Uitleg, zoals het omzetten van TradingView-strategieën |
| `tests/` | Automatische tests |
| `data/` | Je lokale database en logbestanden (niet op GitHub) |
| `PROJECT.md` | Gemaakte keuzes en de planning per fase |
