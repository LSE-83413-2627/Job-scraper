"""Datenquellen: Jobbörse der Bundesagentur für Arbeit (offene API) und RSS-Feeds."""

from __future__ import annotations

import base64
import html
import logging
import re
import time
import urllib.robotparser
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import feedparser
import requests

log = logging.getLogger(__name__)

BA_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v6/jobs"  # v4 seit Herbst 2026 abgeschaltet (403)
BA_DETAIL_PAGE = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"
BA_HEADERS = {
    "X-API-Key": "jobboerse-jobsuche",
    "Accept": "application/json",
    "User-Agent": "studijobs/1.0 (privater Job-Digest fuer Studierende)",
}
PAUSE_SEKUNDEN = 0.6  # höflich bleiben: kurze Pause zwischen Anfragen
MAX_FEHLER_IN_FOLGE = 5  # danach wird die Quelle für diesen Lauf aufgegeben
BA_SEITENGROESSE = 100
# v6 akzeptiert nur diese Werte für "veroeffentlichtseit", andere werden ignoriert
BA_ERLAUBTE_TAGE = (0, 1, 7, 14, 28)


@dataclass
class Rohjob:
    id: str
    titel: str
    arbeitgeber: str
    ort: str
    url: str
    quelle: str
    veroeffentlicht: str | None = None
    typ_hinweis: str | None = None  # Kategorie, aus der die Suche stammt
    remote: bool = False
    land: str = ""
    suchort: str = ""                                    # Region, in der gesucht wurde
    arbeitszeit: list = field(default_factory=list)      # "vollzeit" / "teilzeit"
    extra: dict = field(default_factory=dict)


class QuelleFehlgeschlagen(Exception):
    pass


class EndgueltigerFehler(Exception):
    pass


class TeilErfolg(Exception):
    """Quelle hat nur einen Teil geliefert: Treffer behalten, aber alte Stellen nicht entfernen."""
    def __init__(self, jobs, grund):
        super().__init__(grund)
        self.jobs = jobs


def _get_mit_retry(session: requests.Session, url: str, params: dict, versuche: int = 3):
    for versuch in range(1, versuche + 1):
        try:
            r = session.get(url, params=params, headers=BA_HEADERS, timeout=30)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}")
            if 400 <= r.status_code < 500:
                # Client-Fehler (z. B. 403) werden durch Wiederholen nicht besser
                raise EndgueltigerFehler(f"HTTP {r.status_code}: {r.text[:200]}")
            return r.json()
        except (requests.RequestException, ValueError) as e:
            if versuch == versuche:
                raise
            warte = 2 ** versuch
            log.warning("Anfrage fehlgeschlagen (%s), neuer Versuch in %ss", e, warte)
            time.sleep(warte)


def _ba_suchen(cfg: dict) -> list[dict]:
    """Baut aus der Konfiguration die einzelnen Suchanfragen."""
    arten = cfg.get("jobarten", {})
    vorlagen = []
    if arten.get("werkstudent"):
        vorlagen += [("werkstudent", "Werkstudent {b}", 1)]
    if arten.get("praktikum"):
        vorlagen += [("praktikum", "{b}", 34)]  # 34 = Praktikum/Trainee
    if arten.get("einstieg"):
        # Breite Suche: Die meisten Einstiegsstellen heißen schlicht "Data Analyst (m/w/d)".
        # Ob sie für Berufseinsteiger sind, steht erst in der Beschreibung (siehe main.py).
        vorlagen += [("einstieg", "{b}", 1)]
    if arten.get("phd"):
        vorlagen += [
            ("phd", "Doktorand {b}", 1),
            ("phd", "Wissenschaftlicher Mitarbeiter {b}", 1),
        ]

    de = cfg.get("deutschland") or {}
    orte = [(o["name"], o.get("umkreis", 25)) for o in (de.get("orte") or [])]
    if not orte:
        orte = [(None, None)]

    # Getrennte Abfragen für Vollzeit und Teilzeit, damit sich die Stellen später
    # danach filtern lassen. Werkstudentenjobs sind praktisch immer Teilzeit.
    trennen = de.get("arbeitszeit_unterscheiden", True)

    suchen = []
    for begriff in cfg.get("suchbegriffe_de", []):
        for typ, muster, angebotsart in vorlagen:
            was = muster.format(b=begriff)
            if typ == "werkstudent" or not trennen:
                zeiten = [None]
            else:
                zeiten = ["vz", "tz"]
            for ort, umkreis in orte:
                for az in zeiten:
                    suchen.append(dict(typ=typ, was=was, angebotsart=angebotsart, wo=ort,
                                       umkreis=umkreis, remote=False, arbeitszeit=az,
                                       suchort=ort or "Deutschland"))
            if de.get("remote_einbeziehen") and orte != [(None, None)]:
                suchen.append(dict(typ=typ, was=was, angebotsart=angebotsart, wo=None,
                                   umkreis=None, remote=True, arbeitszeit=None, suchort="Remote"))

    # Eigene Trainee-Suche: alle Trainee-Programme vor Ort, unabhängig vom Fachbegriff.
    # Welche davon zu deinem Fach passen, wird danach anhand der Beschreibung geprüft.
    if trainee_aktiv(cfg) and de.get("trainee_suche", True):
        for was in ("Trainee", "Traineeprogramm"):
            for angebotsart in (34, 1):
                for ort, umkreis in orte:
                    suchen.append(dict(typ="trainee", was=was, angebotsart=angebotsart, wo=ort,
                                       umkreis=umkreis, remote=False, arbeitszeit=None,
                                       suchort=ort or "Deutschland", trainee_suche=True,
                                       max_seiten=int(de.get("trainee_max_seiten", 3))))
    return suchen


