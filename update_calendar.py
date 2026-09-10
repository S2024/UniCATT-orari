"""
Rigenera orari_lezioni.ics scaricando la pagina con un browser headless
(Playwright), eseguendo lì la logica di estrazione già validata, e
scrivendo il file solo se il contenuto e' cambiato rispetto alla versione
precedente. Include retry con backoff esponenziale sul caricamento della
pagina, per resistere a 503 sporadici o rendering lento del sito sorgente.

Installazione:
    pip install playwright
    playwright install chromium

Uso:
    python update_calendar.py "https://tuo-sito/orario"
"""

import hashlib
import pathlib
import sys
import time

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

OUTPUT_PATH = pathlib.Path("orari_lezioni.ics")
READY_SELECTOR = "table.react-collapsible"
JS_PATH = pathlib.Path("extract_ics.js")

MAX_ATTEMPTS = 5
BASE_DELAY_SECONDS = 5  # 5s, 10s, 20s, 40s, 80s tra un tentativo e l'altro
NAV_TIMEOUT_MS = 30_000
SELECTOR_TIMEOUT_MS = 30_000


def load_page_with_retry(page, url: str):
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            print(f"[Tentativo {attempt}/{MAX_ATTEMPTS}] Caricamento pagina...")
            response = page.goto(url, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)

            if response is not None and response.status >= 500:
                raise RuntimeError(f"Risposta server {response.status}")

            page.wait_for_selector(READY_SELECTOR, timeout=SELECTOR_TIMEOUT_MS)
            print(f"[Tentativo {attempt}/{MAX_ATTEMPTS}] Pagina caricata correttamente.")
            return
        except (PlaywrightTimeoutError, RuntimeError) as e:
            last_error = e
            print(f"[Tentativo {attempt}/{MAX_ATTEMPTS}] Fallito: {e}")
            if attempt < MAX_ATTEMPTS:
                delay = BASE_DELAY_SECONDS * (2 ** (attempt - 1))
                print(f"Riprovo tra {delay}s...")
                time.sleep(delay)

    raise RuntimeError(
        f"Caricamento pagina fallito dopo {MAX_ATTEMPTS} tentativi. Ultimo errore: {last_error}"
    )


def fetch_ics(url: str) -> str:
    js_code = JS_PATH.read_text(encoding="utf-8")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()

        try:
            load_page_with_retry(page, url)
            result = page.evaluate(js_code)
        finally:
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

    try:
        ics_content = fetch_ics(url)
    except RuntimeError as e:
        print(f"ERRORE: {e}")
        sys.exit(1)

    write_if_changed(ics_content)
    sys.exit(0)
