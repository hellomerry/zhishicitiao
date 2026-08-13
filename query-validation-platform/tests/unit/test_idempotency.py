from src.pipeline.idempotency import compute_node_key


def test_compute_node_key_stable():
    payload = {"x": 1, "y": "abc"}
    k1 = compute_node_key("task-1", "node_a", payload)
    k2 = compute_node_key("task-1", "node_a", payload)
    assert k1 == k2


def test_compute_node_key_changes_with_payload():
    k1 = compute_node_key("task-1", "node_a", {"x": 1})
    k2 = compute_node_key("task-1", "node_a", {"x": 2})
    assert k1 != k2


def test_compute_node_key_format():
    k = compute_node_key("task-1", "node_a", {})
    assert len(k) == 64  # sha256 hex
