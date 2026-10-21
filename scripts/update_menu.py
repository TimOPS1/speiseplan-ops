#!/usr/bin/env python3
"""
Speiseplan automatisch aktualisieren.

Holt die Wochenpläne (PDF) von https://ops-dillenburg.de/infos/speiseplan/, liest den Text aus
und schreibt daraus die Datei menu.js, die index.html anzeigt.

Grundsatz: lieber nichts anzeigen als etwas Falsches. Ist die Struktur eines Tages nicht eindeutig
(z. B. nicht genau zwei Gerichtsblöcke), wird der Tag mit "pruefen": true markiert. Die Seite zeigt
dann nur einen Hinweis mit Link zum PDF, und der Lauf endet mit Fehlercode 1 (GitHub schickt eine Mail).

Aufrufe:
  python scripts/update_menu.py                   # Normalbetrieb: Seite abrufen, PDFs laden, menu.js schreiben
  python scripts/update_menu.py --pdf plan.pdf    # lokale PDF-Datei prüfen statt Download
  python scripts/update_menu.py --pdf plan.pdf --dry-run   # nur anzeigen, nichts schreiben
"""
import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

PAGE_URL = "https://ops-dillenburg.de/infos/speiseplan/"
ALLOWED_HOSTS = {"ops-dillenburg.de", "www.ops-dillenburg.de"}
USER_AGENT = "ops-speiseplan-updater (automatischer Abruf des oeffentlichen Speiseplans, wenige Abrufe pro Tag)"
ROOT = Path(__file__).resolve().parent.parent
MAX_PDFS = 4
MAX_PDF_BYTES = 5_000_000

WEEKDAYS = ["montag", "dienstag", "mittwoch", "donnerstag", "freitag"]
WEEKDAY_SHORT = ["Mo", "Di", "Mi", "Do", "Fr"]
MONTHS = ["januar", "februar", "maerz", "april", "mai", "juni", "juli", "august",
          "september", "oktober", "november", "dezember"]

DAY_RE = re.compile(r"^\s*(Montag|Dienstag|Mittwoch|Donnerstag|Freitag)\b\s*(.*)$", re.I)
DASH_RE = re.compile(r"^[-–—_\s]{3,}$")
DESSERT_RE = re.compile(r"^dessert\s*:?$", re.I)
FOOTER_RE = re.compile(r"(Wir verarbeiten|Küche der|Änderungen möglich|Allergene|Zusatzstoffe|Bio-Produkte)", re.I)
SKIP_RE = re.compile(r"^\d+(?:[,\s]+\d+)*$|^\([a-z0-9,\s;]+\)$", re.I)
RANGE_RE = re.compile(
    r"(\d{1,2})\.\s*(?:([A-Za-zÄÖÜäöüß]+)\.?\s*)?[–—-]\s*(\d{1,2})\.\s*([A-Za-zÄÖÜäöüß]+)\.?\s*(\d{4})")
VEG_PREFIX_RE = re.compile(r"^(vegetarische[rsnm]?|vegetarisch|vegane[rsnm]?|vegan)\s+", re.I)
NONE_TEXT = {"fleisch": "Heute gibt es kein Fleischgericht", "veg": "Heute gibt es kein vegetarisches Gericht"}


class ParseError(Exception):
    pass


def norm(s):
    """Vergleichsform: klein, Umlaute aufgelöst, nur Buchstaben und Ziffern."""
    s = s.lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s)
    return re.sub(r"[^a-z0-9]", "", s)


def clean(text):
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\s*\((?:[0-9a-z]{1,3}[,;\s]*)+\)\s*$", "", text, flags=re.I)  # Allergenkürzel am Zeilenende
    return text.rstrip("*").strip()


# ---------------------------------------------------------------- Bildbibliothek (data.js)

def load_library(path):
    text = Path(path).read_text(encoding="utf-8")
    m = re.search(r"window\.DISHES\s*=\s*(\{.*\})\s*;", text, re.S)
    if not m:
        raise ParseError("data.js: Bildbibliothek nicht gefunden")
    return json.loads(m.group(1))


