"""
Alerta de estadísticas al minuto 30 de cada partido -> Telegram (para GitHub Actions)

Variables de entorno (en GitHub: Settings > Secrets and variables > Actions):
    TELEGRAM_BOT_TOKEN   token del bot (BotFather)
    TELEGRAM_CHAT_ID     id del chat/grupo/canal

Se ejecuta cada hora (minuto :50) y se encarga de los partidos cuyo inicio (hora Bogotá)
cae en la hora siguiente, p. ej. la ejecución de las 10:50 atiende los partidos de 11:00 a 11:59.
Espera hasta que cada partido llegue al minuto 30 para enviar sus estadísticas.
La ejecución de la ventana de las 06:00 además envía la agenda completa del día.

Uso manual:
    python alerta_30min.py            # ventana según la hora actual
    python alerta_30min.py --probar   # solo envía un mensaje de prueba
"""
import argparse
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

LIGAS = {
    'col.1': 'Liga BetPlay',
    'eng.1': 'Premier League',
    'esp.1': 'LaLiga',
    'uefa.nations': 'Uefa Nationals League',
    'ger.1': 'Bundesliga Alemania',
    'ita.1': 'Italia Seria A',
    'fra.1': 'Francia Liga 1',
    'uefa.champions': 'UEFA Champions League',
    'ned.1': 'Eredivisie Paises Bajos',
    'por.1': 'Portugal Liga 1',
    'bra.1': 'Brasil Seria A',
    'usa.1': 'Usa Mls',
}

TZ_LOCAL = timezone(timedelta(hours=-5))      # Colombia: UTC-5 fijo
VENTANAS = [(h, h + 1) for h in range(24)]    # una ventana por hora
ADELANTO_MIN = 15         # la ejecución de las HH:50 ya atiende la ventana de la hora siguiente
HORA_AGENDA = 6           # ventana en la que se envía la agenda del día

MINUTO_OBJETIVO = 30      # minuto de juego para enviar
MINUTO_MAX_ENVIO = 40     # si ya pasó de este minuto, no se envía (alerta tardía)
ANTICIPO_MIN = 3          # empieza a consultar 3 min antes de kickoff+30
ABANDONAR_MIN = 100       # minutos desde el inicio programado tras los cuales se abandona
HILOS = 6

BASE = 'https://site.api.espn.com/apis/site/v2/sports/soccer'

TRAD = {
    'possessionPct': 'Posesión %',
    'totalShots': 'Tiros',
    'shotsOnTarget': 'Tiros al arco',
    'blockedShots': 'Tiros bloqueados',
    'wonCorners': 'Córners',
    'foulsCommitted': 'Faltas',
    'yellowCards': 'Amarillas',
    'redCards': 'Rojas',
    'offsides': 'Fuera de juego',
    'saves': 'Atajadas',
    'accuratePasses': 'Pases precisos',
    'totalPasses': 'Pases',
    'passPct': 'Precisión pase %',
    'totalCrosses': 'Centros',
    'totalTackles': 'Entradas',
    'interceptions': 'Intercepciones',
}

session = requests.Session()
session.headers.update({
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/124.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Referer': 'https://www.espn.com/',
})


def log(msg):
    print(f"[{datetime.now(TZ_LOCAL):%H:%M:%S}] {msg}", flush=True)


def get_json(url, intentos=3):
    for i in range(intentos):
        try:
            r = session.get(url, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(3 * (i + 1))
                continue
            return None
        except (requests.RequestException, ValueError) as e:
            log(f'  error de red ({type(e).__name__})')
            time.sleep(2 * (i + 1))
    return None


# ------------------------------- Telegram -------------------------------

def esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def enviar_telegram(texto, html=False):
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat:
        log('Faltan los secretos TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID')
        sys.exit(1)

    partes, actual = [], ''
    for linea in texto.split('\n'):
        if len(actual) + len(linea) + 1 > 3800:
            partes.append(actual)
            actual = ''
        actual += linea + '\n'
    if actual:
        partes.append(actual)

    ok = True
    for parte in partes:
        payload = {'chat_id': chat, 'text': parte}
        if html:
            payload['parse_mode'] = 'HTML'
        try:
            r = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                              json=payload, timeout=20)
            if r.status_code != 200:
                ok = False
                log(f'Telegram respondió {r.status_code}: {r.text[:200]}')
        except requests.RequestException as e:
            # No se imprime el error completo: podría incluir la URL con el token
            ok = False
            log(f'Error enviando a Telegram ({type(e).__name__})')
    return ok


# ------------------------------ Partidos de hoy ------------------------------

def a_hora_local(fecha_iso):
    dt = datetime.strptime(fecha_iso, '%Y-%m-%dT%H:%MZ').replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ_LOCAL)


def partidos_liga(codigo, nombre, hoy):
    partidos = {}
    for delta in (-1, 0, 1):
        dia = hoy + timedelta(days=delta)
        data = get_json(f'{BASE}/{codigo}/scoreboard?dates={dia:%Y%m%d}')
        if not data:
            continue
        for e in data.get('events', []):
            if e['id'] in partidos:
                continue
            try:
                inicio = a_hora_local(e['date'])
            except ValueError:
                continue
            if inicio.date() != hoy:
                continue
            comp = e['competitions'][0]
            local = next((c for c in comp['competitors'] if c.get('homeAway') == 'home'), None)
            visita = next((c for c in comp['competitors'] if c.get('homeAway') == 'away'), None)
            if not local or not visita:
                continue
            partidos[e['id']] = {
                'id': e['id'],
                'cod': codigo,
                'liga': nombre,
                'inicio': inicio,
                'local': local['team']['displayName'],
                'visita': visita['team']['displayName'],
            }
    return list(partidos.values())


