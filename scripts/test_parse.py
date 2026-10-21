#!/usr/bin/env python3
"""Tests für den Plan-Leser. Aufruf:  python3 scripts/test_parse.py"""
import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import update_menu as um  # noqa: E402

LIB = um.Library(um.load_library(HERE.parent / "data.js"))
KW43 = (HERE / "fixtures" / "kw43.txt").read_text(encoding="utf-8")
LAYOUT = json.loads((HERE / "fixtures" / "kw43_layout.json").read_text(encoding="utf-8"))


def col_text(col):
    if col.get("none"):
        return "KEIN:" + col["none"]
    return (LIB.label(col["main"]), [LIB.label(s) for s in col["sides"]], col["dessert"])


class ParseTests(unittest.TestCase):
    def test_kw43_entspricht_dem_von_hand_gebauten_plan(self):
        """Diese Erwartung ist der Plan, den wir im Artefakt von Hand aus dem PDF übernommen hatten."""
        menu, problems, unknown = um.parse_week(KW43, LIB, "https://ops-dillenburg.de/x.pdf")
        self.assertEqual(problems, [])
        self.assertEqual(unknown, [])
        self.assertEqual(sorted(menu), ["2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22"])
        exp = {
            "2026-10-19": (("Gefüllte Paprikaschote", ["Tomatensoße", "Reis"], True),
                           ("Vegetarisch gefüllte Paprikaschote", ["Tomatensoße", "Reis"], True)),
            "2026-10-20": ("KEIN:Heute gibt es kein Fleischgericht", ("Rührei", ["Spinat", "Kartoffelpüree"], True)),
            "2026-10-21": (("Schweinegulasch", ["Gemüse", "Nudeln"], True), ("Gemüsegulasch", ["Nudeln"], True)),
            "2026-10-22": (("Backfisch", ["Remouladensoße", "Kartoffeln", "Gurkensalat"], True),
                           ("Fischfingers", ["Remouladensoße", "Kartoffeln", "Gurkensalat"], True)),
        }
        for day, (fl, veg) in exp.items():
            self.assertEqual(col_text(menu[day]["fleisch"]), fl, day)
            self.assertEqual(col_text(menu[day]["veg"]), veg, day)
            self.assertEqual(menu[day]["kw"], 43)
            self.assertEqual(menu[day]["pdf"], "https://ops-dillenburg.de/x.pdf")

    def test_echtes_pdf_layout_ergibt_denselben_plan(self):
        """Wortpositionen aus dem echten KW-43-PDF (pdfplumber) müssen exakt den Plan aus kw43.txt ergeben."""
        text = um.layout_to_text(LAYOUT["words"], LAYOUT["hlines"])
        real, problems, unknown = um.parse_week(text, LIB, "https://ops-dillenburg.de/x.pdf")
        expected, _, _ = um.parse_week(KW43, LIB, "https://ops-dillenburg.de/x.pdf")
        self.assertEqual(problems, [])
        self.assertEqual(unknown, [])
        self.assertEqual(real, expected)

    def test_rohtext_von_pdfplumber_vermischt_die_spalten(self):
        """Darum layout_to_text: extract_text liest quer über beide Spalten und liefert falsche Gerichte."""
        raw = (HERE / "fixtures" / "kw43_pdfplumber_roh.txt").read_text(encoding="utf-8")
        menu, _, _ = um.parse_week(raw, LIB)
        expected, _, _ = um.parse_week(KW43, LIB)
        self.assertNotEqual(menu, expected)

    def test_layout_ohne_zweite_dessert_spalte_bricht_ab(self):
        words = [w for w in LAYOUT["words"] if not (w["text"].strip() == "Dessert" and w["x0"] > 520)]
        with self.assertRaises(um.ParseError):
            um.layout_to_text(words, LAYOUT["hlines"])

    def test_layout_ohne_zeilenlinien_bricht_ab(self):
        with self.assertRaises(um.ParseError):
            um.layout_to_text(LAYOUT["words"], [])

    def test_layout_text_ueber_beide_spalten_bricht_ab(self):
        words = LAYOUT["words"] + [{"text": "Sehr langes Gericht über beide Spalten", "x0": 400.0, "x1": 640.0,
                                    "top": 360.0, "bottom": 370.0}]
        with self.assertRaises(um.ParseError):
            um.layout_to_text(words, LAYOUT["hlines"])

    def test_strich_in_eigener_zeile(self):
        text = KW43.replace(" Dienstag ---------", "Dienstag\n---------")
        menu, problems, _ = um.parse_week(text, LIB)
        self.assertEqual(problems, [])
        self.assertIn("none", menu["2026-10-20"]["fleisch"])

    def test_fußzeile_wird_nicht_zum_gericht(self):
        menu, problems, unknown = um.parse_week(KW43, LIB)
        self.assertEqual(problems, [])
        self.assertNotIn("Bio", " ".join(unknown))

    def test_unklarer_tag_wird_zum_pruefen(self):
        """Fehlt bei einem Gericht das Dessert, sind es nur noch ein Block: nicht raten."""
        text = KW43.replace("Gemüsegulasch\nNudeln\nDessert", "Gemüsegulasch\nNudeln")
        text = text.replace("Schweinegulasch\nGemüse \nNudeln\nDessert", "Schweinegulasch\nGemüse \nNudeln")
        menu, problems, _ = um.parse_week(text, LIB)
        self.assertTrue(menu["2026-10-21"].get("pruefen"))
        self.assertEqual(len(problems), 1)
        self.assertNotIn("fleisch", menu["2026-10-21"])
        self.assertIn("fleisch", menu["2026-10-19"])  # andere Tage bleiben nutzbar

    def test_drei_bloecke_sind_unklar(self):
        text = KW43.replace("Gemüsegulasch\nNudeln\nDessert", "Gemüsegulasch\nNudeln\nDessert\nSalatteller\nDessert")
        menu, problems, _ = um.parse_week(text, LIB)
        self.assertTrue(menu["2026-10-21"].get("pruefen"))

    def test_neues_gericht_ohne_bild_bekommt_platzhalter(self):
        text = KW43.replace("Rührei", "Zucchini-Puffer")
        menu, _, unknown = um.parse_week(text, LIB)
        self.assertEqual(unknown, ["Zucchini-Puffer"])
        self.assertEqual(menu["2026-10-20"]["veg"]["main"], {"t": "Zucchini-Puffer", "platzhalter": True})

    def test_umbruch_im_neuen_gerichtsnamen(self):
        text = KW43.replace("Rührei\nSpinat", "Hähnchen-Geschnetzeltes in\nKräutersoße\nSpinat")
        menu, _, unknown = um.parse_week(text, LIB)
        self.assertEqual(menu["2026-10-20"]["veg"]["main"]["t"], "Hähnchen-Geschnetzeltes in Kräutersoße")
        self.assertEqual([LIB.label(s) for s in menu["2026-10-20"]["veg"]["sides"]], ["Spinat", "Kartoffelpüree"])

    def test_allergenkuerzel_werden_ignoriert(self):
        text = KW43.replace("Backfisch\n", "Backfisch (a,d)\n").replace("Reis\nDessert\nVegetarisch", "Reis\n1,2\nDessert\nVegetarisch")
        menu, problems, unknown = um.parse_week(text, LIB)
        self.assertEqual(problems, [])
        self.assertEqual(LIB.label(menu["2026-10-22"]["fleisch"]["main"]), "Backfisch")

    def test_kalenderwoche_und_datum_muessen_zusammenpassen(self):
        with self.assertRaises(um.ParseError):
            um.parse_week(KW43.replace("KW 43/2026", "KW 44/2026"), LIB)

    def test_ohne_datum_aber_mit_kw(self):
        menu, problems, _ = um.parse_week(KW43.replace("19. – 22. Oktober 2026\n", ""), LIB)
        self.assertEqual(sorted(menu)[0], "2026-10-19")

    def test_ohne_datum_und_ohne_kw_wird_abgebrochen(self):
        text = KW43.replace("19. – 22. Oktober 2026\n", "").replace("KW 43/2026\n", "")
        with self.assertRaises(um.ParseError):
            um.parse_week(text, LIB)

    def test_jahreswechsel(self):
        text = KW43.replace("19. – 22. Oktober 2026", "29. Dezember – 1. Januar 2027").replace("KW 43/2026", "KW 53/2026")
        menu, _, _ = um.parse_week(text, LIB)
        self.assertEqual(sorted(menu), ["2026-12-28", "2026-12-29", "2026-12-30", "2026-12-31"])

    def test_wochentag_doppelt_bricht_ab(self):
        with self.assertRaises(um.ParseError):
            um.parse_week(KW43.replace("Donnerstag\n", "Montag\n"), LIB)

    def test_pdf_links_nur_von_der_schulseite(self):
        html = """
        <a href="https://ops-dillenburg.de/wp-content/uploads/2026/10/KW-43-2026-Website.pdf">KW 43 – 2026 Website</a>
        <a href="https://ops-dillenburg.de/wp-content/uploads/2026/10/KW-43-2026-Website.pdf">Herunterladen</a>
        <a href="http://ops-dillenburg.de/wp-content/uploads/2026/10/KW-44-2026-Website.pdf">KW 44</a>
        <a href="https://fremd.example/KW-45.pdf">KW 45</a>
        <a href="/wp-content/uploads/2026/10/Elternbrief.pdf">Elternbrief</a>
        """
        self.assertEqual(um.find_pdf_urls(html), [
            "https://ops-dillenburg.de/wp-content/uploads/2026/10/KW-43-2026-Website.pdf",
            "https://ops-dillenburg.de/wp-content/uploads/2026/10/KW-44-2026-Website.pdf",
        ])

    def test_ausgabe_ist_gueltiges_js_und_stabil(self):
        menu, _, _ = um.parse_week(KW43, LIB, "https://ops-dillenburg.de/x.pdf")
        js = um.render_js(menu)
        self.assertTrue(js.startswith("// AUTOMATISCH ERZEUGT"))
        self.assertEqual(js, um.render_js(menu))
        self.assertIn("window.MENU = {", js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