class Library:
    def __init__(self, dishes):
        self.dishes = dishes
        self.by_norm = {}
        for key, d in dishes.items():
            for n in (norm(d["n"]), norm(key)):
                keys = self.by_norm.setdefault(n, [])
                if key not in keys:
                    keys.append(key)

    def find(self, text, role):
        cands = self.by_norm.get(norm(text), [])
        if role == "main":
            cands = [k for k in cands if not k.endswith("-klein")] or cands
        return cands[0] if cands else None

    def resolve(self, text, role):
        """Gibt (Eintrag, bekannt) zurück. Eintrag: Schlüsselstring, {k, t} oder {t[, platzhalter]}."""
        text = clean(text)
        key = self.find(text, role)
        if key:
            return key, True
        m = VEG_PREFIX_RE.match(text)
        if m:
            key = self.find(text[m.end():], role)
            if key:
                return {"k": key, "t": text}, True
        if role == "main":
            return {"t": text, "platzhalter": True}, False
        return {"t": text}, False

    def known(self, text, role="main"):
        return self.resolve(text, role)[1]

    def label(self, item):
        if isinstance(item, str):
            item = {"k": item}
        d = self.dishes.get(item.get("k", ""))
        return item.get("t") or (d["n"] if d else item.get("k", ""))


# ---------------------------------------------------------------- Plan-Text -> Struktur

def month_num(word):
    w = norm(word)
    if len(w) < 3:
        return None
    for i, name in enumerate(MONTHS, 1):
        if name.startswith(w[:3]):
            return i
    return None


def parse_header(head_lines):
    """Gibt (Montag der Woche, Kalenderwoche) zurück. Datumsbereich und KW müssen zusammenpassen."""
    t = " ".join(head_lines)
    kw = None
    mk = re.search(r"KW\s*(\d{1,2})\s*/\s*(\d{4})", t)
    if mk:
        kw = (int(mk.group(2)), int(mk.group(1)))
    monday_h = None
    m = RANGE_RE.search(t)
    if m:
        d1, m1w, d2, m2w, y = int(m.group(1)), m.group(2), int(m.group(3)), m.group(4), int(m.group(5))
        m2 = month_num(m2w)
        m1 = month_num(m1w) if m1w else m2
        if m1 and m2:
            try:
                start = date(y - 1 if m1 > m2 else y, m1, d1)
                monday_h = start - timedelta(days=start.weekday())
            except ValueError:
                monday_h = None
    monday_k = None
    if kw:
        try:
            monday_k = date.fromisocalendar(kw[0], kw[1], 1)
        except ValueError:
            monday_k = None
    if monday_h and monday_k and monday_h != monday_k:
        raise ParseError(f"Datumsbereich ({monday_h}) und Kalenderwoche ({monday_k}) passen nicht zusammen")
    monday = monday_h or monday_k
    if not monday:
        raise ParseError("Weder Datumsbereich noch Kalenderwoche im Plan gefunden")
    return monday, (kw[1] if kw else monday.isocalendar()[1])


def split_days(text):
    """Zerlegt den Text in Kopfzeilen und Tage. Alles nach der Fußzeile (Bio, Küche, Änderungen) entfällt."""
    head, days, cur, last_idx = [], {}, None, -1
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        m = DAY_RE.match(raw)
        if m:
            idx = WEEKDAYS.index(m.group(1).lower())
            if idx in days or idx < last_idx:
                raise ParseError(f"Wochentag {m.group(1)} doppelt oder in falscher Reihenfolge")
            last_idx = idx
            cur = days[idx] = []
            raw = m.group(2).strip()
            if not raw:
                continue
        elif cur is None:
            head.append(raw)
            continue
        if FOOTER_RE.search(raw):
            break
        cur.append(raw)
    return head, days


def to_blocks(tokens):
    """Teilt die Zeilen eines Tages in Gerichtsblöcke. Ein Block endet mit 'Dessert'; ein Strich ist ein leerer Block."""
    blocks, cur = [], []
    for t in tokens:
        if DASH_RE.match(t):
            if cur:
                return None
            blocks.append({"none": True})
        elif DESSERT_RE.match(t):
            if not cur:
                return None
            blocks.append({"lines": cur, "dessert": True})
            cur = []
        elif SKIP_RE.match(t):
            continue
        else:
            cur.append(t)
    if cur:
        blocks.append({"lines": cur, "dessert": False})
    return blocks


