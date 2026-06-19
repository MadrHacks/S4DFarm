import os
from datetime import datetime
from urllib.parse import urlsplit

import requests
import yaml

# -----------------------------------------------------------------------------
# Configuration source
#
# S4DFarm natively reads the MadrHacks ad-tools "unified config": a directory of
# YAML files shared by every service (AD_INFRA_CONFIG_DIR, default /config). When
# that config is present it is the single source of truth, so updating the
# central config files updates the farm with no duplication or drift. When it is
# absent, the farm falls back to the upstream environment-variable configuration,
# so it still runs standalone.
# -----------------------------------------------------------------------------

CONFIG_DIR = os.environ.get('AD_INFRA_CONFIG_DIR', '/config')


def _load_yaml(name):
    try:
        with open(os.path.join(CONFIG_DIR, name)) as f:
            return yaml.safe_load(f) or {}
    except (FileNotFoundError, NotADirectoryError):
        return {}


GAME = _load_yaml('game.yml')   # ad-tools GameConfig
FARM = _load_yaml('farm.yml')   # ad-tools FarmConfig


def pick(unified, env, default):
    """Unified-config value (when set) wins, else the env var, else the default.
    With the unified config present this means the central files are the source
    of truth; standalone it preserves the original env-var behaviour."""
    if unified is not None:
        return unified
    return os.environ.get(env, default)


def as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'yes', 'on')


def parse_time(value):
    """Parse an ISO-8601 timestamp to epoch seconds, tolerating a trailing 'Z'
    and a missing seconds field (the ad-tools game.yml uses e.g.
    '2026-06-19T00:00Z'). Returns 0 when absent/unparseable."""
    if not value:
        return 0
    text = str(value).strip().replace('Z', '+00:00')
    try:
        return round(datetime.fromisoformat(text).timestamp())
    except ValueError:
        for fmt in ('%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M%z'):
            try:
                return round(datetime.strptime(text, fmt).timestamp())
            except ValueError:
                continue
    print(f"[config] could not parse time {value!r}")
    return 0


def gameserver_host():
    """Gameserver host, derived from the unified gameserver_url so there is no
    separate hardcoded address to drift."""
    url = pick(GAME.get('gameserver_url'), 'SYSTEM_URL', 'http://10.10.0.1:8080/flags')
    return urlsplit(url).hostname or os.environ.get('SYSTEM_HOST', '10.10.0.1')


def team_format():
    return pick(GAME.get('ip_format'), 'TEAM_FORMAT', '10.60.{}.1')


def fetch_teams():
    """Live team list from the CCIT gameserver /api/status — the source of truth
    for real team names + ids. Returns {sanitized_name: host} or None on failure
    so the caller can fall back (the farm must still boot pre-game / offline)."""
    host = gameserver_host()
    try:
        info = requests.get(f"http://{host}/api/status", timeout=5).json()
    except Exception as e:
        print(f"[config] could not fetch teams from {host}/api/status: {e}")
        return None
    teams = info.get('teams', [])
    if not teams:
        print(f"[config] gameserver {host} returned no teams")
        return None
    fmt = team_format()
    return {
        str(t['name']).replace('"', '').replace("'", ''): fmt.format(t['id'])
        for t in teams
    }


def generate_teams():
    """Fallback team list built from the unified config (team range + ip_format,
    skipping our own team and the NOP team), used when the gameserver is
    unreachable during development or before the game opens."""
    fmt = team_format()
    count = int(pick(GAME.get('range_ip_teams'), 'TEAM_NUM', 45))
    skip = {GAME.get('nop_team'), GAME.get('team_id')}
    return {
        f'Team #{i}': fmt.format(i)
        for i in range(1, count + 1) if i not in skip
    }


def build_teams():
    return fetch_teams() or generate_teams()


def flag_lifetime():
    """Flags older than this (seconds) are not resubmitted. The unified config
    expresses it in ticks, so convert with the tick duration."""
    ticks = GAME.get('flag_lifetime_ticks')
    if ticks is not None:
        tick = float(pick(GAME.get('tick_duration_sec'), 'TICK_DURATION', 120))
        return float(ticks) * tick
    return float(os.environ.get('FLAG_LIFETIME', 60))


