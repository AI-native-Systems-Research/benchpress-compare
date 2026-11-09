"""Bound typed circuit interchange; QPY is not the cross-build contract.

High-level Pauli evolution, arbitrary unitaries and user gate definitions remain
high level. Equality conditions and branch wire maps are explicit. Unknown
semantics are rejected instead of lowering them with one comparison arm.
"""

from __future__ import annotations

import math

from benchpress_compare.metrics import validate_circuit

IR_PROTOCOL = "bound-logical-ir-v1"


class UnsupportedCircuit(ValueError):
    pass


def number(value):
    if getattr(value, "parameters", set()):
        raise UnsupportedCircuit("Logical fixtures must have a frozen parameter binding")
    if isinstance(value, complex):
        return {"complex": [number(value.real), number(value.imag)]}
    result = float(value)
    if not math.isfinite(result):
        raise UnsupportedCircuit("Nonfinite circuit parameter")
    return result


def numeric(value):
    if isinstance(value, dict) and set(value) == {"complex"}:
        return complex(*value["complex"])
    return value


def export_circuit(circuit) -> dict:
    from qiskit.circuit import Clbit, ControlledGate
    from qiskit.circuit.controlflow import ControlFlowOp
    from qiskit.circuit.library import PauliEvolutionGate, UnitaryGate, get_standard_gate_name_mapping

    standard = get_standard_gate_name_mapping()
    output = {
        "schema_version": 1, "protocol": IR_PROTOCOL,
        "num_qubits": circuit.num_qubits, "num_clbits": circuit.num_clbits,
        "global_phase": number(circuit.global_phase),
        "qregs": [{"name": r.name, "bits": [circuit.find_bit(b).index for b in r]} for r in circuit.qregs],
        "cregs": [{"name": r.name, "bits": [circuit.find_bit(b).index for b in r]} for r in circuit.cregs],
        "operations": [],
    }
    for instruction in circuit.data:
        op = instruction.operation
        desc = {
            "name": op.name,
            "qargs": [circuit.find_bit(q).index for q in instruction.qubits],
            "cargs": [circuit.find_bit(c).index for c in instruction.clbits],
            "params": [],
        }
        if isinstance(op, ControlFlowOp):
            if op.name != "if_else":
                raise UnsupportedCircuit(f"Unsupported control-flow operation {op.name}")
            condition = op.condition
            if not isinstance(condition, tuple):
                raise UnsupportedCircuit("Only bit/register equality conditions are supported")
            bits, value = condition
            bits = [bits] if isinstance(bits, Clbit) else list(bits)
            desc.update(kind="if_else", condition={"bits": [circuit.find_bit(b).index for b in bits], "value": int(value)},
                        blocks=[export_circuit(block) for block in op.blocks])
        elif isinstance(op, PauliEvolutionGate):
            operators = op.operator if isinstance(op.operator, list) else [op.operator]
            groups = []
            for operator in operators:
                if not hasattr(operator, "to_list"):
                    from qiskit.quantum_info import SparsePauliOp
                    operator = SparsePauliOp.from_sparse_observable(operator)
                groups.append([[p, number(complex(c).real), number(complex(c).imag)] for p, c in operator.to_list()])
            synthesis = op.synthesis
            if type(synthesis).__name__ not in {"LieTrotter", "SuzukiTrotter"}:
                raise UnsupportedCircuit("Unknown Pauli evolution synthesis policy")
            settings = dict(synthesis.settings)
            if settings.get("atomic_evolution") is not None:
                raise UnsupportedCircuit("Custom evolution callback cannot be frozen")
            desc.update(kind="pauli_evolution", groups=groups, grouped=isinstance(op.operator, list),
                        params=[number(op.time)], synthesis={"class": type(synthesis).__name__, "settings": settings})
        elif isinstance(op, UnitaryGate):
            desc.update(kind="unitary", matrix=[[[number(complex(v).real), number(complex(v).imag)] for v in row] for row in op.to_matrix()])
        elif op.name == "barrier":
            desc["kind"] = "barrier"
        elif op.name == "delay":
            desc.update(kind="delay", params=[number(op.duration)], unit=op.unit)
        elif (op.name in standard and op.base_class is standard[op.name].base_class
              and not (isinstance(op, ControlledGate) and op.ctrl_state != 2 ** op.num_ctrl_qubits - 1)):
            desc.update(kind="standard", params=[number(p) for p in op.params])
        elif op.definition is not None:
            desc.update(kind="definition", params=[number(p) for p in op.params], definition=export_circuit(op.definition))
        else:
            raise UnsupportedCircuit(f"Unknown opaque operation {type(op).__name__}: {op.name}")
        output["operations"].append(desc)
    validate_circuit(output)
    return output


