"""
Alerta de estadísticas entre el minuto 30 y 44 del primer tiempo -> Telegram (GitHub Actions)

Cada ejecución es CORTA (sin esperas): consulta ESPN, mira qué partidos están en vivo,
envía las estadísticas de los que van entre el minuto 30 y el 44 del 1er tiempo y que
todavía no se hayan enviado, y termina. Se dispara cada pocos minutos (cron-job.org
y, de respaldo, el cron de GitHub), así que si una ejecución falla o se salta, la
siguiente cubre el partido.

Estado: 'enviados.json' (en el repositorio) recuerda qué partidos ya se enviaron.

Secretos (Settings > Secrets and variables > Actions):
    TELEGRAM_BOT_TOKEN   token del bot (BotFather)
    TELEGRAM_CHAT_ID     id del chat/grupo/canal

Uso manual:
    python alerta_30min.py             # ejecución normal
    python alerta_30min.py --probar    # solo envía un mensaje de prueba
    python alerta_30min.py --simular   # no envía ni guarda: imprime lo que haría
"""
import argparse
import json
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
MINUTO_OBJETIVO = 30        # desde este minuto del 1er tiempo se envía
MINUTO_MAX_ENVIO = 44       # pasado este minuto ya no se envía (alerta tardía)
HORA_AGENDA = (6, 9)        # la agenda del día se envía en la primera ejecución entre las 06:00 y las 08:59
DIAS_ESTADO = 3             # días que se conserva el historial de enviados
HILOS = 6

BASE = 'https://site.api.espn.com/apis/site/v2/sports/soccer'
ESTADO = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'enviados.json')
SIMULAR = False

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


def marcador(c):
    s = c.get('score')
    s = s.get('displayValue') if isinstance(s, dict) else s
    return s if s not in (None, '') else '0'


def minuto_de(reloj):
    m = re.match(r'\d+', reloj or '')
    return int(m.group()) if m else None


# ------------------------------- Estado -------------------------------

def cargar_estado():
    try:
        with open(ESTADO, encoding='utf-8') as f:
            e = json.load(f)
    except (OSError, ValueError):
        e = {}
    e.setdefault('partidos', {})
    e.setdefault('agenda', '')
    return e


def guardar_estado(e):
    limite = (datetime.now(TZ_LOCAL) - timedelta(days=DIAS_ESTADO)).isoformat()
    e['partidos'] = {k: v for k, v in e['partidos'].items() if v >= limite}
    with open(ESTADO, 'w', encoding='utf-8') as f:
        json.dump(e, f, indent=1, sort_keys=True)


# ------------------------------- Telegram -------------------------------

def esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def enviar_telegram(texto, html=False):
    if SIMULAR:
        print('--- (simulado) mensaje a Telegram ---')
        print(texto)
        print('--- fin ---')
        return True

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


# ------------------------------ Agenda del día ------------------------------

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


# ------------------------------ Partidos en vivo ------------------------------

def en_vivo_liga(codigo, nombre):
    """Partidos de la liga que están en juego ahora (state == 'in').

    Se consultan por fecha (ayer y hoy en UTC) porque el marcador sin fecha de las
    selecciones/copas puede mostrar otra jornada.
    """
    hoy_utc = datetime.now(timezone.utc).date()
    vivos = {}
    for delta in (-1, 0):
        dia = hoy_utc + timedelta(days=delta)
        data = get_json(f'{BASE}/{codigo}/scoreboard?dates={dia:%Y%m%d}')
        for e in (data or {}).get('events', []):
            comp = e['competitions'][0]
            status = comp.get('status') or e.get('status') or {}
            if (status.get('type') or {}).get('state') != 'in':
                continue
            local = next((c for c in comp['competitors'] if c.get('homeAway') == 'home'), None)
            visita = next((c for c in comp['competitors'] if c.get('homeAway') == 'away'), None)
            if not local or not visita:
                continue
            reloj = status.get('displayClock') or ''
            vivos[e['id']] = {
                'id': e['id'],
                'cod': codigo,
                'liga': nombre,
                'periodo': status.get('period') or 1,
                'reloj': reloj,
                'minuto': minuto_de(reloj),
                'id_l': local['team']['id'],
                'id_v': visita['team']['id'],
                'local': local['team']['displayName'],
                'visita': visita['team']['displayName'],
                'g_l': marcador(local),
                'g_v': marcador(visita),
            }
    return list(vivos.values())


