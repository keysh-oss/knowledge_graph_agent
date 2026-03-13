"""Simple Jira and Confluence helper.

This module provides minimal helpers to search Jira and Confluence using REST APIs
and a small LLM summarizer that synthesizes probable cause and resolution steps
from found issues/pages.

Configuration is read from a `config` dict passed from the agent. Expected keys:
- jira: {base_url, user, api_token}
- confluence: {base_url, user, api_token}

These helpers are intentionally defensive: if credentials are missing or a call
fails the function returns an empty list/object rather than raising.
"""
from typing import List, Dict, Optional
import os
import logging
import requests
import json

logger = logging.getLogger(__name__)

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")


def _basic_auth_tuple(cfg: Dict[str, str]):
    if not cfg:
        return None
    user = cfg.get("user") or cfg.get("username")
    token = cfg.get("api_token") or cfg.get("token")
    if user and token:
        return (user, token)
    return None


def query_jira(cfg: Dict, query: Optional[str] = None, jira_keys: Optional[List[str]] = None, max_results: int = 10) -> List[Dict]:
    """Return a list of Jira issues matching jira_keys or free-text query.

    Each issue dict contains at least: key, summary, status, url, assignee, created
    """
    if not cfg:
        return []
    base = cfg.get("base_url") or cfg.get("url")
    if not base:
        return []
    auth = _basic_auth_tuple(cfg)

    headers = {"Accept": "application/json"}

    # prefer exact keys if provided
    if jira_keys:
        keys = ",".join(jira_keys[:50])
        jql = f"key in ({keys})"
    elif query:
        # simple text search fallback
        safe = query.replace('"', '')[:200]
        jql = f'text ~ "{safe}" OR summary ~ "{safe}"'
    else:
        return []

    # Use Atlassian Cloud Search API v3 JQL endpoint: GET /rest/api/3/search/jql
    url = base.rstrip("/") + "/rest/api/3/search/jql"
    # include common fields; caller may override by passing different max_results
    params = {
        "jql": jql,
        "maxResults": max_results,
        "fields": "key,summary,status,assignee,created,updated,labels",
    }
    try:
        r = requests.get(url, params=params, headers=headers, auth=auth, timeout=8)
        if r.status_code != 200:
            logger.warning("Jira search returned %s: %s", r.status_code, r.text[:200])
            return []
        data = r.json()
        issues = []
        for it in data.get("issues", []):
            key = it.get("key")
            f = it.get("fields", {})
            issue = {
                "key": key,
                "summary": f.get("summary"),
                "status": (f.get("status") or {}).get("name"),
                "assignee": (f.get("assignee") or {}).get("displayName"),
                "created": f.get("created"),
                "labels": f.get("labels", []),
                "url": base.rstrip("/") + "/browse/" + key,
            }
            issues.append(issue)
        return issues
    except Exception:
        logger.exception("Error querying Jira")
        return []


