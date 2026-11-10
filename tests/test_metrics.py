import pytest

from benchpress_compare.metrics import (
    UnsupportedMetric,
    active_qubits,
    measure,
    structural_signature,
)


def circuit(n, operations, c=0):
    return {"schema_version": 1, "num_qubits": n, "num_clbits": c, "operations": operations}


def op(name, q, c=(), **kwargs):
    return {"name": name, "qargs": list(q), "cargs": list(c), **kwargs}


def test_parallel_and_serial_weighted_depth():
    assert measure(circuit(4, [op("cz", (0, 1)), op("cz", (2, 3))]))["D2"] == 1
    value = measure(
        circuit(4, [op("cz", (0, 1)), op("x", (1,)), op("cz", (1, 2)), op("cz", (2, 3))])
    )
    assert (value["N2"], value["D2"]) == (3, 3)


def test_barrier_orders_without_counting():
    value = measure(circuit(4, [op("cz", (0, 1)), op("barrier", (0, 1, 2, 3)), op("cz", (2, 3))]))
    assert (value["N2"], value["D2"]) == (2, 2)


def test_operation_names_do_not_conceal_custom_gate_work():
    body = circuit(2, [op("barrier", (0, 1), kind="unitary")])
    assert active_qubits(body) == {0, 1}
    assert len(structural_signature(body)) == 1
    with pytest.raises(UnsupportedMetric):
        measure(body)
    with pytest.raises(UnsupportedMetric):
        measure(circuit(2, [op("cz", (0, 1), kind="unitary")]))


def test_recursive_size_counts_both_branches_depth_uses_maximum():
    true = circuit(3, [op("cz", (0, 1)), op("cz", (1, 2))], 1)
    false = circuit(3, [op("cz", (0, 2))], 1)
    body = circuit(
        4,
        [
            op("cz", (0, 1)),
            op("measure", (1,), (0,)),
            op(
                "if_else",
                (3, 1, 2),
                (0,),
                condition={"bits": [0], "value": 1},
                blocks=[true, false],
            ),
            op("cz", (0, 3)),
        ],
        1,
    )
    value = measure(body)
    assert (value["N2"], value["D2"]) == (5, 4)


def test_single_qubit_container_is_not_two_qubit_instruction():
    body = circuit(
        2,
        [
            op(
                "if_else",
                (0, 1),
                (0,),
                condition={"bits": [0], "value": 0},
                blocks=[circuit(2, [op("x", (0,))], 1)],
            )
        ],
        1,
    )
    assert measure(body)["N2"] == 0
    assert active_qubits(body) == {0}


def test_classical_dependency_links_disjoint_quantum_wires():
    body = circuit(
        4,
        [
            op("cz", (0, 1)),
            op("measure", (0,), (0,)),
            op(
                "if_else",
                (2, 3),
                (0,),
                condition={"bits": [0], "value": 1},
                blocks=[circuit(2, [op("cz", (0, 1))], 1)],
            ),
        ],
        1,
    )
    assert measure(body)["D2"] == 2


def test_unbounded_loop_and_non_native_gate_are_not_valid_measurements():
    with pytest.raises(UnsupportedMetric):
        measure(circuit(2, [op("while_loop", (0, 1), blocks=[circuit(2, [])])]))
    with pytest.raises(UnsupportedMetric):
        measure(circuit(2, [op("cx", (0, 1))]))


def test_structural_signature_ignores_angles_but_not_order_or_block_maps():
    a = circuit(
        3, [op("ry", (0,), params=[0.2]), op("rzz", (0, 1), params=[1.0]), op("cx", (1, 2))]
    )
    b = circuit(3, [op("rzz", (0, 1), params=[5.0]), op("cx", (1, 2))])
    assert structural_signature(a) == structural_signature(b)
    assert structural_signature(a) != structural_signature(
        circuit(3, list(reversed(b["operations"])))
    )


def test_active_width_includes_reset_and_measurement_excludes_barriers():
    assert active_qubits(
        circuit(5, [op("reset", (1,)), op("measure", (3,), (0,)), op("barrier", range(5))], 1)
    ) == {1, 3}
