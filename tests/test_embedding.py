from copy import deepcopy

from benchpress_compare.corpus.embedding import search_embedding, verify_embedding
from benchpress_compare.targets import synthetic_target


def workload(width, edges):
    return {
        "schema_version": 1,
        "num_qubits": width,
        "num_clbits": 0,
        "operations": [{"name": "cx", "qargs": list(edge), "cargs": []} for edge in edges],
    }


def test_verified_embedding_certificate_excludes_even_if_vf2_missed_it():
    logical = workload(9, [(i, i + 1) for i in range(8)])
    target = synthetic_target("square", 9)
    result = search_embedding(logical, target)
    assert result["status"] == "verified"
    assert result["zero_routing"]
    assert verify_embedding(logical, target, result["evidence"]["mapping"])["status"] == "verified"
    wrong = list(range(9))
    assert verify_embedding(logical, target, wrong)["status"] == "mismatch"


def test_timeout_is_unknown_never_evidence_of_nonlocality():
    result = search_embedding(
        workload(9, [(i, i + 1) for i in range(8)]), synthetic_target("square", 9), max_nodes=1
    )
    assert result["status"] == "unknown"
    assert "zero_routing" not in result


def test_odd_cycle_has_no_embedding_in_square_bipartite_graph():
    result = search_embedding(workload(3, [(0, 1), (1, 2), (2, 0)]), synthetic_target("square", 3))
    assert result["status"] == "not_found"
    assert result["zero_routing"] is False


def test_certificate_requires_target_nonunitary_support():
    from benchpress_compare.identity import canonical_hash

    logical = workload(2, [(0, 1)])
    logical["operations"].append({"name": "reset", "qargs": [0], "cargs": []})
    target = deepcopy(synthetic_target("square", 2))
    target["instructions"] = [op for op in target["instructions"] if op["name"] != "reset"]
    target["target_hash"] = canonical_hash({k: v for k, v in target.items() if k != "target_hash"})
    assert search_embedding(logical, target)["status"] == "not_found"


def test_preinitialization_multi_qubit_blocks_are_unsupported():
    logical = workload(3, [(0, 1, 2)])
    assert search_embedding(logical, synthetic_target("square", 3))["status"] == "unsupported"