def trainee_aktiv(cfg: dict) -> bool:
    # Ältere Configs kennen "trainee" noch nicht: dann standardmäßig an
    return (cfg.get("jobarten") or {}).get("trainee", True)


def _ort_text(job: dict) -> str:
    """v6: stellenlokationen[].adresse; v4: arbeitsort (alte Form wird weiter verstanden)."""
    lokationen = job.get("stellenlokationen") or []
    adresse = (lokationen[0].get("adresse") if lokationen else None) or job.get("arbeitsort") or {}
    ort = (adresse.get("ort") or "").strip()
    region = (adresse.get("region") or "").strip()
    if region.isupper():
        region = region.title()
    if ort.isupper():
        ort = ort.title()
    text = ort or region or (adresse.get("land") or "").title()
    if ort and region and region.lower() not in ort.lower():
        text = f"{ort} ({region})"
    if len(lokationen) > 1:
        text += f" und {len(lokationen) - 1} weitere"
    return text


def _ba_tage(tage: int) -> int:
    """Nächsten von v6 akzeptierten Wert wählen (z. B. 30 → 28)."""
    return min(BA_ERLAUBTE_TAGE, key=lambda t: abs(t - tage))


def hole_arbeitsagentur(cfg: dict) -> list[Rohjob]:
    session = requests.Session()
    tage = _ba_tage(int(cfg.get("veroeffentlicht_seit_tagen", 28)))
    max_seiten = int(cfg.get("max_seiten_pro_suche", 2))
    jobs: list[Rohjob] = []
    erfolgreiche = fehler_in_folge = 0
    letzter_fehler = ""
    suchen = _ba_suchen(cfg)

    for s in suchen:
        if fehler_in_folge >= MAX_FEHLER_IN_FOLGE:
            break
        for seite in range(1, s.get("max_seiten", max_seiten) + 1):
            params = {
                "was": s["was"],
                "angebotsart": s["angebotsart"],
                "veroeffentlichtseit": tage,
                "page": seite,
                "size": BA_SEITENGROESSE,
                "pav": "false",  # keine privaten Arbeitsvermittler
            }
            if s["wo"]:
                params["wo"] = s["wo"]
                params["umkreis"] = s["umkreis"]
            if s["arbeitszeit"]:
                params["arbeitszeit"] = s["arbeitszeit"]
            try:
                daten = _get_mit_retry(session, BA_URL, params)
            except Exception as e:  # einzelne Suche darf scheitern
                fehler_in_folge += 1
                letzter_fehler = str(e)
                log.warning("BA-Suche '%s' (%s) fehlgeschlagen: %s", s["was"], s["suchort"], e)
                break
            erfolgreiche += 1
            fehler_in_folge = 0
            treffer = daten.get("ergebnisliste") or daten.get("stellenangebote") or []
            for a in treffer:
                refnr = a.get("referenznummer") or a.get("refnr")
                if not refnr:
                    continue
                homeoffice = bool(a.get("homeofficemoeglich"))
                # Remote-Suche ist deutschlandweit; nur Stellen mit Homeoffice übernehmen
                if s["remote"] and not homeoffice:
                    continue
                zeitraum = a.get("veroeffentlichungszeitraum") or {}
                jobs.append(Rohjob(
                    id=f"ba:{refnr}",
                    titel=(a.get("stellenangebotsTitel") or a.get("titel")
                           or a.get("hauptberuf") or a.get("beruf") or "").strip(),
                    arbeitgeber=(a.get("firma") or a.get("arbeitgeber") or "").strip(),
                    ort=_ort_text(a),
                    url=BA_DETAIL_PAGE.format(refnr=refnr),
                    quelle="Arbeitsagentur",
                    veroeffentlicht=(zeitraum.get("von") or a.get("aktuelleVeroeffentlichungsdatum") or "")[:10] or None,
                    typ_hinweis=s["typ"],
                    remote=homeoffice,
                    land="Deutschland",
                    suchort=s["suchort"],
                    arbeitszeit={"vz": ["vollzeit"], "tz": ["teilzeit"]}.get(s["arbeitszeit"], []),
                    extra={"angebotsart": s["angebotsart"],
                           "externeUrl": a.get("externeURL") or a.get("externeUrl"),
                           "refnr": refnr,
                           # Stellen aus der breiten Suche ohne Hinweis im Titel: Beschreibung prüfen
                           "beschreibung_pruefen": s["typ"] == "einstieg",
                           "trainee_suche": s.get("trainee_suche", False)},
                ))
            time.sleep(PAUSE_SEKUNDEN)
            gesamt = int(daten.get("maxErgebnisse") or 0)
            if seite * BA_SEITENGROESSE >= gesamt or not treffer:
                break

    log.info("Arbeitsagentur: %d Suchen, %d erfolgreich, %d Rohtreffer", len(suchen), erfolgreiche, len(jobs))
    if fehler_in_folge >= MAX_FEHLER_IN_FOLGE and erfolgreiche == 0:
        raise QuelleFehlgeschlagen(f"Abbruch nach {MAX_FEHLER_IN_FOLGE} Fehlern in Folge, zuletzt: {letzter_fehler}")
    if fehler_in_folge >= MAX_FEHLER_IN_FOLGE:
        raise TeilErfolg(jobs, f"Abbruch nach {MAX_FEHLER_IN_FOLGE} Fehlern in Folge, zuletzt: {letzter_fehler}")
    if suchen and erfolgreiche == 0:
        raise QuelleFehlgeschlagen("Keine einzige Anfrage an die Arbeitsagentur war erfolgreich")
    return jobs


