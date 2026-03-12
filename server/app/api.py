import importlib
import time
from collections import defaultdict
from datetime import datetime
import json

import redis
import redis.exceptions
from flask import request, jsonify, Blueprint
from prometheus_client import Counter, Gauge

import auth
import reloader
from database import db_cursor
from models import FlagStatus

import redis
import logging
import json
import requests

def get_scoreboard_team_order(tick=None, all_teams=None):
    """
    Recupera l'ordine dei team dalla scoreboard cachata in Redis.
    
    Args:
        tick: numero del tick (non utilizzato, ma mantenuto per compatibilità)
        all_teams: lista di tutti i team validi dal DB
        
    Returns:
        Lista ordinata dei team secondo la scoreboard
    """
    if not all_teams:
        return []
    
    team_order = all_teams[:]  # default: alfabetico
    
    try:
        # Leggi i dati della scoreboard da Redis
        r = redis.Redis(host='localhost', port=6379, db=1)
        scoreboard_data = r.get('scoreboard_data')
        
        if scoreboard_data:
            data = json.loads(scoreboard_data)
            logging.info(f"Scoreboard data letta da Redis: {len(data.get('teams', []))} team")
            
            # Estrai e ordina i team secondo l'ordine già presente nei dati
            # I dati da fetch_scoreboard.py sono già ordinati per punteggio
            ordered = []
            for team_data in data.get('teams', []):
                team_name = team_data.get('name')  # fetch_scoreboard.py usa 'name'
                if team_name and team_name in all_teams:
                    ordered.append(team_name)
                    logging.info(f"Team {team_name} aggiunto all'ordine con score {team_data.get('score', 0)}")
            
            # Aggiungi i team che non sono nella scoreboard alla fine
            team_order = ordered + [t for t in all_teams if t not in ordered]
            
            logging.info(f"Ordine finale dei team da Redis: {team_order}")
        else:
            logging.warning("Nessun dato scoreboard trovato in Redis, uso ordine alfabetico")
            
    except redis.exceptions.ConnectionError as e:
        logging.warning(f"Errore connessione Redis: {e}")
    except (json.JSONDecodeError, KeyError) as e:
        logging.warning(f"Errore nel parsing dei dati scoreboard da Redis: {e}")
    except Exception as e:
        logging.error(f"Errore generico nel recupero scoreboard da Redis: {e}")
    
    return team_order

def is_valid_team(team):
    """
    Verifica se un team è valido per le statistiche
    Esclude team non validi come 'nop', server team, e dati malformati
    """
    if not team or not team.strip():
        return False
    
    team = team.strip()
    
    # Lista di team da escludere
    invalid_teams = ['*', 'nop', '']
    if team in invalid_teams:
        return False
    
    # Esclude server team (tipicamente 10.60.0.x)
    if team.startswith('10.60.0.'):
        return False
    
    # Esclude team che contengono 'nop' nel nome
    if 'nop' in team.lower():
        return False
    
    return True

api = Blueprint('api', __name__, url_prefix='/api')

FLAGS_RECEIVED = Counter(
    'flags_received',
    'Number of flags received',
    ['sploit', 'team'],
)

TOTAL_TEAMS = Gauge('total_teams', 'Number of teams')
TOTAL_TEAMS.set_function(lambda: len(reloader.get_config()['TEAMS']))


@api.route('/get_config')
@auth.auth_required
def get_config():
    config = reloader.get_config()
    return jsonify({
        key: value
        for key, value in config.items()
        if 'PASSWORD' not in key and 'TOKEN' not in key
    })


@api.route('/post_flags', methods=['POST'])
@auth.auth_required
def post_flags():
    flags = request.json
    cur_time = round(time.time())
    config = reloader.get_config()

    if config.get('SYSTEM_VALIDATOR'):
        validator_module = importlib.import_module('validators.' + config['SYSTEM_VALIDATOR'])
        flags = validator_module.validate_flags(flags, config)

    rows = [
        {
            'flag': flag['flag'],
            'sploit': flag['sploit'],
            'team': flag['team'],
            'time': cur_time,
            'status': FlagStatus.QUEUED.name,
        }
        for flag in flags
    ]

    with db_cursor() as (conn, curs):
        curs.executemany(
            """
            INSERT INTO flags (flag, sploit, team, time, status)
            VALUES (%(flag)s, %(sploit)s, %(team)s, %(time)s, %(status)s)
            ON CONFLICT DO NOTHING
            """,
            rows,
        )
        conn.commit()

    for flag in flags:
        FLAGS_RECEIVED.labels(sploit=flag['sploit'], team=flag['team']).inc()

    return ''


