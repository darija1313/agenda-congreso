#!/usr/bin/env python3
"""
congreso_agenda_ics.py
======================
Descarga la agenda semanal del Congreso de los Diputados y genera un archivo .ics
para importar en cualquier calendario (Google Calendar, Outlook, Apple Calendar…).

Uso:
    python3 congreso_agenda_ics.py                  # semana actual
    python3 congreso_agenda_ics.py --semana 2026-04-07  # semana que contiene esa fecha
    python3 congreso_agenda_ics.py --output mi_agenda.ics

Dependencias:
    pip install playwright beautifulsoup4 lxml
    playwright install chromium
"""

import argparse
import hashlib
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# UTILIDADES ICS (sin dependencias externas)
# ─────────────────────────────────────────────────────────────────────────────

CRLF = "\r\n"

def ics_escape(text: str) -> str:
    """Escapa caracteres especiales según RFC 5545."""
    text = text.replace("\\", "\\\\")
    text = text.replace(";", "\\;")
    text = text.replace(",", "\\,")
    text = text.replace("\n", "\\n")
    return text

def ics_fold(line: str) -> str:
    """Pliega líneas largas a 75 octetos (RFC 5545 §3.1)."""
    if len(line.encode("utf-8")) <= 75:
        return line
    result = []
    current = ""
    for char in line:
        if len((current + char).encode("utf-8")) > 75:
            result.append(current)
            current = " " + char
        else:
            current += char
    result.append(current)
    return CRLF.join(result)

def make_uid(text: str) -> str:
    digest = hashlib.md5(text.encode()).hexdigest()
    return f"{digest}@congreso.es"

def format_dt(dt: datetime) -> str:
    """Formatea datetime a YYYYMMDDTHHMMSSZ (UTC)."""
    return dt.strftime("%Y%m%dT%H%M%SZ")

def build_ics(events: list[dict]) -> str:
    """
    Construye el contenido de un archivo ICS a partir de una lista de eventos.

    Cada evento es un dict con:
        - summary   (str)  título
        - dtstart   (datetime, aware UTC)
        - dtend     (datetime, aware UTC)
        - location  (str, opcional)  sala
        - description (str, opcional)  órgano / descripción
        - url       (str, opcional)
    """
    now_str = format_dt(datetime.now(tz=timezone.utc))
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//congreso_agenda_ics//ES",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:Agenda Congreso de los Diputados",
        f"X-WR-TIMEZONE:Europe/Madrid",
    ]

    for ev in events:
        uid = make_uid(ev["summary"] + str(ev["dtstart"]))
        lines += [
            "BEGIN:VEVENT",
            ics_fold(f"UID:{uid}"),
            ics_fold(f"DTSTAMP:{now_str}"),
            ics_fold(f"DTSTART:{format_dt(ev['dtstart'])}"),
            ics_fold(f"DTEND:{format_dt(ev['dtend'])}"),
            ics_fold(f"SUMMARY:{ics_escape(ev.get('summary', ''))}"),
        ]
        if ev.get("location"):
            lines.append(ics_fold(f"LOCATION:{ics_escape(ev['location'])}"))
        if ev.get("description"):
            lines.append(ics_fold(f"DESCRIPTION:{ics_escape(ev['description'])}"))
        if ev.get("url"):
            lines.append(ics_fold(f"URL:{ev['url']}"))
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return CRLF.join(lines) + CRLF


# ─────────────────────────────────────────────────────────────────────────────
# SCRAPING CON PLAYWRIGHT
# ─────────────────────────────────────────────────────────────────────────────

MESES_ES = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
}

