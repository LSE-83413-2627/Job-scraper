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
    ("einstieg", [r"junior", r"absolvent", r"einsteiger", r"berufsstart", r"\bgraduate",
                  r"entry[- ]level", r"trainee", r"young professional", r"direkteinstieg",
                  r"fast stream", r"associate analyst", r"analyst i\b", r"early careers?"]),
]


def lade_config() -> dict:
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def kategorisiere(job: sources.Rohjob) -> str | None:
    titel = _norm(job.titel)
    for typ, muster in KATEGORIEN:
        if any(re.search(m, titel) for m in muster):
            return typ
    # Praktikums-Suche der Arbeitsagentur liefert auch Titel ohne das Wort "Praktikum"
    if job.extra.get("angebotsart") == 34:
        return "praktikum"
    if job.extra.get("feste_kategorie"):
        return job.typ_hinweis
    return None


def ist_ausgeschlossen(titel: str, ausschluss: list[str]) -> bool:
    t = _norm(titel)
    return any(re.search(rf"(?<![a-zäöüß]){re.escape(w.lower())}(?![a-zäöüß])", t) for w in ausschluss)


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
    return sorted(zeiten)


def filtern(rohjobs: list[sources.Rohjob], cfg: dict) -> dict[str, dict]:
    aktiv = {k for k, v in (cfg.get("jobarten") or {}).items() if v}
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
        typ = kategorisiere(j)
        if typ not in aktiv:
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
        }
    return ergebnis


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
        except Exception as e:
            log.error("%s fehlgeschlagen: %s", name, e)
            status[name] = "fehler"

    if (cfg.get("deutschland") or {}).get("aktiv", True):
        abrufen("Arbeitsagentur", sources.hole_arbeitsagentur, cfg)

    if (cfg.get("uk") or {}).get("aktiv"):
        app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
        if app_id and app_key:
            abrufen("Adzuna", sources.hole_adzuna, cfg, app_id, app_key)
        else:
            log.warning("UK-Suche aktiv, aber ADZUNA_APP_ID/ADZUNA_APP_KEY fehlen – Adzuna übersprungen")

    for feed in cfg.get("rss_feeds") or []:
        if feed.get("url"):
            abrufen(feed["name"], sources.hole_rss, feed)

    neu = filtern(rohjobs, cfg)
    alt = {}
    if DATEN.exists():
        alt = {j["id"]: j for j in json.loads(DATEN.read_text(encoding="utf-8"))}
    bestand = zusammenfuehren(alt, neu, ok_quellen, cfg, heute)

    DATEN.parent.mkdir(parents=True, exist_ok=True)
    DATEN.write_text(json.dumps(list(bestand.values()), ensure_ascii=False, indent=1), encoding="utf-8")
    baue_seite(bestand, cfg, status, jetzt)

    heute_neu = sum(1 for j in bestand.values() if j["zuerst_gesehen"] == heute and j["id"] not in alt)
    log.info("Fertig: %d Stellen insgesamt, %d neu", len(bestand), heute_neu)
    # Fehlercode nur, wenn wirklich gar nichts geklappt hat
    return 0 if ok_quellen else 1


if __name__ == "__main__":
    sys.exit(main())