@api.route('/filter_flags', methods=['GET'])
@auth.auth_required
def get_filtered_flags():
    filters = request.args

    conditions = []
    for column in ['sploit', 'status', 'team']:
        value = filters.get(column)
        if value:
            conditions.append((f'{column} = %s', value))

    for column in ['flag', 'checksystem_response']:
        value = filters.get(column)
        if value:
            conditions.append((f'POSITION(%s in LOWER({column})) > 0', value.lower()))

    for column in ['since', 'until']:
        value = filters.get(column, '').strip()
        if value:
            timestamp = round(datetime.strptime(value, '%Y-%m-%d %H:%M').timestamp())
            sign = '>=' if column == 'since' else '<='
            conditions.append((f'time {sign} %s', timestamp))

    page = int(filters.get('page', 1))
    if page < 1:
        raise ValueError('Invalid page')

    page_size = int(filters.get('page_size', 30))
    if page_size < 1 or page_size > 100:
        raise ValueError('Invalid page size')

    if conditions:
        chunks, values = list(zip(*conditions))
        conditions_sql = 'WHERE ' + ' AND '.join(chunks)
        conditions_args = list(values)
    else:
        conditions_sql = ''
        conditions_args = []

    sql = 'SELECT * FROM flags ' + conditions_sql + ' ORDER BY time DESC LIMIT %s OFFSET %s'
    args = conditions_args + [page_size, page_size * (page - 1)]

    count_sql = 'SELECT COUNT(*) as cnt FROM flags ' + conditions_sql
    count_args = conditions_args

    with db_cursor(True) as (_, curs):
        curs.execute(sql, args)
        flags = curs.fetchall()
        curs.execute(count_sql, count_args)
        total_count = curs.fetchone()['cnt']

    response = {
        'flags': list(map(dict, flags)),
        'page_size': page_size,
        'page': page,
        'total': total_count,
    }

    return jsonify(response)


@api.route('/filter_config', methods=['GET'])
@auth.auth_required
def get_filter_config():
    distinct_values = {}
    with db_cursor(True) as (_, curs):
        for column in ['sploit', 'status', 'team']:
            curs.execute(f'SELECT DISTINCT {column} FROM flags ORDER BY {column}')
            rows = curs.fetchall()
            distinct_values[column] = [item[column] for item in rows]

    config = reloader.get_config()

    server_tz_name = time.strftime('%Z')
    if server_tz_name.startswith('+'):
        server_tz_name = 'UTC' + server_tz_name

    response = {
        'filters': distinct_values,
        'flag_format': config['FLAG_FORMAT'],
        'server_tz': server_tz_name
    }

    return jsonify(response)

@api.route('/summary', methods=['GET'])
@auth.auth_required
def summary():
    filters = request.args

    conditions = []
    for column in ['sploit', 'status', 'team']:
        value = filters.get(column)
        if value:
            conditions.append((f'{column} = %s', value))

    for column in ['flag', 'checksystem_response']:
        value = filters.get(column)
        if value:
            conditions.append((f'POSITION(%s in LOWER({column})) > 0', value.lower()))

    for column in ['since', 'until']:
        value = filters.get(column, '').strip()
        if value:
            timestamp = round(datetime.strptime(value, '%Y-%m-%d %H:%M').timestamp())
            sign = '>=' if column == 'since' else '<='
            conditions.append((f'time {sign} %s', timestamp))

    # result for last x seconds
    for column in ['last']:
        value = filters.get(column, '').strip()
        if value:
            timestamp = round(datetime.strptime(datetime.timestamp()-value, '%Y-%m-%d %H:%M').timestamp())
            sign = '>='
            conditions.append((f'time {sign} %s', timestamp))

    if conditions:
        chunks, values = list(zip(*conditions))
        conditions_sql = 'WHERE ' + ' AND '.join(chunks)
        conditions_args = list(values)
    else:
        conditions_sql = ''
        conditions_args = []

    sql = 'SELECT * FROM flags ' + conditions_sql
    args = conditions_args

    with db_cursor(True) as (_, curs):
        curs.execute(sql, args)
        flags = curs.fetchall()

    result = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    response = filters.get('response','')
    for item in flags:
        key = item['checksystem_response'][:64] if response else item['status']
        result[item['sploit']][item['team']][key] += 1

    teams = reloader.get_config()['TEAMS']
    sploits=[]
    for sploit in result:
        tmp={}
        tmp['sploit_name'] = sploit
        tmp['teams']=[]
        for team in teams:
            if team not in result[sploit]:
                tmp['teams'].append({
                    'team': team,
                    'team_status': 'not status'
                })
        for team in result[sploit]:
            output = ''
            for key, value in result[sploit][team].items():
                output += f'{key}: {value}\n'
            tmp['teams'].append({
                'team': team,
                'team_status': output
            })
        sploits.append(tmp)

    return jsonify(sploits)

