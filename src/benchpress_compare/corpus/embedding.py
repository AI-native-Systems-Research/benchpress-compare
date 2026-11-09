"""Bounded zero-routing embedding search with independently checked certificates.

The search uses the actual post-initialization interaction workload. A timeout is
unknown; it cannot establish nonlocality. An exhaustive miss is evidence only
for this workload and target, and never replaces the three-seed SWAP screen.
"""

from __future__ import annotations

import time

from benchpress_compare.identity import canonical_hash
from benchpress_compare.metrics import validate_circuit
from benchpress_compare.targets import validate_target


def interaction_workload(logical: dict) -> dict:
    validate_circuit(logical)
    edges = set()
    requirements = {q: {"rz", "sx"} for q in range(logical["num_qubits"])}
    control_flow = set()

    def visit(body, mapping):
        for op in body["operations"]:
            qs = [mapping[q] for q in op["qargs"]]
            if op["name"] == "barrier":
                continue
            if "blocks" in op:
                if op["name"] != "if_else":
                    raise ValueError("Unsupported control flow in interaction workload")
                control_flow.add(op["name"])
                for block in op["blocks"]:
                    visit(block, qs)
            elif len(qs) > 2:
                raise ValueError(
                    "Embedding search requires the post-initialization two-qubit workload"
                )
            elif len(qs) == 2:
                edges.add(tuple(sorted(qs)))
            elif op["name"] in {"reset", "measure"}:
                for q in qs:
                    requirements[q].add(op["name"])

    visit(logical, list(range(logical["num_qubits"])))
    return {
        "num_qubits": logical["num_qubits"],
        "edges": sorted([list(e) for e in edges]),
        "requirements": {str(q): sorted(names) for q, names in requirements.items()},
        "control_flow": sorted(control_flow),
    }


def _target_graph(target):
    validate_target(target)
    support = {op["name"]: op for op in target["instructions"]}
    # The frozen design has only CZ as a two-qubit basis. CZ is symmetric as a
    # unitary, so either supported ordered operand tuple establishes adjacency.
    if target["native_2q_names"] != ["cz"]:
        raise ValueError("The embedding certificate supports the native-CZ design only")
    edges = set()
    if "cz" not in support:
        raise ValueError("Target has no CZ support")
    for prop in support["cz"]["properties"]:
        qs = prop["qargs"]
        if qs is None:
            edges.update(
                (a, b)
                for a in range(target["num_qubits"])
                for b in range(a + 1, target["num_qubits"])
            )
        else:
            edges.add(tuple(sorted(qs)))
    adjacency = {q: set() for q in range(target["num_qubits"])}
    for a, b in edges:
        adjacency[a].add(b)
        adjacency[b].add(a)
    allowed = {}
    for name, spec in support.items():
        if spec["arity"] == 1:
            allowed[name] = (
                set(range(target["num_qubits"]))
                if any(p["qargs"] is None for p in spec["properties"])
                else {p["qargs"][0] for p in spec["properties"]}
            )
    return adjacency, allowed, support


def verify_embedding(logical: dict, target: dict, mapping: list[int]) -> dict:
    try:
        workload = interaction_workload(logical)
        adjacency, allowed, support = _target_graph(target)
        width = workload["num_qubits"]
        if (
            len(mapping) != width
            or len(set(mapping)) != width
            or any(type(p) is not int or p not in adjacency for p in mapping)
        ):
            return {"status": "mismatch", "reason": "invalid_mapping"}
        for a, b in workload["edges"]:
            if mapping[b] not in adjacency[mapping[a]]:
                return {"status": "mismatch", "reason": "interaction_not_embedded", "edge": [a, b]}
        for q, names in workload["requirements"].items():
            if any(mapping[int(q)] not in allowed.get(name, set()) for name in names):
                return {
                    "status": "mismatch",
                    "reason": "mapped_instruction_unsupported",
                    "logical_qubit": int(q),
                }
        if not set(workload["control_flow"]).issubset(support):
            return {"status": "mismatch", "reason": "control_flow_unsupported"}
        evidence = {
            "schema_version": 1,
            "algorithm": "checked-cz-subgraph-v1",
            "workload_hash": canonical_hash(workload),
            "target_hash": target["target_hash"],
            "mapping": mapping,
            "checked_edges": len(workload["edges"]),
        }
        return {"status": "verified", "zero_routing": True, "evidence": evidence}
    except (KeyError, ValueError, TypeError) as error:
        return {"status": "unsupported", "reason": str(error)}


def search_embedding(
    logical: dict, target: dict, *, budget_seconds: float = 1.0, max_nodes: int = 100_000
) -> dict:
    if budget_seconds <= 0 or max_nodes < 1:
        raise ValueError("Embedding search requires positive time and node budgets")
    started = time.monotonic()
    deadline = started + budget_seconds
    visited = 0
    try:
        workload = interaction_workload(logical)
        target_neighbors, allowed, support = _target_graph(target)
    except (KeyError, ValueError, TypeError) as error:
        return {"status": "unsupported", "reason": str(error)}
    width = workload["num_qubits"]
    neighbors = {q: set() for q in range(width)}
    for a, b in workload["edges"]:
        neighbors[a].add(b)
        neighbors[b].add(a)
    domains = {}
    for q in range(width):
        domain = {p for p in target_neighbors if len(target_neighbors[p]) >= len(neighbors[q])}
        for name in workload["requirements"][str(q)]:
            domain &= allowed.get(name, set())
        domains[q] = domain
    mapping, used = {}, set()

    def backtrack():
        nonlocal visited
        if time.monotonic() >= deadline or visited >= max_nodes:
            raise TimeoutError
        if len(mapping) == width:
            return True
        choices = []
        for q in range(width):
            if q in mapping:
                continue
            available = domains[q] - used
            for neighbor in neighbors[q] & mapping.keys():
                available &= target_neighbors[mapping[neighbor]]
            if not available:
                return False
            choices.append((len(available), -len(neighbors[q]), q, available))
        _, _, q, available = min(choices, key=lambda row: row[:3])
        for p in sorted(available, key=lambda p: (len(target_neighbors[p]), p)):
            visited += 1
            mapping[q] = p
            used.add(p)
            if backtrack():
                return True
            used.remove(p)
            del mapping[q]
        return False

    try:
        found = (
            False
            if width > target["num_qubits"] or not set(workload["control_flow"]).issubset(support)
            else backtrack()
        )
    except TimeoutError:
        return {
            "status": "unknown",
            "reason": "embedding_search_budget",
            "visited_nodes": visited,
            "budget_seconds": budget_seconds,
            "max_nodes": max_nodes,
            "elapsed_seconds": time.monotonic() - started,
        }
    if found:
        result = verify_embedding(logical, target, [mapping[q] for q in range(width)])
    else:
        result = {
            "status": "not_found",
            "zero_routing": False,
            "reason": "bounded_search_exhausted_graph",
            "evidence": {
                "workload_hash": canonical_hash(workload),
                "target_hash": target["target_hash"],
            },
        }
    result.update(
        visited_nodes=visited,
        elapsed_seconds=time.monotonic() - started,
        budget_seconds=budget_seconds,
        max_nodes=max_nodes,
    )
    return result