def query_jira_with_llm(cfg: Dict, incident_text: str, max_results: int = 50) -> Dict[str, List[Dict]]:
        """Use the LLM to build JQL queries for:
        - tasks executed on the previous day (returned as 'prev_day')
        - related incidents to the incident_text (returned as 'related')

        Returns a dict: { 'prev_day': [...], 'related': [...] }
        Falls back to simple text searches if LLM is unavailable or fails.
        """
        if not cfg:
            return {"prev_day": [], "related": []}

        # compute previous day's YYYY/MM/DD for JQL
        from datetime import datetime, timedelta

        today = datetime.utcnow().date()
        prev = today - timedelta(days=1)
        prev_str = prev.strftime("%Y/%m/%d")
        next_day = prev + timedelta(days=1)
        next_str = next_day.strftime("%Y/%m/%d")

        # If we don't have LLM available, fall back to a heuristic JQL
        if not OPENAI_API_KEY or OpenAI is None:
            # prev day: updated between prev_str and next_str
            prev_jql = f'updated >= "{prev_str}" AND updated < "{next_str}"'
            related_jql = None
            # try to use incident_text for basic text search
            safe = incident_text.replace('"', '')[:200]
            related_jql = f'text ~ "{safe}" OR summary ~ "{safe}"'
            prev_issues = query_jira(cfg, query=None, jira_keys=None, max_results=max_results)
            related_issues = query_jira(cfg, query=safe, jira_keys=None, max_results=max_results)
            return {"prev_day": prev_issues, "related": related_issues}

        # Build prompt for LLM to produce two JQL queries in JSON
        prompt = (
            "You are a helpful assistant that generates Jira JQL queries."
        )
        prompt += (
            f"\nGiven this incident description:\n{incident_text}\n\n"
            "Produce a JSON object with two fields: \n"
            " - prev_day_jql: a JQL query that selects issues that were executed/updated during the previous UTC day (use >= and < with dates in YYYY/MM/DD format).\n"
            " - related_jql: a JQL query that finds issues related to the incident text (by summary, description, or linked issues).\n"
            f"Use the previous day start date: {prev_str} (UTC) and end date: {next_str} (UTC).\n"
            "Return JSON ONLY."
        )

        try:
            client = OpenAI(api_key=OPENAI_API_KEY)
            resp = client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300,
                temperature=0.0,
            )
            content = None
            if hasattr(resp, "choices") and len(resp.choices) > 0:
                c = resp.choices[0]
                if isinstance(c, dict):
                    content = c.get("message", {}).get("content")
                else:
                    content = getattr(getattr(c, "message", None), "content", None)
            if not content:
                content = (resp.get("choices", [])[0].get("message", {}).get("content", "") if isinstance(resp, dict) else "")
            content = (content or "").strip()
            # extract JSON object
            start = content.find("{")
            end = content.rfind("}")
            if start != -1 and end != -1:
                json_text = content[start:end+1]
            else:
                json_text = content
            parsed = json.loads(json_text)
            prev_jql = parsed.get('prev_day_jql')
            related_jql = parsed.get('related_jql')
        except Exception:
            logger.exception("LLM failed to produce JQL; falling back to heuristics")
            prev_jql = f'updated >= "{prev_str}" AND updated < "{next_str}"'
            safe = incident_text.replace('"', '')[:200]
            related_jql = f'text ~ "{safe}" OR summary ~ "{safe}"'

        results = {"prev_day": [], "related": []}
        # run prev_jql
        try:
            if prev_jql:
                results['prev_day'] = _run_jira_jql(cfg, prev_jql, max_results=max_results)
        except Exception:
            logger.exception("Error running prev_day JQL")

        try:
            if related_jql:
                results['related'] = _run_jira_jql(cfg, related_jql, max_results=max_results)
        except Exception:
            logger.exception("Error running related JQL")

        return results


def _run_jira_jql(cfg: Dict, jql: str, max_results: int = 50) -> List[Dict]:
        """Run the given JQL against Jira and return issues list (same shape as query_jira)."""
        base = cfg.get("base_url") or cfg.get("url")
        if not base:
            return []
        auth = _basic_auth_tuple(cfg)
        headers = {"Accept": "application/json"}
        # Use Atlassian Cloud Search API v3 JQL endpoint (GET /rest/api/3/search/jql)
        url = base.rstrip("/") + "/rest/api/3/search/jql"
        params = {
            "jql": jql,
            "maxResults": max_results,
            "fields": "key,summary,status,assignee,created,updated,labels",
        }
        try:
            r = requests.get(url, params=params, headers=headers, auth=auth, timeout=12)
            if r.status_code != 200:
                logger.warning("Jira JQL returned %s: %s", r.status_code, r.text[:200])
                return []
            data = r.json()
            issues = []
            for it in data.get("issues", [])[:max_results]:
                key = it.get("key")
                f = it.get("fields", {})
                issue = {
                    "key": key,
                    "summary": f.get("summary"),
                    "status": (f.get("status") or {}).get("name"),
                    "assignee": (f.get("assignee") or {}).get("displayName"),
                    "created": f.get("created"),
                    "updated": f.get("updated"),
                    "labels": f.get("labels", []),
                    "url": base.rstrip("/") + "/browse/" + key,
                }
                issues.append(issue)
            return issues
        except Exception:
            logger.exception("Error running JQL against Jira")
            return []