def split_main(lines, lib):
    """Trennt Hauptgericht und Beilagen. Umbrüche mitten im Namen werden zusammengeführt."""
    first = lines[0]
    if len(lines) > 1:
        nxt = lines[1]
        if first.endswith("-"):
            return first + nxt, lines[2:]
        joined = first + " " + nxt
        wrapped = (re.search(r"\b(mit|und|in|auf|an|aus|nach)$", first, re.I)
                   or nxt[:1].islower()
                   or re.match(r"(mit|und|in|auf)\s", nxt, re.I)
                   or (not lib.known(first) and lib.known(joined)))
        if wrapped:
            return joined, lines[2:]
    return first, lines[1:]


def block_to_col(block, which, lib, unknown):
    if block.get("none"):
        return {"none": NONE_TEXT[which]}
    main_text, rest = split_main(block["lines"], lib)
    main, known = lib.resolve(main_text, "main")
    if not known:
        unknown.append(clean(main_text))
    sides = []
    for s in rest:
        item, known = lib.resolve(s, "side")
        if not known:
            unknown.append(clean(s))
        sides.append(item)
    if isinstance(main, str):
        main = {"k": main}
    return {"main": main, "sides": sides, "dessert": bool(block["dessert"])}


def parse_week(text, lib, pdf_url=None):
    """Gibt (menu, probleme, unbekannte_gerichte) zurück. menu: {"2026-10-19": {...}}."""
    head, days = split_days(text)
    if not days:
        raise ParseError("Keine Wochentage im Plan gefunden")
    monday, kw = parse_header(head)
    menu, problems, unknown = {}, [], []
    for idx in sorted(days):
        day = monday + timedelta(days=idx)
        key = day.isoformat()
        meta = {"kw": kw}
        if pdf_url:
            meta["pdf"] = pdf_url
        blocks = to_blocks(days[idx])
        if blocks is None or len(blocks) != 2 or not all(b.get("none") or b.get("lines") for b in blocks):
            menu[key] = {"pruefen": True, **meta}
            problems.append(f"{key} ({WEEKDAY_SHORT[idx]}): Aufbau nicht eindeutig ({0 if blocks is None else len(blocks)} Blöcke statt 2)")
            continue
        local_unknown = []
        entry = {
            "fleisch": block_to_col(blocks[0], "fleisch", lib, local_unknown),
            "veg": block_to_col(blocks[1], "veg", lib, local_unknown),
        }
        unknown.extend(local_unknown)
        menu[key] = {**entry, **meta}
    return menu, problems, unknown


def describe(menu, lib):
    """Lesbare Zusammenfassung für Protokoll und Kontrolle."""
    out = []
    for key in sorted(menu):
        e = menu[key]
        wd = WEEKDAY_SHORT[date.fromisoformat(key).weekday()] if date.fromisoformat(key).weekday() < 5 else "?"
        if e.get("pruefen"):
            out.append(f"{wd} {key}: BITTE PRÜFEN (nicht sicher lesbar)")
            continue
        parts = []
        for which, name in (("fleisch", "Fleisch"), ("veg", "Veg"), ):
            c = e[which]
            if c.get("none"):
                parts.append(f"{name}: [{c['none']}]")
            else:
                sides = ", ".join(lib.label(s) for s in c["sides"])
                parts.append(f"{name}: {lib.label(c['main'])}" + (f" + {sides}" if sides else "") + (" + Dessert" if c["dessert"] else ""))
        out.append(f"{wd} {key}: " + " | ".join(parts))
    return out


# ---------------------------------------------------------------- Abruf