# Flag-ids endpoint split into ip/port from the unified flag_ids_url.
_flag_ids = urlsplit(pick(
    GAME.get('flag_ids_url'), 'SYSTEM_ID_FLAGS_URL',
    f"http://{os.environ.get('SYSTEM_ID_FLAGS_IP', '10.10.0.1')}:"
    f"{os.environ.get('SYSTEM_ID_FLAGS_PORT', '8081')}"))

_team_token = pick(GAME.get('team_token'), 'SYSTEM_TOKEN', 'your_secret_token')

CONFIG = {
    'DEBUG': as_bool(os.environ.get('DEBUG', 'False')),

    # The clients run sploits on TEAMS and the FLAG_FORMAT regex scrapes flags
    # from the sploits' stdout.
    'TEAMS': build_teams(),
    'FLAG_FORMAT': pick(GAME.get('flag_regex'), 'FLAG_FORMAT', r'[A-Z0-9]{31}='),

    # Scoreboard for team ranking; defaults to the gameserver.
    'SCOREBOARD_URL': pick(None, 'SCOREBOARD_URL', f"http://{gameserver_host()}"),

    # How and where flags are submitted. The protocol is a module in protocols/.
    'SYSTEM_PROTOCOL': pick(GAME.get('gameserver_protocol'), 'SYSTEM_PROTOCOL', 'ccit_http'),
    # HOST/PORT/TOKEN for the tcp protocols.
    'SYSTEM_HOST': pick(None, 'SYSTEM_HOST', gameserver_host()),
    'SYSTEM_PORT': int(pick(None, 'SYSTEM_PORT', urlsplit(pick(GAME.get('gameserver_url'), 'SYSTEM_URL', '')).port or 8080)),
    'TEAM_TOKEN': _team_token,
    # URL/TOKEN for the http protocols (ccit_http).
    'SYSTEM_URL': pick(GAME.get('gameserver_url'), 'SYSTEM_URL', 'http://10.10.0.1:8080/flags'),
    'SYSTEM_TOKEN': _team_token,
    'HTTP_TIMEOUT': float(os.environ.get('HTTP_TIMEOUT', 30)),
    # Flag-ids endpoint, usable by both http and tcp protocols.
    'SYSTEM_ID_FLAGS_IP': _flag_ids.hostname or '10.10.0.1',
    'SYSTEM_ID_FLAGS_PORT': str(_flag_ids.port or 8081),

    # The server submits at most SUBMIT_FLAG_LIMIT flags every SUBMIT_PERIOD
    # seconds; flags older than FLAG_LIFETIME seconds are skipped.
    'SUBMIT_FLAG_LIMIT': int(pick(FARM.get('submit_flag_limit'), 'SUBMIT_FLAG_LIMIT', 100)),
    'SUBMIT_PERIOD': float(pick(FARM.get('submit_period'), 'SUBMIT_PERIOD', 5)),
    'FLAG_LIFETIME': flag_lifetime(),

    # A/D timing.
    'TICK_DURATION': float(pick(GAME.get('tick_duration_sec'), 'TICK_DURATION', 120)),
    'START_TIME': parse_time(pick(GAME.get('start'), 'START_TIME', '')),
    'END_TIME': parse_time(pick(GAME.get('end'), 'END_TIME', '')),

    # Password for the web interface (any login works). Excluded from the config
    # sent to farm clients.
    'SERVER_PASSWORD': pick(FARM.get('server_password'), 'SERVER_PASSWORD', '1234'),

    # Authorization for API requests.
    'ENABLE_API_AUTH': as_bool(pick(FARM.get('enable_api_auth'), 'ENABLE_API_AUTH', False)),
    'API_TOKEN': pick(FARM.get('api_token'), 'API_TOKEN', 'Tok3N'),

    # Custom library folder added to $PYTHONPATH / $LIBPATH by start_sploit.py.
    'LIBPATH': os.environ.get('LIBPATH', ''),
    'TIMEZONE': 'Europe/Rome',
}