@api.route('/teams', methods=['GET'])
@auth.auth_required
def get_teams():
    teams = reloader.get_config()['TEAMS']
    response = list(map(
        lambda x: {'name': x[0], 'address': x[1]},
        teams.items(),
    ))
    return jsonify(response)


@api.route('/team_stats', methods=['GET'])
@auth.auth_required
def get_team_stats():
    """
    Endpoint per ottenere le statistiche delle flag per team e servizio
    Restituisce l'andamento delle flag accettate per ogni team divise per servizio (sploit)
    """
    config = reloader.get_config()
    tick_duration = config.get('TICK_DURATION', 120)  # durata in secondi di un tick
    start_time = config.get('START_TIME', round(time.time()))
    
    # Ottieni il tick corrente o da parametro
    current_tick = request.args.get('tick', type=int)
    if current_tick is None:
        current_time = round(time.time())
        # Calcola il tick corrente basato sul tempo di inizio della CTF
        current_tick = max(1, (current_time - start_time) // tick_duration + 1)
    
    # Calcola i tempi di inizio e fine del tick
    tick_start_time = start_time + (current_tick - 1) * tick_duration
    tick_end_time = start_time + current_tick * tick_duration
    
    with db_cursor(True) as (_, curs):
        # Query per ottenere le statistiche delle flag accettate per team e servizio per il tick specificato
        curs.execute("""
            SELECT 
                team,
                sploit,
                COUNT(*) as accepted_flags,
                MIN(time) as first_flag_time,
                MAX(time) as last_flag_time
            FROM flags 
            WHERE status = %s 
                AND time >= %s 
                AND time < %s
            GROUP BY team, sploit
            ORDER BY team, sploit
        """, (
            FlagStatus.ACCEPTED.name,
            tick_start_time,
            tick_end_time
        ))
        
        flag_stats = curs.fetchall()
        
        # Query per ottenere tutti i team e servizi distinti (filtra dati malformati e team non validi)
        curs.execute("""
            SELECT DISTINCT sploit 
            FROM flags 
            WHERE sploit IS NOT NULL AND sploit != '' 
            ORDER BY sploit
        """)
        all_services = [row['sploit'] for row in curs.fetchall()]
        
        curs.execute("""
            SELECT DISTINCT team 
            FROM flags 
            WHERE team IS NOT NULL AND team != ''
            ORDER BY team
        """)
        all_teams = [row['team'] for row in curs.fetchall() if is_valid_team(row['team'])]
    
    # Organizza i dati in una struttura più facilmente utilizzabile dal frontend
    stats_matrix = {}
    team_totals = defaultdict(int)
    
    # Inizializza la matrice con zeri
    for team in all_teams:
        stats_matrix[team] = {}
        for service in all_services:
            stats_matrix[team][service] = 0
    
    # Popola la matrice con i dati reali (filtra team non validi)
    for stat in flag_stats:
        team = stat['team']
        service = stat['sploit']
        count = stat['accepted_flags']
        
        # Filtra team non validi e assicurati che il team sia nella lista valida
        if is_valid_team(team) and team in all_teams:
            if team in stats_matrix and service in stats_matrix[team]:
                stats_matrix[team][service] = count
                team_totals[team] += count
    
    # Ottieni l'ordine dei team dalla scoreboard
    team_order = get_scoreboard_team_order(current_tick, all_teams)
    
    response = {
        'tick': current_tick,
        'tick_duration': tick_duration,
        'teams': team_order,
        'services': all_services,
        'stats_matrix': stats_matrix,
        'team_totals': dict(team_totals),
        'start_time': start_time,
        'tick_start_time': tick_start_time,
        'tick_end_time': tick_end_time
    }
    
    return jsonify(response)

@api.route("/team_stats_overall", methods=["GET"])
@auth.auth_required
def get_team_stats_overall():
    """
    Statistiche complessive (tutti i tick) delle flag per team/servizio.
    I team sono ordinati secondo la scoreboard.
    """

    # 1. ---------- Query DB ----------
    with db_cursor(True) as (_, curs):
        # Flag accettate per team/servizio
        curs.execute("""
            SELECT team, sploit, COUNT(*) AS accepted_flags
            FROM flags
            WHERE status = %s
            GROUP BY team, sploit
        """, (FlagStatus.ACCEPTED.name,))
        flag_stats = curs.fetchall()

        # Elenco servizi
        curs.execute("""
            SELECT DISTINCT sploit
            FROM flags
            WHERE sploit <> '' AND sploit IS NOT NULL
            ORDER BY sploit
        """)
        all_services = [r["sploit"] for r in curs.fetchall()]

        # Elenco team validi
        curs.execute("""
            SELECT DISTINCT team
            FROM flags
            WHERE team <> '' AND team IS NOT NULL
            ORDER BY team
        """)
        all_teams = [r["team"] for r in curs.fetchall() if is_valid_team(r["team"])]

    # 2. ---------- Costruzione matrice ----------
    stats_matrix = {t: {s: 0 for s in all_services} for t in all_teams}
    team_totals   = defaultdict(int)

    for row in flag_stats:
        team, service, count = row["team"], row["sploit"], row["accepted_flags"]
        if is_valid_team(team):
            stats_matrix[team][service] = count
            team_totals[team]          += count

    # 3. ---------- Ordine team dalla scoreboard ----------
    team_order = get_scoreboard_team_order(None, all_teams)

    # 4. ---------- Risposta ----------
    return jsonify({
        "teams"        : team_order,
        "services"     : all_services,
        "stats_matrix" : stats_matrix,
        "team_totals"  : dict(team_totals),
        "total_flags"  : sum(team_totals.values()),
    })


@api.route('/team_stats_compare', methods=['GET'])
@auth.auth_required
def get_team_stats_compare():
    """
    Endpoint per confrontare le statistiche tra due tick consecutivi
    Rileva quando si smette di exploitare un team (da >0 a 0 flag)
    """
    config = reloader.get_config()
    tick_duration = config.get('ROUND_TIME', 60)
    start_time = config.get('START_TIME', round(time.time()))
    
    # Ottieni i tick da confrontare
    current_tick = request.args.get('current_tick', type=int)
    previous_tick = request.args.get('previous_tick', type=int)
    
    if current_tick is None:
        current_time = round(time.time())
        current_tick = max(1, (current_time - start_time) // tick_duration)
    
    if previous_tick is None:
        previous_tick = max(1, current_tick - 1)
    
    # Non confrontare se siamo al primo tick
    if previous_tick < 1 or current_tick <= previous_tick:
        return jsonify({
            'current_tick': current_tick,
            'previous_tick': previous_tick,
            'alerts': [],
            'message': 'No comparison available'
        })
    
    def get_tick_stats(tick):
        tick_start = start_time + (tick - 1) * tick_duration
        tick_end = start_time + tick * tick_duration
        
        with db_cursor(True) as (_, curs):
            curs.execute("""
                SELECT 
                    team,
                    sploit,
                    COUNT(*) as accepted_flags
                FROM flags 
                WHERE status = %s 
                    AND time >= %s 
                    AND time < %s
                GROUP BY team, sploit
            """, (FlagStatus.ACCEPTED.name, tick_start, tick_end))
            
            return curs.fetchall()
    
    # Ottieni le statistiche per entrambi i tick
    current_stats = get_tick_stats(current_tick)
    previous_stats = get_tick_stats(previous_tick)
    
    # Organizza i dati in dizionari per facilitare il confronto
    def organize_stats(stats):
        organized = defaultdict(lambda: defaultdict(int))
        for stat in stats:
            organized[stat['team']][stat['sploit']] = stat['accepted_flags']
        return organized
    
    current_data = organize_stats(current_stats)
    previous_data = organize_stats(previous_stats)
    
    # Trova gli alert (team che sono passati da >0 a 0 flag)
    alerts = []
    
    # Ottieni tutti i team e servizi coinvolti (filtra dati malformati e team non validi)
    all_teams = set(team for team in list(current_data.keys()) + list(previous_data.keys()) 
                   if is_valid_team(team))
    all_services = set()
    for team_data in list(current_data.values()) + list(previous_data.values()):
        all_services.update(service for service in team_data.keys() 
                          if service and service.strip())
    
    for team in all_teams:
        for service in all_services:
            previous_count = previous_data[team][service]
            current_count = current_data[team][service]
            
            # Alert se da >0 si va a 0
            if previous_count > 0 and current_count == 0:
                alerts.append({
                    'team': team,
                    'service': service,
                    'previous_flags': previous_count,
                    'current_flags': current_count,
                    'type': 'exploit_stopped'
                })
    
    response = {
        'current_tick': current_tick,
        'previous_tick': previous_tick,
        'alerts': alerts,
        'total_alerts': len(alerts)
    }
    
    return jsonify(response)
