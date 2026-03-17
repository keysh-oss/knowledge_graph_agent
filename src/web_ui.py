"""Simple Flask web UI that shows an incident popup when posted to /incident.
The agent posts incident JSON to /incident. The UI stores it in memory and exposes
/a popup URL for the agent to open in a browser.
"""

import uuid
import json
import logging
import os
import time
import threading
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, session, redirect, url_for

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
# Ensure templates are resolved relative to this file (templates/ at repo root)
templates_path = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "templates"))
app = Flask(__name__, template_folder=templates_path)

# Lock used to atomically claim popups across threads in this process
CLAIM_LOCK = threading.Lock()

# Simple auth: if UI_API_KEY is set in env, require login (session or X-API-KEY header)
UI_API_KEY = os.getenv("UI_API_KEY")
app.secret_key = os.getenv("FLASK_SECRET") or os.urandom(24)

# simple one-time tokens to allow the agent to open a popup in the browser
# without requiring the user to log in interactively. Tokens are short-lived.
ONE_TIME_TOKENS = {}
# TTL in seconds for one-time popup tokens (default 5 minutes). Can be overridden
# via the environment variable ONE_TIME_TOKEN_TTL.
ONE_TIME_TOKEN_TTL = int(os.getenv('ONE_TIME_TOKEN_TTL') or os.getenv('UI_POPUP_TTL') or 300)

def require_auth(f):
    def wrapper(*args, **kwargs):
        # if no UI_API_KEY configured, allow access
        if not UI_API_KEY:
            return f(*args, **kwargs)
        # allow API key header
        header = request.headers.get("X-API-KEY")
        if header and header == UI_API_KEY:
            return f(*args, **kwargs)
        # allow one-time token passed as query param (used when the agent
        # opens the popup in the browser). Tokens are validated against
        # ONE_TIME_TOKENS and are short-lived.
        token = request.args.get('t')
        if token:
            exp = ONE_TIME_TOKENS.get(token)
            if exp and exp > time.time():
                # Allow the one-time token to be reused for the lifetime of the
                # short expiry so client-side polling from the popup can continue
                # to fetch /incident/<id>?t=<token>. Previously the token was
                # consumed on first use which prevented the popup's JS from
                # authenticating subsequent polling requests. We still enforce
                # expiry by checking the timestamp.
                return f(*args, **kwargs)
        # allow session-based login
        if session.get("authenticated"):
            return f(*args, **kwargs)
        # otherwise redirect to login
        return redirect(url_for("login", next=request.path))
    wrapper.__name__ = f.__name__
    return wrapper

# in-memory store of incidents keyed by id
INCIDENTS = {}

