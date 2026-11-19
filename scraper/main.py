"""Täglicher Lauf: Jobs abrufen, einordnen, mit dem Bestand zusammenführen, Seite bauen.

Aufruf:  python -m scraper.main
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from . import sources

ROOT = Path(__file__).resolve().parent.parent
DATEN = ROOT / "data" / "jobs.json"
SEITE = ROOT / "docs" / "index.html"
VORLAGE = Path(__file__).resolve().parent / "template.html"
ZEITZONE = ZoneInfo("Europe/Berlin")

log = logging.getLogger("studijobs")

# Muster im Titel (reguläre Ausdrücke) → Kategorie. Reihenfolge = Priorität.
KATEGORIEN = [
    ("phd", [r"doktorand", r"\bph\.?d\b", r"promotionsstelle", r"zur promotion", r"\(promotion",
             r"promovierend", r"doctoral", r"doctorate", r"studentship",
             r"wissenschaftliche\S* mitarbeit", r"research (assistant|officer)",
             r"predoc", r"pre-doc"]),
    ("werkstudent", [r"werkstudent", r"working student", r"studentische\S* (hilfskraft|aushilfe|mitarbeit)",
                     r"student assistant", r"studentenjob", r"\bshk\b", r"part[- ]time student",
                     r"student (analyst|researcher|data)"]),
    ("praktikum", [r"praktik", r"internship", r"\bintern\b", r"placement", r"abschlussarbeit",
                   r"masterarbeit", r"bachelorarbeit", r"\bthesis\b", r"summer analyst"]),
    ("trainee", [r"trainee", r"graduate (program|programme|scheme)", r"nachwuchs(förder)?programm",
                 r"einstiegsprogramm", r"rotation(al)? program", r"junior[- ]?programm"]),
    ("einstieg", [r"junior", r"absolvent", r"einsteiger", r"berufsstart", r"\bgraduate",
                  r"entry[- ]level", r"young professional", r"einstieg",
                  r"nachwuchs", r"volont[äa]r", r"fast stream", r"associate analyst",
                  r"analyst i\b", r"early careers?"]),
]

# Hinweise in der Stellenbeschreibung, dass eine Stelle für Berufseinsteiger passt …
EINSTIEG_JA = [
    r"berufseinsteiger", r"berufseinstieg", r"absolvent", r"einstiegsposition", r"einstiegsstelle",
    r"direkteinstieg", r"(ersten?|erster) (praktischen? |relevanten? |einschlägigen? )?(berufs|praxis)?erfahrung",
    r"keine berufserfahrung", r"ohne berufserfahrung",
    r"berufserfahrung (ist |sind )?(von vorteil|wünschenswert|nicht (zwingend )?erforderlich|keine voraussetzung|kein muss)",
    r"\b[01]\s*(-|–|bis)\s*2 jahre", r"praktika|werkstudententätigkeit",
    r"entry[- ]level", r"recent graduate", r"graduates?\b", r"first (professional |work |relevant )?experience",
    r"no (prior )?experience (is )?(required|necessary)", r"\b[01]\s*(-|–|to)\s*2 years", r"\bjunior\b",
]
# … und Hinweise, dass deutlich mehr Erfahrung verlangt wird
EINSTIEG_NEIN = [
    r"mehrjährige", r"langjährige", r"fundierte (berufs)?erfahrung", r"umfangreiche (berufs)?erfahrung",
    r"(mindestens|min\.|wenigstens) ([3-9]|drei|vier|fünf|sechs)\s*(jahre|jahren)",
    r"\b([3-9]|1[0-9])\s*\+?\s*(jahre|jahren)\b[^.]{0,40}erfahrung",
    r"(at least|minimum( of)?) ([3-9]|three|four|five)\s*\+?\s*years",
    r"\b([3-9]|1[0-9])\s*\+?\s*years\b[^.]{0,40}experience", r"several years", r"extensive experience",
    r"führungserfahrung", r"personalverantwortung", r"leadership experience",
]


# Woran ein allgemein ausgeschriebenes Trainee-Programm als passend fürs Fach erkannt wird,
# falls in der Config keine eigenen trainee_stichwoerter stehen
TRAINEE_STICHWOERTER = [
    "data", "datenanalyse", "datenanalyst", "analytics", "analytik", "statistik", "statistisch",
    "sozialforschung", "marktforschung", "business intelligence", "datenbasiert", "datengetrieben",
    "data science", "machine learning", "python", "sql", "quantitativ", "research",
]


def passt_fach_text(text: str, cfg: dict) -> bool:
    woerter = cfg.get("trainee_stichwoerter") or TRAINEE_STICHWOERTER
    t = _norm(text)
    return any(re.search(rf"(?<![a-zäöüß]){re.escape(_norm(w))}", t) for w in woerter)


# Arbeitgeber-Typ für den Filter "Arbeitgeber"
FORSCHUNG = [
    r"institut", r"leibniz", r"max[- ]planck", r"helmholtz", r"fraunhofer", r"universit",
    r"hochschule", r"\bfh\b", r"forschung", r"wissenschaftszentrum", r"\bwzb\b", r"\bdiw\b",
    r"\bzew\b", r"\bifo\b", r"\biab\b", r"gesis", r"\bdzhw\b", r"\bgiga\b", r"\bhwwi\b",
    r"\bdeutsches zentrum\b", r"akademie der wissenschaften", r"research", r"charit[ée]",
    r"\brki\b", r"robert koch", r"\bbibb\b", r"\bdji\b", r"thünen", r"\bbaua\b",
]
OEFFENTLICH = [
    r"\bbundes", r"\bbund\b", r"ministerium", r"senatsverwaltung", r"\bsenat\b", r"behörde",
    r"statistisches", r"amt für statistik", r"statistikamt", r"destatis", r"landesamt", r"bezirksamt",
    r"freie und hansestadt", r"\bland berlin\b", r"rentenversicherung", r"bundesagentur", r"bundesbank",
    r"\bbamf\b", r"\bbbsr\b", r"\bbsi\b", r"\bbva\b", r"stadtverwaltung", r"landeshauptstadt",
    r"\bamt\b", r"\bämter\b", r"kommunal", r"öffentlich-rechtlich", r"körperschaft des öffentlichen",
    r"anstalt des öffentlichen",
]


def arbeitgeber_typ(job: dict) -> list[str]:
    name = _norm(job.get("arbeitgeber", ""))
    typen = []
    if any(re.search(m, name) for m in FORSCHUNG) or job.get("typ") == "phd":
        typen.append("forschung")
    if any(re.search(m, name) for m in OEFFENTLICH):
        typen.append("oeffentlich")
    if not typen and job.get("quelle") == "service.bund.de":
        typen.append("oeffentlich")
    return typen or ["unternehmen"]


# service.bund.de durchsucht den ganzen Anzeigentext. Damit nicht jede Stelle, in der irgendwo
# "Statistik" vorkommt, durchrutscht, muss der Titel einen Fachbezug haben.
BUND_TITEL_STICHWOERTER = [
    "daten", "data", "statistik", "statistisch", "analys", "analyt", "empiri", "evaluation",
    "monitoring", "befragung", "survey", "sozialwissenschaft", "sozialforschung", "sozialstatistik",
    "sozialberichterstattung", "sozialpolitik", "soziolog", "politikwissenschaft", "politikberatung",
    "demogra", "ökonometr", "quantitativ", "methoden", "bildungsforschung", "arbeitsmarkt",
    "wirtschaftsforschung", "research", "wirkung", "indikator", "berichterstattung",
]


def passt_bund_titel(titel: str, cfg: dict) -> bool:
    woerter = (cfg.get("bund") or {}).get("titel_stichwoerter") or BUND_TITEL_STICHWOERTER
    t = _norm(titel)
    return any(_norm(w) in t for w in woerter)


def ist_einstieg(beschreibung: str) -> bool:
    text = _norm(beschreibung)
    if not text:
        return False
    ja = any(re.search(m, text) for m in EINSTIEG_JA)
    nein = any(re.search(m, text) for m in EINSTIEG_NEIN)
    return ja and not nein


def heute_iso() -> str:
    return datetime.now(timezone.utc).astimezone(ZEITZONE).date().isoformat()


def lade_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def kategorisiere(job: sources.Rohjob, einstieg_ids: set[str] = frozenset()) -> str | None:
    if job.extra.get("trainee_suche"):
        # Aus der allgemeinen Trainee-Suche nur echte Trainee-Programme übernehmen
        titel = _norm(job.titel)
        return "trainee" if any(re.search(m, titel) for m in dict(KATEGORIEN)["trainee"]) else None
    titel = _norm(job.titel)
    for typ, muster in KATEGORIEN:
        if any(re.search(m, titel) for m in muster):
            return typ
    # Praktikums-Suche der Arbeitsagentur liefert auch Titel ohne das Wort "Praktikum"
    if job.extra.get("angebotsart") == 34:
        return "praktikum"
    if job.extra.get("feste_kategorie"):
        return job.typ_hinweis
    if job.id in einstieg_ids:
        return "einstieg"
    if job.extra.get("bund"):
        # Öffentlicher Dienst nennt selten ein Level im Titel ("Referent:in Statistik").
        # Die Suche lief über deine Fachbegriffe, daher als eigene Kategorie zeigen.
        return "einstieg" if ist_einstieg(job.extra.get("text", "")) else "weitere"
    return None


def ist_ausgeschlossen(titel: str, ausschluss: list[str]) -> bool:
    t = _norm(titel)
    for w in ausschluss:
        w = w.lower()
        if w in ("leitung", "leiter", "leiterin"):
            # Auch Zusammensetzungen wie "Referatsleitung" oder "Teamleiter",
            # aber nicht "Begleitung" (z. B. "Wissenschaftliche Begleitung")
            if re.search(r"(?<!beg)leit(ung|er|erin)(?![a-zäöüß])", t):
                return True
        elif re.search(rf"(?<![a-zäöüß]){re.escape(w)}(?![a-zäöüß])", t):
            return True
    return False


def passt_zum_fach(job: sources.Rohjob, begriffe: list[str]) -> bool:
    text = _norm(job.titel + " " + job.extra.get("text", ""))
    return any(_norm(b) in text for b in begriffe)


def arbeitszeit_bestimmen(job: sources.Rohjob, typ: str) -> list[str]:
    """Angabe der Quelle, ergänzt um Hinweise aus dem Titel."""
    zeiten = set(job.arbeitszeit)
    titel = _norm(job.titel)
    if re.search(r"teilzeit|part[- ]time|\b\d{1,2}\s?(std|stunden|h)\b", titel):
        zeiten.add("teilzeit")
    if re.search(r"vollzeit|full[- ]time", titel):
        zeiten.add("vollzeit")
    if typ == "werkstudent" and not zeiten:
        zeiten.add("teilzeit")
    if typ == "trainee" and not zeiten:
        zeiten.add("vollzeit")  # Trainee-Programme sind praktisch immer Vollzeit
    return sorted(zeiten)


def filtern(rohjobs: list[sources.Rohjob], cfg: dict, einstieg_ids: set[str] = frozenset(),
            trainee_ids: set[str] = frozenset()) -> dict[str, dict]:
    aktiv = {k for k, v in (cfg.get("jobarten") or {}).items() if v}
    if sources.trainee_aktiv(cfg):
        aktiv.add("trainee")
    if (cfg.get("bund") or {}).get("aktiv", True):
        aktiv.add("weitere")
    ausschluss = cfg.get("ausschliessen") or []
    fachbegriffe = (cfg.get("fach_stichwoerter") or
                    (cfg.get("suchbegriffe_de") or []) + (cfg.get("suchbegriffe_en") or []))
    ergebnis: dict[str, dict] = {}
    id_fuer_schluessel: dict[tuple, str] = {}

    for j in rohjobs:
        if not j.titel or not j.url:
            continue
        if ist_ausgeschlossen(j.titel, ausschluss):
            continue
        if j.extra.get("nur_mit_suchbegriff") and not passt_zum_fach(j, fachbegriffe):
            continue
        if j.extra.get("bund") and not passt_bund_titel(j.titel, cfg):
            continue
        if j.extra.get("frist") and j.extra["frist"] < heute_iso():
            continue  # Bewerbungsfrist abgelaufen
        typ = kategorisiere(j, einstieg_ids)
        if typ not in aktiv:
            continue
        # Allgemeine Trainee-Programme nur, wenn Titel oder Beschreibung zum Fach passen
        if j.extra.get("trainee_suche") and not passt_fach_text(j.titel, cfg) and j.id not in trainee_ids:
            continue
        zeiten = arbeitszeit_bestimmen(j, typ)

        # Dieselbe Stelle kann über mehrere Suchen kommen (Vollzeit- und Teilzeitsuche,
        # Stadt und Remote) oder mehrfach eingestellt sein: dann Angaben zusammenführen.
        schluessel = (_norm(j.titel), _norm(j.arbeitgeber), _norm(j.ort))
        vorhanden = ergebnis.get(j.id) or ergebnis.get(id_fuer_schluessel.get(schluessel, ""))
        if vorhanden:
            vorhanden["arbeitszeit"] = sorted(set(vorhanden["arbeitszeit"]) | set(zeiten))
            vorhanden["remote"] = vorhanden["remote"] or j.remote
            if j.suchort and j.suchort not in vorhanden["regionen"]:
                vorhanden["regionen"].append(j.suchort)
            continue
        id_fuer_schluessel[schluessel] = j.id
        ergebnis[j.id] = {
            "id": j.id, "titel": j.titel, "arbeitgeber": j.arbeitgeber, "ort": j.ort,
            "url": j.url, "quelle": j.quelle, "typ": typ, "remote": j.remote, "land": j.land,
            "regionen": [j.suchort] if j.suchort else [],
            "arbeitszeit": zeiten,
            "veroeffentlicht": j.veroeffentlicht,
            "frist": j.extra.get("frist"),
        }
    return ergebnis


GEPRUEFT = ROOT / "data" / "geprueft.json"


def beschreibungen_pruefen(rohjobs: list[sources.Rohjob], cfg: dict, heute: str) -> tuple[set[str], set[str]]:
    """Liest Stellenbeschreibungen, wo der Titel nicht reicht, und merkt sich das Ergebnis,
    damit jede Stelle nur einmal abgerufen wird.
    Ergebnis: (Stellen, die laut Beschreibung für Einsteiger passen,
               allgemeine Trainee-Programme, die laut Beschreibung zum Fach passen)"""
    arten = cfg.get("jobarten") or {}
    ausschluss = cfg.get("ausschliessen") or []
    cache: dict[str, dict] = {}
    if GEPRUEFT.exists():
        cache = json.loads(GEPRUEFT.read_text(encoding="utf-8"))
    grenze = (datetime.fromisoformat(heute) - timedelta(days=60)).date().isoformat()
    cache = {k: v for k, v in cache.items() if v.get("datum", heute) >= grenze}

    einstieg_kand: dict[str, sources.Rohjob] = {}
    trainee_kand: dict[str, sources.Rohjob] = {}
    for j in rohjobs:
        if ist_ausgeschlossen(j.titel, ausschluss):
            continue
        if arten.get("einstieg") and j.extra.get("beschreibung_pruefen") and kategorisiere(j) is None:
            einstieg_kand.setdefault(j.id, j)
        if (j.extra.get("trainee_suche") and kategorisiere(j) == "trainee"
                and not passt_fach_text(j.titel, cfg)):
            trainee_kand.setdefault(j.id, j)

    # Abzurufen: alles, wofür das benötigte Ergebnis noch nicht im Cache steht
    offen = {jid: j for jid, j in einstieg_kand.items() if "einstieg" not in cache.get(jid, {})}
    offen.update({jid: j for jid, j in trainee_kand.items() if "fach" not in cache.get(jid, {})})
    # Trainee-Programme zuerst (wenige, aber wertvoll), dann die neuesten Stellen
    reihenfolge = sorted(offen.values(), key=lambda j: (j.id in trainee_kand, j.veroeffentlicht or ""), reverse=True)
    budget = int((cfg.get("deutschland") or {}).get("max_beschreibungen_pro_lauf", 300))
    texte = sources.hole_ba_beschreibungen([j.extra["refnr"] for j in reihenfolge], budget)
    for j in reihenfolge:
        if j.extra["refnr"] in texte:
            text = texte[j.extra["refnr"]] or ""
            cache[j.id] = {"einstieg": bool(text) and ist_einstieg(text),
                           "fach": bool(text) and passt_fach_text(text, cfg),
                           "datum": heute}

    GEPRUEFT.parent.mkdir(parents=True, exist_ok=True)
    GEPRUEFT.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    einstieg = {jid for jid in einstieg_kand if cache.get(jid, {}).get("einstieg")}
    trainee = {jid for jid in trainee_kand if cache.get(jid, {}).get("fach")}
    log.info("Einstieg geprüft: %d Kandidaten, %d passend, %d noch offen",
             len(einstieg_kand), len(einstieg), sum(1 for k in einstieg_kand if "einstieg" not in cache.get(k, {})))
    log.info("Trainee-Programme geprüft: %d allgemeine, %d passend zum Fach, %d noch offen",
             len(trainee_kand), len(trainee), sum(1 for k in trainee_kand if "fach" not in cache.get(k, {})))
    return einstieg, trainee


def zusammenfuehren(alt: dict[str, dict], neu: dict[str, dict],
                    ok_quellen: set[str], cfg: dict, heute: str) -> dict[str, dict]:
    grenze = (datetime.fromisoformat(heute) - timedelta(days=int(cfg.get("entfernen_nach_tagen", 3)))).date().isoformat()
    bestand: dict[str, dict] = {}
    for jid, job in alt.items():
        if jid in neu:
            continue
        # Nur Stellen aus Quellen entfernen, die heute erfolgreich abgefragt wurden.
        # So verschwindet bei einem Ausfall nicht plötzlich alles.
        if job["quelle"] in ok_quellen and job.get("zuletzt_gesehen", heute) < grenze:
            continue
        if job.get("frist") and job["frist"] < heute:
            continue
        bestand[jid] = job
    for jid, job in neu.items():
        vorher = alt.get(jid, {})
        job["zuerst_gesehen"] = vorher.get("zuerst_gesehen", heute)
        job["zuletzt_gesehen"] = heute
        bestand[jid] = job
    return bestand


def baue_seite(jobs: dict[str, dict], cfg: dict, status: dict, jetzt: datetime) -> None:
    payload = {
        "studiengang": cfg.get("studiengang", ""),
        "aktualisiert": jetzt.isoformat(timespec="minutes"),
        "heute": jetzt.date().isoformat(),
        "quellen": status,
        "jobs": sorted(jobs.values(), key=lambda j: (j["zuerst_gesehen"], j.get("veroeffentlicht") or ""), reverse=True),
    }
    daten = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    html = VORLAGE.read_text(encoding="utf-8")
    html = html.replace("__STUDIENGANG__", _html_escape(cfg.get("studiengang", "")))
    html = html.replace("/*__DATEN__*/null", daten)
    SEITE.parent.mkdir(parents=True, exist_ok=True)
    SEITE.write_text(html, encoding="utf-8")
    (SEITE.parent / "jobs.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = lade_config()
    jetzt = datetime.now(timezone.utc).astimezone(ZEITZONE)
    heute = jetzt.date().isoformat()

    rohjobs: list[sources.Rohjob] = []
    status: dict[str, str] = {}
    ok_quellen: set[str] = set()

    def abrufen(name, funktion, *args):
        nonlocal rohjobs
        try:
            rohjobs += funktion(*args)
            status[name] = "ok"
            ok_quellen.add(name)
        except sources.TeilErfolg as t:
            log.error("%s nur teilweise abgerufen: %s", name, t)
            rohjobs += t.jobs
            status[name] = "teilweise"
        except Exception as e:
            log.error("%s fehlgeschlagen: %s", name, e)
            status[name] = "fehler"

    if (cfg.get("deutschland") or {}).get("aktiv", True):
        abrufen("Arbeitsagentur", sources.hole_arbeitsagentur, cfg)

    if (cfg.get("bund") or {}).get("aktiv", True):
        abrufen("service.bund.de", sources.hole_bund, cfg)

    if (cfg.get("uk") or {}).get("aktiv"):
        app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
        if app_id and app_key:
            abrufen("Adzuna", sources.hole_adzuna, cfg, app_id, app_key)
        else:
            log.warning("UK-Suche aktiv, aber ADZUNA_APP_ID/ADZUNA_APP_KEY fehlen – Adzuna übersprungen")

    for feed in cfg.get("rss_feeds") or []:
        if feed.get("url"):
            abrufen(feed["name"], sources.hole_rss, feed)

    einstieg_ids, trainee_ids = beschreibungen_pruefen(rohjobs, cfg, heute)
    neu = filtern(rohjobs, cfg, einstieg_ids, trainee_ids)
    alt = {}
    if DATEN.exists():
        alt = {j["id"]: j for j in json.loads(DATEN.read_text(encoding="utf-8"))}
    bestand = zusammenfuehren(alt, neu, ok_quellen, cfg, heute)

    for job in bestand.values():
        job["arbeitgeber_typ"] = arbeitgeber_typ(job)

    DATEN.parent.mkdir(parents=True, exist_ok=True)
    DATEN.write_text(json.dumps(list(bestand.values()), ensure_ascii=False, indent=1), encoding="utf-8")
    baue_seite(bestand, cfg, status, jetzt)

    heute_neu = sum(1 for j in bestand.values() if j["zuerst_gesehen"] == heute and j["id"] not in alt)
    log.info("Fertig: %d Stellen insgesamt, %d neu", len(bestand), heute_neu)
    # Fehlercode nur, wenn wirklich gar nichts geklappt hat
    return 0 if ok_quellen else 1


if __name__ == "__main__":
    sys.exit(main())
