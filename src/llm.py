"""Lightweight LLM helper to extract structured fields from a Slack message.

Reads OPENAI_API_KEY and OPENAI_MODEL from environment (or .env). Returns a dict with
keys: priority, jira_keys (list), services (list), summary, confidence.
"""
import os
import json
import logging
from dotenv import load_dotenv

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-3.5-turbo")

try:
    # modern OpenAI client (v1+)
    from openai import OpenAI
except Exception:
    OpenAI = None

logger = logging.getLogger(__name__)

PROMPT = (
    "You are a helpful assistant that extracts structured incident information from a Slack message.\n"
    "Given a Slack message, return a JSON object with these fields: \n"
    " - priority: one of [P1, P2, P3] if present or null\n"
    " - jira_keys: array of probable Jira keys found in the text (e.g., [\"ABC-123\"])\n"
    " - services: array of possible service/component names mentioned\n"
    " - summary: a one-line concise summary of the incident\n"
    " - confidence: a number between 0 and 1 indicating how confident you are in the extraction\n"
    "Return JSON ONLY. If you cannot find a field, use null or an empty array as appropriate.\n\n"
    "Message:\n\n"  # message will be appended
)


def extract_incident_fields(text: str, max_tokens: int = 300):
    """Return parsed JSON dict or None on failure."""
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY not set; skipping LLM extraction")
        return None
    if OpenAI is None:
        logger.warning("OpenAI client not available; skipping LLM extraction")
        return None

    # instantiate client with the API key
    try:
        client = OpenAI(api_key=OPENAI_API_KEY) if OpenAI is not None else None
    except Exception:
        client = None

    prompt = PROMPT + text + "\n\nJSON:"
    try:
        if client is None:
            raise RuntimeError("OpenAI client not available")

        # Use the v1-style chat completions API via the OpenAI client
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=0.0,
        )
        # response structure: resp.choices[0].message.content
        content = None
        if hasattr(resp, "choices") and len(resp.choices) > 0:
            choice = resp.choices[0]
            if isinstance(choice, dict):
                content = choice.get("message", {}).get("content")
            else:
                msg = getattr(choice, "message", None)
                content = getattr(msg, "content", None) if msg is not None else None
        if not content:
            content = (resp.get("choices", [])[0].get("message", {}).get("content", "")
                       if isinstance(resp, dict) else "")
        content = (content or "").strip()
        # Try to find the first JSON object in the response
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1:
            json_text = content[start:end+1]
        else:
            json_text = content
        parsed = json.loads(json_text)
        return parsed
    except Exception as e:
        logger.exception("LLM extraction failed: %s", e)
        return None
