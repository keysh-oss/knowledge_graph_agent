import pytest
from unittest.mock import MagicMock

from src.neo_schema import query_schema, sample_nodes


def make_driver_with_session(runs):
    mock_session = MagicMock()
    mock_session.run.side_effect = runs
    cm = MagicMock()
    cm.__enter__.return_value = mock_session
    cm.__exit__.return_value = False
    mock_driver = MagicMock()
    mock_driver.session.return_value = cm
    return mock_driver


def test_query_schema_basic():
    # labels, props, rels returns
    runs = [
        [{'label': 'Issue'}, {'label': 'Service'}],
        [{'propertyKey': 'summary'}, {'propertyKey': 'status'}],
        [{'relationshipType': 'AFFECTS'}],
    ]
    drv = make_driver_with_session(runs)
    labels, props, rels = query_schema(drv)
    assert 'Issue' in labels
    assert 'summary' in props
    assert 'AFFECTS' in rels


def test_sample_nodes_basic():
    # single label with two sample nodes
    node1 = {'n': {'summary': 'DNS issue', 'status': 'Open'}}
    node2 = {'n': {'summary': 'Other', 'status': 'Closed'}}
    runs = [[node1, node2]]
    drv = make_driver_with_session(runs)
    samples = sample_nodes(drv, ['Issue'], sample_per_label=2)
    assert 'Issue' in samples
    assert isinstance(samples['Issue'], list)
    assert samples['Issue'][0]['summary'] == 'DNS issue'