BA_DETAIL_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobdetails/{code}"


def hole_ba_beschreibungen(refnrs: list[str], budget: int) -> dict[str, str | None]:
    """Lädt Stellenbeschreibungen. Ergebnis: refnr → Text, oder None wenn die Stelle weg ist (404).
    Fehlgeschlagene Abrufe fehlen im Ergebnis und werden beim nächsten Lauf erneut versucht."""
    session = requests.Session()
    ergebnis: dict[str, str | None] = {}
    fehler_in_folge = 0
    for refnr in refnrs[:budget]:
        if fehler_in_folge >= MAX_FEHLER_IN_FOLGE:
            log.error("Stellenbeschreibungen: Abbruch nach %d Fehlern in Folge", MAX_FEHLER_IN_FOLGE)
            break
        code = base64.b64encode(refnr.encode()).decode()
        try:
            r = session.get(BA_DETAIL_URL.format(code=code), headers=BA_HEADERS, timeout=30)
            if r.status_code == 404:
                ergebnis[refnr] = None
                fehler_in_folge = 0
                continue
            r.raise_for_status()
            d = r.json()
            ergebnis[refnr] = " ".join(str(d.get(k) or "") for k in
                                       ("stellenangebotsTitel", "stellenangebotsBeschreibung",
                                        "stellenbeschreibung", "beschreibung"))
            fehler_in_folge = 0
        except Exception as e:
            fehler_in_folge += 1
            log.warning("Beschreibung %s nicht abrufbar: %s", refnr, e)
        time.sleep(PAUSE_SEKUNDEN / 2)
    log.info("Stellenbeschreibungen: %d von %d abgerufen", len(ergebnis), min(len(refnrs), budget))
    return ergebnis


