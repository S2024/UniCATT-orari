"""
Rigenera orari_lezioni.ics scaricando la pagina con un browser headless
(Playwright), eseguendo lì la logica di estrazione già validata, e
scrivendo il file solo se il contenuto e' cambiato rispetto alla versione
precedente (utile per evitare commit/notifiche inutili quando lo esegui
periodicamente da cron o GitHub Actions).

Installazione:
    pip install playwright
    playwright install chromium

Uso:
    python update_calendar.py "https://tuo-sito/orario"
"""

import hashlib
import pathlib
import sys

from playwright.sync_api import sync_playwright

OUTPUT_PATH = pathlib.Path("orari_lezioni.ics")
# Selettore che indica che i dati sono stati renderizzati (non solo la rotella).
READY_SELECTOR = "table.react-collapsible"
JS_PATH = pathlib.Path("extract_ics.js")


def fetch_ics(url: str) -> str:
    js_code = JS_PATH.read_text(encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(url, wait_until="networkidle")

        # Aspetta che almeno una tabella di orario sia comparsa nel DOM,
        # cioè che React abbia finito di caricare e renderizzare i dati.
        page.wait_for_selector(READY_SELECTOR, timeout=30_000)

        result = page.evaluate(js_code)
        browser.close()

    if result.get("error"):
        raise RuntimeError(f"Estrazione fallita: {result['error']}")

    print(f"Trovate {result['count']} lezioni (escluse {result['excludedCount']} M-Z).")
    return result["ics"]


def write_if_changed(new_content: str) -> bool:
    old_hash = None
    if OUTPUT_PATH.exists():
        old_hash = hashlib.sha256(OUTPUT_PATH.read_bytes()).hexdigest()

    new_hash = hashlib.sha256(new_content.encode("utf-8")).hexdigest()

    if old_hash == new_hash:
        print("Nessuna modifica rispetto alla versione precedente.")
        return False

    OUTPUT_PATH.write_text(new_content, encoding="utf-8", newline="")
    print(f"File {OUTPUT_PATH} aggiornato ({len(new_content)} byte).")
    return True


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Uso: python update_calendar.py <url-pagina-orario>")
        sys.exit(1)

    url = sys.argv[1]
    ics_content = fetch_ics(url)
    changed = write_if_changed(ics_content)

    # Exit code utile in CI: 0 = ok (con o senza modifiche), 1 = errore gestito sopra.
    sys.exit(0)