def import_circuit(data: dict):
    from qiskit import QuantumCircuit
    from qiskit.circuit import Qubit, Clbit, QuantumRegister, ClassicalRegister, Gate, Instruction, Barrier, Delay
    from qiskit.circuit.controlflow import IfElseOp
    from qiskit.circuit.library import PauliEvolutionGate, UnitaryGate, get_standard_gate_name_mapping
    from qiskit.quantum_info import SparsePauliOp
    from qiskit.synthesis import LieTrotter, SuzukiTrotter

    validate_circuit(data)
    if data.get("protocol") != IR_PROTOCOL:
        raise UnsupportedCircuit("Unknown logical circuit protocol")
    circuit = QuantumCircuit()
    circuit.add_bits([Qubit() for _ in range(data["num_qubits"])])
    circuit.add_bits([Clbit() for _ in range(data["num_clbits"])])
    for field, cls, bits in (("qregs", QuantumRegister, circuit.qubits), ("cregs", ClassicalRegister, circuit.clbits)):
        for register in data.get(field, []):
            circuit.add_register(cls(name=register["name"], bits=[bits[i] for i in register["bits"]]))
    circuit.global_phase = numeric(data.get("global_phase", 0))
    standard = get_standard_gate_name_mapping()
    for spec in data["operations"]:
        kind, name = spec["kind"], spec["name"]
        params = [numeric(p) for p in spec.get("params", [])]
        if kind == "standard":
            if name not in standard:
                raise UnsupportedCircuit(f"Build lacks standard operation {name}")
            template = standard[name]
            op = template.base_class(*params)
        elif kind == "barrier":
            op = Barrier(len(spec["qargs"]))
        elif kind == "delay":
            op = Delay(params[0], unit=spec["unit"])
        elif kind == "unitary":
            op = UnitaryGate([[complex(*v) for v in row] for row in spec["matrix"]])
        elif kind == "pauli_evolution":
            groups = [SparsePauliOp.from_list([(p, complex(r, i)) for p, r, i in group]) for group in spec["groups"]]
            synth = spec["synthesis"]
            cls = {"LieTrotter": LieTrotter, "SuzukiTrotter": SuzukiTrotter}.get(synth["class"])
            if cls is None:
                raise UnsupportedCircuit("Unknown evolution synthesis")
            op = PauliEvolutionGate(groups if spec["grouped"] else groups[0], time=params[0], synthesis=cls(**synth["settings"]))
        elif kind == "if_else":
            condition = spec["condition"]
            indices = condition["bits"]
            if len(indices) == 1:
                lhs = circuit.clbits[indices[0]]
            else:
                matches = [r for r in circuit.cregs if [circuit.find_bit(c).index for c in r] == indices]
                if len(matches) != 1:
                    raise UnsupportedCircuit("Condition register is not represented exactly")
                lhs = matches[0]
            blocks = [import_circuit(block) for block in spec["blocks"]]
            op = IfElseOp((lhs, condition["value"]), blocks[0], blocks[1] if len(blocks) == 2 else None)
        elif kind == "definition":
            cls = Instruction if spec["cargs"] else Gate
            args = [name, len(spec["qargs"])]
            if spec["cargs"]:
                args.append(len(spec["cargs"]))
            op = cls(*args, params)
            op.definition = import_circuit(spec["definition"])
        else:
            raise UnsupportedCircuit(f"Unknown typed operation {kind}")
        circuit.append(op, spec["qargs"], spec["cargs"])
    return circuit