# ── service.bund.de: Stellen von Bund, Ländern, Kommunen und vielen Forschungseinrichtungen ──
BUND_URL = "https://www.service.bund.de/Content/DE/Stellen/Suche/Formular.html"
BUND_PLZ = {"berlin": "10117 Berlin", "hamburg": "20095 Hamburg", "münchen": "80331 München",
            "köln": "50667 Köln", "frankfurt": "60311 Frankfurt am Main", "bonn": "53111 Bonn",
            "leipzig": "04109 Leipzig", "potsdam": "14467 Potsdam"}


def _bund_text(beschreibung: str) -> str:
    """'Arbeitgeber: <strong>X</strong><br /> Ort: <strong>10117 Berlin</strong>' → Klartext, eine Angabe pro Zeile."""
    t = re.sub(r"<br\s*/?>", "\n", beschreibung or "", flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t)


def _bund_feld(text: str, name: str) -> str:
    m = re.search(rf"{name}\s*:\s*([^\n]+)", text or "", flags=re.I)
    return m.group(1).strip() if m else ""


def _bund_datum(text: str) -> str | None:
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text or "")
    return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else None


def hole_bund(cfg: dict) -> list[Rohjob]:
    bund = cfg.get("bund") or {}
    de = cfg.get("deutschland") or {}
    orte = bund.get("orte") or de.get("orte") or [{"name": None}]
    begriffe = list(dict.fromkeys((cfg.get("suchbegriffe_de") or []) + (bund.get("zusatzbegriffe") or [])))
    session = requests.Session()
    jobs: list[Rohjob] = []
    erfolgreiche = fehler_in_folge = 0
    letzter_fehler = ""

    for begriff in begriffe:
        for ort in orte:
            if fehler_in_folge >= MAX_FEHLER_IN_FOLGE:
                break
            params = {"nn": "4641956", "type": "0", "resultsPerPage": "100", "jobsrss": "true",
                      "templateQueryString": begriff, "sortOrder": "dateOfIssue_dt desc"}
            name = ort.get("name")
            if name:
                params["city_zipcode"] = ort.get("plz_ort") or BUND_PLZ.get(name.lower(), name)
            try:
                r = session.get(BUND_URL, params=params, timeout=30,
                                headers={"User-Agent": BA_HEADERS["User-Agent"]})
                r.raise_for_status()
                feed = feedparser.parse(r.content)
                if feed.bozo and not feed.entries and b"<rss" not in r.content[:500]:
                    raise QuelleFehlgeschlagen("Antwort ist kein RSS-Feed")
            except Exception as e:
                fehler_in_folge += 1
                letzter_fehler = str(e)
                log.warning("service.bund.de '%s' (%s) fehlgeschlagen: %s", begriff, name, e)
                continue
            erfolgreiche += 1
            fehler_in_folge = 0
            for e in feed.entries:
                link = (e.get("link") or "").split("#")[0].split(";")[0].split("?")[0]
                if not link:
                    continue
                nummer = re.search(r"/(\d+)\.html", link)
                text = _bund_text(e.get("summary") or e.get("description") or "")
                datum = time.strftime("%Y-%m-%d", e.published_parsed) if e.get("published_parsed") else None
                az_text = _bund_feld(text, "Arbeitszeit").lower()
                zeiten = [z for z, w in (("vollzeit", "vollzeit"), ("teilzeit", "teilzeit")) if w in az_text]
                jobs.append(Rohjob(
                    id=f"bund:{nummer.group(1) if nummer else link}",
                    titel=re.sub(r"\s+", " ", html.unescape(e.get("title") or "")).strip(),
                    arbeitgeber=_bund_feld(text, "Arbeitgeber") or (e.get("author") or ""),
                    ort=re.sub(r"^\d{5}\s*", "", _bund_feld(text, "Ort") or _bund_feld(text, "Dienstort") or (name or "")),
                    url=link,
                    quelle="service.bund.de",
                    veroeffentlicht=datum,
                    land="Deutschland",
                    suchort=name or "Deutschland",
                    arbeitszeit=zeiten,
                    extra={"text": text[:3000], "bund": True,
                           "frist": _bund_datum(_bund_feld(text, "Bewerbungsfrist"))},
                ))
            time.sleep(PAUSE_SEKUNDEN * 1.5)

    log.info("service.bund.de: %d Abfragen erfolgreich, %d Rohtreffer", erfolgreiche, len(jobs))
    if erfolgreiche == 0:
        raise QuelleFehlgeschlagen(f"Keine Abfrage erfolgreich, zuletzt: {letzter_fehler}")
    if fehler_in_folge >= MAX_FEHLER_IN_FOLGE:
        raise TeilErfolg(jobs, f"Abbruch nach {MAX_FEHLER_IN_FOLGE} Fehlern in Folge: {letzter_fehler}")
    return jobs