@app.route("/incident", methods=["POST"])
@require_auth
def receive_incident():
    payload = request.get_json()
    iid = str(uuid.uuid4())
    INCIDENTS[iid] = payload
    # store a best-effort top-level message so the popup can show the
    # incident text immediately even if the agent nested it under `data` or `slack`.
    try:
        if not INCIDENTS[iid].get('message'):
            msg = INCIDENTS[iid].get('message') or INCIDENTS[iid].get('description')
            data_section = INCIDENTS[iid].get('data') or {}
            if not msg and isinstance(data_section, dict):
                msg = data_section.get('message') or data_section.get('text')
            slack_section = INCIDENTS[iid].get('slack') or {}
            if not msg and isinstance(slack_section, dict):
                msg = slack_section.get('text') or slack_section.get('message')
            if msg:
                INCIDENTS[iid]['message'] = msg
    except Exception:
        pass
    # Normalize enrichment fields so the UI shows Jira/Confluence results
    try:
        def normalize(iid):
            inc = INCIDENTS.get(iid)
            if not inc:
                return
            # If mcp_result is present (array of rows from Neo4j), try to map
            # Jira-like rows into incident.jira so the Jira section renders.
            if not inc.get('jira') and inc.get('mcp_result') and isinstance(inc.get('mcp_result'), list):
                jira_list = []
                for row in inc.get('mcp_result'):
                    # Row shapes vary: could be {'i': {...}} or flattened keys like {'i.summary': '...'}
                    candidate = None
                    if isinstance(row, dict):
                        # prefer nested 'i' dict
                        if 'i' in row and isinstance(row['i'], dict):
                            candidate = row['i']
                        else:
                            # try to reconstruct from dotted keys like 'i.summary'
                            dotted = {k: v for k, v in row.items() if isinstance(k, str) and '.' in k}
                            if dotted:
                                # group by prefix (e.g., 'i') and take the first group
                                groups = {}
                                for k, v in dotted.items():
                                    prefix, prop = k.split('.', 1)
                                    groups.setdefault(prefix, {})[prop] = v
                                # take first group as candidate
                                for grp in groups.values():
                                    candidate = grp
                                    break
                            else:
                                # pick first dict value if present
                                for v in row.values():
                                    if isinstance(v, dict):
                                        candidate = v
                                        break
                    if not candidate:
                        continue
                    src = candidate.get('source') or ''
                    url = candidate.get('url') or ''
                    if src.lower() == 'jira' or 'atlassian' in url:
                        jira_list.append({
                            'key': candidate.get('key') or candidate.get('id'),
                            'url': url,
                            'summary': candidate.get('summary'),
                            'status': candidate.get('status')
                        })
                if jira_list:
                    inc['jira'] = jira_list
                    # mark found so template renders the enrichment block
                    inc['found'] = True
        normalize(iid)
    except Exception:
        pass
    popup_url = f"/popup/{iid}"
    # if the request used a valid API key header (agent posted with X-API-KEY),
    # create a short-lived one-time token so the agent can open the popup in
    # the user's browser without requiring interactive login.
    header = request.headers.get("X-API-KEY")
    if UI_API_KEY and header and header == UI_API_KEY:
        try:
            token = uuid.uuid4().hex
            # token valid for configured TTL
            ONE_TIME_TOKENS[token] = time.time() + ONE_TIME_TOKEN_TTL
            popup_url = f"/popup/{iid}?t={token}"
        except Exception:
            pass
    logging.info("Received incident %s", iid)
    return jsonify({"popup_url": popup_url})


@app.route('/incident_update', methods=['POST'])
@require_auth
def incident_update():
    """Accept partial enrichment updates for an existing incident.

    Expected JSON: { iid: <id>, jira: [...], confluence: [...], suggested_resolution: {...} }
    """
    payload = request.get_json() or {}
    iid = payload.get('iid')
    if not iid:
        return jsonify({'error': 'missing iid'}), 400
    if iid not in INCIDENTS:
        return jsonify({'error': 'unknown iid'}), 404

    # merge fields into stored incident
    # avoid overwriting the original slack/message by merging only provided keys
    def item_key(item):
        """Return a simple dedupe key for a dict/list item.
        Prefer common fields (key, id, url, title) otherwise return its JSON.
        """
        try:
            if not isinstance(item, dict):
                return str(item)
            # unwrap nested neo4j rows like {'i': {...}} or {'n': {...}}
            if 'i' in item and isinstance(item['i'], dict):
                item = item['i']
            elif 'n' in item and isinstance(item['n'], dict):
                item = item['n']
            for k in ('key', 'id', 'url', 'name', 'title'):
                if k in item and item.get(k):
                    return str(item.get(k))
            # fallback: some rows are flattened dotted keys like 'i.summary'
            for k in item.keys():
                if isinstance(k, str) and '.' in k:
                    # use the joined values as a stable key
                    return '|'.join([str(item.get(k)) for k in sorted(item.keys())])
            return json.dumps(item, sort_keys=True)
        except Exception:
            return str(item)

    def merge_list(dst, src):
        """Merge src list into dst list in-place, dedup by item_key."""
        if not isinstance(dst, list) or not isinstance(src, list):
            return src
        seen = {item_key(x): x for x in dst}
        for it in src:
            k = item_key(it)
            if k not in seen:
                dst.append(it)
                seen[k] = it
        return dst

    for k, v in payload.items():
        if k == 'iid':
            continue
        # if both existing and incoming are lists, merge them
        existing = INCIDENTS[iid].get(k)
        if isinstance(existing, list) and isinstance(v, list):
            INCIDENTS[iid][k] = merge_list(existing, v)
            continue
        # if updating the 'data' dict, merge list subkeys instead of replacing
        if k == 'data' and isinstance(v, dict):
            cur_data = INCIDENTS[iid].get('data') or {}
            for subk, subv in v.items():
                if subk in cur_data and isinstance(cur_data[subk], list) and isinstance(subv, list):
                    cur_data[subk] = merge_list(cur_data[subk], subv)
                else:
                    cur_data[subk] = subv
            INCIDENTS[iid]['data'] = cur_data
            continue
        # default: overwrite/assign
        INCIDENTS[iid][k] = v

    logging.info('Updated incident %s', iid)
    return jsonify({'ok': True})

