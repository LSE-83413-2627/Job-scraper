"""Datenquellen: Jobbörse der Bundesagentur für Arbeit (offene API) und RSS-Feeds."""

from __future__ import annotations

import base64
import logging
import re
import time
from dataclasses import dataclass, field

import feedparser
import requests

log = logging.getLogger(__name__)

BA_URL = "https://rest.arbeitsagentur.de/jobboerse/jobsuche-service/pc/v4/jobs"
BA_DETAIL_PAGE = "https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}"
BA_HEADERS = {
    "X-API-Key": "jobboerse-jobsuche",
    "User-Agent": "studijobs/1.0 (privater Job-Digest fuer Studierende)",
}
PAUSE_SEKUNDEN = 0.6  # höflich bleiben: kurze Pause zwischen Anfragen


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


def _get_mit_retry(session: requests.Session, url: str, params: dict, versuche: int = 3):
    for versuch in range(1, versuche + 1):
        try:
            r = session.get(url, params=params, headers=BA_HEADERS, timeout=30)
            if r.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
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
        vorlagen += [
            ("einstieg", "Junior {b}", 1),
            ("einstieg", "Absolvent {b}", 1),
            ("einstieg", "Berufseinsteiger {b}", 1),
        ]
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
    return suchen


def _ort_text(arbeitsort: dict | None) -> str:
    if not arbeitsort:
        return ""
    ort = arbeitsort.get("ort") or ""
    region = arbeitsort.get("region") or ""
    if ort and region and region not in ort:
        return f"{ort} ({region})"
    return ort or region or arbeitsort.get("land", "")


def hole_arbeitsagentur(cfg: dict) -> list[Rohjob]:
    session = requests.Session()
    tage = min(int(cfg.get("veroeffentlicht_seit_tagen", 30)), 100)
    max_seiten = int(cfg.get("max_seiten_pro_suche", 2))
    jobs: list[Rohjob] = []
    erfolgreiche = 0
    suchen = _ba_suchen(cfg)

    for s in suchen:
        for seite in range(1, max_seiten + 1):
            params = {
                "was": s["was"],
                "angebotsart": s["angebotsart"],
                "veroeffentlichtseit": tage,
                "page": seite,
                "size": 50,
                "pav": "false",  # keine privaten Arbeitsvermittler
            }
            if s["wo"]:
                params["wo"] = s["wo"]
                params["umkreis"] = s["umkreis"]
            if s["remote"]:
                params["arbeitszeit"] = "ho"
            elif s["arbeitszeit"]:
                params["arbeitszeit"] = s["arbeitszeit"]
            try:
                daten = _get_mit_retry(session, BA_URL, params)
            except Exception as e:  # einzelne Suche darf scheitern
                log.warning("BA-Suche '%s' (%s) fehlgeschlagen: %s", s["was"], s["wo"] or "remote", e)
                break
            erfolgreiche += 1
            treffer = daten.get("stellenangebote") or []
            for a in treffer:
                refnr = a.get("refnr") or a.get("referenznummer")
                if not refnr:
                    continue
                jobs.append(Rohjob(
                    id=f"ba:{refnr}",
                    titel=(a.get("titel") or a.get("beruf") or "").strip(),
                    arbeitgeber=(a.get("arbeitgeber") or "").strip(),
                    ort=_ort_text(a.get("arbeitsort")),
                    url=BA_DETAIL_PAGE.format(refnr=refnr),
                    quelle="Arbeitsagentur",
                    veroeffentlicht=a.get("aktuelleVeroeffentlichungsdatum"),
                    typ_hinweis=s["typ"],
                    remote=s["remote"],
                    land="Deutschland",
                    suchort=s["suchort"],
                    arbeitszeit={"vz": ["vollzeit"], "tz": ["teilzeit"]}.get(s["arbeitszeit"], []),
                    extra={"angebotsart": s["angebotsart"], "beruf": a.get("beruf"),
                           "externeUrl": a.get("externeUrl")},
                ))
            time.sleep(PAUSE_SEKUNDEN)
            gesamt = int(daten.get("maxErgebnisse") or 0)
            if seite * 50 >= gesamt or not treffer:
                break

    log.info("Arbeitsagentur: %d Suchen, %d Rohtreffer", len(suchen), len(jobs))
    if suchen and erfolgreiche == 0:
        raise QuelleFehlgeschlagen("Keine einzige Anfrage an die Arbeitsagentur war erfolgreich")
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