def partidos_del_dia(hoy):
    with ThreadPoolExecutor(max_workers=HILOS) as ex:
        res = list(ex.map(lambda x: partidos_liga(x[0], x[1], hoy), LIGAS.items()))
    todos = [p for lista in res for p in lista]
    todos.sort(key=lambda p: p['inicio'])
    return todos


# ------------------------------ Minuto 30 ------------------------------

def procesar(p):
    """Devuelve 'enviado', 'descartar' o 'esperar'."""
    data = get_json(f"{BASE}/{p['cod']}/summary?event={p['id']}")
    if not data:
        return 'esperar'
    comps = (data.get('header') or {}).get('competitions') or []
    if not comps:
        return 'esperar'
    comp = comps[0]
    status = comp.get('status') or {}
    estado = (status.get('type') or {}).get('state')

    if estado == 'post':
        return 'descartar'
    if estado != 'in':
        return 'esperar'

    periodo = status.get('period') or 1
    m = re.match(r'\d+', status.get('displayClock') or '')
    minuto = int(m.group()) if m else None

    if periodo > 1 or (minuto is not None and minuto > MINUTO_MAX_ENVIO):
        log(f"  {p['local']} vs {p['visita']}: ya pasó el minuto de envío, se omite")
        return 'descartar'
    if minuto is None or minuto < MINUTO_OBJETIVO:
        return 'esperar'

    local = next(c for c in comp['competitors'] if c.get('homeAway') == 'home')
    visita = next(c for c in comp['competitors'] if c.get('homeAway') == 'away')
    id_l, id_v = local['team']['id'], visita['team']['id']

    stats, orden = {}, []
    for t in (data.get('boxscore') or {}).get('teams', []):
        for s in t.get('statistics', []):
            clave = s.get('name') or s.get('label')
            if clave not in stats:
                stats[clave] = {'label': TRAD.get(s.get('name')) or s.get('label') or s.get('name'), 'v': {}}
                orden.append(clave)
            stats[clave]['v'][t['team']['id']] = s.get('displayValue')

    cabecera = (f"⏱ {status.get('displayClock', '')} · {p['liga']}\n"
                f"⚽ {local['team']['displayName']} {local.get('score', '0')} - "
                f"{visita.get('score', '0')} {visita['team']['displayName']}\n")

    if not orden:
        ok = enviar_telegram(esc(cabecera) + '\nSin estadísticas disponibles todavía.', html=True)
        return 'enviado' if ok else 'esperar'

    tabla = ''
    for k in orden:
        e = stats[k]
        tabla += f"{str(e['v'].get(id_l, '-')):>6}  {e['label']:<17}  {e['v'].get(id_v, '-')}\n"
    msg = (f"{esc(cabecera)}\n<pre>{esc(tabla)}</pre>\n"
           f"{esc('Local: ' + local['team']['displayName'] + ' | Visita: ' + visita['team']['displayName'])}")
    return 'enviado' if enviar_telegram(msg, html=True) else 'esperar'


# ------------------------------------ Main ------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--probar', action='store_true', help='envía un mensaje de prueba y termina')
    args = ap.parse_args()

    if args.probar:
        ok = enviar_telegram('✅ Prueba desde GitHub Actions: el bot está conectado.')
        sys.exit(0 if ok else 1)

    ref = datetime.now(TZ_LOCAL) + timedelta(minutes=ADELANTO_MIN)
    hoy = ref.date()
    ini, fin = next((v for v in VENTANAS if v[0] <= ref.hour < v[1]), VENTANAS[0])
    log(f'Fecha {hoy} · ventana {ini:02d}:00-{fin:02d}:00 (hora Bogotá)')

    todos = partidos_del_dia(hoy)
    log(f'{len(todos)} partidos hoy en total')

    if ini == HORA_AGENDA:   # agenda completa del día
        msg = f'📅 Partidos de hoy ({hoy}): {len(todos)}\n'
        if not todos:
            msg += 'No hay partidos en las ligas configuradas.'
        for p in todos:
            msg += f"\n{p['inicio']:%H:%M} · {p['liga']}: {p['local']} vs {p['visita']}"
        enviar_telegram(msg)

    pendientes = [p for p in todos if ini <= p['inicio'].hour < fin]
    log(f'{len(pendientes)} partidos en esta ventana')

    while pendientes:
        ahora = datetime.now(TZ_LOCAL)
        siguientes = []
        activos = False
        for p in pendientes:
            desde_inicio = (ahora - p['inicio']).total_seconds() / 60
            if desde_inicio < MINUTO_OBJETIVO - ANTICIPO_MIN:
                siguientes.append(p)
                continue
            if desde_inicio > ABANDONAR_MIN:
                log(f"  se abandona {p['local']} vs {p['visita']} (sin datos a tiempo)")
                continue
            activos = True
            res = procesar(p)
            if res == 'enviado':
                log(f"  enviado: {p['local']} vs {p['visita']}")
            elif res == 'esperar':
                siguientes.append(p)
        pendientes = siguientes
        if not pendientes:
            break

        if activos:
            time.sleep(60)
        else:
            primero = min(p['inicio'] for p in pendientes) + timedelta(minutes=MINUTO_OBJETIVO - ANTICIPO_MIN)
            espera = (primero - datetime.now(TZ_LOCAL)).total_seconds()
            time.sleep(max(15, min(300, espera)))

    log('Ventana terminada')


if __name__ == '__main__':
    main()
