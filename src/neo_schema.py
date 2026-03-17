"""Neo4j schema helpers.

Provides:
- query_schema(driver) -> (labels, properties, relationship_types)
- sample_nodes(driver, labels, sample_per_label=3) -> dict[label] = [prop-dicts]

This module is intentionally small and dependency-light so it can be imported
by scripts and the main agent code.
"""
from typing import List, Dict, Any, Tuple


def query_schema(driver) -> Tuple[List[str], List[str], List[str]]:
    labels = []
    props = []
    rels = []
    try:
        with driver.session() as s:
            try:
                res = s.run("CALL db.labels() YIELD label RETURN label")
                labels = [r['label'] for r in res]
            except Exception:
                try:
                    res = s.run("CALL db.labels()")
                    labels = [list(r.values())[0] for r in res]
                except Exception:
                    labels = []

            try:
                res = s.run("CALL db.propertyKeys() YIELD propertyKey RETURN propertyKey")
                props = [r['propertyKey'] for r in res]
            except Exception:
                try:
                    res = s.run("CALL db.propertyKeys()")
                    props = [list(r.values())[0] for r in res]
                except Exception:
                    props = []

            try:
                res = s.run("CALL db.relationshipTypes() YIELD relationshipType RETURN relationshipType")
                rels = [r['relationshipType'] for r in res]
            except Exception:
                try:
                    res = s.run("CALL db.relationshipTypes()")
                    rels = [list(r.values())[0] for r in res]
                except Exception:
                    rels = []
    except Exception:
        # If the session open fails, return empty lists
        return [], [], []
    return labels, props, rels


def sample_nodes(driver, labels: List[str], sample_per_label: int = 3) -> Dict[str, List[Dict[str, Any]]]:
    """Return a small sample of nodes (as property dicts) for each label.

    The function is defensive and will skip labels that raise during sampling.
    """
    samples: Dict[str, List[Dict[str, Any]]] = {}
    try:
        with driver.session() as s:
            for label in labels:
                try:
                    q = f"MATCH (n:`{label}`) RETURN n LIMIT {sample_per_label}"
                    res = s.run(q)
                    arr: List[Dict[str, Any]] = []
                    for r in res:
                        # r is typically a mapping like {'n': node}
                        node = None
                        try:
                            node = r.get('n')
                        except Exception:
                            # r might be a record-like object; fall back
                            vals = list(r.values())
                            if vals:
                                node = vals[0]
                        if node is None:
                            continue
                        try:
                            props = dict(node)
                        except Exception:
                            # Some driver Node objects support .items()
                            try:
                                props = {k: v for k, v in node.items()}
                            except Exception:
                                # Last resort: stringify
                                props = {"_repr": str(node)}
                        arr.append(props)
                    if arr:
                        samples[label] = arr
                except Exception:
                    # skip this label on any error
                    continue
    except Exception:
        return {}
    return samples