# Zona horaria de Madrid (CET/CEST). Usamos UTC+1 (invierno) / UTC+2 (verano).
# Para mayor precisión instala: pip install pytz  o usa zoneinfo (Python 3.9+)
def madrid_offset(dt: datetime) -> int:
    """Devuelve el offset en horas de Europe/Madrid para una fecha dada."""
    try:
        from zoneinfo import ZoneInfo
        madrid = ZoneInfo("Europe/Madrid")
        aware = dt.replace(tzinfo=madrid)
        return int(aware.utcoffset().total_seconds() // 3600)
    except Exception:
        # Fallback: DST europeo simplificado
        # Último domingo de marzo → verano (UTC+2); último domingo de octubre → invierno (UTC+1)
        year = dt.year
        # Último domingo de marzo
        last_sun_march = max(
            d for d in (datetime(year, 3, d) for d in range(25, 32))
            if d.weekday() == 6
        )
        # Último domingo de octubre
        last_sun_oct = max(
            d for d in (datetime(year, 10, d) for d in range(25, 32))
            if d.weekday() == 6
        )
        if last_sun_march <= dt.replace(tzinfo=None) < last_sun_oct:
            return 2  # CEST
        return 1      # CET

def local_to_utc(dt_naive: datetime) -> datetime:
    offset_h = madrid_offset(dt_naive)
    return (dt_naive - timedelta(hours=offset_h)).replace(tzinfo=timezone.utc)

def parse_time(time_str: str, date: datetime) -> datetime | None:
    """Parsea 'HH:MM' junto con una fecha base y devuelve datetime UTC."""
    m = re.match(r"(\d{1,2})[:\.](\d{2})", time_str.strip())
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    naive = date.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return local_to_utc(naive)

def parse_date_from_header(header_text: str, year_hint: int) -> datetime | None:
    """
    Extrae la fecha de cabeceras como 'Martes, 8 de abril de 2026'
    o simplemente 'Martes 8'.
    """
    header_text = header_text.lower().strip()
    # Con año explícito
    m = re.search(r"(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})", header_text)
    if m:
        day, mes_str, year = int(m.group(1)), m.group(2), int(m.group(3))
        month = MESES_ES.get(mes_str)
        if month:
            return datetime(year, month, day)
    # Sin año
    m = re.search(r"(\d{1,2})\s+de\s+(\w+)", header_text)
    if m:
        day, mes_str = int(m.group(1)), m.group(2)
        month = MESES_ES.get(mes_str)
        if month:
            return datetime(year_hint, month, day)
    # Solo número
    m = re.search(r"\b(\d{1,2})\b", header_text)
    if m:
        return datetime(year_hint, datetime.now().month, int(m.group(1)))
    return None


def scrape_agenda(week_date: datetime | None = None) -> list[dict]:
    """
    Carga la vista de agenda completa del Congreso (todos los días en un solo HTML)
    y extrae todos los eventos de la semana.

    Estructura real de la página:
        <h3>Martes 7 de abril de 2026</h3>
        <table class="table-agenda">
          <tr>
            <td>11:30 h.</td>
            <td>
              <div>Descripción del acto</div>
              <div><em class="fas fa-map"></em> Sala Lázaro Dou</div>
            </td>
          </tr>
          ...
        </table>
        <h3>Miércoles 8 de abril de 2026</h3>
        <table class="table-agenda">...
    """
    from playwright.sync_api import sync_playwright
    from bs4 import BeautifulSoup

    if week_date is None:
        week_date = datetime.now()

    # URL que carga la agenda completa de la semana en un solo HTML
    monday = week_date - timedelta(days=week_date.weekday())
    url = (
        "https://www.congreso.es/es/agenda"
        "?p_p_id=agenda&p_p_lifecycle=0&p_p_state=normal&p_p_mode=view"
        f"&_agenda_mvcPath=agendaSemanal&_agenda_tipoagenda=1"
        f"&_agenda_dia={monday.day}&_agenda_mes={monday.month}&_agenda_anio={monday.year}"
    )

    print(f"[*] Cargando agenda semana del {monday.strftime('%d/%m/%Y')}…", file=sys.stderr)
    print(f"    URL: {url}", file=sys.stderr)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        )
        page.goto(url, wait_until="networkidle", timeout=60_000)
        html = page.content()
        browser.close()

    # Guardar para depuración
    Path("/tmp/congreso_agenda_debug.html").write_text(html, encoding="utf-8")

    soup = BeautifulSoup(html, "lxml")
    events = []
    year_hint = monday.year

    # La página tiene pares <h3>fecha</h3> + <table class="table-agenda">
    # Iteramos todos los h3 y leemos la tabla que viene justo después
    for h3 in soup.find_all("h3"):
        fecha_texto = h3.get_text(strip=True)
        fecha = parse_date_from_header(fecha_texto, year_hint)
        if not fecha:
            continue

        # Buscar la tabla inmediatamente siguiente al h3
        tabla = h3.find_next_sibling("table", class_="table-agenda")
        if not tabla:
            continue

        day_events = 0
        for fila in tabla.find_all("tr"):
            celdas = fila.find_all("td")
            if len(celdas) < 2:
                continue

            # Hora (primera celda): "11:30 h."
            hora_str = celdas[0].get_text(strip=True)

            # Segunda celda: descripción + sala
            celda_desc = celdas[1]

            # Extraer sala (div con icono fa-map) sin modificar el árbol original
            sala = ""
            for div in celda_desc.find_all("div"):
                if div.find("em", class_=re.compile(r"fa-map")):
                    # Clonar el div, quitar el icono y leer el texto
                    sala_texto = div.get_text(strip=True)
                    # El icono no deja texto, así que get_text ya da solo la sala
                    sala = sala_texto
                    break

            # Descripción: todo el texto de la celda menos el bloque de sala
            # Clonamos la celda para no mutilar el HTML
            celda_clon = BeautifulSoup(str(celda_desc), "lxml")
            for div in celda_clon.find_all("div"):
                if div.find("em", class_=re.compile(r"fa-map")):
                    div.decompose()
            descripcion = celda_clon.get_text(separator=" ", strip=True)
            descripcion = re.sub(r"\s+", " ", descripcion).strip()

            if not descripcion:
                continue

            # Ignorar filas que solo indican ausencia de actividad
            if re.search(r"sin convocatorias|no hay actos|día inhábil", descripcion, re.I):
                continue

            # Construir evento
            dtstart = parse_time(hora_str, fecha)
            if dtstart:
                dtend = dtstart + timedelta(hours=2)
            else:
                dtstart = local_to_utc(fecha.replace(hour=9, minute=0, second=0))
                dtend   = local_to_utc(fecha.replace(hour=21, minute=0, second=0))

            # Título: primera frase, máx 120 caracteres
            titulo = re.split(r"[.·\n]", descripcion)[0].strip()[:120]

            events.append({
                "summary":     titulo,
                "dtstart":     dtstart,
                "dtend":       dtend,
                "location":    sala,
                "description": f"{descripcion}\nSala: {sala}\nFuente: congreso.es",
                "url":         "https://www.congreso.es/es/agenda",
            })
            day_events += 1

        print(f"    {fecha.strftime('%a %d/%m')}: {day_events} evento(s)", file=sys.stderr)

    print(f"[*] Total: {len(events)} evento(s) extraído(s).", file=sys.stderr)
    return events


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Genera un .ics con la agenda parlamentaria del Congreso."
    )
    parser.add_argument(
        "--semana",
        help="Fecha dentro de la semana deseada (YYYY-MM-DD). Por defecto: semana actual.",
        default=None,
    )
    parser.add_argument(
        "--output",
        help="Ruta del archivo .ics de salida.",
        default=None,
    )
    args = parser.parse_args()

    # Determinar la semana
    if args.semana:
        try:
            week_date = datetime.strptime(args.semana, "%Y-%m-%d")
        except ValueError:
            print("Error: --semana debe tener formato YYYY-MM-DD", file=sys.stderr)
            sys.exit(1)
    else:
        # Por defecto: semana siguiente (útil para ejecutar domingos o lunes)
        week_date = datetime.now() + timedelta(weeks=1)

    # Nombre de salida por defecto: agenda_YYYY-Wnn.ics
    iso_week = week_date.isocalendar()
    default_name = f"agenda_congreso_{iso_week[0]}-W{iso_week[1]:02d}.ics"
    output_path = Path(args.output or default_name)

    # Scraping
    events = scrape_agenda(week_date)

    if not events:
        print(
            "\n⚠️  No se encontraron eventos.\n"
            "   Posibles causas:\n"
            "   1. La web del Congreso cambió su estructura HTML.\n"
            "   2. Es semana sin actividad parlamentaria.\n"
            "   3. El contenido aún no ha sido publicado.\n"
            "\n   Revisa /tmp/congreso_agenda_debug.html para inspeccionar el HTML descargado.\n"
            "   Ajusta las funciones extract_events_from_block() o el fallback de regex\n"
            "   en congreso_agenda_ics.py para adaptarte a la nueva estructura.\n",
            file=sys.stderr,
        )
        # Generar ICS vacío igualmente para no romper flujos automatizados
        content = build_ics([])
        output_path.write_text(content, encoding="utf-8")
        print(f"[*] ICS vacío guardado en: {output_path}", file=sys.stderr)
        sys.exit(0)

    # Ordenar por fecha
    events.sort(key=lambda e: e["dtstart"])

    # Construir y guardar ICS
    ics_content = build_ics(events)
    output_path.write_text(ics_content, encoding="utf-8")

    print(f"\n✅  Archivo generado: {output_path}")
    print(f"    Eventos incluidos: {len(events)}")
    for ev in events:
        hora_local = ev["dtstart"] + timedelta(hours=madrid_offset(ev["dtstart"].replace(tzinfo=None)))
        print(f"    • {hora_local.strftime('%a %d/%m %H:%M')}  {ev['summary'][:60]}")


if __name__ == "__main__":
    main()
