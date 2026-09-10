// Versione "headless" del tuo scraper: invece di scaricare un file via Blob,
// restituisce la stringa ICS al chiamante (Playwright la riceve come valore
// di ritorno di page.evaluate). Logica di parsing invariata rispetto
// all'originale usato in console.
() => {
  "use strict";

  function pad(n) {
    return String(n).padStart(2, "0");
  }

  function parseDateCaption(text) {
    const m = text.match(/(\d{2})\/(\d{2})\/(\d{4})/);
    if (!m) return null;
    const [, dd, mm, yyyy] = m;
    return { day: +dd, month: +mm, year: +yyyy };
  }

  function getCellText(row, cls) {
    const td = row.querySelector(`td.${cls}`);
    if (!td) return "";
    const spans = td.querySelectorAll("span");
    if (spans.length >= 2) return spans[1].textContent.trim();
    return td.textContent.trim();
  }

  function toICSDate(year, month, day, hour, minute) {
    return `${year}${pad(month)}${pad(day)}T${pad(hour)}${pad(minute)}00`;
  }

  function escapeICSField(str) {
    return String(str)
      .replace(/\\/g, "\\\\")
      .replace(/;/g, "\\;")
      .replace(/,/g, "\\,");
  }

  function uid(l) {
    const raw = `${l.subject}-${l.year}${l.month}${l.day}${l.startHour}${l.startMin}${l.room}-${l.teacher}-${l.note}`;
    let hash = 0;
    for (let i = 0; i < raw.length; i++) {
      hash = (hash << 5) - hash + raw.charCodeAt(i);
      hash |= 0;
    }
    return `lezione-${Math.abs(hash)}@orari-scraper`;
  }

  function isExcluded(note) {
    const upper = note.toUpperCase().trim();
    const hasGruppo = /GRUPPO\s*\d/.test(upper);
    const isMZ = /^(CANALE\s*)?M\s*-?\s*Z$/.test(upper);
    return isMZ && !hasGruppo;
  }

  const dayBlocks = document.querySelectorAll("div.col-12.mb-5");
  const lessons = [];
  let excludedCount = 0;

  dayBlocks.forEach((block) => {
    const captionEl = block.querySelector(".react-collapsible-caption");
    if (!captionEl) return;
    const date = parseDateCaption(captionEl.textContent.trim());
    if (!date) return;

    const table = block.querySelector("table.react-collapsible");
    if (!table) return;

    const rows = table.querySelectorAll("tbody tr");
    rows.forEach((row) => {
      const subject = getCellText(row, "insegnamento");
      const room = getCellText(row, "aula");
      const building = getCellText(row, "edificio");
      const teacher = getCellText(row, "docente");
      const orario = getCellText(row, "orario");
      const note = getCellText(row, "note");

      if (!subject || !orario) return;
      if (isExcluded(note)) {
        excludedCount++;
        return;
      }

      const timeMatch = orario.match(/(\d{2}):(\d{2})\s*-\s*(\d{2}):(\d{2})/);
      if (!timeMatch) return;
      const [, sh, sm, eh, em] = timeMatch.map(Number);

      lessons.push({
        ...date,
        subject,
        room,
        building,
        teacher,
        note,
        startHour: sh,
        startMin: sm,
        endHour: eh,
        endMin: em,
      });
    });
  });

  if (lessons.length === 0) {
    return { error: "Nessuna lezione trovata: verifica selettori o attesa caricamento.", ics: null };
  }

  let ics =
    "BEGIN:VCALENDAR\r\n" +
    "VERSION:2.0\r\n" +
    "PRODID:-//OrariScraper//IT\r\n" +
    "CALSCALE:GREGORIAN\r\n";

  lessons.forEach((l) => {
    const dtStart = toICSDate(l.year, l.month, l.day, l.startHour, l.startMin);
    const dtEnd = toICSDate(l.year, l.month, l.day, l.endHour, l.endMin);
    const location = [l.room, l.building].filter(Boolean).join(", ");

    const descriptionParts = [];
    if (l.teacher) descriptionParts.push(`Docente: ${escapeICSField(l.teacher)}`);
    if (l.note) descriptionParts.push(`Note: ${escapeICSField(l.note)}`);
    const description = descriptionParts.join("\\n");

    const eventUid = uid(l);

    ics +=
      "BEGIN:VEVENT\r\n" +
      `UID:${eventUid}\r\n` +
      `DTSTAMP:${toICSDate(l.year, l.month, l.day, l.startHour, l.startMin)}Z\r\n` +
      `DTSTART;TZID=Europe/Rome:${dtStart}\r\n` +
      `DTEND;TZID=Europe/Rome:${dtEnd}\r\n` +
      `SUMMARY:${escapeICSField(l.subject)}\r\n` +
      (location ? `LOCATION:${escapeICSField(location)}\r\n` : "") +
      (description ? `DESCRIPTION:${description}\r\n` : "") +
      "END:VEVENT\r\n";
  });

  ics += "END:VCALENDAR\r\n";

  return { error: null, count: lessons.length, excludedCount, ics };
}
