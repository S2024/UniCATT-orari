"""Aggiorna lo stesso ICS con lezioni filtrate ed esami senza filtri di canale.
Uso: python update_calendar.py URL_LEZIONI URL_ESAMI
Richiede Playwright e l'estrattore lezioni esistente: extract_ics.js.
"""
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import Error as PlaywrightError, sync_playwright

ROOT = Path(__file__).resolve().parent
OUTPUT_PATH = ROOT / "orari_lezioni.ics"
JS_PATH = ROOT / "extract_ics.js"
MAX_ATTEMPTS = 5
BASE_DELAY_SECONDS = 5
ROME = ZoneInfo("Europe/Rome")

EXAM_JS = r"""() => {
  const tables = [...document.querySelectorAll('table.react-collapsible')]
    .filter(t => t.querySelector('th.column-giorno') && t.querySelector('th.column-appello'));
  const rows = tables.flatMap(t => [...t.querySelectorAll('tbody tr')]);
  if (!rows.length) throw new Error('Nessuna riga di esame trovata');
  const cell = (row, cls) => {
    const td = row.querySelector('td.' + cls);
    if (!td) throw new Error('Colonna mancante: ' + cls);
    const spans = td.querySelectorAll('span');
    return (spans.length >= 2 ? spans[1].textContent : td.textContent).trim();
  };
  return rows.map((row, i) => {
    const item = Object.fromEntries(
      ['insegnamento','giorno','orario','edificio','aula','docente','appello']
        .map(cls => [cls, cell(row, cls)])
    );
    for (const key of ['insegnamento','giorno','orario','appello']) {
      if (!item[key]) throw new Error(`Riga ${i + 1}: campo vuoto ${key}`);
    }
    return item;
  });
}"""


def event_blocks(ics):
    return re.findall(r"BEGIN:VEVENT\r?\n.*?END:VEVENT", ics, re.S)


def unfolded(block):
    return re.sub(r"\n[ \t]", "", block.replace("\r\n", "\n"))


def field(block, name):
    match = re.search(r"^" + re.escape(name) + r"(?:;[^:\n]*)?:(.*)$", unfolded(block), re.M)
    return match.group(1) if match else None


def event_date(block, name):
    text = unfolded(block)
    match = re.search(r"^" + name + r"(?P<params>;[^:\n]*)?:(?P<value>[^\n]+)$", text, re.M)
    if not match:
        raise ValueError(f"Evento senza {name}")
    value = match['value']
    dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ" if value.endswith('Z') else "%Y%m%dT%H%M%S")
    if value.endswith('Z'):
        return dt.replace(tzinfo=timezone.utc)
    tz = re.search(r'TZID=([^;]+)', match['params'] or '')
    return dt.replace(tzinfo=ZoneInfo(tz.group(1)) if tz else ROME)


def validate_calendar(ics):
    if not ics.startswith('BEGIN:VCALENDAR\r\n') or not ics.endswith('END:VCALENDAR\r\n'):
        raise ValueError('Contenitore ICS non valido')
    blocks = event_blocks(ics)
    if not blocks or len(blocks) != ics.count('BEGIN:VEVENT') or len(blocks) != ics.count('END:VEVENT'):
        raise ValueError('Nessun evento o struttura VEVENT incompleta')
    seen = set()
    for block in blocks:
        uid = field(block, 'UID')
        if not uid or uid in seen:
            raise ValueError(f'UID assente o duplicato: {uid}')
        seen.add(uid)
        if not field(block, 'SUMMARY'):
            raise ValueError(f'Titolo assente: {uid}')
        if event_date(block, 'DTEND') <= event_date(block, 'DTSTART'):
            raise ValueError(f'Intervallo non valido: {uid}')
    return len(blocks)


def escape(value):
    return (str(value).replace('\\', '\\\\').replace('\r\n', '\n')
            .replace('\r', '\n').replace('\n', r'\n').replace(';', r'\;').replace(',', r'\,'))


def fold(line):
    parts, current = [], ''
    for char in line:
        if len((current + char).encode('utf-8')) > 75:
            parts.append(current)
            current = ' '
        current += char
    parts.append(current)
    return '\r\n'.join(parts)


def exam_events(rows, old_ics, url):
    old_stamps = {field(b, 'UID'): field(b, 'DTSTAMP') for b in event_blocks(old_ics)}
    now = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    events, seen = [], set()
    for row in rows:
        day = datetime.strptime(row['giorno'], '%d/%m/%Y')
        hours = re.fullmatch(r'(\d{2}):(\d{2})\s*[-–]\s*(\d{2}):(\d{2})', row['orario'])
        if not hours:
            raise ValueError(f"Orario esame non riconosciuto: {row['orario']}")
        sh, sm, eh, em = map(int, hours.groups())
        start = day.replace(hour=sh, minute=sm, tzinfo=ROME)
        end = day.replace(hour=eh, minute=em, tzinfo=ROME)
        if end < start:
            end += timedelta(days=1)
        if end == start:
            raise ValueError(f"Durata nulla: {row['insegnamento']}")
        raw = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        uid = 'esame-' + hashlib.sha256(raw.encode()).hexdigest() + '@orari-scraper'
        if uid in seen:
            raise ValueError(f'Riga esame identica ripetuta: {uid}')
        seen.add(uid)
        location = ', '.join(filter(None, [row['aula'], row['edificio']]))
        description = '\n'.join(filter(None, [
            'Docente: ' + row['docente'] if row['docente'] else '',
            'Tipo: ' + row['appello'], 'Fonte: ' + url,
        ]))
        lines = [
            'BEGIN:VEVENT', 'UID:' + uid,
            'DTSTAMP:' + (old_stamps.get(uid) or now),
            'DTSTART:' + start.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
            'DTEND:' + end.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
            'SUMMARY:' + escape(row['appello'] + ': ' + row['insegnamento']),
        ]
        if location:
            lines.append('LOCATION:' + escape(location))
        lines += ['DESCRIPTION:' + escape(description), 'END:VEVENT']
        events.append('\r\n'.join(fold(line) for line in lines) + '\r\n')
    if not events:
        raise ValueError('Nessun esame estratto: il calendario precedente viene conservato')
    return ''.join(events)