# ── Forschungseinrichtungen ──────────────────────────────────────────────
LEIBNIZ_URL = "https://www.leibniz-gemeinschaft.de/karriere/stellenportal"
MPG_RSS = "https://www.mpg.de/feeds/jobs.rss"
_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}


def darf_abrufen(url: str) -> bool:
    """Respektiert die robots.txt der jeweiligen Seite."""
    teile = urlparse(url)
    basis = f"{teile.scheme}://{teile.netloc}"
    if basis not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        try:
            r = requests.get(basis + "/robots.txt", timeout=15,
                             headers={"User-Agent": BA_HEADERS["User-Agent"]})
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            _robots_cache[basis] = rp
        except Exception:
            _robots_cache[basis] = None  # nicht erreichbar: wie "keine Einschränkung"
    rp = _robots_cache[basis]
    return True if rp is None else rp.can_fetch(BA_HEADERS["User-Agent"], url)


def _klartext(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


def _region_aus_text(text: str, cfg: dict) -> str:
    orte = [o["name"] for o in ((cfg.get("deutschland") or {}).get("orte") or []) if o.get("name")]
    t = (text or "").lower()
    for ort in orte:
        if ort.lower() in t:
            return ort
    return ""


def hole_leibniz(cfg: dict) -> list[Rohjob]:
    if not darf_abrufen(LEIBNIZ_URL):
        raise QuelleFehlgeschlagen("robots.txt erlaubt den Abruf nicht")
    r = requests.get(LEIBNIZ_URL, timeout=30, headers={"User-Agent": BA_HEADERS["User-Agent"]})
    r.raise_for_status()
    seite = r.text
    # Jede Stelle: <a href=".../detail/job/show/Job/<slug>">Titel</a>, danach der Institutsname
    treffer = list(re.finditer(r'<a[^>]+href="([^"]*/detail/job/show/Job/[^"]+)"[^>]*>(.*?)</a>', seite, flags=re.S))
    jobs = []
    for i, m in enumerate(treffer):
        ende = treffer[i + 1].start() if i + 1 < len(treffer) else m.end() + 2000
        rest = seite[m.end():ende]
        rest = rest.split("</li>")[0]
        institut = _klartext(rest)
        titel = _klartext(m.group(2))
        if not titel:
            continue
        url = urljoin(LEIBNIZ_URL, html.unescape(m.group(1)))
        slug = url.rstrip("/").rsplit("/", 1)[-1]
        ort = institut.rsplit(",", 1)[1].strip() if "," in institut else ""
        jobs.append(Rohjob(
            id=f"leibniz:{slug}", titel=titel, arbeitgeber=institut, ort=ort, url=url,
            quelle="Leibniz-Stellenportal", land="Deutschland",
            suchort=_region_aus_text(institut + " " + titel, cfg) or "Weitere Orte",
            extra={"forschung": "leibniz", "text": institut},
        ))
    log.info("Leibniz-Stellenportal: %d Stellen", len(jobs))
    if not jobs:
        raise QuelleFehlgeschlagen("Keine Stellen gefunden – hat sich der Seitenaufbau geändert?")
    return jobs


def hole_mpg(cfg: dict) -> list[Rohjob]:
    if not darf_abrufen(MPG_RSS):
        raise QuelleFehlgeschlagen("robots.txt erlaubt den Abruf nicht")
    r = requests.get(MPG_RSS, timeout=30, headers={"User-Agent": BA_HEADERS["User-Agent"]})
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    jobs = []
    for e in feed.entries:
        link = (e.get("link") or "").split("?")[0]
        nummer = re.search(r"mpg\.de/(\d+)/", link)
        titel = _klartext(e.get("title") or "")
        text = _klartext(e.get("summary") or "")
        datum = time.strftime("%Y-%m-%d", e.published_parsed) if e.get("published_parsed") else None
        institut = re.search(r"Max Planck Institute (?:for|of) [A-Z][\w\s,&-]{3,80}?(?= in |[.,(]|$)", text)
        jobs.append(Rohjob(
            id=f"mpg:{nummer.group(1) if nummer else link}", titel=titel,
            arbeitgeber=institut.group(0).strip() if institut else "Max-Planck-Gesellschaft",
            ort="", url=link, quelle="Max-Planck-Gesellschaft", veroeffentlicht=datum, land="Deutschland",
            suchort=_region_aus_text(text, cfg) or "Weitere Orte",
            extra={"forschung": "mpg", "text": text[:3000]},
        ))
    log.info("Max-Planck-Gesellschaft: %d Stellen", len(jobs))
    return jobs


# Linktexte, die nach einer Stellenanzeige aussehen
_STELLEN_LINK = re.compile(
    r"\((?:[mwfdx]|div|all)[^)]{0,12}\)|m/w/d|w/m/d|f/m/d|m/f/d|all genders|alle geschlechter|"
    r"mitarbeiter|hilfskraft|doktorand|phd|praktik|werkstudent|trainee|referent|research|analyst|"
    r"wissenschaftlich|student", re.I)


def hole_karriereseite(seite: dict, cfg: dict) -> list[Rohjob]:
    """Allgemeiner Abruf einer Karriereseite: sammelt Links, deren Text wie eine Stelle aussieht."""
    url = seite["url"]
    if not darf_abrufen(url):
        raise QuelleFehlgeschlagen("robots.txt erlaubt den Abruf nicht")
    r = requests.get(url, timeout=30, headers={"User-Agent": BA_HEADERS["User-Agent"]})
    r.raise_for_status()
    jobs, gesehen = [], set()
    ort = seite.get("ort", "")
    region = _region_aus_text(ort, cfg) or "Weitere Orte"
    for m in re.finditer(r'<a[^>]+href="([^"#][^"]*)"[^>]*>(.*?)</a>', r.text, flags=re.S | re.I):
        titel = _klartext(m.group(2))
        if not (12 <= len(titel) <= 220) or not _STELLEN_LINK.search(titel):
            continue
        link = urljoin(url, html.unescape(m.group(1)))
        if link in gesehen or link.rstrip("/") == url.rstrip("/") or link.startswith("mailto:"):
            continue
        gesehen.add(link)
        jobs.append(Rohjob(
            id=f"seite:{seite['name']}:{base64.urlsafe_b64encode(link.encode()).decode()[:48]}",
            titel=titel, arbeitgeber=seite.get("arbeitgeber") or seite["name"], ort=ort, url=link,
            quelle=seite["name"], land="Deutschland", suchort=region,
            extra={"forschung": "seite", "text": ""},
        ))
    log.info("Karriereseite %s: %d mögliche Stellen", seite["name"], len(jobs))
    return jobs


ADZUNA_URL = "https://api.adzuna.com/v1/api/jobs/{land}/search/{seite}"
# Mindestens eines dieser Wörter muss in der Anzeige vorkommen (Titel wird danach genauer geprüft)
ADZUNA_STUFEN = "graduate junior intern internship placement trainee entry assistant studentship phd doctoral"


def _ohne_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text or "").strip()


