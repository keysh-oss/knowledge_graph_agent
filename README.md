# Knowledge Graph Incident Popup Agent

This prototype demonstrates an agent that:

- Polls configured Slack channels for messages that indicate incidents (P1/P2/P3 or containing a Jira key)
- Queries a Neo4j graph to fetch related Jira issues, Confluence pages, and Slack messages
- Displays the incident information in a local popup served by a small Flask app (opens in the default browser)

This is a starting point you can adapt to your environment.

Prerequisites
- Python 3.10+
- Slack Bot Token with `channels:history` / `channels:read` and/or `groups:history` depending on private/public channels
- Neo4j instance reachable from the machine

Quickstart

1. Copy `config.example.json` to `config.json` and fill in your values (Slack token, channel IDs, Neo4j URI/creds).
2. Create a Python virtual environment and install dependencies:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

3. Start the Flask UI (it will run on http://127.0.0.1:5999):

```bash
python -m src.web_ui
```

4. In another terminal, run the agent:

```bash
python -m src.agent
```

Behavior
- The agent polls channels every `poll_interval_seconds` from the config. When it detects a message containing `P1`, `P2`, `P3` or a Jira key (e.g., `ABC-123`), it queries Neo4j for an `Incident` node or related data and posts the details to the web UI, which opens a browser popup.

Notes & next steps
- This is a prototype: adjust Neo4j Cypher queries to match your schema.
- For production: use Slack Events API / Socket Mode instead of polling, secure the Flask endpoint, persist seen timestamps, and add retries/backoff.

Using an external MCP server
---------------------------
The repository no longer ships a built-in MCP server. If you want to run a separate MCP (Model Context Protocol) server from an external Git repo, you can fetch it into this workspace under `vendor/` and run it from the project's virtualenv.

Quick helper scripts
- Fetch an external MCP repo (adds as a submodule when run from inside a git repo):

  ./scripts/fetch_mcp.sh <git-repo-url> [target-dir]

  Example:

  ./scripts/fetch_mcp.sh https://github.com/yourorg/mcp-server.git vendor/mcp-server

- Start the MCP server from the vendor directory (uses the project's venv and uvicorn):

  ./scripts/start_mcp_from_vendor.sh vendor/mcp-server

The starter script assumes the vendor project exposes a FastAPI/ASGI application at `src/mcp_server:app` (the same interface previously used). If the external MCP project uses a different entrypoint, adapt `scripts/start_mcp_from_vendor.sh` accordingly.

Notes
- The fetch script will attempt to add the repo as a git submodule if you run it from inside a git repository. If you prefer a plain clone, pass a non-git working directory or edit the script.
- After fetching, check the vendor code and follow its README for any additional setup (API keys, env vars). The starter script will, by default, install `requirements.txt` into the active venv if present.


#start the flex UI in backtground

nohup .venv/bin/python -m src.web_ui > /tmp/web_ui.log 2>&1 & echo "web_ui started, PID=$! (logs: /tmp/web_ui.log)"
tail -f /tmp/web_ui.log

#normally start the webui

.venv/bin/python -m src.web_ui

to kill the process using 5999 port.

lsof -iTCP:5999 -sTCP:LISTEN -n -P
if [ -n "$PID" ]; then
  echo "Stopping PID $PID..."
  kill "$PID"
  # wait up to 10s for the port to free
  for i in {1..10}; do
    if ! lsof -iTCP:5999 -sTCP:LISTEN -n -P >/dev/null; then
      echo "Port 5999 freed."
      break
    fi
    echo "Waiting for port to free... ($i)"
    sleep 1
  done
  if lsof -iTCP:5999 -sTCP:LISTEN -n -P >/dev/null; then
    echo "Port still in use; forcing kill $PID"
    kill -9 "$PID"
  fi
else
  echo "No process found listening on port 5999"
fi



# Stop existing services if pid files exist, then start MCP, web_ui, and agent using the venv
set -e
echo "Stopping prior processes if present..."
if [ -f mcp.pid ]; then kill "$(cat mcp.pid)" >/dev/null 2>&1 || true; rm -f mcp.pid; fi
if [ -f web_ui.pid ]; then kill "$(cat web_ui.pid)" >/dev/null 2>&1 || true; rm -f web_ui.pid; fi
if [ -f agent.pid ]; then kill "$(cat agent.pid)" >/dev/null 2>&1 || true; rm -f agent.pid; fi

# Start web UI
echo "Starting web UI on 127.0.0.1:5999"
nohup .venv/bin/python -u -c "from src import web_ui; web_ui.app.run(host='127.0.0.1', port=5999)" > web_ui.log 2>&1 & echo $! > web_ui.pid
sleep 0.5

# Start agent
echo "Starting IncidentAgent (agent)"
nohup .venv/bin/python -u -c "from src.agent import IncidentAgent; agent=IncidentAgent('config.json'); agent.start()" > agent.log 2>&1 & echo $! > agent.pid
sleep 0.5

  # Start MCP server (uvicorn)
echo "Starting MCP server on 127.0.0.1:9000"
nohup .venv/bin/uvicorn src.mcp_server:app --host 127.0.0.1 --port 9000 > mcp.log 2>&1 & echo $! > mcp.pid || (echo "Failed to start mcp with .venv/bin/uvicorn, trying python -m uvicorn" && nohup .venv/bin/python -m uvicorn src.mcp_server:app --host 127.0.0.1 --port 9000 > mcp.log 2>&1 & echo $! > mcp.pid)
sleep 0.5