class LinkFinder(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self._href, self._text = [], None, []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href, self._text = dict(attrs).get("href"), []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href:
            self.links.append((self._href, "".join(self._text).strip()))
            self._href = None


def http_get(url, retries=3):
    import requests
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            last = e
            time.sleep(3 * (i + 1))
    raise last


def find_pdf_urls(html):
    finder = LinkFinder()
    finder.feed(html)
    urls = []
    for href, text in finder.links:
        url = urljoin(PAGE_URL, href.strip())
        parsed = urlparse(url)
        if not parsed.path.lower().endswith(".pdf") or parsed.hostname not in ALLOWED_HOSTS:
            continue
        url = "https://" + parsed.netloc + parsed.path  # immer https
        if url not in [u for u, _ in urls]:
            urls.append((url, text))
    kw = [u for u, t in urls if re.search(r"KW[\s_\-–]*\d{1,2}", u + " " + t, re.I)]
    sp = [u for u, t in urls if re.search(r"speiseplan", u + " " + t, re.I)]
    return (kw or sp)[:MAX_PDFS]


def _lines(words, tol=3):
    """Wörter zu Zeilen gruppieren (oben nach unten, links nach rechts)."""
    out, cur, top = [], [], None
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if cur and w["top"] - top > tol:
            out.append(cur)
            cur = []
        if not cur:
            top = w["top"]
        cur.append(w)
    if cur:
        out.append(cur)
    return [" ".join(w["text"].strip() for w in sorted(l, key=lambda w: w["x0"])) for l in out]


def layout_to_text(words, hlines):
    """
    Baut aus Wortpositionen einer Seite den Plan-Text: Kopf, dann je Tag der Wochentag,
    die Zeilen der linken Spalte (Fleisch), dann die der rechten (vegetarisch).
    pdfplumber liest die Seite sonst zeilenweise quer über beide Spalten und vermischt die Gerichte.
    words: [{text, x0, x1, top, bottom}], hlines: [{top, x0, x1}] waagerechte Tabellenlinien.
    """
    words = [w for w in words if any(c.isalnum() for c in w["text"]) or DASH_RE.match(w["text"])]
    labels = [w for w in words if DAY_RE.match(w["text"]) and not DAY_RE.match(w["text"]).group(2)]
    if not labels:
        return "\n".join(_lines(words))  # ohne Wochentage meldet parse_week den Fehler
    labels.sort(key=lambda w: w["top"])
    lab_x0 = min(w["x0"] for w in labels)
    lab_x1 = max(w["x1"] for w in labels)
    # Zeilentrenner: Linien, die auch die Wochentagsspalte durchqueren
    rows = sorted({round(l["top"], 1) for l in hlines if l["x0"] <= lab_x0 + 5 and l["x1"] >= lab_x1 - 5})

    # Spaltengrenze aus der Lage der "Dessert"-Zeilen: es muss genau zwei Gruppen geben
    xs = sorted((w["x0"] + w["x1"]) / 2 for w in words if DESSERT_RE.match(w["text"].strip()))
    groups = []
    for x in xs:
        if groups and x - groups[-1][-1] < 50:
            groups[-1].append(x)
        else:
            groups.append([x])
    if len(groups) != 2:
        raise ParseError(f"Spalten nicht erkannt ({len(groups)} Dessert-Spalten statt 2)")
    split = (max(groups[0]) + min(groups[1])) / 2

    head_end = None
    out = []
    for lab in labels:
        above = [y for y in rows if y <= lab["top"] + 1]
        below = [y for y in rows if y >= lab["bottom"] - 1]
        if not above or not below:
            raise ParseError(f"Tabellenzeile für {lab['text'].strip()} nicht gefunden")
        y0, y1 = above[-1], below[0]
        head_end = y0 if head_end is None else head_end
        cell = [w for w in words if w is not lab and y0 - 1 <= w["top"] and w["bottom"] <= y1 + 1]
        if any(DAY_RE.match(w["text"]) for w in cell):
            raise ParseError(f"Mehrere Wochentage in einer Tabellenzeile ({lab['text'].strip()})")
        cell = [w for w in cell if w["x0"] > lab_x1]
        if any(w["x0"] < split < w["x1"] for w in cell):
            raise ParseError(f"{lab['text'].strip()}: Text liegt über beiden Spalten")
        out.append(lab["text"].strip())
        out += _lines([w for w in cell if w["x1"] <= split])
        out += _lines([w for w in cell if w["x0"] >= split])
    head = _lines([w for w in words if w["bottom"] <= head_end + 1])
    return "\n".join(head + out)


def pdf_to_text(data):
    import io
    import pdfplumber
    pages = []
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for p in pdf.pages:
            words = p.extract_words(keep_blank_chars=True, x_tolerance=2)
            hlines = [{"top": e["top"], "x0": e["x0"], "x1": e["x1"]} for e in p.horizontal_edges]
            pages.append(layout_to_text(words, hlines))
    return "\n".join(pages)


# ---------------------------------------------------------------- menu.js schreiben

HEADER = """// AUTOMATISCH ERZEUGT von scripts/update_menu.py - bitte nicht von Hand ändern (wird beim nächsten Lauf überschrieben).
// Quelle: Speiseplan der Küche der Lebenshilfe Flammersbach, veröffentlicht auf https://ops-dillenburg.de/infos/speiseplan/ (Änderungen möglich).
// Pro Tag "fleisch" und "veg". Gericht = Schlüssel aus data.js, oder { k, t } mit abweichendem Namen, oder { t } ohne Bild.
// "pruefen": true = der Tag ließ sich nicht sicher lesen; die Seite zeigt dann nur einen Hinweis mit Link zum PDF.
"""


def render_js(menu):
    return HEADER + "window.MENU = " + json.dumps(menu, ensure_ascii=False, indent=2, sort_keys=False) + ";\n"


def load_existing(path):
    try:
        text = Path(path).read_text(encoding="utf-8")
        m = re.search(r"window\.MENU\s*=\s*(\{.*\})\s*;", text, re.S)
        return json.loads(m.group(1)) if m else {}
    except Exception:
        return {}


def summary(lines):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf", action="append", help="lokale PDF-Datei statt Download (mehrfach möglich)")
    ap.add_argument("--text", action="append", help="Textdatei statt PDF (für Tests)")
    ap.add_argument("--out", default=str(ROOT / "menu.js"))
    ap.add_argument("--dry-run", action="store_true", help="nur anzeigen, nichts schreiben")
    args = ap.parse_args(argv)

    lib = Library(load_library(ROOT / "data.js"))
    existing = load_existing(args.out)
    sources = []  # (pdf_url oder None, Text oder Fehlermeldung, ok)

    if args.text:
        sources = [(None, Path(p).read_text(encoding="utf-8")) for p in args.text]
    elif args.pdf:
        for p in args.pdf:
            try:
                sources.append((None, pdf_to_text(Path(p).read_bytes())))
            except Exception as e:
                sources.append((None, e if isinstance(e, ParseError) else ParseError(f"PDF nicht lesbar: {e}")))
    else:
        html = http_get(PAGE_URL).text
        urls = find_pdf_urls(html)
        if not urls:
            msg = "Auf der Speiseplan-Seite wurde kein PDF gefunden. menu.js bleibt unverändert."
            print("::warning::" + msg)
            summary([f"⚠️ {msg}"])
            return 0
        for url in urls:
            r = http_get(url)
            if len(r.content) > MAX_PDF_BYTES or not r.content.startswith(b"%PDF-"):
                sources.append((url, ParseError("Datei ist kein PDF oder ungewöhnlich groß")))
                continue
            try:
                sources.append((url, pdf_to_text(r.content)))
            except Exception as e:  # defektes PDF
                sources.append((url, ParseError(f"PDF nicht lesbar: {e}")))

    menu, problems, unknown, errors = {}, [], [], []
    for url, text in sources:
        try:
            if isinstance(text, Exception):
                raise text
            m, p, u = parse_week(text, lib, url)
            menu.update(m)
            problems += p
            unknown += u
        except ParseError as e:
            errors.append(f"{url or 'Eingabe'}: {e}")
            # Zuvor korrekt gelesene Tage dieses PDFs behalten, statt sie zu verlieren
            for k, v in existing.items():
                if url and v.get("pdf") == url:
                    menu[k] = v

    if not menu:
        for e in errors:
            print("::error::" + e)
        summary(["❌ Kein Speiseplan lesbar. menu.js bleibt unverändert.", *[f"- {e}" for e in errors]])
        return 1

    menu = dict(sorted(menu.items()))
    lines = describe(menu, lib)
    print("\n".join(lines))
    report = ["### Gelesener Speiseplan", *[f"- {l}" for l in lines]]
    uniq = sorted(set(unknown))
    if uniq:
        print("::warning::Gerichte ohne Bild in data.js: " + ", ".join(uniq))
        report += ["", "**Gerichte ohne Bild** (in `data.js` und `img/` ergänzen): " + ", ".join(uniq)]
    for x in problems + errors:
        print("::error::" + x)
    if problems or errors:
        report += ["", "**Probleme:**", *[f"- {x}" for x in problems + errors]]
    summary(report)

    new_js = render_js(menu)
    old_js = Path(args.out).read_text(encoding="utf-8") if Path(args.out).exists() else ""
    if args.dry_run:
        print("(dry-run: nichts geschrieben)")
    elif new_js != old_js:
        Path(args.out).write_text(new_js, encoding="utf-8")
        print(f"{args.out} aktualisiert")
    else:
        print("Keine Änderung gegenüber dem bisherigen Stand")
    return 1 if (problems or errors) else 0


if __name__ == "__main__":
    sys.exit(main())