def hole_adzuna(cfg: dict, app_id: str, app_key: str) -> list[Rohjob]:
    uk = cfg.get("uk") or {}
    land = uk.get("land", "gb")
    budget = int(uk.get("max_anfragen_pro_lauf", 25))
    tage = int(cfg.get("veroeffentlicht_seit_tagen", 30))
    max_seiten = int(cfg.get("max_seiten_pro_suche", 2))
    # Adzuna kann nur einzelne Wörter ausschließen
    ausschluss = " ".join(w.lower() for w in (cfg.get("ausschliessen") or []) if " " not in w)
    orte = uk.get("orte") or [{"name": None}]
    session = requests.Session()
    jobs: list[Rohjob] = []
    anfragen = erfolgreiche = 0
    budget_erreicht = False

    for begriff in cfg.get("suchbegriffe_en", []):
        for ort in orte:
            for seite in range(1, max_seiten + 1):
                if anfragen >= budget:
                    budget_erreicht = True
                    break
                params = {
                    "app_id": app_id, "app_key": app_key,
                    "what": begriff, "what_or": ADZUNA_STUFEN,
                    "max_days_old": tage, "results_per_page": 50, "sort_by": "date",
                    "content-type": "application/json",
                }
                if ausschluss:
                    params["what_exclude"] = ausschluss
                if ort.get("name"):
                    params["where"] = ort["name"]
                    params["distance"] = ort.get("umkreis", 25)
                anfragen += 1
                try:
                    r = session.get(ADZUNA_URL.format(land=land, seite=seite), params=params,
                                    headers={"User-Agent": BA_HEADERS["User-Agent"]}, timeout=30)
                    r.raise_for_status()
                    daten = r.json()
                except Exception as e:
                    log.warning("Adzuna-Suche '%s' (%s) fehlgeschlagen: %s", begriff, ort.get("name"), e)
                    break
                erfolgreiche += 1
                treffer = daten.get("results") or []
                for a in treffer:
                    jobs.append(Rohjob(
                        id=f"adzuna:{a.get('id')}",
                        titel=_ohne_html(a.get("title")),
                        arbeitgeber=_ohne_html((a.get("company") or {}).get("display_name")),
                        ort=_ohne_html((a.get("location") or {}).get("display_name")),
                        url=a.get("redirect_url") or "",
                        quelle="Adzuna",
                        land="UK" if land == "gb" else land.upper(),
                        suchort=ort.get("name") or ("UK" if land == "gb" else land.upper()),
                        arbeitszeit={"full_time": ["vollzeit"], "part_time": ["teilzeit"]}.get(
                            a.get("contract_time"), []),
                        veroeffentlicht=(a.get("created") or "")[:10] or None,
                        extra={"text": _ohne_html(a.get("description"))[:1000]},
                    ))
                time.sleep(PAUSE_SEKUNDEN)
                if seite * 50 >= int(daten.get("count") or 0) or not treffer:
                    break

    if budget_erreicht:
        log.warning("Adzuna: Anfrage-Budget (%d) erreicht, restliche Suchen übersprungen", budget)
    log.info("Adzuna: %d Anfragen, %d Rohtreffer", anfragen, len(jobs))
    if anfragen and erfolgreiche == 0:
        raise QuelleFehlgeschlagen("Keine Anfrage an Adzuna war erfolgreich (Schlüssel prüfen)")
    return jobs


