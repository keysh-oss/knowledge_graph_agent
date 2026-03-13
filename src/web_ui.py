"""Simple Flask web UI that shows an incident popup when posted to /incident.
The agent posts incident JSON to /incident. The UI stores it in memory and exposes
/a popup URL for the agent to open in a browser.
"""

import uuid
import json
import logging
import os
import time
from dotenv import load_dotenv
from flask import Flask, request, jsonify, render_template, session, redirect, url_for

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
# Ensure templates are resolved relative to this file (templates/ at repo root)
templates_path = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "templates"))
app = Flask(__name__, template_folder=templates_path)

# Simple auth: if UI_API_KEY is set in env, require login (session or X-API-KEY header)
UI_API_KEY = os.getenv("UI_API_KEY")
app.secret_key = os.getenv("FLASK_SECRET") or os.urandom(24)

# simple one-time tokens to allow the agent to open a popup in the browser
# without requiring the user to log in interactively. Tokens are short-lived.
ONE_TIME_TOKENS = {}

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
                # consume the token (one-time)
                try:
                    del ONE_TIME_TOKENS[token]
                except Exception:
                    pass
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
    popup_url = f"/popup/{iid}"
    # if the request used a valid API key header (agent posted with X-API-KEY),
    # create a short-lived one-time token so the agent can open the popup in
    # the user's browser without requiring interactive login.
    header = request.headers.get("X-API-KEY")
    if UI_API_KEY and header and header == UI_API_KEY:
        try:
            token = uuid.uuid4().hex
            # token valid for 30 seconds
            ONE_TIME_TOKENS[token] = time.time() + 30
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
    for k, v in payload.items():
        if k == 'iid':
            continue
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
