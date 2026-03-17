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
            # prev day: issues with Start Date on the previous day
            prev_jql = f'"Start Date" >= "{prev_str}" AND "Start Date" < "{next_str}"'
            related_jql = None
            # try to use incident_text for basic text search
            safe = incident_text.replace('"', '')[:200]
            related_jql = f'text ~ "{safe}" OR summary ~ "{safe}"'
            prev_issues = query_jira(cfg, query=None, jira_keys=None, max_results=max_results)
            related_issues = query_jira(cfg, query=safe, jira_keys=None, max_results=max_results)
            return {"prev_day": prev_issues, "related": related_issues}

        # Build prompt for LLM to produce two JQL queries in JSON
        prompt = (
            "You are a helpful assistant that generates Jira JQL queries. "
            "Return ONLY a valid JSON object with no additional text or markdown.\n\n"
            f"Given this incident description:\n{incident_text}\n\n"
            "Produce a JSON object with two fields:\n"
            f'- "prev_day_jql": a JQL query using the custom field "Start Date" to find issues started on {prev_str}. Example: \'"Start Date" >= "{prev_str}" AND "Start Date" < "{next_str}"\'\n'
            '- "related_jql": a JQL query to find issues related to the incident by summary or description text.\n\n'
            "Return JSON ONLY, no markdown code blocks."
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
            
            # Remove markdown code blocks if present
            import re
            code_block_match = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', content)
            if code_block_match:
                content = code_block_match.group(1).strip()
            
            # extract JSON object
            start = content.find("{")
            end = content.rfind("}")
            if start != -1 and end != -1:
                json_text = content[start:end+1]
            else:
                json_text = content
            
            logger.debug(f"LLM JQL response: {json_text[:500]}")
            parsed = json.loads(json_text)
            prev_jql = parsed.get('prev_day_jql')
            related_jql = parsed.get('related_jql')
            logger.info(f"Generated JQL - prev_day: {prev_jql}, related: {related_jql}")
        except Exception:
            logger.exception("LLM failed to produce JQL; falling back to heuristics")
            prev_jql = f'"Start Date" >= "{prev_str}" AND "Start Date" < "{next_str}"'
            safe = incident_text.replace('"', '')[:200]
            related_jql = f'text ~ "{safe}" OR summary ~ "{safe}"'
            logger.info(f"Fallback JQL - prev_day: {prev_jql}, related: {related_jql}")

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


def query_confluence(cfg: Dict, query: Optional[str] = None, jira_keys: Optional[List[str]] = None, max_results: int = 20) -> List[Dict]:
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
    params = {"cql": cql, "limit": max_results, "expand": "space,body.view,_links"}
    try:
        r = requests.get(url, params=params, headers=headers, auth=auth, timeout=8)
        if r.status_code != 200:
            logger.warning("Confluence search returned %s: %s", r.status_code, r.text[:200])
            return []
        data = r.json()
        pages = []
        # Get base URL from response _links.base (e.g., https://instance.atlassian.net/wiki)
        # This is the authoritative base for constructing page URLs
        api_base = data.get("_links", {}).get("base")
        if not api_base:
            # Fallback: use configured base_url
            api_base = base.rstrip("/")
        
        for it in data.get("results", []):
            page_id = it.get("id")
            title = it.get("title")
            space_obj = it.get("space") or {}
            space_key = space_obj.get("key")
            space_name = space_obj.get("name")
            body = (it.get("body") or {}).get("view", {}).get("value")
            # produce a short excerpt by stripping tags naively
            excerpt = None
            if body:
                # crude strip of HTML tags
                import re
                excerpt = re.sub(r'<[^>]+>', ' ', body)
                # take first 400 chars
                excerpt = (excerpt or '')[:400].strip()
            
            # Build proper Confluence URL - prefer _links.webui from API response
            links = it.get("_links") or {}
            webui = links.get("webui")  # e.g., /spaces/SPACE/pages/12345/Page+Title
            if webui:
                # webui is a relative path, prepend the base URL from API response
                # api_base is already the full base (e.g., https://instance.atlassian.net/wiki)
                url_page = api_base + webui
            elif page_id and space_key:
                # Construct URL: {base}/spaces/{space}/pages/{id}
                url_page = api_base + f"/spaces/{space_key}/pages/{page_id}"
            elif page_id:
                # Fallback: use Confluence viewpage endpoint
                url_page = api_base + f"/pages/viewpage.action?pageId={page_id}"
            else:
                url_page = None
            
            pages.append({
                "id": page_id, 
                "title": title, 
                "space": {"key": space_key, "name": space_name} if space_key else None,
                "excerpt": excerpt, 
                "url": url_page,
                "_links": links  # preserve links for template
            })
        return pages
    except Exception:
        logger.exception("Error querying Confluence")
        return []


def summarize_with_llm(jira_items: List[Dict], conf_pages: List[Dict], incident_text: Optional[str] = None) -> Dict:
    """Call an LLM to synthesize probable causes and resolution steps from Jira/Confluence results.

    Returns: {probable_causes: [str], resolution_steps: [str], confidence: float}
    If OPENAI_API_KEY is not present or client missing, returns a best-effort heuristic summary.
    """
    if not OPENAI_API_KEY or OpenAI is None:
        # fallback: minimal heuristic summary - include ALL items
        causes = []
        steps = []
        if len(jira_items) > 0:
            # Add each Jira issue as a potential cause
            for item in jira_items:
                causes.append(f"Related Jira issue {item.get('key')}: {item.get('summary')}")
            steps.append("Review linked Jira issues and their comments for root cause.")
            steps.append("Check issue history and related tickets for patterns.")
            steps.append("Verify if similar issues were resolved before.")
        if len(conf_pages) > 0:
            # Add Confluence pages as sources
            for page in conf_pages:
                causes.append(f"See Confluence page: {page.get('title')}")
            steps.append("Check Confluence runbooks and known-issues pages for remediation steps.")
            steps.append("Review documentation for troubleshooting procedures.")
        return {"probable_causes": causes, "resolution_steps": steps, "confidence": 0.2}

    # Build a concise prompt - include ALL items (up to reasonable limit to avoid token overflow)
    snippets = []
    if incident_text:
        snippets.append(f"Incident text: {incident_text}\n")
    if jira_items:
        # Include all Jira items (up to 20 to avoid token overflow)
        jira_list = [f"- {i.get('key')}: {i.get('summary')} (status={i.get('status')})" for i in jira_items[:20]]
        snippets.append("Jira issues:\n" + "\n".join(jira_list))
    if conf_pages:
        # Include all Confluence pages (up to 15 to avoid token overflow)
        conf_list = [f"- {p.get('title')}: {p.get('url') or ''}" for p in conf_pages[:15]]
        snippets.append("Confluence pages:\n" + "\n".join(conf_list))

    prompt = (
        "You are an SRE assistant analyzing an incident. Given the incident text, related Jira issues and Confluence pages:\n\n"
        "1. Identify ALL probable causes (not just one). List each cause separately.\n"
        "2. Provide ALL applicable resolution steps - do NOT limit to 3 steps. Include every actionable step needed.\n"
        "3. Be comprehensive - if there are 10 relevant steps, include all 10.\n\n"
        "Return JSON with keys:\n"
        "- probable_causes: array of strings (ALL identified causes)\n"
        "- resolution_steps: array of strings (ALL steps, not limited)\n"
        "- confidence: number 0-1\n\n"
        + "\n\n".join(snippets)
    )

    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000,  # Increased to allow more resolution steps
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
