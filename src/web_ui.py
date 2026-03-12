"""Simple Flask web UI that shows an incident popup when posted to /incident.
The agent posts incident JSON to /incident. The UI stores it in memory and exposes
/a popup URL for the agent to open in a browser.
"""

import uuid
import json
import logging
import os
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

def require_auth(f):
    def wrapper(*args, **kwargs):
        # if no UI_API_KEY configured, allow access
        if not UI_API_KEY:
            return f(*args, **kwargs)
        # allow API key header
        header = request.headers.get("X-API-KEY")
        if header and header == UI_API_KEY:
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
    logging.info("Received incident %s", iid)
    return jsonify({"popup_url": popup_url})

@app.route("/popup/<iid>")
@require_auth
def popup(iid):
    data = INCIDENTS.get(iid)
    if not data:
        return "Incident not found", 404
    # render a simple popup page
    return render_template("popup.html", incident=data)


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
    app.run(host="127.0.0.1", port=5000, debug=False)