def hole_rss(feed_cfg: dict) -> list[Rohjob]:
    r = requests.get(feed_cfg["url"], headers={"User-Agent": BA_HEADERS["User-Agent"]}, timeout=30)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    if feed.bozo and not feed.entries:
        raise QuelleFehlgeschlagen(f"Feed nicht lesbar: {feed.bozo_exception}")
    jobs = []
    for e in feed.entries:
        link = e.get("link") or ""
        key = e.get("id") or link or e.get("title", "")
        kurz = base64.urlsafe_b64encode(key.encode()).decode()[:40]
        datum = None
        if e.get("published_parsed"):
            datum = time.strftime("%Y-%m-%d", e.published_parsed)
        jobs.append(Rohjob(
            id=f"rss:{feed_cfg['name']}:{kurz}",
            titel=(e.get("title") or "").strip(),
            arbeitgeber=(e.get("author") or "").strip(),
            ort="",
            url=link,
            quelle=feed_cfg["name"],
            veroeffentlicht=datum,
            typ_hinweis=feed_cfg.get("typ"),
            land=feed_cfg.get("land", ""),
            suchort=feed_cfg.get("suchort", ""),
            extra={"text": e.get("summary", "")[:2000],
                   "nur_mit_suchbegriff": feed_cfg.get("nur_mit_suchbegriff", False),
                   "feste_kategorie": feed_cfg.get("typ") not in (None, "", "auto")},
        ))
    log.info("RSS %s: %d Einträge", feed_cfg["name"], len(jobs))
    return jobs
