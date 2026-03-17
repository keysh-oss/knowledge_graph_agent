"""Agent that polls Slack channels and, on incident messages, fetches related data from Neo4j
and posts it to the local web UI to show a popup.

Assumptions:
- Neo4j contains nodes like Incident, JiraIssue, ConfluencePage, SlackMessage and relationships between them.
- If a message contains a Jira key (e.g., ABC-123) the agent will look up by that key first.
- Otherwise it will try to find an Incident by matching the message text (adjust cypher to your schema).
"""

import re
import json
import time
import logging
import threading
import webbrowser
from datetime import datetime
import importlib
# load env early
from dotenv import load_dotenv
load_dotenv()
import os
import src.llm as llm
# optional Jira/Confluence integration
try:
    from src.integrations import jira_confluence as jc
except Exception:
    jc = None

import requests
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
try:
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse
except Exception:
    SocketModeClient = None
    SocketModeRequest = None
    SocketModeResponse = None
from neo4j import GraphDatabase
from apscheduler.schedulers.background import BackgroundScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
PRIORITY_RE = re.compile(r"\bP([123])\b", re.IGNORECASE)


class IncidentAgent:
    def __init__(self, config_path="config.json"):
        with open(config_path, "r") as f:
            self.config = json.load(f)

        # If jira/confluence not configured in config.json, try to read from env (.env)
        updated = False
        jira_cfg = self.config.get('jira') or {}
        if not jira_cfg.get('base_url'):
            jb = os.getenv('JIRA_BASE_URL')
            je = os.getenv('JIRA_EMAIL')
            jt = os.getenv('JIRA_API_TOKEN')
            if jb and je and jt:
                jira_cfg = {'base_url': jb, 'user': je, 'api_token': jt}
                self.config['jira'] = jira_cfg
                updated = True

        conf_cfg = self.config.get('confluence') or {}
        if not conf_cfg.get('base_url'):
            cb = os.getenv('CONFLUENCE_BASE_URL')
            ce = os.getenv('CONFLUENCE_EMAIL')
            ct = os.getenv('CONFLUENCE_API_TOKEN')
            if cb and ce and ct:
                conf_cfg = {'base_url': cb, 'user': ce, 'api_token': ct}
                self.config['confluence'] = conf_cfg
                updated = True

        # persist back to config.json so future runs pick it up (only if we read from env)
        if updated:
            try:
                with open(config_path, 'w') as f:
                    json.dump(self.config, f, indent=2)
                logging.info('Wrote jira/confluence config to %s from environment', config_path)
            except Exception:
                logging.exception('Failed to persist updated config.json')

        self.slack_conf = self.config.get("slack", {})
        self.neo4j_conf = self.config.get("neo4j", {})
        self.web_conf = self.config.get("web_ui", {})

        self.slack = WebClient(token=self.slack_conf.get("bot_token"))
        self.neo4j_driver = GraphDatabase.driver(
            self.neo4j_conf.get("uri"),
            auth=(self.neo4j_conf.get("user"), self.neo4j_conf.get("password")),
        )
        # state persistence for last seen ts per channel to avoid duplicate processing
        self.state_path = self.config.get("state_file") or os.path.join(os.path.dirname(config_path), "last_ts.json")
        self.last_ts = self._load_last_ts() or {cid: 0.0 for cid in self.slack_conf.get("channels", [])}

        self.poll_interval = int(self.slack_conf.get("poll_interval_seconds", 15))
        self.web_endpoint = f"http://{self.web_conf.get('host', '127.0.0.1')}:{self.web_conf.get('port', 5000)}/incident"
        # optional Socket Mode app token (xapp-...) for real-time events
        self.app_token = self.slack_conf.get("app_token")
        self.socket_client = None
        # track which popup iids we've opened to avoid duplicate windows
        self._opened_iids = set()
        # lock to make check-and-add atomic across threads in this process
        self._opened_iids_lock = threading.Lock()
        # track Slack message ts values that have been processed to prevent
        # race condition re-processing within a single agent process
        self._processed_ts = set()
        self._processed_ts_lock = threading.Lock()

    def start(self):
        logging.info("Starting IncidentAgent")
        # If socket mode available and app_token provided, use Socket Mode for real-time events
        if self.app_token and SocketModeClient is not None:
            logging.info("Starting Socket Mode listener")
            self._start_socket_mode()
            try:
                while True:
                    time.sleep(1)
            except (KeyboardInterrupt, SystemExit):
                if self.socket_client:
                    try:
                        self.socket_client.disconnect()
                    except Exception:
                        pass
                self.neo4j_driver.close()
                logging.info("Agent stopped")
        else:
            logging.info("Starting polling scheduler")
            scheduler = BackgroundScheduler()
            scheduler.add_job(self.poll_channels, "interval", seconds=self.poll_interval)
            scheduler.start()

            try:
                # keep the main thread alive
                while True:
                    time.sleep(1)
            except (KeyboardInterrupt, SystemExit):
                scheduler.shutdown()
                self.neo4j_driver.close()
                logging.info("Agent stopped")

    def poll_channels(self):
        channels = self.slack_conf.get("channels", [])
        for cid in channels:
            try:
                # fetch messages newer than last_ts (omit 'oldest' if we haven't seen any yet)
                oldest_val = self.last_ts.get(cid, 0.0)
                params = {"channel": cid, "limit": 50}
                if oldest_val and oldest_val > 0.0:
                    params["oldest"] = str(oldest_val)
                resp = self.slack.conversations_history(**params)
                messages = resp.get("messages", [])
                if not messages:
                    continue

                # messages come newest first; process oldest->newest
                for msg in reversed(messages):
                    ts = float(msg.get("ts", "0"))
                    if ts <= self.last_ts.get(cid, 0.0):
                        continue

                    self.process_message(cid, msg)
                    # update per-message so we don't reprocess on crash
                    self.last_ts[cid] = ts
                    try:
                        self._save_last_ts()
                    except Exception:
                        logging.exception("Failed to persist last_ts")

                # After processing, ensure last_ts equals the timestamp of the newest
                # message in the channel (so we don't reprocess older messages).
                try:
                    newest_ts = float(messages[0].get("ts", "0"))
                    if newest_ts and newest_ts > self.last_ts.get(cid, 0.0):
                        self.last_ts[cid] = newest_ts
                        self._save_last_ts()
                except Exception:
                    logging.exception("Failed to persist last_ts after polling channel %s", cid)

            except SlackApiError as e:
                logging.error("Slack API error for channel %s: %s", cid, e)
            except Exception as e:
                logging.exception("Unexpected error while polling channel %s: %s", cid, e)

    def process_message(self, channel_id, msg):
        text = msg.get("text", "")
        user = msg.get("user")
        ts = msg.get("ts")

        # Dedupe: skip if this exact Slack message (by ts) was already processed
        # by this agent instance. This prevents race conditions where the same
        # message might be picked up by concurrent poll cycles.
        if ts:
            with self._processed_ts_lock:
                if ts in self._processed_ts:
                    logging.debug("Skipping already-processed message ts=%s", ts)
                    return
                self._processed_ts.add(ts)

        logging.info("Processing message in %s @%s: %s", channel_id, ts, text[:200])

        # detect priority or jira key
        p_match = PRIORITY_RE.search(text)
        jira_match = JIRA_KEY_RE.search(text)

        if not p_match and not jira_match and "incident" not in text.lower():
            logging.debug("Message doesn't look like an incident")
            return

        priority = f"P{p_match.group(1)}" if p_match else None
        jira_key = jira_match.group(1) if jira_match else None

        # use LLM to extract additional fields (priority, jira_keys, services, summary)
        try:
            llm_data = llm.extract_incident_fields(text)
        except Exception:
            llm_data = None

        if llm_data:
            # prefer LLM-detected priority if not present
            if not priority and llm_data.get("priority"):
                priority = llm_data.get("priority")
            # prefer LLM-detected jira keys if not present
            if not jira_key:
                jk = llm_data.get("jira_keys") or []
                if len(jk) > 0:
                    jira_key = jk[0]

        # build search text for fallback searches: prefer LLM summary + services
        search_text = text
        if llm_data:
            summary = llm_data.get("summary")
            services = llm_data.get("services") or []
            parts = []
            if summary:
                parts.append(summary)
            if services:
                parts.extend(services)
            if parts:
                search_text = " ".join(parts)

        # fetch data from Neo4j
        # First try MCP (NL -> Cypher -> Neo4j) if configured. MCP will return a graph
        # (nodes/edges) and the cypher used. Fall back to local fetch if MCP not available
        # We no longer call an external MCP server from this agent; directly
        # query Neo4j for incident data using the local fetch helper.
        incident_data = self.fetch_incident_data(jira_key=jira_key, text=search_text)
        if not incident_data:
            logging.info("No incident data found in Neo4j for message: %s", text[:120])
            # still show a minimal popup with message
            incident_payload = {
                "title": f"Incident detected ({priority or 'unknown'})",
                "message": text,
                "slack": {"channel": channel_id, "ts": ts, "user": user},
                "found": False,
                # mark Neo4j search completed (no results)
                "neo4j_done": True,
                "neo4j_result": None,
            }
            # no MCP integration in this workspace; we show a minimal popup
        else:
            # adapt to discovered schema: primary object may be an Issue or a generic node
            issue = incident_data.get("issue") or incident_data.get("node")
            title_key = issue.get("key") if issue else None
            incident_payload = {
                "title": f"Incident: {title_key or issue.get('title', 'unknown') if issue else 'unknown'}",
                "message": text,
                "data": incident_data,
                "found": True,
                # mark Neo4j search completed and attach raw result
                "neo4j_done": True,
                "neo4j_result": incident_data,
            }
            # no MCP integration in this workspace
            # If the fetch returned multiple rows (mcp_rows), attach them as mcp_result
            if isinstance(incident_data, dict) and incident_data.get('mcp_rows'):
                incident_payload['mcp_result'] = incident_data.get('mcp_rows')
                # also set a simplified neo4j_result to the rows
                incident_payload['neo4j_result'] = incident_data.get('mcp_rows')

        # --- Jira / Confluence enrichments ---
        try:
            jira_cfg = self.config.get('jira') or {}
            conf_cfg = self.config.get('confluence') or {}
            jira_keys = []
            if llm_data:
                jira_keys = llm_data.get('jira_keys') or []
            # also include detected top-level jira_key
            if jira_key and jira_key not in jira_keys:
                jira_keys = [jira_key] + jira_keys

            jira_results = []
            conf_results = []
            suggested = None
            if jc is not None:
                try:
                    # use the LLM-driven Jira query that returns prev_day and related issues
                    jira_sets = jc.query_jira_with_llm(jira_cfg, incident_text=text, max_results=50)
                    jira_results = jira_sets.get('related', [])
                    jira_prev_day = jira_sets.get('prev_day', [])
                except Exception:
                    logging.exception("Error querying Jira via LLM integration")
                try:
                    # for Confluence, search using the LLM summary if available
                    conf_query_text = (llm_data.get('summary') if llm_data and llm_data.get('summary') else search_text)
                    conf_results = jc.query_confluence(conf_cfg, query=conf_query_text, jira_keys=jira_keys)
                except Exception:
                    logging.exception("Error querying Confluence via integration")
                try:
                    suggested = jc.summarize_with_llm((jira_results or []) + (jira_prev_day or []), conf_results, incident_text=text)
                except Exception:
                    logging.exception("Error summarizing Jira/Confluence results with LLM")
            else:
                logging.debug("Jira/Confluence integration module not available; skipping")

            # attach results to payload (attach even when not found so UI can render a "no results" state)
            incident_payload['jira'] = jira_results
            incident_payload['confluence'] = conf_results
            if suggested:
                incident_payload['suggested_resolution'] = suggested
            # mark enrichment as completed synchronously so the agent can open the
            # popup with Jira/Confluence data already present
            incident_payload['jira_done'] = True
            incident_payload['confluence_done'] = True
            incident_payload['enrichment_done'] = True
        except Exception:
            logging.exception("Unexpected error enriching incident with Jira/Confluence data")

        # post to web UI
        try:
            # include UI API key header if configured so the UI accepts the request
            headers = {}
            ui_key = self.web_conf.get('api_key') or os.getenv('UI_API_KEY')
            if ui_key:
                headers['X-API-KEY'] = ui_key
            r = requests.post(self.web_endpoint, json=incident_payload, timeout=5, headers=headers)
            if r.status_code == 200:
                resp_json = r.json()
                # Check if server indicates this is a dedupe (same Slack message
                # already created an incident). If so, skip opening a popup.
                if resp_json.get("dedupe"):
                    logging.info("Server dedupe: incident already exists for this Slack message")
                    return

                # open popup in browser
                pop_url = resp_json.get("popup_url")
                if pop_url:
                    # prefer opening the launcher URL which uses JS to open a new
                    # browser window with features (size/chrome) and then closes
                    # the intermediate tab. The UI returns /popup/<id>, so build
                    # /popup_launcher/<id> instead.
                    host = self.web_conf.get("host", "127.0.0.1")
                    port = self.web_conf.get("port", 5000)
                    base = f"http://{host}:{port}"
                    # convert /popup/<id> -> /popup_launcher/<id>
                    if pop_url.startswith("/popup/"):
                        launcher = "/popup_launcher/" + pop_url.split("/popup/", 1)[1]
                    else:
                        # fallback: just use the returned url
                        launcher = pop_url
                    full = base + launcher
                    # extract iid so we can enrich it asynchronously
                    try:
                        if pop_url.startswith("/popup/"):
                            iid = pop_url.split("/popup/", 1)[1]
                        else:
                            # try to parse from the returned URL
                            iid = pop_url.rstrip("/").split("/")[-1]
                    except Exception:
                        iid = None

                    # open in background thread to avoid blocking. Use a lock-protected
                    # check-and-add so multiple threads in this process don't open the
                    # same iid concurrently. (Note: this does not dedupe across
                    # separate processes.) We still run enrichment even if the popup
                    # was already opened earlier.
                    if iid:
                        already_opened = False
                        try:
                            with self._opened_iids_lock:
                                if iid in self._opened_iids:
                                    already_opened = True
                                else:
                                    # reserve this iid so other threads won't also open it
                                    self._opened_iids.add(iid)
                        except Exception:
                            # if locking fails, fall back to the old behavior
                            already_opened = (iid in self._opened_iids)

                        if already_opened:
                            logging.info('Popup already opened for %s in this process; skipping open', iid)
                        else:
                            # Wait until the server indicates Neo4j search and enrichment are done
                            try:
                                host = self.web_conf.get("host", "127.0.0.1")
                                port = self.web_conf.get("port", 5000)
                                check_url = f"http://{host}:{port}/incident/{iid}"
                                claim_url = f"http://{host}:{port}/claim_popup"
                                headers_check = {}
                                ui_key = self.web_conf.get('api_key') or os.getenv('UI_API_KEY')
                                if ui_key:
                                    headers_check['X-API-KEY'] = ui_key

                                # poll for completion up to a timeout (in seconds)
                                poll_interval = 1.0
                                max_wait = int(os.getenv('POPUP_WAIT_TIMEOUT') or 30)
                                waited = 0
                                opened = False
                                while waited <= max_wait:
                                    try:
                                        resp = requests.get(check_url, headers=headers_check, timeout=3)
                                    except Exception:
                                        resp = None
                                    if resp and resp.ok:
                                        stored = resp.json()
                                        neo_done = bool(stored.get('neo4j_done'))
                                        enrich_done = bool(stored.get('enrichment_done') or (stored.get('jira_done') and stored.get('confluence_done')))
                                        if neo_done and enrich_done:
                                            # attempt to claim the popup on the central UI
                                            try:
                                                rclaim = requests.post(claim_url, json={'iid': iid}, headers=headers_check, timeout=3)
                                                if rclaim and rclaim.ok and rclaim.json().get('claimed'):
                                                    threading.Thread(target=webbrowser.open, args=(full, 1)).start()
                                                    opened = True
                                                    break
                                                else:
                                                    logging.info('Popup for %s already claimed by another process', iid)
                                                    opened = True
                                                    break
                                            except Exception:
                                                # if claim fails, fall back to opening locally
                                                threading.Thread(target=webbrowser.open, args=(full, 1)).start()
                                                opened = True
                                                break
                                    time.sleep(poll_interval)
                                    waited += poll_interval

                                if not opened:
                                    logging.info('Popup wait timed out after %ss, opening anyway for %s', max_wait, iid)
                                    try:
                                        rclaim = requests.post(claim_url, json={'iid': iid}, headers=headers_check, timeout=3)
                                        if rclaim and rclaim.ok and rclaim.json().get('claimed'):
                                            threading.Thread(target=webbrowser.open, args=(full, 1)).start()
                                        else:
                                            logging.info('Popup for %s already claimed by another process (timeout path)', iid)
                                    except Exception:
                                        threading.Thread(target=webbrowser.open, args=(full, 1)).start()
                            except Exception:
                                logging.exception('Error while waiting for enrichment; opening popup by default')
                                try:
                                    rclaim = requests.post(claim_url, json={'iid': iid}, headers=headers_check, timeout=3)
                                    if rclaim and rclaim.ok and rclaim.json().get('claimed'):
                                        threading.Thread(target=webbrowser.open, args=(full, 1)).start()
                                    else:
                                        logging.info('Popup for %s already claimed by another process (exception path)', iid)
                                except Exception:
                                    threading.Thread(target=webbrowser.open, args=(full, 1)).start()

                    # spawn background enrichment (non-blocking)
                    if iid:
                        threading.Thread(target=self._async_enrich_and_update, args=(iid, search_text, jira_key, llm_data, text), daemon=True).start()
            else:
                logging.error("Failed to post incident to web UI: %s %s", r.status_code, r.text)
        except Exception:
            logging.exception("Error posting to web UI")

    def fetch_incident_data(self, jira_key=None, text=None):
        # Try to find Incident by Jira key first
        with self.neo4j_driver.session() as session:
            if jira_key:
                # In this DB Issues are labeled `Issue`, pages are `Page`, messages are `Message`.
                cypher = """
MATCH (issue:Issue {key:$jira_key})
OPTIONAL MATCH (issue)<-[:ON_ISSUE|REPORTED|POSTED]-(m:Message)
OPTIONAL MATCH (issue)-[:WROTE_COMMENT]->(c:Comment)
OPTIONAL MATCH (issue)-[:IN_PROJECT]->(proj:Project)
OPTIONAL MATCH (page:Page) WHERE page.url CONTAINS $jira_key OR toLower(page.body) CONTAINS toLower($jira_key)
RETURN issue, collect(DISTINCT m) AS messages, collect(DISTINCT c) AS comments, collect(DISTINCT proj) AS projects, collect(DISTINCT page) AS pages
"""
                result = session.run(cypher, jira_key=jira_key)
                record = result.single()
                if record:
                    return self._record_to_dict(record)

            # fallback: try to match by fulltext (adjust to your schema)
            if text:
                cypher2 = """
MATCH (n)
WHERE (n:Issue AND (toLower(n.summary) CONTAINS toLower($text) OR toLower(n.key) CONTAINS toLower($text)))
   OR (n:Page AND (toLower(n.title) CONTAINS toLower($text) OR toLower(n.body) CONTAINS toLower($text)))
   OR (n:Message AND toLower(n.text) CONTAINS toLower($text))
WITH n LIMIT 20
OPTIONAL MATCH (n)<-[:ON_ISSUE|REPORTED|POSTED]-(m:Message)
OPTIONAL MATCH (n)-[:WROTE_COMMENT]->(c:Comment)
OPTIONAL MATCH (n)-[:IN_PROJECT]->(proj:Project)
OPTIONAL MATCH (page:Page) WHERE page.url CONTAINS $text OR toLower(page.body) CONTAINS toLower($text)
RETURN n AS node, collect(DISTINCT m) AS messages, collect(DISTINCT c) AS comments, collect(DISTINCT proj) AS projects, collect(DISTINCT page) AS pages
"""
                result = session.run(cypher2, text=text)
                records = list(result)
                if records:
                    # Return multiple results
                    return self._records_to_dict(records)

            # Additional relaxed fallback: split the search text into tokens and
            # try to match Issue.summary using CONTAINS against any token. This
            # helps short prompts like "BR issues" where the generic fulltext
            # search may miss label-specific fields.
            try:
                toks = [t for t in re.split(r"\W+", text or "") if len(t) > 1]
                if toks:
                    cypher3 = """
MATCH (i:Issue)
WHERE any(tok IN $tokens WHERE toLower(i.summary) CONTAINS toLower(tok))
WITH i LIMIT 20
OPTIONAL MATCH (i)<-[:ON_ISSUE|REPORTED|POSTED]-(m:Message)
OPTIONAL MATCH (i)-[:WROTE_COMMENT]->(c:Comment)
OPTIONAL MATCH (i)-[:IN_PROJECT]->(proj:Project)
OPTIONAL MATCH (page:Page) WHERE page.url CONTAINS head($tokens) OR toLower(page.body) CONTAINS toLower(head($tokens))
RETURN i AS issue, collect(DISTINCT m) AS messages, collect(DISTINCT c) AS comments, collect(DISTINCT proj) AS projects, collect(DISTINCT page) AS pages
"""
                    result = session.run(cypher3, tokens=toks)
                    records = list(result)
                    if records:
                        return self._records_to_dict(records)
            except Exception:
                # best-effort fallback; ignore any errors and continue
                logging.exception('Relaxed summary token search failed')

        return None

    def _post_update_to_ui(self, iid: str, update: dict):
        """Post partial enrichment updates to the web UI (/incident_update).

        Sends X-API-KEY header if UI API key is configured via env or config.
        """
        try:
            host = self.web_conf.get("host", "127.0.0.1")
            port = self.web_conf.get("port", 5000)
            url = f"http://{host}:{port}/incident_update"
            headers = {}
            # prefer configured web UI API key, fall back to env
            ui_key = self.web_conf.get('api_key') or os.getenv('UI_API_KEY')
            if ui_key:
                headers['X-API-KEY'] = ui_key
            payload = {'iid': iid}
            payload.update(update)
            requests.post(url, json=payload, headers=headers, timeout=8)
        except Exception:
            logging.exception("Failed to post enrichment update to web UI")

    def _async_enrich_and_update(self, iid: str, search_text: str, jira_key: str, llm_data: dict, original_text: str):
        """Run Jira/Confluence queries and LLM summarization in background and push results to the web UI."""
        if jc is None:
            logging.debug("Jira/Confluence integration not available; skipping async enrichment")
            return

        try:
            jira_cfg = self.config.get('jira') or {}
            conf_cfg = self.config.get('confluence') or {}
            jira_keys = []
            if llm_data:
                jira_keys = llm_data.get('jira_keys') or []
            if jira_key and jira_key not in jira_keys:
                jira_keys = [jira_key] + jira_keys

            # use LLM-driven jira queries (prev_day + related)
            jira_sets = jc.query_jira_with_llm(jira_cfg, incident_text=original_text, max_results=50)
            jira_results = jira_sets.get('related', [])
            jira_prev_day = jira_sets.get('prev_day', [])
            conf_query_text = (llm_data.get('summary') if llm_data and llm_data.get('summary') else search_text)
            conf_results = jc.query_confluence(conf_cfg, query=conf_query_text, jira_keys=jira_keys)
            suggested = jc.summarize_with_llm((jira_results or []) + (jira_prev_day or []), conf_results, incident_text=original_text)

            update = {
                'jira': jira_results,
                'jira_prev_day': jira_prev_day,
                'confluence': conf_results,
            }
            if suggested:
                update['suggested_resolution'] = suggested

            # post update to web UI
            self._post_update_to_ui(iid, update)
        except Exception:
            logging.exception("Error during async enrichment")

    # --- persistence for last_ts ---
    def _load_last_ts(self):
        try:
            if os.path.exists(self.state_path):
                with open(self.state_path, "r") as f:
                    data = json.load(f)
                    return {k: float(v) for k, v in data.items()}
        except Exception:
            logging.exception("Failed to load last_ts state")
        return None

    def _save_last_ts(self):
        try:
            with open(self.state_path, "w") as f:
                json.dump(self.last_ts, f)
        except Exception:
            logging.exception("Failed to save last_ts state")

    # --- Socket Mode support ---
    def _start_socket_mode(self):
        if not self.app_token:
            logging.error("No app_token provided for Socket Mode")
            return
        if SocketModeClient is None:
            logging.error("SocketModeClient not available in environment")
            return
        self.socket_client = SocketModeClient(app_token=self.app_token, web_client=self.slack)

        def _socket_listener(client, req):
            try:
                if req.type == "events_api":
                    payload = req.payload
                    event = payload.get("event", {})
                    # acknowledge
                    resp = SocketModeResponse(envelope_id=req.envelope_id)
                    client.send_socket_mode_response(resp)

                    if event.get("type") == "message" and not event.get("subtype"):
                        channel = event.get("channel")
                        ts = float(event.get("ts", 0.0))
                        last = self.last_ts.get(channel, 0.0)
                        if ts and ts <= last:
                            return
                        msg = {"text": event.get("text", ""), "user": event.get("user"), "ts": event.get("ts")}
                        self.process_message(channel, msg)
                        self.last_ts[channel] = ts
                        self._save_last_ts()

            except Exception:
                logging.exception("Error in socket listener")

        self.socket_client.socket_mode_request_listeners.append(_socket_listener)
        self.socket_client.connect()

    def _record_to_dict(self, record):
        # record may contain `issue` (from issue query) or `node` (from generic search)
        issue_node = record.get("issue") or record.get("node")
        if not issue_node:
            return None
        node_props = dict(issue_node._properties)

        messages = [dict(n._properties) for n in record.get("messages", [])]
        comments = [dict(n._properties) for n in record.get("comments", [])]
        projects = [dict(n._properties) for n in record.get("projects", [])]
        pages = [dict(n._properties) for n in record.get("pages", [])]
        return {"issue": node_props, "messages": messages, "comments": comments, "projects": projects, "pages": pages}

    def _records_to_dict(self, records):
        """Convert multiple Neo4j records to a dict with lists of issues and related data."""
        issues = []
        all_messages = []
        all_comments = []
        all_projects = []
        all_pages = []
        
        for record in records:
            issue_node = record.get("issue") or record.get("node")
            if issue_node:
                try:
                    node_props = dict(issue_node._properties) if hasattr(issue_node, '_properties') else dict(issue_node)
                    issues.append(node_props)
                except Exception:
                    pass
            
            for m in record.get("messages", []):
                try:
                    props = dict(m._properties) if hasattr(m, '_properties') else dict(m)
                    if props not in all_messages:
                        all_messages.append(props)
                except Exception:
                    pass
            
            for c in record.get("comments", []):
                try:
                    props = dict(c._properties) if hasattr(c, '_properties') else dict(c)
                    if props not in all_comments:
                        all_comments.append(props)
                except Exception:
                    pass
            
            for p in record.get("projects", []):
                try:
                    props = dict(p._properties) if hasattr(p, '_properties') else dict(p)
                    if props not in all_projects:
                        all_projects.append(props)
                except Exception:
                    pass
            
            for pg in record.get("pages", []):
                try:
                    props = dict(pg._properties) if hasattr(pg, '_properties') else dict(pg)
                    if props not in all_pages:
                        all_pages.append(props)
                except Exception:
                    pass
        
        # Return first issue for backward compatibility, but also include all issues
        result = {
            "issue": issues[0] if issues else None,
            "issues": issues,
            "messages": all_messages,
            "comments": all_comments,
            "projects": all_projects,
            "pages": all_pages
        }
        return result


if __name__ == "__main__":
    agent = IncidentAgent()
    agent.start()