def query_confluence(cfg: Dict, query: Optional[str] = None, jira_keys: Optional[List[str]] = None, max_results: int = 6) -> List[Dict]:
    """Search Confluence pages by CQL (simple wrapper). Returns list of pages with title, id, url, excerpt.

    This function uses the Confluence REST API `/rest/api/content/search` with a CQL query.
    """
    if not cfg:
        return []
    base = cfg.get("base_url") or cfg.get("url")
    if not base:
        return []
    auth = _basic_auth_tuple(cfg)
    headers = {"Accept": "application/json"}

    # build CQL
    if jira_keys:
        terms = " OR ".join([f'title ~ "{k}" OR text ~ "{k}"' for k in jira_keys[:10]])
        cql = terms
    elif query:
        safe = query.replace('"', '')[:200]
        cql = f'text ~ "{safe}" OR title ~ "{safe}"'
    else:
        return []

    url = base.rstrip("/") + "/rest/api/content/search"
    params = {"cql": cql, "limit": max_results, "expand": "space,body.view"}
    try:
        r = requests.get(url, params=params, headers=headers, auth=auth, timeout=8)
        if r.status_code != 200:
            logger.warning("Confluence search returned %s: %s", r.status_code, r.text[:200])
            return []
        data = r.json()
        pages = []
        for it in data.get("results", []):
            page_id = it.get("id")
            title = it.get("title")
            space = (it.get("space") or {}).get("key")
            body = (it.get("body") or {}).get("view", {}).get("value")
            # produce a short excerpt by stripping tags naively
            excerpt = None
            if body:
                # crude strip of HTML tags
                excerpt = body.replace('<', ' <')
                # take first 400 chars
                excerpt = (excerpt or '')[:400]
            url_page = base.rstrip("/") + "/pages/" + page_id if page_id else None
            pages.append({"id": page_id, "title": title, "space": space, "excerpt": excerpt, "url": url_page})
        return pages
    except Exception:
        logger.exception("Error querying Confluence")
        return []


def summarize_with_llm(jira_items: List[Dict], conf_pages: List[Dict], incident_text: Optional[str] = None) -> Dict:
    """Call an LLM to synthesize probable cause and resolution steps from Jira/Confluence results.

    Returns: {probable_cause: str, resolution_steps: [str], confidence: float}
    If OPENAI_API_KEY is not present or client missing, returns a best-effort heuristic summary.
    """
    if not OPENAI_API_KEY or OpenAI is None:
        # fallback: minimal heuristic summary
        cause = None
        steps = []
        if len(jira_items) > 0:
            cause = f"Related Jira issues: {', '.join([i.get('key') for i in jira_items[:5]])}"
            steps.append("Review linked Jira issues and their comments for root cause.")
        if len(conf_pages) > 0:
            if not cause:
                cause = f"Related Confluence pages found: {', '.join([p.get('title') for p in conf_pages[:3]])}"
            steps.append("Check Confluence runbooks and known-issues pages for remediation steps.")
        return {"probable_cause": cause, "resolution_steps": steps, "confidence": 0.2}

    # Build a concise prompt
    snippets = []
    if incident_text:
        snippets.append(f"Incident text: {incident_text}\n")
    if jira_items:
        snippets.append("Jira issues:\n" + "\n".join([f"- {i.get('key')}: {i.get('summary')} (status={i.get('status')})" for i in jira_items[:6]]))
    if conf_pages:
        snippets.append("Confluence pages:\n" + "\n".join([f"- {p.get('title')}: {p.get('url') or ''}" for p in conf_pages[:6]]))

    prompt = (
        "You are an SRE assistant. Given the incident text, related Jira issues and Confluence pages, "
        "provide a short probable cause (1-2 sentences) and 3 concise, ordered resolution steps. "
        "Return JSON only with keys: probable_cause (string), resolution_steps (array of strings), confidence (0-1).\n\n"
        + "\n\n".join(snippets)
    )

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.0,
        )
        content = None
        if hasattr(resp, "choices") and len(resp.choices) > 0:
            c = resp.choices[0]
            if isinstance(c, dict):
                content = c.get("message", {}).get("content")
            else:
                content = getattr(getattr(c, "message", None), "content", None)
        if not content:
            content = (resp.get("choices", [])[0].get("message", {}).get("content", "") if isinstance(resp, dict) else "")
        content = (content or "").strip()
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1:
            json_text = content[start:end+1]
        else:
            json_text = content
        parsed = json.loads(json_text)
        return parsed
    except Exception:
        logger.exception("LLM summarization failed")
        # fallback heuristic
        return summarize_with_llm([], [], incident_text)