def fetch_source(browser, url, kind, lesson_js):
    selector = ('table.react-collapsible tbody tr td.orario' if kind == 'lezioni'
                else 'table.react-collapsible tbody tr td.giorno')
    for attempt in range(1, MAX_ATTEMPTS + 1):
        context = browser.new_context(locale='it-IT', timezone_id='Europe/Rome')
        try:
            page = context.new_page()
            print(f'[{kind}] Tentativo {attempt}/{MAX_ATTEMPTS}', flush=True)
            response = page.goto(url, wait_until='domcontentloaded', timeout=60_000)
            if response is None or response.status >= 400:
                raise RuntimeError(f'HTTP {response.status if response else "nessuna risposta"}')
            page.wait_for_function(
                '(sel) => [...document.querySelectorAll(sel)].some(el => el.textContent.trim())',
                arg=selector, timeout=60_000,
            )
            # Una breve stabilita del DOM riduce le letture durante il rendering.
            deadline, previous, stable_since = time.monotonic() + 30, None, time.monotonic()
            while True:
                snapshot = page.locator('table.react-collapsible').all_text_contents()
                if snapshot != previous:
                    previous, stable_since = snapshot, time.monotonic()
                if time.monotonic() - stable_since >= 3:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeError('Le tabelle continuano a cambiare')
                page.wait_for_timeout(500)
            result = page.evaluate(lesson_js if kind == 'lezioni' else EXAM_JS)
            if kind == 'lezioni':
                if not isinstance(result, dict) or result.get('error') or not result.get('ics'):
                    raise ValueError(f'Estrazione lezioni fallita: {result}')
                validate_calendar(result['ics'])
                print(f"Lezioni: {result['count']}; escluse: {result.get('excludedCount', 0)}", flush=True)
            else:
                exam_events(result, '', url)  # Valida ogni riga anche dentro il retry.
                print(f'Esami: {len(result)}', flush=True)
            return result
        except (PlaywrightError, RuntimeError, ValueError, KeyError, TypeError) as error:
            print(f'[{kind}] Fallito: {error}', flush=True)
            if attempt == MAX_ATTEMPTS:
                raise RuntimeError(f'{kind}: tutti i tentativi falliti; ICS non modificato') from error
        finally:
            context.close()
        delay = BASE_DELAY_SECONDS * 2 ** (attempt - 1)
        print(f'[{kind}] Nuovo tentativo tra {delay}s', flush=True)
        time.sleep(delay)


def atomic_write(content):
    data = content.encode('utf-8')
    if OUTPUT_PATH.exists() and OUTPUT_PATH.read_bytes() == data:
        print('Nessuna modifica al calendario.', flush=True)
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=OUTPUT_PATH.parent, prefix='.orari-', delete=False) as tmp:
            temporary = Path(tmp.name)
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(temporary, OUTPUT_PATH)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print(f'Calendario aggiornato: {len(data)} byte.', flush=True)


def main():
    if len(sys.argv) != 3 or not all(sys.argv[1:]):
        raise ValueError('Uso: python update_calendar.py URL_LEZIONI URL_ESAMI; entrambi obbligatori')
    lessons_url, exams_url = sys.argv[1:]
    for url in (lessons_url, exams_url):
        if not url.startswith(('https://', 'http://')) or '\n' in url or '\r' in url:
            raise ValueError('Inserire URL HTTP/HTTPS semplici nelle variabili della repository')
    lesson_js = JS_PATH.read_text(encoding='utf-8')
    old_ics = OUTPUT_PATH.read_bytes().decode('utf-8') if OUTPUT_PATH.exists() else ''
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            lessons = fetch_source(browser, lessons_url, 'lezioni', lesson_js)
            exams = fetch_source(browser, exams_url, 'esami', lesson_js)
        finally:
            browser.close()
    # Non riserializzare gli eventi delle lezioni: UID e proprieta restano invariati.
    merged = (lessons['ics'].removesuffix('END:VCALENDAR\r\n')
              + exam_events(exams, old_ics, exams_url) + 'END:VCALENDAR\r\n')
    count = validate_calendar(merged)
    print(f'Validazione completata: {count} eventi totali.', flush=True)
    atomic_write(merged)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'ERRORE: {error}. Il file precedente non viene sovrascritto.', file=sys.stderr, flush=True)
        sys.exit(1)
