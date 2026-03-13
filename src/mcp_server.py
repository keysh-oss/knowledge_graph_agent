"""Minimal MCP server: accepts natural-language queries, asks the LLM to produce
read-only Cypher, validates it, runs it against Neo4j, and returns structured
graph results (nodes + edges) for UI consumption.

This is intended for local/dev use. Add proper auth, TLS and stricter
validation before production.
"""
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from typing import Any, Dict, List, Tuple
import os
import re
import json
import logging
from neo4j import GraphDatabase
from neo4j.exceptions import AuthError as Neo4jAuthError
from openai import OpenAI

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
NEO4J_URI = os.getenv("NEO4J_URI", os.getenv("NEO4J_BOLT", "bolt://127.0.0.1:7687"))
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "neo4j")
MCP_API_KEY = os.getenv("MCP_API_KEY", "changeme")

if not OPENAI_API_KEY:
    logging.warning("OPENAI_API_KEY not set; LLM calls will fail until configured")

client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

app = FastAPI()


class NLQuery(BaseModel):
    q: str


# conservative blacklist of write/mutation keywords
READONLY_BLACKLIST = re.compile(r"\b(create|merge|delete|set|remove|drop|call\s+apoc|call\s+dbms|load)\b", re.I)


def fetch_schema_snapshot(limit_samples: int = 5) -> Dict[str, Any]:
    try:
        with driver.session() as s:
            labels = [r["label"] for r in s.run("CALL db.labels() YIELD label RETURN label")]
            props = [r["propertyKey"] for r in s.run("CALL db.propertyKeys() YIELD propertyKey RETURN propertyKey")]
            samples = s.run("MATCH (n) RETURN labels(n) AS labels, keys(n) AS keys, n LIMIT $l", l=limit_samples).data()
        return {"labels": labels, "props": props, "samples": samples}
    except Neo4jAuthError as e:
        logging.exception("Neo4j authentication failed when fetching schema snapshot")
        # Raise a FastAPI-friendly HTTPException so the endpoint returns a clear error
        raise HTTPException(status_code=503, detail="Neo4j authentication failed: check NEO4J_USER/NEO4J_PASSWORD")
    except Exception:
        logging.exception("Failed to fetch schema snapshot from Neo4j")
        raise HTTPException(status_code=503, detail="Failed to fetch schema snapshot from Neo4j")


def validate_cypher(q: str) -> Tuple[bool, str]:
    if READONLY_BLACKLIST.search(q):
        return False, "contains disallowed keywords"
    # disallow multiple statements separated by semicolon
    if ";" in q.strip().rstrip(";"):
        return False, "multiple statements not allowed"
    # ensure a LIMIT exists to prevent huge returns
    if "limit" not in q.lower():
        q = q.strip().rstrip(";") + " LIMIT 100"
    return True, q


def run_cypher_and_build_graph(query: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
    nodes = {}
    edges = []

    def add_node(n):
        nid = f"{list(n.labels)[0] if n.labels else 'Node'}:{n.id}"
        if nid not in nodes:
            nodes[nid] = {"id": nid, "label": list(n.labels)[0] if n.labels else "Node", "props": dict(n._properties)}
        return nid

    with driver.session() as s:
        res = s.run(query, **(params or {}))
        for record in res:
            # record may contain nodes or lists
            for key, val in record.items():
                # Node
                try:
                    # neo4j Node has .labels and .id
                    if hasattr(val, "labels") and hasattr(val, "id"):
                        nid = add_node(val)
                    elif isinstance(val, list):
                        prev = None
                        for elem in val:
                            if hasattr(elem, "labels") and hasattr(elem, "id"):
                                eid = add_node(elem)
                                # optionally create edge between last and this if natural
                                if prev:
                                    edges.append({"from": prev, "to": eid, "label": key})
                                prev = eid
                except Exception:
                    # fallback: ignore unrecognized types
                    pass

    return {"nodes": list(nodes.values()), "edges": edges}


@app.post("/nl_query")
async def nl_query(payload: NLQuery, request: Request):
    key = request.headers.get("X-MCP-API-KEY")
    if MCP_API_KEY and key != MCP_API_KEY:
        raise HTTPException(status_code=401, detail="unauthorized")

    q = payload.q.strip()
    if not q:
        raise HTTPException(status_code=400, detail="empty query")

    schema = fetch_schema_snapshot()

    prompt = (
        "You are a Cypher expert. Given the following Neo4j schema context:\n"
        f"labels: {schema['labels'][:50]}\n"
        f"properties: {schema['props'][:200]}\n"
        f"sample nodes: {json.dumps(schema['samples'], default=str)}\n\n"
        "Translate the user's natural language request into a read-only Cypher query that returns nodes of interest.\n"
        "Return only the Cypher query text, nothing else.\n"
        "User request:\n"
        f"""{q}""" + "\n\n"
        "Constraints:\n"
        "- DO NOT use CREATE, MERGE, DELETE, SET, REMOVE, DROP, CALL apoc.* or other write operations.\n"
        "- Return nodes and related pages/projects where possible. Add LIMIT 100 unless present.\n"
    )

    if client is None:
        raise HTTPException(status_code=500, detail="LLM client not configured (OPENAI_API_KEY missing)")

    # ask the LLM for a cypher query
    resp = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        messages=[{"role": "system", "content": prompt}],
        max_tokens=512,
        temperature=0.0,
    )

    generated = resp.choices[0].message.content.strip()

    ok, validated = validate_cypher(generated)
    if not ok:
        raise HTTPException(status_code=400, detail=f"unsafe cypher: {validated}")

    try:
        graph = run_cypher_and_build_graph(validated)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"neo4j query failed: {e}")

    return {"cypher": validated, "graph": graph}
