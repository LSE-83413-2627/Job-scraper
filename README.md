# Studi-Jobs

Sucht jeden Morgen automatisch nach Werkstudentenjobs, Praktika, Einstiegsstellen und PhD-Positionen für ASDS und zeigt sie auf einer eigenen Webseite an. Neue Stellen sind farbig markiert, du kannst nach Jobart filtern, suchen und Stellen merken.

Läuft komplett kostenlos auf GitHub, ohne eigenen Server.

## Quellen

- **UK: Adzuna** über die offizielle API. Adzuna sammelt Anzeigen von vielen britischen Jobbörsen und Firmenseiten. Dafür brauchst du einen kostenlosen Schlüssel (siehe unten).
- **Deutschland: Jobbörse der Bundesagentur für Arbeit** über ihre offene Schnittstelle, ohne Schlüssel.
- **DAAD PhDGermany** (RSS-Feed mit Promotionsstellen, gefiltert nach deinen Fach-Stichwörtern)
- **jobs.ac.uk** (optional, RSS): Research Assistants und PhD Studentships an britischen Unis. Den Feed-Link trägst du selbst in der `config.yaml` ein.

## Filter auf der Seite

- **Jobart:** Werkstudent, Praktikum, Berufseinstieg, PhD / Forschung
- **Arbeitszeit:** Vollzeit, Teilzeit, ohne Angabe
- **Region:** die Orte aus der `config.yaml` (z. B. Berlin, Hamburg, London), Remote und weitere Orte aus den RSS-Feeds
- **Anzeigen:** nur heute neue Stellen oder nur gemerkte

Innerhalb einer Gruppe gilt „oder“, zwischen den Gruppen „und“. „Praktikum + Werkstudent“ und „Hamburg“ zeigt also beide Jobarten, aber nur in Hamburg. Die Zahlen an den Filtern zeigen, wie viele Stellen bei der aktuellen Auswahl dazukämen. Deine Auswahl bleibt im Browser gespeichert.

Die Arbeitszeit stammt in Deutschland aus getrennten Vollzeit- und Teilzeit-Suchen bei der Arbeitsagentur, in UK aus den Angaben bei Adzuna. Zusätzlich wird der Titel ausgewertet, z. B. „Teilzeit“ oder „20 Std.“. Werkstudentenjobs gelten ohne andere Angabe als Teilzeit.

## Einrichtung (ca. 10 Minuten)

1. **Repository anlegen:** Auf github.com ein neues, *öffentliches* Repository erstellen (GitHub Pages ist im kostenlosen Plan nur für öffentliche Repos verfügbar) und alle Dateien aus diesem Ordner hochladen, inklusive des versteckten Ordners `.github`.
2. **Adzuna-Schlüssel holen:** Auf https://developer.adzuna.com registrieren. Im Dashboard stehen danach „Application ID“ und „Application Key“. Im Repository unter Settings → Secrets and variables → Actions → „New repository secret“ zwei Secrets anlegen: `ADZUNA_APP_ID` und `ADZUNA_APP_KEY`. (Ohne Schlüssel läuft alles andere trotzdem, nur die UK-Suche fehlt dann.)
3. **Anpassen:** In `config.yaml` Suchbegriffe, Orte und optional die jobs.ac.uk-Feeds prüfen.
4. **Pages aktivieren:** Settings → Pages → bei „Source“ **GitHub Actions** auswählen.
5. **Ersten Lauf starten:** Tab „Actions“ → „Jobs aktualisieren“ → „Run workflow“.
6. Nach 2–4 Minuten ist die Seite unter `https://<dein-name>.github.io/<repo-name>/` erreichbar.

Ab dann läuft das Update jeden Morgen automatisch. Jede Änderung an `config.yaml` startet ebenfalls einen neuen Lauf.

## Lokal testen

```bash
pip install -r requirements.txt
export ADZUNA_APP_ID=... ADZUNA_APP_KEY=...   # optional, für UK
python -m scraper.main
# danach docs/index.html im Browser öffnen
```

## So funktioniert es

- `scraper/sources.py` holt die Stellen ab. In Deutschland wird für jeden Ort (Berlin und Hamburg mit je 30 km Umkreis) gezielt gesucht, z. B. „Werkstudent Data Analyst“ oder Praktikumsangebote zu „Sozialforschung“. Bei Adzuna gibt es pro Suchbegriff und Ort eine Anfrage, die nur Anzeigen mit Wörtern wie graduate, junior, intern oder placement liefert.
- `scraper/main.py` ordnet jede Stelle anhand des Titels einer Kategorie zu, wirft Ausschlusswörter (Senior, Leitung, …) und Dubletten raus und führt alles mit dem Bestand in `data/jobs.json` zusammen. Dort steht auch, wann eine Stelle zum ersten Mal gefunden wurde. Daran erkennt die Seite, was neu ist.
- Stellen, die mehrere Tage nicht mehr auftauchen, werden entfernt. Fällt eine Quelle aus, bleiben ihre Stellen erhalten, und die Seite zeigt einen Hinweis.
- `scraper/template.html` ist die Vorlage für die Webseite, die Daten werden beim Bauen direkt eingebettet.

## Feineinstellung

- **Zu viele unpassende Treffer?** Suchbegriffe spezifischer machen oder Wörter zu `ausschliessen` hinzufügen.
- **Zu wenige Treffer?** Weitere Suchbegriffe oder Orte ergänzen, Umkreis vergrößern, `max_seiten_pro_suche` erhöhen.
- **Kategorien anpassen:** Die Muster, nach denen eingeordnet wird, stehen oben in `scraper/main.py` unter `KATEGORIEN`.

## Gut zu wissen

- Das kostenlose Kontingent von Adzuna ist begrenzt. `max_anfragen_pro_lauf` in der `config.yaml` deckelt die Anfragen pro Tag (Standard: 20). Jeder zusätzliche englische Suchbegriff oder Ort kostet 1–2 Anfragen.
- Karriereportale wie LSE CareerHub, Civil Service Jobs oder Gradcracker sind nicht dabei, weil sie einen Login brauchen oder keine Schnittstelle anbieten. Für die Civil Service (GSR, GSS, Fast Stream) lohnt sich zusätzlich der E-Mail-Alert direkt auf civilservicejobs.service.gov.uk.

- GitHub startet geplante Läufe manchmal mit etwas Verspätung.
- In öffentlichen Repositories pausiert GitHub geplante Workflows nach 60 Tagen ohne Aktivität. Die täglichen Daten-Commits zählen normalerweise als Aktivität. Falls doch einmal pausiert wird, unter „Actions“ wieder aktivieren.
- Die Schnittstelle der Arbeitsagentur ist offen zugänglich, aber nicht offiziell dokumentiert und kann sich ändern. Wenn der Lauf plötzlich fehlschlägt, lohnt ein Blick auf https://github.com/bundesAPI/jobsuche-api.
- Große Portale wie StepStone, Indeed oder LinkedIn sind bewusst nicht dabei: Ihre Nutzungsbedingungen verbieten automatisiertes Auslesen.
