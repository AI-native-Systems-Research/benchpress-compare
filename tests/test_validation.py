import pytest

from benchpress_compare.schema import PROTOCOLS
from benchpress_compare.validation import (
    REQUIRED_CHECKS,
    VALIDATION_PROTOCOL,
    summarize_validation,
    target_legality,
)
from benchpress_compare.targets import synthetic_target


def circuit(n, operations, c=0):
    return {"schema_version": 1, "num_qubits": n, "num_clbits": c, "operations": operations}


def op(name, q, c=(), **kw):
    return {"name": name, "qargs": list(q), "cargs": list(c), "params": [], **kw}


def test_illegal_native_edge():
    target = synthetic_target("square", 4)
    assert target_legality(circuit(4, [op("cz", [0, 3])]), target)["status"] == "mismatch"
    assert target_legality(circuit(4, [op("cz", [0, 1])]), target)["status"] == "verified"


def test_custom_gates_cannot_impersonate_native_operations():
    target = synthetic_target("square", 4)
    for name in ("cz", "barrier"):
        malicious = circuit(4, [op(name, [0, 1], kind="unitary", matrix=[])])
        assert target_legality(malicious, target)["status"] == "mismatch"


def test_target_legality_checks_physical_edges_inside_branches():
    target = synthetic_target("square", 4)
    block = circuit(2, [op("cz", [0, 1])], 1)
    branch = op("if_else", [0, 1], [0], condition={"bits": [0], "value": 1}, blocks=[block])
    output = circuit(4, [branch], 1)
    assert target_legality(output, target)["status"] == "verified"
    branch["qargs"] = [0, 3]
    assert target_legality(output, target)["reason"] == "illegal_directed_qargs"


def test_validation_summary_requires_only_target_legality():
    summary = summarize_validation({"target_legality": {"status": "verified"}})
    assert summary["status"] == "verified"
    assert summary["required_checks"] == ["target_legality"]
    assert summary["checks"]["target_legality"]["required"] is True
    assert "semantic_coverage" not in summary
    assert REQUIRED_CHECKS == ("target_legality",)
    assert VALIDATION_PROTOCOL == PROTOCOLS["validation"] == "target-legality-v4"
    assert summarize_validation({})["status"] == "error"


@pytest.mark.parametrize("status", ["mismatch", "unsupported", "error"])
def test_target_legality_must_be_verified(status):
    summary = summarize_validation({"target_legality": {"status": status, "required": False}})
    assert summary["status"] == status
    assert summary["checks"]["target_legality"]["required"] is True
