# Ratsinfo Kamen Monitor

Überwacht automatisch das Ratsinformationssystem der Stadt Kamen
(<https://ratsportal.kamen.de/bi/info.asp>) und schickt eine E-Mail, sobald
eine neue Sitzung, ein neuer Tagesordnungspunkt oder eine neue Datei
eingestellt wird.

> Dieses Projekt ist eine an Kamen angepasste Kopie eines Monitors, der
> ursprünglich für ein anderes Ratsinformationssystem (SessionNet, PHP-Variante)
> gebaut wurde. Kamen nutzt die **ASP-Variante** von SessionNet – die
> Seitenstruktur ist nahezu identisch, lediglich die Datei-Endungen (`.asp`
> statt `.php`) unterscheiden sich. Diese sind bereits voreingestellt.
> **Es sind keinerlei persönliche Daten oder E-Mail-Adressen aus dem
> Ursprungsprojekt übernommen.**

---

## Überblick

| Datei | Zweck |
|-------|-------|
| `monitor.py` | Das eigentliche Monitor-Skript |
| `.github/workflows/monitor.yml` | GitHub-Actions-Workflow (Start über „Run workflow") |
| `recipients.json` | Verteiler mit persönlicher Anrede (Vorlage, bitte anpassen) |
| `clear_cache.py` | Hilfsskript zum Leeren des Actions-Caches |
| `assets/` | Optionaler Ort für ein Logo (`logo.png`) |

---

## Einrichtung (ca. 10 Minuten)

### Schritt 1 – Neues Repository auf GitHub erstellen

1. Gehe zu [github.com](https://github.com) → **New repository**
2. Name z. B. `kamen-ratsinfo`, **Private** wählen
3. Repository erstellen (leer lassen, kein README/gitignore ankreuzen)
4. Lade **den gesamten Inhalt dieses Ordners** hoch (nicht den Ordner selbst,
   sondern seinen Inhalt), also mindestens:
   - `monitor.py`
   - `clear_cache.py`
   - `recipients.json`
   - `.gitignore`
   - `.github/workflows/monitor.yml`

   Per Kommandozeile:

   ```bash
   cd export-kamen
   git init
   git add .
   git commit -m "Ratsinfo Kamen Monitor"
   git branch -M main
   git remote add origin https://github.com/DEIN-NAME/kamen-ratsinfo.git
   git push -u origin main
   ```

### Schritt 2 – Gmail für den Versand vorbereiten

> Du brauchst ein Gmail-Konto zum **Senden** (kann ein Extra-Konto sein).

1. Google-Konto → **Sicherheit**
2. **2-Faktor-Authentifizierung** aktivieren (falls noch nicht aktiv)
3. <https://myaccount.google.com/apppasswords> öffnen
4. App-Passwort für „Mail" / „Sonstiges" erstellen
5. Das generierte **16-stellige Passwort** notieren

### Schritt 3 – Secrets in GitHub eintragen

Im Repository: **Settings → Secrets and variables → Actions → New repository secret**

| Secret-Name      | Wert |
|------------------|------|
| `EMAIL_SENDER`   | deine-gmail@gmail.com |
| `EMAIL_PASSWORD` | Das 16-stellige App-Passwort aus Schritt 2 |
| `EMAIL_RECIPIENT`| Fallback-Verteiler, wenn keine `recipients.json` vorhanden ist (z. B. `a@x, b@y` oder `Max <max@x>`). |
| `SMTP_HOST`      | smtp.gmail.com |
| `SMTP_PORT`      | 587 |
| `TEST_MAIL_RECIPIENT` | *(optional)* Adresse, an die **nur** die automatische Erstlauf-Testmail geht. Leer lassen → Erstlauf-Testmail geht an den ganzen Verteiler. |

### Empfänger pflegen

Der Verteiler liegt in **`recipients.json`** im Repo-Root. Jeder Eintrag
beschreibt eine Adresse plus persönliche Anrede:

```json
[
  { "email": "max@beispiel.de", "name": "Max", "greeting": "Lieber Max," },
  { "email": "anna@beispiel.de", "name": "Anna", "greeting": "Liebe Anna," }
]
```

- `email` (Pflicht): Ziel-Adresse, bekommt eine eigene Mail
- `greeting` (optional): Anrede oben im Body. Fehlt sie, baut das Skript aus
  `name` eine generische („Hallo Max,") oder schreibt „Hallo,"
- `name` (optional): erscheint im To-Header (z. B. `Max <max@beispiel.de>`)

Empfänger sehen sich nicht gegenseitig im To – jede Mail geht separat raus.
Fehlt `recipients.json` (oder ist sie leer/korrupt), greift der Fallback auf
das Secret `EMAIL_RECIPIENT`.

> Die mitgelieferte `recipients.json` enthält nur Platzhalter
> (`example.com`). Bitte vor dem ersten echten Lauf durch die realen
> Empfänger ersetzen.

### Schritt 4 – Ersten Test durchführen

1. Im Repository auf **Actions**
2. **„Ratsinfo Kamen Monitor"** auswählen
3. **„Run workflow" → „Run workflow"**
4. Log prüfen – beim ersten Mal steht dort: *„Erster Durchlauf – Ausgangszustand
   gespeichert"* und es geht eine **Testmail** raus.
5. Ab dem zweiten Lauf wird verglichen und bei Änderungen eine E-Mail gesendet.

> **Wichtig – Parsing beim ersten Lauf prüfen:** Da Kamen die ASP-Variante
> von SessionNet nutzt, kann die interne Nummerierung der Seiten leicht
> abweichen. Prüfe im Log die Zeile *„Sitzungen auf Startseite: N"*. Steht
> dort `0`, obwohl auf der Seite Sitzungen zu sehen sind, passe die Muster in
> `config.json` an (siehe unten) und starte erneut.

### Testmail an eine einzelne Adresse

Um das Layout nur an eine Adresse zu testen (z. B. fürs Handy), ohne dass der
reguläre Verteiler etwas mitbekommt:

1. **Actions → „Ratsinfo Kamen Monitor" → „Run workflow"**
2. Im Feld **„Test-Lauf: Mail NUR an diese Adresse"** die Wunsch-Adresse eintragen
3. Workflow starten → die Mail geht ausschließlich dorthin und der gespeicherte
   Zustand bleibt unangetastet.

Leeres Feld = regulärer Verteiler-Lauf.

---

## Feineinstellung über `config.json` (optional)

Alle Standardwerte lassen sich ohne Code-Änderung über eine `config.json` im
Repo-Root überschreiben. Beispiel mit den Kamen-Vorgaben:

```json
{
  "base_url": "https://ratsportal.kamen.de/bi/",
  "info_page": "info.asp",
  "referer": "https://ratsportal.kamen.de/",
  "session_href_regex": "si\\d+\\.asp\\?.*__ksinr=",
  "vorlage_href_regex": "vo\\d+\\.asp",
  "run_hours": [21],
  "run_window_hours": 4,
  "request_delay": 1
}
```

- `session_href_regex` erkennt die Links zu den einzelnen Sitzungen.
  Voreingestellt: `si<Nummer>.asp?…__ksinr=…`. Falls das Portal andere
  Seitennamen verwendet, hier anpassen.
- `vorlage_href_regex` erkennt Links zu Vorlagen (`vo<Nummer>.asp`).
- `run_hours` / `run_window_hours` steuern das Zeitfenster für geplante Läufe.

---

## Wie oft wird geprüft?

Die Läufe werden extern über **cron-job.org** per „Run workflow" ausgelöst
(siehe `workflow_dispatch` im Workflow). Manuelle Läufe über die Actions-
Oberfläche umgehen das Zeitfenster und laufen sofort.

> **Hinweis:** GitHub Actions führt geplante Jobs manchmal mit einigen Minuten
> Verzögerung aus.

---

## Beispiel-E-Mail

Wenn sich etwas ändert, erhältst du eine E-Mail mit:
- Link zur Seite
- Liste der neuen/geänderten Sitzungen inkl. Tagesordnungspunkten und
  Dokument-Links

Ein Logo wird eingebettet, wenn `assets/logo.png` existiert. Fehlt die Datei,
wird die Mail einfach ohne Logo verschickt.

---

## Andere E-Mail-Anbieter (nicht Gmail)

| Anbieter | SMTP_HOST | SMTP_PORT |
|----------|-----------|-----------|
| Gmail    | smtp.gmail.com | 587 |
| GMX      | mail.gmx.net | 587 |
| Web.de   | smtp.web.de | 587 |
| Outlook  | smtp.office365.com | 587 |
| T-Online | securesmtp.t-online.de | 587 |