@app.route("/popup/<iid>")
@require_auth
def popup(iid):
    data = INCIDENTS.get(iid)
    if not data:
        return "Incident not found", 404
    # render a simple popup page
    token = request.args.get('t')
    return render_template("popup.html", incident=data, iid=iid, token=token)


@app.route('/incident/<iid>', methods=['GET'])
@require_auth
def get_incident(iid):
    """Return the stored incident JSON for client-side polling/refresh."""
    data = INCIDENTS.get(iid)
    if not data:
        return jsonify({'error': 'not found'}), 404
    return jsonify(data)


@app.route('/claim_popup', methods=['POST'])
@require_auth
def claim_popup():
    """Atomically claim a popup for an incident so only one agent/process
    opens the browser window. POST JSON: {"iid": "..."}. Returns
    {"claimed": true} if this request successfully claimed it, or
    {"claimed": false} if already claimed.
    """
    payload = request.get_json() or {}
    iid = payload.get('iid')
    if not iid:
        return jsonify({'error': 'missing iid'}), 400
    if iid not in INCIDENTS:
        return jsonify({'error': 'unknown iid'}), 404
    with CLAIM_LOCK:
        inc = INCIDENTS.get(iid)
        if inc.get('popup_opened'):
            return jsonify({'claimed': False})
        # mark as opened and record timestamp
        inc['popup_opened'] = True
        inc['popup_opened_at'] = time.time()
    logging.info('Popup claimed for %s', iid)
    return jsonify({'claimed': True})


@app.route('/popup_launcher/<iid>')
@require_auth
def popup_launcher(iid):
    """Intermediary launcher page that opens the real popup in a new browser
    window using window.open(...features...) and then closes itself.
    This increases the chance the browser will open a separate window instead
    of a tab. The agent will open this URL instead of /popup/<iid>.
    """
    data = INCIDENTS.get(iid)
    if not data:
        return "Incident not found", 404
    popup_url = url_for('popup', iid=iid, _external=True)
    # preserve one-time token query param if present so the popup can be
    # opened without requiring interactive login
    token = request.args.get('t')
    if token:
        popup_url = popup_url + ("&" if "?" in popup_url else "?") + f"t={token}"
    return render_template('launcher.html', popup_url=popup_url)

@app.route("/")
@require_auth
def index():
    return "Agent UI running. Use POST /incident to create popup."


@app.route('/login', methods=['GET', 'POST'])
def login():
    # if no UI_API_KEY configured, show message that login is disabled
    if not UI_API_KEY:
        return "Login disabled: no UI_API_KEY configured", 400
    if request.method == 'GET':
        return render_template('login.html')
    # POST
    form_key = request.form.get('api_key')
    if form_key == UI_API_KEY:
        session['authenticated'] = True
        next_url = request.args.get('next') or url_for('index')
        return redirect(next_url)
    return render_template('login.html', error='Invalid API key')

if __name__ == "__main__":
    # Note: for production use a proper WSGI server
    app.run(host="127.0.0.1", port=5999, debug=False)
