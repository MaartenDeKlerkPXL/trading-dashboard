# E-mailmeldingen instellen

Het dashboard stuurt alleen een e-mail bij **urgente** problemen:

- de paper trading-loop werkt niet meer (3 rondes achter elkaar mislukt) of is gecrasht;
- het dashboard is onverwacht gestopt; je krijgt dan een mail zodra het weer start;
- de koersen of de broker zijn langer dan 15 minuten onbereikbaar.

Je krijgt hooguit **één e-mail per probleem per uur**. Alle andere meldingen staan alleen op de pagina
**Risico & alerts**. Denk aan geweigerde orders, een geraakte risicolimiet of een afwijking bij de reconciliatie.

Stel dit in met een **app-wachtwoord** van Gmail. Dat is een apart wachtwoord dat alleen mails mag versturen.
Je echte Gmail-wachtwoord komt nergens in te staan.

## 1. Een app-wachtwoord aanmaken

1. Zet eerst **verificatie in twee stappen** aan als dat nog niet zo is. Ga naar
   <https://myaccount.google.com/signinoptions/twosv> en volg de stappen.
2. Ga naar <https://myaccount.google.com/apppasswords>.
3. Vul bij **App-naam** `Trading Dashboard` in en klik op **Maken**.
4. Google toont een wachtwoord van 16 letters, bijvoorbeeld `abcd efgh ijkl mnop`. Kopieer het.
   Je ziet het maar één keer.

Zie je de pagina met app-wachtwoorden niet? Dan staat verificatie in twee stappen nog niet aan.

## 2. Het wachtwoord in `.env` zetten

1. Open Terminal en ga naar de projectmap:

   ```
   cd ~/Documents/GitHub/trading-dashboard
   ```

2. Maak het bestand `.env` aan, als het er nog niet is:

   ```
   cp -n .env.example .env
   open -e .env
   ```

3. TextEdit opent. Vul deze drie regels in:

   ```
   SMTP_USER=Wordpressuser.0123@gmail.com
   SMTP_APP_PASSWORD=abcdefghijklmnop
   ALERT_EMAIL_TO=Wordpressuser.0123@gmail.com
   ```

   Spaties in het app-wachtwoord mogen; het dashboard haalt ze weg.
4. Sla het bestand op met `Cmd + S`.
5. Herstart het dashboard: druk op `Ctrl + C` en start het opnieuw met `./start.sh`.

Het bestand `.env` staat in `.gitignore`. Het gaat dus nooit naar GitHub. Het wachtwoord komt ook nooit in logbestanden
of in het dashboard terecht.

> Stuur het app-wachtwoord nooit in de chat. Het hoort alleen in `.env`.

## 3. Testen

Ga in het dashboard naar **Risico & alerts** en klik op **Testmail versturen**. Binnen een minuut staat er een mail
in je inbox. Kijk ook in je spammap. Markeer de mail als "geen spam", dan komen de echte meldingen ook aan.

| Melding | Oplossing |
|---|---|
| "Inloggen bij Gmail mislukt" | Controleer `SMTP_USER` en `SMTP_APP_PASSWORD` in `.env`. Het moet een app-wachtwoord zijn, niet je gewone wachtwoord. |
| "Geen e-mail ingesteld" | `.env` ontbreekt of is niet ingevuld. Heb je het dashboard na het invullen herstart? |
| "Versturen mislukt … internet" | De Mac heeft geen verbinding. |

## 4. Optioneel: bewaking van buitenaf (heartbeat)

Als de Mac zelf uitvalt (stroom weg, crash), kan het dashboard niets meer versturen. Daarvoor is een externe "heartbeat"
handig. Het dashboard geeft dan elke ronde een seintje aan een gratis dienst. Blijven die seintjes weg, dan mailt die
dienst jou.

1. Maak een gratis account op <https://healthchecks.io>.
2. Klik op **Add Check**. Zet **Period** op 5 minuten en **Grace** op 15 minuten.
3. Kopieer de ping-URL, bijvoorbeeld `https://hc-ping.com/1234abcd-…`.
4. Zet die in `.env`:

   ```
   HEARTBEAT_URL=https://hc-ping.com/1234abcd-…
   ```

5. Herstart het dashboard.

Let op: stop je het dashboard zelf, dan mailt healthchecks.io je ook. Pauzeer de check daar als je het dashboard
bewust een tijd uitzet.