def toca_enviar(p):
    return (p['periodo'] == 1 and p['minuto'] is not None
            and MINUTO_OBJETIVO <= p['minuto'] <= MINUTO_MAX_ENVIO)


def enviar_estadisticas(p):
    """Trae las estadísticas del partido y las envía. True si se envió."""
    data = get_json(f"{BASE}/{p['cod']}/summary?event={p['id']}")
    if not data:
        return False

    stats, orden = {}, []
    for t in (data.get('boxscore') or {}).get('teams', []):
        for s in t.get('statistics', []):
            clave = s.get('name') or s.get('label')
            if clave not in stats:
                stats[clave] = {'label': TRAD.get(s.get('name')) or s.get('label') or s.get('name'), 'v': {}}
                orden.append(clave)
            stats[clave]['v'][t['team']['id']] = s.get('displayValue')

    cabecera = (f"⏱ {p['reloj']} · {p['liga']}\n"
                f"⚽ {p['local']} {p['g_l']} - {p['g_v']} {p['visita']}\n")

    if not orden:
        return enviar_telegram(esc(cabecera) + '\nSin estadísticas disponibles todavía.', html=True)

    tabla = ''
    for k in orden:
        e = stats[k]
        tabla += f"{str(e['v'].get(p['id_l'], '-')):>6}  {e['label']:<17}  {e['v'].get(p['id_v'], '-')}\n"
    msg = (f"{esc(cabecera)}\n<pre>{esc(tabla)}</pre>\n"
           f"{esc('Local: ' + p['local'] + ' | Visita: ' + p['visita'])}")
    return enviar_telegram(msg, html=True)


# ------------------------------------ Main ------------------------------------

def main():
    global SIMULAR
    ap = argparse.ArgumentParser()
    ap.add_argument('--probar', action='store_true', help='envía un mensaje de prueba y termina')
    ap.add_argument('--simular', action='store_true', help='no envía ni guarda; imprime lo que haría')
    args = ap.parse_args()
    SIMULAR = args.simular

    if args.probar:
        ok = enviar_telegram('✅ Prueba desde GitHub Actions: el bot está conectado.')
        sys.exit(0 if ok else 1)

    estado = cargar_estado()
    cambio = False
    ahora = datetime.now(TZ_LOCAL)
    hoy = ahora.date()

    # 1) Agenda del día (una sola vez, en la primera ejecución de la mañana)
    if HORA_AGENDA[0] <= ahora.hour < HORA_AGENDA[1] and estado['agenda'] != str(hoy):
        todos = partidos_del_dia(hoy)
        msg = f'📅 Partidos de hoy ({hoy}): {len(todos)}\n'
        if not todos:
            msg += 'No hay partidos en las ligas configuradas.'
        for p in todos:
            msg += f"\n{p['inicio']:%H:%M} · {p['liga']}: {p['local']} vs {p['visita']}"
        if enviar_telegram(msg):
            estado['agenda'] = str(hoy)
            cambio = True
            log(f'Agenda enviada ({len(todos)} partidos)')

    # 2) Partidos en vivo que ya van entre el minuto 30 y el 44
    with ThreadPoolExecutor(max_workers=HILOS) as ex:
        res = list(ex.map(lambda x: en_vivo_liga(*x), LIGAS.items()))
    vivos = [p for lista in res for p in lista]
    log(f'{len(vivos)} partidos en vivo')
    for p in vivos:
        pendiente = p['id'] not in estado['partidos']
        log(f"  {p['local']} vs {p['visita']} · {p['reloj']} (t{p['periodo']})"
            f"{' · ya enviado' if not pendiente else ''}")

    for p in vivos:
        if p['id'] in estado['partidos'] or not toca_enviar(p):
            continue
        if enviar_estadisticas(p):
            estado['partidos'][p['id']] = ahora.isoformat()
            cambio = True
            log(f"  enviado: {p['local']} vs {p['visita']}")
        else:
            log(f"  no se pudo enviar {p['local']} vs {p['visita']}; se reintenta en la próxima ejecución")

    if cambio and not SIMULAR:
        guardar_estado(estado)
    log('Listo')


if __name__ == '__main__':
    main()
