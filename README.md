# Speiseplan OPS – Daten und Automatik

Liest den Wochenplan (PDF) von https://ops-dillenburg.de/infos/speiseplan/ und schreibt ihn als `menu.js`.
Die Anzeige selbst liegt nicht hier, sondern im IServ-Ordner der Schule.

- `scripts/update_menu.py` – Plan-Leser
- `scripts/test_parse.py` – Tests des Plan-Lesers
- `.github/workflows/update-menu.yml` – automatischer Lauf (Mo–Fr morgens, Fr-Nachmittag und So-Abend)
- `menu.js` – der Plan, wird automatisch erzeugt, nicht von Hand ändern
- `data.js` – Namen der Gerichte und zugehörige Bilddateinamen (die Bilder liegen nicht hier)

Plan lokal prüfen: `python3 scripts/update_menu.py --pdf plan.pdf --dry-run`
