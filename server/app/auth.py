from functools import wraps

from flask import request, abort

from reloader import get_config


def auth_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        config = get_config()
        # Auth is driven by the unified config's enable_api_auth. When off (the
        # default — the farm runs on the team's internal network), the API is
        # open. When on, requests must carry the server password or API token in
        # the Authorization header.
        if not config.get('ENABLE_API_AUTH'):
            return f(*args, **kwargs)

        auth = request.headers.get('Authorization')
        if auth not in (config['SERVER_PASSWORD'], config.get('API_TOKEN')):
            abort(403)

        return f(*args, **kwargs)

    return decorated
