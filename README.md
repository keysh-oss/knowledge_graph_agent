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

3. Start the Flask UI (it will run on http://127.0.0.1:5000):

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

